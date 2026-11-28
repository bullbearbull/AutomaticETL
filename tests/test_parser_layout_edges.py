"""레이아웃 탐지기(layout_detector)의 경계·오류 경로·가짜 세션/서비스 기반 단위 테스트."""

from __future__ import annotations

import base64
import importlib
import struct
import urllib.error
import zlib
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from autoetl.parser import layout_detector as ld
from autoetl.parser.adapters import (
    DetectedLayoutBox,
    LayoutDetectionInput,
    NoopLayoutDetector,
    ParserInputError,
)
from autoetl.parser.contracts import ElementKind, ParseStrategy
from autoetl.parser.image_io import encode_png
from autoetl.parser.layout_detector import (
    DOCLAYNET_LABELS,
    PP_DOCLAYOUT_V2_LABELS,
    HeuristicRasterLayoutDetector,
    HttpLayoutDetector,
    OnnxDocLayoutDetector,
    apply_layout_nms,
    boxes_to_visual_regions,
    create_layout_detector,
    normalize_layout_label,
    sort_layout_boxes_reading_order,
)
from autoetl.parser.vision import VisionSecurityError
from autoetl.security.levels import TrustZone


def _box(
    label: str,
    kind: ElementKind,
    bbox: tuple[float, float, float, float],
    score: float = 0.9,
    *,
    strategy: ParseStrategy = ParseStrategy.EMBEDDED_IMAGE_OCR,
    reading_order: int = 0,
    reason: str = "",
) -> DetectedLayoutBox:
    return DetectedLayoutBox(
        raw_label=label,
        element_kind=kind,
        recommended_strategy=strategy,
        bbox_px=bbox,
        score=score,
        reading_order=reading_order,
        reason=reason,
    )


def _inp(png: bytes, w: int, h: int) -> LayoutDetectionInput:
    return LayoutDetectionInput(
        page=1,
        image_bytes=png,
        image_extension=".png",
        image_width_px=w,
        image_height_px=h,
        page_width_pt=595.0,
        page_height_pt=842.0,
    )


def _fill_gray(
    buf: bytearray, width: int, height: int, x0: int, y0: int, x1: int, y1: int, value: int
) -> None:
    for y in range(max(0, y0), min(height, y1)):
        row = y * width
        for x in range(max(0, x0), min(width, x1)):
            buf[row + x] = value


def _chunk(ctype: bytes, payload: bytes) -> bytes:
    crc = zlib.crc32(payload, zlib.crc32(ctype)) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + ctype + payload + struct.pack(">I", crc)


def _gray_alpha_png(width: int, height: int, gray: bytes) -> bytes:
    scanlines = bytearray()
    for y in range(height):
        scanlines.append(0)
        for x in range(width):
            scanlines.extend((gray[y * width + x], 255))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 4, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(bytes(scanlines)))
        + _chunk(b"IEND", b"")
    )


# ---------------------------------------------------------------------------
# 라벨 정규화 추가 매핑
# ---------------------------------------------------------------------------


def test_normalize_layout_label_vector_list_and_unknown() -> None:
    assert normalize_layout_label("vector_diagram") == (
        ElementKind.VECTOR_DIAGRAM,
        ParseStrategy.VLM_REGION,
    )
    assert normalize_layout_label("FLOWCHART") == (
        ElementKind.VECTOR_DIAGRAM,
        ParseStrategy.VLM_REGION,
    )
    assert normalize_layout_label("  Table Body  ") == (
        ElementKind.TABLE,
        ParseStrategy.VLM_REGION,
    )
    assert normalize_layout_label("bullet") == (
        ElementKind.LIST_ITEM,
        ParseStrategy.EMBEDDED_IMAGE_OCR,
    )
    assert normalize_layout_label("page_number") == (
        ElementKind.HEADER_FOOTER,
        ParseStrategy.SKIP_DECORATIVE,
    )
    assert normalize_layout_label("totally_unknown") == (
        ElementKind.PARAGRAPH,
        ParseStrategy.EMBEDDED_IMAGE_OCR,
    )


# ---------------------------------------------------------------------------
# NMS 경계: 빈 입력·영면적·동점·교차 유형·포함 임계값
# ---------------------------------------------------------------------------


def test_nms_empty_and_zero_area_boxes() -> None:
    assert apply_layout_nms([]) == []
    zero_area = _box("table", ElementKind.TABLE, (5.0, 5.0, 5.0, 10.0))
    assert apply_layout_nms([zero_area]) == []


def test_nms_tie_score_keeps_larger_area_via_containment() -> None:
    big = _box("table", ElementKind.TABLE, (0.0, 0.0, 100.0, 100.0), score=0.8)
    small = _box("table", ElementKind.TABLE, (0.0, 0.0, 50.0, 50.0), score=0.8)
    kept = apply_layout_nms([small, big])
    assert kept == [big]


