# Queen's Blood read-only probe

## Bulk card reads (#208)

`read_queens_blood_cards(area="Player")` reads the full selected deck on Deck
Select; `area="Edit"` reads the available collection in its displayed order on
Deck Edit, including off-screen rows. It sends no input and invokes no game
function. The response includes positions, catalogue names, raw native IDs,
item-row identities, source, timing and unchanged cursor/UI guards.

The harness reads the native controller's bounded backing arrays with Windows
`PROCESS_VM_READ | PROCESS_QUERY_INFORMATION`. The Lua probe supplies a fresh
controller address from the exact live deck root, even before a selection
callback fires. Resident accessor metadata and the game's row-name registry
resolve each ID through the generated `CardGameCardInfo.KeyItem` catalogue join.
Deck duplicates and collection order are retained. No inventory or deck cache
supplies the results.

The reader requires a verified loaded model/save receipt, the same game process
creation token, the supported executable SHA-256 and loaded code signatures.
It checks array contents, controller headers and UI receipts before and after
reading. Unsupported layouts or unresolved identities refuse bulk reads; the
regular deck reader retains its cursor fallback. A changed screen or loaded
save stops the action. Collection scans use the bulk path only for an
unfiltered grid, and edited decks receive a fresh bulk verification.

Live validation on 2026-10-07 matched independent serial reads on both screens:
15 deck copies and 12 owned card types, including the off-screen editor row.
Five reads per screen took 1.286–1.647 seconds with unchanged UI guards; warm
memory reads took 1.9–3.4 ms. No-op deck verification fell from 24.84 to 1.49
seconds. Two checked edits and fresh bulk verification restored the original
15-card deck. The report (optional source artifact; not distributed)
preserves timings, screenshots, raw response paths and the tested build hashes.

## Opt-in card-cell population experiment (#208)

`card_enumerator.lua` is an isolated developer experiment. It is loaded once,
but normal telemetry sampling never calls a card-population function. Use the
harness operation `probe_queens_blood_card_cells(area="Player"|"Edit")` for
inspection; `execute=true, positions=[[row,index], ...]` is a separate explicit
opt-in. Execution requires a verified model/save, a control session, an active
cursor and an identified original card. Edit probes additionally require an
ownership scan after the current verified load. It is not an automatic deck
reader and no result is marked verified by this experiment alone.

The exact controller functions are:

- `/Script/EndGame.EndNewCardDeckWindow:OnCardListPlayerCellSetupItem`
- `/Script/EndGame.EndNewCardDeckWindow:OnCardListEditCellSetupItem`

Both have signature `(int32 InRowIndex, UWidget* InItemWidget, int32 InItemIndex)`
in the installed `ue4ss/CXXHeaderDump/EndGame.hpp` (lines 29521 and 29526).
The live function must also report `FUNC_BlueprintCallable` (`0x04000000`). The
controller is captured from a selection callback and bound to the same live
deck root. The reusable pane is that root's authored `Card_View.Card_Full`;
CDOs, asset templates, enemy panes and caller-supplied object paths are refused.

Live inspection on 2026-10-07 reported `0x00040401` (Final, Native, Private)
for both candidates, without BlueprintCallable. The execution request was
therefore refused before any population call. This contradicts the premise
in #208; the header signatures alone do not establish callability or safety.
The alternative bulk array path above avoids these functions. Manual population
correctness, restoration and visual side effects remain untested. See
the live experiment report (optional source artifact; not distributed).

The request is bounded, expires after eight seconds, and each ticket is
consumed at most once on the existing game-thread timer. Card identity uses
the same `Billboard_Thumbnail.ImagePath` catalogue join as the normal probe.
Positions and duplicate identities retain their exact input order. The probe
restores the original card using its observed position, including after a
call/read exception, then compares card fields, list counts, cursors and shown
windows. Any unresolved identity or observed side effect returns a refusal
with before/after evidence; it never retries a possibly mutating native call.

Before enabling a fast path, compare the output against a fresh navigation
read and inspect screenshots for effects the reflected checks cannot see.
BlueprintCallable alone is not proof of safety. A visible-pane experiment may
flicker or change presentation even if it restores the card ID. Keep navigation
as the fallback. Manual population remains gated until its separate live
experiment proves correctness and restoration; the validated bulk array path
does not populate widgets.

