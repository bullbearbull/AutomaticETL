"""Milestone E1: 다중 테이블 입력 컨텍스트(`MultiTableContext`) 단위·통합 테스트."""

from __future__ import annotations

import zipfile
from pathlib import Path

import duckdb
import pytest

from autoetl.adapters.detectors import ColumnSample, NormalizationRule
from autoetl.adapters.detectors.builtin import BUILTIN_DETECTORS
from autoetl.contracts import (
    IngestFormat,
    PhysicalType,
    SensitiveType,
    SensitivityLevel,
    TableSourceKind,
)
from autoetl.core.errors import InputError
from autoetl.core.multi_context import (
    MultiTableContext,
    NamedTableInput,
    discover_table_sources,
    profile_all_tables,
    sanitize_table_names,
)
from autoetl.core.pipeline import run_pipeline
from autoetl.core.stages.s03_types import _normalize_expression
from autoetl.core.stages.s07_sensitivity import _classify_column


def test_period_codes_and_amounts_are_not_classified_as_dates() -> None:
    source = NamedTableInput(
        name="payments",
        headers=("period", "price"),
        rows=(("202411", "202411"), ("202412", "202412"), ("202411", "35000"), ("202412", "41000")),
    )
    detector = next(detector for detector in BUILTIN_DETECTORS if detector.name == "builtin.date")
    with MultiTableContext(extra_tables=[source]) as ctx:
        bundle = profile_all_tables(ctx)[0]
        for index, profile in enumerate(bundle.profiles):
            sample = ColumnSample(
                column=profile.column,
                values=tuple(row[index] for row in source.rows),
                profile=profile,
            )
            assert detector.detect(sample) is None
        assert all(assignment.physical_type is not PhysicalType.DATE for assignment in bundle.types)


def test_date_normalization_rejects_seven_digit_dates() -> None:
    expr = _normalize_expression("value", NormalizationRule.DATE, 0)
    with duckdb.connect() as conn:
        assert conn.execute(f"SELECT {expr} FROM (VALUES ('2024111')) AS t(value)").fetchall() == [
            (None,),
        ]


@pytest.mark.parametrize("column", ["BusinessNumber", "businessno", "ClassName", "PassNo"])
def test_rrn_name_hint_does_not_match_inside_words(column: str) -> None:
    assignment, _ = _classify_column(column, None, [])
    assert assignment is None or SensitiveType.RESIDENT_REGISTRATION_NUMBER not in assignment.types


@pytest.mark.parametrize(
    "column", ["customer_ssn", "CustomerSSN", "rrn", "residentNo", "주민등록번호"]
)
def test_rrn_name_hint_recognizes_real_identifier_names(column: str) -> None:
    assignment, _ = _classify_column(column, None, [])
    assert assignment is not None
    assert assignment.types == [SensitiveType.RESIDENT_REGISTRATION_NUMBER]


@pytest.mark.parametrize(
    ("column", "samples", "expected"),
    [
        ("ip_address", ["10.0.0.1"], [SensitiveType.IP_ADDRESS]),
        ("mac_address", [], []),
        ("e_mail_address", [], [SensitiveType.EMAIL]),
        ("email_address", [], [SensitiveType.EMAIL]),
        ("이메일주소", [], [SensitiveType.EMAIL]),
        ("IP주소", [], []),
        ("IPv4Address", [], []),
        ("IPv6Address", [], []),
        ("clientIPv4Address", [], []),
        ("clientIPv6Address", [], []),
        ("URLAddress", [], []),
        ("web_address", [], []),
        ("wallet_address", [], []),
        ("웹주소", [], []),
        ("지갑주소", [], []),
        ("shipping_address", [], [SensitiveType.ADDRESS]),
    ],
)
def test_address_hint_distinguishes_network_and_postal_addresses(
    column: str, samples: list[str], expected: list[SensitiveType]
) -> None:
    assignment, _ = _classify_column(column, None, samples)
    assert (assignment.types if assignment else []) == expected
    if expected == [SensitiveType.IP_ADDRESS]:
        assert assignment is not None and assignment.level is SensitivityLevel.L1


