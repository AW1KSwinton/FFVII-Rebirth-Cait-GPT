"""Execute the real Queen's Blood probe under Lua 5.4 with UE4SS-like mocks.

The mocks reproduce the UE4SS behaviour that misled earlier marker probes: a
missing member or null reference reads back as an *invalid UObject*, not nil.
Requires the optional ``lupa`` package; skipped otherwise.
"""
from __future__ import annotations

from pathlib import Path

import pytest

lupa = pytest.importorskip("lupa")
from lupa import lua54  # noqa: E402

PROBE_LUA = Path(__file__).parents[2] / "ff7r-telemetry-exporter" / "QueenBlood" / "Scripts" / "main.lua"
ROOT_PATH = "/Engine/Transient.GameEngine_1:BP_EndGameInstance_C_1.CardGame_Test_C_7"
HAND = ["004", "006", "007", "000", "010"]

MOCKS = r"""
local clock = 1000
os.time = function() return clock end
function advance(seconds) clock = clock + seconds end
local methods = {}
local ObjMT = {}
local next_address = 10000
function Obj(class, path, props)
  next_address = next_address + 1
  local object = setmetatable({__class = class, __path = path, __props = props or {}, __valid = true,
                              __address = next_address}, ObjMT)
  if registry ~= nil then registry[path] = object end
  return object
end
ObjMT.__index = function(self, key)
  if methods[key] then return methods[key] end
  local value = rawget(self, "__props")[key]
  if value == nil then return INVALID end
  return value
end
methods.IsValid = function(self) return rawget(self, "__valid") end
methods.GetFullName = function(self) return rawget(self, "__class") .. " " .. rawget(self, "__path") end
methods.GetAddress = function(self)
  local override = rawget(self, "__props").GetAddress
  if override ~= nil then return override(self) end
  return rawget(self, "__address")
end
methods.IsVisible = function(self)
  local value = rawget(self, "__props").Visibility
  if type(value) == "boolean" then return value end
  return value == 0 or value == 3 or value == 4
end
methods.IsInViewport = function(self)
  return rawget(self, "__props").InViewport
end
methods.IsInViewportEx = function(self)
  return rawget(self, "__props").InViewportEx
end
methods.GetRenderOpacity = function(self)
  return rawget(self, "__props").RenderOpacity or 1.0
end
methods.GetParent = function(self)
  return rawget(self, "__props").Parent or INVALID
end
methods.GetOuter = function(self)
  return rawget(self, "__props").Outer or INVALID
end
methods.GetFName = function(self)
  local name = rawget(self, "__path"):match("([^.:]+)$")
  return {ToString = function() return name end}
end
methods.GetPropertyValue = function(self, key) return self[key] end
-- UE4SS logs a full stack trace whenever a method is called on a nullptr
-- UObject; count those so tests can require the probe never triggers one.
nullptr_calls = 0
local function array_num(self)
  if not rawget(self, "__valid") then
    nullptr_calls = nullptr_calls + 1
    error("Tried calling a member function but the UObject instance is nullptr")
  end
  return 0
end
methods.GetArrayNum = array_num
-- Calling ToString (text()) or any getter on a missing member is the same
-- nullptr call in UE4SS.
methods.ToString = function(self)
  if not rawget(self, "__valid") then
    nullptr_calls = nullptr_calls + 1
    error("Tried calling a member function but the UObject instance is nullptr")
  end
  return rawget(self, "__path")
end
-- Mocks are Lua tables, so the probe's table branch takes #value where the
-- real userdata would take GetArrayNum(); route both to the same check.
ObjMT.__len = array_num
INVALID = setmetatable({__class = "None", __path = "", __props = {}, __valid = false}, ObjMT)
registry = {}
function StaticFindObject(path) return registry[path] or INVALID end
all_of = {}
find_all_calls = 0
find_all_classes = {}
function FindAllOf(class)
  find_all_calls = find_all_calls + 1
  table.insert(find_all_classes, class)
  return all_of[class]
end
function FindFirstOf(class) return INVALID end
hooks = {}
function RegisterHook(path, callback)
  if path:find("EndNewCardDeckWindow", 1, true) or path:find("EndCardUnitList", 1, true) then
    hooks[path] = callback; return 1, 2
  end
  error("hooks are not simulated")
end
function Param(value) return {get = function() return value end} end
function LoopAsync() end
cards = {}
panels = {}
"""

EXPORTS = """
return {
  scan = debug_live_card_image_identity,
  selection = encode_mulligan_selection,
  probe = function() return live_card_probe end,
  manager_hand = encode_manager_hand,
  sampled_manager_hand = function(root, manager)
    -- Mirror encode_snapshot's cache bookkeeping: learn the live pool first,
    -- read those widget identities, then bind that read to the observed pool.
    encode_manager_hand(manager)
    debug_live_card_image_identity(root, false)
    hand_ids.read = hand_ids.seen
    return encode_manager_hand(manager)
  end,
  match_board = encode_match_board,
  deck_screen = encode_deck_screen,
  talk_menu = encode_talk_menu,
  refresh_cursor = refresh_cursor_from_hand_actors,
  cursor = function() return live_card_focus_slot end,
  is_live_ui = is_live_card_ui_instance,
  find_ui = function() return first_ui_object(UI_CLASSES) end,
  find_manager = find_manager,
  deck_top = deck_screen_top,
  detail = encode_deck_detail,
  board_face = board_card_face,
  idle = idle_snapshot,
  scoped_snapshot = scoped_snapshot,
  requested = probe_requested,
  serve_marker = function(sequence)
    local scope, ticket, mode, expired = demand.read_probe_request()
    local state, reason = demand.service_probe_request(scope, ticket, mode, sequence, expired)
    if state then demand.last_ticket = ticket end
    return state, reason
  end,
  serve = function(scope, ticket, mode, sequence)
    local state, reason = demand.service_probe_request(scope, ticket, mode, sequence)
    if state then demand.last_ticket = ticket end
    return state, reason
  end,
  editor_owner = deck_editor.find_owner,
}
"""

CARD = """
local slot, path, tree, card, variant = ...
local panel = Obj("EndCanvasPanel", tree .. ".Pnl_Mulligan", {Visibility = 1})
panels[slot] = panel
local art = "/Game/Menu/Billboard/Common/U_Com_Billboard_CardGame_Card_" .. card
local props = {
  Billboard_Thumbnail = Obj("EndBillboard", tree .. ".Billboard_Thumbnail",
    {ImagePath = {AssetPathName = art .. "." .. art:match("([^/]+)$")}}),
  WidgetTree = Obj("WidgetTree", tree, {}),
}
if variant == "slot_parent" then
  props.Img_Mulligan_Icon = Obj("EndImage", tree .. ".Img_Mulligan_Icon",
    {Slot = Obj("EndCanvasPanelSlot", tree .. ".EndCanvasPanelSlot_1", {Parent = panel})})
elseif variant == "wrong_parent" then
  local other = Obj("EndCanvasPanel", tree .. ".Pnl_Other", {})
  props.Img_Mulligan_Icon = Obj("EndImage", tree .. ".Img_Mulligan_Icon",
    {Slot = Obj("EndCanvasPanelSlot", tree .. ".EndCanvasPanelSlot_1", {Parent = other})})
elseif variant == "tree_path" then
  registry[tree .. ".Pnl_Mulligan"] = panel
end
if variant == "wrong_parent" or variant == "none" then
  registry[tree .. ".Pnl_Mulligan"] = nil
end
cards[slot] = Obj("CardGame_Card_Full_C", path, props)
"""


