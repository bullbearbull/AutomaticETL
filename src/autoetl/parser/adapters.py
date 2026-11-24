"""Auto Parser 어댑터 인터페이스 — 문서 형식, 시각(OCR/VLM) 파서, 페이지 렌더러.

1차 구현은 PDF 결정론 파싱 + 자동 라우팅 + 크롭 추출까지 완성하되,
- 다음 단계의 PPTX/DOCX/HWPX는 `DocumentFormatAdapter`로,
- 이후 로컬/외부 OCR·VLM 모델은 `VisionParserAdapter`로
핵심 파이프라인을 건드리지 않고 그대로 끼워 넣을 수 있게 경계를 고정한다.
"""

from __future__ import annotations

import importlib
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from autoetl.core.errors import InputError
from autoetl.parser.contracts import (
    BBox,
    ElementKind,
    ParsedDocument,
    ParseStrategy,
    RoutingPlan,
    SourceFormat,
)
from autoetl.parser.image_io import EncodedImage, bgra_to_rgba, encode_png


class ParserInputError(InputError):
    """문서 입력 파일 오류(미지원 형식, 손상·암호화 파일, 잘못된 페이지 범위).

    문서 본문 내용은 메시지에 넣지 않는다.
    """


@dataclass(frozen=True)
class ParseOptions:
    """문서 분석·파싱 실행 옵션."""

    pages: tuple[int, ...] | None = None
    extract_crops: bool = True
    crop_dir: Path | None = None
    render_dpi: int = 150
    detect_borderless_tables: bool = True
    detect_running_headers_footers: bool = True
    include_header_footer_in_markdown: bool = False
    layout_detect_all_pages: bool = False
    include_speaker_notes: bool = True
    include_comments: bool = False


@dataclass(frozen=True)
class LayoutDetectionInput:
    """페이지 레이아웃 객체 탐지기(`PageLayoutDetector`)에 전달하는 입력."""

    page: int
    image_bytes: bytes
    image_extension: str
    image_width_px: int
    image_height_px: int
    page_width_pt: float
    page_height_pt: float


@dataclass(frozen=True)
class DetectedLayoutBox:
    """레이아웃 객체 탐지기가 검출한 개별 문서 영역(이미지 픽셀 좌표 기준)."""

    raw_label: str
    element_kind: ElementKind
    recommended_strategy: ParseStrategy
    bbox_px: tuple[float, float, float, float]  # (x0, y0, x1, y1) in image px
    score: float
    reading_order: int = 0
    reason: str = ""


class PageLayoutDetector(Protocol):
    """스캔/이미지 페이지에서 제목·본문·표·차트·수식 영역을 검출하는 플러그인 인터페이스."""

    @property
    def name(self) -> str: ...

    @property
    def available(self) -> bool: ...

    def detect(self, inp: LayoutDetectionInput) -> list[DetectedLayoutBox]: ...


class NoopLayoutDetector:
    """레이아웃 객체 탐지를 쓰지 않는 기본 어댑터(결정론 분석 결과 그대로 유지)."""

    @property
    def name(self) -> str:
        return "none"

    @property
    def available(self) -> bool:
        return True

    def detect(self, inp: LayoutDetectionInput) -> list[DetectedLayoutBox]:
        return []


@dataclass(frozen=True)
class VisionRequest:
    """OCR/VLM 어댑터로 넘기는 시각 영역 요청."""

    region_id: str
    page: int
    element_kind: ElementKind
    recommended_strategy: ParseStrategy
    bbox: BBox
    reason: str
    image_bytes: bytes | None = None
    image_extension: str | None = None
    crop_path: str | None = None
    embedded_text_hints: tuple[str, ...] = ()
    caption: str | None = None
    context_before: str | None = None
    context_after: str | None = None


@dataclass(frozen=True)
class VisionResult:
    """OCR/VLM 어댑터가 돌려주는 시각 영역 해석 결과."""

    adapter_name: str
    succeeded: bool
    text: str = ""
    markdown: str = ""
    metadata: dict[str, str] = field(default_factory=dict)
    error: str | None = None


class VisionParserAdapter(Protocol):
    """OCR 또는 VLM 파서 플러그인 인터페이스."""

    @property
    def name(self) -> str: ...

    def parse_region(self, request: VisionRequest) -> VisionResult: ...


class NoopVisionAdapter:
    """결정론 전용 모드에서 쓰는 기본 어댑터 — 실제 호출 없이 대기 상태로 남긴다."""

    @property
    def name(self) -> str:
        return "noop"

    def parse_region(self, request: VisionRequest) -> VisionResult:
        return VisionResult(
            adapter_name=self.name,
            succeeded=False,
            error="vision_adapter_not_configured",
        )


class PageRenderer(Protocol):
    """문서 페이지나 지정 영역을 래스터 이미지로 렌더링하는 선택적 어댑터."""

    @property
    def name(self) -> str: ...

    @property
    def available(self) -> bool: ...

    def render_region(
        self,
        source_path: Path,
        page_number: int,
        bbox: BBox | None,
        *,
        dpi: int = 150,
    ) -> EncodedImage | None: ...


