"""폴더 일괄 파싱(`autoetl.parser.batch`) 오류 격리·재개·병렬·CLI 단위/통합 테스트."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from autoetl.cli import main
from autoetl.parser import (
    BatchManifest,
    discover_batch_files,
    parse_batch,
)


def _write_mini_docx(path: Path, body_text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "word/document.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body><w:p><w:r><w:t>{body_text}</w:t></w:r></w:p></w:body>"
            "</w:document>",
        )
    return path


def _write_mini_xlsx(path: Path, title: str, value: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    ns_m = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    ns_r = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    ns_rel = "http://schemas.openxmlformats.org/package/2006/relationships"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "xl/workbook.xml",
            f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<workbook xmlns="{ns_m}" xmlns:r="{ns_r}">'
            f'<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>',
        )
        zf.writestr(
            "xl/_rels/workbook.xml.rels",
            f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<Relationships xmlns="{ns_rel}">'
            f'<Relationship Id="rId1" Type="{ns_r}/worksheet" Target="worksheets/sheet1.xml"/>'
            "</Relationships>",
        )
        zf.writestr(
            "xl/worksheets/sheet1.xml",
            f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<worksheet xmlns="{ns_m}"><sheetData>'
            f'<row r="1"><c r="A1" t="inlineStr"><is><t>항목</t></is></c>'
            f'<c r="B1" t="inlineStr"><is><t>값</t></is></c></row>'
            f'<row r="2"><c r="A2" t="inlineStr"><is><t>{title}</t></is></c>'
            f'<c r="B2" t="inlineStr"><is><t>{value}</t></is></c></row>'
            "</sheetData></worksheet>",
        )
    return path


def test_batch_discovery_error_isolation_resume_and_merged_chunks(tmp_path: Path) -> None:
    """혼합 폴더 스캔(임시파일 제외)·오류 격리·SHA-256 재개·병합 청크 생성을 검증한다."""
    in_dir = tmp_path / "corpus"
    out_dir = tmp_path / "batch_out"

    f_docx = _write_mini_docx(in_dir / "report.docx", "첫 번째 문서 본문입니다.")
    _write_mini_xlsx(in_dir / "sub" / "metrics.xlsx", "매출", "990억")
    # 동일 stem 다른 확장자 충돌 방지 확인
    _write_mini_xlsx(in_dir / "report.xlsx", "이익", "120억")
    # Office 임시 잠금 파일 & 미지원 파일 -> 자동 무시
    _write_mini_docx(in_dir / "~$report.docx", "임시파일")
    (in_dir / "notes.txt").write_text("plain text", encoding="utf-8")
    # 암호화(손상) XLSX -> 오류 격리 대상
    bad_xlsx = in_dir / "sub" / "locked.xlsx"
    bad_xlsx.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 512)

    discovered = discover_batch_files(in_dir, recursive=True)
    rel_names = [p.relative_to(in_dir).as_posix() for p in discovered]
    assert rel_names == ["report.docx", "report.xlsx", "sub/locked.xlsx", "sub/metrics.xlsx"]

    # 1차 실행 (병렬 workers=2)
    m1 = parse_batch(in_dir, out_dir, workers=2, resume=True)
    assert isinstance(m1, BatchManifest)
    assert m1.total_files == 4
    assert m1.succeeded == 3
    assert m1.failed == 1
    assert m1.skipped_resume == 0
    assert (out_dir / "batch_manifest.json").is_file()
    assert (out_dir / "batch_summary.md").is_file()
    assert (out_dir / "all_chunks.jsonl").is_file()

    # 충돌 방지: report__docx와 report__xlsx가 각각 독립 폴더로 생성
    assert (out_dir / "documents" / "report__docx" / "parsed.json").is_file()
    assert (out_dir / "documents" / "report__xlsx" / "parsed.json").is_file()

    # 통합 all_chunks.jsonl 검증
    all_lines = [
        json.loads(ln)
        for ln in (out_dir / "all_chunks.jsonl").read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    sources = {row["source_path"] for row in all_lines}
    assert {"report.docx", "report.xlsx", "metrics.xlsx"} <= sources

    # 2차 실행 (--resume): 변경 없는 3건은 skipped_resume, 1건 수정 시 해당 파일만 재파싱
    _write_mini_docx(f_docx, "수정된 첫 번째 문서 본문입니다.")
    m2 = parse_batch(in_dir, out_dir, workers=1, resume=True)
    assert m2.skipped_resume == 2
    assert m2.succeeded == 1
    assert m2.failed == 1
    by_rel = {it.relative_path: it for it in m2.items}
    assert by_rel["report.docx"].status == "ok"
    assert by_rel["report.xlsx"].status == "skipped_resume"
    assert by_rel["sub/metrics.xlsx"].status == "skipped_resume"
    assert by_rel["sub/locked.xlsx"].status == "failed"
    assert (
        by_rel["sub/locked.xlsx"].error is not None and "암호화" in by_rel["sub/locked.xlsx"].error
    )


def test_cli_parse_folder_batch_and_plan_only(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`autoetl parse <폴더>` CLI 배치 실행과 `--plan-only`·`--include-ext`를 검증한다."""
    in_dir = tmp_path / "docs"
    _write_mini_docx(in_dir / "a.docx", "배치 CLI 테스트 문서")
    _write_mini_xlsx(in_dir / "b.xlsx", "항목A", "100")

    # --out 미지정 시 에러 코드 2
    assert main(["parse", str(in_dir)]) == 2
    capsys.readouterr()

    # --plan-only 배치 진단
    diag_dir = tmp_path / "diag_out"
    assert main(["parse", str(in_dir), "--plan-only", "--out", str(diag_dir)]) == 0
    diag_out = capsys.readouterr().out
    assert "일괄" in diag_out
    assert (diag_dir / "batch_manifest.json").is_file()
    assert (diag_dir / "documents" / "a__docx" / "routing_report.md").is_file()

    # --include-ext .docx 필터링 + --resume
    batch_dir = tmp_path / "full_out"
    rc = main(
        [
            "parse",
            str(in_dir),
            "--include-ext",
            ".docx",
            "--workers",
            "2",
            "--resume",
            "--out",
            str(batch_dir),
        ]
    )
    assert rc == 0
    capsys.readouterr()
    manifest_data = json.loads((batch_dir / "batch_manifest.json").read_text(encoding="utf-8"))
    assert manifest_data["total_files"] == 1
    assert manifest_data["succeeded"] == 1
    assert (batch_dir / "all_chunks.jsonl").is_file()
