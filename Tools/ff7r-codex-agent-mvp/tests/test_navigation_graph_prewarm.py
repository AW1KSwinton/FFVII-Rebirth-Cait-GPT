from __future__ import annotations

import threading
from types import SimpleNamespace

from ff7r_agent import topology_cache
from ff7r_agent.service import AgentService


def test_prewarm_navigation_builds_the_planning_graph_with_the_validator(monkeypatch, tmp_path):
    # Live, the first route in each region waited 9-11 s on a cold graph (#196).
    manifest = tmp_path / "manifest-east.json"
    monkeypatch.setattr(topology_cache, "select_library_manifest", lambda *args, **kwargs: (manifest, {}))
    validated, graphed = threading.Event(), threading.Event()
    calls = {}

    def prewarm_graph(position, target=None):
        calls["graph"] = (position, target)
        graphed.set()
        return {"state": "ready"}

    validator = SimpleNamespace(prewarm_artifact=lambda path: (calls.setdefault("validator", path), validated.set()))
    provider = SimpleNamespace(prewarm_graph=prewarm_graph)
    service = SimpleNamespace(_validated_navigation_resources=("key", provider, validator))

    result = AgentService.prewarm_navigation(service, 50_000.0, 0.0, target_x=51_000.0, target_y=10.0)

    assert result["state"] == "started" and result["graph_prewarm"] == "started"
    assert validated.wait(5) and graphed.wait(5)
    assert calls["validator"] == manifest
    assert calls["graph"] == ((50_000.0, 0.0), (51_000.0, 10.0))
    for _ in range(50):
        if getattr(service, "_topology_graph_prewarm_status", None):
            break
        threading.Event().wait(0.02)
    assert service._topology_graph_prewarm_status == {"state": "ready"}


def test_a_failed_graph_prewarm_is_recorded_not_raised(monkeypatch, tmp_path):
    monkeypatch.setattr(topology_cache, "select_library_manifest",
                        lambda *args, **kwargs: (tmp_path / "manifest-east.json", {}))
    done = threading.Event()

    def prewarm_graph(position, target=None):
        try:
            raise OSError("cache_artifact_unreadable")
        finally:
            done.set()

    service = SimpleNamespace(_validated_navigation_resources=(
        "key", SimpleNamespace(prewarm_graph=prewarm_graph), SimpleNamespace(prewarm_artifact=lambda path: None)))
    assert AgentService.prewarm_navigation(service, 0.0, 0.0)["graph_prewarm"] == "started"
    assert done.wait(5)
    for _ in range(50):
        if getattr(service, "_topology_graph_prewarm_status", None):
            break
        threading.Event().wait(0.02)
    assert service._topology_graph_prewarm_status["state"] == "failed"
    assert service._topology_graph_prewarm_status["reason"] == "OSError"
