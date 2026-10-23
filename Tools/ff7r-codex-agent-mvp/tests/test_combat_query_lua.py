"""Exercise the real Lua producer using synthetic reflected objects and files."""
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import time

import pytest

from ff7r_agent.combat import CombatSnapshot


PRODUCER = Path(__file__).parents[2] / "ff7r-telemetry-exporter/Base/Scripts/combat_query.lua"
HEADERS = Path(__file__).parents[3] / "FF7R2UProj/Source/EndGame/Public"
ROOT = "C:\\CaitGPT\\Telemetry\\"
ID = "a" * 32


@pytest.fixture
def runtime():
    LuaRuntime = pytest.importorskip("lupa.lua54").LuaRuntime
    lua = LuaRuntime(unpack_returned_tuples=True)
    lua.globals().timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lua.execute(r'''
        files, lookups, calls, unavailable = {}, 0, {}, {}
        marker_reads, response_writes = 0, 0
        active, paused, phase_flip, bad_hp, invalid_enemy = true, false, false, false, false
        enemy_mode, array_walks = 'out', 0
        local function counted(name)
            calls[name]=(calls[name] or 0)+1
        end
        world = {IsValid=function() return true end, GetFullName=function() return 'World_1' end}
        local function actor(id, player_type, hp)
            return {id=id, player_type=player_type, hp=hp, max_hp=100, mp=30, max_mp=50,
                IsValid=function(self) return not (invalid_enemy and self.id=='enemy_1') end,
                GetFullName=function(self) return self.id end,
                GetWorld=function() return world end,
                GetDistanceTo=function(self, other) return self.id==other.id and 0 or 350 end}
        end
        pawn=actor('Cloud_1',0,100)
        member2=actor('Tifa_1',1,70)
        enemy=actor('enemy_1',nil,80)
        enemy2=actor('enemy_2',nil,60)
        members={pawn,member2}
        target=enemy
        battle={IsValid=function() return true end,
            IsInBattle=function()
                counted('IsInBattle')
                if phase_flip and calls.IsInBattle % 2==0 then return not active end
                return active
            end,
            GetCharaHP=function(_, actor)
                counted('GetCharaHP')
                if bad_hp then error('fake missing HP getter') end
                return actor.hp
            end,
            GetHPMax=function(_,actor) counted('GetHPMax');return actor.max_hp end,
            GetCharaID=function(_,actor) return actor.id end,
            GetCharacterStateType=function() return 1 end,
            GetTargetCharacter=function() counted('GetTargetCharacter');return target end,
            GetEnemyMembersRef=function(_, output)
                counted('GetEnemyMembersRef')
                if enemy_mode=='out' then output[1]=enemy;return end
                if enemy_mode=='out_two' then output[1]=enemy;output[2]=enemy2;return end
                if enemy_mode=='out_named' then output[1]=enemy;output.OutEnemies=true;return end
                if enemy_mode=='returned' then return {enemy} end
                if enemy_mode=='empty_out' then return end
                if enemy_mode=='opaque' then return 123 end
                local count = enemy_mode=='bounded' and 65 or enemy_mode=='empty_typed' and 0 or 1
                return setmetatable({
                    GetArrayNum=function() return count end,
                    ForEach=function(_, callback)
                        array_walks=array_walks+1
                        for i=1,count do callback(i-1,{get=function() return enemy end}) end
                    end
                }, {__index=function(_,key)
                    if type(key)=='number' then error('native TArray indexing is forbidden') end
                end, __len=function() error('native TArray length is forbidden') end})
            end}
        party={IsValid=function() return true end,
            GetBattleMemberCount=function() counted('GetBattleMemberCount');return #members end,
            GetBattleMember=function(_,i) return members[i+1] end,
            GetBattleMemberType=function(_,i) return members[i+1].player_type end}
        menu={IsValid=function() return true end,
            BPGetPlayerMP=function(_,t) return members[t+1].mp end,
            BPGetPlayerMaxMP=function(_,t) return members[t+1].max_mp end}
        gameplay={IsValid=function() return true end, IsGamePaused=function() return paused end}
        local reflected={IsValid=function() return true end}
        package.preload.object_refs=function() return {resolve=function(path)
            lookups=lookups+1
            if path:find('Default__EndBattleAPI',1,true) then return battle end
            if path:find('Default__EndPartyAPI',1,true) then return party end
            if path:find('Default__EndMenuBPAPI',1,true) then return menu end
            if path:find('Default__GameplayStatics',1,true) then return gameplay end
            local name=path:match(':([^:]+)$')
            if name and not unavailable[name] then return reflected end
        end} end
        io.open=function(path,mode)
            if path:find('combat-query.request',1,true) then marker_reads=marker_reads+1 end
            if mode=='wb' and path:find('combat-query.',1,true) then response_writes=response_writes+1 end
            if mode=='rb' then
                if files[path]==nil then return nil end
                return {read=function(_,count) return files[path]:sub(1,count) end, close=function() return true end}
            end
            files[path]=''
            return {write=function(_,value) files[path]=files[path]..value;return true end,
                    flush=function() return true end,close=function() return true end}
        end
        os.time=function() return 1000 end
        os.date=function() return timestamp end
        os.rename=function(a,b)
            if files[b]~=nil then return false end
            files[b]=files[a];files[a]=nil;return true
        end
        os.remove=function() error('producer must not remove a peer/previous response') end
    ''')
    probe = lua.execute(PRODUCER.read_text(encoding="utf-8"))
    return lua, probe


