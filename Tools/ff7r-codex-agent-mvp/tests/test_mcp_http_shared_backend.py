from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from pathlib import Path

import httpx

from ff7r_agent import mcp_http_server, mcp_runtime_server


class SharedFakeSupervisor:
    def __init__(self) -> None:
        self.generation = 1
        self.worker_pid = 2001
        self.broker = {"pid": 3001, "port": 51132}
        self._request_lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.calls: list[str] = []
        self.closed = False

    def identity(self) -> dict:
        return {
            "ok": True,
            "source_root": "C:/canonical/Tools/ff7r-codex-agent-mvp",
            "package_import_root": "C:/canonical/Tools/ff7r-codex-agent-mvp/src",
            "git_branch": "codex/ff7r-canonical-consolidation",
            "git_commit_sha": "canonical-head",
            "git_dirty": False,
            "worker_generation": self.generation,
            "worker_pid": self.worker_pid,
            "bridge_identity": {"name": "ff7r-agent-mcp-http", "pid": 1001},
            "gamepad_broker_identity": self.broker,
            "runtime_directory": "C:/runtime/ff7r-agent",
            "lifecycle_directory": "C:/runtime/ff7r-agent/lifecycle",
            "module_paths": {},
        }

    def status(self) -> dict:
        return {"ok": True, **self.identity()}

    def call(self, operation: str, arguments: dict | None = None) -> dict:
        with self._request_lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.calls.append(operation)
            time.sleep(0.02)
            self.active -= 1
        return {"ok": True, "operation": operation}

    def restart(self) -> dict:
        with self._request_lock:
            self.generation += 1
            self.worker_pid += 1
        return self.identity()

    def bootstrap_verification(self) -> dict:
        return {"ok": True, "read_only": True, "input_sent": False, "lifecycle_mutated": False}

    def close(self) -> None:
        self.closed = True


@contextlib.asynccontextmanager
async def _client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as http:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async with streamable_http_client("http://127.0.0.1:8765/mcp", http_client=http) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


def test_multiple_http_clients_share_one_supervisor_and_broker(monkeypatch) -> None:
    fake = SharedFakeSupervisor()
    monkeypatch.setattr(mcp_runtime_server, "_supervisor", fake)
    for name in (
        "FF7R_MCP_TRANSPORT",
        "FF7R_MCP_BRIDGE_NAME",
        "FF7R_MCP_BRIDGE_ENTRYPOINT",
    ):
        monkeypatch.delenv(name, raising=False)
    mcp_http_server.configure_http_server()
    app = mcp_http_server.mcp.streamable_http_app()

    async def exercise() -> None:
        async with app.router.lifespan_context(app):
            first_ready = asyncio.Event()
            run_request = asyncio.Event()
            first_request_done = asyncio.Event()
            check_restart = asyncio.Event()
            first_restart_done = asyncio.Event()
            first_after_restart: dict = {}
            close_first = asyncio.Event()

            async def first_client() -> None:
                async with _client(app) as first:
                    first_ready.set()
                    await run_request.wait()
                    await first.call_tool("agent_call", {"operation": "get_agent_status", "arguments": {}})
                    first_request_done.set()
                    await check_restart.wait()
                    first_after_restart.update((await first.call_tool("get_runtime_identity")).structuredContent)
                    first_restart_done.set()
                    await close_first.wait()

            first_task = asyncio.create_task(first_client())
            await first_ready.wait()
            async with _client(app) as second:
                first_identity = fake.identity()
                second_identity = (await second.call_tool("get_runtime_identity")).structuredContent
                assert first_identity["worker_pid"] == second_identity["worker_pid"]
                assert first_identity["worker_generation"] == second_identity["worker_generation"]
                assert first_identity["gamepad_broker_identity"] == second_identity["gamepad_broker_identity"]
                assert mcp_runtime_server._runtime() is fake

                run_request.set()
                await asyncio.gather(
                    second.call_tool("agent_call", {"operation": "get_agent_status", "arguments": {}}),
                    first_request_done.wait(),
                )
                assert fake.max_active == 1

                await second.call_tool("restart_agent_runtime")
                check_restart.set()
                await first_restart_done.wait()
                assert first_after_restart["worker_generation"] == 2
                assert first_after_restart["worker_pid"] == 2002
                assert first_after_restart["gamepad_broker_identity"] == first_identity["gamepad_broker_identity"]

                # Disconnect the first client while the second remains active.
                close_first.set()
                await first_task
                still_alive = (await second.call_tool("get_runtime_identity")).structuredContent
                assert still_alive["worker_pid"] == 2002
            assert fake.closed is False

    asyncio.run(exercise())


def test_http_launcher_has_singleton_and_fail_closed_pid_port_guards() -> None:
    launcher = (Path(__file__).parents[1] / "Start-FF7RHttpMcp.ps1").read_text(encoding="utf-8")

    assert "System.Threading.Mutex" in launcher
    assert "OwningProcess" in launcher
    assert "Get-ManagedHttpProcess" in launcher
    assert "untrusted or unrelated process" in launcher
    assert "Remove-Item -LiteralPath $pidPath" in launcher
    assert "ff7r_agent\\.mcp_http_server" in launcher
    assert "deployment_record" in launcher
    assert "$expectedBranch = [string]$manifest.canonical_branch" in launcher
    assert "codex/ff7r-canonical-consolidation" not in launcher
