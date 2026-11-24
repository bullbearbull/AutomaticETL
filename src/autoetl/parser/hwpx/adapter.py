"""HWPX 문서 형식 어댑터 — KS X 6101 OWPML 구조 분석·표/수식/그림 추출·레이아웃 탐지·비전 연동."""

from __future__ import annotations

from pathlib import Path

from autoetl.parser.adapters import (
    NoopLayoutDetector,
    PageLayoutDetector,
    PageRenderer,
    ParseOptions,
    ParserInputError,
    VisionParserAdapter,
)
from autoetl.parser.contracts import (
    ExtractedTable,
    ParsedDocument,
    ParsedPage,
    RoutingPlan,
    SourceFormat,
    VisualRegion,
)
from autoetl.parser.docx.adapter import _apply_layout_detection_to_docx_page
from autoetl.parser.hwpx.elements import extract_hwpx_pages
from autoetl.parser.hwpx.owpml import HwpxPackage
from autoetl.parser.hwpx.router import (
    AnalyzedHwpxPage,
    analyze_hwpx_page,
    build_hwpx_routing_plan,
    estimate_hwpx_body_font_size,
    find_repeating_hwpx_logos,
)
from autoetl.parser.image_io import DecodedImage, decode_png
from autoetl.parser.pptx.adapter import (
    _extract_crop_for_pptx_visual,
    _find_surrounding_slide_context,
    _run_vision_on_pptx_region,
)


class HwpxDocumentAdapter:
    """KS X 6101 OWPML `.hwpx` 심층 결정론 파서·라우터 어댑터."""

    @property
    def format(self) -> SourceFormat:
        return SourceFormat.HWPX

    @property
    def extensions(self) -> tuple[str, ...]:
        return (".hwpx",)

    def _analyze_document(
        self,
        source_path: Path,
        options: ParseOptions,
        *,
        layout_detector: PageLayoutDetector | None = None,
    ) -> tuple[list[AnalyzedHwpxPage], RoutingPlan]:
        with HwpxPackage(source_path) as pkg:
            raw_pages = extract_hwpx_pages(pkg)
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

        body_font = estimate_hwpx_body_font_size(raw_pages)
        repeating_logos = find_repeating_hwpx_logos(raw_pages)
        active_detector = (
            layout_detector
            if (layout_detector is not None and not isinstance(layout_detector, NoopLayoutDetector))
            else None
        )

        analyzed: list[AnalyzedHwpxPage] = []
        for raw_page in target_pages:
            item = analyze_hwpx_page(
                raw_page,
                doc_body_font_size=body_font,
                repeating_logos=repeating_logos,
                include_comments=options.include_comments,
            )
            if active_detector is not None:
                item = _apply_layout_detection_to_docx_page(item, layout_detector=active_detector)
            analyzed.append(item)

        plan = build_hwpx_routing_plan(
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
        """HWPX 문서의 구조 유형과 페이지·요소별 파싱 전략 계획(`RoutingPlan`)을 진단한다."""
        _ = page_renderer
        _, plan = self._analyze_document(
            source_path,
            options,
            layout_detector=layout_detector,
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
        """HWPX 문서를 구조 진단 → 레이아웃 탐지 → 본문·표·수식 추출 → 비전 해석까지 수행한다."""
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


__all__ = ["HwpxDocumentAdapter"]
