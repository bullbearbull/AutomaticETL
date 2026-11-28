"""ONNX 리랭커(`autoetl.docindex.reranker`) 오류·빈 입력·경계 값 엣지 테스트.

실제 onnxruntime/numpy/tokenizers 설치 없이 가짜 모듈·세션·토크나이저를
`sys.modules`에 주입해 지연 import 경로와 점수 산출 계약만 검증한다.
"""

from __future__ import annotations

import math
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from autoetl.docindex.reranker import (
    CrossEncoderReranker,
    OnnxCrossEncoderReranker,
    _resolve_tokenizer_path,
    _sigmoid,
    _slice_windows,
    create_reranker,
)
from autoetl.parser.adapters import ParserInputError


def _hi_score() -> float:
    return round(1.0 / (1.0 + math.exp(-4.0)), 6)


def _lo_score() -> float:
    return round(1.0 / (1.0 + math.exp(4.0)), 6)


class _Encoded:
    def __init__(self, ids: list[int], mask: list[int], *, with_types: bool = True) -> None:
        self.ids = ids
        self.attention_mask = mask
        if with_types:
            self.type_ids = [0] * len(ids)


class _ContentTokenizer:
    """후보에 'TARGET'이 있으면 높은 토큰 id를 내는 결정론적 가짜 토크나이저."""

    def __init__(self, pad_token: str | None = "<pad>", *, with_types: bool = True) -> None:
        self._pad_token = pad_token
        self._with_types = with_types
        self.loaded_from: str | None = None
        self.truncation: int | None = None
        self.padding: tuple[int, str] | None = None

    def enable_truncation(self, max_length: int) -> None:
        self.truncation = max_length

    def token_to_id(self, token: str) -> int | None:
        return 7 if token == self._pad_token else None

    def enable_padding(self, pad_id: int, pad_token: str) -> None:
        self.padding = (pad_id, pad_token)

    def encode_batch(self, pairs: list[tuple[str, str]]) -> list[_Encoded]:
        return [
            _Encoded([101, 1 if "TARGET" in p else 0, 102], [1, 1, 1], with_types=self._with_types)
            for _q, p in pairs
        ]


class _InputMeta:
    def __init__(self, name: str) -> None:
        self.name = name


class _ContentSession:
    """input_ids의 두 번째 토큰이 1이면 +4.0, 아니면 -4.0 로짓을 내는 가짜 세션."""

    def __init__(self, input_names: list[str] | None = None) -> None:
        self._input_names = input_names or ["input_ids", "attention_mask", "token_type_ids"]
        self.feeds: list[dict[str, Any]] = []

    def get_inputs(self) -> list[_InputMeta]:
        return [_InputMeta(n) for n in self._input_names]

    def run(self, _outputs: Any, feed: dict[str, Any]) -> list[list[list[float]]]:
        self.feeds.append(feed)
        return [[[4.0 if int(row[1]) == 1 else -4.0] for row in feed["input_ids"]]]


class _FixedLogitSession:
    def __init__(self, logits: list[Any]) -> None:
        self._logits = logits

    def get_inputs(self) -> list[_InputMeta]:
        return [_InputMeta("input_ids")]

    def run(self, _outputs: Any, feed: dict[str, Any]) -> list[Any]:
        del feed
        return [self._logits]


def _module(name: str, **attrs: Any) -> ModuleType:
    mod = ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    return mod


def _fake_numpy_module(calls: list[Any]) -> ModuleType:
    def asarray(data: Any, dtype: Any = None) -> Any:
        calls.append(dtype)
        return data

    return _module("numpy", int64="int64", asarray=asarray)


def _fake_ort_module(record: list[dict[str, Any]]) -> ModuleType:
    class _SessionOptions:
        def __init__(self) -> None:
            self.graph_optimization_level = 0

    class _GraphOpt:
        ORT_ENABLE_ALL = 99

    class _InferenceSession:
        def __init__(self, path: str, *, sess_options: Any, providers: list[str]) -> None:
            record.append(
                {
                    "path": path,
                    "providers": providers,
                    "level": sess_options.graph_optimization_level,
                }
            )

        def get_inputs(self) -> list[_InputMeta]:
            return [_InputMeta("input_ids")]

        def run(self, _outputs: Any, feed: dict[str, Any]) -> list[list[list[float]]]:
            return [[[1.0] for _row in feed["input_ids"]]]

    return _module(
        "onnxruntime",
        SessionOptions=_SessionOptions,
        GraphOptimizationLevel=_GraphOpt,
        InferenceSession=_InferenceSession,
    )


