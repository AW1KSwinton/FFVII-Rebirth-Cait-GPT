from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from navigation_validation import DEFAULT_SNAPSHOT, RoutePlanValidator, TopologyAnalyzer, _is_shipping_dynamic_link


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "real-detour-route-plan.json"


def _identity_snapshot(*, direct=True, tile_index=3, salt=1, link_tile=None, dynamic_side=None, dynamic_edge=None, dynamic_bmax=None) -> dict:
    links = []
    if link_tile is not None:
        edge = 0 if dynamic_side is None else (0xFF if dynamic_edge is None else dynamic_edge)
        bmax = 255 if dynamic_bmax is None and edge == 0xFF else (0 if dynamic_bmax is None else dynamic_bmax)
        links.append({
            "ref": (salt << 45) | (link_tile << 31),
            "edge": edge,
            "next": 0xFFFFFFFF,
            "side": 128 if dynamic_side is None else dynamic_side,
            "bmin": 0,
            "bmax": bmax,
        })
    tile = {
        "x": 0, "y": 0, "layer": 0,
        "vertices": [
            {"x": 0, "y": 0, "z": 0}, {"x": 100, "y": 0, "z": 0},
            {"x": 100, "y": 100, "z": 0}, {"x": 0, "y": 100, "z": 0},
        ],
        "polygons": [{"index": 0, "vertex_indices": [0, 1, 2, 3], "type": 0, "links": links}],
    }
    if direct:
        tile.update({"tile_index_valid": True, "tile_index": tile_index, "salt": salt})
    return {"source": "ue4ss_native_detour_geometry_dump", "movement_sent": False, "coordinate_space": "recast", "tiles": [tile]}


class RoutePlanValidatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not DEFAULT_SNAPSHOT.exists():
            raise unittest.SkipTest("Optional raw native topology snapshot is not distributed")
        cls.analyzer = TopologyAnalyzer(DEFAULT_SNAPSHOT)
        cls.validator = RoutePlanValidator(cls.analyzer)
        cls.fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def _valid_plan(self) -> dict:
        return copy.deepcopy(self.fixture)

    def _assert_rejected(self, plan: dict, needle: str) -> None:
        result = self.validator.validate(plan)
        self.assertFalse(result["valid"], result)
        self.assertTrue(any(needle in error for error in result["errors"]), result)

    def test_real_detour_route_plan_to_dict_shape_is_accepted(self) -> None:
        result = self.validator.validate(self._valid_plan())
        self.assertTrue(result["valid"], result)
        self.assertEqual(result["checked_status"], "complete")
        self.assertEqual(len(self.fixture["polygon_corridor"]), 14)
        self.assertEqual(self.fixture["validation"]["tile_crossings"], 9)

    def test_polygon_corridor_and_complete_status_are_primary_contract(self) -> None:
        plan = self._valid_plan()
        self.assertIn("polygon_corridor", plan)
        self.assertNotIn("corridor_polygon_refs", plan)
        self.assertEqual(plan["status"], "complete")
        self.assertTrue(self.validator.validate(plan)["valid"])

    def test_failed_route_plan_status_is_schema_compatible(self) -> None:
        plan = self._valid_plan()
        plan.update({"status": "failed", "reason_code": "end_polygon_unresolved", "resolved_start": None, "resolved_end": None, "start_ref": None, "end_ref": None, "polygon_corridor": [], "portals": [], "waypoints": [], "total_estimated_path_length_m": None})
        plan["start_resolution"]["selected_ref"] = None
        plan["end_resolution"]["selected_ref"] = None
        plan["validation"] = {}
        result = self.validator.validate(plan)
        self.assertTrue(result["valid"], result)

    def test_endpoint_sentinels_are_accepted(self) -> None:
        plan = self._valid_plan()
        self.assertEqual(plan["portals"][0]["from_ref"], plan["start_ref"])
        self.assertEqual(plan["portals"][-1]["to_ref"], plan["end_ref"])
        self.assertTrue(self.validator.validate(plan)["valid"])

    def test_reverse_only_adjacency_is_rejected(self) -> None:
        pair = next(
            (source, target)
            for source, targets in self.analyzer.strict_adjacency.items()
            for target in targets
            if source not in self.analyzer.strict_adjacency.get(target, set())
        )
        plan = self._valid_plan()
        plan["polygon_corridor"] = [pair[1], pair[0]]
        plan["start_ref"], plan["end_ref"] = pair[1], pair[0]
        self._assert_rejected(plan, "broken directed adjacency")

    def test_neighbour_only_no_link_transition_is_rejected(self) -> None:
        pair = None
        for left, right in self.analyzer.edges:
            evidence = self.analyzer.edges[(left, right)]
            left_ref = self.analyzer.nodes[left].get("reference")
            right_ref = self.analyzer.nodes[right].get("reference")
            if "same_tile_neighbor" in evidence and isinstance(left_ref, int) and isinstance(right_ref, int):
                if right_ref not in self.analyzer.strict_adjacency.get(left_ref, set()) and left_ref not in self.analyzer.strict_adjacency.get(right_ref, set()):
                    pair = (left_ref, right_ref)
                    break
        self.assertIsNotNone(pair, "snapshot must contain a neighbour-only pair for this negative test")
        plan = self._valid_plan()
        plan["polygon_corridor"] = [pair[0], pair[1]]
        plan["start_ref"], plan["end_ref"] = pair
        self._assert_rejected(plan, "broken directed adjacency")

    def test_wrong_link_ref_is_rejected(self) -> None:
        plan = self._valid_plan()
        plan["portals"][1]["link_ref"] += 1
        self._assert_rejected(plan, "link_ref expected")

    def test_wrong_source_edge_is_rejected(self) -> None:
        plan = self._valid_plan()
        plan["portals"][1]["source_edge"] += 1
        self._assert_rejected(plan, "source_edge expected")

    def test_incorrect_bmin_bmax_derived_portal_is_rejected(self) -> None:
        plan = self._valid_plan()
        clipped_index = next(index for index, portal in enumerate(plan["portals"]) if portal.get("side") in {0, 2, 4, 6} and index not in {0, len(plan["portals"]) - 1})
        plan["portals"][clipped_index]["left"]["x"] += 100.0
        self._assert_rejected(plan, "portal interval disagrees with raw source-edge geometry")

    def test_malformed_endpoint_sentinel_is_rejected(self) -> None:
        plan = self._valid_plan()
        plan["portals"][0]["to_ref"] = plan["start_ref"] + 1
        self._assert_rejected(plan, "endpoint sentinel 0 has incorrect to_ref")

    def test_nested_tile_crossing_count_is_exact(self) -> None:
        plan = self._valid_plan()
        plan["validation"]["tile_crossings"] += 1
        self._assert_rejected(plan, "incorrect validation.tile_crossings")

    def test_metre_centimetre_route_length_mismatch_is_rejected(self) -> None:
        plan = self._valid_plan()
        plan["total_estimated_path_length_m"] *= 100.0
        self._assert_rejected(plan, "route length mismatch in metres")

    def test_reference_only_geometry_is_rejected_for_strict_success(self) -> None:
        reference = next(reference for reference in self.validator.known_refs if reference not in self.validator.strict_refs)
        plan = self._valid_plan()
        plan["polygon_corridor"][0] = reference
        plan["start_ref"] = reference
        plan["portals"][0]["from_ref"] = reference
        plan["portals"][0]["to_ref"] = reference
        plan["portals"][0]["link_ref"] = reference
        self._assert_rejected(plan, "lacks strict serialized geometry")

    def test_movement_sent_true_is_rejected(self) -> None:
        plan = self._valid_plan()
        plan["movement_sent"] = True
        self._assert_rejected(plan, "movement_sent must be false")

    def test_nonexistent_polygon_ref_is_rejected(self) -> None:
        plan = self._valid_plan()
        plan["polygon_corridor"][0] = 123456789012345
        plan["start_ref"] = 123456789012345
        self._assert_rejected(plan, "nonexistent polygon ref")

    def test_reordered_corridor_is_rejected(self) -> None:
        plan = self._valid_plan()
        plan["polygon_corridor"] = list(reversed(plan["polygon_corridor"]))
        self._assert_rejected(plan, "start_ref must equal first polygon_corridor ref")

    def test_nan_waypoint_is_rejected(self) -> None:
        plan = self._valid_plan()
        plan["waypoints"][0] = {"x": float("nan"), "y": 0.0, "z": 0.0}
        self._assert_rejected(plan, "non-finite waypoint")

    def test_incorrect_endpoint_is_rejected(self) -> None:
        plan = self._valid_plan()
        plan["resolved_start"]["x"] += 1000.0
        self._assert_rejected(plan, "resolved_start exceeds requested endpoint tolerance")

    def test_pathological_loop_is_rejected(self) -> None:
        plan = self._valid_plan()
        first, second = plan["polygon_corridor"][:2]
        plan["polygon_corridor"] = [first, second, first, second]
        plan["start_ref"], plan["end_ref"] = first, second
        self._assert_rejected(plan, "pathological oscillation")


