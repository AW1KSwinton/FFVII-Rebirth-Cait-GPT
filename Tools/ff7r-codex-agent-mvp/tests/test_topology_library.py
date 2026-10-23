from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from ff7r_agent.live_route_adapter import AdapterError
from ff7r_agent.models import Vec3
from ff7r_agent.topology_cache import (
    TILE_EXCLUDED_PARTIAL_SCHEMA,
    TopologyCacheError,
    admit_topology_cache,
    coverage_contains,
    load_topology_library,
    select_library_manifest,
    tile_excluded_partial,
    topology_coverage,
)
from test_topology_cache import EXECUTABLE, WORLD, observation, provider, topology_payload, write_cache


def payload_at(unreal_x: float) -> dict:
    """The fixture's 20 m square tile, moved to be centred on ``unreal_x``."""
    payload = topology_payload()
    for vertex in payload["tiles"][0]["vertices"]:
        vertex["x"] -= unreal_x  # Unreal x is -recast.x
    return payload


def payload_raised(unreal_z: float) -> dict:
    """The fixture's tile lifted ``unreal_z`` cm, like the Gold Saucer over the Dustbowl."""
    payload = topology_payload()
    for vertex in payload["tiles"][0]["vertices"]:
        vertex["y"] += unreal_z  # Unreal z is recast.y
    return payload


def tile_excluded(payload: dict, *, valid: int = 101, exported: int = 100) -> dict:
    payload = deepcopy(payload)
    payload.update({
        "status": "partial", "reason_code": "geometry_stage_rejections", "truncated": False,
        "lookup_chain_complete": True, "polygon_read_failures": 0, "link_read_failures": 0,
        "rejected_pointers": 0, "cyclic_pointers": 0, "valid_unique_tiles": valid,
        "exported_unique_tiles": exported, "tiles_with_incomplete_link_data": valid - exported,
    })
    payload["tiles"] = payload["tiles"] * exported
    return payload


def admit(root: Path, cache: Path, payload: dict, name: str, created: str) -> dict:
    staging = root / name
    staging.mkdir()
    artifact, _, manifest = write_cache(staging, payload, created_at_utc=created)
    return admit_topology_cache(artifact, cache / "manifest.json", manifest)


class CoverageTests(unittest.TestCase):
    def test_junon_full_scan_accepts_only_its_two_rejected_tiles(self):
        # 4,653 active tiles at Junon; two unknown shipping links are still
        # excluded, rather than weakening link checks to get a complete flag.
        payload = tile_excluded(topology_payload(), valid=4653, exported=4651)
        self.assertEqual(tile_excluded_partial(payload)["excluded_tiles"], 2)
        payload["truncated"] = True
        payload["reason_code"] = "geometry_export_bounded"
        self.assertIsNone(tile_excluded_partial(payload))

    def test_coverage_is_in_unreal_space(self):
        coverage = topology_coverage(payload_at(50_000.0))
        self.assertEqual(coverage["x"], [49_000.0, 51_000.0])
        self.assertEqual(coverage["y"], [-1_000.0, 1_000.0])
        self.assertEqual(coverage["tiles"], 1)
        self.assertEqual(coverage["z_bands"], [0])
        self.assertEqual(topology_coverage(payload_raised(50_000.0))["z_bands"], [50])

    def test_a_height_is_covered_only_near_the_navmesh(self):
        coverage = topology_coverage(payload_raised(3_000.0))
        self.assertTrue(coverage_contains(coverage, 0.0, 0.0, 3_100.0))
        self.assertTrue(coverage_contains(coverage, 0.0, 0.0, 2_600.0))
        self.assertFalse(coverage_contains(coverage, 0.0, 0.0, 54_800.0))
        self.assertTrue(coverage_contains(coverage, 0.0, 0.0))
        coverage.pop("z_bands")
        self.assertTrue(coverage_contains(coverage, 0.0, 0.0, 54_800.0))

    def test_a_ramp_covers_the_heights_between_its_ends(self):
        payload = topology_payload()
        for vertex in payload["tiles"][0]["vertices"][:2]:
            vertex["y"] = 4_000.0  # one edge 40 m up a steep ramp
        coverage = topology_coverage(payload)
        self.assertEqual(coverage["z_bands"], [0, 1, 2, 3, 4])
        self.assertTrue(coverage_contains(coverage, 0.0, 0.0, 2_000.0))


