import json
from dataclasses import asdict
from pathlib import Path

import pytest

from ff7r_agent.models import Character, ValidationError


def recorded_player():
    return json.loads((Path(__file__).parent / "fixtures/measured-capsule-pose.json").read_text(encoding="utf-8"))


def test_measured_capsule_pose_round_trip_and_legacy_compatibility():
    record = recorded_player()
    player = Character.from_dict(record)
    assert player.position.z - player.locomotion.feet_position.z == 88.0
    assert player.locomotion.grounded is True
    assert player.locomotion.movement_mode is None
    assert Character.from_dict(asdict(player)) == player
    del record["locomotion"]
    assert Character.from_dict(record).locomotion is None


@pytest.mark.parametrize("mutation", [
    lambda r: r["locomotion"]["feet_position"].update(x=r["position"]["x"] + 100),
    lambda r: r["locomotion"].update(capsule_half_height_cm=10),
    lambda r: r["locomotion"].update(reference="guessed_floor"),
    lambda r: r["locomotion"].update(grounded="true"),
    lambda r: r["locomotion"]["feet_position"].update(z=float("nan")),
    lambda r: r.update(actor_id=None),
])
def test_invalid_measured_pose_is_rejected(mutation):
    record = recorded_player()
    mutation(record)
    with pytest.raises(ValidationError):
        Character.from_dict(record)
