"""Read, create, and transfer the local keyed masking secret."""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from autoetl.core.errors import InputError

_KEY_FILE = "masking_key.json"
_KEY_VERSION = 1
_KEY_BYTES = 32
_KEY_HEX = re.compile(r"[0-9a-fA-F]{64}")
_CORRUPT_KEY = "마스킹 키 파일 형식이 올바르지 않습니다."
_MISSING_KEY = "마스킹 키 파일이 없습니다."
_PERMISSION_WARNING = "마스킹 키 파일을 그룹 또는 다른 사용자가 읽을 수 있습니다."


@dataclass(frozen=True)
class MaskingKey:
    """Key bytes paired with the public metadata needed to use them."""

    path: Path
    key_id: str
    key: bytes = field(repr=False)
    created_at: str
    permission_warning: str | None


def masking_key_path() -> Path:
    """Return the override or the platform-specific user config path."""
    override = os.environ.get("AUTOETL_MASKING_KEY_FILE")
    if override:
        return Path(override).expanduser()

    if sys.platform == "darwin":
        config = Path.home() / "Library" / "Application Support"
    elif sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        config = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    else:
        xdg_config = os.environ.get("XDG_CONFIG_HOME")
        config = Path(xdg_config) if xdg_config else Path.home() / ".config"
    return config / "autoetl" / _KEY_FILE


def _key_id(key: bytes) -> str:
    return "k_" + hmac.digest(key, b"autoetl-masking-key-id", "sha256").hex()[:12]


def _parse_key(path: Path, document: object, permission_warning: str | None) -> MaskingKey:
    if not isinstance(document, dict):
        raise InputError(_CORRUPT_KEY)

    version = document.get("version")
    encoded = document.get("key")
    stored_id = document.get("key_id")
    created_at = document.get("created_at")
    if (
        type(version) is not int
        or version != _KEY_VERSION
        or not isinstance(encoded, str)
        or not _KEY_HEX.fullmatch(encoded)
        or not isinstance(created_at, str)
    ):
        raise InputError(_CORRUPT_KEY)

    key = bytes.fromhex(encoded)
    key_id = _key_id(key)
    if stored_id != key_id:
        raise InputError(_CORRUPT_KEY)

    # Python 3.10's fromisoformat() accepts +00:00 for UTC.
    timestamp = created_at[:-1] + "+00:00" if created_at.endswith("Z") else created_at
    try:
        offset = datetime.fromisoformat(timestamp).utcoffset()
    except (ValueError, OverflowError):
        raise InputError(_CORRUPT_KEY) from None
    if offset != timedelta(0):
        raise InputError(_CORRUPT_KEY)

    return MaskingKey(path, key_id, key, created_at, permission_warning)


def read_masking_key(path: Path | None = None) -> MaskingKey | None:
    """Read the key without creating it. Invalid existing files raise InputError."""
    path = path if path is not None else masking_key_path()
    try:
        with path.open(encoding="utf-8") as stream:
            document: object = json.load(stream)
            shared = os.name == "posix" and os.fstat(stream.fileno()).st_mode & 0o044
    except FileNotFoundError:
        return None
    except (ValueError, RecursionError):
        raise InputError(_CORRUPT_KEY) from None
    except OSError:
        raise InputError("마스킹 키 파일을 읽을 수 없습니다.") from None

    return _parse_key(path, document, _PERMISSION_WARNING if shared else None)


def _ensure_private_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700, exist_ok=True)
    except FileNotFoundError:
        _ensure_private_directory(path.parent)
        path.mkdir(mode=0o700, exist_ok=True)


def _write_new_file(path: Path, data: bytes) -> bool:
    """Create the destination exclusively, returning false if it already exists."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        return False
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        # Only this call created the file, so an incomplete one is safe to remove.
        path.unlink(missing_ok=True)
        raise
    return True


def _publish_key(path: Path, key: MaskingKey, *, overwrite: bool = False) -> bool:
    """Publish a complete file atomically, returning false for an existing destination."""
    document = {
        "version": _KEY_VERSION,
        "key_id": key.key_id,
        "key": key.key.hex(),
        "created_at": key.created_at,
    }
    try:
        _ensure_private_directory(path.parent)
        # mkstemp uses an exclusive os.open with mode 0600.
        fd, staging_name = tempfile.mkstemp(prefix=".masking-key-", suffix=".tmp", dir=path.parent)
        staging = Path(staging_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(document, stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            if overwrite:
                staging.replace(path)
            else:
                try:
                    os.link(staging, path)
                except FileExistsError:
                    return False
                except OSError:
                    # FAT·exFAT 등 하드 링크가 없는 저장 장치에는 새 파일로만 직접 쓴다.
                    return _write_new_file(path, staging.read_bytes())
        finally:
            staging.unlink(missing_ok=True)
    except OSError:
        raise InputError("마스킹 키 파일을 저장할 수 없습니다.") from None
    return True


def _require_key(path: Path) -> MaskingKey:
    key = read_masking_key(path)
    if key is None:
        raise InputError(_MISSING_KEY)
    return key


@contextmanager
def _replacement_lock(path: Path) -> Iterator[None]:
    """Serialize rotations using an empty sibling that survives process exit."""
    error = "마스킹 키 교체를 잠글 수 없습니다."
    try:
        _ensure_private_directory(path.parent)
        lock = path.with_name(f".{path.name}.lock")
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        raise InputError(error) from None
    try:
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError:
            raise InputError(error) from None
        yield
    finally:
        # Both advisory locks are released when the descriptor closes.
        os.close(fd)


def get_or_create_masking_key(path: Path | None = None) -> tuple[MaskingKey, bool]:
    """Return the active key and whether this call created it."""
    path = path if path is not None else masking_key_path()
    existing = read_masking_key(path)
    if existing is not None:
        return existing, False

    key = secrets.token_bytes(_KEY_BYTES)
    created = MaskingKey(path, _key_id(key), key, datetime.now(timezone.utc).isoformat(), None)
    if _publish_key(path, created):
        return created, True
    return _require_key(path), False


def export_masking_key(destination: Path) -> MaskingKey:
    """Copy the active key to a new file without modifying existing files."""
    active = _require_key(masking_key_path())
    if not _publish_key(destination, active):
        raise InputError("내보내기 위치에 파일이 이미 있습니다.")
    return replace(active, path=destination, permission_warning=None)


def import_masking_key(source: Path, *, replace: bool = False) -> tuple[MaskingKey, bool]:
    """Install a key and return whether the active key changed."""
    incoming = _require_key(source)
    path = masking_key_path()
    if replace:
        with _replacement_lock(path):
            return _install_key(path, incoming, allow_replace=True)
    return _install_key(path, incoming, allow_replace=False)


def _install_key(
    path: Path, incoming: MaskingKey, *, allow_replace: bool
) -> tuple[MaskingKey, bool]:
    active = read_masking_key(path)
    if active is None:
        if _publish_key(path, incoming):
            return replace(incoming, path=path, permission_warning=None), True
        active = _require_key(path)

    if hmac.compare_digest(active.key, incoming.key):
        return active, False
    if not allow_replace:
        raise InputError("이미 다른 마스킹 키가 있습니다. 교체하려면 --replace를 지정하세요.")

    backup = path.with_name(f"masking_key.{active.key_id}.bak.json")
    if not _publish_key(backup, active) and _require_key(backup).key != active.key:
        raise InputError("기존 마스킹 키의 백업이 일치하지 않습니다.")
    _publish_key(path, incoming, overwrite=True)
    return replace(incoming, path=path, permission_warning=None), True
