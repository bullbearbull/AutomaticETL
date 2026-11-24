"""DOCX(WordprocessingML) 본문·표·수식·차트·SmartArt·그림·각주/미주·주석·섹션 추출기.

`word/document.xml`과 연결된 파트(`styles.xml`, `numbering.xml`, `header*.xml`,
`footer*.xml`, `footnotes.xml`, `endnotes.xml`, `comments.xml`, `charts/*.xml`,
`diagrams/*.xml`, `media/*`, `embeddings/*`)를 순회해 페이지 단위(`RawDocxPage`)
원시 구조 데이터를 생성한다.
"""

from __future__ import annotations

import contextlib
import posixpath
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from autoetl.parser.adapters import ParserInputError
from autoetl.parser.contracts import ExtractedTable, ParseStrategy, TableCell
from autoetl.parser.docx.ooxml import (
    _MONOSPACE_FONTS,
    DEFAULT_MARGIN_PT,
    DEFAULT_PAGE_HEIGHT_PT,
    DEFAULT_PAGE_WIDTH_PT,
    NS_A,
    NS_ASVG,
    NS_C,
    NS_CX,
    NS_DGM,
    NS_M,
    NS_OFFICE,
    NS_R,
    NS_W,
    NS_WP,
    REL_TYPE_DOCX_COMMENTS,
    REL_TYPE_ENDNOTES,
    REL_TYPE_FOOTER,
    REL_TYPE_FOOTNOTES,
    REL_TYPE_HEADER,
    DocxCommentEntry,
    DocxPackage,
    NumberingCatalog,
    Relationship,
    StyleCatalog,
    clamp,
    emu_to_pt,
    half_pt_to_pt,
    iter_children_resolving_mc,
    local_name,
    make_bbox,
    probe_image_dimensions,
    qn,
    twips_to_pt,
)
from autoetl.parser.pptx.elements import (
    RawPptxChart,
    RawPptxImage,
    RawPptxOleObject,
    RawPptxSmartArt,
    _extract_chartex,
    _extract_embedded_xlsx_table,
    _extract_native_chart,
    _extract_smartart_diagram,
    _extract_svg_text_hints,
)
from autoetl.parser.pptx.ooxml import PptxPackage

_BULLET_CHARS = ("•", "▪", "▫", "■", "□", "●", "○", "◦", "◆", "◇", "▶", "▷", "·", "-", "*")


@dataclass(frozen=True)
class DocxSectionSpec:
    """Word 섹션(`<w:sectPr>`)의 페이지 크기·여백·단수·분할 유형·머리글/바닥글 참조."""

    width_pt: float = DEFAULT_PAGE_WIDTH_PT
    height_pt: float = DEFAULT_PAGE_HEIGHT_PT
    margin_top_pt: float = DEFAULT_MARGIN_PT
    margin_bottom_pt: float = DEFAULT_MARGIN_PT
    margin_left_pt: float = DEFAULT_MARGIN_PT
    margin_right_pt: float = DEFAULT_MARGIN_PT
    column_count: int = 1
    column_space_pt: float = 18.0
    break_type: str = "nextPage"  # "nextPage", "continuous", "evenPage", "oddPage"
    header_rids: tuple[str, ...] = ()
    footer_rids: tuple[str, ...] = ()


@dataclass(frozen=True)
class RawDocxVectorDiagram:
    """Word 그리기 캔버스/그룹 도형(`<wpc:wpc>` / `<wpg:wgp>`) 벡터 도식."""

    diagram_id: str
    name: str
    width_pt: float
    height_pt: float
    shape_count: int
    text_hints: tuple[str, ...] = ()


@dataclass(frozen=True)
class RawDocxParagraph:
    """단일 문단(또는 텍스트 상자 문단) 추출 결과."""

    text: str
    style_id: str | None = None
    style_name: str | None = None
    heading_level: int | None = None
    is_list_item: bool = False
    is_ordered_list: bool = False
    list_level: int = 0
    list_marker: str = ""
    font_size_pt: float | None = None
    is_bold: bool = False
    is_italic: bool = False
    is_code_or_formula: bool = False
    has_math: bool = False
    in_textbox: bool = False
    footnote_ids: tuple[str, ...] = ()
    endnote_ids: tuple[str, ...] = ()
    comment_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class RawDocxTableItem:
    """문서 흐름 내 표 객체."""

    table: ExtractedTable
    raw_width_pt: float
    raw_height_pt: float
    footnote_ids: tuple[str, ...] = ()
    endnote_ids: tuple[str, ...] = ()
    comment_ids: tuple[str, ...] = ()


RawDocxBodyItem = (
    RawDocxParagraph
    | RawDocxTableItem
    | RawPptxImage
    | RawPptxChart
    | RawPptxSmartArt
    | RawPptxOleObject
    | RawDocxVectorDiagram
)


@dataclass
class RawDocxPage:
    """페이지 분할(명시적 페이지/섹션 브레이크 또는 가상 페이지 분할)이 끝난 단일 DOCX 페이지."""

    page_number: int
    width_pt: float
    height_pt: float
    margin_top_pt: float
    margin_bottom_pt: float
    margin_left_pt: float
    margin_right_pt: float
    column_count: int
    column_space_pt: float
    items: list[RawDocxBodyItem] = field(default_factory=list)
    headers: list[str] = field(default_factory=list)
    footers: list[str] = field(default_factory=list)
    footnotes: list[tuple[str, str]] = field(default_factory=list)
    endnotes: list[tuple[str, str]] = field(default_factory=list)
    comments: list[DocxCommentEntry] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class _PptxBridgeAdapter:
    """`pptx/elements.py`의 차트·SmartArt 추출 함수를 `DocxPackage`에 연결하는 경량 어댑터."""

    def __init__(self, docx_pkg: DocxPackage) -> None:
        self._pkg = docx_pkg

    def read_xml(self, part_path: str) -> ET.Element | None:
        return self._pkg.read_xml(part_path)

    def read_bytes(self, part_path: str) -> bytes | None:
        return self._pkg.read_bytes(part_path)

    def read_rels(self, part_path: str) -> dict[str, Relationship]:
        return self._pkg.read_rels(part_path)

    def has_part(self, part_path: str) -> bool:
        return self._pkg.has_part(part_path)


def _format_omath_node(elem: ET.Element | None) -> str:
    """Office Math(`<m:oMath>`, `<m:oMathPara>`) 트리를 읽기 쉬운 수식 문자열로 변환한다."""
    if elem is None:
        return ""
    tag = local_name(elem.tag)

    if tag in ("oMathPara", "oMath", "e", "num", "den", "sup", "sub", "deg", "fName", "lim"):
        parts = [
            _format_omath_node(ch)
            for ch in elem
            if local_name(ch.tag) not in ("oMathParaPr", "ctrlPr", "argPr")
        ]
        return "".join(p for p in parts if p).strip()

    if tag == "r":
        texts = [t.text for t in elem if local_name(t.tag) == "t" and t.text]
        return "".join(texts)

    if tag == "t":
        return elem.text or ""

    if tag == "f":  # 분수 <m:f><m:num>...</m:num><m:den>...</m:den></m:f>
        num = _format_omath_node(elem.find(qn(NS_M, "num")))
        den = _format_omath_node(elem.find(qn(NS_M, "den")))
        num_s = f"({num})" if ("+" in num or "-" in num or " " in num) else num
        den_s = f"({den})" if ("+" in den or "-" in den or " " in den) else den
        if num_s and den_s:
            return f"{num_s}/{den_s}"
        return num_s or den_s

    if tag == "sSup":  # 위첨자
        base = _format_omath_node(elem.find(qn(NS_M, "e")))
        sup = _format_omath_node(elem.find(qn(NS_M, "sup")))
        if not sup:
            return base
        sup_s = sup if len(sup) == 1 else f"{{{sup}}}"
        return f"{base}^{sup_s}"

    if tag == "sSub":  # 아래첨자
        base = _format_omath_node(elem.find(qn(NS_M, "e")))
        sub = _format_omath_node(elem.find(qn(NS_M, "sub")))
        if not sub:
            return base
        sub_s = sub if len(sub) == 1 else f"{{{sub}}}"
        return f"{base}_{sub_s}"

    if tag == "sSubSup":  # 위·아래첨자 동시
        base = _format_omath_node(elem.find(qn(NS_M, "e")))
        sub = _format_omath_node(elem.find(qn(NS_M, "sub")))
        sup = _format_omath_node(elem.find(qn(NS_M, "sup")))
        sub_s = f"_{sub if len(sub) == 1 else f'{{{sub}}}'}" if sub else ""
        sup_s = f"^{sup if len(sup) == 1 else f'{{{sup}}}'}" if sup else ""
        return f"{base}{sub_s}{sup_s}"

    if tag == "rad":  # 근호 sqrt
        deg = _format_omath_node(elem.find(qn(NS_M, "deg")))
        body = _format_omath_node(elem.find(qn(NS_M, "e")))
        if deg:
            return f"sqrt[{deg}]({body})"
        return f"sqrt({body})"

    if tag == "nary":  # 시그마/적분 등
        sym = "∑"
        nary_pr = elem.find(qn(NS_M, "naryPr"))
        if nary_pr is not None:
            chr_el = nary_pr.find(qn(NS_M, "chr"))
            if chr_el is not None:
                c_val = chr_el.attrib.get(qn(NS_M, "val")) or chr_el.attrib.get("val")
                if c_val:
                    sym = c_val
        sub = _format_omath_node(elem.find(qn(NS_M, "sub")))
        sup = _format_omath_node(elem.find(qn(NS_M, "sup")))
        body = _format_omath_node(elem.find(qn(NS_M, "e")))
        sub_s = f"_{{{sub}}}" if sub else ""
        sup_s = f"^{{{sup}}}" if sup else ""
        return f"{sym}{sub_s}{sup_s} {body}".strip()

    if tag == "d":  # 괄호 구분자
        beg_chr = "("
        end_chr = ")"
        sep_chr = ", "
        dpr = elem.find(qn(NS_M, "dPr"))
        if dpr is not None:
            b_el = dpr.find(qn(NS_M, "begChr"))
            e_el = dpr.find(qn(NS_M, "endChr"))
            s_el = dpr.find(qn(NS_M, "sepChr"))
            if b_el is not None:
                beg_chr = b_el.attrib.get(qn(NS_M, "val")) or b_el.attrib.get("val") or ""
            if e_el is not None:
                end_chr = e_el.attrib.get(qn(NS_M, "val")) or e_el.attrib.get("val") or ""
            if s_el is not None:
                raw_s = s_el.attrib.get(qn(NS_M, "val")) or s_el.attrib.get("val")
                if raw_s:
                    sep_chr = f" {raw_s} "
        inner = sep_chr.join(_format_omath_node(ch) for ch in elem.findall(qn(NS_M, "e")))
        return f"{beg_chr}{inner}{end_chr}"

    if tag == "m":  # 행렬
        rows: list[str] = []
        for mr in elem.findall(qn(NS_M, "mr")):
            cols = [_format_omath_node(e) for e in mr.findall(qn(NS_M, "e"))]
            rows.append(", ".join(cols))
        return "[" + "; ".join(rows) + "]"

    if tag == "eqArr":  # 연립 수식 배열
        eqs = [_format_omath_node(e) for e in elem.findall(qn(NS_M, "e"))]
        return " ; ".join(e for e in eqs if e)

    if tag == "func":
        fn = _format_omath_node(elem.find(qn(NS_M, "fName")))
        arg = _format_omath_node(elem.find(qn(NS_M, "e")))
        return f"{fn}({arg})" if fn else arg

    # 기타 컨테이너는 자식 텍스트를 재귀 결합
    if tag.endswith("Pr"):
        return ""
    return "".join(_format_omath_node(ch) for ch in elem)


