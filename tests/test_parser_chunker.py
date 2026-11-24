"""RAG/LLM 구조화 문서 청킹(`autoetl.parser.chunker`) 단위·통합 테스트."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from autoetl.cli import main
from autoetl.parser import (
    BBox,
    ChunkOptions,
    DocumentBlock,
    DocumentChunk,
    DocumentMetadata,
    ElementKind,
    ExtractedTable,
    PageCategory,
    PageMetrics,
    PageRouteDecision,
    ParsedDocument,
    ParsedPage,
    ParseStrategy,
    RoutingPlan,
    RoutingSummary,
    SourceFormat,
    VisionStatus,
    VisualRegion,
    chunk_document,
    estimate_token_count,
    parse_and_export,
    render_chunks_jsonl,
)


def _bbox() -> BBox:
    return BBox(
        x0=0.0,
        top=0.0,
        x1=500.0,
        bottom=50.0,
        norm_x0=0.0,
        norm_top=0.0,
        norm_x1=0.8,
        norm_bottom=0.1,
    )


def _make_sample_parsed_doc(*, table_rows: int = 12) -> ParsedDocument:
    meta = DocumentMetadata(
        source_path="spec.docx",
        format=SourceFormat.DOCX,
        file_size_bytes=4096,
        sha256="abcdef1234567890" * 4,
        page_count=2,
        title="청킹 검증 문서",
    )
    tbl = ExtractedTable(
        table_id="p001_t01",
        page=1,
        bbox=_bbox(),
        strategy=ParseStrategy.RULED_TABLE,
        confidence=0.98,
        row_count=table_rows + 1,
        col_count=3,
        headers=["항목코드", "항목명", "상세 설명"],
        rows=[
            [f"ITEM-{i:02d}", f"세부 지표 {i}", f"이 항목은 {i}번째 월별 지표 설명 문구입니다."]
            for i in range(1, table_rows + 1)
        ],
        caption="표 1. 월별 핵심 지표",
    )
    vis = VisualRegion(
        region_id="p002_v01",
        page=2,
        element_kind=ElementKind.FIGURE_IMAGE,
        recommended_strategy=ParseStrategy.VLM_REGION,
        bbox=_bbox(),
        reason="아키텍처 도식",
        confidence=0.92,
        caption="그림 1. 시스템 구성도",
        vision_status=VisionStatus.COMPLETED,
        vision_adapter="test-vlm",
        vision_markdown="클라이언트 -> API 게이트웨이 -> 파서 엔진",
    )
    route1 = PageRouteDecision(
        page=1,
        category=PageCategory.TABLE_HEAVY,
        primary_strategy=ParseStrategy.RULED_TABLE,
        confidence=0.95,
        metrics=PageMetrics(page=1, width_pt=595.0, height_pt=842.0, aspect_ratio=0.71),
    )
    route2 = PageRouteDecision(
        page=2,
        category=PageCategory.HYBRID_TEXT_IMAGE,
        primary_strategy=ParseStrategy.NATIVE_TEXT,
        confidence=0.95,
        metrics=PageMetrics(page=2, width_pt=595.0, height_pt=842.0, aspect_ratio=0.71),
    )
    p1 = ParsedPage(
        page=1,
        route=route1,
        blocks=[
            DocumentBlock(
                block_id="b1",
                page=1,
                reading_order=0,
                kind=ElementKind.HEADING,
                strategy=ParseStrategy.NATIVE_TEXT,
                bbox=_bbox(),
                text="1. 사업 개요",
                heading_level=1,
            ),
            DocumentBlock(
                block_id="b2",
                page=1,
                reading_order=1,
                kind=ElementKind.HEADING,
                strategy=ParseStrategy.NATIVE_TEXT,
                bbox=_bbox(),
                text="1.1 추진 배경",
                heading_level=2,
            ),
            DocumentBlock(
                block_id="b3",
                page=1,
                reading_order=2,
                kind=ElementKind.PARAGRAPH,
                strategy=ParseStrategy.NATIVE_TEXT,
                bbox=_bbox(),
                text=("한국어 공공문서와 기업 문서를 구조 보존 방식으로 추출한다. " * 12).strip(),
            ),
            DocumentBlock(
                block_id="b4",
                page=1,
                reading_order=3,
                kind=ElementKind.TABLE,
                strategy=ParseStrategy.RULED_TABLE,
                bbox=_bbox(),
                text="",
                table_id="p001_t01",
            ),
        ],
        tables=[tbl],
    )
    p2 = ParsedPage(
        page=2,
        route=route2,
        blocks=[
            DocumentBlock(
                block_id="b5",
                page=2,
                reading_order=0,
                kind=ElementKind.HEADING,
                strategy=ParseStrategy.NATIVE_TEXT,
                bbox=_bbox(),
                text="2. 시스템 아키텍처",
                heading_level=1,
            ),
            DocumentBlock(
                block_id="b6",
                page=2,
                reading_order=1,
                kind=ElementKind.FIGURE_IMAGE,
                strategy=ParseStrategy.VLM_REGION,
                bbox=_bbox(),
                text="",
                visual_region_id="p002_v01",
            ),
        ],
        visual_regions=[vis],
    )
    plan = RoutingPlan(
        document=meta,
        summary=RoutingSummary(
            total_pages=2, analyzed_pages=2, table_count=1, visual_region_count=1
        ),
        pages=[route1, route2],
    )
    return ParsedDocument(
        document=meta,
        routing_plan=plan,
        pages=[p1, p2],
        tables=[tbl],
        visual_regions=[vis],
    )


def test_chunk_document_preserves_hierarchy_table_rows_and_vision() -> None:
    """제목 계층 경로, 초대형 표의 헤더 반복 행 분할, VLM 시각 영역 보존을 검증한다."""
    doc = _make_sample_parsed_doc(table_rows=14)
    chunks = chunk_document(doc, options=ChunkOptions(max_chars=360, overlap_chars=60))
    assert len(chunks) >= 4

    # 1. 산문 청크는 [1. 사업 개요 > 1.1 추진 배경] 계층과 페이지 번호를 보존
    prose_chunks = [c for c in chunks if ElementKind.PARAGRAPH in c.element_kinds]
    assert prose_chunks
    assert all(c.section_path == ["1. 사업 개요", "1.1 추진 배경"] for c in prose_chunks)
    assert all(c.page_start == 1 and c.page_end == 1 for c in prose_chunks)
    assert prose_chunks[0].text.startswith("[1. 사업 개요 > 1.1 추진 배경]\n")

    # 2. 표 청크는 행 중간이 절단되지 않고 모든 분할 청크에 Markdown 헤더 행이 반복됨
    tbl_chunks = [c for c in chunks if ElementKind.TABLE in c.element_kinds]
    assert len(tbl_chunks) >= 2
    for tc in tbl_chunks:
        assert tc.table_ids == ["p001_t01"]
        assert "| 항목코드 | 항목명 | 상세 설명 |" in tc.text
        assert "| --- | --- | --- |" in tc.text
        for ln in tc.text.splitlines():
            if ln.startswith("|"):
                assert ln.endswith("|")
    joined_tbls = "\n".join(c.text for c in tbl_chunks)
    assert "ITEM-01" in joined_tbls
    assert "ITEM-14" in joined_tbls

    # 3. 2페이지 새 H1 진입 시 이전 H2 스택이 정리되고 VLM 해석 텍스트가 반영됨
    p2_chunks = [c for c in chunks if c.page_start == 2]
    assert len(p2_chunks) == 1
    assert p2_chunks[0].section_path == ["2. 시스템 아키텍처"]
    assert p2_chunks[0].visual_region_ids == ["p002_v01"]
    assert "API 게이트웨이" in p2_chunks[0].text

    # 4. JSONL 왕복 직렬화 및 토큰 추정
    jsonl = render_chunks_jsonl(chunks)
    parsed_back = [DocumentChunk.model_validate_json(ln) for ln in jsonl.splitlines() if ln.strip()]
    assert len(parsed_back) == len(chunks)
    assert estimate_token_count("한국어 테스트 hello world") > 0


def test_parse_and_export_and_cli_chunks_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`parse_and_export` 번들과 `autoetl parse --format chunks` CLI가 `chunks.jsonl`을 생성한다."""
    docx_path = tmp_path / "mini.docx"
    with zipfile.ZipFile(docx_path, "w") as zf:
        zf.writestr(
            "word/document.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body>"
            "<w:p><w:r><w:t>청킹 번들 통합 검증 본문입니다.</w:t></w:r></w:p>"
            "</w:body></w:document>",
        )

    out_dir = tmp_path / "bundle"
    _, manifest = parse_and_export(docx_path, out_dir)
    assert manifest.chunks_jsonl is not None
    assert manifest.chunks_jsonl.is_file()
    assert manifest.chunk_count >= 1
    lines = manifest.chunks_jsonl.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    assert first["source_path"] == "mini.docx"
    assert "청킹 번들 통합 검증" in first["text"]

    # CLI --format chunks stdout 출력
    assert main(["parse", str(docx_path), "--format", "chunks"]) == 0
    stdout_lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert len(stdout_lines) >= 1
    assert json.loads(stdout_lines[0])["format"] == "docx"

    # CLI --no-chunks 생략 확인
    out_no_chunks = tmp_path / "bundle_no_chunks"
    assert main(["parse", str(docx_path), "--no-chunks", "--out", str(out_no_chunks)]) == 0
    capsys.readouterr()
    assert not (out_no_chunks / "chunks.jsonl").exists()


