# FFVII navigation probes

`Scripts/main.lua` is the read-only local-clearance probe. It publishes four
camera-relative, multi-height line traces, including hit surface-normal and
slope evidence. A mostly upward normal is reported as a
`walkable_surface_candidate`; unknown normals and non-walkable normals remain
`blocking_or_unknown`. This is evidence only, not a character-capsule
walkability guarantee.

The same payload also includes a low-frequency `capsule_probe` for each
direction. It uses a 42 cm radius, 96 cm half-height character volume, lifts
the sweep by 96 cm to avoid the supporting floor winning an initial-overlap
query, and runs roughly once every two seconds to avoid the performance cost of
250 ms polling. Its scheduler uses the probe callback count rather than
exporter sequence numbers or a wall clock, so batched telemetry cannot starve
the capsule cycle. The capsule horizon is 40 m,
allowing a fresh clear sweep to
authorize one bounded long movement hold; the frequent line traces remain
short-range context. The payload uses `FHitResult.Distance` for sweep distance
and reports `corridor_confirmed: true` only when the capsule sweep has no hit.
Supporting-ground hits are explicitly not treated as corridor confirmation.
When the UE4SS capsule binding is unavailable, the payload says so explicitly
and the consumer must remain fail-closed.

The producer also writes a bounded `navigation-health.json` status artifact.
Game-thread scheduling, capture, and output-write failures are isolated from
the repeating `LoopAsync` callback, which continues returning `false` so a
transient UE4SS Lua-state failure can be retried. The health artifact records
producer state, attempt/success/failure counters, the last successful state
sequence, and the last bounded failure reason. It is diagnostic only: a stale
or missing `navigation.json` remains rejected by the strict telemetry freshness
gate and never authorizes movement.

`Scripts/navmesh_path.lua` is a separate source-only global-path producer. It
reads a target from `navigation-target.json`, calls Unreal's reflected
`FindPathToLocationSynchronously`, and publishes only complete `PathPoints`.
It is disabled by default and has not been runtime validated. A missing or
partial path removes its output; it never falls back to a direct target vector.

The same Lua file contains an optional AI-path observation diagnostic. Its
ordinary callback does not enumerate `EndCharacter` or AI-controller objects.
To opt in, create `C:\CaitGPT\Telemetry\navigation-ai-path-observation.enable`
with the exact ASCII bytes `FF7R_AI_PATH_OBSERVATION_ENABLE_V1` and no newline.
The callback reads at most 65 bytes and ignores missing, empty, oversized, or
invalid markers. The diagnostic remains read-only, runs for at most 600
two-second attempts, and does not restart after success or the cap while the
marker remains present. Removing or invalidating the marker stops an active
scan on the next callback; remove it before creating a new request. Its
observation output never authorizes movement.

The reflected global-path query has its own one-shot request marker:
`C:\CaitGPT\Telemetry\navigation-path-diagnostic.enable` with the
exact ASCII bytes `FF7R_PATH_DIAGNOSTIC_ENABLE_V1` and no newline. Update
`navigation-target.json` before creating the marker. The ordinary callback
does not read a leftover target or call Unreal path APIs; a valid marker runs
one diagnostic attempt and stays latched until the marker is removed or
invalidated. On the first disabled callback, the producer removes stale
`navigation-path.json` output and writes one `disabled` status. Removing an
active marker also removes stale output and writes one `stopped` status. Remove
and recreate the marker to request another attempt. This diagnostic is
read-only and never authorizes movement.

`Tools/ff7r-telemetry-exporter/Install-PersistentSamplers.ps1` copies this source file to
`FF7RNavmeshPathProbe/scripts/main.lua`; the installed source-only
`FF7RNavmeshPathProbe/scripts/navmesh_path.lua` is not the active callback.
`Deploy-AIPathObservationGate.ps1` is a separate, one-file guarded helper for
that active `main.lua`; without `-Apply` it only reports hashes and running
game/launcher processes and performs no writes.

The Lua callback behavior test uses the optional Lupa package. Install it in
the test environment with `python -m pip install lupa`, then run
`python -m pytest -p no:cacheprovider tests/test_navigation_probe_lua.py tests/test_navigation_probe_source.py` from
`Tools/ff7r-codex-agent-mvp`. The behavior test is skipped by the normal suite
when Lupa is not installed.

The Python consumer accepts `navmesh_path`, `walkable_path`, or an occupancy
grid as global route evidence. Local clearance still validates each movement
hold, and recent no-progress headings remain map-scoped no-go evidence.

