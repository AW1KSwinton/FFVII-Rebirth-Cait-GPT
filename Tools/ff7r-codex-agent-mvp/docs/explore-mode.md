# Explore mode

Free exploration with no quest objective: the Chapter 1 Nibelheim flashback,
where `get_active_objective` returns `objective_main_chapter_ambiguous` and
`open_map` opens no map; or any time you want to look around instead of
following an objective. Use it in place of bounded `move_direction` presses:
one view and one walk replace dozens of decision-and-round-trip pulses.

## The loop

1. `explore_view` (read-only, no game input) renders a top-down PNG of the
   navmesh round the player and returns numbered candidate destinations.
2. Open `image.path` with your image reader and pick: a candidate `id`, a
   world point read off the grid, or a pixel of the image.
3. `explore_goto_async` walks there with the pursuit route planner and
   follower (the one `navigation_mission --pursuit` uses), as one job in the
   service. Poll `get_explore_goto_status` with its `job_id`.
4. Look again: a new view from where you stopped, or `observe_game` /
   `capture_game_window`.

Narrate each pick as gameplay (what you see, where you head and why); the
view, the candidate list and the status are harness detail and stay out of the
voice line.

## `explore_view`

```text
agent_call("explore_view", {"radius_m": 60, "max_candidates": 8})
```

| argument | default | meaning |
| --- | --- | --- |
| `radius_m` | 60 | half-width of the square view, 10 to 250 m |
| `px_per_m` | auto | pixels per metre (about 960 px across; never over 2048 px) |
| `center_x`, `center_y` | the player | look elsewhere on the capture (centimetres) |
| `max_candidates` | 8 | far-end destinations, 1 to 20 |
| `show_islands` | true | grey/red for navmesh islands the player cannot reach |
| `include_pois` | true | known NPCs, interactables, landmarks and door links |
| `wait_seconds` | 12 | how long to wait for a graph still being built |

It returns:

- `image.path`: the PNG (under `%LOCALAPPDATA%\ff7r-agent\explore-views`, or
  `FF7R_EXPLORE_VIEW_DIR`). World **+X runs right and world +Y runs down** the
  image. Heights are colour-coded (colour bar bottom-right, metres, spanning
  the player's own floors; floors beyond that range take the end colours; other
  navmesh islands are drawn grey with red edges). The grid
  is labelled in **metres**: world X along the top edge, world Y down the left
  edge. The white dot is the player, the red arrow his facing.
- `transform`: `left_cm`, `top_cm`, `px_per_m` and the formulas
  `x = left_cm + px / px_per_m * 100`, `y = top_cm + py / px_per_m * 100`
  (centimetres, the same world coordinates as `observe_game`).
- `player`: feet position (cm), yaw, pixel, whether he stands on the captured
  navmesh (`route_start_ok`), his island and its size.
- `candidates`: numbered, drawn on the image with the same number.

  | `kind` | marker | meaning |
  | --- | --- | --- |
  | `far_end` | green ring | far end of the walkable area in view, `reachable`, with `path_m` |
  | `capture_edge` | orange ring | the captured mesh ends here; more may lie beyond |
  | `poi` / `door` | cyan / yellow diamond | known point of interest snapped to the nearest floor |
  | `other_island` | red X | nearest point of a separate island: `reachable: false` |

  Each has `position` (cm), `position_m`, `distance_m`, `bearing_deg` (world
  yaw, 0 = +X), `relative_to_facing_deg`, `height_vs_player_m` and `pixel`.
- `view_id`: the last eight views are remembered so a candidate id or a pixel
  needs no other data.

Without a covering capture it answers `topology_not_captured_here` with the
existing capture operation (`start_topology_capture`, then
`get_topology_capture_status`; minutes, no input) and you call `explore_view`
again. A graph still being built answers `topology_graph_building`; call again.
A capture covers a square of navmesh round where it was taken, so an
`capture_edge` candidate is where to capture next. `explore_view` also warms
the route validator for the area, so the first walk is faster.

## `explore_goto_async`

```text
agent_call("explore_goto_async", {"candidate": 3, "execute": true})
agent_call("explore_goto_async", {"x": 12050, "y": 54300, "execute": true})
agent_call("explore_goto_async", {"px": 412, "py": 108, "view_id": "...", "execute": true})
agent_call("get_explore_goto_status", {"job_id": "explore-17"})
```

