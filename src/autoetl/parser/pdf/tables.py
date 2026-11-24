"""PDF 표 탐지 및 구조 보존 추출 엔진(3모드).

1. 격자형 표 (`ruled_table`): 가로·세로 테두리 선/사각형(얇은 채움 사각형 포함)에서
   교차 격자를 복원하고 내부 경계선 유무로 병합 셀(`row_span`/`col_span`)까지 판별한다.
2. 가로선 표 (`horizontal_ruled_table`): 세로선 없이 상단·헤더·하단 가로선만 있는
   재무제표·보고서·논문(booktabs) 양식에서 텍스트 열 정렬을 추론해 표를 복원한다.
3. 무선 표 (`borderless_table`): 선이 전혀 없지만 일관된 열 간격으로 정렬된 표를
   탐지하되, 2단 본문(다단 산문)과 혼동하지 않도록 밀도·셀 길이·수치 비율을 검증한다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from itertools import pairwise

from autoetl.parser.contracts import ExtractedTable, ParseStrategy, TableCell
from autoetl.parser.pdf.geometry import (
    RawPageData,
    RawTextSpan,
    bbox_iou,
    make_bbox,
    rect_contains_center,
)

_NUMERIC_CELL_RE = re.compile(r"^[\s₩$€¥£+\-±~≈(]*\d[\d,./:\-\s%]*[원개명건회배조억만천%)]*$")
_CODE_CELL_RE = re.compile(r"^[A-Z0-9][A-Z0-9_\-./]{1,15}$")


@dataclass(frozen=True)
class _HSeg:
    y: float
    x0: float
    x1: float

    @property
    def length(self) -> float:
        return self.x1 - self.x0


@dataclass(frozen=True)
class _VSeg:
    x: float
    y0: float
    y1: float

    @property
    def length(self) -> float:
        return self.y1 - self.y0


@dataclass(frozen=True)
class TableDetectionResult:
    """한 페이지의 표 추출 결과와 점유 영역 정보."""

    tables: list[ExtractedTable]
    consumed_span_indices: frozenset[int]
    consumed_table_boxes: tuple[tuple[float, float, float, float], ...]


def _cluster_coords(values: list[float], tol: float = 2.5) -> list[float]:
    """허용 오차 안의 좌표를 평균값으로 군집화한다."""
    if not values:
        return []
    ordered = sorted(values)
    clusters: list[list[float]] = [[ordered[0]]]
    for val in ordered[1:]:
        if val - clusters[-1][-1] <= tol:
            clusters[-1].append(val)
        else:
            clusters.append([val])
    return [sum(c) / len(c) for c in clusters]


def _merge_h_segments(raw_segs: list[_HSeg], y_tol: float = 2.5, x_gap: float = 3.0) -> list[_HSeg]:
    if not raw_segs:
        return []
    ys = _cluster_coords([s.y for s in raw_segs], tol=y_tol)
    buckets: dict[int, list[_HSeg]] = {i: [] for i in range(len(ys))}
    for seg in raw_segs:
        best_i = min(range(len(ys)), key=lambda i: abs(ys[i] - seg.y))
        buckets[best_i].append(seg)

    merged: list[_HSeg] = []
    for i, group in buckets.items():
        if not group:
            continue
        group.sort(key=lambda s: s.x0)
        cur_x0, cur_x1 = group[0].x0, group[0].x1
        for seg in group[1:]:
            if seg.x0 <= cur_x1 + x_gap:
                cur_x1 = max(cur_x1, seg.x1)
            else:
                if cur_x1 - cur_x0 >= 10.0:
                    merged.append(_HSeg(y=ys[i], x0=cur_x0, x1=cur_x1))
                cur_x0, cur_x1 = seg.x0, seg.x1
        if cur_x1 - cur_x0 >= 10.0:
            merged.append(_HSeg(y=ys[i], x0=cur_x0, x1=cur_x1))
    return merged


def _merge_v_segments(raw_segs: list[_VSeg], x_tol: float = 2.5, y_gap: float = 3.0) -> list[_VSeg]:
    if not raw_segs:
        return []
    xs = _cluster_coords([s.x for s in raw_segs], tol=x_tol)
    buckets: dict[int, list[_VSeg]] = {i: [] for i in range(len(xs))}
    for seg in raw_segs:
        best_i = min(range(len(xs)), key=lambda i: abs(xs[i] - seg.x))
        buckets[best_i].append(seg)

    merged: list[_VSeg] = []
    for i, group in buckets.items():
        if not group:
            continue
        group.sort(key=lambda s: s.y0)
        cur_y0, cur_y1 = group[0].y0, group[0].y1
        for seg in group[1:]:
            if seg.y0 <= cur_y1 + y_gap:
                cur_y1 = max(cur_y1, seg.y1)
            else:
                if cur_y1 - cur_y0 >= 8.0:
                    merged.append(_VSeg(x=xs[i], y0=cur_y0, y1=cur_y1))
                cur_y0, cur_y1 = seg.y0, seg.y1
        if cur_y1 - cur_y0 >= 8.0:
            merged.append(_VSeg(x=xs[i], y0=cur_y0, y1=cur_y1))
    return merged


def _collect_axis_segments(page: RawPageData) -> tuple[list[_HSeg], list[_VSeg]]:
    """페이지의 직선·사각형(얇은 채움 선 포함)에서 수평·수직 선분을 수집한다."""
    h_raw: list[_HSeg] = []
    v_raw: list[_VSeg] = []

    for ln in page.lines:
        if ln.is_horizontal:
            y_mid = (ln.top + ln.bottom) / 2.0
            h_raw.append(_HSeg(y=y_mid, x0=ln.x0, x1=ln.x1))
        elif ln.is_vertical:
            x_mid = (ln.x0 + ln.x1) / 2.0
            v_raw.append(_VSeg(x=x_mid, y0=ln.top, y1=ln.bottom))

    page_area = page.width * page.height
    for rect in page.rects:
        # 페이지 전체를 덮는 외곽 프레임은 제외
        if rect.area > page_area * 0.85:
            continue
        # 얇은 채움 사각형은 테두리 선으로 취급(Word/Excel/Chrome PDF 흔한 패턴)
        if rect.is_filled and rect.height <= 3.2 and rect.width >= 12.0:
            h_raw.append(_HSeg(y=(rect.top + rect.bottom) / 2.0, x0=rect.x0, x1=rect.x1))
            continue
        if rect.is_filled and rect.width <= 3.2 and rect.height >= 10.0:
            v_raw.append(_VSeg(x=(rect.x0 + rect.x1) / 2.0, y0=rect.top, y1=rect.bottom))
            continue
        if rect.is_stroked and rect.width >= 8.0 and rect.height >= 8.0:
            h_raw.append(_HSeg(y=rect.top, x0=rect.x0, x1=rect.x1))
            h_raw.append(_HSeg(y=rect.bottom, x0=rect.x0, x1=rect.x1))
            v_raw.append(_VSeg(x=rect.x0, y0=rect.top, y1=rect.bottom))
            v_raw.append(_VSeg(x=rect.x1, y0=rect.top, y1=rect.bottom))

    return _merge_h_segments(h_raw), _merge_v_segments(v_raw)


def _segments_intersect(h: _HSeg, v: _VSeg, tol: float = 3.5) -> bool:
    return (h.x0 - tol <= v.x <= h.x1 + tol) and (v.y0 - tol <= h.y <= v.y1 + tol)


def _join_spans_text(spans: list[RawTextSpan]) -> str:
    """셀 안의 텍스트 스팬들을 줄 단위로 정렬해 읽기 자연스럽게 합친다."""
    if not spans:
        return ""
    ordered = sorted(spans, key=lambda s: (round(s.y_center / 3.0), s.x0))
    lines: list[list[RawTextSpan]] = []
    for sp in ordered:
        if not lines:
            lines.append([sp])
            continue
        prev = lines[-1]
        ref_y = sum(s.y_center for s in prev) / len(prev)
        ref_h = max(s.font_size for s in prev)
        if abs(sp.y_center - ref_y) <= max(3.5, ref_h * 0.55):
            prev.append(sp)
        else:
            lines.append([sp])

    rendered_lines: list[str] = []
    for line in lines:
        line.sort(key=lambda s: s.x0)
        parts: list[str] = [line[0].text.strip()]
        for prev_sp, cur_sp in pairwise(line):
            gap = cur_sp.x0 - prev_sp.x1
            cur_txt = cur_sp.text.strip()
            if not cur_txt:
                continue
            has_explicit_space = prev_sp.text.endswith(" ") or cur_sp.text.startswith(" ")
            if has_explicit_space or gap > max(1.5, cur_sp.font_size * 0.18):
                parts.append(" " + cur_txt)
            else:
                parts.append(cur_txt)
        joined = "".join(parts).strip()
        if joined:
            rendered_lines.append(joined)
    return " ".join(rendered_lines).strip()


def _has_v_border(
    v_segs: list[_VSeg], x: float, y0: float, y1: float, *, x_tol: float = 3.5
) -> bool:
    span = max(1.0, y1 - y0)
    covered = 0.0
    for v in v_segs:
        if abs(v.x - x) <= x_tol:
            ov = min(v.y1, y1) - max(v.y0, y0)
            if ov > 0.0:
                covered += ov
    return covered >= span * 0.55


def _has_h_border(
    h_segs: list[_HSeg], y: float, x0: float, x1: float, *, y_tol: float = 3.5
) -> bool:
    span = max(1.0, x1 - x0)
    covered = 0.0
    for h in h_segs:
        if abs(h.y - y) <= y_tol:
            ov = min(h.x1, x1) - max(h.x0, x0)
            if ov > 0.0:
                covered += ov
    return covered >= span * 0.55


def _is_near_white(rgb: tuple[float, float, float] | None) -> bool:
    if rgb is None:
        return False
    return rgb[0] >= 0.96 and rgb[1] >= 0.96 and rgb[2] >= 0.96


def _same_fill_color(
    c0: tuple[float, float, float] | None,
    c1: tuple[float, float, float] | None,
) -> bool:
    if c0 is None or c1 is None:
        return True
    return all(abs(a - b) <= 0.05 for a, b in zip(c0, c1, strict=True))


def _infer_header_row_count(
    rows: list[list[str]],
    row_bold_ratios: list[float],
    row_shaded: list[bool],
    row_fill_colors: list[tuple[float, float, float] | None] | None = None,
) -> int:
    """표의 상단 헤더 행 수(0~2)를 굵기·음영·색상 일관성·수치 패턴 변화로 추론한다."""
    if len(rows) <= 1:
        return 0

    first_non_empty = [c for c in rows[0] if c.strip()]
    second_non_empty = [c for c in rows[1] if c.strip()]
    first_num = sum(1 for c in first_non_empty if _NUMERIC_CELL_RE.match(c.strip()))
    second_num = sum(1 for c in second_non_empty if _NUMERIC_CELL_RE.match(c.strip()))

    if row_shaded and row_shaded[0]:
        if len(rows) > 2 and row_shaded[1]:
            # 2행 헤더 승격은 아래 4가지 안전 조건을 모두 통과할 때만 허용한다(첫 행 누락 차단):
            # 1) 본문(3행 이후)에 교대 음영(zebra)/배경 채움이 없어야 함
            # 2) 1행과 2행의 배경색이 서로 다른 색(예: 진한 헤더 vs 연한 데이터 줄)이 아니어야 함
            # 3) 1행은 순수 텍스트인데 2행에 수치 데이터(매출·건수 등)가 나오면 2행은 데이터 행
            # 4) 1행만 굵게(>=0.6) 강조되고 2행은 일반체(<0.5)이면 2행은 데이터 행
            body_shaded = any(row_shaded[2:])
            same_color = True
            if row_fill_colors is not None and len(row_fill_colors) >= 2:
                same_color = _same_fill_color(row_fill_colors[0], row_fill_colors[1])
            second_looks_numeric = first_num == 0 and second_num >= 1
            bold_drops_to_body = (
                len(row_bold_ratios) >= 2 and row_bold_ratios[0] >= 0.6 and row_bold_ratios[1] < 0.5
            )
            if (
                not body_shaded
                and same_color
                and not second_looks_numeric
                and not bold_drops_to_body
            ):
                return 2
        return 1
    if (
        row_bold_ratios
        and row_bold_ratios[0] >= 0.6
        and (len(row_bold_ratios) == 1 or row_bold_ratios[1] < 0.5)
    ):
        return 1

    # 첫 행은 비수치(레이블)이고 둘째 행부터 수치/코드가 나오면 1행 헤더
    if first_non_empty and second_non_empty and first_num == 0 and second_num >= 1:
        return 1
    return 1 if first_non_empty else 0


def _detect_ruled_grid_tables(
    page: RawPageData,
    h_segs: list[_HSeg],
    v_segs: list[_VSeg],
    excluded_span_indices: set[int],
    next_index: int,
) -> tuple[list[ExtractedTable], set[int], list[tuple[float, float, float, float]]]:
    """교차하는 가로·세로 선분 연결 요소에서 격자형 표(병합 셀 포함)를 추출한다."""
    if len(h_segs) < 2 or len(v_segs) < 2:
        return [], set(), []

    n_h = len(h_segs)
    n_total = n_h + len(v_segs)
    parent = list(range(n_total))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for hi, h in enumerate(h_segs):
        for vi, v in enumerate(v_segs):
            if _segments_intersect(h, v):
                union(hi, n_h + vi)

    comp_h: dict[int, list[_HSeg]] = {}
    comp_v: dict[int, list[_VSeg]] = {}
    for hi, h in enumerate(h_segs):
        comp_h.setdefault(find(hi), []).append(h)
    for vi, v in enumerate(v_segs):
        comp_v.setdefault(find(n_h + vi), []).append(v)

    tables: list[ExtractedTable] = []
    used_spans: set[int] = set()
    used_boxes: list[tuple[float, float, float, float]] = []

    components: list[tuple[float, float, list[_HSeg], list[_VSeg]]] = []
    for root, hs in comp_h.items():
        vs = comp_v.get(root, [])
        if len(hs) < 2 or len(vs) < 1:
            continue
        ys = _cluster_coords([s.y for s in hs], tol=3.0)
        xs = _cluster_coords([s.x for s in vs], tol=3.0)
        if len(ys) < 3 or len(xs) < 1:
            # 2행 미만이면 단순 테두리 상자일 가능성이 높다
            continue
        components.append((ys[0], xs[0], hs, vs))

    components.sort(key=lambda c: (c[0], c[1]))
    shaded_rects = [
        r
        for r in page.rects
        if r.is_filled and r.width >= 15.0 and r.height >= 8.0 and not _is_near_white(r.fill_color)
    ]

    for _, _, hs, vs in components:
        ys = _cluster_coords([s.y for s in hs], tol=3.0)
        xs = _cluster_coords([s.x for s in vs], tol=3.0)
        if len(ys) < 3 or len(xs) < 1:
            continue

        # 세로 외곽선이 생략된 좌·우 끝 열(열린 표: 가로선이 세로선 바깥까지 뻗음) 복원
        min_hx0 = min(h.x0 for h in hs)
        max_hx1 = max(h.x1 for h in hs)
        if xs[0] - min_hx0 >= 25.0:
            left_wing = (min_hx0, ys[0], xs[0], ys[-1])
            if any(
                idx not in excluded_span_indices
                and idx not in used_spans
                and rect_contains_center(left_wing, (sp.x0, sp.top, sp.x1, sp.bottom), margin=2.0)
                for idx, sp in enumerate(page.spans)
            ):
                xs = [min_hx0, *xs]
        if max_hx1 - xs[-1] >= 25.0:
            right_wing = (xs[-1], ys[0], max_hx1, ys[-1])
            if any(
                idx not in excluded_span_indices
                and idx not in used_spans
                and rect_contains_center(right_wing, (sp.x0, sp.top, sp.x1, sp.bottom), margin=2.0)
                for idx, sp in enumerate(page.spans)
            ):
                xs = [*xs, max_hx1]

        n_rows = len(ys) - 1
        n_cols = len(xs) - 1
        if n_rows < 2 or n_cols < 2:
            continue

        tx0, ttop, tx1, tbot = xs[0], ys[0], xs[-1], ys[-1]
        table_width = max(1.0, tx1 - tx0)
        table_height = max(1.0, tbot - ttop)

        # 배경색이 다른 행 밴드(예: 흰 행과 연녹색 소계 행 사이에 테두리선이 생략된 한컴/엑셀 표)의
        # 수평 경계를 복원해 행 경계와 음영 판정을 정확히 맞춘다.
        eff_hs = list(hs)
        table_shaded = [
            sr
            for sr in shaded_rects
            if sr.height < table_height * 0.75
            and sr.x0 >= tx0 - 4.0
            and sr.x1 <= tx1 + 4.0
            and sr.top >= ttop - 4.0
            and sr.bottom <= tbot + 4.0
        ]
        if table_shaded:
            edge_ys = _cluster_coords(
                [val for sr in table_shaded for val in (sr.top, sr.bottom)],
                tol=2.5,
            )
            added_shaded_h = False
            for cy in edge_ys:
                if any(abs(cy - existing_y) <= 3.0 for existing_y in ys):
                    continue
                ivs = sorted(
                    (max(tx0, sr.x0), min(tx1, sr.x1))
                    for sr in table_shaded
                    if (abs(sr.top - cy) <= 2.5 or abs(sr.bottom - cy) <= 2.5)
                    and min(tx1, sr.x1) > max(tx0, sr.x0)
                )
                if not ivs:
                    continue
                merged_ivs: list[tuple[float, float]] = [ivs[0]]
                for ix0, ix1 in ivs[1:]:
                    px0, px1 = merged_ivs[-1]
                    if ix0 <= px1 + 3.0:
                        merged_ivs[-1] = (px0, max(px1, ix1))
                    else:
                        merged_ivs.append((ix0, ix1))
                covered = sum(ix1 - ix0 for ix0, ix1 in merged_ivs)
                if covered >= table_width * 0.60:
                    for ix0, ix1 in merged_ivs:
                        if ix1 - ix0 >= 10.0:
                            eff_hs.append(_HSeg(y=cy, x0=ix0, x1=ix1))
                            added_shaded_h = True
            if added_shaded_h:
                ys = _cluster_coords([s.y for s in eff_hs], tol=3.0)
                n_rows = len(ys) - 1
                ttop, tbot = ys[0], ys[-1]
                table_height = max(1.0, tbot - ttop)

        tbox = (tx0, ttop, tx1, tbot)

        # 이 표 영역 안의 텍스트 스팬 수집
        in_table_indices = [
            idx
            for idx, sp in enumerate(page.spans)
            if idx not in excluded_span_indices
            and idx not in used_spans
            and rect_contains_center(tbox, (sp.x0, sp.top, sp.x1, sp.bottom), margin=2.0)
        ]
        if not in_table_indices:
            continue

        # 병합 셀 탐지: 점유 배열을 돌며 오른쪽/아래쪽 내부 경계선 부재를 확인
        visited = [[False] * n_cols for _ in range(n_rows)]
        raw_cells: list[tuple[TableCell, list[RawTextSpan]]] = []
        row_has_vmerge = [False] * n_rows
        row_bold_hits = [0] * n_rows
        row_span_totals = [0] * n_rows
        has_merged = False

        for r in range(n_rows):
            for c in range(n_cols):
                if visited[r][c]:
                    continue
                c_end = c
                while c_end + 1 < n_cols and not _has_v_border(vs, xs[c_end + 1], ys[r], ys[r + 1]):
                    c_end += 1
                r_end = r
                while r_end + 1 < n_rows:
                    # 가로 병합 폭 전체에서 아래 경계선이 없어야 세로 병합 인정
                    if all(
                        not _has_h_border(eff_hs, ys[r_end + 1], xs[cc], xs[cc + 1])
                        for cc in range(c, c_end + 1)
                    ):
                        r_end += 1
                    else:
                        break

                row_span = r_end - r + 1
                col_span = c_end - c + 1
                if row_span > 1 or col_span > 1:
                    has_merged = True
                if row_span > 1:
                    for rr in range(r, r_end + 1):
                        row_has_vmerge[rr] = True

                for rr in range(r, r_end + 1):
                    for cc in range(c, c_end + 1):
                        visited[rr][cc] = True

                cell_rect = (xs[c], ys[r], xs[c_end + 1], ys[r_end + 1])
                cell_spans = [
                    page.spans[idx]
                    for idx in in_table_indices
                    if rect_contains_center(
                        cell_rect,
                        (
                            page.spans[idx].x0,
                            page.spans[idx].top,
                            page.spans[idx].x1,
                            page.spans[idx].bottom,
                        ),
                        margin=1.0,
                    )
                ]
                cell_text = _join_spans_text(cell_spans)
                for sp in cell_spans:
                    row_span_totals[r] += 1
                    if sp.is_bold:
                        row_bold_hits[r] += 1

                raw_cells.append(
                    (
                        TableCell(
                            row=r,
                            col=c,
                            row_span=row_span,
                            col_span=col_span,
                            text=cell_text,
                            is_header=False,
                            bbox=make_bbox(
                                cell_rect[0],
                                cell_rect[1],
                                cell_rect[2],
                                cell_rect[3],
                                page.width,
                                page.height,
                            ),
                        ),
                        cell_spans,
                    )
                )

        # 묶음 행(예: 5행마다만 가로선을 그리는 통계표·강조 박스 내부 2행) 분할:
        # 세로 병합 없는 격자 행에서 여러 열이 공통 다중 열 기준선(anchor) k(>=2)개를 공유하면 전개
        expanded_grid: list[list[str]] = []
        expanded_cells: list[TableCell] = []
        expanded_bold: list[float] = []
        expanded_shaded: list[bool] = []
        expanded_fill_colors: list[tuple[float, float, float] | None] = []
        split_subrows_used = False

        cells_by_row: dict[int, list[tuple[TableCell, list[RawTextSpan]]]] = {
            r: [] for r in range(n_rows)
        }
        for cell, c_spans in raw_cells:
            cells_by_row[cell.row].append((cell, c_spans))

        for r in range(n_rows):
            row_entries = cells_by_row[r]
            row_h = max(1.0, ys[r + 1] - ys[r])
            # 행 중심을 덮고 표 전체 배경(>=75%)이 아닌 채움 사각형(셀 단위 분할 채움 포함) 집계
            row_y_mid = (ys[r] + ys[r + 1]) / 2.0
            row_shade_hits = [
                sr
                for sr in shaded_rects
                if sr.height < table_height * 0.75
                and sr.top <= row_y_mid <= sr.bottom
                and (min(sr.bottom, ys[r + 1]) - max(sr.top, ys[r])) >= row_h * 0.55
                and min(sr.x1, tx1) > max(sr.x0, tx0)
            ]
            is_shaded_r = False
            fill_color_r: tuple[float, float, float] | None = None
            if row_shade_hits:
                shade_ivs = sorted(
                    (max(tx0, sr.x0), min(tx1, sr.x1)) for sr in row_shade_hits
                )
                merged_sh: list[tuple[float, float]] = [shade_ivs[0]]
                for sx0, sx1 in shade_ivs[1:]:
                    px0, px1 = merged_sh[-1]
                    if sx0 <= px1 + 3.0:
                        merged_sh[-1] = (px0, max(px1, sx1))
                    else:
                        merged_sh.append((sx0, sx1))
                shade_cov = sum(sx1 - sx0 for sx0, sx1 in merged_sh)
                if shade_cov >= table_width * 0.70:
                    is_shaded_r = True
                    best_sr = max(
                        row_shade_hits,
                        key=lambda sr: min(sr.x1, tx1) - max(sr.x0, tx0),
                    )
                    fill_color_r = best_sr.fill_color
            bold_r = (row_bold_hits[r] / row_span_totals[r]) if row_span_totals[r] > 0 else 0.0

            can_split = not row_has_vmerge[r] and len(row_entries) >= 3
            anchor_centers: list[float] = []
            if can_split:
                all_r_spans = [sp for _, sps in row_entries for sp in sps]
                cand_bands = _group_spans_into_rows(all_r_spans)
                if len(cand_bands) >= 2:
                    raw_centers = [
                        sum(sp.y_center for sp in band) / len(band) for band in cand_bands
                    ]
                    # 다중 열(>=2열)이 실제로 정렬된 밴드만 유효 앵커 행으로 채택해
                    # 1개 열의 줄바꿈(예: 2줄 비고 셀)이 3개 빈 행으로 찢어지는 현상을 차단한다.
                    band_col_counts = [0] * len(raw_centers)
                    for _, sps in row_entries:
                        if not sps:
                            continue
                        hit_b = {
                            min(
                                range(len(raw_centers)),
                                key=lambda bi: abs(raw_centers[bi] - sp.y_center),
                            )
                            for sp in sps
                        }
                        for bi in hit_b:
                            band_col_counts[bi] += 1
                    cand_anchors = [
                        raw_centers[bi]
                        for bi, cnt in enumerate(band_col_counts)
                        if cnt >= 2
                    ]
                    if len(cand_anchors) >= 2:
                        multi_line_cols = 0
                        non_empty_cols = 0
                        for _, sps in row_entries:
                            if not sps:
                                continue
                            non_empty_cols += 1
                            hit_anchors = {
                                min(
                                    range(len(cand_anchors)),
                                    key=lambda ai: abs(cand_anchors[ai] - sp.y_center),
                                )
                                for sp in sps
                            }
                            if len(hit_anchors) >= 2:
                                multi_line_cols += 1
                        if non_empty_cols >= 3 and multi_line_cols >= max(
                            2, int(non_empty_cols * 0.5)
                        ):
                            anchor_centers = cand_anchors

            if anchor_centers:
                split_subrows_used = True
                for ai in range(len(anchor_centers)):
                    out_r = len(expanded_grid)
                    row_vals = [""] * n_cols
                    for cell, sps in row_entries:
                        sub_sps = [
                            sp
                            for sp in sps
                            if min(
                                range(len(anchor_centers)),
                                key=lambda k: abs(anchor_centers[k] - sp.y_center),
                            )
                            == ai
                        ]
                        txt = _join_spans_text(sub_sps)
                        row_vals[cell.col] = txt
                        expanded_cells.append(cell.model_copy(update={"row": out_r, "text": txt}))
                    expanded_grid.append(row_vals)
                    expanded_bold.append(bold_r)
                    expanded_shaded.append(is_shaded_r)
                    expanded_fill_colors.append(fill_color_r)
            else:
                out_r = len(expanded_grid)
                row_vals = [""] * n_cols
                for cell, _ in row_entries:
                    row_vals[cell.col] = cell.text
                    expanded_cells.append(cell.model_copy(update={"row": out_r}))
                expanded_grid.append(row_vals)
                expanded_bold.append(bold_r)
                expanded_shaded.append(is_shaded_r)
                expanded_fill_colors.append(fill_color_r)

        non_empty_cells = sum(1 for cell in expanded_cells if cell.text.strip())
        if non_empty_cells < 2:
            continue

        out_n_rows = len(expanded_grid)
        header_rows = _infer_header_row_count(
            expanded_grid, expanded_bold, expanded_shaded, expanded_fill_colors
        )
        final_cells = [
            cell.model_copy(update={"is_header": cell.row < header_rows}) for cell in expanded_cells
        ]
        headers = expanded_grid[0] if header_rows >= 1 else [f"col_{i + 1}" for i in range(n_cols)]
        body_rows = expanded_grid[header_rows:] if header_rows > 0 else expanded_grid

        table_id = f"p{page.page:03d}_t{next_index + len(tables):02d}"
        tables.append(
            ExtractedTable(
                table_id=table_id,
                page=page.page,
                bbox=make_bbox(tx0, ttop, tx1, tbot, page.width, page.height),
                strategy=ParseStrategy.RULED_TABLE,
                confidence=0.95 if non_empty_cells >= n_cols else 0.85,
                row_count=out_n_rows,
                col_count=n_cols,
                header_row_count=header_rows,
                headers=headers,
                rows=body_rows,
                cells=final_cells,
                has_merged_cells=has_merged,
                reason=(
                    f"교차 격자 선분(가로 {len(ys)}개 x 세로 {len(xs)}개)으로 "
                    f"{out_n_rows}행x{n_cols}열 격자 표를 복원함"
                    + (" (병합 셀 감지)" if has_merged else "")
                    + (" (묶음 행 자동 분할)" if split_subrows_used else "")
                ),
            )
        )
        used_spans.update(in_table_indices)
        used_boxes.append(tbox)

    return tables, used_spans, used_boxes


def _group_spans_into_rows(spans: list[RawTextSpan]) -> list[list[RawTextSpan]]:
    """스팬들을 y 중심 기준으로 시각적 행 단위로 묶는다."""
    if not spans:
        return []
    ordered = sorted(spans, key=lambda s: (s.y_center, s.x0))
    rows: list[list[RawTextSpan]] = []
    for sp in ordered:
        if not rows:
            rows.append([sp])
            continue
        cur = rows[-1]
        ref_y = sum(s.y_center for s in cur) / len(cur)
        ref_h = sum(s.font_size for s in cur) / len(cur)
        if abs(sp.y_center - ref_y) <= max(4.0, ref_h * 0.55):
            cur.append(sp)
        else:
            rows.append([sp])
    for row in rows:
        row.sort(key=lambda s: s.x0)
    return rows


def _extract_intervals_from_occ(
    occ: list[int], x_min: float, *, max_bridge: int = 0, min_gap: int = 5
) -> list[tuple[float, float]]:
    width = len(occ)
    col_intervals: list[tuple[float, float]] = []
    in_col = False
    col_start = 0
    low_run = 0
    for i, val in enumerate(occ):
        if val > max_bridge:
            if not in_col:
                in_col = True
                col_start = i
            low_run = 0
        else:
            if in_col:
                low_run += 1
                if low_run >= min_gap:
                    col_end = i - low_run + 1
                    if col_end > col_start + 2:
                        col_intervals.append((x_min + col_start, x_min + col_end))
                    in_col = False
                    low_run = 0
    if in_col:
        col_end = width - low_run
        if col_end > col_start + 2:
            col_intervals.append((x_min + col_start, x_min + col_end))
    return col_intervals


def _infer_columns_from_rows(
    rows: list[list[RawTextSpan]], x_min: float, x_max: float
) -> list[tuple[float, float]]:
    """여러 행의 텍스트 스팬 x 투영(빈 세로 골짜기)에서 열 구간을 추론한다.

    LaTeX booktabs 등의 병합 슈퍼 헤더(1~2행)가 열 간격을 가로질러도 본문 행의
    깨끗한 열 경계를 놓치지 않도록 상위 헤더를 제외한 본문 투영과 브리지 완화를 병행한다.
    """
    width = int(max(1.0, x_max - x_min)) + 1
    if width <= 20:
        return []

    def build_occ(row_slice: list[list[RawTextSpan]]) -> list[int]:
        occ_buf = [0] * width
        for row in row_slice:
            # 한 행 안에서 가까운 스팬(단어 간격)은 이어서 투영해 단어 공백이 열 경계가 되지 않게 함
            merged_segs: list[tuple[float, float]] = []
            for sp in row:
                if not merged_segs:
                    merged_segs.append((sp.x0, sp.x1))
                    continue
                px0, px1 = merged_segs[-1]
                if sp.x0 - px1 <= max(6.0, sp.font_size * 0.7):
                    merged_segs[-1] = (px0, max(px1, sp.x1))
                else:
                    merged_segs.append((sp.x0, sp.x1))
            for sx0, sx1 in merged_segs:
                i0 = max(0, int(sx0 - x_min))
                i1 = min(width - 1, int(sx1 - x_min))
                for i in range(i0, i1 + 1):
                    occ_buf[i] += 1
        return occ_buf

    def all_cols_supported(
        cand_cols: list[tuple[float, float]],
        body_rows: list[list[RawTextSpan]],
        *,
        min_ratio: float = 0.25,
        min_abs: int = 2,
    ) -> bool:
        min_support = max(min_abs, int(len(body_rows) * min_ratio))
        for cx0, cx1 in cand_cols:
            hits = sum(
                1 for row in body_rows if any(cx0 - 2.0 <= sp.x_center <= cx1 + 2.0 for sp in row)
            )
            if hits < min_support:
                return False
        return True

    occ = build_occ(rows)
    cols = _extract_intervals_from_occ(occ, x_min, max_bridge=0, min_gap=5)

    # 상위 1~2행의 병합 슈퍼 헤더('BLEU', 'Training Cost' 등)가 하위 열 간격을 덮은 경우,
    # 본문 행만의 투영에서 깨끗하게 드러나는 하위 열을 우선 복원한다.
    for skip in (1, 2):
        if len(rows) >= 4 + skip:
            body_rows = rows[skip:]
            body_occ = build_occ(body_rows)
            body_cols = _extract_intervals_from_occ(body_occ, x_min, max_bridge=0, min_gap=6)
            if len(body_cols) > len(cols) and all_cols_supported(body_cols, body_rows):
                cols = body_cols
            # 본문 일부 행(예: 하단 요약/병합 1~2행)만 넓은 열 골짜기(>=8pt)를 가로지르는 경우,
            # 모든 하위 열이 본문 행의 40% 이상에서 독립 지지되면 하위 열을 복원한다.
            if len(body_rows) >= 8:
                max_br = min(2, max(1, len(body_rows) // 5))
                valley_cols = _extract_intervals_from_occ(
                    body_occ, x_min, max_bridge=max_br, min_gap=8
                )
                if len(valley_cols) > len(cols) and all_cols_supported(
                    valley_cols, body_rows, min_ratio=0.40, min_abs=3
                ):
                    cols = valley_cols

    # 그래도 5행 이상 표가 1~2열로 뭉개진 경우 브리지 허용치를 단계적으로 완화해 재탐색
    if len(rows) >= 5:
        max_col_w = max((b - a for a, b in cols), default=0.0)
        if len(cols) <= 2 or max_col_w > width * 0.48:
            relaxed = _extract_intervals_from_occ(occ, x_min, max_bridge=1, min_gap=5)
            if len(relaxed) > len(cols):
                cols = relaxed
        if len(rows) >= 8:
            max_col_w = max((b - a for a, b in cols), default=0.0)
            if len(cols) <= 2 or max_col_w > width * 0.45:
                relaxed2 = _extract_intervals_from_occ(occ, x_min, max_bridge=2, min_gap=6)
                if len(relaxed2) > len(cols):
                    cols = relaxed2
    return cols


def _build_table_from_rows_and_cols(
    page: RawPageData,
    rows: list[list[RawTextSpan]],
    col_intervals: list[tuple[float, float]],
    bbox_tuple: tuple[float, float, float, float],
    *,
    table_id: str,
    strategy: ParseStrategy,
    confidence: float,
    reason: str,
) -> ExtractedTable:
    n_rows = len(rows)
    n_cols = len(col_intervals)
    col_centers = [(a + b) / 2.0 for a, b in col_intervals]

    grid_text = [[""] * n_cols for _ in range(n_rows)]
    cells: list[TableCell] = []
    row_bold_ratios: list[float] = []

    for r, row_spans in enumerate(rows):
        buckets: list[list[RawTextSpan]] = [[] for _ in range(n_cols)]
        bold_cnt = sum(1 for sp in row_spans if sp.is_bold)
        row_bold_ratios.append(bold_cnt / max(1, len(row_spans)))
        # 같은 행 안에서 밀접하게 이어진 인라인 스팬(예: '3.3 · 10^18')은 한 단위로 묶어
        # 열 경계에서 수식·어절이 반으로 찢어지지 않게 한다.
        ordered_row = sorted(row_spans, key=lambda s: s.x0)
        phrases: list[list[RawTextSpan]] = []
        for sp in ordered_row:
            if not phrases:
                phrases.append([sp])
                continue
            prev_x1 = max(s.x1 for s in phrases[-1])
            if sp.x0 - prev_x1 <= max(5.0, sp.font_size * 0.55):
                phrases[-1].append(sp)
            else:
                phrases.append([sp])

        for phrase in phrases:
            px0 = min(s.x0 for s in phrase)
            px1 = max(s.x1 for s in phrase)
            pcx = (px0 + px1) / 2.0
            # 겹치는 열 구간 우선, 없으면 가장 가까운 열 중심
            best_col = -1
            best_overlap = 0.0
            for ci, (cx0, cx1) in enumerate(col_intervals):
                ov = min(px1, cx1) - max(px0, cx0)
                if ov > best_overlap:
                    best_overlap = ov
                    best_col = ci
            if best_col < 0:
                best_col = min(range(n_cols), key=lambda ci: abs(col_centers[ci] - pcx))
            buckets[best_col].extend(phrase)

        r_top = min(sp.top for sp in row_spans)
        r_bot = max(sp.bottom for sp in row_spans)
        for c in range(n_cols):
            text = _join_spans_text(buckets[c])
            grid_text[r][c] = text
            cells.append(
                TableCell(
                    row=r,
                    col=c,
                    text=text,
                    is_header=False,
                    bbox=make_bbox(
                        col_intervals[c][0],
                        r_top,
                        col_intervals[c][1],
                        r_bot,
                        page.width,
                        page.height,
                    ),
                )
            )

    header_rows = _infer_header_row_count(grid_text, row_bold_ratios, [])
    final_cells = [cell.model_copy(update={"is_header": cell.row < header_rows}) for cell in cells]
    headers = grid_text[0] if header_rows >= 1 else [f"col_{i + 1}" for i in range(n_cols)]
    body_rows = grid_text[header_rows:] if header_rows > 0 else grid_text

    return ExtractedTable(
        table_id=table_id,
        page=page.page,
        bbox=make_bbox(
            bbox_tuple[0], bbox_tuple[1], bbox_tuple[2], bbox_tuple[3], page.width, page.height
        ),
        strategy=strategy,
        confidence=confidence,
        row_count=n_rows,
        col_count=n_cols,
        header_row_count=header_rows,
        headers=headers,
        rows=body_rows,
        cells=final_cells,
        has_merged_cells=False,
        reason=reason,
    )


def _detect_horizontal_ruled_tables(
    page: RawPageData,
    h_segs: list[_HSeg],
    excluded_span_indices: set[int],
    existing_boxes: list[tuple[float, float, float, float]],
    next_index: int,
) -> tuple[list[ExtractedTable], set[int], list[tuple[float, float, float, float]]]:
    """세로선 없는 가로선 표(재무제표·보고서·booktabs)를 탐지한다."""
    cand_lines = [
        h
        for h in h_segs
        if h.length >= max(100.0, page.width * 0.25)
        and not any(
            box[0] - 4.0 <= (h.x0 + h.x1) / 2.0 <= box[2] + 4.0
            and box[1] - 4.0 <= h.y <= box[3] + 4.0
            for box in existing_boxes
        )
    ]
    if len(cand_lines) < 2:
        return [], set(), []

    cand_lines.sort(key=lambda h: h.y)
    # x 범위가 70% 이상 겹치고 수직 간격이 합리적인 가로선들을 그룹으로 묶음
    groups: list[list[_HSeg]] = []
    for seg in cand_lines:
        placed = False
        for grp in groups:
            ref = grp[-1]
            ov = min(seg.x1, ref.x1) - max(seg.x0, ref.x0)
            min_len = min(seg.length, ref.length)
            if ov >= min_len * 0.7 and 8.0 <= seg.y - ref.y <= page.height * 0.45:
                grp.append(seg)
                placed = True
                break
        if not placed:
            groups.append([seg])

    tables: list[ExtractedTable] = []
    used_spans: set[int] = set()
    used_boxes: list[tuple[float, float, float, float]] = []

    for grp in groups:
        if len(grp) < 2:
            continue
        gx0 = min(s.x0 for s in grp)
        gx1 = max(s.x1 for s in grp)
        gtop = grp[0].y
        gbot = grp[-1].y
        if gbot - gtop < 20.0:
            continue

        # 2선(헤더 아래선 + 하단선)만 있는 경우도 위쪽 1행 헤더를 포함해 확인
        search_top = gtop - 24.0 if len(grp) == 2 else gtop - 2.0
        band_indices = [
            idx
            for idx, sp in enumerate(page.spans)
            if idx not in excluded_span_indices
            and idx not in used_spans
            and gx0 - 6.0 <= sp.x_center <= gx1 + 6.0
            and search_top <= sp.y_center <= gbot + 2.0
        ]
        if len(band_indices) < 4:
            continue

        rows = _group_spans_into_rows([page.spans[i] for i in band_indices])
        if len(rows) < 2:
            continue
        col_intervals = _infer_columns_from_rows(rows, gx0 - 4.0, gx1 + 4.0)
        if len(col_intervals) < 2:
            continue

        # 최소 행의 절반 이상이 2개 이상의 열에 걸친 스팬을 가져야 표로 인정
        col_centers = [(a + b) / 2.0 for a, b in col_intervals]
        multi_col_rows = 0
        for row in rows:
            hit_cols = {
                min(range(len(col_centers)), key=lambda ci: abs(col_centers[ci] - sp.x_center))
                for sp in row
            }
            if len(hit_cols) >= 2:
                multi_col_rows += 1
        if multi_col_rows < max(2, int(len(rows) * 0.6)):
            continue

        actual_top = min(gtop, min(sp.top for sp in rows[0]))
        actual_bot = max(gbot, max(sp.bottom for sp in rows[-1]))
        tbox = (gx0, actual_top, gx1, actual_bot)
        if any(bbox_iou(tbox, b) > 0.3 for b in existing_boxes + used_boxes):
            continue

        table_id = f"p{page.page:03d}_t{next_index + len(tables):02d}"
        tables.append(
            _build_table_from_rows_and_cols(
                page,
                rows,
                col_intervals,
                tbox,
                table_id=table_id,
                strategy=ParseStrategy.HORIZONTAL_RULED_TABLE,
                confidence=0.88,
                reason=(
                    f"평행 가로선 {len(grp)}개 내부의 텍스트 열 정렬로 "
                    f"{len(rows)}행x{len(col_intervals)}열 가로선 표를 복원함"
                ),
            )
        )
        used_spans.update(band_indices)
        used_boxes.append(tbox)

    return tables, used_spans, used_boxes


def _split_row_segments(row: list[RawTextSpan]) -> list[tuple[float, float, str]]:
    """한 행의 스팬들을 큰 가로 간격 기준으로 세그먼트(잠재적 셀)로 나눈다."""
    if not row:
        return []
    segs: list[list[RawTextSpan]] = [[row[0]]]
    for sp in row[1:]:
        prev = segs[-1][-1]
        gap = sp.x0 - prev.x1
        if gap > max(14.0, sp.font_size * 1.4):
            segs.append([sp])
        else:
            segs[-1].append(sp)
    return [(min(s.x0 for s in seg), max(s.x1 for s in seg), _join_spans_text(seg)) for seg in segs]


def _display_width_len(text: str) -> float:
    """한글·한자(W/F)는 2칸, 영문·숫자는 1칸으로 계산한 표시 폭."""
    import unicodedata

    return sum(2.0 if unicodedata.east_asian_width(ch) in ("W", "F") else 1.0 for ch in text)


def _is_likely_multi_column_prose(
    rows: list[list[RawTextSpan]],
    col_intervals: list[tuple[float, float]],
    page_width: float,
) -> bool:
    """2단 산문(논문/한국어 보고서)을 무선 표로 오인하지 않도록 판별한다."""
    n_cols = len(col_intervals)
    if n_cols > 3:
        return False

    col_widths = [b - a for a, b in col_intervals]
    wide_cols = sum(1 for w in col_widths if w >= page_width * 0.28)

    cell_texts: list[str] = []
    for row in rows:
        for _, _, txt in _split_row_segments(row):
            if txt.strip():
                cell_texts.append(txt.strip())

    if not cell_texts:
        return True

    avg_disp_len = sum(_display_width_len(t) for t in cell_texts) / len(cell_texts)
    numeric_or_code = sum(
        1 for t in cell_texts if _NUMERIC_CELL_RE.match(t) or _CODE_CELL_RE.match(t)
    )
    num_ratio = numeric_or_code / len(cell_texts)

    # 문장 어미·구두점이 나타나는 산문 셀 비율
    prose_markers = ("다.", "요.", "는다", "한다", "있다", "였다", "으로 ", "에서 ", "의 ", ". ")
    prose_hits = sum(1 for t in cell_texts if any(m in t for m in prose_markers))
    prose_ratio = prose_hits / len(cell_texts)

    # 2열이고 양쪽 모두 본문 폭이며 표시 폭이 길거나 산문 어미가 많고 수치/코드가 드물면 2단 본문
    if n_cols == 2 and wide_cols >= 2 and avg_disp_len >= 26.0 and num_ratio < 0.25:
        return True
    if n_cols == 2 and wide_cols >= 2 and prose_ratio >= 0.30 and num_ratio < 0.20:
        return True
    # 2열인데 셀 평균 표시 폭이 매우 길면(문장) 본문으로 판단
    if n_cols == 2 and avg_disp_len >= 36.0 and num_ratio < 0.25:
        return True
    # 저자·소속·이메일 그리드나 수치/코드가 전혀 없는 2열 짧은 텍스트 나열은 무선 표에서 제외
    email_hits = sum(1 for t in cell_texts if "@" in t)
    if email_hits >= 2 and num_ratio < 0.2:
        return True
    return n_cols == 2 and num_ratio == 0.0 and len(rows) <= 6


def _detect_borderless_tables(
    page: RawPageData,
    excluded_span_indices: set[int],
    existing_boxes: list[tuple[float, float, float, float]],
    next_index: int,
) -> tuple[list[ExtractedTable], set[int], list[tuple[float, float, float, float]]]:
    """테두리 선 없이 공백 정렬로 구성된 무선 표를 탐지한다."""
    remaining_indices = [i for i in range(len(page.spans)) if i not in excluded_span_indices]
    if len(remaining_indices) < 6:
        return [], set(), []

    # 인덱스 매핑을 보존하며 시각적 행 구성
    indexed_spans = sorted(
        [(i, page.spans[i]) for i in remaining_indices],
        key=lambda pair: (pair[1].y_center, pair[1].x0),
    )
    row_groups: list[list[tuple[int, RawTextSpan]]] = []
    for idx, sp in indexed_spans:
        if not row_groups:
            row_groups.append([(idx, sp)])
            continue
        cur = row_groups[-1]
        ref_y = sum(s.y_center for _, s in cur) / len(cur)
        ref_h = sum(s.font_size for _, s in cur) / len(cur)
        if abs(sp.y_center - ref_y) <= max(4.0, ref_h * 0.55):
            cur.append((idx, sp))
        else:
            row_groups.append([(idx, sp)])
    for grp in row_groups:
        grp.sort(key=lambda pair: pair[1].x0)

    # 연속된 다중 세그먼트(>=2열) 행 구간 추출
    runs: list[list[list[tuple[int, RawTextSpan]]]] = []
    cur_run: list[list[tuple[int, RawTextSpan]]] = []
    for grp in row_groups:
        spans_only = [sp for _, sp in grp]
        segs = _split_row_segments(spans_only)
        if len(segs) >= 2:
            if cur_run:
                prev_bot = max(sp.bottom for _, sp in cur_run[-1])
                cur_top = min(sp.top for _, sp in grp)
                avg_h = sum(sp.font_size for _, sp in grp) / len(grp)
                if cur_top - prev_bot > max(28.0, avg_h * 2.8):
                    if len(cur_run) >= 3:
                        runs.append(cur_run)
                    cur_run = [grp]
                    continue
            cur_run.append(grp)
        else:
            if len(cur_run) >= 3:
                runs.append(cur_run)
            cur_run = []
    if len(cur_run) >= 3:
        runs.append(cur_run)

    tables: list[ExtractedTable] = []
    used_spans: set[int] = set()
    used_boxes: list[tuple[float, float, float, float]] = []

    for run in runs:
        rows_spans = [[sp for _, sp in grp] for grp in run]
        all_spans = [sp for row in rows_spans for sp in row]
        x0 = min(sp.x0 for sp in all_spans)
        x1 = max(sp.x1 for sp in all_spans)
        top = min(sp.top for sp in all_spans)
        bot = max(sp.bottom for sp in all_spans)

        col_intervals = _infer_columns_from_rows(rows_spans, x0, x1)
        if len(col_intervals) < 2:
            continue
        if _is_likely_multi_column_prose(rows_spans, col_intervals, page.width):
            continue

        # 채움률 검증(빈 셀이 과반이면 우연한 정렬일 가능성이 큼)
        total_slots = len(rows_spans) * len(col_intervals)
        filled_slots = 0
        for row in rows_spans:
            hit_cols = {
                min(
                    range(len(col_intervals)),
                    key=lambda ci: abs(
                        (col_intervals[ci][0] + col_intervals[ci][1]) / 2.0 - sp.x_center
                    ),
                )
                for sp in row
            }
            filled_slots += len(hit_cols)
        fill_ratio = filled_slots / max(1, total_slots)
        if fill_ratio < 0.6:
            continue

        tbox = (x0, top, x1, bot)
        if any(bbox_iou(tbox, b) > 0.25 for b in existing_boxes + used_boxes):
            continue

        table_id = f"p{page.page:03d}_t{next_index + len(tables):02d}"
        tables.append(
            _build_table_from_rows_and_cols(
                page,
                rows_spans,
                col_intervals,
                tbox,
                table_id=table_id,
                strategy=ParseStrategy.BORDERLESS_TABLE,
                confidence=round(min(0.86, 0.68 + fill_ratio * 0.2), 2),
                reason=(
                    f"테두리선 없이 {len(rows_spans)}행x{len(col_intervals)}열 "
                    f"공백 정렬 패턴(셀 채움률 {fill_ratio:.0%})을 감지해 무선 표로 추출함"
                ),
            )
        )
        for grp in run:
            for idx, _ in grp:
                used_spans.add(idx)
        used_boxes.append(tbox)

    return tables, used_spans, used_boxes


def extract_ruled_tables(
    page: RawPageData,
    *,
    excluded_span_indices: set[int] | None = None,
) -> TableDetectionResult:
    """한 페이지에서 명시적 선 기반 표(격자형 + 가로선형)만 우선 추출한다."""
    excluded = set(excluded_span_indices or set())
    h_segs, v_segs = _collect_axis_segments(page)

    all_tables: list[ExtractedTable] = []
    consumed_spans: set[int] = set()
    consumed_boxes: list[tuple[float, float, float, float]] = []

    ruled_tables, ruled_spans, ruled_boxes = _detect_ruled_grid_tables(
        page, h_segs, v_segs, excluded, next_index=1
    )
    all_tables.extend(ruled_tables)
    consumed_spans.update(ruled_spans)
    consumed_boxes.extend(ruled_boxes)

    h_tables, h_spans, h_boxes = _detect_horizontal_ruled_tables(
        page,
        h_segs,
        excluded | consumed_spans,
        consumed_boxes,
        next_index=len(all_tables) + 1,
    )
    all_tables.extend(h_tables)
    consumed_spans.update(h_spans)
    consumed_boxes.extend(h_boxes)

    all_tables.sort(key=lambda t: (t.bbox.top, t.bbox.x0))
    return TableDetectionResult(
        tables=all_tables,
        consumed_span_indices=frozenset(consumed_spans),
        consumed_table_boxes=tuple(consumed_boxes),
    )


def extract_borderless_tables(
    page: RawPageData,
    *,
    excluded_span_indices: set[int] | None = None,
    existing_boxes: tuple[tuple[float, float, float, float], ...] = (),
    next_index: int = 1,
) -> TableDetectionResult:
    """선 기반 표·시각 영역이 점유하지 않은 스팬에서 무선(공백 정렬) 표를 탐지한다."""
    b_tables, b_spans, b_boxes = _detect_borderless_tables(
        page,
        set(excluded_span_indices or set()),
        list(existing_boxes),
        next_index=next_index,
    )
    b_tables.sort(key=lambda t: (t.bbox.top, t.bbox.x0))
    return TableDetectionResult(
        tables=b_tables,
        consumed_span_indices=frozenset(b_spans),
        consumed_table_boxes=tuple(b_boxes),
    )


def extract_page_tables(
    page: RawPageData,
    *,
    excluded_span_indices: set[int] | None = None,
    detect_borderless: bool = True,
) -> TableDetectionResult:
    """한 페이지에서 격자형 → 가로선형 → 무선형 순으로 표를 탐지·추출한다."""
    ruled = extract_ruled_tables(page, excluded_span_indices=excluded_span_indices)
    if not detect_borderless:
        return ruled
    borderless = extract_borderless_tables(
        page,
        excluded_span_indices=set(excluded_span_indices or set())
        | set(ruled.consumed_span_indices),
        existing_boxes=ruled.consumed_table_boxes,
        next_index=len(ruled.tables) + 1,
    )
    combined = sorted([*ruled.tables, *borderless.tables], key=lambda t: (t.bbox.top, t.bbox.x0))
    return TableDetectionResult(
        tables=combined,
        consumed_span_indices=ruled.consumed_span_indices | borderless.consumed_span_indices,
        consumed_table_boxes=ruled.consumed_table_boxes + borderless.consumed_table_boxes,
    )
