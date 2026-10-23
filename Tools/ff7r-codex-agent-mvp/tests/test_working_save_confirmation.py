"""Synthetic race injection at the final Save snapshots; no live input."""
import copy
import json
import os

import pytest

from ff7r_agent.save_slot_save import SaveSlotWriter
from test_working_save import game, write


@pytest.mark.parametrize("stage,slot", [
    ("final_save_row_after_file_guards", 9),
    ("final_save_prompt_after_file_guards", 8),
])
@pytest.mark.parametrize("change", ["target", "protected", "owner", "process"])
def test_change_during_final_snapshot_never_receives_confirm(game, monkeypatch, stage, slot, change):
    read = SaveSlotWriter.read

    def raced(self, current):
        screen = read(self, current)
        if current == stage:
            if change in {"target", "protected"}:
                game.path(slot if change == "target" else 14).write_bytes(b"external-race")
            elif change == "process":
                game._environment.tokens[42] = "replacement-token"
            else:
                monkeypatch.setattr(game._queen_blood_runs, "owner", lambda *args: "Claude:other-model")
        return screen

    monkeypatch.setattr(SaveSlotWriter, "read", raced)
    result = write(game, slot)
    assert not result["ok"] and not result["saved"] and not game.writes
    forbidden = "open_save_prompt" if slot == 9 else "save_prompt:verified_yes"
    assert not any(p["why"] == forbidden for p in result["presses"])
    assert result["requires_review"]
    assert (game._queen_blood_runs.root / "save-operation-lock.json").exists()


@pytest.mark.parametrize("change", ["no_selected", "target_changed", "dialog_missing"])
def test_yes_evidence_is_read_after_slow_guards_and_lock_publication(game, monkeypatch, change):
    from ff7r_agent import save_slot_save
    publish = save_slot_save._write

    def publish_then_move(path, value):
        publish(path, value)
        if value.get("phase") == "confirming_save":
            if change == "no_selected":
                game.yes = False
            elif change == "target_changed":
                game.row = 13
            else:
                game.state = "save_list"

    monkeypatch.setattr(save_slot_save, "_write", publish_then_move)
    result = write(game)
    assert not result["ok"] and not game.writes and result["requires_review"]
    assert not any(p["why"] == "save_prompt:verified_yes" for p in result["presses"])


@pytest.mark.parametrize("stage", ["final_save_row_after_file_guards", "final_save_prompt_after_file_guards"])
@pytest.mark.parametrize("change", ["replay", "same_sequence", "wrong_source", "missing_request"])
def test_every_save_layout_requires_fresh_confirmation_frames(game, monkeypatch, stage, change):
    # The legacy keyboard-focus layout must have the same frame fences as a
    # native selected-child layout. Synthetic transport IDs are not UI proof.
    read, snapshot, previous = SaveSlotWriter.read, game.snapshot, {}

    def checked(self, current):
        def frame():
            response = snapshot()
            if current == stage:
                if change == "replay":
                    return copy.deepcopy(previous)
                if change == "same_sequence":
                    response["source_sequence"] = previous["source_sequence"]
                if change == "wrong_source":
                    response["source"] = "unverified"
                if change == "missing_request":
                    response.pop("request_id")
            return response
        game.snapshot = frame
        try:
            screen = read(self, current)
            previous.clear()
            previous.update(copy.deepcopy(screen.response))
            return screen
        finally:
            game.snapshot = snapshot

    monkeypatch.setattr(SaveSlotWriter, "read", checked)
    result = write(game)
    assert result["decision"] == "save_frame_stale_or_invalid"
    forbidden = "open_save_prompt" if "row" in stage else "save_prompt:verified_yes"
    assert not any(p["why"] == forbidden for p in result["presses"])
    assert not game.writes and not result["saved"]


def test_timestamp_change_alone_is_not_a_verified_save_write(game):
    press = game._map_menu_key

    def touch_only(button, milliseconds):
        if game.state == "prompt" and game.yes and button == "confirm":
            game.keys.append(button)
            os.utime(game.path(8), (1790000000, 1790000000))
            game.state = "save_list"
        else:
            press(button, milliseconds)

    game._map_menu_key = touch_only
    result = write(game)
    assert result["decision"] == "save_write_unverified" and result["requires_review"]
    assert not result["saved"] and not game.writes
    assert game.path(8).read_bytes() == b"original-8"
    lock = json.loads((game._queen_blood_runs.root / "save-operation-lock.json").read_text())
    assert lock["phase"] == "confirming_save" and lock["retain"]


@pytest.mark.parametrize("interruption", ["missing", "empty", "reverted"])
def test_write_stability_requires_consecutive_content_proofs(game, monkeypatch, interruption):
    from ff7r_agent import save_slot_save
    prove = save_slot_save.file_proof
    before = prove(game.path(8))
    reads = []

    def interrupted(path):
        proof = prove(path)
        if path == game.path(8) and game.writes:
            reads.append(proof)
            if len(reads) == 2:
                return {"missing": None, "empty": {**proof, "size": 0}, "reverted": before}[interruption]
        return proof

    monkeypatch.setattr(save_slot_save, "file_proof", interrupted)
    result = write(game)
    assert result["saved"] and game.writes == [8]
    # Changed read, interrupted read, two consecutive changed reads, then
    # the post-UI identity read. A separated pair must not finish sooner.
    assert len(reads) >= 5