class Pypdfium2PageRenderer:
    """선택 의존성 `pypdfium2`가 설치돼 있을 때 사용하는 고화질 PDF 영역 렌더러."""

    def __init__(self) -> None:
        self._pdfium: Any = None
        try:
            self._pdfium = importlib.import_module("pypdfium2")
        except ImportError:
            self._pdfium = None

    @property
    def name(self) -> str:
        return "pypdfium2"

    @property
    def available(self) -> bool:
        return self._pdfium is not None

    def render_region(
        self,
        source_path: Path,
        page_number: int,
        bbox: BBox | None,
        *,
        dpi: int = 150,
    ) -> EncodedImage | None:
        if self._pdfium is None:
            return None
        scale = dpi / 72.0
        doc = self._pdfium.PdfDocument(str(source_path))
        try:
            page = doc[page_number - 1]
            try:
                page_w, page_h = page.get_size()
                crop = (0.0, 0.0, 0.0, 0.0)
                if bbox is not None:
                    left = max(0.0, min(bbox.x0, page_w))
                    right = max(0.0, min(page_w - bbox.x1, page_w))
                    top = max(0.0, min(bbox.top, page_h))
                    bottom = max(0.0, min(page_h - bbox.bottom, page_h))
                    if left + right < page_w - 2.0 and top + bottom < page_h - 2.0:
                        crop = (left, bottom, right, top)
                bitmap = page.render(scale=scale, crop=crop)
                try:
                    width = int(bitmap.width)
                    height = int(bitmap.height)
                    raw = bytes(bitmap.buffer)
                    rgba = bgra_to_rgba(raw)
                    png = encode_png(rgba, width, height, channels=4)
                    return EncodedImage(data=png, extension=".png", width=width, height=height)
                finally:
                    bitmap.close()
            finally:
                page.close()
        except Exception:
            return None
        finally:
            doc.close()


class PdftoppmPageRenderer:
    """시스템 PATH에 `pdftoppm`(Poppler)이 있을 때 쓰는 폴백 PDF 영역 렌더러."""

    def __init__(self) -> None:
        self._binary = shutil.which("pdftoppm")

    @property
    def name(self) -> str:
        return "pdftoppm"

    @property
    def available(self) -> bool:
        return self._binary is not None

    def render_region(
        self,
        source_path: Path,
        page_number: int,
        bbox: BBox | None,
        *,
        dpi: int = 150,
    ) -> EncodedImage | None:
        if self._binary is None:
            return None
        scale = dpi / 72.0
        with tempfile.TemporaryDirectory(prefix="autoetl_crop_") as tmp_str:
            prefix = Path(tmp_str) / "crop"
            cmd = [
                self._binary,
                "-png",
                "-r",
                str(dpi),
                "-f",
                str(page_number),
                "-l",
                str(page_number),
                "-singlefile",
            ]
            if bbox is not None and bbox.width >= 4.0 and bbox.height >= 4.0:
                x_px = max(0, round(bbox.x0 * scale))
                y_px = max(0, round(bbox.top * scale))
                w_px = max(1, round(bbox.width * scale))
                h_px = max(1, round(bbox.height * scale))
                cmd.extend(["-x", str(x_px), "-y", str(y_px), "-W", str(w_px), "-H", str(h_px)])
            cmd.extend([str(source_path), str(prefix)])
            try:
                proc = subprocess.run(
                    cmd,
                    check=False,
                    capture_output=True,
                    timeout=30,
                )
            except (OSError, subprocess.SubprocessError):
                return None
            out_file = Path(f"{prefix}.png")
            if proc.returncode != 0 or not out_file.exists():
                return None
            data = out_file.read_bytes()
            width, height = _read_png_dimensions(data)
            return EncodedImage(data=data, extension=".png", width=width, height=height)


class UnavailablePageRenderer:
    """외부 렌더러가 없을 때 사용하는 널 렌더러."""

    @property
    def name(self) -> str:
        return "none"

    @property
    def available(self) -> bool:
        return False

    def render_region(
        self,
        source_path: Path,
        page_number: int,
        bbox: BBox | None,
        *,
        dpi: int = 150,
    ) -> EncodedImage | None:
        return None


def _read_png_dimensions(data: bytes) -> tuple[int | None, int | None]:
    if len(data) < 24 or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return None, None
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    return width, height


def resolve_default_page_renderer() -> PageRenderer:
    """사용 가능한 최적의 페이지 렌더러를 지연 탐색해 반환한다."""
    pdfium = Pypdfium2PageRenderer()
    if pdfium.available:
        return pdfium
    pdftoppm = PdftoppmPageRenderer()
    if pdftoppm.available:
        return pdftoppm
    return UnavailablePageRenderer()


class DocumentFormatAdapter(Protocol):
    """문서 형식별 파서 어댑터 계약(PDF, 향후 PPTX·DOCX·HWPX)."""

    @property
    def format(self) -> SourceFormat: ...

    @property
    def extensions(self) -> tuple[str, ...]: ...

    def inspect(
        self,
        source_path: Path,
        *,
        options: ParseOptions,
        layout_detector: PageLayoutDetector | None = None,
        page_renderer: PageRenderer | None = None,
    ) -> RoutingPlan: ...

    def parse(
        self,
        source_path: Path,
        *,
        options: ParseOptions,
        vision_adapter: VisionParserAdapter | None = None,
        page_renderer: PageRenderer | None = None,
        layout_detector: PageLayoutDetector | None = None,
    ) -> ParsedDocument: ...
