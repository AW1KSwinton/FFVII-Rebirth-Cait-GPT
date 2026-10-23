"""Read-only live character candidate and target snapshots."""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from .navigation_values import MissionError


SELECTOR = re.compile(r"[\x20-\x7e]{1,512}\Z")
MAX_SIDECAR_BYTES = 4 * 1024 * 1024
DEFAULT_REQUEST_SECONDS = 60
MAX_REQUEST_SECONDS = 300
SHORT_SCAN_OBSERVATION_SECONDS = 4.0
DETAILED_SCAN_OBSERVATION_SECONDS = 15.0
OBSERVATION_SECONDS_BY_MODE = {
    "character_candidates": SHORT_SCAN_OBSERVATION_SECONDS,
    "character_target": SHORT_SCAN_OBSERVATION_SECONDS,
    "character_inventory": DETAILED_SCAN_OBSERVATION_SECONDS,
    "actor_inventory": DETAILED_SCAN_OBSERVATION_SECONDS,
    "traversal_gimmicks": DETAILED_SCAN_OBSERVATION_SECONDS,
}
MAX_TRAVERSAL_GIMMICKS = 256


def _telemetry_directory(service: Any) -> Path:
    path = getattr(getattr(service, "telemetry", None), "path", None)
    if path is None:
        raise MissionError("character_scan_live_sampler_required")
    return Path(path).parent


def _validate_seconds(seconds: int) -> int:
    if type(seconds) is not int or not 10 <= seconds <= MAX_REQUEST_SECONDS:
        raise MissionError("character_scan_request_duration_invalid")
    return seconds


def _validate_selector(selector: str) -> str:
    if not isinstance(selector, str) or not SELECTOR.fullmatch(selector):
        raise MissionError("character_selector_invalid")
    if "|" in selector or any(ord(char) < 0x20 for char in selector):
        raise MissionError("character_selector_invalid")
    if selector.startswith("actor_name:") and len(selector) <= len("actor_name:"):
        raise MissionError("character_selector_invalid")
    return selector


def _request_file(directory: Path) -> Path:
    return directory / "entity-poi.request"


def _read_active_request(path: Path) -> list[str] | None:
    try:
        fields = path.read_text(encoding="ascii").split("|")
    except (FileNotFoundError, OSError, UnicodeError):
        return None
    return fields if len(fields) == 5 else None


def _write_request(
    service: Any,
    *,
    mode: str,
    selector: str,
    seconds: int,
) -> tuple[Path, str]:
    directory = _telemetry_directory(service)
    destination = _request_file(directory)
    request_id = uuid.uuid4().hex
    lock = getattr(service, "_lock", None)
    with lock if lock is not None else nullcontext():
        existing = _read_active_request(destination)
        if existing is not None:
            try:
                if int(existing[1]) > int(time.time()):
                    raise MissionError("character_scan_request_busy")
            except ValueError:
                pass
        content = "|".join(
            [request_id, str(int(time.time()) + seconds), "*", selector, mode]
        )
        temporary = destination.with_suffix(".request." + uuid.uuid4().hex)
        try:
            temporary.write_text(content, encoding="ascii")
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
    return directory, request_id


def release_character_scan(service: Any, request_id: str) -> dict[str, Any]:
    if not isinstance(request_id, str) or not request_id:
        raise MissionError("character_scan_request_id_invalid")
    path = _request_file(_telemetry_directory(service))
    lock = getattr(service, "_lock", None)
    with lock if lock is not None else nullcontext():
        fields = _read_active_request(path)
        if fields is not None and fields[0] == request_id:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            return {"ok": True, "released": True, "request_id": request_id}
    return {"ok": True, "released": False, "request_id": request_id}


def _read_sidecar(path: Path) -> dict[str, Any] | None:
    try:
        if path.stat().st_size > MAX_SIDECAR_BYTES:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _sample_source_sequence(state: dict[str, Any]) -> int:
    """Return the producer sequence used by UE4SS sidecars.

    ``state.json.sequence`` is the output publication counter. The sidecars
    produced by the Lua sampler carry the Lua/source counter in
    ``source_sequence``; they are related but need not be numerically equal.
    Older simulation fixtures may only provide ``sequence``, so retain that
    fallback for compatibility.
    """
    for key in ("source_sequence", "sequence"):
        value = state.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return -1


def _validate_snapshot(
    payload: dict[str, Any] | None,
    *,
    source: str,
    request_id: str,
    sequence: int,
    state_sequence: int,
    state: dict[str, Any],
) -> dict[str, Any] | None:
    if payload is None:
        return None
    if (
        payload.get("source") != source
        or payload.get("request_id") != request_id
        or payload.get("source_sequence") != sequence
        or sequence > state_sequence
        or payload.get("map_id") != state.get("map_id")
        or payload.get("player_actor_id") != (state.get("player") or {}).get("actor_id")
    ):
        return None
    if source == "ue4ss_traversal_gimmicks":
        # A bounded scan is still evidence of the gimmicks it did list; the
        # caller sees ``complete`` and decides how far to trust absence.
        gimmicks = payload.get("gimmicks")
        if gimmicks == {}:
            payload = dict(payload)
            payload["gimmicks"] = gimmicks = []
        if not isinstance(payload.get("complete"), bool):
            return None
        if not isinstance(gimmicks, list) or len(gimmicks) > MAX_TRAVERSAL_GIMMICKS:
            return None
        return payload
    if source in ("ue4ss_character_candidates", "ue4ss_character_inventory", "ue4ss_actor_inventory"):
        if payload.get("complete") is not True:
            return None
        key = "characters" if source in ("ue4ss_character_candidates", "ue4ss_character_inventory") else "actors"
        maximum = 1024 if source in ("ue4ss_character_candidates", "ue4ss_character_inventory") else 2048
        characters = payload.get(key)
        if not isinstance(characters, list) or len(characters) > maximum:
            return None
    else:
        if not isinstance(payload.get("complete"), bool):
            return None
        target = payload.get("target")
        if target is not None and not isinstance(target, dict):
            return None
        nearby = payload.get("nearby")
        if not isinstance(nearby, list) or len(nearby) > 1024:
            return None
        lookup = payload.get("lookup")
        if lookup == []:
            # The Lua serializer represents an empty table as [], although this
            # field is semantically an empty object when no lookup was requested.
            payload = dict(payload)
            payload["lookup"] = {}
        elif lookup is not None and not isinstance(lookup, dict):
            return None
    return payload


