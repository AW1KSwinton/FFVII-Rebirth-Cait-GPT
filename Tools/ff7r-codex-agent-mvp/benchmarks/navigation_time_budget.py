from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ff7r_agent.navigation_time_budget import summarize_navigation_time_budget


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Summarize mutually exclusive wall-clock categories from recorded FFVII navigation timelines."
    )
    parser.add_argument("results", nargs="+", type=Path, help="JSON adapter result(s) or wrappers containing attempts")
    parser.add_argument(
        "--overall-elapsed-seconds",
        type=float,
        default=None,
        help="Optional end-to-end benchmark duration; exposes time outside adapter attempt windows.",
    )
    args = parser.parse_args()

    payloads = [_load(path) for path in args.results]
    payload: Any = payloads[0] if len(payloads) == 1 else payloads
    summary = summarize_navigation_time_budget(
        payload,
        overall_elapsed_seconds=args.overall_elapsed_seconds,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
