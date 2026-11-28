"""Masking key storage tests use only generated synthetic key material."""

from __future__ import annotations

import hmac
import json
import os
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest

from autoetl.core.errors import InputError
from autoetl.security.masking_key import (
    export_masking_key,
    get_or_create_masking_key,
    import_masking_key,
    masking_key_path,
    read_masking_key,
)


def _fixture_key(seed: int = 0) -> bytes:
    return bytes(range(seed, seed + 32))


def _key_document(seed: int = 0) -> dict[str, object]:
    key = _fixture_key(seed)
    return {
        "version": 1,
        "key_id": "k_" + hmac.digest(key, b"autoetl-masking-key-id", "sha256").hex()[:12],
        "key": key.hex(),
        "created_at": "2026-01-02T03:04:05+00:00",
    }


def _write_key(path: Path, document: dict[str, object]) -> None:
    path.write_text(json.dumps(document), encoding="utf-8")


@pytest.mark.parametrize(
    ("platform", "environment", "relative_config"),
    [
        ("linux", {}, (".config",)),
        ("linux", {"XDG_CONFIG_HOME": "xdg"}, ("xdg",)),
        ("darwin", {"XDG_CONFIG_HOME": "xdg"}, ("Library", "Application Support")),
        ("win32", {"APPDATA": "appdata"}, ("appdata",)),
        ("win32", {}, ("AppData", "Roaming")),
    ],
)
def test_config_path_follows_the_platform(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    environment: dict[str, str],
    relative_config: tuple[str, ...],
) -> None:
    monkeypatch.delenv("AUTOETL_MASKING_KEY_FILE", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(Path, "home", classmethod(lambda _: tmp_path))
    for name, value in environment.items():
        monkeypatch.setenv(name, str(tmp_path / value))

    assert masking_key_path() == tmp_path.joinpath(*relative_config, "autoetl", "masking_key.json")


def test_create_persists_document_and_reads_the_same_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "private" / "nested" / "masking_key.json"

    def token_bytes(length: int) -> bytes:
        assert length == 32
        return _fixture_key()

    monkeypatch.setattr("autoetl.security.masking_key.secrets.token_bytes", token_bytes)
    assert read_masking_key(path) is None
    assert not path.parent.exists()

    key, created = get_or_create_masking_key(path)

    assert created
    assert key.path == path
    assert key.key == _fixture_key()
    assert key.key_id == _key_document()["key_id"]
    assert key.permission_warning is None
    assert datetime.fromisoformat(key.created_at.replace("Z", "+00:00")).tzinfo == timezone.utc
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored == _key_document() | {"created_at": key.created_at}
    assert _fixture_key().hex() not in repr(key)
    assert repr(_fixture_key()) not in repr(key)
    assert read_masking_key(path) == key
    assert get_or_create_masking_key(path) == (key, False)


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("version", 2),
        ("version", True),
        ("key_id", "k_000000000000"),
        ("key", "ab" * 31),
        ("key", "?!" * 32),
        ("created_at", "2026-01-02T03:04:05"),
    ],
)
def test_invalid_document_is_never_replaced(tmp_path: Path, field: str, invalid: object) -> None:
    path = tmp_path / "masking_key.json"
    _write_key(path, _key_document() | {field: invalid})
    original = path.read_bytes()

    with pytest.raises(InputError) as read_failure:
        read_masking_key(path)
    with pytest.raises(InputError) as create_failure:
        get_or_create_masking_key(path)

    assert path.read_bytes() == original
    for failure in (read_failure, create_failure):
        assert _fixture_key().hex() not in str(failure.value)
        assert failure.value.__cause__ is None


