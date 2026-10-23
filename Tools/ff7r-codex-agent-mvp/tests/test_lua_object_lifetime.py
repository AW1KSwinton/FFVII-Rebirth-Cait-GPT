"""Exercise real Lua caches after unload; even IsValid on a retired object fails.

Accesses are counted before raising, so a swallowed pcall is still a failure.
No game or installed mod is loaded and all telemetry I/O is in memory.
"""
from pathlib import Path

import pytest

from test_queen_blood_probe_marker_sim import MOCKS, ProbeSim, DECK_SCREEN, TALK_MENU

LuaRuntime = pytest.importorskip("lupa.lua54").LuaRuntime
TOOLS = Path(__file__).parents[2]
EXPORTER = TOOLS / "ff7r-telemetry-exporter"
REFS = EXPORTER / "Shared/Scripts/object_refs.lua"


def test_request_scoped_actor_facing_and_unreadable_rotation_regression():
    lua = LuaRuntime(unpack_returned_tuples=True)
    lua.globals().arg = lua.table_from([str(EXPORTER / "Base/Scripts/entity_poi.lua"), str(REFS)])
    lua.execute((EXPORTER / "Base/tests/entity_poi_regression.lua").read_text(encoding="utf-8"))

TRAPS = r'''
stale_accesses = 0
local mt = getmetatable(INVALID)
local original = mt.__index
mt.__index = function(self, key)
  if rawget(self, "__retired") then
    stale_accesses = stale_accesses + 1
    error("dangling UObject read: " .. key)
  end
  return original(self, key)
end
function retire(object)
  registry[rawget(object, "__path")] = nil
  rawset(object, "__retired", true)
end
'''


def runtime():
    lua = LuaRuntime(unpack_returned_tuples=True)
    lua.execute(MOCKS + TRAPS)
    lua.globals().refs = lua.execute(REFS.read_text(encoding="utf-8"))
    lua.execute('package.preload.object_refs = function() return refs end')
    return lua


def test_player_controller_replacement_uses_fresh_lookup_without_a_full_scan():
    lua = runtime()
    lua.execute(r'''
      local path = "/Engine/Transient.Controller_1"
      player = Obj("LocalPlayer", "/Engine/Transient.Player", {})
      old = Obj("PlayerController", path, {Player=player})
      all_of.PlayerController = {old}
      assert(refs.GetPlayerController() == old)
      local scans = find_all_calls
      retire(old)
      replacement = Obj("PlayerController", path, {Player=player})
      all_of.PlayerController = {replacement}
      assert(refs.GetPlayerController() == replacement)
      assert(find_all_calls == scans, "path reuse must not rescan the entire object array")
      retire(replacement)
      all_of.PlayerController = {}
      assert(refs.GetPlayerController() == nil)
      assert(stale_accesses == 0)
    ''')


def test_controller_discovery_is_bounded_and_rearms_after_transition():
    lua = runtime()
    lua.execute(r'''
      all_of.PlayerController = {}
      assert(refs.GetPlayerController() == nil)
      local scans = find_all_calls
      assert(refs.GetPlayerController() == nil and find_all_calls == scans)
      advance(2)
      remote = Obj("PlayerController", "/Engine/Transient.Remote", {Player=INVALID})
      player = Obj("LocalPlayer", "/Engine/Transient.Player", {})
      current = Obj("PlayerController", "/Engine/Transient.Current", {Player=player})
      all_of.PlayerController = {remote, current}
      assert(refs.GetPlayerController() == current)
      assert(find_all_calls == scans + 1)
      retire(current)
      all_of.PlayerController = {}
      advance(2)
      assert(refs.GetPlayerController() == nil)
      assert(stale_accesses == 0)
    ''')


def test_native_path_lookup_verifies_identity_without_short_name_scan():
    lua = runtime()
    lua.execute(r'''
      local path = "/Game/EndGame.EndGame:PersistentLevel.Controller_2147482369"
      player = Obj("LocalPlayer", "/Engine/Transient.Player", {})
      old = Obj("PlayerController", path, {Player=player})
      all_of.PlayerController = {old}
      StaticFindObject = function() return INVALID end
      FindObject = function(class, outer, name, exact)
        assert(class == nil and outer == nil and name == path and exact == false)
        return registry[path] or INVALID
      end
      assert(refs.GetPlayerController() == old)
      retire(old)
      replacement = Obj("PlayerController", path, {Player=player})
      all_of.PlayerController = {replacement}
      assert(refs.GetPlayerController() == replacement)
      retire(replacement)
      assert(refs.resolve(path) == nil)
      wrong = Obj("PlayerController", "/Game/Other.Other:PersistentLevel.Controller_2147482369", {Player=player})
      FindObject = function() return wrong end
      assert(refs.resolve(path) == nil, "path lookup must not accept a different object")
      assert(stale_accesses == 0)
    ''')


