"""pdf/geometry 모듈의 오류 경로·경계 값·CMap 폭 추출 단위 테스트."""

from __future__ import annotations

import pytest

from autoetl.parser.pdf.geometry import (
    IDENTITY_MATRIX,
    PageCoordTransform,
    RawImagePlacement,
    RawTextSpan,
    _decode_cmap_hex,
    _extract_type0_unicode_widths,
    apply_matrix,
    bbox_iou,
    compose_matrix,
    detect_font_style,
    estimate_text_width,
    make_bbox,
    to_matrix6,
    union_area,
)


class _Obj:
    """pypdf 간접 객체의 get_object()만 모방한 가짜 래퍼."""

    def __init__(self, value: object) -> None:
        self._value = value

    def get_object(self) -> object:
        return self._value


class _Stream:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def get_data(self) -> bytes:
        return self._data


class _RaisingGet:
    def get(self, key: str) -> object:
        raise RuntimeError("boom")


# ---------------------------------------------------------------------------
# 원시 객체 속성·행렬·좌표 변환
# ---------------------------------------------------------------------------


def test_raw_span_height_and_image_placement_properties() -> None:
    span = RawTextSpan(
        text="가",
        x0=10.0,
        top=20.0,
        x1=30.0,
        bottom=32.0,
        font_name="F",
        font_size=12.0,
        is_bold=False,
        is_italic=False,
    )
    assert span.height == 12.0
    inverted = RawTextSpan(
        text="x",
        x0=30.0,
        top=40.0,
        x1=10.0,
        bottom=20.0,
        font_name="F",
        font_size=12.0,
        is_bold=False,
        is_italic=False,
    )
    assert inverted.height == 0.0
    assert inverted.width == 0.0

    placement = RawImagePlacement(
        image_key="Im0",
        x0=10.0,
        top=20.0,
        x1=110.0,
        bottom=220.0,
        pixel_width=100,
        pixel_height=200,
    )
    assert placement.width == 100.0
    assert placement.height == 200.0
    assert placement.area == 20000.0
    degenerate = RawImagePlacement(
        image_key="Im1",
        x0=50.0,
        top=50.0,
        x1=10.0,
        bottom=10.0,
        pixel_width=1,
        pixel_height=1,
    )
    assert degenerate.area == 0.0


def test_to_matrix6_invalid_inputs_fall_back_to_identity() -> None:
    assert to_matrix6([1, 2, 3, 4, 5, 6]) == (1.0, 2.0, 3.0, 4.0, 5.0, 6.0)
    assert to_matrix6(["1", "2", "3", "4", "5", "6"]) == (1.0, 2.0, 3.0, 4.0, 5.0, 6.0)
    assert to_matrix6(iter([1, 0, 0, 1, 7, 8])) == (1.0, 0.0, 0.0, 1.0, 7.0, 8.0)
    assert to_matrix6([1, 2, 3]) == IDENTITY_MATRIX
    assert to_matrix6([1, 2, 3, 4, 5, "x"]) == IDENTITY_MATRIX
    assert to_matrix6(None) == IDENTITY_MATRIX
    assert to_matrix6(5) == IDENTITY_MATRIX


def test_compose_and_apply_matrix_order() -> None:
    inner = (2.0, 0.0, 0.0, 2.0, 10.0, 20.0)
    outer = (0.0, 1.0, -1.0, 0.0, 5.0, 7.0)
    # inner 먼저 적용: (3,4) → (16,28), outer 적용 → (-23,23).
    assert apply_matrix(inner, 3.0, 4.0) == (16.0, 28.0)
    assert apply_matrix(outer, 16.0, 28.0) == (-23.0, 23.0)
    composed = compose_matrix(inner, outer)
    assert apply_matrix(composed, 3.0, 4.0) == (-23.0, 23.0)
    assert compose_matrix(inner, IDENTITY_MATRIX) == inner
    assert compose_matrix(IDENTITY_MATRIX, inner) == inner


def test_page_coord_transform_rotations_and_degenerate_crop() -> None:
    t0 = PageCoordTransform(5.0, 5.0, 105.0, 205.0, rotation=0)
    assert t0.point(10.0, 20.0) == (5.0, 185.0)

    t90 = PageCoordTransform(5.0, 5.0, 105.0, 205.0, rotation=90)
    assert (t90.page_w, t90.page_h) == (200.0, 100.0)
    assert t90.point(10.0, 20.0) == (15.0, 5.0)

    t180 = PageCoordTransform(5.0, 5.0, 105.0, 205.0, rotation=180)
    assert t180.point(10.0, 20.0) == (95.0, 15.0)
    assert t180.rect_from_points([(10.0, 20.0), (30.0, 40.0)]) == (75.0, 15.0, 95.0, 35.0)

    t270 = PageCoordTransform(5.0, 5.0, 105.0, 205.0, rotation=270)
    assert t270.point(10.0, 20.0) == (185.0, 95.0)

    # 360 초과·음수 회전은 모듈로 정규화된다.
    assert PageCoordTransform(0, 0, 100, 200, rotation=450).point(10.0, 20.0) == (20.0, 10.0)
    assert PageCoordTransform(0, 0, 100, 200, rotation=-90).point(10.0, 20.0) == (180.0, 90.0)

    degenerate = PageCoordTransform(10.0, 10.0, 10.0, 10.0)
    assert (degenerate.raw_w, degenerate.raw_h) == (1.0, 1.0)


