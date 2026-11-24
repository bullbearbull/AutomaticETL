"""무의존성 OOXML 엑셀(`.xlsx`) 문서용 `DocumentFormatAdapter` 구현체."""

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
    DocumentMetadata,
    ExtractedTable,
    ParsedDocument,
    ParsedPage,
    RoutingPlan,
    SourceFormat,
    VisualRegion,
)
from autoetl.parser.docx.adapter import _apply_layout_detection_to_docx_page
from autoetl.parser.docx.router import (
    AnalyzedDocxPage,
    analyze_docx_page,
    build_docx_routing_plan,
    estimate_docx_body_font_size,
    find_repeating_docx_logos,
)
from autoetl.parser.image_io import DecodedImage, decode_png
from autoetl.parser.pptx.adapter import (
    _extract_crop_for_pptx_visual,
    _find_surrounding_slide_context,
    _run_vision_on_pptx_region,
)
from autoetl.parser.xlsx.elements import extract_xlsx_pages
from autoetl.parser.xlsx.ooxml import XlsxPackage


def _relabel_to_xlsx(text: str) -> str:
    return (
        text.replace("DOCX ", "XLSX ")
        .replace("WordprocessingML", "SpreadsheetML")
        .replace("Word 섹션", "Excel 시트")
        .replace("Word 본문", "Excel 시트")
    )


class XlsxDocumentAdapter:
    """OOXML SpreadsheetML(`.xlsx`)용 무의존성 `DocumentFormatAdapter` 구현체."""

    @property
    def format(self) -> SourceFormat:
        return SourceFormat.XLSX

    @property
    def extensions(self) -> tuple[str, ...]:
        return (".xlsx",)

    def _analyze_document(
        self,
        source_path: Path,
        options: ParseOptions,
        *,
        layout_detector: PageLayoutDetector | None = None,
    ) -> tuple[list[AnalyzedDocxPage], RoutingPlan, list[str]]:
        with XlsxPackage(source_path) as pkg:
            raw_pages, meta, extract_warnings = extract_xlsx_pages(pkg)
            total_sheets = len(raw_pages)
            file_size = pkg.file_size
            file_sha256 = pkg.sha256

        if options.pages is not None:
            invalid = [p for p in options.pages if p < 1 or p > total_sheets]
            if invalid:
                raise ParserInputError(
                    f"선택한 시트 번호가 워크북 범위(1~{total_sheets})를 벗어났다: {invalid}"
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

        analyzed_pages: list[AnalyzedDocxPage] = []
        for raw_page in target_pages:
            ap = analyze_docx_page(
                raw_page,
                doc_body_font_size=body_font,
                repeating_logos=repeating_logos,
                include_comments=options.include_comments,
            )
            ap.visuals = [
                v.model_copy(update={"reason": _relabel_to_xlsx(v.reason)}) for v in ap.visuals
            ]
            ap.route = ap.route.model_copy(
                update={
                    "reasons": [_relabel_to_xlsx(r) for r in ap.route.reasons],
                    "regions": [
                        r.model_copy(update={"reason": _relabel_to_xlsx(r.reason)})
                        for r in ap.route.regions
                    ],
                }
            )
            if active_detector is not None:
                ap = _apply_layout_detection_to_docx_page(ap, layout_detector=active_detector)
            analyzed_pages.append(ap)

        title = meta.title or source_path.stem
        metadata = DocumentMetadata(
            source_path=source_path.name,
            format=SourceFormat.XLSX,
            file_size_bytes=file_size,
            sha256=file_sha256,
            page_count=total_sheets,
            title=title,
            author=meta.author,
            creator=meta.creator or "OOXML SpreadsheetML",
        )
        plan = build_docx_routing_plan(
            metadata,
            analyzed_pages,
            body_font_size=body_font,
        )
        return analyzed_pages, plan, extract_warnings

    def inspect(
        self,
        source_path: Path,
        *,
        options: ParseOptions,
        layout_detector: PageLayoutDetector | None = None,
        page_renderer: PageRenderer | None = None,
    ) -> RoutingPlan:
        """`.xlsx` 워크북을 시트별로 진단해 구조 분류 및 파싱 전략 계획을 반환한다."""
        _, plan, _ = self._analyze_document(
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
        """`.xlsx` 워크북을 시트 단위 `ParsedDocument` 통합 IR로 파싱한다."""
        analyzed_pages, routing_plan, extract_warnings = self._analyze_document(
            source_path,
            options,
            layout_detector=layout_detector,
        )

        parsed_pages: list[ParsedPage] = []
        all_tables: list[ExtractedTable] = []
        all_visuals: list[VisualRegion] = []
        doc_warnings: list[str] = list(extract_warnings)

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

            all_tables.extend(item.tables)
            all_visuals.extend(processed_visuals)
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

        return ParsedDocument(
            document=routing_plan.document,
            routing_plan=routing_plan,
            pages=parsed_pages,
            tables=all_tables,
            visual_regions=all_visuals,
            warnings=doc_warnings,
        )


__all__ = ["XlsxDocumentAdapter"]
