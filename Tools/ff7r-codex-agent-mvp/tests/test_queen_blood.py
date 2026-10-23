from __future__ import annotations

import unittest
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ff7r_agent.queen_blood import (
    QueenBloodError,
    QueenBloodState,
    plan_queen_blood_input,
    plan_queen_blood_mulligan_input,
)


def snapshot(**overrides):
    value = {
        "schema_version": 1,
        "active": True,
        "phase": "player_turn",
        "turn": 2,
        "match_id": "match-1",
        "cursor": {"area": "hand", "hand_index": 0, "row": 0, "column": 0},
        "hand": [
            {"hand_index": 0, "card_id": "cactuar", "cost": 1, "power": 2},
            {"hand_index": 1, "card_id": "tonberry", "cost": 2, "power": 5},
        ],
        "legal_moves": [
            {"hand_index": 0, "card_id": "cactuar", "row": 1, "column": 2, "score": 1},
            {"hand_index": 1, "card_id": "tonberry", "row": 2, "column": 4, "score": 2},
        ],
        "board": {"rows": 3, "columns": 5, "cells": []},
    }
    value.update(overrides)
    return value


class QueenBloodTests(unittest.TestCase):
    def test_policy_prefers_explicit_probe_score(self) -> None:
        state = QueenBloodState.from_dict(snapshot())
        move = state.choose_move()
        self.assertEqual(move.card_id, "tonberry")
        self.assertEqual(move.key(), (1, 2, 4))

    def test_card_catalogue_attributes_are_preserved(self) -> None:
        state = QueenBloodState.from_dict(snapshot(hand=[
            {
                "hand_index": 0,
                "card_id": "card_id_005",
                "name": "Toxirat",
                "cost": 2,
                "power": 2,
                "influence": "DownRight4",
                "influence_square_count": 2,
                "influence_offsets": [
                    {"row": 1, "column": 0},
                    {"row": 1, "column": 1},
                ],
                "note_id": "$cardtext_card_noAbility",
                "effect_id": "debuff_biorat",
                "effect_text_id": "$cardtext_card_Ability_BuffDebuff_OneShot_Down",
                "effect_influence": "DownRight1",
                "effect_influence_square_count": 1,
                "effect_influence_offsets": [{"row": 1, "column": 1}],
                "effect_constant": 0,
                "effect_target": 3,
                "effect_type": 0,
                "effect_int_parameters": [-3],
                "effect_text": "When played, lower the power of allied and enemy cards on affected tiles by 3.",
                "effect_text_source": "CardGameSpecialParam semantic template",
                "effect_type_name": "immediate_effect",
                "effect_target_name": "allied_and_enemy_cards",
                "effect_text_label_references": [],
                "effect_heading_types": [1, 2],
                "effect_parameter_references": [0, 5],
                "effect_reference_counts": [],
                "effect_filter_tags": [1, 9, 9, 9],
                "effect_reference_count0": 0,
            },
            {"hand_index": 1, "card_id": "card_id_036", "cost": 2, "power": 1},
        ]))
        card = state.hand[0]
        self.assertEqual((card.cost, card.power), (2, 2))
        self.assertEqual(card.influence, "DownRight4")
        self.assertEqual(card.influence_square_count, 2)
        self.assertEqual(card.influence_offsets, ((1, 0), (1, 1)))
        self.assertEqual(card.effect_id, "debuff_biorat")
        self.assertEqual(card.effect_text_id, "$cardtext_card_Ability_BuffDebuff_OneShot_Down")
        self.assertEqual(card.effect_influence, "DownRight1")
        self.assertEqual(card.effect_influence_square_count, 1)
        self.assertEqual(card.effect_influence_offsets, ((1, 1),))
        self.assertEqual(card.effect_constant, 0.0)
        self.assertEqual(card.effect_int_parameters, (-3,))
        self.assertEqual((card.effect_target, card.effect_type), (3, 0))
        self.assertEqual(card.effect_text, "When played, lower the power of allied and enemy cards on affected tiles by 3.")
        self.assertEqual(card.effect_text_source, "CardGameSpecialParam semantic template")
        self.assertEqual(card.effect_type_name, "immediate_effect")
        self.assertEqual(card.effect_target_name, "allied_and_enemy_cards")
        self.assertEqual(card.effect_heading_types, (1, 2))
        self.assertEqual(card.effect_parameter_references, (0, 5))
        self.assertEqual(card.effect_filter_tags, (1, 9, 9, 9))
        self.assertEqual(card.effect_reference_count0, 0)

    def test_input_plan_selects_card_then_places_it(self) -> None:
        state = QueenBloodState.from_dict(snapshot())
        steps = plan_queen_blood_input(state, state.legal_moves[1])
        self.assertEqual([step.button for step in steps], ["right", "confirm", "down", "down", "right", "right", "right", "right", "confirm"])

    def test_missing_cursor_fails_closed(self) -> None:
        state = QueenBloodState.from_dict(snapshot(cursor=None))
        with self.assertRaises(QueenBloodError):
            plan_queen_blood_input(state, state.legal_moves[0])

    def test_inactive_snapshot_cannot_choose_a_move(self) -> None:
        state = QueenBloodState.from_dict(snapshot(active=False, phase="inactive"))
        with self.assertRaises(QueenBloodError):
            state.choose_move()

    def test_incomplete_reflection_is_rejected_before_action_planning(self) -> None:
        value = snapshot()
        value.pop("legal_moves")
        with self.assertRaises(QueenBloodError):
            QueenBloodState.from_dict(value)

    def test_canonical_prematch_signal_is_preserved(self) -> None:
        state = QueenBloodState.from_dict(snapshot(
            active=False,
            phase="inactive",
            screen="pre_match",
            begin_match_available=True,
            match_screen=True,
            hand=[],
            legal_moves=[],
        ))
        self.assertEqual(state.screen, "pre_match")
        self.assertTrue(state.begin_match_available)
        self.assertTrue(state.match_screen)

    def test_mulligan_plan_selects_exactly_three_cards_and_submits(self) -> None:
        steps = plan_queen_blood_mulligan_input((0, 2, 4))
        self.assertEqual(
            [step.button for step in steps],
            [
                "queen_blood_mark",
                "queen_blood_right",
                "queen_blood_right",
                "queen_blood_mark",
                "queen_blood_right",
                "queen_blood_right",
                "queen_blood_mark",
                "queen_blood_play",
            ],
        )
        self.assertEqual(
            [step.reason for step in steps],
            [
                "mark_mulligan_card",
                "hover_mulligan_card",
                "hover_mulligan_card",
                "mark_mulligan_card",
                "hover_mulligan_card",
                "hover_mulligan_card",
                "mark_mulligan_card",
                "submit_mulligan",
            ],
        )
        self.assertEqual(
            [step.hand_index for step in steps if step.button == "queen_blood_mark"],
            [0, 2, 4],
        )

    def test_mulligan_plan_rejects_invalid_card_requests(self) -> None:
        for indices in ((0, 1, 1), (0, 1, 5), (0, 1, 2, 3, 4, 5)):
            with self.subTest(indices=indices), self.assertRaises(QueenBloodError):
                plan_queen_blood_mulligan_input(indices)

    def test_mulligan_plan_can_keep_the_opening_hand(self) -> None:
        steps = plan_queen_blood_mulligan_input(())
        self.assertEqual(
            [(step.button, step.reason) for step in steps],
            [("queen_blood_play", "submit_mulligan")],
        )

    def test_mulligan_plan_can_select_all_five_cards(self) -> None:
        steps = plan_queen_blood_mulligan_input((0, 1, 2, 3, 4))
        self.assertEqual(sum(step.button == "queen_blood_mark" for step in steps), 5)
        self.assertEqual(steps[-1].button, "queen_blood_play")
        self.assertEqual(steps[-1].reason, "submit_mulligan")


if __name__ == "__main__":
    unittest.main()
