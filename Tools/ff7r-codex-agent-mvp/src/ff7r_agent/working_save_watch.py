"""Watch one verified working run; recover once after proven process loss.

Run through a native Windows scheduled task, like the other harness drivers,
so stream state and the MCP runtime share the same AppData view. No service is
started and no process is killed by this watcher. Ctrl+C/timeout disarms it.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any

from .navigation_mission import feature_identity_check, unwrap
from .queen_blood_profile import _now, _read, _write
from .safety import WindowsGameEnvironment
from .working_save_recovery import process_lost


async def watch(rpc, environment, *, identity_check, watch_seconds=1800.0,
                recovery_seconds=600.0, rpc_timeout_seconds=30.0,
                clock=time.monotonic, sleep=asyncio.sleep) -> dict[str, Any]:
    if (not 1 <= watch_seconds <= 43200 or not 1 <= recovery_seconds <= 900
            or not 0 < rpc_timeout_seconds <= 60):
        raise ValueError("watch_time_budget_invalid")
    armed, lost = None, False

    async def call(operation, arguments, deadline=None):
        remaining = rpc_timeout_seconds if deadline is None else min(rpc_timeout_seconds, deadline - clock())
        if remaining <= 0:
            raise ValueError("watch_recovery_timeout")
        try:
            return unwrap(await asyncio.wait_for(rpc(operation, arguments), timeout=remaining))
        except asyncio.TimeoutError as exc:
            raise ValueError("watch_rpc_timeout:" + operation) from exc

    async def stop_input(deadline=None):
        response = await call("stop_all_input", {}, deadline)
        if response.get("ok") is not True:
            raise ValueError("watch_input_stop_unverified")

    try:
        identity = await call("get_runtime_identity", {})
        bootstrap = await call("get_bootstrap_verification", {})
        if identity_check(identity, bootstrap).get("ok") is not True:
            return {"ok": False, "decision": "watch_runtime_identity_unverified", "input_sent": False}
        target = await call("arm_working_save_recovery", {})
        if (target.get("ok") is not True
                or not isinstance(target.get("recovery_id"), str) or not re.fullmatch(r"[a-f0-9]{32}", target["recovery_id"])
                or not isinstance(target.get("process"), dict)
                or any(not isinstance(target["process"].get(k), str) or not target["process"][k] for k in ("pid", "token"))
                or not str(target["process"]["pid"]).isdecimal()
                or not isinstance(target.get("receipt_path"), str) or not Path(target["receipt_path"]).is_absolute()
                or Path(target["receipt_path"]).name != "working-save-recovery.json"
                or not isinstance(target.get("stream_state_directory"), str) or not Path(target["stream_state_directory"]).is_absolute()):
            raise ValueError("watch_arm_response_invalid")
        armed = target
        receipt = Path(armed["receipt_path"])
        public = Path(armed["stream_state_directory"]) / "public-working-save-recovery.json"

        def phase(value):
            _write(public, {"version": 1, "recoveryId": armed["recovery_id"], "phase": value, "at": _now()})

        deadline = clock() + watch_seconds
        while clock() < deadline:
            current = _read(receipt)
            if current.get("recovery_id") != armed["recovery_id"] or current.get("phase") != "armed":
                return {"ok": True, "state": "disarmed", "input_sent": False}
            if process_lost(environment, armed["process"]):
                lost = True
                phase("recovering")  # independent of the busy input worker
                recovery_deadline = clock() + recovery_seconds
                await stop_input(recovery_deadline)
                # Successful Save As can refresh this same watcher's target.
                # Re-read after cancellation: a save may have completed between
                # the first receipt read and the process-loss observation.
                current = _read(receipt)
                if (current.get("version") != 1 or current.get("recovery_id") != armed["recovery_id"]
                        or current.get("phase") != "armed" or current.get("process") != armed["process"]):
                    raise ValueError("watch_recovery_target_changed")
                expected_slot = current["file"]["slot"]
                # Cancellation may have left an uncertain-save lock. Recovery
                # refuses that lock and holds BRB for review rather than guessing.
                job = await call("recover_working_save_async", {"recovery_id": armed["recovery_id"]}, recovery_deadline)
                if job.get("ok") is not True or not isinstance(job.get("job_id"), str) or not job["job_id"]:
                    raise ValueError("watch_recovery_job_unverified")
                while clock() < recovery_deadline:
                    status = await call("get_recover_working_save_status", {"job_id": job["job_id"]}, recovery_deadline)
                    if status.get("ok") is not True or status.get("job_id") != job["job_id"]:
                        raise ValueError("watch_recovery_status_unverified")
                    if status.get("state") == "completed":
                        response = unwrap(status.get("response") or {})
                        if (response.get("ok") is not True or response.get("state") != "recovered"
                                or response.get("recovery_id") != armed["recovery_id"]
                                or response.get("slot") != expected_slot):
                            raise ValueError("watch_recovery_response_unverified")
                        return response
                    if status.get("state") in {"failed", "cancelled"}:
                        raise ValueError("watch_recovery_job_failed")
                    await sleep(0.5)
                raise ValueError("watch_recovery_timeout")
            await sleep(0.5)
        return {"ok": True, "state": "watch_timeout_disarmed", "input_sent": False}
    except asyncio.CancelledError:
        if armed and lost:
            phase("failed")
            try:
                await stop_input()
            except Exception:
                pass
            phase("failed")
        raise
    except Exception as exc:
        # An unreadable enumeration, reused PID or replacement game does not
        # prove a crash, but must not leave process-only scene restoration
        # free to expose an unverified world after this opt-in watcher stops.
        if armed:
            phase("failed")
            if lost:
                # A refused/timed-out status does not prove the worker stopped.
                # Cancel it once, without replay or a second recovery attempt.
                try:
                    await stop_input()
                except Exception:
                    pass
                phase("failed")  # cancellation cannot clear the failure cover
        return {"ok": False, "decision": str(exc), "state": "watch_stopped", "requires_review": armed is not None}
    finally:
        if armed:
            current = _read(Path(armed["receipt_path"]))
            if current.get("recovery_id") == armed["recovery_id"] and current.get("phase") == "armed":
                _write(Path(armed["receipt_path"]), {**current, "phase": "disarmed", "at": _now()})


async def run_http(args) -> dict[str, Any]:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    async with streamablehttp_client("http://127.0.0.1:8765/mcp") as (read, write, _):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), timeout=args.rpc_timeout_seconds)
            async def rpc(operation, arguments):
                direct = operation in {"get_runtime_identity", "get_bootstrap_verification"}
                response = await session.call_tool(operation if direct else "agent_call",
                    {} if direct else {"operation": operation, "arguments": arguments})
                if response.isError:
                    raise ValueError("watch_mcp_call_refused")
                return response.structuredContent or json.loads(next(c.text for c in response.content if c.type == "text"))
            return await watch(rpc, WindowsGameEnvironment(),
                identity_check=feature_identity_check(args.repository_root, args.feature_ref, args.expected_commit),
                watch_seconds=args.watch_seconds, recovery_seconds=args.recovery_seconds,
                rpc_timeout_seconds=args.rpc_timeout_seconds)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Arm one recovery attempt; otherwise no MCP calls or input.")
    parser.add_argument("--feature-ref", required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--repository-root", type=Path, default=Path(__file__).resolve().parents[4])
    parser.add_argument("--watch-seconds", type=float, default=1800)
    parser.add_argument("--recovery-seconds", type=float, default=600)
    parser.add_argument("--rpc-timeout-seconds", type=float, default=30,
                        help="Bound each MCP wait, including input stop; 0 < seconds <= 60.")
    args = parser.parse_args(argv)
    if not args.execute:
        print(json.dumps({"ok": True, "state": "dry_run", "input_sent": False}))
        return 0
    try:
        result = asyncio.run(run_http(args))
    except KeyboardInterrupt:
        return 130
    print(json.dumps(result, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
