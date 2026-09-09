"""The media transfer gauge must not outlive the transfer, or belong to a
message the user is not on.

Reported from real use: a progress bar sitting at 100% in the Tab order after
the message list, met most often just after sending a plain text message, and
sometimes two of them "going up and down on top of each other".

There is only one gauge, and both halves of the complaint come from that:

* **Parked at 100%.** Nothing hid it when a transfer finished.
  `MainWindow._download_media_for_sync()` calls
  `update_message_download_progress(msg_id, 1.0)` purely to repaint a row after
  a bulk background download that ran with no progress callbacks at all — and
  that call *showed* the gauge at 100% and left it there for the rest of the
  conversation. Hence "random moments": it is background media sync, not
  anything the user did. Sending a text is simply when they meet it, because
  the send puts focus back in the message field and the next Tab walks into it.
* **Two bars.** Any transfer wrote to the one gauge, including downloads for
  messages in other conversations, so two concurrent ones drove the same widget
  to two different values.

Selection already scoped the gauge in every other direction — on_message_selected()
hides it via _hide_all_media_controls(), and _sync_pending_document_gauge()
restores "only the selected active transfer". Updating it from anywhere was the
odd one out.

The panel is a wx.Panel and cannot be instantiated without a running wx.App, so
its methods are bound onto a stub carrying only what they touch.
"""

import pytest

from ui.conversations import ConversationsPanel


class _ListStub:
    """Enough wx.ListCtrl surface for the two progress methods."""

    def __init__(self, focused=0):
        self._focused = focused
        self.texts = {}
        self.refreshed = []

    def GetFocusedItem(self):
        return self._focused

    def GetFirstSelected(self):
        return self._focused

    def SetItemText(self, index, text):
        self.texts[index] = text

    def RefreshItem(self, index):
        self.refreshed.append(index)


class _Gauge:
    def __init__(self):
        self.value = None
        self.shown = False

    def SetValue(self, value):
        self.value = value

    def IsShown(self):
        return self.shown

    def Show(self):
        self.shown = True

    def Hide(self):
        self.shown = False


def _panel(messages, focused=0):
    panel = ConversationsPanel.__new__(ConversationsPanel)
    panel._sorted_messages = messages
    panel.messages_list = _ListStub(focused)
    panel._download_progress = {}
    panel._media_upload_progress = {}
    panel._upload_stages_seen = {}
    panel._media_transfer_started = set()
    panel._media_transfer_gauge = _Gauge()
    panel._render_message_line = lambda msg: "rendered"
    panel._is_separator = lambda msg: bool(msg.get("_separator"))
    # The two layout collaborators the real helpers reach for.
    panel._media_action_slot = type("_Slot", (), {"Show": lambda self: None})()
    panel.conversation_panel = type("_Conv", (), {"Layout": lambda self: None})()
    panel._sync_media_action_slot_visibility = lambda: None
    return panel


def _download(msg_id, **extra):
    return {"key": {"id": msg_id}, **extra}


class TestAFinishedTransferTakesItsBarWithIt:
    def test_reaching_100_percent_hides_the_gauge(self):
        panel = _panel([_download("m1")])
        ConversationsPanel.update_message_download_progress(panel, "m1", 1.0)
        assert panel._media_transfer_gauge.shown is False

    def test_the_bulk_sync_row_repaint_does_not_raise_a_bar(self):
        """MainWindow._download_media_for_sync() calls this with a flat 1.0 to
        repaint a row after a download that reported no progress at all. It is
        the commonest source of the stuck bar."""
        panel = _panel([_download("m1")])
        ConversationsPanel.update_message_download_progress(panel, "m1", 1.0)
        assert panel._media_transfer_gauge.shown is False
        assert panel.messages_list.texts == {0: "rendered"}, (
            "the row must still repaint — that is all this call ever wanted"
        )

    def test_a_transfer_still_running_keeps_its_bar(self):
        panel = _panel([_download("m1")])
        ConversationsPanel.update_message_download_progress(panel, "m1", 0.4)
        assert panel._media_transfer_gauge.shown is True
        assert panel._media_transfer_gauge.value == 40

    def test_a_finished_upload_pinned_at_100_does_not_come_back(self):
        """_mark_message_sent() pins an upload at 1.0 when the send succeeded
        but its real id was unparseable, and leaves the row pending on purpose.
        _sync_pending_document_gauge() then replayed that 1.0 every time the
        row was selected."""
        panel = _panel([{"_local_id": "u1", "_local_pending": True}])
        panel._media_upload_progress["u1"] = 1.0
        panel._media_transfer_started.add("u1")
        ConversationsPanel._sync_pending_document_gauge(panel)
        assert panel._media_transfer_gauge.shown is False


class TestTheBarBelongsToTheRowTheUserIsOn:
    def test_a_download_for_another_message_does_not_move_it(self):
        panel = _panel([_download("focused"), _download("elsewhere")], focused=0)
        ConversationsPanel.update_message_download_progress(panel, "elsewhere", 0.5)
        assert panel._media_transfer_gauge.shown is False
        assert panel._media_transfer_gauge.value is None

    def test_that_download_still_repaints_its_own_row(self):
        """The row text carries the percentage and is unambiguous about which
        message it belongs to, so it is never the part that has to be scoped."""
        panel = _panel([_download("focused"), _download("elsewhere")], focused=0)
        ConversationsPanel.update_message_download_progress(panel, "elsewhere", 0.5)
        assert panel.messages_list.texts == {1: "rendered"}

    def test_an_upload_for_another_row_does_not_move_it(self):
        panel = _panel(
            [{"_local_id": "focused"}, {"_local_id": "elsewhere"}], focused=0)
        ConversationsPanel.update_media_upload_progress(panel, "elsewhere", 0.5)
        assert panel._media_transfer_gauge.shown is False

    def test_the_focused_rows_own_upload_does_move_it(self):
        panel = _panel([{"_local_id": "u1"}], focused=0)
        ConversationsPanel.update_media_upload_progress(panel, "u1", 0.5)
        assert panel._media_transfer_gauge.shown is True
        assert panel._media_transfer_gauge.value == 50

    @pytest.mark.parametrize("focused", [-1, 5])
    def test_no_usable_focused_row_leaves_the_gauge_alone(self, focused):
        panel = _panel([_download("m1")], focused=focused)
        ConversationsPanel.update_message_download_progress(panel, "m1", 0.5)
        assert panel._media_transfer_gauge.shown is False

    def test_a_separator_row_never_owns_a_transfer(self):
        panel = _panel([{"_separator": True, "key": {"id": "m1"}}], focused=0)
        assert ConversationsPanel._transfer_owns_gauge(panel, "m1") is False

    def test_a_control_without_GetFocusedItem_does_not_raise(self):
        """This runs inside a wx.CallAfter, where an AttributeError killed
        upload progress once already (tests/test_compat_listbox_refresh_item.py).
        Not knowing which row is focused means leaving the gauge alone."""
        panel = _panel([_download("m1")])
        del panel.messages_list.__class__.GetFocusedItem
        try:
            assert ConversationsPanel._transfer_owns_gauge(panel, "m1") is False
        finally:
            _ListStub.GetFocusedItem = lambda self: self._focused
