"""베타 최상위 SDK(`import autoetl`) 및 CLI `--version` 스모크 테스트."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

import autoetl
from autoetl.cli import main


def _write_mini_docx(path: Path, text: str) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "word/document.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body>"
            "</w:document>",
        )


def test_cli_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    """`autoetl --version` 및 `-V`가 현재 패키지 버전을 출력하고 0으로 종료한다."""
    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])
    assert exc_info.value.code == 0
    assert f"autoetl {autoetl.__version__}" in capsys.readouterr().out

    with pytest.raises(SystemExit) as exc_short:
        main(["-V"])
    assert exc_short.value.code == 0
    assert f"autoetl {autoetl.__version__}" in capsys.readouterr().out


def test_top_level_sdk_entrypoints(tmp_path: Path) -> None:
    """`autoetl.parse/inspect/chunk_document/export_bundle/parse_batch/doctor` 최상위 SDK 검증."""
    exts = set(autoetl.supported_extensions())
    assert {".pdf", ".pptx", ".docx", ".hwpx", ".xlsx", ".hwp", ".doc", ".ppt"} <= exts

    in_dir = tmp_path / "docs"
    in_dir.mkdir()
    sample = in_dir / "sample.docx"
    _write_mini_docx(sample, "AutoETL Beta 최상위 SDK 검증 문서입니다.")

    plan = autoetl.inspect(sample)
    assert plan.summary.total_pages >= 1

    doc = autoetl.parse(sample)
    assert doc.document.format.value == "docx"

    chunks = autoetl.chunk_document(doc, options=autoetl.ChunkOptions(max_chars=500))
    assert len(chunks) >= 1
    assert "최상위 SDK 검증" in chunks[0].text

    out_single = tmp_path / "single_out"
    manifest = autoetl.export_bundle(doc, out_single)
    assert manifest.document_markdown.is_file()
    assert manifest.chunks_jsonl is not None and manifest.chunks_jsonl.is_file()

    out_batch = tmp_path / "batch_out"
    batch_manifest = autoetl.parse_batch(in_dir, out_batch)
    assert batch_manifest.succeeded == 1
    assert batch_manifest.failed == 0

    report = autoetl.collect_doctor_report()
    assert report.version == autoetl.__version__

    assert "parse" in dir(autoetl)
    missing_name = "nonexistent_symbol_xyz"
    with pytest.raises(AttributeError):
        _ = getattr(autoetl, missing_name)