def test_nms_cross_kind_near_identical_and_containment_threshold() -> None:
    table = _box("table", ElementKind.TABLE, (0.0, 0.0, 100.0, 100.0), score=0.9)
    figure = _box("chart", ElementKind.FIGURE_IMAGE, (2.0, 2.0, 98.0, 98.0), score=0.8)
    # 서로 다른 유형이어도 IoU >= 0.75면 높은 점수만 유지.
    assert apply_layout_nms([table, figure]) == [table]

    # 포함 비율 50% < 85% 임계값 → 내부 박스 유지.
    half = _box("text", ElementKind.PARAGRAPH, (50.0, 0.0, 150.0, 100.0), score=0.8)
    kept = apply_layout_nms([table, half])
    assert kept == [table, half]

    # 표는 그림의 '내부 텍스트' 억제 대상이 아니므로 유지된다.
    inner_table = _box("table", ElementKind.TABLE, (10.0, 10.0, 50.0, 50.0), score=0.8)
    figure_container = _box("chart", ElementKind.FIGURE_IMAGE, (0.0, 0.0, 100.0, 100.0))
    kept2 = apply_layout_nms([figure_container, inner_table])
    assert kept2 == [figure_container, inner_table]

    # 같은 유형 문단끼리는 85% 이상 포함되면 억제.
    para_big = _box("text", ElementKind.PARAGRAPH, (0.0, 0.0, 100.0, 100.0))
    para_small = _box("text", ElementKind.PARAGRAPH, (10.0, 10.0, 40.0, 40.0), score=0.8)
    assert apply_layout_nms([para_big, para_small]) == [para_big]


# ---------------------------------------------------------------------------
# 읽기 순서 정렬 경계
# ---------------------------------------------------------------------------


def test_reading_order_empty_single_column_row_tie_and_partial_explicit() -> None:
    assert sort_layout_boxes_reading_order([], page_width_px=1000.0) == []

    a = _box("a", ElementKind.PARAGRAPH, (500.0, 100.0, 600.0, 120.0))
    b = _box("b", ElementKind.PARAGRAPH, (100.0, 100.0, 200.0, 120.0))
    c = _box("c", ElementKind.PARAGRAPH, (100.0, 50.0, 200.0, 70.0))
    ordered = sort_layout_boxes_reading_order([a, b, c], page_width_px=1000.0)
    assert [x.raw_label for x in ordered] == ["c", "b", "a"]
    assert [x.reading_order for x in ordered] == [1, 2, 3]

    # 일부만 명시적 순서가 있으면 전체를 위치 기반으로 다시 계산한다.
    partial = [
        _box("late", ElementKind.PARAGRAPH, (100.0, 300.0, 400.0, 350.0), reading_order=2),
        _box("early", ElementKind.PARAGRAPH, (100.0, 50.0, 400.0, 100.0)),
    ]
    recomputed = sort_layout_boxes_reading_order(partial, page_width_px=1000.0)
    assert [x.raw_label for x in recomputed] == ["early", "late"]

    # 명시적 순서 동점은 y, x 순으로 정렬한다.
    tie = [
        _box("second", ElementKind.PARAGRAPH, (100.0, 200.0, 400.0, 250.0), reading_order=1),
        _box("first", ElementKind.PARAGRAPH, (100.0, 50.0, 400.0, 100.0), reading_order=1),
    ]
    tied = sort_layout_boxes_reading_order(tie, page_width_px=1000.0)
    assert [x.raw_label for x in tied] == ["first", "second"]


def test_reading_order_two_column_with_full_width_middle_band() -> None:
    left1 = _box("left1", ElementKind.PARAGRAPH, (50.0, 100.0, 450.0, 180.0))
    left2 = _box("left2", ElementKind.PARAGRAPH, (50.0, 400.0, 450.0, 480.0))
    right1 = _box("right1", ElementKind.PARAGRAPH, (550.0, 50.0, 950.0, 130.0))
    right2 = _box("right2", ElementKind.PARAGRAPH, (550.0, 400.0, 950.0, 480.0))
    full = _box("full", ElementKind.TABLE, (50.0, 250.0, 950.0, 330.0))
    ordered = sort_layout_boxes_reading_order(
        [right2, left2, full, right1, left1], page_width_px=1000.0
    )
    assert [x.raw_label for x in ordered] == ["left1", "right1", "full", "left2", "right2"]


# ---------------------------------------------------------------------------
# boxes_to_visual_regions: 치명적 입력·스킵·클램프·메타데이터
# ---------------------------------------------------------------------------


def test_boxes_to_visual_regions_invalid_dimensions_return_empty() -> None:
    box = _box("table", ElementKind.TABLE, (10.0, 10.0, 60.0, 60.0))
    kwargs: dict[str, Any] = {
        "page": 1,
        "page_width_pt": 100.0,
        "page_height_pt": 100.0,
        "image_width_px": 100,
        "image_height_px": 100,
        "detector_name": "test",
    }
    assert boxes_to_visual_regions([box], **{**kwargs, "image_width_px": 0}) == []
    assert boxes_to_visual_regions([box], **{**kwargs, "page_height_pt": 0.0}) == []


