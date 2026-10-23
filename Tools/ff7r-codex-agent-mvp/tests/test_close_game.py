from __future__ import annotations

from pathlib import Path

from ff7r_agent.autonomy import LifecycleController


class FakeCloseEnvironment:
    def __init__(self, *, has_window: bool, exits_after_request: bool = True):
        self.processes = {2222: "ff7rebirth_.exe"}
        self.has_window = has_window
        self.exits_after_request = exits_after_request
        self.requests = 0

    def game_process_ids(self) -> dict[int, str]:
        return dict(self.processes)

    def request_graceful_close(self) -> dict[str, object]:
        self.requests += 1
        request = {
            "requested": self.has_window,
            "state": "close_requested" if self.has_window else "manual_end_task_required",
            "reason": None if self.has_window else "game_window_not_found",
            "processes": dict(self.processes),
            "windows": [{"hwnd": 1234, "pid": 2222}] if self.has_window else [],
            "posted_windows": [1234] if self.has_window else [],
        }
        if self.has_window and self.exits_after_request:
            self.processes.clear()
        return request

    def game_process_exists(self) -> bool:
        return bool(self.processes)


class DelayedShutdownEnvironment(FakeCloseEnvironment):
    """Model Rebirth's process lingering after the close request/window loss."""

    def __init__(self, *, has_window: bool, polls_until_exit: int = 6):
        super().__init__(has_window=has_window, exits_after_request=False)
        self.polls_until_exit = polls_until_exit
        self.process_polls = 0

    def game_process_ids(self) -> dict[int, str]:
        self.process_polls += 1
        if self.process_polls >= self.polls_until_exit:
            self.processes.clear()
        return dict(self.processes)


class TerminatingCloseEnvironment(FakeCloseEnvironment):
    def __init__(self, *, has_window: bool, exits_after_request: bool = True):
        super().__init__(has_window=has_window, exits_after_request=exits_after_request)
        self.termination_requests = []

    def terminate_game_processes(
        self,
        pids: list[int],
        expected_identities=None,
    ) -> dict[str, object]:
        self.termination_requests.append({
            "pids": list(pids),
            "expected_identities": expected_identities,
        })
        terminated = [pid for pid in pids if pid in self.processes]
        for pid in terminated:
            self.processes.pop(pid, None)
        return {
            "ok": True,
            "state": "termination_requested",
            "terminated_pids": terminated,
        }


class IdentityAwareTerminatingCloseEnvironment(TerminatingCloseEnvironment):
    def game_process_identity_tokens(self, pids):
        return {
            int(pid): f"token-{int(pid)}"
            for pid in pids
            if int(pid) in self.processes
        }


class TargetAwareCloseEnvironment(FakeCloseEnvironment):
    def __init__(self):
        super().__init__(has_window=True)
        self.target_pids = None

    def request_graceful_close(self, target_pids=None):
        self.target_pids = None if target_pids is None else sorted(target_pids)
        result = super().request_graceful_close()
        result["target_pids"] = self.target_pids
        return result


class FailingTerminationEnvironment(FakeCloseEnvironment):
    def __init__(self):
        super().__init__(has_window=True, exits_after_request=False)
        self.processes[3333] = "ff7rebirth.exe"

    def terminate_game_processes(self, pids: list[int]):
        return {
            "ok": False,
            "state": "termination_partial_failure",
            "requested_pids": list(pids),
            "terminated_pids": [],
            "failures": [{"pid": 3333, "reason": "access_denied"}],
        }


def make_controller(tmp_path: Path, environment: FakeCloseEnvironment) -> LifecycleController:
    executable = tmp_path / "ff7rebirth_.exe"
    executable.write_bytes(b"approved-test-executable")
    return LifecycleController(
        executable,
        tmp_path / "state.json",
        tmp_path / "runtime",
        environment=environment,
    )


def make_attached_controller(
    tmp_path: Path, environment: FakeCloseEnvironment
) -> LifecycleController:
    controller = make_controller(tmp_path, environment)
    lease = controller.lease.acquire()
    controller.context = {
        "version": 1,
        "run_id": "attached-run",
        "owner_id": controller.owner_id,
        "lease_id": lease["lease_id"],
        "pid": 2222,
        "launcher_pid": None,
        "executable_path": str(controller.executable),
        "launcher_path": str(controller.launcher_executable),
        "telemetry_path": str(controller.telemetry_path),
        "attachment": "external_or_prior_task",
    }
    return controller


def make_owned_controller(
    tmp_path: Path, environment: FakeCloseEnvironment
) -> LifecycleController:
    controller = make_controller(tmp_path, environment)
    lease = controller.lease.acquire()
    controller.context = {
        "version": 1,
        "run_id": "owned-run",
        "owner_id": controller.owner_id,
        "lease_id": lease["lease_id"],
        "pid": 2222,
        "launcher_pid": None,
        "executable_path": str(controller.executable),
        "launcher_path": str(controller.launcher_executable),
        "telemetry_path": str(controller.telemetry_path),
        "attachment": "existing_controller_run",
    }
    return controller


