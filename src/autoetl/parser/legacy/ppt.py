"""구형 바이너리 MS PowerPoint 97-2003(`.ppt`, OLE2 CFB) 하이브리드 파서 및 어댑터.

2계층 전략으로 동작한다:
1. Tier-1 (선택적 LibreOffice 승격): 시스템 PATH에 `soffice`/`libreoffice`가 있고
   `use_libreoffice=True`이면 `.ppt`를 임시 `.pptx`로 변환해 `PptxDocumentAdapter`로 파싱한다.
2. Tier-2 (무의존성 순수 Python MS-PPT 폴백): LibreOffice가 없거나 변환이 실패하면
   `CfbFile`로 `Current User`·`PowerPoint Document`의 `UserEditAtom`·`PersistDirectoryAtom`과
   `SlideListWithText`·`Slide`·`Notes` 레코드 트리 및 `Pictures` BLIP 이미지를 직접 해석한다.
"""

from __future__ import annotations

import hashlib
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
from autoetl.parser.image_io import DecodedImage, decode_png
from autoetl.parser.legacy.cfb import (
    CfbFile,
    OleSummaryInfo,
    extract_officeart_blips,
    read_ole_summary_info,
)
from autoetl.parser.legacy.converter import convert_legacy_office_document, find_libreoffice_binary
from autoetl.parser.pptx.adapter import (
    PptxDocumentAdapter,
    _apply_layout_detection_to_slide,
    _extract_crop_for_pptx_visual,
    _find_surrounding_slide_context,
    _run_vision_on_pptx_region,
)
from autoetl.parser.pptx.elements import (
    RawPptxImage,
    RawPptxParagraph,
    RawPptxShape,
    RawPptxTable,
    RawSlideData,
)
from autoetl.parser.pptx.ooxml import make_bbox
from autoetl.parser.pptx.router import (
    AnalyzedSlide,
    analyze_slide,
    build_pptx_routing_plan,
    estimate_deck_body_font_size,
    find_repeating_pptx_headers_footers,
    find_repeating_pptx_logos,
)

# MS-PPT 레코드 타입 상수
_RT_DOCUMENT = 0x03E8
_RT_DOCUMENT_ATOM = 0x03E9
_RT_SLIDE = 0x03EE
_RT_SLIDE_ATOM = 0x03EF
_RT_SLIDE_PERSIST_ATOM = 0x03F3
_RT_SLIDE_LIST_WITH_TEXT = 0x0FF0
_RT_USER_EDIT_ATOM = 0x0FF5
_RT_TEXT_HEADER_ATOM = 0x0F9F
_RT_TEXT_CHARS_ATOM = 0x0FA0
_RT_TEXT_BYTES_ATOM = 0x0FA8
_RT_CSTRING = 0x0FBA
_RT_HEADERS_FOOTERS = 0x0FD9
_RT_HEADERS_FOOTERS_ATOM = 0x0FDA
_RT_PERSIST_DIRECTORY_ATOM = 0x1772

# OfficeArt(Escher) 레코드 타입 상수
_RT_ESCHER_SPGR_CONTAINER = 0xF003
_RT_ESCHER_SP_CONTAINER = 0xF004
_RT_ESCHER_OPT = 0xF00B
_RT_ESCHER_CLIENT_TEXTBOX = 0xF00D
_RT_ESCHER_CHILD_ANCHOR = 0xF00F
_RT_ESCHER_TERTIARY_OPT = 0xF122

_DEFAULT_SLIDE_W = 720.0
_DEFAULT_SLIDE_H = 540.0


def _iter_ppt_records(
    buf: bytes,
    start: int = 0,
    end: int | None = None,
) -> list[tuple[int, int, int, int, int]]:
    """MS-PPT 레코드 범위에서 `(ver, inst, type, start, end)` 튜플 목록을 반환한다."""
    limit = len(buf) if end is None else min(end, len(buf))
    out: list[tuple[int, int, int, int, int]] = []
    pos = start
    while pos + 8 <= limit:
        ver_inst, rec_type, rec_len = struct.unpack_from("<HHI", buf, pos)
        rec_ver = ver_inst & 0x0F
        rec_inst = (ver_inst >> 4) & 0x0FFF
        p_start = pos + 8
        p_end = p_start + rec_len
        if p_end > limit or rec_len < 0:
            break
        out.append((rec_ver, rec_inst, rec_type, p_start, p_end))
        pos = p_end
    return out


