"""Unified diff 파싱, 생성/바이너리 파일 필터링 및 라인 번호 검증."""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field, replace

TRUNCATION_MARK = "\n[... truncated ...]"
HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


@dataclass
class FileDiff:
    path: str
    status: str  # added | modified | deleted | renamed
    is_binary: bool
    patch: str
    valid_lines: set[int] = field(default_factory=set)


def _split_diff_lines(text: str) -> list[str]:
    """git unified diff 규칙(LF/CRLF만 줄바꿈)에 따라 줄을 분리한다.

    Python ``str.splitlines()``는 폼피드(``\\x0c``)·유니코드 줄구분자 등에서도
    줄을 갈라 diff 라인 번호가 밀릴 수 있으므로 ``\\n`` 기준으로만 나눈다.
    """
    if not text:
        return []
    raw_lines = text.split("\n")
    if raw_lines and raw_lines[-1] == "":
        raw_lines.pop()
    return [line[:-1] if line.endswith("\r") else line for line in raw_lines]


def _unquote(token: str) -> str:
    """git C-스타일 따옴표 경로(8진수·이스케이프 포함)를 UTF-8 문자열로 디코딩한다."""
    if not (len(token) >= 2 and token[0] == '"' and token[-1] == '"'):
        return token
    body = token[1:-1]
    out = bytearray()
    i = 0
    simple = {"n": 10, "t": 9, "r": 13, '"': 34, "\\": 92}
    while i < len(body):
        c = body[i]
        if c == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            if nxt in simple:
                out.append(simple[nxt])
                i += 2
                continue
            m = re.match(r"[0-7]{1,3}", body[i + 1 :])
            if m:
                out.append(int(m.group(0), 8))
                i += 1 + len(m.group(0))
                continue
        out.extend(c.encode("utf-8"))
        i += 1
    return out.decode("utf-8", errors="replace")


def _strip_prefix(path: str) -> str:
    path = _unquote(path)
    return path[2:] if path[:2] in ("a/", "b/") else path


def _header_path(header: str) -> str:
    rest = header[len("diff --git ") :]
    if rest.startswith('"'):
        m = re.match(r'("(?:[^"\\]|\\.)*")\s+("(?:[^"\\]|\\.)*"|\S.*)$', rest)
        if m:
            return _strip_prefix(m.group(2))
    half = (len(rest) - 5) // 2
    return rest[2 : 2 + half] if half > 0 else rest


def _parse_section(lines: list[str]) -> FileDiff:
    status = "modified"
    is_binary = False
    rename_to: str | None = None
    plus: str | None = None
    minus: str | None = None
    for line in lines[1:]:
        if line.startswith("@@"):
            break
        if line.startswith("new file mode"):
            status = "added"
        elif line.startswith("deleted file mode"):
            status = "deleted"
        elif line.startswith("rename to "):
            status = "renamed"
            rename_to = _unquote(line[len("rename to ") :])
        elif line.startswith("Binary files") or line.startswith("GIT binary patch"):
            is_binary = True
        elif line.startswith("+++ "):
            plus = line[4:].rstrip("\t\r")
        elif line.startswith("--- "):
            minus = line[4:].rstrip("\t\r")
    if rename_to:
        path = rename_to
    elif plus and plus != "/dev/null":
        path = _strip_prefix(plus)
    elif minus and minus != "/dev/null":
        path = _strip_prefix(minus)
    else:
        path = _header_path(lines[0])

    valid: set[int] = set()
    new_line: int | None = None
    for line in lines[1:]:
        m = HUNK_RE.match(line)
        if m:
            new_line = int(m.group(1))
            continue
        if new_line is None or line.startswith("\\"):
            continue
        if line.startswith("-"):
            continue
        # 빈 문자열("")은 일부 도구가 선행 공백 1칸을 제거한 컨텍스트 줄을 뜻한다.
        if line.startswith(("+", " ")) or line == "":
            valid.add(new_line)
            new_line += 1
    return FileDiff(path, status, is_binary, "\n".join(lines) + "\n", valid)


def parse_diff(text: str) -> list[FileDiff]:
    """unified diff 문자열을 파일 단위 ``FileDiff`` 목록으로 파싱한다."""
    sections: list[list[str]] = []
    for line in _split_diff_lines(text):
        if line.startswith("diff --git "):
            sections.append([line])
        elif sections:
            sections[-1].append(line)
    return [_parse_section(s) for s in sections]


_GENERATED = [
    "*.lock",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "poetry.lock",
    "*.min.js",
    "*.min.css",
    "*_pb2.py",
    "*_pb2_grpc.py",
]
_GENERATED_DIRS = ("dist/", "build/", "node_modules/", "vendor/")
_SENSITIVE = (".env*", "*.pem", "*.key", "*.p12", "*.pfx")
_SENSITIVE_DIRS = ("secrets/", ".git/")
_SENSITIVE_EXACT = frozenset({".git/config"})


def _is_generated(path: str) -> bool:
    base = path.rsplit("/", 1)[-1]
    if any(fnmatch.fnmatch(base, pat) for pat in _GENERATED):
        return True
    return any(path.startswith(d) or f"/{d}" in path for d in _GENERATED_DIRS)


def is_sensitive_path(path: str) -> bool:
    """민감 경로(.env·키 파일·secrets/·.git/ 등)가 모델 프롬프트나 도구로 노출되지 않게 판정한다."""
    norm = path.replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    norm = norm.strip("/").lower()
    if not norm:
        return False
    if norm in _SENSITIVE_EXACT:
        return True
    norm_with_slash = f"{norm}/"
    if any(ex.startswith(norm_with_slash) for ex in _SENSITIVE_EXACT):
        return True
    base = norm.rsplit("/", 1)[-1]
    if any(fnmatch.fnmatch(base, pat) for pat in _SENSITIVE):
        return True
    return any(norm_with_slash.startswith(d) or f"/{d}" in norm_with_slash for d in _SENSITIVE_DIRS)


def filter_files(files: list[FileDiff]) -> list[FileDiff]:
    """바이너리·삭제·자동생성/잠금·민감 경로 파일을 리뷰 대상에서 제외한다."""
    return [
        f
        for f in files
        if not f.is_binary
        and f.status != "deleted"
        and not _is_generated(f.path)
        and not is_sensitive_path(f.path)
    ]


def truncate_files(files: list[FileDiff], max_chars: int) -> tuple[list[FileDiff], list[str]]:
    """총 길이가 ``max_chars``를 넘지 않도록 파일 목록을 자르고 생략된 경로를 반환한다."""
    kept: list[FileDiff] = []
    omitted: list[str] = []
    total = 0
    for f in files:
        if omitted:
            omitted.append(f.path)
        elif total + len(f.patch) <= max_chars:
            kept.append(f)
            total += len(f.patch)
        elif not kept:
            kept.append(replace(f, patch=f.patch[:max_chars] + TRUNCATION_MARK))
            total = max_chars
        else:
            omitted.append(f.path)
    return kept, omitted


def is_valid_line(files: list[FileDiff], path: str, line: int) -> bool:
    """``(path, line)``이 diff의 변경 후 파일(RIGHT) 유효 라인인지 확인한다."""
    return any(f.path == path and line in f.valid_lines for f in files)
