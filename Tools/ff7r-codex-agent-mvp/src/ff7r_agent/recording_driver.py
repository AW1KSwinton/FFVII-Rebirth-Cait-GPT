"""Explicit, run-scoped recording attribution; never inferred from chat text."""
from __future__ import annotations

import os
import re

EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"})
MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{2,63}")


def validate_recording_driver(model_id: object, reasoning_effort: object) -> dict[str, str] | None:
    if model_id is None and reasoning_effort is None:
        return None
    if (not isinstance(model_id, str) or MODEL_ID.fullmatch(model_id) is None
            or model_id.lower() in {"auto", "default", "sonnet", "opus", "haiku"}):
        raise ValueError("recording model_id must be an exact model ID, not an alias")
    if not isinstance(reasoning_effort, str) or reasoning_effort not in EFFORTS:
        raise ValueError("recording reasoning_effort must be an explicit supported level")
    return {"modelId": model_id, "effort": reasoning_effort}


def configured_recording_driver(model_id: object, reasoning_effort: object) -> dict[str, str] | None:
    if model_id is None and reasoning_effort is None:
        model_id = os.getenv("FF7R_RECORDING_MODEL")
        reasoning_effort = os.getenv("FF7R_RECORDING_EFFORT")
    return validate_recording_driver(model_id, reasoning_effort)
