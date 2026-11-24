"""PDF 기하 원시 객체, 2D 아핀 변환, 페이지 회전·CropBox 정규화 유틸리티."""

from __future__ import annotations

import math
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from autoetl.parser.contracts import BBox

Matrix6 = tuple[float, float, float, float, float, float]
IDENTITY_MATRIX: Matrix6 = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


@dataclass(frozen=True)
class RawTextSpan:
    """페이지 정규화 좌표(좌상단 원점, pt)로 변환된 원시 텍스트 스팬."""

    text: str
    x0: float
    top: float
    x1: float
    bottom: float
    font_name: str
    font_size: float
    is_bold: bool
    is_italic: bool
    orientation: int = 0

    @property
    def width(self) -> float:
        return max(0.0, self.x1 - self.x0)

    @property
    def height(self) -> float:
        return max(0.0, self.bottom - self.top)

    @property
    def y_center(self) -> float:
        return (self.top + self.bottom) / 2.0

    @property
    def x_center(self) -> float:
        return (self.x0 + self.x1) / 2.0


@dataclass(frozen=True)
class RawVectorLine:
    """직선 벡터 세그먼트(표 테두리·구분선·다이어그램 선)."""

    x0: float
    top: float
    x1: float
    bottom: float
    stroke_width: float = 1.0
    is_stroked: bool = True

    @property
    def length(self) -> float:
        return math.hypot(self.x1 - self.x0, self.bottom - self.top)

    @property
    def is_horizontal(self) -> bool:
        return abs(self.bottom - self.top) <= 2.0 and abs(self.x1 - self.x0) >= 8.0

    @property
    def is_vertical(self) -> bool:
        return abs(self.x1 - self.x0) <= 2.0 and abs(self.bottom - self.top) >= 8.0


@dataclass(frozen=True)
class RawVectorRect:
    """축 정렬 벡터 사각형(표 셀/테두리/음영 헤더/막대 그래프)."""

    x0: float
    top: float
    x1: float
    bottom: float
    is_stroked: bool = True
    is_filled: bool = False
    stroke_width: float = 1.0

    @property
    def width(self) -> float:
        return max(0.0, self.x1 - self.x0)

    @property
    def height(self) -> float:
        return max(0.0, self.bottom - self.top)

    @property
    def area(self) -> float:
        return self.width * self.height


@dataclass(frozen=True)
class RawVectorCurve:
    """곡선·다각형·대각선 경로의 바운딩 박스(도식·차트·벡터 그림 탐지용)."""

    x0: float
    top: float
    x1: float
    bottom: float
    segment_count: int = 1


@dataclass(frozen=True)
class RawImagePlacement:
    """페이지 위에 배치된 래스터 이미지 XObject 정보."""

    image_key: str
    x0: float
    top: float
    x1: float
    bottom: float
    pixel_width: int
    pixel_height: int
    filters: tuple[str, ...] = ()
    color_space: str | None = None
    bits_per_component: int = 8
    raw_data: bytes | None = None
    decoded_data: bytes | None = None

    @property
    def width(self) -> float:
        return max(0.0, self.x1 - self.x0)

    @property
    def height(self) -> float:
        return max(0.0, self.bottom - self.top)

    @property
    def area(self) -> float:
        return self.width * self.height


@dataclass(frozen=True)
class RawPageData:
    """한 페이지에서 추출한 모든 원시 기하 데이터."""

    page: int
    width: float
    height: float
    rotation: int
    spans: list[RawTextSpan] = field(default_factory=list)
    lines: list[RawVectorLine] = field(default_factory=list)
    rects: list[RawVectorRect] = field(default_factory=list)
    curves: list[RawVectorCurve] = field(default_factory=list)
    images: list[RawImagePlacement] = field(default_factory=list)


def to_matrix6(raw: Any) -> Matrix6:
    """리스트·시퀀스를 6요소 아핀 행렬 튜플로 안전하게 변환한다."""
    try:
        vals = [float(v) for v in raw]
        if len(vals) == 6:
            return (vals[0], vals[1], vals[2], vals[3], vals[4], vals[5])
    except (TypeError, ValueError):
        pass
    return IDENTITY_MATRIX


