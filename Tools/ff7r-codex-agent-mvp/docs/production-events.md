# Production event emission

The gameplay harness can write small, model-neutral evidence events for the
independent production worker. It does not analyze media, request a model
assessment, or create clips. Event writes use a bounded background queue and
atomic per-event JSON files. A full queue or disk error is reported through
`get_production_event_status`; it never delays or blocks gameplay input.

The harness writes to `${stateDir}/production/events/<sha256(eventId)>.json`.
The monitor owns `production/current-recording.json` and
`production/stream-state.json`; the producer reads these snapshots to attach
recording identity, stream evidence, and timing. If the snapshots are absent,
stale, paused, rolled back, or inconsistent, timing stays unknown or the
development interval is marked uncertain for reconciliation. An operator
field or the fact that OBS is recording does not prove that a recording is
offline or eligible for processing.

## Opt-in development test context

Navigation and NPC harness CLIs accept `--production-test-context <json>` and
`--production-state-dir <path>`. Context is explicit because model-neutral
outcome evidence must identify what feature was exercised. For example:

```json
{
  "feature": "npc-approach route verification",
  "featureVersion": "route-proof-v2",
  "behaviorIdentity": "talk-prompt-and-arrival-check",
  "testObjective": "Reach the named NPC and verify the Talk prompt",
  "expectedBehavior": "The nested harness result reports talk_offered",
  "issue": "123",
  "pullRequest": "https://example.invalid/private-source-reference"
}
```

`featureVersion` and `behaviorIdentity` describe the tested behavior and stay
stable across unrelated repository commits. The harness adds the runtime
commit and the client/model/effort only when those values are present in the
declared driver record. Both Claude and Codex therefore use the same verified
harness result contract; model context is never treated as evidence.

For MCP-driven work in `FeatureValidation`, register the metadata, begin the
test, run the existing route adapter, and end the test:

```text
register_production_test_context(context={...})
begin_production_test()
start_live_route_adapter(...)
get_live_route_adapter_status(job_id=...)
end_production_test()
```

The route worker persists its own pre/post monitor boundaries and a compact
nested adapter result. `end_production_test` reads that result itself; it has
no caller-supplied success or observed-behavior argument. To demonstrate an
NPC Talk prompt, set `expectTalkPrompt: true` in the registered context. End
then performs a fresh read-only prompt observation and requires both verified
arrival after a movement command and the observed Talk prompt. For retries,
register the next attempt with the prior event ID in `beforeEventId` so the
worker can connect attempts without treating a repeated unchanged test as a
new feature version.

An event is `verified` only when the existing nested route/NPC result confirms
arrival or an offered Talk prompt after actual execution. An explicit harness
failure is `failed`; dry runs, outer dispatch success, incomplete jobs, and
unverified outcomes remain `unknown`. Events include private local evidence
references for reconciliation; they are not public-copy facts. Offline unit
tests use fake telemetry and temporary state and must never be described as
recorded gameplay footage.

## Gameplay events

The harness emits `queens_blood_notable_exchange` only when its existing
commentary detector marks an exchange notable, and `match.finish` only from a
verified finish record with a score. Related exchanges and the finish share a
stable match `storyId`; finish records reference earlier emitted event IDs.
Notable exchange events retain the detector's canonical cue kind and structured
facts (lane, swing, lead direction, card, and score when available), plus the
verified board projection and its margin change. These describe the observed
board state at that exchange, not the final match result. Ordinary cue labels
stay canonical so the worker can select by cue kind without parsing a lane
suffix.
`input.qb.placement-uncertain` preserves an ambiguous placement failure as
unknown evidence, because an error or timeout does not prove the card failed
to land. Ordinary placements, navigation, menus, and standard match results
do not become notable cues and do not themselves request model work or clips.
Gameplay events carry a point interval only when a fresh monitor timestamp
matches an exact OBS duration sample; stale, paused, or quarantined timing is
retained with an uncertain interval. Fresh OBS evidence that streaming is
active excludes the event from intake entirely. Stale or unknown stream state
is preserved for worker reconciliation and cannot authorize processing.

Call `get_production_event_status` to inspect accepted, written, duplicate,
dropped, failed, queued, and retried counts. CLI demonstration runs flush the
bounded queue before exiting and include the status in their output.
