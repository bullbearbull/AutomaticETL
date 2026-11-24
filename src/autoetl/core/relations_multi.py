"""다중 테이블 내부·교차 관계 분석 엔진(Milestone E3).

1. 각 테이블 내부 관계: 기존 `s05_relations`(후보 키·근사 FD·상관) + 산술 파생 칼럼·
   범주 계층 구조 탐지를 실행한다.
2. 테이블 간 포함 종속(Cross-Table FK): 단일 DuckDB 연결 안에서 `t0.normalized` ↔
   `t1.normalized` 교차 조인으로 전수 검증한다.
   - 일반 칼럼 FK뿐만 아니라, E2 문자열 규칙 분해 엔진이 찾아낸 **가변 슬롯 ↔ 타 테이블 PK**
     숨은 외래 키까지 자동 발견한다.
   - 소수 정수 우연 충돌(예: `qty` 1~5 vs `customer_id` 1~100)과 양방향 1:1 중복 사이클을
     결정론적 규칙으로 차단한다.
3. 최적 Primary Key 선정: 발견된 타 테이블 FK 피참조 횟수를 반영해 테이블별 PK를 확정한다.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from autoetl.contracts import (
    ColumnRoleCategory,
    CrossTableForeignKey,
    Derivation,
    Hierarchy,
    InclusionDependency,
    MultiTableRelationAnalysis,
    PhysicalType,
    PrimaryKeyRecommendation,
    RelationGraph,
    RelationshipCardinality,
    StringSegmentKind,
    TableProfileBundle,
)
from autoetl.core.context import quote_identifier
from autoetl.core.multi_context import MultiTableContext, TableSlotContext
from autoetl.core.pk_selector import select_primary_key
from autoetl.core.stages import s05_relations

MIN_INCLUSION_RATE = 0.95
"""외래 키(포함 종속) 채택 최소 포함률."""

_TOKEN_SPLIT_RE = re.compile(r"[^a-z0-9가-힣]+", re.IGNORECASE)

_GENERIC_KEY_TOKENS = frozenset({"id", "no", "num", "code", "cd", "key", "pk", "fk", "idx", "seq"})

_MEASUREMENT_NAMES = frozenset(
    {
        "qty",
        "quantity",
        "count",
        "cnt",
        "amount",
        "amt",
        "price",
        "unit_price",
        "total",
        "total_amount",
        "rate",
        "ratio",
        "score",
        "age",
        "rank",
        "level",
        "step",
        "year",
        "month",
        "day",
        "수량",
        "금액",
        "단가",
        "합계",
        "비율",
        "점수",
        "나이",
        "연도",
    }
)

_DISALLOWED_FK_CATEGORIES = frozenset(
    {
        ColumnRoleCategory.BOOLEAN,
        ColumnRoleCategory.CONTINUOUS_NUMERIC,
        ColumnRoleCategory.FREE_TEXT,
        ColumnRoleCategory.TEMPORAL,
    }
)


def _singularize(tok: str) -> str:
    if tok.endswith("ies") and len(tok) > 4:
        return tok[:-3] + "y"
    if tok.endswith("s") and not tok.endswith("ss") and len(tok) > 3:
        return tok[:-1]
    return tok


def _name_tokens(name: str) -> set[str]:
    parts = [p for p in _TOKEN_SPLIT_RE.split(name.lower()) if p]
    return {_singularize(p) for p in parts}


def _semantic_name_similarity(
    child_col: str,
    parent_table: str,
    parent_col: str,
) -> float:
    """자식 칼럼명과 부모 테이블/칼럼명 간 토큰 유사도(0.0~1.0)를 계산한다."""
    c_toks = _name_tokens(child_col)
    p_table_toks = _name_tokens(parent_table)
    p_col_toks = _name_tokens(parent_col)
    if not c_toks:
        return 0.0

    # 정확 이름 일치(단순 'id'/'row_idx' 제외)
    if child_col.lower() == parent_col.lower() and child_col.lower() not in {
        "id",
        "idx",
        "row_idx",
    }:
        return 1.0

    c_domain = c_toks - _GENERIC_KEY_TOKENS
    p_domain = (p_table_toks | p_col_toks) - _GENERIC_KEY_TOKENS
    if c_domain and p_domain:
        overlap = c_domain & p_domain
        if overlap:
            return round(min(1.0, len(overlap) / min(len(c_domain), len(p_domain))), 4)
        # 부분 문자열 접두 일치 (예: cust_id ↔ customers)
        for ct in c_domain:
            for pt in p_domain:
                if len(ct) >= 3 and len(pt) >= 3 and (pt.startswith(ct) or ct.startswith(pt)):
                    return 0.75

    # 부모 테이블명 토큰과 정확 일치 (예: branch ↔ branches)
    if c_toks & p_table_toks:
        return 0.85
    return 0.0


def _discover_derivations(
    slot: TableSlotContext,
    bundle: TableProfileBundle,
) -> list[Derivation]:
    """단일 테이블 내 수치 칼럼 간 사칙 파생 관계(`target ≈ a * b` 또는 `a + b`)를 탐지한다."""
    view = slot.normalized_view
    if view is None or bundle.source.row_count < 3:
        return []

    numeric_cols = [
        (c.column, quote_identifier(slot.column_id_of(idx)))
        for idx, c in enumerate(bundle.classifications)
        if c.physical_type in {PhysicalType.INTEGER, PhysicalType.DECIMAL, PhysicalType.FLOAT}
        and c.category is not ColumnRoleCategory.INDEX
    ]
    if len(numeric_cols) < 3 or len(numeric_cols) > 12:
        return []

    derivations: list[Derivation] = []
    seen_targets: set[str] = set()
    for t_idx, (t_name, t_q) in enumerate(numeric_cols):
        if t_name in seen_targets:
            continue
        found = False
        for i in range(len(numeric_cols)):
            if i == t_idx:
                continue
            for j in range(i + 1, len(numeric_cols)):
                if j == t_idx:
                    continue
                a_name, a_q = numeric_cols[i]
                b_name, b_q = numeric_cols[j]
                row = slot.connection.execute(
                    f"SELECT COUNT(*), "
                    f"SUM(CASE WHEN ABS(CAST({t_q} AS DOUBLE) - "
                    f"(CAST({a_q} AS DOUBLE) * CAST({b_q} AS DOUBLE))) <= 1e-3 THEN 1 ELSE 0 END), "
                    f"SUM(CASE WHEN ABS(CAST({t_q} AS DOUBLE) - "
                    f"(CAST({a_q} AS DOUBLE) + CAST({b_q} AS DOUBLE))) <= 1e-3 THEN 1 ELSE 0 END) "
                    f"FROM {view} "
                    f"WHERE {t_q} IS NOT NULL AND {a_q} IS NOT NULL AND {b_q} IS NOT NULL"
                ).fetchone()
                if row is None or not isinstance(row[0], int) or row[0] < 3:
                    continue
                n = row[0]
                prod_ok = int(row[1]) if isinstance(row[1], int) else 0
                sum_ok = int(row[2]) if isinstance(row[2], int) else 0
                if prod_ok / n >= 0.99:
                    derivations.append(
                        Derivation(
                            target=t_name,
                            sources=[a_name, b_name],
                            rule_hint=f"{t_name} = {a_name} * {b_name}",
                        )
                    )
                    seen_targets.add(t_name)
                    found = True
                    break
                if sum_ok / n >= 0.99:
                    derivations.append(
                        Derivation(
                            target=t_name,
                            sources=[a_name, b_name],
                            rule_hint=f"{t_name} = {a_name} + {b_name}",
                        )
                    )
                    seen_targets.add(t_name)
                    found = True
                    break
            if found:
                break
    return derivations


def _discover_hierarchies(
    bundle: TableProfileBundle,
    rel: RelationGraph,
) -> list[Hierarchy]:
    """정확 FD(`fine -> coarse`)에서 범주·코드 계층 경로(`상위 → 하위`)를 추출한다."""
    cls_by_col = {c.column: c for c in bundle.classifications}
    prof_by_col = {p.column: p for p in bundle.profiles}
    hier_cats = {
        ColumnRoleCategory.CATEGORICAL_NOMINAL,
        ColumnRoleCategory.CATEGORICAL_ORDINAL,
        ColumnRoleCategory.CODE_IDENTIFIER,
    }

    # fine -> coarse (즉, coarse가 상위, fine이 하위)
    parent_of: dict[str, list[str]] = {}
    for fd in rel.functional_dependencies:
        if len(fd.lhs) != 1 or fd.violation_count > 0:
            continue
        fine = fd.lhs[0]
        coarse = fd.rhs
        c_fine = cls_by_col.get(fine)
        c_coarse = cls_by_col.get(coarse)
        p_fine = prof_by_col.get(fine)
        p_coarse = prof_by_col.get(coarse)
        if not (c_fine and c_coarse and p_fine and p_coarse):
            continue
        if c_fine.category not in hier_cats or c_coarse.category not in hier_cats:
            continue
        if p_fine.unique_count > p_coarse.unique_count >= 2:
            parent_of.setdefault(fine, []).append(coarse)

    hierarchies: list[Hierarchy] = []
    seen: set[tuple[str, ...]] = set()
    for fine, parents in sorted(parent_of.items()):
        for coarse in sorted(parents):
            chain = [coarse, fine]
            # 1단계 상위 확장 (grandparent -> coarse -> fine)
            gparents = parent_of.get(coarse, [])
            if gparents and gparents[0] not in chain:
                chain = [gparents[0], *chain]
            tup = tuple(chain)
            if tup not in seen:
                seen.add(tup)
                hierarchies.append(Hierarchy(path=chain))
    return hierarchies


@dataclass(frozen=True)
class _ParentCandidate:
    table_id: str
    table_name: str
    column_name: str
    col_id: str
    physical_type: PhysicalType
    category: ColumnRoleCategory
    unique_count: int
    likely_surrogate_index: bool


@dataclass(frozen=True)
class _ChildCandidate:
    table_id: str
    table_name: str
    column_name: str
    sql_expr: str  # 정규화 뷰(alias `c`) 기준 추출 SQL
    physical_type: PhysicalType
    category: ColumnRoleCategory
    unique_count: int
    is_unique: bool
    slot_name: str | None = None
    slot_extract_hint: str | None = None


def _types_compatible_for_fk(child_phys: PhysicalType, parent_phys: PhysicalType) -> bool:
    if child_phys is parent_phys:
        return True
    # 정수 ID ↔ 숫자 코드 문자열 간 교차 참조 허용
    int_str = {PhysicalType.INTEGER, PhysicalType.STRING}
    return {child_phys, parent_phys} == int_str


def _collect_parent_candidates(
    mctx: MultiTableContext,
    bundles: Sequence[TableProfileBundle],
) -> list[_ParentCandidate]:
    parents: list[_ParentCandidate] = []
    for bundle in bundles:
        slot = mctx.get_by_id(bundle.source.table_id)
        col_pos = {c.name: c.index for c in bundle.ingest.columns}
        for cls in bundle.classifications:
            if cls.category in _DISALLOWED_FK_CATEGORIES:
                continue
            if cls.missing_count > 0 or cls.unique_count < 2:
                continue
            # 부모 키는 유일(100%)이어야 정식 FK 대상
            if cls.cardinality_ratio < 0.999:
                continue
            pos = col_pos[cls.column]
            likely_surr = bool(cls.index_meta is not None and cls.index_meta.likely_surrogate_index)
            parents.append(
                _ParentCandidate(
                    table_id=bundle.source.table_id,
                    table_name=bundle.source.table_name,
                    column_name=cls.column,
                    col_id=slot.column_id_of(pos),
                    physical_type=cls.physical_type,
                    category=cls.category,
                    unique_count=cls.unique_count,
                    likely_surrogate_index=likely_surr,
                )
            )
    return parents


def _collect_child_candidates(
    mctx: MultiTableContext,
    bundles: Sequence[TableProfileBundle],
) -> list[_ChildCandidate]:
    children: list[_ChildCandidate] = []
    for bundle in bundles:
        slot = mctx.get_by_id(bundle.source.table_id)
        col_pos = {c.name: c.index for c in bundle.ingest.columns}
        for cls in bundle.classifications:
            pos = col_pos[cls.column]
            qcol = quote_identifier(slot.column_id_of(pos))

            # 1. 직접 칼럼 후보
            if cls.category not in _DISALLOWED_FK_CATEGORIES and cls.unique_count >= 1:
                children.append(
                    _ChildCandidate(
                        table_id=bundle.source.table_id,
                        table_name=bundle.source.table_name,
                        column_name=cls.column,
                        sql_expr=f"c.{qcol}",
                        physical_type=cls.physical_type,
                        category=cls.category,
                        unique_count=cls.unique_count,
                        is_unique=(cls.missing_count == 0 and cls.cardinality_ratio >= 0.999),
                    )
                )

            # 2. 규칙성 문자열 칼럼의 가변 슬롯 후보 (숨은 FK 탐지!)
            if cls.string_pattern is not None:
                rx_escaped = cls.string_pattern.regex_pattern.replace("'", "''")
                for seg in cls.string_pattern.segments:
                    if (
                        seg.kind is not StringSegmentKind.SLOT
                        or seg.slot_name is None
                        or seg.capture_group is None
                        or seg.slot_category in _DISALLOWED_FK_CATEGORIES
                        or seg.slot_category is None
                        or seg.slot_physical_type is None
                        or seg.unique_count < 2
                    ):
                        continue
                    slot_sql = (
                        f"NULLIF(regexp_extract(CAST(c.{qcol} AS VARCHAR), "
                        f"'{rx_escaped}', {seg.capture_group}), '')"
                    )
                    children.append(
                        _ChildCandidate(
                            table_id=bundle.source.table_id,
                            table_name=bundle.source.table_name,
                            column_name=cls.column,
                            sql_expr=slot_sql,
                            physical_type=seg.slot_physical_type,
                            category=seg.slot_category,
                            unique_count=seg.unique_count,
                            is_unique=False,
                            slot_name=seg.slot_name,
                            slot_extract_hint=seg.extract_sql_hint,
                        )
                    )
    return children


def _discover_cross_table_fks(
    mctx: MultiTableContext,
    bundles: Sequence[TableProfileBundle],
    *,
    min_inclusion_rate: float = MIN_INCLUSION_RATE,
) -> list[CrossTableForeignKey]:
    """모든 테이블 쌍에서 DuckDB 전수 포함 종속을 계산해 Cross-Table FK 목록을 반환한다."""
    assert mctx.connection is not None
    if len(bundles) < 2:
        return []

    parents = _collect_parent_candidates(mctx, bundles)
    children = _collect_child_candidates(mctx, bundles)

    raw_fks: list[CrossTableForeignKey] = []

    for child in children:
        for parent in parents:
            if child.table_id == parent.table_id:
                continue
            if not _types_compatible_for_fk(child.physical_type, parent.physical_type):
                continue
            # 자식의 고유값 수가 부모보다 지나치게 많으면 포함률 95% 불가능(조기 프루닝)
            if child.unique_count > max(parent.unique_count * 1.15, parent.unique_count + 2):
                continue

            name_sim = _semantic_name_similarity(
                child.column_name if child.slot_name is None else child.slot_name,
                parent.table_name,
                parent.column_name,
            )

            # 측정량 이름(qty, count, age 등)이 이름 유사도 없이
            # 정수 시퀀스 PK에 우연히 겹치는 오탐 차단
            child_low = child.column_name.lower()
            if (
                child.slot_name is None
                and child.category is ColumnRoleCategory.DISCRETE_NUMERIC
                and name_sim == 0.0
                and (
                    child_low in _MEASUREMENT_NAMES
                    or any(tok in _MEASUREMENT_NAMES for tok in _name_tokens(child_low))
                    or parent.likely_surrogate_index
                )
            ):
                continue

            # 서로 다른 테이블의 범용 행번호(`row_idx`, `id`)끼리 이름 근거 없이 1:1 오탐 차단
            if (
                child.is_unique
                and child.slot_name is None
                and child.category is ColumnRoleCategory.INDEX
                and parent.likely_surrogate_index
                and name_sim == 0.0
            ):
                continue

            # 규칙 문자열의 순번/연도/숫자 슬롯(seq, year 등)이 이름 근거 없이
            # 타 테이블 정수 PK(1..N)에 우연히 겹치는 오탐 차단
            if (
                child.slot_name is not None
                and name_sim == 0.0
                and (
                    child.category
                    in {
                        ColumnRoleCategory.INDEX,
                        ColumnRoleCategory.DISCRETE_NUMERIC,
                        ColumnRoleCategory.TEMPORAL,
                    }
                    or child.slot_name in {"seq", "num", "year", "month", "day", "date", "ym"}
                )
                and (parent.likely_surrogate_index or parent.physical_type is PhysicalType.INTEGER)
            ):
                continue

            p_qcol = quote_identifier(parent.col_id)
            row = mctx.connection.execute(
                f"WITH child_vals AS ("
                f"  SELECT CAST({child.sql_expr} AS VARCHAR) AS v "
                f"  FROM {child.table_id}.normalized AS c "
                f"  WHERE {child.sql_expr} IS NOT NULL "
                f"    AND CAST({child.sql_expr} AS VARCHAR) <> ''"
                f"), parent_vals AS ("
                f"  SELECT DISTINCT CAST(p.{p_qcol} AS VARCHAR) AS v "
                f"  FROM {parent.table_id}.normalized AS p "
                f"  WHERE p.{p_qcol} IS NOT NULL "
                f"    AND CAST(p.{p_qcol} AS VARCHAR) <> ''"
                f") "
                f"SELECT "
                f"  COUNT(*) AS total_rows, "
                f"  COALESCE(SUM(CASE WHEN pv.v IS NOT NULL "
                f"    THEN 1 ELSE 0 END), 0) AS matched_rows, "
                f"  COUNT(DISTINCT CASE WHEN pv.v IS NOT NULL THEN cv.v END) AS matched_distinct, "
                f"  (SELECT COUNT(*) FROM parent_vals) AS parent_distinct, "
                f"  COUNT(DISTINCT cv.v) AS child_distinct "
                f"FROM child_vals AS cv "
                f"LEFT JOIN parent_vals AS pv ON cv.v = pv.v"
            ).fetchone()
            if row is None or not isinstance(row[0], int) or row[0] == 0:
                continue

            total_rows = row[0]
            matched_rows = int(row[1]) if isinstance(row[1], int) else 0
            matched_distinct = int(row[2]) if isinstance(row[2], int) else 0
            parent_distinct = int(row[3]) if isinstance(row[3], int) else 0
            child_distinct = int(row[4]) if isinstance(row[4], int) else 0
            if parent_distinct == 0 or child_distinct == 0:
                continue

            inclusion_rate = matched_rows / total_rows
            if inclusion_rate < min_inclusion_rate:
                continue

            parent_coverage = min(1.0, matched_distinct / parent_distinct)
            orphan_rows = total_rows - matched_rows

            # 문자열 슬롯 FK는 최소 2개 이상 서로 다른 부모 키와 매칭되어야 우연 일치 방지
            if child.slot_name is not None and matched_distinct < 2:
                continue

            # 정수-문자열 혼합이면서 이름 근거 없고 커버리지가 매우 낮으면 제외
            if (
                child.physical_type is not parent.physical_type
                and name_sim == 0.0
                and parent_coverage < 0.5
            ):
                continue

            cardinality = (
                RelationshipCardinality.ONE_TO_ONE
                if child.is_unique
                else RelationshipCardinality.MANY_TO_ONE
            )

            sem_bonus = (
                0.10
                if (
                    child.physical_type is parent.physical_type
                    and parent.category
                    in {
                        ColumnRoleCategory.INDEX,
                        ColumnRoleCategory.CODE_IDENTIFIER,
                        ColumnRoleCategory.STRUCTURED_STRING,
                    }
                )
                else 0.04
            )
            confidence = round(
                min(
                    1.0,
                    0.50 * inclusion_rate + 0.25 * name_sim + 0.15 * parent_coverage + sem_bonus,
                ),
                4,
            )

            # 이름 유사도가 0인 경우엔 포함률 100% + 부모 커버리지 ≥ 0.4 이상일 때만 채택
            if name_sim == 0.0 and (inclusion_rate < 1.0 or parent_coverage < 0.4):
                continue

            reasons = [
                f"포함률 {inclusion_rate:.1%} ({matched_rows}/{total_rows}행 일치)",
                f"부모 키 커버리지 {parent_coverage:.1%} ({matched_distinct}/{parent_distinct}개)",
            ]
            if child.slot_name is not None:
                reasons.append(
                    f"규칙 문자열 `{child.column_name}`의 슬롯 `{child.slot_name}`에서 숨은 FK 발견"
                )
            if name_sim > 0.0:
                reasons.append(f"이름 유사도 {name_sim:.2f}")
            if orphan_rows > 0:
                reasons.append(f"고아(미매칭) 행 {orphan_rows}건")

            raw_fks.append(
                CrossTableForeignKey(
                    child_table=child.table_name,
                    child_columns=[child.column_name],
                    child_slot_name=child.slot_name,
                    child_extract_sql=child.slot_extract_hint,
                    parent_table=parent.table_name,
                    parent_columns=[parent.column_name],
                    cardinality=cardinality,
                    inclusion_rate=round(inclusion_rate, 6),
                    parent_coverage=round(parent_coverage, 6),
                    matched_rows=matched_rows,
                    orphan_rows=orphan_rows,
                    confidence=confidence,
                    reasons=reasons,
                )
            )

    # 1. 동일 (child_table, child_columns, child_slot_name)당 최고 신뢰도 부모만 채택
    best_by_child: dict[tuple[str, tuple[str, ...], str | None], CrossTableForeignKey] = {}
    for fk in sorted(
        raw_fks,
        key=lambda f: (
            f.confidence,
            f.inclusion_rate,
            f.parent_coverage,
            -len(f.parent_table),
            f.parent_table,
        ),
        reverse=True,
    ):
        ckey = (fk.child_table, tuple(fk.child_columns), fk.child_slot_name)
        if ckey not in best_by_child:
            best_by_child[ckey] = fk

    # 2. 이미 동일 (child_table, parent_table, parent_columns)에 직접 FK가 있으면
    #    중복되는 문자열 슬롯 FK는 생략한다.
    direct_targets = {
        (fk.child_table, fk.parent_table, tuple(fk.parent_columns))
        for fk in best_by_child.values()
        if fk.child_slot_name is None
    }
    selected = [
        fk
        for fk in best_by_child.values()
        if fk.child_slot_name is None
        or (fk.child_table, fk.parent_table, tuple(fk.parent_columns)) not in direct_targets
    ]

    # 3. 상호 1:1 대칭 쌍(A→B 와 B→A 동시 존재) 중 신뢰도 높은 방향 하나만 유지(순환 방지)
    #    동률이면 더 짧은 기준 테이블명(예: employees)이 부모가 되도록 정렬
    selected.sort(
        key=lambda f: (
            f.confidence,
            f.inclusion_rate,
            f.parent_coverage,
            -len(f.parent_table),
            f.child_table,
            f.parent_table,
        ),
        reverse=True,
    )
    final_fks: list[CrossTableForeignKey] = []
    seen_pairs: set[tuple[str, str]] = set()
    for fk in selected:
        if (fk.parent_table, fk.child_table) in seen_pairs and (
            fk.cardinality is RelationshipCardinality.ONE_TO_ONE
        ):
            continue
        seen_pairs.add((fk.child_table, fk.parent_table))
        final_fks.append(fk)

    return final_fks


def analyze_multi_table_relations(
    mctx: MultiTableContext,
    bundles: Sequence[TableProfileBundle],
    *,
    min_inclusion_rate: float = MIN_INCLUSION_RATE,
) -> MultiTableRelationAnalysis:
    """E3 통합 실행: 테이블 내부 FD/상관/파생/계층 + 테이블 간 FK + 최적 PK 선정."""
    table_relations: dict[str, RelationGraph] = {}
    notes: list[str] = []

    # 1. 테이블별 내부 관계 분석 (기존 S5 + 파생/계층 확장)
    for bundle in bundles:
        slot = mctx.activate(bundle.source.table_id)
        rel = s05_relations.run(slot, bundle.ingest, bundle.types, bundle.profiles)
        derivations = _discover_derivations(slot, bundle)
        hierarchies = _discover_hierarchies(bundle, rel)
        enriched = rel.model_copy(
            update={
                "derivations": derivations,
                "hierarchies": hierarchies,
            }
        )
        slot.stage_artifacts["s05_relations"] = enriched
        table_relations[bundle.source.table_name] = enriched

    # 2. 다중 테이블 간 포함 종속(FK) 탐색
    foreign_keys = _discover_cross_table_fks(mctx, bundles, min_inclusion_rate=min_inclusion_rate)

    # 각 테이블의 RelationGraph.inclusion_dependencies에도 자기 테이블 발신 FK 반영
    fks_by_child: dict[str, list[InclusionDependency]] = {}
    for fk in foreign_keys:
        fks_by_child.setdefault(fk.child_table, []).append(
            InclusionDependency(
                child_columns=list(fk.child_columns),
                parent_columns=list(fk.parent_columns),
                inclusion_rate=fk.inclusion_rate,
                parent_table=fk.parent_table,
            )
        )
    for t_name, inc_list in fks_by_child.items():
        if t_name in table_relations:
            table_relations[t_name] = table_relations[t_name].model_copy(
                update={"inclusion_dependencies": inc_list}
            )

    # 3. 타 테이블 FK 피참조 횟수를 반영한 최적 Primary Key 선정
    primary_keys: dict[str, PrimaryKeyRecommendation] = {}
    for bundle in bundles:
        t_name = bundle.source.table_name
        rel = table_relations[t_name]
        primary_keys[t_name] = select_primary_key(bundle, rel.candidate_keys, foreign_keys)

    if len(bundles) == 1:
        notes.append("단일 테이블 입력이라 테이블 간 FK 탐색은 생략되었다.")

    return MultiTableRelationAnalysis(
        table_relations=table_relations,
        primary_keys=primary_keys,
        foreign_keys=foreign_keys,
        notes=notes,
    )


__all__ = ["MIN_INCLUSION_RATE", "analyze_multi_table_relations"]
