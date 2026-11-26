"""로컬 웹 UI 서버의 Host·Origin·Content-Type 요청 헤더 확인 테스트."""

from __future__ import annotations

import http.client
import json
from collections.abc import Iterator

import pytest

from autoetl.parser.ui import create_ui_server
from autoetl.parser.ui.server import LocalParserUiServer, _is_allowed_host, _parse_host_header

_HOST_ERROR = "허용하지 않는 Host 헤더다."
_ORIGIN_ERROR = "다른 출처에서 온 요청은 허용하지 않는다."
_CONTENT_TYPE_ERROR = "요청 본문은 application/json 형식이어야 한다."
_MISSING_INDEX_ERROR = "문서 인덱스를 찾을 수 없다"
_QUERY_BODY = json.dumps({"index_id": "missing", "query": "x"}).encode("utf-8")
_DEFAULT_HOST = object()


def _request(
    port: int,
    method: str,
    path: str,
    *,
    host: object = _DEFAULT_HOST,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
) -> tuple[int, str, bytes]:
    """`http.client`로 헤더를 그대로 지정해 요청하고 (상태, Content-Type, 본문)을 반환한다.

    `host`를 생략하면 `127.0.0.1:<port>`, `None`이면 Host 헤더를 보내지 않는다.
    """
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        if host is _DEFAULT_HOST:
            conn.putheader("Host", f"127.0.0.1:{port}")
        elif isinstance(host, str):
            conn.putheader("Host", host)
        for key, value in (headers or {}).items():
            conn.putheader(key, value)
        if body is not None:
            conn.putheader("Content-Length", str(len(body)))
        conn.endheaders(body)
        resp = conn.getresponse()
        return resp.status, resp.getheader("Content-Type", ""), resp.read()
    finally:
        conn.close()


def _error_of(content_type: str, body: bytes) -> str:
    assert content_type.startswith("application/json")
    payload = json.loads(body.decode("utf-8"))
    assert isinstance(payload, dict)
    error = payload["error"]
    assert isinstance(error, str)
    return error


def _post_json(
    port: int,
    path: str,
    *,
    host: object = _DEFAULT_HOST,
    origin: str | None = None,
    content_type: str | None = "application/json",
    body: bytes = _QUERY_BODY,
) -> tuple[int, str]:
    headers: dict[str, str] = {}
    if content_type is not None:
        headers["Content-Type"] = content_type
    if origin is not None:
        headers["Origin"] = origin
    status, ctype, raw = _request(port, "POST", path, host=host, headers=headers, body=body)
    return status, _error_of(ctype, raw)


@pytest.fixture(scope="module")
def ui_server() -> Iterator[LocalParserUiServer]:
    with create_ui_server(host="127.0.0.1", port=0) as srv:
        yield srv


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("127.0.0.1:8765", "127.0.0.1"),
        ("  LocalHost:8765 ", "localhost"),
        ("localhost", "localhost"),
        ("[::1]:8765", "::1"),
        ("[::1]", "::1"),
        ("", None),
        (None, None),
        ("localhost:", None),
        ("localhost:abc", None),
        ("localhost:\uff18\uff17", None),  # 전각 숫자
        ("[::1", None),
        ("[::1]x", None),
        ("[]:8765", None),
        ("::1", None),
        (":8765", None),
    ],
)
def test_parse_host_header(raw: str | None, expected: str | None) -> None:
    """Host 헤더 값에서 포트를 뗀 소문자 호스트명을 얻고, 형식 오류는 `None`으로 본다."""
    assert _parse_host_header(raw) == expected


def test_is_allowed_host_modes() -> None:
    """기본 모드는 루프백 이름만, 비루프백 허용 모드는 IP 리터럴·바인딩 호스트도 허용한다."""
    for name in ("127.0.0.1", "localhost", "::1"):
        assert _is_allowed_host(name, allow_non_loopback=False, bind_host="127.0.0.1")
    for name in ("192.0.2.10", "evil.example", "127.0.0.1.evil.example", "ui.lan"):
        assert not _is_allowed_host(name, allow_non_loopback=False, bind_host="ui.lan")
    for name in ("192.0.2.10", "2001:db8::1", "ui.lan"):
        assert _is_allowed_host(name, allow_non_loopback=True, bind_host="ui.lan")
    assert not _is_allowed_host("evil.example", allow_non_loopback=True, bind_host="ui.lan")


