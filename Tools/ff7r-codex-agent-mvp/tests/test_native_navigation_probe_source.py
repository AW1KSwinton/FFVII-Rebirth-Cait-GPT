from pathlib import Path


ROOT = Path(__file__).parents[1]
SOURCE = ROOT.parent / "ff7r-navigation-probe" / "Native" / "src" / "dllmain.cpp"
BUILD = ROOT.parent / "ff7r-navigation-probe" / "Build-NativeNavigationProbe.ps1"
INSTALL = ROOT.parent / "ff7r-navigation-probe" / "Install-NativeNavigationProbe.ps1"
DISABLE = ROOT.parent / "ff7r-navigation-probe" / "Disable-NativeNavigationProbe.ps1"


def test_chained_junon_tiles_fit_both_geometry_and_topology_proof_bounds():
    # Live Junon: 1,396 nonempty buckets held 4,653 active tiles. Both passes
    # incorrectly capped at the lookup's 4,096 buckets, dropping 557 tiles.
    import re
    source = SOURCE.read_text(encoding="utf-8")
    scan_limit = int(re.search(r"MaxLookupTilesVisited = (\d+)", source)[1])
    assert 4653 < scan_limit
    assert "MaxExportTiles = MaxLookupTilesVisited" in source
    assert "MaxTopologyTiles = MaxLookupTilesVisited" in source
    assert "total_polys + sample.poly_count > MaxTotalPolys" in source
    assert "total_vertices + sample.vert_count > MaxTotalVertices" in source


def test_native_probe_is_explicitly_read_only_and_opt_in():
    source = SOURCE.read_text(encoding="utf-8")
    assert "navigation-native-query.enable" in source
    assert "native_path_query_called" in source
    assert '"movement_sent":false' in source
    assert "FWeakObjectPtr" in source
    assert "RawPathFindingQuery" in source
    assert "RawPathFindingResult" in source
    assert "static_assert(sizeof(RawPathFindingQuery) == 0x88" in source
    assert "static_assert(sizeof(RawPathFindingResult) == 0x18" in source
    assert "static_assert(sizeof(RawNavAgentProperties) == 0x30" in source


def test_native_probe_records_recast_and_detour_diagnostics_without_authorising_routes():
    source = SOURCE.read_text(encoding="utf-8")
    for token in (
        "RecastNavMeshImplOffset",
        "scanned_impl_offset",
        "DetourNavMeshMagic",
        "tile_stride_candidates",
        "params_max_tiles",
        "params_max_polys",
        "movement_sent",
        "LevelActorsOffset",
        "authored-route-inventory.json",
        "ScanTileLookup",
        "tile_lookup_inventory",
        "m_posLookup",
        "ReadNavGridRecords",
        "EndNavGridActor",
        "nav_grids",
    ):
        assert token in source
    assert "navigation-path.json" not in source
    assert "NavigationPathPointsOffset" not in source
    assert "path_points_layout_unverified" in source
    assert "FindPathFunction" in source
    assert "void on_update() override" in source
    assert "void on_unreal_init() override" in source


def test_authored_route_inventory_uses_level_actor_array_not_only_cluster_roots():
    source = SOURCE.read_text(encoding="utf-8")
    assert "LevelActorsOffset = 0x98" in source
    assert "ReadRawTArray(level, LevelActorsOffset, actors)" in source
    assert "actor_cluster_count" in source
    assert "level_actor_array_count" in source
    assert "EndMobRouteSpline" in source
    assert "PathWay" in source


def test_spline_points_use_reflected_property_chain_and_world_space():
    source = SOURCE.read_text(encoding="utf-8")
    assert "ReadSplinePoint" in source
    assert "GetFirstProperty" in source
    assert "GetNextFieldAsProperty" in source
    assert "PointIndex" in source
    assert "CoordinateSpace" in source
    assert "WorldCoordinateSpace = 1" in source
    assert "spline_point_parameter_layout_unverified" not in source
    assert "path_way_actor_names" in source


