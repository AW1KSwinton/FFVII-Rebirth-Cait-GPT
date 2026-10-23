#!/usr/bin/env python3
"""Generate the offline Queen's Blood Lua join used by the UE4SS probe.

This keeps the runtime lookup small while preserving the authored CardGame
tables.  The generator intentionally accepts the extracted CSV directory as an
argument; no game process or asset mutation is involved.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path
from typing import Any


TYPE_NAMES = {
    0: "immediate_effect",
    1: "while_in_play_effect",
    2: "lane_win_score_bonus",
    3: "add_card_to_hand",
    4: "lane_win_card_spawn",
    5: "lane_loss_card_spawn",
    6: "triggered_power_change",
    7: "token_power_change",
    9: "card_set_power_change",
    11: "card_destroyed_power_change",
    14: "self_destroyed_effect",
    16: "self_destroyed_add_card",
    18: "flag_triggered_card_spawn",
    22: "flag_overwrite_power_change",
    25: "round_end_score_rule",
    26: "round_end_card_spawn",
    27: "sacrifice_for_allied_effect",
    28: "sacrifice_for_enemy_effect",
    29: "additional_flag_effect",
    30: "card_count_power_change",
}

TARGET_NAMES = {
    0: "event_or_no_board_target",
    1: "allied_cards",
    2: "enemy_cards",
    3: "allied_and_enemy_cards",
}

TOKEN_NAMES = {
    "card_id_Token_003": "Diamond Dust (power 2)",
    "card_id_Token_004": "Diamond Dust (power 4)",
    "card_id_Token_005": "Diamond Dust (power 6)",
    "card_id_Token_006": "Grangalan Junior",
    "card_id_Token_008": "Resurrected Amalgam",
    "card_id_Token_015": "Mandragora Minion",
    "card_id_Token_020": "Moogle",
    "card_id_Token_021": "The Tiny Bronco",
    "card_id_Token_031": "Hype Johnny (level 1)",
    "card_id_Token_032": "Hype Johnny (level 2)",
    "card_id_Token_033": "Hype Johnny (level 3)",
    "card_id_Token_035": "Moogle Mage",
    "card_id_Token_036": "Moogle Bard",
    "card_id_Token_037": "Heatseeker Minion",
}

EXACT_TEXT = {
    "textonly_MythrilGolem": "Possesses a crystal-hard resolve and an overwhelming amount of power.",
    "textonly_DebuChocobo": "Creates a surfeit of positions around it when played.",
    "textonly_Mogsaboton": "Makes use of its adorable nature to enliven the battlefield.",
    "textonly_SkyWheel": "Adds positions in the shape of a certain Gold Saucer attraction.",
    "textonly_Lufus": "Creates an intimidatingly large number of positions around it when played.",
    "GetAllScore": "If this card is in play when the round ends, the loser of each lane's score is added to the victor's.",
}


def integer(value: str) -> int | None:
    value = (value or "").strip()
    if not value:
        return None
    return int(value)


def number(value: str) -> int | float | None:
    value = (value or "").strip()
    if not value:
        return None
    result = float(value)
    return int(result) if result.is_integer() else result


def nonempty(values: list[str]) -> list[str]:
    return [value.strip() for value in values if value.strip()]


def main_card_name_map(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    source = path.read_text(encoding="utf-8")
    return {
        f"card_id_{match.group(1)}": match.group(3)
        for match in re.finditer(
            r'\["Card_(\d{3})"\]\s*=\s*\{.*?name_id\s*=\s*"([^\"]+)".*?name\s*=\s*"([^\"]+)"',
            source,
        )
    }


def fallback_name(card_id: str, name_id: str) -> str:
    if card_id in TOKEN_NAMES:
        return TOKEN_NAMES[card_id]
    if "QueensSlave" in card_id:
        return "Queen's Slave"
    value = name_id.removeprefix("$")
    value = re.sub(r"^(?:en_|M_SMN_|name_|cardtext_card_name_)", "", value)
    value = re.sub(r"_(?:Standard|StandardEnd2|StandardEnd|atk\d+|Poison|KalmFangEnd2)$", "", value)
    value = value.replace("_", " ").strip()
    return value or card_id


def read_special_params(path: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.reader(stream):
            if not row or row[0] == "Index" or len(row) < 37:
                continue
            result[row[1]] = {
                "label_refs": nonempty(row[2:4]),
                "text_params": nonempty(row[4:10]),
                "int_params": [int(value) for value in nonempty(row[10:16])],
                "heading_types": [int(value) for value in nonempty(row[16:19])],
                "parameter_refs": [int(value) for value in nonempty(row[19:22])],
                "reference_counts": [int(value) for value in nonempty(row[22:25])],
                "filter_tags": [int(value) for value in nonempty(row[25:30])],
                "reference_text_id": row[30].strip() or None,
                "text_id": row[31].strip() or None,
                "effect_influence": row[32].strip() or None,
                "constant": number(row[33]),
                "target": integer(row[34]),
                "type": integer(row[35]),
                "reference_count0": integer(row[36]),
            }
    return result


def read_influence_masks(path: Path) -> dict[str, list[dict[str, int]]]:
    rows_by_pattern: dict[str, list[list[int]]] = {}
    with path.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.reader(stream):
            if not row or row[0] == "Index" or len(row) < 7:
                continue
            name = row[1].strip()
            base, _, _variant = name.rpartition("_")
            if not base:
                continue
            # The export places Column1 after Column5 in the header. Restore
            # the authored left-to-right order before decoding the 5x5 mask.
            cells = [row[6], row[2], row[3], row[4], row[5]]
            rows_by_pattern.setdefault(base, []).append([int(value or 0) for value in cells])
    masks: dict[str, list[dict[str, int]]] = {}
    for base, matrix in rows_by_pattern.items():
        if len(matrix) < 5:
            continue
        masks[base] = [
            {"row": matrix_row - 2, "column": column - 2}
            for matrix_row, values in enumerate(matrix[:5])
            for column, value in enumerate(values[:5])
            if value
        ]
    return masks


def influence_offsets(name: str | None, masks: dict[str, list[dict[str, int]]]) -> list[dict[str, int]] | None:
    if not name:
        return None
    offsets = masks.get(name)
    return None if offsets is None else offsets


def target_phrase(target: int | None) -> str:
    return {
        1: "allied cards",
        2: "enemy cards",
        3: "allied and enemy cards",
    }.get(target, "cards")


def affected_suffix(param: dict[str, Any]) -> str:
    return " on affected tiles" if param.get("effect_influence") else ""


def token_phrase(ids: list[str], names: dict[str, str]) -> str:
    values = [names.get(card_id, card_id) for card_id in ids]
    if not values:
        return "a card"
    if len(values) == 1:
        return values[0]
    return ", ".join(values[:-1]) + " and " + values[-1]


def effect_text(card_id: str, effect_id: str | None, param: dict[str, Any] | None, names: dict[str, str]) -> tuple[str, str]:
    if not param:
        return "No ability.", "Samurai Gamers ability list"
    if effect_id in EXACT_TEXT:
        return EXACT_TEXT[effect_id], "Samurai Gamers ability list"
    effect_type = param.get("type")
    target = param.get("target")
    values = param.get("int_params", [])
    amount = abs(values[-1]) if values else None
    suffix = affected_suffix(param)
    subject = target_phrase(target)
    direction = "raise" if (values and values[-1] >= 0) else "lower"
    if effect_type == 0 and effect_id and effect_id.lower().startswith("kill"):
        return f"When played, destroy {subject}{suffix}.", "CardGameSpecialParam semantic template"
    if effect_type == 0 and amount is not None and param.get("text_id"):
        verb = "raise" if "_Up" in param["text_id"] else "lower"
        return f"When played, {verb} the power of {subject}{suffix} by {amount}.", "CardGameSpecialParam semantic template"
    if effect_type == 1 and amount is not None:
        verb = "raise" if "_Up" in (param.get("text_id") or "") else "lower"
        return f"{verb.title()} the power of {subject}{suffix} by {amount} while this card is in play.", "CardGameSpecialParam semantic template"
    if effect_type == 2 and values:
        return f"When you win the lane, receive a score bonus of {values[0]}.", "CardGameSpecialParam semantic template"
    if effect_type in {3, 4, 5, 16, 18, 26}:
        ids = param.get("text_params", [])
        phrase = token_phrase(ids, names)
        if effect_type == 16:
            return f"When this card is destroyed, add {phrase} to your hand.", "CardGameSpecialParam semantic template"
        if effect_type in {18, 26}:
            return f"When triggered, spawn {phrase} in empty positions.", "CardGameSpecialParam semantic template"
        return f"When played, add {phrase} to your hand.", "CardGameSpecialParam semantic template"
    if effect_type == 6 and amount is not None:
        if "Cloud" in (effect_id or ""):
            trigger = f"when this card's power first reaches {values[0]}" if values else "when triggered"
        elif "DebuffTrigger" in (param.get("text_id") or ""):
            trigger = "the first time this card is enfeebled"
        else:
            trigger = "the first time this card is enhanced"
        return f"When {trigger}, {direction} the power of {subject}{suffix} by {amount}.", "CardGameSpecialParam semantic template"
    if effect_type == 9:
        return "Changes card power when the relevant card is played.", "CardGameSpecialParam semantic template"
    if effect_type == 11 and amount is not None:
        return f"When {subject} are destroyed, raise this card's power by {amount}.", "CardGameSpecialParam semantic template"
    if effect_type == 14 and amount is not None:
        verb = "raise" if "_Up" in (param.get("text_id") or "") else "lower"
        return f"When this card is destroyed, {verb} the power of {subject}{suffix} by {amount}.", "CardGameSpecialParam semantic template"
    if effect_type in {27, 28}:
        verb = "raise" if effect_type == 27 else "lower"
        return f"Replace an ally, then {verb} the power of {subject}{suffix} by the replaced ally's power.", "CardGameSpecialParam semantic template"
    if effect_type == 29:
        return "When played, apply an additional board effect.", "CardGameSpecialParam semantic template"
    if effect_type == 30 and amount is not None:
        verb = "raise" if "_Buff_" in (effect_id or "") else "lower"
        return f"{verb.title()} power by {amount} for each relevant {subject[:-1] if subject.endswith('s') else subject}.", "CardGameSpecialParam semantic template"
    return "Special effect; see the authored parameters and influence mask.", "CardGameSpecialParam semantic template"


def lua(value: Any) -> str:
    if value is None:
        return "nil"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        if value and all(isinstance(item, dict) and set(item) == {"row", "column"} for item in value):
            return "{ " + ", ".join(lua(item) for item in value) + " }"
        return "{" + ", ".join(lua(item) for item in value) + "}"
    if isinstance(value, dict) and set(value) == {"row", "column"}:
        return "{ row = %s, column = %s }" % (value["row"], value["column"])
    raise TypeError(f"cannot serialize {value!r}")


def entry(row: dict[str, str], special: dict[str, dict[str, Any]], masks: dict[str, list[dict[str, int]]], names: dict[str, str], *, auxiliary: bool) -> tuple[str, dict[str, Any]]:
    card_id = row["Name"]
    param = special.get(row.get("Special", "")) if row.get("Special") else None
    main_name = names.get(card_id)
    display_name = main_name or fallback_name(card_id, row.get("NameID", ""))
    card: dict[str, Any] = {
        "card_id": card_id,
        "unique_id": integer(row.get("UniqueId", "")),
        "name_id": row.get("NameID") or None,
        "name": display_name,
        "cost": integer(row.get("MP", "")),
        "power": integer(row.get("Power", "")),
        # CardGameCardInfo.Num: copies a player owns once the card is obtained
        # (2 for standard cards, 1 for legendary ones).
        "copies": integer(row.get("Num", "")),
        "key_item": row.get("KeyItem") or None,
        "influence": row.get("Infl") or None,
        "influence_square_count": None,
        "influence_offsets": None,
        "note_id": row.get("Note") or None,
        "effect_id": row.get("Special") or None,
        "effect_text_id": param.get("text_id") if param else None,
        "effect_influence": param.get("effect_influence") if param else None,
        "effect_influence_square_count": None,
        "effect_influence_offsets": None,
        "effect_constant": param.get("constant") if param else None,
        "effect_target": param.get("target") if param else None,
        "effect_type": param.get("type") if param else None,
        "effect_int_parameters": param.get("int_params") if param else None,
        "effect_text_parameters": param.get("text_params") if param else None,
        "effect_reference_text_id": param.get("reference_text_id") if param else None,
        "effect_text": None,
        "effect_text_source": None,
        "effect_type_name": TYPE_NAMES.get(param.get("type")) if param else None,
        "effect_target_name": TARGET_NAMES.get(param.get("target")) if param else None,
        "effect_text_label_references": param.get("label_refs") if param else None,
        "effect_heading_types": param.get("heading_types") if param else None,
        "effect_parameter_references": param.get("parameter_refs") if param else None,
        "effect_reference_counts": param.get("reference_counts") if param else None,
        "effect_filter_tags": param.get("filter_tags") if param else None,
        "effect_reference_count0": param.get("reference_count0") if param else None,
    }
    card_offsets = influence_offsets(row.get("Infl"), masks)
    card["influence_offsets"] = card_offsets
    card["influence_square_count"] = len(card_offsets) if card_offsets is not None else None
    if param:
        effect_offsets = influence_offsets(param.get("effect_influence"), masks)
        card["effect_influence_offsets"] = effect_offsets
        card["effect_influence_square_count"] = len(effect_offsets) if effect_offsets is not None else None
        card["effect_text"], card["effect_text_source"] = effect_text(card_id, row.get("Special"), param, names | TOKEN_NAMES)
    elif not row.get("Special"):
        card["effect_text"] = "No ability."
        card["effect_text_source"] = "Samurai Gamers ability list"
    if auxiliary:
        card["catalogue_kind"] = "token" if card_id.startswith("card_id_Token_") else "auxiliary"
    return card_id, card


IMAGE_COLUMNS = ("ImageID", "ImageBoardID", "ThumbnailID")


def image_card_index(rows: list[dict[str, str]]) -> dict[str, str]:
    """Card art name -> card id, for resolving live widget art to a card.

    Art names do not follow card ids (U_Com_Billboard_CardGame_Card_Token_003
    is card_id_Token_005) and some art is shared, so the index is built from
    the rows: dummy/test rows are skipped, a main card_id_NNN row wins a
    shared image over token and gimmick rows, and otherwise the row the
    image is named after wins. Art alone cannot tell the other sharers apart.
    """
    index: dict[str, str] = {}
    playable = [
        row for row in rows
        if not row["Name"].startswith("dummy_") and not row["Name"].endswith("_test")
    ]
    ordered = sorted(playable, key=lambda row: 0 if re.fullmatch(r"card_id_\d{3}", row["Name"]) else 1)
    for row in ordered:
        for column in IMAGE_COLUMNS:
            image = (row.get(column) or "").strip()
            if not image:
                continue
            current = index.get(image)
            # Among rows sharing art (Token_003/004/005 all use
            # Card_Token_003), prefer the one the art is named after.
            if current is None or (
                not re.fullmatch(r"card_id_\d{3}", current)
                and not image.endswith(current.removeprefix("card_id_"))
                and image.endswith(row["Name"].removeprefix("card_id_"))
            ):
                index[image] = row["Name"]
    return index


def format_entry(key: str, card: dict[str, Any]) -> str:
    fields = ", ".join(f"{name} = {lua(value)}" for name, value in card.items())
    return f"    [{lua(key)}] = {{ {fields} }},"


def generate(resident_root: Path, output: Path) -> None:
    with (resident_root / "CardGameCardInfo.csv").open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    special = read_special_params(resident_root / "CardGameSpecialParam.csv")
    masks = read_influence_masks(resident_root / "CardGameCardInfl.csv")
    names = main_card_name_map(output)
    main = [row for row in rows if re.fullmatch(r"card_id_\d{3}", row["Name"])]
    auxiliary = [row for row in rows if row not in main]
    names.update({row["Name"]: TOKEN_NAMES[row["Name"]] for row in auxiliary if row["Name"] in TOKEN_NAMES})
    lines = [
        "-- Generated from CardGameCardInfo.csv + CardGameSpecialParam.csv +",
        "-- CardGameCardInfl.csv + Text/US/Resident_TxtRes.uasset.",
        "--",
        "-- cost/power are CardGameCardInfo.MP/Power. influence and note_id",
        "-- are the card row's Infl/Note fields. Influence offsets are the",
        "-- CardGameCardInfl 5x5 masks centered on the played card.",
        "-- effect_text is a player-facing English guide join: exact guide text",
        "-- where available, otherwise a semantic rendering of authored params.",
        "-- auxiliary_cards contains token/gimmick rows referenced by effects.",
        "-- image_cards maps card art (ImageID/ImageBoardID/ThumbnailID) to card ids.",
        "return {",
    ]
    for row in main:
        key, card = entry(row, special, masks, names, auxiliary=False)
        lines.append(format_entry("Card_" + key[-3:], card))
    lines.append("    auxiliary_cards = {")
    for row in auxiliary:
        key, card = entry(row, special, masks, names, auxiliary=True)
        lines.append(format_entry(key, card))
    lines.append("    },")
    lines.append("    image_cards = {")
    for image, card_id in sorted(image_card_index(rows).items()):
        lines.append(f"        [{lua(image)}] = {lua(card_id)},")
    lines.extend(["    },", "}", ""])
    output.write_text("\n".join(lines), encoding="utf-8", newline="\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--resident-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    generate(args.resident_root, args.output)


if __name__ == "__main__":
    main()