def test_boxes_to_visual_regions_skips_header_footer_and_tiny_regions() -> None:
    header = _box(
        "header",
        ElementKind.HEADER_FOOTER,
        (10.0, 10.0, 60.0, 60.0),
        strategy=ParseStrategy.SKIP_DECORATIVE,
    )
    decorative = _box(
        "text",
        ElementKind.PARAGRAPH,
        (10.0, 10.0, 60.0, 60.0),
        strategy=ParseStrategy.SKIP_DECORATIVE,
    )
    narrow = _box("text", ElementKind.PARAGRAPH, (0.0, 0.0, 5.0, 50.0))
    short = _box("text", ElementKind.PARAGRAPH, (0.0, 0.0, 50.0, 5.0))
    common: dict[str, Any] = {
        "page": 1,
        "page_width_pt": 100.0,
        "page_height_pt": 100.0,
        "image_width_px": 100,
        "image_height_px": 100,
        "detector_name": "test",
    }
    assert boxes_to_visual_regions([header, decorative, narrow, short], **common) == []
    kept = boxes_to_visual_regions([header], **common, skip_header_footer=False)
    assert len(kept) == 1


def test_boxes_to_visual_regions_metadata_clamping_and_priority() -> None:
    table = _box("table", ElementKind.TABLE, (10.0, 10.0, 60.0, 60.0), score=1.7)
    para = _box("text", ElementKind.PARAGRAPH, (10.0, 70.0, 60.0, 95.0), score=-0.5)
    results = boxes_to_visual_regions(
        [table, para],
        page=2,
        page_width_pt=100.0,
        page_height_pt=100.0,
        image_width_px=100,
        image_height_px=100,
        detector_name="my-detector",
        start_index=5,
    )
    assert len(results) == 2
    region, rect = results[0]
    assert rect == (10, 10, 60, 60)
    assert region.region_id == "p002_v05"
    assert region.confidence == 1.0
    assert region.priority == "high"
    assert "my-detector" in region.reason and "table" in region.reason
    region2, _ = results[1]
    assert region2.region_id == "p002_v06"
    assert region2.confidence == 0.0
    assert region2.priority == "medium"

    # 이미지 밖 좌표는 픽셀·pt 모두 클램프된다.
    outside = _box("table", ElementKind.TABLE, (-20.0, -20.0, 500.0, 500.0))
    clamped = boxes_to_visual_regions(
        [outside],
        page=1,
        page_width_pt=100.0,
        page_height_pt=100.0,
        image_width_px=100,
        image_height_px=100,
        detector_name="test",
    )
    assert clamped[0][1] == (0, 0, 100, 100)
    assert clamped[0][0].bbox.x1 == 100.0

    # 사용자 지정 reason은 그대로 유지된다.
    custom = _box("table", ElementKind.TABLE, (10.0, 10.0, 60.0, 60.0), reason="사용자 사유")
    kept = boxes_to_visual_regions(
        [custom],
        page=1,
        page_width_pt=100.0,
        page_height_pt=100.0,
        image_width_px=100,
        image_height_px=100,
        detector_name="test",
    )
    assert kept[0][0].reason == "사용자 사유"


# ---------------------------------------------------------------------------
# 휴리스틱 탐지기: 빈 입력·임계값·채널별 마스크·제목/본문/표 분류
# ---------------------------------------------------------------------------


def test_heuristic_detector_empty_invalid_and_threshold_clamping() -> None:
    det = HeuristicRasterLayoutDetector()
    assert det.name == "heuristic"
    assert det.available is True
    assert det.detect(_inp(b"not-a-png", 4, 4)) == []
    white = encode_png(bytes([255] * (40 * 40)), 40, 40, channels=1)
    assert det.detect(_inp(white, 40, 40)) == []

    # 생성자 클램프: threshold 5.0 → 1.0이라 어떤 검출(<=0.88)도 통과하지 못한다.
    strict = HeuristicRasterLayoutDetector(score_threshold=5.0, nms_iou=0.0, max_grid_dim=1)
    buf = bytearray([255] * (200 * 200))
    _fill_gray(buf, 200, 200, 40, 40, 140, 140, 30)
    block_png = encode_png(bytes(buf), 200, 200, channels=1)
    assert strict.detect(_inp(block_png, 200, 200)) == []


def test_heuristic_detector_gray_solid_block_is_figure() -> None:
    buf = bytearray([255] * (200 * 200))
    _fill_gray(buf, 200, 200, 40, 40, 140, 140, 30)
    png = encode_png(bytes(buf), 200, 200, channels=1)
    boxes = HeuristicRasterLayoutDetector().detect(_inp(png, 200, 200))
    assert len(boxes) == 1
    assert boxes[0].raw_label == "figure"
    assert boxes[0].element_kind == ElementKind.FIGURE_IMAGE
    assert boxes[0].recommended_strategy == ParseStrategy.VLM_REGION
    assert boxes[0].bbox_px == (36.0, 36.0, 144.0, 144.0)


def test_heuristic_detector_gray_alpha_and_rgba_color_paths() -> None:
    # Gray+Alpha(2채널) 입력도 그레이 마스크로 처리된다.
    gray = bytearray([255] * (200 * 200))
    _fill_gray(gray, 200, 200, 40, 40, 140, 140, 30)
    ga_png = _gray_alpha_png(200, 200, bytes(gray))
    boxes_ga = HeuristicRasterLayoutDetector().detect(_inp(ga_png, 200, 200))
    assert len(boxes_ga) == 1
    assert boxes_ga[0].raw_label == "figure"

    # RGBA 컬러 블록은 색채 마스크 경로를 타고 높은 점수를 받는다.
    rgba = bytearray([255, 255, 255, 255] * (200 * 200))
    for y in range(40, 140):
        for x in range(40, 140):
            off = (y * 200 + x) * 4
            rgba[off] = 200
            rgba[off + 1] = 30
            rgba[off + 2] = 30
    rgba_png = encode_png(bytes(rgba), 200, 200, channels=4)
    boxes_rgba = HeuristicRasterLayoutDetector().detect(_inp(rgba_png, 200, 200))
    assert len(boxes_rgba) == 1
    assert boxes_rgba[0].raw_label == "figure"
    assert boxes_rgba[0].score == 0.85


