"""Offline, durable Queen's Blood campaign planning and evidence reconciliation.

The director selects objectives; it never operates telemetry or input backends.
Confirmed match history is the source of wins, not model-submitted checkboxes.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterator
import uuid

from .qb_opponents import load_opponents, opponent
from .queen_blood_runs import model_key
from .navigation_values import MissionError
from .public_stream_events import REASONING_EFFORTS
from .campaign_readiness import load_readiness, readiness_advisory


TWINS = "oba1310_Actor_CardGamer02"
YOUNGER = TWINS + "_Younger"
# Same reviewed equivalences as scripts/generate-qb-gauntlet.ts.
ALIASES = {
    "oba2610_Actor_CardGamer01_PreIcon": "oba2610_Actor_CardGamer01",
    "oba2350_Actor_Gentleman01_Af": "oba2350_Actor_Gentleman01_Bf",
    "oba2100_CardRiddle_01_AmorReggie": "oba2100_CardRiddle_01",
    "oba2100_CardRiddle_02_AmorReggie": "oba2100_CardRiddle_02",
    "oba2100_CardRiddle_03_AmorReggie": "oba2100_CardRiddle_03",
}
SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,200}$")
HISTORY_NAME = re.compile(r"^\d+-[a-f0-9-]{36}\.json$")
HISTORY_FOLDERS = {"live": "qb-match-history", "rehearsal": "qb-match-history-rehearsal"}
# Catalogued but not loaded nearby: no evidence about availability (#322).
LOOKUP_MISS_STATUSES = ("known_but_not_currently_instantiated", "known_but_no_runtime_location")


class CampaignError(ValueError):
    pass


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise CampaignError("invalid evidence timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc)
    except ValueError as exc:
        raise CampaignError("invalid evidence timestamp") from exc


def _scope(profile_id: str, driver: str, model_id: str, mode: str) -> None:
    if not isinstance(profile_id, str) or not SAFE_ID.fullmatch(profile_id):
        raise CampaignError("invalid campaign profile ID")
    model_key({"driver": driver, "model_id": model_id})
    if mode not in HISTORY_FOLDERS:
        raise CampaignError("campaign mode must be live or rehearsal")


def build_objectives(selectors: list[str] | None, allow_battles: bool,
                     catalogue: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Freeze the requested order and routes. Default: the 30 regional entries."""
    catalogue = catalogue if catalogue is not None else load_opponents()
    if selectors is None:
        selectors = [row["id"] for row in catalogue["opponents"]
                     if row.get("category") == "regional_player" and row["id"] not in ALIASES]
    if not isinstance(selectors, list) or not 1 <= len(selectors) <= 200:
        raise CampaignError("provide 1 to 200 ordered opponents")
    objectives = []
    seen = set()
    for selector in selectors:
        row = opponent(selector, catalogue)
        identifier = ALIASES.get(row["id"], row["id"])
        if identifier == YOUNGER:
            identifier = TWINS
        row = opponent(identifier, catalogue)
        if identifier in seen:
            raise CampaignError("duplicate campaign opponent")
        seen.add(identifier)
        members = [(row.get("display_name") or identifier, identifier)]
        if identifier == TWINS and allow_battles:
            members = [("Devin", YOUNGER), ("Darren", TWINS)]
        requirements = []
        for name, member_id in members:
            member = opponent(member_id, catalogue)
            # Both twins are offered by the shared interaction host; result IDs
            # remain distinct. The public history uses this same game sequence.
            requirements.append({"member": name, "opponent_id": member_id,
                                 "target_id": row.get("canonical_id") or identifier,
                                 "travel_spot": (member.get("travel_spot") or {}).get("key")})
        region = row.get("region") or {}
        objectives.append({"opponent_id": identifier, "label": row.get("display_name") or identifier,
                           "region": region.get("key") or row.get("field"),
                           "region_name": region.get("name") or row.get("field_name"),
                           "required_matches": requirements})
    return objectives


