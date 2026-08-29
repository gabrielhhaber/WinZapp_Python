"""Tests for the startup WebSocket "Invalid namespace" path in MainWindow's
_post_ui_init() (a closure inside __init__ — see below for why it is
checked at source level).

Related to issue #132 ("The app constantly signs me out") alongside the
local-401 fix (tests/test_local_auth_rejected_logout_gate.py). Connecting the
startup WebSocket used to give up on the very first "Invalid namespace" /
"namespaces failed to connect" error (no retry at all, unlike every other
connect failure, which gets 6 attempts) and call _on_disconnect() with its
default wipe=True -- dropping the token and wiping the whole local database.

"Invalid namespace" means the Node server has no Socket.IO namespace for our
session, which can mean the session was really deleted server-side, but this
early in startup can just as easily mean Node has not finished registering
it yet. This code only runs once per launch, before any successful
connection this run (the reuse_existing_ws branch above it returns early
otherwise), so per the same invariant the rest of the app follows -- never
wipe a session that has not connected this run -- a still-invalid namespace
after retrying deserves the pairing dialog, not a destructive wipe.

_post_ui_init is defined inside MainWindow.__init__ (not a class method) and
touches a WebSocketClient, wx dialogs and a live session, so it cannot be
driven directly without a whole running app -- the same situation
test_line_separator_normalization.py's TestSendPathCallsNormalization and
TestEveryOtherFieldThatReachesWhatsApp are in, checked the same way: at
source level.
"""

import inspect

from main import MainWindow


def _step6_source() -> str:
    """The STEP 6 WebSocket-connect block's source, isolated from the rest
    of __init__ so assertions can't accidentally match unrelated code."""
    src = inspect.getsource(MainWindow.__init__)
    start = src.index("STEP 6 — connecting WebSocket")
    end = src.index("ALL STEPS COMPLETED SUCCESSFULLY", start)
    return src[start:end]


class TestInvalidNamespaceRetries:
    def test_invalid_namespace_no_longer_breaks_on_the_first_sighting(self):
        """The old bug: detecting "Invalid namespace" broke out of the retry
        loop immediately, unlike every other connect failure."""
        step6 = _step6_source()
        loop_body = step6[: step6.index("if not ws_connected")]
        assert "break" not in loop_body.split("except Exception as e:")[1], (
            "the retry loop still breaks immediately on an exception instead "
            "of retrying like every other connect failure"
        )

    def test_every_attempt_sleeps_before_retrying(self):
        step6 = _step6_source()
        loop_body = step6[: step6.index("if not ws_connected")]
        assert loop_body.count("time.sleep(3.0)") == 1


class TestInvalidNamespaceNeverWipes:
    def test_the_invalid_namespace_branch_disconnects_without_wiping(self):
        step6 = _step6_source()
        assert "self._on_disconnect(wipe=False" in step6

    def test_the_default_wipe_true_disconnect_call_is_gone(self):
        """Guards against the exact old call (self._on_disconnect() with no
        args, which defaults to wipe=True) reappearing in this block."""
        step6 = _step6_source()
        assert "self._on_disconnect()" not in step6

    def test_the_generic_failure_branch_is_unaffected(self):
        """Every OTHER connect failure (not an invalid-namespace one) must
        keep showing the ordinary reconnect-failed dialog, unchanged."""
        step6 = _step6_source()
        assert "websocket_failed_reconnect" in step6
        assert "self.connect.show_connection_dial()" in step6
        assert "self._just_paired = True" in step6
