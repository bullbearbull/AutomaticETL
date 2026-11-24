"""사용자 확정 스키마 오버라이드(마지막 10%) 및 실제 DB 적재·관리 테이블 회귀 테스트."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest

import autoetl
from autoetl.cli import main
from autoetl.contracts import (
    ColumnOverride,
    DatasetKind,
    MaskingPolicy,
    OrphanPolicy,
    Purpose,
    SchemaOverrideSpec,
    SensitiveType,
)
from autoetl.core.db_loader import (
    DEFAULT_MANAGEMENT_TABLES,
    inspect_db_catalog,
    load_tables,
    mask_value,
    render_management_tables_ddl,
    rollback_batch,
)
from autoetl.core.errors import InputError
from autoetl.core.multi_analyze import analyze_tables


def _write_korean_finance_fixture(
    data_dir: Path,
    *,
    include_orphan: bool = False,
) -> tuple[Path, Path, Path]:
    data_dir.mkdir(parents=True, exist_ok=True)
    branches = data_dir / "branches.csv"
    branches.write_text(
        "지점코드,지점명\nKR,서울본점\nUS,뉴욕지사\nJP,도쿄지사\n",
        encoding="utf-8",
    )
    customers = data_dir / "customers.csv"
    customers.write_text(
        "고객번호,고객명,전화번호,이메일,생년월일,활성여부\n"
        "C001,홍길동,010-1234-5678,honggildong@example.com,1988-05-12,예\n"
        "C002,김영희,010-9876-5432,kimyh@example.com,1992-11-03,예\n"
        "C003,이철수,010-5555-0001,chulsoo@example.com,1985-01-20,아니오\n",
        encoding="utf-8",
    )
    orders_lines = [
        "주문번호,주문코드,고객번호,주문일자,결제금액",
        '1001,ORD-2025-KR-0001,C001,2025년 11월 1일,"120,000원"',
        '1002,ORD-2025-US-0002,C002,2025년 11월 2일,"85,500원"',
        '1003,ORD-2026-JP-0003,C003,2026년 1월 10일,"42,000원"',
        '1004,ORD-2026-KR-0004,C001,2026년 2월 15일,"310,000원"',
    ]
    if include_orphan:
        branch_cycle = ("KR", "US", "JP")
        cust_cycle = ("C001", "C002", "C003")
        for i in range(5, 21):
            b_code = branch_cycle[i % 3]
            c_code = cust_cycle[i % 3]
            orders_lines.append(
                f"{1000 + i},ORD-2026-{b_code}-{i:04d},{c_code},"
                f'2026년 2월 {(i % 25) + 1}일,"{50 + i},000원"'
            )
        orders_lines.append('1021,ORD-2026-KR-0021,C999,2026년 2월 26일,"99,000원"')
    orders = data_dir / "orders.csv"
    orders.write_text("\n".join(orders_lines) + "\n", encoding="utf-8")
    return branches, customers, orders


def test_column_rename_unique_and_masking_overrides_propagate_to_ddl_and_trial_load(
    tmp_path: Path,
) -> None:
    """한글→영문명 확정, UNIQUE 제약, PII 마스킹이 DDL·FK·채점에 일관 반영된다."""
    data_dir = tmp_path / "ov_data"
    branches, customers, orders = _write_korean_finance_fixture(data_dir)

    baseline = analyze_tables([branches, customers, orders], purpose=Purpose.OLTP)
    base_sc = next(
        s
        for s in baseline.load_plan.scorecards
        if s.candidate_id == baseline.load_plan.selected_candidate_id
    )
    # 기본 상태에서는 PII가 미보호라 security_isolation 점수가 낮다
    assert base_sc.security_isolation.score <= 75.0

    spec = SchemaOverrideSpec(
        columns=[
            ColumnOverride(table="branches", source_column="지점코드", target_column="branch_code"),
            ColumnOverride(table="branches", source_column="지점명", target_column="branch_name"),
            ColumnOverride(
                table="customers", source_column="고객번호", target_column="customer_id"
            ),
            ColumnOverride(
                table="customers",
                source_column="고객명",
                target_column="customer_name",
                masking=MaskingPolicy.MASK,
            ),
            ColumnOverride(
                table="customers",
                source_column="전화번호",
                target_column="phone_number",
                masking=MaskingPolicy.MASK,
            ),
            ColumnOverride(
                table="customers",
                source_column="이메일",
                target_column="email",
                unique=True,
                masking=MaskingPolicy.HASH,
            ),
            ColumnOverride(
                table="customers",
                source_column="생년월일",
                target_column="birth_date",
                masking=MaskingPolicy.MASK,
            ),
            ColumnOverride(table="orders", source_column="주문번호", target_column="order_id"),
            ColumnOverride(
                table="orders",
                source_column="주문코드",
                target_column="order_code",
                unique=True,
            ),
            ColumnOverride(table="orders", source_column="고객번호", target_column="customer_id"),
        ]
    )

    confirmed = analyze_tables(
        [branches, customers, orders],
        purpose=Purpose.OLTP,
        overrides=spec,
    )
    lp = confirmed.load_plan
    assert '"customer_id"' in lp.ddl_sql
    assert '"branch_code"' in lp.ddl_sql
    assert 'REFERENCES "customers"("customer_id")' in lp.ddl_sql
    assert 'REFERENCES "branches"("branch_code")' in lp.ddl_sql
    assert "CREATE UNIQUE INDEX" in lp.index_ddl_sql
    assert '"email"' in lp.index_ddl_sql
    assert '"order_code"' in lp.index_ddl_sql
    assert "COMMENT ON COLUMN" in lp.comment_ddl_sql
    assert "'고객번호'" in lp.comment_ddl_sql
    assert "customer_id" in lp.erd_mermaid

    conf_sc = next(s for s in lp.scorecards if s.candidate_id == lp.selected_candidate_id)
    assert conf_sc.validation.ddl_parse_ok is True
    assert conf_sc.validation.trial_load_executed is True
    assert conf_sc.validation.trial_load_ok is True
    # 모든 L2/L3 PII 컬럼 보호 확정 시 보안 점수가 95.0으로 상승
    assert conf_sc.security_isolation.score == 95.0

    # 중복이 있는 컬럼에 잘못 UNIQUE를 지정하면 DuckDB 시험 적재가 즉시 위반을 탐지한다
    bad_spec = SchemaOverrideSpec(
        columns=[
            ColumnOverride(table="orders", source_column="고객번호", unique=True),
        ]
    )
    bad_res = analyze_tables(
        [branches, customers, orders], purpose=Purpose.OLTP, overrides=bad_spec
    )
    bad_sc = next(
        s
        for s in bad_res.load_plan.scorecards
        if s.candidate_id == bad_res.load_plan.selected_candidate_id
    )
    assert bad_sc.validation.trial_load_ok is False
    assert bad_sc.validation.pk_violation_count >= 1


def test_sqlite_real_load_management_tables_masking_and_catalog(tmp_path: Path) -> None:
    """SQLite 실 DB 적재 + 디폴트 4종 관리 테이블 + PII 마스킹/해시 + 행 이력 + 카탈로그 조회."""
    data_dir = tmp_path / "sqlite_src"
    branches, customers, orders = _write_korean_finance_fixture(data_dir)
    db_file = tmp_path / "warehouse.sqlite"

    spec = SchemaOverrideSpec(
        columns=[
            ColumnOverride(table="branches", source_column="지점코드", target_column="branch_code"),
            ColumnOverride(table="branches", source_column="지점명", target_column="branch_name"),
            ColumnOverride(
                table="customers", source_column="고객번호", target_column="customer_id"
            ),
            ColumnOverride(
                table="customers",
                source_column="고객명",
                target_column="customer_name",
                masking=MaskingPolicy.MASK,
            ),
            ColumnOverride(
                table="customers",
                source_column="전화번호",
                target_column="phone_number",
                masking=MaskingPolicy.MASK,
            ),
            ColumnOverride(
                table="customers",
                source_column="이메일",
                target_column="email",
                unique=True,
                masking=MaskingPolicy.HASH,
            ),
            ColumnOverride(
                table="customers",
                source_column="생년월일",
                target_column="birth_date",
                masking=MaskingPolicy.MASK,
            ),
            ColumnOverride(table="customers", source_column="활성여부", target_column="is_active"),
            ColumnOverride(table="orders", source_column="주문번호", target_column="order_id"),
            ColumnOverride(
                table="orders", source_column="주문코드", target_column="order_code", unique=True
            ),
            ColumnOverride(table="orders", source_column="고객번호", target_column="customer_id"),
            ColumnOverride(table="orders", source_column="주문일자", target_column="order_date"),
            ColumnOverride(table="orders", source_column="결제금액", target_column="amount"),
        ]
    )

    _bundle, result = load_tables(
        [branches, customers, orders],
        db_path=db_file,
        engine="sqlite",
        overrides=spec,
        notes="11월 초기 적재",
    )
    assert result.status == "succeeded"
    assert result.total_rows_read == 10
    assert result.total_rows_loaded == 10
    assert result.total_rows_quarantined == 0
    assert set(result.management_tables_created) == set(DEFAULT_MANAGEMENT_TABLES)

    # 실제 SQLite 테이블 내용 직접 검증 (타입 변환·마스킹·해시·슬롯 추출·이력 컬럼)
    con = sqlite3.connect(str(db_file))
    try:
        cust_row = con.execute(
            "SELECT customer_id, customer_name, phone_number, email, birth_date, is_active, "
            "_etl_batch_id, _etl_dataset_id, _etl_loaded_at "
            "FROM customers WHERE customer_id = 'C001'"
        ).fetchone()
        assert cust_row is not None
        assert cust_row[1] == "홍*동"
        assert cust_row[2] == "010-****-5678"
        assert str(cust_row[3]).startswith("h_") and len(str(cust_row[3])) == 18
        assert cust_row[4] == "1988-**-**"
        assert cust_row[5] == 1  # '예' -> boolean 1
        assert cust_row[6] == result.batch_id
        assert str(cust_row[7]).startswith("ds_customers_")
        assert cust_row[8] == result.loaded_at

        ord_row = con.execute(
            "SELECT order_id, customer_id, branch_code, order_date, amount "
            "FROM orders WHERE order_id = 1001"
        ).fetchone()
        assert ord_row is not None
        assert ord_row[1] == "C001"
        assert ord_row[2] == "KR"  # 숨은 슬롯 FK 추출 값!
        assert ord_row[3] == "2025-11-01"  # 한국어 날짜 변환!
        assert ord_row[4] == pytest.approx(120000.0)  # 쉼표·통화기호 제거!
    finally:
        con.close()

    # 카탈로그 조회 검증
    catalog = inspect_db_catalog(db_file, engine="sqlite")
    assert set(catalog["management_tables"]) == set(DEFAULT_MANAGEMENT_TABLES)
    assert len(catalog["batches"]) == 1
    assert catalog["batches"][0]["batch_id"] == result.batch_id
    assert catalog["batches"][0]["notes"] == "11월 초기 적재"

    kind_by_table = {d["target_table"]: d["dataset_kind"] for d in catalog["datasets"]}
    assert kind_by_table["branches"] == DatasetKind.MASTER_REFERENCE.value
    assert kind_by_table["customers"] == DatasetKind.MASTER_REFERENCE.value
    assert kind_by_table["orders"] == DatasetKind.TRANSACTION_EVENT.value
    assert len(catalog["columns"]) >= 13
    email_reg = next(c for c in catalog["columns"] if c["target_column"] == "email")
    assert email_reg["source_column"] == "이메일"
    assert email_reg["masking_policy"] == "hash"
    assert email_reg["is_unique"] is True


def test_duckdb_real_load_and_typed_queries(tmp_path: Path) -> None:
    """DuckDB 실 DB 파일(.duckdb) 적재 시 DECIMAL/DATE/BOOLEAN/FK/관리 테이블이 정확히 생성된다."""
    data_dir = tmp_path / "duck_src"
    branches, customers, orders = _write_korean_finance_fixture(data_dir)
    db_file = tmp_path / "analytics.duckdb"

    _bundle, result = load_tables(
        [branches, customers, orders],
        db_path=db_file,
        engine="duckdb",
        purpose=Purpose.ANALYTICS,
        auto_mask_pii=True,
    )
    assert result.status == "succeeded"
    assert result.total_rows_loaded == 10
    assert db_file.is_file()

    con = duckdb.connect(str(db_file), read_only=True)
    try:
        cnt_orders = con.execute("SELECT COUNT(*) FROM orders").fetchone()
        assert cnt_orders is not None and cnt_orders[0] == 4
        # PII 자동 마스킹 확인 (원본 이름·전화번호·이메일이 DB에 평문 저장되지 않음)
        all_cust_rows = con.execute("SELECT * FROM customers").fetchall()
        flat_text = " ".join(str(cell) for row in all_cust_rows for cell in row)
        assert "홍길동" not in flat_text
        assert "010-1234-5678" not in flat_text
        assert "홍*동" in flat_text
        assert "010-****-5678" in flat_text
    finally:
        con.close()

    cat = inspect_db_catalog(db_file, engine="duckdb")
    assert len(cat["batches"]) == 1
    assert len(cat["datasets"]) == 3
    masked_cols = [c for c in cat["columns"] if c["masking_policy"] == "mask"]
    assert len(masked_cols) >= 3  # 고객명·전화번호·이메일·생년월일 등 L2 PII 자동 보호


def test_orphan_fk_quarantine_vs_reject_all_and_pii_safe_quarantine(tmp_path: Path) -> None:
    """고아 FK 행은 QUARANTINE 모드에서 격리함으로 분리되고, REJECT_ALL 모드에서는 전체 롤백된다."""
    data_dir = tmp_path / "orphan_src"
    branches, customers, orders = _write_korean_finance_fixture(data_dir, include_orphan=True)

    # 1. QUARANTINE 모드: 정상 26건 저장(3+3+20) + 고아 1건 격리함 기록
    q_db = tmp_path / "quarantine.sqlite"
    _b1, res_q = load_tables(
        [branches, customers, orders],
        db_path=q_db,
        engine="sqlite",
        overrides=SchemaOverrideSpec(orphan_policy=OrphanPolicy.QUARANTINE),
    )
    assert res_q.status == "partial"
    assert res_q.total_rows_loaded == 26
    assert res_q.total_rows_quarantined == 1

    cat_q = inspect_db_catalog(q_db, engine="sqlite")
    assert len(cat_q["quarantine"]) == 1
    q_entry = cat_q["quarantine"][0]
    assert q_entry["target_table"] == "orders"
    assert q_entry["error_category"] == "fk_orphan"
    assert q_entry["source_row_number"] == 21

    # 2. REJECT_ALL 모드: 전체 롤백 + 배치 상태 failed 감사 기록
    r_db = tmp_path / "reject.sqlite"
    _b2, res_r = load_tables(
        [branches, customers, orders],
        db_path=r_db,
        engine="sqlite",
        overrides=SchemaOverrideSpec(orphan_policy=OrphanPolicy.REJECT_ALL),
    )
    assert res_r.status == "failed"
    assert res_r.total_rows_loaded == 0

    cat_r = inspect_db_catalog(r_db, engine="sqlite")
    assert cat_r["business_tables"] == []  # 롤백되어 업무 테이블이 남아있지 않음
    assert len(cat_r["batches"]) == 1
    assert cat_r["batches"][0]["status"] == "failed"


def test_duplicate_file_skip_schema_drift_and_batch_rollback(tmp_path: Path) -> None:
    """동일 파일 중복 방지, 스키마 드리프트 감지, FK 보호 및 배치 역순 롤백을 검증한다."""
    data_dir = tmp_path / "lifecycle"
    branches, customers, orders = _write_korean_finance_fixture(data_dir)
    db_file = tmp_path / "lifecycle.sqlite"

    # 1차 적재
    _b1, res1 = load_tables([branches, customers, orders], db_path=db_file, engine="sqlite")
    assert res1.status == "succeeded"
    assert res1.total_rows_loaded == 10

    # 2차: 동일 파일 재적재 -> SHA-256 일치로 자동 건너뜀
    _b2, res2 = load_tables([branches, customers, orders], db_path=db_file, engine="sqlite")
    assert res2.status == "skipped_duplicate"
    assert res2.total_rows_loaded == 0
    assert all(t.skipped_duplicate_file for t in res2.tables)

    # 3차: 신규 컬럼 추가 후 적재 -> 스키마 드리프트 감지 + 컬럼 진화 + 기존 3행 UPSERT
    branches_v2 = tmp_path / "branches.csv"
    branches_v2.write_text(
        "지점코드,지점명,국가\nKR,서울본점,대한민국\nUS,뉴욕지사,미국\nJP,도쿄지사,일본\nSG,싱가포르,싱가포르\n",
        encoding="utf-8",
    )
    _b3, res3 = load_tables([branches_v2], db_path=db_file, engine="sqlite")
    assert res3.status == "succeeded"
    assert res3.tables[0].schema_drift_detected is True
    assert res3.tables[0].rows_loaded == 4

    cat_after_v2 = inspect_db_catalog(db_file, engine="sqlite")
    ds3 = next(d for d in cat_after_v2["datasets"] if d["batch_id"] == res3.batch_id)
    assert ds3["rows_updated"] == 3

    # 4차: 후속 배치(res3)가 res1의 기존 행을 갱신한 상태에서 res1을 먼저 롤백하려 하면 차단된다
    with pytest.raises(InputError, match="후속 배치"):
        rollback_batch(db_file, res1.batch_id, engine="sqlite")

    # 5차: UPSERT 배치(res3) 롤백 -> 신규 행(SG)만 삭제되고 기존 행(KR/US/JP)은 원복된다
    rb3 = rollback_batch(db_file, res3.batch_id, engine="sqlite")
    assert rb3["status"] == "rolled_back"
    assert rb3["deleted_by_table"]["branches"] == 1
    assert rb3["restored_by_table"]["branches"] == 3
    assert rb3["total_deleted"] == 1
    assert rb3["total_restored"] == 3

    con = sqlite3.connect(str(db_file))
    try:
        col_map = {
            c["source_column"]: c["target_column"]
            for c in cat_after_v2["columns"]
            if c["target_table"] == "branches"
        }
        code_col = col_map["지점코드"]
        name_col = col_map["지점명"]
        country_col = col_map["국가"]
        rows_after_rb3 = con.execute(
            f'SELECT "{code_col}", "{name_col}", "{country_col}", "_etl_batch_id" '
            f'FROM "branches" ORDER BY "{code_col}"'
        ).fetchall()
        assert rows_after_rb3 == [
            ("JP", "도쿄지사", None, res1.batch_id),
            ("KR", "서울본점", None, res1.batch_id),
            ("US", "뉴욕지사", None, res1.batch_id),
        ]
        ord_cnt = con.execute('SELECT COUNT(*) FROM "orders"').fetchone()
        assert ord_cnt is not None and ord_cnt[0] == 4
    finally:
        con.close()

    # 6차: res3 원복 후 1차 배치 역순 롤백(orders -> customers -> branches)으로 전체 삭제
    rb1 = rollback_batch(db_file, res1.batch_id, engine="sqlite")
    assert rb1["status"] == "rolled_back"
    assert next(iter(rb1["deleted_by_table"].keys())) == "orders"  # 자식(orders) 먼저 삭제!
    assert rb1["deleted_by_table"]["orders"] == 4
    assert rb1["deleted_by_table"]["customers"] == 3
    assert rb1["deleted_by_table"]["branches"] == 3
    assert rb1["total_deleted"] == 10
    assert rb1["total_restored"] == 0

    # 7차: 롤백 완료 후 동일 파일을 다시 넣으면 force 없이도 재적재 가능
    _b4, res4 = load_tables([branches, customers, orders], db_path=db_file, engine="sqlite")
    assert res4.status == "succeeded"
    assert res4.total_rows_loaded == 10


def test_upsert_rollback_fk_guard_chain_duckdb_and_legacy_safety(tmp_path: Path) -> None:
    """UPSERT 롤백 FK 보호·체인 순서 강제·DuckDB 타입 원복·구버전 스냅샷 부재 차단을 검증한다."""
    # 1. FK 보호: 신규 삽입 부모 행(SG)을 후속 배치의 자식 행이 참조 중이면 부모 배치 롤백 차단
    fk_dir = tmp_path / "fk_guard"
    fk_dir.mkdir(parents=True, exist_ok=True)
    branches = fk_dir / "branches.csv"
    branches.write_text(
        "branch_code,branch_name\nKR,Seoul\nUS,NewYork\nJP,Tokyo\n",
        encoding="utf-8",
    )
    orders = fk_dir / "orders.csv"
    orders.write_text(
        "order_id,branch_code,amount\n1001,KR,100\n1002,US,200\n1003,JP,300\n1004,KR,150\n",
        encoding="utf-8",
    )
    sqlite_db = tmp_path / "fk_guard.sqlite"
    _b1, res1 = load_tables([branches, orders], db_path=sqlite_db, engine="sqlite")
    assert res1.status == "succeeded"

    branches_v2 = tmp_path / "fk_v2" / "branches.csv"
    branches_v2.parent.mkdir(parents=True, exist_ok=True)
    branches_v2.write_text(
        "branch_code,branch_name\nKR,Seoul-HQ\nSG,Singapore\n",
        encoding="utf-8",
    )
    _b2, res2 = load_tables([branches_v2], db_path=sqlite_db, engine="sqlite")
    assert res2.status == "succeeded"

    orders_sg = tmp_path / "fk_v3" / "orders.csv"
    orders_sg.parent.mkdir(parents=True, exist_ok=True)
    orders_sg.write_text(
        "order_id,branch_code,amount\n1005,SG,400\n1006,KR,500\n1007,SG,450\n",
        encoding="utf-8",
    )
    # branches_v2는 SHA-256 중복으로 건너뛰고 orders만 신규 3건 적재(SG 참조)
    _b3, res3 = load_tables([branches_v2, orders_sg], db_path=sqlite_db, engine="sqlite")
    assert res3.status == "succeeded"

    with pytest.raises(InputError, match="참조하고 있어"):
        rollback_batch(sqlite_db, res2.batch_id, engine="sqlite")

    # 자식 배치(res3)를 먼저 롤백하면 res2도 정상 롤백되어 SG 삭제 + KR 원복된다
    rb3 = rollback_batch(sqlite_db, res3.batch_id, engine="sqlite")
    assert rb3["deleted_by_table"]["orders"] == 3
    rb2 = rollback_batch(sqlite_db, res2.batch_id, engine="sqlite")
    assert rb2["deleted_by_table"]["branches"] == 1
    assert rb2["restored_by_table"]["branches"] == 1

    con = sqlite3.connect(str(sqlite_db))
    try:
        kr = con.execute(
            'SELECT "branch_name", "_etl_batch_id" FROM "branches" WHERE "branch_code" = \'KR\''
        ).fetchone()
        assert kr == ("Seoul", res1.batch_id)
        sg = con.execute('SELECT * FROM "branches" WHERE "branch_code" = \'SG\'').fetchone()
        assert sg is None
    finally:
        con.close()

    # 2. DuckDB 다단계 UPSERT 체인(B1 -> B2 -> B3) + DATE/DECIMAL/BOOLEAN 타입 무손실 원복
    duck_db = tmp_path / "chain.duckdb"
    p1 = tmp_path / "c1" / "items.csv"
    p2 = tmp_path / "c2" / "items.csv"
    p3 = tmp_path / "c3" / "items.csv"
    for p in (p1, p2, p3):
        p.parent.mkdir(parents=True, exist_ok=True)
    p1.write_text(
        "item_id,item_name,unit_price,effective_date,is_active\n"
        '1,Alpha,"10,000.50",2025-01-10,true\n'
        '2,Beta,"20,000.00",2025-01-11,false\n',
        encoding="utf-8",
    )
    p2.write_text(
        "item_id,item_name,unit_price,effective_date,is_active\n"
        '1,Alpha-v2,"15,500.25",2025-02-20,false\n'
        '3,Gamma,"30,000.00",2025-02-21,true\n',
        encoding="utf-8",
    )
    p3.write_text(
        "item_id,item_name,unit_price,effective_date,is_active\n"
        '1,Alpha-v3,"19,900.75",2025-03-30,true\n'
        '4,Delta,"40,000.00",2025-03-31,false\n',
        encoding="utf-8",
    )
    _, d1 = load_tables([p1], db_path=duck_db, engine="duckdb")
    _, d2 = load_tables([p2], db_path=duck_db, engine="duckdb")
    _, d3 = load_tables([p3], db_path=duck_db, engine="duckdb")
    assert d1.status == d2.status == d3.status == "succeeded"

    # 역순 위반 차단: d1, d2는 후속 배치가 갱신했으므로 먼저 롤백할 수 없다
    with pytest.raises(InputError, match=d2.batch_id):
        rollback_batch(duck_db, d1.batch_id, engine="duckdb")
    with pytest.raises(InputError, match=d3.batch_id):
        rollback_batch(duck_db, d2.batch_id, engine="duckdb")

    # d3 -> d2 -> d1 역순 롤백 시 DECIMAL/DATE/BOOLEAN 값이 정확히 원복된다
    dr3 = rollback_batch(duck_db, d3.batch_id, engine="duckdb")
    assert dr3["total_deleted"] == 1 and dr3["total_restored"] == 1
    dcon = duckdb.connect(str(duck_db), read_only=True)
    try:
        row_after_d3 = dcon.execute(
            "SELECT item_name, unit_price, effective_date, is_active, _etl_batch_id "
            "FROM items WHERE item_id = 1"
        ).fetchone()
        assert row_after_d3 == (
            "Alpha-v2",
            Decimal("15500.25"),
            date(2025, 2, 20),
            False,
            d2.batch_id,
        )
        assert isinstance(row_after_d3[1], Decimal)
        assert isinstance(row_after_d3[3], bool)
    finally:
        dcon.close()

    dr2 = rollback_batch(duck_db, d2.batch_id, engine="duckdb")
    assert dr2["total_deleted"] == 1 and dr2["total_restored"] == 1
    dcon = duckdb.connect(str(duck_db), read_only=True)
    try:
        all_after_d2 = dcon.execute(
            "SELECT item_id, item_name, unit_price, effective_date, is_active, _etl_batch_id "
            "FROM items ORDER BY item_id"
        ).fetchall()
        assert all_after_d2 == [
            (1, "Alpha", Decimal("10000.50"), date(2025, 1, 10), True, d1.batch_id),
            (2, "Beta", Decimal("20000.00"), date(2025, 1, 11), False, d1.batch_id),
        ]
        assert all(isinstance(r[2], Decimal) and isinstance(r[4], bool) for r in all_after_d2)
    finally:
        dcon.close()

    dr1 = rollback_batch(duck_db, d1.batch_id, engine="duckdb")
    assert dr1["total_deleted"] == 2 and dr1["total_restored"] == 0

    # 3. 구버전 카탈로그 스키마(스냅샷·갱신 컬럼 자체가 없는 `_etl_datasets`) 호환성 검증:
    #    마이그레이션 전에는 UPSERT 롤백을 차단해 기존 행·배치 ID를 보존하고,
    #    다음 적재 시 누락된 스냅샷 컬럼들이 자동 진화 추가된다.
    legacy_db = tmp_path / "legacy.sqlite"
    _, l1 = load_tables([p1], db_path=legacy_db, engine="sqlite")
    _, l2 = load_tables([p2], db_path=legacy_db, engine="sqlite")
    lcon = sqlite3.connect(str(legacy_db))
    try:
        lcon.execute("PRAGMA foreign_keys = OFF")
        lcon.execute(
            'CREATE TABLE "_etl_datasets_old" ('
            '"dataset_id" TEXT PRIMARY KEY, '
            '"batch_id" TEXT NOT NULL REFERENCES "_etl_load_batches" ("batch_id"), '
            '"load_order_index" INTEGER NOT NULL, '
            '"target_table" TEXT NOT NULL, '
            '"dataset_kind" TEXT NOT NULL, '
            '"source_name" TEXT NOT NULL, '
            '"source_format" TEXT NOT NULL, '
            '"source_encoding" TEXT NOT NULL, '
            '"source_sha256" TEXT, '
            '"schema_fingerprint" TEXT NOT NULL, '
            '"increment_mode" TEXT NOT NULL, '
            '"primary_key_columns" TEXT NOT NULL, '
            '"rows_read" INTEGER NOT NULL, '
            '"rows_loaded" INTEGER NOT NULL, '
            '"rows_quarantined" INTEGER NOT NULL, '
            '"schema_drift_detected" INTEGER NOT NULL, '
            '"loaded_at" TEXT NOT NULL)'
        )
        lcon.execute(
            'INSERT INTO "_etl_datasets_old" '
            'SELECT "dataset_id", "batch_id", "load_order_index", "target_table", '
            '"dataset_kind", "source_name", "source_format", "source_encoding", '
            '"source_sha256", "schema_fingerprint", "increment_mode", "primary_key_columns", '
            '"rows_read", "rows_loaded", "rows_quarantined", "schema_drift_detected", '
            '"loaded_at" FROM "_etl_datasets"'
        )
        lcon.execute('DROP TABLE "_etl_datasets"')
        lcon.execute('ALTER TABLE "_etl_datasets_old" RENAME TO "_etl_datasets"')
        lcon.commit()
    finally:
        lcon.close()

    with pytest.raises(InputError, match="스냅샷이 없어"):
        rollback_batch(legacy_db, l2.batch_id, engine="sqlite")
    # 차단 후 행 값과 _etl_batch_id가 전혀 손상되지 않았는지 검증
    lcon = sqlite3.connect(str(legacy_db))
    try:
        rows_preserved = lcon.execute(
            "SELECT item_id, item_name, _etl_batch_id FROM items ORDER BY item_id"
        ).fetchall()
        assert rows_preserved == [
            (1, "Alpha-v2", l2.batch_id),
            (2, "Beta", l1.batch_id),
            (3, "Gamma", l2.batch_id),
        ]
    finally:
        lcon.close()

    # 다음 적재 시 구버전 `_etl_datasets`에 스냅샷 컬럼들이 자동 진화 추가되는지 확인
    _, l3 = load_tables([p3], db_path=legacy_db, engine="sqlite")
    assert l3.status == "succeeded"
    lcon = sqlite3.connect(str(legacy_db))
    try:
        migrated_cols = {
            str(r[1]) for r in lcon.execute('PRAGMA table_info("_etl_datasets")').fetchall()
        }
        assert {
            "rows_updated",
            "rollback_snapshots_json",
            "snapshot_source_batches_json",
        } <= migrated_cols
    finally:
        lcon.close()


def test_analytics_composite_surrogate_pk_sqlite_and_duckdb(tmp_path: Path) -> None:
    """Analytics 복합 PK(서로게이트 ID + 시계열)의 시험 적재·SQLite·DuckDB 적재를 검증한다."""
    data_dir = tmp_path / "analytics_surr"
    data_dir.mkdir(parents=True, exist_ok=True)
    users = data_dir / "users.csv"
    users.write_text(
        "user_id,user_name\n101,Alice\n102,Bob\n103,Charlie\n",
        encoding="utf-8",
    )
    # 자연 PK가 없는 이벤트 로그(동일 날짜·동일 사용자 중복 포함) + users FK + 시계열 컬럼
    events = data_dir / "events.csv"
    events.write_text(
        "user_id,event_date,event_type,score\n"
        "101,2026-01-01,click,10\n"
        "101,2026-01-01,click,10\n"
        "102,2026-01-01,view,5\n"
        "103,2026-01-02,purchase,99\n"
        "102,2026-01-02,view,5\n",
        encoding="utf-8",
    )

    # 1. 시험 적재: Analytics 복합 PK에서 동일 날짜 중복이 PK 위반으로 오탐되지 않음
    analysis = analyze_tables([users, events], purpose=Purpose.ANALYTICS)
    lp = analysis.load_plan
    selected = next(c for c in lp.candidates if c.id == lp.selected_candidate_id)
    ev_tbl = next(t for t in selected.tables if t.name == "events")
    ev_pks = [c for c in ev_tbl.columns if c.is_primary_key]
    assert len(ev_pks) == 2
    surr_col_name = next(c.name for c in ev_pks if c.is_identity)
    assert any(c.name == "event_date" and not c.is_identity for c in ev_pks)

    sc = next(s for s in lp.scorecards if s.candidate_id == lp.selected_candidate_id)
    assert sc.validation.trial_load_executed is True
    assert sc.validation.trial_load_ok is True
    assert sc.validation.pk_violation_count == 0

    # 2. SQLite 실 적재: 복합 PK에서는 AUTOINCREMENT 미생성이므로 명시 시퀀스 ID가 할당되어야 한다
    sqlite_db = tmp_path / "analytics_surr.sqlite"
    _, r1 = load_tables(
        [users, events], db_path=sqlite_db, engine="sqlite", purpose=Purpose.ANALYTICS
    )
    assert r1.status == "succeeded"
    assert r1.total_rows_loaded == 8
    assert r1.total_rows_quarantined == 0

    con = sqlite3.connect(str(sqlite_db))
    try:
        pragma = con.execute('PRAGMA table_info("events")').fetchall()
        pk_names = [r[1] for r in pragma if r[5] > 0]
        assert pk_names == [surr_col_name, "event_date"]
        surr_ids = [
            r[0]
            for r in con.execute(
                f'SELECT "{surr_col_name}" FROM "events" ORDER BY "{surr_col_name}"'
            )
        ]
        assert surr_ids == [1, 2, 3, 4, 5]
    finally:
        con.close()

    # 2차 배치 APPEND 시 기존 MAX(surrogate_id) 이후 번호(6, 7, 8, 9)로 이어서 적재된다
    events_b2 = tmp_path / "b2" / "events.csv"
    events_b2.parent.mkdir(parents=True, exist_ok=True)
    events_b2.write_text(
        "user_id,event_date,event_type,score\n"
        "101,2026-01-01,click,10\n"
        "101,2026-01-01,click,10\n"
        "102,2026-01-03,view,15\n"
        "103,2026-01-03,click,20\n",
        encoding="utf-8",
    )
    _, r2 = load_tables(
        [users, events_b2], db_path=sqlite_db, engine="sqlite", purpose=Purpose.ANALYTICS
    )
    assert r2.status == "succeeded"
    con = sqlite3.connect(str(sqlite_db))
    try:
        surr_ids_2 = [
            r[0]
            for r in con.execute(
                f'SELECT "{surr_col_name}" FROM "events" ORDER BY "{surr_col_name}"'
            )
        ]
        assert surr_ids_2 == [1, 2, 3, 4, 5, 6, 7, 8, 9]
    finally:
        con.close()

    # 2차 배치 롤백 시 6..9번만 삭제되고 1..5는 유지된다
    rb2 = rollback_batch(sqlite_db, r2.batch_id, engine="sqlite")
    assert rb2["deleted_by_table"]["events"] == 4

    # 비-IDENTITY 파티션 키(event_date)가 NULL인 행은 NOT NULL 위반으로 격리된다
    events_null = tmp_path / "b_null" / "events.csv"
    events_null.parent.mkdir(parents=True, exist_ok=True)
    events_null.write_text(
        "user_id,event_date,event_type,score\n"
        "101,2026-01-01,click,10\n"
        "101,2026-01-01,click,10\n"
        "102,,view,15\n"
        "103,2026-01-04,click,20\n",
        encoding="utf-8",
    )
    _, r_null = load_tables(
        [users, events_null], db_path=sqlite_db, engine="sqlite", purpose=Purpose.ANALYTICS
    )
    assert r_null.status == "partial"
    assert r_null.total_rows_loaded == 3
    assert r_null.total_rows_quarantined == 1
    cat_null = inspect_db_catalog(sqlite_db, engine="sqlite")
    assert cat_null["quarantine"][0]["error_category"] == "not_null_violation"
    assert cat_null["quarantine"][0]["error_column"] == "event_date"

    # 3. DuckDB 실 적재에서도 동일하게 1차/2차 모두 성공한다
    duck_db = tmp_path / "analytics_surr.duckdb"
    _, d1 = load_tables(
        [users, events], db_path=duck_db, engine="duckdb", purpose=Purpose.ANALYTICS
    )
    _, d2 = load_tables(
        [users, events_b2], db_path=duck_db, engine="duckdb", purpose=Purpose.ANALYTICS
    )
    assert d1.status == "succeeded" and d2.status == "succeeded"
    dcon = duckdb.connect(str(duck_db), read_only=True)
    try:
        ids = [
            r[0]
            for r in dcon.execute(
                f'SELECT "{surr_col_name}" FROM "events" ORDER BY "{surr_col_name}"'
            ).fetchall()
        ]
        assert ids == [1, 2, 3, 4, 5, 6, 7, 8, 9]
    finally:
        dcon.close()


def test_excel_serial_dates_20000_to_25568_trial_load_and_real_load(tmp_path: Path) -> None:
    """S3 경계(20000~60000) Excel 시리얼 날짜가 시험 적재·SQLite·DuckDB에서 격리 없이 변환된다."""
    serials = [20000, 22000, 25000, 25568, 25569, 45000, 60000]
    csv_lines = ["record_id,record_date,label"] + [
        f"{idx},{serial},s{serial}" for idx, serial in enumerate(serials, start=1)
    ]
    csv_path = tmp_path / "historical_dates.csv"
    csv_path.write_text("\n".join(csv_lines) + "\n", encoding="utf-8")

    expected_dates = [
        (idx, date(1899, 12, 30) + timedelta(days=serial))
        for idx, serial in enumerate(serials, start=1)
    ]

    analysis = analyze_tables([csv_path], purpose=Purpose.OLTP)
    date_type = next(t for t in analysis.tables[0].types if t.column == "record_date")
    assert date_type.physical_type.value == "date"
    sc = next(
        s
        for s in analysis.load_plan.scorecards
        if s.candidate_id == analysis.load_plan.selected_candidate_id
    )
    assert sc.validation.trial_load_executed is True
    assert sc.validation.trial_load_ok is True
    assert sc.validation.cast_failure_count == 0

    sqlite_db = tmp_path / "excel_dates.sqlite"
    _, s_res = load_tables([csv_path], db_path=sqlite_db, engine="sqlite")
    assert s_res.status == "succeeded"
    assert s_res.total_rows_loaded == 7
    assert s_res.total_rows_quarantined == 0
    scon = sqlite3.connect(str(sqlite_db))
    try:
        rows = scon.execute(
            "SELECT record_id, record_date FROM historical_dates ORDER BY record_id"
        ).fetchall()
        assert rows == [(rid, d.isoformat()) for rid, d in expected_dates]
    finally:
        scon.close()

    duck_db = tmp_path / "excel_dates.duckdb"
    _, d_res = load_tables([csv_path], db_path=duck_db, engine="duckdb")
    assert d_res.status == "succeeded"
    assert d_res.total_rows_loaded == 7
    assert d_res.total_rows_quarantined == 0
    dcon = duckdb.connect(str(duck_db), read_only=True)
    try:
        rows = dcon.execute(
            "SELECT record_id, record_date FROM historical_dates ORDER BY record_id"
        ).fetchall()
        assert rows == expected_dates
    finally:
        dcon.close()


def test_rollback_edge_cases_unique_order_generated_col_nocase_cap_and_without_rowid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """역순 UNIQUE 복원·GENERATED 컬럼 제외·COLLATE NOCASE·상한 보호·WITHOUT ROWID를 검증."""
    from autoetl.core import db_loader

    # 1. SQLite WITHOUT ROWID 테이블은 단일 INTEGER PK라도 자동 증가 생략 대상이 아니다
    wr_db = tmp_path / "without_rowid.sqlite"
    con = sqlite3.connect(str(wr_db))
    try:
        con.execute('CREATE TABLE "t_wr" ("id" INTEGER PRIMARY KEY, "v" TEXT) WITHOUT ROWID')
        con.execute('CREATE TABLE "t_normal" ("id" INTEGER PRIMARY KEY AUTOINCREMENT, "v" TEXT)')
        con.commit()
    finally:
        con.close()
    adapter = db_loader._DbAdapter(wr_db, "sqlite")
    try:
        assert adapter.sqlite_single_identity_pk("t_wr", "id") is False
        assert adapter.sqlite_single_identity_pk("t_normal", "id") is True
    finally:
        adapter.close()

    # 2. UNIQUE 값 이동((1, X),(2, Y) -> (1, Z),(2, X)) 후 역순 복원 + GENERATED 컬럼 + 삭제 행 오류
    u_db = tmp_path / "unique_gen.sqlite"
    con = sqlite3.connect(str(u_db))
    try:
        con.execute(
            'CREATE TABLE "codes" ('
            '"id" INTEGER PRIMARY KEY, '
            '"code" TEXT NOT NULL UNIQUE, '
            '"qty" INTEGER NOT NULL, '
            '"doubled" INTEGER GENERATED ALWAYS AS ("qty" * 2) STORED, '
            '"_etl_batch_id" TEXT NOT NULL, '
            '"_etl_dataset_id" TEXT NOT NULL, '
            '"_etl_loaded_at" TEXT NOT NULL'
            ")"
        )
        con.commit()
    finally:
        con.close()

    c1 = tmp_path / "u1" / "codes.csv"
    c2 = tmp_path / "u2" / "codes.csv"
    c1.parent.mkdir()
    c2.parent.mkdir()
    c1.write_text("id,code,qty\n1,X,10\n2,Y,20\n", encoding="utf-8")
    c2.write_text("id,code,qty\n1,Z,30\n2,X,40\n", encoding="utf-8")
    _, res1 = load_tables([c1], db_path=u_db, engine="sqlite")
    _, res2 = load_tables([c2], db_path=u_db, engine="sqlite")
    assert res1.status == res2.status == "succeeded"

    rb2 = rollback_batch(u_db, res2.batch_id, engine="sqlite")
    assert rb2["total_restored"] == 2
    con = sqlite3.connect(str(u_db))
    try:
        rows = con.execute(
            'SELECT "id", "code", "qty", "doubled", "_etl_batch_id" FROM "codes" ORDER BY "id"'
        ).fetchall()
        assert rows == [
            (1, "X", 10, 20, res1.batch_id),
            (2, "Y", 20, 40, res1.batch_id),
        ]
    finally:
        con.close()

    # 3. 갱신 행이 외부에서 삭제된 경우 전용 오류 메시지로 차단
    c3 = tmp_path / "u3" / "codes.csv"
    c3.parent.mkdir()
    c3.write_text("id,code,qty\n1,W,50\n2,Q,60\n", encoding="utf-8")
    _, res3 = load_tables([c3], db_path=u_db, engine="sqlite")
    con = sqlite3.connect(str(u_db))
    try:
        con.execute('DELETE FROM "codes" WHERE "id" = 1')
        con.commit()
    finally:
        con.close()
    with pytest.raises(InputError, match="이미 삭제되어"):
        rollback_batch(u_db, res3.batch_id, engine="sqlite")

    # 4. 외부 COLLATE NOCASE PK로 스냅샷과 실제 갱신 수가 어긋나면 스냅샷 생략 + 롤백 차단
    nc_db = tmp_path / "nocase.sqlite"
    con = sqlite3.connect(str(nc_db))
    try:
        con.execute(
            'CREATE TABLE "branches" ('
            '"code" TEXT NOT NULL, '
            '"name" TEXT NOT NULL, '
            '"_etl_batch_id" TEXT NOT NULL, '
            '"_etl_dataset_id" TEXT NOT NULL, '
            '"_etl_loaded_at" TEXT NOT NULL, '
            'PRIMARY KEY ("code" COLLATE NOCASE)'
            ")"
        )
        con.commit()
    finally:
        con.close()
    b1 = tmp_path / "nc1" / "branches.csv"
    b2 = tmp_path / "nc2" / "branches.csv"
    b1.parent.mkdir()
    b2.parent.mkdir()
    b1.write_text("code,name\nKR,Seoul\nUS,NewYork\n", encoding="utf-8")
    b2.write_text("code,name\nkr,Seoul-v2\nJP,Tokyo\n", encoding="utf-8")
    _, nc_res1 = load_tables([b1], db_path=nc_db, engine="sqlite")
    _, nc_res2 = load_tables([b2], db_path=nc_db, engine="sqlite")
    assert any("PK 비교 규칙 차이" in w for w in nc_res2.warnings)
    with pytest.raises(InputError, match="스냅샷이 없어"):
        rollback_batch(nc_db, nc_res2.batch_id, engine="sqlite")
    # 선행 배치 미확정 상태의 후속 갱신 배치가 있으면 선행 배치(nc_res1) 롤백도 보수적으로 차단
    with pytest.raises(InputError, match="후속 갱신 배치"):
        rollback_batch(nc_db, nc_res1.batch_id, engine="sqlite")

    # 5. MAX_ROLLBACK_SNAPSHOT_ROWS 초과 시 스냅샷 생략 + 경고 + 선행 배치 링크 보존
    cap_db = tmp_path / "cap.sqlite"
    _, cap_base = load_tables([b1], db_path=cap_db, engine="sqlite")
    b_cap = tmp_path / "nc3" / "branches.csv"
    b_cap.parent.mkdir()
    b_cap.write_text("code,name\nKR,Seoul-3\nUS,NY-3\n", encoding="utf-8")
    monkeypatch.setattr(db_loader, "MAX_ROLLBACK_SNAPSHOT_ROWS", 1)
    _, cap_res = load_tables([b_cap], db_path=cap_db, engine="sqlite")
    assert any("스냅샷 상한" in w for w in cap_res.warnings)
    with pytest.raises(InputError, match="스냅샷이 없어"):
        rollback_batch(cap_db, cap_res.batch_id, engine="sqlite")
    # 스냅샷 본문이 없어도 선행 배치 링크가 보존되어 cap_base 롤백이 차단되어야 한다.
    with pytest.raises(InputError, match="후속 배치"):
        rollback_batch(cap_db, cap_base.batch_id, engine="sqlite")
    monkeypatch.setattr(db_loader, "MAX_ROLLBACK_SNAPSHOT_ROWS", 50_000)

    # 6. MAX_ROLLBACK_SNAPSHOT_BYTES 초과 시 스냅샷 생략 + 바이트 경고 + 선행 배치 링크 보존
    byte_db = tmp_path / "bytes.sqlite"
    _, byte_base = load_tables([b1], db_path=byte_db, engine="sqlite")
    b_byte = tmp_path / "nc4" / "branches.csv"
    b_byte.parent.mkdir()
    b_byte.write_text("code,name\nKR,Seoul-big\nUS,NY-big\n", encoding="utf-8")
    monkeypatch.setattr(db_loader, "MAX_ROLLBACK_SNAPSHOT_BYTES", 50)
    _, byte_res = load_tables([b_byte], db_path=byte_db, engine="sqlite")
    assert any("바이트 상한" in w for w in byte_res.warnings)
    with pytest.raises(InputError, match="스냅샷이 없어"):
        rollback_batch(byte_db, byte_res.batch_id, engine="sqlite")
    with pytest.raises(InputError, match="후속 배치"):
        rollback_batch(byte_db, byte_base.batch_id, engine="sqlite")
    monkeypatch.setattr(db_loader, "MAX_ROLLBACK_SNAPSHOT_BYTES", 32 * 1024 * 1024)

    # 7. 컬럼 수준 TEXT COLLATE NOCASE PRIMARY KEY + UNIQUE 값 이동 시 역순 복원 순서 보존
    nocase_swap_db = tmp_path / "nocase_swap.sqlite"
    con = sqlite3.connect(str(nocase_swap_db))
    try:
        con.execute(
            'CREATE TABLE "slots" ('
            '"code" TEXT COLLATE NOCASE PRIMARY KEY, '
            '"tag" TEXT UNIQUE NOT NULL, '
            '"_etl_batch_id" TEXT NOT NULL, '
            '"_etl_dataset_id" TEXT NOT NULL, '
            '"_etl_loaded_at" TEXT NOT NULL'
            ")"
        )
        con.commit()
    finally:
        con.close()
    s1 = tmp_path / "sw1" / "slots.csv"
    s2 = tmp_path / "sw2" / "slots.csv"
    s1.parent.mkdir()
    s2.parent.mkdir()
    s1.write_text("code,tag\nKR,X\nUS,Y\n", encoding="utf-8")
    s2.write_text("code,tag\nkr,Z\nUS,X\n", encoding="utf-8")
    load_tables([s1], db_path=nocase_swap_db, engine="sqlite")
    _, sw_res2 = load_tables([s2], db_path=nocase_swap_db, engine="sqlite")
    assert sw_res2.status == "succeeded"
    sw_rb = rollback_batch(nocase_swap_db, sw_res2.batch_id, engine="sqlite")
    assert sw_rb["total_restored"] == 2
    con = sqlite3.connect(str(nocase_swap_db))
    try:
        sw_rows = con.execute('SELECT "code", "tag" FROM "slots" ORDER BY "code"').fetchall()
    finally:
        con.close()
    assert sw_rows == [("KR", "X"), ("US", "Y")]

    # 8. SQLite INTEGER PRIMARY KEY DESC NOT NULL은 rowid 별칭이 아니므로 명시적 대리키 할당
    desc_db = tmp_path / "desc_pk.sqlite"
    con = sqlite3.connect(str(desc_db))
    try:
        con.execute(
            'CREATE TABLE "events" ('
            '"event_id" INTEGER PRIMARY KEY DESC NOT NULL, '
            '"event_date" TEXT NOT NULL, '
            '"user_id" INTEGER NOT NULL, '
            '"_etl_batch_id" TEXT NOT NULL, '
            '"_etl_dataset_id" TEXT NOT NULL, '
            '"_etl_loaded_at" TEXT NOT NULL'
            ")"
        )
        con.commit()
    finally:
        con.close()
    adapter = db_loader._DbAdapter(desc_db, "sqlite")
    try:
        assert adapter.sqlite_single_identity_pk("events", "event_id") is False
    finally:
        adapter.close()
    ev_csv = tmp_path / "desc_ev" / "events.csv"
    ev_csv.parent.mkdir()
    ev_csv.write_text(
        "event_date,user_id\n2025-01-01,101\n2025-01-01,101\n2025-01-02,102\n",
        encoding="utf-8",
    )
    _, desc_res = load_tables([ev_csv], db_path=desc_db, engine="sqlite", purpose=Purpose.ANALYTICS)
    assert desc_res.status == "succeeded"
    assert desc_res.total_rows_loaded == 3


def test_mask_value_formats_and_management_ddl_dialects() -> None:
    """민감 유형별 결정론적 마스킹/해시 및 3종 방언 관리 테이블 DDL을 검증한다."""
    assert (
        mask_value("010-1234-5678", SensitiveType.PHONE_NUMBER, MaskingPolicy.MASK)
        == "010-****-5678"
    )
    assert (
        mask_value("hong@example.com", SensitiveType.EMAIL, MaskingPolicy.MASK)
        == "ho***@example.com"
    )
    assert (
        mask_value("900101-1234567", SensitiveType.RESIDENT_REGISTRATION_NUMBER, MaskingPolicy.MASK)
        == "900101-1******"
    )
    assert mask_value("홍길동", SensitiveType.NAME, MaskingPolicy.MASK) == "홍*동"
    assert mask_value("1988-05-12", SensitiveType.DATE_OF_BIRTH, MaskingPolicy.MASK) == "1988-**-**"
    h1 = mask_value("secret", SensitiveType.EMAIL, MaskingPolicy.HASH)
    h2 = mask_value("secret", SensitiveType.EMAIL, MaskingPolicy.HASH)
    assert h1 == h2 and h1.startswith("h_")

    for dialect in ("postgres", "sqlite", "duckdb"):
        ddl = render_management_tables_ddl(dialect)
        for tbl in DEFAULT_MANAGEMENT_TABLES:
            assert f'"{tbl}"' in ddl

    with pytest.raises(InputError):
        render_management_tables_ddl("mysql")


def test_cli_load_and_catalog_and_top_level_sdk_exports(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`autoetl load`, `autoetl catalog [--rollback]`, 최상위 SDK export를 검증한다."""
    data_dir = tmp_path / "cli_load_src"
    branches, customers, orders = _write_korean_finance_fixture(data_dir)
    db_file = tmp_path / "cli_out.sqlite"

    # 최상위 SDK 지연 export 검증
    assert autoetl.load_tables is load_tables
    assert autoetl.inspect_db_catalog is inspect_db_catalog
    assert autoetl.rollback_batch is rollback_batch

    # CLI load --mask-pii --json
    rc = main(
        [
            "load",
            str(branches),
            str(customers),
            str(orders),
            "--db",
            str(db_file),
            "--engine",
            "sqlite",
            "--mask-pii",
            "--json",
        ]
    )
    assert rc == 0
    load_out = json.loads(capsys.readouterr().out)
    assert load_out["status"] == "succeeded"
    assert load_out["total_rows_loaded"] == 10
    batch_id = load_out["batch_id"]

    # CLI catalog --json
    rc_cat = main(["catalog", str(db_file), "--engine", "sqlite", "--json"])
    assert rc_cat == 0
    cat_out = json.loads(capsys.readouterr().out)
    assert len(cat_out["batches"]) == 1
    assert len(cat_out["datasets"]) == 3

    # CLI catalog --rollback <batch_id> --json
    rc_rb = main(["catalog", str(db_file), "--engine", "sqlite", "--rollback", batch_id, "--json"])
    assert rc_rb == 0
    rb_out = json.loads(capsys.readouterr().out)
    assert rb_out["status"] == "rolled_back"
    assert rb_out["total_deleted"] == 10