def test_heuristic_detector_top_single_line_is_doc_title() -> None:
    buf = bytearray([255] * (300 * 300))
    _fill_gray(buf, 300, 300, 40, 8, 140, 16, 20)
    png = encode_png(bytes(buf), 300, 300, channels=1)
    boxes = HeuristicRasterLayoutDetector().detect(_inp(png, 300, 300))
    assert len(boxes) == 1
    assert boxes[0].raw_label == "doc_title"
    assert boxes[0].element_kind == ElementKind.HEADING


def test_heuristic_detector_sparse_lines_are_text() -> None:
    buf = bytearray([255] * (300 * 300))
    for ly in (100, 110, 120):
        for gx in range(40, 200, 20):
            _fill_gray(buf, 300, 300, gx, ly, gx + 5, ly + 6, 20)
    png = encode_png(bytes(buf), 300, 300, channels=1)
    boxes = HeuristicRasterLayoutDetector().detect(_inp(png, 300, 300))
    assert boxes
    assert {b.raw_label for b in boxes} == {"text"}
    assert all(b.element_kind == ElementKind.PARAGRAPH for b in boxes)


def test_heuristic_detector_booktabs_rules_form_table() -> None:
    buf = bytearray([255] * (300 * 300))
    for ry in (100, 108, 116):
        _fill_gray(buf, 300, 300, 50, ry, 250, ry + 1, 10)
    png = encode_png(bytes(buf), 300, 300, channels=1)
    boxes = HeuristicRasterLayoutDetector().detect(_inp(png, 300, 300))
    assert len(boxes) == 1
    assert boxes[0].raw_label == "table"
    assert boxes[0].score == 0.82
    assert boxes[0].element_kind == ElementKind.TABLE


def test_heuristic_detector_grid_lines_form_table() -> None:
    buf = bytearray([255] * (300 * 300))
    for hy in (80, 140, 200):
        _fill_gray(buf, 300, 300, 50, hy, 252, hy + 2, 10)
    for vx in (50, 150, 250):
        _fill_gray(buf, 300, 300, vx, 80, vx + 2, 202, 10)
    png = encode_png(bytes(buf), 300, 300, channels=1)
    boxes = HeuristicRasterLayoutDetector().detect(_inp(png, 300, 300))
    assert len(boxes) == 1
    assert boxes[0].raw_label == "table"
    assert boxes[0].score == 0.88


def test_heuristic_detector_vertical_gutter_splits_two_blocks() -> None:
    buf = bytearray([255] * (300 * 200))
    _fill_gray(buf, 300, 200, 20, 40, 120, 160, 30)
    _fill_gray(buf, 300, 200, 180, 40, 280, 160, 30)
    png = encode_png(bytes(buf), 300, 200, channels=1)
    boxes = HeuristicRasterLayoutDetector().detect(_inp(png, 300, 200))
    assert len(boxes) == 2
    assert all(b.raw_label == "figure" for b in boxes)
    left, right = sorted(boxes, key=lambda b: b.bbox_px[0])
    assert left.bbox_px[2] <= right.bbox_px[0]


# ---------------------------------------------------------------------------
# ONNX 동반 라벨 로드와 생성자 검증
# ---------------------------------------------------------------------------


def test_onnx_companion_labels_txt_and_config_json(tmp_path: Path) -> None:
    model = tmp_path / "m.onnx"
    model.write_bytes(b"fake")
    (tmp_path / "labels.txt").write_text("doc_title\n\n table \n\n", encoding="utf-8")
    det = OnnxDocLayoutDetector(model_path=model)
    assert det.labels == ("doc_title", "table")

    (tmp_path / "labels.txt").unlink()
    (tmp_path / "config.json").write_text('{"label_list": ["alpha", "beta"]}', encoding="utf-8")
    det2 = OnnxDocLayoutDetector(model_path=model)
    assert det2.labels == ("alpha", "beta")


def test_onnx_companion_broken_json_falls_back_and_bad_id2label(tmp_path: Path) -> None:
    model = tmp_path / "m.onnx"
    model.write_bytes(b"fake")
    (tmp_path / "config.json").write_text("{broken-json", encoding="utf-8")
    (tmp_path / "labels.txt").write_text("table\n", encoding="utf-8")
    det = OnnxDocLayoutDetector(model_path=model)
    assert det.labels == ("table",)

    # id2label 키가 정수가 아니면 예외 없이 프리셋 기본값으로 폴백한다.
    (tmp_path / "labels.txt").unlink()
    (tmp_path / "config.json").write_text('{"id2label": {"not-an-int": "Title"}}', encoding="utf-8")
    det2 = OnnxDocLayoutDetector(model_path=model)
    assert det2.labels == PP_DOCLAYOUT_V2_LABELS


