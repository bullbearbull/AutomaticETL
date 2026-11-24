"""Auto Parser 시각 영역(OCR/VLM) 어댑터·보안 경계·라우팅·사후 적용·CLI 테스트."""

from __future__ import annotations

import base64
import json
import shutil
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
    BBox,
    ElementKind,
    NoopVisionAdapter,
    OllamaVisionAdapter,
    OpenAICompatibleVlmAdapter,
    PageCategory,
    ParsedDocument,
    ParseOptions,
    ParserInputError,
    ParseStrategy,
    RoutedVisionAdapter,
    TesseractOcrAdapter,
    VisionPromptPreset,
    VisionRequest,
    VisionResult,
    VisionSecurityError,
    VisionStatus,
    apply_vision_to_document,
    build_vision_prompts,
    create_vision_adapter,
    parse_and_export,
    parse_document,
    render_document_markdown,
    resolve_prompt_preset,
    validate_vision_endpoint,
)
from autoetl.parser.adapters import PdftoppmPageRenderer
from autoetl.parser.image_io import encode_png
from autoetl.security.levels import TrustZone

# ---------------------------------------------------------------------------
# 테스트용 최소 PDF 생성기 & 로컬 루프백 Mock HTTP 서버
# ---------------------------------------------------------------------------


def _build_minimal_pdf(
    ops: str,
    *,
    width: float = 595.0,
    height: float = 842.0,
    images: list[tuple[str, int, int, bytes]] | None = None,
) -> bytes:
    """텍스트 또는 RGB 이미지가 들어 있는 최소 유효 PDF 1.4 바이트를 생성한다."""
    objs: list[bytes] = []

    def add_obj(body: bytes) -> int:
        objs.append(body)
        return len(objs)

    f_reg = add_obj(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
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


def _sample_png_bytes(width: int = 8, height: int = 8) -> bytes:
    rgb = bytes([240, 240, 240] * (width * height))
    return encode_png(rgb, width, height, channels=3)


def _sample_bbox() -> BBox:
    return BBox(
        x0=50.0,
        top=100.0,
        x1=350.0,
        bottom=300.0,
        norm_x0=0.1,
        norm_top=0.12,
        norm_x1=0.6,
        norm_bottom=0.36,
    )


@contextmanager
def _mock_vision_http_server(
    handler_fn: Any,
) -> Iterator[tuple[str, list[dict[str, Any]]]]:
    """127.0.0.1 임시 포트로 로컬 Mock VLM HTTP 서버를 띄운다."""
    captured: list[dict[str, Any]] = []

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def do_GET(self) -> None:
            record = {
                "method": "GET",
                "path": self.path,
                "auth": self.headers.get("Authorization"),
            }
            captured.append(record)
            status, body = handler_fn("GET", self.path, None, self.headers)
            raw = json.dumps(body).encode("utf-8") if isinstance(body, dict) else body
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or "0")
            body_raw = self.rfile.read(length) if length > 0 else b"{}"
            payload = json.loads(body_raw.decode("utf-8"))
            captured.append(
                {
                    "method": "POST",
                    "path": self.path,
                    "auth": self.headers.get("Authorization"),
                    "payload": payload,
                }
            )
            status, resp_body = handler_fn("POST", self.path, payload, self.headers)
            raw = (
                json.dumps(resp_body).encode("utf-8") if isinstance(resp_body, dict) else resp_body
            )
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}", captured
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


# ---------------------------------------------------------------------------
# 1. 보안 경계(TrustZone) & 프롬프트 빌더 검증
# ---------------------------------------------------------------------------


def test_validate_vision_endpoint_trust_zone_policy() -> None:
    """루프백·등록 사내 서버는 기본 허용하고 외부·미등록·DNS 호스트는 기본 차단한다."""
    assert validate_vision_endpoint("http://127.0.0.1:11434/v1") is TrustZone.LOOPBACK
    assert validate_vision_endpoint("http://[::1]:8000/v1") is TrustZone.LOOPBACK
    assert (
        validate_vision_endpoint(
            "http://10.20.30.40:8000/v1",
            registered_internal=("10.20.30.40",),
        )
        is TrustZone.REGISTERED_INTERNAL
    )

    # 기본값에서는 외부·DNS·미등록 사설·등록 목록에 섞인 공인 IP 모두 차단(실패 닫힘)
    for blocked in (
        "https://api.openai.com/v1",
        "http://localhost:11434/v1",
        "http://10.20.30.40:8000/v1",
        "https://8.8.8.8/v1",
    ):
        with pytest.raises(VisionSecurityError):
            validate_vision_endpoint(blocked, registered_internal=("8.8.8.8",))

    # 명시적 allow_external=True 시에만 외부 엔드포인트 허용
    assert (
        validate_vision_endpoint("https://api.openai.com/v1", allow_external=True)
        is TrustZone.EXTERNAL
    )

    # 잘못된 스킴 거부
    with pytest.raises(ParserInputError):
        validate_vision_endpoint("file:///etc/passwd")


