"""OOXML SpreadsheetML(`.xlsx`) 시트 스트리밍 추출기 — 셀·병합표·다중헤더·주석·차트·이미지."""

from __future__ import annotations

import io
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from autoetl.parser.contracts import (
    ExtractedTable,
    ParseStrategy,
    TableCell,
)
from autoetl.parser.docx.elements import (
    RawDocxBodyItem,
    RawDocxPage,
    RawDocxParagraph,
    RawDocxTableItem,
)
from autoetl.parser.docx.ooxml import DocxCommentEntry
from autoetl.parser.pptx.elements import (
    RawPptxChart,
    RawPptxImage,
    _extract_chartex,
    _extract_native_chart,
)
from autoetl.parser.pptx.ooxml import (
    MAX_MEDIA_BYTES,
    PptxPackage,
    local_name,
    make_bbox,
    probe_image_dimensions,
)
from autoetl.parser.xlsx.ooxml import (
    XlsxCoreMetadata,
    XlsxPackage,
    XlsxSheetRef,
    XlsxStyleCatalog,
    _extract_si_text,
    format_excel_number,
    parse_cell_range,
    parse_cell_ref,
)

_DEFAULT_SHEET_W = 841.88  # A4 가로 기준 (스프레드시트 표 가독성)
_DEFAULT_SHEET_H = 595.28
_MAX_SHEET_ROWS = 5000
_MAX_SHEET_COLS = 128
_MAX_CELLS_PER_SHEET = 200_000

_EXCEL_HF_CODE_RE = re.compile(r'&(?:"[^"]*"|[LCRBUIESKPAFDTZN&\d+-])')


class _XlsxChartPkgAdapter:
    """`pptx.elements._extract_native_chart` / `_extract_chartex`와 호환되는 경량 래퍼."""

    def __init__(self, pkg: XlsxPackage) -> None:
        self._pkg = pkg

    def read_xml(self, part_path: str) -> ET.Element | None:
        return self._pkg.read_xml(part_path)


@dataclass
class _CellValue:
    row: int
    col: int
    text: str
    has_border: bool = False
    is_bold: bool = False
    font_size_pt: float | None = None


def _clean_excel_hf_text(raw: str) -> str:
    cleaned = _EXCEL_HF_CODE_RE.sub(" ", raw)
    return " ".join(cleaned.split()).strip()


def _extract_sheet_comments(pkg: XlsxPackage, sheet_part: str) -> list[DocxCommentEntry]:
    """시트 관계에서 `xl/comments*.xml`을 찾아 셀 주석 목록을 추출한다."""
    rels = pkg.read_rels(sheet_part)
    comments: list[DocxCommentEntry] = []
    for rel in rels.values():
        if not rel.rel_type.endswith("/comments"):
            continue
        root = pkg.read_xml(rel.target)
        if root is None:
            continue
        authors: list[str] = []
        for el in root:
            if local_name(el.tag) == "authors":
                for a in el:
                    if local_name(a.tag) == "author":
                        authors.append((a.text or "").strip())
        for el in root:
            if local_name(el.tag) != "commentList":
                continue
            for cm in el:
                if local_name(cm.tag) != "comment":
                    continue
                ref = cm.attrib.get("ref", "").strip()
                a_idx_str = cm.attrib.get("authorId", "0")
                author = ""
                if a_idx_str.isdigit():
                    a_idx = int(a_idx_str)
                    if 0 <= a_idx < len(authors):
                        author = authors[a_idx]
                txt = ""
                for sub in cm:
                    if local_name(sub.tag) == "text":
                        txt = _extract_si_text(sub).strip()
                        break
                if txt:
                    cid = str(len(comments) + 1)
                    prefix = f"[{ref}] " if ref else ""
                    comments.append(
                        DocxCommentEntry(
                            comment_id=cid,
                            author=author or "Excel 주석",
                            initials="",
                            date="",
                            text=f"{prefix}{txt}",
                        )
                    )
    return comments


