from ff7r_agent.conversation_state import dialogue_active, field_blocker


BLANK = {"map_id": None, "loading": False, "in_combat": False, "target": None,
         "player": {"position": None},
         "queen_blood": {"active": False, "match_screen": False, "begin_match_available": False}}


def test_blank_feed_is_not_reported_as_dialogue():
    assert not dialogue_active(BLANK)
    assert field_blocker(BLANK) == "field_telemetry_unavailable"


def test_explicit_dialogue_evidence_is_dialogue():
    assert field_blocker({**BLANK, "interaction": {"state": "dialogue"}}) == "dialogue_active"
    wheel = {**BLANK, "queen_blood": {**BLANK["queen_blood"], "talk_menu": {"observed": True, "shown": True}}}
    assert field_blocker(wheel) == "dialogue_active"


def test_field_with_position_has_no_blocker():
    field = {**BLANK, "map_id": "EndGame", "player": {"position": {"x": 1.0, "y": 2.0, "z": 3.0}}}
    assert field_blocker(field) is None


def test_snapshot_without_map_metadata_is_not_blocked():
    assert field_blocker({"player": {"position": None}}) is None
