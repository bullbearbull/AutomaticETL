"""Milestones D1~D2: SQLite FTS5 원문 인덱스·표 DuckDB SQL·ONNX 리랭커·도구 인터페이스 테스트."""

from __future__ import annotations

import json
import sqlite3
import sys
import zipfile
from collections.abc import Sequence
from contextlib import closing
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
    render_evidence_pack_markdown,
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


def test_reranker_parent_tokenizer_resolution_and_query_guided_windows(tmp_path: Path) -> None:
    """HF `onnx/model_int8.onnx` 상위 폴더 토크나이저 탐색과 장문 질의 창 선별을 검증한다."""
    from autoetl.docindex.reranker import _resolve_tokenizer_path, _slice_windows

    repo_dir = tmp_path / "jina_v2_int8"
    onnx_dir = repo_dir / "onnx"
    onnx_dir.mkdir(parents=True)
    model_file = onnx_dir / "model_int8.onnx"
    model_file.write_bytes(b"stub")
    parent_tok = repo_dir / "tokenizer.json"
    parent_tok.write_text("{}", encoding="utf-8")

    assert _resolve_tokenizer_path(model_file, None) == parent_tok

    # 25KB 장문 섹션의 맨 끝(24KB 지점)에 핵심 키워드가 있어도 max_windows=3 안에 포착되어야 한다
    deep_section = (
        "겨울철 일반 안내 문장입니다. " * 1400
    ) + " 핵심 근거: 중국어선 불법조업 단속 및 해경활동 해양사고 대응 매뉴얼."
    windows = _slice_windows(
        deep_section,
        query="중국어선 단속 해양사고",
        max_windows=3,
    )
    assert len(windows) == 3
    assert any("중국어선 불법조업" in w for w in windows)


def _write_custom_docx(path: Path, heading: str, body: str) -> None:
    doc_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>"
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
        f"<w:r><w:t>{heading}</w:t></w:r></w:p>"
        f"<w:p><w:r><w:t>{body}</w:t></w:r></w:p>"
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


def _index_test_table(
    idx: DocumentIndex, tmp_path: Path, headers: list[str], rows: list[list[str]]
) -> str:
    src = tmp_path / "table.docx"
    _write_rich_korean_docx(src)
    doc = autoetl.parse(src)
    table = doc.tables[0].model_copy(
        update={"headers": headers, "rows": rows, "row_count": len(rows), "col_count": len(headers)}
    )
    doc.tables = [table]
    doc.pages[table.page - 1].tables = [table]
    idx.index_parsed_document(doc)
    return idx.list_tables()[0].table_uid


@pytest.mark.parametrize(
    ("values", "expected_type"),
    [
        (["00123", "04567", ""], "VARCHAR"),
        (["00123", "4.5"], "VARCHAR"),
        (["05.30", "06.15"], "VARCHAR"),
        (["007.5", "4.5"], "VARCHAR"),
        (["9223372036854775808", "42"], "VARCHAR"),
        (["-9223372036854775809", "42"], "VARCHAR"),
        (["9" * 4301, "42"], "VARCHAR"),
        (["9223372036854775808", "4.5"], "VARCHAR"),
        (["-9223372036854775808", "9223372036854775807", "0"], "BIGINT"),
    ],
)
def test_docindex_sql_preserves_codes_and_out_of_range_integers(
    tmp_path: Path, values: list[str], expected_type: str
) -> None:
    with DocumentIndex(":memory:") as idx:
        table_uid = _index_test_table(idx, tmp_path, ["value"], [[v] for v in values])

        result = idx.query_table(table_uid, "SELECT value, typeof(value) FROM tbl")

    assert result.rows == [[v, expected_type] for v in values]


@pytest.mark.parametrize(
    ("values", "expected_sum"),
    [
        (["$001", "$2.75"], "3.75"),
        (["001,002", "2"], "1004"),
        (["$05.30", "$0.20"], "5.5"),
        (["0.5", "-0.25"], "0.25"),
    ],
)
def test_docindex_sql_aggregates_formatted_numbers_with_leading_zeros(
    tmp_path: Path, values: list[str], expected_sum: str
) -> None:
    with DocumentIndex(":memory:") as idx:
        table_uid = _index_test_table(idx, tmp_path, ["value"], [[v] for v in values])

        result = idx.query_table(table_uid, "SELECT SUM(value) FROM tbl")

    assert result.rows == [[expected_sum]]