class DirectIdentityAndDynamicLinkTests(unittest.TestCase):
    def analyze(self, payload: dict) -> TopologyAnalyzer:
        handle = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8")
        try:
            json.dump(payload, handle)
            handle.close()
            return TopologyAnalyzer(Path(handle.name))
        finally:
            Path(handle.name).unlink(missing_ok=True)

    def test_valid_direct_identity_is_native_pool(self) -> None:
        analyzer = self.analyze(_identity_snapshot(direct=True))
        node = next(node for node in analyzer.nodes.values() if node.get("kind") == "serialized_polygon")
        self.assertEqual(node["identity_source"], "native_pool")
        self.assertEqual(node["tile_index"], 3)

    def test_matching_direct_and_link_identity_is_accepted(self) -> None:
        analyzer = self.analyze(_identity_snapshot(direct=True, link_tile=3))
        node = next(node for node in analyzer.nodes.values() if node.get("kind") == "serialized_polygon")
        self.assertIsInstance(node["reference"], int)
        self.assertEqual(analyzer.identity_diagnostics.get("disagreements", 0), 0)

    def test_disagreement_and_invalid_direct_identity_are_rejected(self) -> None:
        disagreement = self.analyze(_identity_snapshot(direct=True, link_tile=4))
        node = next(node for node in disagreement.nodes.values() if node.get("kind") == "serialized_polygon")
        self.assertIsNone(node["reference"])
        self.assertEqual(disagreement.identity_diagnostics["disagreements"], 1)
        invalid = self.analyze(_identity_snapshot(direct=True, tile_index=1 << 14, link_tile=3))
        invalid_node = next(node for node in invalid.nodes.values() if node.get("kind") == "serialized_polygon")
        self.assertIsNone(invalid_node["reference"])
        self.assertEqual(invalid.identity_diagnostics["invalid_direct"], 1)

    def test_historical_fallback_and_unresolved_identity(self) -> None:
        fallback = self.analyze(_identity_snapshot(direct=False, link_tile=3))
        fallback_node = next(node for node in fallback.nodes.values() if node.get("kind") == "serialized_polygon")
        self.assertEqual(fallback_node["identity_source"], "inferred_internal_link")
        unresolved = self.analyze(_identity_snapshot(direct=False))
        unresolved_node = next(node for node in unresolved.nodes.values() if node.get("kind") == "serialized_polygon")
        self.assertEqual(unresolved_node["identity_source"], "unresolved")
        self.assertIsNone(unresolved_node["reference"])

    def test_shipping_dynamic_links_are_valid_but_excluded_from_adjacency(self) -> None:
        for side in (0x50, 0xD0):
            self.assertTrue(_is_shipping_dynamic_link(0xFF, side))
            analyzer = self.analyze(_identity_snapshot(direct=True, link_tile=3, dynamic_side=side))
            self.assertEqual(analyzer.dynamic_links["valid"], 1)
            self.assertEqual(analyzer.dynamic_links["route_semantics_unresolved"], 1)
            self.assertEqual(len(analyzer.edges), 0)
        self.assertFalse(_is_shipping_dynamic_link(0xFF, 0x20))
        self.assertFalse(_is_shipping_dynamic_link(1, 0x50))

    def test_attempt027_edge_zero_and_edge_one_are_valid_unresolved(self) -> None:
        for edge, side in ((1, 0x60), (0, 0xF0), (0, 0xD0)):
            self.assertTrue(_is_shipping_dynamic_link(edge, side, 0, 0))
            analyzer = self.analyze(
                _identity_snapshot(
                    direct=True,
                    link_tile=3,
                    dynamic_edge=edge,
                    dynamic_side=side,
                    dynamic_bmax=0,
                )
            )
            self.assertEqual(analyzer.dynamic_links["valid"], 1)
            self.assertEqual(analyzer.dynamic_links["route_semantics_unresolved"], 1)
            self.assertEqual(len(analyzer.edges), 0)

    def test_malformed_edge_zero_and_edge_one_are_not_admitted(self) -> None:
        self.assertFalse(_is_shipping_dynamic_link(0, 0xD0, 1, 0))
        self.assertFalse(_is_shipping_dynamic_link(1, 0x60, 0, 1))
        analyzer = self.analyze(
            _identity_snapshot(
                direct=True,
                link_tile=3,
                dynamic_edge=0,
                dynamic_side=0xD0,
                dynamic_bmax=1,
            )
        )
        self.assertEqual(analyzer.dynamic_links["invalid"], 1)
        self.assertEqual(len(analyzer.edges), 0)


if __name__ == "__main__":
    unittest.main()