def test_detour_inventory_is_re_read_after_owner_validated_impl_scan():
    source = SOURCE.read_text(encoding="utf-8")
    marker = "The fixed offset above is only a candidate."
    marker_index = source.index(marker)
    reread_index = source.index("detour_params_max_tiles = 0;", marker_index)
    report_index = source.index('\\"params_max_tiles\\"', reread_index)
    assert marker_index < reread_index < report_index
    assert "tile_stride_diagnostics.clear()" in source[reread_index:report_index]
    assert "native_query_api" in source
    assert "ANavigationData::FindPath-style implementation" in source


def test_native_query_is_build_bound_and_one_shot():
    source = SOURCE.read_text(encoding="utf-8")
    install = INSTALL.read_text(encoding="utf-8")
    disable = DISABLE.read_text(encoding="utf-8")
    assert "KnownGameSha256" in source
    assert "GetExecutableIdentity" in source
    assert "ConsumeOneShotMarker(EnablePath)" in source
    assert "known_executable_build_mismatch" in source
    assert "navigation-native-query.enable" in install
    assert "Remove-Item -LiteralPath $queryMarker -Force" in install
    assert "navigation-native-geometry.enable" in install
    assert "Remove-Item -LiteralPath $geometryMarker -Force" in install
    assert "Remove-Item -LiteralPath $queryMarker -Force" in disable
    assert "navigation-native-geometry.enable" in disable
    assert "Remove-Item -LiteralPath $geometryMarker -Force" in disable


def test_geometry_capture_requires_settled_readiness_token_and_is_one_attempt_per_arm():
    source = SOURCE.read_text(encoding="utf-8")
    assert "navigation-native-geometry.ready" in source
    assert "geometry_marker_present && geometry_readiness_present" in source
    assert "geometry_marker_present && !geometry_ready && !query_requested" in source
    assert "ConsumeOneShotMarker(GeometryReadyPath)" in source
    assert "geometry_extraction_and_json_serialization_us" in source
    assert "full_build_report_us" in source
    assert "navigation-native-probe-timing.json" in source


def test_native_probe_is_dormant_without_explicit_query_or_armed_geometry():
    source = SOURCE.read_text(encoding="utf-8")
    update_index = source.index("void on_update() override")
    gate_index = source.index("if (!query_requested && !geometry_armed)", update_index)
    reported_index = source.index("if (reported_)", update_index)
    build_index = source.index("report = BuildReport();", update_index)
    assert gate_index < reported_index < build_index
    assert "const bool geometry_armed = geometry_marker_present && geometry_ready;" in source[update_index:gate_index]


def test_geometry_enable_without_ready_token_returns_before_build_report():
    source = SOURCE.read_text(encoding="utf-8")
    update_index = source.index("void on_update() override")
    pending_index = source.index("if (geometry_marker_present && !geometry_ready && !query_requested)", update_index)
    build_index = source.index("report = BuildReport();", update_index)
    assert pending_index < build_index
    assert "return;" in source[pending_index:build_index]


def test_armed_geometry_consumes_readiness_and_prevents_repeated_build_report():
    source = SOURCE.read_text(encoding="utf-8")
    update_index = source.index("void on_update() override")
    consume_index = source.rfind("ConsumeOneShotMarker(GeometryReadyPath)", update_index)
    build_index = source.index("report = BuildReport();", update_index)
    reported_index = source.index("reported_ = (navigation_objects_available", update_index)
    assert build_index < consume_index < reported_index
    assert "A later readiness token is a new explicit capture arm" in source[update_index:reported_index]


