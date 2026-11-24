"""무의존성 OOXML DOCX 어댑터(`autoetl.parser.docx`) 단위·통합 테스트."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from autoetl.cli import main
from autoetl.parser import (
    CropStatus,
    DocxDocumentAdapter,
    ElementKind,
    HeuristicRasterLayoutDetector,
    OpenAICompatibleVlmAdapter,
    PageCategory,
    ParseOptions,
    ParserInputError,
    ParseStrategy,
    SourceFormat,
    VisionStatus,
    inspect_document,
    parse_and_export,
    supported_extensions,
)
from autoetl.parser.image_io import encode_png


def _make_solid_png(
    width: int = 80, height: int = 60, rgb: tuple[int, int, int] = (30, 100, 200)
) -> bytes:
    row = bytes(rgb) * width
    return encode_png(row * height, width, height)


def _make_synthetic_scanned_page_png(width: int = 360, height: int = 480) -> bytes:
    """헤더 텍스트·격자 표·컬러 차트가 포함된 합성 스캔 문서 PNG를 생성한다."""
    buf = bytearray(b"\xff\xff\xff" * (width * height))

    def set_px(x: int, y: int, r: int, g: int, b: int) -> None:
        if 0 <= x < width and 0 <= y < height:
            idx = (y * width + x) * 3
            buf[idx] = r
            buf[idx + 1] = g
            buf[idx + 2] = b

    # 1. 상단 제목 줄 (y: 35..58, x: 40..310)
    for y in range(38, 56):
        if y % 5 != 0:
            for x in range(40, 300):
                if (x // 4) % 6 != 0:
                    set_px(x, y, 20, 20, 20)

    # 2. 중단 격자 표 (y: 110..260, x: 35..325)
    t_x0, t_y0, t_x1, t_y1 = 35, 110, 325, 260
    for y in (110, 145, 185, 225, 260):
        for x in range(t_x0, t_x1 + 1):
            set_px(x, y, 15, 15, 15)
            set_px(x, min(height - 1, y + 1), 15, 15, 15)
    for x in (35, 130, 225, 325):
        for y in range(t_y0, t_y1 + 1):
            set_px(x, y, 15, 15, 15)
            set_px(min(width - 1, x + 1), y, 15, 15, 15)
    # 셀 내부 텍스트 패턴
    for ry in (122, 160, 200, 238):
        for cx in (50, 145, 240):
            for y in range(ry, ry + 8):
                for x in range(cx, cx + 55):
                    if (x + y) % 3 != 0:
                        set_px(x, y, 30, 30, 30)

    # 3. 하단 컬러 막대 차트 (y: 310..440, x: 45..315)
    bars = [
        (65, 350, 110, 430, (220, 60, 50)),
        (135, 330, 180, 430, (40, 130, 220)),
        (205, 370, 250, 430, (50, 180, 90)),
    ]
    for bx0, by0, bx1, by1, (r, g, b) in bars:
        for y in range(by0, by1):
            for x in range(bx0, bx1):
                set_px(x, y, r, g, b)

    return encode_png(bytes(buf), width, height)


def _build_rich_sample_docx(target_path: Path) -> Path:
    """4페이지 분량의 복합 기능을 담은 표준 OOXML `.docx` 테스트 문서를 생성한다."""
    fig_png = _make_solid_png(120, 80, (40, 120, 210))
    scan_png = _make_synthetic_scanned_page_png(360, 480)

    content_types = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels"
    ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Default Extension="png" ContentType="image/png"/>
  <Override PartName="/word/document.xml"
    ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/docProps/core.xml"
    ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
</Types>"""

    root_rels = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
    Target="word/document.xml"/>
  <Relationship Id="rId2"
    Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties"
    Target="docProps/core.xml"/>
</Relationships>"""

    core_xml = """<?xml version="1.0" encoding="UTF-8"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
                   xmlns:dc="http://purl.org/dc/elements/1.1/">
  <dc:title>AutoETL DOCX 심층 파서 검증 보고서</dc:title>
  <dc:creator>AutoETL Test Suite</dc:creator>
</cp:coreProperties>"""

    styles_xml = """<?xml version="1.0" encoding="UTF-8"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:docDefaults>
    <w:rPrDefault>
      <w:rPr>
        <w:sz w:val="22"/>
      </w:rPr>
    </w:rPrDefault>
  </w:docDefaults>
  <w:style w:type="paragraph" w:styleId="Normal">
    <w:name w:val="Normal"/>
    <w:rPr><w:sz w:val="22"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading1">
    <w:name w:val="heading 1"/>
    <w:basedOn w:val="Normal"/>
    <w:pPr><w:outlineLvl w:val="0"/></w:pPr>
    <w:rPr><w:b/><w:sz w:val="36"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="CustomSubHeading">
    <w:name w:val="Custom SubTitle"/>
    <w:basedOn w:val="Heading1"/>
    <w:pPr><w:outlineLvl w:val="1"/></w:pPr>
    <w:rPr><w:b/><w:sz w:val="28"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="CodeBlock">
    <w:name w:val="Source Code"/>
    <w:basedOn w:val="Normal"/>
    <w:rPr>
      <w:rFonts w:ascii="Consolas" w:hAnsi="Consolas"/>
      <w:sz w:val="20"/>
    </w:rPr>
  </w:style>
</w:styles>"""

    numbering_xml = """<?xml version="1.0" encoding="UTF-8"?>
<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:abstractNum w:abstractNumId="0">
    <w:lvl w:ilvl="0">
      <w:numFmt w:val="bullet"/>
      <w:lvlText w:val="•"/>
    </w:lvl>
  </w:abstractNum>
  <w:abstractNum w:abstractNumId="1">
    <w:lvl w:ilvl="0">
      <w:start w:val="1"/>
      <w:numFmt w:val="decimal"/>
      <w:lvlText w:val="%1."/>
    </w:lvl>
  </w:abstractNum>
  <w:num w:numId="1">
    <w:abstractNumId w:val="0"/>
  </w:num>
  <w:num w:numId="2">
    <w:abstractNumId w:val="1"/>
  </w:num>
