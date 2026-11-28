"""CLI coverage for local masking key status and transfer."""

import os
import sqlite3
from base64 import b64encode
from contextlib import closing
from pathlib import Path

import duckdb
import pytest

from autoetl.cli import main
from autoetl.security.masking_key import get_or_create_masking_key, read_masking_key


@pytest.fixture
def key_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "local" / "masking_key.json"
    monkeypatch.setenv("AUTOETL_MASKING_KEY_FILE", str(path))
    return path


def test_key_status_with_no_key_does_not_create_one(
    key_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["key", "status"]) == 0

    captured = capsys.readouterr()
    assert str(key_path) in captured.out
    assert "마스킹 키 없음" in captured.out
    assert captured.err == ""
    assert not key_path.exists()


def _assert_key_not_printed(output: str, key: bytes) -> None:
    if key.hex() in output or b64encode(key).decode("ascii") in output or repr(key) in output:
        pytest.fail("key bytes appeared in CLI output", pytrace=False)


def _assert_key_bytes_equal(actual: bytes, expected: bytes) -> None:
    if actual != expected:
        pytest.fail("key data changed unexpectedly", pytrace=False)


def test_key_status_shows_metadata_and_permission_warning(
    key_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    active, _ = get_or_create_masking_key()
    if os.name == "posix":
        key_path.chmod(0o644)

    assert main(["key", "status"]) == 0

    captured = capsys.readouterr()
    assert str(key_path) in captured.out
    assert "마스킹 키 있음" in captured.out
    assert f"key_id={active.key_id}" in captured.out
    assert f"created_at={active.created_at}" in captured.out
    if os.name == "posix":
        updated = read_masking_key()
        assert updated is not None
        assert updated.permission_warning
        assert updated.permission_warning in captured.err
    _assert_key_not_printed(captured.out + captured.err, active.key)


def test_key_export_is_private_and_refuses_existing_destination(
    tmp_path: Path, key_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    active, _ = get_or_create_masking_key()
    destination = tmp_path / "key-backup.json"

    assert main(["key", "export", str(destination)]) == 0

    captured = capsys.readouterr()
    assert str(destination) in captured.out
    assert "안전한 장소" in captured.err
    assert "원래 값을 추측" in captured.err
    copied = read_masking_key(destination)
    assert copied is not None
    assert copied.key_id == active.key_id
    _assert_key_bytes_equal(copied.key, active.key)
    assert copied.created_at == active.created_at
    if os.name == "posix":
        assert destination.stat().st_mode & 0o777 == 0o600
    _assert_key_not_printed(captured.out + captured.err, active.key)

    destination.write_text("preserve existing output", encoding="utf-8")
    assert main(["key", "export", str(destination)]) == 1
    captured = capsys.readouterr()
    assert "마스킹 키 작업 실패" in captured.err
    assert destination.read_text(encoding="utf-8") == "preserve existing output"
    assert key_path.is_file()
    _assert_key_not_printed(captured.out + captured.err, active.key)


def test_key_import_no_op_refusal_and_replacement_backup(
    tmp_path: Path,
    key_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    active, _ = get_or_create_masking_key()
    incoming_path = tmp_path / "incoming.json"
    with monkeypatch.context() as isolated:
        isolated.setenv("AUTOETL_MASKING_KEY_FILE", str(incoming_path))
        incoming, _ = get_or_create_masking_key()
    before_import = key_path.read_bytes()

    assert main(["key", "import", str(key_path)]) == 0
    captured = capsys.readouterr()
    assert "동일한 마스킹 키" in captured.out
    _assert_key_bytes_equal(key_path.read_bytes(), before_import)
    _assert_key_not_printed(captured.out + captured.err, active.key)

    assert main(["key", "import", str(incoming_path)]) == 1
    captured = capsys.readouterr()
    assert "마스킹 키 작업 실패" in captured.err
    assert "--replace" in captured.err
    _assert_key_bytes_equal(key_path.read_bytes(), before_import)
    _assert_key_not_printed(captured.out + captured.err, incoming.key)

    assert main(["key", "import", str(incoming_path), "--replace"]) == 0
    captured = capsys.readouterr()
    backup = key_path.with_name(f"masking_key.{active.key_id}.bak.json")
    assert str(backup) in captured.out
    _assert_key_bytes_equal(backup.read_bytes(), before_import)
    installed = read_masking_key()
    assert installed is not None
    _assert_key_bytes_equal(installed.key, incoming.key)
    assert installed.key_id == incoming.key_id
    _assert_key_not_printed(captured.out + captured.err, active.key)
    _assert_key_not_printed(captured.out + captured.err, incoming.key)


def test_key_import_install_from_backup(
    tmp_path: Path,
    key_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    backup = tmp_path / "backup.json"
    with monkeypatch.context() as isolated:
        isolated.setenv("AUTOETL_MASKING_KEY_FILE", str(backup))
        backed_up, _ = get_or_create_masking_key()

    assert main(["key", "import", str(backup)]) == 0

    captured = capsys.readouterr()
    assert "마스킹 키 가져오기 완료" in captured.out
    assert str(key_path) in captured.out
    assert f"key_id={backed_up.key_id}" in captured.out
    _assert_key_bytes_equal(key_path.read_bytes(), backup.read_bytes())
    _assert_key_not_printed(captured.out + captured.err, backed_up.key)


def test_key_import_rejects_invalid_contents_without_printing_them(
    tmp_path: Path, key_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    invalid = tmp_path / "invalid.json"
    sentinel = "fake-private-value-that-must-not-be-printed"
    invalid.write_text(sentinel, encoding="utf-8")

    assert main(["key", "import", str(invalid)]) == 1

    captured = capsys.readouterr()
    assert "마스킹 키 작업 실패" in captured.err
    assert sentinel not in captured.out + captured.err
    assert not key_path.exists()


@pytest.mark.parametrize("engine", ["sqlite", "duckdb"])
def test_key_status_compares_db_key_id_read_only(
    engine: str, tmp_path: Path, key_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    active, _ = get_or_create_masking_key()
    db_path = tmp_path / f"warehouse.{engine}"
    table = (
        "CREATE TABLE _etl_masking_keys "
        "(key_id TEXT, scheme TEXT, first_batch_id TEXT, registered_at TEXT)"
    )
    insert = "INSERT INTO _etl_masking_keys VALUES (?, ?, ?, ?)"
    row = (active.key_id, "hmac-sha256-24", "BATCH-TEST", "2026-01-01T00:00:00Z")
    if engine == "sqlite":
        with closing(sqlite3.connect(db_path)) as connection, connection:
            connection.execute(table)
            connection.execute(insert, row)
    else:
        with duckdb.connect(str(db_path)) as duck_connection:
            duck_connection.execute(table)
            duck_connection.execute(insert, row)
    db_before = db_path.read_bytes()
    args = ["key", "status", "--db", str(db_path)]

    assert main(args) == 0
    captured = capsys.readouterr()
    assert f"DB key_id={active.key_id} (로컬 키와 일치)" in captured.out
    _assert_key_not_printed(captured.out + captured.err, active.key)

    key_path.unlink()
    assert main(args) == 0
    captured = capsys.readouterr()
    assert f"DB key_id={active.key_id} (로컬 키 없음)" in captured.out
    assert not key_path.exists()

    other, _ = get_or_create_masking_key()
    assert main(args) == 0
    captured = capsys.readouterr()
    assert f"key_id={other.key_id}" in captured.out
    assert f"DB key_id={active.key_id} (로컬 키와 불일치)" in captured.out
    assert db_path.read_bytes() == db_before
    _assert_key_not_printed(captured.out + captured.err, other.key)


def test_key_status_on_legacy_database_reports_no_id(
    tmp_path: Path, key_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    legacy_db = tmp_path / "legacy.sqlite"
    with closing(sqlite3.connect(legacy_db)) as connection, connection:
        connection.execute("CREATE TABLE legacy_data (item TEXT)")

    assert main(["key", "status", "--db", str(legacy_db)]) == 0

    captured = capsys.readouterr()
    assert "DB 마스킹 키 ID 기록 없음" in captured.out
    assert not key_path.exists()


def test_key_status_for_missing_db_returns_error_without_creating_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "missing.sqlite"

    assert main(["key", "status", "--db", str(missing)]) == 1

    captured = capsys.readouterr()
    assert "마스킹 키 작업 실패" in captured.err
    assert not missing.exists()
