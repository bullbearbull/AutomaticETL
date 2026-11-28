"""시각(OCR/VLM) 어댑터의 오류·경계·순수 함수 단위 테스트.

외부 바이너리(tesseract)·실제 네트워크·모델 없이 가짜 subprocess와
가짜 HTTP 전송 함수로 다음을 검증한다:
- 이미지 MIME 판별·크롭 해석·TSV 복원·응답 본문 추출 등 순수 함수
- Tesseract 어댑터의 언어 해석·설치 언어 캐시·실행 오류
- OpenAI 호환/Ollama VLM 어댑터의 오류 매핑·빈 응답·메타데이터
- 하이브리드 라우터의 분기(폴백·예외·미설정)와 팩토리
"""

from __future__ import annotations

import shutil
import subprocess
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from typing import Any

import pytest

from autoetl.parser import (
    BBox,
    ElementKind,
    NoopVisionAdapter,
    OllamaVisionAdapter,
    OpenAICompatibleVlmAdapter,
    ParserInputError,
    ParseStrategy,
    RoutedVisionAdapter,
    TesseractOcrAdapter,
    VisionPromptPreset,
    VisionRequest,
    VisionResult,
    VisionSecurityError,
    build_vision_prompts,
    create_vision_adapter,
    resolve_prompt_preset,
    validate_vision_endpoint,
)
from autoetl.parser import vision as vision_mod
from autoetl.parser.image_io import encode_png
from autoetl.parser.vision import (
    _clean_vlm_output,
    _detect_image_mime,
    _extract_chat_content,
    _open_json_request,
    _parse_tesseract_tsv,
    _resolve_request_image,
)
from autoetl.security.levels import TrustZone

_PNG = encode_png(bytes([200, 210, 220] * 4), 2, 2, channels=3)


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


def _request(
    *,
    kind: ElementKind = ElementKind.FIGURE_IMAGE,
    strategy: ParseStrategy = ParseStrategy.VLM_REGION,
    image_bytes: bytes | None = _PNG,
    crop_path: str | None = None,
    hints: tuple[str, ...] = (),
    caption: str | None = None,
    context_before: str | None = None,
    context_after: str | None = None,
) -> VisionRequest:
    return VisionRequest(
        region_id="r1",
        page=1,
        element_kind=kind,
        recommended_strategy=strategy,
        bbox=_bbox(),
        reason="합성 요청",
        image_bytes=image_bytes,
        image_extension=".png" if image_bytes is not None else None,
        crop_path=crop_path,
        embedded_text_hints=hints,
        caption=caption,
        context_before=context_before,
        context_after=context_after,
    )


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://x", code, "err", Message(), None)


# ---------------------------------------------------------------------------
# 1. 순수 함수: MIME 판별 / 크롭 해석 / TSV 복원 / 응답 추출 / 태그 정리
# ---------------------------------------------------------------------------


def test_detect_image_mime_magic_and_extension() -> None:
    """매직 바이트 우선, 없으면 확장자, 둘 다 없으면 PNG 기본값."""
    assert _detect_image_mime(_PNG) == "image/png"
    assert _detect_image_mime(b"\xff\xd8\xff\xe0abcd") == "image/jpeg"
    assert _detect_image_mime(b"RIFF\x24\x00\x00\x00WEBPdata") == "image/webp"
    assert _detect_image_mime(b"GIF87aabcdef") == "image/gif"
    assert _detect_image_mime(b"GIF89aabcdef") == "image/gif"
    assert _detect_image_mime(b"plain", ".jpg") == "image/jpeg"
    assert _detect_image_mime(b"plain", "JPEG") == "image/jpeg"
    assert _detect_image_mime(b"plain", ".webp") == "image/webp"
    assert _detect_image_mime(b"plain", ".gif") == "image/gif"
    assert _detect_image_mime(b"plain", ".png") == "image/png"
    assert _detect_image_mime(b"plain", None) == "image/png"
    assert _detect_image_mime(b"RIFF\x00\x00") == "image/png"  # 12바이트 미만 RIFF


def test_resolve_request_image_memory_crop_and_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """메모리 우선, 크롭 파일 폴백, 없음·빈 바이트·읽기 오류는 None."""
    got = _resolve_request_image(_request())
    assert got == (_PNG, "image/png")

    crop = tmp_path / "c.png"
    crop.write_bytes(_PNG)
    got2 = _resolve_request_image(_request(image_bytes=None, crop_path=str(crop)))
    assert got2 == (_PNG, "image/png")

    assert (
        _resolve_request_image(_request(image_bytes=None, crop_path=str(tmp_path / "x.png")))
        is None
    )
    assert _resolve_request_image(_request(image_bytes=None, crop_path=None)) is None
    assert _resolve_request_image(_request(image_bytes=b"")) is None

    def _raise(self: Path) -> bytes:
        raise OSError("가짜 읽기 실패")

    monkeypatch.setattr(Path, "read_bytes", _raise)
    assert _resolve_request_image(_request(image_bytes=None, crop_path=str(crop))) is None


def _tsv(rows: list[tuple[str, str, str, str, str]]) -> str:
    header = (
        "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\t"
        "left\ttop\twidth\theight\tconf\ttext"
    )
    lines = [header]
    for i, (blk, par, ln, conf, text) in enumerate(rows):
        lines.append(f"5\t1\t{blk}\t{par}\t{ln}\t{i}\t0\t0\t10\t10\t{conf}\t{text}")
    return "\n".join(lines)


