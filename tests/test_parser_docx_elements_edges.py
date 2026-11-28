"""DOCX 본문 추출기(`autoetl.parser.docx.elements`) 오류·빈 입력·경계 값 엣지 테스트.

수식·섹션·목록·체크박스·그리기·VML/OLE·표 병합·페이지 분할의 드문 경로를
코드 안에서 작게 만든 OOXML 입력으로 검증한다. 외부 프로그램·모델은 쓰지 않는다.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any

import pytest

from autoetl.parser.adapters import ParserInputError
from autoetl.parser.contracts import ExtractedTable, ParseStrategy
from autoetl.parser.docx.elements import (
    DocxSectionSpec,
    RawDocxParagraph,
    RawDocxTableItem,
    RawDocxVectorDiagram,
    _detect_docx_table_strategy,
    _estimate_item_height_pt,
    _extract_ffdata_checkbox,
    _extract_plain_text_from_container,
    _extract_sdt_checkbox,
    _format_omath_node,
    _format_ordered_list_marker,
    _int_to_roman,
    _iter_unwrapped_children,
    _parse_section_pr,
    _resolve_blip_embed_id,
    extract_docx_pages,
)
from autoetl.parser.docx.ooxml import NS_W, DocxPackage, make_bbox, qn
from autoetl.parser.image_io import encode_png
from autoetl.parser.pptx.elements import (
    RawPptxChart,
    RawPptxImage,
    RawPptxOleObject,
    RawPptxSmartArt,
)

_NS = (
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
    'xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math" '
    'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture" '
    'xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" '
    'xmlns:cx="http://schemas.microsoft.com/office/drawing/2014/chartex" '
    'xmlns:dgm="http://schemas.openxmlformats.org/drawingml/2006/diagram" '
    'xmlns:v="urn:schemas-microsoft-com:vml" '
    'xmlns:o="urn:schemas-microsoft-com:office:office" '
    'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape" '
    'xmlns:wpg="http://schemas.microsoft.com/office/word/2010/wordprocessingGroup" '
    'xmlns:wpc="http://schemas.microsoft.com/office/word/2010/wordprocessingCanvas" '
    'xmlns:asvg="http://schemas.microsoft.com/office/drawing/2016/SVG/main" '
    'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml" '
    'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"'
)

_ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="word/document.xml"/></Relationships>'
)


def _el(xml: str) -> ET.Element:
    wrap = ET.fromstring(f"<w:wrap {_NS}>{xml}</w:wrap>")
    return wrap[0]


def _png(width: int = 40, height: int = 30) -> bytes:
    return encode_png(bytes((90, 120, 160)) * width * height, width, height)


def _make_pkg(
    tmp_path: Path,
    body: str,
    *,
    doc_rels: str = "",
    extra: dict[str, str | bytes] | None = None,
    name: str = "edge.docx",
) -> DocxPackage:
    parts: dict[str, str | bytes] = {
        "_rels/.rels": _ROOT_RELS,
        "word/document.xml": f"<w:document {_NS}><w:body>{body}</w:body></w:document>",
        "word/_rels/document.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f"{doc_rels}</Relationships>"
        ),
    }
    if extra:
        parts.update(extra)
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as zf:
        for part_name, data in parts.items():
            zf.writestr(part_name, data)
    return DocxPackage(path)


def _items(pkg: DocxPackage) -> list[Any]:
    pages = extract_docx_pages(pkg)
    return [it for page in pages for it in page.items]


def _paras(items: list[Any]) -> list[RawDocxParagraph]:
    return [it for it in items if isinstance(it, RawDocxParagraph)]


def _num_p(ilvl: int, text: str) -> str:
    return (
        f'<w:p><w:pPr><w:numPr><w:ilvl w:val="{ilvl}"/><w:numId w:val="7"/>'
        f"</w:numPr></w:pPr><w:r><w:t>{text}</w:t></w:r></w:p>"
    )


# ---------------------------------------------------------------------------
# Office Math 수식 문자열 변환
# ---------------------------------------------------------------------------


def test_omath_none_run_text_and_containers() -> None:
    assert _format_omath_node(None) == ""
    assert _format_omath_node(_el("<m:t>x</m:t>")) == "x"
    assert _format_omath_node(_el("<m:r><m:t>a</m:t><m:t>b</m:t></m:r>")) == "ab"
    node = _el("<m:oMath><m:r><m:t>a</m:t></m:r><m:ctrlPr/><m:r><m:t>b</m:t></m:r></m:oMath>")
    assert _format_omath_node(node) == "ab"
    assert _format_omath_node(_el("<m:rPr/>")) == ""
    assert _format_omath_node(_el("<m:unknown><m:r><m:t>z</m:t></m:r></m:unknown>")) == "z"


def test_omath_fraction_parenthesizes_and_handles_empty_parts() -> None:
    frac = (
        "<m:f><m:num><m:r><m:t>{num}</m:t></m:r></m:num>"
        "<m:den><m:r><m:t>{den}</m:t></m:r></m:den></m:f>"
    )
    assert _format_omath_node(_el(frac.format(num="1", den="2"))) == "1/2"
    assert _format_omath_node(_el(frac.format(num="a+b", den="2"))) == "(a+b)/2"
    assert _format_omath_node(_el("<m:f><m:num><m:r><m:t>1</m:t></m:r></m:num></m:f>")) == "1"
    assert _format_omath_node(_el("<m:f><m:den><m:r><m:t>2</m:t></m:r></m:den></m:f>")) == "2"


def test_omath_sup_sub_and_subsup() -> None:
    sup = (
        "<m:sSup><m:e><m:r><m:t>x</m:t></m:r></m:e>"
        "<m:sup><m:r><m:t>{s}</m:t></m:r></m:sup></m:sSup>"
    )
    assert _format_omath_node(_el(sup.format(s="2"))) == "x^2"
    assert _format_omath_node(_el(sup.format(s="10"))) == "x^{10}"
    assert _format_omath_node(_el("<m:sSup><m:e><m:r><m:t>x</m:t></m:r></m:e></m:sSup>")) == "x"

    sub = (
        "<m:sSub><m:e><m:r><m:t>x</m:t></m:r></m:e>"
        "<m:sub><m:r><m:t>{s}</m:t></m:r></m:sub></m:sSub>"
    )
    assert _format_omath_node(_el(sub.format(s="2"))) == "x_2"
    assert _format_omath_node(_el(sub.format(s="10"))) == "x_{10}"
    assert _format_omath_node(_el("<m:sSub><m:e><m:r><m:t>x</m:t></m:r></m:e></m:sSub>")) == "x"

    ss = (
        "<m:sSubSup><m:e><m:r><m:t>x</m:t></m:r></m:e>"
        "<m:sub><m:r><m:t>{sb}</m:t></m:r></m:sub>"
        "<m:sup><m:r><m:t>{sp}</m:t></m:r></m:sup></m:sSubSup>"
    )
    assert _format_omath_node(_el(ss.format(sb="2", sp="3"))) == "x_2^3"
    assert _format_omath_node(_el(ss.format(sb="10", sp="11"))) == "x_{10}^{11}"
    no_sub = (
        "<m:sSubSup><m:e><m:r><m:t>x</m:t></m:r></m:e>"
        "<m:sup><m:r><m:t>3</m:t></m:r></m:sup></m:sSubSup>"
    )
    assert _format_omath_node(_el(no_sub)) == "x^3"
    no_sup = (
        "<m:sSubSup><m:e><m:r><m:t>x</m:t></m:r></m:e>"
        "<m:sub><m:r><m:t>2</m:t></m:r></m:sub></m:sSubSup>"
    )
    assert _format_omath_node(_el(no_sup)) == "x_2"


def test_omath_radical_nary_delimiter_matrix_eqarr_func() -> None:
    assert _format_omath_node(_el("<m:rad><m:e><m:r><m:t>x</m:t></m:r></m:e></m:rad>")) == "sqrt(x)"
    rad_deg = (
        "<m:rad><m:deg><m:r><m:t>3</m:t></m:r></m:deg><m:e><m:r><m:t>x</m:t></m:r></m:e></m:rad>"
    )
    assert _format_omath_node(_el(rad_deg)) == "sqrt[3](x)"

    nary = (
        "<m:nary><m:naryPr><m:chr m:val='∫'/></m:naryPr>"
        "<m:sub><m:r><m:t>i</m:t></m:r></m:sub><m:sup><m:r><m:t>n</m:t></m:r></m:sup>"
        "<m:e><m:r><m:t>f</m:t></m:r></m:e></m:nary>"
    )
    assert _format_omath_node(_el(nary)) == "∫_{i}^{n} f"
    nary_default = "<m:nary><m:e><m:r><m:t>a</m:t></m:r></m:e></m:nary>"
    assert _format_omath_node(_el(nary_default)) == "∑ a"

    d = "<m:d><m:e><m:r><m:t>a</m:t></m:r></m:e><m:e><m:r><m:t>b</m:t></m:r></m:e></m:d>"
    assert _format_omath_node(_el(d)) == "(a, b)"
    d_custom = (
        "<m:d><m:dPr><m:begChr m:val='['/><m:endChr m:val=']'/><m:sepChr m:val=';'/></m:dPr>"
        "<m:e><m:r><m:t>a</m:t></m:r></m:e><m:e><m:r><m:t>b</m:t></m:r></m:e></m:d>"
    )
    assert _format_omath_node(_el(d_custom)) == "[a ; b]"

    matrix = (
        "<m:m><m:mr><m:e><m:r><m:t>1</m:t></m:r></m:e><m:e><m:r><m:t>2</m:t></m:r></m:e></m:mr>"
        "<m:mr><m:e><m:r><m:t>3</m:t></m:r></m:e></m:mr></m:m>"
    )
    assert _format_omath_node(_el(matrix)) == "[1, 2; 3]"

    eqarr = (
        "<m:eqArr><m:e><m:r><m:t>a</m:t></m:r></m:e><m:e><m:r><m:t>b</m:t></m:r></m:e></m:eqArr>"
    )
    assert _format_omath_node(_el(eqarr)) == "a ; b"

    func = (
        "<m:func><m:fName><m:r><m:t>sin</m:t></m:r></m:fName>"
        "<m:e><m:r><m:t>x</m:t></m:r></m:e></m:func>"
    )
    assert _format_omath_node(_el(func)) == "sin(x)"
    func_no_name = "<m:func><m:e><m:r><m:t>x</m:t></m:r></m:e></m:func>"
    assert _format_omath_node(_el(func_no_name)) == "x"


# ---------------------------------------------------------------------------
# 섹션 속성 파싱
# ---------------------------------------------------------------------------


def test_section_pr_defaults_and_page_size() -> None:
    spec = _parse_section_pr(_el("<w:sectPr/>"))
    assert spec == DocxSectionSpec()
    sized = _parse_section_pr(_el('<w:sectPr><w:pgSz w:w="11906" w:h="16838"/></w:sectPr>'))
    assert (sized.width_pt, sized.height_pt) == (595.3, 841.9)


def test_section_pr_landscape_swap_and_invalid_size() -> None:
    land = _parse_section_pr(
        _el('<w:sectPr><w:pgSz w:w="11906" w:h="16838" w:orient="landscape"/></w:sectPr>')
    )
    assert (land.width_pt, land.height_pt) == (841.9, 595.3)
    already = _parse_section_pr(
        _el('<w:sectPr><w:pgSz w:w="16838" w:h="11906" w:orient="landscape"/></w:sectPr>')
    )
    assert (already.width_pt, already.height_pt) == (841.9, 595.3)
    tiny = _parse_section_pr(_el('<w:sectPr><w:pgSz w:w="10" w:h="10"/></w:sectPr>'))
    assert (tiny.width_pt, tiny.height_pt) == (
        DocxSectionSpec().width_pt,
        DocxSectionSpec().height_pt,
    )


def test_section_pr_margins_clamp_negative_and_columns() -> None:
    mar = _parse_section_pr(
        _el('<w:sectPr><w:pgMar w:top="1440" w:bottom="-100" w:left="720" w:right="0"/></w:sectPr>')
    )
    assert (mar.margin_top_pt, mar.margin_bottom_pt) == (72.0, 0.0)
    assert (mar.margin_left_pt, mar.margin_right_pt) == (36.0, 0.0)

    cols = _parse_section_pr(_el('<w:sectPr><w:cols w:num="2" w:space="720"/></w:sectPr>'))
    assert (cols.column_count, cols.column_space_pt) == (2, 36.0)
    assert _parse_section_pr(_el('<w:sectPr><w:cols w:num="abc"/></w:sectPr>')).column_count == 1
    assert _parse_section_pr(_el('<w:sectPr><w:cols w:num="9"/></w:sectPr>')).column_count == 6
    assert _parse_section_pr(_el('<w:sectPr><w:cols w:num="0"/></w:sectPr>')).column_count == 1
    assert (
        _parse_section_pr(_el('<w:sectPr><w:cols w:space="0"/></w:sectPr>')).column_space_pt == 18.0
    )


def test_section_pr_break_type_and_header_footer_inheritance() -> None:
    typed = _parse_section_pr(_el('<w:sectPr><w:type w:val="continuous"/></w:sectPr>'))
    assert typed.break_type == "continuous"
    refs = _parse_section_pr(
        _el('<w:sectPr><w:headerReference r:id="rIdH"/><w:footerReference r:id="rIdF"/></w:sectPr>')
    )
    assert refs.header_rids == ("rIdH",)
    assert refs.footer_rids == ("rIdF",)
    prev = DocxSectionSpec(header_rids=("oldH",), footer_rids=("oldF",))
    inherited = _parse_section_pr(_el("<w:sectPr/>"), prev)
    assert inherited.header_rids == ("oldH",)
    assert inherited.footer_rids == ("oldF",)


# ---------------------------------------------------------------------------
# 순수 텍스트 · 로마 숫자 · 목록 마커 · 체크박스
# ---------------------------------------------------------------------------


def test_plain_text_container_none_and_run_pieces() -> None:
    assert _extract_plain_text_from_container(None) == ""
    root = _el(
        "<w:p><w:r><w:t>A</w:t></w:r><w:r><w:tab/></w:r><w:r><w:t>B</w:t></w:r>"
        "<w:r><w:br/></w:r><w:r><w:t>C</w:t></w:r>"
        "<w:del><w:r><w:t>GONE</w:t></w:r></w:del>"
        "<w:r><w:instrText>FIELD</w:instrText></w:r></w:p>"
    )
    assert _extract_plain_text_from_container(root) == "A\tB\nC"
    math_root = _el("<w:p><m:oMath><m:r><m:t>E</m:t></m:r></m:oMath></w:p>")
    assert _extract_plain_text_from_container(math_root) == "E"


@pytest.mark.parametrize(
    ("num", "expected"),
    [
        (0, "0"),
        (-5, "-5"),
        (1, "i"),
        (4, "iv"),
        (9, "ix"),
        (14, "xiv"),
        (40, "xl"),
        (90, "xc"),
        (400, "cd"),
        (900, "cm"),
        (1000, "m"),
        (3999, "mmmcmxcix"),
        (4000, "mmmcmxcix"),
    ],
)
def test_int_to_roman_boundaries(num: int, expected: str) -> None:
    assert _int_to_roman(num) == expected


@pytest.mark.parametrize(
    ("num", "fmt", "expected"),
    [
        (1, "decimal", "1."),
        (0, "decimal", "0."),
        (1, "lowerLetter", "a."),
        (27, "lowerLetter", "a."),
        (2, "upperLetter", "B."),
        (4, "lowerRoman", "iv."),
        (0, "lowerRoman", "i."),
        (4, "upperRoman", "IV."),
        (1, "ganada", "가."),
        (2, "chosung", "ㄴ."),
        (1, "koreanlegal", "가."),
        (1, "koreandigital", "가."),
        (3, "unknownFmt", "3."),
    ],
)
def test_format_ordered_list_marker_formats(num: int, fmt: str, expected: str) -> None:
    assert _format_ordered_list_marker(num, fmt) == expected


def test_ffdata_checkbox_checked_default_and_absent() -> None:
    checked = _el(
        '<w:fldChar w:fldCharType="begin"><w:ffData><w:checkBox>'
        '<w:checked w:val="1"/></w:checkBox></w:ffData></w:fldChar>'
    )
    assert _extract_ffdata_checkbox(checked) == "☑"
    unchecked = _el(
        '<w:fldChar w:fldCharType="begin"><w:ffData><w:checkBox>'
        '<w:checked w:val="0"/></w:checkBox></w:ffData></w:fldChar>'
    )
    assert _extract_ffdata_checkbox(unchecked) == "☐"
    default_only = _el(
        '<w:fldChar w:fldCharType="begin"><w:ffData><w:checkBox>'
        '<w:default w:val="1"/></w:checkBox></w:ffData></w:fldChar>'
    )
    assert _extract_ffdata_checkbox(default_only) == "☑"
    assert _extract_ffdata_checkbox(_el('<w:fldChar w:fldCharType="begin"/>')) is None


def test_sdt_checkbox_variants() -> None:
    assert _extract_sdt_checkbox(_el("<w:sdt/>")) is None
    checked = _el(
        "<w:sdt><w:sdtPr><w14:checkbox><w14:checked w14:val='1'/></w14:checkbox></w:sdtPr></w:sdt>"
    )
    assert _extract_sdt_checkbox(checked) == "☑"
    unchecked = _el(
        "<w:sdt><w:sdtPr><w14:checkbox><w14:checked w14:val='0'/></w14:checkbox></w:sdtPr></w:sdt>"
    )
    assert _extract_sdt_checkbox(unchecked) == "☐"
    no_checked = _el("<w:sdt><w:sdtPr><w14:checkbox/></w:sdtPr></w:sdt>")
    assert _extract_sdt_checkbox(no_checked) == "☐"
    no_checkbox = _el("<w:sdt><w:sdtPr><w14:textInput/></w:sdtPr></w:sdt>")
    assert _extract_sdt_checkbox(no_checkbox) is None


def test_resolve_blip_embed_id_direct_svg_and_absent() -> None:
    assert _resolve_blip_embed_id(_el('<a:blip r:embed="rId1"/>')) == "rId1"
    svg = _el(
        '<a:blip><a:extLst><a:ext><asvg:svgBlip r:embed="rIdSvg"/></a:ext></a:extLst></a:blip>'
    )
    assert _resolve_blip_embed_id(svg) == "rIdSvg"
    assert _resolve_blip_embed_id(_el("<a:blip/>")) == ""


def test_iter_unwrapped_children_skips_del_and_unwraps_wrappers() -> None:
    parent = _el(
        "<w:tr><w:tc><w:p><w:r><w:t>A</w:t></w:r></w:p></w:tc>"
        "<w:del><w:tc><w:p><w:r><w:t>X</w:t></w:r></w:p></w:tc></w:del>"
        "<w:sdt><w:sdtContent><w:tc><w:p><w:r><w:t>B</w:t></w:r></w:p></w:tc>"
        "</w:sdtContent></w:sdt>"
        "<w:ins><w:tc><w:p><w:r><w:t>C</w:t></w:r></w:p></w:tc></w:ins>"
        "<w:customXml><w:tc><w:p><w:r><w:t>D</w:t></w:r></w:p></w:tc></w:customXml>"
        "<w:moveTo><w:tc><w:p><w:r><w:t>E</w:t></w:r></w:p></w:tc></w:moveTo></w:tr>"
    )
    tcs = _iter_unwrapped_children(parent, {"tc"})
    # 삭제(del) 셀은 제외되고 moveTo 안 셀은 포함되며, 순서대로 다섯 셀이어야 한다
    assert [_extract_plain_text_from_container(tc) for tc in tcs] == ["A", "B", "C", "D", "E"]


# ---------------------------------------------------------------------------
# 표 테두리 전략 · 높이 추정
# ---------------------------------------------------------------------------


def _tbl_with_borders(
    borders: str, *, tbl_style: bool = False
) -> tuple[ET.Element, list[ET.Element]]:
    style = '<w:tblStyle w:val="Grid"/>' if tbl_style else ""
    tbl = _el(
        f"<w:tbl><w:tblPr>{style}<w:tblBorders>{borders}</w:tblBorders></w:tblPr>"
        "<w:tr><w:tc><w:p><w:r><w:t>x</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
    )
    return tbl, tbl.findall(qn(NS_W, "tr"))


def test_detect_table_strategy_ruled_horizontal_borderless() -> None:
    all_single = "".join(
        f'<w:{t} w:val="single"/>' for t in ("top", "bottom", "insideH", "left", "right", "insideV")
    )
    tbl, trs = _tbl_with_borders(all_single)
    assert _detect_docx_table_strategy(tbl, trs) == ParseStrategy.RULED_TABLE

    horiz = (
        '<w:top w:val="single"/><w:bottom w:val="single"/><w:insideH w:val="single"/>'
        '<w:left w:val="none"/><w:right w:val="none"/><w:insideV w:val="none"/>'
    )
    tbl, trs = _tbl_with_borders(horiz)
    assert _detect_docx_table_strategy(tbl, trs) == ParseStrategy.HORIZONTAL_RULED_TABLE

    none_all = "".join(
        f'<w:{t} w:val="none"/>' for t in ("top", "bottom", "insideH", "left", "right", "insideV")
    )
    tbl, trs = _tbl_with_borders(none_all)
    assert _detect_docx_table_strategy(tbl, trs) == ParseStrategy.BORDERLESS_TABLE
    tbl, trs = _tbl_with_borders(none_all, tbl_style=True)
    assert _detect_docx_table_strategy(tbl, trs) == ParseStrategy.RULED_TABLE


def test_estimate_item_height_by_type() -> None:
    para = RawDocxParagraph(text="hello", font_size_pt=10.0)
    assert _estimate_item_height_pt(para, 400.0) == pytest.approx(1 * (10.0 * 1.38) + 5.0)
    heading = RawDocxParagraph(text="title", heading_level=1, font_size_pt=10.0)
    assert _estimate_item_height_pt(heading, 400.0) == pytest.approx(1 * (10.0 * 1.38) + 10.0)

    tbl = RawDocxTableItem(
        table=ExtractedTable(
            table_id="t",
            page=1,
            bbox=make_bbox(0.0, 0.0, 100.0, 50.0, 400.0, 600.0),
            strategy=ParseStrategy.RULED_TABLE,
            confidence=0.9,
            row_count=1,
            col_count=1,
            header_row_count=0,
            headers=["c"],
            rows=[],
            cells=[],
        ),
        raw_width_pt=100.0,
        raw_height_pt=50.0,
    )
    assert _estimate_item_height_pt(tbl, 400.0) == 64.0

    img = RawPptxImage(
        shape_id="1",
        name="i",
        alt_text="",
        media_part="m",
        extension=".png",
        rect_pt=(0, 0, 100, 20),
        raw_bytes=None,
        pixel_width=None,
        pixel_height=None,
    )
    assert _estimate_item_height_pt(img, 400.0) == 46.0

    diagram = RawDocxVectorDiagram(
        diagram_id="d", name="d", width_pt=100.0, height_pt=30.0, shape_count=3
    )
    assert _estimate_item_height_pt(diagram, 400.0) == 72.0


# ---------------------------------------------------------------------------
# 문단 추출: 런 속성 · 페이지 브레이크 · 필드 · 각주 · 수식 · 불릿
# ---------------------------------------------------------------------------


def test_paragraph_run_font_bold_italic_monospace(tmp_path: Path) -> None:
    pkg = _make_pkg(
        tmp_path,
        '<w:p><w:r><w:rPr><w:sz w:val="24"/><w:b/><w:i/><w:rFonts w:ascii="Consolas"/></w:rPr>'
        "<w:t>code text</w:t></w:r></w:p>",
    )
    para = _paras(_items(pkg))[0]
    assert para.text == "code text"
    assert para.font_size_pt == 12.0
    assert para.is_bold and para.is_italic and para.is_code_or_formula


def test_paragraph_level_rpr_size_bold_italic_and_vanish(tmp_path: Path) -> None:
    pkg = _make_pkg(
        tmp_path,
        '<w:p><w:pPr><w:rPr><w:sz w:val="28"/><w:b/><w:i/>'
        '<w:rFonts w:ascii="Courier New"/></w:rPr></w:pPr>'
        "<w:r><w:t>styled</w:t></w:r></w:p>"
        "<w:p><w:pPr><w:rPr><w:vanish/></w:rPr></w:pPr><w:r><w:t>hidden</w:t></w:r></w:p>",
    )
    paras = _paras(_items(pkg))
    assert [p.text for p in paras] == ["styled"]
    assert paras[0].font_size_pt == 14.0
    assert paras[0].is_bold and paras[0].is_italic and paras[0].is_code_or_formula


def test_paragraph_szcs_fallback_and_invalid_outline(tmp_path: Path) -> None:
    pkg = _make_pkg(
        tmp_path,
        '<w:p><w:pPr><w:rPr><w:szCs w:val="20"/></w:rPr></w:pPr><w:r><w:t>a</w:t></w:r></w:p>'
        '<w:p><w:pPr><w:outlineLvl w:val="2"/></w:pPr><w:r><w:t>head</w:t></w:r></w:p>'
        '<w:p><w:pPr><w:outlineLvl w:val="9"/></w:pPr><w:r><w:t>nohead</w:t></w:r></w:p>'
        '<w:p><w:pPr><w:outlineLvl w:val="abc"/></w:pPr><w:r><w:t>bad</w:t></w:r></w:p>',
    )
    paras = _paras(_items(pkg))
    assert paras[0].font_size_pt == 10.0
    assert paras[1].heading_level == 3
    assert paras[2].heading_level is None
    assert paras[3].heading_level is None


def test_paragraph_page_break_before_and_mid_break_split(tmp_path: Path) -> None:
    pkg = _make_pkg(
        tmp_path,
        "<w:p><w:r><w:t>first</w:t></w:r></w:p>"
        "<w:p><w:pPr><w:pageBreakBefore/></w:pPr><w:r><w:t>second</w:t></w:r></w:p>"
        '<w:p><w:pPr><w:pageBreakBefore w:val="0"/></w:pPr><w:r><w:t>third</w:t></w:r></w:p>',
    )
    pages = extract_docx_pages(pkg)
    assert len(pages) == 2
    assert [p.text for p in _paras(pages[0].items)] == ["first"]
    assert [p.text for p in _paras(pages[1].items)] == ["second", "third"]

    split_pkg = _make_pkg(
        tmp_path,
        "<w:p><w:r><w:t>A</w:t></w:r><w:r><w:lastRenderedPageBreak/><w:t>B</w:t></w:r></w:p>",
        name="split.docx",
    )
    split_pages = extract_docx_pages(split_pkg)
    assert len(split_pages) == 2
    assert _paras(split_pages[0].items)[0].text == "A"
    assert _paras(split_pages[1].items)[0].text == "B"


def test_paragraph_special_chars_math_bullet_and_comment(tmp_path: Path) -> None:
    pkg = _make_pkg(
        tmp_path,
        "<w:p><w:r><w:t>a</w:t><w:noBreakHyphen/><w:t>b</w:t><w:cr/><w:t>c</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>값</w:t></w:r><m:oMath><m:r><m:t>x</m:t></m:r></m:oMath></w:p>"
        "<w:p><w:r><w:t>• 항목</w:t></w:r></w:p>"
        '<w:p><w:commentRangeStart w:id="0"/><w:r><w:t>note</w:t></w:r></w:p>',
        doc_rels=(
            '<Relationship Id="rIdC" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments" '
            'Target="comments.xml"/>'
        ),
        extra={
            "word/comments.xml": (
                f'<w:comments {_NS}><w:comment w:id="0" w:author="검토자">'
                "<w:p><w:r><w:t>메모</w:t></w:r></w:p></w:comment></w:comments>"
            )
        },
    )
    pages = extract_docx_pages(pkg)
    paras = _paras(_items(pkg))
    assert paras[0].text == "a-b\nc"
    assert paras[1].text == "값 x" and paras[1].has_math
    assert paras[2].is_list_item and not paras[2].is_ordered_list
    assert paras[3].comment_ids == ("0",)
    assert pages[-1].comments and pages[-1].comments[0].text == "메모"


def test_paragraph_ordered_list_counter_reset_and_style_list(tmp_path: Path) -> None:
    numbering = (
        f'<w:numbering {_NS}><w:abstractNum w:abstractNumId="0">'
        '<w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/></w:lvl>'
        '<w:lvl w:ilvl="1"><w:start w:val="1"/><w:numFmt w:val="lowerLetter"/></w:lvl>'
        '</w:abstractNum><w:num w:numId="7"><w:abstractNumId w:val="0"/></w:num></w:numbering>'
    )
    styles = (
        f'<w:styles {_NS}><w:style w:type="paragraph" w:styleId="ListBullet">'
        '<w:name w:val="List Bullet"/></w:style>'
        '<w:style w:type="paragraph" w:styleId="StyleNum">'
        '<w:name w:val="Style Num"/><w:pPr><w:numPr>'
        '<w:numId w:val="7"/><w:ilvl w:val="0"/></w:numPr></w:pPr>'
        "</w:style></w:styles>"
    )
    body = (
        _num_p(0, "일")
        + _num_p(1, "에이")
        + _num_p(0, "이")
        + _num_p(1, "비")
        + '<w:p><w:pPr><w:pStyle w:val="ListBullet"/></w:pPr><w:r><w:t>불릿</w:t></w:r></w:p>'
        + '<w:p><w:pPr><w:pStyle w:val="StyleNum"/></w:pPr><w:r><w:t>스타일번호</w:t></w:r></w:p>'
    )
    pkg = _make_pkg(
        tmp_path,
        body,
        doc_rels=(
            '<Relationship Id="rIdN" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering" '
            'Target="numbering.xml"/>'
            '<Relationship Id="rIdS" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
            'Target="styles.xml"/>'
        ),
        extra={"word/numbering.xml": numbering, "word/styles.xml": styles},
    )
    paras = _paras(_items(pkg))
    assert [p.list_marker for p in paras[:4]] == ["1.", "a.", "2.", "a."]
    assert paras[4].is_list_item and not paras[4].is_ordered_list
    assert paras[5].is_ordered_list and paras[5].list_marker == "3."


# ---------------------------------------------------------------------------
# 그리기(drawing): 그림 · 차트 · SmartArt · 텍스트상자 · 벡터 도식
# ---------------------------------------------------------------------------

_CHART_XML = (
    '<c:chartSpace xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart">'
    "<c:chart><c:plotArea><c:barChart><c:ser>"
    '<c:cat><c:strRef><c:strCache><c:pt idx="0"><c:v>2024</c:v></c:pt>'
    "</c:strCache></c:strRef></c:cat>"
    '<c:val><c:numRef><c:numCache><c:pt idx="0"><c:v>120</c:v></c:pt>'
    "</c:numCache></c:numRef></c:val>"
    "</c:ser></c:barChart></c:plotArea></c:chart></c:chartSpace>"
)
_CHARTEX_XML = (
    '<cx:chartSpace xmlns:cx="http://schemas.microsoft.com/office/drawing/2014/chartex">'
    '<cx:chartData><cx:data id="0">'
    '<cx:strDim type="cat"><cx:lvl ptCount="1"><cx:pt idx="0">기초</cx:pt></cx:lvl></cx:strDim>'
    '<cx:numDim type="val"><cx:lvl ptCount="1"><cx:pt idx="0">500</cx:pt></cx:lvl></cx:numDim>'
    "</cx:data></cx:chartData><cx:chart><cx:plotArea><cx:plotAreaRegion>"
    '<cx:series layoutId="waterfall" dataId="0"/>'
    "</cx:plotAreaRegion></cx:plotArea></cx:chart></cx:chartSpace>"
)
_DGM_XML = (
    '<dgm:dataModel xmlns:dgm="http://schemas.openxmlformats.org/drawingml/2006/diagram" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
    '<dgm:ptLst><dgm:pt modelId="0" type="doc"/>'
    '<dgm:pt modelId="1"><dgm:t><a:p><a:r><a:t>수집</a:t></a:r></a:p></dgm:t></dgm:pt></dgm:ptLst>'
    '<dgm:cxnLst><dgm:cxn modelId="c1" srcId="0" destId="1" srcOrd="0" destOrd="0"/></dgm:cxnLst>'
    "</dgm:dataModel>"
)


def _inline(
    graphic_data: str, *, doc_pr: str = "", extent: str = 'cx="2286000" cy="914400"'
) -> str:
    return (
        f"<w:drawing><wp:inline><wp:extent {extent}/>{doc_pr}"
        f"<a:graphic><a:graphicData uri='uri'>{graphic_data}</a:graphicData></a:graphic>"
        "</wp:inline></w:drawing>"
    )


def test_drawing_image_hidden_and_missing_graphic_data(tmp_path: Path) -> None:
    body = (
        "<w:p><w:r>"
        + _inline(
            '<pic:pic><pic:blipFill><a:blip r:embed="rIdImg"/></pic:blipFill></pic:pic>',
            doc_pr='<wp:docPr id="1" name="Pic" descr="설명"/>',
        )
        + "</w:r></w:p>"
        "<w:p><w:r>"
        + _inline(
            '<pic:pic><pic:blipFill><a:blip r:embed="rIdImg"/></pic:blipFill></pic:pic>',
            doc_pr='<wp:docPr id="2" name="Hidden" hidden="1"/>',
        )
        + "</w:r></w:p>"
        "<w:p><w:r><w:drawing><wp:inline><a:graphic/></wp:inline></w:drawing></w:r></w:p>"
        "<w:p><w:r><w:drawing><wp:unknown/></w:drawing></w:r></w:p>"
    )
    pkg = _make_pkg(
        tmp_path,
        body,
        doc_rels=(
            '<Relationship Id="rIdImg" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
            'Target="media/image1.png"/>'
        ),
        extra={"word/media/image1.png": _png()},
    )
    images = [it for it in _items(pkg) if isinstance(it, RawPptxImage)]
    assert len(images) == 1
    assert images[0].name == "Pic"
    assert images[0].alt_text == "설명"
    assert images[0].pixel_width == 40 and images[0].pixel_height == 30


def test_drawing_chart_chartex_smartart_and_external_skip(tmp_path: Path) -> None:
    body = (
        "<w:p><w:r>" + _inline('<c:chart r:id="rIdChart"/>') + "</w:r></w:p>"
        "<w:p><w:r>" + _inline('<cx:chart r:id="rIdChartEx"/>') + "</w:r></w:p>"
        "<w:p><w:r>" + _inline('<dgm:relIds r:dm="rIdDgm"/>') + "</w:r></w:p>"
        "<w:p><w:r>" + _inline('<c:chart r:id="rIdExt"/>') + "</w:r></w:p>"
    )
    pkg = _make_pkg(
        tmp_path,
        body,
        doc_rels=(
            '<Relationship Id="rIdChart" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/chart" '
            'Target="charts/chart1.xml"/>'
            '<Relationship Id="rIdChartEx" '
            'Type="http://schemas.microsoft.com/office/2014/relationships/chartEx" '
            'Target="charts/chartEx1.xml"/>'
            '<Relationship Id="rIdDgm" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            'relationships/diagramData" '
            'Target="diagrams/data1.xml"/>'
            '<Relationship Id="rIdExt" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/chart" '
            'Target="https://example.org/chart.xml" TargetMode="External"/>'
        ),
        extra={
            "word/charts/chart1.xml": _CHART_XML,
            "word/charts/chartEx1.xml": _CHARTEX_XML,
            "word/diagrams/data1.xml": _DGM_XML,
        },
    )
    items = _items(pkg)
    charts = [it for it in items if isinstance(it, RawPptxChart)]
    smartart = [it for it in items if isinstance(it, RawPptxSmartArt)]
    # 네이티브 + chartEx 파트가 정확히 하나씩 연결되어야 한다(외부 차트는 제외)
    assert {c.chart_part for c in charts} == {
        "word/charts/chart1.xml",
        "word/charts/chartEx1.xml",
    }
    assert len(smartart) == 1
    assert any("수집" in text for _depth, text in smartart[0].items)


def test_drawing_vector_diagram_from_textbox_shapes(tmp_path: Path) -> None:
    shapes = (
        "<wps:wsp><wps:txbx><w:txbxContent><w:p><w:r><w:t>상자글</w:t></w:r></w:p>"
        "</w:txbxContent></wps:txbx></wps:wsp><wps:wsp/><wps:wsp/>"
    )
    pkg = _make_pkg(tmp_path, "<w:p><w:r>" + _inline(shapes) + "</w:r></w:p>")
    diagrams = [it for it in _items(pkg) if isinstance(it, RawDocxVectorDiagram)]
    assert len(diagrams) == 1
    assert diagrams[0].shape_count == 3
    assert diagrams[0].text_hints == ("상자글",)
    assert any(isinstance(it, RawDocxParagraph) and it.in_textbox for it in _items(pkg))


# ---------------------------------------------------------------------------
# 레거시 VML(w:pict) · OLE(w:object)
# ---------------------------------------------------------------------------


def _mini_xlsx() -> bytes:
    import io

    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    workbook = (
        f'<workbook xmlns="{ns}" xmlns:r="{rns}"><sheets>'
        '<sheet name="S1" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    wb_rels = (
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'<Relationship Id="rId1" Type="{rns}/worksheet" Target="worksheets/sheet1.xml"/>'
        "</Relationships>"
    )
    sheet = (
        f'<worksheet xmlns="{ns}"><sheetData>'
        '<row r="1"><c r="A1" t="inlineStr"><is><t>항목</t></is></c>'
        '<c r="B1" t="inlineStr"><is><t>값</t></is></c></row>'
        '<row r="2"><c r="A2" t="inlineStr"><is><t>매출</t></is></c>'
        '<c r="B2"><v>100</v></c></row></sheetData></worksheet>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("xl/workbook.xml", workbook)
        zf.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        zf.writestr("xl/worksheets/sheet1.xml", sheet)
    return buf.getvalue()


def test_pict_vml_image_and_textbox(tmp_path: Path) -> None:
    body = (
        '<w:p><w:r><w:pict><v:shape><v:imagedata r:id="rIdImg" o:title="VML그림"/>'
        "</v:shape></w:pict></w:r></w:p>"
        "<w:p><w:r><w:pict><v:shape><v:textbox><w:txbxContent>"
        "<w:p><w:r><w:t>상자 글</w:t></w:r></w:p>"
        "</w:txbxContent></v:textbox></v:shape></w:pict></w:r></w:p>"
    )
    pkg = _make_pkg(
        tmp_path,
        body,
        doc_rels=(
            '<Relationship Id="rIdImg" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
            'Target="media/image1.png"/>'
        ),
        extra={"word/media/image1.png": _png()},
    )
    items = _items(pkg)
    images = [it for it in items if isinstance(it, RawPptxImage)]
    assert len(images) == 1
    assert images[0].alt_text == "VML그림"
    textbox_paras = [it for it in items if isinstance(it, RawDocxParagraph) and it.in_textbox]
    assert [p.text for p in textbox_paras] == ["상자 글"]


def test_object_ole_non_xlsx_and_embedded_xlsx(tmp_path: Path) -> None:
    body = (
        '<w:p><w:r><w:object><o:OLEObject ProgID="Word.Document.8" r:id="rIdBin"/>'
        "</w:object></w:r></w:p>"
        '<w:p><w:r><w:object><o:OLEObject ProgID="Excel.Sheet.12" r:id="rIdXlsx"/>'
        "</w:object></w:r></w:p>"
    )
    pkg = _make_pkg(
        tmp_path,
        body,
        doc_rels=(
            '<Relationship Id="rIdBin" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/oleObject" '
            'Target="embeddings/ole1.bin"/>'
            '<Relationship Id="rIdXlsx" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/oleObject" '
            'Target="embeddings/ole2.xlsx"/>'
        ),
        extra={
            "word/embeddings/ole1.bin": b"not-an-xlsx",
            "word/embeddings/ole2.xlsx": _mini_xlsx(),
        },
    )
    items = _items(pkg)
    ole = [it for it in items if isinstance(it, RawPptxOleObject)]
    tables = [it for it in items if isinstance(it, RawDocxTableItem)]
    assert len(ole) == 1 and ole[0].prog_id == "Word.Document.8"
    assert len(tables) == 1
    joined = " ".join(c.text for c in tables[0].table.cells)
    assert "매출" in joined and "100" in joined


# ---------------------------------------------------------------------------
# 표: 빈 표 · 병합(hMerge/vMerge/gridBefore) · 중첩 표 · 헤더 폴백 · 빈 행 정리
# ---------------------------------------------------------------------------


def _tables(items: list[Any]) -> list[RawDocxTableItem]:
    return [it for it in items if isinstance(it, RawDocxTableItem)]


def test_table_empty_and_no_rows_produce_no_table(tmp_path: Path) -> None:
    pkg = _make_pkg(
        tmp_path,
        "<w:tbl><w:tr><w:tc><w:p/></w:tc></w:tr></w:tbl>"
        '<w:tbl><w:tblGrid><w:gridCol w:w="2400"/></w:tblGrid></w:tbl>',
    )
    assert _tables(_items(pkg)) == []


def test_table_hmerge_restart_continue_and_grid_before(tmp_path: Path) -> None:
    pkg = _make_pkg(
        tmp_path,
        '<w:tbl><w:tblGrid><w:gridCol w:w="2400"/><w:gridCol w:w="2400"/>'
        '<w:gridCol w:w="2400"/></w:tblGrid>'
        "<w:tr>"
        '<w:tc><w:tcPr><w:hMerge w:val="restart"/></w:tcPr>'
        "<w:p><w:r><w:t>머리</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:tcPr><w:hMerge/></w:tcPr><w:p/></w:tc>"
        "<w:tc><w:p><w:r><w:t>끝</w:t></w:r></w:p></w:tc>"
        "</w:tr>"
        "<w:tr>"
        "<w:tc><w:p><w:r><w:t>a</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>b</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>c</w:t></w:r></w:p></w:tc>"
        "</w:tr></w:tbl>"
        '<w:tbl><w:tblGrid><w:gridCol w:w="2400"/><w:gridCol w:w="2400"/></w:tblGrid>'
        '<w:tr><w:trPr><w:gridBefore w:val="1"/></w:trPr>'
        "<w:tc><w:p><w:r><w:t>앞빈칸</w:t></w:r></w:p></w:tc></w:tr>"
        "<w:tr><w:tc><w:p><w:r><w:t>가</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>나</w:t></w:r></w:p></w:tc></w:tr></w:tbl>",
    )
    tables = _tables(_items(pkg))
    assert len(tables) == 2
    hmerge_tbl = tables[0].table
    assert hmerge_tbl.has_merged_cells
    first_cell = next(c for c in hmerge_tbl.cells if c.row == 0 and c.col == 0)
    assert first_cell.col_span == 2
    grid_before_tbl = tables[1].table
    assert any(c.text == "앞빈칸" and c.col == 1 for c in grid_before_tbl.cells)


def test_table_nested_and_single_row_header_fallback(tmp_path: Path) -> None:
    pkg = _make_pkg(
        tmp_path,
        '<w:tbl><w:tblGrid><w:gridCol w:w="4000"/></w:tblGrid><w:tr><w:tc>'
        "<w:p><w:r><w:t>바깥</w:t></w:r></w:p>"
        '<w:tbl><w:tblGrid><w:gridCol w:w="2000"/><w:gridCol w:w="2000"/></w:tblGrid>'
        "<w:tr><w:tc><w:p><w:r><w:t>안</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>쪽</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
        "</w:tc></w:tr></w:tbl>"
        '<w:tbl><w:tblGrid><w:gridCol w:w="2400"/><w:gridCol w:w="2400"/></w:tblGrid>'
        "<w:tr><w:tc><w:p><w:r><w:t>가</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>나</w:t></w:r></w:p></w:tc></w:tr></w:tbl>",
    )
    tables = _tables(_items(pkg))
    assert len(tables) == 2
    outer_text = " ".join(c.text for c in tables[0].table.cells)
    assert "바깥" in outer_text and "안 | 쪽" in outer_text
    single = tables[1].table
    assert single.header_row_count == 0
    assert single.headers == ["col_1", "col_2"]
    assert single.rows == [["가", "나"]]


def test_table_trailing_empty_rows_trimmed_to_two(tmp_path: Path) -> None:
    pkg = _make_pkg(
        tmp_path,
        '<w:tbl><w:tblGrid><w:gridCol w:w="2400"/><w:gridCol w:w="2400"/></w:tblGrid>'
        "<w:tr><w:tc><w:p><w:r><w:t>항목</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>값</w:t></w:r></w:p></w:tc></w:tr>"
        "<w:tr><w:tc><w:p><w:r><w:t>매출</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>100</w:t></w:r></w:p></w:tc></w:tr>"
        "<w:tr><w:tc><w:p/></w:tc><w:tc><w:p/></w:tc></w:tr>"
        "<w:tr><w:tc><w:p/></w:tc><w:tc><w:p/></w:tc></w:tr></w:tbl>",
    )
    tbl = _tables(_items(pkg))[0].table
    assert tbl.row_count == 2
    assert tbl.rows == [["매출", "100"]]


# ---------------------------------------------------------------------------
# 페이지 분할: 오류 · 빈 본문 · 각주/미주/주석 · 머리글/바닥글 · 가상 분할
# ---------------------------------------------------------------------------


def test_extract_pages_corrupt_xml_and_missing_body(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.docx"
    with zipfile.ZipFile(corrupt, "w") as zf:
        zf.writestr("_rels/.rels", _ROOT_RELS)
        zf.writestr("word/document.xml", "<w:document")
    with pytest.raises(ParserInputError, match="손상"):
        extract_docx_pages(DocxPackage(corrupt))

    no_body = tmp_path / "no_body.docx"
    with zipfile.ZipFile(no_body, "w") as zf:
        zf.writestr("_rels/.rels", _ROOT_RELS)
        zf.writestr("word/document.xml", f"<w:document {_NS}/>")
    with pytest.raises(ParserInputError, match="요소"):
        extract_docx_pages(DocxPackage(no_body))


def test_extract_pages_empty_body_yields_one_empty_page(tmp_path: Path) -> None:
    pages = extract_docx_pages(_make_pkg(tmp_path, ""))
    assert len(pages) == 1
    assert pages[0].items == []


def test_unreferenced_notes_and_comments_attach_to_last_page(tmp_path: Path) -> None:
    pkg = _make_pkg(
        tmp_path,
        "<w:p><w:r><w:t>첫쪽</w:t></w:r></w:p>"
        "<w:p><w:pPr><w:pageBreakBefore/></w:pPr><w:r><w:t>끝쪽</w:t></w:r></w:p>",
        doc_rels=(
            '<Relationship Id="rIdFn" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes" '
            'Target="footnotes.xml"/>'
            '<Relationship Id="rIdEn" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/endnotes" '
            'Target="endnotes.xml"/>'
            '<Relationship Id="rIdC" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments" '
            'Target="comments.xml"/>'
        ),
        extra={
            "word/footnotes.xml": (
                f'<w:footnotes {_NS}><w:footnote w:type="separator" w:id="-1"><w:p/></w:footnote>'
                '<w:footnote w:id="1"><w:p><w:r><w:t>각주내용</w:t></w:r></w:p>'
                "</w:footnote></w:footnotes>"
            ),
            "word/endnotes.xml": (
                f'<w:endnotes {_NS}><w:endnote w:id="2"><w:p><w:r><w:t>미주내용</w:t></w:r></w:p>'
                "</w:endnote></w:endnotes>"
            ),
            "word/comments.xml": (
                f'<w:comments {_NS}><w:comment w:id="0" w:author="검토자">'
                "<w:p><w:r><w:t>주석내용</w:t></w:r></w:p></w:comment></w:comments>"
            ),
        },
    )
    pages = extract_docx_pages(pkg)
    assert len(pages) == 2
    first, last = pages[0], pages[-1]
    # 미참조 각주·미주·주석은 마지막 페이지에만 붙고 첫 페이지에는 없어야 한다
    assert first.footnotes == []
    assert first.endnotes == []
    assert first.comments == []
    assert last.footnotes == [("1", "각주내용")]
    assert last.endnotes == [("2", "미주내용")]
    assert [c.text for c in last.comments] == ["주석내용"]


def test_header_footer_internal_included_external_skipped(tmp_path: Path) -> None:
    pkg = _make_pkg(
        tmp_path,
        "<w:p><w:r><w:t>본문</w:t></w:r></w:p>"
        '<w:sectPr><w:headerReference r:id="rIdH"/><w:footerReference r:id="rIdF"/></w:sectPr>',
        doc_rels=(
            '<Relationship Id="rIdH" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/header" '
            'Target="header1.xml"/>'
            '<Relationship Id="rIdF" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer" '
            'Target="https://example.org/footer.xml" TargetMode="External"/>'
        ),
        extra={
            "word/header1.xml": (f"<w:hdr {_NS}><w:p><w:r><w:t>머리글</w:t></w:r></w:p></w:hdr>")
        },
    )
    page = extract_docx_pages(pkg)[0]
    assert page.headers == ["머리글"]
    assert page.footers == []


def test_long_document_without_breaks_splits_virtual_pages(tmp_path: Path) -> None:
    texts = [f"문단 {i:03d} " + "가" * 200 for i in range(100)]
    body = "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in texts)
    pages = extract_docx_pages(_make_pkg(tmp_path, body))
    assert len(pages) >= 2
    assert all(page.items for page in pages)
    # 가상 분할이 문단을 누락·중복 없이 입력 순서 그대로 보존해야 한다
    collected = [p.text for page in pages for p in _paras(page.items)]
    assert collected == texts


def test_body_wrappers_ins_and_custom_xml_are_unwrapped(tmp_path: Path) -> None:
    pkg = _make_pkg(
        tmp_path,
        "<w:ins><w:p><w:r><w:t>삽입</w:t></w:r></w:p></w:ins>"
        "<w:customXml><w:p><w:r><w:t>사용자정의</w:t></w:r></w:p></w:customXml>"
        "<w:moveTo><w:p><w:r><w:t>이동</w:t></w:r></w:p></w:moveTo>",
    )
    assert [p.text for p in _paras(_items(pkg))] == ["삽입", "사용자정의", "이동"]


def test_continuous_section_updates_column_count(tmp_path: Path) -> None:
    sect = '<w:type w:val="continuous"/><w:cols w:num="3" w:space="360"/>'
    pkg = _make_pkg(
        tmp_path,
        "<w:p><w:r><w:t>본문</w:t></w:r></w:p>"
        f"<w:p><w:pPr><w:sectPr>{sect}</w:sectPr></w:pPr></w:p>"
        "<w:p><w:r><w:t>다음</w:t></w:r></w:p>"
        # 최종 본문 섹션은 1단이라 문단 연속 섹션(3단)이 실제 적용되어야만 3단 페이지가 생긴다
        '<w:sectPr><w:cols w:num="1"/></w:sectPr>',
    )
    pages = extract_docx_pages(pkg)
    assert len(pages) == 2
    assert pages[0].column_count == 3  # 문단 연속 섹션이 적용된 페이지
    assert pages[1].column_count == 1  # 최종 본문 섹션 페이지
