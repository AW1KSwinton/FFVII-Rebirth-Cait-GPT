# One-call Queen's Blood setup

`setup_queens_blood_match_async` takes the agent's opponent, exact 15-card deck
and prepared public narration. It reuses pursuit approach, match initiation,
deck preparation/editing and observation. It returns at the identified five-card
mulligan hand; the agent still chooses the mulligan and each subsequent move.

This combined call requires an already chosen deck. To choose it while on the
way, start the route/travel separately, fetch
`get_queens_blood_preparation_context(opponent)`, reason about the cards between
status checks, and validate the candidate with `prepare_queens_blood_deck` and
`expected_snapshot_sha256`. After arrival, revalidate and use the normal deck
screen/editor. See [preparing during travel](../README.md#preparing-a-deck-during-travel).

Pass these arguments through `agent_call`:

| Argument | Meaning |
| --- | --- |
| `target_id` | Catalogue opponent ID or canonical field-object target |
| `cards` | Exactly 15 owned card IDs, including duplicates |
| `model_id`, `reasoning_effort`, `client_id` | Explicit controller attribution; model and effort must match the declared driver |
| `expected_ref`, `expected_commit` | Clean FeatureValidation checkout under test |
| `narration` | Prepared stage messages described below |
| `allow_travel` | Defaults to true; false refuses all fast travel and Moogle rides |
| `timeout_seconds` | Overall deadline, 30–1800 seconds; defaults to 900 |
| `output` | Optional directory for the existing approach's diagnostic artifacts |

Poll `get_setup_queens_blood_match_status(job_id=...)`. Status exposes cached
`progress.stage`, `completed_stages` and elapsed seconds; polling performs no
telemetry reads. As with other asynchronous operations, inspect the nested
response: terminal job state alone does not prove success.

Narration contains `{text, tone}` objects for `opponent`, `approach`,
`interaction`, `deck`, and `begin`. If editing a known deck, also supply an
`edit` message and `edit_from` (the expected original 15 IDs). The swap line
is suppressed when the deck already matches; an unexpected original deck
returns control before announcing or executing stale swaps. The deck line
should explain the strategy rather than claim unverified changes completed.

For travel, supply `travel` and `arrival_approach`; unexpected doors, terrain
actions or Moogle rides require their own messages keyed by operation. Missing
explanations return control before that input. A second travel plan requires
new agent commentary rather than replaying the first line. Speech failures or
failed previous delivery stop new input. Rate limiting waits within the overall
deadline, revalidates the run (and deck at a deck stage), then retries.

`say_on_stream` can reject new text as `repetitive` when its first two
lowercase words match one of the last three accepted lines (within the
10-minute history). It also rejects identical or substantially similar lines
within five minutes. Change the observation or reaction and vary the opening,
rather than only replacing the card name. A rejected line invalidates any
pending move receipt: re-observe, send a materially different line, require
speech `ok: true` and a nonfailed `previous_delivery`, then make the move.
For `rate_limited`, honour `retry_after_seconds` before rechecking and retrying.
Legacy `agent_call` exposes speech acceptance in `result.ok`; its outer `ok`
only confirms dispatch.

The supervisor supplies the actual worker identity/bootstrap context. Caller
context overrides are refused. The loaded-save receipt, process creation token,
assigned slot, declared driver and controller ownership are checked before
input, after approach, before Begin and at completion. Setup and the approach
borrow the verified session for the current game and leave it open. They close
only a session they opened themselves; an emergency stop still ends the session
when input may be in flight.

When calling `npc_approach --execute --pursuit` separately after session startup,
reuse the handoff's `client_id` and keep its session open. A successful
`talk_offered` exit reports `control_session: left_open`. Before the next match
action, inspect `get_control_session_status`: require an active session with the
handoff's client, verified game PID and recording driver. Cold startup binds its
initial unbound session to the launched game's creation identity before handing
it off. Observe the current situation, narrate the next decision and call
`initiate_queens_blood_match_async`; there is no routine close/reopen step.

If the report says `closed`, inspect the stop reason and re-establish your own
verified, process-bound session with `open_control_session` before input. If your
retained session has the same client and recording driver but is unbound, stop
input, call `close_control_session(client_id=...)` with your handoff's client ID
and require `closed: true`, then open it against the verified game PID. Never
adopt an arbitrary unbound peer. A refused
open still means no input should follow. The combined setup follows the same
ownership and process checks.

An exact deck verification is mandatory before Begin. The existing editor
reads composition twice at each batch boundary, corrects only remaining edits
and restores the original deck on failed completion. The wrapper does not hide
those failures or switch to blind input. Cursor/card/count checks before each
Confirm share the same cheap requested snapshot; they do not enumerate every
card again.

Failures return `outcome=setup_stopped`, the failed stage, completed stages,
last result, observed error, actual deck where readable, screenshot and cleanup
results. The watchdog releases input immediately, closes this client's lease,
and joins any in-flight primitive before returning. Existing individual async
operations remain available for recovery. Setup never saves a game, changes
the driver declaration or advances a campaign.

For an editor inspection without edits, first use
`read_queens_blood_cards(area="Player")` on deck select and retain all 15
returned card IDs, including duplicate copies. Open the editor, inspect or
scan it, then call `close_queens_blood_deck_editor(expected_cards=<those IDs>)`.
The close reads the draft twice and refuses changed or incomplete decks before
sending input. An unchanged open editor needs one Escape to close; an already
closed editor is verified without input. The returned deck must still match.
The save profile's inventory is empty until its first verified collection
scan; an editor inspection or close does not require inventing ownership or
performing a no-op deck edit.

Collection scans are asynchronous through MCP: while the unfiltered editor is
open, call `scan_queens_blood_collection_async`, retain its `job_id`, and poll
`get_scan_queens_blood_collection_status(job_id=...)`. Inspect the nested scan
response for `ok=true`; a completed job alone is insufficient. The synchronous
`scan_queens_blood_collection` name is an internal worker operation and returns
a redirect error naming the start/status operations when called through MCP.

The scan returns `distinct_cards`, the scanned `cells`, and `inventory`, keyed
by card ID, with each entry's `owned` copy count and `count_source`. The grid
lists each owned ID once, so duplicate deck copies are not inferred by counting
grid entries. Counts use the game's `CardGameCardInfo.Num` catalogue rule
(`catalogue_copies`: two standard copies, one legendary), as the save profile
already does; they are not independently measured UI quantities. If a card is
missing from the catalogue, the existing one-copy fallback is explicitly
labelled `assumed_single_copy`. Do not treat that fallback as verified stock.

Before choosing a move or passing, read each card's `cost`, `power` and
`playable` from `observe_queens_blood().queen_blood.hand`. The large number on
the card face is power; its pawn symbols express the required pawn rank.
For example, Fleetwing has power 3 and cost 1, so its face does not mean it
requires a three-pawn square. `playable=true` means a legal placement exists,
not that every square accepts the card. Check the intended cell's ownership,
occupancy and `rank` against `cost` before attempting a normal placement.
The hand read already includes these fields; do not infer cost from power.

During a match, `observe_queens_blood` exposes the held state in
`queen_blood.board.placement.active`, with its observed target and cursor.
After fresh telemetry establishes that a card is still held, use
`tap_queens_blood_button(button="queen_blood_cancel")` to return it to hand,
then observe again. Both `queen_blood_cancel` and generic `cancel` map to
Escape, but generic menu input must not bypass an unavailable match sample.
If the board/hand/preview conflict, stop and inspect before sending input.
The NPC challenge wheel is a separate conversation: decline its observed No
option with `finish_conversation_async` and inspect the nested final result.

Match initiation fast-forwards ordinary NPC lines by default; use
`fast_forward_dialogue=false` to leave them to play. Talk starts a request-scoped
checker, with a bounded initial wait. Each right-click requires a fresh attached
subtitle, rendered speaker/line/advance arrow, clear protection roots and safe
game context; the click is released and followed by another read. Retained text
alone is insufficient. Unknown or stale evidence disables fast-forwarding.
The checker sends nothing and suspends subtitle requests on a choice wheel. A
verified answer resumes the checker for subsequent
lines and wheels until verified field return or the deck screen. It never polls
outside that bounded conversation action. Recovery without a new Talk leaves
ordinary lines to play.

For a general NPC, use `talk_to_npc_async` and poll `get_talk_to_npc_status`.
It advances verified ordinary speech and returns control when a menu or choice
appears, selecting nothing. `read_npc_dialogue` provides the same read-only signal
for diagnosis and never sends or authorizes input.
