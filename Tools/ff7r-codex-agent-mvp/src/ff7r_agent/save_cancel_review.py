"""Explicit cancel-only adoption of preserved calibration evidence."""
import hashlib,json,re
from pathlib import Path
from .save_slot_load import SaveSlotLoadError
from .save_selection import selected_cell,complete_nodes

REVIEWED_SOURCE="f8c666ae60e76d1333c2b34458b2fbb97e957595"

def load_review(config,lock):
    if not isinstance(config,dict) or set(config)!={"artifacts","row_contract"}:
        raise SaveSlotLoadError("save_cancel_review_invalid")
    contract=config["row_contract"]
    if not isinstance(contract,dict) or set(contract)!={"slot_node","row_button_node","row_selection"} or contract["slot_node"]!="Txt_ListNum" or contract["row_button_node"]!="Button_00":
        raise SaveSlotLoadError("save_cancel_review_invalid")
    from .save_selection import validate_contract
    validate_contract(contract["row_selection"])
    artifacts=config["artifacts"];raws={};paths={}
    if artifacts == {}:
        final=lock.get("final_row_contract")
        prompt=lock.get("observed_calibration_prompt")
        if not isinstance(final,dict) or final.get("contract")!=contract or not isinstance(prompt,dict):
            raise SaveSlotLoadError("save_cancel_durable_proof_missing")
        cell,entry=selected_cell(final["response"],contract["row_selection"])
        numbers=[n for n in complete_nodes(cell) if n.get("name")=="Txt_ListNum" and n.get("visible") is True]
        if len(numbers)!=1 or numbers[0].get("text")!="15":raise SaveSlotLoadError("save_cancel_original_row_unverified")
        return contract,entry["id"],prompt["id"],cell["full_id"],raws,paths
    if not isinstance(artifacts,dict) or set(artifacts)!={"runtime","start","final","preflight","prompt"}:
        raise SaveSlotLoadError("save_cancel_review_invalid")
    for key,spec in artifacts.items():
        if not isinstance(spec,dict) or set(spec)!={"path","sha256"} or not re.fullmatch(r"[0-9a-fA-F]{64}",str(spec["sha256"])):
            raise SaveSlotLoadError("save_cancel_review_invalid")
        path=Path(spec["path"])
        if not path.is_absolute() or path.is_symlink() or not path.is_file() or path.stat().st_size>16*1024*1024:
            raise SaveSlotLoadError("save_cancel_review_invalid")
        raw=path.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=spec["sha256"].lower():raise SaveSlotLoadError("save_cancel_review_changed")
        raws[key]=raw;paths[key]=path
    runtime=json.loads(raws["runtime"].decode("utf-8-sig"))
    identity=runtime["runtime"]["structuredContent"]
    run=runtime["run"]["structuredContent"]["result"]
    if (identity.get("source_commit_sha")!=REVIEWED_SOURCE or identity.get("git_commit_sha")!=REVIEWED_SOURCE
            or identity.get("git_dirty") is not False or identity.get("runtime_profile_verification",{}).get("ok") is not True
            or any(run.get(k)!=lock["review_binding"]["initial"].get(k) for k in ("assignment","loaded_save"))
            or any((run.get("driver") or {}).get(k)!=(lock["review_binding"]["initial"].get("driver") or {}).get(k) for k in ("driver","model_id","effort"))):
        raise SaveSlotLoadError("save_cancel_source_attestation_invalid")
    preflight=json.loads(raws["preflight"].decode("utf-8-sig"))["structuredContent"]["result"]
    if (preflight.get("run_loaded") is not True or preflight.get("loaded_save_verified") is not True
            or any(preflight.get(k)!=lock["review_binding"]["initial"].get(k) for k in ("driver","assignment","loaded_save"))):
        raise SaveSlotLoadError("save_cancel_preflight_identity_invalid")
    start=json.loads(raws["start"].decode("utf-8-sig"))["structuredContent"]
    final=json.loads(raws["final"].decode("utf-8-sig"))["structuredContent"]
    result=final["response"]["result"]
    if (start.get("ok") is not True or final.get("ok") is not True or final.get("state")!="completed"
            or start.get("operation")!="save_game_progress" or final.get("operation")!="save_game_progress"
            or any(start.get(k)!=final.get(k) for k in ("job_id","started_at","worker_generation","worker_pid"))
            or result.get("ok") is not True or result.get("saved") is not False or result.get("execute") is not True
            or result.get("calibration") is not True or result.get("slot")!=15
            or result.get("state")!="save_calibration_prompt_observed" or result.get("backup")!=lock.get("backup")):
        raise SaveSlotLoadError("save_cancel_chronology_unverified")
    presses=result.get("presses",[]);screens=result.get("screens",[])
    row_confirm=[p for p in presses if p.get("why")=="open_save_prompt"]
    stages=[s for s in screens if s.get("stage")=="final_save_row_after_file_guards"]
    if (len(row_confirm)!=1 or not presses or presses[-1]!=row_confirm[0] or row_confirm[0].get("button")!="confirm"
            or len(stages)!=1 or stages[0]["t"]>row_confirm[0]["t"]
            or not screens or screens[-1].get("stage")!="save_calibration_after_row_confirm"
            or any("verified_yes" in str(p.get("why")) for p in presses)):
        raise SaveSlotLoadError("save_cancel_chronology_unverified")
    cell,entry=selected_cell(result["row_contract_evidence"],contract["row_selection"])
    numbers=[n for n in complete_nodes(cell) if n.get("name")=="Txt_ListNum" and n.get("visible") is True]
    if len(numbers)!=1 or numbers[0].get("text")!="15":raise SaveSlotLoadError("save_cancel_original_row_unverified")
    dialogs=result["raw_save_menu"].get("save_dialogs",[])
    if len(dialogs)!=1:raise SaveSlotLoadError("save_cancel_prompt_unverified")
    native_prompt=json.loads(raws["prompt"].decode("utf-8-sig"))
    if (native_prompt.get("source")!="ue4ss_map_query" or native_prompt.get("schema_version")!=1
            or not re.fullmatch(r"[0-9a-f]{32}",str(native_prompt.get("request_id")))
            or type(native_prompt.get("source_sequence")) is not int):
        raise SaveSlotLoadError("save_cancel_prompt_unverified")
    from types import SimpleNamespace
    verify_prompt(SimpleNamespace(response=native_prompt,text_cap_reached=False,load_prompt_shown=False,signals=[],save_prompt_shown=True),dialogs[0]["id"])
    return contract,entry["id"],dialogs[0]["id"],cell["full_id"],raws,paths

