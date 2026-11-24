"""Milestone D2: 선택 초경량 CPU ONNX Cross-Encoder 리랭커(VectorDB·생성 LLM 불필요).

권장 CPU 경량 모델(ONNX INT8 기준, 환각 0% — 입력 원문 후보의 관련도 점수만 산출):
- 한국어/다국어 실무 권장: ``BAAI/bge-reranker-v2-m3`` 또는 ``bge-reranker-v2-m3-ko`` (INT8 ~280MB)
- 초경량 다국어/영문: ``BAAI/bge-reranker-base`` (~110MB), ``ms-marco-MiniLM-L-6-v2`` (~25MB)

핵심 패키지 의존성을 늘리지 않기 위해 ``onnxruntime``, ``numpy``, ``tokenizers``를
지연 import한다. 미설치 상태에서도 기본 0MB FTS5 BM25 + 목차 탐색 경로는 정상 동작한다.
"""

from __future__ import annotations

import importlib
import math
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from autoetl.parser.adapters import ParserInputError

RECOMMENDED_RERANKER_MODELS: dict[str, str] = {
    "bge-reranker-v2-m3": "BAAI/bge-reranker-v2-m3 (다국어·한국어, INT8 ~280MB)",
    "bge-reranker-v2-m3-ko": "bge-reranker-v2-m3-ko (한국어 특화, INT8 ~280MB)",
    "bge-reranker-base": "BAAI/bge-reranker-base (경량, INT8 ~110MB)",
    "minilm-l6-v2": "cross-encoder/ms-marco-MiniLM-L-6-v2 (초경량 영문, INT8 ~25MB)",
}


@runtime_checkable
class CrossEncoderReranker(Protocol):
    """질의-원문 섹션 쌍의 관련도 점수(0.0~1.0)를 계산하는 Cross-Encoder 인터페이스."""

    @property
    def name(self) -> str: ...

    @property
    def available(self) -> bool: ...

    def score_pairs(self, query: str, passages: Sequence[str]) -> list[float]:
        """질의와 각 원문 후보의 관련도 점수(0.0~1.0)를 후보 순서대로 반환한다."""
        ...


def _sigmoid(x: float) -> float:
    if x >= 0.0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def _slice_windows(text: str, *, max_chars: int = 1600, stride: int = 1200) -> list[str]:
    """긴 원문 섹션의 앞/중/뒷부분을 슬라이딩 창으로 나눠 리랭킹 누락을 방지한다.

    리랭킹 점수 산출에만 창 분할을 쓰고, 최종 EvidencePack에는 항상 원문 섹션 전체를 반환한다.
    """
    cleaned = text.strip()
    if len(cleaned) <= max_chars:
        return [cleaned]
    windows: list[str] = []
    start = 0
    limit = len(cleaned)
    while start < limit and len(windows) < 6:
        end = min(limit, start + max_chars)
        windows.append(cleaned[start:end])
        if end >= limit:
            break
        start += stride
    return windows


