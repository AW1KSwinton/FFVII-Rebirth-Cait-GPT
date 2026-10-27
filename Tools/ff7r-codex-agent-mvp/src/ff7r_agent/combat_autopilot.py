"""Combat autopilot: choose and issue ATB commands for the whole party.

Classic mode fights on its own (attacks, guards); the player's part is the
ATB command menu. This module decides which member uses what, every turn,
from the combat observation, and issues it through ``use_battle_command``
(which verifies each highlight, switches the commanded member with Q/E and
holds while an enemy attack is telegraphed).

Live 2026-10-22, the Materia Guardian (Cloud and Sephiroth): the fight starts
with Sephiroth controlled; his abilities are Zanshin and Hell's Gate (one
Hell's Gate took about 3,000 of the Guardian's 29,313 HP), his spells Fire,
Blizzard, Thunder, Aero (range spells, "<icon=range>", ask a further choice),
Cure and Raise. The observation gives HP/MP per actor but no ATB, so an
unavailable command is learnt from ``command_not_accepted`` and that member
rests briefly. The enemy's telegraphed attack shows as ``enemy_skill``, but
the HUD keeps the last name after the attack lands, so a name unchanged for
``STALE_SKILL_SECONDS`` no longer holds commands until another appears.

Policy, in order: revive a fallen member (Raise, else Phoenix Down); heal the
lowest member below ``heal_below`` (Cure, else Mega-Potion); restore MP with
an Ether when a healer is dry; use a ready Limit; otherwise attack with the
command that has measurably hurt the enemy most. Each attack is tried once
before the measured best is preferred, so weaknesses are learnt in the fight
rather than assumed. Spells keep an MP reserve for two Cures.

Damage is credited to a command from the next paced observation (no extra
snapshots per command), so each command is measured over its animation.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

HEAL_BELOW = 0.45
CURE_MP = 5
CURE_RESERVE = 2
BOSS_MIN_HP = 10_000
# The HUD keeps the last telegraphed attack's name after it lands (live: "Dual
# Tail Lash" read for minutes), so an unchanged name stops holding commands.
STALE_SKILL_SECONDS = 6.0
# Live 2026-10-22 the game hitched while menu/combat snapshots were read every
# couple of seconds, so the loop is paced: one observation per cycle, no extra
# snapshots per command, and a pause after each command for its animation.
PACE_SECONDS = 3.0
WAIT_SECONDS = 2.0
OUT_OF_BATTLE_SECONDS = 8.0
REST_SECONDS = 3.0
MAX_SECONDS = 1800
MAX_BACKOFF_DOUBLINGS = 3  # a refusing member waits 3, 6, 12, then 24 s
# Another input action holds the lock; transient, unlike a latched stop.
BUSY_INPUT = "another input action is executing"
MAX_BUSY_RETRIES = 5
EMA = 0.5
# Character IDs seen live; others are named after their actor class.
CHARA_NAMES = {"PC0000": "Cloud", "PC0002": "Tifa", "PC0004": "Red XIII",
               "PC0005": "Yuffie", "PC0010": "Sephiroth"}
ENEMY_NAMES = {"MateriaKeeper": "Materia Guardian"}
# What each member is known to carry; unknown members try the common spells.
KITS: dict[str, dict[str, list[str]]] = {
    "Cloud": {"abilities": ["Braver", "Focused Thrust", "Triple Slash"], "spells": ["Fire", "Blizzard", "Thunder"]},
    "Sephiroth": {"abilities": ["Hell's Gate", "Zanshin"], "spells": ["Fire", "Blizzard", "Thunder", "Aero"]},
    "Barret": {"abilities": ["Focused Shot", "Maximum Fury"], "spells": ["Fire", "Blizzard", "Thunder", "Aero"]},
}
COMMON_SPELLS = ["Fire", "Blizzard", "Thunder", "Aero"]


@dataclass(frozen=True)
class Member:
    name: str
    hp: int
    max_hp: int
    mp: int
    max_mp: int

    @property
    def ratio(self) -> float:
        return self.hp / self.max_hp if self.max_hp > 0 else 0.0


@dataclass(frozen=True)
class Enemy:
    actor_id: str
    name: str
    hp: int
    max_hp: int


@dataclass(frozen=True)
class Plan:
    member: str
    path: tuple[str, ...]
    target: str | None
    kind: str  # revive, heal, mp, limit, attack
    reason: str


@dataclass
class Memory:
    damage: dict[tuple[str, tuple[str, ...]], float] = field(default_factory=dict)
    tried: set[tuple[str, tuple[str, ...]]] = field(default_factory=set)
    unavailable: set[tuple[str, tuple[str, ...]]] = field(default_factory=set)
    resting_until: dict[str, float] = field(default_factory=dict)
    last_commanded: dict[str, float] = field(default_factory=dict)
    limit_ready: dict[str, bool] = field(default_factory=dict)
    failures: dict[str, int] = field(default_factory=dict)  # refusals in a row, by member


def member_name(actor: Mapping[str, Any]) -> str:
    chara = str(actor.get("chara_id") or "")[:6]
    if chara in CHARA_NAMES:
        return CHARA_NAMES[chara]
    match = re.search(r"PC\d+_\d+_([A-Za-z]+)", str(actor.get("actor_id") or ""))
    return match.group(1) if match else chara or "member"


def enemy_name(actor: Mapping[str, Any]) -> str:
    match = re.search(r"(?:E[A-Z]?|SU)\d+_\d+_([A-Za-z]+?)(?:_|$)", str(actor.get("actor_id") or ""))
    token = match.group(1) if match else str(actor.get("chara_id") or "enemy")
    return ENEMY_NAMES.get(token, re.sub(r"(?<=[a-z])(?=[A-Z])", " ", token))


def situation(combat: Mapping[str, Any]) -> tuple[list[Member], list[Enemy]]:
    """Party (fallen members included) and living enemies from one observation;
    actors without measured HP are left out."""
    party = [Member(member_name(a), int(a["hp"]), int(a["max_hp"]), int(a.get("mp") or 0), int(a.get("max_mp") or 0))
             for a in combat.get("party") or []
             if isinstance(a.get("hp"), int) and isinstance(a.get("max_hp"), int) and a["max_hp"] > 0]
    enemies = [Enemy(str(e.get("actor_id")), enemy_name(e), int(e["hp"]), int(e["max_hp"]))
               for e in combat.get("enemies") or []
               if isinstance(e.get("hp"), int) and isinstance(e.get("max_hp"), int) and e["hp"] > 0]
    return party, enemies


def _usable(memory: Memory, member: str, path: tuple[str, ...]) -> bool:
    return (member, path) not in memory.unavailable


def choose(party: list[Member], enemies: list[Enemy], memory: Memory, now: float,
           heal_below: float = HEAL_BELOW) -> Plan | None:
    """The next command, or None when nobody can act yet."""
    if not enemies or not party:
        return None
    ready = [m for m in party if m.hp > 0 and memory.resting_until.get(m.name, 0.0) <= now]
    if not ready:
        return None
    by_mp = sorted(ready, key=lambda m: -m.mp)
    fallen = [m for m in party if m.hp == 0]
    if fallen:
        target = fallen[0].name
        for caster in by_mp:
            if caster.mp >= CURE_MP * 2 and _usable(memory, caster.name, ("Spells", "Raise")):
                return Plan(caster.name, ("Spells", "Raise"), target, "revive", f"{target} has fallen")
        for caster in ready:
            if _usable(memory, caster.name, ("Items", "Phoenix Down")):
                return Plan(caster.name, ("Items", "Phoenix Down"), target, "revive", f"{target} has fallen, no MP for Raise")
    low = sorted((m for m in party if 0 < m.hp and m.ratio < heal_below), key=lambda m: m.ratio)
    if low:
        target = low[0]
        for caster in by_mp:
            if caster.mp >= CURE_MP and _usable(memory, caster.name, ("Spells", "Cure")):
                return Plan(caster.name, ("Spells", "Cure"), target.name, "heal", f"{target.name} at {target.ratio:.0%} HP")
        for caster in sorted(ready, key=lambda m: m.name != target.name):
            if _usable(memory, caster.name, ("Items", "Mega-Potion")):
                return Plan(caster.name, ("Items", "Mega-Potion"), target.name, "heal", f"{target.name} at {target.ratio:.0%} HP, no MP")
    for member in ready:
        if member.mp < CURE_MP <= member.max_mp and _usable(memory, member.name, ("Items", "Ether")):
            return Plan(member.name, ("Items", "Ether"), member.name, "mp", f"{member.name} cannot cast Cure")
    # Command whoever has waited longest; their ATB has had the most time to fill.
    order = sorted(ready, key=lambda m: memory.last_commanded.get(m.name, 0.0))
    for member in order:
        if memory.limit_ready.get(member.name) and _usable(memory, member.name, ("Limit",)):
            return Plan(member.name, ("Limit",), None, "limit", f"{member.name}'s Limit is ready")
    for member in order:
        plan = _attack(member, memory)
        if plan:
            return plan
    return None


def _attack(member: Member, memory: Memory) -> Plan | None:
    kit = KITS.get(member.name, {"abilities": [], "spells": COMMON_SPELLS})
    options = [("Abilities", name) for name in kit["abilities"]]
    if member.mp >= CURE_MP * (CURE_RESERVE + 1):
        options += [("Spells", name) for name in kit["spells"]]
    options = [path for path in options if _usable(memory, member.name, path)]
    if not options:
        return None
    untried = [path for path in options if (member.name, path) not in memory.tried]
    if untried:
        return Plan(member.name, untried[0], None, "attack", f"trying {untried[0][-1]}")
    best = max(options, key=lambda path: memory.damage.get((member.name, path), 0.0))
    return Plan(member.name, best, None, "attack",
                f"{best[-1]} has done the most ({memory.damage.get((member.name, best), 0.0):.0f} HP)")


def learn(memory: Memory, plan: Plan, result: Mapping[str, Any], now: float,
          enemy_hp_before: int | None, enemy_hp_after: int | None) -> dict[str, Any] | None:
    """Update memory from one use_battle_command result; return what it did."""
    outcome = result.get("outcome")
    key = (plan.member, plan.path)
    top = result.get("top_entries")
    if isinstance(top, list):
        memory.limit_ready[plan.member] = any(str(entry).strip().casefold() == "limit" for entry in top)
    if outcome == "menu_closed_after_command":
        memory.failures[plan.member] = 0
        memory.last_commanded[plan.member] = now
        memory.tried.add(key)
        if plan.kind == "limit":
            memory.limit_ready[plan.member] = False
        if plan.kind in ("attack", "limit") and enemy_hp_before is not None and enemy_hp_after is not None:
            drop = max(0, enemy_hp_before - enemy_hp_after)
            old = memory.damage.get(key)
            memory.damage[key] = drop if old is None else EMA * drop + (1 - EMA) * old
            return {"damage": drop}
        return {}
    if outcome in ("entry_not_found", "entry_not_reached", "menu_still_open", "highlight_unverified"):
        # Not in this member's menu, or a further choice this policy does not make.
        memory.unavailable.add(key)
    elif outcome == "member_not_reached":
        memory.resting_until[plan.member] = now + REST_SECONDS * 4
    elif outcome in ("command_not_accepted", "enemy_attack_incoming", "command_menu_not_opened", "menu_closed_early"):
        # A member the boss holds (live 2026-10-22: Cloud grabbed) refuses
        # every command until freed: wait longer after each refusal in a row
        # rather than open and close the menu every few seconds.
        failures = memory.failures.get(plan.member, 0) + 1
        memory.failures[plan.member] = failures
        memory.resting_until[plan.member] = now + REST_SECONDS * 2 ** min(failures - 1, MAX_BACKOFF_DOUBLINGS)
    return None


def credit(memory: Memory, plan: Plan, enemy_hp_before: int | None, enemy_hp_after: int | None) -> int | None:
    """Credit an attack with the enemy HP it was followed by; the measured drop."""
    if plan.kind not in ("attack", "limit") or enemy_hp_before is None or enemy_hp_after is None:
        return None
    drop = max(0, enemy_hp_before - enemy_hp_after)
    key = (plan.member, plan.path)
    old = memory.damage.get(key)
    memory.damage[key] = drop if old is None else EMA * drop + (1 - EMA) * old
    return drop


def log_directory() -> Path:
    """Where each run's step log is written as it goes."""
    configured = os.environ.get("FF7R_COMBAT_LOG_DIR")
    if configured:
        return Path(configured)
    local = os.environ.get("LOCALAPPDATA")
    return (Path(local) / "ff7r-agent" / "combat-logs") if local else Path(tempfile.gettempdir()) / "ff7r-combat-logs"