def test_docindex_sql_header_deduplication_uses_duckdb_identifier_rules(tmp_path: Path) -> None:
    headers = ["a", "a", "a_2", "Name", "name", "", "COL_6", "É", "é"]
    values = [str(i) for i in range(len(headers))]
    with DocumentIndex(":memory:") as idx:
        table_uid = _index_test_table(idx, tmp_path, headers, [values])

        result = idx.read_table(table_uid)

    assert result.columns == ["a", "a_2", "a_2_2", "Name", "name_2", "col_6", "COL_6_2", "É", "é"]
    assert result.rows == [values]


@pytest.mark.parametrize(
    ("set_operator", "expected_rows"), [("EXCEPT", [["1"]]), ("INTERSECT", [["2"], ["3"]])]
)
def test_docindex_sql_allows_readonly_set_operations(
    tmp_path: Path, set_operator: str, expected_rows: list[list[str]]
) -> None:
    with DocumentIndex(":memory:") as idx:
        table_uid = _index_test_table(idx, tmp_path, ["value"], [["1"], ["2"], ["3"]])
        result = idx.query_table(
            table_uid,
            f"WITH filtered AS (SELECT value FROM tbl WHERE value > 1) "
            f"SELECT value FROM tbl {set_operator} SELECT value FROM filtered ORDER BY value",
        )

        with pytest.raises(ParserInputError, match="허용되지 않는 SQL 함수"):
            idx.query_table(
                table_uid,
                f"SELECT value FROM tbl {set_operator} SELECT * FROM read_csv('unused.csv')",
            )

    assert result.rows == expected_rows


def test_docindex_sql_allows_parenthesized_readonly_subquery(tmp_path: Path) -> None:
    with DocumentIndex(":memory:") as idx:
        table_uid = _index_test_table(idx, tmp_path, ["value"], [["1"], ["2"], ["3"]])
        result = idx.query_table(table_uid, "(SELECT value FROM tbl ORDER BY value)")
        with pytest.raises(ParserInputError, match="허용되지 않는 SQL 함수"):
            idx.query_table(table_uid, "(SELECT * FROM read_csv('unused.csv'))")

    assert result.rows == [["1"], ["2"], ["3"]]


def _write_index_bundle(source: Path, bundle: Path) -> None:
    doc = autoetl.parse(source)
    doc.document.source_path = str(source)
    bundle.write_text(doc.model_dump_json(), encoding="utf-8")


def test_docindex_replace_matches_stored_relative_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    db_path = tmp_path / "index.sqlite"
    relative_src = Path("docs/record.docx")
    relative_src.parent.mkdir()
    _write_custom_docx(relative_src, "old", "obsolete version")
    parsed_json = tmp_path / "parsed.json"
    _write_index_bundle(relative_src, parsed_json)
    assert build_document_index(parsed_json, db_path).documents_indexed == 1
    with DocumentIndex(db_path) as idx:
        assert idx.list_documents()[0].source_path == str(relative_src)

    absolute_src = relative_src.resolve()
    monkeypatch.chdir(absolute_src.parent)
    _write_custom_docx(absolute_src, "new", "current version")
    _write_index_bundle(absolute_src, parsed_json)
    assert build_document_index(parsed_json, db_path, replace=True).documents_indexed == 1

    with DocumentIndex(db_path) as idx:
        assert [doc.source_path for doc in idx.list_documents()] == [str(absolute_src)]
        assert not idx.search("obsolete").hits
        assert len(idx.search("current").hits) == 1


@pytest.mark.parametrize(
    ("legacy", "incoming_relative", "keep_old"),
    [(False, False, True), (False, True, True), (True, False, True), (True, True, False)],
)
def test_docindex_replace_uses_index_time_path_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    legacy: bool,
    incoming_relative: bool,
    keep_old: bool,
) -> None:
    relative_src = Path("docs/record.docx")
    first = tmp_path / "first"
    second = tmp_path / "second"
    for directory in (first, second):
        (directory / relative_src).parent.mkdir(parents=True)
    db_path = tmp_path / "index.sqlite"
    parsed_json = tmp_path / "parsed.json"

    monkeypatch.chdir(first)
    _write_custom_docx(relative_src, "old record", "archived report")
    _write_index_bundle(relative_src, parsed_json)
    assert build_document_index(parsed_json, db_path).documents_indexed == 1

    if legacy:
        # Reproduce an index from before canonical source paths were recorded.
        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute("DROP TABLE IF EXISTS document_source_paths")

    monkeypatch.chdir(second)
    source = relative_src if incoming_relative else relative_src.resolve()
    _write_custom_docx(source, "new record", "current report")
    _write_index_bundle(source, parsed_json)
    assert build_document_index(parsed_json, db_path, replace=True).documents_indexed == 1

    with DocumentIndex(db_path) as idx:
        # An exact legacy key remains usable when the index lacks the original directory.
        assert bool(idx.search("archived").hits) == keep_old
        assert len(idx.list_documents()) == (2 if keep_old else 1)
        assert [hit.source_path for hit in idx.search("current").hits] == [str(source)]


