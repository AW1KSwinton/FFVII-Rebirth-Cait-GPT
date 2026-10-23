# Match deck provenance

`profile.known_deck()` is the last verified inventory deck, useful for planning
and editing. It is never evidence of a later match's deck. Finished history
records now use the match's own `deck` from `finish_queens_blood_match`, retaining
the existing version 2 history schema. Provenance diagnostics remain in harness
responses and action logs, so strict history consumers need no schema change.

Immediately before each actual Begin Match attempt, the service accepts two
equal 15-card native Player-array reads from the observed deck-select screen.
The existing executable, controller, UI, loaded-save and process guards apply.
Composition comparisons retain duplicate copies and ignore list ordering.
Changed composition, source binding or loaded-save receipt stops before Begin.
Absent native evidence produces `deck: null`; the service never adds cursor
input to reconstruct a deck. An already open Begin popup without verification
in this call also produces null. Deck-select stops and prior deck edits cannot
supply verification to another initiation.

Each successful initiation creates independent match evidence. Initiation and
finish responses include:

- `deck`: the verified 15-card list, including copies, or null.
- `deck_provenance.status`: `verified`, `unverified` or `contradicted`.
- `verification`: the current attempt's source, time, profile, slot, controller
  and native guard, or null.
- `observed_minimum_copies`: lower bounds from actual hand/player-board samples
  and confirmed consumed cards, never an inferred full deck.
- `contradictions`: card IDs whose observed count exceeds the verified count.

Repeated observations and mulligan redraws do not add copies. A held-card
preview is not another placement. Confirmed placements count as consumed only
when the existing gates prove commitment and departure from the hand; duplicate
action records and ambiguous attempts do not add consumed cards. Consumed copies
remain evidence after destruction. Opponent cards and unidentified hand slots
do not supply player-deck facts. A contradiction is sticky for that match and
withholds the entire deck. The guard is conservative: generated cards absent
from the verified list can also withhold it rather than guess their origin.

A scored finish clears the match evidence. An unscored finish retains it for
the existing fresh-observation retry. A worker restart can recover the opponent
and start time from the public marker but has no verified composition, so its
deck is null. Result scoring, profile matches, live/rehearsal destinations and
campaign win/save authority use the existing paths.

## Offline evidence and live handoff

Issue #356's original Nene record remains unchanged outside Git at
`%LOCALAPPDATA%/ff7r-cait-gpt/example.json Bridge\state\qb-match-history-rehearsal\1791830465226-9ab42305-e8d3-4335-8bb7-3833ba907d12.json`.
SHA-256: `1cb2630e966ce0ca370d2800cda1f9aa3572338a947f050c960bfb217313b1d2`.
The small `tests/fixtures/issue-356-nene-deck.json` extract retains its deck and
final player card IDs/positions/power, including `card_id_077` absent from that
deck. All added telemetry and StepClock timing in `tests/test_queen_blood_deck.py`
are synthetic; the fixture is not a raw-timing replay.

The gameplay owner must validate these cases on the PR's exact ref/commit:

1. Direct entry to mulligan without a native deck verification: finish records
   `deck: null`, `status: unverified`, even if the profile contains an older deck.
2. Freshly observed deck select: initiation and finish/history retain the same
   accurate 15-card list and duplicate counts, with `status: verified`.
3. Repeat those cases in rehearsal and live modes: results remain in their
   respective folders; rehearsal does not advance campaign/save authority.
4. Finish a second match after changing decks: its verification is independent;
   no stale deck or prior contradiction leaks between matches.

Source: `src/ff7r_agent/service.py` (`_match_deck_before_begin`,
`initiate_queens_blood_match`, `_write_queen_blood_history`) and
`src/ff7r_agent/queen_blood_deck.py` (`MatchDeck`). No live game validation was
performed during this offline implementation.
