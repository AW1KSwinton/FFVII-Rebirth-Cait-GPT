from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from ff7r_agent.deployment_record import write_deployment_record
from ff7r_agent.doctor import STATUS_CHECKOUT, STATUS_MCP, STATUS_READY, STATUS_RUNTIME, build_report


ROOT = Path(__file__).parents[3]
SOURCE_ROOT = ROOT / "Tools" / "ff7r-codex-agent-mvp"
PACKAGE_ROOT = SOURCE_ROOT / "src"
BRANCH = "codex/ff7r-canonical-consolidation"
PYTHON = r"C:\CaitGPT\Tools\ff7r-codex-agent-mvp\.venv\Scripts\python.exe"


def _empty_ownership_inventory() -> dict:
    """Keep doctor unit tests independent of the host's live FF7R services."""

    return {
        "schema_version": 1,
        "platform": "test",
        "processes": [],
        "listeners": [],
        "http_servers": [],
        "workers": [],
        "authoritative_brokers": [],
        "legacy_runtimes": [],
        "stale_brokers": [],
    }


def _manifest(tmp_path: Path) -> Path:
    value = {
        "schema_version": 4,
        "canonical_branch": BRANCH,
        "integration_base_sha": "d07f36956f3ce5d0917e697393af47a9f23518ff",
        "implementation_commit_sha": "34f946b70a9086257790183352b5f73dfba4632e",
        "canonical_commit_policy": {
            "source": "git_head",
            "runtime_gate": "runtime_git_head_equals_checkout_git_head",
            "clean_required": True,
        },
        "canonical_source": {
            "branch": BRANCH,
            "source_root": str(SOURCE_ROOT),
            "package_import_root": str(PACKAGE_ROOT),
        },
        "installed_editable_runtime": {
            "expected_source_root": str(SOURCE_ROOT),
            "expected_package_import_root": str(PACKAGE_ROOT),
            "expected_branch": BRANCH,
            "python": PYTHON,
        },
        "supported_executable": {"path": str(tmp_path / "missing.exe"), "sha256": None},
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _identity(*, sha: str = "head", root: Path = SOURCE_ROOT, branch: str = BRANCH, dirty: bool = False, dirty_paths: list[str] | None = None, connected: bool = False) -> dict:
    modules = {
        name: str(PACKAGE_ROOT / (name.rsplit(".", 1)[-1] + ".py"))
        for name in (
            "ff7r_agent",
            "ff7r_agent.mcp_http_server",
            "ff7r_agent.mcp_runtime_server",
            "ff7r_agent.runtime_bridge",
            "ff7r_agent.runtime_worker",
            "ff7r_agent.startup",
        )
    }
    value = {
        "ok": True,
        "source_root": str(root),
        "package_import_root": str(PACKAGE_ROOT),
        "python_executable": PYTHON,
        "git_branch": branch,
        "git_commit_sha": sha,
        "git_commit": sha,
        "git_dirty": dirty,
        "git_dirty_paths": dirty_paths or [],
        "module_paths": modules,
    }
    if connected:
        value.update(
            {
                "worker_generation": 1,
                "worker_pid": 101,
                "bridge_identity": {"name": "ff7r-agent-mcp-http", "pid": 100},
                "gamepad_broker_identity": {"pid": 102, "port": 51132},
                "runtime_directory": r"C:\CaitGPT\fixtures\AppData\Local\ff7r-agent",
                "lifecycle_directory": r"C:\CaitGPT\fixtures\AppData\Local\ff7r-agent\lifecycle",
            }
        )
    return value


def _report(tmp_path: Path, *, checkout: dict, runtime: dict, connected: dict | None = None) -> dict:
    return build_report(
        repository_root=ROOT,
        manifest_path=_manifest(tmp_path),
        runtime_python=Path(PYTHON),
        checkout_identity=checkout,
        runtime_identity=runtime,
        connected_identity=connected,
        ownership_inventory=_empty_ownership_inventory(),
    )


def test_clean_canonical_checkout_passes(tmp_path: Path) -> None:
    result = _report(tmp_path, checkout=_identity(), runtime=_identity(), connected=_identity(connected=True))
    assert result["status"] == STATUS_READY
    assert result["ok"] is True
    assert result["identity_contract"] == {
        "canonical_commit_sha": "head",
        "source": "checkout_git_head",
        "runtime_gate": "runtime_git_head_equals_checkout_git_head",
        "clean_required": True,
        "manifest_self_reference": False,
    }


def test_modified_python_source_fails(tmp_path: Path) -> None:
    result = _report(
        tmp_path,
        checkout=_identity(),
        runtime=_identity(dirty=True, dirty_paths=["Tools/ff7r-codex-agent-mvp/src/ff7r_agent/service.py"]),
        connected=_identity(connected=True),
    )
    assert result["status"] == STATUS_RUNTIME
    assert any(item["field"] == "git_dirty" for item in result["runtime_probe"]["mismatches"])


def test_modified_tracked_manifest_fails(tmp_path: Path) -> None:
    result = _report(
        tmp_path,
        checkout=_identity(dirty=True, dirty_paths=["ProjectKnowledge/Current/canonical-project-state.json"]),
        runtime=_identity(),
        connected=_identity(connected=True),
    )
    assert result["status"] == STATUS_CHECKOUT
    assert any(item["field"] == "git_dirty" for item in result["checkout"]["mismatches"])


def test_wrong_branch_fails(tmp_path: Path) -> None:
    result = _report(tmp_path, checkout=_identity(branch="codex/wrong"), runtime=_identity(), connected=_identity(connected=True))
    assert result["status"] == STATUS_CHECKOUT
    assert any(item["field"] == "git_branch" for item in result["checkout"]["mismatches"])


def test_wrong_git_head_runtime_relationship_fails(tmp_path: Path) -> None:
    result = _report(tmp_path, checkout=_identity(sha="checkout-head"), runtime=_identity(sha="runtime-head"), connected=_identity(sha="runtime-head", connected=True))
    assert result["status"] == STATUS_RUNTIME
    assert any(item["field"] == "git_commit_sha" for item in result["runtime_probe"]["mismatches"])
    assert any(item["field"] == "git_commit_sha" for item in result["connected_runtime"]["mismatches"])


def test_stale_runtime_source_fails(tmp_path: Path) -> None:
    stale = tmp_path / "stale-runtime"
    result = _report(tmp_path, checkout=_identity(), runtime=_identity(root=stale), connected=_identity(root=stale, connected=True))
    assert result["status"] == STATUS_RUNTIME
    assert any(item["field"] == "source_root" for item in result["runtime_probe"]["mismatches"])


def test_doctor_requires_connected_identity_snapshot(tmp_path: Path) -> None:
    result = _report(tmp_path, checkout=_identity(), runtime=_identity())
    assert result["status"] == STATUS_MCP
    assert result["connected_runtime"]["identity"] is None
    assert result["ok"] is False


def test_doctor_unit_reports_do_not_probe_host_processes(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "ff7r_agent.doctor.collect_runtime_ownership_inventory",
        lambda: pytest.fail("doctor unit tests must provide deterministic ownership state"),
    )
    result = _report(tmp_path, checkout=_identity(), runtime=_identity(), connected=_identity(connected=True))
    assert result["status"] == STATUS_READY


def test_exact_deployment_sha_record_does_not_change_git_head(tmp_path: Path) -> None:
    safe_root = ROOT.as_posix()
    before = subprocess.check_output(
        ["git", "-c", f"safe.directory={safe_root}", "-C", str(ROOT), "rev-parse", "HEAD"],
        text=True,
    ).strip()
    record = write_deployment_record(
        tmp_path / "deployment-record.json",
        identity={
            "git_commit_sha": "exact-deployed-head",
            "git_branch": BRANCH,
            "git_dirty": False,
            "source_root": str(SOURCE_ROOT),
            "package_import_root": str(PACKAGE_ROOT),
            "python_executable": PYTHON,
        },
    )
    after = subprocess.check_output(
        ["git", "-c", f"safe.directory={safe_root}", "-C", str(ROOT), "rev-parse", "HEAD"],
        text=True,
    ).strip()
    assert record["git_commit_sha"] == "exact-deployed-head"
    assert json.loads((tmp_path / "deployment-record.json").read_text(encoding="utf-8"))["git_commit_sha"] == "exact-deployed-head"
    assert after == before
