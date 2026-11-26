"""`rhwp` (`rhwp-python`, Rust `edwardkim/rhwp`) 기반 HWP(`.hwp`) · HWPX(`.hwpx`) 통합 어댑터.

한글 바이너리(`.hwp`, HWP 5.0)와 개방형 XML(`.hwpx`, KS X 6101) 문서를 모두
``rhwp.parse()`` → Document IR(``HwpDocument``) · 내장 이미지(``bytes_for_image``) ·
네이티브 래스터 PNG 렌더링(``render_png``)으로 처리해 AutoETL ``ParsedDocument``
계약으로 변환한다.
"""

from __future__ import annotations

import hashlib
import os
import re
import struct
import zipfile
from collections import Counter, defaultdict
from collections.abc import Sequence
from pathlib import Path
from xml.etree import ElementTree as ET

import rhwp
from rhwp.ir.nodes import (
    Block,
    CaptionBlock,
    EndnoteBlock,
    FieldBlock,
    FootnoteBlock,
    FormulaBlock,
    HwpDocument,
    InlineRun,
    ListItemBlock,
    ParagraphBlock,
    PictureBlock,
    TableBlock,
    TocBlock,
)

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
    BBox,
    CropStatus,
    DocumentBlock,
    DocumentMetadata,
    ElementKind,
    ExtractedTable,
    OutlineEntry,
    PageCategory,
    PageMetrics,
    PageRouteDecision,
    ParsedDocument,
    ParsedPage,
    ParseStrategy,
    RegionRoute,
    RoutingPlan,
    RoutingSummary,
    SourceFormat,
    TableCell,
    VisionStatus,
    VisualRegion,
)
from autoetl.parser.docx.ooxml import clamp, make_bbox, probe_image_dimensions
from autoetl.parser.image_io import (
    EncodedImage,
    crop_decoded_image,
    decode_png,
    save_encoded_image,
)
from autoetl.parser.layout_detector import boxes_to_visual_regions
from autoetl.parser.pptx.adapter import _find_surrounding_slide_context, _union_area_bboxes

_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_A4_WIDTH_PT = 595.28
_A4_HEIGHT_PT = 841.89

_NUMBERED_HEADING_RE = re.compile(r"^(?:\d+(?:\.\d+)*\.?|제\s*\d+\s*[장절관조항])\s+\S")
_EXISTING_LIST_PREFIX_RE = re.compile(
    r"^(?:\d+(?:\.\d+)*[.)]|[가-하ㄱ-ㅎ][.)]|[•·▪▫◦○●□■▸▹►▶\-\u2013\u2014*])\s*"
)


def _mime_to_extension(mime_type: str | None) -> str:
    low = (mime_type or "").lower()
    if "jpeg" in low or "jpg" in low:
        return ".jpg"
    if "gif" in low:
        return ".gif"
    if "bmp" in low:
        return ".bmp"
    if "webp" in low:
        return ".webp"
    if "svg" in low:
        return ".svg"
    if "emf" in low:
        return ".emf"
    if "wmf" in low:
        return ".wmf"
    return ".png"


def _join_inlines(inlines: Sequence[InlineRun], fallback_text: str) -> str:
    if not inlines:
        return fallback_text.strip()
    parts: list[str] = []
    for run in inlines:
        seg = run.text
        if run.ruby and run.ruby not in seg:
            seg = f"{seg}({run.ruby})"
        if run.href and run.href not in seg:
            seg = f"{seg} ({run.href})" if seg else run.href
        parts.append(seg)
    joined = "".join(parts).strip()
    return joined or fallback_text.strip()


def _flatten_block_text(block: Block) -> str:
    if isinstance(block, ParagraphBlock):
        return _join_inlines(block.inlines, block.text)
    if isinstance(block, ListItemBlock):
        body = _join_inlines(block.inlines, block.text)
        if not body:
            return ""
        marker = block.marker.strip()
        if marker and not _EXISTING_LIST_PREFIX_RE.match(body):
            return f"{marker} {body}"
        return body
    if isinstance(block, FormulaBlock):
        return (block.text_alt or block.script).strip()
    if isinstance(block, TableBlock):
        return block.text.strip()
    if isinstance(block, PictureBlock):
        if block.description:
            return block.description.strip()
        if block.caption is not None:
            return _flatten_blocks_text(block.caption.blocks)
        return ""
    if isinstance(block, (CaptionBlock, FootnoteBlock, EndnoteBlock)):
        return _flatten_blocks_text(block.blocks)
    if isinstance(block, FieldBlock):
        if block.field_kind == "hyperlink" and block.cached_value and block.raw_instruction:
            return f"{block.cached_value} ({block.raw_instruction})".strip()
        return (block.cached_value or block.raw_instruction or "").strip()
    if isinstance(block, TocBlock):
        return "\n".join(entry.text.strip() for entry in block.entries if entry.text.strip())
    return ""


def _flatten_blocks_text(blocks: Sequence[Block]) -> str:
    lines = [txt for b in blocks if (txt := _flatten_block_text(b))]
    return " ".join(lines).strip()


def _looks_like_heading(text: str, *, all_bold: bool, is_list: bool, level: int) -> bool:
    stripped = text.strip()
    if not stripped or len(stripped) > 90:
        return False
    if _NUMBERED_HEADING_RE.match(stripped) and (not is_list or level <= 2):
        return True
    return not is_list and all_bold and not stripped.endswith((".", "다.", "요.", "?", "!"))


def _infer_heading_level(text: str, default_level: int = 1) -> int:
    m = re.match(r"^(\d+(?:\.\d+)*)(?:\.|\s)", text.strip())
    if m:
        return min(6, len(m.group(1).split(".")))
    return max(1, min(6, default_level))