def test_rollback_snapshot_preserves_json_nested_and_uuid_types(tmp_path: Path) -> None:
    """스냅샷 직렬화·역직렬화 및 엔진별 복원 바인딩이 JSON/배열/구조체/UUID를 손실 없이 보존한다."""
    import uuid
    from datetime import date, datetime, timezone
    from datetime import time as dt_time
    from decimal import Decimal

    import duckdb

    from autoetl.core import db_loader

    sample_uuid = uuid.UUID("12345678-1234-5678-1234-567812345678")
    # 내부 마커 키(__etl_type__)를 포함한 실제 JSON 객체도 충돌 없이 왕복해야 한다.
    payload_values = [
        {"active": True, "nested": [1, None, {"k": "한글"}]},
        {"__etl_type__": "decimal", "v": "literal-not-tag"},
        [1, {"flag": False}, Decimal("12.34")],
        (10, date(2025, 1, 2)),
        sample_uuid,
        Decimal("99.95"),
        datetime(2025, 5, 1, 12, 30, tzinfo=timezone.utc),
        dt_time(9, 15, 0),
        b"\x00\x01\xff",
    ]
    for orig in payload_values:
        encoded = db_loader._encode_snapshot_value(orig)
        wire = json.loads(json.dumps(encoded, ensure_ascii=False))
        decoded = db_loader._decode_snapshot_value(wire)
        assert decoded == orig
        assert type(decoded) is type(orig)

    # PostgreSQL JSON/JSONB/배열 원문 텍스트 래퍼: JSON null vs SQL NULL 및 정확한 큰 정수 보존
    pg_null = db_loader._PgTypedText("jsonb", "null")
    pg_big_num = db_loader._PgTypedText("jsonb", '{"big": 9007199254740993}')
    pg_jsonb_arr = db_loader._PgTypedText("jsonb[]", '{"{\\"a\\": 1}","null",NULL}')
    for typed_val in (pg_null, pg_big_num, pg_jsonb_arr):
        enc = db_loader._encode_snapshot_value(typed_val)
        dec = db_loader._decode_snapshot_value(json.loads(json.dumps(enc, ensure_ascii=False)))
        assert dec == typed_val
        assert db_loader._bind_restore_value(dec, typed_val.pg_type, "postgres") == typed_val.text
    # SQL NULL은 여전히 None으로 유지되어 JSON 리터럴 "null"과 완전히 구분된다.
    assert db_loader._bind_restore_value(None, "jsonb", "postgres") is None
    assert db_loader._bind_restore_value(pg_null, "jsonb", "postgres") == "null"

    # PostgreSQL JSON/JSONB·jsonb[] 원소별 바인딩 (네이티브 스칼라 배열 int4[]는 그대로 유지)
    pg_obj = db_loader._bind_restore_value({"active": True}, "jsonb", "postgres")
    if isinstance(pg_obj, str):
        assert json.loads(pg_obj) == {"active": True}
    else:
        assert getattr(pg_obj, "obj", None) == {"active": True}
    pg_scalar = db_loader._bind_restore_value("hello", "json", "postgres")
    if isinstance(pg_scalar, str):
        assert json.loads(pg_scalar) == "hello"
    else:
        assert getattr(pg_scalar, "obj", None) == "hello"
    pg_arr_bound = db_loader._bind_restore_value(
        [{"a": 1}, None, {"b": True}], "jsonb[]", "postgres"
    )
    assert isinstance(pg_arr_bound, list) and len(pg_arr_bound) == 3
    assert pg_arr_bound[1] is None
    if isinstance(pg_arr_bound[0], str):
        assert json.loads(pg_arr_bound[0]) == {"a": 1}
        assert json.loads(pg_arr_bound[2]) == {"b": True}
    else:
        assert getattr(pg_arr_bound[0], "obj", None) == {"a": 1}
        assert getattr(pg_arr_bound[2], "obj", None) == {"b": True}
    assert db_loader._bind_restore_value([1, 2], "int4[]", "postgres") == [1, 2]
    assert db_loader._bind_restore_value([1, 2], "array", "postgres") == [1, 2]
    # pg_catalog 정규화 캐스트 및 구버전 다차원 jsonb[] 중첩 리스트 안전 거부 검증
    assert db_loader._PG_TEXT_CAST_TYPES["jsonb"] == "pg_catalog.jsonb"
    assert db_loader._PG_TEXT_CAST_TYPES["jsonb[]"] == "pg_catalog.jsonb[]"
    with pytest.raises(InputError, match="배열 차원"):
        db_loader._bind_restore_value([[None, None], [None, None]], "jsonb[]", "postgres")
    # SQLite fallback도 유효 JSON 문자열로 직렬화
    assert json.loads(db_loader._bind_restore_value({"active": True}, "text", "sqlite")) == {
        "active": True
    }

    # DuckDB 사전 생성 테이블(JSON · LIST · STRUCT 컬럼 포함):
    # 실제로 복합 컬럼을 모두 변경한 뒤 스냅샷 복원이 원값을 되돌리는지 검증
    ddb_path = tmp_path / "complex.duckdb"
    con = duckdb.connect(str(ddb_path))
    try:
        con.execute(
            'CREATE TABLE "items" ('
            '"code" VARCHAR PRIMARY KEY, '
            '"name" VARCHAR NOT NULL, '
            '"meta_json" JSON, '
            '"tags" VARCHAR[], '
            '"spec" STRUCT(active BOOLEAN, ver INTEGER), '
            '"_etl_batch_id" VARCHAR, '
            '"_etl_dataset_id" VARCHAR, '
            '"_etl_loaded_at" VARCHAR)'
        )
        con.execute(
            'INSERT INTO "items" VALUES (?, ?, ?::JSON, ?, ?, ?, ?, ?)',
            [
                "A1",
                "OldName",
                json.dumps({"active": True, "score": 1}),
                ["alpha", "beta"],
                {"active": True, "ver": 1},
                "seed-batch",
                "seed-ds",
                "2025-01-01T00:00:00Z",
            ],
        )
    finally:
        con.close()

    # 1) CSV에 meta_json 컬럼을 포함해 실제 JSON 컬럼 값을 덮어쓰고 롤백 복원 검증
    csv_u = tmp_path / "items.csv"
    csv_u.write_text(
        'code,name,meta_json\nA1,NewName,"{""active"": false, ""score"": 99}"\n'
        'A2,Added,"{""active"": true, ""score"": 5}"\n',
        encoding="utf-8",
    )
    _, res = load_tables([csv_u], db_path=ddb_path, engine="duckdb")
    assert res.status == "succeeded"

    # 롤백 전 실제로 meta_json이 새 값으로 바뀌었는지 확인
    con = duckdb.connect(str(ddb_path))
    try:
        mid = con.execute(
            'SELECT "name", "meta_json" FROM "items" WHERE "code" = \'A1\''
        ).fetchone()
        assert mid is not None
        assert mid[0] == "NewName"
        assert json.loads(str(mid[1])) == {"active": False, "score": 99}
    finally:
        con.close()

    rb = rollback_batch(ddb_path, res.batch_id, engine="duckdb")
    assert rb["total_deleted"] == 1
    assert rb["total_restored"] == 1

    con = duckdb.connect(str(ddb_path))
    try:
        rows = con.execute(
            'SELECT "code", "name", "meta_json", "tags", "spec", "_etl_batch_id" '
            'FROM "items" ORDER BY "code"'
        ).fetchall()
    finally:
        con.close()
    assert len(rows) == 1
    code, name, meta_json, tags, spec, bid = rows[0]
    assert code == "A1"
    assert name == "OldName"
    assert json.loads(str(meta_json)) == {"active": True, "score": 1}
    assert tags == ["alpha", "beta"]
    assert spec == {"active": True, "ver": 1}
    assert bid == "seed-batch"

    # 2) DuckDB LIST(tags) · STRUCT(spec) 컬럼도 실제로 변경된 후 스냅샷으로 원복되는지 검증
    adapter = db_loader._DbAdapter(ddb_path, "duckdb")
    try:
        full_snaps, omit = db_loader._fetch_existing_rows_by_pks(
            adapter, "items", ["code"], [("A1",)], columns=None
        )
        assert omit is None and full_snaps is not None and len(full_snaps) == 1
        adapter.begin()
        adapter.execute(
            'UPDATE "items" SET "name" = ?, "meta_json" = ?::JSON, "tags" = ?, '
            '"spec" = ?, "_etl_batch_id" = ? WHERE "code" = ?',
            [
                "Mutated",
                json.dumps({"active": False, "score": 0}),
                ["gamma"],
                {"active": False, "ver": 9},
                "batch_complex_test",
                "A1",
            ],
        )
        adapter.execute(
            'INSERT INTO "_etl_load_batches" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (
                "batch_complex_test",
                "2026-01-01T00:00:00Z",
                "oltp",
                "c1",
                "duckdb",
                "succeeded",
                1,
                1,
                1,
                0,
                "{}",
                "",
            ),
        )
        adapter.execute(
            'INSERT INTO "_etl_datasets" VALUES ('
            "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "ds_complex_test",
                "batch_complex_test",
                0,
                "items",
                "standalone",
                "items",
                "csv",
                "utf-8",
                None,
                "fp",
                "upsert",
                json.dumps(["code"]),
                1,
                1,
                0,
                0,
                "2026-01-01T00:00:00Z",
                1,
                json.dumps(full_snaps, ensure_ascii=False),
                json.dumps(["seed-batch"]),
            ),
        )
        adapter.commit()
    finally:
        adapter.close()

    # 변경 확인
    con = duckdb.connect(str(ddb_path))
    try:
        mut = con.execute('SELECT "tags", "spec" FROM "items" WHERE "code" = \'A1\'').fetchone()
        assert mut == (["gamma"], {"active": False, "ver": 9})
    finally:
        con.close()

    rb_complex = rollback_batch(ddb_path, "batch_complex_test", engine="duckdb")
    assert rb_complex["total_restored"] == 1
    con = duckdb.connect(str(ddb_path))
    try:
        restored_row = con.execute(
            'SELECT "name", "meta_json", "tags", "spec", "_etl_batch_id" '
            'FROM "items" WHERE "code" = \'A1\''
        ).fetchone()
        assert restored_row is not None
        assert restored_row[0] == "OldName"
        assert json.loads(str(restored_row[1])) == {"active": True, "score": 1}
        assert restored_row[2] == ["alpha", "beta"]
        assert restored_row[3] == {"active": True, "ver": 1}
        assert restored_row[4] == "seed-batch"
    finally:
        con.close()