# ---------------------------------------------------------------------------
# BBox·IoU·합집합 면적
# ---------------------------------------------------------------------------


def test_make_bbox_swaps_clamps_and_zero_page() -> None:
    swapped = make_bbox(50.0, 100.0, 10.0, 20.0, 100.0, 200.0)
    assert (swapped.x0, swapped.top, swapped.x1, swapped.bottom) == (10.0, 20.0, 50.0, 100.0)
    assert (swapped.norm_x0, swapped.norm_top, swapped.norm_x1, swapped.norm_bottom) == (
        0.1,
        0.1,
        0.5,
        0.5,
    )
    clamped = make_bbox(-10.0, -10.0, 500.0, 500.0, 100.0, 200.0)
    assert (clamped.x0, clamped.top, clamped.x1, clamped.bottom) == (0.0, 0.0, 100.0, 200.0)
    assert clamped.norm_x1 == 1.0 and clamped.norm_bottom == 1.0
    zero_page = make_bbox(0.0, 0.0, 50.0, 50.0, 0.0, 0.0)
    assert (zero_page.x1, zero_page.bottom) == (1.0, 1.0)
    rounded = make_bbox(10.126, 20.0, 30.0, 40.0, 100.0, 100.0)
    assert rounded.x0 == 10.13


def test_bbox_iou_overlap_containment_and_degenerate() -> None:
    assert bbox_iou((0, 0, 10, 10), (0, 0, 10, 10)) == 1.0
    assert bbox_iou((0, 0, 10, 10), (5, 0, 15, 10)) == pytest.approx(1.0 / 3.0)
    assert bbox_iou((0, 0, 10, 10), (2, 2, 8, 8)) == pytest.approx(0.36)
    assert bbox_iou((0, 0, 10, 10), (20, 20, 30, 30)) == 0.0
    assert bbox_iou((0, 0, 10, 10), (10, 0, 20, 10)) == 0.0
    assert bbox_iou((0, 0, 0, 10), (0, 0, 0, 10)) == 0.0


def test_union_area_overlap_gap_nesting_and_degenerate() -> None:
    assert union_area([]) == 0.0
    assert union_area([(0, 0, 0, 10), (5, 5, 5, 5)]) == 0.0
    assert union_area([(0, 0, 10, 10)]) == 100.0
    # 같은 x 슬랩에서 y 구간이 떨어져 있으면 gap 분기로 면적을 누적한다.
    assert union_area([(0, 0, 10, 10), (0, 20, 10, 30)]) == 200.0
    assert union_area([(0, 0, 10, 10), (0, 5, 10, 15)]) == 150.0
    assert union_area([(0, 0, 10, 10), (2, 2, 8, 8)]) == 100.0
    assert union_area([(0, 0, 10, 10), (20, 0, 30, 10)]) == 200.0
    assert union_area([(0, 0, 10, 10), (10, 0, 20, 10)]) == 200.0


# ---------------------------------------------------------------------------
# CMap hex 디코딩
# ---------------------------------------------------------------------------


def test_decode_cmap_hex_edge_cases() -> None:
    assert _decode_cmap_hex("") is None
    assert _decode_cmap_hex("ABC") is None
    assert _decode_cmap_hex("ZZ") is None
    assert _decode_cmap_hex("00") is None
    assert _decode_cmap_hex("41") == "A"
    assert _decode_cmap_hex("000041") is None
    assert _decode_cmap_hex("D800") is None
    assert _decode_cmap_hex("FFFF") is None
    assert _decode_cmap_hex("0000") is None
    assert _decode_cmap_hex("AC00") == "가"
    assert _decode_cmap_hex("00410042") == "AB"


# ---------------------------------------------------------------------------
# Type0 CIDFont 폭 추출
# ---------------------------------------------------------------------------