def test_malformed_json_has_a_redacted_error(tmp_path: Path) -> None:
    path = tmp_path / "masking_key.json"
    fake_secret = _fixture_key().hex()
    path.write_text(fake_secret + " invalid", encoding="utf-8")
    with pytest.raises(InputError) as failure:
        get_or_create_masking_key(path)
    assert fake_secret not in str(failure.value)
    assert failure.value.__cause__ is None
    assert path.read_text(encoding="utf-8") == fake_secret + " invalid"


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits")
def test_new_key_and_directories_are_private_and_reading_warns_about_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "private" / "nested" / "masking_key.json"
    monkeypatch.setattr(
        "autoetl.security.masking_key.secrets.token_bytes", lambda _: _fixture_key()
    )
    key, _ = get_or_create_masking_key(path)
    assert key.permission_warning is None
    for directory in (path.parent, path.parent.parent):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    path.chmod(0o640)
    loaded = read_masking_key(path)
    assert loaded is not None
    assert loaded.key_id == key.key_id
    assert loaded.permission_warning
    assert _fixture_key().hex() not in loaded.permission_warning
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_concurrent_processes_see_one_complete_key(tmp_path: Path) -> None:
    path = tmp_path / "private" / "masking_key.json"
    script = """
import sys
from pathlib import Path
from unittest.mock import patch
from autoetl.security.masking_key import get_or_create_masking_key, read_masking_key

path, seed = Path(sys.argv[1]), int(sys.argv[2])
with patch('autoetl.security.masking_key.secrets.token_bytes', return_value=bytes([seed]) * 32):
    key, created = get_or_create_masking_key(path)
assert read_masking_key(path) == key
print(key.key_id, int(created))
"""

    def create(seed: int) -> tuple[str, str]:
        result = subprocess.run(
            [sys.executable, "-c", script, str(path), str(seed)],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        key_id, created = result.stdout.split()
        return key_id, created

    with ThreadPoolExecutor(max_workers=4) as workers:
        observed = list(workers.map(create, range(4)))

    saved = read_masking_key(path)
    assert saved is not None
    assert {key_id for key_id, _ in observed} == {saved.key_id}
    assert [created for _, created in observed].count("1") == 1
    assert list(path.parent.iterdir()) == [path]


def test_export_never_overwrites_and_import_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    active = tmp_path / "configured-key.json"
    source = tmp_path / "import.json"
    exported = tmp_path / "export.json"
    monkeypatch.setenv("AUTOETL_MASKING_KEY_FILE", str(active))
    assert masking_key_path() == active
    _write_key(source, _key_document())

    loaded, changed = import_masking_key(source)
    assert changed
    assert loaded.path == active
    assert loaded.key == _fixture_key()
    active_bytes = active.read_bytes()
    _write_key(source, _key_document() | {"created_at": "2026-02-03T03:04:05Z"})
    assert import_masking_key(source) == (loaded, False)
    assert active.read_bytes() == active_bytes

    export = export_masking_key(exported)
    assert export.path == exported
    assert export.key_id == loaded.key_id
    assert export.key == loaded.key
    assert read_masking_key(exported) == export
    with pytest.raises(InputError):
        export_masking_key(exported)
    assert read_masking_key(exported) == export


def test_replacement_backs_up_the_old_key_without_overwriting_existing_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    active = tmp_path / "configured-key.json"
    source = tmp_path / "import.json"
    monkeypatch.setenv("AUTOETL_MASKING_KEY_FILE", str(active))
    _write_key(active, _key_document())
    _write_key(source, _key_document(seed=1))
    old = read_masking_key()
    assert old is not None
    backup = active.with_name(f"masking_key.{old.key_id}.bak.json")

    with pytest.raises(InputError):
        import_masking_key(source)
    assert read_masking_key() == old
    assert not backup.exists()

    replacement, changed = import_masking_key(source, replace=True)
    assert changed
    assert read_masking_key() == replacement
    assert replacement.key == _fixture_key(seed=1)
    backed_up = read_masking_key(backup)
    assert backed_up is not None
    assert backed_up.key == old.key
    if os.name == "posix":
        assert stat.S_IMODE(backup.stat().st_mode) == 0o600

    _write_key(backup, _key_document() | {"extra": "preserve existing backup"})
    backup_bytes = backup.read_bytes()
    import_masking_key(backup, replace=True)
    import_masking_key(source, replace=True)
    assert backup.read_bytes() == backup_bytes


def test_conflicting_backup_prevents_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    active = tmp_path / "masking_key.json"
    source = tmp_path / "import.json"
    monkeypatch.setenv("AUTOETL_MASKING_KEY_FILE", str(active))
    _write_key(active, _key_document())
    _write_key(source, _key_document(seed=1))
    old = read_masking_key()
    assert old is not None
    backup = active.with_name(f"masking_key.{old.key_id}.bak.json")
    _write_key(backup, _key_document(seed=2))
    backup_bytes = backup.read_bytes()

    with pytest.raises(InputError):
        import_masking_key(source, replace=True)

    assert read_masking_key() == old
    assert backup.read_bytes() == backup_bytes


def test_concurrent_replacements_back_up_every_displaced_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    active = tmp_path / "masking_key.json"
    monkeypatch.setenv("AUTOETL_MASKING_KEY_FILE", str(active))
    _write_key(active, _key_document())
    original = read_masking_key()
    assert original is not None
    script = """
import sys
import time
from pathlib import Path
from unittest.mock import patch
from autoetl.security.masking_key import import_masking_key

source = Path(sys.argv[1])
ready = source.with_suffix('.ready')
ready.touch()
for _ in range(200):
    if len(list(source.parent.glob('*.ready'))) == 2:
        break
    time.sleep(0.01)
else:
    raise RuntimeError('Replacement processes did not both start')

replace = Path.replace
def delayed_replace(self, target):
    time.sleep(0.25)
    return replace(self, target)

with patch.object(Path, 'replace', delayed_replace):
    _, changed = import_masking_key(source, replace=True)
assert changed
"""
    sources = [tmp_path / "first.json", tmp_path / "second.json"]
    for seed, source in enumerate(sources, start=1):
        _write_key(source, _key_document(seed))

    def rotate(source: Path) -> None:
        subprocess.run(
            [sys.executable, "-c", script, str(source)],
            capture_output=True,
            check=True,
            timeout=10,
        )

    with ThreadPoolExecutor(max_workers=2) as workers:
        list(workers.map(rotate, sources))

    final = read_masking_key()
    assert final is not None
    assert final.key in (_fixture_key(1), _fixture_key(2))
    backup_keys = [read_masking_key(path) for path in tmp_path.glob("masking_key.*.bak.json")]
    assert {key.key_id for key in backup_keys if key is not None} == {
        _key_document(seed)["key_id"] for seed in range(3)
    } - {final.key_id}
    lock = active.with_name(f".{active.name}.lock")
    assert lock.stat().st_size == 0
    if os.name == "posix":
        assert stat.S_IMODE(lock.stat().st_mode) == 0o600
