"""PPTX 슬라이드 요소 추출기 — 도형·그룹·연결선·표·네이티브 차트·SmartArt·이미지·발표자 노트.

단일 슬라이드 XML과 관계(`.rels`)를 재귀적으로 탐색해 원시 기하·텍스트·구조 데이터를
추출한다. 모든 좌표는 슬라이드 좌상단 원점 기준 포인트(pt)로 정규화된다.
"""

from __future__ import annotations

import contextlib
import io
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field

from autoetl.parser.contracts import ExtractedTable, ParseStrategy, TableCell
from autoetl.parser.pptx.ooxml import (
    MAX_XML_BYTES,
    NS_A,
    NS_ASVG,
    NS_C,
    NS_CX,
    NS_DGM,
    NS_M,
    NS_P,
    NS_P15,
    NS_R,
    NS_SSML,
    REL_TYPE_COMMENTS,
    REL_TYPE_MODERN_COMMENTS,
    REL_TYPE_NOTES_SLIDE,
    CommentAuthor,
    PlaceholderLookup,
    PptxPackage,
    Relationship,
    emu_to_pt,
    iter_children_resolving_mc,
    local_name,
    make_bbox,
    probe_image_dimensions,
    qn,
)

_CHART_TYPE_LABELS: dict[str, str] = {
    "barChart": "막대 차트",
    "bar3DChart": "3D 막대 차트",
    "lineChart": "선 차트",
    "line3DChart": "3D 선 차트",
    "pieChart": "원형 차트",
    "pie3DChart": "3D 원형 차트",
    "doughnutChart": "도넛 차트",
    "areaChart": "영역 차트",
    "area3DChart": "3D 영역 차트",
    "scatterChart": "분산형 차트",
    "bubbleChart": "거품형 차트",
    "radarChart": "방사형 차트",
    "stockChart": "주식형 차트",
    "surfaceChart": "표면형 차트",
    "surface3DChart": "3D 표면형 차트",
    "ofPieChart": "보조 원형 차트",
}

_CHARTEX_LAYOUT_LABELS: dict[str, str] = {
    "waterfall": "폭포 차트",
    "treemap": "트리맵 차트",
    "sunburst": "선버스트 차트",
    "boxWhisker": "상자 수염 차트",
    "funnel": "깔때기 차트",
    "paretoLine": "파레토 차트",
    "clusteredColumn": "히스토그램/막대 차트",
    "regionMap": "지도 차트",
}

_XLSX_CELL_REF_RE = re.compile(r"^([A-Za-z]+)(\d+)$")

_FLOWCHART_PRST_PREFIXES = (
    "flowChart",
    "chevron",
    "homePlate",
    "rightArrow",
    "leftArrow",
    "upArrow",
    "downArrow",
    "bentArrow",
    "curvedRightArrow",
    "curvedLeftArrow",
    "notchedRightArrow",
    "stripedRightArrow",
    "pentagon",
    "blockArc",
    "circularArrow",
)

_BULLET_PREFIXES = ("•", "▪", "▫", "■", "□", "●", "○", "◦", "◆", "◇", "▶", "▷", "·", "-", "*")


@dataclass(frozen=True)
class GroupTransform:
    """중첩 `<p:grpSp>` 내부 좌표를 슬라이드 전역 좌표(pt)로 투영하는 아핀 스케일·이동 변환."""

    sx: float = 1.0
    sy: float = 1.0
    tx: float = 0.0
    ty: float = 0.0

    def map_rect(
        self, x0: float, top: float, x1: float, bottom: float
    ) -> tuple[float, float, float, float]:
        wx0 = self.tx + x0 * self.sx
        wtop = self.ty + top * self.sy
        wx1 = self.tx + x1 * self.sx
        wbot = self.ty + bottom * self.sy
        return (min(wx0, wx1), min(wtop, wbot), max(wx0, wx1), max(wtop, wbot))

    def compose_group(
        self,
        *,
        off_x: float,
        off_y: float,
        ext_w: float,
        ext_h: float,
        ch_off_x: float,
        ch_off_y: float,
        ch_ext_w: float,
        ch_ext_h: float,
    ) -> GroupTransform:
        local_sx = (ext_w / ch_ext_w) if ch_ext_w > 1e-6 else 1.0
        local_sy = (ext_h / ch_ext_h) if ch_ext_h > 1e-6 else 1.0
        local_tx = off_x - ch_off_x * local_sx
        local_ty = off_y - ch_off_y * local_sy
        return GroupTransform(
            sx=self.sx * local_sx,
            sy=self.sy * local_sy,
            tx=self.tx + self.sx * local_tx,
            ty=self.ty + self.sy * local_ty,
        )


@dataclass(frozen=True)
class RawPptxParagraph:
    """도형 텍스트 본문 안의 단일 문단."""

    text: str
    level: int = 0
    is_bullet: bool = False
    is_numbered: bool = False
    font_size: float | None = None
    is_bold: bool = False
    is_italic: bool = False
    has_math: bool = False


@dataclass(frozen=True)
class RawPptxShape:
    """슬라이드 내 텍스트/도형 객체(`<p:sp>`)."""

    shape_id: str
    name: str
    ph_type: str | None
    ph_idx: str | None
    rect_pt: tuple[float, float, float, float]
    paragraphs: tuple[RawPptxParagraph, ...]
    prst_geom: str | None = None
    has_fill: bool = False
    has_stroke: bool = False
    is_text_box: bool = False
    in_group: bool = False
    is_slide_num_field: bool = False

    @property
    def full_text(self) -> str:
        return "\n".join(p.text for p in self.paragraphs if p.text.strip()).strip()


@dataclass(frozen=True)
class RawPptxConnector:
    """도형 간 화살표/연결선(`<p:cxnSp>`)."""

    shape_id: str
    rect_pt: tuple[float, float, float, float]
    start_shape_id: str | None = None
    end_shape_id: str | None = None
    has_arrow: bool = False


@dataclass(frozen=True)
class RawPptxImage:
    """슬라이드 내 삽입 이미지(`<p:pic>` 및 `asvg:svgBlip`)."""

    shape_id: str
    name: str
    alt_text: str
    media_part: str
    extension: str
    rect_pt: tuple[float, float, float, float]
    raw_bytes: bytes | None
    pixel_width: int | None
    pixel_height: int | None
    svg_text_hints: tuple[str, ...] = ()


@dataclass(frozen=True)
class RawPptxChart:
    """슬라이드 내 네이티브 OOXML 차트(`ppt/charts/chart*.xml`)."""

    shape_id: str
    name: str
    chart_part: str
    chart_type: str
    chart_type_label: str
    title: str | None
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    rect_pt: tuple[float, float, float, float]

    @property
    def has_data(self) -> bool:
        return len(self.headers) >= 2 and len(self.rows) >= 1


@dataclass(frozen=True)
class RawPptxSmartArt:
    """슬라이드 내 SmartArt 다이어그램(`ppt/diagrams/data*.xml`)."""

    shape_id: str
    name: str
    data_part: str
    items: tuple[tuple[int, str], ...]  # (depth, text)
    rect_pt: tuple[float, float, float, float]


@dataclass(frozen=True)
class RawPptxTable:
    """슬라이드 내 네이티브 표(`<a:tbl>`)."""

    shape_id: str
    name: str
    rect_pt: tuple[float, float, float, float]
    table: ExtractedTable


@dataclass(frozen=True)
class RawPptxOleObject:
    """슬라이드 내 내장 OLE 객체(`<p:oleObj>`, 예: MSGraph 차트·내장 Excel 시트)."""

    shape_id: str
    name: str
    prog_id: str
    rect_pt: tuple[float, float, float, float]
    preview_image: RawPptxImage | None = None


@dataclass(frozen=True)
class RawPptxComment:
    """슬라이드 검토 주석(`comment*.xml` / Office 365 현대 스레드 주석)."""

    author: str
    initials: str
    timestamp: str
    text: str
    is_reply: bool = False


@dataclass
class RawSlideData:
    """단일 슬라이드에서 추출한 모든 원시 구조 객체 모음."""

    slide_number: int
    slide_id: str
    part_path: str
    width_pt: float
    height_pt: float
    is_hidden: bool = False
    shapes: list[RawPptxShape] = field(default_factory=list)
    connectors: list[RawPptxConnector] = field(default_factory=list)
    tables: list[RawPptxTable] = field(default_factory=list)
    charts: list[RawPptxChart] = field(default_factory=list)
    smartarts: list[RawPptxSmartArt] = field(default_factory=list)
    images: list[RawPptxImage] = field(default_factory=list)
    ole_objects: list[RawPptxOleObject] = field(default_factory=list)
    comments: list[RawPptxComment] = field(default_factory=list)
    speaker_notes: str = ""
    warnings: list[str] = field(default_factory=list)


