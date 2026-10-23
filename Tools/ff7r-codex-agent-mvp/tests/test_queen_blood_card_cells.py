"""Exercise the opt-in population experiment, including restoration failures."""
from pathlib import Path
from threading import Lock
from types import SimpleNamespace
import json

import pytest

from ff7r_agent.service import AgentService, SafetyError

MODULE = Path(__file__).parents[2] / "ff7r-telemetry-exporter/QueenBlood/Scripts/card_enumerator.lua"


def simulation():
    lua54 = pytest.importorskip("lupa.lua54")
    lua = lua54.LuaRuntime()
    lua.globals().enumerator = lua.execute(MODULE.read_text(encoding="utf-8"))
    lua.execute(r'''
      top = {path = "/Engine/Transient.GameEngine_1:GI_1.Top"}
      widget = {path = top.path .. ".WidgetTree.Card_View.WidgetTree.Card_Full", id = "card_id_000"}
      view = {Card_Full = widget}; top.Card_View = view
      owner = {path = "/Engine/Transient.Controller_1"}
      calls = {}; guard = {cursors = {Player = {row = 0, index = 0, sequence = 1}}, total = "15/15"}
      cards = {"card_id_000", "card_id_000", "card_id_008"}
      fn = setmetatable({GetFunctionFlags = function() return flags or 0x04000000 end}, {
        __call = function(_, context, row, target, index)
          assert(context == owner and target == widget)
          calls[#calls + 1] = index
          target.id = cards[index + 1]
          if index == 2 and break_call then error("native failure") end
          if index == 2 and mutate_guard then guard.cursors.Player.sequence = 99 end
        end})
      deps = {
        valid = function(v) return v ~= nil end, top = function() return top end,
        shown = function() return true end, owner = function() return owner end,
        member = function(o, field) return o and o[field] end, path = function(o) return o and o.path end,
        find = function() return fn end, busy = function(v) busy = v end,
        card = function(o)
          if break_read and o.id == "card_id_008" then error("read failure") end
          return {card_id = o.id, image_id = o.id} end,
        guard = function() return guard end,
      }
      request = {ticket = string.rep("a", 32), area = "Player", execute = true,
        restore_row = 0, restore_index = 0, original = "card_id_000", owned_count = 0,
        positions = {{row=0,index=0}, {row=0,index=1}, {row=0,index=2}}}
      function run() return enumerator.run(request, deps) end
    ''')
    return lua


def test_population_preserves_positions_duplicates_and_restores_original():
    lua = simulation()
    result = lua.globals().run()
    assert result.ok and result.restored and not result.verified
    assert [c.card_id for c in result.cards.values()] == ["card_id_000", "card_id_000", "card_id_008"]
    assert [int(c.index) for c in result.cards.values()] == [0, 1, 2]
    assert list(lua.globals().calls.values()) == [0, 1, 2, 0]
    assert lua.globals().widget.id == "card_id_000"


@pytest.mark.parametrize("fault", ["break_call", "break_read"])
def test_exception_still_restores_the_original_widget(fault):
    lua = simulation()
    lua.globals()[fault] = True
    result = lua.globals().run()
    assert not result.ok and result.restored
    assert lua.globals().widget.id == "card_id_000"
    assert list(lua.globals().calls.values())[-1] == 0
    assert not lua.globals().busy


def test_cursor_side_effect_is_detected_despite_mutating_the_same_guard_table():
    lua = simulation()
    lua.globals().mutate_guard = True
    result = lua.globals().run()
    assert not result.ok and not result.restored
    assert result.guard_before.cursors.Player.sequence == 1
    assert result.guard_after.cursors.Player.sequence == 99


@pytest.mark.parametrize("setup,reason", [
    ("request.execute=false", None),
    ("flags=0", "blueprint_callable_unverified"),
    ("flags=0x00040401", "blueprint_callable_unverified"),
    ("owner=nil", "controller_not_observed"),
    ("request.original='card_id_008'", "original_card_unverified"),
    ("request.positions={{row=3,index=0}}", "position_out_of_bounds"),
    ("request.restore_index=5;guard.cursors.Player.index=5", "restore_position_out_of_bounds"),
    ("top.path='/Game/Template'", "reusable_live_widget_unavailable"),
])
def test_inspection_and_refusals_never_invoke_native_population(setup, reason):
    lua = simulation()
    lua.execute(setup)
    result = lua.globals().run()
    assert result.reason == reason
    assert len(lua.globals().calls) == 0


