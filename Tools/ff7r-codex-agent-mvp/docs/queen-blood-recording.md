# Queen's Blood recording and offline replay

Recording is off on every worker start. Enabling it is an explicit per-run
diagnostic action, independent of streaming/rehearsal and of campaign results.
This implementation was developed and checked offline. No probes, services,
installed DLLs, game input, saves, or live stream configuration were changed.

## Enable a bounded recording

In an already authorized gameplay run, call the harness's generic transport:

```json
{"operation":"start_queens_blood_recording","arguments":{"run_id":"chatgpt-slot13-failure-01","duration_seconds":1800,"max_bytes":67108864,"sample_hz":10}}
```

Use `get_queens_blood_recording_status` to inspect the path, limits, queue,
dropped-event count and writer error. Use `stop_queens_blood_recording` to drain
and close; if `finalizing=true`, poll status until false. Normal worker EOF or
shutdown also drains the writer with a bounded wait. A crash can leave a partial
last line or lose its buffered tail; do not treat that as a complete recording.
Restarting the worker requires another explicit start.

Default destination: `%LOCALAPPDATA%/ff7r-agent/qb-recordings/<run_id>-<uuid>/events.jsonl`.
`FF7R_QB_RECORDING_DIR` changes only the destination, never enables recording.
Resolved destinations inside any Git checkout, including ignored directories
and linked worktrees, are rejected. Existing recordings are never overwritten
or pruned. Each explicit run has its own limits; retention of older recordings
remains the owner's choice.

Each JSONL event has a schema version, UTC timestamp and monotonic offset:

| Kind | Evidence |
| --- | --- |
| header | Run ID, declared driver/model, assigned slot/profile, existing verified loaded-save/process receipt for live capture, limits and input backend |
| sidecar | Raw text from reads already performed: state.json, queen_blood.json, surroundings and navigation sidecars, including invalid JSON |
| telemetry | Validated consumer payload after sidecar binding/merges, including field and QB state |
| telemetry_error | Consumer failure, including absent/invalid field feed |
| input | Existing backend event records, including failed key/pad delivery details; simulation uses its action records |
| action | The harness's existing semantic action record |
| operation_start/result/error | Runtime operation arguments, response or exception |
| action_summary / operation_result_summary | Supplementary bounded outcome, guard/sequence, full final QB/input facts and first/last trace frames when an oversized boundary event drops; explicitly marks raw loss and omitted intermediate trace |
| summary | Stop reason, written bytes/events, sampling, dropped-event counts and reason counters |

The recorder performs **no extra telemetry reads or native requests**. The
dedicated map UI/POI readers remain unchanged; their accepted values are included
in consumer telemetry rather than separately re-read. Native binary
`state.sample`, screenshots, movies and probe request files are not polled.
Recording cannot replace the runbook's screenshot requirement for blank field
feeds, or make an opponent win authoritative: only normal
`finish_queens_blood_match` records do that.

## Performance and bounds

The default duration and hard maximum are 1,800 seconds (30 minutes), allowing
initiation, deck editing, narrated turns and the result to share one recording.
`duration_seconds` accepts 1–1,800 seconds; omitting it uses 1,800. Explicit
shorter durations, including 600 seconds, still stop at their requested deadline.
The 64 MiB file limit can still close a recording earlier.

Other defaults/hard maxima: 64 MiB per file, 10 Hz sampling per raw
channel and consumer (configurable 1–20 Hz), 64 queued events, 8 MiB of queued
weighted data, and 1 MiB/64,000 nodes per event. Traced action and operation-result
boundaries allow 128,000 nodes, retaining long match-start traces without
changing the 1 MiB wire limit or 8 MiB memory/queue limit. Periodic observations
retain the 64,000-node bound. The header records both node limits.
One additional bounded event
can be in the writer. Input/actions/runtime boundaries are not rate-limited.
Bounded snapshot copies prevent later mutations changing queued evidence.
Serialization, disk writes and 250 ms flushes run on one dormant-until-start
daemon. Producers never wait for writer/queue locks or disk. A full queue,
oversized event or contention drops evidence and increments the gap counter;
disk failure disables capture. Duration/storage bounds close the recording.
The legacy input/action logs retain their existing behavior.

