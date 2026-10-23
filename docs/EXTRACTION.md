# Standalone extraction

This is a source-only copy with a fresh Git history. The source project is
unchanged. The retained `Tools/` layout preserves imports, validators, probes,
tests, and sibling catalogue discovery.

The export includes the Python gameplay runtime, native/Lua game adapters,
offline validation/simulation, benchmarks, derived regression fixtures and the
three gameplay catalogues. Personal user paths, Steam account identifiers and
private repository references were replaced or removed. Test account numbers
are synthetic. Catalogue provenance retains source identities/hashes, with
local source locations removed. Evidence projections are labelled as sanitized;
the original captures and historical campaign/readiness records are not copied.

Excluded: the Unreal/modding editor project, asset tools, extraction/indexer
tools, personal agent settings, environment files, local runtime state, saves,
raw evidence, SDK/game binaries, OBS monitor, voice worker, editorial/video
production, YouTube/channel/social automation, and the original Git history.

Packaging changes: standalone simulation-first launcher, configurable native
SDK paths, relative Python discovery, environment-configured optional OBS
game-only capture, independent regional-order test fixture, and standalone CI.
Gameplay algorithms and input safety gates are retained. Lightweight production
event and speech receipt modules remain because gameplay calls depend on them;
they do not contain the external workers or publish videos. Event files contain
runtime data and must remain outside Git.

The legacy `session_startup` module is retained as an optional integration
interface. Its OBS/task service manager is not included. The standalone launcher
starts MCP only; users load/attach the game and open control through the harness.
The live Queen's Blood campaign still requires a compatible external driver and
stream receipt service. Offline tools remain usable independently.

Offline tests needing the excluded raw topology, historical readiness matrix,
game SDK/header dumps or external service manager are marked as optional and
skip when those inputs are absent. They must be run separately with operator
supplied data to claim those integration checks. No native deployment or real
game input is performed as part of this export.
