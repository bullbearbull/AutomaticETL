"""Milestone D1: VectorDB·LangGraph 없는 원문 중심 문서 인덱스(SQLite FTS5 + 목차 + DuckDB 표 SQL).

기본 모드(0MB AI/LLM):
1. Auto Parser(`ParsedDocument`, `parsed.json`, 또는 8대 문서 포맷 원본 파일)에서
   제목 계층(Outline Tree) 단위의 **원문 섹션 전체**와 추출 표를 단일 SQLite 파일에 색인한다.
2. 한국어 2-gram + 공백 토큰 혼합 FTS5 BM25 + 목차 매칭 보너스로 Top-K 원문 섹션 후보를
   수 ms 안에 탐색한다.
3. 선택적으로 Milestone D2의 초경량 CPU ONNX Cross-Encoder 리랭커를 연결하면 Top-20 후보를
   정밀 재정렬한 뒤, 청크 절단 없는 원문 전체와 출처를 ``EvidencePack``으로 반환한다.
4. 추출된 표는 격리된 메모리 DuckDB(``enable_external_access = false``)에서 읽기 전용 SQL로
   직접 집계·필터링할 수 있다.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sqlite3
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from string import ascii_lowercase, ascii_uppercase
from typing import Any

import duckdb
from sqlglot import exp
from sqlglot import parse as sqlglot_parse

from autoetl.core.context import quote_identifier
from autoetl.docindex.contracts import (
    EvidenceHit,
    EvidencePack,
    IndexBuildSummary,
    IndexedDocument,
    IndexedTableInfo,
    SectionContent,
    SectionNode,
    TableQueryResult,
)
from autoetl.docindex.reranker import CrossEncoderReranker
from autoetl.docindex.tokenizer import (
    build_fts5_match_expr,
    compute_outline_overlap,
    extract_index_tokens,
    extract_query_terms,
    index_text_for_fts,
)
from autoetl.parser.adapters import ParseOptions, ParserInputError
from autoetl.parser.contracts import ElementKind, ExtractedTable, ParsedDocument
from autoetl.parser.exporters import render_table_markdown
from autoetl.parser.pipeline import parse_document, supported_extensions

_INT_RE = re.compile(r"^[+-]?\d{1,3}(?:,\d{3})*$|^[+-]?\d+$")
_FLOAT_RE = re.compile(r"^[+-]?(?:\d{1,3}(?:,\d{3})*|\d+)\.\d+$")
_BIGINT_MIN = -(2**63)
_BIGINT_MAX = 2**63 - 1
_BIGINT_MAX_DIGITS = len(str(_BIGINT_MAX))
# DuckDB folds ASCII letters in identifiers, including quoted identifiers.
_DUCKDB_IDENTIFIER_FOLD = str.maketrans(ascii_uppercase, ascii_lowercase)
# FTS5 replaces nonpositive IDF with 1e-6. Even 32 floor-only terms score below 1e-4.
_FTS5_BM25_FLOOR_THRESHOLD = 1e-4
_FORBIDDEN_SQL_FUNCS = frozenset(
    {
        "read_csv",
        "read_csv_auto",
        "read_parquet",
        "read_json",
        "read_json_auto",
        "read_text",
        "read_blob",
        "glob",
        "duckdb_secrets",
        "duckdb_settings",
        "pragma_database_list",
    }
)


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS documents (
    doc_id TEXT PRIMARY KEY,
    source_path TEXT NOT NULL,
    format TEXT NOT NULL,
    sha256 TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    page_count INTEGER NOT NULL,
    metadata_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS document_source_paths (
    doc_id TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    canonical_path TEXT NOT NULL,
    PRIMARY KEY (doc_id, canonical_path)
);

CREATE INDEX IF NOT EXISTS idx_document_source_paths_canonical
    ON document_source_paths(canonical_path);

CREATE TABLE IF NOT EXISTS sections (
    section_id TEXT PRIMARY KEY,
    doc_id TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    parent_section_id TEXT,
    heading_level INTEGER NOT NULL,
    title TEXT NOT NULL,
    section_path_json TEXT NOT NULL,
    page_start INTEGER NOT NULL,
    page_end INTEGER NOT NULL,
    full_text TEXT NOT NULL,
    table_ids_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sections_doc_seq ON sections(doc_id, seq);

CREATE VIRTUAL TABLE IF NOT EXISTS sections_fts USING fts5(
    section_id UNINDEXED,
    title_tokens,
    path_tokens,
    body_tokens,
    tokenize='unicode61'
);

CREATE TABLE IF NOT EXISTS doc_tables (
    table_uid TEXT PRIMARY KEY,
    doc_id TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    table_id TEXT NOT NULL,
    page INTEGER NOT NULL,
    caption TEXT,
    row_count INTEGER NOT NULL,
    col_count INTEGER NOT NULL,
    headers_json TEXT NOT NULL,
    rows_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_doc_tables_doc ON doc_tables(doc_id, page);

CREATE VIRTUAL TABLE IF NOT EXISTS tables_fts USING fts5(
    table_uid UNINDEXED,
    caption_tokens,
    headers_tokens,
    sample_tokens,
    tokenize='unicode61'
);
"""


@dataclass
class _DraftSection:
    section_id: str
    seq: int
    parent_section_id: str | None
    heading_level: int
    title: str
    section_path: list[str]
    page_start: int
    page_end: int
    parts: list[str] = field(default_factory=list)
    table_ids: list[str] = field(default_factory=list)


