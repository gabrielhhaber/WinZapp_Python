"""The WebSocket retry loop must remember an invalid namespace, not just the
last attempt's error.

post_ui_init's STEP 6 retries connect_websocket() up to six times, then picks
between two very different endings: an "Invalid namespace" run means the server
has no Socket.IO namespace for our session, which after the local-401 work is
routed to the pairing dialog WITHOUT wiping; anything else gets the generic
websocket_failed_reconnect warning and leaves the app offline.

The flag deciding that was assigned, not accumulated, inside the loop — so it
described only the *final* attempt. Five namespace failures followed by one
ECONNREFUSED (a Node still restarting, which is exactly the situation that
produces both) landed on the generic dialog, and the non-destructive pairing
route the rest of that work exists to reach was never taken. The comment
underneath said "still invalid after every attempt", which was never what the
code computed.

Checked against the source rather than by running it: _post_ui_init is a
closure defined inside MainWindow.__init__ (deliberately — it has to exist
before init_UI() blocks in MainLoop()), so there is no way to reach it without
constructing a wx.Frame, and no pure-logic seam here worth inventing for a
single boolean. The invariant is a property of how the statement is written,
which is precisely what this can read.

The second test below guards the same loop's other structural property: the
retry sleep has to be skipped after the final attempt. It is a background
thread, so nothing freezes — but a sleep with no attempt left to wait for
just holds back the dialog the user is already waiting on, by 3s per pass
that gets added.
"""

import ast
import pathlib
from tests.god_modules import main_window_source


FLAG = "saw_invalid_namespace"


def _post_ui_init():
    for node in ast.walk(ast.parse(main_window_source())):
        if isinstance(node, ast.FunctionDef) and node.name == "_post_ui_init":
            return node
    raise AssertionError("main.py no longer defines _post_ui_init")


def _assignments_to_flag(function):
    return [
        node for node in ast.walk(function)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == FLAG for t in node.targets)
    ]


def test_the_flag_is_still_there_to_check():
    """If the retry loop is ever rewritten out from under this, the test must
    say so rather than passing on an empty search."""
    assert _assignments_to_flag(_post_ui_init()), (
        f"_post_ui_init no longer assigns {FLAG} — if the two endings are now "
        f"chosen some other way, this file needs rewriting, not deleting"
    )


def test_the_flag_is_accumulated_across_attempts_never_overwritten():
    overwrites = []
    for node in _assignments_to_flag(_post_ui_init()):
        # The one legitimate plain assignment is the initialisation to False
        # before the loop starts.
        if isinstance(node.value, ast.Constant) and node.value.value is False:
            continue
        reads_itself = any(
            isinstance(child, ast.Name) and child.id == FLAG
            for child in ast.walk(node.value)
        )
        if not reads_itself:
            overwrites.append(node.lineno)
    assert overwrites == [], (
        f"main.py line(s) {overwrites}: {FLAG} is reassigned without reading "
        f"its own previous value, so it describes only the last connect "
        f"attempt. One attempt failing on something else (a Node still "
        f"restarting answers ECONNREFUSED just as readily) then hides every "
        f"invalid namespace before it, and the pairing dialog is never shown."
    )


def _retry_loop(function):
    for node in ast.walk(function):
        if (isinstance(node, ast.For) and isinstance(node.target, ast.Name)
                and node.target.id == "attempt"):
            return node
    raise AssertionError("_post_ui_init no longer retries over `attempt`")


def _unconditional_sleeps(node, under_if=False):
    """Lines of every sleep() call reached without passing through an `if`."""
    found = []
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.If):
            found += _unconditional_sleeps(child, under_if=True)
            continue
        called = child.value if isinstance(child, ast.Expr) else None
        if (not under_if and isinstance(called, ast.Call)
                and isinstance(called.func, ast.Attribute)
                and called.func.attr == "sleep"):
            found.append(child.lineno)
        found += _unconditional_sleeps(child, under_if=under_if)
    return found


def test_the_last_failed_attempt_does_not_sleep_before_giving_up():
    """The retry sleep is for the *next* attempt, so after the sixth failure
    there is nothing left to wait for — it only delays the pairing dialog by
    another 3s, pushing it to ~18s after the first failure instead of ~15s."""
    unconditional = _unconditional_sleeps(_retry_loop(_post_ui_init()))
    assert unconditional == [], (
        f"main.py line(s) {unconditional}: the retry loop sleeps on every "
        f"failed attempt including the last one, which no retry follows."
    )