def test_geometry_capture_uses_request_id_ack_and_atomic_phase_status():
    source = SOURCE.read_text(encoding="utf-8")
    for token in (
        "CaptureRequest",
        "request_id",
        "requested_utc",
        "static_topology_cache_seed",
        "GeometryStatusPath",
        "WriteCaptureStatus(\"accepted\")",
        "WriteCaptureStatus(\"build_report_started\")",
        "WriteCaptureStatus(\"geometry_extracting\")",
        "WriteCaptureStatus(\"geometry_serializing\")",
        "WriteCaptureStatus(\"geometry_written\")",
        "WriteCaptureStatus(\"probe_written\")",
        "WriteCaptureStatus(\"completed\"",
        "AddCaptureRequestMetadata",
        "MOVEFILE_WRITE_THROUGH",
        "capture_native_failed",
    ):
        assert token in source
    update_index = source.index("void on_update() override")
    accepted = source.index('WriteCaptureStatus("accepted")', update_index)
    build = source.index("const auto build_started", update_index)
    assert accepted < build
    assert source.index("WriteCaptureStatus(\"completed\"", update_index) > source.index(
        "WriteTimingReport(LastProbeTiming)", update_index
    )


def test_geometry_readiness_marker_is_cleared_by_install_and_disable():
    install = INSTALL.read_text(encoding="utf-8")
    disable = DISABLE.read_text(encoding="utf-8")
    assert "geometryReadyMarker" in install
    assert "geometryReadyMarker" in disable
    assert "stale_geometry_readiness_marker_removed" in install
    assert "geometry_readiness_marker_removed" in disable


def test_build_identity_gate_precedes_private_reads_and_consumes_markers_early():
    source = SOURCE.read_text(encoding="utf-8")
    build_index = source.index("std::string BuildReport()")
    identity_index = source.index("const auto executable_identity = GetExecutableIdentity();", build_index)
    first_private_offset_read = source.index("WorldNavigationSystemOffset", build_index)
    assert identity_index < first_private_offset_read
    assert "bool ConsumeOneShotMarker(const wchar_t* path)" in source
    assert source.index("ConsumeOneShotMarker(EnablePath)", identity_index) > identity_index
    assert source.index("ConsumeOneShotMarker(GeometryEnablePath)", identity_index) > identity_index
    assert "known_executable_build_mismatch" in source[identity_index:first_private_offset_read]


def test_native_diagnostics_wait_for_player_pose_before_private_recast_reads():
    source = SOURCE.read_text(encoding="utf-8")
    build_index = source.index("std::string BuildReport()")
    pose_gate = source.index("const bool gameplay_pose_available = ReadPlayerPosition", build_index)
    recast_read = source.index("ReadValue(main_nav_data, RecastNavMeshVersionOffset", build_index)
    assert pose_gate < recast_read
    assert "status\\\":\\\"waiting_for_gameplay" in source[pose_gate:recast_read]
    assert "reason_code\\\":\\\"player_pose_unavailable" in source[pose_gate:recast_read]


def test_detour_scan_covers_the_full_bounded_runtime_pool():
    source = SOURCE.read_text(encoding="utf-8")
    assert "detour_max_tiles <= 20000" in source
    assert "0x68, 0x70, 0x78, 0x80" in source
    assert "tile_scan_limit" in source


def test_active_tile_lookup_is_scanned_after_corrected_detour_inventory():
    source = SOURCE.read_text(encoding="utf-8")
    correction = source.index("tile_stride_diagnostics.clear()")
    lookup_scan = source.index("const auto tile_lookup_inventory", correction)
    assert correction < lookup_scan
    assert "source-defined m_posLookup" in source
    assert "pointer_entries_non_null" in source
    assert "valid_tiles" in source
    assert "DetourNavMeshVersion" in source


def test_rebirth_tile_headers_and_geometry_report_are_serialized_safely():
    source = SOURCE.read_text(encoding="utf-8")
    assert "RebirthDetourNavMeshVersion = 13" in source
    assert "sample.version != RebirthDetourNavMeshVersion" in source
    assert '<< ",\\"valid_tiles_seen\\":" << inventory.valid_tiles' in source
    assert '<< ",\\"full_lookup_chain_traversed\\":"' in source
    assert '"coordinate_space\\":\\"recast' in source
    assert "tiles_skipped_polygons_or_links" in source
    for token in (
        "polygon_read_failures",
        "polygon_first_link_read_failures",
        "polygon_vertex_array_read_failures",
        "polygon_neighbor_array_read_failures",
        "polygon_flags_read_failures",
        "polygon_vertex_count_read_failures",
        "polygon_area_type_read_failures",
        "polygon_vertex_count_invalid",
        "polygon_off_mesh_vertices_accepted",
        "polygon_vertex_index_failures",
        "link_pointer_rejections",
        "link_array_null_rejections",
        "link_index_out_of_range",
        "link_read_failures",
        "link_chain_bound_hits",
        "off_mesh_pointer_rejections",
        "off_mesh_read_failures",
        "tile_index_valid",
        "tiles_with_incomplete_link_data",
        "polygons_with_incomplete_link_data",
    ):
        assert token in source