def test_controller_without_a_valid_player_is_not_selected():
    lua = runtime()
    lua.execute(r'''
      missing = Obj("PlayerController", "/Engine/Transient.Missing", {})
      remote = Obj("PlayerController", "/Engine/Transient.Remote", {Player=INVALID})
      player = Obj("LocalPlayer", "/Engine/Transient.Player", {})
      current = Obj("PlayerController", "/Engine/Transient.Current", {Player=player})
      all_of.PlayerController = {missing, remote, current}
      assert(refs.GetPlayerController() == current)
      current.Player = INVALID
      assert(refs.GetPlayerController() == nil)
      advance(2)
      current.Player = player
      assert(refs.GetPlayerController() == current)
      assert(stale_accesses == 0)
    ''')


def test_controller_world_lookup_refreshes_without_object_array_scan():
    lua = runtime()
    lua.execute(r'''
      local world_path = "/Game/EndGame.EndGame"
      local path = world_path .. ":PersistentLevel.Controller_2147482369"
      local world = Obj("World", world_path, {})
      local player = Obj("LocalPlayer", "/Engine/Transient.Player", {})
      local old = Obj("PlayerController", path, {Player=player, GetWorld=function() return world end})
      current = old
      local gameplay = Obj("GameplayStatics", "/Script/Engine.Default__GameplayStatics", {
        GetPlayerController=function(_, context, index)
          assert(context == world and index == 0)
          return current
        end
      })
      all_of.PlayerController = {old}
      local original = StaticFindObject
      StaticFindObject = function(name)
        if name == path then return INVALID end
        return original(name)
      end
      FindObject = function() error("periodic lookup must not use an object-array scan") end
      assert(refs.GetPlayerController() == old)
      local scans = find_all_calls
      retire(old)
      current = Obj("PlayerController", world_path .. ":PersistentLevel.Controller_2147482368", {Player=player})
      for _=1,20 do assert(refs.GetPlayerController() == current) end
      assert(find_all_calls == scans)
      current.Player = INVALID
      assert(refs.GetPlayerController() == nil)
      assert(stale_accesses == 0)
    ''')


def test_native_identity_resolver_prevents_fallback_scans_and_stale_reads():
    lua = runtime()
    lua.execute(r'''
      local path = "/Game/World.World:PersistentLevel.Controller_2147482369"
      local player = Obj("LocalPlayer", "/Engine/Transient.Player", {})
      local old = Obj("PlayerController", path, {Player=player, GetAddress=function() return 123 end})
      local remembered = 0
      FF7RRememberObjectPath = function(key, address)
        assert(key == path and address == 123)
        remembered = remembered + 1
      end
      FF7RResolveObjectPath = function(key)
        assert(key == path)
        return registry[path] or INVALID, false
      end
      StaticFindObject = function() error("native resolution must not scan") end
      FindObject = StaticFindObject
      all_of.PlayerController = {old}
      assert(refs.GetPlayerController() == old and remembered > 0)
      retire(old)
      local current = Obj("PlayerController", path, {Player=player})
      local scans = find_all_calls
      for _=1,20 do assert(refs.GetPlayerController() == current) end
      assert(find_all_calls == scans)
      retire(current)
      assert(refs.resolve(path) == nil)
      assert(stale_accesses == 0)
    ''')


@pytest.mark.parametrize("empty_result", ["nil", "{}"])
def test_controller_discovery_falls_back_to_controller_class(empty_result):
    lua = runtime()
    lua.globals().empty_result = lua.eval(empty_result)
    lua.execute(r'''
      player = Obj("LocalPlayer", "/Engine/Transient.Player", {})
      controller = Obj("EndPlayerController", "/Engine/Transient.Controller", {Player=player})
      local original = FindAllOf
      FindAllOf = function(class)
        if class == "PlayerController" then return empty_result end
        if class == "Controller" then return {controller} end
        return original(class)
      end
      assert(refs.GetPlayerController() == controller)
      retire(controller)
      assert(refs.GetPlayerController() == nil)
      assert(stale_accesses == 0)
    ''')


