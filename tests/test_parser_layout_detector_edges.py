"""layout_detector 모듈의 오류 경로·경계 값·가짜 세션 기반 단위 테스트."""

from __future__ import annotations

import importlib
import json
import struct
import urllib.error
import zlib
from collections.abc import Sequence
from email.message import Message
from pathlib import Path
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
from autoetl.parser.image_io import DecodedImage, encode_png
from autoetl.parser.layout_detector import (
    HeuristicRasterLayoutDetector,
    HttpLayoutDetector,
    OnnxDocLayoutDetector,
    _build_downsampled_masks,
    _load_companion_label_list,
    _split_vertical_columns,
    _tight_grid_bbox,
    apply_layout_nms,
    boxes_to_visual_regions,
    create_layout_detector,
    normalize_layout_label,
    sort_layout_boxes_reading_order,
)
from autoetl.security.levels import TrustZone


def _box(
    label: str,
    kind: ElementKind,
    bbox: tuple[float, float, float, float],
    score: float = 0.8,
    *,
    strategy: ParseStrategy = ParseStrategy.EMBEDDED_IMAGE_OCR,
    order: int = 0,
    reason: str = "",
) -> DetectedLayoutBox:
    return DetectedLayoutBox(
        raw_label=label,
        element_kind=kind,
        recommended_strategy=strategy,
        bbox_px=bbox,
        score=score,
        reading_order=order,
        reason=reason,
    )


def _chunk(ctype: bytes, payload: bytes) -> bytes:
    crc = zlib.crc32(payload, zlib.crc32(ctype)) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + ctype + payload + struct.pack(">I", crc)


def _gray_png(width: int, height: int, pixels: bytes) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    scanlines = bytearray()
    for y in range(height):
        scanlines.append(0)
        scanlines.extend(pixels[y * width : (y + 1) * width])
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(bytes(scanlines)))
        + _chunk(b"IEND", b"")
    )


def _ga_png(width: int, height: int, pixels: bytes) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 4, 0, 0, 0)
    scanlines = bytearray()
    stride = width * 2
    for y in range(height):
        scanlines.append(0)
        scanlines.extend(pixels[y * stride : (y + 1) * stride])
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(bytes(scanlines)))
        + _chunk(b"IEND", b"")
    )


def _draw_gray(buf: bytearray, width: int, x0: int, y0: int, x1: int, y1: int, value: int) -> None:
    for y in range(y0, y1):
        for x in range(x0, x1):
            buf[y * width + x] = value


def _draw_gray_text(buf: bytearray, width: int, x0: int, y0: int, x1: int, line_h: int) -> None:
    x = x0
    while x < x1 - 2:
        _draw_gray(buf, width, x, y0, min(x1, x + 2), y0 + line_h, 20)
        x += 7


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


# ---------------------------------------------------------------------------
# 라벨 정규화·NMS·읽기 순서 경계
# ---------------------------------------------------------------------------


def test_normalize_layout_label_vector_and_fallback() -> None:
    assert normalize_layout_label("vector_diagram") == (
        ElementKind.VECTOR_DIAGRAM,
        ParseStrategy.VLM_REGION,
    )
    assert normalize_layout_label("Flowchart") == (
        ElementKind.VECTOR_DIAGRAM,
        ParseStrategy.VLM_REGION,
    )
    assert normalize_layout_label("  Table-Body  ") == (
        ElementKind.TABLE,
        ParseStrategy.VLM_REGION,
    )
    assert normalize_layout_label("formula-number") == (
        ElementKind.HEADER_FOOTER,
        ParseStrategy.SKIP_DECORATIVE,
    )
    assert normalize_layout_label("완전모르는라벨") == (
        ElementKind.PARAGRAPH,
        ParseStrategy.EMBEDDED_IMAGE_OCR,
    )


def test_nms_empty_zero_area_cross_kind_and_tie_breaks() -> None:
    assert apply_layout_nms([]) == []

    zero_area = _box("text", ElementKind.PARAGRAPH, (10.0, 10.0, 10.0, 20.0))
    assert apply_layout_nms([zero_area]) == []

    # 서로 다른 유형이라도 IoU >= 0.75면 점수 높은 쪽만 유지.
    table = _box(
        "table", ElementKind.TABLE, (0.0, 0.0, 100.0, 100.0), 0.9, strategy=ParseStrategy.VLM_REGION
    )
    figure = _box(
        "chart",
        ElementKind.FIGURE_IMAGE,
        (2.0, 2.0, 98.0, 98.0),
        0.8,
        strategy=ParseStrategy.VLM_REGION,
    )
    kept = apply_layout_nms([figure, table])
    assert [b.raw_label for b in kept] == ["table"]

    # 동점이면 면적이 큰 박스 유지.
    big = _box(
        "table", ElementKind.TABLE, (0.0, 0.0, 100.0, 100.0), 0.8, strategy=ParseStrategy.VLM_REGION
    )
    small = _box(
        "table", ElementKind.TABLE, (5.0, 5.0, 95.0, 95.0), 0.8, strategy=ParseStrategy.VLM_REGION
    )
    kept_tie = apply_layout_nms([small, big])
    assert kept_tie == [big]


