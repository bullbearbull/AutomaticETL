"""PPTX 문서 형식 어댑터 — OOXML 구조 분석·차트/표/SmartArt 추출·레이아웃 탐지·비전 연동."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from autoetl.parser.adapters import (
    LayoutDetectionInput,
    NoopLayoutDetector,
    NoopVisionAdapter,
    PageLayoutDetector,
    PageRenderer,
    ParseOptions,
    ParserInputError,
    VisionParserAdapter,
    VisionRequest,
)
from autoetl.parser.contracts import (
    CropStatus,
    DocumentBlock,
    ElementKind,
    ExtractedTable,
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
    decode_png,
    save_encoded_image,
)
from autoetl.parser.layout_detector import boxes_to_visual_regions
from autoetl.parser.pptx.elements import RawPptxImage, RawSlideData, extract_slide_data
from autoetl.parser.pptx.ooxml import PptxPackage, clamp, probe_image_dimensions
from autoetl.parser.pptx.router import (
    AnalyzedSlide,
    analyze_slide,
    build_pptx_routing_plan,
    estimate_deck_body_font_size,
    find_repeating_pptx_headers_footers,
    find_repeating_pptx_logos,
)


def _union_area_bboxes(visuals: list[VisualRegion]) -> float:
    rects = [(v.bbox.x0, v.bbox.top, v.bbox.x1, v.bbox.bottom) for v in visuals]
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
        covered = 0.0
        for s, e in intervals[1:]:
            if s <= cur_e:
                cur_e = max(cur_e, e)
            else:
                covered += cur_e - cur_s
                cur_s, cur_e = s, e
        covered += cur_e - cur_s
        total += (x1 - x0) * covered
    return total


def _apply_layout_detection_to_slide(
    item: AnalyzedSlide,
    *,
    layout_detector: PageLayoutDetector,
) -> AnalyzedSlide:
    """스캔/스크린샷 전용 슬라이드에 레이아웃 객체 탐지를 적용해 세부 영역으로 분기한다."""
    if not item.is_full_slide_scan or not item.visual_images:
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
        page=item.raw.slide_number,
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
        page=item.raw.slide_number,
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

    # 기존 SCANNED_PAGE 블록을 세분화된 시각 블록(읽기 순서 보존)으로 교체
    non_visual_blocks = [b for b in item.blocks if b.visual_region_id is None]
    new_vis_blocks = [
        DocumentBlock(
            block_id=f"{v.region_id}_blk",
            page=item.raw.slide_number,
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
    # 발표자 노트·주석이 있다면 마지막에 유지
    trailing_suffixes = ("_notes", "_comments")
    trailing_blocks = [b for b in non_visual_blocks if b.block_id.endswith(trailing_suffixes)]
    other_blocks = [b for b in non_visual_blocks if not b.block_id.endswith(trailing_suffixes)]
    combined_blocks = [
        blk.model_copy(update={"reading_order": idx})
        for idx, blk in enumerate([*other_blocks, *new_vis_blocks, *trailing_blocks], 1)
    ]

    slide_area = max(1.0, item.raw.width_pt * item.raw.height_pt)
    vis_area_ratio = round(clamp(_union_area_bboxes(new_visuals) / slide_area, 0.0, 1.0), 4)
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
                    f"레이아웃 탐지({layout_detector.name})로 스캔 슬라이드를 "
                    f"{len(new_visuals)}개 영역으로 세분화({summary_parts})"
                ),
            ],
            "metrics": new_metrics,
            "regions": all_routes,
            "requires_vision": True,
        }
    )

    return AnalyzedSlide(
        raw=item.raw,
        tables=item.tables,
        visuals=new_visuals,
        visual_images={},
        blocks=combined_blocks,
        header_footer_blocks=item.header_footer_blocks,
        route=new_route,
        slide_title=item.slide_title,
        is_full_slide_scan=True,
        has_running_header=item.has_running_header,
        has_running_footer=item.has_running_footer,
        layout_page_image_bytes=dom_img.raw_bytes,
        layout_page_image_ext=dom_img.extension,
        layout_page_image_size=(img_w, img_h),
        layout_crop_rects=crop_rects,
        layout_segmented=True,
    )


_DIRECT_RASTER_EXTS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"})


def _extract_crop_for_pptx_visual(
    source_path: Path,
    region: VisualRegion,
    embedded_img: RawPptxImage | None,
    *,
    options: ParseOptions,
    renderer: PageRenderer | None,
    decoded_layout_page: DecodedImage | None,
    layout_crop_rect: tuple[int, int, int, int] | None,
) -> tuple[VisualRegion, EncodedImage | None]:
    """PPTX 시각 영역 크롭을 추출(레이아웃 크롭 → 내장 래스터/렌더러 → 벡터 폴백)한다."""
    if not options.extract_crops:
        return region.model_copy(update={"crop_status": CropStatus.SKIPPED}), None

    encoded: EncodedImage | None = None
    via_embedded = False

    if layout_crop_rect is not None and decoded_layout_page is not None:
        encoded = crop_decoded_image(decoded_layout_page, *layout_crop_rect)

    has_embedded_bytes = embedded_img is not None and bool(embedded_img.raw_bytes)
    is_direct_raster = (
        has_embedded_bytes
        and embedded_img is not None
        and embedded_img.extension.lower() in _DIRECT_RASTER_EXTS
    )

    # 표준 래스터 이미지(PNG/JPG 등)는 렌더러 없이 원본 바이트 즉시 사용
    if encoded is None and is_direct_raster and embedded_img is not None and embedded_img.raw_bytes:
        encoded = EncodedImage(
            data=embedded_img.raw_bytes,
            extension=embedded_img.extension,
            width=embedded_img.pixel_width,
            height=embedded_img.pixel_height,
        )
        via_embedded = True

    # 벡터 도식·OLE·EMF/WMF/SVG 등은 선택 렌더러(LibreOffice)가 있으면 PNG 크롭 우선 생성
    if encoded is None and renderer is not None and renderer.available:
        crop_bbox = None if region.element_kind == ElementKind.SCANNED_PAGE else region.bbox
        encoded = renderer.render_region(
            source_path,
            region.page,
            crop_bbox,
            dpi=options.render_dpi,
        )

    # 렌더러가 없거나 실패해도 내장 벡터/VML 미리보기(.svg/.emf/.wmf 등)가 있으면 보존
    if (
        encoded is None
        and has_embedded_bytes
        and embedded_img is not None
        and embedded_img.raw_bytes
    ):
        encoded = EncodedImage(
            data=embedded_img.raw_bytes,
            extension=embedded_img.extension,
            width=embedded_img.pixel_width,
            height=embedded_img.pixel_height,
        )
        via_embedded = True

    if encoded is None:
        if renderer is not None and renderer.available:
            return region.model_copy(update={"crop_status": CropStatus.FAILED}), None
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


def _find_surrounding_slide_context(
    region: VisualRegion,
    blocks: list[DocumentBlock],
) -> tuple[str | None, str | None]:
    vis_idx = next(
        (idx for idx, b in enumerate(blocks) if b.visual_region_id == region.region_id),
        None,
    )
    if vis_idx is None:
        return None, None
    ctx_before: str | None = None
    for b in reversed(blocks[:vis_idx]):
        if b.text.strip() and b.visual_region_id is None and b.table_id is None:
            ctx_before = b.text.strip()[:240]
            break
    ctx_after: str | None = None
    for b in blocks[vis_idx + 1 :]:
        if b.text.strip() and b.visual_region_id is None and b.table_id is None:
            ctx_after = b.text.strip()[:240]
            break
    return ctx_before, ctx_after


def _run_vision_on_pptx_region(
    region: VisualRegion,
    encoded_img: EncodedImage | None,
    vision_adapter: VisionParserAdapter | None,
    *,
    context_before: str | None = None,
    context_after: str | None = None,
) -> VisualRegion:
    if vision_adapter is None or isinstance(vision_adapter, NoopVisionAdapter):
        return region.model_copy(update={"vision_status": VisionStatus.PENDING})

    req = VisionRequest(
        region_id=region.region_id,
        page=region.page,
        element_kind=region.element_kind,
        recommended_strategy=region.recommended_strategy,
        bbox=region.bbox,
        reason=region.reason,
        image_bytes=encoded_img.data if encoded_img is not None else None,
        image_extension=encoded_img.extension if encoded_img is not None else None,
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


class PptxDocumentAdapter:
    """OOXML `.pptx` 심층 결정론 파서·라우터 어댑터."""

    @property
    def format(self) -> SourceFormat:
        return SourceFormat.PPTX

    @property
    def extensions(self) -> tuple[str, ...]:
        return (".pptx",)

    def _analyze_deck(
        self,
        source_path: Path,
        options: ParseOptions,
        *,
        layout_detector: PageLayoutDetector | None = None,
    ) -> tuple[list[AnalyzedSlide], RoutingPlan]:
        with PptxPackage(source_path) as pkg:
            metadata, slide_w, slide_h, slide_refs, sections = pkg.read_presentation_structure()
            comment_authors = pkg.read_comment_authors()
            total_slides = len(slide_refs)

            if options.pages is not None:
                invalid = [p for p in options.pages if p < 1 or p > total_slides]
                if invalid:
                    raise ParserInputError(
                        f"선택한 슬라이드 번호가 문서 범위(1~{total_slides})를 벗어났다: {invalid}"
                    )
                selected_set = set(options.pages)
                target_refs = [ref for ref in slide_refs if ref.slide_number in selected_set]
            else:
                target_refs = slide_refs

            raw_slides: list[RawSlideData] = [
                extract_slide_data(
                    pkg,
                    slide_number=ref.slide_number,
                    slide_id=ref.slide_id,
                    part_path=ref.part_path,
                    width_pt=slide_w,
                    height_pt=slide_h,
                    comment_authors=comment_authors,
                )
                for ref in target_refs
            ]

        body_font = estimate_deck_body_font_size(raw_slides)
        repeating_logos = find_repeating_pptx_logos(raw_slides)
        if options.detect_running_headers_footers:
            repeating_headers, repeating_footers = find_repeating_pptx_headers_footers(
                raw_slides,
                deck_body_font_size=body_font,
            )
        else:
            repeating_headers, repeating_footers = set(), set()

        active_detector = (
            layout_detector
            if (layout_detector is not None and not isinstance(layout_detector, NoopLayoutDetector))
            else None
        )

        analyzed: list[AnalyzedSlide] = []
        for raw_slide in raw_slides:
            item = analyze_slide(
                raw_slide,
                deck_body_font_size=body_font,
                repeating_logos=repeating_logos,
                repeating_headers=repeating_headers,
                repeating_footers=repeating_footers,
                include_speaker_notes=options.include_speaker_notes,
                include_comments=options.include_comments,
            )
            if active_detector is not None:
                item = _apply_layout_detection_to_slide(item, layout_detector=active_detector)
            analyzed.append(item)

        plan = build_pptx_routing_plan(
            metadata,
            analyzed,
            sections,
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
        """PPTX 발표 자료의 슬라이드별 구조 분류와 파싱 전략 계획을 생성한다."""
        _ = page_renderer
        _, plan = self._analyze_deck(source_path, options, layout_detector=layout_detector)
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
        """PPTX를 구조 진단·차트/표/SmartArt 추출·미디어 크롭·비전 연동까지 실행한다."""
        analyzed_slides, routing_plan = self._analyze_deck(
            source_path,
            options,
            layout_detector=layout_detector,
        )

        parsed_pages: list[ParsedPage] = []
        all_tables: list[ExtractedTable] = []
        all_visuals: list[VisualRegion] = []
        doc_warnings: list[str] = []

        for item in analyzed_slides:
            doc_warnings.extend(f"s.{item.raw.slide_number}: {w}" for w in item.route.warnings)

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
                    page=item.raw.slide_number,
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