def _source_path_keys(source_path: str) -> tuple[str, ...]:
    """디렉터리 정보가 있는 경로의 기준·해석 절대 경로 키 목록을 반환한다.

    상대 경로는 색인 프로세스의 현재 작업 디렉터리에 실제로 존재하는 경우에만
    절대 경로로 확장해, 외부에서 생성된 파싱 번들의 미존재 상대 경로를
    현재 디렉터리 파일과 혼동하지 않도록 한다.
    `os.path.abspath`처럼 심볼릭 링크 해석 전에 `..`를 어휘적으로 접지 않도록
    기준 경로와 부모 디렉터리 해석 경로(`parent.resolve() / name`)를 함께 기록한다.
    """
    raw = source_path.strip()
    path = Path(raw)
    if not raw or path.name == raw:
        return ()
    if not path.is_absolute() and not os.path.lexists(raw):
        return ()

    keys: list[str] = []
    with contextlib.suppress(OSError, RuntimeError):
        anchored = path if path.is_absolute() else (Path.cwd() / path)
        keys.append(str(anchored))
        if anchored.name not in {"", ".", ".."}:
            keys.append(str(anchored.parent.resolve() / anchored.name))
    with contextlib.suppress(OSError, RuntimeError):
        keys.append(str(path.resolve()))
    return tuple(dict.fromkeys(keys))


def _expand_query_terms(
    query: str,
    synonyms: Mapping[str, Sequence[str]] | None = None,
) -> list[str]:
    """원본 질의 토큰에 선택 동의어 사전(양방향 매칭)을 반영해 FTS5 검색 토큰을 확장한다."""
    terms = extract_query_terms(query)
    if not synonyms or not query.strip():
        return terms
    low_q = query.lower()
    seen = set(terms)
    expanded = list(terms)
    for head, aliases in synonyms.items():
        group = [head, *aliases]
        if any(item.strip() and item.strip().lower() in low_q for item in group):
            for item in group:
                for tok in extract_query_terms(item):
                    if tok not in seen:
                        seen.add(tok)
                        expanded.append(tok)
    return expanded


def _make_doc_id(sha256: str, source_path: str) -> str:
    seed = f"{sha256}:{source_path}".encode()
    return "doc_" + hashlib.sha256(seed).hexdigest()[:12]


def _extract_sections_from_parsed(doc: ParsedDocument, doc_id: str) -> list[_DraftSection]:
    """`ParsedDocument`의 읽기 순서 블록과 표를 제목 계층(Outline) 기반 원문 섹션으로 묶는다."""
    tables_by_id: dict[str, ExtractedTable] = {t.table_id: t for t in doc.tables}
    visuals_by_id = {v.region_id: v for v in doc.visual_regions}
    doc_title = (doc.document.title or Path(doc.document.source_path).stem or doc_id).strip()

    sections: list[_DraftSection] = []
    heading_stack: list[tuple[int, str, str]] = []  # (level, title, section_id)
    current: _DraftSection | None = None
    seq = 0
    seen_table_ids: set[str] = set()

    def _open_section(
        *,
        title: str,
        level: int,
        page: int,
        parent_id: str | None,
        path: list[str],
    ) -> _DraftSection:
        nonlocal seq
        seq += 1
        sec = _DraftSection(
            section_id=f"{doc_id}:s{seq:04d}",
            seq=seq,
            parent_section_id=parent_id,
            heading_level=level,
            title=title,
            section_path=path,
            page_start=page,
            page_end=page,
        )
        sections.append(sec)
        return sec

    def _ensure_current(page: int) -> _DraftSection:
        nonlocal current
        if current is None:
            current = _open_section(
                title=doc_title,
                level=0,
                page=page,
                parent_id=None,
                path=[doc_title],
            )
        else:
            current.page_end = max(current.page_end, page)
        return current

    for pg in doc.pages:
        ordered_blocks = sorted(pg.blocks, key=lambda b: b.reading_order)
        for blk in ordered_blocks:
            if blk.kind is ElementKind.HEADER_FOOTER:
                continue

            if blk.kind is ElementKind.HEADING and blk.text.strip():
                h_title = blk.text.strip()
                h_level = blk.heading_level if blk.heading_level is not None else 1
                while heading_stack and heading_stack[-1][0] >= h_level:
                    heading_stack.pop()
                parent_id = heading_stack[-1][2] if heading_stack else None
                path = [item[1] for item in heading_stack] + [h_title]
                current = _open_section(
                    title=h_title,
                    level=h_level,
                    page=pg.page,
                    parent_id=parent_id,
                    path=path,
                )
                heading_stack.append((h_level, h_title, current.section_id))
                continue

            sec = _ensure_current(pg.page)
            if blk.kind is ElementKind.TABLE and blk.table_id and blk.table_id in tables_by_id:
                tbl = tables_by_id[blk.table_id]
                if tbl.table_id not in seen_table_ids:
                    seen_table_ids.add(tbl.table_id)
                    sec.table_ids.append(tbl.table_id)
                    tbl_md = render_table_markdown(tbl).strip()
                    if tbl.caption:
                        sec.parts.append(f"[표: {tbl.caption}]\n{tbl_md}")
                    else:
                        sec.parts.append(tbl_md)
            elif blk.visual_region_id and blk.visual_region_id in visuals_by_id:
                vis = visuals_by_id[blk.visual_region_id]
                vis_body = (vis.vision_markdown or vis.vision_text or "").strip()
                if vis_body:
                    sec.parts.append(vis_body)
                elif blk.text.strip():
                    sec.parts.append(blk.text.strip())
            elif blk.text.strip():
                sec.parts.append(blk.text.strip())

        # 블록에 직접 연결되지 않은 페이지 표도 빠짐없이 현재 섹션에 포함
        for tbl in pg.tables:
            if tbl.table_id not in seen_table_ids:
                seen_table_ids.add(tbl.table_id)
                sec = _ensure_current(pg.page)
                sec.table_ids.append(tbl.table_id)
                tbl_md = render_table_markdown(tbl).strip()
                if tbl.caption:
                    sec.parts.append(f"[표: {tbl.caption}]\n{tbl_md}")
                else:
                    sec.parts.append(tbl_md)

    # 문서에 블록/표가 거의 없는 경우에도 최소 1개 루트 섹션 유지
    if not sections:
        _open_section(
            title=doc_title,
            level=0,
            page=1,
            parent_id=None,
            path=[doc_title],
        )

    return sections


