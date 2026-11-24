"""사용자 확정 스키마 오버라이드(마지막 10%) 및 실제 DB 적재·관리 테이블 회귀 테스트."""

from __future__ import annotations

import json
import sqlite3
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

    # 3차: 스키마에 신규 컬럼 추가 후 적재 -> 스키마 드리프트 감지 + 컬럼 자동 진화
    branches_v2 = tmp_path / "branches.csv"
    branches_v2.write_text(
        "지점코드,지점명,국가\nKR,서울본점,대한민국\nUS,뉴욕지사,미국\nJP,도쿄지사,일본\nSG,싱가포르,싱가포르\n",
        encoding="utf-8",
    )
    _b3, res3 = load_tables([branches_v2], db_path=db_file, engine="sqlite")
    assert res3.status == "succeeded"
    assert res3.tables[0].schema_drift_detected is True
    assert res3.tables[0].rows_loaded == 4

    # 4차: orders가 참조 중인 부모 배치(res3)를 먼저 롤백하려 하면 FK 무결성 보호 InputError 발생
    with pytest.raises(InputError, match="참조하고 있어"):
        rollback_batch(db_file, res3.batch_id, engine="sqlite")

    # 5차: 자식 포함 1차 배치 먼저 역순 롤백(orders -> customers -> branches) 후 3차 롤백
    rb1 = rollback_batch(db_file, res1.batch_id, engine="sqlite")
    assert rb1["status"] == "rolled_back"
    assert next(iter(rb1["deleted_by_table"].keys())) == "orders"  # 자식(orders) 먼저 삭제!
    assert rb1["deleted_by_table"]["orders"] == 4

    rb3 = rollback_batch(db_file, res3.batch_id, engine="sqlite")
    assert rb3["status"] == "rolled_back"
    assert rb3["deleted_by_table"]["branches"] == 4

    # 6차: 롤백 완료 후 동일 파일을 다시 넣으면 force 없이도 재적재 가능
    _b4, res4 = load_tables([branches, customers, orders], db_path=db_file, engine="sqlite")
    assert res4.status == "succeeded"
    assert res4.total_rows_loaded == 10


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
