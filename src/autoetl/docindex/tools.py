"""Milestone D2: 프레임워크 비종속 에이전트 / MCP 표준 도구 인터페이스(`DocumentToolkit`).

LangGraph·MCP·OpenAI Tool Calling·Anthropic Tool Use·사내 자체 봇 등 어떤 에이전트 프레임워크에도
바로 연결할 수 있도록 순수 Python 메서드 5종과 JSON Schema 스펙·단일 디스패처를 제공한다.
에이전트에는 항상 청크로 잘리지 않은 원문 섹션 전체와 DuckDB 표 SQL 실행 결과를 전달한다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from autoetl.docindex.reranker import CrossEncoderReranker
from autoetl.docindex.store import DocumentIndex
from autoetl.parser.adapters import ParserInputError


class DocumentToolkit:
    """`DocumentIndex`를 감싸 에이전트·MCP용 표준 5종 도구와 JSON Schema를 제공한다."""

    def __init__(
        self,
        index: DocumentIndex | str | Path,
        *,
        reranker: CrossEncoderReranker | None = None,
    ) -> None:
        if isinstance(index, DocumentIndex):
            self._index = index
            self._owns_index = False
        else:
            self._index = DocumentIndex(index)
            self._owns_index = True
        self._reranker = reranker

    def close(self) -> None:
        if self._owns_index:
            self._index.close()

    def __enter__(self) -> DocumentToolkit:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def search_documents(
        self,
        query: str,
        *,
        top_k: int = 5,
        doc_id: str | None = None,
        synonyms: Mapping[str, Sequence[str]] | None = None,
    ) -> dict[str, Any]:
        """질의에 가장 관련 높은 원문 섹션 전체와 관련 표 목록(EvidencePack)을 반환한다."""
        pack = self._index.search(
            query,
            top_k=max(1, min(20, int(top_k))),
            doc_id=doc_id,
            reranker=self._reranker,
            synonyms=synonyms,
        )
        return pack.model_dump(mode="json")

    def get_outline(self, *, doc_id: str | None = None) -> dict[str, Any]:
        """인덱싱된 문서 목록과 제목 계층 목차(Outline Tree)를 반환한다."""
        docs = self._index.list_documents()
        if doc_id is not None:
            docs = [d for d in docs if d.doc_id == doc_id]
        nodes = self._index.get_outline(doc_id=doc_id)
        return {
            "documents": [d.model_dump(mode="json") for d in docs],
            "sections": [n.model_dump(mode="json") for n in nodes],
        }

    def read_section(self, section_id: str) -> dict[str, Any]:
        """지정한 `section_id`의 원문 전체 텍스트와 포함 표 메타데이터를 반환한다."""
        content = self._index.read_section(section_id)
        return content.model_dump(mode="json")

    def list_tables(self, *, doc_id: str | None = None) -> dict[str, Any]:
        """문서들에서 추출되어 SQL 조회가 가능한 표 목록(헤더·행/열 수)을 반환한다."""
        tables = self._index.list_tables(doc_id=doc_id)
        return {"tables": [t.model_dump(mode="json") for t in tables]}

    def query_table(
        self,
        table_uid: str,
        sql: str = "SELECT * FROM tbl LIMIT 50",
    ) -> dict[str, Any]:
        """추출 표(`tbl`)에 읽기 전용 DuckDB SQL(SELECT/WITH)을 실행해 결과를 반환한다."""
        res = self._index.query_table(table_uid, sql)
        return res.model_dump(mode="json")

    @staticmethod
    def tool_schemas() -> list[dict[str, Any]]:
        """OpenAI / MCP / Anthropic Tool Use 호환 JSON Schema 스펙 5종을 반환한다."""
        return [
            {
                "name": "search_documents",
                "description": (
                    "SQLite FTS5 BM25(+선택 CPU ONNX 리랭커)로 문서 원문 섹션을 검색하고 "
                    "절단 없는 원문 전체·출처·관련 표(EvidencePack)를 반환한다."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "검색할 자연어 질문 또는 키워드",
                        },
                        "top_k": {
                            "type": "integer",
                            "description": "반환할 원문 섹션 개수(기본 5, 최대 20)",
                            "default": 5,
                            "minimum": 1,
                            "maximum": 20,
                        },
                        "doc_id": {
                            "type": ["string", "null"],
                            "description": "특정 문서로 범위를 제한할 때 doc_id",
                        },
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "get_outline",
                "description": (
                    "인덱싱된 문서 목록과 제목 계층 목차(Outline Tree)를 조회해 "
                    "어느 섹션을 원문 그대로 읽을지 탐색한다."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "doc_id": {
                            "type": ["string", "null"],
                            "description": "특정 문서의 목차만 볼 때 doc_id (생략 시 전체)",
                        }
                    },
                },
            },
            {
                "name": "read_section",
                "description": (
                    "목차나 검색 결과에서 확인한 section_id의 원문 텍스트 전체와 "
                    "해당 섹션 내 추출 표 목록을 절단 없이 읽는다."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "section_id": {
                            "type": "string",
                            "description": "읽을 섹션의 section_id (예: 'doc_xxx:s0003')",
                        }
                    },
                    "required": ["section_id"],
                },
            },
            {
                "name": "list_tables",
                "description": (
                    "문서에서 추출된 표 목록(table_uid, 출처, 페이지, 헤더, 행/열 수)을 조회한다."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "doc_id": {
                            "type": ["string", "null"],
                            "description": "특정 문서의 표만 볼 때 doc_id",
                        }
                    },
                },
            },
            {
                "name": "query_table",
                "description": (
                    "추출 표 하나를 격리 DuckDB 테이블 `tbl`로 로드해 읽기 전용 SELECT/WITH SQL을 "
                    "실행한다(합계·평균·필터링·정렬 등 정확한 수치 집계용)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "table_uid": {
                            "type": "string",
                            "description": "조회할 표의 table_uid (예: 'doc_xxx:p1_t1')",
                        },
                        "sql": {
                            "type": "string",
                            "description": "테이블 `tbl` 대상 읽기 전용 SELECT SQL",
                            "default": "SELECT * FROM tbl LIMIT 50",
                        },
                    },
                    "required": ["table_uid"],
                },
            },
        ]

    def dispatch(self, tool_name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """에이전트 도구 호출 `(tool_name, arguments)`을 단일 진입점으로 실행한다."""
        args = arguments or {}
        if tool_name == "search_documents":
            return self.search_documents(
                query=str(args["query"]),
                top_k=int(args.get("top_k", 5)),
                doc_id=args.get("doc_id"),
            )
        if tool_name == "get_outline":
            return self.get_outline(doc_id=args.get("doc_id"))
        if tool_name == "read_section":
            return self.read_section(section_id=str(args["section_id"]))
        if tool_name == "list_tables":
            return self.list_tables(doc_id=args.get("doc_id"))
        if tool_name == "query_table":
            return self.query_table(
                table_uid=str(args["table_uid"]),
                sql=str(args.get("sql", "SELECT * FROM tbl LIMIT 50")),
            )
        raise ParserInputError(f"알 수 없는 문서 도구 이름이다: '{tool_name}'")


__all__ = ["DocumentToolkit"]
