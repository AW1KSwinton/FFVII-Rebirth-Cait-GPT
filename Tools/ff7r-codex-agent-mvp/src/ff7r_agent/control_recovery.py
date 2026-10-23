"""Input-free recovery of legacy control leases left behind by an exited game."""
from __future__ import annotations

import os
from typing import Any

from .safety import ArmError
from .service import AgentService


def recover_stale_lifecycle(service: Any, lifecycle: Any,
                            expected_control_session: dict | None = None) -> dict:
    if expected_control_session is None:
        return lifecycle.recover_stale()
    lease = getattr(service, "_session_lease", None)
    if lease is None or not isinstance(expected_control_session, dict):
        raise ArmError("legacy_control_session_recovery_unverified")
    status = lifecycle.status()
    context = status.get("run_context") or {}
    lifecycle_lease = status.get("lease") or {}
    game_pid = expected_control_session.get("game_pid")
    controller_pid = expected_control_session.get("controller_pid")
    # Join the legacy control record to the independently stored controller/run.
    # A null/foreign client is allowed only after this binding is proved dead.
    if (expected_control_session.get("owner_matches") is not True
            or expected_control_session.get("state") != "OPEN"
            or expected_control_session.get("lifetime") != "wall_clock"
            or expected_control_session.get("owner_id") != lease.owner_id
            or lifecycle.owner_id != lease.owner_id
            or type(game_pid) is not int or game_pid <= 0
            or type(controller_pid) is not int or controller_pid <= 0
            or not isinstance(context, dict) or not isinstance(lifecycle_lease, dict)
            or context.get("owner_id") != lease.owner_id
            or type(context.get("pid")) is not int or context.get("pid") != game_pid
            or lifecycle_lease.get("owner_id") != lease.owner_id
            or type(lifecycle_lease.get("pid")) is not int or lifecycle_lease.get("pid") != controller_pid
            or not isinstance(context.get("executable_path"), str) or not context["executable_path"]
            or not isinstance(status.get("executable_path"), str) or not status["executable_path"]
            or os.path.normcase(os.path.abspath(context["executable_path"])) !=
               os.path.normcase(os.path.abspath(status["executable_path"]))
            or status.get("owned_process_running") is not False):
        raise ArmError("legacy_control_session_lifecycle_unverified")
    health = service.session_health()
    input_status = service.input_status()
    if health.get("process_exists") is not False or health.get("gameplay_ready") is not False:
        raise ArmError("legacy_control_session_exit_unverified")
    if (input_status.get("real_input") is not True
            or not AgentService._warm_input_is_neutral(input_status)):
        raise ArmError("legacy_control_session_input_not_neutral")

    def verify_no_game():
        # No permissive/cache-backed fallback: a failed scan is not absence.
        probe = getattr(lifecycle.environment, "game_process_ids_verified", None)
        if not callable(probe):
            raise ArmError("legacy_game_process_probe_unavailable")
        try:
            processes = probe()
        except Exception as exc:
            raise ArmError("legacy_game_process_probe_unreadable") from exc
        if not isinstance(processes, dict) or processes:
            raise ArmError("legacy_game_process_absence_unverified")

    # The bridge serialises this operation and rejects it while any input job
    # runs. Revalidate the record and native process evidence under its mutex.
    closed = lease.recover_legacy_exited_game(expected_control_session, verify_no_game)
    recovered = lifecycle.recover_stale()
    if recovered.get("recovered") is not True:
        raise ArmError("legacy_lifecycle_recovery_failed")
    return {**recovered, "control_session": closed}
