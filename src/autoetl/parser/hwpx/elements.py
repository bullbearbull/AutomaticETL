"""KS X 6101 OWPML(`.hwpx`) 섹션·문단·표·수식·그림·차트·도식·각주/미주·머리글/바닥글 추출기.

`Contents/section*.xml`을 구조적으로 순회하여 `<hp:ctrl>` 안의 머리글·바닥글·각주·미주·숨은주석이
일반 본문 문단에 섞이지 않도록 분리하고, `<hp:tbl>`의 격자 좌표(`cellAddr`)와 병합(`cellSpan`),
한컴 수식 스크립트(`<hp:equation><hp:script>`), `<hp:pic>` 바이너리(`BinData/*`),
`<hp:chart>` 및 `<hp:container>` 도형 그룹을 추출한다.
"""

from __future__ import annotations

import contextlib
import posixpath
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from autoetl.parser.contracts import ExtractedTable, ParseStrategy, TableCell
from autoetl.parser.docx.elements import (
    RawDocxBodyItem,
    RawDocxPage,
    RawDocxParagraph,
    RawDocxTableItem,
    RawDocxVectorDiagram,
    _format_ordered_list_marker,
)
from autoetl.parser.docx.ooxml import DocxCommentEntry, Relationship
from autoetl.parser.hwpx.owpml import (
    DEFAULT_MARGIN_BOTTOM_PT,
    DEFAULT_MARGIN_LEFT_PT,
    DEFAULT_MARGIN_RIGHT_PT,
    DEFAULT_MARGIN_TOP_PT,
    DEFAULT_PAGE_HEIGHT_PT,
    DEFAULT_PAGE_WIDTH_PT,
    HwpxHeaderCatalog,
    HwpxPackage,
    _get_attr_local,
    clamp,
    hwpunit_to_pt,
    iter_hwpx_children,
    local_name,
    make_bbox,
    probe_image_dimensions,
)
from autoetl.parser.pptx.elements import (
    RawPptxChart,
    RawPptxImage,
    RawPptxOleObject,
    _extract_chartex,
    _extract_embedded_xlsx_table,
    _extract_native_chart,
    _extract_svg_text_hints,
)
from autoetl.parser.pptx.ooxml import PptxPackage

_BULLET_CHARS = ("•", "▪", "▫", "■", "□", "●", "○", "◦", "◆", "◇", "▶", "▷", "·", "-", "*", "")

_HWP_EQ_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    ("LEFT (", "("),
    ("RIGHT )", ")"),
    ("LEFT [", "["),
    ("RIGHT ]", "]"),
    ("LEFT {", "{"),
    ("RIGHT }", "}"),
    ("LEFT |", "|"),
    ("RIGHT |", "|"),
    ("LEFT", ""),
    ("RIGHT", ""),
    ("sum from", "∑_"),
    ("SUM from", "∑_"),
    ("SUM_", "∑_"),
    ("sum_", "∑_"),
    ("prod from", "∏_"),
    ("PROD from", "∏_"),
    ("int from", "∫_"),
    ("INT from", "∫_"),
    ("INT_", "∫_"),
    ("int_", "∫_"),
    ("lim from", "lim_"),
    ("LIM from", "lim_"),
    ("LIM_", "lim_"),
    (" over ", "/"),
    (" times ", " \u00d7 "),
    (" cdot ", " · "),
    (" +- ", " ± "),
    (" -+ ", " ∓ "),
    (" != ", " ≠ "),
    (" <= ", " ≤ "),
    (" >= ", " ≥ "),
    (" -> ", " → "),
    (" <-> ", " ↔ "),
    (" => ", " ⇒ "),
    (" infty", " ∞"),
    (" inf", " ∞"),
    ("alpha", "\u03b1"),
    ("beta", "β"),
    ("gamma", "\u03b3"),
    ("delta", "δ"),
    ("theta", "θ"),
    ("lambda", "λ"),
    ("mu", "μ"),
    ("pi", "π"),
    ("sigma", "\u03c3"),
    ("omega", "ω"),
    ("Delta", "Δ"),
    ("Sigma", "Σ"),
    ("Omega", "Ω"),
)

_CIRCLED_DIGITS = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
_CIRCLED_HANGUL_SYLLABLES = "㉮㉯㉰㉱㉲㉳㉴㉵㉶㉷㉸㉹㉺㉻"
_CIRCLED_HANGUL_JAMO = "㉠㉡㉢㉣㉤㉥㉦㉧㉨㉩㉪㉫㉬㉭"

_SHAPE_TAGS = frozenset(
    {
        "container",
        "rect",
        "ellipse",
        "arc",
        "polygon",
        "curve",
        "connectLine",
        "line",
        "textart",
    }
)


class _HwpxChartBridge:
    """`pptx/elements.py`의 차트 추출기를 `HwpxPackage`와 연결하는 브리지."""

    def __init__(self, pkg: HwpxPackage) -> None:
        self._pkg = pkg

    def read_xml(self, part_path: str) -> ET.Element | None:
        return self._pkg.read_xml(part_path)

    def read_bytes(self, part_path: str) -> bytes | None:
        return self._pkg.read_bytes(part_path)

    def read_rels(self, part_path: str) -> dict[str, Relationship]:
        return self._pkg.read_rels(part_path)

    def has_part(self, part_path: str) -> bool:
        return self._pkg.has_part(part_path)


@dataclass
class HwpxSectionLayout:
    """`<hp:secPr>` 및 `<hp:colPr>`에서 추출한 페이지·여백·단수 설정."""

    width_pt: float = DEFAULT_PAGE_WIDTH_PT
    height_pt: float = DEFAULT_PAGE_HEIGHT_PT
    margin_top_pt: float = DEFAULT_MARGIN_TOP_PT
    margin_bottom_pt: float = DEFAULT_MARGIN_BOTTOM_PT
    margin_left_pt: float = DEFAULT_MARGIN_LEFT_PT
    margin_right_pt: float = DEFAULT_MARGIN_RIGHT_PT
    column_count: int = 1
    column_space_pt: float = 11.34


