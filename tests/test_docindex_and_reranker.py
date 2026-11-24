"""Milestones D1~D2: SQLite FTS5 원문 인덱스·표 DuckDB SQL·ONNX 리랭커·도구 인터페이스 테스트."""

from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import autoetl
from autoetl.cli import main
from autoetl.docindex import (
    DocumentIndex,
    DocumentToolkit,
    OnnxCrossEncoderReranker,
    build_document_index,
    create_reranker,
)
from autoetl.parser.adapters import ParserInputError


def _write_rich_korean_docx(path: Path) -> None:
    """제목 계층(Heading1/Heading2) + 본문 + 수치 표가 포함된 테스트 DOCX를 생성한다."""
    doc_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>"
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
        "<w:r><w:t>1. 보안 정책</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>사내 모든 시스템은 외부 인터넷 전송 없이 "
        "로컬에서 운영한다.</w:t></w:r></w:p>"
        '<w:p><w:pPr><w:pStyle w:val="Heading2"/></w:pPr>'
        "<w:r><w:t>1.1 개인정보보호 기준</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>개인정보보호법 제29조에 따라 주민등록번호와 계좌번호는 "
        "암호화하여 저장한다. "
        "접근 로그는 최소 3년간 보관한다.</w:t></w:r></w:p>"
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
        "<w:r><w:t>2. 분기별 매출 실적</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>아래 표는 2026년 1분기 부서별 매출액 집계 현황이다.</w:t></w:r></w:p>"
        "<w:tbl>"
        "<w:tr>"
        "<w:tc><w:p><w:r><w:t>부서</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>구분</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>매출액</w:t></w:r></w:p></w:tc>"
        "</w:tr>"
        "<w:tr>"
        "<w:tc><w:p><w:r><w:t>영업1팀</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>구독</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>1,200,000</w:t></w:r></w:p></w:tc>"
        "</w:tr>"
        "<w:tr>"
        "<w:tc><w:p><w:r><w:t>영업1팀</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>컨설팅</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>800,000</w:t></w:r></w:p></w:tc>"
        "</w:tr>"
        "<w:tr>"
        "<w:tc><w:p><w:r><w:t>영업2팀</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>구독</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>1,500,000</w:t></w:r></w:p></w:tc>"
        "</w:tr>"
        "</w:tbl>"
        "</w:body>"
        "</w:document>"
    )
    styles_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/></w:style>'
        '<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/></w:style>'
        "</w:styles>"
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("word/document.xml", doc_xml)
        zf.writestr("word/styles.xml", styles_xml)


def _write_ops_docx(path: Path) -> None:
    doc_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>"
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
        "<w:r><w:t>장애 대응 매뉴얼</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>장애 코드 ERR-2026-DB-01 발생 시 "
        "백업 스냅샷으로 즉시 복구한다.</w:t></w:r></w:p>"
        "</w:body>"
        "</w:document>"
    )
    styles_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/></w:style>'
        "</w:styles>"
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("word/document.xml", doc_xml)
        zf.writestr("word/styles.xml", styles_xml)