def test_parse_tesseract_tsv_paragraphs_and_confidence() -> None:
    """문단은 빈 줄로, 줄은 공백으로 결합하고 평균 신뢰도·단어 수를 반환한다."""
    tsv = _tsv(
        [
            ("1", "1", "1", "90", "Hello"),
            ("1", "1", "1", "80", "world"),
            ("1", "2", "1", "70", "Second"),
            ("1", "2", "1", "60", "para"),
        ]
    )
    text, avg, count = _parse_tesseract_tsv(tsv, min_word_confidence=30.0)
    assert text == "Hello world\n\nSecond para"
    assert avg == 75.0
    assert count == 4


def test_parse_tesseract_tsv_multiline_paragraph() -> None:
    """같은 문단 내 여러 줄은 줄바꿈으로 결합한다."""
    tsv = _tsv([("1", "1", "1", "90", "Line1"), ("1", "1", "2", "90", "Line2")])
    text, _, _ = _parse_tesseract_tsv(tsv, min_word_confidence=30.0)
    assert text == "Line1\nLine2"


def test_parse_tesseract_tsv_filters_low_conf_and_bad_rows() -> None:
    """저신뢰·음수·빈 텍스트·잘못된 숫자 행은 걸러진다."""
    tsv = _tsv(
        [
            ("1", "1", "1", "90", "Keep"),
            ("1", "1", "1", "10", "drop"),
            ("1", "1", "1", "-1", "neg"),
            ("1", "1", "1", "95", ""),
            ("1", "1", "1", "abc", "badconf"),
        ]
    )
    text, avg, count = _parse_tesseract_tsv(tsv, min_word_confidence=30.0)
    assert text == "Keep"
    assert avg == 90.0
    assert count == 1


def test_parse_tesseract_tsv_empty() -> None:
    """빈 입력·헤더만 있는 입력은 빈 결과를 반환한다."""
    assert _parse_tesseract_tsv("", min_word_confidence=30.0) == ("", None, 0)
    assert _parse_tesseract_tsv(_tsv([]), min_word_confidence=30.0) == ("", None, 0)


def test_extract_chat_content_variants() -> None:
    """문자열·리스트 본문 추출과 잘못된 응답 형태는 None."""
    assert _extract_chat_content({}) is None
    assert _extract_chat_content({"choices": []}) is None
    assert _extract_chat_content({"choices": "x"}) is None
    assert _extract_chat_content({"choices": ["notdict"]}) is None
    assert _extract_chat_content({"choices": [{"message": "notdict"}]}) is None
    assert _extract_chat_content({"choices": [{"message": {"content": "  hi  "}}]}) == "hi"
    assert _extract_chat_content({"choices": [{"message": {"content": 123}}]}) is None
    assert (
        _extract_chat_content(
            {"choices": [{"message": {"content": [{"text": "a"}, {"text": " b "}, {"x": 1}]}}]}
        )
        == "a\nb"
    )
    assert _extract_chat_content({"choices": [{"message": {"content": [{"x": 1}]}}]}) is None
    assert _extract_chat_content({"choices": [{"message": {"content": []}}]}) is None


def test_clean_vlm_output_deepseek_and_generic() -> None:
    """DeepSeek grounding/stray 태그 제거·줄바꿈 정리, 그 외 프리셋은 strip만."""
    assert _clean_vlm_output("  hello  ", VisionPromptPreset.GENERIC) == "hello"
    raw = "<|ref|>t<|/ref|><|det|>[[1,2,3,4]]<|/det|>\n# Title\n\n\n\nBody"
    assert _clean_vlm_output(raw, VisionPromptPreset.DEEPSEEK_OCR) == "# Title\n\nBody"
    assert _clean_vlm_output("a<|ref|>b<|/det|>c", VisionPromptPreset.DEEPSEEK_OCR) == "abc"


# ---------------------------------------------------------------------------
# 2. Tesseract 어댑터 (가짜 subprocess)
# ---------------------------------------------------------------------------


class _FakeTesseract:
    def __init__(
        self,
        *,
        langs: list[str],
        tsv: str,
        returncode: int = 0,
        raises: Exception | None = None,
        lang_raises: Exception | None = None,
    ) -> None:
        self.langs = langs
        self.tsv = tsv
        self.returncode = returncode
        self.raises = raises
        self.lang_raises = lang_raises
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        self.calls.append(cmd)
        if "--list-langs" in cmd:
            if self.lang_raises is not None:
                raise self.lang_raises
            stdout = "List of available languages:\n" + "\n".join(self.langs) + "\n"
            return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")
        if self.raises is not None:
            raise self.raises
        return subprocess.CompletedProcess(
            cmd, self.returncode, stdout=self.tsv.encode("utf-8"), stderr=b""
        )


def _fake_binary(tmp_path: Path) -> str:
    binary = tmp_path / "tesseract"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    return str(binary)


def test_tesseract_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """바이너리가 없으면 available=False, tesseract_not_installed 오류."""

    def _which(name: str) -> str | None:
        return None

    monkeypatch.setattr(shutil, "which", _which)
    adapter = TesseractOcrAdapter()
    assert adapter.name == "tesseract"
    assert adapter.available is False
    res = adapter.parse_region(_request())
    assert res.succeeded is False
    assert res.error == "tesseract_not_installed"
    assert res.metadata["ocr_lang_requested"] == "kor+eng"


