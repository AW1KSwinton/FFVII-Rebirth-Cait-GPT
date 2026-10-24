# Contributing

Open an issue or a pull request against `master`. Keep changes focused on the
gameplay harness, explain the observed problem and intended behavior, and add
an offline regression when behavior changes. See the test commands in the
[README](README.md) and [security policy](SECURITY.md).

Use synthetic or sanitized fixtures. Do not include local configuration,
credentials, personal paths, account IDs, saves, raw telemetry, recordings,
game binaries or SDK files. Enable GitHub email privacy and use your noreply
address for commits if you do not want your email in public Git history.

The repository owner handles final review and merges. Automated sync processes
prepare draft PRs; they do not merge into `master`. Shared harness changes are
also reconciled with the maintainer's integrated checkout through an explicit
file allowlist and conflict checks. Standalone packaging and runtime isolation
are deliberate differences and must be preserved. Avoid directory-wide copies
or replacing security checks with files from an integrated installation.

Before merging a shared sync PR, run the offline and security checks and use
**Create a merge commit** so its exact commit remains verifiable. Never force
push the default branch. Report security issues privately using GitHub's
security reporting facilities where enabled; do not post credentials publicly.
