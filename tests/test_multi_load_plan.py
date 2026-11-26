"""Milestone E4: 다중 테이블 스키마 후보 채점·DDL 검증·위상 적재 플랜·ERD·번들 내보내기 테스트."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from sqlglot import parse as sqlglot_parse

from autoetl.contracts import IncrementMode, Purpose
from autoetl.core.db_loader import load_tables
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


def test_partial_string_pattern_loads_unmatched_rows_with_null_slots(tmp_path: Path) -> None:
    tables = _ecommerce_inputs()
    original_orders = next(table for table in tables if table.name == "orders")
    rows = tuple(
        (
            str(order_id),
            f"ORD-2026-{('KR', 'US', 'JP')[order_id % 3]}-{order_id:05d}"
            if order_id <= 1090
            else f"unstructured {order_id}",
            "101",
            "2026-01-01",
        )
        for order_id in range(1001, 1101)
    )
    tables = [
        NamedTableInput(name=table.name, headers=table.headers, rows=rows)
        if table is original_orders
        else table
        for table in tables
    ]
    db_path = tmp_path / "partial.sqlite"
    analysis, batch = load_tables(extra_tables=tables, db_path=db_path)
    candidate = next(
        sc for sc in analysis.load_plan.scorecards if sc.candidate_id == "C2_NORMALIZED"
    )
    assert candidate.validation.trial_load_ok is True
    assert analysis.load_plan.selected_candidate_id == "C2_NORMALIZED"
    assert batch.total_rows_quarantined == 0
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM orders WHERE branch_code IS NULL").fetchone() == (
            10,
        )
        assert conn.execute(
            "SELECT order_code, branch_code FROM orders WHERE order_id = 1001"
        ).fetchone() == ("ORD-2026-JP-01001", "JP")


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
    # 표본 관측 대안 유일 키는 물리 UNIQUE INDEX로 자동 승격하지 않는다(운영 23505 방지)
    assert 'CREATE UNIQUE INDEX "uq_orders_order_code"' not in lp.index_ddl_sql
    assert "uq_branches_branch_name" not in lp.index_ddl_sql
    assert "GENERATED ALWAYS AS IDENTITY" in lp.ddl_sql
    assert all(sc.validation.trial_load_executed for sc in lp.scorecards)
    assert all(sc.validation.trial_load_ok for sc in lp.scorecards)

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
    compact = next(
        sc for sc in archive_bundle.load_plan.scorecards if sc.candidate_id == "C2_COMPACT_RAW"
    )
    assert compact.redundancy.score == 80.0


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


def test_review2_financial_comma_decimal_cp949_comments_pii_and_ssot_types(
    tmp_path: Path,
) -> None:
    """리뷰2 통합 회귀: 쉼표 DECIMAL·CP949·COMMENT·PII·SSOT 타입·슬롯/이행 FK 정제."""
    from sqlglot import exp

    # 1. 지점 (UTF-8)
    (tmp_path / "branches.csv").write_text(
        "branch_code,branch_name,city\n"
        "BR01,강남본점,서울\n"
        "BR02,부산센텀,부산\n"
        "BR03,대전둔산,대전\n",
        encoding="utf-8",
    )
    # 2. 고객 (CP949 + 한글 컬럼명 + PII 컬럼 + 단일 슬롯 고객코드 C000161)
    (tmp_path / "customers.csv").write_text(
        "고객번호,성명,생년월일,전화번호,이메일\n"
        "C000101,홍길동,1988-05-12,010-1234-5678,hong@example.com\n"
        "C000102,김영희,1992-11-03,010-2345-6789,kim@example.com\n"
        "C000103,이철수,1985-07-21,010-3456-7890,lee@example.com\n"
        "C000104,박민지,1995-02-14,010-4567-8901,park@example.com\n",
        encoding="cp949",
    )
    # 3. 계좌 (balance에 천단위 쉼표 포함, branch_code 직접 FK)
    (tmp_path / "accounts.csv").write_text(
        "account_no,고객번호,branch_code,balance,open_date\n"
        '110-100-000001,C000101,BR01,"30,989,665",2024-01-10\n'
        '110-100-000002,C000102,BR02,"12,450,000",2024-03-15\n'
        '110-100-000003,C000103,BR01,"8,720,500",2024-06-20\n'
        '110-100-000004,C000104,BR03,"45,100,000",2024-09-01\n',
        encoding="utf-8",
    )
    # 4. 거래 (account_no 직접 FK + tx_id 내부에 이행적 BR01 슬롯 포함)
    (tmp_path / "transactions.csv").write_text(
        "tx_id,account_no,tx_date,tx_amount\n"
        'TX-2026-BR01-0001,110-100-000001,2026-01-02,"1,500,000"\n'
        'TX-2026-BR02-0002,110-100-000002,2026-01-03,"250,000"\n'
        'TX-2026-BR01-0003,110-100-000003,2026-01-04,"90,000"\n'
        'TX-2026-BR03-0004,110-100-000004,2026-01-05,"3,200,000"\n',
        encoding="utf-8",
    )

    out_dir = tmp_path / "out_fin"
    bundle = analyze_tables(sources=[tmp_path], purpose=Purpose.OLTP, out_dir=out_dir)
    lp = bundle.load_plan
    plan_by = {tp.target_table: tp for tp in lp.table_plans}

    # P-01: 쉼표 포함 balance(DECIMAL)가 strip_currency_and_comma 변환식을 가짐
    acct_maps = {m.source_column: m for m in plan_by["accounts"].column_mappings}
    assert acct_maps["balance"].transform == "strip_currency_and_comma"

    # P-02: 실제 DuckDB 시험 적재 수행 및 통과
    selected_sc = next(sc for sc in lp.scorecards if sc.candidate_id == lp.selected_candidate_id)
    assert selected_sc.validation.trial_load_executed is True
    assert selected_sc.validation.trial_load_ok is True
    assert selected_sc.validation.cast_failure_count == 0
    assert selected_sc.validation.constraint_violation_count == 0
    assert selected_sc.validation.data_loss_rate == 0.0

    # P-04 & 리뷰1 §8: ddl.sql 타입과 load_plan.json type_cast가 1:1 일치
    ddl_types: dict[tuple[str, str], str] = {}
    for stmt in sqlglot_parse(lp.ddl_sql, read="postgres"):
        if isinstance(stmt, exp.Create) and isinstance(stmt.this, exp.Schema):
            tname = stmt.this.this.name
            for cdef in stmt.this.expressions:
                if isinstance(cdef, exp.ColumnDef) and cdef.kind is not None:
                    ddl_types[(tname, cdef.name)] = cdef.kind.sql(dialect="postgres").lower()
    for tp in lp.table_plans:
        for m in tp.column_mappings:
            assert m.type_cast == ddl_types[(tp.target_table, m.target_column)]

    # P-05 & P-07: CP949 인코딩 보존 + 한글 컬럼 COMMENT ON COLUMN 생성 및 sqlglot 검증
    assert plan_by["customers"].source_encoding.lower() in {"cp949", "euc-kr"}
    assert "COMMENT ON COLUMN" in lp.comment_ddl_sql
    assert "'고객번호'" in lp.comment_ddl_sql
    comment_stmts = [s for s in sqlglot_parse(lp.comment_ddl_sql, read="postgres") if s is not None]
    assert comment_stmts and all(isinstance(s, exp.Comment) for s in comment_stmts)
    assert "-- Column Comments" in (out_dir / "ddl.sql").read_text(encoding="utf-8")

    # P-06: PII 민감 컬럼 탐지 (원본 셀 값 비노출 보장)
    cust_pii = set(plan_by["customers"].sensitive_columns)
    assert {"성명", "생년월일", "전화번호", "이메일"} <= cust_pii
    assert "account_no" in set(plan_by["accounts"].sensitive_columns)
    assert selected_sc.security_isolation.score < 90.0
    report_md = (out_dir / "analysis_report.md").read_text(encoding="utf-8")
    assert "개인정보(PII)" in report_md
    assert "hong@example.com" not in report_md
    assert "010-1234-5678" not in report_md

    # P-07: 1:1 중복 단일 슬롯 제거 및 OLTP 이행적 슬롯 FK(transactions -> branches) 생략
    cust_target_cols = {m.target_column for m in plan_by["customers"].column_mappings}
    assert not any("code_0" in c or c.endswith("_seq") for c in cust_target_cols)
    tx_tbl = next(
        t
        for c in lp.candidates
        if c.id == lp.selected_candidate_id
        for t in c.tables
        if t.name == "transactions"
    )
    tx_fk_parents = {c.foreign_key.split("(", 1)[0] for c in tx_tbl.columns if c.foreign_key}
    assert "accounts" in tx_fk_parents
    assert "branches" not in tx_fk_parents


def test_review3_orphan_fk_trial_load_fails_honestly() -> None:
    """리뷰3 #1: 고아 FK 행(C99999) 존재 시 trial_load_ok=False·fk_violation_count=1 보고."""
    customers = NamedTableInput(
        name="customers",
        headers=("customer_id", "customer_name"),
        rows=tuple((f"C{i:03d}", f"고객{i}") for i in range(1, 11)),
    )
    # 30건 중 29건은 정상(96.7% 포함률 >= 95% → 물리 FK 탐지), 1건은 고아 행 C99999
    order_rows = [
        (f"O{i:03d}", f"C{((i - 1) % 10) + 1:03d}", f"2026-01-{(i % 28) + 1:02d}", "10000")
        for i in range(1, 30)
    ]
    order_rows.append(("O030", "C99999", "2026-01-30", "15000"))
    orders = NamedTableInput(
        name="orders",
        headers=("order_id", "customer_id", "order_date", "amount"),
        rows=tuple(order_rows),
    )

    bundle = analyze_tables(extra_tables=[customers, orders], purpose=Purpose.OLTP)
    lp = bundle.load_plan
    selected_sc = next(sc for sc in lp.scorecards if sc.candidate_id == lp.selected_candidate_id)

    assert selected_sc.validation.ddl_parse_ok is True
    assert selected_sc.validation.trial_load_executed is True
    assert selected_sc.validation.trial_load_ok is False
    assert selected_sc.validation.fk_violation_count == 1
    assert selected_sc.validation.constraint_violation_count == 1
    assert selected_sc.validation.data_loss_rate > 0.0
    assert selected_sc.integrity.score < 90.0
    assert any("고아 행" in w for w in lp.cycle_warnings)