def test_expiring_protocol_rejects_noncanonical_or_unbounded_requests():
    lua = simulation()
    line = "\t".join(["v1", "a" * 32, "1008", "Player", "1", "0", "0", "card_id_000", "0,0|0,1", "0"]) + "\n"
    parse = lua.globals().enumerator.parse
    assert parse(line, 1000).positions[2].index == 1
    for invalid in (line.replace("1008", "999"), line.replace("1008", "1011"),
                    line.replace("0,0|0,1", "0,0||0,1"), line.replace("0,0|0,1", "00,0"),
                    line.replace("Player", "Enemy"), line + "\textra", "x" * 5000):
        assert parse(invalid, 1000) is None


def test_bulk_source_receipt_never_looks_up_or_calls_a_population_function():
    lua = simulation()
    lua.execute('owner.path=top.path .. ".EndNewCardDeckWindow_1"; '
                'request.read_only=true; deps.address=function() return "0x10000" end; '
                'deps.find=function() error("must not look up native functions") end')
    result = lua.globals().run()
    assert result.ok and result.read_only and result.population_calls == 0
    assert len(lua.globals().calls) == 0
    assert result.guard.cursors.Player.sequence == 1
    parse = lua.globals().enumerator.parse
    line = "\t".join(["v2", "b" * 32, "1008", "Edit", "read", "-1", "-1", "-", "-", "0"]) + "\n"
    assert parse(line, 1000).read_only
    assert parse(line.replace("\t-1\t-1\t", "\t0\t0\t"), 1000) is None


def test_bulk_source_refuses_a_controller_from_another_live_root():
    lua = simulation()
    lua.execute('request.read_only=true; deps.address=function() return "0x10000" end')
    result = lua.globals().run()
    assert not result.ok and result.reason == "controller_root_unverified"
    assert len(lua.globals().calls) == 0


@pytest.mark.parametrize("read_only", [True, False])
def test_reply_publish_retries_after_windows_reader_releases_file(tmp_path, read_only):
    """A reader without FILE_SHARE_DELETE must not lose a one-shot reply."""
    import ctypes
    from ctypes import wintypes
    import sys

    if sys.platform != "win32":
        pytest.skip("Windows file sharing semantics")
    lua = simulation()
    lua.execute('os.time=function() return 1000 end; '
                'owner.path=top.path .. ".EndNewCardDeckWindow_1"; '
                'deps.address=function() return "0x10000" end; '
                'deps.json=function(result) return \'{"ticket":"\' .. result.ticket .. \'"}\' end')
    fields = (["v2", "a" * 32, "1008", "Player", "read", "-1", "-1", "-", "-", "0"]
              if read_only else
              ["v1", "a" * 32, "1008", "Player", "1", "0", "0", "card_id_000", "0,0|0,1|0,2", "0"])
    (tmp_path / "queen_blood_cells.request").write_text("\t".join(fields) + "\n", newline="\n")
    output = tmp_path / "queen_blood_cells.json"
    output.write_text('{"ticket":"previous"}')
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(str(output), 0x80000000, 3, None, 3, 0, None)
    assert handle != ctypes.c_void_p(-1).value, ctypes.get_last_error()
    root = tmp_path.as_posix() + "/"
    try:
        lua.globals().enumerator.tick(root, lua.globals().deps)
        assert json.loads(output.read_text())["ticket"] == "previous"
        assert json.loads(output.with_suffix(".json.tmp").read_text())["ticket"] == "a" * 32
    finally:
        kernel.CloseHandle(handle)
    calls = list(lua.globals().calls.values())
    assert calls == ([] if read_only else [0, 1, 2, 0])
    lua.globals().enumerator.tick(root, lua.globals().deps)
    assert json.loads(output.read_text())["ticket"] == "a" * 32
    assert list(lua.globals().calls.values()) == calls  # Publish only; never repeat native calls.
    lua.globals().enumerator.tick(root, lua.globals().deps)
    assert list(lua.globals().calls.values()) == calls