def sample(runtime, request_id=ID):
    lua, probe = runtime
    probe.set_active(True)  # The base sampler has observed entry before this request.
    lua.globals().files[ROOT + "combat-query.request"] = request_id + "|1010"
    probe.sample(101, lua.globals().pawn, "EndGame")
    return json.loads(lua.globals().files[ROOT + "combat-query." + request_id + ".json"])


def test_producer_idles_without_request_and_answers_only_once(runtime):
    lua, probe = runtime
    probe.set_active(True)
    probe.sample(100, lua.globals().pawn, "EndGame")
    assert lua.globals().lookups == 0
    data = sample(runtime)
    assert data["observed"] and data["in_battle"] and data["complete"] == {"party": True, "enemies": True}
    assert data["party"][0]["hp"] == 100 and data["party"][0]["mp"] == 30
    assert data["party"][0]["atb"] is data["party"][0]["status_effects"] is None
    assert data["target"] == {"observed": True, "actor_id": "enemy_1"}
    assert data["input_sent"] is data["input_authorized"] is False
    CombatSnapshot.from_dict(data)
    count = lua.globals().lookups
    probe.sample(102, lua.globals().pawn, "EndGame")
    assert lua.globals().lookups == count


@pytest.mark.parametrize("request_line", ["short|1010", "A"*32 + "|1010", ID + "|999", ID + "|2000",
                                      ID + "|1010|attack", ID + "|1010\n", "x"*129])
def test_invalid_or_expired_requests_do_not_read_reflected_objects(runtime, request_line):
    lua, probe = runtime
    probe.set_active(True)
    lua.globals().files[ROOT + "combat-query.request"] = request_line
    probe.sample(100, lua.globals().pawn, "EndGame")
    assert lua.globals().lookups == 0


