"""One-way, opt-in public event export. Chat is never read by the game worker."""

from __future__ import annotations

import json
import math
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, get_args

PUBLIC_KINDS = frozenset({
    "exploring", "navigating", "combat_action", "queens_blood_started",
    "queens_blood_turn", "queens_blood_won", "queens_blood_lost",
    "queens_blood_draw", "resting", "cutscene_skipped",
})


def public_event_for_action(values: dict[str, Any]) -> str | None:
    """Map action outcomes to fixed labels; never serialize action arguments or logs."""
    if values.get("ok") is not True:
        return None
    action = values.get("action")
    if action in {"navigate_next_segment", "travel_forward_chunk", "travel_direction_chunk", "travel_vector_chunk"}:
        return "navigating"
    if action in {"move", "survey_camera"}:
        return "exploring"
    if action == "combat_tap":
        return "combat_action"
    if action == "initiate_queens_blood_match" and values.get("outcome") == "mulligan_ready":
        return "queens_blood_started"
    if (action in {"play_queens_blood_match", "finish_queens_blood_match"}
            and values.get("outcome") == "match_finished"):
        if values.get("won") is True:
            return "queens_blood_won"
        if values.get("won") is False:
            score = values.get("score")
            if (isinstance(score, dict)
                    and type(score.get("player")) is int
                    and type(score.get("opponent")) is int
                    and score["player"] == score["opponent"]):
                return "queens_blood_draw"
            return "queens_blood_lost"
    if action == "use_nearby_bench":
        return "resting"
    if action == "skip_cutscene":
        return "cutscene_skipped"
    if action in {"place_queens_blood_card", "pass_queens_blood_turn"}:
        return "queens_blood_turn"
    return None


def write_public_event(directory: Path, kind: str) -> None:
    """Atomic spool write. Only the public enum label and timestamp cross processes."""
    if kind not in PUBLIC_KINDS:
        raise ValueError("Unsupported public stream event")
    directory.mkdir(parents=True, exist_ok=True)
    event = {"version": 1, "kind": kind, "at": datetime.now(timezone.utc).isoformat()}
    name = f"{int(time.time() * 1000)}-{uuid.uuid4()}.json"
    temporary = directory / f".{name}.tmp"
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(event, stream, separators=(",", ":"))
        os.replace(temporary, directory / name)
    finally:
        temporary.unlink(missing_ok=True)


# Agent speech: one short line the playing agent asks the streamer to say aloud.
# Mirrors validateAgentSpeech and AgentSpeechLimiter in src/streamer-safety.ts;
# change both together. Chat never reaches this path.
AGENT_SAY_KIND = "agent_say"
AGENT_SAY_MIN_LENGTH = 4
AGENT_SAY_MAX_LENGTH = 320
AGENT_SAY_MIN_GAP_SECONDS = 8.0
AGENT_SAY_WINDOW_SECONDS = 600.0
AGENT_SAY_MAX_PER_WINDOW = 60
AGENT_SAY_REPEAT_SECONDS = 300.0
# How a line is said (#182): the streamer maps each tone to the voice's pace,
# expressiveness and pauses. Mirrors SPEECH_TONES in src/streamer-delivery.ts.
# Shared with the MCP speech tools so unsupported tones are excluded from
# their advertised input schemas, not just rejected at runtime.
SpeechTone = Literal[
    "neutral", "calm", "confident", "excited", "tense", "concerned",
    "relieved", "surprised", "frustrated", "amused",
]
SPEECH_TONES = get_args(SpeechTone)
_SPEECH_CHARACTERS = re.compile(r"^[A-Za-z0-9 .,!?'’:;()–—-]+$")
_SPEECH_ADDRESS = re.compile(r"www\.|\b[a-z0-9-]+\.(?:com|net|org|io|tv|gg|me|co|uk|ly|app|dev)\b", re.IGNORECASE)
_SPEECH_TOKEN = re.compile(r"[A-Za-z0-9_-]{24,}")
_SPEECH_BACKEND = re.compile(
    r"\b(?:backend|mcp|harness|telemetry|runtime|deployment|repository|checkout|websocket|pid|dll|sandbox|github|"
    r"development|debugging|debugger|configuration|obs|tts|api|compiler)\b|"
    r"\b(?:identity fix|live test|source code|local files?|internal (?:state|logs?)|deck_select_ready|(?:git|code) commit|commit [a-f0-9]{7,40}|"
    r"unit tests?|test suite|pull requests?|feature branch|config files?|environment variables?|api keys?|credentials?|scheduled tasks?|"
    r"(?:service|server|worker|process) (?:check|restart|startup|status)|(?:start|restart)(?:ing)? (?:the )?(?:service|server|worker)|"
    r"stack trace|input broker|source binding|private reasoning|system prompt|developer prompt|"
    r"(?:edit|change|update|fix|patch)(?:ing)? (?:the )?(?:code|config|probe|script)|(?:code|script|config) (?:changes?|fixes?|patches?))\b", re.IGNORECASE)
