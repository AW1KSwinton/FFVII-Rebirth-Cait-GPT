"""External, non-recursive record of an exact runtime deployment."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .runtime_identity import collect_checkout_identity
from .runtime_profile import CANONICAL_PROFILE, FEATURE_VALIDATION_PROFILE, normalize_profile


def default_deployment_record_path() -> Path | None:
    local_app_data = os.environ.get("LOCALAPPDATA")
    return Path(local_app_data) / "ff7r-cait-gpt" / "deployment-record.json" if local_app_data else None


def default_feature_validation_deployment_record_path() -> Path | None:
    local_app_data = os.environ.get("LOCALAPPDATA")
    return (
        Path(local_app_data) / "ff7r-cait-gpt" / "feature-validation-deployment-record.json"
        if local_app_data
        else None
    )


def write_deployment_record(
    path: str | os.PathLike[str],
    *,
    identity: dict[str, Any],
    transport: str = "streamable-http",
    url: str | None = None,
    deployed_at: str | None = None,
) -> dict[str, Any]:
    """Write exact deployment evidence outside Git without changing the checkout.

    The checkout Git pin is mandatory. Bridge, worker, and broker fields are
    optional enrichment because the normal launcher initially has only
    checkout identity; connected ``get_runtime_identity`` may enrich the same
    record during live acceptance.
    """

    profile = normalize_profile(identity.get("deployment_profile") or CANONICAL_PROFILE)
    if identity.get("git_dirty") is not False:
        raise ValueError("refusing to record a dirty checkout deployment")
    commit = identity.get("git_commit_sha") or identity.get("git_commit")
    branch = identity.get("git_branch")
    source_root = identity.get("source_root")
    repository_root = identity.get("repository_root") or identity.get("git_root")
    if not repository_root and source_root:
        # Preserve compatibility with older checkout-only callers that only
        # supplied the package source root. New launcher records always carry
        # the explicit repository_root field.
        try:
            repository_root = str(Path(source_root).resolve().parents[1])
        except (OSError, IndexError):
            repository_root = None
    expected_feature_ref = identity.get("expected_feature_ref")
    expected_feature_commit = identity.get("expected_feature_commit")
    if not commit or not source_root or not repository_root:
        raise ValueError("deployment identity must include Git HEAD, repository_root, and source_root")
    if profile == CANONICAL_PROFILE and not branch:
        raise ValueError("canonical deployment identity must include an attached Git branch")
    if profile == FEATURE_VALIDATION_PROFILE and (not expected_feature_ref or not expected_feature_commit):
        raise ValueError("feature-validation deployment identity must include expected feature ref and commit")
    connected_identity = bool(
        identity.get("bridge_identity")
        or identity.get("worker_identity")
        or identity.get("worker_generation") is not None
        or identity.get("worker_pid") is not None
        or identity.get("gamepad_broker_identity")
    )
    record = {
        "schema_version": 2,
        "record_kind": "ff7r-agent-deployment",
        "deployment_profile": profile,
        "deployed_at": deployed_at or datetime.now(timezone.utc).isoformat(),
        "repository_root": repository_root,
        "git_commit_sha": commit,
        "git_branch": branch,
        "actual_git_branch": branch,
        "expected_feature_ref": expected_feature_ref,
        "expected_feature_commit": expected_feature_commit,
        "feature_ref_resolved": identity.get("feature_ref_resolved"),
        "expected_clean_checkout": identity.get("expected_clean_checkout", True),
        "git_dirty": False,
        "source_root": source_root,
        "package_import_root": identity.get("package_import_root"),
        "python_executable": identity.get("python_executable"),
        "transport": transport,
        "url": url,
        "runtime_identity_enrichment": "connected_runtime" if connected_identity else "checkout_only",
        "bridge_identity": identity.get("bridge_identity"),
        "worker_generation": identity.get("worker_generation"),
        "worker_pid": identity.get("worker_pid"),
        "gamepad_broker_identity": identity.get("gamepad_broker_identity"),
        "runtime_directory": identity.get("runtime_directory"),
        "lifecycle_directory": identity.get("lifecycle_directory"),
    }
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return record


def read_deployment_record(path: str | os.PathLike[str]) -> dict[str, Any] | None:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write an external FF7R deployment record")
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runtime-python", default=None)
    parser.add_argument("--transport", default="streamable-http")
    parser.add_argument("--url", default=None)
    parser.add_argument("--profile", default=CANONICAL_PROFILE)
    parser.add_argument("--expected-ref", default=None)
    parser.add_argument("--expected-commit", default=None)
    parser.add_argument("--expected-branch", default=None)
    args = parser.parse_args(argv)
    identity = collect_checkout_identity(args.repository_root)
    identity["deployment_profile"] = normalize_profile(args.profile)
    identity["expected_feature_ref"] = args.expected_ref
    identity["expected_feature_commit"] = args.expected_commit
    identity["expected_branch"] = args.expected_branch
    identity["expected_clean_checkout"] = True
    if args.runtime_python:
        identity["python_executable"] = str(Path(args.runtime_python).resolve())
    record = write_deployment_record(
        args.output,
        identity=identity,
        transport=args.transport,
        url=args.url,
    )
    print(json.dumps(record, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
