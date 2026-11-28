"""Auto Parser 스캔 페이지 레이아웃 객체 탐지기(Layout Detection) 단위·통합·CLI 테스트."""

from __future__ import annotations

import json
import shutil
import struct
import threading
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from autoetl.cli import main as cli_main
from autoetl.parser import (
    DOCLAYNET_LABELS,
    PP_DOCLAYOUT_PLUS_L_LABELS,
    PP_DOCLAYOUT_V2_LABELS,
    PUBLAYNET_LABELS,
    CropStatus,
    DetectedLayoutBox,
    ElementKind,
    HeuristicRasterLayoutDetector,
    HttpLayoutDetector,
    LayoutDetectionInput,
    NoopLayoutDetector,
    OnnxDocLayoutDetector,
    OpenAICompatibleVlmAdapter,
    PageCategory,
    ParseOptions,
    ParserInputError,
    ParseStrategy,
    VisionSecurityError,
    VisionStatus,
    apply_layout_nms,
    create_layout_detector,
    inspect_document,
    normalize_layout_label,
    parse_and_export,
    parse_document,
    sort_layout_boxes_reading_order,
)
from autoetl.parser.adapters import PdftoppmPageRenderer
from autoetl.parser.image_io import crop_png_region, decode_png, encode_png, to_grayscale
from autoetl.security.levels import TrustZone

# ---------------------------------------------------------------------------
# 헬퍼: 최소 PDF 생성기, 합성 스캔 페이지 캔버스, 루프백 Mock HTTP 서버
# ---------------------------------------------------------------------------


def _build_minimal_pdf(
    ops: str,
    *,
    width: float = 595.0,
    height: float = 842.0,
    base_font: str = "Helvetica",
    images: list[tuple[str, int, int, bytes]] | None = None,
) -> bytes:
    """텍스트 또는 RGB FlateDecode 이미지가 들어 있는 최소 유효 PDF 1.4 바이트를 생성한다."""
    objs: list[bytes] = []

    def add_obj(body: bytes) -> int:
        objs.append(body)
        return len(objs)

    f_reg = add_obj(f"<< /Type /Font /Subtype /Type1 /BaseFont /{base_font} >>".encode("ascii"))
    img_entries: list[str] = []
    for name, px_w, px_h, rgb_raw in images or []:
        compressed = zlib.compress(rgb_raw)
        img_id = add_obj(
            f"<< /Type /XObject /Subtype /Image /Width {px_w} /Height {px_h} "
            f"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode "
            f"/Length {len(compressed)} >>\nstream\n".encode("ascii")
            + compressed
            + b"\nendstream"
        )
        img_entries.append(f"/{name} {img_id} 0 R")

    stream_bytes = ops.encode("latin-1")
    content_id = add_obj(
        f"<< /Length {len(stream_bytes)} >>\nstream\n".encode("ascii")
        + stream_bytes
        + b"\nendstream"
    )
    xobj_res = f" /XObject << {' '.join(img_entries)} >>" if img_entries else ""
    pages_id_placeholder = len(objs) + 2
    page_id = add_obj(
        f"<< /Type /Page /Parent {pages_id_placeholder} 0 R "
        f"/MediaBox [0 0 {width:.1f} {height:.1f}] "
        f"/Resources << /Font << /F1 {f_reg} 0 R >>{xobj_res} >> "
        f"/Contents {content_id} 0 R >>".encode("ascii")
    )
    pages_id = add_obj(f"<< /Type /Pages /Kids [{page_id} 0 R] /Count 1 >>".encode("ascii"))
    assert pages_id == pages_id_placeholder
    catalog_id = add_obj(f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode("ascii"))

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for idx, body in enumerate(objs, 1):
        offsets.append(len(out))
        out.extend(f"{idx} 0 obj\n".encode("ascii"))
        out.extend(body)
        out.extend(b"\nendobj\n")
    xref_pos = len(out)
    out.extend(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode("ascii"))
    for off in offsets[1:]:
        out.extend(f"{off:010d} 00000 n \n".encode("ascii"))
    out.extend(
        f"trailer\n<< /Size {len(objs) + 1} /Root {catalog_id} 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF\n".encode("ascii")
    )
    return bytes(out)


def _draw_rect(
    buf: bytearray,
    width: int,
    height: int,
    x0: int,
    y0: int,
    x1: int,
    y1: int,
    rgb: tuple[int, int, int],
) -> None:
    r, g, b = rgb
    for y in range(max(0, y0), min(height, y1)):
        row_off = y * width * 3
        for x in range(max(0, x0), min(width, x1)):
            off = row_off + x * 3
            buf[off] = r
            buf[off + 1] = g
            buf[off + 2] = b


def _draw_fake_text_line(
    buf: bytearray,
    width: int,
    height: int,
    x0: int,
    y0: int,
    x1: int,
    line_h: int,
) -> None:
    """실제 글줄과 비슷한 간헐적 잉크 획 패턴을 캔버스에 그린다."""
    x = x0
    toggle = 0
    while x < x1 - 4:
        glyph_w = 5 if (toggle % 3) != 0 else 8
        _draw_rect(buf, width, height, x, y0, min(x1, x + glyph_w), y0 + line_h, (20, 20, 20))
        x += glyph_w + (6 if (toggle % 4) == 3 else 3)
        toggle += 1


