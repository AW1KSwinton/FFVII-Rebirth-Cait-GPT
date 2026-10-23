# Standalone setup

## Python and simulation

Create `.venv` at the repository root and install the package as shown in the
README. Use Python 3.11 or newer. `Start-CaitGPT.ps1` selects simulation unless
the caller explicitly chooses a real backend. Local state defaults to
`%LOCALAPPDATA%\ff7r-cait-gpt`; it is never committed. The MCP server listens
only on loopback and uses port 8766, configurable with `-Port`.

Keep the source checkout and use the documented editable install. A standalone
Python wheel does not bundle the sibling gameplay catalogues, native adapter
sources or validation tools and is not the complete supported distribution.

## Real input

Install Windows input support with
`python -m pip install -e "Tools/ff7r-codex-agent-mvp[windows,dev]"`.
The optional `windows_gamepad` backend needs the `gamepad` extra and a separately
installed compatible virtual controller driver. Read the package's input code
and controller mapping before using it. No installer here silently deploys a
driver. The harness rejects missing process, wrong focus, stale telemetry,
closed control, pause/menu conflicts and latched stops as appropriate.

Before installing the optional gamepad provider, set
`$env:VGAMEPAD_SKIP_VIGEMBUS_INSTALL = 'true'`. Its upstream installer can
otherwise offer a device-driver install. Choose and verify a compatible driver
separately; do not grant an automatic installer administrator privileges merely
to run simulation or install the Python package.

Provide `-GameExecutable` and `-TelemetryFile` to `Start-CaitGPT.ps1` when
selecting a real backend. The launcher starts MCP only; it does not choose or
load a save. Open control through MCP with your actual model/effort, inspect
the active screen, then use the harness's verified async load operations.
Choose your own save slots and keep backups. The old author's slot assignments
have no authority on another installation.

## Telemetry and native builds

Use your own compatible UE4SS installation and SDK. These game instrumentation
adapters are required for live gameplay; they are separate from the excluded
modding/asset editing project. Read the component READMEs before installing.
The copied producers and default consumers use `C:\CaitGPT\Telemetry`.
Python paths can be overridden with `FF7R_TELEMETRY_FILE` and the corresponding
sidecar/cache environment options; producer paths must match. Several Lua/C++
producers retain fixed paths: edit/rebuild those copies if using another root.

For both native build scripts put CMake and Visual Studio 2022 build tools on
PATH and set `UE4SS_SOURCE`, `UEPSEUDO_SOURCE`, `IMGUI_SOURCE`,
`IMGUI_TEXT_EDIT_SOURCE`, and `UE4SS_IMPORT_LIB` to compatible local dependencies.
The base exporter accepts `-Python` for its interface extraction helper. Its
interface pin and the installed UE4SS build must match; this distribution
does not fetch SDKs, fabricate import libraries, or certify a different ABI.
Never deploy while the game is running. Install helpers retain backup and
process gates; review their explicit `-GameRoot`/path parameters first.

Generate topology caches on your own installation; they are intentionally not
shipped. Missing or incompatible topology must fail closed. Legacy map probes
and diagnostic probes are optional and must not all be enabled indiscriminately.

## Optional OBS capture and event hooks

Normal foreground game-window capture uses the native capture implementation.
The optional OBS game-source fallback needs Node.js and `npm install` at the
repository root. Set `FF7R_OBS_URL` (loopback WebSocket only) and, if required,
`FF7R_OBS_PASSWORD` in the process environment. The adapter captures only an OBS
source explicitly bound to the allowlisted game window. It does not start OBS,
record, stream, edit, upload or publish. Never commit passwords or `.env` files.

Speech, driver declarations and live campaign receipts require a compatible
external service. There is no bundled narrator, stream monitor or publisher.
Use explicit `FF7R_OBS_STATE_DIR`/event paths only when intentionally connecting
such a service. Offline simulation and normal gameplay do not require voice.
