#!/usr/bin/env python3
"""Offline forensic breakdown of recorded navigation timing buckets.

This module only reads preserved JSON and filesystem timestamps.  It is not
imported by the live adapter and has no control-loop side effects.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
import importlib
import json
from pathlib import Path
import statistics
import sys
from typing import Any, Mapping, Sequence


UTC = timezone.utc


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def _event_time(event: Mapping[str, Any]) -> float | None:
    return _number(event.get("t", event.get("monotonic_t")))


def _unwrap_adapter(payload: Any) -> Mapping[str, Any]:
    """Find the adapter result in a saved RPC/status envelope."""
    node = payload
    while isinstance(node, Mapping):
        if isinstance(node.get("run"), Mapping) and isinstance(node["run"].get("timeline"), list):
            return node
        for key in ("response", "result"):
            child = node.get(key)
            if isinstance(child, Mapping):
                node = child
                break
        else:
            break
    raise ValueError("saved payload does not contain run.timeline")


def _adapter_started_epoch(payload: Any) -> float | None:
    node = payload
    while isinstance(node, Mapping):
        value = _number(node.get("started_at"))
        if value is not None:
            return value
        child = node.get("response") if isinstance(node.get("response"), Mapping) else node.get("result")
        if isinstance(child, Mapping):
            node = child
        else:
            break
    return None


def _timeline(adapter: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    timeline = adapter.get("run", {}).get("timeline")
    if not isinstance(timeline, list):
        raise ValueError("adapter result has no run.timeline")
    return [e for e in timeline if isinstance(e, Mapping) and _event_time(e) is not None]


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _iso_from_epoch(value: float) -> str:
    return datetime.fromtimestamp(value, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _format_seconds(value: float) -> str:
    return f"{value:.6f}"


def _anchor_offset(events: Sequence[Mapping[str, Any]]) -> float:
    anchors: list[float] = []
    for event in events:
        observation = event.get("observation")
        if not isinstance(observation, Mapping):
            continue
        monotonic = _number(observation.get("monotonic_t"))
        stamp = observation.get("timestamp_utc")
        if monotonic is None or not isinstance(stamp, str):
            continue
        try:
            anchors.append(_parse_utc(stamp).timestamp() - monotonic)
        except (TypeError, ValueError, OverflowError):
            continue
    if not anchors:
        raise ValueError("timeline contains no monotonic_t/timestamp_utc anchor")
    return statistics.median(anchors)


def _utc_for_mono(monotonic: float, offset: float) -> str:
    return _iso_from_epoch(monotonic + offset)


def _event(timeline: Sequence[Mapping[str, Any]], name: str, *, occurrence: int = 0) -> Mapping[str, Any]:
    found = [e for e in timeline if e.get("event") == name]
    if len(found) <= occurrence:
        raise ValueError(f"missing event {name!r} occurrence {occurrence}")
    return found[occurrence]


def _first_event_time(timeline: Sequence[Mapping[str, Any]], name: str) -> float:
    value = _event_time(_event(timeline, name))
    assert value is not None
    return value


def _last_event_time(timeline: Sequence[Mapping[str, Any]], name: str) -> float:
    found = [e for e in timeline if e.get("event") == name]
    if not found:
        raise ValueError(f"missing event {name!r}")
    value = _event_time(found[-1])
    assert value is not None
    return value


@dataclass(frozen=True)
class Interval:
    label: str
    start_monotonic: float
    end_monotonic: float
    classification: str
    evidence: str
    cloud_moving: str
    confidence: str = "high"

    @property
    def seconds(self) -> float:
        return max(0.0, self.end_monotonic - self.start_monotonic)


@dataclass(frozen=True)
class ReadinessEpisode:
    index: int
    start_monotonic: float
    trigger_monotonic: float
    end_monotonic: float
    duration_seconds: float
    trigger_sequence: int | None
    last_valid_sequence: int | None
    last_valid_timestamp_utc: str | None
    settled_wait_sequence: int | None
    next_fresh_sequence: int | None
    next_fresh_timestamp_utc: str | None
    actuator_neutral: bool
    trigger_game_state: str
    recovery_game_state: str
    telemetry_advancement: str
    cause: str
    outcome: str


def _observation(event: Mapping[str, Any]) -> Mapping[str, Any]:
    value = event.get("observation")
    return value if isinstance(value, Mapping) else {}


def _readiness_cause(trigger_obs: Mapping[str, Any], recovery_obs: Mapping[str, Any]) -> tuple[str, str, str]:
    health = trigger_obs.get("raw", {}).get("health", {})
    if not isinstance(health, Mapping):
        health = {}
    if health.get("telemetry_live") is False or "telemetry_not_advancing" in (trigger_obs.get("failures") or []):
        cause = "no advancing telemetry"
    elif health.get("player_spatial") is False:
        cause = "missing player spatial data"
    elif health.get("loading") or health.get("in_menu") or health.get("paused") or health.get("in_cutscene"):
        cause = "game/menu/loading state"
    elif trigger_obs.get("controllable") is False or health.get("gameplay_settled") is False:
        cause = "required fresh/consecutive settled samples after readiness invalidation"
    else:
        cause = "unknown"
    seq0 = _number(trigger_obs.get("sequence"))
    seq1 = _number(recovery_obs.get("sequence"))
    if seq0 is not None and seq1 is not None and seq1 > seq0:
        advancement = "advanced"
    elif seq0 is not None and seq1 == seq0:
        advancement = "did not advance"
    else:
        advancement = "unknown"
    trigger_state = ",".join(str(x) for x in (
        "running" if health.get("game_running") else "not-running",
        "not-paused" if not health.get("paused") else "paused",
        "not-menu" if not health.get("in_menu") else "menu",
        "loaded" if health.get("loaded_gameplay") else "not-loaded",
        "spatial" if health.get("player_spatial") else "no-spatial",
        "settled" if health.get("gameplay_settled") else "settling",
    ))
    rhealth = recovery_obs.get("raw", {}).get("health", {})
    if not isinstance(rhealth, Mapping):
        rhealth = {}
    recovery_state = ",".join(str(x) for x in (
        "running" if rhealth.get("game_running") else "not-running",
        "not-paused" if not rhealth.get("paused") else "paused",
        "not-menu" if not rhealth.get("in_menu") else "menu",
        "loaded" if rhealth.get("loaded_gameplay") else "not-loaded",
        "spatial" if rhealth.get("player_spatial") else "no-spatial",
        "settled" if rhealth.get("gameplay_settled") else "settling",
    ))
    return cause, advancement, f"trigger:{trigger_state}; recovery:{recovery_state}"


def readiness_episodes(timeline: Sequence[Mapping[str, Any]]) -> list[ReadinessEpisode]:
    pauses = [e for e in timeline if e.get("event") == "follower_paused_for_readiness"]
    triggers = [e for e in timeline if e.get("event") == "readiness_loss_triggered"]
    settled = [e for e in timeline if e.get("event") == "readiness_recovery_settled_wait_completed"]
    samples = [e for e in timeline if e.get("event") == "readiness_recovery_sample"]
    reacquired = [e for e in timeline if e.get("event") == "readiness_reacquired_route_state"]
    if not (len(pauses) == len(triggers) == len(settled) == len(samples) == len(reacquired)):
        raise ValueError("readiness event counts do not pair")
    rows: list[ReadinessEpisode] = []
    for index, (pause, trigger, wait, sample, resume) in enumerate(zip(pauses, triggers, settled, samples, reacquired), 1):
        start = _event_time(pause)
        trigger_t = _event_time(trigger)
        end = _event_time(resume)
        assert start is not None and trigger_t is not None and end is not None
        trigger_obs = _observation(trigger)
        recovery_obs = _observation(sample)
        cause, advancement, game_state = _readiness_cause(trigger_obs, recovery_obs)
        wait_sequence = _number(wait.get("result", {}).get("sequence"))
        rows.append(ReadinessEpisode(
            index=index,
            start_monotonic=start,
            trigger_monotonic=trigger_t,
            end_monotonic=end,
            duration_seconds=end - start,
            trigger_sequence=int(_number(trigger.get("trigger_sequence"))) if _number(trigger.get("trigger_sequence")) is not None else None,
            last_valid_sequence=int(_number(trigger_obs.get("sequence"))) if _number(trigger_obs.get("sequence")) is not None else None,
            last_valid_timestamp_utc=trigger_obs.get("timestamp_utc") if isinstance(trigger_obs.get("timestamp_utc"), str) else None,
            settled_wait_sequence=int(wait_sequence) if wait_sequence is not None else None,
            next_fresh_sequence=int(_number(recovery_obs.get("sequence"))) if _number(recovery_obs.get("sequence")) is not None else None,
            next_fresh_timestamp_utc=recovery_obs.get("timestamp_utc") if isinstance(recovery_obs.get("timestamp_utc"), str) else None,
            actuator_neutral=bool(recovery_obs.get("actuator_neutral")) and bool(recovery_obs.get("controllable")),
            trigger_game_state=game_state.split("; recovery:")[0].removeprefix("trigger:"),
            recovery_game_state=game_state.split("; recovery:")[-1],
            telemetry_advancement=advancement,
            cause=cause,
            outcome="recovered/resumed",
        ))
    return rows


def _load_budget_summary(adapter: Mapping[str, Any]) -> Mapping[str, Any]:
    # Reuse the checked-in offline analyzer so the comparison is exact.
    src = Path(__file__).resolve().parents[1] / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    module = importlib.import_module("ff7r_agent.navigation_time_budget")
    return module.summarize_navigation_time_budget(adapter.get("run", adapter), overall_elapsed_seconds=None)


def _wall_seconds(path: Path) -> tuple[float, float]:
    value = json.loads(path.read_text(encoding="utf-8"))
    start = _parse_utc(str(value["start_utc"])).timestamp()
    end = _parse_utc(str(value["end_utc"])).timestamp()
    return start, end


def _file_mtime(path: Path) -> float:
    return path.stat().st_mtime


def outside_intervals(adapter: Mapping[str, Any], timeline: Sequence[Mapping[str, Any]], wall_clock: Path, evidence_dir: Path, adapter_started_epoch: float | None = None) -> list[Interval]:
    offset = _anchor_offset(timeline)
    wall_start_epoch, wall_end_epoch = _wall_seconds(wall_clock)
    wall_start = wall_start_epoch - offset
    wall_end = wall_end_epoch - offset
    result_file = evidence_dir / "result.json"
    mission_start = None
    if result_file.exists():
        result = json.loads(result_file.read_text(encoding="utf-8"))
        if isinstance(result.get("started_at_utc"), str):
            mission_start = _parse_utc(result["started_at_utc"]).timestamp() - offset
    if mission_start is None:
        mission_start = wall_start
    job_start_epoch = adapter_started_epoch if adapter_started_epoch is not None else _number(adapter.get("started_at"))
    if job_start_epoch is None:
        job_start = _first_event_time(timeline, "actuator_neutralized")
    else:
        job_start = job_start_epoch - offset
    first_command = min(
        _event_time(e) - max(0.0, _number(e.get("command_wall_duration_seconds")) or 0.0)
        for e in timeline
        if e.get("event") in {"movement_pulse_completed", "movement_setpoint_issued"}
    )
    topology_decision = _first_event_time(timeline, "topology_source_decision")
    topology_selected = _first_event_time(timeline, "topology_source_selected")
    validator_bound = _first_event_time(timeline, "validator_topology_bound")
    identity_verified = _first_event_time(timeline, "route_plan_identity_verified")
    attempt_end = _load_budget_summary(adapter)["attempts"][0]["window_end_monotonic"]
    # The first interval starts at benchmark wall-clock start; mission_start and
    # job_start are only available from wrapper/result metadata, so those labels
    # are explicitly kept separate from adapter event time.
    intervals = [
        Interval("benchmark launcher/client startup", wall_start, mission_start, "unknown/likely orchestration", "wall-clock.json → result.json started_at_utc", "no"),
        Interval("mission setup RPCs and control-session setup", mission_start, job_start, "required setup plus orchestration overhead", "result.json started_at_utc and async started_at; NavigationMission.run", "no", "medium"),
        Interval("adapter preflight freshness/settled gate", job_start, topology_decision, "required safety gate", "actuator_neutralized(preflight_neutral_proof), fresh_next_telemetry_acquired, post_render_settled_gameplay_reacquired, preflight_pose_settling", "no"),
        Interval("topology cache admission/load", topology_decision, topology_selected, "likely reducible computation; safety validation required", "topology_source_decision/selected; preflight.topology.timings.python_topology_cache_load_ms", "no"),
        Interval("route plan selection and validator binding", topology_selected, validator_bound, "required route binding; small overhead", "cached_topology_route_planned, selected_route_plan_bound, validator_topology_bound", "no"),
        Interval("independent validator construction/validation", validator_bound, identity_verified, "likely removable duplicate parsing; validation invariant required", "preflight.validator_timing.python_validator_construction_ms/validation_ms", "no"),
        Interval("final pre-movement freshness/authorization gate", identity_verified, first_command, "required safety gate", "final_pre_movement_neutral_proof, fresh_next_telemetry_acquired, final_pre_movement_gate_passed", "no"),
    ]
    # Post-adapter tail is outside the adapter attempt by definition.  The
    # 2-second poll and status file timestamps let us separate the known client
    # sleep from the large final response/serialization interval.
    final_adapter_event = attempt_end
    s114 = evidence_dir / "114-get_live_route_adapter_status.json"
    s115 = evidence_dir / "115-get_live_route_adapter_status.json"
    s117 = evidence_dir / "117-close_control_session.json"
    if s114.exists() and s115.exists() and s117.exists():
        t114 = _file_mtime(s114) - offset
        t115 = _file_mtime(s115) - offset
        t117 = _file_mtime(s117) - offset
        poll_sleep_start = t114 + 2.0
        status_ready = t115
        # Git checkout/copy times are not recording timestamps. Only split
        # the tail if every timestamp is ordered inside the recorded window.
        # Otherwise preserve its measured total with an unresolved breakdown.
        tail_times_valid = (
            final_adapter_event <= t114 <= poll_sleep_start <= t115 <= t117 <= wall_end
        )
        if tail_times_valid:
            intervals.extend([
                Interval("adapter final event to status poll 114 completion", final_adapter_event, t114, "protocol/poll race; likely reducible", "final continuous_controller_finally + 114-get_live_route_adapter_status mtime", "no", "medium"),
                Interval("NavigationMission poll sleep", t114, poll_sleep_start, "intentional polling; reducible by event-driven completion", "NavigationMission.run_route_job poll_seconds=2.0", "no"),
                Interval("final status request, worker result assembly, and JSON serialization", poll_sleep_start, status_ready, "likely reducible output/orchestration overhead; exact internal split unresolved", f"115 status mtime; {s115.stat().st_size:,}-byte response; runtime_bridge._finish_async_request; NavigationMission.call json.dump", "no", "medium"),
                Interval("fail-closed stop_all_input and close_control_session", status_ready, t117, "required safety cleanup", "116-stop_all_input and 117-close_control_session", "no"),
                Interval("result/hash/console/wall-clock finalization", t117, wall_end, "benchmark artifact finalization; likely removable from mission latency", "result.json, sha256.json, mission.console.txt, wall-clock.json mtimes", "no", "medium"),
            ])
        else:
            intervals.append(Interval(
                "post-adapter polling/cleanup/finalization", final_adapter_event, wall_end,
                "mixed; file timestamps inconsistent with recorded run",
                "wall-clock.json and adapter final event; copied/checkout mtimes rejected",
                "no", "low",
            ))
    else:
        intervals.append(Interval("post-adapter polling/cleanup/finalization", final_adapter_event, wall_end, "mixed; subinterval evidence unavailable", "wall-clock.json and adapter final event", "no", "low"))
    # Keep only positive intervals and preserve chronological order.
    return [item for item in intervals if item.end_monotonic > item.start_monotonic]


def _markdown(adapter_path: Path, wall_path: Path, adapter: Mapping[str, Any], budget: Mapping[str, Any], intervals: Sequence[Interval], episodes: Sequence[ReadinessEpisode], offset: float) -> str:
    overall = _number(json.loads(wall_path.read_text(encoding="utf-8")).get("elapsed_seconds")) or 0.0
    outside = overall - float(budget.get("adapter_attempt_seconds") or 0.0)
    lines: list[str] = []
    lines.append("# Navigation overhead forensic breakdown")
    lines.append("")
    lines.append(f"- Adapter evidence: `{adapter_path}`")
    lines.append(f"- Wall-clock evidence: `{wall_path}`")
    lines.append(f"- Monotonic-to-UTC median offset: `{offset:.6f}` seconds")
    lines.append(f"- Analyzer adapter time: **{float(budget.get('adapter_attempt_seconds', 0.0)):.6f} s**")
    lines.append(f"- Analyzer outside-adapter time: **{outside:.6f} s**")
    lines.append("")
    lines.append("## Outside-adapter intervals")
    lines.append("")
    lines.append("| Interval | Start UTC | End UTC | Seconds | Classification | Cloud moving | Evidence |")
    lines.append("|---|---|---|---:|---|---|---|")
    for item in intervals:
        lines.append(f"| {item.label} | {_utc_for_mono(item.start_monotonic, offset)} | {_utc_for_mono(item.end_monotonic, offset)} | {item.seconds:.6f} | {item.classification} | {item.cloud_moving} | {item.evidence} |")
    lines.append(f"| **Total identified outside time** |  |  | **{sum(i.seconds for i in intervals):.6f}** |  |  | residual vs analyzer: **{outside - sum(i.seconds for i in intervals):.6f} s** |")
    lines.append("")
    lines.append("A detailed final-status interval is inferred only when the file mtimes are ordered within the recorded wall-clock window. Git checkout or copied timestamps outside that window leave the tail unresolved. Its internal worker/serialization split requires per-request bridge timestamps.")
    lines.append("")
    lines.append("## Readiness-recovery episodes")
    lines.append("")
    lines.append("| # | Start UTC | End UTC | Seconds | Last valid seq/time | Settled wait seq | Next fresh seq/time | Neutral | Trigger state → recovery state | Telemetry | Cause | Outcome |")
    lines.append("|---:|---|---|---:|---|---:|---|---|---|---|---|---|")
    for e in episodes:
        last = f"{e.last_valid_sequence}/{e.last_valid_timestamp_utc or '?'}"
        nxt = f"{e.next_fresh_sequence}/{e.next_fresh_timestamp_utc or '?'}"
        lines.append(f"| {e.index} | {_utc_for_mono(e.start_monotonic, offset)} | {_utc_for_mono(e.end_monotonic, offset)} | {e.duration_seconds:.6f} | {last} | {e.settled_wait_sequence if e.settled_wait_sequence is not None else '?'} | {nxt} | {'yes' if e.actuator_neutral else 'no'} | {e.trigger_game_state} → {e.recovery_game_state} | {e.telemetry_advancement} | {e.cause} | {e.outcome} |")
    lines.append(f"| **Total** |  |  | **{sum(e.duration_seconds for e in episodes):.6f}** |  |  |  |  |  |  |  | {len(episodes)} recovered |")
    lines.append("")
    advanced = sum(e.telemetry_advancement == "advanced" for e in episodes)
    neutral = sum(e.actuator_neutral for e in episodes)
    lines.append(f"The input contains {len(episodes)} paired recovered episodes: {advanced} have advancing telemetry and {neutral} have a neutral, controllable recovery sample. Causes and observed states are recorded per episode above.")
    return "\n".join(lines) + "\n"


def analyze(adapter_path: Path, wall_path: Path, evidence_dir: Path) -> dict[str, Any]:
    raw_adapter = json.loads(adapter_path.read_text(encoding="utf-8"))
    adapter = _unwrap_adapter(raw_adapter)
    adapter_started_epoch = _adapter_started_epoch(raw_adapter)
    timeline = _timeline(adapter)
    offset = _anchor_offset(timeline)
    budget = _load_budget_summary(adapter)
    intervals = outside_intervals(adapter, timeline, wall_path, evidence_dir, adapter_started_epoch)
    episodes = readiness_episodes(timeline)
    wall = json.loads(wall_path.read_text(encoding="utf-8"))
    outside = float(wall["elapsed_seconds"]) - float(budget["adapter_attempt_seconds"])
    return {
        "adapter_path": str(adapter_path),
        "wall_clock_path": str(wall_path),
        "offset_seconds": offset,
        "overall_elapsed_seconds": float(wall["elapsed_seconds"]),
        "adapter_attempt_seconds": float(budget["adapter_attempt_seconds"]),
        "outside_adapter_attempts_seconds": outside,
        "outside_intervals": [asdict(i) | {"seconds": i.seconds} for i in intervals],
        "outside_identified_seconds": sum(i.seconds for i in intervals),
        "outside_residual_seconds": outside - sum(i.seconds for i in intervals),
        "readiness_episodes": [asdict(e) for e in episodes],
        "readiness_recovery_seconds": sum(e.duration_seconds for e in episodes),
        "analyzer_durations_seconds": budget["durations_seconds"],
        "analyzer_counters": budget["counters"],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adapter", type=Path, required=True, help="saved adapter status/result JSON")
    parser.add_argument("--wall-clock", type=Path, required=True, help="saved wall-clock.json")
    parser.add_argument("--evidence-dir", type=Path, required=True, help="retry evidence directory")
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-md", type=Path, required=True)
    args = parser.parse_args(argv)
    result = analyze(args.adapter, args.wall_clock, args.evidence_dir)
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_md.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    raw_adapter = json.loads(args.adapter.read_text(encoding="utf-8"))
    adapter = _unwrap_adapter(raw_adapter)
    adapter_started_epoch = _adapter_started_epoch(raw_adapter)
    budget = _load_budget_summary(adapter)
    timeline = _timeline(adapter)
    args.output_md.write_text(_markdown(args.adapter, args.wall_clock, adapter, budget, outside_intervals(adapter, timeline, args.wall_clock, args.evidence_dir, adapter_started_epoch), readiness_episodes(timeline), result["offset_seconds"]), encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("overall_elapsed_seconds", "adapter_attempt_seconds", "outside_adapter_attempts_seconds", "outside_identified_seconds", "outside_residual_seconds", "readiness_recovery_seconds")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
