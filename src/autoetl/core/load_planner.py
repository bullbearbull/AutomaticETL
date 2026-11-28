"""Milestone E4: 다중 테이블 스키마 후보 생성·채점·시험 적재·위상 플랜·DDL/ERD/리포트 생성기.

전 과정이 100% 결정론적(0MB AI/LLM)이며, PostgreSQL DDL은 ``autoetl.core.ddl.postgres``의
``resolve_postgres_column_types`` + ``generate_ddl`` + ``verify_ddl``(sqlglot AST 재파싱 검증)을
통과하고, ``MultiTableContext``가 제공될 때 DuckDB 실제 시험 적재(변환·캐스팅·PK·FK 검증)를
거친 정직한 결과만 내보낸다.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from sqlglot import exp

from autoetl.adapters.detectors.builtin import EXCEL_SERIAL_MAX, EXCEL_SERIAL_MIN
from autoetl.contracts import (
    ColumnClassification,
    ColumnMapping,
    ColumnOverride,
    ColumnProfile,
    ColumnRoleCategory,
    ColumnStats,
    CriterionScore,
    CrossTableForeignKey,
    DeduplicationRule,
    IncrementMode,
    IndexRecommendation,
    LengthStats,
    LoadOrder,
    MaskingPolicy,
    MultiTableAnalysisBundle,
    MultiTableLoadPlan,
    MultiTableRelationAnalysis,
    NullHandling,
    NumericStats,
    PhysicalType,
    PrimaryKeyRecommendation,
    Purpose,
    RelationshipCardinality,
    SamplingMethod,
    SamplingPlan,
    SchemaCandidate,
    SchemaOverrideSpec,
    SchemaPattern,
    Scorecard,
    SemanticType,
    SensitivityAssignment,
    SensitivityLevel,
    SensitivityMap,
    StatsKind,
    StringSegment,
    StringSegmentKind,
    TableLoadStrategy,
    TableProfileBundle,
    TargetColumn,
    TargetTable,
    TypeAssignment,
    ValidationSummary,
)
from autoetl.core.context import RAW_TABLE, quote_identifier
from autoetl.core.ddl.postgres import (
    INT4_MAX,
    INT4_MIN,
    NUMERIC_LIMIT,
    build_column_comment_ddl,
    generate_ddl,
    resolve_postgres_column_types,
    verify_ddl,
)
from autoetl.core.stages.s02_profile import DEFAULT_MISSING_MARKERS
from autoetl.core.stages.s03_types import CURRENCY_DECIMAL_SHAPE
from autoetl.core.stages.s07_sensitivity import detect_table_sensitivity, masking_recommendation

if TYPE_CHECKING:
    from autoetl.core.multi_context import MultiTableContext

_CAMEL_RE = re.compile(r"([a-z0-9])([A-Z])")
_NON_IDENT_RE = re.compile(r"[^a-z0-9]+")
_VARCHAR_LEN_RE = re.compile(r"^VARCHAR\((\d+)\)$", re.IGNORECASE)
_NUMERIC_PS_RE = re.compile(r"^NUMERIC\((\d+),\s*(\d+)\)$", re.IGNORECASE)

_PURPOSE_WEIGHTS: dict[Purpose, dict[str, float]] = {
    Purpose.OLTP: {
        "integrity": 0.30,
        "redundancy": 0.20,
        "fitness": 0.20,
        "extensibility": 0.15,
        "load_simplicity": 0.10,
        "storage_efficiency": 0.05,
    },
    Purpose.ANALYTICS: {
        "fitness": 0.30,
        "extensibility": 0.20,
        "integrity": 0.15,
        "storage_efficiency": 0.15,
        "load_simplicity": 0.10,
        "redundancy": 0.10,
    },
    Purpose.ARCHIVE: {
        "load_simplicity": 0.35,
        "integrity": 0.25,
        "storage_efficiency": 0.20,
        "fitness": 0.10,
        "redundancy": 0.05,
        "extensibility": 0.05,
    },
}

_SLOT_ROLE_KEEP = {
    ColumnRoleCategory.TEMPORAL,
    ColumnRoleCategory.CODE_IDENTIFIER,
    ColumnRoleCategory.CATEGORICAL_NOMINAL,
    ColumnRoleCategory.CATEGORICAL_ORDINAL,
}

_LEVEL_RANK = {
    SensitivityLevel.L0: 0,
    SensitivityLevel.L1: 1,
    SensitivityLevel.L2: 2,
    SensitivityLevel.L3: 3,
}


def _to_sql_ident(raw: str, *, used: set[str], fallback_prefix: str = "col") -> str:
    """임의 칼럼/테이블명을 PostgreSQL 호환 `^[a-z][a-z0-9_]*$` 식별자로 결정론적 변환한다."""
    step1 = _CAMEL_RE.sub(r"\1_\2", raw.strip()).lower()
    cleaned = _NON_IDENT_RE.sub("_", step1).strip("_")
    if not cleaned:
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:6]
        cleaned = f"{fallback_prefix}_{digest}"
    elif not cleaned[0].isalpha():
        cleaned = f"{fallback_prefix}_{cleaned}"
    cleaned = cleaned[:52].rstrip("_")
    if not cleaned or not cleaned[0].isalpha():
        cleaned = f"{fallback_prefix}_0"

    candidate = cleaned
    suffix = 2
    while candidate in used:
        candidate = f"{cleaned[:48]}_{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def _decimal_precision_scale(
    stat: ColumnStats | None,
    assignment: TypeAssignment | None = None,
) -> tuple[int | None, int | None]:
    if assignment is not None and assignment.precision is not None and assignment.scale is not None:
        return (assignment.precision, assignment.scale)
    if stat is None or stat.numeric is None:
        return (None, None)
    num = stat.numeric
    max_abs = max(abs(num.minimum), abs(num.maximum))
    int_digits = len(str(int(max_abs))) if max_abs >= 1 else 1
    raw_scale = min(max(num.max_decimal_digits, 0), NUMERIC_LIMIT - 1)
    scale = min(raw_scale, max(0, NUMERIC_LIMIT - int_digits)) if int_digits <= NUMERIC_LIMIT else 0
    precision = min(max(int_digits + scale, scale + 1), NUMERIC_LIMIT)
    return (precision, scale)


def _select_acyclic_fks(
    relations: MultiTableRelationAnalysis,
) -> tuple[list[CrossTableForeignKey], list[str]]:
    """부모 단일 PK를 가리키는 FK 중 순환 없는 집합만 결정론적으로 채택한다."""
    warnings: list[str] = []
    valid: list[CrossTableForeignKey] = []
    for fk in relations.foreign_keys:
        parent_pk = relations.primary_keys.get(fk.parent_table)
        if parent_pk is None or parent_pk.recommend_surrogate:
            continue
        if len(fk.parent_columns) != 1 or parent_pk.columns != fk.parent_columns:
            continue
        if fk.child_table == fk.parent_table:
            continue
        valid.append(fk)

    # 신뢰도·포함률·커버리지 순으로 수락하며 순환 여부를 DFS로 판정한다.
    valid.sort(
        key=lambda f: (
            f.confidence,
            f.inclusion_rate,
            f.parent_coverage,
            f.child_slot_name is None,
            f.child_table,
            f.parent_table,
        ),
        reverse=True,
    )

    adj: dict[str, set[str]] = {}
    accepted: list[CrossTableForeignKey] = []

    def _has_path(src: str, dst: str) -> bool:
        stack = [src]
        visited: set[str] = set()
        while stack:
            cur = stack.pop()
            if cur == dst:
                return True
            if cur in visited:
                continue
            visited.add(cur)
            stack.extend(adj.get(cur, ()))
        return False

    for fk in valid:
        # child -> parent 간선 추가 시 parent -> ... -> child 경로가 이미 있으면 순환
        if _has_path(fk.parent_table, fk.child_table):
            warnings.append(
                f"순환 FK 방지를 위해 `{fk.child_table}.{fk.child_columns[0]}` → "
                f"`{fk.parent_table}.{fk.parent_columns[0]}` 제약은 제외했다."
            )
            continue
        adj.setdefault(fk.child_table, set()).add(fk.parent_table)
        accepted.append(fk)

    return accepted, warnings


def _has_indirect_path(
    direct_adj: Mapping[str, set[str]],
    src: str,
    dst: str,
) -> bool:
    """직접 FK 그래프에서 `src -> ... -> dst` 경로가 존재하는지 확인한다."""
    stack = list(direct_adj.get(src, ()))
    visited: set[str] = set()
    while stack:
        cur = stack.pop()
        if cur == dst:
            return True
        if cur in visited:
            continue
        visited.add(cur)
        stack.extend(direct_adj.get(cur, ()))
    return False


@dataclass(frozen=True)
class _BuiltCandidate:
    candidate: SchemaCandidate
    # (target_table, target_col) -> transform metadata
    transforms: dict[tuple[str, str], str]
    # (target_table, target_col) -> (source_column, regex_pattern, capture_group)
    slot_extract_specs: dict[tuple[str, str], tuple[str, str, int]]
    # target_table -> source_table
    source_table_map: dict[str, str]
    # target_table -> surrogate key used?
    surrogate_tables: set[str]
    # dropped derived columns per table (for reporting)
    omitted_derivations: dict[str, list[str]]
    # analytics partition deferred notes per table
    partition_notes: dict[str, list[str]]
    # decomposed string slot columns count
    materialized_slot_count: int
    # target_table -> {source_column: MaskingPolicy}
    column_masking: dict[str, dict[str, MaskingPolicy]]
    skipped_key_masking: dict[str, dict[str, MaskingPolicy]]
    # target_table -> [target_column, ...] explicitly confirmed UNIQUE by user
    confirmed_unique_cols: dict[str, list[str]]


def _materializable_slots(
    cls: ColumnClassification,
    row_count: int,
    slot_fk_names: set[str],
) -> list[StringSegment]:
    """규칙 문자열에서 대상 테이블 컬럼으로 구체화할 가치가 있는 가변 슬롯을 고른다."""
    if cls.string_pattern is None:
        return []
    variable_slots = [
        seg
        for seg in cls.string_pattern.segments
        if seg.kind is StringSegmentKind.SLOT and seg.slot_name is not None
    ]
    out: list[StringSegment] = []
    for seg in variable_slots:
        assert seg.slot_name is not None
        if seg.slot_name in slot_fk_names:
            out.append(seg)
            continue
        # 단일 가변 슬롯이 사실상 원본 식별자와 1:1 대응(예: 'C000161'의 '000161')이면
        # 중복 컬럼을 늘릴 실익이 없으므로 구체화하지 않는다.
        if (
            len(variable_slots) == 1
            and row_count > 0
            and seg.unique_count >= max(1, int(row_count * 0.95))
        ):
            continue
        if (
            seg.slot_category in _SLOT_ROLE_KEEP
            and seg.unique_count >= 2
            and (seg.slot_category is ColumnRoleCategory.TEMPORAL or seg.unique_count < row_count)
        ):
            out.append(seg)
    return out


def _build_candidate(
    candidate_id: str,
    pattern: SchemaPattern,
    rationale: str,
    bundles: Sequence[TableProfileBundle],
    relations: MultiTableRelationAnalysis,
    acyclic_fks: Sequence[CrossTableForeignKey],
    *,
    decompose_strings: bool,
    omit_derivations: bool,
    analytics_mode: bool,
    overrides: SchemaOverrideSpec | None = None,
    engine: str | None = None,
) -> _BuiltCandidate:
    """단일 후보 SchemaCandidate와 변환/원본 매핑 메타데이터를 결정론적으로 조립한다."""
    pk_map = relations.primary_keys
    override_by_col: dict[tuple[str, str], ColumnOverride] = (
        {(ov.table, ov.source_column): ov for ov in overrides.columns}
        if overrides is not None
        else {}
    )

    # 테이블별 원본 컬럼명 -> SQL 식별자 맵을 먼저 확정(FK가 부모 SQL 컬럼명을 참조해야 함)
    col_sql_map: dict[str, dict[str, str]] = {}
    used_cols_by_table: dict[str, set[str]] = {}
    for bundle in bundles:
        t_name = bundle.source.table_name
        used: set[str] = set()
        mapping: dict[str, str] = {}
        pk_rec = pk_map.get(t_name)
        if pk_rec is not None and pk_rec.recommend_surrogate and pk_rec.surrogate_column_name:
            _to_sql_ident(pk_rec.surrogate_column_name, used=used, fallback_prefix="id")
        # 사용자가 명시적으로 지정한 영문 컬럼명을 우선 배정해 불필요한 _2 접미 충돌을 방지한다.
        for col_info in bundle.ingest.columns:
            ov = override_by_col.get((t_name, col_info.name))
            if ov is not None and ov.target_column:
                mapping[col_info.name] = _to_sql_ident(
                    ov.target_column, used=used, fallback_prefix=f"c{col_info.index}"
                )
        for col_info in bundle.ingest.columns:
            if col_info.name in mapping:
                continue
            mapping[col_info.name] = _to_sql_ident(
                col_info.name, used=used, fallback_prefix=f"c{col_info.index}"
            )
        col_sql_map[t_name] = mapping
        used_cols_by_table[t_name] = used

    # 부모 PK 물리 타입 조회용 맵
    parent_pk_phys: dict[tuple[str, str], PhysicalType] = {}
    for bundle in bundles:
        t_name = bundle.source.table_name
        type_by_col = {t.column: t.physical_type for t in bundle.types}
        for col_name, phys in type_by_col.items():
            parent_pk_phys[(t_name, col_name)] = phys

    # 직접 FK 그래프와 슬롯 FK 필터링
    direct_fk_by_col: dict[tuple[str, str], CrossTableForeignKey] = {}
    direct_adj: dict[str, set[str]] = {}
    for fk in acyclic_fks:
        if fk.child_slot_name is None:
            direct_fk_by_col[(fk.child_table, fk.child_columns[0])] = fk
            direct_adj.setdefault(fk.child_table, set()).add(fk.parent_table)

    slot_fk_by_key: dict[tuple[str, str, str], CrossTableForeignKey] = {}
    for fk in acyclic_fks:
        if fk.child_slot_name is None:
            continue
        # OLTP 모드에서 직접 FK 경로(예: transactions -> accounts -> branches)로 이미
        # 도달 가능한 조상 테이블로의 이행적(transitive) 슬롯 FK는 3NF 정규화를 위해 생략한다.
        if not analytics_mode and _has_indirect_path(direct_adj, fk.child_table, fk.parent_table):
            continue
        slot_fk_by_key[(fk.child_table, fk.child_columns[0], fk.child_slot_name)] = fk

    referenced_parents: set[str] = {fk.parent_table for fk in direct_fk_by_col.values()}
    if decompose_strings:
        referenced_parents.update(fk.parent_table for fk in slot_fk_by_key.values())

    tables: list[TargetTable] = []
    transforms: dict[tuple[str, str], str] = {}
    slot_extract_specs: dict[tuple[str, str], tuple[str, str, int]] = {}
    source_table_map: dict[str, str] = {}
    surrogate_tables: set[str] = set()
    omitted_derivations: dict[str, list[str]] = {}
    partition_notes: dict[str, list[str]] = {}
    column_masking: dict[str, dict[str, MaskingPolicy]] = {}
    skipped_key_masking: dict[str, dict[str, MaskingPolicy]] = {}
    confirmed_unique_cols: dict[str, list[str]] = {}
    materialized_slot_count = 0

    for bundle in bundles:
        t_name = bundle.source.table_name
        source_table_map[t_name] = t_name
        table_pk: PrimaryKeyRecommendation | None = pk_map.get(t_name)
        pk_set = set(table_pk.columns) if table_pk is not None else set()
        cls_by_col = {c.column: c for c in bundle.classifications}
        prof_by_col = {p.column: p for p in bundle.profiles}
        stat_by_col = {s.column: s for s in bundle.stats}
        assign_by_col = {t.column: t for t in bundle.types}
        rel_graph = relations.table_relations.get(t_name)

        # 정규화 후보(C2)에서는 PK/FK에 속하지 않는 정확한 산술 파생 컬럼을 생략(뷰/계산식 위임)
        derived_drop: set[str] = set()
        if omit_derivations and rel_graph is not None:
            for d in rel_graph.derivations:
                if d.target not in pk_set and (t_name, d.target) not in direct_fk_by_col:
                    derived_drop.add(d.target)
                    omitted_derivations.setdefault(t_name, []).append(f"{d.target} ({d.rule_hint})")

        target_cols: list[TargetColumn] = []
        used_cols = set(used_cols_by_table[t_name])

        # 1. 자연 PK가 없으면 BIGINT GENERATED ALWAYS AS IDENTITY 서로게이트 PK를 첫 컬럼으로 추가
        if table_pk is not None and table_pk.recommend_surrogate:
            raw_surr = table_pk.surrogate_column_name or f"{t_name}_id"
            surr_used: set[str] = set()
            surr_sql = _to_sql_ident(raw_surr, used=surr_used, fallback_prefix="surr")
            if surr_sql in col_sql_map[t_name].values():
                surr_sql = _to_sql_ident(f"{t_name}_surrogate_id", used=used_cols)
            surrogate_tables.add(t_name)
            target_cols.append(
                TargetColumn(
                    name=surr_sql,
                    physical_type=PhysicalType.INTEGER,
                    source_columns=["__surrogate__"],
                    nullable=False,
                    is_primary_key=True,
                    is_identity=True,
                    description="자연 PK 부재로 자동 생성되는 서로게이트 IDENTITY PK",
                )
            )
            transforms[(t_name, surr_sql)] = "generated_always_as_identity"

        # 2. 원본 컬럼 매핑
        index_cols: list[str] = []
        temporal_partition_col: str | None = None

        for src_col in bundle.ingest.columns:
            col_name = src_col.name
            if col_name in derived_drop:
                continue

            sql_col = col_sql_map[t_name][col_name]
            cls = cls_by_col[col_name]
            prof = prof_by_col[col_name]
            stat = stat_by_col.get(col_name)
            t_assign = assign_by_col[col_name]
            ov = override_by_col.get((t_name, col_name))

            is_pk = col_name in pk_set and not (
                table_pk is not None and table_pk.recommend_surrogate
            )
            direct_fk = direct_fk_by_col.get((t_name, col_name))
            phys = cls.physical_type

            fk_spec: str | None = None
            transform = _default_transform(cls, t_assign)

            if direct_fk is not None:
                p_table = direct_fk.parent_table
                p_col_src = direct_fk.parent_columns[0]
                p_col_sql = col_sql_map[p_table][p_col_src]
                p_phys = parent_pk_phys[(p_table, p_col_src)]
                if phys is not p_phys:
                    phys = p_phys
                    transform = f"cast_to_{p_phys.value}"
                fk_spec = f"{p_table}({p_col_sql})"

            if ov is not None and ov.masking is not MaskingPolicy.NONE:
                if not is_pk and direct_fk is None:
                    # 마스킹(예: 1988-**-**)·해시(h_...) 값은 문자열 타입으로 안전하게 저장
                    phys = PhysicalType.STRING
                    transform = f"pii_{ov.masking.value}"
                # SQLite에 적재하는 날짜·시각은 기존 변환에서 문자열로 가려진다.
                if phys is PhysicalType.STRING or (
                    engine == "sqlite"
                    and phys
                    in {
                        PhysicalType.DATE,
                        PhysicalType.TIME,
                        PhysicalType.TIMESTAMP,
                        PhysicalType.TIMESTAMPTZ,
                    }
                ):
                    column_masking.setdefault(t_name, {})[col_name] = ov.masking
                else:
                    skipped_key_masking.setdefault(t_name, {})[col_name] = ov.masking

            if ov is not None and ov.unique is True:
                uq_list = confirmed_unique_cols.setdefault(t_name, [])
                if sql_col not in uq_list:
                    uq_list.append(sql_col)

            prec: int | None = None
            scale: int | None = None
            if phys is PhysicalType.DECIMAL:
                prec, scale = _decimal_precision_scale(stat, t_assign)

            nullable = False if is_pk else (prof.missing_count > 0)
            desc_parts = [f"유형: {cls.category.value}"]
            if sql_col != col_name:
                desc_parts.append(f"원본: {col_name}")
            if cls.string_pattern is not None:
                desc_parts.append(f"패턴: {cls.string_pattern.template}")
            applied_masking = column_masking.get(t_name, {}).get(col_name)
            if applied_masking is not None:
                desc_parts.append(f"개인정보보호: {applied_masking.value}")

            target_cols.append(
                TargetColumn(
                    name=sql_col,
                    physical_type=phys,
                    precision=prec,
                    scale=scale,
                    source_columns=[col_name],
                    nullable=nullable,
                    is_primary_key=is_pk,
                    foreign_key=fk_spec,
                    description=" · ".join(desc_parts),
                )
            )
            transforms[(t_name, sql_col)] = transform

            if fk_spec is not None and sql_col not in index_cols:
                index_cols.append(sql_col)
            if (
                cls.category is ColumnRoleCategory.TEMPORAL
                and not nullable
                and temporal_partition_col is None
                and (ov is None or ov.masking is MaskingPolicy.NONE)
            ):
                temporal_partition_col = sql_col
            if (
                analytics_mode
                and cls.category
                in {
                    ColumnRoleCategory.TEMPORAL,
                    ColumnRoleCategory.CATEGORICAL_NOMINAL,
                    ColumnRoleCategory.CATEGORICAL_ORDINAL,
                }
                and sql_col not in index_cols
            ):
                index_cols.append(sql_col)

            # 3. 규칙 문자열의 가변 슬롯 구체화 (숨은 FK 슬롯 + 의미 있는 시간/코드/범주 슬롯)
            if decompose_strings and cls.string_pattern is not None:
                slot_fk_names = {
                    s_name for (ct, cc, s_name) in slot_fk_by_key if ct == t_name and cc == col_name
                }
                for seg in _materializable_slots(cls, bundle.source.row_count, slot_fk_names):
                    assert seg.slot_name is not None
                    slot_fk = slot_fk_by_key.get((t_name, col_name, seg.slot_name))
                    if slot_fk is not None:
                        p_table = slot_fk.parent_table
                        p_col_src = slot_fk.parent_columns[0]
                        p_col_sql = col_sql_map[p_table][p_col_src]
                        p_phys = parent_pk_phys[(p_table, p_col_src)]
                        preferred_name = (
                            p_col_sql if p_col_sql not in used_cols else f"{sql_col}_{p_col_sql}"
                        )
                        slot_sql_col = _to_sql_ident(preferred_name, used=used_cols)
                        slot_fk_spec = f"{p_table}({p_col_sql})"
                        slot_phys = p_phys
                        slot_desc = (
                            f"`{col_name}` 슬롯 `{seg.slot_name}`에서 추출된 숨은 FK → "
                            f"{p_table}({p_col_sql})"
                        )
                    else:
                        slot_sql_col = _to_sql_ident(f"{sql_col}_{seg.slot_name}", used=used_cols)
                        slot_fk_spec = None
                        slot_phys = seg.slot_physical_type or PhysicalType.STRING
                        slot_desc = (
                            f"`{col_name}` 규칙 문자열의 가변 슬롯 `{seg.slot_name}` 파싱 컬럼"
                        )

                    cap_group = seg.capture_group or 1
                    extract_expr = seg.extract_sql_hint or (
                        f"NULLIF(regexp_extract({col_name}, '{cls.string_pattern.regex_pattern}', "
                        f"{cap_group}), '')"
                    )
                    target_cols.append(
                        TargetColumn(
                            name=slot_sql_col,
                            physical_type=slot_phys,
                            source_columns=[col_name],
                            nullable=prof.missing_count > 0 or cls.string_pattern.coverage < 1.0,
                            is_primary_key=False,
                            foreign_key=slot_fk_spec,
                            description=slot_desc,
                        )
                    )
                    transforms[(t_name, slot_sql_col)] = extract_expr
                    slot_extract_specs[(t_name, slot_sql_col)] = (
                        col_name,
                        cls.string_pattern.regex_pattern,
                        cap_group,
                    )
                    materialized_slot_count += 1
                    if slot_fk_spec is not None and slot_sql_col not in index_cols:
                        index_cols.append(slot_sql_col)

        # 4. PostgreSQL 선언적 파티셔닝(C3_ANALYTICS):
        #    - 실제 DDL(`PARTITION BY RANGE(...)`)이 실행 가능할 때만 `table.partition`을 설정한다.
        #    - PostgreSQL은 파티션 키가 PRIMARY KEY에 포함되어야 하며, 자식 테이블이 단일 컬럼 PK로
        #      참조 중인 부모 테이블은 복합 PK로 바꾸면 자식 FK가 깨지므로 안전하게 보류한다.
        partition_expr: str | None = None
        has_outgoing_fk = any(c.foreign_key is not None for c in target_cols)
        if analytics_mode and temporal_partition_col is not None and has_outgoing_fk:
            pk_names = [c.name for c in target_cols if c.is_primary_key]
            can_partition = (
                t_name not in referenced_parents
                or temporal_partition_col in pk_names
                or not pk_names
            )
            if can_partition:
                target_cols = [
                    c.model_copy(update={"is_primary_key": True, "nullable": False})
                    if c.name == temporal_partition_col
                    else c
                    for c in target_cols
                ]
                partition_expr = f"RANGE ({temporal_partition_col})"
            else:
                partition_notes.setdefault(t_name, []).append(
                    f"하위 테이블이 단일 PK를 참조 중이라 `{temporal_partition_col}` 자동 "
                    "PARTITION BY RANGE 생성을 보류했다(파티셔닝 적용 시 자식 테이블에도 "
                    "파티션 키를 포함한 복합 FK 설계 필요)."
                )

        tables.append(
            TargetTable(
                name=t_name,
                columns=target_cols,
                partition=partition_expr,
                index=index_cols,
            )
        )

    if not tables:
        raise ValueError("스키마 후보를 생성할 테이블이 없습니다.")

    candidate = SchemaCandidate(
        id=candidate_id,
        pattern=pattern,
        tables=tables,
        rationale=rationale,
        source="code",
    )
    return _BuiltCandidate(
        candidate=candidate,
        transforms=transforms,
        slot_extract_specs=slot_extract_specs,
        source_table_map=source_table_map,
        surrogate_tables=surrogate_tables,
        omitted_derivations=omitted_derivations,
        partition_notes=partition_notes,
        materialized_slot_count=materialized_slot_count,
        column_masking=column_masking,
        skipped_key_masking=skipped_key_masking,
        confirmed_unique_cols=confirmed_unique_cols,
    )


def _default_transform(cls: ColumnClassification, t_assign: TypeAssignment) -> str:
    if (
        t_assign.semantic_type in {SemanticType.AMOUNT, SemanticType.RATIO}
        or cls.physical_type is PhysicalType.DECIMAL
    ):
        return "strip_currency_and_comma"
    if cls.category is ColumnRoleCategory.TEMPORAL:
        if any(("한국어 날짜" in r or "한글 날짜" in r) for r in cls.reasons):
            return "parse_korean_date"
        return "parse_iso_temporal"
    if (
        t_assign.semantic_type is SemanticType.BOOLEAN_LIKE
        or cls.physical_type is PhysicalType.BOOLEAN
        or cls.category is ColumnRoleCategory.BOOLEAN
    ):
        return "normalize_boolean"
    return "identity"


def _merge_profiles_and_stats(
    bundles: Sequence[TableProfileBundle],
) -> tuple[list[ColumnProfile], list[ColumnStats], SamplingPlan]:
    """`generate_ddl`용으로 모든 테이블의 관측 길이·수치 범위를 안전하게 병합한다."""
    merged_profiles: dict[str, ColumnProfile] = {}
    merged_stats: dict[str, ColumnStats] = {}
    all_full = True
    total_rows = 0

    for bundle in bundles:
        total_rows += bundle.source.row_count
        if bundle.sampling.strategy != "full":
            all_full = False
        for prof in bundle.profiles:
            prev = merged_profiles.get(prof.column)
            if prev is None:
                merged_profiles[prof.column] = prof
            else:
                merged_profiles[prof.column] = prev.model_copy(
                    update={
                        "length": LengthStats(
                            minimum=min(prev.length.minimum, prof.length.minimum),
                            maximum=max(prev.length.maximum, prof.length.maximum),
                            mean=max(prev.length.mean, prof.length.mean),
                            p50=max(prev.length.p50, prof.length.p50),
                            p95=max(prev.length.p95, prof.length.p95),
                        )
                    }
                )
        for st in bundle.stats:
            prev_st = merged_stats.get(st.column)
            if prev_st is None or st.numeric is None:
                if prev_st is None:
                    merged_stats[st.column] = st
            elif prev_st.numeric is not None:
                merged_stats[st.column] = prev_st.model_copy(
                    update={
                        "kind": StatsKind.NUMERIC,
                        "numeric": NumericStats(
                            mean=prev_st.numeric.mean,
                            std=prev_st.numeric.std,
                            minimum=min(prev_st.numeric.minimum, st.numeric.minimum),
                            maximum=max(prev_st.numeric.maximum, st.numeric.maximum),
                            p25=prev_st.numeric.p25,
                            p50=prev_st.numeric.p50,
                            p75=prev_st.numeric.p75,
                            skew=prev_st.numeric.skew,
                            max_decimal_digits=max(
                                prev_st.numeric.max_decimal_digits,
                                st.numeric.max_decimal_digits,
                            ),
                            outliers=prev_st.numeric.outliers,
                        ),
                    }
                )
            else:
                merged_stats[st.column] = st

    sampling = SamplingPlan(
        strategy="full" if all_full else "sample",
        method=None if all_full else SamplingMethod.RANDOM,
        total_rows=total_rows,
        sample_rows=None if all_full else max(1, total_rows),
        rationale="다중 테이블 병합 스캔 요약",
    )
    return list(merged_profiles.values()), list(merged_stats.values()), sampling


def _sql_literal(val: str) -> str:
    return "'" + val.replace("'", "''") + "'"


def _raw_missing_predicate(qcol: str) -> str:
    clauses = [f"{qcol} IS NULL", f"regexp_matches({qcol}, '^\\s*$')"]
    clauses += [f"{qcol} = {_sql_literal(m)}" for m in sorted(DEFAULT_MISSING_MARKERS)]
    return " OR ".join(f"({c})" for c in clauses)


def _trial_cast_expr(
    qcol: str,
    col: TargetColumn,
    transform: str,
    pg_type: exp.DataType,
    extracted: str | None,
) -> tuple[str, int | None, bool]:
    """시험 적재용 DuckDB 변환 SQL, VARCHAR 최대 길이, INT4 범위 검사 여부를 반환한다."""
    pg_sql = pg_type.sql(dialect="postgres")
    varchar_limit: int | None = None
    m_vc = _VARCHAR_LEN_RE.fullmatch(pg_sql)
    if m_vc is not None:
        varchar_limit = int(m_vc.group(1))
    check_int4 = pg_sql.upper() in {"INT", "INTEGER"}

    if col.is_identity or transform == "generated_always_as_identity":
        return "row_number() OVER ()", None, False

    if extracted is not None:
        if col.physical_type is PhysicalType.INTEGER:
            inner = (
                f"CASE WHEN regexp_matches({extracted}, '^[+-]?\\d+$') "
                f"THEN TRY_CAST({extracted} AS BIGINT) ELSE NULL END"
            )
        elif col.physical_type is PhysicalType.DECIMAL:
            inner = f"TRY_CAST({extracted} AS DOUBLE)"
        else:
            inner = extracted
        return inner, varchar_limit, check_int4

    if transform == "strip_currency_and_comma" or col.physical_type is PhysicalType.DECIMAL:
        cleaned = f"regexp_replace({qcol}, '[₩$원,\\s]', '', 'g')"
        shape_lit = _sql_literal(CURRENCY_DECIMAL_SHAPE)
        m_num = _NUMERIC_PS_RE.fullmatch(pg_sql)
        if col.physical_type is PhysicalType.INTEGER:
            cast_target = "BIGINT"
        elif m_num is not None:
            cast_target = f"DECIMAL({int(m_num.group(1))}, {int(m_num.group(2))})"
        else:
            cast_target = "DOUBLE"
        inner = (
            f"CASE WHEN regexp_matches({qcol}, {shape_lit}) "
            f"THEN TRY_CAST({cleaned} AS {cast_target}) ELSE NULL END"
        )
        return inner, None, check_int4

    if transform == "normalize_boolean" or col.physical_type is PhysicalType.BOOLEAN:
        inner = (
            f"CASE WHEN lower(trim({qcol})) IN ('y', 'yes', '예', 'o', 'true') THEN TRUE "
            f"WHEN lower(trim({qcol})) IN ('n', 'no', '아니오', 'x', 'false') THEN FALSE "
            "ELSE NULL END"
        )
        return inner, None, False

    if transform == "parse_korean_date":
        inner = (
            "COALESCE("
            f"CAST(TRY_STRPTIME(trim({qcol}), '%Y년 %m월 %d일') AS DATE), "
            f"CAST(TRY_STRPTIME(trim({qcol}), '%Y년%m월%d일') AS DATE), "
            f"CAST(TRY_STRPTIME(trim({qcol}), '%Y-%m-%d') AS DATE), "
            f"CAST(TRY_STRPTIME(trim({qcol}), '%Y/%m/%d') AS DATE), "
            f"CAST(TRY_STRPTIME(trim({qcol}), '%Y.%m.%d') AS DATE)"
            ")"
        )
        return inner, None, False

    if transform == "parse_iso_temporal" or col.physical_type in {
        PhysicalType.DATE,
        PhysicalType.TIMESTAMP,
        PhysicalType.TIMESTAMPTZ,
        PhysicalType.TIME,
    }:
        if col.physical_type is PhysicalType.TIME:
            return f"TRY_CAST(trim({qcol}) AS TIME)", None, False
        if col.physical_type is PhysicalType.TIMESTAMP:
            return f"TRY_CAST(trim({qcol}) AS TIMESTAMP)", None, False
        if col.physical_type is PhysicalType.TIMESTAMPTZ:
            return f"TRY_CAST(trim({qcol}) AS TIMESTAMPTZ)", None, False
        inner = (
            "COALESCE("
            f"CAST(TRY_STRPTIME(trim({qcol}), '%Y-%m-%d') AS DATE), "
            f"CAST(TRY_STRPTIME(trim({qcol}), '%Y/%m/%d') AS DATE), "
            f"CAST(TRY_STRPTIME(trim({qcol}), '%Y.%m.%d') AS DATE), "
            f"CASE WHEN regexp_full_match(trim({qcol}), '[0-9]{{8}}') "
            f"THEN CAST(TRY_STRPTIME(trim({qcol}), '%Y%m%d') AS DATE) END, "
            f"CASE WHEN regexp_matches(trim({qcol}), '^\\d+$') "
            f"AND TRY_CAST(trim({qcol}) AS INTEGER) "
            f"BETWEEN {EXCEL_SERIAL_MIN} AND {EXCEL_SERIAL_MAX} "
            f"THEN DATE '1899-12-30' + TRY_CAST(trim({qcol}) AS INTEGER) ELSE NULL END"
            ")"
        )
        return inner, None, False

    if col.physical_type is PhysicalType.INTEGER:
        inner = (
            f"CASE WHEN regexp_matches(trim({qcol}), '^[+-]?\\d+$') "
            f"THEN TRY_CAST(trim({qcol}) AS BIGINT) ELSE NULL END"
        )
        return inner, None, check_int4

    if col.physical_type is PhysicalType.FLOAT:
        return f"TRY_CAST(trim({qcol}) AS DOUBLE)", None, False

    return f"CAST({qcol} AS VARCHAR)", varchar_limit, False


def _run_trial_load(
    mctx: MultiTableContext | None,
    built: _BuiltCandidate,
    bundles: Sequence[TableProfileBundle],
    relations: MultiTableRelationAnalysis,
    col_types: Mapping[tuple[str, str], exp.DataType],
    *,
    ddl_ok: bool,
) -> ValidationSummary:
    """실제 DuckDB 시험 적재(변환·캐스팅·PK·FK 무결성)를 실행해 정직한 검증 요약을 산출한다."""
    total_rows = sum(b.source.row_count for b in bundles)

    if not ddl_ok:
        return ValidationSummary(
            ddl_parse_ok=False,
            trial_load_ok=False,
            trial_load_executed=False,
            constraint_violation_count=1,
            cast_failure_count=0,
            pk_violation_count=0,
            fk_violation_count=0,
            data_loss_rate=0.0,
        )

    # mctx가 없으면 시험 적재를 수행하지 않았음을 정직하게 보고하되,
    # 관계 분석에서 관측된 물리 FK 고아 행 수는 그대로 반영한다.
    if mctx is None or mctx.connection is None:
        orphan_by_pair: dict[tuple[str, str], int] = {}
        for fk in relations.foreign_keys:
            key = (fk.child_table, fk.parent_table)
            orphan_by_pair[key] = max(orphan_by_pair.get(key, 0), fk.orphan_rows)
        fk_orphans = 0
        for tbl in built.candidate.tables:
            seen_parents: set[str] = set()
            for col in tbl.columns:
                if col.foreign_key is None:
                    continue
                parent = col.foreign_key.split("(", 1)[0]
                if parent not in seen_parents:
                    seen_parents.add(parent)
                    fk_orphans += orphan_by_pair.get((tbl.name, parent), 0)
        return ValidationSummary(
            ddl_parse_ok=True,
            trial_load_ok=False,
            trial_load_executed=False,
            constraint_violation_count=fk_orphans,
            cast_failure_count=0,
            pk_violation_count=0,
            fk_violation_count=fk_orphans,
            data_loss_rate=min(1.0, round(fk_orphans / max(total_rows, 1), 6)),
        )

    conn = mctx.connection
    cand = built.candidate
    load_order = _topological_order(cand)
    table_by_name = {t.name: t for t in cand.tables}
    bundle_by_name = {b.source.table_name: b for b in bundles}
    trial_name_by_table: dict[str, str] = {}
    col_alias_by_table: dict[str, dict[str, str]] = {}
    created_temps: list[str] = []

    cast_failure_count = 0
    failed_cast_rows = 0
    pk_violation_count = 0
    fk_violation_count = 0

    try:
        for idx, lo in enumerate(load_order):
            t_name = lo.table
            target_table = table_by_name[t_name]
            src_name = built.source_table_map[t_name]
            slot = mctx.get_by_name(src_name)
            col_id_by_src = {
                c.name: slot.column_id_of(c.index) for c in bundle_by_name[src_name].ingest.columns
            }
            src_rel = f"{slot.table_info.table_id}.{RAW_TABLE}"
            temp_tbl = f"_autoetl_trial_{idx}"
            trial_name_by_table[t_name] = temp_tbl
            col_aliases: dict[str, str] = {}
            select_exprs: list[str] = []
            fail_predicates: list[str] = []

            for c_idx, col in enumerate(target_table.columns):
                alias = f"v{c_idx}"
                col_aliases[col.name] = alias
                transform = built.transforms.get((t_name, col.name), "identity")
                pg_type = col_types[(t_name, col.name)]
                slot_spec = built.slot_extract_specs.get((t_name, col.name))

                if col.is_identity or transform == "generated_always_as_identity":
                    select_exprs.append(f"row_number() OVER () AS {alias}")
                    continue

                src_col_name = col.source_columns[0]
                raw_cid = col_id_by_src.get(src_col_name)
                if raw_cid is None:
                    select_exprs.append(f"NULL AS {alias}")
                    continue

                qcol = quote_identifier(raw_cid)
                miss_pred = _raw_missing_predicate(qcol)
                extracted = None
                if slot_spec is not None:
                    _src_name, pattern, cap_group = slot_spec
                    pat_lit = _sql_literal(pattern)
                    extracted = f"NULLIF(regexp_extract({qcol}, {pat_lit}, {cap_group}), '')"
                inner_expr, varchar_limit, check_int4 = _trial_cast_expr(
                    qcol, col, transform, pg_type, extracted
                )
                wrapped = f"CASE WHEN ({miss_pred}) THEN NULL ELSE ({inner_expr}) END"
                select_exprs.append(f"{wrapped} AS {alias}")

                # 비결측 원시 값이 변환 후 NULL이 되거나 DDL 길이/정수 범위를 초과하면 실패
                cast_input = f"NOT ({miss_pred})"
                if extracted is not None and col.nullable:
                    cast_input += f" AND ({extracted}) IS NOT NULL"
                fail_parts = [f"({cast_input} AND ({wrapped}) IS NULL)"]
                if varchar_limit is not None:
                    fail_parts.append(
                        f"(({wrapped}) IS NOT NULL AND "
                        f"length(CAST({wrapped} AS VARCHAR)) > {varchar_limit})"
                    )
                if check_int4:
                    fail_parts.append(
                        f"(({wrapped}) IS NOT NULL AND "
                        f"(({wrapped}) < {INT4_MIN} OR ({wrapped}) > {INT4_MAX}))"
                    )
                fail_predicates.append("(" + " OR ".join(fail_parts) + ")")

            col_alias_by_table[t_name] = col_aliases
            conn.execute(
                f"CREATE OR REPLACE TEMP TABLE {temp_tbl} AS "
                f"SELECT {', '.join(select_exprs)} FROM {src_rel}"
            )
            created_temps.append(temp_tbl)

            if fail_predicates:
                sum_cells = " + ".join(
                    f"COALESCE(SUM(CASE WHEN {pred} THEN 1 ELSE 0 END), 0)"
                    for pred in fail_predicates
                )
                any_fail = " OR ".join(fail_predicates)
                row_res = conn.execute(
                    f"SELECT ({sum_cells}), "
                    f"COALESCE(SUM(CASE WHEN {any_fail} THEN 1 ELSE 0 END), 0) "
                    f"FROM {src_rel}"
                ).fetchone()
                if row_res is not None:
                    cast_failure_count += int(row_res[0] or 0)
                    failed_cast_rows += int(row_res[1] or 0)

            # PK 무결성 검사 (NULL 및 중복 행)
            # 서로게이트 IDENTITY 컬럼이 포함된 복합 PK(예: Analytics 파티션 키)는
            # 비-IDENTITY PK 컬럼의 NULL 여부만 검사하고 중복 검사는 생략한다.
            all_pk_cols = [c.name for c in target_table.columns if c.is_primary_key]
            pk_cols = [
                c.name for c in target_table.columns if c.is_primary_key and not c.is_identity
            ]
            has_identity_pk = len(all_pk_cols) != len(pk_cols)
            if pk_cols:
                pk_aliases = [col_aliases[c] for c in pk_cols]
                null_cond = " OR ".join(f"{a} IS NULL" for a in pk_aliases)
                pk_null_row = conn.execute(
                    f"SELECT COUNT(*) FROM temp.main.{temp_tbl} WHERE {null_cond}"
                ).fetchone()
                pk_nulls = int(pk_null_row[0]) if pk_null_row is not None else 0
                pk_dups = 0
                if not has_identity_pk:
                    not_null_cond = " AND ".join(f"{a} IS NOT NULL" for a in pk_aliases)
                    group_expr = ", ".join(pk_aliases)
                    pk_dup_row = conn.execute(
                        f"SELECT COALESCE(SUM(cnt - 1), 0) FROM ("
                        f"SELECT COUNT(*) AS cnt FROM temp.main.{temp_tbl} "
                        f"WHERE {not_null_cond} GROUP BY {group_expr} HAVING COUNT(*) > 1)"
                    ).fetchone()
                    pk_dups = int(pk_dup_row[0]) if pk_dup_row is not None else 0
                pk_violation_count += pk_nulls + pk_dups

            # 사용자 확정 UNIQUE 컬럼 중복 검사 (NULL 제외)
            for uq_col in built.confirmed_unique_cols.get(t_name, ()):
                if all_pk_cols == [uq_col]:
                    continue
                uq_alias = col_aliases.get(uq_col)
                if uq_alias is None:
                    continue
                uq_dup_row = conn.execute(
                    f"SELECT COALESCE(SUM(cnt - 1), 0) FROM ("
                    f"SELECT COUNT(*) AS cnt FROM temp.main.{temp_tbl} "
                    f"WHERE {uq_alias} IS NOT NULL GROUP BY {uq_alias} HAVING COUNT(*) > 1)"
                ).fetchone()
                pk_violation_count += int(uq_dup_row[0]) if uq_dup_row is not None else 0

        # FK 참조 무결성 검사 (실제 변환된 임시 테이블 간 anti-join)
        for target_table in cand.tables:
            child_temp = trial_name_by_table[target_table.name]
            child_aliases = col_alias_by_table[target_table.name]
            for col in target_table.columns:
                if col.foreign_key is None:
                    continue
                parent_table, rest = col.foreign_key.split("(", 1)
                parent_col = rest.rstrip(")")
                parent_temp = trial_name_by_table.get(parent_table)
                parent_alias = col_alias_by_table.get(parent_table, {}).get(parent_col)
                child_alias = child_aliases.get(col.name)
                if parent_temp is None or parent_alias is None or child_alias is None:
                    continue
                fk_res = conn.execute(
                    f"SELECT COUNT(*) FROM temp.main.{child_temp} c "
                    f"WHERE c.{child_alias} IS NOT NULL "
                    f"AND NOT EXISTS ("
                    f"SELECT 1 FROM temp.main.{parent_temp} p "
                    f"WHERE p.{parent_alias} = c.{child_alias})"
                ).fetchone()
                if fk_res is not None:
                    fk_violation_count += int(fk_res[0] or 0)
    finally:
        for temp_tbl in created_temps:
            conn.execute(f"DROP TABLE IF EXISTS temp.main.{temp_tbl}")

    constraint_violations = pk_violation_count + fk_violation_count
    lost_rows = failed_cast_rows + constraint_violations
    loss_rate = min(1.0, round(lost_rows / max(total_rows, 1), 6))
    trial_ok = ddl_ok and cast_failure_count == 0 and constraint_violations == 0

    return ValidationSummary(
        ddl_parse_ok=ddl_ok,
        trial_load_ok=trial_ok,
        trial_load_executed=True,
        constraint_violation_count=constraint_violations,
        cast_failure_count=cast_failure_count,
        pk_violation_count=pk_violation_count,
        fk_violation_count=fk_violation_count,
        data_loss_rate=loss_rate,
    )


def _score_candidate(
    built: _BuiltCandidate,
    purpose: Purpose,
    bundles: Sequence[TableProfileBundle],
    relations: MultiTableRelationAnalysis,
    merged_profiles: Sequence[ColumnProfile],
    merged_stats: Sequence[ColumnStats],
    merged_sampling: SamplingPlan,
    total_derivations: int,
    sensitivity_by_table: Mapping[str, SensitivityMap],
    mctx: MultiTableContext | None,
) -> tuple[Scorecard, str, dict[tuple[str, str], exp.DataType]]:
    """후보 스키마를 `generate_ddl` + `verify_ddl` + DuckDB 시험 적재로 검증하고 채점한다."""
    cand = built.candidate
    ddl_ok = False
    ddl_sql = ""
    col_types: dict[tuple[str, str], exp.DataType] = {}
    try:
        col_types = resolve_postgres_column_types(
            cand,
            profiles=merged_profiles,
            stats=merged_stats,
            sampling=merged_sampling,
        )
        ddl_sql = generate_ddl(
            cand,
            profiles=merged_profiles,
            stats=merged_stats,
            sampling=merged_sampling,
        )
        verify_ddl(ddl_sql, cand)
        ddl_ok = True
    except ValueError:
        ddl_ok = False

    validation = _run_trial_load(
        mctx,
        built,
        bundles,
        relations,
        col_types,
        ddl_ok=ddl_ok,
    )

    n_tables = len(cand.tables)
    total_rows = sum(b.source.row_count for b in bundles)
    pk_tables = sum(1 for t in cand.tables if any(c.is_primary_key for c in t.columns))
    fk_count = sum(1 for t in cand.tables for c in t.columns if c.foreign_key is not None)
    omitted_count = sum(len(v) for v in built.omitted_derivations.values())
    partition_count = sum(1 for t in cand.tables if t.partition is not None)
    idx_count = sum(len(t.index) for t in cand.tables)

    integrity_val = 0.0
    if ddl_ok:
        pk_ratio = pk_tables / max(n_tables, 1)
        fk_bonus = min(15.0, fk_count * 4.0)
        integrity_val = min(100.0, 65.0 + 20.0 * pk_ratio + fk_bonus)
        total_issues = validation.constraint_violation_count + validation.cast_failure_count
        if total_issues > 0:
            issue_ratio = total_issues / max(total_rows, 1)
            penalty = min(45.0, 15.0 + 100.0 * issue_ratio)
            integrity_val = max(20.0, integrity_val - penalty)

    # 중복도: 파생 컬럼을 제거(정규화)했을수록 높음
    if total_derivations > 0:
        redundancy_val = 76.0 + (20.0 * omitted_count / total_derivations)
    else:
        redundancy_val = 88.0 if cand.pattern is SchemaPattern.THIRD_NORMAL_FORM else 80.0
    if cand.id == "C2_COMPACT_RAW":
        redundancy_val = min(100.0, redundancy_val + 4.0)

    # 용도 적합성
    if purpose is Purpose.OLTP:
        if cand.id == "C2_NORMALIZED":
            fitness_val = 95.0
        elif cand.id == "C1_RELATIONAL":
            fitness_val = 91.0 if omitted_count == 0 else 86.0
        elif cand.id == "C3_ANALYTICS":
            fitness_val = 72.0
        else:
            fitness_val = 78.0
    elif purpose is Purpose.ANALYTICS:
        if cand.id == "C3_ANALYTICS":
            fitness_val = 94.0 + min(6.0, partition_count * 3.0 + min(idx_count, 3))
        elif cand.id == "C1_RELATIONAL":
            fitness_val = 84.0
        else:
            fitness_val = 74.0
    else:  # ARCHIVE
        if cand.id == "C2_COMPACT_RAW":
            fitness_val = 94.0
        elif cand.id == "C1_RELATIONAL":
            fitness_val = 88.0
        elif cand.id == "C2_NORMALIZED":
            fitness_val = 75.0
        else:
            fitness_val = 70.0

    # 적재 단순성: 변환·분리 컬럼이 적을수록 단순
    extra_ops = built.materialized_slot_count + omitted_count + len(built.surrogate_tables)
    load_simplicity_val = max(55.0, 95.0 - extra_ops * 4.0)

    # 저장 효율: 파생 컬럼 제거 시 보너스, 추가 슬롯 구체화 시 소폭 차감
    storage_val = min(
        100.0,
        max(60.0, 84.0 + omitted_count * 6.0 - built.materialized_slot_count * 1.5),
    )

    # 확장 용이성: FK + 문자열 슬롯 분해로 조인·분석 확장성이 높아짐
    extensibility_val = min(
        100.0,
        75.0 + min(15.0, fk_count * 3.5) + min(10.0, built.materialized_slot_count * 3.5),
    )

    # 보안·민감정보 격리 점수(S7 PII 탐지 결과 + 사용자 확정 마스킹 정책 반영)
    all_pii_pairs: list[tuple[str, SensitivityAssignment]] = [
        (t_name, a) for t_name, smap in sensitivity_by_table.items() for a in smap.assignments
    ]
    if not all_pii_pairs:
        security_val = 92.0
        security_detail = "민감(PII) 컬럼 미탐지 · 원본 셀 값 비노출 결정론적 스키마 생성"
    else:
        masked_pii = [
            (t_name, a)
            for t_name, a in all_pii_pairs
            if built.column_masking.get(t_name, {}).get(a.column, MaskingPolicy.NONE)
            is not MaskingPolicy.NONE
        ]
        unmasked_high = [
            a
            for t_name, a in all_pii_pairs
            if a.level in {SensitivityLevel.L2, SensitivityLevel.L3}
            and built.column_masking.get(t_name, {}).get(a.column, MaskingPolicy.NONE)
            is MaskingPolicy.NONE
        ]
        if masked_pii and not unmasked_high:
            security_val = 95.0
            security_detail = (
                f"민감(PII) 컬럼 {len(all_pii_pairs)}건 중 보호 대상 전체"
                f"({len(masked_pii)}건)에 마스킹·해시 정책 확정됨"
            )
        else:
            unmasked_all = [
                a
                for t_name, a in all_pii_pairs
                if built.column_masking.get(t_name, {}).get(a.column, MaskingPolicy.NONE)
                is MaskingPolicy.NONE
            ]
            max_level = max(
                (a.level for a in (unmasked_all or [a for _, a in all_pii_pairs])),
                key=lambda lv: _LEVEL_RANK[lv],
            )
            if max_level is SensitivityLevel.L3:
                base_sec = 60.0
            elif max_level is SensitivityLevel.L2:
                base_sec = 72.0
            else:
                base_sec = 82.0
            if masked_pii:
                security_val = min(88.0, base_sec + 8.0)
                security_detail = (
                    f"민감(PII) 컬럼 {len(all_pii_pairs)}건 중 {len(masked_pii)}건 보호 적용"
                    f"(미보호 최고 등급 {max_level.value})"
                )
            else:
                security_val = base_sec
                security_detail = (
                    f"민감(PII) 컬럼 {len(all_pii_pairs)}건 탐지(최고 등급 {max_level.value}) — "
                    "적재 시 암호화·마스킹 및 접근 통제 적용 권고"
                )

    weights = _PURPOSE_WEIGHTS[purpose]
    weighted = round(
        integrity_val * weights["integrity"]
        + redundancy_val * weights["redundancy"]
        + fitness_val * weights["fitness"]
        + load_simplicity_val * weights["load_simplicity"]
        + storage_val * weights["storage_efficiency"]
        + extensibility_val * weights["extensibility"],
        2,
    )

    trial_detail = (
        f"시험적재 {'통과' if validation.trial_load_ok else '실패'}"
        f"(캐스트실패 {validation.cast_failure_count}, PK위반 {validation.pk_violation_count}, "
        f"FK고아 {validation.fk_violation_count})"
        if validation.trial_load_executed
        else f"시험적재 미실행(FK고아 {validation.fk_violation_count})"
    )

    scorecard = Scorecard(
        candidate_id=cand.id,
        integrity=CriterionScore(
            score=round(integrity_val, 2),
            detail=(
                f"DDL 검증 {'통과' if ddl_ok else '실패'}, "
                f"PK {pk_tables}/{n_tables}, FK {fk_count}건, {trial_detail}"
            ),
        ),
        redundancy=CriterionScore(
            score=round(redundancy_val, 2),
            detail=f"파생 중복 제거 {omitted_count}/{total_derivations}건",
        ),
        fitness=CriterionScore(
            score=round(fitness_val, 2),
            detail=f"목적 `{purpose.value}` 기준 스키마 적합도",
        ),
        load_simplicity=CriterionScore(
            score=round(load_simplicity_val, 2),
            detail=(
                f"슬롯 파싱 {built.materialized_slot_count}건, "
                f"서로게이트 {len(built.surrogate_tables)}건"
            ),
        ),
        storage_efficiency=CriterionScore(
            score=round(storage_val, 2),
            detail="관측 범위 기반 최소 안전 타입 매핑",
        ),
        extensibility=CriterionScore(
            score=round(extensibility_val, 2),
            detail=f"FK {fk_count}건 및 구조화 슬롯 {built.materialized_slot_count}건 반영",
        ),
        security_isolation=CriterionScore(
            score=security_val,
            detail=security_detail,
        ),
        weighted_total=weighted,
        purpose=purpose,
        validation=validation,
    )
    return scorecard, ddl_sql, col_types


def _topological_order(candidate: SchemaCandidate) -> list[LoadOrder]:
    """후보 스키마의 FK 의존성으로 Kahn 위상 적재 순서를 계산한다(부모 먼저)."""
    depends: dict[str, set[str]] = {}
    for table in candidate.tables:
        deps: set[str] = set()
        for col in table.columns:
            if col.foreign_key is None:
                continue
            parent = col.foreign_key.split("(", 1)[0]
            if parent != table.name:
                deps.add(parent)
        depends[table.name] = deps

    ordered: list[LoadOrder] = []
    done: set[str] = set()
    remaining = sorted(depends.keys())
    while remaining:
        ready = [name for name in remaining if depends[name] <= done]
        if not ready:
            ready = [remaining[0]]
        for name in ready:
            ordered.append(LoadOrder(table=name, depends_on=sorted(depends[name])))
            done.add(name)
        remaining = [name for name in remaining if name not in done]
    return ordered


def _pg_type_cast_label(
    col: TargetColumn,
    resolved_type: exp.DataType | None = None,
) -> str:
    """`ddl.sql`과 1:1 일치하는 PostgreSQL 캐스팅 타입 문자열을 반환한다."""
    if resolved_type is not None:
        return resolved_type.sql(dialect="postgres").lower()
    if col.physical_type is PhysicalType.INTEGER:
        return "bigint" if col.is_identity or col.source_columns == ["__surrogate__"] else "int"
    if col.physical_type is PhysicalType.DECIMAL:
        if col.precision is not None and col.scale is not None:
            return f"numeric({col.precision}, {col.scale})"
        return "numeric"
    if col.physical_type is PhysicalType.FLOAT:
        return "double precision"
    if col.physical_type is PhysicalType.STRING:
        return "text"
    return str(col.physical_type.value)


def _build_index_ddl(indexes: Sequence[IndexRecommendation]) -> str:
    """`sqlglot` AST로 결정론적 PostgreSQL `CREATE [UNIQUE] INDEX` DDL을 생성한다."""
    stmts: list[str] = []
    seen_names: set[str] = set()
    for idx in indexes:
        prefix = "uq" if idx.unique else "idx"
        raw_name = f"{prefix}_{idx.table}_{'_'.join(idx.columns)}"
        idx_name = _to_sql_ident(raw_name, used=seen_names, fallback_prefix=prefix)
        create_idx = exp.Create(
            this=exp.Index(
                this=exp.Identifier(this=idx_name, quoted=True),
                table=exp.Table(this=exp.Identifier(this=idx.table, quoted=True)),
                params=exp.IndexParameters(
                    columns=[
                        exp.Ordered(this=exp.Column(this=exp.Identifier(this=c, quoted=True)))
                        for c in idx.columns
                    ]
                ),
            ),
            kind="INDEX",
            unique=idx.unique,
        )
        stmts.append(create_idx.sql(dialect="postgres", identify=True))
    return ";\n".join(stmts) + (";" if stmts else "")


def _build_table_load_strategies(
    built: _BuiltCandidate,
    load_order: Sequence[LoadOrder],
    bundles: Sequence[TableProfileBundle],
    relations: MultiTableRelationAnalysis,
    purpose: Purpose,
    col_types: Mapping[tuple[str, str], exp.DataType],
    sensitivity_by_table: Mapping[str, SensitivityMap],
) -> tuple[list[TableLoadStrategy], str, str]:
    """위상 정렬 순서에 맞춰 테이블별 적재 전략·컬럼 변환·권고 인덱스·컬럼 주석 DDL을 생성한다."""
    bundle_by_name = {b.source.table_name: b for b in bundles}
    table_by_name = {t.name: t for t in built.candidate.tables}
    all_indexes: list[IndexRecommendation] = []
    column_comments: list[tuple[str, str, str]] = []
    strategies: list[TableLoadStrategy] = []

    for order_idx, lo in enumerate(load_order):
        t_name = lo.table
        target_table = table_by_name[t_name]
        src_name = built.source_table_map[t_name]
        bundle = bundle_by_name[src_name]
        pk_rec = relations.primary_keys.get(src_name)
        cls_by_col = {c.column: c for c in bundle.classifications}
        uses_surrogate = t_name in built.surrogate_tables
        src_encoding = bundle.ingest.files[0].encoding if bundle.ingest.files else "utf-8"

        pk_cols = [c.name for c in target_table.columns if c.is_primary_key]
        mappings: list[ColumnMapping] = []
        temporal_cols: list[str] = []
        table_indexes: list[IndexRecommendation] = []
        indexed_tuples: set[tuple[str, ...]] = set()

        for col in target_table.columns:
            src_col = col.source_columns[0]
            transform = built.transforms.get((t_name, col.name), "identity")
            null_handling = NullHandling.REJECT_ROW if not col.nullable else NullHandling.NULL
            resolved_dt = col_types.get((t_name, col.name))
            mappings.append(
                ColumnMapping(
                    source_column=src_col,
                    target_table=t_name,
                    target_column=col.name,
                    transform=transform,
                    type_cast=_pg_type_cast_label(col, resolved_dt),
                    null_handling=null_handling,
                )
            )
            if col.name != src_col and src_col != "__surrogate__":
                if (t_name, col.name) in built.slot_extract_specs:
                    column_comments.append((t_name, col.name, f"원본 '{src_col}'에서 추출된 슬롯"))
                else:
                    column_comments.append((t_name, col.name, src_col))

            src_cls = cls_by_col.get(src_col)
            if (
                src_cls is not None
                and src_cls.category is ColumnRoleCategory.TEMPORAL
                and src_cls.missing_count == 0
                and (t_name, col.name) not in built.slot_extract_specs
            ):
                temporal_cols.append(col.name)

            # FK 컬럼 인덱스 (PK의 선두 컬럼과 겹치지 않을 때만)
            if col.foreign_key is not None:
                is_pk_prefix = len(pk_cols) > 0 and pk_cols[0] == col.name
                if not is_pk_prefix and (col.name,) not in indexed_tuples:
                    indexed_tuples.add((col.name,))
                    table_indexes.append(
                        IndexRecommendation(
                            table=t_name,
                            columns=[col.name],
                            unique=False,
                            reason=f"FK 조인 성능 최적화 ({col.foreign_key})",
                        )
                    )

        # 시간 컬럼 인덱스 (워터마크·기간 조회용)
        for t_col in temporal_cols[:1]:
            if (t_col,) not in indexed_tuples and (not pk_cols or pk_cols[0] != t_col):
                indexed_tuples.add((t_col,))
                table_indexes.append(
                    IndexRecommendation(
                        table=t_name,
                        columns=[t_col],
                        unique=False,
                        reason="시계열 기간 조회 및 워터마크 증분 스캔 최적화",
                    )
                )

        # 표본 관측 대안 유일 키는 미래 배치에서 중복이 발생할 수 있으므로(예: 우편번호)
        # 물리 UNIQUE INDEX로 자동 승격하지 않고 권고 노트/리포트에만 기록한다.
        alt_key_advisories: list[str] = []
        if pk_rec is not None:
            for alt_key in pk_rec.alternative_keys[:3]:
                if alt_key != pk_rec.columns:
                    alt_key_advisories.append(", ".join(alt_key))

        # 사용자가 명시적으로 확정한 UNIQUE 제약은 물리 UNIQUE INDEX로 생성한다.
        confirmed_uq_cols = list(built.confirmed_unique_cols.get(t_name, ()))
        for uq_col in confirmed_uq_cols:
            if pk_cols == [uq_col]:
                continue
            if (uq_col,) in indexed_tuples:
                table_indexes = [
                    idx.model_copy(
                        update={
                            "unique": True,
                            "reason": f"{idx.reason} · 사용자 확정 유일 제약(UNIQUE)",
                        }
                    )
                    if idx.columns == [uq_col]
                    else idx
                    for idx in table_indexes
                ]
            else:
                indexed_tuples.add((uq_col,))
                table_indexes.append(
                    IndexRecommendation(
                        table=t_name,
                        columns=[uq_col],
                        unique=True,
                        reason="사용자 확정 유일 제약(UNIQUE)",
                    )
                )

        # 분석 목적일 때 범주형 필터 컬럼 인덱스 추가
        if purpose is Purpose.ANALYTICS:
            for col in target_table.columns:
                src_cls = cls_by_col.get(col.source_columns[0])
                if (
                    src_cls is not None
                    and (t_name, col.name) not in built.slot_extract_specs
                    and src_cls.category
                    in {
                        ColumnRoleCategory.CATEGORICAL_NOMINAL,
                        ColumnRoleCategory.CATEGORICAL_ORDINAL,
                    }
                    and (col.name,) not in indexed_tuples
                ):
                    indexed_tuples.add((col.name,))
                    table_indexes.append(
                        IndexRecommendation(
                            table=t_name,
                            columns=[col.name],
                            unique=False,
                            reason="분석용 범주 필터/GROUP BY 성능 최적화",
                        )
                    )

        # 증분 적재 방식 결정
        watermark_col = temporal_cols[0] if temporal_cols else None
        if uses_surrogate:
            inc_mode = IncrementMode.APPEND
            upsert_keys: list[str] = []
            dedup = DeduplicationRule(enabled=False, keys=[])
        elif watermark_col is not None and pk_cols:
            inc_mode = IncrementMode.WATERMARK
            upsert_keys = list(pk_cols)
            dedup = DeduplicationRule(enabled=True, keys=list(pk_cols), keep="last")
        elif pk_cols:
            inc_mode = IncrementMode.UPSERT
            upsert_keys = list(pk_cols)
            dedup = DeduplicationRule(enabled=True, keys=list(pk_cols), keep="last")
        else:
            inc_mode = IncrementMode.APPEND
            upsert_keys = []
            dedup = DeduplicationRule(enabled=False, keys=[])

        notes: list[str] = []
        if uses_surrogate:
            notes.append(
                "자연 PK 부재로 `GENERATED ALWAYS AS IDENTITY` 서로게이트 BIGINT 키와 "
                "APPEND 적재를 권고한다."
            )
        if t_name in built.omitted_derivations:
            notes.append(
                "정규화 후보 선택으로 산술 파생 컬럼을 테이블에서 제외하고 뷰/계산식 계산을 "
                f"권고한다: {', '.join(built.omitted_derivations[t_name])}"
            )
        if target_table.partition:
            notes.append(
                f"시계열 분석용 선언적 파티셔닝 반영: PARTITION BY {target_table.partition}"
            )
        if t_name in built.partition_notes:
            notes.extend(built.partition_notes[t_name])
        if confirmed_uq_cols:
            notes.append(
                "사용자 확정 유일(UNIQUE) 제약 반영: "
                + ", ".join(f"`{c}`" for c in confirmed_uq_cols)
            )
        if alt_key_advisories:
            notes.append(
                "표본 관측 유일 후보(비즈니스 규칙 확인 후 UNIQUE 제약 적용 검토): "
                + "; ".join(alt_key_advisories)
            )

        table_masking = dict(built.column_masking.get(t_name, {}))
        if table_masking:
            mask_summary = ", ".join(
                f"`{col_k}`={pol.value}" for col_k, pol in table_masking.items()
            )
            notes.append(f"사용자 확정 개인정보 보호 정책: {mask_summary}")

        smap = sensitivity_by_table.get(src_name)
        sensitive_cols: list[str] = []
        if smap is not None and smap.assignments:
            for item in smap.assignments:
                sensitive_cols.append(item.column)
                primary_type = item.types[0]
                notes.append(
                    f"민감(PII) 컬럼 `{item.column}` [{item.level.value} / "
                    f"{', '.join(t.value for t in item.types)}]: "
                    f"{masking_recommendation(primary_type)}"
                )

        all_indexes.extend(table_indexes)
        strategies.append(
            TableLoadStrategy(
                source_table=src_name,
                target_table=t_name,
                source_encoding=src_encoding,
                load_order_index=order_idx,
                depends_on=list(lo.depends_on),
                primary_key=pk_cols,
                uses_surrogate_key=uses_surrogate,
                increment_mode=inc_mode,
                upsert_keys=upsert_keys,
                watermark_column=watermark_col,
                deduplication=dedup,
                column_mappings=mappings,
                recommended_indexes=table_indexes,
                sensitive_columns=sensitive_cols,
                column_masking=table_masking,
                confirmed_unique_columns=confirmed_uq_cols,
                expected_row_count=bundle.source.row_count,
                notes=notes,
            )
        )

    index_ddl = _build_index_ddl(all_indexes)
    comment_ddl = build_column_comment_ddl(column_comments)
    return strategies, index_ddl, comment_ddl


def render_mermaid_erd(
    candidate: SchemaCandidate,
    relations: MultiTableRelationAnalysis,
    *,
    col_types: Mapping[tuple[str, str], exp.DataType] | None = None,
) -> str:
    """선택된 스키마 후보와 관계 정보를 Mermaid ``erDiagram`` 코드로 렌더링한다."""
    card_by_pair: dict[tuple[str, str], RelationshipCardinality] = {}
    for fk in relations.foreign_keys:
        card_by_pair[(fk.child_table, fk.parent_table)] = fk.cardinality

    lines: list[str] = ["erDiagram"]
    for table in candidate.tables:
        lines.append(f"    {table.name} {{")
        for col in table.columns:
            resolved_dt = col_types.get((table.name, col.name)) if col_types is not None else None
            mtype = _mermaid_type(col.physical_type, resolved_dt)
            tags: list[str] = []
            if col.is_primary_key:
                tags.append("PK")
            if col.foreign_key is not None:
                tags.append("FK")
            tag_str = f" {','.join(tags)}" if tags else ""
            lines.append(f"        {mtype} {col.name}{tag_str}")
        lines.append("    }")

    for table in candidate.tables:
        for col in table.columns:
            if col.foreign_key is None:
                continue
            parent_table = col.foreign_key.split("(", 1)[0]
            card = card_by_pair.get((table.name, parent_table), RelationshipCardinality.MANY_TO_ONE)
            connector = "||--o|" if card is RelationshipCardinality.ONE_TO_ONE else "||--o{"
            src_col = col.source_columns[0]
            label = (
                f"{col.name} (from {src_col})"
                if src_col != col.name and src_col != "__surrogate__"
                else col.name
            )
            lines.append(f'    {parent_table} {connector} {table.name} : "{label}"')

    return "\n".join(lines) + "\n"


def _mermaid_type(phys: PhysicalType, resolved_type: exp.DataType | None = None) -> str:
    """`ddl.sql`과 모순 없는 Mermaid `erDiagram` 타입 토큰을 반환한다."""
    if resolved_type is not None:
        pg = resolved_type.sql(dialect="postgres").upper()
        if pg in {"INT", "INTEGER"}:
            return "INT"
        if pg == "BIGINT":
            return "BIGINT"
        if pg.startswith("NUMERIC"):
            return "NUMERIC"
        if pg.startswith("VARCHAR"):
            return "VARCHAR"
        if pg == "TEXT":
            return "TEXT"
        if pg == "DOUBLE PRECISION":
            return "DOUBLE"
        return pg.replace(" ", "_")

    mapping = {
        PhysicalType.INTEGER: "BIGINT",
        PhysicalType.FLOAT: "DOUBLE",
        PhysicalType.DECIMAL: "NUMERIC",
        PhysicalType.BOOLEAN: "BOOLEAN",
        PhysicalType.DATE: "DATE",
        PhysicalType.TIME: "TIME",
        PhysicalType.TIMESTAMP: "TIMESTAMP",
        PhysicalType.TIMESTAMPTZ: "TIMESTAMPTZ",
        PhysicalType.JSON: "JSONB",
        PhysicalType.BINARY: "BYTEA",
        PhysicalType.STRING: "VARCHAR",
    }
    return mapping.get(phys, "TEXT")


_SKIPPED_KEY_MASKING_WARNING = "키 열 가리기 미적용"


def plan_multi_table_load(
    bundles: Sequence[TableProfileBundle],
    relations: MultiTableRelationAnalysis,
    *,
    purpose: Purpose = Purpose.OLTP,
    decompose_strings: bool = True,
    mctx: MultiTableContext | None = None,
    overrides: SchemaOverrideSpec | None = None,
) -> MultiTableLoadPlan:
    """Milestone E4 통합 실행: PII 탐지 → 후보 합성 → DDL+DuckDB 시험 적재 채점 → 플랜·ERD."""
    plan, _, _ = _plan_multi_table_load(
        bundles,
        relations,
        purpose=purpose,
        decompose_strings=decompose_strings,
        mctx=mctx,
        overrides=overrides,
    )
    return plan


def _plan_multi_table_load(
    bundles: Sequence[TableProfileBundle],
    relations: MultiTableRelationAnalysis,
    *,
    purpose: Purpose,
    decompose_strings: bool,
    mctx: MultiTableContext | None,
    overrides: SchemaOverrideSpec | None,
    engine: str | None = None,
) -> tuple[MultiTableLoadPlan, dict[str, dict[str, MaskingPolicy]], list[str]]:
    acyclic_fks, cycle_warnings = _select_acyclic_fks(relations)
    for fk in acyclic_fks:
        if fk.orphan_rows > 0:
            cycle_warnings.append(
                f"물리 FK `{fk.child_table}.{fk.child_columns[0]}` → "
                f"`{fk.parent_table}.{fk.parent_columns[0]}`에 참조 대상 없는 고아 행 "
                f"{fk.orphan_rows}건이 존재한다(포함률 {fk.inclusion_rate:.1%}). "
                "PostgreSQL FK 제약 적용 전 고아 행 정제·격리(quarantine) 또는 "
                "부모 키 보완이 필요하다."
            )

    merged_profiles, merged_stats, merged_sampling = _merge_profiles_and_stats(bundles)

    # S7 결정론적 PII·민감 컬럼 탐지 (원본 셀 값 비노출)
    sensitivity_by_table: dict[str, SensitivityMap] = {}
    for bundle in bundles:
        slot = None
        if mctx is not None:
            slot = mctx.activate(bundle.source.table_id)
        sensitivity_by_table[bundle.source.table_name] = detect_table_sensitivity(bundle, slot)

    total_derivations = sum(
        len(relations.table_relations[b.source.table_name].derivations)
        for b in bundles
        if b.source.table_name in relations.table_relations
    )

    built_candidates: list[_BuiltCandidate] = []

    # C1: 기본 관계형 스키마 (PK + FK + 문자열 슬롯 분해 반영, 원본 파생 컬럼 유지)
    c1 = _build_candidate(
        "C1_RELATIONAL",
        SchemaPattern.THIRD_NORMAL_FORM,
        (
            "모든 원본 테이블을 보존하면서 최적 PK·교차 FK 및 규칙 문자열 가변 슬롯을 "
            "컬럼/숨은 FK로 반영한 균형형 관계형 스키마"
        ),
        bundles,
        relations,
        acyclic_fks,
        decompose_strings=decompose_strings,
        omit_derivations=False,
        analytics_mode=False,
        overrides=overrides,
        engine=engine,
    )
    built_candidates.append(c1)

    # C2: 산술 파생 중복을 제거한 3NF 정규화 후보 (또는 원본 최소 변환 아카이브 후보)
    if total_derivations > 0:
        c2 = _build_candidate(
            "C2_NORMALIZED",
            SchemaPattern.THIRD_NORMAL_FORM,
            (
                "산술 파생 중복 컬럼을 테이블에서 제외(뷰/계산식 위임)해 갱신 이상을 방지하는 "
                "OLTP 3NF 정규화 스키마"
            ),
            bundles,
            relations,
            acyclic_fks,
            decompose_strings=decompose_strings,
            omit_derivations=True,
            analytics_mode=False,
            overrides=overrides,
            engine=engine,
        )
        built_candidates.append(c2)

    if decompose_strings and c1.materialized_slot_count > 0:
        c2_raw = _build_candidate(
            "C2_COMPACT_RAW",
            SchemaPattern.WIDE,
            (
                "문자열 슬롯 분해 없이 원본 컬럼 구조를 최소 변환으로 보존하는 "
                "아카이브/스테이징 스키마"
            ),
            bundles,
            relations,
            acyclic_fks,
            decompose_strings=False,
            omit_derivations=False,
            analytics_mode=False,
            overrides=overrides,
            engine=engine,
        )
        built_candidates.append(c2_raw)

    # C3: 분석·BI용 스키마 (파생 지표 보존 + 시계열 파티셔닝 + 범주/시간 인덱스 확장)
    c3 = _build_candidate(
        "C3_ANALYTICS",
        SchemaPattern.STAR,
        (
            "파생 수치 지표와 문자열 분해 슬롯을 물리 컬럼으로 유지하고 시계열 파티셔닝 및 "
            "범주/FK 인덱스를 강화한 분석·BI 스키마"
        ),
        bundles,
        relations,
        acyclic_fks,
        decompose_strings=decompose_strings,
        omit_derivations=False,
        analytics_mode=True,
        overrides=overrides,
        engine=engine,
    )
    built_candidates.append(c3)

    scored: list[tuple[Scorecard, _BuiltCandidate, str, dict[tuple[str, str], exp.DataType]]] = []
    for built in built_candidates:
        sc, ddl, col_types = _score_candidate(
            built,
            purpose,
            bundles,
            relations,
            merged_profiles,
            merged_stats,
            merged_sampling,
            total_derivations,
            sensitivity_by_table,
            mctx,
        )
        scored.append((sc, built, ddl, col_types))

    # DDL 검증 통과 + 가중 총점 최고 후보 선택 (동률 시 candidate_id 길이 짧은 순)
    scored.sort(
        key=lambda item: (
            item[0].validation.ddl_parse_ok,
            item[0].weighted_total,
            -len(item[0].candidate_id),
        ),
        reverse=True,
    )
    best_sc, best_built, best_ddl, best_col_types = scored[0]

    load_order = _topological_order(best_built.candidate)
    table_plans, index_ddl, comment_ddl = _build_table_load_strategies(
        best_built,
        load_order,
        bundles,
        relations,
        purpose,
        best_col_types,
        sensitivity_by_table,
    )
    erd_mermaid = render_mermaid_erd(best_built.candidate, relations, col_types=best_col_types)

    plan = MultiTableLoadPlan(
        purpose=purpose,
        selected_candidate_id=best_sc.candidate_id,
        candidates=[item[1].candidate for item in scored],
        scorecards=[item[0] for item in scored],
        load_order=load_order,
        table_plans=table_plans,
        ddl_sql=best_ddl,
        index_ddl_sql=index_ddl,
        comment_ddl_sql=comment_ddl,
        erd_mermaid=erd_mermaid,
        cycle_warnings=cycle_warnings,
    )
    skipped = best_built.skipped_key_masking
    masking_warnings = [
        f"{_SKIPPED_KEY_MASKING_WARNING} {table!r}.{column!r}. "
        "키의 저장 타입이 문자열 외 형식으로 유지되어 원래 값이 저장됩니다."
        for table, columns in skipped.items()
        for column in columns
    ]
    # 분석 번들은 관계 메모에서 경고를 수집한다. 같은 관계를 다시 계획하면 갱신한다.
    relations.notes[:] = [
        *(note for note in relations.notes if not note.startswith(_SKIPPED_KEY_MASKING_WARNING)),
        *masking_warnings,
    ]
    return plan, skipped, masking_warnings


def render_analysis_report(bundle: MultiTableAnalysisBundle) -> str:
    """다중 테이블 분석·패턴 분해·PK/FK·시험 적재·PII·적재 계획을 Markdown으로 렌더링한다."""
    lp = bundle.load_plan
    rel = bundle.relations
    total_rows = sum(t.source.row_count for t in bundle.tables)
    slot_fk_count = sum(1 for fk in rel.foreign_keys if fk.child_slot_name is not None)
    selected_sc = next(
        (sc for sc in lp.scorecards if sc.candidate_id == lp.selected_candidate_id),
        lp.scorecards[0] if lp.scorecards else None,
    )
    total_pii = sum(len(tp.sensitive_columns) for tp in lp.table_plans)

    trial_summary = "미실행"
    if selected_sc is not None:
        v = selected_sc.validation
        if v.trial_load_executed:
            status_lbl = "통과" if v.trial_load_ok else "실패"
            trial_summary = (
                f"{status_lbl} (캐스팅 실패 {v.cast_failure_count}건, "
                f"PK 위반 {v.pk_violation_count}건, FK 고아 {v.fk_violation_count}건, "
                f"손실률 {v.data_loss_rate:.2%})"
            )
        else:
            trial_summary = f"미실행 (FK 고아 관측 {v.fk_violation_count}건)"

    lines: list[str] = [
        "# AutomaticETL 다중 테이블 결정론적 분석 및 DB 적재 계획 리포트",
        "",
        "## 1. 전체 요약",
        "",
        f"- **분석 목적(`purpose`)**: `{bundle.purpose.value}`",
        f"- **선택된 최적 스키마 후보**: `{lp.selected_candidate_id}`",
        f"- **입력 테이블 수**: {len(bundle.tables)}개 (총 {total_rows:,}행)",
        f"- **탐지된 교차 FK 관계**: {len(rel.foreign_keys)}건 "
        f"(규칙 문자열 숨은 슬롯 FK {slot_fk_count}건 포함)",
        f"- **DuckDB 시험 적재 검증**: {trial_summary}",
        f"- **탐지된 민감(PII) 컬럼**: {total_pii}건",
        "- **AI/LLM 사용 여부**: 미사용 (100% 결정론적 통계·규칙 엔진)",
        "",
    ]

    if lp.cycle_warnings or bundle.warnings:
        lines.append("### 주의 및 경고")
        for w in [*lp.cycle_warnings, *bundle.warnings]:
            lines.append(f"- {w}")
        lines.append("")

    # 후보 점수표
    lines.extend(
        [
            "## 2. 스키마 후보 평가표",
            "",
            (
                "| 순위 | 후보 ID | 패턴 | 무결성 | 중복도 | 용도적합 | "
                "적재단순 | 확장성 | 가중총점 | DDL 검증 | 시험 적재 |"
            ),
            "|---:|---|---|---:|---:|---:|---:|---:|---:|---|---|",
        ]
    )
    cand_by_id = {c.id: c for c in lp.candidates}
    for rank, sc in enumerate(lp.scorecards, start=1):
        cand = cand_by_id[sc.candidate_id]
        mark = " **★**" if sc.candidate_id == lp.selected_candidate_id else ""
        ddl_mark = "통과" if sc.validation.ddl_parse_ok else "실패"
        if not sc.validation.trial_load_executed:
            trial_mark = "미실행"
        elif sc.validation.trial_load_ok:
            trial_mark = "통과"
        else:
            issues = sc.validation.constraint_violation_count + sc.validation.cast_failure_count
            trial_mark = f"실패 ({issues}건)"
        lines.append(
            f"| {rank} | `{sc.candidate_id}`{mark} | `{cand.pattern.value}` | "
            f"{sc.integrity.score:.1f} | {sc.redundancy.score:.1f} | "
            f"{sc.fitness.score:.1f} | {sc.load_simplicity.score:.1f} | "
            f"{sc.extensibility.score:.1f} | **{sc.weighted_total:.2f}** | "
            f"{ddl_mark} | {trial_mark} |"
        )
    lines.append("")

    # Mermaid ERD
    lines.extend(
        [
            "## 3. 엔티티 관계도 (ERD)",
            "",
            "```mermaid",
            lp.erd_mermaid.rstrip(),
            "```",
            "",
        ]
    )

    # 교차 FK 표
    lines.extend(["## 4. 테이블 간 관계(FK) 분석", ""])
    if rel.foreign_keys:
        lines.extend(
            [
                (
                    "| 자식 테이블 | 자식 컬럼(슬롯) | 기수성 | 부모 테이블 | "
                    "부모 컬럼 | 포함률 | 부모커버리지 | 고아행 | 신뢰도 |"
                ),
                "|---|---|---|---|---|---:|---:|---:|---:|",
            ]
        )
        for fk in rel.foreign_keys:
            child_col = ", ".join(fk.child_columns)
            if fk.child_slot_name:
                child_col = f"`{child_col}` → 슬롯 `{fk.child_slot_name}`"
            else:
                child_col = f"`{child_col}`"
            parent_col = f"`{', '.join(fk.parent_columns)}`"
            lines.append(
                f"| `{fk.child_table}` | {child_col} | `{fk.cardinality.value}` | "
                f"`{fk.parent_table}` | {parent_col} | {fk.inclusion_rate:.1%} | "
                f"{fk.parent_coverage:.1%} | {fk.orphan_rows} | {fk.confidence:.2f} |"
            )
    else:
        lines.append("- 탐지된 교차 테이블 외래 키가 없다.")
    lines.append("")

    # 테이블별 칼럼 유형·결측치·문자열 패턴·PK 분석
    plan_by_table = {tp.source_table: tp for tp in lp.table_plans}
    lines.extend(["## 5. 테이블별 칼럼 프로파일 · 문자열 분해 · PK 선정", ""])
    for tb in bundle.tables:
        t_name = tb.source.table_name
        pk = rel.primary_keys.get(t_name)
        tp_info = plan_by_table.get(t_name)
        enc_lbl = tp_info.source_encoding if tp_info is not None else "utf-8"
        lines.append(f"### `{t_name}` ({tb.source.display_name})")
        lines.append(
            f"- **규모**: {tb.source.row_count:,}행 x {tb.source.column_count}컬럼 "
            f"(`{tb.source.source_kind.value}`, 인코딩 `{enc_lbl}`)"
        )
        if pk is not None:
            if pk.recommend_surrogate:
                lines.append(
                    f"- **권고 PK**: 서로게이트 IDENTITY 키 `{pk.surrogate_column_name}` "
                    f"(사유: {'; '.join(pk.reasons)})"
                )
            else:
                pk_label = ", ".join(f"`{c}`" for c in pk.columns)
                lines.append(
                    f"- **권고 PK**: {pk_label} (점수 {pk.score:.1f}, "
                    f"피참조 {pk.referenced_by_count}건 — {'; '.join(pk.reasons)})"
                )
            alt_labels = [", ".join(f"`{c}`" for c in ak) for ak in pk.alternative_keys[:3]]
            if alt_labels:
                lines.append(
                    "- **표본 관측 유일 후보(업무 규칙 확인 후 UNIQUE 검토)**: "
                    + "; ".join(alt_labels)
                )
        lines.extend(
            [
                "",
                "| 컬럼 | 물리 타입 | 분석 유형 | 결측 수(비율) | 고유값 수 | 기수성 | 비고 |",
                "|---|---|---|---:|---:|---:|---|",
            ]
        )
        pattern_cols: list[ColumnClassification] = []
        sensitive_set = set(tp_info.sensitive_columns) if tp_info is not None else set()
        for cls in tb.classifications:
            note_parts: list[str] = []
            if cls.column in sensitive_set:
                note_parts.append("PII 민감 컬럼")
            if cls.index_meta is not None and cls.index_meta.likely_surrogate_index:
                note_parts.append("단조 순번 인덱스")
            if cls.ordinal_order:
                note_parts.append(f"서열: {' < '.join(cls.ordinal_order[:5])}")
            if cls.string_pattern is not None:
                note_parts.append(f"패턴 `{cls.string_pattern.template}`")
                pattern_cols.append(cls)
            lines.append(
                f"| `{cls.column}` | `{cls.physical_type.value}` | `{cls.category.value}` | "
                f"{cls.missing_count} ({cls.missing_rate:.1%}) | {cls.unique_count} | "
                f"{cls.cardinality_ratio:.2f} | {'; '.join(note_parts) or '-'} |"
            )
        lines.append("")

        if pattern_cols:
            lines.append("#### 규칙성 문자열 내부 구조 분해")
            for pcol in pattern_cols:
                sp = pcol.string_pattern
                assert sp is not None
                lines.append(
                    f"- **`{pcol.column}`**: 템플릿 `{sp.template}` "
                    f"(전략 `{sp.strategy}`, 일치율 {sp.coverage:.1%}, 정규식 `{sp.regex_pattern}`)"
                )
                for seg in sp.segments:
                    if seg.kind is StringSegmentKind.LITERAL:
                        lines.append(f"  - [{seg.position}] 고정부: `{seg.literal_value}`")
                    else:
                        cat_v = seg.slot_category.value if seg.slot_category else "-"
                        lines.append(
                            f"  - [{seg.position}] 가변 슬롯 `{seg.slot_name}`: "
                            f"유형 `{cat_v}`, 고유 {seg.unique_count}개, "
                            f"마스크 `{seg.sample_pattern}`, 추출 `{seg.extract_sql_hint}`"
                        )
            lines.append("")

        trel = rel.table_relations.get(t_name)
        if trel is not None and (trel.derivations or trel.hierarchies or trel.correlations):
            lines.append("#### 테이블 내부 관계")
            for d in trel.derivations:
                lines.append(f"- 파생 컬럼: `{d.target}` ← {d.rule_hint}")
            for h in trel.hierarchies:
                lines.append(f"- 계층 관계: `{' → '.join(h.path)}`")
            for corr in trel.correlations[:5]:
                lines.append(
                    f"- 상관관계: `{corr.a}` ↔ `{corr.b}` ({corr.method.value} r={corr.value:.2f})"
                )
            lines.append("")

    # 개인정보(PII) · 민감 컬럼 탐지 요약
    pii_notes = [
        (tp.target_table, note)
        for tp in lp.table_plans
        for note in tp.notes
        if note.startswith("민감(PII) 컬럼")
    ]
    if pii_notes:
        lines.extend(["### 개인정보(PII) · 민감 컬럼 탐지 및 마스킹 권고", ""])
        for tbl_name, note in pii_notes:
            lines.append(f"- `{tbl_name}` — {note}")
        lines.append("")

    # 위상 적재 계획
    lines.extend(
        [
            "## 6. 최적 DB 위상 적재 계획 (Load Plan)",
            "",
            (
                "| 순서 | 대상 테이블 | 원본 인코딩 | 선행 의존 테이블 | PK | 증분 전략 | "
                "워터마크 | 기대 행수 | 민감 컬럼 | 권고 인덱스 수 |"
            ),
            "|---:|---|---|---|---|---|---|---:|---:|---:|",
        ]
    )
    for tp in lp.table_plans:
        deps = ", ".join(f"`{d}`" for d in tp.depends_on) if tp.depends_on else "-"
        pks = ", ".join(f"`{c}`" for c in tp.primary_key) if tp.primary_key else "-"
        wm = f"`{tp.watermark_column}`" if tp.watermark_column else "-"
        lines.append(
            f"| {tp.load_order_index + 1} | `{tp.target_table}` | `{tp.source_encoding}` | "
            f"{deps} | {pks} | `{tp.increment_mode.value}` | {wm} | "
            f"{tp.expected_row_count:,} | {len(tp.sensitive_columns)} | "
            f"{len(tp.recommended_indexes)} |"
        )
    lines.append("")

    # 비-identity 변환 매핑 요약
    non_id_maps = [
        m for tp in lp.table_plans for m in tp.column_mappings if m.transform != "identity"
    ]
    if non_id_maps:
        lines.extend(
            [
                "### 주요 컬럼 변환·추출 규칙",
                "",
                "| 대상 테이블 | 대상 컬럼 | 원본 컬럼 | 변환식 | 캐스팅 타입 | 결측 정책 |",
                "|---|---|---|---|---|---|",
            ]
        )
        for m in non_id_maps:
            lines.append(
                f"| `{m.target_table}` | `{m.target_column}` | `{m.source_column}` | "
                f"`{m.transform}` | `{m.type_cast}` | `{m.null_handling.value}` |"
            )
        lines.append("")

    # PostgreSQL DDL
    full_ddl = lp.ddl_sql.rstrip()
    if lp.index_ddl_sql.strip():
        full_ddl = f"{full_ddl}\n\n-- Recommended Indexes\n{lp.index_ddl_sql.rstrip()}"
    if lp.comment_ddl_sql.strip():
        full_ddl = f"{full_ddl}\n\n-- Column Comments\n{lp.comment_ddl_sql.rstrip()}"
    lines.extend(
        [
            "## 7. 검증 완료 PostgreSQL DDL (`ddl.sql`)",
            "",
            "```sql",
            full_ddl,
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def export_multi_table_bundle(
    bundle: MultiTableAnalysisBundle,
    out_dir: str | Path,
) -> dict[str, Path]:
    """분석 번들을 DDL·적재플랜·관계 JSON·Mermaid ERD·Markdown 리포트로 저장한다."""
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)

    lp = bundle.load_plan
    ddl_content = lp.ddl_sql.rstrip() + "\n"
    if lp.index_ddl_sql.strip():
        ddl_content += "\n-- Recommended Indexes\n" + lp.index_ddl_sql.rstrip() + "\n"
    if lp.comment_ddl_sql.strip():
        ddl_content += "\n-- Column Comments\n" + lp.comment_ddl_sql.rstrip() + "\n"

    report_md = render_analysis_report(bundle)

    files: dict[str, tuple[Path, str]] = {
        "ddl.sql": (target / "ddl.sql", ddl_content),
        "load_plan.json": (target / "load_plan.json", lp.model_dump_json(indent=2) + "\n"),
        "relations.json": (
            target / "relations.json",
            bundle.relations.model_dump_json(indent=2) + "\n",
        ),
        "analysis_bundle.json": (
            target / "analysis_bundle.json",
            bundle.model_dump_json(indent=2) + "\n",
        ),
        "erd.mmd": (target / "erd.mmd", lp.erd_mermaid),
        "analysis_report.md": (target / "analysis_report.md", report_md),
    }

    written: dict[str, Path] = {}
    for key, (path, content) in files.items():
        path.write_text(content, encoding="utf-8")
        written[key] = path
    return written


__all__ = [
    "export_multi_table_bundle",
    "plan_multi_table_load",
    "render_analysis_report",
    "render_mermaid_erd",
]