def test_tesseract_missing_image(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """이미지가 없으면 missing_image_bytes 오류."""
    fake = _FakeTesseract(langs=["eng"], tsv="")
    monkeypatch.setattr(subprocess, "run", fake)
    adapter = TesseractOcrAdapter(binary_path=_fake_binary(tmp_path))
    res = adapter.parse_region(_request(image_bytes=None, crop_path=None))
    assert res.error == "missing_image_bytes"


def test_tesseract_success_lang_fallback_and_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """설치 언어 폴백·PSM·신뢰도·MIME 메타데이터와 stdin/stdout 명령을 검증한다."""
    tsv = _tsv([("1", "1", "1", "90", "Hello"), ("1", "1", "1", "80", "world")])
    fake = _FakeTesseract(langs=["eng", "osd"], tsv=tsv)
    monkeypatch.setattr(subprocess, "run", fake)
    adapter = TesseractOcrAdapter(binary_path=_fake_binary(tmp_path), lang="kor+eng")
    res = adapter.parse_region(_request())
    assert res.succeeded is True
    assert res.text == "Hello world"
    assert res.metadata["ocr_lang_used"] == "eng"
    assert res.metadata["ocr_lang_fallback"] == "true"
    assert res.metadata["psm"] == "3"
    assert res.metadata["word_count"] == "2"
    assert res.metadata["avg_confidence"] == "85.00"
    assert res.metadata["mime_type"] == "image/png"
    ocr_cmd = fake.calls[-1]
    assert ocr_cmd[1:3] == ["stdin", "stdout"]
    assert ocr_cmd[ocr_cmd.index("-l") + 1] == "eng"


def test_tesseract_no_fallback_when_all_langs_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """요청 언어가 모두 설치돼 있으면 폴백 없이 그대로 사용한다."""
    fake = _FakeTesseract(langs=["kor", "eng"], tsv=_tsv([("1", "1", "1", "90", "가")]))
    monkeypatch.setattr(subprocess, "run", fake)
    adapter = TesseractOcrAdapter(binary_path=_fake_binary(tmp_path), lang="kor+eng")
    res = adapter.parse_region(_request())
    assert res.metadata["ocr_lang_used"] == "kor+eng"
    assert res.metadata["ocr_lang_fallback"] == "false"


@pytest.mark.parametrize(
    ("lang", "strict", "langs"),
    [
        ("kor+eng", True, ["eng"]),  # 일부만 설치 + strict
        ("kor", True, ["eng"]),  # 전혀 없음 + strict
        ("kor", False, ["osd"]),  # eng도 없고 osd만
    ],
)
def test_tesseract_language_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lang: str,
    strict: bool,
    langs: list[str],
) -> None:
    """사용 가능한 언어가 없으면 tesseract_language_unavailable 오류."""
    fake = _FakeTesseract(langs=langs, tsv="")
    monkeypatch.setattr(subprocess, "run", fake)
    adapter = TesseractOcrAdapter(binary_path=_fake_binary(tmp_path), lang=lang, strict_lang=strict)
    res = adapter.parse_region(_request())
    assert res.error == "tesseract_language_unavailable"


def test_tesseract_non_eng_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """eng가 없으면 osd를 제외한 첫 설치 언어로 폴백한다."""
    fake = _FakeTesseract(langs=["deu", "osd"], tsv=_tsv([("1", "1", "1", "90", "x")]))
    monkeypatch.setattr(subprocess, "run", fake)
    adapter = TesseractOcrAdapter(binary_path=_fake_binary(tmp_path), lang="kor")
    res = adapter.parse_region(_request())
    assert res.metadata["ocr_lang_used"] == "deu"
    assert res.metadata["ocr_lang_fallback"] == "true"


def test_tesseract_fallback_to_eng_when_requested_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """요청 언어가 미설치이고 eng가 있으면 eng로 폴백한다."""
    fake = _FakeTesseract(langs=["eng", "osd"], tsv=_tsv([("1", "1", "1", "90", "x")]))
    monkeypatch.setattr(subprocess, "run", fake)
    adapter = TesseractOcrAdapter(binary_path=_fake_binary(tmp_path), lang="kor")
    res = adapter.parse_region(_request())
    assert res.metadata["ocr_lang_used"] == "eng"
    assert res.metadata["ocr_lang_fallback"] == "true"