def test_build_vision_prompts_includes_strategy_and_context() -> None:
    """전략(스캔/표/차트)별로 특화된 지시와 캡션·힌트·주변 문맥이 프롬프트에 포함된다."""
    req = VisionRequest(
        region_id="p003_v01",
        page=3,
        element_kind=ElementKind.FIGURE_IMAGE,
        recommended_strategy=ParseStrategy.VLM_REGION,
        bbox=_sample_bbox(),
        reason="내장 차트 이미지",
        embedded_text_hints=("2021", "2023", "고용률(%)"),
        caption="<그림 1> 고용률 추세",
        context_before="이전 문단에서는 팬데믹 전후 추세를 설명한다.",
        context_after="다음 문단에서는 정책 시사점을 정리한다.",
    )
    sys_p, usr_p = build_vision_prompts(req, ocr_hint="64 63 61", language="ko")
    assert sys_p is not None
    assert "GitHub-Flavored Markdown" in sys_p
    assert "<그림 1> 고용률 추세" in usr_p
    assert "고용률(%)" in usr_p
    assert "64 63 61" in usr_p
    assert "팬데믹 전후 추세" in usr_p
    assert "정책 시사점" in usr_p

    scan_req = VisionRequest(
        region_id="p005_v01",
        page=5,
        element_kind=ElementKind.SCANNED_PAGE,
        recommended_strategy=ParseStrategy.EMBEDDED_IMAGE_OCR,
        bbox=_sample_bbox(),
        reason="스캔 페이지",
    )
    _, scan_usr = build_vision_prompts(scan_req, language="en")
    assert "scanned page" in scan_usr.lower()


# ---------------------------------------------------------------------------
# 2. OpenAI 호환 VLM & Ollama 네이티브 VLM 어댑터 (루프백 Mock HTTP 실통신)
# ---------------------------------------------------------------------------


def test_openai_compatible_vlm_adapter_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """OpenAI 호환 VLM 어댑터가 헤더·base64 이미지·응답·오류 은닉을 정확히 처리한다."""
    secret_token = "sk-very-secret-test-key-9999"
    monkeypatch.setenv("AUTOETL_TEST_VLM_KEY", secret_token)

    def handler(method: str, path: str, payload: Any, headers: Any) -> tuple[int, dict[str, Any]]:
        if method == "GET" and path == "/v1/models":
            return 200, {"data": [{"id": "qwen2.5vl:7b"}]}
        if method == "POST" and path == "/v1/chat/completions":
            return 200, {
                "model": "qwen2.5vl:7b",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "| Year | Rate |\n| --- | --- |\n| 2024 | 2.0% |",
                        }
                    }
                ],
                "usage": {"prompt_tokens": 310, "completion_tokens": 28, "total_tokens": 338},
            }
        return 404, {"error": "not_found"}

    with _mock_vision_http_server(handler) as (base, captured):
        adapter = OpenAICompatibleVlmAdapter(
            model="qwen2.5vl:7b",
            base_url=f"{base}/v1",
            api_key_env="AUTOETL_TEST_VLM_KEY",
        )
        ok, status = adapter.check_health()
        assert ok is True and status == "ok"

        req = VisionRequest(
            region_id="p003_v01",
            page=3,
            element_kind=ElementKind.FIGURE_IMAGE,
            recommended_strategy=ParseStrategy.VLM_REGION,
            bbox=_sample_bbox(),
            reason="차트 이미지",
            image_bytes=_sample_png_bytes(),
            image_extension=".png",
            caption="Figure 1: Growth Rate",
            context_before="Summary before figure.",
        )
        res = adapter.parse_region(req)
        assert res.succeeded is True
        assert res.adapter_name == "openai-compat:qwen2.5vl:7b"
        assert "| 2024 | 2.0% |" in res.markdown
        assert res.metadata["trust_zone"] == "loopback"
        assert res.metadata["total_tokens"] == "338"

        post_call = next(c for c in captured if c["method"] == "POST")
        assert post_call["auth"] == f"Bearer {secret_token}"
        user_content = post_call["payload"]["messages"][1]["content"]
        assert any(
            part.get("type") == "image_url"
            and part["image_url"]["url"].startswith("data:image/png;base64,")
            for part in user_content
        )

    # HTTP 오류 시 API 키·응답 원문이 오류 필드에 노출되지 않음
    def err_handler(
        method: str, path: str, payload: Any, headers: Any
    ) -> tuple[int, dict[str, Any]]:
        return 401, {"error": f"invalid key {secret_token}"}

    with _mock_vision_http_server(err_handler) as (base, _):
        bad_adapter = OpenAICompatibleVlmAdapter(
            model="qwen2.5vl:7b",
            base_url=f"{base}/v1",
            api_key_env="AUTOETL_TEST_VLM_KEY",
        )
        err_res = bad_adapter.parse_region(req)
        assert err_res.succeeded is False
        assert err_res.error == "http_401"
        assert secret_token not in str(err_res)