def test_nms_containment_threshold_and_non_suppressible_kinds() -> None:
    table = _box(
        "table", ElementKind.TABLE, (0.0, 0.0, 100.0, 100.0), 0.9, strategy=ParseStrategy.VLM_REGION
    )
    # 포함 비율 50% < 85% → 유지.
    half_in = _box("text", ElementKind.PARAGRAPH, (50.0, 50.0, 150.0, 100.0), 0.7)
    # 표 안의 HEADER_FOOTER는 포함 억제 대상이 아님.
    header_in = _box(
        "footer",
        ElementKind.HEADER_FOOTER,
        (10.0, 10.0, 40.0, 40.0),
        0.6,
        strategy=ParseStrategy.SKIP_DECORATIVE,
    )
    kept = apply_layout_nms([table, half_in, header_in])
    assert {b.raw_label for b in kept} == {"table", "text", "footer"}

    # 먼저 유지된 박스가 더 작으면 포함 억제를 적용하지 않는다.
    small_table = _box(
        "table", ElementKind.TABLE, (0.0, 0.0, 30.0, 30.0), 0.95, strategy=ParseStrategy.VLM_REGION
    )
    big_text = _box("text", ElementKind.PARAGRAPH, (0.0, 0.0, 100.0, 100.0), 0.7)
    kept2 = apply_layout_nms([small_table, big_text])
    assert {b.raw_label for b in kept2} == {"table", "text"}


def test_sort_reading_order_empty_and_single_column_row_bucket() -> None:
    assert sort_layout_boxes_reading_order([], page_width_px=1000.0) == []
    right = _box("r", ElementKind.PARAGRAPH, (200.0, 100.0, 400.0, 120.0))
    left = _box("l", ElementKind.PARAGRAPH, (50.0, 101.0, 150.0, 121.0))
    below = _box("b", ElementKind.PARAGRAPH, (50.0, 300.0, 400.0, 330.0))
    ordered = sort_layout_boxes_reading_order([right, below, left], page_width_px=1000.0)
    # 같은 줄 버킷(y/12 반올림)에서는 x0 오름차순.
    assert [b.raw_label for b in ordered] == ["l", "r", "b"]
    assert [b.reading_order for b in ordered] == [1, 2, 3]


# ---------------------------------------------------------------------------
# boxes_to_visual_regions 변환·스킵·클램프
# ---------------------------------------------------------------------------


def test_boxes_to_visual_regions_rejects_invalid_dimensions() -> None:
    boxes = [_box("text", ElementKind.PARAGRAPH, (0.0, 0.0, 100.0, 100.0))]
    for kw in (
        {"image_width_px": 0},
        {"image_height_px": -1},
        {"page_width_pt": 0.0},
        {"page_height_pt": -5.0},
    ):
        args: dict[str, Any] = {
            "page": 1,
            "page_width_pt": 595.0,
            "page_height_pt": 842.0,
            "image_width_px": 1000,
            "image_height_px": 2000,
            "detector_name": "d",
        }
        args.update(kw)
        assert boxes_to_visual_regions(boxes, **args) == []


def test_boxes_to_visual_regions_scaling_ids_confidence_and_reason() -> None:
    table = _box(
        "table",
        ElementKind.TABLE,
        (100.0, 200.0, 500.0, 600.0),
        1.5,
        strategy=ParseStrategy.VLM_REGION,
    )
    text = _box(
        "text", ElementKind.PARAGRAPH, (100.0, 700.0, 500.0, 900.0), -0.3, reason="사용자 정의 근거"
    )
    out = boxes_to_visual_regions(
        [table, text],
        page=2,
        page_width_pt=595.0,
        page_height_pt=842.0,
        image_width_px=1000,
        image_height_px=2000,
        detector_name="테스트검출기",
        start_index=5,
    )
    assert len(out) == 2
    region0, crop0 = out[0]
    assert crop0 == (100, 200, 500, 600)
    assert region0.region_id == "p002_v05"
    assert region0.bbox.x0 == pytest.approx(59.5)
    assert region0.bbox.top == pytest.approx(84.2)
    assert region0.bbox.x1 == pytest.approx(297.5)
    assert region0.bbox.bottom == pytest.approx(252.6)
    assert region0.confidence == 1.0
    assert region0.priority == "high"
    assert "테스트검출기" in region0.reason and "`table`" in region0.reason
    region1, _ = out[1]
    assert region1.region_id == "p002_v06"
    assert region1.confidence == 0.0
    assert region1.priority == "medium"
    assert region1.reason == "사용자 정의 근거"


