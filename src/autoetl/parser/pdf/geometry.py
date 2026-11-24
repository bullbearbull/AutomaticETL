"""PDF 기하 원시 객체, 2D 아핀 변환, 페이지 회전·CropBox 정규화 유틸리티."""

from __future__ import annotations

import math
import re
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
    fill_color: tuple[float, float, float] | None = None

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


_BFCHAR_BLOCK_RE = re.compile(r"beginbfchar\s+(.*?)\s+endbfchar", re.DOTALL)
_BFRANGE_BLOCK_RE = re.compile(r"beginbfrange\s+(.*?)\s+endbfrange", re.DOTALL)
_BFCHAR_PAIR_RE = re.compile(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>")
_BFRANGE_ENTRY_RE = re.compile(
    r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(?:<([0-9A-Fa-f]+)>|\[([^\]]+)\])"
)
_HEX_TOKEN_RE = re.compile(r"<([0-9A-Fa-f]+)>")


def _decode_cmap_hex(hex_str: str) -> str | None:
    if not hex_str or len(hex_str) % 2 != 0:
        return None
    try:
        raw = bytes.fromhex(hex_str)
    except ValueError:
        return None
    if len(raw) == 1:
        val = raw[0]
        return chr(val) if val > 0 else None
    if len(raw) % 2 != 0:
        return None
    try:
        decoded = raw.decode("utf-16-be")
    except UnicodeDecodeError:
        return None
    if not decoded or decoded in ("\uffff", "\x00"):
        return None
    return decoded


def _extract_type0_unicode_widths(font_dict: Any) -> dict[str, float] | None:
    """Type0 CIDFont(`/DW`, `/W`, `/ToUnicode`)에서 문자→em 폭 맵을 추출한다."""
    if font_dict is None or not hasattr(font_dict, "get"):
        return None
    try:
        descs = font_dict.get("/DescendantFonts")
        if descs is None:
            return None
        if hasattr(descs, "get_object"):
            descs = descs.get_object()
        if not descs:
            return None
        desc_font = descs[0]
        if hasattr(desc_font, "get_object"):
            desc_font = desc_font.get_object()
        if desc_font is None or not hasattr(desc_font, "get"):
            return None

        dw_raw = desc_font.get("/DW")
        default_w = float(dw_raw) if dw_raw is not None else 1000.0
        cid_widths: dict[int, float] = {}
        w_arr = desc_font.get("/W")
        if w_arr is not None:
            if hasattr(w_arr, "get_object"):
                w_arr = w_arr.get_object()
            items = list(w_arr)
            i = 0
            while i + 1 < len(items):
                c_start = int(items[i])
                nxt = items[i + 1]
                if hasattr(nxt, "get_object"):
                    nxt = nxt.get_object()
                if isinstance(nxt, list):
                    for off, val in enumerate(nxt):
                        cid_widths[c_start + off] = float(val)
                    i += 2
                elif i + 2 < len(items):
                    c_end = int(nxt)
                    val = float(items[i + 2])
                    # 비정상적으로 거대한 범위를 방어한다
                    if c_end >= c_start and (c_end - c_start) <= 65535:
                        for cid in range(c_start, c_end + 1):
                            cid_widths[cid] = val
                    i += 3
                else:
                    break

        tu = font_dict.get("/ToUnicode")
        if tu is None:
            return None
        if hasattr(tu, "get_object"):
            tu = tu.get_object()
        if not hasattr(tu, "get_data"):
            return None
        cmap_text = bytes(tu.get_data()).decode("latin-1", errors="ignore")

        uni_widths: dict[str, float] = {}
        for block in _BFCHAR_BLOCK_RE.findall(cmap_text):
            for cid_hex, uni_hex in _BFCHAR_PAIR_RE.findall(block):
                ch = _decode_cmap_hex(uni_hex)
                if ch is None or len(ch) != 1:
                    continue
                cid = int(cid_hex, 16)
                w_em = cid_widths.get(cid, default_w) / 1000.0
                if w_em > 0.0:
                    uni_widths.setdefault(ch, w_em)

        for block in _BFRANGE_BLOCK_RE.findall(cmap_text):
            for lo_hex, hi_hex, dst_hex, arr_body in _BFRANGE_ENTRY_RE.findall(block):
                lo = int(lo_hex, 16)
                hi = int(hi_hex, 16)
                if hi < lo or (hi - lo) > 4096:
                    continue
                if dst_hex:
                    base_uni = _decode_cmap_hex(dst_hex)
                    if base_uni is None or len(base_uni) != 1:
                        continue
                    base_cp = ord(base_uni)
                    for off in range(hi - lo + 1):
                        cp = base_cp + off
                        if cp > 0x10FFFF:
                            break
                        ch = chr(cp)
                        w_em = cid_widths.get(lo + off, default_w) / 1000.0
                        if w_em > 0.0:
                            uni_widths.setdefault(ch, w_em)
                elif arr_body:
                    for off, tok_hex in enumerate(_HEX_TOKEN_RE.findall(arr_body)):
                        if lo + off > hi:
                            break
                        ch = _decode_cmap_hex(tok_hex)
                        if ch is None or len(ch) != 1:
                            continue
                        w_em = cid_widths.get(lo + off, default_w) / 1000.0
                        if w_em > 0.0:
                            uni_widths.setdefault(ch, w_em)

        return uni_widths or None
    except Exception:
        return None