</w:numbering>"""

    header1_xml = """<?xml version="1.0" encoding="UTF-8"?>
<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:p><w:r><w:t>AutoETL 기술 백서 — 대외비</w:t></w:r></w:p>
</w:hdr>"""

    footer1_xml = """<?xml version="1.0" encoding="UTF-8"?>
<w:ftr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:p><w:r><w:t>Copyright 2026 AutoETL Team</w:t></w:r></w:p>
</w:ftr>"""

    footnotes_xml = """<?xml version="1.0" encoding="UTF-8"?>
<w:footnotes xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:footnote w:type="separator" w:id="-1">
    <w:p><w:r><w:separator/></w:r></w:p>
  </w:footnote>
  <w:footnote w:id="1">
    <w:p><w:r><w:t>OOXML(ISO/IEC 29500) 표준 문서 구조 정의를 따른다.</w:t></w:r></w:p>
  </w:footnote>
</w:footnotes>"""

    endnotes_xml = """<?xml version="1.0" encoding="UTF-8"?>
<w:endnotes xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:endnote w:type="separator" w:id="0">
    <w:p><w:r><w:separator/></w:r></w:p>
  </w:endnote>
  <w:endnote w:id="1">
    <w:p><w:r><w:t>벤치마크 세부 수치는 부록 A를 참조한다.</w:t></w:r></w:p>
  </w:endnote>
</w:endnotes>"""

    comments_xml = """<?xml version="1.0" encoding="UTF-8"?>
<w:comments xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:comment w:id="0" w:author="김리드" w:date="2026-11-23T09:00:00Z">
    <w:p><w:r><w:t>1분기 성장률 목표치 최종 확인 완료.</w:t></w:r></w:p>
  </w:comment>
</w:comments>"""

    chart1_xml = """<?xml version="1.0" encoding="UTF-8"?>
<c:chartSpace xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart"
              xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
  <c:chart>
    <c:title>
      <c:tx><c:rich><a:p><a:r><a:t>연도별 처리량 추이</a:t></a:r></a:p></c:rich></c:tx>
    </c:title>
    <c:plotArea>
      <c:barChart>
        <c:ser>
          <c:tx><c:v>문서수(만건)</c:v></c:tx>
          <c:cat>
            <c:strRef><c:strCache>
              <c:pt idx="0"><c:v>2024</c:v></c:pt>
              <c:pt idx="1"><c:v>2025</c:v></c:pt>
            </c:strCache></c:strRef>
          </c:cat>
          <c:val>
            <c:numRef><c:numCache>
              <c:pt idx="0"><c:v>120</c:v></c:pt>
              <c:pt idx="1"><c:v>280</c:v></c:pt>
            </c:numCache></c:numRef>
          </c:val>
        </c:ser>
      </c:barChart>
    </c:plotArea>
  </c:chart>
</c:chartSpace>"""

    chartex1_xml = """<?xml version="1.0" encoding="UTF-8"?>
<cx:chartSpace xmlns:cx="http://schemas.microsoft.com/office/drawing/2014/chartex"
               xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
  <cx:chartData>
    <cx:data id="0">
      <cx:strDim type="cat">
        <cx:lvl ptCount="2">
          <cx:pt idx="0">기초매출</cx:pt>
          <cx:pt idx="1">신규수주</cx:pt>
        </cx:lvl>
      </cx:strDim>
      <cx:numDim type="val">
        <cx:lvl ptCount="2">
          <cx:pt idx="0">500</cx:pt>
          <cx:pt idx="1">180</cx:pt>
        </cx:lvl>
      </cx:numDim>
    </cx:data>
  </cx:chartData>
  <cx:chart>
    <cx:title><cx:tx><cx:txData><cx:v>매출 증감 폭포 차트</cx:v></cx:txData></cx:tx></cx:title>
    <cx:plotArea>
      <cx:plotAreaRegion>
        <cx:series layoutId="waterfall" dataId="0">
          <cx:tx><cx:txData><cx:v>금액(억원)</cx:v></cx:txData></cx:tx>
        </cx:series>
      </cx:plotAreaRegion>
    </cx:plotArea>
  </cx:chart>
</cx:chartSpace>"""

    diagram_data1_xml = """<?xml version="1.0" encoding="UTF-8"?>
<dgm:dataModel xmlns:dgm="http://schemas.openxmlformats.org/drawingml/2006/diagram"
               xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
  <dgm:ptLst>
    <dgm:pt modelId="0" type="doc"/>
    <dgm:pt modelId="1"><dgm:t><a:p><a:r><a:t>수집 단계</a:t></a:r></a:p></dgm:t></dgm:pt>
    <dgm:pt modelId="2"><dgm:t><a:p><a:r><a:t>구조 라우팅</a:t></a:r></a:p></dgm:t></dgm:pt>
    <dgm:pt modelId="3"><dgm:t><a:p><a:r><a:t>통합 IR 생성</a:t></a:r></a:p></dgm:t></dgm:pt>
  </dgm:ptLst>
  <dgm:cxnLst>
    <dgm:cxn modelId="c1" srcId="0" destId="1" srcOrd="0" destOrd="0"/>
    <dgm:cxn modelId="c2" srcId="0" destId="2" srcOrd="1" destOrd="0"/>
    <dgm:cxn modelId="c3" srcId="2" destId="3" srcOrd="0" destOrd="0"/>
  </dgm:cxnLst>