def test_review3_postal_code_not_promoted_to_unique_index_and_surrogate_identity() -> None:
    """리뷰3 #2/#4: 표본 유일 postal_code의 UNIQUE 승격 차단 및 서로게이트 IDENTITY DDL 검증."""
    from sqlglot import exp

    customers = NamedTableInput(
        name="customers",
        headers=("customer_id", "postal_code", "city"),
        rows=(
            ("C001", "06236", "서울"),
            ("C002", "48058", "부산"),
            ("C003", "35209", "대전"),
            ("C004", "61945", "광주"),
            ("C005", "41911", "대구"),
        ),
    )
    activity = NamedTableInput(
        name="activity",
        headers=("customer_id", "event_type"),
        rows=(
            ("C001", "CLICK"),
            ("C001", "CLICK"),
            ("C002", "VIEW"),
        ),
    )

    bundle = analyze_tables(extra_tables=[customers, activity], purpose=Purpose.OLTP)
    lp = bundle.load_plan

    # postal_code에 대한 CREATE UNIQUE INDEX 금지
    assert "CREATE UNIQUE INDEX" not in lp.index_ddl_sql
    assert "postal_code" not in lp.index_ddl_sql

    # activity 서로게이트 PK는 GENERATED ALWAYS AS IDENTITY 포함 + sqlglot AST 검증
    stmts = [s for s in sqlglot_parse(lp.ddl_sql, read="postgres") if isinstance(s, exp.Create)]
    act_create = next(
        s for s in stmts if isinstance(s.this, exp.Schema) and s.this.this.name == "activity"
    )
    id_constraint = act_create.find(exp.GeneratedAsIdentityColumnConstraint)
    assert id_constraint is not None