class OnnxCrossEncoderReranker:
    """CPU 전용 초경량 ONNX Runtime Cross-Encoder 리랭커.

    - ``onnxruntime``, ``numpy``, ``tokenizers``(Rust 기반 경량 토크나이저)를 지연 import한다.
    - PyTorch나 1B+ 생성 LLM 없이 일반 사무용 PC CPU에서 Top-20 후보를 수십~수백 ms에 재정렬한다.
    - ``session``과 ``tokenizer``를 직접 주입할 수 있어 가중치 파일 없이도 단위 테스트가 가능하다.
    """

    def __init__(
        self,
        model_path: Path | str,
        *,
        tokenizer_path: Path | str | None = None,
        max_length: int = 512,
        batch_size: int = 16,
        providers: Sequence[str] = ("CPUExecutionProvider",),
        session: Any = None,
        tokenizer: Any = None,
    ) -> None:
        self._model_path = Path(model_path)
        self._tokenizer_path = (
            Path(tokenizer_path)
            if tokenizer_path is not None
            else self._model_path.parent / "tokenizer.json"
        )
        self._max_length = max(64, min(2048, int(max_length)))
        self._batch_size = max(1, int(batch_size))
        self._providers = tuple(providers)
        self._session: Any = session
        self._tokenizer: Any = tokenizer
        self._ort: Any = None
        self._np: Any = None
        self._tok_mod: Any = None

        try:
            self._np = importlib.import_module("numpy")
        except ImportError:
            self._np = None
        if self._session is None:
            try:
                self._ort = importlib.import_module("onnxruntime")
            except ImportError:
                self._ort = None
        if self._tokenizer is None:
            try:
                self._tok_mod = importlib.import_module("tokenizers")
            except ImportError:
                self._tok_mod = None

    @property
    def name(self) -> str:
        return f"onnx:{self._model_path.stem}"

    @property
    def available(self) -> bool:
        if self._session is not None and self._tokenizer is not None:
            return True
        if self._np is None or self._ort is None or self._tok_mod is None:
            return False
        return self._model_path.is_file() and self._tokenizer_path.is_file()

    def _ensure_ready(self) -> tuple[Any, Any]:
        if self._session is None:
            if self._ort is None or self._np is None:
                raise ParserInputError(
                    "ONNX 리랭커에는 선택 의존성 `onnxruntime`, `numpy`, `tokenizers`가 필요하다. "
                    "(설치 예: `pip install onnxruntime numpy tokenizers`)"
                )
            if not self._model_path.is_file():
                raise ParserInputError(f"ONNX 리랭커 모델 파일이 없다: {self._model_path}")
            self._session = self._ort.InferenceSession(
                str(self._model_path),
                providers=list(self._providers),
            )
        if self._tokenizer is None:
            if self._tok_mod is None:
                raise ParserInputError(
                    "ONNX 리랭커에는 `tokenizers` 패키지가 필요하다. "
                    "(설치 예: `pip install tokenizers`)"
                )
            if not self._tokenizer_path.is_file():
                raise ParserInputError(
                    f"리랭커 `tokenizer.json` 파일이 없다: {self._tokenizer_path}"
                )
            tok = self._tok_mod.Tokenizer.from_file(str(self._tokenizer_path))
            tok.enable_truncation(max_length=self._max_length)
            tok.enable_padding(pad_id=0, pad_token="<pad>")
            self._tokenizer = tok
        return self._session, self._tokenizer

    def _run_batch(self, pairs: list[tuple[str, str]]) -> list[float]:
        session, tokenizer = self._ensure_ready()
        encoded = tokenizer.encode_batch(pairs)

        ids_list = [list(e.ids) for e in encoded]
        mask_list = [list(e.attention_mask) for e in encoded]
        type_list = [list(getattr(e, "type_ids", [0] * len(e.ids))) for e in encoded]

        input_names = [inp.name for inp in session.get_inputs()]
        feed: dict[str, Any] = {}
        if self._np is not None:
            ids_arr: Any = self._np.asarray(ids_list, dtype=self._np.int64)
            mask_arr: Any = self._np.asarray(mask_list, dtype=self._np.int64)
            type_arr: Any = self._np.asarray(type_list, dtype=self._np.int64)
        else:
            ids_arr, mask_arr, type_arr = ids_list, mask_list, type_list

        for name in input_names:
            low = name.lower()
            if "input_id" in low:
                feed[name] = ids_arr
            elif "attention_mask" in low or "mask" in low:
                feed[name] = mask_arr
            elif "token_type" in low or "segment" in low:
                feed[name] = type_arr

        outputs = session.run(None, feed)
        logits = outputs[0]
        raw_scores: list[float] = []
        for row in logits:
            if isinstance(row, (list, tuple)) or hasattr(row, "__len__"):
                vals = list(row)
                if len(vals) >= 2:
                    # [neg_logit, pos_logit] 이진 분류 헤드
                    raw_scores.append(_sigmoid(float(vals[1]) - float(vals[0])))
                elif len(vals) == 1:
                    raw_scores.append(_sigmoid(float(vals[0])))
                else:
                    raw_scores.append(0.0)
            else:
                raw_scores.append(_sigmoid(float(row)))
        return raw_scores

    def score_pairs(self, query: str, passages: Sequence[str]) -> list[float]:
        """질의와 각 원문 섹션의 관련도 점수(0.0~1.0)를 계산한다(장문 섹션은 최대 창 점수 사용)."""
        if not passages:
            return []
        expanded_pairs: list[tuple[str, str]] = []
        owner_indices: list[int] = []
        for idx, passage in enumerate(passages):
            for win in _slice_windows(passage):
                expanded_pairs.append((query, win))
                owner_indices.append(idx)

        window_scores: list[float] = []
        for start in range(0, len(expanded_pairs), self._batch_size):
            batch = expanded_pairs[start : start + self._batch_size]
            window_scores.extend(self._run_batch(batch))

        best_per_passage = [0.0] * len(passages)
        for owner, score in zip(owner_indices, window_scores, strict=True):
            if score > best_per_passage[owner]:
                best_per_passage[owner] = round(score, 6)
        return best_per_passage


def create_reranker(
    kind: str = "none",
    *,
    model_path: Path | str | None = None,
    tokenizer_path: Path | str | None = None,
    max_length: int = 512,
) -> CrossEncoderReranker | None:
    """CLI/SDK 옵션 문자열로 선택 ONNX Cross-Encoder 리랭커를 생성한다."""
    normalized = (kind or "none").strip().lower()
    if normalized == "none":
        return None
    if normalized == "onnx":
        if model_path is None:
            raise ParserInputError(
                "`--reranker onnx` 사용 시 `--reranker-model <model.onnx>` 경로가 필요하다."
            )
        return OnnxCrossEncoderReranker(
            model_path,
            tokenizer_path=tokenizer_path,
            max_length=max_length,
        )
    raise ParserInputError(f"지원하지 않는 리랭커 유형이다: '{kind}' (지원: none, onnx)")


__all__ = [
    "RECOMMENDED_RERANKER_MODELS",
    "CrossEncoderReranker",
    "OnnxCrossEncoderReranker",
    "create_reranker",
]