def test_ollama_vision_adapter_end_to_end() -> None:
    """Ollama 네이티브 /api/chat 어댑터가 images[base64] 페이로드와 응답을 처리한다."""

    def handler(method: str, path: str, payload: Any, headers: Any) -> tuple[int, dict[str, Any]]:
        assert path == "/api/chat"
        assert payload["stream"] is False
        images = payload["messages"][1]["images"]
        assert len(images) == 1
        assert base64.b64decode(images[0]).startswith(b"\x89PNG\r\n\x1a\n")
        return 200, {
            "model": "llama3.2-vision:11b",
            "message": {"role": "assistant", "content": "Architecture diagram summary."},
            "prompt_eval_count": 210,
            "eval_count": 15,
        }

    with _mock_vision_http_server(handler) as (base, _):
        adapter = OllamaVisionAdapter(model="llama3.2-vision:11b", base_url=base)
        req = VisionRequest(
            region_id="p004_v01",
            page=4,
            element_kind=ElementKind.VECTOR_DIAGRAM,
            recommended_strategy=ParseStrategy.VLM_REGION,
            bbox=_sample_bbox(),
            reason="벡터 도식",
            image_bytes=_sample_png_bytes(),
        )
        res = adapter.parse_region(req)
        assert res.succeeded is True
        assert res.adapter_name == "ollama:llama3.2-vision:11b"
        assert res.markdown == "Architecture diagram summary."
        assert res.metadata["completion_tokens"] == "15"


# ---------------------------------------------------------------------------
# 3. 하이브리드 전략 라우터 (OCR 우선 / VLM 교차검증 / VLM 실패 시 OCR 폴백)
# ---------------------------------------------------------------------------


