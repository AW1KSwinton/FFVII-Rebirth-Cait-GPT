from __future__ import annotations

from copy import deepcopy
from dataclasses import replace as dataclass_replace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ff7r_agent.live_route_adapter import (
    AdapterError,
    IndependentJsonValidator,
    LiveObservation,
    LiveAdapterConfig,
    LiveRouteAdapter,
    RenderedGameplayEvidence,
    ValidatedTopologyCacheProvider,
)
from ff7r_agent.detour_route import DetourTopologyGraph, PolygonReference
from ff7r_agent.models import Vec3
from ff7r_agent.topology_cache import (
    LEGACY_COMPATIBLE_TOPOLOGY_PARSER_IDENTITIES,
    TopologyCacheError,
    build_topology_cache_manifest,
    native_probe_identities_match,
    sha256_file,
    static_topology_fingerprint,
)


EXECUTABLE = "E" * 64
PROBE = "P" * 64
PARSER = "R" * 64
VALIDATOR = "V" * 64
WORLD = "EndGame"
TRANSFORM = "x=-recast.x,y=-recast.z,z=recast.y"


def topology_payload() -> dict:
    return {
        "schema_version": 2,
        "source": "ue4ss_native_detour_geometry_dump",
        "geometry_mode": "shipping_extended_poly_record_diagnostic",
        "source_poly_stride": 64,
        "shipping_poly_record_stride": 64,
        "shipping_binary_layout_proven": True,
        "coordinate_space": "recast",
        "coordinate_transform_to_unreal": TRANSFORM,
        "movement_sent": False,
        "tile_bits": 14,
        "poly_bits": 31,
        "total_vertices": 4,
        "tiles": [
            {
                "tile_index_valid": True,
                "tile_index": 1,
                "salt": 1,
                "lookup_index": 0,
                "x": 0,
                "y": 0,
                "layer": 0,
                "vertices": [
                    {"x": 1000.0, "y": 0.0, "z": 1000.0},
                    {"x": -1000.0, "y": 0.0, "z": 1000.0},
                    {"x": -1000.0, "y": 0.0, "z": -1000.0},
                    {"x": 1000.0, "y": 0.0, "z": -1000.0},
                ],
                "polygons": [
                    {
                        "index": 0,
                        "first_link": 0,
                        "vertex_count": 4,
                        "vertex_indices": [0, 1, 2, 3],
                        "neighbors": [],
                        "flags": 1,
                        "area": 0,
                        "type": 0,
                        "links": [],
                    }
                ],
                "off_mesh_connections": [],
            }
        ],
        "topology_proof": {"status": "proved", "reason_code": "cross_tile_topology_proved"},
        "status": "complete",
        "reason_code": "geometry_export_complete",
    }


def observation(*, session_id="session-1", position=None, executable=EXECUTABLE, process_id=123, sequence=10) -> LiveObservation:
    return LiveObservation(
        monotonic_t=10.0,
        timestamp_utc="2026-08-21T00:00:00Z",
        sequence=sequence,
        position=position or Vec3(0.0, 0.0, 0.0),
        actor_yaw=0.0,
        camera_yaw=0.0,
        actor_id="cloud-1",
        session_id=session_id,
        world_id=WORLD,
        map_id=WORLD,
        process_valid=True,
        executable_valid=True,
        controllable=True,
        loading=False,
        in_menu=False,
        in_cutscene=False,
        in_combat=False,
        in_interaction=False,
        lease_active=True,
        actuator_neutral=True,
        telemetry_age_seconds=0.0,
        process_id=process_id,
        executable_sha256=executable,
        telemetry_gameplay_ready=True,
    )


def write_cache(root: Path, payload=None, **manifest_changes):
    artifact = root / "geometry.json"
    manifest_path = root / "manifest.json"
    artifact.write_text(json.dumps(payload or topology_payload(), sort_keys=True), encoding="utf-8")
    manifest = build_topology_cache_manifest(
        artifact,
        executable_sha256=EXECUTABLE,
        native_probe_sha256=PROBE,
        parser_schema_identity=PARSER,
        world_id=WORLD,
        map_id=WORLD,
        coordinate_space="recast",
        coordinate_transform_identity=TRANSFORM,
        validator_source_sha256=VALIDATOR,
        validation_result={"valid": True, "errors": [], "warnings": []},
        provenance={
            "capture_request_id": "capture-fixture-0001",
            "independent_route_validation": True,
            "route_plan_sha256": "A" * 64,
            "evidence": "accepted-independent-route-replay",
        },
        created_at_utc="2026-08-21T00:00:00Z",
    )
    manifest.update(manifest_changes)
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    return artifact, manifest_path, manifest


def provider(manifest_path: Path) -> ValidatedTopologyCacheProvider:
    return ValidatedTopologyCacheProvider(
        manifest_path,
        expected_executable_sha256=EXECUTABLE,
        expected_native_probe_sha256=PROBE,
        expected_parser_schema_identity=PARSER,
        expected_validator_source_sha256=VALIDATOR,
        native_probe_path=None,
    )


