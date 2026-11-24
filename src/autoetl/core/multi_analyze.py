"""다중 테이블 결정론적 분석·관계 파악·최적 적재 계획 원스톱 파이프라인(E1~E4)."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from autoetl.contracts import MultiTableAnalysisBundle, Purpose, SchemaOverrideSpec
from autoetl.core.load_planner import export_multi_table_bundle, plan_multi_table_load
from autoetl.core.multi_context import MultiTableContext, NamedTableInput, profile_all_tables
from autoetl.core.relations_multi import MIN_INCLUSION_RATE, analyze_multi_table_relations


def analyze_tables(
    sources: Sequence[str | Path] = (),
    *,
    extra_tables: Sequence[NamedTableInput] = (),
    purpose: Purpose | str = Purpose.OLTP,
    decompose_strings: bool = True,
    recursive: bool = True,
    min_inclusion_rate: float = MIN_INCLUSION_RATE,
    overrides: SchemaOverrideSpec | None = None,
    out_dir: str | Path | None = None,
) -> MultiTableAnalysisBundle:
    """여러 CSV/TSV/XLSX 파일·디렉터리 또는 메모리 테이블을 일괄 결정론 분석한다.

    AI/LLM을 전혀 호출하지 않으며(0MB), 다음 4단계를 순서대로 실행한다.
    1. E1: 단일 격리 DuckDB에 모든 파일·시트를 스키마별(`t0..tN`) 적재 + S0~S4 프로파일·통계
    2. E2: 10종 정밀 칼럼 유형 분류·결측치 파악·규칙성 문자열 고정부/가변 슬롯 분해
    3. E3: 테이블 내부 관계(FD/상관/파생/계층) + 테이블 간 FK(슬롯 FK 포함) + 최적 PK 선정
    4. E4: 용도(`purpose`)별 스키마 후보 채점 + sqlglot 검증 PostgreSQL DDL + 위상 적재 플랜 + ERD

    ``out_dir``를 지정하면 ``ddl.sql``, ``load_plan.json``, ``relations.json``,
    ``analysis_bundle.json``, ``erd.mmd``, ``analysis_report.md``를 즉시 내보낸다.
    """
    resolved_purpose = purpose if isinstance(purpose, Purpose) else Purpose(purpose)
    inputs_arg: Sequence[str | Path] | None = list(sources) if sources else None
    with MultiTableContext(
        inputs=inputs_arg,
        extra_tables=extra_tables,
        recursive=recursive,
    ) as mctx:
        tables = profile_all_tables(mctx)
        relations = analyze_multi_table_relations(
            mctx,
            tables,
            min_inclusion_rate=min_inclusion_rate,
        )
        load_plan = plan_multi_table_load(
            tables,
            relations,
            purpose=resolved_purpose,
            decompose_strings=decompose_strings,
            mctx=mctx,
            overrides=overrides,
        )

    warnings: list[str] = []
    for tb in tables:
        warnings.extend(tb.warnings)
    warnings.extend(relations.notes)

    bundle = MultiTableAnalysisBundle(
        purpose=resolved_purpose,
        tables=tables,
        relations=relations,
        load_plan=load_plan,
        warnings=warnings,
    )
    if out_dir is not None:
        export_multi_table_bundle(bundle, out_dir)
    return bundle


__all__ = ["analyze_tables"]