def test_rollback_interleaved_order_self_fk_cascade_and_unique_reuse(tmp_path: Path) -> None:
    """혼합 배치 롤백이 정확한 역순으로 실행되어 자기참조 CASCADE FK와 UNIQUE 재사용을 보존한다."""
    import sqlite3

    # 1. 자기참조 ON DELETE CASCADE FK: 배치 2가 신규 B를 삽입한 뒤 기존 A가 B를 참조하도록 갱신
    #    롤백은 반드시 A 복원(B 참조 해제) -> B 삭제 순으로 실행되어야 A가 연쇄 삭제되지 않는다.
    sfk_db = tmp_path / "self_fk.sqlite"
    con = sqlite3.connect(str(sfk_db))
    try:
        con.execute("PRAGMA foreign_keys = ON")
        con.execute(
            'CREATE TABLE "nodes" ('
            '"id" TEXT PRIMARY KEY, '
            '"parent_id" TEXT REFERENCES "nodes" ("id") ON DELETE CASCADE, '
            '"_etl_batch_id" TEXT NOT NULL, '
            '"_etl_dataset_id" TEXT NOT NULL, '
            '"_etl_loaded_at" TEXT NOT NULL)'
        )
        con.commit()
    finally:
        con.close()

    n1 = tmp_path / "sfk1" / "nodes.csv"
    n2 = tmp_path / "sfk2" / "nodes.csv"
    n1.parent.mkdir()
    n2.parent.mkdir()
    n1.write_text("id,parent_id\nA,\nR,\n", encoding="utf-8")
    # Row 0: 신규 B 삽입(parent=NULL), Row 1: 기존 A를 parent=B로 갱신
    n2.write_text("id,parent_id\nB,\nA,B\n", encoding="utf-8")

    _, sr1 = load_tables([n1], db_path=sfk_db, engine="sqlite")
    _, sr2 = load_tables([n2], db_path=sfk_db, engine="sqlite")
    assert sr2.status == "succeeded"

    srb2 = rollback_batch(sfk_db, sr2.batch_id, engine="sqlite")
    assert srb2["total_deleted"] == 1
    assert srb2["total_restored"] == 1

    con = sqlite3.connect(str(sfk_db))
    try:
        node_rows = con.execute(
            'SELECT "id", "parent_id", "_etl_batch_id" FROM "nodes" ORDER BY "id"'
        ).fetchall()
    finally:
        con.close()
    assert node_rows == [("A", None, sr1.batch_id), ("R", None, sr1.batch_id)]

    # 2. 구버전 스냅샷(삽입 마커 없음)이 자기참조 FK 혼합 배치를 포함하면 삭제 전에 롤백 차단
    _, sr3 = load_tables([n2], db_path=sfk_db, engine="sqlite", force_reload=True)
    con = sqlite3.connect(str(sfk_db))
    try:
        raw_snap = con.execute(
            'SELECT "rollback_snapshots_json" FROM "_etl_datasets" WHERE "batch_id" = ?',
            (sr3.batch_id,),
        ).fetchone()
        assert raw_snap is not None
        only_restores = [s for s in json.loads(str(raw_snap[0])) if "__etl_op__" not in s]
        con.execute(
            'UPDATE "_etl_datasets" SET "rollback_snapshots_json" = ? WHERE "batch_id" = ?',
            (json.dumps(only_restores, ensure_ascii=False), sr3.batch_id),
        )
        con.commit()
    finally:
        con.close()

    with pytest.raises(InputError, match="자기참조 외래 키"):
        rollback_batch(sfk_db, sr3.batch_id, engine="sqlite")
    # 차단 후 A, B 모두 삭제되지 않고 보존됨을 검증
    con = sqlite3.connect(str(sfk_db))
    try:
        preserved = con.execute('SELECT "id", "parent_id" FROM "nodes" ORDER BY "id"').fetchall()
        assert preserved == [("A", "B"), ("B", None), ("R", None)]
    finally:
        con.close()

    # 3. 반대 순서(기존 행 갱신으로 UNIQUE 해제 -> 후속 신규 행 삽입으로 UNIQUE 재사용):
    #    롤백은 반드시 S2 삭제(UNIQUE 해제) -> S1 복원 순으로 실행되어야 UNIQUE 충돌이 없다.
    uq_db = tmp_path / "uq_reuse.sqlite"
    con = sqlite3.connect(str(uq_db))
    try:
        con.execute(
            'CREATE TABLE "slots" ('
            '"slot_id" TEXT PRIMARY KEY, '
            '"tag" TEXT UNIQUE NOT NULL, '
            '"_etl_batch_id" TEXT NOT NULL, '
            '"_etl_dataset_id" TEXT NOT NULL, '
            '"_etl_loaded_at" TEXT NOT NULL)'
        )
        con.commit()
    finally:
        con.close()

    u1 = tmp_path / "uqr1" / "slots.csv"
    u2 = tmp_path / "uqr2" / "slots.csv"
    u1.parent.mkdir()
    u2.parent.mkdir()
    u1.write_text("slot_id,tag\nS1,OLD\nS3,KEEP\n", encoding="utf-8")
    # Row 0: S1을 OLD -> TEMP로 갱신, Row 1: 신규 S2가 해방된 OLD를 재사용
    u2.write_text("slot_id,tag\nS1,TEMP\nS2,OLD\n", encoding="utf-8")

    _, ur1 = load_tables([u1], db_path=uq_db, engine="sqlite")
    _, ur2 = load_tables([u2], db_path=uq_db, engine="sqlite")
    assert ur2.status == "succeeded"

    urb2 = rollback_batch(uq_db, ur2.batch_id, engine="sqlite")
    assert urb2["total_deleted"] == 1
    assert urb2["total_restored"] == 1

    con = sqlite3.connect(str(uq_db))
    try:
        slot_rows = con.execute(
            'SELECT "slot_id", "tag", "_etl_batch_id" FROM "slots" ORDER BY "slot_id"'
        ).fetchall()
    finally:
        con.close()
    assert slot_rows == [("S1", "OLD", ur1.batch_id), ("S3", "KEEP", ur1.batch_id)]


