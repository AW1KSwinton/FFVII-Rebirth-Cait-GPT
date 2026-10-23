from __future__ import annotations

from types import SimpleNamespace

from ff7r_agent.runtime_commands import invoke_runtime


def test_prepare_benchmark_attempt_dispatch_preserves_movement_gate() -> None:
    calls: list[dict[str, object]] = []

    def prepare_benchmark_attempt(**arguments: object) -> dict[str, object]:
        calls.append(arguments)
        return {"ok": True, "movement_authorized": False}

    result = invoke_runtime(
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(prepare_benchmark_attempt=prepare_benchmark_attempt),
        "prepare_benchmark_attempt",
        {"session_seconds": 30},
    )

    assert result["movement_authorized"] is False
    assert calls == [{"session_seconds": 30}]


def test_benchmark_readiness_and_phase_dispatch_use_service_keywords() -> None:
    phase_calls: list[tuple[str, object]] = []

    def mark_chadley_benchmark_phase(phase: str, at: object = None) -> dict[str, object]:
        phase_calls.append((phase, at))
        return {"ok": True, "phase": phase, "at": at}

    service = SimpleNamespace(
        benchmark_readiness_snapshot=lambda: {
            "ok": True,
            "movement_authorized": False,
            "navigation_admitted": False,
        },
        mark_chadley_benchmark_phase=mark_chadley_benchmark_phase,
    )
    lifecycle = SimpleNamespace()
    startup = SimpleNamespace()

    readiness = invoke_runtime(service, lifecycle, startup, "benchmark_readiness_snapshot")
    phase = invoke_runtime(
        service,
        lifecycle,
        startup,
        "mark_chadley_benchmark_phase",
        {"phase": "warm_ready", "at": "2026-08-28T00:00:00Z"},
    )

    assert readiness["movement_authorized"] is False
    assert readiness["navigation_admitted"] is False
    assert phase["phase"] == "warm_ready"
    assert phase_calls == [("warm_ready", "2026-08-28T00:00:00Z")]