def test_onnx_companion_inference_yml_quotes_and_comments(tmp_path: Path) -> None:
    model = tmp_path / "m.onnx"
    model.write_bytes(b"fake")
    (tmp_path / "inference.yml").write_text(
        "Global:\n  model_name: fake\n"
        "label_list:\n  - 'doc_title'\n  - \"table\"\n  # 주석\n"
        "postprocess:\n  name: fake\n",
        encoding="utf-8",
    )
    det = OnnxDocLayoutDetector(model_path=model)
    assert det.labels == ("doc_title", "table")


def test_onnx_constructor_presets_explicit_labels_and_bad_preset(tmp_path: Path) -> None:
    model = tmp_path / "m.onnx"
    model.write_bytes(b"fake")
    det = OnnxDocLayoutDetector(model_path=model, label_preset="DOCLAYNET")
    assert det.labels == DOCLAYNET_LABELS
    det2 = OnnxDocLayoutDetector(model_path=model, labels=["custom_a", "custom_b"])
    assert det2.labels == ("custom_a", "custom_b")
    with pytest.raises(ParserInputError, match="프리셋"):
        OnnxDocLayoutDetector(model_path=model, label_preset="no-such-preset")
    # labels.txt 가 비어 있으면(유효 라벨 0개) 무시하고 프리셋 기본값을 쓴다.
    (tmp_path / "labels.txt").write_text("\n\n", encoding="utf-8")
    det3 = OnnxDocLayoutDetector(model_path=model)
    assert det3.labels == PP_DOCLAYOUT_V2_LABELS


# ---------------------------------------------------------------------------
# ONNX 세션/의존성 분기 (가짜 ort·numpy로 결정론적 검증)
# ---------------------------------------------------------------------------


class _FakeOrtSession:
    def __init__(self, path: str, providers: list[str]) -> None:
        self.path = path
        self.providers = providers


def test_onnx_available_and_ensure_session_error_branches(tmp_path: Path) -> None:
    model = tmp_path / "m.onnx"
    det = OnnxDocLayoutDetector(model_path=model)

    det._np = None
    assert det.available is False
    with pytest.raises(ParserInputError, match="numpy"):
        det._ensure_session()

    det._np = object()
    det._ort = None
    det._session = None
    assert det.available is False
    with pytest.raises(ParserInputError, match="onnxruntime"):
        det._ensure_session()

    fake_ort = SimpleNamespace(InferenceSession=_FakeOrtSession)
    det._ort = fake_ort
    # 모델 파일이 없으면 명확한 오류.
    with pytest.raises(ParserInputError, match="모델 파일"):
        det._ensure_session()
    assert det.available is False

    model.write_bytes(b"fake-onnx")
    assert det.available is True
    session = det._ensure_session()
    assert isinstance(session, _FakeOrtSession)
    assert session.path == str(model)
    assert session.providers == ["CPUExecutionProvider"]
    # 이후 호출은 캐시된 세션을 반환한다.
    assert det._ensure_session() is session

    # 주입된 세션은 의존성과 무관하게 available=True.
    injected = OnnxDocLayoutDetector(model_path=model, session=object())
    injected._np = object()
    assert injected.available is True
    assert injected._ensure_session() is injected._session


def test_onnx_infer_input_size_static_dynamic_and_explicit(tmp_path: Path) -> None:
    model = tmp_path / "m.onnx"
    det = OnnxDocLayoutDetector(model_path=model, input_size=(640, 480))
    assert det._infer_input_size(SimpleNamespace(get_inputs=lambda: [])) == (640, 480)

    det2 = OnnxDocLayoutDetector(model_path=model)
    static_session = SimpleNamespace(
        get_inputs=lambda: [SimpleNamespace(name="image", shape=[1, 3, 768, 512])]
    )
    assert det2._infer_input_size(static_session) == (768, 512)
    dynamic_session = SimpleNamespace(
        get_inputs=lambda: [SimpleNamespace(name="image", shape=[1, 3, "h", "w"])]
    )
    assert det2._infer_input_size(dynamic_session) == (800, 800)
    assert det2._infer_input_size(SimpleNamespace(get_inputs=lambda: [])) == (800, 800)
    wrong_rank = SimpleNamespace(
        get_inputs=lambda: [SimpleNamespace(name="image", shape=[3, 768, 512])]
    )
    assert det2._infer_input_size(wrong_rank) == (800, 800)


def test_onnx_to_list_and_label_for_class_id(tmp_path: Path) -> None:
    det = OnnxDocLayoutDetector(model_path=tmp_path / "m.onnx", labels=["a", "b"])

    class _WithTolist:
        def tolist(self) -> Any:
            return [1, 2]

    class _ScalarTolist:
        def tolist(self) -> Any:
            return 5

    assert det._to_list(_WithTolist()) == [1, 2]
    assert det._to_list(_ScalarTolist()) == [5]
    assert det._to_list([3, 4]) == [3, 4]
    assert det._to_list((3, 4)) == [3, 4]
    assert det._to_list(7) == [7]

    assert det._label_for_class_id(1) == "b"
    assert det._label_for_class_id(-1) == "class_-1"
    assert det._label_for_class_id(99) == "class_99"