def _fake_tokenizers_module(tok: _ContentTokenizer) -> ModuleType:
    class _Factory:
        @staticmethod
        def from_file(path: str) -> _ContentTokenizer:
            tok.loaded_from = path
            return tok

    return _module("tokenizers", Tokenizer=_Factory)


def _content_reranker(**kwargs: Any) -> OnnxCrossEncoderReranker:
    return OnnxCrossEncoderReranker(
        "stub.onnx",
        session=_ContentSession(),
        tokenizer=_ContentTokenizer(),
        **kwargs,
    )


def test_sigmoid_boundaries_symmetry_and_monotonicity() -> None:
    assert _sigmoid(0.0) == 0.5
    assert _sigmoid(-0.0) == 0.5
    assert _sigmoid(700.0) == pytest.approx(1.0)
    assert _sigmoid(-700.0) == pytest.approx(0.0)
    for x in (0.25, 1.0, 4.0, 30.0):
        assert _sigmoid(-x) == pytest.approx(1.0 - _sigmoid(x))
    values = [_sigmoid(x) for x in (-8.0, -1.0, 0.0, 1.0, 8.0)]
    assert values == sorted(values)
    assert all(0.0 <= v <= 1.0 for v in values)


def test_slice_windows_empty_and_short_inputs() -> None:
    assert _slice_windows("") == [""]
    assert _slice_windows("   \n\t ") == [""]
    assert _slice_windows("  짧은 본문  ", max_chars=100) == ["짧은 본문"]
    exact = "가" * 1600
    assert _slice_windows(exact) == [exact]


def test_slice_windows_two_overlapping_windows_when_just_over_limit() -> None:
    text = "나" * 1601
    assert _slice_windows(text) == ["나" * 1600, "나" * 401]


def test_slice_windows_query_guided_selection_keeps_head_and_best_tail() -> None:
    text = "x" * 400 + "z" * 100
    wins = _slice_windows(text, query="zz", max_chars=100, stride=50, max_windows=2)
    assert wins == ["x" * 100, "z" * 100]
    # 대소문자 무시 겹침 계산
    assert _slice_windows(text, query="ZZ", max_chars=100, stride=50, max_windows=2) == wins


def test_slice_windows_without_usable_query_terms_takes_head_windows() -> None:
    text = "x" * 400 + "z" * 100
    expected = [text[0:100], text[50:150]]
    assert _slice_windows(text, query="", max_chars=100, stride=50, max_windows=2) == expected
    # 1글자 토큰은 질의 어휘에서 제외된다
    assert _slice_windows(text, query="a b", max_chars=100, stride=50, max_windows=2) == expected


def test_slice_windows_max_windows_one_and_small_window_count() -> None:
    text = "x" * 400 + "z" * 100
    assert _slice_windows(text, query="zz", max_chars=100, stride=50, max_windows=1) == ["x" * 100]
    short = "y" * 250
    wins = _slice_windows(short, query="zz", max_chars=100, stride=50, max_windows=4)
    assert wins == [short[0:100], short[50:150], short[100:200], short[150:250]]


def test_resolve_tokenizer_path_explicit_same_dir_and_fallback(tmp_path: Path) -> None:
    model = tmp_path / "model.onnx"
    model.write_bytes(b"stub")
    explicit = tmp_path / "custom" / "tok.json"
    assert _resolve_tokenizer_path(model, explicit) == explicit
    assert _resolve_tokenizer_path(model, str(explicit)) == explicit

    same_dir = tmp_path / "tokenizer.json"
    same_dir.write_text("{}", encoding="utf-8")
    assert _resolve_tokenizer_path(model, None) == same_dir

    same_dir.unlink()
    # 같은 폴더·상위 폴더 모두 없으면 같은 폴더 경로를 그대로 돌려준다
    assert _resolve_tokenizer_path(model, None) == tmp_path / "tokenizer.json"