def test_boxes_to_visual_regions_skips_decorative_tiny_and_clamps() -> None:
    header = _box(
        "header",
        ElementKind.HEADER_FOOTER,
        (0.0, 0.0, 900.0, 100.0),
        0.9,
        strategy=ParseStrategy.SKIP_DECORATIVE,
    )
    decorative = _box(
        "text",
        ElementKind.PARAGRAPH,
        (0.0, 120.0, 900.0, 200.0),
        0.9,
        strategy=ParseStrategy.SKIP_DECORATIVE,
    )
    tiny_w = _box("text", ElementKind.PARAGRAPH, (0.0, 300.0, 10.0, 400.0))
    tiny_h = _box("text", ElementKind.PARAGRAPH, (0.0, 500.0, 100.0, 510.0))
    huge = _box("text", ElementKind.PARAGRAPH, (-50.0, -50.0, 5000.0, 5000.0))
    args: dict[str, Any] = {
        "page": 1,
        "page_width_pt": 595.0,
        "page_height_pt": 842.0,
        "image_width_px": 1000,
        "image_height_px": 2000,
        "detector_name": "d",
    }
    kept = boxes_to_visual_regions([header, decorative, tiny_w, tiny_h, huge], **args)
    assert len(kept) == 1
    assert kept[0][1] == (0, 0, 1000, 2000)

    kept_all = boxes_to_visual_regions([header, decorative], **args, skip_header_footer=False)
    assert len(kept_all) == 2


# ---------------------------------------------------------------------------
# 휴리스틱 탐지기: 그레이스케일 마스크, booktabs, 오류·경계 입력
# ---------------------------------------------------------------------------


def test_heuristic_detector_rejects_bad_png_and_blank_page() -> None:
    det = HeuristicRasterLayoutDetector()
    assert det.name == "heuristic"
    assert det.available is True
    assert det.detect(_inp(b"not-png", 10, 10)) == []
    blank = _gray_png(160, 200, bytes([255] * (160 * 200)))
    assert det.detect(_inp(blank, 160, 200)) == []


def test_heuristic_detector_gray_scan_title_text_table_and_bottom_rule() -> None:
    w, h = 160, 200
    buf = bytearray([255] * (w * h))
    # 상단 제목(높이 12), 본문 3줄(높이 4, 간격 3), 격자 표, 최하단 가로선.
    _draw_gray(buf, w, 20, 10, 100, 22, 20)
    for ly in (40, 47, 54):
        _draw_gray_text(buf, w, 20, ly, 140, 4)
    for hy in (100, 130, 159):
        _draw_gray(buf, w, 20, hy, 140, hy + 1, 10)
    for vx in (20, 80, 139):
        _draw_gray(buf, w, vx, 100, vx + 1, 160, 10)
    _draw_gray(buf, w, 0, 199, 160, 200, 10)
    png = _gray_png(w, h, bytes(buf))

    det = HeuristicRasterLayoutDetector()
    boxes = det.detect(_inp(png, w, h))
    labels = {b.raw_label for b in boxes}
    assert "table" in labels
    assert "doc_title" in labels
    assert "text" in labels
    assert [b.reading_order for b in boxes] == list(range(1, len(boxes) + 1))

    # 임계값 1.0이면 모든 후보가 탈락한다.
    strict = HeuristicRasterLayoutDetector(score_threshold=1.0)
    assert strict.detect(_inp(png, w, h)) == []


def test_heuristic_detector_booktabs_rules_merge_into_table() -> None:
    w, h = 160, 200
    buf = bytearray([255] * (w * h))
    for hy in (100, 108, 116):
        _draw_gray(buf, w, 20, hy, 140, hy + 1, 10)
    _draw_gray_text(buf, w, 30, 103, 100, 3)
    png = _gray_png(w, h, bytes(buf))
    boxes = HeuristicRasterLayoutDetector().detect(_inp(png, w, h))
    tables = [b for b in boxes if b.raw_label == "table"]
    assert len(tables) == 1
    assert tables[0].score == pytest.approx(0.82)
    assert "booktabs" in tables[0].reason


def test_heuristic_detector_sparse_block_below_density_and_no_overlap_lines() -> None:
    w, h = 160, 200
    # 희소 잉크 블록: 밀도 < 1.5%로 탈락(중앙값 폴백도 함께 통과).
    sparse = bytearray([255] * (w * h))
    for y in range(50, 62):
        sparse[y * w + (0 if y % 2 == 0 else 159)] = 0
    assert HeuristicRasterLayoutDetector().detect(_inp(_gray_png(w, h, bytes(sparse)), w, h)) == []

    # 가로로 전혀 겹치지 않는 두 줄은 병합되지 않는다.
    split = bytearray([255] * (w * h))
    _draw_gray_text(split, w, 0, 40, 30, 4)
    _draw_gray_text(split, w, 100, 47, 130, 4)
    boxes = HeuristicRasterLayoutDetector().detect(_inp(_gray_png(w, h, bytes(split)), w, h))
    assert len(boxes) == 2
    assert all(b.raw_label == "text" for b in boxes)


