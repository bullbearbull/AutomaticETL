"""테이블별 최적 Primary Key 선정기(Milestone E3).

후보 키(유일성 100% + 결측 0건)를 결정론적 점수표로 평가해
최적의 PK를 확정하고, 적합한 자연/비즈니스 키가 없으면 서로게이트 키를 권고한다.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from autoetl.contracts import (
    CandidateKey,
    ColumnClassification,
    ColumnProfile,
    ColumnRoleCategory,
    CrossTableForeignKey,
    PrimaryKeyRecommendation,
    TableProfileBundle,
)

_CAMEL_RE = re.compile(r"([a-z0-9])([A-Z])")
_KEY_NAME_RE = re.compile(
    r"(^|_)(id|pk|key|code|cd|no|num|seq|uuid|guid|번호|코드|식별자)$",
    re.IGNORECASE,
)
_SEQ_DISCRIMINATOR_RE = re.compile(
    r"(^|_)(line_?no|item_?no|detail_?no|sub_?no|row_?no|seq|seq_?no|line|순번|항번)$",
    re.IGNORECASE,
)

_MIN_ACCEPTABLE_PK_SCORE = 40.0
_SINGULAR_FORMS = {
    "aches": "ache",
    "alias": "alias",
    "aliases": "alias",
    "apaches": "apache",
    "avalanches": "avalanche",
    "axes": "axe",
    "backaches": "backache",
    "caches": "cache",
    "cliches": "cliche",
    "cookies": "cookie",
    "dies": "die",
    "earaches": "earache",
    "headaches": "headache",
    "heartaches": "heartache",
    "lies": "lie",
    "moustaches": "moustache",
    "movies": "movie",
    "mustaches": "mustache",
    "niches": "niche",
    "panaches": "panache",
    "pies": "pie",
    "quiches": "quiche",
    "quizzes": "quiz",
    "status": "status",
    "statuses": "status",
    "stomachaches": "stomachache",
    "ties": "tie",
    "toothaches": "toothache",
    "tranches": "tranche",
    "zombies": "zombie",
}


def _to_snake(name: str) -> str:
    return _CAMEL_RE.sub(r"\1_\2", name.strip()).lower()


def singularize(word: str) -> str:
    w = _to_snake(word)
    prefix, separator, last_word = w.rpartition("_")
    if last_word in _SINGULAR_FORMS:
        return prefix + separator + _SINGULAR_FORMS[last_word]
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w.endswith(("sses", "xes", "ches", "shes", "zzes")):
        return w[:-2]
    if w.endswith("s") and not w.endswith("ss") and len(w) > 3:
        return w[:-1]
    return w


def _score_candidate_key(
    table_name: str,
    candidate: CandidateKey,
    cls_by_col: Mapping[str, ColumnClassification],
    prof_by_col: Mapping[str, ColumnProfile],
    referenced_by_count: int,
    col_positions: Mapping[str, int],
    outgoing_fk_cols: set[str],
) -> tuple[float, list[str]]:
    """후보 키 하나의 PK 적합도(0~100)와 선정 근거 목록을 계산한다."""
    if not candidate.unique or candidate.violation_count > 0:
        return 0.0, ["유일성 위반이 있어 PK 후보에서 제외"]

    score = 0.0
    reasons: list[str] = ["전수 유일성 100% · 결측 0건"]

    # 1. 타 테이블 FK 피참조 횟수(가장 강력한 실전 PK 근거)
    if referenced_by_count > 0:
        ref_bonus = min(45.0, 30.0 + (referenced_by_count - 1) * 7.5)
        score += ref_bonus
        reasons.append(f"타 테이블 {referenced_by_count}곳에서 외래 키로 참조됨")

    # 2. 최소 컬럼 수 보너스 (단일 키 > 2컬럼 복합 > 3컬럼 복합)
    n_cols = len(candidate.columns)
    if n_cols == 1:
        score += 25.0
        reasons.append("단일 칼럼 키")
    elif n_cols == 2:
        score += 14.0
        reasons.append("2칼럼 복합 키")
        c0, c1 = candidate.columns[0], candidate.columns[1]
        is_weak_entity = (
            c0 in outgoing_fk_cols and _SEQ_DISCRIMINATOR_RE.search(_to_snake(c1)) is not None
        ) or (c1 in outgoing_fk_cols and _SEQ_DISCRIMINATOR_RE.search(_to_snake(c0)) is not None)
        if is_weak_entity:
            score += 12.0
            reasons.append("상세(약한 개체) 테이블의 FK + 순번 복합 키")
        elif all(c in outgoing_fk_cols for c in candidate.columns):
            score += 10.0
            reasons.append("연결(매핑/상세) 테이블의 복합 FK 키")
    else:
        score += 6.0
        reasons.append(f"{n_cols}칼럼 복합 키")

    # 3. 타입 안정성 · 길이 평가 (구성 컬럼 평균)
    type_scores: list[float] = []
    has_bad_type = False
    for col in candidate.columns:
        c = cls_by_col.get(col)
        p = prof_by_col.get(col)
        if c is None:
            type_scores.append(5.0)
            continue
        if c.category is ColumnRoleCategory.INDEX:
            type_scores.append(25.0)
        elif c.category in {
            ColumnRoleCategory.CODE_IDENTIFIER,
            ColumnRoleCategory.STRUCTURED_STRING,
        }:
            type_scores.append(22.0)
        elif c.category is ColumnRoleCategory.DISCRETE_NUMERIC:
            type_scores.append(14.0)
        elif c.category is ColumnRoleCategory.TEMPORAL:
            type_scores.append(8.0)
        elif c.category in {
            ColumnRoleCategory.CATEGORICAL_NOMINAL,
            ColumnRoleCategory.CATEGORICAL_ORDINAL,
        }:
            type_scores.append(10.0)
        else:
            # 연속 수치(float/decimal)·자유 텍스트·불리언은 PK로 부적합
            type_scores.append(-20.0)
            has_bad_type = True

        if p is not None and p.length.maximum > 64:
            type_scores[-1] -= 15.0
            has_bad_type = True

    avg_type_score = sum(type_scores) / len(type_scores)
    score += avg_type_score
    if not has_bad_type:
        cats = ", ".join(cls_by_col[c].category.value for c in candidate.columns if c in cls_by_col)
        reasons.append(f"안정적인 키 유형({cats})")
    else:
        reasons.append("연속 수치·장문 텍스트 포함으로 감점")

    # 4. 이름 신호 및 선두 컬럼 보너스(camelCase 포함 동률 판결)
    table_singular = singularize(table_name)
    name_hits = 0
    for col in candidate.columns:
        snake = _to_snake(col)
        if snake in {
            f"{table_singular}_id",
            f"{table_singular}_code",
            f"{table_singular}_no",
            "id",
        }:
            name_hits += 2
        elif _KEY_NAME_RE.search(snake) is not None:
            name_hits += 1
    if name_hits > 0:
        bonus = min(15.0, name_hits * 6.0)
        score += bonus
        reasons.append("식별자 명명 신호 일치")

    # 테이블 첫 번째 칼럼(position 0)에 위치하면 소폭 가산
    if n_cols == 1 and col_positions.get(candidate.columns[0], 99) == 0:
        score += 3.0

    clamped = round(max(0.0, min(100.0, score)), 2)
    return clamped, reasons


def select_primary_key(
    bundle: TableProfileBundle,
    candidate_keys: Sequence[CandidateKey],
    foreign_keys: Sequence[CrossTableForeignKey],
) -> PrimaryKeyRecommendation:
    """단일 테이블의 후보 키 목록과 타 테이블 FK 피참조 횟수를 종합해 최적 PK를 선정한다."""
    table_name = bundle.source.table_name
    cls_by_col = {c.column: c for c in bundle.classifications}
    prof_by_col = {p.column: p for p in bundle.profiles}
    col_positions = {c.name: c.index for c in bundle.ingest.columns}

    # 이 테이블의 각 키 조합을 참조하는 타 테이블 FK 개수 및 이 테이블의 발신 FK 컬럼 집계
    ref_counts: dict[tuple[str, ...], int] = {}
    outgoing_fk_cols: set[str] = set()
    for fk in foreign_keys:
        if fk.parent_table == table_name:
            key_tup = tuple(fk.parent_columns)
            ref_counts[key_tup] = ref_counts.get(key_tup, 0) + 1
        if fk.child_table == table_name and fk.child_slot_name is None:
            outgoing_fk_cols.update(fk.child_columns)

    valid_candidates = [ck for ck in candidate_keys if ck.unique and ck.violation_count == 0]
    if not valid_candidates:
        return PrimaryKeyRecommendation(
            table_name=table_name,
            columns=[],
            is_composite=False,
            recommend_surrogate=True,
            surrogate_column_name=f"{singularize(table_name)}_id",
            score=0.0,
            referenced_by_count=0,
            alternative_keys=[],
            reasons=[
                "유일성 100%·결측 0건을 만족하는 후보 키가 없어 서로게이트 키(IDENTITY) 추가를 권장"
            ],
        )

    scored: list[tuple[float, tuple[int, int], CandidateKey, list[str], int]] = []
    for ck in valid_candidates:
        key_tup = tuple(ck.columns)
        ref_cnt = ref_counts.get(key_tup, 0)
        sc, reasons = _score_candidate_key(
            table_name,
            ck,
            cls_by_col,
            prof_by_col,
            ref_cnt,
            col_positions,
            outgoing_fk_cols,
        )
        pos_sum = sum(col_positions.get(c, 999) for c in ck.columns)
        # 동률 시: 컬럼 수 적을수록, 선두에 가까운 컬럼 조합일수록 우선
        tie_key = (-len(ck.columns), -pos_sum)
        scored.append((sc, tie_key, ck, reasons, ref_cnt))

    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    best_score, _, best_ck, best_reasons, best_ref = scored[0]

    if best_score < _MIN_ACCEPTABLE_PK_SCORE:
        return PrimaryKeyRecommendation(
            table_name=table_name,
            columns=[],
            is_composite=False,
            recommend_surrogate=True,
            surrogate_column_name=f"{singularize(table_name)}_id",
            score=best_score,
            referenced_by_count=best_ref,
            alternative_keys=[item[2].columns for item in scored[:3]],
            reasons=[
                "유일 칼럼이 있으나 장문 텍스트·실수 등 불안정 유형이라 서로게이트 키 추가를 권장",
                *best_reasons,
            ],
        )

    return PrimaryKeyRecommendation(
        table_name=table_name,
        columns=list(best_ck.columns),
        is_composite=len(best_ck.columns) > 1,
        recommend_surrogate=False,
        surrogate_column_name=None,
        score=best_score,
        referenced_by_count=best_ref,
        alternative_keys=[item[2].columns for item in scored[1:4]],
        reasons=best_reasons,
    )


__all__ = ["select_primary_key", "singularize"]