def test_create_reranker_kind_normalization_and_unknown_kind(tmp_path: Path) -> None:
    assert create_reranker("") is None
    assert create_reranker("  NONE  ") is None
    rr = create_reranker(" OnNx ", model_path=tmp_path / "mini.onnx", max_length=128)
    assert isinstance(rr, OnnxCrossEncoderReranker)
    assert rr.name == "onnx:mini"
    with pytest.raises(ParserInputError, match="지원하지 않는"):
        create_reranker("bm25")


@pytest.mark.parametrize(
    ("max_length", "batch_size", "max_windows", "expected"),
    [
        (1, 0, 0, (64, 1, 1)),
        (-100, -5, -3, (64, 1, 1)),
        (99999, 16, 99, (2048, 16, 12)),
        (512, 4, 3, (512, 4, 3)),
    ],
)
def test_constructor_clamps_option_ranges(
    max_length: int, batch_size: int, max_windows: int, expected: tuple[int, int, int]
) -> None:
    rr = OnnxCrossEncoderReranker(
        "stub.onnx",
        max_length=max_length,
        batch_size=batch_size,
        max_windows=max_windows,
        session=object(),
        tokenizer=object(),
    )
    assert (rr._max_length, rr._batch_size, rr._max_windows) == expected


def test_available_false_without_optional_dependencies(tmp_path: Path) -> None:
    rr = OnnxCrossEncoderReranker(tmp_path / "absent.onnx")
    assert rr.available is False
    assert rr.name == "onnx:absent"
    # 세션만 주입되면 토크나이저 모듈이 없어 여전히 사용 불가
    assert OnnxCrossEncoderReranker(tmp_path / "absent.onnx", session=object()).available is False
    # 토크나이저만 주입되면 ONNX 모듈이 없어 여전히 사용 불가
    assert OnnxCrossEncoderReranker(tmp_path / "absent.onnx", tokenizer=object()).available is False


def test_available_tracks_model_and_tokenizer_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = tmp_path / "m.onnx"
    model.write_bytes(b"stub")
    tok = tmp_path / "tokenizer.json"
    tok.write_text("{}", encoding="utf-8")
    monkeypatch.setitem(sys.modules, "numpy", _fake_numpy_module([]))
    monkeypatch.setitem(sys.modules, "onnxruntime", _fake_ort_module([]))
    monkeypatch.setitem(sys.modules, "tokenizers", _fake_tokenizers_module(_ContentTokenizer()))

    rr = OnnxCrossEncoderReranker(model)
    assert rr.available is True
    tok.unlink()
    assert rr.available is False
    tok.write_text("{}", encoding="utf-8")
    model.unlink()
    assert rr.available is False


def test_score_pairs_requires_optional_dependencies(tmp_path: Path) -> None:
    rr = OnnxCrossEncoderReranker(tmp_path / "m.onnx")
    with pytest.raises(ParserInputError, match="선택 의존성"):
        rr.score_pairs("질의", ["후보"])


def test_ensure_ready_missing_model_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "numpy", _fake_numpy_module([]))
    monkeypatch.setitem(sys.modules, "onnxruntime", _fake_ort_module([]))
    rr = OnnxCrossEncoderReranker(tmp_path / "missing.onnx")
    with pytest.raises(ParserInputError, match="모델 파일이 없다"):
        rr.score_pairs("질의", ["후보"])


def test_ensure_ready_missing_tokenizers_package(tmp_path: Path) -> None:
    rr = OnnxCrossEncoderReranker(tmp_path / "m.onnx", session=_ContentSession())
    with pytest.raises(ParserInputError, match="tokenizers"):
        rr.score_pairs("질의", ["후보"])


def test_ensure_ready_missing_tokenizer_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "tokenizers", _fake_tokenizers_module(_ContentTokenizer()))
    rr = OnnxCrossEncoderReranker(tmp_path / "m.onnx", session=_ContentSession())
    with pytest.raises(ParserInputError, match=r"tokenizer\.json"):
        rr.score_pairs("질의", ["후보"])


