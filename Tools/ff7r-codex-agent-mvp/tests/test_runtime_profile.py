from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ff7r_agent.deployment_record import write_deployment_record
from ff7r_agent.runtime_bridge import RuntimeSupervisor
from ff7r_agent.runtime_profile import (
    BASE_MODULES,
    FEATURE_MODULES,
    CANONICAL_PROFILE,
    FEATURE_VALIDATION_PROFILE,
    RuntimeProfileError,
    validate_checkout,
    validate_current_checkout,
    validate_source_roots,
    verify_runtime_identity,
)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", f"safe.directory={root.as_posix()}", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _make_repo(tmp_path: Path, branch: str = "codex/static-obstacle-route-overlay") -> tuple[Path, str]:
    root = tmp_path / "repo"
    package = root / "Tools" / "ff7r-codex-agent-mvp"
    source = package / "src" / "ff7r_agent"
    source.mkdir(parents=True)
    (package / "pyproject.toml").write_text("[project]\nname='fixture'\nversion='0'\n", encoding="utf-8")
    (source / "__init__.py").write_text("", encoding="utf-8")
    for module in (
        "mcp_http_server",
        "mcp_runtime_server",
        "runtime_bridge",
        "runtime_worker",
        "runtime_profile",
        "static_obstacles",
        "live_route_adapter",
    ):
        (source / f"{module}.py").write_text("", encoding="utf-8")
    _git(root, "init") if (root / ".git").exists() else subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    _git(root, "config", "user.email", "fixture@example.invalid")
    _git(root, "config", "user.name", "Runtime Profile Tests")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "fixture")
    _git(root, "branch", "-M", "master")
    if branch != "master":
        _git(root, "checkout", "-b", branch)
    return root, _git(root, "rev-parse", "HEAD")


def _valid_identity(root: Path, commit: str, *, profile: str = FEATURE_VALIDATION_PROFILE) -> dict:
    source = root / "Tools" / "ff7r-codex-agent-mvp"
    package = source / "src"
    module_paths = {
        "ff7r_agent": package / "ff7r_agent" / "__init__.py",
        **{
            name: package / "ff7r_agent" / f"{name.rsplit('.', 1)[-1]}.py"
            for name in (*BASE_MODULES[1:], *FEATURE_MODULES)
        },
    }
    return {
        "deployment_profile": profile,
        "repository_root": str(root),
        "source_root": str(source),
        "package_import_root": str(package),
        "expected_feature_ref": "codex/static-obstacle-route-overlay" if profile == FEATURE_VALIDATION_PROFILE else None,
        "expected_feature_commit": commit if profile == FEATURE_VALIDATION_PROFILE else None,
        "expected_branch": "master" if profile == CANONICAL_PROFILE else None,
        "git_root": str(root),
        "git_branch": None if profile == FEATURE_VALIDATION_PROFILE else "master",
        "git_commit_sha": commit,
        "git_dirty": False,
        "git_dirty_paths": [],
        "loaded_package_path": str(module_paths["ff7r_agent"]),
        "loaded_module_path": str(module_paths["ff7r_agent.runtime_worker"]),
        "module_paths": {name: str(path) for name, path in module_paths.items()},
    }


def test_feature_profile_requires_exact_clean_checkout_and_ref(tmp_path: Path) -> None:
    root, commit = _make_repo(tmp_path)
    identity = validate_checkout(
        profile=FEATURE_VALIDATION_PROFILE,
        repository_root=root,
        expected_commit_sha=commit,
        expected_ref="codex/static-obstacle-route-overlay",
    )
    assert identity["git_commit_sha"] == commit
    assert identity["git_branch"] == "codex/static-obstacle-route-overlay"
    assert identity["expected_feature_ref"] == "codex/static-obstacle-route-overlay"

    (root / "untracked.txt").write_text("must reject", encoding="utf-8")
    with pytest.raises(RuntimeProfileError, match="dirty"):
        validate_checkout(
            profile=FEATURE_VALIDATION_PROFILE,
            repository_root=root,
            expected_commit_sha=commit,
            expected_ref="codex/static-obstacle-route-overlay",
        )


