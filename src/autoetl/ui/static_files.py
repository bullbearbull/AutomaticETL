"""새 웹 화면(`/next/`) 빌드 결과(`autoetl/ui/static/`)를 메모리에 올려 두고 찾는 모듈.

서버 기동 시 정적 폴더를 한 번만 읽어 상대 POSIX 경로 → (본문, MIME 형식) 사전으로 보관한다.
요청 경로는 이 사전에서 정확히 같은 항목만 찾으므로 파일 시스템에 닿지 않는다.
"""

from __future__ import annotations

import re
import warnings
from collections.abc import Iterator
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, TypeAlias

if TYPE_CHECKING:
    from importlib.abc import Traversable

StaticBundle: TypeAlias = dict[str, tuple[bytes, str]]

_MAX_STATIC_BUNDLE_BYTES = 20 * 1024 * 1024
_STATIC_MIME_BY_EXT: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".woff2": "font/woff2",
}
_PATH_SEGMENT_RE = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]*")

STATIC_RESPONSE_HEADERS: dict[str, str] = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
        "frame-ancestors 'none'; form-action 'self'"
    ),
    "Referrer-Policy": "no-referrer",
}


def _static_root() -> Traversable:
    """패키지에 들어 있는 정적 폴더(`autoetl/ui/static/`) 위치를 반환한다."""
    return resources.files("autoetl.ui").joinpath("static")


def _iter_static_files(root: Traversable) -> Iterator[tuple[str, Traversable, str]]:
    """정적 폴더 안의 허용 확장자 일반 파일을 (상대 POSIX 경로, 항목, MIME 형식)으로 낸다.

    이름이 `.`으로 시작하는 항목과 실제 경로가 정적 폴더 밖인 항목(밖을 가리키는 심볼릭
    링크)은 건너뛰고, 이미 들른 폴더는 다시 들어가지 않는다.
    """
    root_real = Path(str(root)).resolve()
    seen_dirs = {root_real}
    pending: list[tuple[Traversable, str]] = [(root, "")]
    while pending:
        folder, prefix = pending.pop()
        for entry in sorted(folder.iterdir(), key=lambda item: item.name):
            if entry.name.startswith("."):
                continue
            rel_path = prefix + entry.name
            if entry.is_dir():
                real_dir = Path(str(entry)).resolve()
                if real_dir.is_relative_to(root_real) and real_dir not in seen_dirs:
                    seen_dirs.add(real_dir)
                    pending.append((entry, rel_path + "/"))
                continue
            content_type = _STATIC_MIME_BY_EXT.get(Path(entry.name).suffix.lower())
            if content_type is None or not entry.is_file():
                continue
            if Path(str(entry)).resolve().is_relative_to(root_real):
                yield rel_path, entry, content_type


def load_static_bundle() -> StaticBundle:
    """정적 폴더의 허용 확장자 파일을 읽어 상대 POSIX 경로 → (본문, MIME 형식) 사전을 만든다.

    합계 크기가 상한(20MB)을 넘게 하는 파일은 읽지 않고 경고를 남긴다. 정적 폴더가 없으면
    빈 사전을 반환하므로 서버는 그대로 기동한다. 정적 폴더를 읽다 OS 오류가 나도 경고만 남기고
    빈 사전을 반환하므로, 기존 화면(`/`)은 `/next/` 없이 그대로 쓸 수 있다.
    """
    root = _static_root()
    bundle: StaticBundle = {}
    budget = _MAX_STATIC_BUNDLE_BYTES
    skipped: list[str] = []
    try:
        if not root.is_dir():
            return bundle
        for rel_path, entry, content_type in _iter_static_files(root):
            with entry.open("rb") as fh:
                body = fh.read(budget + 1)
            if len(body) > budget:
                skipped.append(rel_path)
                continue
            budget -= len(body)
            bundle[rel_path] = (body, content_type)
    except OSError as exc:
        warnings.warn(
            f"`/next/` 정적 폴더를 읽지 못해 새 화면 없이 기동한다: {exc}",
            RuntimeWarning,
            stacklevel=2,
        )
        return {}
    if skipped:
        max_mb = _MAX_STATIC_BUNDLE_BYTES // (1024 * 1024)
        warnings.warn(
            f"`/next/` 정적 파일 합계가 {max_mb}MB 상한을 넘어 파일 {len(skipped)}개를 "
            f"읽지 않았다: {', '.join(skipped)}",
            RuntimeWarning,
            stacklevel=2,
        )
    return bundle


def lookup_static_file(bundle: StaticBundle, rel_path: str) -> tuple[bytes, str] | None:
    """`/next/` 뒤 상대 경로(비었으면 `index.html`)를 검증해 번들에서 정확히 같은 항목을 찾는다.

    경로 조각마다 영문·숫자·`_`·`-`로 시작하고 영문·숫자·`.`·`_`·`-`만 담아야 한다. 빈 조각(`//`,
    끝의 `/`), `.`·`..`, `%` 인코딩, 역슬래시, NUL, `:`, `.`으로 시작하는 이름은 찾지 않고
    `None`을 반환한다.
    """
    path = rel_path or "index.html"
    if not all(_PATH_SEGMENT_RE.fullmatch(segment) for segment in path.split("/")):
        return None
    return bundle.get(path)


__all__ = [
    "STATIC_RESPONSE_HEADERS",
    "StaticBundle",
    "load_static_bundle",
    "lookup_static_file",
]
