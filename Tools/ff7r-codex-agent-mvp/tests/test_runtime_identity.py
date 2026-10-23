from __future__ import annotations

from pathlib import Path

from ff7r_agent.runtime_identity import collect_identity, verify_identity


def test_identity_reports_required_fields() -> None:
    identity = collect_identity(process_role="test")

    assert identity["schema_version"] == 2
    assert identity["python_executable"]
    assert identity["source_root"]
    assert identity["package_import_root"]
    assert identity["loaded_package_path"]
    assert identity["module_paths"]["ff7r_agent"]
    assert identity["module_paths"]["ff7r_agent.runtime_commands"]
    assert "worker_generation" in identity
    assert "worker_pid" in identity
    assert identity["bridge_identity"]["pid"] > 0
    assert "gamepad_broker_identity" in identity
    assert "runtime_directory" in identity
    assert "git_dirty_paths" in identity


def test_identity_rejects_mismatched_source_root_and_sha(tmp_path: Path) -> None:
    identity = collect_identity(process_role="test")
    result = verify_identity(
        identity,
        expected_source_root=tmp_path,
        expected_commit_sha="0000000000000000000000000000000000000000",
        expected_branch="not-the-loaded-branch",
    )

    assert result["ok"] is False
    assert {item["field"] for item in result["mismatches"]} == {
        "source_root",
        "git_commit_sha",
        "git_branch",
    }


def test_identity_accepts_exact_source_and_sha() -> None:
    identity = collect_identity(process_role="test")
    result = verify_identity(
        identity,
        expected_source_root=identity["source_root"],
        expected_package_import_root=identity["package_import_root"],
        expected_commit_sha=identity["git_commit_sha"],
        expected_branch=identity["git_branch"],
    )

    assert result == {"ok": True, "mismatches": []}


def test_identity_rejects_dirty_checkout_when_clean_is_required() -> None:
    identity = collect_identity(process_role="test")
    identity["git_dirty"] = True
    identity["git_dirty_paths"] = ["ProjectKnowledge/Current/canonical-project-state.json"]
    result = verify_identity(identity, expected_clean=True)

    assert result["ok"] is False
    assert result["mismatches"][0]["field"] == "git_dirty"
