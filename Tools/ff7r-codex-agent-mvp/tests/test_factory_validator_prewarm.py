from __future__ import annotations

from ff7r_agent import factory


def test_simulation_service_does_not_start_validator_prewarm(monkeypatch, tmp_path):
    monkeypatch.setenv("FF7R_INPUT_BACKEND", "simulation")
    monkeypatch.setenv("FF7R_AUTONOMY_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.delenv("FF7R_TELEMETRY_FILE", raising=False)

    service = factory.create_service()

    assert not hasattr(service, "_independent_validator_prewarm_thread")
    assert not hasattr(service, "_validated_navigation_resources")


def test_worker_factory_defers_prewarm_until_explicit_start(monkeypatch, tmp_path):
    monkeypatch.setenv("FF7R_INPUT_BACKEND", "simulation")
    monkeypatch.setenv("FF7R_AUTONOMY_RUNTIME_DIR", str(tmp_path / "runtime"))
    calls = []
    monkeypatch.setattr(factory, "start_service_validator_prewarm", calls.append)
    service = factory.create_service(defer_validator_prewarm=True)
    assert calls == []
    factory.start_service_validator_prewarm(service)
    assert calls == [service]


def test_worker_reports_ready_before_starting_speculative_work(monkeypatch):
    import io
    from types import SimpleNamespace
    from ff7r_agent import runtime_worker

    events = []
    service = SimpleNamespace(input=SimpleNamespace(release_all=lambda: events.append("released")))
    def create(**kwargs):
        assert kwargs == {"defer_validator_prewarm": True}
        return service
    monkeypatch.setattr(runtime_worker, "_install_remote_gamepad_if_requested", lambda: None)
    monkeypatch.setattr(runtime_worker, "create_service", create)
    monkeypatch.setattr(runtime_worker, "create_lifecycle_controller", lambda: object())
    monkeypatch.setattr(runtime_worker, "create_startup_coordinator", lambda *a: object())
    monkeypatch.setattr(runtime_worker, "collect_identity", lambda **k: events.append("identity") or {})
    monkeypatch.setattr(runtime_worker, "_reply", lambda value: events.append(value["event"]))
    monkeypatch.setattr(runtime_worker, "start_service_validator_prewarm", lambda value: events.append("prewarm"))
    monkeypatch.setattr(runtime_worker.sys, "stdin", io.StringIO(""))
    runtime_worker.main()
    assert events == ["identity", "runtime_ready", "prewarm", "released"]