def _parse_section_pr(
    sect_pr: ET.Element,
    prev_spec: DocxSectionSpec | None = None,
) -> DocxSectionSpec:
    """`<w:sectPr>` 요소에서 페이지 크기·여백·단수·분할 유형·머리글/바닥글 참조를 추출한다."""
    width_pt = prev_spec.width_pt if prev_spec is not None else DEFAULT_PAGE_WIDTH_PT
    height_pt = prev_spec.height_pt if prev_spec is not None else DEFAULT_PAGE_HEIGHT_PT
    m_top = prev_spec.margin_top_pt if prev_spec is not None else DEFAULT_MARGIN_PT
    m_bot = prev_spec.margin_bottom_pt if prev_spec is not None else DEFAULT_MARGIN_PT
    m_left = prev_spec.margin_left_pt if prev_spec is not None else DEFAULT_MARGIN_PT
    m_right = prev_spec.margin_right_pt if prev_spec is not None else DEFAULT_MARGIN_PT

    pg_sz = sect_pr.find(qn(NS_W, "pgSz"))
    if pg_sz is not None:
        w_val = twips_to_pt(pg_sz.attrib.get(qn(NS_W, "w")) or pg_sz.attrib.get("w"))
        h_val = twips_to_pt(pg_sz.attrib.get(qn(NS_W, "h")) or pg_sz.attrib.get("h"))
        orient = (pg_sz.attrib.get(qn(NS_W, "orient")) or pg_sz.attrib.get("orient") or "").lower()
        if w_val > 36.0 and h_val > 36.0:
            if orient == "landscape" and w_val < h_val:
                width_pt, height_pt = round(h_val, 2), round(w_val, 2)
            else:
                width_pt, height_pt = round(w_val, 2), round(h_val, 2)

    pg_mar = sect_pr.find(qn(NS_W, "pgMar"))
    if pg_mar is not None:
        for attr, target in (
            ("top", "top"),
            ("bottom", "bot"),
            ("left", "left"),
            ("right", "right"),
        ):
            raw_m = pg_mar.attrib.get(qn(NS_W, attr)) or pg_mar.attrib.get(attr)
            if raw_m is not None:
                val_pt = max(0.0, twips_to_pt(raw_m, DEFAULT_MARGIN_PT))
                if target == "top":
                    m_top = round(val_pt, 2)
                elif target == "bot":
                    m_bot = round(val_pt, 2)
                elif target == "left":
                    m_left = round(val_pt, 2)
                else:
                    m_right = round(val_pt, 2)

    col_count = 1
    col_space = 18.0
    cols_el = sect_pr.find(qn(NS_W, "cols"))
    if cols_el is not None:
        raw_num = cols_el.attrib.get(qn(NS_W, "num")) or cols_el.attrib.get("num")
        if raw_num is not None:
            with contextlib.suppress(ValueError):
                col_count = max(1, min(6, int(raw_num)))
        raw_sp = cols_el.attrib.get(qn(NS_W, "space")) or cols_el.attrib.get("space")
        if raw_sp is not None:
            sp_pt = twips_to_pt(raw_sp, 18.0)
            if sp_pt > 0.0:
                col_space = round(sp_pt, 2)

    type_el = sect_pr.find(qn(NS_W, "type"))
    break_type = (
        (type_el.attrib.get(qn(NS_W, "val")) or type_el.attrib.get("val") or "nextPage").strip()
        if type_el is not None
        else "nextPage"
    )

    hdr_rids = [
        (el.attrib.get(qn(NS_R, "id")) or el.attrib.get("id") or "").strip()
        for el in sect_pr.findall(qn(NS_W, "headerReference"))
    ]
    ftr_rids = [
        (el.attrib.get(qn(NS_R, "id")) or el.attrib.get("id") or "").strip()
        for el in sect_pr.findall(qn(NS_W, "footerReference"))
    ]
    hdr_tuple = tuple(r for r in hdr_rids if r) or (
        prev_spec.header_rids if prev_spec is not None else ()
    )
    ftr_tuple = tuple(r for r in ftr_rids if r) or (
        prev_spec.footer_rids if prev_spec is not None else ()
    )

    return DocxSectionSpec(
        width_pt=width_pt,
        height_pt=height_pt,
        margin_top_pt=m_top,
        margin_bottom_pt=m_bot,
        margin_left_pt=m_left,
        margin_right_pt=m_right,
        column_count=col_count,
        column_space_pt=col_space,
        break_type=break_type,
        header_rids=hdr_tuple,
        footer_rids=ftr_tuple,
    )


def _extract_plain_text_from_container(root: ET.Element | None) -> str:
    """각주·미주·주석·머리글/바닥글 XML 조각에서 삭제 텍스트를 제외한 순수 텍스트를 추출한다."""
    if root is None:
        return ""
    para_lines: list[str] = []

    def _collect_runs(node: ET.Element, pieces: list[str]) -> None:
        for ch in iter_children_resolving_mc(node):
            ctag = local_name(ch.tag)
            if ctag in ("del", "moveFrom", "pPr", "rPr", "instrText", "delInstrText", "proofErr"):
                continue
            if ctag == "t" and ch.text:
                pieces.append(ch.text)
            elif ctag == "tab":
                pieces.append("\t")
            elif ctag == "br":
                pieces.append("\n")
            elif ctag in ("oMath", "oMathPara"):
                m_txt = _format_omath_node(ch)
                if m_txt:
                    pieces.append(m_txt)
            else:
                _collect_runs(ch, pieces)

    for p_el in root.iter(qn(NS_W, "p")):
        pieces: list[str] = []
        _collect_runs(p_el, pieces)
        line = "".join(pieces).strip()
        if line:
            para_lines.append(line)

    return "\n".join(para_lines).strip()


_GANADA_CHARS = ("가", "나", "다", "라", "마", "바", "사", "아", "자", "차", "카", "타", "파", "하")
_CHOSUNG_CHARS = (
    "ㄱ",
    "ㄴ",
    "ㄷ",
    "ㄹ",
    "ㅁ",
    "ㅂ",
    "ㅅ",
    "ㅇ",
    "ㅈ",
    "ㅊ",
    "ㅋ",
    "ㅌ",
    "ㅍ",
    "ㅎ",
)
_ROMAN_PAIRS: tuple[tuple[int, str], ...] = (
    (1000, "m"),
    (900, "cm"),
    (500, "d"),
    (400, "cd"),
    (100, "c"),
    (90, "xc"),
    (50, "l"),
    (40, "xl"),
    (10, "x"),
    (9, "ix"),
    (5, "v"),
    (4, "iv"),
    (1, "i"),
)


def _int_to_roman(num: int) -> str:
    if num <= 0:
        return str(num)
    n = min(num, 3999)
    out: list[str] = []
    for val, sym in _ROMAN_PAIRS:
        while n >= val:
            out.append(sym)
            n -= val
    return "".join(out)


def _format_ordered_list_marker(num: int, num_fmt: str) -> str:
    """Word `w:numFmt` 값에 맞춰 번호 목록 마커(`1.`, `a.`, `i.`, `가.`)를 생성한다."""
    fmt = num_fmt.strip().lower()
    idx = max(1, num) - 1
    if fmt == "lowerletter":
        return f"{chr(ord('a') + (idx % 26))}."
    if fmt == "upperletter":
        return f"{chr(ord('A') + (idx % 26))}."
    if fmt == "lowerroman":
        return f"{_int_to_roman(max(1, num))}."
    if fmt == "upperroman":
        return f"{_int_to_roman(max(1, num)).upper()}."
    if fmt in ("ganada", "koreanlegal", "koreandigital"):
        return f"{_GANADA_CHARS[idx % len(_GANADA_CHARS)]}."
    if fmt == "chosung":
        return f"{_CHOSUNG_CHARS[idx % len(_CHOSUNG_CHARS)]}."
    return f"{num}."


def _extract_ffdata_checkbox(fld_char_el: ET.Element) -> str | None:
    """레거시 양식 필드 `<w:fldChar><w:ffData><w:checkBox>`에서 체크 상태(`☑`/`☐`)를 추출한다."""
    for sub in fld_char_el.iter():
        if local_name(sub.tag) == "checkBox":
            is_checked: bool | None = None
            default_checked = False
            for ch in sub:
                ctag = local_name(ch.tag)
                raw_val = (
                    (ch.attrib.get(qn(NS_W, "val")) or ch.attrib.get("val") or "1").strip().lower()
                )
                if ctag == "checked":
                    is_checked = raw_val not in ("0", "false", "off")
                elif ctag == "default":
                    default_checked = raw_val not in ("0", "false", "off")
            checked = is_checked if is_checked is not None else default_checked
            return "☑" if checked else "☐"
    return None