def test_cutscene_players_can_unload_between_samples():
    lua = runtime()
    lua.execute(r'''
      files = {}
      io.open = function(path) return {write=function(_, s) files[path]=s end, flush=function() end, close=function() end} end
      local path = "/Game/Old.Old:PersistentLevel.SequencePlayer_1"
      player = Obj("LevelSequencePlayer", path, {IsPlaying=function() return true end})
      all_of.LevelSequencePlayer = {player}
    ''')
    lua.globals().cutscene = lua.execute((EXPORTER / "Base/Scripts/cutscene.lua").read_text(encoding="utf-8"))
    lua.execute(r'''
      cutscene.sample(1, nil, "first")
      retire(player)
      all_of.LevelSequencePlayer = {}
      cutscene.sample(2, nil, "after unload")
      assert(stale_accesses == 0)
      assert(files["C:\\CaitGPT\\Telemetry\\cutscene.state"]:find('"tracked":0', 1, true))
    ''')


def test_base_menu_and_bench_caches_resolve_paths_after_unload():
    lua = runtime()
    lua.execute(r'''
      io.open = function() return nil end
      menu = Obj("MainMenu_Base_C", "/Engine/Transient.Menu", {Visibility=0})
      all_of.MainMenu_Base_C = {menu}
      pawn = Obj("EndCharacter", "/Engine/Transient.Pawn", {GetDistanceTo=function() return 50 end})
      controller = Obj("PlayerController", "/Engine/Transient.Controller", {Pawn=pawn, Player=Obj("LocalPlayer", "/Engine/Transient.Player", {})})
      all_of.PlayerController = {controller}
      bench_class = Obj("Class", "/Script/EndGame.EndFieldActionActorBenchBreak", {})
      bench = Obj("EndFieldActionActorBenchBreak", "/Game/Old.Old:PersistentLevel.Bench", {IsA=function() return true end})
      all_of.EndFieldActionActorBenchBreak = {bench}
    ''')
    source = (EXPORTER / "Base/Scripts/main.lua").read_text(encoding="utf-8")
    lua.globals().base = lua.execute(source + "\nreturn {menu=update_menu_cache_on_game_thread, bench=update_bench_cache_on_game_thread}")
    lua.execute(r'''
      base.menu(); base.bench()
      local scans = find_all_calls
      base.menu(); base.bench()
      assert(find_all_calls == scans, "stable caches must preserve scan cadence")
      retire(menu); retire(bench)
      all_of.MainMenu_Base_C = {}; all_of.EndFieldActionActorBenchBreak = {}
      base.menu(); base.bench()
      assert(stale_accesses == 0)
    ''')


def test_entity_cache_drops_unloaded_actor_without_touching_its_pointer():
    lua = runtime()
    lua.execute(r'''
      files = {["C:\\CaitGPT\\Telemetry\\entity-poi.request"]="visit|1200|*|point|poi"}
      io.open = function(path, mode)
        if mode == "rb" then
          if not files[path] then return nil end
          return {read=function() return files[path] end, close=function() end}
        end
        return {write=function(_, value) files[path]=value end, flush=function() end, close=function() end}
      end
      FName = function(value) return value end
      field = Obj("EndFieldAPI", "/Script/EndGame.Default__EndFieldAPI", {GetEndTargetPointLocation=function() return {X=1000,Y=0,Z=0} end})
      battle = Obj("EndBattleAPI", "/Script/EndGame.Default__EndBattleAPI", {GetLayoutTransform=function() return {Translation={X=1000,Y=0,Z=0}} end})
      pawn = Obj("EndCharacter", "/Game/Old.Old:PersistentLevel.Cloud", {K2_GetActorLocation=function() return {X=900,Y=0,Z=0} end})
      npc = Obj("EndCharacterBase", "/Game/Old.Old:PersistentLevel.NPC", {IsPlayer=function() return false end, K2_GetActorLocation=function() return {X=1000,Y=0,Z=0} end})
      all_of.EndCharacterBase = {npc}
      all_of.EndFieldActionActorBase = {}; all_of.End2FieldActionActorBase = {}
      prompt = function() return nil, nil, nil end
    ''')
    lua.globals().poi = lua.execute((EXPORTER / "Base/Scripts/entity_poi.lua").read_text(encoding="utf-8"))
    lua.execute(r'''
      poi.sample(1,pawn,"Old",prompt,"first")
      assert(files["C:\\CaitGPT\\Telemetry\\entity-poi.1.json"]:find("PersistentLevel.NPC", 1, true))
      retire(npc); all_of.EndCharacterBase = {}
      local scans = find_all_calls
      poi.sample(2,pawn,"New",prompt,"after travel")
      assert(find_all_calls == scans)
      assert(not files["C:\\CaitGPT\\Telemetry\\entity-poi.0.json"]:find("PersistentLevel.NPC", 1, true))
      assert(stale_accesses == 0)
    ''')