def test_tesseract_list_langs_failure_uses_requested_lang(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """설치 언어 조회가 실패하면 요청 언어를 폴백 없이 그대로 사용한다."""
    tsv = _tsv([("1", "1", "1", "90", "Hello")])
    fake = _FakeTesseract(langs=[], tsv=tsv, lang_raises=OSError("가짜 조회 실패"))
    monkeypatch.setattr(subprocess, "run", fake)
    adapter = TesseractOcrAdapter(binary_path=_fake_binary(tmp_path), lang="kor+eng")
    res = adapter.parse_region(_request())
    assert res.succeeded is True
    assert res.metadata["ocr_lang_used"] == "kor+eng"
    assert res.metadata["ocr_lang_fallback"] == "false"


def test_tesseract_installed_languages_cache_and_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """설치 언어 조회는 캐시되고, 미설치·OSError 시 빈 튜플을 반환한다."""
    fake = _FakeTesseract(langs=["eng"], tsv="")
    monkeypatch.setattr(subprocess, "run", fake)
    adapter = TesseractOcrAdapter(binary_path=_fake_binary(tmp_path))
    assert adapter.installed_languages() == ("eng",)
    n_calls = len(fake.calls)
    assert adapter.installed_languages() == ("eng",)
    assert len(fake.calls) == n_calls  # 캐시 -> 재호출 없음

    def _which(name: str) -> str | None:
        return None

    monkeypatch.setattr(shutil, "which", _which)
    assert TesseractOcrAdapter().installed_languages() == ()

    fake_err = _FakeTesseract(langs=[], tsv="", lang_raises=OSError("가짜 오류"))
    monkeypatch.setattr(subprocess, "run", fake_err)
    adapter2 = TesseractOcrAdapter(binary_path=_fake_binary(tmp_path))
    assert adapter2.installed_languages() == ()


@pytest.mark.parametrize(
    ("raises", "returncode", "expected"),
    [
        (subprocess.TimeoutExpired(cmd=["tesseract"], timeout=30), 0, "tesseract_timeout"),
        (OSError("가짜 실행 오류"), 0, "tesseract_exec_error"),
        (None, 1, "tesseract_exit_1"),
    ],
)
def test_tesseract_exec_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raises: Exception | None,
    returncode: int,
    expected: str,
) -> None:
    """타임아웃·실행 오류·비정상 종료코드별 오류 코드를 검증한다."""
    fake = _FakeTesseract(langs=["eng"], tsv="", returncode=returncode, raises=raises)
    monkeypatch.setattr(subprocess, "run", fake)
    adapter = TesseractOcrAdapter(binary_path=_fake_binary(tmp_path), lang="eng")
    res = adapter.parse_region(_request())
    assert res.succeeded is False
    assert res.error == expected


def test_tesseract_empty_ocr_and_explicit_psm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """빈 OCR은 자리표시 마크다운, 명시적 PSM은 명령에 반영된다."""
    fake = _FakeTesseract(langs=["eng"], tsv=_tsv([]))
    monkeypatch.setattr(subprocess, "run", fake)
    adapter = TesseractOcrAdapter(binary_path=_fake_binary(tmp_path), lang="eng", psm=6)
    res = adapter.parse_region(_request())
    assert res.succeeded is True
    assert res.text == ""
    assert res.metadata["empty_ocr"] == "true"
    assert res.markdown == "*(OCR 추출 텍스트 없음)*"
    assert fake.calls[-1][fake.calls[-1].index("--psm") + 1] == "6"


def test_tesseract_lang_normalization(monkeypatch: pytest.MonkeyPatch) -> None:
    """공백뿐인 언어 요청은 eng로 정규화한다."""

    def _which(name: str) -> str | None:
        return None

    monkeypatch.setattr(shutil, "which", _which)
    adapter = TesseractOcrAdapter(lang="   ")
    assert adapter.parse_region(_request()).metadata["ocr_lang_requested"] == "eng"


# ---------------------------------------------------------------------------
# 3. OpenAI 호환 VLM 어댑터 (가짜 HTTP 전송)
# ---------------------------------------------------------------------------


def _make_openai(**kwargs: Any) -> OpenAICompatibleVlmAdapter:
    return OpenAICompatibleVlmAdapter(model="test-vlm", base_url="http://127.0.0.1:9/v1", **kwargs)


def _raising_transport(exc: Exception) -> Any:
    def _fn(
        url: str,
        *,
        zone: TrustZone,
        payload: Any = None,
        headers: Any = None,
        timeout_sec: float = 90.0,
    ) -> dict[str, Any]:
        raise exc

    return _fn


def test_openai_empty_model_and_external_block() -> None:
    """빈 모델명·외부 엔드포인트(기본값)는 생성 시 거부한다."""
    with pytest.raises(ParserInputError):
        OpenAICompatibleVlmAdapter(model="   ")
    with pytest.raises(VisionSecurityError):
        OpenAICompatibleVlmAdapter(model="m", base_url="https://api.example.com/v1")


def test_openai_api_key_and_trust_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    """환경변수 API 키 공백 제거·Bearer 헤더, 키 없으면 빈 헤더."""
    captured: dict[str, Any] = {}
    resp = {"choices": [{"message": {"content": "가짜 인식 텍스트"}}]}

    def _fake(
        url: str,
        *,
        zone: TrustZone,
        payload: Any = None,
        headers: Any = None,
        timeout_sec: float = 90.0,
    ) -> dict[str, Any]:
        captured["headers"] = headers
        return resp

    monkeypatch.setattr(vision_mod, "_open_json_request", _fake)
    monkeypatch.setenv("FAKE_VLM_KEY", "  fake-key-123  ")
    adapter = OpenAICompatibleVlmAdapter(model="m", api_key_env="FAKE_VLM_KEY")
    assert adapter.trust_zone is TrustZone.LOOPBACK
    assert adapter.parse_region(_request()).succeeded is True
    assert captured["headers"] == {"Authorization": "Bearer fake-key-123"}

    no_key = OpenAICompatibleVlmAdapter(model="m")
    assert no_key.parse_region(_request()).succeeded is True
    assert "Authorization" not in captured["headers"]


