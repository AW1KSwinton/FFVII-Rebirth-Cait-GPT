import hashlib
import json
import pytest
from ff7r_agent.save_slot_save import SaveSlotWriter, SaveCalibrationResolver
from test_working_save import game, calibration_game, CALIBRATION, LAYOUT


def prepared(game, monkeypatch, tmp_path):
    calibration_game(game)
    result = SaveSlotWriter(game).run(15, True, calibrate_prompt=CALIBRATION)
    assert result["state"] == "save_calibration_prompt_observed"
    path = game._queen_blood_runs.root / "save-operation-lock.json"
    raw = path.read_bytes()
    layout = tmp_path / "layout.json"
    layout.write_text(json.dumps(LAYOUT.__dict__))
    monkeypatch.setenv("FF7R_SAVE_WRITE_LAYOUT", str(layout))
    original = game._map_menu_key
    def key(button, ms):
        if game.state == "prompt" and button == "confirm" and not game.yes:
            game.keys.append(button)
            game.state = "save_list"
        elif game.state == "prompt" and button == "down":
            game.keys.append(button)
            game.yes = not game.yes
        else:
            original(button, ms)
    game._map_menu_key = key
    game.keys.clear()
    return path, dict(expected_token=json.loads(raw)["token"], expected_sha256=hashlib.sha256(raw).hexdigest())


def test_dry_run_no_input_or_lock_mutation(game, monkeypatch, tmp_path):
    path, args = prepared(game, monkeypatch, tmp_path)
    before = path.read_bytes()
    result = SaveCalibrationResolver(game).run(**args)
    assert result["ok"] and not game.keys and path.read_bytes() == before


@pytest.mark.parametrize("yes", [False, True])
def test_cancel_only_no_and_archive_exact_lock(game, monkeypatch, tmp_path, yes):
    path, args = prepared(game, monkeypatch, tmp_path)
    game.yes = yes
    result = SaveCalibrationResolver(game).run(**args, execute=True)
    assert result["ok"] and result["state"] == "save_calibration_cancelled"
    assert game.state == "field" and not game.writes and not path.exists()
    assert game.keys.count("confirm") == 1
    assert json.loads(__import__("pathlib").Path(result["archive"]).read_text())["lock"]["token"] == args["expected_token"]


