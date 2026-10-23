"""Durable identity and admission helpers for static Detour topology caches.

The native geometry export contains both a static graph and capture-local
proof.  This module fingerprints only the graph-relevant portion.  The live
adapter still binds a cached graph to the current process, session, world,
Cloud resolution, readiness state, lease and actuator before movement.
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any


STATIC_TOPOLOGY_FINGERPRINT_SCHEMA = "ff7r-static-topology-fingerprint-v1"
TOPOLOGY_CACHE_SCHEMA_VERSION = 1
PARTIAL_CORRIDOR_SCOPE_SCHEMA = "ff7r-validated-corridor-scope-v1"
MAX_PARTIAL_SCOPE_SEED_ROUTES = 4
MAX_PARTIAL_SCOPE_POLYGON_REFS = 512
# A capture whose exporter left out a few tiles it could not read is still a
# sound graph of everything else: links into the missing tiles have no target
# and are never routed. Live 2026-09-26 at Kalm one of 3,075 tiles was left
# out (one dynamic link failed the shipping-flag check), which marked the whole
# export partial. Such captures are admitted whole, not corridor-scoped.
TILE_EXCLUDED_PARTIAL_SCHEMA = "ff7r-tile-excluded-partial-v1"
MAX_EXCLUDED_TILE_FRACTION = 0.01
# Every capture is labelled world/map "EndGame" wherever it was taken, and
# covers only the ~386 m square of navmesh the game has built around Cloud.
# The library indexes admitted captures by the area they cover instead.
TOPOLOGY_LIBRARY_NAME = "library.json"
TOPOLOGY_LIBRARY_SCHEMA_VERSION = 1
# Areas stack: the Gold Saucer (z 44,000-65,000 cm) sits over the Corel
# Dustbowl (z ~3,000), and a Saucer capture covering the Dustbowl's x/y was
# chosen for routes on the ground, where Cloud resolves on none of its
# polygons (#359). Coverage therefore also lists the 10 m height bands its
# navmesh occupies, and a point counts as covered only near one of them.
COVERAGE_Z_BAND_CM = 1_000.0
COVERAGE_Z_MARGIN_CM = 500.0
# This is the semantic identity of the graph decoder/coordinate contract.  It
# is intentionally independent of route-planning policy and evidence-only
# helpers in detour_route.py; those changes must not invalidate an otherwise
# proven static graph.
TOPOLOGY_PARSER_SCHEMA_IDENTITY = "ff7r-detour-topology-parser-v1"

# The first admitted static cache predates the semantic identity field and
# recorded the then-deployed parser source digest.  Keep this one explicit
# migration alias rather than accepting arbitrary old source hashes.  The
# provider still verifies the artifact hash, static fingerprint, executable,
# probe, validator, provenance and graph before declaring a cache hit.
LEGACY_COMPATIBLE_TOPOLOGY_PARSER_IDENTITIES = frozenset({
    "96197D8EF1768C68229F8E6FB74E5D67E53F0E5C3D210CB45A6DA2B2E62BAF29",
})


def topology_parser_schema_compatible(actual: str | None, expected: str | None) -> bool:
    """Return whether a manifest parser identity is safe for this decoder."""
    if not actual or not expected:
        return False
    actual_identity = str(actual).upper()
    expected_identity = str(expected).upper()
    if actual_identity == expected_identity:
        return True
    return (
        expected_identity == TOPOLOGY_PARSER_SCHEMA_IDENTITY.upper()
        and actual_identity in LEGACY_COMPATIBLE_TOPOLOGY_PARSER_IDENTITIES
    )


# Native probe builds whose geometry export is the same: a capture made by
# one stays valid under the others. 9771BD24 (2026-09-30) only remembers the
# readable memory regions AFBF559C queried for every field, which made its
# export take eleven minutes; it reads and writes the same fields. 3372208C
# (2026-10-01) only retries a JSON file rename that a reader blocked. 8AE73944
# (2026-10-03) raises tile export bounds without changing geometry fields;
# already admitted, untruncated captures retain the same geometry contract.
# 053B0B6D (2026-10-04) only reports the exporter's own completion in the
# capture status instead of always "partial_or_incomplete".
GEOMETRY_EQUIVALENT_NATIVE_PROBES = (
    frozenset({
        "AFBF559C6E87FDCF0C3FEBCA4421B1CF352CEC796DC3886FD3D14F68ECCED7E3",
        "9771BD24E1B71D6E45FB0701EB7CEB0D085D7E331D5DB37763B20EAB509FC3E4",
        "3372208CD3EC944E104A0A65CEE7DB090DA34EC4E973E658691F94F2EA9365F2",
        "8AE7394433AD36E840488B5B42BA86001EFD51D2BAD8F0A33BDF682B74961B6B",
        "053B0B6D73AD6D7FA458B28CE6085CEDB74C2FA45FCC7D4CDD2CAB068B064FE7",
    }),
)


def native_probe_identities_match(actual: str | None, expected: str | None) -> bool:
    """Return whether a capture by probe ``actual`` is valid under probe ``expected``."""
    if not actual or not expected:
        return False
    actual_identity = str(actual).upper()
    expected_identity = str(expected).upper()
    return actual_identity == expected_identity or any(
        actual_identity in group and expected_identity in group for group in GEOMETRY_EQUIVALENT_NATIVE_PROBES
    )


class TopologyCacheError(ValueError):
    """A cache artifact or manifest cannot be admitted safely."""


_CAPTURE_ONLY_KEYS = frozenset({
    "player_position",
    "capture_timestamp",
    "captured_at",
    "captured_at_utc",
    "timestamp",
    "timestamp_utc",
    "mtime",
    "mtime_ns",
    "session_id",
    "probe_attempt",
    "process_id",
    "pid",
    "movement_sent",
    "status",
    "reason_code",
    "rejection_samples",
    "memory_diagnostic_samples",
    "shipping_poly_block_diagnostic_samples",
    "candidate_a0_link_diagnostic_samples",
    "diagnostic_dynamic_link_rejection_samples",
})
_POINTER_KEY_PARTS = ("pointer", "address", "ptr")


def _canonical_number(value: Any) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TopologyCacheError("topology_numeric_value_invalid")
    number = float(value)
    if not math.isfinite(number):
        raise TopologyCacheError("topology_numeric_value_non_finite")
    if number == 0.0:
        return 0
    if number.is_integer():
        return int(number)
    # Native JSON is written with finite decimal values.  This removes the
    # harmless int/float and -0.0 distinctions without changing geometry.
    return float(format(number, ".9f"))


def _key_is_capture_only(key: str) -> bool:
    lowered = key.lower()
    if lowered in _CAPTURE_ONLY_KEYS:
        return True
    return any(part in lowered for part in _POINTER_KEY_PARTS)


def _canonical_value(value: Any, *, key: str | None = None) -> Any:
    if key is not None and _key_is_capture_only(key):
        return None
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, (int, float)):
        return _canonical_number(value)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key in sorted(value, key=lambda item: str(item)):
            name = str(raw_key)
            child = _canonical_value(value[raw_key], key=name)
            if child is not None:
                result[name] = child
        return result
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    raise TopologyCacheError(f"topology_value_unsupported:{type(value).__name__}")


def _config_value(payload: Mapping[str, Any], proof: Mapping[str, Any], name: str) -> Any:
    value = payload.get(name)
    if value is None:
        value = proof.get(name)
    return value


def _normalise_link(link: Mapping[str, Any]) -> dict[str, Any]:
    # These are the serialized Detour link fields consumed by the graph
    # parser.  Sorting makes the fingerprint insensitive to a native link
    # array's incidental storage order while retaining the next-chain field.
    fields = ("ref", "next", "edge", "side", "bmin", "bmax")
    return {
        name: _canonical_number(link.get(name, 0))
        for name in fields
    }


def _normalise_polygon(polygon: Mapping[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "index": _canonical_number(polygon.get("index", -1)),
        "first_link": _canonical_number(polygon.get("first_link", 0)),
        "vertex_count": _canonical_number(polygon.get("vertex_count", 0)),
        "vertex_indices": [
            _canonical_number(item)
            for item in (polygon.get("vertex_indices") or [])
        ],
        "neighbors": sorted(
            _canonical_number(item)
            for item in (polygon.get("neighbors") or [])
        ),
        "flags": _canonical_number(polygon.get("flags", 0)),
        "area": _canonical_number(polygon.get("area", 0)),
        "type": _canonical_number(polygon.get("type", 0)),
        "links": sorted(
            (_normalise_link(link) for link in (polygon.get("links") or [])
            if isinstance(link, Mapping)),
            key=lambda value: json.dumps(value, sort_keys=True, separators=(",", ":")),
        ),
        # Preserve any explicitly serialized graph fields not covered by the
        # shipping schema, except capture-local diagnostics and pointers.
    }
    extra = {
        str(name): _canonical_value(value, key=str(name))
        for name, value in polygon.items()
        if str(name) not in fields and not _key_is_capture_only(str(name))
    }
    fields.update({name: value for name, value in extra.items() if value is not None})
    return fields


def _normalise_tile(tile: Mapping[str, Any]) -> dict[str, Any]:
    vertices = tile.get("vertices") or []
    polygons = tile.get("polygons") or []
    off_mesh = tile.get("off_mesh_connections") or []
    result: dict[str, Any] = {
        "tile_identity": {
            "tile_index_valid": tile.get("tile_index_valid"),
            "tile_index": _canonical_number(tile.get("tile_index", -1)),
            "salt": _canonical_number(tile.get("salt", 0)),
        },
        "coordinate": {
            "x": _canonical_number(tile.get("x", 0)),
            "y": _canonical_number(tile.get("y", 0)),
            "layer": _canonical_number(tile.get("layer", 0)),
        },
        "lookup_index": _canonical_number(tile.get("lookup_index", -1)),
        "vertices": [_canonical_value(value) for value in vertices],
        "polygons": sorted(
            (_normalise_polygon(value) for value in polygons if isinstance(value, Mapping)),
            key=lambda value: (value["index"], json.dumps(value, sort_keys=True, separators=(",", ":"))),
        ),
        "off_mesh_connections": [
            _canonical_value(value)
            for value in off_mesh
            if isinstance(value, Mapping)
        ],
    }
    return result


def normalized_static_topology(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return the deterministic graph-only representation of an export."""

    if not isinstance(payload, Mapping):
        raise TopologyCacheError("topology_artifact_not_object")
    tiles = payload.get("tiles")
    if not isinstance(tiles, list) or not tiles:
        raise TopologyCacheError("topology_snapshot_has_no_tiles")
    proof = payload.get("topology_proof")
    proof = proof if isinstance(proof, Mapping) else {}
    config_names = (
        "schema_version",
        "geometry_mode",
        "source_poly_stride",
        "shipping_poly_record_stride",
        "shipping_binary_layout_proven",
        "dynamic_links_o_offset",
        "dynamic_free_list_o_offset",
        "shipping_poly_block_candidate_stride",
        "tile_bits",
        "poly_bits",
        "recast_config",
        "detour_config",
    )
    configuration = {
        name: _canonical_value(value)
        for name in config_names
        if (value := _config_value(payload, proof, name)) is not None
    }
    return {
        "fingerprint_schema": STATIC_TOPOLOGY_FINGERPRINT_SCHEMA,
        "source": payload.get("source"),
        "coordinate_space": payload.get("coordinate_space"),
        "coordinate_transform_to_unreal": _canonical_value(
            payload.get("coordinate_transform_to_unreal")
        ),
        "configuration": configuration,
        "tiles": sorted(
            (_normalise_tile(tile) for tile in tiles if isinstance(tile, Mapping)),
            key=lambda value: (
                value["coordinate"]["x"],
                value["coordinate"]["y"],
                value["coordinate"]["layer"],
                value["tile_identity"]["tile_index"],
                value["tile_identity"]["salt"],
                value["lookup_index"],
            ),
        ),
    }


