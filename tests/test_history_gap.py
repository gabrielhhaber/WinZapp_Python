"""Tests for detecting and repairing a hole in a chat's stored history.

Reported live on an active group: opening it announced 433 unread, but only
~200 messages ever arrived, and paging back with Home jumped straight from
08/08 01:56 to 08/08 16:28. The stored database confirmed it — 1391 messages
for that chat with a 14.52-hour stretch holding *zero*, and exactly 201
messages on the newer side of it: one saturated 200-message page plus one live
arrival.

Cause: sync_chat_messages() asks get-messages for the newest
`messages_page_size` (200) messages and nothing else. WinZapp had been closed
long enough for the group to receive more than that, so the window landed
entirely after the newest message on disk and the middle was never requested.
Nothing repaired it either — _note_backfill_state() retired any chat holding a
full page, which is precisely the case that means "the window saturated, there
is probably more behind it".

history_gap_detected() is a pure function and is tested directly;
_refetch_history_gap() and _note_backfill_state() are exercised as plain
functions against small stubs, since MainWindow is a wx.Frame and cannot be
instantiated without a running wx.App — same approach as
tests/test_message_backfill.py.
"""

import threading

import pytest

from main import MainWindow, history_gap_closed, history_gap_detected

PAGE = 200


def _msg(msg_id, ts):
    return {"key": {"id": msg_id}, "messageTimestamp": ts}


def _run(prefix, count, start_ts, step=60):
    """`count` messages with ids prefix0..prefixN, ascending in time."""
    return [_msg(f"{prefix}{i}", start_ts + i * step) for i in range(count)]


# Stored history: yesterday, ending at t=100_000.
OLD = _run("old", 300, 40_000)
# A fresh page that starts well after the stored history ends — the hole.
DISJOINT = _run("new", PAGE, 200_000)


class TestHistoryGapDetected:
    def test_a_full_page_disjoint_from_stored_history_is_a_gap(self):
        assert history_gap_detected(DISJOINT, OLD, PAGE) is True

    def test_a_short_page_is_never_a_gap(self):
        """The store gave everything it had; a wider request cannot help."""
        short = DISJOINT[:PAGE - 1]
        assert history_gap_detected(short, OLD, PAGE) is False

    def test_no_stored_history_is_a_first_sync_not_a_gap(self):
        assert history_gap_detected(DISJOINT, [], PAGE) is False

    def test_an_ordinary_resync_of_a_chat_we_already_hold_is_not_a_gap(self):
        """The false positive that matters: re-syncing a healthy chat returns
        the same newest messages already on disk."""
        stored = OLD + DISJOINT
        assert history_gap_detected(DISJOINT, stored, PAGE) is False

    def test_a_live_message_arriving_mid_sync_does_not_mask_the_gap(self):
        """The reason this compares message ids and not timestamps: one live
        message dragging the newest stored timestamp past the fetched window
        would hide the hole forever under a timestamp comparison."""
        stored = OLD + [_msg("live", 999_999)]
        assert history_gap_detected(DISJOINT, stored, PAGE) is True

    def test_partial_overlap_below_half_still_counts_as_a_gap(self):
        stored = OLD + DISJOINT[:40]
        assert history_gap_detected(DISJOINT, stored, PAGE) is True

    def test_partial_overlap_above_half_does_not(self):
        stored = OLD + DISJOINT[:120]
        assert history_gap_detected(DISJOINT, stored, PAGE) is False

    def test_stored_history_entirely_newer_than_the_page_is_not_a_gap(self):
        """Nothing older on disk means no second block to be disjoint from."""
        newer = _run("nw", 10, 900_000)
        assert history_gap_detected(DISJOINT, newer, PAGE) is False

    @pytest.mark.parametrize("fetched, local, size", [
        ([], OLD, PAGE),
        (DISJOINT, [], PAGE),
        (DISJOINT, OLD, 0),
        (DISJOINT, OLD, -1),
    ])
    def test_degenerate_inputs_never_claim_a_gap(self, fetched, local, size):
        assert history_gap_detected(fetched, local, size) is False

    def test_messages_without_timestamps_do_not_crash(self):
        junk = [{"key": {"id": f"j{i}"}} for i in range(PAGE)]
        assert history_gap_detected(junk, OLD, PAGE) is False


HOLE_TOP = min(m["messageTimestamp"] for m in DISJOINT)