def _read_current_user_edit_offset(cfb: CfbFile) -> int | None:
    """`Current User` 스트림에서 최신 `UserEditAtom` 오프셋을 읽고 암호화 여부를 검증한다."""
    cu = cfb.read_stream("Current User", max_bytes=4096)
    if not cu or len(cu) < 28:
        return None
    header_token = struct.unpack_from("<I", cu, 12)[0]
    if header_token == 0xF3D1C4DF:
        raise ParserInputError(f"암호화된 MS PowerPoint(.ppt) 문서다: {cfb.source_path.name}")
    return int(struct.unpack_from("<I", cu, 16)[0])


def _resolve_live_persist_map(
    ppt_doc: bytes,
    latest_edit_off: int | None,
) -> tuple[int | None, dict[int, int]]:
    """`UserEditAtom` 체인을 따라 최신 `docPersistIdRef`와 `persistId -> offset` 맵을 복원한다."""
    if latest_edit_off is None or latest_edit_off + 36 > len(ppt_doc):
        return None, {}

    edit_offsets: list[int] = []
    visited: set[int] = set()
    cur = latest_edit_off
    while 0 <= cur <= len(ppt_doc) - 36 and cur not in visited and len(edit_offsets) < 256:
        visited.add(cur)
        _vi, rec_type, rec_len = struct.unpack_from("<HHI", ppt_doc, cur)
        if rec_type != _RT_USER_EDIT_ATOM or rec_len < 28:
            break
        edit_offsets.append(cur)
        offset_last_edit = struct.unpack_from("<I", ppt_doc, cur + 16)[0]
        if offset_last_edit == 0 or offset_last_edit >= len(ppt_doc):
            break
        cur = offset_last_edit

    if not edit_offsets:
        return None, {}

    persist_map: dict[int, int] = {}
    doc_persist_id: int | None = None
    for e_off in reversed(edit_offsets):
        pdir_off = struct.unpack_from("<I", ppt_doc, e_off + 20)[0]
        doc_persist_id = int(struct.unpack_from("<I", ppt_doc, e_off + 24)[0])
        if pdir_off + 8 <= len(ppt_doc):
            _vi, p_type, p_len = struct.unpack_from("<HHI", ppt_doc, pdir_off)
            if p_type == _RT_PERSIST_DIRECTORY_ATOM:
                p_pos = pdir_off + 8
                p_end = min(len(ppt_doc), p_pos + p_len)
                while p_pos + 4 <= p_end:
                    entry = struct.unpack_from("<I", ppt_doc, p_pos)[0]
                    p_pos += 4
                    start_pid = entry & 0x000FFFFF
                    cnt = (entry >> 20) & 0x0FFF
                    for k in range(cnt):
                        if p_pos + 4 > p_end:
                            break
                        rec_off = struct.unpack_from("<I", ppt_doc, p_pos)[0]
                        p_pos += 4
                        persist_map[start_pid + k] = rec_off

    return doc_persist_id, persist_map


def _decode_ppt_text_atom(rec_type: int, raw: bytes) -> str:
    if rec_type == _RT_TEXT_CHARS_ATOM:
        if len(raw) % 2 == 1:
            raw = raw[:-1]
        text = raw.decode("utf-16le", errors="replace")
    else:
        text = raw.decode("cp1252", errors="replace")
    return text.replace("\x0b", "\n").replace("\r", "\n")


def _parse_headers_footers_container(ppt_doc: bytes, start: int, end: int) -> dict[str, str]:
    """`HeadersFootersContainer`(0x0FD9)에서 날짜(`dt`)·머리글(`hdr`)·바닥글(`ftr`)을 읽는다."""
    flags: int | None = None
    strings: dict[int, str] = {}
    for _cv, ci, crt, cps, cpe in _iter_ppt_records(ppt_doc, start, end):
        if crt == _RT_HEADERS_FOOTERS_ATOM and cpe - cps >= 4:
            _fmt, flags = struct.unpack_from("<HH", ppt_doc, cps)
        elif crt == _RT_CSTRING and cpe > cps:
            raw = ppt_doc[cps:cpe]
            if len(raw) % 2 == 1:
                raw = raw[:-1]
            txt = raw.decode("utf-16le", errors="replace").replace("\x00", "").strip()
            if txt:
                strings[ci] = txt

    out: dict[str, str] = {}
    # HeadersFootersAtom flags: bit0=fHasDate, bit2=fHasUserDate, bit4=fHasHeader, bit5=fHasFooter
    if 0 in strings and (flags is None or (flags & 0x05)):
        out["dt"] = strings[0]
    if 1 in strings and (flags is None or (flags & 0x10)):
        out["hdr"] = strings[1]
    if 2 in strings and (flags is None or (flags & 0x20)):
        out["ftr"] = strings[2]
    return out


