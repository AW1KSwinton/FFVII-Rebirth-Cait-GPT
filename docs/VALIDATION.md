# Export validation — 23 October 2026

This is source-only validation. No game input, native installation, save load,
OBS service startup, recording review or video publication was performed.

- Agent corpus: 4,323 test cases passed across the full run and focused retries;
  111 cases were skipped in the full run, with 31 additional passing subtests.
  Retries used temporary fixtures outside every Git checkout, as the recorder
  requires, and a committed clean checkout for source-binding tests.
- Route-following simulator: 28 tests passed.
- Independent navigation validator: 7 tests passed; the class requiring the
  excluded raw topology snapshot was explicitly skipped.
- Optional game-only OBS capture adapter: 6 Node tests passed.
- Python compilation, PowerShell parsing, simulation `observe` and `demo`,
  tracked-file distribution checks and Git whitespace checks passed.
- Gitleaks 8.30.1 scanned the fresh commit and reported no secrets. A separate
  identifier/artifact audit found no unexpected personal paths or identifiers.

The local agent run did not have the optional Lupa Lua runtime installed.
CI installs the declared development extra and runs the offline corpus again.
Excluded owner evidence, historical readiness data, external SDK/header dumps
and the original OBS service manager are not substitutes for native acceptance.
Live gameplay and native ABI compatibility still require testing against each
user's own game, SDK, telemetry probes and controller installation.
