"""무의존성 KS X 6101 OWPML HWPX 어댑터(`autoetl.parser.hwpx`) 단위·통합 테스트."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from autoetl.cli import main
from autoetl.parser import (
    CropStatus,
    ElementKind,
    HeuristicRasterLayoutDetector,
    HwpxDocumentAdapter,
    OpenAICompatibleVlmAdapter,
    PageCategory,
    ParseOptions,
    ParserInputError,
    ParseStrategy,
    RhwpDocumentAdapter,
    RhwpPageRenderer,
    SourceFormat,
    VisionStatus,
    inspect_document,
    parse_and_export,
    supported_extensions,
)
from autoetl.parser.image_io import encode_png


def _make_solid_png(
    width: int = 90, height: int = 60, rgb: tuple[int, int, int] = (30, 110, 190)
) -> bytes:
    row = bytes(rgb) * width
    return encode_png(row * height, width, height)


def _make_synthetic_scanned_page_png(width: int = 360, height: int = 480) -> bytes:
    """상단 제목·중단 격자 표·하단 컬러 막대 차트가 포함된 합성 스캔 페이지 PNG."""
    buf = bytearray(b"\xff\xff\xff" * (width * height))

    def set_px(x: int, y: int, r: int, g: int, b: int) -> None:
        if 0 <= x < width and 0 <= y < height:
            idx = (y * width + x) * 3
            buf[idx] = r
            buf[idx + 1] = g
            buf[idx + 2] = b

    for y in range(38, 56):
        if y % 5 != 0:
            for x in range(40, 300):
                if (x // 4) % 6 != 0:
                    set_px(x, y, 20, 20, 20)

    t_x0, t_y0, t_x1, t_y1 = 35, 110, 325, 260
    for y in (110, 145, 185, 225, 260):
        for x in range(t_x0, t_x1 + 1):
            set_px(x, y, 15, 15, 15)
            set_px(x, min(height - 1, y + 1), 15, 15, 15)
    for x in (35, 130, 225, 325):
        for y in range(t_y0, t_y1 + 1):
            set_px(x, y, 15, 15, 15)
            set_px(min(width - 1, x + 1), y, 15, 15, 15)
    for ry in (122, 160, 200, 238):
        for cx in (50, 145, 240):
            for y in range(ry, ry + 8):
                for x in range(cx, cx + 55):
                    if (x + y) % 3 != 0:
                        set_px(x, y, 30, 30, 30)

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


def _build_rich_sample_hwpx(target_path: Path) -> Path:
    """4페이지(2개 섹션) 분량의 표준 OWPML `.hwpx` 테스트 문서를 생성한다."""
    fig_png = _make_solid_png(120, 80, (40, 130, 200))
    scan_png = _make_synthetic_scanned_page_png(360, 480)

    container_xml = """<?xml version="1.0" encoding="UTF-8"?>
<ocf:container xmlns:ocf="urn:oasis:names:tc:opendocument:xmlns:container">
  <ocf:rootfiles>
    <ocf:rootfile full-path="Contents/content.hpf"
      media-type="application/hwpml-package+xml"/>
  </ocf:rootfiles>
</ocf:container>"""

    content_hpf = """<?xml version="1.0" encoding="UTF-8"?>
<opf:package xmlns:opf="http://www.idpf.org/2007/opf/" version="1.0">
  <opf:metadata>
    <opf:title>2026년도 차세대 공공데이터 표준화 사업 계획서</opf:title>
    <opf:meta name="creator" content="AutoETL HWPX 검증팀"/>
    <opf:meta name="generator" content="Hancom Office HWP"/>
  </opf:metadata>
  <opf:manifest>
    <opf:item id="header" href="Contents/header.xml" media-type="application/xml"/>
    <opf:item id="section0" href="Contents/section0.xml" media-type="application/xml"/>
    <opf:item id="section1" href="Contents/section1.xml" media-type="application/xml"/>
    <opf:item id="img1" href="BinData/image1.png" media-type="image/png"/>
    <opf:item id="scan2" href="BinData/scan2.png" media-type="image/png"/>
    <opf:item id="chart1" href="Chart/chart1.xml" media-type="application/xml"/>
  </opf:manifest>
  <opf:spine>
    <opf:itemref idref="header"/>
    <opf:itemref idref="section0"/>
    <opf:itemref idref="section1"/>
  </opf:spine>
</opf:package>"""

    header_xml = """<?xml version="1.0" encoding="UTF-8"?>
<hh:head xmlns:hh="http://www.hancom.co.kr/hwpml/2011/head"
         xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph">
  <hh:refList>
    <hh:fontfaces itemCnt="2">
      <hh:fontface lang="HANGUL" fontCnt="2">
        <hh:font id="0" face="함초롬바탕" type="TTF"/>
        <hh:font id="1" face="D2Coding" type="TTF"/>
      </hh:fontface>
      <hh:fontface lang="LATIN" fontCnt="2">
        <hh:font id="0" face="함초롬바탕" type="TTF"/>
        <hh:font id="1" face="D2Coding" type="TTF"/>
      </hh:fontface>
    </hh:fontfaces>
    <hh:charProperties itemCnt="4">
      <hh:charPr id="0" height="1000">
        <hh:fontRef hangul="0" latin="0"/>
      </hh:charPr>
      <hh:charPr id="1" height="1800" bold="1">
        <hh:fontRef hangul="0" latin="0"/>
      </hh:charPr>
      <hh:charPr id="2" height="1400" bold="1">
        <hh:fontRef hangul="0" latin="0"/>
      </hh:charPr>
      <hh:charPr id="3" height="950">
        <hh:fontRef hangul="1" latin="1"/>
      </hh:charPr>
    </hh:charProperties>
    <hh:numberings itemCnt="1">
      <hh:numbering id="1" start="1">
        <hh:paraHead level="1" numFormat="HANGUL_SYLLABLE" start="1">^1.</hh:paraHead>
        <hh:paraHead level="2" numFormat="DIGIT" start="1">^2)</hh:paraHead>
      </hh:numbering>
    </hh:numberings>
    <hh:bullets itemCnt="1">
      <hh:bullet id="1" char="•"/>
    </hh:bullets>
    <hh:paraProperties itemCnt="5">
      <hh:paraPr id="0">
        <hh:heading type="NONE" idRef="0" level="0"/>
      </hh:paraPr>
      <hh:paraPr id="1">
        <hp:switch>
          <hp:case hp:required-namespace="http://www.hancom.co.kr/hwpml/2016/HwpUnitChar">
            <hh:heading type="OUTLINE" idRef="0" level="0"/>
          </hp:case>
          <hp:default>
            <hh:heading type="OUTLINE" idRef="0" level="0"/>
          </hp:default>
        </hp:switch>
      </hh:paraPr>
      <hh:paraPr id="2">
        <hh:heading type="OUTLINE" idRef="0" level="1"/>
      </hh:paraPr>
      <hh:paraPr id="3">
        <hh:heading type="BULLET" idRef="1" level="0"/>
      </hh:paraPr>
      <hh:paraPr id="4">
        <hh:heading type="NUMBER" idRef="1" level="0"/>
      </hh:paraPr>
    </hh:paraProperties>
    <hh:styles itemCnt="4">
      <hh:style id="0" type="PARA" name="바탕글" engName="Normal"
        paraPrIDRef="0" charPrIDRef="0"/>
      <hh:style id="1" type="PARA" name="개요 1" engName="Outline 1"
        paraPrIDRef="1" charPrIDRef="1"/>
      <hh:style id="2" type="PARA" name="개요 2" engName="Outline 2"
        paraPrIDRef="2" charPrIDRef="2"/>
      <hh:style id="3" type="PARA" name="소스 코드" engName="Source Code"
        paraPrIDRef="0" charPrIDRef="3"/>
    </hh:styles>
    <hh:borderFills itemCnt="2">
      <hh:borderFill id="1">
        <hh:leftBorder type="SOLID" width="0.12mm"/>
        <hh:rightBorder type="SOLID" width="0.12mm"/>
        <hh:topBorder type="SOLID" width="0.12mm"/>
        <hh:bottomBorder type="SOLID" width="0.12mm"/>
      </hh:borderFill>
      <hh:borderFill id="2">
        <hh:leftBorder type="NONE"/>
        <hh:rightBorder type="NONE"/>
        <hh:topBorder type="SOLID" width="0.4mm"/>
        <hh:bottomBorder type="SOLID" width="0.4mm"/>
      </hh:borderFill>
    </hh:borderFills>
  </hh:refList>
