"""Synthetic native memory, with no process access or controller input."""
from copy import deepcopy
import struct
import threading
from types import SimpleNamespace

import pytest

from ff7r_agent import materia_memory as memory
from ff7r_agent.runtime_commands import invoke_runtime
from ff7r_agent.service import AgentService
from test_queen_blood_memory import MappedReader


def source():
    path = "/Engine/Transient.GI.Equip_C_42"
    return {"ok": True, "loadout_scope": {"owner_id": "Equip_C " + path, "owner_address": "0x10000",
            "active": True, "owner_still_attached": True},
            "selection": {"list_id": "EndVirtualScrollBox " + path +
                          ".WidgetTree.Materia_SelectList.WidgetTree.SelectList",
                          "selection_identity_observed": True, "selected_index": 1,
                          "properties": {"ItemCount": "2"}},
            "capture": {"map_id": "test-map", "source_sequence": 40}}


class MateriaReader(MappedReader):
    def __init__(self):
        super().__init__()
        self.root = bytearray(0x7B0)
        struct.pack_into("<QII", self.root, 0x10, 0x70000, 128, 43)
        struct.pack_into("<Qii", self.root, 0x650, 0x20000, 2, 2)
        struct.pack_into("<Qii", self.root, 0x660, 0x21000, 1, 1)
        self.add(0x10000, self.root)
        self.add(0x20000, struct.pack("<ii", 10, 47))
        self.add(0x21000, struct.pack("<i", 13))
        for address, name, parent in ((0x70000, 128, 0x71000), (0x71000, 160, 0)):
            header = bytearray(0x48)
            struct.pack_into("<II", header, 0x18, name, 0)
            struct.pack_into("<Q", header, 0x40, parent)
            self.add(address, header)
        for index, value in ((16, "M_IND_001"), (32, "M_SUM_001"), (128, "Equip_C"), (160, "EndEquipWindow")):
            self.add(0x60000 + index * 4, bytes(4) + struct.pack("<H", len(value) << 6) + value.encode())
        # One colliding row preceding the sought item; no catalogue guesses.
        self.add(0x40000, struct.pack("<iIIii", 99999, 16, 0, 1, 1)
                 + struct.pack("<iIIii", 13001, 16, 0, 2, 1)
                 + struct.pack("<iIIii", 14001, 32, 0, -1, 1))
        self.add(0x50000, struct.pack("<iiii", -1, 0, -1, -1))
        for index, ap in ((10, 1355), (47, 1135)):
            self.add(self.record(index), struct.pack("<8i", 600000 + index, 13001, ap, 0, 2, 0, 0, 0))
        self.add(self.record(13), struct.pack("<8i", 600013, 14001, 0, 0, 1, 0, 0, 0))
        for rva, signature in memory.SIGNATURES.items():
            self.add(self.base + rva, signature)
        self.add(self.base + memory.SAVE_READY_RVA, struct.pack("<Q", 0x12345))
        self.add(self.base + memory.SAVE_KIND_RVA, struct.pack("<i", 14))

    def record(self, index):
        return self.base + memory.SAVE_RVA + memory.RECORD_OFFSET + index * 32


def test_batch_preserves_distinct_copies_and_total_ap_without_row_widgets(monkeypatch):
    reader = MateriaReader()
    monkeypatch.setattr(memory, "ReadOnlyProcess", lambda _: reader)
    result = memory.read_materia_array(source(), {})
    assert result["ok"] and result["choices_complete"] and not result["input_sent"]
    assert not result["owned_inventory_complete"]  # No claim about filters/eligibility/capacity.
    assert result["standard_count"] == 2 and result["summon_count"] == 1 and result["count"] == 3
    assert result["materia"] == [
        {"menu_index": 0, "category": "standard", "instance_id": "600010", "item_id": "M_IND_001", "ap_total": 1355, "level": 2},
        {"menu_index": 1, "category": "standard", "instance_id": "600047", "item_id": "M_IND_001", "ap_total": 1135, "level": 2},
        {"menu_index": 0, "category": "summon", "instance_id": "600013", "item_id": "M_SUM_001", "ap_total": 0, "level": 1}]
    assert reader.closed


