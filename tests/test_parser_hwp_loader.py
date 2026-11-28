"""`rhwp` 지연 로더의 시스템 libfreetype 선로드(`_preload_linux_freetype`) 단위 테스트.

실제 네이티브 라이브러리를 불러오지 않고 `sys.platform`, `ctypes.CDLL`,
`ctypes.util.find_library`, 모듈의 선로드 완료 플래그를 monkeypatch로 대체해 검증한다.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import sys

import pytest

from autoetl.parser.hwp import _loader


def test_non_linux_does_not_call_cdll(monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux가 아닌 플랫폼에서는 `ctypes.CDLL`을 호출하지 않고 즉시 반환한다."""
    calls: list[tuple[str, int]] = []
    find_args: list[str] = []

    def fake_cdll(name: str, mode: int = 0) -> object:
        calls.append((name, mode))
        return object()

    def fake_find_library(name: str) -> str | None:
        find_args.append(name)
        return None

    monkeypatch.setattr(_loader, "_PRELOADED_LINUX_FREETYPE", False)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(ctypes, "CDLL", fake_cdll)
    monkeypatch.setattr(ctypes.util, "find_library", fake_find_library)

    _loader._preload_linux_freetype()

    assert calls == []
    assert find_args == []


def test_linux_fallback_opens_find_library_candidate_with_rtld_global(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Linux에서 첫 후보가 `OSError`로 실패하면 `find_library` 후보를 `RTLD_GLOBAL`로 연다."""
    resolved = "libfreetype-fallback.so.6"
    calls: list[tuple[str, int]] = []
    find_args: list[str] = []

    def fake_cdll(name: str, mode: int = 0) -> object:
        calls.append((name, mode))
        if name == "libfreetype.so.6":
            raise OSError("fake: first candidate unavailable")
        return object()

    def fake_find_library(name: str) -> str | None:
        find_args.append(name)
        return resolved

    monkeypatch.setattr(_loader, "_PRELOADED_LINUX_FREETYPE", False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(ctypes, "CDLL", fake_cdll)
    monkeypatch.setattr(ctypes.util, "find_library", fake_find_library)

    _loader._preload_linux_freetype()

    assert find_args == ["freetype"]
    assert calls == [
        ("libfreetype.so.6", ctypes.RTLD_GLOBAL),
        (resolved, ctypes.RTLD_GLOBAL),
    ]
    assert _loader._PRELOADED_LINUX_FREETYPE is True


def test_second_call_does_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """선로드가 한 번 완료된 뒤 두 번째 호출은 라이브러리 조회·로드를 하지 않는다."""
    calls: list[tuple[str, int]] = []
    find_args: list[str] = []

    def fake_cdll(name: str, mode: int = 0) -> object:
        calls.append((name, mode))
        return object()

    def fake_find_library(name: str) -> str | None:
        find_args.append(name)
        return None

    monkeypatch.setattr(_loader, "_PRELOADED_LINUX_FREETYPE", False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(ctypes, "CDLL", fake_cdll)
    monkeypatch.setattr(ctypes.util, "find_library", fake_find_library)

    _loader._preload_linux_freetype()
    assert len(calls) == 1

    calls.clear()
    find_args.clear()
    _loader._preload_linux_freetype()

    assert calls == []
    assert find_args == []


def test_linux_all_loads_fail_keeps_flag_and_skips_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Linux에서 로드가 모두 실패해도 예외 없이 끝나고 플래그가 유지되어 재시도하지 않는다."""
    calls: list[tuple[str, int]] = []
    find_args: list[str] = []

    def fake_cdll(name: str, mode: int = 0) -> object:
        calls.append((name, mode))
        raise OSError("fake: library unavailable")

    def fake_find_library(name: str) -> str | None:
        find_args.append(name)
        return None

    monkeypatch.setattr(_loader, "_PRELOADED_LINUX_FREETYPE", False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(ctypes, "CDLL", fake_cdll)
    monkeypatch.setattr(ctypes.util, "find_library", fake_find_library)

    _loader._preload_linux_freetype()

    assert _loader._PRELOADED_LINUX_FREETYPE is True
    assert find_args == ["freetype"]
    assert calls == [("libfreetype.so.6", ctypes.RTLD_GLOBAL)]

    calls.clear()
    find_args.clear()
    _loader._preload_linux_freetype()

    assert calls == []
    assert find_args == []


def test_linux_both_candidates_fail_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Linux에서 첫 후보와 `find_library` 후보가 모두 실패해도 예외가 전파되지 않는다."""
    resolved = "libfreetype-fallback.so.6"
    calls: list[tuple[str, int]] = []
    find_args: list[str] = []

    def fake_cdll(name: str, mode: int = 0) -> object:
        calls.append((name, mode))
        raise OSError("fake: candidate unavailable")

    def fake_find_library(name: str) -> str | None:
        find_args.append(name)
        return resolved

    monkeypatch.setattr(_loader, "_PRELOADED_LINUX_FREETYPE", False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(ctypes, "CDLL", fake_cdll)
    monkeypatch.setattr(ctypes.util, "find_library", fake_find_library)

    _loader._preload_linux_freetype()

    assert find_args == ["freetype"]
    assert calls == [
        ("libfreetype.so.6", ctypes.RTLD_GLOBAL),
        (resolved, ctypes.RTLD_GLOBAL),
    ]
    assert _loader._PRELOADED_LINUX_FREETYPE is True
