"""Persistent Queen's Blood regional campaign routing for one model run.

Campaign state lives beside ``model-runs.json`` and is keyed by the immutable
run profile created by :mod:`queen_blood_runs` (driver/model/save slot). Match
results are reconciled from the save profile's ``opponents.json`` records,
which are written by ``finish_queens_blood_match``; callers cannot mark wins or
losses directly.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from threading import RLock
from typing import Any

from .qb_opponents import load_opponents
from .campaign_readiness import load_readiness, readiness_advisory
from .queen_blood_profile import _now

CAMPAIGN_VERSION = 2
MAX_LOSS_RETRIES = 2
MOORE_TWINS_ID = "oba1310_Actor_CardGamer02"
MOORE_TWINS_MEMBER_IDS = (
    "oba1310_Actor_CardGamer02_Younger",
    MOORE_TWINS_ID,
)
ZOGAN_ID = "oba1310_Actor_CardGamer03"
# A world lookup that finds the NPC in the catalogue but not loaded nearby. It
# proves nothing about availability: only a challenge attempt can lock (#322).
LOOKUP_MISS_STATUSES = ("known_but_not_currently_instantiated", "known_but_no_runtime_location")

# The required regional benchmark rows in src/queens-blood-gauntlet.ts.
REGIONAL_OPPONENT_IDS: tuple[str, ...] = (
    "oba1110_Actor_CardGamer01",
    "oba1110_Actor_CardGamer02",
    "oba1110_Actor_CardGamer03",
    "oba1310_Actor_CardGamer01",
    MOORE_TWINS_ID,
    ZOGAN_ID,
    "oba1300_Actor_CardGamer01",
    "oba1300_Actor_CardGamer02",
    "oba1300_Actor_CardGamer03",
    "oba2260_Actor_CardGamer01",
    "oba2260_Actor_CardGamer02",
    "oba2260_Actor_CardGamer03",
    "oba2350_Actor_Gentleman01_Bf",
    "oba2360_Actor_CardGamer01",
    "oba2360_Actor_CardGamer02",
    "oba2360_Actor_CardGamer03",
    "oba2360_Actor_Wise",
    "oba2410_Actor_CardGamer01",
    "oba2410_Actor_CardGamer02",
    "oba2410_Actor_CardGamer03",
    "oba2500_Actor_CardGamer01",
    "oba2500_Actor_CardGamer02",
    "oba2500_Actor_CardGamer03",
    "oba2610_Actor_CardGamer01",
    "oba2610_Actor_CardGamer02",
    "oba2610_Actor_CardGamer03",
    "oba2110_Actor_CardGamer01",
    "oba2110_Actor_CardGamer02",
    "oba2110_Actor_CardGamer03",
    "oba2400_Actor_CardGamer01",
)


def _atomic_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def _safe_evidence(value: dict[str, Any]) -> dict[str, Any]:
    """Bound persisted evidence to JSON values and a small trace tail."""
    clean = json.loads(json.dumps(value, default=str))
    trace = clean.get("trace")
    if isinstance(trace, list):
        clean["trace"] = trace[-12:]
    return clean


def _is_finish_record(item: dict[str, Any]) -> bool:
    """True only for finish_queens_blood_match records, including PR #209 legacy rows."""
    source = item.get("source")
    if source is not None:
        return source == "finish_queens_blood_match"
    # Before the source tag, finish_queens_blood_match wrote elapsed_seconds
    # plus moves=agent; play_queens_blood_match wrote actions/final_lanes instead.
    elapsed = item.get("elapsed_seconds")
    return (
        item.get("moves") == "agent"
        and type(elapsed) in {int, float}
        and "actions" not in item
        and "final_lanes" not in item
    )


def _is_live(item: dict[str, Any]) -> bool:
    return item.get("mode") == "live"


