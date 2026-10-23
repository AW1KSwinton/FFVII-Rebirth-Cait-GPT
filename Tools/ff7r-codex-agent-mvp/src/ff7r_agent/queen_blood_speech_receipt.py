"""Private one-use acceptance receipt; never derive evidence from pacing history."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import time
import uuid
from typing import Any

from .queen_blood_profile import _write

MAX_AGE_SECONDS = 90


def runtime_binding(directory: Path) -> dict[str, Any] | None:
    from .public_stream_events import match_record_target
    from .queen_blood_runs import QueenBloodRuns, process_identity
    from .safety import WindowsGameEnvironment, default_runtime_dir
    driver, _, history = match_record_target(directory.parent)
    if driver is None:
        return None
    root = Path(os.getenv("FF7R_QUEEN_BLOOD_PROFILE_ROOT") or default_runtime_dir() / "queen-blood")
    run = QueenBloodRuns(root).status(driver, process_identity(WindowsGameEnvironment()))
    return binding(run, "rehearsal" if history == "qb-match-history-rehearsal" else "live")


def binding(run: dict[str, Any], mode: str) -> dict[str, Any] | None:
    assigned, loaded, driver = run.get("assignment"), run.get("loaded_save"), run.get("driver")
    if (mode not in {"live", "rehearsal"} or run.get("run_loaded") is not True
            or run.get("loaded_save_verified") is not True or not isinstance(assigned, dict)
            or not isinstance(loaded, dict) or not isinstance(driver, dict)
            or run.get("profile_id") != assigned.get("profile_id") or not loaded.get("load_id")
            or not driver.get("run_id") or not loaded.get("process")):
        return None
    return {"profile_id": run["profile_id"], "assignment": assigned, "driver": driver,
            "loaded_save": loaded, "mode": mode}


def receipt_path(directory: Path) -> Path:
    return directory.parent / ("." + directory.name + "-speech-receipt.json")


def publish(directory: Path, text: str, tone: str | None, response: dict[str, Any],
            context: dict[str, Any] | None, now: float) -> str | None:
    receipt = None
    if (response.get("ok") is True and isinstance(response.get("queued_at"), str)
            and context is not None and response.get("rehearsal") == (context["mode"] == "rehearsal")):
        receipt = {"id": uuid.uuid4().hex, "created_epoch": now, "context": context,
                   "text": text, "tone": tone, "response": dict(response)}
    _write(receipt_path(directory), {"version": 1, "receipt": receipt})
    return receipt["id"] if receipt else None


def consume_locked(directory: Path, context: dict[str, Any] | None, *, now: float | None = None) -> dict[str, Any] | None:
    """Caller holds the publisher's shared gate. Clear before returning evidence."""
    now = time.time() if now is None else now
    path = receipt_path(directory)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    receipt = value.get("receipt") if isinstance(value, dict) and value.get("version") == 1 else None
    _write(path, {"version": 1, "receipt": None})
    if not isinstance(receipt, dict) or context is None or receipt.get("context") != context:
        return None
    created, response = receipt.get("created_epoch"), receipt.get("response")
    if (type(created) not in {int, float} or not 0 <= now - created <= MAX_AGE_SECONDS
            or not isinstance(response, dict) or response.get("ok") is not True
            or not isinstance(response.get("queued_at"), str)
            or response.get("rehearsal") != (context["mode"] == "rehearsal")
            or not isinstance(receipt.get("id"), str) or len(receipt["id"]) != 32
            or not isinstance(receipt.get("text"), str) or not receipt["text"]):
        return None
    claims_path = directory.parent / ("." + directory.name + "-speech-receipt-claims.json")
    try:
        claims = json.loads(claims_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        claims = {"version": 1, "claimed": {}}
    if not isinstance(claims, dict) or claims.get("version") != 1 or not isinstance(claims.get("claimed"), dict):
        raise ValueError("invalid speech receipt claims")
    claimed = {identifier: at for identifier, at in claims["claimed"].items()
               if type(at) in {int, float} and math.isfinite(at) and now - at <= MAX_AGE_SECONDS * 2}
    if receipt["id"] in claimed:
        return None
    claimed[receipt["id"]] = now
    _write(claims_path, {"version": 1, "claimed": claimed})
    return {"receipt_id": receipt["id"], "text": receipt["text"], "tone": receipt.get("tone"),
            "response": response, "context": context, "created_epoch": created}