def test_disabled_reader_checks_no_files_or_objects_and_reentry_reads_fresh_data(runtime):
    lua, probe = runtime
    lua.globals().files[ROOT + "combat-query.request"] = ID + "|1010"
    for tick in range(500):
        probe.sample(tick, lua.globals().pawn, "EndGame")
    assert lua.globals().marker_reads == lua.globals().response_writes == lua.globals().lookups == 0
    probe.set_active(True)
    probe.sample(500, lua.globals().pawn, "EndGame")
    assert lua.globals().response_writes == 1 and lua.globals().calls["GetCharaHP"] == 3
    probe.set_active(False)
    counts = lua.globals().marker_reads, lua.globals().lookups, lua.globals().response_writes
    lua.globals().files[ROOT + "combat-query.request"] = "b" * 32 + "|1010"
    for tick in range(501, 1001):
        probe.sample(tick, lua.globals().pawn, "EndGame")
    assert counts == (lua.globals().marker_reads, lua.globals().lookups, lua.globals().response_writes)
    lua.globals().pawn.hp = 42
    probe.set_active(True)
    probe.sample(1001, lua.globals().pawn, "EndGame")
    data = json.loads(lua.globals().files[ROOT + "combat-query." + "b" * 32 + ".json"])
    assert data["party"][0]["hp"] == 42 and lua.globals().response_writes == 2


@pytest.mark.parametrize("active", [None, False, 1, "true"])
def test_only_explicit_true_enables_the_reader(runtime, active):
    lua, probe = runtime
    lua.globals().files[ROOT + "combat-query.request"] = ID + "|1010"
    probe.set_active(active)
    probe.sample(100, lua.globals().pawn, "EndGame")
    assert lua.globals().marker_reads == lua.globals().lookups == 0


def test_existing_base_tick_gates_query_checks_on_entry_exit_and_unknown_state(runtime):
    lua, probe = runtime
    lua.globals().combat_module = probe
    lua.execute('''
        package.preload.combat_query=function() return combat_module end
        local refs=require('object_refs')
        refs.GetPlayerController=function()
            return {IsValid=function() return true end, Pawn=pawn}
        end
        gameplay.GetCurrentLevelName=function() return 'EndGame' end
        print=function() end
        active=false
    ''')
    lua.execute((PRODUCER.parent / "main.lua").read_text(encoding="utf-8"))
    tick = lua.globals().FF7RReadOnlyTick
    lua.globals().files[ROOT + "combat-query.request"] = ID + "|1010"
    for _ in range(100):
        tick()
    assert lua.globals().marker_reads == lua.globals().response_writes == 0
    assert lua.globals().calls["IsInBattle"] == 100  # Only the existing base flag.
    assert lua.globals().calls["GetBattleMemberCount"] is lua.globals().calls["GetCharaHP"] is None
    assert lua.globals().files[ROOT + "state.sample"].split("|")[8] == "0"
    lua.globals().active = True
    tick()
    assert lua.globals().marker_reads == 1 and lua.globals().response_writes == 1
    assert lua.globals().files[ROOT + "state.sample"].split("|")[8] == "1"
    # A new marker is ignored while inactive, and also while battle state is unknown.
    lua.globals().files[ROOT + "combat-query.request"] = "b" * 32 + "|1010"
    lua.globals().active = False
    for _ in range(100):
        tick()
    lua.globals().active = None
    tick()
    assert lua.globals().files[ROOT + "state.sample"].split("|")[8] == "-1"
    assert lua.globals().marker_reads == lua.globals().response_writes == 1
    assert lua.globals().calls["GetCharaHP"] == 3
    lua.globals().active = True
    lua.globals().pawn.hp = 25
    tick()
    data = json.loads(lua.globals().files[ROOT + "combat-query." + "b" * 32 + ".json"])
    assert data["party"][0]["hp"] == 25 and lua.globals().response_writes == 2


@pytest.mark.parametrize("mode, complete", [("out", True), ("returned", True), ("wrapped", True),
                                            ("empty_typed", True), ("empty_out", False),
                                            ("opaque", False), ("bounded", False)])
def test_native_and_lua_array_bindings_are_bounded_and_completeness_is_not_guessed(runtime, mode, complete):
    lua, _ = runtime
    lua.globals().enemy_mode = mode
    result = sample(runtime)
    assert result["complete"]["enemies"] is complete
    CombatSnapshot.from_dict(result)
    if mode == "bounded":
        assert lua.globals().array_walks == 0
    if mode == "empty_typed":
        assert result["enemies"] == [] and result["phase"] == "active"