def _validate_readonly_sql(sql: str) -> str:
    """표 조회 SQL이 단일 읽기 전용 SELECT/WITH 문인지 `sqlglot` AST로 검증한다."""
    cleaned = sql.strip().rstrip(";").strip()
    if not cleaned:
        raise ParserInputError("실행할 SQL 질의가 비어 있다.")
    try:
        stmts = [s for s in sqlglot_parse(cleaned, read="duckdb") if s is not None]
    except Exception as exc:
        raise ParserInputError(f"SQL 구문을 해석할 수 없다: {exc}") from exc
    if len(stmts) != 1:
        raise ParserInputError("표 조회에는 단일 SELECT 문만 허용된다.")
    inner_root: exp.Expr | None = stmts[0]
    while isinstance(inner_root, exp.Subquery):
        inner_root = inner_root.this
    if not isinstance(inner_root, (exp.Select, exp.SetOperation)):
        raise ParserInputError("보안상 표 조회에는 읽기 전용 SELECT/WITH 질의만 허용된다.")
    for node in stmts[0].walk():
        if isinstance(
            node, (exp.Command, exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create)
        ):
            raise ParserInputError("읽기 전용 SELECT 질의 외의 구문은 실행할 수 없다.")
        if isinstance(node, exp.Anonymous) and node.name.lower() in _FORBIDDEN_SQL_FUNCS:
            raise ParserInputError(f"허용되지 않는 SQL 함수다: {node.name}")
        if isinstance(node, exp.Func):
            fname = node.sql_name().lower()
            if fname in _FORBIDDEN_SQL_FUNCS:
                raise ParserInputError(f"허용되지 않는 SQL 함수다: {fname}")
    return cleaned


def _strip_currency_prefix(raw: str) -> str:
    s = raw.strip()
    if s and s[0] in "$₩€£¥":
        return s[1:].strip()
    if len(s) >= 2 and s[0] in "+-" and s[1] in "$₩€£¥":
        return s[0] + s[2:].strip()
    return s


def _infer_duckdb_col_type(values: Sequence[str]) -> tuple[str, list[Any]]:
    """정수·실수(천단위 쉼표·통화기호 포함) 표 칼럼을 수치형으로 변환해 집계 SQL을 지원한다."""
    col_type = "VARCHAR"
    numbers: list[str | None] = []
    for value in values:
        text = value.strip()
        if not text:
            numbers.append(None)
            continue
        numeric = _strip_currency_prefix(text)
        ungrouped = numeric.replace(",", "")
        if _INT_RE.fullmatch(numeric):
            digits = ungrouped.lstrip("+-")
            if len(digits) > 1 and int(digits[0]) == 0:
                if text == ungrouped:
                    break
                sign = ungrouped[: len(ungrouped) - len(digits)]
                first_significant_digit = next(
                    (i for i, digit in enumerate(digits) if int(digit)), len(digits) - 1
                )
                digits = digits[first_significant_digit:]
                ungrouped = sign + digits
            if len(digits) > _BIGINT_MAX_DIGITS:
                break
            if len(digits) == _BIGINT_MAX_DIGITS and not (
                _BIGINT_MIN <= int(ungrouped) <= _BIGINT_MAX
            ):
                break
            if col_type == "VARCHAR":
                col_type = "BIGINT"
        elif _FLOAT_RE.fullmatch(numeric):
            col_type = "DOUBLE"
        else:
            break
        numbers.append(ungrouped)
    else:
        if col_type != "VARCHAR":
            cast = int if col_type == "BIGINT" else float
            return col_type, [None if number is None else cast(number) for number in numbers]
    return "VARCHAR", [value.strip() or None for value in values]