def test_chunk_document_emits_heading_only_sections_with_pages() -> None:
    """연속 제목 전용 슬라이드와 문서 끝 빈 제목 섹션이 원래 페이지 번호와 함께 청크로 남는다."""
    meta = DocumentMetadata(
        source_path="title_slides.pptx",
        format=SourceFormat.PPTX,
        file_size_bytes=512,
        sha256="b" * 64,
        page_count=3,
    )

    def _slide(
        num: int, title: str, body: str | None = None
    ) -> tuple[ParsedPage, PageRouteDecision]:
        route = PageRouteDecision(
            page=num,
            category=PageCategory.SLIDE_LIKE,
            primary_strategy=ParseStrategy.NATIVE_TEXT,
            confidence=0.95,
            metrics=PageMetrics(page=num, width_pt=720.0, height_pt=540.0, aspect_ratio=1.33),
        )
        blocks = [
            DocumentBlock(
                block_id=f"p{num:03d}_b01",
                page=num,
                reading_order=0,
                kind=ElementKind.HEADING,
                heading_level=1,
                bbox=_bbox(),
                text=title,
                strategy=ParseStrategy.NATIVE_TEXT,
            )
        ]
        if body:
            blocks.append(
                DocumentBlock(
                    block_id=f"p{num:03d}_b02",
                    page=num,
                    reading_order=1,
                    kind=ElementKind.PARAGRAPH,
                    bbox=_bbox(),
                    text=body,
                    strategy=ParseStrategy.NATIVE_TEXT,
                )
            )
        return ParsedPage(page=num, route=route, blocks=blocks), route

    # 1. 3장 모두 제목 전용 슬라이드
    s1, r1 = _slide(1, "First slide")
    s2, r2 = _slide(2, "Second slide")
    s3, r3 = _slide(3, "Third slide")
    doc_titles = ParsedDocument(
        document=meta,
        routing_plan=RoutingPlan(
            document=meta,
            summary=RoutingSummary(total_pages=3, analyzed_pages=3),
            pages=[r1, r2, r3],
        ),
        pages=[s1, s2, s3],
    )
    title_chunks = chunk_document(doc_titles)
    assert [(c.text, c.page_start, c.page_end) for c in title_chunks] == [
        ("First slide", 1, 1),
        ("Second slide", 2, 2),
        ("Third slide", 3, 3),
    ]
    assert all(c.element_kinds == [ElementKind.HEADING] for c in title_chunks)

    # 2. 본문 섹션 + 중간 빈 제목 + 끝 빈 제목 혼합
    m1, mr1 = _slide(1, "Intro", "본문 내용이 있는 첫 페이지입니다.")
    m2, mr2 = _slide(2, "Divider Slide")
    m3, mr3 = _slide(3, "Conclusion", "결론 본문입니다.")
    m4, mr4 = _slide(4, "Appendix Cover")
    doc_mixed = ParsedDocument(
        document=meta.model_copy(update={"page_count": 4}),
        routing_plan=RoutingPlan(
            document=meta.model_copy(update={"page_count": 4}),
            summary=RoutingSummary(total_pages=4, analyzed_pages=4),
            pages=[mr1, mr2, mr3, mr4],
        ),
        pages=[m1, m2, m3, m4],
    )
    mixed_chunks = chunk_document(doc_mixed)
    assert len(mixed_chunks) == 4
    assert mixed_chunks[0].page_start == 1 and "Intro" in mixed_chunks[0].section_path
    assert mixed_chunks[1].text == "Divider Slide" and mixed_chunks[1].page_start == 2
    assert mixed_chunks[2].page_start == 3 and "Conclusion" in mixed_chunks[2].section_path
    assert mixed_chunks[3].text == "Appendix Cover" and mixed_chunks[3].page_start == 4