def test_docindex_replace_does_not_guess_directory_for_stored_basename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    db_path = tmp_path / "index.sqlite"
    older_src = tmp_path / "other" / "record.docx"
    older_src.parent.mkdir()
    _write_custom_docx(older_src, "other record", "previous document from another folder")
    assert build_document_index(older_src, db_path).documents_indexed == 1

    incoming_src = tmp_path.resolve() / "record.docx"
    _write_custom_docx(incoming_src, "new record", "new document from this folder")
    parsed_json = tmp_path / "parsed.json"
    _write_index_bundle(incoming_src, parsed_json)
    assert build_document_index(parsed_json, db_path, replace=True).documents_indexed == 1

    with DocumentIndex(db_path) as idx:
        assert {doc.source_path for doc in idx.list_documents()} == {
            older_src.name,
            str(incoming_src),
        }


def test_docindex_replace_preserves_repointed_symlink_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "docs").mkdir()
    v1 = tmp_path / "v1.docx"
    v2 = tmp_path / "v2.docx"
    _write_custom_docx(v1, "entry", "obsolete symlink target")
    _write_custom_docx(v2, "entry", "updated symlink target")

    link = Path("docs/current.docx")
    try:
        link.symlink_to(v1)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable in test environment: {exc}")

    db_path = tmp_path / "index.sqlite"
    parsed_json = tmp_path / "parsed.json"
    _write_index_bundle(link, parsed_json)
    assert build_document_index(parsed_json, db_path).documents_indexed == 1

    link.unlink()
    link.symlink_to(v2)
    _write_index_bundle(link, parsed_json)
    assert build_document_index(parsed_json, db_path, replace=True).documents_indexed == 1

    with DocumentIndex(db_path) as idx:
        assert [doc.source_path for doc in idx.list_documents()] == [str(link)]
        assert not idx.search("obsolete").hits
        assert len(idx.search("updated").hits) == 1


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Win32 collapses '..' lexically before evaluating symlinks",
)
def test_docindex_replace_symlink_parent_traversal_does_not_collide_lexically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "docs").mkdir()
    (tmp_path / "archive" / "sub").mkdir(parents=True)
    lexical_neighbor = tmp_path / "docs" / "report.docx"
    archived_target = tmp_path / "archive" / "report.docx"
    _write_custom_docx(lexical_neighbor, "neighbor", "unrelated lexical neighbor")
    _write_custom_docx(archived_target, "target", "archived symlink target")

    link = Path("docs/link")
    try:
        link.symlink_to(tmp_path / "archive" / "sub", target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable in test environment: {exc}")

    db_path = tmp_path / "index.sqlite"
    assert build_document_index(lexical_neighbor, db_path).documents_indexed == 1

    traversal_rel = Path("docs/link/../report.docx")
    parsed_json = tmp_path / "traversal.json"
    _write_index_bundle(traversal_rel, parsed_json)
    assert build_document_index(parsed_json, db_path, replace=True).documents_indexed == 1

    with DocumentIndex(db_path) as idx:
        assert len(idx.list_documents()) == 2
        assert len(idx.search("unrelated").hits) == 1
        assert len(idx.search("archived").hits) == 1

    _write_custom_docx(archived_target, "target", "updated archived target")
    _write_index_bundle(archived_target, parsed_json)
    assert build_document_index(parsed_json, db_path, replace=True).documents_indexed == 1

    with DocumentIndex(db_path) as idx:
        assert len(idx.list_documents()) == 2
        assert len(idx.search("unrelated").hits) == 1
        assert not idx.search("symlink").hits
        assert len(idx.search("updated").hits) == 1


def test_docindex_replace_does_not_bind_missing_relative_bundle_path_to_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dir_a = tmp_path / "a"
    dir_a.mkdir()
    src_file = tmp_path / "sample.docx"
    _write_custom_docx(src_file, "entry", "external bundle content")
    doc_a = autoetl.parse(src_file)
    doc_a.document.source_path = "docs/external.docx"
    bundle_a = tmp_path / "a.json"
    bundle_a.write_text(doc_a.model_dump_json(), encoding="utf-8")

    db_path = tmp_path / "index.sqlite"
    monkeypatch.chdir(dir_a)
    assert build_document_index(bundle_a, db_path).documents_indexed == 1

    (dir_a / "docs").mkdir()
    local_doc = (dir_a / "docs" / "external.docx").resolve()
    _write_custom_docx(local_doc, "local", "different file at dir a")
    bundle_local = tmp_path / "local.json"
    _write_index_bundle(local_doc, bundle_local)
    assert build_document_index(bundle_local, db_path, replace=True).documents_indexed == 1

    with DocumentIndex(db_path) as idx:
        assert len(idx.list_documents()) == 2
        assert len(idx.search("external").hits) == 1


def test_docindex_mixed_bm25_corpus_does_not_outrank_with_floor_calibration(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "mixed.sqlite"
    paths: list[Path] = []
    for i in range(3):
        p = tmp_path / f"rare_{i}.docx"
        _write_custom_docx(p, "entry", f"common rare x{i}")
        paths.append(p)
    for i in range(4):
        p = tmp_path / f"common_{i}.docx"
        _write_custom_docx(p, "entry", f"common pass y{i}")
        paths.append(p)

    assert build_document_index(paths, db_path).documents_indexed == 7

    with DocumentIndex(db_path) as idx:
        pack = idx.search("common rare", top_k=3)

    assert len(pack.hits) == 3
    assert all("rare" in hit.full_text for hit in pack.hits)
    assert "소규모 코퍼스 어휘 보정" not in render_evidence_pack_markdown(pack)


def test_docindex_mixed_corpus_reranked_floored_hit_does_not_claim_floor_calibration(
    tmp_path: Path,
) -> None:
    class _StubReranker:
        @property
        def name(self) -> str:
            return "stub-reranker"

        @property
        def available(self) -> bool:
            return True

        def score_pairs(self, query: str, passages: Sequence[str]) -> list[float]:
            del query
            return [0.95 if "common pass" in passage else 0.10 for passage in passages]

    db_path = tmp_path / "mixed_rerank.sqlite"
    paths: list[Path] = []
    for i in range(3):
        p = tmp_path / f"rare_{i}.docx"
        _write_custom_docx(p, "entry", f"common rare x{i}")
        paths.append(p)
    for i in range(4):
        p = tmp_path / f"common_{i}.docx"
        _write_custom_docx(p, "entry", f"common pass y{i}")
        paths.append(p)

    assert build_document_index(paths, db_path).documents_indexed == 7

    with DocumentIndex(db_path) as idx:
        pack = idx.search("common rare", top_k=1, reranker=_StubReranker())

    assert len(pack.hits) == 1
    assert pack.hits[0].bm25_score < 1e-4
    assert pack.floor_calibrated is False
    assert "소규모 코퍼스 어휘 보정" not in render_evidence_pack_markdown(pack)


def test_docindex_fts5_idf_floor_uses_vocabulary_coverage(tmp_path: Path) -> None:
    src = tmp_path / "sales.docx"
    _write_custom_docx(src, "매출", "매출 보고")
    db_path = tmp_path / "index.sqlite"
    assert build_document_index(src, db_path).documents_indexed == 1

    with DocumentIndex(db_path) as idx:
        result = idx.search("매출")

    assert len(result.hits) == 1
    assert result.floor_calibrated is True
    assert result.hits[0].final_score == pytest.approx(0.625)
    assert 0.0 < result.hits[0].bm25_score < 1e-4
    assert "소규모 코퍼스 어휘 보정" in render_evidence_pack_markdown(result)


@pytest.mark.parametrize("restrict_doc", [False, True])
@pytest.mark.parametrize("query", ["%", "_", "50%", r"\_%"])
def test_docindex_like_fallback_matches_punctuation_literally(
    tmp_path: Path, query: str, restrict_doc: bool
) -> None:
    src = tmp_path / "rate.docx"
    _write_custom_docx(src, "실적", r"연간 목표 150%과 첨부 기록 \_%")
    other = tmp_path / "other.docx"
    _write_custom_docx(other, "전년도", r"연간 목표 500과 첨부 기록 \X500")
    db_path = tmp_path / "index.sqlite"
    assert build_document_index([src, other], db_path).documents_indexed == 2

    with DocumentIndex(db_path) as idx:
        # Limit to the nonmatching document when checking the scoped fallback.
        doc_id = next(d.doc_id for d in idx.list_documents() if d.source_path == other.name)
        result = idx.search(query, doc_id=doc_id if restrict_doc else None)

    assert [hit.title for hit in result.hits] == ([] if restrict_doc else ["실적"])


def test_docindex_replace_replaces_same_source_path_atomically_and_preserves_on_parse_error(
    tmp_path: Path,
) -> None:
    """리뷰3 #5: 동일 경로 --replace 재색인 시 구버전 제거 및 파싱 실패 시 기존 색인 보존."""
    policy_path = tmp_path / "policy.docx"
    db_path = tmp_path / "policy.sqlite"

    # v1: 3일 전 제출
    _write_custom_docx(
        policy_path, "제3조 경비 정산 기한", "출장 경비 영수증은 귀국 후 3일 전 제출한다."
    )
    s1 = build_document_index(policy_path, db_path)
    assert s1.documents_indexed == 1

    # v2: 동일 파일 경로로 7일 전 제출로 개정 + replace=True
    _write_custom_docx(
        policy_path, "제3조 경비 정산 기한", "출장 경비 영수증은 귀국 후 7일 전 제출한다."
    )
    s2 = build_document_index(policy_path, db_path, replace=True)
    assert s2.documents_indexed == 1

    with DocumentIndex(db_path) as idx:
        docs = idx.list_documents()
        assert len(docs) == 1
        pack = idx.search("경비 영수증 제출 기한")
        assert len(pack.hits) == 1
        assert "7일 전" in pack.hits[0].full_text
        assert "3일 전" not in pack.hits[0].full_text

    # v3: 파일이 손상되어 파싱 실패해도 기존 v2 색인은 유지되어야 함
    policy_path.write_bytes(b"corrupted-not-a-zip")
    s3 = build_document_index(policy_path, db_path, replace=True)
    assert s3.documents_indexed == 0
    assert len(s3.warnings) == 1

    with DocumentIndex(db_path) as idx:
        docs = idx.list_documents()
        assert len(docs) == 1
        pack = idx.search("경비 영수증 제출 기한")
        assert len(pack.hits) == 1
        assert "7일 전" in pack.hits[0].full_text


def test_docindex_synonyms_tiny_corpus_score_and_zero_candidate_transparency(
    tmp_path: Path,
) -> None:
    """리뷰2 P-08 + 리뷰3 #7: 소규모 코퍼스 점수 보정·BM25 4자리·동의어 확장·후보 0건 투명성."""
    doc1 = tmp_path / "hr_leave.docx"
    doc2 = tmp_path / "hr_expense.docx"
    _write_custom_docx(
        doc1, "연차 신청 방법", "사내 포털에서 연차 신청서를 작성해 팀장 승인을 받는다."
    )
    _write_custom_docx(doc2, "법인카드 사용 규정", "법인카드 영수증은 월말까지 재무팀에 제출한다.")

    db_path = tmp_path / "hr.sqlite"
    build_document_index([doc1, doc2], db_path)

    with DocumentIndex(db_path) as idx:
        # 1. 문서 2건 소규모 코퍼스에서도 정확 일치 검색 시 final_score > 0 및 4자리 bm25 표기
        exact = idx.search("연차 신청 방법")
        assert len(exact.hits) >= 1
        assert exact.hits[0].final_score > 0.0
        md_exact = render_evidence_pack_markdown(exact)
        assert "bm25=" in md_exact

        # 2. 어휘가 겹치지 않는 질의('휴가 등록')는 후보 0건 모드('fts5_no_candidates') 보고
        miss = idx.search("휴가 등록")
        assert miss.total_candidates == 0
        assert miss.mode == "fts5_no_candidates"
        md_miss = render_evidence_pack_markdown(miss)
        assert "리랭커는 실행되지 않았습니다" in md_miss

        # 3. 동의어 사전(synonyms) 제공 시 '휴가 등록'으로도 '연차 신청 방법' 원문 섹션 탐색
        hit = idx.search(
            "휴가 등록",
            synonyms={"휴가": ["연차"], "등록": ["신청"]},
        )
        assert len(hit.hits) >= 1
        assert "연차 신청" in hit.hits[0].title
        assert hit.hits[0].final_score > 0.0

    # 4. CLI --synonyms 플래그도 동일하게 동작 검증
    rc = main(["query", str(db_path), "휴가 등록", "--synonyms", "휴가=연차,등록=신청"])
    assert rc == 0