@pytest.mark.parametrize("fault,reason", [
    ("build", "unsupported"), ("signature", "layout_changed"), ("stale", "process changed"),
    ("class", "class_changed"), ("root", "owner_changed"), ("count", "count_changed"),
    ("bounds", "bounds_invalid"), ("duplicates", "indices_invalid"), ("index", "indices_invalid"),
    ("record", "record_invalid"), ("ap", "record_invalid"), ("level", "record_invalid"),
    ("kind", "data_unavailable"), ("resident", "data_unavailable"), ("item", "identity_invalid"),
    ("cycle", "chain_invalid"), ("name", "length_invalid"), ("expired", "expired"),
])
def test_unknown_build_corrupt_data_and_unavailable_save_fail_closed(monkeypatch, fault, reason):
    reader = MateriaReader()
    original = reader.read
    if fault == "build":
        reader.verify_build = lambda _: (_ for _ in ()).throw(ValueError("unsupported build"))
    elif fault == "signature":
        reader.add(reader.base + next(iter(memory.SIGNATURES)), bytes(32))
    elif fault == "stale":
        reader.verify_process = lambda: (_ for _ in ()).throw(ValueError("process changed"))
    elif fault == "class":
        reader.add(0x60000 + 160 * 4, bytes(4) + struct.pack("<H", 7 << 6) + b"Invalid")
    elif fault == "root":
        struct.pack_into("<I", reader.root, 0x1C, 44)
        reader.add(0x10000, reader.root)
    elif fault in {"count", "bounds"}:
        struct.pack_into("<i", reader.root, 0x658, 1 if fault == "count" else 4097)
        reader.add(0x10000, reader.root)
    elif fault in {"duplicates", "index"}:
        reader.add(0x20000, struct.pack("<ii", 10, 10 if fault == "duplicates" else 4096))
    elif fault in {"record", "ap", "level"}:
        values = [600010, 13001, 1355, 0, 2, 0, 0, 0]
        values[{"record": 0, "ap": 2, "level": 4}[fault]] = -1 if fault != "level" else 0
        reader.add(reader.record(10), struct.pack("<8i", *values))
    elif fault == "kind":
        reader.add(reader.base + memory.SAVE_KIND_RVA, struct.pack("<i", 13))
    elif fault == "resident":
        reader.add(reader.base + memory.SAVE_READY_RVA, bytes(8))
    elif fault == "item":
        reader.add(0x60000 + 16 * 4, bytes(4) + struct.pack("<H", 7 << 6) + b"Unknown")
    elif fault == "cycle":
        reader.add(0x40000, struct.pack("<iIIii", 99999, 16, 0, 0, 1) + bytes(20))
    elif fault == "name":
        reader.add(0x60000 + 128 * 4, bytes(4) + struct.pack("<H", 300 << 6))
    elif fault == "expired":
        ticks = iter([0, 6])
        monkeypatch.setattr(memory.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(memory, "ReadOnlyProcess", lambda _: reader)
    with pytest.raises(ValueError, match=reason):
        memory.read_materia_array(source(), {})
    assert reader.closed


@pytest.mark.parametrize("target", ["array", "summon_array", "summon_header", "record", "root", "save", "registry", "selection"])
def test_mid_read_changes_are_rejected_without_returning_partial_inventory(monkeypatch, target):
    reader = MateriaReader()
    address, size = {"array": (0x20000, 8), "summon_array": (0x21000, 4),
                     "summon_header": (0x10660, 16), "record": (reader.record(10), 32),
                     "root": (0x10000, 0x28), "save": (reader.base + memory.SAVE_KIND_RVA, 4),
                     "registry": (reader.base + memory.REGISTRY_RVA, 0x50),
                     "selection": (0x10000 + 0x7A0, 16)}[target]
    original = reader.read
    reads = 0

    def changed(at, length):
        nonlocal reads
        raw = original(at, length)
        if (at, length) == (address, size):
            reads += 1
            if reads == 2:
                return bytes([raw[0] ^ 1]) + raw[1:]
        return raw

    reader.read = changed
    monkeypatch.setattr(memory, "ReadOnlyProcess", lambda _: reader)
    with pytest.raises(ValueError, match="changed_during_read"):
        memory.read_materia_array(source(), {})
    assert reader.closed


def test_cross_list_duplicate_or_wrong_category_is_rejected(monkeypatch):
    reader = MateriaReader()
    monkeypatch.setattr(memory, "ReadOnlyProcess", lambda _: reader)
    reader.add(0x21000, struct.pack("<i", 10))
    with pytest.raises(ValueError, match="copy_indices_invalid"):
        memory.read_materia_array(source(), {})
    reader = MateriaReader()
    monkeypatch.setattr(memory, "ReadOnlyProcess", lambda _: reader)
    reader.add(reader.record(13), struct.pack("<8i", 600013, 13001, 0, 0, 1, 0, 0, 0))
    with pytest.raises(ValueError, match="array_category_changed"):
        memory.read_materia_array(source(), {})


def test_empty_summon_array_needs_no_pointer_read(monkeypatch):
    reader = MateriaReader()
    struct.pack_into("<Qii", reader.root, 0x660, 0, 0, 0)
    reader.add(0x10000, reader.root)
    monkeypatch.setattr(memory, "ReadOnlyProcess", lambda _: reader)
    result = memory.read_materia_array(source(), {})
    assert result["count"] == 2 and result["summon_count"] == 0


def test_outside_owner_or_wrong_list_never_opens_process(monkeypatch):
    monkeypatch.setattr(memory, "ReadOnlyProcess", lambda _: pytest.fail("unsafe native read"))
    for field in ("owner_id", "owner_address"):
        src = source()
        src["loadout_scope"][field] = "invalid"
        with pytest.raises(ValueError, match="owner_unverified"):
            memory.read_materia_array(src, {})
    src = source()
    src["selection"]["list_id"] = "EndListBox /OtherMenu.SelectList"
    with pytest.raises(ValueError, match="inventory_not_open"):
        memory.read_materia_array(src, {})


def service_fixture(monkeypatch, change=None):
    loaded = {"loaded_save_verified": True, "loaded_save": {
        "load_id": "verified-load", "process": {"pid": "1", "token": "2"}}}
    before = source()
    after = deepcopy(before)
    after["capture"]["source_sequence"] += 1
    current = deepcopy(loaded)
    if change:
        change(after, current)
    captures = iter([before, after])
    receipts = iter([loaded, current])
    service = SimpleNamespace(get_queens_blood_run_status=lambda: next(receipts))
    monkeypatch.setattr("ff7r_agent.loadout_menu.read_loadout_menu", lambda *a, **k: next(captures))
    monkeypatch.setattr(memory, "read_materia_array", lambda *a: {"ok": True, "input_sent": False})
    return service


def test_service_brackets_batch_with_fresh_same_menu_and_loaded_save(monkeypatch):
    result = memory.read_materia_inventory(service_fixture(monkeypatch))
    assert result["ok"] and result["context"]["load_id"] == "verified-load"


@pytest.mark.parametrize("change", [
    lambda a, c: a.update(ok=False),
    lambda a, c: a["loadout_scope"].update(owner_address="0x99999"),
    lambda a, c: a["selection"].update(selected_index=0),
    lambda a, c: a["capture"].update(source_sequence=40),
    lambda a, c: a["capture"].update(map_id="other-map"),
    lambda a, c: c["loaded_save"].update(load_id="another-load"),
    lambda a, c: c.update(loaded_save_verified=False),
])
def test_menu_exit_changed_selection_save_or_stale_receipt_discards_batch(monkeypatch, change):
    result = memory.read_materia_inventory(service_fixture(monkeypatch, change))
    assert not result["ok"] and result["reason"] == "materia_menu_or_save_changed"
    assert not result["input_sent"] and "materia" not in result


def test_registry_operation_takes_no_addresses_or_injected_callbacks(monkeypatch):
    monkeypatch.setattr(memory, "read_materia_inventory", lambda service: {"ok": True, "input_sent": False})
    service = object.__new__(AgentService)
    service._action_lock = threading.Lock()
    assert invoke_runtime(service, None, None, "read_materia_inventory", {})["ok"]
    assert not service._action_lock.locked()
    with pytest.raises(TypeError):
        invoke_runtime(service, None, None, "read_materia_inventory", {"owner_address": "0x10000"})
