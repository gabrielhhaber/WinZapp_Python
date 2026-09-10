"""Starting with Windows must wait for WPPConnect, exactly as a normal start does.

A foreground launch runs ensure_api_modules_installed / ensure_wpp_version /
ensure_wpp_running synchronously in MainWindow.__init__, so by the time init_UI()
builds the tray icon and post_ui_init() starts connecting, Node is listening.
A --background launch handed the same three calls to a daemon thread and carried
straight on. Measured on a real boot (2026-09-10 09:39:52, background_mode=True):

    T+1.1s   tray icon up, post_ui_init reaches STEP 5
    T+1.2s   check_wa_connection_http -> WinError 10061 (connection refused)
    T+2.0s   connect_websocket attempt 1/6 -> Connection error
    ...      attempts 2 and 3 fail the same way
    T+15.0s  Node finally answers; CLOSED, then INITIALIZING,
             disconnectedMobile, QRCODE

i.e. a tray icon reporting offline, the WebSocket ladder burnt on a dead port,
and the session cold-started by the health checker instead of by the launch —
minutes of avoidable "desconectado do WhatsApp" every boot.

What background mode should skip is the DIALOG, not the wait.
ensure_wpp_running() already knows the difference: its background branch polls
the port and never constructs ApiStartupDialog.
"""

import ast
import inspect
import re
import textwrap

from main import MainWindow


def _init_source() -> str:
    return inspect.getsource(MainWindow.__init__)


def test_the_api_start_is_not_handed_to_a_thread_in_background_mode():
    """The specific regression: a daemon thread running the three init calls
    while __init__ went on to build the UI and connect."""
    src = _init_source()
    for call in ("ensure_api_modules_installed", "ensure_wpp_version",
                 "ensure_wpp_running"):
        assert call in src, f"{call} is no longer started from __init__"
    assert "wpp-api-init" not in src, (
        "the background API start is on a thread again — __init__ will reach "
        "init_UI() and post_ui_init() against a port nobody is listening on"
    )


def test_ensure_wpp_running_is_not_guarded_by_background_mode():
    """It must be called on both paths. The guard that used to sit here is what
    split them."""
    src = _init_source()
    start = src.index("ensure_api_modules_installed")
    # Walk back to the nearest enclosing `if`/`else` line and check it is not a
    # background_mode gate.
    preceding = src[:start].splitlines()
    call_indent = len(src[:start].splitlines()[-1]) - len(
        src[:start].splitlines()[-1].lstrip())
    for line in reversed(preceding):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent < call_indent and (stripped.startswith("if ")
                                     or stripped.startswith("else")
                                     or stripped.startswith("elif ")):
            assert "background_mode" not in stripped, (
                f"the API start is gated on background mode again: {stripped!r}"
            )
            break


def test_background_mode_waits_for_the_port_before_returning():
    """ensure_wpp_running()'s background branch is the wait itself: spawn, then
    poll _is_wpp_running() until it answers. A branch that returned straight
    after the spawn would put the caller back where it started."""
    src = inspect.getsource(MainWindow.ensure_wpp_running)
    branch = src[src.index("if self.background_mode:"):]
    spawn = branch.index("_start_wpp_background()")
    poll = branch.index("_is_wpp_running()")
    assert spawn < poll, "the background branch no longer waits for the port"
    assert "while" in branch[spawn:poll + 200]


def test_background_mode_does_not_build_the_startup_dialog():
    """The dialog is what background mode legitimately skips — it would be a
    modal window on a machine the user has only just switched on."""
    src = inspect.getsource(MainWindow.ensure_wpp_running)
    branch = src[src.index("if self.background_mode:"):]
    # Everything up to the end of that branch (dedent back to method level).
    end = re.search(r"\n        (?!\s)", branch)
    branch = branch[:end.start()] if end else branch
    assert "ApiStartupDialog" not in branch


def test_a_live_server_is_adopted_before_either_branch_spawns_another():
    """The reuse check used to sit AFTER the background branch, which spawned
    unconditionally — so a leftover Node meant a second one launched only to die
    on EADDRINUSE while the poll reported success against the first. Same false
    success as the foreground path's, with no dialog to show it."""
    src = inspect.getsource(MainWindow.ensure_wpp_running)
    reuse = src.index("already listening on")
    background = src.index("if self.background_mode:")
    assert reuse < background


def test_the_three_init_calls_are_background_safe():
    """Making them synchronous is only correct because none of them can put a
    window on screen in background mode. ensure_wpp_version() returns early;
    every dialog in the install path is behind a background_mode guard."""
    version_src = inspect.getsource(MainWindow.ensure_wpp_version)
    body = version_src[version_src.index("if self.background_mode:"):]
    assert body.splitlines()[1].strip() == "return"

    install_src = inspect.getsource(MainWindow._ensure_api_modules_installed)
    tree = ast.parse(textwrap.dedent(install_src))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr not in ("MessageBox", "ShowModal"):
            continue
        # Every one of them must sit under a background_mode test somewhere
        # above it in the source.
        line = install_src.splitlines()[node.lineno - 1]
        preceding = "\n".join(install_src.splitlines()[:node.lineno])
        assert "background_mode" in preceding, (
            f"a dialog with no background_mode guard above it: {line.strip()!r}"
        )
