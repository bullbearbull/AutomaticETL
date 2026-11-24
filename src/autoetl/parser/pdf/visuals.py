"""PDF 시각 영역(스캔 페이지·그림·벡터 도식/차트) 탐지 및 텍스트 품질 진단.

텍스트 추출만으로 충분하지 않은 영역을 정확히 구분해 OCR/VLM 라우팅과
크롭 대상(`VisualRegion`)으로 구조화한다.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from autoetl.parser.contracts import ElementKind, ParseStrategy, VisualRegion
from autoetl.parser.pdf.geometry import (
    RawImagePlacement,
    RawPageData,
    bbox_iou,
    clamp,
    make_bbox,
    rect_contains_center,
    union_area,
)


@dataclass(frozen=True)
class TextQualityStats:
    """페이지 텍스트 레이어의 실사용 가능 품질 지표(깨진 OCR·CID 가비지 감지용)."""

    total_chars: int
    usable_chars: int
    garbage_chars: int
    usable_ratio: float
    garbage_ratio: float


@dataclass(frozen=True)
class VisualDetectionResult:
    """한 페이지에서 탐지한 시각 영역과 부가 기하 정보."""

    visual_regions: list[VisualRegion]
    region_images: dict[str, RawImagePlacement]
    consumed_span_indices: frozenset[int]
    non_decorative_image_count: int
    image_coverage: float
    non_table_vector_count: int
    is_full_page_scan: bool


def analyze_text_quality(page: RawPageData) -> TextQualityStats:
    """페이지의 텍스트 스팬에서 정상 문자 대비 깨진 문자/제어문자 비율을 계산한다."""
    total = 0
    usable = 0
    garbage = 0
    for sp in page.spans:
        for ch in sp.text:
            if ch.isspace():
                continue
            total += 1
            if ch == "\ufffd":
                garbage += 1
                continue
            cat = unicodedata.category(ch)
            if cat.startswith(("C",)):  # Control / Private Use / Surrogate
                garbage += 1
            elif cat.startswith(("L", "N", "P", "S")):  # Letter/Number/Punctuation/Symbol
                usable += 1
            else:
                garbage += 1

    if total == 0:
        return TextQualityStats(
            total_chars=0,
            usable_chars=0,
            garbage_chars=0,
            usable_ratio=0.0,
            garbage_ratio=0.0,
        )
    return TextQualityStats(
        total_chars=total,
        usable_chars=usable,
        garbage_chars=garbage,
        usable_ratio=round(usable / total, 4),
        garbage_ratio=round(garbage / total, 4),
    )


def _clip_to_page(
    rect: tuple[float, float, float, float], page_w: float, page_h: float
) -> tuple[float, float, float, float]:
    return (
        clamp(rect[0], 0.0, page_w),
        clamp(rect[1], 0.0, page_h),
        clamp(rect[2], 0.0, page_w),
        clamp(rect[3], 0.0, page_h),
    )


def _is_decorative_image(
    img: RawImagePlacement,
    page: RawPageData,
    text_quality: TextQualityStats,
    repeating_logo_keys: set[tuple[int, int, int, int]],
) -> tuple[bool, str]:
    """아이콘·불릿·배경 벽지·반복 로고 같은 장식 이미지를 구분한다."""
    clipped = _clip_to_page((img.x0, img.top, img.x1, img.bottom), page.width, page.height)
    w = clipped[2] - clipped[0]
    h = clipped[3] - clipped[1]
    area = w * h
    page_area = max(1.0, page.width * page.height)
    cov = area / page_area

    if w < 24.0 or h < 24.0 or cov < 0.004:
        return True, "작은 아이콘·불릿 크기"

    # PPTX 변환 PDF 등에서 흔한 전체 슬라이드 배경 이미지(위에 정상 텍스트가 풍부)
    if cov >= 0.85 and text_quality.usable_chars >= 120 and text_quality.garbage_ratio < 0.2:
        return True, "충분한 디지털 텍스트 아래 깔린 전체 배경 이미지"

    # 여러 페이지의 상·하단 같은 위치에 반복되는 작은 로고
    sig = (
        round((clipped[0] / page.width) * 20),
        round((clipped[1] / page.height) * 20),
        round((w / page.width) * 20),
        round((h / page.height) * 20),
    )
    if sig in repeating_logo_keys and cov < 0.08:
        return True, "다수 페이지에 반복되는 머리글/바닥글 로고"

    return False, ""


def find_repeating_logo_signatures(pages: list[RawPageData]) -> set[tuple[int, int, int, int]]:
    """3페이지 이상 문서에서 60% 이상 페이지에 같은 위치·크기로 나오는 작은 로고를 찾는다."""
    if len(pages) < 3:
        return set()
    counts: dict[tuple[int, int, int, int], int] = {}
    for page in pages:
        page_area = max(1.0, page.width * page.height)
        seen_in_page: set[tuple[int, int, int, int]] = set()
        for img in page.images:
            clipped = _clip_to_page((img.x0, img.top, img.x1, img.bottom), page.width, page.height)
            w = clipped[2] - clipped[0]
            h = clipped[3] - clipped[1]
            if w < 12.0 or h < 12.0 or (w * h) / page_area >= 0.08:
                continue
            sig = (
                round((clipped[0] / page.width) * 20),
                round((clipped[1] / page.height) * 20),
                round((w / page.width) * 20),
                round((h / page.height) * 20),
            )
            seen_in_page.add(sig)
        for sig in seen_in_page:
            counts[sig] = counts.get(sig, 0) + 1

    threshold = max(3, int(len(pages) * 0.6))
    return {sig for sig, cnt in counts.items() if cnt >= threshold}


def _box_near(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
    pad: float = 12.0,
) -> bool:
    return not (a[2] + pad < b[0] or b[2] + pad < a[0] or a[3] + pad < b[1] or b[3] + pad < a[1])


def _detect_vector_diagram_clusters(
    page: RawPageData,
    table_boxes: tuple[tuple[float, float, float, float], ...],
    figure_boxes: list[tuple[float, float, float, float]],
) -> tuple[list[tuple[tuple[float, float, float, float], int, int]], int]:
    """표 외부의 벡터 도형·곡선·대각선을 공간 군집화해 도식/차트 영역을 탐지한다.

    반환: ([(cluster_bbox, primitive_count, curve_count), ...], non_table_vector_count)
    """
    page_area = max(1.0, page.width * page.height)
    primitives: list[tuple[tuple[float, float, float, float], bool]] = []  # (box, is_curve)

    def inside_table(box: tuple[float, float, float, float]) -> bool:
        return any(rect_contains_center(tb, box, margin=4.0) for tb in table_boxes)

    for ln in page.lines:
        box = (ln.x0, ln.top, ln.x1, ln.bottom)
        if inside_table(box):
            continue
        # 머리글·바닥글 단일 구분선(매우 넓은 단일 수평선)은 도식 군집에서 제외
        if (
            ln.is_horizontal
            and ln.length >= page.width * 0.65
            and (ln.top <= page.height * 0.14 or ln.bottom >= page.height * 0.86)
        ):
            continue
        primitives.append((box, False))

    for rect in page.rects:
        box = (rect.x0, rect.top, rect.x1, rect.bottom)
        if rect.area > page_area * 0.80:
            continue
        if rect.width < 4.0 and rect.height < 4.0:
            continue
        if inside_table(box):
            continue
        # 얇은 전체 폭 구분선 제외
        if (
            rect.height <= 3.0
            and rect.width >= page.width * 0.65
            and (rect.top <= page.height * 0.14 or rect.bottom >= page.height * 0.86)
        ):
            continue
        primitives.append((box, False))

    for crv in page.curves:
        box = (crv.x0, crv.top, crv.x1, crv.bottom)
        if inside_table(box):
            continue
        primitives.append((box, True))

    non_table_count = len(primitives)
    if non_table_count < 4:
        return [], non_table_count

    # Union-Find로 인접 벡터 원시 도형 군집화
    n = len(primitives)
    parent = list(range(n))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i in range(n):
        bi = primitives[i][0]
        for j in range(i + 1, n):
            if _box_near(bi, primitives[j][0], pad=14.0):
                union(i, j)

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)

    clusters: list[tuple[tuple[float, float, float, float], int, int]] = []
    for idxs in groups.values():
        prim_count = len(idxs)
        curve_count = sum(1 for i in idxs if primitives[i][1])
        if prim_count < 5 and not (curve_count >= 2 and prim_count >= 4):
            continue
        cx0 = clamp(min(primitives[i][0][0] for i in idxs), 0.0, page.width)
        ctop = clamp(min(primitives[i][0][1] for i in idxs), 0.0, page.height)
        cx1 = clamp(max(primitives[i][0][2] for i in idxs), 0.0, page.width)
        cbot = clamp(max(primitives[i][0][3] for i in idxs), 0.0, page.height)
        cw = cx1 - cx0
        ch = cbot - ctop
        if cw < 45.0 or ch < 35.0 or (cw * ch) / page_area < 0.015:
            continue
        cbox = (cx0, ctop, cx1, cbot)
        if any(bbox_iou(cbox, fb) > 0.6 for fb in figure_boxes):
            continue
        clusters.append((cbox, prim_count, curve_count))

    clusters.sort(key=lambda item: (item[0][1], item[0][0]))
    return clusters, non_table_count


def detect_page_visuals(
    page: RawPageData,
    *,
    text_quality: TextQualityStats,
    table_boxes: tuple[tuple[float, float, float, float], ...],
    excluded_span_indices: frozenset[int],
    repeating_logo_keys: set[tuple[int, int, int, int]] | None = None,
) -> VisualDetectionResult:
    """한 페이지의 스캔 여부, 그림 이미지, 벡터 도식/차트 영역을 탐지한다."""
    logo_keys = repeating_logo_keys or set()
    page_area = max(1.0, page.width * page.height)

    meaningful_images: list[tuple[RawImagePlacement, tuple[float, float, float, float]]] = []
    for img in page.images:
        is_deco, _ = _is_decorative_image(img, page, text_quality, logo_keys)
        if not is_deco:
            clipped = _clip_to_page((img.x0, img.top, img.x1, img.bottom), page.width, page.height)
            meaningful_images.append((img, clipped))

    img_coverage = clamp(union_area([box for _, box in meaningful_images]) / page_area, 0.0, 1.0)

    regions: list[VisualRegion] = []
    region_images: dict[str, RawImagePlacement] = {}
    consumed_spans: set[int] = set()

    # 1. 전체 페이지 스캔 판정
    is_broken_text = text_quality.total_chars >= 15 and text_quality.garbage_ratio >= 0.35
    is_low_text = text_quality.usable_chars < 40
    is_full_scan = img_coverage >= 0.65 and (is_low_text or is_broken_text)

    if is_full_scan:
        # 가장 큰 이미지 1장이 전체를 덮고 있으면 내장 이미지 직접 OCR 가능
        largest_img, largest_box = max(
            meaningful_images,
            key=lambda pair: (pair[1][2] - pair[1][0]) * (pair[1][3] - pair[1][1]),
        )
        single_dominant = (
            (largest_box[2] - largest_box[0]) * (largest_box[3] - largest_box[1])
        ) / page_area >= 0.65
        is_landscape = page.width > page.height * 1.15
        strategy = (
            ParseStrategy.VLM_FULL_PAGE
            if (is_landscape or not single_dominant)
            else ParseStrategy.EMBEDDED_IMAGE_OCR
        )
        reason_parts = [f"이미지 점유율 {img_coverage:.0%}"]
        if is_broken_text:
            reason_parts.append(f"숨겨진 텍스트 레이어 깨짐 비율 {text_quality.garbage_ratio:.0%}")
        else:
            reason_parts.append(f"사용 가능한 디지털 텍스트 {text_quality.usable_chars}자")
        reason = "스캔/이미지 전용 페이지로 판정(" + ", ".join(reason_parts) + ")"

        region_id = f"p{page.page:03d}_v01"
        regions.append(
            VisualRegion(
                region_id=region_id,
                page=page.page,
                element_kind=ElementKind.SCANNED_PAGE,
                recommended_strategy=strategy,
                bbox=make_bbox(0.0, 0.0, page.width, page.height, page.width, page.height),
                reason=reason,
                confidence=0.95 if text_quality.usable_chars == 0 else 0.88,
                priority="high",
                image_width_px=largest_img.pixel_width or None,
                image_height_px=largest_img.pixel_height or None,
            )
        )
        if single_dominant:
            region_images[region_id] = largest_img

        # 깨진 텍스트 레이어라면 스팬을 전부 소비해 본문 오염 방지
        if is_broken_text:
            consumed_spans.update(range(len(page.spans)))

        return VisualDetectionResult(
            visual_regions=regions,
            region_images=region_images,
            consumed_span_indices=frozenset(consumed_spans),
            non_decorative_image_count=len(meaningful_images),
            image_coverage=round(img_coverage, 4),
            non_table_vector_count=0,
            is_full_page_scan=True,
        )

    # 2. 일반 페이지 내 개별 그림·스크린샷 영역
    figure_boxes: list[tuple[float, float, float, float]] = []
    meaningful_images.sort(key=lambda pair: (pair[1][1], pair[1][0]))
    for img, box in meaningful_images:
        cov = ((box[2] - box[0]) * (box[3] - box[1])) / page_area
        region_id = f"p{page.page:03d}_v{len(regions) + 1:02d}"
        strategy = ParseStrategy.EMBEDDED_IMAGE_OCR if cov >= 0.35 else ParseStrategy.VLM_REGION
        regions.append(
            VisualRegion(
                region_id=region_id,
                page=page.page,
                element_kind=ElementKind.FIGURE_IMAGE,
                recommended_strategy=strategy,
                bbox=make_bbox(box[0], box[1], box[2], box[3], page.width, page.height),
                reason=(
                    f"내장 이미지({img.pixel_width}x{img.pixel_height}px, "
                    f"페이지 면적 {cov:.1%}) — OCR/VLM 시각 해석 대상"
                ),
                confidence=0.92,
                priority="high" if cov >= 0.25 else "medium",
                image_width_px=img.pixel_width or None,
                image_height_px=img.pixel_height or None,
            )
        )
        region_images[region_id] = img
        figure_boxes.append(box)

    # 3. 벡터 도식·차트 군집 탐지
    clusters, non_table_vec_count = _detect_vector_diagram_clusters(page, table_boxes, figure_boxes)
    for cbox, prim_count, curve_count in clusters:
        # 곡선·대각선이 전혀 없는 순수 직선/사각형 군집 안에 긴 본문·연락처 텍스트가
        # 밀집돼 있다면 차트/도식이 아니라 장식용 콜아웃/저자/면책 문구 상자로 본다.
        inner_indices = [
            idx
            for idx, sp in enumerate(page.spans)
            if idx not in excluded_span_indices
            and idx not in consumed_spans
            and rect_contains_center(cbox, (sp.x0, sp.top, sp.x1, sp.bottom), margin=4.0)
        ]
        if curve_count == 0 and inner_indices:
            inner_text = " ".join(page.spans[i].text.strip() for i in inner_indices)
            non_space = "".join(inner_text.split())
            has_prose_sentence = any(
                marker in inner_text for marker in ("다.", "요.", "니다", ". ", "@", "Tel.")
            )
            if len(non_space) >= 55 and (has_prose_sentence or prim_count <= 20):
                continue

        region_id = f"p{page.page:03d}_v{len(regions) + 1:02d}"
        # 도식/차트 내부 텍스트 + 축/범례용 인접 짧은 레이블(왼쪽 y축·아래 x축)을 힌트로 수집
        axis_collar = (
            max(0.0, cbox[0] - 28.0),
            max(0.0, cbox[1] - 12.0),
            min(page.width, cbox[2] + 12.0),
            min(page.height, cbox[3] + 20.0),
        )
        hint_indices: list[int] = list(inner_indices)
        inner_set = set(inner_indices)
        for idx, sp in enumerate(page.spans):
            if idx in excluded_span_indices or idx in consumed_spans or idx in inner_set:
                continue
            sp_box = (sp.x0, sp.top, sp.x1, sp.bottom)
            txt_clean = sp.text.strip()
            if (
                rect_contains_center(axis_collar, sp_box, margin=2.0)
                and len(txt_clean) <= 24
                and sp.width <= 110.0
                and not txt_clean.lower().startswith(("figure", "fig.", "table", "그림", "표"))
            ):
                hint_indices.append(idx)

        hints = [page.spans[i].text.strip() for i in hint_indices if page.spans[i].text.strip()]
        consumed_spans.update(hint_indices)

        # 축 레이블과 외곽선이 잘리지 않도록 소폭 여백(6pt)을 더해 크롭 영역 확장
        pad = 6.0
        if hint_indices:
            final_box = (
                max(0.0, min(cbox[0], *(page.spans[i].x0 for i in hint_indices)) - pad),
                max(0.0, min(cbox[1], *(page.spans[i].top for i in hint_indices)) - pad),
                min(page.width, max(cbox[2], *(page.spans[i].x1 for i in hint_indices)) + pad),
                min(page.height, max(cbox[3], *(page.spans[i].bottom for i in hint_indices)) + pad),
            )
        else:
            final_box = (
                max(0.0, cbox[0] - pad),
                max(0.0, cbox[1] - pad),
                min(page.width, cbox[2] + pad),
                min(page.height, cbox[3] + pad),
            )

        cov = ((final_box[2] - final_box[0]) * (final_box[3] - final_box[1])) / page_area
        regions.append(
            VisualRegion(
                region_id=region_id,
                page=page.page,
                element_kind=ElementKind.VECTOR_DIAGRAM,
                recommended_strategy=ParseStrategy.VLM_REGION,
                bbox=make_bbox(
                    final_box[0],
                    final_box[1],
                    final_box[2],
                    final_box[3],
                    page.width,
                    page.height,
                ),
                reason=(
                    f"표 외부 벡터 도형 군집(도형 {prim_count}개, 곡선/대각선 {curve_count}개, "
                    f"면적 {cov:.1%}) — 차트·도식 VLM 해석 권장"
                ),
                confidence=0.86 if curve_count > 0 else 0.80,
                priority="high" if cov >= 0.15 else "medium",
                embedded_text_hints=hints[:30],
            )
        )

    regions.sort(key=lambda r: (r.bbox.top, r.bbox.x0))
    return VisualDetectionResult(
        visual_regions=regions,
        region_images=region_images,
        consumed_span_indices=frozenset(consumed_spans),
        non_decorative_image_count=len(meaningful_images),
        image_coverage=round(img_coverage, 4),
        non_table_vector_count=non_table_vec_count,
        is_full_page_scan=False,
    )