</dgm:dataModel>"""

    doc_rels = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rIdStyles"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles"
    Target="styles.xml"/>
  <Relationship Id="rIdNum"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering"
    Target="numbering.xml"/>
  <Relationship Id="rIdHdr1"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/header"
    Target="header1.xml"/>
  <Relationship Id="rIdFtr1"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer"
    Target="footer1.xml"/>
  <Relationship Id="rIdChart1"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/chart"
    Target="charts/chart1.xml"/>
  <Relationship Id="rIdChartEx1"
    Type="http://schemas.microsoft.com/office/2014/relationships/chartEx"
    Target="charts/chartEx1.xml"/>
  <Relationship Id="rIdDgm1"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/diagramData"
    Target="diagrams/data1.xml"/>
  <Relationship Id="rIdImg1"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
    Target="media/image1.png"/>
  <Relationship Id="rIdScan1"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
    Target="media/scan_page.png"/>
  <Relationship Id="rIdLink1"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"
    Target="https://example.org/spec" TargetMode="External"/>
</Relationships>"""

    document_xml = """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
            xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
            xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math"
            xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
            xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
            xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"
            xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart"
            xmlns:cx="http://schemas.microsoft.com/office/drawing/2014/chartex"
            xmlns:dgm="http://schemas.openxmlformats.org/drawingml/2006/diagram"
            xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"
            xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape"
            xmlns:v="urn:schemas-microsoft-com:vml">
  <w:body>
    <!-- ================= PAGE 1: DIGITAL_TEXT ================= -->
    <w:p>
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>1. 연구 배경 및 목적</w:t></w:r>
    </w:p>
    <w:p>
      <w:pPr><w:pStyle w:val="CustomSubHeading"/></w:pPr>
      <w:r><w:t>1.1 세부 목표 및 변경 추적</w:t></w:r>
    </w:p>
    <w:p>
      <w:r><w:t>본 문서는 </w:t></w:r>
      <w:del><w:r><w:delText>폐기된 구형 초안 문구</w:delText></w:r></w:del>
      <w:ins><w:r><w:t>최신 OOXML 파서 아키텍처</w:t></w:r></w:ins>
      <w:r><w:t>를 정의한다</w:t></w:r>
      <w:r><w:footnoteReference w:id="1"/></w:r>
      <w:r><w:t>. 상세 성능은 </w:t></w:r>
      <w:hyperlink r:id="rIdLink1"><w:r><w:t>공식 명세서</w:t></w:r></w:hyperlink>
      <w:r><w:t>를 따른다</w:t></w:r>
      <w:r><w:endnoteReference w:id="1"/></w:r>
      <w:r><w:commentReference w:id="0"/></w:r>
      <w:r><w:t>.</w:t></w:r>
      <w:r><w:fldChar w:fldCharType="begin"/></w:r>
      <w:r><w:instrText> HYPERLINK "https://hidden.internal" </w:instrText></w:r>
      <w:r><w:fldChar w:fldCharType="separate"/></w:r>
      <w:r><w:fldChar w:fldCharType="end"/></w:r>
    </w:p>
    <w:sdt>
      <w:sdtContent>
        <w:p>
          <w:r><w:t>콘텐츠 컨트롤(SDT) 내부의 핵심 본문 문단입니다.</w:t></w:r>
        </w:p>
      </w:sdtContent>
    </w:sdt>
    <w:p>
      <w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr>
      <w:r><w:t>무의존성 표준 라이브러리 파싱</w:t></w:r>
    </w:p>
    <w:p>
      <w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="2"/></w:numPr></w:pPr>
      <w:r><w:t>첫 번째 실행 단계</w:t></w:r>
    </w:p>
    <w:p>
      <w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="2"/></w:numPr></w:pPr>
      <w:r><w:t>두 번째 검증 단계</w:t></w:r>
    </w:p>
    <w:p>
      <m:oMathPara>
        <m:oMath>
          <m:r><m:t>E=</m:t></m:r>
          <m:f>
            <m:num><m:sSup><m:e><m:r><m:t>mc</m:t></m:r></m:e><m:sup><m:r><m:t>2</m:t></m:r></m:sup></m:sSup></m:num>
            <m:den><m:rad><m:e><m:r><m:t>1-v</m:t></m:r></m:e></m:rad></m:den>
          </m:f>
        </m:oMath>
      </m:oMathPara>
    </w:p>
    <w:p>
      <w:pPr><w:pStyle w:val="CodeBlock"/></w:pPr>
      <w:r><w:t>uv run autoetl parse sample.docx --out ./out</w:t></w:r>
    </w:p>
    <w:p>
      <w:r><w:br w:type="page"/></w:r>
    </w:p>

    <!-- ================= PAGE 2: TABLE_HEAVY ================= -->
    <w:p>
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>2. 실적 표 및 차트 분석</w:t></w:r>
    </w:p>
    <w:p>
      <w:r><w:t>표 1. 분기별 부문 실적 요약</w:t></w:r>
    </w:p>
    <w:tbl>
      <w:tblPr>
        <w:tblBorders>
          <w:top w:val="single" w:sz="4"/>
          <w:bottom w:val="single" w:sz="4"/>
          <w:left w:val="single" w:sz="4"/>
          <w:right w:val="single" w:sz="4"/>
          <w:insideH w:val="single" w:sz="4"/>
          <w:insideV w:val="single" w:sz="4"/>
        </w:tblBorders>
      </w:tblPr>
      <w:tblGrid>
        <w:gridCol w:w="2400"/>
        <w:gridCol w:w="2400"/>
        <w:gridCol w:w="2400"/>
        <w:gridCol w:w="400"/>
      </w:tblGrid>
      <w:tr>
        <w:trPr><w:tblHeader/></w:trPr>
        <w:tc>
          <w:tcPr><w:vMerge w:val="restart"/></w:tcPr>
          <w:p><w:r><w:t>부문</w:t></w:r></w:p>
        </w:tc>
        <w:tc>
          <w:tcPr><w:gridSpan w:val="2"/></w:tcPr>
          <w:p><w:r><w:t>상반기 실적</w:t></w:r></w:p>
        </w:tc>
        <w:tc><w:p/></w:tc>
      </w:tr>
      <w:tr>
        <w:trPr><w:tblHeader/></w:trPr>
        <w:tc>
          <w:tcPr><w:vMerge/></w:tcPr>
          <w:p/>
        </w:tc>
        <w:tc><w:p><w:r><w:t>1분기</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>2분기</w:t></w:r></w:p></w:tc>
        <w:tc><w:p/></w:tc>
      </w:tr>
      <w:tr>
        <w:tc><w:p><w:r><w:t>플랫폼</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>120억</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>150억</w:t></w:r></w:p></w:tc>
        <w:tc><w:p/></w:tc>
      </w:tr>
    </w:tbl>
    <w:tbl>
      <w:tblPr>
        <w:tblBorders>
          <w:top w:val="single" w:sz="8"/>
          <w:bottom w:val="single" w:sz="8"/>
          <w:insideH w:val="single" w:sz="4"/>
          <w:left w:val="none"/>
          <w:right w:val="none"/>
          <w:insideV w:val="none"/>
        </w:tblBorders>
      </w:tblPr>
      <w:tblGrid>
        <w:gridCol w:w="3000"/>
        <w:gridCol w:w="3000"/>
      </w:tblGrid>
      <w:tr>
        <w:tc><w:p><w:r><w:t>지표</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>결과</w:t></w:r></w:p></w:tc>
      </w:tr>
      <w:tr>
        <w:tc><w:p><w:r><w:t>정확도</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>99.4%</w:t></w:r></w:p></w:tc>
      </w:tr>
    </w:tbl>
    <w:p>
      <w:r>
        <w:drawing>
          <wp:inline>
            <wp:extent cx="3657600" cy="2011680"/>
            <a:graphic>
              <a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/chart">
                <c:chart r:id="rIdChart1"/>
              </a:graphicData>
            </a:graphic>
          </wp:inline>
        </w:drawing>
      </w:r>
    </w:p>
    <w:p>
      <w:r>
        <w:drawing>
          <wp:inline>
            <wp:extent cx="3657600" cy="1828800"/>
            <a:graphic>
              <a:graphicData uri="http://schemas.microsoft.com/office/drawing/2014/chartex">
                <cx:chart r:id="rIdChartEx1"/>
              </a:graphicData>
            </a:graphic>
          </wp:inline>
        </w:drawing>
      </w:r>
    </w:p>
    <w:p>
      <w:pPr>
        <w:sectPr>
          <w:headerReference w:type="default" r:id="rIdHdr1"/>
          <w:footerReference w:type="default" r:id="rIdFtr1"/>
          <w:pgSz w:w="11906" w:h="16838"/>
          <w:pgMar w:top="1440" w:bottom="1440" w:left="1440" w:right="1440"/>
        </w:sectPr>
      </w:pPr>
    </w:p>

    <!-- ======== PAGE 3: 2-COLUMN + FIGURE + SMARTART + MC:ALTERNATECONTENT ======== -->
    <w:p>
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>3. 다단 레이아웃 및 도식 검증</w:t></w:r>
    </w:p>
    <w:p>
      <w:r>
        <w:drawing>
          <wp:inline>
            <wp:extent cx="2743200" cy="1828800"/>
            <wp:docPr id="10" name="Picture 1" descr="시스템 아키텍처 다이어그램"/>
            <a:graphic>
              <a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">
                <pic:pic>
                  <pic:blipFill><a:blip r:embed="rIdImg1"/></pic:blipFill>
                </pic:pic>
              </a:graphicData>
            </a:graphic>
          </wp:inline>
        </w:drawing>
      </w:r>
    </w:p>
    <w:p>
      <w:r><w:t>그림 1. 시스템 아키텍처 개요</w:t></w:r>
    </w:p>
    <w:p>
      <w:r>
        <mc:AlternateContent>
          <mc:Choice Requires="wps">
            <w:drawing>
              <wp:inline>
                <wp:extent cx="2286000" cy="914400"/>
                <a:graphic>
                  <a:graphicData uri="http://schemas.microsoft.com/office/word/2010/wordprocessingShape">
                    <wps:wsp>
                      <wps:txbx>
                        <w:txbxContent>
                          <w:p><w:r><w:t>모던 텍스트상자 단일 추출 검증</w:t></w:r></w:p>
                        </w:txbxContent>
                      </wps:txbx>
                    </wps:wsp>
                  </a:graphicData>
                </a:graphic>
              </wp:inline>
            </w:drawing>
          </mc:Choice>
          <mc:Fallback>
            <w:pict>
              <v:shape>
                <v:textbox>
                  <w:txbxContent>
                    <w:p><w:r><w:t>모던 텍스트상자 단일 추출 검증</w:t></w:r></w:p>
                  </w:txbxContent>
                </v:textbox>
              </v:shape>
            </w:pict>
          </mc:Fallback>
        </mc:AlternateContent>
      </w:r>
    </w:p>
    <w:p>
      <w:r>
        <w:drawing>
          <wp:inline>
            <wp:extent cx="3200400" cy="1600200"/>
            <a:graphic>
              <a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/diagram">
                <dgm:relIds r:dm="rIdDgm1"/>
              </a:graphicData>
            </a:graphic>
          </wp:inline>
        </w:drawing>
      </w:r>
    </w:p>
    <w:p>
      <w:pPr>
        <w:sectPr>
          <w:pgSz w:w="11906" w:h="16838"/>
          <w:pgMar w:top="1440" w:bottom="1440" w:left="1440" w:right="1440"/>
          <w:cols w:num="2" w:space="720"/>
        </w:sectPr>
      </w:pPr>
    </w:p>

    <!-- ================= PAGE 4: SCANNED_IMAGE ================= -->
    <w:p>
      <w:r>
        <w:drawing>
          <wp:inline>
            <wp:extent cx="5486400" cy="7315200"/>
            <wp:docPr id="20" name="Scanned Page"/>
            <a:graphic>
              <a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">
                <pic:pic>
                  <pic:blipFill><a:blip r:embed="rIdScan1"/></pic:blipFill>
                </pic:pic>
              </a:graphicData>
            </a:graphic>
          </wp:inline>
        </w:drawing>
      </w:r>
    </w:p>
    <w:sectPr>
      <w:pgSz w:w="11906" w:h="16838"/>
      <w:pgMar w:top="1440" w:bottom="1440" w:left="1440" w:right="1440"/>
    </w:sectPr>
  </w:body>
</w:document>"""

    with zipfile.ZipFile(target_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", root_rels)
        zf.writestr("docProps/core.xml", core_xml)
        zf.writestr("word/styles.xml", styles_xml)
        zf.writestr("word/numbering.xml", numbering_xml)
        zf.writestr("word/header1.xml", header1_xml)
        zf.writestr("word/footer1.xml", footer1_xml)
        zf.writestr("word/footnotes.xml", footnotes_xml)
        zf.writestr("word/endnotes.xml", endnotes_xml)
        zf.writestr("word/comments.xml", comments_xml)
        zf.writestr("word/charts/chart1.xml", chart1_xml)
        zf.writestr("word/charts/chartEx1.xml", chartex1_xml)
        zf.writestr("word/diagrams/data1.xml", diagram_data1_xml)
        zf.writestr("word/media/image1.png", fig_png)
        zf.writestr("word/media/scan_page.png", scan_png)
        zf.writestr("word/_rels/document.xml.rels", doc_rels)
        zf.writestr("word/document.xml", document_xml)

    return target_path


def test_docx_adapter_registration_and_error_handling(tmp_path: Path) -> None:
    """DOCX 어댑터 기본 등록 및 구형 .doc/암호화/손상 파일 예외 처리를 검증한다."""
    assert ".docx" in supported_extensions()
    adapter = DocxDocumentAdapter()
    assert adapter.format == SourceFormat.DOCX
    assert adapter.extensions == (".docx",)

    # 1. 존재하지 않는 파일
    with pytest.raises(ParserInputError, match="존재하지 않는다"):
        inspect_document(tmp_path / "missing.docx")

    # 2. OLE2 매직 바이트(구형 .doc 또는 암호화된 .docx)
    ole_file = tmp_path / "encrypted.docx"
    ole_file.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64)
    with pytest.raises(ParserInputError, match="OLE"):
        inspect_document(ole_file)

    # 3. 손상된 ZIP 파일
    bad_zip = tmp_path / "corrupt.docx"
    bad_zip.write_bytes(b"not-a-zip-archive")
    with pytest.raises(ParserInputError, match="ZIP"):
        inspect_document(bad_zip)

    # 4. word/document.xml이 없는 ZIP
    empty_zip = tmp_path / "no_doc.docx"
    with zipfile.ZipFile(empty_zip, "w") as zf:
        zf.writestr("hello.txt", "world")
    with pytest.raises(ParserInputError, match=r"word/document\.xml"):
        inspect_document(empty_zip)

    # 5. 범위를 벗어난 페이지 요청
    valid_docx = _build_rich_sample_docx(tmp_path / "sample.docx")
    with pytest.raises(ParserInputError, match="범위"):
        inspect_document(valid_docx, options=ParseOptions(pages=(99,)))