def _is_escher_table_sp(ppt_doc: bytes, sp_start: int, sp_end: int) -> bool:
    """`SpContainer`가 `fIsTable`(OPT/TertiaryFOPT 속성 ID 927 bit0) 표 그룹 헤더인지 판별한다."""
    for _cv, ci, crt, cps, cpe in _iter_ppt_records(ppt_doc, sp_start, sp_end):
        if crt in (_RT_ESCHER_OPT, _RT_ESCHER_TERTIARY_OPT):
            for k in range(ci):
                off = cps + k * 6
                if off + 6 > cpe:
                    break
                pid, val = struct.unpack_from("<HI", ppt_doc, off)
                if (pid & 0x3FFF) == 927 and (val & 0x01):
                    return True
    return False


def _cluster_coords(vals: list[int], tol: int = 12) -> list[int]:
    """근접한 Escher 좌표들을 하나의 행/열 경계선으로 군집화한다."""
    if not vals:
        return []
    sorted_vals = sorted(vals)
    clusters: list[int] = [sorted_vals[0]]
    for v in sorted_vals[1:]:
        if v - clusters[-1] > tol:
            clusters.append(v)
    return clusters


def _extract_escher_tables(
    ppt_doc: bytes,
    start: int,
    end: int,
    *,
    slide_number: int,
    slide_w: float,
    slide_h: float,
    depth: int = 0,
) -> tuple[list[RawPptxTable], list[tuple[int, int]]]:
    """슬라이드 Escher 트리에서 `fIsTable` 표 그룹을 구조화된 `RawPptxTable`로 복원한다."""
    if depth > 8:
        return [], []
    tables: list[RawPptxTable] = []
    consumed_ranges: list[tuple[int, int]] = []

    for rec_ver, _inst, rec_type, p_start, p_end in _iter_ppt_records(ppt_doc, start, end):
        if rec_type == _RT_ESCHER_SPGR_CONTAINER:
            children = _iter_ppt_records(ppt_doc, p_start, p_end)
            if (
                children
                and children[0][2] == _RT_ESCHER_SP_CONTAINER
                and _is_escher_table_sp(ppt_doc, children[0][3], children[0][4])
            ):
                cell_entries: list[tuple[int, int, int, int, str]] = []
                for _cv, _ci, crt, cps, cpe in children[1:]:
                    if crt != _RT_ESCHER_SP_CONTAINER:
                        continue
                    anchor: tuple[int, int, int, int] | None = None
                    has_textbox = False
                    cell_texts: list[str] = []
                    for _sv, _si, srt, sps, spe in _iter_ppt_records(ppt_doc, cps, cpe):
                        if srt == _RT_ESCHER_CHILD_ANCHOR and spe - sps >= 16:
                            x0, y0, x1, y1 = struct.unpack_from("<4i", ppt_doc, sps)
                            anchor = (x0, y0, x1, y1)
                        elif srt == _RT_ESCHER_CLIENT_TEXTBOX:
                            has_textbox = True
                            for _, t_str in _extract_texts_from_container(ppt_doc, sps, spe):
                                cell_texts.append(t_str)
                    if anchor is not None and anchor[2] > anchor[0] and anchor[3] > anchor[1]:
                        txt = " / ".join(t.strip() for t in cell_texts if t.strip()).strip()
                        if has_textbox or txt:
                            cell_entries.append((anchor[0], anchor[1], anchor[2], anchor[3], txt))

                if cell_entries and any(c[4] for c in cell_entries):
                    col_starts = _cluster_coords([c[0] for c in cell_entries])
                    row_starts = _cluster_coords([c[1] for c in cell_entries])
                    n_rows = min(len(row_starts), 200)
                    n_cols = min(len(col_starts), 64)
                    if n_rows >= 1 and n_cols >= 1:
                        grid: list[list[str]] = [["" for _ in range(n_cols)] for _ in range(n_rows)]
                        cells: list[TableCell] = []
                        has_merged = False

                        def _nearest_idx(coords: list[int], val: int) -> int:
                            return min(range(len(coords)), key=lambda i: abs(coords[i] - val))

                        for x0, y0, x1, y1, txt in cell_entries:
                            r = _nearest_idx(row_starts[:n_rows], y0)
                            c = _nearest_idx(col_starts[:n_cols], x0)
                            r_end = sum(1 for rv in row_starts[:n_rows] if rv < y1 - 12)
                            c_end = sum(1 for cv in col_starts[:n_cols] if cv < x1 - 12)
                            r_span = max(1, min(n_rows - r, r_end - r))
                            c_span = max(1, min(n_cols - c, c_end - c))
                            if r_span > 1 or c_span > 1:
                                has_merged = True
                            if txt and not grid[r][c]:
                                grid[r][c] = txt
                            elif txt:
                                grid[r][c] = f"{grid[r][c]} / {txt}"
                            cells.append(
                                TableCell(
                                    row=r,
                                    col=c,
                                    row_span=r_span,
                                    col_span=c_span,
                                    text=grid[r][c],
                                    is_header=(r == 0),
                                )
                            )

                        headers = [
                            grid[0][c] if grid[0][c] else f"col_{c + 1}" for c in range(n_cols)
                        ]
                        data_rows = grid[1:] if n_rows >= 2 else []
                        tx0 = max(0.0, min(c[0] for c in cell_entries) / 8.0)
                        ty0 = max(0.0, min(c[1] for c in cell_entries) / 8.0)
                        tx1 = min(slide_w, max(c[2] for c in cell_entries) / 8.0)
                        ty1 = min(slide_h, max(c[3] for c in cell_entries) / 8.0)
                        if tx1 <= tx0 + 20.0 or ty1 <= ty0 + 20.0:
                            tx0, ty0, tx1, ty1 = 36.0, 96.0, slide_w - 36.0, slide_h - 48.0
                        tbl = ExtractedTable(
                            table_id=f"p{slide_number:03d}_t{len(tables) + 1:02d}",
                            page=slide_number,
                            bbox=make_bbox(tx0, ty0, tx1, ty1, slide_w, slide_h),
                            strategy=ParseStrategy.RULED_TABLE,
                            confidence=0.95,
                            row_count=n_rows,
                            col_count=n_cols,
                            header_row_count=1,
                            headers=headers,
                            rows=data_rows,
                            cells=cells,
                            has_merged_cells=has_merged,
                            reason=(
                                f"MS-PPT 네이티브 Escher 표 직접 추출({n_rows}x{n_cols}"
                                f"{', 병합 셀 포함' if has_merged else ''})"
                            ),
                        )
                        tables.append(
                            RawPptxTable(
                                shape_id=f"s{slide_number}_escher_tbl_{len(tables) + 1}",
                                name=f"PPT Table {len(tables) + 1}",
                                rect_pt=(tx0, ty0, tx1, ty1),
                                table=tbl,
                            )
                        )
                        consumed_ranges.append((p_start, p_end))
                        continue

        if rec_ver == 0x0F:
            sub_t, sub_r = _extract_escher_tables(
                ppt_doc,
                p_start,
                p_end,
                slide_number=slide_number,
                slide_w=slide_w,
                slide_h=slide_h,
                depth=depth + 1,
            )
            tables.extend(sub_t)
            consumed_ranges.extend(sub_r)
    return tables, consumed_ranges


