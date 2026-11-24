"""HWPX(KS X 6101 OWPML) 페이지 구조 분류기 및 파싱 전략 라우터."""

from __future__ import annotations

from autoetl.parser.contracts import DocumentMetadata, RoutingPlan
from autoetl.parser.docx.elements import RawDocxPage
from autoetl.parser.docx.router import (
    AnalyzedDocxPage,
    analyze_docx_page,
    build_docx_routing_plan,
    estimate_docx_body_font_size,
    find_repeating_docx_logos,
)

AnalyzedHwpxPage = AnalyzedDocxPage


def _relabel_to_hwpx(text: str) -> str:
    return (
        text.replace("DOCX ", "HWPX ")
        .replace("Word 섹션", "HWPX 섹션")
        .replace("Word 본문", "HWPX 본문")
        .replace("WordprocessingML", "OWPML")
        .replace("OOXML", "OWPML")
    )


def estimate_hwpx_body_font_size(pages: list[RawDocxPage]) -> float | None:
    """HWPX 문서 전체의 일반 본문(제목 제외) 글꼴 크기 중앙값을 추정한다."""
    return estimate_docx_body_font_size(pages)


def find_repeating_hwpx_logos(pages: list[RawDocxPage]) -> set[str]:
    """3페이지 이상 HWPX 문서에서 반복 등장하는 소형 로고 미디어 경로를 찾는다."""
    return find_repeating_docx_logos(pages)


def analyze_hwpx_page(
    page: RawDocxPage,
    *,
    doc_body_font_size: float | None,
    repeating_logos: set[str],
    include_comments: bool = False,
) -> AnalyzedHwpxPage:
    """단일 HWPX 페이지를 분석해 `AnalyzedHwpxPage`를 생성한다."""
    analyzed = analyze_docx_page(
        page,
        doc_body_font_size=doc_body_font_size,
        repeating_logos=repeating_logos,
        include_comments=include_comments,
    )
    updated_visuals = [
        v.model_copy(update={"reason": _relabel_to_hwpx(v.reason)}) for v in analyzed.visuals
    ]
    updated_regions = [
        r.model_copy(update={"reason": _relabel_to_hwpx(r.reason)}) for r in analyzed.route.regions
    ]
    updated_route = analyzed.route.model_copy(
        update={
            "reasons": [_relabel_to_hwpx(r) for r in analyzed.route.reasons],
            "regions": updated_regions,
        }
    )
    analyzed.visuals = updated_visuals
    analyzed.route = updated_route
    return analyzed


def build_hwpx_routing_plan(
    metadata: DocumentMetadata,
    analyzed_pages: list[AnalyzedHwpxPage],
    *,
    body_font_size: float | None,
) -> RoutingPlan:
    """분석된 HWPX 페이지들을 집계해 `RoutingPlan`을 조립한다."""
    return build_docx_routing_plan(
        metadata,
        analyzed_pages,
        body_font_size=body_font_size,
    )


__all__ = [
    "AnalyzedHwpxPage",
    "analyze_hwpx_page",
    "build_hwpx_routing_plan",
    "estimate_hwpx_body_font_size",
    "find_repeating_hwpx_logos",
]
