"""Tests for coalescing the two ways the open conversation gets re-rendered.

Reported live: opening a group froze the
UI while reading messages. Both classes below apply the same debounce the
chat-list rebuild has always had (_schedule_set_chats): a pending flag plus a
single wx.CallLater, so a burst of N events costs one render.

TestScheduleRefreshActiveMessages covers the confirmed cause. The UI watchdog
(see tests/test_ui_watchdog.py) caught three stalls — 9.4 s, 19.8 s and 40.1 s
— and every one of the 33 stack samples taken during them landed on the same
line: refresh_active_conversation_messages()'s per-row
`SetItemText(i, self._render_message_line(msg))`. Three background loops
([Contact Resolution], [Mentions Scan], [LID Resolution]) wx.CallAfter'd that
full re-render once per resolved batch, and on an account with thousands of
unresolved @lid senders those batches arrive continuously for minutes.

TestScheduleRefreshMessages covers a second path found while chasing this, on
the history side: on_historical_message() scheduled its own
refresh_messages_if_changed() per backfilled message, each walking every stored
record through _messages_signature() and then rebuilding the list. That one was
NOT what the stacks showed — it was a wrong theory about this freeze — but the
unbounded per-message scheduling is real, so the coalescing stays.

MainWindow is a wx.Frame and cannot be instantiated without a running wx.App,
so the methods are exercised as plain functions against a stub — same approach
as tests/test_startup_grace.py.
"""

import pytest

from main import MainWindow


class _Panel:
    def __init__(self, conversation=None):
        self.conversation = conversation
        self.refreshes = 0
        self.repaints = 0
        self.repaint_jids = []   # what each repaint was scoped to (None = all)

    def refresh_messages_if_changed(self):
        self.refreshes += 1

    def refresh_active_conversation_messages(self, jids=None):
        self.repaints += 1
        self.repaint_jids.append(jids)
        return 0


class _Stub:
    _schedule_refresh_messages = MainWindow._schedule_refresh_messages
    _do_scheduled_refresh_messages = MainWindow._do_scheduled_refresh_messages
    _schedule_refresh_active_messages = MainWindow._schedule_refresh_active_messages
    _do_scheduled_refresh_active_messages = MainWindow._do_scheduled_refresh_active_messages
    _ACTIVE_REFRESH_DEBOUNCE_MS = MainWindow._ACTIVE_REFRESH_DEBOUNCE_MS

    def __init__(self, panel=None):
        self.conversations_panel = panel
        self.scheduled = []          # (delay, callable) queued via wx.CallLater


@pytest.fixture
def later(monkeypatch):
    """Capture wx.CallLater instead of needing a running event loop."""
    calls = []

    def _call_later(delay, fn, *a, **kw):
        calls.append((delay, fn, a, kw))

    monkeypatch.setattr("main.wx.CallLater", _call_later)
    return calls


def _fire(later_calls):
    """Run everything wx.CallLater has queued, as the event loop would."""
    pending, later_calls[:] = list(later_calls), []
    for _delay, fn, a, kw in pending:
        fn(*a, **kw)


class TestScheduleRefreshMessages:
    def test_a_burst_of_messages_costs_one_rebuild(self, later):
        """The regression: 300 backfilled messages used to mean 300 rebuilds."""
        panel = _Panel(conversation={"remoteJid": "g@g.us"})
        s = _Stub(panel)

        for _ in range(300):
            s._schedule_refresh_messages()

        assert len(later) == 1          # one timer, not 300
        _fire(later)
        assert panel.refreshes == 1

    def test_a_later_burst_gets_its_own_rebuild(self, later):
        """Coalescing must not swallow changes that arrive after the window."""
        panel = _Panel(conversation={"remoteJid": "g@g.us"})
        s = _Stub(panel)

        s._schedule_refresh_messages()
        _fire(later)
        s._schedule_refresh_messages()
        _fire(later)

        assert panel.refreshes == 2

    def test_the_debounce_matches_the_chat_list_one(self, later):
        s = _Stub(_Panel(conversation={"remoteJid": "g@g.us"}))
        s._schedule_refresh_messages()
        assert later[0][0] == 300

    def test_the_pending_flag_clears_even_when_nothing_is_open(self, later):
        """Otherwise the first refresh with no conversation open would wedge
        the flag and every later burst would be silently dropped."""
        s = _Stub(_Panel(conversation=None))
        s._schedule_refresh_messages()
        _fire(later)
        assert s._refresh_messages_pending is False

        s.conversations_panel.conversation = {"remoteJid": "g@g.us"}
        s._schedule_refresh_messages()
        _fire(later)
        assert s.conversations_panel.refreshes == 1

    def test_no_panel_at_all_is_survivable(self, later):
        s = _Stub(None)
        s._schedule_refresh_messages()
        _fire(later)                      # must not raise
        assert s._refresh_messages_pending is False

    def test_a_raising_refresh_does_not_wedge_the_flag(self, later):
        """A rebuild that throws must still leave the next burst schedulable —
        the flag is cleared before the call, not after."""
        panel = _Panel(conversation={"remoteJid": "g@g.us"})

        def _boom():
            raise RuntimeError("render failed")

        panel.refresh_messages_if_changed = _boom
        s = _Stub(panel)
        s._schedule_refresh_messages()
        _fire(later)                      # must not raise

        assert s._refresh_messages_pending is False