def _history_record(value: Any) -> dict[str, Any]:
    """Validate completion facts in a raw v2 harness record, never public data.json."""
    if not isinstance(value, dict) or value.get("version") != 2:
        raise CampaignError("raw version 2 match history required")
    fields = {"version", "at", "opponent", "opponent_id", "driver", "result", "score", "margin", "lanes",
              "duration_seconds", "cards_played", "opponent_cards_played", "player_cards", "opponent_cards",
              "deck", "save_profile"}
    if set(value) != fields:
        raise CampaignError("invalid raw match history fields")
    _time(value.get("at"))
    driver, score = value.get("driver"), value.get("score")
    if not isinstance(driver, dict) or set(driver) != {"driver", "model_id", "effort", "run_id"}:
        raise CampaignError("unverified match driver")
    model_key(driver)
    if driver.get("effort") not in REASONING_EFFORTS:
        raise CampaignError("unverified match driver")
    if not isinstance(driver.get("run_id"), str):
        raise CampaignError("unverified match driver")
    try:
        uuid.UUID(driver["run_id"])
    except ValueError as exc:
        raise CampaignError("unverified match driver") from exc
    if not isinstance(score, dict) or set(score) != {"player", "opponent"}:
        raise CampaignError("invalid match score")
    if any(type(score[key]) is not int or not 0 <= score[key] <= 999 for key in score):
        raise CampaignError("invalid match score")
    difference = score["player"] - score["opponent"]
    result = "win" if difference > 0 else "loss" if difference < 0 else "draw"
    if value.get("result") != result or type(value.get("margin")) is not int or value["margin"] != difference:
        raise CampaignError("match result disagrees with score")
    if not isinstance(value.get("opponent_id"), str) or not SAFE_ID.fullmatch(value["opponent_id"]):
        raise CampaignError("match opponent ID required")
    if not isinstance(value.get("save_profile"), str) or not SAFE_ID.fullmatch(value["save_profile"]):
        raise CampaignError("match save profile required")
    lanes = value.get("lanes")
    if (not isinstance(lanes, list) or len(lanes) != 3 or
            any(not isinstance(lane, list) or len(lane) != 2 or
                any(type(total) is not int or not -999 <= total <= 999 for total in lane) for lane in lanes)):
        raise CampaignError("invalid match lanes")
    duration = value.get("duration_seconds")
    if duration is not None and (type(duration) not in (int, float) or not math.isfinite(duration) or not 0 <= duration <= 14400):
        raise CampaignError("invalid match duration")
    occupied = set()
    for side in ("player", "opponent"):
        cards = value[side + "_cards"]
        count = value["opponent_cards_played" if side == "opponent" else "cards_played"]
        if not isinstance(cards, list) or len(cards) > 15 or type(count) is not int or count != len(cards):
            raise CampaignError("invalid match card counts")
        for card in cards:
            if (not isinstance(card, dict) or set(card) != {"card_id", "name", "row", "column", "power"} or
                    type(card["row"]) is not int or not 0 <= card["row"] <= 2 or
                    type(card["column"]) is not int or not 0 <= card["column"] <= 4):
                raise CampaignError("invalid match card position")
            cell = (card["row"], card["column"])
            if cell in occupied:
                raise CampaignError("invalid match card position")
            occupied.add(cell)
    if value["deck"] is not None and (not isinstance(value["deck"], list) or len(value["deck"]) != 15 or
                                     any(not isinstance(card, str) or not SAFE_ID.fullmatch(card) for card in value["deck"])):
        raise CampaignError("invalid match deck")
    return value