def test_openai_missing_image() -> None:
    """이미지가 없으면 missing_image_bytes 오류."""
    res = _make_openai().parse_region(_request(image_bytes=None, crop_path=None))
    assert res.error == "missing_image_bytes"


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (_http_error(500), "http_500"),
        (TimeoutError(), "timeout"),
        (urllib.error.URLError("refused"), "connection_error"),
        (ValueError("bad"), "invalid_response"),
    ],
)
def test_openai_error_mapping(
    monkeypatch: pytest.MonkeyPatch, exc: Exception, expected: str
) -> None:
    """전송 예외별 오류 코드와 메타데이터(신뢰구역·모델·프리셋)를 검증한다."""
    adapter = _make_openai()
    monkeypatch.setattr(vision_mod, "_open_json_request", _raising_transport(exc))
    res = adapter.parse_region(_request())
    assert res.succeeded is False
    assert res.error == expected
    assert res.metadata["trust_zone"] == "loopback"
    assert res.metadata["model"] == "test-vlm"
    assert res.metadata["prompt_preset"] == "generic"


def test_openai_empty_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """공백뿐인 본문은 empty_response 오류."""
    adapter = _make_openai()
    monkeypatch.setattr(
        vision_mod,
        "_open_json_request",
        lambda *a, **k: {"choices": [{"message": {"content": "   "}}]},
    )
    res = adapter.parse_region(_request())
    assert res.error == "empty_response"


def test_openai_success_usage_and_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    """성공 응답의 모델·usage 메타데이터와 system+image_url 페이로드를 검증한다."""
    adapter = _make_openai()
    captured: dict[str, Any] = {}
    resp = {
        "model": "server-model",
        "choices": [{"message": {"content": "# Result"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "extra": "x"},
    }

    def _fake(
        url: str,
        *,
        zone: TrustZone,
        payload: Any = None,
        headers: Any = None,
        timeout_sec: float = 90.0,
    ) -> dict[str, Any]:
        captured["url"] = url
        captured["payload"] = payload
        return resp

    monkeypatch.setattr(vision_mod, "_open_json_request", _fake)
    res = adapter.parse_region(_request())
    assert res.succeeded is True
    assert res.text == "# Result"
    assert res.metadata["model"] == "server-model"
    assert res.metadata["total_tokens"] == "15"
    assert "extra" not in res.metadata
    assert captured["url"] == "http://127.0.0.1:9/v1/chat/completions"
    msgs = captured["payload"]["messages"]
    assert msgs[0]["role"] == "system"
    assert any(p.get("type") == "image_url" for p in msgs[1]["content"])


def test_openai_check_health_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    """check_health의 성공·HTTP·연결·잘못된 응답 분기를 검증한다."""
    adapter = _make_openai()
    monkeypatch.setattr(vision_mod, "_open_json_request", lambda *a, **k: {"data": []})
    assert adapter.check_health() == (True, "ok")
    monkeypatch.setattr(
        vision_mod,
        "_open_json_request",
        _raising_transport(_http_error(404)),
    )
    assert adapter.check_health() == (False, "http_404")
    monkeypatch.setattr(
        vision_mod, "_open_json_request", _raising_transport(urllib.error.URLError("x"))
    )
    assert adapter.check_health() == (False, "connection_error")
    monkeypatch.setattr(vision_mod, "_open_json_request", _raising_transport(ValueError("x")))
    assert adapter.check_health() == (False, "invalid_response")


# ---------------------------------------------------------------------------
# 4. Ollama 네이티브 VLM 어댑터 (가짜 HTTP 전송)
# ---------------------------------------------------------------------------


def test_ollama_empty_model_and_external_block() -> None:
    """빈 모델명·외부 엔드포인트(기본값)는 생성 시 거부한다."""
    with pytest.raises(ParserInputError):
        OllamaVisionAdapter(model="   ")
    with pytest.raises(VisionSecurityError):
        OllamaVisionAdapter(model="m", base_url="https://api.example.com")


def test_ollama_missing_image_and_trust_zone() -> None:
    """이미지 없음 오류와 루프백 신뢰구역을 검증한다."""
    adapter = OllamaVisionAdapter(model="llava", base_url="http://127.0.0.1:9")
    assert adapter.trust_zone is TrustZone.LOOPBACK
    res = adapter.parse_region(_request(image_bytes=None, crop_path=None))
    assert res.error == "missing_image_bytes"


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (_http_error(503), "http_503"),
        (TimeoutError(), "timeout"),
        (urllib.error.URLError("refused"), "connection_error"),
        (ValueError("bad"), "invalid_response"),
    ],
)
def test_ollama_error_mapping(
    monkeypatch: pytest.MonkeyPatch, exc: Exception, expected: str
) -> None:
    """전송 예외별 오류 코드와 메타데이터를 검증한다."""
    adapter = OllamaVisionAdapter(model="llava", base_url="http://127.0.0.1:9")
    monkeypatch.setattr(vision_mod, "_open_json_request", _raising_transport(exc))
    res = adapter.parse_region(_request())
    assert res.succeeded is False
    assert res.error == expected
    assert res.metadata["trust_zone"] == "loopback"


def test_ollama_empty_response_non_dict_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """message가 dict가 아니거나 본문이 비면 empty_response 오류."""
    adapter = OllamaVisionAdapter(model="llava", base_url="http://127.0.0.1:9")
    monkeypatch.setattr(vision_mod, "_open_json_request", lambda *a, **k: {"message": "notdict"})
    assert adapter.parse_region(_request()).error == "empty_response"
    monkeypatch.setattr(
        vision_mod, "_open_json_request", lambda *a, **k: {"message": {"content": "  "}}
    )
    assert adapter.parse_region(_request()).error == "empty_response"