def test_rejected_link_chains_still_fail_closed_before_tile_export():
    source = SOURCE.read_text(encoding="utf-8")
    link_rejection = source.index("bool link_chain_rejected = false;")
    tile_rejection = source.index("tile_complete = false;", link_rejection)
    link_output = source.index('tile_output << "]}";', link_rejection)
    assert link_rejection < tile_rejection < link_output
    assert "tile_has_incomplete_link_data" in source[link_rejection:tile_rejection]
    assert 'candidate_export_authorized\\":false' in source


def test_tile_lookup_walk_traverses_source_backed_next_chains_with_bounds_and_cycles():
    source = SOURCE.read_text(encoding="utf-8")
    assert "DetourNavMesh.h: dtMeshTile stores" in source
    assert "DtMeshTileNextOffset = 0x60" in source
    assert "MaxLookupChainLength = 4096" in source
    assert "MaxLookupTilesVisited = 65536" in source
    assert "TraverseTileLookup" in source
    assert "visited_tiles" in source
    assert "chain_seen" in source
    for token in (
        "non_empty_buckets",
        "unique_active_tiles",
        "chained_tiles",
        "longest_chain",
        "rejected_pointers",
        "cyclic_pointers",
        "chain_bound_hits",
        "global_bound_hits",
    ):
        assert token in source


def test_detour_geometry_parser_prepares_source_backed_polygon_and_dynamic_link_layout():
    source = SOURCE.read_text(encoding="utf-8")
    assert "DtPolyStride = 0x20" in source
    assert "DtLinkStride = 0x10" in source
    assert "ShippingPolyRecordStride = 0x248" in source
    assert "DtMeshTileDynamicLinksOOffset = 0xA0" in source
    assert "DtMeshTileDynamicFreeListOOffset = 0xB0" in source
    assert "ShippingBinaryLayoutProven = true" in source
    assert "global firstLink/next encoding" in source
    assert "No alternate dynamic-link" in source
    assert "DtChunkArrayDataOffset = 0x00" in source
    assert "DtChunkArraySizeOffset = 0x08" in source
    assert "DtChunkArrayCapacityOffset = 0x0C" in source
    assert "dynamic_link_array_read_failures" in source
    assert "dynamic_link_array_invalid_rejections" in source
    assert "dynamic_link_array_null_rejections" in source
    assert "dynamic_link_index_out_of_range" in source
    assert "dynamic_links_used" in source
    assert "dynamic_links_route_semantics_unresolved" in source
    assert "uses_dynamic_link" in source
    assert "dynamic_index = link_index" in source
    assert "dynamic_links_size" in source
    assert "candidate_a0_link_diagnostic_samples" in source
    assert "next < base_count" in source
    assert "next - base_count < dynamic_count" in source
    assert "edge == 0x00" in source
    assert "edge == 0x01" in source
    assert "valid_shipping_dynamic_edge_zero" in source
    assert "valid_shipping_dynamic_edge_one" in source
    assert "shipping_edge_zero_side_invalid" in source
    assert "shipping_dynamic_geometry_nonzero" in source
    assert "(side & 0x40) != 0" in source
    assert "FUN_140a7a2d8" in source


