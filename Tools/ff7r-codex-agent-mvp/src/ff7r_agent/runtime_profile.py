"""Explicit source-binding contracts for the FF7R runtime launcher.

The canonical profile is deliberately strict and remains the default.  The
feature-validation profile is an opt-in contract for a clean, exact checkout;
it is never inferred from the current working directory or from ``PYTHONPATH``
alone.  This module contains the decision logic so the PowerShell launcher and
the live bridge/worker can enforce the same rules.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from .subprocess_options import no_console_window_kwargs


CANONICAL_PROFILE = "CANONICAL"
FEATURE_VALIDATION_PROFILE = "FEATURE_VALIDATION"
SUPPORTED_PROFILES = (CANONICAL_PROFILE, FEATURE_VALIDATION_PROFILE)
FEATURE_MODULES = (
    "ff7r_agent.static_obstacles",
    "ff7r_agent.live_route_adapter",
)
BASE_MODULES = (
    "ff7r_agent",
    "ff7r_agent.mcp_http_server",
    "ff7r_agent.mcp_runtime_server",
    "ff7r_agent.runtime_bridge",
    "ff7r_agent.runtime_bridge_process",
    "ff7r_agent.runtime_worker",
)
_REF_PATTERN = re.compile(r"^[A-Za-z0-9._/\-]+$")


class RuntimeProfileError(RuntimeError):
    """Raised when a runtime source contract cannot be proven."""


class GitTimeoutError(RuntimeProfileError):
    """A Git read did not finish in time: nothing was learned about the checkout."""


def normalize_profile(value: str | None) -> str:
    normalized = str(value or "").strip().replace("-", "_").upper()
    aliases = {
        "CANONICAL": CANONICAL_PROFILE,
        "FEATUREVALIDATION": FEATURE_VALIDATION_PROFILE,
        "FEATURE_VALIDATION": FEATURE_VALIDATION_PROFILE,
    }
    try:
        return aliases[normalized]
    except KeyError as exc:
        raise RuntimeProfileError(
            f"unsupported runtime profile {value!r}; expected Canonical or FeatureValidation"
        ) from exc


def _resolve(path: str | os.PathLike[str] | None) -> Path | None:
    if path is None:
        return None
    return Path(path).expanduser().resolve()


def _same_path(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return False
    return os.path.normcase(os.path.normpath(str(left))) == os.path.normcase(os.path.normpath(str(right)))


def _under(path: str | os.PathLike[str] | None, root: str | os.PathLike[str] | None) -> bool:
    if path is None or root is None:
        return False
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (OSError, ValueError):
        return False


def _git(root: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            [
                "git",
                # Read-only: without this, `git status` rewrites the index
                # under index.lock. Concurrent checks then contended for the
                # lock, a timed-out one left it behind (live 2026-10-05), and
                # each rewrite looked like a checkout change to the bridge.
                "--no-optional-locks",
                "-c",
                f"safe.directory={root.as_posix()}",
                "-C",
                str(root),
                *arguments,
            ],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
            **no_console_window_kwargs(),
        )
    except subprocess.TimeoutExpired as exc:
        # Live 2026-10-05 even `rev-parse HEAD` occasionally took over 5 s
        # while the game was running.
        raise GitTimeoutError(f"Git command failed for {root}: {exc}") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeProfileError(f"Git command failed for {root}: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown Git error"
        raise RuntimeProfileError(f"Git command failed for {root}: {detail}")
    return result.stdout.strip()


def _git_optional(root: Path, *arguments: str) -> str | None:
    try:
        return _git(root, *arguments) or None
    except GitTimeoutError:
        raise  # not an answer: "absent" here read as a real mismatch (live 2026-10-05)
    except RuntimeProfileError:
        return None


def _require_source_layout(repository_root: Path) -> tuple[Path, Path]:
    source_root = repository_root / "Tools" / "ff7r-codex-agent-mvp"
    package_import_root = source_root / "src"
    required = {
        "source_root": source_root,
        "package_import_root": package_import_root,
        "pyproject.toml": source_root / "pyproject.toml",
        "ff7r_agent package": package_import_root / "ff7r_agent",
    }
    missing = [name for name, path in required.items() if not path.exists()]
    if missing:
        raise RuntimeProfileError(
            f"selected repository is missing expected package source: {', '.join(missing)}"
        )
    if not _under(source_root, repository_root) or not _under(package_import_root, repository_root):
        raise RuntimeProfileError("source_root and package_import_root must resolve beneath repository_root")
    return source_root, package_import_root


def validate_source_roots(
    repository_root: str | os.PathLike[str],
    source_root: str | os.PathLike[str],
    package_import_root: str | os.PathLike[str],
) -> dict[str, Any]:
    """Validate explicit source roots independently of Git metadata."""

    root = _resolve(repository_root)
    source = _resolve(source_root)
    package = _resolve(package_import_root)
    if root is None or source is None or package is None:
        raise RuntimeProfileError("repository_root, source_root, and package_import_root are required")
    expected_source, expected_package = _require_source_layout(root)
    mismatches: list[dict[str, Any]] = []
    if not _same_path(source, expected_source):
        mismatches.append({"field": "source_root", "expected": str(expected_source), "actual": str(source)})
    if not _same_path(package, expected_package):
        mismatches.append(
            {"field": "package_import_root", "expected": str(expected_package), "actual": str(package)}
        )
    if not _under(source, root) or not _under(package, root):
        mismatches.append(
            {"field": "source_roots_under_repository", "expected": str(root), "actual": [str(source), str(package)]}
        )
    return {"ok": not mismatches, "mismatches": mismatches}


def _ref_candidates(expected_ref: str) -> list[str]:
    if not _REF_PATTERN.fullmatch(expected_ref) or expected_ref.startswith("-"):
        raise RuntimeProfileError(f"invalid expected feature ref: {expected_ref!r}")
    candidates = [expected_ref]
    if not expected_ref.startswith("refs/") and not expected_ref.startswith("origin/"):
        # A detached runtime worktree can have a stale local branch ref when
        # another worktree owns that branch. The pushed remote-tracking ref is
        # an explicit, auditable association in that case; record which ref
        # actually proved the commit below.
        candidates.append(f"origin/{expected_ref}")
    return candidates


def _ref_commits(root: Path, expected_ref: str) -> list[tuple[str, str]]:
    commits: list[tuple[str, str]] = []
    for candidate in _ref_candidates(expected_ref):
        try:
            commits.append((candidate, _git(root, "rev-parse", "--verify", f"{candidate}^{{commit}}")))
        except GitTimeoutError:
            raise  # a slow read, not a missing ref
        except RuntimeProfileError:
            continue
    return commits


def _contract(
    *,
    profile: str,
    repository_root: Path,
    source_root: Path,
    package_import_root: Path,
    expected_commit_sha: str | None,
    expected_ref: str | None,
    expected_branch: str | None,
) -> dict[str, Any]:
    return {
        "deployment_profile": profile,
        "repository_root": str(repository_root),
        "source_root": str(source_root),
        "package_import_root": str(package_import_root),
        "expected_feature_ref": expected_ref if profile == FEATURE_VALIDATION_PROFILE else None,
        "expected_feature_commit": expected_commit_sha if profile == FEATURE_VALIDATION_PROFILE else None,
        "expected_branch": expected_branch if profile == CANONICAL_PROFILE else None,
        "expected_clean_checkout": True,
    }


def validate_checkout(
    *,
    profile: str,
    repository_root: str | os.PathLike[str],
    expected_commit_sha: str | None = None,
    expected_ref: str | None = None,
    expected_branch: str | None = None,
) -> dict[str, Any]:
    """Prove the exact checkout selected for a runtime profile."""

    selected_profile = normalize_profile(profile)
    root = _resolve(repository_root)
    if root is None or not root.is_dir():
        raise RuntimeProfileError(f"repository root does not exist: {repository_root}")
    source_root, package_import_root = _require_source_layout(root)
    git_root = _resolve(_git(root, "rev-parse", "--show-toplevel"))
    if git_root is None or not _same_path(git_root, root):
        raise RuntimeProfileError(
            f"selected repository root is not the Git root (selected={root}, git_root={git_root})"
        )
    actual_commit = _git(root, "rev-parse", "HEAD")
    raw_branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    actual_branch = None if raw_branch == "HEAD" else raw_branch
    dirty = _git(root, "status", "--porcelain", "--untracked-files=all")
    if dirty:
        paths = [line[3:] if len(line) >= 3 else line for line in dirty.splitlines()]
        raise RuntimeProfileError(
            "selected feature/canonical checkout is dirty; tracked and untracked paths are not allow-listed: "
            + "; ".join(paths)
        )

    if selected_profile == CANONICAL_PROFILE:
        if expected_commit_sha is not None or expected_ref is not None:
            raise RuntimeProfileError("canonical profile does not accept feature expected commit/ref arguments")
        if not expected_branch:
            raise RuntimeProfileError("canonical profile requires the manifest canonical_branch")
        if actual_branch != expected_branch:
            raise RuntimeProfileError(
                f"canonical checkout/manifest branch mismatch (checkout={actual_branch}, manifest={expected_branch})"
            )
    else:
        if not expected_commit_sha:
            raise RuntimeProfileError("feature-validation profile requires ExpectedCommitSha")
        if not expected_ref:
            raise RuntimeProfileError("feature-validation profile requires ExpectedRef")
        if actual_commit.lower() != expected_commit_sha.lower():
            raise RuntimeProfileError(
                f"feature-validation HEAD mismatch (expected={expected_commit_sha}, actual={actual_commit})"
            )
        ref_commits = _ref_commits(root, expected_ref)
        matching_refs = [name for name, commit in ref_commits if commit.lower() == expected_commit_sha.lower()]
        if not matching_refs:
            actual_refs = ", ".join(f"{name}={commit}" for name, commit in ref_commits) or "unresolved"
            raise RuntimeProfileError(
                f"feature ref does not resolve to expected commit (ref={expected_ref}, "
                f"expected={expected_commit_sha}, actual={actual_refs})"
            )
        resolved_feature_ref = matching_refs[0]

    identity = {
        "schema_version": 1,
        "deployment_profile": selected_profile,
        "repository_root": str(root),
        "source_root": str(source_root),
        "package_import_root": str(package_import_root),
        "expected_feature_ref": expected_ref if selected_profile == FEATURE_VALIDATION_PROFILE else None,
        "expected_feature_commit": expected_commit_sha if selected_profile == FEATURE_VALIDATION_PROFILE else None,
        "expected_branch": expected_branch if selected_profile == CANONICAL_PROFILE else None,
        "expected_clean_checkout": True,
        "git_root": str(git_root),
        "git_branch": actual_branch,
        "git_commit": actual_commit,
        "git_commit_sha": actual_commit,
        "git_dirty": False,
        "git_dirty_paths": [],
        "feature_ref_commit": expected_commit_sha if expected_ref else None,
        "feature_ref_resolved": resolved_feature_ref if expected_ref else None,
    }
    return identity


def profile_contract_from_environment() -> dict[str, Any] | None:
    raw_profile = os.getenv("FF7R_RUNTIME_PROFILE")
    if not raw_profile:
        return None
    profile = normalize_profile(raw_profile)
    repository_root = _resolve(os.getenv("FF7R_RUNTIME_REPOSITORY_ROOT"))
    source_root = _resolve(os.getenv("FF7R_RUNTIME_SOURCE_ROOT"))
    package_import_root = _resolve(os.getenv("FF7R_RUNTIME_PACKAGE_IMPORT_ROOT"))
    if repository_root is None or source_root is None or package_import_root is None:
        raise RuntimeProfileError("runtime profile environment is incomplete: repository/source/package roots are required")
    expected_commit = os.getenv("FF7R_RUNTIME_EXPECTED_COMMIT") or None
    expected_ref = os.getenv("FF7R_RUNTIME_EXPECTED_REF") or None
    expected_branch = os.getenv("FF7R_RUNTIME_EXPECTED_BRANCH") or None
    if profile == FEATURE_VALIDATION_PROFILE and (not expected_commit or not expected_ref):
        raise RuntimeProfileError("feature-validation runtime environment requires expected commit and ref")
    return _contract(
        profile=profile,
        repository_root=repository_root,
        source_root=source_root,
        package_import_root=package_import_root,
        expected_commit_sha=expected_commit,
        expected_ref=expected_ref,
        expected_branch=expected_branch,
    )


def validate_current_checkout(contract: dict[str, Any]) -> dict[str, Any]:
    """Validate the current checkout using the existing profile contract.

    The launch contract intentionally pins the complete repository HEAD.
    This helper reuses the same clean-checkout, branch/ref, source-layout, and
    Git-root checks used by the launcher; it does not infer a narrower source
    tree or weaken the existing fail-closed source-binding policy.
    """

    if not isinstance(contract, dict):
        raise RuntimeProfileError("runtime profile contract must be an object")
    profile = normalize_profile(str(contract.get("deployment_profile") or ""))
    repository_root = contract.get("repository_root")
    if not repository_root:
        raise RuntimeProfileError("runtime profile contract is missing repository_root")
    root = _resolve(repository_root)
    if root is None:
        raise RuntimeProfileError("runtime profile contract has an invalid repository_root")
    current_commit = _git(root, "rev-parse", "HEAD")
    if profile == FEATURE_VALIDATION_PROFILE:
        expected_ref = contract.get("expected_feature_ref")
        if not expected_ref:
            raise RuntimeProfileError("feature-validation runtime contract is missing expected_feature_ref")
        return validate_checkout(
            profile=profile,
            repository_root=root,
            expected_commit_sha=current_commit,
            expected_ref=str(expected_ref),
        )
    expected_branch = contract.get("expected_branch")
    if not expected_branch:
        raise RuntimeProfileError("canonical runtime contract is missing expected_branch")
    return validate_checkout(
        profile=profile,
        repository_root=root,
        expected_branch=str(expected_branch),
    )


def profile_identity_fields() -> dict[str, Any]:
    """Return explicit profile fields suitable for all runtime identities."""

    raw_profile = os.getenv("FF7R_RUNTIME_PROFILE")
    if not raw_profile:
        return {
            "deployment_profile": None,
            "repository_root": None,
            "expected_feature_ref": None,
            "expected_feature_commit": None,
            "expected_branch": None,
            "expected_clean_checkout": None,
        }
    contract = profile_contract_from_environment()
    assert contract is not None
    return {
        "deployment_profile": contract["deployment_profile"],
        "repository_root": contract["repository_root"],
        "expected_feature_ref": contract["expected_feature_ref"],
        "expected_feature_commit": contract["expected_feature_commit"],
        "expected_branch": contract["expected_branch"],
        "expected_clean_checkout": contract["expected_clean_checkout"],
    }


def _path_mismatch(
    mismatches: list[dict[str, Any]], field: str, expected: Any, actual: Any
) -> None:
    if expected is not None and not _same_path(expected, actual):
        mismatches.append({"field": field, "expected": str(expected), "actual": actual})


def verify_runtime_identity(
    identity: dict[str, Any],
    *,
    contract: dict[str, Any] | None = None,
    worker_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Verify loaded bridge/worker paths against one explicit profile contract."""

    expected = contract if contract is not None else profile_contract_from_environment()
    if expected is None:
        return {"ok": True, "disabled": True, "mismatches": []}
    mismatches: list[dict[str, Any]] = []
    for field in ("deployment_profile", "expected_feature_ref", "expected_feature_commit", "expected_branch"):
        expected_value = expected.get(field)
        if expected_value is not None and identity.get(field) != expected_value:
            mismatches.append({"field": field, "expected": expected_value, "actual": identity.get(field)})
    _path_mismatch(mismatches, "repository_root", expected.get("repository_root"), identity.get("repository_root") or identity.get("git_root"))
    _path_mismatch(mismatches, "source_root", expected.get("source_root"), identity.get("source_root"))
    _path_mismatch(mismatches, "package_import_root", expected.get("package_import_root"), identity.get("package_import_root"))
    expected_commit = expected.get("expected_feature_commit")
    if expected_commit and identity.get("git_commit_sha", identity.get("git_commit")) != expected_commit:
        mismatches.append({
            "field": "git_commit_sha",
            "expected": expected_commit,
            "actual": identity.get("git_commit_sha", identity.get("git_commit")),
        })
    if expected.get("expected_clean_checkout") is True and identity.get("git_dirty") is not False:
        mismatches.append({"field": "git_dirty", "expected": False, "actual": identity.get("git_dirty"), "paths": identity.get("git_dirty_paths", [])})
    if not _under(identity.get("source_root"), expected.get("repository_root")):
        mismatches.append({"field": "source_root_under_repository", "expected": expected.get("repository_root"), "actual": identity.get("source_root")})
    if not _under(identity.get("package_import_root"), expected.get("repository_root")):
        mismatches.append({"field": "package_import_root_under_repository", "expected": expected.get("repository_root"), "actual": identity.get("package_import_root")})

    required_modules = list(BASE_MODULES)
    if expected.get("deployment_profile") == FEATURE_VALIDATION_PROFILE:
        required_modules.extend(FEATURE_MODULES)
    module_paths = identity.get("module_paths") if isinstance(identity.get("module_paths"), dict) else {}
    for module_name in required_modules:
        module_path = module_paths.get(module_name)
        if not module_path:
            mismatches.append({"field": f"module_paths.{module_name}", "expected": "loaded beneath package_import_root", "actual": module_path})
        elif not _under(module_path, expected.get("package_import_root")):
            mismatches.append({"field": f"module_paths.{module_name}", "expected": expected.get("package_import_root"), "actual": module_path})
    for field in ("loaded_package_path", "loaded_module_path"):
        value = identity.get(field)
        if value and not _under(value, expected.get("package_import_root")):
            mismatches.append({"field": field, "expected": expected.get("package_import_root"), "actual": value})

    if worker_identity is not None:
        for field in (
            "deployment_profile",
            "repository_root",
            "source_root",
            "package_import_root",
            "expected_feature_ref",
            "expected_feature_commit",
            "git_commit_sha",
            "git_branch",
            "git_dirty",
        ):
            left = identity.get(field)
            right = worker_identity.get(field)
            equal = _same_path(left, right) if field.endswith("root") else left == right
            if not equal:
                mismatches.append({"field": f"bridge_worker.{field}", "expected": left, "actual": right})
        worker_result = verify_runtime_identity(worker_identity, contract=expected)
        mismatches.extend({"field": f"worker.{item['field']}", **{key: value for key, value in item.items() if key != "field"}} for item in worker_result.get("mismatches", []))
    return {
        "ok": not mismatches,
        "deployment_profile": expected.get("deployment_profile"),
        "expected": dict(expected),
        "mismatches": mismatches,
    }


