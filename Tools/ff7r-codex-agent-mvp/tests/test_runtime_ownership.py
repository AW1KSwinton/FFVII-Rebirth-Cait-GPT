from __future__ import annotations

import io
import json
import threading
from pathlib import Path

import pytest

from ff7r_agent import gamepad_broker, mcp_http_server, runtime_ownership, runtime_worker
from ff7r_agent.doctor import STATUS_OWNERSHIP, STATUS_READY, build_report
from ff7r_agent.runtime_ownership import RuntimeOwnershipError


def test_broker_singleton_rejects_second_thread_owner() -> None:
    first = runtime_ownership._ThreadOwnership()
    second = runtime_ownership._ThreadOwnership()
    with first:
        with pytest.raises(RuntimeOwnershipError, match="ownership conflict"):
            second.__enter__()


def test_broker_refuses_before_virtual_controller_construction(monkeypatch) -> None:
    constructed = []

    def reject():
        raise RuntimeOwnershipError("FF7R gamepad broker ownership conflict")

    class FakeGamepad:
        def __init__(self):
            constructed.append(True)

    monkeypatch.setattr(gamepad_broker, "acquire_broker_ownership", reject)
    monkeypatch.setitem(__import__("sys").modules, "vgamepad", type("VG", (), {"VX360Gamepad": FakeGamepad}))

    with pytest.raises(RuntimeOwnershipError):
        gamepad_broker.serve(owner_pid=1, owner_token="test")
    assert constructed == []


def test_start_preflight_rejects_existing_broker_before_popen(monkeypatch) -> None:
    monkeypatch.setattr(
        gamepad_broker,
        "assert_no_existing_broker",
        lambda: (_ for _ in ()).throw(RuntimeOwnershipError("existing broker")),
    )
    monkeypatch.setattr(gamepad_broker.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("broker was spawned"))
    with pytest.raises(RuntimeOwnershipError, match="existing broker"):
        gamepad_broker.BrokerProcess.start()


def test_worker_eof_releases_input_once(monkeypatch) -> None:
    calls = []

    class Input:
        def release_all(self):
            calls.append("release_all")

    service = type("Service", (), {"input": Input()})()
    monkeypatch.setattr(runtime_worker, "create_service", lambda **kwargs: service)
    monkeypatch.setattr(runtime_worker, "create_lifecycle_controller", lambda: object())
    monkeypatch.setattr(runtime_worker, "create_startup_coordinator", lambda lifecycle, service: object())
    monkeypatch.setattr(runtime_worker, "collect_identity", lambda **kwargs: {})
    monkeypatch.setattr(runtime_worker, "_reply", lambda value: None)
    monkeypatch.setattr(runtime_worker.sys, "stdin", io.StringIO(""))

    runtime_worker.main()

    assert calls == ["release_all"]


def test_worker_explicit_shutdown_and_finally_release_only_once(monkeypatch) -> None:
    calls = []

    class Input:
        def release_all(self):
            calls.append("release_all")

    service = type("Service", (), {"input": Input()})()
    monkeypatch.setattr(runtime_worker, "create_service", lambda **kwargs: service)
    monkeypatch.setattr(runtime_worker, "create_lifecycle_controller", lambda: object())
    monkeypatch.setattr(runtime_worker, "create_startup_coordinator", lambda lifecycle, service: object())
    monkeypatch.setattr(runtime_worker, "collect_identity", lambda **kwargs: {})
    monkeypatch.setattr(runtime_worker, "_reply", lambda value: None)
    monkeypatch.setattr(runtime_worker.sys, "stdin", io.StringIO('{"id":1,"operation":"__shutdown__"}\n'))

    runtime_worker.main()

    assert calls == ["release_all"]


def test_http_shutdown_request_closes_runtime_before_exit(monkeypatch, tmp_path: Path) -> None:
    class ExitSignal(Exception):
        pass

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    token = mcp_http_server._prepare_shutdown_control()
    _, request_path, complete_path = mcp_http_server._control_paths()
    request_path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"pid": 1234, "token": token}).encode("utf-8"))
    events = []
    monkeypatch.setattr(mcp_http_server, "_close_runtime", lambda: events.append("close"))

    with pytest.raises(ExitSignal):
        mcp_http_server._watch_shutdown_request(
            pid=1234,
            token=token,
            poll_seconds=0.001,
            exit_fn=lambda code: (_ for _ in ()).throw(ExitSignal(code)),
        )

    assert events == ["close"]
    assert json.loads(complete_path.read_text(encoding="utf-8"))["ok"] is True