def _validate_hwp_container(source_path: Path) -> tuple[str | None, str | None]:
    """HWP 파일의 암호화·DRM·배포용(ViewText) 차단 및 OLE 요약 메타데이터 추출."""
    try:
        with source_path.open("rb") as f:
            magic = f.read(8)
    except OSError as exc:
        raise ParserInputError(f"HWP 파일을 읽을 수 없다: {source_path.name}") from exc

    if magic != _OLE2_MAGIC:
        return None, None

    from autoetl.parser.legacy.cfb import CfbFile, read_ole_summary_info

    with CfbFile(source_path) as cfb:
        fh = cfb.read_stream("FileHeader")
        if fh is None or len(fh) < 40 or not fh.startswith(b"HWP Document File"):
            raise ParserInputError(f"유효한 HWP 5.0 FileHeader가 아니다: {source_path.name}")
        flags = struct.unpack_from("<I", fh, 36)[0]
        if flags & 0x02:
            raise ParserInputError(f"암호화된 HWP 문서는 지원하지 않는다: {source_path.name}")
        if flags & 0x10:
            raise ParserInputError(
                f"DRM 보호가 적용된 HWP 문서는 지원하지 않는다: {source_path.name}"
            )
        streams_low = [name.lower() for name in cfb.list_streams()]
        if (flags & 0x04) or any(name.startswith("viewtext/") for name in streams_low):
            raise ParserInputError(
                f"배포용(ViewText) 제한 HWP 문서는 지원하지 않는다: {source_path.name}"
            )
        if not any(name.startswith("bodytext/section") for name in streams_low):
            raise ParserInputError(
                f"본문 섹션(BodyText/Section*)이 없는 HWP 문서다: {source_path.name}"
            )
        summary = read_ole_summary_info(
            cfb, ("\x05HwpSummaryInformation", "\x05SummaryInformation")
        )
        return summary.title, summary.author


def _extract_hwpx_opf_metadata(
    zf: zipfile.ZipFile, lower_names: dict[str, zipfile.ZipInfo]
) -> tuple[str | None, str | None]:
    """HWPX OPF(`Contents/content.hpf`)에서 제목·작성자(lastsaveby 폴백 포함)를 읽는다."""
    hpf_info = lower_names.get("contents/content.hpf")
    if hpf_info is None:
        return None, None
    try:
        root = ET.fromstring(zf.read(hpf_info))
    except (ET.ParseError, OSError, KeyError):
        return None, None

    title: str | None = None
    author: str | None = None
    lastsaveby: str | None = None
    for elem in root.iter():
        local = elem.tag.rsplit("}", 1)[-1].lower()
        if local == "title" and not title:
            txt = (elem.text or "").strip()
            if txt:
                title = txt
        elif local == "meta":
            mname = (elem.attrib.get("name") or "").strip().lower()
            raw_content = (elem.attrib.get("content") or "").strip()
            val = (
                raw_content
                if (raw_content and raw_content.lower() != "text")
                else (elem.text or "").strip()
            )
            if mname == "creator" and val and val.lower() != "text":
                author = author or val
            elif mname == "lastsaveby" and val and val.lower() != "text":
                lastsaveby = lastsaveby or val
    return title, author or lastsaveby


def _validate_hwpx_container(source_path: Path) -> tuple[str | None, str | None]:
    """HWPX(ZIP OWPML) 컨테이너의 암호화·손상·구형 HWP 혼용 검증 및 OPF 메타데이터 추출."""
    try:
        with source_path.open("rb") as f:
            magic = f.read(8)
    except OSError as exc:
        raise ParserInputError(f"HWPX 파일을 읽을 수 없다: {source_path.name}") from exc

    if magic == _OLE2_MAGIC:
        raise ParserInputError(
            f"암호화된 문서이거나 구형 바이너리 한글(.hwp, OLE2) 형식이다: {source_path.name}"
        )

    try:
        with zipfile.ZipFile(source_path, "r") as zf:
            infos = zf.infolist()
            lower_names = {info.filename.lower(): info for info in infos}
            if (
                any(info.flag_bits & 0x1 for info in infos)
                or "meta-inf/encryption.xml" in lower_names
            ):
                raise ParserInputError(f"암호화된 HWPX 문서는 지원하지 않는다: {source_path.name}")
            manifest_info = lower_names.get("meta-inf/manifest.xml")
            if manifest_info is not None:
                manifest_text = zf.read(manifest_info).decode("utf-8", errors="ignore").lower()
                if "encryption-data" in manifest_text:
                    raise ParserInputError(
                        f"암호화된 HWPX 문서는 지원하지 않는다: {source_path.name}"
                    )
            has_section = any(
                name.startswith("contents/section") and name.endswith(".xml")
                for name in lower_names
            )
            if not has_section:
                raise ParserInputError(
                    f"본문 섹션(Contents/section*.xml)이 없는 HWPX 문서다: {source_path.name}"
                )
            return _extract_hwpx_opf_metadata(zf, lower_names)
    except zipfile.BadZipFile as exc:
        raise ParserInputError(f"손상된 HWPX(ZIP) 파일이다: {source_path.name}") from exc
    except OSError as exc:
        raise ParserInputError(f"HWPX 파일을 열 수 없다: {source_path.name}") from exc


def _make_slot_bbox(index: int, total_slots: int) -> BBox:
    n = max(1, total_slots)
    y0_norm = clamp(0.08 + (index / n) * 0.84, 0.02, 0.96)
    y1_norm = clamp(y0_norm + max(0.03, 0.80 / n), y0_norm + 0.01, 0.98)
    return make_bbox(
        0.08 * _A4_WIDTH_PT,
        y0_norm * _A4_HEIGHT_PT,
        0.92 * _A4_WIDTH_PT,
        y1_norm * _A4_HEIGHT_PT,
        _A4_WIDTH_PT,
        _A4_HEIGHT_PT,
    )