def normalize_hwp_equation_script(script: str) -> str:
    """한컴 수식 편집기 스크립트(`<hp:script>`)를 읽기 쉬운 선형 수식 표현으로 정규화한다."""
    cleaned = " ".join(script.replace("\r", " ").replace("\n", " ").replace("~", " ").split())
    if not cleaned:
        return ""
    for src, dst in _HWP_EQ_REPLACEMENTS:
        cleaned = cleaned.replace(src, dst)
    cleaned = re.sub(r"\\frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}", r"(\1)/(\2)", cleaned)
    cleaned = re.sub(r"\\?sqrt\s*\[([^\[\]]+)\]\s*\{([^{}]*)\}", r"root(\1, \2)", cleaned)
    cleaned = re.sub(r"\bto\s*\{", "^{", cleaned)
    cleaned = re.sub(r"\\?sqrt\s*\{([^{}]*)\}", r"sqrt(\1)", cleaned)
    cleaned = re.sub(r"\{(sqrt\([^{}]*\))\}", r"\1", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _map_hwpx_num_format(hwp_fmt: str) -> str:
    fmt = hwp_fmt.strip().upper()
    if fmt in ("LATIN_SMALL", "LOWER_LETTER"):
        return "lowerLetter"
    if fmt in ("LATIN_CAPITAL", "UPPER_LETTER"):
        return "upperLetter"
    if fmt in ("ROMAN_SMALL", "LOWER_ROMAN"):
        return "lowerRoman"
    if fmt in ("ROMAN_CAPITAL", "UPPER_ROMAN"):
        return "upperRoman"
    if fmt in ("HANGUL_SYLLABLE", "GANADA"):
        return "ganada"
    if fmt in ("CIRCLED_HANGUL_SYLLABLE", "CIRCLED_GANADA"):
        return "circledGanada"
    if fmt in ("CIRCLED_HANGUL_JAMO", "CIRCLED_CHOSUNG"):
        return "circledChosung"
    if fmt in ("CIRCLED_DIGIT", "CIRCLED_NUMBER"):
        return "circledDecimal"
    if fmt in ("HANGUL_JAMO", "CHOSUNG"):
        return "chosung"
    return "decimal"


def _format_hwpx_num_token(val: int, hwp_fmt: str) -> str:
    """단일 레벨 번호 값 `val`을 OWPML `numFormat` 토큰(마침표 제외)으로 변환한다."""
    mapped = _map_hwpx_num_format(hwp_fmt)
    if mapped == "circledDecimal":
        if 1 <= val <= len(_CIRCLED_DIGITS):
            return _CIRCLED_DIGITS[val - 1]
        return f"({val})"
    if mapped == "circledGanada":
        if 1 <= val <= len(_CIRCLED_HANGUL_SYLLABLES):
            return _CIRCLED_HANGUL_SYLLABLES[val - 1]
        return str(val)
    if mapped == "circledChosung":
        if 1 <= val <= len(_CIRCLED_HANGUL_JAMO):
            return _CIRCLED_HANGUL_JAMO[val - 1]
        return str(val)
    marker = _format_ordered_list_marker(val, mapped)
    return marker[:-1] if marker.endswith(".") else marker


def _format_hwpx_list_marker(
    *,
    num_id: str,
    list_level: int,
    catalog: HwpxHeaderCatalog,
    list_counters: dict[tuple[str, int], int],
) -> str:
    """`<hh:paraHead>` 템플릿(`^1.^2.`, `^3)`, `(^5)`)을 반영해 번호 마커를 생성한다."""
    lvl_1based = list_level + 1
    num_spec = catalog.numberings.get((num_id, lvl_1based))
    start_val = num_spec.start if num_spec is not None else 1
    hwp_fmt = num_spec.num_format if num_spec is not None else "DIGIT"

    key = (num_id, list_level)
    cur = list_counters.get(key, start_val - 1) + 1
    list_counters[key] = cur
    for k_nid, k_lvl in list(list_counters):
        if k_nid == num_id and k_lvl > list_level:
            del list_counters[(k_nid, k_lvl)]

    tmpl = (num_spec.template if num_spec is not None else "").strip()
    if tmpl and "^" in tmpl:

        def _replace_level_placeholder(match: re.Match[str]) -> str:
            ref_1based = int(match.group(1))
            ref_0based = ref_1based - 1
            ref_spec = catalog.numberings.get((num_id, ref_1based))
            ref_start = ref_spec.start if ref_spec is not None else 1
            ref_fmt = ref_spec.num_format if ref_spec is not None else hwp_fmt
            if (num_id, ref_0based) not in list_counters:
                list_counters[(num_id, ref_0based)] = ref_start
            ref_val = list_counters[(num_id, ref_0based)]
            return _format_hwpx_num_token(ref_val, ref_fmt)

        return re.sub(r"\^(\d+)", _replace_level_placeholder, tmpl)

    tok = _format_hwpx_num_token(cur, hwp_fmt)
    if _map_hwpx_num_format(hwp_fmt) in ("circledDecimal", "circledGanada", "circledChosung"):
        return tok
    return f"{tok}."


def _parse_sec_pr_into(sec_pr: ET.Element, layout: HwpxSectionLayout) -> None:
    """`<hp:secPr>` 요소에서 용지 크기와 여백을 읽어 `layout`을 갱신한다.

    KS X 6101 OWPML 규격에서는 `landscape="WIDELY"`가 세로(Portrait) 용지이고,
    `landscape="NARROWLY"`가 가로(Landscape) 용지다.
    """
    for ch in iter_hwpx_children(sec_pr):
        ctag = local_name(ch.tag)
        if ctag == "pagePr":
            w_pt = hwpunit_to_pt(_get_attr_local(ch, "width"), layout.width_pt)
            h_pt = hwpunit_to_pt(_get_attr_local(ch, "height"), layout.height_pt)
            landscape = _get_attr_local(ch, "landscape", "WIDELY").upper()
            if w_pt > 36.0 and h_pt > 36.0:
                if landscape in ("NARROWLY", "LANDSCAPE", "1", "TRUE") and w_pt < h_pt:
                    layout.width_pt, layout.height_pt = h_pt, w_pt
                else:
                    layout.width_pt, layout.height_pt = w_pt, h_pt
            for sub in iter_hwpx_children(ch):
                if local_name(sub.tag) == "margin":
                    _apply_margin_el(sub, layout)
        elif ctag == "margin":
            _apply_margin_el(ch, layout)


def _apply_margin_el(margin_el: ET.Element, layout: HwpxSectionLayout) -> None:
    for attr, field_name, default_val in (
        ("top", "margin_top_pt", DEFAULT_MARGIN_TOP_PT),
        ("bottom", "margin_bottom_pt", DEFAULT_MARGIN_BOTTOM_PT),
        ("left", "margin_left_pt", DEFAULT_MARGIN_LEFT_PT),
        ("right", "margin_right_pt", DEFAULT_MARGIN_RIGHT_PT),
    ):
        raw = _get_attr_local(margin_el, attr)
        if raw:
            val_pt = max(0.0, hwpunit_to_pt(raw, default_val))
            setattr(layout, field_name, val_pt)


def _parse_col_pr_into(col_pr: ET.Element, layout: HwpxSectionLayout) -> None:
    """`<hp:colPr>` 요소에서 다단(`colCount`, `sameGap`) 설정을 읽어 `layout`을 갱신한다."""
    raw_cnt = _get_attr_local(col_pr, "colCount", "1")
    with contextlib.suppress(ValueError):
        layout.column_count = max(1, min(6, int(raw_cnt)))
    raw_gap = _get_attr_local(col_pr, "sameGap")
    if raw_gap:
        gap_pt = hwpunit_to_pt(raw_gap, 11.34)
        if gap_pt >= 0.0:
            layout.column_space_pt = gap_pt


def _extract_t_text(t_el: ET.Element) -> str:
    """`<hp:t>` 요소 내부 텍스트와 인라인 제어 태그(`lineBreak`, `tab` 등)를 추출한다."""
    pieces: list[str] = []
    if t_el.text:
        pieces.append(t_el.text)
    in_deleted = False
    for ch in t_el:
        ctag = local_name(ch.tag)
        if ctag == "deleteBegin":
            in_deleted = True
        elif ctag == "deleteEnd":
            in_deleted = False
        elif not in_deleted:
            if ctag == "lineBreak":
                pieces.append("\n")
            elif ctag == "tab":
                pieces.append("\t")
            elif ctag in ("nbSpace", "fwSpace"):
                pieces.append(" ")
            elif ctag in ("hyphen", "hypen"):
                pieces.append("-")
            elif ch.text:
                pieces.append(ch.text)
        if ch.tail and not in_deleted:
            pieces.append(ch.tail)
    return "".join(pieces)


def _extract_sublist_plain_text(container: ET.Element) -> str:
    """머리글·바닥글·각주·미주·캡션 컨테이너 내부의 문단 텍스트를 추출한다."""
    lines: list[str] = []
    for p_el in container.iter():
        if local_name(p_el.tag) != "p":
            continue
        p_pieces: list[str] = []
        for ch in iter_hwpx_children(p_el):
            if local_name(ch.tag) == "run":
                for r_ch in iter_hwpx_children(ch):
                    rtag = local_name(r_ch.tag)
                    if rtag == "t":
                        p_pieces.append(_extract_t_text(r_ch))
                    elif rtag == "equation":
                        eq_txt = _extract_equation_text(r_ch)
                        if eq_txt:
                            p_pieces.append(eq_txt)
        line = "".join(p_pieces).strip()
        if line:
            lines.append(line)
    return " ".join(lines).strip()


def _extract_equation_text(eq_el: ET.Element) -> str:
    for ch in iter_hwpx_children(eq_el):
        if local_name(ch.tag) == "script" and ch.text:
            return normalize_hwp_equation_script(ch.text)
    for sub in eq_el.iter():
        if local_name(sub.tag) == "script" and sub.text:
            return normalize_hwp_equation_script(sub.text)
    return ""


def _extract_element_size_pt(
    elem: ET.Element,
    *,
    default_w: float = 220.0,
    default_h: float = 140.0,
    page_w: float = DEFAULT_PAGE_WIDTH_PT,
    page_h: float = DEFAULT_PAGE_HEIGHT_PT,
) -> tuple[float, float]:
    """`<hp:sz>`, `<hp:curSz>`, `<hp:orgSz>` 순으로 요소의 실제 너비·높이(pt)를 추출한다."""
    for tag_pref in ("sz", "curSz", "orgSz"):
        for ch in iter_hwpx_children(elem):
            if local_name(ch.tag) == tag_pref:
                w = hwpunit_to_pt(_get_attr_local(ch, "width"), 0.0)
                h = hwpunit_to_pt(_get_attr_local(ch, "height"), 0.0)
                if w > 0.0 and h > 0.0:
                    return (
                        clamp(w, 0.5, page_w),
                        clamp(h, 0.5, page_h),
                    )
    w_attr = hwpunit_to_pt(_get_attr_local(elem, "width"), 0.0)
    h_attr = hwpunit_to_pt(_get_attr_local(elem, "height"), 0.0)
    if w_attr > 0.0 and h_attr > 0.0:
        return (clamp(w_attr, 0.5, page_w), clamp(h_attr, 0.5, page_h))
    return (default_w, default_h)


def _extract_embedded_caption_text(elem: ET.Element) -> str | None:
    """`<hp:tbl>` 또는 `<hp:pic>` 직속 `<hp:caption>`의 텍스트를 추출한다."""
    for ch in iter_hwpx_children(elem):
        if local_name(ch.tag) == "caption":
            txt = _extract_sublist_plain_text(ch)
            if txt:
                return txt
    return None


@dataclass
class _SectionParseContext:
    pkg: HwpxPackage
    catalog: HwpxHeaderCatalog
    layout: HwpxSectionLayout
    list_counters: dict[tuple[str, int], int]
    seq_counter: list[int]
    note_counter: list[int]
    headers: list[str]
    footers: list[str]
    footnotes: dict[str, str]
    endnotes: dict[str, str]
    comments: dict[str, DocxCommentEntry]


def _extract_pic_item(
    pic_el: ET.Element,
    ctx: _SectionParseContext,
) -> list[RawDocxBodyItem]:
    """`<hp:pic>` 요소에서 내장 이미지(`RawPptxImage`)와 캡션 문단을 추출한다."""
    bin_id = ""
    for sub in pic_el.iter():
        if local_name(sub.tag) in ("img", "pic"):
            cand = (
                _get_attr_local(sub, "binaryItemIDRef")
                or _get_attr_local(sub, "binItemIDRef")
                or _get_attr_local(sub, "binData")
            ).strip()
            if cand:
                bin_id = cand
                break

    w_pt, h_pt = _extract_element_size_pt(
        pic_el,
        default_w=240.0,
        default_h=150.0,
        page_w=ctx.layout.width_pt,
        page_h=ctx.layout.height_pt,
    )
    media_part = ctx.pkg.resolve_binary_item(bin_id) or (f"BinData/{bin_id}" if bin_id else "")
    raw_bytes = ctx.pkg.read_bytes(media_part) if media_part else None
    ext = (posixpath.splitext(media_part)[1].lower() if media_part else "") or ".png"
    px_w, px_h = probe_image_dimensions(raw_bytes, ext) if raw_bytes is not None else (None, None)
    svg_hints = _extract_svg_text_hints(raw_bytes) if (ext == ".svg" and raw_bytes) else ()

    cap_txt = _extract_embedded_caption_text(pic_el)
    ctx.seq_counter[0] += 1
    img_item = RawPptxImage(
        shape_id=_get_attr_local(pic_el, "id") or str(ctx.seq_counter[0]),
        name=posixpath.basename(media_part) if media_part else f"Image {ctx.seq_counter[0]}",
        alt_text=cap_txt or "",
        media_part=media_part or f"BinData/image_{ctx.seq_counter[0]}{ext}",
        extension=ext,
        rect_pt=(0.0, 0.0, w_pt, h_pt),
        raw_bytes=raw_bytes,
        pixel_width=px_w,
        pixel_height=px_h,
        svg_text_hints=svg_hints,
    )
    out: list[RawDocxBodyItem] = [img_item]
    if cap_txt:
        out.append(
            RawDocxParagraph(
                text=cap_txt,
                font_size_pt=ctx.catalog.default_font_size_pt,
            )
        )
    return out


def _extract_chart_item(
    chart_el: ET.Element,
    ctx: _SectionParseContext,
) -> list[RawDocxBodyItem]:
    """`<hp:chart>` 요소에서 OOXML 차트 XML(`Chart/*.xml`)을 찾아 `RawPptxChart`로 추출한다."""
    chart_ref = (
        _get_attr_local(chart_el, "chartIDRef")
        or _get_attr_local(chart_el, "binaryItemIDRef")
        or _get_attr_local(chart_el, "id")
    ).strip()
    chart_part = ctx.pkg.resolve_binary_item(chart_ref) if chart_ref else None
    if chart_part is None and chart_ref:
        for cand in (
            chart_ref,
            f"Chart/{chart_ref}",
            f"Chart/{chart_ref}.xml",
            f"Contents/Chart/{chart_ref}.xml",
        ):
            if ctx.pkg.has_part(cand):
                chart_part = cand
                break
    if chart_part is None:
        return []

    w_pt, h_pt = _extract_element_size_pt(
        chart_el,
        default_w=320.0,
        default_h=180.0,
        page_w=ctx.layout.width_pt,
        page_h=ctx.layout.height_pt,
    )
    rect_pt = (0.0, 0.0, w_pt, h_pt)
    bridge: PptxPackage = _HwpxChartBridge(ctx.pkg)  # type: ignore[assignment]
    ctx.seq_counter[0] += 1
    chart = _extract_native_chart(
        bridge,
        chart_part,
        shape_id=str(ctx.seq_counter[0]),
        shape_name=posixpath.basename(chart_part),
        rect_pt=rect_pt,
    )
    if chart is None:
        chart = _extract_chartex(
            bridge,
            chart_part,
            shape_id=str(ctx.seq_counter[0]),
            shape_name=posixpath.basename(chart_part),
            rect_pt=rect_pt,
        )
    if chart is None:
        return []
    out: list[RawDocxBodyItem] = [chart]
    cap_txt = _extract_embedded_caption_text(chart_el)
    if cap_txt:
        out.append(RawDocxParagraph(text=cap_txt, font_size_pt=ctx.catalog.default_font_size_pt))
    return out


def _extract_ole_item(
    ole_el: ET.Element,
    ctx: _SectionParseContext,
) -> list[RawDocxBodyItem]:
    """`<hp:ole>` 요소에서 내장 `.xlsx` 표 또는 `RawPptxOleObject`를 추출한다."""
    bin_id = (
        _get_attr_local(ole_el, "binaryItemIDRef") or _get_attr_local(ole_el, "binItemIDRef")
    ).strip()
    w_pt, h_pt = _extract_element_size_pt(
        ole_el,
        default_w=280.0,
        default_h=160.0,
        page_w=ctx.layout.width_pt,
        page_h=ctx.layout.height_pt,
    )
    rect_pt = (0.0, 0.0, w_pt, h_pt)
    part = ctx.pkg.resolve_binary_item(bin_id) if bin_id else None
    ole_bytes = ctx.pkg.read_bytes(part) if part else None
    ctx.seq_counter[0] += 1
    if ole_bytes is not None:
        xlsx_tbl = _extract_embedded_xlsx_table(
            ole_bytes,
            slide_number=1,
            table_index=ctx.seq_counter[0],
            rect_pt=rect_pt,
            slide_w=ctx.layout.width_pt,
            slide_h=ctx.layout.height_pt,
            ole_name=bin_id or "HWPX OLE",
        )
        if xlsx_tbl is not None:
            return [
                RawDocxTableItem(
                    table=xlsx_tbl,
                    raw_width_pt=w_pt,
                    raw_height_pt=h_pt,
                )
            ]
    return [
        RawPptxOleObject(
            shape_id=str(ctx.seq_counter[0]),
            name=bin_id or f"OLE {ctx.seq_counter[0]}",
            prog_id=_get_attr_local(ole_el, "objectType", "OLE"),
            rect_pt=rect_pt,
            preview_image=None,
        )
    ]


def _extract_shape_or_container_items(
    shape_el: ET.Element,
    ctx: _SectionParseContext,
) -> list[RawDocxBodyItem]:
    """`<hp:container>`, `<hp:rect>`, `<hp:textart>` 등 그리기 개체에서 텍스트와 도식을 추출한다."""
    items: list[RawDocxBodyItem] = []
    shape_texts: list[str] = []
    shape_count = 0

    def _walk_shape(el: ET.Element) -> None:
        nonlocal shape_count
        tag = local_name(el.tag)
        if tag in _SHAPE_TAGS and tag != "container":
            shape_count += 1
        if tag == "textart":
            raw_ta = _get_attr_local(el, "text")
            if raw_ta:
                cleaned_ta = "\n".join(
                    ln.strip()
                    for ln in raw_ta.replace("\u240d\u240a", "\n")
                    .replace("\u240d", "\n")
                    .replace("\u240a", "\n")
                    .splitlines()
                    if ln.strip()
                )
                if cleaned_ta:
                    shape_texts.append(cleaned_ta)
                    items.append(
                        RawDocxParagraph(
                            text=cleaned_ta,
                            font_size_pt=ctx.catalog.default_font_size_pt,
                            in_textbox=True,
                        )
                    )
        for ch in iter_hwpx_children(el):
            ctag = local_name(ch.tag)
            if ctag in ("drawText", "subList"):
                for p_el in iter_hwpx_children(ch):
                    if local_name(p_el.tag) == "p":
                        p_items, _ = _extract_paragraph_items(p_el, ctx, in_textbox=True)
                        for pit in p_items:
                            if isinstance(pit, RawDocxParagraph) and pit.text:
                                shape_texts.append(pit.text)
                            items.append(pit)
                    elif local_name(p_el.tag) == "subList":
                        for sub_p in iter_hwpx_children(p_el):
                            if local_name(sub_p.tag) == "p":
                                p_items, _ = _extract_paragraph_items(sub_p, ctx, in_textbox=True)
                                for pit in p_items:
                                    if isinstance(pit, RawDocxParagraph) and pit.text:
                                        shape_texts.append(pit.text)
                                    items.append(pit)
            elif ctag == "pic":
                items.extend(_extract_pic_item(ch, ctx))
            elif ctag == "tbl":
                tbl_it, side_vis = _extract_hwpx_table(ch, ctx)
                if tbl_it is not None:
                    items.append(tbl_it)
                items.extend(side_vis)
            elif ctag in _SHAPE_TAGS:
                _walk_shape(ch)

    _walk_shape(shape_el)

    is_container = local_name(shape_el.tag) == "container"
    if (is_container and shape_count >= 2) or shape_count >= 3:
        w_pt, h_pt = _extract_element_size_pt(
            shape_el,
            default_w=260.0,
            default_h=140.0,
            page_w=ctx.layout.width_pt,
            page_h=ctx.layout.height_pt,
        )
        # 소형 글머리 장식 아이콘(14x14pt) 및 구분선 장식(473x7.6pt)은 도식에서 제외
        is_tiny_ornament = w_pt < 48.0 or h_pt < 28.0
        # 폭이 넓고 낮은 단행 제목 배너(예: '1장 기상전망')는 텍스트가 이미 추출되므로 제외
        is_title_banner = (
            h_pt < 65.0
            and (w_pt / max(h_pt, 1.0)) >= 7.0
            and shape_count <= 4
            and len(shape_texts) <= 2
        )
        if not is_tiny_ornament and not is_title_banner:
            ctx.seq_counter[0] += 1
            hints = tuple(dict.fromkeys(t.splitlines()[0][:60] for t in shape_texts if t))[:20]
            items.append(
                RawDocxVectorDiagram(
                    diagram_id=f"d_{ctx.seq_counter[0]}",
                    name=f"HWPX Diagram {ctx.seq_counter[0]}",
                    width_pt=w_pt,
                    height_pt=h_pt,
                    shape_count=shape_count,
                    text_hints=hints,
                )
            )
    return items


def _extract_hwpx_table(
    tbl_el: ET.Element,
    ctx: _SectionParseContext,
) -> tuple[RawDocxTableItem | None, list[RawDocxBodyItem]]:
    """`<hp:tbl>`에서 `cellAddr`·`cellSpan`·`borderFillIDRef`를 해석해 표를 생성한다."""
    tr_list = [ch for ch in iter_hwpx_children(tbl_el) if local_name(ch.tag) == "tr"]
    if not tr_list:
        return None, []

    decl_rows = 0
    decl_cols = 0
    with contextlib.suppress(ValueError):
        decl_rows = max(0, int(_get_attr_local(tbl_el, "rowCnt", "0")))
    with contextlib.suppress(ValueError):
        decl_cols = max(0, int(_get_attr_local(tbl_el, "colCnt", "0")))

    @dataclass
    class _ParsedTc:
        tc_el: ET.Element
        row: int
        col: int
        row_span: int
        col_span: int
        width_pt: float
        is_header: bool
        border_fill_id: str

    parsed_tcs: list[_ParsedTc] = []
    occupied: set[tuple[int, int]] = set()
    max_row = decl_rows
    max_col = decl_cols

    for r_fallback, tr_el in enumerate(tr_list):
        tr_is_hdr = _get_attr_local(tr_el, "header", "0").lower() not in ("0", "false", "")
        c_cursor = 0
        for tc_el in iter_hwpx_children(tr_el):
            if local_name(tc_el.tag) != "tc":
                continue
            explicit_r: int | None = None
            explicit_c: int | None = None
            r_span = 1
            c_span = 1
            w_pt = 80.0

            for sub in iter_hwpx_children(tc_el):
                stag = local_name(sub.tag)
                if stag == "cellAddr":
                    with contextlib.suppress(ValueError):
                        explicit_c = max(0, int(_get_attr_local(sub, "colAddr", "0")))
                    with contextlib.suppress(ValueError):
                        explicit_r = max(0, int(_get_attr_local(sub, "rowAddr", "0")))
                elif stag == "cellSpan":
                    with contextlib.suppress(ValueError):
                        c_span = max(1, int(_get_attr_local(sub, "colSpan", "1")))
                    with contextlib.suppress(ValueError):
                        r_span = max(1, int(_get_attr_local(sub, "rowSpan", "1")))
                elif stag == "cellSz":
                    cand_w = hwpunit_to_pt(_get_attr_local(sub, "width"), 80.0)
                    if cand_w > 4.0:
                        w_pt = cand_w

            if explicit_r is not None and explicit_c is not None:
                r_idx = explicit_r
                c_idx = explicit_c
            else:
                r_idx = r_fallback
                while (r_idx, c_cursor) in occupied:
                    c_cursor += 1
                c_idx = c_cursor

            for dr in range(r_idx, r_idx + r_span):
                for dc in range(c_idx, c_idx + c_span):
                    occupied.add((dr, dc))
            c_cursor = c_idx + c_span
            max_row = max(max_row, r_idx + r_span)
            max_col = max(max_col, c_idx + c_span)

            tc_hdr = tr_is_hdr or (
                _get_attr_local(tc_el, "header", "0").lower() not in ("0", "false", "")
            )
            bf_id = _get_attr_local(tc_el, "borderFillIDRef")
            parsed_tcs.append(
                _ParsedTc(
                    tc_el=tc_el,
                    row=r_idx,
                    col=c_idx,
                    row_span=r_span,
                    col_span=c_span,
                    width_pt=w_pt,
                    is_header=tc_hdr,
                    border_fill_id=bf_id,
                )
            )

    n_rows = max_row
    n_cols = max_col
    if n_rows <= 0 or n_cols <= 0 or not parsed_tcs:
        return None, []

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
        is_header: bool

    raw_cells: list[_DraftCell] = []

    for ptc in parsed_tcs:
        if ptc.row >= n_rows or ptc.col >= n_cols:
            continue
        r_span = min(ptc.row_span, n_rows - ptc.row)
        c_span = min(ptc.col_span, n_cols - ptc.col)
        if r_span > 1 or c_span > 1:
            has_merged = True
        if ptc.row == 0 and c_span > 1:
            merged_header_cols.update(range(ptc.col + 1, ptc.col + c_span))
        if c_span == 1 and ptc.width_pt > 4.0:
            col_widths[ptc.col] = ptc.width_pt
        if ptc.border_fill_id:
            border_fill_ids.append(ptc.border_fill_id)

        cell_lines: list[str] = []
        raw_aligned_lines: list[str] = []
        saved_counters = dict(ctx.list_counters)
        ctx.list_counters.clear()

        for sub in iter_hwpx_children(ptc.tc_el):
            if local_name(sub.tag) != "subList":
                continue
            for p_el in iter_hwpx_children(sub):
                if local_name(p_el.tag) != "p":
                    continue
                p_items, _ = _extract_paragraph_items(p_el, ctx)
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
        grid_text[ptc.row][ptc.col] = cell_text
        grid_line_lists[ptc.row][ptc.col] = raw_aligned_lines
        raw_cells.append(
            _DraftCell(
                row=ptc.row,
                col=ptc.col,
                row_span=r_span,
                col_span=c_span,
                text=cell_text,
                is_header=ptc.is_header,
            )
        )

    if not any(c.text.strip() for c in raw_cells):
        return None, side_visuals

    # 병합 없는 표에서 단일 <hp:tr>의 비어 있지 않은 열들이 동일 줄 수(K>=3)를 담고 있으면 행 분할
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
                _DraftCell(
                    row=r_i,
                    col=c_i,
                    row_span=1,
                    col_span=1,
                    text=grid_text[r_i][c_i],
                    is_header=(r_i == 0),
                )
                for r_i in range(n_rows)
                for c_i in range(n_cols)
            ]

    has_explicit_hdr = any(rc.row == 0 and rc.is_header for rc in raw_cells)
    header_row_count = 1 if (has_explicit_hdr or n_rows >= 2) else 0

    cells: list[TableCell] = [
        TableCell(
            row=rc.row,
            col=rc.col,
            row_span=rc.row_span,
            col_span=rc.col_span,
            text=rc.text,
            is_header=(rc.row == 0 and header_row_count == 1),
        )
        for rc in raw_cells
    ]

    # 모든 행이 100% 비어 있는 여백 열 압축
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

    # 100% 비어 있는 장식·여백 행 압축 (상단·중간·하단 테두리 여백 행 제거)
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
                    update={
                        "row": new_r,
                        "row_span": new_rspan,
                        "is_header": (new_r == 0),
                    }
                )
            )
        cells = compacted_rcells
        n_rows = len(non_empty_rows)

    # 한국 공공문서 특유의 '상단 중앙 제목 배지 + 하단 전체 폭 본문' 박스형 표는 1열로 정규화
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

    header_row_count = 1 if (has_explicit_hdr or n_rows >= 1) else 0
    if n_rows >= 2:
        has_any_hdr_text = any(grid_text[0][c].strip() for c in range(n_cols))
        headers = [
            grid_text[0][c]
            if (has_any_hdr_text or grid_text[0][c] or c in merged_header_cols)
            else f"col_{c + 1}"
            for c in range(n_cols)
        ]
        data_rows = grid_text[1:]
    else:
        has_any_hdr_text = any(grid_text[0][c].strip() for c in range(n_cols))
        headers = [
            grid_text[0][c]
            if (has_any_hdr_text or grid_text[0][c] or c in merged_header_cols)
            else f"col_{c + 1}"
            for c in range(n_cols)
        ]
        data_rows = []

    tbl_bf = _get_attr_local(tbl_el, "borderFillIDRef")
    if tbl_bf:
        border_fill_ids.append(tbl_bf)
    strategy = ctx.catalog.resolve_table_border_strategy(border_fill_ids)
    strat_desc = {
        ParseStrategy.RULED_TABLE: "격자선 표",
        ParseStrategy.HORIZONTAL_RULED_TABLE: "가로선(음영/상하선) 표",
        ParseStrategy.BORDERLESS_TABLE: "무선 표",
    }.get(strategy, "OWPML 표")
    merged_note = ", 병합 셀 포함" if has_merged else ""

    cap_txt = _extract_embedded_caption_text(tbl_el)
    raw_w = min(ctx.layout.width_pt * 0.88, max(120.0, sum(col_widths[:n_cols])))
    raw_h = min(ctx.layout.height_pt * 0.78, max(36.0, n_rows * 22.0))
    ctx.seq_counter[0] += 1
    extracted = ExtractedTable(
        table_id=f"p001_t{ctx.seq_counter[0]:02d}",
        page=1,
        bbox=make_bbox(0.0, 0.0, raw_w, raw_h, ctx.layout.width_pt, ctx.layout.height_pt),
        strategy=strategy,
        confidence=0.98,
        row_count=n_rows,
        col_count=n_cols,
        header_row_count=header_row_count,
        headers=headers,
        rows=data_rows,
        cells=cells,
        has_merged_cells=has_merged,
        caption=cap_txt,
        reason=f"HWPX 네이티브 OWPML {strat_desc} 직접 추출({n_rows}x{n_cols}{merged_note})",
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


def _extract_hwpx_hyperlink_url(fb_el: ET.Element) -> str:
    """`<hp:fieldBegin type="HYPERLINK">` 내부 파라미터에서 대상 URL을 추출한다."""
    tooltip_url = ""
    cmd_url = ""
    for param in fb_el.iter():
        if local_name(param.tag) == "stringParam":
            p_name = _get_attr_local(param, "name").lower()
            val = (param.text or "").strip()
            if not val:
                continue
            if p_name == "tooltip" and (
                val.startswith(("http://", "https://", "mailto:")) or "://" in val
            ):
                tooltip_url = val
            elif p_name == "command":
                cleaned = val.lstrip("|(").split(";")[0].strip()
                cleaned = cleaned.replace("\\:", ":").replace("\\?", "?")
                cleaned = re.sub(r"^(https?):///+", r"\1://", cleaned)
                if "://" in cleaned or cleaned.startswith("mailto:"):
                    cmd_url = cleaned
    return tooltip_url or cmd_url


def _extract_inline_control_text(el: ET.Element) -> str:
    """덧말(`dutmal`), 글자겹침(`compose`), 양식 컨트롤(`btn`/`edit` 등) 텍스트를 추출한다."""
    tag = local_name(el.tag)
    if tag == "dutmal":
        main_t = "".join(
            (c.text or "") for c in iter_hwpx_children(el) if local_name(c.tag) == "mainText"
        ).strip()
        sub_t = "".join(
            (c.text or "") for c in iter_hwpx_children(el) if local_name(c.tag) == "subText"
        ).strip()
        if main_t and sub_t:
            return f"{main_t}({sub_t})"
        return main_t or sub_t
    if tag == "compose":
        return _get_attr_local(el, "composeText").strip()
    if tag in ("checkBtn", "radioBtn"):
        val = (_get_attr_local(el, "value") or _get_attr_local(el, "checked")).strip().lower()
        is_checked = val in ("1", "true", "checked")
        sym = ("☑" if is_checked else "☐") if tag == "checkBtn" else ("◉" if is_checked else "○")
        cap = _get_attr_local(el, "caption").strip()
        return f"{sym} {cap}" if cap else sym
    if tag == "btn":
        cap = (_get_attr_local(el, "caption") or _get_attr_local(el, "name")).strip()
        return f"[{cap}]" if cap else ""
    if tag == "edit":
        t_vals = [
            (c.text or "").strip()
            for c in iter_hwpx_children(el)
            if local_name(c.tag) == "text" and (c.text or "").strip()
        ]
        val_str = " ".join(t_vals).strip() or _get_attr_local(el, "text").strip()
        if val_str:
            return val_str
        name_str = _get_attr_local(el, "name").strip()
        return f"[{name_str}]" if name_str else ""
    if tag == "comboBox":
        sel = _get_attr_local(el, "selectedValue").strip()
        if sel:
            return sel
        opts = [
            (_get_attr_local(c, "displayText") or _get_attr_local(c, "value")).strip()
            for c in iter_hwpx_children(el)
            if local_name(c.tag) == "listItem"
        ]
        opts = [o for o in opts if o]
        if opts:
            return " / ".join(opts)
        name_str = _get_attr_local(el, "name").strip()
        return f"[{name_str}]" if name_str else ""
    return ""


_INLINE_CONTROL_TAGS = frozenset(
    {"dutmal", "compose", "btn", "checkBtn", "radioBtn", "comboBox", "edit"}
)


def _extract_paragraph_items(
    p_el: ET.Element,
    ctx: _SectionParseContext,
    *,
    in_textbox: bool = False,
) -> tuple[list[RawDocxBodyItem], bool]:
    """`<hp:p>` 문단 하나에서 본문·수식·표·그림·차트·도식과 `page_break_before`를 추출한다."""
    pb_attr = _get_attr_local(p_el, "pageBreak", "0").lower()
    page_break_before = pb_attr not in ("0", "false", "")

    para_pr_id = _get_attr_local(p_el, "paraPrIDRef", "0")
    style_id = _get_attr_local(p_el, "styleIDRef", "0")

    style_def = ctx.catalog.styles.get(style_id)
    para_prop = ctx.catalog.para_props.get(para_pr_id)
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

    pieces: list[str] = []
    attachments: list[RawDocxBodyItem] = []
    run_fonts: list[float] = []
    bold_chars = 0
    italic_chars = 0
    mono_chars = 0
    prose_chars = 0
    math_chars = 0
    has_math = False
    fn_ids: list[str] = []
    en_ids: list[str] = []
    cm_ids: list[str] = []
    active_hyperlink_url = ""

    def _flush_active_hyperlink() -> None:
        nonlocal active_hyperlink_url
        if not active_hyperlink_url:
            return
        url = active_hyperlink_url
        active_hyperlink_url = ""
        current_joined = "".join(pieces)
        if url not in current_joined:
            if pieces and pieces[-1].endswith(" "):
                pieces[-1] = pieces[-1].rstrip(" ")
                pieces.append(f" ({url}) ")
            else:
                pieces.append(f" ({url})")

    def _process_ctrl(ctrl_el: ET.Element) -> None:
        nonlocal prose_chars, active_hyperlink_url
        for ch in iter_hwpx_children(ctrl_el):
            ctag = local_name(ch.tag)
            if ctag == "secPr":
                _parse_sec_pr_into(ch, ctx.layout)
            elif ctag == "colPr":
                _parse_col_pr_into(ch, ctx.layout)
            elif ctag == "header":
                hdr_txt = _extract_sublist_plain_text(ch)
                if hdr_txt and hdr_txt not in ctx.headers:
                    ctx.headers.append(hdr_txt)
            elif ctag == "footer":
                ftr_txt = _extract_sublist_plain_text(ch)
                if ftr_txt and ftr_txt not in ctx.footers:
                    ctx.footers.append(ftr_txt)
            elif ctag == "footNote":
                fn_txt = _extract_sublist_plain_text(ch)
                if fn_txt:
                    raw_num = _get_attr_local(ch, "number") or _get_attr_local(ch, "id")
                    if not raw_num or raw_num in ctx.footnotes:
                        ctx.note_counter[0] += 1
                        raw_num = str(ctx.note_counter[0])
                    ctx.footnotes[raw_num] = fn_txt
                    pieces.append(f"[^{raw_num}]")
                    fn_ids.append(raw_num)
            elif ctag == "endNote":
                en_txt = _extract_sublist_plain_text(ch)
                if en_txt:
                    raw_num = _get_attr_local(ch, "number") or _get_attr_local(ch, "id")
                    if not raw_num or raw_num in ctx.endnotes:
                        ctx.note_counter[0] += 1
                        raw_num = str(ctx.note_counter[0])
                    ctx.endnotes[raw_num] = en_txt
                    pieces.append(f"[^e{raw_num}]")
                    en_ids.append(raw_num)
            elif ctag == "hiddenComment":
                cm_txt = _extract_sublist_plain_text(ch)
                if cm_txt:
                    ctx.note_counter[0] += 1
                    cid = str(ctx.note_counter[0])
                    ctx.comments[cid] = DocxCommentEntry(
                        comment_id=cid,
                        author=_get_attr_local(ch, "author", "HWPX 주석"),
                        initials="",
                        date=_get_attr_local(ch, "date", ""),
                        text=cm_txt,
                    )
                    cm_ids.append(cid)
            elif ctag == "fieldBegin":
                f_type = _get_attr_local(ch, "type").upper()
                if "CHECK" in f_type:
                    checked = False
                    for param in ch.iter():
                        if local_name(param.tag) in ("integerParam", "stringParam", "booleanParam"):
                            p_name = _get_attr_local(param, "name").lower()
                            if p_name in ("checked", "value", "state") and (
                                (param.text or "").strip().lower() in ("1", "true", "checked")
                            ):
                                checked = True
                    pieces.append("☑" if checked else "☐")
                    prose_chars += 1
                elif "HYPERLINK" in f_type:
                    active_hyperlink_url = _extract_hwpx_hyperlink_url(ch)
            elif ctag == "fieldEnd":
                _flush_active_hyperlink()
            elif ctag in _INLINE_CONTROL_TAGS:
                ctrl_txt = _extract_inline_control_text(ch)
                if ctrl_txt:
                    if pieces and not pieces[-1].endswith((" ", "\n", "\t")):
                        pieces.append(" ")
                    pieces.append(ctrl_txt)
                    prose_chars += len(ctrl_txt)

    for ch in iter_hwpx_children(p_el):
        ctag = local_name(ch.tag)
        if ctag == "secPr":
            _parse_sec_pr_into(ch, ctx.layout)
            continue
        if ctag == "colPr":
            _parse_col_pr_into(ch, ctx.layout)
            continue
        if ctag == "ctrl":
            _process_ctrl(ch)
            continue
        if ctag == "linesegarray":
            continue
        if ctag == "run":
            c_ref = _get_attr_local(ch, "charPrIDRef")
            if not c_ref and style_def is not None:
                c_ref = style_def.char_pr_id
            c_prop = ctx.catalog.char_props.get(c_ref)
            r_font = c_prop.font_size_pt if c_prop is not None else ctx.catalog.default_font_size_pt
            r_bold = c_prop.is_bold if c_prop is not None else False
            r_italic = c_prop.is_italic if c_prop is not None else False
            r_mono = c_prop.is_monospace if c_prop is not None else False

            for r_ch in iter_hwpx_children(ch):
                rtag = local_name(r_ch.tag)
                if rtag == "secPr":
                    _parse_sec_pr_into(r_ch, ctx.layout)
                elif rtag == "colPr":
                    _parse_col_pr_into(r_ch, ctx.layout)
                elif rtag == "ctrl":
                    _process_ctrl(r_ch)
                elif rtag == "t":
                    t_str = _extract_t_text(r_ch)
                    if t_str:
                        pieces.append(t_str)
                        n_ch = len(t_str.strip())
                        if n_ch > 0:
                            prose_chars += n_ch
                            run_fonts.append(r_font)
                            if r_bold:
                                bold_chars += n_ch
                            if r_italic:
                                italic_chars += n_ch
                            if r_mono:
                                mono_chars += n_ch
                elif rtag == "equation":
                    eq_txt = _extract_equation_text(r_ch)
                    if eq_txt:
                        if pieces and not pieces[-1].endswith((" ", "\n")):
                            pieces.append(" ")
                        pieces.append(eq_txt)
                        has_math = True
                        math_chars += len(eq_txt)
                elif rtag == "tbl":
                    tbl_it, side_vis = _extract_hwpx_table(r_ch, ctx)
                    if tbl_it is not None:
                        attachments.append(tbl_it)
                    attachments.extend(side_vis)
                elif rtag == "pic":
                    attachments.extend(_extract_pic_item(r_ch, ctx))
                elif rtag == "chart":
                    attachments.extend(_extract_chart_item(r_ch, ctx))
                elif rtag == "ole":
                    attachments.extend(_extract_ole_item(r_ch, ctx))
                elif rtag in _SHAPE_TAGS:
                    attachments.extend(_extract_shape_or_container_items(r_ch, ctx))
                elif rtag in _INLINE_CONTROL_TAGS:
                    ctrl_txt = _extract_inline_control_text(r_ch)
                    if ctrl_txt:
                        if (
                            pieces
                            and not pieces[-1].endswith((" ", "\n", "\t"))
                            and rtag in ("btn", "checkBtn", "radioBtn", "comboBox", "edit")
                        ):
                            pieces.append(" ")
                        pieces.append(ctrl_txt)
                        prose_chars += len(ctrl_txt)
                        run_fonts.append(r_font)

    _flush_active_hyperlink()
    raw_txt = "".join(pieces)
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
        total_ch = prose_chars + math_chars
        eff_font = (
            sorted(run_fonts)[len(run_fonts) // 2]
            if run_fonts
            else ctx.catalog.default_font_size_pt
        )
        is_b = (bold_chars / prose_chars) >= 0.65 if prose_chars > 0 else False
        is_i = (italic_chars / prose_chars) >= 0.65 if prose_chars > 0 else False
        is_code = bool(style_def is not None and style_def.is_code_style) or (
            prose_chars > 0 and (mono_chars / prose_chars) >= 0.75
        )
        is_formula_only = has_math and prose_chars == 0 and total_ch > 0

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
    return out_items, page_break_before


def _estimate_hwpx_item_height_pt(item: RawDocxBodyItem, usable_w: float) -> float:
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
    if isinstance(item, (RawPptxChart, RawPptxOleObject)):
        return max(80.0, item.rect_pt[3] - item.rect_pt[1]) + 12.0
    if isinstance(item, RawDocxVectorDiagram):
        return max(60.0, item.height_pt) + 12.0
    return 24.0


def _first_lineseg_pos(p_el: ET.Element) -> tuple[int, int] | None:
    """`<hp:linesegarray>`의 첫 `<hp:lineseg>`에서 `(vertpos, horzpos)` HWPUNIT 좌표를 읽는다."""
    for ch in iter_hwpx_children(p_el):
        if local_name(ch.tag) == "linesegarray":
            for seg in iter_hwpx_children(ch):
                if local_name(seg.tag) == "lineseg":
                    try:
                        v = int(_get_attr_local(seg, "vertpos", "-1"))
                        h = int(_get_attr_local(seg, "horzpos", "0"))
                        if v >= 0:
                            return (v, h)
                    except ValueError:
                        return None
    return None


def extract_hwpx_pages(pkg: HwpxPackage) -> list[RawDocxPage]:
    """`Contents/section*.xml` 전체를 순회하여 페이지 단위(`RawDocxPage`) 목록으로 분할·추출한다."""
    catalog = pkg.read_header_catalog()
    layout = HwpxSectionLayout()
    list_counters: dict[tuple[str, int], int] = {}
    seq_counter = [0]
    note_counter = [0]
    headers: list[str] = []
    footers: list[str] = []
    footnotes: dict[str, str] = {}
    endnotes: dict[str, str] = {}
    comments: dict[str, DocxCommentEntry] = {}

    ctx = _SectionParseContext(
        pkg=pkg,
        catalog=catalog,
        layout=layout,
        list_counters=list_counters,
        seq_counter=seq_counter,
        note_counter=note_counter,
        headers=headers,
        footers=footers,
        footnotes=footnotes,
        endnotes=endnotes,
        comments=comments,
    )

    pages: list[RawDocxPage] = []

    def _new_page() -> RawDocxPage:
        page = RawDocxPage(
            page_number=len(pages) + 1,
            width_pt=layout.width_pt,
            height_pt=layout.height_pt,
            margin_top_pt=layout.margin_top_pt,
            margin_bottom_pt=layout.margin_bottom_pt,
            margin_left_pt=layout.margin_left_pt,
            margin_right_pt=layout.margin_right_pt,
            column_count=layout.column_count,
            column_space_pt=layout.column_space_pt,
            headers=list(headers),
            footers=list(footers),
        )
        pages.append(page)
        return page

    def _sync_page_layout(page: RawDocxPage) -> None:
        page.width_pt = layout.width_pt
        page.height_pt = layout.height_pt
        page.margin_top_pt = layout.margin_top_pt
        page.margin_bottom_pt = layout.margin_bottom_pt
        page.margin_left_pt = layout.margin_left_pt
        page.margin_right_pt = layout.margin_right_pt
        page.column_count = max(page.column_count, layout.column_count)
        page.column_space_pt = layout.column_space_pt
        for h in headers:
            if h not in page.headers:
                page.headers.append(h)
        for f in footers:
            if f not in page.footers:
                page.footers.append(f)

    cur_page: RawDocxPage | None = None
    cur_y_pt = 0.0
    has_explicit_breaks = len(pkg.section_parts) > 1

    # 사전 스캔: pageBreak="1" 존재 여부 확인
    for sec_part in pkg.section_parts:
        sec_root = pkg.read_xml(sec_part)
        if sec_root is None:
            continue
        for p_el in iter_hwpx_children(sec_root):
            if local_name(p_el.tag) == "p" and _get_attr_local(
                p_el, "pageBreak", "0"
            ).lower() not in ("0", "false", ""):
                has_explicit_breaks = True
                break

    for sec_idx, sec_part in enumerate(pkg.section_parts):
        sec_root = pkg.read_xml(sec_part)
        if sec_root is None:
            continue

        if cur_page is None or (sec_idx > 0 and cur_page.items):
            cur_page = _new_page()
            cur_y_pt = 0.0

        prev_vert: int | None = None
        prev_horz: int | None = None

        for p_el in iter_hwpx_children(sec_root):
            if local_name(p_el.tag) != "p":
                continue

            seg_pos = _first_lineseg_pos(p_el)
            p_items, pb_before = _extract_paragraph_items(p_el, ctx)
            _sync_page_layout(cur_page)

            # linesegarray 기반 페이지 넘어감 감지(explicit pageBreak가 없을 때만 보조 사용)
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

            usable_w = max(120.0, layout.width_pt - layout.margin_left_pt - layout.margin_right_pt)
            usable_h = max(200.0, layout.height_pt - layout.margin_top_pt - layout.margin_bottom_pt)

            for it in p_items:
                est_h = _estimate_hwpx_item_height_pt(it, usable_w)
                if (
                    not has_explicit_breaks
                    and prev_vert is None
                    and cur_page.items
                    and (
                        cur_y_pt + est_h > usable_h * 1.12
                        or (
                            isinstance(it, RawDocxParagraph)
                            and it.heading_level == 1
                            and cur_y_pt > usable_h * 0.65
                        )
                    )
                ):
                    cur_page = _new_page()
                    cur_y_pt = 0.0
                cur_page.items.append(it)
                cur_y_pt += est_h

    if not pages:
        pages.append(_new_page())

    # 페이지별 참조된 각주·미주·숨은주석 연결
    emitted_fn: set[str] = set()
    emitted_en: set[str] = set()
    emitted_cm: set[str] = set()

    for page in pages:
        for it in page.items:
            if isinstance(it, (RawDocxParagraph, RawDocxTableItem)):
                for fid in it.footnote_ids:
                    if fid in footnotes and fid not in emitted_fn:
                        emitted_fn.add(fid)
                        page.footnotes.append((fid, footnotes[fid]))
                for eid in it.endnote_ids:
                    if eid in endnotes and eid not in emitted_en:
                        emitted_en.add(eid)
                        page.endnotes.append((eid, endnotes[eid]))
                for cid in it.comment_ids:
                    if cid in comments and cid not in emitted_cm:
                        emitted_cm.add(cid)
                        page.comments.append(comments[cid])

    last_page = pages[-1]
    for fid, ftxt in footnotes.items():
        if fid not in emitted_fn:
            last_page.footnotes.append((fid, ftxt))
    for eid, etxt in endnotes.items():
        if eid not in emitted_en:
            last_page.endnotes.append((eid, etxt))
    for cid, cm_entry in comments.items():
        if cid not in emitted_cm:
            last_page.comments.append(cm_entry)

    return pages


__all__ = [
    "HwpxSectionLayout",
    "extract_hwpx_pages",
    "normalize_hwp_equation_script",
]
