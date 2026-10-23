"""Best-effort, model-neutral production evidence emission.

Events are small JSON documents consumed by the independent production worker.
The producer never performs editorial work and never waits for disk in a game
action. Missing monitor timing is represented explicitly as unbound evidence.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import queue
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MAX_EVENT_BYTES = 64 * 1024
DEFAULT_QUEUE_SIZE = 32
SNAPSHOT_MAX_AGE_SECONDS = 6.0


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    """Small private-state JSON write using same-directory replace semantics."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(_canonical(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def timestamp_fresh(value: Any, *, now: datetime | None = None,
                    max_age_seconds: float = SNAPSHOT_MAX_AGE_SECONDS) -> bool:
    """Require a parseable, nonfuture wall timestamp within the monitor window."""
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return False
        age = ((now or datetime.now(timezone.utc)) - parsed.astimezone(timezone.utc)).total_seconds()
        return 0 <= age <= max_age_seconds
    except (ValueError, OverflowError):
        return False


def exact_timing_sample(snapshot: dict[str, Any] | None) -> bool:
    """Require a sample agreeing with the snapshot's exact media point."""
    if not snapshot:
        return False
    observed = snapshot.get("observedAt")
    position = snapshot.get("positionMs")
    samples = snapshot.get("timingSamples")
    return bool(isinstance(observed, str) and isinstance(position, (int, float))
                and not isinstance(position, bool) and isinstance(samples, list)
                and any(isinstance(sample, dict) and sample.get("observedAt") == observed
                        and sample.get("durationMs") == position
                        and sample.get("paused") is False
                        and sample.get("recordingActive") is True for sample in samples))


def _event_id(mode: str, recording_id: str, event_type: str, source_key: str) -> str:
    return hashlib.sha256("\0".join((mode, recording_id, event_type, source_key)).encode("utf-8")).hexdigest()


