"""문자열 내부 규칙성(공통 고정부 vs 가변 슬롯) 결정론적 분해 엔진 (Milestone E2).

규칙성이 있는 문자열 칼럼(예: `ORD-2026-KR-00012`, `PRJ2026US002`, `202604010042`,
`서울시 강남구 역삼동`)을 3가지 결정론적 분해 전략으로 분석해
공통 고정부(`LITERAL`)와 가변 슬롯(`SLOT`)으로 나누고,
각 가변 슬롯의 세부 유형·정규식 캡처 그룹·DuckDB 추출 힌트를 생성한다.
"""

from __future__ import annotations

import datetime
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from autoetl.contracts import (
    ColumnRoleCategory,
    PhysicalType,
    StringPatternDecomposition,
    StringSegment,
    StringSegmentKind,
)
from autoetl.core.stages.s02_profile import pattern_mask

MIN_SAMPLE_VALUES = 3
MIN_DISTINCT_VALUES = 3
MIN_COVERAGE = 0.85
LITERAL_DOMINANCE_THRESHOLD = 0.98
MAX_VARIABLE_SLOTS = 8

_DELIM_SPLIT_RE = re.compile(r"([-_/:.| ]+)")
_CHAR_CLASS_RE = re.compile(r"[A-Z]+|[a-z]+|\d+|[가-힣ㄱ-ㅎㅏ-ㅣ]+|[^A-Za-z0-9가-힣ㄱ-ㅎㅏ-ㅣ]+")
_ASCII_DIGITS_RE = re.compile(r"^\d+$", re.ASCII)
_ASCII_UPPER_RE = re.compile(r"^[A-Z]+$", re.ASCII)
_ASCII_LOWER_RE = re.compile(r"^[a-z]+$", re.ASCII)
_ASCII_ALPHA_RE = re.compile(r"^[A-Za-z]+$", re.ASCII)
_ASCII_ALNUM_RE = re.compile(r"^[A-Za-z0-9]+$", re.ASCII)
_HANGUL_RE = re.compile(r"^[가-힣ㄱ-ㅎㅏ-ㅣ]+$")

_DATE8_MIN_YEAR = 1950
_DATE8_MAX_YEAR = 2099


def _is_valid_yyyymmdd(val: str) -> bool:
    if len(val) != 8 or not _ASCII_DIGITS_RE.fullmatch(val):
        return False
    y = int(val[:4])
    if not (_DATE8_MIN_YEAR <= y <= _DATE8_MAX_YEAR):
        return False
    try:
        datetime.date(y, int(val[4:6]), int(val[6:8]))
        return True
    except ValueError:
        return False


def _is_valid_yyyymm(val: str) -> bool:
    if len(val) != 6 or not _ASCII_DIGITS_RE.fullmatch(val):
        return False
    y = int(val[:4])
    m = int(val[4:6])
    return _DATE8_MIN_YEAR <= y <= _DATE8_MAX_YEAR and 1 <= m <= 12


