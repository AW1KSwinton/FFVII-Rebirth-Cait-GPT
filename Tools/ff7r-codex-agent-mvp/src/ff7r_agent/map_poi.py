"""MapIconInfo identity -> game-observed world point. No movement or input."""
from __future__ import annotations
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
import uuid
from .navigation_values import MissionError, finite_position, horizontal_distance

CATALOG = Path(r"C:\CaitGPT\DataTableIndex\Input\Resident\MapIconInfo.csv")
TOKEN = re.compile(r"[A-Za-z0-9_-]{1,160}\Z")


def catalog_entry(poi_id: str, path: Path = CATALOG) -> dict:
    if not isinstance(poi_id, str) or not TOKEN.fullmatch(poi_id):
        raise MissionError("poi_id_invalid")
    if path.stat().st_size > 8 * 1024 * 1024:
        raise MissionError("poi_catalog_too_large")
    data = path.read_bytes()
    rows = list(csv.DictReader(data.decode("utf-8-sig").splitlines()))
    matches = [(i + 2, row) for i, row in enumerate(rows) if row.get("Name") == poi_id]
    if len(matches) != 1:
        raise MissionError("poi_catalog_missing_or_ambiguous")
    line, row = matches[0]
    object_id = row.get("ObjectID", "")
    if not TOKEN.fullmatch(object_id):
        raise MissionError("poi_placement_id_unavailable")
    offsets = {axis: float(row.get("Offset" + axis.upper()) or 0) for axis in "xyz"}
    if not all(math.isfinite(v) and v == 0 for v in offsets.values()):
        raise MissionError("poi_offset_invalid")
    # A socket places the icon on a part of its object, which needs a
    # different placement resolver. HeightFromOffset only lifts the drawn icon
    # above its object (live 2026-10-20: the Mt. Nibel air purification unit,
    # 30), so the object's own point is still the place to go.
    if row.get("SocketName"):
        raise MissionError("poi_socket_or_height_unsupported")
    try:
        height = float(row.get("HeightFromOffset") or 0)
    except ValueError:
        raise MissionError("poi_offset_invalid") from None
    if not math.isfinite(height):
        raise MissionError("poi_offset_invalid")
    return {"id": poi_id, "object_id": object_id, "offset_cm": offsets, "icon_height_from_offset": height,
            "affiliation": row.get("AffiliationName"), "layer_index": row.get("NaviMapLayerIndex"),
            "catalog_path": str(path), "catalog_sha256": hashlib.sha256(data).hexdigest(),
            "source_row_number": line, "row_index": row.get("Index")}


def merge_poi_snapshot(base: dict, directory: Path) -> None:
    """Two slots preserve the exact source sample while the publisher catches up.

    Neither independent counter closeness nor file mtime authorizes this merge.
    A snapshot from another producer generation, actor, map or request is ignored.
    """
    sequence = base.get("source_sequence")
    if type(sequence) is not int or sequence < 0:
        return
    try:
        path = directory / f"entity-poi.{sequence % 2}.json"
        if path.stat().st_size > 262144:
            return
        payload = json.loads(path.read_text(encoding="utf8"))
        request = (directory / "entity-poi.request").read_text(encoding="ascii").split("|")
        if (len(request) != 5 or int(request[1]) < time.time()
            or payload.get("request_id") != request[0]
            or payload.get("source") != "ue4ss_entity_poi"
            or type(payload.get("source_sequence")) is not int
            or payload["source_sequence"] != sequence
            or payload.get("timestamp_utc") != base.get("source_timestamp_utc")
            or payload.get("player_actor_id") != base.get("player", {}).get("actor_id")
            or payload.get("map_id") != base.get("game", {}).get("map_id")):
            return
        a, b = finite_position(payload.get("player_position")), finite_position(base["player"].get("position"))
        if any(abs(a[k]-b[k]) > 0.1 for k in "xyz"):
            return
        actors = payload.get("nearby_actors")
        if not isinstance(actors, list) or len(actors) > 64:
            return
        # Preserve unrelated map UI telemetry; placement observations are explicit.
        base["map_state"] = {**(base.get("map_state") or {}), "poi_observation": payload}
        existing = base.get("nearby_actors") or []
        incoming_ids = {a.get("id") for a in actors if isinstance(a, dict)}
        base["nearby_actors"] = [a for a in existing if a.get("id") not in incoming_ids] + actors
        base["nearby_entities_complete"] = False  # bounded radius, never disappearance proof
    except (OSError, ValueError, TypeError, KeyError, AttributeError, MissionError):
        return