def test_dynamic_link_plausibility_diagnostics_are_subtyped_and_fail_closed():
    source = SOURCE.read_text(encoding="utf-8")
    for token in (
        "DecodedDynamicLinkReference",
        "DecodeDynamicLinkReference",
        "DynamicLinkPlausibilityReason",
        "reference_zero_or_invalid",
        "next_out_of_range",
        "edge_not_shipping_dynamic",
        "shipping_flag_missing",
        "candidate_a0_link_rejection_reason_counts",
        "candidate_a0_link_rejection_edge_counts",
        "candidate_a0_link_rejection_side_counts",
        "candidate_a0_link_rejection_edge_side_counts",
        "candidate_a0_link_plausibility_first_failure_tiles",
        "candidate_a0_link_plausibility_first_failure_tiles_multiple",
        "candidate_a0_link_plausibility_first_failure_max_per_tile",
        "candidate_a0_link_diagnostic_samples_scope",
        "production_first_failure_only",
        "diagnostic_scan_scope",
        "diagnostic_scan_complete",
        "diagnostic_scan_tiles",
        "diagnostic_dynamic_records_scanned",
        "diagnostic_dynamic_link_rejection_samples",
        "diagnostic_rejection_tile_failure_histogram",
        "diagnostic_dynamic_link_plausibility_failure_tiles_multiple_reasons",
        "decoded_target",
        "source_tile_index",
        "global_link_index",
        "link_storage",
        "base_count",
        "dynamic_count",
        "raw_16_hex",
    ):
        assert token in source
    # The bit-width values are streamed inside the JSON reference string. A
    # closing escaped quote before the concatenation would emit malformed JSON
    # such as `shipping_ref_"14_tile_bits...` in a real native artifact.
    assert r'shipping_ref_\""' not in source

    classifier = source.index("DynamicLinkPlausibilityReason ClassifyDynamicLinkPlausibility")
    assert source.index("reference_zero_or_invalid", classifier) < source.index(
        "next_out_of_range", classifier
    )
    assert source.index("next_out_of_range", classifier) < source.index(
        "edge == 0x00", classifier
    )
    assert source.index("edge == 0x00", classifier) < source.index(
        "edge == 0x01", classifier
    )

    # Deterministic cases mirror the deliberately ordered native predicates:
    # a bad reference or chain is never admitted by a shipping-looking byte.
    edge_one_sides = {
        *range(0x40, 0x48), 0x60, 0x61, 0x62, 0x66, 0x67, 0xC0, 0xD0, 0xE0,
    }

    def classify(reference, next_index, edge, side, bmin, bmax, base_count, dynamic_count):
        if reference == 0:
            return "reference_zero_or_invalid"
        next_ok = (
            next_index == 0xFFFFFFFF
            or next_index < base_count
            or (next_index >= base_count and next_index - base_count < dynamic_count)
        )
        if not next_ok:
            return "next_out_of_range"
        if edge == 0:
            if side not in {0xD0, 0xF0}:
                return "shipping_edge_zero_side_invalid"
            return "valid_shipping_dynamic_edge_zero" if (bmin, bmax) == (0, 0) else "shipping_dynamic_geometry_nonzero"
        if edge == 1:
            if side not in edge_one_sides:
                return "shipping_flag_missing"
            return "valid_shipping_dynamic_edge_one" if (bmin, bmax) == (0, 0) else "shipping_dynamic_geometry_nonzero"
        if edge != 0xFF:
            return "edge_not_shipping_dynamic"
        if side & 0x40 == 0:
            return "shipping_flag_missing"
        return "valid"

    assert classify(0, 0, 0xFF, 0x50, 0, 0, 10, 2) == "reference_zero_or_invalid"
    assert classify(1, 12, 0xFF, 0x50, 0, 0, 10, 2) == "next_out_of_range"
    assert classify(1, 10, 1, 0x60, 0, 0, 10, 2) == "valid_shipping_dynamic_edge_one"
    assert classify(1, 10, 0, 0xF0, 0, 0, 10, 2) == "valid_shipping_dynamic_edge_zero"
    assert classify(1, 10, 0, 0xD0, 0, 0, 10, 2) == "valid_shipping_dynamic_edge_zero"
    assert classify(1, 10, 1, 0x50, 0, 0, 10, 2) == "shipping_flag_missing"
    assert classify(1, 10, 1, 0x60, 1, 0, 10, 2) == "shipping_dynamic_geometry_nonzero"
    assert classify(1, 10, 0xFF, 0x00, 0, 0, 10, 2) == "shipping_flag_missing"
    assert classify(1, 10, 0xFF, 0x50, 0, 0, 10, 2) == "valid"


