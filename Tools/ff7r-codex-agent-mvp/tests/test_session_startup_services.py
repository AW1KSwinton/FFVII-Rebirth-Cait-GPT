"""Offline only: synthetic helper timing and mocked Windows scheduler cmdlets.

No fixture starts OBS, a monitor, MCP, a streamer, or a real scheduled task.
The inherited-handle helper writes a marker and exits by itself.
"""
import asyncio
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from ff7r_agent.session_startup import NativeServices, StartupBlocked, verify_identity


NATIVE_PS = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
BASE_PYTHON = getattr(sys, "_base_executable", sys.executable)
SOURCE = Path(__file__).resolve().parents[3]


def service_options(tmp_path, **changes):
    values = dict(model="gpt-6.1-sol", effort="max", driver="ChatGPT", mode="rehearsal", client_id="offline-test",
                  repository_root=str(tmp_path), runtime_root=str(tmp_path / "installed-fixture"),
                  powershell=str(NATIVE_PS), check_only=False, expected_commit="a" * 40, expected_ref="codex/offline-test",
                  bind_repository=True)
    values.update(changes)
    return SimpleNamespace(**values)


@pytest.fixture
def inherited_helper(tmp_path):
    script = tmp_path / "harmless_helper.py"
    child_done = tmp_path / "child.done"
    parent_done = tmp_path / "parent.done"
    ready = tmp_path / "parent.ready"
    script.write_text('''import os, subprocess, sys, time
from pathlib import Path
if sys.argv[1] == "child":
    time.sleep(4)  # Synthetic timing; this is not a replay of the live stall.
    Path(sys.argv[2]).write_text("child finished")
    print("late helper output", flush=True)
else:
    child = subprocess.Popen([sys.executable, __file__, "child", sys.argv[2]], close_fds=False)
    print("parent output", flush=True)
    print("parent diagnostic", file=sys.stderr, flush=True)
    Path(sys.argv[4]).write_text(str(os.getpid()))
    if sys.argv[1] == "linger":
        time.sleep(4)
    Path(sys.argv[3]).write_text("parent finished")
''', encoding="utf-8")
    yield [BASE_PYTHON, str(script), "exit", str(child_done), str(parent_done), str(ready)], child_done, parent_done, ready
    # Let only our self-terminating dummy finish before pytest removes its folder.
    if ready.exists():
        deadline = time.monotonic() + 6
        while not child_done.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert child_done.read_text() == "child finished"


def test_command_returns_before_descendant_closes_inherited_output(inherited_helper, capsys):
    arguments, child_done, parent_done, _ = inherited_helper
    started = time.monotonic()
    result = asyncio.run(NativeServices(None).process(arguments, timeout=2, name="dummy_parent_exit"))
    assert time.monotonic() - started < 3
    assert result.returncode == 0 and "parent output" in result.stdout and "parent diagnostic" in result.stderr
    assert parent_done.exists() and not child_done.exists()
    assert "SERVICE_COMMAND_DONE=dummy_parent_exit" in capsys.readouterr().out


def test_command_timeout_is_bounded_with_surviving_descendant(inherited_helper, capsys):
    arguments, child_done, parent_done, _ = inherited_helper
    arguments[2] = "linger"
    started = time.monotonic()
    with pytest.raises(StartupBlocked, match="service_command_timeout:dummy_linger"):
        asyncio.run(NativeServices(None).process(arguments, timeout=0.6, name="dummy_linger"))
    assert time.monotonic() - started < 3
    assert not parent_done.exists() and not child_done.exists()
    assert "SERVICE_COMMAND_ABORT=dummy_linger" in capsys.readouterr().out


def test_overall_cancellation_does_not_wait_for_executor_pipe_drain(inherited_helper):
    arguments, child_done, parent_done, ready = inherited_helper
    arguments[2] = "linger"

    async def cancel_waiter():
        task = asyncio.create_task(NativeServices(None).process(arguments, timeout=30, name="dummy_cancel"))
        deadline = time.monotonic() + 3
        while not ready.exists() and not task.done() and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        assert ready.exists()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    started = time.monotonic()
    asyncio.run(cancel_waiter())
    assert time.monotonic() - started < 3
    assert not parent_done.exists() and not child_done.exists()


