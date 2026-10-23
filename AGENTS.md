# Gameplay runbook

Use only the game-playing harness for game input. Simulation is the default.
Real gameplay requires explicit operator authorization, matching probe builds,
fresh telemetry, a verified game process, and an open control session.

- Read `README.md` and `docs/SETUP.md`. Start with `Start-CaitGPT.ps1`.
- Declare the exact model and effort when opening control; never guess them.
- Keep the control session open for the game process lifetime. Do not heartbeat
  it or close it to make room for a navigation task. A stop/close latches input.
- Use pursuit for navigation/NPC approaches. For free exploration, inspect
  `explore_view`, choose a destination, start `explore_goto_async`, and poll it.
- Inspect field prompts before interacting and trust only observed outcomes.
- Use async operations for loads, travel, deck edits and mulligans. Poll the
  matching status job and inspect nested success. Never interleave input jobs.
- After every Queen's Blood placement error or unverified result, stop input
  and re-observe board, hand, held card and cursor. Never replay a placement
  that may have landed. Capture the game window if the outcome is ambiguous.
- Decks require exactly 15 owned card IDs, with duplicates verified against
  the loaded save. The model chooses decks, mulligans and moves.
- Preserve distinct Darren and Devin wins. Campaign progress comes from
  verified finish records and saved receipts, never model recollection.
- Use your own designated saves. Never allocate an occupied slot, overwrite
  an operator's Fresh/base save, or assume the original author's slot numbers.
- Stop on stale/lost telemetry, combat/cutscene/dialogue transitions or an
  ambiguous outcome. Inspect the game before describing the cause of a block.
- Do not change a running installation while another agent controls the game.
  Preserve backups and raw evidence outside Git. Fixture checks are not proof
  of native/live success. Do not upload user paths, accounts, saves or captures.
- Offline gameplay requires no voice narration. Optional live speech/stream
  integrations must verify their own delivery and driver receipts. Never speak
  private logs, paths, configuration, development chatter or hidden reasoning.