def _extract_sdt_checkbox(sdt_el: ET.Element) -> str | None:
    """Word 2010+ 콘텐츠 컨트롤 `<w:sdt><w:sdtPr><w14:checkbox>`에서 체크 상태를 추출한다."""
    sdt_pr = sdt_el.find(qn(NS_W, "sdtPr"))
    if sdt_pr is None:
        return None
    for sub in sdt_pr.iter():
        if local_name(sub.tag) == "checkbox":
            for ch in sub:
                if local_name(ch.tag) == "checked":
                    for attr_val in ch.attrib.values():
                        if attr_val.strip().lower() not in ("0", "false", "off"):
                            return "☑"
                    return "☐"
            return "☐"
    return None


def _load_notes_map(
    pkg: DocxPackage, rel_type: str, fallback_part: str, tag_name: str
) -> dict[str, str]:
    """`word/footnotes.xml` 또는 `word/endnotes.xml`에서 `id -> text` 매핑을 로드한다."""
    doc_rels = pkg.read_rels(pkg.main_document_part)
    part = next(
        (r.target for r in doc_rels.values() if not r.is_external and r.rel_type == rel_type),
        fallback_part,
    )
    root = pkg.read_xml(part)
    if root is None:
        return {}
    notes: dict[str, str] = {}
    for el in root.findall(qn(NS_W, tag_name)):
        nid = (el.attrib.get(qn(NS_W, "id")) or el.attrib.get("id") or "").strip()
        ntype = (el.attrib.get(qn(NS_W, "type")) or el.attrib.get("type") or "").strip().lower()
        if not nid or nid in ("0", "-1") or "separator" in ntype:
            continue
        txt = _extract_plain_text_from_container(el)
        if txt:
            notes[nid] = txt
    return notes


def _load_comments_map(pkg: DocxPackage) -> dict[str, DocxCommentEntry]:
    """`word/comments.xml`에서 `id -> DocxCommentEntry` 매핑을 로드한다."""
    doc_rels = pkg.read_rels(pkg.main_document_part)
    part = next(
        (
            r.target
            for r in doc_rels.values()
            if not r.is_external and r.rel_type == REL_TYPE_DOCX_COMMENTS
        ),
        "word/comments.xml",
    )
    root = pkg.read_xml(part)
    if root is None:
        return {}
    comments: dict[str, DocxCommentEntry] = {}
    for cm in root.findall(qn(NS_W, "comment")):
        cid = (cm.attrib.get(qn(NS_W, "id")) or cm.attrib.get("id") or "").strip()
        if not cid:
            continue
        author = (
            cm.attrib.get(qn(NS_W, "author")) or cm.attrib.get("author") or "Reviewer"
        ).strip()
        initials = (cm.attrib.get(qn(NS_W, "initials")) or cm.attrib.get("initials") or "").strip()
        date = (cm.attrib.get(qn(NS_W, "date")) or cm.attrib.get("date") or "").strip()
        txt = _extract_plain_text_from_container(cm)
        if txt:
            comments[cid] = DocxCommentEntry(
                comment_id=cid,
                author=author,
                initials=initials,
                date=date,
                text=txt,
            )
    return comments


def _resolve_blip_embed_id(blip: ET.Element) -> str:
    embed_id = blip.attrib.get(qn(NS_R, "embed"), "")
    if embed_id:
        return embed_id
    for sub in blip.iter():
        if sub.tag == qn(NS_ASVG, "svgBlip") or local_name(sub.tag) == "svgBlip":
            svg_id = sub.attrib.get(qn(NS_R, "embed"), "")
            if svg_id:
                return svg_id
    return ""


def _extract_drawing_items(
    drawing_el: ET.Element,
    *,
    pkg: DocxPackage,
    part_rels: dict[str, Relationship],
    styles: StyleCatalog,
    numbering: NumberingCatalog,
    footnotes_map: dict[str, str],
    endnotes_map: dict[str, str],
    seq_counter: list[int],
    page_w: float,
    page_h: float,
) -> list[RawDocxBodyItem]:
    """`<w:drawing>`(`wp:inline`/`wp:anchor`)에서 그림·차트·SmartArt·텍스트상자·도식을 추출한다."""
    items: list[RawDocxBodyItem] = []
    bridge: PptxPackage = _PptxBridgeAdapter(pkg)  # type: ignore[assignment]

    for container in iter_children_resolving_mc(drawing_el):
        ctag = local_name(container.tag)
        if ctag not in ("inline", "anchor"):
            continue

        doc_pr = container.find(qn(NS_WP, "docPr"))
        if doc_pr is not None and doc_pr.attrib.get("hidden") in ("1", "true", "True"):
            continue
        shape_id = doc_pr.attrib.get("id", "") if doc_pr is not None else ""
        name = (doc_pr.attrib.get("name", "") if doc_pr is not None else "").strip()
        alt_text = (
            (doc_pr.attrib.get("descr") or doc_pr.attrib.get("title") or "").strip()
            if doc_pr is not None
            else ""
        )

        extent = container.find(qn(NS_WP, "extent"))
        w_pt = emu_to_pt(extent.attrib.get("cx"), 220.0) if extent is not None else 220.0
        h_pt = emu_to_pt(extent.attrib.get("cy"), 140.0) if extent is not None else 140.0
        w_pt = clamp(w_pt if w_pt > 4.0 else 220.0, 12.0, page_w)
        h_pt = clamp(h_pt if h_pt > 4.0 else 140.0, 12.0, page_h)
        rect_pt = (0.0, 0.0, round(w_pt, 2), round(h_pt, 2))

        graphic = container.find(qn(NS_A, "graphic"))
        g_data = graphic.find(qn(NS_A, "graphicData")) if graphic is not None else None
        if g_data is None:
            continue

        # 1) 네이티브 차트(<c:chart>) 및 Office 2016+ 확장 차트(<cx:chart>)
        chart_ref = g_data.find(qn(NS_C, "chart"))
        if chart_ref is not None:
            r_id = chart_ref.attrib.get(qn(NS_R, "id"), "")
            rel = part_rels.get(r_id)
            if rel is not None and not rel.is_external:
                seq_counter[0] += 1
                chart = _extract_native_chart(
                    bridge,
                    rel.target,
                    shape_id=shape_id or str(seq_counter[0]),
                    shape_name=name or f"Chart {seq_counter[0]}",
                    rect_pt=rect_pt,
                )
                if chart is not None:
                    items.append(chart)
            continue

        chartex_ref = g_data.find(qn(NS_CX, "chart"))
        if chartex_ref is not None:
            r_id = chartex_ref.attrib.get(qn(NS_R, "id"), "")
            rel = part_rels.get(r_id)
            if rel is not None and not rel.is_external:
                seq_counter[0] += 1
                chartex = _extract_chartex(
                    bridge,
                    rel.target,
                    shape_id=shape_id or str(seq_counter[0]),
                    shape_name=name or f"ChartEx {seq_counter[0]}",
                    rect_pt=rect_pt,
                )
                if chartex is not None:
                    items.append(chartex)
            continue

        # 2) SmartArt 다이어그램(<dgm:relIds>)
        dgm_ref = g_data.find(qn(NS_DGM, "relIds"))
        if dgm_ref is not None:
            dm_id = dgm_ref.attrib.get(qn(NS_R, "dm"), "")
            rel = part_rels.get(dm_id)
            if rel is not None and not rel.is_external:
                seq_counter[0] += 1
                smartart = _extract_smartart_diagram(
                    bridge,
                    rel.target,
                    shape_id=shape_id or str(seq_counter[0]),
                    shape_name=name or f"SmartArt {seq_counter[0]}",
                    rect_pt=rect_pt,
                )
                if smartart is not None:
                    items.append(smartart)
            continue

        # 3) 삽입 그림(pic:pic / a:blip / asvg:svgBlip)
        blip_el: ET.Element | None = None
        for sub in g_data.iter(qn(NS_A, "blip")):
            blip_el = sub
            break
        if blip_el is not None:
            embed_id = _resolve_blip_embed_id(blip_el)
            rel = part_rels.get(embed_id)
            if rel is not None and not rel.is_external:
                raw_bytes = pkg.read_bytes(rel.target)
                ext = posixpath.splitext(rel.target)[1].lower() or ".bin"
                px_w, px_h = (
                    probe_image_dimensions(raw_bytes, ext)
                    if raw_bytes is not None
                    else (None, None)
                )
                svg_hints = (
                    _extract_svg_text_hints(raw_bytes)
                    if (ext == ".svg" and raw_bytes is not None)
                    else ()
                )
                seq_counter[0] += 1
                items.append(
                    RawPptxImage(
                        shape_id=shape_id or str(seq_counter[0]),
                        name=name or posixpath.basename(rel.target),
                        alt_text=alt_text,
                        media_part=rel.target,
                        extension=ext,
                        rect_pt=rect_pt,
                        raw_bytes=raw_bytes,
                        pixel_width=px_w,
                        pixel_height=px_h,
                        svg_text_hints=svg_hints,
                    )
                )

        # 4) WordprocessingShape 텍스트 상자(<w:txbxContent>) 및 캔버스/그룹 도식
        txbx_contents = list(g_data.iter(qn(NS_W, "txbxContent")))
        shape_nodes = [el for el in g_data.iter() if local_name(el.tag) in ("wsp", "cxnSp")]
        txbx_texts: list[str] = []
        for txbx in txbx_contents:
            for sub_p in txbx.findall(qn(NS_W, "p")):
                sub_items, _, _ = _extract_items_from_paragraph(
                    sub_p,
                    pkg=pkg,
                    part_rels=part_rels,
                    styles=styles,
                    numbering=numbering,
                    footnotes_map=footnotes_map,
                    endnotes_map=endnotes_map,
                    list_counters={},
                    seq_counter=seq_counter,
                    page_w=page_w,
                    page_h=page_h,
                    in_textbox=True,
                )
                for sit in sub_items:
                    if isinstance(sit, RawDocxParagraph) and sit.text:
                        txbx_texts.append(sit.text)
                    items.append(sit)

        has_group_canvas = any(local_name(el.tag) in ("wpc", "wgp") for el in g_data.iter())
        if (has_group_canvas and len(shape_nodes) >= 2) or len(shape_nodes) >= 3:
            seq_counter[0] += 1
            hints = tuple(dict.fromkeys(t.splitlines()[0][:60] for t in txbx_texts if t))[:20]
            items.append(
                RawDocxVectorDiagram(
                    diagram_id=f"d_{seq_counter[0]}",
                    name=name or f"Diagram {seq_counter[0]}",
                    width_pt=w_pt,
                    height_pt=h_pt,
                    shape_count=len(shape_nodes),
                    text_hints=hints,
                )
            )

    return items


