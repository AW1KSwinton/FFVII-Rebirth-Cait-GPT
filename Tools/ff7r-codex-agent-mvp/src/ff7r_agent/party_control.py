"""Switch the controlled combat actor, separately from commanding an ally."""
from __future__ import annotations

import time
from typing import Any, Callable


# Primary EPlayerType.h; membership/availability always comes from the battle.
PLAYER_TYPES = {"cloud": 0, "barret": 1, "tifa": 2, "aerith": 3, "redxiii": 4,
                "yuffie": 5, "caitsith": 6, "zack": 7, "sephiroth": 8}


class ControlStopped(RuntimeError):
    pass


def switch_combat_character(service: Any, member: str, execute: bool = False, *,
                             read: Callable[[], dict[str, Any]] | None = None,
                             read_menu: Callable[[], dict[str, Any]] | None = None,
                             clock: Callable[[], float] | None = None,
                             sleep: Callable[[float], None] | None = None) -> dict[str, Any]:
    """At most three Up pulses; verify the actual controlled actor after each.

    Never opens/closes Tactical Mode or issues a command. Each pulse has a
    bounded wait; no changed-controller receipt means no repeated input.
    """
    if not isinstance(member, str) or not member.strip() or len(member) > 1024 or any(ord(c) < 32 for c in member):
        raise ValueError("member must be a character name or observed party actor ID")
    if type(execute) is not bool:
        raise ValueError("execute must be a boolean")
    clock = clock or getattr(service, "_monotonic", time.monotonic)
    sleep = sleep or getattr(service, "_sleeper", time.sleep)
    deadline = clock() + 8
    read = read or (lambda: service.observe_combat(timeout_seconds=min(1.5, max(0.5, deadline - clock()))))
    read_menu = read_menu or service.read_battle_command_menu
    identity, roster = None, None
    sequence = -1
    target, current, initial = None, None, None
    transitions: list[dict[str, Any]] = []
    attempted, accepted, uncertain = 0, 0, False
    details: dict[str, Any] = {}

    def observe() -> dict[str, Any]:
        nonlocal identity, roster, sequence, current
        if deadline - clock() < 0.5:
            raise ControlStopped("combat_control_wait_expired")
        response = read()
        if response.get("ok") is not True or response.get("available") is not True:
            raise ControlStopped(str(response.get("reason") or "combat_observation_unavailable"))
        battle = response.get("combat") or {}
        if battle.get("in_battle") is not True:
            raise ControlStopped("combat_not_active")
        if battle.get("paused") is not False:
            raise ControlStopped("combat_paused_or_unobserved")
        if battle.get("party_complete") is not True:
            raise ControlStopped("combat_party_unobserved")
        party = battle.get("party")
        if not isinstance(party, list) or not 1 <= len(party) <= 8:
            raise ControlStopped("combat_party_unobserved")
        ids = [r.get("actor_id") for r in party]
        if (not all(isinstance(i, str) and i for i in ids) or len(set(ids)) != len(ids)
                or battle.get("controlled_actor_id") not in ids):
            raise ControlStopped("combat_controller_unobserved")
        observed_identity = response.get("identity")
        if (not isinstance(observed_identity, dict)
                or not all(isinstance(observed_identity.get(k), str) and observed_identity[k]
                           for k in ("process_token", "map_id", "world_id"))):
            raise ControlStopped("combat_identity_unobserved")
        observed_roster = sorted((r["actor_id"], r.get("player_type")) for r in party)
        if identity is not None and (observed_identity != identity or observed_roster != roster):
            raise ControlStopped("combat_control_context_changed")
        identity, roster = dict(observed_identity), observed_roster
        next_sequence = battle.get("source_sequence")
        if type(next_sequence) is not int or next_sequence <= sequence:
            raise ControlStopped("combat_control_capture_not_advanced")
        sequence = next_sequence
        current = battle["controlled_actor_id"]
        if target is not None:
            alive = next((r for r in party if r["actor_id"] == target), {})
            if type(alive.get("hp")) is not int or alive["hp"] <= 0:
                raise ControlStopped("combat_character_unavailable")
        return battle

    def result(ok: bool, outcome: str) -> dict[str, Any]:
        return {"ok": ok, "outcome": outcome, "reason": None if ok else outcome, "requested_member": member,
                "target_actor_id": target, "before_controlled_actor_id": initial,
                "controlled_actor_id": current, "transitions": transitions,
                "input_sent": True if accepted else None if uncertain else False,
                "input_attempted": attempted > 0, "input_outcome_unknown": uncertain,
                "accepted_inputs": accepted, "identity": identity, **details}

    try:
        battle = observe()
        initial = current
        key = member.casefold().replace(" ", "").replace("-", "")
        candidates = [r for r in battle["party"] if r["actor_id"] == member
                      or key in PLAYER_TYPES and type(r.get("player_type")) is int
                      and r["player_type"] == PLAYER_TYPES[key]]
        if len(candidates) != 1 or type(candidates[0].get("hp")) is not int or candidates[0]["hp"] <= 0:
            raise ControlStopped("combat_character_unavailable")
        target = candidates[0]["actor_id"]
        if current == target:
            return result(True, "already_controlled")
        if not execute:
            return result(True, "planned")
        seen = {current}
        for _ in range(3):
            menu = read_menu()
            if type(menu.get("open")) is not bool or not menu.get("party_status"):
                raise ControlStopped("combat_menu_state_unobserved")
            if menu["open"]:
                raise ControlStopped("close_battle_command_menu_first")
            if deadline - clock() < 0.5:
                raise ControlStopped("combat_control_wait_expired")
            previous = current
            attempted += 1
            try:
                receipt = service.battle_menu_tap("up", 120)
            except Exception:
                uncertain = True
                raise
            if not isinstance(receipt, dict) or receipt.get("ok") is not True:
                uncertain = not isinstance(receipt, dict) or receipt.get("ok") is not False
                details["input_refusal"] = receipt
                raise ControlStopped("combat_control_input_unverified" if uncertain else "combat_control_input_refused")
            accepted += 1
            settled = min(deadline, clock() + 2.5)
            for _ in range(6):
                observe()
                if current != previous:
                    transitions.append({"source_sequence": sequence, "controlled_actor_id": current})
                    break
                if clock() + 0.5 >= settled:
                    raise ControlStopped("combat_controller_unchanged")
                sleep(0.5)
            if current == target:
                return result(True, "switched")
            if current == previous:
                raise ControlStopped("combat_controller_unchanged")
            if current in seen:
                raise ControlStopped("combat_character_unreachable")
            seen.add(current)
        raise ControlStopped("combat_character_unreachable")
    except ControlStopped as error:
        return result(False, str(error))
    except Exception as error:
        details.update(error_type=type(error).__name__, error=str(error)[:512])
        return result(False, "combat_control_interrupted")
