"""Placement safety diagnostics survive the worker's error envelope."""
from ff7r_agent.runtime_worker import _error_reply


def test_map_recovery_error_preserves_native_diagnostics_and_zero_input():
    from ff7r_agent.navigation_values import MissionError
    evidence = {"presses": [], "menu_recovery": [{"identified": False, "reason": "ui_owner_unknown"}]}
    reply = _error_reply(MissionError("main_menu_open", response=evidence), {"id": 8}, "travel_to_map_spot")
    assert reply["ok"] is False and reply["error"] == "main_menu_open"
    assert reply["details"] == evidence
from ff7r_agent.service import SafetyError


def test_safety_error_details_survive_worker_reply():
    details = {"stage": "board_navigation", "inputs": [{"button": "down"}]}
    reply = _error_reply(SafetyError("placement ended", details=details), {"id": 7}, "place_queens_blood_card")
    assert reply["ok"] is False
    assert reply["error"] == "placement ended"
    assert reply["error_type"] == "SafetyError"
    assert reply["id"] == 7
    assert reply["details"] == details


def test_unrelated_error_retains_legacy_envelope():
    reply = _error_reply(ValueError("invalid request"), None, None)
    assert reply["error_type"] == "ValueError"
    assert reply["error"] == "invalid request"
    assert reply["id"] is None
    assert "details" not in reply
