"""Walk across a traversal gimmick and keep where it lands (explore mode).

A passage can set the character down on floor the navmesh does not cover:
live at Kalm (2026-10-14) the crawl space ends on a two-polygon pad, and
Cloud walks on about 10 m under the low ceiling, guided by its walls, to
the inn cellar's floor.  A planned route cannot use such a passage until a
live crossing has shown where it leads.

``explore_goto(gimmick=...)`` walks to the gimmick's nearer departure, then
this holds the stick along the gimmick (departure to far end, then on in
that heading) through the action, until the character stands on navmesh
that is neither the departure's island nor a pad, and records that landing
with the evidence (``gimmick_links.record_observed_landing``).  Routes plan
across it from then on.

The push is one renewable held stick, never pulses: releasing it can stop
the action.  It stops, neutral, on anything that ends field control (a menu,
combat, dialogue, a lost feed or the control session), on no progress, or
on its time limit.
"""

from __future__ import annotations

import math
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Callable, Mapping

from .gimmick_links import DIRECTION_FORWARD, DIRECTION_REVERSE, TraversalGimmick, record_observed_landing
from .models import Vec3

# Each held push is a broker lease, at most MAX_SUPERVISED_LEASE_SECONDS long
# (pursuit holds 0.45 s and renews on every sample).
PROBE_HOLD_SECONDS = 0.45
PROBE_POLL_SECONDS = 0.1
PROBE_TIMEOUT_SECONDS = 60.0
PROBE_STALL_SECONDS = 5.0
PROBE_MIN_PROGRESS_M = 0.35
# An island no bigger than this is the action's own pad, not where it leads.
PAD_MAX_POLYGONS = 4
# Walk onto the departure before holding along the gimmick: its trigger is
# entered there, not from a metre to one side.
ENGAGE_RADIUS_M = 0.5
LANDING_SNAP_M = 0.5
LANDING_VERTICAL_M = 1.0
STOP_FLAGS = ("paused", "in_menu", "in_combat", "loading", "cutscene")


def find_gimmick(gimmicks: list[TraversalGimmick], name: str) -> TraversalGimmick:
    """The one gimmick whose id is or ends with ``name`` (``obl1110_Crouched_Basement``)."""
    matches = [item for item in gimmicks if item.gimmick_id == name or item.gimmick_id.endswith(name)]
    if len(matches) != 1:
        raise ValueError(f"gimmick {name!r} names {len(matches)} scanned gimmicks; scan_traversal_gimmicks first "
                         "or give its full id")
    return matches[0]


def nearer_direction(gimmick: TraversalGimmick, position: Vec3) -> str:
    """Cross from whichever end the player is nearer."""
    def distance(point: Vec3) -> float:
        return math.hypot(point.x - position.x, point.y - position.y) + abs(point.z - position.z)
    return DIRECTION_FORWARD if distance(gimmick.first) <= distance(gimmick.second) else DIRECTION_REVERSE


def far_end(gimmick: TraversalGimmick, direction: str) -> Vec3:
    return gimmick.second if direction == DIRECTION_FORWARD else gimmick.first


