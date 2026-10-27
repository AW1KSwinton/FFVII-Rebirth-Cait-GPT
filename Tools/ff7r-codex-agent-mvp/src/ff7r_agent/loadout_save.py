"""Read-only snapshots of the game's resident save data, to prove a change.

A loadout or Combat Settings change made through the menus is verified by
diffing this region before and after its one Confirm. Live 2026-10-24
(verified slot 13, supported build), each change wrote exactly its own
field and nothing else but the play-time clock:

* party member records at +0x20 + (mode * 15 + member) * 256 hold the
  equipped weapon copy (+0x9C), armour (+0xA0), Summon Materia (+0xA4),
  accessory (+0xA8), and the armour and weapon Materia sockets (eight int32
  copy IDs each at +0x7C and +0xC4; -1 is empty). Cloud's Buster Sword ->
  Sleek Saber changed only +0x9C (300001 -> 300009);
* Materia copies at +0xBFA58 + (id - 600000) * 32 (materia_memory) keep
  their level in the low byte of +0x10 and their owner's member index in
  the next byte (0x10 when unequipped): putting the unequipped HP Up copy
  600025 in Barret's socket changed it 0x1002 -> 0x0102;
* weapon copies at +0xB3A40 + (id - 300000) * 0x60 hold their copy ID at
  +0x18 and eight weapon-skill slots at +0x28 (hashed skill IDs, -1 empty).
  Max HP +200 -> Max MP +3 in Cloud's first slot changed only that slot;
* Combat Settings shortcuts sit in a per-member table around +0x108C0
  (Cloud's empty fourth ground shortcut -> Steal changed one int32 there);
* the battle party is two 15-byte lists of member indices, frontline at
  +0xE0745 and backline at +0xE0754, 0x10 for an empty place, after a byte
  per member at +0xE0736 (1 frontline, 5 backline); swapping Red XIII for
  Tifa changed 00 01 04 -> 00 01 02 and 02 03 05 -> 04 03 05;
* the play-time clock at +0x3C20 always ticks and is ignored.

Member indices are the party-name indices (0 Cloud, 1 Barret, 2 Tifa,
3 Aerith, 4 Red XIII, 5 Yuffie, 6 Cait Sith). The record set in use (the
"mode") is chosen the way the game's own accessor does. No input, writes,
native calls or object scans; reads are bounded to the region.
"""
from __future__ import annotations

import struct
import time
from dataclasses import dataclass
from typing import Any

from .materia_memory import MAX_COPIES, RECORD_OFFSET, SAVE_KIND_RVA, SAVE_READY_RVA, SAVE_RVA, SIGNATURES
from .queen_blood_memory import ReadOnlyProcess

REGION_SIZE = 0x100000
CHUNK = 0x2000
READ_SECONDS = 5.0
CLOCK_OFFSET = 0x3C20
MEMBER_RECORDS = 0x20
MEMBER_SIZE = 256
MEMBERS = 7
MODE_OFFSET = 0x3D99
MODE_FLAG_KEY_RVA = 0x6FAF1FC
MODE_FLAGS = 0x448AC
WEAPON_RECORDS = 0xB3A40
WEAPON_SIZE = 0x60
WEAPON_BASE_ID = 300000
WEAPON_COPIES = 256
SHORTCUT_TABLE = (0x10000, 0x12000)
PARTY_TABLE = (0xE0730, 0xE0770)
FRONTLINE = 0xE0745
BACKLINE = 0xE0754
PARTY_SLOTS = 15
PARTY_EMPTY = 0x10
MAX_HP = 0x14
MATERIA_BASE_ID = 600000
MATERIA_SIZE = 32
MATERIA_STATE = 0x10
NO_OWNER = 0x10
MEMBER_FIELDS = {0x9C: "weapon", 0xA0: "armor", 0xA4: "summon_materia", 0xA8: "accessory"}
ARMOR_SOCKETS = 0x7C
WEAPON_SOCKETS = 0xC4
SOCKETS = 8
SKILL_SLOTS_OFFSET = 0x28
SKILL_SLOTS = 8
MEMBER_NAMES = ("cloud", "barret", "tifa", "aerith", "red_xiii", "yuffie", "cait_sith")


