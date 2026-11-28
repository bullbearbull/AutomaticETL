"""파서 어댑터 인터페이스·렌더러 단위 테스트.

외부 바이너리(pypdfium2·pdftoppm) 없이 가짜 모듈·가짜 subprocess로
렌더러의 명령 구성·크롭 좌표 계산·오류 폴백과 널 어댑터 계약을 검증한다.
"""

from __future__ import annotations

import dataclasses
import importlib
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from autoetl.parser import (
    BBox,
    ElementKind,
    LayoutDetectionInput,
    NoopLayoutDetector,
    NoopVisionAdapter,
    ParseOptions,
    ParseStrategy,
    VisionRequest,
)
from autoetl.parser.adapters import (
    PdftoppmPageRenderer,
    Pypdfium2PageRenderer,
    UnavailablePageRenderer,
    _read_png_dimensions,
    resolve_default_page_renderer,
)
from autoetl.parser.image_io import encode_png


def _bbox(x0: float, top: float, x1: float, bottom: float) -> BBox:
    return BBox(
        x0=x0,
        top=top,
        x1=x1,
        bottom=bottom,
        norm_x0=0.0,
        norm_top=0.0,
        norm_x1=1.0,
        norm_bottom=1.0,
    )


def _request() -> VisionRequest:
    return VisionRequest(
        region_id="r1",
        page=1,
        element_kind=ElementKind.FIGURE_IMAGE,
        recommended_strategy=ParseStrategy.EMBEDDED_IMAGE_OCR,
        bbox=_bbox(10.0, 20.0, 110.0, 120.0),
        reason="합성 요청",
    )


# ---------------------------------------------------------------------------
# 1. 널 어댑터 계약
# ---------------------------------------------------------------------------


def test_noop_adapters_and_unavailable_renderer() -> None:
    """Noop 레이아웃/비전 어댑터와 널 렌더러의 이름·가용성·반환값을 검증한다."""
    det = NoopLayoutDetector()
    assert det.name == "none"
    assert det.available is True
    inp = LayoutDetectionInput(
        page=1,
        image_bytes=b"\x89PNG\r\n\x1a\n",
        image_extension=".png",
        image_width_px=2,
        image_height_px=2,
        page_width_pt=612.0,
        page_height_pt=792.0,
    )
    assert det.detect(inp) == []

    vision = NoopVisionAdapter()
    assert vision.name == "noop"
    res = vision.parse_region(_request())
    assert res.adapter_name == "noop"
    assert res.succeeded is False
    assert res.error == "vision_adapter_not_configured"

    renderer = UnavailablePageRenderer()
    assert renderer.name == "none"
    assert renderer.available is False
    assert renderer.render_region(Path("아무파일.pdf"), 1, None) is None


def test_parse_options_defaults_and_frozen() -> None:
    """ParseOptions 기본값과 불변성(frozen)을 검증한다."""
    opts = ParseOptions()
    assert opts.pages is None
    assert opts.extract_crops is True
    assert opts.crop_dir is None
    assert opts.render_dpi == 150
    assert opts.detect_borderless_tables is True
    assert opts.detect_running_headers_footers is True
    assert opts.include_header_footer_in_markdown is False
    assert opts.layout_detect_all_pages is False
    assert opts.include_speaker_notes is True
    assert opts.include_comments is False
    with pytest.raises(dataclasses.FrozenInstanceError):
        opts.render_dpi = 300  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 2. PNG 치수 파싱
# ---------------------------------------------------------------------------


def test_read_png_dimensions_valid_short_and_non_png() -> None:
    """유효 PNG는 치수를, 짧은 데이터·비PNG 매직은 (None, None)을 반환한다."""
    png = encode_png(bytes([10, 20, 30] * 6), 3, 2, channels=3)
    assert _read_png_dimensions(png) == (3, 2)
    assert _read_png_dimensions(png[:20]) == (None, None)  # 매직은 맞지만 24바이트 미만
    assert _read_png_dimensions(b"\x89PNG\r\n\x1a\n") == (None, None)
    assert _read_png_dimensions(b"\xff\xd8\xff\xe0" + b"\x00" * 30) == (None, None)


# ---------------------------------------------------------------------------
# 3. Pypdfium2PageRenderer (가짜 pypdfium2 모듈)
# ---------------------------------------------------------------------------


class _FakeBitmap:
    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self.buffer = bytes([30, 20, 10, 255] * (width * height))  # BGRA
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakePdfiumPage:
    def __init__(self, width: float, height: float, *, fail_render: bool = False) -> None:
        self._size = (width, height)
        self._fail_render = fail_render
        self.render_calls: list[tuple[float, tuple[float, float, float, float]]] = []
        self.bitmaps: list[_FakeBitmap] = []
        self.closed = False

    def get_size(self) -> tuple[float, float]:
        return self._size

    def render(self, *, scale: float, crop: tuple[float, float, float, float]) -> _FakeBitmap:
        if self._fail_render:
            raise RuntimeError("가짜 렌더 실패")
        self.render_calls.append((scale, crop))
        bitmap = _FakeBitmap(int(self._size[0] * scale), int(self._size[1] * scale))
        self.bitmaps.append(bitmap)
        return bitmap

    def close(self) -> None:
        self.closed = True