def _extract_sheet_drawings(
    pkg: XlsxPackage,
    sheet_part: str,
    seq_counter: list[int],
) -> list[RawDocxBodyItem]:
    """시트의 `xl/drawings/drawing*.xml`에서 내장 차트(`c:chart`/`cx:chart`)와 이미지를 추출한다."""
    rels = pkg.read_rels(sheet_part)
    items: list[RawDocxBodyItem] = []
    chart_pkg = cast(PptxPackage, _XlsxChartPkgAdapter(pkg))

    for rel in rels.values():
        if not rel.rel_type.endswith("/drawing"):
            continue
        drawing_part = rel.target
        root = pkg.read_xml(drawing_part)
        if root is None:
            continue
        d_rels = pkg.read_rels(drawing_part)

        for el in root.iter():
            tag = local_name(el.tag)
            if tag == "pic":
                name = ""
                alt_text = ""
                embed_rid = ""
                for sub in el.iter():
                    stag = local_name(sub.tag)
                    if stag == "cNvPr":
                        name = sub.attrib.get("name", "")
                        alt_text = sub.attrib.get("descr", "") or sub.attrib.get("title", "")
                    elif stag == "blip":
                        for k, v in sub.attrib.items():
                            if local_name(k) == "embed":
                                embed_rid = v
                                break
                if not embed_rid or embed_rid not in d_rels:
                    continue
                media_part = d_rels[embed_rid].target
                raw_bytes = pkg.read_bytes(media_part, max_bytes=MAX_MEDIA_BYTES)
                if not raw_bytes:
                    continue
                ext = Path(media_part).suffix.lower() or ".bin"
                px_w, px_h = probe_image_dimensions(raw_bytes, ext)
                w_pt = min(_DEFAULT_SHEET_W * 0.72, float(px_w or 260) * 0.75)
                h_pt = min(_DEFAULT_SHEET_H * 0.65, float(px_h or 180) * 0.75)
                seq_counter[0] += 1
                items.append(
                    RawPptxImage(
                        shape_id=f"xlsx_img_{seq_counter[0]}",
                        name=name or Path(media_part).name,
                        alt_text=alt_text,
                        media_part=media_part,
                        extension=ext,
                        rect_pt=(36.0, 48.0, 36.0 + max(64.0, w_pt), 48.0 + max(48.0, h_pt)),
                        raw_bytes=raw_bytes,
                        pixel_width=px_w,
                        pixel_height=px_h,
                    )
                )
            elif tag == "chart":
                chart_rid = ""
                for k, v in el.attrib.items():
                    if local_name(k) == "id":
                        chart_rid = v
                        break
                if not chart_rid or chart_rid not in d_rels:
                    continue
                chart_part = d_rels[chart_rid].target
                seq_counter[0] += 1
                rect = (36.0, 48.0, _DEFAULT_SHEET_W * 0.72, _DEFAULT_SHEET_H * 0.60)
                chart: RawPptxChart | None = _extract_native_chart(
                    chart_pkg,
                    chart_part,
                    shape_id=f"xlsx_chart_{seq_counter[0]}",
                    shape_name=f"Excel Chart {seq_counter[0]}",
                    rect_pt=rect,
                )
                if chart is None:
                    chart = _extract_chartex(
                        chart_pkg,
                        chart_part,
                        shape_id=f"xlsx_chart_{seq_counter[0]}",
                        shape_name=f"Excel Chart {seq_counter[0]}",
                        rect_pt=rect,
                    )
                if chart is not None:
                    items.append(chart)
    return items


