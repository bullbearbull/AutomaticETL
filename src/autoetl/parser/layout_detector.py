"""스캔/이미지 페이지 레이아웃 객체 탐지기(Object Detection) 확장 모듈.

디지털 PDF는 콘텐츠 스트림 기하로 표·도식·본문을 결정론적으로 분리하지만,
순수 스캔 PDF는 통짜 이미지 1장(`SCANNED_PAGE`)으로 들어온다. 이 모듈은
스캔 페이지 이미지를 제목·본문·표·차트/그림·수식 영역으로 세분화해 각 영역별
크롭과 OCR/VLM 프롬프트(`Table Recognition:`·`Chart Recognition:`·`OCR:` 등)가
정확히 분기되도록 플러그인형 `PageLayoutDetector` 구현체 3종을 제공한다.

구현체:
1. `HeuristicRasterLayoutDetector` — 추가 의존성 0개. 순수 Python PNG 디코딩 +
   수평/수직 투영·격자선·색채/밀도 분석으로 스캔 페이지의 제목·문단·격자표·그림/차트를 분리한다.
2. `OnnxDocLayoutDetector` — 선택 의존성 `onnxruntime` + `numpy`(지연 import).
   허용형 Apache-2.0 라이선스의 `PP-DocLayoutV2`/`V3`·`PP-DocLayout_plus-L`·`DocLayNet`
   ONNX 체크포인트를 로컬에서 실행한다(AGPL 계열 의존성 없음).
3. `HttpLayoutDetector` — 표준 라이브러리 `urllib` 기반 로컬/사내 레이아웃 탐지 서비스 연동.
   `validate_vision_endpoint`의 TrustZone 정책을 그대로 적용해 기본값은 루프백·등록 사내만 허용한다.
"""

from __future__ import annotations

import base64
import importlib
import json
import os
import re
import urllib.error
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from autoetl.parser.adapters import (
    DetectedLayoutBox,
    LayoutDetectionInput,
    NoopLayoutDetector,
    PageLayoutDetector,
    ParserInputError,
)
from autoetl.parser.contracts import BBox, ElementKind, ParseStrategy, VisualRegion
from autoetl.parser.image_io import DecodedImage, decode_png
from autoetl.parser.vision import (
    _detect_image_mime,
    _open_json_request,
    validate_vision_endpoint,
)
from autoetl.security.levels import TrustZone


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _make_bbox(
    x0: float,
    top: float,
    x1: float,
    bottom: float,
    page_width: float,
    page_height: float,
) -> BBox:
    pw = max(1.0, page_width)
    ph = max(1.0, page_height)
    cx0 = _clamp(min(x0, x1), 0.0, pw)
    ctop = _clamp(min(top, bottom), 0.0, ph)
    cx1 = _clamp(max(x0, x1), cx0, pw)
    cbot = _clamp(max(top, bottom), ctop, ph)
    return BBox(
        x0=round(cx0, 2),
        top=round(ctop, 2),
        x1=round(cx1, 2),
        bottom=round(cbot, 2),
        norm_x0=round(_clamp(cx0 / pw, 0.0, 1.0), 4),
        norm_top=round(_clamp(ctop / ph, 0.0, 1.0), 4),
        norm_x1=round(_clamp(cx1 / pw, 0.0, 1.0), 4),
        norm_bottom=round(_clamp(cbot / ph, 0.0, 1.0), 4),
    )


DEFAULT_HTTP_LAYOUT_BASE_URL = "http://127.0.0.1:8001"

# Apache-2.0 공식 PP-DocLayoutV2 / PP-DocLayoutV3 25클래스 라벨 순서(0..24)
PP_DOCLAYOUT_V2_LABELS: tuple[str, ...] = (
    "abstract",
    "algorithm",
    "aside_text",
    "chart",
    "content",
    "display_formula",
    "doc_title",
    "figure_title",
    "footer",
    "footer_image",
    "footnote",
    "formula_number",
    "header",
    "header_image",
    "image",
    "inline_formula",
    "number",
    "paragraph_title",
    "reference",
    "reference_content",
    "seal",
    "table",
    "text",
    "vertical_text",
    "vision_footnote",
)

# Apache-2.0 공식 PP-DocLayout_plus-L 20클래스 라벨 순서(0..19)
PP_DOCLAYOUT_PLUS_L_LABELS: tuple[str, ...] = (
    "paragraph_title",
    "image",
    "text",
    "number",
    "abstract",
    "content",
    "figure_title",
    "formula",
    "table",
    "reference",
    "doc_title",
    "footnote",
    "header",
    "algorithm",
    "footer",
    "seal",
    "chart",
    "formula_number",
    "aside_text",
    "reference_content",
)

# 표준 DocLayNet 11클래스 라벨 순서
DOCLAYNET_LABELS: tuple[str, ...] = (
    "Caption",
    "Footnote",
    "Formula",
    "List-item",
    "Page-footer",
    "Page-header",
    "Picture",
    "Section-header",
    "Table",
    "Text",
    "Title",
)

# 표준 PubLayNet 5클래스 라벨 순서
PUBLAYNET_LABELS: tuple[str, ...] = (
    "text",
    "title",
    "list",
    "table",
    "figure",
)

LAYOUT_LABEL_PRESETS: dict[str, tuple[str, ...]] = {
    "pp-doclayout-v2": PP_DOCLAYOUT_V2_LABELS,
    "pp-doclayout-v3": PP_DOCLAYOUT_V2_LABELS,
    "pp-doclayout-plus-l": PP_DOCLAYOUT_PLUS_L_LABELS,
    "doclaynet": DOCLAYNET_LABELS,
    "publaynet": PUBLAYNET_LABELS,
}

_TABLE_LABELS = frozenset({"table", "tab", "table_body", "table_region"})
_FIGURE_LABELS = frozenset(
    {
        "chart",
        "figure",
        "picture",
        "image",
        "graphic",
        "diagram",
        "plot",
        "seal",
        "stamp",
        "figure_image",
    }
)
_VECTOR_LABELS = frozenset({"vector_diagram", "flowchart"})
_FORMULA_LABELS = frozenset(
    {
        "formula",
        "display_formula",
        "inline_formula",
        "equation",
        "math",
        "algorithm",
        "code",
        "formula_or_code",
    }
)
_HEADING_LABELS = frozenset(
    {
        "title",
        "doc_title",
        "paragraph_title",
        "section_header",
        "heading",
        "sub_title",
        "subtitle",
    }
)
_CAPTION_LABELS = frozenset(
    {
        "caption",
        "figure_title",
        "table_title",
        "chart_title",
        "figure_caption",
        "table_caption",
    }
)
_HEADER_FOOTER_LABELS = frozenset(
    {
        "header",
        "footer",
        "header_image",
        "footer_image",
        "page_header",
        "page_footer",
        "number",
        "page_number",
        "formula_number",
        "header_footer",
        "running_header",
        "running_footer",
    }
)
_LIST_LABELS = frozenset({"list", "list_item", "bullet"})


