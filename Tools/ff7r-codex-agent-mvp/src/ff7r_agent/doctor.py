"""Fail-closed, read-only canonical checkout/runtime doctor.

The committed manifest deliberately does not contain the SHA of the commit
that contains the manifest. The checkout is authoritative for its Git HEAD;
the connected runtime must report that same HEAD. This avoids a tracked
metadata file having to predict its own future commit ID.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from .subprocess_options import no_console_window_kwargs
from typing import Any

from .deployment_record import read_deployment_record
from .runtime_identity import collect_checkout_identity, verify_identity
from .runtime_ownership import collect_runtime_ownership_inventory, ownership_conflicts


STATUS_READY = "READY"
STATUS_CHECKOUT = "CHECKOUT_SOURCE_MISMATCH"
STATUS_RUNTIME = "RUNTIME_SOURCE_MISMATCH"
STATUS_MCP = "MCP_SURFACE_MISSING"
STATUS_MANIFEST = "CANONICAL_STATE_INVALID"
STATUS_OWNERSHIP = "RUNTIME_OWNERSHIP_CONFLICT"


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _sha256(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest().upper()
    except OSError:
        return None


def _probe_runtime(runtime_python: Path) -> dict[str, Any]:
    """Collect identity using the package actually importable by runtime_python."""

    code = r'''
import json
from ff7r_agent.runtime_identity import collect_identity

try:
    identity = collect_identity(process_role="doctor-runtime")
    identity["ok"] = True
except Exception as exc:
    identity = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
print(json.dumps(identity))
'''
    environment = {key: value for key, value in os.environ.items() if key.upper() != "PYTHONPATH"}
    try:
        result = subprocess.run(
            [str(runtime_python), "-c", code],
            capture_output=True,
            text=True,
            timeout=20.0,
            check=False,
            env=environment,
            **no_console_window_kwargs(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": f"runtime probe failed: {exc}"}
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        return {"ok": False, "error": result.stderr.strip() or "runtime probe returned no JSON"}
    try:
        payload = json.loads(lines[-1])
    except ValueError:
        return {"ok": False, "error": "runtime probe returned invalid JSON", "stdout": result.stdout[-2000:]}
    payload["probe_returncode"] = result.returncode
    return payload


def _manifest_expectations(manifest: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    canonical = manifest.get("canonical_source")
    runtime = manifest.get("installed_editable_runtime") or manifest.get("installed_runtime")
    supported_executable = manifest.get("supported_executable")
    required = (canonical, runtime, supported_executable)
    if not all(isinstance(value, dict) for value in required):
        return None, "canonical_source, installed_runtime, and supported_executable are required objects"

    canonical_branch = manifest.get("canonical_branch")
    integration_base_sha = manifest.get("integration_base_sha")
    policy = manifest.get("canonical_commit_policy")
    if not canonical_branch or not integration_base_sha:
        return None, "canonical_branch and integration_base_sha are required"
    if not isinstance(policy, dict) or policy.get("source") != "git_head":
        return None, "canonical_commit_policy.source must be git_head"
    if policy.get("runtime_gate") != "runtime_git_head_equals_checkout_git_head":
        return None, "canonical_commit_policy.runtime_gate is invalid"
    if policy.get("clean_required") is not True:
        return None, "canonical_commit_policy.clean_required must be true"
    if canonical.get("branch") != canonical_branch:
        return None, "canonical_source.branch must mirror canonical_branch"

    canonical_source_root = canonical.get("source_root")
    canonical_package_root = canonical.get("package_import_root")
    runtime_source_root = runtime.get("expected_source_root") or runtime.get("source_root")
    runtime_package_root = runtime.get("expected_package_import_root") or runtime.get("package_import_root")
    runtime_branch = runtime.get("expected_branch") or runtime.get("branch") or canonical_branch
    expected_python = runtime.get("python") or runtime.get("python_executable")
    if not canonical_source_root or not canonical_package_root:
        return None, "canonical_source source_root and package_import_root are required"
    if not runtime_source_root or not runtime_package_root or not expected_python:
        return None, "installed runtime source/package/Python expectations are required"
    if runtime_branch != canonical_branch:
        return None, "installed runtime expected_branch must mirror canonical_branch"

    # This is a policy/configuration consistency check, not a SHA equality gate.
    if os.path.normcase(os.path.normpath(str(runtime_source_root))) != os.path.normcase(os.path.normpath(str(canonical_source_root))):
        return None, "canonical_source and installed runtime source roots differ"
    if os.path.normcase(os.path.normpath(str(runtime_package_root))) != os.path.normcase(os.path.normpath(str(canonical_package_root))):
        return None, "canonical_source and installed runtime package roots differ"

    return {
        "canonical": {
            **canonical,
            "branch": canonical_branch,
            "source_root": canonical_source_root,
            "package_import_root": canonical_package_root,
        },
        "runtime": {
            **runtime,
            "branch": runtime_branch,
            "source_root": runtime_source_root,
            "package_import_root": runtime_package_root,
            "python_executable": expected_python,
        },
        "canonical_branch": canonical_branch,
    }, None


def _add_python_mismatch(mismatches: list[dict[str, Any]], identity: dict[str, Any], expected: str) -> None:
    actual = identity.get("python_executable")
    if os.path.normcase(os.path.normpath(str(actual))) != os.path.normcase(os.path.normpath(str(expected))):
        mismatches.append({"field": "python_executable", "expected": expected, "actual": actual})


def _path_is_under(path: Any, root: str) -> bool:
    if not path:
        return False
    try:
        Path(str(path)).resolve().relative_to(Path(root).resolve())
    except (ValueError, OSError):
        return False
    return True


def _add_module_path_mismatches(
    mismatches: list[dict[str, Any]],
    identity: dict[str, Any],
    expected_source_root: str,
) -> None:
    module_paths = identity.get("module_paths")
    if not isinstance(module_paths, dict):
        mismatches.append({"field": "module_paths", "expected": "mapping under source root", "actual": module_paths})
        return
    for name in ("ff7r_agent", "ff7r_agent.mcp_http_server", "ff7r_agent.mcp_runtime_server", "ff7r_agent.runtime_bridge", "ff7r_agent.runtime_worker", "ff7r_agent.startup"):
        path = module_paths.get(name)
        if not _path_is_under(path, expected_source_root):
            mismatches.append({"field": f"module_paths.{name}", "expected": f"path under {expected_source_root}", "actual": path})


def _deployment_record_check(
    path: Path | None,
    *,
    checkout: dict[str, Any],
    expectations: dict[str, Any],
) -> dict[str, Any] | None:
    if path is None:
        return None
    record = read_deployment_record(path)
    if record is None:
        return {"path": str(path), "ok": False, "mismatches": [{"field": "deployment_record", "actual": "missing_or_invalid"}]}
    mismatches: list[dict[str, Any]] = []
    for field, expected, actual in (
        ("git_commit_sha", checkout.get("git_commit_sha"), record.get("git_commit_sha")),
        ("git_branch", expectations["canonical_branch"], record.get("git_branch")),
        ("source_root", expectations["canonical"]["source_root"], record.get("source_root")),
    ):
        if field == "source_root":
            matches = os.path.normcase(os.path.normpath(str(expected))) == os.path.normcase(os.path.normpath(str(actual)))
        else:
            matches = expected == actual
        if not matches:
            mismatches.append({"field": field, "expected": expected, "actual": actual})
    if record.get("git_dirty") is not False:
        mismatches.append({"field": "git_dirty", "expected": False, "actual": record.get("git_dirty")})
    return {"path": str(path), "ok": not mismatches, "record": record, "mismatches": mismatches}


def build_report(
    *,
    repository_root: Path,
    manifest_path: Path,
    runtime_python: Path,
    connected_identity_path: Path | None = None,
    deployment_record_path: Path | None = None,
    checkout_identity: dict[str, Any] | None = None,
    runtime_identity: dict[str, Any] | None = None,
    connected_identity: dict[str, Any] | None = None,
    ownership_inventory: dict[str, Any] | None = None,
) -> dict[str, Any]:
    manifest = _read_json(manifest_path)
    if manifest is None:
        return {"ok": False, "status": STATUS_MANIFEST, "mismatches": [{"field": "manifest", "actual": "missing_or_invalid"}]}
    expectations, error = _manifest_expectations(manifest)
    if error:
        return {"ok": False, "status": STATUS_MANIFEST, "mismatches": [{"field": "manifest", "actual": error}]}
    assert expectations is not None
    canonical = expectations["canonical"]
    runtime_expected = expectations["runtime"]

    checkout = checkout_identity or collect_checkout_identity(repository_root)
    checkout_mismatches = list(
        verify_identity(
            checkout,
            expected_source_root=canonical.get("source_root"),
            expected_package_import_root=canonical.get("package_import_root"),
            expected_branch=expectations["canonical_branch"],
            expected_clean=True,
        )["mismatches"]
    )
    if not checkout.get("git_commit_sha"):
        checkout_mismatches.append({"field": "git_commit_sha", "expected": "present Git HEAD", "actual": None})

    runtime = runtime_identity or _probe_runtime(runtime_python)
    runtime_mismatches = list(
        verify_identity(
            runtime,
            expected_source_root=runtime_expected.get("source_root"),
            expected_package_import_root=runtime_expected.get("package_import_root"),
            expected_branch=expectations["canonical_branch"],
            expected_commit_sha=checkout.get("git_commit_sha"),
            expected_clean=True,
        )["mismatches"]
    )
    _add_python_mismatch(runtime_mismatches, runtime, runtime_expected["python_executable"])
    _add_module_path_mismatches(runtime_mismatches, runtime, runtime_expected["source_root"])
    if runtime.get("ok") is False:
        runtime_mismatches.append({"field": "runtime_probe", "expected": "importable", "actual": runtime.get("error")})

    connected = connected_identity
    if connected is None and connected_identity_path is not None:
        connected = _read_json(connected_identity_path)
    connected_mismatches: list[dict[str, Any]] = []
    if connected is None:
        status = STATUS_MCP
    else:
        connected_mismatches = list(
            verify_identity(
                connected,
                expected_source_root=runtime_expected.get("source_root"),
                expected_package_import_root=runtime_expected.get("package_import_root"),
                expected_branch=expectations["canonical_branch"],
                expected_commit_sha=checkout.get("git_commit_sha"),
                expected_clean=True,
            )["mismatches"]
        )
        _add_python_mismatch(connected_mismatches, connected, runtime_expected["python_executable"])
        _add_module_path_mismatches(connected_mismatches, connected, runtime_expected["source_root"])
        for field in ("worker_generation", "worker_pid", "bridge_identity", "gamepad_broker_identity", "runtime_directory", "lifecycle_directory"):
            if connected.get(field) in (None, {}, ""):
                connected_mismatches.append({"field": field, "expected": "present runtime identity field", "actual": connected.get(field)})
        status = STATUS_RUNTIME if connected_mismatches else STATUS_READY

    if checkout_mismatches:
        status = STATUS_CHECKOUT
    elif runtime_mismatches or connected_mismatches:
        status = STATUS_RUNTIME

    executable = manifest["supported_executable"]
    executable_path = Path(str(executable.get("path", "")))
    executable_hash = _sha256(executable_path)
    executable_mismatch = None
    if executable.get("sha256") and executable_hash != str(executable["sha256"]).upper():
        executable_mismatch = {"field": "supported_executable.sha256", "expected": executable.get("sha256"), "actual": executable_hash}
        status = STATUS_MANIFEST

    deployment = _deployment_record_check(
        deployment_record_path,
        checkout=checkout,
        expectations=expectations,
    )
    if deployment is not None and not deployment["ok"]:
        status = STATUS_RUNTIME

    ownership = ownership_inventory or collect_runtime_ownership_inventory()
    ownership_conflict_list = ownership_conflicts(ownership, connected)
    if ownership_conflict_list:
        status = STATUS_OWNERSHIP

    return {
        "ok": status == STATUS_READY and not executable_mismatch and not (deployment and not deployment["ok"]),
        "status": status if not executable_mismatch else STATUS_MANIFEST,
        "manifest": str(manifest_path),
        "identity_contract": {
            "canonical_commit_sha": checkout.get("git_commit_sha"),
            "source": "checkout_git_head",
            "runtime_gate": "runtime_git_head_equals_checkout_git_head",
            "clean_required": True,
            "manifest_self_reference": False,
        },
        "checkout": {"identity": checkout, "mismatches": checkout_mismatches},
        "runtime_probe": {"identity": runtime, "mismatches": runtime_mismatches},
        "connected_runtime": {"identity": connected, "mismatches": connected_mismatches, "snapshot_path": str(connected_identity_path) if connected_identity_path else None},
        "deployment_record": deployment,
        "runtime_ownership": {
            "inventory": ownership,
            "conflicts": ownership_conflict_list,
            "ready_eligible": not ownership_conflict_list,
        },
        "supported_executable": {"path": str(executable_path), "sha256": executable_hash, "mismatch": executable_mismatch},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only FF7R canonical identity doctor")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--runtime-python", type=Path, required=True)
    parser.add_argument("--repository-root", type=Path, default=None)
    parser.add_argument("--connected-identity-json", type=Path, default=None)
    parser.add_argument("--deployment-record", type=Path, default=None)
    args = parser.parse_args(argv)
    repository_root = (args.repository_root or Path(__file__).resolve().parents[4]).resolve()
    report = build_report(
        repository_root=repository_root,
        manifest_path=args.manifest.resolve(),
        runtime_python=args.runtime_python.resolve(),
        connected_identity_path=args.connected_identity_json.resolve() if args.connected_identity_json else None,
        deployment_record_path=args.deployment_record.resolve() if args.deployment_record else None,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    if not report["ok"]:
        print(f"FF7R DOCTOR: {report['status']}", file=sys.stderr)
        for scope in ("checkout", "runtime_probe", "connected_runtime", "deployment_record", "runtime_ownership"):
            for mismatch in report.get(scope, {}).get("mismatches", []):
                print(f"- {scope}.{mismatch.get('field')}: expected={mismatch.get('expected')!r} actual={mismatch.get('actual')!r}", file=sys.stderr)
        for conflict in report.get("runtime_ownership", {}).get("conflicts", []):
            print(f"- runtime_ownership.{conflict.get('kind')}: {conflict}", file=sys.stderr)
        return 1
    print("FF7R DOCTOR: READY", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