def _stream_parse_sheet_xml(
    raw_xml: bytes,
    shared_strings: list[str],
    styles: XlsxStyleCatalog,
) -> tuple[
    dict[tuple[int, int], _CellValue],
    list[tuple[int, int, int, int]],
    list[str],
    list[str],
    bool,
]:
    """시트 XML을 `iterparse`로 스트리밍 파싱해 셀·병합·머리글/바닥글·절단 여부를 반환한다."""
    cells: dict[tuple[int, int], _CellValue] = {}
    merges: list[tuple[int, int, int, int]] = []
    headers: list[str] = []
    footers: list[str] = []
    truncated = False

    cur_row_idx = -1
    cur_col_auto = 0

    try:
        context = ET.iterparse(io.BytesIO(raw_xml), events=("start", "end"))
        for event, elem in context:
            tag = local_name(elem.tag)
            if event == "start":
                if tag == "row":
                    r_attr = elem.attrib.get("r", "")
                    if r_attr.isdigit():
                        cur_row_idx = int(r_attr) - 1
                    else:
                        cur_row_idx += 1
                    cur_col_auto = 0
                continue

            # event == "end"
            if tag == "c":
                ref_attr = elem.attrib.get("r", "")
                rc = parse_cell_ref(ref_attr) if ref_attr else None
                if rc is not None:
                    r_idx, c_idx = rc
                    cur_col_auto = c_idx + 1
                else:
                    r_idx = max(0, cur_row_idx)
                    c_idx = cur_col_auto
                    cur_col_auto += 1

                if (
                    r_idx >= _MAX_SHEET_ROWS
                    or c_idx >= _MAX_SHEET_COLS
                    or len(cells) >= _MAX_CELLS_PER_SHEET
                ):
                    truncated = True
                    elem.clear()
                    continue

                c_type = elem.attrib.get("t", "")
                s_attr = elem.attrib.get("s", "")
                style_idx = int(s_attr) if s_attr.isdigit() else None
                style = styles.get_style(style_idx)

                val_text = ""
                if c_type == "inlineStr":
                    for sub in elem:
                        if local_name(sub.tag) == "is":
                            val_text = _extract_si_text(sub)
                            break
                else:
                    raw_v = ""
                    for sub in elem:
                        if local_name(sub.tag) == "v" and sub.text is not None:
                            raw_v = sub.text
                            break
                    if c_type == "s":
                        if raw_v.strip().isdigit():
                            si = int(raw_v.strip())
                            if 0 <= si < len(shared_strings):
                                val_text = shared_strings[si]
                    elif c_type == "b":
                        val_text = "TRUE" if raw_v.strip() in ("1", "true", "TRUE") else "FALSE"
                    elif c_type in ("str", "e"):
                        val_text = raw_v
                    elif raw_v:
                        val_text = format_excel_number(
                            raw_v,
                            style,
                            date1904=styles.date1904,
                        )

                cleaned_val = val_text.replace("\r\n", "\n").replace("\r", "\n").strip()
                if cleaned_val:
                    cells[(r_idx, c_idx)] = _CellValue(
                        row=r_idx,
                        col=c_idx,
                        text=cleaned_val,
                        has_border=bool(style and style.has_border),
                        is_bold=bool(style and style.is_bold),
                        font_size_pt=style.font_size_pt if style else None,
                    )
                elem.clear()
            elif tag == "row":
                elem.clear()
            elif tag == "mergeCell":
                m_ref = elem.attrib.get("ref", "")
                parsed_range = parse_cell_range(m_ref)
                if parsed_range is not None:
                    r0, c0, r1, c1 = parsed_range
                    if r0 < _MAX_SHEET_ROWS and c0 < _MAX_SHEET_COLS:
                        merges.append(
                            (
                                r0,
                                c0,
                                min(r1, _MAX_SHEET_ROWS - 1),
                                min(c1, _MAX_SHEET_COLS - 1),
                            )
                        )
                elem.clear()
            elif tag in ("oddHeader", "firstHeader", "evenHeader"):
                if elem.text:
                    ht = _clean_excel_hf_text(elem.text)
                    if ht and ht not in headers:
                        headers.append(ht)
                elem.clear()
            elif tag in ("oddFooter", "firstFooter", "evenFooter"):
                if elem.text:
                    ft = _clean_excel_hf_text(elem.text)
                    if ft and ft not in footers:
                        footers.append(ft)
                elem.clear()
    except ET.ParseError:
        pass

    return cells, merges, headers, footers, truncated