def test_doctor_rejects_two_authoritative_brokers(tmp_path: Path) -> None:
    from test_doctor import _identity, _manifest, ROOT, SOURCE_ROOT, PYTHON

    connected = _identity(connected=True)
    result = build_report(
        repository_root=ROOT,
        manifest_path=_manifest(tmp_path),
        runtime_python=Path(PYTHON),
        checkout_identity=_identity(),
        runtime_identity=_identity(),
        connected_identity=connected,
        ownership_inventory={
            "http_servers": [{"pid": 100, "listener_ports": [8765]}],
            "workers": [{"pid": 101}],
            "authoritative_brokers": [
                {"pid": 102, "listener_ports": [51132], "ancestry_pids": [100]},
                {"pid": 202, "listener_ports": [51133], "ancestry_pids": [999]},
            ],
            "legacy_runtimes": [],
            "stale_brokers": [],
        },
    )
    assert result["status"] == STATUS_OWNERSHIP
    assert result["ok"] is False
    assert any(item["kind"] == "multiple_gamepad_brokers" for item in result["runtime_ownership"]["conflicts"])


def test_doctor_accepts_one_connected_broker(tmp_path: Path) -> None:
    from test_doctor import _identity, _manifest, ROOT, PYTHON

    result = build_report(
        repository_root=ROOT,
        manifest_path=_manifest(tmp_path),
        runtime_python=Path(PYTHON),
        checkout_identity=_identity(),
        runtime_identity=_identity(),
        connected_identity=_identity(connected=True),
        ownership_inventory={
            "http_servers": [{"pid": 100, "listener_ports": [8765]}],
            "workers": [{"pid": 101}],
            "authoritative_brokers": [{"pid": 102, "listener_ports": [51132], "ancestry_pids": [100]}],
            "legacy_runtimes": [],
            "stale_brokers": [],
        },
    )
    assert result["status"] == STATUS_READY
    assert result["runtime_ownership"]["ready_eligible"] is True


def test_owner_liveness_rejects_dead_pid() -> None:
    assert runtime_ownership.owner_is_alive(2**31 - 1, "not-real") is False


def test_http_stop_script_uses_graceful_request_before_fallback() -> None:
    script = (Path(__file__).parents[1] / "Stop-FF7RHttpMcp.ps1").read_text(encoding="utf-8")
    assert "shutdown.request" in script
    assert "Graceful FF7R HTTP MCP shutdown timed out" in script
    assert "Stop-Process -Id $pidValue -Force" in script



def test_native_listener_inventory_keeps_only_loopback_tcp_listeners():
    rows = [
        "Active Connections", "  Proto Local Address Foreign Address State PID",
        "  TCP 127.0.0.1:8765 0.0.0.0:0 LISTENING 101",
        "  TCP 127.0.0.1:49100 0.0.0.0:0 LISTENING 102",
        "  TCP 127.0.0.1:49101 127.0.0.1:8765 ESTABLISHED 103",
        "  TCP 0.0.0.0:8765 0.0.0.0:0 LISTENING 104",
        "  TCP [::1]:8765 [::]:0 LISTENING 105", "  UDP 127.0.0.1:123 *:* 106",
    ]
    assert runtime_ownership._loopback_tcp_listeners(rows) == [
        {"OwningProcess": 101, "LocalPort": 8765},
        {"OwningProcess": 102, "LocalPort": 49100},
    ]


@pytest.mark.parametrize("row", [
    "TCP 127.0.0.1:8765 truncated",
    "TCP 127.0.0.1:8765 0.0.0.0:0 LISTENING invalid",
    "TCP 127.0.0.1:0 0.0.0.0:0 LISTENING 123",
    "TCP 127.0.0.1:70000 0.0.0.0:0 LISTENING 123",
    "TCP 127.0.0.1:8765 0.0.0.0:0 LISTENING 0",
])
def test_native_listener_inventory_refuses_invalid_identity(row):
    with pytest.raises(ValueError):
        runtime_ownership._loopback_tcp_listeners([row])


