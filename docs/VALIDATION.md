# Export and security validation — 23 October 2026

The second review used a fresh clone of the uploaded repository and a clean
Python virtual environment with the declared development dependencies, including
Lupa. Test state was isolated outside every Git checkout. No game input,
native deployment, save load, OBS startup or video processing was performed.

## Offline checks

- Full clean-environment agent run: **4,523 passed, 48 skipped**, plus 31 passing
  subtests. Unlike the initial export run, Lua behavior tests were enabled.
- The remaining skips require excluded historical readiness/raw topology/SDK
  inputs or the original external service manager; no native success is claimed.
- Focused release-security tests cover Host/Origin rejection, separate runtime
  directories, staged and committed blob checks, redaction and forbidden files.
- Route-following simulator: **28 tests passed**. Independent route validator:
  **7 passed**, with the raw-topology class explicitly skipped.
- Optional game-only OBS capture: **8 Node tests passed**, including URL
  restrictions and configured-password redaction.
- Python compilation, PowerShell parsing, workflow YAML parsing and Git
  whitespace checks passed.

## Privacy and dependencies

- Gitleaks 8.30.1 scanned all reachable source history with decoding enabled:
  **no secrets found**.
- A separate history audit checked all unique text blobs and commit
  author/committer metadata: **no unexpected personal identifiers found**.
  Generic references to a file-sync product were manually reviewed as
  non-personal technical comments.
- The Git-object distribution check passed. It scans actual staged/committed
  content rather than trusting a clean working copy.
- Python dependency advisory audit: **no known vulnerabilities found** in the
  updated isolated environment. Optional keyboard/gamepad provider versions
  were also checked without installing controller drivers.
- Node lockfile audit: **zero known vulnerabilities** across the locked tree.
- Python static analysis: **zero high-severity findings**. Three medium sites
  were reviewed and explained in [the security review](SECURITY-REVIEW.md).

These are point-in-time findings, not a guarantee of zero defects or unknown
vulnerabilities. Native probes were not compiled or exercised against a running
game in this source-only audit. The repository must remain private until its
owner chooses to publish it; no visibility change is part of this work.