class TestScheduleRefreshActiveMessages:
    """The freeze the UI watchdog actually caught.

    refresh_active_conversation_messages() re-renders every row of the open
    conversation, and three background loops — [Contact Resolution],
    [Mentions Scan] and [LID Resolution] — used to wx.CallAfter it once per
    resolved batch. All 33 stack samples taken during three stalls (9.4 s,
    19.8 s, 40.1 s) landed inside that re-render.
    """

    def test_a_batch_storm_costs_one_repaint(self, later):
        panel = _Panel(conversation={"remoteJid": "g@g.us"})
        s = _Stub(panel)

        for _ in range(500):          # a resolution storm
            s._schedule_refresh_active_messages()

        assert len(later) == 1
        _fire(later)
        assert panel.repaints == 1

    def test_a_later_batch_still_repaints(self, later):
        panel = _Panel(conversation={"remoteJid": "g@g.us"})
        s = _Stub(panel)
        s._schedule_refresh_active_messages()
        _fire(later)
        s._schedule_refresh_active_messages()
        _fire(later)
        assert panel.repaints == 2

    def test_it_waits_longer_than_the_content_refresh(self, later):
        """This one only repaints names that resolution filled in; a second
        of delay is invisible, and the loops run for minutes."""
        s = _Stub(_Panel(conversation={"remoteJid": "g@g.us"}))
        s._schedule_refresh_active_messages()
        assert later[0][0] == 1000
        assert later[0][0] > 300      # the content-refresh debounce

    def test_it_repaints_even_with_no_conversation_open(self, later):
        """Unlike the content refresh, this one is safe (and pointless-but-
        harmless) without a conversation: the panel guards internally, so the
        scheduler must not add a second, divergent guard."""
        panel = _Panel(conversation=None)
        s = _Stub(panel)
        s._schedule_refresh_active_messages()
        _fire(later)
        assert panel.repaints == 1

    def test_no_panel_is_survivable(self, later):
        s = _Stub(None)
        s._schedule_refresh_active_messages()
        _fire(later)
        assert s._refresh_active_pending is False

    def test_a_raising_repaint_reschedules_itself(self, monkeypatch, later):
        """The flag is cleared before the call, so a failure can never wedge
        the scheduler — and since the repaint became scoped (see
        tests/test_selective_message_repaint.py) the failed round also has to
        come back as a full one, or the rows it never painted are lost."""
        monkeypatch.setattr("main.wx.CallAfter", lambda fn, *a, **kw: fn(*a, **kw))
        panel = _Panel(conversation={"remoteJid": "g@g.us"})

        def _boom(jids=None):
            raise RuntimeError("render failed")

        panel.refresh_active_conversation_messages = _boom
        s = _Stub(panel)
        s._schedule_refresh_active_messages()
        _fire(later)
        # Still the original point of this test: the flag is cleared before the
        # call, so a raising repaint can never leave the scheduler wedged.
        # (It reads True here only because the retry has already re-armed it.)
        assert s._refresh_active_pending is True
        assert len(later) == 1
        assert s._refresh_active_jids is None
        # …and the scheduler is genuinely still usable afterwards, which is
        # what "does not wedge" actually means.
        panel.refresh_active_conversation_messages = _Panel.refresh_active_conversation_messages.__get__(panel)
        _fire(later)
        s._schedule_refresh_active_messages()
        _fire(later)
        assert panel.repaints == 2
        assert s._refresh_active_pending is False
