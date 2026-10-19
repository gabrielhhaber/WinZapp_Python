"""Call the CI teardown with a fake kernel only; no actual process APIs."""
import types

import pytest

from tests.test_update_guard_native_ci import _close_owned_child


@pytest.mark.parametrize("state", ["already_dead", "live", "terminate_fails", "wait_fails"])
def test_owned_handle_is_closed_even_when_native_test_cleanup_fails(state):
    calls = []
    def wait(handle, timeout):
        calls.append(("wait", handle, timeout))
        return 0 if state == "already_dead" or (timeout and state != "wait_fails") else 258
    def terminate(handle, code):
        calls.append(("terminate", handle, code))
        return state != "terminate_fails"
    kernel = types.SimpleNamespace(WaitForSingleObject=wait, TerminateProcess=terminate,
                                   CloseHandle=lambda handle: calls.append(("close", handle)))
    if state.endswith("fails"):
        with pytest.raises(AssertionError):
            _close_owned_child(kernel, 123)
    else:
        _close_owned_child(kernel, 123)
    assert calls[0] == ("wait", 123, 0) and calls[-1] == ("close", 123)
    assert (("terminate", 123, 1) in calls) is (state != "already_dead")
