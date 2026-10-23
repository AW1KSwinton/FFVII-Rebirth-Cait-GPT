"""Opt-in, bounded diagnostics. No poller, probe requests, or idle writer."""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
from threading import Event, Lock, Thread
import time
from typing import Any
from uuid import uuid4


SCHEMA_VERSION = 1
MAX_BYTES = 64 * 1024 * 1024
MAX_SECONDS = 1800.0
MAX_EVENT_NODES = 64000
MAX_BOUNDARY_EVENT_NODES = 128000


_JSON_ESCAPES = re.compile(r'["\\\x00-\x1f]')


def _snapshot(value: Any, budget: int, *, json_budget: int | None = None,
              node_limit: int = MAX_EVENT_NODES) -> tuple[Any, int]:
    """Copy JSON values with bounded traversal; serialization stays on the writer.

    Conservative weight bounds queue memory independently of the optional UTF-8
    JSON budget. The latter counts compact JSON bytes without serializing the
    complete event on the producer. Never queue mutable service objects.
    """
    remaining = budget
    wire_remaining = json_budget
    nodes = 0
    memory_reason = "event memory limit" if json_budget is not None else "event byte limit"

    def wire(amount: int) -> None:
        nonlocal wire_remaining
        if wire_remaining is not None:
            wire_remaining -= amount
            if wire_remaining < 0:
                raise ValueError("event byte limit")

    def visit(item: Any) -> Any:
        nonlocal remaining, nodes
        nodes += 1
        if nodes > node_limit:
            raise ValueError("event node limit")
        remaining -= 32
        if remaining < 0:
            raise ValueError(memory_reason)
        if item is None:
            wire(4)
            return item
        if isinstance(item, bool):
            wire(4 if item else 5)
            return item
        if isinstance(item, int):
            wire(len(str(item)))
            return item
        if isinstance(item, float) and math.isfinite(item):
            wire(len(repr(item)))
            return item
        if isinstance(item, str):
            remaining -= len(item) * 6
            if remaining < 0:
                raise ValueError(memory_reason)
            if wire_remaining is not None:
                try:
                    length = len(item.encode("utf-8")) + 2
                except UnicodeEncodeError as exc:
                    raise ValueError("non-UTF8 event") from exc
                for match in _JSON_ESCAPES.finditer(item):
                    length += 1 if match.group() in '\"\\\b\f\n\r\t' else 5
                wire(length)
            return item
        if isinstance(item, dict):
            if len(item) > remaining // 32:
                raise ValueError(memory_reason)
            if any(not isinstance(key, str) for key in item):
                raise ValueError("non-string JSON key")
            wire(2 + max(0, len(item) - 1) + len(item))
            return {visit(key): visit(child) for key, child in item.items()}
        if isinstance(item, (list, tuple)):
            if len(item) > remaining // 32:
                raise ValueError(memory_reason)
            wire(2 + max(0, len(item) - 1))
            return [visit(child) for child in item]
        raise ValueError("non-JSON event")

    result = visit(value)
    return result, budget - remaining


def _boundary_trace_source(kind: str, data: dict[str, Any]) -> dict[str, Any] | None:
    if kind not in {"action", "operation_result"} or not isinstance(data, dict):
        return None
    source = data.get("response") if kind == "operation_result" else data
    for _ in range(4):
        if not isinstance(source, dict) or not isinstance(source.get("result"), dict):
            break
        source = source["result"]
    return source if isinstance(source, dict) and isinstance(source.get("trace"), list) else None


