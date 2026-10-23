"""Read and answer the field dialogue wheel (TalkMenu_00_C) by answer text.

The Queen's Blood probe's dialogue scope publishes the wheel natively: whether
it is shown, each Answer cell's layout and Cursor_00's rotation, which points
at the highlighted answer (0 degrees up, clockwise: live, 280 on the left
answer and 100 on the right). The cells carry no text, so each answer's words
come from the menu-text reader (``TalkMenu_00.AnswerN.Txt_00``). A hidden
wheel keeps its old texts, so only cells the probe reports shown count.
"""
from __future__ import annotations

import math
import re
from typing import Any

from .shop import read_screen_texts

ANSWER_TEXT_PATH = re.compile(r"TalkMenu_00\.WidgetTree\.Answer(\d+)\.WidgetTree\.Txt_00$")
QUESTION_TEXT_PATH = "TalkMenu_00.WidgetTree.Txt_Question"
# The cursor must point within this many degrees of the answer it selects,
# and clearly closer to it than to any other shown answer.
MAX_CURSOR_ERROR_DEGREES = 50.0
MIN_CURSOR_MARGIN_DEGREES = 20.0


class DialogueChoiceError(RuntimeError):
    pass


def _number(value: Any) -> float | None:
    return float(value) if type(value) in (int, float) and math.isfinite(value) else None


def _position(layout: dict[str, Any] | None) -> tuple[float, float] | None:
    """Screen position of a cell: its slot offset plus its render translation."""
    if not isinstance(layout, dict):
        return None
    parts = [_number(layout.get(key)) for key in ("left", "top", "x", "y")]
    if all(part is None for part in parts):
        return None
    left, top, x, y = (part or 0.0 for part in parts)
    return left + x, top + y


def _angle_between(a: float, b: float) -> float:
    return abs((a - b + 180.0) % 360.0 - 180.0)


def normalise(text: str) -> str:
    """Casefold and drop quotes, ellipses and spacing so "On second thought..."
    matches "on second thought"."""
    text = text.casefold().replace("…", "...")
    text = re.sub(r"[\"'‘’“”.!?,]", " ", text)
    return " ".join(text.split())


def wheel_from(talk_menu: Any, texts: list[dict[str, Any]]) -> dict[str, Any]:
    """Combine the probe's wheel and the menu texts into the answers shown,
    with their bearings and which one the cursor highlights."""
    wheel = talk_menu if isinstance(talk_menu, dict) else {}
    by_index: dict[int, str] = {}
    question = None
    for item in texts:
        path = str(item.get("path") or "")
        match = ANSWER_TEXT_PATH.search(path)
        if match:
            by_index[int(match.group(1))] = str(item.get("text") or "")
        elif path.endswith(QUESTION_TEXT_PATH):
            question = str(item.get("text") or "")
    shown = bool(wheel.get("observed") and wheel.get("shown"))
    answers = []
    for cell in wheel.get("answers") or []:
        # Live, all six cells report shown on a two-answer wheel; only the
        # cells the game gave text are answers.
        index = _number(cell.get("index")) if isinstance(cell, dict) else None
        if index is None or cell.get("shown") is False or not by_index.get(int(index), "").strip():
            continue
        answers.append({"index": int(index), "text": by_index[int(index)], "position": _position(cell)})
    positioned = [answer for answer in answers if answer["position"] is not None]
    centre = _position(wheel.get("cursor"))
    if centre is None and positioned:
        centre = (sum(a["position"][0] for a in positioned) / len(positioned),
                  sum(a["position"][1] for a in positioned) / len(positioned))
    for answer in answers:
        if answer["position"] is None or centre is None:
            answer["bearing"] = None
            continue
        dx, dy = answer["position"][0] - centre[0], answer["position"][1] - centre[1]
        answer["bearing"] = (math.degrees(math.atan2(dx, -dy)) % 360.0
                             if math.hypot(dx, dy) > 1e-6 else None)
    cursor_angle = _number((wheel.get("cursor") or {}).get("angle"))
    highlighted = None
    highlight_reason = None
    bearings = [a for a in answers if a.get("bearing") is not None]
    if not shown:
        highlight_reason = "wheel_not_shown"
    elif cursor_angle is None:
        highlight_reason = "cursor_angle_unknown"
    elif len(bearings) != len(answers) or not answers:
        highlight_reason = "answer_layout_unknown"
    else:
        ranked = sorted(bearings, key=lambda a: _angle_between(a["bearing"], cursor_angle % 360.0))
        best = _angle_between(ranked[0]["bearing"], cursor_angle % 360.0)
        runner = _angle_between(ranked[1]["bearing"], cursor_angle % 360.0) if len(ranked) > 1 else 360.0
        if best > MAX_CURSOR_ERROR_DEGREES:
            highlight_reason = "cursor_points_at_no_answer"
        elif runner - best < MIN_CURSOR_MARGIN_DEGREES:
            highlight_reason = "cursor_between_answers"
        else:
            highlighted = ranked[0]["index"]
    return {"shown": shown, "question": question if shown else None, "answers": answers,
            "cursor_angle": cursor_angle, "highlighted": highlighted,
            "highlight_reason": highlight_reason}