def verify_prompt(screen,expected_id):
    dialogs=screen.response.get("save_dialogs")
    if (screen.text_cap_reached or screen.load_prompt_shown or "load_list" in screen.signals
            or not screen.save_prompt_shown or not isinstance(dialogs,list) or len(dialogs)!=1):
        raise SaveSlotLoadError("save_cancel_prompt_unverified")
    dialog=dialogs[0]
    if (dialog.get("id")!=expected_id or dialog.get("class")!="Com_Window_01_C"
            or not re.fullmatch(r"Com_Window_01_C /Temp/Untitled_1\.MenuRenderingWorld:Com_Window_01_C_[0-9]+",expected_id)):
        raise SaveSlotLoadError("save_cancel_prompt_unverified")
    nodes=dialog.get("nodes")
    if not isinstance(nodes,list) or len(nodes)>128 or any(not isinstance(n,dict) for n in nodes):raise SaveSlotLoadError("save_cancel_prompt_unverified")
    text=[n for n in nodes if n.get("name")=="Txt" and n.get("class")=="EndTextBlock" and n.get("visible") is True]
    if any("full_id" in n for n in nodes):
        # Deeper walks also include Btn_00/Btn_01.WidgetTree.Txt labels. Bind the
        # question to this exact dialog's own WidgetTree, never a nested button.
        expected_text_id = "EndTextBlock " + expected_id.split(" ", 1)[1] + ".WidgetTree.Txt"
        text=[n for n in text if n.get("full_id")==expected_text_id]
    if len(text)!=1 or text[0].get("text")!="Save your progress to this slot?":raise SaveSlotLoadError("save_cancel_prompt_unverified")
    # Truncated child button details do not authorize Confirm/Yes; only Cancel.
