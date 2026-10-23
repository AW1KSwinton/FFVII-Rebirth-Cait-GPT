"""Bounded public gameplay event intake for the independent live narrator."""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .public_stream_events import PUBLIC_KINDS, _timestamp, rehearsal_is_live, stream_is_live

MAX_EVENT_BYTES = 8 * 1024
QUEUE_SIZE = 32
STREAM_MAX_AGE_SECONDS = 60.0
REHEARSAL_LEASE_MAX_AGE_SECONDS = 6.0
REHEARSAL_EVIDENCE_MAX_AGE_SECONDS = 15.0
SESSION_MODES = {"live", "rehearsal"}
NARRATED_FACTS_KINDS = PUBLIC_KINDS - {"queens_blood_turn"}
EVENT_KINDS = NARRATED_FACTS_KINDS | {"queens_blood_exchange"}
FACT_RE = re.compile(r"^[A-Za-z0-9 _.,:=+\-]{1,180}$")
RECORDING_ID_RE = re.compile(r"^[a-f0-9]{64}$")
QB_CUE_WORDS = {
    "opponent_played": "opponent_card_played",
    "player_card_lost": "player_card_lost",
    "tiles_taken": "tiles_taken",
    "lane_lead_changed": "lane_lead_changed",
    "lane_swing": "lane_swing",
    "close_lane": "close_lane",
    "match_lead_changed": "match_lead_changed",
    "opponent_passed": "opponent_passed",
}