def _convert_table_block(
    tbl: TableBlock,
    *,
    table_id: str,
    page: int,
    bbox: BBox,
) -> ExtractedTable:
    row_count = max(tbl.rows, max((c.row + c.row_span for c in tbl.cells), default=1))
    col_count = max(tbl.cols, max((c.col + c.col_span for c in tbl.cells), default=1))

    grid: list[list[str]] = [["" for _ in range(col_count)] for _ in range(row_count)]
    cells_out: list[TableCell] = []
    has_merged = False

    for c in tbl.cells:
        cell_text = _flatten_blocks_text(c.blocks)
        r_span = max(1, c.row_span)
        c_span = max(1, c.col_span)
        if r_span > 1 or c_span > 1:
            has_merged = True
        if 0 <= c.row < row_count and 0 <= c.col < col_count:
            grid[c.row][c.col] = cell_text
        is_hdr = c.role in ("column_header", "row_header") or (c.row == 0 and row_count >= 2)
        cells_out.append(
            TableCell(
                row=max(0, c.row),
                col=max(0, c.col),
                row_span=r_span,
                col_span=c_span,
                text=cell_text,
                is_header=is_hdr,
            )
        )

    # 표 우측에 모든 행·헤더가 완전히 빈 고스트 열이 붙은 경우 압축
    trimmed_cols = col_count
    while trimmed_cols > 1 and all(grid[r][trimmed_cols - 1] == "" for r in range(row_count)):
        trimmed_cols -= 1
    if trimmed_cols < col_count:
        col_count = trimmed_cols
        grid = [row[:col_count] for row in grid]
        cells_out = [c for c in cells_out if c.col < col_count]
        for idx, cell in enumerate(cells_out):
            if cell.col + cell.col_span > col_count:
                cells_out[idx] = cell.model_copy(update={"col_span": max(1, col_count - cell.col)})

    headers = list(grid[0]) if row_count >= 1 else []
    body_rows = [list(r) for r in grid[1:]] if row_count >= 2 else []

    header_row_flag_count = 0
    for r_idx in range(row_count):
        if any(c.row == r_idx and c.role in ("column_header", "row_header") for c in tbl.cells):
            header_row_flag_count = r_idx + 1
        elif header_row_flag_count > 0:
            break
    header_row_count = header_row_flag_count or (1 if row_count >= 2 else 0)

    caption = tbl.caption.strip() if tbl.caption and tbl.caption.strip() else None
    if caption is None and tbl.caption_block is not None:
        cap_txt = _flatten_blocks_text(tbl.caption_block.blocks)
        caption = cap_txt or None

    return ExtractedTable(
        table_id=table_id,
        page=page,
        bbox=bbox,
        strategy=ParseStrategy.RULED_TABLE,
        confidence=0.92,
        row_count=row_count,
        col_count=col_count,
        header_row_count=header_row_count,
        headers=headers,
        rows=body_rows,
        cells=cells_out,
        has_merged_cells=has_merged,
        caption=caption,
        reason="rhwp IR TableBlock 구조 추출",
    )


class RhwpPageRenderer:
    """`rhwp` 네이티브 래스터 엔진(`render_png`) 기반 HWP/HWPX 페이지 렌더러."""

    @property
    def name(self) -> str:
        return "rhwp"

    @property
    def available(self) -> bool:
        return True

    @staticmethod
    def render_document_region(
        doc: rhwp.Document,
        page_number: int,
        bbox: BBox | None,
        *,
        dpi: int = 150,
    ) -> EncodedImage | None:
        """이미 파싱된 `rhwp.Document`에서 지정 페이지(1-based)·영역을 PNG로 렌더링한다."""
        if page_number < 1 or page_number > doc.page_count:
            return None
        scale = max(0.5, dpi / 96.0)
        try:
            png_bytes = doc.render_png(page_number - 1, scale=scale, dpi=float(dpi))
        except (ValueError, OSError, RuntimeError):
            return None

        decoded = decode_png(png_bytes)
        if decoded is None:
            return None
        if bbox is None:
            return EncodedImage(
                data=png_bytes,
                extension=".png",
                width=decoded.width,
                height=decoded.height,
            )
        px0 = int(bbox.norm_x0 * decoded.width)
        py0 = int(bbox.norm_top * decoded.height)
        px1 = int(bbox.norm_x1 * decoded.width)
        py1 = int(bbox.norm_bottom * decoded.height)
        return crop_decoded_image(decoded, px0, py0, px1, py1)

    def render_region(
        self,
        source_path: Path,
        page_number: int,
        bbox: BBox | None,
        *,
        dpi: int = 150,
    ) -> EncodedImage | None:
        """파일 경로에서 `rhwp.Document`를 생성 스레드에서 열어 PNG/크롭 이미지를 생성한다."""
        if not source_path.is_file():
            return None
        try:
            doc = rhwp.parse(str(source_path))
        except (ValueError, OSError, RuntimeError):
            return None
        return self.render_document_region(doc, page_number, bbox, dpi=dpi)


