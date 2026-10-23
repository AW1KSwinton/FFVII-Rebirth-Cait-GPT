"""Review captured Save telemetry offline; never call the game or generate a layout."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def payload(value):
    for _ in range(8):
        if not isinstance(value, dict):
            raise ValueError("capture payload must be an object")
        if value.get("ok") is False or value.get("isError") is True:
            raise ValueError("capture contains a failed response")
        if isinstance(value.get("structuredContent"), dict):
            value = value["structuredContent"]
        elif isinstance(value.get("result"), dict):
            value = value["result"]
        elif isinstance(value.get("content"), list):
            blocks = [item["text"] for item in value["content"]
                      if isinstance(item, dict) and item.get("type") == "text"]
            if len(blocks) != 1:
                raise ValueError("capture text envelope is ambiguous")
            value = json.loads(blocks[0])
        else:
            return value
    raise ValueError("capture envelope nesting limit exceeded")


def review(path):
    raw = Path(path).read_bytes()
    data = payload(json.loads(raw))
    report = {"capture": str(Path(path).resolve()), "sha256": hashlib.sha256(raw).hexdigest(),
              "source_sequence": data.get("source_sequence"),
              "screen": data.get("screen"), "texts": data.get("texts", []),
              "layout_generated": False}
    for key in ("save_cells", "save_dialogs"):
        records = data.get(key)
        if not isinstance(records, list):
            report[key] = {"available": False}
            continue
        items = []
        for item in records:
            if not isinstance(item, dict) or not isinstance(item.get("nodes"), list):
                raise ValueError("invalid widget evidence")
            nodes = [{key: node.get(key) for key in
                      ("name", "class", "kind", "text", "visible", "keyboard_focus", "hovered")}
                     for node in item["nodes"] if isinstance(node, dict)]
            items.append({"id": item.get("id"), "class": item.get("class"), "node_count": len(nodes),
                          "cap_reached": len(nodes) >= 48,
                          "visible_focused_nodes": [node for node in nodes
                              if node["visible"] is True and node["keyboard_focus"] is True],
                          "visible_numeric_texts": [node for node in nodes
                              if node["visible"] is True and isinstance(node["text"], str)
                              and node["text"].strip().isascii() and node["text"].strip().isdigit()],
                          "nodes": nodes})
        report[key] = {"available": True, "count": len(items), "items": items}
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("captures", nargs="+", type=Path)
    args = parser.parse_args()
    print(json.dumps([review(path) for path in args.captures], indent=2))


if __name__ == "__main__":
    main()