def _extract_pict_or_object_items(
    elem: ET.Element,
    *,
    pkg: DocxPackage,
    part_rels: dict[str, Relationship],
    styles: StyleCatalog,
    numbering: NumberingCatalog,
    footnotes_map: dict[str, str],
    endnotes_map: dict[str, str],
    seq_counter: list[int],
    page_w: float,
    page_h: float,
) -> list[RawDocxBodyItem]:
    """레거시 `<w:pict>` / `<w:object>` 안의 VML 이미지, 텍스트 상자, OLE를 추출한다."""
    items: list[RawDocxBodyItem] = []
    rect_pt = (0.0, 0.0, min(280.0, page_w * 0.6), min(180.0, page_h * 0.25))

    # 1) VML <v:imagedata r:id="..."/> 미리보기 또는 그림
    vml_img: RawPptxImage | None = None
    for sub in elem.iter():
        if local_name(sub.tag) == "imagedata":
            rid = (
                sub.attrib.get(qn(NS_R, "id"))
                or sub.attrib.get(qn(NS_OFFICE, "relid"))
                or sub.attrib.get("id")
                or ""
            ).strip()
            rel = part_rels.get(rid)
            if rel is not None and not rel.is_external:
                raw_bytes = pkg.read_bytes(rel.target)
                ext = posixpath.splitext(rel.target)[1].lower() or ".bin"
                px_w, px_h = (
                    probe_image_dimensions(raw_bytes, ext)
                    if raw_bytes is not None
                    else (None, None)
                )
                seq_counter[0] += 1
                vml_img = RawPptxImage(
                    shape_id=str(seq_counter[0]),
                    name=posixpath.basename(rel.target),
                    alt_text=(sub.attrib.get(qn(NS_OFFICE, "title")) or "").strip(),
                    media_part=rel.target,
                    extension=ext,
                    rect_pt=rect_pt,
                    raw_bytes=raw_bytes,
                    pixel_width=px_w,
                    pixel_height=px_h,
                )
                break

    # 2) OLE 객체(<o:OLEObject r:id="..." ProgID="..."/>)
    ole_el: ET.Element | None = None
    for sub in elem.iter():
        if local_name(sub.tag) in ("OLEObject", "oleObj"):
            ole_el = sub
            break

    if ole_el is not None:
        prog_id = (ole_el.attrib.get("ProgID") or ole_el.attrib.get("progId") or "").strip()
        ole_rid = (ole_el.attrib.get(qn(NS_R, "id")) or ole_el.attrib.get("id") or "").strip()
        ole_name = prog_id or "OLE Object"
        if ole_rid:
            rel = part_rels.get(ole_rid)
            if rel is not None and not rel.is_external:
                ole_bytes = pkg.read_bytes(rel.target)
                if ole_bytes is not None:
                    seq_counter[0] += 1
                    xlsx_tbl = _extract_embedded_xlsx_table(
                        ole_bytes,
                        slide_number=1,
                        table_index=seq_counter[0],
                        rect_pt=rect_pt,
                        slide_w=page_w,
                        slide_h=page_h,
                        ole_name=ole_name,
                    )
                    if xlsx_tbl is not None:
                        items.append(
                            RawDocxTableItem(
                                table=xlsx_tbl,
                                raw_width_pt=rect_pt[2],
                                raw_height_pt=rect_pt[3],
                            )
                        )
                        return items

        seq_counter[0] += 1
        items.append(
            RawPptxOleObject(
                shape_id=str(seq_counter[0]),
                name=ole_name,
                prog_id=prog_id,
                rect_pt=rect_pt,
                preview_image=vml_img,
            )
        )
        return items

    if vml_img is not None:
        items.append(vml_img)

    # 3) VML 텍스트 상자(<v:textbox><w:txbxContent>)
    for txbx in elem.iter(qn(NS_W, "txbxContent")):
        for sub_p in txbx.findall(qn(NS_W, "p")):
            sub_items, _, _ = _extract_items_from_paragraph(
                sub_p,
                pkg=pkg,
                part_rels=part_rels,
                styles=styles,
                numbering=numbering,
                footnotes_map=footnotes_map,
                endnotes_map=endnotes_map,
                list_counters={},
                seq_counter=seq_counter,
                page_w=page_w,
                page_h=page_h,
                in_textbox=True,
            )
            items.extend(sub_items)

    return items


def _extract_items_from_paragraph(
    p_el: ET.Element,
    *,
    pkg: DocxPackage,
    part_rels: dict[str, Relationship],
    styles: StyleCatalog,
    numbering: NumberingCatalog,
    footnotes_map: dict[str, str],
    endnotes_map: dict[str, str],
    list_counters: dict[tuple[str, int], int],
    seq_counter: list[int],
    page_w: float,
    page_h: float,
    in_textbox: bool = False,
) -> tuple[list[RawDocxBodyItem], bool, DocxSectionSpec | None]:
    """`<w:p>` 문단에서 추출 항목 목록, `page_break_before`, 문단 끝 `<w:sectPr>`을 반환한다.

    문단 중간에 `<w:br w:type="page"/>` 또는 `<w:lastRenderedPageBreak/>`가 있으면
    리스트 안에 `"__PAGE_BREAK__"` 센티넬 대신 분할된 segment들을 순서대로 생성한다.
    """
    segments, pb_before, sect_spec = _extract_segmented_paragraph(
        p_el,
        pkg=pkg,
        part_rels=part_rels,
        styles=styles,
        numbering=numbering,
        footnotes_map=footnotes_map,
        endnotes_map=endnotes_map,
        list_counters=list_counters,
        seq_counter=seq_counter,
        page_w=page_w,
        page_h=page_h,
        in_textbox=in_textbox,
    )
    flat: list[RawDocxBodyItem] = [it for seg in segments for it in seg]
    return flat, pb_before, sect_spec


