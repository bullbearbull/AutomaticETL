"""Milestone E1: 다중 테이블 입력 컨텍스트(`MultiTableContext`) 단위·통합 테스트."""

from __future__ import annotations

import zipfile
from pathlib import Path

import duckdb
import pytest

from autoetl.contracts import IngestFormat, PhysicalType, TableSourceKind
from autoetl.core.errors import InputError
from autoetl.core.multi_context import (
    MultiTableContext,
    NamedTableInput,
    discover_table_sources,
    profile_all_tables,
    sanitize_table_names,
)


def _write_multi_sheet_xlsx(path: Path) -> Path:
    """2개 시트(Products + 빈 시트 + Branches)가 들어 있는 최소 유효 `.xlsx`를 만든다."""
    ns_m = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    ns_r = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    ns_rel = "http://schemas.openxmlformats.org/package/2006/relationships"
    rel_doc = f"{ns_r}/officeDocument"
    rel_ws = f"{ns_r}/worksheet"
    ct_rels = "application/vnd.openxmlformats-package.relationships+xml"

    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            f'<Default Extension="rels" ContentType="{ct_rels}"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            "</Types>",
        )
        zf.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<Relationships xmlns="{ns_rel}">'
            f'<Relationship Id="rId1" Type="{rel_doc}" Target="xl/workbook.xml"/>'
            "</Relationships>",
        )
        zf.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<workbook xmlns="{ns_m}" xmlns:r="{ns_r}"><sheets>'
            '<sheet name="Products" sheetId="1" r:id="rId1"/>'
            '<sheet name="EmptySheet" sheetId="2" r:id="rId2"/>'
            '<sheet name="지점목록" sheetId="3" r:id="rId3"/>'
            "</sheets></workbook>",
        )
        zf.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<Relationships xmlns="{ns_rel}">'
            f'<Relationship Id="rId1" Type="{rel_ws}" Target="worksheets/sheet1.xml"/>'
            f'<Relationship Id="rId2" Type="{rel_ws}" Target="worksheets/sheet2.xml"/>'
            f'<Relationship Id="rId3" Type="{rel_ws}" Target="worksheets/sheet3.xml"/>'
            "</Relationships>",
        )
        # Sheet 1: Products
        zf.writestr(
            "xl/worksheets/sheet1.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<worksheet xmlns="{ns_m}"><sheetData>'
            '<row r="1">'
            '<c r="A1" t="inlineStr"><is><t>product_id</t></is></c>'
            '<c r="B1" t="inlineStr"><is><t>product_name</t></is></c>'
            '<c r="C1" t="inlineStr"><is><t>unit_price</t></is></c>'
            "</row>"
            '<row r="2">'
            '<c r="A2"><v>101</v></c>'
            '<c r="B2" t="inlineStr"><is><t>노트북</t></is></c>'
            '<c r="C2"><v>1500000</v></c>'
            "</row>"
            '<row r="3">'
            '<c r="A3"><v>102</v></c>'
            '<c r="B3" t="inlineStr"><is><t>키보드</t></is></c>'
            '<c r="C3"><v>120000</v></c>'
            "</row>"
            "</sheetData></worksheet>",
        )
        # Sheet 2: EmptySheet
        zf.writestr(
            "xl/worksheets/sheet2.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<worksheet xmlns="{ns_m}"><sheetData/></worksheet>',
        )
        # Sheet 3: 지점목록 (순수 한글 시트명)
        zf.writestr(
            "xl/worksheets/sheet3.xml",
            f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<worksheet xmlns="{ns_m}"><sheetData>'
            '<row r="1">'
            '<c r="A1" t="inlineStr"><is><t>branch_code</t></is></c>'
            '<c r="B1" t="inlineStr"><is><t>region</t></is></c>'
            "</row>"
            '<row r="2">'
            '<c r="A2" t="inlineStr"><is><t>KR</t></is></c>'
            '<c r="B2" t="inlineStr"><is><t>서울</t></is></c>'
            "</row>"
            '<row r="3">'
            '<c r="A3" t="inlineStr"><is><t>US</t></is></c>'
            '<c r="B3" t="inlineStr"><is><t>뉴욕</t></is></c>'
            "</row>"
            "</sheetData></worksheet>",
        )
    return path


def test_sanitize_table_names_unique_and_safe() -> None:
    """중복·특수문자·순수 한글 테이블명이 안전한 유일 식별자로 정규화된다."""
    names = sanitize_table_names(
        ["Orders-2026", "orders 2026", "지점목록", "123_sales", "지점목록"]
    )
    assert names == ["orders_2026", "orders_2026_2", "table_3", "table_4", "table_5"]


