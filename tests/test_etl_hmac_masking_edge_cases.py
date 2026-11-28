"""Keyed hashing on storage and database states that differ from a fresh local file."""

from __future__ import annotations

import errno
import os
import shutil
import sqlite3
import stat
from contextlib import closing
from pathlib import Path

import duckdb
import pytest

from autoetl.cli import main
from autoetl.contracts import ColumnOverride, MaskingPolicy, OrphanPolicy, SchemaOverrideSpec
from autoetl.core.db_loader import load_tables
from autoetl.core.errors import InputError
from autoetl.core.multi_context import NamedTableInput
from autoetl.security import masking_key as masking_key_module
from autoetl.security.masking_key import (
    MaskingKey,
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


@pytest.mark.parametrize("local_key", ["missing", "different"])
def test_wrong_key_still_stops_load_when_read_only_check_is_unavailable(
    tmp_path: Path, local_key: str
) -> None:
    """읽기 전용 확인을 못 하는 상태에서도 키가 없거나 다르면 쓰기 전에 멈춘다."""
    database = tmp_path / "warehouse.duckdb"
    key_path = tmp_path / "masking_key.json"
    load_tables(
        extra_tables=[_members(900001)], db_path=database, engine="duckdb", overrides=_HASH_OVERRIDE
    )
    registered = read_masking_key()
    assert registered is not None
    key_path.unlink()
    other = get_or_create_masking_key()[0] if local_key == "different" else None

    with duckdb.connect(str(database)) as held:
        with pytest.raises(InputError, match="autoetl key import") as stopped:
            load_tables(
                extra_tables=[_members(900004)],
                db_path=database,
                engine="duckdb",
                overrides=_HASH_OVERRIDE,
            )
        assert held.execute("SELECT count(*) FROM members").fetchone() == (3,)
        assert held.execute("SELECT key_id FROM _etl_masking_keys").fetchall() == [
            (registered.key_id,)
        ]
    # 이 컴퓨터에 다른 키가 있을 때만 교체 옵션을 안내한다.
    assert ("--replace" in str(stopped.value)) is (other is not None)
    assert read_masking_key() == other


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


@pytest.mark.skipif(os.name != "posix", reason="POSIX 파일 권한만 확인한다")
def test_export_warns_when_backup_is_readable_by_other_users(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """권한이 마운트 설정으로 정해지는 저장 장치에 내보내면 다른 사용자가 읽을 수 있다고 알린다."""
    get_or_create_masking_key()
    usb = tmp_path / "usb"
    assert export_masking_key(tmp_path / "private" / "backup.json").permission_warning is None

    real_publish = masking_key_module._publish_key

    def publish_under_mount_mask(path: Path, key: MaskingKey, *, overwrite: bool = False) -> bool:
        published = real_publish(path, key, overwrite=overwrite)
        if published and path.parent == usb:
            path.chmod(0o644)
        return published

    monkeypatch.setattr(masking_key_module, "_publish_key", publish_under_mount_mask)

    exported = export_masking_key(usb / "backup.json")
    assert exported.permission_warning is not None
    assert "다른 사용자" in exported.permission_warning

    capsys.readouterr()
    assert main(["key", "export", str(usb / "second.json")]) == 0
    assert "다른 사용자" in capsys.readouterr().err


def test_replacement_refuses_when_backup_name_is_the_active_key_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """활성 키 파일이 곧 백업 이름이면 교체로 하나뿐인 사본을 덮어쓰지 않는다."""
    current, _ = get_or_create_masking_key()
    incoming, _ = get_or_create_masking_key(tmp_path / "incoming.json")
    active = tmp_path / f"masking_key.{current.key_id}.bak.json"
    (tmp_path / "masking_key.json").rename(active)
    monkeypatch.setenv("AUTOETL_MASKING_KEY_FILE", str(active))

    with pytest.raises(InputError, match="백업"):
        import_masking_key(incoming.path, replace=True)

    kept = read_masking_key()
    assert kept is not None
    assert kept.key_id == current.key_id


@pytest.mark.skipif(os.name != "posix", reason="디렉터리 동기화는 POSIX에서만 한다")
def test_new_key_file_name_is_synced_to_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """키 파일을 만든 뒤 그 폴더까지 동기화해 파일 이름이 디스크에 남게 한다."""
    synced_directories: list[int] = []
    real_fsync = os.fsync

    def recording_fsync(fd: int) -> None:
        status = os.fstat(fd)
        if stat.S_ISDIR(status.st_mode):
            synced_directories.append(status.st_ino)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", recording_fsync)

    _key, created = get_or_create_masking_key()

    assert created
    assert tmp_path.stat().st_ino in synced_directories
