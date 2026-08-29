"""Delivery receipts must not chop up a screen reader's speech.

Reported live: while NVDA is reading a message, the speech is cut off abruptly
and some row in the message list changes. The session log shows why — receipts
do not arrive one at a time:

    14:21:18  on_message_status_update  msg_id=... status=3   (x10, same second)

When the other side opens the chat, WhatsApp sends a READ receipt for every
message we ever sent in it at once. Each one used to rewrite its row
immediately, and on Windows every SetItemText raises a name-change event on
that ListView item — so a reader mid-sentence was interrupted ten times in a
row, for rows the user was not even on. This is the flood CLAUDE.md's
Freeze/Thaw rule exists to prevent.

ConversationsPanel is a wx.Panel and cannot be instantiated without a wx.App,
so the methods are bound onto a stub.
"""

import pytest

from ui.conversations import ConversationsPanel


class _FakeList:
    def __init__(self, texts):
        self.texts = list(texts)
        self.writes = []
        self.refreshed = []
        self.freezes = 0
        self.thaws = 0

    def GetItemText(self, i):
        return self.texts[i]

    def SetItemText(self, i, text):
        self.texts[i] = text
        self.writes.append((i, text))

    def RefreshItem(self, i):
        self.refreshed.append(i)

    def Freeze(self):
        self.freezes += 1

    def Thaw(self):
        self.thaws += 1


class _Timer:
    def __init__(self):
        self.running = True

    def IsRunning(self):
        return self.running


class _Panel:
    refresh_message_status = ConversationsPanel.refresh_message_status
    _flush_status_repaints = ConversationsPanel._flush_status_repaints
    _STATUS_REPAINT_COALESCE_MS = ConversationsPanel._STATUS_REPAINT_COALESCE_MS

    def __init__(self, messages, rendered=None):
        self._sorted_messages = list(messages)
        self._rendered = rendered or {}
        self.messages_list = _FakeList(
            [self._current_text(m) for m in self._sorted_messages]
        )
        self.scheduled = []

    def _current_text(self, msg):
        return f"{msg['key']['id']}:inicial"

    def _is_separator(self, msg):
        return msg.get("_separator", False)

    def _render_message_line(self, msg, index=None, total=None):
        mid = msg["key"]["id"]
        return self._rendered.get(mid, f"{mid}:inicial")


def _msg(mid):
    return {"key": {"id": mid}, "messageType": "conversation"}


@pytest.fixture
def call_later(monkeypatch):
    """Capture wx.CallLater instead of running it, so the test drives the
    flush explicitly."""
    import ui.conversations as mod
    scheduled = []

    def _fake(ms, fn, *a, **k):
        scheduled.append((ms, fn))
        return _Timer()

    monkeypatch.setattr(mod.wx, "CallLater", _fake)
    return scheduled


class TestABurstBecomesOnePass:
    def test_ten_receipts_schedule_a_single_flush(self, call_later):
        panel = _Panel([_msg(f"m{i}") for i in range(10)])
        for i in range(10):
            panel.refresh_message_status(f"m{i}", "3")
        assert len(call_later) == 1, "one timer for the whole burst"

    def test_nothing_is_written_before_the_flush(self, call_later):
        panel = _Panel([_msg("m1")], rendered={"m1": "m1:lido"})
        panel.refresh_message_status("m1", "3")
        assert panel.messages_list.writes == []

    def test_the_flush_writes_every_queued_row(self, call_later):
        msgs = [_msg("m1"), _msg("m2"), _msg("m3")]
        panel = _Panel(msgs, rendered={
            "m1": "m1:lido", "m2": "m2:lido", "m3": "m3:lido"})
        for m in msgs:
            panel.refresh_message_status(m["key"]["id"], "3")
        panel._flush_status_repaints()
        assert [w[0] for w in panel.messages_list.writes] == [0, 1, 2]

    def test_the_batch_is_wrapped_in_one_freeze_thaw(self, call_later):
        """One accessibility event for the batch instead of one per row."""
        msgs = [_msg("m1"), _msg("m2")]
        panel = _Panel(msgs, rendered={"m1": "m1:lido", "m2": "m2:lido"})
        for m in msgs:
            panel.refresh_message_status(m["key"]["id"], "3")
        panel._flush_status_repaints()
        assert panel.messages_list.freezes == 1
        assert panel.messages_list.thaws == 1

    def test_thaw_happens_even_if_rendering_raises(self, call_later):
        """A frozen list that never thaws is a dead UI."""
        panel = _Panel([_msg("m1")], rendered={"m1": "m1:lido"})

        def _boom(msg, index=None, total=None):
            raise RuntimeError("render falhou")

        panel._render_message_line = _boom
        panel.refresh_message_status("m1", "3")
        with pytest.raises(RuntimeError):
            panel._flush_status_repaints()
        assert panel.messages_list.thaws == 1


class TestAnUnchangedRowIsNotTouched:
    """A no-op SetItemText still raises the accessibility event, so writing
    text identical to what is already there interrupts a screen reader for
    literally no reason."""

    def test_a_row_whose_text_did_not_move_is_skipped(self, call_later):
        panel = _Panel([_msg("m1")])          # renders back to ":inicial"
        panel.refresh_message_status("m1", "3")
        panel._flush_status_repaints()
        assert panel.messages_list.writes == []
        assert panel.messages_list.refreshed == []

    def test_only_the_changed_rows_of_a_burst_are_written(self, call_later):
        msgs = [_msg("m1"), _msg("m2"), _msg("m3")]
        panel = _Panel(msgs, rendered={"m2": "m2:lido"})
        for m in msgs:
            panel.refresh_message_status(m["key"]["id"], "3")
        panel._flush_status_repaints()
        assert [w[0] for w in panel.messages_list.writes] == [1]
        assert panel.messages_list.refreshed == [1]


class TestTheQueueDrains:
    def test_a_second_flush_writes_nothing_more(self, call_later):
        panel = _Panel([_msg("m1")], rendered={"m1": "m1:lido"})
        panel.refresh_message_status("m1", "3")
        panel._flush_status_repaints()
        before = len(panel.messages_list.writes)
        panel._flush_status_repaints()
        assert len(panel.messages_list.writes) == before

    def test_a_receipt_for_a_message_not_on_screen_is_harmless(self, call_later):
        panel = _Panel([_msg("m1")])
        panel.refresh_message_status("desconhecida", "3")
        panel._flush_status_repaints()
        assert panel.messages_list.writes == []

    def test_a_separator_row_is_never_written(self, call_later):
        sep = {"key": {"id": "m1"}, "_separator": True}
        panel = _Panel([sep], rendered={"m1": "outro"})
        panel.refresh_message_status("m1", "3")
        panel._flush_status_repaints()
        assert panel.messages_list.writes == []

    def test_a_new_burst_after_a_flush_schedules_again(self, call_later):
        panel = _Panel([_msg("m1")], rendered={"m1": "m1:lido"})
        panel.refresh_message_status("m1", "3")
        panel._flush_status_repaints()
        panel.refresh_message_status("m1", "4")
        assert len(call_later) == 2