def normalize_layout_label(raw_label: str) -> tuple[ElementKind, ParseStrategy]:
    """레이아웃 모델 클래스 라벨을 표준 `(ElementKind, ParseStrategy)`로 매핑한다."""
    norm = re.sub(r"[\s\-]+", "_", raw_label.strip().lower()).strip("_")

    if norm in _TABLE_LABELS:
        return ElementKind.TABLE, ParseStrategy.VLM_REGION
    if norm in _VECTOR_LABELS:
        return ElementKind.VECTOR_DIAGRAM, ParseStrategy.VLM_REGION
    if norm in _FIGURE_LABELS:
        return ElementKind.FIGURE_IMAGE, ParseStrategy.VLM_REGION
    if norm in _FORMULA_LABELS:
        return ElementKind.FORMULA_OR_CODE, ParseStrategy.VLM_REGION
    if norm in _HEADING_LABELS:
        return ElementKind.HEADING, ParseStrategy.EMBEDDED_IMAGE_OCR
    if norm in _CAPTION_LABELS:
        return ElementKind.CAPTION, ParseStrategy.EMBEDDED_IMAGE_OCR
    if norm in _HEADER_FOOTER_LABELS:
        return ElementKind.HEADER_FOOTER, ParseStrategy.SKIP_DECORATIVE
    if norm in _LIST_LABELS:
        return ElementKind.LIST_ITEM, ParseStrategy.EMBEDDED_IMAGE_OCR
    return ElementKind.PARAGRAPH, ParseStrategy.EMBEDDED_IMAGE_OCR


