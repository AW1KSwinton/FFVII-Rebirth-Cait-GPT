from __future__ import annotations

import json
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

from .models import Character, GameState, Interactable, PoseSkewError, RestState, Vec3
from .queen_blood import QueenBloodError, QueenBloodState


class TelemetryError(RuntimeError):
    pass


# The navigation probe samples independently of the base exporter. Exact
# sequence matching is retained for arbitrary sidecars. The explicitly
# recognized UE4SS capsule producer may use a timestamp binding because its
# counter is independent of the authoritative state counter. This is not a
# general relaxation for arbitrary sidecars.
# Retained as a compatibility/reporting constant for callers that recorded
# the former policy. It is intentionally not an admission rule: independent
# producer counters have no defined relationship to this numeric distance.
NAVIGATION_MAX_SEQUENCE_SKEW = 256
TRUSTED_NAVIGATION_SOURCES = frozenset({
    "ue4ss_line_trace_multi_height_capsule",
})
TRUSTED_QUEEN_BLOOD_SOURCES = frozenset({
    "ue4ss_queen_blood_read_only_probe",
})
# The UE4SS producer publishes whole-second UTC timestamps while the base
# exporter publishes millisecond timestamps. The no-input live sample series
# observed a maximum absolute delta of 0.871 s; 1.25 s is the smallest bound
# that covers that cadence and timestamp quantisation with modest margin.
NAVIGATION_TIMESTAMP_TOLERANCE_SECONDS = 1.25
QUEEN_BLOOD_TIMESTAMP_TOLERANCE_SECONDS = 1.25
# The Queen's Blood probe searches for card-game UI only while requested
# (each search is a whole-object-array scan, ~400 ms on the game thread).
QUEEN_BLOOD_PROBE_REQUEST_SECONDS = 120
QUEEN_BLOOD_PROBE_WAKE_SECONDS = 3.0
# A brief Windows sharing violation on the request marker is retried (#328).
QUEEN_BLOOD_REQUEST_WRITE_SECONDS = 0.5
_NAVIGATION_BINDING_KEY = "_ff7r_navigation_binding"
# The native base exporter preserves the Lua game-thread timestamp separately
# from its own publication timestamp.  A restart must not turn an old sample
# into apparently fresh gameplay telemetry.  Lua timestamps have whole-second
# precision, so allow the bounded handoff/quantisation interval but reject a
# materially older or future-dated source sample.
BASE_SOURCE_TIMESTAMP_TOLERANCE_SECONDS = 2.0

# UE4SS replaces the live JSON snapshot while the game is running.  A reader
# can briefly observe either the Windows sharing lock or a partially-written
# JSON document.  Keep this retry window short so it does not add load during
# normal polling, while avoiding a false fatal telemetry failure during a
# snapshot handoff.
TELEMETRY_READ_RETRY_ATTEMPTS = 6
TELEMETRY_READ_RETRY_SECONDS = 0.05
# A sample whose capsule and actor root disagree is rejected, then the next
# one is awaited: live 2026-10-05 in the Gold Saucer, single such samples
# stopped pursuit routes that succeeded when simply retried.
POSE_SKEW_RETRY_ATTEMPTS = 6


class LifecycleError(RuntimeError):
    pass


def _parse_utc_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _validate_base_source_timestamp(payload: dict[str, Any]) -> None:
    """Reject a republished base snapshot whose game-thread sample is stale.

    ``timestamp_utc`` is the native publisher handoff time, whereas
    ``source_timestamp_utc`` is written by the Lua game-thread producer.  The
    latter is optional for legacy exporters, but when present it is the only
    way for the consumer to detect a native writer that has re-emitted an old
    ``state.sample`` after restart.
    """
    source_raw = payload.get("source_timestamp_utc")
    if source_raw is None:
        return
    base_timestamp = _parse_utc_timestamp(payload.get("timestamp_utc"))
    source_timestamp = _parse_utc_timestamp(source_raw)
    if base_timestamp is None or source_timestamp is None:
        raise TelemetryError("base source timestamp is invalid")
    delta_seconds = abs((base_timestamp - source_timestamp).total_seconds())
    if delta_seconds > BASE_SOURCE_TIMESTAMP_TOLERANCE_SECONDS:
        raise TelemetryError(
            "base source timestamp is stale or future-dated relative to the "
            f"native handoff ({delta_seconds:.3f}s)"
        )


