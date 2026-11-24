"""구형 바이너리 MS Word 97-2003(`.doc`, OLE2 CFB) 하이브리드 파서 및 어댑터.

2계층 전략으로 동작한다:
1. Tier-1 (선택적 LibreOffice 승격): 시스템 PATH에 `soffice`/`libreoffice`가 있고
   `use_libreoffice=True`이면 `.doc`을 임시 `.docx`로 변환해 `DocxDocumentAdapter`로 파싱한다.
2. Tier-2 (무의존성 순수 Python MS-DOC 폴백): LibreOffice가 없거나 변환이 실패하면
   `CfbFile`로 `WordDocument`·`0Table`/`1Table`의 FIB/`Clx`(`PlcPcd` 피스 테이블)를 해석해
   본문·표(`\x07` 경계)·머리글/바닥글·각주/미주·주석·하이퍼링크·내장 이미지를 추출한다.
"""

from __future__ import annotations

import re
import struct
import tempfile
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
    ParseStrategy,
    RoutingPlan,
    SourceFormat,
    TableCell,
    VisualRegion,
)
from autoetl.parser.docx.adapter import DocxDocumentAdapter, _apply_layout_detection_to_docx_page
from autoetl.parser.docx.elements import (
    RawDocxBodyItem,
    RawDocxPage,
    RawDocxParagraph,
    RawDocxTableItem,
)
from autoetl.parser.docx.ooxml import DocxCommentEntry
from autoetl.parser.docx.router import (
    AnalyzedDocxPage,
    analyze_docx_page,
    build_docx_routing_plan,
    estimate_docx_body_font_size,
    find_repeating_docx_logos,
)
from autoetl.parser.image_io import DecodedImage, decode_png
from autoetl.parser.legacy.cfb import (
    CfbFile,
    OleSummaryInfo,
    extract_officeart_blips,
    read_ole_summary_info,
)
from autoetl.parser.legacy.converter import convert_legacy_office_document, find_libreoffice_binary
from autoetl.parser.pptx.adapter import (
    _extract_crop_for_pptx_visual,
    _find_surrounding_slide_context,
    _run_vision_on_pptx_region,
)
from autoetl.parser.pptx.elements import RawPptxImage
from autoetl.parser.pptx.ooxml import make_bbox

_WORD_IDENT = 0xA5EC
_DEFAULT_PAGE_W = 595.28
_DEFAULT_PAGE_H = 841.88
_HYPERLINK_FIELD_RE = re.compile(r'HYPERLINK\s+(?:\\[a-zA-Z]\s+)*"([^"]+)"', re.IGNORECASE)
_HEADING_PREFIX_RE = re.compile(
    r"^(?:제\s*\d+\s*[장절조편]|\d+(?:\.\d+){0,2}\.?\s+\S|[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]\.\s+\S)"
)
_BULLET_PREFIXES = ("•", "·", "◦", "▪", "▫", "‣", "-", "\u2013", "—", "※", "○", "●", "■", "□")