# ---------------------------------------------------------------------------
# ONNX 출력 파싱 분기
# ---------------------------------------------------------------------------


def _parse_det(tmp_path: Path, **kwargs: Any) -> OnnxDocLayoutDetector:
    return OnnxDocLayoutDetector(
        model_path=tmp_path / "m.onnx",
        labels=["doc_title", "table", "chart", "text"],
        **kwargs,
    )


def test_onnx_parse_outputs_empty_and_form_a_unwrap(tmp_path: Path) -> None:
    det = _parse_det(tmp_path)
    assert det._parse_outputs([], orig_w=600, orig_h=840, target_w=800, target_h=800) == []
    # [1, N, 6] 배치 래핑 해제 + 행 길이 부족/비리스트 행 스킵.
    outputs = [[[[1, 0.9, 50.0, 300.0, 550.0, 500.0], [2, 0.8], "bad-row"]]]
    parsed = det._parse_outputs(outputs, orig_w=600, orig_h=840, target_w=800, target_h=800)
    assert len(parsed) == 1
    assert parsed[0].element_kind == ElementKind.TABLE
    assert parsed[0].reading_order == 1
    # 스칼라 출력은 행이 없어 빈 결과.
    assert det._parse_outputs([5], orig_w=600, orig_h=840, target_w=800, target_h=800) == []


def test_onnx_parse_outputs_order_vector_and_negative_order(tmp_path: Path) -> None:
    det = _parse_det(tmp_path)
    primary = [
        [1, 0.9, 50.0, 300.0, 550.0, 500.0],
        [0, 0.95, 50.0, 40.0, 450.0, 90.0],
    ]
    outputs = [primary, None, [[1, 0]]]
    parsed = det._parse_outputs(outputs, orig_w=600, orig_h=840, target_w=800, target_h=800)
    assert [b.raw_label for b in parsed] == ["doc_title", "table"]
    assert [b.reading_order for b in parsed] == [1, 2]

    negative = [[[1, 0.9, 10.0, 10.0, 100.0, 100.0, -3]]]
    parsed_neg = det._parse_outputs(negative, orig_w=600, orig_h=840, target_w=800, target_h=800)
    assert parsed_neg[0].reading_order == 1  # 음수 순서 → 0 → 정렬 시 1 부여


def test_onnx_parse_outputs_separated_tensors_ordering_and_batch(tmp_path: Path) -> None:
    det = _parse_det(tmp_path)
    boxes = [[10.0, 10.0, 100.0, 100.0], [10.0, 200.0, 100.0, 300.0]]
    # (labels, boxes, scores) 순서도 인식한다.
    outputs = [[0, 1], boxes, [0.9, 0.8]]
    parsed = det._parse_outputs(outputs, orig_w=600, orig_h=840, target_w=800, target_h=800)
    assert [b.element_kind for b in parsed] == [ElementKind.HEADING, ElementKind.TABLE]

    # [1, N, ...] 배치 래핑 해제.
    batched = [[boxes], [[0.9, 0.8]], [[0, 1]]]
    parsed_b = det._parse_outputs(batched, orig_w=600, orig_h=840, target_w=800, target_h=800)
    assert len(parsed_b) == 2

    # 길이 불일치는 최솟값으로 자른다.
    mismatch = [boxes, [0.9], [0, 1]]
    parsed_m = det._parse_outputs(mismatch, orig_w=600, orig_h=840, target_w=800, target_h=800)
    assert len(parsed_m) == 1

    # 4좌표 박스가 어디에도 없으면 빈 결과.
    assert (
        det._parse_outputs([[0], [1, 2], [0.9]], orig_w=600, orig_h=840, target_w=800, target_h=800)
        == []
    )


def test_onnx_parse_outputs_coordinate_spaces_and_clamping(tmp_path: Path) -> None:
    det = _parse_det(tmp_path)
    # [0..1] 정규화 좌표는 원본 크기로 환산된다.
    normalized = [[[1, 0.9, 0.1, 0.1, 0.9, 0.5]]]
    parsed = det._parse_outputs(normalized, orig_w=1000, orig_h=1000, target_w=800, target_h=800)
    assert parsed[0].bbox_px == (100.0, 100.0, 900.0, 500.0)

    # 뒤집힌 좌표와 이미지 밖 좌표는 정리·클램프된다.
    swapped = [[[1, 0.9, 900.0, -50.0, 100.0, 1200.0]]]
    parsed_s = det._parse_outputs(swapped, orig_w=1000, orig_h=800, target_w=800, target_h=800)
    assert parsed_s[0].bbox_px == (100.0, 0.0, 900.0, 800.0)

    # 4px 미만 박스는 버린다.
    tiny = [[[1, 0.9, 10.0, 10.0, 12.0, 12.0]]]
    assert det._parse_outputs(tiny, orig_w=600, orig_h=840, target_w=800, target_h=800) == []