class RhwpDocumentAdapter:
    """`rhwp` 코어를 사용하는 HWP / HWPX 공통 어댑터 기본 클래스."""

    def __init__(self, target_format: SourceFormat) -> None:
        self._format = target_format

    @property
    def format(self) -> SourceFormat:
        return self._format

    @property
    def extensions(self) -> tuple[str, ...]:
        return (".hwp",) if self._format is SourceFormat.HWP else (".hwpx",)

    def _open_rhwp_document(
        self, source_path: Path
    ) -> tuple[rhwp.Document, HwpDocument, tuple[str | None, str | None]]:
        if not source_path.is_file():
            raise ParserInputError(f"입력 파일이 존재하지 않는다: {source_path.name}")

        container_meta: tuple[str | None, str | None]
        if self._format is SourceFormat.HWP:
            container_meta = _validate_hwp_container(source_path)
        else:
            container_meta = _validate_hwpx_container(source_path)

        try:
            doc = rhwp.parse(str(source_path))
            ir = doc.to_ir()
        except ParserInputError:
            raise
        except (ValueError, OSError, RuntimeError) as exc:
            raise ParserInputError(
                f"{self._format.value.upper()} 문서를 파싱할 수 없다: {source_path.name}"
            ) from exc

        return doc, ir, container_meta

    def _build_document_and_plan(
        self,
        source_path: Path,
        *,
        options: ParseOptions,
        vision_adapter: VisionParserAdapter | None,
        page_renderer: PageRenderer | None,
        layout_detector: PageLayoutDetector | None,
    ) -> ParsedDocument:
        doc, ir, ole_meta = self._open_rhwp_document(source_path)
        file_bytes = source_path.read_bytes()
        sha256_hex = hashlib.sha256(file_bytes).hexdigest()

        blocks_by_page: dict[int, list[Block]] = defaultdict(list)
        max_grouped_page = 1
        for blk in ir.body:
            if blk.prov.page_range is not None:
                p_num = max(1, blk.prov.page_range[0] + 1)
            else:
                p_num = max(1, blk.prov.section_idx + 1)
            max_grouped_page = max(max_grouped_page, p_num)
            blocks_by_page[p_num].append(blk)

        total_pages = max(1, len(ir.sections), max_grouped_page)
        if options.pages is not None:
            invalid = [p for p in options.pages if p < 1 or p > total_pages]
            if invalid:
                raise ParserInputError(
                    f"선택한 페이지 번호가 문서 범위(1~{total_pages})를 벗어났다: {invalid}"
                )
            selected_pages = list(options.pages)
        else:
            selected_pages = list(range(1, total_pages + 1))

        active_detector = (
            layout_detector
            if (layout_detector is not None and not isinstance(layout_detector, NoopLayoutDetector))
            else None
        )
        active_vision = (
            vision_adapter
            if (vision_adapter is not None and not isinstance(vision_adapter, NoopVisionAdapter))
            else None
        )

        # 각주 / 미주 텍스트 미리 수집
        footnotes_by_page: dict[int, list[str]] = defaultdict(list)
        for fn in ir.furniture.footnotes:
            fn_page = max(1, fn.marker_prov.section_idx + 1)
            fn_text = _flatten_blocks_text(fn.blocks)
            if fn_text:
                footnotes_by_page[fn_page].append(f"[각주 {fn.number}] {fn_text}")

        endnote_texts: list[str] = []
        for en in ir.furniture.endnotes:
            en_text = _flatten_blocks_text(en.blocks)
            if en_text:
                endnote_texts.append(f"[미주 {en.number}] {en_text}")

        header_footer_items: list[DocumentBlock] = []
        for hf_idx, hf_blk in enumerate(
            [*ir.furniture.page_headers, *ir.furniture.page_footers], start=1
        ):
            hf_txt = _flatten_block_text(hf_blk)
            if hf_txt:
                hf_bbox = make_bbox(
                    0.08 * _A4_WIDTH_PT,
                    0.02 * _A4_HEIGHT_PT,
                    0.92 * _A4_WIDTH_PT,
                    0.06 * _A4_HEIGHT_PT,
                    _A4_WIDTH_PT,
                    _A4_HEIGHT_PT,
                )
                header_footer_items.append(
                    DocumentBlock(
                        block_id=f"hwp_hf_{hf_idx}",
                        page=1,
                        reading_order=hf_idx,
                        kind=ElementKind.HEADER_FOOTER,
                        strategy=ParseStrategy.SKIP_DECORATIVE,
                        bbox=hf_bbox,
                        text=hf_txt,
                    )
                )

        parsed_pages: list[ParsedPage] = []
        all_tables: list[ExtractedTable] = []
        all_visuals: list[VisualRegion] = []
        outline_entries: list[OutlineEntry] = []
        warnings: list[str] = []

        tbl_seq = 0
        vis_seq = 0
        category_counts: Counter[str] = Counter()
        strategy_counts: Counter[str] = Counter()
        vision_required_pages: list[int] = []

        for page_num in selected_pages:
            raw_blocks = blocks_by_page.get(page_num, [])
            page_doc_blocks: list[DocumentBlock] = []
            page_tables: list[ExtractedTable] = []
            page_visuals: list[VisualRegion] = []
            page_region_routes: list[RegionRoute] = []
            visual_raw_images: dict[str, tuple[bytes, str, int | None, int | None]] = {}

            slot_total = max(1, len(raw_blocks) + len(footnotes_by_page.get(page_num, [])))
            order_idx = 0
            usable_chars = 0
            line_cnt = 0
            formula_count = 0

            for blk in raw_blocks:
                if isinstance(blk, ParagraphBlock):
                    txt = _join_inlines(blk.inlines, blk.text)
                    if not txt:
                        continue
                    all_bold = bool(blk.inlines) and all(
                        r.bold for r in blk.inlines if r.text.strip()
                    )
                    is_heading = _looks_like_heading(txt, all_bold=all_bold, is_list=False, level=0)
                    kind = ElementKind.HEADING if is_heading else ElementKind.PARAGRAPH
                    h_level = _infer_heading_level(txt) if is_heading else None
                    if is_heading and h_level is not None:
                        outline_entries.append(
                            OutlineEntry(level=h_level, title=txt, page=page_num)
                        )
                    order_idx += 1
                    usable_chars += len(txt)
                    line_cnt += txt.count("\n") + 1
                    page_doc_blocks.append(
                        DocumentBlock(
                            block_id=f"p{page_num}_b{order_idx}",
                            page=page_num,
                            reading_order=order_idx,
                            kind=kind,
                            strategy=ParseStrategy.NATIVE_TEXT,
                            bbox=_make_slot_bbox(order_idx - 1, slot_total),
                            text=txt,
                            heading_level=h_level,
                            is_bold=all_bold,
                        )
                    )

                elif isinstance(blk, ListItemBlock):
                    body_txt = _join_inlines(blk.inlines, blk.text)
                    if not body_txt:
                        continue
                    marker = blk.marker.strip()
                    if marker and not _EXISTING_LIST_PREFIX_RE.match(body_txt):
                        full_txt = f"{marker} {body_txt}"
                    else:
                        full_txt = body_txt
                    all_bold = bool(blk.inlines) and all(
                        r.bold for r in blk.inlines if r.text.strip()
                    )
                    is_heading = _looks_like_heading(
                        body_txt, all_bold=all_bold, is_list=True, level=blk.level
                    )
                    kind = ElementKind.HEADING if is_heading else ElementKind.LIST_ITEM
                    h_level = (
                        _infer_heading_level(full_txt, default_level=blk.level + 1)
                        if is_heading
                        else None
                    )
                    if is_heading and h_level is not None:
                        outline_entries.append(
                            OutlineEntry(level=h_level, title=full_txt, page=page_num)
                        )
                    order_idx += 1
                    usable_chars += len(full_txt)
                    line_cnt += 1
                    page_doc_blocks.append(
                        DocumentBlock(
                            block_id=f"p{page_num}_b{order_idx}",
                            page=page_num,
                            reading_order=order_idx,
                            kind=kind,
                            strategy=ParseStrategy.NATIVE_TEXT,
                            bbox=_make_slot_bbox(order_idx - 1, slot_total),
                            text=full_txt,
                            heading_level=h_level,
                            is_bold=all_bold,
                        )
                    )

                elif isinstance(blk, FormulaBlock):
                    f_txt = (blk.text_alt or blk.script).strip()
                    if not f_txt:
                        continue
                    formula_count += 1
                    order_idx += 1
                    usable_chars += len(f_txt)
                    line_cnt += 1
                    page_doc_blocks.append(
                        DocumentBlock(
                            block_id=f"p{page_num}_b{order_idx}",
                            page=page_num,
                            reading_order=order_idx,
                            kind=ElementKind.FORMULA_OR_CODE,
                            strategy=ParseStrategy.NATIVE_TEXT,
                            bbox=_make_slot_bbox(order_idx - 1, slot_total),
                            text=f_txt,
                        )
                    )

                elif isinstance(blk, TableBlock):
                    tbl_seq += 1
                    order_idx += 1
                    tbl_id = f"tbl_{page_num}_{tbl_seq}"
                    tbl_bbox = _make_slot_bbox(order_idx - 1, slot_total)
                    ext_tbl = _convert_table_block(
                        blk, table_id=tbl_id, page=page_num, bbox=tbl_bbox
                    )
                    page_tables.append(ext_tbl)
                    all_tables.append(ext_tbl)
                    tbl_txt = " ".join(
                        [
                            *(ext_tbl.headers or []),
                            *[cell for row in ext_tbl.rows for cell in row],
                        ]
                    ).strip()
                    usable_chars += len(tbl_txt)
                    line_cnt += ext_tbl.row_count
                    page_doc_blocks.append(
                        DocumentBlock(
                            block_id=f"p{page_num}_b{order_idx}",
                            page=page_num,
                            reading_order=order_idx,
                            kind=ElementKind.TABLE,
                            strategy=ParseStrategy.RULED_TABLE,
                            bbox=tbl_bbox,
                            text=ext_tbl.caption or "",
                            table_id=tbl_id,
                            confidence=ext_tbl.confidence,
                        )
                    )
                    page_region_routes.append(
                        RegionRoute(
                            region_id=tbl_id,
                            element_kind=ElementKind.TABLE,
                            strategy=ParseStrategy.RULED_TABLE,
                            bbox=tbl_bbox,
                            confidence=ext_tbl.confidence,
                            reason="rhwp TableBlock 네이티브 격자 표",
                            requires_vision=False,
                        )
                    )

                elif isinstance(blk, PictureBlock):
                    vis_seq += 1
                    order_idx += 1
                    reg_id = f"vis_{page_num}_{vis_seq}"
                    vis_bbox = _make_slot_bbox(order_idx - 1, slot_total)
                    cap_txt: str | None = (
                        blk.description.strip()
                        if blk.description and blk.description.strip()
                        else None
                    )
                    if cap_txt is None and blk.caption is not None:
                        flat_cap = _flatten_blocks_text(blk.caption.blocks)
                        cap_txt = flat_cap or None

                    raw_bytes: bytes | None = None
                    img_ext = ".png"
                    px_w: int | None = None
                    px_h: int | None = None
                    if blk.image is not None:
                        img_ext = _mime_to_extension(blk.image.mime_type)
                        try:
                            raw_bytes = doc.bytes_for_image(blk)
                        except ValueError:
                            raw_bytes = None
                    if raw_bytes:
                        pw, ph = probe_image_dimensions(raw_bytes, img_ext)
                        px_w, px_h = pw, ph
                        visual_raw_images[reg_id] = (raw_bytes, img_ext, px_w, px_h)

                    vis_reg = VisualRegion(
                        region_id=reg_id,
                        page=page_num,
                        element_kind=ElementKind.FIGURE_IMAGE,
                        recommended_strategy=ParseStrategy.EMBEDDED_IMAGE_OCR,
                        bbox=vis_bbox,
                        reason="rhwp PictureBlock 내장 이미지",
                        confidence=0.90,
                        priority="medium",
                        crop_status=(
                            CropStatus.EMBEDDED_EXTRACTED if raw_bytes else CropStatus.SKIPPED
                        ),
                        image_width_px=px_w,
                        image_height_px=px_h,
                        caption=cap_txt,
                    )
                    page_visuals.append(vis_reg)
                    page_doc_blocks.append(
                        DocumentBlock(
                            block_id=f"{reg_id}_blk",
                            page=page_num,
                            reading_order=order_idx,
                            kind=ElementKind.FIGURE_IMAGE,
                            strategy=ParseStrategy.EMBEDDED_IMAGE_OCR,
                            bbox=vis_bbox,
                            text=cap_txt or "",
                            visual_region_id=reg_id,
                            confidence=0.90,
                        )
                    )
                    page_region_routes.append(
                        RegionRoute(
                            region_id=reg_id,
                            element_kind=ElementKind.FIGURE_IMAGE,
                            strategy=ParseStrategy.EMBEDDED_IMAGE_OCR,
                            bbox=vis_bbox,
                            confidence=0.90,
                            reason="내장 그림·차트 이미지(선택 OCR/VLM 대상)",
                            requires_vision=True,
                        )
                    )

                elif isinstance(blk, FieldBlock):
                    if blk.field_kind == "memo":
                        if options.include_comments and blk.cached_value:
                            txt = f"[문서 주석] {blk.cached_value.strip()}"
                        else:
                            continue
                    elif blk.field_kind == "hyperlink" and (
                        blk.raw_instruction or blk.cached_value
                    ):
                        val = (blk.cached_value or "").strip()
                        url = (blk.raw_instruction or "").strip()
                        txt = (
                            f"{val} ({url})".strip()
                            if val and url and url not in val
                            else (val or url)
                        )
                    else:
                        txt = (blk.cached_value or "").strip()
                    if not txt:
                        continue
                    order_idx += 1
                    usable_chars += len(txt)
                    line_cnt += 1
                    page_doc_blocks.append(
                        DocumentBlock(
                            block_id=f"p{page_num}_b{order_idx}",
                            page=page_num,
                            reading_order=order_idx,
                            kind=ElementKind.PARAGRAPH,
                            strategy=ParseStrategy.NATIVE_TEXT,
                            bbox=_make_slot_bbox(order_idx - 1, slot_total),
                            text=txt,
                        )
                    )

                elif isinstance(blk, TocBlock):
                    toc_lines = [e.text.strip() for e in blk.entries if e.text.strip()]
                    if toc_lines:
                        txt = "\n".join(toc_lines)
                        order_idx += 1
                        usable_chars += len(txt)
                        line_cnt += len(toc_lines)
                        page_doc_blocks.append(
                            DocumentBlock(
                                block_id=f"p{page_num}_b{order_idx}",
                                page=page_num,
                                reading_order=order_idx,
                                kind=ElementKind.PARAGRAPH,
                                strategy=ParseStrategy.NATIVE_TEXT,
                                bbox=_make_slot_bbox(order_idx - 1, slot_total),
                                text=txt,
                            )
                        )

            for fn_txt in footnotes_by_page.get(page_num, []):
                order_idx += 1
                usable_chars += len(fn_txt)
                line_cnt += 1
                page_doc_blocks.append(
                    DocumentBlock(
                        block_id=f"p{page_num}_fn_{order_idx}",
                        page=page_num,
                        reading_order=order_idx,
                        kind=ElementKind.PARAGRAPH,
                        strategy=ParseStrategy.NATIVE_TEXT,
                        bbox=_make_slot_bbox(order_idx - 1, slot_total),
                        text=fn_txt,
                    )
                )

            if page_num == selected_pages[-1]:
                for en_txt in endnote_texts:
                    order_idx += 1
                    usable_chars += len(en_txt)
                    line_cnt += 1
                    page_doc_blocks.append(
                        DocumentBlock(
                            block_id=f"p{page_num}_en_{order_idx}",
                            page=page_num,
                            reading_order=order_idx,
                            kind=ElementKind.PARAGRAPH,
                            strategy=ParseStrategy.NATIVE_TEXT,
                            bbox=_make_slot_bbox(order_idx - 1, slot_total),
                            text=en_txt,
                        )
                    )

            # 스캔 이미지 페이지 판별 및 레이아웃 객체 탐지 + rhwp.render_png 연동
            large_embedded = [
                (rid, info)
                for rid, info in visual_raw_images.items()
                if (info[2] or 0) >= 200 and (info[3] or 0) >= 200
            ]
            is_scan_page = usable_chars < 40 and len(page_tables) == 0 and len(large_embedded) > 0
            layout_extra_reasons: list[str] = []

            if active_detector is not None and (
                is_scan_page or large_embedded or options.layout_detect_all_pages
            ):
                det_bytes: bytes | None = None
                det_ext = ".png"
                det_w = 0
                det_h = 0
                target_scan_id: str | None = None
                if large_embedded:
                    target_scan_id, (det_bytes, det_ext, pw, ph) = large_embedded[0]
                    det_w = pw or 0
                    det_h = ph or 0
                else:
                    render_img = (
                        page_renderer.render_region(
                            source_path, page_num, None, dpi=options.render_dpi
                        )
                        if (page_renderer is not None and page_renderer.available)
                        else RhwpPageRenderer.render_document_region(
                            doc, page_num, None, dpi=options.render_dpi
                        )
                    )
                    if render_img is not None:
                        det_bytes = render_img.data
                        det_ext = render_img.extension
                        det_w = render_img.width or 0
                        det_h = render_img.height or 0

                if det_bytes is not None and det_w > 0 and det_h > 0:
                    det_input = LayoutDetectionInput(
                        page=page_num,
                        image_bytes=det_bytes,
                        image_extension=det_ext,
                        image_width_px=det_w,
                        image_height_px=det_h,
                        page_width_pt=_A4_WIDTH_PT,
                        page_height_pt=_A4_HEIGHT_PT,
                    )
                    boxes = active_detector.detect(det_input)
                    region_pairs = (
                        boxes_to_visual_regions(
                            boxes,
                            page=page_num,
                            page_width_pt=_A4_WIDTH_PT,
                            page_height_pt=_A4_HEIGHT_PT,
                            image_width_px=det_w,
                            image_height_px=det_h,
                            detector_name=active_detector.name,
                            skip_header_footer=True,
                            start_index=1,
                        )
                        if boxes
                        else []
                    )
                    if region_pairs:
                        decoded_page = decode_png(det_bytes) if det_ext == ".png" else None
                        new_vis: list[VisualRegion] = []
                        new_raw_map: dict[str, tuple[bytes, str, int | None, int | None]] = {}
                        if target_scan_id is not None:
                            for existing_v in page_visuals:
                                if existing_v.region_id != target_scan_id:
                                    new_vis.append(existing_v)
                            for existing_id, existing_img in visual_raw_images.items():
                                if existing_id != target_scan_id:
                                    new_raw_map[existing_id] = existing_img

                        start_seq = len(new_vis)
                        added_vis: list[VisualRegion] = []
                        for offset, (reg, (x0, y0, x1, y1)) in enumerate(region_pairs, start=1):
                            reg = reg.model_copy(
                                update={"region_id": f"vis_{page_num}_{start_seq + offset}"}
                            )
                            if decoded_page is not None:
                                cropped = crop_decoded_image(decoded_page, x0, y0, x1, y1)
                                if cropped is not None:
                                    new_raw_map[reg.region_id] = (
                                        cropped.data,
                                        cropped.extension,
                                        cropped.width,
                                        cropped.height,
                                    )
                                    reg = reg.model_copy(
                                        update={
                                            "crop_status": CropStatus.SAVED,
                                            "image_width_px": cropped.width,
                                            "image_height_px": cropped.height,
                                        }
                                    )
                            new_vis.append(reg)
                            added_vis.append(reg)

                        page_visuals = new_vis
                        visual_raw_images = new_raw_map
                        non_vis_blocks = [
                            b
                            for b in page_doc_blocks
                            if b.visual_region_id is None
                            or (target_scan_id is not None and b.visual_region_id != target_scan_id)
                        ]
                        new_vis_blocks = [
                            DocumentBlock(
                                block_id=f"{v.region_id}_blk",
                                page=page_num,
                                reading_order=0,
                                kind=v.element_kind,
                                strategy=v.recommended_strategy,
                                bbox=v.bbox,
                                text=v.caption or "",
                                visual_region_id=v.region_id,
                                confidence=v.confidence,
                            )
                            for v in added_vis
                        ]
                        page_doc_blocks = [
                            b.model_copy(update={"reading_order": idx})
                            for idx, b in enumerate([*non_vis_blocks, *new_vis_blocks], 1)
                        ]
                        non_vis_routes = [
                            r
                            for r in page_region_routes
                            if not r.requires_vision
                            or (target_scan_id is not None and r.region_id != target_scan_id)
                        ]
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
                            for v in added_vis
                        ]
                        page_region_routes = [*non_vis_routes, *vis_routes]
                        layout_extra_reasons.append(
                            f"레이아웃 탐지({active_detector.name})로 "
                            f"{len(added_vis)}개 영역 세분화"
                        )

            # 크롭 저장 및 선택 VLM/OCR 어댑터 실행
            final_visuals: list[VisualRegion] = []
            vis_map: dict[str, VisualRegion] = {}
            for vis in page_visuals:
                raw_entry = visual_raw_images.get(vis.region_id)
                img_bytes = raw_entry[0] if raw_entry is not None else None
                img_ext = raw_entry[1] if raw_entry is not None else ".png"
                crop_path_str = vis.crop_path
                crop_status = vis.crop_status

                if img_bytes is None and (vis.crop_status == CropStatus.SKIPPED):
                    rendered = (
                        page_renderer.render_region(
                            source_path, page_num, vis.bbox, dpi=options.render_dpi
                        )
                        if (page_renderer is not None and page_renderer.available)
                        else RhwpPageRenderer.render_document_region(
                            doc, page_num, vis.bbox, dpi=options.render_dpi
                        )
                    )
                    if rendered is not None:
                        img_bytes = rendered.data
                        img_ext = rendered.extension
                        crop_status = CropStatus.SAVED

                if options.extract_crops and options.crop_dir is not None and img_bytes is not None:
                    saved_path = save_encoded_image(
                        EncodedImage(
                            data=img_bytes,
                            extension=img_ext,
                            width=vis.image_width_px,
                            height=vis.image_height_px,
                        ),
                        options.crop_dir,
                        vis.region_id,
                    )
                    crop_path_str = saved_path.as_posix()
                    crop_status = CropStatus.SAVED

                updated_vis = vis.model_copy(
                    update={"crop_status": crop_status, "crop_path": crop_path_str}
                )

                if active_vision is not None:
                    ctx_before, ctx_after = _find_surrounding_slide_context(vis, page_doc_blocks)
                    req = VisionRequest(
                        region_id=vis.region_id,
                        page=page_num,
                        element_kind=vis.element_kind,
                        recommended_strategy=vis.recommended_strategy,
                        bbox=vis.bbox,
                        reason=vis.reason,
                        image_bytes=img_bytes,
                        image_extension=img_ext,
                        crop_path=crop_path_str,
                        embedded_text_hints=tuple(vis.embedded_text_hints),
                        caption=vis.caption,
                        context_before=ctx_before,
                        context_after=ctx_after,
                    )
                    try:
                        res = active_vision.parse_region(req)
                    except Exception:
                        updated_vis = updated_vis.model_copy(
                            update={
                                "vision_status": VisionStatus.FAILED,
                                "vision_adapter": active_vision.name,
                                "vision_metadata": {"vision_error": "adapter_exception"},
                            }
                        )
                    else:
                        if res.succeeded:
                            updated_vis = updated_vis.model_copy(
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
                            updated_vis = updated_vis.model_copy(
                                update={
                                    "vision_status": VisionStatus.FAILED,
                                    "vision_adapter": res.adapter_name,
                                    "vision_metadata": meta,
                                }
                            )

                final_visuals.append(updated_vis)
                vis_map[updated_vis.region_id] = updated_vis

            if vis_map:
                page_doc_blocks = [
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
                    for blk in page_doc_blocks
                ]

            all_visuals.extend(final_visuals)

            page_area = _A4_WIDTH_PT * _A4_HEIGHT_PT
            tbl_area = sum(t.bbox.area for t in page_tables)
            vis_area = _union_area_bboxes(final_visuals)
            if is_scan_page:
                page_cat = PageCategory.SCANNED_IMAGE
                primary_strat = (
                    ParseStrategy.VLM_REGION
                    if any(
                        v.recommended_strategy == ParseStrategy.VLM_REGION for v in final_visuals
                    )
                    else ParseStrategy.EMBEDDED_IMAGE_OCR
                )
                requires_vis = True
                reasons = [
                    f"rhwp 파싱 결과 텍스트({usable_chars}자) 적음 + 대형 스캔 이미지",
                    *layout_extra_reasons,
                ]
            elif page_tables and usable_chars > 0 and tbl_area / page_area >= 0.25:
                page_cat = PageCategory.TABLE_HEAVY
                primary_strat = ParseStrategy.RULED_TABLE
                requires_vis = bool(final_visuals)
                reasons = [
                    f"rhwp 네이티브 표 {len(page_tables)}개 중심 페이지",
                    *layout_extra_reasons,
                ]
            elif final_visuals and usable_chars > 0:
                page_cat = PageCategory.HYBRID_TEXT_IMAGE
                primary_strat = ParseStrategy.NATIVE_TEXT
                requires_vis = True
                reasons = [
                    f"rhwp 본문 + 시각 영역 {len(final_visuals)}개 혼재 페이지",
                    *layout_extra_reasons,
                ]
            elif usable_chars == 0 and not page_tables and not final_visuals:
                page_cat = PageCategory.BLANK
                primary_strat = ParseStrategy.SKIP_DECORATIVE
                requires_vis = False
                reasons = ["비어 있는 섹션/페이지", *layout_extra_reasons]
            else:
                page_cat = PageCategory.DIGITAL_TEXT
                primary_strat = ParseStrategy.NATIVE_TEXT
                requires_vis = False
                reasons = [
                    f"rhwp 네이티브 본문 추출 (수식 {formula_count}건)"
                    if formula_count
                    else "rhwp 네이티브 본문 추출",
                    *layout_extra_reasons,
                ]

            if requires_vis:
                vision_required_pages.append(page_num)
            category_counts[page_cat.value] += 1
            strategy_counts[primary_strat.value] += 1
            sec_strats = list(
                dict.fromkeys(r.strategy for r in page_region_routes if r.strategy != primary_strat)
            )

            metrics = PageMetrics(
                page=page_num,
                width_pt=_A4_WIDTH_PT,
                height_pt=_A4_HEIGHT_PT,
                aspect_ratio=round(_A4_WIDTH_PT / _A4_HEIGHT_PT, 4),
                char_count=usable_chars,
                usable_char_count=usable_chars,
                usable_text_ratio=1.0 if usable_chars > 0 else 0.0,
                line_count=max(1, line_cnt) if usable_chars > 0 else 0,
                column_count=1,
                image_count=len(final_visuals),
                non_decorative_image_count=len(final_visuals),
                image_coverage=round(clamp(vis_area / page_area, 0.0, 1.0), 4),
                table_count=len(page_tables),
                table_area_ratio=round(clamp(tbl_area / page_area, 0.0, 1.0), 4),
                visual_region_count=len(final_visuals),
                visual_area_ratio=round(clamp(vis_area / page_area, 0.0, 1.0), 4),
                header_footer_line_count=len(header_footer_items) if page_num == 1 else 0,
            )
            route = PageRouteDecision(
                page=page_num,
                category=page_cat,
                primary_strategy=primary_strat,
                secondary_strategies=sec_strats,
                confidence=0.92,
                reasons=reasons,
                metrics=metrics,
                regions=page_region_routes,
                requires_vision=requires_vis,
            )
            parsed_pages.append(
                ParsedPage(
                    page=page_num,
                    route=route,
                    blocks=page_doc_blocks,
                    header_footer_blocks=list(header_footer_items) if page_num == 1 else [],
                    tables=page_tables,
                    visual_regions=final_visuals,
                )
            )

        title = (ir.metadata.title.strip() if ir.metadata.title else None) or ole_meta[0]
        author = (ir.metadata.author.strip() if ir.metadata.author else None) or ole_meta[1]
        src_name = os.fspath(source_path.name) if source_path.name else source_path.as_posix()

        doc_meta = DocumentMetadata(
            source_path=src_name,
            format=self._format,
            file_size_bytes=len(file_bytes),
            sha256=sha256_hex,
            page_count=total_pages,
            title=title or None,
            author=author or None,
            creator=f"rhwp/{rhwp.rhwp_core_version()}",
            producer=f"rhwp-python/{rhwp.version()}",
            has_outline=bool(outline_entries),
            is_encrypted=False,
        )
        summary = RoutingSummary(
            total_pages=total_pages,
            analyzed_pages=len(parsed_pages),
            category_counts=dict(category_counts),
            strategy_counts=dict(strategy_counts),
            table_count=len(all_tables),
            visual_region_count=len(all_visuals),
            vision_required_pages=vision_required_pages,
            multi_column_pages=[],
            running_header_detected=bool(ir.furniture.page_headers),
            running_footer_detected=bool(ir.furniture.page_footers),
        )
        plan = RoutingPlan(
            document=doc_meta,
            outline=outline_entries,
            summary=summary,
            pages=[p.route for p in parsed_pages],
        )
        return ParsedDocument(
            document=doc_meta,
            routing_plan=plan,
            pages=parsed_pages,
            tables=all_tables,
            visual_regions=all_visuals,
            warnings=warnings,
        )

    def inspect(
        self,
        source_path: Path,
        *,
        options: ParseOptions,
        layout_detector: PageLayoutDetector | None = None,
        page_renderer: PageRenderer | None = None,
    ) -> RoutingPlan:
        """HWP/HWPX 문서의 구조 유형과 페이지·요소별 파싱 전략 계획(`RoutingPlan`)을 진단한다."""
        inspect_opts = ParseOptions(
            pages=options.pages,
            extract_crops=False,
            crop_dir=None,
            render_dpi=options.render_dpi,
            detect_borderless_tables=options.detect_borderless_tables,
            detect_running_headers_footers=options.detect_running_headers_footers,
            include_header_footer_in_markdown=options.include_header_footer_in_markdown,
            layout_detect_all_pages=options.layout_detect_all_pages,
            include_speaker_notes=options.include_speaker_notes,
            include_comments=options.include_comments,
        )
        parsed = self._build_document_and_plan(
            source_path,
            options=inspect_opts,
            vision_adapter=None,
            page_renderer=page_renderer,
            layout_detector=layout_detector,
        )
        return parsed.routing_plan

    def parse(
        self,
        source_path: Path,
        *,
        options: ParseOptions,
        vision_adapter: VisionParserAdapter | None = None,
        page_renderer: PageRenderer | None = None,
        layout_detector: PageLayoutDetector | None = None,
    ) -> ParsedDocument:
        """HWP/HWPX 문서를 `rhwp`로 파싱하고 AutoETL `ParsedDocument` IR로 반환한다."""
        return self._build_document_and_plan(
            source_path,
            options=options,
            vision_adapter=vision_adapter,
            page_renderer=page_renderer,
            layout_detector=layout_detector,
        )


class HwpDocumentAdapter(RhwpDocumentAdapter):
    """`rhwp` 기반 구형 바이너리 한글 5.0(`.hwp`) 문서 어댑터."""

    def __init__(self) -> None:
        super().__init__(SourceFormat.HWP)


class HwpxDocumentAdapter(RhwpDocumentAdapter):
    """`rhwp` 기반 개방형 한글 OWPML(`.hwpx`) 문서 어댑터."""

    def __init__(self) -> None:
        super().__init__(SourceFormat.HWPX)


__all__ = [
    "HwpDocumentAdapter",
    "HwpxDocumentAdapter",
    "RhwpDocumentAdapter",
    "RhwpPageRenderer",
]