def verify_current_process_binding() -> dict[str, Any]:
    """Verify the bridge interpreter/module tree before it binds HTTP."""

    contract = profile_contract_from_environment()
    if contract is None:
        return {"ok": True, "disabled": True, "mismatches": []}
    from .runtime_identity import collect_identity

    identity = collect_identity(process_role="mcp-bridge", module_path=__file__)
    return verify_runtime_identity(identity, contract=contract)


def _cli_validate(args: argparse.Namespace) -> int:
    try:
        identity = validate_checkout(
            profile=args.profile,
            repository_root=args.repository_root,
            expected_commit_sha=args.expected_commit_sha,
            expected_ref=args.expected_ref,
            expected_branch=args.expected_branch,
        )
    except RuntimeProfileError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(identity, indent=2, sort_keys=True))
    return 0


def _cli_verify(args: argparse.Namespace) -> int:
    try:
        identity = json.loads(args.identity_json.read_text(encoding="utf-8"))
        if not isinstance(identity, dict):
            raise ValueError("identity JSON must be an object")
        root = _resolve(args.repository_root)
        source = root / "Tools" / "ff7r-codex-agent-mvp" if root else None
        package = source / "src" if source else None
        contract = _contract(
            profile=normalize_profile(args.profile),
            repository_root=root,
            source_root=source,
            package_import_root=package,
            expected_commit_sha=args.expected_commit_sha,
            expected_ref=args.expected_ref,
            expected_branch=args.expected_branch,
        )
        result = verify_runtime_identity(identity, contract=contract)
    except (OSError, ValueError, TypeError, RuntimeProfileError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate an FF7R runtime source profile")
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate-checkout")
    validate.add_argument("--profile", required=True)
    validate.add_argument("--repository-root", type=Path, required=True)
    validate.add_argument("--expected-commit-sha", default=None)
    validate.add_argument("--expected-ref", default=None)
    validate.add_argument("--expected-branch", default=None)
    validate.set_defaults(handler=_cli_validate)
    verify = subparsers.add_parser("verify-identity")
    verify.add_argument("--profile", required=True)
    verify.add_argument("--repository-root", type=Path, required=True)
    verify.add_argument("--identity-json", type=Path, required=True)
    verify.add_argument("--expected-commit-sha", default=None)
    verify.add_argument("--expected-ref", default=None)
    verify.add_argument("--expected-branch", default=None)
    verify.set_defaults(handler=_cli_verify)
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