An offline Windows microbenchmark using a saved 22.4 KB deck snapshot at 10 Hz
measured the raw-text + consumer enqueue pair at 0.69 ms median / 1.20 ms p95,
with 100 events and zero drops. Disabled capture: 0.2 microseconds median;
rate-limited capture: 0.7 microseconds. Writer work was concurrent. A deliberately
unpaced burst exercised backpressure and reported drops. These measurements
cover this payload on this machine, not game frame rate or larger captures.
Use the status counters to identify pressure before increasing sample rate.

## Reproduce a failure offline

Use the Python package directly, never the production factory, MCP worker or
live service:

```python
from pathlib import Path
from ff7r_agent.qb_replay import create_replay_service, load_recording, replay_operation

path = Path(r"C:/evidence/qb-run/events.jsonl")  # preserve the original
events = load_recording(path)
service = create_replay_service(path, output_root=Path(r"C:/offline-tests/run"))
failure = next(e for e in events if e["kind"] == "operation_start"
               and e["data"]["operation"] == "mulligan_cards")
response = replay_operation(service, failure)
```

`replay_operation` has an explicit QB-only allowlist and requires fake telemetry
and input. It cannot replay travel, lifecycle, stream speech, or save loading.
All fake button presses are collected in `service.input.actions`. The fake
environment/arm exercise the service's normal card-game guards without Windows
input devices. No live profile/history or public event destination is configured.
Do not attach a real backend, profile or streamer to replay.

The fake clock drives service sleeps/timeouts and state freshness. It retains
original producer timestamps and recorded observation wall time, including
stale gaps; it never rewrites every frame as fresh. Timeline mode holds the latest
sample until recorded time advances; a bounded read tick prevents busy loops.
`mode="reads"` consumes successive samples for focused unit tests. Exhaustion
raises `ReplayError` instead of repeating the final sample forever.
Start from the operation near the defect; replaying every earlier operation can
consume a sampled trace differently from the original run.

Replay uses the recorded **consumer** snapshots, so it tests service behavior
against what the service saw. For a binding/parser defect, use the corresponding
raw `sidecar.text` as a fixture for `JsonFileTelemetry` in a temporary directory
with probe requests stubbed, rather than relying on the already-merged payload.
Sampling can omit transitions. Foreground/device state, collection ownership,
native text-query responses and lifecycle side effects are not simulated by
this trace. Supply explicit test fakes for them when the failing path needs them.

## Turn the recording into a regression

1. Stop capture and retain the original JSONL, response/screenshot and Git
   commit outside Git. Check the summary and source hash. The loader rejects
   missing summaries, malformed JSON and drops by default. To inspect crash
   evidence, opt into `allow_partial=True`; only an incomplete final JSON line
   is salvageable, and gaps remain limitations.
2. Identify the operation/error, then copy the smallest surrounding raw/consumer
   samples, input events and arguments into a new small fixture. Include the
   original path, SHA-256, timestamps and the exact projection/omissions. Do not
   overwrite original evidence or commit a whole live capture.
3. Build a fake-clock service in a pytest temporary directory and run the
   recorded operation with its recorded arguments. Assert the observable defect:
   e.g. an unreadable popup cannot authorize a card, an unknown cursor sends no
   toggle, a mulligan checks markers/kept cards, or a missing result cannot win.
   Assert inputs, rejection and resulting state, not just JSON serialization.
4. If snapshots are sparse, label invented field envelopes, repeated frames,
   timing and reactive intermediate cursor events as synthetic. Keep tests of
   raw observed gates separate from synthetic navigation. Never silently infer
   a continuous live sequence from an action's final response.
5. Run the new test and relevant QB/telemetry/input tests offline. Record the
   result in the PR; later live validation is a separate authorized session.

Seed fixtures in `tests/fixtures/qb-recording/regional-snapshots.json` cite saved
2026-10-03/07 .artifacts with SHA-256. `test_qb_artifact_replay.py` covers deck
totals/list duplicates, hidden popup and stale/absent cursor gates, mulligan
submission/unknown hover, opponent-turn waiting, and the Rolf 6–13 result. The
fixture omits reflection, talk-menu and consumer binding metadata; field
envelopes and timing are explicitly synthetic.

