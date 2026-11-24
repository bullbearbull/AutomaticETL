"""무의존성 로컬 웹 UI(`autoetl ui`) 및 환경 진단(`autoetl doctor`) E2E 테스트."""

from __future__ import annotations

import base64
import io
import json
import urllib.error
import urllib.request
import zipfile

import pytest

from autoetl.cli import main
from autoetl.doctor import collect_doctor_report, render_doctor_markdown
from autoetl.parser import ParserInputError
from autoetl.parser.ui import create_ui_server


def _make_mini_docx_bytes(text: str = "웹 UI 파싱 테스트 문장입니다.") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "word/document.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body>"
            "</w:document>",
        )
    return buf.getvalue()


def test_doctor_report_and_cli(capsys: pytest.CaptureFixture[str]) -> None:
    """`collect_doctor_report`와 `autoetl doctor [--json]` CLI 출력을 검증한다."""
    report = collect_doctor_report()
    assert len(report.supported_extensions) >= 8
    assert ".xlsx" in report.supported_extensions
    assert ".hwp" in report.supported_extensions
    md = render_doctor_markdown(report)
    assert "Auto ETL / Auto Parser" in md

    assert main(["doctor"]) == 0
    out_md = capsys.readouterr().out
    assert "OOXML" in out_md

    assert main(["doctor", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert ".pdf" in data["supported_extensions"]
    assert len(data["checks"]) >= 5


def test_ui_server_loopback_guard_and_safe_close() -> None:
    """로컬 웹 UI는 비루프백 바인딩을 차단하고, 미기동 서버 close()도 안전하게 종료한다."""
    with pytest.raises(ParserInputError, match="루프백"):
        create_ui_server(host="0.0.0.0", port=0)

    # CLI도 비루프백 요청 시 종료 코드 1 반환
    assert main(["ui", "--host", "0.0.0.0", "--no-browser"]) == 1

    # serve_forever() 미호출 상태에서 close() 호출 시 교착 없이 종료되어야 한다
    idle_srv = create_ui_server(host="127.0.0.1", port=0)
    idle_srv.close()


def test_ui_server_end_to_end_parse_and_zip_download() -> None:
    """실제 로컬 HTTP 서버에서 HTML·`/api/doctor`·`/api/parse`·`/api/download` 왕복을 검증한다."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    with create_ui_server(host="127.0.0.1", port=0) as srv:
        # 1. GET /
        with opener.open(f"{srv.url}/") as resp:
            assert resp.status == 200
            html = resp.read().decode("utf-8")
            assert "Auto Parser" in html

        # 2. GET /api/doctor
        with opener.open(f"{srv.url}/api/doctor") as resp:
            assert resp.status == 200
            doc_json = json.loads(resp.read().decode("utf-8"))
            assert ".xlsx" in doc_json["supported_extensions"]

        # 3. POST /api/parse (chunk_overlap=0 포함)
        docx_bytes = _make_mini_docx_bytes("로컬 웹 UI E2E 검증 본문")
        req_payload = json.dumps(
            {
                "filename": "demo.docx",
                "content_base64": base64.b64encode(docx_bytes).decode("ascii"),
                "options": {
                    "include_comments": True,
                    "include_chunks": True,
                    "chunk_max_chars": 800,
                    "chunk_overlap": 0,
                },
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{srv.url}/api/parse",
            data=req_payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with opener.open(req) as resp:
            assert resp.status == 200
            parsed = json.loads(resp.read().decode("utf-8"))
            assert parsed["document"]["format"] == "docx"
            assert "로컬 웹 UI E2E 검증 본문" in parsed["markdown"]
            assert len(parsed["chunks"]) >= 1
            bundle_id = parsed["bundle_id"]
            assert bundle_id

        # 4. GET /api/download/<bundle_id>
        with opener.open(f"{srv.url}/api/download/{bundle_id}") as resp:
            assert resp.status == 200
            zbytes = resp.read()
        with zipfile.ZipFile(io.BytesIO(zbytes), "r") as zf:
            names = set(zf.namelist())
            assert {"parsed.json", "document.md", "chunks.jsonl", "routing_report.md"} <= names

        # 5. 미지원 확장자 400 가드
        bad_req = urllib.request.Request(
            f"{srv.url}/api/parse",
            data=json.dumps({"filename": "evil.exe", "content_base64": "AAAA"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            opener.open(bad_req)
        assert exc_info.value.code == 400