def test_heuristic_detector_rgb_color_block_uses_color_density_path() -> None:
    w, h = 160, 200
    rgb = bytearray([255, 255, 255] * (w * h))
    for y in range(100, 109):
        for x in range(20, 80):
            off = (y * w + x) * 3
            rgb[off] = 200
            rgb[off + 1] = 30
            rgb[off + 2] = 30
    png = encode_png(bytes(rgb), w, h, channels=3)
    boxes = HeuristicRasterLayoutDetector().detect(_inp(png, w, h))
    assert len(boxes) == 1
    assert boxes[0].raw_label in ("paragraph_title", "doc_title", "figure", "chart")


def test_build_downsampled_masks_gray_alpha_and_subsampling() -> None:
    # Gray+Alpha(2채널)도 그레이 경로로 마스크를 만든다.
    ga = bytearray([255, 255] * (80 * 60))
    ga[(10 * 80 + 10) * 2] = 20
    decoded = DecodedImage(width=80, height=60, channels=2, pixels=bytes(ga))
    ink, color, gw, gh, step = _build_downsampled_masks(decoded)
    assert (gw, gh, step) == (80, 60, 1)
    assert ink[10][10] == 1
    assert sum(sum(row) for row in color) == 0

    # step >= 4일 때 2단계 서브샘플링 경로.
    gray = bytearray([255] * (160 * 200))
    gray[199 * 160 + 159] = 0
    decoded_g = DecodedImage(width=160, height=200, channels=1, pixels=bytes(gray))
    ink2, _color2, gw2, gh2, step2 = _build_downsampled_masks(decoded_g, max_dim=40)
    assert step2 == 5 and (gw2, gh2) == (32, 40)
    assert ink2[39][31] == 1


def test_split_vertical_columns_gutter_cases() -> None:
    gw, gh = 100, 100
    empty = [bytearray(gw) for _ in range(gh)]

    # 작은 영역은 분할하지 않는다.
    assert _split_vertical_columns(empty, (0, 0, 10, 10), gw=gw, gh=gh) == [(0, 0, 10, 10)]

    # 가로 격자선이 있으면 분할하지 않는다.
    ruled = [bytearray(gw) for _ in range(gh)]
    for x in range(0, 90):
        ruled[20][x] = 1
    assert _split_vertical_columns(ruled, (0, 0, 100, 40), gw=gw, gh=gh) == [(0, 0, 100, 40)]

    # 좌·우 블록 + 중앙 여백 → 2분할.
    two = [bytearray(gw) for _ in range(gh)]
    for y in range(0, 40):
        for x in range(5, 21):
            two[y][x] = 1
        for x in range(80, 96):
            two[y][x] = 1
    parts = _split_vertical_columns(two, (0, 0, 100, 40), gw=gw, gh=gh)
    assert parts == [(5, 0, 21, 40), (80, 0, 96, 40)]

    # 여백이 검색 범위 끝까지 이어지는 경우(오른쪽 블록은 검색 범위 밖).
    tail = [bytearray(gw) for _ in range(gh)]
    for y in range(0, 40):
        for x in range(5, 21):
            tail[y][x] = 1
        for x in range(85, 96):
            tail[y][x] = 1
    parts_tail = _split_vertical_columns(tail, (0, 0, 100, 40), gw=gw, gh=gh)
    assert len(parts_tail) == 2

    # 여백이 없으면 분할하지 않는다.
    solid = [bytearray([1] * gw) for _ in range(gh)]
    assert _split_vertical_columns(solid, (0, 0, 100, 40), gw=gw, gh=gh) == [(0, 0, 100, 40)]

    # 한쪽 블록이 너무 좁으면 분할하지 않는다.
    narrow = [bytearray(gw) for _ in range(gh)]
    for y in range(0, 40):
        narrow[y][5] = 1
        for x in range(80, 96):
            narrow[y][x] = 1
    assert _split_vertical_columns(narrow, (0, 0, 100, 40), gw=gw, gh=gh) == [(0, 0, 100, 40)]

    assert _tight_grid_bbox(empty, 0, 0, gw, gh) is None


# ---------------------------------------------------------------------------
# ONNX 동반 라벨 로드
# ---------------------------------------------------------------------------


def test_load_companion_label_list_txt_json_errors_and_yml_break(tmp_path: Path) -> None:
    model = tmp_path / "m.onnx"
    model.write_bytes(b"fake")
    assert _load_companion_label_list(model) is None

    (tmp_path / "labels.txt").write_text("title\n\ntable\n", encoding="utf-8")
    assert _load_companion_label_list(model) == ("title", "table")
    (tmp_path / "labels.txt").unlink()

    (tmp_path / "config.json").write_text(json.dumps({"label_list": ["a", "b"]}), encoding="utf-8")
    assert _load_companion_label_list(model) == ("a", "b")

    # 깨진 JSON은 무시하고 다음 후보로 폴백한다.
    (tmp_path / "config.json").write_text("{broken", encoding="utf-8")
    (tmp_path / "inference.yml").write_text(
        "label_list:\n  - 'x'\n  - \"y\"\nother_key: 1\n  - z\n", encoding="utf-8"
    )
    assert _load_companion_label_list(model) == ("x", "y")

    (tmp_path / "inference.yml").unlink()
    (tmp_path / "config.json").unlink()
    (tmp_path / "labels.txt").write_text("\n\n", encoding="utf-8")
    assert _load_companion_label_list(model) is None


