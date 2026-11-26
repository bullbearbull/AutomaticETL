"""새 웹 화면(`/next/`) 정적 파일 응답·경로 검증 테스트(실제 빌드 결과 없이 임시 폴더 사용)."""

from __future__ import annotations

import http.client
import json
import socket
from collections.abc import Iterator
from html.parser import HTMLParser
from importlib import resources
from pathlib import Path

import pytest

from autoetl.ui import create_ui_server, static_files
from autoetl.ui.server import LocalParserUiServer

_NOT_FOUND_BODY = {"error": "알 수 없는 경로다."}
_HOST_ERROR = "허용하지 않는 Host 헤더다."
_EXPECTED_CSP = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data:",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "frame-ancestors 'none'",
        "form-action 'self'",
    ]
)
_EXPECTED_MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".woff2": "font/woff2",
}
_INDEX_HTML = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>AutoETL</title>
<link rel="icon" href="/next/favicon.svg">
<link rel="stylesheet" href="/next/assets/index.css">
<script type="module" src="/next/assets/index.js"></script>
</head>
<body><div id="root"></div><img src="/next/assets/logo.png" alt=""></body>
</html>
"""
_SERVED_FILES: dict[str, bytes] = {
    "index.html": _INDEX_HTML.encode("utf-8"),
    "favicon.svg": b'<svg xmlns="http://www.w3.org/2000/svg"/>',
    "assets/index.js": b'console.log("next");',
    "assets/index.css": b"@font-face{font-family:x;src:url(/next/assets/font.woff2)}",
    "assets/logo.png": b"\x89PNG\r\n\x1a\n\x00\x00\x00\x00",
    "assets/font.woff2": b"wOF2\x00\x00\x00\x00",
}
_UNSERVED_FILES: dict[str, bytes] = {
    "THIRD_PARTY_LICENSES.md": b"# licenses",
    "x.py": b"print('x')",
    "manifest.json": b"{}",
    "assets/index.js.map": b"{}",
    ".hidden": b"hidden",
    ".hidden.js": b"hidden",
    ".cache/hidden.js": b"hidden",
}


def _write_files(root: Path, files: dict[str, bytes]) -> None:
    for rel_path, data in files.items():
        target = root / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def _get(port: int, path: str, *, host: str | None = None) -> tuple[int, dict[str, str], bytes]:
    """`http.client`로 경로를 그대로 보내 (상태, 소문자 헤더 사전, 본문)을 반환한다."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.putrequest("GET", path, skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", host or f"127.0.0.1:{port}")
        conn.endheaders()
        resp = conn.getresponse()
        headers = {name.lower(): value for name, value in resp.getheaders()}
        return resp.status, headers, resp.read()
    finally:
        conn.close()


def _raw_get(port: int, raw_path: bytes) -> tuple[int, bytes]:
    """`http.client`가 막는 바이트(NUL 등)가 든 요청을 소켓으로 보내 (상태, 본문)을 반환한다."""
    request = (
        b"GET " + raw_path + b" HTTP/1.1\r\n"
        b"Host: 127.0.0.1:" + str(port).encode("ascii") + b"\r\n"
        b"Connection: close\r\n\r\n"
    )
    chunks: list[bytes] = []
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(request)
        while chunk := sock.recv(65536):
            chunks.append(chunk)
    head, _, body = b"".join(chunks).partition(b"\r\n\r\n")
    return int(head.split(b" ", 2)[1]), body


def _assert_not_found(status: int, headers: dict[str, str], body: bytes) -> None:
    assert status == 404
    assert headers["content-type"] == "application/json; charset=utf-8"
    assert json.loads(body.decode("utf-8")) == _NOT_FOUND_BODY


def _assert_still_serving(port: int) -> None:
    status, _, body = _get(port, "/next/")
    assert status == 200
    assert body == _SERVED_FILES["index.html"]


class _RefCollector(HTMLParser):
    """HTML 태그의 `src`·`href` 속성 값을 모은다."""

    def __init__(self) -> None:
        super().__init__()
        self.refs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.refs.extend(value for name, value in attrs if name in ("src", "href") and value)


def _index_refs(html: str) -> list[str]:
    collector = _RefCollector()
    collector.feed(html)
    collector.close()
    return collector.refs


@pytest.fixture(scope="module")
def static_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("next_static") / "static"
    _write_files(root, _SERVED_FILES | _UNSERVED_FILES)
    return root