</hh:head>"""

    chart1_xml = """<?xml version="1.0" encoding="UTF-8"?>
<c:chartSpace xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart"
              xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
  <c:chart>
    <c:title>
      <c:tx><c:rich><a:p><a:r><a:t>기관별 데이터 개방 현황</a:t></a:r></a:p></c:rich></c:tx>
    </c:title>
    <c:plotArea>
      <c:barChart>
        <c:ser>
          <c:tx><c:v>개방건수</c:v></c:tx>
          <c:cat>
            <c:strRef><c:strCache>
              <c:pt idx="0"><c:v>중앙부처</c:v></c:pt>
              <c:pt idx="1"><c:v>지자체</c:v></c:pt>
            </c:strCache></c:strRef>
          </c:cat>
          <c:val>
            <c:numRef><c:numCache>
              <c:pt idx="0"><c:v>450</c:v></c:pt>
              <c:pt idx="1"><c:v>820</c:v></c:pt>
            </c:numCache></c:numRef>
          </c:val>
        </c:ser>
      </c:barChart>
    </c:plotArea>
  </c:chart>
</c:chartSpace>"""

    section0_xml = """<?xml version="1.0" encoding="UTF-8"?>
<hs:sec xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section"
        xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph"
        xmlns:hc="http://www.hancom.co.kr/hwpml/2011/core">
  <!-- ================= PAGE 1: DIGITAL_TEXT ================= -->
  <hp:p paraPrIDRef="1" styleIDRef="1" pageBreak="0">
    <hp:run charPrIDRef="1">
      <hp:secPr>
        <hp:pagePr landscape="WIDELY" width="59528" height="84188">
          <hp:margin left="8504" right="8504" top="5668" bottom="4252"/>
        </hp:pagePr>
      </hp:secPr>
      <hp:ctrl>
        <hp:header>
          <hp:subList>
            <hp:p><hp:run><hp:t>2026년도 공공데이터 표준화 사업 계획서</hp:t></hp:run></hp:p>
          </hp:subList>
        </hp:header>
        <hp:footer>
          <hp:subList>
            <hp:p><hp:run><hp:t>행정안전부 · 한국지능정보사회진흥원</hp:t></hp:run></hp:p>
          </hp:subList>
        </hp:footer>
      </hp:ctrl>
      <hp:t>1. 사업 개요 및 추진 배경</hp:t>
    </hp:run>
  </hp:p>
  <hp:p paraPrIDRef="2" styleIDRef="2">
    <hp:run charPrIDRef="2"><hp:t>1.1 세부 추진 목표</hp:t></hp:run>
  </hp:p>
  <hp:p paraPrIDRef="0" styleIDRef="0">
    <hp:run charPrIDRef="0">
      <hp:t>본 사업은 개방형 문서 표준을 기반으로 추진한다</hp:t>
      <hp:t><hp:deleteBegin/>폐기된 구형 문구<hp:deleteEnd/></hp:t>
      <hp:ctrl>
        <hp:footNote number="1">
          <hp:subList>
            <hp:p><hp:run><hp:t>KS X 6101 OWPML 국가 표준 규격을 준수한다.</hp:t></hp:run></hp:p>
          </hp:subList>
        </hp:footNote>
        <hp:endNote number="1">
          <hp:subList>
            <hp:p><hp:run><hp:t>세부 산출물 내역은 별첨 1을 참조한다.</hp:t></hp:run></hp:p>
          </hp:subList>
        </hp:endNote>
        <hp:hiddenComment author="박심사" date="2026-11-23">
          <hp:subList>
            <hp:p><hp:run><hp:t>예산 산출 근거 최종 확인 완료.</hp:t></hp:run></hp:p>
          </hp:subList>
        </hp:hiddenComment>
      </hp:ctrl>
      <hp:t>. 보안 점검 완료 여부: </hp:t>
      <hp:checkBtn value="1"/>
    </hp:run>
  </hp:p>
  <hp:p paraPrIDRef="0" styleIDRef="0">
    <hp:switch>
      <hp:case hp:required-namespace="http://www.hancom.co.kr/hwpml/2016/HwpUnitChar">
        <hp:run charPrIDRef="0"><hp:t>스위치 블록 중복 방지 단일 추출 문장</hp:t></hp:run>
      </hp:case>
      <hp:default>
        <hp:run charPrIDRef="0"><hp:t>스위치 블록 중복 방지 단일 추출 문장</hp:t></hp:run>
      </hp:default>
    </hp:switch>
  </hp:p>
  <hp:p paraPrIDRef="3" styleIDRef="0">
    <hp:run charPrIDRef="0"><hp:t>표준 라이브러리 기반 무의존성 파서</hp:t></hp:run>
  </hp:p>
  <hp:p paraPrIDRef="4" styleIDRef="0">
    <hp:run charPrIDRef="0"><hp:t>첫 번째 한글 번호 항목</hp:t></hp:run>
  </hp:p>
  <hp:p paraPrIDRef="4" styleIDRef="0">
    <hp:run charPrIDRef="0"><hp:t>두 번째 한글 번호 항목</hp:t></hp:run>
  </hp:p>
  <hp:p paraPrIDRef="0" styleIDRef="0">
    <hp:run charPrIDRef="0">
      <hp:equation>
        <hp:script>E = {mc^2} over {sqrt{1 - v^2}}</hp:script>
      </hp:equation>
    </hp:run>
  </hp:p>
  <hp:p paraPrIDRef="0" styleIDRef="3">
    <hp:run charPrIDRef="3">
      <hp:t>uv run autoetl parse proposal.hwpx --out ./out</hp:t>
    </hp:run>
  </hp:p>

  <!-- ================= PAGE 2: TABLE_HEAVY (explicit pageBreak="1") ================= -->
  <hp:p paraPrIDRef="1" styleIDRef="1" pageBreak="1">
    <hp:run charPrIDRef="1"><hp:t>2. 연차별 소요 예산 및 통계</hp:t></hp:run>
  </hp:p>
  <hp:p paraPrIDRef="0" styleIDRef="0">
    <hp:run charPrIDRef="0">
      <hp:tbl rowCnt="3" colCnt="4" borderFillIDRef="1">
        <hp:caption>
          <hp:subList>
            <hp:p><hp:run><hp:t>표 1. 연차별 소요 예산 총괄표</hp:t></hp:run></hp:p>
          </hp:subList>
        </hp:caption>
        <hp:tr>
          <hp:tc header="1" borderFillIDRef="1">
            <hp:cellAddr colAddr="0" rowAddr="0"/>
            <hp:cellSpan colSpan="1" rowSpan="2"/>
            <hp:cellSz width="12000" height="3000"/>
            <hp:subList><hp:p><hp:run><hp:t>비목</hp:t></hp:run></hp:p></hp:subList>
          </hp:tc>
          <hp:tc header="1" borderFillIDRef="1">
            <hp:cellAddr colAddr="1" rowAddr="0"/>
            <hp:cellSpan colSpan="2" rowSpan="1"/>
            <hp:cellSz width="24000" height="1500"/>
            <hp:subList><hp:p><hp:run><hp:t>소요 예산(백만원)</hp:t></hp:run></hp:p></hp:subList>
          </hp:tc>
          <hp:tc header="1" borderFillIDRef="1">
            <hp:cellAddr colAddr="3" rowAddr="0"/>
            <hp:cellSpan colSpan="1" rowSpan="1"/>
            <hp:cellSz width="2000" height="1500"/>
            <hp:subList><hp:p/></hp:subList>
          </hp:tc>
        </hp:tr>
        <hp:tr>
          <hp:tc header="1" borderFillIDRef="1">
            <hp:cellAddr colAddr="1" rowAddr="1"/>
            <hp:cellSpan colSpan="1" rowSpan="1"/>
            <hp:cellSz width="12000" height="1500"/>
            <hp:subList><hp:p><hp:run><hp:t>1차년도</hp:t></hp:run></hp:p></hp:subList>
          </hp:tc>
          <hp:tc header="1" borderFillIDRef="1">
            <hp:cellAddr colAddr="2" rowAddr="1"/>
            <hp:cellSpan colSpan="1" rowSpan="1"/>
            <hp:cellSz width="12000" height="1500"/>
            <hp:subList><hp:p><hp:run><hp:t>2차년도</hp:t></hp:run></hp:p></hp:subList>
          </hp:tc>
          <hp:tc header="1" borderFillIDRef="1">
            <hp:cellAddr colAddr="3" rowAddr="1"/>
            <hp:cellSpan colSpan="1" rowSpan="1"/>
            <hp:cellSz width="2000" height="1500"/>
            <hp:subList><hp:p/></hp:subList>
          </hp:tc>
        </hp:tr>
        <hp:tr>
          <hp:tc borderFillIDRef="1">
            <hp:cellAddr colAddr="0" rowAddr="2"/>
            <hp:cellSpan colSpan="1" rowSpan="1"/>
            <hp:cellSz width="12000" height="1500"/>
            <hp:subList><hp:p><hp:run><hp:t>개발비</hp:t></hp:run></hp:p></hp:subList>
          </hp:tc>
          <hp:tc borderFillIDRef="1">
            <hp:cellAddr colAddr="1" rowAddr="2"/>
            <hp:cellSpan colSpan="1" rowSpan="1"/>
            <hp:cellSz width="12000" height="1500"/>
            <hp:subList><hp:p><hp:run><hp:t>320</hp:t></hp:run></hp:p></hp:subList>
          </hp:tc>
          <hp:tc borderFillIDRef="1">
            <hp:cellAddr colAddr="2" rowAddr="2"/>
            <hp:cellSpan colSpan="1" rowSpan="1"/>
            <hp:cellSz width="12000" height="1500"/>
            <hp:subList><hp:p><hp:run><hp:t>480</hp:t></hp:run></hp:p></hp:subList>
          </hp:tc>
          <hp:tc borderFillIDRef="1">
            <hp:cellAddr colAddr="3" rowAddr="2"/>
            <hp:cellSpan colSpan="1" rowSpan="1"/>
            <hp:cellSz width="2000" height="1500"/>
            <hp:subList><hp:p/></hp:subList>
          </hp:tc>
        </hp:tr>
      </hp:tbl>
    </hp:run>
  </hp:p>
  <hp:p paraPrIDRef="0" styleIDRef="0">
    <hp:run charPrIDRef="0">
      <hp:tbl rowCnt="2" colCnt="2" borderFillIDRef="2">
        <hp:tr>
          <hp:tc borderFillIDRef="2">
            <hp:cellAddr colAddr="0" rowAddr="0"/>
            <hp:cellSpan colSpan="1" rowSpan="1"/>
            <hp:subList><hp:p><hp:run><hp:t>평가 지표</hp:t></hp:run></hp:p></hp:subList>
          </hp:tc>
          <hp:tc borderFillIDRef="2">
            <hp:cellAddr colAddr="1" rowAddr="0"/>
            <hp:cellSpan colSpan="1" rowSpan="1"/>
            <hp:subList><hp:p><hp:run><hp:t>목표치</hp:t></hp:run></hp:p></hp:subList>
          </hp:tc>
        </hp:tr>
        <hp:tr>
          <hp:tc borderFillIDRef="2">
            <hp:cellAddr colAddr="0" rowAddr="1"/>
            <hp:cellSpan colSpan="1" rowSpan="1"/>
            <hp:subList><hp:p><hp:run><hp:t>표준 준수율</hp:t></hp:run></hp:p></hp:subList>
          </hp:tc>
          <hp:tc borderFillIDRef="2">
            <hp:cellAddr colAddr="1" rowAddr="1"/>
            <hp:cellSpan colSpan="1" rowSpan="1"/>
            <hp:subList><hp:p><hp:run><hp:t>99.8%</hp:t></hp:run></hp:p></hp:subList>
          </hp:tc>
        </hp:tr>
      </hp:tbl>
    </hp:run>
  </hp:p>
  <hp:p paraPrIDRef="0" styleIDRef="0">
    <hp:run charPrIDRef="0">
      <hp:chart chartIDRef="chart1">
        <hp:curSz width="32000" height="18000"/>
      </hp:chart>
    </hp:run>
  </hp:p>
