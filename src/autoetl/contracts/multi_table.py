"""다중 테이블 결정론적 분석·칼럼 정밀 분류·문자열 패턴 분해 계약(E1~E4)."""

from __future__ import annotations

from enum import Enum

from pydantic import Field, NonNegativeInt

from autoetl.contracts.column_types import TypeAssignment
from autoetl.contracts.common import (
    CONTRACTS_VERSION,
    ContractModel,
    PhysicalType,
    Purpose,
    SemanticType,
)
from autoetl.contracts.ingest import IngestManifest, SamplingPlan
from autoetl.contracts.load import (
    ColumnMapping,
    DeduplicationRule,
    IncrementMode,
    LoadOrder,
)
from autoetl.contracts.profile import ColumnProfile
from autoetl.contracts.relations import RelationGraph
from autoetl.contracts.schema import SchemaCandidate, Scorecard
from autoetl.contracts.stats import ColumnStats


class TableSourceKind(str, Enum):
    """다중 테이블 입력 소스 유형."""

    CSV = "csv"
    TSV = "tsv"
    XLSX_SHEET = "xlsx_sheet"
    EXTRACTED_TABLE = "extracted_table"


class ColumnRoleCategory(str, Enum):
    """데이터 분석·DB 설계 관점의 칼럼 정밀 유형."""

    INDEX = "index"
    DISCRETE_NUMERIC = "discrete_numeric"
    CONTINUOUS_NUMERIC = "continuous_numeric"
    CATEGORICAL_NOMINAL = "categorical_nominal"
    CATEGORICAL_ORDINAL = "categorical_ordinal"
    TEMPORAL = "temporal"
    STRUCTURED_STRING = "structured_string"
    CODE_IDENTIFIER = "code_identifier"
    FREE_TEXT = "free_text"
    BOOLEAN = "boolean"


class IndexMetadata(ContractModel):
    """정수/식별자 칼럼의 인덱스·순번 특성 지표."""

    is_unique: bool
    is_monotonic_increasing: bool
    starts_at_zero_or_one: bool
    contiguous_step_rate: float = Field(ge=0, le=1, description="인접 행 차분이 1인 비율")
    likely_surrogate_index: bool = Field(
        description="유일성 + 단조증가 + 순번 특성으로 보아 행번호/서로게이트 키일 가능성"
    )


class StringSegmentKind(str, Enum):
    """규칙 문자열 분해 세그먼트 종류(공통 고정부 vs 가변 슬롯)."""

    LITERAL = "literal"
    SLOT = "slot"


class StringSegment(ContractModel):
    """문자열 내부 규칙 분해 결과의 개별 세그먼트(고정부 또는 가변 슬롯)."""

    position: NonNegativeInt
    kind: StringSegmentKind
    literal_value: str | None = Field(default=None, description="고정부일 때 공통 문자열")
    slot_name: str | None = Field(default=None, description="가변 슬롯일 때 표준 식별자")
    slot_category: ColumnRoleCategory | None = Field(
        default=None, description="가변 슬롯 값의 세부 데이터 유형"
    )
    slot_physical_type: PhysicalType | None = None
    unique_count: NonNegativeInt = 0
    min_length: NonNegativeInt = 0
    max_length: NonNegativeInt = 0
    zero_padded: bool = False
    sample_pattern: str | None = Field(
        default=None, description="원문 값 대신 대표 형태 마스크(예: '9999', 'AA')"
    )
    capture_group: NonNegativeInt | None = Field(
        default=None, description="전체 정규식 내 1-기반 캡처 그룹 번호"
    )
    extract_sql_hint: str | None = Field(
        default=None, description="DuckDB/PostgreSQL에서 해당 슬롯을 파싱하는 표현식 힌트"
    )


class StringPatternDecomposition(ContractModel):
    """규칙성 문자열의 공통 고정부·가변 슬롯 분해 결과."""

    strategy: str = Field(description="적용된 분해 전략('delimiter', 'char_class', 'fixed_width')")
    delimiter: str | None = Field(default=None, description="주요 구분자(해당 시)")
    template: str = Field(description="템플릿 표현(예: 'ORD-{year}-{slot_1}-{seq}')")
    regex_pattern: str = Field(description="전체 문자열 매칭 + 가변 슬롯 캡처 정규식")
    coverage: float = Field(ge=0, le=1, description="비결측 표본 중 패턴에 일치한 비율")
    segments: list[StringSegment] = Field(default_factory=list)
    variable_slot_count: NonNegativeInt = 0


class ColumnClassification(ContractModel):
    """칼럼 하나의 결측치·정밀 유형·인덱스 특성·문자열 규칙 분해 통합 요약."""

    column: str
    physical_type: PhysicalType
    semantic_type: SemanticType
    category: ColumnRoleCategory
    missing_count: NonNegativeInt
    missing_rate: float = Field(ge=0, le=1)
    unique_count: NonNegativeInt
    cardinality_ratio: float = Field(ge=0, le=1)
    index_meta: IndexMetadata | None = None
    string_pattern: StringPatternDecomposition | None = None
    ordinal_order: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)