class ProbeSim:
    def __init__(self, variant: str, *, hand: list[str] | None = None, catalogue: bool = False, clock: float | None = None) -> None:
        self.runtime = lua54.LuaRuntime(unpack_returned_tuples=True)
        self.runtime.execute(MOCKS)
        if clock is not None:
            self.runtime.globals().test_clock = clock
            self.runtime.execute("os.clock = function() return test_clock end")
        shared = PROBE_LUA.parents[2] / "Shared" / "Scripts"
        self.runtime.execute(f'package.path = "{shared.as_posix()}/?.lua;" .. package.path')
        if catalogue:
            scripts = PROBE_LUA.parent.as_posix()
            self.runtime.execute(f'package.path = "{scripts}/?.lua;" .. package.path')
        self.api = self.runtime.execute(PROBE_LUA.read_text(encoding="utf-8") + "\n" + EXPORTS)
        add_card = self.runtime.eval("function(...) " + CARD + " end")
        # CardGame_Test_C authors a 15-widget CardFull pool; the opening hand
        # occupies the first five.
        for slot, card in enumerate((hand or HAND) + ["001"] * 10, start=1):
            path = f"{ROOT_PATH}.WidgetTree.CardFull_{slot:02d}"
            # A duplicated nested tree need not be named plain "WidgetTree".
            add_card(slot, path, f"{path}.WidgetTree_3", card, variant)
        self.runtime.execute(
            f'root = Obj("CardGame_Test_C", "{ROOT_PATH}", {{_DeckCard = cards}})\n'
            'all_of["CardGame_Card_Full_C"] = cards\n'
            'all_of["CardGame_Test_C"] = {root}'
        )
        self.g = self.runtime.globals()

    def set_visibility(self, slot: int, value: int) -> None:
        self.runtime.execute(f"panels[{int(slot)}].__props.Visibility = {int(value)}")

    def scan(self) -> tuple[bool, list[int]]:
        self.g.advance(2)
        self.api.scan(self.g.root, False)
        selection = self.api.selection()
        return bool(selection.observed), sorted(int(v) for v in selection.selected_indices.values())

    def slot_visuals(self, slot: int):
        return self.api.probe().slot_diagnostics[slot].mulligan_visuals


def test_cursor_probe_recovers_visible_deck_top_without_topnpc_root() -> None:
    sim = ProbeSim("slot_parent")
    sim.runtime.execute("""
        local path = "/Engine/Transient.GameEngine_1:MainMenu_Card_Top_C_11"
        local guide = Obj("EndTextBlock", path .. ".WidgetTree.Txt_Guide",
                          {Text = "Change Deck"})
        top_deck = Obj("MainMenu_Card_Top_C", path,
                       {Visibility = 0, Txt_Guide = guide})
        all_of["MainMenu_Card_TopNPC_C"] = {}
        all_of["MainMenu_Card_Top_C"] = {top_deck}
    """)
    # A cursor-only request has no cached deck root. It must recover the
    # visible concrete top instead of returning a seemingly inactive screen.
    state, reason = sim.api.serve("match", "fresh-deck-1", "cursor", 7)
    assert reason is None
    assert state.deck_screen.observed and state.deck_screen.shown
    assert state.screen == "deck_select"
    assert not state.probe_idle

    # The recovered root remains valid on a second ticket.
    state, reason = sim.api.serve("match", "fresh-deck-2", "cursor", 8)
    assert reason is None and state.deck_screen.observed


def _cached_manager_sim():
    sim = ProbeSim("slot_parent")
    sim.runtime.execute(BOARD_MOCKS)
    sim.runtime.execute('all_of["EndCardGame3DManager"] = {manager}')
    assert sim.api.find_manager()["__address"] == sim.g.manager["__address"]
    sim.runtime.execute("""
        function FF7RResolveObjectPath(path) return INVALID, false end
    """)
    return sim


def test_lost_cached_manager_reacquires_before_absent_manager_cooldown() -> None:
    sim = _cached_manager_sim()
    before = sim.g.find_all_calls
    sim.g.advance(1)
    assert sim.api.find_manager()["__address"] == sim.g.manager["__address"]
    assert sim.g.find_all_calls == before + 1


def test_lost_manager_path_survives_short_lookup_cooldown() -> None:
    sim = _cached_manager_sim()
    before = sim.g.find_all_calls
    assert sim.api.find_manager() is None
    assert sim.g.find_all_calls == before
    sim.g.advance(1)
    assert sim.api.find_manager()["__address"] == sim.g.manager["__address"]
    assert sim.g.find_all_calls == before + 1


def test_failed_reacquisition_returns_to_absent_manager_cooldown() -> None:
    sim = _cached_manager_sim()
    sim.runtime.execute('all_of["EndCardGame3DManager"] = {}')
    sim.g.advance(1)
    assert sim.api.find_manager() is None
    before = sim.g.find_all_calls
    sim.g.advance(1)
    assert sim.api.find_manager() is None
    assert sim.g.find_all_calls == before
    sim.g.advance(4)
    assert sim.api.find_manager() is None
    assert sim.g.find_all_calls == before + 1


def test_cursor_probe_rejects_unrelated_visible_top_guide() -> None:
    sim = ProbeSim("slot_parent")
    sim.runtime.execute("""
        local path = "/Engine/Transient.GameEngine_1:MainMenu_Card_Top_C_12"
        local guide = Obj("EndTextBlock", path .. ".WidgetTree.Txt_Guide",
                          {Text = "Open Settings"})
        top_deck = Obj("MainMenu_Card_Top_C", path,
                       {Visibility = 0, Txt_Guide = guide})
        all_of["MainMenu_Card_TopNPC_C"] = {}
        all_of["MainMenu_Card_Top_C"] = {top_deck}
    """)
    state, reason = sim.api.serve("match", "unrelated-top", "cursor", 7)
    assert reason is None
    assert state.probe_idle


@pytest.mark.parametrize("variant", ["slot_parent", "tree_path"])
def test_marker_follows_pnl_mulligan_visibility_across_toggles(variant: str) -> None:
    sim = ProbeSim(variant)
    assert sim.scan() == (True, [])

    sim.set_visibility(1, 0)  # Visible
    sim.set_visibility(3, 0)
    sim.set_visibility(5, 0)
    assert sim.scan() == (True, [0, 2, 4])

    sim.set_visibility(3, 1)  # Collapsed
    assert sim.scan() == (True, [0, 4])