@dataclass(frozen=True)
class SaveSnapshot:
    region: bytes
    mode_flag_key: int
    process_token: str
    taken_at: float

    def int32(self, offset: int) -> int:
        return struct.unpack_from("<i", self.region, offset)[0]

    @property
    def mode(self) -> int:
        """The record set the game reads, as its accessor chooses it."""
        mode = self.region[MODE_OFFSET]
        key = self.mode_flag_key
        word = key >> 5 if key else 0
        if mode == 1 and word < 512:
            flags = struct.unpack_from("<I", self.region, MODE_FLAGS + word * 4)[0]
            if flags & (1 << (key & 31)):
                return 0
        return mode

    def member_offset(self, member: int) -> int:
        if not 0 <= member < MEMBERS or self.mode >= 4:
            raise ValueError("loadout_member_record_unavailable")
        return MEMBER_RECORDS + (self.mode * 15 + member) * MEMBER_SIZE

    def member(self, member: int) -> dict[str, Any]:
        base = self.member_offset(member)
        record = {name: self.int32(base + offset) for offset, name in MEMBER_FIELDS.items()}
        record["armor_materia"] = [self.int32(base + ARMOR_SOCKETS + 4 * i) for i in range(SOCKETS)]
        record["weapon_materia"] = [self.int32(base + WEAPON_SOCKETS + 4 * i) for i in range(SOCKETS)]
        return record

    def materia_owner(self, copy_id: int) -> int | None:
        """The member index holding Materia copy ``copy_id``, None if unequipped."""
        index = copy_id - MATERIA_BASE_ID
        if not 0 <= index < MAX_COPIES or self.int32(RECORD_OFFSET + index * MATERIA_SIZE) != copy_id:
            raise ValueError("loadout_materia_record_mismatch")
        owner = self.region[RECORD_OFFSET + index * MATERIA_SIZE + MATERIA_STATE + 1]
        return None if owner == NO_OWNER else owner

    def max_hp(self, member: int) -> int:
        return self.int32(self.member_offset(member) + MAX_HP)

    def party(self) -> dict[str, list[int]]:
        """The battle party's member indices, frontline and backline."""
        return {line: [b for b in self.region[start:start + PARTY_SLOTS] if b != PARTY_EMPTY]
                for line, start in (("frontline", FRONTLINE), ("backline", BACKLINE))}

    def weapon_offset(self, copy_id: int) -> int:
        index = copy_id - WEAPON_BASE_ID
        if not 0 <= index < WEAPON_COPIES:
            raise ValueError("loadout_weapon_copy_invalid")
        offset = WEAPON_RECORDS + index * WEAPON_SIZE
        if self.int32(offset + 0x18) != copy_id:
            raise ValueError("loadout_weapon_record_mismatch")
        return offset

    def weapon_skills(self, copy_id: int) -> list[int]:
        offset = self.weapon_offset(copy_id) + SKILL_SLOTS_OFFSET
        return [self.int32(offset + 4 * i) for i in range(SKILL_SLOTS)]


def capture(process: dict[str, str]) -> SaveSnapshot:
    """One bounded read of the save region from the verified game process."""
    reader = ReadOnlyProcess(process)
    started = time.monotonic()
    try:
        def read(address: int, size: int) -> bytes:
            if time.monotonic() - started > READ_SECONDS:
                raise ValueError("loadout_save_read_expired")
            return reader.read(address, size)

        base = reader.module_base()
        reader.verify_build(base)
        for rva, signature in SIGNATURES.items():
            if read(base + rva, len(signature)) != signature:
                raise ValueError("loadout_save_layout_changed")

        def ready() -> tuple[bytes, bytes]:
            return read(base + SAVE_READY_RVA, 8), read(base + SAVE_KIND_RVA, 4)

        guard = ready()
        if struct.unpack("<Q", guard[0])[0] == 0 or struct.unpack("<i", guard[1])[0] != 14:
            raise ValueError("loadout_save_data_unavailable")
        key = struct.unpack("<I", read(base + MODE_FLAG_KEY_RVA, 4))[0]
        region = b"".join(read(base + SAVE_RVA + offset, CHUNK) for offset in range(0, REGION_SIZE, CHUNK))
        if ready() != guard:
            raise ValueError("loadout_save_data_changed_during_read")
        reader.verify_process()
        return SaveSnapshot(region, key, str(process["token"]), time.monotonic())
    finally:
        reader.close()


def describe(offset: int, snapshot: SaveSnapshot) -> str:
    """Name a changed save offset by the layouts above."""
    if offset == CLOCK_OFFSET:
        return "play_time"
    first = MEMBER_RECORDS + snapshot.mode * 15 * MEMBER_SIZE
    if first <= offset < first + MEMBERS * MEMBER_SIZE:
        member, field = divmod(offset - first, MEMBER_SIZE)
        name = MEMBER_NAMES[member]
        if field in MEMBER_FIELDS:
            return f"member.{name}.{MEMBER_FIELDS[field]}"
        for start, label in ((ARMOR_SOCKETS, "armor_materia"), (WEAPON_SOCKETS, "weapon_materia")):
            if start <= field < start + 4 * SOCKETS and not (field - start) % 4:
                return f"member.{name}.{label}.{(field - start) // 4}"
        return f"member.{name}.+{field:#x}"
    if WEAPON_RECORDS <= offset < WEAPON_RECORDS + WEAPON_COPIES * WEAPON_SIZE:
        index, field = divmod(offset - WEAPON_RECORDS, WEAPON_SIZE)
        start = SKILL_SLOTS_OFFSET
        if start <= field < start + 4 * SKILL_SLOTS and not (field - start) % 4:
            return f"weapon.{WEAPON_BASE_ID + index}.skill.{(field - start) // 4}"
        return f"weapon.{WEAPON_BASE_ID + index}.+{field:#x}"
    if RECORD_OFFSET <= offset < RECORD_OFFSET + MAX_COPIES * MATERIA_SIZE:
        index, field = divmod(offset - RECORD_OFFSET, MATERIA_SIZE)
        label = "state" if field == MATERIA_STATE else f"+{field:#x}"
        return f"materia.{MATERIA_BASE_ID + index}.{label}"
    if SHORTCUT_TABLE[0] <= offset < SHORTCUT_TABLE[1]:
        return "combat_settings.shortcuts"
    if PARTY_TABLE[0] <= offset < PARTY_TABLE[1]:
        return "combat_settings.party"
    return "unclassified"


def diff(before: SaveSnapshot, after: SaveSnapshot) -> list[dict[str, Any]]:
    """Every changed int32 except the play-time clock, named."""
    if before.process_token != after.process_token:
        raise ValueError("loadout_game_process_changed")
    changes = []
    a, b = before.region, after.region
    for offset in range(0, min(len(a), len(b)) - 3, 4):
        if offset != CLOCK_OFFSET and a[offset:offset + 4] != b[offset:offset + 4]:
            changes.append({"offset": f"{offset:#x}", "field": describe(offset, after),
                            "before": before.int32(offset), "after": after.int32(offset)})
            if len(changes) > 64:
                break
    return changes