def _install_fake_pdfium(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fail_render: bool = False,
) -> list[Any]:
    """가짜 pypdfium2 모듈을 설치하고 생성된 문서 객체 목록을 반환한다."""
    created: list[Any] = []

    class _FakeDoc:
        def __init__(self, path: str) -> None:
            self.path = path
            self.page = _FakePdfiumPage(612.0, 792.0, fail_render=fail_render)
            self.closed = False
            created.append(self)

        def __getitem__(self, index: int) -> _FakePdfiumPage:
            assert index == 0  # page_number=1 -> index 0
            return self.page

        def close(self) -> None:
            self.closed = True

    fake_module = SimpleNamespace(PdfDocument=_FakeDoc)
    orig_import = importlib.import_module

    def _fake_import(name: str) -> Any:
        if name == "pypdfium2":
            return fake_module
        return orig_import(name)

    monkeypatch.setattr(importlib, "import_module", _fake_import)
    return created


def _install_missing_pdfium(monkeypatch: pytest.MonkeyPatch) -> None:
    orig_import = importlib.import_module

    def _fake_import(name: str) -> Any:
        if name == "pypdfium2":
            raise ImportError("가짜 미설치 pypdfium2")
        return orig_import(name)

    monkeypatch.setattr(importlib, "import_module", _fake_import)


def test_pypdfium2_renderer_unavailable_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """pypdfium2 미설치 시 available=False, 렌더 결과는 None."""
    _install_missing_pdfium(monkeypatch)
    renderer = Pypdfium2PageRenderer()
    assert renderer.name == "pypdfium2"
    assert renderer.available is False
    assert renderer.render_region(Path("문서.pdf"), 1, None) is None


def test_pypdfium2_renderer_full_page_png_and_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    """전체 페이지 렌더: DPI 스케일·PNG 인코딩·리소스 정리를 검증한다."""
    created = _install_fake_pdfium(monkeypatch)
    renderer = Pypdfium2PageRenderer()
    assert renderer.available is True

    img = renderer.render_region(Path("문서.pdf"), 1, None, dpi=144)
    assert img is not None
    assert img.extension == ".png"
    assert img.data.startswith(b"\x89PNG\r\n\x1a\n")
    assert img.width == 1224  # 612pt * (144/72)
    assert img.height == 1584  # 792pt * 2

    doc = created[0]
    assert doc.path == "문서.pdf"
    assert doc.page.render_calls == [(2.0, (0.0, 0.0, 0.0, 0.0))]
    assert doc.closed is True
    assert doc.page.closed is True
    assert doc.page.bitmaps[0].closed is True


def test_pypdfium2_renderer_bbox_crop_coordinates(monkeypatch: pytest.MonkeyPatch) -> None:
    """부분 bbox는 (left, bottom, right, top) 크롭으로, 좁은 bbox는 크롭 없이 렌더한다."""
    created = _install_fake_pdfium(monkeypatch)
    renderer = Pypdfium2PageRenderer()

    # 페이지 612x792, bbox (100,50)~(400,300)
    img = renderer.render_region(Path("문서.pdf"), 1, _bbox(100.0, 50.0, 400.0, 300.0))
    assert img is not None
    assert created[0].page.render_calls[0][1] == (100.0, 492.0, 212.0, 50.0)

    # 폭 0.5pt의 좁은 bbox: left+right >= page_w-2 -> 크롭 생략
    img2 = renderer.render_region(Path("문서.pdf"), 1, _bbox(0.5, 0.5, 1.0, 1.0))
    assert img2 is not None
    assert created[1].page.render_calls[0][1] == (0.0, 0.0, 0.0, 0.0)


def test_pypdfium2_renderer_render_failure_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """렌더 중 예외가 나면 None을 반환하고 문서·페이지 리소스는 정리한다."""
    created = _install_fake_pdfium(monkeypatch, fail_render=True)
    renderer = Pypdfium2PageRenderer()
    assert renderer.render_region(Path("문서.pdf"), 1, None) is None
    assert created[0].closed is True
    assert created[0].page.closed is True


# ---------------------------------------------------------------------------
# 4. PdftoppmPageRenderer (가짜 subprocess)
# ---------------------------------------------------------------------------


def _fake_pdftoppm_run(
    calls: list[list[str]],
    png: bytes,
    *,
    returncode: int = 0,
    write_output: bool = True,
    raises: Exception | None = None,
) -> Any:
    def _run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append(cmd)
        if raises is not None:
            raise raises
        if write_output:
            Path(cmd[-1] + ".png").write_bytes(png)
        return subprocess.CompletedProcess(cmd, returncode, stdout=b"", stderr=b"")

    return _run


def test_pdftoppm_renderer_unavailable_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """pdftoppm 바이너리가 없으면 available=False, 렌더 결과는 None."""

    def _which(name: str) -> str | None:
        return None

    monkeypatch.setattr(shutil, "which", _which)
    renderer = PdftoppmPageRenderer()
    assert renderer.name == "pdftoppm"
    assert renderer.available is False
    assert renderer.render_region(Path("문서.pdf"), 1, None) is None


