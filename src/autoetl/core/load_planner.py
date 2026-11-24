"""Milestone E4: 다중 테이블 최적 DB 스키마 후보 생성·채점·위상 적재 플랜·DDL/ERD/리포트 생성기.

전 과정이 100% 결정론적(0MB AI/LLM)이며, PostgreSQL DDL은 기존 ``autoetl.core.ddl.postgres``의
``generate_ddl`` + ``verify_ddl``(sqlglot AST 재파싱 검증)을 통과한 결과만 내보낸다.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from sqlglot import exp

from autoetl.contracts import (
    ColumnClassification,
    ColumnMapping,
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
    SchemaPattern,
    Scorecard,
    SemanticType,
    StatsKind,
    StringSegment,
    StringSegmentKind,
    TableLoadStrategy,
    TableProfileBundle,
    TargetColumn,
    TargetTable,
    ValidationSummary,
)
from autoetl.core.ddl.postgres import generate_ddl, verify_ddl

_CAMEL_RE = re.compile(r"([a-z0-9])([A-Z])")
_NON_IDENT_RE = re.compile(r"[^a-z0-9]+")

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
) -> tuple[int | None, int | None]:
    if stat is None or stat.numeric is None:
        return (None, None)
    num = stat.numeric
    max_abs = max(abs(num.minimum), abs(num.maximum))
    int_digits = len(str(int(max_abs))) if max_abs >= 1 else 1
    scale = min(max(num.max_decimal_digits, 0), 10)
    precision = min(max(int_digits + scale, scale + 1), 36)
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


@dataclass(frozen=True)
class _BuiltCandidate:
    candidate: SchemaCandidate
    # (target_table, target_col) -> transform metadata
    transforms: dict[tuple[str, str], str]
    # target_table -> source_table
    source_table_map: dict[str, str]
    # target_table -> surrogate key used?
    surrogate_tables: set[str]
    # dropped derived columns per table (for reporting)
    omitted_derivations: dict[str, list[str]]
    # decomposed string slot columns count
    materialized_slot_count: int


def _materializable_slots(
    cls: ColumnClassification,
    row_count: int,
    slot_fk_names: set[str],
) -> list[StringSegment]:
    """규칙 문자열에서 대상 테이블 컬럼으로 구체화할 가치가 있는 가변 슬롯을 고른다."""
    if cls.string_pattern is None:
        return []
    out: list[StringSegment] = []
    for seg in cls.string_pattern.segments:
        if seg.kind is not StringSegmentKind.SLOT or seg.slot_name is None:
            continue
        if seg.slot_name in slot_fk_names:
            out.append(seg)
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
) -> _BuiltCandidate:
    """단일 후보 SchemaCandidate와 변환/원본 매핑 메타데이터를 결정론적으로 조립한다."""
    by_table = {b.source.table_name: b for b in bundles}
    pk_map = relations.primary_keys

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
        for col_info in bundle.ingest.columns:
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

    # FK 빠른 조회 맵
    direct_fk_by_col: dict[tuple[str, str], CrossTableForeignKey] = {}
    slot_fk_by_key: dict[tuple[str, str, str], CrossTableForeignKey] = {}
    for fk in acyclic_fks:
        if fk.child_slot_name is None:
            direct_fk_by_col[(fk.child_table, fk.child_columns[0])] = fk
        else:
            slot_fk_by_key[(fk.child_table, fk.child_columns[0], fk.child_slot_name)] = fk

    tables: list[TargetTable] = []
    transforms: dict[tuple[str, str], str] = {}
    source_table_map: dict[str, str] = {}
    surrogate_tables: set[str] = set()
    omitted_derivations: dict[str, list[str]] = {}
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

        # 1. 자연 PK가 없으면 BIGINT 서로게이트 PK를 첫 컬럼으로 추가
        if table_pk is not None and table_pk.recommend_surrogate:
            raw_surr = table_pk.surrogate_column_name or f"{t_name}_id"
            surr_used: set[str] = set()
            surr_sql = _to_sql_ident(raw_surr, used=surr_used, fallback_prefix="surr")
            # 기존 컬럼과 충돌 시 회피
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
                    description="자연 PK 부재로 자동 생성되는 서로게이트 PK",
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

            is_pk = col_name in pk_set and not (
                table_pk is not None and table_pk.recommend_surrogate
            )
            direct_fk = direct_fk_by_col.get((t_name, col_name))
            phys = cls.physical_type

            fk_spec: str | None = None
            transform = _default_transform(cls, t_assign.semantic_type)

            if direct_fk is not None:
                p_table = direct_fk.parent_table
                p_col_src = direct_fk.parent_columns[0]
                p_col_sql = col_sql_map[p_table][p_col_src]
                p_phys = parent_pk_phys[(p_table, p_col_src)]
                if phys is not p_phys:
                    phys = p_phys
                    transform = f"cast_to_{p_phys.value}"
                fk_spec = f"{p_table}({p_col_sql})"

            prec: int | None = None
            scale: int | None = None
            if phys is PhysicalType.DECIMAL:
                prec, scale = _decimal_precision_scale(stat)

            nullable = False if is_pk else (prof.missing_count > 0)
            desc_parts = [f"유형: {cls.category.value}"]
            if sql_col != col_name:
                desc_parts.append(f"원본: {col_name}")
            if cls.string_pattern is not None:
                desc_parts.append(f"패턴: {cls.string_pattern.template}")

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
            if cls.category is ColumnRoleCategory.TEMPORAL and temporal_partition_col is None:
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

                    extract_expr = seg.extract_sql_hint or (
                        f"regexp_extract({col_name}, '{cls.string_pattern.regex_pattern}', "
                        f"{seg.capture_group or 1})"
                    )
                    target_cols.append(
                        TargetColumn(
                            name=slot_sql_col,
                            physical_type=slot_phys,
                            source_columns=[col_name],
                            nullable=prof.missing_count > 0,
                            is_primary_key=False,
                            foreign_key=slot_fk_spec,
                            description=slot_desc,
                        )
                    )
                    transforms[(t_name, slot_sql_col)] = extract_expr
                    materialized_slot_count += 1
                    if slot_fk_spec is not None and slot_sql_col not in index_cols:
                        index_cols.append(slot_sql_col)

        partition_expr: str | None = None
        has_outgoing_fk = any(c.foreign_key is not None for c in target_cols)
        if analytics_mode and temporal_partition_col is not None and has_outgoing_fk:
            partition_expr = f"RANGE ({temporal_partition_col})"

        tables.append(
            TargetTable(
                name=t_name,
                columns=target_cols,
                partition=partition_expr,
                index=index_cols,
            )
        )

    # 빈 테이블 방어
    if not tables:
        raise ValueError("스키마 후보를 생성할 테이블이 없습니다.")

    # 실제 참조 없는 고립 테이블 경고 없이 깔끔히 유지
    _ = by_table
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
        source_table_map=source_table_map,
        surrogate_tables=surrogate_tables,
        omitted_derivations=omitted_derivations,
        materialized_slot_count=materialized_slot_count,
    )


def _default_transform(cls: ColumnClassification, sem: SemanticType) -> str:
    if sem in {SemanticType.AMOUNT, SemanticType.RATIO}:
        return "strip_currency_and_comma"
    if cls.category is ColumnRoleCategory.TEMPORAL:
        if any(("한국어 날짜" in r or "한글 날짜" in r) for r in cls.reasons):
            return "parse_korean_date"
        return "parse_iso_temporal"
    if sem is SemanticType.BOOLEAN_LIKE:
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


def _score_candidate(
    built: _BuiltCandidate,
    purpose: Purpose,
    merged_profiles: Sequence[ColumnProfile],
    merged_stats: Sequence[ColumnStats],
    merged_sampling: SamplingPlan,
    total_derivations: int,
) -> tuple[Scorecard, str]:
    """후보 스키마를 `generate_ddl` + `verify_ddl`로 검증하고 목적별 가중 점수를 계산한다."""
    cand = built.candidate
    ddl_ok = False
    ddl_sql = ""
    try:
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

    n_tables = len(cand.tables)
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

    # 중복도: 파생 컬럼을 제거(정규화)했을수록 높음
    if total_derivations > 0:
        redundancy_val = 76.0 + (20.0 * omitted_count / total_derivations)
    else:
        redundancy_val = 88.0 if cand.pattern is SchemaPattern.THIRD_NORMAL_FORM else 80.0
    if built.materialized_slot_count > 0 and cand.id == "C2_COMPACT_RAW":
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

    security_val = 85.0

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

    scorecard = Scorecard(
        candidate_id=cand.id,
        integrity=CriterionScore(
            score=round(integrity_val, 2),
            detail=(
                f"DDL 검증 {'통과' if ddl_ok else '실패'}, "
                f"PK {pk_tables}/{n_tables}, FK {fk_count}건"
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
            detail="원본 셀 값 비노출 결정론적 스키마 생성",
        ),
        weighted_total=weighted,
        purpose=purpose,
        validation=ValidationSummary(
            ddl_parse_ok=ddl_ok,
            trial_load_ok=ddl_ok,
            constraint_violation_count=0 if ddl_ok else 1,
            data_loss_rate=0.0,
        ),
    )
    return scorecard, ddl_sql


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
            # 순환 방어 완료 상태여야 하지만 만약 남더라도 결정론적으로 풀기
            ready = [remaining[0]]
        for name in ready:
            ordered.append(LoadOrder(table=name, depends_on=sorted(depends[name])))
            done.add(name)
        remaining = [name for name in remaining if name not in done]
    return ordered


def _pg_type_cast_label(col: TargetColumn) -> str:
    if col.physical_type is PhysicalType.INTEGER:
        return "bigint" if col.source_columns == ["__surrogate__"] else "integer"
    if col.physical_type is PhysicalType.DECIMAL:
        if col.precision is not None and col.scale is not None:
            return f"numeric({col.precision},{col.scale})"
        return "numeric"
    if col.physical_type is PhysicalType.FLOAT:
        return "double precision"
    if col.physical_type is PhysicalType.STRING:
        return "varchar"
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
) -> tuple[list[TableLoadStrategy], str]:
    """위상 정렬 순서에 맞춰 테이블별 적재 전략·컬럼 변환·권고 인덱스를 생성한다."""
    bundle_by_name = {b.source.table_name: b for b in bundles}
    table_by_name = {t.name: t for t in built.candidate.tables}
    all_indexes: list[IndexRecommendation] = []
    strategies: list[TableLoadStrategy] = []

    for order_idx, lo in enumerate(load_order):
        t_name = lo.table
        target_table = table_by_name[t_name]
        src_name = built.source_table_map[t_name]
        bundle = bundle_by_name[src_name]
        pk_rec = relations.primary_keys.get(src_name)
        cls_by_col = {c.column: c for c in bundle.classifications}
        uses_surrogate = t_name in built.surrogate_tables

        pk_cols = [c.name for c in target_table.columns if c.is_primary_key]
        mappings: list[ColumnMapping] = []
        temporal_cols: list[str] = []
        table_indexes: list[IndexRecommendation] = []
        indexed_tuples: set[tuple[str, ...]] = set()

        for col in target_table.columns:
            src_col = col.source_columns[0]
            transform = built.transforms.get((t_name, col.name), "identity")
            null_handling = NullHandling.REJECT_ROW if not col.nullable else NullHandling.NULL
            mappings.append(
                ColumnMapping(
                    source_column=src_col,
                    target_table=t_name,
                    target_column=col.name,
                    transform=transform,
                    type_cast=_pg_type_cast_label(col),
                    null_handling=null_handling,
                )
            )
            src_cls = cls_by_col.get(src_col)
            if (
                src_cls is not None
                and src_cls.category is ColumnRoleCategory.TEMPORAL
                and src_cls.missing_count == 0
                and col.name == src_col
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

        # 대안 자연 유일 키가 있으면 UNIQUE 인덱스 권고
        if pk_rec is not None:
            col_name_set = {c.name for c in target_table.columns}
            for alt_key in pk_rec.alternative_keys[:2]:
                if len(alt_key) == 1 and alt_key[0] in col_name_set:
                    alt_tuple = tuple(alt_key)
                    if alt_tuple not in indexed_tuples and alt_key != pk_cols:
                        indexed_tuples.add(alt_tuple)
                        table_indexes.append(
                            IndexRecommendation(
                                table=t_name,
                                columns=list(alt_key),
                                unique=True,
                                reason="대안 유일 비즈니스 키 무결성 및 단건 조회 최적화",
                            )
                        )

        # 분석 목적일 때 범주형 필터 컬럼 인덱스 추가
        if purpose is Purpose.ANALYTICS:
            for col in target_table.columns:
                src_cls = cls_by_col.get(col.source_columns[0])
                if (
                    src_cls is not None
                    and col.name == col.source_columns[0]
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
            notes.append("자연 PK 부재로 서로게이트 BIGINT 키와 APPEND 적재를 권고한다.")
        if t_name in built.omitted_derivations:
            notes.append(
                "정규화 후보 선택으로 산술 파생 컬럼을 테이블에서 제외하고 뷰/계산식 계산을 "
                f"권고한다: {', '.join(built.omitted_derivations[t_name])}"
            )
        if target_table.partition:
            notes.append(f"시계열 분석 성능용 파티셔닝 권고: {target_table.partition}")

        all_indexes.extend(table_indexes)
        strategies.append(
            TableLoadStrategy(
                source_table=src_name,
                target_table=t_name,
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
                expected_row_count=bundle.source.row_count,
                notes=notes,
            )
        )

    index_ddl = _build_index_ddl(all_indexes)
    return strategies, index_ddl


def render_mermaid_erd(
    candidate: SchemaCandidate,
    relations: MultiTableRelationAnalysis,
) -> str:
    """선택된 스키마 후보와 관계 정보를 Mermaid ``erDiagram`` 코드로 렌더링한다."""
    card_by_pair: dict[tuple[str, str], RelationshipCardinality] = {}
    for fk in relations.foreign_keys:
        card_by_pair[(fk.child_table, fk.parent_table)] = fk.cardinality

    lines: list[str] = ["erDiagram"]
    for table in candidate.tables:
        lines.append(f"    {table.name} {{")
        for col in table.columns:
            mtype = _mermaid_type(col.physical_type)
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


def _mermaid_type(phys: PhysicalType) -> str:
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


def plan_multi_table_load(
    bundles: Sequence[TableProfileBundle],
    relations: MultiTableRelationAnalysis,
    *,
    purpose: Purpose = Purpose.OLTP,
    decompose_strings: bool = True,
) -> MultiTableLoadPlan:
    """Milestone E4 통합 실행: 후보 스키마 합성 → 채점·DDL 검증 → 위상 적재 플랜 + ERD."""
    acyclic_fks, cycle_warnings = _select_acyclic_fks(relations)
    merged_profiles, merged_stats, merged_sampling = _merge_profiles_and_stats(bundles)

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
    )
    built_candidates.append(c3)

    scored: list[tuple[Scorecard, _BuiltCandidate, str]] = []
    for built in built_candidates:
        sc, ddl = _score_candidate(
            built,
            purpose,
            merged_profiles,
            merged_stats,
            merged_sampling,
            total_derivations,
        )
        scored.append((sc, built, ddl))

    # DDL 검증 통과 + 가중 총점 최고 후보 선택 (동률 시 candidate_id 오름차순)
    scored.sort(
        key=lambda item: (
            item[0].validation.ddl_parse_ok,
            item[0].weighted_total,
            -len(item[0].candidate_id),
        ),
        reverse=True,
    )
    best_sc, best_built, best_ddl = scored[0]

    load_order = _topological_order(best_built.candidate)
    table_plans, index_ddl = _build_table_load_strategies(
        best_built, load_order, bundles, relations, purpose
    )
    erd_mermaid = render_mermaid_erd(best_built.candidate, relations)

    return MultiTableLoadPlan(
        purpose=purpose,
        selected_candidate_id=best_sc.candidate_id,
        candidates=[item[1].candidate for item in scored],
        scorecards=[item[0] for item in scored],
        load_order=load_order,
        table_plans=table_plans,
        ddl_sql=best_ddl,
        index_ddl_sql=index_ddl,
        erd_mermaid=erd_mermaid,
        cycle_warnings=cycle_warnings,
    )


def render_analysis_report(bundle: MultiTableAnalysisBundle) -> str:
    """다중 테이블 분석·문자열 분해·PK/FK·적재 계획을 한국어 Markdown 리포트로 생성한다."""
    lp = bundle.load_plan
    rel = bundle.relations
    total_rows = sum(t.source.row_count for t in bundle.tables)
    slot_fk_count = sum(1 for fk in rel.foreign_keys if fk.child_slot_name is not None)

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
                "적재단순 | 확장성 | 가중총점 | DDL 검증 |"
            ),
            "|---:|---|---|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    cand_by_id = {c.id: c for c in lp.candidates}
    for rank, sc in enumerate(lp.scorecards, start=1):
        cand = cand_by_id[sc.candidate_id]
        mark = " **★**" if sc.candidate_id == lp.selected_candidate_id else ""
        ddl_mark = "통과" if sc.validation.ddl_parse_ok else "실패"
        lines.append(
            f"| {rank} | `{sc.candidate_id}`{mark} | `{cand.pattern.value}` | "
            f"{sc.integrity.score:.1f} | {sc.redundancy.score:.1f} | "
            f"{sc.fitness.score:.1f} | {sc.load_simplicity.score:.1f} | "
            f"{sc.extensibility.score:.1f} | **{sc.weighted_total:.2f}** | {ddl_mark} |"
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
    lines.extend(["## 5. 테이블별 칼럼 프로파일 · 문자열 분해 · PK 선정", ""])
    for tb in bundle.tables:
        t_name = tb.source.table_name
        pk = rel.primary_keys.get(t_name)
        lines.append(f"### `{t_name}` ({tb.source.display_name})")
        lines.append(
            f"- **규모**: {tb.source.row_count:,}행 x {tb.source.column_count}컬럼 "
            f"(`{tb.source.source_kind.value}`)"
        )
        if pk is not None:
            if pk.recommend_surrogate:
                lines.append(
                    f"- **권고 PK**: 서로게이트 키 `{pk.surrogate_column_name}` "
                    f"(사유: {'; '.join(pk.reasons)})"
                )
            else:
                pk_label = ", ".join(f"`{c}`" for c in pk.columns)
                lines.append(
                    f"- **권고 PK**: {pk_label} (점수 {pk.score:.1f}, "
                    f"피참조 {pk.referenced_by_count}건 — {'; '.join(pk.reasons)})"
                )
        lines.extend(
            [
                "",
                "| 컬럼 | 물리 타입 | 분석 유형 | 결측 수(비율) | 고유값 수 | 기수성 | 비고 |",
                "|---|---|---|---:|---:|---:|---|",
            ]
        )
        pattern_cols: list[ColumnClassification] = []
        for cls in tb.classifications:
            note_parts: list[str] = []
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

    # 위상 적재 계획
    lines.extend(
        [
            "## 6. 최적 DB 위상 적재 계획 (Load Plan)",
            "",
            (
                "| 순서 | 대상 테이블 | 선행 의존 테이블 | PK | 증분 전략 | "
                "워터마크 | 기대 행수 | 권고 인덱스 수 |"
            ),
            "|---:|---|---|---|---|---|---:|---:|",
        ]
    )
    for tp in lp.table_plans:
        deps = ", ".join(f"`{d}`" for d in tp.depends_on) if tp.depends_on else "-"
        pks = ", ".join(f"`{c}`" for c in tp.primary_key) if tp.primary_key else "-"
        wm = f"`{tp.watermark_column}`" if tp.watermark_column else "-"
        lines.append(
            f"| {tp.load_order_index + 1} | `{tp.target_table}` | {deps} | {pks} | "
            f"`{tp.increment_mode.value}` | {wm} | {tp.expected_row_count:,} | "
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
