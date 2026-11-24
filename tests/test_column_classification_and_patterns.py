"""Milestone E2: 칼럼 정밀 유형 분류(7대 범주), S4 심화 통계, 문자열 내부 규칙성 분해 테스트."""

from __future__ import annotations

import re

from autoetl.contracts import (
    ColumnRoleCategory,
    PhysicalType,
    StatsKind,
    StringSegmentKind,
)
from autoetl.core.column_classifier import detect_ordinal_order
from autoetl.core.multi_context import MultiTableContext, NamedTableInput, profile_all_tables
from autoetl.core.string_pattern import analyze_string_pattern


def test_string_pattern_decomposition_delimiter_charclass_and_fixed_width() -> None:
    """구분자·문자클래스·고정폭·한국어 공통접두사 문자열 분해와 자유텍스트 비분해를 검증한다."""
    # 1. 구분자 기반 복합 주문번호: 공통 고정부('ORD', '-') vs 가변 슬롯(year, cat, seq)
    orders = [
        "ORD-2024-KR-00001",
        "ORD-2025-US-00002",
        "ORD-2026-JP-00003",
        "ORD-2026-KR-00004",
        "ORD-2025-KR-00005",
    ]
    d1 = analyze_string_pattern(orders)
    assert d1 is not None
    assert d1.strategy == "delimiter"
    assert d1.coverage == 1.0
    assert d1.variable_slot_count == 3
    literals = [s.literal_value for s in d1.segments if s.kind is StringSegmentKind.LITERAL]
    assert "ORD" in literals
    assert "-" in literals
    slots = [s for s in d1.segments if s.kind is StringSegmentKind.SLOT]
    assert slots[0].slot_name == "year"
    assert slots[0].slot_physical_type is PhysicalType.INTEGER
    assert slots[1].slot_category is ColumnRoleCategory.CATEGORICAL_NOMINAL
    assert slots[2].zero_padded is True
    assert slots[2].extract_sql_hint is not None
    # 생성된 정규식이 원본 표본 전체와 정확히 매칭 + 캡처되는지 검증
    rx = re.compile(d1.regex_pattern)
    m = rx.fullmatch("ORD-2026-KR-00004")
    assert m is not None and m.groups() == ("2026", "KR", "00004")

    # 2. 구분자 없는 문자 클래스 경계 분할: PRJ2026KR001
    no_delim = [
        "PRJ2024KR001",
        "PRJ2025US002",
        "PRJ2026JP003",
        "PRJ2026KR004",
    ]
    d2 = analyze_string_pattern(no_delim)
    assert d2 is not None
    assert d2.strategy == "char_class"
    assert d2.variable_slot_count == 3
    assert any(s.literal_value == "PRJ" for s in d2.segments)
    m2 = re.fullmatch(d2.regex_pattern, "PRJ2025US002")
    assert m2 is not None and m2.groups() == ("2025", "US", "002")

    # 3. 고정폭 숫자 코드(YYYYMMDD + 순번): 202604010001
    fixed_codes = [
        "202604010001",
        "202604010002",
        "202604020003",
        "202604030004",
    ]
    d3 = analyze_string_pattern(fixed_codes)
    assert d3 is not None
    assert d3.strategy == "fixed_width"
    slots3 = [s for s in d3.segments if s.kind is StringSegmentKind.SLOT]
    assert len(slots3) == 2
    assert slots3[0].slot_category is ColumnRoleCategory.TEMPORAL
    assert slots3[1].zero_padded is True

    # 4. 한국어 공백 구분 공통 고정 접두사: 서울시 <구> <동>
    addrs = [
        "서울시 강남구 역삼동",
        "서울시 서초구 서초동",
        "서울시 강남구 삼성동",
        "서울시 송파구 잠실동",
    ]
    d4 = analyze_string_pattern(addrs)
    assert d4 is not None
    assert d4.variable_slot_count == 2
    assert any(s.literal_value == "서울시" for s in d4.segments)

    # 5. 자유 텍스트 문장은 억지로 분해하지 않는다
    prose = [
        "오늘 오후 회의에서 3분기 예산안을 확정했습니다.",
        "고객 요청으로 배송 일정이 하루 연기됨",
        "담당자 검토 완료 후 최종 승인 부탁드립니다.",
        "시스템 점검 공지 발송 완료",
    ]
    assert analyze_string_pattern(prose) is None


def test_detect_ordinal_order_korean_and_english() -> None:
    """한국어/영어 서열 범주 사전과 숫자 단계 접미/접두 서열을 순서대로 인식한다."""
    assert detect_ordinal_order(["높음", "낮음", "보통"]) == ["낮음", "보통", "높음"]
    assert detect_ordinal_order(["3단계", "1단계", "2단계"]) == ["1단계", "2단계", "3단계"]
    assert detect_ordinal_order(["Lv3", "Lv1", "Lv2"]) == ["Lv1", "Lv2", "Lv3"]
    assert detect_ordinal_order(["XL", "S", "M", "L"]) == ["S", "M", "L", "XL"]
    assert detect_ordinal_order(["서울", "부산", "대구"]) == []