def normalized_static_topology_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        normalized_static_topology(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def static_topology_fingerprint(payload: Mapping[str, Any]) -> str:
    """Hash only stable graph material, never capture-local proof metadata."""

    return hashlib.sha256(normalized_static_topology_bytes(payload)).hexdigest().upper()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise TopologyCacheError(f"file_hash_unavailable:{path}") from exc
    return digest.hexdigest().upper()


def validator_identity(path: str | Path) -> str:
    return sha256_file(path)


def topology_graph_statistics(graph: Any, payload: Mapping[str, Any]) -> dict[str, int]:
    diagnostics = getattr(graph, "snapshot_diagnostics", {})
    return {
        "serialized_tile_count": len(payload.get("tiles") or []),
        "resolved_polygon_count": len(getattr(graph, "polygons", {})),
        "ordinary_edge_count": sum(
            len(value) for value in getattr(graph, "adjacency", {}).values()
        ),
        "serialized_polygon_count": int(diagnostics.get("serialized_polygon_count", 0)),
        "serialized_vertex_count": int(payload.get("total_vertices", 0) or 0),
        "off_mesh_count": sum(
            len(tile.get("off_mesh_connections") or [])
            for tile in (payload.get("tiles") or [])
            if isinstance(tile, Mapping)
        ),
    }


def _canonical_json_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest().upper()


def corridor_portal_identity(portal: Mapping[str, Any]) -> str:
    """Hash every serialized portal field, including clipped geometry."""

    if not isinstance(portal, Mapping):
        raise TopologyCacheError("partial_scope_transition_invalid")
    try:
        return _canonical_json_digest(dict(portal))
    except (TypeError, ValueError) as exc:
        raise TopologyCacheError("partial_scope_transition_unhashable") from exc


def _build_validated_corridor_scope(
    graph: Any,
    seed_routes: Any,
    *,
    raw_artifact_sha256: str,
    static_fingerprint: str,
    native_status: str,
    topology_proof_status: str,
    executable_sha256: str,
    native_probe_sha256: str,
    capture_request_id: str,
    capture_session_id: str,
    process_id: int,
    world_id: str,
    map_id: str,
    validator_source_sha256: str,
) -> dict[str, Any]:
    """Derive a small graph scope from independently validated route plans.

    The stored seed plans and their validator evidence are retained so the
    provider can re-derive this exact scope from the original artifact when it
    loads the cache. No tile-wide admission is inferred from these routes.
    """

    if not isinstance(seed_routes, list) or not 1 <= len(seed_routes) <= MAX_PARTIAL_SCOPE_SEED_ROUTES:
        raise TopologyCacheError("partial_scope_seed_route_count_out_of_bounds")
    if native_status != "partial" or topology_proof_status != "proved":
        raise TopologyCacheError("partial_scope_native_proof_not_positive")
    if not all(isinstance(value, str) and value for value in (
        raw_artifact_sha256,
        static_fingerprint,
        executable_sha256,
        native_probe_sha256,
        capture_request_id,
        capture_session_id,
        world_id,
        map_id,
        validator_source_sha256,
    )):
        raise TopologyCacheError("partial_scope_identity_incomplete")
    if type(process_id) is not int or process_id <= 0:
        raise TopologyCacheError("partial_scope_process_identity_missing")
    for value in (raw_artifact_sha256, static_fingerprint):
        if len(value) != 64 or any(character not in "0123456789abcdefABCDEF" for character in value):
            raise TopologyCacheError("partial_scope_hash_identity_invalid")
    if any(len(value) != 64 for value in (executable_sha256, native_probe_sha256, validator_source_sha256)):
        raise TopologyCacheError("partial_scope_hash_identity_invalid")

    from .detour_route import DT_POLYTYPE_GROUND

    polygon_refs: set[int] = set()
    transition_map: dict[str, dict[str, Any]] = {}
    retained_seeds: list[dict[str, Any]] = []
    for seed in seed_routes:
        if not isinstance(seed, Mapping):
            raise TopologyCacheError("partial_scope_seed_route_not_object")
        plan = seed.get("route_plan")
        validation = seed.get("validation_result")
        binding = seed.get("binding_evidence")
        validated_plan_sha = seed.get("validated_route_plan_sha256") or seed.get("route_plan_sha256")
        if not isinstance(plan, Mapping) or not isinstance(validation, Mapping) or not isinstance(binding, Mapping):
            raise TopologyCacheError("partial_scope_seed_evidence_missing")
        try:
            plan_sha = _canonical_json_digest(plan).lower()
        except (TypeError, ValueError) as exc:
            raise TopologyCacheError("partial_scope_seed_route_unhashable") from exc
        if not isinstance(validated_plan_sha, str) or validated_plan_sha.lower() != plan_sha:
            raise TopologyCacheError("partial_scope_seed_plan_identity_mismatch")
        if (
            validation.get("valid") is not True
            or list(validation.get("errors") or [])
            or list(validation.get("warnings") or [])
        ):
            raise TopologyCacheError("partial_scope_seed_validation_not_positive")
        if (
            str(binding.get("expected_raw_artifact_sha256") or "").upper() != raw_artifact_sha256.upper()
            or str(binding.get("expected_static_topology_fingerprint") or "").upper() != static_fingerprint.upper()
            or str(binding.get("validator_source_sha256") or "").upper() != validator_source_sha256.upper()
            or str(binding.get("executable_sha256") or "").upper() != executable_sha256.upper()
            or str(binding.get("native_probe_sha256") or "").upper() != native_probe_sha256.upper()
            or str(binding.get("capture_request_id") or "") != capture_request_id
            or str(binding.get("capture_session_id") or "") != capture_session_id
            or type(binding.get("process_id")) is not int
            or binding.get("process_id") != process_id
            or str(binding.get("world_id") or "") != world_id
            or str(binding.get("map_id") or "") != map_id
        ):
            raise TopologyCacheError("partial_scope_seed_binding_mismatch")
        plan_executable = plan.get("executable_sha256")
        if (
            not isinstance(plan_executable, str)
            or plan_executable.upper() != executable_sha256.upper()
        ):
            raise TopologyCacheError("partial_scope_seed_executable_identity_mismatch")
        if (
            plan.get("status") != "complete"
            or plan.get("reason_code") != "route_plan_complete"
            or plan.get("movement_sent") is not False
        ):
            raise TopologyCacheError("partial_scope_seed_route_not_complete_and_read_only")
        route_validation = plan.get("validation")
        if not isinstance(route_validation, Mapping) or any(
            route_validation.get(key) is not True
            for key in (
                "corridor_refs_present",
                "transition_portals_valid",
                "portal_links_proven",
                "waypoints_finite",
                "waypoint_sequence_nonempty",
                "waypoint_start_matches_resolved",
                "waypoint_end_matches_resolved",
            )
        ):
            raise TopologyCacheError("partial_scope_seed_route_invariants_missing")
        refs = plan.get("polygon_corridor")
        portals = plan.get("portals")
        if (
            not isinstance(refs, list)
            or not refs
            or len(refs) > MAX_PARTIAL_SCOPE_POLYGON_REFS
            or not isinstance(portals, list)
            or len(portals) != len(refs) + 1
            or any(not isinstance(ref, int) or isinstance(ref, bool) for ref in refs)
        ):
            raise TopologyCacheError("partial_scope_seed_corridor_shape_invalid")
        if (
            validation.get("checked_status") != "complete"
            or validation.get("checked_polygon_refs") != refs
            or validation.get("checked_waypoint_count") != len(plan.get("waypoints") or [])
        ):
            raise TopologyCacheError("partial_scope_seed_validation_binding_mismatch")
        start_ref = plan.get("start_ref")
        end_ref = plan.get("end_ref")
        if start_ref != refs[0] or end_ref != refs[-1]:
            raise TopologyCacheError("partial_scope_seed_endpoint_mismatch")
        for ref in refs:
            polygon = graph.polygons.get(ref)
            if polygon is None or polygon.poly_type != DT_POLYTYPE_GROUND:
                raise TopologyCacheError("partial_scope_seed_reference_not_resolved_ground")
            polygon_refs.add(ref)

        first_portal, final_portal = portals[0], portals[-1]
        if not isinstance(first_portal, Mapping) or not isinstance(final_portal, Mapping):
            raise TopologyCacheError("partial_scope_seed_endpoint_portal_invalid")
        if (
            first_portal.get("from_ref") != refs[0]
            or first_portal.get("to_ref") != refs[0]
            or final_portal.get("from_ref") != refs[-1]
            or final_portal.get("to_ref") != refs[-1]
        ):
            raise TopologyCacheError("partial_scope_seed_endpoint_portal_mismatch")
        seed_transitions: list[dict[str, Any]] = []
        for index, raw_portal in enumerate(portals[1:-1], start=1):
            if not isinstance(raw_portal, Mapping):
                raise TopologyCacheError("partial_scope_seed_transition_invalid")
            source_ref, target_ref = refs[index - 1], refs[index]
            try:
                link_ref = int(raw_portal["link_ref"])
                source_edge = int(raw_portal["source_edge"])
                side = int(raw_portal["side"])
            except (KeyError, TypeError, ValueError) as exc:
                raise TopologyCacheError("partial_scope_seed_transition_identity_missing") from exc
            if (
                raw_portal.get("from_ref") != source_ref
                or raw_portal.get("to_ref") != target_ref
                or link_ref != target_ref
            ):
                raise TopologyCacheError("partial_scope_seed_transition_corridor_mismatch")
            matches = [
                portal
                for portal in graph.adjacency.get(source_ref, ())
                if portal.to_reference == target_ref
                and portal.link_reference == link_ref
                and portal.source_edge == source_edge
                and portal.side == side
                and corridor_portal_identity(portal.to_dict()) == corridor_portal_identity(raw_portal)
            ]
            if len(matches) != 1:
                raise TopologyCacheError("partial_scope_seed_transition_not_proven")
            portal_record = matches[0].to_dict()
            signature = corridor_portal_identity(portal_record)
            previous = transition_map.get(signature)
            if previous is not None and previous != portal_record:
                raise TopologyCacheError("partial_scope_transition_ambiguous")
            transition_map[signature] = portal_record
            seed_transitions.append(portal_record)
        retained_seeds.append({
            "route_plan_sha256": plan_sha,
            "route_plan": dict(plan),
            "validation_result": dict(validation),
            "binding_evidence": dict(binding),
            "polygon_refs": list(dict.fromkeys(refs)),
            "transitions": seed_transitions,
        })
    if len(polygon_refs) > MAX_PARTIAL_SCOPE_POLYGON_REFS:
        raise TopologyCacheError("partial_scope_polygon_count_out_of_bounds")

    transitions = [
        transition_map[key]
        for key in sorted(transition_map)
    ]
    scope: dict[str, Any] = {
        "schema": PARTIAL_CORRIDOR_SCOPE_SCHEMA,
        "raw_artifact_sha256": raw_artifact_sha256.upper(),
        "static_topology_fingerprint": static_fingerprint.upper(),
        "native_status": native_status,
        "topology_proof_status": topology_proof_status,
        "executable_sha256": executable_sha256.upper(),
        "native_probe_sha256": native_probe_sha256.upper(),
        "capture_request_id": capture_request_id,
        "capture_session_id": capture_session_id,
        "process_id": process_id,
        "world_id": world_id,
        "map_id": map_id,
        "validator_source_sha256": validator_source_sha256.upper(),
        "polygon_refs": sorted(polygon_refs),
        "transitions": transitions,
        "seed_routes": retained_seeds,
    }
    scope["scope_identity"] = _canonical_json_digest(scope)
    return scope


def verify_validated_corridor_scope(
    graph: Any,
    scope: Mapping[str, Any],
    *,
    raw_artifact_sha256: str,
    static_fingerprint: str,
    native_status: str,
    topology_proof_status: str,
    executable_sha256: str,
    native_probe_sha256: str,
    capture_request_id: str,
    capture_session_id: str,
    process_id: int,
    world_id: str,
    map_id: str,
    validator_source_sha256: str,
) -> dict[str, Any]:
    """Re-derive a stored corridor scope from its seed evidence and graph."""

    if not isinstance(scope, Mapping) or scope.get("schema") != PARTIAL_CORRIDOR_SCOPE_SCHEMA:
        raise TopologyCacheError("partial_scope_schema_incompatible")
    seeds = scope.get("seed_routes")
    if not isinstance(seeds, list):
        raise TopologyCacheError("partial_scope_seed_route_count_out_of_bounds")
    regenerated = _build_validated_corridor_scope(
        graph,
        seeds,
        raw_artifact_sha256=raw_artifact_sha256,
        static_fingerprint=static_fingerprint,
        native_status=native_status,
        topology_proof_status=topology_proof_status,
        executable_sha256=executable_sha256,
        native_probe_sha256=native_probe_sha256,
        capture_request_id=capture_request_id,
        capture_session_id=capture_session_id,
        process_id=process_id,
        world_id=world_id,
        map_id=map_id,
        validator_source_sha256=validator_source_sha256,
    )
    try:
        stored_digest = _canonical_json_digest(dict(scope))
        regenerated_digest = _canonical_json_digest(regenerated)
    except (TypeError, ValueError) as exc:
        raise TopologyCacheError("partial_scope_unhashable") from exc
    if stored_digest != regenerated_digest:
        raise TopologyCacheError("partial_scope_identity_or_contents_mismatch")
    return regenerated


def build_topology_cache_manifest(
    artifact_path: str | Path,
    *,
    executable_sha256: str,
    native_probe_sha256: str,
    parser_schema_identity: str,
    world_id: str,
    map_id: str,
    coordinate_space: str,
    coordinate_transform_identity: str,
    validator_source_sha256: str,
    validation_result: Mapping[str, Any],
    provenance: Mapping[str, Any],
    created_at_utc: str,
    allow_scoped_partial: bool = False,
    scoped_seed_routes: Any = None,
) -> dict[str, Any]:
    """Build an admission manifest, requiring explicit positive evidence.

    The helper intentionally does not write files.  Callers can persist the
    returned manifest atomically after reviewing it.  A partial native report
    without a proved topology and independently accepted route evidence is
    rejected here; this is what prevents route-235 from becoming a cache.
    """

    path = Path(artifact_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TopologyCacheError("topology_artifact_unreadable") from exc
    if not isinstance(payload, Mapping):
        raise TopologyCacheError("topology_artifact_not_object")
    if payload.get("source") != "ue4ss_native_detour_geometry_dump":
        raise TopologyCacheError("topology_artifact_source_not_proven")
    if payload.get("movement_sent") is not False:
        raise TopologyCacheError("topology_artifact_not_read_only")
    proof = payload.get("topology_proof")
    proof_status = proof.get("status") if isinstance(proof, Mapping) else None
    if type(allow_scoped_partial) is not bool:
        raise TopologyCacheError("scoped_partial_opt_in_must_be_boolean")
    partial_scope_requested = allow_scoped_partial or scoped_seed_routes is not None
    excluded_tiles = None if partial_scope_requested else tile_excluded_partial(payload)
    if payload.get("status") != "complete" and excluded_tiles is None:
        if not allow_scoped_partial:
            raise TopologyCacheError("topology_artifact_not_complete_for_cache_admission")
        if payload.get("status") != "partial":
            raise TopologyCacheError("scoped_admission_requires_partial_artifact")
        if proof_status != "proved":
            raise TopologyCacheError("partial_scope_native_proof_not_positive")
        capture_request_id = str(provenance.get("capture_request_id") or "")
        capture_session_id = str(provenance.get("capture_session_id") or "")
        if (
            payload.get("request_id") != capture_request_id
            or not capture_request_id
            or payload.get("capture_session_id") != capture_session_id
            or not capture_session_id
            or payload.get("capture_world_id") != world_id
            or payload.get("capture_map_id") != map_id
            or str(payload.get("native_probe_sha256") or "").upper() != native_probe_sha256.upper()
            or payload.get("capture_purpose") != "static_topology_cache_seed"
            or str(provenance.get("executable_sha256") or "").upper() != executable_sha256.upper()
            or str(provenance.get("native_probe_sha256") or "").upper() != native_probe_sha256.upper()
            or type(provenance.get("process_id")) is not int
            or provenance.get("process_id") <= 0
        ):
            raise TopologyCacheError("partial_scope_capture_binding_mismatch")
    elif partial_scope_requested:
        raise TopologyCacheError("scoped_admission_requires_partial_artifact")
    if validation_result.get("valid") is not True:
        raise TopologyCacheError("cache_validation_not_positive")
    if list(validation_result.get("errors") or []) or list(validation_result.get("warnings") or []):
        raise TopologyCacheError("cache_validation_has_errors_or_warnings")
    if provenance.get("independent_route_validation") is not True:
        raise TopologyCacheError("independent_route_validation_evidence_missing")
    if not isinstance(provenance.get("capture_request_id"), str) or not provenance.get("capture_request_id"):
        raise TopologyCacheError("capture_request_id_provenance_missing")
    if not all(isinstance(value, str) and value for value in (
        executable_sha256,
        native_probe_sha256,
        parser_schema_identity,
        world_id,
        map_id,
        coordinate_space,
        coordinate_transform_identity,
        validator_source_sha256,
        created_at_utc,
    )):
        raise TopologyCacheError("cache_manifest_identity_incomplete")
    # Import lazily to keep the fingerprint helper usable by tooling that does
    # not import the planner package at startup.
    from .detour_route import DetourTopologyGraph

    try:
        graph = DetourTopologyGraph.from_dict(payload)
    except (OSError, ValueError) as exc:
        raise TopologyCacheError("topology_graph_load_failed") from exc
    diagnostics = getattr(graph, "snapshot_diagnostics", {})
    if not getattr(graph, "polygons", None):
        raise TopologyCacheError("topology_graph_empty")
    fingerprint = static_topology_fingerprint(payload)
    raw_sha256 = sha256_file(path)
    corridor_scope = None
    if payload.get("status") == "partial" and excluded_tiles is None:
        corridor_scope = _build_validated_corridor_scope(
            graph,
            scoped_seed_routes,
            raw_artifact_sha256=raw_sha256,
            static_fingerprint=fingerprint,
            native_status=str(payload.get("status") or ""),
            topology_proof_status=str(proof_status or ""),
            executable_sha256=executable_sha256,
            native_probe_sha256=native_probe_sha256,
            capture_request_id=str(provenance.get("capture_request_id") or ""),
            capture_session_id=str(provenance.get("capture_session_id") or ""),
            process_id=provenance.get("process_id"),
            world_id=world_id,
            map_id=map_id,
            validator_source_sha256=validator_source_sha256,
        )
    manifest = {
        "cache_schema_version": TOPOLOGY_CACHE_SCHEMA_VERSION,
        "fingerprint_schema": STATIC_TOPOLOGY_FINGERPRINT_SCHEMA,
        "static_topology_fingerprint": fingerprint,
        "raw_artifact_sha256": raw_sha256,
        "executable_sha256": executable_sha256.upper(),
        "native_probe_sha256": native_probe_sha256.upper(),
        "parser_schema_identity": parser_schema_identity,
        "world_id": world_id,
        "map_id": map_id,
        "coordinate_space": coordinate_space,
        "coordinate_transform_identity": coordinate_transform_identity,
        "graph_statistics": topology_graph_statistics(graph, payload),
        "created_at_utc": created_at_utc,
        "source_artifact_path": path.name,
        "validator_source_sha256": validator_source_sha256,
        "capture_request_id": provenance["capture_request_id"],
        "validation_result": dict(validation_result),
        "provenance": dict(provenance),
        "capture_provenance": {
            "native_status": payload.get("status"),
            "native_reason_code": payload.get("reason_code"),
            "topology_proof_status": proof_status,
            "capture_session_id": payload.get("capture_session_id"),
            "capture_world_id": payload.get("capture_world_id"),
            "capture_map_id": payload.get("capture_map_id"),
            "native_probe_sha256": payload.get("native_probe_sha256"),
            "capture_purpose": payload.get("capture_purpose"),
            "executable_sha256": provenance.get("executable_sha256"),
            "process_id": provenance.get("process_id"),
            "capture_request_id": provenance.get("capture_request_id"),
            "snapshot_diagnostics": dict(diagnostics),
        },
    }
    manifest["coverage"] = topology_coverage(payload)
    if excluded_tiles is not None:
        manifest["admission_mode"] = TILE_EXCLUDED_PARTIAL_SCHEMA
        manifest["excluded_tiles"] = excluded_tiles
    if corridor_scope is not None:
        manifest["admission_mode"] = PARTIAL_CORRIDOR_SCOPE_SCHEMA
        manifest["corridor_scope"] = corridor_scope
        manifest["corridor_scope_identity"] = corridor_scope["scope_identity"]
    return manifest


TOPOLOGY_PROOF_LOCALITY_MISS = "no_nearby_two_hop_cross_tile_link"


def topology_proof_locality_miss(payload: Mapping[str, Any]) -> bool:
    """A topology proof that only missed Cloud's locality.

    The proof walks from the polygon under Cloud to a cross-tile link within
    two hops; standing mid-tile it finds none (live at Costa del Sol, and at
    by Nene in Kalm 2026-09-30) though the capture read tens of
    thousands of cross-tile links without a failure. Accepted only on the
    proven binary layout with no link read failures, cross-tile links
    present, and that exact reason; every route is still checked by the
    independent validator.
    """
    proof = payload.get("topology_proof") if isinstance(payload, Mapping) else None
    if not isinstance(proof, Mapping):
        return False
    try:
        cross_tile = int(proof.get("cross_tile_links") or 0)
        failures = int(proof.get("link_read_failures") or 0)
    except (TypeError, ValueError):
        return False
    return (payload.get("shipping_binary_layout_proven") is True
            and proof.get("reason_code") == TOPOLOGY_PROOF_LOCALITY_MISS
            and failures == 0 and cross_tile > 0)


def tile_excluded_partial(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    """Describe a partial export whose only defect is a few left-out tiles.

    Returns None unless the lookup chain was read in full with no read
    failures, the topology proof passed (or only missed Cloud's locality),
    and at most 1% of the valid tiles (at least one) are missing from the
    export.
    """
    if payload.get("status") != "partial" or payload.get("reason_code") != "geometry_stage_rejections":
        return None
    proof = payload.get("topology_proof")
    if not isinstance(proof, Mapping) or (
        proof.get("status") != "proved" and not topology_proof_locality_miss(payload)
    ):
        return None
    if payload.get("truncated") is not False or payload.get("lookup_chain_complete") is not True:
        return None
    for name in ("polygon_read_failures", "link_read_failures", "rejected_pointers", "cyclic_pointers"):
        if payload.get(name) != 0:
            return None
    valid = payload.get("valid_unique_tiles")
    exported = payload.get("exported_unique_tiles")
    tiles = payload.get("tiles")
    incomplete = payload.get("tiles_with_incomplete_link_data", 0)
    if type(valid) is not int or type(exported) is not int or type(incomplete) is not int or not isinstance(tiles, list):
        return None
    excluded = valid - exported
    if len(tiles) != exported or excluded < 1 or incomplete > excluded:
        return None
    if excluded > max(1, math.floor(valid * MAX_EXCLUDED_TILE_FRACTION)):
        return None
    return {
        "schema": TILE_EXCLUDED_PARTIAL_SCHEMA,
        "valid_tiles": valid,
        "exported_tiles": exported,
        "excluded_tiles": excluded,
        "tiles_with_incomplete_link_data": incomplete,
    }


def topology_coverage(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The area a capture covers: Unreal-space bounds (cm), tile range and
    the height bands (``COVERAGE_Z_BAND_CM``) its navmesh occupies."""
    recast = payload.get("coordinate_space", "recast") == "recast"
    xs: list[float] = []
    ys: list[float] = []
    bands: set[int] = set()
    tile_x: list[int] = []
    tile_y: list[int] = []
    for tile in payload.get("tiles") or []:
        if not isinstance(tile, Mapping):
            continue
        if type(tile.get("x")) is int and type(tile.get("y")) is int:
            tile_x.append(tile["x"])
            tile_y.append(tile["y"])
        heights: list[float | None] = []
        for vertex in tile.get("vertices") or []:
            heights.append(None)
            try:
                x, y, z = ((-float(vertex["x"]), -float(vertex["z"]), float(vertex["y"])) if recast
                           else (float(vertex["x"]), float(vertex["y"]), float(vertex["z"])))
            except (KeyError, TypeError, ValueError):
                continue
            if math.isfinite(x) and math.isfinite(y):
                xs.append(x)
                ys.append(y)
                if math.isfinite(z):
                    heights[-1] = z
                    bands.add(math.floor(z / COVERAGE_Z_BAND_CM))
        # A slope's polygon covers every height between its corners.
        for polygon in tile.get("polygons") or []:
            corners = [heights[i] for i in (polygon.get("vertex_indices") or [])
                       if type(i) is int and 0 <= i < len(heights) and heights[i] is not None] \
                if isinstance(polygon, Mapping) else []
            if corners:
                bands.update(range(math.floor(min(corners) / COVERAGE_Z_BAND_CM),
                                   math.floor(max(corners) / COVERAGE_Z_BAND_CM) + 1))
    if not xs or not tile_x:
        raise TopologyCacheError("topology_coverage_empty")
    return {
        "frame": "unreal_cm",
        "x": [min(xs), max(xs)],
        "y": [min(ys), max(ys)],
        "z_bands": sorted(bands),
        "tile_x": [min(tile_x), max(tile_x)],
        "tile_y": [min(tile_y), max(tile_y)],
        "tiles": len(tile_x),
    }


def coverage_contains(coverage: Any, x: float, y: float, z: float | None = None) -> bool:
    """Whether ``coverage`` includes the point; ``z`` is checked against its
    height bands when both are known (older coverage has none)."""
    try:
        inside = coverage["x"][0] <= x <= coverage["x"][1] and coverage["y"][0] <= y <= coverage["y"][1]
    except (KeyError, IndexError, TypeError):
        return False
    bands = coverage.get("z_bands") if isinstance(coverage, Mapping) else None
    if not inside or z is None or not isinstance(bands, list) or not math.isfinite(z):
        return inside
    low = math.floor((z - COVERAGE_Z_MARGIN_CM) / COVERAGE_Z_BAND_CM)
    high = math.floor((z + COVERAGE_Z_MARGIN_CM) / COVERAGE_Z_BAND_CM)
    return any(low <= band <= high for band in bands)


def load_topology_library(directory: str | Path) -> dict[str, Any] | None:
    """The library index, or None when this cache directory has none yet."""
    path = Path(directory) / TOPOLOGY_LIBRARY_NAME
    try:
        library = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise TopologyCacheError("topology_library_unreadable") from exc
    if (
        not isinstance(library, Mapping)
        or library.get("schema_version") != TOPOLOGY_LIBRARY_SCHEMA_VERSION
        or not isinstance(library.get("entries"), list)
    ):
        raise TopologyCacheError("topology_library_invalid")
    return dict(library)


def library_entry(manifest_name: str, manifest: Mapping[str, Any], coverage: Mapping[str, Any]) -> dict[str, Any]:
    capture = manifest.get("capture_provenance")
    return {
        "manifest": manifest_name,
        "static_topology_fingerprint": manifest.get("static_topology_fingerprint"),
        "world_id": manifest.get("world_id"),
        "map_id": manifest.get("map_id"),
        "executable_sha256": manifest.get("executable_sha256"),
        "native_probe_sha256": manifest.get("native_probe_sha256"),
        "topology_proof_status": capture.get("topology_proof_status") if isinstance(capture, Mapping) else None,
        "admission_mode": manifest.get("admission_mode"),
        "created_at_utc": manifest.get("created_at_utc"),
        "coverage": dict(coverage),
    }


def _write_json_atomic(path: Path, value: Any) -> None:
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    except OSError as exc:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise TopologyCacheError("topology_library_write_failed") from exc


def _manifest_coverage(directory: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    coverage = manifest.get("coverage")
    if isinstance(coverage, Mapping):
        return dict(coverage)
    artifact = resolve_manifest_artifact(directory / "manifest.json", manifest)
    try:
        payload = json.loads(artifact.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TopologyCacheError("topology_library_artifact_unreadable") from exc
    return topology_coverage(payload)


def ensure_topology_library(directory: str | Path) -> dict[str, Any]:
    """The library index, created on first use from the single-slot cache.

    Before the library, each admission replaced ``manifest.json``; indexing
    that slot first keeps the earlier admission reachable. Call this before
    the slot is overwritten.
    """
    directory = Path(directory)
    library = load_topology_library(directory)
    if library is not None:
        return library
    library = {"schema_version": TOPOLOGY_LIBRARY_SCHEMA_VERSION, "entries": []}
    try:
        legacy = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        legacy = None
    if isinstance(legacy, Mapping):
        fingerprint = str(legacy.get("static_topology_fingerprint") or "").upper()
        try:
            legacy_coverage = _manifest_coverage(directory, legacy)
        except TopologyCacheError:
            legacy_coverage = None
        if len(fingerprint) == 64 and legacy_coverage is not None:
            legacy_name = f"manifest-{fingerprint}.json"
            _write_json_atomic(directory / legacy_name, dict(legacy))
            library["entries"].append(library_entry(legacy_name, legacy, legacy_coverage))
    directory.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(directory / TOPOLOGY_LIBRARY_NAME, library)
    return library


def register_library_manifest(directory: str | Path, manifest_name: str, manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Add or replace one admitted manifest in the library index."""
    directory = Path(directory)
    library = ensure_topology_library(directory)
    entry = library_entry(manifest_name, manifest, _manifest_coverage(directory, manifest))
    library["entries"] = [
        existing for existing in library["entries"]
        if existing.get("static_topology_fingerprint") != entry["static_topology_fingerprint"]
    ] + [entry]
    _write_json_atomic(directory / TOPOLOGY_LIBRARY_NAME, library)
    return entry


def _manifest_validator_matches(directory: Path, entry: Mapping[str, Any], validator_source_sha256: str) -> bool:
    """Whether ``entry``'s manifest was admitted under this validator; an unreadable one was not."""
    try:
        manifest = json.loads((directory / str(entry["manifest"])).read_text(encoding="utf-8"))
    except (KeyError, OSError, json.JSONDecodeError):
        return False
    return (isinstance(manifest, Mapping) and str(manifest.get("validator_source_sha256") or "").upper()
            == str(validator_source_sha256).upper())


def _with_z_bands(directory: Path, entry: Mapping[str, Any]) -> Mapping[str, Any]:
    """``entry`` with its coverage's height bands, read once from the
    artifact for entries indexed before coverage had them and saved back to
    the library. An unreadable artifact leaves the entry as it was."""
    coverage = entry.get("coverage")
    if not isinstance(coverage, Mapping) or isinstance(coverage.get("z_bands"), list):
        return entry
    try:
        manifest = json.loads((directory / str(entry["manifest"])).read_text(encoding="utf-8"))
        artifact = resolve_manifest_artifact(directory / "manifest.json", manifest)
        bands = topology_coverage(json.loads(artifact.read_text(encoding="utf-8")))["z_bands"]
    except (KeyError, OSError, json.JSONDecodeError, TopologyCacheError):
        return entry
    fingerprint = entry.get("static_topology_fingerprint")
    try:
        library = load_topology_library(directory)
        if library is not None:
            for existing in library["entries"]:
                if (isinstance(existing, dict) and existing.get("static_topology_fingerprint") == fingerprint
                        and isinstance(existing.get("coverage"), dict)):
                    existing["coverage"]["z_bands"] = bands
            _write_json_atomic(directory / TOPOLOGY_LIBRARY_NAME, library)
    except TopologyCacheError:
        pass  # Selection still uses the bands; the next one reads them again.
    return {**entry, "coverage": {**coverage, "z_bands": bands}}


def select_library_manifest(
    directory: str | Path,
    *,
    world_id: str,
    map_id: str,
    executable_sha256: str,
    position: tuple[float, ...],
    target: tuple[float, ...] | None = None,
    native_probe_sha256: str | None = None,
    validator_source_sha256: str | None = None,
) -> tuple[Path | None, dict[str, Any]]:
    """Choose the admitted capture that covers Cloud (and the target).

    ``position`` and ``target`` are (x, y) or (x, y, z); with z a capture
    covers a point only near a height its navmesh occupies. Returns
    ``(None, decision)`` when the directory has no library, so the caller
    keeps its configured manifest. Among covering entries one that also
    covers the target wins, then a proved one, then the newest.

    With ``validator_source_sha256`` a capture admitted under another
    validator is skipped: the route adapter refuses it, so choosing it
    would hide a capture the current validator admitted (live: after #370
    every Kalm route took the proved 2026-10-03 capture and stopped with
    ``cache_validator_identity_mismatch``, even beside a fresh capture).
    """
    directory = Path(directory)
    library = load_topology_library(directory)
    if library is None:
        return None, {"reason": "topology_library_absent"}
    candidates = []
    validator_skipped: list[Any] = []
    for entry in library["entries"]:
        if not isinstance(entry, Mapping):
            continue
        if (
            entry.get("world_id") != world_id
            or entry.get("map_id") != map_id
            or str(entry.get("executable_sha256") or "").upper() != str(executable_sha256).upper()
            or (native_probe_sha256 is not None and not native_probe_identities_match(
                entry.get("native_probe_sha256"), native_probe_sha256))
            or not coverage_contains(entry.get("coverage"), *position[:2])
        ):
            continue
        if len(position) > 2 or (target is not None and len(target) > 2):
            entry = _with_z_bands(directory, entry)
        if not coverage_contains(entry.get("coverage"), *position):
            continue
        if validator_source_sha256 is not None and not _manifest_validator_matches(
                directory, entry, validator_source_sha256):
            validator_skipped.append(entry.get("static_topology_fingerprint"))
            continue
        covers_target = target is not None and coverage_contains(entry.get("coverage"), *target)
        rank = (covers_target, entry.get("topology_proof_status") == "proved", str(entry.get("created_at_utc") or ""))
        candidates.append((rank, entry))
    decision: dict[str, Any] = {
        "entries": len(library["entries"]),
        "covering_current": [entry.get("static_topology_fingerprint") for _, entry in candidates],
    }
    if validator_skipped:
        decision["skipped_validator_mismatch"] = validator_skipped
    if not candidates:
        decision["reason"] = "topology_library_no_capture_covers_current_position"
        return None, decision
    rank, entry = max(candidates, key=lambda item: item[0])
    decision.update({
        "reason": "topology_library_capture_selected",
        "selected": entry.get("static_topology_fingerprint"),
        "covers_target": rank[0] if target is not None else None,
    })
    return directory / str(entry["manifest"]), decision


def resolve_manifest_artifact(manifest_path: str | Path, manifest: Mapping[str, Any]) -> Path:
    value = manifest.get("source_artifact_path")
    if not isinstance(value, str) or not value:
        raise TopologyCacheError("cache_manifest_artifact_path_missing")
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = Path(manifest_path).resolve().parent / candidate
    return candidate


def admit_topology_cache(
    artifact_path: str | Path,
    manifest_path: str | Path,
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """Atomically publish a validated artifact and its manifest.

    The artifact is copied first under its static fingerprint.  The manifest
    is replaced last, so an interrupted admission cannot make a newer route
    see a half-written artifact or a manifest pointing at a missing file.
    """

    source = Path(artifact_path)
    destination_manifest = Path(manifest_path)
    if not source.is_file():
        raise TopologyCacheError("cache_source_artifact_missing")
    fingerprint = str(manifest.get("static_topology_fingerprint") or "").upper()
    raw_sha = str(manifest.get("raw_artifact_sha256") or "").upper()
    if len(fingerprint) != 64 or len(raw_sha) != 64:
        raise TopologyCacheError("cache_manifest_hashes_incomplete")
    if sha256_file(source) != raw_sha:
        raise TopologyCacheError("cache_source_artifact_hash_mismatch")
    if manifest.get("validation_result", {}).get("valid") is not True:
        raise TopologyCacheError("cache_validation_not_positive")
    destination_manifest.parent.mkdir(parents=True, exist_ok=True)
    if "coverage" in manifest:
        ensure_topology_library(destination_manifest.parent)
    artifact_name = f"topology-{fingerprint}.json"
    destination_artifact = destination_manifest.parent / artifact_name
    artifact_tmp = destination_artifact.with_name(destination_artifact.name + ".tmp")
    manifest_tmp = destination_manifest.with_name(destination_manifest.name + ".tmp")
    try:
        artifact_tmp.write_bytes(source.read_bytes())
        os.replace(artifact_tmp, destination_artifact)
        admitted = dict(manifest)
        admitted["source_artifact_path"] = artifact_name
        admitted["admitted_atomically"] = True
        admitted["admitted_artifact_sha256"] = sha256_file(destination_artifact)
        manifest_tmp.write_text(
            json.dumps(admitted, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(manifest_tmp, destination_manifest)
        # The single slot above stays the latest admission for older callers;
        # the library keeps every admission reachable by the area it covers.
        library_manifest = destination_manifest.parent / f"manifest-{fingerprint}.json"
        library_tmp = library_manifest.with_name(library_manifest.name + ".tmp")
        library_tmp.write_text(
            json.dumps(admitted, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(library_tmp, library_manifest)
    except OSError as exc:
        for temporary in (artifact_tmp, manifest_tmp):
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        raise TopologyCacheError("cache_atomic_admission_failed") from exc
    cached_static_fingerprint(destination_manifest.parent, raw_sha, lambda: fingerprint)
    if "coverage" in admitted:
        register_library_manifest(destination_manifest.parent, f"manifest-{fingerprint}.json", admitted)
    return admitted


FINGERPRINT_CACHE_NAME = "fingerprints.json"


def cached_static_fingerprint(directory: str | Path, raw_sha256: str, compute: Any) -> str:
    """The static fingerprint of an artifact, memoized by its raw SHA-256.

    Recomputing it took 15.8 s of the 26 s cache load at Kalm; it is a pure
    function of the artifact bytes, which the caller has already hashed. The
    memo lives beside the cache and is keyed by the fingerprint schema, so a
    normalization change recomputes it.
    """
    path = Path(directory) / FINGERPRINT_CACHE_NAME
    key = f"{STATIC_TOPOLOGY_FINGERPRINT_SCHEMA}:{str(raw_sha256).upper()}"
    try:
        memo = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        memo = {}
    if not isinstance(memo, dict):
        memo = {}
    value = memo.get(key)
    if isinstance(value, str) and len(value) == 64:
        return value.upper()
    fingerprint = str(compute()).upper()
    memo[key] = fingerprint
    try:
        _write_json_atomic(path, memo)
    except TopologyCacheError:
        pass  # an unwritable memo only costs the recomputation next time
    return fingerprint