def test_rollback_hardening_insert_cap_fallback_numeric_pk_and_delete_trigger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """대량 삽입+소량 갱신 폴백, SQLite NUMERIC PK 동등성, 삭제 무시 트리거 차단을 검증한다."""
    from autoetl.core import db_loader

    # 1. 대량 삽입 + 소량 갱신 혼합 배치에서 삽입 마커가 행 상한을 넘어도
    #    일반 테이블은 갱신 스냅샷만 보존하는 폴백으로 정상 롤백되어야 한다.
    fb_db = tmp_path / "fallback.sqlite"
    f1 = tmp_path / "fb1" / "branches.csv"
    f2 = tmp_path / "fb2" / "branches.csv"
    f1.parent.mkdir()
    f2.parent.mkdir()
    f1.write_text("code,name\nKR,Seoul\nUS,NY\n", encoding="utf-8")
    # 갱신 1건(KR) + 신규 삽입 3건(JP, DE, FR) = 총 4행 > MAX_ROLLBACK_SNAPSHOT_ROWS(2)
    f2.write_text(
        "code,name\nKR,Seoul-v2\nJP,Tokyo\nDE,Berlin\nFR,Paris\n",
        encoding="utf-8",
    )
    _, fb_r1 = load_tables([f1], db_path=fb_db, engine="sqlite")
    monkeypatch.setattr(db_loader, "MAX_ROLLBACK_SNAPSHOT_ROWS", 2)
    _, fb_r2 = load_tables([f2], db_path=fb_db, engine="sqlite")
    monkeypatch.setattr(db_loader, "MAX_ROLLBACK_SNAPSHOT_ROWS", 50_000)
    assert fb_r2.status == "succeeded"
    assert not any("스냅샷 상한" in w for w in fb_r2.warnings)

    fb_rb = rollback_batch(fb_db, fb_r2.batch_id, engine="sqlite")
    assert fb_rb["total_deleted"] == 3
    assert fb_rb["total_restored"] == 1
    con = sqlite3.connect(str(fb_db))
    try:
        fb_rows = con.execute(
            'SELECT "code", "name", "_etl_batch_id" FROM "branches" ORDER BY "code"'
        ).fetchall()
    finally:
        con.close()
    assert fb_rows == [("KR", "Seoul", fb_r1.batch_id), ("US", "NY", fb_r1.batch_id)]

    # 2. SQLite NUMERIC PK("001" -> 1): 혼합 배치 롤백 소유권 검증·삭제가 DB 동등성으로 통과
    num_db = tmp_path / "numeric_pk.sqlite"
    con = sqlite3.connect(str(num_db))
    try:
        con.execute(
            'CREATE TABLE "codes" ('
            '"code_id" NUMERIC PRIMARY KEY, '
            '"label" TEXT NOT NULL, '
            '"_etl_batch_id" TEXT NOT NULL, '
            '"_etl_dataset_id" TEXT NOT NULL, '
            '"_etl_loaded_at" TEXT NOT NULL)'
        )
        con.commit()
    finally:
        con.close()
    c1 = tmp_path / "num1" / "codes.csv"
    c2 = tmp_path / "num2" / "codes.csv"
    c1.parent.mkdir()
    c2.parent.mkdir()
    c1.write_text("code_id,label\nA1,Alpha\nB2,Beta\n", encoding="utf-8")
    # A1 갱신 + "001" 신규 삽입(SQLite NUMERIC 친화도로 정수 1로 저장됨)
    c2.write_text("code_id,label\nA1,Alpha-v2\n001,NumericOne\n", encoding="utf-8")
    _, nr1 = load_tables([c1], db_path=num_db, engine="sqlite")
    _, nr2 = load_tables([c2], db_path=num_db, engine="sqlite")
    assert nr2.status == "succeeded"

    nrb = rollback_batch(num_db, nr2.batch_id, engine="sqlite")
    assert nrb["total_deleted"] == 1
    assert nrb["total_restored"] == 1
    con = sqlite3.connect(str(num_db))
    try:
        num_rows = con.execute(
            'SELECT "code_id", "label", "_etl_batch_id" FROM "codes" ORDER BY "code_id"'
        ).fetchall()
    finally:
        con.close()
    assert num_rows == [("A1", "Alpha", nr1.batch_id), ("B2", "Beta", nr1.batch_id)]

    # 3. BEFORE DELETE 트리거가 신규 행 삭제를 무시(RAISE(IGNORE))할 때 잔존 행을 감지해 롤백 중단
    trg_db = tmp_path / "skip_delete_trigger.sqlite"
    con = sqlite3.connect(str(trg_db))
    try:
        con.execute(
            'CREATE TABLE "nodes" ('
            '"node_id" TEXT PRIMARY KEY, '
            '"val" TEXT NOT NULL, '
            '"_etl_batch_id" TEXT NOT NULL, '
            '"_etl_dataset_id" TEXT NOT NULL, '
            '"_etl_loaded_at" TEXT NOT NULL)'
        )
        con.execute(
            'CREATE TRIGGER skip_b_del BEFORE DELETE ON "nodes" '
            "WHEN OLD.\"node_id\" = 'B' BEGIN SELECT RAISE(IGNORE); END"
        )
        con.commit()
    finally:
        con.close()
    t1 = tmp_path / "trg1" / "nodes.csv"
    t2 = tmp_path / "trg2" / "nodes.csv"
    t1.parent.mkdir()
    t2.parent.mkdir()
    t1.write_text("node_id,val\nA,v1\nC,v1\n", encoding="utf-8")
    t2.write_text("node_id,val\nB,new\nA,v2\n", encoding="utf-8")
    load_tables([t1], db_path=trg_db, engine="sqlite")
    _, tr2 = load_tables([t2], db_path=trg_db, engine="sqlite")

    with pytest.raises(InputError, match="삭제 후에도 남아"):
        rollback_batch(trg_db, tr2.batch_id, engine="sqlite")
    # 트랜잭션 전체가 롤백되어 A도 v2 그대로, 배치 상태도 succeeded 그대로여야 한다.
    con = sqlite3.connect(str(trg_db))
    try:
        after_fail = con.execute(
            'SELECT "node_id", "val" FROM "nodes" ORDER BY "node_id"'
        ).fetchall()
        st = con.execute(
            'SELECT "status" FROM "_etl_load_batches" WHERE "batch_id" = ?',
            (tr2.batch_id,),
        ).fetchone()
    finally:
        con.close()
    assert after_fail == [("A", "v2"), ("B", "new"), ("C", "v1")]
    assert st == ("succeeded",)