def test_extract_type0_widths_array_range_forms_and_wrappers() -> None:
    cmap = (
        b"begincmap\n"
        b"3 beginbfchar\n<0001> <0041>\n<0002> <00420043>\n<0003> <0000>\nendbfchar\n"
        b"2 beginbfrange\n<0010> <0012> <0061>\n<0020> <0021> [<0078> <0079>]\nendbfrange\n"
        b"endcmap\n"
    )
    font = {
        "/DescendantFonts": _Obj(
            [
                _Obj(
                    {
                        "/DW": 800,
                        "/W": _Obj(
                            [
                                1,
                                _Obj([500]),  # 배열 형식 + 간접 객체
                                16,
                                18,
                                600,  # 범위 형식
                                32,
                                [700, 750],
                                99999999,
                                100069999,
                                100,  # 비정상 대형 범위 방어
                            ]
                        ),
                    }
                )
            ]
        ),
        "/ToUnicode": _Obj(_Stream(cmap)),
    }
    widths = _extract_type0_unicode_widths(font)
    assert widths is not None
    assert widths["A"] == pytest.approx(0.5)
    assert widths["a"] == pytest.approx(0.6)
    assert widths["b"] == pytest.approx(0.6)
    assert widths["c"] == pytest.approx(0.6)
    assert widths["x"] == pytest.approx(0.7)
    assert widths["y"] == pytest.approx(0.75)
    # 다중 문자 매핑과 0 매핑은 제외된다.
    assert "BC" not in widths and len(widths) == 6


def test_extract_type0_widths_default_dw_and_truncated_w_array() -> None:
    cmap = b"begincmap\n1 beginbfchar\n<0032> <0041>\nendbfchar\nendcmap\n"
    font = {
        "/DescendantFonts": [{"/W": [50, 60]}],  # 범위 값이 없는 미완성 배열 → break
        "/ToUnicode": _Stream(cmap),
    }
    widths = _extract_type0_unicode_widths(font)
    assert widths == {"A": 1.0}  # /DW 기본값 1000


def test_extract_type0_widths_bfrange_guards_and_array_body() -> None:
    cmap = (
        b"begincmap\n"
        b"1 beginbfrange\n<0005> <0003> <0041>\nendbfrange\n"  # hi < lo
        b"1 beginbfrange\n<0001> <2002> <0041>\nendbfrange\n"  # 범위 > 4096
        b"1 beginbfrange\n<0001> <0002> <00410042>\nendbfrange\n"  # 다중 문자 base
        b"1 beginbfrange\n<0001> <0002> <0000>\nendbfrange\n"  # 무효 base
        b"1 beginbfrange\n<0001> <0003> <DBFFDFFF>\nendbfrange\n"  # 코드포인트 상한
        b"1 beginbfrange\n<0001> <0003> [<0041> <0000> <00420043> <0044>]\nendbfrange\n"
        b"endcmap\n"
    )
    font = {"/DescendantFonts": [{}], "/ToUnicode": _Stream(cmap)}
    widths = _extract_type0_unicode_widths(font)
    assert widths is not None
    assert set(widths) == {chr(0x10FFFF), "A"}
    assert widths["A"] == pytest.approx(1.0)


def test_extract_type0_widths_missing_parts_and_exceptions() -> None:
    assert _extract_type0_unicode_widths(None) is None
    assert _extract_type0_unicode_widths(object()) is None
    assert _extract_type0_unicode_widths({}) is None
    assert _extract_type0_unicode_widths({"/DescendantFonts": []}) is None
    assert _extract_type0_unicode_widths({"/DescendantFonts": [None]}) is None
    assert _extract_type0_unicode_widths({"/DescendantFonts": [{}]}) is None
    assert _extract_type0_unicode_widths({"/DescendantFonts": [{}], "/ToUnicode": object()}) is None
    empty_cmap = {"/DescendantFonts": [{}], "/ToUnicode": _Stream(b"begincmap\nendcmap\n")}
    assert _extract_type0_unicode_widths(empty_cmap) is None
    assert _extract_type0_unicode_widths(_RaisingGet()) is None


# ---------------------------------------------------------------------------
# 텍스트 폭 추정
# ---------------------------------------------------------------------------


def test_estimate_text_width_empty_and_nonpositive_size() -> None:
    assert estimate_text_width("", 12.0) == 0.0
    assert estimate_text_width("A", 0.0) == 0.0
    assert estimate_text_width("A", -3.0) == 0.0


