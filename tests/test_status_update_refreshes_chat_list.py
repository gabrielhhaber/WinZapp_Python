"""A delivery-status change must repaint the conversations list.

The chat list's preview line ends with the last message's delivery status —
_last_msg_preview() appends ConversationsPanel._map_status(last) to it. When an
ack arrives, on_message_status_update() records it on the message and refreshes
the open conversation's own row, but it never told the conversations list to
repaint. So a sent message sat on "Pendente" in the list until some unrelated
event (another message landing anywhere, a sync) happened to trigger
set_chats(). Reported live: a chat still reading "Eu: . 21:07 Pendente" a
couple of seconds after the message had visibly been sent.

MainWindow is a wx.Frame and can't be instantiated without a running wx.App, so
the method is bound onto a stub carrying only what it touches — same approach
as the rest of this suite.
"""

import pytest

from main import MainWindow


class _FakePanel:
    def __init__(self):
        self.refreshed = []

    def refresh_message_status(self, msg_id, status):
        self.refreshed.append((msg_id, status))


class _FakeDB:
    def __init__(self):
        self.written = []

    def insert_message(self, jid, msg):
        self.written.append((jid, msg.get("key", {}).get("id")))


class _Win:
    on_message_status_update = MainWindow.on_message_status_update
    _is_status_stale = staticmethod(MainWindow._is_status_stale)

    def __init__(self, show_status=True, row_refresh_works=True):
        self.chats = {}
        self.db = _FakeDB()
        self.conversations_panel = _FakePanel()
        self.settings = {
            "user_interface": {"show_delivery_status_in_chat_list": show_status}
        }
        self.scheduled = 0
        self.rows_refreshed = []
        self._row_refresh_works = row_refresh_works

    def _normalize_jid(self, jid):
        return jid

    def move_chat_row_to_top(self, chat_jid):
        self.rows_refreshed.append(chat_jid)
        return self._row_refresh_works

    def _schedule_set_chats(self):
        self.scheduled += 1


JID = "grupo@g.us"


def _win_with_message(msg_id="m1", show_status=True, row_refresh_works=True):
    win = _Win(show_status=show_status, row_refresh_works=row_refresh_works)
    win.chats[JID] = {
        "messages": {"messages": {"records": [
            {"key": {"id": msg_id, "remoteJid": JID, "fromMe": True},
             "messageType": "conversation",
             "message": {"conversation": "oi"}},
        ]}}
    }
    return win


def _ack(msg_id="m1", status=2):
    return {"key": {"id": msg_id, "remoteJid": JID, "fromMe": True},
            "update": {"status": status}}


def test_an_ack_repaints_only_that_chat_s_row():
    """The exact bug, and its fix: nothing ever asked the list to repaint, and
    asking via set_chats() would re-resolve and re-render every one of the
    account's chats to change a single row."""
    win = _win_with_message()
    win.on_message_status_update(_ack())
    assert win.rows_refreshed == [JID]
    assert win.scheduled == 0, "must not rebuild the whole list for one ack"


def test_a_row_that_cannot_be_updated_in_place_falls_back_to_a_rebuild():
    """The row may be filtered out of the current view, or the list may be
    mid-rebuild — the preview must still end up correct."""
    win = _win_with_message(row_refresh_works=False)
    win.on_message_status_update(_ack())
    assert win.rows_refreshed == [JID]
    assert win.scheduled == 1


def test_the_status_still_reaches_the_open_conversation():
    """The pre-existing refresh must not be traded away for the new one."""
    win = _win_with_message()
    win.on_message_status_update(_ack(status=3))
    assert win.conversations_panel.refreshed == [("m1", "3")]


def test_the_status_is_recorded_on_the_message():
    win = _win_with_message()
    win.on_message_status_update(_ack(status=2))
    record = win.chats[JID]["messages"]["messages"]["records"][0]
    assert [u["status"] for u in record["MessageUpdate"]] == ["2"]


def test_every_ack_of_a_burst_repaints_the_row():
    """pending -> sent -> delivered -> read all change the preview text, and
    each one is now a single SetItem rather than a full list recompute."""
    win = _win_with_message()
    for status in (1, 2, 3, 4):
        win.on_message_status_update(_ack(status=status))
    assert win.rows_refreshed == [JID] * 4
    assert win.scheduled == 0


def test_no_work_at_all_when_the_preview_does_not_show_status():
    """Turning the setting off must turn the work off too."""
    win = _win_with_message(show_status=False)
    win.on_message_status_update(_ack())
    assert win.scheduled == 0
    assert win.rows_refreshed == []
    # The open conversation's row still refreshes — that row shows the status
    # regardless of this chat-list-only setting.
    assert win.conversations_panel.refreshed == [("m1", "2")]


def test_an_ack_for_an_unknown_message_does_nothing():
    """No message was updated, so no preview can have changed."""
    win = _win_with_message()
    win.on_message_status_update(_ack(msg_id="does-not-exist"))
    assert win.scheduled == 0
    assert win.rows_refreshed == []


def test_an_ack_with_no_status_is_ignored_entirely():
    win = _win_with_message()
    win.on_message_status_update({"key": {"id": "m1", "fromMe": True}, "update": {}})
    assert win.scheduled == 0
    assert win.rows_refreshed == []
    assert win.conversations_panel.refreshed == []