def resolved_point(sample: dict, entry: dict, request_id: str) -> dict:
    from .entity_interaction import validate_sample
    validate_sample(sample, gameplay=False)
    observed = (sample.get("map_state") or {}).get("poi_observation") or {}
    poi = observed.get("poi") or {}
    if (observed.get("request_id") != request_id or poi.get("id") != entry["id"]
        or poi.get("object_id") != entry["object_id"]
        or poi.get("coordinate_source") != "EndFieldAPI.GetEndTargetPointLocation"):
        raise MissionError("poi_observation_unavailable")
    position = finite_position(poi.get("position"))
    confirmation = finite_position(poi.get("layout_position"))
    if any(abs(position[k]-confirmation[k]) > 1 for k in "xyz"):
        raise MissionError("poi_coordinate_getters_disagree")
    if math.sqrt(sum(v*v for v in position.values())) < 1:
        raise MissionError("poi_default_transform_rejected")
    return {**entry, "position": position, "map_id": sample["map_id"],
            "request_id": request_id, "observed_sequence": sample["sequence"],
            "coordinate_source": poi["coordinate_source"], "marker_offset_cm": entry["offset_cm"]}


def observe_poi(service, poi_id: str, *, seconds: int = 900) -> dict:
    if type(seconds) is not int or not 10 <= seconds <= 1500:
        raise MissionError("poi_request_duration_invalid")
    entry = catalog_entry(poi_id)
    state = service.observe()
    from .entity_interaction import validate_sample
    validate_sample(state, gameplay=False)
    path = getattr(service.telemetry, "path", None)
    if path is None or not TOKEN.fullmatch(state["map_id"]):
        raise MissionError("poi_live_sampler_required")
    destination = Path(path).with_name("entity-poi.request")
    request_id = uuid.uuid4().hex
    # Singleton worker lock protects request creation/release, including two
    # clients asking for the same POI. An existing lease is never borrowed.
    with service._lock:
        try:
            existing = destination.read_text(encoding="ascii").split("|")
            if len(existing) == 5 and int(existing[1]) > time.time():
                raise MissionError("poi_observation_request_busy")
        except (OSError, ValueError):
            pass
        content = "|".join([request_id, str(int(time.time()) + seconds), state["map_id"], entry["object_id"], entry["id"]])
        temporary = destination.with_suffix(".request." + uuid.uuid4().hex)
        try:
            temporary.write_text(content, encoding="ascii")
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
    deadline = time.monotonic() + 4
    previous = None
    try:
        while time.monotonic() < deadline:
            candidate = service.observe()
            try:
                validate_sample(candidate, baseline=state, gameplay=False)
            except MissionError as exc:
                if str(exc) != "entity_telemetry_stale_or_unavailable":
                    raise
                # Read-only warm-up can wait out a delayed producer frame.
                # Start the two-fresh-sample proof again; never admit stale data.
                previous = None
                service._sleeper(0.15)
                continue
            try:
                result = resolved_point(candidate, entry, request_id)
            except MissionError:
                service._sleeper(0.15)
                continue
            if previous is not None and candidate["sequence"] > previous["observed_sequence"]:
                if (horizontal_distance(previous["position"], result["position"]) > 0.1
                    or abs(previous["position"]["z"]-result["position"]["z"]) > 10):
                    raise MissionError("poi_position_unstable")
                return {"ok": True, "poi": result, "sample": candidate}
            previous = result
            service._sleeper(0.15)
        raise MissionError("poi_observation_timeout")
    except Exception:
        release_poi(service, request_id)
        raise


def release_poi(service, request_id: str) -> dict:
    path = Path(service.telemetry.path).with_name("entity-poi.request")
    with service._lock:
        try:
            if path.read_text(encoding="ascii").split("|")[0] == request_id:
                path.unlink()
                return {"ok": True, "released": True}
        except FileNotFoundError:
            pass
    return {"ok": True, "released": False}
