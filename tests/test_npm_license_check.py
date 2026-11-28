"""tools/npm_license_check.py 단위 테스트."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "npm_license_check",
    Path(__file__).resolve().parents[1] / "tools" / "npm_license_check.py",
)
assert _SPEC is not None and _SPEC.loader is not None
npm_license_check = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(npm_license_check)

_BUNDLE_HEADER = (
    "# Licenses\n\nThe app bundles dependencies which contain the following licenses:\n"
)


def _lock_entry(version: str, license_id: str | None, *, dev: bool = False) -> dict[str, Any]:
    entry: dict[str, Any] = {"version": version, "resolved": "https://registry.example/x.tgz"}
    if license_id is not None:
        entry["license"] = license_id
    if dev:
        entry["dev"] = True
    return entry


def _base_packages() -> dict[str, dict[str, Any]]:
    return {
        "": {"name": "frontend", "version": "0.0.0", "dependencies": {"react": "19.3.1"}},
        "node_modules/react": _lock_entry("19.3.1", "MIT"),
        "node_modules/@scope/widget": _lock_entry("1.0.0", "Apache-2.0"),
        "node_modules/vite": _lock_entry("8.3.8", "MIT", dev=True),
        "node_modules/vite/node_modules/@scope/inner": _lock_entry(
            "2.0.0", "MIT OR Apache-2.0", dev=True
        ),
    }


def _bundle_text(*packages: tuple[str, str, str | None]) -> str:
    text = _BUNDLE_HEADER
    for name, version, license_id in packages:
        suffix = f" ({license_id})" if license_id is not None else ""
        text += f"\n## {name} - {version}{suffix}\n\nLicense text.\n"
    return text


class Repo:
    """tmp_path 안에 lockfile·번들 라이선스·고지 파일을 만든다."""

    def __init__(self, root: Path) -> None:
        self.lockfile = root / "package-lock.json"
        self.bundle = root / "THIRD_PARTY_LICENSES.md"
        self.notices = root / "THIRD_PARTY_NOTICES.md"
        self.write_lock(_base_packages())
        self.bundle.write_text(_bundle_text(("react", "19.3.1", "MIT")), encoding="utf-8")
        self.notices.write_text("| `react` | MIT | UI |\n", encoding="utf-8")

    def write_lock(self, packages: dict[str, dict[str, Any]], version: int = 3) -> None:
        lock = {"name": "frontend", "lockfileVersion": version, "packages": packages}
        self.lockfile.write_text(json.dumps(lock), encoding="utf-8")

    def run(self) -> int:
        result: int = npm_license_check.main(
            [
                "--lockfile",
                str(self.lockfile),
                "--bundle-licenses",
                str(self.bundle),
                "--notices",
                str(self.notices),
            ]
        )
        return result


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    return Repo(tmp_path)


def test_shipped_set_excludes_mpl() -> None:
    assert "MPL-2.0" in npm_license_check.ALLOWED_LICENSES
    assert "MPL-2.0" not in npm_license_check.SHIPPED_LICENSES
    assert npm_license_check.ALLOWED_LICENSES - {"MPL-2.0"} == npm_license_check.SHIPPED_LICENSES


def test_evaluate_uses_given_allowed_set() -> None:
    evaluate = npm_license_check.evaluate_license_expression
    shipped = npm_license_check.SHIPPED_LICENSES
    assert evaluate("MPL-2.0", npm_license_check.ALLOWED_LICENSES)
    assert not evaluate("MPL-2.0", shipped)
    assert evaluate("MPL-2.0 OR MIT", shipped)
    assert not evaluate("MPL-2.0 AND MIT", shipped)


@pytest.mark.parametrize(
    ("key", "name"),
    [
        ("node_modules/react", "react"),
        ("node_modules/@scope/widget", "@scope/widget"),
        ("node_modules/a/node_modules/b", "b"),
        ("node_modules/a/node_modules/@s/b", "@s/b"),
        ("node_modules/@s/a/node_modules/lightningcss-linux-x64-gnu", "lightningcss-linux-x64-gnu"),
    ],
)
def test_package_name_from_key(key: str, name: str) -> None:
    assert npm_license_check.package_name_from_key(key) == name


def test_pass(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    assert repo.run() == 0
    out = capsys.readouterr().out
    assert "@scope/inner" in out
    assert "모든 npm 의존성" in out


def test_missing_license_names_package(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    packages = _base_packages()
    packages["node_modules/a/node_modules/@s/nolicense"] = _lock_entry("1.0.0", None)
    repo.write_lock(packages)
    assert repo.run() == 1
    out = capsys.readouterr().out
    assert "거부  @s/nolicense 1.0.0" in out
    assert "license 값 없음" in out


def test_non_dev_mpl_fails_even_on_exception_list(
    repo: Repo, capsys: pytest.CaptureFixture[str]
) -> None:
    packages = _base_packages()
    packages["node_modules/lightningcss"] = _lock_entry("1.33.0", "MPL-2.0")
    repo.write_lock(packages)
    assert repo.run() == 1
    assert "거부  lightningcss 1.33.0  MPL-2.0  [런타임]" in capsys.readouterr().out


def test_dev_mpl_not_on_exception_list_fails(
    repo: Repo, capsys: pytest.CaptureFixture[str]
) -> None:
    packages = _base_packages()
    packages["node_modules/some-mpl-tool"] = _lock_entry("1.0.0", "MPL-2.0", dev=True)
    repo.write_lock(packages)
    assert repo.run() == 1
    assert "거부  some-mpl-tool 1.0.0  MPL-2.0  [개발]" in capsys.readouterr().out


@pytest.mark.parametrize(
    "key",
    [
        "node_modules/axe-core",
        "node_modules/@axe-core/playwright",
        "node_modules/lightningcss",
        "node_modules/lightningcss-win32-x64-msvc",
        "node_modules/vite/node_modules/lightningcss-darwin-arm64",
    ],
)
def test_dev_mpl_on_exception_list_passes(repo: Repo, key: str) -> None:
    packages = _base_packages()
    packages[key] = _lock_entry("1.0.0", "MPL-2.0", dev=True)
    repo.write_lock(packages)
    assert repo.run() == 0


def test_exception_does_not_allow_other_licenses(repo: Repo) -> None:
    packages = _base_packages()
    packages["node_modules/axe-core"] = _lock_entry("4.14.1", "GPL-3.0-only", dev=True)
    repo.write_lock(packages)
    assert repo.run() == 1


def test_exception_name_must_match_exactly(repo: Repo) -> None:
    packages = _base_packages()
    packages["node_modules/axe-core-extra"] = _lock_entry("1.0.0", "MPL-2.0", dev=True)
    repo.write_lock(packages)
    assert repo.run() == 1


def test_malformed_license_expression_fails(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    packages = _base_packages()
    packages["node_modules/odd"] = _lock_entry("1.0.0", "SEE LICENSE IN LICENSE.txt")
    repo.write_lock(packages)
    assert repo.run() == 1
    assert "SPDX 식 파싱 실패" in capsys.readouterr().out


@pytest.mark.parametrize("version", [1, 2])
def test_wrong_lockfile_version_fails(
    repo: Repo, version: int, capsys: pytest.CaptureFixture[str]
) -> None:
    repo.write_lock(_base_packages(), version=version)
    assert repo.run() == 1
    assert "lockfileVersion" in capsys.readouterr().err


def test_missing_lockfile_fails(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    repo.lockfile.unlink()
    assert repo.run() == 1
    assert "lockfile이 없다" in capsys.readouterr().err


def test_bundled_mpl_fails(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    repo.bundle.write_text(
        _bundle_text(("react", "19.3.1", "MIT"), ("axe-core", "4.14.1", "MPL-2.0")),
        encoding="utf-8",
    )
    repo.notices.write_text("`react` `axe-core`\n", encoding="utf-8")
    assert repo.run() == 1
    assert "거부  axe-core 4.14.1  MPL-2.0  [번들]" in capsys.readouterr().out


def test_bundled_package_missing_from_notices_fails(
    repo: Repo, capsys: pytest.CaptureFixture[str]
) -> None:
    repo.bundle.write_text(
        _bundle_text(("react", "19.3.1", "MIT"), ("react-dom", "19.3.1", "MIT")),
        encoding="utf-8",
    )
    assert repo.run() == 1
    out = capsys.readouterr().out
    assert "거부  react-dom 19.3.1  MIT  [번들]" in out
    assert "THIRD_PARTY_NOTICES.md에 없음" in out


def test_bundled_scoped_package_and_compound_license(repo: Repo) -> None:
    repo.bundle.write_text(
        _bundle_text(
            ("react", "19.3.1", "MIT"),
            ("@internationalized/string", "3.2.10", "Apache-2.0"),
            ("dual", "1.0.0", "(MIT OR GPL-3.0-only)"),
        ),
        encoding="utf-8",
    )
    repo.notices.write_text("`react` `@internationalized/string` `dual`\n", encoding="utf-8")
    assert repo.run() == 0


def test_bundled_package_without_license_fails(repo: Repo) -> None:
    repo.bundle.write_text(_bundle_text(("react", "19.3.1", None)), encoding="utf-8")
    assert repo.run() == 1


def test_missing_bundle_file_fails(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    repo.bundle.unlink()
    assert repo.run() == 1
    assert "번들 라이선스 파일이 없다" in capsys.readouterr().err


def test_empty_bundle_passes(repo: Repo) -> None:
    repo.bundle.write_text(
        "# Licenses\n\nThe app does not bundle any dependencies with licenses.\n",
        encoding="utf-8",
    )
    assert repo.run() == 0


def test_unrecognised_bundle_format_fails(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    repo.bundle.write_text("# Licenses\n\n### react 19.3.1 MIT\n", encoding="utf-8")
    assert repo.run() == 1
    assert "형식" in capsys.readouterr().err


def test_license_text_headings_are_not_packages() -> None:
    text = _bundle_text(("react", "19.3.1", "MIT")) + "\n## Terms and Conditions\n"
    entries, errors = npm_license_check.parse_bundle_licenses(text)
    assert entries == [("react", "19.3.1", "MIT")]
    assert errors == []