class LibraryTests(unittest.TestCase):
    def test_selects_the_capture_covering_cloud(self):
        with tempfile.TemporaryDirectory() as directory:
            root, cache = Path(directory), Path(directory) / "cache"
            west = admit(root, cache, payload_at(0.0), "west", "2026-09-01T00:00:00Z")
            east = admit(root, cache, payload_at(50_000.0), "east", "2026-09-02T00:00:00Z")
            library = load_topology_library(cache)
            self.assertEqual(len(library["entries"]), 2)

            for x, expected in ((0.0, west), (50_000.0, east)):
                loaded = provider(cache / "manifest.json")
                result = loaded.load_current(observation(position=Vec3(x, 0.0, 0.0)))
                self.assertEqual(result.source_decision, "validated_cache_hit")
                self.assertEqual(result.static_fingerprint, expected["static_topology_fingerprint"])

    def test_one_provider_switches_capture_when_cloud_moves(self):
        with tempfile.TemporaryDirectory() as directory:
            root, cache = Path(directory), Path(directory) / "cache"
            west = admit(root, cache, payload_at(0.0), "west", "2026-09-01T00:00:00Z")
            east = admit(root, cache, payload_at(50_000.0), "east", "2026-09-02T00:00:00Z")
            loaded = provider(cache / "manifest.json")
            first = loaded.load_current(observation(position=Vec3(0.0, 0.0, 0.0)))
            second = loaded.load_current(observation(position=Vec3(50_000.0, 0.0, 0.0)))
            self.assertEqual(first.static_fingerprint, west["static_topology_fingerprint"])
            self.assertEqual(second.static_fingerprint, east["static_topology_fingerprint"])

    def test_prefers_the_capture_that_also_covers_the_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root, cache = Path(directory), Path(directory) / "cache"
            wide = admit(root, cache, payload_at(0.0), "near", "2026-09-01T00:00:00Z")
            admit(root, cache, payload_at(1_500.0), "newer", "2026-09-02T00:00:00Z")
            selected, decision = select_library_manifest(
                cache, world_id=WORLD, map_id=WORLD, executable_sha256=EXECUTABLE,
                position=(600.0, 0.0), target=(-900.0, 0.0))
            self.assertEqual(decision["selected"], wide["static_topology_fingerprint"])
            self.assertTrue(decision["covers_target"])
            _, newest = select_library_manifest(
                cache, world_id=WORLD, map_id=WORLD, executable_sha256=EXECUTABLE, position=(600.0, 0.0))
            self.assertNotEqual(newest["selected"], wide["static_topology_fingerprint"])

    def test_stacked_areas_select_the_capture_at_cloud_height(self):
        # #359: the Gold Saucer capture covers the Dustbowl's x/y from 500 m
        # up and, being newer, was chosen for the route on the ground.
        with tempfile.TemporaryDirectory() as directory:
            root, cache = Path(directory), Path(directory) / "cache"
            ground = admit(root, cache, payload_raised(3_000.0), "dustbowl", "2026-09-01T00:00:00Z")
            saucer = admit(root, cache, payload_raised(50_000.0), "saucer", "2026-09-02T00:00:00Z")
            for z, expected in ((3_100.0, ground), (50_100.0, saucer)):
                _, decision = select_library_manifest(
                    cache, world_id=WORLD, map_id=WORLD, executable_sha256=EXECUTABLE,
                    position=(0.0, 0.0, z), target=(500.0, 0.0, z))
                self.assertEqual(decision["selected"], expected["static_topology_fingerprint"])
                self.assertTrue(decision["covers_target"])
                result = provider(cache / "manifest.json").load_current(observation(position=Vec3(0.0, 0.0, z)))
                self.assertEqual(result.static_fingerprint, expected["static_topology_fingerprint"])
            _, between = select_library_manifest(
                cache, world_id=WORLD, map_id=WORLD, executable_sha256=EXECUTABLE, position=(0.0, 0.0, 25_000.0))
            self.assertEqual(between["reason"], "topology_library_no_capture_covers_current_position")

    def test_a_target_at_another_height_is_not_covered(self):
        with tempfile.TemporaryDirectory() as directory:
            root, cache = Path(directory), Path(directory) / "cache"
            admit(root, cache, payload_raised(50_000.0), "saucer", "2026-09-02T00:00:00Z")
            _, decision = select_library_manifest(
                cache, world_id=WORLD, map_id=WORLD, executable_sha256=EXECUTABLE,
                position=(0.0, 0.0, 50_100.0), target=(500.0, 0.0, 3_100.0))
            self.assertFalse(decision["covers_target"])

    def test_entries_indexed_without_heights_gain_them_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root, cache = Path(directory), Path(directory) / "cache"
            ground = admit(root, cache, payload_raised(3_000.0), "dustbowl", "2026-09-01T00:00:00Z")
            admit(root, cache, payload_raised(50_000.0), "saucer", "2026-09-02T00:00:00Z")
            library = load_topology_library(cache)
            for entry in library["entries"]:
                entry["coverage"].pop("z_bands")
            (cache / "library.json").write_text(json.dumps(library), encoding="utf-8")

            _, xy_only = select_library_manifest(
                cache, world_id=WORLD, map_id=WORLD, executable_sha256=EXECUTABLE, position=(0.0, 0.0))
            self.assertNotEqual(xy_only["selected"], ground["static_topology_fingerprint"])
            _, decision = select_library_manifest(
                cache, world_id=WORLD, map_id=WORLD, executable_sha256=EXECUTABLE, position=(0.0, 0.0, 3_100.0))
            self.assertEqual(decision["selected"], ground["static_topology_fingerprint"])
            saved = {entry["static_topology_fingerprint"]: entry["coverage"].get("z_bands")
                     for entry in load_topology_library(cache)["entries"]}
            self.assertEqual(sorted(saved.values()), [[3], [50]])

    def test_no_capture_covering_cloud_is_a_cache_miss(self):
        with tempfile.TemporaryDirectory() as directory:
            root, cache = Path(directory), Path(directory) / "cache"
            admit(root, cache, payload_at(0.0), "west", "2026-09-01T00:00:00Z")
            loaded = provider(cache / "manifest.json")
            with self.assertRaisesRegex(AdapterError, "topology_library_no_capture_covers_current_position"):
                loaded.load_current(observation(position=Vec3(90_000.0, 0.0, 0.0)))
            self.assertEqual(loaded.source_decision, "validated_cache_miss")

    def test_provider_skips_a_preferred_capture_from_another_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            root, cache = Path(directory), Path(directory) / "cache"
            current = admit(root, cache, payload_at(0.0), "current", "2026-09-01T00:00:00Z")
            stale = admit(root, cache, payload_at(500.0), "stale", "2026-09-02T00:00:00Z")
            library = load_topology_library(cache)
            for entry in library["entries"]:
                if entry["static_topology_fingerprint"] == stale["static_topology_fingerprint"]:
                    entry["native_probe_sha256"] = "X" * 64
            (cache / "library.json").write_text(json.dumps(library), encoding="utf-8")

            result = provider(cache / "manifest.json").load_current(observation())
            self.assertEqual(result.static_fingerprint, current["static_topology_fingerprint"])

    def test_provider_skips_a_proved_capture_from_another_validator(self):
        # Live after #370: the proved 2026-10-03 Kalm capture outranked a fresh
        # one and every route stopped with cache_validator_identity_mismatch.
        with tempfile.TemporaryDirectory() as directory:
            root, cache = Path(directory), Path(directory) / "cache"
            stale = admit(root, cache, payload_at(500.0), "stale", "2026-09-01T00:00:00Z")
            current = admit(root, cache, payload_at(0.0), "current", "2026-09-02T00:00:00Z")
            library = load_topology_library(cache)
            for entry in library["entries"]:
                is_stale = entry["static_topology_fingerprint"] == stale["static_topology_fingerprint"]
                entry["topology_proof_status"] = "proved" if is_stale else "not_proven"
                if is_stale:
                    stale_manifest = cache / entry["manifest"]
            (cache / "library.json").write_text(json.dumps(library), encoding="utf-8")
            manifest = json.loads(stale_manifest.read_text(encoding="utf-8"))
            validator = manifest["validator_source_sha256"]
            manifest["validator_source_sha256"] = "0" * 64
            stale_manifest.write_text(json.dumps(manifest), encoding="utf-8")

            _, unfiltered = select_library_manifest(
                cache, world_id=WORLD, map_id=WORLD, executable_sha256=EXECUTABLE, position=(0.0, 0.0))
            self.assertEqual(unfiltered["selected"], stale["static_topology_fingerprint"])
            _, decision = select_library_manifest(
                cache, world_id=WORLD, map_id=WORLD, executable_sha256=EXECUTABLE, position=(0.0, 0.0),
                validator_source_sha256=validator.lower())
            self.assertEqual(decision["selected"], current["static_topology_fingerprint"])
            self.assertEqual(decision["skipped_validator_mismatch"], [stale["static_topology_fingerprint"]])
            result = provider(cache / "manifest.json").load_current(observation())
            self.assertEqual(result.static_fingerprint, current["static_topology_fingerprint"])

    def test_incompatible_probe_entries_cannot_fall_back_to_the_configured_slot(self):
        with tempfile.TemporaryDirectory() as directory:
            root, cache = Path(directory), Path(directory) / "cache"
            admit(root, cache, payload_at(0.0), "stale", "2026-09-01T00:00:00Z")
            library = load_topology_library(cache)
            library["entries"][0]["native_probe_sha256"] = "X" * 64
            (cache / "library.json").write_text(json.dumps(library), encoding="utf-8")

            loaded = provider(cache / "manifest.json")
            with self.assertRaisesRegex(AdapterError, "topology_library_no_capture_covers_current_position"):
                loaded.load_current(observation())
            self.assertEqual(loaded.source_decision, "validated_cache_miss")

    def test_first_admission_indexes_the_old_single_slot(self):
        with tempfile.TemporaryDirectory() as directory:
            root, cache = Path(directory), Path(directory) / "cache"
            cache.mkdir()
            _, legacy_path, legacy = write_cache(cache, payload_at(0.0))
            legacy.pop("coverage")
            legacy["source_artifact_path"] = "geometry.json"
            legacy_path.write_text(json.dumps(legacy), encoding="utf-8")
            admit(root, cache, payload_at(50_000.0), "east", "2026-09-02T00:00:00Z")
            library = load_topology_library(cache)
            fingerprints = {entry["static_topology_fingerprint"] for entry in library["entries"]}
            self.assertIn(legacy["static_topology_fingerprint"], fingerprints)
            loaded = provider(cache / "manifest.json")
            result = loaded.load_current(observation(position=Vec3(0.0, 0.0, 0.0)))
            self.assertEqual(result.static_fingerprint, legacy["static_topology_fingerprint"])

    def test_without_a_library_the_configured_slot_is_used(self):
        with tempfile.TemporaryDirectory() as directory:
            _, manifest_path, _ = write_cache(Path(directory))
            result = provider(manifest_path).load_current(observation())
            self.assertEqual(result.source_decision, "validated_cache_hit")