def _clean_doc_fields(text: str) -> str:
    """MS-DOC 필드 제어문자(`\\x13`/`\\x14`/`\\x15`)를 해제해 표시 텍스트·URL만 남긴다."""
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\x13":
            depth = 1
            sep_pos = -1
            j = i + 1
            while j < n and depth > 0:
                if text[j] == "\x13":
                    depth += 1
                elif text[j] == "\x15":
                    depth -= 1
                    if depth == 0:
                        break
                elif text[j] == "\x14" and depth == 1 and sep_pos < 0:
                    sep_pos = j
                j += 1
            end_pos = j
            code_part = text[i + 1 : sep_pos] if sep_pos > i else text[i + 1 : end_pos]
            disp_part = text[sep_pos + 1 : end_pos] if sep_pos > i else ""
            cleaned_disp = _clean_doc_fields(disp_part).strip() if disp_part else ""
            m_url = _HYPERLINK_FIELD_RE.search(code_part)
            if m_url:
                url = m_url.group(1).strip()
                if cleaned_disp and url not in cleaned_disp:
                    out.append(f"{cleaned_disp} ({url})")
                else:
                    out.append(cleaned_disp or url)
            elif cleaned_disp:
                out.append(cleaned_disp)
            i = end_pos + 1
        elif ch in (
            "\x14",
            "\x15",
            "\x01",
            "\x02",
            "\x03",
            "\x04",
            "\x05",
            "\x08",
            "\x16",
            "\x1e",
            "\x1f",
        ):
            i += 1
        elif ch == "\x0b":
            out.append("\n")
            i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _decode_piece_table(word_doc: bytes, table_stm: bytes, fc_clx: int, lcb_clx: int) -> str:
    """`Table` 스트림의 `Clx` → `Pcdt`(`PlcPcd`) 피스 테이블로 전체 문서 문자 스트림을 복원한다."""
    if lcb_clx <= 0 or fc_clx + lcb_clx > len(table_stm):
        return ""
    clx = table_stm[fc_clx : fc_clx + lcb_clx]
    pos = 0
    clx_len = len(clx)

    # Prc(0x01) 건너뛰기 -> Pcdt(0x02) 탐색
    while pos < clx_len:
        clxt = clx[pos]
        if clxt == 0x01:
            if pos + 3 > clx_len:
                return ""
            cb_grpprl = struct.unpack_from("<H", clx, pos + 1)[0]
            pos += 3 + cb_grpprl
        elif clxt == 0x02:
            pos += 1
            break
        else:
            return ""

    if pos + 4 > clx_len:
        return ""
    pcdt_len = struct.unpack_from("<I", clx, pos)[0]
    pos += 4
    plc_pcd = clx[pos : pos + pcdt_len]
    if len(plc_pcd) < 16:
        return ""

    n_pieces = (len(plc_pcd) - 4) // 12
    if n_pieces <= 0:
        return ""

    cps = struct.unpack_from(f"<{n_pieces + 1}I", plc_pcd, 0)
    pcd_base = (n_pieces + 1) * 4

    chunks: list[str] = []
    wd_len = len(word_doc)
    for i in range(n_pieces):
        cp_len = cps[i + 1] - cps[i]
        if cp_len <= 0:
            continue
        pcd_off = pcd_base + i * 8
        if pcd_off + 6 > len(plc_pcd):
            break
        fc_raw = struct.unpack_from("<I", plc_pcd, pcd_off + 2)[0]
        is_compressed = bool(fc_raw & 0x40000000)
        fc_val = fc_raw & 0x3FFFFFFF
        if is_compressed:
            byte_off = fc_val // 2
            byte_len = cp_len
            if byte_off >= wd_len:
                continue
            raw_slice = word_doc[byte_off : min(wd_len, byte_off + byte_len)]
            chunks.append(raw_slice.decode("cp1252", errors="replace"))
        else:
            byte_off = fc_val
            byte_len = cp_len * 2
            if byte_off >= wd_len:
                continue
            raw_slice = word_doc[byte_off : min(wd_len, byte_off + byte_len)]
            if len(raw_slice) % 2 == 1:
                raw_slice = raw_slice[:-1]
            chunks.append(raw_slice.decode("utf-16le", errors="replace"))

    return "".join(chunks)


def _split_nonempty_chunks(segment: str) -> list[str]:
    cleaned = _clean_doc_fields(segment)
    parts: list[str] = []
    for raw_ln in re.split(r"[\r\x07\x0c]+", cleaned):
        ln = " ".join(raw_ln.replace("\t", " ").split()).strip()
        if ln and ln not in parts:
            parts.append(ln)
    return parts


