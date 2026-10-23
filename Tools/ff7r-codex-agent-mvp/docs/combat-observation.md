# Combat observation

**RESEARCH ONLY — NOT CANONICAL.** This is an offline-tested candidate. No
game/bridge/service control, probe installation or live acceptance was part of
its implementation. The installed producer is not assumed to support it.

```python
agent_call(operation="observe_combat", arguments={"timeout_seconds": 3.0})
```

The observer sends no gameplay input, acquires no input arm/session, never
pauses or starts a battle, and makes no targeting or strategic decisions. It
uses a dedicated request-only producer on the existing sampler thread rather
than the generic telemetry's placeholder ATB and enemy fields. A successful
observation is information, not authority to act.

## Combat lifecycle and idle cost

The existing base telemetry tick reads **one boolean** `EndBattleAPI.IsInBattle`
alongside its existing world/state reads. It enables the detailed query reader
on observed entry and disables it on exit or unknown state. There is no new
thread, timer, combat watcher or polling loop. The base cadence is unchanged.
The boolean is the entry/exit signal; expensive roster/vital reads are not
used to discover whether a battle has started.

Outside combat, the detailed reader performs **zero request-file checks and
zero combat roster/vital reads**, even if a marker was left behind. Calling
`observe_combat()` in that state returns the already published base presence
observation immediately, without writing a request or waiting for a reply.
Its `observation_kind` is `presence`, its source is the base exporter, and its
contract is `ff7r-combat-presence-v1`. The timestamp precision is explicitly
`base_telemetry`. Pause/measurements are unavailable, rosters are empty and
incomplete, and `world_id` is `null`
when absent from that base feed; none is copied from an earlier battle.

During combat, only the small request marker is checked at the existing base
cadence. A full snapshot is produced once for each new valid nonce, with
`observation_kind="snapshot"`. No roster, HP/MP or target polling runs between
requests. The existing health diagnostics include `section_ms.combat_query`
so controlled acceptance can measure its game-thread cost.

The native publisher adds `game.combat_state_observed`, and `GameState` keeps
that optional boolean separate from legacy `in_combat=false` defaults. A
missing/false observation flag means unknown battle state, disables the
detailed reader, and returns `combat_state_unavailable` without a request.
The producer must therefore be upgraded as a complete paired candidate; old
telemetry cannot establish either combat entry or exit for this operation.

While a query is outstanding, the consumer watches the existing base feed
within its bounded wait. An observed exit returns presence immediately and
cleans its request; unknown/lost/stale state returns unavailable. It does not
continue waiting for or accept a prior active roster after an observed exit.
Independent process identity is checked at query boundaries, not polled on
every wait tick. Re-entry requires a new query and fresh roster data.

## Available data

The nested `combat` object reports:

- `in_battle` and `phase` (`active`, `inactive`, or `unknown`) from the
  reflected `EndBattleAPI.IsInBattle`, checked before and after the reads.
- Actual `controlled_actor_id` from the fresh controller pawn, separate from
  the field-party leader/type.
- `party` from `EndPartyAPI.GetBattleMemberCount`, `GetBattleMember` and
  `GetBattleMemberType`. It includes dead members if the getter includes them;
  it does not use the alive-only party list or all owned characters.
- `enemies` from `EndBattleAPI.GetEnemyMembersRef`, never a broad actor scan
  or an assumption that every non-player actor is an enemy.
- Actor HP/max HP from `GetCharaHP`/`GetHPMax`; party MP/max MP from the
  existing `EndMenuBPAPI.BPGetPlayerMP`/`BPGetPlayerMaxMP` contract keyed to the
  observed battle member's type; distance from the controlled pawn in metres.
- Raw `controller_state`, optional `chara_id`, and `errors` for failed reads.
- Independent `party_complete` and `enemies_complete` flags, plus
  `target_observed` and `target_id`. An observed absent target and an unreadable
  target are different states.

Values are typed and bounded, and current/max pairs must be consistent. A
missing getter produces `null`, not a zero, previous value or fabricated
default. An incomplete roster never proves that a battle ended or was won.
`available=true` means a battle-state observation is available; it does not
mean all measurements or rosters are complete.

## Deliberately unavailable measurements

Current ATB, status effects, pressure and stagger remain `null`. The static
`GetSpendATBBarTotal` reads spent ATB, while the DataObject `GetATB` reads an
ability cost; neither is a current gauge. There is no source-supported current
gauge getter in the inspected headers. Raw controller state is not converted
into pressure/stagger/status claims without verified semantics.

`battle_instance_id`, `command_ui` and `result` are also `null`. Pause is an
observed pause flag, not proof that the tactical command UI is open. An
`inactive` observation never becomes a victory/reward receipt. These boundaries
must be established before building a combat-action executor.

## Protocol and identity

After fresh base telemetry proves active combat, the observer creates
`combat-query.request` exclusively. Its bounded ASCII
body is `<32 lowercase hex nonce>|<expiry epoch seconds>`. An existing request
returns `combat_query_busy` and is not overwritten, even when expired. This
preserves another caller's ownership; interrupted-worker cleanup requires proof
that the old query is no longer owned before removing its marker. Normal
completion/failure/timeout releases only the caller's own nonce.