def test_ollama_success_payload_and_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    """성공 응답의 토큰 메타데이터와 images[base64]·stream=False 페이로드를 검증한다."""
    adapter = OllamaVisionAdapter(model="llava", base_url="http://127.0.0.1:9")
    captured: dict[str, Any] = {}
    resp = {
        "model": "llava-server",
        "message": {"content": "Diagram summary"},
        "eval_count": 7,
        "prompt_eval_count": 20,
    }

    def _fake(
        url: str,
        *,
        zone: TrustZone,
        payload: Any = None,
        headers: Any = None,
        timeout_sec: float = 90.0,
    ) -> dict[str, Any]:
        captured["url"] = url
        captured["payload"] = payload
        return resp

    monkeypatch.setattr(vision_mod, "_open_json_request", _fake)
    res = adapter.parse_region(_request())
    assert res.succeeded is True
    assert res.text == "Diagram summary"
    assert res.metadata["model"] == "llava-server"
    assert res.metadata["completion_tokens"] == "7"
    assert res.metadata["prompt_tokens"] == "20"
    assert captured["url"] == "http://127.0.0.1:9/api/chat"
    assert captured["payload"]["stream"] is False
    assert "images" in captured["payload"]["messages"][-1]


# ---------------------------------------------------------------------------
# 5. 하이브리드 라우터 분기
# ---------------------------------------------------------------------------


class _StubAdapter:
    def __init__(
        self,
        name: str,
        result: VisionResult | None = None,
        *,
        raises: bool = False,
        preset: VisionPromptPreset | None = None,
    ) -> None:
        self._name = name
        self._result = result
        self._raises = raises
        self.requests: list[VisionRequest] = []
        if preset is not None:
            self.prompt_preset = preset

    @property
    def name(self) -> str:
        return self._name

    def parse_region(self, request: VisionRequest) -> VisionResult:
        self.requests.append(request)
        if self._raises:
            raise RuntimeError("가짜 어댑터 장애")
        assert self._result is not None
        return self._result


def _ok(name: str, text: str = "text") -> VisionResult:
    return VisionResult(adapter_name=name, succeeded=True, text=text, markdown=text)


def _fail(name: str, error: str) -> VisionResult:
    return VisionResult(adapter_name=name, succeeded=False, error=error)


def test_routed_name() -> None:
    """라우터 이름은 VLM+OCR 조합을 반영한다."""
    ocr = _StubAdapter("ocr", _ok("ocr"))
    vlm = _StubAdapter("vlm", _ok("vlm"))
    assert RoutedVisionAdapter(ocr_adapter=ocr, vlm_adapter=vlm).name == "routed(vlm+ocr)"
    assert RoutedVisionAdapter(ocr_adapter=ocr).name == "routed(ocr)"
    assert RoutedVisionAdapter().name == "routed(none)"


def test_routed_no_adapters() -> None:
    """어댑터 미설정 시 두 전략 모두 no_vision_adapter_configured 오류."""
    router = RoutedVisionAdapter()
    for strategy in (ParseStrategy.EMBEDDED_IMAGE_OCR, ParseStrategy.VLM_REGION):
        res = router.parse_region(_request(strategy=strategy))
        assert res.error == "no_vision_adapter_configured"


def test_routed_ocr_primary_fallback_to_vlm() -> None:
    """OCR 우선 전략에서 OCR 실패 시 VLM으로 폴백한다."""
    ocr = _StubAdapter("ocr", _fail("ocr", "ocr_fail"))
    vlm = _StubAdapter("vlm", _ok("vlm", "vlm text"))
    router = RoutedVisionAdapter(ocr_adapter=ocr, vlm_adapter=vlm)
    res = router.parse_region(_request(strategy=ParseStrategy.EMBEDDED_IMAGE_OCR))
    assert res.succeeded is True
    assert res.adapter_name == "vlm"
    assert res.metadata["routed_via"] == "vlm_fallback"


def test_routed_ocr_fail_no_vlm_returns_ocr() -> None:
    """OCR 우선·VLM 없음·OCR 실패면 OCR 실패 결과를 그대로 반환한다."""
    ocr = _StubAdapter("ocr", _fail("ocr", "ocr_fail"))
    router = RoutedVisionAdapter(ocr_adapter=ocr)
    res = router.parse_region(_request(strategy=ParseStrategy.EMBEDDED_IMAGE_OCR))
    assert res.succeeded is False
    assert res.error == "ocr_fail"


def test_routed_ocr_exception() -> None:
    """OCR 예외는 ocr_exception으로 기록되고 VLM 폴백·단독 반환을 검증한다."""
    ocr = _StubAdapter("ocr", raises=True)
    vlm = _StubAdapter("vlm", _ok("vlm"))
    router = RoutedVisionAdapter(ocr_adapter=ocr, vlm_adapter=vlm)
    res = router.parse_region(_request(strategy=ParseStrategy.EMBEDDED_IMAGE_OCR))
    assert res.adapter_name == "vlm"
    assert res.metadata["routed_via"] == "vlm_fallback"

    router2 = RoutedVisionAdapter(ocr_adapter=ocr)
    res2 = router2.parse_region(_request(strategy=ParseStrategy.EMBEDDED_IMAGE_OCR))
    assert res2.error == "ocr_exception"