def test_ui_discovery_rejects_hidden_objects_and_hidden_parents() -> None:
    sim = ProbeSim("slot_parent")
    ui_path = "/Game/Menu/MiniGame/CardGame/CardGame_Top.CardGame_Top_C"
    sim.runtime.execute(
        f'ui_root = Obj("CardGame_Top_C", "{ROOT_PATH.replace("CardGame_Test_C_7", "CardGame_Top_C_9")}", '
        '{Visibility = 0, InViewport = true})\n'
        f'registry["{ui_path}"] = ui_root'
    )

    found, ui_class = sim.api.find_ui()
    assert found is not None
    assert ui_class == "StaticFindObject:" + ui_path

    # The UObject remains valid, but hiding the cached root must invalidate it
    # immediately instead of returning it during the one-second scan throttle.
    sim.runtime.execute("ui_root.__props.Visibility = 1")
    found, _ = sim.api.find_ui()
    assert found is None

    sim.runtime.execute(
        'hidden_parent = Obj("CardGame_Top_C", "/Temp/CardScreen", {Visibility = 1, InViewport = true})\n'
        'tree = Obj("WidgetTree", "/Temp/CardScreen.WidgetTree", {Visibility = 0, Outer = hidden_parent})\n'
        'child = Obj("CardGame_ConfirmWindow_C", "/Temp/CardScreen.WidgetTree.Confirm", '
        '{Visibility = 0, Parent = hidden_parent, Outer = tree})'
    )
    assert sim.api.is_live_ui(sim.g.child) is False

    sim.runtime.execute("hidden_parent.__props.Visibility = 0")
    assert sim.api.is_live_ui(sim.g.child) is True


def test_slot_parent_route_is_reported_in_diagnostics() -> None:
    sim = ProbeSim("slot_parent")
    sim.scan()
    visuals = sim.slot_visuals(1)
    assert visuals.panel_source == "CardFull.Img_Mulligan_Icon.Slot.Parent"
    assert visuals.panel_path == f"{ROOT_PATH}.WidgetTree.CardFull_01.WidgetTree_3.Pnl_Mulligan"
    assert visuals.resolution.property == "invalid"
    deck = list(sim.api.probe().deck_card_paths.values())
    assert deck[0] == f"{ROOT_PATH}.WidgetTree.CardFull_01"
    assert len(deck) == 5


@pytest.mark.parametrize("variant", ["wrong_parent", "none"])
def test_unresolved_panel_stays_unknown_never_unmarked(variant: str) -> None:
    sim = ProbeSim(variant)
    assert sim.scan() == (False, [])
    visuals = sim.slot_visuals(1)
    assert visuals.available is False
    if variant == "wrong_parent":
        assert visuals.resolution.Img_Mulligan_Icon == "name:Pnl_Other"


def test_deck_card_diagnostic_never_touches_a_missing_member() -> None:
    sim = ProbeSim("slot_parent")
    sim.runtime.execute(
        f'top = Obj("CardGame_Top_C", "{ROOT_PATH.replace("CardGame_Test_C_7", "CardGame_Top_C_9")}", {{}})'
    )
    sim.g.advance(2)
    sim.api.scan(sim.g.top, False)
    assert sim.g.nullptr_calls == 0
    assert sim.scan() == (True, [])
    assert sim.g.nullptr_calls == 0
    assert len(list(sim.api.probe().deck_card_paths.values())) == 5


def test_repeat_scans_reuse_cached_widgets_but_see_marker_changes() -> None:
    sim = ProbeSim("slot_parent")
    assert sim.scan() == (True, [])
    first_scan_calls = sim.g.find_all_calls
    assert first_scan_calls > 0

    sim.set_visibility(2, 0)
    assert sim.scan() == (True, [1])
    sim.set_visibility(2, 1)
    assert sim.scan() == (True, [])
    # Two further scans inside the refresh window: no full object scans.
    assert sim.g.find_all_calls == first_scan_calls

    sim.g.advance(10)
    sim.scan()
    assert sim.g.find_all_calls > first_scan_calls


def test_cursor_follows_cached_hand_actors_between_manager_samples() -> None:
    sim = ProbeSim("slot_parent")
    sim.runtime.execute(
        'actors = {}\n'
        'for i = 1, 5 do actors[i] = Obj("EndCardGame3DCardHand", "/Game/L.L:PersistentLevel.Hand_" .. i,'
        ' {CardIndex = i - 1, bCursorVisible = (i == 1)}) end\n'
        'manager = Obj("EndCardGame3DManager", "/Game/L.L:PersistentLevel.EndCardGame3DManager", {CardHandActors = actors})'
    )
    sim.api.manager_hand(sim.g.manager)
    assert sim.api.cursor() == 0

    # The manager is not re-read; only the cached actors' flags change.
    sim.runtime.execute("actors[1].__props.bCursorVisible = false; actors[3].__props.bCursorVisible = true")
    sim.api.refresh_cursor()
    assert sim.api.cursor() == 2

    # Two visible cursors are ambiguous: fail closed without losing the cache.
    sim.runtime.execute("actors[4].__props.bCursorVisible = true")
    sim.api.refresh_cursor()
    assert sim.api.cursor() is None
    sim.runtime.execute("actors[3].__props.bCursorVisible = false")
    sim.api.refresh_cursor()
    assert sim.api.cursor() == 3

    # A destroyed actor drops the cache; the cursor keeps its last value.
    sim.runtime.execute("actors[2].__valid = false; actors[4].__props.bCursorVisible = false")
    sim.api.refresh_cursor()
    assert sim.api.cursor() == 3


def test_deal_time_default_visibility_is_unknown_not_marked() -> None:
    sim = ProbeSim("slot_parent")
    # Live: right after Begin Match every panel read SelfHitTestInvisible (4)
    # before the Card_Normal/Card_Mulligan animations had played.
    for slot in range(1, 6):
        sim.set_visibility(slot, 4)
    assert sim.scan() == (False, [])
    assert sim.slot_visuals(1).visibility == "4"
    for slot in range(1, 6):
        sim.set_visibility(slot, 1)
    assert sim.scan() == (True, [])


BOARD_MOCKS = r"""
local ArrMT = {__index = {GetArrayNum = function(self) return #self end}}
function Arr(n)
  local items = {}
  for i = 1, n do items[i] = Obj("Flag", "/Game/L.L:PersistentLevel.Flag_" .. i, {}) end
  return setmetatable(items, ArrMT)
end
function set_cells(spec, highlight)
  -- spec: cell index (0-based) -> "P2" / "E1"
  local infos = {}
  for index = 0, 14 do
    local text = spec[index] or ""
    local players = text:sub(1, 1) == "P" and tonumber(text:sub(2)) or 0
    local opponents = text:sub(1, 1) == "E" and tonumber(text:sub(2)) or 0
    infos[index + 1] = {FlagPlayers = Arr(players), FlagOpponents = Arr(opponents), bCursorVisible = highlight[index] == true}
  end
  manager.__props.FieldInfos = infos
end
function set_actors(occupied)
  local actors = {}
  for index = 0, 14 do
    actors[index + 1] = occupied[index] and Obj("BoardCard", "/Temp/W.W:PersistentLevel.BoardCard_" .. index, {CardIndex = index}) or INVALID
  end
  manager.__props.CardBoardActors = actors
end
manager = Obj("EndCardGame3DManager", "/Game/C.C:PersistentLevel.EndCardGame3DManager", {
  CardPlacementActor = INVALID, TokenPlayerCounts = {0, 0, 0}, TokenEnemyCounts = {0, 0, 0},
})
"""


def _board(sim) -> dict:
    board = sim.api.match_board(sim.g.manager)
    cells = {(int(c.row), int(c.column)): c for c in board.cells.values()}
    return board, cells