def test_current_checkout_validation_tracks_global_head_in_existing_contract(tmp_path: Path) -> None:
    root, initial_commit = _make_repo(tmp_path)
    contract = {
        "deployment_profile": FEATURE_VALIDATION_PROFILE,
        "repository_root": str(root),
        "source_root": str(root / "Tools" / "ff7r-codex-agent-mvp"),
        "package_import_root": str(root / "Tools" / "ff7r-codex-agent-mvp" / "src"),
        "expected_feature_ref": "codex/static-obstacle-route-overlay",
        "expected_feature_commit": initial_commit,
        "expected_branch": None,
        "expected_clean_checkout": True,
    }

    assert validate_current_checkout(contract)["git_commit_sha"] == initial_commit

    (root / "unrelated-root-file.txt").write_text("advance HEAD", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "unrelated repository change")
    current = validate_current_checkout(contract)

    assert current["git_commit_sha"] != initial_commit
    assert current["git_commit_sha"] == _git(root, "rev-parse", "HEAD")


def test_feature_profile_rejects_wrong_commit_and_wrong_ref(tmp_path: Path) -> None:
    root, commit = _make_repo(tmp_path)
    wrong_commit = "0" * 40
    with pytest.raises(RuntimeProfileError, match="HEAD mismatch"):
        validate_checkout(
            profile=FEATURE_VALIDATION_PROFILE,
            repository_root=root,
            expected_commit_sha=wrong_commit,
            expected_ref="codex/static-obstacle-route-overlay",
        )
    _git(root, "checkout", "-b", "other")
    (root / "change.txt").write_text("second commit", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "other")
    _git(root, "checkout", "codex/static-obstacle-route-overlay")
    with pytest.raises(RuntimeProfileError, match="feature ref"):
        validate_checkout(
            profile=FEATURE_VALIDATION_PROFILE,
            repository_root=root,
            expected_commit_sha=commit,
            expected_ref="other",
        )


def test_detached_feature_checkout_is_truthful(tmp_path: Path) -> None:
    root, commit = _make_repo(tmp_path)
    _git(root, "checkout", "--detach", commit)
    identity = validate_checkout(
        profile=FEATURE_VALIDATION_PROFILE,
        repository_root=root,
        expected_commit_sha=commit,
        expected_ref="codex/static-obstacle-route-overlay",
    )
    assert identity["git_branch"] is None
    assert identity["expected_feature_ref"] == "codex/static-obstacle-route-overlay"


def test_canonical_profile_remains_strict_and_feature_opt_in_is_explicit(tmp_path: Path) -> None:
    root, commit = _make_repo(tmp_path, branch="master")
    identity = validate_checkout(profile=CANONICAL_PROFILE, repository_root=root, expected_branch="master")
    assert identity["deployment_profile"] == CANONICAL_PROFILE
    with pytest.raises(RuntimeProfileError, match="canonical profile does not accept"):
        validate_checkout(
            profile=CANONICAL_PROFILE,
            repository_root=root,
            expected_branch="master",
            expected_commit_sha=commit,
        )
    with pytest.raises(RuntimeProfileError, match="requires ExpectedCommitSha"):
        validate_checkout(
            profile=FEATURE_VALIDATION_PROFILE,
            repository_root=root,
            expected_ref="master",
        )


def test_source_roots_and_loaded_module_paths_are_confined_to_selected_checkout(tmp_path: Path) -> None:
    root, commit = _make_repo(tmp_path)
    source = root / "Tools" / "ff7r-codex-agent-mvp"
    package = source / "src"
    outside = tmp_path / "outside"
    outside.mkdir()
    source_result = validate_source_roots(root, outside, package)
    assert source_result["ok"] is False
    identity = _valid_identity(root, commit)
    identity["module_paths"]["ff7r_agent.runtime_worker"] = str(outside / "runtime_worker.py")
    result = verify_runtime_identity(
        identity,
        contract={
            "deployment_profile": FEATURE_VALIDATION_PROFILE,
            "repository_root": str(root),
            "source_root": str(source),
            "package_import_root": str(package),
            "expected_feature_ref": "codex/static-obstacle-route-overlay",
            "expected_feature_commit": commit,
            "expected_branch": None,
            "expected_clean_checkout": True,
        },
    )
    assert result["ok"] is False
    assert any(item["field"] == "module_paths.ff7r_agent.runtime_worker" for item in result["mismatches"])


