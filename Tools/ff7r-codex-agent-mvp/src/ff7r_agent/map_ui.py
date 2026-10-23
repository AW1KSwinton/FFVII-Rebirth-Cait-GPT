"""Consumer-side validation for the read-only map UI sidecar.

The map overlay is produced by a deliberately separate UE4SS probe.  This
module only accepts a snapshot that is bound to the exact ``state.json``
sequence and map identity, and never turns marker data into input authority.
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any


MAX_MARKERS = 512
MAX_TEXT = 512
REQUIRED_STABILITY_SAMPLES = 2
TIMESTAMP_TOLERANCE_SECONDS = 5.0


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _finite_numbers(value: Any) -> bool:
    """Reject non-finite numbers anywhere in marker/page payloads."""
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(_finite_numbers(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return all(_finite_numbers(item) for item in value)
    return True


def _bounded_text(value: Any) -> bool:
    return not isinstance(value, str) or len(value) <= MAX_TEXT


def _valid_marker(marker: Any) -> bool:
    if not isinstance(marker, dict) or not _finite_numbers(marker):
        return False
    if len(marker) > 64:
        return False
    marker_id = marker.get("marker_id", marker.get("id"))
    if not isinstance(marker_id, str) or not marker_id or not _bounded_text(marker_id):
        return False
    # Marker records are descriptive only.  Reject fields that could be
    # mistaken for an action request if a producer is compromised.
    if any(key in marker for key in ("operation", "input", "button", "command")):
        return False
    return all(_bounded_text(value) for value in marker.values())


def read_map_ui(path: str | Path, base: dict[str, Any]) -> dict[str, Any] | None:
    """Return a validated map-state payload, or ``None`` when unavailable.

    The sidecar must be ``ready``, explicitly stable across two samples, and
    bound to the current base sequence.  A stale or malformed file is treated
    as absent so callers fail closed.
    """
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        if int(payload.get("schema_version", 0)) != 1:
            return None
    except (TypeError, ValueError):
        return None
    if payload.get("status") not in {"ready", "complete"}:
        return None
    try:
        if int(payload.get("base_sequence")) != int(base.get("sequence")):
            return None
    except (TypeError, ValueError):
        return None
    sidecar_timestamp = _timestamp(payload.get("timestamp_utc"))
    base_timestamp = _timestamp(base.get("timestamp_utc"))
    if sidecar_timestamp is None or base_timestamp is None:
        return None
    if abs((sidecar_timestamp - base_timestamp).total_seconds()) > TIMESTAMP_TOLERANCE_SECONDS:
        return None
    stability = payload.get("stability")
    if not isinstance(stability, dict):
        return None
    try:
        if int(stability.get("samples", 0)) < REQUIRED_STABILITY_SAMPLES:
            return None
    except (TypeError, ValueError):
        return None
    if stability.get("same_page") is not True or stability.get("same_markers") is not True:
        return None

    base_game = base.get("game") if isinstance(base.get("game"), dict) else {}
    base_map_id = base_game.get("map_id")
    sidecar_map_id = payload.get("map_id")
    if base_map_id is not None and sidecar_map_id is not None and str(base_map_id) != str(sidecar_map_id):
        return None

    map_state = payload.get("map_state", payload.get("payload"))
    if not isinstance(map_state, dict) or not _finite_numbers(map_state):
        return None
    page = map_state.get("page")
    markers = map_state.get("markers")
    if not isinstance(page, dict) or not _finite_numbers(page):
        return None
    if not isinstance(markers, list) or len(markers) > MAX_MARKERS:
        return None
    if not all(_valid_marker(marker) for marker in markers):
        return None

    # Copy the data and attach provenance that consumers can display.  The
    # provenance is metadata only and is never used by movement authorization.
    result = dict(map_state)
    result["_map_ui_meta"] = {
        "schema_version": 1,
        "source": "runtime_map_ui_sidecar",
        "base_sequence": int(payload["base_sequence"]),
        "timestamp_utc": payload["timestamp_utc"],
        "map_id": sidecar_map_id,
        "stability": dict(stability),
    }
    return result