@pytest.mark.parametrize("change", ["hash", "token", "legacy", "target", "protected", "identity"])
def test_changes_refused_without_input(game, monkeypatch, tmp_path, change):
    path, args = prepared(game, monkeypatch, tmp_path)
    if change == "hash": args["expected_sha256"] = "0" * 64
    elif change == "token": args["expected_token"] = "0" * 32
    elif change == "legacy":
        value = json.loads(path.read_text()); value.pop("review_binding"); path.write_text(json.dumps(value))
        args["expected_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    elif change == "target": game.path(15).write_bytes(b"changed")
    elif change == "protected": game.path(14).write_bytes(b"changed")
    else: game._environment.tokens[42] = "different-process"
    result = SaveCalibrationResolver(game).run(**args, execute=True)
    assert not result["ok"] and result["requires_review"] and path.exists() and not game.keys


def test_no_focus_not_observed_never_confirms_yes(game, monkeypatch, tmp_path):
    path, args = prepared(game, monkeypatch, tmp_path)
    game.yes = True
    game._map_menu_key = lambda button, ms: game.keys.append(button)
    result = SaveCalibrationResolver(game).run(**args, execute=True)
    assert not result["ok"] and game.keys == ["down"] and path.exists() and not game.writes


def test_existing_resolution_claim_blocks_replay(game, monkeypatch, tmp_path):
    path, args = prepared(game, monkeypatch, tmp_path)
    (path.parent / "save-calibration-resolution-lock.json").write_text("{}")
    result = SaveCalibrationResolver(game).run(**args, execute=True)
    assert not result["ok"] and not game.keys and path.exists()


def test_lock_race_after_read_stops_before_input(game, monkeypatch, tmp_path):
    path, args = prepared(game, monkeypatch, tmp_path)
    game.before_read = lambda: path.write_text("{}")
    result = SaveCalibrationResolver(game).run(**args, execute=True)
    assert not result["ok"] and not game.keys and path.exists()


def test_post_cancel_file_change_retains_lock_and_sends_no_more_input(game, monkeypatch, tmp_path):
    path, args = prepared(game, monkeypatch, tmp_path)
    original = game._map_menu_key
    def key(button, ms):
        original(button, ms)
        if button == "confirm": game.path(14).write_bytes(b"unexpected-write")
    game._map_menu_key = key
    result = SaveCalibrationResolver(game).run(**args, execute=True)
    assert not result["ok"] and game.keys == ["confirm"] and path.exists()


def test_prompt_still_visible_does_not_repeat_confirmation(game, monkeypatch, tmp_path):
    path, args = prepared(game, monkeypatch, tmp_path)
    game._map_menu_key = lambda button, ms: game.keys.append(button)
    result = SaveCalibrationResolver(game).run(**args, execute=True)
    assert not result["ok"] and game.keys == ["confirm"] and path.exists()


def test_no_focus_changed_during_final_identity_guard_never_confirms_yes(game, monkeypatch, tmp_path):
    path, args = prepared(game, monkeypatch, tmp_path)
    resolver = SaveCalibrationResolver(game)
    original = resolver._guard
    def guard():
        original()
        if resolver.screens and resolver.screens[-1]['stage'] == 'resolution_verify_no':
            game.yes = True
    monkeypatch.setattr(resolver, '_guard', guard)
    result = resolver.run(**args, execute=True)
    assert not result['ok'] and not game.writes
    assert 'confirm' not in game.keys and path.exists()


def test_replaced_resolution_claim_is_preserved_before_confirm(game, monkeypatch, tmp_path):
    path, args = prepared(game, monkeypatch, tmp_path)
    resolver = SaveCalibrationResolver(game)
    original = resolver.read
    claim = path.parent / 'save-calibration-resolution-lock.json'
    foreign = b'{"token":"foreign-review-claim"}'
    def read(stage):
        result = original(stage)
        if stage == 'resolution_final_no': claim.write_bytes(foreign)
        return result
    monkeypatch.setattr(resolver, 'read', read)
    result = resolver.run(**args, execute=True)
    assert result['decision'] == 'save_resolution_claim_changed'
    assert not game.keys and not game.writes and path.exists() and claim.read_bytes() == foreign


def test_archive_retains_exact_original_lock_and_resolution_claim_bytes(game, monkeypatch, tmp_path):
    import base64
    from pathlib import Path
    path, args = prepared(game, monkeypatch, tmp_path)
    raw = path.read_bytes()
    result = SaveCalibrationResolver(game).run(**args, execute=True)
    assert result['ok']
    archive = json.loads(Path(result['archive']).read_text())
    assert base64.b64decode(archive['lock_raw_base64']) == raw
    claim = json.loads(base64.b64decode(archive['claim_raw_base64']))
    assert claim['token'] == args['expected_token'] and claim['lock_sha256'] == args['expected_sha256']
    assert len(claim['resolution_id']) == 32


def test_menu_ui_changed_during_close_guard_sends_no_blind_menu_key(game, monkeypatch, tmp_path):
    path, args = prepared(game, monkeypatch, tmp_path)
    resolver = SaveCalibrationResolver(game)
    original = resolver._guard
    def guard():
        original()
        if resolver.screens and resolver.screens[-1]['stage'] == 'resolution_close_verify':
            game.state = 'field'
    monkeypatch.setattr(resolver, '_guard', guard)
    result = resolver.run(**args, execute=True)
    assert not result['ok'] and result['decision'] == 'save_resolution_close_ui_changed'
    assert game.keys == ['confirm'] and path.exists() and not game.writes


def test_blank_field_telemetry_retains_lock_and_resolution_claim(game, monkeypatch, tmp_path):
    path, args = prepared(game, monkeypatch, tmp_path)
    original = game.observe
    game.observe = lambda: {'in_menu':False,'telemetry':{'live':False}} if game.state == 'field' else original()
    result = SaveCalibrationResolver(game).run(**args, execute=True)
    assert not result['ok'] and result['decision'] == 'save_resolution_ui_not_closed'
    assert path.exists() and (path.parent/'save-calibration-resolution-lock.json').exists() and not game.writes


def test_archive_failure_preserves_both_review_locks_after_no_cancellation(game, monkeypatch, tmp_path):
    path, args = prepared(game, monkeypatch, tmp_path)
    archive = path.parent/'save-operation-reviews'/(args['expected_token']+'.json')
    archive.parent.mkdir(); archive.write_bytes(b'prior-reviewed-evidence')
    result = SaveCalibrationResolver(game).run(**args, execute=True)
    assert not result['ok'] and result['requires_review']
    assert path.exists() and (path.parent/'save-calibration-resolution-lock.json').exists()
    assert archive.read_bytes() == b'prior-reviewed-evidence' and game.keys.count('confirm') == 1 and not game.writes


def test_partial_metadata_cleanup_keeps_save_and_assignment_mutations_blocked(game, monkeypatch, tmp_path):
    from pathlib import Path
    path, args = prepared(game, monkeypatch, tmp_path)
    claim = path.parent/'save-calibration-resolution-lock.json'
    unlink = Path.unlink
    def fail_claim(target, *values, **options):
        if target == claim: raise OSError('simulated sharing refusal')
        return unlink(target,*values,**options)
    monkeypatch.setattr(Path,'unlink',fail_claim)
    result = SaveCalibrationResolver(game).run(**args, execute=True)
    assert not result['ok'] and result['requires_review'] and claim.exists() and not path.exists()
    game.keys.clear()
    refused = SaveSlotWriter(game).run(15, True)
    assert not refused['ok'] and refused['requires_review'] and not game.keys and not game.writes
    assert refused['decision'] == 'save_calibration_resolution_requires_review_or_is_running'
    with pytest.raises(ValueError,match='save_calibration_resolution_requires_review'):
        with game._queen_blood_runs.mutation_lock(): pass


def test_archive_changed_during_final_guard_retains_original_and_claim(game, monkeypatch, tmp_path):
    path, args = prepared(game, monkeypatch, tmp_path)
    archive = path.parent/'save-operation-reviews'/(args['expected_token']+'.json')
    resolver = SaveCalibrationResolver(game)
    original = resolver._guard
    def guard():
        original()
        if archive.exists(): archive.write_bytes(b'changed-archive')
    monkeypatch.setattr(resolver,'_guard',guard)
    result = resolver.run(**args, execute=True)
    assert result['decision'] == 'save_resolution_archive_changed'
    assert path.exists() and (path.parent/'save-calibration-resolution-lock.json').exists() and not game.writes
