from __future__ import annotations

import time
from pathlib import Path

import pytest

from ff7r_agent.autonomy import AutonomyError
from ff7r_agent import factory
from ff7r_agent.startup import (
    FOREGROUND_FOCUS_MAX_ATTEMPTS,
    StartupCoordinator,
    StartupPending,
)


class FakeLifecycle:
    def __init__(self, *, mode: str = "launched", phase: str | None = None):
        self.mode = mode
        self.context = {"run_id": "run-test", "pid": 4242, "startup_phase": phase}
        self.phases: list[str] = []
        self.ready_calls = 0

    def ensure_started(self, *, allow_launch: bool = True, arguments=None):
        if not allow_launch and self.mode == "missing":
            raise RuntimeError("GAME_NOT_RUNNING")
        return {"mode": self.mode, "context": dict(self.context)}

    def wait_for_fresh_telemetry(self, _timeout):
        return {"sequence": 10}

    def wait_for_gameplay_ready(self, _timeout):
        self.ready_calls += 1
        return {"sequence": 100, "readiness": "loaded_active_gameplay"}

    def mark_startup_phase(self, phase: str):
        self.phases.append(phase)
        self.context["startup_phase"] = phase
        return dict(self.context)


class FakeService:
    def __init__(
        self,
        *,
        ready: bool = False,
        frames: list[str] | None = None,
        telemetry_live: bool = True,
    ):
        self.ready = ready
        self.telemetry_live = telemetry_live
        self.taps: list[tuple[str, int]] = []
        self.captures: list[str] = []
        self.settled_samples: list[int | None] = []
        self.reconnect_calls = 0
        self.benchmark_readiness_calls = 0
        self.frames = list(frames or ["title_prompt", "main_menu"])

    def session_health(self):
        return {
            "telemetry_live": self.telemetry_live,
            "process_exists": True,
            "foreground_window": True,
            "gameplay_ready": self.ready,
        }

    def capture_game_window(self):
        self.captures.append(f"capture-{len(self.captures) + 1}")
        frame = self.frames.pop(0) if len(self.frames) > 1 else self.frames[0]
        return {
            "ok": True,
            "image_path": str(Path(self.captures[-1] + ".bmp")),
            "startup_frame": {"label": frame},
        }

    def tap(self, button: str, milliseconds: int):
        self.taps.append((button, milliseconds))
        return {"ok": True, "button": button, "milliseconds": milliseconds}

    def startup_tap(self, button: str, milliseconds: int):
        self.taps.append((button, milliseconds))
        return {
            "ok": True,
            "button": button,
            "milliseconds": milliseconds,
            "mode": "startup_visual",
        }

    def mark_gameplay_settled(self, stable_samples=None):
        self.settled_samples.append(stable_samples)
        return {
            "ok": True,
            "readiness": "settled_loaded_active_gameplay",
            "stable_samples": stable_samples or 3,
        }

    def reconnect_gamepad(self):
        self.reconnect_calls += 1
        return {"ok": True, "event": "gamepad_reconnected_after_gameplay_ready"}

    def benchmark_readiness_snapshot(self):
        self.benchmark_readiness_calls += 1
        return {
            "ok": True,
            "readiness": "warm_session",
            "settled_proof_valid": True,
            "settling_required": False,
            "movement_authorized": False,
            "navigation_admitted": False,
            "navigation_admission_required": True,
            "fresh_advancing_state_required": True,
        }


class InGameLoadService(FakeService):
    def __init__(self):
        super().__init__(ready=True)
        self.menu_open = False
        self.confirmation_count = 0

    def session_health(self):
        return {
            "telemetry_live": True,
            "process_exists": True,
            "foreground_window": True,
            "gameplay_ready": self.ready and not self.menu_open,
            "paused": self.menu_open,
            "in_menu": self.menu_open,
        }

    def tap(self, button: str, milliseconds: int):
        result = super().tap(button, milliseconds)
        if button == "menu":
            self.menu_open = True
        elif button == "confirm":
            self.confirmation_count += 1
            if self.confirmation_count >= 4:
                self.menu_open = False
                self.ready = True
        return result


def make_agent(lifecycle, service, *, sleeps=None):
    sleeper = (lambda seconds: sleeps.append(seconds)) if sleeps is not None else (lambda _seconds: None)
    return StartupCoordinator(
        lifecycle,
        service,
        session_status=lambda: None,
        session_open=lambda **kwargs: {"active": True, **kwargs},
        session_heartbeat=lambda **kwargs: {"active": True, **kwargs},
        sleeper=sleeper,
        monotonic=lambda: 0.0,
    )


class CloseAwareLifecycle(FakeLifecycle):
    def __init__(self):
        super().__init__(mode="launched")
        self.close_calls: list[float] = []

    def close_game(self, timeout_seconds: float):
        self.close_calls.append(timeout_seconds)
        return {"ok": True, "state": "closed_after_termination"}


class RestartLifecycle(CloseAwareLifecycle):
    def __init__(self, *, close_ok: bool = True):
        super().__init__()
        self.events: list[str] = []
        self.close_ok = close_ok

    def ensure_started(self, *, allow_launch: bool = True, arguments=None):
        self.events.append("ensure_started")
        return super().ensure_started(allow_launch=allow_launch, arguments=arguments)

    def close_game(self, timeout_seconds: float):
        self.events.append("close_game")
        self.close_calls.append(timeout_seconds)
        return (
            {"ok": True, "state": "closed"}
            if self.close_ok
            else {"ok": False, "state": "manual_end_task_required"}
        )


class FocusAwareLifecycle(FakeLifecycle):
    def __init__(self, make_foreground):
        super().__init__(mode="attached")
        self.make_foreground = make_foreground
        self.focus_calls = 0

    def focus_game_window(self):
        self.focus_calls += 1
        self.make_foreground()
        return {"ok": True, "state": "foreground"}


def test_phase_one_dismisses_only_title_prompt():
    lifecycle = FakeLifecycle()
    service = FakeService()
    result = make_agent(lifecycle, service).start_game_to_main_menu()

    assert result["ok"] is True
    assert result["phase"] == "main_menu"
    assert result["title_prompt_dismissed"] is True
    assert service.taps == [("confirm", 500)]
    assert lifecycle.phases == ["main_menu"]


def test_failed_owned_startup_closes_session_and_owned_game():
    lifecycle = CloseAwareLifecycle()
    service = FakeService(frames=["unknown"], telemetry_live=False)
    closed: list[str] = []
    clock_value = [0.0]

    def clock():
        clock_value[0] += 0.25
        return clock_value[0]

    agent = StartupCoordinator(
        lifecycle,
        service,
        session_status=lambda: None,
        session_open=lambda **kwargs: {"active": True, **kwargs},
        session_heartbeat=lambda **kwargs: {"active": True, **kwargs},
        session_close=lambda **kwargs: closed.append(kwargs["reason"]) or {"closed": True},
        sleeper=lambda _seconds: None,
        monotonic=clock,
    )

    result = agent.start_game_to_main_menu(interactive_timeout_seconds=1)

    assert result["ok"] is False
    assert result["pending"] is True
    assert result["phase"] == "startup_loading"
    assert closed == []
    assert lifecycle.close_calls == []


def test_foreground_recovery_is_attempted_before_attached_headless_failure():
    foreground = [False]
    lifecycle = FocusAwareLifecycle(lambda: foreground.__setitem__(0, True))
    service = FakeService(telemetry_live=False)
    service.session_health = lambda: {
        "telemetry_live": foreground[0],
        "process_exists": True,
        "foreground_window": foreground[0],
        "gameplay_ready": False,
    }
    clock_value = [0.0]

    def clock():
        clock_value[0] += 0.6
        return clock_value[0]

    agent = StartupCoordinator(
        lifecycle,
        service,
        sleeper=lambda _seconds: None,
        monotonic=clock,
    )
    result = agent._wait_interactive(5.0)

    assert result["telemetry_live"] is True
    assert lifecycle.focus_calls == 1


def test_background_input_route_does_not_trigger_foreground_recovery():
    lifecycle = FocusAwareLifecycle(lambda: pytest.fail("background startup must preserve focus"))
    service = FakeService()
    service.session_health = lambda: {
        "telemetry_live": True,
        "process_exists": True,
        "foreground_window": False,
        "input_reachable": True,
        "gameplay_ready": False,
    }
    agent = make_agent(lifecycle, service)

    result = agent._wait_interactive(1.0)

    assert result["foreground_window"] is False
    assert result["input_reachable"] is True
    assert lifecycle.focus_calls == 0


def test_background_route_timeout_does_not_report_foreground_recovery_failure():
    lifecycle = FocusAwareLifecycle(lambda: pytest.fail("background startup must preserve focus"))
    service = FakeService(telemetry_live=False)
    service.session_health = lambda: {
        "telemetry_live": False,
        "process_exists": True,
        "foreground_window": False,
        "input_reachable": True,
        "gameplay_ready": False,
    }
    clock_value = [0.0]

    def clock():
        clock_value[0] += 0.25
        return clock_value[0]

    agent = StartupCoordinator(
        lifecycle, service, sleeper=lambda _seconds: None, monotonic=clock,
    )

    with pytest.raises(StartupPending, match="no usable game input route or fresh telemetry"):
        agent._wait_interactive(1.0)

    assert lifecycle.focus_calls == 0


