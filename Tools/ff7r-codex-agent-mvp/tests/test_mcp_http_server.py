from __future__ import annotations

import os

import pytest

from ff7r_agent import mcp_http_server


@pytest.fixture(autouse=True)
def restore_http_globals(monkeypatch):
    settings = mcp_http_server.mcp.settings
    for key in ("host", "port", "streamable_http_path", "stateless_http", "transport_security"):
        monkeypatch.setattr(settings, key, getattr(settings, key))
    for key in ("FF7R_MCP_TRANSPORT", "FF7R_MCP_BRIDGE_NAME", "FF7R_MCP_BRIDGE_ENTRYPOINT"):
        monkeypatch.setenv(key, os.environ.get(key, ""))


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


def test_http_transport_checks_hosts_and_origins_at_the_configured_port(monkeypatch):
    import asyncio
    from mcp.server.transport_security import TransportSecurityMiddleware
    from starlette.requests import Request

    settings = mcp_http_server.mcp.settings
    monkeypatch.setenv("FF7R_MCP_HTTP_HOST", "127.0.0.1")

    async def validate(host, origin):
        headers = [(b"host", host.encode())]
        if origin is not None:
            headers.append((b"origin", origin.encode()))
        request = Request({"type": "http", "method": "GET", "path": "/mcp", "headers": headers})
        result = await TransportSecurityMiddleware(settings.transport_security).validate_request(request)
        return None if result is None else result.status_code

    for port in (8765, 9876):
        monkeypatch.setenv("FF7R_MCP_HTTP_PORT", str(port))
        mcp_http_server.configure_http_server()
        for address in ("127.0.0.1", "localhost", "[::1]"):
            assert asyncio.run(validate(f"{address}:{port}", None)) is None
            assert asyncio.run(validate(f"{address}:{port}", f"http://{address}:{port}")) is None
        for host in (f"127.0.0.1:{port + 1}", f"localhost.attacker.invalid:{port}", "localhost"):
            assert asyncio.run(validate(host, None)) == 421
        for origin in ("null", f"http://localhost:{port + 1}", f"https://localhost:{port}",
                       f"http://localhost.attacker.invalid:{port}"):
            assert asyncio.run(validate(f"127.0.0.1:{port}", origin)) == 403


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