def _extract_texts_from_container(
    ppt_doc: bytes,
    start: int,
    end: int,
    *,
    depth: int = 0,
    skip_ranges: tuple[tuple[int, int], ...] = (),
) -> list[tuple[int, str]]:
    """컨테이너 내부에서 `(text_type, text)` 목록을 재귀 추출한다."""
    if depth > 10:
        return []
    out: list[tuple[int, str]] = []
    cur_type = 4
    for rec_ver, _inst, rec_type, p_start, p_end in _iter_ppt_records(ppt_doc, start, end):
        if any(p_start >= rs and p_end <= re for rs, re in skip_ranges):
            continue
        if rec_type == _RT_TEXT_HEADER_ATOM and p_end - p_start >= 4:
            cur_type = int(struct.unpack_from("<I", ppt_doc, p_start)[0])
        elif rec_type in (_RT_TEXT_CHARS_ATOM, _RT_TEXT_BYTES_ATOM):
            txt = _decode_ppt_text_atom(rec_type, ppt_doc[p_start:p_end]).strip()
            if txt:
                out.append((cur_type, txt))
        elif rec_ver == 0x0F:
            out.extend(
                _extract_texts_from_container(
                    ppt_doc,
                    p_start,
                    p_end,
                    depth=depth + 1,
                    skip_ranges=skip_ranges,
                )
            )
    return out