def _observe_snapshot(
    service: Any,
    *,
    mode: str,
    selector: str,
    seconds: int,
) -> dict[str, Any]:
    seconds = _validate_seconds(seconds)
    state = service.observe()
    player = state.get("player") if isinstance(state, dict) else None
    telemetry = state.get("telemetry") if isinstance(state, dict) else None
    if (
        not isinstance(state, dict)
        or not state.get("map_id")
        or not isinstance(player, dict)
        or not player.get("actor_id")
        or (
            state.get("source") != "simulation"
            and (not isinstance(telemetry, dict) or telemetry.get("live") is not True)
        )
    ):
        raise MissionError("character_scan_live_telemetry_unavailable")
    directory, request_id = _write_request(
        service, mode=mode, selector=selector, seconds=seconds
    )
    starting_sequence = _sample_source_sequence(state)
    source = {
        "character_candidates": "ue4ss_character_candidates",
        "character_inventory": "ue4ss_character_inventory",
        "actor_inventory": "ue4ss_actor_inventory",
        "traversal_gimmicks": "ue4ss_traversal_gimmicks",
        "character_target": "ue4ss_character_target",
    }[mode]
    sidecar_prefix = {
        "character_candidates": "character-candidates",
        "character_inventory": "character-inventory",
        "actor_inventory": "actor-inventory",
        "traversal_gimmicks": "traversal-gimmicks",
        "character_target": "character-target",
    }[mode]
    # The request lease controls how long the sampler may own the request file.
    # The observation deadline is independent and mode-specific: cheap
    # candidate/target probes stay short while detailed inventories get enough time
    # for a dense reflected scan to publish its sidecar. Waits remain bounded by
    # the request duration and the mode-specific observation cap.

    clock = getattr(service, "_clock", time.monotonic)
    if not callable(clock):
        clock = time.monotonic
    deadline = clock() + min(float(seconds), OBSERVATION_SECONDS_BY_MODE[mode])
    try:
        while clock() < deadline:
            current = service.observe()
            sequence = _sample_source_sequence(current)
            for parity in (0, 1):
                candidate = _read_sidecar(directory / f"{sidecar_prefix}.{parity}.json")
                if candidate is None:
                    continue
                try:
                    source_sequence = int(candidate.get("source_sequence"))
                except (TypeError, ValueError):
                    continue
                if source_sequence < starting_sequence:
                    continue
                payload = _validate_snapshot(
                    candidate,
                    source=source,
                    request_id=request_id,
                    sequence=source_sequence,
                    state_sequence=sequence,
                    state=current,
                )
                if payload is not None:
                    return {
                        "ok": True,
                        "request_id": request_id,
                        "snapshot": payload,
                        "sample": current,
                    }
            sleeper = getattr(service, "_sleeper", time.sleep)
            sleeper(0.15)
        raise MissionError("character_scan_timeout")
    finally:
        release_character_scan(service, request_id)


def scan_character_candidates(
    service: Any, *, seconds: int = DEFAULT_REQUEST_SECONDS
) -> dict[str, Any]:
    """Take one broad, cheap live actor snapshot and release its request."""
    return _observe_snapshot(
        service,
        mode="character_candidates",
        selector="unused",
        seconds=seconds,
    )

def scan_character_inventory(
    service: Any, *, seconds: int = DEFAULT_REQUEST_SECONDS
) -> dict[str, Any]:
    """Take one detailed live character snapshot and release its request."""
    return _observe_snapshot(
        service,
        mode="character_inventory",
        selector="unused",
        seconds=seconds,
    )


def scan_actor_inventory(
    service: Any, *, seconds: int = DEFAULT_REQUEST_SECONDS
) -> dict[str, Any]:
    """Take one bounded live snapshot across character and field-action actors."""
    return _observe_snapshot(
        service,
        mode="actor_inventory",
        selector="unused",
        seconds=seconds,
    )


def scan_traversal_gimmicks(
    service: Any, *, seconds: int = DEFAULT_REQUEST_SECONDS
) -> dict[str, Any]:
    """One live snapshot of the level's traversal gimmicks.

    Each ladder carries its upper and lower trigger volumes, each climbing
    actor its upper volume and climbing points, and each crack or crawl space
    its enter and exit trigger volumes, in world centimetres. These are the
    joins between navmesh islands that a topology capture does not link (live
    at Kalm: the shelter tunnel reaches the town by a crack, a crawl space
    and the inn-cellar ladder).
    """
    return _observe_snapshot(
        service,
        mode="traversal_gimmicks",
        selector="unused",
        seconds=seconds,
    )


def scan_character_target(
    service: Any, selector: str, *, seconds: int = DEFAULT_REQUEST_SECONDS
) -> dict[str, Any]:
    """Take one rich snapshot for an exact UObject or actor-name selector."""
    return _observe_snapshot(
        service,
        mode="character_target",
        selector=_validate_selector(selector),
        seconds=seconds,
    )