@pytest.mark.parametrize(
    ("pad_token", "expected_padding"),
    [("<pad>", (7, "<pad>")), ("[PAD]", (7, "[PAD]")), (None, (0, "<pad>"))],
)
def test_ensure_ready_builds_session_and_resolves_pad(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pad_token: str | None,
    expected_padding: tuple[int, str],
) -> None:
    model = tmp_path / "m.onnx"
    model.write_bytes(b"stub")
    tok_file = tmp_path / "tokenizer.json"
    tok_file.write_text("{}", encoding="utf-8")
    record: list[dict[str, Any]] = []
    tok = _ContentTokenizer(pad_token=pad_token)
    monkeypatch.setitem(sys.modules, "numpy", _fake_numpy_module([]))
    monkeypatch.setitem(sys.modules, "onnxruntime", _fake_ort_module(record))
    monkeypatch.setitem(sys.modules, "tokenizers", _fake_tokenizers_module(tok))

    rr = OnnxCrossEncoderReranker(model, max_length=300)
    scores = rr.score_pairs("질의", ["TARGET 후보"])

    # 가짜 numpy가 sys.modules에 있는 동안 pytest.approx는 numpy를 탐색하므로
    # 결정론적 반올림 값과 정확히 비교한다.
    assert scores == [round(1.0 / (1.0 + math.exp(-1.0)), 6)]
    assert record[0]["path"] == str(model)
    assert record[0]["providers"] == ["CPUExecutionProvider"]
    assert record[0]["level"] == 99
    assert tok.loaded_from == str(tok_file)
    assert tok.truncation == 300
    assert tok.padding == expected_padding


def test_run_batch_numpy_arrays_input_name_variants_and_type_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Any] = []
    monkeypatch.setitem(sys.modules, "numpy", _fake_numpy_module(calls))
    session = _ContentSession(input_names=["input_ids", "mask", "segment_ids"])
    rr = OnnxCrossEncoderReranker(
        "stub.onnx", session=session, tokenizer=_ContentTokenizer(with_types=False)
    )
    scores = rr.score_pairs("질의", ["TARGET"])

    assert calls == ["int64", "int64", "int64"]
    feed = session.feeds[0]
    assert set(feed) == {"input_ids", "mask", "segment_ids"}
    # type_ids가 없는 인코딩은 0으로 채운다
    assert feed["segment_ids"] == [[0, 0, 0]]
    assert scores == [_hi_score()]


@pytest.mark.parametrize(
    ("logits", "expected"),
    [
        ([[-1.5, 3.0]], _sigmoid(4.5)),
        ([[2.0]], _sigmoid(2.0)),
        ([[]], 0.0),
        ([0.5], _sigmoid(0.5)),
        ([(1.0, 2.0)], _sigmoid(1.0)),
    ],
)
def test_run_batch_logit_head_shapes(logits: list[Any], expected: float) -> None:
    rr = OnnxCrossEncoderReranker(
        "stub.onnx", session=_FixedLogitSession(logits), tokenizer=_ContentTokenizer()
    )
    assert rr._run_batch([("질의", "후보")]) == [pytest.approx(expected)]


def test_score_pairs_empty_passages_returns_empty() -> None:
    assert _content_reranker().score_pairs("질의", []) == []


def test_score_pairs_batch_size_one_preserves_input_order() -> None:
    session = _ContentSession()
    rr = OnnxCrossEncoderReranker(
        "stub.onnx", session=session, tokenizer=_ContentTokenizer(), batch_size=1
    )
    scores = rr.score_pairs("질의", ["TARGET alpha", "plain beta", "TARGET gamma"])
    assert scores == [_hi_score(), _lo_score(), _hi_score()]
    assert len(session.feeds) == 3


def test_score_pairs_long_passage_uses_best_middle_window() -> None:
    rr = _content_reranker()
    # 3개 창 중 가운데 창에만 TARGET이 있어도 최대 창 점수를 사용해야 한다
    passage = "a" * 1600 + "TARGET" + "b" * 1394
    assert rr.score_pairs("질의", [passage]) == [_hi_score()]


def test_score_pairs_whitespace_passage_still_scores() -> None:
    rr = _content_reranker()
    assert rr.score_pairs("질의", ["   "]) == [_lo_score()]


def test_cross_encoder_protocol_runtime_check() -> None:
    class _Stub:
        @property
        def name(self) -> str:
            return "stub"

        @property
        def available(self) -> bool:
            return True

        def score_pairs(self, query: str, passages: Sequence[str]) -> list[float]:
            del query
            return [0.5] * len(passages)

    assert isinstance(_Stub(), CrossEncoderReranker)
    assert not isinstance(object(), CrossEncoderReranker)
