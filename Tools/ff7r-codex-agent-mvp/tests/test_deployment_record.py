from __future__ import annotations

import json
from pathlib import Path

from ff7r_agent.deployment_record import write_deployment_record


def _checkout_identity() -> dict:
    return {
        "git_commit_sha": "master-head",
        "git_branch": "master",
        "git_dirty": False,
        "source_root": "C:/master/Tools/ff7r-codex-agent-mvp",
        "package_import_root": "C:/master/Tools/ff7r-codex-agent-mvp/src",
        "python_executable": "D:/runtime/python.exe",
    }


def test_launcher_style_record_pins_checkout_without_worker_or_broker(tmp_path: Path) -> None:
    path = tmp_path / "deployment-record.json"
    record = write_deployment_record(
        path,
        identity=_checkout_identity(),
        transport="streamable-http",
        url="http://127.0.0.1:8765/mcp",
    )
    assert record["git_commit_sha"] == "master-head"
    assert record["git_branch"] == "master"
    assert record["source_root"] == "C:/master/Tools/ff7r-codex-agent-mvp"
    assert record["runtime_identity_enrichment"] == "checkout_only"
    assert record["worker_pid"] is None
    assert record["gamepad_broker_identity"] is None


def test_connected_identity_can_enrich_record_without_changing_checkout_pin(tmp_path: Path) -> None:
    identity = _checkout_identity() | {
        "bridge_identity": {"name": "ff7r-agent-mcp-http", "pid": 101},
        "worker_generation": 2,
        "worker_pid": 202,
        "gamepad_broker_identity": {"pid": 303, "port": 40404},
    }
    path = tmp_path / "deployment-record.json"
    record = write_deployment_record(path, identity=identity)
    assert record["runtime_identity_enrichment"] == "connected_runtime"
    assert json.loads(path.read_text(encoding="utf-8"))["git_commit_sha"] == "master-head"