def _read_object(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _worker_policy(state_dir: Path) -> dict[str, Any] | None:
    policy = _read_object(Path(state_dir) / "live-narrator" / "policy.json")
    required = {"version", "mode", "enabled"}
    if (not policy or policy.get("version") != 1 or policy.get("mode") != "worker"
            or policy.get("enabled") is not True or not required.issubset(policy)
            or set(policy) - (required | {"changedAt", "sessionMode"})):
        return None
    if "changedAt" in policy and _timestamp(policy.get("changedAt")) is None:
        return None
    if policy.get("sessionMode", "live") not in SESSION_MODES:
        return None
    return policy


def worker_policy_enabled(state_dir: Path) -> bool:
    return _worker_policy(state_dir) is not None


def requested_session_mode(state_dir: Path) -> str | None:
    """Require worker policy and explicit process mode to agree; default is live."""
    policy = _worker_policy(state_dir)
    configured = os.environ.get("LIVE_NARRATOR_SESSION_MODE", "live")
    if policy is None or configured not in SESSION_MODES:
        return None
    policy_mode = policy.get("sessionMode", "live")
    return configured if configured == policy_mode else None


def stream_identity(state_dir: Path, *, now: float | None = None) -> int | None:
    """Return stream start in epoch milliseconds only from a fresh strict marker."""
    state_dir = Path(state_dir)
    live = _read_object(state_dir / "public-stream-live.json")
    if (not live or set(live) != {"version", "streamStartedAt", "at"}
            or live.get("version") != 1 or not stream_is_live(state_dir, now)):
        return None
    started = _timestamp(live.get("streamStartedAt"))
    at = _timestamp(live.get("at"))
    current = time.time() if now is None else now
    if (started is None or at is None or started > current + 10
            or not -10 <= current - at <= STREAM_MAX_AGE_SECONDS):
        return None
    return int(started * 1000)


def narrated_rehearsal_enabled(state_dir: Path, *, now: float | None = None) -> bool:
    """A fresh rehearsal speech-consumer lease is the explicit narration opt-in."""
    state_dir = Path(state_dir)
    if stream_is_live(state_dir, now) or not rehearsal_is_live(state_dir, now):
        return False
    lease = _read_object(state_dir / "speech-consumer-rehearsal.json")
    heartbeat = _read_object(state_dir / "public-stream-rehearsal.json")
    required = {"version", "mode", "at", "available", "busy", "speech_ready", "consecutive_failures", "failure_limit",
                "recordingId", "instance_id"}
    if (not lease or not required.issubset(lease) or lease.get("version") != 1
            or lease.get("mode") != "rehearsal" or lease.get("available") is not True
            or type(lease.get("busy")) is not bool or lease.get("speech_ready") is not True
            or type(lease.get("consecutive_failures")) is not int or lease["consecutive_failures"] >= 2
            or type(lease.get("failure_limit")) is not int or lease.get("failure_limit") != 2
            or not isinstance(lease.get("recordingId"), str)
            or not RECORDING_ID_RE.fullmatch(lease["recordingId"])
            or not isinstance(lease.get("instance_id"), str) or not lease["instance_id"]
            or not heartbeat or heartbeat.get("version") != 2
            or lease["recordingId"] != heartbeat.get("recordingId")):
        return False
    at = _timestamp(lease.get("at"))
    current = time.time() if now is None else now
    if at is None or not -1 <= current - at <= REHEARSAL_LEASE_MAX_AGE_SECONDS:
        return False
    owner = _read_object(state_dir / "speech-consumer-rehearsal.json.owner.json")
    return bool(owner and owner.get("version") == 1 and owner.get("mode") == "rehearsal"
                and owner.get("instance_id") == lease["instance_id"])


def _fresh_inactive_obs_snapshot(state_dir: Path, now: float) -> bool:
    snapshot = _read_object(state_dir / "production" / "stream-state.json")
    if (not snapshot or snapshot.get("monitorFresh") is not True or snapshot.get("obsFresh") is not True
            or snapshot.get("recordingActive") is not True):
        return False
    stream = snapshot.get("stream")
    if not isinstance(stream, dict) or stream.get("state") != "inactive" or stream.get("source") != "obs":
        return False
    stamps = (snapshot.get("observedAt"), snapshot.get("monitorObservedAt"), snapshot.get("obsObservedAt"), stream.get("observedAt"))
    return all((stamp := _timestamp(value)) is not None and -10 <= now - stamp <= REHEARSAL_EVIDENCE_MAX_AGE_SECONDS
               for value in stamps)


def narration_session_identity(state_dir: Path, *, now: float | None = None) -> dict[str, Any] | None:
    """Return authoritative live or explicitly narrated-rehearsal identity."""
    state_dir = Path(state_dir)
    mode = requested_session_mode(state_dir)
    if mode is None:
        return None
    if mode == "live":
        started = stream_identity(state_dir, now=now)
        return {"sessionMode": "live", "streamStartedAt": started} if started is not None else None
    current = time.time() if now is None else now
    heartbeat = _read_object(state_dir / "public-stream-rehearsal.json")
    heartbeat_at = _timestamp(heartbeat.get("at")) if heartbeat else None
    if (heartbeat is None or heartbeat.get("version") != 2
            or set(heartbeat) != {"version", "at", "recordingStartedAt", "recordingId"}
            or not rehearsal_is_live(state_dir, now)
            or heartbeat_at is None or not -10 <= current - heartbeat_at <= REHEARSAL_EVIDENCE_MAX_AGE_SECONDS
            or not _fresh_inactive_obs_snapshot(state_dir, current)
            or not narrated_rehearsal_enabled(state_dir, now=now)):
        return None
    recording_id = heartbeat.get("recordingId")
    recording_started_at = heartbeat.get("recordingStartedAt")
    started_seconds = _timestamp(recording_started_at)
    if (not isinstance(recording_id, str) or not RECORDING_ID_RE.fullmatch(recording_id)
            or started_seconds is None or started_seconds > current + 10):
        return None
    return {"sessionMode": "rehearsal", "recordingId": recording_id,
            "recordingStartedAt": recording_started_at,
            "streamStartedAt": int(started_seconds * 1000)}


def offline_recording_context(state_dir: Path, *, now: float | None = None) -> bool:
    root = Path(state_dir)
    context = _read_object(root / "recording-context.json")
    required = {"version", "mode", "at", "instance_id", "available", "busy", "speech_ready",
                "consecutive_failures", "failure_limit", "recordingId"}
    if (not context or not required.issubset(context) or set(context) - (required | {"previous_delivery"})
            or context.get("version") != 1 or context.get("mode") != "rehearsal"
            or context.get("available") is not True or context.get("busy") is not False
            or context.get("speech_ready") is not False or type(context.get("consecutive_failures")) is not int
            or context.get("consecutive_failures") != 0 or type(context.get("failure_limit")) is not int
            or context.get("failure_limit") != 2 or not isinstance(context.get("instance_id"), str)
            or not context["instance_id"] or not isinstance(context.get("recordingId"), str)
            or not context["recordingId"]):
        return False
    rehearsal = _read_object(root / "public-stream-rehearsal.json")
    if (not rehearsal_is_live(root, now) or not rehearsal
            or rehearsal.get("version") != 2
            or not isinstance(rehearsal.get("recordingId"), str)
            or context.get("recordingId") != rehearsal.get("recordingId")):
        return False
    at = _timestamp(context.get("at"))
    current = time.time() if now is None else now
    owner = _read_object(root / "recording-context.json.owner.json")
    owns = bool(owner and owner.get("version") == 1 and owner.get("mode") == "rehearsal"
                and owner.get("instance_id") == context["instance_id"])
    return bool(owns and at is not None and -1 <= current - at <= 6)


def narrated_rehearsal_lease_present(state_dir: Path) -> bool:
    root = Path(state_dir)
    lease = root / "speech-consumer-rehearsal.json"
    owner_path = lease.with_name(lease.name + ".owner.json")
    if owner_path.exists():
        # Any current ownership claim, even malformed, keeps the safety gate
        # closed until the consumer can be reviewed or removed.
        return True
    snapshot = _read_object(lease)
    if not snapshot or snapshot.get("version") != 1 or snapshot.get("mode") != "rehearsal":
        return False
    at = _timestamp(snapshot.get("at"))
    current = time.time()
    return at is not None and -1 <= current - at <= 6


def action_facts(kind: str, values: dict[str, Any]) -> list[str]:
    """Extract only fixed labels and a verified integer Queen's Blood score."""
    if kind in {"queens_blood_won", "queens_blood_lost", "queens_blood_draw"}:
        score = values.get("score")
        if (isinstance(score, dict) and type(score.get("player")) is int
                and type(score.get("opponent")) is int and 0 <= score["player"] <= 999
                and 0 <= score["opponent"] <= 999):
            return [f"score:{score['player']}-{score['opponent']}"]
    return []


def guide_moogle_arrival_facts(report: dict[str, Any]) -> list[str]:
    """Expose one public route fact only after the nested arrival was verified."""
    from .production_events import verified_guide_moogle_arrival

    if not verified_guide_moogle_arrival(report):
        return []
    destination = report.get("destination")
    if not isinstance(destination, str) or not destination or len(destination) > 48:
        return []
    menu = report.get("menu") or {}
    origin = menu.get("from") if isinstance(menu, dict) else None
    if not isinstance(origin, str) or len(origin) > 48:
        return []
    try:
        from .guide_moogle import zone as guide_moogle_zone
        if guide_moogle_zone(origin).name != origin or guide_moogle_zone(destination).name != destination:
            return []
    except Exception:
        return []
    return [f"Guide Moogle transport connects {origin} and {destination}"]


def queens_blood_exchange_facts(cues: list[dict[str, Any]], projection: dict[str, Any] | None = None,
                                projection_change: Any = None) -> list[str]:
    """Map validated board-commentary cues to small, nonverbal public facts."""
    facts: list[str] = []
    lanes = {"top", "middle", "bottom"}
    leaders = {"player", "opponent", "tied"}
    for cue in cues:
        if not isinstance(cue, dict) or cue.get("kind") not in QB_CUE_WORDS:
            continue
        kind = cue["kind"]
        fact = QB_CUE_WORDS[kind]
        lane = cue.get("lane")
        if lane in lanes and kind in {"lane_lead_changed", "lane_swing", "close_lane"}:
            fact += ":" + lane
        if kind == "lane_lead_changed":
            source, target = cue.get("from"), cue.get("to")
            if source in leaders and target in leaders:
                fact += f":{source}-to-{target}"
        elif kind == "lane_swing":
            swing = cue.get("swing")
            if type(swing) is int and abs(swing) <= 999:
                fact += f":{swing:+d}"
        elif kind == "tiles_taken":
            count = cue.get("count")
            if type(count) is int and 0 < count <= 9:
                fact += f":{count}"
        elif kind == "match_lead_changed":
            source, target = cue.get("from"), cue.get("to")
            if source in leaders and target in leaders:
                fact += f":{source}-to-{target}"
        if fact not in facts:
            facts.append(fact)
        if len(facts) >= 10:
            break
    if isinstance(projection, dict):
        player, opponent = projection.get("player_score"), projection.get("opponent_score")
        if (type(player) is int and type(opponent) is int
                and 0 <= player <= 999 and 0 <= opponent <= 999):
            facts.append(f"projected_score:{player}-{opponent}")
    if type(projection_change) in {int, float} and abs(projection_change) <= 999:
        facts.append(f"projection_change:{projection_change:+g}")
    return facts[:12]


def _validate_facts(facts: list[str] | tuple[str, ...]) -> list[str] | None:
    if not isinstance(facts, (list, tuple)) or len(facts) > 12:
        return None
    clean: list[str] = []
    for fact in facts:
        if not isinstance(fact, str):
            return None
        fact = " ".join(fact.split())
        if (not FACT_RE.fullmatch(fact) or "/" in fact or "\\" in fact
                or ".." in fact or "http" in fact.lower()):
            return None
        clean.append(fact)
    return clean


class LiveNarrationEventSink:
    """Small bounded async spool; game actions never wait for narrator I/O."""

    def __init__(self, state_dir: Path, *, queue_size: int = QUEUE_SIZE) -> None:
        self.state_dir = Path(state_dir)
        self.directory = self.state_dir / "live-narrator" / "events"
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=max(1, int(queue_size)))
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._closed = False
        self._accepted = self._written = self._duplicates = self._dropped = self._failed = 0
        self._retried = 0
        self._last_error: str | None = None
        self._queued_ids: set[str] = set()

    def emit(self, kind: str, facts: list[str] | tuple[str, ...] = (), *, source_key: str) -> bool:
        if kind not in EVENT_KINDS:
            return False
        identity = narration_session_identity(self.state_dir)
        clean_facts = _validate_facts(facts)
        if identity is None or clean_facts is None:
            return False
        started = identity["streamStartedAt"]
        if type(started) is not int:
            return False
        event_id = hashlib.sha256("\0".join((identity["sessionMode"], str(identity.get("recordingId", "")),
                                             str(started), kind, source_key,
                                             json.dumps(clean_facts, separators=(",", ":")))).encode("utf-8")).hexdigest()
        event = {"version": 1, "eventId": event_id,
                 "at": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                 "streamStartedAt": started, "kind": kind, "facts": clean_facts}
        if identity["sessionMode"] == "rehearsal":
            event.update(sessionMode="rehearsal", recordingId=identity["recordingId"],
                         recordingStartedAt=identity["recordingStartedAt"])
        payload = json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        if len(payload) > MAX_EVENT_BYTES:
            self._count("failed", "event byte limit")
            return False
        with self._lock:
            if self._closed:
                self._dropped += 1
                return False
            if event_id in self._queued_ids:
                self._duplicates += 1
                return True
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="ff7r-live-narration-events", daemon=True)
                self._thread.start()
            self._queued_ids.add(event_id)
            try:
                self._queue.put_nowait(event)
            except queue.Full:
                self._queued_ids.discard(event_id)
                self._dropped += 1
                return False
            self._accepted += 1
        return True

    def _count(self, key: str, error: str | None = None) -> None:
        with self._lock:
            setattr(self, "_" + key, getattr(self, "_" + key) + 1)
            if error:
                self._last_error = error

    def _write(self, event: dict[str, Any]) -> None:
        target = self.directory / (hashlib.sha256(event["eventId"].encode("utf-8")).hexdigest() + ".json")
        payload = json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8") + b"\n"
        self.directory.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if target.read_bytes() == payload:
                self._count("duplicates")
                return
            raise FileExistsError("event id already exists with different content")
        temporary = self.directory / ("." + uuid.uuid4().hex + ".tmp")
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
                self._count("duplicates")
            else:
                self._count("written")
        finally:
            temporary.unlink(missing_ok=True)

    def _run(self) -> None:
        while True:
            event = self._queue.get()
            try:
                if event is None:
                    return
                for attempt in range(3):
                    try:
                        self._write(event)
                        break
                    except Exception as exc:
                        if attempt == 2:
                            self._count("failed", f"{type(exc).__name__}: {exc}")
                        else:
                            self._count("retried")
                            time.sleep(0.02 * (attempt + 1))
            finally:
                if event is not None:
                    with self._lock:
                        self._queued_ids.discard(event["eventId"])
                self._queue.task_done()

    def flush(self, timeout: float = 2.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.005)
        return self._queue.unfinished_tasks == 0

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {"enabled": narration_session_identity(self.state_dir) is not None,
                    "accepted": self._accepted, "written": self._written, "duplicates": self._duplicates,
                    "dropped": self._dropped, "failed": self._failed, "retried": self._retried,
                    "queued": self._queue.qsize()}

    def close(self, timeout: float = 2.0) -> None:
        self.flush(timeout)
        with self._lock:
            if self._closed:
                return
            self._closed = True
            thread = self._thread
        if thread is not None:
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                return
            thread.join(timeout=max(0.0, timeout))
