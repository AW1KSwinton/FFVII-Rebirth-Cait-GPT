from __future__ import annotations

import ctypes
from types import SimpleNamespace

import pytest

from ff7r_agent import safety


class _Callable:
    def __init__(self, callback):
        self.callback = callback

    def __call__(self, *args):
        return self.callback(*args)


def _int_value(value):
    return int(getattr(value, "value", value) or 0)


class _FakeUser32:
    def __init__(self, *, attach_results=(), detach_results=()):
        self.foreground = 900
        self.attach_calls = []
        self.detach_calls = []
        self.set_calls = 0
        attach_values = iter(attach_results)
        detach_values = iter(detach_results)
        self.GetForegroundWindow = _Callable(lambda: ctypes.c_void_p(self.foreground))
        self.GetWindowThreadProcessId = _Callable(
            lambda hwnd, _pid: {
                900: 77,
                123: 88,
            }.get(int(getattr(hwnd, "value", hwnd) or 0), 0)
        )
        self.AttachThreadInput = _Callable(
            lambda first, second, attached: (
                self.attach_calls.append((_int_value(first), _int_value(second), bool(attached)))
                or (
                    next(attach_values, True)
                    if bool(attached)
                    else next(detach_values, True)
                )
            )
        )
        self.SetForegroundWindow = _Callable(self._set_foreground)
        self.ShowWindow = _Callable(lambda _hwnd, _command: True)
        self.BringWindowToTop = _Callable(lambda _hwnd: True)

    def _set_foreground(self, _hwnd):
        self.set_calls += 1
        if self.set_calls > 1:
            self.foreground = 123
        return self.set_calls > 1


class _FakeKernel32:
    GetCurrentThreadId = _Callable(lambda: 66)


def _environment(monkeypatch, user32, *, foreground=(False, True)):
    env = safety.WindowsGameEnvironment()
    monkeypatch.setattr(safety.os, "name", "nt")
    monkeypatch.setattr(
        env,
        "game_window_handles",
        lambda: [{"hwnd": 123, "pid": 456}],
    )
    values = iter(foreground)
    monkeypatch.setattr(
        env,
        "game_is_foreground",
        lambda: next(values, True),
    )
    monkeypatch.setattr(
        safety.ctypes,
        "windll",
        SimpleNamespace(user32=user32, kernel32=_FakeKernel32()),
        raising=False,
    )
    return env


def test_focus_attaches_and_detaches_input_queues(monkeypatch):
    user32 = _FakeUser32(attach_results=(True, True), detach_results=(True, True))
    result = _environment(monkeypatch, user32).focus_game_window(456)

    assert result["ok"] is True
    assert result["state"] == "foreground"
    assert result["attached_thread_ids"] == [77, 88]
    assert result["attach_failures"] == []
    assert result["detach_failures"] == []
    assert user32.attach_calls == [
        (66, 77, True),
        (66, 88, True),
        (66, 88, False),
        (66, 77, False),
    ]


def test_focus_fails_closed_when_detach_fails(monkeypatch):
    user32 = _FakeUser32(attach_results=(True, True), detach_results=(True, False))
    result = _environment(monkeypatch, user32).focus_game_window(456)

    assert result["foreground"] is True
    assert result["ok"] is False
    assert result["state"] == "foreground_requested"
    assert result["detach_failures"] == [77]


def test_focus_fails_closed_when_attachment_fails(monkeypatch):
    user32 = _FakeUser32(attach_results=(True, False), detach_results=(True,))
    result = _environment(monkeypatch, user32).focus_game_window(456)

    assert result["ok"] is False
    assert result["state"] == "foreground_requested"
    assert result["attach_failures"] == [88]
    assert result["detach_failures"] == []


def test_focus_does_not_attach_when_initial_request_succeeds(monkeypatch):
    user32 = _FakeUser32()
    env = _environment(monkeypatch, user32, foreground=(True,))
    result = env.focus_game_window(456)

    assert result["ok"] is True
    assert result["attached_thread_ids"] == []
    assert user32.attach_calls == []


def test_focus_fails_closed_without_a_matching_window(monkeypatch):
    env = safety.WindowsGameEnvironment()
    monkeypatch.setattr(safety.os, "name", "nt")
    monkeypatch.setattr(
        env,
        "game_window_handles",
        lambda: [],
    )
    monkeypatch.setattr(env, "game_is_foreground", lambda: True)

    result = env.focus_game_window(456)

    assert result == {
        "ok": False,
        "state": "game_window_not_found",
        "foreground": True,
    }


@pytest.mark.parametrize("platform", ["posix", "java"])
def test_focus_is_unsupported_off_windows(monkeypatch, platform):
    env = safety.WindowsGameEnvironment()
    monkeypatch.setattr(safety.os, "name", platform)

    assert env.focus_game_window() == {
        "ok": False,
        "state": "unsupported_platform",
        "foreground": False,
    }