def test_command_diagnostics_do_not_print_arguments_or_captured_private_output(capsys):
    result = asyncio.run(NativeServices(None).process(
        [BASE_PYTHON, "-c", "import sys; print('private-output'); sys.exit(7)", "private-argument"], name="dummy_failure"))
    output = capsys.readouterr().out
    assert result.returncode == 7 and result.stdout.strip() == "private-output"
    assert "exit_code=7" in output and "private-output" not in output and "private-argument" not in output


def test_unexpected_large_command_output_is_bounded():
    with pytest.raises(StartupBlocked, match="service_command_output_limit:dummy_large"):
        asyncio.run(NativeServices(None).process([BASE_PYTHON, "-c", "print('x' * (1024 * 1024 + 1))"], name="dummy_large"))


@pytest.mark.parametrize("returncode,stdout,blocker", [
    (1, "SESSION_SERVICE_READY=true\n", "native_service_action_failed:mcp:exit_1"),
    (0, "SESSION_SERVICE_READY=false\n", "service_task_receipt_missing:mcp"),
    (1, "SESSION_SERVICE_READY=false\nBLOCKER=service_task_timeout\n", "service_task_timeout:mcp"),
])
def test_task_exit_alone_is_not_service_success(tmp_path, returncode, stdout, blocker):
    services = NativeServices(service_options(tmp_path))
    calls = []

    async def process(arguments, timeout, **kwargs):
        calls.append((arguments, timeout, kwargs))
        return subprocess.CompletedProcess(arguments, returncode, stdout, "private failure")

    services.process = process
    with pytest.raises(StartupBlocked, match=blocker):
        asyncio.run(services.start_service_task("Mcp", 90, commit="a" * 40, ref="codex/offline-test"))
    arguments, timeout, _ = calls[0]
    assert arguments[0] == str(NATIVE_PS) and "-NativeWorker" not in arguments
    assert arguments[arguments.index("-ExpectedCommit") + 1] == "a" * 40
    assert arguments[arguments.index("-ExpectedRef") + 1] == "codex/offline-test"
    assert timeout == 95


def test_bundled_powershell_is_refused_before_starting_service(tmp_path):
    services = NativeServices(service_options(tmp_path, powershell=str(tmp_path / "codex" / "pwsh.exe")))

    async def forbidden(*args, **kwargs):
        pytest.fail("No command may run with the bundled service host")

    services.process = forbidden
    with pytest.raises(StartupBlocked, match="windows_native_powershell_required"):
        asyncio.run(services.start_service_task("Infrastructure", 60))


def test_mcp_task_requires_both_source_commit_and_ref_before_dispatch(tmp_path):
    services = NativeServices(service_options(tmp_path))
    with pytest.raises(StartupBlocked, match="mcp_service_source_binding_required"):
        asyncio.run(services.start_service_task("Mcp", 90, commit="a" * 40))


def test_failed_waiter_preserves_task_coordinates_without_private_output(capsys):
    script = "import time; print('SESSION_SERVICE_TASK=fixture-task', flush=True); print('SESSION_SERVICE_LOG=fixture.log', flush=True); print('private detail', flush=True); time.sleep(4)"
    with pytest.raises(StartupBlocked, match="service_command_timeout:mcp_service_task"):
        asyncio.run(NativeServices(None).process([BASE_PYTHON, "-c", script], timeout=0.6, name="mcp_service_task"))
    output = capsys.readouterr().out
    assert "SESSION_SERVICE_TASK=fixture-task" in output and "SESSION_SERVICE_LOG=fixture.log" in output
    assert "private detail" not in output