def _build_band_table(
    band_rows: list[int],
    cells: dict[tuple[int, int], _CellValue],
    merges: list[tuple[int, int, int, int]],
    *,
    sheet_index: int,
    seq_counter: list[int],
    caption: str | None = None,
) -> RawDocxTableItem | None:
    """연속 비어 있지 않은 행 구간을 `RawDocxTableItem`으로 변환한다(병합 셀·다중 행 헤더 지원)."""
    row_min = band_rows[0]
    row_max = band_rows[-1]
    band_cells = [cv for (r, _c), cv in cells.items() if row_min <= r <= row_max]
    if not band_cells:
        return None

    col_min = min(cv.col for cv in band_cells)
    col_max = max(cv.col for cv in band_cells)
    # 병합 범위가 band 내부에서 열을 확장하는 경우 반영
    band_merges: list[tuple[int, int, int, int]] = []
    for r0, c0, r1, c1 in merges:
        if r0 >= row_min and r1 <= row_max and c0 <= col_max and c1 >= col_min:
            band_merges.append((r0, c0, r1, c1))
            col_min = min(col_min, c0)
            col_max = max(col_max, c1)

    n_rows = row_max - row_min + 1
    n_cols = min(_MAX_SHEET_COLS, col_max - col_min + 1)
    if n_rows <= 0 or n_cols <= 0:
        return None

    # 병합 맵 구성: anchor -> (r_span, c_span), covered set
    merge_anchor: dict[tuple[int, int], tuple[int, int]] = {}
    merge_covered: set[tuple[int, int]] = set()
    has_merged = False
    for r0, c0, r1, c1 in band_merges:
        lr0 = r0 - row_min
        lc0 = c0 - col_min
        lr1 = min(n_rows - 1, r1 - row_min)
        lc1 = min(n_cols - 1, c1 - col_min)
        if lr0 < 0 or lc0 < 0 or lr0 >= n_rows or lc0 >= n_cols:
            continue
        r_span = lr1 - lr0 + 1
        c_span = lc1 - lc0 + 1
        if r_span > 1 or c_span > 1:
            has_merged = True
            merge_anchor[(lr0, lc0)] = (r_span, c_span)
            for rr in range(lr0, lr0 + r_span):
                for cc in range(lc0, lc0 + c_span):
                    if (rr, cc) != (lr0, lc0):
                        merge_covered.add((rr, cc))

    grid: list[list[str]] = [["" for _ in range(n_cols)] for _ in range(n_rows)]
    border_hits = 0
    for cv in band_cells:
        lr = cv.row - row_min
        lc = cv.col - col_min
        if 0 <= lr < n_rows and 0 <= lc < n_cols:
            grid[lr][lc] = cv.text
            if cv.has_border:
                border_hits += 1

    # 100% 빈 내부 열 압축 (병합 없는 경우만 안전하게 압축)
    if not has_merged:
        non_empty_cols = [
            c for c in range(n_cols) if any(grid[r][c].strip() for r in range(n_rows))
        ]
        if 0 < len(non_empty_cols) < n_cols:
            grid = [[row[c] for c in non_empty_cols] for row in grid]
            n_cols = len(non_empty_cols)

    # 다중 행 병합 헤더 감지 (예: 0행에 row_span=2~3 세로 병합 또는 col_span>=2 가로 병합)
    header_row_count = 1
    if has_merged and n_rows >= 3:
        top_max_rspan = max(
            (r_span for (lr, _lc), (r_span, _cs) in merge_anchor.items() if lr == 0),
            default=1,
        )
        top_has_hmerge = any(
            c_span >= 2 for (lr, _lc), (_rs, c_span) in merge_anchor.items() if lr == 0
        )
        if 2 <= top_max_rspan <= 3 and n_rows > top_max_rspan:
            header_row_count = top_max_rspan
        elif top_has_hmerge and n_rows >= 3:
            # 1행이 모두 짧은 텍스트 헤더이고 숫자가 거의 없으면 2행 헤더로 승격
            row1_vals = [v.strip() for v in grid[1] if v.strip()]
            if row1_vals and not any(
                v.replace(",", "").replace(".", "").isdigit() for v in row1_vals
            ):
                header_row_count = 2

    # 헤더 행 병합 라벨 전파로 깔끔한 GFM/CSV 헤더 생성
    if header_row_count == 1:
        headers = [grid[0][c] if grid[0][c] else f"col_{c + 1}" for c in range(n_cols)]
        data_rows = grid[1:] if n_rows >= 2 else []
    else:
        hdr_matrix = [list(grid[r]) for r in range(header_row_count)]
        for (lr, lc), (r_span, c_span) in merge_anchor.items():
            if lr < header_row_count:
                lbl = grid[lr][lc]
                if lbl:
                    for rr in range(lr, min(header_row_count, lr + r_span)):
                        for cc in range(lc, min(n_cols, lc + c_span)):
                            if not hdr_matrix[rr][cc]:
                                hdr_matrix[rr][cc] = lbl
        headers = []
        for c in range(n_cols):
            seen_parts: list[str] = []
            for r in range(header_row_count):
                part = hdr_matrix[r][c].strip()
                if part and part not in seen_parts:
                    seen_parts.append(part)
            headers.append(" / ".join(seen_parts) if seen_parts else f"col_{c + 1}")
        data_rows = grid[header_row_count:]

    table_cells: list[TableCell] = []
    for r in range(n_rows):
        for c in range(n_cols):
            if (r, c) in merge_covered:
                continue
            r_span, c_span = merge_anchor.get((r, c), (1, 1))
            table_cells.append(
                TableCell(
                    row=r,
                    col=c,
                    row_span=r_span,
                    col_span=c_span,
                    text=grid[r][c],
                    is_header=(r < header_row_count),
                )
            )

    strategy = (
        ParseStrategy.RULED_TABLE
        if border_hits >= max(1, int(len(band_cells) * 0.3))
        else ParseStrategy.BORDERLESS_TABLE
    )
    strat_label = "격자선 표" if strategy == ParseStrategy.RULED_TABLE else "무선 시트 표"
    merged_note = ", 병합 셀 포함" if has_merged else ""

    raw_w = min(_DEFAULT_SHEET_W * 0.92, max(160.0, n_cols * 88.0))
    raw_h = min(_DEFAULT_SHEET_H * 0.80, max(36.0, n_rows * 20.0))
    seq_counter[0] += 1
    extracted = ExtractedTable(
        table_id=f"p{sheet_index:03d}_t{seq_counter[0]:02d}",
        page=sheet_index,
        bbox=make_bbox(0.0, 0.0, raw_w, raw_h, _DEFAULT_SHEET_W, _DEFAULT_SHEET_H),
        strategy=strategy,
        confidence=0.98,
        row_count=n_rows,
        col_count=n_cols,
        header_row_count=header_row_count,
        headers=headers,
        rows=data_rows,
        cells=table_cells,
        has_merged_cells=has_merged,
        caption=caption,
        reason=f"XLSX 네이티브 {strat_label} 직접 추출({n_rows}x{n_cols}{merged_note})",
    )
    return RawDocxTableItem(
        table=extracted,
        raw_width_pt=raw_w,
        raw_height_pt=raw_h,
    )