class TableSourceInfo(ContractModel):
    """다중 테이블 컨텍스트에 적재된 논리 테이블 하나의 메타데이터."""

    table_id: str = Field(
        pattern=r"^t\d+$",
        description="DuckDB 내부 스키마 식별자(t0, t1, ...). SQL 조립에는 이 이름만 쓴다.",
    )
    table_name: str = Field(
        pattern=r"^[a-z][a-z0-9_]*$",
        description="PostgreSQL 호환 표준 테이블명(유일화 완료)",
    )
    display_name: str = Field(description="원본 파일명·시트명을 보존한 표시명")
    source_path: str = Field(description="원본 파일의 기준 표시 경로")
    source_kind: TableSourceKind
    sheet_name: str | None = Field(default=None, description="XLSX 시트명(해당 시)")
    row_count: NonNegativeInt
    column_count: NonNegativeInt


class TableProfileBundle(ContractModel):
    """단일 논리 테이블의 S0~S4 결정론적 프로파일·타입·통계·정밀 분류 묶음."""

    contract_version: str = CONTRACTS_VERSION
    source: TableSourceInfo
    ingest: IngestManifest
    sampling: SamplingPlan
    profiles: list[ColumnProfile] = Field(default_factory=list)
    types: list[TypeAssignment] = Field(default_factory=list)
    stats: list[ColumnStats] = Field(default_factory=list)
    classifications: list[ColumnClassification] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class RelationshipCardinality(str, Enum):
    """테이블 간 참조 관계 기수성."""

    ONE_TO_ONE = "1:1"
    MANY_TO_ONE = "N:1"
    MANY_TO_MANY = "N:M"


class PrimaryKeyRecommendation(ContractModel):
    """테이블 하나의 최적 Primary Key 선정 결과와 근거."""

    table_name: str
    columns: list[str] = Field(default_factory=list)
    is_composite: bool = False
    recommend_surrogate: bool = False
    surrogate_column_name: str | None = None
    score: float = Field(ge=0, le=100)
    referenced_by_count: NonNegativeInt = 0
    alternative_keys: list[list[str]] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)


class CrossTableForeignKey(ContractModel):
    """다중 테이블 간 포함 종속 기반 외래 키(FK) 관계."""

    child_table: str
    child_columns: list[str] = Field(min_length=1)
    child_slot_name: str | None = Field(
        default=None,
        description="규칙성 문자열 칼럼의 가변 슬롯에서 추출된 숨은 FK일 때 슬롯 이름",
    )
    child_extract_sql: str | None = Field(
        default=None,
        description="문자열 슬롯 FK일 때 DuckDB/PostgreSQL 추출 표현식",
    )
    parent_table: str
    parent_columns: list[str] = Field(min_length=1)
    cardinality: RelationshipCardinality
    inclusion_rate: float = Field(ge=0, le=1, description="child 비결측 값 중 parent에 포함된 비율")
    parent_coverage: float = Field(ge=0, le=1, description="parent 고유 값 중 child가 참조한 비율")
    matched_rows: NonNegativeInt
    orphan_rows: NonNegativeInt = 0
    confidence: float = Field(ge=0, le=1)
    reasons: list[str] = Field(default_factory=list)


class MultiTableRelationAnalysis(ContractModel):
    """Milestone E3 산출물: 테이블 내부 관계 + 테이블 간 FK + 최적 PK 선정 통합 결과."""

    contract_version: str = CONTRACTS_VERSION
    table_relations: dict[str, RelationGraph] = Field(default_factory=dict)
    primary_keys: dict[str, PrimaryKeyRecommendation] = Field(default_factory=dict)
    foreign_keys: list[CrossTableForeignKey] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class IndexRecommendation(ContractModel):
    """DB 적재 후 조회·조인 성능을 위한 권고 인덱스."""

    table: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    columns: list[str] = Field(min_length=1)
    unique: bool = False
    reason: str


class TableLoadStrategy(ContractModel):
    """다중 테이블 적재 계획 내 개별 대상 테이블의 적재 전략."""

    source_table: str
    target_table: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    load_order_index: NonNegativeInt
    depends_on: list[str] = Field(default_factory=list)
    primary_key: list[str] = Field(default_factory=list)
    uses_surrogate_key: bool = False
    increment_mode: IncrementMode
    upsert_keys: list[str] = Field(default_factory=list)
    watermark_column: str | None = None
    deduplication: DeduplicationRule
    column_mappings: list[ColumnMapping] = Field(default_factory=list)
    recommended_indexes: list[IndexRecommendation] = Field(default_factory=list)
    expected_row_count: NonNegativeInt
    notes: list[str] = Field(default_factory=list)


class MultiTableLoadPlan(ContractModel):
    """Milestone E4 산출물: 다중 테이블 최적 스키마 후보·채점·위상 적재 플랜·DDL·ERD."""

    contract_version: str = CONTRACTS_VERSION
    purpose: Purpose
    selected_candidate_id: str
    candidates: list[SchemaCandidate] = Field(default_factory=list)
    scorecards: list[Scorecard] = Field(default_factory=list)
    load_order: list[LoadOrder] = Field(default_factory=list)
    table_plans: list[TableLoadStrategy] = Field(default_factory=list)
    ddl_sql: str = ""
    index_ddl_sql: str = ""
    erd_mermaid: str = ""
    cycle_warnings: list[str] = Field(default_factory=list)


class MultiTableAnalysisBundle(ContractModel):
    """다중 테이블 결정론적 분석 전체 번들(E1~E4 통합 최상위 계약)."""

    contract_version: str = CONTRACTS_VERSION
    purpose: Purpose
    tables: list[TableProfileBundle] = Field(default_factory=list)
    relations: MultiTableRelationAnalysis
    load_plan: MultiTableLoadPlan
    warnings: list[str] = Field(default_factory=list)