class TileExcludedPartialTests(unittest.TestCase):
    def test_a_few_left_out_tiles_are_admitted_whole(self):
        payload = tile_excluded(topology_payload())
        self.assertEqual(tile_excluded_partial(payload)["excluded_tiles"], 1)
        with tempfile.TemporaryDirectory() as directory:
            root, cache = Path(directory), Path(directory) / "cache"
            admitted = admit(root, cache, payload, "kalm", "2026-09-26T00:00:00Z")
            self.assertEqual(admitted["admission_mode"], TILE_EXCLUDED_PARTIAL_SCHEMA)
            result = provider(cache / "manifest.json").load_current(observation())
            self.assertEqual(result.source_decision, "validated_cache_hit")

    def test_rejects_more_than_one_percent_or_unproved_or_read_failures(self):
        self.assertIsNone(tile_excluded_partial(tile_excluded(topology_payload(), valid=110, exported=100)))
        unproved = tile_excluded(topology_payload())
        unproved["topology_proof"]["status"] = "not_proven"
        self.assertIsNone(tile_excluded_partial(unproved))
        failing = tile_excluded(topology_payload())
        failing["link_read_failures"] = 2
        self.assertIsNone(tile_excluded_partial(failing))
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(TopologyCacheError):
                write_cache(Path(directory), unproved)


