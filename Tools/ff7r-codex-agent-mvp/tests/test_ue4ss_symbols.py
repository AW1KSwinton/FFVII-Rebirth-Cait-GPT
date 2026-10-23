import struct
import uuid

import pytest

from ff7r_agent import ue4ss_symbols as symbols


GUID = uuid.UUID("36f2b75d-00c1-43ee-99ea-2c78c88a0c6f")


def pe(guid=GUID, age=104):
    data = bytearray(1024)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", data, 0x86, 1)
    struct.pack_into("<H", data, 0x94, 240)
    optional = 0x98
    struct.pack_into("<H", data, optional, 0x20B)
    struct.pack_into("<II", data, optional + 112 + 6 * 8, 0x1000, 28)
    struct.pack_into("<IIII", data, optional + 240 + 8, 512, 0x1000, 512, 512)
    struct.pack_into("<I", data, 512 + 12, 2)
    struct.pack_into("<I", data, 512 + 24, 600)
    record = b"RSDS" + guid.bytes_le + struct.pack("<I", age) + b"E:\\Build\\UE4SS.pdb\0"
    data[600:600 + len(record)] = record
    return bytes(data)


def pdb(guid=GUID, age=104):
    data = bytearray(512 * 5)
    data[:32] = b"Microsoft C/C++ MSF 7.00\r\n\x1aDS\0\0\0"
    struct.pack_into("<6I", data, 32, 512, 1, 5, 16, 0, 2)
    struct.pack_into("<I", data, 512 * 2, 3)
    struct.pack_into("<4I", data, 512 * 3, 2, 0, 28, 4)
    struct.pack_into("<3I", data, 512 * 4, 20000404, 12345, age)
    data[512 * 4 + 12:512 * 4 + 28] = guid.bytes_le
    return bytes(data)


def test_pe_and_pdb_identities_use_guid_and_age():
    assert symbols.pe_identity(pe()) == {"guid": str(GUID), "age": 104, "pdb_path": r"E:\Build\UE4SS.pdb"}
    assert symbols.pdb_identity(pdb()) == {"guid": str(GUID), "age": 104}


@pytest.mark.parametrize("different", [pdb(age=1), pdb(guid=uuid.UUID(int=1))])
def test_mismatched_pdb_is_never_used_to_resolve_offsets(tmp_path, monkeypatch, different):
    dll, candidate = tmp_path / "UE4SS.dll", tmp_path / "UE4SS.pdb"
    dll.write_bytes(pe())
    candidate.write_bytes(different)
    monkeypatch.setattr(symbols, "_resolve", lambda *_: pytest.fail("mismatched symbols must never be loaded"))
    report = symbols.inspect(dll, candidate, [0x6A8C4F])
    assert report["symbols_match"] is False
    assert report["resolved"] is False
    assert report["reason"] == "pdb_identity_mismatch"


def test_matching_pdb_is_passed_to_the_resolver(tmp_path, monkeypatch):
    dll, candidate = tmp_path / "UE4SS.dll", tmp_path / "UE4SS.pdb"
    dll.write_bytes(pe())
    candidate.write_bytes(pdb())
    monkeypatch.setattr(symbols, "_resolve", lambda *_: [{"offset": "0x6a8c4f", "symbol": "example", "displacement": "0x1"}])
    assert symbols.inspect(dll, candidate, [0x6A8C4F])["resolved"] is True


@pytest.mark.parametrize("reader", [symbols.pe_identity, symbols.pdb_identity])
def test_wrong_file_types_are_rejected(reader):
    with pytest.raises(ValueError):
        reader(b"unrelated file")