def test_foreground_recovery_retries_until_focus_is_verified():
    foreground = [False]

    class RetryingFocusLifecycle(FocusAwareLifecycle):
        def focus_game_window(self):
            self.focus_calls += 1
            if self.focus_calls >= 3:
                foreground[0] = True
            return {"ok": foreground[0], "foreground": foreground[0]}

    lifecycle = RetryingFocusLifecycle(lambda: None)
    service = FakeService()
    service.session_health = lambda: {
        "telemetry_live": True,
        "process_exists": True,
        "foreground_window": foreground[0],
        "gameplay_ready": False,
    }
    clock_value = [0.0]

    def clock():
        clock_value[0] += 0.2
        return clock_value[0]

    agent = StartupCoordinator(
        lifecycle,
        service,
        sleeper=lambda _seconds: None,
        monotonic=clock,
    )
    result = agent._wait_interactive(5.0)

    assert result["foreground_window"] is True
    assert lifecycle.focus_calls == 3


def test_foreground_recovery_is_bounded_and_never_returns_unverified_focus():
    lifecycle = FocusAwareLifecycle(lambda: None)
    service = FakeService()
    service.session_health = lambda: {
        "telemetry_live": True,
        "process_exists": True,
        "foreground_window": False,
        "gameplay_ready": False,
    }
    clock_value = [0.0]

    def clock():
        clock_value[0] += 0.2
        return clock_value[0]

    agent = StartupCoordinator(
        lifecycle,
        service,
        sleeper=lambda _seconds: None,
        monotonic=clock,
    )

    with pytest.raises(StartupPending, match="verified foreground window"):
        agent._wait_interactive(5.0)

    assert 1 < lifecycle.focus_calls <= FOREGROUND_FOCUS_MAX_ATTEMPTS


def test_phase_one_refuses_blind_input_for_attached_process():
    lifecycle = FakeLifecycle(mode="attached")
    service = FakeService()
    result = make_agent(lifecycle, service).start_game_to_main_menu(
        dismiss_attached_title_prompt=False
    )

    assert result["ok"] is False
    assert result["phase"] == "visual_confirmation_required"
    assert service.taps == []


def test_phase_one_uses_visual_title_prompt_when_telemetry_has_not_started():
    lifecycle = FakeLifecycle()
    service = FakeService(telemetry_live=False)
    result = make_agent(lifecycle, service).start_game_to_main_menu()

    assert result["ok"] is True
    assert result["phase"] == "main_menu"
    assert result["title_prompt_dismissed"] is True
    assert service.taps == [("confirm", 500)]


def test_reconnect_to_our_owned_launch_keeps_loading_grace():
    lifecycle = FocusAwareLifecycle(lambda: None)
    lifecycle.context.update({
        "attachment": "existing_controller_run",
        "launch_timestamp_utc": "2026-08-12T21:00:00Z",
        "owner_id": "ff7r-codex-agent",
    })
    service = FakeService()
    health_calls = [0]

    def health_with_slow_first_frame():
        health_calls[0] += 1
        if health_calls[0] == 1:
            return {
                "telemetry_live": False,
                "process_exists": True,
                "foreground_window": False,
                "gameplay_ready": False,
            }
        return {
            "telemetry_live": True,
            "process_exists": True,
            "foreground_window": True,
            "gameplay_ready": False,
        }

    service.session_health = health_with_slow_first_frame
    result = make_agent(lifecycle, service).start_game_to_main_menu()

    assert result["ok"] is True
    assert result["phase"] == "main_menu"
    assert service.taps == [("confirm", 500)]


def test_continue_wrapper_sends_exactly_two_default_confirms():
    lifecycle = FakeLifecycle(mode="attached", phase="main_menu")
    service = FakeService()
    result = make_agent(lifecycle, service).continue_from_last_save()

    assert result["ok"] is True
    assert result["phase"] == "gameplay_ready"
    assert service.taps == [("confirm", 500), ("confirm", 500)]
    assert lifecycle.ready_calls == 1
    assert service.settled_samples == [None]
    assert service.reconnect_calls == 1


def test_load_last_save_uses_guarded_in_game_menu_sequence():
    lifecycle = FakeLifecycle(mode="attached", phase="gameplay_ready")
    service = InGameLoadService()
    sleeps = []

    result = make_agent(lifecycle, service, sleeps=sleeps).load_last_save()

    assert result["ok"] is True
    assert result["phase"] == "gameplay_ready"
    assert result["mode"] == "load_last_save"
    assert service.taps == [
        ("menu", 500),
        ("up", 150),
        ("up", 150),
        ("confirm", 500),
        ("down", 150),
        ("confirm", 500),
        ("confirm", 500),
        ("up", 150),
        ("confirm", 500),
    ]
    assert 5.0 in sleeps
    assert lifecycle.phases == ["selection_in_progress", "gameplay_ready"]
    assert service.reconnect_calls == 1


def test_load_last_save_refuses_without_settled_gameplay():
    lifecycle = FakeLifecycle(mode="attached", phase="main_menu")
    service = FakeService()

    result = make_agent(lifecycle, service).load_last_save()

    assert result["ok"] is False
    assert result["phase"] == "load_failed"
    assert result["decision"].startswith("LOAD_REQUIRES_GAMEPLAY")
    assert service.taps == []


def test_restart_wrapper_closes_before_starting_again():
    lifecycle = RestartLifecycle()
    service = FakeService()
    agent = StartupCoordinator(
        lifecycle,
        service,
        session_status=lambda: None,
        session_open=lambda **kwargs: {"active": True, **kwargs},
        session_heartbeat=lambda **kwargs: {"active": True, **kwargs},
        session_close=lambda **kwargs: {"closed": True, "reason": kwargs["reason"]},
        sleeper=lambda _seconds: None,
        monotonic=lambda: 0.0,
    )

    result = agent.restart_and_continue_from_last_save(close_timeout_seconds=8.0)

    assert result["ok"] is True
    assert result["phase"] == "gameplay_ready"
    assert result["close"]["ok"] is True
    assert result["start"]["ok"] is True
    # The existing two-phase startup intentionally rebinds the same process
    # for phase one and phase two; the close must still precede both calls.
    assert lifecycle.events == ["close_game", "ensure_started", "ensure_started"]
    assert lifecycle.close_calls == [8.0]


def test_restart_wrapper_does_not_start_after_close_refusal():
    lifecycle = RestartLifecycle(close_ok=False)
    service = FakeService()

    result = make_agent(lifecycle, service).restart_and_continue_from_last_save()

    assert result["ok"] is False
    assert result["phase"] == "restart_failed"
    assert "manual_end_task_required" in result["decision"]
    assert lifecycle.events == ["close_game"]


def test_composed_wrapper_stops_if_phase_one_fails():
    lifecycle = FakeLifecycle(mode="attached")
    service = FakeService()
    service.session_health = lambda: {
        "telemetry_live": False,
        "process_exists": True,
        "foreground_window": False,
        "gameplay_ready": False,
    }
    result = make_agent(lifecycle, service).start_and_continue_from_last_save()

    assert result["ok"] is False
    assert "continue" not in result
    assert service.taps == []


def test_unknown_render_boundary_never_receives_confirm():
    lifecycle = FakeLifecycle()
    service = FakeService(frames=["unknown"])
    clock_value = [0.0]

    def clock():
        clock_value[0] += 0.25
        return clock_value[0]

    agent = StartupCoordinator(
        lifecycle,
        service,
        session_status=lambda: None,
        session_open=lambda **kwargs: {"active": True, **kwargs},
        session_heartbeat=lambda **kwargs: {"active": True, **kwargs},
        sleeper=lambda _seconds: None,
        monotonic=clock,
    )
    result = agent.start_game_to_main_menu(interactive_timeout_seconds=1)

    assert result["ok"] is False
    assert result["pending"] is True
    assert result["phase"] == "startup_loading"
    assert service.taps == []


class StaleTelemetryLifecycle(FakeLifecycle):
    def wait_for_fresh_telemetry(self, _timeout):
        raise AutonomyError("UE4SS_NOT_READY: telemetry sequence did not advance")


class PendingGameplayLifecycle(FakeLifecycle):
    def wait_for_gameplay_ready(self, _timeout):
        raise AutonomyError("TEST_READY_TIMEOUT: gameplay is still loading")


class EventuallyReadyLifecycle(FakeLifecycle):
    def wait_for_gameplay_ready(self, _timeout):
        self.ready_calls += 1
        if self.ready_calls == 1:
            raise AutonomyError("TEST_READY_TIMEOUT: gameplay is still loading")
        return {"sequence": 100, "readiness": "loaded_active_gameplay"}