def test_attempt027_dynamic_link_fixtures_match_binary_proven_subtypes():
    fixtures = [
        {"edge": 1, "side": 0x60, "bmin": 0, "bmax": 0, "raw": "02000080a94200006700000001600000"},
        {"edge": 0, "side": 0xF0, "bmin": 0, "bmax": 0, "raw": "12000000aa420000ffffffff00f00000"},
        {"edge": 0, "side": 0xD0, "bmin": 0, "bmax": 0, "raw": "11000000aa420000ffffffff00d00000"},
    ]
    for fixture in fixtures:
        raw = bytes.fromhex(fixture["raw"])
        assert len(raw) == 16
        assert raw[12] == fixture["edge"]
        assert raw[13] == fixture["side"]
        assert raw[14] == fixture["bmin"]
        assert raw[15] == fixture["bmax"]
    assert fixtures[0]["edge"] == 1 and fixtures[0]["side"] == 0x60
    assert all(fixture["bmin"] == fixture["bmax"] == 0 for fixture in fixtures)


def test_diagnostic_scan_counts_distinct_invalid_records_beyond_production_first_failure():
    """The diagnostic walk may continue safely while export remains fail-fast."""

    base_count = 10
    dynamic_count = 3
    sentinel = 0xFFFFFFFF
    records = {
        # Both records are reachable from the first chain.  The second chain
        # repeats record 10 to prove the diagnostic count is per record, not
        # per pointer occurrence.
        10: {"next": 11, "reference": 1, "edge": 1, "side": 0x60, "bmin": 0, "bmax": 0},
        11: {"next": sentinel, "reference": 2, "edge": 0xFF, "side": 0x00},
        12: {"next": sentinel, "reference": 3, "edge": 0, "side": 0xD0, "bmin": 0, "bmax": 0},
    }
    polygon_chains = [[10], [10, 11], [12]]

    def classify(record):
        if record["reference"] == 0:
            return "reference_zero_or_invalid"
        next_index = record["next"]
        next_ok = (
            next_index == sentinel
            or next_index < base_count
            or (next_index >= base_count and next_index - base_count < dynamic_count)
        )
        if not next_ok:
            return "next_out_of_range"
        if record["edge"] == 0:
            return "valid_shipping_dynamic_edge_zero" if record.get("bmin", 0) == 0 and record.get("bmax", 0) == 0 and record["side"] in {0xD0, 0xF0} else "shipping_edge_zero_side_invalid"
        if record["edge"] == 1:
            return "valid_shipping_dynamic_edge_one" if record.get("bmin", 0) == 0 and record.get("bmax", 0) == 0 and record["side"] in {0x60} else "shipping_flag_missing"
        if record["edge"] != 0xFF:
            return "edge_not_shipping_dynamic"
        if record["side"] & 0x40 == 0:
            return "shipping_flag_missing"
        return "valid"

    seen_dynamic = set()
    accepted = {"valid", "valid_shipping_dynamic_edge_zero", "valid_shipping_dynamic_edge_one"}
    reasons = {}
    edges = {}
    sides = {}
    pairs = {}
    failures = 0
    first_failure = None
    for chain in polygon_chains:
        for global_index in chain:
            record = records[global_index]
            if first_failure is None and classify(record) not in accepted:
                first_failure = classify(record)
            if global_index in seen_dynamic:
                continue
            seen_dynamic.add(global_index)
            reason = classify(record)
            if reason in accepted:
                continue
            failures += 1
            reasons[reason] = reasons.get(reason, 0) + 1
            edges[record["edge"]] = edges.get(record["edge"], 0) + 1
            sides[record["side"]] = sides.get(record["side"], 0) + 1
            pair = (record["edge"], record["side"])
            pairs[pair] = pairs.get(pair, 0) + 1

    # Production still observes only the first failure and rejects the tile.
    assert first_failure == "shipping_flag_missing"
    # Only the malformed edge=0xff record remains rejected; the proven
    # edge=1 and edge=0 records are retained but remain route-unresolved.
    assert failures == 1
    assert reasons == {"shipping_flag_missing": 1}
    assert edges == {0xFF: 1}
    assert sides == {0x00: 1}
    assert pairs == {(0xFF, 0x00): 1}
    assert len(seen_dynamic) == 3
    assert failures == 1
    assert len(reasons) == 1


