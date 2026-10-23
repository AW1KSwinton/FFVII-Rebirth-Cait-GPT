"""Build-gated, read-only access to the live card controller's backing data.

Addresses originate from a one-shot game-thread probe, never from callers.
The reader and discovery helper neither write memory nor call game functions.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import struct
import re
import hashlib
from functools import lru_cache
from pathlib import Path
import time

from .runtime_ownership import process_identity_token
from .queen_blood_catalogue import card_id_for_key_item, catalogue_card

SUPPORTED_EXE_SHA256 = "37d952fabbfa2b710f8ae6490997c9755e4e57b4fd545078c9baaf46c9b233d7"


@lru_cache(maxsize=4)
def _image_hash(path: str, size: int, modified: int) -> str:
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class ReadOnlyProcess:
    def __init__(self, process: dict[str, str]):
        pid = int(process["pid"])
        self.process = dict(process)
        if process_identity_token(pid) != process["token"]:
            raise ValueError("the inspected game process changed")
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.api.OpenProcess.restype = wintypes.HANDLE
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.api.CloseHandle.restype = wintypes.BOOL
        self.api.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                              ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
        self.api.ReadProcessMemory.restype = wintypes.BOOL
        self.handle = self.api.OpenProcess(0x10 | 0x400, False, pid)  # VM_READ | QUERY_INFORMATION
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())

    def module_base(self) -> int:
        modules = (wintypes.HMODULE * 256)()
        count = wintypes.DWORD()
        self.api.K32EnumProcessModules.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                                   ctypes.POINTER(wintypes.DWORD)]
        self.api.K32EnumProcessModules.restype = wintypes.BOOL
        self.api.K32GetModuleFileNameExW.argtypes = [wintypes.HANDLE, wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD]
        self.api.K32GetModuleFileNameExW.restype = wintypes.DWORD
        if not self.api.K32EnumProcessModules(self.handle, modules, ctypes.sizeof(modules), ctypes.byref(count)):
            raise ctypes.WinError(ctypes.get_last_error())
        path = ctypes.create_unicode_buffer(1024)
        if not self.api.K32GetModuleFileNameExW(self.handle, modules[0], path, len(path)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not path.value.lower().endswith("\\ff7rebirth_.exe"):
            raise ValueError("the inspected process is not FFVII Rebirth")
        self.image_path = Path(path.value)
        return int(modules[0])

    def verify_build(self, base: int) -> None:
        info = self.image_path.stat()
        if _image_hash(str(self.image_path), info.st_size, info.st_mtime_ns) != SUPPORTED_EXE_SHA256:
            raise ValueError("bulk card reads are unsupported on this game build")
        # Validate the loaded image as well as the file. Population hooks are
        # on the exec wrappers, not these authored array-access instructions.
        expected = {0x38A88AB: bytes.fromhex("413b9e08010000"),
                    0x38A7AF5: bytes.fromhex("413bb530010000")}
        for rva, signature in expected.items():
            if self.read(base + rva, len(signature)) != signature:
                raise ValueError("the loaded card-list layout signature changed")

    def verify_process(self) -> None:
        if process_identity_token(int(self.process["pid"])) != self.process["token"]:
            raise ValueError("the inspected game process changed")

    def read(self, address: int, size: int) -> bytes:
        if not 0x10000 <= address < 0x0000800000000000 or not 0 < size <= 8192:
            raise ValueError("memory read exceeds the discovery bounds")
        buffer = ctypes.create_string_buffer(size)
        count = ctypes.c_size_t()
        if not self.api.ReadProcessMemory(self.handle, address, buffer, size, ctypes.byref(count)) or count.value != size:
            raise ctypes.WinError(ctypes.get_last_error())
        return buffer.raw

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def inspect_controller(probe: dict, process: dict[str, str]) -> dict:
    if not re.fullmatch(r"/Engine/Transient\..*\.EndNewCardDeckWindow_\d+", str(probe.get("controller", ""))):
        raise ValueError("a live controller was not observed")
    if probe.get("function_path") not in {
            "/Script/EndGame.EndNewCardDeckWindow:OnCardListPlayerCellSetupItem",
            "/Script/EndGame.EndNewCardDeckWindow:OnCardListEditCellSetupItem"}:
        raise ValueError("the inspected function is not a card-list population callback")
    reader = ReadOnlyProcess(process)
    try:
        controller = reader.read(int(probe["controller_address"], 16), 0x308)
        function = reader.read(int(probe["function_address"], 16), 0xE0)
        # The installed UE4SS log confirms UFunction::Func=0xD8 and flags=0xB0.
        flags = struct.unpack_from("<I", function, 0xB0)[0]
        if flags != probe.get("function_flags"):
            raise ValueError("the observed function metadata changed")
        native = struct.unpack_from("<Q", function, 0xD8)[0]
        arrays = []
        for offset in range(0x28, len(controller) - 15, 8):
            pointer, count, capacity = struct.unpack_from("<Qii", controller, offset)
            if 0x10000 <= pointer < 0x800000000000 and 0 < count <= 220 and count <= capacity <= 4096:
                entry = {"offset": hex(offset), "address": hex(pointer), "count": count, "capacity": capacity}
                try:
                    entry["data_hex"] = reader.read(pointer, count * 16).hex()
                except OSError as exc:
                    entry["error"] = str(exc)
                arrays.append(entry)
        bulk = None
        if hasattr(reader, "module_base"):
            bulk = discover_bulk(reader, reader.module_base(), controller)
        return {"read_only": True, "controller_hex": controller.hex(), "function_hex": function.hex(),
                "arrays": arrays, "bulk": bulk, "native_address": hex(native), "native_hex": reader.read(native, 4096).hex()}
    finally:
        reader.close()


def discover_bulk(reader, base: int, controller: bytes) -> dict:
    # Original shipping code: accessor 0x140BE5050 and FName::ToString
    # 0x1407C8948 / name-pool getter 0x1407E4928. No functions are invoked.
    registry = base + 0x6FCEE10
    header = reader.read(registry, 0x50)
    data, slots = struct.unpack_from("<Qi", header)
    hash_pointer = struct.unpack_from("<Q", header, 0x40)[0] or registry + 0x38
    hash_size = struct.unpack_from("<i", header, 0x48)[0]
    if not 0 < slots <= 100000 or not 0 < hash_size <= 262144 or hash_size & (hash_size - 1):
        raise ValueError("the runtime row registry bounds are unverified")
    pool = base + 0x919D280

    def name(index: int, number: int) -> str:
        block, offset = index >> 16, index & 0xFFFF
        if not 0 <= block < 8192:
            raise ValueError("FName block is outside the pool")
        pointer = struct.unpack("<Q", reader.read(pool + 0x10 + block * 8, 8))[0]
        entry = pointer + offset * 4
        length_flags = struct.unpack("<H", reader.read(entry + 4, 2))[0]
        length, wide = length_flags >> 6, length_flags & 1
        if not 0 < length <= 256:
            raise ValueError("FName length is invalid")
        value = reader.read(entry + 6, length * (2 if wide else 1)).decode("utf-16-le" if wide else "utf-8")
        return value if number == 0 else value + "_" + str(number - 1)

    def resolve(identifier: int) -> str:
        if not 100000 <= identifier < 103200:
            raise ValueError("card accessor ID is outside its resident table")
        metadata = reader.read(base + 0x7111998 + (identifier - 100000) * 32, 32)
        row_identifier = struct.unpack_from("<i", metadata, 8)[0]
        bucket = row_identifier & (hash_size - 1)
        slot = struct.unpack("<i", reader.read(hash_pointer + bucket * 4, 4))[0]
        seen = set()
        while slot != -1:
            if not 0 <= slot < slots or slot in seen or len(seen) >= 128:
                raise ValueError("the runtime row registry chain is invalid")
            seen.add(slot)
            key, index, number, following, _ = struct.unpack("<iIIii", reader.read(data + slot * 20, 20))
            if key == row_identifier:
                return name(index, number)
            slot = following
        raise ValueError(f"native card ID {identifier} has no row identity")

    result = {"module_base": hex(base), "registry_slots": slots, "hash_size": hash_size}
    # The editor permits an overfull draft (17/15 observed live). Reading it
    # must not block the owned-grid read. Bound the deck storage by the grid's
    # 220 types and at most two copies per type; match/deck-edit validation
    # still requires exactly 15 cards before play.
    for area, offset, maximum in (("Player", 0x100, 440), ("Edit", 0x128, 220)):
        pointer, count, capacity = struct.unpack_from("<Qii", controller, offset)
        if not 0 <= count <= maximum or not count <= capacity <= 4096:
            raise ValueError("the backing card-list bounds are invalid")
        identifiers = list(struct.unpack("<" + "i" * count, reader.read(pointer, count * 4))) if count else []
        row_names = [resolve(i) for i in identifiers]
        cards = [card_id_for_key_item(row) for row in row_names]
        if any(card is None or catalogue_card(card) is None for card in cards):
            raise ValueError("a native card row has no unique catalogue identity")
        result[area] = {"native_ids": identifiers, "row_names": row_names, "card_ids": cards}
        # Reads are always fresh; never cache deck contents, sort order or
        # ownership across saves/screens. Reject an in-flight array rebuild.
        if count and reader.read(pointer, count * 4) != struct.pack("<" + "i" * count, *identifiers):
            raise ValueError("the backing card list changed during the read")
    if reader.read(registry, 0x50) != header:
        raise ValueError("the native row registry changed during the read")
    return result


def read_card_arrays(source: dict, process: dict[str, str]) -> dict:
    """Read exactly the two authored arrays from a verified live source.

    No scan, memory write, native call, widget population or cursor input.
    A second source receipt is checked by the service before accepting data.
    """
    started = time.monotonic()
    controller_path, top = str(source.get("controller", "")), str(source.get("top_path", ""))
    if (source.get("ok") is not True or source.get("read_only") is not True
            or not top.startswith("/Engine/Transient.")
            or not controller_path.startswith(top + ".")
            or not re.fullmatch(re.escape(top) + r"\.EndNewCardDeckWindow_\d+", controller_path)):
        raise ValueError("the live controller root is unverified")
    address = int(source["controller_address"], 16)
    reader = ReadOnlyProcess(process)
    try:
        base = reader.module_base()
        reader.verify_build(base)
        before = reader.read(address, 0x138)
        result = discover_bulk(reader, base, before)
        after = reader.read(address, 0x138)
        if after != before:
            raise ValueError("the live controller changed during the read")
        if len(set(result["Edit"]["card_ids"])) != len(result["Edit"]["card_ids"]):
            raise ValueError("the owned grid contains duplicate card types")
        reader.verify_process()
        return {"ok": True, "source": "native_card_arrays", "read_only": True,
                "controller": controller_path, "arrays": result,
                "memory_seconds": round(time.monotonic() - started, 4)}
    finally:
        reader.close()
