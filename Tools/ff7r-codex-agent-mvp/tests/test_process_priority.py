from ff7r_agent import process_priority


class FakeKernel32:
    def __init__(self, ok=1):
        self.ok, self.calls = ok, []

    def GetCurrentProcess(self):
        return -1

    def SetPriorityClass(self, handle, priority):
        self.calls.append((handle, priority))
        return self.ok


def test_the_harness_raises_itself_to_above_normal(monkeypatch):
    monkeypatch.delenv("FF7R_PROCESS_PRIORITY", raising=False)
    kernel32 = FakeKernel32()
    assert process_priority.raise_harness_priority(kernel32) == "above_normal"
    assert kernel32.calls == [(-1, 0x00008000)]


def test_normal_priority_opts_out_and_a_refusal_is_reported(monkeypatch):
    monkeypatch.setenv("FF7R_PROCESS_PRIORITY", "normal")
    kernel32 = FakeKernel32()
    assert process_priority.raise_harness_priority(kernel32) is None and not kernel32.calls
    monkeypatch.delenv("FF7R_PROCESS_PRIORITY")
    assert process_priority.raise_harness_priority(FakeKernel32(ok=0)) is None