def _classify_slot_values(
    values: Sequence[str],
    slot_idx: int,
) -> tuple[str, ColumnRoleCategory, PhysicalType, bool, str]:
    """가변 슬롯 값 표본을 분석해 `(이름, 정밀유형, 물리타입, zero_padded, 정규식조각)`을 낸다."""
    non_empty = [v for v in values if v]
    if not non_empty:
        return (
            f"slot_{slot_idx}",
            ColumnRoleCategory.FREE_TEXT,
            PhysicalType.STRING,
            False,
            r"[^-_/:.| ]*",
        )

    n = len(non_empty)
    uniq = set(non_empty)
    n_uniq = len(uniq)
    lens = [len(v) for v in non_empty]
    min_l, max_l = min(lens), max(lens)
    len_quant = f"{{{min_l}}}" if min_l == max_l else f"{{{min_l},{max_l}}}"

    all_digits = all(_ASCII_DIGITS_RE.fullmatch(v) for v in non_empty)
    if all_digits:
        zero_padded = any(len(v) > 1 and v.startswith("0") for v in non_empty)
        regex_piece = f"[0-9]{len_quant}"

        # 8자리 YYYYMMDD 날짜
        if min_l == 8 and max_l == 8 and sum(_is_valid_yyyymmdd(v) for v in non_empty) / n >= 0.95:
            return "date", ColumnRoleCategory.TEMPORAL, PhysicalType.DATE, False, r"[0-9]{8}"
        # 6자리 YYYYMM 연월
        if min_l == 6 and max_l == 6 and sum(_is_valid_yyyymm(v) for v in non_empty) / n >= 0.95:
            return (
                "year_month",
                ColumnRoleCategory.TEMPORAL,
                PhysicalType.STRING,
                False,
                r"[0-9]{6}",
            )
        # 4자리 연도
        if (
            min_l == 4
            and max_l == 4
            and all(1950 <= int(v) <= 2099 for v in non_empty)
            and n_uniq <= 80
        ):
            return (
                "year",
                ColumnRoleCategory.DISCRETE_NUMERIC,
                PhysicalType.INTEGER,
                False,
                r"[0-9]{4}",
            )
        # 2자리 월(01~12)
        if min_l == 2 and max_l == 2 and all(1 <= int(v) <= 12 for v in non_empty) and zero_padded:
            return (
                "month",
                ColumnRoleCategory.DISCRETE_NUMERIC,
                PhysicalType.INTEGER,
                True,
                r"[0-9]{2}",
            )
        # 제로패딩 순번 / 고정폭 숫자 코드
        if zero_padded:
            ints = [int(v) for v in non_empty]
            is_seq = n_uniq / n >= 0.8 and max(ints) > min(ints)
            name = "seq" if is_seq else f"code_{slot_idx}"
            cat = (
                ColumnRoleCategory.INDEX
                if is_seq and n_uniq == n
                else ColumnRoleCategory.CODE_IDENTIFIER
            )
            return name, cat, PhysicalType.STRING, True, regex_piece
        # 일반 정수 슬롯
        if n_uniq == n and n >= 3:
            return "seq", ColumnRoleCategory.INDEX, PhysicalType.INTEGER, False, regex_piece
        return (
            f"num_{slot_idx}",
            ColumnRoleCategory.DISCRETE_NUMERIC,
            PhysicalType.INTEGER,
            False,
            regex_piece,
        )

    # 문자 클래스별 정밀 정규식 조각 결정
    if all(_ASCII_UPPER_RE.fullmatch(v) for v in non_empty):
        regex_piece = f"[A-Z]{len_quant}"
    elif all(_ASCII_LOWER_RE.fullmatch(v) for v in non_empty):
        regex_piece = f"[a-z]{len_quant}"
    elif all(_ASCII_ALPHA_RE.fullmatch(v) for v in non_empty):
        regex_piece = f"[A-Za-z]{len_quant}"
    elif all(_ASCII_ALNUM_RE.fullmatch(v) for v in non_empty):
        regex_piece = f"[A-Za-z0-9]{len_quant}"
    elif all(_HANGUL_RE.fullmatch(v) for v in non_empty):
        regex_piece = f"[가-힣ㄱ-ㅎㅏ-ㅣ]{len_quant}"
    else:
        regex_piece = f"[^-_/:.| ]{len_quant}"

    card_ratio = n_uniq / n
    if n_uniq <= 20 and (card_ratio <= 0.6 or n <= 12):
        return (
            f"cat_{slot_idx}",
            ColumnRoleCategory.CATEGORICAL_NOMINAL,
            PhysicalType.STRING,
            False,
            regex_piece,
        )
    if max_l <= 16:
        return (
            f"code_{slot_idx}",
            ColumnRoleCategory.CODE_IDENTIFIER,
            PhysicalType.STRING,
            False,
            regex_piece,
        )
    return (
        f"slot_{slot_idx}",
        ColumnRoleCategory.FREE_TEXT,
        PhysicalType.STRING,
        False,
        regex_piece,
    )


@dataclass
class _CandidateDecomposition:
    decomp: StringPatternDecomposition
    score: tuple[int, int, float, int]