def _segment_sheet_to_items(
    sheet: XlsxSheetRef,
    cells: dict[tuple[int, int], _CellValue],
    merges: list[tuple[int, int, int, int]],
    seq_counter: list[int],
) -> list[RawDocxBodyItem]:
    """시트 셀 맵을 시트 제목·배너/메모 문단·독립 표 블록으로 분할한다."""
    items: list[RawDocxBodyItem] = [
        RawDocxParagraph(
            text=sheet.name,
            heading_level=1,
            font_size_pt=16.0,
            is_bold=True,
        )
    ]
    if not cells:
        return items

    rows_with_data = sorted({r for r, _c in cells})
    if not rows_with_data:
        return items

    # 행 단위 연속 밴드 분할 (병합 영역이 걸친 빈 행은 끊지 않음)
    merge_row_spans = [(r0, r1) for r0, _c0, r1, _c1 in merges if r1 > r0]

    def _row_inside_merge(row_idx: int) -> bool:
        return any(r0 <= row_idx <= r1 for r0, r1 in merge_row_spans)

    bands: list[list[int]] = []
    cur_band: list[int] = [rows_with_data[0]]
    for r in rows_with_data[1:]:
        prev_r = cur_band[-1]
        if r == prev_r + 1 or all(_row_inside_merge(gap) for gap in range(prev_r + 1, r)):
            cur_band.append(r)
        else:
            bands.append(cur_band)
            cur_band = [r]
    bands.append(cur_band)

    # 각 밴드를 배너/문단 또는 표로 분류 (단일 행 배너는 직후 표의 캡션 후보로 연결)
    pending_caption: str | None = None
    for b_idx, band in enumerate(bands):
        band_by_row: dict[int, list[_CellValue]] = {}
        for r in band:
            r_cells = sorted(
                (cv for (cr, _cc), cv in cells.items() if cr == r),
                key=lambda cv: cv.col,
            )
            if r_cells:
                band_by_row[r] = r_cells

        max_cols_in_row = max((len(rc) for rc in band_by_row.values()), default=0)
        is_banner_band = max_cols_in_row <= 1

        if is_banner_band:
            banner_lines = [rc[0].text for rc in band_by_row.values() if rc and rc[0].text]
            has_next_table = False
            if b_idx + 1 < len(bands):
                next_band = bands[b_idx + 1]
                next_max_c = max(
                    (sum(1 for cr, _ in cells if cr == nr) for nr in next_band),
                    default=0,
                )
                has_next_table = next_max_c >= 2

            if len(banner_lines) == 1 and has_next_table and len(banner_lines[0]) <= 120:
                pending_caption = banner_lines[0]
            else:
                if pending_caption:
                    items.append(RawDocxParagraph(text=pending_caption, font_size_pt=10.5))
                    pending_caption = None
                for r_idx_val, rc in band_by_row.items():
                    cv = rc[0]
                    is_sub_heading = (
                        cv.is_bold or (cv.font_size_pt is not None and cv.font_size_pt >= 13.0)
                    ) and len(cv.text) <= 90
                    items.append(
                        RawDocxParagraph(
                            text=cv.text,
                            heading_level=2 if (is_sub_heading and r_idx_val <= 3) else None,
                            font_size_pt=cv.font_size_pt or 10.5,
                            is_bold=cv.is_bold,
                        )
                    )
            continue

        # 표 밴드 내 상단 단독 제목 행이 바로 붙어 있는 경우(예: 0행 1셀 제목 + 1행 다열 헤더) 분리
        table_rows = list(band)
        local_caption = pending_caption
        pending_caption = None
        if len(table_rows) >= 3:
            first_r = table_rows[0]
            second_r = table_rows[1]
            first_cells = band_by_row.get(first_r, [])
            second_cells = band_by_row.get(second_r, [])
            first_spans_width = any(
                r0 == first_r and r1 == first_r and (c1 - c0 + 1) >= max(2, len(second_cells))
                for r0, c0, r1, c1 in merges
            )
            if (
                len(first_cells) == 1
                and len(second_cells) >= 2
                and (first_spans_width or first_cells[0].is_bold)
                and not any(r0 == first_r and r1 > first_r for r0, _, r1, _ in merges)
                and len(first_cells[0].text) <= 120
            ):
                if local_caption:
                    local_caption = f"{local_caption} — {first_cells[0].text}"
                else:
                    local_caption = first_cells[0].text
                table_rows = table_rows[1:]

        tbl_item = _build_band_table(
            table_rows,
            cells,
            merges,
            sheet_index=sheet.sheet_index,
            seq_counter=seq_counter,
            caption=local_caption,
        )
        if tbl_item is not None:
            items.append(tbl_item)
        elif local_caption:
            items.append(RawDocxParagraph(text=local_caption, font_size_pt=10.5))

    if pending_caption:
        items.append(RawDocxParagraph(text=pending_caption, font_size_pt=10.5))
    return items