if __name__ == "__main__":
    unittest.main()


class AdmissionRouteFallbackTests(unittest.TestCase):
    class Adapter:
        def __init__(self, config, failing):
            self.config, self.failing, self.timeline, self.tried = config, failing, [], []

        def monotonic(self):
            return 0.0

        def _choose_destination(self, topology, observation, start_ref):
            self.tried.append(self.config.route_mode)
            if self.config.route_mode in self.failing:
                raise AdapterError(f"no_{self.config.route_mode}_destination")
            return self.config.route_mode

    def choose(self, adapter):
        from ff7r_agent.live_route_adapter import LiveRouteAdapter
        return LiveRouteAdapter._choose_admission_destination(adapter, None, None, 1)

    def test_falls_back_to_the_next_ordinary_mode(self):
        from ff7r_agent.live_route_adapter import LiveAdapterConfig
        config = LiveAdapterConfig(admission_route_fallback=("ordinary_multi_polygon", "ordinary_multi_tile"))
        adapter = self.Adapter(config, {"same_polygon", "ordinary_multi_polygon"})
        self.assertEqual(self.choose(adapter), "ordinary_multi_tile")
        self.assertEqual(adapter.config.route_mode, "ordinary_multi_tile")
        self.assertEqual(adapter.timeline[0]["event"], "admission_route_mode_fallback")

    def test_without_fallback_the_first_error_stands(self):
        from ff7r_agent.live_route_adapter import LiveAdapterConfig
        adapter = self.Adapter(LiveAdapterConfig(), {"same_polygon"})
        with self.assertRaisesRegex(AdapterError, "no_same_polygon_destination"):
            self.choose(adapter)
        self.assertEqual(adapter.tried, ["same_polygon"])

    def test_all_modes_failing_restores_the_config(self):
        from ff7r_agent.live_route_adapter import LiveAdapterConfig
        config = LiveAdapterConfig(admission_route_fallback=("ordinary_multi_polygon",))
        adapter = self.Adapter(config, {"same_polygon", "ordinary_multi_polygon"})
        with self.assertRaisesRegex(AdapterError, "no_ordinary_multi_polygon_destination"):
            self.choose(adapter)
        self.assertIs(adapter.config, config)
        self.assertEqual([f["route_mode"] for f in adapter.timeline[-1]["failed_modes"]],
                         ["same_polygon", "ordinary_multi_polygon"])

    def test_fallback_modes_are_route_modes(self):
        from ff7r_agent.live_route_adapter import LiveAdapterConfig
        LiveAdapterConfig(admission_route_fallback=("cached_topology",))
        with self.assertRaises(ValueError):
            LiveAdapterConfig(admission_route_fallback=("teleport",))
        with self.assertRaises(ValueError):
            LiveAdapterConfig(admission_route_fallback=["ordinary_multi_tile"])


