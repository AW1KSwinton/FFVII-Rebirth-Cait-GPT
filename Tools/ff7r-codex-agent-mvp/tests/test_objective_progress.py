import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from ff7r_agent.navigation_values import MissionError
from ff7r_agent.objective_progress import read_objective_progress

FLAG = 'stfStory2350_GOLDS_ch12_step030_010'


def fixture(tmp_path, *, change=None, payload_change=None):
    catalog = tmp_path / 'StoryFlag.csv'
    catalog.write_text('Index,Name\n0,' + FLAG + '\n')
    identity = {1: 'game-created-1'}
    base = {'source_sequence': 100, 'map_id': 'EndGame', 'telemetry': {'live': True},
            'player': {'actor_id': 'Cloud_1'}}
    def answer(_):
        request_id, _, flags = (tmp_path / 'objective-query.request').read_text().split('|')
        payload = {'schema_version': 1, 'request_id': request_id, 'source': 'ue4ss_objective_progress',
                   'source_sequence': 101, 'timestamp_utc': datetime.now(timezone.utc).isoformat(),
                   'map_id': 'EndGame', 'player_actor_id': 'Cloud_1', 'observed': True,
                   'flags': {FLAG: False} if flags != '-' else {}, 'chapter_progress': 12, 'errors': {}}
        if payload_change:
            payload_change(payload)
        (tmp_path / 'objective-query.json').write_text(json.dumps(payload))
        if change:
            change(base, identity)
    service = SimpleNamespace(telemetry=SimpleNamespace(path=tmp_path / 'state.json'),
                              _environment=SimpleNamespace(game_process_ids=lambda: {1: 'ff7rebirth_.exe'},
                                                           game_process_identity_tokens=lambda _: identity.copy()),
                              observe=lambda: base.copy(), _sleeper=answer)
    return service, catalog


def test_false_flag_is_preserved_as_an_observed_value_and_request_is_released(tmp_path):
    service, catalog = fixture(tmp_path)
    result = read_objective_progress(service, [FLAG], flag_catalog_path=catalog)
    assert result['ok'] and result['flags'] == {FLAG: False}
    assert result['identity']['process_token'] == '1:game-created-1'
    assert result['input_sent'] is False and result['input_authorized'] is False
    assert not (tmp_path / 'objective-query.request').exists()


@pytest.mark.parametrize('flags', [[FLAG, FLAG], ['missing'], ['x|y'], [None], 'x', [FLAG] * 257])
def test_invalid_or_undeclared_flags_never_create_a_sampler_request(tmp_path, flags):
    service, catalog = fixture(tmp_path)
    with pytest.raises(MissionError):
        read_objective_progress(service, flags, flag_catalog_path=catalog)
    assert not (tmp_path / 'objective-query.request').exists()


@pytest.mark.parametrize('change', [lambda base, ids: ids.update({1: 'reused-pid'}),
                                  lambda base, ids: base.update(map_id='OtherWorld'),
                                  lambda base, ids: base['player'].update(actor_id='Cloud_after_load')])
def test_process_world_and_loaded_actor_changes_invalidate_the_snapshot(tmp_path, change):
    service, catalog = fixture(tmp_path, change=change)
    with pytest.raises(MissionError, match='objective_identity_changed'):
        read_objective_progress(service, [FLAG], flag_catalog_path=catalog)


@pytest.mark.parametrize('mutation, reason', [
    (lambda p: p.update(source_sequence=99), 'objective_response_invalid'),
    (lambda p: p.update(timestamp_utc=(datetime.now(timezone.utc)-timedelta(seconds=10)).isoformat()), 'objective_snapshot_stale'),
    (lambda p: p.update(flags={FLAG: 0}), 'objective_progress_incomplete'),
    (lambda p: p.update(flags={}), 'objective_progress_incomplete'),
])
def test_stale_or_incomplete_flag_evidence_is_not_current_progress(tmp_path, mutation, reason):
    service, catalog = fixture(tmp_path, payload_change=mutation)
    with pytest.raises(MissionError, match=reason):
        read_objective_progress(service, [FLAG], flag_catalog_path=catalog)


