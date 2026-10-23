from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import traceback
import uuid
from typing import Any

from .factory import (
    create_lifecycle_controller, create_service, create_startup_coordinator,
    start_service_validator_prewarm,
)
from .runtime_identity import collect_identity
from .runtime_commands import invoke_runtime
from .service import SafetyError


def _error_reply(exc: Exception, request: Any, operation: Any) -> dict[str, Any]:
    reply = {
        "ok": False,
        "id": request.get("id") if isinstance(request, dict) else None,
        "error": str(exc),
        "error_type": type(exc).__name__,
        "traceback": traceback.format_exc(limit=8),
        "__ff7r_operation": operation if isinstance(operation, str) else None,
    }
    if isinstance(exc, SafetyError) and isinstance(exc.details, dict):
        reply["details"] = exc.details
    return reply


def _install_remote_gamepad_if_requested() -> None:
    port = os.getenv("FF7R_GAMEPAD_BROKER_PORT")
    if not port:
        return
    from .gamepad_broker import RemoteVirtualLeftStick
    from .input import WindowsHybridGamepadInput

    # The worker may restart repeatedly while the broker keeps the real virtual
    # controller alive. Only construction is redirected; the audited hybrid
    # input implementation and its normal safety/release paths remain intact.
    WindowsHybridGamepadInput._create_gamepad = staticmethod(
        lambda: RemoteVirtualLeftStick(int(port))
    )


_ARTIFACT_RESULT_OPERATIONS = frozenset({
    "run_live_route_adapter",
    "capture_topology_cache",
})
_RESULT_ARTIFACT_SCHEMA = "ff7r-runtime-result-v1"


def _result_artifact_root() -> Path:
    configured = os.getenv("FF7R_RESULT_ARTIFACT_ROOT")
    if configured:
        return Path(configured)
    local_app_data = os.getenv("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "ff7r-agent" / "runtime-results"
    return Path(tempfile.gettempdir()) / "ff7r-runtime-results"


class _HashingTextWriter:
    """Stream JSON text to a buffered binary file while hashing exact UTF-8."""

    def __init__(self, stream: Any) -> None:
        self.stream = stream
        self.digest = hashlib.sha256()
        self.bytes_written = 0

    def write(self, value: str) -> int:
        raw = value.encode("utf-8")
        self.stream.write(raw)
        self.digest.update(raw)
        self.bytes_written += len(raw)
        return len(value)


def _write_result_artifact(
    value: dict[str, Any],
    *,
    operation: str,
) -> dict[str, Any]:
    root = _result_artifact_root()
    root.mkdir(parents=True, exist_ok=True)
    response_id = value.get("id")
    name = f"result-{os.getpid()}-{uuid.uuid4().hex}.json"
    path = root / name
    temporary = root / f".{name}.tmp"
    try:
        with temporary.open("wb") as stream:
            writer = _HashingTextWriter(stream)
            json.dump(value, writer, separators=(",", ":"), default=str)
            stream.flush()
            os.fsync(stream.fileno())
            digest = writer.digest.hexdigest()
            byte_count = writer.bytes_written
        temporary.replace(path)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return {
        "schema": _RESULT_ARTIFACT_SCHEMA,
        "path": str(path),
        "sha256": digest,
        "bytes": byte_count,
        "operation": operation,
        "response_id": response_id,
        "worker_pid": os.getpid(),
    }


def _reply(value: dict[str, Any], stream=None) -> None:
    operation = value.pop("__ff7r_operation", None)
    if operation in _ARTIFACT_RESULT_OPERATIONS:
        artifact = _write_result_artifact(value, operation=operation)
        envelope = dict(value)
        envelope["result"] = {
            "artifact": artifact,
            "response_available": True,
        }
        raw = json.dumps(envelope, separators=(",", ":"), default=str).encode("utf-8")
    else:
        raw = json.dumps(value, separators=(",", ":"), default=str).encode("utf-8")
    stream = sys.stdout if stream is None else stream
    stream.buffer.write(raw + b"\n")
    stream.flush()


def main() -> None:
    from .process_priority import raise_harness_priority
    raise_harness_priority()
    _install_remote_gamepad_if_requested()
    service = create_service(defer_validator_prewarm=True)
    lifecycle = create_lifecycle_controller()
    startup = create_startup_coordinator(lifecycle, service)
    broker_port = os.getenv("FF7R_GAMEPAD_BROKER_PORT")
    _reply({
        "ok": True,
        "event": "runtime_ready",
        "pid": os.getpid(),
        "identity": collect_identity(
            process_role="reloadable-worker",
            module_path=__file__,
            worker_pid=os.getpid(),
            bridge_identity={
                "name": "ff7r-agent-reloadable-worker",
                "entrypoint": "ff7r_agent.runtime_worker:main",
                "pid": os.getpid(),
            },
            broker_identity={
                "pid": None,
                "port": int(broker_port) if broker_port else None,
                "active": False,
                "device_active": False,
                "process_alive": bool(broker_port),
                "listener_alive": bool(broker_port),
                "status": "inherited_from_stable_bridge",
            },
        ),
    })

    # JSON decoding and analyzer construction can monopolize the Python GIL.
    # They must not compete with the identity-bearing startup handshake. The
    # route-time binder still validates every speculative cache identity.
    start_service_validator_prewarm(service)

    released = False

    def release_once() -> None:
        nonlocal released
        if released:
            return
        released = True
        # This is the final neutralisation boundary for both explicit
        # shutdown and ordinary stdin EOF.  It never replays a request.
        service.input.release_all()

    try:
        for raw in sys.stdin:
            request: dict[str, Any] | None = None
            try:
                request = json.loads(raw)
                request_id = request.get("id")
                operation = str(request.get("operation") or "")
                if operation == "__shutdown__":
                    release_once()
                    reply = {
                        "ok": True,
                        "id": request_id,
                        "result": {"shutdown": True},
                        "__ff7r_operation": operation,
                    }
                    _reply(reply)
                    return
                if operation == "run_setup_queens_blood_match":
                    # Milestones share the existing protocol pipe; status
                    # calls read cached progress and never scan the game.
                    from contextlib import redirect_stdout
                    protocol_stream = sys.stdout
                    service._match_setup_progress = lambda progress: _reply({
                        "ok": True, "id": request_id, "event": "match_setup_progress", "progress": progress,
                    }, stream=protocol_stream)
                    try:
                        with redirect_stdout(sys.stderr):
                            result = invoke_runtime(service, lifecycle, startup, operation, request.get("arguments"))
                    finally:
                        del service._match_setup_progress
                else:
                    result = invoke_runtime(service, lifecycle, startup, operation, request.get("arguments"))
                reply = {
                    "ok": True,
                    "id": request_id,
                    "result": result,
                    "__ff7r_operation": operation,
                }
                _reply(reply)
            except Exception as exc:
                _reply(_error_reply(exc, request, operation))
    finally:
        # EOF, including a parent/transport disconnect, is a normal cleanup
        # path rather than a reason to leave held input behind.
        try:
            release_once()
        except Exception:
            # The process is exiting; do not turn cleanup failure into a
            # replayable transport request or a second input command.
            pass
        resources = getattr(service, "_validated_navigation_resources", None)
        recorder = getattr(service, "_qb_recorder", None)
        if recorder is not None:
            recorder.stop()
        if resources is not None:
            close = getattr(resources[2], "close", None)
            if callable(close):
                close()


if __name__ == "__main__":
    main()
