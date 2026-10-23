import json
import threading
from pathlib import Path

import pytest

from ff7r_agent.map_projection import build_projection, fit_affine, height_grids, parse_distance_text
from ff7r_agent.map_query import (
    MapView, category_for, classify_icon, get_map_target, map_point_to_world, observe_map_pins,
    request_snapshot, REQUEST_NAME, RESPONSE_NAME,
)
from ff7r_agent.navigation_values import MissionError
from map_fixtures import Projection, base_sample, icon, response

EXPORTER = Path(__file__).resolve().parents[2] / "ff7r-telemetry-exporter" / "Base"


class FakeTelemetry:
    def __init__(self, path):
        self.path = path


class MapService:
    """Answers map-query.request the way the exporter does, from a fixture."""

    def __init__(self, tmp_path, payload=None, base=None, answer=True):
        self.telemetry = FakeTelemetry(tmp_path / "state.json")
        self._lock = threading.Lock()
        self.payload = payload if payload is not None else response()
        self.base = base or base_sample()
        self.answer = answer
        self.requests = []

    def observe(self):
        return json.loads(json.dumps(self.base))

    def _sleeper(self, _seconds):
        request = Path(self.telemetry.path).with_name(REQUEST_NAME)
        if self.answer and request.exists():
            request_id = request.read_text().split("|")[0]
            self.requests.append(request.read_text())
            request.with_name(RESPONSE_NAME).write_text(json.dumps({**self.payload, "request_id": request_id}))


@pytest.mark.parametrize("degrees,scale", [(0, 0.02), (37, 0.05), (-90, 0.001)])
def test_projection_inverts_the_games_own_conversion(degrees, scale):
    projection = Projection(scale=scale, degrees=degrees)
    fitted = build_projection(response(projection))
    for world in [(1000, 2000), (-50000, 73000), (250000, -4000)]:
        local = projection.local(*world)
        assert fitted.to_local(*world) == pytest.approx(local, abs=1e-6)
        assert fitted.to_world(*local) == pytest.approx(world, abs=1e-3)
    assert fitted.alignment == "direct" and fitted.fit_residual_px < 1e-6


def test_projection_rejects_non_affine_or_degenerate_calibration():
    payload = response()
    payload["calibration"][-1]["map"]["x"] += 40
    with pytest.raises(MissionError, match="map_projection_not_affine"):
        build_projection(payload)
    payload = response()
    payload["calibration"] = payload["calibration"][:2]
    with pytest.raises(MissionError, match="map_calibration_insufficient"):
        build_projection(payload)
    with pytest.raises(MissionError, match="map_calibration_degenerate"):
        fit_affine([((0, 0), (0, 0)), ((1, 1), (1, 1)), ((2, 2), (2, 2))])


def test_view_relative_conversion_is_aligned_by_the_player_indicator():
    projection = Projection(convert_offset=(400, 300))
    fitted = build_projection(response(projection))
    assert fitted.alignment == "player_indicator_offset"
    assert fitted.to_world(*projection.local(9000, -3000)) == pytest.approx((9000, -3000), abs=1e-3)
    assert build_projection(response(projection, indicator=False)).alignment == "unverified"
    with pytest.raises(MissionError, match="alignment_implausible"):
        build_projection(response(Projection(convert_offset=(5000, 0))))


def test_screen_pixels_convert_through_the_viewport_corners():
    projection = Projection()
    fitted = build_projection(response(projection))
    lx, ly = projection.local(5000, 2500)
    assert fitted.pixel_to_local(160 + 1.5 * lx, 90 + 1.5 * ly) == pytest.approx((lx, ly))
    with pytest.raises(MissionError, match="screen_conversion_unavailable"):
        build_projection(response(projection, viewport=False)).pixel_to_local(1, 1)


def grid(samples, bx=3, by=2, base=(0.0, 0.0, 0.0), block=5000.0):
    return {"id": "Heights", "block_size": block, "blocks": {"x": bx, "y": by},
            "base_position": dict(zip("xyz", base)), "sample_count": len(samples), "samples": samples}


def test_height_grid_order_is_chosen_by_agreement_with_the_player():
    # x-fastest rows: y=0 -> [60, 60, 900], y=5000 -> [60, 60, 900]. Transposed
    # reading would put 900 under the player, so only one order is accepted.
    selected, _ = height_grids([grid([60, 60, 900, 60, 60, 900])], (1000, 2000, 150))
    assert selected is not None and selected.x_fastest
    assert selected.sample(7500, 2500) == pytest.approx(480)
    assert selected.sample(20000, 0) is None
    rejected, _ = height_grids([grid([900] * 6)], (1000, 2000, 150))
    assert rejected is None