def test_docindex_fts5_korean_2gram_outline_and_duckdb_table_sql(tmp_path: Path) -> None:
    """0MB SQLite FTS5(한글 2-gram) 원문 검색·목차 트리·DuckDB 표 SQL 집계를 검증한다."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    _write_rich_korean_docx(docs_dir / "policy_and_sales.docx")
    _write_ops_docx(docs_dir / "ops_manual.docx")

    db_path = tmp_path / "doc_index.sqlite"
    summary1 = build_document_index(docs_dir, db_path)
    assert summary1.documents_indexed == 2
    assert summary1.documents_skipped == 0
    assert summary1.sections_indexed >= 4
    assert summary1.tables_indexed == 1

    # 동일 파일 재실행 시 SHA-256 중복 스킵(멱등성)
    summary2 = build_document_index(docs_dir, db_path)
    assert summary2.documents_indexed == 0
    assert summary2.documents_skipped == 2

    with DocumentIndex(db_path) as idx:
        docs = idx.list_documents()
        assert len(docs) == 2

        # 1. 목차 계층 트리: 1. 보안 정책 -> 1.1 개인정보보호 기준
        outline = idx.get_outline()
        privacy_node = next(n for n in outline if "개인정보보호" in n.title)
        assert privacy_node.parent_section_id is not None
        assert privacy_node.section_path == ["1. 보안 정책", "1.1 개인정보보호 기준"]

        # 2. 한글 복합명사 부분 질의("개인정보 암호화")로 원문 전체 섹션 적중
        pack = idx.search("개인정보 암호화 보관")
        assert pack.mode == "fts5_bm25"
        assert pack.reranker_model is None
        assert pack.hits
        top_hit = pack.hits[0]
        assert "개인정보보호법 제29조" in top_hit.full_text
        assert "최소 3년간 보관" in top_hit.full_text

        # 3. 코드 식별자 질의("ERR-2026-DB-01") 적중
        code_pack = idx.search("ERR-2026-DB-01")
        assert code_pack.hits and "백업 스냅샷" in code_pack.hits[0].full_text

        # 4. 원문 섹션 전체 읽기
        sec_content = idx.read_section(top_hit.section_id)
        assert sec_content.full_text == top_hit.full_text

        # 5. 추출 표 목록 및 격리 DuckDB SQL 집계(천단위 쉼표 자동 정수 변환!)
        tables = idx.list_tables()
        assert len(tables) == 1
        t_uid = tables[0].table_uid
        qres = idx.query_table(
            t_uid,
            "SELECT 부서, SUM(매출액) AS 합계 FROM tbl GROUP BY 부서 ORDER BY 합계 DESC",
        )
        assert qres.columns == ["부서", "합계"]
        assert qres.rows == [["영업1팀", "2000000"], ["영업2팀", "1500000"]]

        # 6. 쓰기/외부접근 SQL 차단 보안 가드
        with pytest.raises(ParserInputError):
            idx.query_table(t_uid, "DROP TABLE tbl")
        with pytest.raises(ParserInputError):
            idx.query_table(t_uid, "SELECT * FROM read_csv('/etc/passwd')")
        with pytest.raises(ParserInputError):
            idx.query_table(t_uid, "SELECT 1; SELECT 2")


@dataclass
class _MockInputMeta:
    name: str


@dataclass
class _MockEncoding:
    ids: list[int]
    attention_mask: list[int]
    type_ids: list[int]


class _MockTokenizer:
    def encode_batch(self, pairs: list[tuple[str, str]]) -> list[_MockEncoding]:
        out: list[_MockEncoding] = []
        for q, p in pairs:
            # 테스트용: 특정 키워드("백업 스냅샷")가 있으면 높은 id 토큰 표시
            score_flag = 1 if ("복구" in q and "백업 스냅샷" in p) else 0
            out.append(
                _MockEncoding(
                    ids=[101, score_flag, 102],
                    attention_mask=[1, 1, 1],
                    type_ids=[0, 1, 1],
                )
            )
        return out


class _MockOnnxSession:
    def get_inputs(self) -> list[_MockInputMeta]:
        return [
            _MockInputMeta("input_ids"),
            _MockInputMeta("attention_mask"),
            _MockInputMeta("token_type_ids"),
        ]

    def run(self, _outputs: Any, feed: dict[str, Any]) -> list[list[list[float]]]:
        ids_batch = feed["input_ids"]
        logits: list[list[float]] = []
        for row in ids_batch:
            flag = int(row[1])
            logits.append([4.5] if flag == 1 else [-2.0])
        return [logits]


def test_onnx_reranker_toolkit_cli_and_sdk(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """선택 CPU ONNX Cross-Encoder 리랭커·에이전트 도구 5종·CLI(`index`/`query`)·SDK를 검증한다."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    _write_rich_korean_docx(docs_dir / "policy_and_sales.docx")
    _write_ops_docx(docs_dir / "ops_manual.docx")
    db_path = tmp_path / "idx.sqlite"

    # 1. CLI `autoetl index` + `autoetl query`
    assert main(["index", str(docs_dir), "--out", str(db_path)]) == 0
    idx_out = capsys.readouterr().out
    assert "신규 2건" in idx_out

    assert main(["query", str(db_path), "매출액 부서", "--format", "json"]) == 0
    q_json = json.loads(capsys.readouterr().out)
    assert q_json["mode"] == "fts5_bm25"
    assert len(q_json["hits"]) >= 1
    assert len(q_json["table_hits"]) == 1

    assert main(["query", str(db_path), "개인정보보호", "--format", "markdown"]) == 0
    q_md = capsys.readouterr().out
    assert "개인정보보호법 제29조" in q_md

    # 2. D2 OnnxCrossEncoderReranker 주입 테스트 (장문 슬라이딩 창 포함)
    reranker = OnnxCrossEncoderReranker(
        tmp_path / "bge-reranker-v2-m3-int8.onnx",
        session=_MockOnnxSession(),
        tokenizer=_MockTokenizer(),
    )
    assert reranker.available is True
    assert reranker.name == "onnx:bge-reranker-v2-m3-int8"

    long_tail = ("무관한 문장입니다. " * 250) + "백업 스냅샷 복구 절차"
    scores = reranker.score_pairs("장애 복구", ["일반 안내 문장", long_tail])
    assert scores[1] > 0.95 and scores[0] < 0.2

    # 3. DocumentToolkit + ONNX 리랭커 + JSON Schema 5종 + 단일 디스패처 + 최상위 SDK
    with autoetl.DocumentToolkit(db_path, reranker=reranker) as kit:
        schemas = DocumentToolkit.tool_schemas()
        assert {s["name"] for s in schemas} == {
            "search_documents",
            "get_outline",
            "read_section",
            "list_tables",
            "query_table",
        }

        res = kit.dispatch("search_documents", {"query": "장애 복구 로컬", "top_k": 3})
        assert res["mode"] == "fts5+onnx_rerank"
        assert res["reranker_model"] == "onnx:bge-reranker-v2-m3-int8"
        assert "백업 스냅샷" in res["hits"][0]["full_text"]
        assert res["hits"][0]["rerank_score"] > 0.95

        outline_res = kit.dispatch("get_outline")
        assert len(outline_res["documents"]) == 2
        first_sid = outline_res["sections"][0]["section_id"]
        sec_res = kit.dispatch("read_section", {"section_id": first_sid})
        assert sec_res["full_text"]

        tbls_res = kit.dispatch("list_tables")
        t_uid = tbls_res["tables"][0]["table_uid"]
        sql_res = kit.dispatch(
            "query_table", {"table_uid": t_uid, "sql": "SELECT COUNT(*) FROM tbl"}
        )
        assert sql_res["rows"] == [["3"]]

    # create_reranker 가드 검증
    assert create_reranker("none") is None
    with pytest.raises(ParserInputError):
        create_reranker("onnx")