def test_match_board_tracks_pawns_preview_target_and_card_owners() -> None:
    sim = ProbeSim("slot_parent")
    sim.scan()  # caches the CardGame_Test_C root the board faces hang off
    sim.runtime.execute(BOARD_MOCKS)
    sim.runtime.execute(
        "local art = '/Game/Menu/Billboard/Common/U_Com_Billboard_CardGame_Card_024.U_Com_Billboard_CardGame_Card_024'; "
        "local small = Obj('CardGame_Card_Small_C', root.__path .. '.WidgetTree.CardFrame_06.WidgetTree.CardSmall', {"
        "  Billboard_Thumbnail = Obj('EndBillboard', root.__path .. '.B', {ImagePath = {AssetPathName = art}}),"
        "  NumLabel = Obj('EndNumberLabel', root.__path .. '.N', {Value = 3})}); "
        "root.__props.CardFrame_06 = Obj('CardGame_CardFrame_C', root.__path .. '.WidgetTree.CardFrame_06', {CardSmall = small})"
    )
    start = "{[0]='P1',[5]='P1',[10]='P1',[4]='E1',[9]='E1',[14]='E1'}"
    sim.runtime.execute(f"set_cells({start}, {{[0]=true,[5]=true,[10]=true}}); set_actors({{}})")
    board, cells = _board(sim)
    assert (cells[(1, 0)].owner, cells[(1, 0)].rank, cells[(1, 0)].highlight) == ("player", 1, True)
    assert (cells[(0, 4)].owner, cells[(0, 4)].rank) == ("opponent", 1)
    assert cells[(1, 2)].owner == "empty"
    assert board.placement.active is False

    # Hovering the picked-up card over (1,0): the flags become a preview.
    preview = "{[0]='P2',[6]='P1',[10]='P2',[4]='E1',[9]='E1',[14]='E1'}"
    sim.runtime.execute(
        "manager.__props.CardPlacementActor = Obj('Placement', '/Temp/W.W:PersistentLevel.Place', {});"
        f"set_cells({preview}, {{[0]=true,[5]=true,[10]=true}})"
    )
    board, cells = _board(sim)
    assert board.placement.active is True
    assert (board.placement.target_row, board.placement.target_column) == (1, 0)
    assert (cells[(1, 0)].owner, cells[(1, 0)].rank) == ("player", 1)  # pre-preview state
    assert cells[(1, 1)].owner == "empty"

    # Placed: the card's owner is the pre-placement owner of its cell.
    sim.runtime.execute(
        "manager.__props.CardPlacementActor = INVALID; set_actors({[5]=true});"
        f"set_cells({preview}, {{}}); manager.__props.TokenPlayerCounts = {{0, 1, 0}}"
    )
    board, cells = _board(sim)
    assert (cells[(1, 0)].owner, cells[(1, 0)].card_present, cells[(1, 0)].rank) == ("player", True, 0)
    assert (cells[(1, 0)].card_id, int(cells[(1, 0)].power)) == ("card_id_024", 3)
    assert (cells[(0, 0)].owner, cells[(0, 0)].rank) == ("player", 2)
    assert int(board.lanes[2].player_power) == 1

    # The opponent places on its own pawn cell (0,4).
    after = "{[0]='P2',[6]='P1',[10]='P2',[3]='E1',[9]='E2',[14]='E1'}"
    sim.runtime.execute(f"set_actors({{[4]=true,[5]=true}}); set_cells({after}, {{}})")
    board, cells = _board(sim)
    assert (cells[(0, 4)].owner, cells[(0, 4)].card_present) == ("opponent", True)
    assert cells[(1, 0)].owner == "player"


def _face_recovery_sim():
    sim = ProbeSim("slot_parent", catalogue=True)
    sim.scan()
    sim.runtime.execute(BOARD_MOCKS)
    sim.runtime.execute(r'''
      function face(root, id, power)
        local path = root.__path .. ".WidgetTree.CardFrame_06"
        local art = "U_Com_Billboard_CardGame_Card_" .. id
        root.__props.CardFrame_06 = Obj("CardGame_CardFrame_C", path, {CardSmall =
          Obj("CardGame_Card_Small_C", path .. ".CardSmall", {
            Billboard_Thumbnail = Obj("EndBillboard", path .. ".B" .. id,
              {ImagePath = {AssetPathName = "/Game/Menu/" .. art .. "." .. art}}),
            NumLabel = Obj("EndNumberLabel", path .. ".N" .. id, {Value = power})})})
      end
      face(root, "024", 3)
      set_cells({[5]='P1'}, {}); set_actors({})
    ''')
    _board(sim)  # Pre-placement owner evidence, kept independently of UI faces.
    sim.runtime.execute("set_cells({}, {}); set_actors({[5]=true})")
    return sim


def test_board_reacquires_root_and_faces_after_native_path_cache_misses():
    sim = _face_recovery_sim()
    assert _board(sim)[1][(1, 0)].card_id == "card_id_024"
    sim.runtime.execute(r'''
      prototype = Obj("CardGame_Test_C", "/Game/Menu/CardGame_Test.CardGame_Test_C", {})
      all_of["CardGame_Test_C"] = {prototype, root}
      remembered = {}
      function FF7RResolveObjectPath(path)
        return remembered[path] and registry[path] or INVALID, false
      end
      function FF7RRememberObjectPath(path, address) remembered[path] = true end
    ''')
    scans = sim.g.find_all_calls
    board, cells = _board(sim)
    assert sim.g.find_all_calls == scans + 1
    assert (cells[(1, 0)].owner, cells[(1, 0)].card_id, cells[(1, 0)].power) == ("player", "card_id_024", 3)
    # Recovered paths are reused; power is still re-read, never remembered.
    sim.runtime.execute('root.__props.CardFrame_06.__props.CardSmall.__props.NumLabel.__props.Value = 4')
    assert _board(sim)[1][(1, 0)].power == 4
    assert sim.g.find_all_calls == scans + 1


def test_recreated_ui_root_cannot_reuse_faces_from_the_previous_object():
    sim = _face_recovery_sim()
    assert _board(sim)[1][(1, 0)].card_id == "card_id_024"
    sim.runtime.execute(r'''
      root = Obj("CardGame_Test_C", root.__path, {})
      face(root, "006", 2)
      all_of["CardGame_Test_C"] = {root}
    ''')
    cell = _board(sim)[1][(1, 0)]
    assert (cell.card_id, cell.power) == ("card_id_006", 2)


@pytest.mark.parametrize("inventory", ["absent", "ambiguous", "over_bound", "prototype"])
def test_unresolved_board_root_remains_unknown_and_recovery_is_throttled(inventory):
    sim = _face_recovery_sim()
    _board(sim)
    sim.runtime.execute(r'''
      function FF7RResolveObjectPath(path) return INVALID, false end
      prototype = Obj("CardGame_Test_C", "/Game/Menu/CardGame_Test.CardGame_Test_C", {})
      second = Obj("CardGame_Test_C", "/Engine/Transient.GameEngine_1:CardGame_Test_C_8", {})
    ''')
    setup = {
        "absent": 'all_of["CardGame_Test_C"] = {}',
        "ambiguous": 'all_of["CardGame_Test_C"] = {root, second}',
        "over_bound": 'all_of["CardGame_Test_C"] = {root, prototype, prototype, prototype, prototype, prototype, prototype, prototype, second}',
        "prototype": 'all_of["CardGame_Test_C"] = {prototype}',
    }
    sim.runtime.execute(setup[inventory])
    scans = sim.g.find_all_calls
    board, cells = _board(sim)
    assert sim.g.find_all_calls == scans + 1
    assert cells[(1, 0)].card_present and cells[(1, 0)].owner == "player"
    assert cells[(1, 0)].card_id is None and cells[(1, 0)].power is None
    assert list(board.placement.frame_cursor_visibility.values()) == [-1] * 15
    for _ in range(10):
        _board(sim)
    assert sim.g.find_all_calls == scans + 1
    sim.g.advance(1)
    _board(sim)
    assert sim.g.find_all_calls == scans + 2