@pytest.mark.parametrize("check_only,listening,expected", [
    (True, False, []), (False, True, [("Infrastructure", 60, {})]),
    (False, False, [("Infrastructure", 60, {}), ("Mcp", 90, {"commit": "a" * 40, "ref": "codex/offline-test"})]),
])
def test_infrastructure_uses_tasks_and_keeps_exact_feature_binding(tmp_path, monkeypatch, check_only, listening, expected):
    services = NativeServices(service_options(tmp_path, check_only=check_only))
    tasks = []
    monkeypatch.setenv("BRIDGE_STATE_DIR", "fixture-before")
    monkeypatch.setenv("FF7R_PUBLIC_STREAM_EVENT_DIR", "fixture-before")

    async def inspect(operation):
        assert operation == "paths"
        return {"state_dir": str(tmp_path / "state"), "event_dir": str(tmp_path / "events")}

    async def start_service_task(service, timeout, **kwargs):
        tasks.append((service, timeout, kwargs))

    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): pass

    def connect(*args):
        if not listening:
            raise OSError("synthetic closed port")
        return Connection()

    services.inspect = inspect
    services.start_service_task = start_service_task
    monkeypatch.setattr("ff7r_agent.session_startup.socket.create_connection", connect)
    asyncio.run(services.ensure_infrastructure())
    assert tasks == expected
    assert os.environ["FF7R_PUBLIC_STREAM_EVENT_DIR"] == str(tmp_path / "events")


@pytest.mark.parametrize("change,blocker", [
    ({"git_commit_sha": "b" * 40}, "runtime_commit_mismatch"),
    ({"git_dirty": True}, "runtime_commit_mismatch"),
    ({"repository_root": "different-root"}, "runtime_repository_mismatch"),
    ({"runtime_profile_verification": {"ok": False}}, "runtime_binding_unverified"),
    ({"python_executable": "other-python"}, "runtime_python_mismatch"),
    ({"expected_feature_ref": "other-ref"}, "runtime_ref_mismatch"),
    ({"bridge_status": {"active_async_request": {"job_id": "another-job"}}}, "another_input_job_is_running"),
])
def test_source_and_input_job_identity_gates_remain_fail_closed(tmp_path, monkeypatch, change, blocker):
    identity = dict(repository_root=str(tmp_path), git_commit_sha="a" * 40, git_dirty=False,
                    runtime_profile_verification={"ok": True}, python_executable=sys.executable,
                    expected_feature_ref="codex/offline-test", bridge_status={})
    identity.update(change)

    def git(arguments, **kwargs):
        assert kwargs["timeout"] == 10
        return "a" * 40 if "rev-parse" in arguments else ""

    monkeypatch.setattr("ff7r_agent.session_startup.subprocess.check_output", git)
    with pytest.raises(StartupBlocked, match=blocker):
        verify_identity(identity, service_options(tmp_path))


def ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


