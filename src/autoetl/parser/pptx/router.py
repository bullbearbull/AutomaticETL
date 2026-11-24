"""PPTX 슬라이드 구조 분류, 다단 읽기 순서(XY-Cut), 도식/차트 라우팅 및 RoutingPlan 조립."""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field

from autoetl.parser.contracts import (
    BBox,
    DocumentBlock,
    DocumentMetadata,
    ElementKind,
    ExtractedTable,
    OutlineEntry,
    PageCategory,
    PageMetrics,
    PageRouteDecision,
    ParseStrategy,
    RegionRoute,
    RoutingPlan,
    RoutingSummary,
    TableCell,
    VisualRegion,
)
from autoetl.parser.pptx.elements import (
    RawPptxChart,
    RawPptxImage,
    RawPptxShape,
    RawSlideData,
    is_flowchart_preset,
)
from autoetl.parser.pptx.ooxml import SectionRef, clamp, make_bbox

_CAPTION_RE = re.compile(
    r"^(?:<\s*)?(?:표|그림|차트|도표|Table|Figure|Fig\.|Chart)\s*[0-9IVX\-]+",
    re.IGNORECASE,
)

_EQUATION_IMAGE_RE = re.compile(
    r"(?:^|[\s_\-])(?:equation|formula|stepeq|eq\d+|수식)(?:$|[\s_\-\d])",
    re.IGNORECASE,
)

_LOCAL_PATH_RE = re.compile(
    r"^(?:[A-Za-z]:[\\/]|/(?:Users|home|var|tmp|mnt|opt)/|file://)",
    re.IGNORECASE,
)

_BARE_FILENAME_RE = re.compile(
    r"^[\w.\- ]+\.(?:png|jpe?g|gif|bmp|webp|svg|emf|wmf|tiff?)$",
    re.IGNORECASE,
)

_SUPPORTED_RASTER_EXTS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"})


def _normalize_heading_text(text: str) -> str:
    """제목 안의 소프트 줄바꿈(`<a:br/>`)을 공백으로 합쳐 Markdown 제목 깨짐을 방지한다."""
    return re.sub(r"\s*\r?\n\s*", " ", text).strip()


def _sanitize_alt_text(alt_text: str) -> str:
    """로컬 파일 경로(`E:\\...`)나 확장자만 있는 단순 파일명을 대체 텍스트 힌트에서 제외한다."""
    cleaned = alt_text.strip()
    if not cleaned:
        return ""
    if _LOCAL_PATH_RE.match(cleaned) or _BARE_FILENAME_RE.match(cleaned):
        return ""
    return cleaned


def _is_equation_image(name: str, alt_text: str) -> bool:
    """도형명이나 대체 텍스트가 명시적으로 수식 이미지를 가리키는지 판별한다."""
    return bool(_EQUATION_IMAGE_RE.search(name) or _EQUATION_IMAGE_RE.search(alt_text))


def _normalize_hf_pattern(text: str) -> str:
    """머리글/바닥글 반복 판정용 텍스트 정규화(숫자를 `#`으로 마스킹)."""
    collapsed = re.sub(r"\s+", " ", text.strip().lower())
    return re.sub(r"\d+", "#", collapsed)


@dataclass
class AnalyzedSlide:
    """구조 분석과 전략 라우팅이 완료된 단일 슬라이드 결과."""

    raw: RawSlideData
    tables: list[ExtractedTable]
    visuals: list[VisualRegion]
    visual_images: dict[str, RawPptxImage]
    blocks: list[DocumentBlock]
    header_footer_blocks: list[DocumentBlock]
    route: PageRouteDecision
    slide_title: str | None = None
    is_full_slide_scan: bool = False
    has_running_header: bool = False
    has_running_footer: bool = False
    layout_page_image_bytes: bytes | None = None
    layout_page_image_ext: str | None = None
    layout_page_image_size: tuple[int, int] | None = None
    layout_crop_rects: dict[str, tuple[int, int, int, int]] = field(default_factory=dict)
    layout_segmented: bool = False


def _rect_area(r: tuple[float, float, float, float]) -> float:
    return max(0.0, r[2] - r[0]) * max(0.0, r[3] - r[1])


def _clip_rect(
    r: tuple[float, float, float, float], w: float, h: float
) -> tuple[float, float, float, float]:
    return (
        clamp(min(r[0], r[2]), 0.0, w),
        clamp(min(r[1], r[3]), 0.0, h),
        clamp(max(r[0], r[2]), 0.0, w),
        clamp(max(r[1], r[3]), 0.0, h),
    )


def _union_area(rects: list[tuple[float, float, float, float]]) -> float:
    """축 정렬 사각형 목록의 정확한 합집합 면적(수직 스윕 라인)을 계산한다."""
    valid = [r for r in rects if r[2] > r[0] and r[3] > r[1]]
    if not valid:
        return 0.0
    xs = sorted({x for r in valid for x in (r[0], r[2])})
    total = 0.0
    for i in range(len(xs) - 1):
        x0, x1 = xs[i], xs[i + 1]
        if x1 <= x0:
            continue
        intervals = sorted((r[1], r[3]) for r in valid if r[0] <= x0 and r[2] >= x1)
        if not intervals:
            continue
        cur_s, cur_e = intervals[0]
        covered_y = 0.0
        for s, e in intervals[1:]:
            if s <= cur_e:
                cur_e = max(cur_e, e)
            else:
                covered_y += cur_e - cur_s
                cur_s, cur_e = s, e
        covered_y += cur_e - cur_s
        total += (x1 - x0) * covered_y
    return total


def _count_usable_chars(text: str) -> tuple[int, int]:
    total = 0
    usable = 0
    for ch in text:
        if ch.isspace():
            continue
        total += 1
        cat = unicodedata.category(ch)
        if ch != "\ufffd" and cat.startswith(("L", "N", "P", "S")):
            usable += 1
    return total, usable