# ---------------------------------------------------------------------------
# ONNX 탐지기: 가짜 numpy/세션 (실제 의존성 불필요)
# ---------------------------------------------------------------------------


class _FakeNdArray:
    """체이닝 연산만 지원하는 가짜 ndarray."""

    def __init__(self, shape: tuple[int, ...]) -> None:
        self.shape = shape
        self.getitem_keys: list[object] = []

    def reshape(self, shape: tuple[int, ...]) -> _FakeNdArray:
        return _FakeNdArray(shape)

    def astype(self, dtype: object) -> _FakeNdArray:
        return self

    def __truediv__(self, other: object) -> _FakeNdArray:
        return self

    def transpose(self, *axes: int) -> _FakeNdArray:
        return self

    def __getitem__(self, key: object) -> _FakeNdArray:
        self.getitem_keys.append(key)
        if isinstance(key, tuple) and len(key) == 3 and isinstance(key[2], slice):
            stop = key[2].stop
            if isinstance(stop, int) and len(self.shape) == 3:
                return _FakeNdArray((self.shape[0], self.shape[1], stop))
        return self


class _FakeNp:
    """numpy 모듈의 최소 표면만 모방한 가짜 객체."""

    def __init__(self) -> None:
        self.uint8 = "uint8"
        self.float32 = "float32"
        self.calls: list[str] = []
        self.array_values: list[object] = []

    def frombuffer(self, data: bytes, dtype: object) -> _FakeNdArray:
        self.calls.append("frombuffer")
        return _FakeNdArray((len(data),))

    def repeat(self, arr: _FakeNdArray, n: int, axis: int) -> _FakeNdArray:
        self.calls.append(f"repeat:{n}:{axis}")
        return _FakeNdArray((arr.shape[0], arr.shape[1], 3))

    def array(self, values: object, dtype: object = None) -> object:
        self.array_values.append(values)
        return values


class _FakeInputMeta:
    def __init__(self, name: str, shape: list[object]) -> None:
        self.name = name
        self.shape = shape


class _FakeSession:
    def __init__(
        self, outputs: Sequence[object], metas: list[_FakeInputMeta] | None = None
    ) -> None:
        self._outputs = outputs
        self._metas = metas if metas is not None else [_FakeInputMeta("image", [1, 3, 8, 8])]
        self.last_feed: dict[str, object] | None = None

    def get_inputs(self) -> list[_FakeInputMeta]:
        return self._metas

    def run(self, output_names: object, feed: dict[str, object]) -> list[object]:
        self.last_feed = feed
        return list(self._outputs)


class _FakeOrt:
    def __init__(self) -> None:
        self.created: list[tuple[str, list[str]]] = []

    def InferenceSession(self, path: str, providers: list[str]) -> _FakeSession:
        self.created.append((path, providers))
        return _FakeSession([])


def test_onnx_labels_explicit_presets_and_invalid_preset(tmp_path: Path) -> None:
    model = tmp_path / "m.onnx"
    model.write_bytes(b"fake")
    det = OnnxDocLayoutDetector(model, labels=["x", "y"])
    assert det.labels == ("x", "y")
    assert OnnxDocLayoutDetector(model, label_preset="doclaynet").labels == ld.DOCLAYNET_LABELS
    assert OnnxDocLayoutDetector(model, label_preset=" PUBLAYNET ").labels == ld.PUBLAYNET_LABELS
    with pytest.raises(ParserInputError, match="프리셋"):
        OnnxDocLayoutDetector(model, label_preset="no-such-preset")


def test_onnx_available_and_ensure_session_error_paths(tmp_path: Path) -> None:
    model = tmp_path / "m.onnx"
    model.write_bytes(b"fake")
    det = OnnxDocLayoutDetector(model)
    det._np = None
    assert det.available is False
    with pytest.raises(ParserInputError, match="numpy"):
        det._ensure_session()

    fake_np = _FakeNp()
    det._np = fake_np
    det._ort = None
    assert det.available is False
    with pytest.raises(ParserInputError, match="onnxruntime"):
        det._ensure_session()

    det._ort = _FakeOrt()
    missing = OnnxDocLayoutDetector(tmp_path / "missing.onnx")
    missing._np = fake_np
    missing._ort = det._ort
    with pytest.raises(ParserInputError, match="모델 파일"):
        missing._ensure_session()

    assert det.available is True
    session = det._ensure_session()
    assert det._ort.created == [(str(model), ["CPUExecutionProvider"])]
    assert det._ensure_session() is session
    assert det.available is True

    injected = OnnxDocLayoutDetector(model, session=_FakeSession([]))
    injected._np = fake_np
    assert injected.available is True


