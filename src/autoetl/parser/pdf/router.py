"""페이지 구조 유형 분류 및 요소별 파싱 전략 라우터.

추출된 결정론 지표(텍스트 품질·이미지 점유율·표 비율·다단 개수·벡터 도식·종횡비)를
바탕으로 각 페이지의 구조 유형(`PageCategory`)과 요소별 파싱 전략(`ParseStrategy`),
그리고 사람이 바로 납득할 수 있는 한국어 판정 근거를 생성한다.
"""

from __future__ import annotations

from collections import Counter

from autoetl.parser.contracts import (
    DocumentMetadata,
    ElementKind,
    ExtractedTable,
    OutlineEntry,
    PageCategory,
    PageMetrics,
    PageRouteDecision,
    ParseStrategy,
    RegionRoute,
    RoutingPlan,
    RoutingSummary,
    VisualRegion,
)
from autoetl.parser.pdf.geometry import RawPageData, clamp, union_area
from autoetl.parser.pdf.layout import PageLayoutResult
from autoetl.parser.pdf.tables import TableDetectionResult
from autoetl.parser.pdf.visuals import TextQualityStats, VisualDetectionResult


def build_page_route_decision(
    page: RawPageData,
    *,
    text_quality: TextQualityStats,
    tables_res: TableDetectionResult,
    visuals_res: VisualDetectionResult,
    layout_res: PageLayoutResult,
    tables: list[ExtractedTable],
    visuals: list[VisualRegion],
) -> PageRouteDecision:
    """단일 페이지의 지표를 집계하고 구조 유형·파싱 전략·근거를 결정한다."""
    page_area = max(1.0, page.width * page.height)
    aspect_ratio = round(page.width / max(1.0, page.height), 3)

    table_boxes = [(t.bbox.x0, t.bbox.top, t.bbox.x1, t.bbox.bottom) for t in tables]
    table_area_ratio = round(clamp(union_area(table_boxes) / page_area, 0.0, 1.0), 4)

    visual_boxes = [(v.bbox.x0, v.bbox.top, v.bbox.x1, v.bbox.bottom) for v in visuals]
    visual_area_ratio = round(clamp(union_area(visual_boxes) / page_area, 0.0, 1.0), 4)

    effective_col_count = (
        layout_res.column_count
        if (layout_res.column_count >= 2 and layout_res.multi_column_char_ratio >= 0.30)
        else 1
    )
    effective_gutters = layout_res.column_gutters_x if effective_col_count >= 2 else []

    metrics = PageMetrics(
        page=page.page,
        width_pt=round(page.width, 2),
        height_pt=round(page.height, 2),
        rotation=page.rotation,
        aspect_ratio=aspect_ratio,
        char_count=text_quality.total_chars,
        usable_char_count=text_quality.usable_chars,
        usable_text_ratio=text_quality.usable_ratio,
        garbage_char_ratio=text_quality.garbage_ratio,
        line_count=layout_res.line_count,
        column_count=effective_col_count,
        column_gutters_x=effective_gutters,
        median_font_size=layout_res.median_font_size,
        max_font_size=layout_res.max_font_size,
        image_count=len(page.images),
        non_decorative_image_count=visuals_res.non_decorative_image_count,
        image_coverage=visuals_res.image_coverage,
        vector_line_count=len(page.lines),
        vector_rect_count=len(page.rects),
        vector_curve_count=len(page.curves),
        non_table_vector_count=visuals_res.non_table_vector_count,
        table_count=len(tables),
        table_area_ratio=table_area_ratio,
        visual_region_count=len(visuals),
        visual_area_ratio=visual_area_ratio,
        header_footer_line_count=len(layout_res.header_footer_blocks),
    )

    total_spans = max(1, len(page.spans))
    table_span_ratio = len(tables_res.consumed_span_indices) / total_spans
    has_vector_diagrams = any(v.element_kind == ElementKind.VECTOR_DIAGRAM for v in visuals)

    reasons: list[str] = []
    warnings: list[str] = []

    if text_quality.total_chars >= 15 and text_quality.garbage_ratio >= 0.35:
        warnings.append(
            f"텍스트 레이어 깨짐 비율이 높다({text_quality.garbage_ratio:.0%}) — "
            "내장 텍스트 대신 OCR/VLM 재추출을 권장한다."
        )

    # 페이지 구조 유형 결정(우선순위 명확)
    if (
        metrics.usable_char_count < 5
        and metrics.non_decorative_image_count == 0
        and metrics.table_count == 0
        and metrics.non_table_vector_count < 4
    ):
        category = PageCategory.BLANK
        primary_strategy = ParseStrategy.SKIP_DECORATIVE
        confidence = 0.98
        reasons.append("유효 텍스트·표·이미지·도식이 거의 없는 빈 페이지로 판정")
    elif visuals_res.is_full_page_scan:
        category = PageCategory.SCANNED_IMAGE
        primary_strategy = (
            visuals[0].recommended_strategy if visuals else ParseStrategy.VLM_FULL_PAGE
        )
        confidence = visuals[0].confidence if visuals else 0.92
        reasons.append(
            f"이미지 점유율 {metrics.image_coverage:.0%}, "
            f"유효 텍스트 {metrics.usable_char_count}자(깨짐 {metrics.garbage_char_ratio:.0%}) → "
            "전체 스캔/이미지 페이지"
        )
    elif metrics.table_count >= 1 and (table_area_ratio >= 0.35 or table_span_ratio >= 0.55):
        category = PageCategory.TABLE_HEAVY
        primary_strategy = tables[0].strategy
        confidence = round(max(t.confidence for t in tables), 2)
        reasons.append(
            f"표 {metrics.table_count}개 감지(표 면적 {table_area_ratio:.0%}, "
            f"표 텍스트 비중 {table_span_ratio:.0%}) → 표 중심 페이지"
        )
    elif has_vector_diagrams and (
        visual_area_ratio >= 0.25
        or (metrics.non_table_vector_count >= 12 and metrics.usable_char_count < 350)
    ):
        category = PageCategory.VECTOR_DIAGRAM_HEAVY
        primary_strategy = ParseStrategy.VLM_REGION
        confidence = 0.86
        reasons.append(
            f"표 외부 벡터 도형 {metrics.non_table_vector_count}개, "
            f"도식 영역 면적 {visual_area_ratio:.0%} → 벡터 도식/차트 중심 페이지"
        )
    elif metrics.aspect_ratio >= 1.15 and (
        (metrics.median_font_size is not None and metrics.median_font_size >= 14.0)
        or (metrics.usable_char_count <= 700 and metrics.line_count <= 22)
    ):
        category = PageCategory.SLIDE_LIKE
        primary_strategy = (
            ParseStrategy.MULTI_COLUMN_LAYOUT
            if metrics.column_count >= 2
            else ParseStrategy.NATIVE_TEXT
        )
        confidence = 0.88
        reasons.append(
            f"가로형 종횡비({metrics.aspect_ratio:.2f})와 "
            f"글꼴/밀도 특성(중앙 글꼴 {metrics.median_font_size or 0:.1f}pt, "
            f"{metrics.line_count}줄) → 슬라이드형 PDF 페이지"
        )
    elif metrics.column_count >= 2:
        category = PageCategory.MULTI_COLUMN
        primary_strategy = ParseStrategy.MULTI_COLUMN_LAYOUT
        confidence = 0.90
        gutter_desc = ", ".join(f"x={g:.0f}pt" for g in metrics.column_gutters_x)
        reasons.append(
            f"{metrics.column_count}단 레이아웃 여백({gutter_desc}) 감지 → "
            "다단 읽기 순서(XY-Cut) 적용"
        )
    elif (
        metrics.usable_char_count >= 40
        and metrics.visual_region_count >= 1
        and (
            metrics.image_coverage >= 0.10
            or visual_area_ratio >= 0.10
            or (metrics.visual_region_count >= 2 and visual_area_ratio >= 0.07)
        )
    ):
        category = PageCategory.HYBRID_TEXT_IMAGE
        primary_strategy = ParseStrategy.NATIVE_TEXT
        confidence = 0.89
        reasons.append(
            f"디지털 텍스트({metrics.usable_char_count}자)와 "
            f"시각 영역 {metrics.visual_region_count}개(면적 {visual_area_ratio:.0%}) 혼재 → "
            "텍스트 직접 추출 + 시각 영역 OCR/VLM 분기"
        )
    else:
        category = PageCategory.DIGITAL_TEXT
        primary_strategy = ParseStrategy.NATIVE_TEXT
        confidence = 0.94
        reasons.append(
            f"단일 컬럼 디지털 텍스트({metrics.usable_char_count}자, {metrics.line_count}줄) → "
            "결정론 네이티브 텍스트·계층 추출"
        )

    if metrics.table_count >= 1 and category != PageCategory.TABLE_HEAVY:
        reasons.append(f"페이지 내 보조 표 {metrics.table_count}개 병행 추출")
    if metrics.visual_region_count >= 1 and category not in (
        PageCategory.SCANNED_IMAGE,
        PageCategory.HYBRID_TEXT_IMAGE,
        PageCategory.VECTOR_DIAGRAM_HEAVY,
    ):
        reasons.append(f"페이지 내 시각 영역 {metrics.visual_region_count}개 크롭·OCR/VLM 태깅")
    if metrics.header_footer_line_count > 0:
        reasons.append(f"머리글/바닥글·쪽번호 {metrics.header_footer_line_count}줄 분리")

    # 개별 영역별 라우팅 목록 조립
    regions: list[RegionRoute] = []
    for t in tables:
        regions.append(
            RegionRoute(
                region_id=t.table_id,
                element_kind=ElementKind.TABLE,
                strategy=t.strategy,
                bbox=t.bbox,
                confidence=t.confidence,
                reason=t.reason,
                requires_vision=False,
            )
        )
    for v in visuals:
        regions.append(
            RegionRoute(
                region_id=v.region_id,
                element_kind=v.element_kind,
                strategy=v.recommended_strategy,
                bbox=v.bbox,
                confidence=v.confidence,
                reason=v.reason,
                requires_vision=True,
            )
        )
    for idx, blk in enumerate(layout_res.text_blocks, 1):
        regions.append(
            RegionRoute(
                region_id=f"p{page.page:03d}_b{idx:02d}",
                element_kind=blk.kind,
                strategy=blk.strategy,
                bbox=blk.bbox,
                confidence=blk.confidence,
                reason=(
                    f"제목 L{blk.heading_level}({blk.font_size:.1f}pt)"
                    if blk.kind == ElementKind.HEADING and blk.heading_level
                    else f"컬럼 {blk.column_index + 1} {blk.kind.value} 블록"
                ),
                requires_vision=False,
            )
        )
    regions.sort(key=lambda r: (r.bbox.top, r.bbox.x0))

    # 보조 전략 집계(주 전략과 중복 제거)
    all_strats = [r.strategy for r in regions]
    secondary_strategies = list(dict.fromkeys(s for s in all_strats if s != primary_strategy))

    return PageRouteDecision(
        page=page.page,
        category=category,
        primary_strategy=primary_strategy,
        secondary_strategies=secondary_strategies,
        confidence=confidence,
        reasons=reasons,
        metrics=metrics,
        regions=regions,
        requires_vision=len(visuals) > 0,
        warnings=warnings,
    )


