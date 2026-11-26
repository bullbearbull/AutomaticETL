"""Part 2 (D1~D2) 원문 문서 인덱스·검색·표 SQL·EvidencePack 계약 모델."""

from __future__ import annotations

from pydantic import Field, NonNegativeInt, PositiveInt

from autoetl.contracts.common import ContractModel

DOCINDEX_CONTRACT_VERSION = "0.1.0"


class IndexedDocument(ContractModel):
    """문서 인덱스에 등록된 단일 원본 문서 메타데이터."""

    doc_id: str
    source_path: str
    format: str
    sha256: str
    title: str
    page_count: PositiveInt
    section_count: NonNegativeInt = 0
    table_count: NonNegativeInt = 0


class SectionNode(ContractModel):
    """문서 목차(Outline) 트리의 단일 섹션 노드."""

    section_id: str
    doc_id: str
    source_path: str
    parent_section_id: str | None = None
    heading_level: NonNegativeInt = 0
    title: str
    section_path: list[str] = Field(default_factory=list)
    page_start: PositiveInt
    page_end: PositiveInt
    char_count: NonNegativeInt
    table_ids: list[str] = Field(default_factory=list)


class IndexedTableInfo(ContractModel):
    """문서 인덱스 안에 저장된 추출 표 메타데이터."""

    table_uid: str
    doc_id: str
    source_path: str
    table_id: str
    page: PositiveInt
    caption: str | None = None
    row_count: NonNegativeInt
    col_count: NonNegativeInt
    headers: list[str] = Field(default_factory=list)


class SectionContent(ContractModel):
    """청크 절단 없이 원문 전체를 반환하는 단일 섹션 내용."""

    section: SectionNode
    full_text: str
    tables: list[IndexedTableInfo] = Field(default_factory=list)


class TableQueryResult(ContractModel):
    """추출 표 대상 읽기 전용 DuckDB SQL 조회 결과."""

    table_uid: str
    source_path: str
    table_id: str
    page: PositiveInt
    columns: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)
    total_matched: NonNegativeInt
    truncated: bool = False
    sql_executed: str


class EvidenceHit(ContractModel):
    """질의에 매칭된 원문 섹션 근거 1건(출처·점수·원문 전체 포함)."""

    section_id: str
    doc_id: str
    source_path: str
    title: str
    section_path: list[str] = Field(default_factory=list)
    page_start: PositiveInt
    page_end: PositiveInt
    full_text: str
    bm25_score: float
    outline_boost: float = 0.0
    rerank_score: float | None = None
    final_score: float
    matched_terms: list[str] = Field(default_factory=list)
    table_ids: list[str] = Field(default_factory=list)


class EvidencePack(ContractModel):
    """에이전트·사용자에게 반환하는 원문 중심 근거 번들(VectorDB 미사용)."""

    contract_version: str = DOCINDEX_CONTRACT_VERSION
    query: str
    mode: str = Field(description="검색 모드('fts5_bm25' 또는 'fts5+onnx_rerank')")
    reranker_model: str | None = None
    floor_calibrated: bool = False
    elapsed_ms: float = Field(ge=0.0)
    total_candidates: NonNegativeInt = 0
    hits: list[EvidenceHit] = Field(default_factory=list)
    table_hits: list[IndexedTableInfo] = Field(default_factory=list)


class IndexBuildSummary(ContractModel):
    """문서 인덱스 구축 결과 요약."""

    contract_version: str = DOCINDEX_CONTRACT_VERSION
    db_path: str
    documents_indexed: NonNegativeInt
    documents_skipped: NonNegativeInt
    sections_indexed: NonNegativeInt
    tables_indexed: NonNegativeInt
    elapsed_seconds: float = Field(ge=0.0)
    warnings: list[str] = Field(default_factory=list)