def test_single_table_pipeline_samples_duplicate_headers_by_position(tmp_path: Path) -> None:
    source = tmp_path / "contacts.csv"
    source.write_text(
        "data,data\n900101-1234568,oak\n910101-1234563,elm\n920101-1234569,pine\n",
        encoding="utf-8",
    )
    assignments = run_pipeline(source).sensitivity.assignments
    assert [(a.column, a.types) for a in assignments] == [
        ("data", [SensitiveType.RESIDENT_REGISTRATION_NUMBER]),
    ]


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


def test_resilient_csv_fallback_and_international_postcode_header(tmp_path: Path) -> None:
    """비인용 쉼표가 섞인 비표준 CSV 복구와 영문 우편번호 첫 행의 헤더 오인 차단을 검증한다."""
    # 1. 비인용 쉼표로 DuckDB 기본 dialect 스니퍼가 실패하는 실무 CSV (Northwind 패턴)
    messy_csv = tmp_path / "customers.csv"
    messy_csv.write_text(
        "customerID,companyName,address,city\n"
        "ALFKI,Alfreds Futterkiste,Obere Str. 57,Berlin\n"
        "ROMEY,Romero y tomillo,Gran Via, 1,Madrid\n"
        "ANATR,Ana Trujillo,Avda. 2222,Mexico\n"
        "BLAUS,Blauer See,Forsterstr. 57,Mannheim\n",
        encoding="utf-8",
    )

    # 2. 강형 2개(supplierID=int, postalCode=다수 int) 중 첫 행이 영국 우편번호(EC1 4SD)인 CSV
    suppliers_csv = tmp_path / "suppliers.csv"
    suppliers_csv.write_text(
        "supplierID,companyName,postalCode,country\n"
        "1,Exotic Liquids,EC1 4SD,UK\n"
        "2,New Orleans Cajun,70117,USA\n"
        "3,Grandma Kelly,48104,USA\n"
        "4,Tokyo Traders,100,Japan\n"
        "5,Mayumi,545,Japan\n",
        encoding="utf-8",
    )

    with MultiTableContext([messy_csv, suppliers_csv]) as mctx:
        bundles = profile_all_tables(mctx)
        by_name = {b.source.table_name: b for b in bundles}

        # customers.csv: 완화 파서로 복구 + 경고 기록
        cust = by_name["customers"]
        assert cust.source.row_count >= 3
        assert any("완화 파서" in w for w in mctx.warnings)

        # suppliers.csv: 첫 데이터 행(1, Exotic Liquids, EC1 4SD)을 2번째 헤더로 오인하지 않음
        sup = by_name["suppliers"]
        assert sup.ingest.header_row_count == 1
        assert sup.source.row_count == 5
        assert [c.name for c in sup.ingest.columns] == [
            "supplierID",
            "companyName",
            "postalCode",
            "country",
        ]


def test_header_only_empty_csv_raises_friendly_input_error(tmp_path: Path) -> None:
    """헤더만 있고 데이터 행이 0건인 CSV는 명확한 한국어 InputError를 반환한다."""
    empty_csv = tmp_path / "header_only.csv"
    empty_csv.write_text("id,name,amount\n", encoding="utf-8")

    with pytest.raises(InputError, match="데이터 행이 0건"):
        MultiTableContext([empty_csv])


def test_strict_boolean_pair_matching_and_dollar_decimal() -> None:
    """BOOLEAN은 정규 쌍({Y,N}/{O,X} 등)만 허용하고 {Y,N,x}/{x,y}는 문자열로 유지한다."""
    from autoetl.contracts import PhysicalType

    tbl = NamedTableInput(
        name="flags",
        headers=("yn_flag", "ox_flag", "bad_3val", "xy_axis", "usd_amt"),
        rows=(
            ("Y", "O", "Y", "x", "$1,200.50"),
            ("N", "X", "N", "y", "$3,450.00"),
            ("Y", "O", "x", "x", "$980.25"),
            ("N", "X", "Y", "y", "$12,500.00"),
            ("Y", "O", "N", "x", "$420.00"),
        ),
    )
    with MultiTableContext(extra_tables=[tbl]) as mctx:
        bundle = profile_all_tables(mctx)[0]
        type_by = {t.column: t.physical_type for t in bundle.types}
        assert type_by["yn_flag"] is PhysicalType.BOOLEAN
        assert type_by["ox_flag"] is PhysicalType.BOOLEAN
        assert type_by["bad_3val"] is PhysicalType.STRING
        assert type_by["xy_axis"] is PhysicalType.STRING
        assert type_by["usd_amt"] is PhysicalType.DECIMAL