`tests/fixtures/qb-recording/issue-366.json` projects the preserved Nene/Moore
receipts and final session ledger. Its metadata lists original paths, hashes
and omissions. `test_qb_issue_366.py` exercises the observed incomplete board,
unidentified admission and delayed post-Confirm proof with explicitly synthetic
timing and pickup frames. These sparse receipts cannot prove a continuous native
timeline or establish why the UI-root path was missing.

For ordinary board phases, full observation now requires two independent equal
snapshots with all 15 cells, identified occupied cards and their powers, and
identified hand slots. It allows four complete verification samples plus up to
four additional reads for incomplete exports. Incomplete samples break the pair;
their sequence and missing fields are retained in `SafetyError.details` instead
of accepting an owner/occupancy-only board or filling it from a prior read.
Genuine board, hand, cursor, lane, placement and phase changes still exhaust the
four-complete-sample discrepancy bound. Placement admission uses this verification
and refuses unidentified slot placeholders before pickup.

After Confirm, an ambiguous result permits a further input-free reconciliation
wait of at most 12 seconds within the caller's original action deadline. A fresh
exact card on the intended player cell with placement inactive proves it landed;
unidentified hand slots leave the reported copy count and `card_left_hand` unknown.
Wrong cards, missing board identities, active placement, stale samples and source
changes cannot provide that proof. An unknown result stays `placement_unverified`,
`retry_safe=false`, `requires_fresh_observation=true`, with no blind Confirm or
Cancel. The existing retry still requires a fresh unchanged held-card preview,
same turn, target, cursor and hand identities/count before the extra recovery wait.
After any error, follow #268 (source-project reference omitted):
stop input, reconcile fresh board/hand/placement state and inspect a screenshot
when they conflict. A tool error never counts as proof that a card failed to land.


## Oversized boundaries and live acceptance limits

Drop reasons distinguish event byte/node/JSON bounds, producer lock contention,
queue event/byte bounds, storage bounds and queued evidence discarded by a writer
failure. They are diagnostic counts, not a reconstruction of old recordings.
The original Dax run still has four unclassified drops and incomplete evidence;
its `duration_limit` stop at 600 seconds was expected under the previous cap.
Its init85 final response fits the default snapshot budget; Begin92's
approximately 1 MB response with a
60-entry trace exceeds the conservative weighted event budget. Exact offline
reproduction cannot prove which four live events dropped.

When an oversized `action` or `operation_result` containing a trace drops, the
recorder also attempts one explicitly labeled supplementary summary. It retains
outcome/state, scalar decision/sequence fields, full final Queen's Blood and input
facts, and the full first/last trace frames. Intermediate trace entries are omitted,
with the original count and `raw_event_dropped=true`. This summary obeys the same
per-event/queue/storage bounds; if too large, it also drops and is counted. No raw
file reference is invented and neither reflection data nor intermediate frames
are claimed lossless. Snapshot byte limits are conservative traversal weights,
not simply serialized JSON byte counts. Limits and producer nonblocking behavior
remain unchanged; the footer now reserves 2 KiB for bounded reason counters.

A successful summary never reverses the raw drop or makes `evidence_complete`
true. Strict `load_recording` still rejects such recordings; `allow_partial=True`
permits inspection with the known gap. Replay consumes the ordinary recorded
consumer telemetry, not supplementary summaries, and cannot reconstruct omitted
transitions or prove a full raw operation response from them. Start-header binding
is a point-in-time receipt, not continuous proof that the save/process stayed fixed.
Simulation headers do not claim a verified live loaded save.

For the next full-match diagnostic, explicitly start after verified Begin and
before mulligan or the first move. This acceptance interval excludes initiation
raw traces. Use 1,800 seconds and at most 64 MiB, normally 10 Hz. Stop after normal
`finish_queens_blood_match`; poll until `finalizing=false`, then require zero drops,
no error, a summary, and the expected consumer, input/action and operation boundary
events throughout the recorded interval. If the match exceeds the duration or byte
limit, report the missing tail and preserve the bounded recording; no claim of
complete match evidence is justified. This plan is not live acceptance evidence.