def test_close_game_posts_graceful_request_and_reports_closed(tmp_path: Path):
    environment = FakeCloseEnvironment(has_window=True)

    result = make_controller(tmp_path, environment).close_game()

    assert result["ok"] is True
    assert result["state"] == "closed"
    assert result["request"]["posted_windows"] == [1234]
    assert environment.requests == 1


def test_close_game_without_force_leaves_a_windowless_game(tmp_path: Path):
    environment = FakeCloseEnvironment(has_window=False)

    result = make_controller(tmp_path, environment).close_game(force=False)

    assert result["ok"] is False
    assert result["state"] == "manual_end_task_required"
    assert environment.processes == {2222: "ff7rebirth_.exe"}


def test_close_game_force_terminates_a_windowless_game(tmp_path: Path):
    environment = TerminatingCloseEnvironment(has_window=False)

    result = make_controller(tmp_path, environment).close_game(timeout_seconds=.1)

    assert result["ok"] is True and result["state"] == "force_closed"
    assert result["termination"]["terminated_pids"] == [2222]
    assert environment.processes == {}


def test_close_game_waits_for_delayed_graceful_exit_after_window_close(
    tmp_path: Path,
):
    environment = DelayedShutdownEnvironment(has_window=True)

    result = make_attached_controller(tmp_path, environment).close_game(
        timeout_seconds=2.0
    )

    assert result["ok"] is True
    assert result["state"] == "closed"
    assert environment.process_polls >= 6
    assert environment.processes == {}


def test_close_game_waits_when_window_is_gone_but_process_is_shutting_down(
    tmp_path: Path,
):
    environment = DelayedShutdownEnvironment(has_window=False)

    result = make_attached_controller(tmp_path, environment).close_game(
        timeout_seconds=2.0
    )

    assert result["ok"] is True
    assert result["state"] == "closed"
    assert environment.process_polls >= 6
    assert environment.processes == {}




def test_close_game_targets_only_the_verified_context_pids(tmp_path):
    environment = TargetAwareCloseEnvironment()
    result = make_owned_controller(tmp_path, environment).close_game(timeout_seconds=.1)
    assert result["ok"]
    assert environment.target_pids == [2222]


def test_close_game_binds_child_before_targeting_window(tmp_path):
    environment = TargetAwareCloseEnvironment()
    environment.processes[3333] = "ff7rebirth.exe"
    controller = make_owned_controller(tmp_path, environment)
    controller.context["pid"] = None
    controller.context["launcher_pid"] = 3333

    result = controller.close_game(timeout_seconds=.1)

    assert result["ok"]
    assert controller.context["pid"] == 2222
    assert environment.target_pids == [2222]


import pytest


@pytest.mark.parametrize("attached", [False, True])
@pytest.mark.parametrize("has_window", [False, True])
def test_failed_graceful_close_preserves_all_processes_and_lease(tmp_path, monkeypatch, attached, has_window):
    environment = IdentityAwareTerminatingCloseEnvironment(has_window=has_window, exits_after_request=False)
    environment.processes[4444] = "ff7rebirth_.exe"
    factory = make_attached_controller if attached else make_owned_controller
    controller = factory(tmp_path, environment)
    controller.context["process_creation_tokens"] = {"2222": "token-2222"}
    waits = []
    monkeypatch.setattr(controller, "_wait_for_process_exit", lambda seconds: waits.append(seconds) or False)
    result = controller.close_game(timeout_seconds=.1, force=False)
    assert not result["ok"] and not result["lifecycle_released"]
    assert result["termination"]["state"] == "force_not_requested"
    assert result["process_watch"]["remaining_processes"] == {2222: "ff7rebirth_.exe"}
    assert environment.processes == {2222: "ff7rebirth_.exe", 4444: "ff7rebirth_.exe"}
    assert environment.termination_requests == [] and waits == [.1]
    assert controller.lease.status() is not None


@pytest.mark.parametrize("attached", [False, True])
def test_failed_graceful_close_force_terminates_only_the_bound_game(tmp_path, attached):
    environment = IdentityAwareTerminatingCloseEnvironment(has_window=True, exits_after_request=False)
    environment.processes[4444] = "ff7rebirth_.exe"
    factory = make_attached_controller if attached else make_owned_controller
    controller = factory(tmp_path, environment)
    result = controller.close_game(timeout_seconds=.1)
    assert result["ok"] and result["state"] == "force_closed" and result["lifecycle_released"]
    assert [request["pids"] for request in environment.termination_requests] == [[2222]]
    assert environment.processes == {4444: "ff7rebirth_.exe"}
