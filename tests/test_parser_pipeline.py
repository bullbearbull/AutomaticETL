"""파서 파이프라인 오케스트레이터 단위 테스트.

외부 바이너리·네트워크 없이 가짜 어댑터와 합성 IR로 다음을 검증한다:
- 페이지 선택 문자열 파싱의 오류·경계 입력
- 어댑터 레지스트리 등록/조회/미지원 형식 오류
- inspect/parse/parse_and_export의 옵션 전달과 크롭 디렉터리 기본값
- apply_vision_to_document의 사후 OCR/VLM 적용(성공·실패·예외·크롭 해석·문맥 추출)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from autoetl.parser import (
    BBox,
    DocumentBlock,
    DocumentMetadata,
    ElementKind,
    NoopVisionAdapter,
    PageCategory,
    PageMetrics,
    PageRouteDecision,
    ParsedDocument,
    ParsedPage,
    ParseOptions,
    ParserInputError,
    ParseStrategy,
    RoutingPlan,
    RoutingSummary,
    SourceFormat,
    VisionRequest,
    VisionResult,
    VisionStatus,
    VisualRegion,
    apply_vision_to_document,
    get_adapter_for_path,
    inspect_document,
    parse_and_export,
    parse_document,
    parse_page_selection,
    register_adapter,
    supported_extensions,
)
from autoetl.parser import pipeline as pipeline_mod
from autoetl.parser.adapters import PageLayoutDetector, PageRenderer, VisionParserAdapter
from autoetl.parser.chunker import ChunkOptions
from autoetl.parser.image_io import encode_png
from autoetl.parser.pdf.adapter import PdfDocumentAdapter

# ---------------------------------------------------------------------------
# 합성 IR 빌더 (전부 지어낸 값)
# ---------------------------------------------------------------------------


def _bbox() -> BBox:
    return BBox(
        x0=10.0,
        top=20.0,
        x1=110.0,
        bottom=120.0,
        norm_x0=0.05,
        norm_top=0.05,
        norm_x1=0.55,
        norm_bottom=0.55,
    )


def _text_block(block_id: str, page: int, order: int, text: str) -> DocumentBlock:
    return DocumentBlock(
        block_id=block_id,
        page=page,
        reading_order=order,
        kind=ElementKind.PARAGRAPH,
        strategy=ParseStrategy.NATIVE_TEXT,
        bbox=_bbox(),
        text=text,
    )


def _visual_block(block_id: str, page: int, order: int, region_id: str) -> DocumentBlock:
    return DocumentBlock(
        block_id=block_id,
        page=page,
        reading_order=order,
        kind=ElementKind.FIGURE_IMAGE,
        strategy=ParseStrategy.EMBEDDED_IMAGE_OCR,
        bbox=_bbox(),
        text="시각 영역 자리표시자",
        visual_region_id=region_id,
    )


def _table_block(block_id: str, page: int, order: int) -> DocumentBlock:
    return DocumentBlock(
        block_id=block_id,
        page=page,
        reading_order=order,
        kind=ElementKind.TABLE,
        strategy=ParseStrategy.RULED_TABLE,
        bbox=_bbox(),
        text="표 셀 텍스트",
        table_id="t1",
    )


def _visual(
    region_id: str,
    page: int,
    *,
    crop_path: str | None = None,
    status: VisionStatus = VisionStatus.PENDING,
    caption: str | None = None,
    hints: tuple[str, ...] = (),
) -> VisualRegion:
    return VisualRegion(
        region_id=region_id,
        page=page,
        element_kind=ElementKind.FIGURE_IMAGE,
        recommended_strategy=ParseStrategy.EMBEDDED_IMAGE_OCR,
        bbox=_bbox(),
        reason="합성 시각 영역",
        confidence=0.8,
        crop_path=crop_path,
        embedded_text_hints=list(hints),
        caption=caption,
        vision_status=status,
    )


def _page(page_no: int, blocks: list[DocumentBlock], visuals: list[VisualRegion]) -> ParsedPage:
    metrics = PageMetrics(page=page_no, width_pt=612.0, height_pt=792.0, aspect_ratio=612.0 / 792.0)
    route = PageRouteDecision(
        page=page_no,
        category=PageCategory.HYBRID_TEXT_IMAGE,
        primary_strategy=ParseStrategy.NATIVE_TEXT,
        confidence=0.9,
        metrics=metrics,
    )
    return ParsedPage(page=page_no, route=route, blocks=blocks, visual_regions=visuals)


def _doc(pages: list[ParsedPage]) -> ParsedDocument:
    meta = DocumentMetadata(
        source_path="합성문서.pdf",
        format=SourceFormat.PDF,
        file_size_bytes=1024,
        sha256="0" * 64,
        page_count=max(1, len(pages)),
    )
    plan = RoutingPlan(
        document=meta,
        summary=RoutingSummary(total_pages=max(1, len(pages)), analyzed_pages=max(1, len(pages))),
    )
    return ParsedDocument(
        document=meta,
        routing_plan=plan,
        pages=pages,
        visual_regions=[v for p in pages for v in p.visual_regions],
    )


def _png_bytes() -> bytes:
    return encode_png(bytes([200, 210, 220] * 4), 2, 2, channels=3)


class _FakeVisionAdapter:
    """요청을 기록하고 미리 정한 결과(또는 예외)를 돌려주는 가짜 OCR/VLM 어댑터."""

    def __init__(
        self,
        result: VisionResult | None = None,
        *,
        raises: bool = False,
        by_region: dict[str, VisionResult] | None = None,
    ) -> None:
        self.requests: list[VisionRequest] = []
        self._result = result
        self._raises = raises
        self._by_region = by_region or {}

    @property
    def name(self) -> str:
        return "fake-vision"

    def parse_region(self, request: VisionRequest) -> VisionResult:
        self.requests.append(request)
        if self._raises:
            raise RuntimeError("가짜 어댑터 장애")
        if request.region_id in self._by_region:
            return self._by_region[request.region_id]
        assert self._result is not None
        return self._result


class _FakeFormatAdapter:
    """호출 인자를 기록하고 합성 IR을 반환하는 가짜 문서 형식 어댑터."""

    def __init__(self, doc: ParsedDocument) -> None:
        self._doc = doc
        self.inspect_calls: list[tuple[Path, ParseOptions]] = []
        self.parse_calls: list[
            tuple[
                Path,
                ParseOptions,
                VisionParserAdapter | None,
                PageRenderer | None,
                PageLayoutDetector | None,
            ]
        ] = []

    @property
    def format(self) -> SourceFormat:
        return SourceFormat.PDF

    @property
    def extensions(self) -> tuple[str, ...]:
        return (".fake",)

    def inspect(
        self,
        source_path: Path,
        *,
        options: ParseOptions,
        layout_detector: PageLayoutDetector | None = None,
        page_renderer: PageRenderer | None = None,
    ) -> RoutingPlan:
        self.inspect_calls.append((source_path, options))
        return self._doc.routing_plan

    def parse(
        self,
        source_path: Path,
        *,
        options: ParseOptions,
        vision_adapter: VisionParserAdapter | None = None,
        page_renderer: PageRenderer | None = None,
        layout_detector: PageLayoutDetector | None = None,
    ) -> ParsedDocument:
        self.parse_calls.append(
            (source_path, options, vision_adapter, page_renderer, layout_detector)
        )
        return self._doc


# ---------------------------------------------------------------------------
# 1. 페이지 선택 문자열 파싱: 오류·경계 입력
# ---------------------------------------------------------------------------


def test_parse_page_selection_rejects_empty_token_and_malformed_range() -> None:
    """빈 토큰·잘린 범위·숫자 아닌 범위 끝은 모두 ParserInputError로 거부한다."""
    with pytest.raises(ParserInputError, match="페이지 범위"):
        parse_page_selection("1,,2")
    with pytest.raises(ParserInputError, match="페이지 범위"):
        parse_page_selection("1-2-3")
    with pytest.raises(ParserInputError, match="페이지 범위"):
        parse_page_selection("a-2")
    with pytest.raises(ParserInputError, match="페이지 범위"):
        parse_page_selection("1-b")
    with pytest.raises(ParserInputError, match="페이지 범위"):
        parse_page_selection("-3")
    with pytest.raises(ParserInputError, match="페이지 범위"):
        parse_page_selection("3-")
    with pytest.raises(ParserInputError, match="올바르지 않다"):
        parse_page_selection("0-3")


def test_parse_page_selection_dedup_and_whitespace() -> None:
    """공백 제거·중복 제거(범위 겹침 포함) 후 입력 순서를 유지한다."""
    assert parse_page_selection(" 2 , 1-3 , 2 ") == (2, 1, 3)
    assert parse_page_selection("1-3,2-4") == (1, 2, 3, 4)
    assert parse_page_selection("007") == (7,)


# ---------------------------------------------------------------------------
# 2. 어댑터 레지스트리
# ---------------------------------------------------------------------------


def test_registry_defaults_case_insensitive_and_unsupported() -> None:
    """기본 8종 확장자 등록, 대소문자 무시 조회, 미지원/무확장자 오류를 검증한다."""
    exts = supported_extensions()
    assert {".pdf", ".pptx", ".docx", ".hwpx", ".hwp", ".doc", ".ppt", ".xlsx"} <= set(exts)
    assert isinstance(get_adapter_for_path(Path("문서.PdF")), PdfDocumentAdapter)

    with pytest.raises(ParserInputError, match="지원하지 않는 문서 형식"):
        get_adapter_for_path(Path("메모.xyz"))
    with pytest.raises(ParserInputError, match="확장자 없음"):
        get_adapter_for_path(Path("확장자없는파일"))


def test_registry_empty_message_and_custom_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """빈 레지스트리의 '(없음)' 안내와 커스텀 어댑터 등록·조회를 검증한다."""
    original = dict(pipeline_mod._ADAPTER_REGISTRY)
    monkeypatch.setattr(pipeline_mod, "_ADAPTER_REGISTRY", {})
    with pytest.raises(ParserInputError, match="없음"):
        get_adapter_for_path(Path("a.pdf"))

    # 원본 레지스트리 복사본에 등록해 다른 테스트에 영향을 주지 않는다.
    monkeypatch.setattr(pipeline_mod, "_ADAPTER_REGISTRY", dict(original))
    fake = _FakeFormatAdapter(_doc([]))
    register_adapter(fake)
    assert ".fake" in supported_extensions()
    assert ".pdf" in supported_extensions()
    assert get_adapter_for_path(Path("sample.FAKE")) is fake


# ---------------------------------------------------------------------------
# 3. inspect/parse/parse_and_export 오케스트레이션
# ---------------------------------------------------------------------------


def test_inspect_and_parse_dispatch_options_and_plugins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """확장자 라우팅, 기본 옵션 생성, 플러그인 인자 전달을 검증한다."""
    doc = _doc([_page(1, [_text_block("b1", 1, 0, "본문")], [])])
    fake = _FakeFormatAdapter(doc)
    monkeypatch.setattr(pipeline_mod, "_ADAPTER_REGISTRY", dict(pipeline_mod._ADAPTER_REGISTRY))
    register_adapter(fake)

    plan = inspect_document("sample.fake")
    assert plan is doc.routing_plan
    assert len(fake.inspect_calls) == 1
    path, opts = fake.inspect_calls[0]
    assert path == Path("sample.fake")
    assert opts == ParseOptions()  # options=None -> 기본값

    vision = _FakeVisionAdapter(VisionResult(adapter_name="fake-vision", succeeded=True))
    result = parse_document(
        Path("sample.fake"),
        options=ParseOptions(pages=(1,)),
        vision_adapter=vision,
    )
    assert result is doc
    assert len(fake.parse_calls) == 1
    p_path, p_opts, p_vision, p_renderer, p_detector = fake.parse_calls[0]
    assert p_path == Path("sample.fake")
    assert p_opts.pages == (1,)
    assert p_vision is vision
    assert p_renderer is None
    assert p_detector is None


def test_parse_and_export_crop_dir_defaults_and_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """크롭 디렉터리 기본값/재정의, 청크 포함 여부, 번들 파일 생성을 검증한다."""
    doc = _doc([_page(1, [_text_block("b1", 1, 0, "번들용 본문 텍스트")], [])])
    fake = _FakeFormatAdapter(doc)
    monkeypatch.setattr(pipeline_mod, "_ADAPTER_REGISTRY", dict(pipeline_mod._ADAPTER_REGISTRY))
    register_adapter(fake)
    src = tmp_path / "input.fake"
    src.write_bytes(b"fake")

    # 1) extract_crops=True + crop_dir 미지정 -> out_dir/crops 기본값
    out1 = tmp_path / "out1"
    got_doc, manifest = parse_and_export(
        src, out1, options=ParseOptions(render_dpi=200), chunk_options=ChunkOptions(max_chars=500)
    )
    assert got_doc is doc
    opts1 = fake.parse_calls[-1][1]
    assert opts1.crop_dir == out1 / "crops"
    assert opts1.render_dpi == 200
    assert manifest.parsed_json.is_file()
    assert manifest.document_markdown.is_file()
    assert manifest.routing_report_json.is_file()
    assert manifest.routing_report_markdown.is_file()
    assert manifest.chunks_jsonl is not None and manifest.chunks_jsonl.is_file()
    assert manifest.chunk_count >= 1

    # 2) 사용자 지정 crop_dir 유지
    custom = tmp_path / "my_crops"
    parse_and_export(src, tmp_path / "out2", options=ParseOptions(crop_dir=custom))
    assert fake.parse_calls[-1][1].crop_dir == custom

    # 3) extract_crops=False -> crop_dir None, include_chunks=False -> 청크 미생성
    _, manifest3 = parse_and_export(
        src,
        tmp_path / "out3",
        options=ParseOptions(extract_crops=False),
        include_chunks=False,
    )
    assert fake.parse_calls[-1][1].crop_dir is None
    assert manifest3.chunks_jsonl is None
    assert manifest3.chunk_count == 0


# ---------------------------------------------------------------------------
# 4. apply_vision_to_document 사후 적용
# ---------------------------------------------------------------------------


def test_apply_vision_noop_returns_same_document() -> None:
    """NoopVisionAdapter는 문서를 그대로 반환한다."""
    doc = _doc([_page(1, [], [_visual("r1", 1)])])
    assert apply_vision_to_document(doc, NoopVisionAdapter()) is doc


def test_apply_vision_success_reads_crop_and_context(tmp_path: Path) -> None:
    """성공 결과의 상태·메타데이터·블록 텍스트 갱신과 크롭/문맥 전달을 검증한다."""
    crop = tmp_path / "crop.png"
    png = _png_bytes()
    crop.write_bytes(png)

    blocks = [
        _text_block("b0", 1, 0, "앞 문맥 텍스트"),
        _visual_block("b1", 1, 1, "r1"),
        _table_block("b2", 1, 2),
        _text_block("b3", 1, 3, "   "),
        _text_block("b4", 1, 4, "뒤 문맥 텍스트"),
    ]
    vis = _visual("r1", 1, crop_path=str(crop), hints=("힌트A",), caption="캡션")
    doc = _doc([_page(1, blocks, [vis])])
    adapter = _FakeVisionAdapter(
        VisionResult(
            adapter_name="fake-vision",
            succeeded=True,
            text="인식된 텍스트",
            markdown="",  # markdown이 비면 text로 폴백
            metadata={"engine": "fake"},
        )
    )
    out = apply_vision_to_document(doc, adapter)

    assert len(adapter.requests) == 1
    req = adapter.requests[0]
    assert req.region_id == "r1"
    assert req.image_bytes == png
    assert req.image_extension == ".png"
    assert req.crop_path == crop.as_posix()
    assert req.embedded_text_hints == ("힌트A",)
    assert req.caption == "캡션"
    # 표 블록·공백 블록은 건너뛰고 가장 가까운 본문 블록에서 문맥을 추출
    assert req.context_before == "앞 문맥 텍스트"
    assert req.context_after == "뒤 문맥 텍스트"

    out_vis = out.visual_regions[0]
    assert out_vis.vision_status == VisionStatus.COMPLETED
    assert out_vis.vision_adapter == "fake-vision"
    assert out_vis.vision_text == "인식된 텍스트"
    assert out_vis.vision_markdown == "인식된 텍스트"
    assert out_vis.vision_metadata == {"engine": "fake"}
    assert out.pages[0].blocks[1].text == "인식된 텍스트"
    # 나머지 블록은 그대로
    assert out.pages[0].blocks[0].text == "앞 문맥 텍스트"
    assert out.pages[0].blocks[2].text == "표 셀 텍스트"


def test_apply_vision_markdown_priority_and_caption_fallback(tmp_path: Path) -> None:
    """블록 텍스트는 markdown > text > 캡션 > 원본 순으로 폴백한다."""
    vis_a = _visual("ra", 1)
    vis_b = _visual("rb", 1, caption="캡션B")
    vis_c = _visual("rc", 1)
    blocks = [
        _visual_block("ba", 1, 0, "ra"),
        _visual_block("bb", 1, 1, "rb"),
        _visual_block("bc", 1, 2, "rc"),
    ]
    doc = _doc([_page(1, blocks, [vis_a, vis_b, vis_c])])
    adapter = _FakeVisionAdapter(
        by_region={
            "ra": VisionResult(
                adapter_name="fake-vision",
                succeeded=True,
                text="평문",
                markdown="# 마크다운 우선",
            ),
            "rb": VisionResult(adapter_name="fake-vision", succeeded=True),  # 빈 텍스트
            "rc": VisionResult(adapter_name="fake-vision", succeeded=True),  # 빈 텍스트+캡션 없음
        }
    )
    out = apply_vision_to_document(doc, adapter)
    assert out.pages[0].blocks[0].text == "# 마크다운 우선"
    assert out.pages[0].blocks[1].text == "캡션B"
    assert out.pages[0].blocks[2].text == "시각 영역 자리표시자"


def test_apply_vision_only_pending_skips_completed() -> None:
    """only_pending=True는 COMPLETED 영역을 건너뛰고, False는 다시 처리한다."""
    done = _visual("r_done", 1, status=VisionStatus.COMPLETED)
    done = done.model_copy(update={"vision_text": "기존 결과", "vision_adapter": "old"})
    pending = _visual("r_pending", 1)
    doc = _doc([_page(1, [], [done, pending])])
    adapter = _FakeVisionAdapter(
        VisionResult(adapter_name="fake-vision", succeeded=True, text="새 결과")
    )

    out = apply_vision_to_document(doc, adapter, only_pending=True)
    assert [r.region_id for r in adapter.requests] == ["r_pending"]
    assert out.visual_regions[0].vision_text == "기존 결과"
    assert out.visual_regions[0].vision_adapter == "old"
    assert out.visual_regions[1].vision_text == "새 결과"

    adapter2 = _FakeVisionAdapter(
        VisionResult(adapter_name="fake-vision", succeeded=True, text="재처리")
    )
    out2 = apply_vision_to_document(doc, adapter2, only_pending=False)
    assert [r.region_id for r in adapter2.requests] == ["r_done", "r_pending"]
    assert out2.visual_regions[0].vision_text == "재처리"


def test_apply_vision_adapter_exception_marks_failed() -> None:
    """어댑터 예외는 FAILED(adapter_exception)로 기록하고 블록은 캡션으로 폴백한다."""
    vis = _visual("r1", 1, caption="예외 시 캡션")
    doc = _doc([_page(1, [_visual_block("b1", 1, 0, "r1")], [vis])])
    out = apply_vision_to_document(doc, _FakeVisionAdapter(raises=True))
    out_vis = out.visual_regions[0]
    assert out_vis.vision_status == VisionStatus.FAILED
    assert out_vis.vision_adapter == "fake-vision"
    assert out_vis.vision_metadata == {"vision_error": "adapter_exception"}
    assert out.pages[0].blocks[0].text == "예외 시 캡션"


def test_apply_vision_failure_result_merges_metadata() -> None:
    """실패 결과의 메타데이터에 vision_error를 합치고, error 없으면 추가하지 않는다."""
    vis_a = _visual("ra", 1)
    vis_b = _visual("rb", 1)
    doc = _doc([_page(1, [], [vis_a, vis_b])])
    adapter = _FakeVisionAdapter(
        by_region={
            "ra": VisionResult(
                adapter_name="fake-vision",
                succeeded=False,
                metadata={"stage": "ocr"},
                error="timeout",
            ),
            "rb": VisionResult(
                adapter_name="fake-vision", succeeded=False, metadata={"stage": "vlm"}
            ),
        }
    )
    out = apply_vision_to_document(doc, adapter)
    assert out.visual_regions[0].vision_status == VisionStatus.FAILED
    assert out.visual_regions[0].vision_metadata == {"stage": "ocr", "vision_error": "timeout"}
    assert out.visual_regions[1].vision_metadata == {"stage": "vlm"}


def test_apply_vision_relative_crop_resolution(tmp_path: Path) -> None:
    """상대 크롭 경로는 base_dir로 해석하고, 없으면 원본 경로·이미지 None으로 넘긴다."""
    rel_dir = tmp_path / "crops"
    rel_dir.mkdir()
    png = _png_bytes()
    (rel_dir / "rel.png").write_bytes(png)

    vis_found = _visual("r_found", 1, crop_path="crops/rel.png")
    vis_missing = _visual("r_missing", 1, crop_path="crops/없음.png")
    doc = _doc([_page(1, [], [vis_found, vis_missing])])
    adapter = _FakeVisionAdapter(
        VisionResult(adapter_name="fake-vision", succeeded=True, text="ok")
    )
    apply_vision_to_document(doc, adapter, base_dir=tmp_path)

    req_found, req_missing = adapter.requests
    assert req_found.crop_path == (rel_dir / "rel.png").as_posix()
    assert req_found.image_bytes == png
    assert req_found.image_extension == ".png"
    assert req_missing.crop_path == "crops/없음.png"
    assert req_missing.image_bytes is None
    assert req_missing.image_extension is None


def test_apply_vision_crop_read_oserror_keeps_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """크롭 읽기가 OSError여도 경로는 유지하고 이미지 없이 요청을 보낸다."""
    crop = tmp_path / "unreadable.png"
    crop.write_bytes(_png_bytes())

    def _raise_oserror(self: Path) -> bytes:
        raise OSError("가짜 읽기 실패")

    monkeypatch.setattr(Path, "read_bytes", _raise_oserror)
    vis = _visual("r1", 1, crop_path=str(crop))
    doc = _doc([_page(1, [], [vis])])
    adapter = _FakeVisionAdapter(
        VisionResult(adapter_name="fake-vision", succeeded=True, text="ok")
    )
    apply_vision_to_document(doc, adapter)

    req = adapter.requests[0]
    assert req.image_bytes is None
    assert req.crop_path == crop.as_posix()
    assert req.image_extension == ".png"


def test_apply_vision_context_skips_visual_blocks_and_clips() -> None:
    """문맥 추출은 다른 시각 블록을 건너뛰고 240자로 자른다."""
    long_before = "가" * 300
    blocks = [
        _text_block("b0", 1, 0, long_before),
        _visual_block("b1", 1, 1, "r_other"),  # vis_map에 없는 다른 시각 블록
        _visual_block("b2", 1, 2, "r1"),
        _text_block("b3", 1, 3, "뒤 문맥"),
    ]
    vis = _visual("r1", 1)
    doc = _doc([_page(1, blocks, [vis])])
    adapter = _FakeVisionAdapter(
        VisionResult(adapter_name="fake-vision", succeeded=True, text="ok")
    )
    out = apply_vision_to_document(doc, adapter)
    req = adapter.requests[0]
    assert req.context_before == "가" * 240
    assert req.context_after == "뒤 문맥"
    # 매핑에 없는 시각 블록은 원본 그대로
    assert out.pages[0].blocks[1].text == "시각 영역 자리표시자"


def test_apply_vision_aggregates_multiple_pages() -> None:
    """여러 페이지의 시각 영역을 페이지 순서대로 통합 갱신한다."""
    doc = _doc(
        [
            _page(1, [_visual_block("b1", 1, 0, "r1")], [_visual("r1", 1)]),
            _page(2, [_visual_block("b2", 2, 0, "r2")], [_visual("r2", 2)]),
        ]
    )
    adapter = _FakeVisionAdapter(
        by_region={
            "r1": VisionResult(adapter_name="fake-vision", succeeded=True, text="페이지1"),
            "r2": VisionResult(adapter_name="fake-vision", succeeded=True, text="페이지2"),
        }
    )
    out = apply_vision_to_document(doc, adapter)
    assert [v.region_id for v in out.visual_regions] == ["r1", "r2"]
    assert out.pages[0].blocks[0].text == "페이지1"
    assert out.pages[1].blocks[0].text == "페이지2"
    assert out.pages[0].visual_regions[0].vision_text == "페이지1"
    assert out.pages[1].visual_regions[0].vision_text == "페이지2"
