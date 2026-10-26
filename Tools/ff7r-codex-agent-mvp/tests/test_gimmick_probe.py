"""Explore mode's first crossing of a gimmick: held push, landing recorded."""

from __future__ import annotations

import math
from types import SimpleNamespace

from ff7r_agent import gimmick_links as gl
from ff7r_agent import gimmick_probe as gp
from ff7r_agent.models import Vec3

CRAWL = gl.TraversalGimmick("Crouched00_C /Test:PersistentLevel.Crawl", "crouch",
                            Vec3(0.0, 0.0, 0.0), Vec3(360.0, 0.0, 0.0))


class Floor:
    """x < 50: departure island (0, 20 polygons); 340..400: a 2-polygon pad (1);
    x > 1100: the far island (2, 30 polygons). Nothing in between."""

    def ground_islands(self):
        return {**{index: 0 for index in range(20)}, 100: 1, 101: 1, **{200 + i: 2 for i in range(30)}}

    def resolve_point_if_resolvable(self, point, *, max_snap_distance_m, max_vertical_separation_m):
        reference = 0 if point.x < 50 else 100 if 340 <= point.x <= 400 else 200 if point.x > 1100 else None
        return SimpleNamespace(ok=reference is not None, selected_reference=reference,
                               closest_point=Vec3(point.x, point.y, 0.0) if reference is not None else None)


class Character:
    """Moves 150 cm/s along the held heading; the game holds it (not
    grounded) while x is inside the passage, 50..340."""

    def __init__(self, blocked_at=None):
        self.position = Vec3(-60.0, 0.0, 0.0)
        self.now = 0.0
        self.heading = None
        self.blocked_at = blocked_at
        self.released = 0
        self.input = SimpleNamespace(supervised_move=self.supervised_move, release_all=self.release_all)

    def supervised_move(self, x, y, *, deadline, stream_id, sequence, sprint):
        # Camera yaw 0: stick +y is world +x.
        self.heading = math.degrees(math.atan2(x, y)) if (x or y) else None
        from ff7r_agent.continuous_control import MAX_SUPERVISED_LEASE_SECONDS
        assert 0 < deadline - self.now <= MAX_SUPERVISED_LEASE_SECONDS
        assert not sprint and stream_id.startswith("gimmick-probe-")
        # The broker renews only on a newer sample.
        assert sequence > getattr(self, "last_sequence", -1)
        self.last_sequence = sequence

    def release_all(self):
        self.released += 1

    def sleep(self, seconds):
        self.now += seconds
        if self.heading is not None:
            step = 150.0 * seconds
            x = self.position.x + math.cos(math.radians(self.heading)) * step
            if self.blocked_at is not None:
                x = min(x, self.blocked_at)
            self.position = Vec3(x, self.position.y + math.sin(math.radians(self.heading)) * step, 0.0)

    def observe(self):
        inside = 50.0 <= self.position.x <= 340.0
        self.samples = getattr(self, "samples", 0) + 1
        # Telemetry ticks every other observation: some repeat a sequence.
        return {"sequence": self.samples // 2, "running": True, "telemetry": {"live": True},
                "player": {"position": {"x": self.position.x, "y": self.position.y, "z": 0.0},
                           "locomotion": {"grounded": not inside}}}


def run(character, tmp_path):
    path = tmp_path / "evidence.json"
    gl.record_scan(path, [{"id": CRAWL.gimmick_id, "kind": "crouch",
                           "enter": {"x": 0, "y": 0, "z": 0}, "exit": {"x": 360, "y": 0, "z": 0}}])
    report = gp.probe_gimmick(
        character, Floor(), CRAWL, gl.DIRECTION_FORWARD, path,
        pose=lambda state: (Vec3(**state["player"]["position"]), None, "feet"),
        camera_yaw=lambda state: 0.0, lease_active=lambda: True,
        clock=lambda: character.now, sleep=character.sleep)
    return report, path


def test_the_probe_holds_past_the_pad_and_records_where_the_crossing_lands(tmp_path):
    character = Character()
    report, path = run(character, tmp_path)
    assert report["outcome"] == "landed", report
    assert report["landing_island"] == 2 and report["landing"]["x"] > 1100
    assert report["engaged_at_seconds"] is not None and character.released == 1
    (recorded,) = gl.load_evidence(path)
    assert recorded.ends(gl.DIRECTION_FORWARD)[1].x > 1100
    assert gl.DIRECTION_REVERSE not in recorded.observed_landings


def test_the_probe_stops_neutral_when_nothing_moves(tmp_path):
    character = Character(blocked_at=200.0)
    report, path = run(character, tmp_path)
    assert report["outcome"] == "stopped" and report["reason"] == "probe_no_progress"
    assert character.released == 1
    assert gl.load_evidence(path)[0].observed_landings == {}


class Ladder(Character):
    """Starts on the ladder (inside the passage, not grounded) and climbs down 100 cm/s for 17 m,
    longer than the stall window, before stepping off towards the far island."""

    def __init__(self):
        super().__init__()
        self.position = Vec3(60.0, 0.0, 0.0)

    def sleep(self, seconds):
        self.now += seconds
        if self.heading is None:
            return
        if self.position.z > -1700.0:
            self.position = Vec3(self.position.x, self.position.y, max(-1700.0, self.position.z - 100.0 * seconds))
        else:
            self.position = Vec3(self.position.x + 150.0 * seconds, self.position.y, self.position.z)

    def observe(self):
        state = super().observe()
        state["player"]["position"]["z"] = self.position.z
        return state


def test_a_long_ladder_climb_is_progress_not_a_stall(tmp_path):
    # Live 2026-10-22: the horizontal-only check stopped a 17 m ladder 5 m down.
    report, _ = run(Ladder(), tmp_path)
    assert report["outcome"] == "landed", report
    assert report["landing"]["x"] > 1100


def test_the_nearer_end_is_the_departure():
    assert gp.nearer_direction(CRAWL, Vec3(-100.0, 0.0, 0.0)) == gl.DIRECTION_FORWARD
    assert gp.nearer_direction(CRAWL, Vec3(500.0, 0.0, 0.0)) == gl.DIRECTION_REVERSE


def test_the_probe_stops_neutral_when_a_scene_takes_the_player(tmp_path):
    # Live, Nibelheim water tank: a scene started at the top of the ladder and
    # the feed carried no player position; the probe must stop, not raise.
    character = Character()
    seen = character.observe

    def observe():
        state = seen()
        if character.position.x > 200.0:
            state["player"] = {"position": None}
        return state

    character.observe = observe
    report, _ = run(character, tmp_path)
    assert report["outcome"] == "stopped" and report["reason"] == "field_telemetry_unavailable"
    assert character.released == 1