def test_docx_rich_document_inspection_and_parsing(tmp_path: Path) -> None:
    """스타일 상속·목록·변경추적·수식·각주·주석·병합표·차트·다단·스캔 페이지를 종합 검증한다."""
    docx_path = _build_rich_sample_docx(tmp_path / "rich.docx")
    out_dir = tmp_path / "parsed_docx"

    plan = inspect_document(docx_path, options=ParseOptions(include_comments=True))
    assert plan.document.format == SourceFormat.DOCX
    assert plan.document.title == "AutoETL DOCX 심층 파서 검증 보고서"
    assert plan.document.author == "AutoETL Test Suite"
    assert plan.summary.total_pages == 4
    assert plan.summary.analyzed_pages == 4

    # 페이지별 카테고리 확인
    p_cats = {p.page: p.category for p in plan.pages}
    assert p_cats[1] == PageCategory.DIGITAL_TEXT
    assert p_cats[2] == PageCategory.TABLE_HEAVY
    assert p_cats[4] == PageCategory.SCANNED_IMAGE

    # 목차(Outline) 확인 (Heading1 L1 + CustomSubHeading basedOn Heading1 -> L2)
    outline_titles = [(o.level, o.title, o.page) for o in plan.outline]
    assert (1, "1. 연구 배경 및 목적", 1) in outline_titles
    assert (2, "1.1 세부 목표 및 변경 추적", 1) in outline_titles
    assert (1, "2. 실적 표 및 차트 분석", 2) in outline_titles
    assert (1, "3. 다단 레이아웃 및 도식 검증", 3) in outline_titles

    # 전체 파싱 및 번들 내보내기
    doc, manifest = parse_and_export(
        docx_path,
        out_dir,
        options=ParseOptions(include_comments=True),
    )
    assert manifest.parsed_json.is_file()
    assert manifest.document_markdown.is_file()
    assert manifest.routing_report_markdown.is_file()

    # --- Page 1 검증: 변경추적, 하이퍼링크, SDT, 목록, 수식, 코드, 각주/미주/주석 ---
    p1 = doc.pages[0]
    p1_texts = [b.text for b in p1.blocks]
    joined_p1 = "\n".join(p1_texts)

    # 삭제된 텍스트(<w:del>)와 필드 명령(<w:instrText>)은 제외되고 삽입 텍스트는 보존
    assert "폐기된 구형 초안 문구" not in joined_p1
    assert "hidden.internal" not in joined_p1
    assert "최신 OOXML 파서 아키텍처" in joined_p1
    assert "공식 명세서" in joined_p1
    assert "콘텐츠 컨트롤(SDT) 내부의 핵심 본문 문단입니다." in joined_p1

    # 글머리표 및 번호 매기기 목록
    assert any(
        b.kind == ElementKind.LIST_ITEM and "무의존성 표준 라이브러리 파싱" in b.text
        for b in p1.blocks
    )
    assert any(
        b.kind == ElementKind.LIST_ITEM and "1. 첫 번째 실행 단계" in b.text for b in p1.blocks
    )
    assert any(
        b.kind == ElementKind.LIST_ITEM and "2. 두 번째 검증 단계" in b.text for b in p1.blocks
    )

    # Office Math 수식 및 코드 블록
    formula_blocks = [b for b in p1.blocks if b.kind == ElementKind.FORMULA_OR_CODE]
    assert len(formula_blocks) == 2
    assert any("mc^2" in b.text and "sqrt(1-v)" in b.text for b in formula_blocks)
    assert any("uv run autoetl parse sample.docx" in b.text for b in formula_blocks)

    # 각주·미주·주석 및 머리글·바닥글
    assert any("[각주 1]" in b.text and "ISO/IEC 29500" in b.text for b in p1.blocks)
    assert any("[미주 1]" in b.text and "부록 A" in b.text for b in p1.blocks)
    assert any(
        "[문서 주석]" in b.text and "김리드" in b.text and "1분기 성장률 목표치" in b.text
        for b in p1.blocks
    )
    assert any("AutoETL 기술 백서" in b.text for b in p1.header_footer_blocks)
    assert any("Copyright 2026" in b.text for b in p1.header_footer_blocks)

    # --- Page 2 검증: 병합 표(gridSpan + vMerge + 빈 열 압축), 가로선 표, 차트 2종 ---
    p2 = doc.pages[1]
    assert len(p2.tables) == 4  # 네이티브 표 2개 + c:chart 1개 + cx:chart 1개

    merged_tbl = p2.tables[0]
    assert merged_tbl.caption == "표 1. 분기별 부문 실적 요약"
    assert merged_tbl.strategy == ParseStrategy.RULED_TABLE
    # 끝에 있던 100% 빈 패딩 열(4번째 gridCol)이 자동 압축되어 3열이어야 함
    assert merged_tbl.col_count == 3
    assert merged_tbl.row_count == 3
    assert merged_tbl.headers == ["부문", "상반기 실적", ""]
    assert merged_tbl.rows[0] == ["", "1분기", "2분기"]
    assert merged_tbl.rows[1] == ["플랫폼", "120억", "150억"]
    # 첫 행 첫 셀(부문)의 vMerge row_span == 2, 두 번째 셀(상반기 실적)의 col_span == 2 확인
    r0c0 = next(c for c in merged_tbl.cells if c.row == 0 and c.col == 0)
    r0c1 = next(c for c in merged_tbl.cells if c.row == 0 and c.col == 1)
    assert r0c0.row_span == 2
    assert r0c1.col_span == 2

    booktabs_tbl = p2.tables[1]
    assert booktabs_tbl.strategy == ParseStrategy.HORIZONTAL_RULED_TABLE
    assert booktabs_tbl.headers == ["지표", "결과"]
    assert booktabs_tbl.rows == [["정확도", "99.4%"]]

    chart_tbl = p2.tables[2]
    assert chart_tbl.caption is not None and "연도별 처리량 추이" in chart_tbl.caption
    assert chart_tbl.headers == ["항목", "문서수(만건)"]
    assert chart_tbl.rows[0] == ["2024", "120"]

    chartex_tbl = p2.tables[3]
    assert chartex_tbl.caption is not None and "매출 증감 폭포 차트" in chartex_tbl.caption
    assert chartex_tbl.rows[0] == ["기초매출", "500"]

    # --- Page 3 검증: 2단 섹션, 그림+캡션, mc:AlternateContent 중복 제거, SmartArt ---
    p3 = doc.pages[2]
    assert p3.route.metrics.column_count == 2
    p3_texts = [b.text for b in p3.blocks]
    # mc:AlternateContent의 Choice와 Fallback이 중복 추출되지 않고 딱 1번만 나와야 함
    assert sum(1 for t in p3_texts if "모던 텍스트상자 단일 추출 검증" in t) == 1

    fig_vis = next(v for v in p3.visual_regions if v.element_kind == ElementKind.FIGURE_IMAGE)
    assert fig_vis.caption == "그림 1. 시스템 아키텍처 개요"
    assert fig_vis.crop_status in (CropStatus.EMBEDDED_EXTRACTED, CropStatus.SAVED)

    # SmartArt 항목이 리스트 블록으로 추출되었는지 확인
    assert any("수집 단계" in t for t in p3_texts)
    assert any("구조 라우팅" in t for t in p3_texts)
    assert any("통합 IR 생성" in t for t in p3_texts)