def default_parser_provider(manifest_path: Path) -> ValidatedTopologyCacheProvider:
    return ValidatedTopologyCacheProvider(
        manifest_path,
        expected_executable_sha256=EXECUTABLE,
        expected_native_probe_sha256=PROBE,
        expected_validator_source_sha256=VALIDATOR,
        native_probe_path=None,
    )


def partial_connected_payload(*, include_unvalidated: bool = False) -> dict:
    payload = topology_payload()
    payload["status"] = "partial"
    payload["reason_code"] = "geometry_stage_rejections"
    payload.update({
        "request_id": "partial-capture-0001",
        "capture_session_id": "session-1",
        "capture_world_id": WORLD,
        "capture_map_id": WORLD,
        "native_probe_sha256": PROBE,
        "capture_purpose": "static_topology_cache_seed",
    })
    payload["tiles"] = [{
        "tile_index_valid": True,
        "tile_index": 1,
        "salt": 1,
        "lookup_index": 0,
        "x": 0,
        "y": 0,
        "layer": 0,
        "vertices": [
            {"x": 0.0, "y": 0.0, "z": 0.0},
            {"x": -100.0, "y": 0.0, "z": 0.0},
            {"x": -100.0, "y": 0.0, "z": -100.0},
            {"x": 0.0, "y": 0.0, "z": -100.0},
            {"x": -200.0, "y": 0.0, "z": 0.0},
            {"x": -200.0, "y": 0.0, "z": -100.0},
            *(
                [
                    {"x": -300.0, "y": 0.0, "z": 0.0},
                    {"x": -300.0, "y": 0.0, "z": -100.0},
                ]
                if include_unvalidated else []
            ),
        ],
        "polygons": [
            {
                "index": 0,
                "first_link": 0,
                "vertex_count": 4,
                "vertex_indices": [0, 1, 2, 3],
                "neighbors": [],
                "flags": 1,
                "area": 0,
                "type": 0,
                "links": [
                    {"ref": PolygonReference.make(1, 1, 1).value, "next": 0, "edge": 1, "side": 128, "bmin": 0, "bmax": 0},
                ],
            },
            {
                "index": 1,
                "first_link": 0,
                "vertex_count": 4,
                "vertex_indices": [1, 4, 5, 2],
                "neighbors": [],
                "flags": 1,
                "area": 0,
                "type": 0,
                "links": [
                    {"ref": PolygonReference.make(1, 1, 0).value, "next": 0, "edge": 3, "side": 128, "bmin": 0, "bmax": 0},
                    *(
                        [{"ref": PolygonReference.make(1, 1, 2).value, "next": 0, "edge": 1, "side": 128, "bmin": 0, "bmax": 0}]
                        if include_unvalidated else []
                    ),
                ],
            },
            *(
                [{
                    "index": 2,
                    "first_link": 0,
                    "vertex_count": 4,
                    "vertex_indices": [4, 6, 7, 5],
                    "neighbors": [],
                    "flags": 1,
                    "area": 0,
                    "type": 0,
                    "links": [
                        {"ref": PolygonReference.make(1, 1, 1).value, "next": 0, "edge": 3, "side": 128, "bmin": 0, "bmax": 0},
                    ],
                }]
                if include_unvalidated else []
            ),
        ],
        "off_mesh_connections": [],
    }]
    payload["total_vertices"] = 8 if include_unvalidated else 6
    return payload