def test_estimate_text_width_simple_font_widths_and_fallback() -> None:
    font = {"/Widths": [500, 600], "/FirstChar": 65}
    assert estimate_text_width("AB", 10.0, font) == pytest.approx(11.0)
    # 목록 밖 문자는 유니코드 휴리스틱으로 보정한다(É는 대문자 0.64).
    assert estimate_text_width("AÉ", 10.0, font) == pytest.approx(5.0 + 6.4)

    wrapped = {"/Widths": _Obj([500, 600]), "/FirstChar": 65}
    assert estimate_text_width("AB", 10.0, wrapped) == pytest.approx(11.0)

    # 매칭 문자가 하나도 없으면 휴리스틱으로 폴백한다.
    assert estimate_text_width("©", 10.0, font) == pytest.approx(5.0)

    # /FirstChar 파싱 실패는 예외를 삼키고 휴리스틱으로 폴백한다.
    broken = {"/Widths": [500], "/FirstChar": "abc"}
    assert estimate_text_width("A", 10.0, broken) == pytest.approx(6.4)


def test_estimate_text_width_cache_hit_and_none_cache() -> None:
    font_a: dict[str, object] = {}
    cache: dict[int, dict[str, float] | None] = {id(font_a): {"A": 0.5}}
    assert estimate_text_width("A", 10.0, font_a, width_cache=cache) == pytest.approx(5.0)

    font_b: dict[str, object] = {}
    cache2: dict[int, dict[str, float] | None] = {}
    assert estimate_text_width("A", 10.0, font_b, width_cache=cache2) == pytest.approx(6.4)
    assert cache2[id(font_b)] is None
    # None 캐시 히트도 휴리스틱으로 폴백한다.
    assert estimate_text_width("A", 10.0, font_b, width_cache=cache2) == pytest.approx(6.4)


def test_estimate_text_width_cid_map_unmatched_char_fallback() -> None:
    cmap = b"begincmap\n1 beginbfchar\n<0001> <0041>\nendbfchar\nendcmap\n"
    font = {
        "/DescendantFonts": [{"/DW": 1000, "/W": [1, [500]]}],
        "/ToUnicode": _Stream(cmap),
    }
    # A는 실측 0.5em, z는 맵에 없어 휴리스틱 0.5em.
    assert estimate_text_width("Az", 10.0, font) == pytest.approx(10.0)


def test_char_width_heuristic_categories() -> None:
    assert estimate_text_width(" ", 10.0) == pytest.approx(2.8)
    assert estimate_text_width("i", 10.0) == pytest.approx(2.8)
    assert estimate_text_width("m", 10.0) == pytest.approx(7.8)
    assert estimate_text_width("가", 10.0) == pytest.approx(9.7)
    assert estimate_text_width("\uff21", 10.0) == pytest.approx(9.7)  # 전각 A (F 카테고리)
    assert estimate_text_width("Z", 10.0) == pytest.approx(6.4)
    assert estimate_text_width("7", 10.0) == pytest.approx(5.6)
    assert estimate_text_width("x", 10.0) == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# 글꼴 스타일 판정
# ---------------------------------------------------------------------------


def test_detect_font_style_name_tokens() -> None:
    assert detect_font_style("Some-Bold") == (True, False)
    assert detect_font_style("Some-BlackItalic") == (True, True)
    assert detect_font_style("HeavyOblique") == (True, True)
    assert detect_font_style("SemiboldSlant") == (True, True)
    assert detect_font_style("DemiMediHeadline-H2Hdr") == (True, False)
    assert detect_font_style("Medium") == (True, False)
    assert detect_font_style("Regular") == (False, False)


def test_detect_font_style_descriptor_weight_flags_and_fallbacks() -> None:
    assert detect_font_style("Regular", {"/FontDescriptor": {"/FontWeight": 700}}) == (
        True,
        False,
    )
    assert detect_font_style("Regular", {"/FontDescriptor": {"/FontWeight": 400}}) == (
        False,
        False,
    )
    assert detect_font_style("Regular", {"/FontDescriptor": {"/Flags": 64}}) == (False, True)
    assert detect_font_style("Regular", {"/FontDescriptor": {"/Flags": 1 << 18}}) == (
        True,
        False,
    )
    assert detect_font_style("Regular", {"/FontDescriptor": {"/Flags": "64"}}) == (False, True)

    wrapped = {
        "/FontDescriptor": _Obj({"/FontWeight": 600, "/Flags": 64 | (1 << 18)}),
    }
    assert detect_font_style("Regular", wrapped) == (True, True)

    # /FontDescriptor가 없으면 DescendantFonts의 첫 폰트에서 찾는다.
    descendant = {
        "/DescendantFonts": _Obj([_Obj({"/FontDescriptor": {"/Flags": 64}})]),
    }
    assert detect_font_style("Regular", descendant) == (False, True)

    assert detect_font_style("Regular", {"/FontDescriptor": None}) == (False, False)
    assert detect_font_style("Regular", object()) == (False, False)
    # descriptor 접근 예외는 이름 기반 결과를 유지한다.
    assert detect_font_style("Plain-Bold", {"/FontDescriptor": _RaisingGet()}) == (True, False)
