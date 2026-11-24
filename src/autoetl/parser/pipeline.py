"""Auto Parser 파이프라인 오케스트레이터와 형식 어댑터 레지스트리.

확장자 기반으로 적절한 `DocumentFormatAdapter`를 선택해 구조 진단(`inspect_document`)
및 전체 파싱(`parse_document`, `parse_and_export`)을 수행한다.
현재 PDF·PPTX·DOCX·HWPX·XLSX 및 구형 OLE2(HWP·DOC·PPT) 8종 어댑터가 기본 등록되어 있으며,
추가 형식도 `register_adapter(...)` 한 줄로 연결할 수 있다.
"""

from __future__ import annotations

from pathlib import Path

from autoetl.parser.adapters import (
    DocumentFormatAdapter,
    NoopVisionAdapter,
    PageLayoutDetector,
    PageRenderer,
    ParseOptions,
    ParserInputError,
    VisionParserAdapter,
    VisionRequest,
)
from autoetl.parser.chunker import ChunkOptions
from autoetl.parser.contracts import (
    ParsedDocument,
    ParsedPage,
    RoutingPlan,
    VisionStatus,
    VisualRegion,
)
from autoetl.parser.docx.adapter import DocxDocumentAdapter
from autoetl.parser.exporters import ExportManifest, export_parse_bundle
from autoetl.parser.hwpx.adapter import HwpxDocumentAdapter
from autoetl.parser.legacy import DocDocumentAdapter, HwpDocumentAdapter, PptDocumentAdapter
from autoetl.parser.pdf.adapter import PdfDocumentAdapter
from autoetl.parser.pptx.adapter import PptxDocumentAdapter
from autoetl.parser.xlsx import XlsxDocumentAdapter

_ADAPTER_REGISTRY: dict[str, DocumentFormatAdapter] = {}


def register_adapter(adapter: DocumentFormatAdapter) -> None:
    """확장자별 문서 형식 어댑터를 등록한다."""
    for ext in adapter.extensions:
        _ADAPTER_REGISTRY[ext.lower()] = adapter


def supported_extensions() -> tuple[str, ...]:
    """현재 등록된 지원 문서 확장자 목록을 반환한다."""
    return tuple(sorted(_ADAPTER_REGISTRY))


def get_adapter_for_path(source_path: Path) -> DocumentFormatAdapter:
    """입력 파일의 확장자에 맞는 어댑터를 찾는다. 미지원 형식이면 ParserInputError."""
    ext = source_path.suffix.lower()
    adapter = _ADAPTER_REGISTRY.get(ext)
    if adapter is None:
        supported = ", ".join(supported_extensions()) or "(없음)"
        raise ParserInputError(
            f"지원하지 않는 문서 형식이다: '{ext or '(확장자 없음)'}' (현재 지원: {supported})"
        )
    return adapter


def parse_page_selection(spec: str) -> tuple[int, ...]:
    """'1,3-5,8' 형식의 페이지 선택 문자열을 중복 없는 1-based 페이지 튜플로 변환한다."""
    cleaned = spec.strip()
    if not cleaned:
        raise ParserInputError("페이지 지정 문자열이 비어 있다.")

    pages: list[int] = []
    seen: set[int] = set()
    for part in cleaned.split(","):
        token = part.strip()
        if not token:
            raise ParserInputError(f"잘못된 페이지 범위 형식이다: '{spec}'")
        if "-" in token:
            pieces = token.split("-")
            if (
                len(pieces) != 2
                or not pieces[0].strip().isdigit()
                or not pieces[1].strip().isdigit()
            ):
                raise ParserInputError(f"잘못된 페이지 범위 형식이다: '{token}'")
            start = int(pieces[0].strip())
            end = int(pieces[1].strip())
            if start < 1 or end < start:
                raise ParserInputError(f"페이지 범위가 올바르지 않다: '{token}'")
            for p in range(start, end + 1):
                if p not in seen:
                    seen.add(p)
                    pages.append(p)
        else:
            if not token.isdigit():
                raise ParserInputError(f"잘못된 페이지 번호다: '{token}'")
            p = int(token)
            if p < 1:
                raise ParserInputError(f"페이지 번호는 1 이상이어야 한다: '{token}'")
            if p not in seen:
                seen.add(p)
                pages.append(p)
    return tuple(pages)


