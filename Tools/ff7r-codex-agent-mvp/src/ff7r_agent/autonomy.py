"""Fail-closed control-plane primitives for bounded FFVII runtime runs.

This module does not provide a general-purpose process-kill or automatic
stale-lease reclaim operation. The game lifecycle has one deliberately narrow
exception: after a graceful close has failed, it may terminate only the
currently bound, identity-verified FFVII game/launcher processes. It never
terminates arbitrary processes discovered by name alone.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Callable

from .safety import WindowsGameEnvironment


class AutonomyError(RuntimeError):
    """A fail-closed control-plane refusal."""


# Rebirth can remain alive after WM_CLOSE while its launcher/window teardown
# completes. Keep short caller values useful for tests and emergency paths,
# but do not report an attached run as failed before a realistic shutdown
# window has elapsed.
DEFAULT_CLOSE_TIMEOUT_SECONDS = 90.0
MAX_CLOSE_TIMEOUT_SECONDS = 120.0
CLOSE_POLL_INTERVAL_SECONDS = 0.1
FORCE_CLOSE_EXIT_WAIT_SECONDS = 15.0


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def stable_sha256(path: str | Path, delay_seconds: float = 0.05) -> str:
    """Hash a file twice and refuse if its size/mtime/hash changes."""

    target = Path(path)
    first_stat = target.stat()
    first_hash = sha256_file(target)
    time.sleep(max(0.0, float(delay_seconds)))
    second_stat = target.stat()
    second_hash = sha256_file(target)
    if (
        first_stat.st_size != second_stat.st_size
        or first_stat.st_mtime_ns != second_stat.st_mtime_ns
        or first_hash != second_hash
    ):
        raise AutonomyError(f"SOURCE_CHANGED: {target}")
    return first_hash


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


class LifecycleLease:
    """A single-owner lease; an existing lease is never reclaimed automatically."""

    def __init__(
        self,
        runtime_dir: str | Path,
        owner_id: str,
        clock: Callable[[], str] = utc_now,
        process_id: int | None = None,
    ) -> None:
        self.runtime_dir = Path(runtime_dir)
        self.owner_id = str(owner_id)
        self.clock = clock
        self.process_id = int(process_id or os.getpid())
        self.path = self.runtime_dir / "lifecycle-lease.json"
        self.lease_id: str | None = None
        self._lock = Lock()

    def status(self) -> dict[str, Any] | None:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            raise AutonomyError(f"OWNER_UNKNOWN_OR_STALE: unreadable lease: {self.path}") from exc
        if not isinstance(value, dict) or value.get("version") != 1:
            raise AutonomyError(f"OWNER_UNKNOWN_OR_STALE: invalid lease: {self.path}")
        return value

    def acquire(self) -> dict[str, Any]:
        with self._lock:
            if self.status() is not None:
                raise AutonomyError("REFUSED_OWNER_ACTIVE_OR_UNKNOWN: lifecycle lease already exists")
            self.runtime_dir.mkdir(parents=True, exist_ok=True)
            self.lease_id = str(uuid.uuid4())
            value = {
                "version": 1,
                "lease_id": self.lease_id,
                "owner_id": self.owner_id,
                "pid": self.process_id,
                "acquired_at_utc": self.clock(),
                "state": "OWNED_BY_THIS_CONTROLLER",
            }
            try:
                descriptor = os.open(
                    self.path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
            except FileExistsError as exc:
                raise AutonomyError("REFUSED_OWNER_ACTIVE_OR_UNKNOWN: lease race") from exc
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    json.dump(value, stream, indent=2, sort_keys=True)
            except Exception:
                try:
                    self.path.unlink()
                except OSError:
                    pass
                raise
            return value

    def assert_owned(self) -> dict[str, Any]:
        value = self.status()
        if value is None or value.get("lease_id") != self.lease_id or value.get("owner_id") != self.owner_id:
            raise AutonomyError("OWNER_UNKNOWN_OR_STALE: lease is not owned by this controller")
        return value

    def release(self) -> None:
        with self._lock:
            self.assert_owned()
            self.path.unlink()
            self.lease_id = None

    def archive_stale(self, archive_path: str | Path) -> dict[str, Any]:
        """Archive an existing lease during an explicit recovery operation."""
        with self._lock:
            value = self.status()
            if value is None:
                return {"recovered": False, "reason": "no_lease"}
            if value.get("owner_id") != self.owner_id:
                raise AutonomyError("OWNER_UNKNOWN_OR_STALE: lease owner differs")
            destination = Path(archive_path)
            if destination.exists():
                raise AutonomyError("REFUSED_OVERWRITE: recovery archive exists")
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(self.path, destination)
            self.lease_id = None
            return {
                "recovered": True,
                "lease_id": value.get("lease_id"),
                "archive_path": str(destination),
            }


@dataclass(frozen=True)
class FixtureManifest:
    fixture_id: str
    source_path: str
    target_path: str
    source_sha256: str
    target_sha256: str
    created_at_utc: str
    classification: str = "DISPOSABLE_CLONE"

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "fixture_id": self.fixture_id,
            "source_path": self.source_path,
            "target_path": self.target_path,
            "source_sha256": self.source_sha256,
            "target_sha256": self.target_sha256,
            "created_at_utc": self.created_at_utc,
            "classification": self.classification,
        }


class FixtureBroker:
    """Create an explicit, hash-verified clone without overwriting a target."""

    def clone(
        self,
        source: str | Path,
        fixture_root: str | Path,
        fixture_id: str | None = None,
    ) -> FixtureManifest:
        source_path = Path(source).resolve(strict=True)
        fixture_name = fixture_id or f"fixture-{uuid.uuid4()}"
        root = Path(fixture_root).resolve() / fixture_name
        target_path = root / source_path.name
        if target_path.exists() or root.exists():
            raise AutonomyError("REFUSED_OVERWRITE: fixture destination already exists")
        source_hash = stable_sha256(source_path)
        root.mkdir(parents=True, exist_ok=False)
        try:
            shutil.copy2(source_path, target_path)
            target_hash = stable_sha256(target_path)
            if target_hash != source_hash:
                raise AutonomyError("FIXTURE_HASH_MISMATCH: source and clone differ")
            manifest = FixtureManifest(
                fixture_id=fixture_name,
                source_path=str(source_path),
                target_path=str(target_path),
                source_sha256=source_hash,
                target_sha256=target_hash,
                created_at_utc=utc_now(),
            )
            _atomic_json(root / "fixture-manifest.json", manifest.to_dict())
            return manifest
        except Exception:
            # The directory belongs to this failed transaction, but is retained
            # for forensic inspection rather than silently deleted.
            raise


def _get_path(value: Any, path: str) -> tuple[bool, Any]:
    current = value
    for part in str(path).split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return False, None
    return True, current


class AssertionEvaluator:
    """Evaluate small declarative assertions against a captured observation."""

    OPERATORS = {"eq", "ne", "gt", "gte", "lt", "lte", "truthy", "exists"}

    def evaluate(self, observation: dict[str, Any], conditions: list[dict[str, Any]]) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        for condition in conditions:
            path = str(condition.get("path", ""))
            operator = str(condition.get("op", "eq"))
            if not path or operator not in self.OPERATORS:
                raise AutonomyError("INVALID_ASSERTION: path and supported op are required")
            exists, actual = _get_path(observation, path)
            expected = condition.get("value")
            if operator == "exists":
                passed = exists is bool(expected if expected is not None else True)
            elif operator == "truthy":
                passed = exists and bool(actual) is bool(expected if expected is not None else True)
            elif not exists:
                passed = False
            elif operator == "eq":
                passed = actual == expected
            elif operator == "ne":
                passed = actual != expected
            elif operator == "gt":
                passed = actual > expected
            elif operator == "gte":
                passed = actual >= expected
            elif operator == "lt":
                passed = actual < expected
            else:
                passed = actual <= expected
            results.append({
                "path": path,
                "op": operator,
                "expected": expected,
                "actual": actual if exists else None,
                "exists": exists,
                "passed": bool(passed),
            })
        return {
            "passed": all(item["passed"] for item in results),
            "assertions": results,
        }


class EvidenceBundle:
    """Append-only run evidence with a deterministic SHA-256 seal."""

    def __init__(self, root: str | Path, run_id: str, context: dict[str, Any]) -> None:
        self.root = Path(root).resolve() / run_id
        self.root.mkdir(parents=True, exist_ok=False)
        self.events_path = self.root / "events.jsonl"
        _atomic_json(self.root / "run-context.json", context)

    def record(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        event = {"captured_at_utc": utc_now(), "kind": str(kind), "payload": payload}
        with self.events_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, sort_keys=True) + "\n")
        return event

    def seal(self) -> dict[str, Any]:
        files: dict[str, str] = {}
        for path in sorted(self.root.rglob("*")):
            if path.is_file() and path.name != "SEAL.json":
                files[str(path.relative_to(self.root)).replace("\\", "/")] = sha256_file(path)
        seal = {
            "version": 1,
            "run_id": self.root.name,
            "sealed_at_utc": utc_now(),
            "hash_algorithm": "SHA-256",
            "files": files,
        }
        _atomic_json(self.root / "SEAL.json", seal)
        return seal

    def verify(self) -> dict[str, Any]:
        seal_path = self.root / "SEAL.json"
        try:
            seal = json.loads(seal_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AutonomyError("A16_EVIDENCE_MISSING: no valid SEAL.json") from exc
        mismatches = []
        listed = set(seal.get("files", {}))
        for relative, expected in seal.get("files", {}).items():
            path = self.root / relative
            if not path.exists() or sha256_file(path) != expected:
                mismatches.append(relative)
        for path in self.root.rglob("*"):
            if path.is_file() and path.name != "SEAL.json":
                relative = str(path.relative_to(self.root)).replace("\\", "/")
                if relative not in listed:
                    mismatches.append(relative)
        return {"valid": not mismatches, "mismatches": mismatches, "seal": seal}


class LifecycleController:
    """Bounded launcher/telemetry binder for a controller-owned run.

    Normal shutdown is graceful.  If that fails, the controller may terminate
    only the exact allow-listed PIDs bound to its own durable run context; an
    unowned or ambiguous process still produces a manual-intervention result.
    """

    def __init__(
        self,
        executable: str | Path,
        telemetry_path: str | Path,
        runtime_dir: str | Path,
        owner_id: str = "ff7r-codex-agent",
        environment: WindowsGameEnvironment | None = None,
        launcher_executable: str | Path | None = None,
    ) -> None:
        self.executable = Path(executable).resolve()
        inferred_launcher = self.executable
        if self.executable.name.lower() == "ff7rebirth_.exe":
            candidate = self.executable.parents[3] / "ff7rebirth.exe"
            if candidate.is_file():
                inferred_launcher = candidate
        self.launcher_executable = Path(launcher_executable or inferred_launcher).resolve()
        self.telemetry_path = Path(telemetry_path).resolve()
        self.runtime_dir = Path(runtime_dir).resolve()
        self.owner_id = owner_id
        self.environment = environment or WindowsGameEnvironment()
        self.lease = LifecycleLease(self.runtime_dir, owner_id)
        self.process: subprocess.Popen[Any] | None = None
        self.run_id: str | None = None
        self.context: dict[str, Any] | None = None

    def status(self) -> dict[str, Any]:
        lease = self.lease.status()
        context = self.context
        if context is None:
            context_path = self.runtime_dir / "run-context.json"
            if context_path.exists():
                try:
                    context = json.loads(context_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    context = {"state": "UNKNOWN_OR_CORRUPT"}
        return {
            "controller": "available",
            "executable_path": str(self.executable),
            "executable_present": self.executable.is_file(),
            "launcher_path": str(self.launcher_executable),
            "launcher_present": self.launcher_executable.is_file(),
            "telemetry_path": str(self.telemetry_path),
            "telemetry_present": self.telemetry_path.is_file(),
            "runtime_dir": str(self.runtime_dir),
            "lease": lease,
            "run_context": context,
            "owned_process_running": self._owned_process_running() if context else False,
        }

    def verify_executable_identity(self, expected_sha256: str) -> dict[str, Any]:
        """Verify the bound executable once and persist the result.

        A newly attached process deliberately has no executable hash in its
        context.  This method is therefore lazy: callers must decide that the
        session is settled gameplay before invoking it.  Once a hash is in the
        durable context it is reused, so title/loading polling cannot trigger
        repeated large-file hashing.
        """
        expected = str(expected_sha256 or "").strip().upper()
        # A runtime worker may construct a fresh controller and call the live
        # adapter directly.  ``status()`` can already see the durable attached
        # context, but it intentionally does not mutate controller state.  The
        # lazy gameplay gate is the first point at which executable hashing is
        # allowed, so rehydrate that exact context here without launching,
        # attaching to a different PID, or weakening identity checks.
        if not self.context:
            try:
                self._load_durable_context()
            except AutonomyError as exc:
                return {
                    "verified": False,
                    "reason": "executable_identity_context_missing",
                    "sha256": None,
                    "error": str(exc),
                }
        if not self.context:
            return {
                "verified": False,
                "reason": "executable_identity_context_missing",
                "sha256": None,
            }
        stored = str(self.context.get("executable_sha256") or "").strip().upper()
        if stored:
            return {
                "verified": stored == expected,
                "reason": "durable_executable_identity_match" if stored == expected else "executable_identity_mismatch",
                "sha256": stored,
                "source": "durable_run_context",
            }
        try:
            actual = stable_sha256(self.executable)
        except (OSError, AutonomyError) as exc:
            return {
                "verified": False,
                "reason": "executable_identity_unavailable",
                "sha256": None,
                "error": str(exc),
            }
        self.context["executable_sha256"] = actual
        self.context["executable_identity_verified"] = actual == expected
        self.context["executable_identity_verified_at_utc"] = utc_now()
        self.context["executable_identity_source"] = "stable_sha256_lazy_gameplay_gate"
        _atomic_json(self.runtime_dir / "run-context.json", self.context)
        return {
            "verified": actual == expected,
            "reason": "stable_executable_identity_match" if actual == expected else "executable_identity_mismatch",
            "sha256": actual,
            "source": "stable_sha256_lazy_gameplay_gate",
        }

    @classmethod
    def attach(
        cls,
        executable: str | Path,
        telemetry_path: str | Path,
        runtime_dir: str | Path,
        owner_id: str = "ff7r-codex-agent",
        environment: WindowsGameEnvironment | None = None,
    ) -> "LifecycleController":
        controller = cls(executable, telemetry_path, runtime_dir, owner_id, environment)
        context_path = controller.runtime_dir / "run-context.json"
        try:
            context = json.loads(context_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AutonomyError("LIFECYCLE_CONTROLLER_ABSENT: no durable run context") from exc
        controller.context = context
        controller.run_id = str(context.get("run_id"))
        if context.get("owner_id") != owner_id:
            raise AutonomyError("OWNER_UNKNOWN_OR_STALE: run owner does not match")
        if Path(str(context.get("executable_path"))).resolve() != controller.executable:
            raise AutonomyError("PROCESS_IDENTITY_UNKNOWN: executable context does not match")
        if context.get("launcher_path") and Path(str(context["launcher_path"])).resolve() != controller.launcher_executable:
            raise AutonomyError("PROCESS_IDENTITY_UNKNOWN: launcher context does not match")
        if Path(str(context.get("telemetry_path"))).resolve() != controller.telemetry_path:
            raise AutonomyError("TELEMETRY_IDENTITY_MISSING: telemetry context does not match")
        controller.lease.lease_id = str(context.get("lease_id"))
        controller.lease.assert_owned()
        return controller

    def _owned_process_running(self) -> bool:
        # On Windows, os.kill(pid, 0) is not a reliable liveness probe: it can
        # report success for a PID that has already exited. Once the target PID
        # is durably bound, use the Win32 process snapshot as the authority and
        # ignore the long-lived Popen handle, which may retain stale state after
        # Task Manager closes the game.
        if self.context and self.context.get("pid"):
            try:
                processes = self.environment.game_process_ids()
            except AttributeError:
                processes = None
            if processes is not None:
                bound_pids = {
                    int(value) for key in ("pid", "launcher_pid")
                    if (value := self.context.get(key))
                }
                return any(pid in processes for pid in bound_pids)

        if self.process is not None and self.process.poll() is None:
            return True
        if self.context:
            for key in ("pid", "launcher_pid"):
                pid = self.context.get(key)
                if not pid:
                    continue
                if os.name == "nt":
                    # A Windows adapter without game_process_ids() cannot
                    # prove a bound PID is live; do not resurrect the broken
                    # os.kill(pid, 0) fallback on that platform.
                    continue
                try:
                    os.kill(int(pid), 0)
                except PermissionError:
                    return True
                except OSError:
                    continue
                return True
        try:
            return bool(self.environment.game_process_ids())
        except AttributeError:
            return self.environment.game_process_exists()

    def _wait_for_process_exit(self, timeout_seconds: float) -> bool:
        """Wait for the bound/observed game PIDs to disappear."""
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        while self._owned_process_running() and time.monotonic() < deadline:
            time.sleep(CLOSE_POLL_INTERVAL_SECONDS)
        return not self._owned_process_running()

    def _load_durable_context(self) -> bool:
        """Rehydrate an owned run so a fresh MCP can close it safely."""
        if self.context is not None:
            return True
        context_path = self.runtime_dir / "run-context.json"
        if not context_path.is_file():
            return False
        try:
            value = json.loads(context_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AutonomyError("OWNER_UNKNOWN_OR_STALE: invalid run context") from exc
        if not isinstance(value, dict):
            raise AutonomyError("OWNER_UNKNOWN_OR_STALE: invalid run context")
        if value.get("owner_id") != self.owner_id:
            raise AutonomyError("OWNER_UNKNOWN_OR_STALE: run owner does not match")
        if Path(str(value.get("executable_path"))).resolve() != self.executable:
            raise AutonomyError("PROCESS_IDENTITY_UNKNOWN: executable context does not match")
        if value.get("launcher_path") and Path(str(value["launcher_path"])).resolve() != self.launcher_executable:
            raise AutonomyError("PROCESS_IDENTITY_UNKNOWN: launcher context does not match")
        if Path(str(value.get("telemetry_path"))).resolve() != self.telemetry_path:
            raise AutonomyError("TELEMETRY_IDENTITY_MISSING: telemetry context does not match")
        self.context = value
        self.run_id = str(value.get("run_id"))
        lease = self.lease.status()
        if lease is not None and lease.get("lease_id") == value.get("lease_id"):
            self.lease.lease_id = str(lease["lease_id"])
        return True

    def focus_game_window(self) -> dict[str, Any]:
        """Best-effort foreground recovery for the bound gameplay process."""
        focus = getattr(self.environment, "focus_game_window", None)
        if not callable(focus):
            return {"ok": False, "state": "foreground_recovery_unavailable"}
        pid = int(self.context["pid"]) if self.context and self.context.get("pid") else None
        try:
            result = focus(pid=pid)
        except TypeError:
            result = focus()
        return dict(result) if isinstance(result, dict) else {"ok": bool(result)}

    def _can_force_close_owned_run(self) -> bool:
        """Project policy prohibits force-closing even a controller-owned game."""
        return False

    def _owned_process_ids(self) -> list[int]:
        if not self.context:
            return []
        bound = {
            int(self.context[key])
            for key in ("pid", "launcher_pid")
            if self.context.get(key)
        }
        try:
            observed = self.environment.game_process_ids()
        except AttributeError:
            return sorted(bound)
        return sorted(pid for pid in bound if pid in observed)

    def _force_terminate(self, processes: dict[int, str]) -> dict[str, Any]:
        """Terminate the remaining bound game processes and wait for exit."""
        terminate = getattr(self.environment, "terminate_game_processes", None)
        if not callable(terminate):
            return {"ok": False, "state": "termination_unavailable", "terminated_pids": []}
        tokens = (self.context or {}).get("process_creation_tokens") or {}
        identities = {pid: {"name": name, "creation_token": tokens[str(pid)]}
                      for pid, name in processes.items() if tokens.get(str(pid))}
        # Verify creation tokens only when the run recorded one for every PID.
        outcome = dict(terminate(sorted(processes), identities)
                       if identities and len(identities) == len(processes)
                       else terminate(sorted(processes)))
        self._wait_for_process_exit(FORCE_CLOSE_EXIT_WAIT_SECONDS)
        return {"ok": not self._owned_process_running(), "state": "terminated", **outcome}

    def _bound_process_snapshot(self) -> dict[int, str]:
        """Return only currently observed PIDs belonging to this context."""
        try:
            observed = self.environment.game_process_ids()
        except AttributeError:
            observed = {}
        if not self.context:
            return dict(observed)
        bound = {
            int(self.context[key])
            for key in ("pid", "launcher_pid")
            if self.context.get(key)
        }
        return {pid: name for pid, name in observed.items() if pid in bound}

    @staticmethod
    def _failed_pid_details(
        processes: dict[int, str],
        reason: str,
    ) -> list[dict[str, object]]:
        return [
            {"pid": int(pid), "name": name, "reason": reason}
            for pid, name in sorted(processes.items())
        ]

    def _bind_target_process(self) -> None:
        if not self.context or self.context.get("pid"):
            return
        try:
            processes = self.environment.game_process_ids()
        except AttributeError:
            processes = {}
        if not processes:
            return
        launcher_pid = self.context.get("launcher_pid")
        target_candidates = [
            pid for pid, name in processes.items()
            if name == "ff7rebirth_.exe" and pid != launcher_pid
        ]
        candidates = [pid for pid in processes if pid != launcher_pid]
        if len(target_candidates) == 1:
            target_pid = target_candidates[0]
        elif len(target_candidates) > 1:
            raise AutonomyError("PROCESS_IDENTITY_UNKNOWN: multiple game PIDs appeared")
        elif len(processes) == 1 and launcher_pid is None:
            # Do not bind the launcher as the gameplay target while the child
            # is still starting.  The next telemetry poll will retry.
            only_pid = next(iter(processes))
            if processes[only_pid] == "ff7rebirth.exe":
                return
            if processes[only_pid] != "ff7rebirth_.exe":
                return
            target_pid = only_pid
        elif len(candidates) == 1:
            target_pid = candidates[0]
        else:
            raise AutonomyError("PROCESS_IDENTITY_UNKNOWN: multiple game PIDs appeared")
        self.context["pid"] = int(target_pid)
        self.context["target_process_name"] = processes[target_pid]
        _atomic_json(self.runtime_dir / "run-context.json", self.context)

    def _attach_running_processes(self, processes: dict[int, str]) -> dict[str, Any]:
        """Bind to one existing Rebirth process without launching another."""
        if not processes:
            raise AutonomyError("GAME_NOT_RUNNING: no FFVII Rebirth process exists")
        context_path = self.runtime_dir / "run-context.json"
        existing: dict[str, Any] | None = None
        if context_path.is_file():
            try:
                value = json.loads(context_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise AutonomyError("OWNER_UNKNOWN_OR_STALE: invalid run context") from exc
            if isinstance(value, dict):
                existing = value

        target_pids = [pid for pid, name in processes.items() if name == "ff7rebirth_.exe"]
        launcher_pids = [pid for pid, name in processes.items() if name == "ff7rebirth.exe"]
        current_lease = self.lease.status()
        existing_lease_matches = bool(
            existing
            and existing.get("lease_id")
            and current_lease
            and current_lease.get("owner_id") == self.owner_id
            and current_lease.get("lease_id") == existing.get("lease_id")
        )
        if existing and existing.get("pid") in processes:
            target_pid = int(existing["pid"])
            launcher_pid = existing.get("launcher_pid")
            if launcher_pid is not None and int(launcher_pid) not in processes:
                launcher_pid = launcher_pids[0] if len(launcher_pids) == 1 else None
            context = dict(existing)
            # A worker restart may see a durable context left by an older
            # controller.  The same owner name is not ownership proof: only a
            # matching durable lease may classify the process as our exact
            # run and therefore permit a termination fallback.
            context["attachment"] = (
                "existing_controller_run"
                if existing_lease_matches
                else "external_or_prior_task"
            )
        else:
            if len(target_pids) != 1:
                raise AutonomyError(
                    "PROCESS_IDENTITY_UNKNOWN: expected exactly one running ff7rebirth_.exe"
                )
            target_pid = target_pids[0]
            launcher_pid = launcher_pids[0] if len(launcher_pids) == 1 else None
            context = {
                "version": 1,
                "run_id": (
                    f"attach-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-"
                    f"{uuid.uuid4().hex[:8]}"
                ),
                "owner_id": self.owner_id,
                "lease_id": None,
                "pid": target_pid,
                "launcher_pid": launcher_pid,
                "launch_timestamp_utc": None,
                "executable_path": str(self.executable),
                "launcher_path": str(self.launcher_executable),
                "telemetry_path": str(self.telemetry_path),
                "telemetry_binding": "attached-existing-process",
                "attachment": "external_or_prior_task",
            }
        if Path(str(context.get("executable_path"))).resolve() != self.executable:
            raise AutonomyError("PROCESS_IDENTITY_UNKNOWN: executable context does not match")
        if context.get("telemetry_path") and Path(
            str(context["telemetry_path"])
        ).resolve() != self.telemetry_path:
            raise AutonomyError("TELEMETRY_IDENTITY_MISSING: telemetry context does not match")
        context["pid"] = target_pid
        if launcher_pid is not None:
            context["launcher_pid"] = int(launcher_pid)
        self.context = context
        self.run_id = str(context.get("run_id"))
        self.process = None
        lease = current_lease
        if lease is not None and lease.get("owner_id") == self.owner_id:
            if context.get("lease_id") == lease.get("lease_id"):
                self.lease.lease_id = str(lease["lease_id"])
        _atomic_json(context_path, context)
        return dict(context)

    def ensure_started(
        self,
        *,
        allow_launch: bool = True,
        arguments: list[str] | None = None,
    ) -> dict[str, Any]:
        """Attach to an existing game or launch it once through the launcher."""
        try:
            processes = self.environment.game_process_ids()
        except AttributeError:
            processes = {}
        if processes:
            return {
                "mode": "attached",
                "processes": dict(processes),
                "context": self._attach_running_processes(processes),
            }
        if not allow_launch:
            raise AutonomyError("STARTUP_PHASE_REQUIRED: start_game_to_main_menu must run first")
        if self.lease.status() is not None:
            self.recover_stale()
        return {"mode": "launched", "processes": {}, "context": self.start(arguments)}

    def mark_startup_phase(self, phase: str) -> dict[str, Any]:
        """Persist the reusable startup state-machine phase."""
        if self.context is None:
            raise AutonomyError("LIFECYCLE_CONTROLLER_ABSENT: no run context")
        allowed = {
            "main_menu",
            "selection_in_progress",
            "post_selection_prompt",
            "new_game_setup",
            "gameplay_loading",
            "gameplay_ready",
        }
        if phase not in allowed:
            raise AutonomyError(f"INVALID_STARTUP_PHASE: {phase}")
        self.context["startup_phase"] = phase
        self.context["startup_phase_updated_at_utc"] = utc_now()
        _atomic_json(self.runtime_dir / "run-context.json", self.context)
        return dict(self.context)

    def start(self, arguments: list[str] | None = None) -> dict[str, Any]:
        if not self.executable.is_file():
            raise AutonomyError(f"ABSENT: executable not found: {self.executable}")
        if not self.launcher_executable.is_file():
            raise AutonomyError(f"ABSENT: launcher not found: {self.launcher_executable}")
        if self.environment.game_process_exists():
            raise AutonomyError("REFUSED_GAME_RUNNING: an FFVII process already exists")
        executable_hash = stable_sha256(self.executable)
        launcher_hash = stable_sha256(self.launcher_executable)
        lease = self.lease.acquire()
        self.run_id = f"run-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
        try:
            command = [str(self.launcher_executable), *(arguments or [])]
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            self.process = subprocess.Popen(
                command,
                # The launcher resolves its content, bootstrap DLLs, and
                # child-process handoff relative to the installation root.
                # The target executable lives one level deeper in Win64.
                cwd=str(self.launcher_executable.parent),
                creationflags=creationflags,
            )
            self.context = {
                "version": 1,
                "run_id": self.run_id,
                "owner_id": self.owner_id,
                "lease_id": lease["lease_id"],
                "pid": None,
                "launcher_pid": self.process.pid,
                "launch_timestamp_utc": utc_now(),
                "executable_path": str(self.executable),
                "executable_sha256": executable_hash,
                "launcher_path": str(self.launcher_executable),
                "launcher_sha256": launcher_hash,
                "telemetry_path": str(self.telemetry_path),
                "telemetry_binding": "controller-run-window",
            }
            # Test doubles and non-Windows adapters may not expose a separate
            # target-process enumeration surface.  In that case the spawned
            # process is the only identity available; the real Windows
            # environment leaves this unset until the child target appears.
            if not hasattr(self.environment, "game_process_ids"):
                self.context["pid"] = self.process.pid
            _atomic_json(self.runtime_dir / "run-context.json", self.context)
            return dict(self.context)
        except Exception:
            # Popen and durable-context creation are one transaction from the
            # caller's perspective.  If the transaction fails, clean up the
            # exact launcher handle we created and release the lease when the
            # process is gone; never leave a half-written run behind.
            if self.process is not None and self.process.poll() is None:
                try:
                    self.process.terminate()
                    self.process.wait(timeout=3.0)
                except (OSError, subprocess.TimeoutExpired):
                    try:
                        self.process.kill()
                        self.process.wait(timeout=3.0)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
            if self.process is None or self.process.poll() is not None:
                if self.lease.lease_id is not None:
                    try:
                        self.lease.release()
                    except AutonomyError:
                        pass
            self.process = None
            raise

    def recover_stale(self) -> dict[str, Any]:
        """Archive this owner's stale lease only when no game process exists."""
        if self.environment.game_process_exists():
            raise AutonomyError(
                "REFUSED_GAME_RUNNING: stale-lease recovery requires an exited game"
            )
        lease = self.lease.status()
        if lease is None:
            self.context = None
            self.run_id = None
            return {"recovered": False, "reason": "no_lease"}
        if lease.get("owner_id") != self.owner_id:
            raise AutonomyError("OWNER_UNKNOWN_OR_STALE: lease owner differs")
        context_path = self.runtime_dir / "run-context.json"
        context: dict[str, Any] | None = None
        context_warning: str | None = None
        if context_path.is_file():
            try:
                context_value = json.loads(context_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise AutonomyError("OWNER_UNKNOWN_OR_STALE: invalid run context") from exc
            if not isinstance(context_value, dict):
                raise AutonomyError("OWNER_UNKNOWN_OR_STALE: invalid run context")
            context = context_value
            if context.get("lease_id") != lease.get("lease_id"):
                # An attached-existing-process run historically wrote a
                # null lease_id, then left the controller lease behind when
                # the game exited. Once process absence is proven above, the
                # lease is recoverable even though its context cannot be
                # cryptographically joined to the old run. Preserve that
                # distinction in the archived context instead of trapping
                # every later startup behind an unrecoverable stale file.
                context_warning = "lease_context_mismatch"
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        archive_path = self.runtime_dir / f"lifecycle-lease.stale-{timestamp}-{uuid.uuid4().hex[:8]}.json"
        result = self.lease.archive_stale(archive_path)
        if context_warning is not None:
            result["warning"] = context_warning
        if context is not None:
            context["state"] = "stale_recovered"
            context["recovered_at_utc"] = utc_now()
            context["recovery_archive_path"] = str(archive_path)
            if context_warning is not None:
                context["recovery_warning"] = context_warning
            _atomic_json(context_path, context)
        self.context = None
        self.run_id = None
        return result

    def wait_for_fresh_telemetry(
        self,
        timeout_seconds: float = 30.0,
        max_age_seconds: float = 2.0,
        poll_seconds: float = 0.1,
    ) -> dict[str, Any]:
        if self.context is None:
            raise AutonomyError("LIFECYCLE_CONTROLLER_ABSENT: no owned run")
        deadline = time.monotonic() + float(timeout_seconds)
        previous_sequence: int | None = None
        first_mtime: int | None = None
        while time.monotonic() < deadline:
            if not self._owned_process_running():
                raise AutonomyError("STARTUP_FAILED: owned game process exited")
            try:
                self._bind_target_process()
                stat = self.telemetry_path.stat()
                payload = json.loads(self.telemetry_path.read_text(encoding="utf-8"))
                timestamp = datetime.fromisoformat(
                    str(payload["timestamp_utc"]).replace("Z", "+00:00")
                )
                age = max(0.0, (datetime.now(timezone.utc) - timestamp).total_seconds())
                sequence = int(payload["sequence"])
                running = bool(payload["game"]["running"])
                if first_mtime is None:
                    first_mtime = stat.st_mtime_ns
                elif (
                    running
                    and age < max_age_seconds
                    and previous_sequence is not None
                    and sequence > previous_sequence
                    and stat.st_mtime_ns >= first_mtime
                ):
                    result = dict(payload)
                    result["run_binding"] = {
                        "run_id": self.context["run_id"],
                        "pid": self.context["pid"],
                        "sequence": sequence,
                        "age_seconds": age,
                        "identity_binding": self.context["telemetry_binding"],
                    }
                    return result
                previous_sequence = sequence
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                pass
            time.sleep(max(0.01, float(poll_seconds)))
        raise AutonomyError("UE4SS_NOT_READY: fresh advancing telemetry was not observed")

    def wait_for_gameplay_ready(
        self,
        timeout_seconds: float = 60.0,
        max_age_seconds: float = 2.0,
        poll_seconds: float = 0.1,
    ) -> dict[str, Any]:
        """Wait until advancing telemetry contains settled active gameplay.

        A running process and advancing exporter are insufficient for a test:
        title screens, resume prompts, and black loading windows can all emit
        telemetry. This readiness gate requires a map, player position,
        non-paused/non-menu gameplay, and a short run of fresh advancing
        samples with the same map/actor/process identity.  The consecutive
        samples are important: the exporter can become live before the save
        world has finished accepting input.
        """
        if self.context is None:
            raise AutonomyError("LIFECYCLE_CONTROLLER_ABSENT: no owned run")
        deadline = time.monotonic() + float(timeout_seconds)
        previous_sequence: int | None = None
        previous_identity: tuple[Any, ...] | None = None
        settled_samples = 0
        first_mtime: int | None = None
        while time.monotonic() < deadline:
            if not self._owned_process_running():
                raise AutonomyError("STARTUP_FAILED: owned game process exited")
            try:
                self._bind_target_process()
                stat = self.telemetry_path.stat()
                payload = json.loads(self.telemetry_path.read_text(encoding="utf-8"))
                timestamp = datetime.fromisoformat(
                    str(payload["timestamp_utc"]).replace("Z", "+00:00")
                )
                age = max(0.0, (datetime.now(timezone.utc) - timestamp).total_seconds())
                sequence = int(payload["sequence"])
                game = payload["game"]
                # The live exporter keeps player at the telemetry root; the
                # older test fixture/schema nested it under game. Accept both
                # so a loaded world is not misclassified as a title screen.
                player = payload.get("player") or game.get("player") or {}
                position = player.get("position")
                loaded = bool(
                    game.get("running")
                    and not game.get("paused", False)
                    and not game.get("in_menu", False)
                    and game.get("map_id")
                    and isinstance(position, dict)
                    and all(key in position for key in ("x", "y", "z"))
                )
                actor_id = player.get("actor_id")
                foreground = None
                foreground_probe = getattr(self.environment, "game_is_foreground", None)
                if callable(foreground_probe):
                    try:
                        foreground = bool(foreground_probe())
                    except Exception:
                        foreground = False
                process_identity: tuple[Any, ...]
                process_probe = getattr(self.environment, "game_process_ids", None)
                if callable(process_probe):
                    try:
                        process_identity = tuple(sorted(
                            (int(pid), str(name)) for pid, name in process_probe().items()
                        ))
                    except Exception:
                        process_identity = (self.context.get("pid"),)
                else:
                    process_identity = (self.context.get("pid"),)
                identity = (
                    game.get("map_id"),
                    actor_id,
                    process_identity,
                    foreground,
                )
                if first_mtime is None:
                    first_mtime = stat.st_mtime_ns
                advancing = (
                    previous_sequence is not None and sequence > previous_sequence
                )
                if loaded and age < max_age_seconds and stat.st_mtime_ns >= first_mtime and foreground is not False:
                    if advancing and identity == previous_identity:
                        settled_samples += 1
                    else:
                        settled_samples = 1
                    if settled_samples >= 3:
                        result = dict(payload)
                        result["run_binding"] = {
                            "run_id": self.context["run_id"],
                            "pid": self.context["pid"],
                            "sequence": sequence,
                            "age_seconds": age,
                            "identity_binding": self.context["telemetry_binding"],
                            "readiness": "settled_loaded_active_gameplay",
                            "stable_samples": settled_samples,
                            "stable_identity": identity,
                        }
                        return result
                else:
                    settled_samples = 0
                previous_identity = identity if loaded else None
                previous_sequence = sequence
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                pass
            time.sleep(max(0.01, float(poll_seconds)))
        raise AutonomyError(
            "TEST_READY_TIMEOUT: active loaded gameplay was not observed"
        )

    def release(self) -> None:
        if self._owned_process_running():
            raise AutonomyError("CLEANUP_REFUSED_PROCESS_STILL_RUNNING: close the game first")
        if self.context is not None:
            self.context["state"] = "released"
            self.context["released_at_utc"] = utc_now()
            _atomic_json(self.runtime_dir / "run-context.json", self.context)
        self.lease.release()

    def close_game(
        self, timeout_seconds: float = DEFAULT_CLOSE_TIMEOUT_SECONDS, force: bool = True
    ) -> dict[str, Any]:
        """Post WM_CLOSE, wait a bounded time, then force-terminate if ``force``.

        Only allow-listed Rebirth processes are terminated (the user allows
        force-closing the game; live, graceful closes hung in post-match and
        dialogue states and needed a manual End Task). Keep the lifecycle
        lease if the game still remains running: a failed close must never
        authorize installation or launching a duplicate process.
        """
        self._load_durable_context()
        # The launcher can remain the only PID in the durable context while
        # the game child finishes starting.  Rebind the verified gameplay PID
        # before selecting the WM_CLOSE target; otherwise a close request is
        # posted to the launcher and the owned game survives, which then
        # correctly prevents probe installation and duplicate launches.
        self._bind_target_process()
        timeout = max(0.1, min(MAX_CLOSE_TIMEOUT_SECONDS, float(timeout_seconds)))
        watched_pids = {
            int(self.context[key])
            for key in ("pid", "launcher_pid")
            if self.context and self.context.get(key)
        }
        close_window_pids = {
            int(self.context["pid"])
            if self.context and self.context.get("pid")
            else int(self.context["launcher_pid"])
        } if self.context and (
            self.context.get("pid") or self.context.get("launcher_pid")
        ) else set()
        try:
            request_close = self.environment.request_graceful_close
            try:
                request = request_close(target_pids=close_window_pids or None)
            except TypeError:
                # Keep small test/non-Windows adapters compatible while the
                # real environment uses the verified PID filter above.
                request = request_close()
        except AttributeError as exc:
            raise AutonomyError("CLOSE_UNAVAILABLE: environment has no graceful close") from exc

        initial_processes = dict(request.get("processes") or {})
        watch = {
            "watched_pids": sorted(watched_pids),
            "close_window_pids": sorted(close_window_pids),
            "initial_processes": initial_processes,
            "graceful_request_posted": bool(request.get("requested")),
        }
        if not initial_processes:
            result: dict[str, Any] = {
                "ok": True,
                "state": "already_closed",
                "request": request,
                "process_watch": {**watch, "remaining_processes": {}},
            }
        elif not self.context and not request.get("requested") and not force:
            # Without a durable context there is no ownership proof and no
            # verified window to close; without ``force`` do not wait
            # indefinitely.
            remaining = self._bound_process_snapshot()
            result = {
                "ok": False,
                "state": "manual_end_task_required",
                "request": request,
                "reason": "CLOSE_OWNERSHIP_UNKNOWN_AND_GAME_WINDOW_NOT_FOUND",
                "process_watch": {
                    **watch,
                    "remaining_processes": remaining,
                    "failed_pids": self._failed_pid_details(
                        remaining,
                        "ownership_unknown_and_no_verified_window",
                    ),
                },
            }
        else:
            self._wait_for_process_exit(timeout)
            remaining = self._bound_process_snapshot()
            termination = None
            if remaining and force:
                termination = self._force_terminate(remaining)
                remaining = self._bound_process_snapshot()
            if not remaining and termination is not None:
                result = {
                    "ok": True, "state": "force_closed", "request": request,
                    "graceful_wait_seconds": timeout,
                    "termination": termination,
                    "process_watch": {**watch, "remaining_processes": {}},
                }
            elif not remaining:
                result = {
                    "ok": True, "state": "closed", "request": request,
                    "graceful_wait_seconds": timeout,
                    "process_watch": {**watch, "remaining_processes": {}},
                }
            else:
                result = {
                    "ok": False, "state": "manual_end_task_required", "request": request,
                    "reason": "GAME_DID_NOT_EXIT_AFTER_GRACEFUL_CLOSE",
                    "graceful_wait_seconds": timeout,
                    "termination": termination or {"ok": False, "state": "force_not_requested", "terminated_pids": []},
                    "process_watch": {**watch, "remaining_processes": remaining,
                        "failed_pids": self._failed_pid_details(
                            remaining, "force_termination_failed" if force else "force_not_requested")},
                }
        if not result.get("ok"):
            result["lifecycle_released"] = False
            return result
        if self.lease.lease_id is not None:
            self.release()
            result["lifecycle_released"] = True
        elif self.lease.status() is not None:
            result["lifecycle_recovery"] = self.recover_stale()
            result["lifecycle_released"] = True
        else:
            result["lifecycle_released"] = False
        return result