`combat_query.lua` is disabled outside combat. During combat it checks the
small marker file, performs no reflected reads without a new valid nonce, and
answers each nonce once. It writes a complete unique
`combat-query.<nonce>.json.tmp`, flushes and
closes it, then renames to `combat-query.<nonce>.json`; it never removes an
earlier or peer response. The observer cleans only its nonce's artifacts.
A late reply cannot satisfy a subsequent observation.

The active snapshot wire envelope requires schema version 1, source
`ue4ss_combat_query`, and producer contract `ff7r-combat-readonly-v1`. It carries source sequence,
capture UTC, map, actual world identity and controlled actor identity. The
consumer checks independent process creation identity before and after the
query, consistent map/world/pawn, a source sequence newer than its starting
base sample, and a base publication that has caught up to the query's tick.
Missing, stale, oversized, malformed or conflicting data returns unavailable
with `combat=null`. No previous snapshot is retained as a fallback.

Default response wait is 3 seconds, bounded to 0.1–8 seconds. The producer
timestamp is explicitly whole-second UTC, taken **after** the combat reads.
Maximum permitted snapshot/base age is 2.5 seconds, matching that precision;
this does not supply the finer freshness contract a future real-time combat
actuator will need. Response bytes are capped at 256 KiB, party members at 16,
and enemies at 64.

Native TArrays are read only through the existing `GetArrayNum`/`ForEach`/
`element:get` pattern, never direct indexing. A dense returned/mutated Lua
table is supported. A void enemy getter that leaves its out table unchanged
and empty cannot prove that no enemies exist: it returns an incomplete roster.
The enemy out-array binding remains a specific live acceptance item.

## Source evidence and integration

Candidate getter declarations are in
`FF7R2UProj/Source/EndGame/Public/EndBattleAPI.h`, `EndPartyAPI.h` and
`EndMenuBPAPI.h`. These reconstructed declarations are source evidence, not
runtime implementation or current-build ABI acceptance. The generated C++
method bodies are stubs and are not used as behavioral evidence.

The Lua code reacquires CDO/function references on each request. It retains
only the handled nonce and an enabled flag between ticks, not UObject wrappers.
It calls only its fixed read getter set, contains no hooks, setters, memory access, input or
object-array scans, and refuses missing functions/invalid objects.

`AgentService.observe_combat` delegates to the focused `combat.py` module;
`SERVICE_ALIASES` and the accepted-operation catalogue expose it through the
existing `agent_call`. It is not added to an active async job's allowed worker
operations. There is no second input backend or telemetry thread. The base
`in_combat` now carries the measured boolean when available; its new observation
flag protects consumers from legacy/unknown defaults. Generic ATB fields are
unchanged and never used as combat measurements.

The producer is included in `Install-PersistentSamplers.ps1`'s complete paired
script/DLL plan. The deployment plan was edited. No installer, including its
dry-run, was invoked. Native source adds the observation flag to JSON without
changing the Lua handoff layout or C++/UE4SS ABI. The native candidate has not
been built: the existing build wrapper requires the game to be closed. Binary
build, paired installation and current-build getter acceptance remain pending.
No installed artifact was changed for this feature.

## Verification and remaining live acceptance

The tests run the real Lua source with synthetic reflected objects and an
in-memory filesystem, then pass its JSON to the real Python parser/observer.
They cover unknown versus measured zero, inactive versus unknown battle state,
phase changes, missing getters, invalid/native/Lua arrays, membership bounds,
duplicate identities, new source publication, process/actor/world changes,
request ownership, timeouts and old replies. They execute the actual base tick
through field → battle → field → unknown → battle transitions, and instrument
the reader's file/getter calls across hundreds of disabled ticks. Consumer
tests prove inactive calls perform no combat-file I/O and exit cancels pending
waits. Existing operation-parity and producer/objective tests run alongside them.

Outstanding controlled checks on a separately pinned, clean candidate build:

1. Build the native candidate with the game closed. Record executable, UE4SS,
   paired native DLL/script hashes and loaded source
   identity. Install only through the existing game-closed, verified-backup
   paired deployment workflow when the live session is available.
2. Compare field → active battle → target/controlled-character switch → pause
   → battle end → field observations with the visible game. Confirm that battle
   members, HP/MP, distances, target and enemy membership actually match.
3. Verify how `GetEnemyMembersRef` populates its Lua out/return array; missing or
   ambiguous output must continue to report incomplete.
4. Confirm the actual getter bindings and query cost on the installed build;
   measure base boolean cost and verify no detailed query activity outside
   combat. Retain raw evidence outside Git. Investigate current ATB and semantic UI/
   result/status reads separately rather than substituting inferred values.

Every response currently reports `validation="live_acceptance_pending"`.
The source catalogue registration is not a claim of canonical deployment or
live gameplay support.
