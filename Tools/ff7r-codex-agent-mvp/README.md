# FFVII Rebirth agent harness

This package contains the local Python MCP runtime and bounded gameplay tools.
See the [repository README](../../README.md), [standalone setup](../../docs/SETUP.md),
and [gameplay runbook](../../AGENTS.md) for installation and operation.

Install: `python -m pip install -e "Tools/ff7r-codex-agent-mvp[dev]"` from the repository root.
Simulation CLI: `ff7r-agent observe` or `ff7r-agent demo`.
Tests: `python -m pytest Tools/ff7r-codex-agent-mvp/tests -q`.

Module and command names retain the original `ff7r_agent` namespace for compatibility.
The virtual gamepad backend is optional; install its provider and device driver
separately and explicitly. Real input remains gated by process, foreground,
telemetry freshness, control-session and emergency-stop checks.
