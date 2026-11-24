"""pypdf 기반 PDF 저수준 기하·텍스트·벡터·이미지·메타데이터 추출기.

PDF 콘텐츠 스트림의 연산자(`m`, `l`, `re`, `c`, `v`, `y`, `S`, `f`, `B`, `Do`, `q`, `Q`)와
텍스트 콜백(`visitor_text`)을 함께 추적해 다음을 정확히 추출한다:
- Form XObject 중첩 CTM 스택까지 보정한 텍스트 스팬(위치·글꼴·크기·굵기)
- 표 테두리·구분선·음영 사각형·벡터 도식 경로
- 이미지 XObject 배치 위치와 원시 스트림(스캔·그림 크롭 직접 추출용)
- 문서 메타데이터 및 북마크(Outline/TOC)
"""

from __future__ import annotations

import contextlib
import hashlib
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pypdf
from pypdf.errors import PdfReadError

from autoetl.parser.adapters import ParserInputError
from autoetl.parser.contracts import DocumentMetadata, OutlineEntry, SourceFormat
from autoetl.parser.pdf.geometry import (
    IDENTITY_MATRIX,
    Matrix6,
    PageCoordTransform,
    RawImagePlacement,
    RawPageData,
    RawTextSpan,
    RawVectorCurve,
    RawVectorLine,
    RawVectorRect,
    apply_matrix,
    compose_matrix,
    detect_font_style,
    estimate_text_width,
    to_matrix6,
)


@dataclass
class _FormFrame:
    ctm: Matrix6
    resources: Any
    active: bool = False


@dataclass(frozen=True)
class ExtractedPdfDocument:
    """PDF 전체에서 추출한 메타·목차·페이지별 원시 기하 데이터."""

    metadata: DocumentMetadata
    outline: list[OutlineEntry]
    pages: list[RawPageData]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _extract_outline(reader: pypdf.PdfReader) -> list[OutlineEntry]:
    entries: list[OutlineEntry] = []
    try:
        raw_outline = reader.outline
    except Exception:
        return entries

    def walk(items: Any, level: int) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            if isinstance(item, list):
                walk(item, level + 1)
                continue
            title = _safe_str(getattr(item, "title", None))
            if not title:
                continue
            page_num: int | None = None
            try:
                idx = reader.get_destination_page_number(item)
                if idx is not None and idx >= 0:
                    page_num = idx + 1
            except Exception:
                page_num = None
            entries.append(OutlineEntry(level=max(1, level), title=title, page=page_num))

    walk(raw_outline, 1)
    return entries


def _resolve_obj(obj: Any) -> Any:
    if obj is not None and hasattr(obj, "get_object"):
        return obj.get_object()
    return obj


def _lookup_xobject(resources: Any, name: str) -> Any | None:
    res = _resolve_obj(resources)
    if res is None or not hasattr(res, "get"):
        return None
    xobjs = _resolve_obj(res.get("/XObject"))
    if xobjs is None or not hasattr(xobjs, "get"):
        return None
    obj = xobjs.get(name)
    return _resolve_obj(obj) if obj is not None else None


def _extract_image_stream_info(
    xobj: Any,
) -> tuple[int, int, tuple[str, ...], str | None, int, bytes | None, bytes | None]:
    width = int(xobj.get("/Width", 0) or 0)
    height = int(xobj.get("/Height", 0) or 0)
    bpc = int(xobj.get("/BitsPerComponent", 8) or 8)

    raw_filter = _resolve_obj(xobj.get("/Filter"))
    filters: list[str] = []
    if isinstance(raw_filter, list):
        filters = [str(f) for f in raw_filter]
    elif raw_filter is not None:
        filters = [str(raw_filter)]

    cs_obj = _resolve_obj(xobj.get("/ColorSpace"))
    color_space: str | None = None
    if isinstance(cs_obj, list) and cs_obj:
        color_space = str(cs_obj[0])
    elif cs_obj is not None:
        color_space = str(cs_obj)

    raw_bytes: bytes | None = None
    decoded_bytes: bytes | None = None
    try:
        raw_bytes = bytes(xobj._data) if hasattr(xobj, "_data") else None
    except Exception:
        raw_bytes = None
    try:
        if not filters or filters[-1] not in ("/DCTDecode", "/DCT", "/JPXDecode"):
            decoded_bytes = bytes(xobj.get_data())
    except Exception:
        decoded_bytes = None

    return (width, height, tuple(filters), color_space, bpc, raw_bytes, decoded_bytes)


