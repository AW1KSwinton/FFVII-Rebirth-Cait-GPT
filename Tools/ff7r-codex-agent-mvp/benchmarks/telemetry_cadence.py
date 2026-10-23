"""Measure live producer gaps without capture, input, or foreground changes.

This measures telemetry delivery, not rendered FPS. Long gaps are evidence of
missing control observations; they alone cannot attribute a game-thread hitch.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import statistics
import time


def summarize(samples: list[dict], *, elapsed_seconds: float, read_errors: int) -> dict:
    gaps = [b["observed_t"] - a["observed_t"] for a, b in zip(samples, samples[1:])]
    ordered = sorted(gaps)
    def percentile(fraction):
        return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))] if ordered else None
    return {
        "metric": "telemetry_delivery_not_render_fps",
        "elapsed_seconds": elapsed_seconds,
        "samples": len(samples), "read_errors": read_errors,
        "median_gap_seconds": statistics.median(gaps) if gaps else None,
        "p95_gap_seconds": percentile(0.95), "p99_gap_seconds": percentile(0.99),
        "max_gap_seconds": max(gaps, default=None),
        "gaps_over_250ms": sum(g > 0.25 for g in gaps),
        "gaps_over_500ms": sum(g > 0.5 for g in gaps),
        "long_gaps": [{"after_sequence": a["sequence"],
                       "source_sequence": a.get("source_sequence"),
                       "at_seconds": a["observed_t"], "gap_seconds": g}
                      for a, g in zip(samples, gaps) if g > 0.25],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, default=Path(r"C:\CaitGPT\Telemetry\state.json"))
    parser.add_argument("--seconds", type=float, default=45)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.seconds <= 600:
        parser.error("seconds must be between 1 and 600")
    samples, errors, previous = [], 0, None
    started_utc = datetime.now(timezone.utc).isoformat()
    start = time.monotonic()
    while time.monotonic() - start < args.seconds:
        try:
            state = json.loads(args.path.read_text(encoding="utf-8"))
            sequence = state["sequence"]
            if sequence != previous:
                samples.append({"observed_t": time.monotonic() - start,
                    **{key: state.get(key) for key in ("sequence", "source_sequence", "timestamp_utc", "sampled_at_utc", "producer_elapsed_ms")}})
                previous = sequence
        except (OSError, ValueError, KeyError):
            errors += 1
        time.sleep(0.02)
    result = {"started_utc": started_utc, "source_path": str(args.path),
              "summary": summarize(samples, elapsed_seconds=time.monotonic() - start, read_errors=errors),
              "samples": samples}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result["summary"], indent=2))


if __name__ == "__main__":
    main()