def test_routed_vlm_exception_fallback_to_ocr() -> None:
    """VLM 우선 경로에서 VLM 예외 시 OCR로 폴백한다."""
    ocr = _StubAdapter("ocr", _ok("ocr", "ocr text"))
    vlm = _StubAdapter("vlm", raises=True)
    router = RoutedVisionAdapter(ocr_adapter=ocr, vlm_adapter=vlm)
    res = router.parse_region(_request(strategy=ParseStrategy.VLM_REGION))
    assert res.succeeded is True
    assert res.adapter_name == "ocr"
    assert res.metadata["routed_via"] == "ocr_fallback"


def test_routed_no_fallback_returns_vlm_failure() -> None:
    """폴백 비활성 시 VLM 실패 결과를 그대로 반환한다."""
    vlm = _StubAdapter("vlm", _fail("vlm", "vlm_fail"))
    router = RoutedVisionAdapter(vlm_adapter=vlm, fallback_to_ocr=False)
    res = router.parse_region(_request(strategy=ParseStrategy.VLM_REGION))
    assert res.succeeded is False
    assert res.error == "vlm_fail"


def test_routed_vlm_none_ocr_fallback() -> None:
    """VLM 미설정·OCR 성공이면 ocr_fallback(fallback_from=none)으로 반환한다."""
    ocr = _StubAdapter("ocr", _ok("ocr"))
    router = RoutedVisionAdapter(ocr_adapter=ocr)
    res = router.parse_region(_request(strategy=ParseStrategy.VLM_REGION))
    assert res.adapter_name == "ocr"
    assert res.metadata["routed_via"] == "ocr_fallback"
    assert res.metadata["fallback_from"] == "none"


def test_routed_vlm_none_ocr_fail_returns_ocr() -> None:
    """VLM 미설정·OCR 실패면 폴백 후 OCR 실패 결과를 반환한다."""
    ocr = _StubAdapter("ocr", _fail("ocr", "ocr_fail"))
    router = RoutedVisionAdapter(ocr_adapter=ocr)
    res = router.parse_region(_request(strategy=ParseStrategy.VLM_REGION))
    assert res.error == "ocr_fail"


def test_routed_prefer_vlm_for_scans() -> None:
    """prefer_vlm_for_scans=True면 스캔 전략도 VLM 우선으로 라우팅한다."""
    ocr = _StubAdapter("ocr", _ok("ocr"))
    vlm = _StubAdapter("vlm", _ok("vlm"))
    router = RoutedVisionAdapter(ocr_adapter=ocr, vlm_adapter=vlm, prefer_vlm_for_scans=True)
    res = router.parse_region(_request(strategy=ParseStrategy.EMBEDDED_IMAGE_OCR))
    assert res.adapter_name == "vlm"
    assert res.metadata["routed_via"] == "vlm_primary"


def test_routed_no_hint_injection() -> None:
    """힌트 주입 비활성 시 사전 OCR을 생략하고 교차검증 false로 표시한다."""
    ocr = _StubAdapter("ocr", _ok("ocr"))
    vlm = _StubAdapter("vlm", _ok("vlm"))
    router = RoutedVisionAdapter(ocr_adapter=ocr, vlm_adapter=vlm, inject_ocr_hints_to_vlm=False)
    res = router.parse_region(_request(strategy=ParseStrategy.VLM_REGION))
    assert res.metadata["ocr_cross_check"] == "false"
    assert len(ocr.requests) == 0


def test_routed_short_preset_skips_preocr() -> None:
    """짧은 OCR 프리셋 VLM은 사전 OCR을 생략한다."""
    ocr = _StubAdapter("ocr", _ok("ocr"))
    vlm = _StubAdapter("vlm", _ok("vlm"), preset=VisionPromptPreset.GLM_OCR)
    router = RoutedVisionAdapter(ocr_adapter=ocr, vlm_adapter=vlm)
    res = router.parse_region(_request(strategy=ParseStrategy.VLM_REGION))
    assert len(ocr.requests) == 0
    assert res.metadata["ocr_cross_check"] == "false"


def test_routed_empty_ocr_text_no_crosscheck() -> None:
    """OCR 텍스트가 공백뿐이면 교차검증 false로 표시한다."""
    ocr = _StubAdapter("ocr", _ok("ocr", "   "))
    vlm = _StubAdapter("vlm", _ok("vlm"))
    router = RoutedVisionAdapter(ocr_adapter=ocr, vlm_adapter=vlm)
    res = router.parse_region(_request(strategy=ParseStrategy.VLM_REGION))
    assert res.metadata["ocr_cross_check"] == "false"


def test_routed_both_fail_returns_vlm() -> None:
    """VLM·OCR 모두 실패하면 VLM 실패 결과를 반환한다."""
    ocr = _StubAdapter("ocr", _fail("ocr", "o"))
    vlm = _StubAdapter("vlm", _fail("vlm", "v"))
    router = RoutedVisionAdapter(ocr_adapter=ocr, vlm_adapter=vlm)
    res = router.parse_region(_request(strategy=ParseStrategy.VLM_REGION))
    assert res.error == "v"


# ---------------------------------------------------------------------------
# 6. 팩토리 / 엔드포인트 검증 / 프롬프트 프리셋·빌더
# ---------------------------------------------------------------------------