def test_unavailable_getters_are_reported_without_fabricating_false_flags(tmp_path):
    service, catalog = fixture(tmp_path, payload_change=lambda p:p.update(
        observed=False, flags={}, reason='objective_getters_unavailable'))
    result = read_objective_progress(service, [FLAG], flag_catalog_path=catalog)
    assert result['ok'] is False and result['reason'] == 'objective_getters_unavailable'
    assert result['flags'] == {}


def test_configured_export_root_is_also_used_to_validate_flag_ids(tmp_path, monkeypatch):
    service, catalog = fixture(tmp_path)
    resident = tmp_path / 'exports/Resident'
    resident.mkdir(parents=True)
    (resident / 'StoryFlag.csv').write_bytes(catalog.read_bytes())
    monkeypatch.setenv('FF7R_OBJECTIVE_DATA_ROOT', str(resident.parent))
    result = read_objective_progress(service, [FLAG])
    assert result['ok'] and result['input_sent'] is False


def test_real_lua_probe_is_idle_between_requests_and_reacquires_the_api_after_reload():
    LuaRuntime = pytest.importorskip('lupa.lua54').LuaRuntime
    lua = LuaRuntime(unpack_returned_tuples=True)
    lua.execute(r'''
        files, calls, api_lookups = {}, 0, 0
        local object = {IsValid=function() return true end}
        pawn = {IsValid=function() return true end, GetFullName=function() return 'Cloud_1' end}
        db = {IsValid=function() return true end,
              GetChapterProgress=function() calls=calls+1; return 12 end,
              IsStoryFlag=function(self, id) calls=calls+1; return id=='flag_on' end}
        local refs = {resolve=function(path)
            api_lookups=api_lookups+1
            if path:find('Default__',1,true) then return db end
            return object
        end}
        package.preload.object_refs=function() return refs end
        FName=function(s) return s end
        io.open=function(path, mode)
            if mode=='rb' then
                if files[path]==nil then return nil end
                return {read=function(_, count) return files[path]:sub(1,count) end,close=function() end}
            end
            return {write=function(_, value) files[path]=value end,close=function() end}
        end
        os.remove=function(path) files[path]=nil end
        os.rename=function(a,b) files[b]=files[a]; files[a]=nil; return true end
        os.time=function() return 1000 end
    ''')
    probe = lua.execute((Path(__file__).parents[2] / 'ff7r-telemetry-exporter/Base/Scripts/objective_query.lua').read_text())
    path = 'C:\\CaitGPT\\Telemetry\\objective-query.request'
    response = path.replace('.request', '.json')
    probe.sample(100, lua.globals().pawn, 'EndGame', '2026-10-12T00:00:00Z')
    assert lua.globals().calls == lua.globals().api_lookups == 0
    lua.globals().files[path] = 'one|1030|flag_on,flag_off'
    probe.sample(101, lua.globals().pawn, 'EndGame', '2026-10-12T00:00:00Z')
    data = json.loads(lua.globals().files[response])
    assert data['observed'] and data['flags'] == {'flag_on': True, 'flag_off': False}
    calls = lua.globals().calls
    probe.sample(102, lua.globals().pawn, 'EndGame', '2026-10-12T00:00:00Z')
    assert lua.globals().calls == calls
    lua.execute("db = {IsValid=function() return true end, GetChapterProgress=function() return 13 end, IsStoryFlag=function() return false end}")
    lua.globals().files[path] = 'two|1030|flag_on'
    probe.sample(103, lua.globals().pawn, 'EndGame', '2026-10-12T00:00:00Z')
    assert json.loads(lua.globals().files[response])['chapter_progress'] == 13
    lua.globals().files[path] = 'three|1030|flag_on'
    lookups = lua.globals().api_lookups
    probe.sample(104, None, 'EndGame', '2026-10-12T00:00:00Z')
    assert lua.globals().api_lookups == lookups
    assert json.loads(lua.globals().files[response])['reason'] == 'field_not_loaded'
