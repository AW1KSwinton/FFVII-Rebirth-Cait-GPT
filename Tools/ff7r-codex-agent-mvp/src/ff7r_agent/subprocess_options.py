"""Shared Windows process options for background helper commands."""
from __future__ import annotations

import os
import subprocess
from typing import Any


def no_console_window_kwargs(platform: str | None = None) -> dict[str, Any]:
    """Return a Windows-only creation flag without changing stdio or shell use."""
    if (platform or os.name) != "nt":
        return {}
    # subprocess.CREATE_NO_WINDOW is 0x08000000 on Windows. Keep the documented
    # value as a fallback so an unusual Python build cannot silently show a console.
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