@pytest.fixture(scope="module")
def next_server(static_root: Path) -> Iterator[LocalParserUiServer]:
    """임시 정적 폴더로 기동한 서버. 번들은 서버를 만들 때 읽으므로 바꾼 위치는 곧바로 되돌린다."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(static_files, "_static_root", lambda: static_root)
        srv = create_ui_server(host="127.0.0.1", port=0)
    with srv:
        yield srv


def test_load_static_bundle_reads_only_allowed_files(
    static_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """허용 확장자 일반 파일만 상대 POSIX 경로 → (본문, 정확한 MIME 형식)으로 읽는다."""
    monkeypatch.setattr(static_files, "_static_root", lambda: static_root)
    bundle = static_files.load_static_bundle()
    assert bundle == {
        rel_path: (data, _EXPECTED_MIME[Path(rel_path).suffix])
        for rel_path, data in _SERVED_FILES.items()
    }


@pytest.mark.parametrize("path", ["/next/", "/next/index.html"])
def test_next_index_is_served_with_security_headers(
    next_server: LocalParserUiServer, path: str
) -> None:
    """`/next/`와 `/next/index.html`은 index.html을 보안 헤더와 함께 200으로 응답한다."""
    status, headers, body = _get(next_server.port, path)
    assert status == 200
    assert body == _SERVED_FILES["index.html"]
    assert headers["content-type"] == "text/html; charset=utf-8"
    assert headers["cache-control"] == "no-store"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["content-security-policy"] == _EXPECTED_CSP
    assert headers["referrer-policy"] == "no-referrer"


def test_next_without_trailing_slash_redirects(next_server: LocalParserUiServer) -> None:
    """`/next`는 빈 본문의 307로 `/next/`에 보낸다."""
    status, headers, body = _get(next_server.port, "/next")
    assert status == 307
    assert headers["location"] == "/next/"
    assert headers["cache-control"] == "no-store"
    assert body == b""


def test_next_index_references_are_served(next_server: LocalParserUiServer) -> None:
    """index.html의 모든 `src`·`href`가 200, 정확한 MIME 형식, `no-store`로 응답한다."""
    status, _, index_body = _get(next_server.port, "/next/")
    assert status == 200
    refs = _index_refs(index_body.decode("utf-8"))
    assert sorted(refs) == sorted(
        f"/next/{rel_path}"
        for rel_path in ("favicon.svg", "assets/index.css", "assets/index.js", "assets/logo.png")
    )
    for ref in refs:
        status, headers, body = _get(next_server.port, ref)
        assert status == 200, ref
        assert headers["content-type"] == _EXPECTED_MIME[Path(ref).suffix], ref
        assert headers["cache-control"] == "no-store", ref
        assert headers["x-content-type-options"] == "nosniff", ref
        assert body == _SERVED_FILES[ref.removeprefix("/next/")], ref


@pytest.mark.parametrize(
    "path",
    [
        "/next/../server.py",
        "/next/%2e%2e/app.html",
        "/next/..%2fserver.py",
        "/next/assets/..%5c..%5cserver.py",
        "/next/assets\\..\\server.py",
        "/next//x",
        "/next/./index.html",
        "/next/assets/",
        "/next/.hidden",
        "/next/.hidden.js",
        "/next/.cache/hidden.js",
        "/next/THIRD_PARTY_LICENSES.md",
        "/next/x.py",
        "/next/manifest.json",
        "/next/assets/index.js.map",
        "/next/missing.js",
    ],
)
def test_next_rejects_unsafe_or_unknown_paths(next_server: LocalParserUiServer, path: str) -> None:
    """경로 조작·숨김 파일·허용 목록 밖 파일은 기존 JSON 404만 돌려주고 서버는 계속 응답한다."""
    _assert_not_found(*_get(next_server.port, path))
    _assert_still_serving(next_server.port)


@pytest.mark.parametrize(
    "raw_path", [b"/next/\x00", b"/next/index.html\x00", b"/next/assets/\x00/index.js"]
)
def test_next_rejects_nul_byte_in_path(next_server: LocalParserUiServer, raw_path: bytes) -> None:
    """NUL 바이트가 든 경로는 기존 JSON 404만 돌려주고 서버는 계속 응답한다."""
    status, body = _raw_get(next_server.port, raw_path)
    assert status == 404
    assert json.loads(body.decode("utf-8")) == _NOT_FOUND_BODY
    _assert_still_serving(next_server.port)


def test_lookup_static_file_checks_every_segment() -> None:
    """번들에 같은 키가 있어도 경로 조각 하나라도 허용 문자 규칙을 어기면 찾지 않는다."""
    entry = (b"x", "text/javascript; charset=utf-8")
    rejected = [
        "index.html\n",
        "assets/index.js\n",
        "assets//index.js",
        "assets/",
        "./index.html",
        "../index.html",
        "%2e%2e/index.html",
        "assets\\index.js",
        "index.html\x00",
        "c:index.html",
        ".hidden.js",
        "assets/.hidden.js",
        "a b.js",
        "자산/index.js",
    ]
    bundle = {"index.html": entry, "assets/index.js": entry} | dict.fromkeys(rejected, entry)
    assert static_files.lookup_static_file(bundle, "") is entry
    assert static_files.lookup_static_file(bundle, "index.html") is entry
    assert static_files.lookup_static_file(bundle, "assets/index.js") is entry
    for rel_path in rejected:
        assert static_files.lookup_static_file(bundle, rel_path) is None, repr(rel_path)


def test_next_skips_symlinks_that_leave_the_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """정적 폴더 밖을 가리키는 파일 링크와 밖의 폴더는 읽지 않는다.

    밖의 폴더 속 링크가 정적 폴더 안을 되가리켜도 그 폴더에는 들어가지 않고, 정적 폴더를
    되가리키는 폴더 링크는 다시 들르지 않는다.
    """
    root = tmp_path / "static"
    _write_files(root, {"index.html": _SERVED_FILES["index.html"]})
    outside = tmp_path / "outside"
    _write_files(outside, {"secret.js": b"OUTSIDE-SECRET"})
    try:
        (root / "leak.js").symlink_to(outside / "secret.js")
        (root / "linked").symlink_to(outside, target_is_directory=True)
        (outside / "back.html").symlink_to(root / "index.html")
        (root / "loop").symlink_to(root, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable in test environment: {exc}")

    monkeypatch.setattr(static_files, "_static_root", lambda: root)
    assert set(static_files.load_static_bundle()) == {"index.html"}

    with create_ui_server(host="127.0.0.1", port=0) as srv:
        for path in (
            "/next/leak.js",
            "/next/linked/secret.js",
            "/next/linked/back.html",
            "/next/loop/index.html",
        ):
            status, headers, body = _get(srv.port, path)
            _assert_not_found(status, headers, body)
            assert b"OUTSIDE-SECRET" not in body


def test_missing_static_folder_keeps_server_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """정적 폴더가 없으면 빈 번들로 기동하고, `/next/`는 404, `/`는 그대로 응답한다."""
    missing = tmp_path / "no_static"
    monkeypatch.setattr(static_files, "_static_root", lambda: missing)
    assert static_files.load_static_bundle() == {}

    with create_ui_server(host="127.0.0.1", port=0) as srv:
        _assert_not_found(*_get(srv.port, "/next/"))
        status, headers, _ = _get(srv.port, "/")
        assert status == 200
        assert headers["content-type"] == "text/html; charset=utf-8"


def test_load_static_bundle_caps_total_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """합계 크기 상한을 넘게 하는 파일은 읽지 않고 RuntimeWarning으로 알린다."""
    root = tmp_path / "static"
    _write_files(root, {"a.js": b"123456", "b.js": b"123456", "c.css": b"123"})
    monkeypatch.setattr(static_files, "_static_root", lambda: root)
    monkeypatch.setattr(static_files, "_MAX_STATIC_BUNDLE_BYTES", 10)

    with pytest.warns(RuntimeWarning, match=r"파일 1개를 읽지 않았다: b\.js"):
        bundle = static_files.load_static_bundle()
    assert set(bundle) == {"a.js", "c.css"}
    assert sum(len(body) for body, _ in bundle.values()) <= 10


def test_next_rejects_foreign_host(next_server: LocalParserUiServer) -> None:
    """`/next` 경로도 루프백이 아닌 Host 헤더는 라우팅 전에 403으로 거절한다."""
    for path in ("/next/", "/next", "/next/assets/index.js"):
        status, headers, body = _get(next_server.port, path, host="evil.example")
        assert status == 403, path
        assert headers["content-type"] == "application/json; charset=utf-8"
        assert json.loads(body.decode("utf-8")) == {"error": _HOST_ERROR}


def test_post_does_not_route_next(next_server: LocalParserUiServer) -> None:
    """POST는 `/next`를 라우팅하지 않고 기존 API 404를 돌려준다."""
    for path in ("/next", "/next/", "/next/index.html"):
        conn = http.client.HTTPConnection("127.0.0.1", next_server.port, timeout=10)
        try:
            conn.request("POST", path, body=b"{}", headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            assert resp.status == 404, path
            assert json.loads(resp.read().decode("utf-8")) == {"error": "알 수 없는 API 경로다."}
        finally:
            conn.close()


def test_root_still_serves_classic_app_html(next_server: LocalParserUiServer) -> None:
    """`/`는 그대로 `autoetl.ui` 패키지의 app.html 바이트를 보내고 CSP 헤더는 붙이지 않는다."""
    status, headers, body = _get(next_server.port, "/")
    assert status == 200
    assert headers["content-type"] == "text/html; charset=utf-8"
    assert "content-security-policy" not in headers
    app_html = resources.files("autoetl.ui").joinpath("app.html").read_bytes()
    # 서버는 app.html을 텍스트로 읽으므로 CRLF로 체크아웃된 환경에서도 LF로 보낸다.
    assert body == app_html.replace(b"\r\n", b"\n")
    assert "문서 글·표 추출" in body.decode("utf-8")
