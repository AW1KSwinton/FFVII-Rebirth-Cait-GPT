import struct

import pytest

from ff7r_agent import queen_blood_memory as memory


def test_discovery_reads_only_fixed_controller_function_and_code_blocks(monkeypatch):
    reads = []
    closed = []
    function = bytearray(0xE0)
    struct.pack_into("<I", function, 0xB0, 0x40401)
    struct.pack_into("<Q", function, 0xD8, 0x30000)

    class Reader:
        def __init__(self, process):
            assert process == {"pid": "7", "token": "8"}

        def read(self, address, size):
            reads.append((address, size))
            return bytes(function) if address == 0x20000 else bytes(size)

        def close(self):
            closed.append(True)

    monkeypatch.setattr(memory, "ReadOnlyProcess", Reader)
    result = memory.inspect_controller({
        "controller": "/Engine/Transient.GI.Top.EndNewCardDeckWindow_42",
        "controller_address": "0x10000", "function_address": "0x20000",
        "function_flags": 0x40401,
        "function_path": "/Script/EndGame.EndNewCardDeckWindow:OnCardListPlayerCellSetupItem",
    }, {"pid": "7", "token": "8"})
    assert result["read_only"]
    assert reads == [(0x10000, 0x308), (0x20000, 0xE0), (0x30000, 4096)]
    assert closed == [True]


def test_discovery_rejects_template_or_unrelated_function_before_opening_process():
    with pytest.raises(ValueError, match="live controller"):
        memory.inspect_controller({"controller": "/Game/Template"}, {})
    with pytest.raises(ValueError, match="card-list"):
        memory.inspect_controller({"controller": "/Engine/Transient.GI.Top.EndNewCardDeckWindow_42",
                                   "function_path": "/Script/Unrelated:Mutate"}, {})


class MappedReader:
    """Sparse memory fixture with collisions, a sparse hole, and duplicate copies."""
    base = 0x10000000

    def __init__(self):
        self.regions = {}
        self.closed = False
        self.controller = bytearray(0x138)
        self.add(0x20000, struct.pack("<iii", 100015, 100015, 100017))
        self.add(0x30000, struct.pack("<ii", 100017, 100015))
        struct.pack_into("<Qii", self.controller, 0x100, 0x20000, 3, 24)
        struct.pack_into("<Qii", self.controller, 0x128, 0x30000, 2, 12)
        self.add(0x10000, self.controller)
        registry = bytearray(0x50)
        struct.pack_into("<Qi", registry, 0, 0x40000, 3)
        struct.pack_into("<Qi", registry, 0x40, 0x50000, 4)
        self.add(self.base + 0x6FCEE10, registry)
        # Both row IDs hash to bucket 3. Slot 1 is an unused sparse hole.
        self.add(0x40000, struct.pack("<iIIii", 11, 16, 0, -1, 3)
                 + bytes(20) + struct.pack("<iIIii", 15, 64, 0, 0, 3))
        self.add(0x50000, struct.pack("<iiii", -1, -1, -1, 2))
        for ordinal, row_id in ((15, 11), (17, 15)):
            metadata = bytearray(32)
            struct.pack_into("<i", metadata, 8, row_id)
            self.add(self.base + 0x7111998 + ordinal * 32, metadata)
        self.add(self.base + 0x919D280 + 0x10, struct.pack("<Q", 0x60000))
        for offset, value in ((16, "it_key_minigamecard_000"), (64, "it_key_minigamecard_002")):
            self.add(0x60000 + offset * 4, bytes(4) + struct.pack("<H", len(value) << 6) + value.encode())

    def add(self, address, data):
        self.regions[address] = bytes(data)

    def read(self, address, size):
        assert 0 < size <= 8192
        for start, data in self.regions.items():
            if start <= address and address + size <= start + len(data):
                return data[address - start:address - start + size]
        raise OSError(f"unmapped read {address:#x}/{size}")

    def module_base(self):
        return self.base

    def verify_build(self, base):
        assert base == self.base

    def verify_process(self):
        pass

    def close(self):
        self.closed = True


def source():
    return {"ok": True, "read_only": True, "top_path": "/Engine/Transient.GI.Top",
            "controller": "/Engine/Transient.GI.Top.EndNewCardDeckWindow_42", "controller_address": "0x10000"}


