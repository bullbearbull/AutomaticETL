"""순수 Python 무의존성 구형 바이너리 한글 5.0(`.hwp`, OLE2 CFB) 파서 및 어댑터.

외부 서드파티 패키지나 LibreOffice/한컴오피스 없이 Python 표준 라이브러리(`struct`,
`zlib`, `pathlib`)만으로 HWP 5.0 바이너리 문서의 `FileHeader`, `DocInfo`(글꼴·글자모양·
문단모양·스타일·번호/글머리표·테두리·BinData), `BodyText/Section*`(문단·병합 표·수식·
그림·글상자·도식·머리글/바닥글·각주/미주·숨은설명·하이퍼링크·다단·쪽 나누기),
`BinData/*`, `\\x05HwpSummaryInformation`을 네이티브로 파싱한다.
"""

from __future__ import annotations

import re
import struct
import zlib
from collections.abc import Iterator
from dataclasses import dataclass, field
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
from autoetl.parser.docx.adapter import _apply_layout_detection_to_docx_page
from autoetl.parser.docx.elements import (
    RawDocxBodyItem,
    RawDocxPage,
    RawDocxParagraph,
    RawDocxTableItem,
    RawDocxVectorDiagram,
)
from autoetl.parser.docx.ooxml import DocxCommentEntry
from autoetl.parser.docx.router import (
    AnalyzedDocxPage,
    analyze_docx_page,
    build_docx_routing_plan,
    estimate_docx_body_font_size,
    find_repeating_docx_logos,
)
from autoetl.parser.hwpx.elements import (
    HwpxSectionLayout,
    _format_hwpx_list_marker,
    normalize_hwp_equation_script,
)
from autoetl.parser.hwpx.owpml import (
    _CODE_STYLE_TOKENS,
    _MONOSPACE_FONTS,
    HwpxBorderSpec,
    HwpxCharProp,
    HwpxHeaderCatalog,
    HwpxNumberingLevel,
    HwpxParaProp,
    HwpxStyleDef,
    _parse_style_heading_level,
    hwpunit_to_pt,
)
from autoetl.parser.image_io import DecodedImage, decode_png
from autoetl.parser.legacy.cfb import CfbFile, OleSummaryInfo, read_ole_summary_info
from autoetl.parser.pptx.adapter import (
    _extract_crop_for_pptx_visual,
    _find_surrounding_slide_context,
    _run_vision_on_pptx_region,
)
from autoetl.parser.pptx.elements import RawPptxImage, RawPptxOleObject
from autoetl.parser.pptx.ooxml import (
    MAX_MEDIA_BYTES,
    clamp,
    make_bbox,
    probe_image_dimensions,
)

# HWP 5.0 레코드 태그 상수 (HWPTAG_BEGIN = 0x10 = 16)
_TAG_BIN_DATA = 0x12
_TAG_FACE_NAME = 0x13
_TAG_BORDER_FILL = 0x14
_TAG_CHAR_SHAPE = 0x15
_TAG_NUMBERING = 0x17
_TAG_BULLET = 0x18
_TAG_PARA_SHAPE = 0x19
_TAG_STYLE = 0x1A

_TAG_PARA_HEADER = 0x42
_TAG_PARA_TEXT = 0x43
_TAG_PARA_CHAR_SHAPE = 0x44
_TAG_PARA_LINE_SEG = 0x45
_TAG_CTRL_HEADER = 0x47
_TAG_LIST_HEADER = 0x48
_TAG_PAGE_DEF = 0x49
_TAG_SHAPE_COMPONENT = 0x4C
_TAG_TABLE = 0x4D
_TAG_SHAPE_COMPONENT_OLE = 0x54
_TAG_SHAPE_COMPONENT_PICTURE = 0x55
_TAG_EQEDIT = 0x58

# HWP 5.0 확장/인라인 제어 문자 코드 (각각 8 uint16 = 16바이트 소비)
_HWP_EXTENDED_CHARS = frozenset({1, 2, 3, 11, 12, 14, 15, 16, 17, 18, 21, 22, 23})
_HWP_INLINE_CHARS = frozenset({4, 5, 6, 7, 8, 9, 19, 20})

_MAX_DECOMPRESSED_BYTES = 64 * 1024 * 1024

_BULLET_CHARS = (
    "•",
    "·",
    "◦",
    "▪",
    "▫",
    "‣",
    "\u2043",
    "∙",
    "○",
    "●",
    "■",
    "□",
    "◆",
    "◇",
    "▶",
    "▷",
    "-",
    "※",
)

# HWP 5.0 번호 형식 코드 -> OWPML numFormat 문자열 매핑
_HWP5_NUM_FORMAT_MAP: dict[int, str] = {
    0: "DIGIT",
    1: "CIRCLED_DIGIT",
    2: "ROMAN_CAPITAL",
    3: "ROMAN_SMALL",
    4: "LATIN_CAPITAL",
    5: "LATIN_SMALL",
    6: "CIRCLED_LATIN_CAPITAL",
    7: "CIRCLED_LATIN_SMALL",
    8: "HANGUL_SYLLABLE",
    9: "CIRCLED_HANGUL_SYLLABLE",
    10: "HANGUL_JAMO",
    11: "CIRCLED_HANGUL_JAMO",
}


@dataclass(frozen=True)
class HwpBinDataEntry:
    """`DocInfo`의 `HWPTAG_BIN_DATA` 항목 (1-based ID로 참조)."""

    bin_id: int
    bin_type: int  # 0=LINK, 1=EMBEDDING, 2=STORAGE(OLE)
    compression: int  # 0=기본(FileHeader 따름), 1=압축, 2=비압축
    storage_id: int
    extension: str


@dataclass
class HwpRecordNode:
    """레코드 레벨(`level`) 기반으로 계층화한 HWP 5.0 레코드 트리 노드."""

    tag_id: int
    level: int
    payload: bytes
    children: list[HwpRecordNode] = field(default_factory=list)


def _decompress_hwp_stream(raw: bytes, *, compressed: bool) -> bytes:
    """HWP 5.0 스트림(Raw Deflate `-15`)을 안전하게 압축 해제한다."""
    if not raw:
        return b""
    if not compressed:
        return raw[:_MAX_DECOMPRESSED_BYTES]

    for wbits in (-15, 15):
        try:
            decomp = zlib.decompressobj(wbits)
            out = decomp.decompress(raw, _MAX_DECOMPRESSED_BYTES)
            return out
        except zlib.error:
            continue
    return raw[:_MAX_DECOMPRESSED_BYTES]


def _parse_record_tree(stream_bytes: bytes) -> list[HwpRecordNode]:
    """선형 HWP 5.0 레코드 바이트열을 `level` 기반 트리로 복원한다."""
    roots: list[HwpRecordNode] = []
    stack: list[HwpRecordNode] = []
    pos = 0
    total = len(stream_bytes)

    while pos + 4 <= total:
        hdr = struct.unpack_from("<I", stream_bytes, pos)[0]
        pos += 4
        tag_id = hdr & 0x3FF
        level = (hdr >> 10) & 0x3FF
        size = (hdr >> 20) & 0xFFF
        if size == 0xFFF:
            if pos + 4 > total:
                break
            size = struct.unpack_from("<I", stream_bytes, pos)[0]
            pos += 4
        if pos + size > total:
            size = total - pos
        payload = stream_bytes[pos : pos + size]
        pos += size

        node = HwpRecordNode(tag_id=tag_id, level=level, payload=payload)
        while stack and stack[-1].level >= level:
            stack.pop()
        if stack:
            stack[-1].children.append(node)
        else:
            roots.append(node)
        stack.append(node)

    return roots


def _has_explicit_page_break(stream_bytes: bytes) -> bool:
    """레코드 트리를 구축하지 않고 level=0 문단에 명시적 쪽 나누기가 있는지 선형 스캔한다."""
    pos = 0
    total = len(stream_bytes)
    while pos + 4 <= total:
        hdr = struct.unpack_from("<I", stream_bytes, pos)[0]
        pos += 4
        tag_id = hdr & 0x3FF
        level = (hdr >> 10) & 0x3FF
        size = (hdr >> 20) & 0xFFF
        if size == 0xFFF:
            if pos + 4 > total:
                break
            size = struct.unpack_from("<I", stream_bytes, pos)[0]
            pos += 4
        if pos + size > total:
            size = total - pos
        if (
            level == 0
            and tag_id == _TAG_PARA_HEADER
            and size >= 12
            and (stream_bytes[pos + 11] & 0x04)
        ):
            return True
        pos += size
    return False


