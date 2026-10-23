"""Explicit gameplay commentary, independent of the game input worker."""

from __future__ import annotations

import json
import math
import os
import uuid
from datetime import datetime
from contextlib import contextmanager
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .public_stream_events import (
    AgentSpeechLimiter, agent_speech_problem, rehearsal_is_live,
    speech_tone_problem, stream_is_live, write_agent_speech,
    AGENT_SAY_WINDOW_SECONDS,
)


GAMEPLAY_NARRATION_INSTRUCTIONS = """During a narrated gameplay session, use
say_on_stream before each new gameplay action or decision: travel, navigation,
NPC conversations, menus, shopping, rest, combat, and Queen's Blood. Give a
short, natural public explanation of the plan, an observation, or a reaction,
like a player streaming the game. One line can cover the repeated inputs inside
a bounded route or menu action; do not narrate every controller pulse or poll.
React to verified outcomes and explain changes of plan. Never claim arrival,
rewards, a conversation or a win before observing it. While an async job runs,
say_on_stream and its matching status poll are allowed, but no second game input.
Inspect the nested speech result: ok means queued, not heard. On rate_limited,
wait retry_after_seconds and recheck the situation before retrying; rephrase
repetitive lines. Inspect previous_delivery too: a failed, dropped or skipped
previous line is a speech failure even when the new line was queued. On not_live,
speech_delivery_failing, speech_runtime_unavailable or another speech failure,
pause new gameplay
input and report privately; do not interrupt an already running job just to
speak. Emergency stop/release is immediate, without waiting for narration.
Prefer the dedicated say_on_stream tool: its tone choices are restricted to
neutral, calm, confident, excited, tense, concerned, relieved, surprised,
frustrated or amused; omit tone for neutral. With legacy agent_call, inspect
result.ok, not the outer dispatch ok. Say one distinct line per decision from
the latest observed board and hand, including every Queen's Blood placement/pass
and deck/mulligan decision; do not submit near-duplicate narration for one
pending move. After invalid_tone or repetitive, do not move or reuse an old
receipt: re-observe and compose a genuinely different line with a valid tone.
React to the match result with the observed reaction tone. Speak only
visible gameplay and brief decision summaries, never development, code, tools,
harness, services, telemetry, debugging, raw responses, paths, credentials,
private reasoning or unseen story. Require say_on_stream and its accepted
receipt only during an actual live stream in manual narration mode, or an
explicitly narrated rehearsal with a fresh rehearsal heartbeat and its
speech-consumer lease. Offline gameplay and silent recordings never require
pre-input narration. In live worker mode, do not request or author synchronous
lines: the independent live narrator consumes bounded public gameplay events,
and its absence never falls back to the playing model. Fixed event labels do
not supply voice text.
"""