def _build_synthetic_multi_element_scan_rgb(width: int = 600, height: int = 840) -> bytes:
    """제목 + 3줄 본문 문단 + 3x3 격자 표 + 컬러 막대 차트가 함께 있는 스캔 페이지 RGB를 만든다."""
    buf = bytearray([255, 255, 255] * (width * height))

    # 1. 상단 큰 제목(y=45..67)
    _draw_fake_text_line(buf, width, height, 60, 45, 360, 20)

    # 2. 본문 3줄 문단(y=120..176)
    for line_idx in range(3):
        ly = 120 + line_idx * 20
        _draw_fake_text_line(buf, width, height, 60, ly, 520, 10)

    # 3. 3x3 격자 표(x=60..540, y=240..440)
    tx0, ty0, tx1, ty1 = 60, 240, 540, 440
    for hy in (ty0, 290, 340, 390, ty1):
        _draw_rect(buf, width, height, tx0, hy, tx1, hy + 2, (15, 15, 15))
    for vx in (tx0, 220, 380, tx1):
        _draw_rect(buf, width, height, vx, ty0, vx + 2, ty1 + 2, (15, 15, 15))
    for row_y in (260, 310, 360, 410):
        _draw_fake_text_line(buf, width, height, 75, row_y, 195, 9)
        _draw_fake_text_line(buf, width, height, 235, row_y, 355, 9)
        _draw_fake_text_line(buf, width, height, 395, row_y, 515, 9)

    # 4. 하단 컬러 막대 차트(x=80..520, y=520..760)
    cx0, cy0, cx1, cy1 = 80, 520, 520, 760
    _draw_rect(buf, width, height, cx0, cy0, cx0 + 3, cy1, (20, 20, 20))  # y축
    _draw_rect(buf, width, height, cx0, cy1 - 3, cx1, cy1, (20, 20, 20))  # x축
    _draw_rect(buf, width, height, 130, 620, 190, cy1 - 4, (30, 110, 220))
    _draw_rect(buf, width, height, 240, 560, 300, cy1 - 4, (220, 70, 50))
    _draw_rect(buf, width, height, 350, 590, 410, cy1 - 4, (40, 165, 95))

    return bytes(buf)


def _create_scanned_pdf(path: Path, rgb: bytes, width_px: int, height_px: int) -> Path:
    pdf_bytes = _build_minimal_pdf(
        "q 595 0 0 842 0 0 cm /Im0 Do Q",
        width=595.0,
        height=842.0,
        images=[("Im0", width_px, height_px, rgb)],
    )
    path.write_bytes(pdf_bytes)
    return path