def inspect_document(
    source_path: Path | str,
    *,
    options: ParseOptions | None = None,
    layout_detector: PageLayoutDetector | None = None,
    page_renderer: PageRenderer | None = None,
) -> RoutingPlan:
    """문서의 구조 유형과 페이지·요소별 파싱 전략 계획(`RoutingPlan`)을 진단한다."""
    path = Path(source_path)
    adapter = get_adapter_for_path(path)
    return adapter.inspect(
        path,
        options=options or ParseOptions(),
        layout_detector=layout_detector,
        page_renderer=page_renderer,
    )


def parse_document(
    source_path: Path | str,
    *,
    options: ParseOptions | None = None,
    vision_adapter: VisionParserAdapter | None = None,
    page_renderer: PageRenderer | None = None,
    layout_detector: PageLayoutDetector | None = None,
) -> ParsedDocument:
    """문서를 구조 진단 → (선택)레이아웃 탐지 → 라우팅 → 자료별 파싱 → 통합 IR까지 실행한다."""
    path = Path(source_path)
    adapter = get_adapter_for_path(path)
    return adapter.parse(
        path,
        options=options or ParseOptions(),
        vision_adapter=vision_adapter,
        page_renderer=page_renderer,
        layout_detector=layout_detector,
    )


def parse_and_export(
    source_path: Path | str,
    out_dir: Path | str,
    *,
    options: ParseOptions | None = None,
    vision_adapter: VisionParserAdapter | None = None,
    page_renderer: PageRenderer | None = None,
    layout_detector: PageLayoutDetector | None = None,
    include_chunks: bool = True,
    chunk_options: ChunkOptions | None = None,
) -> tuple[ParsedDocument, ExportManifest]:
    """문서를 파싱하고 크롭·표·Markdown·청크(JSONL)·전략 리포트 일체를 `out_dir`에 저장한다."""
    path = Path(source_path)
    target = Path(out_dir)
    base_opts = options or ParseOptions()
    effective_opts = ParseOptions(
        pages=base_opts.pages,
        extract_crops=base_opts.extract_crops,
        crop_dir=(base_opts.crop_dir or (target / "crops")) if base_opts.extract_crops else None,
        render_dpi=base_opts.render_dpi,
        detect_borderless_tables=base_opts.detect_borderless_tables,
        detect_running_headers_footers=base_opts.detect_running_headers_footers,
        include_header_footer_in_markdown=base_opts.include_header_footer_in_markdown,
        layout_detect_all_pages=base_opts.layout_detect_all_pages,
        include_speaker_notes=base_opts.include_speaker_notes,
        include_comments=base_opts.include_comments,
    )
    doc = parse_document(
        path,
        options=effective_opts,
        vision_adapter=vision_adapter,
        page_renderer=page_renderer,
        layout_detector=layout_detector,
    )
    manifest = export_parse_bundle(
        doc,
        target,
        include_header_footer=effective_opts.include_header_footer_in_markdown,
        include_chunks=include_chunks,
        chunk_options=chunk_options,
    )
    return doc, manifest


