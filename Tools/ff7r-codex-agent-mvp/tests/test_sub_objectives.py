from __future__ import annotations

import pytest

from ff7r_agent.navigation_values import MissionError
from ff7r_agent.objective_catalogue import sub_objective_states


def row(name: str, group: int, index: int, status: int, title: str) -> dict:
    return {"values": {"Name": name, "StoryProgressFlagID": "stf" + name, "GroupIndex": str(group),
                       "StoryIndex": str(index), "CompleteStatus": str(status), "ItemName": title,
                       "StoryType": "4"}}


# Chapter 1's town visit (StoryResident, 2660_Ch01_Step010 children).
ROWS = [row("s010_010", 1, 1, 0, "$strCh01_Step010_s010"), row("s010_990", 1, 2, 1, "$strCh01_Step010_s010"),
        row("s020_010", 2, 1, 0, "$strCh01_Step010_s020"), row("s020_020", 2, 2, 0, "$strCh01_Step010_s020"),
        row("s020_990", 2, 4, 1, "$strCh01_Step010_s020"),
        row("s040_010", 4, 1, 0, "$strCh01_Step010_s040"), row("s040_999", 4, 3, 7, "$strCh01_Step010_s040")]


def test_each_group_reports_its_latest_reached_row() -> None:
    flags = {r["values"]["StoryProgressFlagID"]: False for r in ROWS}
    flags.update({"stfs010_010": True, "stfs010_990": True, "stfs020_010": True, "stfs020_020": True,
                  "stfs040_010": True, "stfs040_999": True})
    states = {s["title_text_id"]: (s["state"], s["row"]["values"]["Name"] if s["row"] else None)
              for s in sub_objective_states(ROWS, flags)}
    assert states == {"$strCh01_Step010_s010": ("complete", "s010_990"),
                      "$strCh01_Step010_s020": ("active", "s020_020"),
                      "$strCh01_Step010_s040": ("closed", "s040_999")}


def test_unreached_groups_are_not_started_and_missing_flags_refuse() -> None:
    flags = {r["values"]["StoryProgressFlagID"]: False for r in ROWS}
    assert {s["state"] for s in sub_objective_states(ROWS, flags)} == {"not_started"}
    del flags["stfs020_020"]
    with pytest.raises(MissionError, match="flag_evidence_incomplete"):
        sub_objective_states(ROWS, flags)