def test_icon_kinds_come_from_properties_then_textures():
    assert classify_icon({"properties": {"IconKind": "EEndMenuMapIconKind::ChocoboCarriage"}}) == ("ChocoboCarriage", "property:IconKind")
    assert classify_icon({"properties": {"IconKind": "16"}}) == ("Chadley", "property:IconKind")
    assert classify_icon({"images": ["PaperSprite /Game/U_Icon_SubQuest_Goal.U_Icon_SubQuest_Goal"]})[0] == "SubStory"
    assert classify_icon({"images": ["Texture2D /Game/Map/U_Map_Icon_ChocoboStable.U_Map_Icon_ChocoboStable"]}) == ("ChocoboStable", "texture:name_match")
    assert classify_icon({"images": ["Texture2D /Game/U_Unknown.U_Unknown"]}) == (None, None)
    assert [category_for(k) for k in ("ChocoboCarriageBefore", "ChocoboCarriage", "TalkLikability3", "SubStoryActive", "UserMarker", "SubjugationPointRareAfter", None)] == [
        "other", "fast_travel", "npc", "quest", "user_pin", "enemy", "unclassified"]


def test_distance_labels_parse_and_a_majority_mismatch_rejects_the_projection():
    assert parse_distance_text("120m") == 120 and parse_distance_text("1.5km") == 1500 and parse_distance_text("?") is None
    projection = Projection()
    good = icon(projection, "a", (11000, 2000), distance_text="100m")
    MapView(response(projection, icons=[good]), base_sample())
    bad = icon(projection, "b", (11000, 2000), distance_text="400m")
    with pytest.raises(MissionError, match="distance_mismatch"):
        MapView(response(projection, icons=[bad]), base_sample())


def test_request_is_one_shot_and_removed_after_the_answer(tmp_path):
    service = MapService(tmp_path)
    payload, base = request_snapshot(service)
    assert payload["status"] == "ready" and base["map_id"] == "EndGame"
    assert len(service.requests) == 1 and service.requests[0].endswith("|snapshot|heights")
    assert not (tmp_path / REQUEST_NAME).exists()


def test_request_fails_closed(tmp_path):
    with pytest.raises(MissionError, match="map_query_timeout"):
        request_snapshot(MapService(tmp_path, answer=False), timeout_seconds=0.5)
    assert not (tmp_path / REQUEST_NAME).exists()
    with pytest.raises(MissionError, match="map_query_map_changed"):
        request_snapshot(MapService(tmp_path, payload=response(map_id="Other")))
    stale = base_sample()
    stale["telemetry"]["live"] = False
    with pytest.raises(MissionError, match="telemetry_stale"):
        request_snapshot(MapService(tmp_path, base=stale))
    with pytest.raises(MissionError, match="not_paused"):
        request_snapshot(MapService(tmp_path, payload={**response(), "paused": 0}))
    with pytest.raises(MissionError, match="map_not_open"):
        observe_map_pins(MapService(tmp_path, payload=response(status="map_not_open")))


def test_pins_carry_world_coordinates_and_become_route_targets(tmp_path):
    projection = Projection(degrees=15)
    icons = [
        icon(projection, "quest", (6000, 2000), images=["X /Game/U_Icon_MainStory_Goal.U_Icon_MainStory_Goal"]),
        icon(projection, "npc", (1500, 2600), properties={"IconKind": "Talk"}),
        icon(projection, "hidden", (0, 0), visible=False),
    ]
    heights = [grid([60, 60, 60, 60, 60, 60], bx=3, by=2)]
    actor = {"id": "NPC_1", "position": {"x": 1520, "y": 2610, "z": 222}}
    service = MapService(tmp_path, payload=response(projection, icons=icons),
                         base=base_sample(actors=[actor]))
    result = observe_map_pins(service)
    assert [p["pin_id"] for p in result["pins"]] == ["quest", "npc"]
    quest, npc = result["pins"]
    assert quest["category"] == "quest" and quest["world"]["x"] == pytest.approx(6000, abs=0.01)
    assert quest["navigable"] is False and quest["world"]["z_source"] == "unresolved"
    assert npc["world"]["z"] == 222 and npc["world"]["z_source"] == "live_actor:NPC_1"
    assert npc["navigable"] is True and result["counts"] == {"quest": 1, "npc": 1}
    target = get_map_target(service, npc["target_id"])["target"]
    assert target["position"]["y"] == pytest.approx(2600, abs=0.01) and target["map_id"] == "EndGame"

    service.payload = response(projection, icons=icons, heights=heights)
    only_quests = observe_map_pins(service, ["quest"])
    assert [p["pin_id"] for p in only_quests["pins"]] == ["quest"]
    assert only_quests["pins"][0]["world"]["z"] == pytest.approx(60)
    with pytest.raises(MissionError, match="category_invalid"):
        observe_map_pins(service, ["shops"])