def test_visual_main_menu_does_not_authorize_next_phase_without_fresh_telemetry():
    lifecycle = StaleTelemetryLifecycle()
    service = FakeService(telemetry_live=False, frames=["main_menu"])

    result = make_agent(lifecycle, service).start_game_to_main_menu()

    assert result["ok"] is False
    assert result["pending"] is True
    assert result["phase"] == "telemetry_pending"
    assert service.taps == []


def test_continue_returns_loading_pending_after_inputs_without_cleanup():
    lifecycle = PendingGameplayLifecycle(mode="attached", phase="main_menu")
    service = FakeService()
    closed: list[str] = []
    agent = StartupCoordinator(
        lifecycle,
        service,
        session_status=lambda: None,
        session_open=lambda **kwargs: {"active": True, **kwargs},
        session_heartbeat=lambda **kwargs: {"active": True, **kwargs},
        session_close=lambda **kwargs: closed.append(kwargs["reason"]) or {"closed": True},
        sleeper=lambda _seconds: None,
        monotonic=lambda: 0.0,
    )

    result = agent.continue_from_last_save()

    assert result["ok"] is False
    assert result["pending"] is True
    assert result["phase"] == "gameplay_loading"
    assert len(result["confirmations"]) == 2
    assert result["retry"].startswith("call_wait_for_gameplay_ready")
    assert closed == []


def test_resumable_startup_finishes_readiness_without_replaying_confirmations():
    lifecycle = EventuallyReadyLifecycle(mode="attached", phase="main_menu")
    service = FakeService()
    agent = StartupCoordinator(
        lifecycle,
        service,
        session_status=lambda: None,
        session_open=lambda **kwargs: {"active": True, **kwargs},
        session_heartbeat=lambda **kwargs: {"active": True, **kwargs},
        sleeper=lambda _seconds: None,
        monotonic=lambda: time.monotonic(),
    )

    result = agent.start_and_continue_from_last_save_resumable(
        readiness_timeout_seconds=2.0,
    )

    assert result["ok"] is True
    assert result["phase"] == "gameplay_ready"
    assert result["continue"]["phase"] == "gameplay_loading"
    assert result["readiness_resume"]["input_replayed"] is False
    assert lifecycle.ready_calls == 2
    assert service.taps == [
        ("confirm", 500),
        ("confirm", 500),
        ("confirm", 500),
    ]
    assert lifecycle.phases[-2:] == ["gameplay_loading", "gameplay_ready"]


def test_resumable_readiness_refuses_without_gameplay_loading_phase():
    lifecycle = FakeLifecycle(mode="attached", phase="main_menu")
    service = FakeService()

    result = make_agent(lifecycle, service).resume_gameplay_readiness()

    assert result["ok"] is False
    assert result["phase"] == "startup_not_resumable"
    assert result["input_replayed"] is False
    assert service.taps == []


def test_benchmark_attempt_warm_attaches_without_startup_or_movement_input():
    lifecycle = FakeLifecycle(mode='attached', phase='gameplay_ready')
    service = FakeService(ready=True)
    agent = make_agent(lifecycle, service)

    def unexpected_cold_start(**_kwargs):
        raise AssertionError('warm attach must not delegate to cold startup')

    agent.start_and_continue_from_last_save_resumable = unexpected_cold_start

    result = agent.prepare_benchmark_attempt()

    assert result['ok'] is True
    assert result['phase'] == 'gameplay_ready'
    assert result['mode'] == 'warm_attach'
    assert result['reason'] == 'already_running_settled_gameplay'
    assert result['movement_authorized'] is False
    assert result['input_replayed'] is False
    assert result['already_ready'] is True
    assert result['benchmark_readiness']['settled_proof_valid'] is True
    assert service.benchmark_readiness_calls == 1
    assert service.taps == []
    assert lifecycle.ready_calls == 0


def test_benchmark_attempt_refuses_warm_attach_without_final_readiness_proof():
    lifecycle = FakeLifecycle(mode='attached', phase='gameplay_ready')
    service = FakeService(ready=True)
    service.benchmark_readiness_snapshot = lambda: {
        'ok': True,
        'readiness': 'settling_required',
        'settled_proof_valid': False,
        'settled_proof_invalidation_reason': 'input_not_neutral',
        'settling_required': True,
        'movement_authorized': False,
        'navigation_admitted': False,
        'navigation_admission_required': True,
        'fresh_advancing_state_required': True,
    }
    agent = make_agent(lifecycle, service)

    def unexpected_cold_start(**_kwargs):
        raise AssertionError('an unproven warm attach must not replay startup input')

    agent.start_and_continue_from_last_save_resumable = unexpected_cold_start

    result = agent.prepare_benchmark_attempt()

    assert result['ok'] is False
    assert result['phase'] == 'benchmark_readiness_required'
    assert result['mode'] == 'warm_attach'
    assert result['reason'] == 'warm_readiness_not_proven'
    assert result['movement_authorized'] is False
    assert result['input_replayed'] is False
    assert result['benchmark_readiness']['settled_proof_invalidation_reason'] == 'input_not_neutral'
    assert service.taps == []


def test_benchmark_attempt_delegates_non_ready_session_to_resumable_startup():
    lifecycle = FakeLifecycle(mode='attached', phase='main_menu')
    service = FakeService()
    agent = make_agent(lifecycle, service)
    calls = []

    def cold_start(**kwargs):
        calls.append(kwargs)
        return {
            'ok': True,
            'phase': 'gameplay_ready',
            'decision': 'cold startup complete',
        }

    agent.start_and_continue_from_last_save_resumable = cold_start

    result = agent.prepare_benchmark_attempt(
        session_seconds=900,
        launch_timeout_seconds=11.0,
        selection_timeout_seconds=12.0,
        interactive_timeout_seconds=13.0,
        readiness_timeout_seconds=14.0,
    )

    assert result['ok'] is True
    assert result['mode'] == 'cold_resumable_startup'
    assert result['reason'] == 'warm_attach_not_ready'
    assert result['movement_authorized'] is False
    assert calls == [{
        'session_seconds': 900,
        'launch_timeout_seconds': 11.0,
        'selection_timeout_seconds': 12.0,
        'interactive_timeout_seconds': 13.0,
        'readiness_timeout_seconds': 14.0,
    }]
    assert service.benchmark_readiness_calls == 0
    assert service.taps == []


def test_benchmark_attempt_resumes_durable_gameplay_loading_without_input():
    lifecycle = EventuallyReadyLifecycle(mode='attached', phase='gameplay_loading')
    service = FakeService()
    active_session = {'active': True, 'game_pid': 4242}
    agent = StartupCoordinator(
        lifecycle,
        service,
        session_status=lambda: active_session,
        session_open=lambda **kwargs: {'active': True, **kwargs},
        session_heartbeat=lambda **kwargs: {'active': True, **kwargs},
        sleeper=lambda _seconds: None,
        monotonic=time.monotonic,
    )

    def unexpected_cold_start(**_kwargs):
        raise AssertionError('durable gameplay loading must not replay startup input')

    agent.start_and_continue_from_last_save_resumable = unexpected_cold_start

    result = agent.prepare_benchmark_attempt(readiness_timeout_seconds=2.0)

    assert result['ok'] is True
    assert result['phase'] == 'gameplay_ready'
    assert result['mode'] == 'resumable_startup'
    assert result['reason'] == 'durable_gameplay_loading'
    assert result['movement_authorized'] is False
    assert result['input_replayed'] is False
    assert lifecycle.ready_calls == 2
    assert service.taps == []


def test_factory_binds_startup_session_to_configured_runtime_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("FF7R_AUTONOMY_RUNTIME_DIR", str(tmp_path))

    coordinator = factory.create_startup_coordinator(FakeLifecycle(), FakeService())
    opened = coordinator._session_open(seconds=60, game_pid=4242)

    assert opened["active"] is True
    assert (tmp_path / "control-session.json").exists()
    assert coordinator._session_status()["active"] is True

    closed = coordinator._session_close(reason="test_runtime_binding")
    assert closed["closed"] is True


def test_confirmation_waits_for_the_existing_action_throttle():
    class ThrottledService(FakeService):
        remaining = 0.25
        def _wait_for_coordinator_cooldown(self):
            self.remaining = 0
        def startup_tap(self, button, milliseconds):
            if self.remaining > 0:
                raise RuntimeError("minimum delay between menu actions has not elapsed")
            self.remaining = 0.25
            return super().startup_tap(button, milliseconds)
        def tap(self, button, milliseconds):
            raise RuntimeError("ordinary fallback must not bypass the throttle")
    service = ThrottledService()
    coordinator = StartupCoordinator(FakeLifecycle(), service)
    coordinator._tap_confirm()
    coordinator._tap_confirm()
    assert service.taps == [("confirm", 500), ("confirm", 500)]


class SlotLoadService(FakeService):
    def __init__(self, result=None):
        super().__init__()
        self.loads = []
        self.result = result or {"ok": True, "phase": "gameplay_ready", "state": "loaded"}

    def load_save_slot(self, slot, execute=False, steam_id=None):
        self.loads.append((slot, execute))
        self.loaded_account = steam_id
        return dict(self.result)