def run_autopilot(service: Any, *, seconds: float, execute: bool, heal_below: float = HEAL_BELOW,
                  pace_seconds: float = PACE_SECONDS,
                  clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                  log_path: Path | None = None) -> dict[str, Any]:
    """Command the party until the fight ends, ``seconds`` pass or input is refused.

    Each step is also appended to ``log_path`` as it happens: a cancelled
    job returns no result, and a fight that goes wrong needs its evidence."""
    from .game_over import read_game_over

    memory, log, started = Memory(), [], clock()
    party_down = False
    busy_retries = 0

    def write(entry: dict[str, Any]) -> None:
        if log_path is None:
            return
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(), **entry}) + "\n")
        except OSError:
            pass  # The log is evidence, never a reason to stop the fight.

    def record(entry: dict[str, Any]) -> None:
        entry = {"at": datetime.now(timezone.utc).isoformat(), **entry}
        log.append(entry)
        write(entry)
    seen_boss: Enemy | None = None
    last_enemy_hp: dict[str, int] = {}
    out_of_battle_since: float | None = None
    outcome = "time_limit"
    skill_text: str | None = None
    skill_since = 0.0
    pending: tuple[Plan, int, str] | None = None  # an attack awaiting its measured effect

    while clock() - started < seconds:
        observation = service.observe_combat(timeout_seconds=3.0) or {}
        combat = observation.get("combat") or {}
        if observation.get("available") is False or type(combat.get("in_battle")) is not bool:
            outcome = "combat_observation_unavailable"
            record({"outcome": outcome, "reason": observation.get("reason")})
            break
        if combat["in_battle"] is False:
            if out_of_battle_since is None:
                out_of_battle_since = clock()
            # A Game Over follows a downed party; the screen read scans every
            # text in the game (1 s on the game thread), so it waits for one.
            if party_down:
                texts = [(str(t.get("path", "")), str(t.get("text", "")))
                         for t in (service.read_screen_texts() or {}).get("texts") or [] if isinstance(t, dict)]
                if read_game_over(texts):
                    outcome = "game_over"
                    break
            if seen_boss is not None and clock() - out_of_battle_since >= OUT_OF_BATTLE_SECONDS:
                last_hp = last_enemy_hp.get(seen_boss.actor_id, seen_boss.hp)
                # Low HP and an absent roster do not prove a win.
                outcome = "victory" if last_hp == 0 and not party_down else "battle_ended"
                break
            sleep(1.0)
            continue
        out_of_battle_since = None
        party, enemies = situation(combat)
        party_down = bool(party) and all(m.hp <= 0 for m in party)
        for enemy in combat.get("enemies") or []:
            if isinstance(enemy.get("hp"), int):
                last_enemy_hp[str(enemy.get("actor_id"))] = enemy["hp"]
        if pending is not None:
            done, before, focus_id = pending
            pending = None
            drop = credit(memory, done, before, last_enemy_hp.get(focus_id))
            if drop is not None:
                log[-1]["damage"] = drop
                write({"credited": " > ".join(done.path), "member": done.member, "damage": drop})
        boss = max(enemies, key=lambda e: e.max_hp, default=None)
        if boss is not None and boss.max_hp >= BOSS_MIN_HP and seen_boss is None:
            seen_boss = boss
        plan = choose(party, enemies, memory, clock(), heal_below)
        if plan is None:
            sleep(WAIT_SECONDS)
            continue
        focus = seen_boss or boss
        before = last_enemy_hp.get(focus.actor_id) if focus else None
        hold = skill_text is None or clock() - skill_since < STALE_SKILL_SECONDS
        try:
            result = service.use_battle_command(path=list(plan.path), execute=execute, target=plan.target,
                                                member=plan.member, hold_on_enemy_skill=hold,
                                                take_highlighted=plan.kind == "limit", measure_effect=False,
                                                enemies=[enemy.name for enemy in enemies],
                                                prefer_child_target=plan.kind in ("attack", "limit"))
        except Exception as error:  # A latched stop or lost control ends the run.
            record({"t": round(clock() - started, 1), "member": plan.member, "path": list(plan.path),
                    "error": type(error).__name__, "detail": str(error)[:200]})
            # A watcher's own press (closing a tip, skipping a scene) holds the
            # input lock for a moment (live 2026-10-22): wait, then go on.
            if BUSY_INPUT in str(error) and busy_retries < MAX_BUSY_RETRIES:
                busy_retries += 1
                sleep(WAIT_SECONDS)
                continue
            outcome = "input_refused"
            break
        busy_retries = 0
        shown = (result.get("menu") or {}).get("enemy_skill") or result.get("enemy_skill")
        if shown != skill_text:
            skill_text, skill_since = shown, clock()
        if result.get("outcome") == "menu_still_open":
            for _ in range(5):  # Back out of a further choice this policy does not make.
                if not (service.read_battle_command_menu() or {}).get("open"):
                    break
                service.battle_menu_tap("cancel")
        learn(memory, plan, result, clock(), None, None)
        record({"t": round(clock() - started, 1), "member": plan.member, "path": list(plan.path), "target": plan.target,
                "kind": plan.kind, "reason": plan.reason, "outcome": result.get("outcome"),
                "party_hp": {m.name: m.hp for m in party}, "boss_hp": before,
                # Where a command stalls, the screen it stalled on.
                **({"menu": {k: (result.get("menu") or {}).get(k) for k in ("title", "help", "entries", "selected")}}
                   if result.get("outcome") in ("menu_still_open", "entry_not_found", "highlight_unverified") else {})})
        if not execute:
            outcome = "dry_run"
            break
        closed = result.get("outcome") == "menu_closed_after_command"
        if closed and focus is not None and before is not None:
            pending = (plan, before, focus.actor_id)
        sleep(pace_seconds if closed else WAIT_SECONDS)
    write({"outcome": outcome})
    return {"ok": outcome in ("victory", "dry_run", "time_limit", "battle_ended"), "outcome": outcome,
            "log_path": str(log_path) if log_path is not None else None,
            "seconds": round(clock() - started, 1), "boss": seen_boss.name if seen_boss else None,
            "commands": len(log), "learnt_damage": {f"{m}: {' > '.join(p)}": round(v) for (m, p), v in memory.damage.items()},
            "unavailable": sorted(f"{m}: {' > '.join(p)}" for m, p in memory.unavailable), "log": log[-60:]}


def run_combat_autopilot(service: Any, lifecycle: Any, values: Mapping[str, Any]) -> dict[str, Any]:
    """Worker side of ``combat_autopilot_async``: validate, then run."""
    del lifecycle
    seconds = values.get("seconds", 600)
    execute = values.get("execute", False)
    heal_below = values.get("heal_below", HEAL_BELOW)
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or not 5 <= seconds <= MAX_SECONDS:
        return {"ok": False, "outcome": "invalid_arguments", "error": f"seconds must be 5-{MAX_SECONDS}"}
    if type(execute) is not bool:
        return {"ok": False, "outcome": "invalid_arguments", "error": "execute must be a boolean"}
    if not isinstance(heal_below, (int, float)) or isinstance(heal_below, bool) or not 0.1 <= heal_below <= 0.9:
        return {"ok": False, "outcome": "invalid_arguments", "error": "heal_below must be 0.1-0.9"}
    log_path = log_directory() / f"combat-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.jsonl"
    return run_autopilot(service, seconds=float(seconds), execute=execute, heal_below=float(heal_below),
                         log_path=log_path)