def _build_doc_table_item(
    rows_raw: list[list[str]],
    seq_counter: list[int],
) -> RawDocxTableItem | None:
    """`\x07` 셀 구분자로 수집한 MS-DOC 원시 행 목록을 `RawDocxTableItem`으로 변환한다."""
    if not rows_raw:
        return None
    n_cols = max((len(r) for r in rows_raw), default=0)
    if n_cols <= 0:
        return None

    grid = [list(r) + [""] * (n_cols - len(r)) for r in rows_raw]
    # 100% 빈 열/행 압축
    non_empty_cols = [c for c in range(n_cols) if any(row[c].strip() for row in grid)]
    if not non_empty_cols:
        return None
    grid = [[row[c] for c in non_empty_cols] for row in grid]
    n_cols = len(non_empty_cols)
    grid = [row for row in grid if any(cell.strip() for cell in row)]
    if not grid:
        return None
    n_rows = len(grid)

    cells: list[TableCell] = [
        TableCell(
            row=r_i,
            col=c_i,
            row_span=1,
            col_span=1,
            text=grid[r_i][c_i],
            is_header=(r_i == 0),
        )
        for r_i in range(n_rows)
        for c_i in range(n_cols)
    ]

    headers = [grid[0][c] if grid[0][c] else f"col_{c + 1}" for c in range(n_cols)]
    data_rows = grid[1:] if n_rows >= 2 else []
    raw_w = min(_DEFAULT_PAGE_W * 0.88, max(140.0, n_cols * 95.0))
    raw_h = min(_DEFAULT_PAGE_H * 0.78, max(36.0, n_rows * 22.0))
    seq_counter[0] += 1

    extracted = ExtractedTable(
        table_id=f"p001_t{seq_counter[0]:02d}",
        page=1,
        bbox=make_bbox(0.0, 0.0, raw_w, raw_h, _DEFAULT_PAGE_W, _DEFAULT_PAGE_H),
        strategy=ParseStrategy.RULED_TABLE,
        confidence=0.90,
        row_count=n_rows,
        col_count=n_cols,
        header_row_count=1,
        headers=headers,
        rows=data_rows,
        cells=cells,
        has_merged_cells=False,
        caption=None,
        reason=f"MS-DOC 네이티브 바이너리 표 직접 추출({n_rows}x{n_cols})",
    )
    return RawDocxTableItem(
        table=extracted,
        raw_width_pt=raw_w,
        raw_height_pt=raw_h,
    )


