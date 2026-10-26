from __future__ import annotations

from ff7r_agent.combat_autopilot import (Enemy, Member, Memory, Plan, choose, enemy_name, learn,
                                         run_autopilot, run_combat_autopilot, situation)

# Shaped like the live 2026-10-22 Materia Guardian observation (Cloud, Sephiroth, the Guardian).
CLOUD = "Lv.PC0000_04_Cloud_ZackCostume_C_2147481126"
SEPH = "Lv.PC0010_00_Sephiroth_Standard_C_2147480678"
BOSS = "Lv.EB2000_00_MateriaKeeper_Standard_C_2147479384"


def actor(actor_id, chara, hp, max_hp, mp=None, max_mp=None):
    return {"actor_id": actor_id, "chara_id": chara, "hp": hp, "max_hp": max_hp, "mp": mp, "max_mp": max_mp}


def combat(cloud_hp=3736, seph_hp=4259, boss_hp=29313, cloud_mp=41, seph_mp=60, in_battle=True):
    return {"combat": {"in_battle": in_battle,
                       "party": [actor(CLOUD, "PC0000_04", cloud_hp, 3736, cloud_mp, 53), actor(SEPH, "PC0010_00", seph_hp, 4259, seph_mp, 60)],
                       "enemies": [actor(BOSS, "EB2000_00", boss_hp, 29313)]}}


def members(cloud=1.0, seph=1.0, cloud_mp=41, seph_mp=60):
    party, _ = situation(combat(int(3736 * cloud), int(4259 * seph), cloud_mp=cloud_mp, seph_mp=seph_mp)["combat"])
    return party


GUARDIAN = [Enemy(BOSS, "Materia Guardian", 20000, 29313)]


def test_actors_are_named_from_their_ids():
    party, enemies = situation(combat()["combat"])
    assert [m.name for m in party] == ["Cloud", "Sephiroth"] and enemies[0].name == "Materia Guardian"
    assert enemy_name({"actor_id": "Lv.EB1100_00_SonicSpeed_Standard_C_1"}) == "Sonic Speed"


def test_phoenix_party_uses_menu_names_for_casters_and_healing_targets():
    party, enemies = situation({
        "party": [actor("Lv.PC0002_00_Tifa_Standard_C_1", "PC0002_00", 400, 4077, 50, 50),
                  actor("Lv.PC0005_00_Yuffie_Standard_C_2", "PC0005_00", 3988, 3988, 55, 55),
                  actor("Lv.PC0004_00_RedXIII_Standard_C_3", "PC0004_00", 4785, 4785, 57, 57)],
        "enemies": [actor("Lv.SU2040_00_Phoenix_Standard_C_4", "SU2040_01", 47109, 47109)],
    })
    assert [m.name for m in party] == ["Tifa", "Yuffie", "Red XIII"]
    plan = choose(party, enemies, Memory(), 0)
    assert (plan.member, plan.path, plan.target) == ("Red XIII", ("Spells", "Cure"), "Tifa")


def test_a_fallen_member_is_revived_first_by_the_member_with_mp():
    party = members(cloud=0.0)
    plan = choose(party, GUARDIAN, Memory(), 0.0)
    assert plan == Plan("Sephiroth", ("Spells", "Raise"), "Cloud", "revive", "Cloud has fallen")
    memory = Memory(unavailable={("Sephiroth", ("Spells", "Raise"))})
    assert choose(party, GUARDIAN, memory, 0.0).path == ("Items", "Phoenix Down")


def test_the_lowest_member_is_healed_with_cure_then_an_item_when_mp_is_gone():
    plan = choose(members(cloud=0.3, seph=0.4), GUARDIAN, Memory(), 0.0)
    assert (plan.member, plan.path, plan.target, plan.kind) == ("Sephiroth", ("Spells", "Cure"), "Cloud", "heal")
    plan = choose(members(cloud=0.3, cloud_mp=0, seph_mp=0), GUARDIAN, Memory(), 0.0)
    assert plan.path == ("Items", "Mega-Potion") and plan.target == "Cloud" and plan.member == "Cloud"


def test_attacks_are_each_tried_once_then_the_measured_best_is_preferred():
    memory, party = Memory(), members()
    first = choose(party, GUARDIAN, memory, 0.0)
    assert first.kind == "attack" and first.member == "Cloud" and first.path == ("Abilities", "Braver")
    done = {"outcome": "menu_closed_after_command", "top_entries": ["Abilities", "Spells", "Items"]}
    assert learn(memory, first, done, 1.0, 20000, 19900) == {"damage": 100}
    gate = Plan("Sephiroth", ("Abilities", "Hell's Gate"), None, "attack", "")
    learn(memory, gate, done, 2.0, 19900, 16900)
    memory.tried |= {("Sephiroth", ("Abilities", "Zanshin"))} | {("Sephiroth", ("Spells", s)) for s in ("Fire", "Blizzard", "Thunder", "Aero")}
    memory.last_commanded["Cloud"] = 5.0
    plan = choose(party, GUARDIAN, memory, 6.0)
    assert plan.member == "Sephiroth" and plan.path == ("Abilities", "Hell's Gate") and "3000" in plan.reason


