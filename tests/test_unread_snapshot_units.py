"""A chat read on the phone that stays unread forever (issue #173).

`reconcile_snapshot_unread()` refuses to let a snapshot lower a count when the
local activity is newer than the snapshot. That rule is right and protects a
real case — a snapshot one second older than a live arrival must not clear the
badge that arrival just raised.

What was wrong is that it compared the two **in different units**. `t` arrives
from WhatsApp Web in seconds, while several local paths write a millisecond
value into the same field (`on_historical_message()` is the one CLAUDE.md
names). A millisecond timestamp is a thousand times any second-based snapshot,
so `incoming < local` became permanently true for that chat and no later
snapshot could ever lower its count again — including a snapshot saying the
conversation had been read.

That also explains the shape of the report: *one* conversation stuck while the
others updated correctly. Only a chat whose `t` went through the millisecond
path is affected, which is why this is occasional rather than universal.

F5 "fixed" it by wiping `self.chats` entirely, leaving nothing to preserve —
not by reconciling anything.

`core/incremental_sync.py` had already learned this on its own side; its
`_seconds()` docstring says comparing the two raw "makes the local side look
impossibly newer, which is the direction that silently skips a chat".
"""

import logging

import pytest

from main import _unread_seconds, reconcile_snapshot_unread


SECONDS = 1_700_000_000
#: The same instant, written the way on_historical_message() writes it.
MILLIS = 1_700_000_000_000


class TestTheUnitsAreNormalisedBeforeComparing:
    def test_a_millisecond_local_timestamp_no_longer_blocks_a_read(self):
        """The bug, in one line: the same instant in two units read as a
        thousand-year gap, so the badge could never be cleared."""
        assert reconcile_snapshot_unread(0, 8, SECONDS, MILLIS) == 0

    def test_it_did_not_matter_which_way_the_count_moved(self):
        """A merely lagging count was refused for the same wrong reason."""
        assert reconcile_snapshot_unread(3, 8, SECONDS, MILLIS) == 3

    @pytest.mark.parametrize("value, expected", [
        (SECONDS, SECONDS),
        (MILLIS, SECONDS),
        (0, 0),
        (None, 0),
        ("", 0),
        ("nonsense", 0),
        (1_000_000_000_000, 1_000_000_000_000),   # exactly the threshold: seconds
        (1_000_000_000_001, 1_000_000_000),       # just above it: milliseconds
    ])
    def test_the_conversion(self, value, expected):
        assert _unread_seconds(value) == expected


class TestTheGuardItselfIsUnchanged:
    """It protects a real case and this must not have weakened it."""

    def test_a_snapshot_older_than_a_live_arrival_still_cannot_clear_it(self):
        assert reconcile_snapshot_unread(0, 1, 1000, 1001) == 1

    def test_the_same_in_realistic_units(self):
        assert reconcile_snapshot_unread(0, 4, SECONDS, SECONDS + 1) == 4

    def test_a_read_at_the_same_instant_is_still_accepted(self):
        assert reconcile_snapshot_unread(0, 4, SECONDS, SECONDS) == 0

    def test_a_higher_remote_count_is_still_always_accepted(self):
        assert reconcile_snapshot_unread(5, 2, 2000, 1000) == 5

    def test_a_chat_first_seen_live_still_keeps_its_own_count(self):
        """Its local count is built on an assumed zero, so there is nothing
        here to believe yet — including a zero."""
        assert reconcile_snapshot_unread(0, 2, SECONDS, MILLIS, unsynced=True) == 2

    def test_a_lagging_count_is_still_refused_when_local_is_genuinely_newer(self):
        assert reconcile_snapshot_unread(3, 8, SECONDS, SECONDS + 5) == 8


class TestTheRefusalIsNowVisibleInTheLog:
    """Issue #173 said the available diagnostics could not establish whether
    the server reported a stale count or WinZapp refused a good one. Now they
    can."""

    def _emit(self, caplog, **kwargs):
        from main import _log_refused_read_receipt
        with caplog.at_level(logging.INFO):
            _log_refused_read_receipt(**kwargs)
        return [r.message for r in caplog.records]

    def test_a_refused_read_receipt_is_logged(self, caplog):
        messages = self._emit(
            caplog, jid="5511@s.whatsapp.net", server_unread=0, local_unread=8,
            incoming_timestamp=SECONDS, local_timestamp=MILLIS, merged=8)
        assert any("snapshot says read" in m for m in messages)

    def test_both_timestamps_are_reported_raw(self, caplog):
        """The units are the thing under suspicion; normalising them for the
        log would hide the evidence."""
        messages = self._emit(
            caplog, jid="5511@s.whatsapp.net", server_unread=0, local_unread=8,
            incoming_timestamp=SECONDS, local_timestamp=MILLIS, merged=8)
        assert any(str(MILLIS) in m and str(SECONDS) in m for m in messages)

    def test_an_accepted_read_is_not_logged(self, caplog):
        messages = self._emit(
            caplog, jid="5511@s.whatsapp.net", server_unread=0, local_unread=8,
            incoming_timestamp=SECONDS, local_timestamp=SECONDS, merged=0)
        assert messages == []

    def test_a_merely_lagging_count_is_not_logged(self, caplog):
        """Only a refused *read receipt* is interesting — a count that dropped
        from 8 to 3 says nothing about whether the chat was read."""
        messages = self._emit(
            caplog, jid="5511@s.whatsapp.net", server_unread=3, local_unread=8,
            incoming_timestamp=SECONDS, local_timestamp=MILLIS, merged=8)
        assert messages == []

    def test_nothing_is_logged_when_there_was_no_badge_to_keep(self, caplog):
        messages = self._emit(
            caplog, jid="5511@s.whatsapp.net", server_unread=0, local_unread=0,
            incoming_timestamp=SECONDS, local_timestamp=MILLIS, merged=0)
        assert messages == []
