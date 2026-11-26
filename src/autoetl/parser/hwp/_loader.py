"""`rhwp` 네이티브 모듈 지연 로더.

Linux(`manylinux2014`) 휠에 번들된 구형 `libfreetype`에 누락된 심볼(`FT_Palette_Data_Get`)
충돌을 피하기 위해 시스템 `libfreetype.so.6`을 `RTLD_GLOBAL`로 선로드한 뒤 `rhwp`를 불러온다.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import importlib
import sys
from typing import Any

_RHWP_MODULE: Any | None = None
_RHWP_NODES_MODULE: Any | None = None
_PRELOADED_LINUX_FREETYPE = False


def _preload_linux_freetype() -> None:
    """Linux 환경에서 시스템 `libfreetype.so.6`을 전역 심볼 테이블에 선로드한다."""
    global _PRELOADED_LINUX_FREETYPE
    if _PRELOADED_LINUX_FREETYPE:
        return
    _PRELOADED_LINUX_FREETYPE = True
    if not sys.platform.startswith("linux"):
        return

    candidates: list[str] = ["libfreetype.so.6"]
    resolved = ctypes.util.find_library("freetype")
    if resolved and resolved not in candidates:
        candidates.append(resolved)

    for lib_name in candidates:
        try:
            ctypes.CDLL(lib_name, mode=ctypes.RTLD_GLOBAL)
            return
        except OSError:
            continue


def load_rhwp() -> Any:
    """`rhwp` 최상위 모듈을 로드해 반환한다. 실패 시 `ImportError`/`OSError`를 그대로 올린다."""
    global _RHWP_MODULE
    if _RHWP_MODULE is not None:
        return _RHWP_MODULE
    _preload_linux_freetype()
    mod = importlib.import_module("rhwp")
    _RHWP_MODULE = mod
    return mod


def load_rhwp_nodes() -> Any:
    """`rhwp.ir.nodes` 모듈을 로드해 반환한다."""
    global _RHWP_NODES_MODULE
    if _RHWP_NODES_MODULE is not None:
        return _RHWP_NODES_MODULE
    load_rhwp()
    nodes_mod = importlib.import_module("rhwp.ir.nodes")
    _RHWP_NODES_MODULE = nodes_mod
    return nodes_mod


def rhwp_available() -> bool:
    """현재 환경에서 `rhwp` 네이티브 모듈을 실제로 로드할 수 있는지 반환한다."""
    try:
        load_rhwp()
        return True
    except (ImportError, OSError):
        return False


__all__ = ["load_rhwp", "load_rhwp_nodes", "rhwp_available"]
