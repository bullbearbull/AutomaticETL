"""패키지 기본 메타 검증."""

import re

import autoetl

_PEP440_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?$")


def test_version_is_semver() -> None:
    assert _PEP440_SEMVER_RE.match(autoetl.__version__) is not None