def test_missing_is_in_battle_getter_does_not_report_false(runtime):
    lua, _ = runtime
    lua.globals().unavailable["IsInBattle"] = True
    result = sample(runtime)
    assert result["in_battle"] is None and result["phase"] == "unknown" and not result["observed"]
    assert result["errors"] == ["battle_state_unavailable"]
    CombatSnapshot.from_dict(result)


def test_inactive_state_skips_all_roster_and_vital_getters(runtime):
    lua, _ = runtime
    lua.globals().active = False
    result = sample(runtime)
    assert result["in_battle"] is False and result["phase"] == "inactive"
    assert result["party"] == result["enemies"] == []
    assert lua.globals().calls["GetBattleMemberCount"] is None
    assert lua.globals().calls["GetCharaHP"] is None
    CombatSnapshot.from_dict(result)


def test_phase_change_during_getters_discards_mixed_battle_data(runtime):
    lua, _ = runtime
    lua.globals().phase_flip = True
    result = sample(runtime)
    assert not result["observed"] and result["in_battle"] is None
    assert result["party"] == result["enemies"] == [] and "combat_changed_during_sample" in result["errors"]
    CombatSnapshot.from_dict(result)


def test_unreadable_vital_does_not_reuse_a_previous_measurement(runtime):
    lua, _ = runtime
    assert sample(runtime)["party"][0]["hp"] == 100
    lua.globals().bad_hp = True
    result = sample(runtime, "b" * 32)
    assert result["party"][0]["hp"] is None and "hp_unreadable" in result["party"][0]["errors"]


def test_dead_enemy_zero_hp_and_no_target_remain_explicit_values(runtime):
    lua, _ = runtime
    lua.globals().enemy.hp = 0
    lua.globals().target = None
    result = sample(runtime)
    assert result["enemies"][0]["hp"] == 0 and result["phase"] == "active"
    assert result["target"] == {"observed": True, "actor_id": None}


def test_invalid_enemy_wrapper_is_not_a_complete_roster(runtime):
    lua, _ = runtime
    lua.globals().invalid_enemy = True
    result = sample(runtime)
    assert not result["complete"]["enemies"] and not result["target"]["observed"]


def test_query_reacquires_api_after_reload_instead_of_retaining_uobjects(runtime):
    lua, _ = runtime
    sample(runtime)
    lua.execute("local old=battle; battle=setmetatable({IsValid=function() return true end, GetCharaHP=function() return 42 end},{__index=old})")
    assert sample(runtime, "b" * 32)["party"][0]["hp"] == 42


def test_real_lua_reply_roundtrips_through_observer_without_generic_feed_placeholders(runtime, tmp_path, monkeypatch):
    from test_combat import query_fixture
    from ff7r_agent.combat import observe_combat
    lua, probe = runtime
    lua.globals().os.time = lambda: int(time.time())
    service, _, base = query_fixture(tmp_path, monkeypatch, answer=False)
    base.update(party=[{"atb": 0}], nearby_enemies=[])
    probe.set_active(True)
    previous_sleep = service._sleeper
    def answer(seconds):
        previous_sleep(seconds)
        marker = (tmp_path / "combat-query.request").read_text()
        request_id, _ = marker.split("|")
        lua.globals().files[ROOT + "combat-query.request"] = marker
        probe.sample(101, lua.globals().pawn, "EndGame")
        data = lua.globals().files[ROOT + "combat-query." + request_id + ".json"]
        (tmp_path / ("combat-query." + request_id + ".json")).write_text(data)
        base["source_sequence"] = 102
    service._sleeper = answer
    result = observe_combat(service)
    assert result["ok"] and result["combat"]["in_battle"] is True
    assert result["combat"]["party"][0]["mp"] == 30
    assert result["combat"]["party"][0]["atb"] is None
    assert result["combat"]["enemies"][0]["hp"] == 80
    assert result["combat"]["target_id"] == "enemy_1" and result["input_sent"] is False


