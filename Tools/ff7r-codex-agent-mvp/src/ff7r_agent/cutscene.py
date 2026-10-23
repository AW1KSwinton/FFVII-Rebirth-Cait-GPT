"""Cutscene state, from the telemetry exporter's ``cutscene.state``.

The exporter (``cutscene.lua``) lists the level sequence players playing or
paused, by kind: ``cinema`` (the game's event scenes: the ones to skip; live
2026-10-05 the combat simulator's intro, EV_COMON_2112), ``dialogue``,
``minigame`` or ``level`` (ambient shows). It rewrites the file when that
changes and at least once a second, so an older file means the producer
stopped. ``leader`` is False while an event holds Cloud (conversations,
scenes, loads); his own signals are then blank.

Read-only: nothing here sends input.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

STATE_FILE = "cutscene.state"
SCHEMA_VERSION = 4
# The producer rewrites the file at least once a second.
STALE_SECONDS = 2.5
SCENE_KINDS = ("cinema",)
STATUS_PAUSED = 6


def state_path(service: Any) -> Path:
    """``cutscene.state`` beside the telemetry file the service reads."""
    path = getattr(getattr(service, "telemetry", None), "path", None)
    if path is None:
        return Path(r"C:\CaitGPT\Telemetry") / STATE_FILE
    return Path(path).parent / STATE_FILE


def read_cutscene_state(path: Path, now: float | None = None) -> dict[str, Any]:
    """Whether a cutscene is on screen, as the exporter last saw it.

    ``active``: a cinema sequence plays or is paused. ``observed`` is False
    when the file is missing, unreadable, stale or of another schema;
    ``active`` is then None, never False, since nothing is known.
    """
    now = time.time() if now is None else now
    try:
        modified = path.stat().st_mtime
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"observed": False, "active": None, "reason": "cutscene_state_unreadable",
                "detail": type(exc).__name__}
    age = round(max(0.0, now - modified), 3)
    if age > STALE_SECONDS:
        return {"observed": False, "active": None, "reason": "cutscene_state_stale", "age_seconds": age}
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        return {"observed": False, "active": None, "reason": "cutscene_state_schema_unsupported",
                "schema_version": payload.get("schema_version") if isinstance(payload, dict) else None}
    playing = [item for item in payload.get("playing") or [] if isinstance(item, dict)]
    scenes = [item for item in playing if item.get("kind") in SCENE_KINDS]
    return {
        "observed": True,
        "active": bool(scenes),
        "paused": bool(scenes) and all(item.get("status") == STATUS_PAUSED for item in scenes),
        "scenes": scenes,
        "playing": playing,
        "leader": payload.get("leader"),
        "tracked": payload.get("tracked"),
        "anim_cutscene": payload.get("anim_cutscene"),
        "view_target": payload.get("view_target"),
        "sequence": payload.get("sequence"),
        "age_seconds": age,
    }