class LoadSpeedTests(unittest.TestCase):
    def test_fingerprint_is_computed_once_per_artifact(self):
        from ff7r_agent.topology_cache import cached_static_fingerprint
        with tempfile.TemporaryDirectory() as directory:
            calls = []
            compute = lambda: calls.append(1) or "A" * 64
            self.assertEqual(cached_static_fingerprint(directory, "b" * 64, compute), "A" * 64)
            self.assertEqual(cached_static_fingerprint(directory, "B" * 64, compute), "A" * 64)
            self.assertEqual(len(calls), 1)

    def test_a_tampered_manifest_fingerprint_is_still_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root, cache = Path(directory), Path(directory) / "cache"
            admit(root, cache, payload_at(0.0), "west", "2026-09-01T00:00:00Z")
            manifest_path = next(cache.glob("manifest-*.json"))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["static_topology_fingerprint"] = "C" * 64
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(AdapterError, "cache_static_fingerprint_mismatch"):
                provider(cache / "manifest.json").load_current(observation())

    def test_a_prewarmed_graph_is_reused_by_the_first_route(self):
        from unittest.mock import patch
        from ff7r_agent.detour_route import DetourTopologyGraph
        with tempfile.TemporaryDirectory() as directory:
            root, cache = Path(directory), Path(directory) / "cache"
            admit(root, cache, payload_at(50_000.0), "east", "2026-09-02T00:00:00Z")
            loaded = provider(cache / "manifest.json")
            self.assertEqual(loaded.prewarm_graph((50_000.0, 0.0))["state"], "ready")
            with patch.object(DetourTopologyGraph, "from_dict", side_effect=AssertionError("graph rebuilt")):
                result = loaded.load_current(observation(position=Vec3(50_000.0, 0.0, 0.0)))
            self.assertEqual(result.source_decision, "validated_cache_hit")

    def test_a_route_waits_for_a_graph_still_being_prewarmed(self):
        import threading
        from unittest.mock import patch
        from ff7r_agent.detour_route import DetourTopologyGraph
        build = DetourTopologyGraph.from_dict
        started, release = threading.Event(), threading.Event()
        calls = []

        def slow_build(payload):
            calls.append(1)
            started.set()
            release.wait(5)
            return build(payload)

        with tempfile.TemporaryDirectory() as directory:
            root, cache = Path(directory), Path(directory) / "cache"
            admit(root, cache, payload_at(50_000.0), "east", "2026-09-02T00:00:00Z")
            loaded = provider(cache / "manifest.json")
            with patch.object(DetourTopologyGraph, "from_dict", side_effect=slow_build):
                warm = threading.Thread(target=loaded.prewarm_graph, args=((50_000.0, 0.0),))
                warm.start()
                self.assertTrue(started.wait(5))
                self.assertTrue(loaded.graph_prewarm_pending())
                self.assertEqual(loaded.prewarm_graph((50_000.0, 0.0))["state"], "building")
                threading.Timer(0.2, release.set).start()
                result = loaded.load_current(observation(position=Vec3(50_000.0, 0.0, 0.0)))
                warm.join(5)
            self.assertEqual(result.source_decision, "validated_cache_hit")
            self.assertEqual(len(calls), 1)
            self.assertFalse(loaded.graph_prewarm_pending())
            self.assertEqual(loaded.prewarm_graph((50_000.0, 0.0))["state"], "current")

    def test_two_regions_stay_prewarmed(self):
        with tempfile.TemporaryDirectory() as directory:
            root, cache = Path(directory), Path(directory) / "cache"
            west = admit(root, cache, payload_at(0.0), "west", "2026-09-01T00:00:00Z")
            east = admit(root, cache, payload_at(50_000.0), "east", "2026-09-02T00:00:00Z")
            loaded = provider(cache / "manifest.json")
            self.assertEqual(loaded.prewarm_graph((0.0, 0.0))["state"], "ready")
            self.assertEqual(loaded.prewarm_graph((50_000.0, 0.0))["state"], "ready")
            self.assertEqual(loaded.prewarm_graph((0.0, 0.0))["state"], "ready")
            graphs = loaded.prewarmed_graphs()
            self.assertEqual(set(graphs), {west["raw_artifact_sha256"].upper(), east["raw_artifact_sha256"].upper()})

    def test_a_failed_prewarm_leaves_the_route_to_build_its_own(self):
        from unittest.mock import patch
        from ff7r_agent.detour_route import DetourTopologyGraph
        with tempfile.TemporaryDirectory() as directory:
            root, cache = Path(directory), Path(directory) / "cache"
            admit(root, cache, payload_at(50_000.0), "east", "2026-09-02T00:00:00Z")
            loaded = provider(cache / "manifest.json")
            with patch.object(DetourTopologyGraph, "from_dict", side_effect=RuntimeError("boom")):
                with self.assertRaises(RuntimeError):
                    loaded.prewarm_graph((50_000.0, 0.0))
            self.assertEqual(loaded.prewarmed_graphs(), {})
            self.assertFalse(loaded.graph_prewarm_pending())
            result = loaded.load_current(observation(position=Vec3(50_000.0, 0.0, 0.0)))
            self.assertEqual(result.source_decision, "validated_cache_hit")