def _build_decomposition_from_token_rows(
    strategy: str,
    raw_values: Sequence[str],
    tokenized_rows: Sequence[list[str]],
    *,
    delimiter_hint: str | None = None,
) -> _CandidateDecomposition | None:
    """동일 길이 토큰 행렬에서 공통 고정부·가변 슬롯을 분리하고 정규식을 검증한다."""
    if not tokenized_rows:
        return None

    # 가장 지배적인 토큰 개수 선택
    len_counts = Counter(len(toks) for toks in tokenized_rows)
    dom_len, dom_cnt = len_counts.most_common(1)[0]
    if dom_len < 2 or dom_len > 25:
        return None
    if dom_cnt / len(raw_values) < MIN_COVERAGE:
        return None

    aligned = [toks for toks in tokenized_rows if len(toks) == dom_len]

    # 공백 구분자 기반의 자유 문장 오탐 차단
    if delimiter_hint == " " and dom_len > 9:
        return None

    segments: list[StringSegment] = []
    regex_parts: list[str] = ["^"]
    template_parts: list[str] = []
    used_slot_names: set[str] = set()
    literal_count = 0
    non_delim_literal_count = 0
    slot_count = 0
    capture_idx = 0

    for pos in range(dom_len):
        col_vals = [row[pos] for row in aligned]
        freq = Counter(col_vals)
        top_val, top_cnt = freq.most_common(1)[0]
        dominance = top_cnt / len(col_vals)

        if dominance >= LITERAL_DOMINANCE_THRESHOLD:
            literal_count += 1
            if not _DELIM_SPLIT_RE.fullmatch(top_val):
                non_delim_literal_count += 1
            segments.append(
                StringSegment(
                    position=pos,
                    kind=StringSegmentKind.LITERAL,
                    literal_value=top_val,
                    unique_count=len(freq),
                    min_length=len(top_val),
                    max_length=len(top_val),
                )
            )
            regex_parts.append(re.escape(top_val))
            template_parts.append(top_val)
            continue

        slot_count += 1
        if slot_count > MAX_VARIABLE_SLOTS:
            return None

        capture_idx += 1
        base_name, slot_cat, slot_phys, zero_pad, rx_piece = _classify_slot_values(
            col_vals, slot_count - 1
        )
        slot_name = base_name
        suffix = 2
        while slot_name in used_slot_names:
            slot_name = f"{base_name}_{suffix}"
            suffix += 1
        used_slot_names.add(slot_name)

        masks = Counter(pattern_mask(v) for v in col_vals if v)
        top_mask = masks.most_common(1)[0][0] if masks else None
        lens = [len(v) for v in col_vals]

        regex_parts.append(f"({rx_piece})")
        template_parts.append(f"{{{slot_name}}}")
        segments.append(
            StringSegment(
                position=pos,
                kind=StringSegmentKind.SLOT,
                slot_name=slot_name,
                slot_category=slot_cat,
                slot_physical_type=slot_phys,
                unique_count=len(freq),
                min_length=min(lens),
                max_length=max(lens),
                zero_padded=zero_pad,
                sample_pattern=top_mask,
                capture_group=capture_idx,
            )
        )

    if slot_count == 0:
        return None

    # 자유 문장·인명 방어: 공백 구분자에 고정어가 전혀 없으면
    # 숫자/날짜 슬롯이나 저카디널리티 계층 슬롯이 있는 경우에만 규칙 문자열로 인정
    if delimiter_hint == " " and non_delim_literal_count == 0:
        avg_len = sum(len(v) for v in raw_values) / len(raw_values)
        if avg_len > 18 or slot_count >= 4:
            return None
        slot_segs = [s for s in segments if s.kind is StringSegmentKind.SLOT]
        has_structured_slot = any(
            s.slot_category
            in {
                ColumnRoleCategory.TEMPORAL,
                ColumnRoleCategory.INDEX,
                ColumnRoleCategory.DISCRETE_NUMERIC,
            }
            or (
                s.slot_category
                in {
                    ColumnRoleCategory.CATEGORICAL_NOMINAL,
                    ColumnRoleCategory.CATEGORICAL_ORDINAL,
                }
                and s.unique_count / max(len(aligned), 1) <= 0.35
            )
            for s in slot_segs
        )
        if not has_structured_slot:
            return None

    # 고정부(구분자 또는 리터럴)가 전혀 없고 슬롯이 1개뿐이면 규칙 분해 의미가 없다
    if literal_count == 0 and slot_count < 2:
        return None

    regex_parts.append("$")
    full_regex = "".join(regex_parts)
    try:
        compiled = re.compile(full_regex)
    except re.error:
        return None

    # 실제 표본으로 정규식 매칭률 검증
    matched = sum(1 for v in raw_values if compiled.fullmatch(v) is not None)
    coverage = matched / len(raw_values)
    if coverage < MIN_COVERAGE:
        return None

    escaped_sql_rx = full_regex.replace("'", "''")
    final_segments: list[StringSegment] = []
    for seg in segments:
        if seg.kind is StringSegmentKind.SLOT and seg.capture_group is not None:
            hint = f"regexp_extract(col, '{escaped_sql_rx}', {seg.capture_group})"
            final_segments.append(seg.model_copy(update={"extract_sql_hint": hint}))
        else:
            final_segments.append(seg)

    decomp = StringPatternDecomposition(
        strategy=strategy,
        delimiter=delimiter_hint,
        template="".join(template_parts),
        regex_pattern=full_regex,
        coverage=round(coverage, 6),
        segments=final_segments,
        variable_slot_count=slot_count,
    )
    # 전략 우선순위: 명시적 구분자(3) > 문자클래스 경계(2) > 고정폭 슬라이싱(1)
    # (고정폭 전략이 구분자 안쪽의 우연한 상수 접두사 '202' 등을 쪼개는 과적합을 막는다.)
    strategy_rank = {"delimiter": 3, "char_class": 2, "fixed_width": 1}.get(strategy, 0)
    has_strong_lit = 1 if (non_delim_literal_count > 0 or literal_count > 0) else 0
    score = (
        strategy_rank,
        has_strong_lit,
        round(coverage, 2),
        min(slot_count, 4),
    )
    return _CandidateDecomposition(decomp=decomp, score=score)


