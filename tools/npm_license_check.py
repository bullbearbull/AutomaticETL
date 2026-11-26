"""웹 UI(frontend/) npm 의존성 라이선스 자동 검사.

표준 라이브러리만 쓴다. 라이선스 식 판정은 tools/license_check.py를 그대로 쓴다.

    uv run python tools/npm_license_check.py

검사 규칙:

1. frontend/package-lock.json은 lockfileVersion 3이어야 하고, 루트("")를 뺀 모든
   packages 항목에 license 값이 있어야 한다.
2. 배포 허용 목록(SHIPPED)은 license_check.ALLOWED_LICENSES에서 MPL-2.0을 뺀 것이다.
   런타임(dev가 아닌) 항목은 SHIPPED 안에 들어야 한다.
3. dev: true 항목도 SHIPPED 안에 들어야 한다. 단, 개발·테스트 전용 도구로 승인된
   DEV_MPL_EXCEPTIONS(axe-core, @axe-core/playwright, lightningcss, lightningcss-*)만
   MPL-2.0을 허용한다.
4. Vite가 빌드 때 만드는 src/autoetl/ui/static/THIRD_PARTY_LICENSES.md의 번들 패키지는
   모두 SHIPPED 안에 들어야 하고, 루트 THIRD_PARTY_NOTICES.md에 `이름` 형태(백틱)로
   적혀 있어야 한다.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any, NamedTuple

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOCKFILE = ROOT / "frontend" / "package-lock.json"
DEFAULT_BUNDLE_LICENSES = ROOT / "src" / "autoetl" / "ui" / "static" / "THIRD_PARTY_LICENSES.md"
DEFAULT_NOTICES = ROOT / "THIRD_PARTY_NOTICES.md"

REQUIRED_LOCKFILE_VERSION = 3


def _load_license_check() -> tuple[Callable[[str, Iterable[str]], bool], frozenset[str]]:
    path = Path(__file__).resolve().with_name("license_check.py")
    spec = importlib.util.spec_from_file_location("license_check", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    evaluate: Callable[[str, Iterable[str]], bool] = module.evaluate_license_expression
    allowed: frozenset[str] = module.ALLOWED_LICENSES
    return evaluate, allowed


evaluate_license_expression, ALLOWED_LICENSES = _load_license_check()

# 웹 UI 번들과 런타임 의존성에 허용하는 라이선스. MPL-2.0은 배포물에 넣지 않는다.
SHIPPED_LICENSES = ALLOWED_LICENSES - {"MPL-2.0"}

# 개발·테스트 전용으로만 MPL-2.0을 허용하는 패키지. 소유자 승인 사항이다.
DEV_MPL_EXCEPTIONS = frozenset({"axe-core", "@axe-core/playwright", "lightningcss"})
DEV_MPL_EXCEPTION_PREFIXES = ("lightningcss-",)

# Vite build.license 마크다운의 패키지 제목: "## <이름> - <버전>" 또는 "... (<SPDX>)".
_BUNDLE_HEADING_RE = re.compile(
    r"^## (?P<name>(?:@[A-Za-z0-9][A-Za-z0-9._~-]*/)?[A-Za-z0-9][A-Za-z0-9._~-]*)"
    r" - (?P<version>[0-9][^\s()]*)(?: \((?P<license>.*)\))?$"
)
_BUNDLE_TITLE = "# Licenses"
_BUNDLE_INTRO_SOME = "The app bundles dependencies which contain the following licenses:"
_BUNDLE_INTRO_NONE = "The app does not bundle any dependencies with licenses."


class Verdict(NamedTuple):
    """패키지 하나의 판정 결과."""

    name: str
    version: str
    license: str
    kind: str
    allowed: bool
    reason: str = ""


def package_name_from_key(key: str) -> str:
    """lockfile packages 키에서 패키지 이름을 얻는다.

    "node_modules/a/node_modules/@s/b" → "@s/b". node_modules가 없는 키(작업 공간
    경로 등)는 키 그대로 돌려준다.
    """
    marker = "node_modules/"
    index = key.rfind(marker)
    return key[index + len(marker) :] if index >= 0 else key


def is_dev_mpl_exception(name: str) -> bool:
    return name in DEV_MPL_EXCEPTIONS or name.startswith(DEV_MPL_EXCEPTION_PREFIXES)


def _evaluate(expr: str, allowed: Iterable[str]) -> tuple[bool, str]:
    try:
        return evaluate_license_expression(expr, allowed), ""
    except ValueError as exc:
        return False, f"SPDX 식 파싱 실패({exc})"


def check_lock_entry(key: str, entry: dict[str, Any]) -> Verdict:
    """lockfile packages 항목 하나를 판정한다."""
    name = str(entry.get("name") or package_name_from_key(key))
    version = str(entry.get("version", "?"))
    dev = entry.get("dev") is True
    kind = "개발" if dev else "런타임"
    raw = entry.get("license")
    if raw is None:
        return Verdict(name, version, "", kind, False, "license 값 없음")
    if not isinstance(raw, str) or not raw.strip():
        return Verdict(name, version, repr(raw), kind, False, "license 값이 SPDX 문자열이 아님")
    expr = raw.strip()
    allowed, reason = _evaluate(expr, SHIPPED_LICENSES)
    if allowed:
        return Verdict(name, version, expr, kind, True)
    if reason:
        return Verdict(name, version, expr, kind, False, reason)
    if dev and is_dev_mpl_exception(name):
        allowed, reason = _evaluate(expr, ALLOWED_LICENSES)
        if allowed:
            return Verdict(name, version, expr, "개발(MPL-2.0 예외)", True)
    if dev:
        return Verdict(name, version, expr, kind, False, "개발 의존성 허용 목록에 없음")
    return Verdict(name, version, expr, kind, False, "배포 허용 목록에 없음")


def check_lockfile(lock: object) -> tuple[list[Verdict], list[str]]:
    """lockfile 전체를 판정한다. 반환은 (패키지별 판정, 파일 단위 오류)."""
    if not isinstance(lock, dict):
        return [], ["package-lock.json 최상위가 JSON 객체가 아니다."]
    version = lock.get("lockfileVersion")
    if version != REQUIRED_LOCKFILE_VERSION:
        return [], [
            f"lockfileVersion {version!r}은(는) 지원하지 않는다. "
            f"{REQUIRED_LOCKFILE_VERSION}이어야 한다(npm 9 이상으로 다시 만든다)."
        ]
    packages = lock.get("packages")
    if not isinstance(packages, dict):
        return [], ["package-lock.json에 packages 객체가 없다."]
    verdicts: list[Verdict] = []
    errors: list[str] = []
    for key, entry in packages.items():
        if key == "":
            continue
        if not isinstance(entry, dict):
            errors.append(f"packages[{key!r}] 항목이 JSON 객체가 아니다.")
            continue
        verdicts.append(check_lock_entry(key, entry))
    return verdicts, errors


def parse_bundle_licenses(text: str) -> tuple[list[tuple[str, str, str | None]], list[str]]:
    """Vite build.license 마크다운에서 (이름, 버전, SPDX 또는 None) 목록을 읽는다."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != _BUNDLE_TITLE:
        return [], [f"번들 라이선스 파일 첫 줄이 {_BUNDLE_TITLE!r}가 아니다(형식 확인 필요)."]
    has_some = _BUNDLE_INTRO_SOME in lines
    has_none = _BUNDLE_INTRO_NONE in lines
    entries: list[tuple[str, str, str | None]] = []
    for line in lines:
        match = _BUNDLE_HEADING_RE.match(line)
        if match:
            entries.append((match["name"], match["version"], match["license"]))
    if has_none and not entries:
        return [], []
    if has_some and entries:
        return entries, []
    return entries, ["번들 라이선스 파일 형식을 알아볼 수 없다(Vite build.license 형식 확인 필요)."]


