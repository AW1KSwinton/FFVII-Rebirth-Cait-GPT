"""Card abilities in the Queen's Blood rules model (queen_blood_ai.BoardModel)."""
from __future__ import annotations

from ff7r_agent.queen_blood import QueenBloodCard
from ff7r_agent.queen_blood_ai import BoardModel, _Cell


def _board(cards: dict | None = None) -> BoardModel:
    """Empty board with rank-1 pawns in each side's home column."""
    model = BoardModel()
    for row in range(3):
        for column in range(5):
            model.cells[(row, column)] = _Cell("empty", 0, False)
        model.cells[(row, 0)] = _Cell("player", 1, False)
        model.cells[(row, 4)] = _Cell("opponent", 1, False)
    for (row, column), (owner, card) in (cards or {}).items():
        model.cells[(row, column)] = _Cell(owner, 0, True, card.power, 0, False, card, card.power)
    model._recompute_lanes()
    return model


def _card(card_id="c", power=2, cost=1, influence=(), effect=None, target=None, params=(),
          effect_offsets=(), texts=()) -> QueenBloodCard:
    return QueenBloodCard(
        hand_index=0, card_id=card_id, name=card_id, cost=cost, power=power,
        influence_offsets=tuple(influence), effect_type_name=effect, effect_target_name=target,
        effect_int_parameters=tuple(params), effect_influence_offsets=tuple(effect_offsets),
        effect_text_parameters=tuple(texts),
    )


def test_while_in_play_aura_reaches_cards_placed_later_and_ends_with_its_source() -> None:
    # Magic Pot style: +2 to allied cards on its diagonals while in play.
    pot = _card("pot", power=1, effect="while_in_play_effect", target="allied_cards", params=(2,),
                effect_offsets=((1, 1),))
    model = _board({(0, 0): ("player", pot)})
    model.cells[(1, 1)] = _Cell("player", 1, False)
    after = model.place(_card("wolf", power=2), 1, 1)
    assert after.cells[(1, 1)].power == 4 and after.player_lanes[1] == 4
    # Destroying the pot removes the +2 again.
    bomb = _card("bomb", power=1, effect="immediate_effect", target="enemy_cards", params=(-99,),
                 effect_offsets=((0, 4),))
    after.cells[(0, 4)] = _Cell("opponent", 1, False)
    blown = after.place(bomb, 0, 4, side="opponent")  # mirrored: hits (0, 0)
    assert blown.cells[(0, 0)].card is False
    assert blown.cells[(1, 1)].power == 2 and blown.player_lanes[1] == 2


def test_count_power_raises_per_other_enfeebled_allied_card() -> None:
    # Valron (0, 2): "Raise power by 2 for each other enfeebled allied card".
    valron = _card("valron", power=3, effect="card_count_power_change", target="allied_cards", params=(0, 2))
    weak = _card("weak", power=3)
    model = _board({(0, 0): ("opponent", valron), (1, 0): ("opponent", weak), (2, 0): ("opponent", weak)})
    model.cells[(1, 0)].power = 1  # enfeebled
    model._recompute_lanes()
    assert model.effective_power((0, 0)) == 5
    assert model.opponent_lanes[0] == 5


def test_add_card_to_hand_credits_the_named_cards() -> None:
    trio = _card("trio", power=1, cost=2, effect="add_card_to_hand", params=(1, 1),
                 texts=("card_id_Token_035", "card_id_Token_036"))
    model = _board()
    model.cells[(1, 0)] = _Cell("player", 2, False)
    after = model.place(trio, 1, 0)
    assert after.hand_additions["player"] == ["card_id_Token_035", "card_id_Token_036"]
    assert after.value() > model.place(_card("plain", power=1, cost=2), 1, 0).value()


def test_played_and_destroyed_card_watchers_gain_power() -> None:
    watcher = _card("sea_devil", power=2, effect="card_set_power_change", target="allied_cards", params=(1,))
    avenger = _card("tonberry", power=2, effect="card_destroyed_power_change", target="allied_cards", params=(2,))
    model = _board({(0, 1): ("player", watcher), (2, 1): ("player", avenger), (1, 1): ("player", _card("x", 1))})
    after = model.place(_card("new", power=1), 1, 0)
    assert after.cells[(0, 1)].power == 3  # an allied card was played
    killer = _card("k", power=1, effect="immediate_effect", target="enemy_cards", params=(-5,),
                   effect_offsets=((0, 3),))
    killed = after.place(killer, 1, 4, side="opponent")  # mirrored: hits (1, 1)
    assert killed.cells[(1, 1)].card is False
    assert killed.cells[(2, 1)].power == 4  # an allied card was destroyed


def test_self_destroyed_effect_fires_on_its_tiles() -> None:
    pie = _card("pie", power=1, effect="self_destroyed_effect", target="allied_cards", params=(3,),
                effect_offsets=((1, 0),))
    friend = _card("friend", power=2)
    model = _board({(0, 1): ("player", pie), (1, 1): ("player", friend)})
    killer = _card("k", power=1, effect="immediate_effect", target="enemy_cards", params=(-99,),
                   effect_offsets=((0, 3),))
    after = model.place(killer, 0, 4, side="opponent")
    assert after.cells[(0, 1)].card is False and after.cells[(1, 1)].power == 5


def test_first_time_threshold_trigger_fires_once() -> None:
    # Cloud (7, 0, 2): when its power first reaches 7, +2 to allies around.
    cloud = _card("cloud", power=5, effect="triggered_power_change", target="allied_cards", params=(7, 0, 2),
                  effect_offsets=((1, 0),))
    ally = _card("ally", power=1)
    model = _board({(0, 1): ("player", cloud), (1, 1): ("player", ally)})
    buff = _card("buff", power=1, effect="immediate_effect", target="allied_cards", params=(2,),
                 effect_offsets=((0, 1),))
    after = model.place(buff, 0, 0)
    assert after.cells[(0, 1)].power == 7 and after.cells[(1, 1)].power == 3
    again = after.place(buff, 2, 0)
    assert again.cells[(1, 1)].power == 3  # fired only once


def test_sacrifice_card_replaces_an_ally_and_uses_its_power() -> None:
    grandhorn = _card("grandhorn", power=1, effect="sacrifice_for_allied_effect", target="allied_cards",
                      effect_offsets=((1, 0),))
    model = _board({(0, 1): ("player", _card("fodder", power=4)), (1, 1): ("player", _card("ally", power=2))})
    assert model.legal_cells(grandhorn) == [(0, 1), (1, 1)]
    after = model.place(grandhorn, 0, 1)
    assert after.cells[(0, 1)].card_ref.card_id == "grandhorn"
    assert after.cells[(1, 1)].power == 6