## Normal sampling

Card discovery sleeps during field gameplay, travel and NPC resolution. After
a verified Talk affordance, the harness requests only the `TalkMenu_00_C`
answer wheel; its existing five-second discovery throttle and fresh path cache
remain in effect. Confirming a verified Yes creates the match lease. UI reads
cannot create that lease. Declines, failed challenges, result dismissal and
game loads release it.

Sampling is on demand. The marker contains `<expiry> <scope> <ticket> <mode>`.
Each explicit UI consumer read requests a unique ticket and waits for the
matching acknowledgement. The existing timer serves each ticket once and
then sleeps; no card scan or snapshot write repeats while the screen is idle.
There is no new timer or background process. Generic observations, narration,
navigation, recording and campaign bookkeeping never request QB samples.

Full match observations require two independent equal, complete samples. Four
complete samples remain the bound on a discrepancy; up to four additional
reads allow incomplete exports to recover. An incomplete identity sample breaks
the verification pair and never borrows a card identity or power from an older
sample. Deck composition is read twice before and after each edit
batch; a mismatch replans only the remaining edits. During an edit, cursor and
count acknowledgements resolve known paths and callback state without global
discovery. Full census discovery caches paths, never UObject wrappers, and
invalidates when the root/editor changes or a path disappears. Transition waits
remain bounded and stop as soon as their expected state is verified.

`Scripts/main.lua` is a source-only UE4SS probe for the card-game telemetry
sidecar. It writes `C:\CaitGPT\Telemetry\queen_blood.json` and binds
each snapshot to the exact `state.sample` sequence it read.

The hand identity path is state-to-catalogue based. For the five player slots,
the probe reads the direct `ImagePath.AssetPathName` on each live
`CardFull_01` through `CardFull_05` `Billboard_Thumbnail`. A path such as
`U_Com_Billboard_CardGame_Card_010` is joined to the existing
`CardGameCardInfo.csv` catalogue as `card_id_010`, including its authoritative
`UniqueId` and `NameID`. `Scripts/card_catalogue.lua` is the compact offline
join of that catalogue to the English `Resident_TxtRes.uasset` resource, so
the runtime never calls the unsafe in-game text-resource API. It also reads
the single authored `Txt_CardName.Text` field as a bounded identity
fallback/diagnostic; it does not OCR rendered pixels or derive identity from
widget names.

`CardIndex` remains in the output only as the hand-slot diagnostic; it is not
used as card identity. If a live artwork path or catalogue entry is missing,
the identity/name fields remain null rather than being guessed.

The sidecar also includes a bounded `reflection.card_probe.slot_diagnostics`
array for the five known live slots. It records whether the authored
`Txt_CardName` widget and text value were readable, the value types and bounded
text of the two named `FSoftObjectPath` members, which source resolved a
catalogue identity, and the per-card mulligan marker and source. These
diagnostics expose the existing allow-listed reads; they do not infer a card
from its widget name or rendered pixels. The published hand still contains
only identities that join exactly to the offline catalogue.

For mulligan and hover state, the probe tries each known CardFull field through
direct/reflected access and may resolve an exact authored child name with
`GetWidgetFromName`; it never enumerates or traverses arbitrary WidgetTree
children.

The five slot diagnostics record the actual CardGame_Top_C or
CardGame_Test_C lookup root and the marker field/source used for each hand
slot. Constructed confirmation and menu widgets can report UI presence, but
they are not accepted as card roots and do not trigger the card scan. Mulligan
state uses only direct marker booleans and visibility from named widgets. The
probe does not read icon opacity, color/tint channels, brush resources, or
parent visual chains in its live sampling path.

The probe does not inspect the mulligan prompt text. It exports whichever
identified `CardFull` hand slots are currently readable, including partial
hands before the 3D manager is constructed. After the harness sends Begin
Match, the first fresh sidecar sample with an identified card is treated as
the mulligan transition. If no card identity appears, the harness waits 20
seconds and retries the Begin Match action, up to three attempts.