def _box_area(box: tuple[float, float, float, float]) -> float:
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def _box_intersection(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> float:
    ix0 = max(a[0], b[0])
    iy0 = max(a[1], b[1])
    ix1 = min(a[2], b[2])
    iy1 = min(a[3], b[3])
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    return (ix1 - ix0) * (iy1 - iy0)


def _box_iou_px(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> float:
    inter = _box_intersection(a, b)
    if inter <= 0.0:
        return 0.0
    union = _box_area(a) + _box_area(b) - inter
    return inter / union if union > 0.0 else 0.0


def apply_layout_nms(
    boxes: Sequence[DetectedLayoutBox],
    *,
    iou_threshold: float = 0.45,
    containment_threshold: float = 0.85,
) -> list[DetectedLayoutBox]:
    """중복·중첩 검출 박스를 NMS 및 포함 관계(containment) 규칙으로 정리한다.

    - 같은 유형끼리 IoU >= `iou_threshold`이면 점수가 높은(동점이면 면적이 큰) 박스만 유지한다.
    - 서로 다른 유형이라도 IoU >= 0.75로 거의 동일한 위치라면 점수가 높은 쪽만 유지한다.
    - 표(`TABLE`)나 그림/차트(`FIGURE_IMAGE`/`VECTOR_DIAGRAM`) 안에 85% 이상 포함되는
      내부 텍스트/셀 중복 박스는 제거해 이중 OCR을 막는다.
    """
    if not boxes:
        return []

    ordered = sorted(
        boxes,
        key=lambda b: (b.score, _box_area(b.bbox_px)),
        reverse=True,
    )
    container_kinds = {
        ElementKind.TABLE,
        ElementKind.FIGURE_IMAGE,
        ElementKind.VECTOR_DIAGRAM,
    }
    inner_suppressible_kinds = {
        ElementKind.PARAGRAPH,
        ElementKind.HEADING,
        ElementKind.LIST_ITEM,
        ElementKind.CAPTION,
        ElementKind.FORMULA_OR_CODE,
    }

    kept: list[DetectedLayoutBox] = []
    for cand in ordered:
        cand_area = _box_area(cand.bbox_px)
        if cand_area <= 0.0:
            continue
        suppressed = False
        for prev in kept:
            iou = _box_iou_px(cand.bbox_px, prev.bbox_px)
            if cand.element_kind == prev.element_kind and iou >= iou_threshold:
                suppressed = True
                break
            if iou >= 0.75:
                suppressed = True
                break
            inter = _box_intersection(cand.bbox_px, prev.bbox_px)
            prev_area = _box_area(prev.bbox_px)
            if inter > 0.0 and prev_area >= cand_area:
                contain = inter / cand_area
                if contain >= containment_threshold and (
                    cand.element_kind == prev.element_kind
                    or (
                        prev.element_kind in container_kinds
                        and cand.element_kind in inner_suppressible_kinds
                    )
                ):
                    suppressed = True
                    break
        if not suppressed:
            kept.append(cand)
    return kept


def sort_layout_boxes_reading_order(
    boxes: Sequence[DetectedLayoutBox],
    *,
    page_width_px: float,
) -> list[DetectedLayoutBox]:
    """검출 박스를 읽기 순서(2단 여백 인식 XY-Cut 포함)로 정렬해 `reading_order`를 부여한다."""
    if not boxes:
        return []

    # 모델/서비스가 명시적 reading_order(1 이상)를 제공했으면 그 순서를 존중한다.
    if all(b.reading_order > 0 for b in boxes):
        ordered = sorted(boxes, key=lambda b: (b.reading_order, b.bbox_px[1], b.bbox_px[0]))
        return [
            DetectedLayoutBox(
                raw_label=b.raw_label,
                element_kind=b.element_kind,
                recommended_strategy=b.recommended_strategy,
                bbox_px=b.bbox_px,
                score=b.score,
                reading_order=idx,
                reason=b.reason,
            )
            for idx, b in enumerate(ordered, 1)
        ]

    # 2단 레이아웃 여부 판단: 중앙선(50%)을 가로지르지 않는 좌·우 반폭 박스가 각 2개 이상
    mid_x = page_width_px * 0.5
    margin = page_width_px * 0.03
    left_narrow: list[DetectedLayoutBox] = []
    right_narrow: list[DetectedLayoutBox] = []
    full_width: list[DetectedLayoutBox] = []

    for b in boxes:
        x0, _y0, x1, _y1 = b.bbox_px
        bw = x1 - x0
        if x1 <= mid_x + margin and bw <= page_width_px * 0.56:
            left_narrow.append(b)
        elif x0 >= mid_x - margin and bw <= page_width_px * 0.56:
            right_narrow.append(b)
        else:
            full_width.append(b)

    is_two_col = len(left_narrow) >= 2 and len(right_narrow) >= 2

    if not is_two_col:
        # 단일 컬럼: y0 기준 정렬(거의 같은 줄이면 x0 오름차순)
        ordered = sorted(boxes, key=lambda b: (round(b.bbox_px[1] / 12.0), b.bbox_px[0]))
    else:
        full_sorted = sorted(full_width, key=lambda b: (b.bbox_px[1], b.bbox_px[0]))
        rem_left = sorted(left_narrow, key=lambda b: (b.bbox_px[1], b.bbox_px[0]))
        rem_right = sorted(right_narrow, key=lambda b: (b.bbox_px[1], b.bbox_px[0]))
        ordered = []
        for fb in full_sorted:
            fy_top = fb.bbox_px[1]
            above_l = [b for b in rem_left if (b.bbox_px[1] + b.bbox_px[3]) * 0.5 < fy_top]
            above_r = [b for b in rem_right if (b.bbox_px[1] + b.bbox_px[3]) * 0.5 < fy_top]
            rem_left = [b for b in rem_left if b not in above_l]
            rem_right = [b for b in rem_right if b not in above_r]
            ordered.extend(above_l)
            ordered.extend(above_r)
            ordered.append(fb)
        ordered.extend(rem_left)
        ordered.extend(rem_right)

    return [
        DetectedLayoutBox(
            raw_label=b.raw_label,
            element_kind=b.element_kind,
            recommended_strategy=b.recommended_strategy,
            bbox_px=b.bbox_px,
            score=b.score,
            reading_order=idx,
            reason=b.reason,
        )
        for idx, b in enumerate(ordered, 1)
    ]


def boxes_to_visual_regions(
    boxes: Sequence[DetectedLayoutBox],
    *,
    page: int,
    page_width_pt: float,
    page_height_pt: float,
    image_width_px: int,
    image_height_px: int,
    detector_name: str,
    skip_header_footer: bool = True,
    start_index: int = 1,
) -> list[tuple[VisualRegion, tuple[int, int, int, int]]]:
    """검출 박스 목록을 `(VisualRegion, pixel_crop_rect)` 목록으로 변환한다."""
    if image_width_px <= 0 or image_height_px <= 0 or page_width_pt <= 0.0 or page_height_pt <= 0.0:
        return []

    sx = page_width_pt / float(image_width_px)
    sy = page_height_pt / float(image_height_px)
    high_priority_kinds = {
        ElementKind.TABLE,
        ElementKind.FIGURE_IMAGE,
        ElementKind.VECTOR_DIAGRAM,
        ElementKind.FORMULA_OR_CODE,
        ElementKind.SCANNED_PAGE,
    }

    results: list[tuple[VisualRegion, tuple[int, int, int, int]]] = []
    next_idx = start_index
    for box in boxes:
        if skip_header_footer and (
            box.element_kind == ElementKind.HEADER_FOOTER
            or box.recommended_strategy == ParseStrategy.SKIP_DECORATIVE
        ):
            continue

        px0 = max(0, min(round(box.bbox_px[0]), image_width_px - 1))
        py0 = max(0, min(round(box.bbox_px[1]), image_height_px - 1))
        px1 = max(px0 + 1, min(round(box.bbox_px[2]), image_width_px))
        py1 = max(py0 + 1, min(round(box.bbox_px[3]), image_height_px))

        x0_pt = _clamp(px0 * sx, 0.0, page_width_pt)
        top_pt = _clamp(py0 * sy, 0.0, page_height_pt)
        x1_pt = _clamp(px1 * sx, 0.0, page_width_pt)
        bot_pt = _clamp(py1 * sy, 0.0, page_height_pt)
        if (x1_pt - x0_pt) < 10.0 or (bot_pt - top_pt) < 8.0:
            continue

        region_id = f"p{page:03d}_v{next_idx:02d}"
        next_idx += 1
        conf = round(_clamp(box.score, 0.0, 1.0), 3)
        base_reason = (
            box.reason
            if box.reason
            else (
                f"레이아웃 탐지({detector_name}): `{box.raw_label}` "
                f"(신뢰도 {conf:.2f}) → {box.element_kind.value}/{box.recommended_strategy.value}"
            )
        )
        region = VisualRegion(
            region_id=region_id,
            page=page,
            element_kind=box.element_kind,
            recommended_strategy=box.recommended_strategy,
            bbox=_make_bbox(x0_pt, top_pt, x1_pt, bot_pt, page_width_pt, page_height_pt),
            reason=base_reason,
            confidence=conf,
            priority="high" if box.element_kind in high_priority_kinds else "medium",
            image_width_px=px1 - px0,
            image_height_px=py1 - py0,
        )
        results.append((region, (px0, py0, px1, py1)))
    return results


# ---------------------------------------------------------------------------
# 1. 순수 Python 무의존성 래스터 레이아웃 휴리스틱 탐지기
# ---------------------------------------------------------------------------


def _build_downsampled_masks(
    decoded: DecodedImage,
    *,
    max_dim: int = 320,
    ink_threshold: int = 215,
    color_diff_threshold: int = 30,
) -> tuple[list[bytearray], list[bytearray], int, int, int]:
    """디코딩된 이미지를 `max_dim` 이하 작업 격자로 축소하며 잉크·색채 마스크를 만든다.

    각 셀에서 가장 어두운 픽셀을 반영(min-pool)해 1px 표 격자선이 축소 과정에 사라지지
    않도록 보존하고, 빠른 처리를 위해 셀 내부를 최대 2단계 샘플링으로 스캔한다.
    """
    w = decoded.width
    h = decoded.height
    step = max(1, max(w, h) // max_dim)
    gw = max(1, (w + step - 1) // step)
    gh = max(1, (h + step - 1) // step)
    ink = [bytearray(gw) for _ in range(gh)]
    color = [bytearray(gw) for _ in range(gh)]

    ch = decoded.channels
    px = decoded.pixels
    sub = max(1, step // 2) if step >= 4 else 1

    if ch == 1 or ch == 2:
        for gy in range(gh):
            y_start = gy * step
            y_end = min(h, y_start + step)
            row_ink = ink[gy]
            for y in range(y_start, y_end, sub):
                row_off = y * w * ch
                for gx in range(gw):
                    if row_ink[gx]:
                        continue
                    x_start = gx * step
                    x_end = min(w, x_start + step)
                    for x in range(x_start, x_end, sub):
                        if px[row_off + x * ch] < ink_threshold:
                            row_ink[gx] = 1
                            break
        return ink, color, gw, gh, step

    # RGB (3) 또는 RGBA (4)
    for gy in range(gh):
        y_start = gy * step
        y_end = min(h, y_start + step)
        row_ink = ink[gy]
        row_col = color[gy]
        for y in range(y_start, y_end, sub):
            row_off = y * w * ch
            for gx in range(gw):
                if row_ink[gx] and row_col[gx]:
                    continue
                x_start = gx * step
                x_end = min(w, x_start + step)
                for x in range(x_start, x_end, sub):
                    off = row_off + x * ch
                    r = px[off]
                    g = px[off + 1]
                    b = px[off + 2]
                    if not row_ink[gx] and ((r * 77 + g * 150 + b * 29) >> 8) < ink_threshold:
                        row_ink[gx] = 1
                    if not row_col[gx] and (max(r, g, b) - min(r, g, b)) > color_diff_threshold:
                        row_col[gx] = 1
                    if row_ink[gx] and row_col[gx]:
                        break
    return ink, color, gw, gh, step


def _longest_run_in_row(row: bytearray, x0: int, x1: int) -> int:
    best = 0
    cur = 0
    for x in range(x0, x1):
        if row[x]:
            cur += 1
            if cur > best:
                best = cur
        else:
            cur = 0
    return best


def _longest_run_in_col(ink: list[bytearray], x: int, y0: int, y1: int) -> int:
    best = 0
    cur = 0
    for y in range(y0, y1):
        if ink[y][x]:
            cur += 1
            if cur > best:
                best = cur
        else:
            cur = 0
    return best


def _count_rule_clusters(flags: Sequence[bool], *, max_thickness: int) -> int:
    """연속 True 구간 중 두께가 `max_thickness` 이하인 얇은 직선(격자선) 묶음 개수를 센다."""
    count = 0
    run_len = 0
    for flag in flags:
        if flag:
            run_len += 1
        elif run_len > 0:
            if run_len <= max_thickness:
                count += 1
            run_len = 0
    if 0 < run_len <= max_thickness:
        count += 1
    return count


def _tight_grid_bbox(
    ink: list[bytearray],
    x0: int,
    y0: int,
    x1: int,
    y1: int,
) -> tuple[int, int, int, int] | None:
    min_x = x1
    max_x = -1
    min_y = y1
    max_y = -1
    for y in range(y0, y1):
        row = ink[y]
        for x in range(x0, x1):
            if row[x]:
                if x < min_x:
                    min_x = x
                if x > max_x:
                    max_x = x
                if y < min_y:
                    min_y = y
                if y > max_y:
                    max_y = y
    if max_x < min_x or max_y < min_y:
        return None
    return (min_x, min_y, max_x + 1, max_y + 1)


def _split_vertical_columns(
    ink: list[bytearray],
    box: tuple[int, int, int, int],
    *,
    gw: int,
    gh: int,
) -> list[tuple[int, int, int, int]]:
    """영역 중앙부에 깔끔한 세로 공백 여백(gutter)이 있으면 좌·우 블록으로 분할한다."""
    x0, y0, x1, y1 = box
    rw = x1 - x0
    rh = y1 - y0
    if rw < int(gw * 0.48) or rh < int(gh * 0.04):
        return [box]

    # 가로 격자선이 있는 표/구분 영역은 세로 분할하지 않는다.
    for y in range(y0, y1):
        if _longest_run_in_row(ink[y], x0, x1) >= int(rw * 0.65):
            return [box]

    min_gutter = max(4, int(gw * 0.025))
    search_start = x0 + int(rw * 0.22)
    search_end = x0 + int(rw * 0.78)
    best_run_start = -1
    best_run_len = 0
    cur_start = -1

    for x in range(search_start, search_end):
        col_has_ink = any(ink[y][x] for y in range(y0, y1))
        if not col_has_ink:
            if cur_start < 0:
                cur_start = x
        else:
            if cur_start >= 0:
                run_len = x - cur_start
                if run_len > best_run_len:
                    best_run_len = run_len
                    best_run_start = cur_start
                cur_start = -1
    if cur_start >= 0:
        run_len = search_end - cur_start
        if run_len > best_run_len:
            best_run_len = run_len
            best_run_start = cur_start

    if best_run_len < min_gutter or best_run_start < 0:
        return [box]

    split_x = best_run_start + best_run_len // 2
    left_box = _tight_grid_bbox(ink, x0, y0, split_x, y1)
    right_box = _tight_grid_bbox(ink, split_x, y0, x1, y1)
    parts: list[tuple[int, int, int, int]] = []
    if left_box is not None and (left_box[2] - left_box[0]) >= max(4, int(gw * 0.06)):
        parts.append(left_box)
    if right_box is not None and (right_box[2] - right_box[0]) >= max(4, int(gw * 0.06)):
        parts.append(right_box)
    return parts if len(parts) == 2 else [box]


class HeuristicRasterLayoutDetector:
    """추가 ML 의존성 없이 PNG 스캔 페이지의 제목·본문·표·차트/그림을 분리하는 탐지기.

    동작 원리:
    1. 순수 Python `decode_png`로 래스터를 디코딩하고 최대 320px 격자로 축소(min-pool로 1px 선 보존)
    2. 수평 투영으로 잉크 줄(run) 추출 → 표/그림 같은 대형 블록과 줄 높이 차이를 존중해 문단 병합
    3. 가로선이 없는 넓은 블록에 세로 공백 여백(gutter)이 있으면 2단으로 재분할
    4. 각 블록의 가로/세로 격자선 수·잉크 밀도·색채 비율·줄 수로 `table`·`chart`/`figure`·
       `doc_title`/`paragraph_title`·`text`를 분류한다.
    """

    def __init__(
        self,
        *,
        score_threshold: float = 0.4,
        nms_iou: float = 0.45,
        max_grid_dim: int = 320,
    ) -> None:
        self._score_threshold = _clamp(float(score_threshold), 0.0, 1.0)
        self._nms_iou = _clamp(float(nms_iou), 0.05, 0.95)
        self._max_grid_dim = max(120, int(max_grid_dim))

    @property
    def name(self) -> str:
        return "heuristic"

    @property
    def available(self) -> bool:
        return True

    def detect(self, inp: LayoutDetectionInput) -> list[DetectedLayoutBox]:
        decoded = decode_png(inp.image_bytes)
        if decoded is None:
            return []

        ink, color, gw, gh, step = _build_downsampled_masks(decoded, max_dim=self._max_grid_dim)

        # 1. 수평 투영으로 연속 잉크 행 구간(run) 수집
        min_row_ink = max(1, int(gw * 0.004))
        raw_runs: list[tuple[int, int]] = []
        in_run = False
        run_y0 = 0
        for y in range(gh):
            cnt = sum(ink[y])
            if cnt >= min_row_ink:
                if not in_run:
                    in_run = True
                    run_y0 = y
            elif in_run:
                raw_runs.append((run_y0, y))
                in_run = False
        if in_run:
            raw_runs.append((run_y0, gh))

        if not raw_runs:
            return []

        # 각 잉크 행 구간의 tight bbox와 성질(대형 블록 vs 텍스트 줄 vs 얇은 구분선) 계산
        run_boxes: list[tuple[int, int, int, int]] = []
        for ry0, ry1 in raw_runs:
            tb = _tight_grid_bbox(ink, 0, ry0, gw, ry1)
            if tb is not None:
                run_boxes.append(tb)

        if not run_boxes:
            return []

        def _is_large_block(b: tuple[int, int, int, int]) -> bool:
            bh = b[3] - b[1]
            bw = b[2] - b[0]
            if bh >= max(8, int(gh * 0.055)):
                return True
            if bw >= int(gw * 0.14) and bh >= max(6, int(gh * 0.04)):
                col_cells = sum(color[y][x] for y in range(b[1], b[3]) for x in range(b[0], b[2]))
                if col_cells / max(1, bw * bh) >= 0.04:
                    return True
            return False

        def _is_thin_hrule(b: tuple[int, int, int, int]) -> bool:
            bw = b[2] - b[0]
            bh = b[3] - b[1]
            if bh > max(2, int(gh * 0.012)) or bw < int(gw * 0.25):
                return False
            best_run = max(_longest_run_in_row(ink[y], b[0], b[2]) for y in range(b[1], b[3]))
            return best_run >= int(bw * 0.72)

        # 2. 인접 텍스트 줄을 문단으로 병합하되 대형 블록(표·그림)·제목 크기 차이·구분선은 분리
        # booktabs(가로선만 있는 표)는 상단선~하단선 사이를 묶기 위해 먼저 얇은 가로선 그룹 탐지
        max_para_gap = max(3, int(gh * 0.019))
        booktabs_gap = max(6, int(gh * 0.035))

        merged_bands: list[tuple[int, int, int, int]] = []
        i = 0
        while i < len(run_boxes):
            cur = run_boxes[i]

            # booktabs(가로선 3개 이상으로 감싼 표) 패턴 우선 병합
            if _is_thin_hrule(cur):
                rule_indices = [i]
                j = i + 1
                while j < len(run_boxes):
                    prev_b = run_boxes[j - 1]
                    next_b = run_boxes[j]
                    gap = next_b[1] - prev_b[3]
                    if gap > booktabs_gap or _is_large_block(next_b):
                        break
                    # 가로선과 비슷한 x 범위에 있는 행만 포함
                    if next_b[2] < cur[0] or next_b[0] > cur[2]:
                        break
                    if _is_thin_hrule(next_b):
                        rule_indices.append(j)
                    j += 1
                if len(rule_indices) >= 3:
                    end_idx = rule_indices[-1]
                    grp = run_boxes[i : end_idx + 1]
                    merged_bands.append(
                        (
                            min(b[0] for b in grp),
                            min(b[1] for b in grp),
                            max(b[2] for b in grp),
                            max(b[3] for b in grp),
                        )
                    )
                    i = end_idx + 1
                    continue

            if _is_large_block(cur):
                merged_bands.append(cur)
                i += 1
                continue

            # 일반 텍스트 줄: 비슷한 높이·근접 간격·가로 겹침이 있는 후속 텍스트 줄과 병합
            grp = [cur]
            cur_line_h = cur[3] - cur[1]
            j = i + 1
            while j < len(run_boxes):
                nxt = run_boxes[j]
                if _is_large_block(nxt) or _is_thin_hrule(nxt):
                    break
                gap = nxt[1] - grp[-1][3]
                if gap > max_para_gap:
                    break
                nxt_h = nxt[3] - nxt[1]
                ratio = max(cur_line_h, nxt_h) / max(1, min(cur_line_h, nxt_h))
                if ratio > 1.45:
                    break
                # 가로 범위가 전혀 겹치지 않으면 분리
                grp_x0 = min(b[0] for b in grp)
                grp_x1 = max(b[2] for b in grp)
                if min(grp_x1, nxt[2]) <= max(grp_x0, nxt[0]):
                    break
                grp.append(nxt)
                j += 1
            merged_bands.append(
                (
                    min(b[0] for b in grp),
                    min(b[1] for b in grp),
                    max(b[2] for b in grp),
                    max(b[3] for b in grp),
                )
            )
            i = j

        # 3. 각 밴드 내 2단 분할 체크
        region_boxes: list[tuple[int, int, int, int]] = []
        for band in merged_bands:
            region_boxes.extend(_split_vertical_columns(ink, band, gw=gw, gh=gh))

        # 본문 텍스트 줄 높이 중앙값 추정(제목 판정 기준)
        small_line_heights: list[int] = []
        for rb in run_boxes:
            rh = rb[3] - rb[1]
            if 2 <= rh <= max(6, int(gh * 0.04)):
                small_line_heights.append(rh)
        small_line_heights.sort()
        median_line_h = (
            float(small_line_heights[len(small_line_heights) // 2])
            if small_line_heights
            else max(3.0, gh * 0.015)
        )

        # 4. 각 후보 블록의 기하·격자선·색채·밀도 지표 계산 및 유형 분류
        detected: list[DetectedLayoutBox] = []
        pad_px = max(4.0, float(step) * 1.2)

        for gx0, gy0, gx1, gy1 in region_boxes:
            rw = gx1 - gx0
            rh = gy1 - gy0
            area = rw * rh
            if rw < max(4, int(gw * 0.05)) or rh < max(2, int(gh * 0.01)):
                continue

            ink_count = 0
            color_count = 0
            solid_rows = 0
            h_rule_flags: list[bool] = []
            non_empty_rows: list[bool] = []
            for y in range(gy0, gy1):
                row_i = ink[y]
                row_c = color[y]
                r_ink = sum(row_i[gx0:gx1])
                ink_count += r_ink
                color_count += sum(row_c[gx0:gx1])
                non_empty_rows.append(r_ink >= max(1, int(rw * 0.02)))
                if r_ink >= int(rw * 0.45):
                    solid_rows += 1
                longest_h = _longest_run_in_row(row_i, gx0, gx1)
                h_rule_flags.append(longest_h >= max(8, int(rw * 0.65)))

            v_rule_flags: list[bool] = []
            for x in range(gx0, gx1):
                longest_v = _longest_run_in_col(ink, x, gy0, gy1)
                v_rule_flags.append(longest_v >= max(6, int(rh * 0.55)))

            max_h_thick = max(3, int(gh * 0.014))
            max_v_thick = max(3, int(gw * 0.016))
            h_rules = _count_rule_clusters(h_rule_flags, max_thickness=max_h_thick)
            v_rules = _count_rule_clusters(v_rule_flags, max_thickness=max_v_thick)

            ink_density = ink_count / max(1, area)
            color_ratio = color_count / max(1, area)
            solid_row_ratio = solid_rows / max(1, rh)

            # 영역 내부 텍스트 줄(수평 잉크 밴드) 수와 평균 높이
            sub_line_heights: list[int] = []
            sub_len = 0
            for active in non_empty_rows:
                if active:
                    sub_len += 1
                elif sub_len > 0:
                    sub_line_heights.append(sub_len)
                    sub_len = 0
            if sub_len > 0:
                sub_line_heights.append(sub_len)
            line_cnt = len(sub_line_heights)
            avg_sub_h = (sum(sub_line_heights) / line_cnt) if line_cnt > 0 else float(rh)

            if ink_density < 0.015:
                continue

            raw_label: str
            score: float
            detail: str

            if h_rules >= 2 and v_rules >= 2 and rw >= int(gw * 0.16) and rh >= int(gh * 0.04):
                raw_label = "table"
                score = 0.88
                detail = f"격자선(가로 {h_rules}·세로 {v_rules})"
            elif (
                h_rules >= 3
                and rw >= int(gw * 0.28)
                and rh >= int(gh * 0.05)
                and color_ratio < 0.08
            ):
                raw_label = "table"
                score = 0.82
                detail = f"가로 구분선 {h_rules}개(booktabs형)"
            elif (
                rw >= int(gw * 0.14)
                and rh >= int(gh * 0.07)
                and (
                    color_ratio >= 0.035
                    or ink_density >= 0.30
                    or solid_row_ratio >= 0.28
                    or (h_rules == 1 and v_rules == 1)
                )
            ):
                raw_label = "chart" if (h_rules >= 1 or v_rules >= 1) else "figure"
                score = 0.85 if color_ratio >= 0.035 else 0.80
                detail = (
                    f"색채 비율 {color_ratio:.0%}, 잉크 밀도 {ink_density:.0%}"
                    if color_ratio >= 0.035
                    else f"고밀도 그래픽(밀도 {ink_density:.0%}, 축선 {h_rules}/{v_rules})"
                )
            elif (
                line_cnt <= 2
                and rh <= int(gh * 0.065)
                and (
                    avg_sub_h >= median_line_h * 1.25
                    or (gy0 <= int(gh * 0.16) and line_cnt == 1 and rw <= int(gw * 0.85))
                )
            ):
                raw_label = "doc_title" if gy0 <= int(gh * 0.16) else "paragraph_title"
                score = 0.78
                rel_h = avg_sub_h / max(1.0, median_line_h)
                detail = f"단독 제목형 줄({line_cnt}줄, 상대 높이 {rel_h:.2f}x)"
            else:
                raw_label = "text"
                score = 0.75
                detail = f"본문 텍스트 블록({line_cnt}줄, 밀도 {ink_density:.0%})"

            if score < self._score_threshold:
                continue

            kind, strat = normalize_layout_label(raw_label)
            x0_px = _clamp(gx0 * step - pad_px, 0.0, float(decoded.width))
            y0_px = _clamp(gy0 * step - pad_px, 0.0, float(decoded.height))
            x1_px = _clamp(gx1 * step + pad_px, 0.0, float(decoded.width))
            y1_px = _clamp(gy1 * step + pad_px, 0.0, float(decoded.height))
            detected.append(
                DetectedLayoutBox(
                    raw_label=raw_label,
                    element_kind=kind,
                    recommended_strategy=strat,
                    bbox_px=(x0_px, y0_px, x1_px, y1_px),
                    score=score,
                    reason=(
                        f"래스터 레이아웃 탐지(heuristic): `{raw_label}` "
                        f"({detail}, 신뢰도 {score:.2f})"
                    ),
                )
            )

        cleaned = apply_layout_nms(detected, iou_threshold=self._nms_iou)
        return sort_layout_boxes_reading_order(cleaned, page_width_px=float(decoded.width))


# ---------------------------------------------------------------------------
# 2. ONNX Runtime 기반 오픈웨이트 레이아웃 탐지기 (Apache-2.0 PP-DocLayoutV2/V3 등)
# ---------------------------------------------------------------------------


def _load_companion_label_list(model_path: Path) -> tuple[str, ...] | None:
    """ONNX 모델 옆에 `inference.yml`, `config.json`, `labels.txt`가 있으면 라벨 목록을 읽는다."""
    parent = model_path.parent
    cfg_json = parent / "config.json"
    if cfg_json.is_file():
        try:
            obj = json.loads(cfg_json.read_text(encoding="utf-8"))
            if isinstance(obj, dict):
                raw_list = obj.get("label_list")
                if isinstance(raw_list, list) and raw_list:
                    return tuple(str(x) for x in raw_list)
                id2label = obj.get("id2label")
                if isinstance(id2label, dict) and id2label:
                    ordered = sorted(id2label.items(), key=lambda kv: int(kv[0]))
                    return tuple(str(v) for _, v in ordered)
        except (OSError, ValueError, json.JSONDecodeError):
            pass

    inf_yml = parent / "inference.yml"
    if inf_yml.is_file():
        try:
            lines = inf_yml.read_text(encoding="utf-8").splitlines()
            labels: list[str] = []
            in_list = False
            for line in lines:
                stripped = line.strip()
                if stripped.startswith("label_list:"):
                    in_list = True
                    continue
                if in_list:
                    if stripped.startswith("- "):
                        val = stripped[2:].strip().strip("'\"")
                        if val:
                            labels.append(val)
                    elif stripped and not stripped.startswith("#"):
                        break
            if labels:
                return tuple(labels)
        except OSError:
            pass

    labels_txt = parent / "labels.txt"
    if labels_txt.is_file():
        try:
            lines = [ln.strip() for ln in labels_txt.read_text(encoding="utf-8").splitlines()]
            cleaned = [ln for ln in lines if ln]
            if cleaned:
                return tuple(cleaned)
        except OSError:
            pass
    return None


def _decode_image_to_rgb_array(inp: LayoutDetectionInput, np_mod: Any) -> Any:
    """이미지 바이트를 `(H, W, 3)` uint8 RGB numpy 배열로 디코딩한다(PNG는 Pillow 불필요)."""
    decoded = decode_png(inp.image_bytes)
    if decoded is not None:
        arr = np_mod.frombuffer(decoded.pixels, dtype=np_mod.uint8)
        if decoded.channels == 1:
            gray = arr.reshape((decoded.height, decoded.width, 1))
            return np_mod.repeat(gray, 3, axis=2)
        if decoded.channels == 2:
            ga = arr.reshape((decoded.height, decoded.width, 2))
            return np_mod.repeat(ga[:, :, :1], 3, axis=2)
        if decoded.channels == 3:
            return arr.reshape((decoded.height, decoded.width, 3))
        if decoded.channels == 4:
            return arr.reshape((decoded.height, decoded.width, 4))[:, :, :3]

    # JPEG/WebP 등은 선택 의존성 Pillow가 있을 때 디코딩 폴백
    try:
        pil_image = importlib.import_module("PIL.Image")
    except ImportError as exc:
        raise ParserInputError(
            "PNG 외 이미지 형식을 ONNX 레이아웃 탐지기에 넣으려면 "
            "Pillow 또는 PNG 페이지 렌더러가 필요하다."
        ) from exc
    import io

    with pil_image.open(io.BytesIO(inp.image_bytes)) as img:
        rgb_img = img.convert("RGB")
        return np_mod.asarray(rgb_img, dtype=np_mod.uint8)


def _resize_rgb_bilinear(rgb: Any, target_h: int, target_w: int, np_mod: Any) -> Any:
    """OpenCV/Pillow 없이 numpy만으로 `(H, W, 3)` 이미지를 쌍선형 보간 리사이즈한다."""
    src_h, src_w = int(rgb.shape[0]), int(rgb.shape[1])
    if src_h == target_h and src_w == target_w:
        return rgb.astype(np_mod.float32)

    y_coords = np_mod.linspace(0.0, max(0.0, float(src_h - 1)), target_h, dtype=np_mod.float32)
    x_coords = np_mod.linspace(0.0, max(0.0, float(src_w - 1)), target_w, dtype=np_mod.float32)

    y0 = np_mod.floor(y_coords).astype(np_mod.int64)
    x0 = np_mod.floor(x_coords).astype(np_mod.int64)
    y1 = np_mod.minimum(y0 + 1, src_h - 1)
    x1 = np_mod.minimum(x0 + 1, src_w - 1)

    wy = (y_coords - y0)[:, None, None]
    wx = (x_coords - x0)[None, :, None]

    top = (1.0 - wx) * rgb[y0[:, None], x0[None, :]] + wx * rgb[y0[:, None], x1[None, :]]
    bot = (1.0 - wx) * rgb[y1[:, None], x0[None, :]] + wx * rgb[y1[:, None], x1[None, :]]
    return ((1.0 - wy) * top + wy * bot).astype(np_mod.float32)


class OnnxDocLayoutDetector:
    """ONNX Runtime 기반 오픈웨이트 문서 레이아웃 탐지기(Apache-2.0 PP-DocLayoutV2/V3 권장).

    - 핵심 패키지 의존성을 늘리지 않도록 `onnxruntime`과 `numpy`를 지연 import한다.
    - Paddle2ONNX RT-DETR 표준 3입력(`image`, `im_shape`, `scale_factor`)과
      단일 입력(`images` / `image`) ONNX 그래프를 모두 자동 감지해 지원한다.
    - 출력 텐서 `[N, 6]`(`cls_id, score, x0, y0, x1, y1`), `[N, 7/8]`(읽기 순서 포함),
      `[1, N, 6]`, 또는 분리된 `(boxes, scores, labels)` 텐서를 모두 해석한다.
    """

    def __init__(
        self,
        model_path: Path | str,
        *,
        labels: Sequence[str] | None = None,
        label_preset: str = "pp-doclayout-v2",
        score_threshold: float = 0.4,
        nms_iou: float = 0.45,
        input_size: tuple[int, int] | None = None,
        providers: Sequence[str] = ("CPUExecutionProvider",),
        bgr_input: bool = False,
        session: Any = None,
    ) -> None:
        self._model_path = Path(model_path)
        preset_key = label_preset.strip().lower()
        if labels is not None:
            self._labels = tuple(labels)
        else:
            companion = _load_companion_label_list(self._model_path)
            if companion is not None:
                self._labels = companion
            elif preset_key in LAYOUT_LABEL_PRESETS:
                self._labels = LAYOUT_LABEL_PRESETS[preset_key]
            else:
                valid = ", ".join(sorted(LAYOUT_LABEL_PRESETS))
                raise ParserInputError(
                    f"지원하지 않는 레이아웃 라벨 프리셋이다: '{label_preset}' (지원: {valid})"
                )

        self._score_threshold = _clamp(float(score_threshold), 0.0, 1.0)
        self._nms_iou = _clamp(float(nms_iou), 0.05, 0.95)
        self._explicit_input_size = input_size
        self._providers = tuple(providers)
        self._bgr_input = bgr_input
        self._session: Any = session
        self._ort: Any = None
        self._np: Any = None

        try:
            self._np = importlib.import_module("numpy")
        except ImportError:
            self._np = None

        if self._session is None:
            try:
                self._ort = importlib.import_module("onnxruntime")
            except ImportError:
                self._ort = None

    @property
    def name(self) -> str:
        return f"onnx:{self._model_path.stem}"

    @property
    def available(self) -> bool:
        if self._np is None:
            return False
        if self._session is not None:
            return True
        return self._ort is not None and self._model_path.is_file()

    @property
    def labels(self) -> tuple[str, ...]:
        return self._labels

    def _ensure_session(self) -> Any:
        if self._np is None:
            raise ParserInputError(
                "ONNX 레이아웃 탐지기에는 선택 의존성 `numpy`와 `onnxruntime`이 필요하다. "
                "설치 예: `uv pip install onnxruntime numpy` "
                "(Apache-2.0 PP-DocLayoutV2 ONNX 권장)"
            )
        if self._session is not None:
            return self._session
        if self._ort is None:
            raise ParserInputError(
                "ONNX 레이아웃 탐지기에는 선택 의존성 `onnxruntime`이 필요하다. "
                "설치 예: `uv pip install onnxruntime`"
            )
        if not self._model_path.is_file():
            raise ParserInputError(f"ONNX 레이아웃 모델 파일이 없다: {self._model_path}")
        self._session = self._ort.InferenceSession(
            str(self._model_path),
            providers=list(self._providers),
        )
        return self._session

    def _infer_input_size(self, session: Any) -> tuple[int, int]:
        if self._explicit_input_size is not None:
            return self._explicit_input_size
        for meta in session.get_inputs():
            shape = getattr(meta, "shape", None)
            if isinstance(shape, (list, tuple)) and len(shape) == 4:
                h_dim, w_dim = shape[2], shape[3]
                if isinstance(h_dim, int) and isinstance(w_dim, int) and h_dim > 0 and w_dim > 0:
                    return (h_dim, w_dim)
        return (800, 800)

    def _label_for_class_id(self, cls_id: int) -> str:
        if 0 <= cls_id < len(self._labels):
            return self._labels[cls_id]
        return f"class_{cls_id}"

    @staticmethod
    def _to_list(arr: Any) -> list[Any]:
        if hasattr(arr, "tolist"):
            converted = arr.tolist()
            return converted if isinstance(converted, list) else [converted]
        if isinstance(arr, list):
            return arr
        if isinstance(arr, tuple):
            return list(arr)
        return [arr]

    def _parse_outputs(
        self,
        outputs: Sequence[Any],
        *,
        orig_w: int,
        orig_h: int,
        target_w: int,
        target_h: int,
    ) -> list[DetectedLayoutBox]:
        if not outputs:
            return []

        rows: list[tuple[int, float, float, float, float, float, int]] = []
        primary = self._to_list(outputs[0])

        # 형태 A: Paddle2ONNX RT-DETR `[N, 6+]` / `[1, N, 6+]` ([cls, score, x0, y0, x1, y1, ord?])
        if (
            len(primary) == 1
            and isinstance(primary[0], list)
            and primary[0]
            and isinstance(primary[0][0], list)
        ):
            primary = primary[0]

        if primary and isinstance(primary[0], list) and len(primary[0]) >= 6:
            order_vec: list[Any] | None = None
            if len(outputs) >= 3:
                cand_ord = self._to_list(outputs[2])
                if cand_ord and isinstance(cand_ord[0], list) and len(cand_ord) == 1:
                    cand_ord = cand_ord[0]
                if len(cand_ord) == len(primary) and not isinstance(cand_ord[0], list):
                    order_vec = cand_ord
            for idx, row in enumerate(primary):
                if not isinstance(row, list) or len(row) < 6:
                    continue
                cls_id = round(float(row[0]))
                score = float(row[1])
                x0, y0, x1, y1 = float(row[2]), float(row[3]), float(row[4]), float(row[5])
                order_val = 0
                if len(row) >= 7:
                    raw_ord = float(row[6])
                    if raw_ord >= 0.0:
                        order_val = round(raw_ord) + 1
                elif order_vec is not None:
                    order_val = round(float(order_vec[idx])) + 1
                rows.append((cls_id, score, x0, y0, x1, y1, order_val))
        # 형태 B: 분리된 출력 `(boxes[N,4], scores[N], labels[N])` 또는 `(labels, boxes, scores)`
        elif len(outputs) >= 3:
            a0 = self._to_list(outputs[0])
            a1 = self._to_list(outputs[1])
            a2 = self._to_list(outputs[2])
            if len(a0) == 1 and isinstance(a0[0], list) and a0[0] and isinstance(a0[0][0], list):
                a0 = a0[0]
            if (
                len(a1) == 1
                and isinstance(a1[0], list)
                and (not a1[0] or not isinstance(a1[0][0], list))
            ):
                a1 = a1[0]
            if (
                len(a2) == 1
                and isinstance(a2[0], list)
                and (not a2[0] or not isinstance(a2[0][0], list))
            ):
                a2 = a2[0]
            if a0 and isinstance(a0[0], list) and len(a0[0]) == 4:
                boxes_arr, scores_arr, labels_arr = a0, a1, a2
            elif a1 and isinstance(a1[0], list) and len(a1[0]) == 4:
                labels_arr, boxes_arr, scores_arr = a0, a1, a2
            else:
                boxes_arr, scores_arr, labels_arr = None, None, None
            if boxes_arr is not None and scores_arr is not None and labels_arr is not None:
                n = min(len(boxes_arr), len(scores_arr), len(labels_arr))
                for idx in range(n):
                    b = boxes_arr[idx]
                    rows.append(
                        (
                            round(float(labels_arr[idx])),
                            float(scores_arr[idx]),
                            float(b[0]),
                            float(b[1]),
                            float(b[2]),
                            float(b[3]),
                            0,
                        )
                    )

        if not rows:
            return []

        # 좌표 공간 자동 판별: 모두 <= 1.05이면 [0..1] 정규화 좌표
        max_coord = max(max(r[2], r[3], r[4], r[5]) for r in rows)
        is_normalized = max_coord <= 1.05
        detected: list[DetectedLayoutBox] = []
        for cls_id, score, x0, y0, x1, y1, order_val in rows:
            if score < self._score_threshold:
                continue
            if is_normalized:
                x0, x1 = x0 * orig_w, x1 * orig_w
                y0, y1 = y0 * orig_h, y1 * orig_h
            cx0 = _clamp(min(x0, x1), 0.0, float(orig_w))
            cy0 = _clamp(min(y0, y1), 0.0, float(orig_h))
            cx1 = _clamp(max(x0, x1), 0.0, float(orig_w))
            cy1 = _clamp(max(y0, y1), 0.0, float(orig_h))
            if (cx1 - cx0) < 4.0 or (cy1 - cy0) < 4.0:
                continue

            raw_label = self._label_for_class_id(cls_id)
            kind, strat = normalize_layout_label(raw_label)
            detected.append(
                DetectedLayoutBox(
                    raw_label=raw_label,
                    element_kind=kind,
                    recommended_strategy=strat,
                    bbox_px=(cx0, cy0, cx1, cy1),
                    score=_clamp(score, 0.0, 1.0),
                    reading_order=order_val,
                    reason=(
                        f"ONNX 레이아웃 탐지({self._model_path.stem}): "
                        f"`{raw_label}` (신뢰도 {score:.2f})"
                    ),
                )
            )
        _ = (target_w, target_h)
        cleaned = apply_layout_nms(detected, iou_threshold=self._nms_iou)
        return sort_layout_boxes_reading_order(cleaned, page_width_px=float(orig_w))

    def detect(self, inp: LayoutDetectionInput) -> list[DetectedLayoutBox]:
        session = self._ensure_session()
        np = self._np

        rgb = _decode_image_to_rgb_array(inp, np)
        orig_h, orig_w = int(rgb.shape[0]), int(rgb.shape[1])
        target_h, target_w = self._infer_input_size(session)

        resized = _resize_rgb_bilinear(rgb, target_h, target_w, np)
        if self._bgr_input:
            resized = resized[:, :, ::-1]
        tensor = (resized / 255.0).transpose(2, 0, 1)[None, :, :, :].astype(np.float32)

        feed: dict[str, Any] = {}
        for meta in session.get_inputs():
            name = str(meta.name)
            low = name.lower()
            if low in ("im_shape", "orig_target_sizes", "im_size"):
                feed[name] = np.array([[float(target_h), float(target_w)]], dtype=np.float32)
            elif low == "scale_factor":
                feed[name] = np.array(
                    [[float(target_h) / max(1, orig_h), float(target_w) / max(1, orig_w)]],
                    dtype=np.float32,
                )
            else:
                feed[name] = tensor

        outputs = session.run(None, feed)
        return self._parse_outputs(
            outputs,
            orig_w=orig_w,
            orig_h=orig_h,
            target_w=target_w,
            target_h=target_h,
        )


# ---------------------------------------------------------------------------
# 3. HTTP 레이아웃 탐지 서비스 어댑터 (TrustZone 보안 경계 적용)
# ---------------------------------------------------------------------------


class HttpLayoutDetector:
    """로컬/사내 레이아웃 탐지 마이크로서비스(`POST /v1/layout/detect`) 연동 어댑터.

    `validate_vision_endpoint`의 실패 닫힘(fail-closed) 신뢰 구역 정책을 그대로 적용한다.
    루프백(`127.0.0.1`, `::1`)과 등록 사내 사설 IP만 기본 허용하며, 외부 호스트는
    `allow_external=True`가 없으면 `VisionSecurityError`로 즉시 차단한다.
    """

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_HTTP_LAYOUT_BASE_URL,
        api_key: str | None = None,
        api_key_env: str | None = None,
        score_threshold: float = 0.4,
        nms_iou: float = 0.45,
        timeout_sec: float = 60.0,
        allow_external: bool = False,
        registered_internal: Sequence[str] = (),
    ) -> None:
        self._base_url = base_url.strip().rstrip("/")
        self._zone = validate_vision_endpoint(
            self._base_url,
            allow_external=allow_external,
            registered_internal=registered_internal,
        )
        resolved_key = api_key
        if not resolved_key and api_key_env:
            resolved_key = os.environ.get(api_key_env.strip())
        self._api_key = resolved_key.strip() if resolved_key else None
        self._score_threshold = _clamp(float(score_threshold), 0.0, 1.0)
        self._nms_iou = _clamp(float(nms_iou), 0.05, 0.95)
        self._timeout_sec = max(1.0, float(timeout_sec))

    @property
    def name(self) -> str:
        return "http-layout"

    @property
    def available(self) -> bool:
        return True

    @property
    def trust_zone(self) -> TrustZone:
        return self._zone

    def _auth_headers(self) -> dict[str, str]:
        if not self._api_key:
            return {}
        return {"Authorization": f"Bearer {self._api_key}"}

    def detect(self, inp: LayoutDetectionInput) -> list[DetectedLayoutBox]:
        mime_type = _detect_image_mime(inp.image_bytes, inp.image_extension)
        b64 = base64.b64encode(inp.image_bytes).decode("ascii")
        payload: dict[str, Any] = {
            "page": inp.page,
            "image_base64": b64,
            "mime_type": mime_type,
            "width_px": inp.image_width_px,
            "height_px": inp.image_height_px,
            "score_threshold": self._score_threshold,
        }
        url = (
            self._base_url
            if self._base_url.endswith("/detect")
            else f"{self._base_url}/v1/layout/detect"
        )
        try:
            resp = _open_json_request(
                url,
                zone=self._zone,
                payload=payload,
                headers=self._auth_headers(),
                timeout_sec=self._timeout_sec,
            )
        except urllib.error.HTTPError as exc:
            raise ParserInputError(
                f"레이아웃 탐지 HTTP 서비스 호출이 실패했다(HTTP {exc.code})."
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ParserInputError(
                "레이아웃 탐지 HTTP 서비스에 연결할 수 없다(호스트·포트·실행 여부를 확인하라)."
            ) from exc
        except (ValueError, json.JSONDecodeError) as exc:
            raise ParserInputError(
                "레이아웃 탐지 HTTP 서비스 응답 JSON 형식이 올바르지 않다."
            ) from exc

        raw_items = resp.get("boxes") or resp.get("regions") or resp.get("detections")
        if not isinstance(raw_items, list):
            return []

        coord_space = str(resp.get("coordinate_space") or "").strip().lower()
        img_w = float(max(1, inp.image_width_px))
        img_h = float(max(1, inp.image_height_px))
        detected: list[DetectedLayoutBox] = []

        for item in raw_items:
            if not isinstance(item, dict):
                continue
            raw_label = str(
                item.get("label") or item.get("category") or item.get("class_name") or "text"
            )
            coords = item.get("bbox") or item.get("coordinate") or item.get("box")
            if not isinstance(coords, list) or len(coords) < 4:
                continue
            try:
                x0, y0, x1, y1 = (
                    float(coords[0]),
                    float(coords[1]),
                    float(coords[2]),
                    float(coords[3]),
                )
                raw_score = item.get("score")
                if raw_score is None:
                    raw_score = item.get("confidence", 1.0)
                score = float(raw_score if raw_score is not None else 1.0)
            except (TypeError, ValueError):
                continue
            if score < self._score_threshold:
                continue

            if coord_space == "norm_1000":
                x0, x1 = (x0 / 1000.0) * img_w, (x1 / 1000.0) * img_w
                y0, y1 = (y0 / 1000.0) * img_h, (y1 / 1000.0) * img_h
            elif coord_space == "normalized" or max(x0, y0, x1, y1) <= 1.05:
                x0, x1 = x0 * img_w, x1 * img_w
                y0, y1 = y0 * img_h, y1 * img_h

            cx0 = _clamp(min(x0, x1), 0.0, img_w)
            cy0 = _clamp(min(y0, y1), 0.0, img_h)
            cx1 = _clamp(max(x0, x1), 0.0, img_w)
            cy1 = _clamp(max(y0, y1), 0.0, img_h)
            if (cx1 - cx0) < 4.0 or (cy1 - cy0) < 4.0:
                continue

            order_raw = item.get("reading_order")
            if order_raw is None:
                order_raw = item.get("order", 0)
            try:
                order_val = max(0, int(order_raw)) if order_raw is not None else 0
            except (TypeError, ValueError):
                order_val = 0

            kind, strat = normalize_layout_label(raw_label)
            detected.append(
                DetectedLayoutBox(
                    raw_label=raw_label,
                    element_kind=kind,
                    recommended_strategy=strat,
                    bbox_px=(cx0, cy0, cx1, cy1),
                    score=_clamp(score, 0.0, 1.0),
                    reading_order=order_val,
                    reason=(
                        f"HTTP 레이아웃 탐지({self._zone.value}): "
                        f"`{raw_label}` (신뢰도 {score:.2f})"
                    ),
                )
            )

        cleaned = apply_layout_nms(detected, iou_threshold=self._nms_iou)
        return sort_layout_boxes_reading_order(cleaned, page_width_px=img_w)


# ---------------------------------------------------------------------------
# 4. 팩토리 함수
# ---------------------------------------------------------------------------


def create_layout_detector(
    provider: str,
    *,
    model_path: Path | str | None = None,
    labels: Sequence[str] | None = None,
    label_preset: str = "pp-doclayout-v2",
    base_url: str | None = None,
    api_key: str | None = None,
    api_key_env: str | None = None,
    score_threshold: float = 0.4,
    nms_iou: float = 0.45,
    allow_external: bool = False,
    registered_internal: Sequence[str] = (),
    timeout_sec: float = 60.0,
) -> PageLayoutDetector:
    """문자열 프로바이더 이름으로 적절한 `PageLayoutDetector`를 생성한다.

    지원 프로바이더: `none`, `heuristic`, `onnx`, `http`.
    """
    norm = provider.strip().lower()
    if norm in ("", "none", "noop"):
        return NoopLayoutDetector()
    if norm == "heuristic":
        return HeuristicRasterLayoutDetector(
            score_threshold=score_threshold,
            nms_iou=nms_iou,
        )
    if norm == "onnx":
        if model_path is None or not str(model_path).strip():
            raise ParserInputError(
                "ONNX 레이아웃 탐지기를 쓰려면 --layout-model-path로 "
                "모델 파일(.onnx)을 지정해야 한다."
            )
        return OnnxDocLayoutDetector(
            model_path=model_path,
            labels=labels,
            label_preset=label_preset,
            score_threshold=score_threshold,
            nms_iou=nms_iou,
        )
    if norm == "http":
        return HttpLayoutDetector(
            base_url=base_url or DEFAULT_HTTP_LAYOUT_BASE_URL,
            api_key=api_key,
            api_key_env=api_key_env,
            score_threshold=score_threshold,
            nms_iou=nms_iou,
            timeout_sec=timeout_sec,
            allow_external=allow_external,
            registered_internal=registered_internal,
        )

    raise ParserInputError(
        f"지원하지 않는 레이아웃 탐지기다: '{provider}' (지원: none, heuristic, onnx, http)"
    )
