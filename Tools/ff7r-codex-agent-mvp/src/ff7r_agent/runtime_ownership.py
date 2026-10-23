"""Local FF7R runtime ownership and process-identity safeguards.

The gamepad broker is an input owner, not merely a TCP helper.  This module
provides the OS-level singleton used by the broker and a read-only Windows
inventory used by startup/doctor gates.  Process metadata is diagnostic only;
the named mutex is authoritative for broker ownership.
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import threading
from pathlib import Path
from typing import Any
from .subprocess_options import no_console_window_kwargs


BROKER_MUTEX_NAME = r"Local\FF7R-Codex-Gamepad-Broker-v1"
BROKER_OWNERSHIP_PROTOCOL = 1
_WAIT_OBJECT_0 = 0
_WAIT_ABANDONED = 0x80
_WAIT_TIMEOUT = 0x102
_STILL_ACTIVE = 259
_ERROR_ACCESS_DENIED = 5
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SYNCHRONIZE = 0x00100000


class RuntimeOwnershipError(RuntimeError):
    """A validated FF7R runtime owner prevents safe startup."""


def _windows_last_error(kernel32: Any) -> int:
    kernel32.GetLastError.restype = ctypes.c_uint32
    return int(kernel32.GetLastError())


class _ThreadOwnership:
    _lock = threading.Lock()

    def __enter__(self) -> "_ThreadOwnership":
        if not self._lock.acquire(blocking=False):
            raise RuntimeOwnershipError("FF7R gamepad broker ownership conflict")
        return self

    def __exit__(self, *_: object) -> None:
        self._lock.release()


class _WindowsMutexOwnership:
    def __init__(self, handle: Any) -> None:
        self.handle = handle

    def __enter__(self) -> "_WindowsMutexOwnership":
        return self

    def __exit__(self, *_: object) -> None:
        kernel32 = ctypes.windll.kernel32
        kernel32.ReleaseMutex(ctypes.c_void_p(self.handle))
        kernel32.CloseHandle(ctypes.c_void_p(self.handle))


def acquire_broker_ownership() -> _WindowsMutexOwnership | _ThreadOwnership:
    """Acquire the user/session broker singleton before device construction.

    ``WaitForSingleObject`` accepts an abandoned mutex, which is the safe
    recovery path after a dead owner.  A live owner returns timeout and the
    new broker fails before importing/constructing the virtual controller.
    """

    if os.name != "nt":
        return _ThreadOwnership()
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool
    handle = kernel32.CreateMutexW(None, False, BROKER_MUTEX_NAME)
    if not handle:
        raise RuntimeOwnershipError("unable to create FF7R gamepad broker ownership mutex")
    result = kernel32.WaitForSingleObject(handle, 0)
    if result == _WAIT_TIMEOUT:
        kernel32.CloseHandle(handle)
        raise RuntimeOwnershipError("FF7R gamepad broker ownership conflict")
    if result not in {_WAIT_OBJECT_0, _WAIT_ABANDONED}:
        kernel32.CloseHandle(handle)
        raise RuntimeOwnershipError(f"unable to acquire FF7R broker ownership mutex: wait={result}")
    return _WindowsMutexOwnership(handle)


def process_identity_token(pid: int | None) -> str | None:
    """Return a PID-reuse-resistant Windows process creation token."""

    if not pid or os.name != "nt":
        return None
    class _FileTime(ctypes.Structure):
        _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]

    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetProcessTimes.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_FileTime),
        ctypes.POINTER(_FileTime),
        ctypes.POINTER(_FileTime),
        ctypes.POINTER(_FileTime),
    ]
    kernel32.GetProcessTimes.restype = ctypes.c_bool
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return None
    created = _FileTime()
    exited = _FileTime()
    kernel = _FileTime()
    user = _FileTime()
    try:
        if not kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)):
            return None
        value = (int(created.high) << 32) | int(created.low)
        return str(value)
    finally:
        kernel32.CloseHandle(handle)


def owner_is_alive(pid: int | None, identity_token: str | None) -> bool:
    """Validate owner liveness and, on Windows, its creation identity."""

    try:
        pid = int(pid) if pid is not None else 0
    except (TypeError, ValueError, OverflowError):
        return False
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    kernel32.GetExitCodeProcess.restype = ctypes.c_bool
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE, False, pid)
    if not handle:
        # An inaccessible process may still own the lock. Treat only a
        # proven access-denied result as live; invalid/nonexistent PIDs remain
        # recoverable. This preserves fail-closed ownership semantics.
        return _windows_last_error(kernel32) == _ERROR_ACCESS_DENIED
    try:
        code = ctypes.c_uint32(0)
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return _windows_last_error(kernel32) == _ERROR_ACCESS_DENIED
        if int(code.value) != _STILL_ACTIVE:
            return False
    finally:
        kernel32.CloseHandle(handle)
    if identity_token is None:
        return True
    actual_token = process_identity_token(pid)
    # We already observed a live process. If its creation identity cannot be
    # read now, retain the conservative live result rather than recovering a
    # lock on an unproven PID-reuse/access boundary.
    return actual_token is None or actual_token == str(identity_token)


def process_absence_verified(pid: int) -> bool:
    """Prove a PID absent; failed/inaccessible probes are never exit evidence.

    Legacy control leases have no creation token. A live reused PID must
    therefore block recovery even when it is no longer a game executable.
    """
    if type(pid) is not int or pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except OSError:
            return False
        return False
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    kernel32.GetExitCodeProcess.restype = ctypes.c_bool
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE, False, pid)
    if not handle:
        # ERROR_INVALID_PARAMETER is the documented result for a missing PID.
        # PID 0 is excluded above. Access denial and all other errors are unknown.
        return _windows_last_error(kernel32) == 87
    try:
        code = ctypes.c_uint32()
        return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value != _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _role(command_line: str, name: str) -> str | None:
    if str(name).lower() not in {"python", "python.exe", "ff7r-agent-mcp", "ff7r-agent-mcp.exe"}:
        return None
    command = f"{name} {command_line}".lower()
    if "ff7r_agent.mcp_http_server" in command:
        return "http"
    if "ff7r_agent.runtime_worker" in command:
        return "worker"
    if "ff7r_agent.gamepad_broker" in command and "serve" in command:
        return "broker"
    if "ff7r-agent-mcp" in command or "ff7r_agent.mcp_runtime_server" in command:
        return "legacy_stdio"
    return None


def _loopback_tcp_listeners(lines: list[str]) -> list[dict[str, int]]:
    """Parse native netstat output, using the same loopback scope as startup."""
    listeners = []
    for line in lines:
        fields = str(line).split()
        if len(fields) < 2 or fields[0] != "TCP" or not fields[1].startswith("127.0.0.1:"):
            continue
        if len(fields) != 5:
            raise ValueError("malformed loopback TCP inventory row")
        if fields[3] != "LISTENING":
            continue
        port, pid = int(fields[1].rsplit(":", 1)[1]), int(fields[4])
        if not 1 <= port <= 65535 or pid <= 0:
            raise ValueError("invalid loopback TCP listener identity")
        listeners.append({"OwningProcess": pid, "LocalPort": port})
    return listeners


def collect_runtime_ownership_inventory() -> dict[str, Any]:
    """Collect only FF7R process/listener metadata on the local Windows host."""

    if os.name != "nt":
        return {
            "schema_version": 1,
            "platform": os.name,
            "processes": [],
            "listeners": [],
            "http_servers": [],
            "workers": [],
            "authoritative_brokers": [],
            "legacy_runtimes": [],
            "stale_brokers": [],
        }
    script = r'''
$patterns = 'ff7r_agent\.mcp_http_server|ff7r_agent\.runtime_worker|ff7r_agent\.gamepad_broker|ff7r-agent-mcp|ff7r_agent\.mcp_runtime_server'
# Filter by exactly the executable names accepted by _role before asking WMI
# to materialize metadata for every unrelated process on the host.
$processNames = "Name = 'python.exe' OR Name = 'python' OR Name = 'ff7r-agent-mcp.exe' OR Name = 'ff7r-agent-mcp'"
$processes = @(Get-CimInstance Win32_Process -Filter $processNames -ErrorAction Stop |
  Where-Object { $_.CommandLine -and $_.CommandLine -match $patterns } |
  Select-Object ProcessId,ParentProcessId,Name,ExecutablePath,CommandLine,CreationDate)
# Reuse the native listener source used by Start/Stop-FF7RHttpMcp. Avoid the
# expensive NetTCPConnection CIM provider while retaining PID/port evidence.
$listenerLines = @(& "$env:SystemRoot\System32\netstat.exe" -ano -p tcp)
if ($LASTEXITCODE -ne 0) { throw "native listener inventory failed: $LASTEXITCODE" }
[pscustomobject]@{ processes = $processes; listener_lines = $listenerLines } | ConvertTo-Json -Depth 5 -Compress
'''
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True,
            text=True,
            # Read-only startup work may outlast a host hitch. Gameplay
            # freshness and movement watchdogs have separate, unchanged bounds.
            timeout=20.0,
            check=False,
            **no_console_window_kwargs(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"schema_version": 1, "inspection_error": str(exc), "processes": []}
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if result.returncode != 0 or not lines:
        return {"schema_version": 1, "inspection_error": result.stderr.strip() or "process inventory failed", "processes": []}
    try:
        raw = json.loads(lines[-1])
    except ValueError as exc:
        return {"schema_version": 1, "inspection_error": f"invalid process inventory: {exc}", "processes": []}
    raw_processes = raw.get("processes") if isinstance(raw, dict) else []
    try:
        if not isinstance(raw, dict) or not isinstance(raw.get("listener_lines"), list):
            raise ValueError("native listener inventory missing")
        raw_listeners = _loopback_tcp_listeners(raw["listener_lines"])
    except (TypeError, ValueError) as exc:
        return {"schema_version": 1, "inspection_error": f"invalid listener inventory: {exc}", "processes": []}
    if isinstance(raw_processes, dict):
        raw_processes = [raw_processes]
    if isinstance(raw_listeners, dict):
        raw_listeners = [raw_listeners]
    by_pid: dict[int, dict[str, Any]] = {}
    processes: list[dict[str, Any]] = []
    for value in raw_processes or []:
        if not isinstance(value, dict):
            continue
        pid = int(value.get("ProcessId") or 0)
        if not pid:
            continue
        role = _role(str(value.get("CommandLine") or ""), str(value.get("Name") or ""))
        if role is None:
            continue
        record = {
            "pid": pid,
            "ppid": int(value.get("ParentProcessId") or 0),
            "name": value.get("Name"),
            "executable": value.get("ExecutablePath"),
            "command_line": value.get("CommandLine"),
            "creation_time": str(value.get("CreationDate") or ""),
            "role": role,
            "process_alive": True,
        }
        processes.append(record)
        by_pid[pid] = record
    listeners_by_pid: dict[int, list[int]] = {}
    for value in raw_listeners or []:
        if not isinstance(value, dict):
            continue
        pid = int(value.get("OwningProcess") or 0)
        port = int(value.get("LocalPort") or 0)
        if pid in by_pid and port:
            listeners_by_pid.setdefault(pid, []).append(port)
    for record in processes:
        record["listener_ports"] = sorted(set(listeners_by_pid.get(record["pid"], [])))
        ancestry: list[int] = []
        current = record["ppid"]
        seen: set[int] = set()
        while current and current not in seen:
            seen.add(current)
            ancestry.append(current)
            parent = by_pid.get(current)
            if parent is None:
                break
            current = int(parent.get("ppid") or 0)
        record["ancestry_pids"] = ancestry
    def records(role: str) -> list[dict[str, Any]]:
        values = [record for record in processes if record.get("role") == role]
        pids = {int(record["pid"]) for record in values}
        # Windows Python launcher + interpreter pairs share a command line.
        # Count the leaf service, not both processes as two runtimes.
        return [record for record in values if int(record.get("pid") or 0) not in {int(child.get("ppid") or 0) for child in values if int(child.get("ppid") or 0) in pids}]
    brokers = []
    for record in records("broker"):
        if record.get("listener_ports"):
            brokers.append({
                "pid": record["pid"],
                "listener_ports": record["listener_ports"],
                "process_alive": True,
                "listener_alive": True,
                "ancestry_pids": record["ancestry_pids"],
                "command_line": record["command_line"],
            })
    listener_owner_pids = set(listeners_by_pid)
    listener_ancestry = {
        ancestor
        for owner_pid in listener_owner_pids
        for ancestor in (by_pid.get(owner_pid, {}).get("ancestry_pids") or [])
    }
    stale_brokers = [
        record
        for record in records("broker")
        if not record.get("listener_ports")
        and record.get("pid") not in listener_owner_pids
        and record.get("pid") not in listener_ancestry
    ]
    return {
        "schema_version": 1,
        "platform": "win32",
        "processes": processes,
        "listeners": [{"pid": pid, "port": port} for pid, ports in listeners_by_pid.items() for port in sorted(set(ports))],
        "http_servers": [record for record in records("http") if record.get("listener_ports")],
        "workers": records("worker"),
        "authoritative_brokers": brokers,
        "legacy_runtimes": records("legacy_stdio"),
        "stale_brokers": stale_brokers,
    }


def assert_no_existing_broker() -> None:
    """Fail closed before a new broker subprocess can construct a device."""

    inventory = collect_runtime_ownership_inventory()
    if inventory.get("inspection_error"):
        raise RuntimeOwnershipError(f"cannot prove FF7R broker ownership is clear: {inventory['inspection_error']}")
    if inventory.get("authoritative_brokers") or inventory.get("stale_brokers"):
        raise RuntimeOwnershipError("an existing FF7R gamepad broker or stale broker process prevents safe startup")


def ownership_conflicts(
    inventory: dict[str, Any],
    connected_identity: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Return fail-closed ownership conflicts for doctor/readiness checks."""

    conflicts: list[dict[str, Any]] = []
    if inventory.get("inspection_error"):
        return [{"kind": "inspection_error", "detail": inventory["inspection_error"]}]
    http_servers = list(inventory.get("http_servers") or [])
    brokers = list(inventory.get("authoritative_brokers") or [])
    workers = list(inventory.get("workers") or [])
    legacy = list(inventory.get("legacy_runtimes") or [])
    stale = list(inventory.get("stale_brokers") or [])
    if len(http_servers) > 1:
        conflicts.append({"kind": "multiple_http_servers", "count": len(http_servers)})
    if len(brokers) > 1:
        conflicts.append({"kind": "multiple_gamepad_brokers", "count": len(brokers)})
    if legacy and http_servers:
        conflicts.append({"kind": "legacy_stdio_with_http", "count": len(legacy)})
    if len(workers) > 1:
        conflicts.append({"kind": "multiple_runtime_workers", "count": len(workers)})
    if stale:
        conflicts.append({"kind": "stale_actionable_brokers", "count": len(stale)})
    broker_identity = (connected_identity or {}).get("gamepad_broker_identity") or {}
    expected_port = broker_identity.get("port")
    bridge_identity = (connected_identity or {}).get("bridge_identity") or {}
    bridge_pid = bridge_identity.get("pid") or (connected_identity or {}).get("pid")
    worker_pid = (connected_identity or {}).get("worker_pid")
    if brokers and expected_port is not None:
        matching = [broker for broker in brokers if int(expected_port) in [int(port) for port in broker.get("listener_ports", [])]]
        if len(matching) != 1:
            conflicts.append({"kind": "connected_broker_not_unique", "expected_port": expected_port, "matching": len(matching)})
        else:
            ancestry = set(matching[0].get("ancestry_pids") or [])
            # The broker is started with --owner-pid of the long-lived HTTP
            # server and an in-place source rebind reuses it; the rebound
            # bridge process is not its ancestor (live 2026-09-28: every
            # route after a successful rebind refused runtime_refused).
            http_pids = {int(server.get("pid") or 0) for server in http_servers}
            if len(http_servers) == 1 and ancestry & http_pids:
                pass
            elif bridge_pid and int(bridge_pid) not in ancestry and int(matching[0].get("pid") or 0) != int(bridge_pid):
                if worker_pid is None or int(worker_pid) not in ancestry and int(matching[0].get("pid") or 0) != int(worker_pid):
                    conflicts.append({"kind": "broker_not_owned_by_connected_backend", "broker_pid": matching[0].get("pid"), "bridge_pid": bridge_pid, "worker_pid": worker_pid})
    elif brokers and expected_port is None:
        conflicts.append({"kind": "unreported_live_broker", "count": len(brokers)})
    return conflicts
