"""무의존성 OOXML 엑셀(`.xlsx`, SpreadsheetML) 파서 단위·통합 테스트."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from autoetl.cli import main
from autoetl.parser import (
    CropStatus,
    ElementKind,
    ParseOptions,
    ParserInputError,
    ParseStrategy,
    SourceFormat,
    XlsxDocumentAdapter,
    get_adapter_for_path,
    inspect_document,
    parse_and_export,
    parse_document,
    supported_extensions,
)
from autoetl.parser.image_io import encode_png


def _make_png(
    width: int = 80, height: int = 50, rgb: tuple[int, int, int] = (40, 120, 190)
) -> bytes:
    return encode_png(bytes(rgb) * (width * height), width, height)


def _build_rich_sample_xlsx(path: Path) -> Path:
    """다중 시트·병합 다중헤더·날짜/퍼센트/천단위 서식·수식·주석·차트·이미지를 담은 `.xlsx` 생성."""
    ns_main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    ns_r = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    ns_rel = "http://schemas.openxmlformats.org/package/2006/relationships"
    ns_xdr = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
    ns_a = "http://schemas.openxmlformats.org/drawingml/2006/main"
    ns_c = "http://schemas.openxmlformats.org/drawingml/2006/chart"

    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<workbook xmlns="{ns_main}" xmlns:r="{ns_r}">'
        '<workbookPr date1904="0"/>'
        "<sheets>"
        '<sheet name="예산총괄" sheetId="1" r:id="rId1"/>'
        '<sheet name="차트및시각" sheetId="2" r:id="rId2"/>'
        '<sheet name="숨김시트" sheetId="3" state="veryHidden" r:id="rId3"/>'
        "</sheets></workbook>"
    )
    wb_rels = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<Relationships xmlns="{ns_rel}">'
        f'<Relationship Id="rId1" Type="{ns_r}/worksheet" Target="worksheets/sheet1.xml"/>'
        f'<Relationship Id="rId2" Type="{ns_r}/worksheet" Target="worksheets/sheet2.xml"/>'
        f'<Relationship Id="rId3" Type="{ns_r}/worksheet" Target="worksheets/sheet3.xml"/>'
        "</Relationships>"
    )

    # sharedStrings (인덱스 0..10, 발음 루비 <rPh> 무시 검증 포함)
    shared_strings = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<sst xmlns="{ns_main}" count="11" uniqueCount="11">'
        "<si><t>2026년도 예산 집행 현황</t></si>"
        "<si><t>구분</t></si>"
        "<si><t>2026년</t></si>"
        "<si><t>상반기</t></si>"
        "<si><t>하반기</t></si>"
        "<si><t>집행률</t></si>"
        "<si><t>기준일</t></si>"
        '<si><t>개발비</t><rPh sb="0" eb="3"><t>개</t></rPh></si>'
        "<si><t>운영비</t></si>"
        "<si><t>단위: 백만원</t></si>"
        "<si><t>비고: 확정안</t></si>"
        "</sst>"
    )

    # styles.xml: xf 0=일반, 1=굵은 제목, 2=천단위, 3=백분율, 4=날짜
    styles_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<styleSheet xmlns="{ns_main}">'
        '<numFmts count="1"><numFmt numFmtId="164" formatCode="yyyy-mm-dd"/></numFmts>'
        '<fonts count="2">'
        '<font><sz val="11"/></font>'
        '<font><b/><sz val="14"/></font>'
        "</fonts>"
        '<borders count="2">'
        "<border/>"
        '<border><left style="thin"/><right style="thin"/>'
        '<top style="thin"/><bottom style="thin"/></border>'
        "</borders>"
        '<cellXfs count="5">'
        '<xf numFmtId="0" fontId="0" borderId="0"/>'
        '<xf numFmtId="0" fontId="1" borderId="0"/>'
        '<xf numFmtId="3" fontId="0" borderId="1"/>'
        '<xf numFmtId="10" fontId="0" borderId="1"/>'
        '<xf numFmtId="164" fontId="0" borderId="1"/>'
        "</cellXfs></styleSheet>"
    )

    # Sheet 1: 상단 제목 배너 + 2행 병합 헤더 표(구분 rowspan=2, 2026년 colspan=2) + 하단 메모
    sheet1_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<worksheet xmlns="{ns_main}" xmlns:r="{ns_r}">'
        "<sheetData>"
        '<row r="1"><c r="A1" t="s" s="1"><v>0</v></c></row>'
        '<row r="2"><c r="A2" t="s" s="0"><v>9</v></c></row>'
        '<row r="4">'
        '<c r="A4" t="s" s="2"><v>1</v></c>'
        '<c r="B4" t="s" s="2"><v>2</v></c>'
        '<c r="D4" t="s" s="2"><v>5</v></c>'
        '<c r="E4" t="s" s="2"><v>6</v></c>'
        "</row>"
        '<row r="5">'
        '<c r="B5" t="s" s="2"><v>3</v></c>'
        '<c r="C5" t="s" s="2"><v>4</v></c>'
        "</row>"
        '<row r="6">'
        '<c r="A6" t="s" s="2"><v>7</v></c>'
        '<c r="B6" s="2"><v>1200</v></c>'
        '<c r="C6" s="2"><v>1800</v></c>'
        '<c r="D6" s="3"><f>B6/(B6+C6)</f><v>0.855</v></c>'
        '<c r="E6" s="4"><v>46023</v></c>'
        "</row>"
        '<row r="7">'
        '<c r="A7" t="s" s="2"><v>8</v></c>'
        '<c r="B7" s="2"><v>450</v></c>'
        '<c r="C7" s="2"><v>550</v></c>'
        '<c r="D7" s="3"><v>1</v></c>'
        '<c r="E7" s="4"><v>46143</v></c>'
        "</row>"
        '<row r="9"><c r="A9" t="s" s="0"><v>10</v></c></row>'
        "</sheetData>"
        '<mergeCells count="4">'
        '<mergeCell ref="A1:E1"/>'
        '<mergeCell ref="A4:A5"/>'
        '<mergeCell ref="B4:C4"/>'
        '<mergeCell ref="D4:D5"/>'
        '<mergeCell ref="E4:E5"/>'
        "</mergeCells>"
        "<headerFooter><oddHeader>&amp;C공공재정 보고서</oddHeader>"
        "<oddFooter>&amp;RAutoETL XLSX</oddFooter></headerFooter>"
        "</worksheet>"
    )
    sheet1_rels = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<Relationships xmlns="{ns_rel}">'
        f'<Relationship Id="rIdC1" Type="{ns_r}/comments" Target="../comments1.xml"/>'
        "</Relationships>"
    )
    comments1_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<comments xmlns="{ns_main}">'
        "<authors><author>김담당</author></authors>"
        "<commentList>"
        '<comment ref="D6" authorId="0"><text><t>집행률 기준치 초과 달성</t></text></comment>'
        "</commentList></comments>"
    )

    # Sheet 2: inlineStr + 불리언 + 내장 차트 + 내장 PNG 이미지
    sheet2_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<worksheet xmlns="{ns_main}" xmlns:r="{ns_r}">'
        "<sheetData>"
        '<row r="1">'
        '<c r="A1" t="inlineStr"><is><t>지표</t></is></c>'
        '<c r="B1" t="inlineStr"><is><t>통과여부</t></is></c>'
        "</row>"
        '<row r="2">'
        '<c r="A2" t="inlineStr"><is><t>품질게이트</t></is></c>'
        '<c r="B2" t="b"><v>1</v></c>'
        "</row>"
        "</sheetData>"
        '<drawing r:id="rIdD1"/>'
        "</worksheet>"
    )
    sheet2_rels = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<Relationships xmlns="{ns_rel}">'
        f'<Relationship Id="rIdD1" Type="{ns_r}/drawing" Target="../drawings/drawing1.xml"/>'
        "</Relationships>"
    )
    drawing1_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<xdr:wsDr xmlns:xdr="{ns_xdr}" xmlns:a="{ns_a}" xmlns:r="{ns_r}" xmlns:c="{ns_c}">'
        "<xdr:twoCellAnchor>"
        '<xdr:pic><xdr:nvPicPr><xdr:cNvPr id="2" name="ChartSnap" descr="대시보드 스냅샷"/>'
        "<xdr:cNvPicPr/></xdr:nvPicPr>"
        '<xdr:blipFill><a:blip r:embed="rIdImg1"/></xdr:blipFill></xdr:pic>'
        "</xdr:twoCellAnchor>"
        "<xdr:twoCellAnchor>"
        "<xdr:graphicFrame><a:graphic><a:graphicData>"
        '<c:chart r:id="rIdCh1"/>'
        "</a:graphicData></a:graphic></xdr:graphicFrame>"
        "</xdr:twoCellAnchor>"
        "</xdr:wsDr>"
    )
    drawing1_rels = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<Relationships xmlns="{ns_rel}">'
        f'<Relationship Id="rIdImg1" Type="{ns_r}/image" Target="../media/image1.png"/>'
        f'<Relationship Id="rIdCh1" Type="{ns_r}/chart" Target="../charts/chart1.xml"/>'
        "</Relationships>"
    )
    chart1_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<c:chartSpace xmlns:c="{ns_c}" xmlns:a="{ns_a}">'
        "<c:chart><c:title><c:tx><c:rich>"
        "<a:p><a:r><a:t>분기별 실적</a:t></a:r></a:p>"
        "</c:rich></c:tx></c:title>"
        "<c:plotArea><c:barChart><c:ser>"
        "<c:tx><c:v>매출</c:v></c:tx>"
        '<c:cat><c:strRef><c:strCache><c:ptCount val="2"/>'
        '<c:pt idx="0"><c:v>Q1</c:v></c:pt><c:pt idx="1"><c:v>Q2</c:v></c:pt>'
        "</c:strCache></c:strRef></c:cat>"
        '<c:val><c:numRef><c:numCache><c:ptCount val="2"/>'
        '<c:pt idx="0"><c:v>100</c:v></c:pt><c:pt idx="1"><c:v>150</c:v></c:pt>'
        "</c:numCache></c:numRef></c:val>"
        "</c:ser></c:barChart></c:plotArea></c:chart></c:chartSpace>"
    )
    sheet3_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<worksheet xmlns="{ns_main}"><sheetData>'
        '<row r="1"><c r="A1" t="inlineStr"><is><t>비밀 시트</t></is></c></row>'
        "</sheetData></worksheet>"
    )
    ns_cp = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
    core_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<cp:coreProperties xmlns:cp="{ns_cp}" xmlns:dc="http://purl.org/dc/elements/1.1/">'
        "<dc:title>2026 재정 워크북</dc:title>"
        "<dc:creator>박재정</dc:creator>"
        "</cp:coreProperties>"
    )

    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("xl/workbook.xml", workbook_xml)
        zf.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        zf.writestr("xl/sharedStrings.xml", shared_strings)
        zf.writestr("xl/styles.xml", styles_xml)
        zf.writestr("xl/worksheets/sheet1.xml", sheet1_xml)
        zf.writestr("xl/worksheets/_rels/sheet1.xml.rels", sheet1_rels)
        zf.writestr("xl/comments1.xml", comments1_xml)
        zf.writestr("xl/worksheets/sheet2.xml", sheet2_xml)
        zf.writestr("xl/worksheets/_rels/sheet2.xml.rels", sheet2_rels)
        zf.writestr("xl/drawings/drawing1.xml", drawing1_xml)
        zf.writestr("xl/drawings/_rels/drawing1.xml.rels", drawing1_rels)
        zf.writestr("xl/charts/chart1.xml", chart1_xml)
        zf.writestr("xl/media/image1.png", _make_png())
        zf.writestr("xl/worksheets/sheet3.xml", sheet3_xml)
        zf.writestr("docProps/core.xml", core_xml)
    return path


def test_xlsx_extension_registered() -> None:
    """`.xlsx` 어댑터가 기본 레지스트리에 등록되어 8대 문서 형식을 지원한다."""
    exts = supported_extensions()
    assert ".xlsx" in exts
    assert len(exts) >= 8
    adapter = get_adapter_for_path(Path("budget.xlsx"))
    assert isinstance(adapter, XlsxDocumentAdapter)
    assert adapter.format == SourceFormat.XLSX


def test_xlsx_rich_workbook_end_to_end(tmp_path: Path) -> None:
    """다중 시트·병합 다중헤더·날짜/퍼센트/천단위·주석·차트·이미지·veryHidden 제외를 검증한다."""
    xlsx_path = _build_rich_sample_xlsx(tmp_path / "budget.xlsx")

    plan = inspect_document(xlsx_path)
    assert plan.document.format == SourceFormat.XLSX
    assert plan.document.title == "2026 재정 워크북"
    assert plan.document.author == "박재정"
    assert plan.document.page_count == 2  # veryHidden 시트 제외
    outline_titles = [o.title for o in plan.outline]
    assert "예산총괄" in outline_titles
    assert "차트및시각" in outline_titles
    assert "숨김시트" not in outline_titles

    out_dir = tmp_path / "out_xlsx"
    doc, manifest = parse_and_export(
        xlsx_path,
        out_dir,
        options=ParseOptions(include_comments=True, extract_crops=True),
    )
    assert manifest.parsed_json.is_file()
    assert manifest.document_markdown.is_file()

    # Sheet 1 검증
    p1 = doc.pages[0]
    assert len(p1.tables) == 1
    tbl = p1.tables[0]
    assert tbl.strategy == ParseStrategy.RULED_TABLE
    assert tbl.has_merged_cells is True
    assert tbl.header_row_count == 2
    assert tbl.headers == ["구분", "2026년 / 상반기", "2026년 / 하반기", "집행률", "기준일"]
    assert tbl.rows[0] == ["개발비", "1,200", "1,800", "85.5%", "2026-01-01"]
    assert tbl.rows[1] == ["운영비", "450", "550", "100%", "2026-05-01"]

    p1_text = "\n".join(b.text for b in p1.blocks)
    assert "2026년도 예산 집행 현황" in p1_text
    assert "비고: 확정안" in p1_text
    assert "김담당" in p1_text and "[D6] 집행률 기준치 초과 달성" in p1_text
    hf_text = " ".join(b.text for b in p1.header_footer_blocks)
    assert "공공재정 보고서" in hf_text
    assert "AutoETL XLSX" in hf_text

    # Sheet 2 검증: 시트 표 + 네이티브 차트 표 승격 + 내장 PNG 이미지
    p2 = doc.pages[1]
    assert len(p2.tables) == 2
    assert p2.tables[0].headers == ["지표", "통과여부"]
    assert p2.tables[0].rows == [["품질게이트", "TRUE"]]
    chart_tbl = p2.tables[1]
    assert chart_tbl.caption is not None and "분기별 실적" in chart_tbl.caption
    assert chart_tbl.rows == [["Q1", "100"], ["Q2", "150"]]
    assert len(p2.visual_regions) == 1
    assert p2.visual_regions[0].element_kind == ElementKind.FIGURE_IMAGE
    assert p2.visual_regions[0].crop_status in (CropStatus.EMBEDDED_EXTRACTED, CropStatus.SAVED)


def test_xlsx_security_and_sheet_selection_guards(tmp_path: Path) -> None:
    """암호화 OLE2 `.xlsx`·비ZIP 파일·범위 초과 시트 선택을 `ParserInputError`로 차단한다."""
    enc = tmp_path / "encrypted.xlsx"
    enc.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 512)
    with pytest.raises(ParserInputError, match="암호화"):
        parse_document(enc)

    bad = tmp_path / "corrupt.xlsx"
    bad.write_bytes(b"NOT-A-ZIP" * 32)
    with pytest.raises(ParserInputError, match="ZIP"):
        parse_document(bad)

    xlsx_path = _build_rich_sample_xlsx(tmp_path / "range.xlsx")
    with pytest.raises(ParserInputError, match="범위"):
        parse_document(xlsx_path, options=ParseOptions(pages=(99,)))

    only_s2 = parse_document(xlsx_path, options=ParseOptions(pages=(2,)))
    assert len(only_s2.pages) == 1
    assert only_s2.pages[0].page == 2


def test_cli_parse_xlsx(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """`autoetl parse` CLI가 `.xlsx`의 `--plan-only`와 `--out` 내보내기를 처리한다."""
    xlsx_path = _build_rich_sample_xlsx(tmp_path / "cli_sheet.xlsx")
    assert main(["parse", str(xlsx_path), "--plan-only"]) == 0
    out_str = capsys.readouterr().out
    assert "xlsx" in out_str.lower()

    out_dir = tmp_path / "cli_xlsx_out"
    assert main(["parse", str(xlsx_path), "--include-comments", "--out", str(out_dir)]) == 0
    capsys.readouterr()
    assert (out_dir / "parsed.json").is_file()
    md = (out_dir / "document.md").read_text(encoding="utf-8")
    assert "2026년 / 상반기" in md
    assert "1,200" in md


def test_format_excel_number_preserves_numeric_precision() -> None:
    """일반/천단위 숫자 포맷 시 작은 비영(nonzero) 값과 고정밀 소수가 손실되지 않는지 검증한다."""
    from autoetl.parser.xlsx.ooxml import XlsxCellStyle, format_excel_number

    assert format_excel_number("1e-10", None) == "1e-10"
    assert format_excel_number("1.0000000005", None) == "1.0000000005"
    assert format_excel_number("0.1234567890123", None) == "0.1234567890123"
    assert format_excel_number("3.141592653589793", None) == "3.141592653589793"
    # IEEE-754 float64 저장 잔차는 정리된다
    assert format_excel_number("0.30000000000000004", None) == "0.3"
    assert format_excel_number("42.0", None) == "42"
    # 천단위·백분율 서식에서도 소수 정밀도 유지
    comma_style = XlsxCellStyle(num_fmt_id=4, fmt_code="#,##0.00")
    assert format_excel_number("1234567.890123456789", comma_style) == "1,234,567.890123456789"
    pct_style = XlsxCellStyle(num_fmt_id=10, fmt_code="0.00%")
    assert format_excel_number("0.155", pct_style) == "15.5%"
    assert format_excel_number("0.000001", pct_style) == "0.0001%"