</hs:sec>"""

    section1_xml = """<?xml version="1.0" encoding="UTF-8"?>
<hs:sec xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section"
        xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph"
        xmlns:hc="http://www.hancom.co.kr/hwpml/2011/core">
  <!-- ================= PAGE 3: 2-COLUMN + PICTURE + VECTOR CONTAINER ================= -->
  <hp:p paraPrIDRef="1" styleIDRef="1">
    <hp:run charPrIDRef="1">
      <hp:secPr>
        <hp:pagePr landscape="WIDELY" width="59528" height="84188">
          <hp:margin left="8504" right="8504" top="5668" bottom="4252"/>
        </hp:pagePr>
      </hp:secPr>
      <hp:ctrl>
        <hp:colPr type="NEWSPAPER" colCount="2" sameGap="1134"/>
      </hp:ctrl>
      <hp:t>3. 시스템 구성도 및 다단 레이아웃</hp:t>
    </hp:run>
  </hp:p>
  <hp:p paraPrIDRef="0" styleIDRef="0">
    <hp:run charPrIDRef="0">
      <hp:pic id="pic1">
        <hp:curSz width="24000" height="16000"/>
        <hc:img binaryItemIDRef="img1"/>
        <hp:caption>
          <hp:subList>
            <hp:p><hp:run><hp:t>&lt;그림 1&gt; 플랫폼 목표 아키텍처</hp:t></hp:run></hp:p>
          </hp:subList>
        </hp:caption>
      </hp:pic>
    </hp:run>
  </hp:p>
  <hp:p paraPrIDRef="0" styleIDRef="0">
    <hp:run charPrIDRef="0">
      <hp:container id="grp1">
        <hp:curSz width="28000" height="14000"/>
        <hp:rect id="r1">
          <hp:drawText>
            <hp:subList>
              <hp:p><hp:run><hp:t>데이터 수집 게이트웨이</hp:t></hp:run></hp:p>
            </hp:subList>
          </hp:drawText>
        </hp:rect>
        <hp:rect id="r2">
          <hp:drawText>
            <hp:subList>
              <hp:p><hp:run><hp:t>문서 구조 라우팅 엔진</hp:t></hp:run></hp:p>
            </hp:subList>
          </hp:drawText>
        </hp:rect>
      </hp:container>
    </hp:run>
  </hp:p>

  <!-- ================= PAGE 4: SCANNED_IMAGE ================= -->
  <hp:p paraPrIDRef="0" styleIDRef="0" pageBreak="1">
    <hp:run charPrIDRef="0">
      <hp:pic id="scan_pic">
        <hp:curSz width="44000" height="62000"/>
        <hc:img binaryItemIDRef="scan2"/>
      </hp:pic>
    </hp:run>
  </hp:p>