def _slot_agent(monkeypatch, service, start_result, *, latest=False, missing=False):
    from ff7r_agent import save_slot_load

    def resolve(slot, **_kwargs):
        if missing:
            raise save_slot_load.SaveSlotLoadError("save_slot_missing", slot=slot)
        return {"slot": 8, "requested": slot, "alias": None, "file": {"steam_id": "7656"}, "latest": latest}

    monkeypatch.setattr(save_slot_load, "resolve_save_slot", resolve)
    agent = make_agent(FakeLifecycle(), service)
    starts = []

    def resumable(**kwargs):
        starts.append(kwargs)
        return {"phase": "gameplay_ready", **start_result}

    agent.start_game_to_main_menu = resumable
    return agent, starts


def test_start_and_load_save_slot_attaches_to_field_then_loads_the_slot(monkeypatch):
    service = SlotLoadService()
    agent, starts = _slot_agent(monkeypatch, service, {"ok": True, "phase": "gameplay_ready"})

    result = agent.start_and_load_save_slot(slot="proof_of_concept", execute=True,
                                            readiness_timeout_seconds=90.0)

    assert result["ok"] is True and result["phase"] == "gameplay_ready"
    assert result["mode"] == "start_and_load_save_slot" and result["slot"] == 8
    assert starts[0]["launch_timeout_seconds"] == 60.0
    assert service.loads == [(8, True)]


def test_closed_model_run_loads_from_title_without_continue_or_field_loader(monkeypatch):
    from ff7r_agent import save_slot_load
    agent, service, _, sessions = _bound_slot_agent(monkeypatch)
    phase_one = agent.start_game_to_main_menu
    agent.start_game_to_main_menu = lambda **kwargs: {**phase_one(**kwargs), "phase": "main_menu"}
    title_calls = []

    def title_load(service, **kwargs):
        kwargs.pop("before_input")()
        title_calls.append(kwargs)
        service.loads.append((kwargs["slot"], kwargs["execute"]))
        return {"ok": True, "phase": "gameplay_ready", "loaded_save": {"verified": True}}

    monkeypatch.setattr(save_slot_load, "load_title_save_slot", title_load)
    agent.start_and_continue_from_last_save_resumable = lambda **kwargs: pytest.fail("Continue must not run")
    service.load_save_slot = lambda **kwargs: pytest.fail("field loader must not run")
    result = agent.start_and_load_save_slot(model_run=True)
    assert result["ok"] and result["load_path"] == "title"
    assert title_calls == [{"slot": 8, "execute": True, "steam_id": "7656"}]
    assert result["load"]["loaded_save"]["verified"] and sessions[0][0] == "open"
    assert agent.lifecycle.context["startup_phase"] == "gameplay_ready"


@pytest.mark.parametrize("failure", ["load_arrival_timeout", "loaded_save_receipt_unverified", "load_state_unknown"])
def test_title_failure_after_confirmation_never_runs_continue_or_field_load(monkeypatch, failure):
    from ff7r_agent import save_slot_load
    service = SlotLoadService()
    agent, _ = _slot_agent(monkeypatch, service, {"ok": True, "phase": "main_menu"})
    monkeypatch.setattr(save_slot_load, "load_title_save_slot", lambda *args, **kwargs:
                        {"ok": False, "phase": "load_failed", "decision": failure, "fallback_allowed": False})
    agent.start_and_continue_from_last_save_resumable = lambda **kwargs: pytest.fail("load input must not replay")
    result = agent.start_and_load_save_slot(slot=8, execute=True)
    assert not result["ok"] and result["decision"] == failure and service.loads == []


@pytest.mark.parametrize("frame", ["main_menu", "unknown", "loading"])
def test_title_path_fallback_requires_verified_menu_and_preserves_account(monkeypatch, frame):
    from ff7r_agent import save_slot_load
    service = SlotLoadService()
    service.frames = [frame]
    agent, _ = _slot_agent(monkeypatch, service, {"ok": True, "phase": "main_menu"})
    monkeypatch.setattr(save_slot_load, "load_title_save_slot", lambda *args, **kwargs:
                        {"ok": False, "phase": "load_failed", "decision": "title_menu_selection_unverified", "fallback_allowed": True})
    continues = []
    agent.start_and_continue_from_last_save_resumable = lambda **kwargs: continues.append(kwargs) or {"ok": True}
    result = agent.start_and_load_save_slot(slot=8, execute=True)
    if frame == "main_menu":
        assert result["ok"] and result["load_path"] == "field_fallback"
        assert len(continues) == 1 and service.loads == [(8, True)] and service.loaded_account == "7656"
    else:
        assert not result["ok"] and result["decision"] == "TITLE_LOAD_FALLBACK_UNVERIFIED"
        assert not continues and not service.loads


def test_model_declaration_is_checked_before_each_title_input(monkeypatch):
    from ff7r_agent import save_slot_load
    agent, service, driver, _ = _bound_slot_agent(monkeypatch)
    phase_one = agent.start_game_to_main_menu
    agent.start_game_to_main_menu = lambda **kwargs: {**phase_one(**kwargs), "phase": "main_menu"}

    def title_load(service, **kwargs):
        driver["effort"] = "high"
        kwargs["before_input"]()
        pytest.fail("changed declaration must refuse input")

    monkeypatch.setattr(save_slot_load, "load_title_save_slot", title_load)
    with pytest.raises(Exception, match="MODEL_RUN_CHANGED_DURING_STARTUP"):
        agent.start_and_load_save_slot(model_run=True)
    assert not service.loads


def test_model_run_is_reverified_after_render_wait(monkeypatch):
    from ff7r_agent import save_slot_load
    agent, service, driver, _ = _bound_slot_agent(monkeypatch)
    phase_one = agent.start_game_to_main_menu
    agent.start_game_to_main_menu = lambda **kwargs: {**phase_one(**kwargs), "phase": "main_menu"}
    def title_load(*args, **kwargs):
        service.loads.append((8, True))
        return {"ok": True, "phase": "gameplay_ready"}
    monkeypatch.setattr(save_slot_load, "load_title_save_slot", title_load)
    def render():
        driver["effort"] = "high"
        return {"ok": True}
    agent._await_rendered_world = render
    result = agent.start_and_load_save_slot(model_run=True)
    assert not result["ok"] and result["decision"] == "MODEL_RUN_LOAD_UNVERIFIED"
    assert service.reconnect_calls == 0


def test_attached_field_render_gate_runs_before_any_load_input(monkeypatch):
    service = SlotLoadService()
    agent, _ = _slot_agent(monkeypatch, service, {"ok": True, "phase": "gameplay_ready"})
    agent._await_rendered_world = lambda: {"ok": False}
    result = agent.start_and_load_save_slot(slot=8, execute=True)
    assert not result["ok"] and result["phase"] == "render_stuck_loading" and not service.loads


def test_model_title_startup_retries_only_pending_launch(monkeypatch):
    from ff7r_agent import save_slot_load
    service = SlotLoadService()
    agent, starts = _slot_agent(monkeypatch, service, {"ok": True, "phase": "main_menu"})
    start = agent.start_game_to_main_menu
    calls = []
    def pending(**kwargs):
        calls.append(kwargs)
        if len(calls) < 3:
            return {"ok": False, "phase": "startup_loading", "decision": "STARTUP_PENDING: waiting for window"}
        return start(**kwargs)
    agent.start_game_to_main_menu = pending
    titles = []
    monkeypatch.setattr(save_slot_load, "load_title_save_slot", lambda *args, **kwargs:
                        titles.append(kwargs) or {"ok": True, "phase": "dry_run_highlighted"})
    result = agent.start_and_load_save_slot(slot=8)
    assert result["ok"] and result["start"]["startup_pending_retries"] == 2
    assert len(titles) == 1 and len(calls) == 3


def test_continue_wrapper_attaches_to_ready_game_without_selecting_continue():
    service = FakeService(ready=True, frames=["gameplay"])
    agent = make_agent(FakeLifecycle(mode="attached", phase="gameplay_ready"), service)

    result = agent.start_and_continue_from_last_save_resumable()

    assert result["ok"] and result["phase"] == "gameplay_ready" and result["already_ready"]
    assert service.taps == []


def test_start_and_load_save_slot_resolves_the_slot_before_launching(monkeypatch):
    service = SlotLoadService()
    agent, starts = _slot_agent(monkeypatch, service, {"ok": True}, missing=True)

    result = agent.start_and_load_save_slot(slot=12, execute=True)

    assert result["ok"] is False and result["decision"] == "save_slot_missing"
    assert result["started"] is False and starts == [] and service.loads == []


def test_start_and_load_save_slot_stops_when_startup_fails(monkeypatch):
    service = SlotLoadService()
    agent, _ = _slot_agent(monkeypatch, service, {
        "ok": False, "phase": "gameplay_loading", "decision": "GAMEPLAY_LOADING"})

    result = agent.start_and_load_save_slot(slot=8, execute=True)

    assert result["ok"] is False and result["phase"] == "gameplay_loading"
    assert service.loads == []


