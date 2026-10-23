"""Inspect PE/PDB identity and resolve crash RVAs using matching local symbols.

Reads files only; never loads UE4SS into the game or downloads symbols. Run as
python -m ff7r_agent.ue4ss_symbols --dll ... --pdb ... --offset 0x6a8c4f ...
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import struct
import uuid


def _u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def pe_identity(data: bytes) -> dict:
    if data[:2] != b"MZ":
        raise ValueError("not a PE image")
    pe = _u32(data, 0x3C)
    if data[pe:pe + 4] != b"PE\0\0":
        raise ValueError("invalid PE header")
    optional = pe + 24
    magic = struct.unpack_from("<H", data, optional)[0]
    directory = {0x10B: 96, 0x20B: 112}.get(magic)
    if directory is None:
        raise ValueError("unsupported PE optional header")
    sections = optional + struct.unpack_from("<H", data, pe + 20)[0]

    def raw(rva: int) -> int:
        for i in range(struct.unpack_from("<H", data, pe + 6)[0]):
            section = sections + i * 40
            size, address, raw_size, pointer = struct.unpack_from("<IIII", data, section + 8)
            if address <= rva < address + max(size, raw_size):
                return pointer + rva - address
        raise ValueError("debug RVA is outside PE sections")

    rva, size = struct.unpack_from("<II", data, optional + directory + 6 * 8)
    if not rva or size % 28:
        raise ValueError("no valid PE debug directory")
    start = raw(rva)
    for entry in range(start, start + size, 28):
        if _u32(data, entry + 12) != 2:  # IMAGE_DEBUG_TYPE_CODEVIEW
            continue
        pointer = _u32(data, entry + 24)
        if data[pointer:pointer + 4] == b"RSDS":
            return {"guid": str(uuid.UUID(bytes_le=data[pointer + 4:pointer + 20])),
                    "age": _u32(data, pointer + 20),
                    "pdb_path": data[pointer + 24:].split(b"\0", 1)[0].decode("utf-8", errors="replace")}
    raise ValueError("PE has no RSDS CodeView identity")


def pdb_identity(data: bytes) -> dict:
    if not data.startswith(b"Microsoft C/C++ MSF 7.00\r\n\x1aDS\0\0\0"):
        raise ValueError("not an MSF 7 PDB")
    block_size, _, _, directory_size, _, block_map = struct.unpack_from("<6I", data, 32)
    if block_size < 512 or block_size > 65536 or block_size & (block_size - 1):
        raise ValueError("invalid PDB block size")

    def count(size: int) -> int:
        return (size + block_size - 1) // block_size

    def blocks(indices, size):
        result = b"".join(data[i * block_size:(i + 1) * block_size] for i in indices)[:size]
        if len(result) != size:
            raise ValueError("truncated PDB stream")
        return result

    indices = struct.unpack_from(f"<{count(directory_size)}I", data, block_map * block_size)
    directory = blocks(indices, directory_size)
    streams = _u32(directory, 0)
    if streams < 2:
        raise ValueError("PDB info stream is missing")
    sizes = struct.unpack_from(f"<{streams}I", directory, 4)
    cursor = 4 + streams * 4
    for stream, size in enumerate(sizes):
        n = 0 if size == 0xFFFFFFFF else count(size)
        indices = struct.unpack_from(f"<{n}I", directory, cursor)
        cursor += n * 4
        if stream == 1:
            info = blocks(indices, size)
            return {"guid": str(uuid.UUID(bytes_le=info[12:28])), "age": _u32(info, 8)}
    raise ValueError("PDB info stream is missing")


def _resolve(dll: Path, pdb: Path, offsets: list[int]) -> list[dict]:
    if os.name != "nt":
        raise OSError("local symbol resolution requires Windows DbgHelp")
    from ctypes import wintypes as w

    class Symbol(ctypes.Structure):
        _fields_ = [("SizeOfStruct", w.ULONG), ("TypeIndex", w.ULONG),
                    ("Reserved", ctypes.c_uint64 * 2), ("Index", w.ULONG), ("Size", w.ULONG),
                    ("ModBase", ctypes.c_uint64), ("Flags", w.ULONG), ("Value", ctypes.c_uint64),
                    ("Address", ctypes.c_uint64), ("Register", w.ULONG), ("Scope", w.ULONG),
                    ("Tag", w.ULONG), ("NameLen", w.ULONG), ("MaxNameLen", w.ULONG), ("Name", ctypes.c_char * 1)]

    class Module(ctypes.Structure):
        _fields_ = [("SizeOfStruct", w.DWORD), ("BaseOfImage", ctypes.c_uint64),
                    ("ImageSize", w.DWORD), ("TimeDateStamp", w.DWORD), ("CheckSum", w.DWORD),
                    ("NumSyms", w.DWORD), ("SymType", w.DWORD), ("ModuleName", ctypes.c_char * 32),
                    ("ImageName", ctypes.c_char * 256), ("LoadedImageName", ctypes.c_char * 256),
                    ("LoadedPdbName", ctypes.c_char * 256), ("CVSig", w.DWORD), ("CVData", ctypes.c_char * 780),
                    ("PdbSig", w.DWORD), ("PdbSig70", ctypes.c_byte * 16), ("PdbAge", w.DWORD),
                    ("PdbUnmatched", w.BOOL), ("DbgUnmatched", w.BOOL), ("LineNumbers", w.BOOL),
                    ("GlobalSymbols", w.BOOL), ("TypeInfo", w.BOOL), ("SourceIndexed", w.BOOL),
                    ("Publics", w.BOOL), ("MachineType", w.DWORD), ("Reserved", w.DWORD)]

    api = ctypes.WinDLL("dbghelp.dll", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32.dll", use_last_error=True)
    kernel.GetCurrentProcess.restype = w.HANDLE
    process = kernel.GetCurrentProcess()
    api.SymSetOptions.argtypes = [w.DWORD]
    api.SymInitializeW.argtypes = [w.HANDLE, w.LPCWSTR, w.BOOL]
    api.SymInitializeW.restype = w.BOOL
    api.SymLoadModuleExW.argtypes = [w.HANDLE, w.HANDLE, w.LPCWSTR, w.LPCWSTR,
                                   ctypes.c_uint64, w.DWORD, ctypes.c_void_p, w.DWORD]
    api.SymLoadModuleExW.restype = ctypes.c_uint64
    api.SymFromAddr.argtypes = [w.HANDLE, ctypes.c_uint64, ctypes.POINTER(ctypes.c_uint64), ctypes.POINTER(Symbol)]
    api.SymFromAddr.restype = w.BOOL
    api.SymGetModuleInfo64.argtypes = [w.HANDLE, ctypes.c_uint64, ctypes.POINTER(Module)]
    api.SymGetModuleInfo64.restype = w.BOOL
    api.SymCleanup.argtypes = [w.HANDLE]
    # Exact symbols, no prompts, no symbol-server path; only this local PDB directory.
    api.SymSetOptions(0x400 | 0x80000 | 0x200 | 0x2)
    if not api.SymInitializeW(process, str(pdb.parent), False):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        base = api.SymLoadModuleExW(process, None, str(dll), None, 0x10000000, 0, None, 0)
        if not base:
            raise ctypes.WinError(ctypes.get_last_error())
        module = Module()
        module.SizeOfStruct = ctypes.sizeof(Module)
        if not api.SymGetModuleInfo64(process, base, ctypes.byref(module)):
            raise ctypes.WinError(ctypes.get_last_error())
        # An export-table fallback cannot name arbitrary interior crash RVAs.
        if module.SymType not in (3, 7) or module.PdbUnmatched:
            raise OSError("DbgHelp did not load matching PDB symbols")
        results = []
        for offset in offsets:
            buffer = ctypes.create_string_buffer(ctypes.sizeof(Symbol) + 4096)
            symbol = ctypes.cast(buffer, ctypes.POINTER(Symbol))
            symbol.contents.SizeOfStruct, symbol.contents.MaxNameLen = ctypes.sizeof(Symbol), 4096
            displacement = ctypes.c_uint64()
            if api.SymFromAddr(process, base + offset, ctypes.byref(displacement), symbol):
                name = ctypes.string_at(ctypes.addressof(buffer) + Symbol.Name.offset, symbol.contents.NameLen)
                results.append({"offset": hex(offset), "symbol": name.decode(errors="replace"),
                                "displacement": hex(displacement.value)})
            else:
                results.append({"offset": hex(offset), "error": ctypes.get_last_error()})
        return results
    finally:
        api.SymCleanup(process)


def inspect(dll: Path, pdb: Path, offsets: list[int]) -> dict:
    data = dll.read_bytes()
    image = pe_identity(data)
    symbols = pdb_identity(pdb.read_bytes())
    matches = all(image[key] == symbols[key] for key in ("guid", "age"))
    report = {"dll": str(dll), "dll_sha256": hashlib.sha256(data).hexdigest(), "codeview": image,
              "pdb": str(pdb), "pdb_identity": symbols, "symbols_match": matches}
    if not matches:
        report.update(resolved=False, reason="pdb_identity_mismatch", offsets=[hex(i) for i in offsets])
    elif offsets:
        report["symbols"] = _resolve(dll, pdb, offsets)
        report["resolved"] = all("symbol" in result for result in report["symbols"])
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dll", type=Path, required=True)
    parser.add_argument("--pdb", type=Path, required=True)
    parser.add_argument("--offset", type=lambda value: int(value, 0), nargs="*", default=[])
    args = parser.parse_args()
    print(json.dumps(inspect(args.dll.resolve(), args.pdb.resolve(), args.offset), indent=2))


if __name__ == "__main__":
    main()
