"""Loopback Streamable HTTP entrypoint for one shared FF7R MCP bridge.

Codex starts a separate process for each configured stdio MCP connection.  The
HTTP entrypoint keeps one bridge/supervisor process alive and lets multiple
Codex tasks share that one bounded runtime through the existing supervisor
lock and lifecycle ownership gates.  It binds to loopback by default and does
not change any game or input operation.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Callable

from .mcp_runtime_server import _close_runtime, _runtime, mcp
from .runtime_identity import default_runtime_directory
from .runtime_profile import profile_identity_fields, verify_current_process_binding
from .runtime_ownership import process_identity_token


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_PATH = "/mcp"
HTTP_ENTRYPOINT = "ff7r_agent.mcp_http_server"
HTTP_IDENTITY_FILENAME = "server.identity.json"
HTTP_SHUTDOWN_PROTOCOL = "ff7r-http-shutdown-v1"


def validate_http_process_identity(
    identity: object,
    *,
    pid: int,
    process_creation_token: str | None,
    command_line: str | None,
) -> tuple[bool, str]:
    """Apply the non-secret PID identity contract used by local stop tooling."""

    if not isinstance(identity, dict):
        return False, "missing or invalid HTTP process identity"
    if int(identity.get("pid") or 0) != int(pid):
        return False, "HTTP PID identity mismatch"
    if identity.get("entrypoint") != HTTP_ENTRYPOINT:
        return False, "HTTP entrypoint identity mismatch"
    if identity.get("command_identity") != "python -m ff7r_agent.mcp_http_server":
        return False, "HTTP command identity mismatch"
    if identity.get("transport") != "streamable-http" or identity.get("protocol") != HTTP_SHUTDOWN_PROTOCOL:
        return False, "HTTP transport/protocol identity mismatch"
    if not process_creation_token or str(identity.get("process_creation_token") or "") != str(process_creation_token):
        return False, "HTTP process creation identity mismatch"
    if command_line is None:
        return True, ""
    command = str(command_line or "")
    import re

    if not re.search(r"(?:^|\s)ff7r_agent\.mcp_http_server(?:\s|$)", command):
        return False, "HTTP entrypoint command identity mismatch"
    return True, ""


def _http_runtime_dir() -> Path:
    configured = os.getenv("FF7R_MCP_HTTP_RUNTIME_DIR")
    if configured:
        return Path(configured).resolve()
    base = default_runtime_directory()
    return (base or Path.cwd()) / "mcp-http"


def _control_paths() -> tuple[Path, Path, Path]:
    runtime_dir = _http_runtime_dir()
    return runtime_dir / "server.token", runtime_dir / "shutdown.request", runtime_dir / "shutdown.complete"


def _identity_path() -> Path:
    return _http_runtime_dir() / HTTP_IDENTITY_FILENAME


def _prepare_shutdown_control() -> str:
    token_path, request_path, complete_path = _control_paths()
    token_path.parent.mkdir(parents=True, exist_ok=True)
    for path in (request_path, complete_path):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    try:
        _identity_path().unlink()
    except FileNotFoundError:
        pass
    token = secrets.token_hex(32)
    token_path.write_text(token + "\n", encoding="utf-8")
    return token


def _write_process_identity() -> dict[str, object]:
    """Persist non-secret PID identity before the HTTP listener starts."""

    pid = os.getpid()
    creation_token = process_identity_token(pid)
    if os.name == "nt" and not creation_token:
        raise RuntimeError("unable to prove HTTP process creation identity")
    identity = {
        "schema_version": 1,
        "pid": pid,
        "process_creation_token": creation_token,
        "entrypoint": HTTP_ENTRYPOINT,
        "command_identity": "python -m ff7r_agent.mcp_http_server",
        "transport": "streamable-http",
        "protocol": HTTP_SHUTDOWN_PROTOCOL,
        "shutdown_token_file": str(_control_paths()[0]),
    }
    identity.update(profile_identity_fields())
    _identity_path().write_text(json.dumps(identity, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return identity


def _watch_shutdown_request(
    *,
    pid: int,
    token: str,
    poll_seconds: float = 0.05,
    exit_fn: Callable[[int], None] = os._exit,
) -> None:
    """Honor only the local launcher’s tokenized graceful-stop request."""

    _, request_path, complete_path = _control_paths()
    while True:
        try:
            # Windows PowerShell 5.1 may emit a UTF-8 BOM even when the
            # launcher requests UTF-8. Accept it defensively; the launcher
            # also writes BOM-free JSON explicitly.
            request = json.loads(request_path.read_text(encoding="utf-8-sig"))
        except (FileNotFoundError, OSError, ValueError):
            time.sleep(poll_seconds)
            continue
        if request.get("pid") != pid or request.get("token") != token:
            time.sleep(poll_seconds)
            continue
        # RuntimeSupervisor.close() serializes against in-flight work, sends
        # the worker's explicit shutdown/release, then closes the broker.
        _close_runtime()
        complete_path.write_text(json.dumps({"ok": True, "pid": pid}) + "\n", encoding="utf-8")
        exit_fn(0)
        return


def configure_http_server() -> dict[str, object]:
    """Apply explicit loopback HTTP settings and return the effective config."""

    host = os.getenv("FF7R_MCP_HTTP_HOST", DEFAULT_HOST).strip() or DEFAULT_HOST
    port = int(os.getenv("FF7R_MCP_HTTP_PORT", str(DEFAULT_PORT)))
    path = os.getenv("FF7R_MCP_HTTP_PATH", DEFAULT_PATH).strip() or DEFAULT_PATH
    if not 1 <= port <= 65535:
        raise ValueError("FF7R_MCP_HTTP_PORT must be between 1 and 65535")
    if not path.startswith("/"):
        raise ValueError("FF7R_MCP_HTTP_PATH must start with '/'")
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("FF7R MCP HTTP server must remain loopback-only")
    os.environ["FF7R_MCP_TRANSPORT"] = "streamable-http"
    os.environ["FF7R_MCP_BRIDGE_NAME"] = "ff7r-agent-mcp-http"
    os.environ["FF7R_MCP_BRIDGE_ENTRYPOINT"] = "ff7r_agent.mcp_http_server:main"
    mcp.settings.host = host
    mcp.settings.port = port
    mcp.settings.streamable_http_path = path
    mcp.settings.stateless_http = False
    return {
        "host": host,
        "port": port,
        "path": path,
        "transport": "streamable-http",
        "bridge_name": "ff7r-agent-mcp-http",
        "stateless": False,
    }


def main() -> None:
    from .process_priority import raise_harness_priority
    raise_harness_priority()
    configure_http_server()
    # Bind the stable bridge before entering the HTTP loop. Live calls can
    # then distinguish a checkout advance from the process revision.
    _runtime()
    profile_verification = verify_current_process_binding()
    if not profile_verification.get("ok"):
        raise RuntimeError(
            "refusing to bind FF7R HTTP MCP because the bridge source binding is not verified: "
            + json.dumps(profile_verification, sort_keys=True)
        )
    token = _prepare_shutdown_control()
    identity = _write_process_identity()
    identity["runtime_profile_verification"] = profile_verification
    _identity_path().write_text(json.dumps(identity, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    threading.Thread(
        target=_watch_shutdown_request,
        kwargs={"pid": os.getpid(), "token": token},
        daemon=True,
        name="ff7r-http-shutdown-watchdog",
    ).start()
    # Start the runtime worker now rather than on the first request: with the
    # game running it takes 15-25 s, and the first call after a restart
    # failed with "runtime worker startup timed out" (live 2026-10-05). A
    # request arriving meanwhile waits on the supervisor lock.
    threading.Thread(target=lambda: _runtime().status(), daemon=True, name="ff7r-worker-prewarm").start()
    try:
        mcp.run(transport="streamable-http")
    finally:
        # Covers framework-level normal exit.  The tokenized watcher handles
        # the production PowerShell stop path before process termination.
        _close_runtime()


if __name__ == "__main__":
    main()