def _try_delimiter_strategy(values: Sequence[str]) -> _CandidateDecomposition | None:
    """전략 A: 구조 구분자(`-`, `_`, `/`, `:`, `.`, `|`, 공백)를 보존해 토큰화한다."""
    tokenized: list[list[str]] = []
    delim_counter: Counter[str] = Counter()
    for v in values:
        parts = [p for p in _DELIM_SPLIT_RE.split(v) if p != ""]
        if len(parts) >= 3:
            for p in parts:
                if _DELIM_SPLIT_RE.fullmatch(p):
                    delim_counter[p] += 1
        tokenized.append(parts)

    if not delim_counter:
        return None
    main_delim = delim_counter.most_common(1)[0][0]
    return _build_decomposition_from_token_rows(
        "delimiter", values, tokenized, delimiter_hint=main_delim
    )


def _try_char_class_strategy(values: Sequence[str]) -> _CandidateDecomposition | None:
    """전략 B: 구분자 없이 영문·숫자·한글 경계로 분해한다(예: `PRJ2026KR001`)."""
    tokenized = [_CHAR_CLASS_RE.findall(v) for v in values]
    # 일반 영문 대소문자 단어(Title Case: 'January', 'High', 'London') 오탐 차단:
    # 문자클래스 경계 분해는 반드시 숫자 등 이질적 도메인이 결합된 코드에만 적용한다.
    has_digit_boundary = sum(any(tok.isdigit() for tok in row) for row in tokenized) / max(
        len(tokenized), 1
    )
    if has_digit_boundary < MIN_COVERAGE:
        return None
    return _build_decomposition_from_token_rows(
        "char_class", values, tokenized, delimiter_hint=None
    )


def _char_bucket(ch: str) -> str:
    if ch.isascii() and ch.isdigit():
        return "D"
    if ch.isascii() and ch.isupper():
        return "U"
    if ch.isascii() and ch.islower():
        return "L"
    if "\uac00" <= ch <= "\ud7a3":
        return "H"
    return "X"