def _paused_board() -> ProbeSim:
    sim = ProbeSim("slot_parent")
    sim.api.serve("match", "initial", "cursor", 1)
    sim.runtime.execute(BOARD_MOCKS)
    sim.runtime.execute("set_cells({[5]='P1',[4]='E1',[10]='P1'}, {}); set_actors({})")
    _board(sim)
    sim.runtime.execute("set_cells({[10]='P1'}, {}); set_actors({[5]=true,[4]=true})")
    _, cells = _board(sim)
    assert cells[(1, 0)].owner == "player" and cells[(0, 4)].owner == "opponent"
    sim.runtime.execute("""
        local real_open = io.open
        io.open = function(path, mode)
            if path:match('queen_blood%.request$') then
                if request_marker == nil then return nil end
                return {read = function() return request_marker end, close = function() end}
            end
            return real_open(path, mode)
        end
    """)
    return sim


def _expire_board(sim: ProbeSim) -> None:
    sim.g.request_marker = "999 match old-ticket full\n"
    before = sim.g.find_all_calls
    state, reason = sim.api.serve_marker(2)
    assert reason is None
    assert state.probe_idle and state.probe_scope == "idle"
    # Repeated idle ticks must neither discard the suspended ownership nor
    # discover/dereference UObjects while the request is expired.
    assert sim.api.serve_marker(3)[1] == "awaiting_request"
    assert sim.g.find_all_calls == before


def _resume_board(sim: ProbeSim):
    sim.g.request_marker = "1120 match resumed-ticket cursor\n"
    sim.api.serve_marker(4)
    return _board(sim)[1]


def test_expired_match_lease_preserves_owners_of_the_same_live_cards() -> None:
    sim = _paused_board()
    _expire_board(sim)
    cells = _resume_board(sim)
    assert cells[(1, 0)].owner == "player" and cells[(0, 4)].owner == "opponent"
    assert cells[(2, 0)].owner == "player" and int(cells[(2, 0)].rank) == 1


@pytest.mark.parametrize("marker", ["0\n", "invalid\n", None,
                                      "999 match malformed\n", "1120 dialogue new full\n"])
def test_release_or_other_lifecycle_discards_suspended_owners(marker) -> None:
    sim = _paused_board()
    _expire_board(sim)
    sim.g.request_marker = marker
    sim.api.serve_marker(3)
    cells = _resume_board(sim)
    assert cells[(1, 0)].owner == "unknown" and cells[(0, 4)].owner == "unknown"


@pytest.mark.parametrize("change", [
    "manager.__address = manager.__address + 100",
    "manager.__path = '/Temp/NewMatch.Manager'",
    "manager.__address = nil",
])
def test_resuming_a_different_or_unidentified_manager_does_not_restore_owners(change) -> None:
    sim = _paused_board()
    _expire_board(sim)
    sim.runtime.execute(change)
    cells = _resume_board(sim)
    assert cells[(1, 0)].owner == "unknown" and cells[(0, 4)].owner == "unknown"


def test_card_recreated_under_the_same_path_does_not_inherit_suspended_owner() -> None:
    sim = _paused_board()
    _expire_board(sim)
    sim.runtime.execute("""
        local actors = manager.__props.CardBoardActors
        actors[6] = Obj('BoardCard', actors[6].__path, {CardIndex = 5})
        actors[11] = Obj('BoardCard', '/Temp/NewCard', {CardIndex = 10})
        set_cells({}, {})
    """)
    cells = _resume_board(sim)
    assert cells[(1, 0)].owner == "unknown" and cells[(2, 0)].owner == "unknown"
    assert cells[(0, 4)].owner == "opponent"


def test_manager_hand_orders_by_position_and_reads_identity_from_pool_widget() -> None:
    sim = ProbeSim("slot_parent")
    sim.scan()  # reads identities from the CardFull pool widgets
    sim.runtime.execute(
        "actors = {}\n"
        "local pool = {1, 3, 0, 2, 4, -1, -1}\n"
        "for i = 1, #pool do actors[i] = Obj('Hand', '/Temp/W.W:PersistentLevel.Hand_' .. i,"
        " {CardIndex = pool[i], bCursorVisible = (i == 2), bChooseableVisible = (pool[i] ~= 2)}) end\n"
        "manager = Obj('EndCardGame3DManager', '/Game/C.C:PersistentLevel.M', {CardHandActors = actors})"
    )
    hand = sim.api.sampled_manager_hand(sim.g.root, sim.g.manager)
    cards = [hand[i] for i in range(1, len(hand) + 1)]
    # HAND = ["004", "006", "007", "000", "010"] by pool widget 0..4.
    assert [c.card_id for c in cards] == [
        "card_id_006", "card_id_000", "card_id_004", "card_id_007", "card_id_010",
    ]
    assert [int(c.hand_index) for c in cards] == [0, 1, 2, 3, 4]
    assert [c.playable for c in cards] == [True, True, True, False, True]
    assert sim.api.cursor() == 1


def test_spawned_token_cards_resolve_through_the_art_index() -> None:
    # Live 2026-09-25: Moogle Trio added Moogle Mage and Moogle Bard to the
    # hand; their Card_Token_035/036 art has no three-digit number, so the
    # cards came through with no identity and broke the state parse.
    sim = ProbeSim("slot_parent", hand=["118", "006", "Token_003", "Token_035", "Token_036"], catalogue=True)
    sim.scan()
    sim.runtime.execute(
        "actors = {}\n"
        "for i = 1, 5 do actors[i] = Obj('Hand', '/Temp/W.W:PersistentLevel.Hand_' .. i,"
        " {CardIndex = i - 1, bCursorVisible = (i == 1), bChooseableVisible = true}) end\n"
        "manager = Obj('EndCardGame3DManager', '/Game/C.C:PersistentLevel.M', {CardHandActors = actors})"
    )
    hand = sim.api.sampled_manager_hand(sim.g.root, sim.g.manager)
    cards = [hand[i] for i in range(1, len(hand) + 1)]
    assert [c.card_id for c in cards] == [
        "card_id_118", "card_id_006", "card_id_Token_003", "card_id_Token_035", "card_id_Token_036",
    ]
    assert [c.name for c in cards][3:] == ["Moogle Mage", "Moogle Bard"]
    assert all(c.cost is not None and c.power is not None for c in cards)


