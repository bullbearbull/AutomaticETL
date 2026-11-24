"""PDF 문서 형식 어댑터 — 추출·표·시각·레이아웃·라우팅·크롭·비전 연동을 통합한다."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from autoetl.parser.adapters import (
    LayoutDetectionInput,
    NoopLayoutDetector,
    NoopVisionAdapter,
    PageLayoutDetector,
    PageRenderer,
    ParseOptions,
    VisionParserAdapter,
    VisionRequest,
    resolve_default_page_renderer,
)
from autoetl.parser.contracts import (
    BBox,
    CropStatus,
    DocumentBlock,
    ElementKind,
    ExtractedTable,
    PageRouteDecision,
    ParsedDocument,
    ParsedPage,
    ParseStrategy,
    RegionRoute,
    RoutingPlan,
    SourceFormat,
    VisionStatus,
    VisualRegion,
)
from autoetl.parser.image_io import (
    DecodedImage,
    EncodedImage,
    crop_decoded_image,
    decode_pdf_image_stream,
    decode_png,
    save_encoded_image,
)
from autoetl.parser.layout_detector import boxes_to_visual_regions
from autoetl.parser.pdf.extractor import ExtractedPdfDocument, extract_pdf_document
from autoetl.parser.pdf.geometry import (
    RawImagePlacement,
    RawPageData,
    bbox_iou,
    clamp,
    union_area,
)
from autoetl.parser.pdf.layout import (
    LayoutTextBlock,
    PageLayoutResult,
    analyze_page_layout,
    detect_running_headers_footers,
    estimate_document_body_font_size,
    link_captions_to_elements,
)
from autoetl.parser.pdf.router import build_page_route_decision, build_routing_plan
from autoetl.parser.pdf.tables import (
    TableDetectionResult,
    extract_borderless_tables,
    extract_ruled_tables,
)
from autoetl.parser.pdf.visuals import (
    TextQualityStats,
    VisualDetectionResult,
    analyze_text_quality,
    detect_page_visuals,
    find_repeating_logo_signatures,
)


@dataclass(frozen=True)
class _AnalyzedPage:
    raw: RawPageData
    text_quality: TextQualityStats
    tables_res: TableDetectionResult
    visuals_res: VisualDetectionResult
    layout_res: PageLayoutResult
    tables: list[ExtractedTable]
    visuals: list[VisualRegion]
    route: PageRouteDecision
    layout_page_image: EncodedImage | None = None
    layout_crop_rects: dict[str, tuple[int, int, int, int]] = field(default_factory=dict)
    layout_segmented: bool = False


def _assemble_reading_order_blocks(
    page_num: int,
    text_blocks: list[LayoutTextBlock],
    tables: list[ExtractedTable],
    visuals: list[VisualRegion],
    gutters_x: list[float],
    *,
    preserve_visual_order: bool = False,
) -> list[DocumentBlock]:
    """다단 텍스트 읽기 순서를 보존하면서 표·시각 영역을 자연스러운 위치에 끼워 넣는다."""

    @dataclass
    class _Item:
        kind: ElementKind
        column_index: int
        is_full_width: bool
        top: float
        bottom: float
        x0: float
        block: DocumentBlock

    def col_for_bbox(bbox: BBox) -> tuple[int, bool]:
        if not gutters_x:
            return 0, False
        # 어떤 다단 여백(gutter)이라도 가로지르면 전폭 요소
        for g in gutters_x:
            if bbox.x0 < g - 4.0 and bbox.x1 > g + 4.0:
                return 0, True
        cx = (bbox.x0 + bbox.x1) / 2.0
        col = 0
        for g in gutters_x:
            if cx > g:
                col += 1
        return col, False

    items: list[_Item] = []

    # 텍스트 블록은 이미 밴드→컬럼→위아래 순서로 정렬되어 있음
    for idx, tb in enumerate(text_blocks, 1):
        _, full_w = col_for_bbox(tb.bbox)
        items.append(
            _Item(
                kind=tb.kind,
                column_index=tb.column_index,
                is_full_width=full_w,
                top=tb.bbox.top,
                bottom=tb.bbox.bottom,
                x0=tb.bbox.x0,
                block=DocumentBlock(
                    block_id=f"p{page_num:03d}_b{idx:03d}",
                    page=page_num,
                    reading_order=0,
                    column_index=tb.column_index,
                    kind=tb.kind,
                    strategy=tb.strategy,
                    bbox=tb.bbox,
                    text=tb.text,
                    heading_level=tb.heading_level,
                    font_size=tb.font_size,
                    is_bold=tb.is_bold,
                    confidence=tb.confidence,
                ),
            )
        )

    for tbl in tables:
        col, full_w = col_for_bbox(tbl.bbox)
        items.append(
            _Item(
                kind=ElementKind.TABLE,
                column_index=col,
                is_full_width=full_w,
                top=tbl.bbox.top,
                bottom=tbl.bbox.bottom,
                x0=tbl.bbox.x0,
                block=DocumentBlock(
                    block_id=f"{tbl.table_id}_blk",
                    page=page_num,
                    reading_order=0,
                    column_index=col,
                    kind=ElementKind.TABLE,
                    strategy=tbl.strategy,
                    bbox=tbl.bbox,
                    text=tbl.caption or "",
                    table_id=tbl.table_id,
                    confidence=tbl.confidence,
                ),
            )
        )

    for vis in visuals:
        col, full_w = col_for_bbox(vis.bbox)
        items.append(
            _Item(
                kind=vis.element_kind,
                column_index=col,
                is_full_width=full_w,
                top=vis.bbox.top,
                bottom=vis.bbox.bottom,
                x0=vis.bbox.x0,
                block=DocumentBlock(
                    block_id=f"{vis.region_id}_blk",
                    page=page_num,
                    reading_order=0,
                    column_index=col,
                    kind=vis.element_kind,
                    strategy=vis.recommended_strategy,
                    bbox=vis.bbox,
                    text=vis.vision_markdown or vis.vision_text or vis.caption or "",
                    visual_region_id=vis.region_id,
                    confidence=vis.confidence,
                ),
            )
        )

    if not items:
        return []

    if not gutters_x:
        if not (preserve_visual_order and not text_blocks and not tables):
            items.sort(key=lambda it: (it.top, it.x0))
        return [
            it.block.model_copy(update={"reading_order": order})
            for order, it in enumerate(items, 1)
        ]

    # 다단 페이지: 전폭 항목의 y 구간을 기준으로 수평 밴드를 나누고,
    # 각 비전폭 밴드 안에서는 (column_index, top, x0) 순으로 정렬한다.
    full_items = sorted([it for it in items if it.is_full_width], key=lambda it: (it.top, it.x0))
    col_items = [it for it in items if not it.is_full_width]

    ordered_items: list[_Item] = []
    remaining_cols = list(col_items)

    for full_it in full_items:
        above = [it for it in remaining_cols if (it.top + it.bottom) / 2.0 < full_it.top]
        remaining_cols = [it for it in remaining_cols if it not in above]
        above.sort(key=lambda it: (it.column_index, it.top, it.x0))
        ordered_items.extend(above)
        ordered_items.append(full_it)

    remaining_cols.sort(key=lambda it: (it.column_index, it.top, it.x0))
    ordered_items.extend(remaining_cols)

    return [
        it.block.model_copy(update={"reading_order": order})
        for order, it in enumerate(ordered_items, 1)
    ]


def _extract_crop_for_visual(
    source_path: Path,
    region: VisualRegion,
    embedded_image: RawImagePlacement | None,
    *,
    options: ParseOptions,
    renderer: PageRenderer,
    layout_decoded_page: DecodedImage | None = None,
    layout_crop_rect: tuple[int, int, int, int] | None = None,
) -> tuple[VisualRegion, EncodedImage | None]:
    """시각 영역의 크롭 이미지를 추출(레이아웃 부분 크롭 → 내장 스트림 → 페이지 렌더러)한다."""
    if not options.extract_crops:
        return region.model_copy(update={"crop_status": CropStatus.SKIPPED}), None

    encoded: EncodedImage | None = None
    via_embedded = False

    if layout_crop_rect is not None and layout_decoded_page is not None:
        encoded = crop_decoded_image(layout_decoded_page, *layout_crop_rect)

    if (
        encoded is None
        and layout_crop_rect is None
        and embedded_image is not None
        and (embedded_image.raw_data is not None or embedded_image.decoded_data is not None)
    ):
        encoded = decode_pdf_image_stream(
            embedded_image.raw_data or b"",
            filters=list(embedded_image.filters),
            color_space=embedded_image.color_space,
            width=embedded_image.pixel_width,
            height=embedded_image.pixel_height,
            bits_per_component=embedded_image.bits_per_component,
            decoded_data=embedded_image.decoded_data,
        )
        if encoded is not None:
            via_embedded = True

    if encoded is None:
        if renderer.available:
            crop_bbox = None if region.element_kind == ElementKind.SCANNED_PAGE else region.bbox
            encoded = renderer.render_region(
                source_path,
                region.page,
                crop_bbox,
                dpi=options.render_dpi,
            )
            if encoded is None:
                return region.model_copy(update={"crop_status": CropStatus.FAILED}), None
        else:
            return region.model_copy(update={"crop_status": CropStatus.RENDERER_UNAVAILABLE}), None

    crop_path_str: str | None = None
    status = CropStatus.EMBEDDED_EXTRACTED if via_embedded else CropStatus.SAVED
    if options.crop_dir is not None:
        saved = save_encoded_image(encoded, options.crop_dir, region.region_id)
        crop_path_str = saved.as_posix()
        status = CropStatus.SAVED

    updated = region.model_copy(
        update={
            "crop_status": status,
            "crop_path": crop_path_str,
            "image_width_px": encoded.width or region.image_width_px,
            "image_height_px": encoded.height or region.image_height_px,
        }
    )
    return updated, encoded


def _find_surrounding_context(
    region: VisualRegion,
    text_blocks: list[LayoutTextBlock],
) -> tuple[str | None, str | None]:
    """시각 영역 위·아래에 가장 가까운 본문 텍스트 블록을 찾아 VLM 문맥으로 제공한다."""
    if not text_blocks:
        return None, None

    before_candidates = [
        b
        for b in text_blocks
        if b.bbox.bottom <= region.bbox.top + 8.0
        and b.text.strip() != (region.caption or "").strip()
    ]
    after_candidates = [
        b
        for b in text_blocks
        if b.bbox.top >= region.bbox.bottom - 8.0
        and b.text.strip() != (region.caption or "").strip()
    ]
    ctx_before: str | None = None
    if before_candidates:
        nearest_before = max(before_candidates, key=lambda b: b.bbox.bottom)
        ctx_before = nearest_before.text.strip()[:240] or None

    ctx_after: str | None = None
    if after_candidates:
        nearest_after = min(after_candidates, key=lambda b: b.bbox.top)
        ctx_after = nearest_after.text.strip()[:240] or None

    return ctx_before, ctx_after


def _run_vision_adapter_on_region(
    region: VisualRegion,
    encoded_image: EncodedImage | None,
    vision_adapter: VisionParserAdapter | None,
    *,
    context_before: str | None = None,
    context_after: str | None = None,
) -> VisualRegion:
    """실제 OCR/VLM 어댑터가 연결된 경우에만 시각 영역 해석을 실행한다."""
    if vision_adapter is None or isinstance(vision_adapter, NoopVisionAdapter):
        return region.model_copy(update={"vision_status": VisionStatus.PENDING})

    req = VisionRequest(
        region_id=region.region_id,
        page=region.page,
        element_kind=region.element_kind,
        recommended_strategy=region.recommended_strategy,
        bbox=region.bbox,
        reason=region.reason,
        image_bytes=encoded_image.data if encoded_image is not None else None,
        image_extension=encoded_image.extension if encoded_image is not None else None,
        crop_path=region.crop_path,
        embedded_text_hints=tuple(region.embedded_text_hints),
        caption=region.caption,
        context_before=context_before,
        context_after=context_after,
    )
    try:
        res = vision_adapter.parse_region(req)
    except Exception:
        return region.model_copy(
            update={
                "vision_status": VisionStatus.FAILED,
                "vision_adapter": vision_adapter.name,
                "vision_metadata": {"vision_error": "adapter_exception"},
            }
        )

    if not res.succeeded:
        meta = dict(res.metadata)
        if res.error:
            meta["vision_error"] = res.error
        return region.model_copy(
            update={
                "vision_status": VisionStatus.FAILED,
                "vision_adapter": res.adapter_name,
                "vision_metadata": meta,
            }
        )

    return region.model_copy(
        update={
            "vision_status": VisionStatus.COMPLETED,
            "vision_adapter": res.adapter_name,
            "vision_text": res.text,
            "vision_markdown": res.markdown or res.text,
            "vision_metadata": dict(res.metadata),
        }
    )


def _link_cross_page_tables(analyzed: list[_AnalyzedPage]) -> list[_AnalyzedPage]:
    """연속된 페이지에 걸쳐 이어지는 표를 탐지해 헤더를 상속하고 첫 데이터 행 오인을 막는다."""
    if len(analyzed) < 2:
        return analyzed

    out = list(analyzed)
    for idx in range(1, len(out)):
        prev_page = out[idx - 1]
        cur_page = out[idx]
        if cur_page.raw.page != prev_page.raw.page + 1:
            continue
        if not prev_page.tables or not cur_page.tables:
            continue

        prev_tbl = prev_page.tables[-1]
        cur_tbl = cur_page.tables[0]

        # 이전 페이지 하단(>=70%)에서 끝나고 현재 페이지 상단(<=20%)에서 시작하며
        # 열 수·가로 폭이 일치하는 경우만 이어진 표로 판단
        if prev_tbl.bbox.norm_bottom < 0.70 or cur_tbl.bbox.norm_top > 0.20:
            continue
        if cur_tbl.col_count != prev_tbl.col_count or cur_tbl.col_count < 2:
            continue
        if (
            abs(cur_tbl.bbox.x0 - prev_tbl.bbox.x0) > 18.0
            or abs(cur_tbl.bbox.x1 - prev_tbl.bbox.x1) > 18.0
        ):
            continue

        # 현재 표의 첫 행이 이미 이전 표의 헤더를 반복한 경우라면 그대로 두고 이어짐 태그만 부착
        prev_norm_headers = [h.strip() for h in prev_tbl.headers]
        cur_first_row = (
            [h.strip() for h in cur_tbl.headers]
            if cur_tbl.header_row_count >= 1
            else ([c.strip() for c in cur_tbl.rows[0]] if cur_tbl.rows else [])
        )
        repeats_header = bool(prev_norm_headers) and cur_first_row == prev_norm_headers

        if repeats_header:
            updated_tbl = cur_tbl.model_copy(
                update={
                    "continued_from_table_id": prev_tbl.table_id,
                    "reason": f"{cur_tbl.reason} (이전 페이지 표 {prev_tbl.table_id}에서 이어짐)",
                }
            )
        else:
            # 2페이지 첫 행이 헤더가 아닌 데이터 행(예: '5회차')이므로 데이터 행으로 복원
            restored_rows = (
                [cur_tbl.headers, *cur_tbl.rows] if cur_tbl.header_row_count >= 1 else cur_tbl.rows
            )
            restored_cells = [
                cell.model_copy(update={"is_header": False}) for cell in cur_tbl.cells
            ]
            updated_tbl = cur_tbl.model_copy(
                update={
                    "header_row_count": 0,
                    "headers": list(prev_tbl.headers),
                    "rows": restored_rows,
                    "cells": restored_cells,
                    "continued_from_table_id": prev_tbl.table_id,
                    "reason": (
                        f"{cur_tbl.reason} "
                        f"(이전 페이지 표 {prev_tbl.table_id}에서 이어짐 · 헤더 상속)"
                    ),
                }
            )

        new_tables = [updated_tbl, *cur_page.tables[1:]]
        updated_regions = [
            reg.model_copy(update={"reason": updated_tbl.reason})
            if reg.region_id == updated_tbl.table_id
            else reg
            for reg in cur_page.route.regions
        ]
        updated_route = cur_page.route.model_copy(update={"regions": updated_regions})
        out[idx] = _AnalyzedPage(
            raw=cur_page.raw,
            text_quality=cur_page.text_quality,
            tables_res=cur_page.tables_res,
            visuals_res=cur_page.visuals_res,
            layout_res=cur_page.layout_res,
            tables=new_tables,
            visuals=cur_page.visuals,
            route=updated_route,
            layout_page_image=cur_page.layout_page_image,
            layout_crop_rects=cur_page.layout_crop_rects,
            layout_segmented=cur_page.layout_segmented,
        )
    return out


def _resolve_full_page_image_for_layout(
    source_path: Path,
    item: _AnalyzedPage,
    *,
    options: ParseOptions,
    renderer: PageRenderer,
) -> EncodedImage | None:
    """레이아웃 객체 탐지용 전체 페이지 래스터 이미지를 확보한다(PNG 우선)."""
    embedded_encoded: EncodedImage | None = None
    if item.visuals_res.is_full_page_scan and item.visuals_res.region_images:
        first_emb = next(iter(item.visuals_res.region_images.values()))
        if first_emb.raw_data is not None or first_emb.decoded_data is not None:
            embedded_encoded = decode_pdf_image_stream(
                first_emb.raw_data or b"",
                filters=list(first_emb.filters),
                color_space=first_emb.color_space,
                width=first_emb.pixel_width,
                height=first_emb.pixel_height,
                bits_per_component=first_emb.bits_per_component,
                decoded_data=first_emb.decoded_data,
            )

    if embedded_encoded is not None and embedded_encoded.extension == ".png":
        return embedded_encoded

    if renderer.available:
        rendered = renderer.render_region(
            source_path,
            item.raw.page,
            None,
            dpi=options.render_dpi,
        )
        if rendered is not None:
            return rendered

    return embedded_encoded


def _apply_layout_detection_to_page(
    source_path: Path,
    item: _AnalyzedPage,
    *,
    options: ParseOptions,
    layout_detector: PageLayoutDetector,
    renderer: PageRenderer,
) -> _AnalyzedPage:
    """스캔 페이지(또는 전체 페이지 옵션 시)에 레이아웃 탐지를 적용해 세부 영역으로 분기한다."""
    if not item.visuals_res.is_full_page_scan and not options.layout_detect_all_pages:
        return item

    full_img = _resolve_full_page_image_for_layout(
        source_path,
        item,
        options=options,
        renderer=renderer,
    )
    if full_img is None:
        return item

    img_w = full_img.width or 0
    img_h = full_img.height or 0
    if (img_w <= 0 or img_h <= 0) and full_img.extension == ".png":
        decoded_probe = decode_png(full_img.data)
        if decoded_probe is not None:
            img_w, img_h = decoded_probe.width, decoded_probe.height
    if img_w <= 0 or img_h <= 0:
        return item

    det_input = LayoutDetectionInput(
        page=item.raw.page,
        image_bytes=full_img.data,
        image_extension=full_img.extension,
        image_width_px=img_w,
        image_height_px=img_h,
        page_width_pt=item.raw.width,
        page_height_pt=item.raw.height,
    )
    boxes = layout_detector.detect(det_input)
    if not boxes:
        return item

    page_area = max(1.0, item.raw.width * item.raw.height)

    if item.visuals_res.is_full_page_scan:
        region_pairs = boxes_to_visual_regions(
            boxes,
            page=item.raw.page,
            page_width_pt=item.raw.width,
            page_height_pt=item.raw.height,
            image_width_px=img_w,
            image_height_px=img_h,
            detector_name=layout_detector.name,
            skip_header_footer=True,
            start_index=1,
        )
        if not region_pairs:
            return item

        new_visuals = [reg for reg, _ in region_pairs]
        crop_rects = {reg.region_id: px_rect for reg, px_rect in region_pairs}

        # 페이지 라우팅 결정 및 지표 갱신
        kind_counts = Counter(v.element_kind.value for v in new_visuals)
        summary_parts = ", ".join(f"{k} {cnt}" for k, cnt in kind_counts.items())
        vis_boxes = [(v.bbox.x0, v.bbox.top, v.bbox.x1, v.bbox.bottom) for v in new_visuals]
        vis_area_ratio = round(clamp(union_area(vis_boxes) / page_area, 0.0, 1.0), 4)

        has_vlm_region = any(
            v.recommended_strategy == ParseStrategy.VLM_REGION for v in new_visuals
        )
        new_primary = (
            ParseStrategy.VLM_REGION if has_vlm_region else ParseStrategy.EMBEDDED_IMAGE_OCR
        )
        non_visual_routes = [r for r in item.route.regions if not r.requires_vision]
        visual_routes = [
            RegionRoute(
                region_id=v.region_id,
                element_kind=v.element_kind,
                strategy=v.recommended_strategy,
                bbox=v.bbox,
                confidence=v.confidence,
                reason=v.reason,
                requires_vision=True,
            )
            for v in new_visuals
        ]
        all_routes = [*non_visual_routes, *visual_routes]
        all_strats = [r.strategy for r in all_routes]
        new_secondary = list(dict.fromkeys(s for s in all_strats if s != new_primary))
        new_metrics = item.route.metrics.model_copy(
            update={
                "visual_region_count": len(new_visuals),
                "visual_area_ratio": vis_area_ratio,
            }
        )
        new_reasons = [
            *item.route.reasons,
            (
                f"레이아웃 탐지({layout_detector.name})로 스캔 페이지를 "
                f"{len(new_visuals)}개 영역으로 세분화({summary_parts})"
            ),
        ]
        new_route = item.route.model_copy(
            update={
                "primary_strategy": new_primary,
                "secondary_strategies": new_secondary,
                "reasons": new_reasons,
                "metrics": new_metrics,
                "regions": all_routes,
                "requires_vision": True,
            }
        )
        return _AnalyzedPage(
            raw=item.raw,
            text_quality=item.text_quality,
            tables_res=item.tables_res,
            visuals_res=item.visuals_res,
            layout_res=item.layout_res,
            tables=item.tables,
            visuals=new_visuals,
            route=new_route,
            layout_page_image=full_img,
            layout_crop_rects=crop_rects,
            layout_segmented=True,
        )

    # 비스캔 페이지(layout_detect_all_pages=True): 기존 결정론 표·시각 영역과 겹치지 않는
    # 표·그림·차트·수식 영역만 보조 시각 영역으로 보강한다.
    augment_kinds = {
        ElementKind.TABLE,
        ElementKind.FIGURE_IMAGE,
        ElementKind.VECTOR_DIAGRAM,
        ElementKind.FORMULA_OR_CODE,
    }
    candidate_pairs = boxes_to_visual_regions(
        [b for b in boxes if b.element_kind in augment_kinds],
        page=item.raw.page,
        page_width_pt=item.raw.width,
        page_height_pt=item.raw.height,
        image_width_px=img_w,
        image_height_px=img_h,
        detector_name=layout_detector.name,
        skip_header_footer=True,
        start_index=len(item.visuals) + 1,
    )
    existing_boxes = [(t.bbox.x0, t.bbox.top, t.bbox.x1, t.bbox.bottom) for t in item.tables] + [
        (v.bbox.x0, v.bbox.top, v.bbox.x1, v.bbox.bottom) for v in item.visuals
    ]

    added_visuals: list[VisualRegion] = []
    added_rects: dict[str, tuple[int, int, int, int]] = {}
    next_idx = len(item.visuals) + 1
    for reg, px_rect in candidate_pairs:
        rbox = (reg.bbox.x0, reg.bbox.top, reg.bbox.x1, reg.bbox.bottom)
        if any(bbox_iou(rbox, eb) >= 0.35 for eb in existing_boxes):
            continue
        reindexed = reg.model_copy(update={"region_id": f"p{item.raw.page:03d}_v{next_idx:02d}"})
        next_idx += 1
        added_visuals.append(reindexed)
        added_rects[reindexed.region_id] = px_rect
        existing_boxes.append(rbox)

    if not added_visuals:
        return item

    combined_visuals = [*item.visuals, *added_visuals]
    vis_boxes = [(v.bbox.x0, v.bbox.top, v.bbox.x1, v.bbox.bottom) for v in combined_visuals]
    vis_area_ratio = round(clamp(union_area(vis_boxes) / page_area, 0.0, 1.0), 4)
    added_routes = [
        RegionRoute(
            region_id=v.region_id,
            element_kind=v.element_kind,
            strategy=v.recommended_strategy,
            bbox=v.bbox,
            confidence=v.confidence,
            reason=v.reason,
            requires_vision=True,
        )
        for v in added_visuals
    ]
    combined_routes = sorted(
        [*item.route.regions, *added_routes],
        key=lambda r: (r.bbox.top, r.bbox.x0),
    )
    all_strats = [r.strategy for r in combined_routes]
    new_secondary = list(dict.fromkeys(s for s in all_strats if s != item.route.primary_strategy))
    new_metrics = item.route.metrics.model_copy(
        update={
            "visual_region_count": len(combined_visuals),
            "visual_area_ratio": vis_area_ratio,
        }
    )
    new_route = item.route.model_copy(
        update={
            "secondary_strategies": new_secondary,
            "reasons": [
                *item.route.reasons,
                (
                    f"레이아웃 탐지({layout_detector.name})로 "
                    f"추가 비정형 영역 {len(added_visuals)}개 보강"
                ),
            ],
            "metrics": new_metrics,
            "regions": combined_routes,
            "requires_vision": True,
        }
    )
    return _AnalyzedPage(
        raw=item.raw,
        text_quality=item.text_quality,
        tables_res=item.tables_res,
        visuals_res=item.visuals_res,
        layout_res=item.layout_res,
        tables=item.tables,
        visuals=combined_visuals,
        route=new_route,
        layout_page_image=full_img,
        layout_crop_rects=added_rects,
        layout_segmented=False,
    )


class PdfDocumentAdapter:
    """PDF 심층 결정론 파서·라우터 어댑터."""

    @property
    def format(self) -> SourceFormat:
        return SourceFormat.PDF

    @property
    def extensions(self) -> tuple[str, ...]:
        return (".pdf",)

    def _analyze_pages(
        self,
        source_path: Path,
        extracted: ExtractedPdfDocument,
        options: ParseOptions,
        *,
        layout_detector: PageLayoutDetector | None = None,
        renderer: PageRenderer | None = None,
    ) -> tuple[list[_AnalyzedPage], RoutingPlan]:
        body_font_size = estimate_document_body_font_size(extracted.pages)
        repeating_logos = find_repeating_logo_signatures(extracted.pages)

        if options.detect_running_headers_footers:
            hf_map, has_header, has_footer = detect_running_headers_footers(extracted.pages)
        else:
            hf_map = {p.page: frozenset() for p in extracted.pages}
            has_header, has_footer = False, False

        active_detector = (
            layout_detector
            if (layout_detector is not None and not isinstance(layout_detector, NoopLayoutDetector))
            else None
        )
        eff_renderer = (
            renderer
            if renderer is not None
            else (resolve_default_page_renderer() if active_detector is not None else None)
        )

        analyzed: list[_AnalyzedPage] = []

        for raw_page in extracted.pages:
            hf_spans = hf_map.get(raw_page.page, frozenset())
            text_q = analyze_text_quality(raw_page)

            ruled_res = extract_ruled_tables(
                raw_page,
                excluded_span_indices=set(hf_spans),
            )
            visuals_res = detect_page_visuals(
                raw_page,
                text_quality=text_q,
                table_boxes=ruled_res.consumed_table_boxes,
                excluded_span_indices=hf_spans | ruled_res.consumed_span_indices,
                repeating_logo_keys=repeating_logos,
            )
            if options.detect_borderless_tables and not visuals_res.is_full_page_scan:
                visual_boxes = tuple(
                    (v.bbox.x0, v.bbox.top, v.bbox.x1, v.bbox.bottom)
                    for v in visuals_res.visual_regions
                )
                borderless_res = extract_borderless_tables(
                    raw_page,
                    excluded_span_indices=(
                        set(hf_spans)
                        | set(ruled_res.consumed_span_indices)
                        | set(visuals_res.consumed_span_indices)
                    ),
                    existing_boxes=ruled_res.consumed_table_boxes + visual_boxes,
                    next_index=len(ruled_res.tables) + 1,
                )
                combined_tables = sorted(
                    [*ruled_res.tables, *borderless_res.tables],
                    key=lambda t: (t.bbox.top, t.bbox.x0),
                )
                tables_res = TableDetectionResult(
                    tables=combined_tables,
                    consumed_span_indices=(
                        ruled_res.consumed_span_indices | borderless_res.consumed_span_indices
                    ),
                    consumed_table_boxes=(
                        ruled_res.consumed_table_boxes + borderless_res.consumed_table_boxes
                    ),
                )
            else:
                tables_res = ruled_res

            layout_res = analyze_page_layout(
                raw_page,
                excluded_span_indices=(
                    tables_res.consumed_span_indices | visuals_res.consumed_span_indices
                ),
                header_footer_span_indices=hf_spans,
                body_font_size=body_font_size,
                outline=extracted.outline,
            )

            tables, visuals = link_captions_to_elements(
                layout_res.text_blocks, tables_res.tables, visuals_res.visual_regions
            )
            route = build_page_route_decision(
                raw_page,
                text_quality=text_q,
                tables_res=tables_res,
                visuals_res=visuals_res,
                layout_res=layout_res,
                tables=tables,
                visuals=visuals,
            )
            page_item = _AnalyzedPage(
                raw=raw_page,
                text_quality=text_q,
                tables_res=tables_res,
                visuals_res=visuals_res,
                layout_res=layout_res,
                tables=tables,
                visuals=visuals,
                route=route,
            )
            if active_detector is not None and eff_renderer is not None:
                page_item = _apply_layout_detection_to_page(
                    source_path,
                    page_item,
                    options=options,
                    layout_detector=active_detector,
                    renderer=eff_renderer,
                )
            analyzed.append(page_item)

        analyzed = _link_cross_page_tables(analyzed)
        decisions = [item.route for item in analyzed]

        plan = build_routing_plan(
            extracted.metadata,
            extracted.outline,
            decisions,
            body_font_size=body_font_size,
            running_header_detected=has_header,
            running_footer_detected=has_footer,
        )
        return analyzed, plan

    def inspect(
        self,
        source_path: Path,
        *,
        options: ParseOptions,
        layout_detector: PageLayoutDetector | None = None,
        page_renderer: PageRenderer | None = None,
    ) -> RoutingPlan:
        """문서의 구조와 페이지·요소별 파싱 전략 계획만 빠르게 생성한다."""
        extracted = extract_pdf_document(source_path, selected_pages=options.pages)
        _, plan = self._analyze_pages(
            source_path,
            extracted,
            options,
            layout_detector=layout_detector,
            renderer=page_renderer,
        )
        return plan

    def parse(
        self,
        source_path: Path,
        *,
        options: ParseOptions,
        vision_adapter: VisionParserAdapter | None = None,
        page_renderer: PageRenderer | None = None,
        layout_detector: PageLayoutDetector | None = None,
    ) -> ParsedDocument:
        """PDF를 구조 진단·레이아웃 탐지·라우팅·추출·크롭·비전 연동까지 수행한다."""
        extracted = extract_pdf_document(source_path, selected_pages=options.pages)
        renderer = page_renderer or resolve_default_page_renderer()
        analyzed_pages, routing_plan = self._analyze_pages(
            source_path,
            extracted,
            options,
            layout_detector=layout_detector,
            renderer=renderer,
        )

        parsed_pages: list[ParsedPage] = []
        all_tables: list[ExtractedTable] = []
        all_visuals: list[VisualRegion] = []
        doc_warnings: list[str] = []

        for item in analyzed_pages:
            doc_warnings.extend(f"p.{item.raw.page}: {w}" for w in item.route.warnings)

            # 레이아웃 세분화 페이지라면 전체 PNG를 페이지당 1회만 디코딩해 부분 크롭에 재사용한다.
            decoded_layout_page: DecodedImage | None = None
            if (
                options.extract_crops
                and item.layout_crop_rects
                and item.layout_page_image is not None
                and item.layout_page_image.extension == ".png"
            ):
                decoded_layout_page = decode_png(item.layout_page_image.data)

            processed_visuals: list[VisualRegion] = []
            for vis in item.visuals:
                embedded = item.visuals_res.region_images.get(vis.region_id)
                layout_rect = item.layout_crop_rects.get(vis.region_id)
                vis_cropped, encoded_img = _extract_crop_for_visual(
                    source_path,
                    vis,
                    embedded,
                    options=options,
                    renderer=renderer,
                    layout_decoded_page=decoded_layout_page,
                    layout_crop_rect=layout_rect,
                )
                ctx_before, ctx_after = _find_surrounding_context(
                    vis_cropped, item.layout_res.text_blocks
                )
                vis_final = _run_vision_adapter_on_region(
                    vis_cropped,
                    encoded_img,
                    vision_adapter,
                    context_before=ctx_before,
                    context_after=ctx_after,
                )
                processed_visuals.append(vis_final)

            blocks = _assemble_reading_order_blocks(
                item.raw.page,
                item.layout_res.text_blocks,
                item.tables,
                processed_visuals,
                item.layout_res.column_gutters_x,
                preserve_visual_order=item.layout_segmented,
            )
            hf_blocks = [
                DocumentBlock(
                    block_id=f"p{item.raw.page:03d}_hf{idx:02d}",
                    page=item.raw.page,
                    reading_order=idx,
                    column_index=0,
                    kind=ElementKind.HEADER_FOOTER,
                    strategy=blk.strategy,
                    bbox=blk.bbox,
                    text=blk.text,
                    font_size=blk.font_size,
                    is_bold=blk.is_bold,
                    confidence=blk.confidence,
                )
                for idx, blk in enumerate(item.layout_res.header_footer_blocks, 1)
            ]

            parsed_pages.append(
                ParsedPage(
                    page=item.raw.page,
                    route=item.route,
                    blocks=blocks,
                    header_footer_blocks=hf_blocks,
                    tables=item.tables,
                    visual_regions=processed_visuals,
                )
            )
            all_tables.extend(item.tables)
            all_visuals.extend(processed_visuals)

        return ParsedDocument(
            document=extracted.metadata,
            routing_plan=routing_plan,
            pages=parsed_pages,
            tables=all_tables,
            visual_regions=all_visuals,
            warnings=doc_warnings,
        )