_SPEECH_PROMOTION = re.compile(
    r"ko-?fi|patreon|paypal|venmo|cash ?app|buy ?me ?a ?coffee|donat(?:e|ion|ing)|tip ?jar|streamlabs|"
    r"streamelements|\bmerch(?:andise)?\b|sponsor|affiliate|promo code|discount code", re.IGNORECASE)
_SPEECH_PROFANITY = re.compile(
    r"\b(?:fuck\w*|motherf\w*|shit\w*|cunts?|bitch\w*|bastards?|dicks?|piss\w*|slut\w*|whore\w*|"
    r"fags?|faggot\w*|nigg\w*|retard\w*|twats?|wank\w*|cocks?)\b", re.IGNORECASE)


def agent_speech_problem(text: Any) -> str | None:
    """Why a requested line cannot be spoken, or None when it is acceptable."""
    if not isinstance(text, str):
        return "text must be a string"
    line = " ".join(text.split())
    if not AGENT_SAY_MIN_LENGTH <= len(line) <= AGENT_SAY_MAX_LENGTH:
        return f"text must be {AGENT_SAY_MIN_LENGTH}-{AGENT_SAY_MAX_LENGTH} characters on one line"
    if any(character in text.strip() for character in "\r\n\t"):
        return "text must be one line"
    if not _SPEECH_CHARACTERS.match(line):
        return "only letters, digits, spaces and . , ! ? ' : ; ( ) - – — are allowed"
    if _SPEECH_ADDRESS.search(line):
        return "web addresses are not allowed"
    if _SPEECH_TOKEN.search(line):
        return "long code-like words are not allowed"
    if _SPEECH_BACKEND.search(line):
        return "backend details are not gameplay narration"
    if _SPEECH_PROMOTION.search(line):
        return "support, donation or product wording is not allowed"
    if _SPEECH_PROFANITY.search(line):
        return "profanity is not allowed"
    return None


def speech_tone_problem(tone: Any) -> str | None:
    """Why a requested tone cannot be used, or None (no tone is neutral)."""
    if tone is None or tone in SPEECH_TONES:
        return None
    return "tone must be one of " + ", ".join(SPEECH_TONES)