def _identity_value(payload: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    game = payload.get("game")
    containers = (payload, game) if isinstance(game, dict) else (payload,)
    for container in containers:
        for key in keys:
            value = container.get(key)
            if value is not None and str(value).strip():
                return str(value)
    return None


class RestLifecycleTracker:
    def __init__(self, monotonic=None, start_timeout=3.0, terminal_seconds=5.0) -> None:
        import time
        self._monotonic = monotonic or time.monotonic
        self._start_timeout = float(start_timeout)
        self._terminal_seconds = float(terminal_seconds)
        self._counter = 0
        self._active = None

    def begin(self, state: GameState) -> RestState:
        bench = state.nearby_interactable
        player = state.player
        if bench is None or bench.kind != "bench" or not bench.usable:
            raise LifecycleError("cannot start lifecycle without a usable bench")
        if player is None or not player.actor_id:
            raise LifecycleError("cannot start lifecycle without controlled actor identity")
        if self._active and self._active["state"] not in {
            "interaction_completed", "cancelled", "failed"
        }:
            raise LifecycleError("a rest lifecycle is already active")
        self._counter += 1
        self._active = {
            "id": f"codex-bench-{self._counter}",
            "bench_id": bench.id,
            "identity": player.actor_id,
            "started_sequence": state.sequence,
            "completed_sequence": None,
            "started_at": self._monotonic(),
            "terminal_at": None,
            "state": "interaction_started",
            "failure_reason": None,
        }
        return self._rest()

    def _rest(self) -> RestState:
        a = self._active
        if not a:
            return RestState()
        return RestState(a["state"], a["id"], a["identity"], a["bench_id"],
                         a["started_sequence"], a["completed_sequence"], a["failure_reason"])

    def _terminal(self, state: str, sequence: int, reason: str | None = None) -> None:
        if not self._active or self._active["state"] in {
            "interaction_completed", "cancelled", "failed"
        }:
            raise LifecycleError("invalid terminal lifecycle transition")
        self._active["state"] = state
        self._active["completed_sequence"] = sequence
        self._active["failure_reason"] = reason
        self._active["terminal_at"] = self._monotonic()

    def fail(self, sequence: int, reason: str) -> RestState:
        self._terminal("failed", sequence, reason)
        return self._rest()

    def update(self, state: GameState, interaction_id=None, state_identity=None) -> GameState:
        a = self._active
        if not a:
            return state
        now = self._monotonic()
        if a["state"] in {"interaction_completed", "cancelled", "failed"}:
            if now - a["terminal_at"] >= self._terminal_seconds:
                self._active = None
                baseline = "bench_detected" if state.nearby_interactable else "idle"
                return replace(state, rest=RestState(baseline))
            return replace(state, rest=self._rest())
        if interaction_id is not None and interaction_id != a["id"]:
            self._terminal("failed", state.sequence, "mismatched_interaction_id")
            return replace(state, rest=self._rest())
        observed_identity = state_identity or (state.player.actor_id if state.player else None)
        if observed_identity != a["identity"]:
            self._terminal("failed", state.sequence, "mismatched_state_identity")
            return replace(state, rest=self._rest())
        if not state.running or state.age_seconds() > 2.0:
            self._terminal("failed", state.sequence, "stale_or_stopped_telemetry")
            return replace(state, rest=self._rest())
        controller_state = state.player.controller_state if state.player else None
        if a["state"] == "interaction_started":
            if controller_state == 8:
                a["state"] = "resting"
            elif now - a["started_at"] > self._start_timeout:
                self._terminal("failed", state.sequence, "field_action_not_started")
        elif a["state"] == "resting":
            if controller_state == 1:
                self._terminal("interaction_completed", state.sequence)
            elif controller_state != 8:
                self._terminal("cancelled", state.sequence,
                               f"field_action_left_to_state_{controller_state}")
        return replace(state, rest=self._rest())


class SimulationTelemetry:
    def __init__(self) -> None:
        self._lock = Lock()
        self._sequence = 1
        self._distance = 3.0

    def observe(self) -> GameState:
        with self._lock:
            return GameState(
                schema_version=1,
                sequence=self._sequence,
                timestamp_utc=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                running=True,
                paused=False,
                in_menu=False,
                in_combat=False,
                location="Simulation room",
                objective="Approach and interact with the bench",
                player=Character(
                    name="Cloud", hp=1000, max_hp=1000, mp=30, max_mp=30,
                    position=Vec3(0.0, 0.0, 0.0)
                ),
                nearby_interactable=Interactable("bench_sim", "bench", self._distance, True),
                source="simulation",
            )

    def apply_forward_progress(self, amount: float) -> None:
        with self._lock:
            self._distance = max(0.0, self._distance - amount)
            self._sequence += 1


def _queen_blood_screen_shown(payload: dict) -> bool:
    deck_screen = payload.get("deck_screen")
    talk_menu = payload.get("talk_menu")
    return bool(
        payload.get("active") is True
        or payload.get("match_screen") is True
        or payload.get("begin_match_available") is True
        or (isinstance(deck_screen, dict) and deck_screen.get("observed") is True)
        or (isinstance(talk_menu, dict) and talk_menu.get("observed") is True)
    )


class JsonFileTelemetry:
    def __init__(
        self,
        path: str | Path,
        lifecycle: RestLifecycleTracker | None = None,
        supplemental_path: str | Path | None = None,
        navigation_path: str | Path | None = None,
        navigation_route_path: str | Path | None = None,
        queen_blood_path: str | Path | None = None,
        map_ui_path: str | Path | None = None,
    ) -> None:
        self.path = Path(path)
        self.lifecycle = lifecycle or RestLifecycleTracker()
        self.supplemental_path = (
            Path(supplemental_path)
            if supplemental_path is not None
            else self.path.with_name("surroundings.json")
        )
        self.navigation_path = Path(navigation_path) if navigation_path is not None else self.path.with_name("navigation.json")
        self.navigation_route_path = Path(navigation_route_path) if navigation_route_path is not None else self.path.with_name("navigation-path.json")
        self.queen_blood_path = Path(queen_blood_path) if queen_blood_path is not None else self.path.with_name("queen_blood.json")
        self.map_ui_path = Path(map_ui_path) if map_ui_path is not None else self.path.with_name("map-ui.json")
        self.queen_blood_request_path = self.queen_blood_path.with_name("queen_blood.request")
        self.qb_recorder = None
        self._queen_blood_read_mode = ContextVar("queen_blood_read_mode", default=None)
        self._queen_blood_request_lock = Lock()
        self._queen_blood_request_error: str | None = None

    def _read_recorded_text(self, path: Path) -> str:
        """Capture only reads the consumer already performs, including bad JSON."""
        text = path.read_text(encoding="utf-8")
        recorder = self.qb_recorder
        if recorder is not None and recorder.active:
            recorder.capture("sidecar", {"name": path.name, "text": text},
                             sample_key="raw:" + str(path))
        return text

    def _queen_blood_probe_request(self) -> tuple[int, str]:
        try:
            fields = self.queen_blood_request_path.read_text(encoding="ascii").split()
            scope = fields[1] if len(fields) >= 2 else "match"
            if len(fields) not in (1, 2, 4) or scope not in {"match", "dialogue"}:
                return 0, "match"
            if len(fields) == 4 and fields[3] not in {"full", "cursor"}:
                return 0, "match"
            return int(fields[0]), scope
        except (OSError, ValueError):
            return 0, "match"

    def _queen_blood_probe_request_expiry(self) -> int:
        return self._queen_blood_probe_request()[0]

    def _write_queen_blood_probe_request(self, expires: int, scope: str = "match",
                                       ticket: str = "-", mode: str = "full") -> bool:
        # Live 2026-10-12 (#328): one write in many failed during a Talk and
        # aborted the challenge. The game reads this marker every sample, and
        # Windows refuses to replace a file another process has open, so a
        # brief sharing violation is retried. Each write has its own
        # temporary file, so concurrent writers never replace each other's.
        content = (f"{expires}" + (" dialogue" if scope == "dialogue" else "")
                   if ticket == "-" else f"{expires} {scope} {ticket} {mode}")
        temporary = self.queen_blood_request_path.with_name(
            f"{self.queen_blood_request_path.name}.{uuid.uuid4().hex}.tmp")
        deadline = time.monotonic() + QUEEN_BLOOD_REQUEST_WRITE_SECONDS
        try:
            temporary.write_text(content + "\n", encoding="ascii")
            while True:
                try:
                    temporary.replace(self.queen_blood_request_path)
                    break
                except PermissionError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.02)
        except OSError as exc:
            self._queen_blood_request_error = f"{type(exc).__name__}: {exc}"
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            return False
        self._queen_blood_request_error = None
        return True

    def request_queen_blood_probe(self, seconds: float = QUEEN_BLOOD_PROBE_REQUEST_SECONDS) -> bool:
        """Create the match lease after Yes; this alone performs no scan.

        Read the shared marker rather than a cached expiry: another worker
        may have released it after the previous match.
        """
        now = time.time()
        expires, scope = self._queen_blood_probe_request()
        if scope == "match" and expires - now > seconds / 2:
            return True
        return self._write_queen_blood_probe_request(int(now + seconds))

    def request_queen_blood_dialogue(self, seconds: float = QUEEN_BLOOD_PROBE_REQUEST_SECONDS) -> bool:
        """Read only Talk's answer wheel, without card/manager discovery."""
        now = time.time()
        expires, scope = self._queen_blood_probe_request()
        if scope == "dialogue" and expires - now > seconds / 2:
            return True
        return self._write_queen_blood_probe_request(int(now + seconds), "dialogue")

    def release_queen_blood_probe(self) -> bool:
        """Atomically expire the scan request after result dismissal."""
        return self._write_queen_blood_probe_request(0)

    def wake_queen_blood_probe(self, timeout_seconds: float = QUEEN_BLOOD_PROBE_WAKE_SECONDS) -> bool:
        """Compatibility entry point for one fresh full read, never a loop."""
        expires, scope = self._queen_blood_probe_request()
        # Only a confirmed Yes creates the match lease. Read/edit/move calls
        # may renew it, but cannot start full discovery in ordinary gameplay.
        if scope != "match" or expires <= 0:
            return False
        return self._refresh_queen_blood("full", timeout_seconds)

    @contextmanager
    def queen_blood_reads(self, mode: str = "full"):
        """Refresh QB only inside an explicit UI operation, on consumer reads.

        The recorder, speech, field navigation and generic observations never
        request samples. Context-local state prevents an async action from
        turning unrelated reads into scan requests.
        """
        if mode not in {"full", "cursor"}:
            raise ValueError("unknown Queen's Blood read mode")
        token = self._queen_blood_read_mode.set(mode)
        try:
            yield
        finally:
            self._queen_blood_read_mode.reset(token)

    def _refresh_queen_blood(self, mode: str,
                            timeout_seconds: float = QUEEN_BLOOD_PROBE_WAKE_SECONDS) -> bool:
        # Only initiation can create a lease. Explicit reads may renew a
        # paused match's lease, but marker 0 is an authoritative release.
        with self._queen_blood_request_lock:
            expires, scope = self._queen_blood_probe_request()
            if expires <= 0:
                return False
            ticket = uuid.uuid4().hex
            if not self._write_queen_blood_probe_request(
                    int(time.time() + QUEEN_BLOOD_PROBE_REQUEST_SECONDS), scope, ticket, mode):
                raise TelemetryError("cannot request a fresh Queen's Blood sample"
                                     + (f" ({self._queen_blood_request_error})"
                                        if self._queen_blood_request_error else ""))
            return self._wait_queen_blood_ticket(ticket, timeout_seconds)

    def _wait_queen_blood_ticket(self, ticket: str, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + float(timeout_seconds)
        while True:
            try:
                sidecar = json.loads(self._read_recorded_text(self.queen_blood_path))
                payload = sidecar.get("queen_blood") if isinstance(sidecar, dict) else None
                if isinstance(payload, dict) and payload.get("probe_request") == ticket:
                    return True
            except (OSError, json.JSONDecodeError):
                pass
            if time.monotonic() >= deadline:
                raise TelemetryError("fresh Queen's Blood sample request timed out")
            time.sleep(0.1)

    def _read_map_ui(self, base: dict) -> dict | None:
        """Merge only a stable, exact-sequence map UI snapshot."""
        from .map_ui import read_map_ui

        return read_map_ui(self.map_ui_path, base)

    def _read_queen_blood(self, base: dict) -> dict | None:
        """Read a schema-valid Queen's Blood sidecar bound to this sample.

        The native Queen's Blood probe keys its snapshot to ``state.sample``.
        The JSON publisher exposes that value as ``source_sequence`` while
        ``sequence`` is its own output counter.  The probe and base exporter
        sample independently, so an exact source-sequence match is preferred
        but cannot be required.  A recognized producer may use its UTC sample
        timestamp within a tight bound instead.
        """
        try:
            sidecar = json.loads(self._read_recorded_text(self.queen_blood_path))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return None
        if not isinstance(sidecar, dict):
            return None
        try:
            if int(sidecar.get("schema_version", 1)) != 1:
                return None
            source_sequence = base.get("source_sequence")
            if source_sequence is None:
                source_sequence = base.get("sequence")
            sidecar_sequence = int(sidecar.get("base_sequence"))
            matches = sidecar_sequence == int(source_sequence)
        except (TypeError, ValueError):
            return None
        if not matches:
            source = str(sidecar.get("source") or "")
            if source not in TRUSTED_QUEEN_BLOOD_SOURCES:
                return None
            base_timestamp = _parse_utc_timestamp(
                base.get("source_timestamp_utc", base.get("timestamp_utc"))
            )
            sidecar_timestamp = _parse_utc_timestamp(sidecar.get("timestamp_utc"))
            if base_timestamp is None or sidecar_timestamp is None:
                return None
            timestamp_delta_seconds = (
                sidecar_timestamp - base_timestamp
            ).total_seconds()
            if abs(timestamp_delta_seconds) > QUEEN_BLOOD_TIMESTAMP_TOLERANCE_SECONDS:
                return None
        payload = sidecar.get("queen_blood", sidecar.get("payload"))
        if not isinstance(payload, dict):
            return None
        try:
            QueenBloodState.from_dict(payload)
        except QueenBloodError:
            return None
        # Keep producer binding visible to callers without allowing the
        # producer to weaken the base-state sequence contract.
        result = dict(payload)
        result.setdefault("telemetry", {
            "base_sequence": int(source_sequence),
            "source": sidecar.get("source", "unknown"),
            "timestamp_utc": sidecar.get("timestamp_utc"),
        })
        return result

    def _read_supplemental(self, base: dict) -> dict:
        """Merge only a sidecar snapshot explicitly tied to this base sequence."""
        try:
            raw = self._read_recorded_text(self.supplemental_path)
            supplemental = json.loads(raw)
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            supplemental = None
        if isinstance(supplemental, dict):
            source_sequence = supplemental.get("base_sequence", supplemental.get("sequence"))
            try:
                sequence_matches = source_sequence is not None and int(source_sequence) == int(base.get("sequence"))
            except (TypeError, ValueError):
                sequence_matches = False
            if sequence_matches:
                for key in ("camera", "map_state", "navigation", "nearby_interactables", "nearby_actors", "interaction", "nearby_entities_complete"):
                    if key in supplemental:
                        base[key] = supplemental[key]

        # Queen's Blood is a separate producer so card-game probing can be enabled
        # and disabled without changing the existing surroundings sidecar.
        queen_blood = self._read_queen_blood(base)
        if queen_blood is not None:
            base["queen_blood"] = queen_blood

        map_ui = self._read_map_ui(base)
        if map_ui is not None:
            # Preserve the existing supplemental payload (including the POI
            # resolver) while letting the dedicated map probe provide the
            # authoritative current page/marker set.
            existing_map_state = base.get("map_state")
            merged_map_state = dict(existing_map_state) if isinstance(existing_map_state, dict) else {}
            merged_map_state.update(map_ui)
            base["map_state"] = merged_map_state
        from .map_poi import merge_poi_snapshot
        merge_poi_snapshot(base, self.path.parent)

        def merge_navigation(
            path: Path,
            *,
            require_exact_sequence: bool = False,
            publish_diagnostics: bool = False,
        ) -> None:
            def publish_diagnostic(
                *,
                reason: str,
                accepted: bool,
                source: str | None = None,
                sidecar_sequence: int | None = None,
                timestamp_delta_seconds: float | None = None,
            ) -> None:
                if not publish_diagnostics:
                    return
                base_sequence_value = base.get("sequence")
                try:
                    base_sequence_value = int(base_sequence_value)
                except (TypeError, ValueError):
                    base_sequence_value = None
                sequence_delta = None
                if base_sequence_value is not None and sidecar_sequence is not None:
                    sequence_delta = sidecar_sequence - base_sequence_value
                base["navigation_diagnostics"] = {
                    "accepted": bool(accepted),
                    "reason": reason,
                    "source": source,
                    "source_path": str(path),
                    "base_sequence": base_sequence_value,
                    "sidecar_sequence": sidecar_sequence,
                    "sequence_delta": sequence_delta,
                    "base_timestamp_utc": base.get("timestamp_utc"),
                    "sidecar_timestamp_utc": navigation.get("timestamp_utc") if isinstance(navigation, dict) else None,
                    "timestamp_delta_seconds": timestamp_delta_seconds,
                }

            try:
                raw_navigation = self._read_recorded_text(path)
                navigation = json.loads(raw_navigation)
            except (FileNotFoundError, OSError, json.JSONDecodeError):
                if publish_diagnostics:
                    base["navigation_diagnostics"] = {
                        "accepted": False,
                        "reason": "navigation_unavailable",
                        "source": None,
                        "source_path": str(path),
                    }
                return
            if not isinstance(navigation, dict):
                publish_diagnostic(reason="navigation_schema_invalid", accepted=False)
                return
            # This marker is consumer-owned. Never trust a value supplied by a
            # sidecar; it is added only after this merge proves freshness.
            navigation = dict(navigation)
            navigation.pop(_NAVIGATION_BINDING_KEY, None)
            source = str(navigation.get("source") or "") or None
            source_sequence = navigation.get("base_sequence", navigation.get("sequence"))
            try:
                base_sequence = int(base.get("sequence"))
                sidecar_sequence = int(source_sequence) if source_sequence is not None else None
            except (TypeError, ValueError):
                publish_diagnostic(
                    reason="navigation_sequence_invalid",
                    accepted=False,
                    source=source,
                )
                return

            sequence_matches = sidecar_sequence is not None and sidecar_sequence == base_sequence
            if sequence_matches:
                accepted_reason = "navigation_exact_sequence_match"
                trusted_timestamp = False
            elif require_exact_sequence:
                publish_diagnostic(
                    reason="navigation_sequence_mismatch",
                    accepted=False,
                    source=source,
                    sidecar_sequence=sidecar_sequence,
                )
                return
            elif source not in TRUSTED_NAVIGATION_SOURCES:
                publish_diagnostic(
                    reason="navigation_untrusted_source",
                    accepted=False,
                    source=source,
                    sidecar_sequence=sidecar_sequence,
                )
                return
            else:
                base_timestamp = _parse_utc_timestamp(base.get("timestamp_utc"))
                sidecar_timestamp_raw = navigation.get("timestamp_utc")
                sidecar_timestamp = _parse_utc_timestamp(sidecar_timestamp_raw)
                if base_timestamp is None:
                    publish_diagnostic(
                        reason="navigation_base_timestamp_invalid",
                        accepted=False,
                        source=source,
                        sidecar_sequence=sidecar_sequence,
                    )
                    return
                if not isinstance(sidecar_timestamp_raw, str) or not sidecar_timestamp_raw.strip():
                    publish_diagnostic(
                        reason="navigation_timestamp_missing",
                        accepted=False,
                        source=source,
                        sidecar_sequence=sidecar_sequence,
                    )
                    return
                if sidecar_timestamp is None:
                    publish_diagnostic(
                        reason="navigation_timestamp_invalid",
                        accepted=False,
                        source=source,
                        sidecar_sequence=sidecar_sequence,
                    )
                    return
                timestamp_delta_seconds = (
                    sidecar_timestamp - base_timestamp
                ).total_seconds()
                for identity_name, aliases in (
                    ("map", ("map_id",)),
                    ("world", ("world_id", "world")),
                    ("session", ("session_id", "run_id")),
                ):
                    base_identity = _identity_value(base, aliases)
                    sidecar_identity = _identity_value(navigation, aliases)
                    if (
                        base_identity is not None
                        and sidecar_identity is not None
                        and base_identity != sidecar_identity
                    ):
                        publish_diagnostic(
                            reason=f"navigation_{identity_name}_mismatch",
                            accepted=False,
                            source=source,
                            sidecar_sequence=sidecar_sequence,
                            timestamp_delta_seconds=timestamp_delta_seconds,
                        )
                        return
                if timestamp_delta_seconds < -NAVIGATION_TIMESTAMP_TOLERANCE_SECONDS:
                    publish_diagnostic(
                        reason="navigation_timestamp_stale",
                        accepted=False,
                        source=source,
                        sidecar_sequence=sidecar_sequence,
                        timestamp_delta_seconds=timestamp_delta_seconds,
                    )
                    return
                if timestamp_delta_seconds > NAVIGATION_TIMESTAMP_TOLERANCE_SECONDS:
                    publish_diagnostic(
                        reason="navigation_timestamp_future",
                        accepted=False,
                        source=source,
                        sidecar_sequence=sidecar_sequence,
                        timestamp_delta_seconds=timestamp_delta_seconds,
                    )
                    return
                accepted_reason = "navigation_trusted_timestamp_match"
                trusted_timestamp = True

            existing = base.get("navigation")
            merged = dict(existing) if isinstance(existing, dict) else {}
            if not require_exact_sequence:
                merged.pop(_NAVIGATION_BINDING_KEY, None)
            merged.update(navigation)
            if trusted_timestamp:
                merged[_NAVIGATION_BINDING_KEY] = {
                    "mode": "trusted_timestamp",
                    "source": source,
                    "observed_sequence": base_sequence,
                    "sidecar_sequence": sidecar_sequence,
                }
            base["navigation"] = merged
            publish_diagnostic(
                reason=accepted_reason,
                accepted=True,
                source=source,
                sidecar_sequence=sidecar_sequence,
                timestamp_delta_seconds=(
                    timestamp_delta_seconds if trusted_timestamp else None
                ),
            )

        merge_navigation(self.navigation_path, publish_diagnostics=True)
        # A global route is a movement-authorising plan, not merely contextual
        # clearance evidence. It must bind to this exact player snapshot so a
        # previous target/position can never supply a route leg after a refresh.
        merge_navigation(self.navigation_route_path, require_exact_sequence=True)
        return base

    def observe(self) -> GameState:
        try:
            mode = self._queen_blood_read_mode.get()
            if mode is not None:
                self._refresh_queen_blood(mode)
            return self._observe()
        except TelemetryError as exc:
            if self.qb_recorder is not None:
                self.qb_recorder.capture("telemetry_error", {"error": str(exc)}, sample_key="errors")
            raise

    def _observe(self) -> GameState:
        try:
            for skew_attempt in range(POSE_SKEW_RETRY_ATTEMPTS):
                payload = self._read_payload()
                try:
                    state = GameState.from_dict(payload, source=str(self.path))
                except PoseSkewError:
                    if skew_attempt == POSE_SKEW_RETRY_ATTEMPTS - 1:
                        raise
                    time.sleep(TELEMETRY_READ_RETRY_SECONDS)
                    continue
                state = self.lifecycle.update(state)
                if self.qb_recorder is not None and self.qb_recorder.active:
                    self.qb_recorder.capture("telemetry", {"payload": {**payload, "rest": asdict(state.rest)}},
                                             sample_key="consumer")
                return state
            raise AssertionError("unreachable")
        except FileNotFoundError as exc:
            raise TelemetryError(f"telemetry file not found: {self.path}") from exc
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise TelemetryError(f"cannot read valid telemetry: {exc}") from exc

    def _read_payload(self) -> dict[str, Any]:
        for attempt in range(TELEMETRY_READ_RETRY_ATTEMPTS):
            try:
                payload = json.loads(self._read_recorded_text(self.path))
                break
            except FileNotFoundError:
                raise
            except (OSError, json.JSONDecodeError):
                if attempt == TELEMETRY_READ_RETRY_ATTEMPTS - 1:
                    raise
                time.sleep(TELEMETRY_READ_RETRY_SECONDS)
        if not isinstance(payload, dict):
            raise TelemetryError("telemetry root must be an object")
        _validate_base_source_timestamp(payload)
        return self._read_supplemental(payload)

    def begin_bench_interaction(self, state: GameState) -> RestState:
        return self.lifecycle.begin(state)

    def fail_bench_interaction(self, sequence: int, reason: str) -> RestState:
        return self.lifecycle.fail(sequence, reason)