def _parse_doc_body_stream(
    body_text: str,
    seq_counter: list[int],
) -> list[tuple[list[RawDocxBodyItem], bool]]:
    """MS-DOC 본문 문자열에서 페이지 나누기(`\x0c`), 표(`\x07`), 문단(`\r`)을 분리한다."""
    # 페이지 나누기(\x0c) 기준 세그먼트 분할
    raw_segments = body_text.split("\x0c")
    results: list[tuple[list[RawDocxBodyItem], bool]] = []

    for seg_i, seg in enumerate(raw_segments):
        items: list[RawDocxBodyItem] = []
        table_rows: list[list[str]] = []
        current_row: list[str] = []
        buf: list[str] = []

        def _flush_table(target: list[RawDocxBodyItem] = items) -> None:
            nonlocal table_rows, current_row
            if current_row:
                table_rows.append(current_row)
                current_row = []
            if table_rows:
                tbl_item = _build_doc_table_item(table_rows, seq_counter)
                if tbl_item is not None:
                    target.append(tbl_item)
                table_rows = []

        def _flush_para(raw_str: str, target: list[RawDocxBodyItem] = items) -> None:
            cleaned = _clean_doc_fields(raw_str)
            lines = [" ".join(ln.replace("\t", " ").split()).strip() for ln in cleaned.splitlines()]
            txt = "\n".join(ln for ln in lines if ln).strip()
            if not txt:
                return
            is_list = txt.startswith(_BULLET_PREFIXES) and len(txt) >= 3 and txt[1:2] in (" ", "\t")
            heading_lvl: int | None = None
            if not is_list and len(txt) <= 80 and _HEADING_PREFIX_RE.match(txt):
                heading_lvl = 1
            target.append(
                RawDocxParagraph(
                    text=txt,
                    heading_level=heading_lvl,
                    is_list_item=is_list,
                    font_size_pt=14.0 if heading_lvl == 1 else 10.5,
                    is_bold=(heading_lvl == 1),
                )
            )

        idx = 0
        seg_len = len(seg)
        while idx < seg_len:
            ch = seg[idx]
            if ch == "\x07":
                # 셀 끝(\x07) 또는 행 끝(연속 \x07\x07)
                cell_raw = "".join(buf)
                buf.clear()
                cell_clean = _clean_doc_fields(cell_raw)
                cell_txt = " / ".join(
                    " ".join(ln.replace("\t", " ").split()).strip()
                    for ln in cell_clean.replace("\r", "\n").splitlines()
                    if ln.strip()
                ).strip()
                if idx + 1 < seg_len and seg[idx + 1] == "\x07":
                    # 현재 셀이 비어 있지 않거나 행 끝 마커인 경우
                    if cell_txt or current_row:
                        if cell_txt:
                            current_row.append(cell_txt)
                        if current_row:
                            table_rows.append(current_row)
                            current_row = []
                    idx += 2
                else:
                    current_row.append(cell_txt)
                    idx += 1
            elif ch == "\r":
                if current_row or table_rows:
                    # 표 셀 내부의 문단 줄바꿈인지, 표 종료 후 본문인지 판별
                    next_bel = seg.find("\x07", idx + 1)
                    next_cr = seg.find("\r", idx + 1)
                    if next_bel >= 0 and (next_cr < 0 or next_bel < next_cr):
                        buf.append("\n")
                    else:
                        if buf:
                            trailing = "".join(buf)
                            buf.clear()
                            _flush_table()
                            _flush_para(trailing)
                        else:
                            _flush_table()
                else:
                    _flush_para("".join(buf))
                    buf.clear()
                idx += 1
            else:
                buf.append(ch)
                idx += 1

        if current_row or table_rows:
            if buf:
                leftover = _clean_doc_fields("".join(buf)).strip()
                buf.clear()
                if leftover:
                    current_row.append(leftover)
            _flush_table()
        elif buf:
            _flush_para("".join(buf))
            buf.clear()

        if items:
            results.append((items, seg_i > 0))

    return results


def _relabel_to_doc(text: str) -> str:
    return (
        text.replace("DOCX ", "DOC ")
        .replace("WordprocessingML", "MS-DOC")
        .replace("OOXML", "MS-DOC")
    )