def build_event(*, state_dir: Path | None, mode: str, event_type: str,
                details: dict[str, Any], source_key: str, story_id: str | None = None,
                related_event_ids: list[str] | None = None,
                occurred_at: str | None = None,
                start_snapshot: dict[str, Any] | None = None,
                end_snapshot: dict[str, Any] | None = None,
                interval_wall_start: str | None = None,
                interval_wall_end: str | None = None) -> dict[str, Any]:
    """Build the shared schema from the latest monitor snapshot, if available.

    The event timestamp and position are never inferred from OBS elapsed time.
    A missing or stale snapshot leaves positionKnown false for worker-side
    reconciliation instead of pretending that zero is an observed offset.
    """
    occurred_at = occurred_at or _utc_now()
    snapshot = end_snapshot or (_read_json(Path(state_dir) / "production" / "current-recording.json") if state_dir else None)
    recording_id = str(snapshot.get("recordingId")) if snapshot and snapshot.get("recordingId") else "unbound"
    session_id = str(snapshot.get("sessionId")) if snapshot and snapshot.get("sessionId") else "unknown-session"
    position = snapshot.get("positionMs") if snapshot else 0
    now = datetime.now(timezone.utc)
    position_known = bool(snapshot and snapshot.get("monitorFresh") is True
                          and snapshot.get("obsFresh") is True
                          and timestamp_fresh(snapshot.get("observedAt"), now=now)
                          and timestamp_fresh(snapshot.get("monitorObservedAt"), now=now)
                          and timestamp_fresh(snapshot.get("obsObservedAt"), now=now)
                          and timestamp_fresh(snapshot.get("durationObservedAt"), now=now)
                          and exact_timing_sample(snapshot)
                          and snapshot.get("paused") is False
                          and snapshot.get("recordingActive") is True
                          and snapshot.get("quarantined") is not True
                          and snapshot.get("durationRollback") is not True
                          and isinstance(position, (int, float)) and not isinstance(position, bool)
                          and position >= 0)
    recording = {
        "recordingId": recording_id,
        "sessionId": session_id,
        "startedAt": snapshot.get("startedAt") if snapshot else occurred_at,
        "observedAt": snapshot.get("observedAt") if snapshot else occurred_at,
        "positionMs": position if isinstance(position, (int, float)) and not isinstance(position, bool) else 0,
        "positionKnown": position_known,
        "provenance": snapshot.get("provenance", "external") if snapshot else "external",
        "visualProof": snapshot.get("visualProof") is True if snapshot else False,
        "paused": snapshot.get("paused") is True if snapshot else False,
    }
    # Keep the event envelope compact. The monitor owns the complete timing
    # history and the worker reads that authoritative snapshot when it needs to
    # reconcile an interval; copying hundreds of samples/evidence IDs into each
    # event would eventually exceed the sink's bounded payload limit.
    for key in ("outputPath", "durationMs", "durationObservedAt", "durationRollback", "everStreamActive",
                "recordingPath", "recordingActive", "recordingComplete", "coverageStartMs",
                "coverageEndMs", "quarantined", "quarantineReason", "monitorFresh", "obsFresh",
                "monitorObservedAt", "obsObservedAt"):
        if snapshot is not None and key in snapshot:
            recording[key] = snapshot[key]
    stream_state = _read_json(Path(state_dir) / "production" / "stream-state.json") if state_dir else None
    stream = stream_state.get("stream") if stream_state and isinstance(stream_state.get("stream"), dict) else None
    stream_fresh = bool(stream_state and stream_state.get("monitorFresh") is True
                        and stream_state.get("obsFresh") is True
                        and timestamp_fresh(stream_state.get("observedAt"), now=now)
                        and timestamp_fresh(stream_state.get("monitorObservedAt"), now=now)
                        and timestamp_fresh(stream_state.get("obsObservedAt"), now=now)
                        and timestamp_fresh(stream.get("observedAt") if stream else None, now=now))
    if stream and stream.get("state") in {"active", "inactive", "unknown"} and (stream_state is None or stream_fresh):
        stream_evidence = {"state": stream["state"], "observedAt": stream.get("observedAt") or occurred_at,
                           "source": stream.get("source") if stream.get("source") in {"obs", "heartbeat", "operator"} else "obs"}
    else:
        stream_evidence = {"state": "unknown", "observedAt": occurred_at, "source": "operator"}
    event = {"schemaVersion": 1,
             "eventId": _event_id(mode, recording_id, event_type, source_key),
             "occurredAt": occurred_at, "mode": mode, "eventType": event_type,
             "recording": recording, "stream": stream_evidence}
    if mode == "development":
        event["interval"] = recording_interval(
            start_snapshot, end_snapshot, occurred_at,
            interval_wall_start, interval_wall_end)
    else:
        event["interval"] = event_point_interval(snapshot, occurred_at, now=now)
    if story_id:
        event["storyId"] = story_id
    if related_event_ids:
        event["relatedEventIds"] = sorted(set(related_event_ids))
    event.update(details)
    return event


def event_point_interval(snapshot: dict[str, Any] | None, occurred_at: str,
                         *, now: datetime | None = None) -> dict[str, Any]:
    """Conservative gameplay point bound from one exact fresh monitor sample."""
    reasons: list[str] = []
    position = snapshot.get("positionMs") if snapshot else None
    observed = snapshot.get("observedAt") if snapshot else None
    if not snapshot:
        reasons.append("recording_snapshot_missing")
    else:
        for name in ("observedAt", "monitorObservedAt", "obsObservedAt", "durationObservedAt"):
            if not timestamp_fresh(snapshot.get(name), now=now):
                reasons.append(name + "_stale_or_missing")
        if not exact_timing_sample(snapshot):
            reasons.append("exact_timing_sample_missing")
        if snapshot.get("monitorFresh") is not True or snapshot.get("obsFresh") is not True:
            reasons.append("timing_source_not_fresh")
        if snapshot.get("paused") is True:
            reasons.append("recording_paused")
        if snapshot.get("durationRollback") is True or snapshot.get("quarantined") is True:
            reasons.append("recording_quarantined_or_rolled_back")
    if not isinstance(position, (int, float)) or isinstance(position, bool) or position < 0:
        reasons.append("media_position_unknown")
        position = 0
    point_wall = observed if isinstance(observed, str) else occurred_at
    point_ms = max(0, int(position))
    return {"wallStartedAt": point_wall, "wallEndedAt": point_wall,
            "mediaStartMs": point_ms, "mediaEndMs": point_ms,
            "uncertain": bool(reasons), "uncertaintyReasons": sorted(set(reasons))}


