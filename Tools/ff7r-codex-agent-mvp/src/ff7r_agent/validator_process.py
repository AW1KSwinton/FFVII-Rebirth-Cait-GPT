"""Input-free process isolation for the exact standalone route validator.

The parent keeps topology admission and every binding check. Only construction
and calls to the unchanged independent implementation run in this helper.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import uuid
from typing import Any

# Construction reads the whole capture: live, Kalm's 35 MB one took 74 s with
# the game and OBS running, so at 60 s every route there timed out before a
# validator was ever built and cached.
VALIDATOR_READY_TIMEOUT_SECONDS = 180.0


class ValidatorProcess:
    def __init__(self, source: Path, artifact: Path, *, source_sha: str, artifact_sha: str):
        self._lock = threading.Lock()
        self._replies: queue.Queue[str] = queue.Queue()
        self._request_id = 0
        self._closed = False
        self._nonce = uuid.uuid4().hex
        self.worker_pid = None
        self.process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), str(source.resolve()),
             source_sha, str(artifact.resolve()), artifact_sha, self._nonce],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", bufsize=1,
            # This module is also executed directly as a standalone script by
            # the isolated child, so keep its launcher independent of package
            # imports while suppressing a Windows console window.
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000 if os.name == "nt" else 0),
        )
        def read():
            try:
                for line in self.process.stdout:
                    self._replies.put(line)
            finally:
                self._replies.put("")
        threading.Thread(target=read, name="ff7r-validator-results", daemon=True).start()

    def _response(self, timeout: float) -> dict[str, Any]:
        try:
            line = self._replies.get(timeout=timeout)
            value = json.loads(line)
            if not isinstance(value, dict) or value.get("ok") is not True:
                raise RuntimeError("independent_validator_process_refused:" + str(value))
            return value
        except (queue.Empty, ValueError, RuntimeError) as exc:
            self.close()
            raise RuntimeError("independent_validator_process_unavailable") from exc

    def wait_ready(self, timeout: float = VALIDATOR_READY_TIMEOUT_SECONDS) -> None:
        result = self._response(timeout)
        # Windows venv launchers may have a different PID from their Python
        # child. The private pipe and per-launch nonce bind this handshake.
        if (result.get("event") != "validator_ready" or result.get("nonce") != self._nonce
                or type(result.get("pid")) is not int or result["pid"] <= 0):
            self.close()
            raise RuntimeError("independent_validator_process_identity_mismatch")
        self.worker_pid = result["pid"]

    def validate(self, plan: Any) -> dict[str, Any]:
        with self._lock:
            if self._closed or self.process.poll() is not None:
                raise RuntimeError("independent_validator_process_closed")
            self._request_id += 1
            request_id = self._request_id
            try:
                self.process.stdin.write(json.dumps({"id": request_id, "plan": plan}, allow_nan=False) + "\n")
                self.process.stdin.flush()
                result = self._response(10.0)
            except (OSError, ValueError) as exc:
                self.close()
                raise RuntimeError("independent_validator_process_transport_failed") from exc
            if result.get("id") != request_id or not isinstance(result.get("result"), dict):
                self.close()
                raise RuntimeError("independent_validator_process_response_mismatch")
            return result["result"]

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            self.process.terminate()  # Only this owned, input-free helper.
            self.process.wait(timeout=2.0)


def _main() -> None:
    # EOF is also the owner-death boundary during expensive construction. It
    # must not leave speculative CPU work running after the worker disappears.
    requests: queue.Queue[str] = queue.Queue()
    def read():
        for line in sys.stdin:
            requests.put(line)
        os._exit(0)
    threading.Thread(target=read, name="ff7r-validator-owner", daemon=True).start()

    def reply(value):
        print(json.dumps(value, allow_nan=False, separators=(",", ":")), flush=True)

    try:
        import types
        source, source_sha, artifact, artifact_sha, nonce = sys.argv[1:]
        source_path, artifact_path = Path(source), Path(artifact)
        source_bytes = source_path.read_bytes()
        if hashlib.sha256(source_bytes).hexdigest().upper() != source_sha.upper():
            raise ValueError("validator_source_identity_mismatch")
        before = artifact_path.stat()
        with artifact_path.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest().upper()
        if digest != artifact_sha.upper():
            raise ValueError("validator_artifact_identity_mismatch")
        module = types.ModuleType("ff7r_isolated_independent_validator")
        module.__file__ = str(source_path)
        sys.modules[module.__name__] = module
        exec(compile(source_bytes, str(source_path), "exec"), module.__dict__)
        validator = module.RoutePlanValidator(module.TopologyAnalyzer(artifact_path))
        after = artifact_path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError("validator_artifact_changed_during_construction")
        reply({"ok": True, "event": "validator_ready", "pid": os.getpid(), "nonce": nonce})
        while True:
            request = json.loads(requests.get())
            result = validator.validate(request["plan"])
            reply({"ok": True, "id": request["id"], "result": result})
    except Exception as exc:
        reply({"ok": False, "error": type(exc).__name__, "detail": str(exc)[:512]})


if __name__ == "__main__":
    _main()