def extract_doc_pages(cfb: CfbFile) -> tuple[list[RawDocxPage], OleSummaryInfo]:
    """MS-DOC 바이너리 CFB에서 페이지 단위 IR(`RawDocxPage`)과 요약 메타데이터를 추출한다."""
    word_doc = cfb.read_stream("WordDocument")
    if not word_doc or len(word_doc) < 160:
        raise ParserInputError(
            f"유효한 MS Word 97-2003(.doc) 스트림(WordDocument)을 찾을 수 없다: "
            f"{cfb.source_path.name}"
        )

    w_ident = struct.unpack_from("<H", word_doc, 0)[0]
    if w_ident != _WORD_IDENT:
        raise ParserInputError(
            f"유효한 MS-DOC 헤더 시그니처(0xA5EC)가 아니다(0x{w_ident:04X}): {cfb.source_path.name}"
        )

    flags = struct.unpack_from("<H", word_doc, 10)[0]
    if flags & 0x0100:
        raise ParserInputError(f"암호화된 MS Word(.doc) 문서다: {cfb.source_path.name}")

    tbl_name = "1Table" if (flags & 0x0200) else "0Table"
    table_stm = cfb.read_stream(tbl_name) or cfb.read_stream(
        "0Table" if tbl_name == "1Table" else "1Table"
    )
    if not table_stm:
        raise ParserInputError(
            f"MS-DOC 테이블 스트림({tbl_name})을 찾을 수 없다: {cfb.source_path.name}"
        )

    csw = struct.unpack_from("<H", word_doc, 32)[0]
    cslw_off = 34 + csw * 2
    if cslw_off + 2 > len(word_doc):
        raise ParserInputError(f"손상된 MS-DOC FIB 구조다: {cfb.source_path.name}")
    cslw = struct.unpack_from("<H", word_doc, cslw_off)[0]
    lw_base = cslw_off + 2
    fclcb_cnt_off = lw_base + cslw * 4
    if fclcb_cnt_off + 2 > len(word_doc):
        raise ParserInputError(f"손상된 MS-DOC FIB(FibRgLw) 구조다: {cfb.source_path.name}")

    def _lw(idx: int) -> int:
        if idx >= cslw or lw_base + (idx + 1) * 4 > len(word_doc):
            return 0
        val = int(struct.unpack_from("<i", word_doc, lw_base + idx * 4)[0])
        return max(0, val)

    ccp_text = _lw(3)
    ccp_ftn = _lw(4)
    ccp_hdd = _lw(5)
    ccp_atn = _lw(7)
    ccp_edn = _lw(8)
    ccp_txbx = _lw(9)

    cb_fclcb = struct.unpack_from("<H", word_doc, fclcb_cnt_off)[0]
    fclcb_base = fclcb_cnt_off + 2
    clx_pair_off = fclcb_base + 33 * 8
    if cb_fclcb <= 33 or clx_pair_off + 8 > len(word_doc):
        raise ParserInputError(
            f"MS-DOC 피스 테이블(fcClx) 오프셋을 찾을 수 없다: {cfb.source_path.name}"
        )

    fc_clx, lcb_clx = struct.unpack_from("<II", word_doc, clx_pair_off)
    full_text = _decode_piece_table(word_doc, table_stm, fc_clx, lcb_clx)

    # 서브문서 경계 분할 (ccpText -> ccpFtn -> ccpHdd -> ccpMcr -> ccpAtn -> ccpEdn -> ccpTxbx)
    pos = 0
    body_text = full_text[pos : pos + ccp_text] if ccp_text > 0 else full_text
    pos += ccp_text
    ftn_text = full_text[pos : pos + ccp_ftn] if ccp_ftn > 0 else ""
    pos += ccp_ftn
    hdd_text = full_text[pos : pos + ccp_hdd] if ccp_hdd > 0 else ""
    pos += ccp_hdd
    pos += _lw(6)  # ccpMcr (macro)
    atn_text = full_text[pos : pos + ccp_atn] if ccp_atn > 0 else ""
    pos += ccp_atn
    edn_text = full_text[pos : pos + ccp_edn] if ccp_edn > 0 else ""
    pos += ccp_edn
    txbx_text = full_text[pos : pos + ccp_txbx] if ccp_txbx > 0 else ""

    seq_counter = [0]
    segments = _parse_doc_body_stream(body_text, seq_counter)

    # 텍스트 상자 텍스트 보충
    if txbx_text.strip():
        txbx_items: list[RawDocxBodyItem] = [
            RawDocxParagraph(text=ln, font_size_pt=10.0, in_textbox=True)
            for ln in _split_nonempty_chunks(txbx_text)
        ]
        if txbx_items:
            if segments:
                segments[-1][0].extend(txbx_items)
            else:
                segments.append((txbx_items, False))

    # 내장 OfficeArt BLIP 이미지 추출 (`Data` 및 `WordDocument` 스트림)
    data_stm = cfb.read_stream("Data") or b""
    blips = extract_officeart_blips(data_stm) or extract_officeart_blips(word_doc)
    blip_items: list[RawDocxBodyItem] = []
    for blip in blips:
        seq_counter[0] += 1
        w_pt = min(_DEFAULT_PAGE_W * 0.78, float(blip.pixel_width or 240) * 0.75)
        h_pt = min(_DEFAULT_PAGE_H * 0.65, float(blip.pixel_height or 160) * 0.75)
        blip_items.append(
            RawPptxImage(
                shape_id=str(seq_counter[0]),
                name=f"doc_image_{blip.index}{blip.extension}",
                alt_text="",
                media_part=f"Data/blip_{blip.index}{blip.extension}",
                extension=blip.extension,
                rect_pt=(0.0, 0.0, max(48.0, w_pt), max(36.0, h_pt)),
                raw_bytes=blip.raw_bytes,
                pixel_width=blip.pixel_width,
                pixel_height=blip.pixel_height,
            )
        )

    headers_list = _split_nonempty_chunks(hdd_text)[:4]
    footnotes_list = [(str(i + 1), ln) for i, ln in enumerate(_split_nonempty_chunks(ftn_text))]
    endnotes_list = [(str(i + 1), ln) for i, ln in enumerate(_split_nonempty_chunks(edn_text))]
    comments_list = [
        DocxCommentEntry(
            comment_id=str(i + 1),
            author="DOC 검토 주석",
            initials="",
            date="",
            text=ln,
        )
        for i, ln in enumerate(_split_nonempty_chunks(atn_text))
    ]

    pages: list[RawDocxPage] = []
    usable_h = _DEFAULT_PAGE_H - 144.0

    def _new_page() -> RawDocxPage:
        pg = RawDocxPage(
            page_number=len(pages) + 1,
            width_pt=_DEFAULT_PAGE_W,
            height_pt=_DEFAULT_PAGE_H,
            margin_top_pt=72.0,
            margin_bottom_pt=72.0,
            margin_left_pt=72.0,
            margin_right_pt=72.0,
            column_count=1,
            column_space_pt=18.0,
            headers=list(headers_list),
        )
        pages.append(pg)
        return pg

    cur_page = _new_page()
    cur_y = 0.0
    has_explicit_breaks = len(segments) > 1

    for seg_items, break_before in segments:
        if break_before and cur_page.items:
            cur_page = _new_page()
            cur_y = 0.0
        for it in seg_items:
            est_h = (
                it.raw_height_pt + 14.0
                if isinstance(it, RawDocxTableItem)
                else max(18.0, (len(it.text) / 65.0 + 1) * 15.0)
                if isinstance(it, RawDocxParagraph)
                else 140.0
            )
            if not has_explicit_breaks and cur_page.items and cur_y + est_h > usable_h * 1.15:
                cur_page = _new_page()
                cur_y = 0.0
            cur_page.items.append(it)
            cur_y += est_h

    if blip_items:
        pages[0].items.extend(blip_items)
    if footnotes_list:
        pages[-1].footnotes.extend(footnotes_list)
    if endnotes_list:
        pages[-1].endnotes.extend(endnotes_list)
    if comments_list:
        pages[-1].comments.extend(comments_list)

    summary = read_ole_summary_info(cfb, ("\x05SummaryInformation",))
    return pages, summary