def _iter_root_record_nodes(stream_bytes: bytes) -> Iterator[HwpRecordNode]:
    """대용량 섹션 스트림에서 최상위(`level == 0`) 레코드 서브트리를 하나씩 스트리밍 생성한다."""
    stack: list[HwpRecordNode] = []
    current_root: HwpRecordNode | None = None
    pos = 0
    total = len(stream_bytes)

    while pos + 4 <= total:
        hdr = struct.unpack_from("<I", stream_bytes, pos)[0]
        pos += 4
        tag_id = hdr & 0x3FF
        level = (hdr >> 10) & 0x3FF
        size = (hdr >> 20) & 0xFFF
        if size == 0xFFF:
            if pos + 4 > total:
                break
            size = struct.unpack_from("<I", stream_bytes, pos)[0]
            pos += 4
        if pos + size > total:
            size = total - pos
        payload = stream_bytes[pos : pos + size]
        pos += size

        node = HwpRecordNode(tag_id=tag_id, level=level, payload=payload)
        while stack and stack[-1].level >= level:
            stack.pop()
        if stack:
            stack[-1].children.append(node)
        else:
            if current_root is not None:
                yield current_root
            current_root = node
        stack.append(node)

    if current_root is not None:
        yield current_root


def _iter_records_flat(nodes: list[HwpRecordNode]) -> list[HwpRecordNode]:
    out: list[HwpRecordNode] = []

    def _walk(ns: list[HwpRecordNode]) -> None:
        for nd in ns:
            out.append(nd)
            if nd.children:
                _walk(nd.children)

    _walk(nodes)
    return out


