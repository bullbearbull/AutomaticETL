"""S4 심화 통계 — 수치·범주·시계열 컬럼별 결정론적 통계(ColumnStats)."""

from __future__ import annotations

import datetime
import math
from collections.abc import Sequence

from autoetl.contracts import (
    CategoricalStats,
    ColumnStats,
    GapSummary,
    IngestManifest,
    NumericStats,
    OutlierMethod,
    OutlierSummary,
    PhysicalType,
    SeasonalitySummary,
    SemanticType,
    StatsKind,
    TemporalStats,
    TypeAssignment,
)
from autoetl.contracts.stats import CategoryValue
from autoetl.core.context import RAW_TABLE, AnalysisContext, quote_identifier

_NUMERIC_PHYSICAL = frozenset({PhysicalType.INTEGER, PhysicalType.DECIMAL, PhysicalType.FLOAT})
_TEMPORAL_PHYSICAL = frozenset(
    {PhysicalType.DATE, PhysicalType.TIME, PhysicalType.TIMESTAMP, PhysicalType.TIMESTAMPTZ}
)
_CATEGORICAL_SEMANTIC = frozenset(
    {
        SemanticType.NOMINAL_CATEGORY,
        SemanticType.ORDINAL_CATEGORY,
        SemanticType.BOOLEAN_LIKE,
    }
)


def _safe_float(val: object, default: float = 0.0) -> float:
    if val is None or isinstance(val, bool):
        return default
    if isinstance(val, (int, float)):
        f = float(val)
        return f if math.isfinite(f) else default
    try:
        f = float(str(val))
        return f if math.isfinite(f) else default
    except ValueError:
        return default


def _compute_numeric_stats(
    ctx: AnalysisContext,
    view: str,
    col_id: str,
    assignment: TypeAssignment,
) -> NumericStats | None:
    quoted = quote_identifier(col_id)
    row = ctx.connection.execute(
        f"SELECT COUNT({quoted}), AVG(CAST({quoted} AS DOUBLE)), "
        f"STDDEV_POP(CAST({quoted} AS DOUBLE)), "
        f"MIN(CAST({quoted} AS DOUBLE)), MAX(CAST({quoted} AS DOUBLE)), "
        f"QUANTILE_CONT(CAST({quoted} AS DOUBLE), 0.25), "
        f"QUANTILE_CONT(CAST({quoted} AS DOUBLE), 0.50), "
        f"QUANTILE_CONT(CAST({quoted} AS DOUBLE), 0.75) "
        f"FROM {view} WHERE {quoted} IS NOT NULL"
    ).fetchone()
    if row is None or not isinstance(row[0], int) or row[0] == 0:
        return None

    n = row[0]
    mean = _safe_float(row[1])
    std = _safe_float(row[2])
    minimum = _safe_float(row[3])
    maximum = _safe_float(row[4])
    p25 = _safe_float(row[5])
    p50 = _safe_float(row[6])
    p75 = _safe_float(row[7])

    # 왜도(skewness): std > 0이고 행 수 >= 3일 때 계산
    skew = 0.0
    if std > 0.0 and n >= 3:
        skew_row = ctx.connection.execute(
            f"SELECT AVG(POWER((CAST({quoted} AS DOUBLE) - ?) / ?, 3)) "
            f"FROM {view} WHERE {quoted} IS NOT NULL",
            [mean, std],
        ).fetchone()
        if skew_row is not None:
            skew = round(_safe_float(skew_row[0]), 6)

    iqr = max(0.0, p75 - p25)
    low_bound = p25 - 1.5 * iqr
    high_bound = p75 + 1.5 * iqr
    out_row = ctx.connection.execute(
        f"SELECT COUNT(*) FROM {view} "
        f"WHERE {quoted} IS NOT NULL "
        f"AND (CAST({quoted} AS DOUBLE) < ? OR CAST({quoted} AS DOUBLE) > ?)",
        [low_bound, high_bound],
    ).fetchone()
    outlier_count = int(out_row[0]) if (out_row and isinstance(out_row[0], int)) else 0
    outlier_rate = min(1.0, max(0.0, outlier_count / n))

    # 실제 최대 소수 자릿수 추정
    max_dec = assignment.scale or 0
    if assignment.physical_type is PhysicalType.INTEGER:
        max_dec = 0
    elif max_dec == 0 and assignment.physical_type in {PhysicalType.DECIMAL, PhysicalType.FLOAT}:
        dec_row = ctx.connection.execute(
            f"SELECT MAX(CASE WHEN POSITION('.' IN CAST({quoted} AS VARCHAR)) > 0 "
            f"THEN LENGTH(RTRIM(SPLIT_PART(CAST({quoted} AS VARCHAR), '.', 2), '0')) ELSE 0 END) "
            f"FROM {view} WHERE {quoted} IS NOT NULL"
        ).fetchone()
        if dec_row is not None and isinstance(dec_row[0], int) and dec_row[0] > 0:
            max_dec = dec_row[0]

    return NumericStats(
        mean=round(mean, 6),
        std=round(std, 6),
        minimum=round(minimum, 6),
        maximum=round(maximum, 6),
        p25=round(p25, 6),
        p50=round(p50, 6),
        p75=round(p75, 6),
        skew=skew,
        max_decimal_digits=max_dec,
        outliers=OutlierSummary(
            method=OutlierMethod.IQR,
            count=outlier_count,
            rate=round(outlier_rate, 6),
            bounds=f"IQR 1.5배: [{low_bound:.4g}, {high_bound:.4g}]",
        ),
    )


