# Standalone session startup

Use [`Start-CaitGPT.ps1`](../../../Start-CaitGPT.ps1) in this distribution.
It starts a simulation-first loopback MCP service. It does not start the original
OBS monitor, streamer or scheduled-task service manager. See
[setup](../../../docs/SETUP.md) for real input and optional integrations.

Open the control session once with the exact client-reported model and effort.
It lasts for the game process lifetime. Do not heartbeat it or close it before
navigation tasks; they borrow it. After a latched stop, inspect the reason and
explicitly open control again. Load saves only through verified asynchronous
harness operations. Poll each job and inspect its nested outcome before acting.
