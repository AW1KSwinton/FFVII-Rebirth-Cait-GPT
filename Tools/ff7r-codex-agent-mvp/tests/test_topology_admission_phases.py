from test_topology_cache import observation, provider, write_cache


def test_cold_admission_reports_phases_but_reuse_does_not_repeat_old_timings(tmp_path):
    _, manifest, _ = write_cache(tmp_path)
    cached = provider(manifest)
    cold = cached.load_current(observation())
    phases = cold.payload_summary["python_topology_cache_admission_phases_ms"]
    assert set(phases) == {"manifest_selection", "manifest_read_hash", "raw_artifact_read_hash",
                          "identity_and_provenance", "json_decode_and_capture_proof", "static_semantic_fingerprint",
                          "planner_graph_and_spatial_index", "admission_scope", "first_point_resolution", "cache_key_binding"}
    assert all(seconds >= 0 for seconds in phases.values())
    reused = cached.load_current(observation())
    assert reused.graph is cold.graph
    assert "python_topology_cache_admission_phases_ms" not in reused.payload_summary