def _relabel_to_ppt(text: str) -> str:
    return text.replace("PPTX ", "PPT ").replace("OOXML", "MS-PPT")


def extract_ppt_slides(cfb: CfbFile) -> tuple[list[RawSlideData], OleSummaryInfo]:
    """MS-PPT 바이너리 CFB에서 슬라이드 단위 IR(`RawSlideData`)과 요약 메타데이터를 추출한다."""
    if cfb.has_stream("EncryptedSummary"):
        raise ParserInputError(f"암호화된 MS PowerPoint(.ppt) 문서다: {cfb.source_path.name}")

    ppt_doc = cfb.read_stream("PowerPoint Document")
    if not ppt_doc or len(ppt_doc) < 16:
        raise ParserInputError(
            f"유효한 MS PowerPoint 97-2003(.ppt) 스트림(PowerPoint Document)을 찾을 수 없다: "
            f"{cfb.source_path.name}"
        )

    latest_edit_off = _read_current_user_edit_offset(cfb)
    doc_persist_id, persist_map = _resolve_live_persist_map(ppt_doc, latest_edit_off)

    doc_range: tuple[int, int] | None = None
    if doc_persist_id is not None and doc_persist_id in persist_map:
        d_off = persist_map[doc_persist_id]
        if d_off + 8 <= len(ppt_doc):
            _vi, dtype, dlen = struct.unpack_from("<HHI", ppt_doc, d_off)
            if dtype == _RT_DOCUMENT and d_off + 8 + dlen <= len(ppt_doc):
                doc_range = (d_off + 8, d_off + 8 + dlen)

    if doc_range is None:
        for _ver, _inst, rtype, ps, pe in _iter_ppt_records(ppt_doc):
            if rtype == _RT_DOCUMENT:
                doc_range = (ps, pe)

    slide_w = _DEFAULT_SLIDE_W
    slide_h = _DEFAULT_SLIDE_H
    slide_entries: list[tuple[int, int, list[tuple[int, str]]]] = []
    notes_by_slide_id: dict[int, list[str]] = {}
    global_slide_hf: dict[str, str] = {}
    notes_hf: dict[str, str] = {}

    if doc_range is not None:
        for _ver, inst, rtype, ps, pe in _iter_ppt_records(ppt_doc, doc_range[0], doc_range[1]):
            if rtype == _RT_DOCUMENT_ATOM and pe - ps >= 8:
                sx, sy = struct.unpack_from("<ii", ppt_doc, ps)
                if 1000 <= sx <= 30000 and 1000 <= sy <= 30000:
                    slide_w = round(sx / 8.0, 2)
                    slide_h = round(sy / 8.0, 2)
            elif rtype == _RT_HEADERS_FOOTERS:
                hf_parsed = _parse_headers_footers_container(ppt_doc, ps, pe)
                if inst == 3:
                    global_slide_hf.update(hf_parsed)
                elif inst == 4:
                    notes_hf.update(hf_parsed)
            elif rtype == _RT_SLIDE_LIST_WITH_TEXT and inst in (0, 2):
                cur_persist_ref = -1
                cur_slide_id = -1
                cur_text_type = 4
                cur_texts: list[tuple[int, str]] = []

                def _flush_slwt_entry(is_notes: bool) -> None:
                    nonlocal cur_persist_ref, cur_slide_id, cur_texts
                    if cur_persist_ref < 0 and not cur_texts:
                        return
                    if is_notes:
                        if cur_slide_id > 0 and cur_texts:
                            notes_by_slide_id.setdefault(cur_slide_id, []).extend(
                                t for _, t in cur_texts
                            )
                    else:
                        slide_entries.append((cur_persist_ref, cur_slide_id, list(cur_texts)))
                    cur_persist_ref = -1
                    cur_slide_id = -1
                    cur_texts = []

                for _cv, _ci, crt, cps, cpe in _iter_ppt_records(ppt_doc, ps, pe):
                    if crt == _RT_SLIDE_PERSIST_ATOM and cpe - cps >= 20:
                        _flush_slwt_entry(is_notes=(inst == 2))
                        cur_persist_ref = int(struct.unpack_from("<I", ppt_doc, cps)[0])
                        cur_slide_id = int(struct.unpack_from("<I", ppt_doc, cps + 12)[0])
                        cur_text_type = 2 if inst == 2 else 4
                    elif crt == _RT_TEXT_HEADER_ATOM and cpe - cps >= 4:
                        cur_text_type = int(struct.unpack_from("<I", ppt_doc, cps)[0])
                    elif crt in (_RT_TEXT_CHARS_ATOM, _RT_TEXT_BYTES_ATOM):
                        txt = _decode_ppt_text_atom(crt, ppt_doc[cps:cpe]).strip()
                        if txt:
                            cur_texts.append((cur_text_type, txt))
                _flush_slwt_entry(is_notes=(inst == 2))

    if not slide_entries:
        fallback_texts = _extract_texts_from_container(ppt_doc, 0, len(ppt_doc))
        if fallback_texts:
            slide_entries.append((-1, 0, fallback_texts))

    slides: list[RawSlideData] = []
    for s_idx, (persist_ref, slide_id, slwt_texts) in enumerate(slide_entries, start=1):
        combined: list[tuple[int, str]] = list(slwt_texts)
        seen_norm = {" ".join(t.split()) for _, t in combined}
        notes_texts: list[str] = list(notes_by_slide_id.get(slide_id, ()))
        slide_tables: list[RawPptxTable] = []
        slide_hf: dict[str, str] = dict(global_slide_hf)

        if persist_ref in persist_map:
            s_off = persist_map[persist_ref]
            if s_off + 8 <= len(ppt_doc):
                _svi, stype, slen = struct.unpack_from("<HHI", ppt_doc, s_off)
                if stype == _RT_SLIDE and s_off + 8 + slen <= len(ppt_doc):
                    s_start = s_off + 8
                    s_end = s_off + 8 + slen
                    slide_tables, tbl_ranges = _extract_escher_tables(
                        ppt_doc,
                        s_start,
                        s_end,
                        slide_number=s_idx,
                        slide_w=slide_w,
                        slide_h=slide_h,
                    )
                    extra_texts = _extract_texts_from_container(
                        ppt_doc,
                        s_start,
                        s_end,
                        skip_ranges=tuple(tbl_ranges),
                    )
                    for t_type, txt in extra_texts:
                        norm = " ".join(txt.split())
                        if norm and norm not in seen_norm:
                            seen_norm.add(norm)
                            combined.append((t_type, txt))
                    for _av, _ai, art, aps, ape in _iter_ppt_records(ppt_doc, s_start, s_end):
                        if art == _RT_SLIDE_ATOM and ape - aps >= 20:
                            notes_id = int(struct.unpack_from("<I", ppt_doc, aps + 16)[0])
                            if notes_id in notes_by_slide_id and not notes_texts:
                                notes_texts.extend(notes_by_slide_id[notes_id])
                        elif art == _RT_HEADERS_FOOTERS:
                            slide_hf.update(_parse_headers_footers_container(ppt_doc, aps, ape))

        if not slide_hf and notes_hf:
            for k, v in notes_hf.items():
                slide_hf.setdefault(k, v)

        shapes: list[RawPptxShape] = []
        y_cursor = 36.0
        for box_i, (t_type, txt) in enumerate(combined, start=1):
            is_title = t_type in (0, 6)
            is_subtitle = t_type == 5
            font_sz = 24.0 if is_title else (16.0 if is_subtitle else 12.0)
            paras = tuple(
                RawPptxParagraph(
                    text=ln.strip(),
                    level=0,
                    is_bullet=(not is_title and box_i > 1 and len(combined) > 1),
                    font_size=font_sz,
                    is_bold=is_title,
                )
                for ln in txt.splitlines()
                if ln.strip()
            )
            if not paras:
                continue
            box_h = max(32.0, len(paras) * (font_sz * 1.45) + 12.0)
            y0 = min(y_cursor, max(16.0, slide_h - 48.0))
            y1 = min(slide_h - 16.0, y0 + box_h)
            ph_type = "title" if is_title else ("subTitle" if is_subtitle else None)
            shapes.append(
                RawPptxShape(
                    shape_id=f"s{s_idx}_tb{box_i}",
                    name="Title" if is_title else f"TextBox {box_i}",
                    ph_type=ph_type,
                    ph_idx=str(box_i),
                    rect_pt=(36.0, y0, slide_w - 36.0, max(y0 + 18.0, y1)),
                    paragraphs=paras,
                    is_text_box=True,
                )
            )
            y_cursor = y1 + 10.0

        # MS-PPT HeadersFootersContainer(0x0FD9) 머리글·바닥글·날짜 플레이스홀더 연결
        if "hdr" in slide_hf:
            shapes.append(
                RawPptxShape(
                    shape_id=f"s{s_idx}_hdr",
                    name="Header",
                    ph_type="hdr",
                    ph_idx="hdr",
                    rect_pt=(36.0, 6.0, slide_w - 36.0, 26.0),
                    paragraphs=(RawPptxParagraph(text=slide_hf["hdr"], font_size=10.0),),
                    is_text_box=True,
                )
            )
        if "ftr" in slide_hf:
            shapes.append(
                RawPptxShape(
                    shape_id=f"s{s_idx}_ftr",
                    name="Footer",
                    ph_type="ftr",
                    ph_idx="ftr",
                    rect_pt=(36.0, slide_h - 26.0, slide_w - 36.0, slide_h - 6.0),
                    paragraphs=(RawPptxParagraph(text=slide_hf["ftr"], font_size=10.0),),
                    is_text_box=True,
                )
            )
        if "dt" in slide_hf:
            shapes.append(
                RawPptxShape(
                    shape_id=f"s{s_idx}_dt",
                    name="Date",
                    ph_type="dt",
                    ph_idx="dt",
                    rect_pt=(36.0, slide_h - 26.0, min(260.0, slide_w * 0.4), slide_h - 6.0),
                    paragraphs=(RawPptxParagraph(text=slide_hf["dt"], font_size=10.0),),
                    is_text_box=True,
                )
            )

        slides.append(
            RawSlideData(
                slide_number=s_idx,
                slide_id=str(slide_id if slide_id > 0 else s_idx),
                part_path=f"PowerPoint Document#slide{s_idx}",
                width_pt=slide_w,
                height_pt=slide_h,
                shapes=shapes,
                tables=slide_tables,
                speaker_notes="\n".join(notes_texts).strip(),
            )
        )

    if not slides:
        slides.append(
            RawSlideData(
                slide_number=1,
                slide_id="1",
                part_path="PowerPoint Document#slide1",
                width_pt=slide_w,
                height_pt=slide_h,
            )
        )

    pics_stm = cfb.read_stream("Pictures") or b""
    blips = extract_officeart_blips(pics_stm)
    for b_i, blip in enumerate(blips):
        target_slide = slides[min(b_i, len(slides) - 1)]
        w_pt = min(slide_w * 0.72, float(blip.pixel_width or 280) * 0.75)
        h_pt = min(slide_h * 0.65, float(blip.pixel_height or 180) * 0.75)
        target_slide.images.append(
            RawPptxImage(
                shape_id=f"pic_{blip.index}",
                name=f"ppt_image_{blip.index}{blip.extension}",
                alt_text="",
                media_part=f"Pictures/blip_{blip.index}{blip.extension}",
                extension=blip.extension,
                rect_pt=(48.0, 120.0, 48.0 + max(64.0, w_pt), 120.0 + max(48.0, h_pt)),
                raw_bytes=blip.raw_bytes,
                pixel_width=blip.pixel_width,
                pixel_height=blip.pixel_height,
            )
        )

    summary = read_ole_summary_info(cfb, ("\x05SummaryInformation",))
    return slides, summary


