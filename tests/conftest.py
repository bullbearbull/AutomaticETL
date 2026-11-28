"""Keep every masking-key operation isolated from the user's configuration."""

import hmac
import json
import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_masking_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    key_file = tmp_path / "masking_key.json"
    monkeypatch.setenv("AUTOETL_MASKING_KEY_FILE", str(key_file))
    if request.node.path.name in {
        "test_etl_value_transforms.py",
        "test_etl_key_masking_reporting.py",
    }:
        # Existing assertions use fixed digests derived from a synthetic key.
        key = bytes(32)
        key_id = "k_" + hmac.digest(key, b"autoetl-masking-key-id", "sha256").hex()[:12]
        fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(
                {
                    "version": 1,
                    "key_id": key_id,
                    "key": key.hex(),
                    "created_at": "2031-01-01T00:00:00+00:00",
                },
                stream,
            )
