"""DOCX 페이지별 구조 분류, 캡션 연결, 차트/표 승격, 목차(Outline) 및 RoutingPlan 조립."""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field

from autoetl.parser.contracts import (
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
    VisualRegion,
)
from autoetl.parser.docx.elements import (
    RawDocxBodyItem,
    RawDocxPage,
    RawDocxParagraph,
    RawDocxTableItem,
    RawDocxVectorDiagram,
)
from autoetl.parser.docx.ooxml import clamp, make_bbox
from autoetl.parser.pptx.elements import (
    RawPptxChart,
    RawPptxImage,
    RawPptxOleObject,
    RawPptxSmartArt,
)
from autoetl.parser.pptx.router import (
    _chart_to_extracted_table,
    _is_equation_image,
    _normalize_heading_text,
    _sanitize_alt_text,
)

_CAPTION_RE = re.compile(
    r"^(?:<\s*|\[)?(?:표|그림|차트|도표|도식|사진|수식|Table|Tab\.|Figure|Fig\.|Chart|Diagram|Equation)\s*[0-9IVX\-]+",
    re.IGNORECASE,
)
_TABLE_CAPTION_RE = re.compile(
    r"^(?:<\s*|\[)?(?:표|도표|Table|Tab\.)\s*[0-9IVX\-]+",
    re.IGNORECASE,
)
_FIGURE_CAPTION_RE = re.compile(
    r"^(?:<\s*|\[)?(?:그림|차트|도식|사진|Figure|Fig\.|Chart|Diagram)\s*[0-9IVX\-]+",
    re.IGNORECASE,
)

_SUPPORTED_RASTER_EXTS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"})


@dataclass
class AnalyzedDocxPage:
    """구조 분석과 전략 라우팅이 완료된 단일 DOCX 페이지 결과."""

    raw: RawDocxPage
    tables: list[ExtractedTable]
    visuals: list[VisualRegion]
    visual_images: dict[str, RawPptxImage]
    blocks: list[DocumentBlock]
    header_footer_blocks: list[DocumentBlock]
    route: PageRouteDecision
    headings: list[tuple[int, str]] = field(default_factory=list)
    is_full_page_scan: bool = False
    has_running_header: bool = False
    has_running_footer: bool = False
    layout_page_image_bytes: bytes | None = None
    layout_page_image_ext: str | None = None
    layout_page_image_size: tuple[int, int] | None = None
    layout_crop_rects: dict[str, tuple[int, int, int, int]] = field(default_factory=dict)
    layout_segmented: bool = False


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


