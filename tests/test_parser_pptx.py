"""Auto Parser PPTX(OOXML) 어댑터 구조 분류·표/차트/SmartArt/도식·레이아웃 탐지·CLI 테스트."""

from __future__ import annotations

import io
import json
import struct
import threading
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from autoetl.cli import main as cli_main
from autoetl.parser import (
    BBox,
    CropStatus,
    ElementKind,
    HeuristicRasterLayoutDetector,
    LibreOfficePptxRenderer,
    OpenAICompatibleVlmAdapter,
    PageCategory,
    ParseOptions,
    ParserInputError,
    ParseStrategy,
    PptxDocumentAdapter,
    SourceFormat,
    VisionStatus,
    convert_ppt_to_pptx,
    inspect_document,
    parse_and_export,
    parse_document,
    supported_extensions,
)
from autoetl.parser.image_io import EncodedImage, decode_png, encode_png
from autoetl.parser.pptx.ooxml import EMU_PER_PT, OLE_MAGIC, probe_image_dimensions

# ---------------------------------------------------------------------------
# 헬퍼: 현실적 다중 슬라이드 OOXML .pptx 빌더 & 루프백 Mock VLM 서버
# ---------------------------------------------------------------------------


def _emu(pt: float) -> int:
    return round(pt * EMU_PER_PT)


def _draw_rect(
    buf: bytearray,
    width: int,
    height: int,
    x0: int,
    y0: int,
    x1: int,
    y1: int,
    rgb: tuple[int, int, int],
) -> None:
    r, g, b = rgb
    for y in range(max(0, y0), min(height, y1)):
        row_off = y * width * 3
        for x in range(max(0, x0), min(width, x1)):
            off = row_off + x * 3
            buf[off] = r
            buf[off + 1] = g
            buf[off + 2] = b


def _draw_text_line(
    buf: bytearray, width: int, height: int, x0: int, y0: int, x1: int, line_h: int
) -> None:
    x = x0
    toggle = 0
    while x < x1 - 4:
        gw = 5 if (toggle % 3) != 0 else 8
        _draw_rect(buf, width, height, x, y0, min(x1, x + gw), y0 + line_h, (20, 20, 20))
        x += gw + (6 if (toggle % 4) == 3 else 3)
        toggle += 1


def _make_screenshot_slide_png(width: int = 600, height: int = 840) -> bytes:
    """제목 + 문단 + 3x3 격자표 + 컬러 막대 차트가 포함된 스캔/스크린샷 PNG를 생성한다."""
    buf = bytearray([255, 255, 255] * (width * height))
    _draw_text_line(buf, width, height, 60, 45, 360, 20)
    for idx in range(3):
        _draw_text_line(buf, width, height, 60, 120 + idx * 20, 520, 10)
    tx0, ty0, tx1, ty1 = 60, 240, 540, 440
    for hy in (ty0, 290, 340, 390, ty1):
        _draw_rect(buf, width, height, tx0, hy, tx1, hy + 2, (15, 15, 15))
    for vx in (tx0, 220, 380, tx1):
        _draw_rect(buf, width, height, vx, ty0, vx + 2, ty1 + 2, (15, 15, 15))
    for ry in (260, 310, 360, 410):
        _draw_text_line(buf, width, height, 75, ry, 195, 9)
        _draw_text_line(buf, width, height, 235, ry, 355, 9)
        _draw_text_line(buf, width, height, 395, ry, 515, 9)
    cx0, cy0, cx1, cy1 = 80, 520, 520, 760
    _draw_rect(buf, width, height, cx0, cy0, cx0 + 3, cy1, (20, 20, 20))
    _draw_rect(buf, width, height, cx0, cy1 - 3, cx1, cy1, (20, 20, 20))
    _draw_rect(buf, width, height, 130, 620, 190, cy1 - 4, (30, 110, 220))
    _draw_rect(buf, width, height, 240, 560, 300, cy1 - 4, (220, 70, 50))
    _draw_rect(buf, width, height, 350, 590, 410, cy1 - 4, (40, 165, 95))
    return encode_png(bytes(buf), width, height, channels=3)


def _make_solid_png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    return encode_png(bytes(rgb) * (width * height), width, height, channels=3)


def _rels_xml(entries: list[tuple[str, str, str]]) -> str:
    items = "\n".join(
        f'  <Relationship Id="{rid}" Type="{rtype}" Target="{target}"/>'
        for rid, rtype, target in entries
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
        f"{items}\n"
        "</Relationships>"
    )


def _wrap_slide(sp_tree_inner: str, *, show: str | None = None) -> str:
    show_attr = f' show="{show}"' if show is not None else ""
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
        'xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" '
        'xmlns:dgm="http://schemas.openxmlformats.org/drawingml/2006/diagram" '
        'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
        f'xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math"{show_attr}>\n'
        "  <p:cSld>\n"
        "    <p:spTree>\n"
        '      <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>\n'
        "      <p:grpSpPr/>\n"
        f"{sp_tree_inner}\n"
        "    </p:spTree>\n"
        "  </p:cSld>\n"
        "</p:sld>"
    )


def _xfrm(x: float, y: float, w: float, h: float) -> str:
    return (
        f'<a:xfrm><a:off x="{_emu(x)}" y="{_emu(y)}"/>'
        f'<a:ext cx="{_emu(w)}" cy="{_emu(h)}"/></a:xfrm>'
    )


def _tc(text: str, *, attrs: str = "") -> str:
    attr_part = f" {attrs}" if attrs else ""
    body = f"<a:p><a:r><a:t>{text}</a:t></a:r></a:p>" if text else "<a:p/>"
    return f"<a:tc{attr_part}><a:txBody><a:bodyPr/><a:lstStyle/>{body}</a:txBody></a:tc>"


def _dgm_pt(mid: str, text: str) -> str:
    return (
        f'    <dgm:pt modelId="{mid}" type="node">'
        f"<dgm:t><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>{text}</a:t></a:r></a:p></dgm:t>"
        f"</dgm:pt>\n"
    )


def _pic_xml(
    sid: int,
    name: str,
    rid: str,
    x: float,
    y: float,
    w: float,
    h: float,
    *,
    descr: str = "",
) -> str:
    descr_attr = f' descr="{descr}"' if descr else ""
    return (
        f"<p:pic>"
        f'<p:nvPicPr><p:cNvPr id="{sid}" name="{name}"{descr_attr}/>'
        f"<p:cNvPicPr/><p:nvPr/></p:nvPicPr>"
        f'<p:blipFill><a:blip r:embed="{rid}"/></p:blipFill>'
        f"<p:spPr>{_xfrm(x, y, w, h)}</p:spPr>"
        f"</p:pic>"
    )


