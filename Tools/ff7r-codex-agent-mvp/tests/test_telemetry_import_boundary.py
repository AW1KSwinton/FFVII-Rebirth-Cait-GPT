"""Telemetry cannot pay mission-client lazy imports during validator warmup."""
import os
from pathlib import Path
import subprocess
import sys


def test_map_poi_telemetry_merge_does_not_import_mission_client():
    source = Path(__file__).resolve().parents[1] / "src"
    env = {**os.environ, "PYTHONPATH": str(source)}
    result = subprocess.run([sys.executable, "-c", """
import sys
from ff7r_agent.map_poi import merge_poi_snapshot
assert 'ff7r_agent.navigation_mission' not in sys.modules
assert 'asyncio' not in sys.modules
from ff7r_agent import navigation_values, navigation_mission
for name in ('MissionError', 'finite_position', 'horizontal_distance'):
    assert getattr(navigation_values, name) is getattr(navigation_mission, name)
"""], env=env, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