Exactly one target form:

- `candidate` (an id from `explore_view`, optional `view_id`);
- `x`, `y` and optional `z`, in world centimetres (the `observe_game` unit);
- `x_m`, `y_m` and optional `z_m`, in metres (what the grid labels read);
- `px`, `py` of an image, with `view_id` (the last view when omitted) or the
  view's `transform`;
- `gimmick`: a scanned traversal gimmick's id or its end
  (`obl1110_Crouched_Basement`), to cross it once and learn where it leads
  (see below).

Options: `execute` (default **false**: snap, plan and run the route adapter's
dry-run preflight, sending no input), `max_snap_m` (6, up to 25),
`timeout_seconds` (300, up to 900), `max_cycles` (2400), `allow_off_island`
(false). Any other argument is refused, so a mistyped unit cannot
silently be read as centimetres.

It needs an open control session (`open_control_session` with your model and
effort) and the usual arm gating, exactly as `start_live_route_adapter`; it
never opens, closes or heartbeats the session and never latches the stop, so a
cutscene watcher keeps working. Other operations are refused while it runs
(`async_explore_in_progress`) except status, `say_on_stream` and
`stop_all_input`. While a job runs, keep polling the status and do not
interleave input.

The target is snapped onto a walkable polygon the player can reach and kept
0.5 m off its edge; the report gives `target.position`, `snap_distance_m`,
`snap_vertical_m`, `polygon_ref` and any `warnings`. If you give no height,
the player's floor wins over a stacked floor. An explicit height selects the
nearest floor at that height; an unreachable floor is refused even when a
reachable floor lies less than 3 m above or below it. Nearest reachable
suggestions also use height to break horizontal-distance ties.

The route can start only from inside a captured polygon. If the player stands
just off the floor (up to 0.75 m, within 1 m of its height; live: 0.07 m past
an eroded edge at the Dustbowl fast-travel point), an executed walk first
takes up to three short stick taps toward a point 0.4 m inside the nearest
polygon, re-reading the player after each, and reports them as `start_step`
(`aim`, `taps`, `on_navmesh`). If that does not put the player on the floor,
or a field blocker or map change appears, the walk stops before routing. A
dry run sends nothing and reports `start_step_needed` instead.

### Ladders, cracks and crawl spaces

`scan_traversal_gimmicks` (read-only) lists the loaded levels' ladders,
climbing walls, cracks and crawl spaces with both their ends, and keeps them
as evidence beside the telemetry (`TraversalGimmicks/evidence.json`). Routes
(explore, pursuit and `npc_approach`) then plan across them like the
capture's own links, from one floor to another, and walk each one as a single
held push: up to its trigger without stopping, then through the action while
the game has the controls, until Cloud stands on the far floor. Scan once in
each new area before routing there.

A passage can set Cloud down on floor the navmesh does not cover (at Kalm
the crawl space ends on a tiny pad, and he walks on about 10 m under the low
ceiling to the inn cellar). Until a crossing has been walked live, such a
passage does not join the floors. `explore_goto_async(gimmick=..., execute=true)`
walks to its nearer end, holds the stick along it until Cloud stands on
substantial navmesh beyond, and records that landing; the result is
`outcome: crossed` with `gimmick_probe.landing`. Routes use the passage from
then on.

Result (`response` of the finished job):

| `outcome` | meaning |
| --- | --- |
| `arrived` | the route arrived **and** a live sample on the same map confirms horizontal and vertical distances within 1.5 m (`final.distance_to_target_m`, `final.vertical_distance_to_target_m`) |
| `crossed` | with `gimmick`: arrived at its end, crossed it, and recorded where it landed (`gimmick_probe`) |
| `stopped` | the route did not complete, a later preflight refused after earlier movement, or the start step did not reach the floor; inspect `input_sent`, `reason` and, for executed routes, `stop_reason` |
| `refused` | nothing moved: see `reason` |
| `dry_run_ok` | `execute` was false and the route validated |

