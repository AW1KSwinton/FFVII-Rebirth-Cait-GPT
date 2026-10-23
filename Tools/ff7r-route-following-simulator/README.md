# FF7R RoutePlan Following Simulator

This is a standalone, offline execution-state model for a future Cloud route
runner. It consumes a frozen, planner-shaped `RoutePlan` and synthetic
telemetry. It does not generate routes, import `detour_route.py`, call MCP or
UE4SS, inspect process memory, launch Rebirth, or send input.

## Run a replay

From this directory:

```powershell
python .\replay.py .\fixtures\replays\perfect_traversal.json
```

The command writes a deterministic JSON event timeline to stdout. Each
telemetry update includes the active waypoint, waypoint distance, final
distance, recent best distance, elapsed time since meaningful progress, route
progress, cross-track error, desired heading, heading error, turn direction,
and a diagnostic speed band. The speed band is not a control command.

The accepted planner fields are consumed directly:

`status`, `reason_code`, `requested_start`, `requested_end`, `resolved_start`,
`resolved_end`, `start_ref`, `end_ref`, `polygon_corridor`, `portals`,
`waypoints`, `total_estimated_path_length_m`, `validation`, and
`movement_sent=false`. Extra planner metadata is preserved in the normalized
plan. The explicit traversal handoff additionally checks `traversal_segments`;
other extra metadata is not interpreted.

The fixture uses the planner’s Unreal-space convention of 100 world units per
metre. Tolerances are configurable through `SimulatorConfig` and are expressed
in metres.

Telemetry `heading` is Unreal `FRotator.Yaw`: +X is yaw 0 degrees, +Y is yaw
+90 degrees, positive yaw turns right, and negative yaw turns left. The
simulator computes desired yaw with `atan2(Y, X)` and interprets signed
desired-minus-actual yaw error using that convention. A live adapter must not
silently substitute screen, compass, or camera-heading semantics.

## State model and rules

The explicit states are `IDLE`, `ROUTE_READY`, `FOLLOWING`,
`WAYPOINT_REACHED`, `ARRIVED`, `STALLED`, `DEVIATED`, `REPLAN_REQUIRED`, and
`FAILED`. Waypoint indices only increase. Intermediate waypoints may be
advanced by safe along-segment overshoot: the actor must be beyond the
waypoint by the configured overshoot margin, remain within the cross-track
bound of the incoming segment, and continue on the immediately following
route leg. A sharp intermediate corner may also be consumed by the bounded
`adjacent_segment_crossing` proof: two consecutive fresh positions must lie
near the incoming and outgoing route legs, show bounded forward displacement,
and remain within the existing vertical envelope. This proof advances only
one adjacent waypoint and creates no lookahead target or corridor exception;
the live adapter's independent corridor/portal authorization remains
authoritative. The final waypoint is different: `ARRIVED` requires the actor
to be within `final_arrival_radius_m` of the resolved final point; any amount
of final-line overshoot outside that radius remains `FOLLOWING`. Closely
spaced and already-satisfied waypoints are advanced in one deterministic
update. A declared forward `step_over` has a separate
`commit_traversal_landing(start_index, end_index, sample)` handoff. It accepts
only that crossing's consecutive departure/landing indices, an active follower
at one of those indices, a non-regressing finite sample, and the landing
envelope. The live adapter must first prove controls, floor and containment
in the exact landing polygon. The handoff advances to the landing waypoint
and resets progress at that sample; it cannot skip an arbitrary route leg.

A stall requires both the configured elapsed-time threshold and no meaningful
distance/route-progress improvement. Movement away from the active target does
not reset the timer. Meaningful progress recovers `STALLED` to `FOLLOWING`.
Progress and deviation are anchored to the active incoming leg (or the first
outgoing leg at route start). A bounded neighborhood of nearby segments is
retained only for local diagnostics; a much later or earlier folded-back leg
cannot prove progress or hide active-leg deviation. Two consecutive materially
off-route samples transition through `DEVIATED` to `REPLAN_REQUIRED` by default.
The invalidation interface
accepts `topology_changed`, `plan_stale`, `polygon_ref_invalid`, and
`world_changed`, and transitions to `REPLAN_REQUIRED` without attempting a
replacement route. Oscillation is detected from repeated signed crossings of
the active waypoint along the route direction, recomputed from timestamped
sign samples retained inside the current `oscillation_window_s`, and is
reported as a stall. Meaningful progress that recovers `STALLED` to
`FOLLOWING` starts a new oscillation episode, clearing the previous episode's
crossing history and trigger latch.

Telemetry must have finite timestamps, finite 3D position, and a finite
heading. Timestamp regression fails closed. Invalid RoutePlans and invalid
telemetry produce `FAILED` events.

## Fixtures and tests

`fixtures/route_plan_example.json` is a complete, movement-free RoutePlan
fixture. `fixtures/replays/` contains the required synthetic scenarios:

- perfect, noisy successful, already-at-first, overshoot, stationary stall;
- wrong-direction stall, large deviation/replan, pause/recovery,
  waypoint oscillation, non-finite telemetry, external invalidation, and
  final arrival;
- exact/near final arrival, slight/large final overshoot, yaw convention,
  oscillation-window expiry, oscillation recovery, and folded-back parallel
  legs;
- a sharp-corner pass-through with pre-corner, lateral, backward, long-jump,
  and final-destination negative controls.

Run the tests from this directory:

```powershell
python -m unittest discover -s tests -v
```

The test suite loads only these frozen JSON fixtures and the standalone
package. It never starts a game process and never sends input.

## Future integration boundary

The safe future seam is an adapter owned by the navigation runtime that
converts its already-observed Cloud telemetry into the fixture telemetry
shape, calls this follower for one update, and consumes diagnostics/state
events. Route generation, route validation, navmesh queries, movement
actuation, runtime safety gates, and replan selection should remain outside
this package. Before any controlled live traversal, the adapter still needs
real telemetry field/unit validation, an explicit plan identity/version
source, live invalidation wiring, actuator-neutralization ownership, and
fail-closed integration tests with the existing safety system.