def estimate_text_width(
    text: str,
    font_size: float,
    font_dict: Any = None,
    *,
    width_cache: dict[int, dict[str, float] | None] | None = None,
) -> float:
    """스팬 가로 폭(pt)을 `/Widths`·CIDFont `/W` 또는 유니코드 폭 휴리스틱으로 추정한다."""
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

        # 2. Type0 CIDFont의 /DescendantFonts(/W, /DW) + /ToUnicode 실측 폭 사용
        cache_key = id(font_dict)
        if width_cache is not None and cache_key in width_cache:
            cid_uni_widths = width_cache[cache_key]
        else:
            cid_uni_widths = _extract_type0_unicode_widths(font_dict)
            if width_cache is not None:
                width_cache[cache_key] = cid_uni_widths
        if cid_uni_widths:
            total_em = 0.0
            matched = 0
            for ch in text:
                w_em = cid_uni_widths.get(ch)
                if w_em is not None:
                    total_em += w_em
                    matched += 1
                else:
                    total_em += _char_width_factor(ch)
            if matched > 0:
                return max(font_size * 0.25, total_em * font_size)

    # 3. CJK(한글·한자·가나)와 라틴 문자 비율을 구분한 유니코드 휴리스틱
    total_factor = sum(_char_width_factor(ch) for ch in text)
    return max(font_size * 0.25, total_factor * font_size)


def _char_width_factor(ch: str) -> float:
    if ch == " ":
        return 0.28
    if ch in "ilI.,:;|!'`":
        return 0.28
    if ch in "mwMW@#%&":
        return 0.78
    eaw = unicodedata.east_asian_width(ch)
    if eaw in ("W", "F"):
        return 0.97
    if ch.isupper():
        return 0.64
    if ch.isdigit():
        return 0.56
    return 0.50


def detect_font_style(font_name: str, font_dict: Any = None) -> tuple[bool, bool]:
    """글꼴 이름과 FontDescriptor 플래그에서 굵게(bold)·기울임(italic) 여부를 판정한다."""
    lower = font_name.lower()
    is_bold = any(
        tok in lower
        for tok in ("bold", "black", "heavy", "semibold", "demi", "medi", "headline", "h2hdr")
    )
    is_italic = any(tok in lower for tok in ("italic", "oblique", "slant"))

    if font_dict is not None and hasattr(font_dict, "get"):
        try:
            desc = font_dict.get("/FontDescriptor")
            if desc is None:
                descs = font_dict.get("/DescendantFonts")
                if descs is not None and hasattr(descs, "get_object"):
                    descs = descs.get_object()
                if descs:
                    first_desc = descs[0]
                    if hasattr(first_desc, "get_object"):
                        first_desc = first_desc.get_object()
                    if first_desc is not None and hasattr(first_desc, "get"):
                        desc = first_desc.get("/FontDescriptor")
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
