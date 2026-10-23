"""Advance ordinary NPC lines only from fresh, positive rendered UI evidence."""
from __future__ import annotations

from datetime import datetime, timezone
import math
import time
from typing import Any

from .map_query import request_snapshot
from .navigation_values import MissionError
from .conversation_state import has_field_telemetry

MAX_READ_SECONDS = 1.25
CLICK_MILLISECONDS = 20
MAX_CLICKS = 60
OPENING_WAIT_SECONDS = 4.0


def signal(roots: Any, base: dict, cinema: dict) -> dict:
    result = {"dialogue_present": False, "fast_forwardable": False, "reason": "dialogue_unknown"}
    if not isinstance(roots, dict) or roots.get("observed") is not True or roots.get("configured_complete") is not True:
        return result
    widgets = roots.get("widgets")
    if not isinstance(widgets, list) or any(not isinstance(w, dict) for w in widgets):
        return result
    if any(not isinstance(w.get("details"), dict) for w in widgets):
        return result
    result["interactive_ui"] = [w.get("class") for w in widgets
                                if w.get("class") != "Subtitle00_C"
                                and w["details"].get("in_viewport_ex") is True]
    subtitles = [w for w in widgets if w.get("class") == "Subtitle00_C"
                 and w["details"].get("in_viewport_ex") is True]
    result["dialogue_present"] = bool(subtitles)
    if len(subtitles) != 1:
        result["reason"] = "subtitle_absent_or_ambiguous"
        if (not subtitles and isinstance(base, dict)
                and base.get("running") is True and (base.get("telemetry") or {}).get("live") is True
                and base.get("map_id") and (base.get("player") or {}).get("position")
                and all(w.get("configured_root") is True
                        and w["details"].get("in_viewport_ex") is False for w in widgets)):
            result["reason"] = "conversation_finished"
        return result
    for widget in widgets:
        if widget.get("configured_root") is not True:
            result["reason"] = "overlay_unknown"
            return result
        attached = (widget.get("details") or {}).get("in_viewport_ex")
        if type(attached) is not bool:
            result["reason"] = "overlay_unknown"
            return result
        if widget.get("class") != "Subtitle00_C" and attached:
            result["reason"] = "decision_or_menu_visible"
            return result
    if not isinstance(base, dict) or not isinstance(cinema, dict):
        return result
    if base.get("running") is not True or (base.get("telemetry") or {}).get("live") is not True:
        result["reason"] = "telemetry_unavailable"
        return result
    if any(type(base.get(k)) is not bool or base[k] for k in ("paused", "in_menu", "in_combat", "loading")):
        result["reason"] = "game_context_blocked"
        return result
    if base.get("target") is not None or (base.get("map_id") and (base.get("player") or {}).get("position")):
        result["reason"] = "outside_conversation"
        return result
    if (cinema.get("observed") is not True or cinema.get("active") is not False
            or cinema.get("anim_cutscene") is True
            or not isinstance(cinema.get("playing", []), list)
            or any(not isinstance(item, dict) or item.get("kind") != "dialogue"
                   for item in cinema.get("playing") or [])):
        result["reason"] = "cinema_or_unknown_scene"
        return result
    subtitle = subtitles[0]
    details = subtitle.get("details") or {}
    nodes = details.get("nodes")
    if details.get("truncated") is not False or not isinstance(nodes, list):
        return result
    # WidgetTree parents stop at their tree root, before the owning user
    # widget. Cached children can look visible inside a collapsed owner.
    owners = [n for n in nodes if isinstance(n, dict) and n.get("depth") == 0
              and n.get("class") == "Subtitle00_C" and n.get("path") == subtitle.get("path")]
    if len(owners) != 1 or type(owners[0].get("rendered")) is not bool:
        return result
    if owners[0]["rendered"] is not True:
        result["reason"] = "subtitle_container_hidden"
        return result
    required = {"Img_Arrow": "EndImage", "Txt_Lines": "EndTextBlock", "Txt_Name": "EndTextBlock"}
    leaves = {}
    for name, kind in required.items():
        matches = [n for n in nodes if isinstance(n, dict) and n.get("class") == kind
                   and str(n.get("path", "")).endswith(".WidgetTree." + name)]
        if len(matches) != 1 or matches[0].get("rendered") is not True:
            result["reason"] = "line_or_advance_arrow_hidden"
            return result
        leaves[name] = matches[0]
    for name in ("Txt_Lines", "Txt_Name"):
        value = leaves[name].get("text")
        if not isinstance(value, str) or not value.strip():
            result["reason"] = "line_or_speaker_unknown"
            return result
    # A ventriloquist can speak as a puppet: this is the current visible
    # speaker, not an equality check against the route's NPC display name.
    result.update(fast_forwardable=True, reason="ordinary_line_with_advance_arrow",
                  speaker=leaves["Txt_Name"]["text"], line=leaves["Txt_Lines"]["text"],
                  subtitle_path=subtitle.get("path"))
    return result


