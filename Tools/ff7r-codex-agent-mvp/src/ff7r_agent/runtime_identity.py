"""Read-only identity reporting for the FF7R bridge and reloadable worker."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable
from .subprocess_options import no_console_window_kwargs


IDENTITY_SCHEMA_VERSION = 2
DEFAULT_IDENTITY_MODULES = (
    "ff7r_agent",
    "ff7r_agent.mcp_http_server",
    "ff7r_agent.mcp_runtime_server",
    "ff7r_agent.runtime_bridge",
    "ff7r_agent.runtime_bridge_process",
    "ff7r_agent.runtime_worker",
    "ff7r_agent.runtime_commands",
    "ff7r_agent.runtime_profile",
    "ff7r_agent.startup",
    "ff7r_agent.autonomy",
    "ff7r_agent.factory",
    "ff7r_agent.gamepad_broker",
    "ff7r_agent.static_obstacles",
    "ff7r_agent.live_route_adapter",
)


def default_runtime_directory() -> Path | None:
    configured = os.environ.get("FF7R_AUTONOMY_RUNTIME_DIR")
    if configured:
        return Path(configured)
    value = os.environ.get("LOCALAPPDATA")
    return Path(value) / "ff7r-cait-gpt" if value else None


def _normalise_path(value: str | os.PathLike[str] | None) -> str | None:
    if value is None:
        return None
    try:
        return str(Path(value).resolve())
    except OSError:
        return str(value)


def _git_value(root: Path | None, arguments: Iterable[str]) -> str | None:
    if root is None:
        return None
    try:
        result = subprocess.run(
            [
                "git",
                "--no-optional-locks",  # read-only; see runtime_profile._git
                "-c",
                f"safe.directory={root.as_posix()}",
                "-C",
                str(root),
                *arguments,
            ],
            capture_output=True,
            text=True,
            timeout=3.0,
            check=False,
            **no_console_window_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def find_git_root(path: str | os.PathLike[str] | None) -> Path | None:
    candidate = Path(path or __file__).resolve()
    if candidate.is_file():
        candidate = candidate.parent
    for parent in (candidate, *candidate.parents):
        if (parent / ".git").exists():
            return parent
    return None


def find_package_root(path: str | os.PathLike[str] | None) -> Path | None:
    candidate = Path(path or __file__).resolve()
    if candidate.is_file():
        candidate = candidate.parent
    for parent in (candidate, *candidate.parents):
        if (parent / "pyproject.toml").exists():
            return parent
    return None


def _module_path(name: str) -> str | None:
    module = sys.modules.get(name)
    if module is not None and getattr(module, "__file__", None):
        return _normalise_path(module.__file__)
    try:
        spec = importlib.util.find_spec(name)
    except (ImportError, ModuleNotFoundError, ValueError):
        return None
    if spec is None or not spec.origin or spec.origin in {"built-in", "frozen"}:
        return None
    return _normalise_path(spec.origin)


def _git_metadata(git_root: Path | None, source_root: Path | None = None) -> dict[str, Any]:
    branch = _git_value(git_root, ("rev-parse", "--abbrev-ref", "HEAD"))
    commit = _git_value(git_root, ("rev-parse", "HEAD"))
    dirty_text = _git_value(git_root, ("status", "--porcelain", "--untracked-files=all"))
    dirty_paths = []
    if dirty_text:
        for line in dirty_text.splitlines():
            dirty_paths.append(line[3:] if len(line) >= 3 else line)
    source_commit = None
    if git_root is not None and source_root is not None:
        try:
            relative_source = source_root.resolve().relative_to(git_root.resolve())
            source_commit = _git_value(
                git_root,
                ("log", "-1", "--format=%H", "--", relative_source.as_posix()),
            )
        except (ValueError, OSError):
            source_commit = None
    return {
        "git_root": str(git_root) if git_root else None,
        "git_branch": None if branch in (None, "HEAD") else branch,
        "git_commit": commit,
        "git_commit_sha": commit,
        "source_commit_sha": source_commit,
        "git_dirty": dirty_text is not None and bool(dirty_text),
        "git_dirty_paths": dirty_paths,
    }


def collect_identity(
    *,
    process_role: str,
    module_path: str | os.PathLike[str] | None = None,
    worker_generation: int | None = None,
    worker_pid: int | None = None,
    bridge_identity: dict[str, Any] | None = None,
    broker_identity: dict[str, Any] | None = None,
    runtime_directory: str | os.PathLike[str] | None = None,
    module_names: Iterable[str] = DEFAULT_IDENTITY_MODULES,
) -> dict[str, Any]:
    loaded_module_path = _normalise_path(module_path or __file__)
    package_root = find_package_root(loaded_module_path)
    package_import_root = (
        package_root / "src" if package_root and (package_root / "src").exists() else None
    )
    git_root = find_git_root(package_root or loaded_module_path)
    metadata = _git_metadata(git_root, package_root)
    runtime_dir = _normalise_path(runtime_directory) if runtime_directory else default_runtime_directory()
    from .runtime_profile import profile_identity_fields

    profile_fields = profile_identity_fields()
    if profile_fields.get("repository_root") is None and git_root is not None:
        profile_fields["repository_root"] = str(git_root)
    return {
        "schema_version": IDENTITY_SCHEMA_VERSION,
        "process_role": process_role,
        "pid": os.getpid(),
        "source_root": str(package_root) if package_root else loaded_module_path,
        "package_import_root": str(package_import_root) if package_import_root else None,
        "loaded_package_path": _module_path("ff7r_agent"),
        "loaded_module_path": loaded_module_path,
        "module_paths": {name: _module_path(name) for name in module_names},
        "python_executable": _normalise_path(sys.executable),
        "mcp_transport": os.getenv("FF7R_MCP_TRANSPORT"),
        "worker_generation": worker_generation,
        "worker_pid": worker_pid,
        "bridge_identity": bridge_identity
        or {
            "name": "ff7r-agent-mcp",
            "entrypoint": "ff7r_agent.mcp_runtime_server:main",
            "pid": os.getpid(),
        },
        "gamepad_broker_identity": broker_identity
        or {
            "pid": None,
            "port": None,
            "active": False,
            "device_active": False,
            "process_alive": False,
            "listener_alive": False,
            "status": "not_attached_to_this_process",
        },
        "runtime_directory": str(runtime_dir) if runtime_dir else None,
        "lifecycle_directory": (
            str(Path(runtime_dir) / "lifecycle") if runtime_dir else None
        ),
        **metadata,
        **profile_fields,
    }


def collect_checkout_identity(repository_root: str | os.PathLike[str]) -> dict[str, Any]:
    """Report the checkout itself without requiring package imports."""
    root = Path(repository_root).resolve()
    package_root = root / "Tools" / "ff7r-codex-agent-mvp"
    metadata = _git_metadata(root, package_root)
    from .runtime_profile import profile_identity_fields

    profile_fields = profile_identity_fields()
    profile_fields["repository_root"] = str(root)
    return {
        "schema_version": IDENTITY_SCHEMA_VERSION,
        "process_role": "checkout",
        "pid": os.getpid(),
        "source_root": str(package_root),
        "repository_root": str(root),
        "package_import_root": str(package_root / "src"),
        "loaded_package_path": None,
        "loaded_module_path": None,
        "module_paths": {},
        "python_executable": _normalise_path(sys.executable),
        "worker_generation": None,
        "worker_pid": None,
        "bridge_identity": None,
        "gamepad_broker_identity": None,
        "runtime_directory": str(default_runtime_directory()) if default_runtime_directory() else None,
        "lifecycle_directory": (
            str(default_runtime_directory() / "lifecycle")
            if default_runtime_directory()
            else None
        ),
        **metadata,
        **profile_fields,
    }


def _same_path(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return False
    return os.path.normcase(os.path.normpath(str(left))) == os.path.normcase(os.path.normpath(str(right)))


def verify_identity(
    identity: dict[str, Any],
    *,
    expected_source_root: str | os.PathLike[str] | None = None,
    expected_package_import_root: str | os.PathLike[str] | None = None,
    expected_commit_sha: str | None = None,
    expected_source_commit_sha: str | None = None,
    expected_branch: str | None = None,
    expected_clean: bool | None = None,
) -> dict[str, Any]:
    mismatches: list[dict[str, Any]] = []
    comparisons = (
        ("source_root", expected_source_root, identity.get("source_root"), _same_path),
        (
            "package_import_root",
            expected_package_import_root,
            identity.get("package_import_root"),
            _same_path,
        ),
        ("git_commit_sha", expected_commit_sha, identity.get("git_commit_sha"), lambda a, b: a == b),
        (
            "source_commit_sha",
            expected_source_commit_sha,
            identity.get("source_commit_sha"),
            lambda a, b: a == b,
        ),
        ("git_branch", expected_branch, identity.get("git_branch"), lambda a, b: a == b),
    )
    for field, expected, actual, matcher in comparisons:
        if expected is not None and not matcher(expected, actual):
            mismatches.append({"field": field, "expected": str(expected), "actual": actual})
    if expected_clean is True and identity.get("git_dirty") is not False:
        mismatches.append(
            {
                "field": "git_dirty",
                "expected": False,
                "actual": identity.get("git_dirty"),
                "paths": identity.get("git_dirty_paths", []),
            }
        )
    return {"ok": not mismatches, "mismatches": mismatches}