def _extract_rect_from_xfrm(
    xfrm: ET.Element | None,
    transform: GroupTransform,
) -> tuple[float, float, float, float] | None:
    if xfrm is None:
        return None
    off = xfrm.find(qn(NS_A, "off"))
    ext = xfrm.find(qn(NS_A, "ext"))
    if off is None or ext is None:
        return None
    x0 = emu_to_pt(off.attrib.get("x"))
    top = emu_to_pt(off.attrib.get("y"))
    w = max(0.0, emu_to_pt(ext.attrib.get("cx")))
    h = max(0.0, emu_to_pt(ext.attrib.get("cy")))
    return transform.map_rect(x0, top, x0 + w, top + h)


def _format_chart_number(raw: str) -> str:
    cleaned = raw.strip()
    if not cleaned:
        return ""
    try:
        val = float(cleaned)
    except ValueError:
        return cleaned
    if val != val:  # NaN
        return ""
    if abs(val - round(val)) < 1e-9 and abs(val) < 1e15:
        return str(round(val))
    return f"{val:.6g}"


def _extract_math_text(elem: ET.Element) -> str:
    parts: list[str] = []
    for node in elem.iter():
        if local_name(node.tag) == "t" and node.text:
            parts.append(node.text)
    return "".join(parts).strip()


