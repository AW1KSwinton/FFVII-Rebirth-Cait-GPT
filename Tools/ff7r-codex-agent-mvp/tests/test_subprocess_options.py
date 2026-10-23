import io
import os
import subprocess
from types import SimpleNamespace

import pytest

from ff7r_agent import gamepad_broker, runtime_identity
from ff7r_agent.subprocess_options import no_console_window_kwargs


def test_no_console_options_are_platform_specific(monkeypatch):
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    assert no_console_window_kwargs("nt") == {"creationflags": 0x08000000}
    assert no_console_window_kwargs("posix") == {}
    monkeypatch.delattr(subprocess, "CREATE_NO_WINDOW", raising=False)
    assert no_console_window_kwargs("nt") == {"creationflags": 0x08000000}


@pytest.mark.skipif(os.name != "nt", reason="Windows process creation flags only")
def test_gamepad_broker_helper_is_hidden_and_keeps_pipe_contract(monkeypatch):
    captured = {}

    class FakeProcess:
        pid = 43210
        stdout = io.StringIO('{"ok":true,"port":32001}\n')

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return FakeProcess()

    monkeypatch.setattr(gamepad_broker, "assert_no_existing_broker", lambda: None)
    monkeypatch.setattr(gamepad_broker, "process_identity_token", lambda _pid: "synthetic-token")
    monkeypatch.setattr(gamepad_broker.subprocess, "Popen", fake_popen)
    broker = gamepad_broker.BrokerProcess.start()

    assert broker.port == 32001
    assert isinstance(captured["command"], list)
    assert captured["command"][1:4] == ["-m", "ff7r_agent.gamepad_broker", "serve"]
    assert captured["creationflags"] == subprocess.CREATE_NO_WINDOW
    assert captured["stdin"] == subprocess.DEVNULL
    assert captured["stdout"] == subprocess.PIPE
    assert captured["stderr"] == subprocess.DEVNULL
    assert captured["text"] is True
    assert captured.get("shell", False) is False


@pytest.mark.skipif(os.name != "nt", reason="Windows process creation flags only")
def test_read_only_git_probe_is_hidden_and_keeps_capture_and_timeout(monkeypatch, tmp_path):
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="synthetic-head\n", stderr="")

    monkeypatch.setattr(runtime_identity.subprocess, "run", fake_run)
    root = tmp_path / "repo"
    assert runtime_identity._git_value(root, ("rev-parse", "HEAD")) == "synthetic-head"
    assert isinstance(captured["command"], list)
    assert captured["capture_output"] is True
    assert captured["text"] is True
    assert captured["timeout"] == 3.0
    assert captured["creationflags"] == subprocess.CREATE_NO_WINDOW
    assert captured.get("shell", False) is False