def test_onnx_parse_outputs_threshold_score_clamp_and_unknown_class(tmp_path: Path) -> None:
    det = _parse_det(tmp_path, score_threshold=0.4)
    below = [[[1, 0.2, 10.0, 10.0, 100.0, 100.0]]]
    assert det._parse_outputs(below, orig_w=600, orig_h=840, target_w=800, target_h=800) == []

    over = [[[1, 1.5, 10.0, 10.0, 100.0, 100.0]]]
    parsed = det._parse_outputs(over, orig_w=600, orig_h=840, target_w=800, target_h=800)
    assert parsed[0].score == 1.0

    unknown = [[[99, 0.9, 10.0, 10.0, 100.0, 100.0]]]
    parsed_u = det._parse_outputs(unknown, orig_w=600, orig_h=840, target_w=800, target_h=800)
    assert parsed_u[0].raw_label == "class_99"
    assert parsed_u[0].element_kind == ElementKind.PARAGRAPH


def test_decode_non_png_without_pillow_raises_parser_input_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _no_pil(name: str) -> Any:
        raise ImportError(name)

    monkeypatch.setattr(importlib, "import_module", _no_pil)
    inp = _inp(b"\xff\xd8\xff\xe0fake-jpeg", 4, 4)
    with pytest.raises(ParserInputError, match="Pillow"):
        ld._decode_image_to_rgb_array(inp, object())


# ---------------------------------------------------------------------------
# HTTP 탐지기: 가짜 전송 계층으로 요청/응답 계약 검증(네트워크 미사용)
# ---------------------------------------------------------------------------