def test_bridge_and_worker_source_identity_must_match(tmp_path: Path) -> None:
    root, commit = _make_repo(tmp_path)
    identity = _valid_identity(root, commit)
    worker = dict(identity)
    worker["source_root"] = str(tmp_path / "other" / "Tools" / "ff7r-codex-agent-mvp")
    result = verify_runtime_identity(
        identity,
        contract={
            "deployment_profile": FEATURE_VALIDATION_PROFILE,
            "repository_root": str(root),
            "source_root": identity["source_root"],
            "package_import_root": identity["package_import_root"],
            "expected_feature_ref": identity["expected_feature_ref"],
            "expected_feature_commit": commit,
            "expected_branch": None,
            "expected_clean_checkout": True,
        },
        worker_identity=worker,
    )
    assert result["ok"] is False
    assert any(item["field"] == "bridge_worker.source_root" for item in result["mismatches"])


def test_feature_deployment_record_preserves_detached_truth_and_profile(tmp_path: Path) -> None:
    root, commit = _make_repo(tmp_path)
    _git(root, "checkout", "--detach", commit)
    checkout = validate_checkout(
        profile=FEATURE_VALIDATION_PROFILE,
        repository_root=root,
        expected_commit_sha=commit,
        expected_ref="codex/static-obstacle-route-overlay",
    )
    path = tmp_path / "feature-validation-deployment-record.json"
    record = write_deployment_record(
        path,
        identity=checkout | {"python_executable": "C:/python.exe"},
        url="http://127.0.0.1:8765/mcp",
    )
    assert record["deployment_profile"] == FEATURE_VALIDATION_PROFILE
    assert record["expected_feature_commit"] == commit
    assert record["expected_feature_ref"] == "codex/static-obstacle-route-overlay"
    assert record["git_branch"] is None
    assert record["actual_git_branch"] is None


def test_restart_worker_environment_preserves_selected_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FF7R_RUNTIME_PROFILE", FEATURE_VALIDATION_PROFILE)
    monkeypatch.setenv("FF7R_RUNTIME_REPOSITORY_ROOT", "C:/runtime-feature")
    monkeypatch.setenv("FF7R_RUNTIME_SOURCE_ROOT", "C:/runtime-feature/Tools/ff7r-codex-agent-mvp")
    monkeypatch.setenv("FF7R_RUNTIME_PACKAGE_IMPORT_ROOT", "C:/runtime-feature/Tools/ff7r-codex-agent-mvp/src")
    monkeypatch.setenv("FF7R_RUNTIME_EXPECTED_REF", "codex/static-obstacle-route-overlay")
    monkeypatch.setenv("FF7R_RUNTIME_EXPECTED_COMMIT", "6" * 40)
    supervisor = RuntimeSupervisor()
    environment = supervisor._worker_environment()
    assert environment["FF7R_RUNTIME_PROFILE"] == FEATURE_VALIDATION_PROFILE
    assert environment["FF7R_RUNTIME_REPOSITORY_ROOT"] == "C:/runtime-feature"
    assert environment["FF7R_RUNTIME_EXPECTED_COMMIT"] == "6" * 40


def test_launcher_replacement_and_restoration_guards_are_explicit() -> None:
    package_root = Path(__file__).parents[1]
    launcher = (package_root / "Start-FF7RHttpMcp.ps1").read_text(encoding="utf-8")
    stopper = (package_root / "Stop-FF7RHttpMcp.ps1").read_text(encoding="utf-8")
    restorer = (package_root / "Restore-FF7RHttpMcp.ps1").read_text(encoding="utf-8")
    assert "FeatureValidation" in launcher
    assert "ExpectedCommitSha" in launcher
    assert "ExpectedRef" in launcher
    assert "different runtime profile or source binding" in launcher
    assert "untrusted or unrelated process" in launcher
    assert "port $Port remains occupied" in stopper
    assert "Stop-FF7RHttpMcp.ps1" in restorer
    assert "Start-FF7RHttpMcp.ps1" in restorer


def test_a_timed_out_ref_lookup_is_not_read_as_a_missing_ref(monkeypatch, tmp_path):
    # Live 2026-10-05 a slow `git rev-parse` made a feature ref "unresolved"
    # and a route was refused as a source-binding mismatch.
    from ff7r_agent import runtime_profile
    def slow(root, *args):
        raise runtime_profile.GitTimeoutError("timed out")
    monkeypatch.setattr(runtime_profile, "_git", slow)
    with pytest.raises(runtime_profile.GitTimeoutError):
        runtime_profile._ref_commits(tmp_path, "feature/x")
    with pytest.raises(runtime_profile.GitTimeoutError):
        runtime_profile._git_optional(tmp_path, "rev-parse", "HEAD")
