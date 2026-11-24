"""Milestone E4: 다중 테이블 스키마 후보 채점·DDL 검증·위상 적재 플랜·ERD·번들 내보내기 테스트."""

from __future__ import annotations

import json
from pathlib import Path

from sqlglot import parse as sqlglot_parse

from autoetl.contracts import IncrementMode, Purpose
from autoetl.core.load_planner import export_multi_table_bundle, render_analysis_report
from autoetl.core.multi_analyze import analyze_tables
from autoetl.core.multi_context import NamedTableInput


def _ecommerce_inputs() -> list[NamedTableInput]:
    return [
        NamedTableInput(
            name="branches",
            headers=("branch_code", "branch_name", "city"),
            rows=(
                ("KR", "서울본점", "서울"),
                ("US", "뉴욕지사", "뉴욕"),
                ("JP", "도쿄지사", "도쿄"),
            ),
        ),
        NamedTableInput(
            name="customers",
            headers=("customer_id", "customer_name", "grade", "city", "district"),
            rows=(
                ("101", "김민수", "VIP", "서울", "강남구"),
                ("102", "이서연", "골드", "서울", "서초구"),
                ("103", "박지훈", "일반", "부산", "해운대구"),
                ("104", "최유진", "골드", "서울", "강남구"),
            ),
        ),
        NamedTableInput(
            name="products",
            headers=("product_id", "product_name", "category"),
            rows=(
                ("501", "노트북 프로", "전자"),
                ("502", "기계식 키보드", "전자"),
                ("503", "스탠딩 데스크", "가구"),
            ),
        ),
        NamedTableInput(
            name="orders",
            headers=("order_id", "order_code", "customer_id", "order_date"),
            rows=(
                ("1001", "ORD-2025-KR-0001", "101", "2025-11-01"),
                ("1002", "ORD-2025-US-0002", "102", "2025-11-02"),
                ("1003", "ORD-2026-JP-0003", "103", "2026-01-10"),
                ("1004", "ORD-2026-KR-0004", "101", "2026-02-15"),
                ("1005", "ORD-2026-US-0005", "104", "2026-03-01"),
            ),
        ),
        NamedTableInput(
            name="order_items",
            headers=("order_id", "line_no", "product_id", "unit_price", "qty", "total_amount"),
            rows=(
                ("1001", "1", "501", "1500000", "1", "1500000"),
                ("1001", "2", "502", "120000", "2", "240000"),
                ("1002", "1", "503", "450000", "1", "450000"),
                ("1003", "1", "501", "1500000", "2", "3000000"),
                ("1004", "1", "502", "120000", "3", "360000"),
                ("1005", "1", "503", "450000", "2", "900000"),
            ),
        ),
        NamedTableInput(
            name="audit_logs",
            headers=("event_type", "actor"),
            rows=(
                ("LOGIN", "kim"),
                ("LOGIN", "kim"),
                ("EXPORT", "lee"),
            ),
        ),
    ]


def test_oltp_load_plan_topological_order_slot_fk_and_ddl(tmp_path: Path) -> None:
    """OLTP 목적: 정규화 후보 선택·숨은 슬롯 FK DDL 반영·위상 적재 순서·번들 내보내기 검증."""
    out_dir = tmp_path / "etl_oltp"
    bundle = analyze_tables(
        extra_tables=_ecommerce_inputs(),
        purpose=Purpose.OLTP,
        decompose_strings=True,
        out_dir=out_dir,
    )
    lp = bundle.load_plan

    # 1. 모든 후보가 sqlglot DDL 검증 통과 + OLTP에서는 정규화 후보 선택
    assert all(sc.validation.ddl_parse_ok for sc in lp.scorecards)
    assert lp.selected_candidate_id == "C2_NORMALIZED"

    # 2. 위상 적재 순서: 부모(branches, customers, products) < orders < order_items
    order_pos = {lo.table: idx for idx, lo in enumerate(lp.load_order)}
    assert order_pos["branches"] < order_pos["orders"]
    assert order_pos["customers"] < order_pos["orders"]
    assert order_pos["orders"] < order_pos["order_items"]
    assert order_pos["products"] < order_pos["order_items"]

    # 3. 테이블별 증분 전략 검증
    plan_by_table = {tp.target_table: tp for tp in lp.table_plans}
    assert plan_by_table["orders"].increment_mode is IncrementMode.WATERMARK
    assert plan_by_table["orders"].watermark_column == "order_date"
    assert plan_by_table["customers"].increment_mode is IncrementMode.UPSERT
    assert plan_by_table["customers"].upsert_keys == ["customer_id"]
    assert plan_by_table["audit_logs"].increment_mode is IncrementMode.APPEND
    assert plan_by_table["audit_logs"].uses_surrogate_key is True

    # 4. 숨은 문자열 슬롯 FK(orders.order_code → branch_code → branches.branch_code)가
    #    ColumnMapping(regexp_extract)과 PostgreSQL DDL FOREIGN KEY에 모두 반영됨
    orders_maps = {m.target_column: m for m in plan_by_table["orders"].column_mappings}
    assert "branch_code" in orders_maps
    assert orders_maps["branch_code"].source_column == "order_code"
    assert "regexp_extract" in orders_maps["branch_code"].transform

    assert 'FOREIGN KEY ("branch_code") REFERENCES "branches"("branch_code")' in lp.ddl_sql
    assert 'FOREIGN KEY ("customer_id") REFERENCES "customers"("customer_id")' in lp.ddl_sql
    assert 'CREATE UNIQUE INDEX "uq_orders_order_code"' in lp.index_ddl_sql
    assert "uq_branches_branch_name" not in lp.index_ddl_sql

    # 5. 정규화 후보에서 파생 컬럼 total_amount는 order_items 물리 컬럼에서 제외됨
    oi_cols = {m.target_column for m in plan_by_table["order_items"].column_mappings}
    assert "total_amount" not in oi_cols

    # 6. 내보내기 산출물 6종 검증 + DDL 전체 재파싱
    expected_files = [
        "ddl.sql",
        "load_plan.json",
        "relations.json",
        "analysis_bundle.json",
        "erd.mmd",
        "analysis_report.md",
    ]
    for fname in expected_files:
        fpath = out_dir / fname
        assert fpath.exists() and fpath.stat().st_size > 0

    ddl_text = (out_dir / "ddl.sql").read_text(encoding="utf-8")
    parsed_stmts = [s for s in sqlglot_parse(ddl_text, read="postgres") if s is not None]
    assert len(parsed_stmts) >= 6

    erd_text = (out_dir / "erd.mmd").read_text(encoding="utf-8")
    assert erd_text.startswith("erDiagram\n")
    assert "branches ||--o{ orders" in erd_text

    loaded_plan = json.loads((out_dir / "load_plan.json").read_text(encoding="utf-8"))
    assert loaded_plan["selected_candidate_id"] == "C2_NORMALIZED"

    report_md = (out_dir / "analysis_report.md").read_text(encoding="utf-8")
    assert "규칙성 문자열 내부 구조 분해" in report_md
    assert "최적 DB 위상 적재 계획" in report_md