def test_onnx_infer_input_size_label_lookup_and_to_list(tmp_path: Path) -> None:
    model = tmp_path / "m.onnx"
    model.write_bytes(b"fake")
    det = OnnxDocLayoutDetector(model, labels=("table",), input_size=(640, 480))
    assert det._infer_input_size(_FakeSession([])) == (640, 480)

    det2 = OnnxDocLayoutDetector(model, labels=("table",))
    static = _FakeSession([], metas=[_FakeInputMeta("image", [1, 3, 600, 800])])
    assert det2._infer_input_size(static) == (600, 800)
    dynamic = _FakeSession([], metas=[_FakeInputMeta("image", ["b", 3, "h", "w"])])
    assert det2._infer_input_size(dynamic) == (800, 800)

    assert det2._label_for_class_id(0) == "table"
    assert det2._label_for_class_id(99) == "class_99"
    assert det2._label_for_class_id(-1) == "class_-1"

    class _ScalarToList:
        def tolist(self) -> int:
            return 7

    assert det2._to_list(_ScalarToList()) == [7]
    assert det2._to_list((1, 2)) == [1, 2]
    assert det2._to_list(5) == [5]
    assert det2._to_list([1]) == [1]


def test_onnx_parse_outputs_shapes_orders_and_degenerate_rows(tmp_path: Path) -> None:
    model = tmp_path / "m.onnx"
    model.write_bytes(b"fake")
    det = OnnxDocLayoutDetector(model, labels=("doc_title", "table", "text"))
    kw: dict[str, int] = {"orig_w": 1000, "orig_h": 1000, "target_w": 800, "target_h": 800}

    assert det._parse_outputs([], **kw) == []
    assert det._parse_outputs([[1, 2, 3]], **kw) == []

    # [1, N, 6] 래핑 + 별도 order 텐서 + 짧은 행 스킵.
    wrapped = [
        [[0, 0.9, 100.0, 100.0, 500.0, 200.0], [1, 0.8, 100.0, 300.0, 500.0, 600.0], [1, 2]],
        [],
        [[1, 0, 0]],
    ]
    parsed = det._parse_outputs(wrapped, **kw)
    assert [b.raw_label for b in parsed] == ["table", "doc_title"]
    assert [b.reading_order for b in parsed] == [1, 2]

    # 행 내 order가 음수면 0으로 파싱되고, 정렬기가 위치 기준 1부터 다시 번호를 매긴다.
    neg = [[[1, 0.9, 100.0, 100.0, 500.0, 500.0, -3.0]]]
    parsed_neg = det._parse_outputs(neg, **kw)
    assert len(parsed_neg) == 1
    assert parsed_neg[0].reading_order == 1

    # 형태 B: (labels, boxes, scores) 순서.
    labels_first = [
        [0, 1],
        [[100.0, 100.0, 500.0, 200.0], [100.0, 300.0, 500.0, 600.0]],
        [0.9, 0.8],
    ]
    parsed_b = det._parse_outputs(labels_first, **kw)
    assert [b.raw_label for b in parsed_b] == ["doc_title", "table"]

    # 형태 B: (boxes, scores, labels) + 각 텐서의 [1, N] 배치 래핑 해제.
    batch_wrapped = [
        [[100.0, 100.0, 500.0, 200.0], [100.0, 300.0, 500.0, 600.0]],
        [[0.9, 0.8]],
        [[0, 1]],
    ]
    parsed_batch = det._parse_outputs(batch_wrapped, **kw)
    assert [b.raw_label for b in parsed_batch] == ["doc_title", "table"]

    # 박스 형태가 아니면 행을 만들지 않는다.
    assert det._parse_outputs([[1, 2], [3, 4], [5, 6]], **kw) == []

    # 4px 미만 박스는 탈락.
    tiny = [[[1, 0.9, 100.0, 100.0, 102.0, 500.0]]]
    assert det._parse_outputs(tiny, **kw) == []


def test_onnx_detect_with_fake_session_feeds_and_bgr(tmp_path: Path) -> None:
    model = tmp_path / "m.onnx"
    model.write_bytes(b"fake")
    png = encode_png(bytes([255] * (8 * 8 * 3)), 8, 8, channels=3)
    outputs = [[[1, 0.9, 1.0, 1.0, 7.0, 7.0]]]
    metas = [
        _FakeInputMeta("image", [1, 3, 8, 8]),
        _FakeInputMeta("im_shape", [1, 2]),
        _FakeInputMeta("scale_factor", [1, 2]),
    ]
    session = _FakeSession(outputs, metas)
    det = OnnxDocLayoutDetector(model, labels=("text", "table"), session=session, input_size=(8, 8))
    fake_np = _FakeNp()
    det._np = fake_np
    boxes = det.detect(_inp(png, 8, 8))
    assert len(boxes) == 1
    assert boxes[0].element_kind == ElementKind.TABLE
    assert boxes[0].bbox_px == (1.0, 1.0, 7.0, 7.0)
    assert session.last_feed is not None
    assert session.last_feed["im_shape"] == [[8.0, 8.0]]
    assert session.last_feed["scale_factor"] == [[1.0, 1.0]]
    assert "image" in session.last_feed

    # BGR 입력 옵션은 리사이즈 결과에 역순 슬라이스를 적용한다.
    session_bgr = _FakeSession(outputs, metas)
    det_bgr = OnnxDocLayoutDetector(
        model, labels=("text", "table"), session=session_bgr, input_size=(8, 8), bgr_input=True
    )
    det_bgr._np = _FakeNp()
    assert len(det_bgr.detect(_inp(png, 8, 8))) == 1


