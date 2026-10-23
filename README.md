# FFVII-Rebirth-Cait-GPT

A local, bounded game-playing harness for FINAL FANTASY VII REBIRTH on Windows.
Connect an MCP-capable agent to game telemetry, navigation, exploration, combat,
menus, save handling, and Queen's Blood tools. The model chooses its actions;
the harness validates observations and sends bounded controller input.

Contributions target `master`; see [contributing](CONTRIBUTING.md) for scope,
privacy and the owner-reviewed merge workflow.

## Start in simulation

Requires Python 3.11 or newer and Git. Run from this repository:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e "Tools/ff7r-codex-agent-mvp[dev]"
.\.venv\Scripts\ff7r-agent.exe observe
.\.venv\Scripts\ff7r-agent.exe demo
.\Start-CaitGPT.ps1
```

The launcher starts the MCP server at `http://127.0.0.1:8766/mcp` in simulation.
It never launches the game, installs probes, loads a save, starts OBS, or sends
real input. Configure your MCP client to use that loopback URL. The server must
run from a clean Git checkout; branch and source identity checks stay enabled.
For a feature branch use `-ExpectedRef <branch> -ExpectedCommit <full SHA>`.

For a foreground stdio server, set `FF7R_INPUT_BACKEND=simulation`
and run `.venv\Scripts\ff7r-agent-mcp.exe`.

## Real game setup

Read [setup](docs/SETUP.md) and the component READMEs before enabling real input.
Use your own legally installed game, compatible UE4SS SDK/runtime, and controller
mapping. The native probes are source only; building or installing them is a
separate explicit step. No game binaries, UE4SS binaries, saves, account data,
recordings, screenshots, topology caches, or credentials are bundled.

```powershell
# After installing and verifying the matching telemetry probes:
.\Start-CaitGPT.ps1 -InputBackend windows `
  -TelemetryFile 'C:\CaitGPT\Telemetry\state.json' `
  -GameExecutable 'C:\Games\FINAL FANTASY VII REBIRTH\End\Binaries\Win64\ff7rebirth_.exe'
```

Opening a control session and loading a save are separate MCP actions. Check
observed outcomes, poll asynchronous jobs to completion, and stop on ambiguous
placement, telemetry, process, or input state. Never overwrite occupied saves.

## Included components

- `Tools/ff7r-codex-agent-mvp`: Python agent, MCP runtime, safety/input brokers,
  navigation and exploration, battle/menu/save tools, Queen's Blood, tests and benchmarks.
- `Tools/ff7r-telemetry-exporter`: read-only gameplay telemetry and Queen's Blood adapters.
- `Tools/ff7r-navigation-probe`: Lua and native navigation probes plus build/install helpers.
- `Tools/ff7r-map-surface-probe`: optional legacy read-only map probe.
- `Tools/ff7r-navigation-validation`: independent offline route validator.
- `Tools/ff7r-route-following-simulator`: offline traversal simulator and replay fixtures.
- `Tools/ff7r-first-live-route-selection`: offline route candidate selection using your own topology.
- `ProjectKnowledge/Current`: gameplay catalogues and the stable operation contract only.

Optional speech, event and recording receipts are lightweight integration hooks.
The OBS monitor, narrator worker, video editor, clipping, thumbnails, YouTube
uploader, channel automation, modding tools and Unreal editor project are excluded.
The old `session_startup` module requires an external compatible service manager;
use `Start-CaitGPT.ps1` for this standalone distribution. The live Queen's Blood
campaign still requires verified external driver/stream/save receipts; offline
simulation does not award live progress. See [extraction notes](docs/EXTRACTION.md).

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest Tools/ff7r-codex-agent-mvp/tests -q
Push-Location Tools/ff7r-route-following-simulator
..\..\.venv\Scripts\python.exe -m unittest discover -s tests -v
Pop-Location
```

CI runs offline tests only. Tests requiring the excluded owner's raw topology,
historical readiness matrix, external SDK/header dumps or OBS service manager
are explicitly skipped when those optional inputs are absent. Native or live
game compatibility must be validated separately; simulation is not native proof.
See [export validation](docs/VALIDATION.md) for the checks performed on this copy.

## License

Original harness code is available under the [MIT license](LICENSE).
Game-derived identifiers and catalogue/fixture data retain their respective
owners' rights; see [third-party notices](THIRD_PARTY_NOTICES.md).
This project is unofficial and is not affiliated with Square Enix or OpenAI.

Read the [security policy](SECURITY.md) and [source security review](docs/SECURITY-REVIEW.md)
before enabling real input or preparing a public release.
