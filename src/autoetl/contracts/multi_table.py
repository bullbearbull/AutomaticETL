"""다중 테이블 결정론적 분석·관계·적재 계획 계약(E1~E4)."""

from __future__ import annotations

from enum import Enum

from pydantic import Field, NonNegativeInt

from autoetl.contracts.column_types import TypeAssignment
from autoetl.contracts.common import CONTRACTS_VERSION, ContractModel
from autoetl.contracts.ingest import IngestManifest, SamplingPlan
from autoetl.contracts.profile import ColumnProfile
from autoetl.contracts.stats import ColumnStats


class TableSourceKind(str, Enum):
    """다중 테이블 입력 소스 유형."""

    CSV = "csv"
    TSV = "tsv"
    XLSX_SHEET = "xlsx_sheet"
    EXTRACTED_TABLE = "extracted_table"


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
    """단일 논리 테이블의 S0~S4 결정론적 프로파일·타입·통계 묶음."""

    contract_version: str = CONTRACTS_VERSION
    source: TableSourceInfo
    ingest: IngestManifest
    sampling: SamplingPlan
    profiles: list[ColumnProfile] = Field(default_factory=list)
    types: list[TypeAssignment] = Field(default_factory=list)
    stats: list[ColumnStats] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