@pytest.mark.skipif(os.name != "nt" or not NATIVE_PS.exists(), reason="Windows-native PowerShell fixture")
@pytest.mark.parametrize("service,state,receipt_code", [
    ("Infrastructure", "Ready", 0), ("Mcp", "Running", 0), ("Mcp", "Ready", 1), ("Mcp", "Running", None),
])
def test_native_task_workflow_with_mocked_scheduler_and_synthetic_clock(tmp_path, service, state, receipt_code):
    # Execute the real dispatch/wait/cleanup path; every scheduler and service
    # cmdlet is replaced. Only fake receipts in this temporary folder are written.
    wrapper = tmp_path / "scheduler_fixture.ps1"
    root = tmp_path / "source's checkout"
    root.mkdir()
    receipt = "$null" if receipt_code is None else str(receipt_code)
    wrapper.write_text('''$ErrorActionPreference='Stop'
$global:fixtureState = ''' + ps_quote(state) + '''
$global:fixtureReceipt = ''' + receipt + '''
$global:fixtureTick = 0
function global:Get-Date {
    $global:fixtureTick += 1
    # Synthetic 70-second ticks exercise the real 60-second receipt deadline.
    return [datetime]'2026-10-13T12:00:00Z' + [timespan]::FromSeconds(70 * $global:fixtureTick)
}
function global:Start-Sleep { param($Seconds,$Milliseconds) }
function global:Start-Process { throw 'Actual process launch is forbidden in this fixture' }
function global:Stop-ScheduledTask { throw 'Stopping any task is forbidden in this fixture' }
function global:New-ScheduledTaskAction {
    param($Execute,$Argument,$WorkingDirectory)
    [pscustomobject]@{Execute=$Execute; Arguments=$Argument; WorkingDirectory=$WorkingDirectory}
}
function global:New-ScheduledTaskPrincipal { param($UserId,$LogonType,$RunLevel) [pscustomobject]@{LogonType=$LogonType; RunLevel=$RunLevel} }
function global:New-ScheduledTaskSettingsSet {
    param($ExecutionTimeLimit,$MultipleInstances,[switch]$AllowStartIfOnBatteries,[switch]$DontStopIfGoingOnBatteries)
    [pscustomobject]@{Limit=$ExecutionTimeLimit.TotalSeconds; Instances=$MultipleInstances}
}
function global:Register-ScheduledTask {
    param($TaskName,$Action,$Principal,$Settings,$Trigger)
    if ($Trigger) { throw 'Unexpected recurring/logon trigger' }
    $global:fixtureAction=$Action
    $global:fixtureMetadata=@{Name=$TaskName; Action=$Action; Principal=$Principal; Settings=$Settings}
}
function global:Start-ScheduledTask {
    param($TaskName)
    if ($TaskName -notlike 'FF7R Session Service *') { throw 'A real service task must never be started' }
    Write-Output ('FIXTURE_ACTION=' + ($global:fixtureMetadata | ConvertTo-Json -Compress -Depth 5))
    $encoded = ($global:fixtureAction.Arguments -split ' ')[-1]
    $command = [Text.Encoding]::Unicode.GetString([Convert]::FromBase64String($encoded))
    $tokens=$null; $errors=$null
    $ast=[System.Management.Automation.Language.Parser]::ParseInput($command,[ref]$tokens,[ref]$errors)
    if ($errors.Count) { throw 'Invalid generated task command' }
    $elements=$ast.EndBlock.Statements[0].PipelineElements[0].CommandElements
    $values=@{}
    for ($index=1; $index -lt $elements.Count; $index++) {
        if ($elements[$index] -is [System.Management.Automation.Language.CommandParameterAst] -and $index+1 -lt $elements.Count -and $elements[$index+1] -is [System.Management.Automation.Language.StringConstantExpressionAst]) {
            $values[$elements[$index].ParameterName]=$elements[$index+1].Value
        }
    }
    if ($null -ne $global:fixtureReceipt) {
        $(if ($global:fixtureReceipt -eq 0) {'SESSION_SERVICE_READY=true'} else {'SESSION_SERVICE_READY=false'}) | Set-Content -LiteralPath $values.ResultLog
        [string]$global:fixtureReceipt | Set-Content -LiteralPath $values.ExitFile
    }
    Write-Output ('FIXTURE_FILES=' + (@{Log=$values.ResultLog; Exit=$values.ExitFile} | ConvertTo-Json -Compress))
}
function global:Get-ScheduledTask { param($TaskName,$ErrorAction) [pscustomobject]@{State=$global:fixtureState; TaskName=$TaskName} }
function global:Unregister-ScheduledTask { param($TaskName,$Confirm) Write-Output ('FIXTURE_UNREGISTER=' + $TaskName) }
& ''' + ps_quote(SOURCE / "Tools/Start-FF7RSession.ps1") + " -Start" + service + " -Model gpt-6.1-sol -Effort max -Mode Rehearsal -RepositoryRoot " + ps_quote(root) + " -ExpectedCommit " + ps_quote("a" * 40) + " -ExpectedRef codex/offline-test -TimeoutSeconds 60\nexit $LASTEXITCODE\n", encoding="utf-8")
    result = subprocess.run([str(NATIVE_PS), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(wrapper)],
                            capture_output=True, text=True, timeout=15)
    lines = result.stdout.splitlines()
    assert "FIXTURE_ACTION=" in result.stdout, result.stdout + result.stderr
    action = json.loads(next(line.removeprefix("FIXTURE_ACTION=") for line in lines if line.startswith("FIXTURE_ACTION=")))
    files = json.loads(next(line.removeprefix("FIXTURE_FILES=") for line in lines if line.startswith("FIXTURE_FILES=")))
    command = base64.b64decode(action["Action"]["Arguments"].split()[-1]).decode("utf-16le")
    assert Path(action["Action"]["Execute"]) == NATIVE_PS
    assert action["Settings"] == {"Limit": 0, "Instances": "IgnoreNew"}
    assert action["Principal"] == {"LogonType": "Interactive", "RunLevel": "Limited"}
    assert "-WindowStyle Hidden" in action["Action"]["Arguments"]
    assert "-NativeWorker" in command and "-Start" + service in command
    assert ps_quote(root) in command and "-ExpectedCommit '" + "a" * 40 + "'" in command
    assert "-ExpectedRef 'codex/offline-test'" in command
    assert result.returncode == (0 if receipt_code == 0 else 1), result.stderr
    assert any(line.startswith("SESSION_SERVICE_LOG=") for line in lines)
    assert ("FIXTURE_UNREGISTER=" in result.stdout) == (state == "Ready")
    if receipt_code is None:
        assert "BLOCKER=service_task_timeout" in lines and not Path(files["Exit"]).exists()
    else:
        assert Path(files["Log"]).exists()  # Diagnostics survive cleanup.
        assert Path(files["Exit"]).exists() == (state == "Running")