class DocumentIndex:
    """VectorDB 없는 SQLite FTS5 + 목차 트리 + 원문 전체 + DuckDB 표 SQL 문서 인덱스."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        if str(db_path) != ":memory:":
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if str(db_path) != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(_SCHEMA_SQL)
        self._conn.commit()

    @property
    def db_path(self) -> Path:
        return self._db_path

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> DocumentIndex:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def index_parsed_document(
        self,
        doc: ParsedDocument,
        *,
        replace: bool = False,
    ) -> tuple[str, bool, int, int]:
        """`ParsedDocument` 1건을 원문 섹션·목차·FTS5·표 저장소에 색인한다.

        ``replace=True``이면 동일 ``sha256`` 또는 동일 ``source_path``(정규화 경로 포함)로
        등록된 구버전 문서를 단일 트랜잭션 안에서 원자적으로 삭제 후 새 버전으로 교체한다.

        반환값: ``(doc_id, indexed_now, section_count, table_count)``
        """
        sha = doc.document.sha256
        raw_source_path = doc.document.source_path
        if not replace:
            existing_sha = self._conn.execute(
                "SELECT doc_id FROM documents WHERE sha256 = ?", (sha,)
            ).fetchone()
            if existing_sha is not None:
                return (str(existing_sha["doc_id"]), False, 0, 0)

        path_keys = _source_path_keys(raw_source_path)
        doc_id = _make_doc_id(sha, raw_source_path)
        doc_title = (doc.document.title or Path(raw_source_path).stem or doc_id).strip()
        sections = _extract_sections_from_parsed(doc, doc_id)

        with self._conn:
            if replace:
                absolute_source_path = (
                    raw_source_path if Path(raw_source_path.strip()).is_absolute() else None
                )
                # 기존 상대 경로(path_keys 부재)는 원래 키 또는 레거시 행과 비교한다.
                self._conn.execute("BEGIN IMMEDIATE")
                raw_and_key_targets = tuple(dict.fromkeys([raw_source_path, *path_keys]))
                target_ph = ", ".join("?" for _ in raw_and_key_targets)
                union_parts = ["SELECT doc_id FROM documents WHERE sha256 = ?"]
                params: list[Any] = [sha]
                if path_keys:
                    key_ph = ", ".join("?" for _ in path_keys)
                    union_parts.append(
                        f"SELECT doc_id FROM document_source_paths "
                        f"WHERE canonical_path IN ({key_ph})"
                    )
                    params.extend(path_keys)
                union_parts.append(
                    "SELECT d.doc_id FROM documents d "
                    "LEFT JOIN document_source_paths p ON p.doc_id = d.doc_id "
                    "WHERE d.source_path = ? "
                    f"OR (p.doc_id IS NULL AND d.source_path IN ({target_ph}))"
                )
                params.append(absolute_source_path)
                params.extend(raw_and_key_targets)
                existing_rows = self._conn.execute(" UNION ".join(union_parts), params).fetchall()
                for row in existing_rows:
                    self._delete_document_in_tx(str(row["doc_id"]))

            self._conn.execute(
                "INSERT INTO documents "
                "(doc_id, source_path, format, sha256, title, page_count, metadata_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    doc_id,
                    raw_source_path,
                    doc.document.format.value,
                    sha,
                    doc_title,
                    doc.document.page_count,
                    doc.document.model_dump_json(),
                ),
            )
            for key in path_keys:
                self._conn.execute(
                    "INSERT OR IGNORE INTO document_source_paths "
                    "(doc_id, canonical_path) VALUES (?, ?)",
                    (doc_id, key),
                )

            for sec in sections:
                full_text = "\n\n".join(sec.parts).strip()
                if not full_text:
                    full_text = sec.title
                path_json = json.dumps(sec.section_path, ensure_ascii=False)
                tids_json = json.dumps(sec.table_ids, ensure_ascii=False)
                self._conn.execute(
                    "INSERT INTO sections "
                    "(section_id, doc_id, seq, parent_section_id, heading_level, title, "
                    "section_path_json, page_start, page_end, full_text, table_ids_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        sec.section_id,
                        doc_id,
                        sec.seq,
                        sec.parent_section_id,
                        sec.heading_level,
                        sec.title,
                        path_json,
                        sec.page_start,
                        sec.page_end,
                        full_text,
                        tids_json,
                    ),
                )
                self._conn.execute(
                    "INSERT INTO sections_fts "
                    "(section_id, title_tokens, path_tokens, body_tokens) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        sec.section_id,
                        index_text_for_fts(sec.title),
                        index_text_for_fts(" ".join(sec.section_path)),
                        index_text_for_fts(full_text),
                    ),
                )

            for tbl in doc.tables:
                table_uid = f"{doc_id}:{tbl.table_id}"
                headers_json = json.dumps(tbl.headers, ensure_ascii=False)
                rows_json = json.dumps(tbl.rows, ensure_ascii=False)
                sample_cells = " ".join(
                    cell for row in tbl.rows[:25] for cell in row[:20] if cell.strip()
                )
                self._conn.execute(
                    "INSERT INTO doc_tables "
                    "(table_uid, doc_id, table_id, page, caption, row_count, col_count, "
                    "headers_json, rows_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        table_uid,
                        doc_id,
                        tbl.table_id,
                        tbl.page,
                        tbl.caption,
                        tbl.row_count,
                        tbl.col_count,
                        headers_json,
                        rows_json,
                    ),
                )
                self._conn.execute(
                    "INSERT INTO tables_fts "
                    "(table_uid, caption_tokens, headers_tokens, sample_tokens) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        table_uid,
                        index_text_for_fts(tbl.caption or ""),
                        index_text_for_fts(" ".join(tbl.headers)),
                        index_text_for_fts(sample_cells),
                    ),
                )

        return (doc_id, True, len(sections), len(doc.tables))

    def _delete_document_in_tx(self, doc_id: str) -> None:
        """현재 트랜잭션 안에서 문서와 연계된 FTS5·섹션·표 행을 제거한다."""
        sec_ids = [
            str(r["section_id"])
            for r in self._conn.execute(
                "SELECT section_id FROM sections WHERE doc_id = ?", (doc_id,)
            ).fetchall()
        ]
        tbl_uids = [
            str(r["table_uid"])
            for r in self._conn.execute(
                "SELECT table_uid FROM doc_tables WHERE doc_id = ?", (doc_id,)
            ).fetchall()
        ]
        for sid in sec_ids:
            self._conn.execute("DELETE FROM sections_fts WHERE section_id = ?", (sid,))
        for uid in tbl_uids:
            self._conn.execute("DELETE FROM tables_fts WHERE table_uid = ?", (uid,))
        self._conn.execute("DELETE FROM documents WHERE doc_id = ?", (doc_id,))

    def _delete_document(self, doc_id: str) -> None:
        with self._conn:
            self._delete_document_in_tx(doc_id)

    def list_documents(self) -> list[IndexedDocument]:
        """인덱스에 등록된 모든 문서 요약 목록을 반환한다."""
        rows = self._conn.execute(
            """
            SELECT d.doc_id, d.source_path, d.format, d.sha256, d.title, d.page_count,
                   (SELECT COUNT(*) FROM sections s WHERE s.doc_id = d.doc_id) AS sec_cnt,
                   (SELECT COUNT(*) FROM doc_tables t WHERE t.doc_id = d.doc_id) AS tbl_cnt
            FROM documents d
            ORDER BY d.source_path, d.doc_id
            """
        ).fetchall()
        return [
            IndexedDocument(
                doc_id=str(r["doc_id"]),
                source_path=str(r["source_path"]),
                format=str(r["format"]),
                sha256=str(r["sha256"]),
                title=str(r["title"]),
                page_count=int(r["page_count"]),
                section_count=int(r["sec_cnt"]),
                table_count=int(r["tbl_cnt"]),
            )
            for r in rows
        ]

    def get_outline(self, doc_id: str | None = None) -> list[SectionNode]:
        """문서 전체 또는 특정 문서의 제목 계층 목차(Outline Tree)를 반환한다."""
        if doc_id is not None:
            rows = self._conn.execute(
                """
                SELECT s.*, d.source_path
                FROM sections s
                JOIN documents d ON d.doc_id = s.doc_id
                WHERE s.doc_id = ?
                ORDER BY s.seq
                """,
                (doc_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT s.*, d.source_path
                FROM sections s
                JOIN documents d ON d.doc_id = s.doc_id
                ORDER BY d.source_path, s.seq
                """
            ).fetchall()
        return [self._row_to_section_node(r) for r in rows]

    def read_section(self, section_id: str) -> SectionContent:
        """지정한 섹션의 원문 텍스트 전체(청크 절단 없음)와 포함 표 목록을 반환한다."""
        row = self._conn.execute(
            """
            SELECT s.*, d.source_path
            FROM sections s
            JOIN documents d ON d.doc_id = s.doc_id
            WHERE s.section_id = ?
            """,
            (section_id,),
        ).fetchone()
        if row is None:
            raise ParserInputError(f"섹션을 찾을 수 없다: '{section_id}'")
        node = self._row_to_section_node(row)
        tables: list[IndexedTableInfo] = []
        if node.table_ids:
            uids = [f"{node.doc_id}:{tid}" for tid in node.table_ids]
            placeholders = ",".join("?" for _ in uids)
            trows = self._conn.execute(
                f"""
                SELECT t.*, d.source_path
                FROM doc_tables t
                JOIN documents d ON d.doc_id = t.doc_id
                WHERE t.table_uid IN ({placeholders})
                ORDER BY t.page, t.table_id
                """,
                uids,
            ).fetchall()
            tables = [self._row_to_table_info(tr) for tr in trows]
        return SectionContent(section=node, full_text=str(row["full_text"]), tables=tables)

    def list_tables(self, doc_id: str | None = None) -> list[IndexedTableInfo]:
        """인덱스에 저장된 추출 표 목록(헤더·행/열 수·출처 포함)을 반환한다."""
        if doc_id is not None:
            rows = self._conn.execute(
                """
                SELECT t.*, d.source_path
                FROM doc_tables t
                JOIN documents d ON d.doc_id = t.doc_id
                WHERE t.doc_id = ?
                ORDER BY t.page, t.table_id
                """,
                (doc_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT t.*, d.source_path
                FROM doc_tables t
                JOIN documents d ON d.doc_id = t.doc_id
                ORDER BY d.source_path, t.page, t.table_id
                """
            ).fetchall()
        return [self._row_to_table_info(r) for r in rows]

    def read_table(self, table_uid: str, *, limit: int = 50) -> TableQueryResult:
        """지정한 추출 표의 상위 N행을 반환한다."""
        safe_limit = max(1, min(500, int(limit)))
        return self.query_table(table_uid, f"SELECT * FROM tbl LIMIT {safe_limit}", max_rows=500)

    def query_table(
        self,
        table_uid: str,
        sql: str = "SELECT * FROM tbl LIMIT 50",
        *,
        max_rows: int = 200,
    ) -> TableQueryResult:
        """지정한 추출 표를 격리 DuckDB 테이블(`tbl`)에 적재해 읽기 전용 SQL을 실행한다."""
        row = self._conn.execute(
            """
            SELECT t.*, d.source_path
            FROM doc_tables t
            JOIN documents d ON d.doc_id = t.doc_id
            WHERE t.table_uid = ?
            """,
            (table_uid,),
        ).fetchone()
        if row is None:
            raise ParserInputError(f"추출 표를 찾을 수 없다: '{table_uid}'")

        validated_sql = _validate_readonly_sql(sql)
        headers: list[str] = json.loads(str(row["headers_json"]))
        data_rows: list[list[str]] = json.loads(str(row["rows_json"]))
        n_cols = max(len(headers), max((len(r) for r in data_rows), default=0))
        if n_cols == 0:
            return TableQueryResult(
                table_uid=table_uid,
                source_path=str(row["source_path"]),
                table_id=str(row["table_id"]),
                page=int(row["page"]),
                columns=[],
                rows=[],
                total_matched=0,
                truncated=False,
                sql_executed=validated_sql,
            )

        # 중복/빈 헤더를 안전하게 유일화
        unique_headers: list[str] = []
        used_headers: set[str] = set()
        next_suffix: dict[str, int] = {}
        for i in range(n_cols):
            name = (headers[i].strip() if i < len(headers) else "") or f"col_{i + 1}"
            folded = name.translate(_DUCKDB_IDENTIFIER_FOLD)
            if folded in used_headers:
                suffix = next_suffix.get(folded, 2)
                while f"{folded}_{suffix}" in used_headers:
                    suffix += 1
                next_suffix[folded] = suffix + 1
                name = f"{name}_{suffix}"
                folded = f"{folded}_{suffix}"
            used_headers.add(folded)
            unique_headers.append(name)

        # 열별 수치 자동 판정으로 SUM/AVG/비교 SQL 바로 동작 지원
        col_types: list[str] = []
        typed_cols: list[list[Any]] = []
        for col_idx in range(n_cols):
            raw_col = [r[col_idx] if col_idx < len(r) else "" for r in data_rows]
            c_type, c_vals = _infer_duckdb_col_type(raw_col)
            col_types.append(c_type)
            typed_cols.append(c_vals)

        dcon = duckdb.connect(":memory:")
        try:
            dcon.execute("SET enable_external_access = false")
            col_defs = ", ".join(
                f"{quote_identifier(h)} {t}" for h, t in zip(unique_headers, col_types, strict=True)
            )
            dcon.execute(f"CREATE TABLE tbl ({col_defs})")
            if data_rows:
                placeholders = ", ".join("?" for _ in range(n_cols))
                transposed = [
                    tuple(typed_cols[c][r] for c in range(n_cols)) for r in range(len(data_rows))
                ]
                dcon.executemany(f"INSERT INTO tbl VALUES ({placeholders})", transposed)

            cursor = dcon.execute(validated_sql)
            desc = cursor.description or []
            out_cols = [str(col_desc[0]) for col_desc in desc]
            fetched = cursor.fetchall()
            total_matched = len(fetched)
            cap = max(1, min(1000, int(max_rows)))
            truncated = total_matched > cap
            out_rows = [
                ["" if cell is None else str(cell) for cell in rec] for rec in fetched[:cap]
            ]
            return TableQueryResult(
                table_uid=table_uid,
                source_path=str(row["source_path"]),
                table_id=str(row["table_id"]),
                page=int(row["page"]),
                columns=out_cols,
                rows=out_rows,
                total_matched=total_matched,
                truncated=truncated,
                sql_executed=validated_sql,
            )
        except duckdb.Error as exc:
            raise ParserInputError(f"DuckDB 표 SQL 실행 오류: {exc}") from exc
        finally:
            dcon.close()

    def search_tables(
        self,
        query: str,
        *,
        top_k: int = 5,
        doc_id: str | None = None,
        synonyms: Mapping[str, Sequence[str]] | None = None,
    ) -> list[IndexedTableInfo]:
        """질의와 관련된 추출 표 후보를 FTS5 BM25로 탐색한다."""
        terms = _expand_query_terms(query, synonyms)
        match_expr = build_fts5_match_expr(terms)
        if not match_expr:
            return []
        if doc_id is not None:
            rows = self._conn.execute(
                """
                SELECT t.*, d.source_path,
                       bm25(tables_fts, 0.0, 3.0, 2.5, 1.0) AS rank_score
                FROM tables_fts
                JOIN doc_tables t ON t.table_uid = tables_fts.table_uid
                JOIN documents d ON d.doc_id = t.doc_id
                WHERE tables_fts MATCH ? AND t.doc_id = ?
                ORDER BY rank_score
                LIMIT ?
                """,
                (match_expr, doc_id, top_k),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT t.*, d.source_path,
                       bm25(tables_fts, 0.0, 3.0, 2.5, 1.0) AS rank_score
                FROM tables_fts
                JOIN doc_tables t ON t.table_uid = tables_fts.table_uid
                JOIN documents d ON d.doc_id = t.doc_id
                WHERE tables_fts MATCH ?
                ORDER BY rank_score
                LIMIT ?
                """,
                (match_expr, top_k),
            ).fetchall()
        return [self._row_to_table_info(r) for r in rows]

    def search(
        self,
        query: str,
        *,
        top_k: int = 5,
        candidate_k: int = 20,
        doc_id: str | None = None,
        reranker: CrossEncoderReranker | None = None,
        synonyms: Mapping[str, Sequence[str]] | None = None,
    ) -> EvidencePack:
        """2단계 원문 리트리벌(0MB FTS5 BM25+목차 → 선택 초경량 CPU ONNX 리랭커 → EvidencePack)."""
        t0 = time.perf_counter()
        query_terms = _expand_query_terms(query, synonyms)
        match_expr = build_fts5_match_expr(query_terms)
        fetch_k = max(top_k, candidate_k)

        rows: list[sqlite3.Row] = []
        if match_expr:
            if doc_id is not None:
                rows = self._conn.execute(
                    """
                    SELECT s.*, d.source_path,
                           bm25(sections_fts, 0.0, 4.0, 2.5, 1.0) AS rank_score
                    FROM sections_fts
                    JOIN sections s ON s.section_id = sections_fts.section_id
                    JOIN documents d ON d.doc_id = s.doc_id
                    WHERE sections_fts MATCH ? AND s.doc_id = ?
                    ORDER BY rank_score
                    LIMIT ?
                    """,
                    (match_expr, doc_id, fetch_k),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """
                    SELECT s.*, d.source_path,
                           bm25(sections_fts, 0.0, 4.0, 2.5, 1.0) AS rank_score
                    FROM sections_fts
                    JOIN sections s ON s.section_id = sections_fts.section_id
                    JOIN documents d ON d.doc_id = s.doc_id
                    WHERE sections_fts MATCH ?
                    ORDER BY rank_score
                    LIMIT ?
                    """,
                    (match_expr, fetch_k),
                ).fetchall()

        # FTS5 결과가 비어 있으면 짧은 문자열/기호 질의를 위한 부분 일치 폴백
        stripped_query = query.strip()
        if not rows and stripped_query:
            escaped_query = (
                stripped_query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            like_pat = f"%{escaped_query}%"
            if doc_id is not None:
                rows = self._conn.execute(
                    r"""
                    SELECT s.*, d.source_path, -0.5 AS rank_score
                    FROM sections s
                    JOIN documents d ON d.doc_id = s.doc_id
                    WHERE s.doc_id = ?
                      AND (s.title LIKE ? ESCAPE '\' OR s.full_text LIKE ? ESCAPE '\')
                    LIMIT ?
                    """,
                    (doc_id, like_pat, like_pat, fetch_k),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    r"""
                    SELECT s.*, d.source_path, -0.5 AS rank_score
                    FROM sections s
                    JOIN documents d ON d.doc_id = s.doc_id
                    WHERE s.title LIKE ? ESCAPE '\' OR s.full_text LIKE ? ESCAPE '\'
                    LIMIT ?
                    """,
                    (like_pat, like_pat, fetch_k),
                ).fetchall()

        use_floor_calibration = bool(rows) and all(
            round(max(0.0, -float(r["rank_score"])), 6) < _FTS5_BM25_FLOOR_THRESHOLD for r in rows
        )
        candidates: list[EvidenceHit] = []
        for r in rows:
            title = str(r["title"])
            sec_path: list[str] = json.loads(str(r["section_path_json"]))
            full_text = str(r["full_text"])
            table_ids: list[str] = json.loads(str(r["table_ids_json"]))
            raw_bm25 = max(0.0, -float(r["rank_score"]))
            bm25_score = round(raw_bm25, 6)

            haystack_raw = f"{title} {' '.join(sec_path)} {full_text}"
            haystack = haystack_raw.lower()
            matched = [t for t in query_terms if t in haystack][:12]
            outline_boost = compute_outline_overlap(query_terms, title, sec_path)

            if not use_floor_calibration or bm25_score >= _FTS5_BM25_FLOOR_THRESHOLD:
                bm25_norm = raw_bm25 / (1.0 + raw_bm25)
            else:
                # 후보 전체가 FTS5 IDF 하한일 때만 어휘 커버리지 보정을 적용해 척도 역전을 방지한다.
                haystack_tokens = set(extract_index_tokens(haystack_raw))
                overlap = (
                    sum(1 for t in query_terms if t in haystack_tokens) / max(len(query_terms), 1)
                    if query_terms
                    else 0.0
                )
                bm25_norm = max(0.05, 0.5 * overlap) if (matched or overlap > 0.0) else 0.0

            stage1_score = round(0.75 * bm25_norm + 0.25 * outline_boost, 6)

            candidates.append(
                EvidenceHit(
                    section_id=str(r["section_id"]),
                    doc_id=str(r["doc_id"]),
                    source_path=str(r["source_path"]),
                    title=title,
                    section_path=sec_path,
                    page_start=int(r["page_start"]),
                    page_end=int(r["page_end"]),
                    full_text=full_text,
                    bm25_score=bm25_score,
                    outline_boost=round(outline_boost, 6),
                    rerank_score=None,
                    final_score=stage1_score,
                    matched_terms=matched,
                    table_ids=table_ids,
                )
            )

        mode = "fts5_bm25" if candidates else "fts5_no_candidates"
        reranker_name: str | None = None
        if reranker is not None and candidates:
            passages = [f"{c.title}\n{c.full_text}" for c in candidates]
            r_scores = reranker.score_pairs(query, passages)
            reranked: list[EvidenceHit] = []
            for cand, r_score in zip(candidates, r_scores, strict=True):
                bm25_norm = cand.bm25_score / (1.0 + cand.bm25_score)
                blended = round(0.80 * r_score + 0.15 * bm25_norm + 0.05 * cand.outline_boost, 6)
                reranked.append(
                    cand.model_copy(
                        update={
                            "rerank_score": round(r_score, 6),
                            "final_score": blended,
                        }
                    )
                )
            candidates = reranked
            mode = "fts5+onnx_rerank"
            reranker_name = reranker.name

        candidates.sort(key=lambda h: (h.final_score, h.bm25_score), reverse=True)
        table_hits = self.search_tables(
            query, top_k=min(5, top_k), doc_id=doc_id, synonyms=synonyms
        )
        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 3)

        return EvidencePack(
            query=query,
            mode=mode,
            reranker_model=reranker_name,
            floor_calibrated=use_floor_calibration and reranker is None,
            elapsed_ms=elapsed_ms,
            total_candidates=len(candidates),
            hits=candidates[:top_k],
            table_hits=table_hits,
        )

    @staticmethod
    def _row_to_section_node(row: sqlite3.Row) -> SectionNode:
        return SectionNode(
            section_id=str(row["section_id"]),
            doc_id=str(row["doc_id"]),
            source_path=str(row["source_path"]),
            parent_section_id=(
                str(row["parent_section_id"]) if row["parent_section_id"] is not None else None
            ),
            heading_level=int(row["heading_level"]),
            title=str(row["title"]),
            section_path=json.loads(str(row["section_path_json"])),
            page_start=int(row["page_start"]),
            page_end=int(row["page_end"]),
            char_count=len(str(row["full_text"])),
            table_ids=json.loads(str(row["table_ids_json"])),
        )

    @staticmethod
    def _row_to_table_info(row: sqlite3.Row) -> IndexedTableInfo:
        return IndexedTableInfo(
            table_uid=str(row["table_uid"]),
            doc_id=str(row["doc_id"]),
            source_path=str(row["source_path"]),
            table_id=str(row["table_id"]),
            page=int(row["page"]),
            caption=str(row["caption"]) if row["caption"] is not None else None,
            row_count=int(row["row_count"]),
            col_count=int(row["col_count"]),
            headers=json.loads(str(row["headers_json"])),
        )


def _discover_index_inputs(
    inputs: Sequence[str | Path],
    *,
    recursive: bool = True,
) -> list[Path]:
    """폴더·파일 목록에서 `parsed.json` 또는 Auto Parser 지원 8대 문서를 결정론 순서로 찾는다."""
    doc_exts = set(supported_extensions())
    found: list[Path] = []
    seen: set[Path] = set()

    for raw in inputs:
        p = Path(raw)
        if not p.exists():
            raise ParserInputError(f"인덱싱 대상 파일 또는 폴더가 없다: {p}")
        if p.is_file():
            rp = p.resolve()
            if rp not in seen:
                seen.add(rp)
                found.append(p)
            continue

        # 폴더 내부에 parsed.json 번들이 있으면 우선 수집, 아니면 원본 문서 파일 수집
        iterator = sorted(p.rglob("*") if recursive else p.glob("*"))
        parsed_jsons = [f for f in iterator if f.is_file() and f.name == "parsed.json"]
        if parsed_jsons:
            for pj in parsed_jsons:
                rp = pj.resolve()
                if rp not in seen:
                    seen.add(rp)
                    found.append(pj)
        else:
            for f in iterator:
                if f.is_file() and f.suffix.lower() in doc_exts:
                    rp = f.resolve()
                    if rp not in seen:
                        seen.add(rp)
                        found.append(f)

    if not found:
        raise ParserInputError("인덱싱할 `parsed.json` 또는 지원 문서 파일을 찾지 못했다.")
    return found


def build_document_index(
    inputs: Sequence[str | Path] | str | Path,
    db_path: str | Path,
    *,
    recursive: bool = True,
    replace: bool = False,
) -> IndexBuildSummary:
    """`parsed.json` 번들 또는 원본 문서 파일/폴더에서 SQLite FTS5 원문 인덱스를 구축한다."""
    t0 = time.perf_counter()
    in_list: list[str | Path] = [inputs] if isinstance(inputs, (str, Path)) else list(inputs)
    files = _discover_index_inputs(in_list, recursive=recursive)

    indexed = 0
    skipped = 0
    total_sections = 0
    total_tables = 0
    warnings: list[str] = []

    with DocumentIndex(db_path) as idx:
        for fpath in files:
            try:
                if fpath.suffix.lower() == ".json":
                    parsed = ParsedDocument.model_validate_json(fpath.read_text(encoding="utf-8"))
                else:
                    parsed = parse_document(fpath, options=ParseOptions(extract_crops=False))
                _doc_id, did_index, n_sec, n_tbl = idx.index_parsed_document(
                    parsed, replace=replace
                )
                if did_index:
                    indexed += 1
                    total_sections += n_sec
                    total_tables += n_tbl
                else:
                    skipped += 1
            except Exception as exc:
                warnings.append(f"{fpath.name}: 인덱싱 건너뜀 ({type(exc).__name__})")

    return IndexBuildSummary(
        db_path=str(Path(db_path)),
        documents_indexed=indexed,
        documents_skipped=skipped,
        sections_indexed=total_sections,
        tables_indexed=total_tables,
        elapsed_seconds=round(time.perf_counter() - t0, 3),
        warnings=warnings,
    )


def render_evidence_pack_markdown(pack: EvidencePack) -> str:
    """`EvidencePack`을 출처·점수·원문 섹션 전체가 보이는 Markdown으로 렌더링한다."""
    mode_lbl = f"{pack.mode} ({pack.reranker_model})" if pack.reranker_model else pack.mode
    lines: list[str] = [
        f"# 문서 원문 검색 결과 (`{pack.query}`)",
        "",
        f"- **검색 모드**: `{mode_lbl}` · 후보 {pack.total_candidates}건 · {pack.elapsed_ms:.1f}ms",
        f"- **반환된 원문 섹션**: {len(pack.hits)}건 · 관련 표 {len(pack.table_hits)}건",
        "",
    ]

    if not pack.hits:
        lines.append(
            "일치하는 원문 섹션을 찾지 못했습니다. "
            "(1단계 FTS5 어휘/2-gram 후보가 0건이어서 2단계 리랭커는 실행되지 않았습니다 — "
            "동의어 사전(`--synonyms`/`synonyms`) 또는 목차 탐색(`get_outline`)을 활용해 보세요.)"
        )
        return "\n".join(lines) + "\n"

    for rank, hit in enumerate(pack.hits, start=1):
        path_str = " > ".join(hit.section_path) if hit.section_path else hit.title
        page_lbl = (
            f"p.{hit.page_start}"
            if hit.page_start == hit.page_end
            else f"p.{hit.page_start}-{hit.page_end}"
        )
        score_detail = f"final={hit.final_score:.3f}, bm25={hit.bm25_score:.4f}"
        if (
            pack.floor_calibrated
            and hit.bm25_score < _FTS5_BM25_FLOOR_THRESHOLD
            and hit.final_score > 0.0
        ):
            score_detail += " (소규모 코퍼스 어휘 보정)"
        if hit.rerank_score is not None:
            score_detail += f", rerank={hit.rerank_score:.3f}"
        lines.extend(
            [
                f"## [{rank}] {path_str}",
                (
                    f"- **출처**: `{hit.source_path}` ({page_lbl}) · "
                    f"`{hit.section_id}` · ({score_detail})"
                ),
                "",
                hit.full_text,
                "",
            ]
        )

    if pack.table_hits:
        lines.extend(["## 관련 추출 표 (DuckDB SQL 조회 가능)", ""])
        for tbl in pack.table_hits:
            hdrs = ", ".join(tbl.headers[:8])
            cap = f" — {tbl.caption}" if tbl.caption else ""
            lines.append(
                f"- `{tbl.table_uid}` ({tbl.source_path}, p.{tbl.page}, "
                f"{tbl.row_count}x{tbl.col_count}){cap}: [{hdrs}]"
            )
        lines.append("")

    return "\n".join(lines)


__all__ = [
    "DocumentIndex",
    "build_document_index",
    "render_evidence_pack_markdown",
]