def estimate_deck_body_font_size(slides: list[RawSlideData]) -> float | None:
    """전체 슬라이드에서 본문(제목·머리글/바닥글 제외) 글꼴 크기 중앙값을 추정한다."""
    sizes: list[float] = []
    skip_ph = {"title", "ctrTitle", "subTitle", "sldNum", "ftr", "dt", "hdr"}
    for slide in slides:
        for sp in slide.shapes:
            if sp.ph_type in skip_ph or sp.is_slide_num_field:
                continue
            for p in sp.paragraphs:
                if p.font_size is not None and 6.0 <= p.font_size <= 40.0:
                    weight = max(1, min(20, len(p.text) // 10))
                    sizes.extend([p.font_size] * weight)
    if not sizes:
        return None
    sizes.sort()
    return round(sizes[len(sizes) // 2], 1)


def find_repeating_pptx_logos(slides: list[RawSlideData]) -> set[str]:
    """3장 이상 발표 자료에서 60% 이상 슬라이드에 반복되는 소형 로고 미디어 경로를 찾는다."""
    if len(slides) < 3:
        return set()
    counts: Counter[str] = Counter()
    for slide in slides:
        slide_area = max(1.0, slide.width_pt * slide.height_pt)
        seen_in_slide: set[str] = set()
        for img in slide.images:
            clipped = _clip_rect(img.rect_pt, slide.width_pt, slide.height_pt)
            cov = _rect_area(clipped) / slide_area
            if cov < 0.08:
                seen_in_slide.add(img.media_part)
        for media_part in seen_in_slide:
            counts[media_part] += 1
    threshold = max(3, int(len(slides) * 0.6))
    return {mp for mp, cnt in counts.items() if cnt >= threshold}


def find_repeating_pptx_headers_footers(
    slides: list[RawSlideData],
    *,
    deck_body_font_size: float | None,
) -> tuple[set[str], set[str]]:
    """3장 이상 슬라이드 상·하단 여백에 40% 이상 반복되는 머리글/바닥글 정규화 패턴을 찾는다."""
    if len(slides) < 3:
        return set(), set()

    max_hf_font = max(14.5, (deck_body_font_size or 14.0) * 1.15)
    title_phs = {"title", "ctrTitle", "subTitle"}
    top_counts: Counter[str] = Counter()
    bot_counts: Counter[str] = Counter()

    for slide in slides:
        sw = max(1.0, slide.width_pt)
        sh = max(1.0, slide.height_pt)
        seen_top: set[str] = set()
        seen_bot: set[str] = set()
        for sp in slide.shapes:
            if not sp.paragraphs or sp.ph_type in title_phs:
                continue
            txt = sp.full_text
            if not txt or len(txt) > 90 or len(sp.paragraphs) > 2:
                continue
            fonts = [p.font_size for p in sp.paragraphs if p.font_size is not None]
            if fonts and max(fonts) > max_hf_font:
                continue
            clipped = _clip_rect(sp.rect_pt, sw, sh)
            norm_top = clipped[1] / sh
            norm_bot = clipped[3] / sh
            pattern = _normalize_hf_pattern(txt)
            if not pattern:
                continue
            if norm_bot <= 0.10:
                seen_top.add(pattern)
            elif norm_top >= 0.87:
                seen_bot.add(pattern)
        for pat in seen_top:
            top_counts[pat] += 1
        for pat in seen_bot:
            bot_counts[pat] += 1

    threshold = max(3, int(len(slides) * 0.40))
    rep_top = {pat for pat, cnt in top_counts.items() if cnt >= threshold}
    rep_bot = {pat for pat, cnt in bot_counts.items() if cnt >= threshold}
    return rep_top, rep_bot


def _chart_to_extracted_table(
    chart: RawPptxChart,
    *,
    slide_number: int,
    table_index: int,
    slide_w: float,
    slide_h: float,
) -> ExtractedTable:
    n_cols = len(chart.headers)
    n_rows = 1 + len(chart.rows)
    cells: list[TableCell] = []
    for c_idx, h in enumerate(chart.headers):
        cells.append(TableCell(row=0, col=c_idx, text=h, is_header=True))
    for r_idx, row_vals in enumerate(chart.rows, 1):
        for c_idx, val in enumerate(row_vals[:n_cols]):
            cells.append(TableCell(row=r_idx, col=c_idx, text=val, is_header=False))

    caption = (
        f"[{chart.chart_type_label}] {chart.title}"
        if chart.title
        else f"[{chart.chart_type_label}] {chart.name}"
    )
    rect = _clip_rect(chart.rect_pt, slide_w, slide_h)
    return ExtractedTable(
        table_id=f"p{slide_number:03d}_t{table_index:02d}",
        page=slide_number,
        bbox=make_bbox(rect[0], rect[1], rect[2], rect[3], slide_w, slide_h),
        strategy=ParseStrategy.RULED_TABLE,
        confidence=0.97,
        row_count=n_rows,
        col_count=n_cols,
        header_row_count=1,
        headers=list(chart.headers),
        rows=[list(r) for r in chart.rows],
        cells=cells,
        has_merged_cells=False,
        caption=caption,
        reason=(
            f"PPTX 네이티브 차트 XML({chart.chart_type_label}·{chart.chart_type}) "
            f"시리즈 수치 직접 추출({len(chart.rows)}항목 x {n_cols - 1}계열)"
        ),
    )


def _detect_slide_column_gutters(
    item_rects: list[tuple[float, float, float, float]],
    slide_w: float,
    slide_h: float,
) -> list[float]:
    """슬라이드 본문 영역의 2단·3단 카드/컬럼 공백 여백(gutter x좌표)을 탐지한다."""
    # 제목 영역(상단 15%)과 전폭 요소(폭 >= 58%) 제외
    narrow = [
        r
        for r in item_rects
        if (r[2] - r[0]) <= slide_w * 0.56
        and (r[2] - r[0]) >= slide_w * 0.12
        and r[1] >= slide_h * 0.12
    ]
    if len(narrow) < 2:
        return []

    # 1pt 해상도 히스토그램으로 비점유 세로 띠 탐색
    w_bins = max(10, round(slide_w))
    occ = [0] * w_bins
    for r in narrow:
        bx0 = max(0, min(w_bins - 1, int(r[0])))
        bx1 = max(bx0 + 1, min(w_bins, round(r[2])))
        for bx in range(bx0, bx1):
            occ[bx] += 1

    min_gutter_w = max(10, int(slide_w * 0.015))
    search_lo = int(w_bins * 0.20)
    search_hi = int(w_bins * 0.80)
    gutters: list[float] = []
    cur_start = -1
    for bx in range(search_lo, search_hi):
        if occ[bx] == 0:
            if cur_start < 0:
                cur_start = bx
        else:
            if cur_start >= 0 and (bx - cur_start) >= min_gutter_w:
                gx = (cur_start + bx) / 2.0
                left_cnt = sum(1 for r in narrow if r[2] <= gx + 4.0)
                right_cnt = sum(1 for r in narrow if r[0] >= gx - 4.0)
                if left_cnt >= 1 and right_cnt >= 1:
                    gutters.append(round(gx, 1))
            cur_start = -1
    if cur_start >= 0 and (search_hi - cur_start) >= min_gutter_w:
        gx = (cur_start + search_hi) / 2.0
        left_cnt = sum(1 for r in narrow if r[2] <= gx + 4.0)
        right_cnt = sum(1 for r in narrow if r[0] >= gx - 4.0)
        if left_cnt >= 1 and right_cnt >= 1:
            gutters.append(round(gx, 1))

    # 최대 2개 여백(최대 3단)만 유지
    return gutters[:2]


@dataclass
class _LayoutUnit:
    """도형·SmartArt·표·시각 영역 1개를 원자 단위로 묶은 레이아웃 정렬 노드."""

    bbox: BBox
    column_index: int
    is_title_anchor: bool
    blocks: list[DocumentBlock]


def _assign_column(bbox: BBox, gutters_x: list[float]) -> tuple[int, bool]:
    if not gutters_x:
        return 0, False
    for g in gutters_x:
        if bbox.x0 < g - 4.0 and bbox.x1 > g + 4.0:
            return 0, True
    cx = (bbox.x0 + bbox.x1) / 2.0
    col = 0
    for g in gutters_x:
        if cx > g:
            col += 1
    return col, False


def _find_best_cut(
    units: list[_LayoutUnit],
    *,
    axis: str,
    min_gap: float,
) -> tuple[float, list[_LayoutUnit], list[_LayoutUnit]] | None:
    """축(`x` 또는 `y`)을 따라 겹치지 않는 가장 넓은 공백(gap) 분할 지점을 찾는다."""
    if len(units) < 2:
        return None
    if axis == "y":
        ordered = sorted(units, key=lambda u: (u.bbox.top, u.bbox.x0))
        cur_end = ordered[0].bbox.bottom
        best_gap = -1.0
        best_idx = -1
        for i in range(1, len(ordered)):
            gap = ordered[i].bbox.top - cur_end
            if gap >= min_gap and gap > best_gap:
                best_gap = gap
                best_idx = i
            cur_end = max(cur_end, ordered[i].bbox.bottom)
    else:
        ordered = sorted(units, key=lambda u: (u.bbox.x0, u.bbox.top))
        cur_end = ordered[0].bbox.x1
        best_gap = -1.0
        best_idx = -1
        for i in range(1, len(ordered)):
            gap = ordered[i].bbox.x0 - cur_end
            if gap >= min_gap and gap > best_gap:
                best_gap = gap
                best_idx = i
            cur_end = max(cur_end, ordered[i].bbox.x1)

    if best_idx < 0:
        return None
    return best_gap, ordered[:best_idx], ordered[best_idx:]


def _order_units_xycut(units: list[_LayoutUnit], depth: int = 0) -> list[_LayoutUnit]:
    """최대 공백 우선(Largest-Gap-First) 재귀적 XY-Cut으로 원자 레이아웃 단위를 정렬한다.

    단일 도형/SmartArt 안의 문단들은 원자 단위로 묶여 있으므로 옆 열의 도형이나
    이미지가 본문 리스트 중간에 끼어들지 않으며, 2~6열 가로 카드 그리드와
    상·하 전폭 배너가 섞인 슬라이드도 카드별·밴드별로 자연스럽게 읽는다.
    """
    if len(units) <= 1 or depth >= 8:
        return sorted(units, key=lambda u: (round(u.bbox.top / 10.0), u.bbox.x0))

    y_cut = _find_best_cut(units, axis="y", min_gap=3.5)
    x_cut = _find_best_cut(units, axis="x", min_gap=8.0)

    chosen: tuple[float, list[_LayoutUnit], list[_LayoutUnit]] | None = None
    if y_cut is not None and x_cut is not None:
        chosen = x_cut if x_cut[0] > y_cut[0] else y_cut
    elif y_cut is not None:
        chosen = y_cut
    elif x_cut is not None:
        chosen = x_cut

    if chosen is None:
        return sorted(units, key=lambda u: (round(u.bbox.top / 10.0), u.bbox.x0))

    _, first_half, second_half = chosen
    return [*_order_units_xycut(first_half, depth + 1), *_order_units_xycut(second_half, depth + 1)]


def _sort_slide_units_reading_order(units: list[_LayoutUnit]) -> list[DocumentBlock]:
    if not units:
        return []

    title_anchors = sorted(
        [u for u in units if u.is_title_anchor],
        key=lambda u: (u.bbox.top, u.bbox.x0),
    )
    body_units = [u for u in units if not u.is_title_anchor]
    ordered_units = [*title_anchors, *_order_units_xycut(body_units)]

    out: list[DocumentBlock] = []
    order_idx = 0
    for unit in ordered_units:
        for blk in unit.blocks:
            order_idx += 1
            out.append(
                blk.model_copy(
                    update={"reading_order": order_idx, "column_index": unit.column_index}
                )
            )
    return out


def _extract_diagram_edges(slide: RawSlideData) -> list[str]:
    """연결선(`<p:cxnSp>`)이 잇는 도형 쌍의 텍스트 흐름(`A -> B`)을 복원한다."""
    id_to_label: dict[str, str] = {}
    for sp in slide.shapes:
        if sp.shape_id and sp.full_text:
            first_line = sp.full_text.splitlines()[0].strip()
            if first_line:
                id_to_label[sp.shape_id] = first_line[:60]

    edges: list[str] = []
    seen: set[str] = set()
    for cxn in slide.connectors:
        if not cxn.start_shape_id or not cxn.end_shape_id:
            continue
        src = id_to_label.get(cxn.start_shape_id)
        dst = id_to_label.get(cxn.end_shape_id)
        if src and dst and src != dst:
            edge = f"{src} -> {dst}"
            if edge not in seen:
                seen.add(edge)
                edges.append(edge)
    return edges


def analyze_slide(
    slide: RawSlideData,
    *,
    deck_body_font_size: float | None,
    repeating_logos: set[str],
    repeating_headers: set[str] | None = None,
    repeating_footers: set[str] | None = None,
    include_speaker_notes: bool = True,
    include_comments: bool = False,
) -> AnalyzedSlide:
    """단일 슬라이드의 구조 유형 분류, 파싱 전략 배정, 읽기 순서 블록 조립을 수행한다."""
    sw = max(1.0, slide.width_pt)
    sh = max(1.0, slide.height_pt)
    slide_area = sw * sh
    page_num = slide.slide_number
    rep_headers = repeating_headers or set()
    rep_footers = repeating_footers or set()

    # 1. 텍스트·머리글/바닥글·제목 분류 준비
    hf_ph_types = {"sldNum", "ftr", "dt", "hdr"}
    title_ph_types = {"title", "ctrTitle", "subTitle"}
    header_footer_shapes: list[RawPptxShape] = []
    content_shapes: list[RawPptxShape] = []
    has_running_header = False
    has_running_footer = False

    for sp in slide.shapes:
        if not sp.paragraphs:
            continue
        clipped = _clip_rect(sp.rect_pt, sw, sh)
        if sp.ph_type in hf_ph_types or sp.is_slide_num_field:
            header_footer_shapes.append(sp)
            if sp.ph_type == "hdr" or clipped[3] <= sh * 0.15:
                has_running_header = True
            else:
                has_running_footer = True
            continue
        txt = sp.full_text
        norm_pat = _normalize_hf_pattern(txt) if len(txt) <= 90 else ""
        if sp.ph_type not in title_ph_types and norm_pat:
            if clipped[3] <= sh * 0.10 and norm_pat in rep_headers:
                header_footer_shapes.append(sp)
                has_running_header = True
                continue
            if clipped[1] >= sh * 0.87 and norm_pat in rep_footers:
                header_footer_shapes.append(sp)
                has_running_footer = True
                continue
        if (
            sp.ph_type is None
            and len(txt) <= 32
            and (clipped[3] <= sh * 0.08 or clipped[1] >= sh * 0.92)
            and (txt.isdigit() or len(sp.paragraphs) == 1)
            and (
                txt.isdigit()
                or any(tok in txt.lower() for tok in ("confidential", "copyright", "©"))
            )
        ):
            header_footer_shapes.append(sp)
            if clipped[3] <= sh * 0.08:
                has_running_header = True
            else:
                has_running_footer = True
            continue
        content_shapes.append(sp)

    # 유효 문자 수 및 글꼴 크기 지표 집계
    all_content_text_parts = [sp.full_text for sp in content_shapes]
    for sa in slide.smartarts:
        all_content_text_parts.extend(txt for _, txt in sa.items)
    total_chars, usable_chars = _count_usable_chars("\n".join(all_content_text_parts))

    font_sizes: list[float] = []
    for sp in content_shapes:
        for p in sp.paragraphs:
            if p.font_size is not None:
                font_sizes.append(p.font_size)
    font_sizes.sort()
    median_font = font_sizes[len(font_sizes) // 2] if font_sizes else deck_body_font_size
    max_font = max(font_sizes) if font_sizes else None
    ref_body_font = deck_body_font_size or median_font or 14.0

    # 2. 네이티브 표 + 네이티브 차트(수치 있는 차트는 ExtractedTable로 승격)
    tables: list[ExtractedTable] = []
    for idx, raw_tbl in enumerate(slide.tables, 1):
        tbl = raw_tbl.table.model_copy(update={"table_id": f"p{page_num:03d}_t{idx:02d}"})
        tables.append(tbl)

    visuals: list[VisualRegion] = []
    visual_images: dict[str, RawPptxImage] = {}

    for chart in slide.charts:
        if chart.has_data:
            tbl = _chart_to_extracted_table(
                chart,
                slide_number=page_num,
                table_index=len(tables) + 1,
                slide_w=sw,
                slide_h=sh,
            )
            tables.append(tbl)
        else:
            rect = _clip_rect(chart.rect_pt, sw, sh)
            region_id = f"p{page_num:03d}_v{len(visuals) + 1:02d}"
            hints = [h for h in (chart.title, chart.chart_type_label, chart.name) if h]
            visuals.append(
                VisualRegion(
                    region_id=region_id,
                    page=page_num,
                    element_kind=ElementKind.VECTOR_DIAGRAM,
                    recommended_strategy=ParseStrategy.VLM_REGION,
                    bbox=make_bbox(rect[0], rect[1], rect[2], rect[3], sw, sh),
                    reason=(
                        f"캐시 수치가 비어 있는 PPTX 차트({chart.chart_type_label}) — "
                        "VLM 시각 해석 권장"
                    ),
                    confidence=0.88,
                    priority="high",
                    embedded_text_hints=hints,
                    caption=chart.title,
                )
            )

    # 2-b. 내장 OLE 객체(MSGraph 구형 차트·내장 Excel 시트 등) 보존 + VML/pic 미리보기 연결
    for ole in slide.ole_objects:
        rect = _clip_rect(ole.rect_pt, sw, sh)
        if (rect[2] - rect[0]) < 24.0 or (rect[3] - rect[1]) < 24.0:
            continue
        region_id = f"p{page_num:03d}_v{len(visuals) + 1:02d}"
        prog_lower = ole.prog_id.lower()
        is_excel_ole = "excel" in prog_lower or "sheet" in prog_lower
        ole_kind = ElementKind.TABLE if is_excel_ole else ElementKind.VECTOR_DIAGRAM
        ole_hints = [h for h in (ole.name, ole.prog_id) if h]
        prev = ole.preview_image
        prev_note = f", 미리보기 {prev.extension}" if prev is not None else ""
        visuals.append(
            VisualRegion(
                region_id=region_id,
                page=page_num,
                element_kind=ole_kind,
                recommended_strategy=ParseStrategy.VLM_REGION,
                bbox=make_bbox(rect[0], rect[1], rect[2], rect[3], sw, sh),
                reason=(
                    f"PPTX 내장 OLE 객체 `{ole.name}`"
                    f"({ole.prog_id or 'embedded'}{prev_note}) — VLM 시각 해석 대상"
                ),
                confidence=0.86,
                priority="high",
                image_width_px=prev.pixel_width if prev is not None else None,
                image_height_px=prev.pixel_height if prev is not None else None,
                embedded_text_hints=ole_hints,
            )
        )
        if prev is not None:
            visual_images[region_id] = prev

    # 3. 삽입 이미지 분류(반복 로고·아이콘·배경 벽지 필터링 + 픽토그램 군집 압축 + 스캔 판정)
    has_title_ph = any(sp.ph_type in title_ph_types for sp in content_shapes)
    meaningful_imgs: list[tuple[RawPptxImage, tuple[float, float, float, float]]] = []
    for img in slide.images:
        rect = _clip_rect(img.rect_pt, sw, sh)
        iw = rect[2] - rect[0]
        ih = rect[3] - rect[1]
        cov = (iw * ih) / slide_area
        if iw < 24.0 or ih < 24.0 or cov < 0.004:
            continue
        if img.media_part in repeating_logos and cov < 0.08:
            continue
        if cov >= 0.85 and (
            usable_chars >= 25
            or (has_title_ph and usable_chars >= 6)
            or len(tables) >= 1
            or len(slide.smartarts) >= 1
        ):
            continue
        meaningful_imgs.append((img, rect))

    img_coverage = clamp(_union_area([r for _, r in meaningful_imgs]) / slide_area, 0.0, 1.0)
    is_full_slide_scan = (
        img_coverage >= 0.65
        and usable_chars < 40
        and len(tables) == 0
        and len(slide.smartarts) == 0
        and len(slide.ole_objects) == 0
    )

    if is_full_slide_scan and meaningful_imgs:
        largest_img, largest_rect = max(meaningful_imgs, key=lambda pair: _rect_area(pair[1]))
        region_id = f"p{page_num:03d}_v{len(visuals) + 1:02d}"
        is_single_dom = (_rect_area(largest_rect) / slide_area) >= 0.65
        strategy = (
            ParseStrategy.EMBEDDED_IMAGE_OCR if is_single_dom else ParseStrategy.VLM_FULL_PAGE
        )
        clean_alt = _sanitize_alt_text(largest_img.alt_text)
        hints = list(
            dict.fromkeys([*([clean_alt] if clean_alt else []), *largest_img.svg_text_hints])
        )
        visuals.append(
            VisualRegion(
                region_id=region_id,
                page=page_num,
                element_kind=ElementKind.SCANNED_PAGE,
                recommended_strategy=strategy,
                bbox=make_bbox(0.0, 0.0, sw, sh, sw, sh),
                reason=(
                    f"슬라이드 전체 이미지/스크린샷(이미지 점유율 {img_coverage:.0%}, "
                    f"유효 텍스트 {usable_chars}자) — OCR/레이아웃 탐지 대상"
                ),
                confidence=0.94,
                priority="high",
                image_width_px=largest_img.pixel_width,
                image_height_px=largest_img.pixel_height,
                embedded_text_hints=hints,
            )
        )
        visual_images[region_id] = largest_img
    else:
        # 한 슬라이드에서 동일 소형 미디어가 4회 이상 반복되면 픽토그램 군집 1개로 압축
        by_media: dict[str, list[tuple[RawPptxImage, tuple[float, float, float, float]]]] = {}
        for pair in meaningful_imgs:
            by_media.setdefault(pair[0].media_part, []).append(pair)

        pictogram_parts: set[str] = set()
        for mp, group in by_media.items():
            if len(group) >= 4:
                covs = sorted(_rect_area(r) / slide_area for _, r in group)
                if covs[len(covs) // 2] < 0.10:
                    pictogram_parts.add(mp)

        emitted_pictograms: set[str] = set()
        meaningful_imgs.sort(key=lambda pair: (pair[1][1], pair[1][0]))
        for img, rect in meaningful_imgs:
            if img.media_part in pictogram_parts:
                if img.media_part in emitted_pictograms:
                    continue
                emitted_pictograms.add(img.media_part)
                group = by_media[img.media_part]
                gx0 = min(r[0] for _, r in group)
                gy0 = min(r[1] for _, r in group)
                gx1 = max(r[2] for _, r in group)
                gy1 = max(r[3] for _, r in group)
                gcov = ((gx1 - gx0) * (gy1 - gy0)) / slide_area
                region_id = f"p{page_num:03d}_v{len(visuals) + 1:02d}"
                clean_alt = _sanitize_alt_text(img.alt_text)
                hints = [clean_alt] if clean_alt else []
                visuals.append(
                    VisualRegion(
                        region_id=region_id,
                        page=page_num,
                        element_kind=ElementKind.VECTOR_DIAGRAM,
                        recommended_strategy=ParseStrategy.VLM_REGION,
                        bbox=make_bbox(gx0, gy0, gx1, gy1, sw, sh),
                        reason=(
                            f"동일 이미지 `{img.name or img.media_part}` {len(group)}회 반복 배치 "
                            f"(군집 면적 {gcov:.1%}) — 픽토그램/인포그래픽 군집"
                        ),
                        confidence=0.88,
                        priority="high" if gcov >= 0.20 else "medium",
                        image_width_px=img.pixel_width,
                        image_height_px=img.pixel_height,
                        embedded_text_hints=hints,
                    )
                )
                visual_images[region_id] = img
                continue

            cov = _rect_area(rect) / slide_area
            region_id = f"p{page_num:03d}_v{len(visuals) + 1:02d}"
            is_eq = _is_equation_image(img.name, img.alt_text)
            is_svg = img.extension == ".svg"
            if is_eq:
                elem_kind = ElementKind.FORMULA_OR_CODE
                strategy = ParseStrategy.VLM_REGION
            elif is_svg:
                elem_kind = ElementKind.VECTOR_DIAGRAM
                strategy = ParseStrategy.VLM_REGION
            else:
                elem_kind = ElementKind.FIGURE_IMAGE
                strategy = (
                    ParseStrategy.EMBEDDED_IMAGE_OCR
                    if (cov >= 0.45 and img.extension in _SUPPORTED_RASTER_EXTS)
                    else ParseStrategy.VLM_REGION
                )
            clean_alt = _sanitize_alt_text(img.alt_text)
            hints = list(dict.fromkeys([*([clean_alt] if clean_alt else []), *img.svg_text_hints]))
            size_desc = (
                f"{img.pixel_width}x{img.pixel_height}px, "
                if (img.pixel_width and img.pixel_height)
                else ""
            )
            kind_label = (
                "수식 이미지" if is_eq else ("SVG 벡터 그래픽" if is_svg else "삽입 이미지")
            )
            visuals.append(
                VisualRegion(
                    region_id=region_id,
                    page=page_num,
                    element_kind=elem_kind,
                    recommended_strategy=strategy,
                    bbox=make_bbox(rect[0], rect[1], rect[2], rect[3], sw, sh),
                    reason=(
                        f"PPTX {kind_label} `{img.name or img.media_part}` "
                        f"({size_desc}면적 {cov:.1%}) — OCR/VLM 시각 해석 대상"
                    ),
                    confidence=0.93,
                    priority="high" if (cov >= 0.20 or is_eq) else "medium",
                    image_width_px=img.pixel_width,
                    image_height_px=img.pixel_height,
                    embedded_text_hints=hints,
                )
            )
            visual_images[region_id] = img

    # 4. 도식/플로우차트(연결선 + 순서도 도형 군집) 탐지
    diagram_edges = _extract_diagram_edges(slide)
    flowchart_shapes = [
        sp
        for sp in slide.shapes
        if is_flowchart_preset(sp.prst_geom)
        or (sp.has_fill and sp.has_stroke and not sp.is_text_box and sp.ph_type is None)
    ]
    non_table_vector_count = len(slide.connectors) + len(flowchart_shapes) + len(slide.smartarts)
    has_diagram_cluster = (
        len(slide.connectors) >= 2
        or len(diagram_edges) >= 1
        or (len(flowchart_shapes) >= 4 and len(slide.connectors) >= 1)
    )
    if has_diagram_cluster and not is_full_slide_scan:
        diag_rects = [
            _clip_rect(c.rect_pt, sw, sh) for c in slide.connectors if _rect_area(c.rect_pt) > 0
        ]
        connected_ids = {
            sid for c in slide.connectors for sid in (c.start_shape_id, c.end_shape_id) if sid
        }
        for sp in slide.shapes:
            if sp.shape_id in connected_ids or is_flowchart_preset(sp.prst_geom):
                diag_rects.append(_clip_rect(sp.rect_pt, sw, sh))
        if diag_rects:
            dx0 = min(r[0] for r in diag_rects)
            dy0 = min(r[1] for r in diag_rects)
            dx1 = max(r[2] for r in diag_rects)
            dy1 = max(r[3] for r in diag_rects)
            dcov = ((dx1 - dx0) * (dy1 - dy0)) / slide_area
            if (dx1 - dx0) >= 50.0 and (dy1 - dy0) >= 35.0 and dcov >= 0.04:
                region_id = f"p{page_num:03d}_v{len(visuals) + 1:02d}"
                node_hints = [
                    sp.full_text.splitlines()[0].strip()[:60]
                    for sp in slide.shapes
                    if (sp.shape_id in connected_ids or is_flowchart_preset(sp.prst_geom))
                    and sp.full_text
                ]
                combined_hints = list(dict.fromkeys([*diagram_edges, *node_hints]))[:30]
                visuals.append(
                    VisualRegion(
                        region_id=region_id,
                        page=page_num,
                        element_kind=ElementKind.VECTOR_DIAGRAM,
                        recommended_strategy=ParseStrategy.VLM_REGION,
                        bbox=make_bbox(dx0, dy0, dx1, dy1, sw, sh),
                        reason=(
                            f"PPTX 도형/연결선 다이어그램(연결선 {len(slide.connectors)}개, "
                            f"도형 {len(flowchart_shapes)}개, 흐름 {len(diagram_edges)}쌍)"
                        ),
                        confidence=0.87,
                        priority="high" if dcov >= 0.15 else "medium",
                        embedded_text_hints=combined_hints,
                    )
                )

    # 5. 캡션 후보 도형과 표/이미지 연결
    caption_shape_ids: set[int] = set()
    for sp_idx, sp in enumerate(content_shapes):
        if sp.ph_type in ("title", "ctrTitle", "subTitle"):
            continue
        txt = sp.full_text
        if len(txt) > 130 or len(sp.paragraphs) > 2:
            continue
        if not _CAPTION_RE.match(txt):
            continue
        srect = _clip_rect(sp.rect_pt, sw, sh)
        # 가장 가까운 표 또는 시각 영역에 캡션 연결
        best_target: tuple[str, int, float] | None = None
        for t_idx, tbl in enumerate(tables):
            if tbl.caption:
                continue
            v_dist = min(abs(srect[3] - tbl.bbox.top), abs(srect[1] - tbl.bbox.bottom))
            if v_dist <= 45.0 and (best_target is None or v_dist < best_target[2]):
                best_target = ("table", t_idx, v_dist)
        for v_idx, vis in enumerate(visuals):
            if vis.caption or vis.element_kind == ElementKind.SCANNED_PAGE:
                continue
            v_dist = min(abs(srect[3] - vis.bbox.top), abs(srect[1] - vis.bbox.bottom))
            if v_dist <= 45.0 and (best_target is None or v_dist < best_target[2]):
                best_target = ("visual", v_idx, v_dist)
        if best_target is not None:
            kind_t, target_idx, _ = best_target
            if kind_t == "table":
                tables[target_idx] = tables[target_idx].model_copy(update={"caption": txt})
            else:
                visuals[target_idx] = visuals[target_idx].model_copy(update={"caption": txt})
            caption_shape_ids.add(sp_idx)

    # 6. 다단 여백(gutter) 탐지(다이어그램 노드 박스는 본문 다단 후보에서 제외)
    diagram_node_ids: set[str] = set()
    if has_diagram_cluster:
        diagram_node_ids = {
            sid for c in slide.connectors for sid in (c.start_shape_id, c.end_shape_id) if sid
        }
    layout_rects: list[tuple[float, float, float, float]] = []
    for sp_idx, sp in enumerate(content_shapes):
        if (
            sp_idx in caption_shape_ids
            or sp.ph_type in ("title", "ctrTitle", "subTitle")
            or (
                has_diagram_cluster
                and (sp.shape_id in diagram_node_ids or is_flowchart_preset(sp.prst_geom))
            )
        ):
            continue
        layout_rects.append(_clip_rect(sp.rect_pt, sw, sh))
    for tbl in tables:
        layout_rects.append((tbl.bbox.x0, tbl.bbox.top, tbl.bbox.x1, tbl.bbox.bottom))
    for vis in visuals:
        if vis.element_kind != ElementKind.SCANNED_PAGE:
            layout_rects.append((vis.bbox.x0, vis.bbox.top, vis.bbox.x1, vis.bbox.bottom))
    for sa in slide.smartarts:
        layout_rects.append(_clip_rect(sa.rect_pt, sw, sh))

    gutters_x = _detect_slide_column_gutters(layout_rects, sw, sh)
    column_count = len(gutters_x) + 1

    # 7. 통합 DocumentBlock 목록 조립(도형/SmartArt/표/시각 영역을 원자 단위로 묶어 XY-Cut 정렬)
    layout_units: list[_LayoutUnit] = []
    slide_title: str | None = None
    has_explicit_title = any(sp.ph_type in ("title", "ctrTitle") for sp in content_shapes)
    block_seq = 0
    line_count = 0

    for sp_idx, sp in enumerate(content_shapes):
        rect = _clip_rect(sp.rect_pt, sw, sh)
        bbox = make_bbox(rect[0], rect[1], rect[2], rect[3], sw, sh)
        col_idx, _ = _assign_column(bbox, gutters_x)

        if sp_idx in caption_shape_ids:
            block_seq += 1
            layout_units.append(
                _LayoutUnit(
                    bbox=bbox,
                    column_index=col_idx,
                    is_title_anchor=False,
                    blocks=[
                        DocumentBlock(
                            block_id=f"p{page_num:03d}_b{block_seq:03d}",
                            page=page_num,
                            reading_order=0,
                            column_index=col_idx,
                            kind=ElementKind.CAPTION,
                            strategy=ParseStrategy.NATIVE_TEXT,
                            bbox=bbox,
                            text=sp.full_text,
                            confidence=0.95,
                        )
                    ],
                )
            )
            continue

        is_ph_title = sp.ph_type in ("title", "ctrTitle")
        is_ph_subtitle = sp.ph_type == "subTitle"

        # 비플레이스홀더 상단 큰 제목 추정
        first_para = sp.paragraphs[0]
        first_font = first_para.font_size or ref_body_font
        inferred_title = (
            not has_explicit_title
            and slide_title is None
            and len(sp.paragraphs) == 1
            and len(first_para.text) <= 90
            and rect[1] <= sh * 0.24
            and (
                first_font >= max(18.0, ref_body_font * 1.28)
                or (first_para.is_bold and first_font >= 16.0)
            )
        )

        if is_ph_title or inferred_title:
            title_txt = _normalize_heading_text(" ".join(p.text for p in sp.paragraphs))
            if slide_title is None:
                slide_title = title_txt
            block_seq += 1
            line_count += len(sp.paragraphs)
            layout_units.append(
                _LayoutUnit(
                    bbox=bbox,
                    column_index=0,
                    is_title_anchor=True,
                    blocks=[
                        DocumentBlock(
                            block_id=f"p{page_num:03d}_b{block_seq:03d}",
                            page=page_num,
                            reading_order=0,
                            column_index=0,
                            kind=ElementKind.HEADING,
                            strategy=ParseStrategy.NATIVE_TEXT,
                            bbox=bbox,
                            text=title_txt,
                            heading_level=1,
                            font_size=first_para.font_size,
                            is_bold=first_para.is_bold,
                            confidence=0.98 if is_ph_title else 0.90,
                        )
                    ],
                )
            )
            continue

        if is_ph_subtitle:
            sub_txt = _normalize_heading_text(" ".join(p.text for p in sp.paragraphs))
            block_seq += 1
            line_count += len(sp.paragraphs)
            layout_units.append(
                _LayoutUnit(
                    bbox=bbox,
                    column_index=0,
                    is_title_anchor=True,
                    blocks=[
                        DocumentBlock(
                            block_id=f"p{page_num:03d}_b{block_seq:03d}",
                            page=page_num,
                            reading_order=0,
                            column_index=0,
                            kind=ElementKind.HEADING,
                            strategy=ParseStrategy.NATIVE_TEXT,
                            bbox=bbox,
                            text=sub_txt,
                            heading_level=2,
                            font_size=first_para.font_size,
                            is_bold=first_para.is_bold,
                            confidence=0.96,
                        )
                    ],
                )
            )
            continue

        # 일반 본문 도형: 문단들은 한 원자 단위(_LayoutUnit)로 묶어 다른 도형·이미지와 뒤섞임 방지
        n_paras = max(1, len(sp.paragraphs))
        para_h = max(1.0, (rect[3] - rect[1]) / n_paras)
        shape_blocks: list[DocumentBlock] = []
        for p_idx, p in enumerate(sp.paragraphs):
            line_count += 1
            p_top = rect[1] + p_idx * para_h
            p_bot = min(rect[3], p_top + para_h)
            p_bbox = make_bbox(rect[0], p_top, rect[2], p_bot, sw, sh)
            p_font = p.font_size or ref_body_font
            p_strat = (
                ParseStrategy.MULTI_COLUMN_LAYOUT
                if column_count >= 2
                else ParseStrategy.NATIVE_TEXT
            )

            block_seq += 1
            if p.has_math and not p.is_bullet and len(p.text) <= 160:
                kind = ElementKind.FORMULA_OR_CODE
                heading_lvl = None
                text_out = p.text
            elif p.is_bullet:
                kind = ElementKind.LIST_ITEM
                heading_lvl = None
                text_out = f"o {p.text}" if p.level >= 1 else p.text
            elif (
                len(p.text) <= 75
                and p_idx == 0
                and (
                    p_font >= max(16.0, ref_body_font * 1.22)
                    or (p.is_bold and p_font >= ref_body_font * 1.05 and len(sp.paragraphs) >= 2)
                )
            ):
                kind = ElementKind.HEADING
                heading_lvl = 2
                text_out = _normalize_heading_text(p.text)
            else:
                kind = ElementKind.PARAGRAPH
                heading_lvl = None
                text_out = p.text

            shape_blocks.append(
                DocumentBlock(
                    block_id=f"p{page_num:03d}_b{block_seq:03d}",
                    page=page_num,
                    reading_order=0,
                    column_index=col_idx,
                    kind=kind,
                    strategy=p_strat,
                    bbox=p_bbox,
                    text=text_out,
                    heading_level=heading_lvl,
                    font_size=p.font_size,
                    is_bold=p.is_bold,
                    confidence=0.96,
                )
            )
        if shape_blocks:
            layout_units.append(
                _LayoutUnit(
                    bbox=bbox,
                    column_index=col_idx,
                    is_title_anchor=False,
                    blocks=shape_blocks,
                )
            )

    # SmartArt 계층 노드 → 원자 단위 LIST_ITEM 블록 묶음
    for sa in slide.smartarts:
        rect = _clip_rect(sa.rect_pt, sw, sh)
        bbox = make_bbox(rect[0], rect[1], rect[2], rect[3], sw, sh)
        col_idx, _ = _assign_column(bbox, gutters_x)
        n_items = max(1, len(sa.items))
        step_h = max(1.0, (rect[3] - rect[1]) / n_items)
        sa_blocks: list[DocumentBlock] = []
        for idx, (depth, txt) in enumerate(sa.items):
            block_seq += 1
            line_count += 1
            it_top = rect[1] + idx * step_h
            it_bot = min(rect[3], it_top + step_h)
            it_bbox = make_bbox(rect[0], it_top, rect[2], it_bot, sw, sh)
            sa_blocks.append(
                DocumentBlock(
                    block_id=f"p{page_num:03d}_b{block_seq:03d}",
                    page=page_num,
                    reading_order=0,
                    column_index=col_idx,
                    kind=ElementKind.LIST_ITEM,
                    strategy=ParseStrategy.NATIVE_TEXT,
                    bbox=it_bbox,
                    text=f"o {txt}" if depth >= 1 else txt,
                    confidence=0.95,
                )
            )
        if sa_blocks:
            layout_units.append(
                _LayoutUnit(
                    bbox=bbox,
                    column_index=col_idx,
                    is_title_anchor=False,
                    blocks=sa_blocks,
                )
            )

    # 표 블록 추가
    for tbl in tables:
        col_idx, _ = _assign_column(tbl.bbox, gutters_x)
        layout_units.append(
            _LayoutUnit(
                bbox=tbl.bbox,
                column_index=col_idx,
                is_title_anchor=False,
                blocks=[
                    DocumentBlock(
                        block_id=f"{tbl.table_id}_blk",
                        page=page_num,
                        reading_order=0,
                        column_index=col_idx,
                        kind=ElementKind.TABLE,
                        strategy=tbl.strategy,
                        bbox=tbl.bbox,
                        text=tbl.caption or "",
                        table_id=tbl.table_id,
                        confidence=tbl.confidence,
                    )
                ],
            )
        )

    # 시각 영역 블록 추가
    for vis in visuals:
        col_idx, _ = _assign_column(vis.bbox, gutters_x)
        layout_units.append(
            _LayoutUnit(
                bbox=vis.bbox,
                column_index=col_idx,
                is_title_anchor=False,
                blocks=[
                    DocumentBlock(
                        block_id=f"{vis.region_id}_blk",
                        page=page_num,
                        reading_order=0,
                        column_index=col_idx,
                        kind=vis.element_kind,
                        strategy=vis.recommended_strategy,
                        bbox=vis.bbox,
                        text=vis.caption or "",
                        visual_region_id=vis.region_id,
                        confidence=vis.confidence,
                    )
                ],
            )
        )

    blocks = _sort_slide_units_reading_order(layout_units)

    # 연결선 기반 도식 흐름 관계가 있으면 결정론 요약 블록을 슬라이드 하단에 보강
    if diagram_edges:
        block_seq += 1
        blocks.append(
            DocumentBlock(
                block_id=f"p{page_num:03d}_b{block_seq:03d}",
                page=page_num,
                reading_order=len(blocks) + 1,
                column_index=0,
                kind=ElementKind.PARAGRAPH,
                strategy=ParseStrategy.NATIVE_TEXT,
                bbox=make_bbox(0.0, sh * 0.90, sw, sh * 0.96, sw, sh),
                text="[도식 연결 흐름] " + " ; ".join(diagram_edges),
                confidence=0.92,
            )
        )

    # 발표자 노트 추가(옵션 활성화 시)
    if include_speaker_notes and slide.speaker_notes:
        block_seq += 1
        blocks.append(
            DocumentBlock(
                block_id=f"p{page_num:03d}_notes",
                page=page_num,
                reading_order=len(blocks) + 1,
                column_index=0,
                kind=ElementKind.PARAGRAPH,
                strategy=ParseStrategy.NATIVE_TEXT,
                bbox=make_bbox(0.0, sh * 0.96, sw, sh, sw, sh),
                text=f"[발표자 노트] {slide.speaker_notes}",
                confidence=0.99,
            )
        )

    # 슬라이드 검토 주석 추가(옵션 활성화 시)
    if include_comments and slide.comments:
        comment_lines: list[str] = []
        for cm in slide.comments:
            prefix = "↳ " if cm.is_reply else ""
            date_part = f" ({cm.timestamp[:10]})" if len(cm.timestamp) >= 10 else ""
            comment_lines.append(f"{prefix}{cm.author}{date_part}: {cm.text}")
        blocks.append(
            DocumentBlock(
                block_id=f"p{page_num:03d}_comments",
                page=page_num,
                reading_order=len(blocks) + 1,
                column_index=0,
                kind=ElementKind.PARAGRAPH,
                strategy=ParseStrategy.NATIVE_TEXT,
                bbox=make_bbox(0.0, sh * 0.97, sw, sh, sw, sh),
                text="[슬라이드 주석]\n" + "\n".join(comment_lines),
                confidence=0.99,
            )
        )

    hf_blocks: list[DocumentBlock] = []
    for hf_idx, sp in enumerate(header_footer_shapes, 1):
        rect = _clip_rect(sp.rect_pt, sw, sh)
        hf_blocks.append(
            DocumentBlock(
                block_id=f"p{page_num:03d}_hf{hf_idx:02d}",
                page=page_num,
                reading_order=hf_idx,
                column_index=0,
                kind=ElementKind.HEADER_FOOTER,
                strategy=ParseStrategy.SKIP_DECORATIVE,
                bbox=make_bbox(rect[0], rect[1], rect[2], rect[3], sw, sh),
                text=sp.full_text,
                font_size=sp.paragraphs[0].font_size if sp.paragraphs else None,
                confidence=0.96,
            )
        )

    # 8. 페이지 지표(PageMetrics) 및 구조 분류(PageRouteDecision)
    table_boxes = [(t.bbox.x0, t.bbox.top, t.bbox.x1, t.bbox.bottom) for t in tables]
    table_area_ratio = round(clamp(_union_area(table_boxes) / slide_area, 0.0, 1.0), 4)
    visual_boxes = [(v.bbox.x0, v.bbox.top, v.bbox.x1, v.bbox.bottom) for v in visuals]
    visual_area_ratio = round(clamp(_union_area(visual_boxes) / slide_area, 0.0, 1.0), 4)

    metrics = PageMetrics(
        page=page_num,
        width_pt=round(sw, 2),
        height_pt=round(sh, 2),
        rotation=0,
        aspect_ratio=round(sw / sh, 3),
        char_count=total_chars,
        usable_char_count=usable_chars,
        usable_text_ratio=round(usable_chars / total_chars, 4) if total_chars > 0 else 0.0,
        garbage_char_ratio=round((total_chars - usable_chars) / total_chars, 4)
        if total_chars > 0
        else 0.0,
        line_count=line_count,
        column_count=column_count,
        column_gutters_x=gutters_x,
        median_font_size=median_font,
        max_font_size=max_font,
        image_count=len(slide.images),
        non_decorative_image_count=len(meaningful_imgs),
        image_coverage=round(img_coverage, 4),
        vector_line_count=len(slide.connectors),
        vector_rect_count=len(flowchart_shapes),
        vector_curve_count=len(slide.smartarts),
        non_table_vector_count=non_table_vector_count,
        table_count=len(tables),
        table_area_ratio=table_area_ratio,
        visual_region_count=len(visuals),
        visual_area_ratio=visual_area_ratio,
        header_footer_line_count=len(hf_blocks),
    )

    reasons: list[str] = []
    has_vector_diagrams = any(v.element_kind == ElementKind.VECTOR_DIAGRAM for v in visuals)

    if (
        usable_chars < 3
        and len(tables) == 0
        and len(visuals) == 0
        and len(slide.smartarts) == 0
        and non_table_vector_count == 0
    ):
        category = PageCategory.BLANK
        primary_strategy = ParseStrategy.SKIP_DECORATIVE
        confidence = 0.99
        reasons.append("텍스트·표·차트·이미지·도식이 없는 빈 슬라이드")
    elif is_full_slide_scan:
        category = PageCategory.SCANNED_IMAGE
        primary_strategy = (
            visuals[0].recommended_strategy if visuals else ParseStrategy.VLM_FULL_PAGE
        )
        confidence = 0.94
        reasons.append(
            f"슬라이드 전체 이미지 점유율 {img_coverage:.0%}, "
            f"디지털 텍스트 {usable_chars}자 → 스캔/스크린샷 슬라이드"
        )
    elif len(tables) >= 1 and (table_area_ratio >= 0.25 or usable_chars < 180):
        category = PageCategory.TABLE_HEAVY
        primary_strategy = ParseStrategy.RULED_TABLE
        confidence = 0.97
        chart_cnt = sum(1 for c in slide.charts if c.has_data)
        detail = f"네이티브 차트 {chart_cnt}개 포함" if chart_cnt > 0 else "네이티브 표 중심"
        reasons.append(
            f"표/차트 데이터 {len(tables)}개({detail}, 면적 {table_area_ratio:.0%}) → "
            "OOXML 구조 직접 추출"
        )
    elif (has_vector_diagrams or len(slide.smartarts) >= 1) and (
        visual_area_ratio >= 0.20 or len(slide.smartarts) >= 1
    ):
        category = PageCategory.VECTOR_DIAGRAM_HEAVY
        primary_strategy = (
            ParseStrategy.VLM_REGION if has_vector_diagrams else ParseStrategy.NATIVE_TEXT
        )
        confidence = 0.90
        reasons.append(
            f"SmartArt {len(slide.smartarts)}개·연결선 {len(slide.connectors)}개·"
            f"도형 {len(flowchart_shapes)}개 → 다이어그램/도식 중심 슬라이드"
        )
    elif column_count >= 2 and usable_chars >= 40:
        category = PageCategory.MULTI_COLUMN
        primary_strategy = ParseStrategy.MULTI_COLUMN_LAYOUT
        confidence = 0.91
        gutter_desc = ", ".join(f"x={g:.0f}pt" for g in gutters_x)
        reasons.append(
            f"{column_count}단 슬라이드 여백({gutter_desc}) 감지 → 다단 XY-Cut 읽기 순서"
        )
    elif (
        usable_chars >= 20
        and len(visuals) >= 1
        and (img_coverage >= 0.10 or visual_area_ratio >= 0.12)
    ):
        category = PageCategory.HYBRID_TEXT_IMAGE
        primary_strategy = ParseStrategy.NATIVE_TEXT
        confidence = 0.92
        reasons.append(
            f"디지털 텍스트({usable_chars}자)와 삽입 이미지/도식 {len(visuals)}개 혼재 → "
            "텍스트 직접 추출 + 시각 영역 OCR/VLM 분기"
        )
    else:
        category = PageCategory.SLIDE_LIKE
        primary_strategy = ParseStrategy.NATIVE_TEXT
        confidence = 0.95
        reasons.append(
            f"표준 PPTX 슬라이드({usable_chars}자, {line_count}문단, "
            f"종횡비 {metrics.aspect_ratio:.2f}) → 네이티브 XML 직접 추출"
        )

    if len(tables) >= 1 and category != PageCategory.TABLE_HEAVY:
        reasons.append(f"보조 표/차트 {len(tables)}개 직접 추출")
    if len(visuals) >= 1 and category not in (
        PageCategory.SCANNED_IMAGE,
        PageCategory.HYBRID_TEXT_IMAGE,
        PageCategory.VECTOR_DIAGRAM_HEAVY,
    ):
        reasons.append(f"삽입 시각 영역 {len(visuals)}개 추출·OCR/VLM 태깅")
    if include_speaker_notes and slide.speaker_notes:
        reasons.append("발표자 노트 포함")
    if include_comments and slide.comments:
        reasons.append(f"슬라이드 주석 {len(slide.comments)}건 포함")
    if hf_blocks:
        reasons.append(f"머리글/바닥글·슬라이드 번호 {len(hf_blocks)}개 분리")

    # 영역별 라우팅 목록 조립
    regions: list[RegionRoute] = []
    for tbl in tables:
        regions.append(
            RegionRoute(
                region_id=tbl.table_id,
                element_kind=ElementKind.TABLE,
                strategy=tbl.strategy,
                bbox=tbl.bbox,
                confidence=tbl.confidence,
                reason=tbl.reason,
                requires_vision=False,
            )
        )
    for vis in visuals:
        regions.append(
            RegionRoute(
                region_id=vis.region_id,
                element_kind=vis.element_kind,
                strategy=vis.recommended_strategy,
                bbox=vis.bbox,
                confidence=vis.confidence,
                reason=vis.reason,
                requires_vision=True,
            )
        )
    for blk in blocks:
        if blk.table_id is not None or blk.visual_region_id is not None:
            continue
        regions.append(
            RegionRoute(
                region_id=blk.block_id,
                element_kind=blk.kind,
                strategy=blk.strategy,
                bbox=blk.bbox,
                confidence=blk.confidence,
                reason=(
                    f"제목 L{blk.heading_level}"
                    if blk.kind == ElementKind.HEADING and blk.heading_level
                    else f"컬럼 {blk.column_index + 1} {blk.kind.value} 블록"
                ),
                requires_vision=False,
            )
        )
    regions.sort(key=lambda r: (r.bbox.top, r.bbox.x0))

    all_strats = [r.strategy for r in regions]
    secondary_strategies = list(dict.fromkeys(s for s in all_strats if s != primary_strategy))

    route = PageRouteDecision(
        page=page_num,
        category=category,
        primary_strategy=primary_strategy,
        secondary_strategies=secondary_strategies,
        confidence=confidence,
        reasons=reasons,
        metrics=metrics,
        regions=regions,
        requires_vision=len(visuals) > 0,
        warnings=list(slide.warnings),
    )
    return AnalyzedSlide(
        raw=slide,
        tables=tables,
        visuals=visuals,
        visual_images=visual_images,
        blocks=blocks,
        header_footer_blocks=hf_blocks,
        route=route,
        slide_title=slide_title,
        is_full_slide_scan=is_full_slide_scan,
        has_running_header=has_running_header,
        has_running_footer=has_running_footer,
    )


def build_pptx_outline(
    analyzed_slides: list[AnalyzedSlide],
    sections: list[SectionRef],
) -> list[OutlineEntry]:
    """PowerPoint 섹션(`p14:sectionLst`)과 슬라이드 제목으로 문서 목차(Outline)를 구성한다."""
    slide_by_id = {item.raw.slide_id: item for item in analyzed_slides}
    outline: list[OutlineEntry] = []

    if sections:
        emitted_ids: set[str] = set()
        for sec in sections:
            first_slide = next(
                (slide_by_id[sid].raw.slide_number for sid in sec.slide_ids if sid in slide_by_id),
                None,
            )
            outline.append(OutlineEntry(level=1, title=sec.name, page=first_slide))
            for sid in sec.slide_ids:
                item = slide_by_id.get(sid)
                if item is None:
                    continue
                emitted_ids.add(sid)
                if item.slide_title:
                    outline.append(
                        OutlineEntry(level=2, title=item.slide_title, page=item.raw.slide_number)
                    )
        for item in analyzed_slides:
            if item.raw.slide_id not in emitted_ids and item.slide_title:
                outline.append(
                    OutlineEntry(level=1, title=item.slide_title, page=item.raw.slide_number)
                )
        return outline

    for item in analyzed_slides:
        if item.slide_title:
            outline.append(
                OutlineEntry(level=1, title=item.slide_title, page=item.raw.slide_number)
            )
    return outline


def build_pptx_routing_plan(
    metadata: DocumentMetadata,
    analyzed_slides: list[AnalyzedSlide],
    sections: list[SectionRef],
    *,
    body_font_size: float | None,
) -> RoutingPlan:
    """슬라이드 분석 결과 목록으로 통합 `RoutingPlan`을 조립한다."""
    outline = build_pptx_outline(analyzed_slides, sections)
    updated_meta = metadata.model_copy(update={"has_outline": len(outline) > 0})

    cat_counts: Counter[str] = Counter()
    strat_counts: Counter[str] = Counter()
    vision_pages: list[int] = []
    multi_col_pages: list[int] = []
    total_tables = 0
    total_visuals = 0
    has_running_hdr = any(item.has_running_header for item in analyzed_slides)
    has_running_ftr = any(item.has_running_footer for item in analyzed_slides)

    decisions = [item.route for item in analyzed_slides]
    for dec in decisions:
        cat_counts[dec.category.value] += 1
        strat_counts[dec.primary_strategy.value] += 1
        for sec in dec.secondary_strategies:
            strat_counts[sec.value] += 1
        total_tables += dec.metrics.table_count
        total_visuals += dec.metrics.visual_region_count
        if dec.requires_vision:
            vision_pages.append(dec.page)
        if dec.metrics.column_count >= 2:
            multi_col_pages.append(dec.page)

    summary = RoutingSummary(
        total_pages=updated_meta.page_count,
        analyzed_pages=len(decisions),
        category_counts=dict(sorted(cat_counts.items())),
        strategy_counts=dict(sorted(strat_counts.items())),
        table_count=total_tables,
        visual_region_count=total_visuals,
        vision_required_pages=vision_pages,
        multi_column_pages=multi_col_pages,
        detected_body_font_size=body_font_size,
        running_header_detected=has_running_hdr,
        running_footer_detected=has_running_ftr,
    )
    return RoutingPlan(
        document=updated_meta,
        outline=outline,
        summary=summary,
        pages=decisions,
    )