def test_review3_analytics_declarative_partition_matches_ddl_and_pk() -> None:
    """리뷰3 #6: C3_ANALYTICS 파티셔닝이 실제 DDL PARTITION BY RANGE 및 복합 PK와 100% 일치한다."""
    from sqlglot import exp

    customers = NamedTableInput(
        name="customers",
        headers=("customer_id", "grade"),
        rows=(("101", "VIP"), ("102", "일반"), ("103", "골드")),
    )
    orders = NamedTableInput(
        name="orders",
        headers=("order_id", "customer_id", "order_date", "amount"),
        rows=(
            ("1", "101", "2026-01-05", "50000"),
            ("2", "102", "2026-01-12", "30000"),
            ("3", "103", "2026-02-01", "75000"),
            ("4", "101", "2026-02-18", "20000"),
        ),
    )

    bundle = analyze_tables(extra_tables=[customers, orders], purpose=Purpose.ANALYTICS)
    lp = bundle.load_plan
    assert lp.selected_candidate_id == "C3_ANALYTICS"

    c3 = next(c for c in lp.candidates if c.id == "C3_ANALYTICS")
    orders_tbl = next(t for t in c3.tables if t.name == "orders")
    assert orders_tbl.partition == "RANGE (order_date)"

    # DDL에 실제 PARTITION BY RANGE("order_date")와 복합 PK("order_id", "order_date") 존재
    assert 'PARTITION BY RANGE("order_date")' in lp.ddl_sql
    stmts = [s for s in sqlglot_parse(lp.ddl_sql, read="postgres") if isinstance(s, exp.Create)]
    orders_create = next(
        s for s in stmts if isinstance(s.this, exp.Schema) and s.this.this.name == "orders"
    )
    part_prop = orders_create.find(exp.PartitionedByProperty)
    assert part_prop is not None
    pk = orders_create.find(exp.PrimaryKey)
    assert pk is not None
    assert [c.name for c in pk.expressions] == ["order_id", "order_date"]


