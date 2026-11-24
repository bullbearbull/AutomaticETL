"""Milestone E5: 다중 테이블 CLI·최상위 SDK·로컬 웹 UI 통합 테스트."""

from __future__ import annotations

import base64
import io
import json
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import pytest

import autoetl
from autoetl.cli import main
from autoetl.parser.ui import create_ui_server


def _write_sample_dataset(data_dir: Path) -> tuple[Path, Path, Path]:
    data_dir.mkdir(parents=True, exist_ok=True)
    branches = data_dir / "branches.csv"
    branches.write_text(
        "branch_code,branch_name\nKR,서울본점\nUS,뉴욕지사\nJP,도쿄지사\n",
        encoding="utf-8",
    )
    customers = data_dir / "customers.csv"
    customers.write_text(
        "customer_id,customer_name,grade\n101,김민수,VIP\n102,이서연,골드\n103,박지훈,일반\n",
        encoding="utf-8",
    )
    orders = data_dir / "orders.csv"
    orders.write_text(
        "order_id,order_code,customer_id,order_date\n"
        "1001,ORD-2025-KR-0001,101,2025-11-01\n"
        "1002,ORD-2025-US-0002,102,2025-11-02\n"
        "1003,ORD-2026-JP-0003,103,2026-01-10\n"
        "1004,ORD-2026-KR-0004,101,2026-02-15\n",
        encoding="utf-8",
    )
    return branches, customers, orders


def test_cli_analyze_directory_and_formats(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`autoetl analyze <dir|files...> [--out dir|.json] [--format ddl|erd|json]` CLI 동작 검증."""
    data_dir = tmp_path / "tables"
    branches, customers, orders = _write_sample_dataset(data_dir)
    out_dir = tmp_path / "etl_plan"

    # 1. 디렉터리 입력 + 번들 폴더 출력
    rc = main(["analyze", str(data_dir), "--purpose", "oltp", "--out", str(out_dir)])
    assert rc == 0
    stdout = capsys.readouterr().out
    assert "테이블 3개" in stdout
    assert "숨은 슬롯 FK 1건" in stdout
    for fname in ("ddl.sql", "load_plan.json", "relations.json", "erd.mmd", "analysis_report.md"):
        assert (out_dir / fname).is_file()

    # 2. 복수 파일 직접 지정 + --format ddl / erd
    assert main(["analyze", str(branches), str(orders), "--format", "ddl"]) == 0
    ddl_out = capsys.readouterr().out
    assert 'FOREIGN KEY ("branch_code") REFERENCES "branches"("branch_code")' in ddl_out

    assert main(["analyze", str(branches), str(orders), "--format", "erd"]) == 0
    erd_out = capsys.readouterr().out
    assert erd_out.startswith("erDiagram\n")

    # 3. 기존 호환 플래그(--no-llm --out bundle.json) 동작 검증
    json_out = tmp_path / "bundle.json"
    assert main(["analyze", str(customers), "--no-llm", "--out", str(json_out)]) == 0
    _ = capsys.readouterr()
    parsed_json = json.loads(json_out.read_text(encoding="utf-8"))
    assert len(parsed_json["tables"]) == 1

    # 4. 존재하지 않는 입력 경로는 종료 코드 2
    assert main(["analyze", str(tmp_path / "missing.csv")]) == 2
    _ = capsys.readouterr()


def test_top_level_sdk_multi_table_exports(tmp_path: Path) -> None:
    """최상위 `autoetl` 패키지의 다중 테이블 분석 SDK 지연 export를 검증한다."""
    data_dir = tmp_path / "sdk_tables"
    _write_sample_dataset(data_dir)

    bundle = autoetl.analyze_tables([data_dir], purpose=autoetl.Purpose.ANALYTICS)
    assert bundle.load_plan.selected_candidate_id == "C3_ANALYTICS"
    assert len(bundle.tables) == 3

    pat = autoetl.analyze_string_pattern(
        ["ORD-2025-KR-0001", "ORD-2026-US-0002", "ORD-2026-JP-0003"]
    )
    assert pat is not None
    assert pat.variable_slot_count >= 2

    out = autoetl.export_multi_table_bundle(bundle, tmp_path / "sdk_out")
    assert out["ddl.sql"].is_file()
    md = autoetl.render_analysis_report(bundle)
    assert "AutomaticETL" in md


def test_ui_server_analyze_tables_e2e_and_zip_download(tmp_path: Path) -> None:
    """로컬 웹 UI `/api/analyze-tables` 업로드 → 결과 JSON → ZIP 다운로드를 검증한다."""
    data_dir = tmp_path / "ui_tables"
    branches, customers, orders = _write_sample_dataset(data_dir)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    with create_ui_server(host="127.0.0.1", port=0) as srv:
        # 1. GET / 에 엑셀·CSV 구조 분석 탭 존재 확인
        with opener.open(f"{srv.url}/") as resp:
            assert resp.status == 200
            html = resp.read().decode("utf-8")
            assert "엑셀·CSV 구조 분석" in html
            assert "etlModePanel" in html

        # 2. POST /api/analyze-tables
        files_payload = [
            {
                "filename": p.name,
                "content_base64": base64.b64encode(p.read_bytes()).decode("ascii"),
            }
            for p in (branches, customers, orders)
        ]
        req = urllib.request.Request(
            f"{srv.url}/api/analyze-tables",
            data=json.dumps(
                {
                    "files": files_payload,
                    "options": {"purpose": "oltp", "decompose_strings": True},
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with opener.open(req) as resp:
            assert resp.status == 200
            body = json.loads(resp.read().decode("utf-8"))
            assert len(body["tables"]) == 3
            assert body["selected_candidate_id"]
            assert any(
                fk["child_slot_name"] is not None and fk["parent_table"] == "branches"
                for fk in body["relations"]["foreign_keys"]
            )
            assert "erDiagram" in body["load_plan"]["erd_mermaid"]
            assert "AutomaticETL" in body["analysis_report_markdown"]
            bundle_id = body["bundle_id"]

        # 3. GET /api/download/<bundle_id>
        with opener.open(f"{srv.url}/api/download/{bundle_id}") as resp:
            assert resp.status == 200
            zbytes = resp.read()
        with zipfile.ZipFile(io.BytesIO(zbytes), "r") as zf:
            names = set(zf.namelist())
            assert {
                "ddl.sql",
                "load_plan.json",
                "relations.json",
                "analysis_bundle.json",
                "erd.mmd",
                "analysis_report.md",
            } <= names

        # 4. 미지원 확장자 400 가드
        bad_req = urllib.request.Request(
            f"{srv.url}/api/analyze-tables",
            data=json.dumps({"files": [{"filename": "bad.pdf", "content_base64": "AAAA"}]}).encode(
                "utf-8"
            ),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            opener.open(bad_req)
        assert exc_info.value.code == 400