def test_start_and_load_save_slot_reports_a_failed_in_game_load(monkeypatch):
    service = SlotLoadService({"ok": False, "phase": "load_failed",
                               "decision": "load_list_highlight_unreadable"})
    agent, _ = _slot_agent(monkeypatch, service, {"ok": True, "phase": "gameplay_ready"})

    result = agent.start_and_load_save_slot(slot=8)

    assert result["ok"] is False and result["decision"] == "load_list_highlight_unreadable"
    assert service.loads == [(8, False)]


def test_newest_save_does_not_prove_what_continue_loaded(monkeypatch):
    service = SlotLoadService()
    agent, _ = _slot_agent(monkeypatch, service, {"ok": True, "phase": "gameplay_ready"}, latest=True)

    assert agent.start_and_load_save_slot(slot=8, execute=True)["load"]["ok"] is True
    service.loads.clear()
    skipped = agent.start_and_load_save_slot(slot=8, execute=True, skip_if_latest=True)

    assert skipped["ok"] is True and not skipped.get("load_skipped") and service.loads == [(8, True)]
    service.loads.clear()
    service.get_queens_blood_run_status = lambda: {"loaded_save": {"slot": 8, "steam_id": "7656"}}
    skipped = agent.start_and_load_save_slot(slot=8, execute=True, skip_if_latest=True)
    assert skipped["load_skipped"] and not service.loads


def test_unassigned_model_startup_stops_before_launch_or_input(monkeypatch):
    service = SlotLoadService()
    service.get_queens_blood_run_status = lambda: {"driver": None, "assignment": None}
    agent, starts = _slot_agent(monkeypatch, service, {"ok": True})
    result = agent.start_and_load_save_slot(model_run=True)
    assert result["decision"] == "MODEL_RUN_UNASSIGNED" and not starts and not service.loads


def test_model_startup_uses_its_registered_slot_and_never_timestamp_skips(monkeypatch):
    service = SlotLoadService()
    service.get_queens_blood_run_status = lambda: {
        "driver": {"driver": "ChatGPT", "model_id": "gpt-6.1-sol", "effort": "xhigh", "run_id": "registered-test-run"},
        "assignment": {"slot": 8, "steam_id": "7656"}, "loaded_save": None,
        "run_loaded": bool(service.loads)}
    agent, _ = _slot_agent(monkeypatch, service, {"ok": True}, latest=True)
    result = agent.start_and_load_save_slot(model_run=True)
    assert result["ok"] and service.loads == [(8, True)] and not result.get("load_skipped")


def test_model_startup_requires_verified_receipt_after_successful_arrival(monkeypatch):
    service = SlotLoadService()
    service.get_queens_blood_run_status = lambda: {
        "driver": {"driver": "ChatGPT", "model_id": "gpt-6.1-sol", "effort": "xhigh", "run_id": "registered-test-run"},
        "assignment": {"slot": 8, "steam_id": "7656"}, "run_loaded": False}
    agent, _ = _slot_agent(monkeypatch, service, {"ok": True})
    result = agent.start_and_load_save_slot(model_run=True)
    assert not result["ok"] and result["decision"] == "MODEL_RUN_LOAD_UNVERIFIED"


def test_model_startup_refuses_a_different_steam_account(monkeypatch):
    service = SlotLoadService()
    service.get_queens_blood_run_status = lambda: {
        "driver": {"driver": "ChatGPT", "model_id": "gpt-6.1-sol", "effort": "xhigh", "run_id": "registered-test-run"},
        "assignment": {"slot": 8, "steam_id": "other-account"}}
    agent, starts = _slot_agent(monkeypatch, service, {"ok": True})
    result = agent.start_and_load_save_slot(model_run=True)
    assert result["decision"] == "MODEL_RUN_ACCOUNT_MISMATCH" and not starts and not service.loads


class RenderFrames:
    """capture_game_window returning frames with a given lit fraction."""

    def __init__(self, nonblack):
        self.nonblack = list(nonblack)
        self.captures = 0

    def capture_game_window(self):
        self.captures += 1
        value = self.nonblack.pop(0) if len(self.nonblack) > 1 else self.nonblack[0]
        return {"image_path": f"frame-{self.captures}.bmp",
                "startup_frame": {"label": "gameplay", "metrics": {"whole_nonblack": value}}}


def render_agent(frames):
    clock = [0.0]
    agent = StartupCoordinator(
        FakeLifecycle(mode="attached", phase="gameplay_ready"), frames,
        session_status=lambda: None,
        session_open=lambda **kwargs: {"active": True, **kwargs},
        session_heartbeat=lambda **kwargs: {"active": True, **kwargs},
        sleeper=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
        monotonic=lambda: clock[0],
    )
    return agent


def test_render_check_fails_a_load_screen_that_never_clears():
    # Live 2026-09-28: telemetry reported a settled world under the black
    # "Loading" screen.
    frames = RenderFrames([0.01])
    result = render_agent(frames)._await_rendered_world()
    assert result["ok"] is False and result["checked"] is True
    assert frames.captures == 11  # every 6 s for 60 s, plus the first look


def test_render_check_passes_once_the_world_is_drawn():
    frames = RenderFrames([0.01, 0.02, 0.9])
    result = render_agent(frames)._await_rendered_world()
    assert result["ok"] is True and result["whole_nonblack"] == 0.9
    assert frames.captures == 3


class LabelledFrames:
    """capture_game_window yielding (raw label, whole_nonblack) or an error."""

    def __init__(self, frames):
        self.frames = list(frames)
        self.captures = 0

    def capture_game_window(self):
        self.captures += 1
        frame = self.frames.pop(0) if len(self.frames) > 1 else self.frames[0]
        if isinstance(frame, Exception):
            raise frame
        label, value = frame
        return {"image_path": f"frame-{self.captures}.bmp",
                "startup_frame": {"label": "gameplay", "render_classifier_label": label,
                                  "metrics": {"whole_nonblack": value}}}


def test_render_check_waits_while_the_loading_widget_shows():
    # A bright-enough frame is still the loading screen while its widget shows.
    frames = LabelledFrames([("loading", 0.06), ("loading", 0.06), ("title_prompt", 0.13)])
    result = render_agent(frames)._await_rendered_world()
    assert result["ok"] is True and result["render_label"] == "title_prompt"
    assert frames.captures == 3


def test_render_check_fails_when_the_loading_widget_never_clears():
    frames = LabelledFrames([("loading", 0.06)])
    result = render_agent(frames)._await_rendered_world()
    assert result["ok"] is False and result["render_label"] == "loading"


def test_render_check_waits_through_a_colourless_background_capture():
    # Live 2026-10-14 background PrintWindow saw no colour for the whole
    # loading screen of a fast travel and of a save load.
    from ff7r_agent.capture import BLANK_BACKGROUND_CAPTURE
    blank = RuntimeError(BLANK_BACKGROUND_CAPTURE)
    frames = LabelledFrames([blank, blank, ("unknown", 0.8)])
    result = render_agent(frames)._await_rendered_world()
    assert result["ok"] is True and result["checked"] is True
    assert frames.captures == 3


def test_render_check_is_unverified_when_capture_stays_colourless():
    from ff7r_agent.capture import BLANK_BACKGROUND_CAPTURE
    frames = LabelledFrames([RuntimeError(BLANK_BACKGROUND_CAPTURE)])
    result = render_agent(frames)._await_rendered_world()
    assert result["ok"] is True and result["checked"] is False
    assert frames.captures == 11


def test_render_check_does_not_wait_on_other_capture_failures():
    frames = LabelledFrames([RuntimeError("game process identity changed")])
    result = render_agent(frames)._await_rendered_world()
    assert result["ok"] is True and result["checked"] is False and frames.captures == 1


def test_resumable_startup_retries_a_pending_start_until_the_window_is_up():
    # Live 2026-09-30: after a crash the relaunched window appeared after the
    # focus budget; the job ended pending and only a later call reached play.
    agent = make_agent(FakeLifecycle(mode="attached"), FakeService())
    calls = []

    def start(**_kwargs):
        calls.append(1)
        if len(calls) < 3:
            return {"ok": False, "phase": "startup_loading",
                    "decision": "STARTUP_PENDING: verified foreground window was not established"}
        return {"ok": False, "phase": "main_menu", "decision": "stopped for the test"}

    agent.start_and_continue_from_last_save = start
    result = agent.start_and_continue_from_last_save_resumable()

    assert len(calls) == 3
    assert result["startup_pending_retries"] == 2
    assert result["phase"] == "main_menu"


