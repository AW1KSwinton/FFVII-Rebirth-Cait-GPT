# Security policy

This project controls a local game process. Run it as a normal user on a trusted,
single-user desktop, with your own trusted MCP client, game installation and
SDK dependencies. Do not run it as administrator, expose it through a proxy or
tunnel, publish its local endpoint, or treat it as a multi-user hosted service.

## Boundaries

- Simulation is the default. Real input needs an explicitly selected backend,
  fresh game telemetry, process/focus checks and an open control session.
- The HTTP server accepts loopback binding only and explicitly validates Host
  and Origin against its configured local port. Current patched MCP 1.x is
  required. Use stdio when a network endpoint is unnecessary.
- Loopback is not user authentication. Other processes with access to the same
  local desktop are outside this trust boundary. The local controller broker
  and telemetry files are not intended for hostile local or shared machines.
- Keep runtime, telemetry, probe markers, snapshots and SDK files in directories
  writable only by the intended operator. Do not load untrusted topology,
  probe files, plugins or repository code. An isolated validator executes only
  its source-bound, hash-checked repository module, not code from route JSON.
- This repository does not contain user credentials, saves, recordings, raw
  captures or the original private Git history. Runtime artifacts stay outside
  Git. Check Git objects and the entire reachable history before publication.

## Updates and reports

Use an up-to-date Python/pip installation, the declared supported dependencies,
the Node lockfile and current security scans. Known-advisory checks are a point
in time and cannot prove that unknown vulnerabilities do not exist.

Report a suspected vulnerability privately through GitHub's security advisory
reporting when enabled. Do not paste tokens, user paths, account IDs, save files
or private runtime logs into public issues. Describe the affected commit,
configuration, impact and a minimal synthetic reproduction.

Native probes depend on the exact game/UE4SS ABI. Source review and fixtures do
not establish native compatibility or crash freedom. Build, verify hashes,
preserve backups and perform controlled native acceptance on your installation
before enabling real game input. No live-game or native deployment is part of
the source-only security audit.