def test_get_allows_loopback_host_headers(ui_server: LocalParserUiServer) -> None:
    """루프백 Host 헤더(포트 포함)로 온 GET 요청은 그대로 처리한다."""
    port = ui_server.port
    for host in (f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"):
        status, ctype, _ = _request(port, "GET", "/", host=host)
        assert status == 200, host
        assert ctype.startswith("text/html")


@pytest.mark.parametrize("path", ["/", "/api/doctor"])
def test_get_rejects_foreign_host_with_json_error(
    ui_server: LocalParserUiServer, path: str
) -> None:
    """루프백이 아닌 Host 헤더의 GET 요청은 라우팅 전에 403 JSON 오류로 거절한다."""
    port = ui_server.port
    status, ctype, raw = _request(port, "GET", path, host=f"evil.example:{port}")
    assert status == 403
    assert _error_of(ctype, raw) == _HOST_ERROR


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1.evil.example",
        "localhost.evil.example",
        "192.0.2.10:{port}",
        "",
        None,
    ],
)
def test_get_rejects_invalid_host_headers(ui_server: LocalParserUiServer, host: str | None) -> None:
    """루프백 이름을 앞에 붙인 도메인, 기본 모드의 IP, 빈 Host, Host 없음은 모두 403이다."""
    port = ui_server.port
    sent = None if host is None else host.format(port=port)
    status, ctype, raw = _request(port, "GET", "/", host=sent)
    assert status == 403
    assert _error_of(ctype, raw) == _HOST_ERROR


def test_post_origin_check(ui_server: LocalParserUiServer) -> None:
    """Origin이 없거나 `http://<Host>`와 같으면 통과하고, 다른 출처면 403이다."""
    port = ui_server.port
    path = "/api/docindex/query"

    status, error = _post_json(port, path)
    assert status == 404
    assert _MISSING_INDEX_ERROR in error

    status, error = _post_json(port, path, origin=f"http://127.0.0.1:{port}")
    assert status == 404
    assert _MISSING_INDEX_ERROR in error

    status, error = _post_json(port, path, origin=f"HTTP://127.0.0.1:{port}")
    assert status == 404

    for origin in (
        "http://evil.example",
        "null",
        f"http://localhost:{port}",
        f"https://127.0.0.1:{port}",
        f"http://127.0.0.1:{port + 1 if port < 65535 else port - 1}",
    ):
        status, error = _post_json(port, path, origin=origin)
        assert status == 403, origin
        assert error == _ORIGIN_ERROR


def test_post_content_type_check(ui_server: LocalParserUiServer) -> None:
    """JSON 본문을 읽는 엔드포인트는 `application/json` 외의 Content-Type을 415로 거절한다."""
    port = ui_server.port
    path = "/api/docindex/query"

    for ctype in (
        "text/plain",
        "application/x-www-form-urlencoded",
        "multipart/form-data; boundary=x",
        None,
    ):
        status, error = _post_json(port, path, content_type=ctype)
        assert status == 415, ctype
        assert error == _CONTENT_TYPE_ERROR

    for ctype in ("application/json; charset=utf-8", "Application/JSON"):
        status, error = _post_json(port, path, content_type=ctype)
        assert status == 404, ctype
        assert _MISSING_INDEX_ERROR in error


def test_post_rejects_foreign_host(ui_server: LocalParserUiServer) -> None:
    """루프백이 아닌 Host 헤더의 POST 요청은 Content-Type이 맞아도 403이다."""
    port = ui_server.port
    status, error = _post_json(port, "/api/docindex/query", host=f"evil.example:{port}")
    assert status == 403
    assert error == _HOST_ERROR


def test_post_unknown_path_keeps_404(ui_server: LocalParserUiServer) -> None:
    """알 수 없는 POST 경로는 Content-Type과 무관하게 기존 404 응답을 유지한다."""
    port = ui_server.port
    for ctype in ("application/json", "text/plain"):
        status, error = _post_json(port, "/api/unknown", content_type=ctype)
        assert status == 404, ctype
        assert error == "알 수 없는 API 경로다."


def test_non_loopback_mode_allows_ip_literal_hosts() -> None:
    """`allow_non_loopback=True`이면 IP 리터럴 Host는 허용하되 다른 도메인 이름은 거절한다."""
    with create_ui_server(host="127.0.0.1", port=0, allow_non_loopback=True) as srv:
        port = srv.port
        for host in (f"192.0.2.10:{port}", f"[2001:db8::1]:{port}"):
            status, _, _ = _request(port, "GET", "/", host=host)
            assert status == 200, host

        status, ctype, raw = _request(port, "GET", "/", host=f"evil.example:{port}")
        assert status == 403
        assert _error_of(ctype, raw) == _HOST_ERROR
