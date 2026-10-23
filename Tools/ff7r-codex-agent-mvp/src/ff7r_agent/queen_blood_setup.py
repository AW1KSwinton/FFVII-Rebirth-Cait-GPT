"""One bounded, narrated setup job; strategy resumes at the mulligan hand."""
from __future__ import annotations

import asyncio
import math
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import time
import uuid

from .navigation_mission import feature_identity_check, input_recovery_refused, lifecycle_binding, unwrap
from .navigation_values import MissionError
from .npc_approach import approach_npc
from .public_stream_events import agent_speech_problem, speech_tone_problem
from .qb_opponents import opponent, travel_spot_for


STAGES = frozenset({"preflight", "approach", "travel", "interaction", "deck", "edit", "begin", "mulligan", "stopped"})
ASYNC_CHILDREN = {
    "start_live_route_adapter": ("run_live_route_adapter", "get_live_route_adapter_status"),
    "start_topology_capture": ("capture_topology_cache", "get_topology_capture_status"),
    "travel_to_map_spot_async": ("travel_to_map_spot", "get_travel_to_map_spot_status"),
}


async def already_at_target(rpc, canonical):
    """Skip a walking route only for a fresh, facing, identified Talk target.

    Initiation still validates the target and interaction independently.
    This uses the existing read-only primitives and sends no input.
    """
    try:
        resolved = unwrap(await rpc("resolve_world_target", {"name": canonical}))
    except MissionError as exc:
        # A target that is not loaded here (another region, or not yet
        # streamed in) is simply not nearby: the approach travels to it (#321).
        if input_recovery_refused(exc):
            raise
        return None
    actor = resolved.get("live_actor") or {}
    if (resolved.get("status") != "located" or resolved.get("canonical_id") != canonical
            or resolved.get("location_quality") != "authoritative_live"
            or not actor.get("id") or actor.get("is_player") is not False):
        return None
    state = unwrap(await rpc("observe_game", {}))
    player = state.get("player") or {}
    position, target = player.get("position") or {}, resolved.get("position") or {}
    yaw = (player.get("rotation") or {}).get("yaw")
    if (state.get("running") is not True or (state.get("telemetry") or {}).get("live") is not True
            or state.get("map_id") != resolved.get("map_id") or not state.get("map_id")
            or any(state.get(key) for key in ("paused", "in_menu", "in_combat", "loading", "cutscene"))
            or any(type(pose.get(axis)) not in (int, float) or not math.isfinite(pose[axis])
                   for pose in (position, target) for axis in ("x", "y", "z"))
            or type(yaw) not in (int, float) or not math.isfinite(yaw)
            or type(state.get("age_seconds")) not in (int, float) or not 0 <= state["age_seconds"] <= 2):
        return None
    distance = math.hypot(target["x"] - position["x"], target["y"] - position["y"]) / 100
    bearing = math.degrees(math.atan2(target["y"] - position["y"], target["x"] - position["x"]))
    error = abs((bearing - yaw + 180) % 360 - 180)
    if distance > 5 or abs(target["z"] - position["z"]) > 150 or error > 40:
        return None
    prompt = unwrap(await rpc("read_field_prompt", {}))
    if prompt.get("talk") is not True or prompt.get("source_sequence", -1) < state.get("sequence", 0):
        return None
    return {"ok": True, "state": "talk_offered", "already_at_target": True,
            "target": {"canonical_id": canonical, "actor_id": actor["id"]},
            "distance_m": distance, "heading_error_degrees": error}


