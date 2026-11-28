"""Keyed hashing on storage and database states that differ from a fresh local file."""

from __future__ import annotations

import errno
import os
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import duckdb
import pytest

from autoetl.contracts import ColumnOverride, MaskingPolicy, OrphanPolicy, SchemaOverrideSpec
from autoetl.core.db_loader import load_tables
from autoetl.core.errors import InputError
from autoetl.core.multi_context import NamedTableInput
from autoetl.security.masking_key import (
    export_masking_key,
    get_or_create_masking_key,
    import_masking_key,
    read_masking_key,
)

_HASH_OVERRIDE = SchemaOverrideSpec(
    columns=[ColumnOverride(table="members", source_column="phone", masking=MaskingPolicy.HASH)]
)


def _members(first: int, count: int = 3) -> NamedTableInput:
    return NamedTableInput(
        "members",
        ("member_no", "phone"),
        tuple((str(first + i), f"202-555-01{(first + i) % 100:02d}") for i in range(count)),
    )


def _without_hard_links(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_link(*_args: object, **_kwargs: object) -> None:
        raise OSError(errno.EPERM, "hard links are not supported")

    monkeypatch.setattr(os, "link", no_link)


def test_key_is_created_and_backed_up_on_storage_without_hard_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FAT·exFAT처럼 하드 링크가 없는 저장 장치에도 키를 만들고 백업할 수 있다."""
    _without_hard_links(monkeypatch)
    key_path = tmp_path / "masking_key.json"

    created, was_created = get_or_create_masking_key()
    assert was_created
    assert read_masking_key() == created
    if os.name == "posix":
        assert key_path.stat().st_mode & 0o777 == 0o600

    backup = tmp_path / "usb" / "backup.json"
    exported = export_masking_key(backup)
    assert exported.key == created.key
    assert read_masking_key(backup) is not None
    if os.name == "posix":
        assert backup.stat().st_mode & 0o777 == 0o600
    assert sorted(path.name for path in backup.parent.iterdir()) == ["backup.json"]

    # 이미 있는 파일은 이 경로에서도 덮어쓰지 않는다.
    before = backup.read_bytes()
    with pytest.raises(InputError, match="이미"):
        export_masking_key(backup)
    assert backup.read_bytes() == before
    assert get_or_create_masking_key() == (created, False)

    key_path.unlink()
    imported, changed = import_masking_key(backup)
    assert changed
    assert imported.key == created.key
    assert sorted(path.name for path in tmp_path.iterdir()) == ["masking_key.json", "usb"]


def test_hash_load_recovers_sqlite_database_left_with_hot_journal(tmp_path: Path) -> None:
    """쓰던 프로세스가 죽어 롤백 저널이 남은 DB에도 해시 적재가 이어진다."""
    original = tmp_path / "warehouse.sqlite"
    _bundle, first = load_tables(
        extra_tables=[_members(900001)], db_path=original, overrides=_HASH_OVERRIDE
    )
    assert first.status == "succeeded"

    # 커밋하지 않은 트랜잭션이 DB 파일까지 쓴 순간을 복사하면 죽은 프로세스가 남긴 상태가 된다.
    crashed = tmp_path / "crashed.sqlite"
    with closing(sqlite3.connect(original, isolation_level=None)) as writer:
        writer.execute("PRAGMA cache_size = 5")
        writer.execute("BEGIN")
        writer.execute("CREATE TABLE junk (a, b)")
        writer.executemany("INSERT INTO junk VALUES (?, ?)", ((i, "x" * 500) for i in range(5000)))
        shutil.copy(original, crashed)
        shutil.copy(f"{original}-journal", f"{crashed}-journal")
        writer.execute("ROLLBACK")

    _bundle, result = load_tables(
        extra_tables=[_members(900004)], db_path=crashed, overrides=_HASH_OVERRIDE
    )

    assert result.status == "succeeded"
    assert not Path(f"{crashed}-journal").exists()
    with closing(sqlite3.connect(crashed)) as connection:
        assert connection.execute("SELECT count(*) FROM members").fetchone() == (6,)
        assert connection.execute("SELECT count(*) FROM _etl_masking_keys").fetchone() == (1,)
        assert (
            connection.execute("SELECT name FROM sqlite_master WHERE name = 'junk'").fetchall()
            == []
        )


def test_hash_load_works_while_caller_keeps_duckdb_connection_open(tmp_path: Path) -> None:
    """같은 프로세스가 대상 DuckDB 파일을 이미 열어 둔 상태에서도 해시 적재가 된다."""
    database = tmp_path / "warehouse.duckdb"
    load_tables(
        extra_tables=[_members(900001)], db_path=database, engine="duckdb", overrides=_HASH_OVERRIDE
    )

    with duckdb.connect(str(database)) as held:
        _bundle, result = load_tables(
            extra_tables=[_members(900004)],
            db_path=database,
            engine="duckdb",
            overrides=_HASH_OVERRIDE,
        )
        assert result.status == "succeeded"
        assert held.execute("SELECT count(*) FROM members").fetchone() == (6,)
        assert held.execute("SELECT count(*) FROM _etl_masking_keys").fetchone() == (1,)


def test_wrong_key_still_stops_load_when_read_only_check_is_unavailable(tmp_path: Path) -> None:
    """읽기 전용 확인을 못 하는 상태에서도 키가 다르면 쓰기 전에 멈춘다."""
    database = tmp_path / "warehouse.duckdb"
    load_tables(
        extra_tables=[_members(900001)], db_path=database, engine="duckdb", overrides=_HASH_OVERRIDE
    )
    (tmp_path / "masking_key.json").unlink()

    with duckdb.connect(str(database)) as held:
        with pytest.raises(InputError, match="autoetl key import"):
            load_tables(
                extra_tables=[_members(900004)],
                db_path=database,
                engine="duckdb",
                overrides=_HASH_OVERRIDE,
            )
        assert held.execute("SELECT count(*) FROM members").fetchone() == (3,)
    assert not (tmp_path / "masking_key.json").exists()


@pytest.mark.parametrize("engine", ["sqlite", "duckdb"])
def test_new_key_warning_shows_path_but_stored_batch_note_does_not(
    tmp_path: Path, engine: str
) -> None:
    """새 키 안내의 파일 경로는 결과에만 보이고 DB에 남는 메모에는 들어가지 않는다."""
    key_path = tmp_path / "masking_key.json"
    database = tmp_path / f"warehouse.{engine}"

    _bundle, result = load_tables(
        extra_tables=[_members(900001)], db_path=database, engine=engine, overrides=_HASH_OVERRIDE
    )

    assert any(str(key_path) in warning for warning in result.warnings)
    connection: sqlite3.Connection | duckdb.DuckDBPyConnection = (
        sqlite3.connect(database) if engine == "sqlite" else duckdb.connect(str(database))
    )
    with closing(connection):
        ((notes,),) = connection.execute("SELECT notes FROM _etl_load_batches").fetchall()
    assert "autoetl key export" in notes
    assert str(key_path) not in notes
    assert str(tmp_path) not in notes


@pytest.mark.parametrize("orphan_policy", list(OrphanPolicy))
@pytest.mark.parametrize("engine", ["sqlite", "duckdb"])
def test_hash_requested_only_on_numeric_key_prepares_key_under_either_error_policy(
    tmp_path: Path, engine: str, orphan_policy: OrphanPolicy
) -> None:
    """해시를 요청한 열이 숫자 키뿐이어도 오류 처리 정책과 관계없이 키를 준비해 둔다."""
    source = NamedTableInput("members", ("member_no",), (("900001",), ("900002",), ("900003",)))
    overrides = SchemaOverrideSpec(
        columns=[
            ColumnOverride(table="members", source_column="member_no", masking=MaskingPolicy.HASH)
        ],
        orphan_policy=orphan_policy,
    )
    database = tmp_path / f"warehouse.{engine}"

    _bundle, result = load_tables(
        extra_tables=[source], db_path=database, engine=engine, overrides=overrides
    )

    assert result.status == "succeeded"
    key = read_masking_key()
    assert key is not None
    connection: sqlite3.Connection | duckdb.DuckDBPyConnection = (
        sqlite3.connect(database) if engine == "sqlite" else duckdb.connect(str(database))
    )
    with closing(connection):
        stored = connection.execute("SELECT member_no FROM members ORDER BY member_no").fetchall()
        recorded = connection.execute("SELECT key_id FROM _etl_masking_keys").fetchall()
    assert stored == [(900001,), (900002,), (900003,)]
    assert recorded == [(key.key_id,)]