@pytest.mark.parametrize("when", ["settle", "load"])
def test_model_startup_rechecks_run_identity_after_settle_and_load(monkeypatch, when):
    service = SlotLoadService()
    changed = [False]

    def status():
        return {"driver": {"driver": "ChatGPT", "model_id": "other-model" if changed[0] else "gpt-6.1-sol", "effort": "xhigh", "run_id": "registered-test-run"},
                "assignment": {"slot": 8, "steam_id": "7656"}, "run_loaded": bool(service.loads)}

    service.get_queens_blood_run_status = status
    agent, _ = _slot_agent(monkeypatch, service, {"ok": True})
    if when == "settle":
        def settle(**kwargs):
            changed[0] = True
            return {"ok": True}
        service.wait_for_settled_gameplay = settle
    else:
        original_load = service.load_save_slot
        def load(**kwargs):
            result = original_load(**kwargs)
            changed[0] = True
            return result
        service.load_save_slot = load

    result = agent.start_and_load_save_slot(model_run=True)
    assert not result["ok"]
    assert result["decision"] == ("MODEL_RUN_CHANGED_DURING_STARTUP" if when == "settle" else "MODEL_RUN_LOAD_UNVERIFIED")
    assert service.loads == ([] if when == "settle" else [(8, True)])


def _bound_slot_agent(monkeypatch, current=None):
    from ff7r_agent.startup import StartupError
    service = SlotLoadService()
    driver = {"driver": "ChatGPT", "model_id": "gpt-6.1-sol", "effort": "xhigh", "run_id": "registered-test-run"}
    assignment = {"slot": 8, "steam_id": "7656"}
    service.get_queens_blood_run_status = lambda: {"driver": dict(driver), "assignment": dict(assignment), "run_loaded": bool(service.loads)}
    agent, starts = _slot_agent(monkeypatch, service, {"ok": True})
    calls=[]
    agent._session_status = lambda: current
    def session(kind, values):
        nonlocal current
        calls.append((kind, values))
        current = {**(current or {}), "active": True, "owner_matches": True,
                   "recording_driver": {"modelId": values["model_id"], "effort": values["reasoning_effort"]}, **values}
        return dict(current)
    agent._session_open = lambda **values: session("open",values)
    agent._session_heartbeat = lambda **values: session("heartbeat",values)
    def close(**values):
        nonlocal current
        calls.append(("close", values))
        current = None
        return {"closed": True}
    agent._session_close = close
    def startup(**kwargs):
        try:
            result=agent._ensure_session(4242,1800)
            return {"ok": True,"phase": "gameplay_ready","session": result}
        except StartupError as exc:
            return {"ok": False,"decision": str(exc)}
    agent.start_game_to_main_menu=startup
    return agent, service, driver, calls


@pytest.mark.parametrize("explicit", [False, True])
def test_registered_model_startup_and_recovery_bind_exact_driver(monkeypatch, explicit):
    agent, service, driver, calls = _bound_slot_agent(monkeypatch)
    args={"model_id":"gpt-6.1-sol","reasoning_effort":"xhigh","client_id":"caller-owned"} if explicit else {}
    result=agent.start_and_load_save_slot(model_run=True, **args)
    assert result["ok"] and service.loads==[(8,True)]
    assert calls[0]==("open", {"seconds":1800,"game_pid":4242,"model_id":"gpt-6.1-sol","reasoning_effort":"xhigh","client_id":"caller-owned" if explicit else "model-startup:registered-test-run"})
    assert len(calls) == (1 if explicit else 2)
    if not explicit:
        assert calls[1] == ("close", {"reason": "model_startup_finished",
                                     "expected_session": result["start"]["session"]})
        assert result["control_session_release"]["ok"]
    assert result["start"]["session"]["recording_driver"]=={"modelId":"gpt-6.1-sol","effort":"xhigh"}
    assert agent._startup_session_binding.get() is None


@pytest.mark.parametrize("current, decision", [
    ({"active":True,"owner_matches":False,"game_pid":4242,"recording_driver":{"modelId":"gpt-6.1-sol","effort":"xhigh"}}, "CONTROL_SESSION_OWNER_MISMATCH"),
    ({"active":True,"owner_matches":False,"game_pid":4242,"recording_driver":None}, "CONTROL_SESSION_OWNER_MISMATCH"),
    ({"active":True,"owner_matches":True,"game_pid":4242,"recording_driver":{"modelId":"gpt-6.1-sol","effort":"high"}}, "CONTROL_SESSION_RECORDING_DRIVER_MISMATCH"),
    ({"active":True,"owner_matches":True,"game_pid":4242,"recording_driver":{"modelId":"other-model","effort":"xhigh"}}, "CONTROL_SESSION_RECORDING_DRIVER_MISMATCH"),
    ({"active":True,"owner_matches":True,"game_pid":4242,"recording_driver":{}}, "CONTROL_SESSION_RECORDING_DRIVER_MISMATCH"),
    ({"active":False,"state":"EXPIRED","owner_matches":False}, "CONTROL_SESSION_OWNER_MISMATCH")])
def test_bound_startup_never_adopts_foreign_or_conflicting_lease(monkeypatch,current,decision):
    agent, service, driver, calls = _bound_slot_agent(monkeypatch,current)
    result=agent.start_and_load_save_slot(model_run=True)
    assert not result["ok"] and result["decision"]==decision
    assert not calls and not service.loads


def test_bound_startup_delegates_proven_dead_windows_lease_to_open(monkeypatch):
    current = {"active": False, "state": "STALE_WINDOWS_SESSION", "owner_matches": False,
               "controller_pid": 1001, "game_pid": 1002,
               "recorded_controller_alive": False, "recorded_game_alive": False}
    agent, service, _driver, calls = _bound_slot_agent(monkeypatch, current)
    result = agent.start_and_load_save_slot(model_run=True, client_id="caller-owned")
    assert result["ok"] and service.loads == [(8, True)]
    assert [kind for kind, _values in calls] == ["open"]
    assert result["start"]["session"]["client_id"] == "caller-owned"


@pytest.mark.parametrize("change", [
    {"recorded_controller_alive": True}, {"recorded_game_alive": True},
    {"recorded_controller_alive": None}, {"recorded_game_alive": None},
    {"controller_pid": None}, {"game_pid": None},
    {"controller_pid": 0}, {"game_pid": "1002"}, {"state": "OWNER_MISMATCH"},
])
def test_bound_startup_refuses_live_or_unproven_stale_lease(monkeypatch, change):
    current = {"active": False, "state": "STALE_WINDOWS_SESSION", "owner_matches": False,
               "controller_pid": 1001, "game_pid": 1002,
               "recorded_controller_alive": False, "recorded_game_alive": False, **change}
    agent, service, _driver, calls = _bound_slot_agent(monkeypatch, current)
    result = agent.start_and_load_save_slot(model_run=True)
    assert not result["ok"] and result["decision"] == "CONTROL_SESSION_OWNER_MISMATCH"
    assert not calls and not service.loads


def test_bound_startup_propagates_stale_archive_revalidation_refusal(monkeypatch):
    from ff7r_agent.safety import ArmError
    current = {"active": False, "state": "STALE_WINDOWS_SESSION", "owner_matches": False,
               "controller_pid": 1001, "game_pid": 1002,
               "recorded_controller_alive": False, "recorded_game_alive": False}
    agent, service, _driver, calls = _bound_slot_agent(monkeypatch, current)
    def refuse(**_values):
        raise ArmError("recorded stale-session controller is still alive")
    agent._session_open = refuse
    result = agent.start_and_load_save_slot(model_run=True)
    assert not result["ok"] and "CONTROL_SESSION_OPEN_FAILED" in result["decision"]
    assert not calls and not service.loads


@pytest.mark.parametrize("client_id", [None, "owned"])
def test_bound_startup_attributes_owned_undeclared_lease(monkeypatch, client_id):
    current = {"active": True, "owner_matches": True, "game_pid": 4242,
               "client_id": client_id, "recording_driver": None}
    agent, service, driver, calls = _bound_slot_agent(monkeypatch, current)

    result = agent.start_and_load_save_slot(model_run=True, client_id=client_id)

    assert result["ok"] and service.loads == [(8, True)]
    assert calls[0] == ("heartbeat", {"seconds": 1800, "model_id": "gpt-6.1-sol",
                                     "reasoning_effort": "xhigh"})
    assert len(calls) == (1 if client_id else 2)
    assert result["start"]["session"]["recording_driver"] == {
        "modelId": "gpt-6.1-sol", "effort": "xhigh"}
    assert agent._startup_session_binding.get() is None


