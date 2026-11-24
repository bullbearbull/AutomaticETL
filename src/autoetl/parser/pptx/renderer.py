"""선택적 LibreOffice 기반 PPTX 영역 렌더러 및 구형 `.ppt` → `.pptx` 변환 유틸리티.

핵심 의존성 추가 없이 시스템 PATH에 설치된 `soffice`/`libreoffice`와
기존 PDF 렌더러(`pypdfium2` 또는 `pdftoppm`)를 조합해 벡터 도식·SmartArt·레거시
OLE 차트·EMF/WMF 이미지를 고화질 PNG 크롭으로 변환한다.
"""

from __future__ import annotations

import atexit
import contextlib
import shutil
import subprocess
import tempfile
from pathlib import Path

from autoetl.parser.adapters import PageRenderer, ParserInputError, resolve_default_page_renderer
from autoetl.parser.contracts import BBox
from autoetl.parser.image_io import EncodedImage


def find_libreoffice_binary(binary: str | None = None) -> str | None:
    """사용 가능한 LibreOffice 실행 파일(`soffice` / `libreoffice`) 경로를 탐색한다."""
    if binary:
        candidate = Path(binary)
        if candidate.is_file():
            return str(candidate)
        return shutil.which(binary)
    return shutil.which("soffice") or shutil.which("libreoffice")


def convert_ppt_to_pptx(
    ppt_path: Path | str,
    out_dir: Path | str,
    *,
    binary: str | None = None,
    timeout_seconds: float = 60.0,
) -> Path:
    """구형 바이너리 `.ppt`(OLE2) 파일을 LibreOffice 헤드리스 모드로 `.pptx`로 변환한다.

    외부 프로세스 실행이 필요한 선택 기능으로, 호출자가 명시적으로 요청할 때만 동작한다.
    """
    src = Path(ppt_path)
    if not src.is_file():
        raise ParserInputError(f"변환할 PPT 파일이 존재하지 않는다: {src.name}")

    soffice = find_libreoffice_binary(binary)
    if soffice is None:
        raise ParserInputError(
            "구형 .ppt 변환에 필요한 LibreOffice(`soffice`/`libreoffice`)가 PATH에 없다."
        )

    target_dir = Path(out_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="autoetl_lo_profile_") as profile_dir:
        profile_uri = Path(profile_dir).resolve().as_uri()
        cmd = [
            soffice,
            "--headless",
            "--nologo",
            "--nodefault",
            "--norestore",
            f"-env:UserInstallation={profile_uri}",
            "--convert-to",
            "pptx",
            "--outdir",
            str(target_dir),
            str(src),
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ParserInputError(f"LibreOffice PPT→PPTX 변환이 실패했다: {src.name}") from exc

    if proc.returncode != 0:
        raise ParserInputError(
            f"LibreOffice PPT→PPTX 변환이 비정상 종료했다(exit={proc.returncode}): {src.name}"
        )

    expected = target_dir / f"{src.stem}.pptx"
    if not expected.is_file():
        raise ParserInputError(f"LibreOffice 변환 결과 .pptx 파일을 찾을 수 없다: {expected.name}")
    return expected


class LibreOfficePptxRenderer:
    """LibreOffice(`soffice --convert-to pdf`) + PDF 렌더러 조합 PPTX `PageRenderer`.

    동일 `.pptx` 파일은 세션 내 임시 폴더에 1회만 PDF로 변환해 캐시한 뒤,
    요청된 슬라이드/영역(`bbox`)을 기존 PDF 렌더러(`pypdfium2` 또는 `pdftoppm`)로
    고화질 PNG 크롭한다.
    """

    def __init__(
        self,
        *,
        binary: str | None = None,
        pdf_renderer: PageRenderer | None = None,
        timeout_seconds: float = 60.0,
    ) -> None:
        self._binary = find_libreoffice_binary(binary)
        self._pdf_renderer = (
            pdf_renderer if pdf_renderer is not None else resolve_default_page_renderer()
        )
        self._timeout_seconds = timeout_seconds
        self._tmp_dir: tempfile.TemporaryDirectory[str] | None = None
        self._pdf_cache: dict[tuple[str, int], Path | None] = {}
        self._closed = False
        atexit.register(self.close)

    @property
    def name(self) -> str:
        pdf_name = self._pdf_renderer.name if self._pdf_renderer is not None else "none"
        return f"libreoffice+{pdf_name}"

    @property
    def available(self) -> bool:
        return (
            not self._closed
            and self._binary is not None
            and self._pdf_renderer is not None
            and self._pdf_renderer.available
        )

    def close(self) -> None:
        """변환된 임시 PDF 캐시 폴더를 정리한다."""
        self._closed = True
        self._pdf_cache.clear()
        if self._tmp_dir is not None:
            with contextlib.suppress(OSError):
                self._tmp_dir.cleanup()
            self._tmp_dir = None

    def __enter__(self) -> LibreOfficePptxRenderer:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _ensure_converted_pdf(self, source_path: Path) -> Path | None:
        if not self.available or self._binary is None:
            return None
        try:
            resolved = source_path.resolve()
            mtime_ns = resolved.stat().st_mtime_ns
        except OSError:
            return None

        cache_key = (str(resolved), mtime_ns)
        if cache_key in self._pdf_cache:
            return self._pdf_cache[cache_key]

        if self._tmp_dir is None:
            self._tmp_dir = tempfile.TemporaryDirectory(prefix="autoetl_pptx_render_")
        base_dir = Path(self._tmp_dir.name)
        idx = len(self._pdf_cache) + 1
        out_dir = base_dir / f"job_{idx}"
        profile_dir = base_dir / f"profile_{idx}"
        out_dir.mkdir(parents=True, exist_ok=True)
        profile_dir.mkdir(parents=True, exist_ok=True)

        cmd = [
            self._binary,
            "--headless",
            "--nologo",
            "--nodefault",
            "--norestore",
            f"-env:UserInstallation={profile_dir.resolve().as_uri()}",
            "--convert-to",
            "pdf",
            "--outdir",
            str(out_dir),
            str(resolved),
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                timeout=self._timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            self._pdf_cache[cache_key] = None
            return None

        expected_pdf = out_dir / f"{resolved.stem}.pdf"
        if proc.returncode != 0 or not expected_pdf.is_file():
            self._pdf_cache[cache_key] = None
            return None

        self._pdf_cache[cache_key] = expected_pdf
        return expected_pdf

    def render_region(
        self,
        source_path: Path,
        page_number: int,
        bbox: BBox | None,
        *,
        dpi: int = 150,
    ) -> EncodedImage | None:
        """PPTX 슬라이드(또는 지정 영역)를 임시 PDF 경유 PNG로 렌더링한다."""
        if not self.available or self._pdf_renderer is None:
            return None
        pdf_path = self._ensure_converted_pdf(source_path)
        if pdf_path is None:
            return None
        return self._pdf_renderer.render_region(pdf_path, page_number, bbox, dpi=dpi)