def test_hand_widgets_panels_and_cursor_actors_do_not_retain_unloaded_objects():
    sim = ProbeSim("slot_parent")
    sim.runtime.execute(TRAPS)
    assert sim.scan() == (True, [])
    sim.runtime.execute(r'''
      actor = Obj("EndCardGameHandActor", "/Game/Old.Old:PersistentLevel.Hand", {bCursorVisible=true, CardIndex=4})
      manager = Obj("EndCardGame3DManager", "/Game/Old.Old:PersistentLevel.Manager", {CardHandActors={actor}})
    ''')
    sim.api.manager_hand(sim.g.manager)
    sim.api.refresh_cursor()
    sim.runtime.execute(r'''
      retire(actor)
      for _, object in ipairs(cards) do retire(object) end
      for _, object in ipairs(panels) do retire(object) end
      retire(root)
      all_of.CardGame_Test_C = {}; all_of.CardGame_Card_Full_C = {}
    ''')
    sim.api.refresh_cursor()
    sim.g.advance(1)
    sim.api.scan(None, False)
    assert sim.g.stale_accesses == 0
    assert not sim.api.selection().observed


def test_cached_panel_path_follows_a_recreated_panel():
    sim = ProbeSim("slot_parent")
    sim.runtime.execute(TRAPS)
    assert sim.scan() == (True, [])
    sim.runtime.execute(r'''
      local path = rawget(panels[1], "__path")
      retire(panels[1])
      panels[1] = Obj("EndCanvasPanel", path, {Visibility=0})
    ''')
    assert sim.scan() == (True, [0])
    assert sim.g.stale_accesses == 0


def test_deck_screen_owner_and_talk_menu_can_unload_between_ticks():
    sim = ProbeSim("slot_parent")
    sim.runtime.execute(TRAPS + DECK_SCREEN + TALK_MENU)
    sim.runtime.execute(r'''
      owner = Obj("EndNewCardDeckWindow", "/Temp/Top.EndNewCardDeckWindow_1", {})
      all_of.EndNewCardDeckWindow = {owner}
    ''')
    assert sim.api.deck_top() is not None
    assert sim.api.editor_owner() is not None
    assert sim.api.talk_menu().observed
    sim.runtime.execute(r'''
      retire(owner)
      retire(registry["/Temp/Top"]); retire(registry["/Temp/TopNPC"]); retire(registry["/Temp/Talk"])
      all_of.MainMenu_Card_TopNPC_C = {}; all_of.EndNewCardDeckWindow = {}; all_of.TalkMenu_00_C = {}
    ''')
    assert sim.api.deck_top() is None
    assert sim.api.editor_owner() is None
    assert not sim.api.talk_menu().observed
    assert sim.g.stale_accesses == 0


def test_cached_ui_and_manager_paths_are_resolved_again_after_replacement():
    sim = ProbeSim("slot_parent")
    sim.runtime.execute(TRAPS)
    sim.runtime.execute(r'''
      ui = Obj("CardGame_Top_C", "/Engine/Transient.CardGame_Top_C_1", {Visibility=0, InViewport=true})
      manager = Obj("EndCardGame3DManager", "/Game/Old.Old:PersistentLevel.Manager", {})
      all_of.CardGame_Top_C = {ui}; all_of.EndCardGame3DManager = {manager}
    ''')
    assert sim.api.find_ui()[0] is not None
    assert sim.api.find_manager() is not None
    sim.runtime.execute(r'''
      local ui_path, manager_path = rawget(ui, "__path"), rawget(manager, "__path")
      retire(ui); retire(manager)
      ui = Obj("CardGame_Top_C", ui_path, {Visibility=0, InViewport=true})
      manager = Obj("EndCardGame3DManager", manager_path, {})
      all_of.CardGame_Top_C = {ui}; all_of.EndCardGame3DManager = {manager}
    ''')
    scans = sim.g.find_all_calls
    assert sim.api.find_ui()[0] is not None
    assert sim.api.find_manager() is not None
    assert sim.g.find_all_calls == scans
    assert sim.g.stale_accesses == 0