def test_decode_image_to_rgb_array_channel_paths_and_pil_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_np = _FakeNp()

    gray = encode_png(bytes([10, 20, 30, 40]), 2, 2, channels=1)
    arr1 = ld._decode_image_to_rgb_array(_inp(gray, 2, 2), fake_np)
    assert arr1.shape == (2, 2, 3)
    assert "repeat:3:2" in fake_np.calls

    ga = _ga_png(2, 1, bytes([10, 255, 20, 128]))
    arr2 = ld._decode_image_to_rgb_array(_inp(ga, 2, 1), _FakeNp())
    assert arr2.shape == (1, 2, 3)

    rgb = encode_png(bytes([1, 2, 3] * 2), 2, 1, channels=3)
    arr3 = ld._decode_image_to_rgb_array(_inp(rgb, 2, 1), _FakeNp())
    assert arr3.shape == (1, 2, 3)

    rgba = encode_png(bytes([1, 2, 3, 4] * 2), 2, 1, channels=4)
    arr4 = ld._decode_image_to_rgb_array(_inp(rgba, 2, 1), _FakeNp())
    assert arr4.shape == (1, 2, 3)

    # PNG가 아닌 입력은 Pillow 폴백을 시도하고, 없으면 ParserInputError.
    def _no_pil(name: str) -> Any:
        raise ImportError(name)

    monkeypatch.setattr(importlib, "import_module", _no_pil)
    with pytest.raises(ParserInputError, match="Pillow"):
        ld._decode_image_to_rgb_array(_inp(b"\xff\xd8\xff\xe0jpeg", 2, 2), fake_np)


# ---------------------------------------------------------------------------
# HTTP 탐지기: 가짜 전송 계층 (네트워크 불필요)
# ---------------------------------------------------------------------------


def _http_inp() -> LayoutDetectionInput:
    png = encode_png(bytes([255] * 12), 2, 2, channels=3)
    return _inp(png, 600, 800)


def test_http_detector_key_resolution_url_and_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_open(url: str, **kwargs: Any) -> dict[str, Any]:
        captured["url"] = url
        captured.update(kwargs)
        return {"boxes": []}

    monkeypatch.setattr(ld, "_open_json_request", fake_open)
    monkeypatch.setenv("FAKE_LAYOUT_KEY", "  env-token  ")
    det = HttpLayoutDetector(
        base_url=" http://127.0.0.1:8001/ ", api_key_env="FAKE_LAYOUT_KEY", timeout_sec=0.2
    )
    assert det.name == "http-layout"
    assert det.available is True
    assert det.trust_zone is TrustZone.LOOPBACK
    assert det._auth_headers() == {"Authorization": "Bearer env-token"}
    det.detect(_http_inp())
    assert captured["url"] == "http://127.0.0.1:8001/v1/layout/detect"
    assert captured["timeout_sec"] == 1.0
    assert captured["payload"]["mime_type"] == "image/png"
    assert captured["payload"]["width_px"] == 600
    assert captured["headers"] == {"Authorization": "Bearer env-token"}

    # /detect로 끝나는 URL은 그대로 사용, 키가 없으면 인증 헤더 없음.
    det2 = HttpLayoutDetector(base_url="http://127.0.0.1:8001/custom/detect")
    assert det2._auth_headers() == {}
    det2.detect(_http_inp())
    assert captured["url"] == "http://127.0.0.1:8001/custom/detect"
    assert captured["headers"] == {}


def test_http_detector_transport_errors_become_parser_input_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    det = HttpLayoutDetector()

    def raise_http(url: str, **kwargs: Any) -> dict[str, Any]:
        raise urllib.error.HTTPError(url, 503, "down", Message(), None)

    monkeypatch.setattr(ld, "_open_json_request", raise_http)
    with pytest.raises(ParserInputError, match="HTTP 503"):
        det.detect(_http_inp())

    def raise_url(url: str, **kwargs: Any) -> dict[str, Any]:
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(ld, "_open_json_request", raise_url)
    with pytest.raises(ParserInputError, match="연결"):
        det.detect(_http_inp())

    def raise_value(url: str, **kwargs: Any) -> dict[str, Any]:
        raise ValueError("non_object_json_response")

    monkeypatch.setattr(ld, "_open_json_request", raise_value)
    with pytest.raises(ParserInputError, match="JSON"):
        det.detect(_http_inp())