def scoped_seed_routes(artifact: Path, payload: dict, *, missing_ref: int | None = None):
    graph = DetourTopologyGraph.from_dict(payload)
    first_ref = PolygonReference.make(1, 1, 0).value
    second_ref = PolygonReference.make(1, 1, 1).value
    raw_sha = sha256_file(artifact)
    static = static_topology_fingerprint(payload)
    binding = {
        "expected_raw_artifact_sha256": raw_sha,
        "expected_static_topology_fingerprint": static,
        "executable_sha256": EXECUTABLE,
        "native_probe_sha256": PROBE,
        "capture_request_id": "partial-capture-0001",
        "capture_session_id": "session-1",
        "process_id": 123,
        "world_id": WORLD,
        "map_id": WORLD,
        "validator_source_sha256": VALIDATOR,
    }
    seeds = []
    for start_ref, end_ref in ((first_ref, second_ref), (second_ref, first_ref)):
        start = graph.polygons[start_ref].centroid
        end = graph.polygons[end_ref].centroid
        plan = graph.plan(start, end).to_dict()
        plan["executable_sha256"] = EXECUTABLE
        if missing_ref is not None and start_ref == first_ref:
            plan["polygon_corridor"][-1] = missing_ref
            plan["end_ref"] = missing_ref
        plan_sha = hashlib.sha256(
            json.dumps(plan, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        ).hexdigest()
        independent_validation = {
            "valid": True,
            "errors": [],
            "warnings": [],
            "checked_polygon_refs": list(plan["polygon_corridor"]),
            "checked_waypoint_count": len(plan["waypoints"]),
            "checked_status": "complete",
        }
        seeds.append({
            "route_plan": plan,
            "validated_route_plan_sha256": plan_sha,
            "validation_result": independent_validation,
            "binding_evidence": dict(binding),
        })
    return seeds


def write_scoped_partial_cache(root: Path, *, payload=None, missing_ref=None):
    payload = payload or partial_connected_payload()
    artifact = root / "geometry-partial.json"
    manifest_path = root / "partial-manifest.json"
    artifact.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    seeds = scoped_seed_routes(artifact, payload, missing_ref=missing_ref)
    manifest = build_topology_cache_manifest(
        artifact,
        executable_sha256=EXECUTABLE,
        native_probe_sha256=PROBE,
        parser_schema_identity=PARSER,
        world_id=WORLD,
        map_id=WORLD,
        coordinate_space="recast",
        coordinate_transform_identity=TRANSFORM,
        validator_source_sha256=VALIDATOR,
        validation_result=seeds[0]["validation_result"],
        provenance={
            "capture_request_id": "partial-capture-0001",
            "capture_session_id": "session-1",
            "independent_route_validation": True,
            "route_plan_sha256": seeds[0]["validated_route_plan_sha256"],
            "process_id": 123,
            "executable_sha256": EXECUTABLE,
            "native_probe_sha256": PROBE,
        },
        created_at_utc="2026-09-26T00:00:00Z",
        allow_scoped_partial=True,
        scoped_seed_routes=seeds,
    )
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    return artifact, manifest_path, manifest


class StaticFingerprintTests(unittest.TestCase):
    def test_capture_pose_and_timestamp_do_not_change_static_fingerprint(self):
        first = topology_payload()
        second = deepcopy(first)
        second["player_position"] = {"x": 1.0, "y": 2.0, "z": 3.0}
        second["capture_timestamp"] = "later"
        second["status"] = "partial"
        second["topology_proof"]["player_position"] = {"x": 9.0, "y": 8.0, "z": 7.0}
        self.assertNotEqual(
            hashlib.sha256(json.dumps(first, sort_keys=True).encode()).hexdigest(),
            hashlib.sha256(json.dumps(second, sort_keys=True).encode()).hexdigest(),
        )
        self.assertEqual(static_topology_fingerprint(first), static_topology_fingerprint(second))

    def test_vertex_change_changes_static_fingerprint(self):
        changed = topology_payload()
        changed["tiles"][0]["vertices"][1]["x"] += 1.0
        self.assertNotEqual(static_topology_fingerprint(topology_payload()), static_topology_fingerprint(changed))

    def test_ordinary_adjacency_link_change_changes_static_fingerprint(self):
        changed = topology_payload()
        changed["tiles"][0]["polygons"][0]["links"] = [
            {"ref": 37941741092877, "next": 0, "edge": 0, "side": 128, "bmin": 0, "bmax": 255}
        ]
        self.assertNotEqual(static_topology_fingerprint(topology_payload()), static_topology_fingerprint(changed))


class CacheAdmissionTests(unittest.TestCase):
    def test_legacy_seed_parser_identity_is_accepted_by_semantic_decoder_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, manifest_path, manifest = write_cache(root)
            legacy_identity = next(iter(LEGACY_COMPATIBLE_TOPOLOGY_PARSER_IDENTITIES))
            manifest["parser_schema_identity"] = legacy_identity
            manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
            result = default_parser_provider(manifest_path).load_current(observation())
            self.assertEqual(result.source_decision, "validated_cache_hit")

    def test_unrecognized_parser_identity_still_rejects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, manifest_path, manifest = write_cache(root)
            manifest["parser_schema_identity"] = "UNSUPPORTED-PARSER-IDENTITY"
            manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
            with self.assertRaisesRegex(AdapterError, "cache_parser_schema_identity_mismatch"):
                default_parser_provider(manifest_path).load_current(observation())
    def test_validated_cache_hit_does_not_create_native_markers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, manifest_path, manifest = write_cache(root)
            result = provider(manifest_path).load_current(observation())
            self.assertEqual(result.source_decision, "validated_cache_hit")
            self.assertEqual(result.session_id, "session-1")
            self.assertEqual(result.payload_summary["current_cloud_resolution"]["reason_code"], "point_inside_polygon")
            self.assertFalse((root / "geometry.enable").exists())
            self.assertFalse((root / "geometry.ready").exists())
            self.assertEqual(result.static_fingerprint, manifest["static_topology_fingerprint"])

    def test_cache_survives_process_or_session_restart_but_binding_is_new(self):
        with tempfile.TemporaryDirectory() as directory:
            _, manifest_path, _ = write_cache(Path(directory))
            loaded = provider(manifest_path)
            first = loaded.load_current(observation(session_id="old-session"))
            second = loaded.load_current(observation(session_id="new-session"))
            self.assertEqual(first.static_fingerprint, second.static_fingerprint)
            self.assertEqual(first.session_id, "old-session")
            self.assertEqual(second.session_id, "new-session")
            self.assertNotEqual(first.session_id, second.session_id)

    def test_unchanged_cache_reuses_graph_and_only_rebinds_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, manifest_path, _ = write_cache(root)
            loaded = provider(manifest_path)
            first = loaded.load_current(observation(session_id="old-session"))
            cached_parser_identity = loaded._cached_state["cache_key"]["parser_source_sha256"]
            with patch("ff7r_agent.live_route_adapter.load_shipping_detour_topology", side_effect=AssertionError("graph reload")), patch.object(
                loaded, "_parser_source_identity", return_value=cached_parser_identity
            ), patch("ff7r_agent.live_route_adapter.sha256_file", side_effect=AssertionError("artifact rehash")):
                second = loaded.load_current(observation(session_id="new-session"))

            self.assertIs(first.graph, second.graph)
            self.assertEqual(second.session_id, "new-session")
            self.assertEqual(second.map_id, WORLD)
            self.assertTrue(second.payload_summary["cache_reuse"])
            self.assertIn("python_topology_cache_reuse_ms", second.timings)

    def test_artifact_change_invalidates_cached_entry_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact, manifest_path, _ = write_cache(root)
            loaded = provider(manifest_path)
            loaded.load_current(observation())
            artifact.write_text(artifact.read_text(encoding="utf-8") + "\n", encoding="utf-8")

            with self.assertRaisesRegex(AdapterError, "cache_artifact_hash_mismatch"):
                loaded.load_current(observation(session_id="changed-artifact"))
            with self.assertRaisesRegex(AdapterError, "previous_identity_change"):
                loaded.load_current(observation(session_id="after-invalidation"))

    def test_manifest_identity_change_invalidates_cached_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, manifest_path, manifest = write_cache(root)
            loaded = provider(manifest_path)
            loaded.load_current(observation())
            manifest["capture_request_id"] = "capture-fixture-0002"
            manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")

            with self.assertRaisesRegex(AdapterError, "cache_manifest_stat_changed"):
                loaded.load_current(observation(session_id="changed-manifest"))

    def test_configured_identity_change_invalidates_cached_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, manifest_path, _ = write_cache(root)
            loaded = provider(manifest_path)
            loaded.load_current(observation())
            loaded.expected_validator_source_sha256 = "X" * 64

            with self.assertRaisesRegex(AdapterError, "cache_validator_identity_mismatch"):
                loaded.load_current(observation(session_id="changed-validator"))

    def test_validator_reuses_construction_but_validates_each_new_route_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, manifest_path, _ = write_cache(root)
            loaded = provider(manifest_path)
            first_topology = loaded.load_current(observation(session_id="first-session"))
            second_topology = loaded.load_current(observation(session_id="second-session"))
            validator = IndependentJsonValidator(None, require_topology_binding=True)

            first_evidence = validator.bind_topology_session(first_topology)
            first_validator = validator._load()
            first_plan = first_topology.graph.plan(
                Vec3(0.0, 0.0, 0.0), Vec3(100.0, 0.0, 0.0)
            ).to_dict()
            second_plan = second_topology.graph.plan(
                Vec3(0.0, 0.0, 0.0), Vec3(-100.0, 0.0, 0.0)
            ).to_dict()
            with patch.object(first_validator, "validate", wraps=first_validator.validate) as validate:
                first_result = validator.validate(first_plan)
                second_evidence = validator.bind_topology_session(second_topology)
                self.assertIs(first_validator, validator._load())
                second_result = validator.validate(second_plan)

            self.assertTrue(first_result["valid"])
            self.assertTrue(second_result["valid"])
            self.assertEqual(validate.call_count, 2)
            self.assertNotEqual(validate.call_args_list[0].args[0], validate.call_args_list[1].args[0])
            self.assertEqual(first_evidence["session_id"], "first-session")
            self.assertEqual(second_evidence["session_id"], "second-session")
            self.assertFalse(first_evidence["validator_construction_reused"])
            self.assertTrue(second_evidence["validator_construction_reused"])

    def test_world_map_executable_and_probe_mismatch_reject(self):
        cases = [
            (observation(), {"world_id": "OtherWorld"}, "cache_world_mismatch"),
            (observation(), {}, "cache_current_executable_identity_mismatch"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, manifest_path, _ = write_cache(root, **cases[0][1])
            with self.assertRaisesRegex(AdapterError, cases[0][2]):
                provider(manifest_path).load_current(cases[0][0])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, manifest_path, _ = write_cache(root)
            with self.assertRaisesRegex(AdapterError, "cache_current_executable_identity_mismatch"):
                provider(manifest_path).load_current(observation(executable="X" * 64))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, manifest_path, _ = write_cache(root, native_probe_sha256="Q" * 64)
            with self.assertRaisesRegex(AdapterError, "cache_native_probe_identity_mismatch"):
                provider(manifest_path).load_current(observation())

    def test_cache_from_a_geometry_equivalent_probe_build_loads(self):
        with tempfile.TemporaryDirectory() as directory:
            _, manifest_path, _ = write_cache(Path(directory), native_probe_sha256="Q" * 64)
            with patch("ff7r_agent.topology_cache.GEOMETRY_EQUIVALENT_NATIVE_PROBES",
                       (frozenset({PROBE, "Q" * 64}),)):
                provider(manifest_path).load_current(observation())
        self.assertTrue(native_probe_identities_match("q" * 64, "Q" * 64))
        self.assertFalse(native_probe_identities_match(None, "Q" * 64))
        self.assertTrue(native_probe_identities_match(
            "AFBF559C6E87FDCF0C3FEBCA4421B1CF352CEC796DC3886FD3D14F68ECCED7E3",
            "9771BD24E1B71D6E45FB0701EB7CEB0D085D7E331D5DB37763B20EAB509FC3E4"))

    def test_status_partial_without_positive_provenance_cannot_enter_cache(self):
        payload = topology_payload()
        payload["status"] = "partial"
        payload["topology_proof"]["status"] = "rejected"
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(TopologyCacheError, "topology_artifact_not_complete"):
                write_cache(Path(directory), payload=payload)

    def test_partial_scope_requires_opt_in_and_preserves_original_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact, manifest_path, manifest = write_scoped_partial_cache(root)
            with self.assertRaisesRegex(AdapterError, "cache_scoped_partial_requires_feature_opt_in"):
                provider(manifest_path).load_current(observation(position=Vec3(50.0, 50.0, 0.0)))
            loaded = ValidatedTopologyCacheProvider(
                manifest_path,
                allow_scoped_partial=True,
                expected_executable_sha256=EXECUTABLE,
                expected_native_probe_sha256=PROBE,
                expected_parser_schema_identity=PARSER,
                expected_validator_source_sha256=VALIDATOR,
                native_probe_path=None,
            ).load_current(observation(position=Vec3(50.0, 50.0, 0.0)))
            scope = manifest["corridor_scope"]
            self.assertEqual(json.loads(artifact.read_text(encoding="utf-8"))["status"], "partial")
            self.assertEqual(loaded.artifact_sha256, sha256_file(artifact))
            self.assertEqual(loaded.fingerprint, manifest["static_topology_fingerprint"])
            self.assertEqual(loaded.scope_identity, scope["scope_identity"])
            self.assertEqual(set(loaded.scope_polygon_refs), set(scope["polygon_refs"]))
            self.assertEqual(len(loaded.scope_transition_signatures), 2)
            self.assertEqual(len(loaded.graph.polygons), 2)
            self.assertEqual(sum(map(len, loaded.graph.adjacency.values())), 2)

    def test_partial_scope_builder_rejects_unresolved_failed_tile_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = partial_connected_payload()
            artifact = root / "geometry.json"
            artifact.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
            failed_tile_ref = PolygonReference.make(1, 1276, 0).value
            with self.assertRaisesRegex(TopologyCacheError, "seed_reference_not_resolved_ground"):
                seeds = scoped_seed_routes(artifact, payload, missing_ref=failed_tile_ref)
                build_topology_cache_manifest(
                    artifact,
                    executable_sha256=EXECUTABLE,
                    native_probe_sha256=PROBE,
                    parser_schema_identity=PARSER,
                    world_id=WORLD,
                    map_id=WORLD,
                    coordinate_space="recast",
                    coordinate_transform_identity=TRANSFORM,
                    validator_source_sha256=VALIDATOR,
                    validation_result=seeds[0]["validation_result"],
                    provenance={"capture_request_id": "partial-capture-0001", "capture_session_id": "session-1", "independent_route_validation": True, "process_id": 123, "executable_sha256": EXECUTABLE, "native_probe_sha256": PROBE},
                    created_at_utc="2026-09-26T00:00:00Z",
                    allow_scoped_partial=True,
                    scoped_seed_routes=seeds,
                )

    def test_partial_scope_requires_proved_artifact_and_matching_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = partial_connected_payload()
            payload["topology_proof"]["status"] = "rejected"
            artifact = root / "geometry.json"
            artifact.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
            with self.assertRaisesRegex(TopologyCacheError, "partial_scope_native_proof_not_positive"):
                seeds = scoped_seed_routes(artifact, payload)
                build_topology_cache_manifest(
                    artifact,
                    executable_sha256=EXECUTABLE,
                    native_probe_sha256=PROBE,
                    parser_schema_identity=PARSER,
                    world_id=WORLD,
                    map_id=WORLD,
                    coordinate_space="recast",
                    coordinate_transform_identity=TRANSFORM,
                    validator_source_sha256=VALIDATOR,
                    validation_result=seeds[0]["validation_result"],
                    provenance={"capture_request_id": "partial-capture-0001", "capture_session_id": "session-1", "independent_route_validation": True, "process_id": 123, "executable_sha256": EXECUTABLE, "native_probe_sha256": PROBE},
                    created_at_utc="2026-09-26T00:00:00Z",
                    allow_scoped_partial=True,
                    scoped_seed_routes=seeds,
                )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, manifest_path, manifest = write_scoped_partial_cache(root)
            manifest["corridor_scope"]["transitions"][0]["left"]["x"] += 1.0
            manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
            scoped_provider = ValidatedTopologyCacheProvider(
                manifest_path,
                allow_scoped_partial=True,
                expected_executable_sha256=EXECUTABLE,
                expected_native_probe_sha256=PROBE,
                expected_parser_schema_identity=PARSER,
                expected_validator_source_sha256=VALIDATOR,
                native_probe_path=None,
            )
            with self.assertRaisesRegex(AdapterError, "partial_scope_identity_or_contents_mismatch"):
                scoped_provider.load_current(observation(position=Vec3(50.0, 50.0, 0.0)))

    def test_partial_scope_binds_changed_artifact_and_validator_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact, manifest_path, _ = write_scoped_partial_cache(root)
            artifact.write_text(artifact.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            scoped_provider = ValidatedTopologyCacheProvider(
                manifest_path,
                allow_scoped_partial=True,
                expected_executable_sha256=EXECUTABLE,
                expected_native_probe_sha256=PROBE,
                expected_parser_schema_identity=PARSER,
                expected_validator_source_sha256=VALIDATOR,
                native_probe_path=None,
            )
            with self.assertRaisesRegex(AdapterError, "cache_artifact_hash_mismatch"):
                scoped_provider.load_current(observation(position=Vec3(50.0, 50.0, 0.0)))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, manifest_path, _ = write_scoped_partial_cache(root)
            changed_validator_provider = ValidatedTopologyCacheProvider(
                manifest_path,
                allow_scoped_partial=True,
                expected_executable_sha256=EXECUTABLE,
                expected_native_probe_sha256=PROBE,
                expected_parser_schema_identity=PARSER,
                expected_validator_source_sha256="X" * 64,
                native_probe_path=None,
            )
            with self.assertRaisesRegex(AdapterError, "cache_validator_identity_mismatch"):
                changed_validator_provider.load_current(observation(position=Vec3(50.0, 50.0, 0.0)))

    def test_partial_scope_binds_capture_and_current_process_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, manifest_path, _ = write_scoped_partial_cache(root)
            scoped_provider = ValidatedTopologyCacheProvider(
                manifest_path,
                allow_scoped_partial=True,
                expected_executable_sha256=EXECUTABLE,
                expected_native_probe_sha256=PROBE,
                expected_parser_schema_identity=PARSER,
                expected_validator_source_sha256=VALIDATOR,
                native_probe_path=None,
            )
            with self.assertRaisesRegex(AdapterError, "cache_partial_scope_process_identity_mismatch"):
                scoped_provider.load_current(observation(position=Vec3(50.0, 50.0, 0.0), process_id=124))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, manifest_path, _ = write_scoped_partial_cache(root)
            scoped_provider = ValidatedTopologyCacheProvider(
                manifest_path,
                allow_scoped_partial=True,
                expected_executable_sha256=EXECUTABLE,
                expected_native_probe_sha256=PROBE,
                expected_parser_schema_identity=PARSER,
                expected_validator_source_sha256=VALIDATOR,
                native_probe_path=None,
            )
            first = scoped_provider.load_current(observation(position=Vec3(50.0, 50.0, 0.0)))
            self.assertEqual(first.scope_process_id, 123)
            with self.assertRaisesRegex(AdapterError, "cache_partial_scope_process_identity_mismatch"):
                scoped_provider.load_current(observation(
                    session_id="new-session", position=Vec3(50.0, 50.0, 0.0), process_id=124,
                ))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, manifest_path, manifest = write_scoped_partial_cache(root)
            manifest["capture_request_id"] = "different-capture-request"
            manifest["provenance"]["capture_request_id"] = "different-capture-request"
            manifest["capture_provenance"]["capture_request_id"] = "different-capture-request"
            manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
            scoped_provider = ValidatedTopologyCacheProvider(
                manifest_path,
                allow_scoped_partial=True,
                expected_executable_sha256=EXECUTABLE,
                expected_native_probe_sha256=PROBE,
                expected_parser_schema_identity=PARSER,
                expected_validator_source_sha256=VALIDATOR,
                native_probe_path=None,
            )
            with self.assertRaisesRegex(AdapterError, "cache_partial_scope_proof_not_positive"):
                scoped_provider.load_current(observation(position=Vec3(50.0, 50.0, 0.0)))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, manifest_path, manifest = write_scoped_partial_cache(root)
            manifest["native_probe_sha256"] = "Q" * 64
            manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
            scoped_provider = ValidatedTopologyCacheProvider(
                manifest_path,
                allow_scoped_partial=True,
                expected_executable_sha256=EXECUTABLE,
                expected_native_probe_sha256="Q" * 64,
                expected_parser_schema_identity=PARSER,
                expected_validator_source_sha256=VALIDATOR,
                native_probe_path=None,
            )
            with self.assertRaisesRegex(AdapterError, "cache_partial_scope_proof_not_positive"):
                scoped_provider.load_current(observation(position=Vec3(50.0, 50.0, 0.0)))

    def test_partial_scope_rejects_capture_identity_not_matching_raw_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = partial_connected_payload()
            payload["capture_session_id"] = "another-session"
            artifact = root / "geometry.json"
            artifact.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
            seeds = scoped_seed_routes(artifact, payload)
            with self.assertRaisesRegex(TopologyCacheError, "partial_scope_capture_binding_mismatch"):
                build_topology_cache_manifest(
                    artifact,
                    executable_sha256=EXECUTABLE,
                    native_probe_sha256=PROBE,
                    parser_schema_identity=PARSER,
                    world_id=WORLD,
                    map_id=WORLD,
                    coordinate_space="recast",
                    coordinate_transform_identity=TRANSFORM,
                    validator_source_sha256=VALIDATOR,
                    validation_result=seeds[0]["validation_result"],
                    provenance={
                        "capture_request_id": "partial-capture-0001",
                        "capture_session_id": "session-1",
                        "independent_route_validation": True,
                        "process_id": 123,
                        "executable_sha256": EXECUTABLE,
                        "native_probe_sha256": PROBE,
                    },
                    created_at_utc="2026-09-26T00:00:00Z",
                    allow_scoped_partial=True,
                    scoped_seed_routes=seeds,
                )

    def test_scope_excludes_outside_polygons_from_route_and_recovery_plans(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = partial_connected_payload(include_unvalidated=True)
            artifact, manifest_path, _ = write_scoped_partial_cache(root, payload=payload)
            full_graph = DetourTopologyGraph.from_dict(payload)
            scoped = ValidatedTopologyCacheProvider(
                manifest_path,
                allow_scoped_partial=True,
                expected_executable_sha256=EXECUTABLE,
                expected_native_probe_sha256=PROBE,
                expected_parser_schema_identity=PARSER,
                expected_validator_source_sha256=VALIDATOR,
                native_probe_path=None,
            ).load_current(observation(position=Vec3(50.0, 50.0, 0.0)))

            first_ref = PolygonReference.make(1, 1, 0).value
            second_ref = PolygonReference.make(1, 1, 1).value
            outside_ref = PolygonReference.make(1, 1, 2).value
            self.assertEqual(set(scoped.graph.polygons), {first_ref, second_ref})
            outside_point = full_graph.polygons[outside_ref].centroid
            self.assertFalse(scoped.graph.resolve_point(
                outside_point,
                max_snap_distance_m=0.25,
                max_vertical_separation_m=1.0,
            ).ok)
            self.assertNotEqual(
                scoped.graph.plan(Vec3(50.0, 50.0, 0.0), outside_point, max_snap_distance_m=0.25).status,
                "complete",
            )

            start = observation(position=Vec3(50.0, 50.0, 0.0))
            outside_plan = full_graph.plan(start.position, outside_point).to_dict()
            adapter = object.__new__(LiveRouteAdapter)
            adapter.config = LiveAdapterConfig(route_mode="cached_topology")
            adapter.start_resolution = scoped.graph.resolve_point(
                start.position,
                max_snap_distance_m=adapter.config.start_max_snap_m,
                max_vertical_separation_m=adapter.config.start_max_vertical_m,
            )
            inside_plan = full_graph.plan(start.position, full_graph.polygons[second_ref].centroid).to_dict()
            adapter._validate_first_route(inside_plan, scoped, start)
            with self.assertRaisesRegex(AdapterError, "cached_topology_corridor_not_ordinary_ground"):
                adapter._validate_first_route(outside_plan, scoped, start)
            with self.assertRaisesRegex(AdapterError, "cached_topology_corridor_not_ordinary_ground"):
                adapter._validate_recovery_route(outside_plan, scoped, start, outside_point)

    def test_scope_filters_alternate_portals_with_same_polygon_refs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = partial_connected_payload()
            _, _, manifest = write_scoped_partial_cache(root, payload=payload)
            graph = DetourTopologyGraph.from_dict(payload)
            first_ref = PolygonReference.make(1, 1, 0).value
            second_ref = PolygonReference.make(1, 1, 1).value
            exact = next(portal for portal in graph.adjacency[first_ref] if portal.to_reference == second_ref)
            alternate = dataclass_replace(
                exact,
                left=Vec3(exact.left.x + 1.0, exact.left.y, exact.left.z),
            )
            adjacency = dict(graph.adjacency)
            adjacency[first_ref] = (*adjacency[first_ref], alternate)
            graph_with_alternate = DetourTopologyGraph(
                graph.polygons,
                adjacency,
                snapshot_diagnostics=graph.snapshot_diagnostics,
                portal_tolerance_cm=graph.portal_tolerance_cm,
            )
            restricted, _, _ = ValidatedTopologyCacheProvider._restrict_graph_to_corridor_scope(
                graph_with_alternate,
                manifest["corridor_scope"],
            )
            self.assertEqual(
                [portal.to_dict() for portal in restricted.adjacency[first_ref]],
                [exact.to_dict()],
            )

            altered = deepcopy(manifest["corridor_scope"])
            transition = next(
                item for item in altered["transitions"]
                if item["from_ref"] == first_ref and item["to_ref"] == second_ref
            )
            transition["left"]["x"] += 1.0
            with self.assertRaisesRegex(AdapterError, "cache_partial_scope_transition_not_in_graph"):
                ValidatedTopologyCacheProvider._restrict_graph_to_corridor_scope(
                    graph,
                    altered,
                )

    def test_manifest_hash_or_shape_failure_rejects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact, manifest_path, _ = write_cache(root)
            artifact.write_text(artifact.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            with self.assertRaisesRegex(AdapterError, "cache_artifact_hash_mismatch"):
                provider(manifest_path).load_current(observation())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = root / "manifest.json"
            manifest_path.write_text("{\"cache_schema_version\": 1}", encoding="utf-8")
            with self.assertRaisesRegex(AdapterError, "cache_fingerprint_schema_incompatible"):
                provider(manifest_path).load_current(observation())

    def test_current_cloud_resolution_bounds_are_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            _, manifest_path, _ = write_cache(Path(directory))
            with self.assertRaisesRegex(AdapterError, "cache_current_cloud_unresolvable"):
                provider(manifest_path).load_current(observation(position=Vec3(1800.0, 0.0, 0.0)))
        with tempfile.TemporaryDirectory() as directory:
            _, manifest_path, _ = write_cache(Path(directory))
            with self.assertRaisesRegex(AdapterError, "cache_current_cloud_unresolvable"):
                provider(manifest_path).load_current(observation(position=Vec3(0.0, 0.0, 200.0)))

    def test_positive_validation_is_required_even_if_manifest_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, manifest_path, _ = write_cache(
                root,
                validation_result={"valid": False, "errors": ["rejected"], "warnings": []},
            )
            with self.assertRaisesRegex(AdapterError, "cache_independent_validation_not_positive"):
                provider(manifest_path).load_current(observation())

    def test_capture_request_provenance_is_required_for_cache_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, manifest_path, manifest = write_cache(root)
            manifest.pop("capture_request_id", None)
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(AdapterError, "cache_provenance_not_positive"):
                provider(manifest_path).load_current(observation())

    def test_cache_hit_feeds_current_resolution_planning_and_independent_gate(self):
        class Telemetry:
            def __init__(self):
                self.samples = [
                    observation(),
                    observation(session_id="session-1", sequence=11),
                    observation(session_id="session-1", sequence=12),
                ]
                self.index = 0

            def observe(self):
                sample = self.samples[min(self.index, len(self.samples) - 1)]
                self.index += 1
                return sample

        class Actuator:
            def neutralize(self):
                return {"neutral": True}

            def is_neutral(self):
                return True

        class Validator:
            timing = {}

            def validate(self, plan):
                self.plan = plan
                return {"valid": True, "errors": [], "warnings": []}

        class Render:
            def observe(self, sample):
                return RenderedGameplayEvidence(
                    ready=True,
                    reason="rendered_gameplay_confirmed",
                    classifier_label="gameplay",
                    metrics={"whole_nonblack": 1.0},
                    capture={"ok": True, "pid": sample.process_id},
                )

        with tempfile.TemporaryDirectory() as directory:
            _, manifest_path, _ = write_cache(Path(directory))
            adapter = LiveRouteAdapter(
                Telemetry(),
                provider(manifest_path),
                Validator(),
                lambda plan: object(),
                Actuator(),
                render_evidence=Render(),
                config=LiveAdapterConfig(max_cycles=2, post_arrival_samples=0),
                monotonic=lambda: 10.0,
                sleeper=lambda _: None,
            )
            report = adapter.preflight()
            self.assertTrue(report["movement_authorized"])
            self.assertEqual(report["topology"]["source_decision"], "validated_cache_hit")
            self.assertEqual(report["start_resolution"]["horizontal_distance_m"], 0.0)
            self.assertTrue(any(
                event.get("event") == "topology_source_selected"
                and event.get("decision") == "validated_cache_hit"
                for event in report["timeline"]
            ))


if __name__ == "__main__":
    unittest.main()
