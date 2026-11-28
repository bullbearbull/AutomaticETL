"""Reporting for requested value protection on ETL key columns."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from datetime import date
from pathlib import Path

import duckdb
import pytest

from autoetl.contracts import ColumnOverride, MaskingPolicy, SchemaOverrideSpec
from autoetl.core.db_loader import inspect_db_catalog, load_tables
from autoetl.core.load_planner import plan_multi_table_load
from autoetl.core.multi_analyze import analyze_tables
from autoetl.core.multi_context import NamedTableInput


def _numeric_members() -> tuple[NamedTableInput, SchemaOverrideSpec]:
    return (
        NamedTableInput(
            name="members",
            headers=("member_no",),
            rows=(("900001",), ("900002",), ("900003",)),
        ),
        SchemaOverrideSpec(
            columns=[
                ColumnOverride(
                    table="members",
                    source_column="member_no",
                    masking=MaskingPolicy.HASH,
                )
            ]
        ),
    )


def _assert_key_warning(warnings: Sequence[str], table: str, column: str) -> None:
    (warning,) = [
        entry
        for entry in warnings
        if table in entry and column in entry and "키" in entry and "가리기" in entry
    ]
    assert "원래 값" in warning
    assert "문자열" in warning


def _fetch_rows(db_path: Path, engine: str, query: str) -> list[tuple[object, ...]]:
    if engine == "sqlite":
        with closing(sqlite3.connect(db_path)) as db:
            return db.execute(query).fetchall()
    with duckdb.connect(str(db_path), read_only=True) as db:
        return db.execute(query).fetchall()


def _hash(value: str) -> str:
    return "h_" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


@pytest.mark.parametrize("engine", ["sqlite", "duckdb"])
def test_numeric_primary_key_keeps_values_and_reports_no_effective_hash(
    tmp_path: Path, engine: str
) -> None:
    source, overrides = _numeric_members()
    db_path = tmp_path / f"members.{engine}"

    bundle, result = load_tables(
        extra_tables=[source], db_path=db_path, engine=engine, overrides=overrides
    )

    assert result.status == "succeeded"
    values = _fetch_rows(db_path, engine, "SELECT member_no FROM members ORDER BY member_no")
    assert values == [(900001,), (900002,), (900003,)]

    catalog = inspect_db_catalog(db_path, engine=engine)
    (column,) = catalog["columns"]
    assert column["is_primary_key"] is True
    assert column["masking_policy"] == "none"
    assert result.tables[0].masked_columns == []
    assert bundle.load_plan.table_plans[0].column_masking == {}
    _assert_key_warning(bundle.warnings, "members", "member_no")
    _assert_key_warning(result.warnings, "members", "member_no")
    for (raw,) in source.rows:
        assert all(raw not in warning for warning in [*bundle.warnings, *result.warnings])


def test_analysis_reports_skipped_numeric_primary_key_hash_once() -> None:
    source, overrides = _numeric_members()

    bundle = analyze_tables(extra_tables=[source], overrides=overrides)

    _assert_key_warning(bundle.warnings, "members", "member_no")
    assert bundle.load_plan.table_plans[0].column_masking == {}
    for (raw,) in source.rows:
        assert all(raw not in warning for warning in bundle.warnings)

    plan_multi_table_load(bundle.tables, bundle.relations, overrides=overrides)
    _assert_key_warning(bundle.relations.notes, "members", "member_no")
    plan_multi_table_load(bundle.tables, bundle.relations)
    assert not any("키 열 가리기 미적용" in note for note in bundle.relations.notes)


@pytest.mark.parametrize("engine", ["sqlite", "duckdb"])
@pytest.mark.parametrize("text_keys", [False, True], ids=["numeric-keys", "text-keys"])
def test_linked_keys_report_effective_policy_and_keep_matching_values(
    tmp_path: Path, engine: str, text_keys: bool
) -> None:
    keys = ("key-alpha", "key-beta", "key-gamma") if text_keys else ("900001", "900002", "900003")
    members = NamedTableInput("members", ("member_no",), tuple((key,) for key in keys))
    entries = NamedTableInput(
        "entries",
        ("entry_id", "member_no", "memo"),
        (
            ("101", keys[0], "alpha@example.test"),
            ("102", keys[0], "alpha@example.test"),
            ("103", keys[1], "beta@example.test"),
            ("104", keys[2], "beta@example.test"),
        ),
    )
    overrides = SchemaOverrideSpec(
        columns=[
            ColumnOverride(table="members", source_column="member_no", masking=MaskingPolicy.HASH),
            ColumnOverride(table="entries", source_column="member_no", masking=MaskingPolicy.HASH),
            ColumnOverride(table="entries", source_column="memo", masking=MaskingPolicy.MASK),
        ]
    )
    db_path = tmp_path / f"linked.{engine}"
    bundle, result = load_tables(
        extra_tables=[members, entries],
        db_path=db_path,
        engine=engine,
        overrides=overrides,
        decompose_strings=False,
    )

    assert result.status == "succeeded"
    assert result.total_rows_loaded == 7
    stored = [_hash(key) if text_keys else int(key) for key in keys]
    assert _fetch_rows(db_path, engine, "SELECT member_no FROM members ORDER BY member_no") == [
        (value,) for value in sorted(stored, key=str)
    ]
    assert _fetch_rows(
        db_path,
        engine,
        "SELECT entries.member_no, memo FROM entries JOIN members USING (member_no) "
        "ORDER BY entry_id",
    ) == [
        (stored[0], "al***@example.test"),
        (stored[0], "al***@example.test"),
        (stored[1], "be***@example.test"),
        (stored[2], "be***@example.test"),
    ]

    registry = {
        (c["target_table"], c["target_column"]): c
        for c in inspect_db_catalog(db_path, engine=engine)["columns"]
    }
    parent = registry["members", "member_no"]
    child = registry["entries", "member_no"]
    assert parent["is_primary_key"] is True
    assert child["foreign_key_ref"] == "members(member_no)"
    expected_policy = "hash" if text_keys else "none"
    assert parent["masking_policy"] == child["masking_policy"] == expected_policy
    assert registry["entries", "memo"]["masking_policy"] == "mask"

    for warnings in (bundle.warnings, result.warnings):
        if text_keys:
            assert not any("키 열 가리기 미적용" in warning for warning in warnings)
        else:
            _assert_key_warning(warnings, "members", "member_no")
            _assert_key_warning(warnings, "entries", "member_no")
        assert all(key not in warning for key in keys for warning in warnings)


@pytest.mark.parametrize("engine", ["sqlite", "duckdb"])
def test_auto_masking_preserves_non_text_key_and_reports_it_unprotected(
    tmp_path: Path, engine: str
) -> None:
    keys = ("7100001", "7100002", "7100003")
    source = NamedTableInput("accounts", ("bank_account",), tuple((key,) for key in keys))
    without_masking = analyze_tables(extra_tables=[source])
    db_path = tmp_path / f"accounts.{engine}"
    bundle, result = load_tables(
        extra_tables=[source], db_path=db_path, engine=engine, auto_mask_pii=True
    )

    assert result.status == "succeeded"
    assert _fetch_rows(
        db_path, engine, "SELECT bank_account FROM accounts ORDER BY bank_account"
    ) == [(int(key),) for key in keys]
    (column,) = inspect_db_catalog(db_path, engine=engine)["columns"]
    assert (column["is_primary_key"], column["pii_level"], column["masking_policy"]) == (
        True,
        "L2",
        "none",
    )
    assert result.tables[0].masked_columns == []
    (strategy,) = bundle.load_plan.table_plans
    assert strategy.column_masking == {}
    assert not any("사용자 확정 개인정보 보호 정책" in note for note in strategy.notes)
    for candidate in bundle.load_plan.candidates:
        (target,) = candidate.tables[0].columns
        assert "개인정보보호:" not in target.description
    assert {c.candidate_id: c.security_isolation for c in bundle.load_plan.scorecards} == {
        c.candidate_id: c.security_isolation for c in without_masking.load_plan.scorecards
    }
    _assert_key_warning(bundle.warnings, "accounts", "bank_account")
    _assert_key_warning(result.warnings, "accounts", "bank_account")
    assert all(
        key not in warning for key in keys for warning in [*bundle.warnings, *result.warnings]
    )


@pytest.mark.parametrize("engine", ["sqlite", "duckdb"])
def test_skipped_key_policy_still_protects_quarantine_payload(tmp_path: Path, engine: str) -> None:
    source = NamedTableInput(
        "members",
        ("member_no", "sample_code"),
        (("900001", "A1"), ("900002", "B2"), ("900003", "A1")),
    )
    overrides = SchemaOverrideSpec(
        columns=[
            ColumnOverride(table="members", source_column="member_no", masking=MaskingPolicy.HASH),
            ColumnOverride(table="members", source_column="sample_code", unique=True),
        ]
    )
    db_path = tmp_path / f"quarantine.{engine}"
    _, result = load_tables(
        extra_tables=[source], db_path=db_path, engine=engine, overrides=overrides
    )

    assert (result.total_rows_loaded, result.total_rows_quarantined) == (2, 1)
    (rejected,) = inspect_db_catalog(db_path, engine=engine)["quarantine"]
    assert json.loads(rejected["raw_row_json"])["member_no"] == _hash("900003")
    _assert_key_warning(result.warnings, "members", "member_no")


@pytest.mark.parametrize("engine", ["sqlite", "duckdb"])
def test_date_key_reports_each_engines_existing_storage_behavior(
    tmp_path: Path, engine: str
) -> None:
    values = ("2031-03-01", "2031-03-02", "2031-03-03")
    source = NamedTableInput("dated", ("dated_id",), tuple((value,) for value in values))
    overrides = SchemaOverrideSpec(
        columns=[
            ColumnOverride(table="dated", source_column="dated_id", masking=MaskingPolicy.HASH)
        ]
    )
    db_path = tmp_path / f"dated.{engine}"
    bundle, result = load_tables(
        extra_tables=[source], db_path=db_path, engine=engine, overrides=overrides
    )

    assert result.status == "succeeded"
    (column,) = inspect_db_catalog(db_path, engine=engine)["columns"]
    assert column["is_primary_key"] is True
    rows = _fetch_rows(db_path, engine, "SELECT dated_id FROM dated ORDER BY dated_id")
    if engine == "sqlite":
        assert column["masking_policy"] == "hash"
        assert rows == [(value,) for value in sorted(_hash(value) for value in values)]
        assert not any("키 열 가리기 미적용" in warning for warning in result.warnings)
    else:
        assert column["masking_policy"] == "none"
        assert rows == [(date.fromisoformat(value),) for value in values]
        _assert_key_warning(bundle.warnings, "dated", "dated_id")
        _assert_key_warning(result.warnings, "dated", "dated_id")


def test_analysis_without_engine_says_date_key_masking_depends_on_engine() -> None:
    values = ("2031-03-01", "2031-03-02", "2031-03-03")
    source = NamedTableInput("dated", ("dated_id",), tuple((value,) for value in values))
    overrides = SchemaOverrideSpec(
        columns=[
            ColumnOverride(table="dated", source_column="dated_id", masking=MaskingPolicy.HASH)
        ]
    )

    bundle = analyze_tables(extra_tables=[source], overrides=overrides)

    (warning,) = [entry for entry in bundle.warnings if "키 열 가리기 미적용" in entry]
    assert "'dated'.'dated_id'" in warning
    assert "SQLite에 적재할 때만 가려지고" in warning
    assert "그 밖의 엔진에서는 원래 값이 저장됩니다" in warning
    assert bundle.load_plan.table_plans[0].column_masking == {}
    assert all(value not in warning for value in values)