def test_endnavapi_unsafe_projection_gate_remains_disabled():
    source = (ROOT.parent / "ff7r-navigation-probe" / "Scripts" / "custom_nav_api_probe.lua").read_text(
        encoding="utf-8"
    )
    assert "LIVE_REFLECTED_PROJECTION_CALLS_ENABLED = false" in source


def test_geometry_completion_requires_full_chain_and_every_unique_tile_exported():
    source = SOURCE.read_text(encoding="utf-8")
    assert "geometry_stage_skips" in source
    assert "exported_tile_count_matches" in source


def test_shipping_block_geometry_stays_diagnostic_until_topology_is_proven():
    source = SOURCE.read_text(encoding="utf-8")
    for token in (
        "geometry_mode\\\":\\\"shipping_extended_poly_record_diagnostic",
        "candidate_export_authorized\\\":false",
        "shipping_poly_record_stride",
        "shipping_binary_layout_proven",
        "dynamic_links_o_offset",
        "dynamic_free_list_o_offset",
        "candidate_poly_range_in_payload",
        "candidate_link_range_in_payload",
        "shipping_poly_block_prefixes_link_ref_plausible",
        "shipping_poly_block_payload_bounds_invalid",
        "candidate_validation_passed",
        "candidate_layout_independently_validated = ShippingBinaryLayoutProven",
        "topology_proof",
        "BuildTopologyProof",
        "cross_tile_topology_proved",
        "no_nearby_two_hop_cross_tile_link",
    ):
        assert token in source
    assert "candidate_validation_passed && candidate_layout_independently_validated" in source
    assert "lookup_chain_incomplete" in source
    assert "geometry_stage_rejections" in source
    assert "geometry_tile_count_mismatch" in source
    assert "lookup_chain_complete" in source
    assert "exported_unique_tiles" in source
    assert "const bool geometry_complete = inventory.full_lookup_chain_traversed" in source
    assert "geometry_stage_skips == 0" in source
    assert "exported_tile_count_matches" in source


def test_native_probe_build_and_install_are_process_guarded():
    build = BUILD.read_text(encoding="utf-8")
    install = INSTALL.read_text(encoding="utf-8")
    assert "ff7rebirth_" in build
    assert "LASTEXITCODE" in build
    assert "ff7rebirth_" in install
    assert "native_query_opt_in" in install
    assert "movement_sent" in install


def test_native_query_uses_live_pawn_as_querier_when_available():
    source = SOURCE.read_text(encoding="utf-8")
    assert "player_pawn_object" in source
    assert "ControllerPawnOffset" in source
    assert "same object Unreal movement/path requests use" in source
    assert "auto* query_owner = player_pawn_object != nullptr ? player_pawn_object : world_object" in source


def test_geometry_export_remembers_readable_regions_and_guards_copies():
    source = SOURCE.read_text(encoding="utf-8")
    # A VirtualQuery per field read made the export take eleven minutes.
    assert "thread_local ReadableRegionCacheState RegionCache;" in source
    assert "ReadableRegionCache region_cache;" in source
    assert "region_cache.End();" in source
    assert "return GuardedCopy(&out, address, sizeof(T));" in source
    assert "__except (EXCEPTION_EXECUTE_HANDLER)" in source