Controlled validation must be performed with Rebirth running in the known
interactive MCP/UE4SS environment. Do not enable the global producer during a
normal benchmark until its payload and performance have been observed.

## Native diagnostic probe

`Native/src/dllmain.cpp` is a separate, read-only UE4SS C++ diagnostic. It
locates the live `EndNavigationSystem`, `EndRecastNavMesh`, Recast private
implementation, Detour object, and default query filter, and can optionally
issue one guarded native `FindPath` query when the empty marker file
`C:\\CaitGPT\\Telemetry\\navigation-native-query.enable` exists.
The target is supplied by `navigation-native-target.json`; an optional
`start_position` makes deterministic controls possible. The result is written
to `navigation-native-probe.json`, never to `navigation-path.json`.

The probe was live-validated on `EndGame` with no movement. The corrected
owner-validated implementation reports an allocated Detour mesh with
`tileWidth=990`, `tileHeight=990`, `maxTiles=12800`, `tile_lut_size=4096`, and
readable lookup/pool pointers. An earlier report of “no valid tile headers”
was not conclusive: the probe read the provisional implementation first, and
the scan guard also skipped pools larger than 4096 slots. The current source
re-reads the owner-validated object, scans the full bounded 12,800-slot pool,
and includes the source-derived `dtMeshTile` base/extension strides (`0x68`,
`0x70`, and `0x78`) plus the previously observed `0x80+` compiled variants.
A fresh live result is required before making any claim
about loaded tiles. The guarded call that was actually tested is the
two-argument `ANavigationData::FindPath`-style implementation, not
`UNavigationSystemV1::FindPathSync`; it returned
`ENavigationQueryResult::Error` for a settled same-point control with zero
path points. This does not prove that `FindPathSync` can never work, nor that
no navigation tiles are streamed in any other context, and it is not enough
evidence to enable this as a production route source. The probe remains
disabled by default and must not author movement until a complete finite bent
path is observed and independently validated.

The report also scans the source-defined `m_posLookup` table after owner
validation. It validates each non-null `dtMeshTile*` by `DNAV` magic, Detour
version, and finite positive header counts, then records a bounded sample of
tile coordinates and polygon/vertex counts. This pointer-table result is the
primary active-tile diagnostic; contiguous-pool stride results are secondary
layout evidence.

For the next extraction step, keep the explicit marker
`C:\\CaitGPT\\Telemetry\\navigation-native-geometry.enable` dormant
until settled gameplay. After fresh telemetry and a positive raw
rendered-gameplay boundary, the live adapter records the existing artifact and
probe/timing identities, creates `.enable`, then publishes the separate
`navigation-native-geometry.ready` token last. The native probe requires both
markers, consumes the readiness token after one expensive attempt, and
therefore does not retry `BuildReport` while a pending geometry marker crosses
a loading boundary. The adapter waits up to 30 seconds for a new geometry
artifact plus matching consumed-marker and timing evidence; timeout or parse
failure removes both markers. The one-shot, build-identity-gated export writes
`navigation-native-geometry.json` with source-derived tile vertices, polygon
vertex indices, raw neighbor values, link references/edge metadata, area/type
flags, and off-mesh connection endpoints. It remains separate from the
production route sidecar and sends no movement. Installation and disable
scripts remove stale geometry markers.

The separate authored-route inventory now reads the source-defined
`ULevel::Actors` array at the verified `0x98` offset across loaded levels. The
previous `ActorCluster.Actors` traversal returned only two cluster roots and
was not a meaningful inventory of route actors. It remains metadata-only and
does not issue actor movement or write `navigation-path.json`.

The inventory also recognizes `AEndNavGridActor` and reads its source-defined
`NavGrids` records (`AssetName`, grid coordinates/layer, center, and
half-height) without treating the actor as a spline. These records are
diagnostic clues for streamed/custom navigation chunks, not route commands.

The native query marker is one-shot: installation and disable remove stale
markers, the probe consumes the marker before a native call, and the call is
gated on the known Rebirth executable filename and SHA-256 build identity.

Build and installation are explicit and refuse to run while Rebirth is
active:

```powershell
.\\Tools\\ff7r-navigation-probe\\Build-NativeNavigationProbe.ps1
.\\Tools\\ff7r-navigation-probe\\Install-NativeNavigationProbe.ps1
.\\Tools\\ff7r-navigation-probe\\Disable-NativeNavigationProbe.ps1
```

The native probe depends on the external official UE4SS/engine toolchain
configured in the build script. It does not install drivers, own input, call
MCP, or author movement.
