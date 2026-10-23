from types import SimpleNamespace

from ff7r_agent.service import AgentService


def _service(status):
    service = object.__new__(AgentService)
    service.input = SimpleNamespace(is_real=True)
    service._session_lease = SimpleNamespace(status=lambda: status)
    return service


def test_a_stop_latched_by_a_closed_session_says_so():
    # #193: closing a control session latches the stop; the bare message sent
    # agents looking for an emergency that never happened.
    assert _service(None)._latched_stop_message() == (
        "emergency stop is latched: no control session is open; open_control_session first")
    assert _service({"active": False})._latched_stop_message().endswith("open_control_session first")


def test_a_stop_latched_inside_an_open_session_is_a_real_stop():
    assert _service({"active": True})._latched_stop_message() == "emergency stop is latched"