def test_filtered_inventory_keeps_launcher_ancestry_and_duplicate_brokers(monkeypatch):
    from types import SimpleNamespace
    processes = [
        {"ProcessId": 100, "ParentProcessId": 1, "Name": "python.exe", "CommandLine": "-m ff7r_agent.mcp_http_server"},
        {"ProcessId": 101, "ParentProcessId": 100, "Name": "python.exe", "CommandLine": "-m ff7r_agent.mcp_http_server"},
        {"ProcessId": 102, "ParentProcessId": 101, "Name": "python.exe", "CommandLine": "-m ff7r_agent.gamepad_broker serve"},
        {"ProcessId": 103, "ParentProcessId": 102, "Name": "python.exe", "CommandLine": "-m ff7r_agent.gamepad_broker serve"},
        {"ProcessId": 201, "ParentProcessId": 999, "Name": "python.exe", "CommandLine": "-m ff7r_agent.gamepad_broker serve"},
        {"ProcessId": 301, "ParentProcessId": 1, "Name": "ff7r-agent-mcp.exe", "CommandLine": "ff7r-agent-mcp"},
    ]
    raw = {"processes": processes, "listener_lines": [
        "TCP 127.0.0.1:8765 0.0.0.0:0 LISTENING 101",
        "TCP 127.0.0.1:49100 0.0.0.0:0 LISTENING 103",
        "TCP 127.0.0.1:49101 0.0.0.0:0 LISTENING 201",
        "TCP 127.0.0.1:49102 0.0.0.0:0 LISTENING 9999",
    ]}
    monkeypatch.setattr(runtime_ownership, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(runtime_ownership.subprocess, "run", lambda *a, **kw:
        SimpleNamespace(returncode=0, stdout=json.dumps(raw), stderr=""))
    inventory = runtime_ownership.collect_runtime_ownership_inventory()
    assert "inspection_error" not in inventory
    assert [r["pid"] for r in inventory["http_servers"]] == [101]
    assert all(r["pid"] != 9999 for r in inventory["listeners"])
    assert [r["pid"] for r in inventory["authoritative_brokers"]] == [103, 201]
    assert inventory["authoritative_brokers"][0]["ancestry_pids"] == [102, 101, 100, 1]
    assert [r["pid"] for r in inventory["legacy_runtimes"]] == [301]


def test_missing_native_listener_output_remains_an_inspection_error(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(runtime_ownership, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(runtime_ownership.subprocess, "run", lambda *a, **kw:
        SimpleNamespace(returncode=0, stdout='{"processes": []}', stderr=""))
    assert "inspection_error" in runtime_ownership.collect_runtime_ownership_inventory()


def test_a_rebound_bridge_may_use_the_broker_owned_by_the_http_server():
    # Live 2026-09-28: after an in-place source rebind the new bridge process
    # (29928) is not the broker's ancestor; the HTTP server that owns it is.
    from ff7r_agent.runtime_ownership import ownership_conflicts

    http = {"pid": 33752, "listener_ports": [8765], "ancestry_pids": [25224]}
    broker = {"pid": 29288, "listener_ports": [50111], "ancestry_pids": [12784, 33752, 25224]}
    identity = {"gamepad_broker_identity": {"port": 50111}, "bridge_identity": {"pid": 29928}, "worker_pid": 7632}
    inventory = {"http_servers": [http], "authoritative_brokers": [broker], "workers": [{"pid": 7632}]}
    assert ownership_conflicts(inventory, identity) == []

    foreign = {**broker, "ancestry_pids": [4444]}
    kinds = [c["kind"] for c in ownership_conflicts({**inventory, "authoritative_brokers": [foreign]}, identity)]
    assert kinds == ["broker_not_owned_by_connected_backend"]
    two_http = {**inventory, "http_servers": [http, {**http, "pid": 1}]}
    assert "multiple_http_servers" in [c["kind"] for c in ownership_conflicts(two_http, identity)]