class DocDocumentAdapter:
    """구형 바이너리 MS Word(`.doc`) 하이브리드(LibreOffice 승격 + 순수 Python MS-DOC) 어댑터."""

    @property
    def format(self) -> SourceFormat:
        return SourceFormat.DOC

    @property
    def extensions(self) -> tuple[str, ...]:
        return (".doc",)

    def __init__(
        self,
        *,
        use_libreoffice: bool = True,
        libreoffice_binary: str | None = None,
    ) -> None:
        self._use_libreoffice = use_libreoffice
        self._libreoffice_binary = libreoffice_binary
        self._docx_adapter = DocxDocumentAdapter()

    def _try_libreoffice_docx(self, source_path: Path, tmp_dir: Path) -> Path | None:
        if not self._use_libreoffice:
            return None
        if find_libreoffice_binary(self._libreoffice_binary) is None:
            return None
        try:
            return convert_legacy_office_document(
                source_path,
                "docx",
                tmp_dir,
                binary=self._libreoffice_binary,
            )
        except ParserInputError:
            return None

    @staticmethod
    def _restamp_metadata(orig_path: Path, meta: DocumentMetadata) -> DocumentMetadata:
        import hashlib

        raw = orig_path.read_bytes()
        return meta.model_copy(
            update={
                "source_path": orig_path.name,
                "format": SourceFormat.DOC,
                "file_size_bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )

    def _analyze_native(
        self,
        source_path: Path,
        options: ParseOptions,
        *,
        layout_detector: PageLayoutDetector | None = None,
    ) -> tuple[list[AnalyzedDocxPage], RoutingPlan]:
        with CfbFile(source_path) as cfb:
            raw_pages, summary = extract_doc_pages(cfb)
            total_pages = len(raw_pages)
            file_size = cfb.file_size
            file_sha256 = cfb.sha256

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

        analyzed_pages: list[AnalyzedDocxPage] = []
        for raw_page in target_pages:
            ap = analyze_docx_page(
                raw_page,
                doc_body_font_size=body_font,
                repeating_logos=repeating_logos,
                include_comments=options.include_comments,
            )
            ap.visuals = [
                v.model_copy(update={"reason": _relabel_to_doc(v.reason)}) for v in ap.visuals
            ]
            ap.route = ap.route.model_copy(
                update={
                    "reasons": [_relabel_to_doc(r) for r in ap.route.reasons],
                    "regions": [
                        r.model_copy(update={"reason": _relabel_to_doc(r.reason)})
                        for r in ap.route.regions
                    ],
                }
            )
            if active_detector is not None:
                ap = _apply_layout_detection_to_docx_page(ap, layout_detector=active_detector)
            analyzed_pages.append(ap)

        title = summary.title
        if not title:
            for ap in analyzed_pages:
                for blk in ap.blocks:
                    if blk.text.strip():
                        title = blk.text.strip()[:120]
                        break
                if title:
                    break

        metadata = DocumentMetadata(
            source_path=source_path.name,
            format=SourceFormat.DOC,
            file_size_bytes=file_size,
            sha256=file_sha256,
            page_count=total_pages,
            title=title,
            author=summary.author,
            creator=summary.creator_app or "Microsoft Word 97-2003 (Native MS-DOC)",
        )
        plan = build_docx_routing_plan(metadata, analyzed_pages, body_font_size=body_font)
        return analyzed_pages, plan

    def inspect(
        self,
        source_path: Path,
        *,
        options: ParseOptions,
        layout_detector: PageLayoutDetector | None = None,
        page_renderer: PageRenderer | None = None,
    ) -> RoutingPlan:
        """`.doc` 문서를 진단한다(LibreOffice 있으면 `.docx` 승격, 없으면 순수 Python MS-DOC)."""
        with tempfile.TemporaryDirectory(prefix="autoetl_doc_conv_") as td:
            converted = self._try_libreoffice_docx(source_path, Path(td))
            if converted is not None:
                plan = self._docx_adapter.inspect(
                    converted,
                    options=options,
                    layout_detector=layout_detector,
                    page_renderer=page_renderer,
                )
                return plan.model_copy(
                    update={"document": self._restamp_metadata(source_path, plan.document)}
                )
        _, plan = self._analyze_native(
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
        """`.doc` 문서를 통합 IR로 파싱한다(LibreOffice 승격 우선 + 순수 Python MS-DOC 폴백)."""
        with tempfile.TemporaryDirectory(prefix="autoetl_doc_conv_") as td:
            converted = self._try_libreoffice_docx(source_path, Path(td))
            if converted is not None:
                doc = self._docx_adapter.parse(
                    converted,
                    options=options,
                    vision_adapter=vision_adapter,
                    page_renderer=page_renderer,
                    layout_detector=layout_detector,
                )
                restamped = self._restamp_metadata(source_path, doc.document)
                return doc.model_copy(
                    update={
                        "document": restamped,
                        "routing_plan": doc.routing_plan.model_copy(update={"document": restamped}),
                    }
                )

        analyzed_pages, routing_plan = self._analyze_native(
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


__all__ = [
    "DocDocumentAdapter",
    "extract_doc_pages",
]