class SetupRpc:
    """Reuse the runtime primitives locally without starting a second worker.

    Only one child input job may run. Polls inspect its Future, rather than
    adding telemetry reads. Identity/bootstrap are actual bridge snapshots,
    supplied by the supervisor after binding its worker, never caller claims.
    """
    def __init__(self, invoke, context):
        self.invoke, self.context = invoke, context
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="qb-setup-child")
        self.child = None
        self.child_id = None
        self.status_operation = None

    async def __call__(self, operation, arguments):
        if operation == "get_runtime_identity":
            return deepcopy(self.context["identity"])
        if operation == "get_bootstrap_verification":
            return deepcopy(self.context["bootstrap"])
        if operation == "get_agent_runtime_status":
            return deepcopy(self.context["status"])
        if operation in ASYNC_CHILDREN:
            if self.child is not None and not self.child.done():
                raise MissionError("setup_child_input_in_progress")
            work, self.status_operation = ASYNC_CHILDREN[operation]
            self.child_id = "setup-child-" + uuid.uuid4().hex
            self.child = self.pool.submit(self.invoke, work, dict(arguments))
            return {"ok": True, "job_id": self.child_id, "state": "running"}
        if operation in {entry[1] for entry in ASYNC_CHILDREN.values()}:
            if operation != self.status_operation or arguments.get("job_id") != self.child_id:
                raise MissionError("setup_child_job_not_found")
            if not self.child.done():
                return {"ok": True, "job_id": self.child_id, "state": "running"}
            try:
                response = {"ok": True, "result": self.child.result(), "id": self.child_id}
            except Exception as exc:
                response = {"ok": False, "error": str(exc), "notes": list(getattr(exc, "__notes__", []))}
            return {"ok": True, "job_id": self.child_id, "state": "completed", "response": response}
        cleanup_operations = {"stop_all_input", "close_control_session", "get_control_session_status"}
        if self.child is not None and not self.child.done() and operation not in cleanup_operations:
            raise MissionError("setup_child_input_in_progress")
        if operation in cleanup_operations:
            return await asyncio.to_thread(self.invoke, operation, dict(arguments))
        return await asyncio.wrap_future(self.pool.submit(self.invoke, operation, dict(arguments)))

    async def drain(self):
        # The serial executor's barrier also joins a cancelled await whose
        # native operation was already running. No abandoned input survives.
        await asyncio.wrap_future(self.pool.submit(lambda: None))

    def close(self):
        # NavigationMission stops input on uncertain starts. Joining prevents
        # any abandoned child from sending input after the outer job returns.
        self.pool.shutdown(wait=True, cancel_futures=True)


