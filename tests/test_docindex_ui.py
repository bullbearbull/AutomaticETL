"""로컬 웹 UI 3번째 탭(원문 문서 인덱스 · 검색 · 목차 뷰어 · DuckDB 표 SQL) E2E 테스트."""

from __future__ import annotations

import base64
import io
import json
import urllib.error
import urllib.request
import zipfile

import pytest

from autoetl.parser.ui import create_ui_server


def _make_korean_sales_docx_bytes() -> bytes:
    doc_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>"
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
        "<w:r><w:t>1. 보안 운영 원칙</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>모든 문서는 외부 VectorDB 없이 로컬 SQLite FTS5에서 "
        "원문 그대로 검색한다.</w:t></w:r></w:p>"
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
        "<w:r><w:t>2. 부서별 매출 현황</w:t></w:r></w:p>"
        "<w:tbl>"
        "<w:tr>"
        "<w:tc><w:p><w:r><w:t>Category</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>Amount</w:t></w:r></w:p></w:tc>"
        "</w:tr>"
        "<w:tr>"
        "<w:tc><w:p><w:r><w:t>Hardware</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>$12,500.50</w:t></w:r></w:p></w:tc>"
        "</w:tr>"
        "<w:tr>"
        "<w:tc><w:p><w:r><w:t>Hardware</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>$7,500.00</w:t></w:r></w:p></w:tc>"
        "</w:tr>"
        "<w:tr>"
        "<w:tc><w:p><w:r><w:t>Software</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>$30,000.00</w:t></w:r></w:p></w:tc>"
        "</w:tr>"
        "</w:tbl>"
        "</w:body>"
        "</w:document>"
    )
    styles_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/></w:style>'
        "</w:styles>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", doc_xml)
        zf.writestr("word/styles.xml", styles_xml)
    return buf.getvalue()


def _make_ops_docx_bytes() -> bytes:
    doc_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>"
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
        "<w:r><w:t>장애 복구 절차</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>장애 코드 ERR-2026 발생 시 "
        "백업 스냅샷으로 5분 내 복구한다.</w:t></w:r></w:p>"
        "</w:body>"
        "</w:document>"
    )
    styles_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/></w:style>'
        "</w:styles>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", doc_xml)
        zf.writestr("word/styles.xml", styles_xml)
    return buf.getvalue()


def _post_json(
    opener: urllib.request.OpenerDirector, url: str, payload: dict[str, object]
) -> dict[str, object]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with opener.open(req, timeout=30) as resp:
        assert resp.status == 200
        data: dict[str, object] = json.loads(resp.read().decode("utf-8"))
        return data


def test_ui_server_docindex_e2e_and_security_guards() -> None:
    """로컬 웹 UI 문서 인덱스 구축·FTS5 검색·목차 조회·DuckDB 표 SQL·보안 가드를 검증한다."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    with create_ui_server(host="127.0.0.1", port=0) as srv:
        # 1. GET / 에 3번째 탭 존재 확인
        with opener.open(f"{srv.url}/") as resp:
            assert resp.status == 200
            html = resp.read().decode("utf-8")
            assert "문서 내용 검색" in html
            assert "docIndexModePanel" in html

        # 2. POST /api/docindex/build
        built = _post_json(
            opener,
            f"{srv.url}/api/docindex/build",
            {
                "files": [
                    {
                        "filename": "policy_sales.docx",
                        "content_base64": base64.b64encode(_make_korean_sales_docx_bytes()).decode(
                            "ascii"
                        ),
                    },
                    {
                        "filename": "ops_manual.docx",
                        "content_base64": base64.b64encode(_make_ops_docx_bytes()).decode("ascii"),
                    },
                ]
            },
        )
        index_id = str(built["index_id"])
        dl_token = str(built["download_token"])
        assert index_id
        assert len(built["documents"]) == 2  # type: ignore[arg-type]
        assert len(built["sections"]) >= 3  # type: ignore[arg-type]
        assert len(built["tables"]) == 1  # type: ignore[arg-type]

        # 3. GET /api/download/<download_token> -> 실제 SQLite 파일 확인
        with opener.open(f"{srv.url}/api/download/{dl_token}") as resp:
            assert resp.status == 200
            sqlite_bytes = resp.read()
            assert sqlite_bytes.startswith(b"SQLite format 3\x00")

        # 4. POST /api/docindex/query (0MB SQLite FTS5 한글 2-gram 검색 + 동의어 확장)
        q_res = _post_json(
            opener,
            f"{srv.url}/api/docindex/query",
            {"index_id": index_id, "query": "백업 스냅샷 복구", "top_k": 3},
        )
        assert q_res["mode"] == "fts5_bm25"
        hits = q_res["hits"]
        assert isinstance(hits, list) and len(hits) >= 1
        assert "ERR-2026" in hits[0]["full_text"]

        q_syn = _post_json(
            opener,
            f"{srv.url}/api/docindex/query",
            {
                "index_id": index_id,
                "query": "사고 조치",
                "top_k": 3,
                "synonyms": "사고=장애,조치=복구",
            },
        )
        syn_hits = q_syn["hits"]
        assert isinstance(syn_hits, list) and len(syn_hits) >= 1
        assert "ERR-2026" in syn_hits[0]["full_text"]

        # 5. POST /api/docindex/section (청크 절단 없는 원문 섹션 조회)
        sec_id = str(hits[0]["section_id"])
        sec_res = _post_json(
            opener,
            f"{srv.url}/api/docindex/section",
            {"index_id": index_id, "section_id": sec_id},
        )
        assert "5분 내 복구" in str(sec_res["full_text"])

        # 6. POST /api/docindex/table-sql (통화 기호 표 DuckDB SUM 집계)
        t_uid = str(built["tables"][0]["table_uid"])  # type: ignore[index]
        sql_res = _post_json(
            opener,
            f"{srv.url}/api/docindex/table-sql",
            {
                "index_id": index_id,
                "table_uid": t_uid,
                "sql": (
                    'SELECT "Category", ROUND(SUM("Amount"), 2) AS total '
                    'FROM tbl GROUP BY "Category" ORDER BY total DESC'
                ),
            },
        )
        assert sql_res["rows"] == [["Software", "30000.0"], ["Hardware", "20000.5"]]

        # 7. 보안 가드: 쓰기 SQL(400), 경로 탈출 index_id(404), 비-ONNX 모델(422) 차단
        for url, payload, expected_code in [
            (
                f"{srv.url}/api/docindex/table-sql",
                {"index_id": index_id, "table_uid": t_uid, "sql": "DROP TABLE tbl"},
                400,
            ),
            (
                f"{srv.url}/api/docindex/query",
                {"index_id": "../../etc/passwd", "query": "test"},
                404,
            ),
            (
                f"{srv.url}/api/docindex/query",
                {
                    "index_id": index_id,
                    "query": "복구",
                    "reranker": "onnx",
                    "reranker_model": "/etc/passwd",
                },
                422,
            ),
            (
                f"{srv.url}/api/docindex/build",
                {"files": [{"filename": "bad.exe", "content_base64": "AAAA"}]},
                400,
            ),
        ]:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                opener.open(req)
            assert exc_info.value.code == expected_code