def test_bulk_resolves_accessor_metadata_hash_collisions_and_exact_duplicate_order(monkeypatch):
    reader = MappedReader()
    monkeypatch.setattr(memory, "ReadOnlyProcess", lambda _: reader)
    result = memory.read_card_arrays(source(), {})
    assert result["arrays"]["Player"]["card_ids"] == ["card_id_000", "card_id_000", "card_id_002"]
    assert result["arrays"]["Edit"]["card_ids"] == ["card_id_002", "card_id_000"]
    assert reader.closed


def test_bulk_can_read_collection_while_the_editor_has_an_overfull_draft(monkeypatch):
    reader = MappedReader()
    identifiers = [100015, 100017] * 8 + [100015]
    reader.add(0x20000, struct.pack("<" + "i" * len(identifiers), *identifiers))
    struct.pack_into("<i", reader.controller, 0x108, len(identifiers))
    reader.add(0x10000, reader.controller)
    monkeypatch.setattr(memory, "ReadOnlyProcess", lambda _: reader)
    result = memory.read_card_arrays(source(), {})
    assert result["arrays"]["Player"]["card_ids"] == ["card_id_000", "card_id_002"] * 8 + ["card_id_000"]
    assert result["arrays"]["Edit"]["card_ids"] == ["card_id_002", "card_id_000"]
    assert reader.closed


@pytest.mark.parametrize("fault,message", [
    ("cycle", "chain"), ("bad_count", "bounds"), ("unknown", "catalogue"),
    ("long_name", "length"), ("outside_accessor", "resident table"),
    ("stale", "process changed"), ("unsupported_build", "unsupported"),
])
def test_bulk_refuses_corrupt_or_stale_sources_and_closes_handle(monkeypatch, fault, message):
    reader = MappedReader()
    if fault == "cycle":
        reader.add(0x40000, struct.pack("<iIIii", 99, 16, 0, 0, 3) + bytes(20)
                   + struct.pack("<iIIii", 98, 64, 0, 0, 3))
    elif fault == "bad_count":
        struct.pack_into("<ii", reader.controller, 0x108, 441, 512)
        reader.add(0x10000, reader.controller)
    elif fault == "unknown":
        monkeypatch.setattr(memory, "card_id_for_key_item", lambda _: None)
    elif fault == "long_name":
        reader.add(0x60000 + 64, bytes(4) + struct.pack("<H", 300 << 6))
    elif fault == "outside_accessor":
        reader.add(0x20000, struct.pack("<iii", -1, 100015, 100017))
    elif fault == "stale":
        def stale():
            raise ValueError("the inspected game process changed")
        reader.verify_process = stale
    elif fault == "unsupported_build":
        def unsupported(_):
            raise ValueError("unsupported build")
        reader.verify_build = unsupported
    monkeypatch.setattr(memory, "ReadOnlyProcess", lambda _: reader)
    with pytest.raises(ValueError, match=message):
        memory.read_card_arrays(source(), {})
    assert reader.closed


def test_bulk_rechecks_array_data_to_reject_mid_read_mutation():
    reader = MappedReader()
    original = reader.read
    reads = 0

    def mutating(address, size):
        nonlocal reads
        if address == 0x20000:
            reads += 1
            if reads == 2:
                return struct.pack("<iii", 100017, 100015, 100017)
        return original(address, size)

    reader.read = mutating
    with pytest.raises(ValueError, match="changed during"):
        memory.discover_bulk(reader, reader.base, reader.controller)


def test_bulk_uses_current_arrays_on_every_read_instead_of_save_cache():
    reader = MappedReader()
    before = memory.discover_bulk(reader, reader.base, reader.controller)
    reader.add(0x20000, struct.pack("<iii", 100017, 100017, 100015))
    after = memory.discover_bulk(reader, reader.base, reader.controller)
    assert before["Player"]["card_ids"] != after["Player"]["card_ids"]


def test_build_hash_and_loaded_instruction_signatures_are_both_required(monkeypatch):
    from types import SimpleNamespace
    reader = object.__new__(memory.ReadOnlyProcess)
    reader.image_path = SimpleNamespace(stat=lambda: SimpleNamespace(st_size=7, st_mtime_ns=9))
    monkeypatch.setattr(memory, "_image_hash", lambda *_: "wrong")
    with pytest.raises(ValueError, match="unsupported"):
        reader.verify_build(0)
    monkeypatch.setattr(memory, "_image_hash", lambda *_: memory.SUPPORTED_EXE_SHA256)
    reader.read = lambda *_: b"wrong"
    with pytest.raises(ValueError, match="signature changed"):
        reader.verify_build(0)