def known_live_stream(event: dict[str, Any]) -> bool:
    """True only for fresh OBS active evidence; stale state never authorizes work."""
    stream = event.get("stream")
    return bool(isinstance(stream, dict) and stream.get("state") == "active"
                and stream.get("source") == "obs"
                and timestamp_fresh(stream.get("observedAt")))


def read_recording_snapshot(state_dir: Path | None) -> dict[str, Any] | None:
    return _read_json(Path(state_dir) / "production" / "current-recording.json") if state_dir else None


def recording_interval(start: dict[str, Any] | None, end: dict[str, Any] | None,
                       fallback_end: str | None = None, wall_start: str | None = None,
                       wall_end: str | None = None) -> dict[str, Any]:
    """Bound a development run only with matching, monotone monitor evidence."""
    reasons: list[str] = []
    start, end = start or {}, end or {}
    wall_start = wall_start or start.get("observedAt") or start.get("startedAt") or fallback_end or _utc_now()
    wall_end = wall_end or end.get("observedAt") or fallback_end or wall_start
    media_start = start.get("positionMs") if isinstance(start.get("positionMs"), (int, float)) else 0
    media_end = end.get("positionMs") if isinstance(end.get("positionMs"), (int, float)) else 0
    if not start or not end:
        reasons.append("recording_snapshot_missing")
    else:
        for field in ("recordingId", "sessionId", "recordingPath", "outputPath"):
            if start.get(field) != end.get(field) or not start.get(field):
                reasons.append("recording_identity_or_path_changed")
                break
    if start.get("monitorFresh") is not True or end.get("monitorFresh") is not True:
        reasons.append("monitor_timing_not_fresh")
    if start.get("obsFresh") is not True or end.get("obsFresh") is not True:
        reasons.append("obs_timing_not_fresh")
    if not timestamp_fresh(end.get("observedAt")) or not timestamp_fresh(end.get("monitorObservedAt")) \
            or not timestamp_fresh(end.get("obsObservedAt")) or not timestamp_fresh(end.get("durationObservedAt")):
        reasons.append("end_snapshot_stale_or_missing")
    if not exact_timing_sample(start) or not exact_timing_sample(end):
        reasons.append("boundary_timing_sample_missing")
    if start.get("paused") is True or end.get("paused") is True:
        reasons.append("recording_paused_at_boundary")
    if start.get("durationRollback") is True or end.get("durationRollback") is True:
        reasons.append("duration_rollback")
    if end.get("quarantined") is True or start.get("quarantined") is True:
        reasons.append("recording_quarantined")
    if media_end < media_start:
        reasons.append("media_position_regressed")
    try:
        wall_start_time = datetime.fromisoformat(str(wall_start).replace("Z", "+00:00"))
        wall_end_time = datetime.fromisoformat(str(wall_end).replace("Z", "+00:00"))
        if wall_start_time.tzinfo is None or wall_end_time.tzinfo is None:
            reasons.append("action_wall_boundaries_invalid")
        elif wall_end_time < wall_start_time:
            reasons.append("action_wall_boundaries_reversed")
    except (ValueError, TypeError):
        reasons.append("action_wall_boundaries_invalid")
    samples = end.get("timingSamples")
    if isinstance(samples, list):
        prior_duration = None
        for sample in samples:
            if not isinstance(sample, dict):
                continue
            observed = sample.get("observedAt")
            if isinstance(observed, str) and (observed < wall_start or observed > wall_end):
                continue
            duration = sample.get("durationMs")
            if sample.get("paused") is True:
                reasons.append("recording_paused_within_interval")
            if sample.get("recordingActive") is False:
                reasons.append("recording_inactive_within_interval")
            if isinstance(duration, (int, float)):
                if prior_duration is not None and duration < prior_duration:
                    reasons.append("duration_rollback_within_interval")
                prior_duration = duration
    else:
        reasons.append("timing_samples_unavailable")
    return {"wallStartedAt": wall_start, "wallEndedAt": wall_end,
            "mediaStartMs": max(0, int(media_start)), "mediaEndMs": max(0, int(media_end)),
            "uncertain": bool(reasons), "uncertaintyReasons": sorted(set(reasons))}