def test_docx_scanned_page_layout_detector_and_vlm_pipeline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DOCX 내 전체 스캔 이미지 페이지에 레이아웃 탐지기와 VLM을 연동해 크롭·해석을 검증한다."""
    docx_path = _build_rich_sample_docx(tmp_path / "scanned_docx.docx")
    detector = HeuristicRasterLayoutDetector()

    # 4페이지만 선택하여 레이아웃 탐지 실행
    plan = inspect_document(
        docx_path,
        options=ParseOptions(pages=(4,)),
        layout_detector=detector,
    )
    assert len(plan.pages) == 1
    p4_route = plan.pages[0]
    assert p4_route.category == PageCategory.SCANNED_IMAGE
    assert any(f"레이아웃 탐지({detector.name})" in r for r in p4_route.reasons)
    assert len(p4_route.regions) >= 2

    # 모의 PaddleOCR-VL 응답을 돌려주는 OpenAICompatibleVlmAdapter 연결
    captured_prompts: list[str] = []

    def fake_open_json(
        url: str,
        *,
        zone: object,
        payload: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
        timeout_sec: float = 90.0,
    ) -> dict[str, object]:
        assert payload is not None
        msgs = payload["messages"]
        assert isinstance(msgs, list)
        user_content = msgs[-1]["content"]
        prompt_text = next(c["text"] for c in user_content if c["type"] == "text")
        captured_prompts.append(prompt_text)
        content = f"[VLM 해석 완료: {prompt_text}] | 항목 | 값 |\n| --- | --- |\n| A | 100 |"
        return {"choices": [{"message": {"content": content}}]}

    monkeypatch.setattr("autoetl.parser.vision._open_json_request", fake_open_json)
    vlm = OpenAICompatibleVlmAdapter(
        model="PaddleOCR-VL-1.6",
        base_url="http://127.0.0.1:8080/v1",
    )

    out_dir = tmp_path / "out_scan"
    doc, manifest = parse_and_export(
        docx_path,
        out_dir,
        options=ParseOptions(pages=(4,)),
        layout_detector=detector,
        vision_adapter=vlm,
    )
    assert len(doc.pages) == 1
    p4 = doc.pages[0]
    assert len(p4.visual_regions) >= 2
    assert all(
        v.crop_status in (CropStatus.EMBEDDED_EXTRACTED, CropStatus.SAVED)
        for v in p4.visual_regions
    )
    assert all(v.vision_status == VisionStatus.COMPLETED for v in p4.visual_regions)
    assert len(manifest.crop_paths) == len(p4.visual_regions)
    assert captured_prompts


def test_cli_parse_docx_plan_and_export(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`autoetl parse` CLI가 `.docx`의 `--plan-only` 및 `--include-comments --out`을 처리한다."""
    docx_path = _build_rich_sample_docx(tmp_path / "cli_test.docx")

    rc = main(["parse", str(docx_path), "--plan-only"])
    assert rc == 0
    out_str = capsys.readouterr().out
    assert "AutoETL DOCX 심층 파서 검증 보고서" in out_str
    assert "docx" in out_str.lower()

    bundle_dir = tmp_path / "cli_bundle"
    rc2 = main(
        [
            "parse",
            str(docx_path),
            "--pages",
            "1-2",
            "--include-comments",
            "--out",
            str(bundle_dir),
        ]
    )
    assert rc2 == 0
    assert (bundle_dir / "parsed.json").is_file()
    assert (bundle_dir / "document.md").is_file()
    md_text = (bundle_dir / "document.md").read_text(encoding="utf-8")
    assert "1. 연구 배경 및 목적" in md_text
    assert "김리드" in md_text
    assert "상반기 실적" in md_text
    assert "플랫폼" in md_text