def build_routing_plan(
    metadata: DocumentMetadata,
    outline: list[OutlineEntry],
    page_decisions: list[PageRouteDecision],
    *,
    body_font_size: float | None,
    running_header_detected: bool,
    running_footer_detected: bool,
) -> RoutingPlan:
    """문서 전체의 구조 진단·파싱 전략 계획(`RoutingPlan`)을 조립한다."""
    cat_counts: Counter[str] = Counter()
    strat_counts: Counter[str] = Counter()
    vision_pages: list[int] = []
    multi_col_pages: list[int] = []
    total_tables = 0
    total_visuals = 0

    for dec in page_decisions:
        cat_counts[dec.category.value] += 1
        strat_counts[dec.primary_strategy.value] += 1
        for sec in dec.secondary_strategies:
            strat_counts[sec.value] += 1
        total_tables += dec.metrics.table_count
        total_visuals += dec.metrics.visual_region_count
        if dec.requires_vision:
            vision_pages.append(dec.page)
        if dec.metrics.column_count >= 2:
            multi_col_pages.append(dec.page)

    summary = RoutingSummary(
        total_pages=metadata.page_count,
        analyzed_pages=len(page_decisions),
        category_counts=dict(sorted(cat_counts.items())),
        strategy_counts=dict(sorted(strat_counts.items())),
        table_count=total_tables,
        visual_region_count=total_visuals,
        vision_required_pages=vision_pages,
        multi_column_pages=multi_col_pages,
        detected_body_font_size=body_font_size,
        running_header_detected=running_header_detected,
        running_footer_detected=running_footer_detected,
    )
    return RoutingPlan(
        document=metadata,
        outline=outline,
        summary=summary,
        pages=page_decisions,
    )