def test_http_detector_response_parsing_edge_cases(monkeypatch: pytest.MonkeyPatch) -> None:
    def make_detector(resp: dict[str, Any]) -> HttpLayoutDetector:
        def fake_open(url: str, **kwargs: Any) -> dict[str, Any]:
            return resp

        monkeypatch.setattr(ld, "_open_json_request", fake_open)
        return HttpLayoutDetector(score_threshold=0.5)

    # boxes가 리스트가 아니면 빈 결과. regions/detections 키도 지원.
    assert make_detector({"boxes": "nope"}).detect(_http_inp()) == []
    assert make_detector({}).detect(_http_inp()) == []
    regions = make_detector(
        {"regions": [{"label": "table", "bbox": [10, 20, 300, 400], "score": 0.9}]}
    ).detect(_http_inp())
    assert [b.raw_label for b in regions] == ["table"]
    detections = make_detector(
        {"detections": [{"category": "chart", "box": [10, 20, 300, 400], "confidence": 0.9}]}
    ).detect(_http_inp())
    assert detections[0].element_kind == ElementKind.FIGURE_IMAGE

    resp: dict[str, Any] = {
        "boxes": [
            "not-a-dict",
            {"label": "text", "bbox": [1, 2]},
            {"label": "text", "bbox": ["a", "b", "c", "d"]},
            {"label": "text", "bbox": [10, 20, 300, 400], "score": 0.2},
            {"label": "text", "coordinate": [10, 20, 300, 400], "confidence": 0.8},
            {"class_name": "table", "bbox": [10, 420, 300, 700]},
            {"label": "text", "bbox": [0, 0, 2, 2], "score": 0.9},
        ]
    }
    kept = make_detector(resp).detect(_http_inp())
    assert [b.raw_label for b in kept] == ["text", "table"]
    assert kept[0].score == pytest.approx(0.8)
    assert kept[1].score == pytest.approx(1.0)

    # norm_1000 좌표계 변환.
    norm1000 = make_detector(
        {
            "coordinate_space": "norm_1000",
            "boxes": [{"label": "table", "bbox": [100, 200, 900, 800], "score": 0.9}],
        }
    ).detect(_http_inp())
    assert norm1000[0].bbox_px == (60.0, 160.0, 540.0, 640.0)

    # 1.05 이하 좌표는 자동 정규화 간주.
    auto_norm = make_detector(
        {"boxes": [{"label": "table", "bbox": [0.1, 0.2, 0.9, 0.8], "score": 0.9}]}
    ).detect(_http_inp())
    assert auto_norm[0].bbox_px == (60.0, 160.0, 540.0, 640.0)

    # 모두 양수인 명시적 순서는 위치보다 우선한다(order 폴백·reading_order 키).
    orders = make_detector(
        {
            "boxes": [
                {"label": "text", "bbox": [10, 20, 300, 200], "score": 0.9, "order": 3},
                {"label": "text", "bbox": [10, 300, 300, 500], "score": 0.9, "order": 2},
                {"label": "text", "bbox": [10, 600, 300, 780], "score": 0.9, "reading_order": 1},
            ]
        }
    ).detect(_http_inp())
    assert [b.bbox_px[1] for b in orders] == [600.0, 300.0, 20.0]
    assert [b.reading_order for b in orders] == [1, 2, 3]

    # 잘못된 순서 값·음수는 0으로 정규화되어 위치 기준 정렬로 폴백한다.
    bad_orders = make_detector(
        {
            "boxes": [
                {"label": "text", "bbox": [10, 20, 300, 200], "score": 0.9, "order": "abc"},
                {"label": "text", "bbox": [10, 300, 300, 500], "score": 0.9, "reading_order": -2},
                {"label": "text", "bbox": [10, 600, 300, 780], "score": 0.9},
            ]
        }
    ).detect(_http_inp())
    assert [b.bbox_px[1] for b in bad_orders] == [20.0, 300.0, 600.0]


# ---------------------------------------------------------------------------
# 팩토리
# ---------------------------------------------------------------------------


def test_create_layout_detector_onnx_http_and_noop(tmp_path: Path) -> None:
    assert isinstance(create_layout_detector(""), NoopLayoutDetector)
    assert isinstance(create_layout_detector(" noop "), NoopLayoutDetector)
    model = tmp_path / "m.onnx"
    model.write_bytes(b"fake")
    onnx_det = create_layout_detector(" ONNX ", model_path=model)
    assert isinstance(onnx_det, OnnxDocLayoutDetector)
    http_det = create_layout_detector("http", base_url="http://127.0.0.1:8001")
    assert isinstance(http_det, HttpLayoutDetector)
    with pytest.raises(ParserInputError, match="layout-model-path"):
        create_layout_detector("onnx", model_path="   ")
