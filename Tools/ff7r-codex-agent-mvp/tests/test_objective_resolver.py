from copy import deepcopy
from datetime import datetime, timezone

import pytest

from ff7r_agent.objective_resolver import objective_key, resolve_active_objective

NOW = datetime(2026, 10, 7, 14, 0, tzinfo=timezone.utc)
IDENTITY = {"world_id": "world", "map_id": "EndGame", "process_token": "creation-1"}


def snapshot():
    return {"schema_version": 1, "sequence": 42, "observed": True, "observed_at": NOW.isoformat(),
            "identity": dict(IDENTITY), "source": "offline objective replay", "authority": "fixture only",
            "objectives": [{"state": "active", "quest_id": "quest", "objective_id": "A", "step_id": "step-1",
                            "targets": [{"target_type": "object_id", "target_id": "NPC1", "source": "fixture"}]}]}


def located(reference):
    return {"status": "located", "position": {"x": 1, "y": 2, "z": 3}, "map_id": "EndGame",
            "source": "offline resolver", "authority": "fixture only"}


def resolve(value, resolver=located):
    return resolve_active_objective(value, current_identity=IDENTITY, resolve_target=resolver, now=NOW)


def test_transition_keeps_what_where_and_provenance_separate():
    first = resolve(snapshot())
    assert first["ok"] and first["destination_type"] == "exact" and not first["input_sent"]
    assert first["reference_source"] == "fixture" and first["objective_source"] == "offline objective replay"
    second = snapshot()
    second["objectives"][0]["step_id"] = "step-2"
    assert objective_key(first) != objective_key(resolve(second))


def test_unloaded_target_has_an_explicit_macro_destination():
    result = resolve(snapshot(), lambda _: {"status": "macro", "field": "Junon", "sub_area": "Crow's Nest",
                                           "position": None, "source": "static placement", "authority": "fixture only"})
    assert result["ok"] and result["position"] is None and result["requires_reacquisition"]


@pytest.mark.parametrize("defect", ["identity", "old", "future", "unobserved", "unknown_state", "multiple", "targets", "missing_step"])
def test_ambiguous_or_stale_snapshots_do_not_call_the_target_resolver(defect):
    value = snapshot()
    if defect == "identity": value["identity"]["process_token"] = "restarted"
    elif defect == "old": value["observed_at"] = "2026-10-07T13:59:00Z"
    elif defect == "future": value["observed_at"] = "2026-10-07T14:01:00Z"
    elif defect == "unobserved": value["observed"] = False
    elif defect == "unknown_state": value["objectives"].append({"state": "unknown"})
    elif defect == "multiple": value["objectives"].append(deepcopy(value["objectives"][0]))
    elif defect == "targets": value["objectives"][0]["targets"] *= 2
    else: del value["objectives"][0]["step_id"]
    def forbidden(_): raise AssertionError("must fail before resolving")
    assert not resolve(value, forbidden)["ok"]


@pytest.mark.parametrize("bad", [{"x": float("nan"), "y": 2, "z": 3}, {"x": True, "y": 2, "z": 3}, None])
def test_bad_coordinates_never_become_navigation_destinations(bad):
    assert not resolve(snapshot(), lambda ref: {**located(ref), "position": bad})["ok"]