def test_docx_real_world_edge_cases(tmp_path: Path) -> None:
    """실제 공개 코퍼스에서 발견된 7대 엣지 케이스(대체 경로·Strict OOXML 등)를 검증한다."""
    fig_png = _make_solid_png(100, 60, (60, 140, 80))

    # 1) 비표준 본문 경로(/word/document2.xml) + Strict OOXML 네임스페이스 + UniversalMeasure(pt)
    # 2) startOverride(10번부터 시작) + lowerLetter 하위 목록(a., b.)
    # 3) 레거시 양식 체크박스(ffData/checkBox) 및 모던 SDT 체크박스(w14:checkbox)
    # 4) 그림 직후·표 직전의 `<그림 1>` 캡션이 표가 아닌 그림에 정확히 매칭
    # 5) 단일 <w:tr> 내부 병렬 다중 줄(K=3) 행 분할 + 하단 빈 여백 행 제거
    rels_xml = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1"
    Type="http://purl.oclc.org/ooxml/officeDocument/relationships/officeDocument"
    Target="/word/document2.xml"/>
</Relationships>"""

    doc2_rels = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rIdNum"
    Type="http://purl.oclc.org/ooxml/officeDocument/relationships/numbering"
    Target="numbering.xml"/>
  <Relationship Id="rIdImg1"
    Type="http://purl.oclc.org/ooxml/officeDocument/relationships/image"
    Target="media/image1.png"/>
</Relationships>"""

    numbering_xml = """<?xml version="1.0" encoding="UTF-8"?>
<w:numbering xmlns:w="http://purl.oclc.org/ooxml/wordprocessingml/main">
  <w:abstractNum w:abstractNumId="0">
    <w:lvl w:ilvl="0">
      <w:start w:val="1"/>
      <w:numFmt w:val="decimal"/>
      <w:lvlText w:val="%1."/>
    </w:lvl>
    <w:lvl w:ilvl="1">
      <w:start w:val="1"/>
      <w:numFmt w:val="lowerLetter"/>
      <w:lvlText w:val="%2."/>
    </w:lvl>
  </w:abstractNum>
  <w:num w:numId="5">
    <w:abstractNumId w:val="0"/>
    <w:lvlOverride w:ilvl="0">
      <w:startOverride w:val="10"/>
    </w:lvlOverride>
  </w:num>
</w:numbering>"""

    doc2_xml = """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://purl.oclc.org/ooxml/wordprocessingml/main"
            xmlns:r="http://purl.oclc.org/ooxml/officeDocument/relationships"
            xmlns:wp="http://purl.oclc.org/ooxml/drawingml/wordprocessingDrawing"
            xmlns:a="http://purl.oclc.org/ooxml/drawingml/main"
            xmlns:pic="http://purl.oclc.org/ooxml/drawingml/picture"
            xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"
            w:conformance="strict">
  <w:body>
    <w:p>
      <w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="5"/></w:numPr></w:pPr>
      <w:r><w:t>열 번째 항목부터 시작</w:t></w:r>
    </w:p>
    <w:p>
      <w:pPr><w:numPr><w:ilvl w:val="1"/><w:numId w:val="5"/></w:numPr></w:pPr>
      <w:r><w:t>하위 알파벳 첫 항목</w:t></w:r>
    </w:p>
    <w:p>
      <w:r><w:t>체크박스 검증: </w:t></w:r>
      <w:r>
        <w:fldChar w:fldCharType="begin">
          <w:ffData><w:checkBox><w:default w:val="0"/></w:checkBox></w:ffData>
        </w:fldChar>
      </w:r>
      <w:r><w:instrText> FORMCHECKBOX </w:instrText></w:r>
      <w:r><w:fldChar w:fldCharType="separate"/></w:r>
      <w:r><w:fldChar w:fldCharType="end"/></w:r>
      <w:r><w:t> / </w:t></w:r>
      <w:sdt>
        <w:sdtPr><w14:checkbox><w14:checked w14:val="1"/></w14:checkbox></w:sdtPr>
        <w:sdtContent><w:r><w:t>승인완료</w:t></w:r></w:sdtContent>
      </w:sdt>
    </w:p>
    <w:p>
      <w:r>
        <w:drawing>
          <wp:inline>
            <wp:extent cx="2286000" cy="1371600"/>
            <wp:docPr id="1" name="Picture 1"/>
            <a:graphic>
              <a:graphicData uri="http://purl.oclc.org/ooxml/drawingml/picture">
                <pic:pic>
                  <pic:blipFill><a:blip r:embed="rIdImg1"/></pic:blipFill>
                </pic:pic>
              </a:graphicData>
            </a:graphic>
          </wp:inline>
        </w:drawing>
      </w:r>
    </w:p>
    <w:p>
      <w:r><w:t>&lt;그림 1&gt; 공정 계측 결과</w:t></w:r>
    </w:p>
    <w:tbl>
      <w:tblGrid>
        <w:gridCol w:w="2400"/>
        <w:gridCol w:w="2000"/>
        <w:gridCol w:w="2000"/>
      </w:tblGrid>
      <w:tr>
        <w:tc><w:p/></w:tc>
        <w:tc><w:p><w:r><w:t>2025</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>2026</w:t></w:r></w:p></w:tc>
      </w:tr>
      <w:tr>
        <w:tc>
          <w:p><w:r><w:t>매출액</w:t></w:r></w:p>
          <w:p><w:r><w:t>영업이익</w:t></w:r></w:p>
          <w:p><w:r><w:t>순이익</w:t></w:r></w:p>
        </w:tc>
        <w:tc>
          <w:p><w:r><w:t>100</w:t><w:br/><w:t>20</w:t><w:br/><w:t>15</w:t></w:r></w:p>
        </w:tc>
        <w:tc>
          <w:p><w:r><w:t>140</w:t></w:r></w:p>
          <w:p><w:r><w:t>35</w:t></w:r></w:p>
          <w:p><w:r><w:t>28</w:t></w:r></w:p>
          <w:p/>
        </w:tc>
      </w:tr>
      <w:tr>
        <w:tc><w:p/></w:tc>
        <w:tc><w:p/></w:tc>
        <w:tc><w:p/></w:tc>
      </w:tr>
    </w:tbl>
    <w:sectPr>
      <w:pgSz w:w="595.30pt" w:h="841.90pt"/>
      <w:pgMar w:top="72pt" w:bottom="72pt" w:left="72pt" w:right="72pt"/>
    </w:sectPr>
  </w:body>
</w:document>"""

    edge_docx = tmp_path / "strict_edge.docx"
    with zipfile.ZipFile(edge_docx, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("_rels/.rels", rels_xml)
        zf.writestr("word/_rels/document2.xml.rels", doc2_rels)
        zf.writestr("word/numbering.xml", numbering_xml)
        zf.writestr("word/media/image1.png", fig_png)
        zf.writestr("word/document2.xml", doc2_xml)

    out_dir = tmp_path / "out_edge"
    doc, manifest = parse_and_export(edge_docx, out_dir)
    md = manifest.document_markdown.read_text(encoding="utf-8")

    # 1. startOverride=10 및 하위 lowerLetter(a.) 목록이 중복 불릿('- 10.') 없이 렌더링
    assert "\n10. 열 번째 항목부터 시작\n" in md
    assert "\n  a. 하위 알파벳 첫 항목\n" in md

    # 2. 레거시 체크박스(☐) 및 SDT 체크박스(☑) 추출
    assert "체크박스 검증: ☐ / ☑승인완료" in md

    # 3. <그림 1> 캡션이 직후의 표가 아닌 직전의 그림에 연결됨
    assert doc.visual_regions[0].caption == "<그림 1> 공정 계측 결과"
    assert doc.tables[0].caption is None

    # 4. 단일 <w:tr> 내부의 3줄 병렬 데이터가 3개 데이터 행으로 분할되고 하단 빈 행이 제거됨
    tbl = doc.tables[0]
    assert tbl.headers == ["", "2025", "2026"]
    assert tbl.rows == [
        ["매출액", "100", "140"],
        ["영업이익", "20", "35"],
        ["순이익", "15", "28"],
    ]