def test_routed_vision_adapter_strategy_routing_cross_check_and_fallback() -> None:
    """RoutedVisionAdapter의 전략 분배, OCR 힌트 교차 주입, VLM 실패 폴백을 검증한다."""
    seen_vlm_requests: list[VisionRequest] = []

    class _StubOcr:
        @property
        def name(self) -> str:
            return "stub-ocr"

        def parse_region(self, request: VisionRequest) -> VisionResult:
            return VisionResult(
                adapter_name=self.name,
                succeeded=True,
                text="2024 Q4 Revenue 15800",
                markdown="2024 Q4 Revenue 15800",
            )

    class _StubVlm:
        def __init__(self, *, should_fail: bool = False) -> None:
            self._should_fail = should_fail

        @property
        def name(self) -> str:
            return "stub-vlm"

        def parse_region(self, request: VisionRequest) -> VisionResult:
            seen_vlm_requests.append(request)
            if self._should_fail:
                return VisionResult(adapter_name=self.name, succeeded=False, error="timeout")
            return VisionResult(
                adapter_name=self.name,
                succeeded=True,
                text="Chart summary",
                markdown="Chart summary",
            )

    routed = RoutedVisionAdapter(
        ocr_adapter=_StubOcr(),
        vlm_adapter=_StubVlm(should_fail=False),
        inject_ocr_hints_to_vlm=True,
    )

    # 1) EMBEDDED_IMAGE_OCR 전략 -> 빠른 로컬 OCR 우선
    scan_req = VisionRequest(
        region_id="p001_v01",
        page=1,
        element_kind=ElementKind.SCANNED_PAGE,
        recommended_strategy=ParseStrategy.EMBEDDED_IMAGE_OCR,
        bbox=_sample_bbox(),
        reason="스캔 페이지",
        image_bytes=_sample_png_bytes(),
    )
    scan_res = routed.parse_region(scan_req)
    assert scan_res.succeeded is True
    assert scan_res.adapter_name == "stub-ocr"
    assert scan_res.metadata["routed_via"] == "ocr_primary"
    assert len(seen_vlm_requests) == 0

    # 2) VLM_REGION 전략 -> OCR 텍스트를 교차검증 힌트로 주입해 VLM 호출
    fig_req = VisionRequest(
        region_id="p002_v01",
        page=2,
        element_kind=ElementKind.FIGURE_IMAGE,
        recommended_strategy=ParseStrategy.VLM_REGION,
        bbox=_sample_bbox(),
        reason="차트 이미지",
        image_bytes=_sample_png_bytes(),
        embedded_text_hints=("ExistingLabel",),
    )
    fig_res = routed.parse_region(fig_req)
    assert fig_res.succeeded is True
    assert fig_res.adapter_name == "stub-vlm"
    assert fig_res.metadata["routed_via"] == "vlm_primary"
    assert fig_res.metadata["ocr_cross_check"] == "true"
    assert "2024 Q4 Revenue 15800" in seen_vlm_requests[-1].embedded_text_hints
    assert "ExistingLabel" in seen_vlm_requests[-1].embedded_text_hints

    # 3) VLM 실패 시 OCR로 안전하게 폴백
    routed_fallback = RoutedVisionAdapter(
        ocr_adapter=_StubOcr(),
        vlm_adapter=_StubVlm(should_fail=True),
        fallback_to_ocr=True,
    )
    fb_res = routed_fallback.parse_region(fig_req)
    assert fb_res.succeeded is True
    assert fb_res.adapter_name == "stub-ocr"
    assert fb_res.metadata["routed_via"] == "ocr_fallback"
    assert fb_res.metadata["vlm_error"] == "timeout"


# ---------------------------------------------------------------------------
# 4. 실제 Tesseract OCR(스캔 PDF 생성 → 크롭 → OCR) & 사후 적용 & CLI 연동
# ---------------------------------------------------------------------------


def _make_real_scanned_pdf(tmp_path: Path) -> Path:
    """텍스트 PDF를 pdftoppm으로 래스터화한 뒤 순수 이미지 스캔 PDF로 감싼다."""
    text_ops = (
        "BT /F1 24 Tf 1 0 0 1 60 720 Tm (AUTOETL SCANNED INVOICE) Tj ET\n"
        "BT /F1 16 Tf 1 0 0 1 60 670 Tm (Customer: ACME Corp) Tj ET\n"
        "BT /F1 16 Tf 1 0 0 1 60 635 Tm (Total Amount: 15800 USD) Tj ET\n"
    )
    src_pdf = tmp_path / "clean_source.pdf"
    src_pdf.write_bytes(_build_minimal_pdf(text_ops))

    renderer = PdftoppmPageRenderer()
    assert renderer.available
    rendered = renderer.render_region(src_pdf, 1, None, dpi=150)
    assert rendered is not None

    # PNG를 디코딩하는 대신 pypdfium2/PIL 없이도 바로 넣을 수 있게 DCT/PNG 대신
    # pdftoppm의 raw PPM(P6)으로 RGB 픽셀을 얻어 순수 이미지 PDF를 조립한다.
    import subprocess

    ppm_prefix = tmp_path / "scan_ppm"
    subprocess.run(
        [
            "pdftoppm",
            "-r",
            "150",
            "-f",
            "1",
            "-l",
            "1",
            "-singlefile",
            str(src_pdf),
            str(ppm_prefix),
        ],
        check=True,
        capture_output=True,
    )
    ppm_bytes = (tmp_path / "scan_ppm.ppm").read_bytes()
    # P6\n<w> <h>\n255\n<rgb>
    parts = ppm_bytes.split(b"\n", 3)
    w_str, h_str = parts[1].decode("ascii").split()
    px_w, px_h = int(w_str), int(h_str)
    rgb_raw = parts[3]

    scan_ops = "q 595.0 0 0 842.0 0 0 cm /ScanImg Do Q"
    scan_pdf = tmp_path / "real_scanned.pdf"
    scan_pdf.write_bytes(_build_minimal_pdf(scan_ops, images=[("ScanImg", px_w, px_h, rgb_raw)]))
    return scan_pdf


