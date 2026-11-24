"""시각 영역(스캔·그림·차트·벡터 도식) OCR/VLM 어댑터 구현체 모음.

외부 의존성을 추가로 설치하지 않고 Python 표준 라이브러리와 시스템 바이너리만으로
다음 네 가지 `VisionParserAdapter` 구현체를 제공한다.

1. `TesseractOcrAdapter`: 로컬 `tesseract` 바이너리(stdin→stdout TSV, 임시 파일 0건).
2. `OpenAICompatibleVlmAdapter`: Ollama(`/v1`)·llama-server·vLLM·LM Studio·Unsloth 등
   OpenAI 호환 Vision Chat Completions API 어댑터.
3. `OllamaVisionAdapter`: Ollama 네이티브 `/api/chat` (`images: [base64]`) 어댑터.
4. `RoutedVisionAdapter`: `ParseStrategy`에 따라 스캔 OCR과 도식/차트 VLM을 자동 분배하고,
   선택적으로 OCR 텍스트 힌트 교차검증과 VLM→OCR 폴백을 수행하는 하이브리드 어댑터.

보안 기본값(Secure Mode / TrustZone):
- 원문 이미지 크롭은 기본적으로 루프백(`127.0.0.1`, `::1`) 또는 명시적으로 등록한
  사내 사설 IP(`registered_internal`)로만 전송한다(`autoetl.security.levels.trust_zone`).
- 외부 엔드포인트(`TrustZone.EXTERNAL`)는 `allow_external=True`를 명시하지 않으면 거부한다.
- 오류 메시지·메타데이터에는 API 키, 이미지 바이트, 문서 원문을 절대 담지 않는다.
"""

from __future__ import annotations

import base64
import csv
import io
import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from enum import Enum
from pathlib import Path
from typing import Any

from autoetl.parser.adapters import (
    NoopVisionAdapter,
    ParserInputError,
    VisionParserAdapter,
    VisionRequest,
    VisionResult,
)
from autoetl.parser.contracts import ElementKind, ParseStrategy
from autoetl.security.levels import TrustZone, trust_zone

DEFAULT_OPENAI_COMPAT_BASE_URL = "http://127.0.0.1:11434/v1"
DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_OCR_LANG = "kor+eng"


class VisionPromptPreset(str, Enum):
    """VLM·OCR 모델 계열별 프롬프트 프리셋.

    - `auto`: 모델명(`glm-ocr`, `paddleocr-vl`, `deepseek-ocr`)을 보고 전용 짧은 트리거
      프리셋을 자동 선택하며, 그 외 범용 VLM에는 `generic`을 적용한다.
    - `generic`: 범용 VLM(Qwen-VL, Gemma, GPT-4o 등)용 상세 문맥·지시 프롬프트.
    - `glm-ocr`: GLM-OCR(0.9B) 공식 짧은 태스크 프롬프트(`Text/Table/Formula Recognition:`).
    - `paddleocr-vl`: PaddleOCR-VL(0.9B) 공식 짧은 태스크 프롬프트(`OCR:/Table/Chart/Formula`).
    - `deepseek-ocr`: DeepSeek-OCR(3B) 공식 단일 턴 프롬프트(`Free OCR.`/`Parse the figure.` 등).
    """

    AUTO = "auto"
    GENERIC = "generic"
    GLM_OCR = "glm-ocr"
    PADDLEOCR_VL = "paddleocr-vl"
    DEEPSEEK_OCR = "deepseek-ocr"


_DEEPSEEK_GROUNDING_TAG_RE = re.compile(
    r"<\|ref\|>.*?<\|/ref\|>\s*<\|det\|>.*?<\|/det\|>\s*",
    re.DOTALL,
)
_DEEPSEEK_STRAY_TAG_RE = re.compile(r"<\|/?(?:ref|det|grounding)\|>")


def resolve_prompt_preset(
    preset: VisionPromptPreset | str = VisionPromptPreset.AUTO,
    *,
    model: str = "",
) -> VisionPromptPreset:
    """문자열/열거형 프리셋과 모델명을 해석해 실제 적용할 프롬프트 프리셋을 결정한다."""
    if isinstance(preset, VisionPromptPreset):
        parsed = preset
    else:
        norm = preset.strip().lower()
        try:
            parsed = VisionPromptPreset(norm)
        except ValueError as exc:
            valid = ", ".join(p.value for p in VisionPromptPreset)
            raise ParserInputError(
                f"지원하지 않는 비전 프롬프트 프리셋이다: '{preset}' (지원: {valid})"
            ) from exc

    if parsed is not VisionPromptPreset.AUTO:
        return parsed

    lower_model = model.strip().lower().replace("_", "-")
    if "glm-ocr" in lower_model:
        return VisionPromptPreset.GLM_OCR
    if "paddleocr-vl" in lower_model:
        return VisionPromptPreset.PADDLEOCR_VL
    if "deepseek-ocr" in lower_model:
        return VisionPromptPreset.DEEPSEEK_OCR
    return VisionPromptPreset.GENERIC


