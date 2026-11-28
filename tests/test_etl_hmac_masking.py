"""Keyed hashing and database identity through the public ETL entry points."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import duckdb
import pytest

from autoetl.contracts import ColumnOverride, MaskingPolicy, OrphanPolicy, SchemaOverrideSpec
from autoetl.core.db_loader import inspect_db_catalog, load_tables, mask_value
from autoetl.core.errors import InputError
from autoetl.core.multi_analyze import analyze_tables
from autoetl.core.multi_context import NamedTableInput

if TYPE_CHECKING:
    from collections.abc import Iterator


_MEMBERS = NamedTableInput(
    "members",
    ("member_no", "phone"),
    (("900001", "202-555-0140"), ("900002", "202-555-0141"), ("900003", "202-555-0142")),
)
_HASH_PHONE = ColumnOverride(table="members", source_column="phone", masking=MaskingPolicy.HASH)
_HASH_OVERRIDE = SchemaOverrideSpec(columns=[_HASH_PHONE])


@contextmanager
def _connect(path: Path, engine: str) -> Iterator[sqlite3.Connection | duckdb.DuckDBPyConnection]:
    if engine == "sqlite":
        with closing(sqlite3.connect(path)) as connection, connection:
            yield connection
    else:
        with duckdb.connect(str(path)) as duck_connection:
            yield duck_connection


def _digest(key: bytes, value: str) -> str:
    return "h_" + hmac.digest(key, value.encode("utf-8"), "sha256").hex()[:24]


def test_public_hash_requires_explicit_key_and_preserves_input_bytes() -> None:
    raw = "  가상-입력  "
    with pytest.raises(ValueError, match="키"):
        mask_value(raw, None, MaskingPolicy.HASH)
    assert mask_value(raw, None, MaskingPolicy.HASH, hash_key=bytes(32)) == _digest(bytes(32), raw)


def test_analysis_and_plain_load_do_not_prepare_a_key(tmp_path: Path) -> None:
    key_path = tmp_path / "masking_key.json"
    analyze_tables(extra_tables=[_MEMBERS], overrides=_HASH_OVERRIDE)
    assert not key_path.exists()

    database = tmp_path / "plain.sqlite"
    load_tables(extra_tables=[_MEMBERS], db_path=database)
    assert not key_path.exists()
    catalog = inspect_db_catalog(database)
    assert len(catalog["management_tables"]) == 4
    with _connect(database, "sqlite") as connection:
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name = '_etl_masking_keys'"
            ).fetchall()
            == []
        )


@pytest.mark.parametrize("engine", ["sqlite", "duckdb"])
def test_hash_load_pins_key_and_restored_backup_resumes_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: str
) -> None:
    from autoetl.security.masking_key import (
        export_masking_key,
        get_or_create_masking_key,
        import_masking_key,
        read_masking_key,
    )

    key_path = tmp_path / "masking_key.json"
    database = tmp_path / f"warehouse.{engine}"
    bundle, result = load_tables(
        extra_tables=[_MEMBERS], db_path=database, engine=engine, overrides=_HASH_OVERRIDE
    )
    key = read_masking_key()
    assert key is not None
    assert result.status == "succeeded"
    assert sum("autoetl key export" in warning for warning in result.warnings) == 1
    if os.name == "posix":
        assert key_path.stat().st_mode & 0o777 == 0o600

    expected = [(_digest(key.key, phone),) for _, phone in _MEMBERS.rows]
    legacy = [
        ("h_" + hashlib.sha256(phone.encode()).hexdigest()[:16],) for _, phone in _MEMBERS.rows
    ]
    with _connect(database, engine) as connection:
        assert (
            connection.execute("SELECT phone FROM members ORDER BY member_no").fetchall()
            == expected
        )
        assert connection.execute(
            "SELECT key_id, scheme, first_batch_id FROM _etl_masking_keys"
        ).fetchall() == [(key.key_id, "hmac-sha256-24", result.batch_id)]
    assert expected != legacy
    assert all(len(digest) == 26 for (digest,) in expected)
    catalog = inspect_db_catalog(database, engine=engine)
    assert [table["table"] for table in catalog["business_tables"]] == ["members"]

    again = load_tables(
        extra_tables=[_MEMBERS],
        db_path=database,
        engine=engine,
        overrides=_HASH_OVERRIDE,
        force_reload=True,
    )[1]
    assert again.status == "succeeded"
    assert not any("autoetl key export" in warning for warning in again.warnings)
    with _connect(database, engine) as connection:
        assert (
            connection.execute("SELECT phone FROM members ORDER BY member_no").fetchall()
            == expected
        )
    if key.key.hex() in bundle.model_dump_json() or key.key.hex() in result.model_dump_json():
        pytest.fail("masking key data leaked in a load response", pytrace=False)
    if key.key.hex().encode() in database.read_bytes() or key.key in database.read_bytes():
        pytest.fail("masking key data leaked in the database", pytrace=False)

    backup = tmp_path / "backup.json"
    export_masking_key(backup)
    key_path.unlink()
    before = database.read_bytes()
    with pytest.raises(InputError, match="autoetl key import") as missing:
        load_tables(
            extra_tables=[_MEMBERS], db_path=database, engine=engine, overrides=_HASH_OVERRIDE
        )
    if key.key.hex() in str(missing.value):
        pytest.fail("masking key data leaked in an error", pytrace=False)
    assert key.key_id in str(missing.value)
    assert not key_path.exists()
    assert database.read_bytes() == before

    other_path = tmp_path / "other.json"
    monkeypatch.setenv("AUTOETL_MASKING_KEY_FILE", str(other_path))
    other, _ = get_or_create_masking_key()
    with pytest.raises(InputError, match="autoetl key import") as mismatch:
        load_tables(
            extra_tables=[_MEMBERS], db_path=database, engine=engine, overrides=_HASH_OVERRIDE
        )
    assert key.key_id in str(mismatch.value) and other.key_id in str(mismatch.value)
    if any(item.key.hex() in str(mismatch.value) for item in (key, other)):
        pytest.fail("masking key data leaked in an error", pytrace=False)
    assert database.read_bytes() == before

    monkeypatch.setenv("AUTOETL_MASKING_KEY_FILE", str(key_path))
    import_masking_key(backup)
    restored = load_tables(
        extra_tables=[_MEMBERS],
        db_path=database,
        engine=engine,
        overrides=_HASH_OVERRIDE,
        force_reload=True,
    )[1]
    assert restored.status == "succeeded"
    with _connect(database, engine) as connection:
        assert (
            connection.execute("SELECT phone FROM members ORDER BY member_no").fetchall()
            == expected
        )


@pytest.mark.parametrize("engine", ["sqlite", "duckdb"])
def test_legacy_registry_blocks_hash_only_before_key_creation(tmp_path: Path, engine: str) -> None:
    database = tmp_path / f"legacy.{engine}"
    load_tables(extra_tables=[_MEMBERS], db_path=database, engine=engine)
    with _connect(database, engine) as connection:
        connection.execute("UPDATE _etl_column_registry SET masking_policy = 'hash'")
    before = database.read_bytes()

    with pytest.raises(InputError, match=r"예전 방식.*새 DB"):
        load_tables(
            extra_tables=[_MEMBERS], db_path=database, engine=engine, overrides=_HASH_OVERRIDE
        )
    assert not (tmp_path / "masking_key.json").exists()
    assert database.read_bytes() == before
    assert (
        load_tables(extra_tables=[_MEMBERS], db_path=database, engine=engine, force_reload=True)[
            1
        ].status
        == "succeeded"
    )
    assert not (tmp_path / "masking_key.json").exists()


def test_quarantine_hash_and_key_registration_follow_load_transaction(tmp_path: Path) -> None:
    from autoetl.security.masking_key import read_masking_key

    source = NamedTableInput(
        "members",
        ("member_no", "phone", "sample_code"),
        (
            ("900001", "202-555-0140", "one"),
            ("900002", "202-555-0141", "two"),
            ("900003", "202-555-0142", "one"),
        ),
    )
    spec = SchemaOverrideSpec(
        columns=[
            _HASH_PHONE,
            ColumnOverride(table="members", source_column="sample_code", unique=True),
        ]
    )
    rejected_db = tmp_path / "rejected.sqlite"
    result = load_tables(
        extra_tables=[source],
        db_path=rejected_db,
        overrides=spec.model_copy(update={"orphan_policy": OrphanPolicy.REJECT_ALL}),
    )[1]
    assert result.status == "failed"
    with _connect(rejected_db, "sqlite") as connection:
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    assert ("_etl_masking_keys",) not in tables
    assert ("members",) not in tables

    key = read_masking_key()
    assert key is not None
    database = tmp_path / "partial.sqlite"
    result = load_tables(extra_tables=[source], db_path=database, overrides=spec)[1]
    assert result.status == "partial"
    (row,) = inspect_db_catalog(database)["quarantine"]
    assert json.loads(row["raw_row_json"])["phone"] == _digest(key.key, "202-555-0142")
    with _connect(database, "sqlite") as connection:
        assert connection.execute("SELECT key_id FROM _etl_masking_keys").fetchall() == [
            (key.key_id,)
        ]
