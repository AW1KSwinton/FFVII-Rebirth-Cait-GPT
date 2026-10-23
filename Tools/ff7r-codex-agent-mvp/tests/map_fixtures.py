"""Synthetic map-query.json payloads shaped like map_query.lua's output."""
import math

PLAYER = {"x": 1000.0, "y": 2000.0, "z": 150.0}
OFFSETS = [(0, 0), (10000, 0), (0, 10000), (-10000, 0), (0, -10000), (100000, 0), (0, 100000)]


class Projection:
    """World cm -> NaviMap local px: scale, rotation, and a view offset."""

    def __init__(self, scale=0.02, degrees=0.0, origin=(400.0, 300.0), convert_offset=(0.0, 0.0)):
        self.scale, self.theta = scale, math.radians(degrees)
        self.origin, self.convert_offset = origin, convert_offset

    def local(self, x, y):
        dx, dy = x - PLAYER["x"], y - PLAYER["y"]
        c, s = math.cos(self.theta), math.sin(self.theta)
        return (self.origin[0] + self.scale * (c * dx - s * dy),
                self.origin[1] + self.scale * (s * dx + c * dy))

    def convert(self, x, y):
        lx, ly = self.local(x, y)
        return lx - self.convert_offset[0], ly - self.convert_offset[1]


def widget(runtime_id, local, *, visible=True, images=(), distance_text="", properties=None, size=16):
    return {
        "runtime_id": runtime_id, "class": "Class Icon_NaviMap_C", "visible": visible,
        "placement": {"navi_local": {"x": local[0], "y": local[1]}, "size": {"x": size, "y": size}},
        "images": list(images), "texts": [], "properties": properties or {}, "distance_text": distance_text,
    }


def icon(projection, runtime_id, world_xy, **kwargs):
    return widget(runtime_id, projection.local(*world_xy), **kwargs)


def response(projection=None, *, icons=(), cursor=None, indicator=True, heights=None, request_id="req",
             map_id="EndGame", status="ready", prompt=False, player=None, viewport=True):
    projection = projection or Projection()
    player = player or PLAYER
    calibration = []
    for dx, dy in OFFSETS:
        world = {"x": player["x"] + dx, "y": player["y"] + dy, "z": player["z"]}
        mx, my = projection.convert(world["x"], world["y"])
        calibration.append({"world": world, "map": {"x": mx, "y": my}})
    navi = {"local_size": {"x": 800, "y": 600}, "pixel_per_cm": projection.scale}
    if viewport:
        navi["viewport_corners"] = [
            {"local_point": {"x": x, "y": y}, "pixel": {"x": 160 + 1.5 * x, "y": 90 + 1.5 * y}}
            for x, y in ((0, 0), (800, 0), (0, 600), (800, 600))]
    payload = {
        "schema_version": 1, "source": "ue4ss_map_query", "request_id": request_id,
        "source_sequence": 40, "timestamp_utc": "2026-09-25T12:00:00Z", "map_id": map_id,
        "paused": 1, "status": status, "player_position": dict(player),
        "navi_map": navi, "calibration": calibration, "icons": list(icons),
        "icons_complete": True, "fast_travel_prompt": {"visible": prompt},
        "cursor": widget("Map_Cursor_C", cursor if cursor is not None else projection.local(player["x"], player["y"])),
    }
    if indicator:
        payload["player_indicator"] = widget("Map_PlayerLoca_C", projection.local(player["x"], player["y"]))
    if heights is not None:
        payload["height_data"] = heights
    return payload


def base_sample(map_id="EndGame", player=None, actors=()):
    return {
        "sequence": 41, "map_id": map_id, "running": True, "paused": True, "in_menu": True,
        "telemetry": {"live": True, "age_seconds": 0.05},
        "player": {"actor_id": "PC", "position": dict(player or PLAYER)},
        "nearby_actors": list(actors), "nearby_interactables": [],
    }