DECK_SCREEN = r"""
local function card(path, art)
  return Obj("MainMenu_Card_Card_Full_C", path, {
    Visibility = 0,
    Billboard_Thumbnail = Obj("EndBillboard", path .. ".B", {ImagePath = {AssetPathName =
      "/Game/Menu/Billboard/Common/" .. art .. "." .. art}}),
    NumLabel = Obj("EndNumberLabel", path .. ".N", {Value = 1}),
  })
end
local rows = {}
local arts = {
  {"U_Com_Billboard_CardGame_Card_000", "U_Com_Billboard_CardGame_Card_000", "U_Com_Billboard_CardGame_Card_002", "U_Com_Billboard_CardGame_Card_002", "U_Com_Billboard_CardGame_Card_006"},
  {"U_Com_Billboard_CardGame_Card_006", "U_Com_Billboard_CardGame_Card_007", "U_Com_Billboard_CardGame_Card_007", "U_Com_Billboard_CardGame_Card_068", "U_Com_Billboard_CardGame_Card_117"},
  {"U_Com_Billboard_CardGame_Card_004", "U_Com_Billboard_CardGame_Card_010", "U_Com_Billboard_CardGame_Card_010", "U_Com_Billboard_CardGame_Card_118", "U_Com_Billboard_CardGame_Card_Token_035"},
}
for r = 1, 3 do
  local cells = {}
  for c = 1, 5 do cells[c] = card("/Temp/Deck.Row" .. r .. ".Cell" .. c, arts[r][c]) end
  rows[r] = Obj("MainMenu_Card_Card_Full_Unit_C", "/Temp/Deck.Row" .. r, {
    EndVirtualScrollBox_103 = Obj("EndVirtualScrollBox", "/Temp/Deck.Row" .. r .. ".Box", {
      ItemCount = 5, ContentWidgets = cells,
      GetSelectedIndex = function(self, user) return (r == 2) and 3 or -1 end,
    }),
  })
end
local deck = Obj("MainMenu_Card_List_C", "/Temp/Top.Deck", {
  Visibility = 0,
  End_Text = Obj("EndTextBlock", "/Temp/Top.Deck.T", {Text = "Deck #1"}),
  Txt_TotalNumber = Obj("EndTextBlock", "/Temp/Top.Deck.N", {Text = "15/15"}),
  Virtual_Scroll_Box_Card = Obj("EndVirtualScrollBox", "/Temp/Top.Deck.Box", {
    ItemCount = 3, ContentWidgets = rows,
    GetSelectedIndex = function(self, user) return 1 end,
  }),
})
local top = Obj("MainMenu_Card_Top_C", "/Temp/Top", {Visibility = 0, CardList_Player = deck,
  Card_EditListNew = Obj("MainMenu_Card_EditListNew_C", "/Temp/Top.Edit", {Visibility = 1})})
all_of["MainMenu_Card_TopNPC_C"] = {Obj("MainMenu_Card_TopNPC_C", "/Temp/TopNPC", {MainMenu_Card_Top = top})}
local census = {}
for r = 1, 3 do for c = 1, 5 do census[#census + 1] = rows[r].EndVirtualScrollBox_103.ContentWidgets[c] end end
all_of["MainMenu_Card_Card_Full_C"] = census
local billboards = {}
for i, cell in ipairs(census) do billboards[i] = cell.Billboard_Thumbnail end
all_of["EndBillboard"] = billboards
"""


def test_deck_screen_reports_the_current_deck_and_cursor() -> None:
    sim = ProbeSim("slot_parent", catalogue=True)
    sim.runtime.execute(DECK_SCREEN)
    screen = sim.api.deck_screen()
    assert screen.observed is True and screen.shown is True
    assert screen.windows["CardList_Player"] is True and screen.windows["Card_EditListNew"] is False
    deck = screen.deck
    assert (deck.title, deck.total) == ("Deck #1", "15/15")
    assert (int(deck.list.item_count), int(deck.list.selected)) == (3, 1)
    rows = [deck.list.rows[i] for i in range(1, len(deck.list.rows) + 1)]
    cards = [row.rows[j].card_id for row in rows for j in range(1, len(row.rows) + 1)]
    assert len(cards) == 15 and cards[0] == "card_id_000" and cards[-1] == "card_id_Token_035"
    assert int(rows[1].selected) == 3
    assert rows[2].rows[5].name == "Moogle Mage"
    boards = [screen.billboards[i] for i in range(1, len(screen.billboards) + 1)]
    assert boards == [] or all(b.path for b in boards)
    census = [screen.census.cells[i] for i in range(1, len(screen.census.cells) + 1)]
    assert len(census) == 15 and census[0].path == "/Temp/Deck.Row1.Cell1"
    assert census[-1].card_id == "card_id_Token_035" and not census[0].cursor
    assert int(sim.runtime.eval("nullptr_calls")) == 0


TALK_MENU = r"""
local function answer(name, left)
  return Obj("TalkMenu_Cell_00_C", "/Temp/Talk." .. name, {
    Visibility = 0,
    RenderTransform = {Translation = {X = 0, Y = 0}, Angle = 0},
    Slot = Obj("CanvasPanelSlot", "/Temp/Talk." .. name .. ".Slot", {LayoutData = {Offsets = {Left = left, Top = 40}}}),
  })
end
all_of["TalkMenu_00_C"] = {Obj("TalkMenu_00_C", "/Temp/Talk", {
  Visibility = 0, InViewportEx = true,
  Answer1 = answer("Answer1", -160), Answer2 = answer("Answer2", 160),
  Cursor_00 = Obj("TalkMenu_Cursor_00_C", "/Temp/Talk.Cursor", {Visibility = 0,
    RenderTransform = {Translation = {X = 0, Y = 0}, Angle = 90}}),
})}
"""


def test_talk_menu_reports_the_choice_wheel_without_touching_missing_answers() -> None:
    # The "Challenge to a match?" wheel: confirming is only safe while it is
    # shown, and the cursor angle says which answer is highlighted.
    sim = ProbeSim("slot_parent")
    sim.runtime.execute(TALK_MENU)
    menu = sim.api.talk_menu()
    assert menu.observed is True and menu.shown is True
    answers = [menu.answers[i] for i in range(1, len(menu.answers) + 1)]
    assert [(int(a.index), a.shown, int(a.left)) for a in answers] == [(1, True, -160), (2, True, 160)]
    assert int(menu.cursor.angle) == 90 and menu.cursor_shown is True
    assert int(sim.runtime.eval("nullptr_calls")) == 0


def test_talk_menu_recovers_when_an_old_cached_wheel_is_hidden() -> None:
    # A hidden-but-valid widget must not mask Nene's new challenge wheel.
    sim = ProbeSim("slot_parent")
    sim.runtime.execute(TALK_MENU)
    assert sim.api.talk_menu().shown is True
    sim.runtime.execute("""
      local old = all_of["TalkMenu_00_C"][1]
      old.__props.Visibility = 1
      local next_wheel = Obj("TalkMenu_00_C", "/Temp/TalkNew", {
        Visibility = 0, InViewportEx = true,
        Cursor_00 = Obj("TalkMenu_Cursor_00_C", "/Temp/TalkNew.Cursor", {
          Visibility = 0, RenderTransform = {Translation = {X = 0, Y = 0}, Angle = 280}
        }),
      })
      table.insert(all_of["TalkMenu_00_C"], next_wheel)
    """)
    # Discovery is throttled, but the next refresh must choose the live wheel.
    assert sim.api.talk_menu().shown is False
    sim.g.advance(5)
    wheel = sim.api.talk_menu()
    assert wheel.observed is True and wheel.shown is True
    assert int(wheel.cursor.angle) == 280
    assert sim.api.talk_menu().shown is True
    assert int(sim.runtime.eval("nullptr_calls")) == 0