def _build_rich_demo_pptx(path: Path) -> Path:
    """8개 실전 슬라이드 아키타입(표지·2단·병합표·차트·도식·그림·스캔·빈슬라이드) PPTX 생성."""
    rel_slide = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide"
    rel_layout = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout"
    rel_master = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster"
    rel_image = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
    rel_chart = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/chart"
    rel_dgm_data = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/diagramData"
    rel_notes = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/notesSlide"

    logo_png = _make_solid_png(48, 24, (10, 40, 120))
    bg_png = _make_solid_png(64, 36, (245, 248, 255))
    chart_pic_png = _make_solid_png(220, 140, (40, 120, 210))
    screenshot_png = _make_screenshot_slide_png(600, 840)

    # 마스터 & 레이아웃(플레이스홀더 위치/글꼴 상속 테스트)
    master_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<p:sldMaster xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">\n'
        "  <p:cSld><p:spTree/></p:cSld>\n"
        "  <p:txStyles>\n"
        '    <p:titleStyle><a:lvl1pPr><a:defRPr sz="3200"/></a:lvl1pPr></p:titleStyle>\n'
        '    <p:bodyStyle><a:lvl1pPr><a:defRPr sz="1600"/></a:lvl1pPr></p:bodyStyle>\n'
        "  </p:txStyles>\n"
        "</p:sldMaster>"
    )
    layout_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<p:sldLayout xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">\n'
        "  <p:cSld><p:spTree>\n"
        "    <p:sp>\n"
        '      <p:nvSpPr><p:cNvPr id="2" name="Title Placeholder"/><p:cNvSpPr/>'
        '<p:nvPr><p:ph type="ctrTitle"/></p:nvPr></p:nvSpPr>\n'
        f"      <p:spPr>{_xfrm(60, 120, 840, 80)}</p:spPr>\n"
        '      <p:txBody><a:bodyPr/><a:lstStyle><a:lvl1pPr><a:defRPr sz="3600"/>'
        "</a:lvl1pPr></a:lstStyle><a:p/></p:txBody>\n"
        "    </p:sp>\n"
        "    <p:sp>\n"
        '      <p:nvSpPr><p:cNvPr id="3" name="Title Bar"/><p:cNvSpPr/>'
        '<p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr>\n'
        f"      <p:spPr>{_xfrm(40, 25, 880, 55)}</p:spPr>\n"
        "    </p:sp>\n"
        "  </p:spTree></p:cSld>\n"
        "</p:sldLayout>"
    )

    # Slide 1: 표지 + 전체 배경 벽지(장식 필터링) + 반복 로고(장식 필터링) + 상속 ctrTitle
    s1 = _wrap_slide(
        _pic_xml(10, "Wallpaper", "rIdBg", 0, 0, 960, 540)
        + _pic_xml(11, "CornerLogo", "rIdLogo", 850, 15, 80, 35)
        + "<p:sp>"
        '<p:nvSpPr><p:cNvPr id="12" name="Cover Title"/><p:cNvSpPr/>'
        '<p:nvPr><p:ph type="ctrTitle"/></p:nvPr></p:nvSpPr>'
        "<p:spPr/>"
        "<p:txBody><a:bodyPr/><a:lstStyle/>"
        '<a:p><a:r><a:rPr b="1"/><a:t>2026년 데이터 플랫폼 전략 보고서</a:t></a:r></a:p>'
        "</p:txBody></p:sp><p:sp>"
        '<p:nvSpPr><p:cNvPr id="13" name="Subtitle"/><p:cNvSpPr/>'
        '<p:nvPr><p:ph type="subTitle"/></p:nvPr></p:nvSpPr>'
        f"<p:spPr>{_xfrm(120, 230, 720, 60)}</p:spPr>"
        "<p:txBody><a:bodyPr/><a:lstStyle/>"
        '<a:p><a:r><a:rPr sz="2000"/>'
        "<a:t>Auto ETL 및 문서 구조 파싱 로드맵 — 2026년 4분기 보고 요약</a:t>"
        "</a:r></a:p></p:txBody></p:sp>"
    )

    # Slide 2: 2단 카드(중첩 grpSp 좌표 변환) + 계층 불릿 + mc:AlternateContent 수식 + 푸터 + 노트
    s2 = _wrap_slide(
        _pic_xml(20, "CornerLogo", "rIdLogo", 850, 15, 80, 35) + "<p:sp>"
        '<p:nvSpPr><p:cNvPr id="21" name="Slide2 Title"/><p:cNvSpPr/>'
        '<p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr>'
        "<p:spPr/>"
        "<p:txBody><a:bodyPr/><a:lstStyle/>"
        '<a:p><a:r><a:rPr sz="2800" b="1"/><a:t>핵심 아키텍처 2단 비교</a:t></a:r></a:p>'
        "</p:txBody></p:sp>"
        # 좌측 그룹 카드(x=40..440, y=110..420, chExt 2배 스케일)
         + "<p:grpSp>"
        '<p:nvGrpSpPr><p:cNvPr id="22" name="LeftCardGroup"/>'
        "<p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>"
        f'<p:grpSpPr><a:xfrm><a:off x="{_emu(40)}" y="{_emu(110)}"/>'
        f'<a:ext cx="{_emu(400)}" cy="{_emu(310)}"/>'
        f'<a:chOff x="0" y="0"/><a:chExt cx="{_emu(200)}" cy="{_emu(155)}"/>'
        "</a:xfrm></p:grpSpPr>"
        "<p:sp>"
        '<p:nvSpPr><p:cNvPr id="23" name="LeftCard"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>'
        f"<p:spPr>{_xfrm(0, 0, 200, 155)}</p:spPr>"
        "<p:txBody><a:bodyPr/><a:lstStyle/>"
        '<a:p><a:r><a:rPr sz="1800" b="1"/><a:t>좌측 트랙: 결정론 엔진</a:t></a:r></a:p>'
        '<a:p><a:pPr lvl="0"><a:buChar char="•"/></a:pPr>'
        '<a:r><a:rPr sz="1400"/><a:t>PDF 및 PPTX 네이티브 XML 직접 파싱</a:t></a:r></a:p>'
        '<a:p><a:pPr lvl="1"><a:buChar char="-"/></a:pPr>'
        '<a:r><a:rPr sz="1300"/><a:t>중첩 그룹 도형 좌표 변환 지원</a:t></a:r></a:p>'
        "</p:txBody></p:sp></p:grpSp>"
        # 우측 카드(x=520..920, y=110..420) + mc:AlternateContent 수식
         + "<p:sp>"
        '<p:nvSpPr><p:cNvPr id="24" name="RightCard"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>'
        f"<p:spPr>{_xfrm(520, 110, 400, 310)}</p:spPr>"
        "<p:txBody><a:bodyPr/><a:lstStyle/>"
        '<a:p><a:r><a:rPr sz="1800" b="1"/><a:t>우측 트랙: 시각·VLM 보강</a:t></a:r></a:p>'
        '<a:p><a:pPr lvl="0"><a:buChar char="•"/></a:pPr>'
        '<a:r><a:rPr sz="1400"/><a:t>스캔 슬라이드 레이아웃 자동 분할</a:t></a:r></a:p>'
        "<a:p><mc:AlternateContent>"
        "<mc:Choice><m:oMath><m:r><m:t>Accuracy = TP / (TP + FP)</m:t></m:r></m:oMath></mc:Choice>"
        "<mc:Fallback><a:r><a:t>DUPLICATE_FALLBACK</a:t></a:r></mc:Fallback>"
        "</mc:AlternateContent></a:p>"
        "</p:txBody></p:sp>"
        # 바닥글 & 슬라이드 번호
         + "<p:sp>"
        '<p:nvSpPr><p:cNvPr id="25" name="Footer"/><p:cNvSpPr/>'
        '<p:nvPr><p:ph type="ftr"/></p:nvPr></p:nvSpPr>'
        f"<p:spPr>{_xfrm(40, 505, 400, 24)}</p:spPr>"
        "<p:txBody><a:bodyPr/><a:lstStyle/>"
        "<a:p><a:r><a:t>사내 대외비</a:t></a:r></a:p></p:txBody></p:sp>" + "<p:sp>"
        '<p:nvSpPr><p:cNvPr id="26" name="SlideNum"/><p:cNvSpPr/>'
        '<p:nvPr><p:ph type="sldNum"/></p:nvPr></p:nvSpPr>'
        f"<p:spPr>{_xfrm(880, 505, 40, 24)}</p:spPr>"
        "<p:txBody><a:bodyPr/><a:lstStyle/>"
        "<a:p><a:r><a:t>2</a:t></a:r></a:p></p:txBody></p:sp>"
    )
    notes2_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<p:notes xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">\n'
        "  <p:cSld><p:spTree>\n"
        '    <p:sp><p:nvSpPr><p:cNvPr id="2" name="Notes"/><p:cNvSpPr/>'
        '<p:nvPr><p:ph type="body"/></p:nvPr></p:nvSpPr>'
        "<p:txBody><a:bodyPr/><a:lstStyle/>"
        "<a:p><a:r><a:t>좌측 결정론 엔진과 우측 VLM 보강의 역할 분담을 설명한다.</a:t></a:r></a:p>"
        "</p:txBody></p:sp>\n"
        '    <p:sp><p:nvSpPr><p:cNvPr id="3" name="Num"/><p:cNvSpPr/>'
        '<p:nvPr><p:ph type="sldNum"/></p:nvPr></p:nvSpPr>'
        "<p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>2</a:t></a:r></a:p></p:txBody></p:sp>\n"
        "  </p:spTree></p:cSld>\n"
        "</p:notes>"
    )

    # Slide 3: 병합 셀(gridSpan + rowSpan) 네이티브 표 + 캡션
    s3 = _wrap_slide(
        _pic_xml(30, "CornerLogo", "rIdLogo", 850, 15, 80, 35) + "<p:sp>"
        '<p:nvSpPr><p:cNvPr id="31" name="Slide3 Title"/><p:cNvSpPr/>'
        '<p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr>'
        "<p:spPr/>"
        "<p:txBody><a:bodyPr/><a:lstStyle/>"
        '<a:p><a:r><a:rPr sz="2800" b="1"/><a:t>지역별 실적 표</a:t></a:r></a:p>'
        "</p:txBody></p:sp><p:sp>"
        '<p:nvSpPr><p:cNvPr id="32" name="Caption"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>'
        f"<p:spPr>{_xfrm(60, 95, 420, 25)}</p:spPr>"
        "<p:txBody><a:bodyPr/><a:lstStyle/>"
        '<a:p><a:r><a:rPr sz="1300"/><a:t>표 1. 분기별 지역 매출 현황(억원)</a:t></a:r></a:p>'
        "</p:txBody></p:sp><p:graphicFrame>"
        '<p:nvGraphicFramePr><p:cNvPr id="33" name="SalesTable"/>'
        "<p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr>"
        f'<p:xfrm><a:off x="{_emu(60)}" y="{_emu(130)}"/>'
        f'<a:ext cx="{_emu(840)}" cy="{_emu(300)}"/></p:xfrm>'
        "<a:graphic><a:graphicData><a:tbl>"
        '<a:tblPr firstRow="1"/>'
        f'<a:tblGrid><a:gridCol w="{_emu(280)}"/>'
        f'<a:gridCol w="{_emu(280)}"/><a:gridCol w="{_emu(280)}"/></a:tblGrid>'
        f'<a:tr h="{_emu(60)}">'
        + _tc("구분")
        + _tc("상반기 매출", attrs='gridSpan="2"')
        + _tc("", attrs='hMerge="1"')
        + f'</a:tr><a:tr h="{_emu(80)}">'
        + _tc("수도권", attrs='rowSpan="2"')
        + _tc("서울")
        + _tc("1250")
        + f'</a:tr><a:tr h="{_emu(80)}">'
        + _tc("", attrs='vMerge="1"')
        + _tc("경기")
        + _tc("980")
        + "</a:tr></a:tbl></a:graphicData></a:graphic></p:graphicFrame>"
    )

    # Slide 4: 네이티브 OOXML 복합 차트(barChart + lineChart)
    s4 = _wrap_slide(
        _pic_xml(40, "CornerLogo", "rIdLogo", 850, 15, 80, 35) + "<p:sp>"
        '<p:nvSpPr><p:cNvPr id="41" name="Slide4 Title"/><p:cNvSpPr/>'
        '<p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr>'
        "<p:spPr/>"
        "<p:txBody><a:bodyPr/><a:lstStyle/>"
        '<a:p><a:r><a:rPr sz="2800" b="1"/><a:t>분기별 성장 추이 차트</a:t></a:r></a:p>'
        "</p:txBody></p:sp><p:graphicFrame>"
        '<p:nvGraphicFramePr><p:cNvPr id="42" name="RevenueChart"/>'
        "<p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr>"
        f'<p:xfrm><a:off x="{_emu(80)}" y="{_emu(110)}"/>'
        f'<a:ext cx="{_emu(800)}" cy="{_emu(360)}"/></p:xfrm>'
        '<a:graphic><a:graphicData><c:chart r:id="rIdChart1"/></a:graphicData></a:graphic>'
        "</p:graphicFrame>"
    )
    chart1_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<c:chartSpace xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">\n'
        "  <c:chart>\n"
        "    <c:title><c:tx><c:rich><a:bodyPr/><a:lstStyle/>"
        "<a:p><a:r><a:t>2026년 분기별 매출 및 성장률</a:t></a:r></a:p>"
        "</c:rich></c:tx></c:title>\n"
        "    <c:plotArea>\n"
        "      <c:barChart><c:ser>\n"
        "        <c:tx><c:strRef><c:strCache>"
        '<c:pt idx="0"><c:v>매출(억원)</c:v></c:pt>'
        "</c:strCache></c:strRef></c:tx>\n"
        "        <c:cat><c:strRef><c:strCache>"
        '<c:pt idx="0"><c:v>Q1</c:v></c:pt><c:pt idx="1"><c:v>Q2</c:v></c:pt>'
        '<c:pt idx="2"><c:v>Q3</c:v></c:pt><c:pt idx="3"><c:v>Q4</c:v></c:pt>'
        "</c:strCache></c:strRef></c:cat>\n"
        "        <c:val><c:numRef><c:numCache>"
        '<c:pt idx="0"><c:v>120.0</c:v></c:pt><c:pt idx="1"><c:v>145.0</c:v></c:pt>'
        '<c:pt idx="2"><c:v>168.0</c:v></c:pt><c:pt idx="3"><c:v>210.0</c:v></c:pt>'
        "</c:numCache></c:numRef></c:val>\n"
        "      </c:ser></c:barChart>\n"
        "      <c:lineChart><c:ser>\n"
        "        <c:tx><c:v>성장률(%)</c:v></c:tx>\n"
        "        <c:val><c:numLit>"
        '<c:pt idx="0"><c:v>8.5</c:v></c:pt><c:pt idx="1"><c:v>12.0</c:v></c:pt>'
        '<c:pt idx="2"><c:v>15.2</c:v></c:pt><c:pt idx="3"><c:v>24.8</c:v></c:pt>'
        "</c:numLit></c:val>\n"
        "      </c:ser></c:lineChart>\n"
        "    </c:plotArea>\n"
        "  </c:chart>\n"
        "</c:chartSpace>"
    )

    # Slide 5: SmartArt 계층 트리 + 연결선 플로우차트 다이어그램
    def _flow_node(nid: int, name: str, x: float, prst: str, label: str) -> str:
        return (
            f'<p:sp><p:nvSpPr><p:cNvPr id="{nid}" name="{name}"/>'
            "<p:cNvSpPr/><p:nvPr/></p:nvSpPr>"
            f'<p:spPr>{_xfrm(x, 300, 200, 110)}<a:prstGeom prst="{prst}"/>'
            "<a:solidFill/><a:ln/></p:spPr>"
            f"<p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>{label}</a:t></a:r></a:p>"
            "</p:txBody></p:sp>"
        )

    def _arrow(cid: int, name: str, st: int, end: int, x: float) -> str:
        return (
            f'<p:cxnSp><p:nvCxnSpPr><p:cNvPr id="{cid}" name="{name}"/>'
            f'<p:cNvCxnSpPr><a:stCxn id="{st}" idx="1"/>'
            f'<a:endCxn id="{end}" idx="3"/></p:cNvCxnSpPr><p:nvPr/></p:nvCxnSpPr>'
            f"<p:spPr>{_xfrm(x, 350, 100, 10)}"
            '<a:ln><a:tailEnd type="triangle"/></a:ln></p:spPr></p:cxnSp>'
        )

    s5 = _wrap_slide(
        _pic_xml(50, "CornerLogo", "rIdLogo", 850, 15, 80, 35) + "<p:sp>"
        '<p:nvSpPr><p:cNvPr id="51" name="Slide5 Title"/><p:cNvSpPr/>'
        '<p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr>'
        "<p:spPr/>"
        "<p:txBody><a:bodyPr/><a:lstStyle/>"
        '<a:p><a:r><a:rPr sz="2800" b="1"/><a:t>처리 파이프라인 도식</a:t></a:r></a:p>'
        "</p:txBody></p:sp><p:graphicFrame>"
        '<p:nvGraphicFramePr><p:cNvPr id="52" name="PipelineSmartArt"/>'
        "<p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr>"
        f'<p:xfrm><a:off x="{_emu(40)}" y="{_emu(100)}"/>'
        f'<a:ext cx="{_emu(880)}" cy="{_emu(150)}"/></p:xfrm>'
        '<a:graphic><a:graphicData><dgm:relIds r:dm="rIdDgm1"/></a:graphicData></a:graphic>'
        "</p:graphicFrame>"
        + _flow_node(53, "Node1", 60, "flowChartProcess", "문서 입력")
        + _flow_node(54, "Node2", 360, "flowChartDecision", "구조 분류")
        + _flow_node(55, "Node3", 660, "chevron", "전략 추출")
        + _arrow(56, "Arrow1", 53, 54, 260)
        + _arrow(57, "Arrow2", 54, 55, 560)
    )
    smartart1_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<dgm:dataModel xmlns:dgm="http://schemas.openxmlformats.org/drawingml/2006/diagram" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">\n'
        "  <dgm:ptLst>\n"
        '    <dgm:pt modelId="0" type="doc"/>\n'
        + _dgm_pt("1", "1단계: 문서 수집")
        + _dgm_pt("2", "PDF 및 PPTX 자동 감지")
        + _dgm_pt("3", "2단계: IR 통합 내보내기")
        + "  </dgm:ptLst>\n"
        "  <dgm:cxnLst>\n"
        '    <dgm:cxn modelId="10" type="parOf" srcId="0" destId="1" srcOrd="0"/>\n'
        '    <dgm:cxn modelId="11" type="parOf" srcId="1" destId="2" srcOrd="0"/>\n'
        '    <dgm:cxn modelId="12" type="parOf" srcId="0" destId="3" srcOrd="1"/>\n'
        "  </dgm:cxnLst>\n"
        "</dgm:dataModel>"
    )

    # Slide 6: 텍스트 + 삽입 PNG 그림(하이브리드 슬라이드)
    s6 = _wrap_slide(
        _pic_xml(60, "CornerLogo", "rIdLogo", 850, 15, 80, 35) + "<p:sp>"
        '<p:nvSpPr><p:cNvPr id="61" name="Slide6 Title"/><p:cNvSpPr/>'
        '<p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr>'
        "<p:spPr/>"
        "<p:txBody><a:bodyPr/><a:lstStyle/>"
        '<a:p><a:r><a:rPr sz="2800" b="1"/><a:t>시장 점유율 분석</a:t></a:r></a:p>'
        "</p:txBody></p:sp><p:sp>"
        '<p:nvSpPr><p:cNvPr id="62" name="Summary"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>'
        f"<p:spPr>{_xfrm(40, 100, 880, 70)}</p:spPr>"
        "<p:txBody><a:bodyPr/><a:lstStyle/>"
        '<a:p><a:r><a:rPr sz="1500"/>'
        "<a:t>외부 리서치 기관의 점유율 차트 이미지를 VLM으로 해석해 수치를 보강한다.</a:t>"
        "</a:r></a:p></p:txBody></p:sp>"
        + _pic_xml(
            63,
            "MarketShareChart",
            "rIdPic6",
            140,
            190,
            680,
            260,
            descr="2026년 국내 시장 점유율 차트",
        )
        + "<p:sp>"
        '<p:nvSpPr><p:cNvPr id="64" name="FigCaption"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>'
        f"<p:spPr>{_xfrm(140, 460, 500, 25)}</p:spPr>"
        "<p:txBody><a:bodyPr/><a:lstStyle/>"
        '<a:p><a:r><a:rPr sz="1200"/><a:t>그림 1. 2026년 시장 점유율 추이</a:t></a:r></a:p>'
        "</p:txBody></p:sp>"
    )

    # Slide 7: 전체 슬라이드 스크린샷/스캔 이미지(레이아웃 탐지 확장 검증용)
    s7 = _wrap_slide(_pic_xml(70, "FullScreenshot", "rIdScan7", 0, 0, 960, 540))

    # Slide 8: 빈 숨김 슬라이드
    s8 = _wrap_slide("", show="0")

    pres_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<p:presentation xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
        'xmlns:p14="http://schemas.microsoft.com/office/powerpoint/2010/main">\n'
        f'  <p:sldSz cx="{_emu(960)}" cy="{_emu(540)}"/>\n'
        "  <p:sldIdLst>\n"
        '    <p:sldId id="256" r:id="rId1"/>\n'
        '    <p:sldId id="257" r:id="rId2"/>\n'
        '    <p:sldId id="258" r:id="rId3"/>\n'
        '    <p:sldId id="259" r:id="rId4"/>\n'
        '    <p:sldId id="260" r:id="rId5"/>\n'
        '    <p:sldId id="261" r:id="rId6"/>\n'
        '    <p:sldId id="262" r:id="rId7"/>\n'
        '    <p:sldId id="263" r:id="rId8"/>\n'
        "  </p:sldIdLst>\n"
        "  <p:extLst><p:ext><p14:sectionLst>\n"
        '    <p14:section name="1부. 개요 및 아키텍처"><p14:sldIdLst>'
        '<p14:sldId id="256"/><p14:sldId id="257"/>'
        "</p14:sldIdLst></p14:section>\n"
        '    <p14:section name="2부. 표·차트·도식 분석"><p14:sldIdLst>'
        '<p14:sldId id="258"/><p14:sldId id="259"/><p14:sldId id="260"/>'
        '<p14:sldId id="261"/><p14:sldId id="262"/><p14:sldId id="263"/>'
        "</p14:sldIdLst></p14:section>\n"
        "  </p14:sectionLst></p:ext></p:extLst>\n"
        "</p:presentation>"
    )
    core_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        "<cp:coreProperties "
        'xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        "  <dc:title>2026년 데이터 플랫폼 전략 보고서</dc:title>\n"
        "  <dc:creator>AutoETL Team</dc:creator>\n"
        "</cp:coreProperties>"
    )
    app_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">\n'
        "  <Application>Microsoft Office PowerPoint</Application>\n"
        "</Properties>"
    )

    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("docProps/core.xml", core_xml)
        zf.writestr("docProps/app.xml", app_xml)
        zf.writestr("ppt/presentation.xml", pres_xml)
        zf.writestr(
            "ppt/_rels/presentation.xml.rels",
            _rels_xml([(f"rId{i}", rel_slide, f"slides/slide{i}.xml") for i in range(1, 9)]),
        )
        zf.writestr("ppt/slideMasters/slideMaster1.xml", master_xml)
        zf.writestr("ppt/slideLayouts/slideLayout1.xml", layout_xml)
        zf.writestr(
            "ppt/slideLayouts/_rels/slideLayout1.xml.rels",
            _rels_xml([("rIdM1", rel_master, "../slideMasters/slideMaster1.xml")]),
        )

        # 미디어·차트·SmartArt·발표자 노트
        zf.writestr("ppt/media/logo.png", logo_png)
        zf.writestr("ppt/media/bg.png", bg_png)
        zf.writestr("ppt/media/chart_pic.png", chart_pic_png)
        zf.writestr("ppt/media/scan7.png", screenshot_png)
        zf.writestr("ppt/charts/chart1.xml", chart1_xml)
        zf.writestr("ppt/diagrams/data1.xml", smartart1_xml)
        zf.writestr("ppt/notesSlides/notesSlide2.xml", notes2_xml)

        # 슬라이드 XML & 관계
        slides_content = [s1, s2, s3, s4, s5, s6, s7, s8]
        slides_rels: list[list[tuple[str, str, str]]] = [
            [
                ("rIdL", rel_layout, "../slideLayouts/slideLayout1.xml"),
                ("rIdBg", rel_image, "../media/bg.png"),
                ("rIdLogo", rel_image, "../media/logo.png"),
            ],
            [
                ("rIdL", rel_layout, "../slideLayouts/slideLayout1.xml"),
                ("rIdLogo", rel_image, "../media/logo.png"),
                ("rIdN2", rel_notes, "../notesSlides/notesSlide2.xml"),
            ],
            [
                ("rIdL", rel_layout, "../slideLayouts/slideLayout1.xml"),
                ("rIdLogo", rel_image, "../media/logo.png"),
            ],
            [
                ("rIdL", rel_layout, "../slideLayouts/slideLayout1.xml"),
                ("rIdLogo", rel_image, "../media/logo.png"),
                ("rIdChart1", rel_chart, "../charts/chart1.xml"),
            ],
            [
                ("rIdL", rel_layout, "../slideLayouts/slideLayout1.xml"),
                ("rIdLogo", rel_image, "../media/logo.png"),
                ("rIdDgm1", rel_dgm_data, "../diagrams/data1.xml"),
            ],
            [
                ("rIdL", rel_layout, "../slideLayouts/slideLayout1.xml"),
                ("rIdLogo", rel_image, "../media/logo.png"),
                ("rIdPic6", rel_image, "../media/chart_pic.png"),
            ],
            [
                ("rIdL", rel_layout, "../slideLayouts/slideLayout1.xml"),
                ("rIdScan7", rel_image, "../media/scan7.png"),
            ],
            [("rIdL", rel_layout, "../slideLayouts/slideLayout1.xml")],
        ]
        for idx, (xml_txt, rel_list) in enumerate(zip(slides_content, slides_rels, strict=True), 1):
            zf.writestr(f"ppt/slides/slide{idx}.xml", xml_txt)
            zf.writestr(f"ppt/slides/_rels/slide{idx}.xml.rels", _rels_xml(rel_list))

    return path