def test_source_uses_declared_readers_and_is_in_the_complete_paired_deployment():
    source = PRODUCER.read_text(encoding="utf-8")
    readers = set(re.findall(r"reader\([^\n]+?'(End\w+API)', '(\w+)'", source))
    readers |= {("EndBattleAPI", "GetEnemyMembersRef"), ("EndBattleAPI", "GetTargetCharacter")}
    assert readers
    for owner, name in readers:
        assert re.search(r"static [^;\n]+\b" + name + r"\(", (HEADERS / (owner + ".h")).read_text())
        assert name.startswith(("Get", "Is", "BPGet"))
    assert "FindAllOf" not in source and "RegisterHook" not in source and "LoopAsync" not in source
    assert not re.search(r"reader\([^\n]+?'(?:Set\w+|GetSpendATBBarTotal|GetATB)'", source)
    main = (PRODUCER.parent / "main.lua").read_text()
    assert 'pcall(require, "combat_query")' in main
    assert "pcall(combat_query.sample, sequence, combat_pawn, map_id, combat_pawn_source)" in main
    install = (PRODUCER.parents[2] / "Install-PersistentSamplers.ps1").read_text()
    assert "Base\\Scripts\\combat_query.lua" in install and "FF7RTelemetryExporter\\scripts\\combat_query.lua" in install


def test_battle_without_a_controller_pawn_uses_the_camera_view_target(runtime):
    """Live 2026-10-20 (Mt. Nibel, Classic): in battle the controller is valid
    but its Pawn is not; the camera still views the controlled party member."""
    lua, probe = runtime
    lua.globals().combat_module = probe
    lua.execute('''
        package.preload.combat_query=function() return combat_module end
        local refs=require('object_refs')
        pawn.GetClass=function() return {GetFName=function() return {ToString=function() return 'PC0000_04_Cloud_C' end} end} end
        camera={IsValid=function() return true end, ViewTarget={Target=pawn}}
        refs.GetPlayerController=function()
            return {IsValid=function() return true end, Pawn=nil, PlayerCameraManager=camera,
                    GetWorld=function() return world end}
        end
        gameplay.GetCurrentLevelName=function() return 'EndGame' end
        party.GetPartyLeaderType=function() return 0 end
        party.GetAllPartyMemberCount=function() return 1 end
        party.GetAllPartyMemberType=function() return 0 end
        menu.BPGetPlayerHP=function() return 100 end
        menu.BPGetPlayerHPMax=function() return 100 end
        print=function() end
        active=true
    ''')
    lua.execute((PRODUCER.parent / "main.lua").read_text(encoding="utf-8"))
    lua.globals().files[ROOT + "combat-query.request"] = ID + "|1010"
    tick = lua.globals().FF7RReadOnlyTick
    tick()
    tick()
    assert lua.globals().files[ROOT + "state.sample"].split("|")[8] == "1"
    data = json.loads(lua.globals().files[ROOT + "combat-query." + ID + ".json"])
    assert data["controlled_actor_source"] == "camera_view_target"
    assert data["controlled_actor_id"] == "Cloud_1" and data["map_id"] == "EndGame"
    assert data["in_battle"] is True and data["party"][0]["actor_id"] == "Cloud_1"