def test_purpose_changes_selected_candidate() -> None:
    """같은 데이터라도 Purpose(analytics / archive)에 따라 최적 스키마 후보가 달라진다."""
    tables = _ecommerce_inputs()

    analytics_bundle = analyze_tables(extra_tables=tables, purpose=Purpose.ANALYTICS)
    assert analytics_bundle.load_plan.selected_candidate_id == "C3_ANALYTICS"
    # 분석 목적에서는 파생 지표 total_amount가 물리 컬럼으로 유지됨
    oi_plan = next(
        tp for tp in analytics_bundle.load_plan.table_plans if tp.target_table == "order_items"
    )
    assert any(m.target_column == "total_amount" for m in oi_plan.column_mappings)

    archive_bundle = analyze_tables(extra_tables=tables, purpose=Purpose.ARCHIVE)
    assert archive_bundle.load_plan.selected_candidate_id == "C2_COMPACT_RAW"


def test_korean_table_and_column_names_produce_valid_ddl_and_report(tmp_path: Path) -> None:
    """한글 파일명·한글 칼럼명 CSV에서도 PostgreSQL 호환 식별자·검증된 DDL·리포트가 생성된다."""
    customers_csv = tmp_path / "고객목록.csv"
    customers_csv.write_text(
        "고객코드,고객명,등급\nC001,홍길동,VIP\nC002,김철수,일반\nC003,이영희,골드\n",
        encoding="utf-8",
    )
    orders_csv = tmp_path / "주문내역.csv"
    orders_csv.write_text(
        "주문번호,고객코드,주문일자,결제금액\n"
        "1,C001,2026년 4월 1일,150000\n"
        "2,C002,2026년 4월 2일,82000\n"
        "3,C001,2026년 4월 3일,45000\n",
        encoding="utf-8",
    )

    bundle = analyze_tables(sources=[tmp_path], purpose=Purpose.OLTP)
    assert all(sc.validation.ddl_parse_ok for sc in bundle.load_plan.scorecards)
    assert len(bundle.relations.foreign_keys) >= 1

    # 원본 한글 컬럼명은 ColumnMapping.source_column과 리포트에 그대로 보존됨
    all_sources = {
        m.source_column for tp in bundle.load_plan.table_plans for m in tp.column_mappings
    }
    assert "고객코드" in all_sources
    assert "주문일자" in all_sources

    # 한글 날짜 파싱 변환식 확인
    order_maps = [
        m
        for tp in bundle.load_plan.table_plans
        for m in tp.column_mappings
        if m.source_column == "주문일자"
    ]
    assert order_maps and order_maps[0].transform == "parse_korean_date"

    written = export_multi_table_bundle(bundle, tmp_path / "out_kr")
    assert set(written.keys()) == {
        "ddl.sql",
        "load_plan.json",
        "relations.json",
        "analysis_bundle.json",
        "erd.mmd",
        "analysis_report.md",
    }
    md = render_analysis_report(bundle)
    assert "고객목록" in md and "주문내역" in md