def _clean_vlm_output(content: str, preset: VisionPromptPreset) -> str:
    """OCR 특화 모델 출력의 특수 태그(예: DeepSeek grounding 태그)를 정리한다."""
    cleaned = content.strip()
    if preset is VisionPromptPreset.DEEPSEEK_OCR:
        cleaned = _DEEPSEEK_GROUNDING_TAG_RE.sub("", cleaned)
        cleaned = _DEEPSEEK_STRAY_TAG_RE.sub("", cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


class VisionSecurityError(ParserInputError):
    """신뢰 구역 밖으로 원문 이미지를 전송하려 할 때 발생하는 보안 정책 오류."""


def validate_vision_endpoint(
    base_url: str,
    *,
    allow_external: bool = False,
    registered_internal: Sequence[str] = (),
) -> TrustZone:
    """VLM HTTP 엔드포인트의 URL 형식과 신뢰 구역(TrustZone)을 검증한다.

    `autoetl.security.levels.trust_zone`의 실패 닫힘(fail-closed) 규칙을 그대로 따른다.
    루프백(`127.0.0.1`, `::1`)과 등록된 사내 사설 IP는 기본 허용하며, 그 외 호스트
    (공인 IP, 미등록 사설 IP, DNS 이름 포함)는 `allow_external=True`가 있어야만 허용한다.
    """
    parsed = urllib.parse.urlparse(base_url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ParserInputError(
            "VLM 엔드포인트 URL은 http:// 또는 https://와 호스트를 포함해야 한다."
        )

    zone = trust_zone(parsed.hostname, registered_internal=registered_internal)
    if zone is TrustZone.EXTERNAL and not allow_external:
        raise VisionSecurityError(
            f"신뢰 구역 밖 엔드포인트({parsed.hostname})로의 문서 이미지 전송이 "
            "보안 기본값으로 차단됐다. 로컬 서버는 127.0.0.1 또는 [::1]을 사용하고, "
            "외부 API를 의도적으로 쓰려면 allow_external=True(--allow-external-vision)를 "
            "명시해야 한다."
        )
    return zone


def _detect_image_mime(data: bytes, extension: str | None = None) -> str:
    """이미지 매직 바이트와 확장자로 MIME 타입을 판별한다."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"RIFF") and len(data) >= 12 and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    ext = (extension or "").lower().lstrip(".")
    if ext in ("jpg", "jpeg"):
        return "image/jpeg"
    if ext == "webp":
        return "image/webp"
    if ext == "gif":
        return "image/gif"
    return "image/png"


def _resolve_request_image(request: VisionRequest) -> tuple[bytes, str] | None:
    """VisionRequest에서 이미지 바이트와 MIME 타입을 꺼낸다(메모리 우선, 없으면 크롭 파일)."""
    data = request.image_bytes
    ext = request.image_extension
    if not data and request.crop_path:
        crop_file = Path(request.crop_path)
        if crop_file.is_file():
            try:
                data = crop_file.read_bytes()
                ext = ext or crop_file.suffix
            except OSError:
                return None
    if not data:
        return None
    return data, _detect_image_mime(data, ext)


def build_vision_prompts(
    request: VisionRequest,
    *,
    ocr_hint: str | None = None,
    language: str = "ko",
    preset: VisionPromptPreset | str = VisionPromptPreset.GENERIC,
    model: str = "",
) -> tuple[str | None, str]:
    """요소 유형·파싱 전략·모델 프리셋에 맞는 `(system_prompt, user_prompt)`를 생성한다.

    `glm-ocr`·`paddleocr-vl`·`deepseek-ocr` 같은 경량 문서 특화 OCR-VLM은
    시스템 메시지 없이 학습된 짧은 공식 태스크 트리거 문자열만 보낼 때 정확도가 가장 높다.
    """
    eff_preset = resolve_prompt_preset(preset, model=model)

    if eff_preset is VisionPromptPreset.GLM_OCR:
        if request.element_kind == ElementKind.TABLE:
            return None, "Table Recognition:"
        if request.element_kind == ElementKind.FORMULA_OR_CODE:
            return None, "Formula Recognition:"
        return None, "Text Recognition:"

    if eff_preset is VisionPromptPreset.PADDLEOCR_VL:
        if request.element_kind == ElementKind.TABLE:
            return None, "Table Recognition:"
        if request.element_kind == ElementKind.FORMULA_OR_CODE:
            return None, "Formula Recognition:"
        if request.element_kind in (ElementKind.FIGURE_IMAGE, ElementKind.VECTOR_DIAGRAM):
            return None, "Chart Recognition:"
        return None, "OCR:"

    if eff_preset is VisionPromptPreset.DEEPSEEK_OCR:
        if request.element_kind in (ElementKind.FIGURE_IMAGE, ElementKind.VECTOR_DIAGRAM):
            return None, "Parse the figure."
        if (
            request.element_kind == ElementKind.TABLE
            or request.recommended_strategy == ParseStrategy.VLM_FULL_PAGE
        ):
            return None, "<|grounding|>Convert the document to markdown."
        return None, "Free OCR."

    is_ko = language.lower().startswith("ko")
    if is_ko:
        system = (
            "당신은 문서 구조 복원 전문 VLM 파서다. 이미지에 실제로 보이는 내용만 "
            "정확하게 추출하고, 보이지 않는 숫자나 사실은 절대 추측하지 마라. "
            "출력은 불필요한 인사말 없이 깔끔한 GitHub-Flavored Markdown으로만 작성하라."
        )
    else:
        system = (
            "You are a precision document-structure VLM parser. Extract only what is visibly "
            "present in the image without inventing values. Respond strictly in clean "
            "GitHub-Flavored Markdown without preamble."
        )

    if request.element_kind == ElementKind.SCANNED_PAGE or (
        request.recommended_strategy == ParseStrategy.VLM_FULL_PAGE
    ):
        task = (
            "스캔 페이지 전체를 읽기 순서대로 Markdown으로 전사하라. "
            "제목(#), 목록(-), 표(| ... |) 구조를 유지하고 "
            "숫자·단위·고유명사를 원문 그대로 보존하라."
            if is_ko
            else "Transcribe the full scanned page into reading-order Markdown, preserving "
            "headings, lists, tables, and exact numbers/units."
        )
    elif request.recommended_strategy == ParseStrategy.EMBEDDED_IMAGE_OCR:
        task = (
            "이 이미지 영역의 텍스트를 읽기 순서대로 Markdown으로 전사하라. "
            "제목(#), 목록(-), 숫자·단위·고유명사를 원문 그대로 보존하라."
            if is_ko
            else "Transcribe all visible text in this image region into reading-order Markdown, "
            "preserving headings, lists, and exact numbers/units."
        )
    elif request.element_kind == ElementKind.TABLE:
        task = (
            "이미지의 표를 헤더와 행·열 구조를 유지한 GitHub-Flavored Markdown 표로 변환하라. "
            "단위·주석·빈 셀을 누락하지 마라."
            if is_ko
            else "Convert the table in the image into an exact GitHub-Flavored Markdown table, "
            "preserving headers, units, footnotes, and empty cells."
        )
    elif request.element_kind == ElementKind.FORMULA_OR_CODE:
        task = (
            "이미지의 수식은 LaTeX(`$$ ... $$`)으로, "
            "알고리즘/코드 블록은 코드 펜스로 정확히 변환하라."
            if is_ko
            else "Convert mathematical formulas in this image into exact LaTeX (`$$ ... $$`), "
            "or algorithms/code into fenced code blocks."
        )
    else:
        task = (
            "이 차트/도식/그림을 구조화해 Markdown으로 정리하라.\n"
            "1) 시각 자료 유형과 핵심 요지를 1~2문장으로 요약\n"
            "2) 축·범례·수치가 있는 차트라면 읽을 수 있는 값을 Markdown 표로 정리\n"
            "3) 아키텍처·흐름도라면 구성 요소와 화살표/관계를 목록으로 정리"
            if is_ko
            else "Structure this chart/diagram/figure in Markdown:\n"
            "1) Summarize the visual type and key takeaway in 1-2 sentences\n"
            "2) If quantitative, extract readable axes/legend/values as a Markdown table\n"
            "3) If a diagram/flowchart, list components and directed relationships."
        )

    context_lines: list[str] = [task]
    if request.caption:
        label = "캡션" if is_ko else "Caption"
        context_lines.append(f"\n[{label}]\n{request.caption.strip()}")
    if request.embedded_text_hints:
        hints = ", ".join(h.strip() for h in request.embedded_text_hints[:40] if h.strip())
        if hints:
            label = "PDF 내부 벡터/텍스트 레이블 힌트" if is_ko else "Embedded text hints"
            context_lines.append(f"\n[{label}]\n{hints}")
    if ocr_hint and ocr_hint.strip():
        clipped_ocr = ocr_hint.strip()[:1200]
        label = "보조 OCR 교차검증 텍스트" if is_ko else "Auxiliary OCR text for cross-check"
        context_lines.append(f"\n[{label}]\n{clipped_ocr}")
    if request.context_before and request.context_before.strip():
        label = "앞 문맥" if is_ko else "Preceding context"
        context_lines.append(f"\n[{label}]\n{request.context_before.strip()[:240]}")
    if request.context_after and request.context_after.strip():
        label = "뒤 문맥" if is_ko else "Following context"
        context_lines.append(f"\n[{label}]\n{request.context_after.strip()[:240]}")

    return system, "\n".join(context_lines)


def _open_json_request(
    url: str,
    *,
    zone: TrustZone,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout_sec: float = 90.0,
) -> dict[str, Any]:
    """표준 라이브러리 urllib로 JSON HTTP 요청을 보내고 응답 객체를 반환한다."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req_headers = {"Accept": "application/json"}
    if data is not None:
        req_headers["Content-Type"] = "application/json"
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(
        url, data=data, headers=req_headers, method="POST" if data else "GET"
    )

    if zone is TrustZone.LOOPBACK:
        # 루프백 로컬 서버는 주변 HTTP_PROXY를 거치지 않고 직접 연결한다.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        cm = opener.open(req, timeout=timeout_sec)
    else:
        cm = urllib.request.urlopen(req, timeout=timeout_sec)

    with cm as resp:
        raw = resp.read()
    parsed = json.loads(raw.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("non_object_json_response")
    return parsed


def _parse_tesseract_tsv(
    tsv_text: str,
    *,
    min_word_confidence: float,
) -> tuple[str, float | None, int]:
    """Tesseract TSV 출력을 문단·줄 단위 텍스트와 평균 신뢰도·단어 수로 복원한다."""
    reader = csv.DictReader(io.StringIO(tsv_text), delimiter="\t")
    lines_by_key: dict[tuple[int, int, int], list[str]] = {}
    confs: list[float] = []

    for row in reader:
        text = (row.get("text") or "").strip()
        if not text:
            continue
        try:
            conf = float(row.get("conf") or "-1")
            blk = int(row.get("block_num") or "0")
            par = int(row.get("par_num") or "0")
            ln = int(row.get("line_num") or "0")
        except ValueError:
            continue
        if conf < 0.0 or conf < min_word_confidence:
            continue
        lines_by_key.setdefault((blk, par, ln), []).append(text)
        confs.append(conf)

    if not lines_by_key:
        return "", None, 0

    paragraphs: list[str] = []
    cur_para_key: tuple[int, int] | None = None
    cur_lines: list[str] = []

    for (blk, par, _ln), words in sorted(lines_by_key.items()):
        line_text = " ".join(words).strip()
        if not line_text:
            continue
        para_key = (blk, par)
        if cur_para_key is not None and para_key != cur_para_key and cur_lines:
            paragraphs.append("\n".join(cur_lines))
            cur_lines = []
        cur_para_key = para_key
        cur_lines.append(line_text)
    if cur_lines:
        paragraphs.append("\n".join(cur_lines))

    avg_conf = round(sum(confs) / len(confs), 2) if confs else None
    return "\n\n".join(paragraphs).strip(), avg_conf, len(confs)


class TesseractOcrAdapter:
    """로컬 시스템 `tesseract` 바이너리를 stdin/stdout으로 호출하는 무의존성 OCR 어댑터.

    네트워크 호출과 임시 평문 파일 기록이 전혀 발생하지 않는다.
    """

    def __init__(
        self,
        *,
        lang: str = DEFAULT_OCR_LANG,
        binary_path: str | None = None,
        psm: int | None = None,
        min_word_confidence: float = 30.0,
        timeout_sec: float = 30.0,
        strict_lang: bool = False,
    ) -> None:
        self._binary = binary_path or shutil.which("tesseract")
        self._requested_lang = lang.strip() or "eng"
        self._psm = psm
        self._min_word_confidence = max(0.0, min(100.0, float(min_word_confidence)))
        self._timeout_sec = max(1.0, float(timeout_sec))
        self._strict_lang = strict_lang
        self._installed_langs_cache: tuple[str, ...] | None = None

    @property
    def name(self) -> str:
        return "tesseract"

    @property
    def available(self) -> bool:
        return self._binary is not None and Path(self._binary).exists()

    def installed_languages(self) -> tuple[str, ...]:
        """설치된 Tesseract 언어 팩 목록을 조회한다(캐시됨)."""
        if self._installed_langs_cache is not None:
            return self._installed_langs_cache
        if not self.available or self._binary is None:
            self._installed_langs_cache = ()
            return ()
        try:
            proc = subprocess.run(
                [self._binary, "--list-langs"],
                check=False,
                capture_output=True,
                text=True,
                timeout=10.0,
            )
        except (OSError, subprocess.SubprocessError):
            self._installed_langs_cache = ()
            return ()
        langs: list[str] = []
        for raw_line in (proc.stdout + "\n" + proc.stderr).splitlines():
            token = raw_line.strip()
            if not token or token.lower().startswith("list of") or " " in token or "/" in token:
                continue
            if token not in langs:
                langs.append(token)
        self._installed_langs_cache = tuple(langs)
        return self._installed_langs_cache

    def _resolve_lang(self) -> tuple[str | None, bool]:
        """요청 언어(`kor+eng` 등) 중 설치된 언어만 골라 반환한다."""
        installed = set(self.installed_languages())
        if not installed:
            return self._requested_lang, False
        parts = [p.strip() for p in self._requested_lang.split("+") if p.strip()]
        available_parts = [p for p in parts if p in installed]
        if available_parts:
            fallback = len(available_parts) < len(parts)
            if fallback and self._strict_lang:
                return None, True
            return "+".join(available_parts), fallback
        if self._strict_lang:
            return None, True
        if "eng" in installed:
            return "eng", True
        non_osd = [lang for lang in sorted(installed) if lang != "osd"]
        return (non_osd[0], True) if non_osd else (None, True)

    def _select_psm(self, request: VisionRequest) -> int:
        if self._psm is not None:
            return self._psm
        # PSM 6(단일 균일 텍스트 블록)은 격자선이 있는 표 크롭에서 테두리를 문자로 오인하므로
        # 자동 레이아웃 분할(PSM 3)을 기본으로 사용한다.
        _ = request
        return 3

    def parse_region(self, request: VisionRequest) -> VisionResult:
        if not self.available or self._binary is None:
            return VisionResult(
                adapter_name=self.name,
                succeeded=False,
                error="tesseract_not_installed",
                metadata={"ocr_lang_requested": self._requested_lang},
            )

        resolved_img = _resolve_request_image(request)
        if resolved_img is None:
            return VisionResult(
                adapter_name=self.name,
                succeeded=False,
                error="missing_image_bytes",
            )
        img_bytes, mime_type = resolved_img

        eff_lang, is_fallback = self._resolve_lang()
        if eff_lang is None:
            return VisionResult(
                adapter_name=self.name,
                succeeded=False,
                error="tesseract_language_unavailable",
                metadata={"ocr_lang_requested": self._requested_lang},
            )

        psm = self._select_psm(request)
        cmd = [self._binary, "stdin", "stdout", "-l", eff_lang, "--psm", str(psm), "tsv"]
        try:
            proc = subprocess.run(
                cmd,
                input=img_bytes,
                check=False,
                capture_output=True,
                timeout=self._timeout_sec,
            )
        except subprocess.TimeoutExpired:
            return VisionResult(
                adapter_name=self.name,
                succeeded=False,
                error="tesseract_timeout",
                metadata={"ocr_lang_used": eff_lang, "psm": str(psm)},
            )
        except OSError:
            return VisionResult(
                adapter_name=self.name,
                succeeded=False,
                error="tesseract_exec_error",
            )

        if proc.returncode != 0:
            return VisionResult(
                adapter_name=self.name,
                succeeded=False,
                error=f"tesseract_exit_{proc.returncode}",
                metadata={"ocr_lang_used": eff_lang, "psm": str(psm)},
            )

        tsv_out = proc.stdout.decode("utf-8", errors="replace")
        text, avg_conf, word_count = _parse_tesseract_tsv(
            tsv_out,
            min_word_confidence=self._min_word_confidence,
        )
        meta: dict[str, str] = {
            "ocr_lang_requested": self._requested_lang,
            "ocr_lang_used": eff_lang,
            "ocr_lang_fallback": "true" if is_fallback else "false",
            "psm": str(psm),
            "mime_type": mime_type,
            "word_count": str(word_count),
        }
        if avg_conf is not None:
            meta["avg_confidence"] = f"{avg_conf:.2f}"
        if not text:
            meta["empty_ocr"] = "true"

        markdown = text if text else "*(OCR 추출 텍스트 없음)*"
        return VisionResult(
            adapter_name=self.name,
            succeeded=True,
            text=text,
            markdown=markdown,
            metadata=meta,
        )


def _extract_chat_content(response_obj: dict[str, Any]) -> str | None:
    """OpenAI Chat Completions 응답 JSON에서 텍스트 본문을 안전하게 추출한다."""
    choices = response_obj.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    message = first.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        joined = "\n".join(p.strip() for p in parts if p.strip()).strip()
        return joined or None
    return None


class OpenAICompatibleVlmAdapter:
    """OpenAI 호환 `/v1/chat/completions` Vision 어댑터.

    Ollama(`/v1`), llama-server(llama.cpp), vLLM, LM Studio, Unsloth 및
    (명시적 외부 허용 시) OpenAI 호환 클라우드 VLM API를 모두 지원한다.
    """

    def __init__(
        self,
        *,
        model: str,
        base_url: str = DEFAULT_OPENAI_COMPAT_BASE_URL,
        api_key: str | None = None,
        api_key_env: str | None = None,
        timeout_sec: float = 90.0,
        max_tokens: int = 2048,
        temperature: float = 0.0,
        language: str = "ko",
        prompt_preset: VisionPromptPreset | str = VisionPromptPreset.AUTO,
        allow_external: bool = False,
        registered_internal: Sequence[str] = (),
    ) -> None:
        cleaned_model = model.strip()
        if not cleaned_model:
            raise ParserInputError("VLM 모델 이름(--vision-model)이 비어 있다.")
        self._model = cleaned_model
        self._base_url = base_url.strip().rstrip("/")
        self._zone = validate_vision_endpoint(
            self._base_url,
            allow_external=allow_external,
            registered_internal=registered_internal,
        )
        resolved_key = api_key
        if not resolved_key and api_key_env:
            resolved_key = os.environ.get(api_key_env.strip())
        self._api_key = resolved_key.strip() if resolved_key else None
        self._timeout_sec = max(1.0, float(timeout_sec))
        self._max_tokens = max(64, int(max_tokens))
        self._temperature = float(temperature)
        self._language = language
        self._prompt_preset = resolve_prompt_preset(prompt_preset, model=self._model)

    @property
    def name(self) -> str:
        return f"openai-compat:{self._model}"

    @property
    def trust_zone(self) -> TrustZone:
        return self._zone

    @property
    def prompt_preset(self) -> VisionPromptPreset:
        return self._prompt_preset

    def _auth_headers(self) -> dict[str, str]:
        if not self._api_key:
            return {}
        return {"Authorization": f"Bearer {self._api_key}"}

    def check_health(self) -> tuple[bool, str]:
        """`GET /models`로 엔드포인트 연결 가능 여부를 점검한다."""
        url = f"{self._base_url}/models"
        try:
            _open_json_request(
                url,
                zone=self._zone,
                headers=self._auth_headers(),
                timeout_sec=min(10.0, self._timeout_sec),
            )
            return True, "ok"
        except urllib.error.HTTPError as exc:
            return False, f"http_{exc.code}"
        except (urllib.error.URLError, TimeoutError, OSError):
            return False, "connection_error"
        except (ValueError, json.JSONDecodeError):
            return False, "invalid_response"

    def parse_region(self, request: VisionRequest) -> VisionResult:
        resolved_img = _resolve_request_image(request)
        if resolved_img is None:
            return VisionResult(
                adapter_name=self.name,
                succeeded=False,
                error="missing_image_bytes",
            )
        img_bytes, mime_type = resolved_img
        b64 = base64.b64encode(img_bytes).decode("ascii")
        data_url = f"data:{mime_type};base64,{b64}"

        system_prompt, user_prompt = build_vision_prompts(
            request,
            language=self._language,
            preset=self._prompt_preset,
            model=self._model,
        )
        messages: list[dict[str, Any]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append(
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        )
        payload: dict[str, Any] = {
            "model": self._model,
            "temperature": self._temperature,
            "max_tokens": self._max_tokens,
            "messages": messages,
        }

        url = f"{self._base_url}/chat/completions"
        try:
            resp = _open_json_request(
                url,
                zone=self._zone,
                payload=payload,
                headers=self._auth_headers(),
                timeout_sec=self._timeout_sec,
            )
        except urllib.error.HTTPError as exc:
            return VisionResult(
                adapter_name=self.name,
                succeeded=False,
                error=f"http_{exc.code}",
                metadata={
                    "trust_zone": self._zone.value,
                    "model": self._model,
                    "prompt_preset": self._prompt_preset.value,
                },
            )
        except TimeoutError:
            return VisionResult(
                adapter_name=self.name,
                succeeded=False,
                error="timeout",
                metadata={
                    "trust_zone": self._zone.value,
                    "model": self._model,
                    "prompt_preset": self._prompt_preset.value,
                },
            )
        except (urllib.error.URLError, OSError):
            return VisionResult(
                adapter_name=self.name,
                succeeded=False,
                error="connection_error",
                metadata={
                    "trust_zone": self._zone.value,
                    "model": self._model,
                    "prompt_preset": self._prompt_preset.value,
                },
            )
        except (ValueError, json.JSONDecodeError):
            return VisionResult(
                adapter_name=self.name,
                succeeded=False,
                error="invalid_response",
                metadata={
                    "trust_zone": self._zone.value,
                    "model": self._model,
                    "prompt_preset": self._prompt_preset.value,
                },
            )

        raw_content = _extract_chat_content(resp)
        content = _clean_vlm_output(raw_content, self._prompt_preset) if raw_content else ""
        if not content:
            return VisionResult(
                adapter_name=self.name,
                succeeded=False,
                error="empty_response",
                metadata={
                    "trust_zone": self._zone.value,
                    "model": self._model,
                    "prompt_preset": self._prompt_preset.value,
                },
            )

        meta: dict[str, str] = {
            "model": str(resp.get("model") or self._model),
            "trust_zone": self._zone.value,
            "prompt_preset": self._prompt_preset.value,
            "mime_type": mime_type,
        }
        usage = resp.get("usage")
        if isinstance(usage, dict):
            for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
                if isinstance(usage.get(k), int):
                    meta[k] = str(usage[k])

        return VisionResult(
            adapter_name=self.name,
            succeeded=True,
            text=content,
            markdown=content,
            metadata=meta,
        )


class OllamaVisionAdapter:
    """Ollama 네이티브 `/api/chat` (`images: [base64]`) Vision 어댑터."""

    def __init__(
        self,
        *,
        model: str,
        base_url: str = DEFAULT_OLLAMA_BASE_URL,
        timeout_sec: float = 120.0,
        temperature: float = 0.0,
        language: str = "ko",
        prompt_preset: VisionPromptPreset | str = VisionPromptPreset.AUTO,
        allow_external: bool = False,
        registered_internal: Sequence[str] = (),
    ) -> None:
        cleaned_model = model.strip()
        if not cleaned_model:
            raise ParserInputError("Ollama VLM 모델 이름(--vision-model)이 비어 있다.")
        self._model = cleaned_model
        self._base_url = base_url.strip().rstrip("/")
        self._zone = validate_vision_endpoint(
            self._base_url,
            allow_external=allow_external,
            registered_internal=registered_internal,
        )
        self._timeout_sec = max(1.0, float(timeout_sec))
        self._temperature = float(temperature)
        self._language = language
        self._prompt_preset = resolve_prompt_preset(prompt_preset, model=self._model)

    @property
    def name(self) -> str:
        return f"ollama:{self._model}"

    @property
    def trust_zone(self) -> TrustZone:
        return self._zone

    @property
    def prompt_preset(self) -> VisionPromptPreset:
        return self._prompt_preset

    def parse_region(self, request: VisionRequest) -> VisionResult:
        resolved_img = _resolve_request_image(request)
        if resolved_img is None:
            return VisionResult(
                adapter_name=self.name,
                succeeded=False,
                error="missing_image_bytes",
            )
        img_bytes, mime_type = resolved_img
        b64 = base64.b64encode(img_bytes).decode("ascii")

        system_prompt, user_prompt = build_vision_prompts(
            request,
            language=self._language,
            preset=self._prompt_preset,
            model=self._model,
        )
        messages: list[dict[str, Any]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_prompt, "images": [b64]})
        payload: dict[str, Any] = {
            "model": self._model,
            "stream": False,
            "options": {"temperature": self._temperature},
            "messages": messages,
        }

        url = f"{self._base_url}/api/chat"
        try:
            resp = _open_json_request(
                url,
                zone=self._zone,
                payload=payload,
                timeout_sec=self._timeout_sec,
            )
        except urllib.error.HTTPError as exc:
            return VisionResult(
                adapter_name=self.name,
                succeeded=False,
                error=f"http_{exc.code}",
                metadata={
                    "trust_zone": self._zone.value,
                    "model": self._model,
                    "prompt_preset": self._prompt_preset.value,
                },
            )
        except TimeoutError:
            return VisionResult(
                adapter_name=self.name,
                succeeded=False,
                error="timeout",
                metadata={
                    "trust_zone": self._zone.value,
                    "model": self._model,
                    "prompt_preset": self._prompt_preset.value,
                },
            )
        except (urllib.error.URLError, OSError):
            return VisionResult(
                adapter_name=self.name,
                succeeded=False,
                error="connection_error",
                metadata={
                    "trust_zone": self._zone.value,
                    "model": self._model,
                    "prompt_preset": self._prompt_preset.value,
                },
            )
        except (ValueError, json.JSONDecodeError):
            return VisionResult(
                adapter_name=self.name,
                succeeded=False,
                error="invalid_response",
                metadata={
                    "trust_zone": self._zone.value,
                    "model": self._model,
                    "prompt_preset": self._prompt_preset.value,
                },
            )

        msg = resp.get("message")
        raw_content = msg.get("content", "") if isinstance(msg, dict) else ""
        content = _clean_vlm_output(str(raw_content), self._prompt_preset) if raw_content else ""
        if not content:
            return VisionResult(
                adapter_name=self.name,
                succeeded=False,
                error="empty_response",
                metadata={
                    "trust_zone": self._zone.value,
                    "model": self._model,
                    "prompt_preset": self._prompt_preset.value,
                },
            )

        meta: dict[str, str] = {
            "model": str(resp.get("model") or self._model),
            "trust_zone": self._zone.value,
            "prompt_preset": self._prompt_preset.value,
            "mime_type": mime_type,
        }
        if isinstance(resp.get("eval_count"), int):
            meta["completion_tokens"] = str(resp["eval_count"])
        if isinstance(resp.get("prompt_eval_count"), int):
            meta["prompt_tokens"] = str(resp["prompt_eval_count"])

        return VisionResult(
            adapter_name=self.name,
            succeeded=True,
            text=content,
            markdown=content,
            metadata=meta,
        )


class RoutedVisionAdapter:
    """`ParseStrategy`에 따라 OCR과 VLM을 자동 분배하는 하이브리드 어댑터.

    - `EMBEDDED_IMAGE_OCR`: 빠른 로컬 OCR 우선(`prefer_vlm_for_scans=True`면 VLM 우선)
    - `VLM_REGION` / `VLM_FULL_PAGE`: VLM 우선 호출
      - 범용 VLM이고 `inject_ocr_hints_to_vlm=True`이면 로컬 OCR 텍스트를 힌트로 교차 주입
      - VLM 호출이 실패하고 `fallback_to_ocr=True`이면 OCR 결과로 안전하게 폴백
    """

    def __init__(
        self,
        *,
        ocr_adapter: VisionParserAdapter | None = None,
        vlm_adapter: VisionParserAdapter | None = None,
        prefer_vlm_for_scans: bool = False,
        inject_ocr_hints_to_vlm: bool = True,
        fallback_to_ocr: bool = True,
    ) -> None:
        self._ocr = ocr_adapter
        self._vlm = vlm_adapter
        self._prefer_vlm_for_scans = prefer_vlm_for_scans
        self._inject_ocr_hints = inject_ocr_hints_to_vlm
        self._fallback_to_ocr = fallback_to_ocr

    @property
    def name(self) -> str:
        parts: list[str] = []
        if self._vlm is not None:
            parts.append(self._vlm.name)
        if self._ocr is not None:
            parts.append(self._ocr.name)
        return f"routed({'+'.join(parts) or 'none'})"

    def _vlm_uses_short_ocr_preset(self) -> bool:
        preset = getattr(self._vlm, "prompt_preset", VisionPromptPreset.GENERIC)
        return isinstance(preset, VisionPromptPreset) and preset is not VisionPromptPreset.GENERIC

    def _run_ocr(self, request: VisionRequest) -> VisionResult | None:
        if self._ocr is None:
            return None
        try:
            return self._ocr.parse_region(request)
        except Exception:
            return VisionResult(
                adapter_name=self._ocr.name,
                succeeded=False,
                error="ocr_exception",
            )

    def _run_vlm(self, request: VisionRequest) -> VisionResult | None:
        if self._vlm is None:
            return None
        try:
            return self._vlm.parse_region(request)
        except Exception:
            return VisionResult(
                adapter_name=self._vlm.name,
                succeeded=False,
                error="vlm_exception",
            )

    def parse_region(self, request: VisionRequest) -> VisionResult:
        use_ocr_primary = (
            request.recommended_strategy == ParseStrategy.EMBEDDED_IMAGE_OCR
            and not self._prefer_vlm_for_scans
        )

        if use_ocr_primary:
            ocr_res = self._run_ocr(request)
            if ocr_res is not None and ocr_res.succeeded:
                meta = {**ocr_res.metadata, "routed_via": "ocr_primary"}
                return VisionResult(
                    adapter_name=ocr_res.adapter_name,
                    succeeded=True,
                    text=ocr_res.text,
                    markdown=ocr_res.markdown,
                    metadata=meta,
                )
            vlm_res = self._run_vlm(request)
            if vlm_res is not None:
                meta = {**vlm_res.metadata, "routed_via": "vlm_fallback"}
                return VisionResult(
                    adapter_name=vlm_res.adapter_name,
                    succeeded=vlm_res.succeeded,
                    text=vlm_res.text,
                    markdown=vlm_res.markdown,
                    metadata=meta,
                    error=vlm_res.error,
                )
            if ocr_res is not None:
                return ocr_res
            return VisionResult(
                adapter_name=self.name,
                succeeded=False,
                error="no_vision_adapter_configured",
            )

        # VLM 우선 경로(차트·도식·복합 스캔 페이지)
        # 짧은 트리거 프리셋(GLM-OCR/PaddleOCR-VL 등)은 추가 텍스트 힌트를 쓰지 않으므로
        # 불필요한 사전 OCR 호출을 생략한다(실패 시 폴백 경로는 그대로 유지).
        ocr_res = None
        enriched_req = request
        if (
            self._vlm is not None
            and self._ocr is not None
            and self._inject_ocr_hints
            and not self._vlm_uses_short_ocr_preset()
        ):
            ocr_res = self._run_ocr(request)
            if ocr_res is not None and ocr_res.succeeded and ocr_res.text.strip():
                ocr_tokens = tuple(
                    line.strip() for line in ocr_res.text.splitlines() if line.strip()
                )[:25]
                merged_hints = tuple(dict.fromkeys((*request.embedded_text_hints, *ocr_tokens)))
                enriched_req = VisionRequest(
                    region_id=request.region_id,
                    page=request.page,
                    element_kind=request.element_kind,
                    recommended_strategy=request.recommended_strategy,
                    bbox=request.bbox,
                    reason=request.reason,
                    image_bytes=request.image_bytes,
                    image_extension=request.image_extension,
                    crop_path=request.crop_path,
                    embedded_text_hints=merged_hints,
                    caption=request.caption,
                    context_before=request.context_before,
                    context_after=request.context_after,
                )

        vlm_res = self._run_vlm(enriched_req)
        if vlm_res is not None and vlm_res.succeeded:
            meta = {
                **vlm_res.metadata,
                "routed_via": "vlm_primary",
                "ocr_cross_check": "true"
                if (ocr_res is not None and ocr_res.succeeded and bool(ocr_res.text.strip()))
                else "false",
            }
            return VisionResult(
                adapter_name=vlm_res.adapter_name,
                succeeded=True,
                text=vlm_res.text,
                markdown=vlm_res.markdown,
                metadata=meta,
            )

        # VLM 실패 또는 미설정 시 OCR 폴백
        if self._fallback_to_ocr and self._ocr is not None:
            if ocr_res is None:
                ocr_res = self._run_ocr(request)
            if ocr_res is not None and ocr_res.succeeded:
                meta = {
                    **ocr_res.metadata,
                    "routed_via": "ocr_fallback",
                    "fallback_from": vlm_res.adapter_name if vlm_res is not None else "none",
                }
                if vlm_res is not None and vlm_res.error:
                    meta["vlm_error"] = vlm_res.error
                return VisionResult(
                    adapter_name=ocr_res.adapter_name,
                    succeeded=True,
                    text=ocr_res.text,
                    markdown=ocr_res.markdown,
                    metadata=meta,
                )

        if vlm_res is not None:
            return vlm_res
        if ocr_res is not None:
            return ocr_res
        return VisionResult(
            adapter_name=self.name,
            succeeded=False,
            error="no_vision_adapter_configured",
        )


def create_vision_adapter(
    provider: str,
    *,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    api_key_env: str | None = None,
    ocr_lang: str = DEFAULT_OCR_LANG,
    language: str = "ko",
    prompt_preset: VisionPromptPreset | str = VisionPromptPreset.AUTO,
    allow_external: bool = False,
    registered_internal: Sequence[str] = (),
    timeout_sec: float = 90.0,
) -> VisionParserAdapter:
    """문자열 프로바이더 이름으로 적절한 `VisionParserAdapter`를 생성한다.

    지원 프로바이더: `none`, `tesseract`, `openai-compat`, `ollama`, `hybrid`.
    """
    norm = provider.strip().lower()
    if norm in ("", "none", "noop"):
        return NoopVisionAdapter()
    if norm == "tesseract":
        return TesseractOcrAdapter(lang=ocr_lang, timeout_sec=min(timeout_sec, 60.0))
    if norm == "openai-compat":
        return OpenAICompatibleVlmAdapter(
            model=model or "",
            base_url=base_url or DEFAULT_OPENAI_COMPAT_BASE_URL,
            api_key=api_key,
            api_key_env=api_key_env,
            timeout_sec=timeout_sec,
            language=language,
            prompt_preset=prompt_preset,
            allow_external=allow_external,
            registered_internal=registered_internal,
        )
    if norm == "ollama":
        return OllamaVisionAdapter(
            model=model or "",
            base_url=base_url or DEFAULT_OLLAMA_BASE_URL,
            timeout_sec=timeout_sec,
            language=language,
            prompt_preset=prompt_preset,
            allow_external=allow_external,
            registered_internal=registered_internal,
        )
    if norm == "hybrid":
        ocr = TesseractOcrAdapter(lang=ocr_lang, timeout_sec=min(timeout_sec, 60.0))
        vlm: VisionParserAdapter | None = None
        if model and model.strip():
            vlm = OpenAICompatibleVlmAdapter(
                model=model,
                base_url=base_url or DEFAULT_OPENAI_COMPAT_BASE_URL,
                api_key=api_key,
                api_key_env=api_key_env,
                timeout_sec=timeout_sec,
                language=language,
                prompt_preset=prompt_preset,
                allow_external=allow_external,
                registered_internal=registered_internal,
            )
        return RoutedVisionAdapter(ocr_adapter=ocr, vlm_adapter=vlm)

    raise ParserInputError(
        f"지원하지 않는 비전 어댑터다: '{provider}' "
        "(지원: none, tesseract, openai-compat, ollama, hybrid)"
    )
