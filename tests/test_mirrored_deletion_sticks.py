"""A mirrored deletion has to leave `records`, even with no row on screen.

``remove_messages_by_id()`` used to `return` as soon as none of the ids it was
given matched a rendered row, and that turned ``_mirror_remote_deletions()``
into a permanent no-op loop. The ids it mirrors are precisely the ones the phone
no longer has, and those are routinely NOT rendered rows: a reaction or other
non-displayable record, or a message paginated out of the current window.
Nothing was removed, so the next 60s poll found exactly the same ids missing,
and so did the next.

Measured on a real session (2026-09-10): "21 message(s) in <group> no longer on
the phone — removing locally" sixty times in one log, the same count every
minute, each round paying its own `get-messages?count=200` round trip — and the
log line printed before the early return, so it claimed work that never
happened.

ConversationsPanel is a wx.Panel, so the method runs unbound against a stub —
same approach as tests/test_selective_message_repaint.py.
"""

import pytest

from ui.conversations import ConversationsPanel

JID = "5511900000001@s.whatsapp.net"


class _FakeList:
    def __init__(self, rows):
        self._count = rows
        self.deleted = []
        self.focused = []
        self.selected = []
        self.ensured = []

    def GetItemCount(self):
        return self._count

    def GetFocusedItem(self):
        return 0

    def DeleteItem(self, idx):
        self.deleted.append(idx)
        self._count -= 1

    def Focus(self, idx):
        self.focused.append(idx)

    def Select(self, idx, on=True):
        self.selected.append(idx)

    def EnsureVisible(self, idx):
        self.ensured.append(idx)


class _DB:
    def __init__(self):
        self.deleted = []

    def delete_message(self, jid, mid):
        self.deleted.append((jid, mid))


class _Main:
    def __init__(self):
        self.db = _DB()
        self.recomputed = []
        self.set_chats = 0

    def _recompute_chat_last_message(self, jid):
        self.recomputed.append(jid)

    def _schedule_set_chats(self):
        self.set_chats += 1


def _msg(mid):
    return {"key": {"id": mid, "remoteJid": JID}, "messageType": "conversation",
            "message": {"conversation": mid}, "messageTimestamp": 100}


class _Panel:
    remove_messages_by_id = ConversationsPanel.remove_messages_by_id

    def __init__(self, records, rendered_ids):
        self.conversation = {
            "remoteJid": JID,
            "messages": {"messages": {"records": list(records)}},
        }
        rendered = [r for r in records if r["key"]["id"] in rendered_ids]
        self._sorted_messages = list(rendered)
        self._all_sorted_messages = list(records)
        self._messages_offset = 0
        self._unread_sep_idx = -1
        self.messages_list = _FakeList(len(rendered))
        self.main_window = _Main()
        self.stopped_playback = []

    def _stop_playback_for_removed_messages(self, ids):
        self.stopped_playback.append(set(ids))

    def _focused_msg_id(self):
        if self._sorted_messages:
            return self._sorted_messages[0]["key"]["id"]
        return ""

    def _record_ids(self):
        return [
            r["key"]["id"]
            for r in self.conversation["messages"]["messages"]["records"]
        ]


class TestAnOffScreenIdIsStillRemoved:
    """The regression. Every id here is absent from _sorted_messages, which is
    the ordinary shape of a mirrored phone-side deletion."""

    def test_it_leaves_the_records(self):
        p = _Panel([_msg("a"), _msg("hidden")], rendered_ids={"a"})

        p.remove_messages_by_id({"hidden"}, focus_previous=True)

        assert p._record_ids() == ["a"], (
            "the record survived, so the next poll reports it missing again — "
            "forever, once a minute"
        )

    def test_it_reaches_the_database(self):
        """Memory alone would not survive a reload: the next launch reads the
        record straight back in and the loop resumes."""
        p = _Panel([_msg("a"), _msg("hidden")], rendered_ids={"a"})

        p.remove_messages_by_id({"hidden"}, focus_previous=True)

        assert p.main_window.db.deleted == [(JID, "hidden")]

    def test_it_leaves_the_unpaginated_list(self):
        p = _Panel([_msg("a"), _msg("hidden")], rendered_ids={"a"})

        p.remove_messages_by_id({"hidden"}, focus_previous=True)

        assert [m["key"]["id"] for m in p._all_sorted_messages] == ["a"]

    def test_the_chat_list_preview_is_recomputed(self):
        """The removed message can be the one the conversations list is showing
        as the preview and sorting by."""
        p = _Panel([_msg("a"), _msg("hidden")], rendered_ids={"a"})

        p.remove_messages_by_id({"hidden"}, focus_previous=True)

        assert p.main_window.recomputed == [JID]
        assert p.main_window.set_chats == 1

    def test_no_row_is_deleted_from_the_control(self):
        p = _Panel([_msg("a"), _msg("hidden")], rendered_ids={"a"})

        p.remove_messages_by_id({"hidden"}, focus_previous=True)

        assert p.messages_list.deleted == []

    def test_focus_is_not_touched(self):
        """No row disappeared, so there is nothing to adjust focus for. Calling
        Focus()/Select() on the row the user is already sitting on fires
        EVT_LIST_ITEM_FOCUSED for a move that did not happen — the screen reader
        re-announces the row and the selection sound fires again."""
        p = _Panel([_msg("a"), _msg("hidden")], rendered_ids={"a"})

        p.remove_messages_by_id({"hidden"}, focus_previous=True)

        assert p.messages_list.focused == []
        assert p.messages_list.selected == []


class TestTheOnScreenPathIsUnchanged:
    def test_a_rendered_row_is_still_deleted(self):
        p = _Panel([_msg("a"), _msg("b")], rendered_ids={"a", "b"})

        p.remove_messages_by_id({"b"}, focus_previous=True)

        assert p.messages_list.deleted == [1]
        assert p._record_ids() == ["a"]

    def test_focus_is_still_adjusted_when_a_row_goes(self):
        """The user-initiated single-delete path: the deleted message IS what
        was focused, so focus has to land somewhere."""
        p = _Panel([_msg("a"), _msg("b")], rendered_ids={"a", "b"})

        p.remove_messages_by_id({"a"}, focus_previous=True)

        assert p.messages_list.focused == [0]

    def test_playback_is_stopped_for_every_id_either_way(self):
        """A playing audio message may not be in _sorted_messages at all —
        pagination can scroll it out while it keeps playing."""
        p = _Panel([_msg("a"), _msg("hidden")], rendered_ids={"a"})

        p.remove_messages_by_id({"hidden"})

        assert p.stopped_playback == [{"hidden"}]

    def test_an_empty_id_set_still_does_nothing(self):
        p = _Panel([_msg("a")], rendered_ids={"a"})

        p.remove_messages_by_id(set())

        assert p._record_ids() == ["a"]
        assert p.stopped_playback == []
        assert p.main_window.db.deleted == []
