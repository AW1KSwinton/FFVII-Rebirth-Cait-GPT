import pytest

from ff7r_agent.service import AgentService, SafetyError
from test_map_query import MapService, response


def test_root_census_returns_fresh_diagnostics_without_authorizing_input(tmp_path):
    roots = {"observed": True, "widgets": [{"class": "Subtitle00_C", "is_visible": False, "in_viewport": False}],
             "truncated": False, "input_authorized": False}
    service = MapService(tmp_path, payload={**response(), "ui_roots": roots})
    result = AgentService.read_ui_roots(service)
    assert result["ok"] and result["roots"] == roots
    assert not result["input_sent"] and not result["input_authorized"]
    assert result["request_id"] and type(result["source_sequence"]) is int


def test_older_exporter_cannot_masquerade_as_an_absent_overlay(tmp_path):
    with pytest.raises(SafetyError, match="unavailable"):
        AgentService.read_ui_roots(MapService(tmp_path, payload=response()))


def test_detailed_roots_are_explicit_and_remain_read_only(tmp_path):
    roots = {"observed": True, "widgets": [], "input_authorized": False}
    service = MapService(tmp_path, payload={**response(), "ui_roots": roots})
    result = AgentService.read_ui_roots(service, details=True)
    assert service.requests[0].split("|")[3] == "uiroots,uirootdetails"
    assert not result["input_authorized"] and not result["input_sent"]
    with pytest.raises(SafetyError, match="boolean"):
        AgentService.read_ui_roots(service, details="true")
