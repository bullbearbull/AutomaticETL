"""선택적 LibreOffice 헤드리스 기반 구형 바이너리 오피스 변환 유틸리티.

핵심 의존성 추가 없이 시스템 PATH에 `soffice`/`libreoffice`가 설치되어 있을 때
구형 바이너리 포맷(`.doc` → `.docx`, `.ppt` → `.pptx`, `.hwp` → `.pdf`)을
임시 디렉터리로 변환해 고충실도 XML 어댑터로 승격(Tier-1)할 수 있게 지원한다.
LibreOffice가 없으면 각 어댑터의 무의존성 순수 Python MS-CFB 네이티브 파서(Tier-2)가 동작한다.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from autoetl.parser.adapters import ParserInputError
from autoetl.parser.pptx.renderer import find_libreoffice_binary


def convert_legacy_office_document(
    source_path: Path | str,
    target_format: str,
    out_dir: Path | str,
    *,
    binary: str | None = None,
    timeout_seconds: float = 60.0,
) -> Path:
    """구형 바이너리 문서를 LibreOffice 헤드리스 모드로 `docx`/`pptx`/`pdf`로 변환한다."""
    src = Path(source_path)
    if not src.is_file():
        raise ParserInputError(f"변환할 문서 파일이 존재하지 않는다: {src.name}")

    fmt = target_format.strip().lower().lstrip(".")
    if not fmt:
        raise ParserInputError("변환 대상 형식이 비어 있다.")

    soffice = find_libreoffice_binary(binary)
    if soffice is None:
        raise ParserInputError(
            f"구형 문서({src.suffix}) 변환에 필요한 LibreOffice(`soffice`/`libreoffice`)가 "
            "PATH에 없다."
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
            fmt,
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
            raise ParserInputError(
                f"LibreOffice {src.suffix}→.{fmt} 변환이 실패했다: {src.name}"
            ) from exc

    if proc.returncode != 0:
        raise ParserInputError(
            f"LibreOffice {src.suffix}→.{fmt} 변환이 비정상 종료했다"
            f"(exit={proc.returncode}): {src.name}"
        )

    expected = target_dir / f"{src.stem}.{fmt}"
    if not expected.is_file():
        raise ParserInputError(f"LibreOffice 변환 결과 .{fmt} 파일을 찾을 수 없다: {expected.name}")
    return expected


__all__ = [
    "convert_legacy_office_document",
    "find_libreoffice_binary",
]