def test_any_map_point_cursor_or_pixel_becomes_a_world_target(tmp_path):
    projection = Projection(degrees=-30, scale=0.01)
    cursor = projection.local(-4000, 9000)
    service = MapService(tmp_path, payload=response(projection, cursor=cursor))
    by_cursor = map_point_to_world(service, space="cursor")["target"]
    assert (by_cursor["position"]["x"], by_cursor["position"]["y"]) == pytest.approx((-4000, 9000), abs=0.01)
    lx, ly = projection.local(3000, 3000)
    by_pixel = map_point_to_world(service, 160 + 1.5 * lx, 90 + 1.5 * ly, space="screen", z=75, label="bridge")["target"]
    assert by_pixel["position"] == pytest.approx({"x": 3000, "y": 3000, "z": 75}, abs=0.01)
    assert by_pixel["navigable"] and by_pixel["z_source"] == "caller" and by_pixel["label"] == "bridge"
    with pytest.raises(MissionError, match="space_invalid"):
        map_point_to_world(service, 1, 1, space="world")
    with pytest.raises(MissionError, match="map_point_invalid"):
        map_point_to_world(service, float("nan"), 1)
    with pytest.raises(MissionError, match="unknown_or_expired"):
        get_map_target(service, "0" * 16)
    with pytest.raises(MissionError, match="target_id_invalid"):
        get_map_target(service, "../etc")


def test_lua_reader_output_is_accepted_by_the_python_consumer():
    lupa = pytest.importorskip("lupa.lua54")
    runtime = lupa.LuaRuntime(unpack_returned_tuples=True)
    runtime.globals().arg = runtime.table(str(EXPORTER / "Scripts" / "map_query.lua"))
    text = runtime.eval(f'loadfile([[{EXPORTER / "tests" / "map_query_regression.lua"}]])')()
    payload = json.loads(text)
    view = MapView(payload, base_sample(player={"x": 1000, "y": 2000, "z": 150}))
    quest = next(p for p in view.pins if p["visible"])
    assert quest["kind"] == "MainStory" and quest["category"] == "quest"
    assert (quest["world"]["x"], quest["world"]["y"]) == pytest.approx((6000, 2000), abs=0.01)
    assert quest["world"]["z"] == pytest.approx(60) and quest["world"]["z_source"] == "navimap_height_data"
    # The reader skips ConvertToPosition2D live, so the scale-based fallback is used.
    assert view.projection.alignment == "pixel_per_cm_north_up_assumed" and view.distance_check["checked"] == 1
    cursor = view.summary()["cursor"]["world"]
    assert (cursor["x"], cursor["y"]) == pytest.approx((2000, 2500), abs=0.01)


def test_field_prompt_reads_only_the_prompt_widgets(tmp_path):
    from ff7r_agent.field_prompt import read_field_prompt
    service = MapService(tmp_path, payload={**response(), "status": "map_not_open", "texts": [],
                                            "field_prompt": {"commands": ["$level_action_talk"]}})
    prompt = read_field_prompt(service)
    assert prompt["talk"] and not prompt["door"]
    assert [request.split("|")[3] for request in service.requests] == ["fieldprompt"]


def test_field_prompt_falls_back_to_the_all_texts_scan_on_an_older_exporter(tmp_path):
    from ff7r_agent.field_prompt import COMMAND_PATH, read_field_prompt
    texts = [{"text": "$level_action_door", "path": "BP_EndGameInstance." + COMMAND_PATH},
             {"text": "$level_action_talk", "path": "SomeOtherWidget.WidgetTree.Txt_Label"}]
    service = MapService(tmp_path, payload={**response(), "status": "map_not_open", "texts": texts})
    prompt = read_field_prompt(service)
    assert prompt["door"] and not prompt["talk"]
    assert [request.split("|")[3] for request in service.requests] == ["fieldprompt", "alltexts"]