def test_multi_table_context_loads_csv_tsv_cp949_xlsx_and_joins(tmp_path: Path) -> None:
    """다중 CSV(UTF-8/CP949) + TSV + 멀티 시트 XLSX를 한 DuckDB 세션에서 프로파일·교차 조인한다."""
    data_dir = tmp_path / "dataset"
    data_dir.mkdir()

    # 1. UTF-8 CSV (orders)
    (data_dir / "orders.csv").write_text(
        "order_id,customer_id,product_id,qty\n1,10,101,2\n2,20,102,1\n3,10,102,3\n",
        encoding="utf-8",
    )
    # 2. CP949 CSV (customers)
    (data_dir / "customers.csv").write_bytes(
        "customer_id,고객명,등급\n10,김철수,VIP\n20,이영희,일반\n".encode("cp949")
    )
    # 3. TSV (events)
    (data_dir / "events.tsv").write_text(
        "event_id\torder_id\tevent_date\n1001\t1\t2026-04-01\n1002\t2\t2026-04-02\n",
        encoding="utf-8",
    )
    # 4. 멀티 시트 XLSX (Products + EmptySheet + 지점목록)
    _write_multi_sheet_xlsx(data_dir / "master.xlsx")
    # 5. Office 임시 잠금 파일은 자동 제외되어야 한다
    (data_dir / "~$master.xlsx").write_bytes(b"lock")
    (data_dir / ".hidden.csv").write_text("a,b\n1,2\n", encoding="utf-8")

    discovered = discover_table_sources(data_dir)
    assert [p.name for p in discovered] == [
        "customers.csv",
        "events.tsv",
        "master.xlsx",
        "orders.csv",
    ]

    with MultiTableContext(data_dir) as mctx:
        # 5개 유효 논리 테이블: customers, events, master_products, master (지점목록), orders
        assert len(mctx.slots) == 5
        # EmptySheet는 경고 후 건너뜀
        assert any("EmptySheet" in w for w in mctx.warnings)

        bundles = profile_all_tables(mctx)
        assert len(bundles) == 5

        by_display = {b.source.display_name: b for b in bundles}
        assert by_display["customers.csv"].ingest.files[0].encoding.lower() in {"cp949", "euc-kr"}
        assert by_display["events.tsv"].ingest.files[0].format is IngestFormat.TSV
        assert by_display["master.xlsx::Products"].source.source_kind is TableSourceKind.XLSX_SHEET
        assert by_display["master.xlsx::Products"].ingest.files[0].format is IngestFormat.XLSX
        assert by_display["master.xlsx::Products"].ingest.files[0].sheet == "Products"

        orders_bundle = by_display["orders.csv"]
        orders_types = {t.column: t.physical_type for t in orders_bundle.types}
        assert orders_types["product_id"] is PhysicalType.INTEGER

        # 단일 DuckDB 연결 안에서 CSV(orders) ↔ XLSX(Products) 정규화 뷰 교차 조인 검증
        orders_id = orders_bundle.source.table_id
        products_id = by_display["master.xlsx::Products"].source.table_id
        assert mctx.connection is not None
        joined = mctx.connection.execute(
            f"SELECT o.c0, p.c1, o.c3 FROM {orders_id}.normalized AS o "
            f"JOIN {products_id}.normalized AS p ON o.c2 = p.c0 "
            "ORDER BY o.c0"
        ).fetchall()
        assert joined == [(1, "노트북", 2), (2, "키보드", 1), (3, "키보드", 3)]

        # 심층 방어: 적재 완료 후 외부 파일 접근이 잠겨 있어야 한다
        with pytest.raises(duckdb.PermissionException):
            mctx.connection.execute("SELECT * FROM read_csv(?)", [str(data_dir / "orders.csv")])


def test_multi_table_context_in_memory_and_sql_injection_safety(tmp_path: Path) -> None:
    """메모리 테이블 입력과 위험한 파일명/헤더 문자열 SQL 인젝션 방어를 검증한다."""
    weird_csv = tmp_path / "a'; DROP TABLE raw; --.csv"
    weird_csv.write_text('"col"; DROP TABLE x,val\n1,hello\n2,world\n', encoding="utf-8")

    mem = NamedTableInput(
        name="extracted_summary",
        headers=("code", "amount"),
        rows=(("A01", "100"), ("A02", "250")),
    )
    with MultiTableContext([weird_csv], extra_tables=[mem]) as mctx:
        bundles = profile_all_tables(mctx)
        assert len(bundles) == 2
        assert bundles[0].source.row_count == 2
        assert bundles[1].source.table_name == "extracted_summary"
        assert bundles[1].source.source_kind is TableSourceKind.EXTRACTED_TABLE


def test_multi_table_context_error_guards(tmp_path: Path) -> None:
    """존재하지 않는 경로·미지원 확장자·빈 폴더는 `InputError`로 차단한다."""
    with pytest.raises(InputError):
        discover_table_sources(tmp_path / "missing.csv")

    bad_ext = tmp_path / "notes.txt"
    bad_ext.write_text("hello", encoding="utf-8")
    with pytest.raises(InputError, match="지원하지 않는"):
        discover_table_sources(bad_ext)

    empty_dir = tmp_path / "empty_dir"
    empty_dir.mkdir()
    with pytest.raises(InputError):
        discover_table_sources(empty_dir)