def probe_gimmick(
    service: Any,
    graph: Any,
    gimmick: TraversalGimmick,
    direction: str,
    evidence_file: str | Path,
    *,
    pose: Callable[[Mapping[str, Any]], tuple[Vec3, float | None, str]],
    camera_yaw: Callable[[Mapping[str, Any]], float | None],
    lease_active: Callable[[], bool],
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Hold across ``gimmick`` from ``direction``'s departure; record the landing."""
    from .live_route_adapter import world_heading_to_camera_stick

    departure, _ = gimmick.ends(direction)
    beyond = far_end(gimmick, direction)
    heading = math.degrees(math.atan2(beyond.y - departure.y, beyond.x - departure.x))
    labels = graph.ground_islands()
    sizes: dict[int, int] = {}
    for island in labels.values():
        sizes[island] = sizes.get(island, 0) + 1
    start_resolution = graph.resolve_point_if_resolvable(departure, max_snap_distance_m=2.0,
                                                         max_vertical_separation_m=1.5)
    departure_island = labels.get(start_resolution.selected_reference) if start_resolution.ok else None
    stream_id = "gimmick-probe-" + uuid.uuid4().hex
    started = clock()
    window: deque[tuple[float, Vec3]] = deque()
    samples: list[dict[str, Any]] = []
    engaged = False
    last_sequence: int | None = None
    report: dict[str, Any] = {"gimmick_id": gimmick.gimmick_id, "kind": gimmick.kind, "direction": direction,
                              "heading_degrees": round(heading, 1), "departure_island": departure_island,
                              "pushes": 0}

    def finish(outcome: str, **detail: Any) -> dict[str, Any]:
        report.update(outcome=outcome, elapsed_seconds=round(clock() - started, 2), samples=samples[-12:], **detail)
        return report

    try:
        while True:
            now = clock()
            state = service.observe()
            sequence = int(state.get("sequence") or 0)
            if last_sequence is not None and sequence <= last_sequence:
                # The broker renews a hold only on a newer sample; the
                # current lease carries until the next one.
                sleep(PROBE_POLL_SECONDS / 2)
                continue
            last_sequence = sequence
            stopped = [flag for flag in STOP_FLAGS if state.get(flag)]
            if stopped:
                return finish("stopped", reason=stopped[0])
            if state.get("running") is False or (state.get("telemetry") or {}).get("live") is False:
                return finish("stopped", reason="field_telemetry_unavailable")
            if not lease_active():
                return finish("stopped", reason="control_session_closed")
            if not isinstance((state.get("player") or {}).get("position"), dict):
                # Live, Nibelheim water tank: a scene took over at the top of
                # the ladder and the feed had no player for a moment.
                return finish("stopped", reason="field_telemetry_unavailable")
            feet, _, _ = pose(state)
            grounded = ((state.get("player") or {}).get("locomotion") or {}).get("grounded")
            resolution = graph.resolve_point_if_resolvable(feet, max_snap_distance_m=LANDING_SNAP_M,
                                                           max_vertical_separation_m=LANDING_VERTICAL_M)
            island = labels.get(resolution.selected_reference) if resolution.ok else None
            samples.append({"t": round(now - started, 2), "x": round(feet.x), "y": round(feet.y), "z": round(feet.z),
                            "grounded": grounded, "island": island})
            if (grounded is True and island is not None and island != departure_island
                    and sizes.get(island, 0) > PAD_MAX_POLYGONS):
                landing = resolution.closest_point
                recorded = record_observed_landing(evidence_file, gimmick.gimmick_id, direction, landing)
                return finish("landed", landing={"x": landing.x, "y": landing.y, "z": landing.z},
                              landing_island=island, recorded=recorded)
            if now - started > PROBE_TIMEOUT_SECONDS:
                return finish("stopped", reason="probe_timeout")
            window.append((now, feet))
            while len(window) > 1 and now - window[1][0] >= PROBE_STALL_SECONDS:
                window.popleft()
            first_t, first = window[0]
            # Progress counts in 3D: a ladder moves the character up or down, not across (live 2026-10-22,
            # Mt. Nibel reactor: a 17 m ladder stopped 5 m down as "no progress" and left Cloud on it).
            if now - first_t >= PROBE_STALL_SECONDS and math.dist((feet.x, feet.y, feet.z), (first.x, first.y, first.z)) / 100.0 < PROBE_MIN_PROGRESS_M:
                return finish("stopped", reason="probe_no_progress")
            if not engaged and (grounded is False or math.hypot(
                    feet.x - departure.x, feet.y - departure.y) / 100.0 <= ENGAGE_RADIUS_M):
                engaged = True
                report["engaged_at_seconds"] = round(now - started, 2)
            aim = heading if engaged else math.degrees(math.atan2(departure.y - feet.y, departure.x - feet.x))
            yaw = camera_yaw(state)
            if yaw is None:
                return finish("stopped", reason="camera_yaw_unavailable")
            x, y = world_heading_to_camera_stick(aim, yaw, 1.0)
            service.input.supervised_move(x, y, deadline=clock() + PROBE_HOLD_SECONDS, stream_id=stream_id,
                                          sequence=sequence, sprint=False)
            report["pushes"] += 1
            sleep(PROBE_POLL_SECONDS)
    finally:
        service.input.release_all()
