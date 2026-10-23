"""Per-save Queen's Blood records: the card inventory and opponent history.

Runtime profiles are selected by queen_blood_runs from a verified loaded-save
receipt and the declared driver/model (efforts share a run). Records live in:

    <runtime dir>/queen-blood/profiles/<profile id>/inventory.json
                                                    /opponents.json

The legacy detect_profile_id helper below is retained for offline migration.
Its mtime/alias/environment heuristic is never used by the gameplay service.
Existing inventories are preserved and never copied into a model's profile:
the loaded save must be scanned. Windows Documents redirection is honoured.

The inventory holds only card ids and owned counts. Card details (cost,
power, influence, abilities, rarity) are always looked up in the generated
catalogue, so a catalogue fix never leaves stale copies in inventories.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .queen_blood_catalogue import catalogue_card, catalogue_copies

PROFILE_ENV = "FF7R_QUEEN_BLOOD_PROFILE"
SAVE_SUBPATH = Path("My Games") / "FINAL FANTASY VII REBIRTH" / "Steam"
_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def documents_folder() -> Path:
    """The Windows Documents known folder (honours folder redirection)."""
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class GUID(ctypes.Structure):
                _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                            ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

            # FOLDERID_Documents {FDD39AD0-238F-46AF-ADB4-6C85480369C7}
            folder_id = GUID(0xFDD39AD0, 0x238F, 0x46AF,
                             (ctypes.c_ubyte * 8)(0xAD, 0xB4, 0x6C, 0x85, 0x48, 0x03, 0x69, 0xC7))
            path_pointer = ctypes.c_wchar_p()
            shell32 = ctypes.windll.shell32
            if shell32.SHGetKnownFolderPath(ctypes.byref(folder_id), 0, None, ctypes.byref(path_pointer)) == 0:
                try:
                    return Path(path_pointer.value)
                finally:
                    ctypes.windll.ole32.CoTaskMemFree(path_pointer)
        except (OSError, AttributeError):
            pass
    return Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Documents"


def detect_profile_id(save_root: Path | None = None, aliases_path: Path | None = None) -> str | None:
    """The profile for the most recently written manual save slot, or None.

    ``<steam id>-slot<NNN>``, unless ``slot-aliases.json`` maps that slot
    to a profile name or ``FF7R_QUEEN_BLOOD_PROFILE`` overrides it.

    Slot 000 is the autosave, which the game writes for whichever
    playthrough is loaded (live 2026-09-26 a fast travel autosaved the
    campaign over a slot aliased to another playthrough), so it only
    counts when no manual slot exists.
    """
    override = os.environ.get(PROFILE_ENV)
    if override:
        return _SAFE.sub("_", override)
    root = save_root if save_root is not None else documents_folder() / SAVE_SUBPATH
    try:
        saves = [path for path in root.glob("*/ff7rebirth[0-9][0-9][0-9].sav") if path.is_file()]
    except OSError:
        return None
    if not saves:
        return None
    manual = [path for path in saves if path.stem != "ff7rebirth000"]
    latest = max(manual or saves, key=lambda path: path.stat().st_mtime)
    slot_id = _SAFE.sub("_", f"{latest.parent.name}-{latest.stem.replace('ff7rebirth', 'slot')}")
    if aliases_path is not None:
        alias = _read(aliases_path).get(slot_id)
        if isinstance(alias, str) and alias.strip():
            return _SAFE.sub("_", alias.strip())
    return slot_id


def _read(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path: Path, data: dict[str, Any]) -> None:
    """Publish one complete snapshot without sharing a temporary file.

    Unique sibling temporaries avoid collisions between concurrent status calls.
    Windows readers can briefly deny replacement; retry only those sharing/access
    errors, with a 70 ms total wait bound. Other failures remain visible.
    This does not serialize callers' read/modify/write transactions.
    """
    payload = json.dumps(data, indent=2, sort_keys=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
        for attempt in range(4):
            try:
                os.replace(temporary, path)
                break
            except OSError as exc:
                if getattr(exc, "winerror", None) not in {5, 32, 33} or attempt == 3:
                    raise
                time.sleep(0.01 * (2 ** attempt))
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class QueenBloodProfile:
    def __init__(self, root: Path, profile_id: str) -> None:
        self.profile_id = _SAFE.sub("_", profile_id)
        self.directory = Path(root) / "profiles" / self.profile_id
        self.inventory_path = self.directory / "inventory.json"
        self.opponents_path = self.directory / "opponents.json"

    # -- inventory -------------------------------------------------------

    def inventory(self) -> dict[str, Any]:
        data = _read(self.inventory_path)
        data.setdefault("schema_version", 1)
        data.setdefault("profile_id", self.profile_id)
        data.setdefault("cards", {})
        return data

    def record_collection(self, cards: list[str], *, source: str) -> dict[str, Any]:
        """Replace the owned-card list with a full collection scan.

        ``cards`` are the distinct card ids seen in the owned-card grid. Owned
        counts follow the catalogue's copies (2 standard, 1 legendary): the
        grid lists a card once however many copies are owned.
        """
        data = self.inventory()
        data["revision"] = int(data.get("revision", 0)) + 1
        owned = {}
        for card_id in dict.fromkeys(cards):
            copies = catalogue_copies(card_id)
            owned[card_id] = {
                "owned": copies or 1,
                "count_source": "catalogue_copies" if copies else "assumed_single_copy",
            }
        data["cards"] = owned
        data["scanned_at"] = _now()
        data["scan_source"] = source
        _write(self.inventory_path, data)
        return data

    def add_card(self, card_id: str, *, source: str) -> dict[str, Any]:
        """A newly obtained card (e.g. a match reward)."""
        data = self.inventory()
        data["revision"] = int(data.get("revision", 0)) + 1
        copies = catalogue_copies(card_id)
        data["cards"][card_id] = {
            "owned": copies or 1,
            "count_source": "catalogue_copies" if copies else "assumed_single_copy",
            "obtained_from": source,
            "obtained_at": _now(),
        }
        _write(self.inventory_path, data)
        return data

    def known_deck(self) -> list[str] | None:
        """The deck as last verified on the deck screen, if any."""
        cards = (self.inventory().get("known_deck") or {}).get("cards")
        return [str(card) for card in cards] if isinstance(cards, list) and len(cards) == 15 else None

    def record_deck(self, cards: list[str], *, source: str) -> None:
        """Remember the deck just verified, so the next edit need not read it."""
        data = self.inventory()
        data["revision"] = int(data.get("revision", 0)) + 1
        data["known_deck"] = {"cards": list(cards), "verified_at": _now(), "source": source}
        _write(self.inventory_path, data)

    def owned_cards(self) -> list[dict[str, Any]]:
        """Owned cards joined to catalogue details (details are never stored)."""
        result = []
        for card_id, entry in sorted(self.inventory()["cards"].items()):
            card = catalogue_card(card_id)
            result.append({
                "card_id": card_id,
                "owned": entry.get("owned"),
                "name": card.name if card else None,
                "cost": card.cost if card else None,
                "power": card.power if card else None,
                "effect_type_name": card.effect_type_name if card else None,
            })
        return result

    # -- opponents -------------------------------------------------------

    def opponents(self) -> dict[str, Any]:
        data = _read(self.opponents_path)
        data.setdefault("schema_version", 1)
        data.setdefault("profile_id", self.profile_id)
        data.setdefault("opponents", {})
        return data

    def opponent(self, key: str) -> dict[str, Any]:
        return self.opponents()["opponents"].get(key.strip().lower(), {})

    def update_opponent(
        self,
        key: str,
        *,
        name_id: str | None = None,
        card_ids: list[str] | None = None,
        match: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Merge what was seen of an opponent.

        ``name_id`` is the opponent's CardGameOpponent name key from the deck
        screen (e.g. ``$card_opponent_NIBLT_03``); rows ending ``_Hard`` in
        that table are the harder (NG+/rematch) variants, recorded as a
        separate ``variant`` once the key is known.
        """
        data = self.opponents()
        record = data["opponents"].setdefault(key.strip().lower(), {"card_ids": [], "matches": []})
        if name_id:
            record["name_id"] = name_id
        if card_ids:
            record["card_ids"] = list(dict.fromkeys(list(record.get("card_ids", [])) + list(card_ids)))
        if match:
            record.setdefault("matches", []).append({"at": _now(), **match})
            record["matches"] = record["matches"][-50:]
            wins = sum(1 for item in record["matches"] if item.get("won") is True)
            record["record"] = {"played": len(record["matches"]), "won": wins}
        _write(self.opponents_path, data)
        return record

    def unsaved_live_wins(self) -> list[str]:
        """Opponent keys with a live-stream win not yet written to the save slot."""
        return sorted(key for key, record in self.opponents()["opponents"].items()
                      if any(_unsaved_live_win(item) for item in record.get("matches") or []))

    def mark_live_wins_saved(self, *, saved_at: str, receipt: str | None = None) -> int:
        """A verified save to the run's slot now holds every earlier live win.

        Live wins only count towards the campaign once saved (owner,
        2026-10-08): the game autosaves to the shared autosave slot, never the
        run's own slot, so an unsaved win is lost on the next load.
        """
        data = self.opponents()
        marked = 0
        for record in data["opponents"].values():
            for item in record.get("matches") or []:
                if _unsaved_live_win(item) and str(item.get("at") or "") <= saved_at:
                    item["saved_at"] = saved_at
                    if receipt:
                        item["save_receipt"] = receipt
                    marked += 1
        if marked:
            _write(self.opponents_path, data)
        return marked


def _unsaved_live_win(item: Any) -> bool:
    return (isinstance(item, dict) and item.get("mode") == "live" and item.get("won") is True
            and not item.get("saved_at"))