def test_pdftoppm_renderer_command_and_png_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """명령 인자(-png -r -f -l -singlefile)와 PNG 치수 판독을 검증한다."""
    png = encode_png(bytes([1, 2, 3] * 8), 4, 2, channels=3)
    calls: list[list[str]] = []

    def _which(name: str) -> str | None:
        return "/fake/bin/pdftoppm" if name == "pdftoppm" else None

    monkeypatch.setattr(shutil, "which", _which)
    monkeypatch.setattr(subprocess, "run", _fake_pdftoppm_run(calls, png))
    renderer = PdftoppmPageRenderer()
    assert renderer.available is True

    src = tmp_path / "문서.pdf"
    img = renderer.render_region(src, 2, None, dpi=150)
    assert img is not None
    assert img.data == png
    assert img.extension == ".png"
    assert (img.width, img.height) == (4, 2)

    cmd = calls[0]
    assert cmd[0] == "/fake/bin/pdftoppm"
    assert cmd[1:9] == ["-png", "-r", "150", "-f", "2", "-l", "2", "-singlefile"]
    assert cmd[-2] == str(src)
    assert "-x" not in cmd  # bbox 없음 -> 크롭 인자 없음


def test_pdftoppm_renderer_bbox_crop_args(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """bbox 크롭 인자는 DPI 스케일 반올림 픽셀 좌표로, 4pt 미만 bbox는 생략한다."""
    png = encode_png(bytes([9, 9, 9] * 4), 2, 2, channels=3)
    calls: list[list[str]] = []

    def _which(name: str) -> str | None:
        return "/fake/bin/pdftoppm"

    monkeypatch.setattr(shutil, "which", _which)
    monkeypatch.setattr(subprocess, "run", _fake_pdftoppm_run(calls, png))
    renderer = PdftoppmPageRenderer()

    # dpi=144 -> scale=2: (10,20)~(110,70) -> x=20 y=40 W=200 H=100
    renderer.render_region(tmp_path / "a.pdf", 1, _bbox(10.0, 20.0, 110.0, 70.0), dpi=144)
    cmd = calls[0]
    crop_idx = cmd.index("-x")
    assert cmd[crop_idx : crop_idx + 8] == ["-x", "20", "-y", "40", "-W", "200", "-H", "100"]

    # 폭 2pt(<4) bbox -> 크롭 인자 생략
    renderer.render_region(tmp_path / "a.pdf", 1, _bbox(10.0, 20.0, 12.0, 70.0))
    assert "-x" not in calls[1]


@pytest.mark.parametrize(
    ("returncode", "write_output", "raises"),
    [
        (1, False, None),  # 비정상 종료
        (0, False, None),  # 종료코드 0이지만 출력 파일 없음
        (0, False, OSError("가짜 실행 오류")),
        (0, False, subprocess.TimeoutExpired(cmd=["pdftoppm"], timeout=30)),
    ],
)
def test_pdftoppm_renderer_failure_paths_return_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    write_output: bool,
    raises: Exception | None,
) -> None:
    """실행 실패·출력 누락·OSError·타임아웃은 모두 None으로 폴백한다."""
    calls: list[list[str]] = []

    def _which(name: str) -> str | None:
        return "/fake/bin/pdftoppm"

    monkeypatch.setattr(shutil, "which", _which)
    monkeypatch.setattr(
        subprocess,
        "run",
        _fake_pdftoppm_run(
            calls, b"", returncode=returncode, write_output=write_output, raises=raises
        ),
    )
    renderer = PdftoppmPageRenderer()
    assert renderer.render_region(tmp_path / "문서.pdf", 1, None) is None


# ---------------------------------------------------------------------------
# 5. 기본 렌더러 탐색 우선순위
# ---------------------------------------------------------------------------


def test_resolve_default_renderer_prefers_pdfium(monkeypatch: pytest.MonkeyPatch) -> None:
    """pypdfium2가 있으면 최우선으로 선택한다."""
    _install_fake_pdfium(monkeypatch)
    assert isinstance(resolve_default_page_renderer(), Pypdfium2PageRenderer)


def test_resolve_default_renderer_falls_back_to_pdftoppm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pypdfium2가 없고 pdftoppm이 있으면 폴백 렌더러를 선택한다."""
    _install_missing_pdfium(monkeypatch)

    def _which(name: str) -> str | None:
        return "/fake/bin/pdftoppm" if name == "pdftoppm" else None

    monkeypatch.setattr(shutil, "which", _which)
    assert isinstance(resolve_default_page_renderer(), PdftoppmPageRenderer)


def test_resolve_default_renderer_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """둘 다 없으면 널 렌더러를 반환한다."""
    _install_missing_pdfium(monkeypatch)

    def _which(name: str) -> str | None:
        return None

    monkeypatch.setattr(shutil, "which", _which)
    renderer = resolve_default_page_renderer()
    assert isinstance(renderer, UnavailablePageRenderer)
    assert renderer.available is False