def compose_matrix(inner: Matrix6, outer: Matrix6) -> Matrix6:
    """점 p에 `inner`를 먼저 적용하고 그 결과에 `outer`를 적용하는 합성 행렬을 만든다."""
    a1, b1, c1, d1, e1, f1 = inner
    a2, b2, c2, d2, e2, f2 = outer
    return (
        a2 * a1 + c2 * b1,
        b2 * a1 + d2 * b1,
        a2 * c1 + c2 * d1,
        b2 * c1 + d2 * d1,
        a2 * e1 + c2 * f1 + e2,
        b2 * e1 + d2 * f1 + f2,
    )


def apply_matrix(m: Matrix6, x: float, y: float) -> tuple[float, float]:
    """2D 아핀 행렬을 점 (x, y)에 적용한다."""
    a, b, c, d, e, f = m
    return (a * x + c * y + e, b * x + d * y + f)


class PageCoordTransform:
    """PDF 사용자 공간(좌하단 원점 + CropBox + Rotate) → 좌상단 원점 정규 좌표 변환기."""

    def __init__(
        self,
        crop_x0: float,
        crop_y0: float,
        crop_x1: float,
        crop_y1: float,
        rotation: int = 0,
    ) -> None:
        self.crop_x0 = crop_x0
        self.crop_y0 = crop_y0
        self.raw_w = max(1.0, crop_x1 - crop_x0)
        self.raw_h = max(1.0, crop_y1 - crop_y0)
        self.rotation = rotation % 360
        if self.rotation in (90, 270):
            self.page_w = self.raw_h
            self.page_h = self.raw_w
        else:
            self.page_w = self.raw_w
            self.page_h = self.raw_h

    def point(self, x: float, y: float) -> tuple[float, float]:
        """PDF 사용자 공간 점을 정규화 (x, top)으로 바꾼다."""
        u = x - self.crop_x0
        v = y - self.crop_y0
        if self.rotation == 90:
            return (v, u)
        if self.rotation == 180:
            return (self.raw_w - u, v)
        if self.rotation == 270:
            return (self.raw_h - v, self.raw_w - u)
        return (u, self.raw_h - v)

    def rect_from_points(self, pts: list[tuple[float, float]]) -> tuple[float, float, float, float]:
        """PDF 공간 점 집합의 정규화 바운딩 박스 (x0, top, x1, bottom)을 계산한다."""
        transformed = [self.point(px, py) for px, py in pts]
        xs = [p[0] for p in transformed]
        ys = [p[1] for p in transformed]
        return (min(xs), min(ys), max(xs), max(ys))


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def make_bbox(
    x0: float,
    top: float,
    x1: float,
    bottom: float,
    page_w: float,
    page_h: float,
) -> BBox:
    """좌표를 페이지 범위로 클램프하고 정규화 좌표까지 채운 `BBox`를 생성한다."""
    safe_w = max(1.0, page_w)
    safe_h = max(1.0, page_h)
    cx0 = clamp(min(x0, x1), 0.0, safe_w)
    cx1 = clamp(max(x0, x1), 0.0, safe_w)
    ctop = clamp(min(top, bottom), 0.0, safe_h)
    cbot = clamp(max(top, bottom), 0.0, safe_h)
    return BBox(
        x0=round(cx0, 2),
        top=round(ctop, 2),
        x1=round(cx1, 2),
        bottom=round(cbot, 2),
        norm_x0=round(clamp(cx0 / safe_w, 0.0, 1.0), 4),
        norm_top=round(clamp(ctop / safe_h, 0.0, 1.0), 4),
        norm_x1=round(clamp(cx1 / safe_w, 0.0, 1.0), 4),
        norm_bottom=round(clamp(cbot / safe_h, 0.0, 1.0), 4),
    )


def bbox_iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    ix0 = max(a[0], b[0])
    iy0 = max(a[1], b[1])
    ix1 = min(a[2], b[2])
    iy1 = min(a[3], b[3])
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    denom = area_a + area_b - inter
    return inter / denom if denom > 0.0 else 0.0


def rect_contains_center(
    outer: tuple[float, float, float, float],
    inner: tuple[float, float, float, float],
    *,
    margin: float = 1.5,
) -> bool:
    cx = (inner[0] + inner[2]) / 2.0
    cy = (inner[1] + inner[3]) / 2.0
    return (
        outer[0] - margin <= cx <= outer[2] + margin
        and outer[1] - margin <= cy <= outer[3] + margin
    )