class ProductionEventSink:
    """Bounded asynchronous atomic-file sink; failures are observable, never fatal."""

    def __init__(self, state_dir: Path, *, queue_size: int = DEFAULT_QUEUE_SIZE) -> None:
        self.directory = Path(state_dir) / "production" / "events"
        self._queue: queue.Queue[tuple[dict[str, Any], str | None] | None] = queue.Queue(maxsize=max(1, int(queue_size)))
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._closed = False
        self._accepted = self._written = self._duplicates = self._dropped = self._failed = 0
        self._retried = 0
        self._excluded = 0
        self._last_error: str | None = None

    def emit(self, event: dict[str, Any]) -> bool:
        return self._enqueue(event, None)

    def emit_excluded(self, event: dict[str, Any], reason: str) -> bool:
        """Retain exclusion provenance outside the worker's intake directory."""
        return self._enqueue(event, str(reason)[:120])

    def _enqueue(self, event: dict[str, Any], exclusion_reason: str | None) -> bool:
        try:
            payload = _canonical(event)
            if len(payload) > MAX_EVENT_BYTES:
                raise ValueError("event byte limit")
            if event.get("schemaVersion") != 1 or not event.get("eventId"):
                raise ValueError("invalid production event envelope")
            with self._lock:
                if self._closed:
                    self._dropped += 1
                    return False
                if self._thread is None:
                    self._thread = threading.Thread(target=self._run, name="ff7r-production-events", daemon=True)
                    self._thread.start()
            self._queue.put_nowait((event, exclusion_reason))
            with self._lock:
                self._accepted += 1
                if exclusion_reason:
                    self._excluded += 1
            return True
        except queue.Full:
            with self._lock:
                self._dropped += 1
            return False
        except Exception as exc:
            with self._lock:
                self._failed += 1
                self._last_error = f"{type(exc).__name__}: {exc}"
            return False

    def _write_once(self, event: dict[str, Any], exclusion_reason: str | None = None) -> None:
        destination = self.directory.parent / "excluded-events" if exclusion_reason else self.directory
        document = ({"excluded": True, "reason": exclusion_reason, "event": event}
                    if exclusion_reason else event)
        payload = _canonical(document) + b"\n"
        target = destination / (hashlib.sha256(str(event["eventId"]).encode("utf-8")).hexdigest() + ".json")
        destination.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if target.read_bytes() == payload:
                with self._lock:
                    self._duplicates += 1
                return
            raise FileExistsError("event id already exists with different content")
        temporary = destination / ("." + uuid.uuid4().hex + ".tmp")
        created = True
        try:
            with temporary.open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, target)
            except FileExistsError:
                if target.read_bytes() != payload:
                    raise
                with self._lock:
                    self._duplicates += 1
                created = False
            finally:
                temporary.unlink(missing_ok=True)
        finally:
            temporary.unlink(missing_ok=True)
        if created:
            with self._lock:
                self._written += 1

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                event, exclusion_reason = item
                for attempt in range(3):
                    try:
                        self._write_once(event, exclusion_reason)
                        break
                    except Exception:
                        if attempt == 2:
                            raise
                        with self._lock:
                            self._retried += 1
                        time.sleep(0.02 * (attempt + 1))
            except Exception as exc:
                with self._lock:
                    self._failed += 1
                    self._last_error = f"{type(exc).__name__}: {exc}"
            finally:
                self._queue.task_done()

    def flush(self, timeout: float = 1.0) -> bool:
        """Wait a bounded time for queued local writes (used by CLI shutdown)."""
        deadline = time.monotonic() + max(0.0, timeout)
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.005)
        return self._queue.unfinished_tasks == 0

    def close(self, timeout: float = 1.0) -> None:
        with self._lock:
            self._closed = True
            thread = self._thread
        if thread is None:
            return
        self.flush(timeout)
        try:
            self._queue.put(None, timeout=max(0.0, timeout))
        except queue.Full:
            # A full queue is still bounded and the worker is a daemon.
            pass
        thread.join(max(0.0, timeout))

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {"accepted": self._accepted, "written": self._written, "duplicates": self._duplicates,
                    "dropped": self._dropped, "failed": self._failed, "queued": self._queue.qsize(),
                    "retried": self._retried, "excluded": self._excluded, "last_error": self._last_error}


