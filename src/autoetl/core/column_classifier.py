"""칼럼 정밀 유형 분류기(Milestone E2).

프로파일(S2)·타입(S3)·통계(S4)·DuckDB 단조증가 검사·서열 범주 사전·
문자열 내부 규칙 분해(`analyze_string_pattern`)를 결합해 각 칼럼을
인덱스·이산수치·연속수치·명목/서열 범주·날짜·규칙문자열·코드·자유텍스트로 정밀 분류한다.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from autoetl.contracts import (
    ColumnClassification,
    ColumnProfile,
    ColumnRoleCategory,
    ColumnStats,
    IndexMetadata,
    IngestManifest,
    PhysicalType,
    SemanticType,
    StringPatternDecomposition,
    TypeAssignment,
)
from autoetl.core.context import RAW_TABLE, AnalysisContext, quote_identifier
from autoetl.core.stages.s02_profile import DEFAULT_MISSING_MARKERS
from autoetl.core.string_pattern import analyze_string_pattern

_SAMPLE_LIMIT = 2000

_INDEX_NAME_RE = re.compile(
    r"(^|_)(id|idx|index|seq|no|num|pk|rownum|row_num|순번|번호|연번)$",
    re.IGNORECASE,
)

# 알려진 서열 범주 사전(낮음 → 높음 순서)
_ORDINAL_DICTS: tuple[tuple[str, ...], ...] = (
    ("매우 낮음", "낮음", "보통", "높음", "매우 높음"),
    ("최하", "하", "중", "상", "최상"),
    ("하", "중", "상"),
    ("xs", "s", "m", "l", "xl", "xxl"),
    ("very low", "low", "medium", "high", "very high"),
    ("low", "medium", "high"),
    ("bronze", "silver", "gold", "platinum", "diamond", "vip"),
    ("일반", "실버", "골드", "플래티넘", "vip", "vvip"),
    ("q1", "q2", "q3", "q4"),
    ("1분기", "2분기", "3분기", "4분기"),
    ("f", "d", "c", "b", "a", "s"),
)

# 숫자 접두/접미 서열 패턴 (예: 1단계/2단계/3단계, Lv1/Lv2, 1등급/2등급)
_RANKED_PREFIX_RE = re.compile(r"^(lv\.?|level\s*|tier\s*|grade\s*|step\s*|p)(\d+)$", re.IGNORECASE)
_RANKED_SUFFIX_RE = re.compile(r"^(\d+)\s*(단계|등급|차|분기|학년|급|순위|위)$")

# 한국어 표기 날짜 (예: 2026년 11월 23일, 2026년 4월 1일)
_KOREAN_DATE_RE = re.compile(r"^\d{4}년\s*\d{1,2}월\s*\d{1,2}일$")

_CODE_SEMANTIC_TYPES = frozenset(
    {
        SemanticType.CODE,
        SemanticType.EMAIL,
        SemanticType.URL,
        SemanticType.PHONE,
        SemanticType.POSTAL_CODE,
        SemanticType.RESIDENT_REGISTRATION_NUMBER,
        SemanticType.BUSINESS_REGISTRATION_NUMBER,
        SemanticType.LEGAL_DISTRICT_CODE,
    }
)


def detect_ordinal_order(distinct_values: Sequence[str]) -> list[str]:
    """고유 범주 값 집합이 알려진 서열 척도 또는 숫자 단계 패턴이면 정렬된 순서 목록을 반환한다."""
    cleaned = [v.strip() for v in distinct_values if v and v.strip()]
    if len(cleaned) < 2 or len(cleaned) > 15:
        return []

    lower_map = {v.lower(): v for v in cleaned}
    lower_set = set(lower_map)

    for scale in _ORDINAL_DICTS:
        scale_set = set(scale)
        matched = lower_set & scale_set
        if len(matched) == len(lower_set) and len(matched) >= 2:
            return [lower_map[item] for item in scale if item in lower_map]

    # 접두사 기반 숫자 서열 (Lv1, Lv2, Lv3 ...)
    prefix_parsed: list[tuple[str, int, str]] = []
    for v in cleaned:
        m = _RANKED_PREFIX_RE.fullmatch(v)
        if m:
            prefix_parsed.append((m.group(1).lower().strip(), int(m.group(2)), v))
    if len(prefix_parsed) == len(cleaned) and len({p[0] for p in prefix_parsed}) == 1:
        return [v for _, _, v in sorted(prefix_parsed, key=lambda x: x[1])]

    # 접미사 기반 숫자 서열 (1단계, 2단계, 3단계 ...)
    suffix_parsed: list[tuple[str, int, str]] = []
    for v in cleaned:
        m = _RANKED_SUFFIX_RE.fullmatch(v)
        if m:
            suffix_parsed.append((m.group(2), int(m.group(1)), v))
    if len(suffix_parsed) == len(cleaned) and len({p[0] for p in suffix_parsed}) == 1:
        return [v for _, _, v in sorted(suffix_parsed, key=lambda x: x[1])]

    return []


def _compute_index_metadata(
    ctx: AnalysisContext,
    view: str,
    col_id: str,
    profile: ColumnProfile,
) -> IndexMetadata | None:
    """정수 칼럼의 행 순서 단조증가·연속성 지표를 DuckDB로 계산한다."""
    if profile.count < 2 or profile.missing_count > 0:
        return None
    is_unique = profile.unique_count == profile.count
    quoted = quote_identifier(col_id)

    row = ctx.connection.execute(
        f"WITH ordered AS ("
        f"  SELECT CAST({quoted} AS BIGINT) AS v, "
        f"         ROW_NUMBER() OVER () AS rn "
        f"  FROM {view} WHERE {quoted} IS NOT NULL"
        f"), diffs AS ("
        f"  SELECT v, v - LAG(v) OVER (ORDER BY rn) AS dv, rn "
        f"  FROM ordered"
        f") SELECT "
        f"  MIN(CASE WHEN rn = 1 THEN v END) AS first_v, "
        f"  COUNT(dv) AS n_diffs, "
        f"  SUM(CASE WHEN dv > 0 THEN 1 ELSE 0 END) AS pos_cnt, "
        f"  SUM(CASE WHEN dv = 1 THEN 1 ELSE 0 END) AS step1_cnt "
        f"FROM diffs"
    ).fetchone()
    if row is None or not isinstance(row[1], int) or row[1] == 0:
        return None

    first_v = int(row[0]) if isinstance(row[0], int) else -1
    n_diffs = row[1]
    pos_cnt = int(row[2]) if isinstance(row[2], int) else 0
    step1_cnt = int(row[3]) if isinstance(row[3], int) else 0

    is_monotonic = pos_cnt == n_diffs
    starts_01 = first_v in (0, 1)
    step1_rate = round(step1_cnt / n_diffs, 6)
    likely_surrogate = is_unique and is_monotonic and (starts_01 or step1_rate >= 0.8)

    return IndexMetadata(
        is_unique=is_unique,
        is_monotonic_increasing=is_monotonic,
        starts_at_zero_or_one=starts_01,
        contiguous_step_rate=step1_rate,
        likely_surrogate_index=likely_surrogate,
    )


def _fetch_non_missing_strings(
    ctx: AnalysisContext,
    col_id: str,
    limit: int = _SAMPLE_LIMIT,
) -> list[str]:
    quoted = quote_identifier(col_id)
    markers_sql = ", ".join(
        "'" + m.replace("'", "''") + "'" for m in sorted(DEFAULT_MISSING_MARKERS)
    )
    rows = ctx.connection.execute(
        f"SELECT {quoted} FROM {RAW_TABLE} "
        f"WHERE {quoted} IS NOT NULL "
        f"AND NOT regexp_matches({quoted}, '^\\s*$') "
        f"AND {quoted} NOT IN ({markers_sql}) "
        f"LIMIT {limit}"
    ).fetchall()
    return [str(r[0]) for r in rows if r and r[0] is not None]


def classify_table_columns(
    ctx: AnalysisContext,
    ingest: IngestManifest,
    profiles: Sequence[ColumnProfile],
    types: Sequence[TypeAssignment],
    stats: Sequence[ColumnStats],
) -> list[ColumnClassification]:
    """테이블의 모든 칼럼을 정밀 역할 유형 + 인덱스 지표 + 문자열 규칙 분해로 분류한다."""
    prof_by_col = {p.column: p for p in profiles}
    type_by_col = {t.column: t for t in types}
    stat_by_col = {s.column: s for s in stats}
    view = ctx.normalized_view or RAW_TABLE

    results: list[ColumnClassification] = []

    for col in ingest.columns:
        prof = prof_by_col[col.name]
        t_assign = type_by_col[col.name]
        c_stat = stat_by_col.get(col.name)
        col_id = ctx.column_id_of(col.index)

        phys = t_assign.physical_type
        sem = t_assign.semantic_type
        reasons: list[str] = []
        index_meta: IndexMetadata | None = None
        str_pattern: StringPatternDecomposition | None = None
        ordinal_order: list[str] = []
        category: ColumnRoleCategory

        # 1. 불리언
        if phys is PhysicalType.BOOLEAN or sem is SemanticType.BOOLEAN_LIKE:
            category = ColumnRoleCategory.BOOLEAN
            reasons.append("불리언 표기(Y/N·true/false 등)")

        # 2. 날짜 / 시간
        elif phys in {
            PhysicalType.DATE,
            PhysicalType.TIME,
            PhysicalType.TIMESTAMP,
            PhysicalType.TIMESTAMPTZ,
        }:
            category = ColumnRoleCategory.TEMPORAL
            reasons.append(f"시간/날짜 물리 타입({phys.value})")

        # 3. 정수 / 수치형: 고정폭 복합숫자코드(YYYYMMDD+순번) / 인덱스 / 이산 수치형
        elif phys is PhysicalType.INTEGER:
            index_meta = _compute_index_metadata(ctx, view, col_id, prof)
            is_unique = (
                prof.count > 0 and prof.missing_count == 0 and prof.unique_count == prof.count
            )
            if prof.length.minimum == prof.length.maximum and prof.length.minimum >= 10:
                sample_strs = _fetch_non_missing_strings(ctx, col_id)
                cand_pat = analyze_string_pattern(sample_strs)
                if cand_pat is not None and any(
                    seg.slot_category is ColumnRoleCategory.TEMPORAL for seg in cand_pat.segments
                ):
                    str_pattern = cand_pat

            if str_pattern is not None:
                category = ColumnRoleCategory.STRUCTURED_STRING
                reasons.append(
                    f"고정폭 복합 숫자 코드({str_pattern.template}, "
                    f"일치율 {str_pattern.coverage:.0%})"
                )
            elif is_unique and (
                (index_meta is not None and index_meta.likely_surrogate_index)
                or sem is SemanticType.IDENTIFIER
                or _INDEX_NAME_RE.search(col.name) is not None
            ):
                category = ColumnRoleCategory.INDEX
                if index_meta is not None and index_meta.likely_surrogate_index:
                    rate = index_meta.contiguous_step_rate
                    reasons.append(f"유일 정수 + 단조증가(연속 차분1 비율 {rate:.0%})")
                else:
                    reasons.append("결측 없는 유일 정수 식별자")
            else:
                category = ColumnRoleCategory.DISCRETE_NUMERIC
                reasons.append("소수부 없는 정수 분포(이산 수치형)")

        elif phys in {PhysicalType.DECIMAL, PhysicalType.FLOAT}:
            max_dec = (
                c_stat.numeric.max_decimal_digits
                if (c_stat is not None and c_stat.numeric is not None)
                else (t_assign.scale or 0)
            )
            if phys is PhysicalType.DECIMAL and max_dec == 0:
                category = ColumnRoleCategory.DISCRETE_NUMERIC
                reasons.append("소수 자릿수 0인 정수형 DECIMAL(이산 수치형)")
            else:
                category = ColumnRoleCategory.CONTINUOUS_NUMERIC
                reasons.append(f"소수부 포함 연속 수치({phys.value}, 최대 소수 {max_dec}자리)")

        # 4. 문자열 계열: 한국어 날짜 → 도메인 코드 → 규칙 문자열 분해 → 범주 → 자유 텍스트
        else:
            sample_strs = _fetch_non_missing_strings(ctx, col_id)
            if (
                sample_strs
                and sum(_KOREAN_DATE_RE.fullmatch(v.strip()) is not None for v in sample_strs)
                / len(sample_strs)
                >= 0.95
            ):
                category = ColumnRoleCategory.TEMPORAL
                phys = PhysicalType.DATE
                sem = SemanticType.DATETIME
                reasons.append("한국어 날짜 표기(YYYY년 M월 D일)")
            elif sem in _CODE_SEMANTIC_TYPES:
                # CODE여도 내부 규칙성(예: ORD-2026-001, 202604010001)이 있으면 분해 정보 함께 제공
                if sem is SemanticType.CODE:
                    str_pattern = analyze_string_pattern(sample_strs)
                if str_pattern is not None:
                    category = ColumnRoleCategory.STRUCTURED_STRING
                    reasons.append(
                        f"규칙성 코드 문자열({str_pattern.strategy}, "
                        f"슬롯 {str_pattern.variable_slot_count}개, "
                        f"일치율 {str_pattern.coverage:.0%})"
                    )
                else:
                    category = ColumnRoleCategory.CODE_IDENTIFIER
                    reasons.append(f"도메인 코드/식별자({sem.value})")
            else:
                # 일반 문자열: 먼저 서열 범주 여부와 규칙성 패턴 분해 모두 확인
                distinct_sample = list(dict.fromkeys(v.strip() for v in sample_strs if v.strip()))
                if prof.unique_count <= 20:
                    ordinal_order = detect_ordinal_order(distinct_sample)

                if ordinal_order:
                    category = ColumnRoleCategory.CATEGORICAL_ORDINAL
                    sem = SemanticType.ORDINAL_CATEGORY
                    reasons.append(f"서열 범주 순서 감지({len(ordinal_order)}단계)")
                else:
                    str_pattern = analyze_string_pattern(sample_strs)
                    if str_pattern is not None:
                        category = ColumnRoleCategory.STRUCTURED_STRING
                        reasons.append(
                            f"내부 규칙 문자열({str_pattern.strategy}: `{str_pattern.template}`, "
                            f"일치율 {str_pattern.coverage:.0%})"
                        )
                    elif sem is SemanticType.NOMINAL_CATEGORY or (
                        prof.count >= 2
                        and prof.unique_count <= 20
                        and prof.unique_count < prof.count
                        and prof.cardinality_ratio <= 0.75
                        and prof.length.mean <= 24.0
                    ):
                        category = ColumnRoleCategory.CATEGORICAL_NOMINAL
                        reasons.append(
                            f"저카디널리티 명목 범주(고유 {prof.unique_count}개, "
                            f"비율 {prof.cardinality_ratio:.2f})"
                        )
                    elif (
                        prof.count > 0
                        and prof.missing_count == 0
                        and prof.unique_count == prof.count
                        and prof.length.maximum <= 32
                        and sample_strs
                        and all(" " not in v.strip() for v in sample_strs)
                    ):
                        category = ColumnRoleCategory.CODE_IDENTIFIER
                        reasons.append("결측 없는 유일 단문 식별 코드")
                    else:
                        category = ColumnRoleCategory.FREE_TEXT
                        reasons.append("규칙성 없는 자유 텍스트")

        results.append(
            ColumnClassification(
                column=col.name,
                physical_type=phys,
                semantic_type=sem,
                category=category,
                missing_count=prof.missing_count,
                missing_rate=prof.missing_rate,
                unique_count=prof.unique_count,
                cardinality_ratio=prof.cardinality_ratio,
                index_meta=index_meta,
                string_pattern=str_pattern,
                ordinal_order=ordinal_order,
                reasons=reasons,
            )
        )

    return results


__all__ = ["classify_table_columns", "detect_ordinal_order"]