def extract_xlsx_pages(
    pkg: XlsxPackage,
) -> tuple[list[RawDocxPage], XlsxCoreMetadata, list[str]]:
    """`.xlsx` 패키지에서 워크시트별 페이지 IR(`RawDocxPage`)·메타데이터·경고 목록을 추출한다."""
    sheets = pkg.list_sheets()
    shared_strings = pkg.read_shared_strings()
    styles = pkg.read_styles()
    meta = pkg.read_core_metadata()

    pages: list[RawDocxPage] = []
    warnings: list[str] = []
    seq_counter = [0]

    for sheet in sheets:
        raw_xml = pkg.open_stream(sheet.part_path)
        if not raw_xml:
            cells: dict[tuple[int, int], _CellValue] = {}
            merges: list[tuple[int, int, int, int]] = []
            headers: list[str] = []
            footers: list[str] = []
            truncated = False
        else:
            cells, merges, headers, footers, truncated = _stream_parse_sheet_xml(
                raw_xml,
                shared_strings,
                styles,
            )

        if truncated:
            warnings.append(
                f"시트 '{sheet.name}'가 안전 상한({_MAX_SHEET_ROWS}행/{_MAX_SHEET_COLS}열/"
                f"{_MAX_CELLS_PER_SHEET}셀)을 초과해 일부 셀이 절단되었다."
            )

        items = _segment_sheet_to_items(sheet, cells, merges, seq_counter)
        drawings = _extract_sheet_drawings(pkg, sheet.part_path, seq_counter)
        items.extend(drawings)
        comments = _extract_sheet_comments(pkg, sheet.part_path)

        pages.append(
            RawDocxPage(
                page_number=sheet.sheet_index,
                width_pt=_DEFAULT_SHEET_W,
                height_pt=_DEFAULT_SHEET_H,
                margin_top_pt=36.0,
                margin_bottom_pt=36.0,
                margin_left_pt=36.0,
                margin_right_pt=36.0,
                column_count=1,
                column_space_pt=0.0,
                items=items,
                headers=headers,
                footers=footers,
                comments=comments,
            )
        )

    return pages, meta, warnings


__all__ = ["extract_xlsx_pages"]