def feature_test_context(value: Any) -> dict[str, Any] | None:
    """Accept only the explicit, small context needed to describe a dev test."""
    if not isinstance(value, dict):
        return None
    required = ("feature", "featureVersion", "behaviorIdentity", "testObjective", "expectedBehavior")
    if any(not isinstance(value.get(key), str) or not value[key].strip() for key in required):
        return None
    context = {key: value[key].strip() for key in required} | {
        key: value[key].strip() for key in ("issue", "pullRequest")
        if isinstance(value.get(key), str) and value[key].strip()
    } | {
        key: value[key].strip() for key in ("beforeEventId", "afterEventId")
        if isinstance(value.get(key), str) and value[key].strip()
    }
    if "expectTalkPrompt" in value:
        if type(value["expectTalkPrompt"]) is not bool:
            return None
        context["expectTalkPrompt"] = value["expectTalkPrompt"]
    return context


def state_dir_from_env() -> Path | None:
    configured = os.getenv("FF7R_PRODUCTION_STATE_DIR")
    if configured:
        return Path(configured)
    event_dir = os.getenv("FF7R_PUBLIC_STREAM_EVENT_DIR")
    return Path(event_dir).parent if event_dir else None


def feature_validation_runtime() -> bool:
    return os.getenv("FF7R_RUNTIME_PROFILE", "").replace("-", "").replace("_", "").upper() == "FEATUREVALIDATION"


def emit_development_outcome(*, state_dir: Path | None, context: Any, report: dict[str, Any],
                             event_type: str, start_snapshot: dict[str, Any] | None = None,
                             end_snapshot: dict[str, Any] | None = None,
                             verified_result_override: str | None = None) -> dict[str, Any] | None:
    """Persist a CLI mission result after its normal harness verification path."""
    context = feature_test_context(context)
    if context is None:
        return None
    started = report.get("started_at_utc") or _utc_now()
    result = "unknown"
    if event_type == "guide_moogle_travel_outcome":
        if report.get("ok") is False and report.get("reason"):
            result = "failed"
        else:
            result = "verified" if verified_guide_moogle_arrival(report) else "unknown"
    elif verified_result_override in {"verified", "failed", "unknown"}:
        result = verified_result_override
    elif report.get("ok") is True and report.get("state") in {"ARRIVED", "talk_offered"}:
        if report.get("dry_run") is False or report.get("execute") is True:
            route_verified = (report.get("state") == "talk_offered"
                              or (report.get("route_state") == "ARRIVED"
                                  and report.get("movement_attempted") is True))
            if route_verified:
                result = "verified"
    elif report.get("ok") is False:
        result = "failed" if report.get("reason") or report.get("state") in {"STOPPED", "stopped"} else "unknown"
    observed = report.get("observed_behavior") or report.get("reason") or report.get("state") or "outcome not verified"
    runtime = report.get("runtime_identity") if isinstance(report.get("runtime_identity"), dict) else {}
    driver = None
    if state_dir is not None:
        try:
            from .public_stream_events import match_record_target
            driver = match_record_target(Path(state_dir))[0]
        except Exception:
            driver = None
    attribution = {}
    if isinstance(driver, dict):
        if isinstance(driver.get("model_id"), str):
            attribution["model"] = driver["model_id"]
        if isinstance(driver.get("effort"), str):
            attribution["effort"] = driver["effort"]
    output_dir = report.get("output_directory")
    safe_context = {key: value for key, value in context.items() if key != "expectTalkPrompt"}
    evidence_refs = report.get("evidence_refs")
    if not isinstance(evidence_refs, list):
        evidence_refs = [str(Path(output_dir) / name) for name in ("result.json", "sha256.json")] if output_dir else []
    details_fields = {**safe_context, **attribution,
        "observedBehavior": str(observed)[:1200],
        "evidenceRefs": [str(value) for value in evidence_refs if isinstance(value, (str, Path))],
        "result": result}
    commit = runtime.get("git_commit_sha")
    if isinstance(commit, str) and commit:
        details_fields["commit"] = commit
    details = {"development": details_fields}
    return build_event(state_dir=state_dir, mode="development", event_type=event_type,
                       source_key=started + ":" + context["featureVersion"] + ":" + context["behaviorIdentity"],
                       details=details, occurred_at=report.get("ended_at_utc") or _utc_now(),
                       start_snapshot=start_snapshot, end_snapshot=end_snapshot,
                       interval_wall_start=report.get("started_at_utc"),
                       interval_wall_end=report.get("ended_at_utc"))