def check_bundle(
    entries: Iterable[tuple[str, str, str | None]], notices_text: str
) -> list[Verdict]:
    """번들에 들어간 패키지를 판정한다."""
    verdicts: list[Verdict] = []
    for name, version, expr in entries:
        if not expr:
            verdicts.append(Verdict(name, version, "", "번들", False, "라이선스 표기 없음"))
            continue
        allowed, reason = _evaluate(expr, SHIPPED_LICENSES)
        if not allowed:
            verdicts.append(
                Verdict(name, version, expr, "번들", False, reason or "배포 허용 목록에 없음")
            )
        elif f"`{name}`" not in notices_text:
            verdicts.append(
                Verdict(name, version, expr, "번들", False, "THIRD_PARTY_NOTICES.md에 없음")
            )
        else:
            verdicts.append(Verdict(name, version, expr, "번들", True))
    return verdicts


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _print_verdicts(verdicts: Iterable[Verdict]) -> int:
    failures = 0
    for v in verdicts:
        mark = "허용" if v.allowed else "거부"
        detail = f"  — {v.reason}" if v.reason else ""
        print(f"{mark}  {v.name} {v.version}  {v.license or '(라이선스 없음)'}  [{v.kind}]{detail}")
        failures += not v.allowed
    return failures


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="웹 UI npm 의존성 라이선스 검사")
    parser.add_argument("--lockfile", type=Path, default=DEFAULT_LOCKFILE)
    parser.add_argument("--bundle-licenses", type=Path, default=DEFAULT_BUNDLE_LICENSES)
    parser.add_argument("--notices", type=Path, default=DEFAULT_NOTICES)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """lockfile과 번들 라이선스를 검사하고 결과를 출력한다. 0이 통과다."""
    args = _parse_args(argv)
    lockfile: Path = args.lockfile
    bundle_path: Path = args.bundle_licenses
    notices_path: Path = args.notices
    errors: list[str] = []
    verdicts: list[Verdict] = []

    if not lockfile.is_file():
        errors.append(f"lockfile이 없다: {lockfile}")
    else:
        try:
            lock = json.loads(_read_text(lockfile))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            errors.append(f"lockfile을 읽지 못했다: {lockfile} ({exc})")
        else:
            lock_verdicts, lock_errors = check_lockfile(lock)
            verdicts += lock_verdicts
            errors += lock_errors

        if not bundle_path.is_file():
            errors.append(
                f"번들 라이선스 파일이 없다: {bundle_path} "
                "(frontend에서 npm run build로 만들어 함께 커밋한다)"
            )
        elif not notices_path.is_file():
            errors.append(f"THIRD_PARTY_NOTICES.md가 없다: {notices_path}")
        else:
            entries, bundle_errors = parse_bundle_licenses(_read_text(bundle_path))
            errors += bundle_errors
            verdicts += check_bundle(entries, _read_text(notices_path))

    failures = _print_verdicts(verdicts)
    counts = Counter(v.license or "(없음)" for v in verdicts if v.kind != "번들")
    if counts:
        summary = ", ".join(f"{lic} {n}" for lic, n in sorted(counts.items()))
        print(f"lockfile 라이선스 분포: {summary}")
    for error in errors:
        print(f"오류  {error}", file=sys.stderr)
    if failures or errors:
        print(
            f"npm 라이선스 검사 실패: 거부 {failures}건, 파일 오류 {len(errors)}건",
            file=sys.stderr,
        )
        return 1
    print("모든 npm 의존성과 번들 패키지가 허용형 라이선스 안에 있다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
