"""Tutorial tip panels: read from the screen texts and closed automatically.

Live 2026-10-13 (the prologue, Nibelheim after the Disgorgon fight): a tips
panel ("Main Menu", then "Movement Options") opened over the field and held
the game paused with ``in_menu`` clear. While it showed, the screen texts
listed ``BP_EndGameInstance.BattleTips.WidgetTree.`` Txt_Title
(``$tutorial_Fld_01_0070``), Txt_Main (``..._00``), Txt_Btn
(``<button=pause-menu>/<button=cancel> Close``) and Txt_Btn2. One Cancel
closed a tip and the next queued tip replaced it (``$tutorial_Fld_01_0020``);
after the last one the paths were gone and the game resumed. A
``BattleTutorial.WidgetTree.Txt_Title`` was listed before and after: a loaded
but hidden full-screen tutorial, never read as an open tip. The full-screen,
paged variant has not been seen open, so its controls are not guessed: it is
left alone (follow-up once one is observed).

Gating (the cutscene auto-skip is the model): nothing is polled except the
base state file's pause flags (1.4 KB). Only when the game has stayed paused,
outside any menu, for a moment does one text snapshot confirm a tips panel,
and a pause that shows none is looked at once more and then left alone until
the pause ends. A close presses Cancel once per confirmed tip and waits for
its title to change; a tip that does not change stops the run.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import timezone
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any

from .conversation_state import dialogue_active
from .cutscene import read_cutscene_state, state_path
from .public_stream_events import rehearsal_is_live, stream_is_live
from .telemetry import _queen_blood_screen_shown

# The tips widget's texts, listed only while it is shown (live 2026-10-06 over
# the Queen's Blood deck screens, 2026-10-13 over the field).
TIPS_PANEL_PATH = ".BattleTips.WidgetTree."
TIPS_TITLE_SUFFIX = TIPS_PANEL_PATH + "Txt_Title"
TIPS_BODY_SUFFIX = TIPS_PANEL_PATH + "Txt_Main"
TIPS_BUTTONS_SUFFIX = TIPS_PANEL_PATH + "Txt_Btn"
# Titles are text keys, not yet localised; the Queen's Blood panels have other
# titles and are left to the deck flow that owns them.
# Battle tutorials (live 2026-10-20, Cloud's first fight on Mt. Nibel) use
# ``$menu_Tutorial_End2Btl_...`` keys in the same panel, closed by the same
# Cancel. Some show over a running battle; "Targeting" (2026-10-20, second
# fight) paused it, outside any menu, and is closed by the watcher. In combat
# only these battle tips are closed.
TIPS_KEY_PREFIX = ("$tutorial_", "$menu_Tutorial_")
TIPS_CANCEL_GLYPH = "<button=cancel>"
# On a battle tutorial the keyboard Cancel is Escape, which closes the tip and
# opens the battle pause menu with Resume selected (live 2026-10-20, twice).
BATTLE_TIP_PREFIX = "$menu_Tutorial_"
PAUSE_TITLE_SUFFIX = ".Pause_00.WidgetTree.Txt_Pause"
PAUSE_TITLE_KEY = "$menu_pause_0000_0000"
PAUSE_RESUME_WAIT_SECONDS = 2.0

TIPS_AUTOCLOSE_MODES = ("off", "stream", "always")
# The base exporter rewrites the state file many times a second.
STATE_STALE_SECONDS = 2.5
# One Cancel closed a field tip live (2026-10-13, a 120 ms tap).
TIPS_CLOSE_MS = 120
TIPS_MAX_PER_CALL = 8
TIPS_WATCH_SECONDS = 0.25
# The pause must hold this long before the first look: a pause that ends
# within it (a scene change, a menu opening) costs no snapshot at all.
TIPS_PAUSE_DEBOUNCE_SECONDS = 0.5
# A pause that showed no tip (or could not be looked at) is looked at once
# more this long after, in case the panel's text appears a moment later, then
# left alone until the pause ends.
TIPS_RECHECK_SECONDS = 2.0
TIPS_EPISODE_LOOKS = 2
# After a failed close (a tip that did not change, an unreadable snapshot)
# nothing is tried for this long, and not at all after TIPS_EPISODE_FAILURES in
# one pause. A refused one (no control session, a latched stop, an unarmed or
# unfocused game) costs no snapshot, so it waits and is tried again for as long
# as the pause lasts: the wait doubles up to the maximum.
TIPS_BACKOFF_SECONDS = 30.0
TIPS_EPISODE_FAILURES = 3
TIPS_REFUSED_BACKOFF_SECONDS = 15.0
TIPS_REFUSED_BACKOFF_MAX_SECONDS = 120.0
# Waiting for a tip's title to change after Cancel, by text snapshots (each
# 0.2-0.4 s on the game thread).
TIPS_CLOSE_WAIT_SECONDS = 3.0
TIPS_POLL_SECONDS = 0.3
# A tip ignores Cancel while it opens: live 2026-10-13 the first press on
# "Main Menu" (half a second into the pause) and on "Movement Options" (as it
# replaced the closed tip) did nothing, and each later press closed its tip.
# So a tip is left this long, confirmed still shown, then pressed; one still
# shown after its press gets one more before the attempt counts as failed.
TIPS_OPEN_SETTLE_SECONDS = 1.0
TIPS_PRESSES_PER_TIP = 2
TIPS_LOG = 20
TIPS_ERROR_BACKOFF_SECONDS = 2.0
TIPS_ERROR_BACKOFF_MAX_SECONDS = 30.0


@dataclass(frozen=True)
class TipsPanel:
    """A tutorial tip panel on screen: its title key, body key and whether its
    button hint says Cancel closes it."""

    key: str
    body: str | None
    cancel_closes: bool


def tips_shown(texts: list[tuple[str, str]]) -> bool:
    """Any text of the tips widget is listed (it is listed only while shown)."""
    return any(TIPS_PANEL_PATH in path for path, _ in texts)


def read_tips_panel(texts: list[tuple[str, str]]) -> TipsPanel | None:
    """The tutorial tip panel shown, or None.

    Only a ``BattleTips`` title that is a tutorial key counts; the
    ``BattleTutorial`` widget is not a tip and is never looked at.
    """
    title = body = buttons = None
    for path, text in texts:
        if path.endswith(TIPS_TITLE_SUFFIX):
            title = text
        elif path.endswith(TIPS_BODY_SUFFIX):
            body = text
        elif path.endswith(TIPS_BUTTONS_SUFFIX):
            buttons = text
    if title is None or not title.startswith(TIPS_KEY_PREFIX):
        return None
    return TipsPanel(key=title, body=body, cancel_closes=buttons is not None and TIPS_CANCEL_GLYPH in buttons)


def read_game_flags(path: Path, now: float | None = None) -> dict[str, Any]:
    """The pause flags from the base state file, without the sidecars that
    ``telemetry.observe`` merges; ``observed`` is False when the file is
    missing, unreadable or stale (nothing is known)."""
    now = time.time() if now is None else now
    try:
        modified = path.stat().st_mtime
        game = json.loads(path.read_text(encoding="utf-8")).get("game")
    except (OSError, ValueError, AttributeError):
        return {"observed": False}
    if not isinstance(game, dict) or now - modified > STATE_STALE_SECONDS:
        return {"observed": False}
    return {"observed": True, "running": bool(game.get("running")), "paused": bool(game.get("paused")),
            "in_menu": bool(game.get("in_menu")), "in_combat": bool(game.get("in_combat"))}


@dataclass
class _Episode:
    """One continuous pause: looked at a bounded number of times."""

    since: float
    looks: int = 0
    failures: int = 0
    refusals: int = 0
    next_at: float = 0.0
    done: bool = False


class TipsAutoClose:
    """Closes tutorial tip panels for an ``AgentService``: on demand
    (``close``) and from a watcher thread (``set_mode``)."""

    def __init__(self, service: Any) -> None:
        self.service = service
        self.mode = "off"
        self.snapshots = 0
        self.closed_total = 0
        self._stop = Event()
        self._thread: Thread | None = None
        self._lock = Lock()
        self._log: list[dict[str, Any]] = []
        self._episode: _Episode | None = None
        self._last_look: dict[str, Any] | None = None

    # -- modes and status ---------------------------------------------------

    def set_mode(self, mode: str) -> dict[str, Any]:
        """"stream" (while OBS streams or a rehearsal records), "always" or "off"."""
        if mode not in TIPS_AUTOCLOSE_MODES:
            from .service import SafetyError
            raise SafetyError("mode must be one of " + ", ".join(TIPS_AUTOCLOSE_MODES))
        self.mode = mode
        if mode != "off":
            self._start()
        return self.status()

    def status(self) -> dict[str, Any]:
        thread = self._thread
        with self._lock:
            recent = [dict(entry) for entry in self._log]
        episode = self._episode
        wait = None
        if episode is not None and episode.next_at > self.service._monotonic():
            wait = round(episode.next_at - self.service._monotonic(), 1)
        return {"mode": self.mode, "watching": thread is not None and thread.is_alive(),
                "applies_now": self.applies(), "snapshots": self.snapshots,
                "closed_total": self.closed_total, "last_look": self._last_look,
                "backoff_seconds": wait, "recent": recent}

    def applies(self) -> bool:
        if self.mode == "always":
            return True
        directory = self.service._public_stream_event_dir
        if self.mode != "stream" or directory is None:
            return False
        return stream_is_live(directory.parent) or rehearsal_is_live(directory.parent)

    def _start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = Thread(target=self._run, name="tips-autoclose", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # -- the watcher --------------------------------------------------------

    def _run(self) -> None:
        delay = TIPS_WATCH_SECONDS
        while not self._stop.wait(delay):
            try:
                self.watch_once()
                delay = TIPS_WATCH_SECONDS
            except Exception as exc:  # the watcher must outlive one bad read, without spinning on it
                self._note({"ok": False, "reason": "watch_error", "detail": f"{type(exc).__name__}: {exc}"[:200]},
                           "watch")
                delay = min(TIPS_ERROR_BACKOFF_MAX_SECONDS,
                            TIPS_ERROR_BACKOFF_SECONDS if delay <= TIPS_WATCH_SECONDS else delay * 2)

    def _flags(self) -> dict[str, Any]:
        path = getattr(self.service.telemetry, "path", None)
        return read_game_flags(Path(path)) if path is not None else {"observed": False}

    def watch_once(self) -> dict[str, Any] | None:
        """One watcher step. Returns what a close did, or None when nothing was tried.

        The only per-step cost is the state file's pause flags. A snapshot is
        taken once the game has stayed paused outside any menu for
        TIPS_PAUSE_DEBOUNCE_SECONDS, at most TIPS_EPISODE_LOOKS times in one
        pause, and never while another action holds the controls.
        """
        if self.mode == "off":
            return None
        service = self.service
        flags = self._flags()
        if not (flags.get("observed") and flags["running"] and flags["paused"]
                and not flags["in_menu"]):
            self._episode = None
            return None
        now = service._monotonic()
        episode = self._episode
        if episode is None:
            episode = self._episode = _Episode(since=now)
        if episode.done or now - episode.since < TIPS_PAUSE_DEBOUNCE_SECONDS or now < episode.next_at:
            return None
        if not self.applies():
            episode.next_at = now + TIPS_RECHECK_SECONDS
            return None
        if service._action_lock.locked():
            return None  # another action owns the controls: look again next step
        blocker = self._blocker()
        if blocker == "not_paused":
            self._episode = None
            return None
        if blocker is not None:
            self._looked(episode, now, f"blocked: {blocker}")
            return None
        if not service._action_lock.acquire(blocking=False):
            return None
        try:
            result = self._close_open_tips(TIPS_MAX_PER_CALL, battle_only=bool(flags["in_combat"]))
        finally:
            service._action_lock.release()
        reason = result.get("reason")
        now = service._monotonic()  # a backoff counts from the end of the attempt, which waited on the game
        if reason in {"no_tips", "texts_unreadable"}:
            self._looked(episode, now, reason + (f": {result['detail']}" if result.get("detail") else ""))
            return None
        if result.get("ok") or reason in {"tips_unrecognised", "no_cancel_hint"}:
            episode.done = True  # closed, or nothing further to press
        elif reason == "input_refused":
            episode.refusals += 1
            episode.next_at = now + min(TIPS_REFUSED_BACKOFF_MAX_SECONDS,
                                        TIPS_REFUSED_BACKOFF_SECONDS * 2 ** (episode.refusals - 1))
        else:
            episode.failures += 1
            episode.next_at = now + TIPS_BACKOFF_SECONDS
            episode.done = episode.failures >= TIPS_EPISODE_FAILURES
        self._last_look = {"at": self._now(), "outcome": "closed" if result.get("ok") else reason}
        self._note(result, "watch")
        return result

    def _looked(self, episode: _Episode, now: float, outcome: str) -> None:
        episode.looks += 1
        episode.next_at = now + TIPS_RECHECK_SECONDS
        episode.done = episode.done or episode.looks >= TIPS_EPISODE_LOOKS
        self._last_look = {"at": self._now(), "outcome": outcome}

    def _blocker(self) -> str | None:
        """Why a paused game is not a tips panel to look for, or None.

        Read with one full observation, once per look: not while a Queen's
        Blood match or screen, a conversation, a load or a cinema owns the
        screen. A paused fight is looked into for battle tips only.
        """
        service = self.service
        try:
            state = service.telemetry.observe()
        except Exception:
            return "telemetry_unavailable"
        if not state.running:
            return "game_not_running"
        if state.age_seconds() > service.max_telemetry_age:
            return "telemetry_stale"
        if not state.paused or state.in_menu:
            return "not_paused"
        if getattr(state, "loading", False):
            return "loading"
        if dialogue_active(state):
            return "dialogue"
        payload = getattr(state, "queen_blood", None)
        if getattr(service, "_queen_blood_match", None) is not None or (
                isinstance(payload, dict) and _queen_blood_screen_shown(payload)):
            return "queen_blood"
        if read_cutscene_state(state_path(service)).get("active"):
            return "cutscene"
        return None

    # -- closing ------------------------------------------------------------

    def close(self, max_tips: int = TIPS_MAX_PER_CALL) -> dict[str, Any]:
        """Close the tutorial tip panel shown, and each queued behind it, with
        Cancel: one press per tip, each confirmed by its title changing."""
        from .service import SafetyError
        try:
            max_tips = int(max_tips)
        except (TypeError, ValueError):
            raise SafetyError(f"max_tips must be between 1 and {TIPS_MAX_PER_CALL}") from None
        if not 1 <= max_tips <= TIPS_MAX_PER_CALL:
            raise SafetyError(f"max_tips must be between 1 and {TIPS_MAX_PER_CALL}")
        lock = self.service._action_lock
        if not lock.acquire(blocking=False):
            return {"ok": False, "reason": "busy", "detail": "another input action is executing",
                    "closed": [], "input_sent": False}
        try:
            result = self._close_open_tips(max_tips)
        finally:
            lock.release()
        self._note(result, "manual")
        return result

    def _input_refusal(self) -> str | None:
        """Why input would be refused, checked before spending a snapshot on
        it: no open control session with a latched stop, or not armed."""
        service = self.service
        if not getattr(service.input, "is_real", False):
            return None
        lease = service._session_lease
        if lease is not None:
            try:
                status = lease.status()
            except Exception:
                status = None
            if status and status.get("active"):
                return None  # the live session re-arms each bounded action
        arm = service._arm
        armed, _, stopped = arm.snapshot() if arm is not None else (False, 0.0, True)
        if stopped:
            return service._latched_stop_message()
        return None if armed else "real input is not manually armed"

    def _look(self) -> tuple[bool, TipsPanel | None]:
        """One text snapshot: whether any tips text is shown, and the tutorial panel."""
        self.snapshots += 1
        texts = self.service._deck_screen_texts()
        return tips_shown(texts), read_tips_panel(texts)

    def _close_open_tips(self, max_tips: int, battle_only: bool = False) -> dict[str, Any]:
        """The close run, with the action lock held. ``battle_only`` (in
        combat) leaves any panel but a battle tip alone."""
        refusal = self._input_refusal()
        if refusal is not None:
            return {"ok": False, "reason": "input_refused", "detail": refusal[:200],
                    "closed": [], "input_sent": False}
        try:
            shown, panel = self._look()
        except Exception as exc:
            return {"ok": False, "reason": "texts_unreadable", "detail": f"{type(exc).__name__}: {exc}"[:200],
                    "closed": [], "input_sent": False}
        if panel is None:
            if shown:
                return {"ok": False, "reason": "tips_unrecognised", "closed": [], "input_sent": False,
                        "detail": "a tips panel is shown but its title is not a tutorial key"}
            return {"ok": False, "reason": "no_tips", "closed": [], "input_sent": False}
        if battle_only and not panel.key.startswith(BATTLE_TIP_PREFIX):
            return {"ok": False, "reason": "tips_unrecognised", "closed": [], "input_sent": False,
                    "detail": f"{panel.key} is not a battle tip and the game is in combat"}
        service = self.service
        try:
            return self._close_panels(panel, max_tips)
        finally:
            service.input.release_all()

    def _close_panels(self, panel: TipsPanel, max_tips: int) -> dict[str, Any]:
        from .service import SafetyError
        service = self.service
        closed: list[str] = []
        presses = 0

        def outcome(ok: bool, reason: str | None = None, detail: str | None = None) -> dict[str, Any]:
            result: dict[str, Any] = {"ok": ok, "closed": closed, "presses": presses, "input_sent": presses > 0}
            if reason is not None:
                result["reason"] = reason
            if detail is not None:
                result["detail"] = detail
            return result

        while True:
            if not panel.cancel_closes:
                return outcome(False, "no_cancel_hint", f"{panel.key} does not say Cancel closes it")
            if len(closed) >= max_tips:
                return outcome(False, "tip_limit", f"{len(closed)} tips closed and {panel.key} is still shown")
            # Let the tip finish opening, and never press once it has gone:
            # Cancel in the field opens the pause menu.
            service._sleeper(TIPS_OPEN_SETTLE_SECONDS)
            try:
                shown, current = self._look()
            except Exception as exc:
                return outcome(False, "texts_unreadable", f"{type(exc).__name__}: {exc}"[:200])
            if current is None:
                if shown:
                    return outcome(False, "tips_unrecognised",
                                   "a tips panel is shown but its title is not a tutorial key")
                return outcome(True) if closed else outcome(False, "no_tips")
            if current.key != panel.key:
                panel = current
                continue
            for _ in range(TIPS_PRESSES_PER_TIP):
                try:
                    if panel.key.startswith(BATTLE_TIP_PREFIX):
                        service._tap_battle_tip_button("cancel", TIPS_CLOSE_MS)
                    else:
                        service.tap("cancel", TIPS_CLOSE_MS)
                except SafetyError as exc:
                    return outcome(False, "input_refused", str(exc)[:200])
                presses += 1
                try:
                    shown, after = self._wait_for_change(panel.key)
                except Exception as exc:
                    return outcome(False, "texts_unreadable", f"{type(exc).__name__}: {exc}"[:200])
                if not ((after is not None and after.key == panel.key) or (shown and after is None)):
                    break
            else:
                return outcome(False, "tip_not_closed", f"{panel.key} is still shown after Cancel")
            closed.append(panel.key)
            self.closed_total += 1
            if panel.key.startswith(BATTLE_TIP_PREFIX):
                resumed = self._resume_battle_pause()
                if resumed is False:
                    return outcome(False, "battle_pause_not_resumed",
                                   f"{panel.key} closed but the battle pause menu stayed open")
                if resumed:
                    presses += 1
                    shown, after = self._look()
            if after is None:
                return outcome(True)
            panel = after

    def _resume_battle_pause(self) -> bool | None:
        """Resume the battle pause menu our own Escape opened: None when it did
        not open, True once it closed after Confirm, False if it stayed."""
        from .service import SafetyError
        service = self.service

        def pause_shown() -> bool:
            return any(path.endswith(PAUSE_TITLE_SUFFIX) and text == PAUSE_TITLE_KEY
                       for path, text in service._deck_screen_texts())

        service._sleeper(TIPS_POLL_SECONDS)
        if not pause_shown():
            return None
        try:
            service._tap_battle_tip_button("confirm", TIPS_CLOSE_MS)
        except SafetyError:
            return False
        deadline = service._monotonic() + PAUSE_RESUME_WAIT_SECONDS
        while service._monotonic() < deadline:
            service._sleeper(TIPS_POLL_SECONDS)
            if not pause_shown():
                return True
        return False

    def _wait_for_change(self, key: str) -> tuple[bool, TipsPanel | None]:
        """Snapshot until the shown tip is not ``key``: (shown, next tip).

        Returns the unchanged tip, or a shown but unrecognised panel, when the
        wait runs out.
        """
        service = self.service
        deadline = service._monotonic() + TIPS_CLOSE_WAIT_SECONDS
        while True:
            service._sleeper(TIPS_POLL_SECONDS)
            shown, panel = self._look()
            if not shown or (panel is not None and panel.key != key):
                return shown, panel
            if service._monotonic() >= deadline:
                return shown, panel

    # -- the record ---------------------------------------------------------

    def _now(self) -> str:
        return self.service._wall_clock().astimezone(timezone.utc).isoformat()

    def _note(self, result: dict[str, Any], source: str) -> None:
        """Record a close for ``get_tips_autoclose_status``, and an applied one as an action.

        A failure the same as the one before is counted, not logged again.
        """
        entry = {"at": self._now(), "source": source, "ok": bool(result.get("ok"))}
        for key in ("reason", "closed", "detail"):
            if result.get(key) is not None:
                entry[key] = result[key]
        with self._lock:
            last = self._log[-1] if self._log else None
            if (not entry["ok"] and last is not None and not last["ok"]
                    and (last.get("reason"), last.get("detail")) == (entry.get("reason"), entry.get("detail"))):
                last["repeats"] = last.get("repeats", 1) + 1
                last["last_at"] = entry["at"]
            else:
                self._log = (self._log + [entry])[-TIPS_LOG:]
        if result.get("ok") and result.get("closed"):
            self.service._record_action(action="close_tips", **{k: v for k, v in result.items() if k != "ok"}, ok=True)