def _try_fixed_width_strategy(values: Sequence[str]) -> _CandidateDecomposition | None:
    """전략 C: 고정 길이 코드(`202604010001` 등)를 위치별 상수·날짜·순번 블록으로 슬라이싱한다."""
    len_counts = Counter(len(v) for v in values)
    dom_len, dom_cnt = len_counts.most_common(1)[0]
    if dom_len < 4 or dom_len > 32 or dom_cnt / len(values) < MIN_COVERAGE:
        return None

    same_len = [v for v in values if len(v) == dom_len]

    # 1) 순수 숫자 고정폭 코드의 YYYYMMDD / YYYYMM 접두사 + 순번 우선 분해
    #    (같은 달 표본에서 앞 7자리 '2026040'이 상수로 과적합되는 것을 방지한다.)
    if dom_len >= 10 and all(_ASCII_DIGITS_RE.fullmatch(v) for v in same_len):
        if sum(_is_valid_yyyymmdd(v[:8]) for v in same_len) / len(same_len) >= 0.95:
            tok_rows = [[v[:8], v[8:]] if len(v) == dom_len else [v] for v in values]
            cand = _build_decomposition_from_token_rows(
                "fixed_width", values, tok_rows, delimiter_hint=None
            )
            if cand is not None:
                return cand
        if sum(_is_valid_yyyymm(v[:6]) for v in same_len) / len(same_len) >= 0.95:
            tok_rows = [[v[:6], v[6:]] if len(v) == dom_len else [v] for v in values]
            cand = _build_decomposition_from_token_rows(
                "fixed_width", values, tok_rows, delimiter_hint=None
            )
            if cand is not None:
                return cand

    # 2) 위치별 문자 상수 여부와 문자 클래스 판정
    pos_tags: list[str] = []
    for i in range(dom_len):
        chars = [v[i] for v in same_len]
        top_ch, top_c = Counter(chars).most_common(1)[0]
        if top_c / len(same_len) >= LITERAL_DOMINANCE_THRESHOLD:
            pos_tags.append(f"CONST:{top_ch}")
        else:
            buckets = Counter(_char_bucket(c) for c in chars)
            top_b, top_bc = buckets.most_common(1)[0]
            pos_tags.append(top_b if top_bc / len(same_len) >= 0.95 else "X")

    # 연속된 같은 태그 구간 병합 → 스팬 목록 (start, end, is_const, tag)
    spans: list[tuple[int, int, bool, str]] = []
    start = 0
    while start < dom_len:
        cur = pos_tags[start]
        is_const = cur.startswith("CONST:")
        end = start + 1
        while end < dom_len:
            nxt = pos_tags[end]
            if (is_const and nxt.startswith("CONST:")) or (not is_const and nxt == cur):
                end += 1
            else:
                break
        spans.append((start, end, is_const, "CONST" if is_const else cur))
        start = end

    if len(spans) < 2:
        return None

    tokenized_rows: list[list[str]] = []
    for v in values:
        if len(v) != dom_len:
            tokenized_rows.append([v])
        else:
            tokenized_rows.append([v[s:e] for s, e, _, _ in spans])

    return _build_decomposition_from_token_rows(
        "fixed_width", values, tokenized_rows, delimiter_hint=None
    )


def analyze_string_pattern(values: Sequence[str]) -> StringPatternDecomposition | None:
    """비결측 문자열 표본에서 공통 고정부와 가변 슬롯 구조를 결정론적으로 분해한다.

    규칙성이 뚜렷하지 않은 자유 텍스트·상수 컬럼·단순 단어는 `None`을 반환한다.
    """
    cleaned = [v.strip() for v in values if v and v.strip()]
    if len(cleaned) < MIN_SAMPLE_VALUES or len(set(cleaned)) < MIN_DISTINCT_VALUES:
        return None

    # 너무 긴 문단(자유 텍스트)은 조기 제외
    avg_len = sum(len(v) for v in cleaned) / len(cleaned)
    if avg_len > 80:
        return None

    candidates: list[_CandidateDecomposition] = []
    for fn in (_try_delimiter_strategy, _try_char_class_strategy, _try_fixed_width_strategy):
        cand = fn(cleaned)
        if cand is not None:
            candidates.append(cand)

    if not candidates:
        return None

    best = max(candidates, key=lambda c: c.score)
    return best.decomp


__all__ = ["analyze_string_pattern"]