def _compute_categorical_stats(
    ctx: AnalysisContext,
    view: str,
    col_id: str,
) -> CategoricalStats | None:
    quoted = quote_identifier(col_id)
    tot_row = ctx.connection.execute(
        f"SELECT COUNT({quoted}), COUNT(DISTINCT {quoted}) FROM {view} WHERE {quoted} IS NOT NULL"
    ).fetchone()
    if tot_row is None or not isinstance(tot_row[0], int) or tot_row[0] == 0:
        return None
    total = tot_row[0]
    distinct = int(tot_row[1]) if isinstance(tot_row[1], int) else 0

    rows = ctx.connection.execute(
        f"SELECT CAST({quoted} AS VARCHAR) AS v, COUNT(*) AS cnt "
        f"FROM {view} WHERE {quoted} IS NOT NULL "
        f"GROUP BY v ORDER BY cnt DESC, v ASC LIMIT 20"
    ).fetchall()
    top_values = [
        CategoryValue(
            value=str(val),
            count=int(cnt),
            rate=round(min(1.0, int(cnt) / total), 6),
        )
        for val, cnt in rows
    ]

    # 전체 엔트로피 계산
    ent_row = ctx.connection.execute(
        f"SELECT SUM(-(cnt::DOUBLE / {total}) * LOG2(cnt::DOUBLE / {total})) "
        f"FROM (SELECT COUNT(*) AS cnt FROM {view} WHERE {quoted} IS NOT NULL GROUP BY {quoted})"
    ).fetchone()
    entropy = max(0.0, round(_safe_float(ent_row[0] if ent_row else 0.0), 6))

    return CategoricalStats(
        entropy=entropy,
        top_values=top_values,
        distinct_ratio=round(min(1.0, distinct / total), 6),
    )


def _format_duration_label(seconds: float) -> str:
    if seconds >= 86400 * 27:
        return f"{round(seconds / (86400 * 30))}mo"
    if seconds >= 86400:
        return f"{round(seconds / 86400)}D"
    if seconds >= 3600:
        return f"{round(seconds / 3600)}h"
    if seconds >= 60:
        return f"{round(seconds / 60)}m"
    return f"{max(1, round(seconds))}s"


def _to_iso_str(val: object) -> str:
    if isinstance(val, (datetime.datetime, datetime.date, datetime.time)):
        return val.isoformat()
    return str(val)


