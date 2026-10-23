import asyncio

import pytest

from ff7r_agent import npc_approach


@pytest.mark.parametrize("arrival", [{"x": 100.0, "y": 200.0, "z": 0.0}, None])
def test_verified_moogle_arrival_is_preserved_but_missing_receipt_allows_normal_reset(tmp_path, monkeypatch, arrival):
    calls = []

    async def approach(*args, **kwargs):
        calls.append("approach")
        return {"ok": False, "reason": "npc_approach_no_capture_covers_route",
                "guide_moogle": {"destination": "Wonderment Square", "arrival_position": arrival}}

    async def travel(*args, **kwargs):
        calls.append("travel")
        return {"arrival_position": {"x": 0.0, "y": 0.0, "z": 0.0}}

    monkeypatch.setattr(npc_approach, "_approach_once", approach)
    monkeypatch.setattr(npc_approach, "travel_to_spot", travel)
    report = asyncio.run(npc_approach.approach_npc(None, "Tournament Organizer", output=tmp_path,
                                                identity_check=None, execute=True, reset_spot="$map2350"))
    if arrival:
        assert calls == ["approach"]
        assert report["reset_suppressed"]["reason"] == "verified_guide_moogle_arrival"
        assert report["guide_moogle"]["arrival_position"] == arrival
    else:
        assert calls == ["approach", "travel", "approach"]
        assert "reset_suppressed" not in report
