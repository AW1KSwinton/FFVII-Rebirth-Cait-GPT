"""Build-pinned, read-only Materia choices from native equipment arrays.

The owner address comes only from a fresh, attached game-thread menu receipt.
No object-array scan, widget population, native call, memory write or input.
Copy IDs are scoped to the verified loaded save, never widget identities.
"""
from __future__ import annotations

import re
import struct
import time
from typing import Any

from .queen_blood_memory import ReadOnlyProcess

ARRAY_OFFSET = 0x650
ARRAY_OFFSETS = {"standard": ARRAY_OFFSET, "summon": 0x660}
SAVE_RVA = 0x70C1F40
SAVE_READY_RVA = 0x92A0440
SAVE_KIND_RVA = 0x70C1F50
RECORD_OFFSET = 0xBFA58
REGISTRY_RVA = 0x6FCEE10
NAME_POOL_RVA = 0x919D280
MAX_COPIES = 4096
SIGNATURES = {
    # EndEquipWindow::OnMateriaEditListSetupItem indexes int32 copies at +650.
    0x3E32A8C: bytes.fromhex("3b9e580600000f8d2a020000488b8650060000448b3c98418bcf"),
    # Separate Summon choices use +660, not a filter of the standard array.
    0x3E330DA: bytes.fromhex("413b9e680600000f8da6000000498b8660060000488d55d78b1c98"),
    # Copy accessor bounds 0..4095, then save + BFA58 + index * 32.
    0x140B2C6: bytes.fromhex("81faff0f00007715488bca480558fa0b0048c1e1054803c1"),
    # Save accessor requires a resident game context and save kind 14.
    0xAC3F20: bytes.fromhex("33c048390517c57d087412833d1ee05f060e488d0d07e05f06480f44c1c3"),
}


class _Rows:
    def __init__(self, read, base):
        self.read = read
        self.pool = base + NAME_POOL_RVA
        self.registry = base + REGISTRY_RVA
        self.header = read(self.registry, 0x50)
        self.data, self.slots = struct.unpack_from("<Qi", self.header)
        self.hash_pointer = struct.unpack_from("<Q", self.header, 0x40)[0] or self.registry + 0x38
        self.hash_size = struct.unpack_from("<i", self.header, 0x48)[0]
        if (not 0 < self.slots <= 100000 or not 0 < self.hash_size <= 262144
                or self.hash_size & (self.hash_size - 1)):
            raise ValueError("materia_registry_bounds_invalid")

    def name(self, index: int, number: int) -> str:
        block, offset = index >> 16, index & 0xFFFF
        if not 0 <= block < 8192:
            raise ValueError("materia_name_block_invalid")
        pointer = struct.unpack("<Q", self.read(self.pool + 0x10 + block * 8, 8))[0]
        entry = pointer + offset * 4
        flags = struct.unpack("<H", self.read(entry + 4, 2))[0]
        length, wide = flags >> 6, flags & 1
        if not 0 < length <= 256:
            raise ValueError("materia_name_length_invalid")
        value = self.read(entry + 6, length * (2 if wide else 1)).decode("utf-16-le" if wide else "utf-8")
        return value if number == 0 else value + "_" + str(number - 1)

    def object_name(self, header: bytes) -> str:
        return self.name(*struct.unpack_from("<II", header, 0x18))

    def resolve(self, identifier: int) -> str:
        bucket = identifier & (self.hash_size - 1)
        slot = struct.unpack("<i", self.read(self.hash_pointer + bucket * 4, 4))[0]
        seen = set()
        while slot != -1:
            if not 0 <= slot < self.slots or slot in seen or len(seen) >= 128:
                raise ValueError("materia_registry_chain_invalid")
            seen.add(slot)
            key, index, number, following, _ = struct.unpack("<iIIii", self.read(self.data + slot * 20, 20))
            if key == identifier:
                value = self.name(index, number)
                if not re.fullmatch(r"M_(?:MAG|SUP|COM|IND|SUM)_[0-9]{3}", value):
                    raise ValueError("materia_item_identity_invalid")
                return value
            slot = following
        raise ValueError("materia_item_identity_missing")