def test_decimal_precision_scale_preserves_full_scale_and_int_digits() -> None:
    """M2: 폴백 경로에서 소수 12자리 보존 및 p<=38 초과 시 정수부 자릿수를 우선 보존한다."""
    from autoetl.contracts import (
        ColumnStats,
        NumericStats,
        OutlierMethod,
        OutlierSummary,
        StatsKind,
    )
    from autoetl.core.load_planner import _decimal_precision_scale

    outliers = OutlierSummary(method=OutlierMethod.IQR, count=0, rate=0.0, bounds="[]")
    high_scale_stat = ColumnStats(
        column="rate",
        kind=StatsKind.NUMERIC,
        numeric=NumericStats(
            mean=0.1,
            std=0.05,
            minimum=0.0,
            maximum=0.123456789012,
            p25=0.0,
            p50=0.1,
            p75=0.12,
            skew=0.0,
            max_decimal_digits=12,
            outliers=outliers,
        ),
    )
    assert _decimal_precision_scale(high_scale_stat) == (13, 12)

    overflow_stat = ColumnStats(
        column="big_val",
        kind=StatsKind.NUMERIC,
        numeric=NumericStats(
            mean=1.0,
            std=0.0,
            minimum=0.0,
            maximum=10**19,
            p25=0.0,
            p50=1.0,
            p75=1.0,
            skew=0.0,
            max_decimal_digits=20,
            outliers=outliers,
        ),
    )
    assert _decimal_precision_scale(overflow_stat) == (38, 18)
