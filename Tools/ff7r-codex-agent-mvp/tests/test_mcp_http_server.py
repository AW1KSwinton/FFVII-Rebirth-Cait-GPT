from __future__ import annotations

import os

from ff7r_agent import mcp_http_server


def test_http_server_defaults_to_loopback_and_stateful_transport(monkeypatch):
    from ff7r_agent import mcp_http_server

    monkeypatch.delenv("FF7R_MCP_HTTP_HOST", raising=False)
    monkeypatch.delenv("FF7R_MCP_HTTP_PORT", raising=False)
    monkeypatch.delenv("FF7R_MCP_HTTP_PATH", raising=False)
    monkeypatch.delenv("FF7R_MCP_TRANSPORT", raising=False)
    monkeypatch.delenv("FF7R_MCP_BRIDGE_NAME", raising=False)
    monkeypatch.delenv("FF7R_MCP_BRIDGE_ENTRYPOINT", raising=False)

    result = mcp_http_server.configure_http_server()

    assert result == {
        "host": "127.0.0.1",
        "port": 8765,
        "path": "/mcp",
        "transport": "streamable-http",
        "bridge_name": "ff7r-agent-mcp-http",
        "stateless": False,
    }
    assert os.environ["FF7R_MCP_TRANSPORT"] == "streamable-http"
    assert os.environ["FF7R_MCP_BRIDGE_NAME"] == "ff7r-agent-mcp-http"


def test_http_server_refuses_non_loopback_bind(monkeypatch):
    monkeypatch.setenv("FF7R_MCP_HTTP_HOST", "0.0.0.0")

    try:
        mcp_http_server.configure_http_server()
    except ValueError as exc:
        assert "loopback-only" in str(exc)
    else:
        raise AssertionError("non-loopback HTTP bind was accepted")


def test_http_process_identity_record_contains_non_secret_creation_identity(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(mcp_http_server, "process_identity_token", lambda pid: "creation-1")

    mcp_http_server._prepare_shutdown_control()
    identity = mcp_http_server._write_process_identity()

    assert identity["pid"] == os.getpid()
    assert identity["process_creation_token"] == "creation-1"
    assert identity["entrypoint"] == "ff7r_agent.mcp_http_server"
    assert identity["command_identity"] == "python -m ff7r_agent.mcp_http_server"
    assert identity["protocol"] == "ff7r-http-shutdown-v1"
    assert "token" not in identity


def test_http_process_identity_accepts_matching_pid_creation_token_and_command():
    identity = {
        "pid": 123,
        "process_creation_token": "creation-1",
        "entrypoint": "ff7r_agent.mcp_http_server",
        "command_identity": "python -m ff7r_agent.mcp_http_server",
        "transport": "streamable-http",
        "protocol": "ff7r-http-shutdown-v1",
    }

    assert mcp_http_server.validate_http_process_identity(
        identity,
        pid=123,
        process_creation_token="creation-1",
        command_line="python.exe -m ff7r_agent.mcp_http_server",
    ) == (True, "")
    assert mcp_http_server.validate_http_process_identity(
        identity,
        pid=123,
        process_creation_token="creation-1",
        command_line=None,
    ) == (True, "")


def test_http_process_identity_refuses_pid_reuse_or_matchingish_command():
    identity = {
        "pid": 123,
        "process_creation_token": "creation-1",
        "entrypoint": "ff7r_agent.mcp_http_server",
        "command_identity": "python -m ff7r_agent.mcp_http_server",
        "transport": "streamable-http",
        "protocol": "ff7r-http-shutdown-v1",
    }

    assert not mcp_http_server.validate_http_process_identity(
        identity,
        pid=123,
        process_creation_token="creation-2",
        command_line="python.exe -m ff7r_agent.mcp_http_server",
    )[0]
    assert not mcp_http_server.validate_http_process_identity(
        identity,
        pid=123,
        process_creation_token="creation-1",
        command_line="python.exe -m ff7r_agent.mcp_http_server_malicious",
    )[0]


def test_http_stop_script_requires_identity_and_recovers_stale_dead_record():
    script = (mcp_http_server.Path(__file__).parents[1] / "Stop-FF7RHttpMcp.ps1").read_text(encoding="utf-8")

    assert "server.identity.json" in script
    assert "process_creation_token" in script
    assert "ToFileTimeUtc" in script
    assert "Refusing to stop PID $pidValue because its HTTP process-identity record is missing." in script
    assert "FF7R HTTP MCP PID $pidValue was already absent." in script
    assert "foreach ($path in @($identityPath" in script
    assert "WriteAllText($requestPath" in script
    assert "UTF8Encoding($false)" in script
