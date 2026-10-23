"""Run the harness's long-lived processes at above-normal priority.

In the open world the game keeps the CPU near 96% busy (live 2026-10-01,
Grasslands: about five of eight cores), and the harness then read telemetry
up to a second late and refused routes as stale. The harness is mostly idle,
so a higher priority costs the game little. Windows does not pass
above-normal priority on to child processes, so each process raises its own.
Set FF7R_PROCESS_PRIORITY=normal to opt out.
"""
from __future__ import annotations

import os
from typing import Any

ABOVE_NORMAL_PRIORITY_CLASS = 0x00008000


def raise_harness_priority(kernel32: Any = None) -> str | None:
    """Raise this process to above-normal priority; returns the class set, or None."""
    if os.environ.get("FF7R_PROCESS_PRIORITY", "above_normal").strip().lower() == "normal":
        return None
    if kernel32 is None:
        if os.name != "nt":
            return None
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    try:
        ok = kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), ABOVE_NORMAL_PRIORITY_CLASS)
    except OSError:
        return None
    return "above_normal" if ok else None