class TestHistoryGapClosed:
    """The closing test is a separate question from the detecting one, and
    reusing history_gap_detected() for it was wrong: `known` is counted over
    the local snapshot (200 records, what get_chats() keeps in memory) while
    the widened page holds 800 or 2000. When the widened window reaches past
    the whole snapshot the ratio happens to answer correctly, but when it
    lands *inside* it the ratio calls a reached hole still open — costing an
    escalation to the ceiling and a spurious backfill entry."""

    def test_reaching_a_message_from_below_the_hole_closes_it(self):
        widened = OLD[-50:] + DISJOINT
        assert history_gap_closed(widened, OLD, HOLE_TOP) is True

    def test_a_page_that_never_reaches_the_far_side_stays_open(self):
        assert history_gap_closed(DISJOINT, OLD, HOLE_TOP) is False

    def test_a_widened_page_landing_inside_the_local_snapshot(self):
        """The band where the ratio test misreads: the widened page reaches
        back into the middle of the 200-record snapshot rather than past it,
        so some stored records are still older than it. `known` tops out at
        the snapshot size while the page holds 800 — the ratio calls that a
        gap and burns another escalation, though the far side was reached."""
        local = _run("old", 200, 40_000)
        widened = local[100:] + _run("new", 700, 200_000)

        assert history_gap_closed(widened, local, HOLE_TOP) is True
        # The ratio test, asked the same question, gets it wrong:
        assert history_gap_detected(widened, local, len(widened)) is True

    def test_a_live_message_above_the_hole_cannot_fake_a_close(self):
        """Only messages stored *below* the ceiling count as the far side."""
        local = [_msg("live", 999_999)]
        widened = DISJOINT + local
        assert history_gap_closed(widened, local, HOLE_TOP) is False

    @pytest.mark.parametrize("fetched, local, top", [
        ([], OLD, HOLE_TOP),
        (DISJOINT, [], HOLE_TOP),
        (DISJOINT, OLD, 0),
    ])
    def test_degenerate_inputs_never_claim_a_close(self, fetched, local, top):
        assert history_gap_closed(fetched, local, top) is False


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


class _RefetchStub:
    _HISTORY_GAP_FACTORS = MainWindow._HISTORY_GAP_FACTORS
    _HISTORY_GAP_MAX_COUNT = MainWindow._HISTORY_GAP_MAX_COUNT
    _refetch_history_gap = MainWindow._refetch_history_gap
    _normalize_fetched_messages = MainWindow._normalize_fetched_messages

    def __init__(self):
        self.wpp_server = "http://127.0.0.1"
        self.wpp_port = 6300
        self.token = "tok"
        self.ws = self          # _normalize_wpp_message lives here for the test
        self.requested = []

    @staticmethod
    def _normalize_wpp_message(wm):
        return dict(wm)


def _fake_get(stub, by_count):
    """requests.get double: maps the ?count= in the URL to a response."""
    def _get(url, headers=None, timeout=None, **kw):
        count = int(url.rsplit("count=", 1)[1])
        stub.requested.append(count)
        return by_count(count)
    return _get


class TestRefetchHistoryGap:
    def test_widens_until_the_window_reaches_stored_history(self, monkeypatch):
        # count=800 reaches back far enough to overlap OLD.
        def _by_count(count):
            if count == 800:
                return _Resp(200, {"response": OLD[-50:] + DISJOINT})
            return _Resp(200, {"response": DISJOINT})

        s = _RefetchStub()
        monkeypatch.setattr("main.requests.get", _fake_get(s, _by_count))
        out = s._refetch_history_gap("g@g.us", "g@g.us", {}, PAGE, OLD, HOLE_TOP)

        assert s.requested == [800]          # stopped as soon as it closed
        assert len(out) == 250

    def test_escalates_to_the_next_factor_when_the_first_is_not_enough(self, monkeypatch):
        def _by_count(count):
            if count == 800:
                return _Resp(200, {"response": _run("mid", 800, 150_000)})
            return _Resp(200, {"response": OLD[-50:] + DISJOINT})

        s = _RefetchStub()
        monkeypatch.setattr("main.requests.get", _fake_get(s, _by_count))
        s._refetch_history_gap("g@g.us", "g@g.us", {}, PAGE, OLD, HOLE_TOP)
        assert s.requested == [800, 2000]

    def test_stops_when_the_store_has_nothing_more_to_give(self, monkeypatch):
        """A wider request answering with the same count means WhatsApp Web
        does not hold the missing stretch — escalating again is pointless."""
        s = _RefetchStub()
        monkeypatch.setattr(
            "main.requests.get", _fake_get(s, lambda c: _Resp(200, {"response": DISJOINT})))
        out = s._refetch_history_gap("g@g.us", "g@g.us", {}, PAGE, OLD, HOLE_TOP)
        assert s.requested == [800]
        assert out == []

    def test_never_exceeds_the_api_page_ceiling(self, monkeypatch):
        s = _RefetchStub()
        monkeypatch.setattr(
            "main.requests.get",
            _fake_get(s, lambda c: _Resp(200, {"response": _run("m", c, 1)})))
        s._refetch_history_gap("g@g.us", "g@g.us", {}, PAGE, OLD, HOLE_TOP)
        assert max(s.requested) <= MainWindow._HISTORY_GAP_MAX_COUNT

    def test_an_http_error_gives_up_without_raising(self, monkeypatch):
        s = _RefetchStub()
        monkeypatch.setattr("main.requests.get", _fake_get(s, lambda c: _Resp(500)))
        assert s._refetch_history_gap("g@g.us", "g@g.us", {}, PAGE, OLD, HOLE_TOP) == []

    def test_a_request_exception_gives_up_without_raising(self, monkeypatch):
        def _boom(*a, **kw):
            raise ConnectionError("down")

        monkeypatch.setattr("main.requests.get", _boom)
        s = _RefetchStub()
        assert s._refetch_history_gap("g@g.us", "g@g.us", {}, PAGE, OLD, HOLE_TOP) == []