def _patch_open(
    monkeypatch: pytest.MonkeyPatch,
    *,
    response: dict[str, Any] | None = None,
    exc: Exception | None = None,
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def _fake_open(
        url: str,
        *,
        zone: TrustZone,
        payload: dict[str, Any] | None,
        headers: dict[str, str] | None,
        timeout_sec: float,
    ) -> dict[str, Any]:
        calls.append(
            {
                "url": url,
                "zone": zone,
                "payload": payload,
                "headers": headers,
                "timeout_sec": timeout_sec,
            }
        )
        if exc is not None:
            raise exc
        return response if response is not None else {}

    monkeypatch.setattr(ld, "_open_json_request", _fake_open)
    return calls


def test_http_detector_request_payload_url_and_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    png = encode_png(bytes([255] * 12), 2, 2, channels=3)
    calls = _patch_open(monkeypatch, response={"boxes": []})
    det = HttpLayoutDetector(base_url="http://127.0.0.1:8001/", api_key="  test-key-123  ")
    assert det.name == "http-layout"
    assert det.available is True
    assert det.trust_zone is TrustZone.LOOPBACK
    det.detect(_inp(png, 2, 2))
    call = calls[0]
    assert call["url"] == "http://127.0.0.1:8001/v1/layout/detect"
    assert call["headers"] == {"Authorization": "Bearer test-key-123"}
    payload = call["payload"]
    assert payload["page"] == 1
    assert payload["mime_type"] == "image/png"
    assert payload["width_px"] == 2
    assert payload["height_px"] == 2
    assert payload["score_threshold"] == 0.4
    assert base64.b64decode(payload["image_base64"]) == png

    # /detect로 끝나는 URL은 그대로 사용한다.
    det2 = HttpLayoutDetector(base_url="http://127.0.0.1:8001/custom/detect")
    det2.detect(_inp(png, 2, 2))
    assert calls[1]["url"] == "http://127.0.0.1:8001/custom/detect"
    assert calls[1]["headers"] == {}

    # 환경 변수 키 해석(지어낸 값).
    monkeypatch.setenv("FAKE_LAYOUT_KEY", "env-key-456")
    det3 = HttpLayoutDetector(api_key_env="FAKE_LAYOUT_KEY")
    det3.detect(_inp(png, 2, 2))
    assert calls[2]["headers"] == {"Authorization": "Bearer env-key-456"}
    # 명시 키가 환경 변수보다 우선한다.
    det4 = HttpLayoutDetector(api_key="direct-key", api_key_env="FAKE_LAYOUT_KEY")
    det4.detect(_inp(png, 2, 2))
    assert calls[3]["headers"] == {"Authorization": "Bearer direct-key"}


def test_http_detector_timeout_clamp_and_security_block() -> None:
    det = HttpLayoutDetector(timeout_sec=0.1)
    assert det._timeout_sec == 1.0
    with pytest.raises(VisionSecurityError):
        HttpLayoutDetector(base_url="https://layout.example.com")


def test_http_detector_transport_errors_map_to_parser_input_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    png = encode_png(bytes([255] * 12), 2, 2, channels=3)
    http_exc = urllib.error.HTTPError("http://127.0.0.1:8001", 503, "down", Message(), None)
    _patch_open(monkeypatch, exc=http_exc)
    det = HttpLayoutDetector()
    with pytest.raises(ParserInputError, match="HTTP 503"):
        det.detect(_inp(png, 2, 2))

    _patch_open(monkeypatch, exc=urllib.error.URLError("connection refused"))
    with pytest.raises(ParserInputError, match="연결"):
        det.detect(_inp(png, 2, 2))

    _patch_open(monkeypatch, exc=ValueError("bad json"))
    with pytest.raises(ParserInputError, match="JSON"):
        det.detect(_inp(png, 2, 2))


def test_http_detector_response_item_filtering_and_norm_1000(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    png = encode_png(bytes([255] * 12), 2, 2, channels=3)
    resp: dict[str, Any] = {
        "coordinate_space": "norm_1000",
        "boxes": [
            {"label": "table", "bbox": [100, 200, 900, 500], "score": 0.95},
            {"label": "text", "bbox": [100, 600, 900, 700], "confidence": 0.8},
            "not-a-dict",
            {"label": "text"},
            {"label": "text", "bbox": [1, 2, 3]},
            {"label": "text", "bbox": ["a", "b", "c", "d"]},
            {"label": "text", "bbox": [0, 0, 1000, 1000], "score": 0.1},
            {"label": "text", "bbox": [0, 0, 2, 2], "score": 0.9},
        ],
    }
    _patch_open(monkeypatch, response=resp)
    det = HttpLayoutDetector()
    boxes = det.detect(_inp(png, 1000, 1000))
    assert [b.raw_label for b in boxes] == ["table", "text"]
    assert boxes[0].bbox_px == (100.0, 200.0, 900.0, 500.0)
    assert boxes[1].bbox_px == (100.0, 600.0, 900.0, 700.0)
    assert [b.reading_order for b in boxes] == [1, 2]


def test_http_detector_alternative_keys_and_coordinate_spaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    png = encode_png(bytes([255] * 12), 2, 2, channels=3)
    item = {"category": "table", "coordinate": [100.0, 100.0, 500.0, 500.0], "score": 0.9}
    _patch_open(monkeypatch, response={"regions": [item]})
    det = HttpLayoutDetector()
    boxes = det.detect(_inp(png, 1000, 1000))
    assert len(boxes) == 1
    assert boxes[0].bbox_px == (100.0, 100.0, 500.0, 500.0)

    _patch_open(monkeypatch, response={"detections": [item]})
    assert len(det.detect(_inp(png, 1000, 1000))) == 1

    # 좌표 공간 선언이 없어도 최대값 <= 1.05면 정규화로 간주한다.
    norm_item = {"class_name": "text", "box": [0.1, 0.1, 0.5, 0.5], "score": 0.9}
    _patch_open(monkeypatch, response={"boxes": [norm_item]})
    boxes_n = det.detect(_inp(png, 1000, 1000))
    assert boxes_n[0].bbox_px == (100.0, 100.0, 500.0, 500.0)

    # 목록이 아니거나 키가 없으면 빈 결과.
    _patch_open(monkeypatch, response={"boxes": "not-a-list"})
    assert det.detect(_inp(png, 1000, 1000)) == []
    _patch_open(monkeypatch, response={})
    assert det.detect(_inp(png, 1000, 1000)) == []


def test_http_detector_swapped_out_of_bounds_and_reading_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    png = encode_png(bytes([255] * 12), 2, 2, channels=3)
    resp = {
        "boxes": [
            {"label": "table", "bbox": [900, 500, 100, 200], "score": 0.9, "order": "bad"},
            {"label": "text", "bbox": [-100, -100, 2000, 2000], "score": 0.9},
        ]
    }
    _patch_open(monkeypatch, response=resp)
    det = HttpLayoutDetector()
    boxes = det.detect(_inp(png, 1000, 1000))
    assert len(boxes) == 2
    # 잘못된 order는 0이 되어 위치 기반 정렬로 폴백한다(전폭 text가 먼저).
    assert [b.raw_label for b in boxes] == ["text", "table"]
    assert boxes[0].bbox_px == (0.0, 0.0, 1000.0, 1000.0)
    assert boxes[1].bbox_px == (100.0, 200.0, 900.0, 500.0)

    # 모든 박스에 명시적 순서가 있으면 위치와 무관하게 존중된다.
    resp2 = {
        "boxes": [
            {"label": "table", "bbox": [100, 200, 900, 500], "score": 0.9, "reading_order": 1},
            {"label": "text", "bbox": [10, 10, 100, 100], "score": 0.9, "order": 2},
        ]
    }
    _patch_open(monkeypatch, response=resp2)
    boxes2 = det.detect(_inp(png, 1000, 1000))
    assert [b.raw_label for b in boxes2] == ["table", "text"]
    assert [b.reading_order for b in boxes2] == [1, 2]


# ---------------------------------------------------------------------------
# 팩토리
# ---------------------------------------------------------------------------


def test_create_layout_detector_normalization_and_errors(tmp_path: Path) -> None:
    assert isinstance(create_layout_detector(""), NoopLayoutDetector)
    assert isinstance(create_layout_detector(" NOOP "), NoopLayoutDetector)
    with pytest.raises(ParserInputError, match="layout-model-path"):
        create_layout_detector("onnx", model_path="   ")
    onnx_det = create_layout_detector(" ONNX ", model_path=tmp_path / "m.onnx")
    assert isinstance(onnx_det, OnnxDocLayoutDetector)
    http_det = create_layout_detector("http")
    assert isinstance(http_det, HttpLayoutDetector)
    assert http_det.trust_zone is TrustZone.LOOPBACK
    heuristic = create_layout_detector("heuristic", score_threshold=0.9, nms_iou=0.2)
    assert isinstance(heuristic, HeuristicRasterLayoutDetector)
    assert heuristic._score_threshold == 0.9
    assert heuristic._nms_iou == 0.2