@pytest.mark.skipif(
    shutil.which("tesseract") is None or shutil.which("pdftoppm") is None,
    reason="실제 tesseract 또는 pdftoppm 바이너리가 없는 환경",
)
def test_tesseract_live_scanned_pdf_and_posthoc_apply_and_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """실제 스캔 PDF에 대해 Tesseract OCR 직접 실행, 사후 적용, CLI 연동을 끝까지 검증한다."""
    scan_pdf = _make_real_scanned_pdf(tmp_path)

    # 1) 결정론 전용 1차 파싱(Noop) -> 스캔 페이지 분류 + 크롭 저장 + PENDING 상태
    bundle_dir = tmp_path / "bundle_step1"
    doc_step1, manifest = parse_and_export(scan_pdf, bundle_dir)
    assert doc_step1.pages[0].route.category == PageCategory.SCANNED_IMAGE
    assert len(doc_step1.visual_regions) == 1
    assert doc_step1.visual_regions[0].vision_status == VisionStatus.PENDING
    assert len(manifest.crop_paths) == 1

    # 2) 저장된 parsed.json 로드 후 apply_vision_to_document로 사후 Tesseract OCR 실행
    reloaded = ParsedDocument.model_validate_json(manifest.parsed_json.read_text(encoding="utf-8"))
    ocr = TesseractOcrAdapter(lang="kor+eng")
    doc_after = apply_vision_to_document(reloaded, ocr)
    vis = doc_after.visual_regions[0]
    assert vis.vision_status == VisionStatus.COMPLETED
    assert vis.vision_adapter == "tesseract"
    assert vis.vision_text is not None
    assert "SCANNED INVOICE" in vis.vision_text
    assert "15800" in vis.vision_text
    assert "avg_confidence" in vis.vision_metadata
    md_after = render_document_markdown(doc_after, relative_to=bundle_dir)
    assert "SCANNED INVOICE" in md_after
    assert "시각 해석:" in md_after

    # 3) CLI: autoetl parse --vision tesseract --out ...
    cli_out = tmp_path / "cli_tesseract"
    code = cli_main(["parse", str(scan_pdf), "--vision", "tesseract", "--out", str(cli_out)])
    assert code == 0
    stdout = capsys.readouterr().out
    assert "시각 해석(tesseract): 완료 1/1개" in stdout
    cli_md = (cli_out / "document.md").read_text(encoding="utf-8")
    assert "ACME Corp" in cli_md