class QueenBloodRecorder:
    """One explicitly started run; a full queue drops evidence, never waits.

    The producer only copies bounded JSON values and enqueues. One daemon
    serializes/writes/flushes in the background. Storage errors disable capture
    and remain visible in status; they cannot fail a gameplay input.
    """

    def __init__(self, root: Path, *, monotonic=time.monotonic,
                 wall_clock=lambda: datetime.now(timezone.utc),
                 queue_events: int = 64, queue_bytes: int = 8 * 1024 * 1024,
                 event_bytes: int = 1024 * 1024) -> None:
        self.root = Path(root)
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._queue_events = max(1, queue_events)
        self._queue_limit = max(1, queue_bytes)
        self._event_limit = max(1, event_bytes)
        self._lock = Lock()
        self._wake = Event()
        self._queue: deque = deque()
        self._thread: Thread | None = None
        self.active = False
        self._path: Path | None = None
        self._reason = "not_started"
        self._error: str | None = None
        self._queued_bytes = self._written_bytes = self._written_events = 0
        self._dropped = self._sampled_out = self._serial = 0
        self._drop_reasons: dict[str, int] = {}
        self._last_sample: dict[str, float] = {}

    def start(self, run_id: str, *, duration_seconds: float = MAX_SECONDS,
              max_bytes: int = MAX_BYTES, sample_hz: float = 10.0,
              metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._thread is not None and self._thread.is_alive():
            raise ValueError("recording already active or finalizing")
        if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", run_id):
            raise ValueError("run_id must be a short filesystem-safe run identifier")
        if not math.isfinite(duration_seconds) or not 1 <= duration_seconds <= MAX_SECONDS:
            raise ValueError(f"duration_seconds must be between 1 and {MAX_SECONDS:g}")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 4096 <= max_bytes <= MAX_BYTES:
            raise ValueError("max_bytes must be between 4096 and 67108864")
        if not math.isfinite(sample_hz) or not 1 <= sample_hz <= 20:
            raise ValueError("sample_hz must be between 1 and 20")
        # Check resolved parents, including a linked worktree's .git file.
        root = self.root.resolve()
        if any((parent / ".git").exists() for parent in (root, *root.parents)):
            raise ValueError("recordings must be outside every Git checkout")
        copied, _ = _snapshot(metadata or {}, 8192)
        self._started = self._monotonic()
        self._deadline = self._started + duration_seconds
        self._sample_interval = 1 / sample_hz
        self._max_bytes = int(max_bytes)
        stamp = self._wall_clock().isoformat().replace("+00:00", "Z")
        directory = root / (run_id + "-" + uuid4().hex)
        directory.mkdir(parents=True, exist_ok=False)
        self._path = directory / "events.jsonl"
        self._queue.clear()
        self._queued_bytes = self._written_bytes = self._written_events = 0
        self._dropped = self._sampled_out = self._serial = 0
        self._drop_reasons: dict[str, int] = {}
        self._last_sample.clear()
        self._error = None
        self._reason = "recording"
        self._header = {"schema_version": SCHEMA_VERSION, "kind": "header",
                        "timestamp_utc": stamp, "offset_seconds": 0.0,
                        "run_id": run_id, "metadata": copied,
                        "limits": {"duration_seconds": duration_seconds,
                                   "max_bytes": max_bytes, "sample_hz": sample_hz,
                                   "queue_events": self._queue_events,
                                   "queue_bytes": self._queue_limit,
                                   "event_bytes": self._event_limit,
                                   "event_memory_bytes": self._queue_limit,
                                   "event_nodes": MAX_EVENT_NODES,
                                   "boundary_event_nodes": MAX_BOUNDARY_EVENT_NODES},
                        "sampling": "consumer observations; not an exhaustive producer trace"}
        self._wake.clear()
        self.active = True
        self._thread = Thread(target=self._write, name="qb-diagnostic-writer", daemon=True)
        self._thread.start()
        return self.status()

    def _drop(self, reason: str, count: int = 1) -> None:
        self._dropped += count
        self._drop_reasons[reason] = self._drop_reasons.get(reason, 0) + count

    def _oversized_boundary_summary(self, kind: str, data: dict[str, Any], reason: str) -> None:
        # This is supplementary evidence, never a replacement for the dropped
        # raw event. Keep the gap counter and incomplete status honest.
        source = _boundary_trace_source(kind, data)
        if source is None:
            return
        summary = {"original_kind": kind, "raw_event_dropped": True,
                   "drop_reason": reason, "trace_omitted": True,
                   "trace_entries": len(source["trace"])}
        if isinstance(data.get("operation"), str):
            summary["operation"] = data["operation"]
        for key in ("ok", "action", "outcome", "state", "state_inference", "opponent", "target",
                    "decision", "reason", "interaction_sent", "dialogue_confirmations_sent",
                    "pre_sequence", "post_sequence", "match_screen_sequence", "timestamp_utc"):
            value = source.get(key)
            if value is None or isinstance(value, (bool, int, float, str)):
                summary[key] = value
        # Preserve the full final normalized card/board facts if they fit;
        # never slice cards or invent a raw-file reference.
        summary["target"] = source.get("target")
        summary["queen_blood"] = source.get("queen_blood")
        summary["inputs"] = source.get("inputs")
        if source["trace"]:
            summary["trace_first"] = source["trace"][0]
            summary["trace_last"] = source["trace"][-1]
        self.capture(kind + "_summary", summary)

    def capture(self, kind: str, data: dict[str, Any], *, sample_key: str | None = None) -> bool:
        if not self.active:
            return False
        now = self._monotonic()
        if now >= self._deadline:
            self._finish("duration_limit")
            return False
        # Never contend with disk/serialization; the writer holds this lock
        # only to remove a queue entry or update small counters.
        if not self._lock.acquire(blocking=False):
            self._drop("producer_lock_contention")
            return False
        try:
            if sample_key is not None:
                last = self._last_sample.get(sample_key, float("-inf"))
                if now - last < self._sample_interval:
                    self._sampled_out += 1
                    return False
                self._last_sample[sample_key] = now
            if len(self._queue) >= self._queue_events:
                self._drop("queue_event_limit")
                return False
        finally:
            self._lock.release()
        try:
            # Long initiation traces are occasional operation boundaries, not
            # periodic observations. Retain them without expanding wire/queue
            # bounds or doing any additional telemetry work.
            node_limit = (MAX_BOUNDARY_EVENT_NODES if _boundary_trace_source(kind, data) is not None
                          else MAX_EVENT_NODES)
            copied, weight = _snapshot(data, self._queue_limit, json_budget=self._event_limit - 256,
                                       node_limit=node_limit)
        except (ValueError, RecursionError) as exc:
            reason = str(exc) if isinstance(exc, ValueError) else "event recursion limit"
            self._drop(reason)
            if reason in {"event byte limit", "event node limit", "event memory limit"}:
                self._oversized_boundary_summary(kind, data, reason)
            return False
        if not self._lock.acquire(blocking=False):
            self._drop("producer_lock_contention")
            return False
        try:
            if not self.active or len(self._queue) >= self._queue_events or self._queued_bytes + weight > self._queue_limit:
                self._drop("capture_ended" if not self.active else
                           "queue_event_limit" if len(self._queue) >= self._queue_events else "queue_byte_limit")
                return False
            self._serial += 1
            record = {"schema_version": SCHEMA_VERSION, "kind": kind,
                      "event_id": self._serial,
                      "timestamp_utc": self._wall_clock().isoformat().replace("+00:00", "Z"),
                      "offset_seconds": max(0.0, now - self._started), "data": copied}
            self._queue.append((record, weight))
            self._queued_bytes += weight
        finally:
            self._lock.release()
        self._wake.set()
        return True

    def _finish(self, reason: str) -> None:
        if self.active or reason in {"byte_limit", "writer_error"}:
            self._reason = reason
            self.active = False
        self._wake.set()

    def stop(self, *, wait_seconds: float = 1.0) -> dict[str, Any]:
        self._finish("stopped")
        if self._thread is not None:
            self._thread.join(timeout=max(0, min(1.0, wait_seconds)))
        return self.status()

    def status(self) -> dict[str, Any]:
        return {"ok": self._error is None, "enabled": self.active,
                "limits": self._header["limits"] if hasattr(self, "_header") else None,
                "finalizing": bool(self._thread and self._thread.is_alive() and not self.active),
                "path": str(self._path) if self._path else None,
                "reason": self._reason, "error": self._error,
                "written_bytes": self._written_bytes, "written_events": self._written_events,
                "queued_bytes": self._queued_bytes, "queued_events": len(self._queue),
                "dropped_events": self._dropped, "drop_reasons": dict(self._drop_reasons), "sampled_out": self._sampled_out,
                "evidence_complete": self._dropped == 0 and self._error is None}

    def _open(self):
        return self._path.open("xb", buffering=65536)

    @staticmethod
    def _encode(record: dict) -> bytes:
        return (json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")

    def _write(self) -> None:
        try:
            with self._open() as stream:
                header = self._encode(self._header)
                if len(header) > self._max_bytes - 2048:
                    raise ValueError("metadata exceeds recording storage budget")
                stream.write(header)
                self._written_bytes = len(header)
                last_flush = self._monotonic()
                while True:
                    if self._monotonic() >= self._deadline:
                        self._finish("duration_limit")
                    with self._lock:
                        entry = self._queue.popleft() if self._queue else None
                        if entry:
                            self._queued_bytes -= entry[1]
                    if entry is None:
                        if not self.active:
                            break
                        self._wake.wait(0.25)
                        self._wake.clear()
                    else:
                        encoded = self._encode(entry[0])
                        if len(encoded) > self._event_limit:
                            self._drop("event byte limit")
                        elif self._written_bytes + len(encoded) > self._max_bytes - 2048:
                            self._drop("recording_byte_limit")
                            self._finish("byte_limit")
                        else:
                            stream.write(encoded)
                            self._written_bytes += len(encoded)
                            self._written_events += 1
                    if self._monotonic() - last_flush >= 0.25:
                        stream.flush()
                        last_flush = self._monotonic()
                footer = self._encode({"schema_version": SCHEMA_VERSION, "kind": "summary",
                                       "timestamp_utc": self._wall_clock().isoformat(),
                                       "offset_seconds": max(0, self._monotonic() - self._started),
                                       "data": {**{key: value for key, value in self.status().items()
                                                    if key not in {"path", "limits"}},
                                                "finalizing": False}})
                # The fixed reserve includes the bounded status/error fields.
                if self._written_bytes + len(footer) <= self._max_bytes:
                    stream.write(footer)
                    self._written_bytes += len(footer)
        except (OSError, ValueError, TypeError) as exc:
            self._error = str(exc)[:240]
            self._finish("writer_error")
        finally:
            self.active = False
            with self._lock:
                if self._queue:
                    self._drop("writer_discarded_queue", len(self._queue))
                self._queue.clear()
                self._queued_bytes = 0