def _extract_paragraphs_from_txbody(
    tx_body: ET.Element | None,
    *,
    default_font_size: float | None = None,
) -> tuple[RawPptxParagraph, ...]:
    if tx_body is None:
        return ()

    # txBody 자체의 lstStyle에서 레벨별 기본 글꼴 크기 추출
    lvl_fonts: dict[int, float] = {}
    lst_style = tx_body.find(qn(NS_A, "lstStyle"))
    if lst_style is not None:
        for lvl_idx in range(1, 10):
            ppr = lst_style.find(qn(NS_A, f"lvl{lvl_idx}pPr"))
            if ppr is not None:
                def_rpr = ppr.find(qn(NS_A, "defRPr"))
                if def_rpr is not None and "sz" in def_rpr.attrib:
                    with contextlib.suppress(ValueError):
                        lvl_fonts[lvl_idx - 1] = round(int(def_rpr.attrib["sz"]) / 100.0, 2)

    paragraphs: list[RawPptxParagraph] = []
    for p_el in tx_body.findall(qn(NS_A, "p")):
        ppr = p_el.find(qn(NS_A, "pPr"))
        level = 0
        is_bullet = False
        is_numbered = False
        explicit_no_bullet = False
        p_def_font: float | None = None

        if ppr is not None:
            try:
                level = max(0, int(ppr.attrib.get("lvl", "0")))
            except ValueError:
                level = 0
            if ppr.find(qn(NS_A, "buNone")) is not None:
                explicit_no_bullet = True
            elif ppr.find(qn(NS_A, "buAutoNum")) is not None:
                is_bullet = True
                is_numbered = True
            elif (
                ppr.find(qn(NS_A, "buChar")) is not None or ppr.find(qn(NS_A, "buBlip")) is not None
            ):
                is_bullet = True
            def_rpr = ppr.find(qn(NS_A, "defRPr"))
            if def_rpr is not None and "sz" in def_rpr.attrib:
                with contextlib.suppress(ValueError):
                    p_def_font = round(int(def_rpr.attrib["sz"]) / 100.0, 2)

        pieces: list[str] = []
        run_fonts: list[float] = []
        bold_chars = 0
        italic_chars = 0
        total_chars = 0
        has_math = False

        for child in iter_children_resolving_mc(p_el):
            tag = local_name(child.tag)
            if tag == "r":
                rpr = child.find(qn(NS_A, "rPr"))
                t_el = child.find(qn(NS_A, "t"))
                txt = t_el.text if (t_el is not None and t_el.text) else ""
                if not txt:
                    continue
                pieces.append(txt)
                n_ch = len(txt.strip())
                total_chars += n_ch
                if rpr is not None:
                    if "sz" in rpr.attrib:
                        with contextlib.suppress(ValueError):
                            run_fonts.append(round(int(rpr.attrib["sz"]) / 100.0, 2))
                    if rpr.attrib.get("b") in ("1", "true", "True"):
                        bold_chars += n_ch
                    if rpr.attrib.get("i") in ("1", "true", "True"):
                        italic_chars += n_ch
            elif tag == "fld":
                t_el = child.find(qn(NS_A, "t"))
                if t_el is not None and t_el.text:
                    pieces.append(t_el.text)
                    total_chars += len(t_el.text.strip())
            elif tag == "br":
                pieces.append("\n")
            elif tag == "m" or child.tag == qn(NS_M, "oMath") or child.tag == qn(NS_M, "oMathPara"):
                m_txt = _extract_math_text(child)
                if m_txt:
                    pieces.append(m_txt)
                    total_chars += len(m_txt)
                    has_math = True

        raw_text = "".join(pieces)
        cleaned_lines = [ln.strip() for ln in raw_text.splitlines()]
        text = "\n".join(ln for ln in cleaned_lines if ln).strip()
        if not text:
            continue

        if (
            not explicit_no_bullet
            and not is_bullet
            and (
                level >= 1
                or (
                    text.startswith(_BULLET_PREFIXES)
                    and len(text) >= 3
                    and text[1:2] in (" ", "\t")
                )
            )
        ):
            is_bullet = True

        eff_font: float | None = None
        if run_fonts:
            run_fonts.sort()
            eff_font = run_fonts[len(run_fonts) // 2]
        elif p_def_font is not None:
            eff_font = p_def_font
        elif level in lvl_fonts:
            eff_font = lvl_fonts[level]
        else:
            eff_font = default_font_size

        is_bold = total_chars > 0 and (bold_chars / total_chars) >= 0.65
        is_italic = total_chars > 0 and (italic_chars / total_chars) >= 0.65
        paragraphs.append(
            RawPptxParagraph(
                text=text,
                level=level,
                is_bullet=is_bullet,
                is_numbered=is_numbered,
                font_size=eff_font,
                is_bold=is_bold,
                is_italic=is_italic,
                has_math=has_math,
            )
        )
    return tuple(paragraphs)


def _extract_native_table(
    tbl_el: ET.Element,
    *,
    slide_number: int,
    table_index: int,
    rect_pt: tuple[float, float, float, float],
    slide_w: float,
    slide_h: float,
    shape_name: str,
) -> ExtractedTable | None:
    """OOXML `<a:tbl>`에서 병합 셀(`gridSpan`/`rowSpan`)까지 보존해 표를 추출한다."""
    tr_list = tbl_el.findall(qn(NS_A, "tr"))
    if not tr_list:
        return None

    tbl_grid = tbl_el.find(qn(NS_A, "tblGrid"))
    col_widths = [
        max(1.0, emu_to_pt(gc.attrib.get("w"), 72.0))
        for gc in (tbl_grid.findall(qn(NS_A, "gridCol")) if tbl_grid is not None else [])
    ]
    row_heights = [max(1.0, emu_to_pt(tr.attrib.get("h"), 20.0)) for tr in tr_list]

    raw_rows_tc = [tr.findall(qn(NS_A, "tc")) for tr in tr_list]
    n_rows = len(raw_rows_tc)
    n_cols = max(len(col_widths), max((len(tcs) for tcs in raw_rows_tc), default=0))
    if n_rows == 0 or n_cols == 0:
        return None

    if len(col_widths) < n_cols:
        avg_w = (sum(col_widths) / len(col_widths)) if col_widths else 72.0
        col_widths.extend([avg_w] * (n_cols - len(col_widths)))

    tbl_pr = tbl_el.find(qn(NS_A, "tblPr"))
    first_row_header = True
    if tbl_pr is not None and tbl_pr.attrib.get("firstRow") in ("0", "false", "False"):
        first_row_header = False

    # 누적 x/y 비율로 셀 좌표 계산
    tx0, ttop, tx1, tbot = rect_pt
    tw = max(1.0, tx1 - tx0)
    th = max(1.0, tbot - ttop)
    total_cw = max(1.0, sum(col_widths[:n_cols]))
    total_rh = max(1.0, sum(row_heights))

    col_xs = [tx0]
    acc_w = 0.0
    for cw in col_widths[:n_cols]:
        acc_w += cw
        col_xs.append(tx0 + tw * (acc_w / total_cw))

    row_ys = [ttop]
    acc_h = 0.0
    for rh in row_heights:
        acc_h += rh
        row_ys.append(ttop + th * (acc_h / total_rh))

    grid_text: list[list[str]] = [["" for _ in range(n_cols)] for _ in range(n_rows)]
    cells: list[TableCell] = []
    has_merged = False

    merged_header_cols: set[int] = set()

    for r_idx, tcs in enumerate(raw_rows_tc):
        for c_idx, tc in enumerate(tcs[:n_cols]):
            is_h_merge = tc.attrib.get("hMerge") in ("1", "true", "True")
            is_v_merge = tc.attrib.get("vMerge") in ("1", "true", "True")
            if is_h_merge or is_v_merge:
                has_merged = True
                if r_idx == 0 and is_h_merge:
                    merged_header_cols.add(c_idx)
                continue

            try:
                col_span = max(1, int(tc.attrib.get("gridSpan", "1")))
            except ValueError:
                col_span = 1
            try:
                row_span = max(1, int(tc.attrib.get("rowSpan", "1")))
            except ValueError:
                row_span = 1
            col_span = min(col_span, n_cols - c_idx)
            row_span = min(row_span, n_rows - r_idx)
            if col_span > 1 or row_span > 1:
                has_merged = True
                if r_idx == 0 and col_span > 1:
                    merged_header_cols.update(range(c_idx + 1, c_idx + col_span))

            tx_body = tc.find(qn(NS_A, "txBody"))
            paras = _extract_paragraphs_from_txbody(tx_body)
            cell_text = " / ".join(p.text for p in paras if p.text.strip()).strip()
            grid_text[r_idx][c_idx] = cell_text

            cx0 = col_xs[c_idx]
            cx1 = col_xs[min(n_cols, c_idx + col_span)]
            cy0 = row_ys[r_idx]
            cy1 = row_ys[min(n_rows, r_idx + row_span)]
            cells.append(
                TableCell(
                    row=r_idx,
                    col=c_idx,
                    row_span=row_span,
                    col_span=col_span,
                    text=cell_text,
                    is_header=(r_idx == 0 and first_row_header and n_rows >= 2),
                    bbox=make_bbox(cx0, cy0, cx1, cy1, slide_w, slide_h),
                )
            )

    if not any(cell.text for cell in cells):
        return None

    # 모든 행(헤더 포함)이 완전히 비어 있는 패딩 열(LibreOffice/Excel 병합 여백 열) 압축
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

    header_row_count = 1 if (first_row_header and n_rows >= 2) else 0
    if header_row_count == 1:
        headers = [
            grid_text[0][c] if (grid_text[0][c] or c in merged_header_cols) else f"col_{c + 1}"
            for c in range(n_cols)
        ]
        data_rows = grid_text[1:]
    else:
        headers = [f"col_{c + 1}" for c in range(n_cols)]
        data_rows = grid_text

    table_id = f"p{slide_number:03d}_t{table_index:02d}"
    merged_note = ", 병합 셀 포함" if has_merged else ""
    return ExtractedTable(
        table_id=table_id,
        page=slide_number,
        bbox=make_bbox(tx0, ttop, tx1, tbot, slide_w, slide_h),
        strategy=ParseStrategy.RULED_TABLE,
        confidence=0.98,
        row_count=n_rows,
        col_count=n_cols,
        header_row_count=header_row_count,
        headers=headers,
        rows=data_rows,
        cells=cells,
        has_merged_cells=has_merged,
        caption=None,
        reason=(
            f"PPTX 네이티브 OOXML 표 직접 추출({n_rows}x{n_cols}{merged_note}, 도형 `{shape_name}`)"
        ),
    )


def _read_chart_cache(ref_parent: ET.Element | None) -> list[str]:
    """`<c:cat>`·`<c:val>`·`<c:xVal>`·`<c:yVal>` 안의 strCache/numCache/multiLvlStrRef를 읽는다."""
    if ref_parent is None:
        return []

    # 1. 다단계 카테고리(multiLvlStrRef)
    multi = ref_parent.find(qn(NS_C, "multiLvlStrRef"))
    if multi is not None:
        cache = multi.find(qn(NS_C, "multiLvlStrCache"))
        if cache is not None:
            lvls = cache.findall(qn(NS_C, "lvl"))
            if lvls:
                pt_maps: list[dict[int, str]] = []
                max_idx = -1
                for lvl in lvls:
                    pmap: dict[int, str] = {}
                    for pt in lvl.findall(qn(NS_C, "pt")):
                        v = pt.find(qn(NS_C, "v"))
                        try:
                            idx = int(pt.attrib.get("idx", "-1"))
                        except ValueError:
                            continue
                        if idx >= 0 and v is not None and v.text:
                            pmap[idx] = v.text.strip()
                            max_idx = max(max_idx, idx)
                    pt_maps.append(pmap)
                if max_idx >= 0:
                    out: list[str] = []
                    for i in range(max_idx + 1):
                        parts = [m[i] for m in reversed(pt_maps) if i in m]
                        out.append(" / ".join(parts))
                    return out

    # 2. 일반 문자열/숫자 캐시 또는 리터럴
    for container_tag, cache_tag, is_num in (
        ("strRef", "strCache", False),
        ("strLit", None, False),
        ("numRef", "numCache", True),
        ("numLit", None, True),
    ):
        cont = ref_parent.find(qn(NS_C, container_tag))
        if cont is None:
            continue
        src = cont.find(qn(NS_C, cache_tag)) if cache_tag is not None else cont
        if src is None:
            continue
        pts: dict[int, str] = {}
        max_idx = -1
        for pt in src.findall(qn(NS_C, "pt")):
            v = pt.find(qn(NS_C, "v"))
            try:
                idx = int(pt.attrib.get("idx", "-1"))
            except ValueError:
                continue
            if idx < 0:
                continue
            raw_v = v.text if (v is not None and v.text) else ""
            pts[idx] = _format_chart_number(raw_v) if is_num else raw_v.strip()
            max_idx = max(max_idx, idx)
        if max_idx >= 0:
            return [pts.get(i, "") for i in range(max_idx + 1)]
    return []


def _extract_chart_title(chart_el: ET.Element) -> str | None:
    title_el = chart_el.find(qn(NS_C, "title"))
    if title_el is None:
        return None
    parts: list[str] = []
    for t_el in title_el.iter(qn(NS_A, "t")):
        if t_el.text and t_el.text.strip():
            parts.append(t_el.text.strip())
    if parts:
        return " ".join(parts).strip()
    for v_el in title_el.iter(qn(NS_C, "v")):
        if v_el.text and v_el.text.strip():
            parts.append(v_el.text.strip())
    return " ".join(parts).strip() or None


def _extract_series_name(ser_el: ET.Element, fallback_idx: int) -> str:
    tx = ser_el.find(qn(NS_C, "tx"))
    if tx is not None:
        v_el = tx.find(qn(NS_C, "v"))
        if v_el is not None and v_el.text and v_el.text.strip():
            return v_el.text.strip()
        vals = _read_chart_cache(tx)
        if vals and vals[0].strip():
            return vals[0].strip()
    return f"Series {fallback_idx}"


def _extract_native_chart(
    pkg: PptxPackage,
    chart_part: str,
    *,
    shape_id: str,
    shape_name: str,
    rect_pt: tuple[float, float, float, float],
) -> RawPptxChart | None:
    """`ppt/charts/chart*.xml`에서 차트 유형·제목·시리즈 수치 데이터를 결정론적으로 추출한다."""
    root = pkg.read_xml(chart_part)
    if root is None:
        return None
    chart_el = root.find(qn(NS_C, "chart"))
    if chart_el is None:
        return None
    plot_area = chart_el.find(qn(NS_C, "plotArea"))
    if plot_area is None:
        return None

    title = _extract_chart_title(chart_el)
    found_types: list[str] = []
    series_names: list[str] = []
    series_values: list[list[str]] = []
    categories: list[str] = []

    for child in plot_area:
        tag = local_name(child.tag)
        if tag not in _CHART_TYPE_LABELS:
            continue
        if tag not in found_types:
            found_types.append(tag)
        for ser in child.findall(qn(NS_C, "ser")):
            s_idx = len(series_names) + 1
            s_name = _extract_series_name(ser, s_idx)
            # 일반 차트(cat + val) 또는 분산/거품형 차트(xVal + yVal)
            cat_vals = _read_chart_cache(ser.find(qn(NS_C, "cat")))
            if not cat_vals:
                cat_vals = _read_chart_cache(ser.find(qn(NS_C, "xVal")))
            y_vals = _read_chart_cache(ser.find(qn(NS_C, "val")))
            if not y_vals:
                y_vals = _read_chart_cache(ser.find(qn(NS_C, "yVal")))

            if cat_vals and len(cat_vals) > len(categories):
                categories = cat_vals
            if y_vals:
                series_names.append(s_name)
                series_values.append(y_vals)

    chart_type = "+".join(found_types) if found_types else "chart"
    if len(found_types) == 1:
        chart_label = _CHART_TYPE_LABELS[found_types[0]]
    elif len(found_types) > 1:
        chart_label = "복합 차트(" + "+".join(_CHART_TYPE_LABELS[t] for t in found_types) + ")"
    else:
        chart_label = "차트"

    if not series_values:
        return RawPptxChart(
            shape_id=shape_id,
            name=shape_name,
            chart_part=chart_part,
            chart_type=chart_type,
            chart_type_label=chart_label,
            title=title,
            headers=(),
            rows=(),
            rect_pt=rect_pt,
        )

    max_rows = max(len(categories), max(len(vals) for vals in series_values))
    if not categories:
        categories = [f"Item {i + 1}" for i in range(max_rows)]
    elif len(categories) < max_rows:
        categories = [*categories, *(f"Item {i + 1}" for i in range(len(categories), max_rows))]

    headers = ("항목", *series_names)
    rows: list[tuple[str, ...]] = []
    for r_idx in range(max_rows):
        row_cells = [categories[r_idx]]
        for vals in series_values:
            row_cells.append(vals[r_idx] if r_idx < len(vals) else "")
        rows.append(tuple(row_cells))

    return RawPptxChart(
        shape_id=shape_id,
        name=shape_name,
        chart_part=chart_part,
        chart_type=chart_type,
        chart_type_label=chart_label,
        title=title,
        headers=headers,
        rows=tuple(rows),
        rect_pt=rect_pt,
    )


def _extract_chartex_text(tx_el: ET.Element | None) -> str:
    """`chartEx` `<cx:tx>`에서 `<cx:txData><cx:v>` 또는 `<cx:rich>` 텍스트를 읽는다."""
    if tx_el is None:
        return ""
    tx_data = tx_el.find(qn(NS_CX, "txData"))
    if tx_data is not None:
        v_el = tx_data.find(qn(NS_CX, "v"))
        if v_el is not None and v_el.text and v_el.text.strip():
            return v_el.text.strip()
    rich = tx_el.find(qn(NS_CX, "rich"))
    if rich is not None:
        parts = [t.text.strip() for t in rich.iter(qn(NS_A, "t")) if t.text and t.text.strip()]
        if parts:
            return " ".join(parts).strip()
    return ""


def _read_chartex_dim(dim_el: ET.Element, *, is_numeric: bool) -> list[str]:
    """`chartEx` `<cx:strDim>`/`<cx:numDim>` 안의 단일·다단계 `<cx:lvl>` 값을 읽는다."""
    lvls = dim_el.findall(qn(NS_CX, "lvl"))
    if not lvls:
        return []
    pt_maps: list[dict[int, str]] = []
    max_idx = -1
    for lvl in lvls:
        pmap: dict[int, str] = {}
        for pt in lvl.findall(qn(NS_CX, "pt")):
            try:
                idx = int(pt.attrib.get("idx", "-1"))
            except ValueError:
                continue
            if idx < 0:
                continue
            raw_v = (pt.text or "").strip()
            pmap[idx] = _format_chart_number(raw_v) if is_numeric else raw_v
            max_idx = max(max_idx, idx)
        pt_maps.append(pmap)
    if max_idx < 0:
        return []
    if len(pt_maps) == 1:
        return [pt_maps[0].get(i, "") for i in range(max_idx + 1)]
    out: list[str] = []
    for i in range(max_idx + 1):
        parts = [m[i] for m in reversed(pt_maps) if m.get(i)]
        out.append(" / ".join(parts))
    return out


def _extract_chartex(
    pkg: PptxPackage,
    chart_part: str,
    *,
    shape_id: str,
    shape_name: str,
    rect_pt: tuple[float, float, float, float],
) -> RawPptxChart | None:
    """Office 2016+ 확장 차트(`ppt/charts/chartEx*.xml`: 폭포·트리맵·선버스트·깔때기 등) 추출."""
    root = pkg.read_xml(chart_part)
    if root is None:
        return None
    chart_el = root.find(qn(NS_CX, "chart"))
    if chart_el is None:
        return None

    # 1. <cx:chartData><cx:data id="..."> 매핑 읽기
    data_by_id: dict[str, tuple[list[str], list[str]]] = {}
    chart_data = root.find(qn(NS_CX, "chartData"))
    if chart_data is not None:
        for data_el in chart_data.findall(qn(NS_CX, "data")):
            did = (data_el.attrib.get("id") or "").strip()
            if not did:
                continue
            cats: list[str] = []
            vals: list[str] = []
            for child in data_el:
                tag = local_name(child.tag)
                if tag == "strDim" and not cats:
                    cats = _read_chartex_dim(child, is_numeric=False)
                elif tag == "numDim":
                    dtype = (child.attrib.get("type") or "").lower()
                    num_vals = _read_chartex_dim(child, is_numeric=True)
                    if dtype in ("val", "size", "y", "") or not vals:
                        vals = num_vals
            data_by_id[did] = (cats, vals)

    # 2. 제목 및 시리즈별 layoutId·데이터 결합
    title_el = chart_el.find(qn(NS_CX, "title"))
    title_txt = _extract_chartex_text(
        title_el.find(qn(NS_CX, "tx")) if title_el is not None else None
    )
    title = title_txt or None

    found_types: list[str] = []
    series_names: list[str] = []
    series_values: list[list[str]] = []
    categories: list[str] = []

    plot_area = chart_el.find(qn(NS_CX, "plotArea"))
    region = plot_area.find(qn(NS_CX, "plotAreaRegion")) if plot_area is not None else None
    if region is not None:
        for ser in region.findall(qn(NS_CX, "series")):
            if ser.attrib.get("hidden") in ("1", "true", "True"):
                continue
            layout_id = (ser.attrib.get("layoutId") or "").strip()
            if layout_id and layout_id not in found_types:
                found_types.append(layout_id)
            s_idx = len(series_names) + 1
            s_name = _extract_chartex_text(ser.find(qn(NS_CX, "tx"))) or f"Series {s_idx}"
            data_id_el = ser.find(qn(NS_CX, "dataId"))
            did = (data_id_el.attrib.get("val") or "").strip() if data_id_el is not None else ""
            cats, vals = data_by_id.get(did, ([], []))
            if cats and len(cats) > len(categories):
                categories = cats
            if vals:
                series_names.append(s_name)
                series_values.append(vals)

    # plotAreaRegion에 dataId 연결이 없더라도 chartData가 있으면 폴백 수집
    if not series_values and data_by_id:
        for idx, (_, (cats, vals)) in enumerate(data_by_id.items(), 1):
            if cats and len(cats) > len(categories):
                categories = cats
            if vals:
                series_names.append(f"Series {idx}")
                series_values.append(vals)

    chart_type = "+".join(found_types) if found_types else "chartEx"
    if len(found_types) == 1:
        chart_label = _CHARTEX_LAYOUT_LABELS.get(found_types[0], f"확장 차트({found_types[0]})")
    elif len(found_types) > 1:
        labels = [_CHARTEX_LAYOUT_LABELS.get(t, t) for t in found_types]
        chart_label = "복합 확장 차트(" + "+".join(labels) + ")"
    else:
        chart_label = "확장 차트"

    if not series_values:
        return RawPptxChart(
            shape_id=shape_id,
            name=shape_name,
            chart_part=chart_part,
            chart_type=chart_type,
            chart_type_label=chart_label,
            title=title,
            headers=(),
            rows=(),
            rect_pt=rect_pt,
        )

    max_rows = max(len(categories), max(len(vals) for vals in series_values))
    if not categories:
        categories = [f"Item {i + 1}" for i in range(max_rows)]
    elif len(categories) < max_rows:
        categories = [*categories, *(f"Item {i + 1}" for i in range(len(categories), max_rows))]

    headers = ("항목", *series_names)
    rows: list[tuple[str, ...]] = []
    for r_idx in range(max_rows):
        row_cells = [categories[r_idx]]
        for vals in series_values:
            row_cells.append(vals[r_idx] if r_idx < len(vals) else "")
        rows.append(tuple(row_cells))

    return RawPptxChart(
        shape_id=shape_id,
        name=shape_name,
        chart_part=chart_part,
        chart_type=chart_type,
        chart_type_label=chart_label,
        title=title,
        headers=headers,
        rows=tuple(rows),
        rect_pt=rect_pt,
    )


def _xlsx_col_to_index(col_letters: str) -> int:
    idx = 0
    for ch in col_letters.upper():
        idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return idx - 1


def _extract_embedded_xlsx_table(
    xlsx_bytes: bytes,
    *,
    slide_number: int,
    table_index: int,
    rect_pt: tuple[float, float, float, float],
    slide_w: float,
    slide_h: float,
    ole_name: str,
) -> ExtractedTable | None:
    """내장 Excel(`.xlsx`) OLE 객체에서 첫 번째 워크시트 표 데이터를 직접 추출한다."""
    if not xlsx_bytes.startswith(b"PK"):
        return None
    try:
        zf = zipfile.ZipFile(io.BytesIO(xlsx_bytes), "r")
    except zipfile.BadZipFile:
        return None

    with zf:
        names = set(zf.namelist())
        if "xl/workbook.xml" not in names:
            return None

        def _read_xml_safe(part: str) -> ET.Element | None:
            if part not in names:
                return None
            info = zf.getinfo(part)
            if info.file_size > MAX_XML_BYTES:
                return None
            try:
                raw = zf.read(info)
                if len(raw) > MAX_XML_BYTES:
                    return None
                return ET.fromstring(raw)
            except (zipfile.BadZipFile, ET.ParseError, OSError):
                return None

        # 공유 문자열(xl/sharedStrings.xml) 로드
        shared_strings: list[str] = []
        ss_root = _read_xml_safe("xl/sharedStrings.xml")
        if ss_root is not None:
            for si in ss_root.findall(qn(NS_SSML, "si")):
                parts = [t.text for t in si.iter(qn(NS_SSML, "t")) if t.text]
                shared_strings.append("".join(parts).strip())

        # 첫 번째 가시 시트 경로 탐색
        sheet_part: str | None = None
        wb_root = _read_xml_safe("xl/workbook.xml")
        wb_rels_root = _read_xml_safe("xl/_rels/workbook.xml.rels")
        wb_rel_map: dict[str, str] = {}
        if wb_rels_root is not None:
            for rel_el in wb_rels_root:
                if local_name(rel_el.tag) == "Relationship":
                    rid = rel_el.attrib.get("Id", "")
                    target = (rel_el.attrib.get("Target") or "").lstrip("/")
                    if rid and target:
                        norm = target if target.startswith("xl/") else f"xl/{target}"
                        wb_rel_map[rid] = posixpath.normpath(norm)
        if wb_root is not None:
            sheets_el = wb_root.find(qn(NS_SSML, "sheets"))
            if sheets_el is not None:
                for sh_el in sheets_el.findall(qn(NS_SSML, "sheet")):
                    if sh_el.attrib.get("state") in ("hidden", "veryHidden"):
                        continue
                    rid = sh_el.attrib.get(qn(NS_R, "id"), "")
                    cand = wb_rel_map.get(rid)
                    if cand and cand in names:
                        sheet_part = cand
                        break
        if sheet_part is None and "xl/worksheets/sheet1.xml" in names:
            sheet_part = "xl/worksheets/sheet1.xml"
        if sheet_part is None:
            return None

        sh_root = _read_xml_safe(sheet_part)
        if sh_root is None:
            return None
        sheet_data = sh_root.find(qn(NS_SSML, "sheetData"))
        if sheet_data is None:
            return None

        row_map: dict[int, dict[int, str]] = {}
        min_col: int | None = None
        max_col = -1
        for r_idx_fallback, row_el in enumerate(sheet_data.findall(qn(NS_SSML, "row"))):
            try:
                r_num = int(row_el.attrib.get("r", str(r_idx_fallback + 1))) - 1
            except ValueError:
                r_num = r_idx_fallback
            for c_idx_fallback, c_el in enumerate(row_el.findall(qn(NS_SSML, "c"))):
                ref = c_el.attrib.get("r", "")
                m = _XLSX_CELL_REF_RE.match(ref)
                if m is not None:
                    c_idx = _xlsx_col_to_index(m.group(1))
                    r_num_cell = int(m.group(2)) - 1
                else:
                    c_idx = c_idx_fallback
                    r_num_cell = r_num

                ctype = c_el.attrib.get("t", "")
                val = ""
                if ctype == "s":
                    v_el = c_el.find(qn(NS_SSML, "v"))
                    if v_el is not None and v_el.text and v_el.text.strip().isdigit():
                        s_i = int(v_el.text.strip())
                        if 0 <= s_i < len(shared_strings):
                            val = shared_strings[s_i]
                elif ctype == "inlineStr":
                    is_el = c_el.find(qn(NS_SSML, "is"))
                    if is_el is not None:
                        val = "".join(
                            t.text for t in is_el.iter(qn(NS_SSML, "t")) if t.text
                        ).strip()
                elif ctype == "b":
                    v_el = c_el.find(qn(NS_SSML, "v"))
                    val = "TRUE" if (v_el is not None and v_el.text == "1") else "FALSE"
                else:
                    v_el = c_el.find(qn(NS_SSML, "v"))
                    if v_el is not None and v_el.text:
                        val = (
                            v_el.text.strip() if ctype == "str" else _format_chart_number(v_el.text)
                        )
                if val:
                    row_map.setdefault(r_num_cell, {})[c_idx] = val
                    min_col = c_idx if min_col is None else min(min_col, c_idx)
                    max_col = max(max_col, c_idx)

        if not row_map or min_col is None:
            return None

        sorted_rows = sorted(row_map)
        col_indices = [
            c
            for c in range(min_col, max_col + 1)
            if any(row_map[r].get(c, "").strip() for r in sorted_rows)
        ]
        if not col_indices:
            return None

        grid = [[row_map[r].get(c, "") for c in col_indices] for r in sorted_rows]
        n_rows = len(grid)
        n_cols = len(col_indices)
        header_row_count = 1 if n_rows >= 2 else 0
        if header_row_count == 1:
            headers = [grid[0][c] or f"col_{c + 1}" for c in range(n_cols)]
            data_rows = grid[1:]
        else:
            headers = [f"col_{c + 1}" for c in range(n_cols)]
            data_rows = grid

        cells: list[TableCell] = []
        for r_i, row_vals in enumerate(grid):
            for c_i, txt in enumerate(row_vals):
                cells.append(
                    TableCell(
                        row=r_i,
                        col=c_i,
                        text=txt,
                        is_header=(r_i == 0 and header_row_count == 1),
                    )
                )

        tx0, ttop, tx1, tbot = rect_pt
        return ExtractedTable(
            table_id=f"p{slide_number:03d}_t{table_index:02d}",
            page=slide_number,
            bbox=make_bbox(tx0, ttop, tx1, tbot, slide_w, slide_h),
            strategy=ParseStrategy.RULED_TABLE,
            confidence=0.97,
            row_count=n_rows,
            col_count=n_cols,
            header_row_count=header_row_count,
            headers=headers,
            rows=data_rows,
            cells=cells,
            has_merged_cells=False,
            caption=f"[내장 Excel 시트] {ole_name}" if ole_name else None,
            reason=f"PPTX 내장 Excel(.xlsx) OLE 시트 직접 추출({n_rows}x{n_cols}, `{ole_name}`)",
        )


def _extract_smartart_diagram(
    pkg: PptxPackage,
    data_part: str,
    *,
    shape_id: str,
    shape_name: str,
    rect_pt: tuple[float, float, float, float],
) -> RawPptxSmartArt | None:
    """`ppt/diagrams/data*.xml`에서 SmartArt 노드 계층 트리 텍스트를 추출한다."""
    root = pkg.read_xml(data_part)
    if root is None:
        return None

    pt_lst = root.find(qn(NS_DGM, "ptLst"))
    if pt_lst is None:
        return None

    node_texts: dict[str, str] = {}
    node_types: dict[str, str] = {}
    doc_roots: list[str] = []

    for pt in pt_lst.findall(qn(NS_DGM, "pt")):
        mid = pt.attrib.get("modelId", "")
        ptype = pt.attrib.get("type", "node")
        if not mid:
            continue
        node_types[mid] = ptype
        if ptype == "doc":
            doc_roots.append(mid)
            continue
        if ptype in ("pres", "parTrans", "sibTrans"):
            continue
        t_el = pt.find(qn(NS_DGM, "t"))
        paras = _extract_paragraphs_from_txbody(t_el)
        txt = " ".join(p.text for p in paras if p.text.strip()).strip()
        if txt:
            node_texts[mid] = txt

    if not node_texts:
        return None

    # 부모-자식 연결(cxnLst)로 계층 복원
    children_map: dict[str, list[tuple[int, str]]] = {}
    cxn_lst = root.find(qn(NS_DGM, "cxnLst"))
    if cxn_lst is not None:
        for cxn in cxn_lst.findall(qn(NS_DGM, "cxn")):
            ctype = cxn.attrib.get("type", "parOf")
            if ctype != "parOf":
                continue
            src_id = cxn.attrib.get("srcId", "")
            dst_id = cxn.attrib.get("destId", "")
            try:
                src_ord = int(cxn.attrib.get("srcOrd", "0"))
            except ValueError:
                src_ord = 0
            if src_id and dst_id:
                children_map.setdefault(src_id, []).append((src_ord, dst_id))

    ordered_items: list[tuple[int, str]] = []
    visited: set[str] = set()

    def _dfs(node_id: str, depth: int) -> None:
        if node_id in visited:
            return
        visited.add(node_id)
        if node_id in node_texts:
            ordered_items.append((max(0, depth), node_texts[node_id]))
            next_depth = depth + 1
        else:
            next_depth = depth
        for _, child_id in sorted(children_map.get(node_id, ()), key=lambda pair: pair[0]):
            _dfs(child_id, next_depth)

    if doc_roots:
        for root_id in doc_roots:
            _dfs(root_id, 0)
    # 연결 트리에서 누락된 텍스트 노드 보완
    for nid, txt in node_texts.items():
        if nid not in visited:
            ordered_items.append((0, txt))

    return RawPptxSmartArt(
        shape_id=shape_id,
        name=shape_name,
        data_part=data_part,
        items=tuple(ordered_items),
        rect_pt=rect_pt,
    )


def _is_hidden_cnvpr(c_nv_pr: ET.Element | None) -> bool:
    return c_nv_pr is not None and c_nv_pr.attrib.get("hidden") in ("1", "true", "True")


def _is_slide_num_only_txbody(tx_body: ET.Element | None) -> bool:
    """도형 본문이 오직 `<a:fld type="slidenum">` 슬라이드 번호 필드만 담고 있는지 판별한다."""
    if tx_body is None:
        return False
    has_sld_num_fld = False
    for p_el in tx_body.findall(qn(NS_A, "p")):
        for child in iter_children_resolving_mc(p_el):
            tag = local_name(child.tag)
            if tag == "fld":
                ftype = (child.attrib.get("type") or "").lower()
                if ftype == "slidenum":
                    has_sld_num_fld = True
                else:
                    t_el = child.find(qn(NS_A, "t"))
                    if t_el is not None and t_el.text and t_el.text.strip():
                        return False
            elif tag == "r":
                t_el = child.find(qn(NS_A, "t"))
                if t_el is not None and t_el.text and t_el.text.strip():
                    return False
            elif tag in ("m", "oMath", "oMathPara"):
                return False
    return has_sld_num_fld


def _extract_svg_text_hints(svg_bytes: bytes) -> tuple[str, ...]:
    """내장 SVG XML 안의 `<text>` 라벨을 안전하게 추출한다."""
    try:
        root = ET.fromstring(svg_bytes)
    except ET.ParseError:
        return ()
    hints: list[str] = []
    seen: set[str] = set()
    for el in root.iter():
        if local_name(el.tag) == "text":
            txt = "".join(el.itertext()).strip()
            if txt and txt not in seen:
                seen.add(txt)
                hints.append(txt[:120])
                if len(hints) >= 20:
                    break
    return tuple(hints)


def _extract_speaker_notes(
    pkg: PptxPackage,
    slide_rels: dict[str, Relationship],
    *,
    slide_number: int,
) -> str:
    notes_part = next(
        (
            r.target
            for r in slide_rels.values()
            if not r.is_external and r.rel_type == REL_TYPE_NOTES_SLIDE
        ),
        None,
    )
    if notes_part is None:
        return ""
    notes_root = pkg.read_xml(notes_part)
    if notes_root is None:
        return ""
    c_sld = notes_root.find(qn(NS_P, "cSld"))
    sp_tree = c_sld.find(qn(NS_P, "spTree")) if c_sld is not None else None
    if sp_tree is None:
        return ""

    skip_ph = {"sldImg", "sldNum", "hdr", "ftr", "dt"}
    note_lines: list[str] = []
    for elem in iter_children_resolving_mc(sp_tree):
        if local_name(elem.tag) != "sp":
            continue
        nv = elem.find(qn(NS_P, "nvSpPr"))
        c_nv_pr = nv.find(qn(NS_P, "cNvPr")) if nv is not None else None
        if _is_hidden_cnvpr(c_nv_pr):
            continue
        nv_pr = nv.find(qn(NS_P, "nvPr")) if nv is not None else None
        ph = nv_pr.find(qn(NS_P, "ph")) if nv_pr is not None else None
        ph_type = ph.attrib.get("type") if ph is not None else None
        if ph_type in skip_ph:
            continue
        tx_body = elem.find(qn(NS_P, "txBody"))
        if _is_slide_num_only_txbody(tx_body):
            continue
        paras = _extract_paragraphs_from_txbody(tx_body)
        for p in paras:
            if p.text.strip():
                note_lines.append(p.text.strip())

    joined = "\n".join(note_lines).strip()
    if joined == str(slide_number):
        return ""
    return joined


def _resolve_blip_embed_id(blip: ET.Element) -> str:
    embed_id = blip.attrib.get(qn(NS_R, "embed"), "")
    if embed_id:
        return embed_id
    # Office 2016+ 래스터 폴백 없는 SVG `<asvg:svgBlip r:embed="..."/>` 지원
    for sub in blip.iter():
        if sub.tag == qn(NS_ASVG, "svgBlip") or local_name(sub.tag) == "svgBlip":
            svg_id = sub.attrib.get(qn(NS_R, "embed"), "")
            if svg_id:
                return svg_id
    return ""


def _extract_picture_from_blip(
    pkg: PptxPackage,
    blip_fill: ET.Element | None,
    slide_rels: dict[str, Relationship],
    *,
    shape_id: str,
    name: str,
    alt_text: str,
    rect_pt: tuple[float, float, float, float],
) -> RawPptxImage | None:
    if blip_fill is None:
        return None
    blip = blip_fill.find(qn(NS_A, "blip"))
    if blip is None:
        return None
    embed_id = _resolve_blip_embed_id(blip)
    if not embed_id:
        return None
    rel = slide_rels.get(embed_id)
    if rel is None or rel.is_external:
        return None
    raw_bytes = pkg.read_bytes(rel.target)
    ext = posixpath.splitext(rel.target)[1].lower() or ".bin"
    px_w, px_h = probe_image_dimensions(raw_bytes, ext) if raw_bytes is not None else (None, None)
    svg_hints = _extract_svg_text_hints(raw_bytes) if (ext == ".svg" and raw_bytes) else ()
    return RawPptxImage(
        shape_id=shape_id,
        name=name,
        alt_text=alt_text,
        media_part=rel.target,
        extension=ext,
        rect_pt=rect_pt,
        raw_bytes=raw_bytes,
        pixel_width=px_w,
        pixel_height=px_h,
        svg_text_hints=svg_hints,
    )


def _extract_picture_from_part(
    pkg: PptxPackage,
    media_part: str,
    *,
    shape_id: str,
    name: str,
    alt_text: str,
    rect_pt: tuple[float, float, float, float],
) -> RawPptxImage | None:
    raw_bytes = pkg.read_bytes(media_part)
    if raw_bytes is None:
        return None
    ext = posixpath.splitext(media_part)[1].lower() or ".bin"
    px_w, px_h = probe_image_dimensions(raw_bytes, ext)
    svg_hints = _extract_svg_text_hints(raw_bytes) if ext == ".svg" else ()
    return RawPptxImage(
        shape_id=shape_id,
        name=name,
        alt_text=alt_text,
        media_part=media_part,
        extension=ext,
        rect_pt=rect_pt,
        raw_bytes=raw_bytes,
        pixel_width=px_w,
        pixel_height=px_h,
        svg_text_hints=svg_hints,
    )


def _extract_slide_comments(
    pkg: PptxPackage,
    slide_rels: dict[str, Relationship],
    comment_authors: dict[str, CommentAuthor],
) -> list[RawPptxComment]:
    """슬라이드의 구형(`comment*.xml`) 및 Office 365 현대 스레드 주석을 추출한다."""
    comments: list[RawPptxComment] = []

    def _resolve_author(author_id: str) -> tuple[str, str]:
        info = comment_authors.get(author_id)
        if info is not None:
            return info.name, info.initials
        return (f"Author {author_id}" if author_id else "Reviewer"), ""

    for rel in slide_rels.values():
        if rel.is_external or rel.rel_type not in (REL_TYPE_COMMENTS, REL_TYPE_MODERN_COMMENTS):
            continue
        root = pkg.read_xml(rel.target)
        if root is None:
            continue

        if rel.rel_type == REL_TYPE_COMMENTS:
            # 구형 주석(<p:cm>) + p15:parentCm 기반 스레드 정렬
            raw_items: list[tuple[tuple[str, str], tuple[str, str] | None, RawPptxComment]] = []
            for cm in root.findall(qn(NS_P, "cm")):
                aid = (cm.attrib.get("authorId") or "").strip()
                idx = (cm.attrib.get("idx") or "").strip()
                dt = (cm.attrib.get("dt") or "").strip()
                txt_el = cm.find(qn(NS_P, "text"))
                txt = (txt_el.text or "").strip() if txt_el is not None else ""
                if not txt:
                    continue
                parent_key: tuple[str, str] | None = None
                for parent_el in cm.iter(qn(NS_P15, "parentCm")):
                    p_aid = (parent_el.attrib.get("authorId") or "").strip()
                    p_idx = (parent_el.attrib.get("idx") or "").strip()
                    if p_aid or p_idx:
                        parent_key = (p_aid, p_idx)
                        break
                author_name, initials = _resolve_author(aid)
                raw_items.append(
                    (
                        (aid, idx),
                        parent_key,
                        RawPptxComment(
                            author=author_name,
                            initials=initials,
                            timestamp=dt,
                            text=txt,
                            is_reply=parent_key is not None,
                        ),
                    )
                )

            replies_by_parent: dict[tuple[str, str], list[RawPptxComment]] = {}
            roots_list: list[tuple[tuple[str, str], RawPptxComment]] = []
            known_keys = {k for k, _, _ in raw_items}
            for key, pkey, item in raw_items:
                if pkey is not None and pkey in known_keys:
                    replies_by_parent.setdefault(pkey, []).append(item)
                else:
                    roots_list.append((key, item))
            for key, root_item in roots_list:
                comments.append(root_item)
                comments.extend(replies_by_parent.get(key, ()))
        else:
            # Office 365 현대 스레드 주석(<p188:cm> / <p188:replyLst>)
            for cm in root:
                if local_name(cm.tag) != "cm":
                    continue
                aid = (cm.attrib.get("authorId") or "").strip()
                created = (cm.attrib.get("created") or "").strip()
                is_reply = bool((cm.attrib.get("parentCmId") or "").strip())
                tx_body = next((c for c in cm if local_name(c.tag) == "txBody"), None)
                paras = _extract_paragraphs_from_txbody(tx_body)
                txt = "\n".join(p.text for p in paras if p.text.strip()).strip()
                if txt:
                    author_name, initials = _resolve_author(aid)
                    comments.append(
                        RawPptxComment(
                            author=author_name,
                            initials=initials,
                            timestamp=created,
                            text=txt,
                            is_reply=is_reply,
                        )
                    )
                # 중첩 replyLst 지원
                for child in cm:
                    if local_name(child.tag) == "replyLst":
                        for rep in child:
                            if local_name(rep.tag) != "reply":
                                continue
                            r_aid = (rep.attrib.get("authorId") or "").strip()
                            r_dt = (rep.attrib.get("created") or "").strip()
                            r_tx = next((c for c in rep if local_name(c.tag) == "txBody"), None)
                            r_paras = _extract_paragraphs_from_txbody(r_tx)
                            r_txt = "\n".join(p.text for p in r_paras if p.text.strip()).strip()
                            if r_txt:
                                r_name, r_init = _resolve_author(r_aid)
                                comments.append(
                                    RawPptxComment(
                                        author=r_name,
                                        initials=r_init,
                                        timestamp=r_dt,
                                        text=r_txt,
                                        is_reply=True,
                                    )
                                )
    return comments


def _walk_sp_tree(
    sp_tree: ET.Element,
    *,
    pkg: PptxPackage,
    slide_data: RawSlideData,
    slide_rels: dict[str, Relationship],
    vml_images: dict[str, str],
    placeholders: PlaceholderLookup,
    transform: GroupTransform,
    in_group: bool,
) -> None:
    for elem in iter_children_resolving_mc(sp_tree):
        tag = local_name(elem.tag)

        if tag == "grpSp":
            nv_grp = elem.find(qn(NS_P, "nvGrpSpPr"))
            grp_cnv = nv_grp.find(qn(NS_P, "cNvPr")) if nv_grp is not None else None
            if _is_hidden_cnvpr(grp_cnv):
                continue
            grp_pr = elem.find(qn(NS_P, "grpSpPr"))
            xfrm = grp_pr.find(qn(NS_A, "xfrm")) if grp_pr is not None else None
            next_tf = transform
            if xfrm is not None:
                off = xfrm.find(qn(NS_A, "off"))
                ext = xfrm.find(qn(NS_A, "ext"))
                ch_off = xfrm.find(qn(NS_A, "chOff"))
                ch_ext = xfrm.find(qn(NS_A, "chExt"))
                if (
                    off is not None
                    and ext is not None
                    and ch_off is not None
                    and ch_ext is not None
                ):
                    next_tf = transform.compose_group(
                        off_x=emu_to_pt(off.attrib.get("x")),
                        off_y=emu_to_pt(off.attrib.get("y")),
                        ext_w=emu_to_pt(ext.attrib.get("cx")),
                        ext_h=emu_to_pt(ext.attrib.get("cy")),
                        ch_off_x=emu_to_pt(ch_off.attrib.get("x")),
                        ch_off_y=emu_to_pt(ch_off.attrib.get("y")),
                        ch_ext_w=emu_to_pt(ch_ext.attrib.get("cx")),
                        ch_ext_h=emu_to_pt(ch_ext.attrib.get("cy")),
                    )
            _walk_sp_tree(
                elem,
                pkg=pkg,
                slide_data=slide_data,
                slide_rels=slide_rels,
                vml_images=vml_images,
                placeholders=placeholders,
                transform=next_tf,
                in_group=True,
            )
            continue

        if tag == "sp":
            nv = elem.find(qn(NS_P, "nvSpPr"))
            c_nv_pr = nv.find(qn(NS_P, "cNvPr")) if nv is not None else None
            if _is_hidden_cnvpr(c_nv_pr):
                continue
            c_nv_sp = nv.find(qn(NS_P, "cNvSpPr")) if nv is not None else None
            nv_pr = nv.find(qn(NS_P, "nvPr")) if nv is not None else None
            ph = nv_pr.find(qn(NS_P, "ph")) if nv_pr is not None else None

            shape_id = c_nv_pr.attrib.get("id", "") if c_nv_pr is not None else ""
            name = c_nv_pr.attrib.get("name", "") if c_nv_pr is not None else ""
            alt_text = (
                (c_nv_pr.attrib.get("descr") or c_nv_pr.attrib.get("title") or "").strip()
                if c_nv_pr is not None
                else ""
            )
            is_tx_box = c_nv_sp is not None and c_nv_sp.attrib.get("txBox") in ("1", "true", "True")
            ph_type = ph.attrib.get("type") if ph is not None else None
            ph_idx = ph.attrib.get("idx") if ph is not None else None

            sp_pr = elem.find(qn(NS_P, "spPr"))
            rect = _extract_rect_from_xfrm(
                sp_pr.find(qn(NS_A, "xfrm")) if sp_pr is not None else None,
                transform,
            )
            inherited_rect, inherited_font = placeholders.resolve(ph_type, ph_idx)
            if rect is None:
                rect = inherited_rect
            if rect is None:
                rect = (0.0, 0.0, slide_data.width_pt, slide_data.height_pt)

            # 도형 안에 사진 채우기(blipFill)가 들어 있는 경우 이미지로도 수집
            if sp_pr is not None and sp_pr.find(qn(NS_A, "blipFill")) is not None:
                pic_img = _extract_picture_from_blip(
                    pkg,
                    sp_pr.find(qn(NS_A, "blipFill")),
                    slide_rels,
                    shape_id=shape_id,
                    name=name,
                    alt_text=alt_text,
                    rect_pt=rect,
                )
                if pic_img is not None:
                    slide_data.images.append(pic_img)

            prst_geom: str | None = None
            has_fill = False
            has_stroke = False
            if sp_pr is not None:
                prst_el = sp_pr.find(qn(NS_A, "prstGeom"))
                if prst_el is not None:
                    prst_geom = prst_el.attrib.get("prst")
                has_fill = any(
                    sp_pr.find(qn(NS_A, ft)) is not None
                    for ft in ("solidFill", "gradFill", "pattFill")
                )
                ln_el = sp_pr.find(qn(NS_A, "ln"))
                if ln_el is not None and ln_el.find(qn(NS_A, "noFill")) is None:
                    has_stroke = True

            tx_body = elem.find(qn(NS_P, "txBody"))
            paragraphs = _extract_paragraphs_from_txbody(
                tx_body,
                default_font_size=inherited_font,
            )
            slide_data.shapes.append(
                RawPptxShape(
                    shape_id=shape_id,
                    name=name,
                    ph_type=ph_type,
                    ph_idx=ph_idx,
                    rect_pt=rect,
                    paragraphs=paragraphs,
                    prst_geom=prst_geom,
                    has_fill=has_fill,
                    has_stroke=has_stroke,
                    is_text_box=is_tx_box,
                    in_group=in_group,
                    is_slide_num_field=_is_slide_num_only_txbody(tx_body),
                )
            )
            continue

        if tag == "cxnSp":
            nv = elem.find(qn(NS_P, "nvCxnSpPr"))
            c_nv_pr = nv.find(qn(NS_P, "cNvPr")) if nv is not None else None
            if _is_hidden_cnvpr(c_nv_pr):
                continue
            c_nv_cxn = nv.find(qn(NS_P, "cNvCxnSpPr")) if nv is not None else None
            shape_id = c_nv_pr.attrib.get("id", "") if c_nv_pr is not None else ""
            st_id: str | None = None
            end_id: str | None = None
            if c_nv_cxn is not None:
                st_el = c_nv_cxn.find(qn(NS_A, "stCxn"))
                end_el = c_nv_cxn.find(qn(NS_A, "endCxn"))
                st_id = st_el.attrib.get("id") if st_el is not None else None
                end_id = end_el.attrib.get("id") if end_el is not None else None

            sp_pr = elem.find(qn(NS_P, "spPr"))
            rect = _extract_rect_from_xfrm(
                sp_pr.find(qn(NS_A, "xfrm")) if sp_pr is not None else None,
                transform,
            ) or (0.0, 0.0, 0.0, 0.0)
            has_arrow = False
            if sp_pr is not None:
                ln_el = sp_pr.find(qn(NS_A, "ln"))
                if ln_el is not None:
                    for end_tag in ("headEnd", "tailEnd"):
                        end_node = ln_el.find(qn(NS_A, end_tag))
                        if end_node is not None and end_node.attrib.get("type", "none") != "none":
                            has_arrow = True
            slide_data.connectors.append(
                RawPptxConnector(
                    shape_id=shape_id,
                    rect_pt=rect,
                    start_shape_id=st_id,
                    end_shape_id=end_id,
                    has_arrow=has_arrow,
                )
            )
            continue

        if tag == "pic":
            nv = elem.find(qn(NS_P, "nvPicPr"))
            c_nv_pr = nv.find(qn(NS_P, "cNvPr")) if nv is not None else None
            if _is_hidden_cnvpr(c_nv_pr):
                continue
            shape_id = c_nv_pr.attrib.get("id", "") if c_nv_pr is not None else ""
            name = c_nv_pr.attrib.get("name", "") if c_nv_pr is not None else ""
            alt_text = (
                (c_nv_pr.attrib.get("descr") or c_nv_pr.attrib.get("title") or "").strip()
                if c_nv_pr is not None
                else ""
            )
            sp_pr = elem.find(qn(NS_P, "spPr"))
            rect = _extract_rect_from_xfrm(
                sp_pr.find(qn(NS_A, "xfrm")) if sp_pr is not None else None,
                transform,
            ) or (0.0, 0.0, slide_data.width_pt, slide_data.height_pt)
            pic_img = _extract_picture_from_blip(
                pkg,
                elem.find(qn(NS_P, "blipFill")),
                slide_rels,
                shape_id=shape_id,
                name=name,
                alt_text=alt_text,
                rect_pt=rect,
            )
            if pic_img is not None:
                slide_data.images.append(pic_img)
            continue

        if tag == "graphicFrame":
            nv = elem.find(qn(NS_P, "nvGraphicFramePr"))
            c_nv_pr = nv.find(qn(NS_P, "cNvPr")) if nv is not None else None
            if _is_hidden_cnvpr(c_nv_pr):
                continue
            shape_id = c_nv_pr.attrib.get("id", "") if c_nv_pr is not None else ""
            name = c_nv_pr.attrib.get("name", "") if c_nv_pr is not None else ""
            rect = _extract_rect_from_xfrm(
                elem.find(qn(NS_P, "xfrm")),
                transform,
            ) or (0.0, 0.0, slide_data.width_pt, slide_data.height_pt)

            graphic = elem.find(qn(NS_A, "graphic"))
            g_data = graphic.find(qn(NS_A, "graphicData")) if graphic is not None else None
            if g_data is None:
                continue

            # 1) 네이티브 표(<a:tbl>)
            tbl_el = g_data.find(qn(NS_A, "tbl"))
            if tbl_el is not None:
                extracted_tbl = _extract_native_table(
                    tbl_el,
                    slide_number=slide_data.slide_number,
                    table_index=len(slide_data.tables) + 1,
                    rect_pt=rect,
                    slide_w=slide_data.width_pt,
                    slide_h=slide_data.height_pt,
                    shape_name=name or f"Table {len(slide_data.tables) + 1}",
                )
                if extracted_tbl is not None:
                    slide_data.tables.append(
                        RawPptxTable(
                            shape_id=shape_id,
                            name=name,
                            rect_pt=rect,
                            table=extracted_tbl,
                        )
                    )
                continue

            # 2) 네이티브 차트(<c:chart>) 및 Office 2016+ 확장 차트(<cx:chart>)
            chart_ref = g_data.find(qn(NS_C, "chart"))
            if chart_ref is not None:
                r_id = chart_ref.attrib.get(qn(NS_R, "id"), "")
                rel = slide_rels.get(r_id)
                if rel is not None and not rel.is_external:
                    chart = _extract_native_chart(
                        pkg,
                        rel.target,
                        shape_id=shape_id,
                        shape_name=name or f"Chart {len(slide_data.charts) + 1}",
                        rect_pt=rect,
                    )
                    if chart is not None:
                        slide_data.charts.append(chart)
                continue

            chartex_ref = g_data.find(qn(NS_CX, "chart"))
            if chartex_ref is not None:
                r_id = chartex_ref.attrib.get(qn(NS_R, "id"), "")
                rel = slide_rels.get(r_id)
                if rel is not None and not rel.is_external:
                    chartex = _extract_chartex(
                        pkg,
                        rel.target,
                        shape_id=shape_id,
                        shape_name=name or f"ChartEx {len(slide_data.charts) + 1}",
                        rect_pt=rect,
                    )
                    if chartex is not None:
                        slide_data.charts.append(chartex)
                continue

            # 3) SmartArt 다이어그램(<dgm:relIds r:dm="..."/>)
            dgm_ref = g_data.find(qn(NS_DGM, "relIds"))
            if dgm_ref is not None:
                dm_id = dgm_ref.attrib.get(qn(NS_R, "dm"), "")
                rel = slide_rels.get(dm_id)
                if rel is not None and not rel.is_external:
                    smartart = _extract_smartart_diagram(
                        pkg,
                        rel.target,
                        shape_id=shape_id,
                        shape_name=name or f"SmartArt {len(slide_data.smartarts) + 1}",
                        rect_pt=rect,
                    )
                    if smartart is not None:
                        slide_data.smartarts.append(smartart)
                continue

            # 4) 내장 OLE(<p:oleObj>): .xlsx 표 직접 추출 → <p:pic>/VML 미리보기 → OLE 보존
            ole_el: ET.Element | None = None
            for sub in g_data.iter():
                if local_name(sub.tag) == "oleObj":
                    ole_el = sub
                    break
            if ole_el is not None:
                ole_name = (ole_el.attrib.get("name") or name or "OLE Object").strip()
                prog_id = (ole_el.attrib.get("progId") or "").strip()
                ole_rid = (ole_el.attrib.get(qn(NS_R, "id")) or "").strip()
                ole_spid = (ole_el.attrib.get("spid") or "").strip()

                # 4-a) 내장 Excel(.xlsx) 워크북인 경우 표 데이터를 직접 추출
                if ole_rid:
                    ole_rel = slide_rels.get(ole_rid)
                    if ole_rel is not None and not ole_rel.is_external:
                        ole_bytes = pkg.read_bytes(ole_rel.target)
                        if ole_bytes is not None:
                            xlsx_tbl = _extract_embedded_xlsx_table(
                                ole_bytes,
                                slide_number=slide_data.slide_number,
                                table_index=len(slide_data.tables) + 1,
                                rect_pt=rect,
                                slide_w=slide_data.width_pt,
                                slide_h=slide_data.height_pt,
                                ole_name=ole_name,
                            )
                            if xlsx_tbl is not None:
                                slide_data.tables.append(
                                    RawPptxTable(
                                        shape_id=shape_id,
                                        name=ole_name,
                                        rect_pt=rect,
                                        table=xlsx_tbl,
                                    )
                                )
                                continue

                # 4-b) 미리보기 이미지(<p:pic> 또는 VML vmlDrawing*.vml의 spid 매핑) 추출
                preview_pic: ET.Element | None = None
                for sub in g_data.iter():
                    if local_name(sub.tag) == "pic":
                        preview_pic = sub
                        break
                extracted_preview = (
                    _extract_picture_from_blip(
                        pkg,
                        preview_pic.find(qn(NS_P, "blipFill")),
                        slide_rels,
                        shape_id=shape_id,
                        name=ole_name,
                        alt_text=prog_id,
                        rect_pt=rect,
                    )
                    if preview_pic is not None
                    else None
                )
                if extracted_preview is None and ole_spid and ole_spid in vml_images:
                    extracted_preview = _extract_picture_from_part(
                        pkg,
                        vml_images[ole_spid],
                        shape_id=shape_id,
                        name=ole_name,
                        alt_text=prog_id,
                        rect_pt=rect,
                    )

                slide_data.ole_objects.append(
                    RawPptxOleObject(
                        shape_id=shape_id,
                        name=ole_name,
                        prog_id=prog_id,
                        rect_pt=rect,
                        preview_image=extracted_preview,
                    )
                )
                continue


def extract_slide_data(
    pkg: PptxPackage,
    *,
    slide_number: int,
    slide_id: str,
    part_path: str,
    width_pt: float,
    height_pt: float,
    comment_authors: dict[str, CommentAuthor] | None = None,
) -> RawSlideData:
    """단일 슬라이드 파트(`ppt/slides/slideN.xml`)의 모든 구조 요소를 추출한다."""
    slide_data = RawSlideData(
        slide_number=slide_number,
        slide_id=slide_id,
        part_path=part_path,
        width_pt=width_pt,
        height_pt=height_pt,
    )
    root = pkg.read_xml(part_path)
    if root is None:
        slide_data.warnings.append(f"슬라이드 XML({part_path})을 파싱할 수 없다.")
        return slide_data

    if root.attrib.get("show") in ("0", "false", "False"):
        slide_data.is_hidden = True
        slide_data.warnings.append("숨김 처리된 슬라이드(show='0')다.")

    slide_rels = pkg.read_rels(part_path)
    vml_images = pkg.read_vml_image_targets(slide_rels)
    placeholders = pkg.resolve_placeholder_lookup(part_path)

    c_sld = root.find(qn(NS_P, "cSld"))
    sp_tree = c_sld.find(qn(NS_P, "spTree")) if c_sld is not None else None
    if sp_tree is not None:
        _walk_sp_tree(
            sp_tree,
            pkg=pkg,
            slide_data=slide_data,
            slide_rels=slide_rels,
            vml_images=vml_images,
            placeholders=placeholders,
            transform=GroupTransform(),
            in_group=False,
        )

    slide_data.speaker_notes = _extract_speaker_notes(
        pkg,
        slide_rels,
        slide_number=slide_number,
    )
    slide_data.comments = _extract_slide_comments(
        pkg,
        slide_rels,
        comment_authors or {},
    )
    return slide_data


def is_flowchart_preset(prst_geom: str | None) -> bool:
    if not prst_geom:
        return False
    return prst_geom.startswith(_FLOWCHART_PRST_PREFIXES)
