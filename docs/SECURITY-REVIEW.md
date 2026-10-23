# Source security review — 23 October 2026

The review began from a fresh clone of the actual uploaded repository, not an
untracked staging folder. Source, history and dependencies were checked again
after publication. This report contains no user paths, private account
identifiers, credentials or private audit logs.

## Changes made

- Require MCP 1.30.0 or newer in the supported 1.x line. The original broad
  dependency range allowed obsolete SDK releases. The upstream advisories for
  [localhost DNS rebinding](https://github.com/modelcontextprotocol/python-sdk/security/advisories/GHSA-9h52-p55h-vw2f),
  [authenticated HTTP session ownership](https://github.com/modelcontextprotocol/python-sdk/security/advisories/GHSA-jpw9-pfvf-9f58)
  and [experimental task ownership](https://github.com/modelcontextprotocol/python-sdk/security/advisories/GHSA-hvrp-rf83-w775)
  establish why older releases should not be permitted. Not all advisory
  configurations are enabled by this harness.
- Explicit HTTP Host/Origin allowlists use the configured loopback port, with
  regression tests for foreign hosts, browser origins, null origins and other
  ports. Non-loopback binding remains refused.
- Runtime-directory defaults use a separate namespace from the source project;
  the control/runtime directory helpers also honor an explicit isolated path.
- The distribution check reads staged or committed Git blobs, not working
  files. It rejects symlinks, submodules, unresolved entries, binary/runtime
  artifacts, non-example email addresses, personal paths, account IDs and
  recognized credential patterns. It enforces size budgets and never prints
  matched values. Tests cover a clean working copy hiding a staged secret and
  a clean index hiding a secret in a committed ref.
- Fixture account identifiers use an all-zero example, not a valid-looking
  Steam account ID. Original private account identifiers are not distributed.
- Optional OBS capture refuses credential-bearing URLs and redacts configured
  passwords in diagnostic errors. Capture remains restricted to a verified
  game source, not the desktop.
- CI uses immutable official Action commits, read-only permissions, no
  persisted checkout credentials and a fixed runner OS. Security checks cover
  Git objects, complete reachable history, Python dependencies, the Node
  lockfile and high-severity Python static findings. Gitleaks downloads are
  pinned by version and SHA-256.

## Scanner scope and triage

Gitleaks checks source history with decoding enabled. The identifier audit also
checks blob content and commit author/committer metadata. Dependency checks use
current advisory data; optional keyboard/gamepad providers are queried without
installing device drivers. The Python audit environment's packaging tools were
updated separately; they are not bundled application code.

Bandit initially reported no high-severity findings. Its two B113 medium
findings refer to a local `queue.Queue` named `requests`, not HTTP requests.
The B102 medium finding is the isolated validator's deliberate execution of
the repository validator module after its source SHA-256 has been checked.
The source path comes from the source-bound runtime, not from route JSON.
These sites were reviewed; they were not silently excluded or described as
proof of zero vulnerabilities. Low findings are chiefly standard subprocess
and non-security random-number uses and require the trusted-local-code boundary.

## Limits

The supported boundary is a trusted local desktop and operator-owned files;
see [SECURITY.md](../SECURITY.md). Loopback checks do not authenticate other
local processes. Native ABI compatibility, driver behavior and live gameplay
require separate controlled acceptance. No native deployment or game input is
performed by this audit. Automated scans and passing fixtures cannot establish
that every defect or unknown vulnerability has been eliminated.

The final test and scan results are recorded in [validation](VALIDATION.md).
