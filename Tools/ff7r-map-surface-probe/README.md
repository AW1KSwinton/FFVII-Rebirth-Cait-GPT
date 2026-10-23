> Superseded by the on-demand `map_query.lua` in
> `Tools/ff7r-telemetry-exporter/Base/Scripts`, which uses the game's real map
> classes (`AreaMap_C`, `UEndNaviMap`, `Icon_NaviMap_C`). Most class names this
> probe scans for (`MapMarker_C`, `MapWidget_C`, ...) are not in the header dump
> or `DefaultEndMenu.ini`.

# FF7R live map UI probe

This is a deliberately low-rate, read-only UE4SS Lua probe. It waits for a
paused map menu with a live, advancing telemetry sequence and non-empty map identity,
performs bounded reads from an allow-list of map page and marker classes, and records candidate evidence in:

`C:\CaitGPT\Telemetry\map-surface-probe.txt`

When a visible page and marker fields remain stable across consecutive samples,
it publishes the validated `map-ui.json` sidecar. It performs no work while
the game is in normal gameplay. Use `Install-MapSurfaceProbe.ps1` for a
controlled validation and `Disable-MapSurfaceProbe.ps1` to remove it; both
preserve a timestamped runtime backup. Its gate follows the base exporter’s
live/running/pause bits and requires two advancing samples with a map identity
before any UE4SS object scan. Telemetry file reads stay on the async loop;
`ExecuteInGameThread` receives only the validated sequence/map identity and
performs the bounded UE4SS read. This prevents stale startup state or file I/O
from reaching the game-thread callback.

When the probe resolves a visible map page for two consecutive samples, it also
writes `C:\CaitGPT\Telemetry\map-ui.json`. The sidecar contains the
current page, visible marker records, icon/subicon fields, target identity,
optional map/world positions, and exact `base_sequence` provenance. The Python
agent accepts it only when the sequence, map identity, finite values, and
stability proof are valid. An unresolved or stale probe remains unavailable
and cannot authorize movement or interaction.