@pytest.mark.skipif(os.name != "nt" or not NATIVE_PS.exists(), reason="Windows-native PowerShell fixture")
@pytest.mark.parametrize("service,already_running,refused", [
    ("Infrastructure", False, False), ("Infrastructure", True, False), ("Mcp", False, False), ("Mcp", False, True),
])
def test_native_worker_service_paths_with_only_synthetic_configuration(tmp_path, service, already_running, refused):
    # The real worker resolves a fake installed release in a process-local fake
    # AppData. Its node and HTTP launchers are fixture functions/scripts; all
    # OBS/monitor actions are mocked. No real service configuration is accessed.
    appdata = tmp_path / "fake-appdata"
    bridge = appdata / "Codex/OBS Bridge"
    release = tmp_path / "installed-fixture"
    root = tmp_path / "feature-fixture"
    for folder in (bridge / "runtime", bridge / "config", release, root / "Tools/ff7r-codex-agent-mvp"):
        folder.mkdir(parents=True)
    (bridge / "runtime/current.json").write_text(json.dumps({"runtimeRoot": str(release)}))
    for name in ("bridge.env", "streamer.env"):
        (bridge / "config" / name).write_text("SYNTHETIC_FIXTURE=true\n")
    for dependency in ("dotenv", "googleapis", "obs-websocket-js"):
        folder = release / "node_modules" / dependency
        folder.mkdir(parents=True)
        (folder / "package.json").write_text("{}")
    (release / "service-runtime.json").write_text(json.dumps({"nodeExe": "FixtureNode"}))
    launcher = root / "Tools/ff7r-codex-agent-mvp/Start-FF7RHttpMcp.ps1"
    launcher.write_text('''param($RuntimeProfile,$RepositoryRoot,$ExpectedCommitSha,$ExpectedRef,$RuntimePython)
Write-Output ('FIXTURE_BINDING=' + (@{Profile=$RuntimeProfile; Root=$RepositoryRoot; Commit=$ExpectedCommitSha; Ref=$ExpectedRef; Python=$RuntimePython; State=$env:BRIDGE_STATE_DIR; Events=$env:FF7R_PUBLIC_STREAM_EVENT_DIR; QbProfile=$env:FF7R_QUEEN_BLOOD_PROFILE; AppData=$env:LOCALAPPDATA} | ConvertTo-Json -Compress))
''' + ("throw 'private synthetic source refusal'\n" if refused else "$global:LASTEXITCODE=0\n"), encoding="utf-8")
    log = tmp_path / "worker.log"
    exit_file = tmp_path / "worker.exit"
    wrapper = tmp_path / "native_worker_fixture.ps1"
    wrapper.write_text('''$ErrorActionPreference='Stop'
$env:LOCALAPPDATA=''' + ps_quote(appdata) + '''
$global:fixtureCalls=@()
function global:Get-Process {
    param($Name,$ErrorAction)
    if ($Name -ne 'obs64') { throw 'Unexpected process inspection' }
''' + ("    [pscustomobject]@{Name='obs64'}\n" if already_running else "") + r'''}
function global:Start-Process {
    param($FilePath,$WorkingDirectory,$WindowStyle)
    if ($FilePath -ne 'C:\Program Files\obs-studio\bin\64bit\obs64.exe' -or $WorkingDirectory -ne 'C:\Program Files\obs-studio\bin\64bit' -or $WindowStyle -ne 'Hidden') { throw 'Unexpected OBS launch parameters' }
    $global:fixtureCalls += 'obs'
}
function global:Get-ScheduledTask {
    param($TaskName,$ErrorAction)
    if ($TaskName -ne 'Codex OBS Bridge Monitor') { throw 'Unexpected service task inspection' }
    [pscustomobject]@{TaskName=$TaskName; State=''' + ps_quote("Running" if already_running else "Ready") + '''}
}
function global:Start-ScheduledTask {
    param($TaskName)
    if ($TaskName -ne 'Codex OBS Bridge Monitor') { throw 'Unexpected service start' }
    $global:fixtureCalls += 'monitor'
}
function global:Stop-ScheduledTask { throw 'Stopping any service is forbidden' }
function global:FixtureNode {
    param($Script,$Runtime,$Operation,$Mode)
    if ($Script -ne ''' + ps_quote(SOURCE / "scripts/session-service-check.mjs") + ''' -or $Runtime -ne ''' + ps_quote(release) + ''' -or $Operation -ne 'paths' -or $Mode -ne 'rehearsal') { throw 'Unexpected service paths request' }
    @{ok=$true; state_dir=''' + ps_quote(tmp_path / "state") + '''; event_dir=''' + ps_quote(tmp_path / "state/public-stream-events") + '''} | ConvertTo-Json -Compress
    $global:LASTEXITCODE=0
}
& ''' + ps_quote(SOURCE / "Tools/Start-FF7RSession.ps1") + " -NativeWorker -Start" + service + " -Model gpt-6.1-sol -Effort max -Mode Rehearsal -RepositoryRoot " + ps_quote(root) + " -ExpectedCommit " + ps_quote("a" * 40) + " -ExpectedRef codex/offline-test -RuntimePython " + ps_quote(sys.executable) + " -ResultLog " + ps_quote(log) + " -ExitFile " + ps_quote(exit_file) + '''
$fixtureExit=$LASTEXITCODE
Write-Output ('FIXTURE_CALLS=' + (ConvertTo-Json -Compress -InputObject @($global:fixtureCalls)))
exit $fixtureExit
''', encoding="utf-8")
    result = subprocess.run([str(NATIVE_PS), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(wrapper)],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == (1 if refused else 0), result.stderr
    assert exit_file.read_text().strip() == str(result.returncode)
    calls = json.loads(next(line.removeprefix("FIXTURE_CALLS=") for line in result.stdout.splitlines() if line.startswith("FIXTURE_CALLS=")))
    assert calls == (["obs", "monitor"] if service == "Infrastructure" and not already_running else [])
    raw_log = log.read_bytes()
    content = raw_log.decode("utf-16" if raw_log.startswith(b"\xff\xfe") else "utf-8")
    assert ("SESSION_SERVICE_READY=true" in content) == (not refused)
    if service == "Mcp":
        binding = json.loads(next(line.removeprefix("FIXTURE_BINDING=") for line in content.splitlines() if line.startswith("FIXTURE_BINDING=")))
        assert binding == {"Profile": "FeatureValidation", "Root": str(root), "Commit": "a" * 40,
                           "Ref": "codex/offline-test", "Python": sys.executable, "State": str(tmp_path / "state"),
                           "Events": str(tmp_path / "state/public-stream-events"), "QbProfile": "queens_blood_campaign", "AppData": str(appdata)}
        if refused:
            assert "BLOCKER=native_service_worker_failed" in content
            assert "private synthetic source refusal" not in result.stdout