class CampaignDirector:
    """SQLite transactions preserve immutable plans and idempotent evidence.

    One campaign per profile and mode prevents new plans from silently resetting
    a run. A fresh production run uses a fresh working-save/model profile.
    """

    def __init__(self, root: Path) -> None:
        self.path = Path(root) / "campaigns.sqlite3"

    @contextmanager
    def _db(self, *, create: bool = False) -> Iterator[sqlite3.Connection]:
        if not create and not self.path.exists():
            raise CampaignError("campaign store does not exist")
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = None
        try:
            connection = sqlite3.connect(self.path, timeout=5)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 0 and create:
                if connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
                    raise CampaignError("unsupported campaign database")
                connection.execute("CREATE TABLE campaigns (id TEXT PRIMARY KEY, profile TEXT NOT NULL, "
                                   "mode TEXT NOT NULL, plan TEXT NOT NULL, UNIQUE(profile, mode))")
                connection.execute("CREATE TABLE receipts (campaign TEXT NOT NULL REFERENCES campaigns(id), "
                                   "id TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(campaign, id))")
                connection.execute("CREATE TABLE selections (campaign TEXT PRIMARY KEY REFERENCES campaigns(id), "
                                   "target TEXT NOT NULL)")
                connection.execute("PRAGMA user_version=1")
            elif version != 1:
                raise CampaignError("unsupported campaign database version")
            yield connection
            connection.commit()
        except sqlite3.Error as exc:
            raise CampaignError("campaign database unavailable or invalid") from exc
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _plan(db: sqlite3.Connection, campaign_id: str) -> dict[str, Any]:
        row = db.execute("SELECT plan FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        if row is None:
            raise CampaignError("campaign not found")
        try:
            plan = json.loads(row[0])
            if plan["version"] != 1 or not isinstance(plan["objectives"], list) or not plan["objectives"]:
                raise ValueError()
            _scope(plan["profile_id"], plan["driver"], plan["model_id"], plan["mode"])
            _time(plan["created_at"])
            if (plan["campaign_id"] != campaign_id or type(plan["max_attempts"]) is not int or
                    not 1 <= plan["max_attempts"] <= 10 or type(plan["allow_battles"]) is not bool or
                    type(plan["include_existing_history"]) is not bool or
                    plan["catalogue_digest"] != hashlib.sha256(_json(plan["objectives"]).encode()).hexdigest()):
                raise ValueError()
            return plan
        except (ValueError, KeyError, TypeError) as exc:
            raise CampaignError("campaign plan invalid") from exc

    def create(self, profile_id: str, driver: str, model_id: str, *, mode: str = "live",
               opponents: list[str] | None = None, allow_battles: bool = True,
               include_existing_history: bool = False, campaign_id: str | None = None,
               catalogue: dict[str, Any] | None = None, max_attempts: int = 3) -> dict[str, Any]:
        _scope(profile_id, driver, model_id, mode)
        if type(allow_battles) is not bool or type(include_existing_history) is not bool:
            raise CampaignError("campaign policies must be booleans")
        if type(max_attempts) is not int or not 1 <= max_attempts <= 10:
            raise CampaignError("max_attempts must be between 1 and 10")
        campaign_id = campaign_id or str(uuid.uuid4())
        if not isinstance(campaign_id, str) or not SAFE_ID.fullmatch(campaign_id):
            raise CampaignError("invalid campaign ID")
        objectives = build_objectives(opponents, allow_battles, catalogue)
        plan = {"version": 1, "campaign_id": campaign_id, "created_at": _now(),
                "profile_id": profile_id, "driver": driver, "model_id": model_id.lower(),
                "mode": mode, "allow_battles": allow_battles,
                "max_attempts": max_attempts,
                "include_existing_history": include_existing_history, "objectives": objectives,
                "catalogue_digest": hashlib.sha256(_json(objectives).encode()).hexdigest()}
        with self._db(create=True) as db:
            existing = db.execute("SELECT id FROM campaigns WHERE profile=? AND mode=?",
                                  (profile_id, mode)).fetchone()
            if existing is not None:
                previous = self._plan(db, existing[0])
                comparable = set(plan) - {"campaign_id", "created_at"}
                if all(plan[key] == previous[key] for key in comparable):
                    return {"ok": True, "input_sent": False, "resumed": True, "plan": previous}
                raise CampaignError("this profile already has a different campaign; existing plan preserved")
            db.execute("INSERT INTO campaigns VALUES (?, ?, ?, ?)",
                       (campaign_id, profile_id, mode, _json(plan)))
        return {"ok": True, "input_sent": False, "resumed": False, "plan": plan}

    def plans(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with self._db() as db:
            return [self._plan(db, row[0]) for row in db.execute("SELECT id FROM campaigns ORDER BY rowid")]

    @staticmethod
    def _receipt(db: sqlite3.Connection, campaign_id: str, event_id: str, evidence: dict[str, Any]) -> bool:
        payload = _json(evidence)
        previous = db.execute("SELECT payload FROM receipts WHERE campaign=? AND id=?",
                              (campaign_id, event_id)).fetchone()
        if previous is not None:
            if previous[0] != payload:
                raise CampaignError("campaign evidence changed; original receipt preserved")
            return False
        db.execute("INSERT INTO receipts VALUES (?, ?, ?)", (campaign_id, event_id, payload))
        return True

    def reconcile(self, campaign_id: str, state_dir: Path) -> dict[str, Any]:
        """Read the owner's raw history without changing or publishing any source.

        Like qb-history.ts, resolve older shared-host twin IDs chronologically
        using all history for the exact save/model, even when early matches are
        outside this campaign's time window. Explicit younger IDs always mean Devin.
        """
        rejected, ignored, imported = [], 0, 0
        with self._db() as db:
            plan = self._plan(db, campaign_id)
            folder = Path(state_dir) / HISTORY_FOLDERS[plan["mode"]]
            records = []
            if not folder.is_dir():
                return {"ok": True, "input_sent": False, "imported": 0, "ignored": 0, "rejected": [],
                        "history_available": False}
            for path in sorted(folder.glob("*.json")):
                if not HISTORY_NAME.fullmatch(path.name):
                    continue
                try:
                    record = _history_record(json.loads(path.read_text(encoding="utf-8")))
                except (OSError, ValueError, TypeError) as exc:
                    rejected.append({"file": str(path), "reason": str(exc) if isinstance(exc, CampaignError)
                                     else "unreadable match history"})
                    continue
                declared = record["driver"]
                if (record["save_profile"] != plan["profile_id"] or declared["driver"] != plan["driver"]
                        or declared["model_id"].lower() != plan["model_id"]):
                    ignored += 1
                    continue
                records.append((path, record))
            # Stats reset may archive the original folder between queries. Merge
            # already reconciled records so the twin sequence retains its first
            # win without changing or searching the owner's public-stat archives.
            present_names = {path.name for path, _ in records}
            for saved in db.execute("SELECT payload FROM receipts WHERE campaign=?", (campaign_id,)):
                evidence = json.loads(saved[0])
                if evidence["kind"] == "match":
                    source = Path(evidence["source"])
                    if source.name not in present_names:
                        records.append((source, evidence["record"]))
            records.sort(key=lambda item: (_time(item[1]["at"]), item[0].name))
            devin_won = False
            valid_ids = {requirement["opponent_id"] for objective in plan["objectives"]
                         for requirement in objective["required_matches"]}
            for path, record in records:
                identifier = ALIASES.get(record["opponent_id"], record["opponent_id"])
                inferred = False
                if identifier in {TWINS, YOUNGER}:
                    explicit = {"Devin (The Moore Twins)": YOUNGER, "Darren (The Moore Twins)": TWINS}
                    if record.get("opponent") in explicit:
                        identifier = explicit[record["opponent"]]
                    else:
                        inferred = identifier == TWINS
                        identifier = YOUNGER if identifier == YOUNGER or not devin_won else TWINS
                    if identifier == YOUNGER and record["result"] == "win":
                        devin_won = True
                if (not plan["allow_battles"] or identifier not in valid_ids or
                        (not plan["include_existing_history"] and _time(record["at"]) < _time(plan["created_at"]))):
                    ignored += 1
                    continue
                evidence = {"kind": "match", "opponent_id": identifier, "result": record["result"],
                            "at": record["at"], "source": str(path.resolve()), "record": record,
                            "identity_inferred": inferred}
                imported += self._receipt(db, campaign_id, "history:" + path.name, evidence)
        return {"ok": True, "input_sent": False, "imported": imported, "ignored": ignored, "rejected": rejected,
                "history_available": True}

    def record_action(self, profile_id: str, driver: str, model_id: str, mode: str,
                      action: dict[str, Any], speech: dict[str, Any] | None = None) -> bool:
        """Internal service hook. There is deliberately no MCP mark-won operation."""
        if not self.path.exists():
            return False
        with self._db() as db:
            row = db.execute("SELECT id FROM campaigns WHERE profile=? AND mode=?", (profile_id, mode)).fetchone()
            if row is None:
                return False
            plan = self._plan(db, row[0])
            if driver != plan["driver"] or model_id.lower() != plan["model_id"]:
                return False
            selector = action.get("opponent") or action.get("target_id")
            target = action.get("target")
            if not selector and isinstance(target, dict):
                selector = target.get("id") or target.get("canonical_id") or target.get("name")
            if not selector:
                selection = db.execute("SELECT target FROM selections WHERE campaign=?", (row[0],)).fetchone()
                selector = selection[0] if selection else None
            identifier = None
            if isinstance(selector, str):
                try:
                    selected_id = opponent(selector)["id"]
                    identifier = ALIASES.get(selected_id, selected_id)
                except (ValueError, MissionError):
                    pass
            if identifier == YOUNGER:
                identifier = TWINS
            if identifier not in {objective["opponent_id"] for objective in plan["objectives"]}:
                return False
            kind = "action"
            if (not plan["allow_battles"] and action.get("action") == "initiate_queens_blood_match"
                    and action.get("ok") is True and action.get("outcome") == "declined"):
                kind = "visit"
            evidence = {"kind": kind, "opponent_id": identifier, "at": action["timestamp_utc"],
                        "action": action, "speech": speech}
            return self._receipt(db, row[0], "action:" + str(uuid.uuid4()), evidence)

    def status(self, campaign_id: str) -> dict[str, Any]:
        with self._db() as db:
            plan = self._plan(db, campaign_id)
            receipts = [json.loads(row[0]) for row in db.execute(
                "SELECT payload FROM receipts WHERE campaign=? ORDER BY rowid", (campaign_id,))]
        objectives = []
        receipts.sort(key=lambda receipt: (_time(receipt["at"]), receipt.get("source", "")))
        progress = len({receipt["opponent_id"] for receipt in receipts
                        if receipt["kind"] == "match" and receipt.get("result") == "win"})
        won_ids: set[str] = set()
        for receipt in receipts:
            receipt["progress_at_record"] = len(won_ids)
            if receipt["kind"] == "match" and receipt.get("result") == "win":
                won_ids.add(receipt["opponent_id"])
        for objective in plan["objectives"]:
            requirements = []
            for requirement in objective["required_matches"]:
                matches = [receipt for receipt in receipts if receipt["opponent_id"] == requirement["opponent_id"]
                           and receipt["kind"] == ("match" if plan["allow_battles"] else "visit")]
                wins = [receipt for receipt in matches if receipt.get("result") == "win"]
                complete = bool(wins if plan["allow_battles"] else matches)
                requirements.append({**requirement, "state": "won" if complete else "lost" if matches else "pending",
                                     "completed": complete, "attempts": len(matches), "wins": len(wins),
                                     "evidence": matches})
            actions = [receipt for receipt in receipts if receipt["opponent_id"] == objective["opponent_id"]
                       and receipt["kind"] == "action"]
            last_failure = None
            for receipt in actions:
                action = receipt["action"]
                failure = str(action.get("reason") or action.get("rejection_reason") or action.get("status") or "")
                if action.get("ok") is False and any(token in failure for token in LOOKUP_MISS_STATUSES):
                    continue
                if action.get("ok") is False:
                    if action.get("action") != "capture_game_window" or last_failure is None:
                        last_failure = receipt
                elif action.get("ok") is True:
                    retried_failure = last_failure is not None and action.get("action") == last_failure["action"].get("action")
                    conversation_recovered = (action.get("action") == "finish_conversation" and last_failure is not None and
                                              any(token in str(last_failure["action"]) for token in (
                                                  "dialogue_active", "field_telemetry_unavailable")))
                    if action.get("action") == "initiate_queens_blood_match" or retried_failure or conversation_recovered:
                        last_failure = None
            complete = all(item["completed"] for item in requirements)
            state = "won" if complete else "lost" if any(item["state"] == "lost" for item in requirements) else "pending"
            reason = "confirmed_win" if complete else "retry_after_loss" if state == "lost" else "not_attempted"
            retry_ready = False
            if last_failure is not None and not complete:
                last_failure = {**last_failure, "screenshots": [receipt["action"] for receipt in actions
                                if receipt["action"].get("action") == "capture_game_window"
                                and receipt["action"].get("ok") is True and _time(receipt["at"]) >= _time(last_failure["at"])]}
                action = last_failure["action"]
                failure = str(action.get("reason") or action.get("rejection_reason") or action.get("status") or "")
                state = "locked" if any(token in failure for token in (
                    "no_match_prompt", "npc_absent", "live_actor_not_found")) else "blocked"
                reason = failure or "harness_failure"
                retry_ready = state == "locked" and progress > last_failure["progress_at_record"]
                # A later confirmed result clears an old failure even across restarts.
                if any(_time(match["at"]) > _time(last_failure["at"]) for item in requirements for match in item["evidence"]):
                    state, reason = "lost", "retry_after_loss"
            objectives.append({**objective, "required_matches": requirements,
                               "completed": complete, "state": state, "reason": reason,
                               "attempts": sum(item["attempts"] for item in requirements),
                               "locked_retry_ready": retry_ready, "failure_evidence": last_failure})
        next_step, stop_reason = self._next(plan, objectives, receipts)
        # Cross-save readiness is advice only. It cannot grant wins, unlock a
        # profile's targets, reorder objectives or authorize a cached route.
        readiness = load_readiness()
        for objective in objectives:
            objective["readiness"] = readiness_advisory(readiness, objective["opponent_id"])
        if next_step is not None:
            next_step["readiness"] = readiness_advisory(readiness, next_step["opponent_id"])
        return {"ok": True, "input_sent": False, "campaign_id": campaign_id,
                "plan": plan, "state": "completed" if all(item["completed"] for item in objectives) else "pending",
                "completed_opponents": [item["label"] for item in objectives if item["completed"]],
                "remaining_opponents": [item["label"] for item in objectives if not item["completed"]],
                "objectives": objectives, "next_step": next_step,
                "readiness_index": {key: value for key, value in readiness.items() if key != "opponents"},
                "reason": stop_reason,
                "last_action": next((receipt for receipt in reversed(receipts) if receipt["kind"] == "action"), None),
                "receipt_count": len(receipts)}

    def next_step(self, campaign_id: str) -> dict[str, Any]:
        status = self.status(campaign_id)
        step = status["next_step"]
        if step is not None:
            target = TWINS if step["opponent_id"] == YOUNGER else step["opponent_id"]
            with self._db() as db:
                db.execute("INSERT INTO selections VALUES (?, ?) ON CONFLICT(campaign) DO UPDATE SET target=excluded.target",
                           (campaign_id, target))
        return {key: status[key] for key in ("ok", "input_sent", "campaign_id", "state", "reason", "next_step",
                                            "completed_opponents", "remaining_opponents")}

    @staticmethod
    def _next(plan: dict[str, Any], objectives: list[dict[str, Any]],
              receipts: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str]:
        regions = list(dict.fromkeys(item["region"] for item in objectives))
        by_id = {item["opponent_id"]: item for item in objectives}
        for item in objectives:
            failure = item["failure_evidence"]
            if item["state"] == "blocked" and any(token in item["reason"] for token in (
                    "dialogue_active", "field_telemetry_unavailable")):
                return None, "capture_game_window_required" if not failure["screenshots"] else "harness_recovery_required"
        last_target = next((TWINS if receipt["opponent_id"] == YOUNGER else receipt["opponent_id"]
                            for receipt in reversed(receipts)
                            if receipt["opponent_id"] in by_id or receipt["opponent_id"] == YOUNGER), None)
        current_region = by_id[last_target]["region"] if last_target in by_id else None
        candidates = []
        for index, objective in enumerate(objectives):
            if objective["state"] in {"won", "blocked"}:
                continue
            if objective["state"] == "locked" and not objective["locked_retry_ready"]:
                continue
            # Zogan is never offered while either required twin remains unfinished.
            if objective["label"] == "Zogan" and TWINS in by_id and not by_id[TWINS]["completed"]:
                continue
            requirement = next(item for item in objective["required_matches"] if not item["completed"])
            if requirement["attempts"] >= plan["max_attempts"]:
                continue
            priority = 1 if objective["state"] == "lost" else 0
            same_region = 0 if objective["region"] == current_region else 1
            candidates.append(((priority, same_region, regions.index(objective["region"]), index), objective, requirement))
        if not candidates:
            if all(item["completed"] for item in objectives):
                return None, "campaign_completed"
            if any(item["state"] == "blocked" for item in objectives):
                return None, "harness_recovery_required"
            if any(any(member["attempts"] >= plan["max_attempts"] and not member["completed"]
                       for member in item["required_matches"]) for item in objectives):
                return None, "attempt_limit_reached"
            return None, "waiting_for_progress_to_unlock_opponents"
        _, objective, requirement = min(candidates, key=lambda item: item[0])
        reason = ("revisit_locked_after_progress" if objective["state"] == "locked" else
                  "retry_loss_after_other_opponents" if objective["state"] == "lost" else
                  "finish_required_twin" if objective["opponent_id"] == TWINS else
                  "continue_current_region" if objective["region"] == current_region else "next_region")
        return {"opponent": objective["label"], "member": requirement["member"],
                "opponent_id": requirement["opponent_id"], "target_id": requirement["target_id"],
                "travel_spot": requirement["travel_spot"], "region": objective["region_name"],
                "reason": reason, "state": objective["state"], "attempts": requirement["attempts"],
                "max_attempts": plan["max_attempts"], "allow_battles": plan["allow_battles"],
                "requires_fresh_observation": True, "pursuit": True,
                "completion": "confirmed_history_win" if plan["allow_battles"] else "outcome_declined"}, reason


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="isolated campaign store directory")
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("--profile", required=True)
    create.add_argument("--driver", required=True)
    create.add_argument("--model", required=True)
    create.add_argument("--mode", choices=tuple(HISTORY_FOLDERS), default="live")
    create.add_argument("--opponent", action="append", dest="opponents")
    create.add_argument("--visits-only", action="store_true")
    create.add_argument("--include-existing-history", action="store_true")
    create.add_argument("--id", dest="campaign_id")
    create.add_argument("--max-attempts", type=int, default=3)
    for command in ("status", "next", "sync"):
        child = subparsers.add_parser(command)
        child.add_argument("campaign_id")
        if command == "sync":
            child.add_argument("--state-dir", type=Path, required=True)
    subparsers.add_parser("list")
    args = parser.parse_args()
    director = CampaignDirector(args.root)
    try:
        if args.command == "create":
            result = director.create(args.profile, args.driver, args.model, mode=args.mode,
                                     opponents=args.opponents, allow_battles=not args.visits_only,
                                     include_existing_history=args.include_existing_history, campaign_id=args.campaign_id,
                                     max_attempts=args.max_attempts)
        elif args.command == "list":
            result = {"ok": True, "input_sent": False, "plans": director.plans()}
        elif args.command == "sync":
            result = director.reconcile(args.campaign_id, args.state_dir)
        else:
            result = director.next_step(args.campaign_id) if args.command == "next" else director.status(args.campaign_id)
        print(json.dumps(result, indent=2))
        return 0
    except (ValueError, OSError) as exc:
        print(json.dumps({"ok": False, "input_sent": False, "error": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