Refusal `reason`s before any input: `control_session_required`,
`start_not_on_navmesh`, `start_step_needed` (dry run only), `target_on_unreachable_island` (with
`nearest_reachable` and the island's size), `target_off_navmesh` (with
`nearest_reachable`), `no_corridor_to_target`, `explore_view_unknown`,
`candidate_unknown`, `explore_view_map_changed`, `topology_not_captured_here`, a field blocker
(`dialogue_active`, `field_telemetry_unavailable`) or the route preflight's
own reason. A target on another island is refused rather than attempted: a
door or gap may join it (see `other_island` candidates and known door links),
which this operation does not open; walk to the door and use the door and
interaction operations (see [Doors and other prompts](#doors-and-other-prompts)), or pass `allow_off_island` to let the planner decide.

`stop_reason` names why a walk stopped, using the route follower's readiness
vocabulary, decided from a fresh sample: `combat_active`, `cutscene_active`,
`dialogue_active`, `field_telemetry_unavailable`, `loading_or_streaming`,
`menu_or_pause`, `arrival_not_confirmed`, otherwise the follower's own
reason. `stop_evidence` keeps the follower's reason and the readiness
failures it recorded. On `dialogue_active` or `field_telemetry_unavailable`,
call `capture_game_window` and look before anything else, then
`finish_conversation_async` for an open conversation. After a combat stop,
fight or flee; after a cutscene stop, wait for it or `skip_cutscene`; then
look again and walk on.

The report counts movement and events across **all route attempts**. A run
already includes its preflight events; a refused attempt contributes its
preflight timeline instead. The duplicated final run and objective timeline
are never counted again. `input_sent` stays true if any earlier attempt moved,
including when the last attempt refuses before input.

`route_state`, `route_reason`, `preflight` and `stop_evidence` describe the
final attempt. `metrics`, `movement_cycles`, `timeline_scope:
"all_route_attempts"`, and `timeline_event_count` cover the whole job.
`last_events` keeps eight events with their `attempt_index`;
`last_gate_events` independently keeps eight gate events so a recovery does
not hide an earlier wait. `timeline_event_counts` keeps at most 40 event
names, prioritising gate events; `timeline_event_kinds_omitted` and
`timeline_named_events_omitted` report any truncation. No raw timelines or
per-attempt route payloads are returned. `control_session: "untouched"` and
`stop_latched: false` record that the job left both alone.

Cold graph requests join only an identical pending start/target selection.
The wait budget includes waiting for another build and then this build.
Completed requests reselect the capture next time while reusing built graphs
by artifact SHA; a remembered view from a different map is refused. Retained
positions in a stopped or stale telemetry feed do not authorize a view or walk.

## Doors and other prompts

Explore mode stops beside things; it never presses interact. When Cloud is
close to an object and facing it, `read_field_prompt` lists the in-world
prompt's key (live at Nibelheim, Cloud's front door showed nothing from
about 1.5 m and `$level_action_check` once he stood at it facing it).
Then use the operation for that prompt:

| Prompt | Key | Operation |
| --- | --- | --- |
| Talk | `$level_action_talk` | `talk_to_npc` |
| Open | `$level_action_door` | `open_door` |
| Terrain Action | `$level_sys_show_parkour_input` | `terrain_action` |
| any other interact prompt, e.g. Check | `$level_action_<word>` | `use_field_action(command="check")` |

`use_field_action` presses interact only while exactly that prompt is
shown, then watches for at most `observe_seconds` (default 4) and returns
`outcome`: `dialogue`, `menu`, `paused` (usually a tutorial tip),
`loading`, `field_feed_blank` (a scene or conversation; capture the window
before anything else), `moved` (Cloud walked somewhere and stopped),
`prompt_cleared`, or `no_change`. Only an observed outcome is evidence the
press did something.

## Limits

- Reachability follows the planner's directed portals, including off-mesh
  links permitted by its traversal policy. Short level step-over links can
  join otherwise separate islands; excluded climbs, drops and reverse links
  remain unreachable. With PR #369 present, automatic gates on a planned
  corridor use the route follower's gate wait; interactive doors still require
  their interaction. The older planner uses its existing adjacency, so neither
  #369 nor #370 is required for standalone exploration. Off-mesh fixture tests
  skip when #370 is absent; directed-portal and adjacency regressions always run.
- `path_m` is the planner's own funnelled path length (`path_source:
  "planner"`); past a 3 s budget it is an estimate through polygon centres,
  up to about a third long (`"estimate"`).
- A capture shows the navmesh, not props or collision: the route adapter
  audits the planned route against the game's collision before moving, as
  for every route.
- The image shows only the captured area and its stacked floors overlap; read
  the colour bar and `height_vs_player_m` before choosing a floor.
