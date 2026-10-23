from types import SimpleNamespace

import pytest

from ff7r_agent import safety


class FakeFunction:
    def __init__(self, callback):
        self.callback = callback

    def __call__(self, *args):
        return self.callback(*args)


@pytest.mark.parametrize("count,expected_wakes", [(0, 0), (1, 1), (2, 0)])
def test_display_wake_requires_one_verified_render_window(monkeypatch, count, expected_wakes):
    requests = []
    focus_requests = []
    kernel = SimpleNamespace(SetThreadExecutionState=FakeFunction(lambda flags: requests.append(flags) or 0x80000000))
    user = SimpleNamespace(**{name: FakeFunction(lambda *args: focus_requests.append(args) or True) for name in ("ShowWindow", "BringWindowToTop", "SetForegroundWindow")})
    monkeypatch.setattr(safety, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(safety.ctypes, "windll", SimpleNamespace(kernel32=kernel, user32=user), raising=False)
    environment = safety.WindowsGameEnvironment()
    monkeypatch.setattr(environment, "game_render_window_handles", lambda: [{"hwnd": 100+i, "pid": 123} for i in range(count)])
    monkeypatch.setattr(environment, "game_window_handles", lambda: pytest.fail("Wake must exclude helper windows"))
    monkeypatch.setattr(environment, "_game_processes", lambda: {123: "ff7rebirth_.exe"})
    monkeypatch.setattr(environment, "game_is_foreground", lambda: True)
    result = environment.focus_game_window(pid=123, wake_display=True)
    assert len(requests) == expected_wakes
    if expected_wakes:
        assert requests == [0x00000002]
        assert result["display_wake_ok"] is True
        assert result["pid"] == 123
    else:
        assert result["ok"] is False
        assert not focus_requests