</hs:sec>"""

    with zipfile.ZipFile(target_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mimetype", "application/hwp+zip")
        zf.writestr("META-INF/container.xml", container_xml)
        zf.writestr("Contents/content.hpf", content_hpf)
        zf.writestr("Contents/header.xml", header_xml)
        zf.writestr("Contents/section0.xml", section0_xml)
        zf.writestr("Contents/section1.xml", section1_xml)
        zf.writestr("Chart/chart1.xml", chart1_xml)
        zf.writestr("BinData/image1.png", fig_png)
        zf.writestr("BinData/scan2.png", scan_png)

    return target_path


def test_hwpx_adapter_registration_and_error_handling(tmp_path: Path) -> None:
    """HWPX 어댑터 등록 및 구형 .hwp(OLE2)·배포용 암호화·손상 파일 예외 처리를 검증한다."""
    assert ".hwpx" in supported_extensions()
    adapter = HwpxDocumentAdapter()
    assert adapter.format == SourceFormat.HWPX
    assert adapter.extensions == (".hwpx",)

    # 1. 존재하지 않는 파일
    with pytest.raises(ParserInputError, match="존재하지 않는다"):
        inspect_document(tmp_path / "missing.hwpx")

    # 2. 구형 바이너리 .hwp (OLE2 매직 바이트)
    ole_file = tmp_path / "legacy_hwp.hwpx"
    ole_file.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64)
    with pytest.raises(ParserInputError, match="OLE2"):
        inspect_document(ole_file)

    # 3. 배포용 암호화 컨테이너(META-INF/manifest.xml 내 encryption-data)
    enc_hwpx = tmp_path / "encrypted.hwpx"
    with zipfile.ZipFile(enc_hwpx, "w") as zf:
        zf.writestr(
            "META-INF/manifest.xml",
            "<manifest><file-entry>"
            '<encryption-data algorithm-name="PBKDF2"/>'
            "</file-entry></manifest>",
        )
        zf.writestr("Contents/section0.xml", "<hs:sec/>")
    with pytest.raises(ParserInputError, match="암호화"):
        inspect_document(enc_hwpx)

    # 4. 손상된 ZIP 파일
    bad_zip = tmp_path / "corrupt.hwpx"
    bad_zip.write_bytes(b"not-a-zip")
    with pytest.raises(ParserInputError, match="ZIP"):
        inspect_document(bad_zip)

    # 5. section*.xml이 없는 ZIP
    empty_zip = tmp_path / "no_sec.hwpx"
    with zipfile.ZipFile(empty_zip, "w") as zf:
        zf.writestr("mimetype", "application/hwp+zip")
    with pytest.raises(ParserInputError, match="section"):
        inspect_document(empty_zip)

    # 6. 범위를 벗어난 페이지 선택
    valid_hwpx = _build_rich_sample_hwpx(tmp_path / "sample.hwpx")
    with pytest.raises(ParserInputError, match="범위"):
        inspect_document(valid_hwpx, options=ParseOptions(pages=(99,)))


def test_hwpx_rich_document_inspection_and_parsing(tmp_path: Path) -> None:
    """`rhwp` 기반 HWPX 어댑터가 개요·목록·수식·각주·병합표·그림·머리글/바닥글을 추출한다."""
    hwpx_path = _build_rich_sample_hwpx(tmp_path / "rich.hwpx")
    out_dir = tmp_path / "parsed_hwpx"

    plan = inspect_document(hwpx_path, options=ParseOptions(include_comments=True))
    assert plan.document.format == SourceFormat.HWPX
    assert plan.document.title == "2026년도 차세대 공공데이터 표준화 사업 계획서"
    assert plan.document.author == "AutoETL HWPX 검증팀"
    assert plan.summary.total_pages == 4
    assert plan.summary.analyzed_pages == 4
    assert 3 in plan.summary.multi_column_pages
    assert 4 in plan.summary.multi_column_pages
    assert plan.pages[1].category == PageCategory.TABLE_HEAVY
    assert plan.pages[2].metrics.column_count == 2
    assert plan.pages[3].metrics.column_count == 2
    assert plan.pages[3].category == PageCategory.SCANNED_IMAGE

    outline_titles = [(o.level, o.title, o.page) for o in plan.outline]
    assert (1, "1. 사업 개요 및 추진 배경", 1) in outline_titles
    assert (1, "2. 연차별 소요 예산 및 통계", 2) in outline_titles
    assert (1, "3. 시스템 구성도 및 다단 레이아웃", 3) in outline_titles

    # `--pages 1` 선택 시 마지막 페이지(4p) 전용 미주는 1페이지에 섞이지 않고 경고만 기록해야 한다
    p1_only = inspect_document(hwpx_path, options=ParseOptions(pages=(1,)))
    assert len(p1_only.pages) == 1
    from autoetl.parser import parse_document

    p1_doc = parse_document(hwpx_path, options=ParseOptions(pages=(1,)))
    assert not any("[미주 1]" in b.text for b in p1_doc.pages[0].blocks)
    assert any("미주 1건" in w for w in p1_doc.warnings)

    doc, manifest = parse_and_export(
        hwpx_path,
        out_dir,
        options=ParseOptions(include_comments=True),
    )
    assert isinstance(HwpxDocumentAdapter(), RhwpDocumentAdapter)
    assert manifest.parsed_json.is_file()
    assert manifest.document_markdown.is_file()
    md = manifest.document_markdown.read_text(encoding="utf-8")

    # --- Page 1 (Section 0, Part 1): 변경추적·스위치·목록·수식·코드·각주·머리글/바닥글 ---
    p1 = doc.pages[0]

    heading_blk = next(b for b in p1.blocks if b.kind == ElementKind.HEADING)
    assert heading_blk.text == "1. 사업 개요 및 추진 배경"

    assert any(
        b.kind == ElementKind.LIST_ITEM and "표준 라이브러리 기반 무의존성 파서" in b.text
        for b in p1.blocks
    )
    assert any(
        b.kind == ElementKind.LIST_ITEM and "첫 번째 한글 번호 항목" in b.text for b in p1.blocks
    )
    assert any(
        b.kind == ElementKind.LIST_ITEM and "두 번째 한글 번호 항목" in b.text for b in p1.blocks
    )

    # 한컴 수식 스크립트 및 D2Coding 코드 블록
    formula_blocks = [b for b in p1.blocks if b.kind == ElementKind.FORMULA_OR_CODE]
    assert any("mc^2" in b.text for b in formula_blocks)
    assert any("uv run autoetl parse proposal.hwpx" in b.text for b in p1.blocks)

    # 각주·미주(4p) 및 머리글·바닥글 분리
    assert any("[각주 1]" in b.text and "KS X 6101 OWPML" in b.text for b in p1.blocks)
    assert any("[미주 1]" in b.text and "별첨 1" in b.text for b in doc.pages[3].blocks)
    assert any("2026년도 공공데이터 표준화 사업 계획서" in b.text for b in p1.header_footer_blocks)
    assert any("행정안전부" in b.text for b in p1.header_footer_blocks)

    # Page 2: 병합 표 + 상하선 표
    p2 = doc.pages[1]
    assert len(p2.tables) >= 2
    merged_tbl = p2.tables[0]
    assert merged_tbl.caption == "표 1. 연차별 소요 예산 총괄표"
    assert merged_tbl.strategy == ParseStrategy.RULED_TABLE
    assert merged_tbl.col_count == 3
    assert merged_tbl.row_count == 3
    assert merged_tbl.has_merged_cells is True
    assert merged_tbl.headers == ["비목", "소요 예산(백만원)", ""]
    assert merged_tbl.rows[0] == ["", "1차년도", "2차년도"]
    assert merged_tbl.rows[1] == ["개발비", "320", "480"]
    r0c0 = next(c for c in merged_tbl.cells if c.row == 0 and c.col == 0)
    r0c1 = next(c for c in merged_tbl.cells if c.row == 0 and c.col == 1)
    assert r0c0.row_span == 2
    assert r0c1.col_span == 2

    horiz_tbl = p2.tables[1]
    assert horiz_tbl.headers == ["평가 지표", "목표치"]
    assert horiz_tbl.rows == [["표준 준수율", "99.8%"]]
    assert "소요 예산(백만원)" in md

    # Page 3: 그림 + 캡션 + 크롭 추출
    p3 = doc.pages[2]
    fig_vis = next(v for v in p3.visual_regions if v.element_kind == ElementKind.FIGURE_IMAGE)
    assert fig_vis.caption == "<그림 1> 플랫폼 목표 아키텍처"
    assert fig_vis.crop_status in (CropStatus.EMBEDDED_EXTRACTED, CropStatus.SAVED)


def test_hwpx_scanned_page_layout_detector_and_vlm_pipeline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`rhwp` 네이티브 `render_png`·스캔 이미지 레이아웃 탐지기·VLM 연동을 검증한다."""
    hwpx_path = _build_rich_sample_hwpx(tmp_path / "scanned.hwpx")
    detector = HeuristicRasterLayoutDetector()
    renderer = RhwpPageRenderer()

    full_img = renderer.render_region(hwpx_path, 1, None, dpi=120)
    assert full_img is not None
    assert full_img.extension == ".png"
    assert full_img.data.startswith(b"\x89PNG\r\n\x1a\n")
    assert (full_img.width or 0) > 100 and (full_img.height or 0) > 100

    plan = inspect_document(
        hwpx_path,
        options=ParseOptions(pages=(4,)),
        layout_detector=detector,
    )
    assert len(plan.pages) == 1
    p4_route = plan.pages[0]
    assert p4_route.category == PageCategory.SCANNED_IMAGE
    assert any(f"레이아웃 탐지({detector.name})" in r for r in p4_route.reasons)
    assert len(p4_route.regions) >= 2

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

    out_dir = tmp_path / "out_scan_hwpx"
    doc, manifest = parse_and_export(
        hwpx_path,
        out_dir,
        options=ParseOptions(pages=(4,)),
        layout_detector=detector,
        page_renderer=renderer,
        vision_adapter=vlm,
    )
    assert len(doc.pages) == 1
    p4 = doc.pages[0]
    assert p4.header_footer_blocks  # --pages (4,)에서도 머리글/바닥글 보존
    assert len(p4.visual_regions) >= 2
    assert all(
        v.crop_status in (CropStatus.EMBEDDED_EXTRACTED, CropStatus.SAVED)
        for v in p4.visual_regions
    )
    assert all(v.vision_status == VisionStatus.COMPLETED for v in p4.visual_regions)
    assert len(manifest.crop_paths) == len(p4.visual_regions)
    assert captured_prompts


