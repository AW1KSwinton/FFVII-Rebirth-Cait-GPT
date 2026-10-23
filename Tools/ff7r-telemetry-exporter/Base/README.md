# FF7R base telemetry exporter

This directory contains the authoritative producer that is deployed as the
`FF7RTelemetryExporter` UE4SS C++/Lua mod. It was reconstructed from the
external verification-task source at
`%LOCALAPPDATA%/ff7r-cait-gpt/example.json`
because the original canonical repository only contained the separate
`surroundings.json` sidecar source.

The Lua script samples the gameplay state on the game thread and writes a
complete `state.sample` handoff. The C++ publisher validates that handoff and
atomically publishes `state.json` through `state.json.tmp`, flush/close, and
`MoveFileExW(MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)`.

`timestamp_utc` and `sampled_at_utc` are the high-resolution UTC timestamp
taken by the native publisher when it has read the complete game-thread
snapshot. `source_timestamp_utc` preserves the original Lua timestamp and is
checked against the native handoff within the bounded two-second
whole-second-quantisation envelope. This prevents a native writer restart from
relabeling an old `state.sample` as a fresh snapshot; it is not the movement
freshness clock. The native handoff timestamp is taken before JSON construction
or publication, not from the file mtime and not after the final atomic replace.
published_at_utc records the native publisher's handoff into the atomic
publication operation immediately before the temporary file is written;
producer_elapsed_ms measures the native handoff-to-publication preparation
interval. The movement clock is timestamp_utc, never file mtime or
published_at_utc.

The Lua producer also writes a bounded diagnostic `telemetry-health.json`.
The paired native DLL dispatches `FF7RReadOnlyTick` on persistent Lua states.
The installed build's GameViewportClient tick establishes game-thread identity. Mod activation occurs after synchronous Lua startup,
and unload detaches the state before UE4SS destroys it. Failed reads/writes are
recorded and retried on the next period. No per-sample coroutine is allocated.
Health is diagnostic only: the native publisher still rejects unchanged or
stale `state.sample` source sequences/timestamps, so a frozen sample cannot be
republished as fresh telemetry.

The sibling `../Scripts/main.lua` remains the independent surroundings-sidecar
probe and is not the base producer.

Build with `../Build-BaseTelemetryExporter.ps1`. Deploy through
`../Install-PersistentSamplers.ps1 -NativeDll <built DLL> -ExpectedDllSha256 <hash>`
(first a dry-run, then `-Apply`). It installs the base DLL and the paired
Lua samplers (including `entity_poi.lua` and `map_query.lua`) together, after complete mod-directory backups are hash-verified.
Lua-only installation is refused. The game must be closed; binary build output
and backups stay outside Git. This persistent-sampling candidate remains
**RESEARCH ONLY — NOT CANONICAL** until controlled live acceptance is recorded.

The installed runtime is c0a8d4c, whose CppUserModBase includes an additional
on_ui_init virtual method missing from the older public template. The build
extracts the exact installed-build interface with Prepare-InstalledUe4ssInterface.py
and refuses to configure without it. The tick callback declaration comes from
its Unreal dependency b4a6e7e. Native scheduler diagnostics are published separately
as persistent-sampler-health.json and never substitute for fresh gameplay samples.

In Rebirth's observed title screen the viewport tick callback did not fire. Sampling
therefore also uses UE4SS's existing ProcessEvent dispatch mechanism, restricted to
the main thread discovered using UE4SS's own earliest-thread-creation method. Ambiguous
thread discovery refuses sampling. The optional viewport callback must agree with that
thread if it fires. This preserves the persistent-state design without relying on a
viewport callback that registration alone did not prove usable.

The continuous-navigation candidate targets a 100 ms base pose/state period.
Menu and health refreshes remain at 1 s and bench refreshes at 2 s (10/20 base
ticks); other probe slots remain 500/2000/1000 ms. Actual telemetry rate depends
on game-thread availability; missed periods are not replayed in catch-up bursts.
The 0.5 s movement freshness bound remains mandatory during game lag.

## Combat observation (offline candidate)

The offline combat candidate adds one `EndBattleAPI.IsInBattle` boolean to
the existing base tick and publishes `game.combat_state_observed` so unknown
state cannot be mistaken for the legacy `in_combat=false` default. It enables
`combat_query.lua` on entry and disables it on exit/unknown state. Outside
combat that reader checks no request files and reads no combat rosters. During
combat a full roster/vital snapshot is read only for a new explicit request.
This requires a new native build and the complete paired script deployment;
it has not been installed or accepted live. See
[combat observation](../../ff7r-codex-agent-mvp/docs/combat-observation.md).

## Character inventory diagnostic

The Lua producer has opt-in, read-only character localization modes for the
target-localization investigation. Write a bounded request to
`C:\CaitGPT\Telemetry\entity-poi.request` using the existing
five-field format:

```text
request_id|expiry_unix_seconds|*|unused|character_inventory
```

The `*` map selector allows the request to run regardless of the currently
reported level name. The persistent game-thread sampler writes alternating
`character-inventory.0.json` / `character-inventory.1.json` sidecars containing
full UObject identity, exact class, actor name, optional CharaID/spec lookups,
`K2_GetActorLocation()`, player classification, and distance from the player.
The scan is limited to `EndCharacterBase`, capped at 4096 returned objects and
1024 emitted records, and reports `complete:false` when a bound is hit. Each
request ID is a one-shot snapshot: leaving the request file active does not
rescan or rewrite the sidecar every sampler tick. Use a new request ID for a
new snapshot.

For the scan-route-verify workflow, use these modes:

```text
request_id|expiry_unix_seconds|*|unused|character_candidates
request_id|expiry_unix_seconds|*|full_uobject_name|character_target
```

`character_candidates` performs the broad snapshot with actor name, class,
UObject identity, player status, coordinates, and distance, while deferring
CharaID/spec lookups. It writes `character-candidates.0.json` /
`character-candidates.1.json`. `character_target` accepts either the exact
UObject identity returned by the candidate snapshot or an `actor_name:<name>`
selector. It writes `character-target.0.json` / `character-target.1.json`,
including rich CharaID/spec data for the selected actor and basic actors within
10 m for local verification.

These modes do not invoke navigation, interaction, map UI, spawning,
teleportation, or story state changes. Expire or remove the request after
evidence capture.

## On-demand map query

`Scripts/map_query.lua` reads the open area map only when
`C:\CaitGPT\Telemetry\map-query.request` holds a new request:

```text
request_id|expiry_unix_seconds|snapshot|heights   (or "-" for no height grid)
```

It answers once in `map-query.json` (`status` `ready`, `map_not_open`,
`navimap_unavailable`, `player_position_unavailable` or `error`) with the
NaviMap geometry, `ConvertToPosition2D` calibration pairs, cursor, player and
target widgets, every `IconWidgets` entry (geometry, textures, texts,
distance label, allow-listed properties), the `FastTravel_C` prompt and,
when asked, bounded `UEndMenuNaviMapHeightData` grids. Test with
`lua tests/map_query_regression.lua Scripts/map_query.lua`.