def test_launch_then_declare_then_model_load_attributes_existing_lease(monkeypatch, tmp_path):
    import json
    from ff7r_agent.safety import SessionLease

    monkeypatch.delenv("FF7R_RECORDING_MODEL", raising=False)
    monkeypatch.delenv("FF7R_RECORDING_EFFORT", raising=False)
    lease = SessionLease(runtime_dir=tmp_path, owner_id="test-launcher")
    agent, service, driver, calls = _bound_slot_agent(monkeypatch)
    declaration = service.get_queens_blood_run_status
    service.get_queens_blood_run_status = lambda: {"driver": None, "assignment": None}
    agent._session_status = lease.status
    agent._session_open = lease.open
    agent._session_heartbeat = lease.heartbeat
    agent._session_close = lease.close

    launched = agent.start_game_to_main_menu()
    before = lease.status()
    assert launched["ok"] and before["recording_driver"] is None
    service.get_queens_blood_run_status = declaration

    loaded = agent.start_and_load_save_slot(model_run=True)
    after = lease.status()

    assert loaded["ok"] and service.loads == [(8, True)]
    assert after is None and loaded["control_session_release"]["ok"]
    archived = json.loads(Path(loaded["control_session_release"]["archive_path"]).read_text())
    assert archived["recording_driver"] == {
        "modelId": "gpt-6.1-sol", "effort": "xhigh"}
    for field in ("opened_at", "owner_id", "game_pid", "client_id"):
        assert archived[field] == before[field]
    assert not calls  # The lease was attributed in place, then released at completion.
    assert len(list(tmp_path.glob("control-session.closed-*.json"))) == 1


@pytest.mark.parametrize("receipt", [None, {"modelId": "other-model", "effort": "xhigh"}])
def test_bound_startup_requires_attribution_receipt_before_loading(monkeypatch, receipt):
    current = {"active": True, "owner_matches": True, "game_pid": 4242,
               "recording_driver": None}
    agent, service, driver, calls = _bound_slot_agent(monkeypatch, current)
    agent._session_heartbeat = lambda **values: {"active": True, "recording_driver": receipt}

    result = agent.start_and_load_save_slot(model_run=True)

    assert result["decision"] == "CONTROL_SESSION_ATTRIBUTION_UNVERIFIED"
    assert not service.loads


def test_undeclared_startup_lease_cannot_belong_to_another_running_game(monkeypatch):
    current = {"active": True, "owner_matches": True, "game_pid": 1234,
               "recording_driver": None}
    agent, service, driver, calls = _bound_slot_agent(monkeypatch, current)
    class Environment:
        def game_process_ids_verified(self):
            return {1234: "other", 4242: "current"}
    agent.lifecycle.environment = Environment()

    result = agent.start_and_load_save_slot(model_run=True)

    assert result["decision"].startswith("CONTROL_SESSION_GAME_MISMATCH")
    assert not calls and not service.loads


def test_undeclared_startup_lease_rechecks_declaration_after_attribution(monkeypatch):
    current = {"active": True, "owner_matches": True, "game_pid": 4242,
               "recording_driver": None}
    agent, service, driver, calls = _bound_slot_agent(monkeypatch, current)
    heartbeat = agent._session_heartbeat
    def changed(**values):
        result = heartbeat(**values)
        driver["run_id"] = "new-run"
        return result
    agent._session_heartbeat = changed

    result = agent.start_and_load_save_slot(model_run=True)

    assert result["decision"] == "MODEL_RUN_CHANGED_DURING_STARTUP"
    assert calls[0][0] == "heartbeat" and not service.loads


def test_bound_startup_renews_only_matching_owned_attributed_lease(monkeypatch):
    current={"active":True,"owner_matches":True,"game_pid":4242,"client_id":"owned", "recording_driver":{"modelId":"gpt-6.1-sol","effort":"xhigh"}}
    agent, service, driver, calls = _bound_slot_agent(monkeypatch,current)
    assert agent.start_and_load_save_slot(model_run=True,client_id="owned")["ok"]
    assert calls==[("heartbeat",{"seconds":1800,"model_id":"gpt-6.1-sol","reasoning_effort":"xhigh"})]


@pytest.mark.parametrize("field,value", [("effort","high"),("run_id","new-run"),("model_id","other-model")])
def test_bound_startup_refuses_changed_declaration_before_session(monkeypatch,field,value):
    agent, service, driver, calls = _bound_slot_agent(monkeypatch)
    original=agent.start_game_to_main_menu
    def changed(**kwargs):
        driver[field]=value
        return original(**kwargs)
    agent.start_game_to_main_menu=changed
    result=agent.start_and_load_save_slot(model_run=True)
    assert not result["ok"] and result["decision"]=="MODEL_RUN_CHANGED_DURING_STARTUP"
    assert not calls and not service.loads


def test_bound_startup_explicit_mismatch_and_missing_effort_refuse(monkeypatch):
    agent, service, driver, calls = _bound_slot_agent(monkeypatch)
    result=agent.start_and_load_save_slot(model_run=True,model_id="gpt-6.1-sol",reasoning_effort="high")
    assert result["decision"]=="MODEL_RUN_RECORDING_DRIVER_MISMATCH" and not calls
    driver.pop("effort")
    assert not agent.start_and_load_save_slot(model_run=True)["ok"] and not calls


def test_bound_startup_failed_old_process_probe_never_closes_lease(monkeypatch):
    current={"active":True,"owner_matches":True,"game_pid":1234,"recording_driver":None}
    agent, service, driver, calls = _bound_slot_agent(monkeypatch,current)
    class Environment:
        def game_process_ids_verified(self):raise OSError("unreadable")
    agent.lifecycle.environment=Environment()
    result=agent.start_and_load_save_slot(model_run=True)
    assert result["decision"]=="CONTROL_SESSION_PROCESS_PROBE_FAILED" and not calls and not service.loads


@pytest.mark.parametrize("recording_driver", [None, {"modelId": "gpt-6.1-sol", "effort": "xhigh"}])
def test_bound_startup_explicit_client_cannot_adopt_other_owned_client(monkeypatch, recording_driver):
    current={"active":True,"owner_matches":True,"game_pid":4242,"client_id":"other", "recording_driver":recording_driver}
    agent, service, driver, calls = _bound_slot_agent(monkeypatch,current)
    result=agent.start_and_load_save_slot(model_run=True,client_id="caller")
    assert result["decision"]=="CONTROL_SESSION_CLIENT_MISMATCH"
    assert not calls and not service.loads


def test_bound_startup_proven_exited_owned_lease_reopens_with_attribution(monkeypatch):
    current={"active":True,"owner_matches":True,"game_pid":1234,"recording_driver":None}
    agent, service, driver, calls = _bound_slot_agent(monkeypatch,current)
    class Environment:
        def game_process_ids_verified(self): return {4242:"current"}
    agent.lifecycle.environment=Environment()
    assert agent.start_and_load_save_slot(model_run=True)["ok"]
    assert calls[0]==("close",{"reason":"startup_rebind_exited_game"})
    assert calls[1][0]=="open" and calls[1][1]["model_id"]=="gpt-6.1-sol"
    assert calls[1][1]["reasoning_effort"]=="xhigh"


def test_bound_startup_missing_open_receipt_stops_before_load(monkeypatch):
    agent, service, driver, calls = _bound_slot_agent(monkeypatch)
    agent._session_open=lambda **values: {"active":True,"recording_driver":None}
    result=agent.start_and_load_save_slot(model_run=True)
    assert result["decision"]=="CONTROL_SESSION_ATTRIBUTION_UNVERIFIED"
    assert not service.loads


class NewGameService(FakeService):
    """The New Game screens as seen live 2026-10-13: each confirm moves to the
    next screen, Down moves the cursor, and the opening cinema plays until skipped."""

    TITLES = {"camera": "$menu_title_2018_1208", "difficulty": "$menu_title_2019_0305",
              "combat_style": "$menu_options_0002_0035",
              "bonus": "$menu_title_savedata_bonus_check_title",
              "notice": "$menu_title_savedata_bonus_check_title"}
    HELP = {"camera": ["$menu_title_camera_help_normal_normal", "$camera_y", "$camera_x", "$camera_xy"],
            "difficulty": ["$help_message_0010_0010", "$help_message_0010_0011", "$help_message_0010_0012"],
            "combat_style": ["$menu_options_0002_0036_help", "$menu_options_0002_0037_help"],
            "bonus": ["$menu_title_savedata_bonus_check_execute_help", "$bonus_skip"], "notice": ["$bonus_notice"]}
    NEXT = {"main_menu": "bonus", "bonus": "notice", "notice": "camera", "camera": "difficulty",
            "combat_style": "movie"}

    def __init__(self, *, stuck_cursor_on=None):
        super().__init__(frames=["unknown"])
        self.screen = "main_menu"
        self.cursor = 1  # Continue is preselected under New Game
        self.last_title = None
        self.choices = {}
        self.cinema = False
        self.skips = 0
        self.stuck_cursor_on = stuck_cursor_on

    def read_screen_texts(self, contains=None):
        # Like the game, the widget keeps its last title between screens.
        self.last_title = self.TITLES.get(self.screen, self.last_title)
        texts = [{"path": "BP_EndGameInstance.Startmenu_00.WidgetTree.Txt_Title", "text": self.last_title}]
        if self.screen in self.HELP:
            texts.append({"path": "BP_EndGameInstance.Startmenu_00.WidgetTree.Txt_Help",
                          "text": self.HELP[self.screen][self.cursor]})
        return {"texts": [item for item in texts if item["text"] is not None]}

    def _press(self, button):
        if button in {"up", "down"} and self.screen != self.stuck_cursor_on:
            self.cursor += -1 if button == "up" else 1
        elif button == "confirm":
            assert self.screen != "main_menu" or self.cursor == 0, "confirmed Continue instead of New Game"
            self.choices[self.screen] = self.cursor
            if self.screen == "difficulty":
                self.screen = "combat_style" if self.cursor == 0 else "movie"
            else:
                self.screen = self.NEXT[self.screen]
            self.cinema = self.screen == "movie"
            self.cursor = 0

    def tap(self, button, milliseconds):
        self._press(button)
        return super().tap(button, milliseconds)

    def startup_tap(self, button, milliseconds):
        self._press(button)
        return super().startup_tap(button, milliseconds)

    def cutscene_state(self):
        scenes = [{"sequence": "MV_MIDGR_0010"}] if self.cinema else []
        return {"observed": True, "active": self.cinema, "scenes": scenes}

    def skip_cutscene(self):
        self.skips += 1
        self.cinema = False
        return {"ok": True, "skipped": ["MV_MIDGR_0010"]}