def _float_operand(val: Any) -> float:
    return float(val)


def extract_page_primitives(page: pypdf.PageObject, page_number: int) -> RawPageData:
    """단일 PDF 페이지에서 텍스트 스팬, 벡터 선·사각형·곡선, 배치 이미지를 추출한다."""
    crop = page.cropbox if page.cropbox is not None else page.mediabox
    rotation = int(page.get("/Rotate", 0) or 0)
    coord = PageCoordTransform(
        crop_x0=float(crop.left),
        crop_y0=float(crop.bottom),
        crop_x1=float(crop.right),
        crop_y1=float(crop.top),
        rotation=rotation,
    )

    spans: list[RawTextSpan] = []
    lines: list[RawVectorLine] = []
    rects: list[RawVectorRect] = []
    curves: list[RawVectorCurve] = []
    images: list[RawImagePlacement] = []

    root_resources = _resolve_obj(page.get("/Resources"))
    form_stack: list[_FormFrame] = []
    line_width_stack: list[float] = [1.0]
    char_spacing_stack: list[float] = [0.0]

    # 현재 visitor_text 플러시 사이클 동안 누적된 대간격 TJ/Tc/Td 점프 힌트
    pending_glyph_cuts: list[tuple[int, float]] = []  # (nonspace_idx, jump_em)
    pending_space_cuts: list[tuple[int, float]] = []  # (nonleading_space_idx, jump_em)
    pending_nonspace_count = 0
    pending_space_count = 0
    pending_ascii_exact = True
    pending_td_lead: tuple[float, float] | None = None
    pending_td_jumps: list[tuple[float, float]] = []
    saw_text_draw_since_flush = False
    saw_nonspace_since_flush = False
    last_draw_tm: Matrix6 | None = None
    last_draw_cm: Matrix6 | None = None
    last_draw_fs: float = 10.0

    # 현재 구성 중인 벡터 경로 버퍼(각 항목은 (op_kind, points_in_pdf_space))
    cur_point: tuple[float, float] | None = None
    subpath_start: tuple[float, float] | None = None
    pending_segments: list[tuple[tuple[float, float], tuple[float, float]]] = []
    pending_rects: list[tuple[float, float, float, float]] = []
    pending_curve_pts: list[tuple[float, float]] = []
    pending_curve_count = 0

    def active_form_ctm() -> Matrix6:
        for frame in reversed(form_stack):
            if frame.active:
                return frame.ctm
        return IDENTITY_MATRIX

    def active_resources() -> Any:
        for frame in reversed(form_stack):
            if frame.active and frame.resources is not None:
                return frame.resources
        return root_resources

    def effective_cm(local_cm: Any) -> Matrix6:
        local = to_matrix6(local_cm)
        form_ctm = active_form_ctm()
        if form_ctm == IDENTITY_MATRIX:
            return local
        return compose_matrix(local, form_ctm)

    def clear_path() -> None:
        nonlocal cur_point, subpath_start, pending_curve_count
        cur_point = None
        subpath_start = None
        pending_segments.clear()
        pending_rects.clear()
        pending_curve_pts.clear()
        pending_curve_count = 0

    def flush_path(*, stroked: bool, filled: bool) -> None:
        stroke_w = line_width_stack[-1] if line_width_stack else 1.0
        for p1, p2 in pending_segments:
            nx0, ny0 = coord.point(p1[0], p1[1])
            nx1, ny1 = coord.point(p2[0], p2[1])
            seg_len = math.hypot(nx1 - nx0, ny1 - ny0)
            if seg_len < 1.0:
                continue
            if abs(ny1 - ny0) <= 2.0 or abs(nx1 - nx0) <= 2.0:
                lines.append(
                    RawVectorLine(
                        x0=min(nx0, nx1),
                        top=min(ny0, ny1),
                        x1=max(nx0, nx1),
                        bottom=max(ny0, ny1),
                        stroke_width=stroke_w,
                        is_stroked=stroked,
                    )
                )
            else:
                curves.append(
                    RawVectorCurve(
                        x0=min(nx0, nx1),
                        top=min(ny0, ny1),
                        x1=max(nx0, nx1),
                        bottom=max(ny0, ny1),
                        segment_count=1,
                    )
                )

        for rx0, ry0, rx1, ry1 in pending_rects:
            if rx1 - rx0 < 0.5 and ry1 - ry0 < 0.5:
                continue
            rects.append(
                RawVectorRect(
                    x0=rx0,
                    top=ry0,
                    x1=rx1,
                    bottom=ry1,
                    is_stroked=stroked,
                    is_filled=filled,
                    stroke_width=stroke_w,
                )
            )

        if pending_curve_pts:
            cx0, ctop, cx1, cbot = coord.rect_from_points(pending_curve_pts)
            if cx1 - cx0 >= 2.0 or cbot - ctop >= 2.0:
                curves.append(
                    RawVectorCurve(
                        x0=cx0,
                        top=ctop,
                        x1=cx1,
                        bottom=cbot,
                        segment_count=max(1, pending_curve_count),
                    )
                )
        clear_path()

    def _record_text_show_splits(elems: list[Any]) -> None:
        nonlocal pending_nonspace_count, pending_space_count
        nonlocal pending_ascii_exact, saw_nonspace_since_flush
        cur_tc = char_spacing_stack[-1] if char_spacing_stack else 0.0
        cur_fs = last_draw_fs if last_draw_fs > 1e-4 else 1.0
        tc_em = cur_tc / cur_fs
        tj_num = 0.0
        for elem in elems:
            if isinstance(elem, (int, float)):
                tj_num += float(elem)
                continue
            if not isinstance(elem, (bytes, str)):
                continue
            if isinstance(elem, bytes):
                if all(0x20 <= b <= 0x7E for b in elem):
                    txt_s = elem.decode("ascii")
                else:
                    pending_ascii_exact = False
                    txt_s = elem.decode("latin-1")
            else:
                txt_s = str(elem)
                if not all(0x20 <= ord(ch) <= 0x7E for ch in txt_s):
                    pending_ascii_exact = False

            # PDF 명세(ISO 32000-1 9.4.4): 글자 간 추가 전진 = Tc - TJ/1000 (em 환산)
            if pending_nonspace_count > 0:
                if txt_s.strip():
                    extra_em = tc_em - (tj_num / 1000.0)
                    if extra_em >= 0.95:
                        pending_glyph_cuts.append((pending_nonspace_count, extra_em))
                if -tj_num >= 190.0:
                    if -tj_num >= 950.0 and txt_s.strip():
                        pending_space_cuts.append((pending_space_count, -tj_num / 1000.0))
                    pending_space_count += 1
            tj_num = 0.0

            prev_in_str_nonspace = False
            for ch in txt_s:
                if ch == " ":
                    if pending_nonspace_count > 0:
                        pending_space_count += 1
                    prev_in_str_nonspace = False
                    continue
                if prev_in_str_nonspace and tc_em >= 0.95:
                    pending_glyph_cuts.append((pending_nonspace_count, tc_em))
                pending_nonspace_count += 1
                saw_nonspace_since_flush = True
                prev_in_str_nonspace = True

    def on_op_before(op: Any, args: Any, cm: Any, tm: Any) -> None:
        nonlocal cur_point, subpath_start, pending_curve_count
        nonlocal saw_text_draw_since_flush, last_draw_tm, last_draw_cm, last_draw_fs

        # 이전 Do가 Form XObject를 열었다면, Form 내부 첫 연산자 진입 시점에 스택 활성화
        if form_stack and not form_stack[-1].active:
            form_stack[-1].active = True

        if op == b"q":
            line_width_stack.append(line_width_stack[-1] if line_width_stack else 1.0)
            char_spacing_stack.append(char_spacing_stack[-1] if char_spacing_stack else 0.0)
            return
        if op == b"Q":
            if len(line_width_stack) > 1:
                line_width_stack.pop()
            if len(char_spacing_stack) > 1:
                char_spacing_stack.pop()
            return
        if op == b"w" and args:
            with contextlib.suppress(TypeError, ValueError):
                line_width_stack[-1] = max(0.2, _float_operand(args[0]))
            return
        if op == b"Tc" and args:
            with contextlib.suppress(TypeError, ValueError):
                char_spacing_stack[-1] = _float_operand(args[0])
            return
        if op == b"Tf" and len(args) >= 2:
            with contextlib.suppress(TypeError, ValueError):
                last_draw_fs = abs(_float_operand(args[1])) or last_draw_fs
            return

        if op == b"TJ" and args:
            saw_text_draw_since_flush = True
            last_draw_tm = to_matrix6(tm)
            last_draw_cm = effective_cm(cm)
            arr = args[0]
            if isinstance(arr, list):
                _record_text_show_splits(arr)
            return

        if op in (b"Tj", b"'", b'"'):
            saw_text_draw_since_flush = True
            last_draw_tm = to_matrix6(tm)
            last_draw_cm = effective_cm(cm)
            if args:
                _record_text_show_splits([args[-1]])
            return

        if op == b"Do" and args:
            name = str(args[0])
            xobj = _lookup_xobject(active_resources(), name)
            if xobj is None or not hasattr(xobj, "get"):
                return
            subtype = str(xobj.get("/Subtype", ""))
            eff_cm = effective_cm(cm)
            if subtype == "/Form":
                form_mat = to_matrix6(xobj.get("/Matrix", IDENTITY_MATRIX))
                nested_ctm = compose_matrix(form_mat, eff_cm)
                form_res = _resolve_obj(xobj.get("/Resources")) or active_resources()
                form_stack.append(_FormFrame(ctm=nested_ctm, resources=form_res, active=False))
            elif subtype == "/Image":
                corners = [
                    apply_matrix(eff_cm, 0.0, 0.0),
                    apply_matrix(eff_cm, 1.0, 0.0),
                    apply_matrix(eff_cm, 0.0, 1.0),
                    apply_matrix(eff_cm, 1.0, 1.0),
                ]
                ix0, itop, ix1, ibot = coord.rect_from_points(corners)
                pw, ph, filters, cs, bpc, raw_d, dec_d = _extract_image_stream_info(xobj)
                images.append(
                    RawImagePlacement(
                        image_key=f"p{page_number}_{name.lstrip('/')}_{len(images) + 1}",
                        x0=ix0,
                        top=itop,
                        x1=ix1,
                        bottom=ibot,
                        pixel_width=pw,
                        pixel_height=ph,
                        filters=filters,
                        color_space=cs,
                        bits_per_component=bpc,
                        raw_data=raw_d,
                        decoded_data=dec_d,
                    )
                )
            return

        eff_cm = effective_cm(cm)
        try:
            if op == b"m" and len(args) >= 2:
                pt = apply_matrix(eff_cm, _float_operand(args[0]), _float_operand(args[1]))
                cur_point = pt
                subpath_start = pt
            elif op == b"l" and len(args) >= 2:
                pt = apply_matrix(eff_cm, _float_operand(args[0]), _float_operand(args[1]))
                if cur_point is not None:
                    pending_segments.append((cur_point, pt))
                cur_point = pt
            elif op == b"h":
                if cur_point is not None and subpath_start is not None:
                    pending_segments.append((cur_point, subpath_start))
                    cur_point = subpath_start
            elif op == b"re" and len(args) >= 4:
                rx = _float_operand(args[0])
                ry = _float_operand(args[1])
                rw = _float_operand(args[2])
                rh = _float_operand(args[3])
                corners = [
                    apply_matrix(eff_cm, rx, ry),
                    apply_matrix(eff_cm, rx + rw, ry),
                    apply_matrix(eff_cm, rx, ry + rh),
                    apply_matrix(eff_cm, rx + rw, ry + rh),
                ]
                pending_rects.append(coord.rect_from_points(corners))
                cur_point = corners[0]
                subpath_start = corners[0]
            elif op in (b"c", b"v", b"y") and len(args) >= 4:
                if cur_point is not None:
                    pending_curve_pts.append(cur_point)
                for idx in range(0, len(args) - 1, 2):
                    pt = apply_matrix(
                        eff_cm, _float_operand(args[idx]), _float_operand(args[idx + 1])
                    )
                    pending_curve_pts.append(pt)
                cur_point = pending_curve_pts[-1]
                pending_curve_count += 1
            elif op == b"S":
                flush_path(stroked=True, filled=False)
            elif op == b"s":
                if cur_point is not None and subpath_start is not None:
                    pending_segments.append((cur_point, subpath_start))
                flush_path(stroked=True, filled=False)
            elif op in (b"f", b"F", b"f*"):
                flush_path(stroked=False, filled=True)
            elif op in (b"B", b"B*"):
                flush_path(stroked=True, filled=True)
            elif op in (b"b", b"b*"):
                if cur_point is not None and subpath_start is not None:
                    pending_segments.append((cur_point, subpath_start))
                flush_path(stroked=True, filled=True)
            elif op == b"n":
                clear_path()
        except (TypeError, ValueError):
            clear_path()

    def on_op_after(op: Any, args: Any, cm: Any, tm: Any) -> None:
        nonlocal pending_td_lead
        if op == b"Do" and form_stack:
            form_stack.pop()
            return
        # 같은 줄에서 Td/TD/Tm이 크게 수평 이동했는데 pypdf가 visitor_text를 플러시하지
        # 않았을 때(예: Word 표 셀 경계 점프), 새 절대 좌표를 분할/이동 힌트로 기록한다.
        if (
            op in (b"Td", b"TD", b"Tm")
            and saw_text_draw_since_flush
            and last_draw_tm is not None
            and last_draw_cm is not None
        ):
            new_tm = to_matrix6(tm)
            eff_cm = effective_cm(cm)
            old_trm = compose_matrix(last_draw_tm, last_draw_cm)
            new_trm = compose_matrix(new_tm, eff_cm)
            scale_y = math.hypot(new_trm[2], new_trm[3]) or 1.0
            eff_fs = max(4.0, last_draw_fs * scale_y)
            dx = new_trm[4] - old_trm[4]
            dy = abs(new_trm[5] - old_trm[5])
            if dy <= max(2.5, eff_fs * 0.25):
                if not saw_nonspace_since_flush and dx >= max(12.0, eff_fs * 1.1):
                    pending_td_lead = (new_trm[4], new_trm[5])
                elif saw_nonspace_since_flush and dx >= max(24.0, eff_fs * 2.2):
                    pending_td_jumps.append((new_trm[4], new_trm[5]))

    _WINGDING_BULLETS = frozenset({"n", "l", "u", "v", "q", "p", "Ÿ", "Ø", "§", "¨", "ü", "þ", "w"})

    def _sanitize_pdf_text(raw_text: str, font_name: str) -> str:
        # NUL(\x00) 제거, 기타 비공백 제어문자는 U+FFFD(깨진 OCR 감지 유지),
        # HWP/Word PUA 불릿(\uf000~\uf0ff) 및 Wingdings 불릿 문자를 '• '로 정규화한다.
        lower_font = font_name.lower()
        is_dingbat = any(tok in lower_font for tok in ("wingding", "webding", "zapfdingbat"))
        if is_dingbat and len(raw_text.strip()) <= 2:
            stripped = raw_text.strip()
            if stripped in _WINGDING_BULLETS or any(0xF000 <= ord(c) <= 0xF0FF for c in stripped):
                return "• "

        out_chars: list[str] = []
        for ch in raw_text:
            code = ord(ch)
            if ch in ("\r", "\n"):
                continue
            if code == 0:
                continue
            if code < 32:
                if ch == "\t":
                    out_chars.append(" ")
                else:
                    out_chars.append("\ufffd")
                continue
            if 0xF000 <= code <= 0xF0FF:
                out_chars.append("• ")
                continue
            out_chars.append(ch)
        return "".join(out_chars)

    def _emit_single_span(
        piece: str,
        base_x: float,
        base_y: float,
        eff_size: float,
        scale_x: float,
        scale_y: float,
        orientation: int,
        font_name: str,
        font_dict: Any,
        is_bold: bool,
        is_italic: bool,
        *,
        allow_pen_advance: bool = True,
    ) -> float:
        text_w = estimate_text_width(piece, eff_size, font_dict)
        if scale_x > 1e-4 and scale_y > 1e-4:
            aspect = scale_x / scale_y
            if 0.2 <= aspect <= 5.0:
                text_w *= aspect

        ascent = eff_size * 0.82
        descent = eff_size * 0.18
        if orientation == 0:
            pdf_corners = [
                (base_x, base_y - descent),
                (base_x + text_w, base_y - descent),
                (base_x, base_y + ascent),
                (base_x + text_w, base_y + ascent),
            ]
        elif orientation == 90:
            pdf_corners = [
                (base_x - ascent, base_y),
                (base_x + descent, base_y),
                (base_x - ascent, base_y + text_w),
                (base_x + descent, base_y + text_w),
            ]
        elif orientation == 180:
            pdf_corners = [
                (base_x - text_w, base_y - ascent),
                (base_x, base_y - ascent),
                (base_x - text_w, base_y + descent),
                (base_x, base_y + descent),
            ]
        else:
            pdf_corners = [
                (base_x - descent, base_y - text_w),
                (base_x + ascent, base_y - text_w),
                (base_x - descent, base_y),
                (base_x + ascent, base_y),
            ]

        sx0, stop, sx1, sbot = coord.rect_from_points(pdf_corners)
        eff_orientation = (orientation + coord.rotation) % 360

        # pypdf는 같은 BT..ET 안에서 Tf(글꼴 전환)만 일어날 때 tm을 앞 스팬 폭만큼
        # 전진시키지 않아 같은 (x0, top)으로 보고한다. 직전 스팬과 좌표가 겹치면 펜 전진 보정.
        if (
            allow_pen_advance
            and eff_orientation == 0
            and spans
            and spans[-1].orientation == 0
            and abs(stop - spans[-1].top) <= max(2.5, eff_size * 0.25)
            and abs(sx0 - spans[-1].x0) <= 0.75
            and spans[-1].x1 > sx0
        ):
            span_w = max(0.5, sx1 - sx0)
            sx0 = spans[-1].x1
            sx1 = sx0 + span_w

        spans.append(
            RawTextSpan(
                text=piece,
                x0=sx0,
                top=stop,
                x1=sx1,
                bottom=sbot,
                font_name=font_name,
                font_size=round(eff_size, 2),
                is_bold=is_bold,
                is_italic=is_italic,
                orientation=eff_orientation,
            )
        )
        return text_w

    def on_text(text: str, cm: Any, tm: Any, font_dict: Any, font_size: float) -> None:
        nonlocal saw_text_draw_since_flush, saw_nonspace_since_flush
        nonlocal pending_nonspace_count, pending_space_count
        nonlocal pending_ascii_exact, pending_td_lead

        glyph_cuts = list(pending_glyph_cuts)
        space_cuts = list(pending_space_cuts)
        tracked_nonspace = pending_nonspace_count
        ascii_exact = pending_ascii_exact
        td_lead = pending_td_lead
        td_jumps_raw = list(pending_td_jumps)

        pending_glyph_cuts.clear()
        pending_space_cuts.clear()
        pending_nonspace_count = 0
        pending_space_count = 0
        pending_ascii_exact = True
        pending_td_lead = None
        pending_td_jumps.clear()
        saw_text_draw_since_flush = False
        saw_nonspace_since_flush = False

        if not text:
            return

        font_name = ""
        if font_dict is not None and hasattr(font_dict, "get"):
            bf = font_dict.get("/BaseFont")
            if bf is not None:
                font_name = str(bf).lstrip("/")

        cleaned = _sanitize_pdf_text(text, font_name)
        if not cleaned.strip():
            return

        eff_cm = effective_cm(cm)
        tm_mat = to_matrix6(tm)
        trm = compose_matrix(tm_mat, eff_cm)

        scale_y = math.hypot(trm[2], trm[3])
        scale_x = math.hypot(trm[0], trm[1])
        eff_size = float(font_size) * (scale_y if scale_y > 1e-4 else 1.0)
        if eff_size <= 0.5:
            eff_size = max(1.0, float(font_size) if font_size else 10.0)

        angle = math.degrees(math.atan2(trm[1], trm[0])) % 360.0
        orientation = int(round(angle / 90.0) * 90) % 360
        is_bold, is_italic = detect_font_style(font_name, font_dict)

        base_x, base_y = trm[4], trm[5]
        allow_pen = True

        # 0) 공백만 먼저 그린 뒤 Td로 다음 셀까지 점프해 실제 단어를 그린 경우(예: Word ' AI ')
        if orientation == 0 and td_lead is not None and td_lead[0] > base_x + 10.0:
            base_x, base_y = td_lead
            cleaned = cleaned.lstrip(" ")
            allow_pen = False

        # 1) Tc + TJ 대간격 점프로 합쳐진 다중 셀 문자열을 각 셀의 정확한 x 좌표로 분할
        body = cleaned.lstrip(" ")
        if orientation == 0 and (glyph_cuts or space_cuts) and body:
            valid_cuts: list[tuple[int, float]] = []
            nonspace_pos = [i for i, ch in enumerate(body) if ch != " "]
            if glyph_cuts and ascii_exact and len(nonspace_pos) == tracked_nonspace:
                for ns_idx, jump_em in glyph_cuts:
                    if 1 <= ns_idx < len(nonspace_pos):
                        valid_cuts.append((nonspace_pos[ns_idx - 1] + 1, jump_em))
            elif space_cuts and " " in body:
                space_positions = [i for i, ch in enumerate(body) if ch == " "]
                for sp_idx, jump_em in space_cuts:
                    if 0 <= sp_idx < len(space_positions):
                        valid_cuts.append((space_positions[sp_idx], jump_em))

            if valid_cuts:
                valid_cuts.sort(key=lambda item: item[0])
                cur_x = base_x
                start_i = 0
                em_scale = eff_size * (
                    scale_x / scale_y if scale_x > 1e-4 and scale_y > 1e-4 else 1.0
                )
                for pos, jump_em in valid_cuts:
                    if pos < start_i:
                        continue
                    chunk = body[start_i:pos]
                    if chunk.strip():
                        w = _emit_single_span(
                            chunk,
                            cur_x,
                            base_y,
                            eff_size,
                            scale_x,
                            scale_y,
                            orientation,
                            font_name,
                            font_dict,
                            is_bold,
                            is_italic,
                            allow_pen_advance=(allow_pen and start_i == 0),
                        )
                        cur_x += w
                    cur_x += jump_em * em_scale
                    start_i = pos
                    while start_i < len(body) and body[start_i] == " ":
                        start_i += 1
                tail = body[start_i:]
                if tail.strip():
                    _emit_single_span(
                        tail,
                        cur_x,
                        base_y,
                        eff_size,
                        scale_x,
                        scale_y,
                        orientation,
                        font_name,
                        font_dict,
                        is_bold,
                        is_italic,
                        allow_pen_advance=False,
                    )
                return

        # 2) 같은 글꼴에서 Td/Tm 수평 대점프로 합쳐진 두 셀 문자열 분할(예: ' (1) AI ')
        td_jumps = [(a, b) for a, b in td_jumps_raw if a > base_x + 12.0]
        if orientation == 0 and td_jumps and len(cleaned.strip().split()) >= 2:
            tokens = cleaned.strip().split()
            if len(td_jumps) == 1:
                jump_x, jump_y = td_jumps[0]
                left_txt = " ".join(tokens[:-1])
                right_txt = tokens[-1]
                _emit_single_span(
                    left_txt,
                    base_x,
                    base_y,
                    eff_size,
                    scale_x,
                    scale_y,
                    orientation,
                    font_name,
                    font_dict,
                    is_bold,
                    is_italic,
                    allow_pen_advance=allow_pen,
                )
                _emit_single_span(
                    right_txt,
                    jump_x,
                    jump_y,
                    eff_size,
                    scale_x,
                    scale_y,
                    orientation,
                    font_name,
                    font_dict,
                    is_bold,
                    is_italic,
                    allow_pen_advance=False,
                )
                return

        _emit_single_span(
            cleaned,
            base_x,
            base_y,
            eff_size,
            scale_x,
            scale_y,
            orientation,
            font_name,
            font_dict,
            is_bold,
            is_italic,
            allow_pen_advance=allow_pen,
        )

    page.extract_text(
        visitor_text=on_text,
        visitor_operand_before=on_op_before,
        visitor_operand_after=on_op_after,
    )

    return RawPageData(
        page=page_number,
        width=coord.page_w,
        height=coord.page_h,
        rotation=coord.rotation,
        spans=spans,
        lines=lines,
        rects=rects,
        curves=curves,
        images=images,
    )