def _read_u16_utf16(payload: bytes, offset: int) -> tuple[str, int]:
    """`(uint16 글자수 + UTF-16LE 문자열)` 필드를 읽고 `(문자열, 다음 오프셋)`을 반환한다."""
    if offset + 2 > len(payload):
        return "", len(payload)
    char_cnt = struct.unpack_from("<H", payload, offset)[0]
    offset += 2
    byte_len = char_cnt * 2
    if offset + byte_len > len(payload):
        byte_len = max(0, ((len(payload) - offset) // 2) * 2)
    text = payload[offset : offset + byte_len].decode("utf-16le", errors="replace")
    return text.rstrip("\x00"), offset + byte_len


def _parse_doc_info(
    doc_info_bytes: bytes,
) -> tuple[HwpxHeaderCatalog, list[HwpBinDataEntry]]:
    """`DocInfo` 스트림에서 스타일·글꼴·글자/문단모양·번호·테두리·BinData 카탈로그를 구축한다."""
    flat = _iter_records_flat(_parse_record_tree(doc_info_bytes))

    bin_entries: list[HwpBinDataEntry] = []
    face_names: list[str] = []
    border_fills: dict[str, HwpxBorderSpec] = {}
    char_props: dict[str, HwpxCharProp] = {}
    para_props: dict[str, HwpxParaProp] = {}
    styles: dict[str, HwpxStyleDef] = {}
    numberings: dict[tuple[str, int], HwpxNumberingLevel] = {}
    bullets: dict[str, str] = {}

    bf_idx = 0
    cs_idx = 0
    ps_idx = 0
    st_idx = 0
    num_idx = 0
    bul_idx = 0

    for rec in flat:
        pl = rec.payload
        tag = rec.tag_id

        if tag == _TAG_BIN_DATA and len(pl) >= 2:
            prop = struct.unpack_from("<H", pl, 0)[0]
            b_type = prop & 0x0F
            b_comp = (prop >> 4) & 0x03
            storage_id = 0
            ext = ""
            if b_type == 1 and len(pl) >= 6:  # EMBEDDING
                storage_id = struct.unpack_from("<H", pl, 2)[0]
                ext, _ = _read_u16_utf16(pl, 4)
            elif b_type == 2 and len(pl) >= 4:  # STORAGE (OLE)
                storage_id = struct.unpack_from("<H", pl, 2)[0]
                ext = "ole"
            bin_entries.append(
                HwpBinDataEntry(
                    bin_id=len(bin_entries) + 1,
                    bin_type=b_type,
                    compression=b_comp,
                    storage_id=storage_id,
                    extension=ext.lower().lstrip("."),
                )
            )

        elif tag == _TAG_FACE_NAME and len(pl) >= 3:
            face, _ = _read_u16_utf16(pl, 1)
            face_names.append(face.strip().lower())

        elif tag == _TAG_BORDER_FILL and len(pl) >= 18:
            bf_idx += 1  # HWP 5.0 borderFillID는 1-based
            # 2B attr + 4*(1B type + 1B width + 4B color) = 2 + 24B (좌·우·상·하)
            # 또는 type[4] + width[4] + color[4]: HWP 5.0 표 23 규격은 type 4B, width 4B, color 16B!
            has_l = pl[2] != 0
            has_r = pl[3] != 0
            has_t = pl[4] != 0
            has_b = pl[5] != 0
            border_fills[str(bf_idx)] = HwpxBorderSpec(
                border_fill_id=str(bf_idx),
                has_top=has_t,
                has_bottom=has_b,
                has_left=has_l,
                has_right=has_r,
            )

        elif tag == _TAG_CHAR_SHAPE and len(pl) >= 50:
            # 0..14: font_ids[7] (uint16 x 7)
            # 42..46: base_size (int32, HWPUNIT = 1/100 pt)
            # 46..50: attr (uint32: bit0=italic, bit1=bold)
            font_ids = struct.unpack_from("<7H", pl, 0)
            base_size_raw = struct.unpack_from("<i", pl, 42)[0]
            attr = struct.unpack_from("<I", pl, 46)[0]
            sz_pt = hwpunit_to_pt(base_size_raw, 10.0)
            if sz_pt <= 0.0 or sz_pt > 200.0:
                sz_pt = 10.0
            is_italic = bool(attr & 0x01)
            is_bold = bool(attr & 0x02)
            is_mono = any(
                0 <= fid < len(face_names) and face_names[fid] in _MONOSPACE_FONTS
                for fid in font_ids[:2]
            )
            char_props[str(cs_idx)] = HwpxCharProp(
                char_pr_id=str(cs_idx),
                font_size_pt=sz_pt,
                is_bold=is_bold,
                is_italic=is_italic,
                is_monospace=is_mono,
            )
            cs_idx += 1

        elif tag == _TAG_NUMBERING and len(pl) >= 14:
            num_idx += 1  # 1-based numbering ID
            off = 0
            for lvl in range(1, 8):
                if off + 12 > len(pl):
                    break
                p_attr = struct.unpack_from("<I", pl, off)[0]
                off += 12  # 4B attr + 2B width_adj + 2B text_dist + 4B char_shape_id
                tmpl, off = _read_u16_utf16(pl, off)
                num_type_code = (p_attr >> 4) & 0x0F
                num_fmt = _HWP5_NUM_FORMAT_MAP.get(num_type_code, "DIGIT")
                numberings[(str(num_idx), lvl)] = HwpxNumberingLevel(
                    level=lvl,
                    num_format=num_fmt,
                    start=1,
                    template=tmpl,
                )
            # start_number (uint16) follows 7 levels
            if off + 2 <= len(pl):
                base_start = max(1, struct.unpack_from("<H", pl, off)[0])
                off += 2
                if (str(num_idx), 1) in numberings:
                    first = numberings[(str(num_idx), 1)]
                    numberings[(str(num_idx), 1)] = HwpxNumberingLevel(
                        level=1,
                        num_format=first.num_format,
                        start=base_start,
                        template=first.template,
                    )

        elif tag == _TAG_BULLET and len(pl) >= 14:
            bul_idx += 1
            b_code = struct.unpack_from("<H", pl, 12)[0]
            b_char = chr(b_code) if 0x20 <= b_code <= 0xFFFD else "•"
            bullets[str(bul_idx)] = b_char

        elif tag == _TAG_PARA_SHAPE and len(pl) >= 28:
            # 0..4: attr1 (bit 23..24 = heading type, bit 25..27 = heading level 0..6)
            # 24..26: heading_id_ref (uint16)
            attr1 = struct.unpack_from("<I", pl, 0)[0]
            h_type_code = (attr1 >> 23) & 0x03
            h_type = ("NONE", "OUTLINE", "NUMBER", "BULLET")[h_type_code]
            h_level = (attr1 >> 25) & 0x07
            h_id_ref = struct.unpack_from("<H", pl, 24)[0]
            para_props[str(ps_idx)] = HwpxParaProp(
                para_pr_id=str(ps_idx),
                heading_type=h_type,
                heading_id_ref=str(h_id_ref),
                heading_level=h_level,
            )
            ps_idx += 1

        elif tag == _TAG_STYLE and len(pl) >= 12:
            off = 0
            name, off = _read_u16_utf16(pl, off)
            eng_name, off = _read_u16_utf16(pl, off)
            if off + 8 <= len(pl):
                # 1B kind, 1B next_style, 2B lang_id, 2B para_shape_id, 2B char_shape_id
                p_ref = str(struct.unpack_from("<H", pl, off + 4)[0])
                c_ref = str(struct.unpack_from("<H", pl, off + 6)[0])
            else:
                p_ref = "0"
                c_ref = "0"
            h_lvl = _parse_style_heading_level(name, eng_name)
            p_prop = para_props.get(p_ref)
            if h_lvl is None and p_prop is not None and p_prop.heading_type == "OUTLINE":
                h_lvl = min(6, p_prop.heading_level + 1)
            combined_name = f"{name} {eng_name}".lower()
            c_prop = char_props.get(c_ref)
            is_code = any(tok in combined_name for tok in _CODE_STYLE_TOKENS) or bool(
                c_prop is not None and c_prop.is_monospace
            )
            styles[str(st_idx)] = HwpxStyleDef(
                style_id=str(st_idx),
                name=name,
                eng_name=eng_name,
                para_pr_id=p_ref,
                char_pr_id=c_ref,
                heading_level=h_lvl,
                is_code_style=is_code,
            )
            st_idx += 1

    default_font = 10.0
    if "0" in styles and styles["0"].char_pr_id in char_props:
        default_font = char_props[styles["0"].char_pr_id].font_size_pt
    elif "0" in char_props:
        default_font = char_props["0"].font_size_pt

    catalog = HwpxHeaderCatalog(
        char_props=char_props,
        para_props=para_props,
        styles=styles,
        numberings=numberings,
        bullets=bullets,
        border_fills=border_fills,
        default_font_size_pt=default_font,
    )
    return catalog, bin_entries


def _decode_para_text(payload: bytes) -> str:
    """`HWPTAG_PARA_TEXT` UTF-16LE 스트림에서 제어 코드를 해석해 본문 텍스트를 디코딩한다."""
    if not payload or len(payload) < 2:
        return ""
    n_u16 = len(payload) // 2
    units = struct.unpack_from(f"<{n_u16}H", payload, 0)
    out: list[str] = []
    i = 0
    while i < n_u16:
        code = units[i]
        if code in _HWP_EXTENDED_CHARS:
            i += 8
            continue
        if code in _HWP_INLINE_CHARS:
            if code == 9:
                out.append("\t")
            i += 8
            continue
        if code == 0:
            i += 1
        elif code == 10:
            out.append("\n")
            i += 1
        elif code == 13:
            i += 1
        elif code == 24:
            out.append("-")
            i += 1
        elif code in (30, 31):
            out.append(" ")
            i += 1
        elif code < 32:
            i += 1
        else:
            # UTF-16 서로게이트 페어 처리
            if 0xD800 <= code <= 0xDBFF and i + 1 < n_u16 and 0xDC00 <= units[i + 1] <= 0xDFFF:
                pair = struct.pack("<HH", code, units[i + 1]).decode("utf-16le", errors="replace")
                out.append(pair)
                i += 2
            else:
                out.append(chr(code))
                i += 1
    return "".join(out)


def _decode_ctrl_id(payload: bytes) -> str:
    """`HWPTAG_CTRL_HEADER` 앞 4바이트 리틀엔디안 CtrlID를 ASCII 문자열로 변환한다."""
    if len(payload) < 4:
        return ""
    return payload[:4][::-1].decode("latin1", errors="replace")


@dataclass
class _HwpParseContext:
    cfb: CfbFile
    compressed: bool
    catalog: HwpxHeaderCatalog
    bin_entries: list[HwpBinDataEntry]
    layout: HwpxSectionLayout
    list_counters: dict[tuple[str, int], int]
    seq_counter: list[int]
    note_counter: list[int]
    headers: list[str]
    footers: list[str]
    footnotes: dict[str, str]
    endnotes: dict[str, str]
    comments: dict[str, DocxCommentEntry]
    pending_fn: list[str] = field(default_factory=list)
    pending_en: list[str] = field(default_factory=list)
    pending_cm: list[str] = field(default_factory=list)
    _bin_cache: dict[int, tuple[str, bytes, int | None, int | None] | None] = field(
        default_factory=dict
    )

    def read_bin_data(self, bin_id: int) -> tuple[str, bytes, int | None, int | None] | None:
        """1-based `bin_id`의 `BinData/BIN{id:04X}.{ext}` 바이트와 픽셀 크기를 캐시해 반환한다."""
        if bin_id < 1:
            return None
        if bin_id in self._bin_cache:
            return self._bin_cache[bin_id]

        entry = self.bin_entries[bin_id - 1] if bin_id <= len(self.bin_entries) else None
        candidates: list[str] = []
        if entry is not None and entry.storage_id > 0 and entry.extension:
            candidates.append(f"BinData/BIN{entry.storage_id:04X}.{entry.extension}")
        for ext in ("png", "jpg", "jpeg", "bmp", "gif", "tif", "tiff", "wmf", "emf"):
            candidates.append(f"BinData/BIN{bin_id:04X}.{ext}")

        for cand in candidates:
            actual = self.cfb.resolve_stream_name(cand)
            if actual is None:
                continue
            raw = self.cfb.read_stream(actual, max_bytes=MAX_MEDIA_BYTES)
            if raw is None:
                continue
            should_decomp = (
                (entry.compression == 1)
                if (entry is not None and entry.compression in (1, 2))
                else self.compressed
            )
            # 이미 PNG/JPEG/BMP/GIF 매직 바이트로 시작하면 압축 해제 불필요
            if not raw.startswith((b"\x89PNG", b"\xff\xd8\xff", b"BM", b"GIF8")) and should_decomp:
                raw = _decompress_hwp_stream(raw, compressed=True)
            ext = Path(actual).suffix.lower() or ".bin"
            px_w, px_h = probe_image_dimensions(raw, ext)
            result = (actual, raw, px_w, px_h)
            self._bin_cache[bin_id] = result
            return result
        self._bin_cache[bin_id] = None
        return None


def _extract_sublist_plain_text_hwp(nodes: list[HwpRecordNode]) -> str:
    """머리글·바닥글·각주·미주·숨은설명·캡션 서브트리에서 순수 텍스트를 추출한다."""
    lines: list[str] = []
    for rec in _iter_records_flat(nodes):
        if rec.tag_id == _TAG_PARA_TEXT:
            txt = _decode_para_text(rec.payload).strip()
            if txt:
                lines.append(txt)
        elif rec.tag_id == _TAG_EQEDIT and len(rec.payload) >= 6:
            slen = struct.unpack_from("<H", rec.payload, 4)[0]
            script = rec.payload[6 : 6 + slen * 2].decode("utf-16le", errors="replace")
            eq_txt = normalize_hwp_equation_script(script)
            if eq_txt:
                lines.append(eq_txt)
    return " ".join(lines).strip()


def _parse_page_def(payload: bytes, layout: HwpxSectionLayout) -> None:
    """`HWPTAG_PAGE_DEF`(0x49)에서 용지 크기·여백·방향을 읽어 `layout`을 갱신한다."""
    if len(payload) < 40:
        return
    w_raw, h_raw, m_left, m_right, m_top, m_bottom = struct.unpack_from("<6I", payload, 0)
    flags = struct.unpack_from("<I", payload, 36)[0]
    w_pt = hwpunit_to_pt(w_raw, layout.width_pt)
    h_pt = hwpunit_to_pt(h_raw, layout.height_pt)
    is_landscape = bool(flags & 0x01)
    if w_pt > 36.0 and h_pt > 36.0:
        if is_landscape and w_pt < h_pt:
            layout.width_pt, layout.height_pt = h_pt, w_pt
        else:
            layout.width_pt, layout.height_pt = w_pt, h_pt
    layout.margin_left_pt = max(0.0, hwpunit_to_pt(m_left, layout.margin_left_pt))
    layout.margin_right_pt = max(0.0, hwpunit_to_pt(m_right, layout.margin_right_pt))
    layout.margin_top_pt = max(0.0, hwpunit_to_pt(m_top, layout.margin_top_pt))
    layout.margin_bottom_pt = max(0.0, hwpunit_to_pt(m_bottom, layout.margin_bottom_pt))


def _parse_cold(payload: bytes, layout: HwpxSectionLayout) -> None:
    """`cold` 컨트롤 헤더에서 다단(`colCount`)을 읽어 `layout`을 갱신한다."""
    if len(payload) < 6:
        return
    attr = struct.unpack_from("<H", payload, 4)[0]
    col_cnt = (attr >> 2) & 0xFF
    if 1 <= col_cnt <= 6:
        layout.column_count = col_cnt


def _extract_hwp_table(
    ctrl_node: HwpRecordNode,
    ctx: _HwpParseContext,
) -> tuple[RawDocxTableItem | None, list[RawDocxBodyItem]]:
    """`tbl ` 컨트롤 노드(`HWPTAG_TABLE` + 셀별 `HWPTAG_LIST_HEADER`)를 표로 추출한다."""
    tbl_rec: HwpRecordNode | None = None
    caption_text: str | None = None
    cell_groups: list[tuple[HwpRecordNode, list[HwpRecordNode]]] = []

    children = ctrl_node.children
    idx = 0
    # 1. HWPTAG_TABLE 이전의 LIST_HEADER는 캡션 영역
    while idx < len(children):
        ch = children[idx]
        if ch.tag_id == _TAG_TABLE:
            tbl_rec = ch
            idx += 1
            break
        if ch.tag_id == _TAG_LIST_HEADER:
            cap_nodes: list[HwpRecordNode] = []
            idx += 1
            while idx < len(children) and children[idx].tag_id not in (
                _TAG_TABLE,
                _TAG_LIST_HEADER,
            ):
                cap_nodes.append(children[idx])
                idx += 1
            cap_txt = _extract_sublist_plain_text_hwp(cap_nodes)
            if cap_txt:
                caption_text = cap_txt
            continue
        idx += 1

    if tbl_rec is None or len(tbl_rec.payload) < 8:
        return None, []

    decl_rows, decl_cols = struct.unpack_from("<HH", tbl_rec.payload, 4)
    tbl_bf_id = ""
    bf_off = 18 + decl_rows * 2
    if bf_off + 2 <= len(tbl_rec.payload):
        tbl_bf_id = str(struct.unpack_from("<H", tbl_rec.payload, bf_off)[0])

    # 2. HWPTAG_TABLE 이후의 각 LIST_HEADER + 뒤따르는 PARA_HEADER 묶음 = 셀
    while idx < len(children):
        ch = children[idx]
        if ch.tag_id == _TAG_LIST_HEADER:
            lh_node = ch
            para_nodes: list[HwpRecordNode] = []
            idx += 1
            while idx < len(children) and children[idx].tag_id != _TAG_LIST_HEADER:
                para_nodes.append(children[idx])
                idx += 1
            cell_groups.append((lh_node, para_nodes))
        else:
            idx += 1

    if not cell_groups or decl_rows <= 0 or decl_cols <= 0:
        return None, []

    @dataclass
    class _ParsedCell:
        row: int
        col: int
        row_span: int
        col_span: int
        width_pt: float
        border_fill_id: str
        para_nodes: list[HwpRecordNode]

    parsed_cells: list[_ParsedCell] = []
    max_row = decl_rows
    max_col = decl_cols

    for cell_i, (lh_node, para_nodes) in enumerate(cell_groups):
        pl = lh_node.payload
        if len(pl) >= 24:
            col_addr, row_addr, col_span, row_span = struct.unpack_from("<HHHH", pl, 8)
            w_raw = struct.unpack_from("<I", pl, 16)[0]
            w_pt = max(16.0, hwpunit_to_pt(w_raw, 80.0))
            bf_id = str(struct.unpack_from("<H", pl, 32)[0]) if len(pl) >= 34 else ""
        else:
            row_addr = cell_i // max(1, decl_cols)
            col_addr = cell_i % max(1, decl_cols)
            col_span = 1
            row_span = 1
            w_pt = 80.0
            bf_id = ""

        c_span = max(1, col_span)
        r_span = max(1, row_span)
        max_row = max(max_row, row_addr + r_span)
        max_col = max(max_col, col_addr + c_span)
        parsed_cells.append(
            _ParsedCell(
                row=row_addr,
                col=col_addr,
                row_span=r_span,
                col_span=c_span,
                width_pt=w_pt,
                border_fill_id=bf_id,
                para_nodes=para_nodes,
            )
        )

    n_rows = min(max_row, 5000)
    n_cols = min(max_col, 256)
    grid_text: list[list[str]] = [["" for _ in range(n_cols)] for _ in range(n_rows)]
    grid_line_lists: list[list[list[str]]] = [[[] for _ in range(n_cols)] for _ in range(n_rows)]
    col_widths: list[float] = [80.0] * n_cols
    has_merged = False
    merged_header_cols: set[int] = set()
    border_fill_ids: list[str] = []
    side_visuals: list[RawDocxBodyItem] = []
    fn_ids: list[str] = []
    en_ids: list[str] = []
    cm_ids: list[str] = []

    @dataclass
    class _DraftCell:
        row: int
        col: int
        row_span: int
        col_span: int
        text: str

    raw_cells: list[_DraftCell] = []

    for pc in parsed_cells:
        if pc.row >= n_rows or pc.col >= n_cols:
            continue
        r_span = min(pc.row_span, n_rows - pc.row)
        c_span = min(pc.col_span, n_cols - pc.col)
        if r_span > 1 or c_span > 1:
            has_merged = True
        if pc.row == 0 and c_span > 1:
            merged_header_cols.update(range(pc.col + 1, pc.col + c_span))
        if c_span == 1 and pc.width_pt > 4.0:
            col_widths[pc.col] = pc.width_pt
        if pc.border_fill_id:
            border_fill_ids.append(pc.border_fill_id)

        cell_lines: list[str] = []
        raw_aligned_lines: list[str] = []
        saved_counters = dict(ctx.list_counters)
        ctx.list_counters.clear()

        for p_node in pc.para_nodes:
            if p_node.tag_id != _TAG_PARA_HEADER:
                continue
            p_items, _, _ = _extract_hwp_paragraph(p_node, ctx)
            had_p_text = False
            for pit in p_items:
                if isinstance(pit, RawDocxParagraph):
                    if pit.text:
                        cell_lines.append(pit.text)
                        raw_aligned_lines.extend(ln.strip() for ln in pit.text.splitlines())
                        had_p_text = True
                    fn_ids.extend(pit.footnote_ids)
                    en_ids.extend(pit.endnote_ids)
                    cm_ids.extend(pit.comment_ids)
                elif isinstance(pit, RawDocxTableItem):
                    nested_rows = [
                        " | ".join(r) for r in ([pit.table.headers, *pit.table.rows]) if any(r)
                    ]
                    if nested_rows:
                        joined_nested = " / ".join(nested_rows)
                        cell_lines.append(joined_nested)
                        raw_aligned_lines.append(joined_nested)
                        had_p_text = True
                else:
                    side_visuals.append(pit)
            if not had_p_text:
                raw_aligned_lines.append("")

        ctx.list_counters.clear()
        ctx.list_counters.update(saved_counters)

        while raw_aligned_lines and not raw_aligned_lines[-1]:
            raw_aligned_lines.pop()

        cell_text = " / ".join(ln for ln in cell_lines if ln.strip()).strip()
        grid_text[pc.row][pc.col] = cell_text
        grid_line_lists[pc.row][pc.col] = raw_aligned_lines
        raw_cells.append(
            _DraftCell(
                row=pc.row,
                col=pc.col,
                row_span=r_span,
                col_span=c_span,
                text=cell_text,
            )
        )

    if not any(c.text.strip() for c in raw_cells):
        return None, side_visuals

    # 병합 없는 표에서 단일 행의 비어 있지 않은 열들이 동일 줄 수(K>=3)를 담고 있으면 행 분할
    if not has_merged:
        expanded_grid: list[list[str]] = []
        did_expand = False
        for r_idx in range(n_rows):
            row_lists = grid_line_lists[r_idx]
            non_empty_lengths = {len(lst) for lst in row_lists if any(s.strip() for s in lst)}
            non_empty_cnt = sum(1 for lst in row_lists if any(s.strip() for s in lst))
            if r_idx >= 1 and non_empty_cnt >= 2 and len(non_empty_lengths) == 1:
                k_lines = next(iter(non_empty_lengths))
                if k_lines >= 3:
                    did_expand = True
                    for line_i in range(k_lines):
                        expanded_grid.append(
                            [
                                row_lists[c][line_i] if line_i < len(row_lists[c]) else ""
                                for c in range(n_cols)
                            ]
                        )
                    continue
            expanded_grid.append(grid_text[r_idx])
        if did_expand:
            grid_text = expanded_grid
            n_rows = len(grid_text)
            raw_cells = [
                _DraftCell(row=r_i, col=c_i, row_span=1, col_span=1, text=grid_text[r_i][c_i])
                for r_i in range(n_rows)
                for c_i in range(n_cols)
            ]

    cells: list[TableCell] = [
        TableCell(
            row=rc.row,
            col=rc.col,
            row_span=rc.row_span,
            col_span=rc.col_span,
            text=rc.text,
            is_header=(rc.row == 0),
        )
        for rc in raw_cells
    ]

    # 100% 빈 여백 열 압축
    non_empty_cols = [
        c for c in range(n_cols) if any(grid_text[r][c].strip() for r in range(n_rows))
    ]
    if 0 < len(non_empty_cols) < n_cols:
        col_remap = {old_c: new_c for new_c, old_c in enumerate(non_empty_cols)}
        grid_text = [[row[c] for c in non_empty_cols] for row in grid_text]
        merged_header_cols = {col_remap[c] for c in merged_header_cols if c in col_remap}
        compacted_cells: list[TableCell] = []
        for cell in cells:
            covered_new = [
                col_remap[oc] for oc in range(cell.col, cell.col + cell.col_span) if oc in col_remap
            ]
            if not covered_new:
                continue
            new_c = covered_new[0]
            new_span = max(1, covered_new[-1] - new_c + 1)
            compacted_cells.append(cell.model_copy(update={"col": new_c, "col_span": new_span}))
        cells = compacted_cells
        n_cols = len(non_empty_cols)

    # 100% 빈 여백 행 압축
    non_empty_rows = [
        r for r in range(n_rows) if any(grid_text[r][c].strip() for c in range(n_cols))
    ]
    if 0 < len(non_empty_rows) < n_rows:
        row_remap = {old_r: new_r for new_r, old_r in enumerate(non_empty_rows)}
        grid_text = [grid_text[r] for r in non_empty_rows]
        compacted_rcells: list[TableCell] = []
        for cell in cells:
            covered_new_r = [
                row_remap[orow]
                for orow in range(cell.row, cell.row + cell.row_span)
                if orow in row_remap
            ]
            if not covered_new_r:
                continue
            new_r = covered_new_r[0]
            new_rspan = max(1, covered_new_r[-1] - new_r + 1)
            compacted_rcells.append(
                cell.model_copy(
                    update={"row": new_r, "row_span": new_rspan, "is_header": (new_r == 0)}
                )
            )
        cells = compacted_rcells
        n_rows = len(non_empty_rows)

    # 한국 공공문서 상단 배지 + 전체폭 본문 콜아웃 박스 1열 정규화
    if (
        n_cols >= 2
        and all(sum(1 for c in row if c.strip()) == 1 for row in grid_text)
        and any(cell.col_span >= n_cols for cell in cells)
    ):
        grid_text = [[next(c for c in row if c.strip())] for row in grid_text]
        n_cols = 1
        merged_header_cols.clear()
        has_merged = False
        cells = [
            TableCell(
                row=r_i,
                col=0,
                row_span=1,
                col_span=1,
                text=grid_text[r_i][0],
                is_header=(r_i == 0),
            )
            for r_i in range(n_rows)
        ]

    has_any_hdr_text = any(grid_text[0][c].strip() for c in range(n_cols))
    headers = [
        grid_text[0][c]
        if (has_any_hdr_text or grid_text[0][c] or c in merged_header_cols)
        else f"col_{c + 1}"
        for c in range(n_cols)
    ]
    data_rows = grid_text[1:] if n_rows >= 2 else []
    header_row_count = 1

    if tbl_bf_id:
        border_fill_ids.append(tbl_bf_id)
    strategy = ctx.catalog.resolve_table_border_strategy(border_fill_ids)
    strat_desc = {
        ParseStrategy.RULED_TABLE: "격자선 표",
        ParseStrategy.HORIZONTAL_RULED_TABLE: "가로선(음영/상하선) 표",
        ParseStrategy.BORDERLESS_TABLE: "무선 표",
    }.get(strategy, "HWP 표")
    merged_note = ", 병합 셀 포함" if has_merged else ""

    raw_w = min(ctx.layout.width_pt * 0.88, max(120.0, sum(col_widths[:n_cols])))
    if len(ctrl_node.payload) >= 24:
        ctrl_w = hwpunit_to_pt(struct.unpack_from("<I", ctrl_node.payload, 16)[0], 0.0)
        if ctrl_w > 40.0:
            raw_w = min(ctx.layout.width_pt * 0.92, ctrl_w)
    raw_h = min(ctx.layout.height_pt * 0.78, max(36.0, n_rows * 22.0))
    ctx.seq_counter[0] += 1
    extracted = ExtractedTable(
        table_id=f"p001_t{ctx.seq_counter[0]:02d}",
        page=1,
        bbox=make_bbox(0.0, 0.0, raw_w, raw_h, ctx.layout.width_pt, ctx.layout.height_pt),
        strategy=strategy,
        confidence=0.97,
        row_count=n_rows,
        col_count=n_cols,
        header_row_count=header_row_count,
        headers=headers,
        rows=data_rows,
        cells=cells,
        has_merged_cells=has_merged,
        caption=caption_text,
        reason=f"HWP 5.0 네이티브 {strat_desc} 직접 추출({n_rows}x{n_cols}{merged_note})",
    )
    return (
        RawDocxTableItem(
            table=extracted,
            raw_width_pt=raw_w,
            raw_height_pt=raw_h,
            footnote_ids=tuple(dict.fromkeys(fn_ids)),
            endnote_ids=tuple(dict.fromkeys(en_ids)),
            comment_ids=tuple(dict.fromkeys(cm_ids)),
        ),
        side_visuals,
    )


def _extract_gso_items(
    ctrl_node: HwpRecordNode,
    ctx: _HwpParseContext,
) -> list[RawDocxBodyItem]:
    """`gso ` 그리기 개체에서 그림(`Picture`), 글상자 텍스트, 중첩 표, 도식을 추출한다."""
    items: list[RawDocxBodyItem] = []
    shape_texts: list[str] = []
    shape_count = 0

    # 공통 개체 헤더 크기 (offset 16=width, 20=height)
    gso_w = 220.0
    gso_h = 140.0
    if len(ctrl_node.payload) >= 24:
        w_cand = hwpunit_to_pt(struct.unpack_from("<I", ctrl_node.payload, 16)[0], 0.0)
        h_cand = hwpunit_to_pt(struct.unpack_from("<I", ctrl_node.payload, 20)[0], 0.0)
        if w_cand > 0.0 and h_cand > 0.0:
            gso_w = clamp(w_cand, 0.5, ctx.layout.width_pt)
            gso_h = clamp(h_cand, 0.5, ctx.layout.height_pt)

    seen_pic_bin_ids: set[int] = set()
    has_container = False
    has_ole = False

    def _walk_gso(nodes: list[HwpRecordNode]) -> None:
        nonlocal shape_count, has_container, has_ole
        idx = 0
        while idx < len(nodes):
            nd = nodes[idx]
            if nd.tag_id == _TAG_SHAPE_COMPONENT:
                cid = _decode_ctrl_id(nd.payload)
                if cid == "$con":
                    has_container = True
                elif cid == "$ole":
                    has_ole = True
                elif cid not in ("$pic", ""):
                    shape_count += 1
                _walk_gso(nd.children)
            elif nd.tag_id == _TAG_SHAPE_COMPONENT_OLE:
                has_ole = True
            elif nd.tag_id == _TAG_SHAPE_COMPONENT_PICTURE:
                bin_id = 0
                if len(nd.payload) >= 73:
                    bin_id = struct.unpack_from("<H", nd.payload, 71)[0]
                if bin_id > 0 and bin_id not in seen_pic_bin_ids:
                    seen_pic_bin_ids.add(bin_id)
                    found = ctx.read_bin_data(bin_id)
                    if found is not None:
                        stream_path, raw_bytes, px_w, px_h = found
                        ext = Path(stream_path).suffix.lower() or ".bin"
                        ctx.seq_counter[0] += 1
                        items.append(
                            RawPptxImage(
                                shape_id=str(ctx.seq_counter[0]),
                                name=Path(stream_path).name,
                                alt_text="",
                                media_part=stream_path,
                                extension=ext,
                                rect_pt=(0.0, 0.0, gso_w, gso_h),
                                raw_bytes=raw_bytes,
                                pixel_width=px_w,
                                pixel_height=px_h,
                            )
                        )
            elif nd.tag_id == _TAG_LIST_HEADER:
                # 글상자 내부 문단들 (같은 부모의 뒤따르는 PARA_HEADER 소비)
                idx += 1
                while idx < len(nodes) and nodes[idx].tag_id == _TAG_PARA_HEADER:
                    p_items, _, _ = _extract_hwp_paragraph(nodes[idx], ctx, in_textbox=True)
                    for pit in p_items:
                        if isinstance(pit, RawDocxParagraph) and pit.text:
                            shape_texts.append(pit.text)
                        items.append(pit)
                    idx += 1
                continue
            elif nd.tag_id == _TAG_CTRL_HEADER:
                sub_cid = _decode_ctrl_id(nd.payload)
                if sub_cid == "tbl ":
                    tbl_it, side_vis = _extract_hwp_table(nd, ctx)
                    if tbl_it is not None:
                        items.append(tbl_it)
                    items.extend(side_vis)
                elif sub_cid == "gso ":
                    items.extend(_extract_gso_items(nd, ctx))
                else:
                    _walk_gso(nd.children)
            elif nd.children:
                _walk_gso(nd.children)
            idx += 1

    _walk_gso(ctrl_node.children)

    # OLE 개체($ole / HWPTAG_SHAPE_COMPONENT_OLE 0x54) 감지
    if has_ole and not items:
        ctx.seq_counter[0] += 1
        items.append(
            RawPptxOleObject(
                shape_id=str(ctx.seq_counter[0]),
                name=f"HWP OLE {ctx.seq_counter[0]}",
                prog_id="HWP_OLE",
                rect_pt=(0.0, 0.0, gso_w, gso_h),
                preview_image=None,
            )
        )

    if (has_container and shape_count >= 2) or shape_count >= 3:
        is_tiny_ornament = gso_w < 48.0 or gso_h < 28.0
        is_title_banner = (
            gso_h < 65.0
            and (gso_w / max(gso_h, 1.0)) >= 7.0
            and shape_count <= 4
            and len(shape_texts) <= 2
        )
        if not is_tiny_ornament and not is_title_banner:
            ctx.seq_counter[0] += 1
            hints = tuple(dict.fromkeys(t.splitlines()[0][:60] for t in shape_texts if t))[:20]
            items.append(
                RawDocxVectorDiagram(
                    diagram_id=f"d_{ctx.seq_counter[0]}",
                    name=f"HWP Diagram {ctx.seq_counter[0]}",
                    width_pt=gso_w,
                    height_pt=gso_h,
                    shape_count=shape_count,
                    text_hints=hints,
                )
            )
    return items


def _extract_hwp_hyperlink_url(payload: bytes) -> str:
    """`%hlk` 컨트롤 헤더 파라미터에서 하이퍼링크 URL을 추출한다."""
    if len(payload) <= 10:
        return ""
    decoded = payload[4:].decode("utf-16le", errors="ignore")
    m = re.search(r"(https?://[^\s;|\x00]+|mailto:[^\s;|\x00]+)", decoded.replace("\\:", ":"))
    if m:
        return m.group(1).rstrip(";,)")
    return ""


def _extract_hwp_paragraph(
    p_node: HwpRecordNode,
    ctx: _HwpParseContext,
    *,
    in_textbox: bool = False,
) -> tuple[list[RawDocxBodyItem], bool, tuple[int, int] | None]:
    """`HWPTAG_PARA_HEADER` 노드에서 본문·수식·표·그림·각주·쪽나누기·줄좌표를 추출한다."""
    pl = p_node.payload
    para_shape_id = "0"
    style_id = "0"
    page_break_before = False
    if len(pl) >= 12:
        para_shape_id = str(struct.unpack_from("<H", pl, 8)[0])
        style_id = str(pl[10])
        col_div = pl[11]
        page_break_before = bool(col_div & 0x04)

    style_def = ctx.catalog.styles.get(style_id)
    para_prop = ctx.catalog.para_props.get(para_shape_id)
    if para_prop is None and style_def is not None:
        para_prop = ctx.catalog.para_props.get(style_def.para_pr_id)

    heading_lvl = style_def.heading_level if style_def is not None else None
    is_list_item = False
    is_ordered_list = False
    list_level = 0
    list_marker = ""
    pending_num_id: str | None = None

    if para_prop is not None:
        if para_prop.heading_type == "OUTLINE" and heading_lvl is None:
            heading_lvl = min(6, para_prop.heading_level + 1)
        elif para_prop.heading_type == "NUMBER" and heading_lvl is None:
            is_list_item = True
            is_ordered_list = True
            list_level = para_prop.heading_level
            pending_num_id = para_prop.heading_id_ref or "1"
        elif para_prop.heading_type == "BULLET" and heading_lvl is None:
            is_list_item = True
            list_level = para_prop.heading_level

    text_pieces: list[str] = []
    attachments: list[RawDocxBodyItem] = []
    char_shape_ids: list[str] = []
    seg_pos: tuple[int, int] | None = None
    has_math = False
    math_chars = 0
    fn_ids: list[str] = []
    en_ids: list[str] = []
    cm_ids: list[str] = []
    hyperlink_urls: list[str] = []

    for ch in p_node.children:
        tag = ch.tag_id
        if tag == _TAG_PARA_TEXT:
            decoded = _decode_para_text(ch.payload)
            if decoded:
                text_pieces.append(decoded)
        elif tag == _TAG_PARA_CHAR_SHAPE:
            cnt = len(ch.payload) // 8
            for i in range(cnt):
                _pos, csid = struct.unpack_from("<II", ch.payload, i * 8)
                char_shape_ids.append(str(csid))
        elif tag == _TAG_PARA_LINE_SEG and seg_pos is None and len(ch.payload) >= 24:
            # 0..4: text_start_pos, 4..8: vert_pos, 16..20: horz_pos (int32 HWPUNIT)
            v_pos = struct.unpack_from("<i", ch.payload, 4)[0]
            h_pos = struct.unpack_from("<i", ch.payload, 16)[0]
            if v_pos >= 0:
                seg_pos = (v_pos, max(0, h_pos))
        elif tag == _TAG_CTRL_HEADER:
            cid = _decode_ctrl_id(ch.payload)
            if cid == "secd":
                for sub in ch.children:
                    if sub.tag_id == _TAG_PAGE_DEF:
                        _parse_page_def(sub.payload, ctx.layout)
            elif cid == "cold":
                _parse_cold(ch.payload, ctx.layout)
            elif cid == "head":
                hdr_txt = _extract_sublist_plain_text_hwp(ch.children)
                if hdr_txt and hdr_txt not in ctx.headers:
                    ctx.headers.append(hdr_txt)
            elif cid == "foot":
                ftr_txt = _extract_sublist_plain_text_hwp(ch.children)
                if ftr_txt and ftr_txt not in ctx.footers:
                    ctx.footers.append(ftr_txt)
            elif cid == "fn  ":
                fn_txt = _extract_sublist_plain_text_hwp(ch.children)
                if fn_txt:
                    fid = str(len(ctx.footnotes) + 1)
                    ctx.footnotes[fid] = fn_txt
                    text_pieces.append(f"[^{fid}]")
                    fn_ids.append(fid)
                    ctx.pending_fn.append(fid)
            elif cid == "en  ":
                en_txt = _extract_sublist_plain_text_hwp(ch.children)
                if en_txt:
                    eid = str(len(ctx.endnotes) + 1)
                    ctx.endnotes[eid] = en_txt
                    text_pieces.append(f"[^e{eid}]")
                    en_ids.append(eid)
                    ctx.pending_en.append(eid)
            elif cid == "tcmt":
                cm_txt = _extract_sublist_plain_text_hwp(ch.children)
                if cm_txt:
                    cmid = str(len(ctx.comments) + 1)
                    ctx.comments[cmid] = DocxCommentEntry(
                        comment_id=cmid,
                        author="HWP 숨은설명",
                        initials="",
                        date="",
                        text=cm_txt,
                    )
                    cm_ids.append(cmid)
                    ctx.pending_cm.append(cmid)
            elif cid == "tbl ":
                tbl_it, side_vis = _extract_hwp_table(ch, ctx)
                if tbl_it is not None:
                    attachments.append(tbl_it)
                attachments.extend(side_vis)
            elif cid == "gso ":
                attachments.extend(_extract_gso_items(ch, ctx))
            elif cid == "eqed":
                for sub in ch.children:
                    if sub.tag_id == _TAG_EQEDIT and len(sub.payload) >= 6:
                        slen = struct.unpack_from("<H", sub.payload, 4)[0]
                        script = sub.payload[6 : 6 + slen * 2].decode("utf-16le", errors="replace")
                        eq_txt = normalize_hwp_equation_script(script)
                        if eq_txt:
                            if text_pieces and not text_pieces[-1].endswith((" ", "\n")):
                                text_pieces.append(" ")
                            text_pieces.append(eq_txt)
                            has_math = True
                            math_chars += len(eq_txt)
            elif cid == "%hlk":
                url = _extract_hwp_hyperlink_url(ch.payload)
                if url:
                    hyperlink_urls.append(url)
            elif cid == "tdut" and len(ch.payload) >= 8:
                # 덧말 (tdut): mainText + subText
                m_txt, next_off = _read_u16_utf16(ch.payload, 4)
                s_txt, _ = _read_u16_utf16(ch.payload, next_off)
                dut_str = f"{m_txt}({s_txt})" if (m_txt and s_txt) else (m_txt or s_txt)
                if dut_str:
                    text_pieces.append(dut_str)
            elif cid == "tcps" and len(ch.payload) >= 6:
                # 글자겹침 (tcps): uint16 길이 + UTF-16LE 겹침 문자열
                cps_txt, _ = _read_u16_utf16(ch.payload, 4)
                if cps_txt:
                    text_pieces.append(cps_txt)

    raw_txt = "".join(text_pieces)
    for url in hyperlink_urls:
        if url not in raw_txt:
            raw_txt = f"{raw_txt} ({url})".strip()
    cleaned_lines = [ln.strip() for ln in raw_txt.splitlines()]
    txt = "\n".join(ln for ln in cleaned_lines if ln).strip()

    out_items: list[RawDocxBodyItem] = []
    if txt:
        if pending_num_id is not None:
            list_marker = _format_hwpx_list_marker(
                num_id=pending_num_id,
                list_level=list_level,
                catalog=ctx.catalog,
                list_counters=ctx.list_counters,
            )

        if not char_shape_ids and style_def is not None:
            char_shape_ids = [style_def.char_pr_id]
        c_props = [
            ctx.catalog.char_props[cid] for cid in char_shape_ids if cid in ctx.catalog.char_props
        ]
        eff_font = (
            sorted(cp.font_size_pt for cp in c_props)[len(c_props) // 2]
            if c_props
            else ctx.catalog.default_font_size_pt
        )
        is_b = bool(c_props) and all(cp.is_bold for cp in c_props)
        is_i = bool(c_props) and all(cp.is_italic for cp in c_props)
        is_mono = bool(c_props) and all(cp.is_monospace for cp in c_props)
        is_code = bool(style_def is not None and style_def.is_code_style) or is_mono
        prose_chars = max(0, len(txt) - math_chars)
        is_formula_only = has_math and prose_chars == 0

        if (
            not is_list_item
            and heading_lvl is None
            and txt.startswith(_BULLET_CHARS)
            and len(txt) >= 3
            and txt[1:2] in (" ", "\t")
        ):
            is_list_item = True

        out_items.append(
            RawDocxParagraph(
                text=txt,
                style_id=style_id,
                style_name=style_def.name if style_def is not None else style_id,
                heading_level=heading_lvl,
                is_list_item=is_list_item,
                is_ordered_list=is_ordered_list,
                list_level=list_level if is_list_item else 0,
                list_marker=list_marker,
                font_size_pt=eff_font,
                is_bold=is_b,
                is_italic=is_i,
                is_code_or_formula=(is_code or is_formula_only),
                has_math=has_math,
                in_textbox=in_textbox,
                footnote_ids=tuple(dict.fromkeys(fn_ids)),
                endnote_ids=tuple(dict.fromkeys(en_ids)),
                comment_ids=tuple(dict.fromkeys(cm_ids)),
            )
        )

    out_items.extend(attachments)
    return out_items, page_break_before, seg_pos


def _estimate_hwp_item_height_pt(item: RawDocxBodyItem, usable_w: float) -> float:
    if isinstance(item, RawDocxParagraph):
        font = item.font_size_pt or 10.0
        chars_per_line = max(20.0, usable_w / max(6.0, font * 0.55))
        lines = sum(
            max(1, int(len(ln) / chars_per_line) + 1)
            for ln in (item.text.splitlines() or [item.text])
        )
        spacing = 10.0 if item.heading_level is not None else 5.0
        return lines * (font * 1.45) + spacing
    if isinstance(item, RawDocxTableItem):
        return item.raw_height_pt + 14.0
    if isinstance(item, RawPptxImage):
        return max(36.0, item.rect_pt[3] - item.rect_pt[1]) + 10.0
    if isinstance(item, RawPptxOleObject):
        return max(80.0, item.rect_pt[3] - item.rect_pt[1]) + 12.0
    if isinstance(item, RawDocxVectorDiagram):
        return max(60.0, item.height_pt) + 12.0
    return 24.0


def _relabel_to_hwp(text: str) -> str:
    return (
        text.replace("DOCX ", "HWP ")
        .replace("Word 섹션", "HWP 섹션")
        .replace("Word 본문", "HWP 본문")
        .replace("WordprocessingML", "HWP 5.0")
        .replace("OOXML", "HWP 5.0")
    )


def extract_hwp_pages(cfb: CfbFile) -> tuple[list[RawDocxPage], OleSummaryInfo, str]:
    """HWP 5.0 CFB 컨테이너에서 페이지 IR(`RawDocxPage`), 요약 메타데이터, 버전을 추출한다."""
    fh = cfb.read_stream("FileHeader", max_bytes=256)
    if not fh or len(fh) < 40 or not fh.startswith(b"HWP Document File"):
        raise ParserInputError(
            f"유효한 HWP 5.0 바이너리 문서(FileHeader)가 아니다: {cfb.source_path.name}"
        )

    rev, build, minor, major = struct.unpack_from("<BBBB", fh, 32)
    flags = struct.unpack_from("<I", fh, 36)[0]
    compressed = bool(flags & 0x01)
    encrypted = bool(flags & 0x02)
    distributed = bool(flags & 0x04)
    drm = bool(flags & 0x10) or bool(flags & 0x100) or bool(flags & 0x400)

    if encrypted:
        raise ParserInputError(f"암호화된 HWP 5.0 문서다: {cfb.source_path.name}")
    if drm:
        raise ParserInputError(f"DRM으로 보호된 HWP 5.0 문서다: {cfb.source_path.name}")

    all_streams = cfb.list_streams()
    if distributed or any(s.lower().startswith("viewtext/section") for s in all_streams):
        raise ParserInputError(
            f"배포용(ViewText 암호화)으로 잠긴 HWP 5.0 문서다: {cfb.source_path.name}"
        )

    section_streams = sorted(
        (s for s in all_streams if s.lower().startswith("bodytext/section")),
        key=lambda s: int("".join(ch for ch in s.rsplit("/", 1)[-1] if ch.isdigit()) or "0"),
    )
    if not section_streams:
        raise ParserInputError(
            f"HWP 5.0 본문 스트림(BodyText/Section*)을 찾을 수 없다: {cfb.source_path.name}"
        )

    doc_info_raw = cfb.read_stream("DocInfo") or b""
    doc_info_bytes = _decompress_hwp_stream(doc_info_raw, compressed=compressed)
    catalog, bin_entries = _parse_doc_info(doc_info_bytes)
    summary = read_ole_summary_info(cfb, ("\x05HwpSummaryInformation", "\x05SummaryInformation"))

    layout = HwpxSectionLayout()
    ctx = _HwpParseContext(
        cfb=cfb,
        compressed=compressed,
        catalog=catalog,
        bin_entries=bin_entries,
        layout=layout,
        list_counters={},
        seq_counter=[0],
        note_counter=[0],
        headers=[],
        footers=[],
        footnotes={},
        endnotes={},
        comments={},
    )

    pages: list[RawDocxPage] = []

    def _new_page() -> RawDocxPage:
        pg = RawDocxPage(
            page_number=len(pages) + 1,
            width_pt=layout.width_pt,
            height_pt=layout.height_pt,
            margin_top_pt=layout.margin_top_pt,
            margin_bottom_pt=layout.margin_bottom_pt,
            margin_left_pt=layout.margin_left_pt,
            margin_right_pt=layout.margin_right_pt,
            column_count=layout.column_count,
            column_space_pt=layout.column_space_pt,
            headers=list(ctx.headers),
            footers=list(ctx.footers),
        )
        pages.append(pg)
        return pg

    def _sync_layout(pg: RawDocxPage) -> None:
        pg.width_pt = layout.width_pt
        pg.height_pt = layout.height_pt
        pg.margin_top_pt = layout.margin_top_pt
        pg.margin_bottom_pt = layout.margin_bottom_pt
        pg.margin_left_pt = layout.margin_left_pt
        pg.margin_right_pt = layout.margin_right_pt
        pg.column_count = max(pg.column_count, layout.column_count)
        pg.column_space_pt = layout.column_space_pt
        for h in ctx.headers:
            if h not in pg.headers:
                pg.headers.append(h)
        for f in ctx.footers:
            if f not in pg.footers:
                pg.footers.append(f)

    emitted_fn: set[str] = set()
    emitted_en: set[str] = set()
    emitted_cm: set[str] = set()

    def _flush_pending_notes(pg: RawDocxPage) -> None:
        if ctx.pending_fn:
            for fid in ctx.pending_fn:
                if fid in ctx.footnotes and fid not in emitted_fn:
                    emitted_fn.add(fid)
                    pg.footnotes.append((fid, ctx.footnotes[fid]))
            ctx.pending_fn.clear()
        if ctx.pending_en:
            for eid in ctx.pending_en:
                if eid in ctx.endnotes and eid not in emitted_en:
                    emitted_en.add(eid)
                    pg.endnotes.append((eid, ctx.endnotes[eid]))
            ctx.pending_en.clear()
        if ctx.pending_cm:
            for cmid in ctx.pending_cm:
                if cmid in ctx.comments and cmid not in emitted_cm:
                    emitted_cm.add(cmid)
                    pg.comments.append(ctx.comments[cmid])
            ctx.pending_cm.clear()

    multi_section = len(section_streams) > 1
    cur_page: RawDocxPage | None = None
    cur_y_pt = 0.0

    # 섹션별 스트리밍 파싱 (전체 레코드 트리를 메모리에 누적하지 않음)
    for sec_idx, sname in enumerate(section_streams):
        raw_sec = cfb.read_stream(sname) or b""
        sec_bytes = _decompress_hwp_stream(raw_sec, compressed=compressed)
        del raw_sec
        has_explicit_breaks = multi_section or _has_explicit_page_break(sec_bytes)

        if cur_page is None or (sec_idx > 0 and cur_page.items):
            cur_page = _new_page()
            cur_y_pt = 0.0

        prev_vert: int | None = None
        prev_horz: int | None = None

        for nd in _iter_root_record_nodes(sec_bytes):
            if nd.tag_id != _TAG_PARA_HEADER:
                continue
            p_items, pb_before, seg_pos = _extract_hwp_paragraph(nd, ctx)
            _sync_layout(cur_page)

            lineseg_new_page = False
            if seg_pos is not None:
                v_pos, h_pos = seg_pos
                if (
                    not has_explicit_breaks
                    and prev_vert is not None
                    and prev_horz is not None
                    and v_pos + 8000 < prev_vert
                    and h_pos <= prev_horz + 1000
                ):
                    lineseg_new_page = True
                prev_vert, prev_horz = v_pos, h_pos

            if (pb_before or lineseg_new_page) and cur_page.items:
                cur_page = _new_page()
                cur_y_pt = 0.0

            _flush_pending_notes(cur_page)

            usable_w = max(120.0, layout.width_pt - layout.margin_left_pt - layout.margin_right_pt)
            usable_h = max(200.0, layout.height_pt - layout.margin_top_pt - layout.margin_bottom_pt)

            for it in p_items:
                est_h = _estimate_hwp_item_height_pt(it, usable_w)
                if (
                    not has_explicit_breaks
                    and prev_vert is None
                    and cur_page.items
                    and cur_y_pt + est_h > usable_h * 1.12
                ):
                    cur_page = _new_page()
                    cur_y_pt = 0.0
                cur_page.items.append(it)
                cur_y_pt += est_h

        del sec_bytes

    if not pages:
        pages.append(_new_page())
    _flush_pending_notes(pages[-1])

    version_str = f"Hancom HWP {major}.{minor}.{build}.{rev}"
    return pages, summary, version_str


class HwpDocumentAdapter:
    """구형 바이너리 한글 5.0(`.hwp`, OLE2 CFB)용 `DocumentFormatAdapter` 구현체."""

    @property
    def format(self) -> SourceFormat:
        return SourceFormat.HWP

    @property
    def extensions(self) -> tuple[str, ...]:
        return (".hwp",)

    def _analyze_document(
        self,
        source_path: Path,
        options: ParseOptions,
        *,
        layout_detector: PageLayoutDetector | None = None,
    ) -> tuple[list[AnalyzedDocxPage], RoutingPlan]:
        with CfbFile(source_path) as cfb:
            raw_pages, summary, version_str = extract_hwp_pages(cfb)
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
                v.model_copy(update={"reason": _relabel_to_hwp(v.reason)}) for v in ap.visuals
            ]
            ap.route = ap.route.model_copy(
                update={
                    "reasons": [_relabel_to_hwp(r) for r in ap.route.reasons],
                    "regions": [
                        r.model_copy(update={"reason": _relabel_to_hwp(r.reason)})
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
                    if blk.heading_level == 1 and blk.text.strip():
                        title = blk.text.strip()[:120]
                        break
                if title:
                    break

        metadata = DocumentMetadata(
            source_path=source_path.name,
            format=SourceFormat.HWP,
            file_size_bytes=file_size,
            sha256=file_sha256,
            page_count=total_pages,
            title=title,
            author=summary.author,
            creator=summary.creator_app or version_str,
        )
        plan = build_docx_routing_plan(
            metadata,
            analyzed_pages,
            body_font_size=body_font,
        )
        return analyzed_pages, plan

    def inspect(
        self,
        source_path: Path,
        *,
        options: ParseOptions,
        layout_detector: PageLayoutDetector | None = None,
        page_renderer: PageRenderer | None = None,
    ) -> RoutingPlan:
        """HWP 5.0 문서를 진단하여 페이지별 구조 분류 및 파싱 전략 계획을 반환한다."""
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
        """HWP 5.0 문서의 본문·개요·표·수식·각주·이미지를 통합 IR로 파싱한다."""
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
    "HwpBinDataEntry",
    "HwpDocumentAdapter",
    "extract_hwp_pages",
]