class AgentSpeechPublisher:
    """Validate and queue voice-only copy without touching any game controller.

    The stable supervisor owns this during both idle and busy gameplay. Its
    pacing history is shared on disk across workers and supervisor restarts.
    The direct service uses the
    same implementation for legacy callers. Chat is never read here.
    """

    def __init__(self, directory: Path | None, *, monotonic: Callable[[], float] | None = None,
                 receipt_context: Callable[[], dict[str, Any] | None] | None = None) -> None:
        self._directory = directory
        self._receipt_context = receipt_context
        # Persist wall-clock timestamps across process restarts and machine boots.
        self._clock = monotonic or time.time
        self._limiter = AgentSpeechLimiter()
        self._lock = threading.Lock()

    def say(self, text: str, tone: str | None = None) -> dict[str, Any]:
        directory = self._directory
        if directory is None:
            return {"ok": False, "reason": "stream_events_disabled", "input_sent": False}
        with self._lock:
            try:
                # Every publisher for this spool shares the gate, including a
                # legacy service in another worker and a restarted supervisor.
                with self._shared_gate(directory) as acquired:
                    if not acquired:
                        return {"ok": False, "reason": "rate_limited",
                                "detail": "another publisher is queuing a line",
                                "retry_after_seconds": 1, "input_sent": False}
                    from .queen_blood_speech_receipt import runtime_binding, publish
                    # Revoke an older pending line before this attempt can queue.
                    # A later private-write failure can never expose the old receipt.
                    publish(directory, text, tone, {"ok": False}, None, self._clock())
                    problem = agent_speech_problem(text)
                    if problem is not None:
                        from .queen_blood_speech_receipt import publish
                        publish(directory, text, tone, {"ok": False}, None, self._clock())
                        return {"ok": False, "reason": "invalid_text", "detail": problem, "input_sent": False}
                    problem = speech_tone_problem(tone)
                    if problem is not None:
                        from .queen_blood_speech_receipt import publish
                        publish(directory, text, tone, {"ok": False}, None, self._clock())
                        return {"ok": False, "reason": "invalid_tone", "detail": problem, "input_sent": False}
                    from .queen_blood_speech_receipt import runtime_binding, publish
                    provider = self._receipt_context or (lambda: runtime_binding(directory))
                    try:
                        before = provider()
                    except (OSError, ValueError, TypeError):
                        before = None
                    result = self._say_locked(directory, text, tone)
                    try:
                        after = provider() if result.get("ok") is True else None
                    except (OSError, ValueError, TypeError):
                        after = None
                    context = before if before == after else None
                    try:
                        receipt_id = publish(directory, text, tone, result, context, self._clock())
                    except OSError:
                        result["receipt_error"] = "private_receipt_unavailable"
                        return result
                    if receipt_id is not None:
                        result["receipt_id"] = receipt_id
                    return result
            except (OSError, ValueError, TypeError):
                return {"ok": False, "reason": "spool_unavailable", "input_sent": False}

    @contextmanager
    def _shared_gate(self, directory: Path):
        directory.parent.mkdir(parents=True, exist_ok=True)
        # Keep private pacing state outside the public JSON event spool.
        with (directory.parent / ("." + directory.name + "-speech.lock")).open("a+b") as gate:
            if os.name == "nt":
                import msvcrt
                if gate.seek(0, os.SEEK_END) == 0:
                    gate.write(b"0")
                    gate.flush()
                gate.seek(0)
                try:
                    msvcrt.locking(gate.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    yield False
                    return
            else:
                import fcntl
                try:
                    fcntl.flock(gate.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    yield False
                    return
            try:
                yield True
            finally:
                if os.name == "nt":
                    gate.seek(0)
                    msvcrt.locking(gate.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(gate.fileno(), fcntl.LOCK_UN)

    def _save_history(self, path: Path, history: list) -> None:
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                json.dump({"version": 1, "spoken": history}, stream)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _delivery_backpressure(self, directory: Path, rehearsal: bool, now: float) -> dict[str, Any] | None:
        mode = "rehearsal" if rehearsal else "live"
        path = directory.parent / f"speech-consumer-{mode}.json"
        try:
            lease = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            other_mode = "live" if rehearsal else "rehearsal"
            if (path.with_name(path.name + ".owner.json").exists()
                    or (directory.parent / f"speech-consumer-{other_mode}.json").exists()
                    or (directory.parent / f"speech-consumer-{other_mode}.json.owner.json").exists()):
                return {"ok": False, "reason": "speech_delivery_unavailable", "input_sent": False}
            return None  # Compatibility with a consumer predating admission leases.
        except (OSError, ValueError):
            return {"ok": False, "reason": "speech_delivery_unavailable", "input_sent": False}
        try:
            age = now - datetime.fromisoformat(lease["at"].replace("Z", "+00:00")).timestamp()
            valid = (lease.get("version") == 1 and lease.get("mode") == mode
                     and type(lease.get("busy")) is bool and type(lease.get("available")) is bool)
        except (KeyError, TypeError, ValueError, AttributeError):
            valid, age = False, 0
        if not valid or not -1 <= age <= 6:
            return {"ok": False, "reason": "speech_delivery_unavailable", "input_sent": False}
        # Updated consumers bind their renewable snapshot to an exclusive
        # instance claim. Older leases remain compatible without inventing one.
        if "instance_id" in lease or path.with_name(path.name + ".owner.json").exists():
            try:
                claim = json.loads(path.with_name(path.name + ".owner.json").read_text(encoding="utf-8"))
                owned = (isinstance(claim, dict) and isinstance(lease.get("instance_id"), str)
                         and bool(lease.get("instance_id")) and claim.get("version") == 1
                         and claim.get("mode") == mode and claim.get("instance_id") == lease["instance_id"])
            except (OSError, ValueError):
                owned = False
            if not owned:
                return {"ok": False, "reason": "speech_delivery_unavailable", "input_sent": False}
        delivery = {}
        if any(key in lease for key in ("speech_ready", "consecutive_failures", "previous_delivery", "failure_limit")):
            failures = lease.get("consecutive_failures")
            if (type(failures) is not int or failures < 0 or type(lease.get("speech_ready")) is not bool
                    or type(lease.get("failure_limit")) is not int or lease["failure_limit"] != 2):
                return {"ok": False, "reason": "speech_delivery_unavailable", "input_sent": False}
            delivery["consecutive_failures"] = failures
            previous = lease.get("previous_delivery")
            if previous is not None:
                try:
                    valid_previous = (isinstance(previous, dict) and previous.get("status") in
                                      ("media_completed", "failed", "dropped", "voice_disabled", "not_on_air")
                                      and all(isinstance(previous.get(key), str) for key in ("queued_at", "at")))
                    if not valid_previous:
                        raise ValueError("invalid previous delivery")
                    for key in ("queued_at", "at"):
                        datetime.fromisoformat(previous[key].replace("Z", "+00:00"))
                    error = previous.get("error")
                    if error is not None and (not isinstance(error, dict) or any(
                            not isinstance(error.get(key), str) or len(error[key]) > 300 for key in ("name", "message"))):
                        raise ValueError("invalid delivery error")
                    # Return only private delivery facts, never the speech text or a game receipt.
                    delivery["previous_delivery"] = {key: previous[key] for key in ("status", "queued_at", "at")}
                    if isinstance(previous.get("detail"), str):
                        delivery["previous_delivery"]["detail"] = previous["detail"][:300]
                    if error is not None:
                        delivery["previous_delivery"]["error"] = {key: error[key] for key in ("name", "message")}
                        if ((isinstance(error.get("code"), str) and len(error["code"]) <= 40)
                                or type(error.get("code")) is int):
                            delivery["previous_delivery"]["error"]["code"] = error["code"]
                except (TypeError, ValueError, AttributeError):
                    return {"ok": False, "reason": "speech_delivery_unavailable", "input_sent": False}
            if failures >= 2:
                return {"ok": False, "reason": "speech_delivery_failing", "input_sent": False, **delivery}
            if not lease["speech_ready"]:
                return {"ok": False, "reason": "speech_runtime_unavailable", "input_sent": False, **delivery}
        if not lease["available"]:
            return {"ok": False, "reason": "speech_delivery_unavailable", "input_sent": False, **delivery}
        busy = lease["busy"]
        if not busy:
            try:
                # No second accepted line while the first waits to be consumed.
                for item in directory.glob("*.json"):
                    try:
                        event = json.loads(item.read_text(encoding="utf-8"))
                    except FileNotFoundError:
                        continue
                    if isinstance(event, dict) and event.get("kind") == "agent_say":
                        busy = True
                        break
            except (OSError, ValueError):
                return {"ok": False, "reason": "speech_delivery_unavailable", "input_sent": False}
        if busy:
            return {"ok": False, "reason": "rate_limited", "detail": "speech delivery is busy",
                    "retry_after_seconds": 2, "input_sent": False, **delivery}
        return delivery or None

    def _say_locked(self, directory: Path, text: str, tone: str | None) -> dict[str, Any]:
        rehearsal = not stream_is_live(directory.parent)
        if rehearsal and not rehearsal_is_live(directory.parent):
            return {"ok": False, "reason": "not_live", "input_sent": False}
        now = self._clock()
        delivery = self._delivery_backpressure(directory, rehearsal, now)
        if delivery is not None and delivery.get("ok") is False:
            return delivery
        path = directory.parent / ("." + directory.name + "-speech-history.json")
        try:
            history = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            history = {"version": 1, "spoken": []}
        if not isinstance(history, dict) or set(history) != {"version", "spoken"} or history["version"] != 1:
            raise ValueError("invalid speech history")
        spoken = history["spoken"]
        if not isinstance(spoken, list) or any(
                not isinstance(entry, list) or len(entry) != 2
                or type(entry[0]) not in (int, float) or not math.isfinite(entry[0])
                or not isinstance(entry[1], str) for entry in spoken):
            raise ValueError("invalid speech history")
        # A clock rollback is conservative: do not clear future entries and
        # accidentally permit a burst. Advancing the clock naturally expires them.
        self._limiter._spoken = [(at, line) for at, line in spoken if now - at < AGENT_SAY_WINDOW_SECONDS]
        problem = self._limiter.problem(text, now)
        if problem is not None:
            repetitive = problem.startswith("repetitive") or problem.startswith("the same line")
            result = {"ok": False, "reason": "repetitive" if repetitive else "rate_limited",
                      "detail": problem, "input_sent": False}
            if not repetitive:
                result["retry_after_seconds"] = math.ceil(self._limiter.retry_after_seconds(now))
            result.update(delivery or {})
            return result
        previous = list(self._limiter._spoken)
        self._limiter.record(text, now)
        # Reserve before the public write. If persistence fails, nothing is
        # queued. A crash between these writes conservatively consumes the gap.
        self._save_history(path, self._limiter._spoken)
        try:
            at = write_agent_speech(directory, text, tone)
        except OSError:
            self._save_history(path, previous)
            return {"ok": False, "reason": "spool_unavailable", "input_sent": False}
        return {"ok": True, "queued_at": at, "rehearsal": rehearsal, "input_sent": False, **(delivery or {})}