def _compute_temporal_stats(
    ctx: AnalysisContext,
    view: str,
    col_id: str,
    phys: PhysicalType,
) -> TemporalStats | None:
    quoted = quote_identifier(col_id)
    row = ctx.connection.execute(
        f"SELECT MIN({quoted}), MAX({quoted}), COUNT({quoted}) "
        f"FROM {view} WHERE {quoted} IS NOT NULL"
    ).fetchone()
    if row is None or row[0] is None or row[1] is None:
        return None
    min_iso = _to_iso_str(row[0])
    max_iso = _to_iso_str(row[1])

    dominant_freq: str | None = None
    gap_count = 0
    longest_gap = "없음"

    if phys is not PhysicalType.TIME:
        # 정렬된 고유 시점 간 양의 차분(초)으로 지배 주기와 결측 구간 계산
        diffs = ctx.connection.execute(
            f"WITH ordered AS ("
            f"  SELECT DISTINCT CAST({quoted} AS TIMESTAMP) AS ts "
            f"  FROM {view} WHERE {quoted} IS NOT NULL ORDER BY ts LIMIT 5000"
            f"), deltas AS ("
            f"  SELECT prev_ts, ts, EPOCH(ts) - EPOCH(prev_ts) AS diff_s "
            f"  FROM (SELECT ts, LAG(ts) OVER (ORDER BY ts) AS prev_ts FROM ordered) "
            f"  WHERE prev_ts IS NOT NULL AND EPOCH(ts) > EPOCH(prev_ts)"
            f") SELECT prev_ts, ts, diff_s FROM deltas ORDER BY diff_s ASC"
        ).fetchall()
        if diffs:
            median_s = _safe_float(diffs[len(diffs) // 2][2])
            if median_s > 0:
                dominant_freq = _format_duration_label(median_s)
                threshold = median_s * 2.5
                gaps = [d for d in diffs if _safe_float(d[2]) > threshold]
                gap_count = len(gaps)
                if gaps:
                    worst = max(gaps, key=lambda d: _safe_float(d[2]))
                    longest_gap = f"{_to_iso_str(worst[0])} ~ {_to_iso_str(worst[1])}"

    return TemporalStats(
        minimum=min_iso,
        maximum=max_iso,
        dominant_frequency=dominant_freq,
        gaps=GapSummary(count=gap_count, longest=longest_gap),
        seasonality=SeasonalitySummary(detected=False, period=None, method="acf"),
    )


def run(
    ctx: AnalysisContext,
    ingest: IngestManifest,
    types: Sequence[TypeAssignment] | None = None,
) -> list[ColumnStats]:
    """S4 심화 통계 계산: 확정된 타입(S3)에 따라 수치·범주·시계열 통계를 채운다."""
    resolved_types: Sequence[TypeAssignment]
    if types is not None:
        resolved_types = types
    else:
        raw_art = ctx.stage_artifacts.get("s03_types")
        resolved_types = raw_art if isinstance(raw_art, list) else []

    type_by_col = {t.column: t for t in resolved_types}
    raw_prof = ctx.stage_artifacts.get("s02_profile")
    prof_by_col = {p.column: p for p in raw_prof} if isinstance(raw_prof, list) else {}
    view = ctx.normalized_view or RAW_TABLE
    out: list[ColumnStats] = []

    for col in ingest.columns:
        assignment = type_by_col.get(col.name)
        if assignment is None:
            out.append(ColumnStats(column=col.name, kind=StatsKind.OTHER))
            continue

        col_id = ctx.column_id_of(col.index)
        prof = prof_by_col.get(col.name)
        is_low_card_str = (
            assignment.physical_type is PhysicalType.STRING
            and prof is not None
            and prof.count > 0
            and 1 <= prof.unique_count <= 20
            and (prof.cardinality_ratio <= 0.75 or prof.unique_count < prof.count)
        )
        if assignment.physical_type in _NUMERIC_PHYSICAL:
            num_stats = _compute_numeric_stats(ctx, view, col_id, assignment)
            if num_stats is not None:
                out.append(ColumnStats(column=col.name, kind=StatsKind.NUMERIC, numeric=num_stats))
            else:
                out.append(ColumnStats(column=col.name, kind=StatsKind.OTHER))
        elif assignment.physical_type in _TEMPORAL_PHYSICAL:
            tmp_stats = _compute_temporal_stats(ctx, view, col_id, assignment.physical_type)
            if tmp_stats is not None:
                out.append(
                    ColumnStats(column=col.name, kind=StatsKind.DATETIME, temporal=tmp_stats)
                )
            else:
                out.append(ColumnStats(column=col.name, kind=StatsKind.OTHER))
        elif (
            assignment.semantic_type in _CATEGORICAL_SEMANTIC
            or assignment.physical_type is PhysicalType.BOOLEAN
            or is_low_card_str
        ):
            cat_stats = _compute_categorical_stats(ctx, view, col_id)
            if cat_stats is not None:
                out.append(
                    ColumnStats(column=col.name, kind=StatsKind.CATEGORICAL, categorical=cat_stats)
                )
            else:
                out.append(ColumnStats(column=col.name, kind=StatsKind.OTHER))
        elif assignment.physical_type is PhysicalType.STRING:
            out.append(ColumnStats(column=col.name, kind=StatsKind.TEXT))
        else:
            out.append(ColumnStats(column=col.name, kind=StatsKind.OTHER))

    return out