def test_a_missing_setting_defaults_to_repainting_the_row():
    """Installs whose settings.json predates the option still get the fix."""
    win = _win_with_message()
    win.settings = {"user_interface": {}}
    win.on_message_status_update(_ack())
    assert win.rows_refreshed == [JID]


# ── refresh_chat_row_text: the targeted repaint itself ────────────────────────

class _FakeList:
    def __init__(self, texts):
        self.rows = list(texts)
        self.set_calls = []

    def GetItemCount(self):
        return len(self.rows)

    def GetItemText(self, idx, col=0):
        return self.rows[idx]

    def SetItem(self, idx, col, text):
        self.set_calls.append(idx)
        self.rows[idx] = text


class _ListPanel:
    def __init__(self, jids, texts):
        self._displayed_jids = list(jids)
        self.chats_list = [{"remoteJid": j} for j in jids]
        self.chat_names = [j.split("@")[0] for j in jids]
        self.conversations_list = _FakeList(texts)
        self.selected_chats = set()


class _RowWin:
    refresh_chat_row_text = MainWindow.refresh_chat_row_text

    def __init__(self, jids, texts, built="NOVO TEXTO"):
        self.conversations_panel = _ListPanel(jids, texts)
        self.chats = {j: {"remoteJid": j} for j in jids}
        self.built = built
        self.build_calls = []

    def _build_chat_item_text(self, chat, name):
        self.build_calls.append(chat.get("remoteJid"))
        return self.built

    def _normalize_jid(self, jid):
        return jid


def test_only_the_target_row_is_rebuilt_and_written():
    """The whole point: one row's text is recomputed, not every chat's."""
    jids = [f"c{i}@s.whatsapp.net" for i in range(500)]
    win = _RowWin(jids, [f"old{i}" for i in range(500)])
    assert win.refresh_chat_row_text(jids[300]) is True
    assert win.build_calls == [jids[300]], "must not rebuild 500 rows to change one"
    assert win.conversations_panel.conversations_list.set_calls == [300]


def test_an_unchanged_row_is_not_written_at_all():
    jids = ["a@s.whatsapp.net"]
    win = _RowWin(jids, ["same"], built="same")
    assert win.refresh_chat_row_text("a@s.whatsapp.net") is True
    assert win.conversations_panel.conversations_list.set_calls == []


def test_a_chat_stored_under_a_different_key_than_its_remotejid_is_found():
    """self.chats keys and chat["remoteJid"] are not always the same string
    (see _compute_chat_lists) — rows are identified by remoteJid."""
    win = _RowWin(["real@s.whatsapp.net"], ["old"])
    win.chats["stored-key@lid"] = {"remoteJid": "real@s.whatsapp.net"}
    assert win.refresh_chat_row_text("stored-key@lid") is True
    assert win.conversations_panel.conversations_list.set_calls == [0]


def test_a_chat_not_currently_displayed_reports_failure():
    """Filtered out by a search or the unread filter — caller must fall back."""
    win = _RowWin(["a@s.whatsapp.net"], ["old"])
    assert win.refresh_chat_row_text("b@s.whatsapp.net") is False


def test_misaligned_backing_arrays_report_failure():
    """If the arrays and the control disagree, a targeted SetItem would write
    the right text into the wrong row — the exact class of bug that has caused
    opening the wrong conversation before."""
    win = _RowWin(["a@s.whatsapp.net", "b@s.whatsapp.net"], ["only-one-row"])
    assert win.refresh_chat_row_text("a@s.whatsapp.net") is False


def test_a_build_failure_reports_failure_instead_of_raising():
    win = _RowWin(["a@s.whatsapp.net"], ["old"])
    win._build_chat_item_text = lambda chat, name: (_ for _ in ()).throw(RuntimeError("boom"))
    assert win.refresh_chat_row_text("a@s.whatsapp.net") is False


class TestAnAckCanAlsoReorderTheList:
    """Regression: an ack was routed to a repaint-in-place, on the assumption
    that it never changes a chat's position. That holds for the message's own
    timestamp, but not for the list: a message you just sent enters as a local
    pending record and the ack is what settles the chat's real ordering, so the
    ack can be exactly the event that should float the chat up. Repainting in
    place left a chat you had just posted to sitting where it was — reported
    live as a group not rising after sending to it, while groups that merely
    received messages rose fine (those go through on_new_message, which was
    never routed to the repaint).
    """

    def test_the_ack_path_is_the_one_that_can_move_the_row(self):
        """Whatever the ack calls must be able to reorder, not only repaint."""
        win = _win_with_message()
        moved = []
        win.move_chat_row_to_top = lambda jid: moved.append(jid) or True
        win.refresh_chat_row_text = lambda jid: pytest.fail(
            "a repaint-only path cannot reorder; the ack must go through "
            "move_chat_row_to_top"
        )
        win.on_message_status_update(_ack())
        assert moved == [JID]

    def test_when_the_row_cannot_settle_its_position_the_full_path_runs(self):
        win = _win_with_message(row_refresh_works=False)
        win.on_message_status_update(_ack())
        assert win.scheduled == 1, "the reorder still has to happen somewhere"