@contextmanager
def _run_mock_server(handler_fn: Any) -> Iterator[str]:
    """루프백 127.0.0.1 임시 HTTP 서버를 띄우고 base_url을 반환한다."""

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length > 0 else b"{}"
            body = json.loads(raw.decode("utf-8"))
            status, resp_obj = handler_fn(self.path, dict(self.headers.items()), body)
            encoded = json.dumps(resp_obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()


def _encode_filtered_png(pixels: bytes, width: int, height: int, filter_type: int) -> bytes:
    """특정 PNG 필터(0=None, 1=Sub, 2=Up, 3=Avg, 4=Paeth)를 적용한 8비트 RGB PNG를 만든다."""
    bpp = 3
    stride = width * bpp
    scanlines = bytearray()
    prev = bytearray(stride)
    for y in range(height):
        cur = pixels[y * stride : (y + 1) * stride]
        scanlines.append(filter_type)
        for i in range(stride):
            a = cur[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            if filter_type == 0:
                pred = 0
            elif filter_type == 1:
                pred = a
            elif filter_type == 2:
                pred = b
            elif filter_type == 3:
                pred = (a + b) >> 1
            else:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
            scanlines.append((cur[i] - pred) & 0xFF)
        prev = bytearray(cur)

    def chunk(ctype: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(payload, zlib.crc32(ctype)) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + ctype + payload + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(bytes(scanlines)))
        + chunk(b"IEND", b"")
    )


# ---------------------------------------------------------------------------
# 1. 순수 Python PNG 디코딩·크롭 단위 테스트
# ---------------------------------------------------------------------------


def test_png_decode_filters_and_crop_roundtrip() -> None:
    """모든 PNG 필터(0~4) 디코딩, 그레이스케일 변환, 부분 픽셀 크롭의 정확도를 검증한다."""
    w, h = 12, 10
    rgb = bytearray(w * h * 3)
    for y in range(h):
        for x in range(w):
            off = (y * w + x) * 3
            rgb[off] = (x * 21) & 0xFF
            rgb[off + 1] = (y * 25) & 0xFF
            rgb[off + 2] = ((x + y) * 11) & 0xFF

    for flt in range(5):
        png_bytes = _encode_filtered_png(bytes(rgb), w, h, flt)
        decoded = decode_png(png_bytes)
        assert decoded is not None
        assert (decoded.width, decoded.height, decoded.channels) == (w, h, 3)
        assert decoded.pixels == bytes(rgb)

    png_rgb = encode_png(bytes(rgb), w, h, channels=3)
    cropped = crop_png_region(png_rgb, 2, 3, 8, 7)
    assert cropped is not None
    assert (cropped.width, cropped.height) == (6, 4)
    dec_crop = decode_png(cropped.data)
    assert dec_crop is not None
    # (x=2, y=3) 픽셀이 크롭의 (0, 0) 픽셀과 정확히 일치해야 함
    src_off = (3 * w + 2) * 3
    assert dec_crop.pixels[:3] == bytes(rgb[src_off : src_off + 3])

    gray = to_grayscale(dec_crop)
    assert len(gray) == 6 * 4


# ---------------------------------------------------------------------------
# 2. 라벨 정규화·NMS·포함 제거·다단 읽기 순서 정렬 테스트
# ---------------------------------------------------------------------------


def test_normalize_layout_label_presets() -> None:
    """PP-DocLayoutV2/plus-L·DocLayNet·PubLayNet 표준 라벨이 올바른 요소/전략으로 매핑된다."""
    assert len(PP_DOCLAYOUT_V2_LABELS) == 25
    assert len(PP_DOCLAYOUT_PLUS_L_LABELS) == 20
    assert len(DOCLAYNET_LABELS) == 11
    assert len(PUBLAYNET_LABELS) == 5

    assert normalize_layout_label("table") == (ElementKind.TABLE, ParseStrategy.VLM_REGION)
    assert normalize_layout_label("Table") == (ElementKind.TABLE, ParseStrategy.VLM_REGION)
    assert normalize_layout_label("chart") == (ElementKind.FIGURE_IMAGE, ParseStrategy.VLM_REGION)
    assert normalize_layout_label("Picture") == (ElementKind.FIGURE_IMAGE, ParseStrategy.VLM_REGION)
    assert normalize_layout_label("display_formula") == (
        ElementKind.FORMULA_OR_CODE,
        ParseStrategy.VLM_REGION,
    )
    assert normalize_layout_label("doc_title") == (
        ElementKind.HEADING,
        ParseStrategy.EMBEDDED_IMAGE_OCR,
    )
    assert normalize_layout_label("Section-header") == (
        ElementKind.HEADING,
        ParseStrategy.EMBEDDED_IMAGE_OCR,
    )
    assert normalize_layout_label("figure_title") == (
        ElementKind.CAPTION,
        ParseStrategy.EMBEDDED_IMAGE_OCR,
    )
    assert normalize_layout_label("Page-header") == (
        ElementKind.HEADER_FOOTER,
        ParseStrategy.SKIP_DECORATIVE,
    )
    assert normalize_layout_label("footer_image") == (
        ElementKind.HEADER_FOOTER,
        ParseStrategy.SKIP_DECORATIVE,
    )
    assert normalize_layout_label("List-item") == (
        ElementKind.LIST_ITEM,
        ParseStrategy.EMBEDDED_IMAGE_OCR,
    )
    assert normalize_layout_label("abstract") == (
        ElementKind.PARAGRAPH,
        ParseStrategy.EMBEDDED_IMAGE_OCR,
    )


def test_nms_containment_and_two_column_reading_order() -> None:
    """NMS 중복 제거, 표 내부 텍스트 포함 억제, 2단 읽기 순서 및 명시적 순서 보존을 검증한다."""
    table_box = DetectedLayoutBox(
        raw_label="table",
        element_kind=ElementKind.TABLE,
        recommended_strategy=ParseStrategy.VLM_REGION,
        bbox_px=(50.0, 200.0, 550.0, 450.0),
        score=0.92,
    )
    dup_table = DetectedLayoutBox(
        raw_label="table",
        element_kind=ElementKind.TABLE,
        recommended_strategy=ParseStrategy.VLM_REGION,
        bbox_px=(55.0, 205.0, 545.0, 445.0),
        score=0.75,
    )
    inner_cell_text = DetectedLayoutBox(
        raw_label="text",
        element_kind=ElementKind.PARAGRAPH,
        recommended_strategy=ParseStrategy.EMBEDDED_IMAGE_OCR,
        bbox_px=(80.0, 240.0, 200.0, 270.0),
        score=0.80,
    )
    outside_caption = DetectedLayoutBox(
        raw_label="caption",
        element_kind=ElementKind.CAPTION,
        recommended_strategy=ParseStrategy.EMBEDDED_IMAGE_OCR,
        bbox_px=(50.0, 170.0, 300.0, 195.0),
        score=0.81,
    )
    kept = apply_layout_nms([table_box, dup_table, inner_cell_text, outside_caption])
    assert [b.raw_label for b in kept] == ["table", "caption"]

    # 2단 레이아웃 읽기 순서: 상단 전폭 제목 → 좌단 상·하 → 우단 상·하 → 하단 전폭 표
    two_col_boxes = [
        DetectedLayoutBox(
            raw_label="right_2",
            element_kind=ElementKind.PARAGRAPH,
            recommended_strategy=ParseStrategy.EMBEDDED_IMAGE_OCR,
            bbox_px=(530.0, 360.0, 950.0, 500.0),
            score=0.8,
        ),
        DetectedLayoutBox(
            raw_label="bottom_table",
            element_kind=ElementKind.TABLE,
            recommended_strategy=ParseStrategy.VLM_REGION,
            bbox_px=(50.0, 550.0, 950.0, 750.0),
            score=0.9,
        ),
        DetectedLayoutBox(
            raw_label="left_2",
            element_kind=ElementKind.PARAGRAPH,
            recommended_strategy=ParseStrategy.EMBEDDED_IMAGE_OCR,
            bbox_px=(50.0, 350.0, 460.0, 490.0),
            score=0.8,
        ),
        DetectedLayoutBox(
            raw_label="right_1",
            element_kind=ElementKind.PARAGRAPH,
            recommended_strategy=ParseStrategy.EMBEDDED_IMAGE_OCR,
            bbox_px=(530.0, 160.0, 950.0, 300.0),
            score=0.8,
        ),
        DetectedLayoutBox(
            raw_label="top_title",
            element_kind=ElementKind.HEADING,
            recommended_strategy=ParseStrategy.EMBEDDED_IMAGE_OCR,
            bbox_px=(50.0, 40.0, 950.0, 120.0),
            score=0.95,
        ),
        DetectedLayoutBox(
            raw_label="left_1",
            element_kind=ElementKind.PARAGRAPH,
            recommended_strategy=ParseStrategy.EMBEDDED_IMAGE_OCR,
            bbox_px=(50.0, 160.0, 460.0, 300.0),
            score=0.8,
        ),
    ]
    ordered = sort_layout_boxes_reading_order(two_col_boxes, page_width_px=1000.0)
    assert [b.raw_label for b in ordered] == [
        "top_title",
        "left_1",
        "left_2",
        "right_1",
        "right_2",
        "bottom_table",
    ]
    assert [b.reading_order for b in ordered] == [1, 2, 3, 4, 5, 6]

    # 모델이 이미 부여한 reading_order가 있으면 그대로 유지
    explicit = [
        DetectedLayoutBox(
            raw_label="second",
            element_kind=ElementKind.PARAGRAPH,
            recommended_strategy=ParseStrategy.EMBEDDED_IMAGE_OCR,
            bbox_px=(50.0, 50.0, 400.0, 120.0),
            score=0.9,
            reading_order=2,
        ),
        DetectedLayoutBox(
            raw_label="first",
            element_kind=ElementKind.HEADING,
            recommended_strategy=ParseStrategy.EMBEDDED_IMAGE_OCR,
            bbox_px=(50.0, 300.0, 400.0, 360.0),
            score=0.9,
            reading_order=1,
        ),
    ]
    preserved = sort_layout_boxes_reading_order(explicit, page_width_px=1000.0)
    assert [b.raw_label for b in preserved] == ["first", "second"]


# ---------------------------------------------------------------------------
# 3. 무의존성 휴리스틱 탐지기 + VLM 짧은 프롬프트 자동 분기 통합 테스트
# ---------------------------------------------------------------------------


def test_heuristic_detector_segments_scanned_pdf_and_routes_vlm_prompts(tmp_path: Path) -> None:
    """스캔 PDF를 제목·본문·표·차트로 자동 분할·크롭하고 영역별 VLM 프롬프트를 분기한다."""
    rgb = _build_synthetic_multi_element_scan_rgb(600, 840)
    pdf_path = _create_scanned_pdf(tmp_path / "scanned_report.pdf", rgb, 600, 840)

    # 1) 기본값(layout_detector 없음)에서는 단일 SCANNED_PAGE로 유지
    plain_doc = parse_document(pdf_path, options=ParseOptions(extract_crops=False))
    assert plain_doc.pages[0].route.category == PageCategory.SCANNED_IMAGE
    assert len(plain_doc.visual_regions) == 1
    assert plain_doc.visual_regions[0].element_kind == ElementKind.SCANNED_PAGE

    # 2) HeuristicRasterLayoutDetector + PaddleOCR-VL 루프백 모의 VLM 서버
    received_prompts: list[tuple[list[str], str]] = []

    def handler(
        path: str, _headers: dict[str, str], body: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        assert path == "/v1/chat/completions"
        msgs = body["messages"]
        roles = [m["role"] for m in msgs]
        user_text = msgs[-1]["content"][0]["text"]
        received_prompts.append((roles, user_text))
        if user_text == "Table Recognition:":
            reply = "| Col A | Col B | Col C |\n| --- | --- | --- |\n| 1 | 2 | 3 |"
        elif user_text == "Chart Recognition:":
            reply = "Bar chart with 3 colored series (Blue, Red, Green)."
        else:
            reply = "Detected text content"
        return 200, {
            "model": body["model"],
            "choices": [{"message": {"role": "assistant", "content": reply}}],
        }

    out_dir = tmp_path / "out_bundle"
    with _run_mock_server(handler) as base_url:
        vlm = OpenAICompatibleVlmAdapter(
            model="PaddleOCR-VL-1.6",
            base_url=f"{base_url}/v1",
        )
        detector = HeuristicRasterLayoutDetector()
        doc, manifest = parse_and_export(
            pdf_path,
            out_dir,
            vision_adapter=vlm,
            layout_detector=detector,
        )

    kinds = [v.element_kind for v in doc.visual_regions]
    assert ElementKind.SCANNED_PAGE not in kinds
    assert ElementKind.TABLE in kinds
    assert ElementKind.FIGURE_IMAGE in kinds
    assert any(k in (ElementKind.HEADING, ElementKind.PARAGRAPH) for k in kinds)
    assert len(doc.visual_regions) >= 3

    # 모든 분할 영역이 개별 부분 크롭 PNG로 저장됐고 전체 페이지(600x840)보다 작아야 함
    for vis in doc.visual_regions:
        assert vis.crop_status == CropStatus.SAVED
        assert vis.crop_path is not None
        crop_file = Path(vis.crop_path)
        assert crop_file.is_file()
        dec = decode_png(crop_file.read_bytes())
        assert dec is not None
        assert dec.height < 840
        assert vis.vision_status == VisionStatus.COMPLETED

    # PaddleOCR-VL 짧은 프롬프트가 영역 유형에 따라 정확히 분기됐는지 확인
    assert all(roles == ["user"] for roles, _ in received_prompts)
    prompt_texts = [p for _, p in received_prompts]
    assert "Table Recognition:" in prompt_texts
    assert "Chart Recognition:" in prompt_texts
    assert "OCR:" in prompt_texts

    # 리포트와 Markdown에 세분화 결과·표·차트가 모두 반영됐는지 확인
    report_md = manifest.routing_report_markdown.read_text(encoding="utf-8")
    doc_md = manifest.document_markdown.read_text(encoding="utf-8")
    assert "레이아웃 탐지(heuristic)로 스캔 페이지를" in report_md
    assert "| Col A | Col B | Col C |" in doc_md
    assert "Bar chart with 3 colored series" in doc_md


# ---------------------------------------------------------------------------
# 4. ONNX 레이아웃 탐지기 (Apache-2.0 PP-DocLayoutV2/V3) 단위 테스트
# ---------------------------------------------------------------------------


def test_onnx_detector_companion_labels_missing_deps_and_output_parsing(tmp_path: Path) -> None:
    """ONNX 동반 라벨 자동 로드, 미설치 의존성 안내, PP-DocLayoutV2·분리 텐서 파싱을 검증한다."""
    # 동반 inference.yml 자동 로드 검증
    model_dir = tmp_path / "pp_doc"
    model_dir.mkdir()
    model_file = model_dir / "PP-DocLayoutV2.onnx"
    model_file.write_bytes(b"fake-onnx")
    (model_dir / "inference.yml").write_text(
        "Global:\n  model_name: PP-DocLayoutV2\n"
        "label_list:\n  - doc_title\n  - table\n  - chart\n  - text\n",
        encoding="utf-8",
    )

    det = OnnxDocLayoutDetector(model_path=model_file, score_threshold=0.4)
    assert det.labels == ("doc_title", "table", "chart", "text")
    assert det.name == "onnx:PP-DocLayoutV2"

    # onnxruntime/numpy가 없는 환경에서는 깔끔한 ParserInputError 발생
    sample_png = encode_png(bytes([255, 255, 255] * 16), 4, 4, channels=3)
    if not det.available:
        with pytest.raises(ParserInputError, match=r"onnxruntime|numpy"):
            det.detect(
                LayoutDetectionInput(
                    page=1,
                    image_bytes=sample_png,
                    image_extension=".png",
                    image_width_px=4,
                    image_height_px=4,
                    page_width_pt=595.0,
                    page_height_pt=842.0,
                )
            )

    # 형태 A: PP-DocLayoutV2 `[N, 7]` ([cls_id, score, x0, y0, x1, y1, reading_order])
    raw_v2 = [
        [
            [1, 0.91, 50.0, 300.0, 550.0, 500.0, 1],  # table, order=2
            [0, 0.96, 50.0, 40.0, 450.0, 90.0, 0],  # doc_title, order=1
            [2, 0.84, 60.0, 540.0, 540.0, 760.0, 2],  # chart, order=3
            [3, 0.20, 60.0, 780.0, 300.0, 810.0, 3],  # threshold 미만 -> 제외
        ]
    ]
    parsed_v2 = det._parse_outputs(raw_v2, orig_w=600, orig_h=840, target_w=800, target_h=800)
    assert [b.element_kind for b in parsed_v2] == [
        ElementKind.HEADING,
        ElementKind.TABLE,
        ElementKind.FIGURE_IMAGE,
    ]
    assert [b.reading_order for b in parsed_v2] == [1, 2, 3]

    # 형태 B: 분리된 출력 `(boxes, scores, labels)` + [0..1] 정규화 좌표
    raw_sep = [
        [[0.1, 0.1, 0.9, 0.2], [0.1, 0.3, 0.9, 0.6]],  # boxes (normalized)
        [0.88, 0.93],  # scores
        [0, 1],  # labels: doc_title, table
    ]
    parsed_sep = det._parse_outputs(raw_sep, orig_w=1000, orig_h=1000, target_w=800, target_h=800)
    assert [b.element_kind for b in parsed_sep] == [ElementKind.HEADING, ElementKind.TABLE]
    assert parsed_sep[1].bbox_px == (100.0, 300.0, 900.0, 600.0)


def test_onnx_detector_config_json_and_factory_validation(tmp_path: Path) -> None:
    """config.json 라벨 로드 및 create_layout_detector 유효성 검사를 확인한다."""
    model_dir = tmp_path / "cfg_doc"
    model_dir.mkdir()
    model_file = model_dir / "layout.onnx"
    model_file.write_bytes(b"fake")
    (model_dir / "config.json").write_text(
        json.dumps({"id2label": {"0": "Title", "1": "Table", "2": "Formula"}}),
        encoding="utf-8",
    )
    det = OnnxDocLayoutDetector(model_path=model_file)
    assert det.labels == ("Title", "Table", "Formula")

    assert isinstance(create_layout_detector("none"), NoopLayoutDetector)
    assert isinstance(create_layout_detector("heuristic"), HeuristicRasterLayoutDetector)
    with pytest.raises(ParserInputError, match="layout-model-path"):
        create_layout_detector("onnx")
    with pytest.raises(ParserInputError, match="지원하지 않는 레이아웃 탐지기"):
        create_layout_detector("unknown-detector")


# ---------------------------------------------------------------------------
# 5. HTTP 레이아웃 탐지 서비스 + TrustZone 보안 경계 테스트
# ---------------------------------------------------------------------------


def test_http_layout_detector_trust_zone_and_loopback_integration(tmp_path: Path) -> None:
    """외부 HTTP 차단을 검증하고 루프백 레이아웃 서비스로 스캔 페이지를 세분화한다."""
    # 외부 호스트 및 fail-closed DNS(localhost)는 기본 차단
    with pytest.raises(VisionSecurityError):
        HttpLayoutDetector(base_url="https://api.example.com")
    with pytest.raises(VisionSecurityError):
        HttpLayoutDetector(base_url="http://localhost:8001")

    # 명시적 외부 허용 및 사내 IP 등록 허용
    ext_det = HttpLayoutDetector(base_url="https://api.example.com", allow_external=True)
    assert ext_det.trust_zone is TrustZone.EXTERNAL
    int_det = HttpLayoutDetector(
        base_url="http://10.20.30.40:8001",
        registered_internal=("10.20.30.40",),
    )
    assert int_det.trust_zone is TrustZone.REGISTERED_INTERNAL

    # 루프백 Mock HTTP 레이아웃 서비스 연동
    rgb = _build_synthetic_multi_element_scan_rgb(600, 840)
    pdf_path = _create_scanned_pdf(tmp_path / "http_scan.pdf", rgb, 600, 840)
    seen_auth: list[str | None] = []

    def layout_handler(
        path: str, headers: dict[str, str], body: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        assert path == "/v1/layout/detect"
        assert body["page"] == 1
        assert body["width_px"] == 600
        assert body["height_px"] == 840
        seen_auth.append(headers.get("Authorization"))
        return 200, {
            "coordinate_space": "normalized",
            "boxes": [
                {"label": "header", "bbox": [0.05, 0.01, 0.95, 0.04], "score": 0.91},
                {"label": "doc_title", "bbox": [0.10, 0.05, 0.65, 0.10], "score": 0.95},
                {"label": "table", "bbox": [0.10, 0.28, 0.90, 0.53], "score": 0.93},
                {"label": "display_formula", "bbox": [0.20, 0.56, 0.80, 0.61], "score": 0.89},
                {"label": "chart", "bbox": [0.12, 0.62, 0.88, 0.91], "score": 0.90},
            ],
        }

    with _run_mock_server(layout_handler) as base_url:
        http_det = HttpLayoutDetector(base_url=base_url, api_key="secret-token")
        doc = parse_document(
            pdf_path,
            options=ParseOptions(extract_crops=True, crop_dir=tmp_path / "crops"),
            layout_detector=http_det,
        )

    assert seen_auth == ["Bearer secret-token"]
    # header는 SKIP_DECORATIVE로 자동 제외되고 4개 영역만 남아야 함
    assert [v.element_kind for v in doc.visual_regions] == [
        ElementKind.HEADING,
        ElementKind.TABLE,
        ElementKind.FORMULA_OR_CODE,
        ElementKind.FIGURE_IMAGE,
    ]
    assert [v.recommended_strategy for v in doc.visual_regions] == [
        ParseStrategy.EMBEDDED_IMAGE_OCR,
        ParseStrategy.VLM_REGION,
        ParseStrategy.VLM_REGION,
        ParseStrategy.VLM_REGION,
    ]
    assert all(v.crop_status == CropStatus.SAVED for v in doc.visual_regions)


# ---------------------------------------------------------------------------
# 6. CLI 통합 테스트(--plan-only, HTTP+GLM-OCR, 실제 pdftoppm+Tesseract 스캔)
# ---------------------------------------------------------------------------


def test_cli_layout_detector_plan_only_and_two_stage_glm_ocr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI `--layout-detector heuristic --plan-only`와 2단계 `http + glm-ocr` 조합을 검증한다."""
    rgb = _build_synthetic_multi_element_scan_rgb(600, 840)
    pdf_path = _create_scanned_pdf(tmp_path / "cli_scan.pdf", rgb, 600, 840)

    # 1) --plan-only에서도 세분화된 표·그림 영역이 전략 리포트에 표시돼야 함
    rc_plan = cli_main(
        [
            "parse",
            str(pdf_path),
            "--plan-only",
            "--layout-detector",
            "heuristic",
            "--format",
            "report",
        ]
    )
    assert rc_plan == 0
    plan_out = capsys.readouterr().out
    assert "레이아웃 탐지(heuristic)" in plan_out
    assert "`table`" in plan_out
    assert "`figure_image`" in plan_out

    # 잘못된 threshold 검증
    rc_bad = cli_main(
        [
            "parse",
            str(pdf_path),
            "--layout-detector",
            "heuristic",
            "--layout-score-threshold",
            "1.5",
        ]
    )
    assert rc_bad == 2
    capsys.readouterr()

    # 2) 2단계 CLI: HTTP 레이아웃 탐지기 + GLM-OCR 루프백 서버
    glm_prompts: list[str] = []

    def layout_handler(
        _path: str, _headers: dict[str, str], _body: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        return 200, {
            "coordinate_space": "normalized",
            "boxes": [
                {"label": "text", "bbox": [0.10, 0.12, 0.90, 0.22], "score": 0.90},
                {"label": "table", "bbox": [0.10, 0.28, 0.90, 0.53], "score": 0.94},
                {"label": "display_formula", "bbox": [0.15, 0.56, 0.85, 0.62], "score": 0.88},
            ],
        }

    def vlm_handler(
        _path: str, _headers: dict[str, str], body: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        prompt = body["messages"][-1]["content"][0]["text"]
        glm_prompts.append(prompt)
        return 200, {
            "model": body["model"],
            "choices": [{"message": {"role": "assistant", "content": f"OK({prompt})"}}],
        }

    out_dir = tmp_path / "cli_out"
    with _run_mock_server(layout_handler) as layout_url, _run_mock_server(vlm_handler) as vlm_url:
        rc = cli_main(
            [
                "parse",
                str(pdf_path),
                "--layout-detector",
                "http",
                "--layout-base-url",
                layout_url,
                "--vision",
                "openai-compat",
                "--vision-model",
                "glm-ocr",
                "--vision-base-url",
                f"{vlm_url}/v1",
                "--out",
                str(out_dir),
            ]
        )
    assert rc == 0
    stdout = capsys.readouterr().out
    assert "레이아웃 탐지: http-layout" in stdout
    assert "시각 해석(openai-compat:glm-ocr): 완료 3/3개" in stdout
    assert glm_prompts == [
        "Text Recognition:",
        "Table Recognition:",
        "Formula Recognition:",
    ]
    assert (out_dir / "parsed.json").is_file()
    assert (out_dir / "document.md").is_file()


def test_live_rasterized_scanned_pdf_with_heuristic_and_tesseract(tmp_path: Path) -> None:
    """실제 `pdftoppm`으로 래스터화한 스캔 PDF에서 휴리스틱 분할 + Tesseract OCR을 검증한다."""
    if shutil.which("pdftoppm") is None or shutil.which("tesseract") is None:
        pytest.skip("pdftoppm 또는 tesseract 바이너리가 없어 실전 스캔 테스트를 건너뛴다.")

    # 1. 실제 텍스트·격자표·컬러 막대 차트가 든 디지털 PDF를 만든 뒤 pdftoppm으로 PNG 래스터화
    ops = "\n".join(
        [
            "BT /F1 22 Tf 60 770 Td (Quarterly Revenue Report) Tj ET",
            "BT /F1 11 Tf 60 715 Td "
            "(This report summarizes quarterly sales across three regions.) Tj ET",
            "BT /F1 11 Tf 60 698 Td "
            "(All figures are verified against internal accounting logs.) Tj ET",
            # 3x3 격자선 표 (PDF 좌하단 원점 y=440..620)
            "1.2 w 0 0 0 RG",
            "60 620 m 535 620 l S",
            "60 560 m 535 560 l S",
            "60 500 m 535 500 l S",
            "60 440 m 535 440 l S",
            "60 440 m 60 620 l S",
            "220 440 m 220 620 l S",
            "380 440 m 380 620 l S",
            "535 440 m 535 620 l S",
            "BT /F1 11 Tf 75 585 Td (Region) Tj ET",
            "BT /F1 11 Tf 235 585 Td (Q1 Sales) Tj ET",
            "BT /F1 11 Tf 395 585 Td (Q2 Sales) Tj ET",
            "BT /F1 11 Tf 75 525 Td (Seoul) Tj ET",
            "BT /F1 11 Tf 235 525 Td (1200) Tj ET",
            "BT /F1 11 Tf 395 525 Td (1450) Tj ET",
            "BT /F1 11 Tf 75 465 Td (Busan) Tj ET",
            "BT /F1 11 Tf 235 465 Td (850) Tj ET",
            "BT /F1 11 Tf 395 465 Td (920) Tj ET",
            # 컬러 막대 차트 (y=120..340)
            "1.5 w 0 0 0 RG 80 340 m 80 120 l 510 120 l S",
            "0.15 0.45 0.85 rg 130 122 65 130 re f",
            "0.85 0.25 0.20 rg 245 122 65 185 re f",
            "0.20 0.65 0.35 rg 360 122 65 155 re f",
        ]
    )
    digital_pdf = tmp_path / "source_digital.pdf"
    # Helvetica가 고정폭 글꼴로 대체될 때 글자 간격이 달라지므로 고정폭 원본을 사용한다.
    digital_pdf.write_bytes(_build_minimal_pdf(ops, base_font="Courier-Bold"))

    renderer = PdftoppmPageRenderer()
    rendered = renderer.render_region(digital_pdf, 1, None, dpi=120)
    assert rendered is not None
    decoded = decode_png(rendered.data)
    assert decoded is not None and decoded.channels == 3

    # 래스터 이미지만 넣은 순수 스캔 PDF 생성(디지털 텍스트 0자)
    scanned_pdf = _create_scanned_pdf(
        tmp_path / "real_scanned.pdf",
        decoded.pixels,
        decoded.width,
        decoded.height,
    )

    plan = inspect_document(scanned_pdf, layout_detector=HeuristicRasterLayoutDetector())
    assert plan.pages[0].category == PageCategory.SCANNED_IMAGE
    region_kinds = {r.element_kind for r in plan.pages[0].regions}
    assert ElementKind.TABLE in region_kinds
    assert ElementKind.FIGURE_IMAGE in region_kinds

    out_dir = tmp_path / "real_scan_out"
    rc = cli_main(
        [
            "parse",
            str(scanned_pdf),
            "--layout-detector",
            "heuristic",
            "--vision",
            "tesseract",
            "--ocr-lang",
            "eng",
            "--out",
            str(out_dir),
        ]
    )
    assert rc == 0
    doc_md = (out_dir / "document.md").read_text(encoding="utf-8")
    assert "Quarterly Revenue Report" in doc_md
    assert "Seoul" in doc_md or "1450" in doc_md


def test_layout_segmented_scan_with_hybrid_routed_vision_and_all_pages_augment(
    tmp_path: Path,
) -> None:
    """레이아웃 분할 + 하이브리드 분기 및 디지털 페이지 보조 탐지를 검증한다."""
    from autoetl.parser import RoutedVisionAdapter, VisionRequest, VisionResult

    # 1. 스캔 페이지: 텍스트/제목은 OCR로, 표/차트는 VLM으로 자동 분배
    rgb = _build_synthetic_multi_element_scan_rgb(600, 840)
    scanned_pdf = _create_scanned_pdf(tmp_path / "hybrid_scan.pdf", rgb, 600, 840)

    ocr_kinds: list[ElementKind] = []
    vlm_kinds: list[ElementKind] = []

    class _StubOcr:
        @property
        def name(self) -> str:
            return "stub-ocr"

        def parse_region(self, request: VisionRequest) -> VisionResult:
            ocr_kinds.append(request.element_kind)
            return VisionResult(
                adapter_name=self.name,
                succeeded=True,
                text="OCR text",
                markdown="OCR text",
            )

    class _StubVlm:
        @property
        def name(self) -> str:
            return "stub-vlm"

        def parse_region(self, request: VisionRequest) -> VisionResult:
            vlm_kinds.append(request.element_kind)
            return VisionResult(
                adapter_name=self.name,
                succeeded=True,
                text="VLM structured",
                markdown="VLM structured",
            )

    routed = RoutedVisionAdapter(
        ocr_adapter=_StubOcr(),
        vlm_adapter=_StubVlm(),
        inject_ocr_hints_to_vlm=False,
    )
    doc = parse_document(
        scanned_pdf,
        options=ParseOptions(extract_crops=True),
        vision_adapter=routed,
        layout_detector=HeuristicRasterLayoutDetector(),
    )
    assert ElementKind.TABLE in vlm_kinds
    assert ElementKind.FIGURE_IMAGE in vlm_kinds
    assert any(k in (ElementKind.HEADING, ElementKind.PARAGRAPH) for k in ocr_kinds)
    assert ElementKind.TABLE not in ocr_kinds
    assert all(v.vision_status == VisionStatus.COMPLETED for v in doc.visual_regions)

    # 2. 디지털 페이지 + layout_detect_all_pages=True: 미탐지 수식만 추가 보강
    digital_pdf = tmp_path / "digital_formula.pdf"
    digital_ops = "\n".join(
        [
            "BT /F1 14 Tf 60 760 Td (Introduction to Document Analysis) Tj ET",
            "BT /F1 11 Tf 60 720 Td "
            "(This digital PDF contains normal prose text across lines.) Tj ET",
            "BT /F1 11 Tf 60 700 Td "
            "(Additional sentence so page is classified as digital text.) Tj ET",
        ]
    )
    digital_pdf.write_bytes(_build_minimal_pdf(digital_ops))

    class _FormulaStubDetector:
        @property
        def name(self) -> str:
            return "stub-formula"

        @property
        def available(self) -> bool:
            return True

        def detect(self, inp: LayoutDetectionInput) -> list[DetectedLayoutBox]:
            return [
                DetectedLayoutBox(
                    raw_label="display_formula",
                    element_kind=ElementKind.FORMULA_OR_CODE,
                    recommended_strategy=ParseStrategy.VLM_REGION,
                    bbox_px=(
                        inp.image_width_px * 0.15,
                        inp.image_height_px * 0.45,
                        inp.image_width_px * 0.85,
                        inp.image_height_px * 0.55,
                    ),
                    score=0.92,
                )
            ]

    # 기본값(layout_detect_all_pages=False)에서는 디지털 페이지에 실행하지 않음
    doc_default = parse_document(
        digital_pdf,
        options=ParseOptions(extract_crops=False, layout_detect_all_pages=False),
        layout_detector=_FormulaStubDetector(),
    )
    assert doc_default.pages[0].route.category == PageCategory.DIGITAL_TEXT
    assert len(doc_default.visual_regions) == 0

    # layout_detect_all_pages=True이면 디지털 페이지에도 수식 영역이 보강됨(렌더러 필요)
    if shutil.which("pdftoppm") is not None:
        doc_aug = parse_document(
            digital_pdf,
            options=ParseOptions(extract_crops=True, layout_detect_all_pages=True),
            layout_detector=_FormulaStubDetector(),
        )
        assert [v.element_kind for v in doc_aug.visual_regions] == [ElementKind.FORMULA_OR_CODE]
        assert any("보강" in r for r in doc_aug.pages[0].route.reasons)
