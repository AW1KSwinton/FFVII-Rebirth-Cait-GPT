"""Extract the exact installed-build mod interface without changing the SDK checkout."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

COMMIT = "c0a8d4cb259dccb8d7b58aec8b89e51c97222f9b"
FILES = (
    "UE4SS/include/Mod/CppUserModBase.hpp",
    "deps/first/String/include/String/StringType.hpp",
)


def main() -> None:
    repository, destination = map(Path, sys.argv[1:3])
    actual = subprocess.check_output(["git", "-C", str(repository), "rev-parse", COMMIT]).decode().strip()
    if actual != COMMIT:
        raise RuntimeError("Installed-build interface commit did not resolve exactly")
    records = []
    for name in FILES:
        source = subprocess.check_output(["git", "-C", str(repository), "show", f"{COMMIT}:{name}"])
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(source)
        records.append({"path": name, "sha256": hashlib.sha256(source).hexdigest()})
    manifest = {"commit": COMMIT, "source_repository": str(repository), "files": records}
    (destination / "interface-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest))


if __name__ == "__main__":
    main()