async def setup_match(rpc, *, target_id, cards, model_id, reasoning_effort, client_id,
                      expected_ref, expected_commit, repository_root, narration,
                      output, timeout_seconds=900, progress=lambda value: None,
                      allow_travel=True, approach=approach_npc,
                      identity_checker=feature_identity_check,
                      clock=time.monotonic, sleep=asyncio.sleep):
    started = clock()
    deadline = started
    stage = "preflight"
    completed, timings, speech = [], {}, []
    last_result = None
    approach_result = None
    edit_result = None
    actual_deck = None
    input_started = False
    run_binding = None
    phase_started = started

    def milestone(name):
        nonlocal stage, phase_started
        now = clock()
        timings[stage] = timings.get(stage, 0) + now - phase_started
        stage, phase_started = name, now
        progress({"stage": name, "completed_stages": list(completed),
                  "elapsed_seconds": round(now - started, 3)})

    def remaining():
        if clock() >= deadline:
            raise MissionError("match_setup_deadline_exceeded")

    async def call(operation, arguments=None, *, cleanup=False):
        if not cleanup:
            remaining()
        return unwrap(await rpc(operation, arguments or {}))

    async def say(key):
        line = narration.get(key)
        if not isinstance(line, dict):
            raise MissionError("setup_narration_missing:" + key)
        while True:
            remaining()
            # A transport success may contain a speech refusal; unwrap would
            # throw before we could honour its bounded rate-limit response.
            receipt = await rpc("say_on_stream", line)
            if receipt.get("ok") is True and isinstance(receipt.get("result"), dict):
                receipt = receipt["result"]
            previous = receipt.get("previous_delivery") or {}
            speech.append({"stage": stage, "key": key, **line, "response": receipt})
            if previous.get("status") in {"failed", "dropped", "skipped"}:
                raise MissionError("setup_previous_speech_delivery_failed", response=receipt)
            if receipt.get("ok") is True:
                return
            # Only bounded rate-limit waits retry the same prepared line. A
            # changed plan or rejected wording returns control to the agent.
            if receipt.get("reason") != "rate_limited":
                raise MissionError("setup_speech_failed", response=receipt)
            delay = receipt.get("retry_after_seconds")
            if type(delay) not in (int, float) or not math.isfinite(delay) or not 0 < delay <= 30:
                raise MissionError("setup_speech_retry_unbounded", response=receipt)
            await sleep(min(delay, max(0, deadline - clock())))
            await bind(acquire=False)
            if stage in {"deck", "edit", "begin"}:
                fresh = await call("read_queens_blood_cards", {"area": "Player"})
                if Counter(card["card_id"] for card in fresh.get("cards", [])) != Counter(actual_deck or []):
                    raise MissionError("match_setup_deck_changed_during_speech_wait", response=fresh)

    async def bind(*, acquire=False):
        nonlocal run_binding
        current = await call("get_queens_blood_run_status")
        loaded = current.get("loaded_save") or {}
        driver = current.get("driver") or {}
        assigned = current.get("assignment") or {}
        process = loaded.get("process") or {}
        binding = deepcopy((current.get("profile_id"), loaded.get("load_id"), loaded.get("slot"), process))
        if (current.get("loaded_save_verified") is not True or current.get("run_loaded") is not True
                or not binding[0] or not binding[1] or not str(process.get("pid", "")).isdigit()
                or int(process["pid"]) <= 0 or not process.get("token")
                or driver.get("model_id") != model_id or driver.get("effort") != reasoning_effort
                or assigned.get("model_id") != model_id or assigned.get("slot") != loaded.get("slot")):
            raise MissionError("match_setup_requires_verified_model_save", response=current)
        if run_binding is not None and binding != run_binding:
            raise MissionError("match_setup_loaded_save_changed", response=current)
        run_binding = binding
        lease = await call("get_control_session_status")
        if lease.get("active"):
            # The open session is used whoever holds it, if it is for this
            # model and this game; it is left open.
            if (lease.get("recording_driver") != {"modelId": model_id, "effort": reasoning_effort}
                    or lease.get("game_pid") != int(process["pid"])):
                raise MissionError("control_session_identity_mismatch", response=lease)
            return
        if not acquire:
            return
        session = await call("open_control_session", {"game_pid": int(process["pid"]),
            "model_id": model_id, "reasoning_effort": reasoning_effort, "client_id": client_id})
        if not session.get("active") or session.get("client_id") != client_id:
            raise MissionError("control_session_identity_mismatch", response=session)

    async def workflow():
        nonlocal deadline, input_started, last_result, actual_deck, approach_result, edit_result
        if not isinstance(cards, list) or len(cards) != 15 or any(not isinstance(c, str) or not c for c in cards):
            raise MissionError("match_setup_requires_exact_15_card_ids")
        if any(not isinstance(v, str) or not v.strip() for v in
               (model_id, reasoning_effort, client_id, expected_ref, expected_commit)):
            raise MissionError("match_setup_requires_explicit_identity")
        if type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or not 30 <= timeout_seconds <= 1800:
            raise MissionError("match_setup_timeout_out_of_bounds")
        deadline = started + timeout_seconds
        if type(allow_travel) is not bool:
            raise MissionError("match_setup_allow_travel_must_be_boolean")
        if not isinstance(narration, dict):
            raise MissionError("match_setup_requires_prepared_narration")
        required = {"opponent", "approach", "interaction", "deck", "begin"}
        for key in required | (set(narration) - {"edit_from"}):
            line = narration.get(key)
            if not isinstance(line, dict) or set(line) - {"text", "tone"}:
                raise MissionError("setup_narration_invalid:" + key)
            if agent_speech_problem(line.get("text")) or speech_tone_problem(line.get("tone")):
                raise MissionError("setup_narration_invalid:" + key)
        record = opponent(target_id)
        canonical = record.get("canonical_id") or record["id"]
        identity = await call("get_runtime_identity")
        bootstrap = await call("get_bootstrap_verification")
        identity_check = identity_checker(Path(repository_root), expected_ref, expected_commit)
        if identity_check(identity, bootstrap).get("ok") is not True:
            raise MissionError("runtime_identity_mismatch")
        game_binding = lifecycle_binding(await call("get_lifecycle_status"))
        candidate = await call("prepare_queens_blood_deck", {"cards": cards, "opponent": record["id"],
            "rationale": narration["deck"]["text"]})
        await bind()
        if game_binding["pid"] != int(run_binding[3]["pid"]):
            raise MissionError("match_setup_game_process_changed")
        completed.append("preflight")
        await say("opponent")
        milestone("approach")

        async def approach_rpc(operation, arguments):
            nonlocal last_result
            if operation == "travel_to_map_spot_async":
                if not allow_travel:
                    raise MissionError("match_setup_travel_not_authorized")
                if any(line["key"] == "travel" and line["response"].get("ok") for line in speech):
                    raise MissionError("match_setup_travel_plan_changed")
                milestone("travel")
                await say("travel")
            elif operation == "start_live_route_adapter" and stage == "travel":
                completed.append("travel")
                milestone("approach")
                await say("arrival_approach")
            elif operation == "use_guide_moogle":
                if not allow_travel:
                    raise MissionError("match_setup_travel_not_authorized")
                await say(operation)
            elif operation in {"open_door", "terrain_action", "talk"}:
                # An unexpected interaction needs its own truthful prepared
                # explanation; absent one, return control before input.
                await say(operation)
            return await rpc(operation, arguments)

        await say("approach")
        try:
            reset_spot = travel_spot_for(canonical).get("key") if allow_travel else None
        except MissionError:
            reset_spot = None
        input_started = True
        last_result = await already_at_target(rpc, canonical)
        if last_result is None:
            last_result = await approach(approach_rpc, canonical, output=Path(output),
                identity_check=identity_check, execute=True, pursuit=True,
                reset_spot=reset_spot, hand_off_when_stuck=True, model_id=model_id,
                reasoning_effort=reasoning_effort, client_id=client_id)
        if last_result.get("ok") is not True or last_result.get("state") != "talk_offered":
            raise MissionError("match_setup_approach_failed", response=last_result)
        approach_result = last_result
        completed.append("approach")
        await bind(acquire=True)  # The approach leaves an open session open.
        after_binding = lifecycle_binding(await call("get_lifecycle_status"))
        # Route preflight may fill a previously unknown executable hash.
        # The verified save's PID/creation token is the process boundary;
        # lifecycle attachment metadata may be strengthened in place.
        if (after_binding["pid"] != game_binding["pid"] or
                (game_binding["executable_sha256"] and after_binding["executable_sha256"] != game_binding["executable_sha256"])):
            raise MissionError("match_setup_game_process_changed")
        milestone("interaction")
        await say("interaction")
        last_result = await call("run_initiate_queens_blood_match", {"target_id": canonical,
            "stop_at_deck_select": True, "timeout_seconds": min(120, deadline - clock())})
        if last_result.get("ok") is not True or last_result.get("outcome") != "deck_select_ready":
            raise MissionError("match_setup_deck_select_not_ready", response=last_result)
        completed.append("interaction")
        milestone("deck")
        await call("revalidate_queens_blood_prepared_deck", {"candidate": candidate, "opponent": record["id"]})
        read = await call("read_queens_blood_cards", {"area": "Player"})
        actual_deck = [card["card_id"] for card in read.get("cards", [])]
        if len(actual_deck) != 15:
            raise MissionError("match_setup_actual_deck_incomplete", response=read)
        await say("deck")
        if Counter(actual_deck) != Counter(cards):
            if Counter(narration.get("edit_from", [])) != Counter(actual_deck):
                raise MissionError("match_setup_edit_plan_changed", response={"actual_deck": actual_deck, "desired_deck": cards})
            milestone("edit")
            await say("edit")
        last_result = await call("edit_queens_blood_deck", {"cards": cards})
        edit_result = last_result
        actual_deck = last_result.get("after")
        if last_result.get("ok") is not True or not isinstance(actual_deck, list) or Counter(actual_deck) != Counter(cards):
            raise MissionError("match_setup_deck_verification_failed", response=last_result)
        completed.append("deck")
        await bind(acquire=True)
        milestone("begin")
        await say("begin")
        last_result = await call("run_initiate_queens_blood_match", {"target_id": canonical,
            "timeout_seconds": min(60, deadline - clock())})
        if last_result.get("ok") is not True or last_result.get("outcome") != "mulligan_ready":
            raise MissionError("match_setup_mulligan_not_ready", response=last_result)
        completed.append("begin")
        milestone("mulligan")
        observation = None
        for _ in range(40):
            try:
                observation = await call("observe_queens_blood")
            except Exception as exc:
                if str(exc) != "Queen's Blood state changed across four verification samples":
                    raise
                # The cards are still being dealt: another bounded attempt
                # follows only a reported discrepancy, never an idle scan.
                await sleep(0.5)
                continue
            qb = observation.get("queen_blood") or {}
            hand = qb.get("hand") or []
            mulligan = qb.get("screen") == "mulligan" or (qb.get("popups") or {}).get("mulligan_confirm") is True
            if mulligan and len(hand) == 5 and all(c.get("card_id") for c in hand):
                # observe_queens_blood already verifies two equal, advancing
                # snapshots; do not request another pair for the same hand.
                await bind(acquire=True)
                completed.append("mulligan")
                timings[stage] = clock() - phase_started
                return {"ok": True, "outcome": "mulligan_ready", "completed_stages": completed,
                    "stage": stage, "opponent": record["id"], "deck": actual_deck, "observation": observation,
                    "approach": approach_result, "deck_edit": edit_result,
                    "speech": speech, "timings": timings, "seconds": round(clock() - started, 3)}
            await sleep(0.5)
        raise MissionError("match_setup_mulligan_hand_unverified", response=observation)

    try:
        # Validate before arming the watchdog (including NaN and strings).
        if type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or not 30 <= timeout_seconds <= 1800:
            raise MissionError("match_setup_timeout_out_of_bounds")
        return await asyncio.wait_for(workflow(), timeout=timeout_seconds)
    except (Exception, asyncio.CancelledError) as exc:
        failed_stage = stage
        cleanup = {}
        if input_started:
            try:
                # Input may be in flight, so this is an emergency stop,
                # whoever holds the session; it closes the session and says so.
                # Only this client's own session is closed beyond that.
                cleanup["stop"] = await call("stop_all_input", cleanup=True)
                lease = await call("get_control_session_status", cleanup=True)
                if lease.get("active") and lease.get("client_id") == client_id:
                    cleanup["close"] = await call("close_control_session", {"client_id": client_id}, cleanup=True)
                drain = getattr(rpc, "drain", None)
                if drain is not None:
                    await drain()
                lease = await call("get_control_session_status", cleanup=True)
                if lease.get("active") and lease.get("client_id") == client_id:
                    cleanup["close_after_drain"] = await call("close_control_session", {"client_id": client_id}, cleanup=True)
            except Exception as cleanup_error:
                cleanup["error"] = str(cleanup_error)
        if failed_stage in {"deck", "edit"}:
            try:
                read = await call("read_queens_blood_cards", {"area": "Player"}, cleanup=True)
                actual_deck = [card["card_id"] for card in read.get("cards", [])]
            except Exception as deck_error:
                cleanup["deck_read_error"] = str(deck_error)
        try:
            capture = await call("capture_game_window", cleanup=True)
        except Exception as capture_error:
            capture = {"ok": False, "reason": str(capture_error)}
        milestone("stopped")
        return {"ok": False, "outcome": "setup_stopped", "stage": failed_stage,
            "completed_stages": completed, "reason": "match_setup_deadline_exceeded" if isinstance(exc, TimeoutError) else str(exc),
            "observed": getattr(exc, "response", None),
            "notes": list(getattr(exc, "__notes__", [])), "last_result": last_result,
            "actual_deck": actual_deck, "capture": capture, "cleanup": cleanup,
            "speech": speech, "timings": timings, "seconds": round(clock() - started, 3)}


def run_runtime_setup(service, lifecycle, startup, values):
    from .runtime_commands import invoke_runtime
    context = values.pop("__setup_context", None)
    if not isinstance(context, dict) or not context.get("identity") or not context.get("bootstrap"):
        raise ValueError("match setup requires a supervisor-bound context")
    public_arguments = {"target_id", "cards", "model_id", "reasoning_effort", "client_id",
        "expected_ref", "expected_commit", "narration", "output", "timeout_seconds", "allow_travel"}
    if set(values) - public_arguments:
        raise ValueError("unsupported match setup arguments: " + ", ".join(sorted(set(values) - public_arguments)))
    output = values.pop("output", None)
    if output is None:
        from .factory import default_runtime_dir
        output = default_runtime_dir() / "qb-setup" / uuid.uuid4().hex
    rpc = SetupRpc(lambda operation, arguments: invoke_runtime(service, lifecycle, startup, operation, arguments), context)
    try:
        return asyncio.run(setup_match(rpc, repository_root=context["identity"]["repository_root"],
            output=Path(output), progress=getattr(service, "_match_setup_progress", lambda value: None), **values))
    finally:
        rpc.close()
