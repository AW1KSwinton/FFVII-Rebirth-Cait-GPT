#!/usr/bin/env python3
"""Read-only live calibration recorder for the time-aware shadow planner.

This tool never sends input. The player performs each maneuver manually while
it samples the existing FFVII telemetry snapshot and writes normalized evidence.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
from typing import Any

from ff7r_agent.telemetry import JsonFileTelemetry, TelemetryError
from ff7r_agent.time_aware_calibration import (
    analyze_calibration_samples,
    sample_from_game_state,
    sample_from_mapping,
    summarize_calibration_runs,
)

DEFAULT_TELEMETRY_PATH = Path(r"C:\CaitGPT\Telemetry\state.json")
GUIDED_SCENARIOS = (
    ("straight-sprint", None, "Start from rest. Sprint straight, hold speed, then release and let Cloud slow down."),
    ("turn-30", 30.0, "Build sprint speed, make one clean ~30 degree turn, then continue straight."),
    ("turn-60", 60.0, "Build sprint speed, make one clean ~60 degree turn, then continue straight."),
    ("turn-90", 90.0, "Build sprint speed, make one clean ~90 degree turn, then continue straight."),
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _record(args: argparse.Namespace) -> int:
    telemetry = JsonFileTelemetry(args.telemetry)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    print(f"Read-only calibration: {args.scenario}")
    print("No agent movement will be sent. You control Cloud manually.")
    for remaining in range(int(args.countdown), 0, -1):
        print(f"Starting in {remaining}...", flush=True)
        time.sleep(1.0)

    samples = []
    last_sequence: int | None = None
    errors: list[str] = []
    started = time.monotonic()
    print("RECORDING", flush=True)
    while time.monotonic() - started < args.duration:
        try:
            state = telemetry.observe()
        except TelemetryError as exc:
            errors.append(str(exc))
            if len(errors) >= args.max_errors:
                raise SystemExit(f"telemetry failed repeatedly: {errors[-1]}")
            time.sleep(args.poll_seconds)
            continue
        if last_sequence is not None and int(state.sequence) <= last_sequence:
            time.sleep(args.poll_seconds)
            continue
        last_sequence = int(state.sequence)
        try:
            sample = sample_from_game_state(state, captured_monotonic=time.monotonic())
        except ValueError as exc:
            errors.append(str(exc))
            time.sleep(args.poll_seconds)
            continue
        samples.append(sample)
        time.sleep(args.poll_seconds)

    if len(samples) < 3:
        raise SystemExit("not enough advancing locomotion samples were recorded")
    normalized_start = samples[0].t
    samples = [
        type(sample)(
            sample.sequence,
            sample.t - normalized_start,
            sample.timestamp_utc,
            sample.x_cm,
            sample.y_cm,
            sample.z_cm,
            sample.yaw_degrees,
            sample.vx_cm_s,
            sample.vy_cm_s,
            sample.vz_cm_s,
            sample.grounded,
            sample.movement_mode,
        )
        for sample in samples
    ]
    summary = analyze_calibration_samples(
        samples,
        scenario=args.scenario,
        target_turn_degrees=args.target_turn_degrees,
    )
    payload: dict[str, Any] = {
        "schema_version": 1,
        "authoritative": False,
        "movement_sent": False,
        "recorded_at_utc": _utc_now(),
        "scenario": args.scenario,
        "target_turn_degrees": args.target_turn_degrees,
        "requested_duration_seconds": args.duration,
        "telemetry_path": str(args.telemetry),
        "telemetry_errors": errors,
        "samples": [sample.to_dict() for sample in samples],
        "summary": summary,
    }
    output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print("DONE")
    print(json.dumps(summary, indent=2, sort_keys=True))
    print(f"Saved: {output}")
    return 0


def _analyze(args: argparse.Namespace) -> int:
    payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
    raw_samples = payload.get("samples")
    if not isinstance(raw_samples, list):
        raise SystemExit("input does not contain a samples list")
    samples = [sample_from_mapping(item) for item in raw_samples]
    summary = analyze_calibration_samples(
        samples,
        scenario=args.scenario or str(payload.get("scenario") or "unknown"),
        target_turn_degrees=(
            args.target_turn_degrees
            if args.target_turn_degrees is not None
            else payload.get("target_turn_degrees")
        ),
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def _summarize(args: argparse.Namespace) -> int:
    directory = Path(args.directory)
    paths = sorted(path for path in directory.glob("*.json") if path.name != Path(args.output).name)
    payloads = []
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and isinstance(payload.get("summary"), dict):
            payloads.append(payload)
    if not payloads:
        raise SystemExit(f"no calibration JSON files found in {directory}")
    summary = summarize_calibration_runs(payloads)
    output = directory / args.output
    output.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))
    print(f"Saved: {output}")
    return 0


def _guided(args: argparse.Namespace) -> int:
    directory = Path(args.directory)
    directory.mkdir(parents=True, exist_ok=True)
    print("FFVII traversal-time calibration")
    print("This is read-only. The agent will not move Cloud.")
    print("Use a flat, open, safe area. Avoid combat, menus, obstacles and NPC collisions.")
    print(f"Runs per maneuver: {args.repeats}")
    for repeat in range(1, int(args.repeats) + 1):
        for scenario, target_turn, instruction in GUIDED_SCENARIOS:
            print("\n" + "=" * 72)
            print(f"Run {repeat}/{args.repeats}: {scenario}")
            print(instruction)
            print("Reposition Cloud so there is plenty of clear space before continuing.")
            input("Press Enter when ready...")
            output = directory / f"{scenario}-{repeat:02d}.json"
            record_args = argparse.Namespace(
                scenario=scenario,
                target_turn_degrees=target_turn,
                duration=float(args.duration),
                countdown=int(args.countdown),
                poll_seconds=float(args.poll_seconds),
                max_errors=int(args.max_errors),
                telemetry=Path(args.telemetry),
                output=output,
            )
            _record(record_args)

    print("\nAll requested maneuvers recorded. Building combined summary...")
    return _summarize(
        argparse.Namespace(directory=directory, output="calibration-summary.json")
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Record or summarize read-only FFVII traversal calibration telemetry.")
    sub = parser.add_subparsers(dest="command", required=True)

    record = sub.add_parser("record", help="Record one manually-driven calibration maneuver")
    record.add_argument("--scenario", required=True, choices=["straight-sprint", "turn-30", "turn-60", "turn-90"])
    record.add_argument("--target-turn-degrees", type=float, default=None)
    record.add_argument("--duration", type=float, default=10.0)
    record.add_argument("--countdown", type=int, default=3)
    record.add_argument("--poll-seconds", type=float, default=0.05)
    record.add_argument("--max-errors", type=int, default=10)
    record.add_argument("--telemetry", type=Path, default=DEFAULT_TELEMETRY_PATH)
    record.add_argument("--output", type=Path, required=True)
    record.set_defaults(func=_record)

    guided = sub.add_parser("guided", help="Interactively record the full straight/30/60/90 calibration set")
    guided.add_argument("--directory", type=Path, default=Path("calibration"))
    guided.add_argument("--repeats", type=int, default=2)
    guided.add_argument("--duration", type=float, default=10.0)
    guided.add_argument("--countdown", type=int, default=3)
    guided.add_argument("--poll-seconds", type=float, default=0.05)
    guided.add_argument("--max-errors", type=int, default=10)
    guided.add_argument("--telemetry", type=Path, default=DEFAULT_TELEMETRY_PATH)
    guided.set_defaults(func=_guided)

    analyze = sub.add_parser("analyze", help="Re-analyze one recorded calibration JSON")
    analyze.add_argument("input", type=Path)
    analyze.add_argument("--scenario", default=None)
    analyze.add_argument("--target-turn-degrees", type=float, default=None)
    analyze.set_defaults(func=_analyze)

    summarize = sub.add_parser("summarize", help="Combine all calibration JSON files in a directory")
    summarize.add_argument("--directory", type=Path, required=True)
    summarize.add_argument("--output", default="calibration-summary.json")
    summarize.set_defaults(func=_summarize)

    args = parser.parse_args()
    if getattr(args, "target_turn_degrees", None) is None and getattr(args, "scenario", "").startswith("turn-"):
        args.target_turn_degrees = float(args.scenario.split("-", 1)[1])
    if getattr(args, "duration", 1.0) <= 0.0 or getattr(args, "poll_seconds", 0.05) <= 0.0:
        parser.error("duration and poll-seconds must be positive")
    if getattr(args, "repeats", 1) <= 0:
        parser.error("repeats must be positive")
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