class _MockBinaryLogitSession:
    def get_inputs(self) -> list[_MockInputMeta]:
        return [_MockInputMeta("input_ids"), _MockInputMeta("attention_mask")]

    def run(self, _outputs: Any, feed: dict[str, Any]) -> list[list[list[float]]]:
        return [[[-1.5, 3.0] if int(row[1]) == 1 else [2.5, -1.0] for row in feed["input_ids"]]]


def test_index_from_parsed_json_bundle_and_binary_logit_reranker(tmp_path: Path) -> None:
    """`parse_and_export`로 생성된 `parsed.json` 번들 인덱싱과 2-클래스 로짓 리랭커를 검증한다."""
    src_docx = tmp_path / "ops.docx"
    _write_ops_docx(src_docx)
    parsed_dir = tmp_path / "parsed_bundle"
    doc = autoetl.parse(src_docx)
    autoetl.export_bundle(doc, parsed_dir)
    assert (parsed_dir / "parsed.json").is_file()

    db_path = tmp_path / "from_bundle.sqlite"
    summary = autoetl.build_document_index(parsed_dir, db_path)
    assert summary.documents_indexed == 1
    assert summary.sections_indexed >= 1

    reranker = OnnxCrossEncoderReranker(
        tmp_path / "bge-reranker-base-int8.onnx",
        session=_MockBinaryLogitSession(),
        tokenizer=_MockTokenizer(),
    )
    scores = reranker.score_pairs("복구 절차", ["일반 안내", "백업 스냅샷 복구"])
    assert scores[1] > 0.95 and scores[0] < 0.05


def test_currency_prefixed_table_auto_casts_for_duckdb_sql(tmp_path: Path) -> None:
    """달러($)·원화(₩) 통화 기호가 붙은 추출 표도 수동 CAST 없이 DuckDB SUM 집계가 동작한다."""
    doc_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>"
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
        "<w:r><w:t>Invoice Summary</w:t></w:r></w:p>"
        "<w:tbl>"
        "<w:tr>"
        "<w:tc><w:p><w:r><w:t>Category</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>Total Value</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>KRW Budget</w:t></w:r></w:p></w:tc>"
        "</w:tr>"
        "<w:tr>"
        "<w:tc><w:p><w:r><w:t>Hardware</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>$25,285.00</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>₩1,200,000</w:t></w:r></w:p></w:tc>"
        "</w:tr>"
        "<w:tr>"
        "<w:tc><w:p><w:r><w:t>Hardware</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>$10,000.50</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>₩800,000</w:t></w:r></w:p></w:tc>"
        "</w:tr>"
        "<w:tr>"
        "<w:tc><w:p><w:r><w:t>Software</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>$60,720.00</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>₩500,000</w:t></w:r></w:p></w:tc>"
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
    src = tmp_path / "invoice.docx"
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr("word/document.xml", doc_xml)
        zf.writestr("word/styles.xml", styles_xml)

    db_path = tmp_path / "inv.sqlite"
    build_document_index(src, db_path)
    with DocumentIndex(db_path) as idx:
        tbls = idx.list_tables()
        assert len(tbls) == 1
        res = idx.query_table(
            tbls[0].table_uid,
            'SELECT "Category", ROUND(SUM("Total Value"), 2) AS usd_sum, '
            'SUM("KRW Budget") AS krw_sum FROM tbl GROUP BY "Category" ORDER BY usd_sum DESC',
        )
        assert res.rows == [
            ["Software", "60720.0", "500000"],
            ["Hardware", "35285.5", "2000000"],
        ]
