"""Request-scoped combat observations. No input, memory writes or battle policy.

The Lua producer uses declared reflected getters, not the generic feed's ATB
and enemy placeholders. Unknown measurements remain None. Source compatibility
does not establish live acceptance of the new getters on an installed build.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import time
from typing import Any
from uuid import uuid4

from .queen_blood_runs import process_identity
from .telemetry import TelemetryError


SCHEMA_VERSION = 1
PRODUCER_CONTRACT = "ff7r-combat-readonly-v1"
PRESENCE_CONTRACT = "ff7r-combat-presence-v1"
SOURCE = "ue4ss_combat_query"
MAX_PARTY = 16
MAX_ENEMIES = 64
MAX_RESPONSE_BYTES = 256 * 1024
MAX_AGE_SECONDS = 2.5  # Lua's timestamp is whole-second UTC, explicitly reported.
ERROR_CODE = re.compile(r"[A-Za-z0-9_.:-]{1,160}\Z")


class CombatObservationError(ValueError):
    pass


def _text(value: Any, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if (not isinstance(value, str) or not value.strip() or len(value) > 1024
            or any(ord(c) < 32 for c in value)):
        raise CombatObservationError("combat_text_invalid")
    return value


def _number(value: Any, *, integer: bool = False) -> int | float | None:
    if value is None:
        return None
    if (type(value) not in (int, float) or (integer and type(value) is not int)
            or not 0 <= value <= 1_000_000_000 or not math.isfinite(value)):
        raise CombatObservationError("combat_measurement_invalid")
    return value


def _boolean(value: Any) -> bool | None:
    if value is not None and type(value) is not bool:
        raise CombatObservationError("combat_boolean_invalid")
    return value


def _errors(value: Any) -> tuple[str, ...]:
    if (not isinstance(value, list) or len(value) > 128
            or any(not isinstance(v, str) or not ERROR_CODE.fullmatch(v) for v in value)):
        raise CombatObservationError("combat_errors_invalid")
    return tuple(value)


@dataclass(frozen=True)
class CombatActor:
    actor_id: str
    chara_id: str | None
    player_type: int | None
    hp: int | None
    max_hp: int | None
    mp: int | None
    max_mp: int | None
    atb: float | None
    distance_m: float | None
    controller_state: int | None
    staggered: bool | None
    pressured: bool | None
    status_effects: tuple[str, ...] | None
    errors: tuple[str, ...]

    @classmethod
    def from_dict(cls, value: Any) -> CombatActor:
        if not isinstance(value, dict):
            raise CombatObservationError("combat_actor_invalid")
        required = {"actor_id", "chara_id", "player_type", "hp", "max_hp", "mp", "max_mp",
                    "atb", "distance_m", "controller_state", "staggered", "pressured", "status_effects", "errors"}
        if not required <= value.keys():
            raise CombatObservationError("combat_actor_incomplete")
        counts = {k: _number(value[k], integer=True)
                  for k in ("player_type", "hp", "max_hp", "mp", "max_mp", "controller_state")}
        if ((counts["player_type"] is not None and counts["player_type"] > 255)
                or (counts["controller_state"] is not None and counts["controller_state"] > 39)
                or any(counts[k] is not None and counts["max_" + k] is not None
                       and counts[k] > counts["max_" + k] for k in ("hp", "mp"))):
            raise CombatObservationError("combat_measurement_conflict")
        statuses = value["status_effects"]
        if statuses is not None:
            if not isinstance(statuses, list) or len(statuses) > 128:
                raise CombatObservationError("combat_status_effects_invalid")
            statuses = tuple(_text(v) for v in statuses)
            if len(statuses) != len(set(statuses)):
                raise CombatObservationError("combat_status_effects_invalid")
        return cls(actor_id=_text(value["actor_id"]), chara_id=_text(value["chara_id"], optional=True),
                   **counts, atb=_number(value["atb"]), distance_m=_number(value["distance_m"]),
                   staggered=_boolean(value["staggered"]), pressured=_boolean(value["pressured"]),
                   status_effects=statuses, errors=_errors(value["errors"]))


@dataclass(frozen=True)
class CombatSnapshot:
    source_sequence: int
    timestamp_utc: str
    map_id: str
    world_id: str | None
    controlled_actor_id: str
    in_battle: bool | None
    phase: str
    paused: bool | None
    party: tuple[CombatActor, ...]
    enemies: tuple[CombatActor, ...]
    party_complete: bool
    enemies_complete: bool
    target_id: str | None
    target_observed: bool
    errors: tuple[str, ...]

    @classmethod
    def from_dict(cls, payload: Any) -> CombatSnapshot:
        if not isinstance(payload, dict):
            raise CombatObservationError("combat_response_invalid")
        if (type(payload.get("schema_version")) is not int or payload["schema_version"] != SCHEMA_VERSION
                or payload.get("producer_contract") != PRODUCER_CONTRACT or payload.get("source") != SOURCE):
            raise CombatObservationError("combat_schema_unsupported")
        if (type(payload.get("source_sequence")) is not int or payload["source_sequence"] < 0
                or payload.get("timestamp_precision") != "seconds"
                or type(payload.get("observed")) is not bool
                or "in_battle" not in payload or "paused" not in payload):
            raise CombatObservationError("combat_response_incomplete")
        if payload.get("input_sent") is not False or payload.get("input_authorized") is not False:
            raise CombatObservationError("combat_producer_input_contract_invalid")
        battle = _boolean(payload["in_battle"])
        phase = "unknown" if battle is None else "active" if battle else "inactive"
        if payload.get("phase") != phase or payload["observed"] != (battle is not None):
            raise CombatObservationError("combat_phase_conflict")
        complete = payload.get("complete")
        target = payload.get("target")
        if (not isinstance(complete, dict) or set(complete) != {"party", "enemies"}
                or any(type(complete.get(k)) is not bool for k in ("party", "enemies"))
                or not isinstance(target, dict) or type(target.get("observed")) is not bool
                or "actor_id" not in target):
            raise CombatObservationError("combat_response_incomplete")
        rosters = {}
        for key, bound in (("party", MAX_PARTY), ("enemies", MAX_ENEMIES)):
            rows = payload.get(key)
            if not isinstance(rows, list) or len(rows) > bound:
                raise CombatObservationError("combat_roster_invalid")
            rosters[key] = tuple(CombatActor.from_dict(v) for v in rows)
        ids = [v.actor_id for rows in rosters.values() for v in rows]
        if len(ids) != len(set(ids)):
            raise CombatObservationError("combat_roster_identity_conflict")
        actor = _text(payload.get("controlled_actor_id"))
        if battle and complete["party"] and actor not in {v.actor_id for v in rosters["party"]}:
            raise CombatObservationError("combat_controlled_actor_not_in_party")
        target_id = _text(target["actor_id"], optional=True)
        if not target["observed"] and target_id is not None:
            raise CombatObservationError("combat_target_conflict")
        if battle is not True and (ids or any(complete.values()) or target_id is not None):
            raise CombatObservationError("combat_inactive_roster_conflict")
        return cls(source_sequence=payload["source_sequence"], timestamp_utc=_text(payload.get("timestamp_utc")),
                   map_id=_text(payload.get("map_id")), world_id=_text(payload.get("world_id")),
                   controlled_actor_id=actor, in_battle=battle, phase=phase, paused=_boolean(payload["paused"]),
                   **rosters, party_complete=complete["party"], enemies_complete=complete["enemies"],
                   target_id=target_id, target_observed=target["observed"], errors=_errors(payload.get("errors")))

    def to_dict(self) -> dict[str, Any]:
        return json.loads(json.dumps(asdict(self)))  # tuples become JSON arrays; no stale raw fields escape.


def _base_observation(service: Any) -> dict[str, Any]:
    base = service.observe()
    if (not isinstance(base, dict) or base.get("source") in (None, "simulation", "unknown")
            or base.get("running") is not True or not isinstance(base.get("telemetry"), dict)
            or base["telemetry"].get("live") is not True or not isinstance(base.get("player"), dict)):
        raise CombatObservationError("combat_live_identity_unavailable")
    age = base["telemetry"].get("age_seconds")
    if type(age) not in (int, float) or not 0 <= age <= MAX_AGE_SECONDS or not math.isfinite(age):
        raise CombatObservationError("combat_base_stale")
    _text(base.get("map_id"))
    _text((base.get("player") or {}).get("actor_id"))
    sequence = base.get("source_sequence", base.get("sequence"))
    if type(sequence) is not int or sequence < 0:
        raise CombatObservationError("combat_base_sequence_unavailable")
    return base


def _battle_state(base: dict[str, Any]) -> bool | None:
    # The old generic false was a placeholder. Never use it to disable a
    # reader or claim battle exit without the new producer's observed flag.
    if base.get("combat_state_observed") is not True or type(base.get("in_combat")) is not bool:
        return None
    return base["in_combat"]


def _same_base(before: dict[str, Any], after: dict[str, Any]) -> None:
    if (after["map_id"] != before["map_id"]
            or after["player"]["actor_id"] != before["player"]["actor_id"]):
        raise CombatObservationError("combat_identity_changed")
    if before.get("world_id") is not None and after.get("world_id") != before["world_id"]:
        raise CombatObservationError("combat_world_changed")
    if after.get("source_sequence", after.get("sequence")) < before.get("source_sequence", before.get("sequence")):
        raise CombatObservationError("combat_sequence_regressed")


def _inactive(base: dict[str, Any], process: dict[str, str]) -> dict[str, Any]:
    # An inactive receipt uses the already published base flag, never a new
    # combat query or a stale combat roster. Base telemetry may lack world ID.
    snapshot = CombatSnapshot(
        source_sequence=base.get("source_sequence", base.get("sequence")),
        timestamp_utc=_text(base.get("timestamp_utc")), map_id=base["map_id"],
        world_id=_text(base.get("world_id"), optional=True), controlled_actor_id=base["player"]["actor_id"],
        # The base pause boolean also has legacy unknown-to-false defaults;
        # a presence-only receipt does not claim that measurement is known.
        in_battle=False, phase="inactive", paused=None,
        party=(), enemies=(), party_complete=False, enemies_complete=False,
        target_id=None, target_observed=False, errors=(),
    )
    return {"ok": True, "available": True, "request_id": None, "combat": snapshot.to_dict(),
            "schema_version": SCHEMA_VERSION, "source": base["source"], "producer_contract": PRESENCE_CONTRACT,
            "observation_kind": "presence", "age_seconds": base["telemetry"]["age_seconds"],
            "timestamp_precision": "base_telemetry", "base_source_sequence": snapshot.source_sequence,
            "identity": {"process_token": process["pid"] + ":" + process["token"],
                         "map_id": snapshot.map_id, "world_id": snapshot.world_id},
            "battle_instance_id": None, "command_ui": None, "result": None,
            "validation": "live_acceptance_pending", "input_sent": False, "input_authorized": False}


def _refusal(reason: str, **details: Any) -> dict[str, Any]:
    return {"ok": False, "available": False, "reason": reason, "combat": None,
            "validation": "live_acceptance_pending", "input_sent": False, "input_authorized": False, **details}


def observe_combat(service: Any, timeout_seconds: float = 3.0) -> dict[str, Any]:
    """One bounded query, never an idle poller or an authorization for input."""
    if type(timeout_seconds) not in (int, float) or not 0.1 <= timeout_seconds <= 8 or not math.isfinite(timeout_seconds):
        raise CombatObservationError("combat_timeout_invalid")
    try:
        base = _base_observation(service)
        active = _battle_state(base)
        if active is None:
            return _refusal("combat_state_unavailable", errors=["battle_presence_unavailable"])
        process = process_identity(service._environment)
        if process is None:
            return _refusal("combat_live_identity_unavailable")
        if not active:
            return _inactive(base, process)
    except TelemetryError:
        return _refusal("combat_telemetry_unavailable")
    except (CombatObservationError, OSError) as exc:
        return _refusal(str(exc) if isinstance(exc, CombatObservationError) else "combat_identity_unreadable")
    directory = Path(service.telemetry.path).parent
    request_path = directory / "combat-query.request"
    request_id = uuid4().hex
    response_path = directory / f"combat-query.{request_id}.json"
    # Exclusive publication prevents stealing another query. No expired marker
    # is automatically removed: the caller that created it owns its cleanup.
    try:
        stream = request_path.open("x", encoding="ascii")
    except FileExistsError:
        return _refusal("combat_query_busy")
    except OSError:
        return _refusal("combat_request_unwritable")
    deadline = time.monotonic() + timeout_seconds
    try:
        with stream:
            stream.write(f"{request_id}|{int(time.time()) + 10}")
        while time.monotonic() < deadline:
            # Only this bounded active-combat query watches the existing base
            # publication. No process enumeration is added to each wait tick.
            after = _base_observation(service)
            _same_base(base, after)
            active = _battle_state(after)
            if active is None:
                return _refusal("combat_state_unavailable", errors=["battle_presence_unavailable"])
            if not active:
                if process_identity(service._environment) != process:
                    raise CombatObservationError("combat_identity_changed")
                return _inactive(after, process)
            try:
                with response_path.open("rb") as reply:
                    raw = reply.read(MAX_RESPONSE_BYTES + 1)
            except FileNotFoundError:
                service._sleeper(min(0.05, max(0, deadline - time.monotonic())))
                continue
            if len(raw) > MAX_RESPONSE_BYTES:
                raise CombatObservationError("combat_response_too_large")
            payload = json.loads(raw)
            if not isinstance(payload, dict) or payload.get("request_id") != request_id:
                raise CombatObservationError("combat_request_identity_mismatch")
            snapshot = CombatSnapshot.from_dict(payload)
            if snapshot.source_sequence <= base.get("source_sequence", base.get("sequence")):
                raise CombatObservationError("combat_sequence_not_advanced")
            while True:
                after = _base_observation(service)
                _same_base(base, after)
                if (snapshot.map_id != base["map_id"]
                        or snapshot.controlled_actor_id != base["player"]["actor_id"]):
                    raise CombatObservationError("combat_identity_changed")
                active = _battle_state(after)
                if active is None:
                    return _refusal("combat_state_unavailable", errors=["battle_presence_unavailable"])
                if not active:
                    if process_identity(service._environment) != process:
                        raise CombatObservationError("combat_identity_changed")
                    return _inactive(after, process)
                for sample in (base, after):
                    world = sample.get("world_id")
                    if world is not None and world != snapshot.world_id:
                        raise CombatObservationError("combat_world_changed")
                # The Lua reply is published before this tick's native base
                # snapshot. Wait for that publication, not for a second query.
                if after.get("source_sequence", after.get("sequence")) >= snapshot.source_sequence:
                    break
                if time.monotonic() >= deadline:
                    raise CombatObservationError("combat_sequence_unbound")
                service._sleeper(min(0.05, max(0, deadline - time.monotonic())))
            if process_identity(service._environment) != process:
                raise CombatObservationError("combat_identity_changed")
            stamp = datetime.fromisoformat(snapshot.timestamp_utc.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                raise CombatObservationError("combat_timestamp_invalid")
            age = (datetime.now(timezone.utc) - stamp).total_seconds()
            if not 0 <= age <= MAX_AGE_SECONDS:
                raise CombatObservationError("combat_snapshot_stale")
            if snapshot.in_battle is None:
                return _refusal("combat_state_unavailable", errors=list(snapshot.errors))
            return {"ok": True, "available": True, "request_id": request_id, "combat": snapshot.to_dict(),
                    "schema_version": SCHEMA_VERSION, "source": SOURCE, "producer_contract": PRODUCER_CONTRACT,
                    "observation_kind": "snapshot",
                    "identity": {"process_token": process["pid"] + ":" + process["token"],
                                 "map_id": snapshot.map_id, "world_id": snapshot.world_id},
                    "age_seconds": age, "timestamp_precision": "seconds", "battle_instance_id": None,
                    "base_source_sequence": after.get("source_sequence", after.get("sequence")),
                    "command_ui": None, "result": None, "validation": "live_acceptance_pending",
                    "input_sent": False, "input_authorized": False}
        return _refusal("combat_sampler_timeout")
    except CombatObservationError as exc:
        return _refusal(str(exc))
    except TelemetryError:
        return _refusal("combat_telemetry_unavailable")
    except (ValueError, UnicodeError, TypeError, KeyError, RecursionError):
        return _refusal("combat_response_invalid")
    except OSError:
        return _refusal("combat_response_unreadable")
    finally:
        try:
            if request_path.read_text(encoding="ascii").split("|")[0] == request_id:
                request_path.unlink()
        except (OSError, UnicodeError):
            pass
        # Only this nonce's artifacts are ours. A late reply can never satisfy
        # a later observation; do not read or clean unrelated telemetry files.
        for path in (response_path, response_path.with_suffix(".json.tmp")):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
