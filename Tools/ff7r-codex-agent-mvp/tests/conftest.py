"""Process-state isolation for the offline test suite."""

from __future__ import annotations

import os
import sys

import pytest
from pathlib import Path


def pytest_collection_modifyitems(items):
    """Do not claim integration coverage for inputs excluded from this distribution."""
    root = Path(__file__).resolve().parents[3]
    unavailable = {
        "test_campaign_readiness.py": (
            root / "ProjectKnowledge/Current/queens-blood-readiness.json",
            "Optional historical owner readiness/evidence matrix is not distributed",
        ),
        "test_session_startup_services.py": (
            root / "Tools/Start-FF7RSession.ps1",
            "External OBS/scheduled-task service manager is not distributed",
        ),
    }
    for item in items:
        module = item.module
        optional = unavailable.get(Path(module.__file__).name)
        if optional and not optional[0].exists():
            item.add_marker(pytest.mark.skip(reason=optional[1]))
        names = getattr(getattr(item.obj, "__code__", None), "co_names", ())
        for variable in ("SNAPSHOT", "HEADERS"):
            path = getattr(module, variable, None)
            depends = variable in names or (variable == "SNAPSHOT" and "topology" in item.fixturenames)
            if isinstance(path, Path) and not path.exists() and depends:
                item.add_marker(pytest.mark.skip(reason="Optional raw game evidence/SDK input is not distributed"))


@pytest.fixture(autouse=True)
def restore_test_process_state():
    """Restore FF7R configuration and import/process location after each test."""

    original_directory = os.getcwd()
    original_sys_path = list(sys.path)
    original_environment = {
        key: value
        for key, value in os.environ.items()
        if key.startswith("FF7R_") or key in {"PYTHONPATH", "LOCALAPPDATA", "TEMP", "TMP"}
    }
    yield

    tracked_keys = {
        key
        for key in os.environ
        if key.startswith("FF7R_") or key in {"PYTHONPATH", "LOCALAPPDATA", "TEMP", "TMP"}
    }
    for key in tracked_keys - original_environment.keys():
        os.environ.pop(key, None)
    for key, value in original_environment.items():
        os.environ[key] = value
    os.chdir(original_directory)
    sys.path[:] = original_sys_path