def _extract_segmented_paragraph(
    p_el: ET.Element,
    *,
    pkg: DocxPackage,
    part_rels: dict[str, Relationship],
    styles: StyleCatalog,
    numbering: NumberingCatalog,
    footnotes_map: dict[str, str],
    endnotes_map: dict[str, str],
    list_counters: dict[tuple[str, int], int],
    seq_counter: list[int],
    page_w: float,
    page_h: float,
    in_textbox: bool = False,
    prev_section_spec: DocxSectionSpec | None = None,
) -> tuple[list[list[RawDocxBodyItem]], bool, DocxSectionSpec | None]:
    """문단을 페이지 브레이크 기준 세그먼트 목록(`list[list[RawDocxBodyItem]]`)으로 추출한다."""
    ppr = p_el.find(qn(NS_W, "pPr"))
    style_id: str | None = None
    explicit_heading_lvl: int | None = None
    explicit_num_id: str | None = None
    explicit_ilvl: int | None = None
    page_break_before = False
    p_font_size: float | None = None
    p_bold: bool | None = None
    p_italic: bool | None = None
    p_font_family: str | None = None
    sect_spec: DocxSectionSpec | None = None

    if ppr is not None:
        pstyle_el = ppr.find(qn(NS_W, "pStyle"))
        if pstyle_el is not None:
            style_id = (
                pstyle_el.attrib.get(qn(NS_W, "val")) or pstyle_el.attrib.get("val") or ""
            ).strip() or None
        out_el = ppr.find(qn(NS_W, "outlineLvl"))
        if out_el is not None:
            raw_out = out_el.attrib.get(qn(NS_W, "val")) or out_el.attrib.get("val")
            if raw_out is not None:
                with contextlib.suppress(ValueError):
                    v = int(raw_out)
                    if 0 <= v <= 8:
                        explicit_heading_lvl = min(6, v + 1)
        num_pr = ppr.find(qn(NS_W, "numPr"))
        if num_pr is not None:
            nid_el = num_pr.find(qn(NS_W, "numId"))
            ilvl_el = num_pr.find(qn(NS_W, "ilvl"))
            if nid_el is not None:
                explicit_num_id = (
                    nid_el.attrib.get(qn(NS_W, "val")) or nid_el.attrib.get("val") or ""
                ).strip() or None
            if ilvl_el is not None:
                raw_l = ilvl_el.attrib.get(qn(NS_W, "val")) or ilvl_el.attrib.get("val")
                if raw_l is not None:
                    with contextlib.suppress(ValueError):
                        explicit_ilvl = max(0, int(raw_l))
        if ppr.find(qn(NS_W, "pageBreakBefore")) is not None:
            pb_el = ppr.find(qn(NS_W, "pageBreakBefore"))
            pb_val = (
                (pb_el.attrib.get(qn(NS_W, "val")) or pb_el.attrib.get("val") or "1").lower()
                if pb_el is not None
                else "1"
            )
            if pb_val not in ("0", "false", "off"):
                page_break_before = True

        p_rpr = ppr.find(qn(NS_W, "rPr"))
        if p_rpr is not None:
            if p_rpr.find(qn(NS_W, "vanish")) is not None:
                return [], False, None
            sz_el = p_rpr.find(qn(NS_W, "sz"))
            p_font_size = (
                half_pt_to_pt(sz_el.attrib.get(qn(NS_W, "val")) or sz_el.attrib.get("val"))
                if sz_el is not None
                else None
            )
            if p_font_size is None and p_rpr.find(qn(NS_W, "szCs")) is not None:
                sz_cs = p_rpr.find(qn(NS_W, "szCs"))
                p_font_size = half_pt_to_pt(
                    sz_cs.attrib.get(qn(NS_W, "val")) or sz_cs.attrib.get("val")
                    if sz_cs is not None
                    else None
                )
            b_el = p_rpr.find(qn(NS_W, "b"))
            if b_el is not None:
                b_val = (b_el.attrib.get(qn(NS_W, "val")) or b_el.attrib.get("val") or "1").lower()
                p_bold = b_val not in ("0", "false", "off")
            i_el = p_rpr.find(qn(NS_W, "i"))
            if i_el is not None:
                i_val = (i_el.attrib.get(qn(NS_W, "val")) or i_el.attrib.get("val") or "1").lower()
                p_italic = i_val not in ("0", "false", "off")
            rf_el = p_rpr.find(qn(NS_W, "rFonts"))
            if rf_el is not None:
                p_font_family = (
                    rf_el.attrib.get(qn(NS_W, "ascii"))
                    or rf_el.attrib.get("ascii")
                    or rf_el.attrib.get(qn(NS_W, "hAnsi"))
                    or rf_el.attrib.get("hAnsi")
                )

        sect_el = ppr.find(qn(NS_W, "sectPr"))
        if sect_el is not None:
            sect_spec = _parse_section_pr(sect_el, prev_section_spec)

    resolved_style = styles.resolve(style_id)
    heading_lvl = explicit_heading_lvl or (
        resolved_style.heading_level if resolved_style is not None else None
    )

    eff_num_id = explicit_num_id
    eff_ilvl = explicit_ilvl if explicit_ilvl is not None else 0
    if eff_num_id is None and resolved_style is not None and resolved_style.num_id:
        eff_num_id = resolved_style.num_id
        if explicit_ilvl is None:
            eff_ilvl = resolved_style.ilvl

    is_list_item = False
    is_ordered_list = False
    list_marker = ""
    if eff_num_id and eff_num_id != "0" and heading_lvl is None:
        is_list_item = True
        lvl_fmt = numbering.resolve(eff_num_id, eff_ilvl)
        if lvl_fmt is not None and lvl_fmt.is_ordered:
            is_ordered_list = True
            key = (eff_num_id, eff_ilvl)
            cur = list_counters.get(key, lvl_fmt.start - 1) + 1
            list_counters[key] = cur
            # 하위 레벨 카운터 리셋
            for k_nid, k_lvl in list(list_counters):
                if k_nid == eff_num_id and k_lvl > eff_ilvl:
                    del list_counters[(k_nid, k_lvl)]
            list_marker = _format_ordered_list_marker(cur, lvl_fmt.num_fmt)
    elif (
        eff_num_id != "0"
        and resolved_style is not None
        and resolved_style.is_list_style
        and heading_lvl is None
    ):
        is_list_item = True

    # 문단 내부 런·수식·그리기·각주·페이지 브레이크 순회
    segments: list[list[RawDocxBodyItem]] = []
    cur_pieces: list[str] = []
    cur_attachments: list[RawDocxBodyItem] = []
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
    field_depth = 0
    in_field_instr = False

    def _flush_segment(*, is_first_seg: bool) -> None:
        nonlocal cur_pieces, cur_attachments
        raw_txt = "".join(cur_pieces)
        cleaned_lines = [ln.strip() for ln in raw_txt.splitlines()]
        txt = "\n".join(ln for ln in cleaned_lines if ln).strip()
        seg_items: list[RawDocxBodyItem] = []

        if txt:
            total_ch = prose_chars + math_chars
            eff_font: float | None = None
            if run_fonts:
                sorted_f = sorted(run_fonts)
                eff_font = sorted_f[len(sorted_f) // 2]
            elif p_font_size is not None:
                eff_font = p_font_size
            elif resolved_style is not None and resolved_style.font_size_pt is not None:
                eff_font = resolved_style.font_size_pt
            else:
                eff_font = styles.default_font_size_pt

            if prose_chars > 0:
                is_b = (bold_chars / prose_chars) >= 0.65
                is_i = (italic_chars / prose_chars) >= 0.65
            else:
                is_b = bool(
                    p_bold
                    if p_bold is not None
                    else (resolved_style.is_bold if resolved_style is not None else False)
                )
                is_i = bool(
                    p_italic
                    if p_italic is not None
                    else (resolved_style.is_italic if resolved_style is not None else False)
                )

            is_code = bool(resolved_style is not None and resolved_style.is_code_style)
            if not is_code and p_font_family and p_font_family.lower() in _MONOSPACE_FONTS:
                is_code = True
            if not is_code and prose_chars > 0 and (mono_chars / prose_chars) >= 0.75:
                is_code = True

            is_formula_only = has_math and prose_chars == 0 and total_ch > 0
            seg_is_list = is_list_item and is_first_seg
            if (
                not seg_is_list
                and heading_lvl is None
                and txt.startswith(_BULLET_CHARS)
                and len(txt) >= 3
                and txt[1:2] in (" ", "\t")
            ):
                seg_is_list = True

            seg_items.append(
                RawDocxParagraph(
                    text=txt,
                    style_id=style_id,
                    style_name=resolved_style.name if resolved_style is not None else style_id,
                    heading_level=heading_lvl if is_first_seg else None,
                    is_list_item=seg_is_list,
                    is_ordered_list=is_ordered_list if is_first_seg else False,
                    list_level=eff_ilvl if seg_is_list else 0,
                    list_marker=list_marker if is_first_seg else "",
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

        seg_items.extend(cur_attachments)
        cur_pieces = []
        cur_attachments = []
        if seg_items:
            segments.append(seg_items)

    def _trigger_page_break() -> None:
        nonlocal page_break_before
        has_content = bool("".join(cur_pieces).strip() or cur_attachments)
        if not has_content and not segments:
            page_break_before = True
            return
        _flush_segment(is_first_seg=(len(segments) == 0))
        if not segments:
            page_break_before = True

    def _walk_run(r_el: ET.Element) -> None:
        nonlocal bold_chars, italic_chars, mono_chars, prose_chars, field_depth, in_field_instr
        rpr = r_el.find(qn(NS_W, "rPr"))
        if rpr is not None and rpr.find(qn(NS_W, "vanish")) is not None:
            return

        r_font: float | None = None
        r_bold = p_bold
        r_italic = p_italic
        r_mono = bool(p_font_family and p_font_family.lower() in _MONOSPACE_FONTS)
        if rpr is not None:
            for sz_tag in ("sz", "szCs"):
                sz_el = rpr.find(qn(NS_W, sz_tag))
                if sz_el is not None:
                    r_font = half_pt_to_pt(
                        sz_el.attrib.get(qn(NS_W, "val")) or sz_el.attrib.get("val")
                    )
                    if r_font is not None:
                        break
            b_el = rpr.find(qn(NS_W, "b"))
            if b_el is not None:
                bv = (b_el.attrib.get(qn(NS_W, "val")) or b_el.attrib.get("val") or "1").lower()
                r_bold = bv not in ("0", "false", "off")
            i_el = rpr.find(qn(NS_W, "i"))
            if i_el is not None:
                iv = (i_el.attrib.get(qn(NS_W, "val")) or i_el.attrib.get("val") or "1").lower()
                r_italic = iv not in ("0", "false", "off")
            rf_el = rpr.find(qn(NS_W, "rFonts"))
            if rf_el is not None:
                ff = (
                    rf_el.attrib.get(qn(NS_W, "ascii"))
                    or rf_el.attrib.get("ascii")
                    or rf_el.attrib.get(qn(NS_W, "hAnsi"))
                    or rf_el.attrib.get("hAnsi")
                    or ""
                ).strip()
                if ff.lower() in _MONOSPACE_FONTS:
                    r_mono = True

        for ch in iter_children_resolving_mc(r_el):
            ctag = local_name(ch.tag)
            if ctag == "rPr":
                continue
            if ctag == "fldChar":
                ftype = (
                    ch.attrib.get(qn(NS_W, "fldCharType")) or ch.attrib.get("fldCharType") or ""
                ).lower()
                if ftype == "begin":
                    field_depth += 1
                    in_field_instr = True
                    cb_sym = _extract_ffdata_checkbox(ch)
                    if cb_sym is not None:
                        cur_pieces.append(cb_sym)
                        prose_chars += 1
                elif ftype == "separate":
                    in_field_instr = False
                elif ftype == "end":
                    field_depth = max(0, field_depth - 1)
                    if field_depth == 0:
                        in_field_instr = False
                continue
            if ctag in ("instrText", "delInstrText") or in_field_instr:
                continue
            if ctag == "lastRenderedPageBreak":
                _trigger_page_break()
                continue
            if ctag == "br":
                btype = (ch.attrib.get(qn(NS_W, "type")) or ch.attrib.get("type") or "").lower()
                if btype == "page":
                    _trigger_page_break()
                else:
                    cur_pieces.append("\n")
                continue
            if ctag in ("tab", "ptab"):
                cur_pieces.append("\t")
                continue
            if ctag in ("cr",):
                cur_pieces.append("\n")
                continue
            if ctag in ("noBreakHyphen",):
                cur_pieces.append("-")
                continue
            if ctag == "t" and ch.text:
                cur_pieces.append(ch.text)
                n_ch = len(ch.text.strip())
                if n_ch > 0:
                    prose_chars += n_ch
                    if r_font is not None:
                        run_fonts.append(r_font)
                    if r_bold:
                        bold_chars += n_ch
                    if r_italic:
                        italic_chars += n_ch
                    if r_mono:
                        mono_chars += n_ch
                continue
            if ctag == "footnoteReference":
                fid = (ch.attrib.get(qn(NS_W, "id")) or ch.attrib.get("id") or "").strip()
                if fid and fid in footnotes_map:
                    cur_pieces.append(f"[^{fid}]")
                    fn_ids.append(fid)
                continue
            if ctag == "endnoteReference":
                eid = (ch.attrib.get(qn(NS_W, "id")) or ch.attrib.get("id") or "").strip()
                if eid and eid in endnotes_map:
                    cur_pieces.append(f"[^e{eid}]")
                    en_ids.append(eid)
                continue
            if ctag == "commentReference":
                cid = (ch.attrib.get(qn(NS_W, "id")) or ch.attrib.get("id") or "").strip()
                if cid:
                    cm_ids.append(cid)
                continue
            if ctag == "drawing":
                cur_attachments.extend(
                    _extract_drawing_items(
                        ch,
                        pkg=pkg,
                        part_rels=part_rels,
                        styles=styles,
                        numbering=numbering,
                        footnotes_map=footnotes_map,
                        endnotes_map=endnotes_map,
                        seq_counter=seq_counter,
                        page_w=page_w,
                        page_h=page_h,
                    )
                )
                continue
            if ctag in ("pict", "object"):
                cur_attachments.extend(
                    _extract_pict_or_object_items(
                        ch,
                        pkg=pkg,
                        part_rels=part_rels,
                        styles=styles,
                        numbering=numbering,
                        footnotes_map=footnotes_map,
                        endnotes_map=endnotes_map,
                        seq_counter=seq_counter,
                        page_w=page_w,
                        page_h=page_h,
                    )
                )

    def _walk_p_children(parent: ET.Element) -> None:
        nonlocal has_math, math_chars, prose_chars
        for ch in iter_children_resolving_mc(parent):
            ctag = local_name(ch.tag)
            if ctag in ("pPr", "del", "moveFrom", "proofErr", "bookmarkStart", "bookmarkEnd"):
                continue
            if ctag == "commentRangeStart":
                cid = (ch.attrib.get(qn(NS_W, "id")) or ch.attrib.get("id") or "").strip()
                if cid:
                    cm_ids.append(cid)
                continue
            if ctag == "lastRenderedPageBreak":
                _trigger_page_break()
                continue
            if ctag == "r":
                _walk_run(ch)
                continue
            if ctag in ("oMathPara", "oMath"):
                m_txt = _format_omath_node(ch)
                if m_txt:
                    if cur_pieces and not cur_pieces[-1].endswith((" ", "\n")):
                        cur_pieces.append(" ")
                    cur_pieces.append(m_txt)
                    has_math = True
                    math_chars += len(m_txt.strip())
                continue
            if ctag == "sdt":
                sdt_cb = _extract_sdt_checkbox(ch)
                before_len = len(cur_pieces)
                sdt_content = ch.find(qn(NS_W, "sdtContent"))
                if sdt_content is not None:
                    _walk_p_children(sdt_content)
                if sdt_cb is not None:
                    added_txt = "".join(cur_pieces[before_len:])
                    if "☐" not in added_txt and "☑" not in added_txt:
                        cur_pieces.insert(before_len, sdt_cb)
                        prose_chars += 1
                continue
            if ctag in (
                "ins",
                "moveTo",
                "hyperlink",
                "smartTag",
                "customXml",
                "fldSimple",
                "dir",
                "bdo",
            ):
                _walk_p_children(ch)
                continue
            if ctag == "drawing":
                cur_attachments.extend(
                    _extract_drawing_items(
                        ch,
                        pkg=pkg,
                        part_rels=part_rels,
                        styles=styles,
                        numbering=numbering,
                        footnotes_map=footnotes_map,
                        endnotes_map=endnotes_map,
                        seq_counter=seq_counter,
                        page_w=page_w,
                        page_h=page_h,
                    )
                )
                continue
            if ctag in ("pict", "object"):
                cur_attachments.extend(
                    _extract_pict_or_object_items(
                        ch,
                        pkg=pkg,
                        part_rels=part_rels,
                        styles=styles,
                        numbering=numbering,
                        footnotes_map=footnotes_map,
                        endnotes_map=endnotes_map,
                        seq_counter=seq_counter,
                        page_w=page_w,
                        page_h=page_h,
                    )
                )

    _walk_p_children(p_el)
    _flush_segment(is_first_seg=(len(segments) == 0))
    return segments, page_break_before, sect_spec


def _iter_unwrapped_children(parent: ET.Element, target_tags: set[str]) -> list[ET.Element]:
    """`<w:sdt>`, `<w:ins>`, `<w:moveTo>`, `<w:customXml>` 래퍼를 풀며 대상 태그 목록을 수집한다."""
    out: list[ET.Element] = []
    for ch in iter_children_resolving_mc(parent):
        ctag = local_name(ch.tag)
        if ctag in ("del", "moveFrom"):
            continue
        if ctag in target_tags:
            out.append(ch)
        elif ctag == "sdt":
            sdt_c = ch.find(qn(NS_W, "sdtContent"))
            if sdt_c is not None:
                out.extend(_iter_unwrapped_children(sdt_c, target_tags))
        elif ctag in ("ins", "moveTo", "customXml"):
            out.extend(_iter_unwrapped_children(ch, target_tags))
    return out


def _detect_docx_table_strategy(tbl_el: ET.Element, tr_list: list[ET.Element]) -> ParseStrategy:
    """`<w:tblBorders>` 및 `<w:tcBorders>` 테두리 구성을 분석해 표 복원 전략을 결정한다."""
    active_h = 0
    active_v = 0
    explicit_none = 0

    def _inspect_borders(borders_el: ET.Element | None) -> None:
        nonlocal active_h, active_v, explicit_none
        if borders_el is None:
            return
        for b_tag, is_vert in (
            ("top", False),
            ("bottom", False),
            ("insideH", False),
            ("left", True),
            ("right", True),
            ("insideV", True),
        ):
            b_el = borders_el.find(qn(NS_W, b_tag))
            if b_el is None:
                continue
            val = (b_el.attrib.get(qn(NS_W, "val")) or b_el.attrib.get("val") or "").strip().lower()
            if val in ("none", "nil", ""):
                explicit_none += 1
            else:
                if is_vert:
                    active_v += 1
                else:
                    active_h += 1

    tbl_pr = tbl_el.find(qn(NS_W, "tblPr"))
    has_tbl_style = False
    if tbl_pr is not None:
        if tbl_pr.find(qn(NS_W, "tblStyle")) is not None:
            has_tbl_style = True
        _inspect_borders(tbl_pr.find(qn(NS_W, "tblBorders")))

    for tr in tr_list[:6]:
        for tc in _iter_unwrapped_children(tr, {"tc"})[:10]:
            tc_pr = tc.find(qn(NS_W, "tcPr"))
            if tc_pr is not None:
                _inspect_borders(tc_pr.find(qn(NS_W, "tcBorders")))

    if active_v > 0:
        return ParseStrategy.RULED_TABLE
    if active_h > 0 and active_v == 0:
        return ParseStrategy.HORIZONTAL_RULED_TABLE
    if explicit_none >= 4 and not has_tbl_style:
        return ParseStrategy.BORDERLESS_TABLE
    return ParseStrategy.RULED_TABLE


def _extract_docx_table(
    tbl_el: ET.Element,
    *,
    pkg: DocxPackage,
    part_rels: dict[str, Relationship],
    styles: StyleCatalog,
    numbering: NumberingCatalog,
    footnotes_map: dict[str, str],
    endnotes_map: dict[str, str],
    seq_counter: list[int],
    page_w: float,
    page_h: float,
) -> tuple[RawDocxTableItem | None, list[RawDocxBodyItem]]:
    """WordprocessingML `<w:tbl>`에서 가로·세로 병합 셀과 테두리 전략을 보존해 표를 추출한다."""
    tr_list = _iter_unwrapped_children(tbl_el, {"tr"})
    if not tr_list:
        return None, []

    tbl_grid = tbl_el.find(qn(NS_W, "tblGrid"))
    col_widths = [
        max(12.0, twips_to_pt(gc.attrib.get(qn(NS_W, "w")) or gc.attrib.get("w"), 72.0))
        for gc in (tbl_grid.findall(qn(NS_W, "gridCol")) if tbl_grid is not None else [])
    ]

    # 각 행의 셀과 gridSpan/gridBefore를 먼저 스캔하여 전체 열 수 산출
    parsed_rows: list[tuple[bool, int, list[tuple[ET.Element, int, str | None, str | None]]]] = []
    max_cols = len(col_widths)

    for tr in tr_list:
        tr_pr = tr.find(qn(NS_W, "trPr"))
        is_hdr_tr = False
        grid_before = 0
        if tr_pr is not None:
            if tr_pr.find(qn(NS_W, "tblHeader")) is not None:
                is_hdr_tr = True
            gb_el = tr_pr.find(qn(NS_W, "gridBefore"))
            if gb_el is not None:
                raw_gb = gb_el.attrib.get(qn(NS_W, "val")) or gb_el.attrib.get("val")
                if raw_gb is not None:
                    with contextlib.suppress(ValueError):
                        grid_before = max(0, int(raw_gb))

        tcs = _iter_unwrapped_children(tr, {"tc"})
        row_tcs: list[tuple[ET.Element, int, str | None, str | None]] = []
        col_cursor = grid_before
        for tc in tcs:
            tc_pr = tc.find(qn(NS_W, "tcPr"))
            span = 1
            v_merge: str | None = None
            h_merge: str | None = None
            if tc_pr is not None:
                gs_el = tc_pr.find(qn(NS_W, "gridSpan"))
                if gs_el is not None:
                    raw_gs = gs_el.attrib.get(qn(NS_W, "val")) or gs_el.attrib.get("val")
                    if raw_gs is not None:
                        with contextlib.suppress(ValueError):
                            span = max(1, int(raw_gs))
                vm_el = tc_pr.find(qn(NS_W, "vMerge"))
                if vm_el is not None:
                    vm_val = (
                        (vm_el.attrib.get(qn(NS_W, "val")) or vm_el.attrib.get("val") or "continue")
                        .strip()
                        .lower()
                    )
                    v_merge = "restart" if vm_val == "restart" else "continue"
                hm_el = tc_pr.find(qn(NS_W, "hMerge"))
                if hm_el is not None:
                    hm_val = (
                        (hm_el.attrib.get(qn(NS_W, "val")) or hm_el.attrib.get("val") or "continue")
                        .strip()
                        .lower()
                    )
                    h_merge = "restart" if hm_val == "restart" else "continue"
            row_tcs.append((tc, span, v_merge, h_merge))
            col_cursor += span
        max_cols = max(max_cols, col_cursor)
        parsed_rows.append((is_hdr_tr, grid_before, row_tcs))

    n_rows = len(parsed_rows)
    n_cols = max_cols
    if n_rows == 0 or n_cols == 0:
        return None, []

    if len(col_widths) < n_cols:
        avg_w = (sum(col_widths) / len(col_widths)) if col_widths else 80.0
        col_widths.extend([avg_w] * (n_cols - len(col_widths)))

    @dataclass
    class _DraftCell:
        row: int
        col: int
        row_span: int
        col_span: int
        text: str

    grid_text: list[list[str]] = [["" for _ in range(n_cols)] for _ in range(n_rows)]
    grid_line_lists: list[list[list[str]]] = [[[] for _ in range(n_cols)] for _ in range(n_rows)]
    row_has_merges: list[bool] = [False] * n_rows
    raw_cells: list[_DraftCell] = []
    has_merged = False
    merged_header_cols: set[int] = set()
    # col -> raw_cells index of active vMerge anchor
    active_vmerge_anchor: dict[int, int] = {}
    active_hmerge_anchor: int | None = None

    fn_ids: list[str] = []
    en_ids: list[str] = []
    cm_ids: list[str] = []
    side_visuals: list[RawDocxBodyItem] = []

    for r_idx, (_, grid_before, row_tcs) in enumerate(parsed_rows):
        c_idx = grid_before
        active_hmerge_anchor = None
        if grid_before > 0:
            row_has_merges[r_idx] = True
        for tc, col_span, v_merge, h_merge in row_tcs:
            if c_idx >= n_cols:
                break
            col_span = min(col_span, n_cols - c_idx)

            # 레거시 hMerge="continue" 처리
            if h_merge == "continue" and active_hmerge_anchor is not None:
                has_merged = True
                row_has_merges[r_idx] = True
                raw_cells[active_hmerge_anchor].col_span += col_span
                if r_idx == 0:
                    merged_header_cols.update(range(c_idx, c_idx + col_span))
                c_idx += col_span
                continue

            # vMerge="continue" 처리: 위쪽 앵커 셀의 row_span을 1 증가시키고 현재 셀은 건너뜀
            if v_merge == "continue" and c_idx in active_vmerge_anchor:
                has_merged = True
                row_has_merges[r_idx] = True
                anchor_idx = active_vmerge_anchor[c_idx]
                prev_rspan = raw_cells[anchor_idx].row_span
                anchor_row = raw_cells[anchor_idx].row
                if r_idx + 1 - anchor_row > prev_rspan:
                    raw_cells[anchor_idx].row_span = r_idx + 1 - anchor_row
                c_idx += col_span
                continue

            if col_span > 1 or v_merge is not None or h_merge is not None:
                row_has_merges[r_idx] = True
            if col_span > 1:
                has_merged = True
                if r_idx == 0:
                    merged_header_cols.update(range(c_idx + 1, c_idx + col_span))

            # 셀 내부 문단·중첩 표 추출(텍스트 상자는 문단 추출기가 1회만 수집)
            cell_lines: list[str] = []
            raw_aligned_lines: list[str] = []
            cell_counters: dict[tuple[str, int], int] = {}
            for ch in _iter_unwrapped_children(tc, {"p", "tbl"}):
                ctag = local_name(ch.tag)
                if ctag == "p":
                    p_items, _, _ = _extract_items_from_paragraph(
                        ch,
                        pkg=pkg,
                        part_rels=part_rels,
                        styles=styles,
                        numbering=numbering,
                        footnotes_map=footnotes_map,
                        endnotes_map=endnotes_map,
                        list_counters=cell_counters,
                        seq_counter=seq_counter,
                        page_w=page_w,
                        page_h=page_h,
                    )
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
                        else:
                            side_visuals.append(pit)
                    if not had_p_text:
                        raw_aligned_lines.append("")
                elif ctag == "tbl":
                    nested_item, nested_vis = _extract_docx_table(
                        ch,
                        pkg=pkg,
                        part_rels=part_rels,
                        styles=styles,
                        numbering=numbering,
                        footnotes_map=footnotes_map,
                        endnotes_map=endnotes_map,
                        seq_counter=seq_counter,
                        page_w=page_w,
                        page_h=page_h,
                    )
                    side_visuals.extend(nested_vis)
                    if nested_item is not None:
                        nested_rows = [
                            " | ".join(r)
                            for r in ([nested_item.table.headers, *nested_item.table.rows])
                            if any(r)
                        ]
                        if nested_rows:
                            joined_nested = " / ".join(nested_rows)
                            cell_lines.append(joined_nested)
                            raw_aligned_lines.append(joined_nested)

            while raw_aligned_lines and not raw_aligned_lines[-1]:
                raw_aligned_lines.pop()

            cell_text = " / ".join(ln for ln in cell_lines if ln.strip()).strip()
            grid_text[r_idx][c_idx] = cell_text
            grid_line_lists[r_idx][c_idx] = raw_aligned_lines

            cell_idx = len(raw_cells)
            raw_cells.append(
                _DraftCell(
                    row=r_idx,
                    col=c_idx,
                    row_span=1,
                    col_span=col_span,
                    text=cell_text,
                )
            )
            if h_merge == "restart":
                active_hmerge_anchor = cell_idx

            if v_merge == "restart":
                for sc in range(c_idx, c_idx + col_span):
                    active_vmerge_anchor[sc] = cell_idx
            else:
                for sc in range(c_idx, c_idx + col_span):
                    active_vmerge_anchor.pop(sc, None)

            c_idx += col_span

    if not any(c.text.strip() for c in raw_cells):
        return None, side_visuals

    # 병합 없는 표에서 단일 <w:tr>의 비어 있지 않은 열들이 동일 줄 수(K>=3)를 담고 있으면 행 분할
    if not has_merged and not any(row_has_merges):
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
                )
                for r_i in range(n_rows)
                for c_i in range(n_cols)
            ]

    explicit_hdr_rows = 0
    for is_hdr_tr, _, _ in parsed_rows:
        if is_hdr_tr:
            explicit_hdr_rows += 1
        else:
            break
    header_row_count = 1 if (explicit_hdr_rows >= 1 or n_rows >= 2) else 0

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
    if any(c.row_span > 1 or c.col_span > 1 for c in cells):
        has_merged = True

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

    # 양식 표 하단의 100% 빈 여백 행 제거 (최소 2행 유지)
    while n_rows > 2 and not any(c.strip() for c in grid_text[-1]):
        grid_text.pop()
        n_rows -= 1
    if cells:
        trimmed_cells: list[TableCell] = []
        for cell in cells:
            if cell.row >= n_rows:
                continue
            clamped_rspan = min(cell.row_span, n_rows - cell.row)
            trimmed_cells.append(
                cell.model_copy(update={"row_span": clamped_rspan})
                if clamped_rspan != cell.row_span
                else cell
            )
        cells = trimmed_cells

    if header_row_count == 1 and n_rows >= 2:
        has_any_hdr_text = any(grid_text[0][c].strip() for c in range(n_cols))
        headers = [
            grid_text[0][c]
            if (has_any_hdr_text or grid_text[0][c] or c in merged_header_cols)
            else f"col_{c + 1}"
            for c in range(n_cols)
        ]
        data_rows = grid_text[1:]
    else:
        header_row_count = 0
        headers = [f"col_{c + 1}" for c in range(n_cols)]
        data_rows = grid_text

    strategy = _detect_docx_table_strategy(tbl_el, tr_list)
    strat_desc = {
        ParseStrategy.RULED_TABLE: "격자선 표",
        ParseStrategy.HORIZONTAL_RULED_TABLE: "가로선(booktabs) 표",
        ParseStrategy.BORDERLESS_TABLE: "무선 표",
    }.get(strategy, "OOXML 표")
    merged_note = ", 병합 셀 포함" if has_merged else ""

    raw_w = min(page_w * 0.88, max(120.0, sum(col_widths[:n_cols])))
    raw_h = min(page_h * 0.78, max(36.0, n_rows * 22.0))
    seq_counter[0] += 1
    extracted = ExtractedTable(
        table_id=f"p001_t{seq_counter[0]:02d}",
        page=1,
        bbox=make_bbox(0.0, 0.0, raw_w, raw_h, page_w, page_h),
        strategy=strategy,
        confidence=0.98,
        row_count=n_rows,
        col_count=n_cols,
        header_row_count=header_row_count,
        headers=headers,
        rows=data_rows,
        cells=cells,
        has_merged_cells=has_merged,
        caption=None,
        reason=f"DOCX 네이티브 OOXML {strat_desc} 직접 추출({n_rows}x{n_cols}{merged_note})",
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


def _estimate_item_height_pt(item: RawDocxBodyItem, usable_w: float) -> float:
    """단일 요소의 가상 세로 높이(pt)를 추정한다."""
    if isinstance(item, RawDocxParagraph):
        font = item.font_size_pt or 11.0
        chars_per_line = max(20.0, usable_w / max(6.0, font * 0.55))
        lines = sum(
            max(1, int(len(ln) / chars_per_line) + 1)
            for ln in (item.text.splitlines() or [item.text])
        )
        spacing = 10.0 if item.heading_level is not None else 5.0
        return lines * (font * 1.38) + spacing
    if isinstance(item, RawDocxTableItem):
        return item.raw_height_pt + 14.0
    if isinstance(item, RawPptxImage):
        return max(36.0, item.rect_pt[3] - item.rect_pt[1]) + 10.0
    if isinstance(item, (RawPptxChart, RawPptxSmartArt, RawPptxOleObject)):
        return max(80.0, item.rect_pt[3] - item.rect_pt[1]) + 12.0
    if isinstance(item, RawDocxVectorDiagram):
        return max(60.0, item.height_pt) + 12.0
    return 24.0


def _resolve_header_footer_texts(
    pkg: DocxPackage,
    doc_rels: dict[str, Relationship],
    rids: tuple[str, ...],
    cache: dict[str, str],
) -> list[str]:
    texts: list[str] = []
    seen: set[str] = set()
    for rid in rids:
        rel = doc_rels.get(rid)
        if rel is None or rel.is_external or rel.rel_type not in (REL_TYPE_HEADER, REL_TYPE_FOOTER):
            continue
        if rel.target not in cache:
            cache[rel.target] = _extract_plain_text_from_container(pkg.read_xml(rel.target))
        txt = cache[rel.target]
        if txt and txt not in seen:
            seen.add(txt)
            texts.append(txt)
    return texts


def extract_docx_pages(pkg: DocxPackage) -> list[RawDocxPage]:
    """메인 문서 XML 전체를 순회하여 페이지 단위(`RawDocxPage`) 목록으로 분할·추출한다."""
    main_part = pkg.main_document_part
    doc_root = pkg.read_xml(main_part)
    if doc_root is None:
        raise ParserInputError(
            f"DOCX 본문 XML({main_part})이 손상되어 파싱할 수 없다: {pkg.source_path.name}"
        )

    body = doc_root.find(qn(NS_W, "body"))
    if body is None:
        raise ParserInputError(f"DOCX 본문(<w:body>) 요소가 없다: {pkg.source_path.name}")

    doc_rels = pkg.read_rels(main_part)
    styles = pkg.read_styles()
    numbering = pkg.read_numbering()
    footnotes_map = _load_notes_map(pkg, REL_TYPE_FOOTNOTES, "word/footnotes.xml", "footnote")
    endnotes_map = _load_notes_map(pkg, REL_TYPE_ENDNOTES, "word/endnotes.xml", "endnote")
    comments_map = _load_comments_map(pkg)

    # 1. 섹션 정의(<w:sectPr>)를 먼저 스캔해 각 본문 요소에 적용될 섹션 규격을 매핑
    body_Sect_el = body.find(qn(NS_W, "sectPr"))
    default_sect = (
        _parse_section_pr(body_Sect_el) if body_Sect_el is not None else DocxSectionSpec()
    )

    # (item_list, page_break_before, section_break_after_spec)
    staged_units: list[tuple[list[RawDocxBodyItem], bool, DocxSectionSpec | None]] = []
    list_counters: dict[tuple[str, int], int] = {}
    seq_counter = [0]
    has_explicit_page_breaks = False
    prev_sect_spec: DocxSectionSpec | None = None

    def _walk_body_container(container: ET.Element) -> None:
        nonlocal has_explicit_page_breaks, prev_sect_spec
        for elem in iter_children_resolving_mc(container):
            tag = local_name(elem.tag)
            if tag in ("del", "moveFrom", "sectPr", "bookmarkStart", "bookmarkEnd"):
                continue
            if tag == "sdt":
                sdt_c = elem.find(qn(NS_W, "sdtContent"))
                if sdt_c is not None:
                    _walk_body_container(sdt_c)
                continue
            if tag in ("ins", "moveTo", "customXml"):
                _walk_body_container(elem)
                continue
            if tag == "p":
                cur_w = prev_sect_spec.width_pt if prev_sect_spec else default_sect.width_pt
                cur_h = prev_sect_spec.height_pt if prev_sect_spec else default_sect.height_pt
                segments, pb_before, p_sect = _extract_segmented_paragraph(
                    elem,
                    pkg=pkg,
                    part_rels=doc_rels,
                    styles=styles,
                    numbering=numbering,
                    footnotes_map=footnotes_map,
                    endnotes_map=endnotes_map,
                    list_counters=list_counters,
                    seq_counter=seq_counter,
                    page_w=cur_w,
                    page_h=cur_h,
                    prev_section_spec=prev_sect_spec,
                )
                if pb_before or len(segments) > 1:
                    has_explicit_page_breaks = True
                if p_sect is not None:
                    prev_sect_spec = p_sect
                    if p_sect.break_type != "continuous":
                        has_explicit_page_breaks = True

                if not segments:
                    if pb_before or p_sect is not None:
                        staged_units.append(([], pb_before, p_sect))
                    continue

                for seg_idx, seg_items in enumerate(segments):
                    seg_pb = pb_before if seg_idx == 0 else True
                    seg_sect = p_sect if seg_idx == len(segments) - 1 else None
                    staged_units.append((seg_items, seg_pb, seg_sect))
                continue

            if tag == "tbl":
                cur_w = prev_sect_spec.width_pt if prev_sect_spec else default_sect.width_pt
                cur_h = prev_sect_spec.height_pt if prev_sect_spec else default_sect.height_pt
                tbl_item, side_vis = _extract_docx_table(
                    elem,
                    pkg=pkg,
                    part_rels=doc_rels,
                    styles=styles,
                    numbering=numbering,
                    footnotes_map=footnotes_map,
                    endnotes_map=endnotes_map,
                    seq_counter=seq_counter,
                    page_w=cur_w,
                    page_h=cur_h,
                )
                unit_items: list[RawDocxBodyItem] = []
                if tbl_item is not None:
                    unit_items.append(tbl_item)
                unit_items.extend(side_vis)
                if unit_items:
                    staged_units.append((unit_items, False, None))

    _walk_body_container(body)

    # 각 unit이 속한 섹션 규격 역방향/정방향 연결
    final_sect = (
        _parse_section_pr(body_Sect_el, prev_sect_spec)
        if body_Sect_el is not None
        else (prev_sect_spec or default_sect)
    )
    unit_sections: list[DocxSectionSpec] = [final_sect] * len(staged_units)
    active_sect = final_sect
    for idx in range(len(staged_units) - 1, -1, -1):
        _, _, s_after = staged_units[idx]
        if s_after is not None:
            active_sect = s_after
        unit_sections[idx] = active_sect

    # 2. 페이지 분할 조립
    hf_cache: dict[str, str] = {}
    pages: list[RawDocxPage] = []

    def _new_page(spec: DocxSectionSpec) -> RawDocxPage:
        p_num = len(pages) + 1
        hdrs = _resolve_header_footer_texts(pkg, doc_rels, spec.header_rids, hf_cache)
        ftrs = _resolve_header_footer_texts(pkg, doc_rels, spec.footer_rids, hf_cache)
        page = RawDocxPage(
            page_number=p_num,
            width_pt=spec.width_pt,
            height_pt=spec.height_pt,
            margin_top_pt=spec.margin_top_pt,
            margin_bottom_pt=spec.margin_bottom_pt,
            margin_left_pt=spec.margin_left_pt,
            margin_right_pt=spec.margin_right_pt,
            column_count=spec.column_count,
            column_space_pt=spec.column_space_pt,
            headers=hdrs,
            footers=ftrs,
        )
        pages.append(page)
        return page

    cur_page = _new_page(unit_sections[0] if unit_sections else final_sect)
    cur_y_pt = 0.0

    for idx, (unit_items, pb_before, sect_after) in enumerate(staged_units):
        spec = unit_sections[idx]
        usable_w = max(120.0, spec.width_pt - spec.margin_left_pt - spec.margin_right_pt)
        usable_h = max(200.0, spec.height_pt - spec.margin_top_pt - spec.margin_bottom_pt)

        if pb_before and cur_page.items:
            cur_page = _new_page(spec)
            cur_y_pt = 0.0

        for it in unit_items:
            est_h = _estimate_item_height_pt(it, usable_w)
            # 명시적 페이지 브레이크가 전혀 없는 긴 문서는 가상 페이지 예산 초과 시 자연 분할
            if (
                not has_explicit_page_breaks
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
                cur_page = _new_page(spec)
                cur_y_pt = 0.0

            cur_page.items.append(it)
            cur_y_pt += est_h

        if sect_after is not None:
            next_spec = unit_sections[idx + 1] if (idx + 1) < len(unit_sections) else final_sect
            if next_spec.break_type == "continuous":
                if next_spec.column_count > cur_page.column_count:
                    cur_page.column_count = next_spec.column_count
                    cur_page.column_space_pt = next_spec.column_space_pt
            elif (idx + 1) < len(staged_units):
                cur_page = _new_page(next_spec)
                cur_y_pt = 0.0

    # 각 페이지에서 참조된 각주·미주·주석 연결(미참조 미주/주석은 마지막 페이지에 귀속)
    emitted_fn: set[str] = set()
    emitted_en: set[str] = set()
    emitted_cm: set[str] = set()

    for page in pages:
        for it in page.items:
            if isinstance(it, (RawDocxParagraph, RawDocxTableItem)):
                for fid in it.footnote_ids:
                    if fid in footnotes_map and fid not in emitted_fn:
                        emitted_fn.add(fid)
                        page.footnotes.append((fid, footnotes_map[fid]))
                for eid in it.endnote_ids:
                    if eid in endnotes_map and eid not in emitted_en:
                        emitted_en.add(eid)
                        page.endnotes.append((eid, endnotes_map[eid]))
                for cid in it.comment_ids:
                    if cid in comments_map and cid not in emitted_cm:
                        emitted_cm.add(cid)
                        page.comments.append(comments_map[cid])

    if pages:
        last_page = pages[-1]
        for fid, ftxt in footnotes_map.items():
            if fid not in emitted_fn:
                last_page.footnotes.append((fid, ftxt))
        for eid, etxt in endnotes_map.items():
            if eid not in emitted_en:
                last_page.endnotes.append((eid, etxt))
        for cid, centry in comments_map.items():
            if cid not in emitted_cm:
                last_page.comments.append(centry)

    return pages


__all__ = [
    "DocxSectionSpec",
    "RawDocxBodyItem",
    "RawDocxPage",
    "RawDocxParagraph",
    "RawDocxTableItem",
    "RawDocxVectorDiagram",
    "extract_docx_pages",
]
