"""One-shot session bootstrap. Game input goes only through the existing MCP."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
from typing import Any
from .subprocess_options import no_console_window_kwargs


class StartupBlocked(RuntimeError):
    pass


def failure_reason(error: BaseException) -> str:
    # AnyIO's MCP context can wrap a workflow refusal in an ExceptionGroup.
    if isinstance(error, StartupBlocked):
        return str(error)
    for child in getattr(error, "exceptions", ()):
        reason = failure_reason(child)
        if isinstance(child, StartupBlocked) or reason != type(child).__name__:
            return reason
    return type(error).__name__


def require(value: Any, reason: str) -> None:
    if not value:
        raise StartupBlocked(reason)


def payload(response: Any) -> dict:
    """Unwrap MCP content and worker envelopes, including nested failures."""
    if hasattr(response, "content"):
        require(not response.isError, "mcp_tool_error")
        value = response.structuredContent
        if value is None:
            texts = [item.text for item in response.content if item.type == "text"]
            require(bool(texts), "mcp_response_missing")
            value = json.loads(texts[0])
    else:
        value = response
    require(isinstance(value, dict), "mcp_response_invalid")
    # Semantic refusals (including speech rate limits) remain available to the
    # workflow. A worker/transport exception must never look like a success.
    require(not (value.get("ok") is False and "error" in value), str(value.get("error_type") or "mcp_operation_failed"))
    if "result" in value:
        return payload(value["result"])
    return value


@asynccontextmanager
async def connect(url: str):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            async def call(operation: str, arguments: dict | None = None):
                return payload(await asyncio.wait_for(session.call_tool("agent_call", {
                    "operation": operation, "arguments": arguments or {},
                }), timeout=45))
            yield call


class NativeServices:
    def __init__(self, options):
        self.options = options
        self.source = Path(__file__).resolve().parents[4]

    async def process(self, arguments, timeout=30, *, name="service_command"):
        # A Windows subprocess.run timeout kills the direct child, then drains
        # captured pipes without a deadline. A surviving descendant can keep
        # those pipes open, including through asyncio.run's executor shutdown.
        # Files have no EOF wait; poll only the direct process, without a thread.
        loop = asyncio.get_running_loop()
        started = loop.time()
        print(f"SERVICE_COMMAND_START={name} timeout_seconds={timeout}", flush=True)
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            process = subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                       close_fds=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            print(f"SERVICE_COMMAND_PID={name} pid={process.pid}", flush=True)
            try:
                while process.poll() is None:
                    remaining = started + timeout - loop.time()
                    require(remaining > 0, "service_command_timeout:" + name)
                    await asyncio.sleep(min(0.1, remaining))
            except BaseException:
                # Stop only this short-lived command/waiter. Never kill a service
                # process tree; a scheduled service may already be healthy.
                if process.poll() is None:
                    try:
                        process.kill()
                    except OSError:
                        pass
                cleanup_deadline = loop.time() + 2
                while process.poll() is None and loop.time() < cleanup_deadline:
                    await asyncio.sleep(0.05)
                print(f"SERVICE_COMMAND_ABORT={name} pid={process.pid} exit_code={process.poll()}", flush=True)
                if name in ("infrastructure_service_task", "mcp_service_task"):
                    # Keep the independent task's recovery coordinates even if
                    # this waiter failed before returning its captured output.
                    stdout.seek(0)
                    for line in stdout.read(1024 * 1024).decode("utf-8", errors="replace").splitlines():
                        if line.startswith(("SESSION_SERVICE_TASK=", "SESSION_SERVICE_LOG=", "SESSION_SERVICE_TASK_STATE=")):
                            print(line, flush=True)
                raise
            print(f"SERVICE_COMMAND_DONE={name} pid={process.pid} exit_code={process.returncode} "
                  f"elapsed_seconds={loop.time() - started:.2f}", flush=True)
            # Service checks return small JSON/receipt lines. Bound the snapshot
            # even if an unexpected descendant is still writing into the files.
            output = []
            for stream in (stdout, stderr):
                stream.seek(0)
                data = stream.read(1024 * 1024 + 1)
                require(len(data) <= 1024 * 1024, "service_command_output_limit:" + name)
                output.append(data.decode("utf-8", errors="replace"))
            return subprocess.CompletedProcess(arguments, process.returncode, *output)

    async def inspect(self, operation="inspect"):
        result = await self.process([self.options.node, str(self.source / "scripts/session-service-check.mjs"),
                                     self.options.runtime_root, operation, self.options.mode], 20, name="installed_services_" + operation)
        try:
            value = json.loads(result.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError) as exc:
            raise StartupBlocked("installed_service_check_invalid") from exc
        require(result.returncode == 0 and value.get("ok") is True, value.get("blocker", "installed_service_check_failed")
                + (":" + str(value["phase"]) if value.get("phase") else "") + (":" + str(value["code"]) if value.get("code") else ""))
        return value

    async def start_service_task(self, service, timeout, *, commit=None, ref=None):
        native = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        require(Path(self.options.powershell).resolve() == native.resolve(), "windows_native_powershell_required")
        require(service in ("Infrastructure", "Mcp"), "unknown_session_service")
        require(service != "Mcp" or (bool(commit) and bool(ref)), "mcp_service_source_binding_required")
        options = self.options
        # The waiter starts a separate native task. Neither OBS nor the HTTP
        # listener can inherit this bootstrap's command handles or AppData view.
        arguments = [str(native), "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass",
                     "-File", str(self.source / "Tools/Start-FF7RSession.ps1"), "-Start" + service,
                     "-Model", options.model, "-Effort", options.effort, "-Driver", options.driver,
                     "-Mode", options.mode.capitalize(), "-RepositoryRoot", options.repository_root,
                     "-RuntimePython", os.sys.executable, "-TimeoutSeconds", str(timeout)]
        if commit:
            arguments += ["-ExpectedCommit", commit, "-ExpectedRef", ref]
        result = await self.process(arguments, timeout + 5, name=service.lower() + "_service_task")
        for line in result.stdout.splitlines():
            if line.startswith("SESSION_SERVICE_"):
                print(line, flush=True)
        require("BLOCKER=service_task_timeout" not in result.stdout.splitlines(), "service_task_timeout:" + service.lower())
        require(result.returncode == 0, "native_service_action_failed:" + service.lower() + ":exit_" + str(result.returncode))
        require("SESSION_SERVICE_READY=true" in result.stdout.splitlines(), "service_task_receipt_missing:" + service.lower())

    async def ensure_infrastructure(self):
        options = self.options
        root = Path(options.repository_root)
        paths = await self.inspect("paths")
        os.environ["BRIDGE_STATE_DIR"] = paths["state_dir"]
        os.environ["FF7R_PUBLIC_STREAM_EVENT_DIR"] = paths["event_dir"]
        if options.check_only:
            return
        print("Checking OBS, monitor and MCP...", flush=True)
        # Do not restart or terminate any running process.
        await self.start_service_task("Infrastructure", 60)
        def listening():
            try:
                with socket.create_connection(("127.0.0.1", 8765), 1):
                    return True
            except OSError:
                return False
        if not await asyncio.to_thread(listening):
            commit = options.expected_commit
            if not commit:
                head = await self.process(["git", "-C", str(root), "rev-parse", "HEAD"], 10, name="service_checkout_head")
                require(head.returncode == 0 and bool(head.stdout.strip()), "service_checkout_head_unavailable")
                commit = head.stdout.strip()
            ref = options.expected_ref or commit
            await self.start_service_task("Mcp", 90, commit=commit, ref=ref)

    async def _start_session_streamer(self, mode):
        script = self.source / "Tools/Start-FF7RSession.ps1"
        arguments = [self.options.powershell, "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
                     "-ExecutionPolicy", "Bypass",
                     "-File", str(script), "-NativeWorker", "-StartSpeech", "-Model", self.options.model,
                     "-Effort", self.options.effort, "-Mode", mode.capitalize()]
        result = await self.process(arguments, 30, name="recording_context_start" if mode == "offline" else "speech_service_start")
        require(result.returncode == 0, "recording_context_start_failed" if mode == "offline" else "speech_service_start_failed")

    async def start_speech(self):
        await self._start_session_streamer(self.options.mode)

    async def start_recording_context(self):
        await self._start_session_streamer("offline")

    async def declare_driver(self):
        arguments = [self.options.node, str(Path(self.options.runtime_root) / "dist/streamer.js"), "driver-set",
                     "--driver", self.options.driver, "--model", self.options.model, "--effort", self.options.effort]
        if self.options.mode in ("rehearsal", "offline"):
            arguments += ["--rehearsal"]
        if self.options.mode == "offline":
            arguments += ["--silent"]
        result = await self.process(arguments, 25, name="driver_declaration")
        require(result.returncode == 0, "driver_declaration_failed")

    async def readiness_checks(self):
        checks = [("obs.ps1", "monitorRunning=true")]
        if self.options.mode != "offline":
            checks.append(("streamer.ps1", "localSpeechReady=true"))
        for script, required_line in checks:
            command = [self.options.powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                       str(Path(self.options.runtime_root) / script), "status" if script == "obs.ps1" else "check"]
            result = await self.process(command, 30, name=script + "_readiness")
            require(result.returncode == 0 and required_line in result.stdout.splitlines(), script + "_readiness_failed")


def verify_identity(identity, options):
    # Join a healthy existing runtime by default. An explicit repository binding
    # or feature ref/commit still fails closed; never silently restart a peer.
    root = Path(options.repository_root if options.bind_repository else identity.get("repository_root", ""))
    head = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True, timeout=10,
                                   **no_console_window_kwargs()).strip()
    dirty = subprocess.check_output(["git", "-C", str(root), "status", "--porcelain"], text=True, timeout=10,
                                    **no_console_window_kwargs()).strip()
    require(not dirty, "runtime_checkout_dirty")
    require(identity.get("git_dirty") is False and identity.get("git_commit_sha") == (options.expected_commit or head) == head,
            "runtime_commit_mismatch")
    require(Path(identity.get("repository_root", "")).resolve() == root.resolve(), "runtime_repository_mismatch")
    require(identity.get("runtime_profile_verification", {}).get("ok") is True, "runtime_binding_unverified")
    require(os.path.normcase(identity.get("python_executable", "")) == os.path.normcase(os.sys.executable), "runtime_python_mismatch")
    if options.expected_ref:
        require(identity.get("expected_feature_ref") == options.expected_ref, "runtime_ref_mismatch")
    require(not identity.get("bridge_status", {}).get("active_async_request"), "another_input_job_is_running")


def verify_driver(run, options):
    driver = run.get("driver") or {}
    require(driver.get("driver") == options.driver and driver.get("model_id") == options.model and driver.get("effort") == options.effort,
            "harness_driver_does_not_match")
    state = run.get("driver_state") or {}
    require(state.get("stream_live" if options.mode == "live" else "rehearsal_live") is True, "harness_stream_mode_unverified")
    assignment = run.get("assignment") or {}
    require(assignment.get("model_id") == options.model and isinstance(assignment.get("slot"), int)
            and assignment["slot"] not in (0, 14), "model_run_unassigned")


def verify_loaded(run):
    loaded = run.get("loaded_save") or {}
    process = loaded.get("process") or {}
    require(run.get("run_loaded") is True and run.get("loaded_save_verified") is True and bool(loaded.get("load_id"))
            and loaded.get("slot") == run["assignment"]["slot"] and run.get("profile_id")
            and process.get("pid") and process.get("token"), "model_save_receipt_unverified")
    return loaded


async def startup(call, services, options, *, sleep=asyncio.sleep, clock=time.monotonic, identity_validator=verify_identity):
    deadline = clock() + options.timeout
    async def narrate(lines):
        line_index = 0
        for _ in range(3):
            speech = await call("say_on_stream", {"text": lines[line_index], "tone": "calm"})
            require((speech.get("previous_delivery") or {}).get("status") in (None, "media_completed"), "startup_speech_failed")
            if speech.get("reason") == "repetitive":
                line_index = min(line_index + 1, len(lines) - 1)
                continue
            if speech.get("reason") == "rate_limited":
                await sleep(float(speech.get("retry_after_seconds", 8)))
                continue
            require(speech.get("ok") is True, "startup_speech_failed")
            return
        raise StartupBlocked("startup_speech_rate_limited")

    # Mandatory order, with no input or narration before identity/ownership checks.
    identity = await call("get_runtime_identity")
    lifecycle = await call("get_lifecycle_status")
    health = await call("get_session_health")
    observation = await call("observe_game")
    identity_validator(identity, options)
    bootstrap = await call("get_bootstrap_verification")
    require(bootstrap.get("ok") is True and not bootstrap.get("runtime_ownership", {}).get("conflicts"), "runtime_ownership_unverified")
    lease = await call("get_control_session_status")
    control_recovery = None
    if (lease.get("active") and lease.get("state") == "OPEN"
            and lease.get("lifetime") == "wall_clock" and lease.get("game_pid") is not None
            and health.get("process_exists") is False):
        # A still-running worker does not make its dead game's legacy lease a
        # live peer. Recovery must precede adoption/foreign-client rejection.
        bridge_status = identity.get("bridge_status") or {}
        require("active_async_request" in bridge_status
                and bridge_status["active_async_request"] is None, "another_input_job_is_running")
        require(lease.get("owner_matches") is True
                and lifecycle.get("owned_process_running") is False
                and (lifecycle.get("run_context") or {}).get("pid") == lease.get("game_pid"),
                "legacy_control_session_exit_unverified")
        require(not options.check_only, "legacy_control_session_recovery_required")
        control_recovery = await call("recover_stale_lifecycle", {"expected_control_session": lease})
        recovered_control = control_recovery.get("control_session") or {}
        require(control_recovery.get("recovered") is True and recovered_control.get("closed") is True
                and bool(recovered_control.get("raw_archive_path")), "legacy_control_session_recovery_failed")
        lease = await call("get_control_session_status")
        require(lease.get("active") is False and lease.get("reason") == "no_session",
                "control_session_changed_after_recovery")
        lifecycle = {**lifecycle, "lease": None}
    require(not lease.get("active") or lease.get("client_id") == options.client_id, "another_client_owns_control")
    print("Harness identity and ownership verified.", flush=True)
    # OBS and the scheduled monitor can take a short time to initialize after
    # login. Retry service readiness only; this sends no game input/probe reads.
    for attempt in range(20):
        try:
            status = await services.inspect()
            break
        except StartupBlocked as error:
            transient = str(error).startswith(("obs_connection_refused", "obs_check_timeout"))
            if options.check_only or not transient or attempt == 19:
                raise
            require(clock() < deadline, "obs_startup_timeout")
            await sleep(2)
    if options.mode == "offline":
        require(status.get("monitorRunning") is True, "monitor_heartbeat_stale")
        require(status.get("legalNoticeReady") is True, "legalNoticeReady=false")
    else:
        for name in ("localSpeechReady", "voiceInputReady", "statsInputReady", "legalNoticeReady"):
            require(status.get(name) is True, name + "=false")
    if not options.check_only:
        if options.mode in ("rehearsal", "offline"):
            if options.mode == "offline":
                require(status.get("streaming") is False, "offline_stream_state_unverified")
            else:
                require(not status.get("streaming"), "rehearsal_refused_while_streaming")
            if not status.get("recording"):
                status = await services.inspect("record")
            if options.mode == "offline":
                require(status.get("recording") is True, "offline_recording_not_ready")
        else:
            require(status.get("streaming") is True, "obs_not_streaming:use_Offline_for_an_offline_session")
    if options.mode == "offline":
        if options.check_only:
            require(status.get("recording") is True, "offline_recording_not_ready")
            require(status.get("contextReady") is True, "recording_context_not_ready")
        elif not status.get("contextReady"):
            require(not status.get("contextPresent"), "existing_recording_context_unhealthy")
            await services.start_recording_context()
            for _ in range(15):
                await sleep(1)
                status = await services.inspect()
                if status.get("contextReady"):
                    break
        require(status.get("contextReady") is True, "recording_context_not_ready")
    elif not options.check_only and not status.get("consumerReady"):
        require(not status.get("consumerPresent"), "existing_speech_consumer_unhealthy")
        await services.start_speech()
        for _ in range(15):
            await sleep(1)
            status = await services.inspect()
            if status.get("consumerReady"):
                break
    if options.mode != "offline":
        require(status.get("consumerReady") is True, "speech_consumer_not_ready")
        require(status.get("speechFailures", 0) < 2 and status.get("previousDelivery") in (None, "media_completed"), "speech_delivery_failed")
    require(status.get("monitorRunning") is True, "monitor_heartbeat_stale")
    await services.readiness_checks()
    if options.check_only:
        run = await call("get_queens_blood_run_status")
        print("CHECK_ONLY=true", flush=True)
        require(health.get("gameplay_ready") is True and status.get("monitorSeesGame") is True, "game_not_ready")
        verify_driver(run, options)
        verify_loaded(run)
        return {"ready": True, "check_only": True, "slot": run["assignment"]["slot"]}
    # A foreign lease was already rejected before changing the shared declaration.
    await services.declare_driver()
    run = await call("get_queens_blood_run_status")
    verify_driver(run, options)
    load_needed = run.get("run_loaded") is not True or run.get("loaded_save_verified") is not True
    input_started = False
    job_id = None
    job_running = False
    try:
        if load_needed:
            if health.get("process_exists") is False and lifecycle.get("lease"):
                await call("recover_stale_lifecycle")
            if options.mode != "offline":
                line = "I'll start the game and load our saved journey." if health.get("process_exists") is False else "I'll load our saved journey before we carry on."
                await narrate([line, "Let's return to our saved journey.", "I'll get our saved game ready to continue."])
            input_started = True
            job_running = True
            job = await call("start_model_run_async", {"model_id": options.model, "reasoning_effort": options.effort,
                                                       "client_id": options.client_id})
            job_id = job.get("job_id")
            require(bool(job_id), "model_load_job_missing")
            print("Loading the registered model save...", flush=True)
            while True:
                # Only cached async status while load input is running.
                result = await call("get_start_model_run_status", {"job_id": job_id})
                if result.get("state") in ("completed", "failed", "cancelled"):
                    job_running = False
                    require(result["state"] == "completed", "model_load_job_failed")
                    nested = payload(result.get("response"))
                    require(nested.get("ok") is True, "model_load_failed")
                    break
                require(clock() < deadline, "model_load_timeout")
                await sleep(2)
            run = await call("get_queens_blood_run_status")
        verify_driver(run, options)
        loaded = verify_loaded(run)
        health = await call("get_session_health")
        if health.get("diagnosis") == "foreground_missing":
            if options.mode != "offline":
                await narrate(["Let's return to Cloud and continue from here.", "Back to our journey.", "Let's pick up where we left off."])
            await call("focus_game_window", {"pid": int(loaded["process"]["pid"]), "wake_display": True})
            health = await call("get_session_health")
            require(health.get("diagnosis") != "foreground_missing", "game_focus_denied:alt_tab_out_and_back")
        # Focus loss invalidates the settled proof. Passive health reads cannot
        # recreate it; use the existing bounded, input-free field settling gate.
        if health.get("gameplay_ready") is not True:
            require(health.get("process_exists") is not False, "game_process_exited")
            settled = await call("wait_for_settled_gameplay", {"timeout_seconds": 15})
            require(settled.get("ok") is True, "loaded_gameplay_not_settled")
            health = await call("get_session_health")
        require(health.get("gameplay_ready") is True, "loaded_gameplay_not_ready")
        # Wait for monitor's existing heartbeat, without polling the game/probes.
        for _ in range(10):
            status = await services.inspect()
            if status.get("monitorRunning") and status.get("monitorSeesGame"):
                break
            await sleep(1)
        require(status.get("monitorRunning") and status.get("monitorSeesGame"), "monitor_does_not_see_game")
        # An async model load retains a lease when given an explicit client ID.
        # Refresh the pre-load snapshot instead of trying to open a second lease.
        lease = await call("get_control_session_status")
        require(not lease.get("active") or lease.get("client_id") == options.client_id, "another_client_owns_control")
        game_pid = int(loaded["process"]["pid"])
        if lease.get("active") and lease.get("game_pid") is None:
            # Cold title startup can retain a lease opened before the process
            # exists. Replace only our correctly attributed, unbound lease,
            # after the loaded-save receipt proves the actual game process.
            require(lease.get("owner_matches") is True and lease.get("recording_driver") ==
                    {"modelId": options.model, "effort": options.effort}, "control_session_unverified")
            closed = await call("close_control_session", {"client_id": options.client_id})
            require(closed.get("closed") is True, "unbound_control_session_close_failed")
            lease = {"active": False}
        if not lease.get("active"):
            if lease.get("state") in ("EXPIRED", "CLOSED", "GAME_EXITED") and lease.get("owner_matches") is True:
                await call("close_control_session")
            # The session lasts as long as this game; `seconds` is ignored.
            lease = await call("open_control_session", {"seconds": 1800, "game_pid": game_pid,
                "model_id": options.model, "reasoning_effort": options.effort, "client_id": options.client_id})
        require(lease.get("active") and lease.get("client_id") == options.client_id
                and lease.get("game_pid") == game_pid
                and lease.get("recording_driver") == {"modelId": options.model, "effort": options.effort}, "control_session_unverified")
        campaign = await call("get_campaign_status")
        next_step = await call("get_campaign_next_step")
        return {"ready": True, "mode": options.mode, "model": options.model, "effort": options.effort,
                "client_id": options.client_id, "slot": loaded["slot"], "loaded_save_receipt": loaded["load_id"],
                "game_pid": game_pid, "expected_commit": identity["git_commit_sha"],
                "expected_ref": identity.get("expected_feature_ref"), "repository_root": identity["repository_root"],
                "control_session": lease, "control_recovery": control_recovery,
                "campaign_next_step": next_step, "campaign_status": campaign.get("status")}
    except BaseException:
        if job_running:
            # Emergency release never waits for narration; no automatic input retry.
            await call("stop_all_input")
        # Close only a lease owned by this invocation, never another client.
        current = await call("get_control_session_status")
        if input_started and not job_running and (not current.get("active") or current.get("client_id") == options.client_id):
            await call("stop_all_input")
        if current.get("active") and current.get("client_id") == options.client_id:
            await call("close_control_session")
        raise


async def main(options):
    require(options.check_only or (bool(options.model.strip()) and bool(options.effort.strip())), "exact_model_and_effort_required")
    services = NativeServices(options)
    async with asyncio.timeout(options.timeout):
        await services.ensure_infrastructure()
        async with connect("http://127.0.0.1:8765/mcp") as call:
            result = await startup(call, services, options)
    print("SESSION_READY=true", flush=True)
    # Single stdout handoff; no additional JSON ledger or repeated game polling.
    print(json.dumps(result, ensure_ascii=True), flush=True)


def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("repository-root", "runtime-root", "node", "powershell", "client-id"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--model", default="")
    parser.add_argument("--effort", default="")
    parser.add_argument("--driver", choices=("ChatGPT", "Claude", "Gemini", "Grok"), default="ChatGPT")
    parser.add_argument("--mode", choices=("live", "rehearsal", "offline"), default="live")
    parser.add_argument("--expected-commit")
    parser.add_argument("--expected-ref")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--bind-repository", action="store_true")
    options = parser.parse_args()
    try:
        asyncio.run(main(options))
    except (Exception, KeyboardInterrupt) as error:
        print("SESSION_READY=false", flush=True)
        # Arbitrary transport/config errors can contain secrets. Only our stable codes are printed.
        print("BLOCKER=" + failure_reason(error), flush=True)
        raise SystemExit(1)


if __name__ == "__main__":
    cli()