def estimate_docx_body_font_size(pages: list[RawDocxPage]) -> float | None:
    """문서 전체의 일반 본문(제목 제외) 글꼴 크기 중앙값을 추정한다."""
    sizes: list[float] = []
    for page in pages:
        for it in page.items:
            if (
                isinstance(it, RawDocxParagraph)
                and it.heading_level is None
                and it.font_size_pt is not None
                and 6.0 <= it.font_size_pt <= 36.0
            ):
                weight = max(1, min(25, len(it.text) // 12))
                sizes.extend([it.font_size_pt] * weight)
    if not sizes:
        return None
    sizes.sort()
    return round(sizes[len(sizes) // 2], 1)


def find_repeating_docx_logos(pages: list[RawDocxPage]) -> set[str]:
    """3페이지 이상 문서에서 60% 이상 페이지에 반복 등장하는 소형 로고 미디어 경로를 찾는다."""
    if len(pages) < 3:
        return set()
    counts: Counter[str] = Counter()
    for page in pages:
        page_area = max(1.0, page.width_pt * page.height_pt)
        seen: set[str] = set()
        for it in page.items:
            if isinstance(it, RawPptxImage):
                w = max(0.0, it.rect_pt[2] - it.rect_pt[0])
                h = max(0.0, it.rect_pt[3] - it.rect_pt[1])
                if (w * h) / page_area < 0.08:
                    seen.add(it.media_part)
        for mp in seen:
            counts[mp] += 1
    threshold = max(3, int(len(pages) * 0.6))
    return {mp for mp, cnt in counts.items() if cnt >= threshold}


def _compute_column_gutters(page: RawDocxPage) -> list[float]:
    if page.column_count < 2:
        return []
    usable_w = max(120.0, page.width_pt - page.margin_left_pt - page.margin_right_pt)
    n_cols = min(4, page.column_count)
    total_space = page.column_space_pt * (n_cols - 1)
    col_w = max(40.0, (usable_w - total_space) / n_cols)
    gutters: list[float] = []
    for i in range(1, n_cols):
        gx = page.margin_left_pt + i * col_w + (i - 0.5) * page.column_space_pt
        gutters.append(round(gx, 1))
    return gutters


def _estimate_raw_item_h(it: RawDocxBodyItem, col_w: float) -> float:
    if isinstance(it, RawDocxParagraph):
        font = it.font_size_pt or 11.0
        cpl = max(16.0, col_w / max(6.0, font * 0.55))
        lines = sum(max(1, int(len(ln) / cpl) + 1) for ln in (it.text.splitlines() or [it.text]))
        return lines * (font * 1.35) + (10.0 if it.heading_level else 5.0)
    if isinstance(it, RawDocxTableItem):
        return it.raw_height_pt + 10.0
    if isinstance(it, RawPptxImage):
        return max(28.0, it.rect_pt[3] - it.rect_pt[1]) + 8.0
    if isinstance(it, (RawPptxChart, RawPptxSmartArt, RawPptxOleObject)):
        return max(70.0, it.rect_pt[3] - it.rect_pt[1]) + 10.0
    if isinstance(it, RawDocxVectorDiagram):
        return max(60.0, it.height_pt) + 10.0
    return 20.0


def analyze_docx_page(
    page: RawDocxPage,
    *,
    doc_body_font_size: float | None,
    repeating_logos: set[str],
    include_comments: bool = False,
) -> AnalyzedDocxPage:
    """단일 DOCX 페이지의 구조 유형 분류, 파싱 전략 배정, 캡션 연결, 블록 조립을 수행한다."""
    pw = max(100.0, page.width_pt)
    ph = max(100.0, page.height_pt)
    page_area = pw * ph
    p_num = page.page_number

    m_top = clamp(page.margin_top_pt, 18.0, ph * 0.25)
    m_bot = clamp(page.margin_bottom_pt, 18.0, ph * 0.25)
    m_left = clamp(page.margin_left_pt, 18.0, pw * 0.25)
    m_right = clamp(page.margin_right_pt, 18.0, pw * 0.25)
    usable_w = max(80.0, pw - m_left - m_right)
    usable_h = max(120.0, ph - m_top - m_bot)

    gutters_x = _compute_column_gutters(page)
    n_cols = len(gutters_x) + 1
    col_bounds: list[tuple[float, float]] = []
    if n_cols == 1:
        col_bounds.append((m_left, pw - m_right))
    else:
        boundaries = [m_left, *gutters_x, pw - m_right]
        half_sp = page.column_space_pt / 2.0
        for c_i in range(n_cols):
            cx0 = boundaries[c_i] + (0.0 if c_i == 0 else half_sp)
            cx1 = boundaries[c_i + 1] - (0.0 if c_i == n_cols - 1 else half_sp)
            col_bounds.append((cx0, max(cx0 + 30.0, cx1)))

    # 1. 텍스트·글꼴 통계 집계
    text_parts: list[str] = []
    font_sizes: list[float] = []
    line_count = 0
    smartart_count = 0
    for it in page.items:
        if isinstance(it, RawDocxParagraph):
            text_parts.append(it.text)
            line_count += max(1, len(it.text.splitlines()))
            if it.font_size_pt is not None:
                font_sizes.append(it.font_size_pt)
        elif isinstance(it, RawPptxSmartArt):
            smartart_count += 1
            for _, stxt in it.items:
                text_parts.append(stxt)
                line_count += 1

    total_chars, usable_chars = _count_usable_chars("\n".join(text_parts))
    font_sizes.sort()
    median_font = font_sizes[len(font_sizes) // 2] if font_sizes else doc_body_font_size
    max_font = max(font_sizes) if font_sizes else None
    ref_body_font = doc_body_font_size or median_font or 11.0

    # 2. 전체 스캔 이미지 페이지 사전 판정
    raw_images = [it for it in page.items if isinstance(it, RawPptxImage)]
    meaningful_raw_imgs: list[RawPptxImage] = []
    for img in raw_images:
        iw = max(0.0, img.rect_pt[2] - img.rect_pt[0])
        ih = max(0.0, img.rect_pt[3] - img.rect_pt[1])
        cov = (iw * ih) / page_area
        if iw < 18.0 or ih < 18.0 or cov < 0.002:
            continue
        if img.media_part in repeating_logos and cov < 0.08:
            continue
        meaningful_raw_imgs.append(img)

    raw_img_area = sum(
        max(0.0, im.rect_pt[2] - im.rect_pt[0]) * max(0.0, im.rect_pt[3] - im.rect_pt[1])
        for im in meaningful_raw_imgs
    )
    img_coverage = clamp(raw_img_area / page_area, 0.0, 1.0)
    usable_area = max(1.0, usable_w * usable_h)
    img_usable_coverage = clamp(raw_img_area / usable_area, 0.0, 1.0)
    has_raw_tables_or_charts = any(
        isinstance(it, RawDocxTableItem) or (isinstance(it, RawPptxChart) and it.has_data)
        for it in page.items
    )
    is_full_page_scan = (
        len(meaningful_raw_imgs) >= 1
        and (img_coverage >= 0.45 or img_usable_coverage >= 0.65)
        and usable_chars < 40
        and not has_raw_tables_or_charts
        and smartart_count == 0
    )

    # 3. 세로 배치 스케일 계산(가상 y좌표가 페이지 높이를 넘지 않도록 정규화)
    col_w_avg = usable_w / n_cols
    total_est_h = sum(_estimate_raw_item_h(it, col_w_avg) for it in page.items)
    v_scale = min(1.0, (usable_h * n_cols * 0.92) / max(1.0, total_est_h))

    tables: list[ExtractedTable] = []
    visuals: list[VisualRegion] = []
    visual_images: dict[str, RawPptxImage] = {}
    blocks: list[DocumentBlock] = []
    headings: list[tuple[int, str]] = []

    cur_col = 0
    cur_y = m_top
    block_seq = 0

    def _alloc_rect(
        raw_w: float, raw_h: float, *, full_width: bool = False
    ) -> tuple[int, tuple[float, float, float, float]]:
        nonlocal cur_col, cur_y
        scaled_h = clamp(raw_h * v_scale, 8.0, usable_h * 0.85)
        if (
            not full_width
            and n_cols > 1
            and (cur_y + scaled_h > ph - m_bot)
            and (cur_col + 1 < n_cols)
        ):
            cur_col += 1
            cur_y = m_top
        cx0, cx1 = (m_left, pw - m_right) if full_width else col_bounds[cur_col]
        w_avail = max(30.0, cx1 - cx0)
        eff_w = min(w_avail, max(36.0, raw_w))
        y0 = clamp(cur_y, m_top, ph - m_bot - 4.0)
        y1 = clamp(y0 + scaled_h, y0 + 4.0, ph - m_bot)
        cur_y = min(ph - m_bot, y1 + 3.0 * v_scale)
        return (0 if full_width else cur_col), (
            round(cx0, 2),
            round(y0, 2),
            round(cx0 + eff_w, 2),
            round(y1, 2),
        )

    if is_full_page_scan:
        dom_img = max(
            meaningful_raw_imgs,
            key=lambda im: (im.rect_pt[2] - im.rect_pt[0]) * (im.rect_pt[3] - im.rect_pt[1]),
        )
        region_id = f"p{p_num:03d}_v01"
        clean_alt = _sanitize_alt_text(dom_img.alt_text)
        hints = list(dict.fromkeys([*([clean_alt] if clean_alt else []), *dom_img.svg_text_hints]))
        scan_bbox = make_bbox(0.0, 0.0, pw, ph, pw, ph)
        vis = VisualRegion(
            region_id=region_id,
            page=p_num,
            element_kind=ElementKind.SCANNED_PAGE,
            recommended_strategy=ParseStrategy.EMBEDDED_IMAGE_OCR,
            bbox=scan_bbox,
            reason=(
                f"DOCX 페이지 전체 스캔/래스터 이미지(점유율 {img_coverage:.0%}, "
                f"유효 텍스트 {usable_chars}자) — OCR/레이아웃 탐지 대상"
            ),
            confidence=0.94,
            priority="high",
            image_width_px=dom_img.pixel_width,
            image_height_px=dom_img.pixel_height,
            embedded_text_hints=hints,
        )
        visuals.append(vis)
        visual_images[region_id] = dom_img
        blocks.append(
            DocumentBlock(
                block_id=f"{region_id}_blk",
                page=p_num,
                reading_order=1,
                column_index=0,
                kind=ElementKind.SCANNED_PAGE,
                strategy=ParseStrategy.EMBEDDED_IMAGE_OCR,
                bbox=scan_bbox,
                text="",
                visual_region_id=region_id,
                confidence=0.94,
            )
        )
    else:
        # 캡션 후보 문단 인덱스 탐색(표·차트·그림 바로 앞/뒤의 `표 1.`, `그림 1.` 등)
        caption_for_item: dict[int, str] = {}
        consumed_caption_indices: set[int] = set()

        def _is_table_target(it: RawDocxBodyItem) -> bool:
            return isinstance(it, (RawDocxTableItem, RawPptxChart))

        def _is_figure_target(it: RawDocxBodyItem) -> bool:
            return isinstance(
                it,
                (
                    RawPptxImage,
                    RawPptxChart,
                    RawPptxOleObject,
                    RawDocxVectorDiagram,
                    RawPptxSmartArt,
                ),
            )

        def _is_any_target(it: RawDocxBodyItem) -> bool:
            return _is_table_target(it) or _is_figure_target(it)

        for idx, it in enumerate(page.items):
            if not isinstance(it, RawDocxParagraph):
                continue
            if it.heading_level is not None or len(it.text) > 140 or "\n" in it.text:
                continue
            s_txt = it.text.strip()
            if not _CAPTION_RE.match(s_txt):
                continue

            if _TABLE_CAPTION_RE.match(s_txt):
                # 표 캡션은 표 상단(idx+1) 우선, 없으면 직전 표(idx-1)
                cands = [idx + 1, idx - 1]
                pred = _is_table_target
            elif _FIGURE_CAPTION_RE.match(s_txt):
                # 그림·도식 캡션은 그림 하단(idx-1) 우선, 없으면 직후 그림(idx+1)
                cands = [idx - 1, idx + 1]
                pred = _is_figure_target
            else:
                cands = [idx + 1, idx - 1]
                pred = _is_any_target

            for t_idx in cands:
                if (
                    0 <= t_idx < len(page.items)
                    and pred(page.items[t_idx])
                    and t_idx not in caption_for_item
                ):
                    caption_for_item[t_idx] = s_txt
                    consumed_caption_indices.add(idx)
                    break

        for idx, it in enumerate(page.items):
            linked_cap = caption_for_item.get(idx)

            if isinstance(it, RawDocxParagraph):
                est_h = _estimate_raw_item_h(it, col_w_avg)
                is_full = it.heading_level == 1 and n_cols > 1
                c_idx, rect = _alloc_rect(
                    col_w_avg if not is_full else usable_w, est_h, full_width=is_full
                )
                bbox = make_bbox(rect[0], rect[1], rect[2], rect[3], pw, ph)
                p_strat = (
                    ParseStrategy.MULTI_COLUMN_LAYOUT if n_cols >= 2 else ParseStrategy.NATIVE_TEXT
                )

                if idx in consumed_caption_indices:
                    block_seq += 1
                    blocks.append(
                        DocumentBlock(
                            block_id=f"p{p_num:03d}_b{block_seq:03d}",
                            page=p_num,
                            reading_order=len(blocks) + 1,
                            column_index=c_idx,
                            kind=ElementKind.CAPTION,
                            strategy=ParseStrategy.NATIVE_TEXT,
                            bbox=bbox,
                            text=it.text,
                            confidence=0.96,
                        )
                    )
                    continue

                p_font = it.font_size_pt or ref_body_font
                inferred_h_lvl: int | None = it.heading_level
                if (
                    inferred_h_lvl is None
                    and not it.is_list_item
                    and not it.is_code_or_formula
                    and len(it.text) <= 85
                    and "\n" not in it.text
                ):
                    if p_font >= max(16.0, ref_body_font * 1.35):
                        inferred_h_lvl = 1
                    elif it.is_bold and p_font >= max(13.0, ref_body_font * 1.15):
                        inferred_h_lvl = 2

                block_seq += 1
                if inferred_h_lvl is not None:
                    norm_h = _normalize_heading_text(it.text)
                    headings.append((inferred_h_lvl, norm_h))
                    blocks.append(
                        DocumentBlock(
                            block_id=f"p{p_num:03d}_b{block_seq:03d}",
                            page=p_num,
                            reading_order=len(blocks) + 1,
                            column_index=c_idx,
                            kind=ElementKind.HEADING,
                            strategy=ParseStrategy.NATIVE_TEXT,
                            bbox=bbox,
                            text=norm_h,
                            heading_level=inferred_h_lvl,
                            font_size=it.font_size_pt,
                            is_bold=it.is_bold,
                            confidence=0.98 if it.heading_level is not None else 0.90,
                        )
                    )
                elif it.is_code_or_formula:
                    blocks.append(
                        DocumentBlock(
                            block_id=f"p{p_num:03d}_b{block_seq:03d}",
                            page=p_num,
                            reading_order=len(blocks) + 1,
                            column_index=c_idx,
                            kind=ElementKind.FORMULA_OR_CODE,
                            strategy=p_strat,
                            bbox=bbox,
                            text=it.text,
                            font_size=it.font_size_pt,
                            confidence=0.96,
                        )
                    )
                elif it.is_list_item:
                    if it.is_ordered_list and it.list_marker:
                        body_txt = f"{it.list_marker} {it.text}"
                    else:
                        body_txt = it.text
                    list_txt = f"o {body_txt}" if it.list_level >= 1 else body_txt
                    blocks.append(
                        DocumentBlock(
                            block_id=f"p{p_num:03d}_b{block_seq:03d}",
                            page=p_num,
                            reading_order=len(blocks) + 1,
                            column_index=c_idx,
                            kind=ElementKind.LIST_ITEM,
                            strategy=p_strat,
                            bbox=bbox,
                            text=list_txt,
                            font_size=it.font_size_pt,
                            is_bold=it.is_bold,
                            confidence=0.96,
                        )
                    )
                else:
                    blocks.append(
                        DocumentBlock(
                            block_id=f"p{p_num:03d}_b{block_seq:03d}",
                            page=p_num,
                            reading_order=len(blocks) + 1,
                            column_index=c_idx,
                            kind=ElementKind.PARAGRAPH,
                            strategy=p_strat,
                            bbox=bbox,
                            text=it.text,
                            font_size=it.font_size_pt,
                            is_bold=it.is_bold,
                            confidence=0.96,
                        )
                    )
                continue

            if isinstance(it, RawDocxTableItem):
                c_idx, rect = _alloc_rect(it.raw_width_pt, it.raw_height_pt)
                bbox = make_bbox(rect[0], rect[1], rect[2], rect[3], pw, ph)
                t_id = f"p{p_num:03d}_t{len(tables) + 1:02d}"
                tbl = it.table.model_copy(
                    update={
                        "table_id": t_id,
                        "page": p_num,
                        "bbox": bbox,
                        "caption": linked_cap or it.table.caption,
                    }
                )
                tables.append(tbl)
                blocks.append(
                    DocumentBlock(
                        block_id=f"{t_id}_blk",
                        page=p_num,
                        reading_order=len(blocks) + 1,
                        column_index=c_idx,
                        kind=ElementKind.TABLE,
                        strategy=tbl.strategy,
                        bbox=bbox,
                        text=tbl.caption or "",
                        table_id=t_id,
                        confidence=tbl.confidence,
                    )
                )
                continue

            if isinstance(it, RawPptxChart):
                cw = max(160.0, it.rect_pt[2] - it.rect_pt[0])
                ch = max(100.0, it.rect_pt[3] - it.rect_pt[1])
                c_idx, rect = _alloc_rect(cw, ch)
                bbox = make_bbox(rect[0], rect[1], rect[2], rect[3], pw, ph)
                if it.has_data:
                    positioned_chart = RawPptxChart(
                        shape_id=it.shape_id,
                        name=it.name,
                        chart_part=it.chart_part,
                        chart_type=it.chart_type,
                        chart_type_label=it.chart_type_label,
                        title=linked_cap or it.title,
                        headers=it.headers,
                        rows=it.rows,
                        rect_pt=rect,
                    )
                    tbl = _chart_to_extracted_table(
                        positioned_chart,
                        slide_number=p_num,
                        table_index=len(tables) + 1,
                        slide_w=pw,
                        slide_h=ph,
                    )
                    tables.append(tbl)
                    blocks.append(
                        DocumentBlock(
                            block_id=f"{tbl.table_id}_blk",
                            page=p_num,
                            reading_order=len(blocks) + 1,
                            column_index=c_idx,
                            kind=ElementKind.TABLE,
                            strategy=tbl.strategy,
                            bbox=tbl.bbox,
                            text=tbl.caption or "",
                            table_id=tbl.table_id,
                            confidence=tbl.confidence,
                        )
                    )
                else:
                    region_id = f"p{p_num:03d}_v{len(visuals) + 1:02d}"
                    hints = [h for h in (it.title, it.chart_type_label, it.name) if h]
                    vis = VisualRegion(
                        region_id=region_id,
                        page=p_num,
                        element_kind=ElementKind.VECTOR_DIAGRAM,
                        recommended_strategy=ParseStrategy.VLM_REGION,
                        bbox=bbox,
                        reason=(
                            f"캐시 수치가 비어 있는 DOCX 차트({it.chart_type_label}) "
                            "— VLM 해석 권장"
                        ),
                        confidence=0.88,
                        priority="high",
                        embedded_text_hints=hints,
                        caption=linked_cap or it.title,
                    )
                    visuals.append(vis)
                    blocks.append(
                        DocumentBlock(
                            block_id=f"{region_id}_blk",
                            page=p_num,
                            reading_order=len(blocks) + 1,
                            column_index=c_idx,
                            kind=ElementKind.VECTOR_DIAGRAM,
                            strategy=ParseStrategy.VLM_REGION,
                            bbox=bbox,
                            text=vis.caption or "",
                            visual_region_id=region_id,
                            confidence=0.88,
                        )
                    )
                continue

            if isinstance(it, RawPptxSmartArt):
                sw_pt = max(160.0, it.rect_pt[2] - it.rect_pt[0])
                sh_pt = max(80.0, it.rect_pt[3] - it.rect_pt[1])
                c_idx, rect = _alloc_rect(sw_pt, sh_pt)
                n_items = max(1, len(it.items))
                step_h = max(1.0, (rect[3] - rect[1]) / n_items)
                for s_i, (depth, stxt) in enumerate(it.items):
                    block_seq += 1
                    it_top = rect[1] + s_i * step_h
                    it_bot = min(rect[3], it_top + step_h)
                    blocks.append(
                        DocumentBlock(
                            block_id=f"p{p_num:03d}_b{block_seq:03d}",
                            page=p_num,
                            reading_order=len(blocks) + 1,
                            column_index=c_idx,
                            kind=ElementKind.LIST_ITEM,
                            strategy=ParseStrategy.NATIVE_TEXT,
                            bbox=make_bbox(rect[0], it_top, rect[2], it_bot, pw, ph),
                            text=f"o {stxt}" if depth >= 1 else stxt,
                            confidence=0.95,
                        )
                    )
                continue

            if isinstance(it, RawPptxImage):
                if it not in meaningful_raw_imgs:
                    continue
                iw = max(24.0, it.rect_pt[2] - it.rect_pt[0])
                ih = max(24.0, it.rect_pt[3] - it.rect_pt[1])
                cov = (iw * ih) / page_area
                c_idx, rect = _alloc_rect(iw, ih)
                bbox = make_bbox(rect[0], rect[1], rect[2], rect[3], pw, ph)
                region_id = f"p{p_num:03d}_v{len(visuals) + 1:02d}"
                is_eq = _is_equation_image(it.name, it.alt_text)
                is_svg = it.extension == ".svg"
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
                        if (cov >= 0.40 and it.extension in _SUPPORTED_RASTER_EXTS)
                        else ParseStrategy.VLM_REGION
                    )
                clean_alt = _sanitize_alt_text(it.alt_text)
                hints = list(
                    dict.fromkeys([*([clean_alt] if clean_alt else []), *it.svg_text_hints])
                )
                size_desc = (
                    f"{it.pixel_width}x{it.pixel_height}px, "
                    if (it.pixel_width and it.pixel_height)
                    else ""
                )
                kind_label = (
                    "수식 이미지" if is_eq else ("SVG 벡터 그래픽" if is_svg else "삽입 이미지")
                )
                vis = VisualRegion(
                    region_id=region_id,
                    page=p_num,
                    element_kind=elem_kind,
                    recommended_strategy=strategy,
                    bbox=bbox,
                    reason=(
                        f"DOCX {kind_label} `{it.name or it.media_part}` "
                        f"({size_desc}면적 {cov:.1%}) — OCR/VLM 시각 해석 대상"
                    ),
                    confidence=0.93,
                    priority="high" if (cov >= 0.20 or is_eq) else "medium",
                    image_width_px=it.pixel_width,
                    image_height_px=it.pixel_height,
                    embedded_text_hints=hints,
                    caption=linked_cap,
                )
                visuals.append(vis)
                visual_images[region_id] = it
                blocks.append(
                    DocumentBlock(
                        block_id=f"{region_id}_blk",
                        page=p_num,
                        reading_order=len(blocks) + 1,
                        column_index=c_idx,
                        kind=elem_kind,
                        strategy=strategy,
                        bbox=bbox,
                        text=vis.caption or "",
                        visual_region_id=region_id,
                        confidence=0.93,
                    )
                )
                continue

            if isinstance(it, RawPptxOleObject):
                ow = max(60.0, it.rect_pt[2] - it.rect_pt[0])
                oh = max(40.0, it.rect_pt[3] - it.rect_pt[1])
                c_idx, rect = _alloc_rect(ow, oh)
                bbox = make_bbox(rect[0], rect[1], rect[2], rect[3], pw, ph)
                region_id = f"p{p_num:03d}_v{len(visuals) + 1:02d}"
                prog_lower = it.prog_id.lower()
                ole_kind = (
                    ElementKind.TABLE
                    if ("excel" in prog_lower or "sheet" in prog_lower)
                    else ElementKind.VECTOR_DIAGRAM
                )
                prev = it.preview_image
                prev_note = f", 미리보기 {prev.extension}" if prev is not None else ""
                vis = VisualRegion(
                    region_id=region_id,
                    page=p_num,
                    element_kind=ole_kind,
                    recommended_strategy=ParseStrategy.VLM_REGION,
                    bbox=bbox,
                    reason=(
                        f"DOCX 내장 OLE 객체 `{it.name}`"
                        f"({it.prog_id or 'embedded'}{prev_note}) — VLM 시각 해석 대상"
                    ),
                    confidence=0.86,
                    priority="high",
                    image_width_px=prev.pixel_width if prev is not None else None,
                    image_height_px=prev.pixel_height if prev is not None else None,
                    embedded_text_hints=[h for h in (it.name, it.prog_id) if h],
                    caption=linked_cap,
                )
                visuals.append(vis)
                if prev is not None:
                    visual_images[region_id] = prev
                blocks.append(
                    DocumentBlock(
                        block_id=f"{region_id}_blk",
                        page=p_num,
                        reading_order=len(blocks) + 1,
                        column_index=c_idx,
                        kind=ole_kind,
                        strategy=ParseStrategy.VLM_REGION,
                        bbox=bbox,
                        text=vis.caption or "",
                        visual_region_id=region_id,
                        confidence=0.86,
                    )
                )
                continue

            if isinstance(it, RawDocxVectorDiagram):
                c_idx, rect = _alloc_rect(it.width_pt, it.height_pt)
                bbox = make_bbox(rect[0], rect[1], rect[2], rect[3], pw, ph)
                region_id = f"p{p_num:03d}_v{len(visuals) + 1:02d}"
                vis = VisualRegion(
                    region_id=region_id,
                    page=p_num,
                    element_kind=ElementKind.VECTOR_DIAGRAM,
                    recommended_strategy=ParseStrategy.VLM_REGION,
                    bbox=bbox,
                    reason=(
                        f"DOCX 벡터 그리기 도식 `{it.name}`"
                        f"(도형 {it.shape_count}개) — VLM 시각 해석 대상"
                    ),
                    confidence=0.88,
                    priority="medium",
                    embedded_text_hints=list(it.text_hints),
                    caption=linked_cap,
                )
                visuals.append(vis)
                blocks.append(
                    DocumentBlock(
                        block_id=f"{region_id}_blk",
                        page=p_num,
                        reading_order=len(blocks) + 1,
                        column_index=c_idx,
                        kind=ElementKind.VECTOR_DIAGRAM,
                        strategy=ParseStrategy.VLM_REGION,
                        bbox=bbox,
                        text=vis.caption or "",
                        visual_region_id=region_id,
                        confidence=0.88,
                    )
                )

    # 4. 각주(Footnotes)·미주(Endnotes)·주석(Comments) 하단 블록 추가
    for fid, ftxt in page.footnotes:
        blocks.append(
            DocumentBlock(
                block_id=f"p{p_num:03d}_fn{fid}",
                page=p_num,
                reading_order=len(blocks) + 1,
                column_index=0,
                kind=ElementKind.PARAGRAPH,
                strategy=ParseStrategy.NATIVE_TEXT,
                bbox=make_bbox(m_left, ph - m_bot, pw - m_right, ph - m_bot * 0.5, pw, ph),
                text=f"[각주 {fid}] {ftxt}",
                confidence=0.99,
            )
        )
    for eid, etxt in page.endnotes:
        blocks.append(
            DocumentBlock(
                block_id=f"p{p_num:03d}_en{eid}",
                page=p_num,
                reading_order=len(blocks) + 1,
                column_index=0,
                kind=ElementKind.PARAGRAPH,
                strategy=ParseStrategy.NATIVE_TEXT,
                bbox=make_bbox(m_left, ph - m_bot, pw - m_right, ph - m_bot * 0.5, pw, ph),
                text=f"[미주 {eid}] {etxt}",
                confidence=0.99,
            )
        )
    if include_comments and page.comments:
        cm_lines: list[str] = []
        for cm in page.comments:
            date_part = f" ({cm.date[:10]})" if len(cm.date) >= 10 else ""
            cm_lines.append(f"{cm.author}{date_part}: {cm.text}")
        blocks.append(
            DocumentBlock(
                block_id=f"p{p_num:03d}_comments",
                page=p_num,
                reading_order=len(blocks) + 1,
                column_index=0,
                kind=ElementKind.PARAGRAPH,
                strategy=ParseStrategy.NATIVE_TEXT,
                bbox=make_bbox(m_left, ph - m_bot * 0.5, pw - m_right, ph, pw, ph),
                text="[문서 주석]\n" + "\n".join(cm_lines),
                confidence=0.99,
            )
        )

    # 5. 머리글·바닥글 블록
    hf_blocks: list[DocumentBlock] = []
    for hdr_txt in page.headers:
        hf_idx = len(hf_blocks) + 1
        hf_blocks.append(
            DocumentBlock(
                block_id=f"p{p_num:03d}_hf{hf_idx:02d}",
                page=p_num,
                reading_order=hf_idx,
                column_index=0,
                kind=ElementKind.HEADER_FOOTER,
                strategy=ParseStrategy.SKIP_DECORATIVE,
                bbox=make_bbox(m_left, 6.0, pw - m_right, max(12.0, m_top - 4.0), pw, ph),
                text=hdr_txt,
                confidence=0.98,
            )
        )
    for ftr_txt in page.footers:
        hf_idx = len(hf_blocks) + 1
        hf_blocks.append(
            DocumentBlock(
                block_id=f"p{p_num:03d}_hf{hf_idx:02d}",
                page=p_num,
                reading_order=hf_idx,
                column_index=0,
                kind=ElementKind.HEADER_FOOTER,
                strategy=ParseStrategy.SKIP_DECORATIVE,
                bbox=make_bbox(
                    m_left, min(ph - 6.0, ph - m_bot + 4.0), pw - m_right, ph - 4.0, pw, ph
                ),
                text=ftr_txt,
                confidence=0.98,
            )
        )

    # 6. 페이지 지표(PageMetrics) 및 구조 분류(PageRouteDecision)
    table_area = sum(t.bbox.area for t in tables)
    table_area_ratio = round(clamp(table_area / page_area, 0.0, 1.0), 4)
    visual_area = sum(v.bbox.area for v in visuals)
    visual_area_ratio = round(clamp(visual_area / page_area, 0.0, 1.0), 4)
    vec_diag_count = sum(1 for v in visuals if v.element_kind == ElementKind.VECTOR_DIAGRAM)

    metrics = PageMetrics(
        page=p_num,
        width_pt=round(pw, 2),
        height_pt=round(ph, 2),
        rotation=0,
        aspect_ratio=round(pw / ph, 3),
        char_count=total_chars,
        usable_char_count=usable_chars,
        usable_text_ratio=round(usable_chars / total_chars, 4) if total_chars > 0 else 0.0,
        garbage_char_ratio=round((total_chars - usable_chars) / total_chars, 4)
        if total_chars > 0
        else 0.0,
        line_count=line_count,
        column_count=n_cols,
        column_gutters_x=gutters_x,
        median_font_size=median_font,
        max_font_size=max_font,
        image_count=len(raw_images),
        non_decorative_image_count=len(meaningful_raw_imgs),
        image_coverage=round(img_coverage, 4),
        vector_line_count=0,
        vector_rect_count=vec_diag_count,
        vector_curve_count=smartart_count,
        non_table_vector_count=vec_diag_count + smartart_count,
        table_count=len(tables),
        table_area_ratio=table_area_ratio,
        visual_region_count=len(visuals),
        visual_area_ratio=visual_area_ratio,
        header_footer_line_count=len(hf_blocks),
    )

    reasons: list[str] = []
    if (
        usable_chars < 3
        and len(tables) == 0
        and len(visuals) == 0
        and smartart_count == 0
        and not page.footnotes
        and not page.endnotes
    ):
        category = PageCategory.BLANK
        primary_strategy = ParseStrategy.SKIP_DECORATIVE
        confidence = 0.99
        reasons.append("텍스트·표·차트·그림이 없는 빈 DOCX 페이지")
    elif is_full_page_scan:
        category = PageCategory.SCANNED_IMAGE
        primary_strategy = ParseStrategy.EMBEDDED_IMAGE_OCR
        confidence = 0.94
        reasons.append(
            f"페이지 전체 이미지 점유율 {img_coverage:.0%}, 디지털 텍스트 {usable_chars}자 "
            "→ 스캔 이미지 DOCX 페이지"
        )
    elif len(tables) >= 1 and (table_area_ratio >= 0.22 or usable_chars < 220 or len(tables) >= 2):
        category = PageCategory.TABLE_HEAVY
        primary_strategy = tables[0].strategy
        confidence = 0.97
        reasons.append(
            f"표/차트 데이터 {len(tables)}개(면적 {table_area_ratio:.0%}) → OOXML 표 구조 직접 추출"
        )
    elif (vec_diag_count >= 1 or smartart_count >= 1) and (
        visual_area_ratio >= 0.16 or smartart_count >= 1 or usable_chars < 120
    ):
        category = PageCategory.VECTOR_DIAGRAM_HEAVY
        primary_strategy = (
            ParseStrategy.VLM_REGION if vec_diag_count >= 1 else ParseStrategy.NATIVE_TEXT
        )
        confidence = 0.90
        reasons.append(
            f"벡터 도식 {vec_diag_count}개·SmartArt {smartart_count}개 → 도식 중심 DOCX 페이지"
        )
    elif n_cols >= 2 and usable_chars >= 40:
        category = PageCategory.MULTI_COLUMN
        primary_strategy = ParseStrategy.MULTI_COLUMN_LAYOUT
        confidence = 0.93
        gutter_desc = ", ".join(f"x={g:.0f}pt" for g in gutters_x)
        reasons.append(f"Word 섹션 {n_cols}단 레이아웃({gutter_desc}) → 다단 읽기 순서 추출")
    elif usable_chars >= 15 and len(visuals) >= 1:
        category = PageCategory.HYBRID_TEXT_IMAGE
        primary_strategy = ParseStrategy.NATIVE_TEXT
        confidence = 0.93
        reasons.append(
            f"디지털 본문({usable_chars}자)과 시각 영역 {len(visuals)}개 혼재 → "
            "본문 직접 추출 + 시각 영역 OCR/VLM 분기"
        )
    elif metrics.aspect_ratio >= 1.18 and usable_chars < 350:
        category = PageCategory.SLIDE_LIKE
        primary_strategy = ParseStrategy.NATIVE_TEXT
        confidence = 0.93
        reasons.append(
            f"가로형 섹션(종횡비 {metrics.aspect_ratio:.2f}, {usable_chars}자) → "
            "네이티브 XML 직접 추출"
        )
    else:
        category = PageCategory.DIGITAL_TEXT
        primary_strategy = ParseStrategy.NATIVE_TEXT
        confidence = 0.97
        reasons.append(
            f"디지털 Word 본문({usable_chars}자, {line_count}줄) → WordprocessingML 직접 추출"
        )

    if len(tables) >= 1 and category != PageCategory.TABLE_HEAVY:
        reasons.append(f"보조 표/차트 {len(tables)}개 직접 추출")
    if page.footnotes or page.endnotes:
        reasons.append(f"각주 {len(page.footnotes)}건·미주 {len(page.endnotes)}건 포함")
    if include_comments and page.comments:
        reasons.append(f"문서 주석 {len(page.comments)}건 포함")
    if hf_blocks:
        reasons.append(f"머리글/바닥글 {len(hf_blocks)}개 분리")

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

    all_strats = [r.strategy for r in regions]
    secondary_strategies = list(dict.fromkeys(s for s in all_strats if s != primary_strategy))

    route = PageRouteDecision(
        page=p_num,
        category=category,
        primary_strategy=primary_strategy,
        secondary_strategies=secondary_strategies,
        confidence=confidence,
        reasons=reasons,
        metrics=metrics,
        regions=regions,
        requires_vision=len(visuals) > 0,
        warnings=list(page.warnings),
    )

    return AnalyzedDocxPage(
        raw=page,
        tables=tables,
        visuals=visuals,
        visual_images=visual_images,
        blocks=blocks,
        header_footer_blocks=hf_blocks,
        route=route,
        headings=headings,
        is_full_page_scan=is_full_page_scan,
        has_running_header=len(page.headers) > 0,
        has_running_footer=len(page.footers) > 0,
    )


def build_docx_outline(analyzed_pages: list[AnalyzedDocxPage]) -> list[OutlineEntry]:
    """문서 내 제목(`ElementKind.HEADING`) 계층으로부터 문서 목차(Outline)를 조립한다."""
    outline: list[OutlineEntry] = []
    for item in analyzed_pages:
        for lvl, title in item.headings:
            if title:
                outline.append(
                    OutlineEntry(
                        level=max(1, min(6, lvl)),
                        title=title,
                        page=item.raw.page_number,
                    )
                )
    return outline


def build_docx_routing_plan(
    metadata: DocumentMetadata,
    analyzed_pages: list[AnalyzedDocxPage],
    *,
    body_font_size: float | None,
) -> RoutingPlan:
    """페이지 분석 결과 목록으로 통합 `RoutingPlan`을 조립한다."""
    outline = build_docx_outline(analyzed_pages)
    inferred_title = metadata.title
    if not inferred_title and outline:
        first_l1 = next((e.title for e in outline if e.level == 1), None)
        inferred_title = first_l1 or outline[0].title

    updated_meta = metadata.model_copy(
        update={
            "title": inferred_title,
            "has_outline": len(outline) > 0,
        }
    )

    cat_counts: Counter[str] = Counter()
    strat_counts: Counter[str] = Counter()
    vision_pages: list[int] = []
    multi_col_pages: list[int] = []
    total_tables = 0
    total_visuals = 0
    has_running_hdr = any(item.has_running_header for item in analyzed_pages)
    has_running_ftr = any(item.has_running_footer for item in analyzed_pages)

    decisions = [item.route for item in analyzed_pages]
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


__all__ = [
    "AnalyzedDocxPage",
    "analyze_docx_page",
    "build_docx_outline",
    "build_docx_routing_plan",
    "estimate_docx_body_font_size",
    "find_repeating_docx_logos",
]