def read_dialogue(service: Any) -> dict:
    begin = service._monotonic() if hasattr(service, "_monotonic") else time.monotonic()
    response, base = request_snapshot(service, heights=False, flags=("npcdialogue",),
                                      require_map=False, timeout_seconds=MAX_READ_SECONDS)
    cinema = service.cutscene_state()
    now = service._monotonic() if hasattr(service, "_monotonic") else time.monotonic()
    result = signal(response.get("ui_roots"), base, cinema)
    elapsed = now - begin
    try:
        produced = datetime.fromisoformat(response["timestamp_utc"].replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - produced).total_seconds()
    except (KeyError, TypeError, ValueError):
        age = math.inf
    # The producer timestamp has whole-second precision. A newly matched
    # request can therefore be up to one extra second old by this clock.
    if not 0 <= elapsed <= MAX_READ_SECONDS or not -0.5 <= age <= 1.0 + MAX_READ_SECONDS:
        result.update(fast_forwardable=False, reason="dialogue_read_stale")
    result.update(request_id=response.get("request_id"), source_sequence=response.get("source_sequence"),
                  timestamp_utc=response.get("timestamp_utc"), round_trip_seconds=round(elapsed, 4),
                  input_sent=False, input_authorized=False)
    return result


class DialogueForwarder:
    """Talk arms a conversation checker; verified field return disarms it.

    Nothing runs in the background. An owned initiation action calls this
    checker only during the conversation, bypassing it while a wheel is shown.
    A wheel or a gap between lines does not end the conversation.
    """

    def __init__(self, service: Any, enabled: bool, trace: list, inputs: list):
        self.service, self.trace, self.inputs = service, trace, inputs
        self.available = enabled and getattr(service.telemetry, "path", None) is not None
        self.enabled = False
        self.seen_dialogue = False
        self.opening_deadline = 0.0
        self.previous_sequence = None
        self.clicks = 0
        self.reads = 0
        self.last_summary = None
        self.last_value = None
        self.waiting_for_choice = False

    def talk_started(self) -> None:
        if not self.available or self.clicks >= MAX_CLICKS:
            return
        self.enabled = True
        self.waiting_for_choice = False
        self.seen_dialogue = False
        self.opening_deadline = self.service._monotonic() + OPENING_WAIT_SECONDS
        self.trace.append({"event": "npc_dialogue_checker_started", "reason": "talk_input"})

    def stop(self, reason: str) -> None:
        if self.enabled:
            self.enabled = False
            self.trace.append({"event": "npc_dialogue_checker_stopped", "reason": reason,
                               "clicks": self.clicks, "reads": self.reads})

    def wheel_seen(self) -> None:
        # A prompt can appear before any subtitle. It proves the Talk opened,
        # but never authorizes an advance click or requires another UI read.
        if self.enabled:
            self.seen_dialogue = True
            if not self.waiting_for_choice:
                self.trace.append({"event": "npc_dialogue_checker_suspended", "reason": "choice_wheel"})
            self.waiting_for_choice = True

    def choice_handled(self) -> None:
        # The owning wheel handler has just confirmed a verified answer.
        # Resume fresh subtitle requests for replies and any subsequent wheel.
        if self.enabled and self.waiting_for_choice:
            self.waiting_for_choice = False
            self.trace.append({"event": "npc_dialogue_checker_resumed", "reason": "verified_choice"})

    def read(self) -> dict | None:
        if not self.enabled or self.waiting_for_choice:
            return None
        if not self.seen_dialogue and self.service._monotonic() >= self.opening_deadline:
            self.stop("opening_wait_expired")
            return None
        try:
            self.reads += 1
            value = read_dialogue(self.service)
            sequence = value.get("source_sequence")
            if type(sequence) is not int or (self.previous_sequence is not None and sequence <= self.previous_sequence):
                raise MissionError("npc_dialogue_sample_did_not_advance")
            self.previous_sequence = sequence
        except (MissionError, OSError, ValueError, TypeError) as exc:
            self.stop("read_unavailable")
            self.trace.append({"event": "npc_dialogue_fast_forward_unavailable", "reason": str(exc)})
            return None
        summary = (value["dialogue_present"], value["fast_forwardable"], value["reason"], value.get("line"),
                   tuple(value.get("interactive_ui") or []))
        self.last_value = value
        if summary != self.last_summary:
            self.trace.append({"event": "npc_dialogue_signal", **value})
            self.last_summary = summary
        if value.get("reason") in {"dialogue_unknown", "overlay_unknown", "telemetry_unavailable",
                                   "game_context_blocked", "cinema_or_unknown_scene", "dialogue_read_stale"}:
            self.stop(value["reason"])
        elif value.get("reason") == "conversation_finished" and self.seen_dialogue:
            self.stop("conversation_finished")
        elif value["dialogue_present"]:
            self.seen_dialogue = True
        if any(name in {"TalkMenu_00_C", "TalkMenuCenter_C"} for name in value.get("interactive_ui") or []):
            self.wheel_seen()
        return value

    def present(self) -> bool:
        if self.enabled and self.waiting_for_choice:
            return True
        value = self.read()
        return bool(value and (value.get("dialogue_present") is True or self.waiting_for_choice))

    def advance(self, deadline: float) -> bool:
        service = self.service
        if self.clicks >= MAX_CLICKS:
            self.stop("click_limit")
        if not self.enabled or self.waiting_for_choice or service._monotonic() >= deadline:
            return False
        if not self.seen_dialogue and service._monotonic() >= self.opening_deadline:
            self.stop("opening_wait_expired")
            return False
        service._ensure_game_foreground()
        # Lease rearming and process enumeration are relatively slow. Perform
        # them before the request that will authorize a click, not after it.
        latest = service._assert_action_allowed(allow_queen_blood_dialogue_transition=True)
        if has_field_telemetry(latest):
            self.read()  # Confirm subtitle detachment; no click on the field.
            return False
        if service._queen_blood_prematch_visible(latest):
            self.stop("prematch_visible")
            return False
        service._assert_one_off_visit_unchanged()
        hwnd = service._environment.foreground_window_handle()
        handles = service._environment.game_window_handles()
        if (type(hwnd) is not int or hwnd <= 0
                or not any(window.get("hwnd") == hwnd for window in handles)):
            return False
        value = self.read()
        if not self.enabled or self.waiting_for_choice or not value or value.get("fast_forwardable") is not True:
            return False
        read_finished = service._monotonic()
        armed, _ = service._arm.status() if service._arm else (False, 0.0)
        if (not armed or service._environment.foreground_window_handle() != hwnd
                or service._monotonic() >= deadline or service._monotonic() - read_finished > 0.1):
            return False
        if not service._action_lock.locked():
            raise MissionError("npc_dialogue_requires_owned_input_action")
        try:
            service.input.mouse_click_in_game_window("right", CLICK_MILLISECONDS, hwnd)
        finally:
            service.input.release_all()
        self.clicks += 1
        self.inputs.append({"button": "mouse_right", "milliseconds": CLICK_MILLISECONDS,
                            "reason": "npc_dialogue_advance", "source_sequence": value["source_sequence"],
                            "request_id": value["request_id"], "speaker": value.get("speaker"),
                            "line": value.get("line"), "clock": round(service._wall_clock().timestamp(), 3)})
        # Even the final click gets a new read. Unknown or nonadvancing state
        # disables further clicks; no cached positive signal is reused.
        self.read()
        return True
