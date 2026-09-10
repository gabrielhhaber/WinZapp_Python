"""Both Windows shutdown handlers have to leave a mark before they do anything.

A real `shutdown_audit.log` covering 159 launches carries seventeen runs that
ended with no `_stop_wpp_server` line at all — eleven of them overnight gaps of
7 to 12 hours, exactly the shape of Windows ending the session with WinZapp
open. It also carries **zero** lines from either of these handlers.

That silence is the diagnosis stuck. "Windows asked us and the teardown never
finished" and "Windows never asked us at all" (power loss, a forced Update
restart that skips the polite path, a kill) produce an identical file and want
opposite fixes. `log.log` cannot settle it either — it is truncated on every
launch, so the run that mattered is gone by the time anyone looks.

The audit line `_on_end_session` already had is too late to answer it: it sits
after the lock, after the already-tearing-down branch that returns without
auditing, and after the safety timer. So both handlers now write a breadcrumb
as their very first statement, and this pins that — an entry that can be
skipped is an entry that will be, on precisely the run that needed it.
"""

import inspect
import re

import pytest

from main import MainWindow


def _first_statement(method):
    """The first real line of the body, past the docstring."""
    source = inspect.getsource(method)
    body = source[source.index("):") + 2:]
    # Drop the docstring, however it is quoted.
    body = re.sub(r'^\s*(?P<q>"""|\'\'\').*?(?P=q)', "", body, count=1, flags=re.S)
    for line in body.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped
    return ""


@pytest.mark.parametrize("method_name, marker", [
    ("_on_query_end_session", "WM_QUERYENDSESSION"),
    ("_on_end_session", "WM_ENDSESSION"),
])
def test_the_handler_audits_before_anything_else(method_name, marker):
    first = _first_statement(getattr(MainWindow, method_name))
    assert first.startswith("self._shutdown_audit("), (
        f"{method_name} must audit before anything that can throw or return "
        f"early; its first statement is {first!r}"
    )
    assert marker in first


def test_the_two_markers_are_distinguishable():
    """They answer different questions — whether Windows *asked*, and whether
    it went through with it — so one string for both would lose the half that
    tells a cancelled shutdown from a completed one."""
    asked = _first_statement(MainWindow._on_query_end_session)
    ending = _first_statement(MainWindow._on_end_session)
    assert asked != ending
    # WM_ENDSESSION must not be a substring match away from WM_QUERYENDSESSION,
    # or a grep for one silently counts the other.
    assert "WM_QUERYENDSESSION" not in ending


def test_the_teardown_still_audits_the_budget_it_is_working_to():
    """The later, more detailed line is kept: the breadcrumb says Windows
    called, that one says what the teardown was given to work with.

    It lives on _run_windows_session_teardown now — the body moved there when
    the teardown was hoisted to WM_QUERYENDSESSION, because Windows can kill
    our Node before WM_ENDSESSION ever arrives."""
    source = inspect.getsource(MainWindow._run_windows_session_teardown)
    assert "_WINDOWS_SHUTDOWN_BUDGET" in source
    assert "_shutdown_audit(" in source


def test_the_already_tearing_down_branch_is_covered_by_the_breadcrumb():
    """That branch returns before the detailed audit line, which is one of the
    ways the file ended up silent. Both handlers breadcrumb before delegating,
    so the branch is reached with the mark already written either way."""
    for handler in (MainWindow._on_query_end_session, MainWindow._on_end_session):
        source = inspect.getsource(handler)
        assert source.index("_shutdown_audit(") < source.index(
            "_run_windows_session_teardown")

    teardown = inspect.getsource(MainWindow._run_windows_session_teardown)
    assert "if already_tearing_down:" in teardown