def new_game_agent(service):
    lifecycle = FakeLifecycle(mode="attached", phase="main_menu")
    clock = [0.0]

    def sleep(seconds):
        clock[0] += seconds

    def monotonic():
        clock[0] += 0.01
        return clock[0]

    agent = StartupCoordinator(
        lifecycle, service, session_status=lambda: None,
        session_open=lambda **kwargs: {"active": True, **kwargs},
        session_heartbeat=lambda **kwargs: {"active": True, **kwargs},
        sleeper=sleep, monotonic=monotonic)
    agent.start_game_to_main_menu = lambda **_kwargs: {"ok": True, "phase": "main_menu"}
    return agent, lifecycle


def test_start_new_game_classic_easy_walks_every_setup_screen_and_skips_the_opening():
    service = NewGameService()
    agent, lifecycle = new_game_agent(service)

    result = agent.start_new_game(difficulty="classic (easy)")

    assert result["ok"] is True and result["phase"] == "gameplay_ready", result.get("decision")
    assert result["settings"] == {"difficulty": "easy", "combat_style": "classic",
                                  "camera": "default", "bonus_check": "check"}
    # New Game above Continue, Check and its notice, then camera, Easy, Classic.
    assert service.choices == {"main_menu": 0, "bonus": 0, "notice": 0, "camera": 0,
                               "difficulty": 0, "combat_style": 1}
    assert service.taps == [("up", 150), ("confirm", 500)] + [("confirm", 150)] * 4 + [
        ("down", 150), ("confirm", 150)]
    assert service.skips == 1 and result["skips"] == [{"ok": True, "skipped": ["MV_MIDGR_0010"]}]
    assert [item["choice"] for item in result["screens"]] == ["default", "easy", "classic"]
    assert lifecycle.phases[-1] == "gameplay_ready"


def test_start_new_game_normal_has_no_combat_style_screen():
    service = NewGameService()
    agent, _lifecycle = new_game_agent(service)

    result = agent.start_new_game(difficulty="Normal", bonus_check="skip")

    assert result["ok"] is True, result.get("decision")
    assert service.choices["difficulty"] == 1 and "combat_style" not in service.choices
    assert service.choices["bonus"] == 1  # Skip, below Check
    assert [item["screen"] for item in result["screens"]] == ["camera", "difficulty"]


@pytest.mark.parametrize(("arguments", "expected"), [
    ({"difficulty": "classic_easy"}, ("easy", "classic")),
    ({"difficulty": "Classic"}, ("easy", "classic")),
    ({"difficulty": "easy", "combat_style": "Classic"}, ("easy", "classic")),
    ({"difficulty": "dynamic"}, ("dynamic", "active")),
])
def test_new_game_settings_read_combined_difficulty_names(arguments, expected):
    settings = StartupCoordinator._new_game_settings(
        arguments["difficulty"], arguments.get("combat_style"), "default", "check")
    assert (settings["difficulty"], settings["combat_style"]) == expected


@pytest.mark.parametrize(("arguments", "decision"), [
    ({"difficulty": "hard"}, "NEW_GAME_DIFFICULTY_UNKNOWN"),
    ({"difficulty": "normal", "combat_style": "classic"}, "NEW_GAME_CLASSIC_REQUIRES_EASY"),
    ({"difficulty": "classic (easy)", "combat_style": "active"}, "NEW_GAME_COMBAT_STYLE_CONFLICT"),
    ({"difficulty": "easy", "camera": "upside_down"}, "NEW_GAME_CAMERA_UNKNOWN"),
])
def test_start_new_game_refuses_unknown_settings_before_any_input(arguments, decision):
    service = NewGameService()
    agent, _lifecycle = new_game_agent(service)

    result = agent.start_new_game(**arguments)

    assert result["ok"] is False and result["decision"].startswith(decision)
    assert result["input_sent"] is False and service.taps == []


def test_start_new_game_never_confirms_a_choice_the_cursor_did_not_reach():
    service = NewGameService(stuck_cursor_on="combat_style")
    agent, _lifecycle = new_game_agent(service)

    result = agent.start_new_game(difficulty="classic (easy)")

    assert result["ok"] is False
    assert result["decision"].startswith("NEW_GAME_CURSOR_UNVERIFIED:combat_style=classic")
    assert "combat_style" not in service.choices and service.cinema is False


def test_start_new_game_refuses_a_game_already_in_gameplay():
    service = NewGameService()
    agent, _lifecycle = new_game_agent(service)
    agent.start_game_to_main_menu = lambda **_kwargs: {"ok": True, "phase": "gameplay_ready"}

    result = agent.start_new_game(difficulty="easy")

    assert result["ok"] is False and result["decision"].startswith("NEW_GAME_REQUIRES_MAIN_MENU")
    assert service.taps == []


@pytest.mark.parametrize(("screen", "arguments"), [
    ("difficulty", {"difficulty": "dynamic"}),
    ("camera", {"camera": "inverted_xy"}),
])
def test_start_new_game_does_not_confirm_when_a_later_cursor_step_is_lost(screen, arguments):
    class LostSecondDown(NewGameService):
        def _press(self, button):
            if self.screen == screen and button == "down" and self.cursor == 1:
                return
            super()._press(button)

    service = LostSecondDown()
    agent, _lifecycle = new_game_agent(service)
    result = agent.start_new_game(**arguments)

    assert result["ok"] is False
    assert result["decision"].startswith(f"NEW_GAME_CURSOR_UNVERIFIED:{screen}=")
    assert screen not in service.choices
    assert service.cinema is False


def test_start_new_game_accepts_the_real_lifecycle_phase_validation(tmp_path):
    from types import MethodType
    from ff7r_agent.autonomy import LifecycleController

    service = NewGameService()
    agent, lifecycle = new_game_agent(service)
    lifecycle.runtime_dir = tmp_path
    lifecycle.mark_startup_phase = MethodType(LifecycleController.mark_startup_phase, lifecycle)

    result = agent.start_new_game(difficulty="classic (easy)")

    assert result["ok"] is True, result.get("decision")
    assert lifecycle.context["startup_phase"] == "gameplay_ready"


def test_new_game_bonus_texts_override_the_coarse_main_menu_frame_label():
    service = NewGameService()
    service.frames = ["main_menu"]  # The live bonus screen has this brightness label.
    agent, _lifecycle = new_game_agent(service)

    result = agent.start_new_game(difficulty="classic (easy)")

    assert result["ok"] is True, result.get("decision")
    assert service.choices["bonus"] == 0


def test_start_new_game_propagates_explicit_recording_attribution():
    service = NewGameService()
    agent, _lifecycle = new_game_agent(service)
    sessions = []
    def opened(**values):
        sessions.append(values)
        return {"active": True, "recording_driver": {
            "modelId": values["model_id"], "effort": values["reasoning_effort"]}, **values}
    agent._session_open = opened

    result = agent.start_new_game(model_id="gpt-6.1-sol", reasoning_effort="max", client_id="reviewer")

    assert result["ok"] is True, result.get("decision")
    assert sessions and all(item["client_id"] == "reviewer" and item["model_id"] == "gpt-6.1-sol"
                            and item["reasoning_effort"] == "max" for item in sessions)
    assert agent._startup_session_binding.get() is None


@pytest.mark.parametrize(("difficulty", "wanted_index", "moves"), [
    ("classic (easy)", 1, []),
    ("easy", 0, ["up"]),
])
def test_new_game_handles_a_remembered_classic_selection(difficulty, wanted_index, moves):
    class RememberedClassic(NewGameService):
        def _press(self, button):
            before = self.screen
            super()._press(button)
            if before != "combat_style" and self.screen == "combat_style":
                self.cursor = 1

    service = RememberedClassic()
    agent, _lifecycle = new_game_agent(service)
    result = agent.start_new_game(difficulty=difficulty)

    assert result["ok"] is True, result.get("decision")
    assert service.choices["combat_style"] == wanted_index
    combat = next(screen for screen in result["screens"] if screen["screen"] == "combat_style")
    assert [step["direction"] for step in combat["cursor_steps"]] == moves