def test_detached_wheel_with_retained_visible_cursor_is_not_shown() -> None:
    sim = ProbeSim("slot_parent")
    sim.runtime.execute(TALK_MENU)
    assert sim.api.talk_menu().shown is True
    sim.runtime.execute('all_of["TalkMenu_00_C"][1].__props.InViewportEx = false')
    wheel = sim.api.talk_menu()
    assert wheel.observed is True and wheel.shown is False
    assert wheel.in_viewport_ex is False and int(wheel.cursor.angle) == 90
    assert int(sim.runtime.eval("nullptr_calls")) == 0


def test_deck_editor_maps_grid_cells_from_the_setup_callbacks() -> None:
    # Live 2026-09-25: the editor's owned-card grid cells are recycled virtual
    # list cells outside the widget tree; only the setup callback sees them.
    sim = ProbeSim("slot_parent", catalogue=True)
    sim.runtime.execute(DECK_SCREEN)
    sim.api.deck_screen()  # registers the editor hooks
    sim.runtime.execute(r"""
      local base = "/Script/EndGame.EndNewCardDeckWindow:"
      local function cell(art, limit)
        return Obj("MainMenu_Card_Card_Full_C", "/Temp/Recycled." .. art, {Visibility = 0,
          Billboard_Thumbnail = Obj("EndBillboard", "/Temp/B." .. art, {ImagePath = {AssetPathName =
            "/Game/Menu/Billboard/Common/" .. art .. "." .. art}}),
          Txt_Limit = Obj("EndTextBlock", "/Temp/L." .. art, {Text = limit})})
      end
      hooks[base .. "OnCardListEditCellSetupItem"](nil, Param(0), Param(cell("U_Com_Billboard_CardGame_Card_000", "0/2")), Param(0))
      hooks[base .. "OnCardListEditCellSetupItem"](nil, Param(0), Param(cell("U_Com_Billboard_CardGame_Card_008", "2/2")), Param(1))
      hooks[base .. "OnCardListEditCellSetupItem"](nil, Param(1), Param(cell("U_Com_Billboard_CardGame_Card_117", "0/1")), Param(0))
      hooks[base .. "OnCardListEditCellSelectedIndexChanged"](nil, Param(0), Param(1))
    """)
    editor = sim.api.deck_screen().editor
    cells = [editor.grids["Edit"][i] for i in range(1, len(editor.grids["Edit"]) + 1)]
    assert [(int(c.row), int(c.index), c.name, c.limit_text) for c in cells] == [
        (0, 0, "Security Officer", "0/2"), (0, 1, "Mu", "2/2"), (1, 0, "Magic Pot", "0/1")]
    assert (int(editor.cursors["Edit"].row), int(editor.cursors["Edit"].index)) == (0, 1)
    assert int(sim.runtime.eval("nullptr_calls")) == 0


@pytest.mark.parametrize("grid", ["Edit", "All", "Player"])
def test_deck_cursor_events_survive_owner_capture_failure(grid: str) -> None:
    sim = ProbeSim("slot_parent", catalogue=True)
    sim.runtime.execute(DECK_SCREEN)
    sim.api.deck_screen()  # registers the editor hooks
    sim.g.test_grid = grid
    sim.runtime.execute(r"""
      local callback = hooks["/Script/EndGame.EndNewCardDeckWindow:OnCardList"
        .. test_grid .. "CellSelectedIndexChanged"]
      local top_index, original_top, editor
      for index = 1, 20 do
        local name, value = debug.getupvalue(callback, index)
        if name == nil then break end
        if name == "deck_screen_top" then top_index, original_top = index, value end
        if name == "deck_editor" then editor = value end
      end
      assert(top_index and editor, "owner capture upvalues must be observed")
      local owner = Obj("EndNewCardDeckWindow", "/Temp/Owner", {})
      callback(Param(owner), Param(0), Param(1))
      local captured_owner, captured_top = editor.owner, editor.owner_top
      assert(captured_owner == "/Temp/Owner" and captured_top == rawget(original_top(), "__path"))
      debug.setupvalue(callback, top_index, function()
        -- Cursor evidence must already be available when owner lookup starts.
        local cursor = editor.cursors[test_grid]
        owner_capture_cursor_fresh = cursor.row == 1 and cursor.index == 2 and cursor.sequence == 2
        error("owner capture failed")
      end)
      local ok, failure = pcall(callback,
        Param(Obj("EndNewCardDeckWindow", "/Temp/OtherOwner", {})), Param(1), Param(2))
      debug.setupvalue(callback, top_index, original_top)
      assert(ok, failure)
      assert(owner_capture_cursor_fresh, "owner lookup ran before cursor bookkeeping")
      assert(editor.owner == captured_owner and editor.owner_top == captured_top)
    """)
    cursor = sim.api.deck_screen().editor.cursors[grid]
    assert (int(cursor.row), int(cursor.index), int(cursor.sequence)) == (1, 2, 2)

    sim.runtime.execute(r"""
      hooks["/Script/EndGame.EndNewCardDeckWindow:OnCardList"
        .. test_grid .. "CellSelectedIndexChanged"](nil, Param(2), Param(3))
    """)
    cursor = sim.api.deck_screen().editor.cursors[grid]
    assert (int(cursor.row), int(cursor.index), int(cursor.sequence)) == (2, 3, 3)


def test_deck_editor_maps_cells_through_row_lists() -> None:
    # Live 2026-09-25 the window's cell-setup callback never fired; each row
    # list's own OnSetupItem does, and row setup names the row lists.
    sim = ProbeSim("slot_parent", catalogue=True)
    sim.runtime.execute(DECK_SCREEN)
    sim.api.deck_screen()
    sim.runtime.execute(r"""
      local base = "/Script/EndGame.EndNewCardDeckWindow:"
      local unit_hook = hooks["/Script/EndGame.EndCardUnitList:OnSetupItem"]
      local function cell(art, limit)
        return Obj("MainMenu_Card_Card_Full_C", "/Temp/R." .. art, {Visibility = 0,
          Billboard_Thumbnail = Obj("EndBillboard", "/Temp/B." .. art, {ImagePath = {AssetPathName =
            "/Game/Menu/Billboard/Common/" .. art .. "." .. art}}),
          Txt_Limit = Obj("EndTextBlock", "/Temp/L." .. art, {Text = limit})})
      end
      local row0 = Obj("MainMenu_Card_CardAll_Full_Unit_C", "/Temp/Unit_A", {})
      local row1 = Obj("MainMenu_Card_CardAll_Full_Unit_C", "/Temp/Unit_B", {})
      hooks[base .. "OnCardListEditRowSetupItem"](nil, Param(row0), Param(0))
      hooks[base .. "OnCardListEditRowSetupItem"](nil, Param(row1), Param(1))
      unit_hook(Param(row0), Param(cell("U_Com_Billboard_CardGame_Card_008", "2/2")), Param(0))
      unit_hook(Param(row1), Param(cell("U_Com_Billboard_CardGame_Card_117", "0/1")), Param(1))
    """)
    editor = sim.api.deck_screen().editor
    cells = [editor.grids["Edit"][i] for i in range(1, len(editor.grids["Edit"]) + 1)]
    assert [(int(c.row), int(c.index), c.name, c.limit_text) for c in cells] == [
        (0, 0, "Mu", "2/2"), (1, 1, "Magic Pot", "0/1")]
    assert int(sim.runtime.eval("nullptr_calls")) == 0


