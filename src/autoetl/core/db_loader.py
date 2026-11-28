"""결정론적 실제 DB 적재 엔진(SQLite · DuckDB · 선택적 PostgreSQL) 및 기본 데이터 관리 카탈로그.

핵심 기능:
1. 0개 추가 코어 의존성: Python 표준 ``sqlite3`` 및 코어 의존성 ``duckdb``로 즉시 DB 파일을
   생성·적재하며, ``postgres`` 엔진은 ``psycopg``가 설치되어 있을 때 지연 임포트로 지원한다.
2. 디폴트 데이터 관리·리니지 테이블 4종(``_etl_*``) 자동 생성 및 기록:
   - ``_etl_load_batches``: 적재 실행(배치) 단위 시점·상태·사용자 확정 오버라이드 스냅샷
   - ``_etl_datasets``: 넣은 데이터 고유 ID(``dataset_id``)·데이터 종류(``dataset_kind``)·
     원본 해시(``source_sha256``)·스키마 지문(``schema_fingerprint``)·적재 건수
   - ``_etl_column_registry``: 원본 한글명 ↔ DB 영문명·역할·타입·PK/UNIQUE/FK·변환식·
     개인정보(PII) 등급 및 적용 마스킹 정책 사전
   - ``_etl_quarantine_rows``: 변환 오류·NULL 위반·PK/UNIQUE 중복·고아 FK 행 격리 보관함
3. 행 단위 리니지 컬럼(``_etl_batch_id``, ``_etl_dataset_id``, ``_etl_loaded_at``) 자동 부여,
   동일 파일 중복 적재 방지, 스키마 변경(드리프트) 감지, 배치 롤백(``rollback_batch``),
   카탈로그 조회(``inspect_db_catalog``).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import re
import sqlite3
import time
import uuid
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from datetime import time as dt_time
from decimal import ROUND_HALF_UP, Context, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import duckdb

from autoetl.adapters.detectors.builtin import EXCEL_SERIAL_MAX, EXCEL_SERIAL_MIN, parse_date_text
from autoetl.contracts import (
    ColumnClassification,
    ColumnMapping,
    DatasetKind,
    DbLoadResult,
    IncrementMode,
    MaskingPolicy,
    MultiTableAnalysisBundle,
    MultiTableRelationAnalysis,
    OrphanPolicy,
    PhysicalType,
    Purpose,
    SchemaCandidate,
    SchemaOverrideSpec,
    SensitiveType,
    SensitivityLevel,
    SensitivityMap,
    TableLoadOutcome,
    TableLoadStrategy,
    TableProfileBundle,
    TargetColumn,
    TargetTable,
)
from autoetl.core.context import RAW_TABLE, quote_identifier
from autoetl.core.ddl.postgres import INT4_MAX, INT4_MIN
from autoetl.core.errors import InputError
from autoetl.core.load_planner import _plan_multi_table_load
from autoetl.core.multi_context import (
    MultiTableContext,
    NamedTableInput,
    TableSlotContext,
    profile_all_tables,
)
from autoetl.core.relations_multi import MIN_INCLUSION_RATE, analyze_multi_table_relations
from autoetl.core.stages.s02_profile import DEFAULT_MISSING_MARKERS
from autoetl.core.stages.s03_types import CURRENCY_DECIMAL_SHAPE
from autoetl.core.stages.s07_sensitivity import detect_table_sensitivity
from autoetl.security.masking_key import MaskingKey, get_or_create_masking_key, read_masking_key

DEFAULT_MANAGEMENT_TABLES: tuple[str, ...] = (
    "_etl_load_batches",
    "_etl_datasets",
    "_etl_column_registry",
    "_etl_quarantine_rows",
)
_MASKING_KEY_TABLE = "_etl_masking_keys"
_MASKING_SCHEME = "hmac-sha256-24"
_MASKING_KEY_ID_RE = re.compile(r"k_[0-9a-f]{12}\Z")

AUDIT_COLUMNS: tuple[str, ...] = (
    "_etl_batch_id",
    "_etl_dataset_id",
    "_etl_loaded_at",
)

_CURRENCY_SHAPE_RE = re.compile(CURRENCY_DECIMAL_SHAPE)
_CURRENCY_STRIP_RE = re.compile(r"[₩$원,\s]")
_INT_RE = re.compile(r"^[+-]?\d+$")
_VARCHAR_LEN_RE = re.compile(r"^varchar\((\d+)\)$", re.IGNORECASE)
_DECIMAL_PS_RE = re.compile(r"^(?:numeric|decimal)\((\d+),\s*(-?\d+)\)$", re.IGNORECASE)
_KOREAN_DATE_RE = re.compile(r"^(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일$")
_YEAR_RE = re.compile(r"(19\d{2}|20\d{2})")
_PHONE_PARTS_RE = re.compile(
    r"^(\+?\d{2,4}(?=[-.\s])|02|050\d|0(?!2|50)\d{2})([-.\s]?)(\d{3,4})([-.\s]?)(\d{4})$"
)
_RRN_SPLIT_RE = re.compile(r"^(\d{6})(-?)([1-8])(\d{6})$")
_BRN_SPLIT_RE = re.compile(r"^(\d{3})-(\d{2})-(\d{5})$")

_TRUE_TOKENS = frozenset({"y", "yes", "예", "o", "true"})
_FALSE_TOKENS = frozenset({"n", "no", "아니오", "x", "false"})

_DATASET_KIND_LABELS_KO: dict[DatasetKind, str] = {
    DatasetKind.MASTER_REFERENCE: "기준·마스터 데이터",
    DatasetKind.TRANSACTION_EVENT: "거래·이벤트 데이터",
    DatasetKind.RELATION_FACT: "관계·연결 데이터",
    DatasetKind.LOG_APPEND: "이력·로그 데이터",
    DatasetKind.STANDALONE: "단독 데이터",
}


def _qi(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _normalize_engine(engine: str) -> str:
    low = engine.strip().lower()
    if low == "postgresql":
        return "postgres"
    if low in {"sqlite", "duckdb", "postgres"}:
        return low
    raise InputError(f"지원하지 않는 DB 엔진입니다: '{engine}' (지원: sqlite, duckdb, postgres)")


def _sha256_path(path: Path) -> str | None:
    if not path.is_file():
        return None
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(65536):
            h.update(chunk)
    return h.hexdigest()


def mask_value(
    val: str,
    stype: SensitiveType | None,
    policy: MaskingPolicy,
    *,
    hash_key: bytes | None = None,
) -> str:
    """정책에 따라 민감 문자열을 변환한다. ``hash``에는 ``hash_key``가 필요하다."""
    if policy is MaskingPolicy.NONE:
        return val
    if policy is MaskingPolicy.HASH:
        if hash_key is None:
            raise ValueError("hash 정책에는 비밀 키가 필요합니다.")
        return "h_" + hmac.digest(hash_key, val.encode("utf-8"), "sha256").hex()[:24]

    s = val.strip()
    if not s:
        return s

    if stype is SensitiveType.PHONE_NUMBER:
        m = _PHONE_PARTS_RE.fullmatch(s)
        if m:
            sep1 = m.group(2) or "-"
            sep2 = m.group(4) or "-"
            return f"{m.group(1)}{sep1}{'*' * len(m.group(3))}{sep2}{m.group(5)}"
        digits = "".join(ch for ch in s if ch.isdigit())
        if len(digits) >= 7:
            return f"****-{digits[-4:]}"

    if stype is SensitiveType.EMAIL and "@" in s:
        local, domain = s.split("@", 1)
        keep = 2 if len(local) > 2 else 1
        return f"{local[:keep]}***@{domain}"

    if stype in {
        SensitiveType.RESIDENT_REGISTRATION_NUMBER,
        SensitiveType.FOREIGNER_REGISTRATION_NUMBER,
    }:
        m_rrn = _RRN_SPLIT_RE.fullmatch(s)
        if m_rrn:
            sep = m_rrn.group(2) or "-"
            return f"{m_rrn.group(1)}{sep}{m_rrn.group(3)}******"
        if len(s) >= 7:
            return s[:7] + "*" * max(6, len(s) - 7)

    if stype is SensitiveType.CARD_NUMBER:
        digits = "".join(ch for ch in s if ch.isdigit())
        if len(digits) >= 12:
            return f"{digits[:4]}-****-****-{digits[-4:]}"

    if stype is SensitiveType.BANK_ACCOUNT:
        digits = "".join(ch for ch in s if ch.isdigit())
        if len(digits) >= 7:
            return f"{digits[:3]}-***-***{digits[-3:]}"

    if stype is SensitiveType.DATE_OF_BIRTH:
        m_yr = _YEAR_RE.search(s)
        return f"{m_yr.group(1)}-**-**" if m_yr else "****-**-**"

    if stype is SensitiveType.NAME:
        if len(s) == 1:
            return "*"
        if len(s) == 2:
            return f"{s[0]}*"
        return f"{s[0]}{'*' * (len(s) - 2)}{s[-1]}"

    if stype is SensitiveType.ADDRESS:
        tokens = s.split()
        if len(tokens) >= 3:
            return f"{tokens[0]} {tokens[1]} ***"
        half = max(1, len(s) // 2)
        return s[:half] + "***"

    if stype is SensitiveType.IP_ADDRESS:
        parts = s.split(".")
        if len(parts) == 4:
            return f"{parts[0]}.{parts[1]}.{parts[2]}.***"

    if stype is SensitiveType.BUSINESS_REGISTRATION_NUMBER:
        m_brn = _BRN_SPLIT_RE.fullmatch(s)
        if m_brn:
            return f"{m_brn.group(1)}-**-***{m_brn.group(3)[-2:]}"

    if len(s) <= 2:
        return "*" * len(s)
    if len(s) <= 4:
        return f"{s[0]}{'*' * (len(s) - 2)}{s[-1]}"
    return f"{s[:2]}{'*' * min(len(s) - 4, 6)}{s[-2:]}"


def classify_dataset_kind(
    table_name: str,
    candidate: SchemaCandidate,
    relations: MultiTableRelationAnalysis,
    table_plan: TableLoadStrategy,
) -> DatasetKind:
    """테이블의 PK/FK/시계열 구조로부터 데이터 종류(DatasetKind)를 결정론적으로 분류한다."""
    if table_plan.uses_surrogate_key:
        return DatasetKind.LOG_APPEND

    referenced_parents: set[str] = set()
    has_outgoing_fk = False
    for tbl in candidate.tables:
        for col in tbl.columns:
            if col.foreign_key is None:
                continue
            parent = col.foreign_key.split("(", 1)[0]
            referenced_parents.add(parent)
            if tbl.name == table_name:
                has_outgoing_fk = True

    if table_name in referenced_parents:
        return DatasetKind.MASTER_REFERENCE
    if has_outgoing_fk and table_plan.watermark_column is not None:
        return DatasetKind.TRANSACTION_EVENT
    if has_outgoing_fk:
        return DatasetKind.RELATION_FACT

    # 단독 테이블이라도 PK 참조 이력·시계열 컬럼이 있으면 참고
    pk_rec = relations.primary_keys.get(table_plan.source_table)
    if pk_rec is not None and pk_rec.referenced_by_count > 0:
        return DatasetKind.MASTER_REFERENCE
    return DatasetKind.STANDALONE


def compute_schema_fingerprint(table_plan: TableLoadStrategy) -> str:
    """테이블 스키마(대상 컬럼·타입·NULL 정책)의 결정론적 16자리 SHA-256 지문을 반환한다."""
    payload = [
        [m.target_column, m.type_cast, m.null_handling.value] for m in table_plan.column_mappings
    ]
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def render_management_tables_ddl(dialect: str = "postgres") -> str:
    """디폴트 데이터 관리 테이블 4종(``_etl_*``)의 DDL을 반환한다."""
    eng = _normalize_engine(dialect)
    txt = "VARCHAR" if eng == "duckdb" else "TEXT"
    int_t = "BIGINT" if eng in {"duckdb", "postgres"} else "INTEGER"
    if eng == "sqlite":
        q_id_col = '"quarantine_id" INTEGER PRIMARY KEY AUTOINCREMENT'
    elif eng == "postgres":
        q_id_col = '"quarantine_id" BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY'
    else:
        q_id_col = '"quarantine_id" BIGINT PRIMARY KEY'

    stmts = [
        (
            'CREATE TABLE IF NOT EXISTS "_etl_load_batches" (\n'
            f'    "batch_id" {txt} PRIMARY KEY,\n'
            f'    "loaded_at" {txt} NOT NULL,\n'
            f'    "purpose" {txt} NOT NULL,\n'
            f'    "selected_candidate_id" {txt} NOT NULL,\n'
            f'    "db_engine" {txt} NOT NULL,\n'
            f'    "status" {txt} NOT NULL,\n'
            f'    "table_count" {int_t} NOT NULL,\n'
            f'    "total_rows_read" {int_t} NOT NULL,\n'
            f'    "total_rows_loaded" {int_t} NOT NULL,\n'
            f'    "total_rows_quarantined" {int_t} NOT NULL,\n'
            f'    "overrides_json" {txt} NOT NULL,\n'
            f'    "notes" {txt} NOT NULL\n'
            ")"
        ),
        (
            'CREATE TABLE IF NOT EXISTS "_etl_datasets" (\n'
            f'    "dataset_id" {txt} PRIMARY KEY,\n'
            f'    "batch_id" {txt} NOT NULL REFERENCES "_etl_load_batches" ("batch_id"),\n'
            f'    "load_order_index" {int_t} NOT NULL,\n'
            f'    "target_table" {txt} NOT NULL,\n'
            f'    "dataset_kind" {txt} NOT NULL,\n'
            f'    "source_name" {txt} NOT NULL,\n'
            f'    "source_format" {txt} NOT NULL,\n'
            f'    "source_encoding" {txt} NOT NULL,\n'
            f'    "source_sha256" {txt},\n'
            f'    "schema_fingerprint" {txt} NOT NULL,\n'
            f'    "increment_mode" {txt} NOT NULL,\n'
            f'    "primary_key_columns" {txt} NOT NULL,\n'
            f'    "rows_read" {int_t} NOT NULL,\n'
            f'    "rows_loaded" {int_t} NOT NULL,\n'
            f'    "rows_quarantined" {int_t} NOT NULL,\n'
            f'    "schema_drift_detected" {int_t} NOT NULL,\n'
            f'    "loaded_at" {txt} NOT NULL,\n'
            f'    "rows_updated" {int_t} NOT NULL DEFAULT 0,\n'
            f'    "rollback_snapshots_json" {txt},\n'
            f'    "snapshot_source_batches_json" {txt}\n'
            ")"
        ),
        (
            'CREATE TABLE IF NOT EXISTS "_etl_column_registry" (\n'
            f'    "dataset_id" {txt} NOT NULL REFERENCES "_etl_datasets" ("dataset_id"),\n'
            f'    "target_table" {txt} NOT NULL,\n'
            f'    "column_order" {int_t} NOT NULL,\n'
            f'    "source_column" {txt} NOT NULL,\n'
            f'    "target_column" {txt} NOT NULL,\n'
            f'    "data_role" {txt} NOT NULL,\n'
            f'    "db_type" {txt} NOT NULL,\n'
            f'    "is_primary_key" {int_t} NOT NULL,\n'
            f'    "is_unique" {int_t} NOT NULL,\n'
            f'    "foreign_key_ref" {txt},\n'
            f'    "transform_rule" {txt} NOT NULL,\n'
            f'    "pii_level" {txt} NOT NULL,\n'
            f'    "pii_types" {txt} NOT NULL,\n'
            f'    "masking_policy" {txt} NOT NULL,\n'
            '    PRIMARY KEY ("dataset_id", "target_column")\n'
            ")"
        ),
        (
            'CREATE TABLE IF NOT EXISTS "_etl_quarantine_rows" (\n'
            f"    {q_id_col},\n"
            f'    "batch_id" {txt} NOT NULL,\n'
            f'    "dataset_id" {txt} NOT NULL,\n'
            f'    "target_table" {txt} NOT NULL,\n'
            f'    "source_row_number" {int_t} NOT NULL,\n'
            f'    "error_category" {txt} NOT NULL,\n'
            f'    "error_column" {txt} NOT NULL,\n'
            f'    "error_detail" {txt} NOT NULL,\n'
            f'    "raw_row_json" {txt} NOT NULL,\n'
            f'    "quarantined_at" {txt} NOT NULL\n'
            ")"
        ),
    ]
    return ";\n\n".join(stmts) + ";\n"


class _DbAdapter:
    """SQLite · DuckDB · 선택적 PostgreSQL(psycopg) 공통 경량 DB 어댑터."""

    def __init__(self, target: str | Path, engine: str, *, read_only: bool = False) -> None:
        self.engine = _normalize_engine(engine)
        self.target_str = str(target)
        self.placeholder = "%s" if self.engine == "postgres" else "?"
        self._conn: Any = None
        self._in_tx = False

        if self.engine == "sqlite":
            db_p = Path(target)
            if read_only:
                conn = sqlite3.connect(
                    f"{db_p.absolute().as_uri()}?mode=ro", uri=True, isolation_level=None
                )
            else:
                if self.target_str != ":memory:":
                    db_p.parent.mkdir(parents=True, exist_ok=True)
                conn = sqlite3.connect(str(db_p), isolation_level=None)
            conn.execute("PRAGMA foreign_keys = ON")
            self._conn = conn
        elif self.engine == "duckdb":
            db_p = Path(target)
            if not read_only and self.target_str != ":memory:":
                db_p.parent.mkdir(parents=True, exist_ok=True)
            self._conn = duckdb.connect(str(db_p), read_only=read_only)
        else:
            try:
                import psycopg  # type: ignore[import-not-found]
            except ImportError as exc:
                raise InputError(
                    "PostgreSQL 직접 적재를 사용하려면 'psycopg' 패키지가 필요합니다"
                    "(예: uv pip install 'psycopg[binary]'). "
                    "추가 설치 없이 저장하려면 --engine sqlite 또는 duckdb를 선택하세요."
                ) from exc
            self._conn = psycopg.connect(self.target_str, autocommit=False)
            if read_only:
                self._conn.read_only = True

    def begin(self) -> None:
        if self._in_tx:
            return
        if self.engine == "sqlite":
            self._conn.execute("BEGIN")
        elif self.engine == "duckdb":
            self._conn.execute("BEGIN TRANSACTION")
        self._in_tx = True

    def commit(self) -> None:
        if not self._in_tx:
            return
        if self.engine in {"sqlite", "duckdb"}:
            self._conn.execute("COMMIT")
        else:
            self._conn.commit()
        self._in_tx = False

    def rollback(self) -> None:
        if not self._in_tx:
            return
        try:
            if self.engine in {"sqlite", "duckdb"}:
                self._conn.execute("ROLLBACK")
            else:
                self._conn.rollback()
        finally:
            self._in_tx = False

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Any:
        if params:
            return self._conn.execute(sql, list(params))
        return self._conn.execute(sql)

    def executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> None:
        if not rows:
            return
        if self.engine == "postgres":
            with self._conn.cursor() as cur:
                cur.executemany(sql, rows)
            return
        self._conn.executemany(sql, rows)

    def execute_updates_checked(self, sql: str, rows: Sequence[Sequence[Any]]) -> int:
        """UPDATE 묶음을 실행하고 실제로 갱신된 총 행 수를 반환한다."""
        if not rows:
            return 0
        if self.engine == "sqlite":
            cur = self._conn.executemany(sql, rows)
            return max(0, int(cur.rowcount))
        if self.engine == "duckdb":
            total = 0
            for row in rows:
                res = self._conn.execute(sql, list(row)).fetchone()
                total += int(res[0]) if res is not None and res[0] is not None else 0
            return total
        if _pg_supports_batched_rowcount():
            with self._conn.cursor() as cur:
                cur.executemany(sql, rows)
                rc = cur.rowcount
                if rc is not None and rc >= 0:
                    return int(rc)
                raise InputError(
                    "PostgreSQL executemany가 갱신 행 수를 보고하지 않아 복원을 검증할 수 없습니다."
                )
        total = 0
        with self._conn.cursor() as cur:
            for row in rows:
                cur.execute(sql, row)
                if cur.rowcount is not None and cur.rowcount > 0:
                    total += int(cur.rowcount)
        return total

    def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        cur = self.execute(sql, params)
        rows = cur.fetchall()
        return [tuple(r) for r in rows]

    def fetchone(self, sql: str, params: Sequence[Any] = ()) -> tuple[Any, ...] | None:
        cur = self.execute(sql, params)
        row = cur.fetchone()
        return tuple(row) if row is not None else None

    def table_exists(self, table_name: str) -> bool:
        p = self.placeholder
        if self.engine == "sqlite":
            row = self.fetchone(
                f"SELECT 1 FROM sqlite_master WHERE type='table' AND name={p}",
                (table_name,),
            )
            return row is not None
        if self.engine == "duckdb":
            row = self.fetchone(
                f"SELECT 1 FROM information_schema.tables "
                f"WHERE table_schema='main' AND table_name={p}",
                (table_name,),
            )
            return row is not None
        row = self.fetchone(
            f"SELECT 1 FROM information_schema.tables "
            f"WHERE table_schema=current_schema() AND table_name={p}",
            (table_name,),
        )
        return row is not None

    def existing_columns(self, table_name: str) -> list[str]:
        if not self.table_exists(table_name):
            return []
        if self.engine == "sqlite":
            rows = self.fetchall(f"PRAGMA table_info({_qi(table_name)})")
            return [str(r[1]) for r in rows]
        p = self.placeholder
        schema_clause = (
            "table_schema='main'" if self.engine == "duckdb" else "table_schema=current_schema()"
        )
        rows = self.fetchall(
            f"SELECT column_name FROM information_schema.columns "
            f"WHERE {schema_clause} AND table_name={p} ORDER BY ordinal_position",
            (table_name,),
        )
        return [str(r[0]) for r in rows]

    def list_user_tables(self) -> list[str]:
        if self.engine == "sqlite":
            rows = self.fetchall(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
            return [str(r[0]) for r in rows]
        schema_clause = (
            "table_schema='main'" if self.engine == "duckdb" else "table_schema=current_schema()"
        )
        rows = self.fetchall(
            f"SELECT table_name FROM information_schema.tables "
            f"WHERE {schema_clause} AND table_type='BASE TABLE' ORDER BY table_name"
        )
        return [str(r[0]) for r in rows]

    def sqlite_single_identity_pk(self, table_name: str, col_name: str) -> bool:
        """SQLite 실제 테이블이 일반 rowid 별칭(단일 INTEGER PK 자동 할당)인지 확인한다."""
        if self.engine != "sqlite" or not self.table_exists(table_name):
            return False
        ddl_row = self.fetchone(
            f"SELECT sql FROM sqlite_master WHERE type='table' AND name={self.placeholder}",
            (table_name,),
        )
        if (
            ddl_row is not None
            and ddl_row[0] is not None
            and re.search(r"\bWITHOUT\s+ROWID\b", str(ddl_row[0]), re.IGNORECASE)
        ):
            return False
        # 별도 PK 인덱스(origin == 'pk')가 있으면 INTEGER PRIMARY KEY DESC 등 비-rowid 별칭이다.
        idx_rows = self.fetchall(f"PRAGMA index_list({_qi(table_name)})")
        if any(len(r) >= 4 and str(r[3]) == "pk" for r in idx_rows):
            return False
        rows = self.fetchall(f"PRAGMA table_info({_qi(table_name)})")
        pk_entries = [(str(r[1]), str(r[2]).strip().upper()) for r in rows if int(r[5]) > 0]
        return len(pk_entries) == 1 and pk_entries[0] == (col_name, "INTEGER")

    def writable_columns(self, table_name: str) -> list[str]:
        """DB 계산(GENERATED) 컬럼을 제외한 쓰기 가능 컬럼 목록을 반환한다."""
        if not self.table_exists(table_name):
            return []
        if self.engine == "sqlite":
            rows = self.fetchall(f"PRAGMA table_xinfo({_qi(table_name)})")
            # hidden: 0=일반, 1=숨김(rowid 등), 2=VIRTUAL GENERATED, 3=STORED GENERATED
            return [str(r[1]) for r in rows if int(r[6]) == 0]
        if self.engine == "postgres":
            p = self.placeholder
            rows = self.fetchall(
                f"SELECT column_name FROM information_schema.columns "
                f"WHERE table_schema=current_schema() AND table_name={p} "
                f"AND COALESCE(is_generated, 'NEVER') = 'NEVER' "
                f"ORDER BY ordinal_position",
                (table_name,),
            )
            return [str(r[0]) for r in rows]
        return self.existing_columns(table_name)

    def existing_column_constraints(self, table_name: str) -> tuple[set[str], dict[str, str]]:
        """기존 테이블의 물리 NOT NULL과 범위가 정해진 수치 타입을 반환한다."""
        if not self.table_exists(table_name):
            return set(), {}
        if self.engine == "sqlite":
            rows = self.fetchall(f"PRAGMA table_info({_qi(table_name)})")
            return {str(r[1]) for r in rows if int(r[3]) != 0 or int(r[5]) > 0}, {}
        p = self.placeholder
        schema_clause = (
            "table_schema='main'" if self.engine == "duckdb" else "table_schema=current_schema()"
        )
        rows = self.fetchall(
            f"SELECT column_name, is_nullable, data_type, "
            f"numeric_precision, numeric_precision_radix, numeric_scale "
            f"FROM information_schema.columns WHERE {schema_clause} AND table_name={p}",
            (table_name,),
        )
        not_null: set[str] = set()
        numeric_types: dict[str, str] = {}
        for name, nullable, dtype, precision, radix, scale in rows:
            if nullable == "NO":
                not_null.add(str(name))
            if str(dtype).lower() == "integer":
                numeric_types[str(name)] = "integer"
            elif radix == 10 and precision is not None and scale is not None:
                numeric_types[str(name)] = f"numeric({precision}, {scale})"
        return not_null, numeric_types

    def column_data_types(self, table_name: str) -> dict[str, str]:
        """테이블 각 컬럼의 소문자 DB 타입명(PostgreSQL 배열 원소 타입 포함)을 반환한다."""
        if not self.table_exists(table_name):
            return {}
        if self.engine == "sqlite":
            rows = self.fetchall(f"PRAGMA table_info({_qi(table_name)})")
            return {str(r[1]): str(r[2]).strip().lower() for r in rows}
        p = self.placeholder
        if self.engine == "postgres":
            rows = self.fetchall(
                f"SELECT column_name, data_type, udt_name, udt_schema "
                f"FROM information_schema.columns "
                f"WHERE table_schema=current_schema() AND table_name={p}",
                (table_name,),
            )
            types_out: dict[str, str] = {}
            for col_name, dtype_raw, udt_raw, udt_schema_raw in rows:
                dtype = str(dtype_raw).strip().lower()
                udt = str(udt_raw or "").strip().lower()
                udt_schema = str(udt_schema_raw or "").strip().lower()
                if dtype == "array" and udt.startswith("_"):
                    if udt_schema == "pg_catalog":
                        types_out[str(col_name)] = f"{udt[1:]}[]"
                    else:
                        types_out[str(col_name)] = f"{udt_schema}.{udt[1:]}[]"
                elif udt_schema and udt_schema != "pg_catalog":
                    types_out[str(col_name)] = f"{udt_schema}.{udt or dtype}"
                else:
                    types_out[str(col_name)] = dtype
            return types_out
        rows = self.fetchall(
            f"SELECT column_name, data_type FROM information_schema.columns "
            f"WHERE table_schema='main' AND table_name={p}",
            (table_name,),
        )
        return {str(r[0]): str(r[1]).strip().lower() for r in rows}

    def has_self_referencing_fk(self, table_name: str) -> bool:
        """테이블이 자기 자신을 참조하는 외래 키(SELF-FK)를 가지는지 확인한다."""
        if not self.table_exists(table_name):
            return False
        if self.engine == "sqlite":
            rows = self.fetchall(f"PRAGMA foreign_key_list({_qi(table_name)})")
            low_tbl = table_name.lower()
            return any(len(r) >= 3 and str(r[2]).lower() == low_tbl for r in rows)
        if self.engine == "postgres":
            row = self.fetchone(
                "SELECT 1 FROM pg_constraint c "
                "JOIN pg_class t ON c.conrelid = t.oid "
                "JOIN pg_namespace n ON t.relnamespace = n.oid "
                "WHERE c.contype = 'f' AND c.conrelid = c.confrelid "
                "AND n.nspname = current_schema() AND t.relname = %s LIMIT 1",
                (table_name,),
            )
            return row is not None
        return False

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


def _database_masking_key_id(adapter: _DbAdapter, *, reject_legacy: bool = False) -> str | None:
    if adapter.table_exists(_MASKING_KEY_TABLE):
        rows = adapter.fetchall('SELECT "key_id", "scheme" FROM "_etl_masking_keys" LIMIT 2')
        if rows:
            key_id, scheme = rows[0]
            if (
                len(rows) != 1
                or not isinstance(key_id, str)
                or _MASKING_KEY_ID_RE.fullmatch(key_id) is None
                or scheme != _MASKING_SCHEME
            ):
                raise InputError("DB의 가리기 키 기록이 올바르지 않습니다.")
            return key_id
    if reject_legacy and adapter.table_exists("_etl_column_registry"):
        legacy = adapter.fetchone(
            'SELECT 1 FROM "_etl_column_registry" WHERE "masking_policy" = \'hash\' LIMIT 1'
        )
        if legacy is not None:
            raise InputError(
                "예전 방식으로 만든 해시가 DB에 있어 새 방식의 해시를 섞을 수 없습니다. "
                "새 DB에 다시 적재해 주세요."
            )
    return None


def masking_key_id_for_database(db_path: str | Path) -> str | None:
    """기존 SQLite/DuckDB 파일의 키 이름표를 읽기 전용으로 확인한다."""
    path = Path(db_path)
    try:
        with path.open("rb") as stream:
            header = stream.read(16)
        engine = "sqlite" if header in {b"SQLite format 3\x00", b""} else "duckdb"
        adapter = _DbAdapter(path, engine, read_only=True)
        try:
            return _database_masking_key_id(adapter)
        finally:
            adapter.close()
    except FileNotFoundError as exc:
        raise InputError("지정한 DB 파일을 찾을 수 없습니다.") from exc
    except (OSError, sqlite3.Error, duckdb.Error) as exc:
        raise InputError("지정한 DB 파일의 가리기 키 기록을 읽을 수 없습니다.") from exc


def _require_matching_masking_key(database_id: str, local_id: str | None) -> None:
    if database_id != local_id:
        local = local_id or "없음"
        raise InputError(
            f"DB의 가리기 키는 {database_id}이고 이 컴퓨터의 키는 {local}입니다. "
            "백업한 키를 `autoetl key import <파일>`로 가져온 뒤 다시 시도해 주세요."
        )


def _prepare_masking_key(db_path: str | Path, engine: str) -> tuple[MaskingKey, bool]:
    database_id = None
    if engine == "postgres" or (str(db_path) != ":memory:" and Path(db_path).exists()):
        adapter = _DbAdapter(db_path, engine, read_only=True)
        try:
            database_id = _database_masking_key_id(adapter, reject_legacy=True)
        finally:
            adapter.close()

    if database_id is None:
        return get_or_create_masking_key()
    key = read_masking_key()
    _require_matching_masking_key(database_id, key.key_id if key else None)
    assert key is not None
    return key, False


def _register_masking_key(
    adapter: _DbAdapter, key_id: str, batch_id: str, registered_at: str
) -> None:
    """적재 트랜잭션 안에서 기록을 재확인하고 첫 키를 고정한다."""
    database_id = _database_masking_key_id(adapter, reject_legacy=True)
    if database_id is not None:
        _require_matching_masking_key(database_id, key_id)
        return

    adapter.execute(
        'CREATE TABLE IF NOT EXISTS "_etl_masking_keys" ('
        '"key_id" TEXT PRIMARY KEY, '
        f'"scheme" TEXT NOT NULL UNIQUE CHECK ("scheme" = \'{_MASKING_SCHEME}\'), '
        '"first_batch_id" TEXT NOT NULL, "registered_at" TEXT NOT NULL)'
    )
    p = adapter.placeholder
    adapter.execute(
        'INSERT INTO "_etl_masking_keys" ("key_id", "scheme", "first_batch_id", "registered_at") '
        f'VALUES ({p}, {p}, {p}, {p}) ON CONFLICT ("scheme") DO NOTHING',
        (key_id, _MASKING_SCHEME, batch_id, registered_at),
    )
    database_id = _database_masking_key_id(adapter)
    if database_id is None:
        raise InputError("DB의 가리기 키 등록을 확인할 수 없습니다.")
    _require_matching_masking_key(database_id, key_id)


def _dialect_col_type(
    col: TargetColumn,
    mapping: ColumnMapping,
    engine: str,
) -> str:
    if engine == "sqlite":
        if col.physical_type is PhysicalType.INTEGER:
            return "INTEGER"
        if col.physical_type in {PhysicalType.DECIMAL, PhysicalType.FLOAT}:
            return "REAL"
        if col.physical_type is PhysicalType.BOOLEAN:
            return "INTEGER"
        if col.physical_type is PhysicalType.BINARY:
            return "BLOB"
        return "TEXT"

    if engine == "duckdb":
        if col.physical_type is PhysicalType.INTEGER:
            return "BIGINT" if col.is_identity or mapping.type_cast == "bigint" else "INTEGER"
        if col.physical_type is PhysicalType.DECIMAL:
            m_dec = _DECIMAL_PS_RE.fullmatch(mapping.type_cast.strip())
            if m_dec is not None:
                return f"DECIMAL({int(m_dec.group(1))}, {int(m_dec.group(2))})"
            if col.precision is not None and col.scale is not None:
                return f"DECIMAL({col.precision}, {col.scale})"
            return "DOUBLE"
        if col.physical_type is PhysicalType.FLOAT:
            return "DOUBLE"
        if col.physical_type is PhysicalType.BOOLEAN:
            return "BOOLEAN"
        if col.physical_type is PhysicalType.DATE:
            return "DATE"
        if col.physical_type is PhysicalType.TIME:
            return "TIME"
        if col.physical_type is PhysicalType.TIMESTAMP:
            return "TIMESTAMP"
        if col.physical_type is PhysicalType.TIMESTAMPTZ:
            return "TIMESTAMPTZ"
        if col.physical_type is PhysicalType.BINARY:
            return "BLOB"
        return "VARCHAR"

    # postgres
    if col.is_identity:
        return "BIGINT GENERATED ALWAYS AS IDENTITY"
    return mapping.type_cast.upper()


def _uses_db_autoincrement(target_table: TargetTable, engine: str) -> bool:
    """DDL이 DB 엔진 측 자동 증가 IDENTITY 컬럼을 생성하는지 반환한다.

    - PostgreSQL: 단일·복합 PK 모두 ``GENERATED ALWAYS AS IDENTITY``를 지원한다.
    - SQLite: 단일 컬럼 PK일 때만 ``INTEGER PRIMARY KEY AUTOINCREMENT``를 생성한다.
    - DuckDB: 명시적 시퀀스 값(``MAX + 1``)을 할당한다.
    """
    if engine == "postgres":
        return any(c.is_identity for c in target_table.columns)
    if engine == "sqlite":
        pk_cols = [c.name for c in target_table.columns if c.is_primary_key]
        return len(pk_cols) == 1 and any(
            c.name == pk_cols[0] and c.is_identity for c in target_table.columns
        )
    return False


def _build_business_table_ddl(
    target_table: TargetTable,
    table_plan: TableLoadStrategy,
    parent_pk_counts: Mapping[str, int],
    *,
    engine: str,
    include_audit_columns: bool,
) -> str:
    map_by_target = {m.target_column: m for m in table_plan.column_mappings}
    pk_cols = [c.name for c in target_table.columns if c.is_primary_key]
    inline_sqlite_surr = engine == "sqlite" and _uses_db_autoincrement(target_table, engine)

    col_defs: list[str] = []
    for col in target_table.columns:
        mapping = map_by_target[col.name]
        dtype = _dialect_col_type(col, mapping, engine)
        if inline_sqlite_surr and col.is_identity:
            col_defs.append(f"{_qi(col.name)} INTEGER PRIMARY KEY AUTOINCREMENT")
            continue
        null_sql = "" if col.nullable else " NOT NULL"
        col_defs.append(f"{_qi(col.name)} {dtype}{null_sql}")

    if include_audit_columns:
        audit_type = "VARCHAR" if engine == "duckdb" else "TEXT"
        for ac in AUDIT_COLUMNS:
            col_defs.append(f"{_qi(ac)} {audit_type} NOT NULL")

    if pk_cols and not inline_sqlite_surr:
        pk_expr = ", ".join(_qi(c) for c in pk_cols)
        col_defs.append(f"PRIMARY KEY ({pk_expr})")

    # 외래 키 제약(부모 단일 PK -> 컬럼별 FK, 복합 PK -> 부모 단위 묶음)
    composite_groups: dict[str, list[tuple[str, str]]] = {}
    for col in target_table.columns:
        if col.foreign_key is None:
            continue
        p_tbl, rest = col.foreign_key.split("(", 1)
        p_col = rest.rstrip(")")
        if parent_pk_counts.get(p_tbl, 1) > 1:
            composite_groups.setdefault(p_tbl, []).append((col.name, p_col))
        else:
            col_defs.append(f"FOREIGN KEY ({_qi(col.name)}) REFERENCES {_qi(p_tbl)} ({_qi(p_col)})")
    for p_tbl, pairs in composite_groups.items():
        if len(pairs) == parent_pk_counts.get(p_tbl, 0):
            c_expr = ", ".join(_qi(c) for c, _ in pairs)
            p_expr = ", ".join(_qi(p) for _, p in pairs)
            col_defs.append(f"FOREIGN KEY ({c_expr}) REFERENCES {_qi(p_tbl)} ({p_expr})")

    body = ",\n    ".join(col_defs)
    return f"CREATE TABLE IF NOT EXISTS {_qi(target_table.name)} (\n    {body}\n)"


def _parse_temporal_date(raw: str, *, korean: bool) -> date | None:
    s = raw.strip()
    if korean:
        m_ko = _KOREAN_DATE_RE.fullmatch(s)
        if m_ko is not None:
            try:
                return date(int(m_ko.group(1)), int(m_ko.group(2)), int(m_ko.group(3)))
            except ValueError:
                return None
    parsed = parse_date_text(s)
    if parsed is not None:
        return parsed
    if not korean and s.isascii() and s.isdigit():
        num = int(s)
        if EXCEL_SERIAL_MIN <= num <= EXCEL_SERIAL_MAX:
            return date(1899, 12, 30) + timedelta(days=num)
    return None


def _parse_temporal_timestamp(raw: str) -> datetime | None:
    s = raw.strip()
    iso_s = s[:-1] + "+00:00" if s.endswith("Z") else s
    try:
        return datetime.fromisoformat(iso_s)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y.%m.%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _parse_temporal_time(raw: str) -> dt_time | None:
    s = raw.strip()
    try:
        return dt_time.fromisoformat(s)
    except ValueError:
        pass
    for fmt in ("%H:%M:%S.%f", "%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(s, fmt).time()
        except ValueError:
            continue
    return None


def _is_missing_raw(raw: Any) -> bool:
    if raw is None:
        return True
    s = str(raw)
    if not s.strip():
        return True
    return s in DEFAULT_MISSING_MARKERS


def _transform_cell(
    raw: Any,
    *,
    col: TargetColumn,
    mapping: ColumnMapping,
    slot_rx: tuple[re.Pattern[str], int] | None,
    mask_policy: MaskingPolicy,
    sensitive_type: SensitiveType | None,
    engine: str,
    db_type_cast: str | None = None,
    hash_key: bytes | None = None,
) -> tuple[bool, Any, str]:
    """단일 원본 셀을 결정론적으로 변환·마스킹한다. 반환: `(ok, value, error_detail)`."""
    if _is_missing_raw(raw):
        return True, None, ""

    s = str(raw)

    # 1. 규칙 문자열 가변 슬롯 추출
    if slot_rx is not None:
        rx, cap_group = slot_rx
        m = rx.search(s)
        if m is None:
            if col.nullable:
                return True, None, ""
            return False, None, "문자열 패턴 슬롯 추출 불일치"
        extracted = m.group(cap_group)
        if extracted is None or extracted == "":
            if col.nullable:
                return True, None, ""
            return False, None, "추출된 슬롯 값이 비어 있음"
        s = extracted

    # 2. 마스킹/해시가 우선 지정된 비-PK/FK 컬럼(문자열 타입으로 승격됨)
    if mapping.transform in {"pii_mask", "pii_hash"} or (
        mask_policy is not MaskingPolicy.NONE and col.physical_type is PhysicalType.STRING
    ):
        masked = mask_value(s, sensitive_type, mask_policy, hash_key=hash_key)
        return True, masked, ""

    # 3. 변환식·물리 타입별 파싱
    type_cast = mapping.type_cast.strip().lower()
    val: Any
    if mapping.transform == "strip_currency_and_comma" or col.physical_type is PhysicalType.DECIMAL:
        if not _CURRENCY_SHAPE_RE.search(s):
            return False, None, "통화·숫자 형식 불일치"
        cleaned = _CURRENCY_STRIP_RE.sub("", s)
        try:
            dec = Decimal(cleaned)
        except InvalidOperation:
            return False, None, "DECIMAL 변환 실패"
        if not dec.is_finite():
            return False, None, "유한하지 않은 수치 값"
        if col.physical_type is PhysicalType.INTEGER:
            if dec != int(dec):
                return False, None, "정수 변환 불가(소수 존재)"
            val = int(dec)
        else:
            val = dec
    elif mapping.transform == "normalize_boolean" or col.physical_type is PhysicalType.BOOLEAN:
        low = s.strip().lower()
        if low in _TRUE_TOKENS:
            val = 1 if engine == "sqlite" else True
        elif low in _FALSE_TOKENS:
            val = 0 if engine == "sqlite" else False
        else:
            return False, None, "불리언 값(Y/N·예/아니오·true/false) 형식 불일치"
    elif mapping.transform == "parse_korean_date":
        d = _parse_temporal_date(s, korean=True)
        if d is None:
            return False, None, "한국어 날짜 형식 변환 실패"
        val = d.isoformat() if engine == "sqlite" else d
    elif mapping.transform == "parse_iso_temporal" or col.physical_type in {
        PhysicalType.DATE,
        PhysicalType.TIME,
        PhysicalType.TIMESTAMP,
        PhysicalType.TIMESTAMPTZ,
    }:
        if col.physical_type is PhysicalType.TIME:
            t_val = _parse_temporal_time(s)
            if t_val is None:
                return False, None, "시간(TIME) 변환 실패"
            val = t_val.isoformat() if engine == "sqlite" else t_val
        elif col.physical_type in {PhysicalType.TIMESTAMP, PhysicalType.TIMESTAMPTZ}:
            ts_val = _parse_temporal_timestamp(s)
            if ts_val is None:
                return False, None, "타임스탬프(TIMESTAMP) 변환 실패"
            val = ts_val.isoformat() if engine == "sqlite" else ts_val
        else:
            d = _parse_temporal_date(s, korean=False)
            if d is None:
                return False, None, "날짜(DATE) 변환 실패"
            val = d.isoformat() if engine == "sqlite" else d
    elif col.physical_type is PhysicalType.INTEGER:
        st = s.strip()
        if not _INT_RE.fullmatch(st):
            return False, None, "정수(INTEGER) 형식 불일치"
        val = int(st)
    elif col.physical_type is PhysicalType.FLOAT:
        try:
            fv = float(s.strip())
        except ValueError:
            return False, None, "실수(FLOAT) 변환 실패"
        if not math.isfinite(fv):
            return False, None, "유한하지 않은 실수 값"
        val = fv
    else:
        val = s
        m_vc = _VARCHAR_LEN_RE.fullmatch(type_cast)
        if m_vc is not None and len(val) > int(m_vc.group(1)):
            return False, None, f"VARCHAR({m_vc.group(1)}) 최대 길이 초과"

    if not isinstance(val, bool) and isinstance(val, (int, float, Decimal)):
        numeric_type = db_type_cast or (
            _dialect_col_type(col, mapping, engine)
            if engine == "duckdb" and col.physical_type is PhysicalType.DECIMAL
            else type_cast
        )
        m_num = _DECIMAL_PS_RE.fullmatch(numeric_type)
        if m_num is not None:
            precision, scale = int(m_num.group(1)), int(m_num.group(2))
            dec_val = val if isinstance(val, Decimal) else Decimal(str(val))
            # SQL NUMERIC은 소수부 자릿수를 반올림한 뒤 정수부 범위를 검사한다.
            overflow_at = Decimal((0, (9,) * precision + (5,), -scale - 1))
            if dec_val.copy_abs() >= overflow_at:
                return False, None, f"NUMERIC({precision}, {scale}) 허용 범위 초과"
            if engine != "sqlite" and isinstance(val, Decimal):
                quantum = Decimal((0, (1,), -scale))
                context = Context(prec=precision, rounding=ROUND_HALF_UP)
                val = val.quantize(quantum, context=context)
        if isinstance(val, Decimal) and val.is_zero():
            val = val.copy_abs()
        if db_type_cast == "integer" or (
            col.physical_type is PhysicalType.INTEGER and type_cast in {"int", "integer"}
        ):
            dec_val = val if isinstance(val, Decimal) else Decimal(str(val))
            rounded_int = int(dec_val.to_integral_value(rounding=ROUND_HALF_UP))
            if not (INT4_MIN <= rounded_int <= INT4_MAX):
                return False, None, "INT4 허용 범위 초과"
        if engine == "sqlite" and isinstance(val, Decimal):
            fv_dec = float(val)
            if not math.isfinite(fv_dec):
                return False, None, "유한하지 않은 수치 값"
            val = fv_dec
    if mask_policy is not MaskingPolicy.NONE and isinstance(val, str):
        val = mask_value(val, sensitive_type, mask_policy, hash_key=hash_key)
    return True, val, ""


def _resolve_slot_regex(
    mapping: ColumnMapping,
    cls_by_col: Mapping[str, ColumnClassification],
) -> tuple[re.Pattern[str], int] | None:
    cls = cls_by_col.get(mapping.source_column)
    if cls is None or cls.string_pattern is None:
        return None
    sp = cls.string_pattern
    for seg in sp.segments:
        if seg.capture_group is not None and (
            seg.extract_sql_hint == mapping.transform
            or (
                mapping.transform.startswith("NULLIF(regexp_extract(")
                and f", {seg.capture_group}), '')" in mapping.transform
            )
        ):
            return re.compile(sp.regex_pattern), int(seg.capture_group)
    return None


def _safe_quarantine_row_json(
    headers: Sequence[str],
    raw_row: Sequence[Any],
    *,
    table_masking: Mapping[str, MaskingPolicy],
    pii_type_by_col: Mapping[str, SensitiveType],
    pii_level_by_col: Mapping[str, SensitivityLevel],
    hash_key: bytes | None = None,
) -> str:
    """격리 보관함에 기록할 원본 행 JSON을 생성하되 PII·마스킹 대상 컬럼은 항상 보호한다."""
    payload: dict[str, Any] = {}
    for h, cell in zip(headers, raw_row, strict=True):
        if cell is None:
            payload[h] = None
            continue
        s = str(cell)
        policy = table_masking.get(h, MaskingPolicy.NONE)
        stype = pii_type_by_col.get(h)
        level = pii_level_by_col.get(h, SensitivityLevel.L0)
        if policy is not MaskingPolicy.NONE:
            payload[h] = mask_value(s, stype, policy, hash_key=hash_key)
        elif level is not SensitivityLevel.L0:
            payload[h] = mask_value(s, stype, MaskingPolicy.MASK)
        else:
            payload[h] = s
    return json.dumps(payload, ensure_ascii=False)


def _normalize_key_for_lookup(val: Any) -> Any:
    if isinstance(val, (date, datetime, dt_time)):
        return val.isoformat()
    if isinstance(val, Decimal):
        return str(val)
    return val


def _insert_management_batch_record(
    adapter: _DbAdapter,
    *,
    batch_id: str,
    loaded_at: str,
    purpose: Purpose,
    selected_candidate_id: str,
    status: str,
    table_count: int,
    total_rows_read: int,
    total_rows_loaded: int,
    total_rows_quarantined: int,
    overrides_json: str,
    notes: str,
) -> None:
    p = adapter.placeholder
    placeholders = ", ".join([p] * 12)
    adapter.execute(
        f'INSERT INTO "_etl_load_batches" ('
        f'"batch_id", "loaded_at", "purpose", "selected_candidate_id", '
        f'"db_engine", "status", "table_count", "total_rows_read", '
        f'"total_rows_loaded", "total_rows_quarantined", "overrides_json", "notes"'
        f") VALUES ({placeholders})",
        (
            batch_id,
            loaded_at,
            purpose.value,
            selected_candidate_id,
            adapter.engine,
            status,
            table_count,
            total_rows_read,
            total_rows_loaded,
            total_rows_quarantined,
            overrides_json,
            notes,
        ),
    )


def _next_quarantine_id(adapter: _DbAdapter) -> int:
    row = adapter.fetchone('SELECT COALESCE(MAX("quarantine_id"), 0) FROM "_etl_quarantine_rows"')
    return int(row[0]) + 1 if row is not None else 1


def _next_monotonic_dataset_timestamp(adapter: _DbAdapter, target_table: str) -> str:
    """테이블 잠금 보유 중 `_etl_datasets.loaded_at`이 커밋 순서대로 단조 증가하도록 생성한다."""
    now_dt = datetime.now(timezone.utc)
    prev_row = adapter.fetchone(
        f'SELECT MAX("loaded_at") FROM "_etl_datasets" '
        f'WHERE "target_table" = {adapter.placeholder}',
        (target_table,),
    )
    if prev_row is not None and prev_row[0]:
        prev_str = str(prev_row[0]).strip()
        try:
            prev_dt = datetime.fromisoformat(prev_str.replace("Z", "+00:00"))
            if prev_dt.tzinfo is None:
                prev_dt = prev_dt.replace(tzinfo=timezone.utc)
            # 구버전 초 단위 문자열("...01Z")은 문자열 비교 시 ".000001Z"보다 크므로 +1초 올림
            step = timedelta(microseconds=1) if "." in prev_str else timedelta(seconds=1)
            min_next = prev_dt + step
            if now_dt < min_next:
                now_dt = min_next
        except ValueError:
            pass
    return now_dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


_SNAPSHOT_TYPE_KEY = "__etl_type__"
_SNAPSHOT_OP_KEY = "__etl_op__"
_PK_CHUNK_PARAM_LIMIT = 400
MAX_ROLLBACK_SNAPSHOT_ROWS = 50_000
MAX_ROLLBACK_SNAPSHOT_BYTES = 32 * 1024 * 1024

_PG_TEXT_CAST_TYPES: dict[str, str] = {
    "json": "pg_catalog.json",
    "jsonb": "pg_catalog.jsonb",
    "json[]": "pg_catalog.json[]",
    "jsonb[]": "pg_catalog.jsonb[]",
}


def _pg_supports_batched_rowcount() -> bool:
    """Psycopg 3.1+ 및 libpq 14+의 executemany 파이프라인 누적 rowcount 지원 여부를 판정한다."""
    try:
        import psycopg
    except ImportError:
        return False
    ver_str = str(getattr(psycopg, "__version__", "0.0"))
    parts: list[int] = []
    for tok in ver_str.split(".")[:2]:
        digits = "".join(ch for ch in tok if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    if tuple(parts) < (3, 1):
        return False
    pq_mod = getattr(psycopg, "pq", None)
    pq_ver_fn = getattr(pq_mod, "version", None)
    if callable(pq_ver_fn):
        try:
            return int(pq_ver_fn()) >= 140000
        except (TypeError, ValueError):
            return False
    return False


class _PgTypedText:
    """PostgreSQL JSON/JSONB(및 배열) DB 원문 텍스트를 SQL NULL과 구분해 보존하는 래퍼."""

    __slots__ = ("pg_type", "text")

    def __init__(self, pg_type: str, text: str) -> None:
        self.pg_type = pg_type
        self.text = text

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, _PgTypedText)
            and self.pg_type == other.pg_type
            and self.text == other.text
        )

    def __repr__(self) -> str:
        return f"_PgTypedText({self.pg_type!r}, {self.text!r})"


def _ensure_management_table_columns(adapter: _DbAdapter) -> None:
    """구버전 DB의 `_etl_datasets` 테이블에 롤백 스냅샷 컬럼이 없으면 안전하게 진화 추가한다."""
    if not adapter.table_exists("_etl_datasets"):
        return
    existing = set(adapter.existing_columns("_etl_datasets"))
    int_t = "BIGINT" if adapter.engine in {"duckdb", "postgres"} else "INTEGER"
    txt_t = "VARCHAR" if adapter.engine == "duckdb" else "TEXT"
    if "rows_updated" not in existing:
        adapter.execute(f'ALTER TABLE "_etl_datasets" ADD COLUMN "rows_updated" {int_t} DEFAULT 0')
    if "rollback_snapshots_json" not in existing:
        adapter.execute(f'ALTER TABLE "_etl_datasets" ADD COLUMN "rollback_snapshots_json" {txt_t}')
    if "snapshot_source_batches_json" not in existing:
        adapter.execute(
            f'ALTER TABLE "_etl_datasets" ADD COLUMN "snapshot_source_batches_json" {txt_t}'
        )


def _encode_snapshot_value(val: Any) -> Any:
    """DB 셀 값을 타입 손실 없이 JSON 직렬화 가능한 구조로 인코딩한다."""
    if val is None or isinstance(val, (bool, int, float, str)):
        return val
    if isinstance(val, _PgTypedText):
        return {_SNAPSHOT_TYPE_KEY: "pg_text", "t": val.pg_type, "v": val.text}
    if isinstance(val, Decimal):
        return {_SNAPSHOT_TYPE_KEY: "decimal", "v": str(val)}
    if isinstance(val, datetime):
        return {_SNAPSHOT_TYPE_KEY: "datetime", "v": val.isoformat()}
    if isinstance(val, date):
        return {_SNAPSHOT_TYPE_KEY: "date", "v": val.isoformat()}
    if isinstance(val, dt_time):
        return {_SNAPSHOT_TYPE_KEY: "time", "v": val.isoformat()}
    if isinstance(val, (bytes, bytearray, memoryview)):
        return {
            _SNAPSHOT_TYPE_KEY: "bytes",
            "v": base64.b64encode(bytes(val)).decode("ascii"),
        }
    if isinstance(val, uuid.UUID):
        return {_SNAPSHOT_TYPE_KEY: "uuid", "v": str(val)}
    if isinstance(val, dict):
        return {
            _SNAPSHOT_TYPE_KEY: "dict",
            "v": {str(k): _encode_snapshot_value(v) for k, v in val.items()},
        }
    if isinstance(val, list):
        return {
            _SNAPSHOT_TYPE_KEY: "list",
            "v": [_encode_snapshot_value(item) for item in val],
        }
    if isinstance(val, tuple):
        return {
            _SNAPSHOT_TYPE_KEY: "tuple",
            "v": [_encode_snapshot_value(item) for item in val],
        }
    return str(val)


def _decode_snapshot_value(val: Any) -> Any:
    """`_encode_snapshot_value`로 직렬화된 값을 원래 Python/DB 바인딩 타입으로 복원한다."""
    if isinstance(val, dict) and _SNAPSHOT_TYPE_KEY in val:
        tag = val.get(_SNAPSHOT_TYPE_KEY)
        raw_val = val.get("v")
        if tag == "pg_text":
            pg_t = str(val.get("t", ""))
            if pg_t in _PG_TEXT_CAST_TYPES and raw_val is not None:
                return _PgTypedText(pg_t, str(raw_val))
        if tag == "decimal":
            return Decimal(str(raw_val))
        if tag == "datetime":
            return datetime.fromisoformat(str(raw_val))
        if tag == "date":
            return date.fromisoformat(str(raw_val))
        if tag == "time":
            return dt_time.fromisoformat(str(raw_val))
        if tag == "bytes":
            return base64.b64decode(str(raw_val))
        if tag == "uuid":
            return uuid.UUID(str(raw_val))
        if tag == "dict" and isinstance(raw_val, dict):
            return {str(k): _decode_snapshot_value(v) for k, v in raw_val.items()}
        if tag == "list" and isinstance(raw_val, list):
            return [_decode_snapshot_value(item) for item in raw_val]
        if tag == "tuple" and isinstance(raw_val, list):
            return tuple(_decode_snapshot_value(item) for item in raw_val)
    if isinstance(val, list):
        return [_decode_snapshot_value(item) for item in val]
    return val


def _import_pg_json_wrappers() -> tuple[Any, Any] | None:
    try:
        from psycopg.types.json import Json, Jsonb  # type: ignore[import-not-found]
    except ImportError:
        return None
    return Json, Jsonb


def _bind_restore_value(val: Any, col_type: str, engine: str) -> Any:
    """스냅샷에서 복원한 Python 값을 DB 엔진·컬럼 타입에 맞는 바인딩 파라미터로 변환한다."""
    if val is None:
        return None
    if isinstance(val, _PgTypedText):
        return val.text
    norm_type = col_type.strip().lower()
    if engine == "postgres":
        if norm_type in {"json", "jsonb"} or isinstance(val, dict):
            wrappers = _import_pg_json_wrappers()
            if wrappers is None:
                return json.dumps(val, ensure_ascii=False)
            pg_json, pg_jsonb = wrappers
            return pg_json(val) if norm_type == "json" else pg_jsonb(val)
        if norm_type in {"json[]", "jsonb[]"} and isinstance(val, list):
            if any(isinstance(el, list) for el in val):
                raise InputError(
                    f"구버전 `{norm_type}` 스냅샷에 중첩 리스트가 포함되어 "
                    "배열 차원을 안전하게 판별할 수 없어 복원을 중단합니다."
                )
            wrappers = _import_pg_json_wrappers()
            if wrappers is None:
                return [None if el is None else json.dumps(el, ensure_ascii=False) for el in val]
            pg_json, pg_jsonb = wrappers
            wrapper = pg_json if norm_type == "json[]" else pg_jsonb
            return [None if el is None else wrapper(el) for el in val]
    if engine == "sqlite" and isinstance(val, (dict, list)):
        return json.dumps(val, ensure_ascii=False)
    return val


def _pk_predicate_and_params(
    pk_cols: Sequence[str],
    pk_tuples: Sequence[tuple[Any, ...]],
    placeholder: str,
) -> tuple[str, list[Any]]:
    """단일·복합 PK 튜플 목록을 안전하게 파라미터 바인딩된 WHERE 조건절로 변환한다."""
    if len(pk_cols) == 1:
        ph_list = ", ".join([placeholder] * len(pk_tuples))
        return f"{_qi(pk_cols[0])} IN ({ph_list})", [t[0] for t in pk_tuples]
    one_cond = "(" + " AND ".join(f"{_qi(c)} = {placeholder}" for c in pk_cols) + ")"
    where_sql = "(" + " OR ".join([one_cond] * len(pk_tuples)) + ")"
    params = [v for tup in pk_tuples for v in tup]
    return where_sql, params


def _fetch_existing_rows_by_pks(
    adapter: _DbAdapter,
    table_name: str,
    pk_cols: Sequence[str],
    pk_tuples: Sequence[tuple[Any, ...]],
    *,
    columns: Sequence[str] | None = None,
    max_rows: int | None = None,
    max_bytes: int | None = None,
    include_insert_ops: bool = False,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """UPSERT 갱신 직전 기존 DB 행의 쓰기 가능 컬럼 스냅샷을 PK 입력 순서대로 수집한다.

    ``include_insert_ops=True``이고 기존 행이 1건 이상 매칭되면, 신규 삽입 행의 PK 위치도
    ``{"__etl_op__": "delete", "pk": {...}}`` 마커로 입력 순서에 끼워 넣어 혼합 배치의
    정확한 역순 롤백(자기참조 CASCADE FK · UNIQUE 값 재사용)을 보장한다.
    갱신 대상 행 수가 ``max_rows``를 초과하거나 직렬화 크기가 ``max_bytes``를 초과하면
    메모리·셀 크기 보호를 위해 ``(None, reason)``을 반환한다.
    """
    if not pk_cols or not pk_tuples:
        return [], None
    row_limit = MAX_ROLLBACK_SNAPSHOT_ROWS if max_rows is None else max_rows
    byte_limit = MAX_ROLLBACK_SNAPSHOT_BYTES if max_bytes is None else max_bytes
    writable = adapter.writable_columns(table_name)
    if not writable:
        return [], None
    if columns is not None:
        writable_set = set(writable)
        ordered_cols: list[str] = []
        for c in (*pk_cols, *columns):
            if c in writable_set and c not in ordered_cols:
                ordered_cols.append(c)
        select_cols = ordered_cols
    else:
        select_cols = writable
    if not select_cols:
        return [], None

    pg_raw_types: dict[str, str] = {}
    if adapter.engine == "postgres":
        col_types = adapter.column_data_types(table_name)
        pg_raw_types = {
            c: col_types[c] for c in select_cols if col_types.get(c, "") in _PG_TEXT_CAST_TYPES
        }

    pk_positions = [select_cols.index(c) for c in pk_cols]
    order_by_pk: dict[tuple[Any, ...], int] = {}
    casefold_order_by_pk: dict[tuple[Any, ...], int | None] = {}
    for idx, tup in enumerate(pk_tuples):
        norm = tuple(_normalize_key_for_lookup(v) for v in tup)
        order_by_pk[norm] = idx
        cf = tuple(v.casefold() if isinstance(v, str) else v for v in norm)
        if cf not in casefold_order_by_pk:
            casefold_order_by_pk[cf] = idx
        else:
            casefold_order_by_pk[cf] = None

    select_sql = ", ".join(f"{_qi(c)}::text" if c in pg_raw_types else _qi(c) for c in select_cols)
    chunk_size = max(1, _PK_CHUNK_PARAM_LIMIT // len(pk_cols))
    found: list[tuple[int, dict[str, Any]]] = []
    total_bytes = 2  # "[]" JSON 배열 오버헤드
    for start in range(0, len(pk_tuples), chunk_size):
        chunk = pk_tuples[start : start + chunk_size]
        where_sql, params = _pk_predicate_and_params(pk_cols, chunk, adapter.placeholder)
        rows = adapter.fetchall(
            f"SELECT {select_sql} FROM {_qi(table_name)} WHERE {where_sql}",
            params,
        )
        for row in rows:
            if len(found) >= row_limit:
                return None, "rows"
            snap_dict: dict[str, Any] = {}
            for col, v in zip(select_cols, row, strict=True):
                pg_t = pg_raw_types.get(col)
                if pg_t is not None:
                    snap_dict[col] = (
                        None
                        if v is None
                        else {_SNAPSHOT_TYPE_KEY: "pg_text", "t": pg_t, "v": str(v)}
                    )
                else:
                    snap_dict[col] = _encode_snapshot_value(v)
            total_bytes += len(json.dumps(snap_dict, ensure_ascii=False).encode("utf-8")) + 2
            if total_bytes > byte_limit:
                return None, "bytes"
            norm_pk = tuple(_normalize_key_for_lookup(row[pos]) for pos in pk_positions)
            order_idx = order_by_pk.get(norm_pk)
            if order_idx is None:
                cf_pk = tuple(v.casefold() if isinstance(v, str) else v for v in norm_pk)
                cf_idx = casefold_order_by_pk.get(cf_pk)
                order_idx = cf_idx if cf_idx is not None else len(pk_tuples)
            found.append((order_idx, snap_dict))
    found.sort(key=lambda item: item[0])
    restore_only = [snap for _, snap in found]
    if not include_insert_ops or not found:
        return restore_only, None

    # 입력 위치가 매칭되지 않은 행이 있으면 삽입/갱신 순서를 확정할 수 없으므로 스냅샷 생략
    if any(idx >= len(pk_tuples) for idx, _ in found):
        return None, "order"

    found_by_idx: dict[int, dict[str, Any]] = dict(found)
    if len(found_by_idx) != len(found):
        return None, "order"

    # 삽입 마커를 포함한 전체 연산 수가 상한을 넘으면 갱신 스냅샷만 보존하는 폴백으로 전환한다.
    # (일반 테이블은 롤백 가능성을 유지하고, 자기참조 FK 테이블은 rollback_batch가 안전 차단)
    if len(pk_tuples) > row_limit:
        return restore_only, None

    ops: list[dict[str, Any]] = []
    ops_bytes = total_bytes
    for idx, pk_tup in enumerate(pk_tuples):
        if idx in found_by_idx:
            ops.append(found_by_idx[idx])
        else:
            del_op: dict[str, Any] = {
                _SNAPSHOT_OP_KEY: "delete",
                "pk": {
                    col: _encode_snapshot_value(val)
                    for col, val in zip(pk_cols, pk_tup, strict=True)
                },
            }
            ops_bytes += len(json.dumps(del_op, ensure_ascii=False).encode("utf-8")) + 2
            if ops_bytes > byte_limit:
                return restore_only, None
            ops.append(del_op)
    return ops, None


def _fetch_existing_source_batches_by_pks(
    adapter: _DbAdapter,
    table_name: str,
    pk_cols: Sequence[str],
    pk_tuples: Sequence[tuple[Any, ...]],
) -> list[str] | None:
    """스냅샷 본문을 생략할 때도 영향받는 선행 배치 ID(`_etl_batch_id`) 집합을 경량 조회한다."""
    if not pk_cols or not pk_tuples:
        return []
    if "_etl_batch_id" not in set(adapter.existing_columns(table_name)):
        return None
    chunk_size = max(1, _PK_CHUNK_PARAM_LIMIT // len(pk_cols))
    batch_ids: set[str] = set()
    for start in range(0, len(pk_tuples), chunk_size):
        chunk = pk_tuples[start : start + chunk_size]
        where_sql, params = _pk_predicate_and_params(pk_cols, chunk, adapter.placeholder)
        rows = adapter.fetchall(
            f'SELECT DISTINCT "_etl_batch_id" FROM {_qi(table_name)} '
            f'WHERE {where_sql} AND "_etl_batch_id" IS NOT NULL',
            params,
        )
        for r in rows:
            if r[0] is not None:
                batch_ids.add(str(r[0]))
    return sorted(batch_ids)


def _is_integrity_error(exc: Exception, engine: str) -> bool:
    """SQLite/DuckDB/PostgreSQL 제약(FK/UNIQUE/PK) 위반 예외 여부를 판정한다."""
    if isinstance(exc, (sqlite3.IntegrityError, duckdb.ConstraintException)):
        return True
    return engine == "postgres" and exc.__class__.__name__ in {
        "IntegrityError",
        "ForeignKeyViolation",
        "UniqueViolation",
        "NotNullViolation",
        "CheckViolation",
    }


def _load_single_table(
    adapter: _DbAdapter,
    *,
    mctx: MultiTableContext,
    bundle: TableProfileBundle,
    target_table: TargetTable,
    table_plan: TableLoadStrategy,
    smap: SensitivityMap,
    batch_id: str,
    dataset_id: str,
    loaded_at: str,
    include_audit_columns: bool,
    orphan_policy: OrphanPolicy,
    table_existed_before: bool = False,
    capture_rollback_snapshots: bool = True,
    skipped_key_masking: Mapping[str, MaskingPolicy] | None = None,
    hash_key: bytes | None = None,
) -> tuple[
    int,
    int,
    list[tuple[Any, ...]],
    int,
    list[dict[str, Any]] | None,
    list[str] | None,
    list[str],
    str | None,
]:
    """단일 테이블의 행 변환·제약 검증·DB INSERT/UPSERT를 수행한다."""
    slot: TableSlotContext = mctx.get_by_name(table_plan.source_table)
    src_rel = f"{slot.table_info.table_id}.{RAW_TABLE}"
    raw_col_ids = [c.column_id for c in slot.columns]
    header_pos = {c.name: c.index for c in bundle.ingest.columns}
    raw_headers = sorted(header_pos, key=header_pos.__getitem__)

    assert mctx.connection is not None
    sel_sql = ", ".join(quote_identifier(cid) for cid in raw_col_ids)
    raw_rows = mctx.connection.execute(f"SELECT {sel_sql} FROM {src_rel}").fetchall()

    cls_by_col = {c.column: c for c in bundle.classifications}
    map_by_target = {m.target_column: m for m in table_plan.column_mappings}
    pii_type_by_col = {a.column: a.types[0] for a in smap.assignments if a.types}
    pii_level_by_col = {a.column: a.level for a in smap.assignments}
    # 원래의 요청 정책은 행 변환과 격리함 보호에 계속 사용한다.
    table_masking = {**table_plan.column_masking, **(skipped_key_masking or {})}

    # 미리 정규식·슬롯 정보 준비
    slot_rx_by_target: dict[str, tuple[re.Pattern[str], int] | None] = {}
    for col in target_table.columns:
        mapping = map_by_target[col.name]
        slot_rx_by_target[col.name] = _resolve_slot_regex(mapping, cls_by_col)

    # 라이브 DB에서 부모 FK 키 집합 조회
    parent_keys: dict[str, set[Any]] = {}
    for col in target_table.columns:
        if col.foreign_key is None:
            continue
        p_tbl, rest = col.foreign_key.split("(", 1)
        p_col = rest.rstrip(")")
        rows_p = adapter.fetchall(
            f"SELECT DISTINCT {_qi(p_col)} FROM {_qi(p_tbl)} WHERE {_qi(p_col)} IS NOT NULL"
        )
        parent_keys[col.name] = {_normalize_key_for_lookup(r[0]) for r in rows_p}

    # 서로게이트 IDENTITY 컬럼이 포함된 PK(단일 또는 Analytics 파티션 복합 PK)는
    # 모든 신규 행이 고유 시퀀스 ID를 받으므로 비-IDENTITY 파티션 컬럼 단독 중복 검사를 하지 않는다.
    all_pk_cols = [c.name for c in target_table.columns if c.is_primary_key]
    has_identity_pk = any(c.is_primary_key and c.is_identity for c in target_table.columns)
    pk_cols = [] if has_identity_pk else all_pk_cols
    use_upsert = bool(pk_cols) and table_plan.increment_mode in {
        IncrementMode.UPSERT,
        IncrementMode.WATERMARK,
    }
    dedup = table_plan.deduplication
    keep_last_pk = (
        use_upsert and dedup.enabled and dedup.keep == "last" and set(dedup.keys) == set(pk_cols)
    )
    confirmed_uq_cols = [uq for uq in table_plan.confirmed_unique_columns if all_pk_cols != [uq]]

    # 기존 DB에 저장된 UNIQUE 컬럼 값(다른 PK와 충돌 검사)
    existing_unique_owner: dict[str, dict[Any, tuple[Any, ...]]] = {}
    if confirmed_uq_cols and adapter.table_exists(target_table.name):
        for uq_col in confirmed_uq_cols:
            if pk_cols:
                pk_sel = ", ".join(_qi(pk) for pk in pk_cols)
                uq_rows = adapter.fetchall(
                    f"SELECT {_qi(uq_col)}, {pk_sel} FROM {_qi(target_table.name)} "
                    f"WHERE {_qi(uq_col)} IS NOT NULL"
                )
                existing_unique_owner[uq_col] = {
                    _normalize_key_for_lookup(r[0]): tuple(
                        _normalize_key_for_lookup(v) for v in r[1:]
                    )
                    for r in uq_rows
                }
            else:
                uq_rows = adapter.fetchall(
                    f"SELECT {_qi(uq_col)} FROM {_qi(target_table.name)} "
                    f"WHERE {_qi(uq_col)} IS NOT NULL"
                )
                existing_unique_owner[uq_col] = {
                    _normalize_key_for_lookup(r[0]): () for r in uq_rows
                }

    # INSERT 대상 컬럼 결정 (DDL에 자동 증가 IDENTITY가 활성화된 경우에만 생략, 그 외엔 명시 할당)
    surr_col = next((c for c in target_table.columns if c.is_identity), None)
    omit_identity_in_insert = _uses_db_autoincrement(target_table, adapter.engine)
    if (
        omit_identity_in_insert
        and adapter.engine == "sqlite"
        and surr_col is not None
        and table_existed_before
        and not adapter.sqlite_single_identity_pk(target_table.name, surr_col.name)
    ):
        omit_identity_in_insert = False
    insert_cols = [
        c for c in target_table.columns if not (omit_identity_in_insert and c.is_identity)
    ]
    insert_col_names = [c.name for c in insert_cols]
    if include_audit_columns:
        insert_col_names.extend(AUDIT_COLUMNS)

    next_surr_id = 1
    if surr_col is not None and not omit_identity_in_insert:
        max_row = adapter.fetchone(
            f"SELECT COALESCE(MAX({_qi(surr_col.name)}), 0) FROM {_qi(target_table.name)}"
        )
        next_surr_id = int(max_row[0]) + 1 if max_row is not None else 1

    seen_pk_positions: dict[tuple[Any, ...], int] = {}
    seen_uqs: dict[str, set[Any]] = {uq: set() for uq in confirmed_uq_cols}
    uq_positions = {uq: insert_col_names.index(uq) for uq in confirmed_uq_cols}
    db_not_null_cols, db_numeric_types = (
        adapter.existing_column_constraints(target_table.name)
        if table_existed_before
        else (set(), {})
    )
    valid_rows: list[list[Any]] = []
    dedup_replaced = 0
    quarantine_entries: list[tuple[Any, ...]] = []

    for row_idx, raw_row in enumerate(raw_rows, start=1):
        converted: dict[str, Any] = {}
        err_cat: str | None = None
        err_col = ""
        err_detail = ""

        for col in target_table.columns:
            if col.is_identity:
                continue
            mapping = map_by_target[col.name]
            src_col = mapping.source_column
            pos = header_pos.get(src_col)
            raw_val = raw_row[pos] if pos is not None else None
            policy = table_masking.get(src_col, MaskingPolicy.NONE)
            stype = pii_type_by_col.get(src_col)

            ok, val, detail = _transform_cell(
                raw_val,
                col=col,
                mapping=mapping,
                slot_rx=slot_rx_by_target[col.name],
                mask_policy=policy,
                sensitive_type=stype,
                engine=adapter.engine,
                db_type_cast=db_numeric_types.get(col.name),
                hash_key=hash_key,
            )
            if not ok:
                err_cat = "cast_error"
                err_col = col.name
                err_detail = detail
                break
            if val is None and (not col.nullable or col.name in db_not_null_cols):
                err_cat = "not_null_violation"
                err_col = col.name
                err_detail = "필수(NOT NULL) 컬럼 값이 비어 있음"
                break
            converted[col.name] = val

        # PK 중복 검사 (단일 배치 내 중복)
        pk_tuple: tuple[Any, ...] = ()
        previous_position = None
        previous_row = None
        if err_cat is None and pk_cols:
            pk_tuple = tuple(_normalize_key_for_lookup(converted[c]) for c in pk_cols)
            previous_position = seen_pk_positions.get(pk_tuple)
            if previous_position is not None:
                if keep_last_pk:
                    previous_row = valid_rows[previous_position]
                else:
                    err_cat = "pk_duplicate"
                    err_col = ",".join(pk_cols)
                    err_detail = "동일 파일 내 기본 키(PK) 중복"

        # 확정 UNIQUE 컬럼 중복 검사
        if err_cat is None and confirmed_uq_cols:
            for uq_col in confirmed_uq_cols:
                uq_val = converted.get(uq_col)
                if uq_val is None:
                    continue
                norm_uq = _normalize_key_for_lookup(uq_val)
                own_uq = previous_row is not None and norm_uq == _normalize_key_for_lookup(
                    previous_row[uq_positions[uq_col]]
                )
                if norm_uq in seen_uqs[uq_col] and not own_uq:
                    err_cat = "unique_violation"
                    err_col = uq_col
                    err_detail = "동일 파일 내 확정 UNIQUE 컬럼 중복"
                    break
                prev_owner = existing_unique_owner.get(uq_col, {}).get(norm_uq)
                if prev_owner is not None and (not pk_cols or prev_owner != pk_tuple):
                    err_cat = "unique_violation"
                    err_col = uq_col
                    err_detail = "기존 DB 행과 확정 UNIQUE 컬럼 중복"
                    break

        # FK 부모 존재 검사
        if err_cat is None:
            for col in target_table.columns:
                if col.foreign_key is None:
                    continue
                fk_val = converted.get(col.name)
                if fk_val is None:
                    continue
                if _normalize_key_for_lookup(fk_val) not in parent_keys.get(col.name, set()):
                    err_cat = "fk_orphan"
                    err_col = col.name
                    err_detail = f"참조 부모({col.foreign_key})에 존재하지 않는 고아 외래 키"
                    break

        if err_cat is not None:
            if orphan_policy is OrphanPolicy.REJECT_ALL:
                return (
                    len(raw_rows),
                    0,
                    [],
                    0,
                    [],
                    [],
                    [],
                    f"`{target_table.name}` {row_idx}행 `{err_col}` ({err_cat}): {err_detail}",
                )
            safe_json = _safe_quarantine_row_json(
                raw_headers,
                raw_row,
                table_masking=table_masking,
                pii_type_by_col=pii_type_by_col,
                pii_level_by_col=pii_level_by_col,
                hash_key=hash_key,
            )
            quarantine_entries.append(
                (
                    batch_id,
                    dataset_id,
                    target_table.name,
                    row_idx,
                    err_cat,
                    err_col,
                    err_detail,
                    safe_json,
                    loaded_at,
                )
            )
            continue

        for uq_col in confirmed_uq_cols:
            if previous_row is not None:
                old_uq = previous_row[uq_positions[uq_col]]
                if old_uq is not None:
                    seen_uqs[uq_col].remove(_normalize_key_for_lookup(old_uq))
            uq_val = converted.get(uq_col)
            if uq_val is not None:
                seen_uqs[uq_col].add(_normalize_key_for_lookup(uq_val))

        row_values: list[Any] = []
        for col in insert_cols:
            if col.is_identity:
                row_values.append(next_surr_id)
                next_surr_id += 1
            else:
                row_values.append(converted[col.name])
        if include_audit_columns:
            row_values.extend([batch_id, dataset_id, loaded_at])
        if previous_position is None:
            if pk_cols:
                seen_pk_positions[pk_tuple] = len(valid_rows)
            valid_rows.append(row_values)
        else:
            valid_rows[previous_position] = row_values
            dedup_replaced += 1

    pre_update_snapshots: list[dict[str, Any]] | None = []
    predecessor_batch_ids: list[str] | None = []
    snap_omit_reason: str | None = None
    actual_updated = 0
    table_warnings: list[str] = []
    if dedup_replaced > 0:
        table_warnings.append(
            f"`{target_table.name}` 동일 파일 내 PK 중복 {dedup_replaced:,}건을 "
            "마지막 유효 행으로 대체했습니다."
        )
    if valid_rows:
        cols_sql = ", ".join(_qi(c) for c in insert_col_names)
        placeholders = ", ".join([adapter.placeholder] * len(insert_col_names))
        insert_sql = f"INSERT INTO {_qi(target_table.name)} ({cols_sql}) VALUES ({placeholders})"
        do_upsert_update = False
        if use_upsert:
            pk_set = set(pk_cols)
            non_pk_cols = [c for c in insert_col_names if c not in pk_set]
            pk_sql = ", ".join(_qi(c) for c in pk_cols)
            if non_pk_cols:
                do_upsert_update = True
                set_sql = ", ".join(f"{_qi(c)} = excluded.{_qi(c)}" for c in non_pk_cols)
                insert_sql += f" ON CONFLICT ({pk_sql}) DO UPDATE SET {set_sql}"
            else:
                insert_sql += f" ON CONFLICT ({pk_sql}) DO NOTHING"

        count_before: int | None = None
        if do_upsert_update and table_existed_before:
            # PostgreSQL 쓰기 직렬화 잠금은 load_tables에서 대상 테이블을 정렬 순서로 선획득한다.
            cnt_before_row = adapter.fetchone(f"SELECT COUNT(*) FROM {_qi(target_table.name)}")
            count_before = int(cnt_before_row[0]) if cnt_before_row is not None else 0
            pk_positions = [insert_col_names.index(c) for c in pk_cols]
            incoming_pks = [tuple(row[pos] for pos in pk_positions) for row in valid_rows]
            if capture_rollback_snapshots:
                pre_update_snapshots, snap_omit_reason = _fetch_existing_rows_by_pks(
                    adapter,
                    target_table.name,
                    pk_cols,
                    incoming_pks,
                    columns=insert_col_names,
                    include_insert_ops=True,
                )
                if pre_update_snapshots is None:
                    # 상한 초과로 본문 스냅샷을 생략하더라도 영향받는 선행 배치 링크는 보존한다.
                    predecessor_batch_ids = (
                        None
                        if snap_omit_reason == "order"
                        else _fetch_existing_source_batches_by_pks(
                            adapter,
                            target_table.name,
                            pk_cols,
                            incoming_pks,
                        )
                    )
                else:
                    predecessor_batch_ids = sorted(
                        {
                            str(s["_etl_batch_id"])
                            for s in pre_update_snapshots
                            if _SNAPSHOT_OP_KEY not in s and s.get("_etl_batch_id") is not None
                        }
                    )
            else:
                # 감사 컬럼 비활성화 등 소유권을 갱신하지 않는 UPSERT에는 복원 스냅샷을 남기지 않음
                pre_update_snapshots = None
                predecessor_batch_ids = _fetch_existing_source_batches_by_pks(
                    adapter,
                    target_table.name,
                    pk_cols,
                    incoming_pks,
                )

        adapter.executemany(insert_sql, valid_rows)

        if do_upsert_update and table_existed_before and count_before is not None:
            cnt_after_row = adapter.fetchone(f"SELECT COUNT(*) FROM {_qi(target_table.name)}")
            count_after = int(cnt_after_row[0]) if cnt_after_row is not None else count_before
            actual_inserted = max(0, count_after - count_before)
            actual_updated = max(0, len(valid_rows) - actual_inserted)
            if capture_rollback_snapshots:
                if pre_update_snapshots is None:
                    if snap_omit_reason == "bytes":
                        table_warnings.append(
                            f"`{target_table.name}` 갱신 스냅샷 크기가 바이트 상한"
                            f"({MAX_ROLLBACK_SNAPSHOT_BYTES:,}B)을 초과해 스냅샷을 생략했습니다."
                        )
                    elif snap_omit_reason == "order":
                        table_warnings.append(
                            f"`{target_table.name}` PK 비교 규칙 차이로 입력 순서를 확정할 수 없어 "
                            "롤백 스냅샷을 생략했습니다."
                        )
                    else:
                        table_warnings.append(
                            f"`{target_table.name}` 갱신 행({actual_updated:,}건)이 "
                            f"롤백 스냅샷 상한({MAX_ROLLBACK_SNAPSHOT_ROWS:,}건)을 초과해 "
                            "스냅샷을 생략했습니다."
                        )
                else:
                    captured_cnt = sum(1 for s in pre_update_snapshots if _SNAPSHOT_OP_KEY not in s)
                    if captured_cnt != actual_updated:
                        pre_update_snapshots = None
                        # PK 비교 차이로 누락된 선행 배치가 있을 수 있어 미확정(None) 처리
                        predecessor_batch_ids = None
                        table_warnings.append(
                            f"`{target_table.name}` PK 비교 규칙 차이로 "
                            f"스냅샷({captured_cnt:,}건)과 실제 갱신({actual_updated:,}건)이 "
                            "달라 롤백 스냅샷을 생략했습니다."
                        )
                    elif (
                        actual_updated > 0
                        and actual_inserted > 0
                        and not any(_SNAPSHOT_OP_KEY in s for s in pre_update_snapshots)
                        and adapter.has_self_referencing_fk(target_table.name)
                    ):
                        table_warnings.append(
                            f"`{target_table.name}` 자기참조 외래 키 테이블의 혼합 배치 연산 수가 "
                            "상한을 초과해 삽입 순서 마커를 생략했습니다(배치 롤백이 제한됩니다)."
                        )

    return (
        len(raw_rows),
        len(valid_rows),
        quarantine_entries,
        actual_updated,
        pre_update_snapshots,
        predecessor_batch_ids,
        table_warnings,
        None,
    )


def load_tables(
    sources: Sequence[str | Path] = (),
    *,
    db_path: str | Path,
    engine: str = "sqlite",
    extra_tables: Sequence[NamedTableInput] = (),
    purpose: Purpose | str = Purpose.OLTP,
    decompose_strings: bool = True,
    overrides: SchemaOverrideSpec | None = None,
    auto_mask_pii: bool = False,
    skip_duplicate_files: bool = True,
    force_reload: bool = False,
    recursive: bool = True,
    min_inclusion_rate: float = MIN_INCLUSION_RATE,
    notes: str = "",
) -> tuple[MultiTableAnalysisBundle, DbLoadResult]:
    """여러 표 파일을 결정론 분석하고 실제 DB(SQLite/DuckDB/PostgreSQL)에 적재한다.

    기본적으로 4개의 데이터 관리 테이블(``_etl_load_batches``, ``_etl_datasets``,
    ``_etl_column_registry``, ``_etl_quarantine_rows``)과 행 단위 이력 컬럼
    (``_etl_batch_id``, ``_etl_dataset_id``, ``_etl_loaded_at``)을 함께 생성·기록한다.
    """
    t0 = time.perf_counter()
    norm_engine = _normalize_engine(engine)
    resolved_purpose = purpose if isinstance(purpose, Purpose) else Purpose(purpose)
    spec = overrides if overrides is not None else SchemaOverrideSpec()

    batch_stamp = datetime.now(timezone.utc)
    loaded_at = batch_stamp.strftime("%Y-%m-%dT%H:%M:%SZ")
    batch_id = f"batch_{batch_stamp.strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:8]}"

    inputs_arg: Sequence[str | Path] | None = list(sources) if sources else None
    with MultiTableContext(
        inputs=inputs_arg,
        extra_tables=extra_tables,
        recursive=recursive,
    ) as mctx:
        tables = profile_all_tables(mctx)
        relations = analyze_multi_table_relations(
            mctx,
            tables,
            min_inclusion_rate=min_inclusion_rate,
        )
        sensitivity_by_table: dict[str, SensitivityMap] = {}
        for tb in tables:
            slot = mctx.activate(tb.source.table_id)
            sensitivity_by_table[tb.source.table_name] = detect_table_sensitivity(tb, slot)

        if auto_mask_pii:
            from autoetl.contracts import ColumnOverride

            existing_keys = {(ov.table, ov.source_column) for ov in spec.columns}
            merged_cols = list(spec.columns)
            for t_name, smap in sensitivity_by_table.items():
                for assign in smap.assignments:
                    if assign.level not in {SensitivityLevel.L2, SensitivityLevel.L3}:
                        continue
                    key = (t_name, assign.column)
                    if key in existing_keys:
                        merged_cols = [
                            ov.model_copy(update={"masking": MaskingPolicy.MASK})
                            if (ov.table, ov.source_column) == key
                            and ov.masking is MaskingPolicy.NONE
                            else ov
                            for ov in merged_cols
                        ]
                    else:
                        merged_cols.append(
                            ColumnOverride(
                                table=t_name,
                                source_column=assign.column,
                                masking=MaskingPolicy.MASK,
                            )
                        )
            spec = spec.model_copy(update={"columns": merged_cols})

        overrides_json = spec.model_dump_json()

        load_plan, skipped_key_masking, masking_warnings = _plan_multi_table_load(
            tables,
            relations,
            purpose=resolved_purpose,
            decompose_strings=decompose_strings,
            mctx=mctx,
            overrides=spec,
            engine=norm_engine,
        )

        analysis_warnings: list[str] = []
        for tb in tables:
            analysis_warnings.extend(tb.warnings)
        analysis_warnings.extend(relations.notes)

        bundle = MultiTableAnalysisBundle(
            purpose=resolved_purpose,
            tables=tables,
            relations=relations,
            load_plan=load_plan,
            warnings=analysis_warnings,
        )

        selected_cand = next(
            c for c in load_plan.candidates if c.id == load_plan.selected_candidate_id
        )
        cand_table_by_name = {t.name: t for t in selected_cand.tables}
        bundle_by_name = {tb.source.table_name: tb for tb in tables}
        parent_pk_counts = {
            t.name: sum(1 for c in t.columns if c.is_primary_key) for t in selected_cand.tables
        }

        load_warnings = list(masking_warnings)
        # 건너뛴 키의 요청 정책은 격리 행을 만들 때도 적용될 수 있다.
        needs_hash_key = any(
            MaskingPolicy.HASH in tp.column_masking.values() for tp in load_plan.table_plans
        ) or (
            spec.orphan_policy is OrphanPolicy.QUARANTINE
            and any(
                MaskingPolicy.HASH in policies.values() for policies in skipped_key_masking.values()
            )
        )
        masking_key = None
        if needs_hash_key:
            masking_key, created = _prepare_masking_key(db_path, norm_engine)
            if created:
                load_warnings.append(
                    f"새 가리기 비밀 키를 만들었습니다 ({masking_key.path}). "
                    "`autoetl key export <파일>`로 백업해 두세요."
                )
            if masking_key.permission_warning:
                load_warnings.append(masking_key.permission_warning)

        adapter = _DbAdapter(db_path, norm_engine)
        outcomes: list[TableLoadOutcome] = []
        mgmt_created: list[str] = []

        try:
            adapter.begin()
            if masking_key is not None:
                _register_masking_key(adapter, masking_key.key_id, batch_id, loaded_at)

            # 1. 디폴트 관리 테이블 생성 (+ 구버전 DB 스냅샷 컬럼 진화)
            if spec.include_management_tables:
                mgmt_ddl = render_management_tables_ddl(norm_engine)
                for stmt in mgmt_ddl.split(";"):
                    s_clean = stmt.strip()
                    if s_clean:
                        adapter.execute(s_clean)
                _ensure_management_table_columns(adapter)
                mgmt_created = list(DEFAULT_MANAGEMENT_TABLES)

            # 2. 배치 레코드 선등록(FK 참조 대상: _etl_datasets.batch_id)
            if spec.include_management_tables:
                _insert_management_batch_record(
                    adapter,
                    batch_id=batch_id,
                    loaded_at=loaded_at,
                    purpose=resolved_purpose,
                    selected_candidate_id=load_plan.selected_candidate_id,
                    status="running",
                    table_count=len(load_plan.table_plans),
                    total_rows_read=0,
                    total_rows_loaded=0,
                    total_rows_quarantined=0,
                    overrides_json=overrides_json,
                    notes=notes,
                )

            total_read = 0
            total_loaded = 0
            total_quarantined = 0
            all_quarantine_rows: list[tuple[Any, ...]] = []
            reject_reason: str | None = None

            if norm_engine == "postgres":
                # 동시 적재·롤백 AB-BA 교착 방지를 위해 롤백과 동일한 테이블명 정렬 순서로 선잠금
                for lock_tbl in sorted({tp.target_table for tp in load_plan.table_plans}):
                    if adapter.table_exists(lock_tbl):
                        adapter.execute(f"LOCK TABLE {_qi(lock_tbl)} IN SHARE ROW EXCLUSIVE MODE")

            # 3. 위상 적재 순서(부모 -> 자식)대로 테이블 생성·진화·적재
            for tp in load_plan.table_plans:
                t_name = tp.target_table
                target_tbl = cand_table_by_name[t_name]
                tb = bundle_by_name[tp.source_table]
                slot = mctx.get_by_name(tp.source_table)
                src_sha = slot.source_sha256 or _sha256_path(slot.spec.load_path)
                schema_fp = compute_schema_fingerprint(tp)
                dataset_kind = classify_dataset_kind(t_name, selected_cand, relations, tp)
                dataset_id = (
                    f"ds_{t_name}_{batch_stamp.strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:6]}"
                )

                # 3a. 동일 파일 중복 적재 및 스키마 변경(드리프트) 감지
                duplicate_hit = False
                schema_drift = False
                if spec.include_management_tables and adapter.table_exists("_etl_datasets"):
                    p = adapter.placeholder
                    if skip_duplicate_files and not force_reload and src_sha:
                        dup_row = adapter.fetchone(
                            f'SELECT d."dataset_id" FROM "_etl_datasets" d '
                            f'JOIN "_etl_load_batches" b ON d."batch_id" = b."batch_id" '
                            f'WHERE d."target_table" = {p} AND d."source_sha256" = {p} '
                            f"AND b.\"status\" IN ('succeeded', 'partial') LIMIT 1",
                            (t_name, src_sha),
                        )
                        if dup_row is not None:
                            duplicate_hit = True
                            load_warnings.append(
                                f"`{t_name}` ({tb.source.display_name}): 동일 파일(SHA-256 일치)이 "
                                f"이미 적재되어 건너뛰었습니다(강제 재적재 시 force_reload=True)."
                            )

                    prev_fp_row = adapter.fetchone(
                        f'SELECT d."schema_fingerprint" FROM "_etl_datasets" d '
                        f'JOIN "_etl_load_batches" b ON d."batch_id" = b."batch_id" '
                        f'WHERE d."target_table" = {p} '
                        f"AND b.\"status\" IN ('succeeded', 'partial') "
                        f'ORDER BY d."loaded_at" DESC LIMIT 1',
                        (t_name,),
                    )
                    if prev_fp_row is not None and str(prev_fp_row[0]) != schema_fp:
                        schema_drift = True
                        load_warnings.append(
                            f"`{t_name}` 스키마 변경(드리프트) 감지: "
                            f"이전 지문 `{prev_fp_row[0]}` → 현재 `{schema_fp}`."
                        )

                if duplicate_hit:
                    outcomes.append(
                        TableLoadOutcome(
                            dataset_id=dataset_id,
                            target_table=t_name,
                            dataset_kind=dataset_kind,
                            source_name=tb.source.display_name,
                            source_sha256=src_sha,
                            schema_fingerprint=schema_fp,
                            increment_mode=tp.increment_mode,
                            rows_read=0,
                            rows_loaded=0,
                            rows_quarantined=0,
                            skipped_duplicate_file=True,
                            schema_drift_detected=schema_drift,
                            masked_columns=sorted(tp.column_masking.keys()),
                        )
                    )
                    continue

                # 3b. 업무 테이블 DDL 생성(없으면 생성, 이미 있으면 신규 컬럼 진화 추가)
                existed = adapter.table_exists(t_name)
                if not existed:
                    ddl_stmt = _build_business_table_ddl(
                        target_tbl,
                        tp,
                        parent_pk_counts,
                        engine=norm_engine,
                        include_audit_columns=spec.include_audit_columns,
                    )
                    adapter.execute(ddl_stmt)
                else:
                    existing_cols = set(adapter.existing_columns(t_name))
                    map_by_target = {m.target_column: m for m in tp.column_mappings}
                    for col in target_tbl.columns:
                        if col.name not in existing_cols:
                            dtype = _dialect_col_type(col, map_by_target[col.name], norm_engine)
                            adapter.execute(
                                f"ALTER TABLE {_qi(t_name)} ADD COLUMN {_qi(col.name)} {dtype}"
                            )
                    if spec.include_audit_columns:
                        audit_type = "VARCHAR" if norm_engine == "duckdb" else "TEXT"
                        for ac in AUDIT_COLUMNS:
                            if ac not in existing_cols:
                                adapter.execute(
                                    f"ALTER TABLE {_qi(t_name)} ADD COLUMN {_qi(ac)} {audit_type}"
                                )

                # 3c. 권고·확정 인덱스 생성
                for idx_i, idx_rec in enumerate(tp.recommended_indexes):
                    prefix = "uq" if idx_rec.unique else "idx"
                    idx_name = f"{prefix}_{t_name}_{'_'.join(idx_rec.columns)}_{idx_i}"
                    if len(idx_name) > 60:
                        identity = "\0".join((prefix, t_name, *idx_rec.columns))
                        suffix = "_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
                        idx_name = idx_name[: 60 - len(suffix)] + suffix
                    uq_kw = "UNIQUE " if idx_rec.unique else ""
                    idx_cols_sql = ", ".join(_qi(c) for c in idx_rec.columns)
                    adapter.execute(
                        f"CREATE {uq_kw}INDEX IF NOT EXISTS {_qi(idx_name)} "
                        f"ON {_qi(t_name)} ({idx_cols_sql})"
                    )

                # 3d. 실제 행 변환·검증·적재
                smap = sensitivity_by_table[tp.source_table]
                (
                    rows_read,
                    rows_loaded,
                    q_entries,
                    rows_updated,
                    pre_update_snapshots,
                    predecessor_batch_ids,
                    table_warnings,
                    err_msg,
                ) = _load_single_table(
                    adapter,
                    mctx=mctx,
                    bundle=tb,
                    target_table=target_tbl,
                    table_plan=tp,
                    skipped_key_masking=skipped_key_masking.get(t_name, {}),
                    smap=smap,
                    batch_id=batch_id,
                    dataset_id=dataset_id,
                    loaded_at=loaded_at,
                    include_audit_columns=spec.include_audit_columns,
                    orphan_policy=spec.orphan_policy,
                    table_existed_before=existed,
                    hash_key=masking_key.key if masking_key is not None else None,
                    capture_rollback_snapshots=(
                        spec.include_management_tables and spec.include_audit_columns
                    ),
                )

                if err_msg is not None:
                    reject_reason = err_msg
                    break

                load_warnings.extend(table_warnings)
                total_read += rows_read
                total_loaded += rows_loaded
                total_quarantined += len(q_entries)
                all_quarantine_rows.extend(q_entries)

                # 3e. 데이터셋 & 컬럼 사전 카탈로그 기록
                if spec.include_management_tables:
                    p = adapter.placeholder
                    ds_ph = ", ".join([p] * 20)
                    if pre_update_snapshots is None:
                        snapshots_json: str | None = None
                    else:
                        snapshots_json = json.dumps(pre_update_snapshots, ensure_ascii=False)
                    source_batches_json: str | None = (
                        None
                        if predecessor_batch_ids is None
                        else json.dumps(predecessor_batch_ids, ensure_ascii=False)
                    )
                    ds_loaded_at = _next_monotonic_dataset_timestamp(adapter, t_name)
                    adapter.execute(
                        f'INSERT INTO "_etl_datasets" ('
                        f'"dataset_id", "batch_id", "load_order_index", "target_table", '
                        f'"dataset_kind", "source_name", "source_format", "source_encoding", '
                        f'"source_sha256", "schema_fingerprint", "increment_mode", '
                        f'"primary_key_columns", "rows_read", "rows_loaded", "rows_quarantined", '
                        f'"schema_drift_detected", "loaded_at", '
                        f'"rows_updated", "rollback_snapshots_json", '
                        f'"snapshot_source_batches_json"'
                        f") VALUES ({ds_ph})",
                        (
                            dataset_id,
                            batch_id,
                            tp.load_order_index,
                            t_name,
                            dataset_kind.value,
                            tb.source.display_name,
                            tb.source.source_kind.value,
                            tp.source_encoding,
                            src_sha,
                            schema_fp,
                            tp.increment_mode.value,
                            json.dumps(tp.primary_key, ensure_ascii=False),
                            rows_read,
                            rows_loaded,
                            len(q_entries),
                            1 if schema_drift else 0,
                            ds_loaded_at,
                            rows_updated,
                            snapshots_json,
                            source_batches_json,
                        ),
                    )

                    cls_by_col = {c.column: c for c in tb.classifications}
                    s_assign_by_col = {a.column: a for a in smap.assignments}
                    col_by_name = {c.name: c for c in target_tbl.columns}
                    uq_set = set(tp.confirmed_unique_columns)
                    reg_rows: list[tuple[Any, ...]] = []
                    for col_ord, cm in enumerate(tp.column_mappings, start=1):
                        tcol = col_by_name[cm.target_column]
                        src_cls = cls_by_col.get(cm.source_column)
                        if tcol.is_identity:
                            role = "surrogate_key"
                        elif cm.transform.startswith("NULLIF(regexp_extract("):
                            role = "extracted_slot"
                        elif src_cls is not None:
                            role = src_cls.category.value
                        else:
                            role = "unknown"
                        s_assign = s_assign_by_col.get(cm.source_column)
                        pii_lvl = s_assign.level.value if s_assign is not None else "L0"
                        pii_types_json = json.dumps(
                            [st.value for st in s_assign.types] if s_assign is not None else [],
                            ensure_ascii=False,
                        )
                        mask_pol = tp.column_masking.get(cm.source_column, MaskingPolicy.NONE).value
                        reg_rows.append(
                            (
                                dataset_id,
                                t_name,
                                col_ord,
                                cm.source_column,
                                cm.target_column,
                                role,
                                cm.type_cast,
                                1 if tcol.is_primary_key else 0,
                                1 if (tcol.name in uq_set or tp.primary_key == [tcol.name]) else 0,
                                tcol.foreign_key,
                                cm.transform,
                                pii_lvl,
                                pii_types_json,
                                mask_pol,
                            )
                        )
                    reg_ph = ", ".join([p] * 14)
                    adapter.executemany(
                        f'INSERT INTO "_etl_column_registry" ('
                        f'"dataset_id", "target_table", "column_order", "source_column", '
                        f'"target_column", "data_role", "db_type", "is_primary_key", '
                        f'"is_unique", "foreign_key_ref", "transform_rule", "pii_level", '
                        f'"pii_types", "masking_policy"'
                        f") VALUES ({reg_ph})",
                        reg_rows,
                    )

                outcomes.append(
                    TableLoadOutcome(
                        dataset_id=dataset_id,
                        target_table=t_name,
                        dataset_kind=dataset_kind,
                        source_name=tb.source.display_name,
                        source_sha256=src_sha,
                        schema_fingerprint=schema_fp,
                        increment_mode=tp.increment_mode,
                        rows_read=rows_read,
                        rows_loaded=rows_loaded,
                        rows_quarantined=len(q_entries),
                        skipped_duplicate_file=False,
                        schema_drift_detected=schema_drift,
                        masked_columns=sorted(tp.column_masking.keys()),
                    )
                )

            # 4. REJECT_ALL 중단 시 전체 롤백 + 실패 배치 이력만 별도 감사 기록
            if reject_reason is not None:
                adapter.rollback()
                load_warnings.append(f"적재 중단(reject_all): {reject_reason}")
                if spec.include_management_tables:
                    adapter.begin()
                    mgmt_ddl = render_management_tables_ddl(norm_engine)
                    for stmt in mgmt_ddl.split(";"):
                        s_clean = stmt.strip()
                        if s_clean:
                            adapter.execute(s_clean)
                    _ensure_management_table_columns(adapter)
                    _insert_management_batch_record(
                        adapter,
                        batch_id=batch_id,
                        loaded_at=loaded_at,
                        purpose=resolved_purpose,
                        selected_candidate_id=load_plan.selected_candidate_id,
                        status="failed",
                        table_count=len(load_plan.table_plans),
                        total_rows_read=total_read,
                        total_rows_loaded=0,
                        total_rows_quarantined=0,
                        overrides_json=overrides_json,
                        notes=reject_reason,
                    )
                    adapter.commit()
                elapsed = round(time.perf_counter() - t0, 4)
                return bundle, DbLoadResult(
                    batch_id=batch_id,
                    loaded_at=loaded_at,
                    db_engine=norm_engine,
                    db_target=str(db_path),
                    status="failed",
                    purpose=resolved_purpose,
                    selected_candidate_id=load_plan.selected_candidate_id,
                    management_tables_created=mgmt_created,
                    tables=[],
                    total_rows_read=total_read,
                    total_rows_loaded=0,
                    total_rows_quarantined=0,
                    elapsed_seconds=elapsed,
                    warnings=load_warnings,
                )

            # 5. 격리 행 기록 및 최종 배치 상태 갱신
            if spec.include_management_tables and all_quarantine_rows:
                p = adapter.placeholder
                if norm_engine == "duckdb":
                    next_qid = _next_quarantine_id(adapter)
                    q_with_id = [(next_qid + i, *row) for i, row in enumerate(all_quarantine_rows)]
                    q_ph = ", ".join([p] * 10)
                    adapter.executemany(
                        f'INSERT INTO "_etl_quarantine_rows" ('
                        f'"quarantine_id", "batch_id", "dataset_id", "target_table", '
                        f'"source_row_number", "error_category", "error_column", '
                        f'"error_detail", "raw_row_json", "quarantined_at"'
                        f") VALUES ({q_ph})",
                        q_with_id,
                    )
                else:
                    q_ph = ", ".join([p] * 9)
                    adapter.executemany(
                        f'INSERT INTO "_etl_quarantine_rows" ('
                        f'"batch_id", "dataset_id", "target_table", "source_row_number", '
                        f'"error_category", "error_column", "error_detail", '
                        f'"raw_row_json", "quarantined_at"'
                        f") VALUES ({q_ph})",
                        all_quarantine_rows,
                    )

            if outcomes and all(o.skipped_duplicate_file for o in outcomes):
                final_status = "skipped_duplicate"
            elif total_quarantined > 0:
                final_status = "partial"
                load_warnings.append(
                    f"변환·제약 오류 행 {total_quarantined}건을 `_etl_quarantine_rows` 격리함에 "
                    "분리 저장했습니다."
                )
            else:
                final_status = "succeeded"

            if spec.include_management_tables:
                p = adapter.placeholder
                final_notes = notes or ("; ".join(load_warnings) if load_warnings else "")
                adapter.execute(
                    f'UPDATE "_etl_load_batches" '
                    f'SET "status" = {p}, "total_rows_read" = {p}, '
                    f'"total_rows_loaded" = {p}, "total_rows_quarantined" = {p}, '
                    f'"notes" = {p} WHERE "batch_id" = {p}',
                    (
                        final_status,
                        total_read,
                        total_loaded,
                        total_quarantined,
                        final_notes,
                        batch_id,
                    ),
                )

            adapter.commit()
        except Exception:
            adapter.rollback()
            raise
        finally:
            adapter.close()

    elapsed = round(time.perf_counter() - t0, 4)
    return bundle, DbLoadResult(
        batch_id=batch_id,
        loaded_at=loaded_at,
        db_engine=norm_engine,
        db_target=str(db_path),
        status=final_status,
        purpose=resolved_purpose,
        selected_candidate_id=load_plan.selected_candidate_id,
        management_tables_created=mgmt_created,
        tables=outcomes,
        total_rows_read=total_read,
        total_rows_loaded=total_loaded,
        total_rows_quarantined=total_quarantined,
        elapsed_seconds=elapsed,
        warnings=load_warnings,
    )


def inspect_db_catalog(
    db_path: str | Path,
    *,
    engine: str = "sqlite",
    limit: int = 50,
) -> dict[str, Any]:
    """적재된 DB의 기본 데이터 관리 테이블(배치·데이터셋·컬럼 사전·격리함)과 표 현황을 조회한다."""
    norm_engine = _normalize_engine(engine)
    adapter = _DbAdapter(db_path, norm_engine)
    try:
        p = adapter.placeholder
        all_tables = adapter.list_user_tables()
        mgmt_set = {*DEFAULT_MANAGEMENT_TABLES, _MASKING_KEY_TABLE}
        business_tables: list[dict[str, Any]] = []
        for tbl in all_tables:
            if tbl in mgmt_set:
                continue
            cnt_row = adapter.fetchone(f"SELECT COUNT(*) FROM {_qi(tbl)}")
            business_tables.append(
                {
                    "table": tbl,
                    "row_count": int(cnt_row[0]) if cnt_row is not None else 0,
                    "columns": adapter.existing_columns(tbl),
                }
            )

        batches: list[dict[str, Any]] = []
        if adapter.table_exists("_etl_load_batches"):
            rows = adapter.fetchall(
                f'SELECT "batch_id", "loaded_at", "purpose", "selected_candidate_id", '
                f'"db_engine", "status", "table_count", "total_rows_read", '
                f'"total_rows_loaded", "total_rows_quarantined", "notes" '
                f'FROM "_etl_load_batches" ORDER BY "loaded_at" DESC LIMIT {p}',
                (limit,),
            )
            for r in rows:
                batches.append(
                    {
                        "batch_id": r[0],
                        "loaded_at": r[1],
                        "purpose": r[2],
                        "selected_candidate_id": r[3],
                        "db_engine": r[4],
                        "status": r[5],
                        "table_count": int(r[6]),
                        "total_rows_read": int(r[7]),
                        "total_rows_loaded": int(r[8]),
                        "total_rows_quarantined": int(r[9]),
                        "notes": r[10],
                    }
                )

        datasets: list[dict[str, Any]] = []
        if adapter.table_exists("_etl_datasets"):
            ds_cols = set(adapter.existing_columns("_etl_datasets"))
            has_rows_updated = "rows_updated" in ds_cols
            extra_sel = ', "rows_updated"' if has_rows_updated else ""
            rows = adapter.fetchall(
                f'SELECT "dataset_id", "batch_id", "load_order_index", "target_table", '
                f'"dataset_kind", "source_name", "source_format", "source_encoding", '
                f'"source_sha256", "schema_fingerprint", "increment_mode", "primary_key_columns", '
                f'"rows_read", "rows_loaded", "rows_quarantined", '
                f'"schema_drift_detected", "loaded_at"{extra_sel} '
                f'FROM "_etl_datasets" ORDER BY "loaded_at" DESC, "load_order_index" ASC '
                f"LIMIT {p}",
                (limit,),
            )
            for r in rows:
                kind_str = str(r[4])
                try:
                    kind_ko = _DATASET_KIND_LABELS_KO[DatasetKind(kind_str)]
                except ValueError:
                    kind_ko = kind_str
                datasets.append(
                    {
                        "dataset_id": r[0],
                        "batch_id": r[1],
                        "load_order_index": int(r[2]),
                        "target_table": r[3],
                        "dataset_kind": kind_str,
                        "dataset_kind_label": kind_ko,
                        "source_name": r[5],
                        "source_format": r[6],
                        "source_encoding": r[7],
                        "source_sha256": r[8],
                        "schema_fingerprint": r[9],
                        "increment_mode": r[10],
                        "primary_key_columns": json.loads(str(r[11])) if r[11] else [],
                        "rows_read": int(r[12]),
                        "rows_loaded": int(r[13]),
                        "rows_quarantined": int(r[14]),
                        "rows_updated": int(r[17]) if has_rows_updated and r[17] is not None else 0,
                        "schema_drift_detected": bool(r[15]),
                        "loaded_at": r[16],
                    }
                )

        columns: list[dict[str, Any]] = []
        if adapter.table_exists("_etl_column_registry"):
            rows = adapter.fetchall(
                f'SELECT "dataset_id", "target_table", "column_order", "source_column", '
                f'"target_column", "data_role", "db_type", "is_primary_key", '
                f'"is_unique", "foreign_key_ref", "transform_rule", "pii_level", '
                f'"pii_types", "masking_policy" '
                f'FROM "_etl_column_registry" ORDER BY "dataset_id" DESC, "column_order" ASC '
                f"LIMIT {p}",
                (limit * 10,),
            )
            for r in rows:
                columns.append(
                    {
                        "dataset_id": r[0],
                        "target_table": r[1],
                        "column_order": int(r[2]),
                        "source_column": r[3],
                        "target_column": r[4],
                        "data_role": r[5],
                        "db_type": r[6],
                        "is_primary_key": bool(r[7]),
                        "is_unique": bool(r[8]),
                        "foreign_key_ref": r[9],
                        "transform_rule": r[10],
                        "pii_level": r[11],
                        "pii_types": json.loads(str(r[12])) if r[12] else [],
                        "masking_policy": r[13],
                    }
                )

        quarantine: list[dict[str, Any]] = []
        if adapter.table_exists("_etl_quarantine_rows"):
            rows = adapter.fetchall(
                f'SELECT "quarantine_id", "batch_id", "dataset_id", "target_table", '
                f'"source_row_number", "error_category", "error_column", '
                f'"error_detail", "raw_row_json", "quarantined_at" '
                f'FROM "_etl_quarantine_rows" ORDER BY "quarantine_id" DESC LIMIT {p}',
                (limit,),
            )
            for r in rows:
                quarantine.append(
                    {
                        "quarantine_id": int(r[0]),
                        "batch_id": r[1],
                        "dataset_id": r[2],
                        "target_table": r[3],
                        "source_row_number": int(r[4]),
                        "error_category": r[5],
                        "error_column": r[6],
                        "error_detail": r[7],
                        "raw_row_json": r[8],
                        "quarantined_at": r[9],
                    }
                )

        return {
            "db_engine": norm_engine,
            "db_target": str(db_path),
            "management_tables": [t for t in DEFAULT_MANAGEMENT_TABLES if t in set(all_tables)],
            "business_tables": business_tables,
            "batches": batches,
            "datasets": datasets,
            "columns": columns,
            "quarantine": quarantine,
        }
    finally:
        adapter.close()


def rollback_batch(
    db_path: str | Path,
    batch_id: str,
    *,
    engine: str = "sqlite",
) -> dict[str, Any]:
    """지정 ``batch_id``의 신규 적재 행은 역순 삭제하고, UPSERT로 갱신된 기존 행은 원복한다."""
    norm_engine = _normalize_engine(engine)
    adapter = _DbAdapter(db_path, norm_engine)
    try:
        p = adapter.placeholder
        if not adapter.table_exists("_etl_load_batches") or not adapter.table_exists(
            "_etl_datasets"
        ):
            raise InputError(
                "데이터 관리 테이블(_etl_load_batches/_etl_datasets)이 없어 롤백할 수 없습니다."
            )

        batch_row = adapter.fetchone(
            f'SELECT "status" FROM "_etl_load_batches" WHERE "batch_id" = {p}',
            (batch_id,),
        )
        if batch_row is None:
            raise InputError(f"존재하지 않는 batch_id입니다: {batch_id}")
        if str(batch_row[0]) == "rolled_back":
            raise InputError(f"이미 롤백된 배치입니다: {batch_id}")

        ds_cols = set(adapter.existing_columns("_etl_datasets"))
        has_snapshot_col = "rollback_snapshots_json" in ds_cols
        has_src_batches_col = "snapshot_source_batches_json" in ds_cols
        has_rows_updated_col = "rows_updated" in ds_cols
        snap_select = '"rollback_snapshots_json"' if has_snapshot_col else "NULL"

        ds_rows = adapter.fetchall(
            f'SELECT "target_table", "increment_mode", "primary_key_columns", '
            f'"loaded_at", {snap_select} '
            f'FROM "_etl_datasets" WHERE "batch_id" = {p} '
            f'ORDER BY "load_order_index" DESC',
            (batch_id,),
        )

        # 위상 적재 역순(자식 테이블 먼저, 부모 테이블 나중)으로 롤백 계획을 수립·사전 검증한다.
        adapter.begin()
        if norm_engine == "postgres":
            # 동시 PostgreSQL 적재·롤백과 동일한 테이블명 정렬 순서로 잠금(AB-BA 교착 방지)
            for lock_tbl in sorted({str(r[0]) for r in ds_rows}):
                if adapter.table_exists(lock_tbl):
                    adapter.execute(f"LOCK TABLE {_qi(lock_tbl)} IN SHARE ROW EXCLUSIVE MODE")
            # 잠금 대기 중 다른 롤백이 먼저 커밋했을 수 있으므로 행 잠금 후 상태를 재확인한다.
            locked_status = adapter.fetchone(
                f'SELECT "status" FROM "_etl_load_batches" WHERE "batch_id" = {p} FOR UPDATE',
                (batch_id,),
            )
            if locked_status is not None and str(locked_status[0]) == "rolled_back":
                raise InputError(f"이미 롤백된 배치입니다: {batch_id}")

        rollback_plans: list[
            tuple[str, list[str], list[str], list[tuple[str, dict[str, Any]]], bool]
        ] = []
        seen_tables: set[str] = set()
        for tbl_raw, inc_mode_raw, pk_json_raw, target_loaded_at_raw, snap_raw in ds_rows:
            tbl = str(tbl_raw)
            if tbl in seen_tables or not adapter.table_exists(tbl):
                continue
            seen_tables.add(tbl)
            writable_cols = adapter.writable_columns(tbl)
            if "_etl_batch_id" not in writable_cols:
                raise InputError(
                    f"`{tbl}` 테이블에 `_etl_batch_id` 이력 컬럼이 없어 "
                    "배치 롤백을 수행할 수 없습니다."
                )

            inc_mode = str(inc_mode_raw or "")
            pk_cols: list[str] = (
                [str(c) for c in json.loads(str(pk_json_raw))] if pk_json_raw else []
            )

            # 1) 스냅샷이 없는 UPSERT/WATERMARK 배치는 신규 삽입 여부를 보장할 수 없으므로 차단
            if snap_raw is None and inc_mode in {
                IncrementMode.UPSERT.value,
                IncrementMode.WATERMARK.value,
            }:
                raise InputError(
                    f"`{tbl}` 테이블의 UPSERT/WATERMARK 적재에 변경 전 스냅샷이 없어 "
                    f"배치 `{batch_id}`를 안전하게 롤백할 수 없습니다."
                )

            # 2) 후속 활성 배치가 이 배치의 행을 갱신한 경우 먼저 롤백하면 좀비 행이 되므로 차단
            if has_src_batches_col:
                later_meta_rows = adapter.fetchall(
                    f'SELECT d."batch_id", d."snapshot_source_batches_json" '
                    f'FROM "_etl_datasets" d '
                    f'JOIN "_etl_load_batches" b ON d."batch_id" = b."batch_id" '
                    f'WHERE d."target_table" = {p} AND d."batch_id" != {p} '
                    f"AND b.\"status\" IN ('succeeded', 'partial') "
                    f'AND d."snapshot_source_batches_json" IS NOT NULL '
                    f"AND d.\"snapshot_source_batches_json\" != '[]'",
                    (tbl, batch_id),
                )
                for later_batch_id, src_batches_raw in later_meta_rows:
                    if batch_id in json.loads(str(src_batches_raw)):
                        raise InputError(
                            f"`{tbl}` 테이블의 행이 후속 배치(`{later_batch_id}`)에 의해 갱신되어 "
                            f"배치 `{batch_id}`를 먼저 롤백할 수 없습니다. "
                            f"후속 배치(`{later_batch_id}`)를 먼저 롤백해 주세요."
                        )
            if has_snapshot_col:
                extra_filter = (
                    'AND d."snapshot_source_batches_json" IS NULL ' if has_src_batches_col else ""
                )
                later_rows = adapter.fetchall(
                    f'SELECT d."batch_id", d."rollback_snapshots_json" '
                    f'FROM "_etl_datasets" d '
                    f'JOIN "_etl_load_batches" b ON d."batch_id" = b."batch_id" '
                    f'WHERE d."target_table" = {p} AND d."batch_id" != {p} '
                    f"AND b.\"status\" IN ('succeeded', 'partial') "
                    f"{extra_filter}"
                    f'AND d."rollback_snapshots_json" IS NOT NULL '
                    f"AND d.\"rollback_snapshots_json\" != '[]'",
                    (tbl, batch_id),
                )
                for later_batch_id, later_snaps_raw in later_rows:
                    later_snaps = json.loads(str(later_snaps_raw))
                    if any(
                        isinstance(s, dict)
                        and _SNAPSHOT_OP_KEY not in s
                        and s.get("_etl_batch_id") == batch_id
                        for s in later_snaps
                    ):
                        raise InputError(
                            f"`{tbl}` 테이블의 행이 후속 배치(`{later_batch_id}`)에 의해 갱신되어 "
                            f"배치 `{batch_id}`를 먼저 롤백할 수 없습니다. "
                            f"후속 배치(`{later_batch_id}`)를 먼저 롤백해 주세요."
                        )
            # 스냅샷·선행 배치 목록이 모두 없는 후속 갱신 배치(rows_updated > 0)는
            # 커밋 순서를 보장하는 단조 증가 loaded_at 기준으로 이후 배치만 차단한다.
            target_loaded_at = str(target_loaded_at_raw or "")
            ts_op = ">" if "." in target_loaded_at else ">="
            unknown_upd_filter = (
                'AND d."rows_updated" > 0 '
                if has_rows_updated_col
                else "AND d.\"increment_mode\" IN ('upsert', 'watermark') "
            )
            snap_null_filter = (
                'AND d."rollback_snapshots_json" IS NULL ' if has_snapshot_col else ""
            )
            src_null_filter = (
                'AND d."snapshot_source_batches_json" IS NULL ' if has_src_batches_col else ""
            )
            unknown_later_rows = adapter.fetchall(
                f'SELECT d."batch_id" FROM "_etl_datasets" d '
                f'JOIN "_etl_load_batches" b ON d."batch_id" = b."batch_id" '
                f'WHERE d."target_table" = {p} AND d."batch_id" != {p} '
                f"AND b.\"status\" IN ('succeeded', 'partial') "
                f'AND d."loaded_at" {ts_op} {p} '
                f"{unknown_upd_filter}{snap_null_filter}{src_null_filter}",
                (tbl, batch_id, target_loaded_at),
            )
            if unknown_later_rows:
                later_bid = str(unknown_later_rows[0][0])
                raise InputError(
                    f"`{tbl}` 테이블에 스냅샷·선행 배치 정보가 없는 후속 갱신 배치"
                    f"(`{later_bid}`)가 있어 배치 `{batch_id}`를 안전하게 롤백할 수 없습니다."
                )

            rollback_ops: list[tuple[str, dict[str, Any]]] = []
            has_delete_ops = False
            if snap_raw is not None:
                parsed_snaps = json.loads(str(snap_raw))
                for raw_snap in parsed_snaps:
                    if not isinstance(raw_snap, dict):
                        continue
                    if (
                        raw_snap.get(_SNAPSHOT_OP_KEY) == "delete"
                        and set(raw_snap.keys()) == {_SNAPSHOT_OP_KEY, "pk"}
                        and isinstance(raw_snap.get("pk"), dict)
                    ):
                        raw_pk = raw_snap["pk"]
                        has_delete_ops = True
                        rollback_ops.append(
                            (
                                "delete",
                                {str(k): _decode_snapshot_value(v) for k, v in raw_pk.items()},
                            )
                        )
                    else:
                        rollback_ops.append(
                            (
                                "restore",
                                {str(k): _decode_snapshot_value(v) for k, v in raw_snap.items()},
                            )
                        )

            decoded_snapshots = [op_d for op_kind, op_d in rollback_ops if op_kind == "restore"]

            # 3) 스냅샷·연산 대상 행이 현재도 이 배치 소유인지 청크 조회로 일괄 확인
            if rollback_ops:
                if not pk_cols or any(c not in writable_cols for c in pk_cols):
                    raise InputError(
                        f"`{tbl}` 테이블의 기본 키 메타데이터가 현재 스키마와 일치하지 않아 "
                        f"배치 `{batch_id}`의 갱신 행을 복원할 수 없습니다."
                    )
                op_pks = [tuple(op_d[c] for c in pk_cols) for _, op_d in rollback_ops]
                pk_sel = ", ".join(_qi(c) for c in pk_cols)
                current_owner_by_pk: dict[tuple[Any, ...], str] = {}
                chunk_size = max(1, _PK_CHUNK_PARAM_LIMIT // len(pk_cols))
                for start in range(0, len(op_pks), chunk_size):
                    chunk = op_pks[start : start + chunk_size]
                    where_chunk, pk_params = _pk_predicate_and_params(pk_cols, chunk, p)
                    owner_rows = adapter.fetchall(
                        f'SELECT {pk_sel}, "_etl_batch_id" FROM {_qi(tbl)} WHERE {where_chunk}',
                        pk_params,
                    )
                    for o_row in owner_rows:
                        norm_pk = tuple(_normalize_key_for_lookup(v) for v in o_row[:-1])
                        current_owner_by_pk[norm_pk] = str(o_row[-1])
                where_one_pk = " AND ".join(f"{_qi(c)} = {p}" for c in pk_cols)
                for op_kind, op_d in rollback_ops:
                    norm_pk = tuple(_normalize_key_for_lookup(op_d[c]) for c in pk_cols)
                    owner = current_owner_by_pk.get(norm_pk)
                    if owner is None:
                        # SQLite NUMERIC/COLLATE 등 DB 동등성과 Python 비교가 다를 때 단건 DB 재확인
                        single_row = adapter.fetchone(
                            f'SELECT "_etl_batch_id" FROM {_qi(tbl)} WHERE {where_one_pk} LIMIT 1',
                            [op_d[c] for c in pk_cols],
                        )
                        if single_row is not None and single_row[0] is not None:
                            owner = str(single_row[0])
                    target_label = "갱신 대상 행" if op_kind == "restore" else "신규 삽입 행"
                    if owner is None:
                        raise InputError(
                            f"`{tbl}` 테이블의 {target_label}이 이미 삭제되어 "
                            f"배치 `{batch_id}`를 롤백할 수 없습니다."
                        )
                    if owner != batch_id:
                        raise InputError(
                            f"`{tbl}` 테이블의 {target_label}이 후속 배치(`{owner}`)에 의해 "
                            f"변경되어 배치 `{batch_id}`를 롤백할 수 없습니다. "
                            "후속 배치를 먼저 롤백해 주세요."
                        )

                # 구버전·폴백 스냅샷(삽입 마커 없음)은 저비용 자기참조 FK 여부를 먼저 검사하고,
                # 참일 때만 미기록 삽입 행 존재 여부를 확인해 불필요한 전체 PK 조회를 피한다.
                if (
                    decoded_snapshots
                    and not has_delete_ops
                    and adapter.has_self_referencing_fk(tbl)
                ):
                    snap_pk_set = {
                        tuple(_normalize_key_for_lookup(snap[c]) for c in pk_cols)
                        for snap in decoded_snapshots
                    }
                    batch_pk_rows = adapter.fetchall(
                        f'SELECT {pk_sel} FROM {_qi(tbl)} WHERE "_etl_batch_id" = {p}',
                        (batch_id,),
                    )
                    has_unrecorded_inserts = any(
                        tuple(_normalize_key_for_lookup(v) for v in row) not in snap_pk_set
                        for row in batch_pk_rows
                    )
                    if has_unrecorded_inserts:
                        raise InputError(
                            f"`{tbl}` 테이블에 자기참조 외래 키가 있고 배치 `{batch_id}`의 "
                            "삽입·갱신 실행 순서 정보가 없어 안전하게 롤백할 수 없습니다."
                        )

            rollback_plans.append((tbl, pk_cols, writable_cols, rollback_ops, has_delete_ops))

        deleted_by_table: dict[str, int] = {}
        restored_by_table: dict[str, int] = {}
        total_deleted = 0
        total_restored = 0

        for tbl, pk_cols, writable_cols, rollback_ops, has_delete_ops in rollback_plans:
            n_del = 0
            n_restored = 0
            if not rollback_ops:
                cnt_row = adapter.fetchone(
                    f'SELECT COUNT(*) FROM {_qi(tbl)} WHERE "_etl_batch_id" = {p}',
                    (batch_id,),
                )
                n_del = int(cnt_row[0]) if cnt_row is not None else 0
                try:
                    adapter.execute(
                        f'DELETE FROM {_qi(tbl)} WHERE "_etl_batch_id" = {p}',
                        (batch_id,),
                    )
                except Exception as exc:
                    if _is_integrity_error(exc, norm_engine):
                        raise InputError(
                            f"`{tbl}` 테이블의 행을 다른 자식 테이블이 참조하고 있어 "
                            f"배치 `{batch_id}`를 롤백할 수 없습니다. "
                            "참조하는 자식 배치를 먼저 롤백해 주세요."
                        ) from exc
                    raise
                leftover_row = adapter.fetchone(
                    f'SELECT 1 FROM {_qi(tbl)} WHERE "_etl_batch_id" = {p} LIMIT 1',
                    (batch_id,),
                )
                if leftover_row is not None:
                    raise InputError(
                        f"`{tbl}` 테이블의 배치 `{batch_id}` 행이 삭제 후에도 남아 있어 "
                        "롤백을 중단했습니다."
                    )
            else:
                col_types = adapter.column_data_types(tbl)
                has_self_fk = adapter.has_self_referencing_fk(tbl)
                pk_set = set(pk_cols)
                where_pk = " AND ".join(f"{_qi(c)} = {p}" for c in pk_cols)

                def _exec_delete_pks(
                    pk_tuples_to_del: Sequence[tuple[Any, ...]],
                    _tbl: str = tbl,
                    _pk_cols: list[str] = pk_cols,
                    _has_self_fk: bool = has_self_fk,
                ) -> int:
                    if not pk_tuples_to_del:
                        return 0
                    # 자기참조 FK 테이블은 역순 개별 삭제로 RESTRICT/CASCADE 순서를 엄격 보장
                    c_size = 1 if _has_self_fk else max(1, _PK_CHUNK_PARAM_LIMIT // len(_pk_cols))
                    for st in range(0, len(pk_tuples_to_del), c_size):
                        chunk = pk_tuples_to_del[st : st + c_size]
                        where_pk_chunk, pk_params = _pk_predicate_and_params(_pk_cols, chunk, p)
                        try:
                            adapter.execute(
                                f'DELETE FROM {_qi(_tbl)} WHERE "_etl_batch_id" = {p} '
                                f"AND {where_pk_chunk}",
                                [batch_id, *pk_params],
                            )
                        except Exception as exc:
                            if _is_integrity_error(exc, norm_engine):
                                raise InputError(
                                    f"`{_tbl}` 테이블의 신규 행을 다른 자식 테이블이 참조하고 있어 "
                                    f"배치 `{batch_id}`를 롤백할 수 없습니다. "
                                    "참조하는 자식 배치를 먼저 롤백해 주세요."
                                ) from exc
                            raise
                        leftover_del = adapter.fetchone(
                            f'SELECT 1 FROM {_qi(_tbl)} WHERE "_etl_batch_id" = {p} '
                            f"AND {where_pk_chunk} LIMIT 1",
                            [batch_id, *pk_params],
                        )
                        if leftover_del is not None:
                            raise InputError(
                                f"`{_tbl}` 테이블의 삭제 대상 신규 행이 삭제 후에도 남아 있어 "
                                f"배치 `{batch_id}` 롤백을 중단했습니다."
                            )
                    return len(pk_tuples_to_del)

                def _exec_restore_run(
                    restore_sig: tuple[tuple[str, str | None], ...],
                    batch_params: list[list[Any]],
                    _tbl: str = tbl,
                    _where_pk: str = where_pk,
                ) -> int:
                    set_sql = ", ".join(
                        f"{_qi(c)} = CAST({p} AS {pg_cast})" if pg_cast else f"{_qi(c)} = {p}"
                        for c, pg_cast in restore_sig
                    )
                    update_sql = (
                        f"UPDATE {_qi(_tbl)} SET {set_sql} "
                        f'WHERE {_where_pk} AND "_etl_batch_id" = {p}'
                    )
                    try:
                        affected = adapter.execute_updates_checked(update_sql, batch_params)
                    except Exception as exc:
                        if _is_integrity_error(exc, norm_engine):
                            raise InputError(
                                f"`{_tbl}` 테이블의 기존 행 복원 중 무결성 제약 충돌이 발생하여 "
                                f"배치 `{batch_id}`를 롤백할 수 없습니다."
                            ) from exc
                        raise
                    if affected != len(batch_params):
                        raise InputError(
                            f"`{_tbl}` 테이블의 기존 행 복원 대상({len(batch_params)}건) 중 "
                            f"{affected}건만 반영되어 배치 `{batch_id}` 롤백을 중단했습니다."
                        )
                    return affected

                def _build_restore_spec(
                    snap: dict[str, Any],
                    _writable: list[str] = writable_cols,
                    _pk_set: set[str] = pk_set,
                    _pk_cols: list[str] = pk_cols,
                    _col_types: dict[str, str] = col_types,
                ) -> tuple[tuple[tuple[str, str | None], ...], list[Any]] | None:
                    r_cols = tuple(c for c in _writable if c not in _pk_set and c in snap)
                    if not r_cols:
                        return None
                    r_sig = tuple(
                        (
                            c,
                            _PG_TEXT_CAST_TYPES.get(_col_types.get(c, ""))
                            if norm_engine == "postgres"
                            and _col_types.get(c, "") in _PG_TEXT_CAST_TYPES
                            and (isinstance(snap[c], _PgTypedText) or snap[c] is None)
                            else None,
                        )
                        for c in r_cols
                    )
                    params = (
                        [
                            _bind_restore_value(snap[c], _col_types.get(c, ""), norm_engine)
                            for c in r_cols
                        ]
                        + [
                            _bind_restore_value(snap[c], _col_types.get(c, ""), norm_engine)
                            for c in _pk_cols
                        ]
                        + [batch_id]
                    )
                    return r_sig, params

                if not has_delete_ops:
                    # 구버전 스냅샷(삽입 마커 없음): 신규 삽입 행을 먼저 삭제 후 역순 복원
                    recorded_pk_set = {
                        tuple(_normalize_key_for_lookup(op_d[c]) for c in pk_cols)
                        for _, op_d in rollback_ops
                    }
                    pk_sel = ", ".join(_qi(c) for c in pk_cols)
                    batch_pk_rows = adapter.fetchall(
                        f'SELECT {pk_sel} FROM {_qi(tbl)} WHERE "_etl_batch_id" = {p}',
                        (batch_id,),
                    )
                    inserted_pks = [
                        row
                        for row in batch_pk_rows
                        if tuple(_normalize_key_for_lookup(v) for v in row) not in recorded_pk_set
                    ]
                    n_del += _exec_delete_pks(inserted_pks)

                # 기록된 삽입·갱신 연산을 실제 실행의 정확한 역순으로 되돌린다.
                pending_deletes: list[tuple[Any, ...]] = []
                pending_sig: tuple[tuple[str, str | None], ...] | None = None
                pending_restores: list[list[Any]] = []

                for op_kind, op_d in reversed(rollback_ops):
                    if op_kind == "delete":
                        if pending_sig is not None and pending_restores:
                            n_restored += _exec_restore_run(pending_sig, pending_restores)
                            pending_sig = None
                            pending_restores = []
                        pending_deletes.append(tuple(op_d[c] for c in pk_cols))
                    else:
                        if pending_deletes:
                            n_del += _exec_delete_pks(pending_deletes)
                            pending_deletes = []
                        spec_row = _build_restore_spec(op_d)
                        if spec_row is None:
                            continue
                        r_sig, row_params = spec_row
                        if pending_sig == r_sig:
                            pending_restores.append(row_params)
                        else:
                            if pending_sig is not None and pending_restores:
                                n_restored += _exec_restore_run(pending_sig, pending_restores)
                            pending_sig = r_sig
                            pending_restores = [row_params]

                if pending_deletes:
                    n_del += _exec_delete_pks(pending_deletes)
                if pending_sig is not None and pending_restores:
                    n_restored += _exec_restore_run(pending_sig, pending_restores)

            deleted_by_table[tbl] = n_del
            restored_by_table[tbl] = n_restored
            total_deleted += n_del
            total_restored += n_restored

        adapter.execute(
            f'UPDATE "_etl_load_batches" SET "status" = \'rolled_back\' WHERE "batch_id" = {p}',
            (batch_id,),
        )
        adapter.commit()
        return {
            "batch_id": batch_id,
            "status": "rolled_back",
            "deleted_by_table": deleted_by_table,
            "total_deleted": total_deleted,
            "restored_by_table": restored_by_table,
            "total_restored": total_restored,
        }
    except Exception:
        adapter.rollback()
        raise
    finally:
        adapter.close()


__all__ = [
    "AUDIT_COLUMNS",
    "DEFAULT_MANAGEMENT_TABLES",
    "classify_dataset_kind",
    "compute_schema_fingerprint",
    "inspect_db_catalog",
    "load_tables",
    "mask_value",
    "render_management_tables_ddl",
    "rollback_batch",
]
