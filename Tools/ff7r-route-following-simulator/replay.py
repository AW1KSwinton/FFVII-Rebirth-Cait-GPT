"""Command-line replay entry point for the standalone route simulator."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from route_following_simulator import SimulatorConfig, replay_file


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay synthetic RoutePlan telemetry offline.")
    parser.add_argument("replay", type=Path, help="JSON replay envelope")
    parser.add_argument("--indent", type=int, default=2, help="JSON indentation (default: 2)")
    args = parser.parse_args(argv)
    timeline = replay_file(args.replay, config=SimulatorConfig())
    json.dump(timeline, sys.stdout, indent=args.indent, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
