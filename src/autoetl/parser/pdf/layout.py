"""PDF 레이아웃 분석 — 머리글/바닥글 제거, 다단(XY-Cut) 감지, 읽기 순서, 제목/목록/캡션 분류.

표(`tables.py`)와 시각 영역(`visuals.py`)이 점유한 스팬을 제외한 텍스트 스팬을 대상으로:
1. 문서 전체 본문 글꼴 크기 추정
2. 페이지 간 반복 머리글·바닥글 및 쪽번호 분리
3. 스팬 → 시각적 텍스트 줄(가로 큰 간격은 다단 분리를 위해 유지)
4. 전폭 배너 + 다단 본문이 섞인 혼합 레이아웃까지 지원하는 밴드-컬럼 XY-Cut 다단 탐지
5. 읽기 순서 재구성 및 제목(H1~H4)·목록·캡션·코드/수식·본문 문단 분류
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from itertools import pairwise

from autoetl.parser.contracts import (
    BBox,
    ElementKind,
    ExtractedTable,
    OutlineEntry,
    ParseStrategy,
    VisualRegion,
)
from autoetl.parser.pdf.geometry import RawPageData, RawTextSpan, make_bbox

_PAGE_NUM_ONLY_RE = re.compile(
    r"^\s*(?:[-\u2013—]\s*\d{1,4}\s*[-\u2013—]|"
    r"page\s*\d{1,4}(?:\s*(?:/|of)\s*\d{1,4})?|"
    r"\d{1,4}\s*/\s*\d{1,4}|\d{1,4}\s*쪽|\d{1,4})\s*$",
    re.IGNORECASE,
)
_DIGIT_NORM_RE = re.compile(r"\d+")
_HEADING_PREFIX_RE = re.compile(
    r"^(?:제\s*\d+\s*[장절관편부조]|(?:chapter|section|part)\s+\d+|"
    r"\d+(?:\.\d+){0,3}\.?\s+\S|[IVXLCDM]+\.\s+\S|[가-힣]\.\s+\S)",
    re.IGNORECASE,
)
_LIST_ITEM_RE = re.compile(
    r"^\s*(?:[•▪▫■□●○◦◆◇▶▷※·\-\u2013—\*]\s+|"
    r"\(?[0-9a-zA-Z가-하]\)\s+|[0-9]{1,2}\)\s+|[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮]\s*)"
)
_CAPTION_TABLE_RE = re.compile(
    r"^\s*(?:<|\[)?\s*(?:표|table|tbl\.?)\s*\d+(?:[-.]\d+)*\s*(?:>|\]|:|\.)?",
    re.IGNORECASE,
)
_CAPTION_FIGURE_RE = re.compile(
    r"^\s*(?:<|\[)?\s*(?:그림|도표|차트|figure|fig\.?)\s*\d+(?:[-.]\d+)*\s*(?:>|\]|:|\.)?",
    re.IGNORECASE,
)
_MONOSPACE_HINTS = ("courier", "consolas", "mono", "menlo", "inconsolata", "fira code", "d2coding")


@dataclass(frozen=True)
class TextLine:
    """같은 기준선에 놓인 인접 스팬을 합친 텍스트 줄 조각."""

    text: str
    x0: float
    top: float
    x1: float
    bottom: float
    font_size: float
    is_bold: bool
    is_monospace: bool
    span_indices: tuple[int, ...]

    @property
    def width(self) -> float:
        return max(0.0, self.x1 - self.x0)

    @property
    def height(self) -> float:
        return max(0.0, self.bottom - self.top)

    @property
    def y_center(self) -> float:
        return (self.top + self.bottom) / 2.0


@dataclass(frozen=True)
class LayoutTextBlock:
    """분류와 컬럼·읽기 순서가 부여된 텍스트 블록."""

    kind: ElementKind
    strategy: ParseStrategy
    column_index: int
    bbox: BBox
    text: str
    heading_level: int | None
    font_size: float
    is_bold: bool
    confidence: float


_SUPERSCRIPT_REF_RE = re.compile(r"^\s*(?:\d{1,2}\)|[\u2217*†‡§])\s*$")


@dataclass(frozen=True)
class PageLayoutResult:
    """한 페이지의 레이아웃 분석 결과."""

    text_blocks: list[LayoutTextBlock]
    header_footer_blocks: list[LayoutTextBlock]
    column_count: int
    column_gutters_x: list[float]
    line_count: int
    median_font_size: float | None
    max_font_size: float | None
    multi_column_char_ratio: float = 0.0


def estimate_document_body_font_size(pages: list[RawPageData]) -> float | None:
    """문서 전체에서 문자 수 가중 최빈값으로 본문 기준 글꼴 크기를 추정한다."""
    weights: Counter[float] = Counter()
    for page in pages:
        for sp in page.spans:
            n_chars = len(sp.text.strip())
            if n_chars == 0 or sp.font_size <= 3.0:
                continue
            bucket = round(sp.font_size * 2.0) / 2.0
            weights[bucket] += n_chars
    if not weights:
        return None
    return weights.most_common(1)[0][0]


def _normalize_hf_text(text: str) -> str:
    return re.sub(r"\s+", " ", _DIGIT_NORM_RE.sub("#", text.strip())).lower()


def _build_lines_from_spans(
    page: RawPageData,
    span_indices: list[int],
) -> list[TextLine]:
    """스팬들을 y 기준선으로 묶되, 큰 가로 간격(다단/열 경계)은 별도 줄 조각으로 유지한다."""
    if not span_indices:
        return []

    ordered = sorted(
        [(i, page.spans[i]) for i in span_indices if page.spans[i].text.strip()],
        key=lambda pair: (pair[1].y_center, pair[1].x0),
    )
    if not ordered:
        return []

    y_bands: list[list[tuple[int, RawTextSpan]]] = []
    for idx, sp in ordered:
        if not y_bands:
            y_bands.append([(idx, sp)])
            continue
        cur = y_bands[-1]
        ref_y = sum(s.y_center for _, s in cur) / len(cur)
        ref_h = min(s.font_size for _, s in cur)
        if abs(sp.y_center - ref_y) <= max(3.5, ref_h * 0.5):
            cur.append((idx, sp))
        else:
            y_bands.append([(idx, sp)])

    lines: list[TextLine] = []
    for band in y_bands:
        band.sort(key=lambda pair: pair[1].x0)
        # 같은 줄이라도 가로 간격이 크면 다단/별도 블록으로 분리하되,
        # 단독 불릿 문자('•', 'o', '▪' 등) 뒤의 들여쓰기는 같은 줄로 유지한다.
        segments: list[list[tuple[int, RawTextSpan]]] = [[band[0]]]
        for idx, sp in band[1:]:
            prev_seg = segments[-1]
            prev_sp = prev_seg[-1][1]
            gap = sp.x0 - prev_sp.x1
            prev_txt = prev_sp.text.strip()
            is_bullet_lead = len(prev_seg) == 1 and prev_txt in (
                "•",
                "o",
                "O",
                "▪",
                "▫",
                "■",
                "□",
                "●",
                "○",
                "◦",
                "·",
                "-",
                "※",
            )
            split_threshold = (
                max(28.0, max(prev_sp.font_size, sp.font_size) * 2.5)
                if is_bullet_lead
                else max(16.0, max(prev_sp.font_size, sp.font_size) * 1.45)
            )
            if gap > split_threshold:
                segments.append([(idx, sp)])
            else:
                prev_seg.append((idx, sp))

        for seg in segments:
            joined = _join_line_spans(seg)
            if not joined:
                continue

            total_chars = sum(max(1, len(s.text.strip())) for _, s in seg)
            avg_size = sum(s.font_size * max(1, len(s.text.strip())) for _, s in seg) / total_chars
            bold_chars = sum(len(s.text.strip()) for _, s in seg if s.is_bold)
            mono_chars = sum(
                len(s.text.strip())
                for _, s in seg
                if any(hint in s.font_name.lower() for hint in _MONOSPACE_HINTS)
            )
            lines.append(
                TextLine(
                    text=joined,
                    x0=min(s.x0 for _, s in seg),
                    top=min(s.top for _, s in seg),
                    x1=max(s.x1 for _, s in seg),
                    bottom=max(s.bottom for _, s in seg),
                    font_size=round(avg_size, 2),
                    is_bold=(bold_chars / max(1, total_chars)) >= 0.6,
                    is_monospace=(mono_chars / max(1, total_chars)) >= 0.6,
                    span_indices=tuple(i for i, _ in seg),
                )
            )
    return lines


def detect_running_headers_footers(
    pages: list[RawPageData],
) -> tuple[dict[int, frozenset[int]], bool, bool]:
    """문서 전체를 훑어 반복 머리글·바닥글과 쪽번호 스팬 인덱스를 찾아낸다."""
    header_counts: Counter[str] = Counter()
    footer_counts: Counter[str] = Counter()
    page_top_lines: dict[int, list[TextLine]] = {}
    page_bot_lines: dict[int, list[TextLine]] = {}

    for page in pages:
        all_lines = _build_lines_from_spans(page, list(range(len(page.spans))))
        top_lines = [ln for ln in all_lines if ln.top <= page.height * 0.11]
        bot_lines = [ln for ln in all_lines if ln.bottom >= page.height * 0.89]
        page_top_lines[page.page] = top_lines
        page_bot_lines[page.page] = bot_lines

        for sig in {_normalize_hf_text(ln.text) for ln in top_lines if ln.text.strip()}:
            header_counts[sig] += 1
        for sig in {_normalize_hf_text(ln.text) for ln in bot_lines if ln.text.strip()}:
            footer_counts[sig] += 1

    repeat_threshold = max(2, int(len(pages) * 0.6)) if len(pages) >= 3 else 9999
    repeated_headers = {sig for sig, cnt in header_counts.items() if cnt >= repeat_threshold}
    repeated_footers = {sig for sig, cnt in footer_counts.items() if cnt >= repeat_threshold}

    result: dict[int, frozenset[int]] = {}
    has_header = False
    has_footer = False

    for page in pages:
        marked: set[int] = set()
        for ln in page_top_lines[page.page]:
            sig = _normalize_hf_text(ln.text)
            if sig in repeated_headers or (
                ln.top <= page.height * 0.08 and _PAGE_NUM_ONLY_RE.match(ln.text)
            ):
                marked.update(ln.span_indices)
                has_header = True
        for ln in page_bot_lines[page.page]:
            sig = _normalize_hf_text(ln.text)
            if sig in repeated_footers or (
                ln.bottom >= page.height * 0.92 and _PAGE_NUM_ONLY_RE.match(ln.text)
            ):
                marked.update(ln.span_indices)
                has_footer = True
        result[page.page] = frozenset(marked)

    return result, has_header, has_footer


def _find_vertical_gutters_in_band(
    lines: list[TextLine],
    page_w: float,
    body_font_size: float,
) -> list[float]:
    """한 수평 밴드 안의 텍스트 줄 x 투영에서 다단 여백(gutter)의 중심 x 좌표를 찾는다."""
    if len(lines) < 4:
        return []

    x_min = min(ln.x0 for ln in lines)
    x_max = max(ln.x1 for ln in lines)
    span_w = x_max - x_min
    if span_w < page_w * 0.45:
        return []

    n_bins = int(span_w) + 1
    occ = [0] * n_bins
    for ln in lines:
        b0 = max(0, int(ln.x0 - x_min))
        b1 = min(n_bins - 1, int(ln.x1 - x_min))
        for b in range(b0, b1 + 1):
            occ[b] += 1

    min_gutter_w = max(10, round(body_font_size * 1.0))
    # 가장자리 15% 안쪽의 내부 여백만 탐색
    margin_bins = max(10, int(n_bins * 0.15))

    gutters: list[float] = []
    i = margin_bins
    end_limit = n_bins - margin_bins
    while i < end_limit:
        if occ[i] == 0:
            start = i
            while i < end_limit and occ[i] == 0:
                i += 1
            gap_w = i - start
            if gap_w >= min_gutter_w:
                gutter_x = x_min + (start + i) / 2.0
                left_lines = [ln for ln in lines if ln.x1 <= gutter_x + 2.0]
                right_lines = [ln for ln in lines if ln.x0 >= gutter_x - 2.0]
                crossing = [ln for ln in lines if ln.x0 < gutter_x - 2.0 and ln.x1 > gutter_x + 2.0]
                if len(left_lines) >= 2 and len(right_lines) >= 2 and len(crossing) == 0:
                    # 같은 줄의 가로 조각을 별도 행으로 세지 않도록 고유 행 수·텍스트 밀도 검증
                    row_step = max(6.0, body_font_size * 0.7)
                    left_rows = {round(ln.y_center / row_step) for ln in left_lines}
                    right_rows = {round(ln.y_center / row_step) for ln in right_lines}
                    left_chars = sum(len(ln.text.strip()) for ln in left_lines)
                    right_chars = sum(len(ln.text.strip()) for ln in right_lines)
                    if (
                        len(left_rows) < 2
                        or len(right_rows) < 2
                        or left_chars < 20
                        or right_chars < 20
                    ):
                        continue
                    # 좌우 컬럼이 실제로 수직으로 겹치는지 확인(들여쓴 서명·주석 오탐 방지)
                    l_top = min(ln.top for ln in left_lines)
                    l_bot = max(ln.bottom for ln in left_lines)
                    r_top = min(ln.top for ln in right_lines)
                    r_bot = max(ln.bottom for ln in right_lines)
                    ov = min(l_bot, r_bot) - max(l_top, r_top)
                    min_h = max(1.0, min(l_bot - l_top, r_bot - r_top))
                    if ov >= body_font_size * 1.5 and ov / min_h >= 0.25:
                        gutters.append(round(gutter_x, 2))
        else:
            i += 1

    return gutters[:3]  # 최대 4단까지 지원


def _split_into_reading_groups(
    lines: list[TextLine],
    page_w: float,
    body_font_size: float,
) -> tuple[list[tuple[int, list[TextLine]]], int, list[float], float]:
    """혼합 레이아웃(전폭 배너 + 다단 본문)을 지원하며 읽기 순서 그룹을 구성한다.

    단일 컬럼 연속 구간은 합쳐 문단의 마지막 짧은 줄이 분리되지 않게 보존한다.
    반환: ([(column_index, lines_in_column_top_to_bottom), ...], max_column_count, all_gutters)
    """
    if not lines:
        return [], 1, [], 0.0

    ordered = sorted(lines, key=lambda ln: (ln.top, ln.x0))
    content_x0 = min(ln.x0 for ln in ordered)
    content_x1 = max(ln.x1 for ln in ordered)
    content_w = max(1.0, content_x1 - content_x0)
    mid_x = (content_x0 + content_x1) / 2.0

    # 1단계: 페이지 중앙을 가로지르는 전폭 줄(제목 배너·초록·전폭 문장)을 경계로 수평 밴드 분할
    def is_full_width(ln: TextLine) -> bool:
        return (
            ln.width >= max(content_w * 0.58, page_w * 0.50)
            and ln.x0 < mid_x - 20.0
            and ln.x1 > mid_x + 20.0
        )

    bands: list[tuple[bool, list[TextLine]]] = []  # (is_banner, band_lines)
    for ln in ordered:
        banner = is_full_width(ln)
        if not bands or bands[-1][0] != banner:
            bands.append((banner, [ln]))
        else:
            bands[-1][1].append(ln)

    # 각 밴드가 실제로 다단인지 먼저 판정하고, 연속된 단일 컬럼 밴드는 하나로 병합
    raw_groups: list[tuple[bool, int, list[TextLine]]] = []  # (is_multi_col, col_idx, lines)
    max_cols = 1
    detected_gutters: list[float] = []

    for is_banner, band_lines in bands:
        if is_banner or len(band_lines) < 4:
            raw_groups.append((False, 0, band_lines))
            continue

        gutters = _find_vertical_gutters_in_band(band_lines, page_w, body_font_size)
        if not gutters:
            raw_groups.append((False, 0, band_lines))
            continue

        n_cols = len(gutters) + 1
        max_cols = max(max_cols, n_cols)
        for g in gutters:
            if not any(abs(g - prev) <= 10.0 for prev in detected_gutters):
                detected_gutters.append(g)

        col_buckets: list[list[TextLine]] = [[] for _ in range(n_cols)]
        for ln in band_lines:
            cx = (ln.x0 + ln.x1) / 2.0
            col_idx = 0
            for g in gutters:
                if cx > g:
                    col_idx += 1
            col_buckets[col_idx].append(ln)

        for ci, bucket in enumerate(col_buckets):
            if bucket:
                bucket.sort(key=lambda ln: (ln.top, ln.x0))
                raw_groups.append((True, ci, bucket))

    # 연속된 단일 컬럼 구간을 병합해 문단의 마지막 짧은 줄이 깨지지 않게 유지
    reading_groups: list[tuple[int, list[TextLine]]] = []
    pending_single: list[TextLine] = []
    mc_chars = 0
    total_chars = 0
    for is_mc, ci, grp_lines in raw_groups:
        grp_chars = sum(len(ln.text.strip()) for ln in grp_lines)
        total_chars += grp_chars
        if not is_mc:
            pending_single.extend(grp_lines)
        else:
            mc_chars += grp_chars
            if pending_single:
                pending_single.sort(key=lambda ln: (ln.top, ln.x0))
                reading_groups.append((0, pending_single))
                pending_single = []
            reading_groups.append((ci, grp_lines))
    if pending_single:
        pending_single.sort(key=lambda ln: (ln.top, ln.x0))
        reading_groups.append((0, pending_single))

    detected_gutters.sort()
    mc_ratio = round(mc_chars / total_chars, 4) if total_chars > 0 else 0.0
    return reading_groups, max_cols, detected_gutters, mc_ratio


def _classify_line_kind(
    line: TextLine,
    *,
    body_font_size: float,
    outline_titles: set[str],
) -> tuple[ElementKind, int | None, float]:
    """단일 줄의 요소 유형(제목·목록·캡션·코드·본문)과 제목 레벨을 판정한다."""
    text = line.text.strip()
    ratio = line.font_size / max(1.0, body_font_size)

    if _CAPTION_TABLE_RE.match(text) or _CAPTION_FIGURE_RE.match(text):
        return ElementKind.CAPTION, None, 0.92

    if _LIST_ITEM_RE.match(text) or text.startswith(("o ", "O ")):
        return ElementKind.LIST_ITEM, None, 0.90

    if line.is_monospace and len(text) >= 4:
        return ElementKind.FORMULA_OR_CODE, None, 0.85

    # 북마크(Outline) 제목과 일치하면 높은 신뢰도의 제목
    norm_text = re.sub(r"\s+", " ", text).lower()
    if norm_text in outline_titles and len(text) <= 120:
        level = 1 if ratio >= 1.45 else 2 if ratio >= 1.15 else 3
        return ElementKind.HEADING, level, 0.96

    # 글꼴 크기 기반 제목 판정
    if len(text) <= 140 and not text.endswith((".", "다.", "요.", "까?")):
        if ratio >= 1.55:
            return ElementKind.HEADING, 1, 0.93
        if ratio >= 1.28:
            return ElementKind.HEADING, 2, 0.90
        if ratio >= 1.14 and (line.is_bold or _HEADING_PREFIX_RE.match(text)):
            return ElementKind.HEADING, 3, 0.87
        if line.is_bold and _HEADING_PREFIX_RE.match(text) and len(text) <= 80:
            return ElementKind.HEADING, 3, 0.84

    return ElementKind.PARAGRAPH, None, 0.90


def _join_line_spans(seg: list[tuple[int, RawTextSpan]]) -> str:
    """가로 순으로 정렬된 스팬 목록을 자연스러운 공백 규칙으로 한 줄 문자열로 합친다."""
    if not seg:
        return ""
    parts: list[str] = [seg[0][1].text.strip()]
    for (_, p_sp), (_, c_sp) in pairwise(seg):
        gap = c_sp.x0 - p_sp.x1
        txt = c_sp.text.strip()
        if not txt:
            continue
        is_c_sup = c_sp.font_size < p_sp.font_size * 0.88 and bool(_SUPERSCRIPT_REF_RE.match(txt))
        is_p_sup = p_sp.font_size < c_sp.font_size * 0.88 and bool(
            _SUPERSCRIPT_REF_RE.match(p_sp.text.strip())
        )
        if is_c_sup and gap <= 3.5:
            parts.append(txt)
            continue
        if is_p_sup and gap <= 2.5:
            parts.append(txt)
            continue
        has_explicit_space = p_sp.text.endswith(" ") or c_sp.text.startswith(" ")
        if has_explicit_space or gap > max(1.4, c_sp.font_size * 0.18):
            parts.append(" " + txt)
        else:
            parts.append(txt)
    return "".join(parts).strip()


def _absorb_inline_superscripts(
    lines: list[TextLine],
    body_font_size: float,
    page_spans: list[RawTextSpan],
) -> list[TextLine]:
    """문장 중간에 끼어든 위첨자 각주 번호('1)', '2)' 등)를 원래 x 위치에 인라인 병합한다."""
    if len(lines) < 2:
        return lines

    def is_sup(ln: TextLine) -> bool:
        return (
            ln.font_size <= body_font_size * 0.85
            and len(ln.text.strip()) <= 4
            and bool(_SUPERSCRIPT_REF_RE.match(ln.text))
        )

    def merge_into(target: TextLine, sup: TextLine) -> TextLine:
        combined_indices = sorted(
            (*target.span_indices, *sup.span_indices),
            key=lambda idx: (page_spans[idx].x0, page_spans[idx].top),
        )
        merged_txt = _join_line_spans([(idx, page_spans[idx]) for idx in combined_indices])
        return TextLine(
            text=merged_txt,
            x0=min(target.x0, sup.x0),
            top=min(target.top, sup.top),
            x1=max(target.x1, sup.x1),
            bottom=max(target.bottom, sup.bottom),
            font_size=target.font_size,
            is_bold=target.is_bold,
            is_monospace=target.is_monospace,
            span_indices=tuple(combined_indices),
        )

    out: list[TextLine] = []
    i = 0
    while i < len(lines):
        ln = lines[i]
        if is_sup(ln):
            # 바로 다음 줄과 수직으로 겹치면(위첨자가 본문보다 살짝 위) 다음 줄의 원래 x 위치에 병합
            if i + 1 < len(lines) and not is_sup(lines[i + 1]):
                nxt = lines[i + 1]
                if abs(ln.top - nxt.top) <= max(5.0, body_font_size * 0.55):
                    lines[i + 1] = merge_into(nxt, ln)
                    i += 1
                    continue
            # 바로 앞 줄과 수직으로 겹치면 앞 줄의 원래 x 위치에 병합
            if out and abs(ln.top - out[-1].top) <= max(5.0, body_font_size * 0.55):
                out[-1] = merge_into(out[-1], ln)
                i += 1
                continue
        out.append(ln)
        i += 1
    return out


def _merge_column_lines_into_blocks(
    page: RawPageData,
    col_idx: int,
    lines: list[TextLine],
    *,
    body_font_size: float,
    outline_titles: set[str],
    strategy: ParseStrategy,
) -> list[LayoutTextBlock]:
    """같은 컬럼 안의 연속된 텍스트 줄들을 의미 블록(제목·문단·목록·캡션)으로 병합한다."""
    if not lines:
        return []

    lines = _absorb_inline_superscripts(list(lines), body_font_size, page.spans)

    blocks: list[LayoutTextBlock] = []
    cur_lines: list[TextLine] = []
    cur_kind: ElementKind | None = None
    cur_level: int | None = None
    cur_conf = 0.9

    def flush() -> None:
        nonlocal cur_lines, cur_kind, cur_level
        if not cur_lines or cur_kind is None:
            return
        joined_text = _join_paragraph_lines([ln.text for ln in cur_lines], cur_kind)
        bx0 = min(ln.x0 for ln in cur_lines)
        btop = min(ln.top for ln in cur_lines)
        bx1 = max(ln.x1 for ln in cur_lines)
        bbot = max(ln.bottom for ln in cur_lines)
        avg_fs = round(sum(ln.font_size for ln in cur_lines) / len(cur_lines), 2)
        bold_ratio = sum(1 for ln in cur_lines if ln.is_bold) / len(cur_lines)
        blocks.append(
            LayoutTextBlock(
                kind=cur_kind,
                strategy=strategy,
                column_index=col_idx,
                bbox=make_bbox(bx0, btop, bx1, bbot, page.width, page.height),
                text=joined_text,
                heading_level=cur_level,
                font_size=avg_fs,
                is_bold=bold_ratio >= 0.6,
                confidence=cur_conf,
            )
        )
        cur_lines = []
        cur_kind = None
        cur_level = None

    for ln in lines:
        kind, level, conf = _classify_line_kind(
            ln, body_font_size=body_font_size, outline_titles=outline_titles
        )
        if not cur_lines:
            cur_lines = [ln]
            cur_kind = kind
            cur_level = level
            cur_conf = conf
            continue

        prev = cur_lines[-1]
        v_gap = ln.top - prev.bottom
        max_line_gap = max(prev.font_size, ln.font_size) * 0.95

        can_merge = False
        if cur_kind == ElementKind.PARAGRAPH and kind == ElementKind.PARAGRAPH:
            same_size = abs(ln.font_size - prev.font_size) <= 1.2
            same_indent = abs(ln.x0 - prev.x0) <= max(24.0, body_font_size * 2.2)
            if v_gap <= max_line_gap and same_size and same_indent:
                can_merge = True
        elif cur_kind == ElementKind.LIST_ITEM and kind == ElementKind.PARAGRAPH:
            # 목록 항목의 이어지는 들여쓰기 줄 병합
            if (
                v_gap <= max_line_gap
                and ln.x0 >= prev.x0 - 4.0
                and abs(ln.font_size - prev.font_size) <= 1.2
            ):
                can_merge = True
        elif cur_kind == ElementKind.CAPTION and kind == ElementKind.PARAGRAPH:
            # 2줄로 줄바꿈된 '<그림 1> ... / 고용률 추세' 캡션의 이어지는 짧은 줄 병합
            if (
                len(cur_lines) < 3
                and v_gap <= max_line_gap * 0.95
                and abs(ln.font_size - prev.font_size) <= 1.2
                and len(ln.text.strip()) <= 65
                and not ln.text.strip().endswith(("다.", "요."))
            ):
                can_merge = True
        elif cur_kind == ElementKind.HEADING and kind == ElementKind.HEADING and level == cur_level:
            if v_gap <= max_line_gap * 0.85 and abs(ln.font_size - prev.font_size) <= 1.0:
                can_merge = True
        elif cur_kind == ElementKind.FORMULA_OR_CODE and kind == ElementKind.FORMULA_OR_CODE:
            if v_gap <= max_line_gap * 1.4:
                can_merge = True

        if can_merge:
            cur_lines.append(ln)
        else:
            flush()
            cur_lines = [ln]
            cur_kind = kind
            cur_level = level
            cur_conf = conf

    flush()
    return blocks


def _join_paragraph_lines(lines: list[str], kind: ElementKind) -> str:
    if kind == ElementKind.FORMULA_OR_CODE:
        return "\n".join(lines)
    out = ""
    for line in lines:
        s = line.strip()
        if not s:
            continue
        if not out:
            out = s
        elif out.endswith("-") and len(out) >= 2 and out[-2].isalpha() and s[0].isalpha():
            # 영문 줄바꿈 하이픈 복원
            out = out[:-1] + s
        else:
            out = f"{out} {s}"
    return out


def analyze_page_layout(
    page: RawPageData,
    *,
    excluded_span_indices: frozenset[int],
    header_footer_span_indices: frozenset[int],
    body_font_size: float | None,
    outline: list[OutlineEntry],
) -> PageLayoutResult:
    """한 페이지의 텍스트 레이아웃(다단·읽기 순서·제목·목록·머리글/바닥글)을 분석한다."""
    all_sizes = [sp.font_size for sp in page.spans if sp.text.strip() and sp.font_size > 0]
    median_fs: float | None = None
    max_fs: float | None = None
    if all_sizes:
        ordered_sizes = sorted(all_sizes)
        median_fs = round(ordered_sizes[len(ordered_sizes) // 2], 2)
        max_fs = round(ordered_sizes[-1], 2)

    ref_body_size = body_font_size or median_fs or 10.5
    outline_titles = {
        re.sub(r"\s+", " ", entry.title.strip()).lower()
        for entry in outline
        if entry.page is None or entry.page == page.page
    }

    # 머리글/바닥글 블록 별도 생성
    hf_lines = _build_lines_from_spans(page, sorted(header_footer_span_indices))
    hf_blocks = [
        LayoutTextBlock(
            kind=ElementKind.HEADER_FOOTER,
            strategy=ParseStrategy.SKIP_DECORATIVE,
            column_index=0,
            bbox=make_bbox(ln.x0, ln.top, ln.x1, ln.bottom, page.width, page.height),
            text=ln.text,
            heading_level=None,
            font_size=ln.font_size,
            is_bold=ln.is_bold,
            confidence=0.9,
        )
        for ln in hf_lines
    ]

    skip_all = excluded_span_indices | header_footer_span_indices
    body_indices = [i for i in range(len(page.spans)) if i not in skip_all]
    body_lines = _build_lines_from_spans(page, body_indices)

    reading_groups, col_count, gutters, mc_ratio = _split_into_reading_groups(
        body_lines, page.width, ref_body_size
    )
    text_strategy = (
        ParseStrategy.MULTI_COLUMN_LAYOUT
        if (col_count >= 2 and mc_ratio >= 0.30)
        else ParseStrategy.NATIVE_TEXT
    )

    text_blocks: list[LayoutTextBlock] = []
    for col_idx, col_lines in reading_groups:
        text_blocks.extend(
            _merge_column_lines_into_blocks(
                page,
                col_idx,
                col_lines,
                body_font_size=ref_body_size,
                outline_titles=outline_titles,
                strategy=text_strategy,
            )
        )

    return PageLayoutResult(
        text_blocks=text_blocks,
        header_footer_blocks=hf_blocks,
        column_count=col_count,
        column_gutters_x=gutters,
        line_count=len(body_lines) + len(hf_lines),
        median_font_size=median_fs,
        max_font_size=max_fs,
        multi_column_char_ratio=mc_ratio,
    )


def link_captions_to_elements(
    text_blocks: list[LayoutTextBlock],
    tables: list[ExtractedTable],
    visuals: list[VisualRegion],
) -> tuple[list[ExtractedTable], list[VisualRegion]]:
    """근접한 표·그림 캡션 블록을 해당 표/시각 영역에 연결한다."""
    caption_blocks = [b for b in text_blocks if b.kind == ElementKind.CAPTION]
    if not caption_blocks:
        return tables, visuals

    def find_caption(target: BBox, *, for_table: bool) -> str | None:
        pattern = _CAPTION_TABLE_RE if for_table else _CAPTION_FIGURE_RE
        best_text: str | None = None
        best_dist = 54.0
        for blk in caption_blocks:
            if not pattern.match(blk.text):
                continue
            h_ov = min(blk.bbox.x1, target.x1) - max(blk.bbox.x0, target.x0)
            if h_ov < -24.0:
                continue
            if blk.bbox.bottom <= target.top + 6.0:
                dist = target.top - blk.bbox.bottom
            elif blk.bbox.top >= target.bottom - 6.0:
                dist = blk.bbox.top - target.bottom
            else:
                continue
            if 0.0 <= dist < best_dist:
                best_dist = dist
                best_text = blk.text
        return best_text

    updated_tables = [
        t.model_copy(update={"caption": cap})
        if (cap := find_caption(t.bbox, for_table=True)) is not None
        else t
        for t in tables
    ]
    updated_visuals = [
        v.model_copy(update={"caption": cap})
        if (cap := find_caption(v.bbox, for_table=False)) is not None
        else v
        for v in visuals
    ]
    return updated_tables, updated_visuals