@pytest.mark.parametrize("failure", ["open", "rename"])
def test_reply_retry_does_not_repeat_population_after_publication_failure(tmp_path, failure):
    lua = simulation()
    lua.execute('os.time=function() return 1000 end; '
                'deps.json=function(result) return \'{"ticket":"\' .. result.ticket .. \'"}\' end')
    line = "\t".join(["v1", "a" * 32, "1008", "Player", "1", "0", "0", "card_id_000",
                       "0,0|0,1|0,2", "0"]) + "\n"
    (tmp_path / "queen_blood_cells.request").write_text(line, newline="\n")
    if failure == "open":
        lua.execute('original_open=io.open; io.open=function(path, mode) '
                    'if mode=="wb" then return nil, "locked" end; return original_open(path,mode) end')
    else:
        lua.execute('original_rename=os.rename; os.rename=function() return nil, "locked" end')
    root = tmp_path.as_posix() + "/"
    for _ in range(2):
        lua.globals().enumerator.tick(root, lua.globals().deps)
    assert list(lua.globals().calls.values()) == [0, 1, 2, 0]
    assert not (tmp_path / "queen_blood_cells.json").exists()
    lua.execute('if original_open then io.open=original_open end; '
                'if original_rename then os.rename=original_rename end')
    lua.globals().enumerator.tick(root, lua.globals().deps)
    assert json.loads((tmp_path / "queen_blood_cells.json").read_text())["ticket"] == "a" * 32
    assert list(lua.globals().calls.values()) == [0, 1, 2, 0]


def service(tmp_path):
    agent = object.__new__(AgentService)
    agent.telemetry = SimpleNamespace(queen_blood_path=tmp_path / "queen_blood.json", observe=lambda: {})
    agent._action_lock = Lock()
    agent._session_lease = object()
    agent._deck_screen = lambda _: {"shown": True, "observed": True}
    agent._deck_cursor = lambda _: (0, 0)
    agent._deck_active_area = lambda _: "Player"
    agent._deck_popup_card = lambda *_: "card_id_000"
    agent._deck_total = lambda _: 15
    agent.queen_blood_profile = lambda: SimpleNamespace(owned_counts=lambda: {}, inventory=lambda: {})
    agent._record_action = lambda **_: None
    clock = [0.0]
    agent._monotonic = lambda: clock[0]

    def advance(seconds):
        clock[0] += seconds
        request = (tmp_path / "queen_blood_cells.request").read_text().split("\t")
        (tmp_path / "queen_blood_cells.json").write_text(json.dumps({"ticket": request[1], "ok": True}))

    agent._sleeper = advance
    return agent


def test_harness_inspection_defaults_to_no_native_calls_and_cleans_its_ticket(tmp_path):
    agent = service(tmp_path)
    result = agent.probe_queens_blood_card_cells()
    assert result["ok"]
    assert not (tmp_path / "queen_blood_cells.request").exists()
    assert not agent._action_lock.locked()


@pytest.mark.parametrize("execute", [False, True])
@pytest.mark.parametrize("consumed", [False, True])
def test_harness_timeout_reports_unknown_input_without_a_probe_receipt(tmp_path, execute, consumed):
    agent = service(tmp_path)
    request_path = tmp_path / "queen_blood_cells.request"
    clock = [0.0]
    requests = []
    agent._monotonic = lambda: clock[0]

    def lose_receipt(seconds):
        requests.append(request_path.read_text().split("\t"))
        if consumed:
            # The game thread may consume and act on the request without
            # publishing a receipt that the harness can read.
            request_path.unlink()
        clock[0] += 10

    agent._sleeper = lose_receipt
    result = agent.probe_queens_blood_card_cells(positions=[[0, 0]], execute=execute)

    assert not result["ok"] and result["experimental"]
    assert result["reason"] == "cell_probe_timeout"
    assert result["input_sent"] is None
    assert result["requires_review"] is True
    assert json.loads(json.dumps(result))["input_sent"] is None
    assert requests[0][1] == result["ticket"]
    assert requests[0][4] == str(int(execute))
    assert not request_path.exists()
    assert not agent._action_lock.locked()
    assert agent._last_action_finished == clock[0]


@pytest.mark.parametrize("positions", [[[0, True]], [[0, 5]], [[3, 0]], [[0, -1]], []])
def test_harness_rejects_unsafe_positions_before_creating_a_request(tmp_path, positions):
    agent = service(tmp_path)
    with pytest.raises(SafetyError):
        agent.probe_queens_blood_card_cells(positions=positions, execute=True)
    assert not (tmp_path / "queen_blood_cells.request").exists()


def test_harness_requires_a_verified_original_and_control_session(tmp_path):
    agent = service(tmp_path)
    agent._session_lease = None
    with pytest.raises(SafetyError, match="control session"):
        agent.probe_queens_blood_card_cells(positions=[[0, 0]], execute=True)
    agent._session_lease = object()
    agent._deck_popup_card = lambda *_: None
    with pytest.raises(SafetyError, match="original card identity"):
        agent.probe_queens_blood_card_cells(positions=[[0, 0]], execute=True)