def test_spells_keep_an_mp_reserve_for_cures():
    memory = Memory(tried={("Cloud", ("Abilities", a)) for a in ("Braver", "Focused Thrust", "Triple Slash")})
    memory.last_commanded["Sephiroth"] = 9.0
    plan = choose(members(cloud_mp=12), GUARDIAN, memory, 10.0)
    assert plan.member == "Cloud" and plan.path[0] == "Abilities"  # 12 MP is below the reserve for spells


def test_unavailable_commands_and_resting_members_are_skipped():
    memory = Memory()
    plan = Plan("Cloud", ("Abilities", "Braver"), None, "attack", "")
    learn(memory, plan, {"outcome": "entry_not_found"}, 0.0, None, None)
    learn(memory, Plan("Sephiroth", ("Abilities", "Hell's Gate"), None, "attack", ""), {"outcome": "command_not_accepted"}, 0.0, None, None)
    nxt = choose(members(), GUARDIAN, memory, 1.0)
    assert nxt.member == "Cloud" and nxt.path != ("Abilities", "Braver")
    assert choose(members(), GUARDIAN, memory, 4.0).member in ("Cloud", "Sephiroth")


def test_a_ready_limit_is_used_and_cleared():
    memory = Memory()
    plan = Plan("Cloud", ("Abilities", "Braver"), None, "attack", "")
    learn(memory, plan, {"outcome": "command_not_accepted", "top_entries": ["Abilities", "Spells", "Items", "Limit"]}, 0.0, None, None)
    limit = choose(members(), GUARDIAN, memory, 5.0)
    assert limit.kind == "limit" and limit.member == "Cloud" and limit.path == ("Limit",)
    learn(memory, limit, {"outcome": "menu_closed_after_command"}, 6.0, 20000, 18000)
    assert memory.limit_ready["Cloud"] is False


class FakeService:
    """Observations, menu results and screen texts in order; records commands."""

    def __init__(self, observations, results, game_over_after=None):
        self.observations, self.results, self.commands, self.moments = list(observations), list(results), [], []
        self.game_over_after = game_over_after
        self.reads = 0

    def observe_combat(self, timeout_seconds=3.0):
        self.reads += 1
        return self.observations.pop(0) if len(self.observations) > 1 else self.observations[0]

    def read_screen_texts(self):
        if self.game_over_after is not None and self.reads >= self.game_over_after:
            return {"texts": [{"path": "BP.GameOver.WidgetTree.Txt_GameOver", "text": "$menu_gameover_0000_0000"}]}
        return {"texts": []}

    def use_battle_command(self, **values):
        self.commands.append(values)
        result = self.results.pop(0) if self.results else {"outcome": "command_not_accepted"}
        if isinstance(result, Exception):
            raise result
        return result

    def read_battle_command_menu(self):
        return {"open": False}

    def battle_menu_tap(self, button):
        pass


def clock_and_sleep():
    now = [0.0]

    def sleep(seconds):
        now[0] += seconds

    def clock():
        now[0] += 0.5
        return now[0]
    return clock, sleep


def hit(boss_after):
    return {"outcome": "menu_closed_after_command", "top_entries": ["Abilities", "Spells", "Items"],
            "effect": {"after": {"enemies": {BOSS: {"hp": boss_after}}, "party": {}}}}


def test_a_won_fight_requires_observed_zero_hp():
    # Damage is credited from the next paced observation: 4000 -> 1000 is the big hit.
    service = FakeService([combat(boss_hp=4000), combat(boss_hp=1000), combat(boss_hp=900), combat(boss_hp=0), combat(in_battle=False, boss_hp=0)],
                          [hit(1000), hit(900)])
    clock, sleep = clock_and_sleep()
    result = run_autopilot(service, seconds=120, execute=True, clock=clock, sleep=sleep)
    assert result["outcome"] == "victory" and result["ok"] and result["boss"] == "Materia Guardian"
    assert all(c["hold_on_enemy_skill"] for c in service.commands) and service.commands[0]["member"] == "Cloud"
    assert all(c["measure_effect"] is False for c in service.commands)  # no extra snapshots per command
    assert result["log"][0]["damage"] == 3000


def test_a_game_over_is_reported_without_claiming_a_win():
    service = FakeService([combat(cloud_hp=0, seph_hp=0), combat(in_battle=False)], [], game_over_after=2)
    clock, sleep = clock_and_sleep()
    result = run_autopilot(service, seconds=60, execute=True, clock=clock, sleep=sleep)
    assert result["outcome"] == "game_over" and result["ok"] is False


def test_refused_input_ends_the_run_and_a_dry_run_sends_one_preview():
    service = FakeService([combat()], [RuntimeError("control session latched")])
    clock, sleep = clock_and_sleep()
    assert run_autopilot(service, seconds=60, execute=True, clock=clock, sleep=sleep)["outcome"] == "input_refused"
    service = FakeService([combat()], [{"outcome": "dry_run"}])
    result = run_autopilot(service, seconds=60, execute=False, clock=clock, sleep=sleep)
    assert result["outcome"] == "dry_run" and len(service.commands) == 1 and service.commands[0]["execute"] is False


