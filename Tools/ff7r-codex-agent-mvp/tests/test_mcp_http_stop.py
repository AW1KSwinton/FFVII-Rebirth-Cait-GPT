"""Exercise the real stop script with isolated files and mocked OS processes."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.parametrize("defect", [None, "runtime_child", "missing_identity", "invalid_identity", "pid", "creation", "entrypoint", "transport", "protocol", "missing_token", "empty_token", "non_python"])
def test_stop_recovers_missing_launcher_pid_only_with_authenticated_listener(tmp_path, defect):
    shell = os.getenv("FF7R_TEST_PWSH") or shutil.which("pwsh") or shutil.which("powershell")
    if not shell:
        pytest.skip("PowerShell is required to exercise the Windows stop script")
    control = tmp_path / "ff7r-agent" / "mcp-http"
    control.mkdir(parents=True)
    # Known FILETIME for 2026-10-01T00:00:00Z; no real process is queried.
    identity = {
        "pid": 1234,
        "process_creation_token": "134352864000000000",
        "entrypoint": "ff7r_agent.mcp_http_server",
        "command_identity": "python -m ff7r_agent.mcp_http_server",
        "transport": "streamable-http",
        "protocol": "ff7r-http-shutdown-v1",
    }
    for key, value in {"pid": 5678, "creation": "old", "entrypoint": "unrelated", "transport": "stdio", "protocol": "unknown"}.items():
        if defect == key:
            identity["process_creation_token" if key == "creation" else key] = value
    identity_file = control / "server.identity.json"
    if defect != "missing_identity":
        identity_file.write_text("invalid" if defect == "invalid_identity" else json.dumps(identity), encoding="utf-8")
    if defect != "missing_token":
        (control / "server.token").write_text("" if defect == "empty_token" else "isolated-test-token", encoding="utf-8")
    assert not (control / "server.pid").exists()
    script = Path(__file__).parents[1] / "Stop-FF7RHttpMcp.ps1"
    harness = tmp_path / "mock-stop.ps1"
    harness.write_text(r"""
param([string]$StopScript, [string]$Defect)
$ErrorActionPreference = 'Stop'
$script:processQueries = 0
function Get-Process {
    param($Id, $ErrorAction)
    $script:processQueries++
    # Recovery, normal validation, then the authenticated request succeeds.
    if ($script:processQueries -gt 2) {
        $request = Join-Path $env:FF7R_MCP_HTTP_RUNTIME_DIR 'shutdown.request'
        if (-not (Test-Path -LiteralPath $request)) { throw 'Process disappeared before authenticated request' }
        $json = Get-Content -LiteralPath $request -Raw | ConvertFrom-Json
        if ($json.pid -ne 1234 -or $json.token -ne 'isolated-test-token') { throw 'Invalid shutdown request' }
        return $null
    }
    [pscustomobject]@{
        Id = 1234
        Path = $(if ($Defect -eq 'non_python') { 'C:\unrelated.exe' } else { 'C:\Python313\python.exe' })
        StartTime = [datetime]::Parse('2026-10-01T00:00:00Z').ToUniversalTime()
        CommandLine = 'python.exe -m ff7r_agent.mcp_http_server'
    }
}
function Get-NetTCPConnection {
    param($LocalAddress, $LocalPort, $State, $ErrorAction)
    if ($script:processQueries -gt 2) { throw 'No listener' }
    [pscustomobject]@{ OwningProcess = 1234 }
}
function Get-CimInstance {
    param($ClassName, $Filter, $ErrorAction)
    # A gamepad broker under the server, gone once it has stopped.
    if ($Defect -ne 'runtime_child') { return @() }
    $child = [pscustomobject]@{ ProcessId = 4321; ParentProcessId = 1234; CreationDate = '20261001000000.000000+000'
        CommandLine = 'python.exe -m ff7r_agent.gamepad_broker serve --owner-pid 1234' }
    if ($Filter) { if ($script:processQueries -gt 2) { return $null }; return $child }
    return @($child)
}
function netstat { }
function Start-Sleep { param($Milliseconds) }
function Stop-Process { throw 'Test must never terminate any process' }
& $StopScript
""", encoding="utf-8")
    result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-File", str(harness), "-StopScript", str(script), "-Defect", defect or "none"], env={**os.environ, "LOCALAPPDATA": str(tmp_path), "FF7R_MCP_HTTP_RUNTIME_DIR": str(control)}, capture_output=True, text=True, timeout=30)
    output = result.stdout + result.stderr
    if defect in (None, "runtime_child"):
        assert result.returncode == 0, output
        assert "Gracefully stopped FF7R HTTP MCP PID 1234" in output
        assert "port 8765 is released" in output
        assert not list(control.iterdir())
        # Live 2026-10-05: a replacement started before the old broker had
        # exited failed its first call, so the stop waits for it.
        assert ("runtime processes have exited (1)" in output) == (defect == "runtime_child"), output
    else:
        assert result.returncode != 0, output
        assert "refusing" in output.lower(), output
        assert not (control / "shutdown.request").exists()
        if defect != "missing_identity":
            assert identity_file.exists()