def match_answer(wheel: dict[str, Any], answer: str | None = None, index: int | None = None) -> dict[str, Any]:
    """The one shown answer named by ``index`` or by its (normalised) text:
    an exact match, else the only answer containing it."""
    answers = wheel.get("answers") or []
    if index is not None:
        found = [a for a in answers if a.get("index") == index]
    else:
        wanted = normalise(answer or "")
        if not wanted:
            raise DialogueChoiceError("dialogue_answer_required")
        found = [a for a in answers if a.get("text") is not None and normalise(a["text"]) == wanted]
        if not found:
            found = [a for a in answers if a.get("text") is not None and wanted in normalise(a["text"])]
    if len(found) != 1:
        raise DialogueChoiceError("dialogue_answer_not_found" if not found else "dialogue_answer_ambiguous")
    return found[0]


def step_toward(current: dict[str, Any], target: dict[str, Any]) -> str:
    """The arrow that moves the cursor from ``current`` toward ``target``:
    along whichever screen axis separates them most."""
    (cx, cy), (tx, ty) = current["position"], target["position"]
    dx, dy = tx - cx, ty - cy
    if abs(dx) >= abs(dy):
        return "dialogue_right" if dx > 0 else "dialogue_left"
    return "down" if dy > 0 else "up"


# Live 2026-10-20 (Mt. Nibel, "Review the materia tutorial?"): consecutive
# reads of a wheel left open alternated shown / empty, so one empty read is
# not a closed wheel. It counts as gone only after this many reads in a row.
WHEEL_READS = 4
WHEEL_READ_GAP_SECONDS = 0.3


def read_wheel(service: Any) -> dict[str, Any]:
    """A fresh native read of the wheel plus its texts; no input. An empty
    read is repeated up to WHEEL_READS times before the wheel counts as gone."""
    request = getattr(service.telemetry, "request_queen_blood_dialogue", None)
    if not callable(request):
        raise DialogueChoiceError("dialogue_wheel_probe_unavailable")
    wheel: dict[str, Any] = {}
    for attempt in range(WHEEL_READS):
        if attempt:
            service._sleeper(WHEEL_READ_GAP_SECONDS)
        if request() is False:
            raise DialogueChoiceError("dialogue_wheel_probe_request_failed")
        talk_menu = service._queen_blood_talk_wheel(service._queen_blood_observe())
        if talk_menu is None:
            # The probe answers the request on its own timer; give it one beat.
            service._sleeper(WHEEL_READ_GAP_SECONDS)
            talk_menu = service._queen_blood_talk_wheel(service._queen_blood_observe())
        texts = read_screen_texts(service, "TalkMenu_00")["texts"]
        wheel = wheel_from(talk_menu, texts)
        wheel["probe"] = talk_menu
        wheel["reads"] = attempt + 1
        if wheel["shown"] and wheel["answers"]:
            return wheel
    return wheel
