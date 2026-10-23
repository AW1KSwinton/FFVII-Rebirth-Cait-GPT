"""Preserved PrintWindow receipts and synthetic unsafe variants; no runtime."""
import copy,json
from pathlib import Path
from types import SimpleNamespace
import pytest
from ff7r_agent.live_route_adapter import AgentServiceRenderEvidenceSource,_render_evidence_confirms_gameplay

FRAMES=json.loads((Path(__file__).parent/'fixtures/pietro-dark-field-render.json').read_text())['frames']

def evidence(frame):
 obs=SimpleNamespace(**frame['observation']);capture=frame['render_confirmation']['capture']
 source=AgentServiceRenderEvidenceSource(SimpleNamespace(capture_game_window=lambda:capture))
 return obs,source.observe(obs)

@pytest.mark.parametrize('frame',FRAMES)
def test_preserved_dark_textured_field_resolves_bound_brightness_false_negative(frame):
 obs,proof=evidence(frame)
 assert frame['render_confirmation']['ready'] is False  # Preserve original refusal.
 assert .20 <= proof.metrics['whole_nonblack'] < .30
 assert proof.ready and _render_evidence_confirms_gameplay(obs,proof)
 assert proof.reason=='telemetry_backed_gameplay_confirmed'

@pytest.mark.parametrize('defect',['menu','loading','cutscene','combat','not_ready','not_grounded','bad_position_reference','process','map','stale_capture','stale_observation','regressed_sequence','black','uniform','no_texture','unbound','semantic_title','semantic_menu','unknown_dark_not_grounded','ordinary_gameplay'])
def test_dark_frame_never_replaces_independent_binding_and_texture_guards(defect):
 frame=copy.deepcopy(FRAMES[0]);obs=frame['observation'];cap=frame['render_confirmation']['capture'];f=cap['startup_frame'];m=f['metrics']
 if defect in ['menu','loading','cutscene','combat']:obs[{'menu':'in_menu','loading':'loading','cutscene':'in_cutscene','combat':'in_combat'}[defect]]=True
 elif defect=='not_ready':obs['telemetry_gameplay_ready']=False
 elif defect=='not_grounded':obs['grounded']=False
 elif defect=='bad_position_reference':obs['position_reference']='actor_origin'
 elif defect=='process':cap['pid']+=1
 elif defect=='map':cap['map_id']='Other'
 elif defect=='stale_capture':cap['telemetry_age_seconds']=2.01
 elif defect=='stale_observation':obs['telemetry_age_seconds']=.51
 elif defect=='regressed_sequence':cap['telemetry_sequence']=obs['sequence']-1
 elif defect=='black':m['whole_nonblack']=.01
 elif defect=='uniform':m['luminance_stddev']=0;m['edge_fraction']=0
 elif defect=='no_texture':m.pop('edge_fraction')
 elif defect=='unbound':f.pop('telemetry_override')
 elif defect in ['semantic_title','semantic_menu']:f['render_classifier_label']='title_prompt' if defect=='semantic_title' else 'main_menu';f['classification_basis']='semantic_text'
 elif defect=='unknown_dark_not_grounded':f['render_classifier_label']='unknown';obs['grounded']=False
 elif defect=='ordinary_gameplay':f['render_classifier_label']='gameplay';f.pop('telemetry_override');f['label']='gameplay'
 observation,proof=evidence(frame)
 assert not proof.ready and not _render_evidence_confirms_gameplay(observation,proof)

# Live 2026-10-14, the lamp-lit cellar under the Kalm inn: these metrics were
# measured there; the observation binding is Pietro's (synthetic pairing).
KALM_CELLAR={'whole_nonblack':.13322368421052633,'left_menu_nonblack':.01594896331738437,'right_background_nonblack':.28289473684210525,'logo_nonblack':.19138755980861244,'title_prompt_nonblack':.22655122655122656,'luminance_mean':.07092166053921568,'luminance_stddev':.054537408881508605,'edge_fraction':.029059449866903283}

def cellar_frame(**changes):
 frame=copy.deepcopy(FRAMES[0]);frame['render_confirmation']['capture']['startup_frame']['metrics']={**KALM_CELLAR,**changes};return frame

def test_dark_lamp_lit_cellar_is_bound_gameplay():
 observation,proof=evidence(cellar_frame())
 assert proof.ready and _render_evidence_confirms_gameplay(observation,proof)

@pytest.mark.parametrize('changes',[{'whole_nonblack':.07},{'luminance_stddev':.03,'edge_fraction':.015}])
def test_dimmer_textured_scenery_is_not_mistaken_for_loading(changes):
 # Darkness alone is not the loading screen; the classifier names that.
 observation,proof=evidence(cellar_frame(**changes))
 assert proof.ready and _render_evidence_confirms_gameplay(observation,proof)

@pytest.mark.parametrize('changes',[{'whole_nonblack':.012},{'luminance_stddev':.01},{'edge_fraction':.005},{'luminance_stddev':0,'edge_fraction':0}])
def test_blank_or_flat_frame_stays_refused(changes):
 observation,proof=evidence(cellar_frame(**changes))
 assert not proof.ready and not _render_evidence_confirms_gameplay(observation,proof)

def test_loading_screen_is_refused_even_with_fresh_bound_telemetry():
 frame=cellar_frame();frame['render_confirmation']['capture']['startup_frame']['render_classifier_label']='loading'
 observation,proof=evidence(frame)
 assert not proof.ready and not _render_evidence_confirms_gameplay(observation,proof)


# Live 2026-10-21, a Mt. Nibel cave above the second gas yard (classifier: unknown).
NIBEL_CAVE={'whole_nonblack':.0197,'left_menu_nonblack':.027,'right_background_nonblack':.0197,'logo_nonblack':.021,'title_prompt_nonblack':.009,'luminance_mean':.0199,'luminance_stddev':.0258,'edge_fraction':.0072}

def cave_frame(**observation):
 frame=copy.deepcopy(FRAMES[0]);f=frame['render_confirmation']['capture']['startup_frame']
 f['metrics']=dict(NIBEL_CAVE);f['render_classifier_label']='unknown';frame['observation'].update(observation);return frame

def test_a_very_dark_cave_with_cloud_standing_is_bound_gameplay():
 observation,proof=evidence(cave_frame())
 assert proof.ready and _render_evidence_confirms_gameplay(observation,proof)

@pytest.mark.parametrize('observation',[{'grounded':False},{'position_reference':'actor_origin'}])
def test_a_very_dark_unknown_frame_needs_cloud_measured_standing(observation):
 observation,proof=evidence(cave_frame(**observation))
 assert not proof.ready and not _render_evidence_confirms_gameplay(observation,proof)