@contextmanager
def _run_mock_vlm_server(handler_fn: Any) -> Iterator[str]:
    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length > 0 else b"{}"
            body = json.loads(raw.decode("utf-8"))
            status, resp_obj = handler_fn(self.path, body)
            encoded = json.dumps(resp_obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# 1. 등록·이미지 헤더 탐지·입력 검증 단위 테스트
# ---------------------------------------------------------------------------


def test_pptx_adapter_registered_and_image_dimension_probe() -> None:
    """`.pptx` 어댑터가 기본 등록되어 있고 순수 Python 이미지 크기 탐지가 동작한다."""
    exts = supported_extensions()
    assert ".pdf" in exts
    assert ".pptx" in exts
    assert PptxDocumentAdapter().format == SourceFormat.PPTX

    # PNG
    png = _make_solid_png(32, 18, (10, 20, 30))
    assert probe_image_dimensions(png, ".png") == (32, 18)

    # GIF89a
    gif = b"GIF89a" + struct.pack("<HH", 120, 80) + b"\x00\x00\x00"
    assert probe_image_dimensions(gif, ".gif") == (120, 80)

    # 최소 JPEG SOF0 세그먼트
    sof0_payload = struct.pack(">BHHB", 8, 240, 320, 3) + b"\x00" * 9
    jpeg = (
        b"\xff\xd8\xff\xc0" + struct.pack(">H", len(sof0_payload) + 2) + sof0_payload + b"\xff\xd9"
    )
    assert probe_image_dimensions(jpeg, ".jpg") == (320, 240)


def test_pptx_error_handling_encrypted_ole_corrupt_and_bad_page_range(tmp_path: Path) -> None:
    """암호화/구형 OLE 파일, 손상 ZIP, 잘못된 슬라이드 범위를 ParserInputError로 처리한다."""
    ole_file = tmp_path / "encrypted.pptx"
    ole_file.write_bytes(OLE_MAGIC + b"\x00" * 64)
    with pytest.raises(ParserInputError, match="암호화"):
        inspect_document(ole_file)

    corrupt = tmp_path / "corrupt.pptx"
    corrupt.write_bytes(b"not-a-zip-archive")
    with pytest.raises(ParserInputError, match="유효한 PPTX"):
        inspect_document(corrupt)

    deck = _build_rich_demo_pptx(tmp_path / "demo.pptx")
    with pytest.raises(ParserInputError, match="범위"):
        inspect_document(deck, options=ParseOptions(pages=(99,)))


# ---------------------------------------------------------------------------
# 2. 8개 실전 슬라이드 구조 분류·추출·내보내기 종합 테스트
# ---------------------------------------------------------------------------


def test_pptx_rich_deck_classification_extraction_and_export(tmp_path: Path) -> None:
    """8개 아키타입 슬라이드의 구조 분류, 병합표, 네이티브 차트, SmartArt, 그룹, 노트를 검증한다."""
    deck = _build_rich_demo_pptx(tmp_path / "strategy_deck.pptx")
    out_dir = tmp_path / "pptx_out"

    doc, manifest = parse_and_export(deck, out_dir)

    assert doc.document.format == SourceFormat.PPTX
    assert doc.document.title == "2026년 데이터 플랫폼 전략 보고서"
    assert doc.document.author == "AutoETL Team"
    assert doc.document.page_count == 8
    assert doc.document.has_outline is True
    assert len(doc.routing_plan.outline) >= 6
    assert doc.routing_plan.outline[0].title == "1부. 개요 및 아키텍처"

    pages = {p.page: p for p in doc.pages}

    # Slide 1: 표지 — 배경 벽지와 반복 로고가 모두 장식으로 제외되어야 함
    p1 = pages[1]
    assert p1.route.category == PageCategory.SLIDE_LIKE
    assert len(p1.visual_regions) == 0
    assert any(
        b.kind == ElementKind.HEADING and "2026년 데이터 플랫폼 전략 보고서" in b.text
        for b in p1.blocks
    )

    # Slide 2: 2단 그룹 카드 + 계층 불릿 + mc:AlternateContent 수식 + 바닥글 분리 + 발표자 노트
    p2 = pages[2]
    assert p2.route.category == PageCategory.MULTI_COLUMN
    assert p2.route.primary_strategy == ParseStrategy.MULTI_COLUMN_LAYOUT
    assert p2.route.metrics.column_count == 2
    p2_texts = [b.text for b in p2.blocks]
    joined_p2 = "\n".join(p2_texts)
    assert "좌측 트랙: 결정론 엔진" in joined_p2
    assert "우측 트랙: 시각·VLM 보강" in joined_p2
    assert "Accuracy = TP / (TP + FP)" in joined_p2
    assert "DUPLICATE_FALLBACK" not in joined_p2
    assert "o 중첩 그룹 도형 좌표 변환 지원" in p2_texts
    assert any(b.text.startswith("[발표자 노트]") for b in p2.blocks)
    # 읽기 순서 검증: 좌측 카드 전체가 우측 카드보다 먼저 나와야 함
    idx_left_sub = next(i for i, t in enumerate(p2_texts) if "중첩 그룹 도형" in t)
    idx_right_head = next(i for i, t in enumerate(p2_texts) if "우측 트랙" in t)
    assert idx_left_sub < idx_right_head
    # 바닥글과 슬라이드 번호는 header_footer_blocks로 분리
    hf_texts = " ".join(b.text for b in p2.header_footer_blocks)
    assert "사내 대외비" in hf_texts

    # Slide 3: 병합 셀 네이티브 표 + 캡션 연결
    p3 = pages[3]
    assert p3.route.category == PageCategory.TABLE_HEAVY
    assert len(p3.tables) == 1
    t3 = p3.tables[0]
    assert t3.has_merged_cells is True
    assert t3.row_count == 3 and t3.col_count == 3
    assert t3.headers == ["구분", "상반기 매출", ""]
    assert t3.caption == "표 1. 분기별 지역 매출 현황(억원)"
    assert any(c.col_span == 2 and c.text == "상반기 매출" for c in t3.cells)
    assert any(c.row_span == 2 and c.text == "수도권" for c in t3.cells)

    # Slide 4: 네이티브 OOXML 복합 차트 XML → 결정론 ExtractedTable 승격
    p4 = pages[4]
    assert p4.route.category == PageCategory.TABLE_HEAVY
    assert len(p4.tables) == 1
    t4 = p4.tables[0]
    assert t4.headers == ["항목", "매출(억원)", "성장률(%)"]
    assert t4.rows == [
        ["Q1", "120", "8.5"],
        ["Q2", "145", "12"],
        ["Q3", "168", "15.2"],
        ["Q4", "210", "24.8"],
    ]
    assert t4.caption is not None and "2026년 분기별 매출 및 성장률" in t4.caption

    # Slide 5: SmartArt 계층 트리 + 연결선 도식 흐름 + VECTOR_DIAGRAM 영역
    p5 = pages[5]
    assert p5.route.category == PageCategory.VECTOR_DIAGRAM_HEAVY
    assert p5.route.metrics.column_count == 1
    p5_joined = "\n".join(b.text for b in p5.blocks)
    assert "1단계: 문서 수집" in p5_joined
    assert "o PDF 및 PPTX 자동 감지" in p5_joined
    assert "문서 입력 -> 구조 분류" in p5_joined
    assert "구조 분류 -> 전략 추출" in p5_joined
    assert any(v.element_kind == ElementKind.VECTOR_DIAGRAM for v in p5.visual_regions)

    # Slide 6: 텍스트 + 삽입 PNG 이미지(크롭 직접 추출 + alt_text 힌트 + 캡션 연결)
    p6 = pages[6]
    assert p6.route.category == PageCategory.HYBRID_TEXT_IMAGE
    assert len(p6.visual_regions) == 1
    v6 = p6.visual_regions[0]
    assert v6.element_kind == ElementKind.FIGURE_IMAGE
    assert v6.crop_status == CropStatus.SAVED
    assert v6.crop_path is not None and Path(v6.crop_path).is_file()
    assert "2026년 국내 시장 점유율 차트" in v6.embedded_text_hints
    assert v6.caption == "그림 1. 2026년 시장 점유율 추이"

    # Slide 7: 전체 스크린샷/스캔 슬라이드(기본 단일 SCANNED_PAGE)
    p7 = pages[7]
    assert p7.route.category == PageCategory.SCANNED_IMAGE
    assert len(p7.visual_regions) == 1
    assert p7.visual_regions[0].element_kind == ElementKind.SCANNED_PAGE

    # Slide 8: 빈 숨김 슬라이드 경고
    p8 = pages[8]
    assert p8.route.category == PageCategory.BLANK
    assert any("숨김" in w for w in p8.route.warnings)

    # 산출물 번들(Markdown, CSV, JSON, 리포트) 검증
    assert manifest.parsed_json.is_file()
    assert manifest.document_markdown.is_file()
    assert manifest.routing_report_markdown.is_file()
    assert len(manifest.table_csv_paths) == 2  # Slide 3 표 + Slide 4 차트 테이블
    doc_md = manifest.document_markdown.read_text(encoding="utf-8")
    assert "| 항목 | 매출(억원) | 성장률(%) |" in doc_md
    assert "  - 중첩 그룹 도형 좌표 변환 지원" in doc_md
    assert "  - PDF 및 PPTX 자동 감지" in doc_md


# ---------------------------------------------------------------------------
# 3. PPTX 스캔 슬라이드 레이아웃 분할 + OCR/VLM 연동 & CLI 테스트
# ---------------------------------------------------------------------------


def test_pptx_scanned_slide_layout_detection_and_vlm_routing(tmp_path: Path) -> None:
    """PPTX의 스크린샷 슬라이드(Slide 7)와 그림 슬라이드(Slide 6)가 VLM에 정확히 분기된다."""
    deck = _build_rich_demo_pptx(tmp_path / "vision_deck.pptx")
    prompts_by_call: list[str] = []

    def vlm_handler(_path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        prompt = body["messages"][-1]["content"][0]["text"]
        prompts_by_call.append(prompt)
        return 200, {
            "model": body["model"],
            "choices": [{"message": {"role": "assistant", "content": f"PARSED({prompt})"}}],
        }

    with _run_mock_vlm_server(vlm_handler) as base_url:
        vlm = OpenAICompatibleVlmAdapter(model="PaddleOCR-VL-1.6", base_url=f"{base_url}/v1")
        doc = parse_document(
            deck,
            options=ParseOptions(pages=(6, 7), extract_crops=True, crop_dir=tmp_path / "crops"),
            vision_adapter=vlm,
            layout_detector=HeuristicRasterLayoutDetector(),
        )

    assert len(doc.pages) == 2
    p6, p7 = doc.pages[0], doc.pages[1]

    # Slide 6 그림은 Chart Recognition으로 처리 완료
    assert p6.visual_regions[0].vision_status == VisionStatus.COMPLETED

    # Slide 7 스크린샷은 레이아웃 탐지로 표·차트·텍스트 영역으로 세분화 & 개별 크롭 완료
    p7_kinds = [v.element_kind for v in p7.visual_regions]
    assert ElementKind.SCANNED_PAGE not in p7_kinds
    assert ElementKind.TABLE in p7_kinds
    assert ElementKind.FIGURE_IMAGE in p7_kinds
    for vis in p7.visual_regions:
        assert vis.crop_status == CropStatus.SAVED
        assert vis.crop_path is not None
        dec = decode_png(Path(vis.crop_path).read_bytes())
        assert dec is not None and dec.height < 840
        assert vis.vision_status == VisionStatus.COMPLETED

    assert "Table Recognition:" in prompts_by_call
    assert "Chart Recognition:" in prompts_by_call
    assert "OCR:" in prompts_by_call


def test_cli_parse_pptx_plan_only_and_no_speaker_notes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI `autoetl parse deck.pptx`의 `--plan-only`, `--pages`, `--no-speaker-notes`를 검증한다."""
    deck = _build_rich_demo_pptx(tmp_path / "cli_deck.pptx")

    rc_plan = cli_main(["parse", str(deck), "--plan-only", "--format", "report"])
    assert rc_plan == 0
    plan_md = capsys.readouterr().out
    assert "`pptx`" in plan_md
    assert "1부. 개요 및 아키텍처" in plan_md
    assert "2026년 분기별 매출 및 성장률" in plan_md or "네이티브 차트" in plan_md

    out_dir = tmp_path / "cli_pptx_out"
    rc_out = cli_main(
        [
            "parse",
            str(deck),
            "--pages",
            "2-4",
            "--no-speaker-notes",
            "--out",
            str(out_dir),
        ]
    )
    assert rc_out == 0
    stdout = capsys.readouterr().out
    assert "파싱 완료: cli_deck.pptx (3/8p, 표 2개" in stdout
    doc_md = (out_dir / "document.md").read_text(encoding="utf-8")
    assert "[발표자 노트]" not in doc_md
    assert "| 항목 | 매출(억원) | 성장률(%) |" in doc_md


# ---------------------------------------------------------------------------
# 4. 실전 공개 덱 29종 품질 점검에서 발굴한 8가지 회귀 테스트
# ---------------------------------------------------------------------------


def test_pptx_realworld_xycut_cards_repeating_footer_notes_and_headings(tmp_path: Path) -> None:
    """실전 공개 덱 29종 품질 점검에서 발굴한 8가지 회귀를 검증한다."""
    rel_slide = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide"
    rel_notes = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/notesSlide"
    rel_image = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"

    def _card_sp(sid: str, x: float, y: float, w: float, h: float, txt: str, sz: int = 1200) -> str:
        return (
            f'<p:sp><p:nvSpPr><p:cNvPr id="{sid}" name="Card{sid}"/>'
            "<p:cNvSpPr/><p:nvPr/></p:nvSpPr>"
            f"<p:spPr>{_xfrm(x, y, w, h)}</p:spPr>"
            f'<p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:rPr sz="{sz}"/>'
            f"<a:t>{txt}</a:t></a:r></a:p></p:txBody></p:sp>"
        )

    def _footer_sp(sid: str) -> str:
        return _card_sp(sid, 48, 505, 500, 20, "ACME CORP · INTERNAL DECK", sz=900)

    # Slide 1: 3열 가로 타임라인 카드 + 하단 전폭 요약 + 다중 줄(<a:br/>) 제목 + 숨김 도형
    s1 = _wrap_slide(
        '<p:sp><p:nvSpPr><p:cNvPr id="2" name="Title"/><p:cNvSpPr/>'
        '<p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr>'
        f"<p:spPr>{_xfrm(48, 16, 800, 50)}</p:spPr>"
        '<p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:rPr sz="2400"/>'
        '<a:t>OVERVIEW OF</a:t></a:r><a:br/><a:r><a:rPr sz="2400"/>'
        "<a:t>TIMELINE</a:t></a:r></a:p></p:txBody></p:sp>"
        + _card_sp("10", 60, 110, 180, 26, "1단계", 1600)
        + _card_sp("11", 60, 140, 180, 20, "0:00-0:15")
        + _card_sp("12", 60, 164, 180, 20, "환경 백업")
        + _card_sp("20", 360, 110, 180, 26, "2단계", 1600)
        + _card_sp("21", 360, 140, 180, 20, "0:15-0:40")
        + _card_sp("22", 360, 164, 180, 20, "수치 검증")
        + _card_sp("30", 660, 110, 180, 26, "3단계", 1600)
        + _card_sp("31", 660, 140, 180, 20, "0:40-1:00")
        + _card_sp("32", 660, 164, 180, 20, "결과 정리")
        + _card_sp("40", 60, 260, 800, 45, "전체 요약 배너 문장입니다.")
        + '<p:sp><p:nvSpPr><p:cNvPr id="50" name="Draft" hidden="1"/>'
        "<p:cNvSpPr/><p:nvPr/></p:nvSpPr>"
        f"<p:spPr>{_xfrm(60, 320, 300, 30)}</p:spPr>"
        "<p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>HIDDEN_SECRET</a:t></a:r>"
        "</a:p></p:txBody></p:sp>" + _footer_sp("90")
    )

    # Slide 2: 좌측 불릿 + 우측 수식 이미지(끼어들기 금지 + FORMULA_OR_CODE + 경로 정화)
    s2 = _wrap_slide(
        '<p:sp><p:nvSpPr><p:cNvPr id="2" name="Bullets"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>'
        f"<p:spPr>{_xfrm(40, 100, 430, 320)}</p:spPr>"
        "<p:txBody><a:bodyPr/><a:lstStyle/>"
        '<a:p><a:pPr><a:buChar char="•"/></a:pPr><a:r><a:t>불릿 항목 1</a:t></a:r></a:p>'
        '<a:p><a:pPr><a:buChar char="•"/></a:pPr><a:r><a:t>불릿 항목 2</a:t></a:r></a:p>'
        '<a:p><a:pPr><a:buChar char="•"/></a:pPr><a:r><a:t>불릿 항목 3</a:t></a:r></a:p>'
        "</p:txBody></p:sp>"
        '<p:pic><p:nvPicPr><p:cNvPr id="3" name="Equation" '
        'descr="E:\\private\\eq_01.png"/><p:cNvPicPr/><p:nvPr/></p:nvPicPr>'
        '<p:blipFill><a:blip r:embed="rIdEq"/></p:blipFill>'
        f"<p:spPr>{_xfrm(520, 160, 360, 180)}</p:spPr></p:pic>" + _footer_sp("91")
    )

    # Slide 3: 동일 픽토그램 6회 반복 + SVG(asvg:svgBlip) + OLE MSGraph + 6열 희소 병합 표
    picto_xml = "".join(
        f'<p:pic><p:nvPicPr><p:cNvPr id="{100 + i}" name="Icon"/>'
        "<p:cNvPicPr/><p:nvPr/></p:nvPicPr>"
        '<p:blipFill><a:blip r:embed="rIdIcon"/></p:blipFill>'
        f"<p:spPr>{_xfrm(50 + i * 70, 330, 50, 80)}</p:spPr></p:pic>"
        for i in range(6)
    )
    svg_pic_xml = (
        '<p:pic><p:nvPicPr><p:cNvPr id="200" name="LogoSVG" descr="System diagram"/>'
        "<p:cNvPicPr/><p:nvPr/></p:nvPicPr>"
        "<p:blipFill><a:blip><a:extLst>"
        '<a:ext uri="{96DAC541-7B7A-43D3-8B79-37D633B846F1}">'
        '<asvg:svgBlip xmlns:asvg="http://schemas.microsoft.com/office/drawing/2016/SVG/main" '
        'r:embed="rIdSvg"/></a:ext></a:extLst></a:blip></p:blipFill>'
        f"<p:spPr>{_xfrm(520, 60, 140, 100)}</p:spPr></p:pic>"
    )
    ole_xml = (
        '<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="201" name="Obj1"/>'
        "<p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr>"
        f"<p:xfrm>{_xfrm(700, 60, 200, 140)[8:-9]}</p:xfrm>"
        '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/presentationml/2006/ole">'
        '<p:oleObj name="LegacyChart" progId="MSGraph.Chart.8"/>'
        "</a:graphicData></a:graphic></p:graphicFrame>"
    )
    sparse_tbl_xml = (
        '<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="202" name="SparseTbl"/>'
        "<p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr>"
        f"<p:xfrm>{_xfrm(40, 60, 440, 180)[8:-9]}</p:xfrm>"
        '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/table">'
        '<a:tbl><a:tblPr firstRow="1"/>'
        '<a:tblGrid><a:gridCol w="1000000"/><a:gridCol w="1000000"/>'
        '<a:gridCol w="1000000"/><a:gridCol w="1000000"/>'
        '<a:gridCol w="1000000"/><a:gridCol w="1000000"/></a:tblGrid>'
        '<a:tr h="300000">'
        + _tc("Survey Summary", attrs='gridSpan="6"')
        + _tc("", attrs='hMerge="1"') * 5
        + "</a:tr>"
        '<a:tr h="300000">'
        + _tc("Sedan")
        + _tc("")
        + _tc("Hatch")
        + _tc("")
        + _tc("SUV")
        + _tc("")
        + "</a:tr>"
        '<a:tr h="300000">'
        + _tc("13")
        + _tc("")
        + _tc("6")
        + _tc("")
        + _tc("9")
        + _tc("")
        + "</a:tr></a:tbl></a:graphicData></a:graphic></p:graphicFrame>"
    )
    s3 = _wrap_slide(sparse_tbl_xml + svg_pic_xml + ole_xml + picto_xml + _footer_sp("92"))

    # Notes slide with ONLY a plain <a:fld type="slidenum"> (no <p:ph>) -> must NOT leak as notes
    notes1 = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<p:notes xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
        ' xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
        '<p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/>'
        "<p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>"
        '<p:sp><p:nvSpPr><p:cNvPr id="2" name="RectNum"/><p:cNvSpPr txBox="1"/>'
        "<p:nvPr/></p:nvSpPr><p:spPr/><p:txBody><a:bodyPr/><a:lstStyle/>"
        '<a:p><a:fld id="{1}" type="slidenum"><a:t>1</a:t></a:fld></a:p>'
        "</p:txBody></p:sp></p:spTree></p:cSld></p:notes>"
    )

    pres_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
        ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        "<p:sldIdLst>"
        '<p:sldId id="256" r:id="rId1"/>'
        '<p:sldId id="257" r:id="rId2"/>'
        '<p:sldId id="258" r:id="rId3"/>'
        "</p:sldIdLst>"
        f'<p:sldSz cx="{_emu(960)}" cy="{_emu(540)}"/>'
        "</p:presentation>"
    )

    svg_bytes = (
        b'<svg xmlns="http://www.w3.org/2000/svg" width="120" height="80">'
        b"<text>Pipeline Node</text></svg>"
    )
    png_bytes = _make_solid_png(60, 40, (20, 90, 160))

    deck_path = tmp_path / "realworld_fixes.pptx"
    with zipfile.ZipFile(deck_path, "w") as zf:
        zf.writestr("ppt/presentation.xml", pres_xml)
        zf.writestr(
            "ppt/_rels/presentation.xml.rels",
            _rels_xml(
                [
                    ("rId1", rel_slide, "slides/slide1.xml"),
                    ("rId2", rel_slide, "slides/slide2.xml"),
                    ("rId3", rel_slide, "slides/slide3.xml"),
                ]
            ),
        )
        zf.writestr("ppt/slides/slide1.xml", s1)
        zf.writestr(
            "ppt/slides/_rels/slide1.xml.rels",
            _rels_xml([("rIdN1", rel_notes, "../notesSlides/notesSlide1.xml")]),
        )
        zf.writestr("ppt/notesSlides/notesSlide1.xml", notes1)
        zf.writestr("ppt/slides/slide2.xml", s2)
        zf.writestr(
            "ppt/slides/_rels/slide2.xml.rels",
            _rels_xml([("rIdEq", rel_image, "../media/eq.png")]),
        )
        zf.writestr("ppt/slides/slide3.xml", s3)
        zf.writestr(
            "ppt/slides/_rels/slide3.xml.rels",
            _rels_xml(
                [
                    ("rIdIcon", rel_image, "../media/icon.png"),
                    ("rIdSvg", rel_image, "../media/diag.svg"),
                ]
            ),
        )
        zf.writestr("ppt/media/eq.png", png_bytes)
        zf.writestr("ppt/media/icon.png", png_bytes)
        zf.writestr("ppt/media/diag.svg", svg_bytes)

    doc = parse_document(deck_path, options=ParseOptions(extract_crops=True))
    p1, p2, p3 = doc.pages

    # 1. 다중 줄 제목 평탄화 + 숨김 도형 차단 + 반복 바닥글 분리 + 가짜 슬라이드 번호 노트 차단
    assert p1.blocks[0].text == "OVERVIEW OF TIMELINE"
    p1_texts = [b.text for b in p1.blocks]
    assert "HIDDEN_SECRET" not in p1_texts
    assert not any(t.startswith("[발표자 노트]") for t in p1_texts)
    assert any("ACME CORP" in b.text for b in p1.header_footer_blocks)
    assert doc.routing_plan.summary.running_footer_detected is True

    # 2. 3열 가로 카드 XY-Cut: 각 카드가 행별이 아닌 카드별로 묶여 읽혀야 함
    assert p1_texts.index("1단계") < p1_texts.index("환경 백업") < p1_texts.index("2단계")
    assert p1_texts.index("2단계") < p1_texts.index("수치 검증") < p1_texts.index("3단계")
    assert (
        p1_texts.index("3단계")
        < p1_texts.index("결과 정리")
        < p1_texts.index("전체 요약 배너 문장입니다.")
    )

    # 3. 좌측 불릿 연속성 유지 + 수식 이미지 FORMULA_OR_CODE 분류 + 로컬 경로 제거
    p2_kinds = [(b.kind, b.text) for b in p2.blocks]
    bullet_indices = [i for i, (k, _) in enumerate(p2_kinds) if k == ElementKind.LIST_ITEM]
    assert bullet_indices == [0, 1, 2]
    assert len(p2.visual_regions) == 1
    assert p2.visual_regions[0].element_kind == ElementKind.FORMULA_OR_CODE
    assert p2.visual_regions[0].embedded_text_hints == []

    # 4. 6열 희소 표 → 3열 압축, SVG + OLE + 6개 반복 픽토그램 군집(총 3개 시각 영역으로 압축)
    assert len(p3.tables) == 1
    assert p3.tables[0].col_count == 3
    assert p3.tables[0].headers == ["Survey Summary", "", ""]
    assert p3.tables[0].rows == [["Sedan", "Hatch", "SUV"], ["13", "6", "9"]]
    assert len(p3.visual_regions) == 3  # OLE 1 + SVG 1 + 6개 픽토그램 군집 1
    assert any("Pipeline Node" in v.embedded_text_hints for v in p3.visual_regions)
    assert any("MSGraph.Chart.8" in v.reason for v in p3.visual_regions)
    assert any("6회 반복 배치" in v.reason for v in p3.visual_regions)


# ---------------------------------------------------------------------------
# 5. Office 2016+ ChartEx, 슬라이드 주석, 내장 Excel(.xlsx)/VML, LibreOffice 렌더러 테스트
# ---------------------------------------------------------------------------


def _make_mini_xlsx_bytes() -> bytes:
    """내장 Excel(.xlsx) OLE 테스트용 최소 워크북 바이트를 생성한다."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
            ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="Summary" sheetId="1" r:id="rId1"/></sheets></workbook>',
        )
        zf.writestr(
            "xl/_rels/workbook.xml.rels",
            _rels_xml(
                [
                    (
                        "rId1",
                        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet",
                        "worksheets/sheet1.xml",
                    )
                ]
            ),
        )
        zf.writestr(
            "xl/sharedStrings.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="4">'
            "<si><t>Region</t></si><si><t>Revenue</t></si>"
            "<si><t>Active</t></si><si><t>KR</t></si></sst>",
        )
        zf.writestr(
            "xl/worksheets/sheet1.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            "<sheetData>"
            '<row r="1">'
            '<c r="A1" t="s"><v>0</v></c>'
            '<c r="B1" t="s"><v>1</v></c>'
            '<c r="C1" t="s"><v>2</v></c>'
            "</row>"
            '<row r="2">'
            '<c r="A2" t="s"><v>3</v></c>'
            '<c r="B2"><v>420.5</v></c>'
            '<c r="C2" t="b"><v>1</v></c>'
            "</row>"
            '<row r="3">'
            '<c r="A3" t="inlineStr"><is><t>JP</t></is></c>'
            '<c r="B3"><v>310</v></c>'
            '<c r="C3" t="b"><v>0</v></c>'
            "</row>"
            "</sheetData></worksheet>",
        )
    return buf.getvalue()


def test_pptx_chartex_comments_embedded_xlsx_and_vml_preview(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """ChartEx(폭포·트리맵), 구형/현대 주석, 내장 Excel(.xlsx) OLE, VML 미리보기를 검증한다."""
    rel_slide = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide"
    rel_chartex = "http://schemas.microsoft.com/office/2014/relationships/chartEx"
    rel_ole = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/oleObject"
    rel_vml = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/vmlDrawing"
    rel_img = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
    rel_cm = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments"
    rel_mod_cm = "http://schemas.microsoft.com/office/2018/10/relationships/comments"
    rel_authors = (
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/commentAuthors"
    )

    # Slide 1: Office 2016+ 다단계 트리맵 ChartEx + 구형 스레드 주석
    s1 = _wrap_slide(
        '<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="2" name="Treemap1"/>'
        "<p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr>"
        f"<p:xfrm>{_xfrm(50, 80, 820, 380)[8:-9]}</p:xfrm>"
        '<a:graphic><a:graphicData uri="http://schemas.microsoft.com/office/drawing/2014/chartex">'
        '<cx:chart xmlns:cx="http://schemas.microsoft.com/office/drawing/2014/chartex" '
        'r:id="rIdCx1"/></a:graphicData></a:graphic></p:graphicFrame>'
    )
    chartex1 = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<cx:chartSpace xmlns:cx="http://schemas.microsoft.com/office/drawing/2014/chartex"'
        ' xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        "<cx:chartData>"
        '<cx:data id="0">'
        '<cx:strDim type="cat">'
        '<cx:lvl ptCount="3"><cx:pt idx="0">한국</cx:pt><cx:pt idx="1">일본</cx:pt>'
        '<cx:pt idx="2">독일</cx:pt></cx:lvl>'
        '<cx:lvl ptCount="3"><cx:pt idx="0">아시아</cx:pt><cx:pt idx="1">아시아</cx:pt>'
        '<cx:pt idx="2">유럽</cx:pt></cx:lvl>'
        "</cx:strDim>"
        '<cx:numDim type="val">'
        '<cx:lvl ptCount="3"><cx:pt idx="0">520</cx:pt><cx:pt idx="1">310</cx:pt>'
        '<cx:pt idx="2">245.5</cx:pt></cx:lvl>'
        "</cx:numDim>"
        "</cx:data>"
        "</cx:chartData>"
        "<cx:chart>"
        "<cx:title><cx:tx><cx:txData><cx:v>권역별 매출 비중</cx:v></cx:txData></cx:tx></cx:title>"
        "<cx:plotArea><cx:plotAreaRegion>"
        '<cx:series layoutId="treemap">'
        "<cx:tx><cx:txData><cx:v>매출(억원)</cx:v></cx:txData></cx:tx>"
        '<cx:dataId val="0"/>'
        "</cx:series>"
        "</cx:plotAreaRegion></cx:plotArea>"
        "</cx:chart></cx:chartSpace>"
    )

    # Slide 2: 내장 Excel(.xlsx) OLE + 구형 MSGraph OLE(VML .emf 미리보기) + 현대 주석
    s2 = _wrap_slide(
        '<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="3" name="EmbeddedSheet"/>'
        "<p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr>"
        f"<p:xfrm>{_xfrm(40, 80, 400, 260)[8:-9]}</p:xfrm>"
        '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/presentationml/2006/ole">'
        '<p:oleObj name="SalesWorkbook" r:id="rIdXlsx" progId="Excel.Sheet.12"/>'
        "</a:graphicData></a:graphic></p:graphicFrame>"
        '<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="4" name="OldChart"/>'
        "<p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr>"
        f"<p:xfrm>{_xfrm(480, 80, 420, 260)[8:-9]}</p:xfrm>"
        '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/presentationml/2006/ole">'
        '<p:oleObj spid="_x0000_s1026" name="LegacyGraph" r:id="rIdBin" '
        'progId="MSGraph.Chart.8"/>'
        "</a:graphicData></a:graphic></p:graphicFrame>"
    )

    vml_xml = (
        '<xml xmlns:v="urn:schemas-microsoft-com:vml"'
        ' xmlns:o="urn:schemas-microsoft-com:office:office">'
        '<v:shape id="Obj4" o:spid="_x0000_s1026">'
        '<v:imagedata o:relid="rIdVmlImg"/></v:shape></xml>'
    )
    authors_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<p:cmAuthorLst xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
        '<p:cmAuthor id="0" name="김리뷰" initials="KR"/>'
        '<p:cmAuthor id="1" name="이작성" initials="LJ"/>'
        "</p:cmAuthorLst>"
    )
    comments1_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<p:cmLst xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
        '<p:cm authorId="0" dt="2026-11-01T10:00:00" idx="1">'
        "<p:text>유럽 수치 재확인 필요</p:text></p:cm>"
        '<p:cm authorId="1" dt="2026-11-02T11:30:00" idx="2">'
        "<p:text>재무팀 확정 수치로 반영했습니다</p:text>"
        '<p:extLst><p:ext uri="{C676402C-5697-4E1C-873F-D02D1690AC5C}">'
        '<p15:threadingInfo xmlns:p15="http://schemas.microsoft.com/office/powerpoint/2012/main">'
        '<p15:parentCm authorId="0" idx="1"/></p15:threadingInfo></p:ext></p:extLst>'
        "</p:cm></p:cmLst>"
    )
    modern_cm2_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<p188:cmLst xmlns:p188="http://schemas.microsoft.com/office/powerpoint/2018/8/main"'
        ' xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        '<p188:cm id="{C1}" authorId="0" created="2026-11-03T09:00:00Z">'
        "<p188:txBody><a:bodyPr/><a:lstStyle/>"
        "<a:p><a:r><a:t>현대 스레드 주석 본문</a:t></a:r></a:p></p188:txBody>"
        "<p188:replyLst>"
        '<p188:reply id="{R1}" authorId="1" created="2026-11-03T09:15:00Z">'
        "<p188:txBody><a:bodyPr/><a:lstStyle/>"
        "<a:p><a:r><a:t>현대 스레드 답글</a:t></a:r></a:p></p188:txBody>"
        "</p188:reply></p188:replyLst></p188:cm></p188:cmLst>"
    )
    pres_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
        ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        "<p:sldIdLst>"
        '<p:sldId id="256" r:id="rId1"/><p:sldId id="257" r:id="rId2"/>'
        "</p:sldIdLst>"
        f'<p:sldSz cx="{_emu(960)}" cy="{_emu(540)}"/>'
        "</p:presentation>"
    )

    deck_path = tmp_path / "sec5_extensions.pptx"
    with zipfile.ZipFile(deck_path, "w") as zf:
        zf.writestr("ppt/presentation.xml", pres_xml)
        zf.writestr(
            "ppt/_rels/presentation.xml.rels",
            _rels_xml(
                [
                    ("rId1", rel_slide, "slides/slide1.xml"),
                    ("rId2", rel_slide, "slides/slide2.xml"),
                    ("rIdA", rel_authors, "commentAuthors.xml"),
                ]
            ),
        )
        zf.writestr("ppt/commentAuthors.xml", authors_xml)
        zf.writestr("ppt/slides/slide1.xml", s1)
        zf.writestr(
            "ppt/slides/_rels/slide1.xml.rels",
            _rels_xml(
                [
                    ("rIdCx1", rel_chartex, "../charts/chartEx1.xml"),
                    ("rIdCm1", rel_cm, "../comments/comment1.xml"),
                ]
            ),
        )
        zf.writestr("ppt/charts/chartEx1.xml", chartex1)
        zf.writestr("ppt/comments/comment1.xml", comments1_xml)

        zf.writestr("ppt/slides/slide2.xml", s2)
        zf.writestr(
            "ppt/slides/_rels/slide2.xml.rels",
            _rels_xml(
                [
                    ("rIdXlsx", rel_ole, "../embeddings/sheet1.xlsx"),
                    ("rIdBin", rel_ole, "../embeddings/oleObject1.bin"),
                    ("rIdVml", rel_vml, "../drawings/vmlDrawing1.vml"),
                    ("rIdModCm", rel_mod_cm, "../comments/modernComment2.xml"),
                ]
            ),
        )
        zf.writestr("ppt/embeddings/sheet1.xlsx", _make_mini_xlsx_bytes())
        zf.writestr("ppt/embeddings/oleObject1.bin", OLE_MAGIC + b"\x00" * 32)
        zf.writestr("ppt/drawings/vmlDrawing1.vml", vml_xml)
        zf.writestr(
            "ppt/drawings/_rels/vmlDrawing1.vml.rels",
            _rels_xml([("rIdVmlImg", rel_img, "../media/preview1.emf")]),
        )
        zf.writestr("ppt/media/preview1.emf", b"\x01\x00\x00\x00FAKE_EMF_PREVIEW")
        zf.writestr("ppt/comments/modernComment2.xml", modern_cm2_xml)

    # 1. 기본 옵션(include_comments=False): 주석 미포함, ChartEx & 내장 XLSX 표 직접 추출, VML 크롭
    doc_default = parse_document(
        deck_path,
        options=ParseOptions(extract_crops=True, crop_dir=tmp_path / "crops_default"),
    )
    p1, p2 = doc_default.pages
    assert not any(b.block_id.endswith("_comments") for b in p1.blocks)
    assert len(p1.tables) == 1
    assert p1.tables[0].caption == "[트리맵 차트] 권역별 매출 비중"
    assert p1.tables[0].headers == ["항목", "매출(억원)"]
    assert p1.tables[0].rows == [
        ["아시아 / 한국", "520"],
        ["아시아 / 일본", "310"],
        ["유럽 / 독일", "245.5"],
    ]

    assert len(p2.tables) == 1
    assert p2.tables[0].headers == ["Region", "Revenue", "Active"]
    assert p2.tables[0].rows == [["KR", "420.5", "TRUE"], ["JP", "310", "FALSE"]]
    assert len(p2.visual_regions) == 1
    assert p2.visual_regions[0].crop_status == CropStatus.SAVED
    assert p2.visual_regions[0].crop_path is not None
    assert p2.visual_regions[0].crop_path.endswith(".emf")

    # 1-b. PageRenderer가 공급되면 원시 .emf 대신 PNG 크롭을 우선 생성한다
    png_stub = _make_solid_png(100, 70, (10, 80, 150))

    class _MockEmfRenderer:
        @property
        def name(self) -> str:
            return "mock-lo"

        @property
        def available(self) -> bool:
            return True

        def render_region(
            self,
            source_path: Path,
            page_number: int,
            bbox: BBox | None,
            *,
            dpi: int = 150,
        ) -> EncodedImage | None:
            _ = (source_path, page_number, bbox, dpi)
            return EncodedImage(data=png_stub, extension=".png", width=100, height=70)

    doc_rendered = parse_document(
        deck_path,
        options=ParseOptions(pages=(2,), extract_crops=True, crop_dir=tmp_path / "crops_png"),
        page_renderer=_MockEmfRenderer(),
    )
    assert doc_rendered.pages[0].visual_regions[0].crop_path is not None
    assert doc_rendered.pages[0].visual_regions[0].crop_path.endswith(".png")

    # 2. CLI --include-comments 활성화 시 구형 스레드 + 현대 스레드 주석 모두 출력
    out_dir = tmp_path / "sec5_cli_out"
    rc = cli_main(["parse", str(deck_path), "--include-comments", "--out", str(out_dir)])
    assert rc == 0
    _ = capsys.readouterr()
    doc_md = (out_dir / "document.md").read_text(encoding="utf-8")
    assert "김리뷰 (2026-11-01): 유럽 수치 재확인 필요" in doc_md
    assert "↳ 이작성 (2026-11-02): 재무팀 확정 수치로 반영했습니다" in doc_md
    assert "김리뷰 (2026-11-03): 현대 스레드 주석 본문" in doc_md
    assert "↳ 이작성 (2026-11-03): 현대 스레드 답글" in doc_md