During a mulligan, the harness also requires complete marker telemetry before
pressing Play. The probe reports `mulligan_selected` on each hand slot only
when an explicit authored marker field is available; missing marker state is
left unknown so the harness fails closed instead of assuming that a left-click
landed on the card focused by the in-game cursor. The desktop mouse pointer is
independent of that card focus and is not repositioned by the harness.

The same catalogue join publishes `cost` (`CardGameCardInfo.MP`), `power`
(`CardGameCardInfo.Power`), the base `influence`/`note_id`, and the linked
effect identifiers: `effect_id`, `effect_text_id`, and `effect_influence` from
`CardGameSpecialParam`. It also publishes `effect_constant`, `effect_target`,
`effect_type`, `effect_int_parameters`, `effect_text_parameters`, and
`effect_reference_text_id`, preserving the authored effect parameters.

Each card now has `effect_text`, a player-facing English description. Exact
wording is included for the externally documented card abilities; the
remaining descriptions are deterministic English renderings of the authored
special-param type, target, amount, trigger, and affected-area data. The
`effect_text_source` field distinguishes those two cases. The original
localization identifiers remain present for provenance and future replacement
with an extracted official text resource.

The numeric `effect_type` and `effect_target` values are accompanied by
derived names in `effect_type_name` and `effect_target_name`. The names are
conservative semantic labels, not replacements for the raw values:
`effect_target_name` maps 1/2/3 to allied/enemy/both board cards and 0 to an
event or no-board target. The special-param arrays are also emitted as
`effect_text_label_references`, `effect_heading_types`,
`effect_parameter_references`, `effect_reference_counts`,
`effect_filter_tags`, and `effect_reference_count0`.

Effects that create or reference cards retain their raw IDs in
`effect_text_parameters`. `card_catalogue.lua` additionally exposes every
non-main CardGameCardInfo row under `auxiliary_cards`, including the token and
Queen's Slave/gimmick definitions. A hand card includes matching
`effect_card_definitions` entries when its effect parameters refer to one of
those auxiliary rows, so an LLM can follow a token reference without another
catalogue lookup.

The raw influence names are also decoded from `CardGameCardInfl.csv`. Each
pattern is a 5x5 mask centered on the played card; the hand record publishes
`influence_square_count`/`influence_offsets` for the card's board influence and
`effect_influence_square_count`/`effect_influence_offsets` for the special
ability's affected area. Offsets are `{row, column}` pairs relative to the
card, with `{row=0, column=0}` reserved for the card itself. This keeps names
such as `Cross1`, `NoInfl1`, and `Xletter1` directly LLM-readable without
depending on a screenshot or an external guide.

The generated join can be rebuilt with:

```powershell
python Scripts\build_card_catalogue.py `
  --resident-root C:\CaitGPT\DataTableIndex\Input\Resident `
  --output Scripts\card_catalogue.lua
```

The English ability wording was checked against the published card lists from
[Samurai Gamers](https://samurai-gamers.com/final-fantasy-vii-rebirth/ff7reb-queens-blood-cards-list/)
and [Destructoid](https://www.destructoid.com/ff7-rebirth-every-queens-blood-card-and-where-to-get-them/).

The probe is intentionally conservative. It reads only allow-listed manager,
board, hand, move, card, and UI classes; it never sends input or chooses a
move. If a reflected field is unavailable, the field remains absent and the
Python harness refuses to act until a legal move and cursor position are
present.

Board faces and frame cursors resolve the constructed `CardGame_Test_C` root
once per board sample. If its remembered path is unavailable, a bounded class
inventory (at most eight entries, at most once per second) can reacquire one
unambiguous constructed root. Cooked prototypes, multiple constructed roots,
and an over-bound inventory remain unresolved. The root path plus current
address invalidates face/cursor paths when the object changes. Only primitive
paths/identity strings survive callbacks; actual artwork and power are read
again, never cached as gameplay facts. This recovery is covered by mocked Lua
regressions; live root-loss causality and acceptance for #366 remain unconfirmed.

Install it as an isolated UE4SS script for controlled evidence capture. Keep
the existing telemetry exporter enabled, preserve the original `mods.txt`, and
remove the probe after the capture. This is a producer implementation, not
live acceptance of the Queen's Blood runtime field names.