def stream_is_live(state_dir: Path, now: float | None = None) -> bool:
    """True while the streamer's heartbeat says OBS is streaming."""
    try:
        live = json.loads((state_dir / "public-stream-live.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(live, dict) or set(live) != {"version", "streamStartedAt", "at"} or live.get("version") != 1:
        return False
    live_at = _timestamp(live.get("at"))
    now = time.time() if now is None else now
    return live_at is not None and -10.0 <= now - live_at <= STREAM_LIVE_MAX_AGE_SECONDS


def rehearsal_is_live(state_dir: Path, now: float | None = None) -> bool:
    """True while ``streamer run --rehearsal`` narrates an OBS recording.

    Speech and rehearsal results honour it. Live stream attribution still
    requires public-stream-live.json (stream_driver).
    """
    try:
        live = json.loads((state_dir / "public-stream-rehearsal.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(live, dict):
        return False
    legacy = live.get("version") == 1 and set(live) in ({"version", "at"}, {"version", "at", "recordingStartedAt"})
    stable = (live.get("version") == 2 and set(live) == {"version", "at", "recordingStartedAt", "recordingId"}
              and isinstance(live.get("recordingId"), str) and RECORDING_ID.fullmatch(live["recordingId"])
              and _timestamp(live.get("recordingStartedAt")) is not None)
    if not (legacy or stable):
        return False
    live_at = _timestamp(live.get("at"))
    now = time.time() if now is None else now
    return live_at is not None and -10.0 <= now - live_at <= STREAM_LIVE_MAX_AGE_SECONDS


# Near-repeats (#182), checked here only, so the agent rephrases: a line
# sharing most of its words with one from the last 5 minutes, or opening with
# the same two words as one of the last three lines ("I'll place ..." every
# move). The streamer refuses exact repeats on its own.
SIMILAR_WORD_SHARE = 0.75
SAME_OPENING_WORDS = 2
SAME_OPENING_LINES = 3
_WORD = re.compile(r"[a-z0-9']+")


def _words(line: str) -> list[str]:
    return _WORD.findall(line.lower())


def _too_similar(line: str, earlier: str) -> bool:
    words, other = set(_words(line)), set(_words(earlier))
    if not words or not other:
        return False
    return len(words & other) / len(words | other) >= SIMILAR_WORD_SHARE


class AgentSpeechLimiter:
    """At most one line per 8 s, 60 per 10 min, no repeat or near-repeat
    within 5 min, and no opening like one of the last three lines."""

    def __init__(self) -> None:
        self._spoken: list[tuple[float, str]] = []

    def problem(self, text: str, now: float) -> str | None:
        key = " ".join(text.split()).lower()
        self._spoken = [(at, line) for at, line in self._spoken if now - at < AGENT_SAY_WINDOW_SECONDS]
        if self._spoken and now - self._spoken[-1][0] < AGENT_SAY_MIN_GAP_SECONDS:
            return f"wait {AGENT_SAY_MIN_GAP_SECONDS - (now - self._spoken[-1][0]):.0f} s between lines"
        if len(self._spoken) >= AGENT_SAY_MAX_PER_WINDOW:
            return f"at most {AGENT_SAY_MAX_PER_WINDOW} lines per 10 minutes"
        recent = [line for at, line in self._spoken if now - at < AGENT_SAY_REPEAT_SECONDS]
        if key in recent:
            return "the same line was said in the last 5 minutes"
        if any(_too_similar(key, line) for line in recent):
            return "repetitive: a line like this was said in the last 5 minutes; say something new"
        opening = _words(key)[:SAME_OPENING_WORDS]
        if len(opening) == SAME_OPENING_WORDS and any(
                _words(line)[:SAME_OPENING_WORDS] == opening for _, line in self._spoken[-SAME_OPENING_LINES:]):
            return "repetitive: a recent line opened the same way; vary how lines start"
        return None

    def record(self, text: str, now: float) -> None:
        self._spoken.append((now, " ".join(text.split()).lower()))

    def retry_after_seconds(self, now: float) -> float:
        """Delay satisfying both pacing limits; repeats need new words instead."""
        recent = [(at, line) for at, line in self._spoken if now - at < AGENT_SAY_WINDOW_SECONDS]
        gap = max(0.0, AGENT_SAY_MIN_GAP_SECONDS - (now - recent[-1][0])) if recent else 0.0
        window = max(0.0, AGENT_SAY_WINDOW_SECONDS - (now - recent[0][0])) if len(recent) >= AGENT_SAY_MAX_PER_WINDOW else 0.0
        return max(gap, window)


def write_agent_speech(directory: Path, text: str, tone: str | None = None) -> str:
    """Atomic spool write of one validated line; returns the timestamp written."""
    problem = agent_speech_problem(text) or speech_tone_problem(tone)
    if problem is not None:
        raise ValueError(problem)
    directory.mkdir(parents=True, exist_ok=True)
    at = datetime.now(timezone.utc).isoformat()
    record = {"version": 1, "kind": AGENT_SAY_KIND, "text": " ".join(text.split()), "at": at}
    if tone is not None:
        record["tone"] = tone
    name = f"{int(time.time() * 1000)}-{uuid.uuid4()}.json"
    temporary = directory / f".{name}.tmp"
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(record, stream, separators=(",", ":"))
        os.replace(temporary, directory / name)
    finally:
        temporary.unlink(missing_ok=True)
    return at


# The streamer's driver declaration (streamer.ps1 driver-set) and its live-stream
# heartbeat, both in the stream state directory. Mirrors src/streamer-driver.ts.
DRIVER_NAMES = frozenset({"Claude", "ChatGPT", "Gemini", "Grok"})
REASONING_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"})
MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$")
RUN_ID = re.compile(r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$", re.IGNORECASE)
STREAM_LIVE_MAX_AGE_SECONDS = 60.0


def _timestamp(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.timestamp() if parsed.tzinfo is not None else None


RECORDING_ID = re.compile(r"[a-f0-9]{64}")


def _declared_driver(declared_file: Path, live_file: Path, live_keys: set[str], started_key: str,
                     now: float | None, *, recording: bool = False) -> dict[str, str] | None:
    """A declaration valid for the run its fresh heartbeat names, else None."""
    try:
        declared = json.loads(declared_file.read_text(encoding="utf-8"))
        live = json.loads(live_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not (isinstance(declared, dict) and isinstance(live, dict)):
        return None
    stable = recording and live.get("version") == 2
    declared_keys = {"version", "driver", "modelId", "effort", "runId", "streamStartedAt", "declaredAt"}
    if stable:
        live_keys = live_keys | {"recordingId"}
        declared_keys.add("recordingId")
        identity = live.get("recordingId")
        if not (isinstance(identity, str) and RECORDING_ID.fullmatch(identity)
                and declared.get("recordingId") == identity):
            return None
    if set(declared) != declared_keys:
        return None
    if set(live) != live_keys or live.get("version") != (2 if stable else 1) or declared.get("version") != 1:
        return None
    driver, model, effort, run = (declared.get(key) for key in ("driver", "modelId", "effort", "runId"))
    if not (driver in DRIVER_NAMES and isinstance(model, str) and MODEL_ID.match(model)
            and effort in REASONING_EFFORTS and isinstance(run, str) and RUN_ID.match(run)):
        return None
    now = time.time() if now is None else now
    live_at, live_started = _timestamp(live.get("at")), _timestamp(live.get(started_key))
    declared_started, declared_at = _timestamp(declared.get("streamStartedAt")), _timestamp(declared.get("declaredAt"))
    if None in (live_at, live_started, declared_started, declared_at):
        return None
    if not -10.0 <= now - live_at <= STREAM_LIVE_MAX_AGE_SECONDS:
        return None
    if abs(declared_started - live_started) > 10.0 or not live_started - 10.0 <= declared_at <= now + 10.0:
        return None
    return {"driver": driver, "model_id": model, "effort": effort, "run_id": run.lower()}


def stream_driver(state_dir: Path, now: float | None = None) -> dict[str, str] | None:
    """Who is playing: the driver, model and effort declared for the live stream.

    A match belongs to a model only while the streamer confirms OBS is
    streaming (a fresh ``public-stream-live.json``) and the declaration in
    ``public-driver.json`` was made for that same stream. Anything else --
    no stream, a stale or cleared declaration, a malformed file -- is None,
    and the match counts as unverified rather than guessing an owner.
    """
    return _declared_driver(state_dir / "public-driver.json", state_dir / "public-stream-live.json",
                            {"version", "streamStartedAt", "at"}, "streamStartedAt", now)


def rehearsal_driver(state_dir: Path, now: float | None = None) -> dict[str, str] | None:
    """The driver declared for the current rehearsal recording (``driver-set --rehearsal``).

    Only ever credits rehearsal records, which live in their own folders
    (``REHEARSAL_RESULTS_DIR``, ``REHEARSAL_HISTORY_DIR``) and never reach the
    live overlay or the published history page.
    """
    return _declared_driver(state_dir / "public-driver-rehearsal.json", state_dir / "public-stream-rehearsal.json",
                            {"version", "at", "recordingStartedAt"}, "recordingStartedAt", now, recording=True)


LIVE_RESULTS_DIR, LIVE_HISTORY_DIR = "public-qb-results", "qb-match-history"
REHEARSAL_RESULTS_DIR, REHEARSAL_HISTORY_DIR = "public-qb-results-rehearsal", "qb-match-history-rehearsal"


def match_record_target(state_dir: Path, now: float | None = None) -> tuple[dict[str, str] | None, str, str]:
    """(driver, results folder, history folder) for a match finishing now.

    A live stream records to the live folders with its declared driver. A
    rehearsal (OBS recording under ``streamer run --rehearsal``, not
    streaming) records to the rehearsal folders, credited to the rehearsal
    declaration if there is one, so a dry run can never change the live
    stats or the published history. Otherwise the match is unverified and
    recorded to the live folders, as before.
    """
    if stream_is_live(state_dir, now):
        return stream_driver(state_dir, now), LIVE_RESULTS_DIR, LIVE_HISTORY_DIR
    if rehearsal_is_live(state_dir, now):
        return rehearsal_driver(state_dir, now), REHEARSAL_RESULTS_DIR, REHEARSAL_HISTORY_DIR
    return None, LIVE_RESULTS_DIR, LIVE_HISTORY_DIR


def write_public_match_result(directory: Path, values: dict[str, Any],
                              driver: dict[str, str] | None = None) -> bool:
    """Record only a confirmed result, bounded match duration and the verified
    driver (``stream_driver``) for public stats."""
    kind = public_event_for_action(values)
    result = {
        "queens_blood_won": "win",
        "queens_blood_lost": "loss",
        "queens_blood_draw": "draw",
    }.get(kind)
    elapsed = values.get("elapsed_seconds")
    if result is None or type(elapsed) not in (int, float):
        return False
    if not math.isfinite(elapsed) or not 0 <= elapsed <= 3600:
        return False
    directory.mkdir(parents=True, exist_ok=True)
    record = {
        "version": 2,
        "result": result,
        "durationSeconds": round(elapsed),
        "at": datetime.now(timezone.utc).isoformat(),
        "driver": None if driver is None else {
            "driver": driver["driver"], "modelId": driver["model_id"],
            "effort": driver["effort"], "runId": driver["run_id"],
        },
    }
    name = f"{int(time.time() * 1000)}-{uuid.uuid4()}.json"
    temporary = directory / f".{name}.tmp"
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(record, stream, separators=(",", ":"))
        os.replace(temporary, directory / name)
    finally:
        temporary.unlink(missing_ok=True)
    return True


# Matches the overlay's accepted opponent text (queens-blood-stats.ts).
PUBLIC_OPPONENT_NAME = re.compile(r"^[A-Z][A-Za-z0-9 .'&;-]{0,31}$")


def public_opponent_name(name: Any) -> str:
    """The catalogue display name of a known opponent, else "Unknown".

    Only names from the game's own opponent catalogue reach the overlay; a
    caller-supplied selector is never echoed.
    """
    if not isinstance(name, str) or not name.strip():
        return "Unknown"
    try:
        from .qb_opponents import opponent

        display = opponent(name).get("display_name")
    except Exception:
        return "Unknown"
    return display if isinstance(display, str) and PUBLIC_OPPONENT_NAME.match(display) else "Unknown"


def write_public_match_state(path: Path, values: dict[str, Any]) -> bool:
    """Publish a bounded current-match marker; never echo an arbitrary target name."""
    kind = public_event_for_action(values)
    if kind == "queens_blood_started":
        target = values.get("target")
        name = values.get("opponent") or (target.get("name") if isinstance(target, dict) else None)
        opponent_id = None
        try:
            from .qb_opponents import opponent

            row = opponent(str(name)) if name else None
            if row is not None and isinstance(row.get("id"), str):
                opponent_id = row["id"]
        except Exception:
            pass
        state = {"version": 1, "active": True, "opponent": public_opponent_name(name),
                 "opponent_id": opponent_id, "at": datetime.now(timezone.utc).isoformat()}
    elif values.get("action") in {"play_queens_blood_match", "finish_queens_blood_match"}:
        state = {"version": 1, "active": False, "opponent": "Unknown",
                 "at": datetime.now(timezone.utc).isoformat()}
    else:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(state, stream, separators=(",", ":"))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return True