class PptDocumentAdapter:
    """구형 MS PowerPoint(`.ppt`) 하이브리드(LibreOffice 승격 + 순수 Python MS-PPT) 어댑터."""

    @property
    def format(self) -> SourceFormat:
        return SourceFormat.PPT

    @property
    def extensions(self) -> tuple[str, ...]:
        return (".ppt",)

    def __init__(
        self,
        *,
        use_libreoffice: bool = True,
        libreoffice_binary: str | None = None,
    ) -> None:
        self._use_libreoffice = use_libreoffice
        self._libreoffice_binary = libreoffice_binary
        self._pptx_adapter = PptxDocumentAdapter()

    def _try_libreoffice_pptx(self, source_path: Path, tmp_dir: Path) -> Path | None:
        if not self._use_libreoffice:
            return None
        if find_libreoffice_binary(self._libreoffice_binary) is None:
            return None
        try:
            return convert_legacy_office_document(
                source_path,
                "pptx",
                tmp_dir,
                binary=self._libreoffice_binary,
            )
        except ParserInputError:
            return None

    @staticmethod
    def _restamp_metadata(orig_path: Path, meta: DocumentMetadata) -> DocumentMetadata:
        raw = orig_path.read_bytes()
        return meta.model_copy(
            update={
                "source_path": orig_path.name,
                "format": SourceFormat.PPT,
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
    ) -> tuple[list[AnalyzedSlide], RoutingPlan]:
        with CfbFile(source_path) as cfb:
            raw_slides, summary = extract_ppt_slides(cfb)
            total_slides = len(raw_slides)
            file_size = cfb.file_size
            file_sha256 = cfb.sha256

        if options.pages is not None:
            invalid = [p for p in options.pages if p < 1 or p > total_slides]
            if invalid:
                raise ParserInputError(
                    f"선택한 슬라이드 번호가 문서 범위(1~{total_slides})를 벗어났다: {invalid}"
                )
            selected_set = set(options.pages)
            target_slides = [s for s in raw_slides if s.slide_number in selected_set]
        else:
            target_slides = raw_slides

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
        for raw_slide in target_slides:
            aslide = analyze_slide(
                raw_slide,
                deck_body_font_size=body_font,
                repeating_logos=repeating_logos,
                repeating_headers=repeating_headers,
                repeating_footers=repeating_footers,
                include_speaker_notes=options.include_speaker_notes,
                include_comments=options.include_comments,
            )
            aslide.visuals = [
                v.model_copy(update={"reason": _relabel_to_ppt(v.reason)}) for v in aslide.visuals
            ]
            aslide.route = aslide.route.model_copy(
                update={
                    "reasons": [_relabel_to_ppt(r) for r in aslide.route.reasons],
                    "regions": [
                        r.model_copy(update={"reason": _relabel_to_ppt(r.reason)})
                        for r in aslide.route.regions
                    ],
                }
            )
            if active_detector is not None:
                aslide = _apply_layout_detection_to_slide(aslide, layout_detector=active_detector)
            analyzed.append(aslide)

        title = summary.title
        if not title:
            for aslide in analyzed:
                for blk in aslide.blocks:
                    if blk.heading_level == 1 and blk.text.strip():
                        title = blk.text.strip()[:120]
                        break
                if title:
                    break

        metadata = DocumentMetadata(
            source_path=source_path.name,
            format=SourceFormat.PPT,
            file_size_bytes=file_size,
            sha256=file_sha256,
            page_count=total_slides,
            title=title,
            author=summary.author,
            creator=summary.creator_app or "Microsoft PowerPoint 97-2003 (Native MS-PPT)",
        )
        plan = build_pptx_routing_plan(
            metadata,
            analyzed,
            sections=[],
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
        """`.ppt` 문서를 진단한다(LibreOffice 있으면 `.pptx` 승격, 없으면 순수 Python MS-PPT)."""
        with tempfile.TemporaryDirectory(prefix="autoetl_ppt_conv_") as td:
            converted = self._try_libreoffice_pptx(source_path, Path(td))
            if converted is not None:
                plan = self._pptx_adapter.inspect(
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
        """`.ppt` 문서를 통합 IR로 파싱한다(LibreOffice 승격 우선 + 순수 Python MS-PPT 폴백)."""
        with tempfile.TemporaryDirectory(prefix="autoetl_ppt_conv_") as td:
            converted = self._try_libreoffice_pptx(source_path, Path(td))
            if converted is not None:
                doc = self._pptx_adapter.parse(
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

        analyzed_slides, routing_plan = self._analyze_native(
            source_path,
            options,
            layout_detector=layout_detector,
        )
        parsed_pages: list[ParsedPage] = []
        all_tables: list[ExtractedTable] = []
        all_visuals: list[VisualRegion] = []
        doc_warnings: list[str] = []

        for item in analyzed_slides:
            doc_warnings.extend(f"p.{item.raw.slide_number}: {w}" for w in item.route.warnings)
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
                    page=item.raw.slide_number,
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
    "PptDocumentAdapter",
    "extract_ppt_slides",
]