def test_cli_vision_openai_compat_and_security_block(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI --vision openai-compat 실통신, 외부 차단, --no-crops 충돌, 팩토리 생성을 검증한다."""
    # 간단한 내장 이미지 포함 문서 PDF 생성 (앞뒤 텍스트 문맥 포함)
    px_w, px_h = 20, 16
    rgb_raw = bytes([180, 200, 220] * (px_w * px_h))
    ops = (
        "BT /F1 12 Tf 1 0 0 1 60 760 Tm (Context before the embedded chart.) Tj ET\n"
        "q 240 0 0 140 60 540 cm /ChartImg Do Q\n"
        "BT /F1 12 Tf 1 0 0 1 60 490 Tm (Context after the embedded chart.) Tj ET\n"
    )
    pdf_path = tmp_path / "chart_doc.pdf"
    pdf_path.write_bytes(_build_minimal_pdf(ops, images=[("ChartImg", px_w, px_h, rgb_raw)]))

    # 1) --vision과 --no-crops 동시 지정 거부(exit 2)
    assert cli_main(["parse", str(pdf_path), "--vision", "tesseract", "--no-crops"]) == 2
    capsys.readouterr()

    # 2) 외부 URL은 --allow-external-vision 없이 차단(exit 1)
    code_blocked = cli_main(
        [
            "parse",
            str(pdf_path),
            "--vision",
            "openai-compat",
            "--vision-model",
            "gpt-4o",
            "--vision-base-url",
            "https://api.openai.com/v1",
        ]
    )
    assert code_blocked == 1
    assert "차단" in capsys.readouterr().err

    # 3) 로컬 루프백 VLM 서버와 CLI end-to-end 연동 + 앞뒤 문맥 전달 검증
    def handler(method: str, path: str, payload: Any, headers: Any) -> tuple[int, dict[str, Any]]:
        return 200, {
            "model": "local-vlm",
            "choices": [{"message": {"role": "assistant", "content": "Extracted chart data: 42%"}}],
        }

    with _mock_vision_http_server(handler) as (base, captured):
        out_dir = tmp_path / "cli_vlm_out"
        code_ok = cli_main(
            [
                "parse",
                str(pdf_path),
                "--vision",
                "openai-compat",
                "--vision-model",
                "local-vlm",
                "--vision-base-url",
                f"{base}/v1",
                "--out",
                str(out_dir),
            ]
        )
        assert code_ok == 0
        out_txt = capsys.readouterr().out
        assert "시각 해석(openai-compat:local-vlm): 완료 1/1개" in out_txt
        doc_md = (out_dir / "document.md").read_text(encoding="utf-8")
        assert "Extracted chart data: 42%" in doc_md

        # 앞뒤 문맥이 실제 VLM 프롬프트에 주입됐는지 확인
        user_text = captured[0]["payload"]["messages"][1]["content"][0]["text"]
        assert "Context before the embedded chart." in user_text
        assert "Context after the embedded chart." in user_text

    # 4) 팩토리 기본 동작 및 미지원 프로바이더 오류
    assert isinstance(create_vision_adapter("none"), NoopVisionAdapter)
    assert isinstance(create_vision_adapter("hybrid"), RoutedVisionAdapter)
    with pytest.raises(ParserInputError):
        create_vision_adapter("unknown-provider")

    # 5) 어댑터 없는 parse_document 기본 경로는 여전히 PENDING 유지
    plain = parse_document(pdf_path, options=ParseOptions(extract_crops=True))
    assert plain.visual_regions[0].vision_status == VisionStatus.PENDING


def test_ocr_prompt_presets_auto_detection_and_triggers() -> None:
    """GLM-OCR·PaddleOCR-VL·DeepSeek-OCR 모델명 자동 감지 및 공식 짧은 트리거를 검증한다."""
    assert resolve_prompt_preset("auto", model="glm-ocr:latest") is VisionPromptPreset.GLM_OCR
    assert (
        resolve_prompt_preset("auto", model="MedAIBase/PaddleOCR-VL:0.9b")
        is VisionPromptPreset.PADDLEOCR_VL
    )
    assert (
        resolve_prompt_preset("auto", model="PaddleOCR-VL-1.6-GGUF")
        is VisionPromptPreset.PADDLEOCR_VL
    )
    assert resolve_prompt_preset("auto", model="deepseek_ocr_2") is VisionPromptPreset.DEEPSEEK_OCR
    assert resolve_prompt_preset("auto", model="qwen2.5vl:7b") is VisionPromptPreset.GENERIC
    assert resolve_prompt_preset("glm-ocr", model="my-custom-name") is VisionPromptPreset.GLM_OCR

    with pytest.raises(ParserInputError):
        resolve_prompt_preset("invalid-preset")

    def make_req(kind: ElementKind, strategy: ParseStrategy) -> VisionRequest:
        return VisionRequest(
            region_id="r1",
            page=1,
            element_kind=kind,
            recommended_strategy=strategy,
            bbox=_sample_bbox(),
            reason="test",
            caption="ignored-for-short-preset",
        )

    # GLM-OCR 공식 트리거 (system=None)
    assert build_vision_prompts(
        make_req(ElementKind.SCANNED_PAGE, ParseStrategy.EMBEDDED_IMAGE_OCR),
        preset=VisionPromptPreset.GLM_OCR,
    ) == (None, "Text Recognition:")
    assert build_vision_prompts(
        make_req(ElementKind.TABLE, ParseStrategy.VLM_REGION),
        preset=VisionPromptPreset.GLM_OCR,
    ) == (None, "Table Recognition:")
    assert build_vision_prompts(
        make_req(ElementKind.FORMULA_OR_CODE, ParseStrategy.VLM_REGION),
        preset=VisionPromptPreset.GLM_OCR,
    ) == (None, "Formula Recognition:")

    # PaddleOCR-VL 공식 트리거 (system=None)
    assert build_vision_prompts(
        make_req(ElementKind.SCANNED_PAGE, ParseStrategy.EMBEDDED_IMAGE_OCR),
        preset=VisionPromptPreset.PADDLEOCR_VL,
    ) == (None, "OCR:")
    assert build_vision_prompts(
        make_req(ElementKind.TABLE, ParseStrategy.VLM_REGION),
        preset=VisionPromptPreset.PADDLEOCR_VL,
    ) == (None, "Table Recognition:")
    assert build_vision_prompts(
        make_req(ElementKind.FIGURE_IMAGE, ParseStrategy.VLM_REGION),
        preset=VisionPromptPreset.PADDLEOCR_VL,
    ) == (None, "Chart Recognition:")
    assert build_vision_prompts(
        make_req(ElementKind.FORMULA_OR_CODE, ParseStrategy.VLM_REGION),
        preset=VisionPromptPreset.PADDLEOCR_VL,
    ) == (None, "Formula Recognition:")

    # DeepSeek-OCR 공식 트리거 (system=None)
    assert build_vision_prompts(
        make_req(ElementKind.SCANNED_PAGE, ParseStrategy.EMBEDDED_IMAGE_OCR),
        preset=VisionPromptPreset.DEEPSEEK_OCR,
    ) == (None, "Free OCR.")
    assert build_vision_prompts(
        make_req(ElementKind.SCANNED_PAGE, ParseStrategy.VLM_FULL_PAGE),
        preset=VisionPromptPreset.DEEPSEEK_OCR,
    ) == (None, "<|grounding|>Convert the document to markdown.")
    assert build_vision_prompts(
        make_req(ElementKind.FIGURE_IMAGE, ParseStrategy.VLM_REGION),
        preset=VisionPromptPreset.DEEPSEEK_OCR,
    ) == (None, "Parse the figure.")


def test_ocr_preset_adapters_omit_system_message_and_clean_deepseek_tags() -> None:
    """전용 OCR 프리셋의 system 턴 생략·grounding 태그 정리·사전 OCR 생략을 검증한다."""
    fig_req = VisionRequest(
        region_id="p003_v01",
        page=3,
        element_kind=ElementKind.FIGURE_IMAGE,
        recommended_strategy=ParseStrategy.VLM_REGION,
        bbox=_sample_bbox(),
        reason="차트",
        image_bytes=_sample_png_bytes(),
    )

    # 1) Ollama + glm-ocr 자동 감지: 단일 user 턴('Text Recognition:')만 전송
    def ollama_handler(
        method: str, path: str, payload: Any, headers: Any
    ) -> tuple[int, dict[str, Any]]:
        return 200, {"model": "glm-ocr", "message": {"content": "Recognized Korean text."}}

    with _mock_vision_http_server(ollama_handler) as (base, captured):
        adapter = OllamaVisionAdapter(model="glm-ocr", base_url=base)
        assert adapter.prompt_preset is VisionPromptPreset.GLM_OCR
        res = adapter.parse_region(fig_req)
        assert res.succeeded is True
        assert res.metadata["prompt_preset"] == "glm-ocr"
        msgs = captured[0]["payload"]["messages"]
        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"
        assert msgs[0]["content"] == "Text Recognition:"

    # 2) OpenAI-compat + PaddleOCR-VL-1.6 자동 감지: 단일 user 턴('Chart Recognition:')
    def paddle_handler(
        method: str, path: str, payload: Any, headers: Any
    ) -> tuple[int, dict[str, Any]]:
        return 200, {
            "model": "PaddleOCR-VL-1.6",
            "choices": [{"message": {"content": "| Year | Value |\n| --- | --- |\n| 2025 | 64 |"}}],
        }

    with _mock_vision_http_server(paddle_handler) as (base, captured):
        adapter_p = OpenAICompatibleVlmAdapter(model="PaddleOCR-VL-1.6", base_url=f"{base}/v1")
        assert adapter_p.prompt_preset is VisionPromptPreset.PADDLEOCR_VL
        res_p = adapter_p.parse_region(fig_req)
        assert res_p.succeeded is True
        assert res_p.metadata["prompt_preset"] == "paddleocr-vl"
        msgs = captured[0]["payload"]["messages"]
        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"
        assert msgs[0]["content"][0]["text"] == "Chart Recognition:"

        # RoutedVisionAdapter가 짧은 OCR 프리셋 VLM에는 불필요한 사전 OCR을 생략하는지 확인
        ocr_calls = 0

        class _SpyOcr:
            @property
            def name(self) -> str:
                return "spy-ocr"

            def parse_region(self, request: VisionRequest) -> VisionResult:
                nonlocal ocr_calls
                ocr_calls += 1
                return VisionResult(adapter_name=self.name, succeeded=True, text="fallback")

        routed = RoutedVisionAdapter(ocr_adapter=_SpyOcr(), vlm_adapter=adapter_p)
        routed_res = routed.parse_region(fig_req)
        assert routed_res.succeeded is True
        assert ocr_calls == 0
        assert routed_res.metadata["ocr_cross_check"] == "false"

    # 3) DeepSeek-OCR grounding 태그(<|ref|>...<|/ref|><|det|>...<|/det|>) 자동 정리
    def ds_handler(
        method: str, path: str, payload: Any, headers: Any
    ) -> tuple[int, dict[str, Any]]:
        raw = (
            "<|ref|>title<|/ref|><|det|>[[10, 20, 300, 60]]<|/det|>\n"
            "# Annual Report\n\n\n"
            "<|ref|>text<|/ref|><|det|>[[10, 70, 500, 120]]<|/det|>\n"
            "Revenue rose 12%."
        )
        return 200, {"model": "deepseek-ocr", "choices": [{"message": {"content": raw}}]}

    with _mock_vision_http_server(ds_handler) as (base, _):
        adapter_ds = OpenAICompatibleVlmAdapter(model="deepseek-ocr", base_url=f"{base}/v1")
        scan_req = VisionRequest(
            region_id="p001_v01",
            page=1,
            element_kind=ElementKind.SCANNED_PAGE,
            recommended_strategy=ParseStrategy.VLM_FULL_PAGE,
            bbox=_sample_bbox(),
            reason="스캔",
            image_bytes=_sample_png_bytes(),
        )
        res_ds = adapter_ds.parse_region(scan_req)
        assert res_ds.succeeded is True
        assert res_ds.markdown == "# Annual Report\n\nRevenue rose 12%."
        assert "<|ref|>" not in res_ds.markdown
        assert "<|det|>" not in res_ds.markdown


def test_cli_vision_prompt_preset_override(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI --vision-prompt-preset 옵션이 커스텀 모델명에도 정상 적용됨을 검증한다."""
    px_w, px_h = 16, 16
    rgb_raw = bytes([200, 210, 220] * (px_w * px_h))
    ops = "BT /F1 12 Tf 1 0 0 1 60 760 Tm (Chart page) Tj ET\nq 220 0 0 140 60 540 cm /Img Do Q"
    pdf_path = tmp_path / "preset_cli.pdf"
    pdf_path.write_bytes(_build_minimal_pdf(ops, images=[("Img", px_w, px_h, rgb_raw)]))

    def handler(method: str, path: str, payload: Any, headers: Any) -> tuple[int, dict[str, Any]]:
        return 200, {
            "model": "my-custom-ocr",
            "message": {"content": "| A | B |\n| --- | --- |\n| 1 | 2 |"},
        }

    with _mock_vision_http_server(handler) as (base, captured):
        out_dir = tmp_path / "out_preset"
        code = cli_main(
            [
                "parse",
                str(pdf_path),
                "--vision",
                "ollama",
                "--vision-model",
                "my-custom-ocr",
                "--vision-prompt-preset",
                "paddleocr-vl",
                "--vision-base-url",
                base,
                "--out",
                str(out_dir),
            ]
        )
        assert code == 0
        capsys.readouterr()
        msgs = captured[0]["payload"]["messages"]
        assert len(msgs) == 1
        assert msgs[0]["content"] == "Chart Recognition:"
        parsed = ParsedDocument.model_validate_json(
            (out_dir / "parsed.json").read_text(encoding="utf-8")
        )
        assert parsed.visual_regions[0].vision_metadata["prompt_preset"] == "paddleocr-vl"