def read_materia_array(source: dict, process: dict[str, str]) -> dict[str, Any]:
    scope = source.get("loadout_scope") or {}
    owner = scope.get("owner_id", "")
    selection = source.get("selection") or {}
    if (source.get("ok") is not True or scope.get("active") is not True
            or scope.get("owner_still_attached") is not True
            or not isinstance(owner, str)
            or not re.fullmatch(r"Equip_C /Engine/Transient\..*\.Equip_C_\d+", owner)
            or not re.fullmatch(r"0x[0-9a-fA-F]+", str(scope.get("owner_address", "")))):
        raise ValueError("materia_native_owner_unverified")
    path = owner.partition(" ")[2]
    from .loadout_menu import SELECTION_LISTS
    if (selection.get("list_id", "").partition(" ")[2] != path + SELECTION_LISTS["materia_inventory"]
            or selection.get("selection_identity_observed") is not True):
        raise ValueError("materia_inventory_not_open")
    count_text = str((selection.get("properties") or {}).get("ItemCount", ""))
    if not re.fullmatch(r"[0-9]{1,4}", count_text):
        raise ValueError("materia_menu_count_unverified")
    expected_count = int(count_text)
    reader = ReadOnlyProcess(process)
    started = time.monotonic()
    try:
        def read(address, size):
            if time.monotonic() - started > 5:
                raise ValueError("materia_memory_read_expired")
            return reader.read(address, size)

        base = reader.module_base()
        reader.verify_build(base)
        for rva, signature in SIGNATURES.items():
            if read(base + rva, len(signature)) != signature:
                raise ValueError("materia_native_layout_changed")
        rows = _Rows(read, base)
        address = int(scope["owner_address"], 16)
        root = read(address, 0x28)
        if rows.object_name(root) != path.rsplit(".", 1)[-1]:
            raise ValueError("materia_native_owner_changed")
        cls = struct.unpack_from("<Q", root, 0x10)[0]
        ancestry = []
        for _ in range(8):
            if not cls:
                break
            header = read(cls, 0x48)
            ancestry.append(rows.object_name(header))
            cls = struct.unpack_from("<Q", header, 0x40)[0]
        if ancestry[:2] != ["Equip_C", "EndEquipWindow"]:
            raise ValueError("materia_native_class_changed")
        guard = read(address + 0x590, 2), read(address + 0x7A0, 16)
        array_headers = {kind: read(address + offset, 16) for kind, offset in ARRAY_OFFSETS.items()}
        arrays = {kind: struct.unpack("<Qii", header) for kind, header in array_headers.items()}
        if (any(not 0 <= count <= MAX_COPIES or not count <= capacity <= MAX_COPIES
                for pointer, count, capacity in arrays.values())
                or sum(a[1] for a in arrays.values()) > MAX_COPIES):
            raise ValueError("materia_array_bounds_invalid")
        if arrays["standard"][1] != expected_count:
            raise ValueError("materia_menu_count_changed")

        def array_bytes(kind):
            pointer, count, _ = arrays[kind]
            return b"".join(read(pointer + offset, min(8192, count * 4 - offset))
                            for offset in range(0, count * 4, 8192))

        contents = {kind: array_bytes(kind) for kind in arrays}
        identifiers = {kind: struct.unpack("<" + "i" * arrays[kind][1], raw) for kind, raw in contents.items()}
        all_identifiers = [identifier for items in identifiers.values() for identifier in items]
        if (len(set(all_identifiers)) != len(all_identifiers)
                or any(not 0 <= i < MAX_COPIES for i in all_identifiers)):
            raise ValueError("materia_copy_indices_invalid")

        def save_guard():
            return read(base + SAVE_READY_RVA, 8), read(base + SAVE_KIND_RVA, 4)

        ready = save_guard()
        if struct.unpack("<Q", ready[0])[0] == 0 or struct.unpack("<i", ready[1])[0] != 14:
            raise ValueError("materia_save_data_unavailable")
        result, records = [], []
        for kind, items in identifiers.items():
            for menu_index, identifier in enumerate(items):
                record_address = base + SAVE_RVA + RECORD_OFFSET + identifier * 32
                raw = read(record_address, 32)
                instance, item, ap, _, state, *_ = struct.unpack("<8i", raw)
                level = state & 0xFF
                if instance != 600000 + identifier or not 0 <= ap <= 0x7FFFFFFF or not 1 <= level <= 5:
                    raise ValueError("materia_copy_record_invalid")
                item_id = rows.resolve(item)
                if item_id.startswith("M_SUM_") != (kind == "summon"):
                    raise ValueError("materia_array_category_changed")
                result.append({"menu_index": menu_index, "category": kind, "instance_id": str(instance),
                               "item_id": item_id, "ap_total": ap, "level": level})
                records.append((record_address, raw))
        if (read(address, 0x28) != root
                or any(read(address + ARRAY_OFFSETS[k], 16) != h for k, h in array_headers.items())
                or any(array_bytes(k) != raw for k, raw in contents.items()) or save_guard() != ready
                or (read(address + 0x590, 2), read(address + 0x7A0, 16)) != guard
                or read(rows.registry, 0x50) != rows.header
                or any(read(at, 32) != raw for at, raw in records)):
            raise ValueError("materia_data_changed_during_read")
        reader.verify_process()
        return {"ok": True, "source": "native_materia_array", "read_only": True,
                "input_sent": False, "choices_complete": True, "scope": "equipment_menu_materia_choices",
                "owned_inventory_complete": False, "count": len(result),
                "standard_count": arrays["standard"][1], "summon_count": arrays["summon"][1], "materia": result,
                "memory_seconds": round(time.monotonic() - started, 4)}
    finally:
        reader.close()


