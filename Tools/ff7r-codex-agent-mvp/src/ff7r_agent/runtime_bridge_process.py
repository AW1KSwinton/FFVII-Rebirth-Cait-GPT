"""Fresh source-bound bridge runtime used during an in-flight rebind."""

from __future__ import annotations

import json
import sys
from typing import Any

from .runtime_bridge import (
    INTERNAL_ACTIVE_ASYNC_JOB_OPERATION,
    RuntimeInfrastructureError,
    RuntimeSupervisor,
)
from .runtime_profile import profile_contract_from_environment, validate_current_checkout, verify_runtime_identity


def _write(value: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(value, separators=(",", ":"), default=str) + "\n")
    sys.stdout.flush()


def main() -> None:
    supervisor = RuntimeSupervisor()
    contract = profile_contract_from_environment()
    if contract is not None:
        validate_current_checkout(contract)
    identity = supervisor.identity()
    verification = verify_runtime_identity(
        identity,
        contract=contract,
        worker_identity=identity.get("worker_identity"),
    )
    if not verification.get("ok"):
        raise RuntimeError("fresh bridge source binding verification failed: " + json.dumps(verification, sort_keys=True))
    _write({"ok": True, "event": "bridge_ready", "identity": identity})
    try:
        for raw in sys.stdin:
            request = json.loads(raw)
            request_id = request.get("id")
            operation = str(request.get("operation") or "")
            if operation == "__bridge_shutdown__":
                _write({"ok": True, "id": request_id, "result": {"ok": True}})
                return
            if operation == INTERNAL_ACTIVE_ASYNC_JOB_OPERATION:
                _write({
                    "ok": True,
                    "id": request_id,
                    "result": {"active_job": supervisor.active_async_job()},
                })
                continue
            try:
                result = supervisor.call(operation, request.get("arguments"))
            except RuntimeInfrastructureError as exc:
                result = exc.as_response()
            except Exception as exc:
                result = {"ok": False, "error": str(exc), "error_type": type(exc).__name__}
            _write({"ok": True, "id": request_id, "result": result})
    finally:
        supervisor.close()


if __name__ == "__main__":
    main()