def union_area(rects: list[tuple[float, float, float, float]]) -> float:
    """축 정렬 사각형 목록의 합집합 면적을 좌표 압축으로 정확히 계산한다."""
    valid = [(r[0], r[1], r[2], r[3]) for r in rects if r[2] > r[0] and r[3] > r[1]]
    if not valid:
        return 0.0
    xs = sorted({x for r in valid for x in (r[0], r[2])})
    total = 0.0
    for i in range(len(xs) - 1):
        x_left, x_right = xs[i], xs[i + 1]
        dx = x_right - x_left
        if dx <= 0.0:
            continue
        intervals = sorted((r[1], r[3]) for r in valid if r[0] <= x_left and r[2] >= x_right)
        if not intervals:
            continue
        cur_start, cur_end = intervals[0]
        covered_y = 0.0
        for start, end in intervals[1:]:
            if start <= cur_end:
                cur_end = max(cur_end, end)
            else:
                covered_y += cur_end - cur_start
                cur_start, cur_end = start, end
        covered_y += cur_end - cur_start
        total += dx * covered_y
    return total


def estimate_text_width(text: str, font_size: float, font_dict: Any = None) -> float:
    """텍스트 스팬의 가로 폭(pt)을 글꼴 `/Widths` 또는 유니코드 폭 휴리스틱으로 추정한다."""
    if not text or font_size <= 0.0:
        return 0.0

    # 1. 단순 글꼴(Type1/TrueType)의 /Widths + /FirstChar가 있으면 우선 사용
    if font_dict is not None and hasattr(font_dict, "get"):
        try:
            widths_obj = font_dict.get("/Widths")
            first_char = font_dict.get("/FirstChar")
            if widths_obj is not None and first_char is not None:
                if hasattr(widths_obj, "get_object"):
                    widths_obj = widths_obj.get_object()
                first_idx = int(first_char)
                widths_list = list(widths_obj)
                total_em = 0.0
                matched = 0
                for ch in text:
                    code = ord(ch)
                    idx = code - first_idx
                    if 0 <= idx < len(widths_list):
                        total_em += float(widths_list[idx]) / 1000.0
                        matched += 1
                    else:
                        total_em += _char_width_factor(ch)
                if matched > 0:
                    return max(font_size * 0.25, total_em * font_size)
        except Exception:
            pass

    # 2. CJK(한글·한자·가나)와 라틴 문자 비율을 구분한 유니코드 휴리스틱
    total_factor = sum(_char_width_factor(ch) for ch in text)
    return max(font_size * 0.25, total_factor * font_size)


def _char_width_factor(ch: str) -> float:
    if ch == " ":
        return 0.28
    if ch in "ilI1.,:;|!'`":
        return 0.28
    if ch in "mwMW@#%&":
        return 0.78
    eaw = unicodedata.east_asian_width(ch)
    if eaw in ("W", "F"):
        return 0.91
    if ch.isupper():
        return 0.64
    if ch.isdigit():
        return 0.54
    return 0.50


def detect_font_style(font_name: str, font_dict: Any = None) -> tuple[bool, bool]:
    """글꼴 이름과 FontDescriptor 플래그에서 굵게(bold)·기울임(italic) 여부를 판정한다."""
    lower = font_name.lower()
    is_bold = any(tok in lower for tok in ("bold", "black", "heavy", "semibold", "demi", "medi"))
    is_italic = any(tok in lower for tok in ("italic", "oblique", "slant"))

    if font_dict is not None and hasattr(font_dict, "get"):
        try:
            desc = font_dict.get("/FontDescriptor")
            if desc is not None and hasattr(desc, "get_object"):
                desc = desc.get_object()
            if desc is not None and hasattr(desc, "get"):
                weight = desc.get("/FontWeight")
                if weight is not None and float(weight) >= 600:
                    is_bold = True
                flags = desc.get("/Flags")
                if flags is not None:
                    flag_int = int(flags)
                    if flag_int & (1 << 6):  # Italic bit
                        is_italic = True
                    if flag_int & (1 << 18):  # ForceBold bit
                        is_bold = True
        except Exception:
            pass
    return is_bold, is_italic