def read_materia_inventory(service) -> dict[str, Any]:
    """Two single-shot menu receipts around one bounded native-array read."""
    from .loadout_menu import read_loadout_menu

    def refuse(reason):
        return {"ok": False, "reason": reason, "input_sent": False, "choices_complete": False}

    loaded = service.get_queens_blood_run_status()
    if not loaded.get("loaded_save_verified") or not (loaded.get("loaded_save") or {}).get("process"):
        return refuse("materia_verified_save_required")
    before = read_loadout_menu(service, stage="materia_inventory")
    if not before.get("ok"):
        return refuse(before.get("reason") or "materia_inventory_not_open")
    try:
        result = read_materia_array(before, loaded["loaded_save"]["process"])
    except (OSError, ValueError) as error:
        return refuse(str(error))
    after = read_loadout_menu(service, stage="materia_inventory")
    current = service.get_queens_blood_run_status()
    if (not after.get("ok") or not current.get("loaded_save_verified")
            or loaded.get("loaded_save") != current.get("loaded_save")
            or any((before.get("loadout_scope") or {}).get(key) != (after.get("loadout_scope") or {}).get(key)
                   for key in ("owner_id", "owner_address"))
            or any((before.get("selection") or {}).get(key) != (after.get("selection") or {}).get(key)
                   for key in ("list_id", "selected_index", "properties"))
            or before["capture"]["map_id"] != after["capture"]["map_id"]
            or after["capture"]["source_sequence"] <= before["capture"]["source_sequence"]):
        return refuse("materia_menu_or_save_changed")
    result["context"] = {"load_id": loaded["loaded_save"]["load_id"],
                         "process_token": loaded["loaded_save"]["process"]["token"],
                         "map_id": after["capture"]["map_id"],
                         "source_sequence": after["capture"]["source_sequence"]}
    return result
