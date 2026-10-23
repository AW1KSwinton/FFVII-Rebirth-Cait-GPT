# FF7R surroundings sidecar probe

This is a source-only, read-only UE4SS Lua probe for the existing
`FF7RTelemetryExporter`. It writes a sequence-bound
`C:\\CaitGPT\\Telemetry\\surroundings.json` sidecar containing
nearby field-action actors and nearby `EndCharacter` actors.

It is intentionally not installed or enabled by this repository change. The
probe uses an allowlist, hard caps, distance filtering, and `pcall` around every
runtime access. It does not send input, change actors, enumerate arbitrary
objects, or alter the existing exporter DLL/Lua format.

Before enabling it in a controlled disposable run, validate the class names,
sampling cost, sidecar sequence matching, and UE4SS log for errors. The Python
agent consumes the sidecar only when its `base_sequence` exactly matches the
main `state.json` sequence.

## Intended deployment

Copy `Scripts/main.lua` into an isolated UE4SS script-mod directory, add that
mod to `mods.txt` only for the controlled probe, and preserve the original
`mods.txt` and complete exporter package triplet first. Remove or disable the
probe after evidence capture.
