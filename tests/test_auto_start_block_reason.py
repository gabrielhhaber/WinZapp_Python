"""Tests for the four guards that stop check_wa_connection_http() answering a
CLOSED status with /start-session.

A CLOSED reading normally means "nothing is running, bring the session back",
and that reflex has cost something every time it fired at the wrong moment:

* during a shutdown it revived the browser moments before taskkill hit it,
  cutting the profile mid-write and leaving the account on a pairing screen
  next launch (tests/test_shutdown_suppresses_auto_start.py);
* during a wake-from-suspend restart it raced that sequence's own
  close-session and spawned a second Chrome against the same userDataDir;
* and after MainWindow._halt_unattended_qr_session() it undid the halt on the
  very next poll — the session came back and resumed asking WhatsApp for a
  code every ~30s that nobody could scan, which is the flood that got a real
  account banned.

The decision used to be a four-way if/elif buried in check_wa_connection_http,
a method with too many dependencies (HTTP, wx, several subsystems) to drive end
to end, so it was only ever checked by matching its own source text. It is now
connection_state.auto_start_block_reason() and is checked by calling it. The
reason string it returns is what the caller logs, so each guard is still
distinguishable in the log.
"""

import connection_state as cs


ALLOWED = dict(
    pairing_dialog_active=False,
    qr_flood_halted=False,
    recovery_restart_active=False,
    self_inflicted_teardown=False,
)


class TestStartingIsAllowed:
    def test_a_plain_closed_session_may_be_restarted(self):
        """The whole point of the branch: nothing owns the browser, so the
        health loop brings the session back on its own."""
        assert cs.auto_start_block_reason(**ALLOWED) is None


class TestEachGuardBlocks:
    def test_the_pairing_dialog_blocks(self):
        """The pairing flow manages its own session; starting one here spawns
        a duplicate Chrome alongside it."""
        assert cs.auto_start_block_reason(**{**ALLOWED, "pairing_dialog_active": True}) == (
            cs.AUTO_START_BLOCKED_PAIRING_DIALOG
        )

    def test_a_halted_qr_flood_blocks(self):
        """The load-bearing half of _halt_unattended_qr_session(): without it
        the close buys one poll cycle rather than a fix."""
        assert cs.auto_start_block_reason(**{**ALLOWED, "qr_flood_halted": True}) == (
            cs.AUTO_START_BLOCKED_QR_FLOOD
        )

    def test_an_active_recovery_restart_blocks(self):
        assert cs.auto_start_block_reason(**{**ALLOWED, "recovery_restart_active": True}) == (
            cs.AUTO_START_BLOCKED_RECOVERY_RESTART
        )

    def test_our_own_teardown_blocks(self):
        assert cs.auto_start_block_reason(**{**ALLOWED, "self_inflicted_teardown": True}) == (
            cs.AUTO_START_BLOCKED_SELF_INFLICTED
        )


class TestTheReasonsStayDistinguishable:
    def test_every_reason_is_a_different_string(self):
        """Each guard logs its own line — that granularity is how an incident
        log says WHICH of the four kept the session down, and collapsing them
        into one message would lose it."""
        reasons = {
            cs.AUTO_START_BLOCKED_PAIRING_DIALOG,
            cs.AUTO_START_BLOCKED_QR_FLOOD,
            cs.AUTO_START_BLOCKED_RECOVERY_RESTART,
            cs.AUTO_START_BLOCKED_SELF_INFLICTED,
        }
        assert len(reasons) == 4

    def test_the_first_guard_wins_when_several_apply(self):
        """Order only decides what is logged — any one of them blocks — but it
        must be stable, so a log line can be traced back to a guard."""
        assert cs.auto_start_block_reason(
            pairing_dialog_active=True,
            qr_flood_halted=True,
            recovery_restart_active=True,
            self_inflicted_teardown=True,
        ) == cs.AUTO_START_BLOCKED_PAIRING_DIALOG