def verified_guide_moogle_arrival(report: dict[str, Any]) -> bool:
    """Accept only the nested menu, observed arrival and settle proof from the ride."""
    if (not isinstance(report, dict) or report.get("ok") is not True
            or report.get("state") != "arrived"):
        return False
    menu = report.get("menu")
    proof = report.get("guide_moogle_proof")
    if not isinstance(menu, dict) or not isinstance(proof, dict):
        return False
    if (menu.get("ok") is not True or menu.get("state") != "travelling"
            or not isinstance(menu.get("destination"), str)
            or not isinstance(menu.get("destination_key"), str)
            or menu.get("destination") != report.get("destination")
            or not isinstance(menu.get("arrival"), dict)):
        return False
    try:
        from .guide_moogle import GOLD_SAUCER_FIELD, zone as guide_moogle_zone
        destination = guide_moogle_zone(report["destination"])
    except Exception:
        return False
    if (menu.get("destination") != destination.name
            or menu.get("destination_key") != destination.key
            or menu.get("arrival") != dict(zip(("x", "y", "z"), destination.arrival))):
        return False
    admission = menu.get("talk_admission")
    if (not isinstance(admission, dict) or admission.get("ok") is not True
            or not isinstance(admission.get("actor_id"), str)
            or not isinstance(admission.get("target"), dict)
            or not str(admission["target"].get("canonical_id", "")).startswith(
                f"field_object:{GOLD_SAUCER_FIELD}:oba2350_guideMoogle_")
            or admission.get("actor_id") != admission["target"].get("actor_id")):
        return False
    if proof.get("settled") is not True or proof.get("arrival_observed") is not True:
        return False
    expected, observed = menu["arrival"], proof.get("arrival_position")
    if (not isinstance(observed, dict) or report.get("arrival_position") != observed):
        return False
    try:
        raw_values = [expected[key] for key in ("x", "y", "z")] + [observed[key] for key in ("x", "y", "z")]
        if any(type(value) not in (int, float) for value in raw_values):
            return False
        values = [float(value) for value in raw_values]
    except (KeyError, TypeError, ValueError):
        return False
    if any(not math.isfinite(value) for value in values):
        return False
    distance_m = (((values[0] - values[3]) ** 2 + (values[1] - values[4]) ** 2) ** 0.5) / 100.0
    vertical_m = abs(values[2] - values[5]) / 100.0
    return distance_m <= 1.5 and vertical_m <= 1.5


def emit_development_report(*, state_dir: Path | None, context_path: Path,
                            report: dict[str, Any], event_type: str,
                            start_snapshot: dict[str, Any] | None,
                            end_snapshot: dict[str, Any] | None) -> dict[str, Any]:
    """CLI bridge: validate explicit context, then bounded flush before exit."""
    context = _read_json(Path(context_path))
    if feature_test_context(context) is not None and not feature_validation_runtime():
        return {"accepted": False, "reason": "feature_validation_runtime_required"}
    event = emit_development_outcome(state_dir=state_dir, context=context, report=report,
                                     event_type=event_type, start_snapshot=start_snapshot,
                                     end_snapshot=end_snapshot)
    if event is None:
        return {"accepted": False, "reason": "invalid_production_test_context"}
    if state_dir is None:
        return {"accepted": False, "reason": "production_state_directory_unavailable"}
    sink = ProductionEventSink(Path(state_dir), queue_size=2)
    if known_live_stream(event):
        excluded = sink.emit_excluded(event, "fresh_obs_stream_active")
        flushed = sink.flush(1.0) if excluded else False
        status = sink.status()
        sink.close(1.0)
        return {"accepted": False, "reason": "active_stream_excluded", "exclusionRecorded": excluded,
                "eventId": event["eventId"],
                "result": event["development"]["result"], "intervalUncertain": event.get("interval", {}).get("uncertain", True),
                "exclusionFlushed": flushed, "status": status}
    accepted = sink.emit(event)
    flushed = sink.flush(1.0) if accepted else False
    status = sink.status()
    sink.close(1.0)
    return {"accepted": accepted and flushed, "eventId": event["eventId"],
            "result": event["development"]["result"],
            "intervalUncertain": event.get("interval", {}).get("uncertain", True),
            "status": status}