def test_cli_parse_hwpx_plan_and_export(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`autoetl parse` CLI가 `.hwpx`의 `--plan-only` 및 `--include-comments --out`을 처리한다."""
    hwpx_path = _build_rich_sample_hwpx(tmp_path / "cli_test.hwpx")

    rc = main(["parse", str(hwpx_path), "--plan-only"])
    assert rc == 0
    out_str = capsys.readouterr().out
    assert "2026년도 차세대 공공데이터 표준화 사업 계획서" in out_str
    assert "hwpx" in out_str.lower()

    bundle_dir = tmp_path / "cli_hwpx_bundle"
    rc2 = main(
        [
            "parse",
            str(hwpx_path),
            "--pages",
            "1,2",
            "--include-comments",
            "--out",
            str(bundle_dir),
        ]
    )
    assert rc2 == 0
    assert (bundle_dir / "parsed.json").is_file()
    assert (bundle_dir / "document.md").is_file()
    md_text = (bundle_dir / "document.md").read_text(encoding="utf-8")
    assert "1. 사업 개요 및 추진 배경" in md_text
    assert "소요 예산(백만원)" in md_text
    assert "개발비" in md_text


def test_hwpx_real_world_owpml_edge_cases(tmp_path: Path) -> None:
    """실제 공개 HWPX 55종 평가에서 도출된 10대 OWPML 엣지케이스를 회귀 검증한다."""
    target_path = tmp_path / "edge_cases.hwpx"

    container_xml = """<?xml version="1.0" encoding="UTF-8"?>
<ocf:container xmlns:ocf="urn:oasis:names:tc:opendocument:xmlns:container">
  <ocf:rootfiles>
    <ocf:rootfile full-path="Contents/content.hpf" media-type="application/hwpml-package+xml"/>
  </ocf:rootfiles>
</ocf:container>"""

    version_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<hv:HCFVersion xmlns:hv="http://www.hancom.co.kr/hwpml/2011/version" '
        'application="Hancom Office Hangul" appVersion="12, 0, 0, 3146"/>'
    )

    content_hpf = """<?xml version="1.0" encoding="UTF-8"?>
<opf:package xmlns:opf="http://www.idpf.org/2007/opf/">
  <opf:metadata>
    <opf:title>공공문서 실전 엣지케이스 검증</opf:title>
    <opf:meta name="creator" content="text"/>
    <opf:meta name="lastsaveby" content="text">MOLIT</opf:meta>
  </opf:metadata>
  <opf:manifest>
    <opf:item id="header" href="Contents/header.xml" media-type="application/xml"/>
    <opf:item id="section0" href="Contents/section0.xml" media-type="application/xml"/>
    <opf:item id="section1" href="Contents/section1.xml" media-type="application/xml"/>
  </opf:manifest>
  <opf:spine>
    <opf:itemref idref="header"/>
    <opf:itemref idref="section0"/>
    <opf:itemref idref="section1"/>
  </opf:spine>
</opf:package>"""

    header_xml = """<?xml version="1.0" encoding="UTF-8"?>
<hh:head xmlns:hh="http://www.hancom.co.kr/hwpml/2011/head">
  <hh:refList>
    <hh:charProperties>
      <hh:charPr id="0" height="1000"/>
    </hh:charProperties>
    <hh:borderFills>
      <hh:borderFill id="1">
        <hh:leftBorder type="SOLID"/>
        <hh:rightBorder type="SOLID"/>
        <hh:topBorder type="SOLID"/>
        <hh:bottomBorder type="SOLID"/>
      </hh:borderFill>
    </hh:borderFills>
    <hh:numberings>
      <hh:numbering id="3" start="1">
        <hh:paraHead level="1" start="1" numFormat="DIGIT">^1.</hh:paraHead>
        <hh:paraHead level="2" start="1" numFormat="DIGIT">^1.^2.</hh:paraHead>
        <hh:paraHead level="3" start="1" numFormat="CIRCLED_DIGIT">^3</hh:paraHead>
      </hh:numbering>
    </hh:numberings>
    <hh:paraProperties>
      <hh:paraPr id="0"><hh:heading type="NONE" idRef="0" level="0"/></hh:paraPr>
      <hh:paraPr id="10"><hh:heading type="NUMBER" idRef="3" level="0"/></hh:paraPr>
      <hh:paraPr id="11"><hh:heading type="NUMBER" idRef="3" level="1"/></hh:paraPr>
      <hh:paraPr id="12"><hh:heading type="NUMBER" idRef="3" level="2"/></hh:paraPr>
    </hh:paraProperties>
    <hh:styles>
      <hh:style id="0" name="바탕글" engName="Normal" paraPrIDRef="0" charPrIDRef="0"/>
      <hh:style id="17" name="차례 제목" engName="TOC Heading" paraPrIDRef="0" charPrIDRef="0"/>
      <hh:style id="18" name="차례 1" engName="TOC 1" paraPrIDRef="0" charPrIDRef="0"/>
      <hh:style id="19" name="차례 2" engName="TOC 2" paraPrIDRef="0" charPrIDRef="0"/>
      <hh:style id="20" name="차례 3" engName="TOC 3" paraPrIDRef="0" charPrIDRef="0"/>
    </hh:styles>
  </hh:refList>
</hh:head>"""

    section0_xml = """<?xml version="1.0" encoding="UTF-8"?>
<hs:sec xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section"
        xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph"
        xmlns:hc="http://www.hancom.co.kr/hwpml/2011/core">
  <hp:p paraPrIDRef="0" styleIDRef="17">
    <hp:run charPrIDRef="0">
      <hp:secPr>
        <hp:pagePr landscape="WIDELY" width="59528" height="84188">
          <hp:margin left="8504" right="8504" top="5668" bottom="4252"/>
        </hp:pagePr>
      </hp:secPr>
      <hp:t>문서 최상위 제목</hp:t>
    </hp:run>
  </hp:p>
  <hp:p paraPrIDRef="0" styleIDRef="18"><hp:run><hp:t>제1장 추진 배경</hp:t></hp:run></hp:p>
  <hp:p paraPrIDRef="0" styleIDRef="19"><hp:run><hp:t>1절 주요 현황</hp:t></hp:run></hp:p>
  <hp:p paraPrIDRef="0" styleIDRef="20"><hp:run><hp:t>세부 분석 항목</hp:t></hp:run></hp:p>

  <!-- 빈 NUMBER 문단(카운터가 잘못 증가하면 안 됨) + ^1.^2. / 원문자 번호 -->
  <hp:p paraPrIDRef="10" styleIDRef="0"><hp:run><hp:t/></hp:run></hp:p>
  <hp:p paraPrIDRef="10" styleIDRef="0"><hp:run><hp:t>상위 번호</hp:t></hp:run></hp:p>
  <hp:p paraPrIDRef="11" styleIDRef="0"><hp:run><hp:t>하위 번호 첫째</hp:t></hp:run></hp:p>
  <hp:p paraPrIDRef="11" styleIDRef="0"><hp:run><hp:t>하위 번호 둘째</hp:t></hp:run></hp:p>
  <hp:p paraPrIDRef="12" styleIDRef="0"><hp:run><hp:t>원문자 항목</hp:t></hp:run></hp:p>

  <!-- 하이퍼링크, 덧말, 글자겹침, 글맵시, 양식 컨트롤, hypen 오타 태그 -->
  <hp:p paraPrIDRef="0" styleIDRef="0">
    <hp:run>
      <hp:t>자세한 사항은 </hp:t>
      <hp:ctrl>
        <hp:fieldBegin id="1" type="HYPERLINK">
          <hp:parameters>
            <hp:stringParam name="Command">|https\\://www.hancom.com/;0;0;0;</hp:stringParam>
            <hp:stringParam name="ToolTip">https://www.hancom.com/</hp:stringParam>
          </hp:parameters>
        </hp:fieldBegin>
      </hp:ctrl>
      <hp:t>공식 사이트</hp:t>
      <hp:ctrl><hp:fieldEnd beginIDRef="1"/></hp:ctrl>
      <hp:t>와 </hp:t>
      <hp:dutmal><hp:mainText>본말</hp:mainText><hp:subText>닷말</hp:subText></hp:dutmal>
      <hp:t> 및 </hp:t>
      <hp:compose composeText="AO"/>
      <hp:t> auto<hp:hypen/>etl 확인</hp:t>
    </hp:run>
  </hp:p>
  <hp:p paraPrIDRef="0" styleIDRef="0">
    <hp:run>
      <hp:btn caption="제출 버튼"/>
      <hp:checkBtn caption="동의함" value="CHECKED"/>
      <hp:radioBtn caption="옵션A" value="UNCHECKED"/>
      <hp:comboBox selectedValue="서울특별시"/>
      <hp:edit><hp:text>2026-001</hp:text></hp:edit>
      <hp:textart text="글맵시 제목&#x240D;&#x240A;부제목"/>
    </hp:run>
  </hp:p>

  <!-- 확장 한컴/LaTeX 혼용 수식 -->
  <hp:p paraPrIDRef="0" styleIDRef="0">
    <hp:run>
      <hp:equation>
        <hp:script>LIM_{x -> 0} {sin x} over x + INT_a^b f(x) dx</hp:script>
      </hp:equation>
    </hp:run>
  </hp:p>
  <hp:p paraPrIDRef="0" styleIDRef="0">
    <hp:run>
      <hp:t>세제곱근: </hp:t>
      <hp:equation><hp:script>\\sqrt[3]{x}</hp:script></hp:equation>
    </hp:run>
  </hp:p>

  <!-- 한국 공공문서 장식 표: 상단 여백 행 + 중앙 배지 + 하단 3열 병합 본문 -->
  <hp:p paraPrIDRef="0" styleIDRef="0">
    <hp:run>
      <hp:tbl rowCnt="4" colCnt="3" borderFillIDRef="1">
        <hp:tr>
          <hp:tc borderFillIDRef="1">
            <hp:cellAddr colAddr="0" rowAddr="0"/>
            <hp:cellSpan colSpan="3" rowSpan="1"/>
            <hp:subList><hp:p><hp:run><hp:t/></hp:run></hp:p></hp:subList>
          </hp:tc>
        </hp:tr>
        <hp:tr>
          <hp:tc borderFillIDRef="1">
            <hp:cellAddr colAddr="0" rowAddr="1"/>
            <hp:cellSpan colSpan="1" rowSpan="1"/>
            <hp:subList><hp:p/></hp:subList>
          </hp:tc>
          <hp:tc borderFillIDRef="1">
            <hp:cellAddr colAddr="1" rowAddr="1"/>
            <hp:cellSpan colSpan="1" rowSpan="2"/>
            <hp:subList>
              <hp:p><hp:run><hp:t>&lt;주요 기능 요약&gt;</hp:t></hp:run></hp:p>
            </hp:subList>
          </hp:tc>
          <hp:tc borderFillIDRef="1">
            <hp:cellAddr colAddr="2" rowAddr="1"/>
            <hp:cellSpan colSpan="1" rowSpan="1"/>
            <hp:subList><hp:p/></hp:subList>
          </hp:tc>
        </hp:tr>
        <hp:tr>
          <hp:tc borderFillIDRef="1">
            <hp:cellAddr colAddr="0" rowAddr="2"/>
            <hp:cellSpan colSpan="1" rowSpan="1"/>
            <hp:subList><hp:p/></hp:subList>
          </hp:tc>
          <hp:tc borderFillIDRef="1">
            <hp:cellAddr colAddr="2" rowAddr="2"/>
            <hp:cellSpan colSpan="1" rowSpan="1"/>
            <hp:subList><hp:p/></hp:subList>
          </hp:tc>
        </hp:tr>
        <hp:tr>
          <hp:tc borderFillIDRef="1">
            <hp:cellAddr colAddr="0" rowAddr="3"/>
            <hp:cellSpan colSpan="3" rowSpan="1"/>
            <hp:subList>
              <hp:p><hp:run><hp:t>국민 안전관리 및 디지털 정부 혁신</hp:t></hp:run></hp:p>
            </hp:subList>
          </hp:tc>
        </hp:tr>
      </hp:tbl>
    </hp:run>
  </hp:p>

  <!-- 단일 행 배너 표(col_1 없이 실제 텍스트가 헤더가 되어야 함) -->
  <hp:p paraPrIDRef="0" styleIDRef="0">
    <hp:run>
      <hp:tbl rowCnt="1" colCnt="1" borderFillIDRef="1">
        <hp:tr>
          <hp:tc borderFillIDRef="1">
            <hp:cellAddr colAddr="0" rowAddr="0"/>
            <hp:cellSpan colSpan="1" rowSpan="1"/>
            <hp:subList><hp:p><hp:run><hp:t>2026년 보도자료</hp:t></hp:run></hp:p></hp:subList>
          </hp:tc>
        </hp:tr>
      </hp:tbl>
    </hp:run>
  </hp:p>

  <!-- 장식용 컨테이너 3종(소형 불릿 아이콘·얇은 구분선·단행 제목 배너) -> VECTOR_DIAGRAM 제외 -->
  <hp:p paraPrIDRef="0" styleIDRef="0">
    <hp:run>
      <hp:container id="orn1">
        <hp:curSz width="1379" height="1461"/>
        <hp:sz width="1381" height="1461"/>
        <hp:polygon/><hp:polygon/><hp:rect/>
      </hp:container>
      <hp:container id="div1">
        <hp:curSz width="47315" height="0"/>
        <hp:sz width="47315" height="760"/>
        <hp:rect/><hp:rect/><hp:rect/><hp:rect/>
      </hp:container>
      <hp:container id="ban1">
        <hp:curSz width="0" height="3135"/>
        <hp:sz width="48635" height="3135"/>
        <hp:rect>
          <hp:drawText>
            <hp:subList><hp:p><hp:run><hp:t>배너 제목 텍스트</hp:t></hp:run></hp:p></hp:subList>
          </hp:drawText>
        </hp:rect>
        <hp:polygon/><hp:polygon/>
      </hp:container>
    </hp:run>
  </hp:p>
</hs:sec>"""

    # Section 1: KS X 6101 가로(Landscape) 용지 — landscape="NARROWLY"
    section1_xml = """<?xml version="1.0" encoding="UTF-8"?>
<hs:sec xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section"
        xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph">
  <hp:p paraPrIDRef="0" styleIDRef="0">
    <hp:run>
      <hp:secPr>
        <hp:pagePr landscape="NARROWLY" width="72852" height="103180">
          <hp:margin left="8504" right="8504" top="5668" bottom="4252"/>
        </hp:pagePr>
      </hp:secPr>
      <hp:t>가로 용지(B4 Landscape) 섹션 본문</hp:t>
    </hp:run>
  </hp:p>
</hs:sec>"""

    with zipfile.ZipFile(target_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mimetype", "application/hwp+zip")
        zf.writestr("version.xml", version_xml)
        zf.writestr("META-INF/container.xml", container_xml)
        zf.writestr("Contents/content.hpf", content_hpf)
        zf.writestr("Contents/header.xml", header_xml)
        zf.writestr("Contents/section0.xml", section0_xml)
        zf.writestr("Contents/section1.xml", section1_xml)

    plan = inspect_document(target_path)
    doc, manifest = parse_and_export(target_path, tmp_path / "edge_out")

    # 1. content.hpf lastsaveby 폴백 + rhwp creator 메타데이터
    assert doc.document.title == "공공문서 실전 엣지케이스 검증"
    assert doc.document.author == "MOLIT"
    assert doc.document.creator is not None and doc.document.creator.startswith("rhwp/")
    assert len(plan.pages) == 2
    assert plan.pages[1].metrics.width_pt == pytest.approx(1031.8, abs=1.0)
    assert plan.pages[1].metrics.height_pt == pytest.approx(728.52, abs=1.0)
    assert plan.pages[1].metrics.width_pt > plan.pages[1].metrics.height_pt

    # 2. 본문 개요 및 텍스트 추출 확인
    p1_joined = "\n".join(b.text for b in doc.pages[0].blocks)
    p2_joined = "\n".join(b.text for b in doc.pages[1].blocks)
    assert "문서 최상위 제목" in p1_joined
    assert "상위 번호" in p1_joined
    assert "하위 번호 첫째" in p1_joined
    assert "가로 용지(B4 Landscape) 섹션 본문" in p2_joined
    assert "LIM_(x -> 0)" in p1_joined or "lim_{x -> 0}" in p1_joined

    # 3. 표 추출 및 RhwpPageRenderer 네이티브 PNG 렌더링
    assert len(doc.pages[0].tables) >= 2
    assert any(
        "국민 안전관리 및 디지털 정부 혁신" in " ".join(r)
        for t in doc.pages[0].tables
        for r in t.rows
    )
    assert any("2026년 보도자료" in " ".join(t.headers) for t in doc.pages[0].tables)
    assert manifest.document_markdown.is_file()

    rendered = RhwpPageRenderer().render_region(target_path, 1, None, dpi=96)
    assert rendered is not None
    assert rendered.data.startswith(b"\x89PNG\r\n\x1a\n")


def test_hwpx_review_regression_guards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """병합 1x3 표 보존, 셀 중첩 그림·주석, SVG 래스터화, 소형 이미지 비전, ID 무충돌 검증."""
    import rhwp

    small_png = _make_solid_png(120, 80, (30, 120, 200))
    scan_png = _make_synthetic_scanned_page_png(360, 480)
    svg_bytes = (
        b'<svg xmlns="http://www.w3.org/2000/svg" width="80" height="40">'
        b'<rect width="80" height="40"/></svg>'
    )

    container_xml = """<?xml version="1.0" encoding="UTF-8"?>
<ocf:container xmlns:ocf="urn:oasis:names:tc:opendocument:xmlns:container">
  <ocf:rootfiles>
    <ocf:rootfile full-path="Contents/content.hpf" media-type="application/hwpml-package+xml"/>
  </ocf:rootfiles>
</ocf:container>"""

    content_hpf = """<?xml version="1.0" encoding="UTF-8"?>
<opf:package xmlns:opf="http://www.idpf.org/2007/opf/" version="1.0">
  <opf:metadata><opf:title>리뷰 회귀 검증</opf:title></opf:metadata>
  <opf:manifest>
    <opf:item id="header" href="Contents/header.xml" media-type="application/xml"/>
    <opf:item id="section0" href="Contents/section0.xml" media-type="application/xml"/>
    <opf:item id="section1" href="Contents/section1.xml" media-type="application/xml"/>
    <opf:item id="img1" href="BinData/image1.png" media-type="image/png"/>
    <opf:item id="scan2" href="BinData/scan2.png" media-type="image/png"/>
    <opf:item id="vec3" href="BinData/vec3.svg" media-type="image/svg+xml"/>
  </opf:manifest>
  <opf:spine>
    <opf:itemref idref="header"/>
    <opf:itemref idref="section0"/>
    <opf:itemref idref="section1"/>
  </opf:spine>
</opf:package>"""

    header_xml = """<?xml version="1.0" encoding="UTF-8"?>
<hh:head xmlns:hh="http://www.hancom.co.kr/hwpml/2011/head">
  <hh:refList>
    <hh:charProperties><hh:charPr id="0" height="1000"/></hh:charProperties>
    <hh:borderFills>
      <hh:borderFill id="1">
        <hh:leftBorder type="SOLID"/><hh:rightBorder type="SOLID"/>
        <hh:topBorder type="SOLID"/><hh:bottomBorder type="SOLID"/>
      </hh:borderFill>
    </hh:borderFills>
    <hh:paraProperties><hh:paraPr id="0"/></hh:paraProperties>
    <hh:styles>
      <hh:style id="0" name="바탕글" engName="Normal" paraPrIDRef="0" charPrIDRef="0"/>
    </hh:styles>
  </hh:refList>
</hh:head>"""

    # Page 1: col_span=3 1행 병합표 + 표 셀 안 그림/숨은주석 + Page 2 대형 이미지 뒤 소형 그림
    section0_xml = """<?xml version="1.0" encoding="UTF-8"?>
<hs:sec xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section"
        xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph"
        xmlns:hc="http://www.hancom.co.kr/hwpml/2011/core">
  <hp:p paraPrIDRef="0" styleIDRef="0">
    <hp:run>
      <hp:secPr><hp:pagePr landscape="WIDELY" width="59528" height="84188"/></hp:secPr>
      <hp:tbl rowCnt="1" colCnt="3" borderFillIDRef="1">
        <hp:tr>
          <hp:tc borderFillIDRef="1">
            <hp:cellAddr colAddr="0" rowAddr="0"/><hp:cellSpan colSpan="3" rowSpan="1"/>
            <hp:subList><hp:p><hp:run><hp:t>전체 제목</hp:t></hp:run></hp:p></hp:subList>
          </hp:tc>
        </hp:tr>
      </hp:tbl>
    </hp:run>
  </hp:p>
  <hp:p paraPrIDRef="0" styleIDRef="0">
    <hp:run>
      <hp:tbl rowCnt="1" colCnt="1" borderFillIDRef="1">
        <hp:tr>
          <hp:tc borderFillIDRef="1">
            <hp:cellAddr colAddr="0" rowAddr="0"/><hp:cellSpan colSpan="1" rowSpan="1"/>
            <hp:subList>
              <hp:p>
                <hp:run>
                  <hp:pic id="cell_pic"><hc:img binaryItemIDRef="img1"/></hp:pic>
                  <hp:ctrl>
                    <hp:hiddenComment>
                      <hp:subList>
                        <hp:p><hp:run><hp:t>셀 내부 주석</hp:t></hp:run></hp:p>
                      </hp:subList>
                    </hp:hiddenComment>
                  </hp:ctrl>
                </hp:run>
              </hp:p>
            </hp:subList>
          </hp:tc>
        </hp:tr>
      </hp:tbl>
    </hp:run>
  </hp:p>
  <hp:p paraPrIDRef="0" styleIDRef="0" pageBreak="1">
    <hp:run>
      <hp:pic id="big_scan">
        <hp:curSz width="44000" height="62000"/><hc:img binaryItemIDRef="scan2"/>
      </hp:pic>
      <hp:pic id="small_tail">
        <hp:curSz width="8000" height="5000"/><hc:img binaryItemIDRef="img1"/>
      </hp:pic>
    </hp:run>
  </hp:p>
</hs:sec>"""

    # Page 3: 텍스트 없이 소형 그림 1개 + SVG 1개
    section1_xml = """<?xml version="1.0" encoding="UTF-8"?>
<hs:sec xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section"
        xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph"
        xmlns:hc="http://www.hancom.co.kr/hwpml/2011/core">
  <hp:p paraPrIDRef="0" styleIDRef="0">
    <hp:run>
      <hp:secPr><hp:pagePr landscape="WIDELY" width="59528" height="84188"/></hp:secPr>
      <hp:pic id="only_small">
        <hp:curSz width="8000" height="5000"/><hc:img binaryItemIDRef="img1"/>
      </hp:pic>
      <hp:pic id="vec_svg">
        <hp:curSz width="12000" height="8000"/><hc:img binaryItemIDRef="vec3"/>
      </hp:pic>
    </hp:run>
  </hp:p>
</hs:sec>"""

    hwpx_path = tmp_path / "regression.hwpx"
    with zipfile.ZipFile(hwpx_path, "w") as zf:
        zf.writestr("mimetype", "application/hwp+zip")
        zf.writestr("META-INF/container.xml", container_xml)
        zf.writestr("Contents/content.hpf", content_hpf)
        zf.writestr("Contents/header.xml", header_xml)
        zf.writestr("Contents/section0.xml", section0_xml)
        zf.writestr("Contents/section1.xml", section1_xml)
        zf.writestr("BinData/image1.png", small_png)
        zf.writestr("BinData/scan2.png", scan_png)
        zf.writestr("BinData/vec3.svg", svg_bytes)

    parse_calls = 0
    orig_parse = rhwp.parse

    def counted_parse(path: str) -> rhwp.Document:
        nonlocal parse_calls
        parse_calls += 1
        return orig_parse(path)

    monkeypatch.setattr(rhwp, "parse", counted_parse)

    captured_mimes: list[str] = []

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
        assert isinstance(user_content, list)
        for part in user_content:
            if isinstance(part, dict) and part.get("type") == "image_url":
                img_url = part.get("image_url")
                if isinstance(img_url, dict):
                    captured_mimes.append(str(img_url.get("url", ""))[:22])
        return {"choices": [{"message": {"content": "VLM ok"}}]}

    monkeypatch.setattr("autoetl.parser.vision._open_json_request", fake_open_json)

    vlm = OpenAICompatibleVlmAdapter(model="TestVLM", base_url="http://127.0.0.1:8080/v1")
    doc, _ = parse_and_export(
        hwpx_path,
        tmp_path / "reg_out",
        options=ParseOptions(extract_crops=True),
        layout_detector=HeuristicRasterLayoutDetector(),
        page_renderer=RhwpPageRenderer(),
        vision_adapter=vlm,
    )

    assert parse_calls == 1  # RhwpPageRenderer 사용 시 문서 전체 재파싱 0회

    # 1x3 병합 표 열 보존 + 표 셀 중첩 PictureBlock 추출
    t0 = doc.pages[0].tables[0]
    assert t0.col_count == 3
    assert t0.cells[0].col_span == 3
    assert len(doc.pages[0].visual_regions) >= 1

    # Page 2: 대형 이미지 분할 후 소형 그림 ID와 중복되지 않아야 함
    p2_ids = [v.region_id for v in doc.pages[1].visual_regions]
    assert len(p2_ids) == len(set(p2_ids)) and len(p2_ids) >= 3

    # Page 3: 텍스트 없는 이미지 전용 페이지도 vision_required_pages 포함 + SVG는 PNG 래스터화
    assert 3 in doc.routing_plan.summary.vision_required_pages
    assert doc.pages[2].route.requires_vision is True
    assert all(m.startswith("data:image/png;base64") for m in captured_mimes)

    # PageCategory enum & include_comments=False 시 셀 중첩 주석 필터 확인
    assert PageCategory.SCANNED_IMAGE.value == "scanned_image"
    from autoetl.parser import parse_document

    doc_no_comments = parse_document(hwpx_path, options=ParseOptions(include_comments=False))
    t1_no_cmt = doc_no_comments.pages[0].tables[1]
    assert "셀 내부 주석" not in (t1_no_cmt.headers[0] if t1_no_cmt.headers else "")


def test_hwpx_render_cache_section_headers_and_page_mismatch_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """페이지당 전체 렌더 1회 캐시·섹션별 머리글 분리·조판 페이지 불일치 경고를 검증한다."""
    import rhwp

    from autoetl.parser import parse_document

    svg_bytes = (
        b'<svg xmlns="http://www.w3.org/2000/svg" width="80" height="40">'
        b'<rect width="80" height="40"/></svg>'
    )
    container_xml = """<?xml version="1.0" encoding="UTF-8"?>
<ocf:container xmlns:ocf="urn:oasis:names:tc:opendocument:xmlns:container">
  <ocf:rootfiles>
    <ocf:rootfile full-path="Contents/content.hpf" media-type="application/hwpml-package+xml"/>
  </ocf:rootfiles>
</ocf:container>"""
    content_hpf = """<?xml version="1.0" encoding="UTF-8"?>
<opf:package xmlns:opf="http://www.idpf.org/2007/opf/" version="1.0">
  <opf:metadata><opf:title>캐시 및 섹션 머리글 검증</opf:title></opf:metadata>
  <opf:manifest>
    <opf:item id="header" href="Contents/header.xml" media-type="application/xml"/>
    <opf:item id="section0" href="Contents/section0.xml" media-type="application/xml"/>
    <opf:item id="section1" href="Contents/section1.xml" media-type="application/xml"/>
    <opf:item id="vec1" href="BinData/vec1.svg" media-type="image/svg+xml"/>
    <opf:item id="vec2" href="BinData/vec2.svg" media-type="image/svg+xml"/>
  </opf:manifest>
  <opf:spine>
    <opf:itemref idref="header"/>
    <opf:itemref idref="section0"/>
    <opf:itemref idref="section1"/>
  </opf:spine>
</opf:package>"""
    header_xml = """<?xml version="1.0" encoding="UTF-8"?>
<hh:head xmlns:hh="http://www.hancom.co.kr/hwpml/2011/head">
  <hh:refList>
    <hh:charProperties><hh:charPr id="0" height="1000"/></hh:charProperties>
    <hh:paraProperties><hh:paraPr id="0"/></hh:paraProperties>
    <hh:styles>
      <hh:style id="0" name="바탕글" engName="Normal" paraPrIDRef="0" charPrIDRef="0"/>
    </hh:styles>
  </hh:refList>
</hh:head>"""
    section0_xml = """<?xml version="1.0" encoding="UTF-8"?>
<hs:sec xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section"
        xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph"
        xmlns:hc="http://www.hancom.co.kr/hwpml/2011/core">
  <hp:p paraPrIDRef="0" styleIDRef="0">
    <hp:run>
      <hp:secPr><hp:pagePr landscape="WIDELY" width="59528" height="84188"/></hp:secPr>
      <hp:ctrl>
        <hp:header>
          <hp:subList><hp:p><hp:run><hp:t>섹션0 머리글</hp:t></hp:run></hp:p></hp:subList>
        </hp:header>
      </hp:ctrl>
      <hp:pic id="svg_a">
        <hp:curSz width="12000" height="8000"/><hc:img binaryItemIDRef="vec1"/>
      </hp:pic>
      <hp:pic id="svg_b">
        <hp:curSz width="12000" height="8000"/><hc:img binaryItemIDRef="vec2"/>
      </hp:pic>
    </hp:run>
  </hp:p>
</hs:sec>"""
    section1_xml = """<?xml version="1.0" encoding="UTF-8"?>
<hs:sec xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section"
        xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph"
        xmlns:hc="http://www.hancom.co.kr/hwpml/2011/core">
  <hp:p paraPrIDRef="0" styleIDRef="0">
    <hp:run>
      <hp:secPr><hp:pagePr landscape="WIDELY" width="59528" height="84188"/></hp:secPr>
      <hp:ctrl>
        <hp:header>
          <hp:subList><hp:p><hp:run><hp:t>섹션1 머리글</hp:t></hp:run></hp:p></hp:subList>
        </hp:header>
      </hp:ctrl>
      <hp:pic id="svg_c">
        <hp:curSz width="12000" height="8000"/><hc:img binaryItemIDRef="vec1"/>
      </hp:pic>
    </hp:run>
  </hp:p>
</hs:sec>"""

    hwpx_path = tmp_path / "cache_hdr.hwpx"
    with zipfile.ZipFile(hwpx_path, "w") as zf:
        zf.writestr("mimetype", "application/hwp+zip")
        zf.writestr("META-INF/container.xml", container_xml)
        zf.writestr("Contents/content.hpf", content_hpf)
        zf.writestr("Contents/header.xml", header_xml)
        zf.writestr("Contents/section0.xml", section0_xml)
        zf.writestr("Contents/section1.xml", section1_xml)
        zf.writestr("BinData/vec1.svg", svg_bytes)
        zf.writestr("BinData/vec2.svg", svg_bytes)

    render_calls_by_page: dict[int, int] = {}
    orig_render_region = RhwpPageRenderer.render_document_region

    def counting_render_region(
        doc_obj: object,
        page_number: int,
        bbox: object,
        *,
        dpi: int = 150,
    ) -> object:
        render_calls_by_page[page_number] = render_calls_by_page.get(page_number, 0) + 1
        return orig_render_region(doc_obj, page_number, bbox, dpi=dpi)  # type: ignore[arg-type]

    monkeypatch.setattr(RhwpPageRenderer, "render_document_region", counting_render_region)

    doc_cached = parse_document(hwpx_path, options=ParseOptions(extract_crops=True))
    # Page 1에 SVG가 2개 있어도 전체 페이지 렌더는 1회만 호출
    assert render_calls_by_page.get(1) == 1
    assert render_calls_by_page.get(2) == 1
    # 섹션별 머리글 분리
    p1_hf = [b.text for b in doc_cached.pages[0].header_footer_blocks]
    p2_hf = [b.text for b in doc_cached.pages[1].header_footer_blocks]
    assert p1_hf == ["섹션0 머리글"]
    assert p2_hf == ["섹션1 머리글"]

    # 논리 페이지 수(2) != 조판 페이지 수(1) 불일치 시 Page 2 렌더링 건너뛰고 경고 기록
    orig_parse = rhwp.parse

    class _MismatchDocProxy:
        def __init__(self, inner: object) -> None:
            self._inner = inner

        @property
        def page_count(self) -> int:
            return 1

        def __getattr__(self, name: str) -> object:
            return getattr(self._inner, name)

    monkeypatch.setattr(rhwp, "parse", lambda path: _MismatchDocProxy(orig_parse(path)))
    doc_mismatch = parse_document(hwpx_path, options=ParseOptions(extract_crops=True))
    assert doc_mismatch.pages[1].visual_regions[0].crop_status == CropStatus.RENDERER_UNAVAILABLE
    assert any("조판 페이지 수(1)" in w for w in doc_mismatch.warnings)

    # rhwp 런타임 로드 실패 시 원시 ImportError가 아닌 ParserInputError로 일관 변환되어야 함
    def _fail_rhwp() -> object:
        raise ImportError("missing libfreetype6")

    monkeypatch.setattr("autoetl.parser.hwp.adapter.load_rhwp", _fail_rhwp)
    monkeypatch.setattr("autoetl.parser.hwp.adapter.load_rhwp_nodes", _fail_rhwp)
    with pytest.raises(ParserInputError, match="rhwp"):
        parse_document(hwpx_path)