def extract_pdf_document(
    source_path: Path,
    *,
    selected_pages: tuple[int, ...] | None = None,
) -> ExtractedPdfDocument:
    """PDF 파일을 열고 지정 페이지(기본 전체)의 원시 기하 데이터를 추출한다."""
    if not source_path.exists():
        raise ParserInputError(f"입력 PDF 파일을 찾을 수 없다: {source_path.name}")
    if not source_path.is_file():
        raise ParserInputError(f"입력 경로가 일반 파일이 아니다: {source_path.name}")

    size_bytes = source_path.stat().st_size
    if size_bytes == 0:
        raise ParserInputError("빈 PDF 파일이다(0바이트).")

    sha256 = _sha256_file(source_path)

    try:
        reader = pypdf.PdfReader(str(source_path), strict=False)
    except (PdfReadError, OSError, ValueError) as exc:
        raise ParserInputError(
            f"PDF 파일을 열 수 없다(손상되었거나 올바른 PDF가 아님): {type(exc).__name__}"
        ) from exc

    is_encrypted = bool(reader.is_encrypted)
    if is_encrypted:
        # 빈 암호(권한 잠금만 있는 PDF)는 자동 해제 시도
        try:
            decrypted = reader.decrypt("")
        except Exception as exc:
            raise ParserInputError(
                "암호화된 PDF 파일이다. 암호를 해제한 뒤 다시 입력해 달라."
            ) from exc
        if not decrypted:
            raise ParserInputError("암호로 보호된 PDF 파일이다. 암호를 해제한 뒤 다시 입력해 달라.")

    try:
        total_pages = len(reader.pages)
    except Exception as exc:
        raise ParserInputError("PDF 페이지 트리를 읽을 수 없다.") from exc

    if total_pages <= 0:
        raise ParserInputError("페이지가 없는 PDF 파일이다.")

    if selected_pages is not None:
        invalid = [p for p in selected_pages if p < 1 or p > total_pages]
        if invalid:
            raise ParserInputError(
                f"요청한 페이지 번호가 범위를 벗어났다(총 {total_pages}페이지): {invalid}"
            )
        target_pages = list(dict.fromkeys(selected_pages))
    else:
        target_pages = list(range(1, total_pages + 1))

    meta_raw = reader.metadata
    outline = _extract_outline(reader)
    metadata = DocumentMetadata(
        source_path=source_path.name,
        format=SourceFormat.PDF,
        file_size_bytes=size_bytes,
        sha256=sha256,
        page_count=total_pages,
        title=_safe_str(getattr(meta_raw, "title", None)) if meta_raw else None,
        author=_safe_str(getattr(meta_raw, "author", None)) if meta_raw else None,
        creator=_safe_str(getattr(meta_raw, "creator", None)) if meta_raw else None,
        producer=_safe_str(getattr(meta_raw, "producer", None)) if meta_raw else None,
        has_outline=bool(outline),
        is_encrypted=is_encrypted,
    )

    raw_pages: list[RawPageData] = []
    for page_num in target_pages:
        raw_pages.append(extract_page_primitives(reader.pages[page_num - 1], page_num))

    return ExtractedPdfDocument(metadata=metadata, outline=outline, pages=raw_pages)
