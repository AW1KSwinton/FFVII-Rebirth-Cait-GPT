"""Offline contract and request tests; no real game, process or input backend."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from ff7r_agent import combat
from ff7r_agent.combat import CombatObservationError, CombatSnapshot, observe_combat
from ff7r_agent.models import GameState, ValidationError
from ff7r_agent.runtime_commands import invoke_runtime
from ff7r_agent.service import AgentService


def actor(actor_id="Cloud_1", **changes):
    return {"actor_id": actor_id, "chara_id": None, "player_type": 0 if actor_id == "Cloud_1" else None,
            "hp": 100, "max_hp": 100, "mp": None, "max_mp": None, "atb": None,
            "distance_m": 0, "controller_state": 1, "staggered": None, "pressured": None,
            "status_effects": None, "errors": [], **changes}


def snapshot(**changes):
    return {"schema_version": 1, "producer_contract": combat.PRODUCER_CONTRACT, "request_id": "a" * 32,
            "source": combat.SOURCE, "source_sequence": 101,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(), "timestamp_precision": "seconds",
            "map_id": "EndGame", "world_id": "World_1", "controlled_actor_id": "Cloud_1",
            "observed": True, "in_battle": True, "phase": "active", "paused": False,
            "party": [actor()], "enemies": [actor("enemy_1", distance_m=3.5)],
            "complete": {"party": True, "enemies": True}, "target": {"observed": True, "actor_id": "enemy_1"},
            "errors": [], "input_sent": False, "input_authorized": False, **changes}


def query_fixture(tmp_path, monkeypatch, *, mutate=None, after_mutate=None, answer=True):
    clock = [0.0]
    monkeypatch.setattr(combat.time, "monotonic", lambda: clock[0])
    tokens = {1: "process-created-1"}
    base = {"source": "FF7RTelemetryExporter", "source_sequence": 100, "running": True,
            "telemetry": {"live": True, "age_seconds": 0.01}, "map_id": "EndGame", "world_id": "World_1",
            "timestamp_utc": datetime.now(timezone.utc).isoformat(), "paused": False,
            "in_combat": True, "combat_state_observed": True,
            "player": {"actor_id": "Cloud_1"}}
    requests = []

    def sleep(seconds):
        clock[0] += seconds
        if answer:
            marker = (tmp_path / "combat-query.request").read_text(encoding="ascii")
            request_id, _ = marker.split("|")
            if request_id in requests:
                return
            requests.append(request_id)
            response = snapshot(request_id=request_id, source_sequence=base["source_sequence"] + 1)
            if mutate:
                mutate(response)
            (tmp_path / f"combat-query.{request_id}.json").write_text(json.dumps(response))
            base["source_sequence"] += 2
            if after_mutate:
                after_mutate(base, tokens)

    service = SimpleNamespace(
        telemetry=SimpleNamespace(path=tmp_path / "state.json"),
        _environment=SimpleNamespace(game_process_ids=lambda: {1: "ff7rebirth_.exe"},
                                     game_process_identity_tokens=lambda _: dict(tokens)),
        observe=lambda: deepcopy(base), _sleeper=sleep,
    )
    return service, requests, base


def test_one_query_returns_coherent_readonly_data_and_cleans_only_its_artifacts(tmp_path, monkeypatch):
    service, requests, _ = query_fixture(tmp_path, monkeypatch)
    unrelated = tmp_path / ("combat-query." + "b" * 32 + ".json")
    unrelated.write_text("old response")
    result = observe_combat(service)
    assert result["ok"] and result["available"]
    assert result["identity"]["process_token"] == "1:process-created-1"
    assert result["combat"]["target_id"] == "enemy_1"
    assert result["combat"]["party"][0]["atb"] is None
    assert result["battle_instance_id"] is result["command_ui"] is result["result"] is None
    assert result["input_sent"] is result["input_authorized"] is False
    assert result["schema_version"] == 1 and result["producer_contract"] == combat.PRODUCER_CONTRACT
    assert not (tmp_path / "combat-query.request").exists()
    assert not (tmp_path / f"combat-query.{requests[0]}.json").exists()
    assert unrelated.read_text() == "old response"


@pytest.mark.parametrize("atb", [None, 0, 0.0, 1.5])
def test_unknown_atb_is_distinct_from_an_observed_zero(atb):
    result = CombatSnapshot.from_dict(snapshot(party=[actor(atb=atb)])).to_dict()
    assert result["party"][0]["atb"] == atb
    assert (result["party"][0]["atb"] is None) == (atb is None)


@pytest.mark.parametrize("change", [
    lambda p: p.update(schema_version=True), lambda p: p.update(schema_version=2),
    lambda p: p.update(producer_contract="old"), lambda p: p.update(source="generic_feed"),
    lambda p: p.update(source_sequence=True), lambda p: p.update(observed=1),
    lambda p: p.update(in_battle=1), lambda p: p.update(phase="result"),
    lambda p: p.update(paused="false"), lambda p: p.update(input_sent=True),
    lambda p: p.update(complete={"party": 1, "enemies": True}),
    lambda p: p["party"][0].update(hp=-1), lambda p: p["party"][0].update(mp=True),
    lambda p: p["party"][0].update(hp=101), lambda p: p["party"][0].update(atb=float("nan")),
    lambda p: p["party"][0].update(hp=10**400), lambda p: p["party"][0].update(controller_state=40),
    lambda p: p["party"][0].update(status_effects="Poison"),
    lambda p: p["party"][0].update(status_effects=["Poison", "Poison"]),
    lambda p: p["party"][0].pop("atb"),
    lambda p: p.update(party=[actor()] * 17), lambda p: p.update(enemies=[actor("e" + str(i)) for i in range(65)]),
    lambda p: p.update(enemies=[actor()]), lambda p: p.update(controlled_actor_id="Tifa"),
    lambda p: p.update(target={"observed": False, "actor_id": "enemy_1"}),
])
def test_invalid_or_conflicting_data_is_refused_without_filling_from_old_samples(change):
    payload = snapshot()
    change(payload)
    with pytest.raises(CombatObservationError):
        CombatSnapshot.from_dict(payload)


def test_incomplete_enemy_roster_does_not_imply_battle_end():
    result = CombatSnapshot.from_dict(snapshot(enemies=[], complete={"party": True, "enemies": False})).to_dict()
    assert result["in_battle"] is True and result["phase"] == "active" and not result["enemies_complete"]


def test_zero_hp_and_explicitly_empty_statuses_remain_observations():
    result = CombatSnapshot.from_dict(snapshot(enemies=[actor("enemy_1", hp=0, status_effects=[])])).to_dict()
    assert result["enemies"][0]["hp"] == 0 and result["enemies"][0]["status_effects"] == []
    assert result["phase"] == "active"


def test_paused_battle_does_not_invent_a_command_menu(tmp_path, monkeypatch):
    service, _, _ = query_fixture(tmp_path, monkeypatch, mutate=lambda p: p.update(paused=True))
    result = observe_combat(service)
    assert result["combat"]["paused"] is True and result["command_ui"] is None


def test_observed_inactive_phase_is_not_a_victory_receipt(tmp_path, monkeypatch):
    def inactive(p):
        p.update(in_battle=False, phase="inactive", party=[], enemies=[], complete={"party": False, "enemies": False},
                 target={"observed": False, "actor_id": None})
    service, _, _ = query_fixture(tmp_path, monkeypatch, mutate=inactive)
    result = observe_combat(service)
    assert result["ok"] and result["combat"]["phase"] == "inactive"
    assert result["result"] is None and "won" not in result


def test_outside_combat_uses_only_base_presence_and_never_opens_query_files(tmp_path, monkeypatch):
    service, requests, base = query_fixture(tmp_path, monkeypatch)
    base.update(in_combat=False, world_id=None)
    marker = tmp_path / "combat-query.request"
    marker.write_text("another owner's request")
    original_open = combat.Path.open
    def no_combat_io(path, *args, **kwargs):
        assert not path.name.startswith("combat-query"), "inactive observer performed combat I/O"
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(combat.Path, "open", no_combat_io)
    service._sleeper = lambda _: pytest.fail("inactive observer waited for a combat reply")
    result = observe_combat(service)
    assert result["ok"] and result["observation_kind"] == "presence" and result["request_id"] is None
    assert result["producer_contract"] == combat.PRESENCE_CONTRACT
    assert result["combat"]["phase"] == "inactive" and result["combat"]["world_id"] is None
    assert result["combat"]["paused"] is None
    assert result["combat"]["party"] == result["combat"]["enemies"] == []
    assert result["result"] is None and requests == []
    assert marker.exists()  # A peer's request is neither consulted nor cleaned.


@pytest.mark.parametrize("observed, active", [(None, False), (False, False), (1, True),
                                             ("true", True), (True, None), (True, 1)])
def test_unknown_or_legacy_battle_presence_creates_no_combat_query(tmp_path, monkeypatch, observed, active):
    service, requests, base = query_fixture(tmp_path, monkeypatch)
    base.update(combat_state_observed=observed, in_combat=active)
    service._environment.game_process_ids = lambda: pytest.fail("unknown battle presence caused process enumeration")
    result = observe_combat(service)
    assert result["reason"] == "combat_state_unavailable" and result["combat"] is None
    assert not (tmp_path / "combat-query.request").exists() and requests == []


def test_entry_exit_and_reentry_require_fresh_rosters_only_while_active(tmp_path, monkeypatch):
    service, requests, base = query_fixture(tmp_path, monkeypatch)
    base["in_combat"] = False
    assert observe_combat(service)["combat"]["phase"] == "inactive" and requests == []
    base["in_combat"] = True
    assert observe_combat(service)["combat"]["party"][0]["hp"] == 100
    base["in_combat"] = False
    assert observe_combat(service)["combat"]["party"] == [] and len(requests) == 1
    base["in_combat"] = True
    assert observe_combat(service)["combat"]["phase"] == "active"
    assert len(requests) == 2 and requests[0] != requests[1]


@pytest.mark.parametrize("answer", [False, True])
def test_exit_during_pending_query_returns_base_presence_and_releases_request(tmp_path, monkeypatch, answer):
    service, _, base = query_fixture(tmp_path, monkeypatch, answer=answer)
    original = service._sleeper
    sleeps = []
    def leave_battle(seconds):
        sleeps.append(seconds)
        original(seconds)
        base.update(in_combat=False, source_sequence=102)
    service._sleeper = leave_battle
    result = observe_combat(service)
    assert result["ok"] and result["observation_kind"] == "presence"
    assert result["combat"]["phase"] == "inactive" and result["combat"]["enemies"] == []
    assert len(sleeps) == 1 and not (tmp_path / "combat-query.request").exists()
    assert list(tmp_path.glob("combat-query.*.json")) == []


def test_lost_presence_during_pending_query_stops_waiting_and_discards_rosters(tmp_path, monkeypatch):
    service, _, _ = query_fixture(tmp_path, monkeypatch, after_mutate=lambda b, _: b.update(combat_state_observed=False))
    result = observe_combat(service)
    assert result["reason"] == "combat_state_unavailable" and result["combat"] is None
    assert not (tmp_path / "combat-query.request").exists()


@pytest.mark.parametrize("active", [False, True])
def test_base_parser_preserves_explicit_observed_battle_state(active):
    payload = {"schema_version": 1, "sequence": 1, "timestamp_utc": datetime.now(timezone.utc).isoformat(),
               "game": {"running": True, "combat_state_observed": True, "in_combat": active}}
    state = GameState.from_dict(payload)
    assert state.in_combat is active and state.to_dict()["combat_state_observed"] is True
    payload["game"].pop("combat_state_observed")
    legacy = GameState.from_dict(payload)
    assert legacy.combat_state_observed is None and "combat_state_observed" not in legacy.to_dict()


@pytest.mark.parametrize("observed, active", [(1, False), ("true", True), (True, "false"),
                                             (True, 1), (True, None)])
def test_base_parser_rejects_untyped_or_missing_observed_battle_state(observed, active):
    payload = {"schema_version": 1, "sequence": 1, "timestamp_utc": datetime.now(timezone.utc).isoformat(),
               "game": {"running": True, "combat_state_observed": observed, "in_combat": active}}
    with pytest.raises(ValidationError):
        GameState.from_dict(payload)


@pytest.mark.parametrize("change, reason", [
    (lambda p: p.update(request_id="b" * 32), "combat_request_identity_mismatch"),
    (lambda p: p.update(source_sequence=100), "combat_sequence_not_advanced"),
    (lambda p: p.update(timestamp_utc=(datetime.now(timezone.utc) - timedelta(seconds=4)).isoformat()), "combat_snapshot_stale"),
    (lambda p: p.update(timestamp_utc=(datetime.now(timezone.utc) + timedelta(seconds=4)).isoformat()), "combat_snapshot_stale"),
    (lambda p: p.update(timestamp_utc="2026-10-14T00:00:00"), "combat_timestamp_invalid"),
    (lambda p: p.update(map_id="other"), "combat_identity_changed"),
    (lambda p: p.update(world_id="other"), "combat_world_changed"),
])
def test_stale_wrong_request_or_wrong_world_replies_never_become_available(tmp_path, monkeypatch, change, reason):
    service, _, _ = query_fixture(tmp_path, monkeypatch, mutate=change)
    result = observe_combat(service)
    assert not result["ok"] and result["reason"] == reason and result["combat"] is None
    assert not (tmp_path / "combat-query.request").exists()


@pytest.mark.parametrize("change", [lambda b, t: t.update({1: "reused-pid"}),
                                   lambda b, t: b.update(map_id="other"),
                                   lambda b, t: b["player"].update(actor_id="Tifa")])
def test_load_process_reuse_or_controlled_character_change_invalidates_query(tmp_path, monkeypatch, change):
    service, _, _ = query_fixture(tmp_path, monkeypatch, after_mutate=change)
    assert observe_combat(service)["reason"] == "combat_identity_changed"


def test_missing_battle_getter_returns_unavailable_not_inactive(tmp_path, monkeypatch):
    def unknown(p):
        p.update(observed=False, in_battle=None, phase="unknown", party=[], enemies=[],
                 complete={"party": False, "enemies": False}, target={"observed": False, "actor_id": None},
                 errors=["battle_state_unavailable"])
    service, _, _ = query_fixture(tmp_path, monkeypatch, mutate=unknown)
    result = observe_combat(service)
    assert result["reason"] == "combat_state_unavailable" and result["combat"] is None


def test_a_future_sequence_without_a_matching_base_publication_is_refused(tmp_path, monkeypatch):
    service, _, _ = query_fixture(tmp_path, monkeypatch, mutate=lambda p: p.update(source_sequence=100000))
    result = observe_combat(service, 0.1)
    assert result["reason"] == "combat_sequence_unbound" and result["combat"] is None


def test_response_published_before_the_native_base_waits_for_its_sequence(tmp_path, monkeypatch):
    service, requests, base = query_fixture(tmp_path, monkeypatch, after_mutate=lambda b, _: b.update(source_sequence=100))
    original = service._sleeper
    original_identity = combat.process_identity
    identity_checks = []
    def identity(environment):
        identity_checks.append(1)
        return original_identity(environment)
    monkeypatch.setattr(combat, "process_identity", identity)
    def publish(seconds):
        already_answered = bool(requests)
        original(seconds)
        if already_answered:
            base["source_sequence"] = 101
    service._sleeper = publish
    result = observe_combat(service)
    assert result["ok"] and result["base_source_sequence"] == 101 and len(requests) == 1
    assert len(identity_checks) == 2  # No OS process polling on each wait tick.


def test_an_existing_request_is_never_overwritten_or_cleaned(tmp_path, monkeypatch):
    service, _, _ = query_fixture(tmp_path, monkeypatch)
    marker = tmp_path / "combat-query.request"
    marker.write_text("another owner's request")
    assert observe_combat(service)["reason"] == "combat_query_busy"
    assert marker.read_text() == "another owner's request"


def test_oversized_response_is_rejected_before_decoding(tmp_path, monkeypatch):
    service, _, _ = query_fixture(tmp_path, monkeypatch, mutate=lambda p: p.update(padding="x" * combat.MAX_RESPONSE_BYTES))
    assert observe_combat(service)["reason"] == "combat_response_too_large"


def test_field_feed_failure_does_not_create_a_request(tmp_path, monkeypatch):
    from ff7r_agent.telemetry import TelemetryError
    service, _, _ = query_fixture(tmp_path, monkeypatch)
    def lost_feed():
        raise TelemetryError("synthetic telemetry loss")
    service.observe = lost_feed
    assert observe_combat(service)["reason"] == "combat_telemetry_unavailable"
    assert not (tmp_path / "combat-query.request").exists()


@pytest.mark.parametrize("field, value", [("player", []), ("telemetry", "missing"),
                                           ("telemetry", {"live": True, "age_seconds": 10**400})])
def test_malformed_base_observations_are_unavailable(tmp_path, monkeypatch, field, value):
    service, _, base = query_fixture(tmp_path, monkeypatch)
    base[field] = value
    assert observe_combat(service)["ok"] is False
    assert not (tmp_path / "combat-query.request").exists()


def test_no_response_is_bounded_and_late_old_reply_cannot_satisfy_next_query(tmp_path, monkeypatch):
    service, requests, _ = query_fixture(tmp_path, monkeypatch, answer=False)
    assert observe_combat(service, 0.1)["reason"] == "combat_sampler_timeout"
    assert not (tmp_path / "combat-query.request").exists()
    (tmp_path / ("combat-query." + "a" * 32 + ".json")).write_text(json.dumps(snapshot()))
    assert observe_combat(service, 0.1)["reason"] == "combat_sampler_timeout"


@pytest.mark.parametrize("timeout", [True, 0, 8.1, float("nan"), "3", 10**400])
def test_invalid_timeout_creates_no_probe_request(tmp_path, monkeypatch, timeout):
    service, _, _ = query_fixture(tmp_path, monkeypatch)
    with pytest.raises(CombatObservationError, match="combat_timeout_invalid"):
        observe_combat(service, timeout)
    assert not (tmp_path / "combat-query.request").exists()


@pytest.mark.parametrize("fault", ["simulation", "stale", "not_running", "missing_actor"])
def test_unverified_base_never_requests_new_telemetry(tmp_path, monkeypatch, fault):
    service, _, base = query_fixture(tmp_path, monkeypatch)
    if fault == "simulation": base["source"] = "simulation"
    elif fault == "stale": base["telemetry"]["age_seconds"] = 4
    elif fault == "not_running": base["running"] = False
    else: base["player"]["actor_id"] = None
    service._environment.game_process_ids = lambda: pytest.fail("unverified base caused process enumeration")
    assert observe_combat(service)["ok"] is False
    assert not (tmp_path / "combat-query.request").exists()


def test_registered_service_call_uses_observer_only(tmp_path, monkeypatch):
    fixture, _, _ = query_fixture(tmp_path, monkeypatch)
    service = AgentService.__new__(AgentService)
    service.__dict__.update(fixture.__dict__)
    result = invoke_runtime(service, None, None, "observe_combat")
    assert result["ok"] and result["input_sent"] is False
