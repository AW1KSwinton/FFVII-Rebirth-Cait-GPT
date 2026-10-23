import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ff7r_agent.telemetry import JsonFileTelemetry  # noqa: E402


def base_snapshot(sequence: int) -> dict:
    return {
        "schema_version": 1,
        "sequence": sequence,
        "timestamp_utc": "2026-09-14T12:00:00.000Z",
        "game": {
            "running": True,
            "paused": False,
            "in_menu": False,
            "in_combat": False,
            "map_id": None,
            "location": "",
            "objective": "",
        },
        "player": None,
        "party": [],
    }


def queen_blood_snapshot(**overrides: object) -> dict:
    payload = {
        "schema_version": 1,
        "active": True,
        "phase": "player_turn",
        "turn": 1,
        "match_id": "match-1",
        "screen": None,
        "begin_match_available": False,
        "match_screen": False,
        "cursor": {"area": "hand", "hand_index": 0, "row": 0, "column": 0},
        "hand": [{"hand_index": 0, "card_id": "cactuar"}],
        "legal_moves": [{"hand_index": 0, "card_id": "cactuar", "row": 0, "column": 0}],
        "board": {"rows": 3, "columns": 5, "cells": []},
    }
    payload.update(overrides)
    return payload


class QueenBloodTelemetryTests(unittest.TestCase):
    def test_exact_sequence_sidecar_is_merged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path = root / "state.json"
            queen_blood_path = root / "queen_blood.json"
            state_path.write_text(json.dumps(base_snapshot(7)), encoding="utf-8")
            queen_blood_path.write_text(
                json.dumps({
                    "schema_version": 1,
                    "base_sequence": 7,
                    "queen_blood": queen_blood_snapshot(),
                }),
                encoding="utf-8",
            )

            state = JsonFileTelemetry(state_path, queen_blood_path=queen_blood_path).observe()
            self.assertEqual(state.queen_blood["phase"], "player_turn")

    def test_source_sequence_sidecar_uses_native_sample_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path = root / "state.json"
            queen_blood_path = root / "queen_blood.json"
            state_path.write_text(
                json.dumps({**base_snapshot(7), "source_sequence": 12}),
                encoding="utf-8",
            )
            queen_blood_path.write_text(
                json.dumps({
                    "schema_version": 1,
                    "base_sequence": 12,
                    "queen_blood": queen_blood_snapshot(),
                }),
                encoding="utf-8",
            )

            state = JsonFileTelemetry(state_path, queen_blood_path=queen_blood_path).observe()
            self.assertEqual(state.queen_blood["phase"], "player_turn")

    def test_trusted_probe_sidecar_uses_bounded_timestamp_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path = root / "state.json"
            queen_blood_path = root / "queen_blood.json"
            state_path.write_text(
                json.dumps({
                    **base_snapshot(7),
                    "source_sequence": 12,
                    "source_timestamp_utc": "2026-09-14T12:00:00.800Z",
                }),
                encoding="utf-8",
            )
            queen_blood_path.write_text(
                json.dumps({
                    "schema_version": 1,
                    "source": "ue4ss_queen_blood_read_only_probe",
                    "base_sequence": 8,
                    "timestamp_utc": "2026-09-14T12:00:00Z",
                    "queen_blood": queen_blood_snapshot(),
                }),
                encoding="utf-8",
            )

            state = JsonFileTelemetry(state_path, queen_blood_path=queen_blood_path).observe()
            self.assertEqual(state.queen_blood["phase"], "player_turn")

    def test_trusted_probe_sidecar_outside_timestamp_bound_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path = root / "state.json"
            queen_blood_path = root / "queen_blood.json"
            state_path.write_text(
                json.dumps({
                    **base_snapshot(7),
                    "source_sequence": 12,
                    "source_timestamp_utc": "2026-09-14T12:00:02.000Z",
                }),
                encoding="utf-8",
            )
            queen_blood_path.write_text(
                json.dumps({
                    "schema_version": 1,
                    "source": "ue4ss_queen_blood_read_only_probe",
                    "base_sequence": 8,
                    "timestamp_utc": "2026-09-14T12:00:00Z",
                    "queen_blood": queen_blood_snapshot(),
                }),
                encoding="utf-8",
            )

            state = JsonFileTelemetry(state_path, queen_blood_path=queen_blood_path).observe()
            self.assertIsNone(state.queen_blood)

    def test_stale_sequence_sidecar_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path = root / "state.json"
            queen_blood_path = root / "queen_blood.json"
            state_path.write_text(json.dumps(base_snapshot(8)), encoding="utf-8")
            queen_blood_path.write_text(
                json.dumps({
                    "schema_version": 1,
                    "base_sequence": 7,
                    "queen_blood": queen_blood_snapshot(),
                }),
                encoding="utf-8",
            )

            state = JsonFileTelemetry(state_path, queen_blood_path=queen_blood_path).observe()
            self.assertIsNone(state.queen_blood)


if __name__ == "__main__":
    unittest.main()