def test_pptx_libreoffice_renderer_caching_vector_priority_and_ppt_convert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LibreOfficePptxRenderer의 1회 PDF 변환 캐시, EMF→PNG 우선 렌더링, .ppt 변환을 검증한다."""
    import subprocess

    fake_bin = tmp_path / "soffice"
    fake_bin.write_text("stub", encoding="utf-8")
    calls: list[list[str]] = []

    def _fake_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append(list(cmd))
        fmt = cmd[cmd.index("--convert-to") + 1]
        outdir = Path(cmd[cmd.index("--outdir") + 1])
        src = Path(cmd[-1])
        (outdir / f"{src.stem}.{fmt}").write_bytes(f"FAKE_{fmt}_CONTENT".encode())
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    rendered_png = _make_solid_png(120, 90, (40, 120, 200))

    class _StubPdfRenderer:
        def __init__(self) -> None:
            self.calls: list[tuple[str, int]] = []

        @property
        def name(self) -> str:
            return "stub-pdf"

        @property
        def available(self) -> bool:
            return True

        def render_region(
            self,
            source_path: Path,
            page_number: int,
            bbox: BBox | None,
            *,
            dpi: int = 150,
        ) -> EncodedImage | None:
            _ = (bbox, dpi)
            assert source_path.suffix == ".pdf" and source_path.is_file()
            self.calls.append((source_path.name, page_number))
            return EncodedImage(data=rendered_png, extension=".png", width=120, height=90)

    deck = _build_rich_demo_pptx(tmp_path / "render_demo.pptx")
    stub_pdf = _StubPdfRenderer()

    # Slide 5의 VECTOR_DIAGRAM(기존 renderer_unavailable)이 LibreOfficePptxRenderer로 PNG 크롭됨
    with LibreOfficePptxRenderer(binary=str(fake_bin), pdf_renderer=stub_pdf) as lo_renderer:
        assert lo_renderer.available is True
        assert lo_renderer.name == "libreoffice+stub-pdf"
        doc = parse_document(
            deck,
            options=ParseOptions(pages=(5, 6), extract_crops=True, crop_dir=tmp_path / "lo_crops"),
            page_renderer=lo_renderer,
        )
        # 두 번째 호출도 동일 파일 캐시를 재사용해야 함
        extra = lo_renderer.render_region(deck, 5, None, dpi=150)
        assert extra is not None and extra.extension == ".png"

    # soffice는 동일 .pptx에 대해 단 1회만 실행됨
    assert len(calls) == 1
    p5_vec = next(
        v for v in doc.pages[0].visual_regions if v.element_kind == ElementKind.VECTOR_DIAGRAM
    )
    assert p5_vec.crop_status == CropStatus.SAVED
    assert p5_vec.crop_path is not None and p5_vec.crop_path.endswith(".png")

    # 구형 .ppt → .pptx 변환 헬퍼 검증
    fake_ppt = tmp_path / "legacy_deck.ppt"
    fake_ppt.write_bytes(OLE_MAGIC + b"\x00" * 32)
    converted = convert_ppt_to_pptx(fake_ppt, tmp_path / "converted", binary=str(fake_bin))
    assert converted.is_file() and converted.name == "legacy_deck.pptx"

    # 존재하지 않는 바이너리는 명확한 ParserInputError
    with pytest.raises(ParserInputError, match="LibreOffice"):
        convert_ppt_to_pptx(fake_ppt, tmp_path / "converted2", binary="nonexistent_soffice_bin_xyz")