def test_a_camera_viewing_something_else_is_not_the_battle_pawn(runtime):
    lua, probe = runtime
    lua.globals().combat_module = probe
    lua.execute('''
        package.preload.combat_query=function() return combat_module end
        local refs=require('object_refs')
        local cine={IsValid=function() return true end, GetFullName=function() return 'CineCamera_1' end,
            GetClass=function() return {GetFName=function() return {ToString=function() return 'CineCameraActor' end} end} end}
        camera={IsValid=function() return true end, ViewTarget={Target=cine}}
        refs.GetPlayerController=function()
            return {IsValid=function() return true end, Pawn=nil, PlayerCameraManager=camera}
        end
        gameplay.GetCurrentLevelName=function() return 'EndGame' end
        party.GetPartyLeaderType=function() return 0 end
        party.GetAllPartyMemberCount=function() return 1 end
        party.GetAllPartyMemberType=function() return 0 end
        menu.BPGetPlayerHP=function() return 100 end
        menu.BPGetPlayerHPMax=function() return 100 end
        print=function() end
        active=true
    ''')
    lua.execute((PRODUCER.parent / "main.lua").read_text(encoding="utf-8"))
    lua.globals().files[ROOT + "combat-query.request"] = ID + "|1010"
    tick = lua.globals().FF7RReadOnlyTick
    tick()
    tick()
    data = json.loads(lua.globals().files[ROOT + "combat-query." + ID + ".json"])
    # Never the camera actor: the leader's typed battle member instead.
    assert data["controlled_actor_source"] == "battle_party_leader" and data["controlled_actor_id"] == "Cloud_1"


def test_a_battle_without_pawn_or_party_view_falls_back_to_the_leaders_battle_member(runtime):
    lua, probe = runtime
    lua.globals().combat_module = probe
    lua.execute('''
        package.preload.combat_query=function() return combat_module end
        local refs=require('object_refs')
        local cine={IsValid=function() return true end, GetFullName=function() return 'BattleCamera_1' end,
            GetClass=function() return {GetFName=function() return {ToString=function() return 'EndBattleCameraActor' end} end} end}
        camera={IsValid=function() return true end, ViewTarget={Target=cine}}
        refs.GetPlayerController=function()
            return {IsValid=function() return true end, Pawn=nil, PlayerCameraManager=camera}
        end
        gameplay.GetCurrentLevelName=function() return 'EndGame' end
        party.GetPartyLeaderType=function() return 0 end
        party.GetAllPartyMemberCount=function() return 1 end
        party.GetAllPartyMemberType=function() return 0 end
        menu.BPGetPlayerHP=function() return 100 end
        menu.BPGetPlayerHPMax=function() return 100 end
        print=function() end
        active=true
    ''')
    lua.execute((PRODUCER.parent / "main.lua").read_text(encoding="utf-8"))
    lua.globals().files[ROOT + "combat-query.request"] = ID + "|1010"
    tick = lua.globals().FF7RReadOnlyTick
    tick()
    tick()
    data = json.loads(lua.globals().files[ROOT + "combat-query." + ID + ".json"])
    assert data["controlled_actor_source"] == "battle_party_leader"
    assert data["controlled_actor_id"] == "Cloud_1" and data["in_battle"] is True
    # The battle pawn stands in for the controller pawn in the base sample:
    # its target is the player's (input needs a position and a target).
    sample_fields = lua.globals().files[ROOT + "state.sample"].split("|")
    assert any(field.startswith("enemy_1,") for field in sample_fields)


def test_two_enemies_in_the_out_table_are_the_roster(runtime):
    # Live 2026-10-20 (Mt. Nibel): the getter returned nothing and filled
    # the out table with two entries.
    lua, _ = runtime
    lua.globals().enemy_mode = "out_two"
    result = sample(runtime)
    assert result["complete"]["enemies"] is True
    assert [e["actor_id"] for e in result["enemies"]] == ["enemy_1", "enemy_2"]
    CombatSnapshot.from_dict(result)


def test_an_unexpected_out_table_names_what_it_held(runtime):
    lua, _ = runtime
    lua.globals().enemy_mode = "out_named"
    result = sample(runtime)
    assert result["complete"]["enemies"] is False
    assert "enemy_shape:table:n-1:key-string" in result["errors"]
    lua.globals().invalid_enemy = True
    lua.globals().enemy_mode = "out_two"
    result = sample(runtime, "b" * 32)
    assert "enemy_shape:table:n-2:entry-1-table-table" in result["errors"]
    CombatSnapshot.from_dict(result)