def test_deck_billboards_identify_cards_by_art_and_path() -> None:
    sim = ProbeSim("slot_parent", catalogue=True)
    sim.runtime.execute(DECK_SCREEN)
    sim.runtime.execute(r"""
      all_of["EndBillboard"] = {
        Obj("EndBillboard", "/Temp/MainMenu_Card_Top.WidgetTree.Card_EditListNew.Row_2.Cell_1.Billboard_Thumbnail",
          {Visibility = 0, ImagePath = {AssetPathName = "/Game/Menu/Billboard/Common/U_Com_Billboard_CardGame_Card_008.U_Com_Billboard_CardGame_Card_008"}}),
        Obj("EndBillboard", "/Temp/Field.Sign", {Visibility = 0, ImagePath = {AssetPathName = "/Game/Other.Other"}}),
      }
    """)
    boards = sim.api.deck_screen().billboards
    items = [boards[i] for i in range(1, len(boards) + 1)]
    assert [(b.name, b.path.endswith("Row_2.Cell_1.Billboard_Thumbnail")) for b in items] == [("Mu", True)]
    assert int(sim.runtime.eval("nullptr_calls")) == 0


def test_deck_detail_popup_names_the_hovered_card() -> None:
    # Live 2026-09-26: with Toggle Details (F4) the editor's popup follows the
    # grid cursor; Txt_Num_01 is the card number (UniqueId) and Txt_Name the
    # name key, as read live for Magic Pot (111, $M_SMN_109_atk001).
    sim = ProbeSim("slot_parent", catalogue=True)
    sim.runtime.execute(DECK_SCREEN)
    sim.runtime.execute(r"""
      local root = "/Temp/Top.WidgetTree.Card_ListAll.WidgetTree.Information.WidgetTree."
      all_of["EndTextBlock"] = {
        Obj("EndTextBlock", root .. "Txt_Num_01", {Visibility = 0, Text = "7"}),
        Obj("EndTextBlock", root .. "Txt_Name", {Visibility = 0, Text = "$en_Levrikon_Standard"}),
        Obj("EndTextBlock", "/Temp/Top.WidgetTree.EnemyInformation.WidgetTree.CardInfo.WidgetTree.Txt_Num_01", {Visibility = 0, Text = "68"}),
        Obj("EndTextBlock", "/Temp/Field.Txt_Other", {Visibility = 0, Text = "Levrikon"}),
      }
    """)
    detail = sim.api.deck_screen().detail
    assert detail.hovered.name == "Levrikon" and detail.hovered.card_id == "card_id_006"
    assert int(detail.hovered.unique_id) == 7 and detail.hovered.popup == "Information"
    assert len(detail.texts) == 3 and len(detail.resolved) == 2  # Valron (opponent) is not hovered
    assert int(sim.runtime.eval("nullptr_calls")) == 0


def test_deck_strip_cursor_and_presses_are_recorded_with_sequence() -> None:
    # Live 2026-09-26: Up from grid row 0 enters the deck strip, whose
    # cells show Delete; its cursor and presses must be visible.
    sim = ProbeSim("slot_parent", catalogue=True)
    sim.runtime.execute(DECK_SCREEN)
    sim.api.deck_screen()
    sim.runtime.execute(r"""
      local base = "/Script/EndGame.EndNewCardDeckWindow:"
      hooks[base .. "OnCardListEditCellSelectedIndexChanged"](nil, Param(0), Param(2))
      hooks[base .. "OnCardDeckListSelectedIndexChanged"](nil, Param(3))
      hooks[base .. "OnCardDeckListPressedItem"](nil, Param(3))
    """)
    editor = sim.api.deck_screen().editor
    assert int(editor.cursors["Strip"].index) == 3
    assert int(editor.cursors["Strip"].sequence) > int(editor.cursors["Edit"].sequence)
    assert int(editor.strip_presses[1].index) == 3


def test_dialogue_scope_scans_only_talk_and_preserves_discovery_throttle() -> None:
    sim = ProbeSim("slot_parent", catalogue=True)
    sim.runtime.execute("find_all_classes = {}; find_all_calls = 0")
    for sequence in range(1, 5):
        state = sim.api.scoped_snapshot("dialogue", sequence)
        assert state.probe_idle and state.probe_scope == "dialogue"
        assert not state.reflection.manager_found and len(state.hand) == 0
        sim.g.advance(1)
    assert list(sim.g.find_all_classes.values()) == ["TalkMenu_00_C"]
    sim.g.advance(2)
    sim.api.scoped_snapshot("dialogue", 5)
    assert list(sim.g.find_all_classes.values()) == ["TalkMenu_00_C"] * 2
    sim.api.scoped_snapshot("match", 6)
    assert any(name != "TalkMenu_00_C" for name in sim.g.find_all_classes.values())
    before = sim.g.find_all_calls
    for sequence in range(7, 12):
        state = sim.api.scoped_snapshot(False, sequence)
        assert state.probe_idle and state.probe_scope == "idle"
        sim.g.advance(1)
    assert sim.g.find_all_calls == before


@pytest.mark.parametrize("marker,expected", [
    ("1120\n", "match"), ("1120 dialogue\n", "dialogue"),
    ("1120 match\n", "match"), ("999 dialogue\n", False),
    ("1120 invalid\n", False), ("1120 dialogue extra\n", False), ("invalid\n", False),
])
def test_scoped_marker_is_bounded_and_unknown_modes_remain_idle(marker, expected) -> None:
    sim = ProbeSim("slot_parent")
    sim.g.request_marker = marker
    sim.runtime.execute("""
      local real_open = io.open
      io.open = function(path, mode)
        if path:match('queen_blood%.request$') then
          return {read = function() return request_marker end, close = function() end}
        end
        return real_open(path, mode)
      end
    """)
    assert sim.api.requested() == expected


def test_hovered_card_comes_from_the_visible_popup() -> None:
    # Live 2026-09-26 the grid and the deck strip each have a details popup
    # of the same class; the hidden one keeps its last card.
    sim = ProbeSim("slot_parent", catalogue=True)
    sim.runtime.execute(DECK_SCREEN)
    sim.runtime.execute(r"""
      local grid = "/Temp/Inst.MainMenu_Card_CardInformation_C_1"
      local strip = "/Temp/Inst.MainMenu_Card_CardInformation_C_2"
      registry[grid] = Obj("MainMenu_Card_CardInformation_C", grid, {Visibility = 1})
      registry[strip] = Obj("MainMenu_Card_CardInformation_C", strip, {Visibility = 4})
      all_of["EndTextBlock"] = {
        Obj("EndTextBlock", grid .. ".WidgetTree.Txt_Num_01", {Visibility = 0, Text = "7"}),
        Obj("EndTextBlock", strip .. ".WidgetTree.Txt_Num_01", {Visibility = 0, Text = "8"}),
      }
    """)
    detail = sim.api.deck_screen().detail
    assert detail.hovered.name == "Grasslands Wolf" and detail.hovered.popup_shown is True