def test_create_vision_adapter_factory() -> None:
    """프로바이더별 어댑터 생성과 미지원·빈 모델 오류를 검증한다."""
    assert isinstance(create_vision_adapter("none"), NoopVisionAdapter)
    assert isinstance(create_vision_adapter(""), NoopVisionAdapter)
    assert isinstance(create_vision_adapter("noop"), NoopVisionAdapter)
    assert isinstance(create_vision_adapter(" Tesseract "), TesseractOcrAdapter)
    assert isinstance(create_vision_adapter("openai-compat", model="m"), OpenAICompatibleVlmAdapter)
    assert isinstance(create_vision_adapter("ollama", model="m"), OllamaVisionAdapter)
    assert isinstance(create_vision_adapter("hybrid"), RoutedVisionAdapter)
    assert isinstance(create_vision_adapter("hybrid", model="m"), RoutedVisionAdapter)
    with pytest.raises(ParserInputError):
        create_vision_adapter("unknown")
    with pytest.raises(ParserInputError):
        create_vision_adapter("openai-compat")


def test_validate_endpoint_missing_host_and_scheme() -> None:
    """호스트 없음·빈 URL·잘못된 스킴은 ParserInputError로 거부한다."""
    for bad in ("http://", "", "ftp://127.0.0.1"):
        with pytest.raises(ParserInputError):
            validate_vision_endpoint(bad)


def test_resolve_prompt_preset_enum_and_normalization() -> None:
    """열거형 통과·문자열 정규화·auto 기본 generic을 검증한다."""
    for preset in VisionPromptPreset:
        if preset is not VisionPromptPreset.AUTO:
            assert resolve_prompt_preset(preset) is preset
    assert resolve_prompt_preset(" GLM-OCR ") is VisionPromptPreset.GLM_OCR
    assert resolve_prompt_preset(VisionPromptPreset.AUTO, model="") is VisionPromptPreset.GENERIC


def test_build_vision_prompts_generic_tasks() -> None:
    """요소·전략별 범용 지시(표·수식·이미지 OCR·차트·전체 페이지)를 검증한다."""
    _, table_ko = build_vision_prompts(
        _request(kind=ElementKind.TABLE, strategy=ParseStrategy.VLM_REGION), language="ko"
    )
    assert "표" in table_ko
    _, formula_ko = build_vision_prompts(
        _request(kind=ElementKind.FORMULA_OR_CODE, strategy=ParseStrategy.VLM_REGION), language="ko"
    )
    assert "LaTeX" in formula_ko
    _, ocr_en = build_vision_prompts(
        _request(kind=ElementKind.FIGURE_IMAGE, strategy=ParseStrategy.EMBEDDED_IMAGE_OCR),
        language="en",
    )
    assert "image region" in ocr_en
    _, chart_ko = build_vision_prompts(
        _request(kind=ElementKind.VECTOR_DIAGRAM, strategy=ParseStrategy.VLM_REGION), language="ko"
    )
    assert "차트" in chart_ko
    _, full_en = build_vision_prompts(
        _request(kind=ElementKind.FIGURE_IMAGE, strategy=ParseStrategy.VLM_FULL_PAGE), language="en"
    )
    assert "scanned page" in full_en.lower()


def test_build_vision_prompts_clipping_and_empty_hints() -> None:
    """힌트 40개·OCR 1200자·문맥 240자 절삭과 빈 힌트 생략을 검증한다."""
    hints = tuple(f"h{i}" for i in range(45))
    _, user = build_vision_prompts(
        _request(hints=hints, context_before="y" * 300, context_after="z" * 300),
        ocr_hint="x" * 1500,
        language="ko",
    )
    assert "h39" in user and "h40" not in user
    assert "x" * 1200 in user and "x" * 1201 not in user
    assert "y" * 240 in user and "y" * 241 not in user
    assert "z" * 240 in user and "z" * 241 not in user

    _, user2 = build_vision_prompts(_request(hints=("  ", "")), language="ko")
    assert "힌트" not in user2


def test_open_json_request_external_and_non_object(monkeypatch: pytest.MonkeyPatch) -> None:
    """비루프백은 urlopen을 쓰고, JSON 객체가 아니면 ValueError를 던진다."""

    class _Resp:
        def __init__(self, data: bytes) -> None:
            self._data = data

        def read(self) -> bytes:
            return self._data

        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

    captured: dict[str, Any] = {}

    def _urlopen(req: Any, timeout: float | None = None) -> _Resp:
        captured["req"] = req
        return _Resp(b'{"ok": true}')

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    out = _open_json_request("http://10.0.0.1:9/v1/models", zone=TrustZone.REGISTERED_INTERNAL)
    assert out == {"ok": True}
    assert captured["req"].method == "GET"

    def _urlopen_post(req: Any, timeout: float | None = None) -> _Resp:
        captured["req"] = req
        return _Resp(b'{"ok": 1}')

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen_post)
    _open_json_request(
        "http://10.0.0.1:9/x",
        zone=TrustZone.REGISTERED_INTERNAL,
        payload={"a": 1},
        headers={"X": "y"},
    )
    assert captured["req"].method == "POST"
    assert captured["req"].data is not None

    def _urlopen_list(req: Any, timeout: float | None = None) -> _Resp:
        return _Resp(b"[1,2,3]")

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen_list)
    with pytest.raises(ValueError):
        _open_json_request("http://10.0.0.1:9/x", zone=TrustZone.REGISTERED_INTERNAL)