class _BackfillStub:
    _note_backfill_state = MainWindow._note_backfill_state
    _backfill_state_guard = MainWindow._backfill_state_guard
    _canonical_backfill_jid = MainWindow._canonical_backfill_jid
    _jid_address_forms = MainWindow._jid_address_forms
    history_page_target = MainWindow.history_page_target

    def __init__(self, gap_jids=(), still_landing=False):
        self._backfill_state_lock = threading.RLock()
        self.settings = {"user_interface": {"messages_page_size": PAGE}}
        self._history_gap_jids = set(gap_jids)
        self._history_still_landing = still_landing
        self._chats_awaiting_messages = set()
        self._partial_history_counts = {}
        self._lid_to_phone = {}
        self._phone_to_lid = {}

    def _server_claims_content(self, chat):
        return False


def _chat_with(n):
    return {"messages": {"messages": {"records": _run("r", n, 1000)}}}


class TestBackfillKeepsGapChats:
    def test_a_full_page_without_a_gap_is_retired(self):
        s = _BackfillStub()
        s._note_backfill_state("g@g.us", _chat_with(PAGE), api_ok=True)
        assert s._chats_awaiting_messages == set()

    def test_a_full_page_with_an_open_gap_is_queued(self):
        """The inverted signal: a saturated page is the case most likely to be
        hiding history, and it used to be the one treated as finished."""
        s = _BackfillStub(gap_jids={"g@g.us"})
        s._note_backfill_state("g@g.us", _chat_with(PAGE), api_ok=True)
        assert s._chats_awaiting_messages == {"g@g.us"}

    def test_a_queued_gap_chat_that_stops_growing_is_dropped(self):
        """Termination: WhatsApp Web may simply never hold the missing
        stretch, and the queue has to drain anyway."""
        s = _BackfillStub(gap_jids={"g@g.us"})
        s._note_backfill_state("g@g.us", _chat_with(PAGE), api_ok=True)
        assert s._chats_awaiting_messages == {"g@g.us"}
        s._note_backfill_state("g@g.us", _chat_with(PAGE), api_ok=True)
        assert s._chats_awaiting_messages == set()

    def test_the_repair_marker_is_released_with_the_queue_slot(self):
        """Leaving the chat in _history_gap_jids while dropping it from the
        queue only moves the leak: _plan_message_sync() reads that set as
        repair_needed too, so the chat would still be re-planned as a FULL sync
        every round — forever, and across restarts, since the set is
        persisted. Both markers go together."""
        s = _BackfillStub(gap_jids={"g@g.us"})
        s._note_backfill_state("g@g.us", _chat_with(PAGE), api_ok=True)
        assert s._history_gap_jids == {"g@g.us"}
        s._note_backfill_state("g@g.us", _chat_with(PAGE), api_ok=True)
        assert s._history_gap_jids == set()

    def test_a_gap_chat_that_is_still_growing_keeps_its_repair_marker(self):
        """The release is keyed on a pass that added nothing — a chat whose
        history is still landing must not lose the marker mid-ramp."""
        s = _BackfillStub(gap_jids={"g@g.us"})
        s._note_backfill_state("g@g.us", _chat_with(PAGE), api_ok=True)
        s._note_backfill_state("g@g.us", _chat_with(PAGE + 5), api_ok=True)
        assert s._chats_awaiting_messages == {"g@g.us"}
        assert s._history_gap_jids == {"g@g.us"}

    def test_a_queued_gap_chat_that_grows_keeps_its_slot(self):
        s = _BackfillStub(gap_jids={"g@g.us"})
        s._note_backfill_state("g@g.us", _chat_with(PAGE), api_ok=True)
        s._note_backfill_state("g@g.us", _chat_with(PAGE + 40), api_ok=True)
        assert s._chats_awaiting_messages == {"g@g.us"}

    def test_a_failed_api_call_is_still_not_the_backfills_business(self):
        s = _BackfillStub(gap_jids={"g@g.us"})
        s._note_backfill_state("g@g.us", _chat_with(PAGE), api_ok=False)
        assert s._chats_awaiting_messages == set()
