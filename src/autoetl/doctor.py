"""Auto ETL·Auto Parser 실행 환경 진단(`autoetl doctor`) 모듈."""

from __future__ import annotations

import importlib.util
import platform
import shutil

from pydantic import Field

from autoetl import __version__
from autoetl.contracts.common import ContractModel
from autoetl.parser.legacy.converter import find_libreoffice_binary
from autoetl.parser.pipeline import supported_extensions


class DoctorCheck(ContractModel):
    """개별 기능·백엔드 준비 상태 점검 항목."""

    name: str
    category: str
    available: bool
    detail: str
    install_hint: str = ""


class DoctorReport(ContractModel):
    """`autoetl doctor` 전체 진단 결과 계약."""

    version: str
    python_version: str
    platform: str
    supported_extensions: list[str] = Field(default_factory=list)
    checks: list[DoctorCheck] = Field(default_factory=list)


def _has_module(module_name: str) -> bool:
    return importlib.util.find_spec(module_name) is not None


def collect_doctor_report() -> DoctorReport:
    """현재 Python 환경과 시스템 PATH를 점검해 지원 포맷 및 선택 백엔드 가용성을 반환한다."""
    exts = list(supported_extensions())
    lo_bin = find_libreoffice_binary()
    pdftoppm_bin = shutil.which("pdftoppm")
    tesseract_bin = shutil.which("tesseract")
    has_pypdf = _has_module("pypdf")
    has_pypdfium2 = _has_module("pypdfium2")
    has_onnx = _has_module("onnxruntime") and _has_module("numpy")

    checks = [
        DoctorCheck(
            name="PDF 파서 (pypdf)",
            category="core_parser",
            available=has_pypdf,
            detail="설치됨 (.pdf 디지털 텍스트·표·시각 영역 추출 가능)" if has_pypdf else "미설치",
            install_hint="" if has_pypdf else "uv sync 또는 pip install pypdf",
        ),
        DoctorCheck(
            name="OOXML 네이티브 파서 (PPTX·DOCX·HWPX·XLSX)",
            category="core_parser",
            available=True,
            detail="내장 표준 라이브러리 전용 (외부 의존성 불필요)",
        ),
        DoctorCheck(
            name="구형 OLE2 네이티브 파서 (HWP 5.0·DOC·PPT)",
            category="core_parser",
            available=True,
            detail="내장 순수 Python MS-CFB 리더 활성 (외부 의존성 불필요)",
        ),
        DoctorCheck(
            name="LibreOffice 헤드리스 (DOC/PPT Tier-1 승격·PPTX 도식 렌더)",
            category="optional_converter",
            available=lo_bin is not None,
            detail=f"감지됨: {lo_bin}"
            if lo_bin
            else "미설치 (DOC/PPT는 내장 Tier-2 순수 Python 파서로 자동 폴백)",
            install_hint=""
            if lo_bin
            else "선택 설치: libreoffice / soffice를 PATH에 추가하면 DOC/PPT 고충실도 변환 활성",
        ),
        DoctorCheck(
            name="PDF 페이지 렌더러 (pypdfium2 / pdftoppm)",
            category="optional_renderer",
            available=has_pypdfium2 or (pdftoppm_bin is not None),
            detail=(
                "pypdfium2 활성"
                if has_pypdfium2
                else (
                    f"pdftoppm 활성 ({pdftoppm_bin})"
                    if pdftoppm_bin
                    else "미설치 (내장 이미지 추출 폴백 사용)"
                )
            ),
            install_hint=""
            if (has_pypdfium2 or pdftoppm_bin)
            else "선택 설치: pip install pypdfium2 또는 poppler-utils(pdftoppm)",
        ),
        DoctorCheck(
            name="스캔 레이아웃 탐지 (Heuristic 내장 / ONNX PP-DocLayout)",
            category="optional_layout",
            available=True,
            detail=(
                "Heuristic 내장 + ONNX(onnxruntime·numpy) 활성"
                if has_onnx
                else "Heuristic 내장 활성 (ONNX PP-DocLayout은 onnxruntime+numpy 설치 시 활성)"
            ),
            install_hint="" if has_onnx else "선택 설치: pip install onnxruntime numpy",
        ),
        DoctorCheck(
            name="로컬 OCR (Tesseract)",
            category="optional_vision",
            available=tesseract_bin is not None,
            detail=(
                f"감지됨: {tesseract_bin}"
                if tesseract_bin
                else "미설치 (로컬 VLM: --vision ollama 또는 --vision openai-compat 사용 가능)"
            ),
            install_hint="" if tesseract_bin else "선택 설치: tesseract-ocr (+ kor/eng 언어팩)",
        ),
    ]

    return DoctorReport(
        version=__version__,
        python_version=platform.python_version(),
        platform=platform.platform(),
        supported_extensions=exts,
        checks=checks,
    )


def render_doctor_markdown(report: DoctorReport) -> str:
    """`DoctorReport`를 터미널/Markdown 출력용 요약 텍스트로 렌더링한다."""
    lines = [
        f"Auto ETL / Auto Parser v{report.version} 환경 진단",
        f"- Python: {report.python_version} ({report.platform})",
        f"- 지원 문서 확장자 ({len(report.supported_extensions)}종): "
        f"{', '.join(report.supported_extensions)}",
        "",
        "| 구성 요소 | 상태 | 상세 |",
        "| --- | :-: | --- |",
    ]
    for chk in report.checks:
        badge = "OK" if chk.available else "OPTIONAL"
        detail = chk.detail
        if not chk.available and chk.install_hint:
            detail = f"{detail} — {chk.install_hint}"
        lines.append(f"| {chk.name} | {badge} | {detail} |")
    return "\n".join(lines) + "\n"


__all__ = [
    "DoctorCheck",
    "DoctorReport",
    "collect_doctor_report",
    "render_doctor_markdown",
]