def test_end_to_end_column_classification_and_s4_stats() -> None:
    """7대 칼럼 정밀 분류(인덱스·이산/연속수치·범주·날짜·규칙문자열·결측)와 S4 통계를 검증한다."""
    table = NamedTableInput(
        name="orders_rich",
        headers=(
            "row_idx",
            "order_code",
            "erp_batch_no",
            "qty",
            "unit_price",
            "priority",
            "region",
            "order_date",
            "kr_date",
            "is_paid",
            "memo",
        ),
        rows=(
            (
                "1",
                "ORD-2025-KR-0001",
                "202604010001",
                "2",
                "12500.50",
                "1단계",
                "서울",
                "2026-04-01",
                "2026년 4월 1일",
                "Y",
                "배송 전 연락 부탁드립니다.",
            ),
            (
                "2",
                "ORD-2025-US-0002",
                "202604010002",
                "5",
                "8900.25",
                "2단계",
                "부산",
                "2026-04-02",
                "2026년 4월 2일",
                "N",
                "문 앞에 놓아주세요",
            ),
            (
                "3",
                "ORD-2026-KR-0003",
                "202604020003",
                "1",
                "34000.75",
                "3단계",
                "서울",
                "2026-04-05",
                "2026년 4월 5일",
                "Y",
                "포장 꼼꼼히 요청",
            ),
            (
                "4",
                "ORD-2026-JP-0004",
                "202604030004",
                "2",
                "15200.00",
                "2단계",
                "대구",
                "2026-04-06",
                "2026년 4월 6일",
                "Y",
                "",
            ),
            (
                "5",
                "ORD-2026-KR-0005",
                "202604040005",
                "3",
                "999999.99",
                "1단계",
                "부산",
                "2026-04-07",
                "2026년 4월 7일",
                "N",
                "특이사항 없음 및 영수증 동봉",
            ),
        ),
    )

    with MultiTableContext(extra_tables=[table]) as mctx:
        bundles = profile_all_tables(mctx)
        assert len(bundles) == 1
        bundle = bundles[0]

        cls_by = {c.column: c for c in bundle.classifications}
        stat_by = {s.column: s for s in bundle.stats}

        # 1. 인덱스 (단조증가 순번)
        assert cls_by["row_idx"].category is ColumnRoleCategory.INDEX
        assert cls_by["row_idx"].index_meta is not None
        assert cls_by["row_idx"].index_meta.likely_surrogate_index is True
        assert cls_by["row_idx"].index_meta.contiguous_step_rate == 1.0

        # 2. 규칙성 문자열 분해 (ORD-YYYY-CC-NNNN)
        assert cls_by["order_code"].category is ColumnRoleCategory.STRUCTURED_STRING
        assert cls_by["order_code"].string_pattern is not None
        assert cls_by["order_code"].string_pattern.variable_slot_count == 3

        # 3. 12자리 고정폭 복합 숫자 코드 (YYYYMMDD + 순번)
        assert cls_by["erp_batch_no"].category is ColumnRoleCategory.STRUCTURED_STRING
        assert cls_by["erp_batch_no"].string_pattern is not None
        assert cls_by["erp_batch_no"].string_pattern.strategy == "fixed_width"

        # 4. 이산 수치형 vs 연속 수치형 + S4 수치 통계
        assert cls_by["qty"].category is ColumnRoleCategory.DISCRETE_NUMERIC
        assert cls_by["unit_price"].category is ColumnRoleCategory.CONTINUOUS_NUMERIC
        assert stat_by["unit_price"].kind is StatsKind.NUMERIC
        assert stat_by["unit_price"].numeric is not None
        assert stat_by["unit_price"].numeric.max_decimal_digits == 2
        assert stat_by["unit_price"].numeric.outliers.count >= 1  # 999999.99 IQR 이상치

        # 5. 서열 범주형 vs 명목 범주형 + S4 범주 통계
        assert cls_by["priority"].category is ColumnRoleCategory.CATEGORICAL_ORDINAL
        assert cls_by["priority"].ordinal_order == ["1단계", "2단계", "3단계"]
        assert cls_by["region"].category is ColumnRoleCategory.CATEGORICAL_NOMINAL
        assert stat_by["region"].kind is StatsKind.CATEGORICAL
        assert stat_by["region"].categorical is not None
        assert stat_by["region"].categorical.entropy > 0.0

        # 6. 날짜 (ISO + 한국어 날짜) + S4 시계열 통계
        assert cls_by["order_date"].category is ColumnRoleCategory.TEMPORAL
        assert stat_by["order_date"].kind is StatsKind.DATETIME
        assert stat_by["order_date"].temporal is not None
        assert stat_by["order_date"].temporal.minimum == "2026-04-01"
        assert stat_by["order_date"].temporal.gaps.count == 1  # 4/2 → 4/5 결측 구간
        assert cls_by["kr_date"].category is ColumnRoleCategory.TEMPORAL

        # 7. 불리언 & 자유 텍스트(결측 포함)
        assert cls_by["is_paid"].category is ColumnRoleCategory.BOOLEAN
        assert cls_by["memo"].category is ColumnRoleCategory.FREE_TEXT
        assert cls_by["memo"].missing_count == 1
