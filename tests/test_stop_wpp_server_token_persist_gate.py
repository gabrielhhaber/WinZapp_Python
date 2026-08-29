"""Tests for two gaps found by re-reviewing _stop_wpp_server()'s token-persist
gate (main.py) after the first pass of this fix (_wait_for_token_persisted()
itself, covered by tests/test_token_store_persistent_path.py).

Gap 1 -- the wait was nested INSIDE the close-session try block, so a
close-session request that raised (timeout, connection error -- precisely
the case where Chrome is most likely to still be mid-write) skipped the
token-persist wait entirely and went straight to taskkill. It must run
unconditionally after the try/except, not only on the happy path.

Gap 2 -- the decision to even bother waiting was gated on a single fresh
HTTP probe (_raw_session_status(), called pre_status here), which returns ""
on ANY failure of that one call (timeout, non-200, connection error). A
transient hiccup on exactly that probe -- at the single busiest moment in
the process's life -- would silently skip protecting a session that really
was CONNECTED. self._wa_connected is the app's own in-memory belief,
updated continuously all run and frozen (not clearable) the instant
_shutting_down/_wpp_updating goes True, so it is a race-free second signal
that costs no extra network round trip. Either signal must be enough to
trigger the wait.

_stop_wpp_server() is a large method with many dependencies (HTTP calls,
subprocess, taskkill) that make it impractical to drive end to end in a unit
test -- same situation check_wa_connection_http is in
(tests/test_shutdown_suppresses_auto_start.py), checked the same way: at
source level.
"""

import inspect

from main import MainWindow


def _body_source() -> str:
    return inspect.getsource(MainWindow._stop_wpp_server)


def _if_token_block() -> str:
    """The `if token:` block through STEP 2's node-lease release, isolated so
    assertions can't accidentally match unrelated code."""
    src = _body_source()
    start = src.index("if token:")
    end = src.index("# STEP 2:", start)
    return src[start:end]


class TestSessionWasConnectedUsesTwoSignals:
    def test_gate_checks_the_in_memory_flag_not_only_the_fresh_probe(self):
        block = _if_token_block()
        assert "session_was_connected" in block
        assert '"_wa_connected"' in block or "_wa_connected" in block
        assert 'pre_status in ("CONNECTED", "open")' in block

    def test_session_was_connected_is_computed_before_the_try(self):
        block = _if_token_block()
        decl_pos = block.index("session_was_connected =")
        try_pos = block.index("try:")
        assert decl_pos < try_pos, (
            "session_was_connected must be decided from pre_status/_wa_connected "
            "BEFORE the close-session attempt, not derived from anything the "
            "try block itself produces"
        )


class TestTokenPersistWaitRunsRegardlessOfException:
    def test_wait_for_token_persisted_call_is_outside_the_try_block(self):
        """The actual regression: this call must not be nested inside the
        `try:` whose `except Exception:` catches a close-session timeout/
        connection error -- otherwise that exact failure mode skips the wait
        that matters most."""
        block = _if_token_block()
        try_start = block.index("try:")
        except_start = block.index("except Exception as e:", try_start)
        # Find the end of the except suite: the next line at the same
        # indentation as `except` itself that isn't part of its body. The
        # except body in this method ends where the closing dedent to the
        # `if session_was_connected:` gate begins.
        gate_pos = block.index("if session_was_connected:")
        wait_pos = block.index("self._wait_for_token_persisted(token)")

        assert except_start < gate_pos < wait_pos, (
            "_wait_for_token_persisted must be called after (not nested "
            "inside) both the try and except suites"
        )

    def test_no_early_return_between_except_and_the_wait(self):
        """Guard against a future edit reintroducing an early return/continue
        inside except that would once again skip the wait on that path."""
        block = _if_token_block()
        except_start = block.index("except Exception as e:")
        gate_pos = block.index("if session_was_connected:")
        except_body = block[except_start:gate_pos]
        assert "return" not in except_body
        assert "continue" not in except_body