def _is_credited(item: dict[str, Any]) -> bool:
    """A finish counts towards the campaign only if the save slot reflects it.

    Owner decision 2026-10-08: rehearsal and test matches never count, and a
    live win counts only after a verified save to the run's own slot (the
    game's autosave goes to the shared autosave slot). Live losses need no
    save. Records without a mode predate this rule and are not credited.
    """
    return _is_live(item) and (item.get("won") is not True or bool(item.get("saved_at")))


def _is_unsaved_live_win(item: dict[str, Any]) -> bool:
    return _is_live(item) and item.get("won") is True and not item.get("saved_at")


class QueenBloodCampaign:
    """One persisted director store shared by all registered model runs."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.path = self.root / "campaigns.json"
        self._lock = RLock()
        catalogue = load_opponents()
        by_id = {row.get("id"): row for row in catalogue["opponents"]}
        rows: list[dict[str, Any]] = []
        for opponent_id in REGIONAL_OPPONENT_IDS:
            row = by_id.get(opponent_id)
            if not isinstance(row, dict) or row.get("category") != "regional_player":
                raise ValueError(f"regional Queen's Blood opponent is missing: {opponent_id}")
            if not isinstance(row.get("canonical_id"), str) or not isinstance(row.get("travel_spot"), dict):
                raise ValueError(f"regional Queen's Blood route is incomplete: {opponent_id}")
            rows.append(row)
        self._rows = tuple(rows)
        self._row_by_id = {row["id"]: row for row in rows}
        self._index_by_id = {row["id"]: index for index, row in enumerate(rows)}
        self._member_rows = {row["id"]: (row,) for row in rows}
        moore_members: list[dict[str, Any]] = []
        for member_id in MOORE_TWINS_MEMBER_IDS:
            member = by_id.get(member_id)
            if not isinstance(member, dict):
                raise ValueError(f"Moore Twins Queen's Blood opponent is missing: {member_id}")
            if not isinstance(member.get("canonical_id"), str) or not isinstance(member.get("travel_spot"), dict):
                raise ValueError(f"Moore Twins Queen's Blood route is incomplete: {member_id}")
            moore_members.append(member)
        self._member_rows[MOORE_TWINS_ID] = tuple(moore_members)
        self._selectors: dict[str, str] = {}
        for row in rows:
            for member in self._member_rows[row["id"]]:
                for selector in (
                    member.get("id"),
                    member.get("canonical_id"),
                    member.get("actor_name"),
                    member.get("display_name"),
                ):
                    if isinstance(selector, str) and selector.strip():
                        self._selectors.setdefault(selector.strip().casefold(), row["id"])
        self._display_counts: dict[str, int] = {}
        for row in catalogue["opponents"]:
            display = row.get("display_name")
            if isinstance(display, str) and display.strip():
                key = display.strip().casefold()
                self._display_counts[key] = self._display_counts.get(key, 0) + 1
        self._region_order: list[str] = []
        for row in rows:
            region = self._region_name(row)
            if region not in self._region_order:
                self._region_order.append(region)

    @staticmethod
    def _region_name(row: dict[str, Any]) -> str:
        region = row.get("region")
        name = region.get("name") if isinstance(region, dict) else None
        return str(name or row.get("field_name") or row.get("field") or "Unknown")

    @staticmethod
    def _assignment_identity(assignment: dict[str, Any]) -> dict[str, Any]:
        required = ("driver", "model_id", "slot", "steam_id", "profile_id")
        if not isinstance(assignment, dict) or any(key not in assignment for key in required):
            raise ValueError("a Queen's Blood model-run assignment is required")
        if not isinstance(assignment["driver"], str) or not isinstance(assignment["model_id"], str):
            raise ValueError("the Queen's Blood model-run identity is invalid")
        if type(assignment["slot"]) is not int or not isinstance(assignment["steam_id"], str):
            raise ValueError("the Queen's Blood model-run save identity is invalid")
        if not isinstance(assignment["profile_id"], str) or not assignment["profile_id"].strip():
            raise ValueError("the Queen's Blood model-run profile is invalid")
        # A verified Save As advances the working file while the registry keeps
        # this run's original profile slot and ownership of previous files.
        anchor = assignment.get("profile_slot", assignment["slot"])
        previous = assignment.get("previous_slots", [])
        if (type(anchor) is not int or anchor == 14 or not 1 <= anchor <= 999
                or not isinstance(previous, list)
                or anchor not in [assignment["slot"], *previous]):
            raise ValueError("the Queen's Blood campaign profile slot is invalid")
        return {**{key: assignment[key] for key in required}, "slot": anchor}

    def _read_store(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": CAMPAIGN_VERSION, "campaigns": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError("the Queen's Blood campaign store is invalid") from exc
        if not isinstance(data, dict) or data.get("version") != CAMPAIGN_VERSION or not isinstance(data.get("campaigns"), dict):
            raise ValueError("the Queen's Blood campaign store is invalid")
        return data

    def _new_opponent(self, opponent_id: str) -> dict[str, Any]:
        members = self._member_rows[opponent_id]
        return {
            "state": "pending",
            "attempts": 0,
            "wins": 0,
            "losses": 0,
            "required_matches": len(members),
            "finish_records": 0,
            "member_results": {
                member["id"]: {
                    "attempts": 0,
                    "wins": 0,
                    "losses": 0,
                    "won": False,
                    "last_result": None,
                    "last_match_at": None,
                }
                for member in members
            },
            "locked_at_progress": None,
            "last_result": None,
            "last_match_at": None,
            "evidence": [],
        }

    def _new_campaign(self, assignment: dict[str, Any]) -> dict[str, Any]:
        identity = self._assignment_identity(assignment)
        return {
            **identity,
            "created_at": _now(),
            "updated_at": _now(),
            "progress": 0,
            "last_region": None,
            "opponents": {opponent_id: self._new_opponent(opponent_id) for opponent_id in REGIONAL_OPPONENT_IDS},
        }

    def _validate_campaign(self, campaign: dict[str, Any], identity: dict[str, Any]) -> None:
        if any(campaign.get(key) != value for key, value in identity.items()):
            raise ValueError("the Queen's Blood campaign does not match its model-run assignment")
        opponents = campaign.get("opponents")
        if not isinstance(opponents, dict) or set(opponents) != set(REGIONAL_OPPONENT_IDS):
            raise ValueError("the Queen's Blood campaign opponent set is invalid")
        for opponent_id, state in opponents.items():
            if not isinstance(state, dict) or state.get("state") not in {"pending", "won", "lost", "locked", "blocked"}:
                raise ValueError(f"the Queen's Blood campaign state is invalid for {opponent_id}")
            for key in ("attempts", "wins", "losses", "required_matches", "finish_records"):
                if type(state.get(key)) is not int or state[key] < 0:
                    raise ValueError(f"the Queen's Blood campaign counters are invalid for {opponent_id}")
            members = state.get("member_results")
            expected_members = {row["id"] for row in self._member_rows[opponent_id]}
            if not isinstance(members, dict) or set(members) != expected_members:
                raise ValueError(f"the Queen's Blood campaign member state is invalid for {opponent_id}")
            for member_id, member in members.items():
                if not isinstance(member, dict) or type(member.get("won")) is not bool:
                    raise ValueError(f"the Queen's Blood campaign member state is invalid for {member_id}")
                for key in ("attempts", "wins", "losses"):
                    if type(member.get(key)) is not int or member[key] < 0:
                        raise ValueError(f"the Queen's Blood campaign member counters are invalid for {member_id}")

    def _load_campaign(self, assignment: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        identity = self._assignment_identity(assignment)
        store = self._read_store()
        campaign = store["campaigns"].get(identity["profile_id"])
        if campaign is None:
            campaign = self._new_campaign(identity)
            store["campaigns"][identity["profile_id"]] = campaign
            _atomic_write(self.path, store)
        if not isinstance(campaign, dict):
            raise ValueError("the Queen's Blood campaign entry is invalid")
        self._validate_campaign(campaign, identity)
        return store, campaign

    def _profile_finish_records(
        self,
        profile_id: str,
        row: dict[str, Any],
    ) -> dict[str, list[dict[str, Any]]]:
        path = self.root / "profiles" / profile_id / "opponents.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {"opponents": {}}
        except (OSError, ValueError) as exc:
            raise ValueError("the Queen's Blood opponent finish records are invalid") from exc
        if not isinstance(data, dict) or not isinstance(data.get("opponents"), dict):
            raise ValueError("the Queen's Blood opponent finish records are invalid")

        result: dict[str, list[dict[str, Any]]] = {}
        for member in self._member_rows[row["id"]]:
            selectors = [member.get("canonical_id"), member.get("id"), member.get("actor_name")]
            display = member.get("display_name")
            if (
                isinstance(display, str)
                and self._display_counts.get(display.strip().casefold()) == 1
            ):
                selectors.append(display)
            found: list[dict[str, Any]] = []
            used: set[str] = set()
            for selector in selectors:
                if not isinstance(selector, str):
                    continue
                key = selector.strip().casefold()
                if not key or key in used:
                    continue
                used.add(key)
                record = data["opponents"].get(key)
                matches = record.get("matches") if isinstance(record, dict) else None
                if isinstance(matches, list):
                    found.extend(
                        item for item in matches
                        if isinstance(item, dict) and _is_finish_record(item)
                    )
            found.sort(key=lambda item: str(item.get("at") or ""))
            result[member["id"]] = found
        return result

    def _sync_finish_records(self, store: dict[str, Any], campaign: dict[str, Any]) -> bool:
        changed = False
        progress = 0
        latest_region: tuple[str, str] | None = None
        for row in self._rows:
            opponent_id = row["id"]
            state = campaign["opponents"][opponent_id]
            records = self._profile_finish_records(campaign["profile_id"], row)
            total_records = sum(len(matches) for matches in records.values())
            previous = state["finish_records"]
            if total_records < previous:
                raise ValueError(f"finish_queens_blood_match records regressed for {opponent_id}")

            members: dict[str, dict[str, Any]] = {}
            chronological: list[dict[str, Any]] = []
            credited_wins = 0
            losses = 0
            unsaved_wins = 0
            credited_attempts = 0
            for member in self._member_rows[opponent_id]:
                unsaved_wins += sum(1 for item in records[member["id"]] if _is_unsaved_live_win(item))
                matches = [item for item in records[member["id"]] if _is_credited(item)]
                actual_wins = sum(1 for item in matches if item.get("won") is True)
                member_losses = len(matches) - actual_wins
                member_won = actual_wins > 0
                credited_wins += 1 if member_won else 0
                losses += member_losses
                credited_attempts += len(matches)
                last = matches[-1] if matches else None
                members[member["id"]] = {
                    "attempts": len(matches),
                    "wins": actual_wins,
                    "losses": member_losses,
                    "won": member_won,
                    "last_result": (
                        {
                            "result": "win" if last.get("won") is True else "loss",
                            "score": last.get("score") if isinstance(last.get("score"), dict) else None,
                        }
                        if isinstance(last, dict) else None
                    ),
                    "last_match_at": last.get("at") if isinstance(last, dict) else None,
                }
                chronological.extend(matches)

            chronological.sort(key=lambda item: str(item.get("at") or ""))
            progress += credited_wins
            if state.get("unsaved_wins", 0) != unsaved_wins:
                state["unsaved_wins"] = unsaved_wins
                changed = True
            if not chronological and state["state"] in {"won", "lost"}:
                # Results credited under the earlier rule (rehearsal or unsaved
                # wins) no longer count; the save slot never held them.
                state["state"] = "pending"
                changed = True
            if total_records != previous or state.get("member_results") != members:
                state["attempts"] = credited_attempts
                state["wins"] = credited_wins
                state["losses"] = losses
                state["finish_records"] = total_records
                state["member_results"] = members
                last = chronological[-1] if chronological else None
                if isinstance(last, dict):
                    score = last.get("score") if isinstance(last.get("score"), dict) else None
                    state["last_result"] = {
                        "result": "win" if last.get("won") is True else "loss",
                        "score": score,
                    }
                    state["last_match_at"] = last.get("at")
                else:
                    state["last_result"] = None
                    state["last_match_at"] = None
                if credited_wins >= state["required_matches"]:
                    state["state"] = "won"
                elif isinstance(last, dict) and last.get("won") is True:
                    state["state"] = "pending"
                elif chronological:
                    state["state"] = "lost"
                state["locked_at_progress"] = None
                changed = True
            at = state.get("last_match_at")
            if isinstance(at, str) and (latest_region is None or at > latest_region[0]):
                latest_region = (at, self._region_name(row))
        if campaign.get("progress") != progress:
            campaign["progress"] = progress
            changed = True
        if latest_region is not None and campaign.get("last_region") is None:
            campaign["last_region"] = latest_region[1]
            changed = True
        if changed:
            campaign["updated_at"] = _now()
            _atomic_write(self.path, store)
        return changed

    def _next_member(self, row: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        results = state["member_results"]
        unfinished = [member for member in self._member_rows[row["id"]]
                      if results[member["id"]]["won"] is not True]
        if unfinished:
            # An owner may play the twins out of catalogue order. Retry the
            # recently lost unfinished member, preserving the other twin's win.
            latest = max(unfinished, key=lambda member: str(results[member["id"]].get("last_match_at") or ""))
            if (results[latest["id"]].get("last_result") or {}).get("result") == "loss":
                return latest
            return unfinished[0]
        return self._member_rows[row["id"]][-1]

    def _retry_losses(self, row: dict[str, Any], state: dict[str, Any]) -> int:
        member = self._next_member(row, state)
        return int(state["member_results"][member["id"]]["losses"])

    def _resolve(self, selector: str | None) -> dict[str, Any] | None:
        if not isinstance(selector, str):
            return None
        opponent_id = self._selectors.get(selector.strip().casefold())
        return self._row_by_id.get(opponent_id) if opponent_id else None

    def record_available(self, assignment: dict[str, Any], selector: str | None) -> bool:
        with self._lock:
            return self._record_available(assignment, selector)

    def _record_available(self, assignment: dict[str, Any], selector: str | None) -> bool:
        row = self._resolve(selector)
        if row is None:
            return False
        store, campaign = self._load_campaign(assignment)
        self._sync_finish_records(store, campaign)
        state = campaign["opponents"][row["id"]]
        if state["state"] == "won":
            return True
        state["state"] = "lost" if self._retry_losses(row, state) else "pending"
        state["locked_at_progress"] = None
        campaign["last_region"] = self._region_name(row)
        campaign["updated_at"] = _now()
        _atomic_write(self.path, store)
        return True

    def record_locked(self, assignment: dict[str, Any], selector: str | None, evidence: dict[str, Any]) -> bool:
        with self._lock:
            return self._record_locked(assignment, selector, evidence)

    def _record_locked(self, assignment: dict[str, Any], selector: str | None, evidence: dict[str, Any]) -> bool:
        row = self._resolve(selector)
        if row is None:
            return False
        store, campaign = self._load_campaign(assignment)
        self._sync_finish_records(store, campaign)
        state = campaign["opponents"][row["id"]]
        if state["state"] == "won":
            return True
        state["state"] = "locked"
        state["locked_at_progress"] = campaign["progress"]
        state["evidence"] = (state.get("evidence") or [])[-9:] + [_safe_evidence(evidence)]
        campaign["last_region"] = self._region_name(row)
        campaign["updated_at"] = _now()
        _atomic_write(self.path, store)
        return True

    def record_blocked(self, assignment: dict[str, Any], selector: str | None, evidence: dict[str, Any]) -> bool:
        with self._lock:
            return self._record_blocked(assignment, selector, evidence)

    def _record_blocked(self, assignment: dict[str, Any], selector: str | None, evidence: dict[str, Any]) -> bool:
        row = self._resolve(selector)
        if row is None:
            return False
        store, campaign = self._load_campaign(assignment)
        self._sync_finish_records(store, campaign)
        state = campaign["opponents"][row["id"]]
        if state["state"] == "won":
            return True
        state["state"] = "blocked"
        state["evidence"] = (state.get("evidence") or [])[-9:] + [_safe_evidence(evidence)]
        campaign["last_region"] = self._region_name(row)
        campaign["updated_at"] = _now()
        _atomic_write(self.path, store)
        return True

    def record_action(self, assignment: dict[str, Any], action: dict[str, Any],
                      speech: dict[str, Any] | None = None, *, mode: str = "live") -> bool:
        """Bounded action evidence; match outcomes remain finish-record authority."""
        if mode not in {"live", "rehearsal"}:
            raise ValueError("invalid campaign evidence mode")
        with self._lock:
            store, campaign = self._load_campaign(assignment)
            selector = action.get("opponent") or action.get("target_id")
            target = action.get("target")
            if not selector and isinstance(target, dict):
                selector = target.get("id") or target.get("canonical_id")
            # Director advice is not execution context. An owner can override it;
            # coordinates, prior actions and the previous suggestion prove no NPC.
            row = self._resolve(selector)
            if row is None:
                return False
            receipt = _safe_evidence({"opponent_id": row["id"], "at": action.get("timestamp_utc") or _now(),
                                      "action": action, "speech": speech, "mode": mode, "run": self._assignment_identity(assignment)})
            campaign["actions"] = (campaign.get("actions") or [])[-99:] + [receipt]
            state = campaign["opponents"][row["id"]]
            operation = action.get("action")
            failure = str(action.get("reason") or action.get("status") or "")
            if operation in {"resolve_world_target", "travel_to_map_spot", "run_live_route_adapter",
                             "plan_npc_approach", "finish_conversation"} and state["state"] != "won":
                if action.get("ok") is False and not any(token in failure for token in LOOKUP_MISS_STATUSES):
                    locked = any(token in failure for token in ("no_match_prompt", "npc_absent", "live_actor_not_found"))
                    state["state"] = "locked" if locked else "blocked"
                    state["locked_at_progress"] = campaign["progress"] if locked else None
                    state["evidence"] = (state.get("evidence") or [])[-9:] + [receipt]
                elif action.get("ok") is True and state["state"] in {"blocked", "locked"}:
                    previous = (state.get("evidence") or [{}])[-1].get("action", {})
                    if operation == previous.get("action") or (operation == "finish_conversation" and
                            any(token in str(previous) for token in ("dialogue_active", "field_telemetry_unavailable"))):
                        state["state"] = "lost" if self._retry_losses(row, state) else "pending"
                        state["locked_at_progress"] = None
            campaign["updated_at"] = _now()
            _atomic_write(self.path, store)
            return True

    def _public_opponent(self, row: dict[str, Any], state: dict[str, Any], progress: int) -> dict[str, Any]:
        locked_at = state.get("locked_at_progress")
        next_member = self._next_member(row, state)
        retry_losses = self._retry_losses(row, state)
        retry_exhausted = state["state"] == "lost" and retry_losses > MAX_LOSS_RETRIES
        result = {
            "id": row["id"],
            "name": row.get("display_name") or row["id"],
            "region": self._region_name(row),
            "state": state["state"],
            "attempts": state["attempts"],
            "wins": state["wins"],
            "losses": state["losses"],
            "required_matches": state["required_matches"],
            "unsaved_wins": state.get("unsaved_wins", 0),
            "retry_exhausted": retry_exhausted,
            "last_result": state.get("last_result"),
            "locked_at_progress": locked_at,
            "revisit_after_progress": (
                state["state"] == "locked" and isinstance(locked_at, int) and progress > locked_at
            ),
            "target_id": next_member.get("canonical_id"),
            "travel_spot": next_member.get("travel_spot"),
            "required_targets": [
                {
                    "id": member["id"],
                    "target_id": member.get("canonical_id"),
                    "won": state["member_results"][member["id"]]["won"],
                    "attempts": state["member_results"][member["id"]]["attempts"],
                    "wins": state["member_results"][member["id"]]["wins"],
                    "losses": state["member_results"][member["id"]]["losses"],
                }
                for member in self._member_rows[row["id"]]
            ],
        }
        if state["state"] in {"locked", "blocked"}:
            result["evidence"] = state.get("evidence") or []
        if row["id"] == ZOGAN_ID:
            result["gated_by"] = MOORE_TWINS_ID
        return result

    def status(self, assignment: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            return self._status(assignment)

    def _status(self, assignment: dict[str, Any]) -> dict[str, Any]:
        store, campaign = self._load_campaign(assignment)
        self._sync_finish_records(store, campaign)
        progress = campaign["progress"]
        opponents = [self._public_opponent(row, campaign["opponents"][row["id"]], progress) for row in self._rows]
        readiness = load_readiness()
        for item in opponents:
            item["readiness"] = readiness_advisory(readiness, item["id"])
        counts = {state: sum(1 for item in opponents if item["state"] == state)
                  for state in ("pending", "won", "lost", "locked", "blocked")}
        exhausted = sum(1 for item in opponents if item["retry_exhausted"])
        return {
            "ok": True,
            "profile_id": campaign["profile_id"],
            "run": {key: campaign[key] for key in ("driver", "model_id", "slot", "steam_id")},
            "campaign_file": str(self.path),
            "opponent_count": len(opponents),
            "required_match_count": sum(item["required_matches"] for item in opponents),
            "progress": progress,
            "last_region": campaign.get("last_region"),
            "max_loss_retries": MAX_LOSS_RETRIES,
            "summary": {**counts, "retry_exhausted": exhausted},
            "unsaved_live_wins": [item["name"] for item in opponents if item["unsaved_wins"]],
            "complete": counts["won"] == len(opponents),
            "last_action": (campaign.get("actions") or [None])[-1],
            "receipt_count": len(campaign.get("actions") or []),
            "opponents": opponents,
            "input_sent": False,
            "readiness_index": {key: value for key, value in readiness.items() if key != "opponents"},
        }

    def next_step(self, assignment: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            return self._next_step(assignment)

    def _next_step(self, assignment: dict[str, Any]) -> dict[str, Any]:
        store, campaign = self._load_campaign(assignment)
        self._sync_finish_records(store, campaign)
        progress = campaign["progress"]
        for opponent_id, state in campaign["opponents"].items():
            evidence = (state.get("evidence") or [{}])[-1]
            if state["state"] == "blocked" and any(token in str(evidence) for token in (
                    "dialogue_active", "field_telemetry_unavailable")):
                captured = any(item.get("opponent_id") == opponent_id and
                               item["action"].get("action") == "capture_game_window" and
                               item["action"].get("ok") is True and item["at"] >= evidence.get("at", "")
                               for item in campaign.get("actions", []))
                return {**self._status(assignment), "opponent": None, "target_id": None, "travel_spot": None,
                        "reason": "harness_recovery_required" if captured else "capture_game_window_required"}
        unsaved = [row for row in self._rows if campaign["opponents"][row["id"]].get("unsaved_wins", 0)]
        if unsaved:
            # Never route on with a win the save slot does not hold yet.
            names = ", ".join(str(row.get("display_name") or row["id"]) for row in unsaved)
            return {**self._status(assignment), "opponent": None, "target_id": None, "travel_spot": None,
                    "action": "save_game_progress",
                    "reason": f"save the game to the run's slot before the next opponent; unsaved live win: {names}"}
        # Owner decision 2026-10-08: one fixed order (the catalogue order, Kalm
        # to the Shadowblood Queen), whatever the player's location. A loss is
        # therefore retried straight away: it is the earliest unwon entry.
        moore_won = campaign["opponents"][MOORE_TWINS_ID]["state"] == "won"
        candidates: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
        for row in self._rows:
            state = campaign["opponents"][row["id"]]
            if state["state"] in {"won", "blocked"}:
                continue
            if row["id"] == ZOGAN_ID and not moore_won:
                continue
            if state["state"] == "locked":
                locked_at = state.get("locked_at_progress")
                if not isinstance(locked_at, int) or progress <= locked_at:
                    continue
            elif state["state"] == "lost" and self._retry_losses(row, state) > MAX_LOSS_RETRIES:
                continue
            candidates.append((self._index_by_id[row["id"]], row, state))

        if not candidates:
            status = self._status(assignment)
            if status["complete"]:
                reason = "all 30 regional Queen's Blood opponents are won"
            else:
                reason = "no actionable opponent: remaining entries are locked without new progress, blocked, retry-exhausted, or dependency-gated"
            return {**status, "opponent": None, "target_id": None, "travel_spot": None, "reason": reason}

        _index, row, state = min(candidates, key=lambda item: item[0])
        next_member = self._next_member(row, state)
        retry_losses = self._retry_losses(row, state)
        # A retry is of the member actually lost to; replaying a won twin and
        # losing makes the entry "lost" without the other twin being played.
        member_last = state["member_results"][next_member["id"]].get("last_result") or {}
        immediate_retry = state["state"] == "lost" and member_last.get("result") == "loss"
        if immediate_retry:
            reason = f"retry immediately after a loss; {MAX_LOSS_RETRIES - retry_losses + 1} attempt(s) remain"
        elif state["state"] == "locked":
            reason = f"revisit after campaign progress advanced from {state['locked_at_progress']} to {progress}"
        else:
            reason = "next opponent in the fixed campaign order"
        if row["id"] == MOORE_TWINS_ID:
            next_match = min(state["wins"] + 1, state["required_matches"])
            twin_reason = (
                f"Moore Twins match {next_match} of {state['required_matches']} is required before Zogan; "
                f"target the unfinished twin {next_member['id']}"
            )
            reason = f"{reason}; {twin_reason}" if immediate_retry else twin_reason
        campaign["selected_opponent"] = row["id"]
        _atomic_write(self.path, store)
        public = self._public_opponent(row, state, progress)
        return {
            "ok": True,
            "profile_id": campaign["profile_id"],
            "complete": False,
            "max_loss_retries": MAX_LOSS_RETRIES,
            "opponent": {key: public[key] for key in ("id", "name", "region", "state", "attempts", "wins", "losses", "required_matches")},
            "input_sent": False,
            "readiness": readiness_advisory(load_readiness(), row["id"]),
            "target_id": next_member["canonical_id"],
            # Always the route: a retry may follow a reload or crash recovery.
            "travel_spot": next_member["travel_spot"],
            "reason": reason,
        }


def classify_initiation_failure(message: str) -> str | None:
    """Classify target-specific initiation failures without caller judgment."""
    text = str(message).casefold()
    blocked = (
        "telemetry is stale",
        "telemetry stopped",
        "game telemetry reports that the game is stopped",
        "state did not advance",
        "telemetry did not advance",
        "sidecar is no longer fresh",
        "pre-match screen is no longer visible",
        "match screen was not observed",
        "hand identities did not appear",
        "npc interaction is ambiguous",
        "npc target resolution exceeded the action deadline",
        "dialogue wheel did not appear before the timeout",
        "deck screen did not appear before the timeout",
        "game may have hung",
    )
    if any(marker in text for marker in blocked):
        return "blocked"
    locked = (
        "no_match_prompt:",
        "conversation ended without the match prompt",
        "npc target is unavailable",
    )
    if any(marker in text for marker in locked):
        return "locked"
    return None
