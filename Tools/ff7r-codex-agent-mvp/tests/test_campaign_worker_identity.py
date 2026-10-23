"""Campaign identity reads must work on the first request after worker restart."""
import json
from datetime import datetime,timezone,timedelta
import pytest
from ff7r_agent.service import AgentService,SafetyError
from ff7r_agent.queen_blood_runs import QueenBloodRuns
from test_campaign_director import NoGame,Processes
from test_queen_blood_runs import DRIVER,save,loaded


def fixture(tmp_path):
    root=tmp_path/"queen-blood";state=tmp_path/"state";state.mkdir()
    store=QueenBloodRuns(root);file=save(tmp_path,15)
    store.assign(DRIVER,file);loaded(store,file)
    now=datetime.now(timezone.utc);started=(now-timedelta(minutes=5)).isoformat()
    (state/"public-driver-rehearsal.json").write_text(json.dumps({
        "version":1,"driver":DRIVER["driver"],"modelId":DRIVER["model_id"],"effort":DRIVER["effort"],
        "runId":DRIVER["run_id"],"streamStartedAt":started,"declaredAt":now.isoformat()}))
    def heartbeat(age=0):
        (state/"public-stream-rehearsal.json").write_text(json.dumps({"version":1,
            "at":(datetime.now(timezone.utc)-timedelta(seconds=age)).isoformat(),"recordingStartedAt":started}))
    heartbeat()
    def worker():
        return AgentService(NoGame(),NoGame(),environment=Processes(),queen_blood_profile_root=root,
                            public_stream_event_dir=state/"events")
    return worker,heartbeat,state


@pytest.mark.parametrize("operation",["get_campaign_status","get_campaign_next_step"])
def test_first_campaign_request_resolves_native_identity_without_status_priming(tmp_path,operation):
    worker,_,_=fixture(tmp_path)
    for _ in range(2):
        service=worker()
        result=getattr(service,operation)()
        assert result["profile_id"].endswith("slot015")
        assert service.get_queens_blood_run_status()["run_loaded"] is True


def test_expired_native_heartbeat_refuses_then_fresh_heartbeat_recovers_without_priming(tmp_path):
    worker,heartbeat,_=fixture(tmp_path);service=worker();heartbeat(age=61)
    with pytest.raises(SafetyError,match="declare and assign"):
        service.get_campaign_next_step()
    heartbeat()
    assert service.get_campaign_next_step()["profile_id"].endswith("slot015")


def test_declaration_wrong_recording_run_refuses_on_first_campaign_request(tmp_path):
    worker,_,state=fixture(tmp_path)
    p=state/"public-driver-rehearsal.json";d=json.loads(p.read_text())
    d["streamStartedAt"]="2020-01-01T00:00:00+00:00";p.write_text(json.dumps(d))
    with pytest.raises(SafetyError,match="declare and assign"):
        worker().get_campaign_status()