def apply_vision_to_document(
    doc: ParsedDocument,
    vision_adapter: VisionParserAdapter,
    *,
    only_pending: bool = True,
    base_dir: Path | None = None,
) -> ParsedDocument:
    """이미 파싱된 `ParsedDocument`의 저장된 시각 크롭에 OCR/VLM 어댑터를 사후 적용한다."""
    if isinstance(vision_adapter, NoopVisionAdapter):
        return doc

    new_pages: list[ParsedPage] = []
    all_visuals: list[VisualRegion] = []

    for page in doc.pages:
        updated_visuals: list[VisualRegion] = []
        vis_map: dict[str, VisualRegion] = {}
        for vis in page.visual_regions:
            if only_pending and vis.vision_status == VisionStatus.COMPLETED:
                updated_visuals.append(vis)
                vis_map[vis.region_id] = vis
                continue

            crop_path_resolved: str | None = vis.crop_path
            img_bytes: bytes | None = None
            img_ext: str | None = None
            if vis.crop_path:
                cand = Path(vis.crop_path)
                if not cand.is_file() and base_dir is not None:
                    rel_cand = base_dir / vis.crop_path
                    if rel_cand.is_file():
                        cand = rel_cand
                if cand.is_file():
                    crop_path_resolved = cand.as_posix()
                    img_ext = cand.suffix
                    try:
                        img_bytes = cand.read_bytes()
                    except OSError:
                        img_bytes = None

            # 같은 페이지 블록에서 직전·직후 본문 문맥 추출
            vis_idx = next(
                (idx for idx, b in enumerate(page.blocks) if b.visual_region_id == vis.region_id),
                None,
            )
            ctx_before: str | None = None
            ctx_after: str | None = None
            if vis_idx is not None:
                for b in reversed(page.blocks[:vis_idx]):
                    if b.text.strip() and b.visual_region_id is None and b.table_id is None:
                        ctx_before = b.text.strip()[:240]
                        break
                for b in page.blocks[vis_idx + 1 :]:
                    if b.text.strip() and b.visual_region_id is None and b.table_id is None:
                        ctx_after = b.text.strip()[:240]
                        break

            req = VisionRequest(
                region_id=vis.region_id,
                page=vis.page,
                element_kind=vis.element_kind,
                recommended_strategy=vis.recommended_strategy,
                bbox=vis.bbox,
                reason=vis.reason,
                image_bytes=img_bytes,
                image_extension=img_ext,
                crop_path=crop_path_resolved,
                embedded_text_hints=tuple(vis.embedded_text_hints),
                caption=vis.caption,
                context_before=ctx_before,
                context_after=ctx_after,
            )
            try:
                res = vision_adapter.parse_region(req)
            except Exception:
                vis_out = vis.model_copy(
                    update={
                        "vision_status": VisionStatus.FAILED,
                        "vision_adapter": vision_adapter.name,
                        "vision_metadata": {"vision_error": "adapter_exception"},
                    }
                )
            else:
                if res.succeeded:
                    vis_out = vis.model_copy(
                        update={
                            "vision_status": VisionStatus.COMPLETED,
                            "vision_adapter": res.adapter_name,
                            "vision_text": res.text,
                            "vision_markdown": res.markdown or res.text,
                            "vision_metadata": dict(res.metadata),
                        }
                    )
                else:
                    meta = dict(res.metadata)
                    if res.error:
                        meta["vision_error"] = res.error
                    vis_out = vis.model_copy(
                        update={
                            "vision_status": VisionStatus.FAILED,
                            "vision_adapter": res.adapter_name,
                            "vision_metadata": meta,
                        }
                    )
            updated_visuals.append(vis_out)
            vis_map[vis_out.region_id] = vis_out

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
            for blk in page.blocks
        ]
        new_pages.append(
            page.model_copy(update={"blocks": updated_blocks, "visual_regions": updated_visuals})
        )
        all_visuals.extend(updated_visuals)

    return doc.model_copy(update={"pages": new_pages, "visual_regions": all_visuals})


# 기본 내장 어댑터 등록
register_adapter(PdfDocumentAdapter())
register_adapter(PptxDocumentAdapter())
register_adapter(DocxDocumentAdapter())
register_adapter(HwpxDocumentAdapter())
register_adapter(HwpDocumentAdapter())
register_adapter(DocDocumentAdapter())
register_adapter(PptDocumentAdapter())
register_adapter(XlsxDocumentAdapter())
