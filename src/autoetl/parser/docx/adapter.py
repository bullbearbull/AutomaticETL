"""DOCX 문서 형식 어댑터 — OOXML 구조 분석·표/차트/수식/각주/주석 추출·레이아웃 탐지·비전 연동."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from autoetl.parser.adapters import (
    LayoutDetectionInput,
    NoopLayoutDetector,
    PageLayoutDetector,
    PageRenderer,
    ParseOptions,
    ParserInputError,
    VisionParserAdapter,
)
from autoetl.parser.contracts import (
    DocumentBlock,
    ExtractedTable,
    ParsedDocument,
    ParsedPage,
    ParseStrategy,
    RegionRoute,
    RoutingPlan,
    SourceFormat,
    VisualRegion,
)
from autoetl.parser.docx.elements import extract_docx_pages
from autoetl.parser.docx.ooxml import DocxPackage, clamp, probe_image_dimensions
from autoetl.parser.docx.router import (
    AnalyzedDocxPage,
    analyze_docx_page,
    build_docx_routing_plan,
    estimate_docx_body_font_size,
    find_repeating_docx_logos,
)
from autoetl.parser.image_io import DecodedImage, decode_png
from autoetl.parser.layout_detector import boxes_to_visual_regions
from autoetl.parser.pptx.adapter import (
    _extract_crop_for_pptx_visual,
    _find_surrounding_slide_context,
    _run_vision_on_pptx_region,
    _union_area_bboxes,
)


def _apply_layout_detection_to_docx_page(
    item: AnalyzedDocxPage,
    *,
    layout_detector: PageLayoutDetector,
) -> AnalyzedDocxPage:
    """전체 스캔 이미지 DOCX 페이지에 레이아웃 객체 탐지를 적용해 세부 영역으로 분기한다."""
    if not item.is_full_page_scan or not item.visual_images:
        return item

    dom_img = next(iter(item.visual_images.values()))
    if not dom_img.raw_bytes:
        return item

    img_w = dom_img.pixel_width or 0
    img_h = dom_img.pixel_height or 0
    if img_w <= 0 or img_h <= 0:
        pw, ph = probe_image_dimensions(dom_img.raw_bytes, dom_img.extension)
        img_w = pw or 0
        img_h = ph or 0
    if img_w <= 0 or img_h <= 0:
        return item

    det_input = LayoutDetectionInput(
        page=item.raw.page_number,
        image_bytes=dom_img.raw_bytes,
        image_extension=dom_img.extension,
        image_width_px=img_w,
        image_height_px=img_h,
        page_width_pt=item.raw.width_pt,
        page_height_pt=item.raw.height_pt,
    )
    boxes = layout_detector.detect(det_input)
    if not boxes:
        return item

    region_pairs = boxes_to_visual_regions(
        boxes,
        page=item.raw.page_number,
        page_width_pt=item.raw.width_pt,
        page_height_pt=item.raw.height_pt,
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

    non_visual_blocks = [b for b in item.blocks if b.visual_region_id is None]
    new_vis_blocks = [
        DocumentBlock(
            block_id=f"{v.region_id}_blk",
            page=item.raw.page_number,
            reading_order=0,
            column_index=0,
            kind=v.element_kind,
            strategy=v.recommended_strategy,
            bbox=v.bbox,
            text=v.caption or "",
            visual_region_id=v.region_id,
            confidence=v.confidence,
        )
        for v in new_visuals
    ]

    trailing_blocks = [
        b
        for b in non_visual_blocks
        if b.block_id.endswith("_comments") or "_fn" in b.block_id or "_en" in b.block_id
    ]
    other_blocks = [b for b in non_visual_blocks if b not in trailing_blocks]
    combined_blocks = [
        blk.model_copy(update={"reading_order": idx})
        for idx, blk in enumerate([*other_blocks, *new_vis_blocks, *trailing_blocks], 1)
    ]

    page_area = max(1.0, item.raw.width_pt * item.raw.height_pt)
    vis_area_ratio = round(clamp(_union_area_bboxes(new_visuals) / page_area, 0.0, 1.0), 4)
    has_vlm = any(v.recommended_strategy == ParseStrategy.VLM_REGION for v in new_visuals)
    new_primary = ParseStrategy.VLM_REGION if has_vlm else ParseStrategy.EMBEDDED_IMAGE_OCR

    non_vis_routes = [r for r in item.route.regions if not r.requires_vision]
    vis_routes = [
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
    all_routes = [*non_vis_routes, *vis_routes]
    all_strats = [r.strategy for r in all_routes]
    new_secondary = list(dict.fromkeys(s for s in all_strats if s != new_primary))

    kind_counts = Counter(v.element_kind.value for v in new_visuals)
    summary_parts = ", ".join(f"{k} {cnt}" for k, cnt in kind_counts.items())
    new_metrics = item.route.metrics.model_copy(
        update={
            "visual_region_count": len(new_visuals),
            "visual_area_ratio": vis_area_ratio,
        }
    )
    new_route = item.route.model_copy(
        update={
            "primary_strategy": new_primary,
            "secondary_strategies": new_secondary,
            "reasons": [
                *item.route.reasons,
                (
                    f"레이아웃 탐지({layout_detector.name})로 스캔 페이지를 "
                    f"{len(new_visuals)}개 영역으로 세분화({summary_parts})"
                ),
            ],
            "metrics": new_metrics,
            "regions": all_routes,
            "requires_vision": True,
        }
    )

    return AnalyzedDocxPage(
        raw=item.raw,
        tables=item.tables,
        visuals=new_visuals,
        visual_images={},
        blocks=combined_blocks,
        header_footer_blocks=item.header_footer_blocks,
        route=new_route,
        headings=item.headings,
        is_full_page_scan=True,
        has_running_header=item.has_running_header,
        has_running_footer=item.has_running_footer,
        layout_page_image_bytes=dom_img.raw_bytes,
        layout_page_image_ext=dom_img.extension,
        layout_page_image_size=(img_w, img_h),
        layout_crop_rects=crop_rects,
        layout_segmented=True,
    )


class DocxDocumentAdapter:
    """OOXML `.docx` 심층 결정론 파서·라우터 어댑터."""

    @property
    def format(self) -> SourceFormat:
        return SourceFormat.DOCX

    @property
    def extensions(self) -> tuple[str, ...]:
        return (".docx",)

    def _analyze_document(
        self,
        source_path: Path,
        options: ParseOptions,
        *,
        layout_detector: PageLayoutDetector | None = None,
    ) -> tuple[list[AnalyzedDocxPage], RoutingPlan]:
        with DocxPackage(source_path) as pkg:
            raw_pages = extract_docx_pages(pkg)
            total_pages = len(raw_pages)
            metadata = pkg.read_metadata(page_count=total_pages)

        if options.pages is not None:
            invalid = [p for p in options.pages if p < 1 or p > total_pages]
            if invalid:
                raise ParserInputError(
                    f"선택한 페이지 번호가 문서 범위(1~{total_pages})를 벗어났다: {invalid}"
                )
            selected_set = set(options.pages)
            target_pages = [p for p in raw_pages if p.page_number in selected_set]
        else:
            target_pages = raw_pages

        body_font = estimate_docx_body_font_size(raw_pages)
        repeating_logos = find_repeating_docx_logos(raw_pages)
        active_detector = (
            layout_detector
            if (layout_detector is not None and not isinstance(layout_detector, NoopLayoutDetector))
            else None
        )

        analyzed: list[AnalyzedDocxPage] = []
        for raw_page in target_pages:
            item = analyze_docx_page(
                raw_page,
                doc_body_font_size=body_font,
                repeating_logos=repeating_logos,
                include_comments=options.include_comments,
            )
            if active_detector is not None:
                item = _apply_layout_detection_to_docx_page(item, layout_detector=active_detector)
            analyzed.append(item)

        plan = build_docx_routing_plan(
            metadata,
            analyzed,
            body_font_size=body_font,
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
        """DOCX 문서의 페이지별 구조 분류와 파싱 전략 계획을 생성한다."""
        _ = page_renderer
        _, plan = self._analyze_document(source_path, options, layout_detector=layout_detector)
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
        """DOCX를 구조 진단·본문/표/차트/수식/각주/주석 추출·미디어 크롭·비전 연동까지 실행한다."""
        analyzed_pages, routing_plan = self._analyze_document(
            source_path,
            options,
            layout_detector=layout_detector,
        )

        parsed_pages: list[ParsedPage] = []
        all_tables: list[ExtractedTable] = []
        all_visuals: list[VisualRegion] = []
        doc_warnings: list[str] = []

        for item in analyzed_pages:
            doc_warnings.extend(f"p.{item.raw.page_number}: {w}" for w in item.route.warnings)

            decoded_layout_page: DecodedImage | None = None
            if (
                options.extract_crops
                and item.layout_crop_rects
                and item.layout_page_image_bytes is not None
                and item.layout_page_image_ext == ".png"
            ):
                decoded_layout_page = decode_png(item.layout_page_image_bytes)

            processed_visuals: list[VisualRegion] = []
            vis_map: dict[str, VisualRegion] = {}
            for vis in item.visuals:
                emb_img = item.visual_images.get(vis.region_id)
                layout_rect = item.layout_crop_rects.get(vis.region_id)
                vis_cropped, encoded_img = _extract_crop_for_pptx_visual(
                    source_path,
                    vis,
                    emb_img,
                    options=options,
                    renderer=page_renderer,
                    decoded_layout_page=decoded_layout_page,
                    layout_crop_rect=layout_rect,
                )
                ctx_before, ctx_after = _find_surrounding_slide_context(vis_cropped, item.blocks)
                vis_final = _run_vision_on_pptx_region(
                    vis_cropped,
                    encoded_img,
                    vision_adapter,
                    context_before=ctx_before,
                    context_after=ctx_after,
                )
                processed_visuals.append(vis_final)
                vis_map[vis_final.region_id] = vis_final

            updated_blocks = [
                blk.model_copy(
                    update={
                        "text": (
                            vis_map[blk.visual_region_id].vision_markdown
                            or vis_map[blk.visual_region_id].vision_text
                            or vis_map[blk.visual_region_id].caption
                            or blk.text
                        )
                    }
                )
                if blk.visual_region_id and blk.visual_region_id in vis_map
                else blk
                for blk in item.blocks
            ]

            parsed_pages.append(
                ParsedPage(
                    page=item.raw.page_number,
                    route=item.route,
                    blocks=updated_blocks,
                    header_footer_blocks=item.header_footer_blocks,
                    tables=item.tables,
                    visual_regions=processed_visuals,
                )
            )
            all_tables.extend(item.tables)
            all_visuals.extend(processed_visuals)

        return ParsedDocument(
            document=routing_plan.document,
            routing_plan=routing_plan,
            pages=parsed_pages,
            tables=all_tables,
            visual_regions=all_visuals,
            warnings=doc_warnings,
        )


__all__ = ["DocxDocumentAdapter"]