def test_worker_arguments_are_validated():
    assert run_combat_autopilot(None, None, {"seconds": 1})["outcome"] == "invalid_arguments"
    assert run_combat_autopilot(None, None, {"execute": "yes"})["outcome"] == "invalid_arguments"
    assert run_combat_autopilot(None, None, {"heal_below": 0.95})["outcome"] == "invalid_arguments"


def test_a_telegraph_name_left_on_the_hud_stops_holding_commands():
    held = {"outcome": "enemy_attack_incoming", "enemy_skill": "Dual Tail Lash", "menu": {"enemy_skill": "Dual Tail Lash"}}
    service = FakeService([combat()], [held] * 40)
    clock, sleep = clock_and_sleep()
    run_autopilot(service, seconds=40, execute=True, clock=clock, sleep=sleep)
    holds = [c["hold_on_enemy_skill"] for c in service.commands]
    assert holds[0] is True and False in holds  # the unchanged name went stale
    first_free = holds.index(False)
    assert all(h is False for h in holds[first_free:])


def test_each_step_is_logged_as_it_happens_and_the_screen_is_read_only_after_a_wipe(tmp_path):
    reads = []

    class Counting(FakeService):
        def read_screen_texts(self):
            reads.append(self.reads)
            return super().read_screen_texts()

    # A won fight never reads the whole screen (1 s on the game thread a read).
    service = Counting([combat(boss_hp=4000), combat(boss_hp=1000), combat(boss_hp=0), combat(in_battle=False, boss_hp=0)], [hit(1000)])
    clock, sleep = clock_and_sleep()
    path = tmp_path / "combat.jsonl"
    result = run_autopilot(service, seconds=120, execute=True, clock=clock, sleep=sleep, log_path=path)
    assert result["outcome"] == "victory" and result["log_path"] == str(path) and reads == []
    import json
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert lines[0]["path"] == ["Abilities", "Braver"] and lines[0]["boss_hp"] == 4000 and "Cloud" in lines[0]["party_hp"]
    assert any(line.get("damage") == 3000 for line in lines) and lines[-1] == {"at": lines[-1]["at"], "outcome": "victory"}


def test_a_watcher_holding_the_input_lock_is_waited_out_not_a_stop():
    busy = RuntimeError("another input action is executing")
    service = FakeService([combat(boss_hp=4000), combat(boss_hp=1000), combat(boss_hp=0), combat(in_battle=False, boss_hp=0)], [busy, hit(1000)])
    clock, sleep = clock_and_sleep()
    result = run_autopilot(service, seconds=120, execute=True, clock=clock, sleep=sleep)
    assert result["outcome"] == "victory" and len(service.commands) == 2
    service = FakeService([combat()], [busy] * 10)
    assert run_autopilot(service, seconds=600, execute=True, clock=clock, sleep=sleep)["outcome"] == "input_refused"
    assert len(service.commands) == 6  # five waits, then the lasting refusal ends the run


def test_a_member_refusing_in_a_row_waits_longer_each_time_until_a_command_lands():
    memory, plan = Memory(), Plan("Cloud", ("Abilities", "Braver"), None, "attack", "")
    waits = []
    for now in (0.0, 10.0, 20.0, 40.0, 80.0):
        learn(memory, plan, {"outcome": "command_not_accepted"}, now, None, None)
        waits.append(memory.resting_until["Cloud"] - now)
    assert waits == [3.0, 6.0, 12.0, 24.0, 24.0]  # held by the boss: no menu every few seconds
    learn(memory, plan, {"outcome": "menu_closed_after_command"}, 100.0, None, None)
    learn(memory, plan, {"outcome": "command_not_accepted"}, 110.0, None, None)
    assert memory.resting_until["Cloud"] == 113.0


def test_low_boss_hp_and_lost_telemetry_never_become_victory_evidence():
    for ended, expected in (({"available": False, "combat": {}}, "combat_observation_unavailable"),
                            (combat(in_battle=False, boss_hp=0), "battle_ended")):
        service = FakeService([combat(boss_hp=1000), ended], [hit(1000)])
        clock, sleep = clock_and_sleep()
        result = run_autopilot(service, seconds=60, execute=True, clock=clock, sleep=sleep)
        assert result["outcome"] == expected


def test_titan_party_can_use_barrets_native_abilities_and_aeriths_aero():
    from ff7r_agent.combat_autopilot import _attack
    barret = Member("Barret", 5000, 5229, 46, 46)
    assert _attack(barret, Memory()).path == ("Abilities", "Focused Shot")
    memory = Memory(unavailable={("Aerith", ("Spells", spell)) for spell in ("Fire", "Blizzard", "Thunder")})
    assert _attack(Member("Aerith", 3000, 3633, 64, 64), memory).path == ("Spells", "Aero")