def test_deck_details_refresh_recreated_blocks_without_a_full_scan():
    sim = ProbeSim("slot_parent", catalogue=True, clock=1000)
    sim.runtime.execute(TRAPS)
    sim.runtime.execute(r'''
      local path = "/Temp/MainMenu_Card_Top.Information.WidgetTree.Txt_Num_01"
      block = Obj("EndTextBlock", path, {Visibility=0, Text="7"})
      all_of.EndTextBlock = {block}
    ''')
    assert sim.api.detail().hovered.card_id == "card_id_006"
    sim.runtime.execute(r'''
      local path = rawget(block, "__path")
      retire(block)
      block = Obj("EndTextBlock", path, {Visibility=0, Text="111"})
      all_of.EndTextBlock = {block}
    ''')
    sim.g.test_clock += 0.21
    scans = sim.g.find_all_calls
    assert sim.api.detail().hovered.card_id == "card_id_117"
    assert sim.g.find_all_calls == scans
    assert sim.g.stale_accesses == 0


def test_deck_detail_discovery_primes_native_paths_without_global_lookup():
    sim = ProbeSim("slot_parent", catalogue=True, clock=1000)
    sim.runtime.execute(TRAPS)
    sim.runtime.execute(r'''
      remembered = {}; detail_path_searches = 0
      FF7RRememberObjectPath = function(path, address) remembered[path] = address end
      FF7RResolveObjectPath = function(path)
        local object = registry[path]
        if object and remembered[path] == object:GetAddress() then return object, false end
        return INVALID, true
      end
      StaticFindObject = function() detail_path_searches = detail_path_searches + 1; return INVALID end
      FindObject = function() detail_path_searches = detail_path_searches + 1; return INVALID end
      popup = Obj("Popup", "/Engine/Transient.MainMenu_Card_Top.Information",
                  {Visibility=0, GetAddress=function() return 103 end})
      tree = Obj("WidgetTree", "/Engine/Transient.MainMenu_Card_Top.Information.WidgetTree", {Outer=popup})
      block = Obj("EndTextBlock", "/Engine/Transient.MainMenu_Card_Top.Information.WidgetTree.Txt_Num_01",
                  {Visibility=0, Text="7", Outer=tree, GetAddress=function() return 101 end})
      total = Obj("EndTextBlock", "/Engine/Transient.MainMenu_Card_Top.Txt_TotalNumber",
                  {Visibility=0, Text="15/15", GetAddress=function() return 102 end})
      all_of.EndTextBlock = {block, total}
    ''')
    detail = sim.api.detail()
    assert detail.hovered is not None and detail.hovered.card_id == "card_id_006"
    assert detail.totals[1].text == "15/15"
    scans = sim.g.find_all_calls
    sim.g.test_clock += 0.21
    assert sim.api.detail().hovered.card_id == "card_id_006"
    assert sim.g.find_all_calls == scans
    assert sim.g.detail_path_searches == 0
    assert sim.g.stale_accesses == 0


def test_idle_boundary_clears_hand_observation_without_touching_old_objects():
    sim = ProbeSim("slot_parent")
    sim.runtime.execute(TRAPS)
    assert sim.scan() == (True, [])
    sim.runtime.execute('for _, object in ipairs(cards) do retire(object) end; retire(root)')
    sim.api.idle()
    assert not sim.api.selection().observed
    assert sim.g.stale_accesses == 0


def test_every_modified_lua_source_compiles_in_lua54():
    lua = LuaRuntime(unpack_returned_tuples=True)
    compile_lua = lua.eval("function(s) local f, e=load(s); assert(f,e) end")
    sources = list((EXPORTER / "Base/Scripts").glob("*.lua")) + [
        REFS, EXPORTER / "QueenBlood/Scripts/main.lua",
        TOOLS / "ff7r-navigation-probe/Scripts/main.lua",
        TOOLS / "ff7r-navigation-probe/Scripts/navmesh_path.lua",
    ]
    for path in sources:
        compile_lua(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", ["sampler", "entity_poi", "cutscene"])
def test_existing_exporter_regression_scripts(name):
    lua = LuaRuntime(unpack_returned_tuples=True)
    source = EXPORTER / "Base/Scripts" / ("main.lua" if name == "sampler" else f"{name}.lua")
    lua.globals().arg = lua.table(str(source), str(REFS))
    lua.eval(f'loadfile([[{EXPORTER / "Base/tests" / f"{name}_regression.lua"}]])')()
