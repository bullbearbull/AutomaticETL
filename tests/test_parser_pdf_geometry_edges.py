"""PDF 기하(geometry) 원시 객체·아핀 변환·CMap 폭 추정·글꼴 스타일 판정 경계 테스트."""

from __future__ import annotations

import pytest

from autoetl.parser.pdf.geometry import (
    IDENTITY_MATRIX,
    PageCoordTransform,
    RawImagePlacement,
    RawTextSpan,
    RawVectorLine,
    RawVectorRect,
    _char_width_factor,
    _decode_cmap_hex,
    _extract_type0_unicode_widths,
    apply_matrix,
    bbox_iou,
    clamp,
    compose_matrix,
    detect_font_style,
    estimate_text_width,
    make_bbox,
    rect_contains_center,
    to_matrix6,
    union_area,
)


class _Indirect:
    """pypdf 간접 참조를 흉내 내는 가짜 객체."""

    def __init__(self, obj: object) -> None:
        self._obj = obj

    def get_object(self) -> object:
        return self._obj


class _CmapStream:
    """ToUnicode CMap 스트림을 흉내 내는 가짜 객체."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    def get_data(self) -> bytes:
        return self._data


def _type0_font(
    cmap: str,
    *,
    w: list[object] | None = None,
    dw: int | None = 500,
) -> dict[str, object]:
    desc: dict[str, object] = {}
    if dw is not None:
        desc["/DW"] = dw
    if w is not None:
        desc["/W"] = w
    return {
        "/DescendantFonts": [desc],
        "/ToUnicode": _CmapStream(cmap.encode("latin-1")),
    }


# ---------------------------------------------------------------------------
# 원시 기하 객체 프로퍼티
# ---------------------------------------------------------------------------


def test_raw_text_span_properties_and_inverted_coords() -> None:
    span = RawTextSpan("가나", 10.0, 20.0, 30.0, 40.0, "FakeFont", 12.0, False, False)
    assert span.width == 20.0
    assert span.height == 20.0
    assert span.x_center == 20.0
    assert span.y_center == 30.0
    inverted = RawTextSpan("x", 30.0, 40.0, 10.0, 20.0, "F", 10.0, False, False)
    assert inverted.width == 0.0
    assert inverted.height == 0.0


def test_raw_vector_line_length_and_axis_boundaries() -> None:
    diag = RawVectorLine(0.0, 0.0, 3.0, 4.0)
    assert diag.length == 5.0
    assert diag.is_horizontal is False
    assert diag.is_vertical is False

    assert RawVectorLine(0.0, 0.0, 10.0, 2.0).is_horizontal is True
    assert RawVectorLine(0.0, 0.0, 10.0, 2.5).is_horizontal is False
    assert RawVectorLine(0.0, 0.0, 7.0, 0.0).is_horizontal is False
    assert RawVectorLine(0.0, 0.0, 2.0, 10.0).is_vertical is True
    assert RawVectorLine(0.0, 0.0, 2.5, 10.0).is_vertical is False
    assert RawVectorLine(0.0, 0.0, 0.0, 7.0).is_vertical is False


def test_raw_vector_rect_and_image_placement_areas() -> None:
    rect = RawVectorRect(0.0, 0.0, 3.0, 4.0)
    assert (rect.width, rect.height, rect.area) == (3.0, 4.0, 12.0)
    inverted = RawVectorRect(10.0, 10.0, 5.0, 5.0)
    assert (inverted.width, inverted.height, inverted.area) == (0.0, 0.0, 0.0)

    img = RawImagePlacement("Im0", 0.0, 0.0, 8.0, 6.0, 8, 6)
    assert (img.width, img.height, img.area) == (8.0, 6.0, 48.0)
    img_inv = RawImagePlacement("Im1", 8.0, 6.0, 0.0, 0.0, 8, 6)
    assert img_inv.area == 0.0


# ---------------------------------------------------------------------------
# 아핀 행렬 변환
# ---------------------------------------------------------------------------


def test_to_matrix6_valid_invalid_and_iterator_inputs() -> None:
    assert to_matrix6([1, 2, 3, 4, 5, 6]) == (1.0, 2.0, 3.0, 4.0, 5.0, 6.0)
    assert to_matrix6(iter(range(6))) == (0.0, 1.0, 2.0, 3.0, 4.0, 5.0)
    assert to_matrix6([1, 2, 3]) == IDENTITY_MATRIX
    assert to_matrix6(["a", "b", "c", "d", "e", "f"]) == IDENTITY_MATRIX
    assert to_matrix6(None) == IDENTITY_MATRIX


def test_compose_matrix_applies_inner_then_outer() -> None:
    inner = (1.0, 0.0, 0.0, 1.0, 10.0, 20.0)  # 평행이동
    outer = (2.0, 0.0, 0.0, 2.0, 3.0, 4.0)  # 배율 + 평행이동
    composed = compose_matrix(inner, outer)
    assert apply_matrix(composed, 1.0, 1.0) == (25.0, 46.0)
    stepwise = apply_matrix(outer, *apply_matrix(inner, 1.0, 1.0))
    assert apply_matrix(composed, 1.0, 1.0) == stepwise
    assert compose_matrix(inner, IDENTITY_MATRIX) == inner
    assert apply_matrix(IDENTITY_MATRIX, 7.0, -3.0) == (7.0, -3.0)


# ---------------------------------------------------------------------------
# PageCoordTransform: 회전·CropBox 경계
# ---------------------------------------------------------------------------


def test_page_coord_transform_rotation_0_and_degenerate_crop() -> None:
    t = PageCoordTransform(10.0, 20.0, 110.0, 220.0, rotation=0)
    assert (t.page_w, t.page_h) == (100.0, 200.0)
    assert t.point(10.0, 220.0) == (0.0, 0.0)  # 좌상단
    assert t.point(110.0, 20.0) == (100.0, 200.0)  # 우하단
    assert t.point(60.0, 120.0) == (50.0, 100.0)

    degenerate = PageCoordTransform(5.0, 5.0, 5.0, 5.0)
    assert (degenerate.raw_w, degenerate.raw_h) == (1.0, 1.0)
    assert degenerate.point(5.0, 5.0) == (0.0, 1.0)


@pytest.mark.parametrize(
    ("rotation", "expected_size", "corner_bl", "corner_tr"),
    [
        (90, (200.0, 100.0), (0.0, 0.0), (200.0, 100.0)),
        (180, (100.0, 200.0), (100.0, 0.0), (0.0, 200.0)),
        (270, (200.0, 100.0), (200.0, 100.0), (0.0, 0.0)),
    ],
)
def test_page_coord_transform_rotations(
    rotation: int,
    expected_size: tuple[float, float],
    corner_bl: tuple[float, float],
    corner_tr: tuple[float, float],
) -> None:
    t = PageCoordTransform(10.0, 20.0, 110.0, 220.0, rotation=rotation)
    assert (t.page_w, t.page_h) == expected_size
    # PDF 좌하단(10,20)과 우상단(110,220) 코너의 정규화 위치.
    assert t.point(10.0, 20.0) == corner_bl
    assert t.point(110.0, 220.0) == corner_tr


def test_page_coord_transform_normalizes_rotation_and_rect_bbox() -> None:
    assert PageCoordTransform(0, 0, 100, 200, rotation=450).rotation == 90
    assert PageCoordTransform(0, 0, 100, 200, rotation=-90).rotation == 270

    t = PageCoordTransform(10.0, 20.0, 110.0, 220.0)
    bbox = t.rect_from_points([(10.0, 20.0), (60.0, 120.0), (110.0, 220.0)])
    assert bbox == (0.0, 0.0, 100.0, 200.0)


# ---------------------------------------------------------------------------
# clamp / make_bbox / bbox_iou / rect_contains_center / union_area
# ---------------------------------------------------------------------------


def test_clamp_and_make_bbox_swapped_out_of_range_and_tiny_page() -> None:
    assert clamp(5.0, 0.0, 3.0) == 3.0
    assert clamp(-1.0, 0.0, 3.0) == 0.0
    assert clamp(2.0, 0.0, 3.0) == 2.0

    swapped = make_bbox(80.0, 90.0, 20.0, 30.0, 100.0, 100.0)
    assert (swapped.x0, swapped.top, swapped.x1, swapped.bottom) == (20.0, 30.0, 80.0, 90.0)
    assert (swapped.norm_x0, swapped.norm_top, swapped.norm_x1, swapped.norm_bottom) == (
        0.2,
        0.3,
        0.8,
        0.9,
    )

    outside = make_bbox(-10.0, -20.0, 300.0, 400.0, 100.0, 100.0)
    assert (outside.x0, outside.top, outside.x1, outside.bottom) == (0.0, 0.0, 100.0, 100.0)
    assert (outside.norm_x0, outside.norm_x1) == (0.0, 1.0)

    tiny = make_bbox(0.5, 0.5, 2.0, 2.0, 0.0, 0.0)
    assert (tiny.x1, tiny.bottom) == (1.0, 1.0)

    rounded = make_bbox(10.126, 20.0, 30.999, 40.0, 100.0, 100.0)
    assert (rounded.x0, rounded.x1) == (10.13, 31.0)


def test_bbox_iou_identical_disjoint_touching_partial_and_zero_area() -> None:
    box = (0.0, 0.0, 10.0, 10.0)
    assert bbox_iou(box, box) == 1.0
    assert bbox_iou(box, (20.0, 20.0, 30.0, 30.0)) == 0.0
    assert bbox_iou(box, (10.0, 0.0, 20.0, 10.0)) == 0.0  # 모서리만 접촉
    assert bbox_iou(box, (5.0, 0.0, 15.0, 10.0)) == pytest.approx(1.0 / 3.0)
    assert bbox_iou(box, (2.0, 2.0, 4.0, 4.0)) == pytest.approx(0.04)
    assert bbox_iou((0.0, 0.0, 0.0, 10.0), box) == 0.0


def test_rect_contains_center_margin_is_inclusive() -> None:
    outer = (0.0, 0.0, 10.0, 10.0)
    assert rect_contains_center(outer, (2.0, 2.0, 4.0, 4.0)) is True
    assert rect_contains_center(outer, (20.0, 20.0, 30.0, 30.0)) is False
    # 중심 x=11.5는 outer.x1 + margin(1.5) 경계와 정확히 일치 → 포함.
    assert rect_contains_center(outer, (10.0, 4.0, 13.0, 6.0)) is True
    assert rect_contains_center(outer, (11.0, 4.0, 13.0, 6.0)) is False
    assert rect_contains_center(outer, (10.0, 4.0, 13.0, 6.0), margin=0.0) is False


def test_union_area_empty_degenerate_overlap_nested_and_touching() -> None:
    assert union_area([]) == 0.0
    assert union_area([(0.0, 0.0, 0.0, 5.0), (1.0, 1.0, 1.0, 2.0)]) == 0.0
    assert union_area([(0.0, 0.0, 10.0, 10.0)]) == 100.0
    assert union_area([(0.0, 0.0, 10.0, 10.0), (5.0, 5.0, 15.0, 15.0)]) == 175.0
    assert union_area([(0.0, 0.0, 10.0, 10.0), (20.0, 0.0, 30.0, 10.0)]) == 200.0
    assert union_area([(0.0, 0.0, 10.0, 10.0), (2.0, 2.0, 4.0, 4.0)]) == 100.0
    assert union_area([(0.0, 0.0, 10.0, 10.0), (10.0, 0.0, 20.0, 10.0)]) == 200.0
    triple = [
        (0.0, 0.0, 10.0, 10.0),
        (5.0, 0.0, 15.0, 10.0),
        (0.0, 5.0, 10.0, 15.0),
    ]
    assert union_area(triple) == 200.0


# ---------------------------------------------------------------------------
# CMap hex 디코딩
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("hex_str", "expected"),
    [
        ("", None),
        ("0", None),  # 홀수 길이 hex
        ("00", None),  # 0 코드포인트
        ("41", "A"),
        ("0041", "A"),
        ("D800", None),  # 단독 서로게이트
        ("FFFF", None),
        ("000000", None),  # 3바이트(홀수) UTF-16
        ("00410042", "AB"),
    ],
)
def test_decode_cmap_hex_boundaries(hex_str: str, expected: str | None) -> None:
    assert _decode_cmap_hex(hex_str) == expected


# ---------------------------------------------------------------------------
# Type0 CIDFont 유니코드 폭 추출
# ---------------------------------------------------------------------------


def test_extract_type0_widths_bfchar_bfrange_and_skips() -> None:
    cmap = (
        "beginbfchar\n"
        "<0001> <0041>\n"  # A, cid1=250
        "<0002> <0042>\n"  # B, cid2=750
        "<0003> <00430044>\n"  # 2문자 매핑 → 제외
        "<0004> <0000>\n"  # 0 코드포인트 → 제외
        "endbfchar\n"
        "beginbfrange\n"
        "<000A> <000C> <0061>\n"  # a,b,c ← cid10..12=900
        "endbfrange\n"
    )
    font = _type0_font(cmap, w=[1, [250, 750], 10, 12, 900])
    widths = _extract_type0_unicode_widths(font)
    assert widths == {"A": 0.25, "B": 0.75, "a": 0.9, "b": 0.9, "c": 0.9}


def test_extract_type0_widths_default_dw_and_indirect_objects() -> None:
    cmap = "beginbfchar\n<0005> <0045>\nendbfchar\n"
    font = _type0_font(cmap, dw=None)
    assert _extract_type0_unicode_widths(font) == {"E": 1.0}

    desc: dict[str, object] = {"/DW": 500, "/W": _Indirect([5, [400]])}
    wrapped: dict[str, object] = {
        "/DescendantFonts": _Indirect([_Indirect(desc)]),
        "/ToUnicode": _Indirect(_CmapStream(b"beginbfchar\n<0005> <0045>\nendbfchar\n")),
    }
    assert _extract_type0_unicode_widths(wrapped) == {"E": 0.4}


def test_extract_type0_widths_guard_huge_range_and_malformed_w() -> None:
    cmap = "beginbfchar\n<0005> <0045>\nendbfchar\n"
    # 65535 초과 범위는 무시되고 기본 DW가 쓰인다.
    guarded = _type0_font(cmap, w=[0, 70000, 500])
    assert _extract_type0_unicode_widths(guarded) == {"E": 0.5}
    # float 변환 불가 항목은 전체 None(안전 폴백).
    malformed = _type0_font(cmap, w=[1, 2, "abc"])
    assert _extract_type0_unicode_widths(malformed) is None
    # 꼬리가 잘린 W 배열은 조용히 무시된다.
    truncated = _type0_font(cmap, w=[5])
    assert _extract_type0_unicode_widths(truncated) == {"E": 0.5}


def test_extract_type0_widths_bfrange_array_form_and_limits() -> None:
    cmap = (
        "beginbfrange\n"
        "<0020> <0021> [<0041> <0042> <0043>]\n"  # 세 번째 토큰은 hi 초과 → 중단
        "<0005> <0003> <0041>\n"  # hi < lo → 제외
        "<0000> <2000> <0041>\n"  # 4096 초과 범위 → 제외
        "<0030> <0030> <00410042>\n"  # 2문자 dst → 제외
        "endbfrange\n"
    )
    widths = _extract_type0_unicode_widths(_type0_font(cmap))
    assert widths == {"A": 0.5, "B": 0.5}


def test_extract_type0_widths_codepoint_overflow_and_zero_width() -> None:
    # U+10FFFF에서 시작하는 범위: 두 번째 코드포인트는 상한 초과로 중단.
    cmap = "beginbfrange\n<0000> <0001> <DBFFDFFF>\nendbfrange\n"
    widths = _extract_type0_unicode_widths(_type0_font(cmap))
    assert widths == {chr(0x10FFFF): 0.5}

    zero = _type0_font("beginbfchar\n<0001> <0041>\nendbfchar\n", w=[1, [0]])
    assert _extract_type0_unicode_widths(zero) is None


def test_extract_type0_widths_first_mapping_wins() -> None:
    cmap = "beginbfchar\n<0001> <0041>\nendbfchar\nbeginbfrange\n<0002> <0002> <0041>\nendbfrange\n"
    font = _type0_font(cmap, w=[1, [250], 2, [750]])
    assert _extract_type0_unicode_widths(font) == {"A": 0.25}


def test_extract_type0_widths_missing_parts_return_none() -> None:
    assert _extract_type0_unicode_widths(None) is None
    assert _extract_type0_unicode_widths(object()) is None
    assert _extract_type0_unicode_widths({}) is None
    assert _extract_type0_unicode_widths({"/DescendantFonts": []}) is None
    assert _extract_type0_unicode_widths({"/DescendantFonts": [None]}) is None
    assert _extract_type0_unicode_widths({"/DescendantFonts": [object()]}) is None
    no_tu: dict[str, object] = {"/DescendantFonts": [{"/DW": 500}]}
    assert _extract_type0_unicode_widths(no_tu) is None
    bad_tu: dict[str, object] = {
        "/DescendantFonts": [{"/DW": 500}],
        "/ToUnicode": object(),
    }
    assert _extract_type0_unicode_widths(bad_tu) is None


# ---------------------------------------------------------------------------
# estimate_text_width: /Widths·Type0·휴리스틱·캐시
# ---------------------------------------------------------------------------


def test_estimate_text_width_empty_and_non_positive_size() -> None:
    assert estimate_text_width("", 12.0) == 0.0
    assert estimate_text_width("가", 0.0) == 0.0
    assert estimate_text_width("가", -3.0) == 0.0


def test_estimate_text_width_simple_widths_matched_and_fallback() -> None:
    font = {"/Widths": [500, 600], "/FirstChar": 65}
    assert estimate_text_width("AB", 10.0, font) == pytest.approx(11.0)
    # Z는 /Widths 밖 → 대문자 휴리스틱 0.64로 보정.
    assert estimate_text_width("AZ", 10.0, font) == pytest.approx(11.4)
    # 하나도 매칭되지 않으면 전체 휴리스틱 경로.
    assert estimate_text_width("zz", 10.0, font) == pytest.approx(10.0)
    # 최소 폭 클램프(font_size * 0.25).
    tiny = {"/Widths": [10], "/FirstChar": 65}
    assert estimate_text_width("A", 10.0, tiny) == pytest.approx(2.5)
    indirect = {"/Widths": _Indirect([500]), "/FirstChar": 65}
    assert estimate_text_width("A", 10.0, indirect) == pytest.approx(5.0)
    # FirstChar 파싱 실패는 예외 없이 휴리스틱으로 폴백.
    broken = {"/Widths": [500], "/FirstChar": "abc"}
    assert estimate_text_width("A", 10.0, broken) == pytest.approx(6.4)


def test_estimate_text_width_type0_with_cache_and_partial_match() -> None:
    cmap = "beginbfchar\n<0001> <0041>\n<0002> <0042>\nendbfchar\n"
    font = _type0_font(cmap, w=[1, [250, 750]])
    cache: dict[int, dict[str, float] | None] = {}
    assert estimate_text_width("AB", 10.0, font, width_cache=cache) == pytest.approx(10.0)
    assert id(font) in cache
    # 캐시 히트 경로에서도 동일 결과.
    assert estimate_text_width("AB", 10.0, font, width_cache=cache) == pytest.approx(10.0)
    # 미매칭 문자는 휴리스틱과 합산(가=0.97).
    assert estimate_text_width("A가", 10.0, font, width_cache=cache) == pytest.approx(12.2)
    # 캐시에 None이 미리 있으면 추출을 건너뛰고 휴리스틱을 쓴다.
    font2 = _type0_font(cmap, w=[1, [250, 750]])
    cache2: dict[int, dict[str, float] | None] = {id(font2): None}
    assert estimate_text_width("A", 10.0, font2, width_cache=cache2) == pytest.approx(6.4)


def test_char_width_factor_categories() -> None:
    assert _char_width_factor(" ") == 0.28
    assert _char_width_factor("i") == 0.28
    assert _char_width_factor("m") == 0.78
    assert _char_width_factor("M") == 0.78
    assert _char_width_factor("가") == 0.97
    assert _char_width_factor(chr(0xFF21)) == 0.97  # 전각 라틴 대문자 A(EAW=F)
    assert _char_width_factor("A") == 0.64
    assert _char_width_factor("1") == 0.56
    assert _char_width_factor("z") == 0.50


# ---------------------------------------------------------------------------
# detect_font_style: 이름 토큰·FontDescriptor 플래그
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "bold", "italic"),
    [
        ("Helvetica-BoldOblique", True, True),
        ("ArialMT", False, False),
        ("SomeBlack", True, False),
        ("HeavyFace", True, False),
        ("Semibold", True, False),
        ("DemiCondensed", True, False),
        ("Medium", True, False),
        ("Headline", True, False),
        ("H2Hdr", True, False),
        ("Slanted", False, True),
        ("Oblique", False, True),
    ],
)
def test_detect_font_style_name_tokens(name: str, bold: bool, italic: bool) -> None:
    assert detect_font_style(name) == (bold, italic)


def test_detect_font_style_descriptor_weight_and_flags() -> None:
    heavy = {"/FontDescriptor": {"/FontWeight": 700}}
    assert detect_font_style("Arial", heavy) == (True, False)
    normal = {"/FontDescriptor": {"/FontWeight": 400}}
    assert detect_font_style("Arial", normal) == (False, False)

    italic_flag = {"/FontDescriptor": {"/Flags": 1 << 6}}
    assert detect_font_style("Arial", italic_flag) == (False, True)
    force_bold = {"/FontDescriptor": {"/Flags": 1 << 18}}
    assert detect_font_style("Arial", force_bold) == (True, False)
    both = {"/FontDescriptor": {"/Flags": (1 << 6) | (1 << 18)}}
    assert detect_font_style("Arial", both) == (True, True)


def test_detect_font_style_descendant_indirect_and_broken_descriptor() -> None:
    indirect = {
        "/DescendantFonts": _Indirect(
            [_Indirect({"/FontDescriptor": _Indirect({"/Flags": 1 << 6})})]
        )
    }
    assert detect_font_style("Arial", indirect) == (False, True)

    plain_list = {"/DescendantFonts": [{"/FontDescriptor": {"/FontWeight": 800}}]}
    assert detect_font_style("Arial", plain_list) == (True, False)

    # FontWeight float 변환 실패는 이름 기반 결과를 유지한다.
    broken = {"/FontDescriptor": {"/FontWeight": "heavy"}}
    assert detect_font_style("Arial", broken) == (False, False)
    # get이 없는 descriptor는 무시한다.
    assert detect_font_style("Arial", {"/FontDescriptor": "not-a-dict"}) == (False, False)
