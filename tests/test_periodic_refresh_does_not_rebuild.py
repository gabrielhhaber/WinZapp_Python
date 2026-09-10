"""The 60s poll must repaint the rows that changed, never rebuild the list.

A native ListView row is a single MSAA object, so ``populate_messages()``'s
DeleteAllItems() + re-Append() + re-Focus()/re-Select() fires
EVT_LIST_ITEM_FOCUSED for the row the user is already sitting on. The screen
reader re-announces it and the selection sound fires again — once a minute,
mid-read. It cannot be fixed by making the rebuild quieter: once the control has
been cleared, re-focusing is the only way to put the user back, and that event
is unavoidable. So the rebuild has to not happen.

``refresh_messages_if_changed()`` already had two rungs — "nothing changed" and
_append_new_tail_rows() for rows added at the END. The two ordinary changes that
fit neither are the ones the poll actually delivers:

* a delivery/read receipt moving an existing message's ``status``;
* a reaction, a record that never becomes a row of its own and instead changes
  the text of ANOTHER row.

Measured on a real session (2026-09-10): a full rebuild every ~60s, including
one pair two minutes apart that both rebuilt an identical 209 rows.

ConversationsPanel is a wx.Panel, so the methods run unbound against a stub
carrying a fake list control — same approach as
tests/test_selective_message_repaint.py.
"""

import pytest

from ui.conversations import ConversationsPanel

JID = "5511900000001@s.whatsapp.net"


class _FakeList:
    """Just enough wx.ListCtrl for the repaint path, recording every write."""

    def __init__(self, rows=0):
        self._count = rows
        self.texts = {}
        self.deleted_all = 0
        self.focused = []
        self.selected = []

    def GetItemCount(self):
        return self._count

    def SetItemText(self, idx, text):
        self.texts[idx] = text

    def DeleteAllItems(self):
        self.deleted_all += 1
        self._count = 0

    def Focus(self, idx):
        self.focused.append(idx)

    def Select(self, idx, on=True):
        self.selected.append(idx)


def _msg(mid, ts, status="", body="hi"):
    return {
        "key": {"id": mid, "remoteJid": JID, "fromMe": False},
        "message": {"conversation": body},
        "messageType": "conversation",
        "messageTimestamp": ts,
        "status": status,
    }


def _reaction(mid, ts, target, emoji="👍"):
    return {
        "key": {"id": mid, "remoteJid": JID, "fromMe": False,
                "participant": "5511900000002@s.whatsapp.net"},
        "message": {"reactionMessage": {"key": {"id": target}, "text": emoji}},
        "messageType": "reactionMessage",
        "messageTimestamp": ts,
    }


class _Panel:
    _messages_signature = ConversationsPanel._messages_signature
    # staticmethod on the real class: unwrapped here it would be rebound as a
    # plain method and receive `self` as its first argument.
    _signature_changed_ids = staticmethod(ConversationsPanel._signature_changed_ids)
    _repaint_changed_rows_in_place = ConversationsPanel._repaint_changed_rows_in_place
    _sorted_deduped_records = ConversationsPanel._sorted_deduped_records
    _reaction_map_from_sorted = ConversationsPanel._reaction_map_from_sorted
    _reaction_target_id = staticmethod(ConversationsPanel._reaction_target_id)
    _set_message_row_texts = ConversationsPanel._set_message_row_texts
    _extract_timestamp = ConversationsPanel._extract_timestamp
    _SELF_REACTOR_KEY = ConversationsPanel._SELF_REACTOR_KEY

    def __init__(self, records):
        self._set_records(records)
        self._first_unread_msg_id = ""
        self._pending_open_unread = 0
        self._reaction_map = {}
        self._messages_signature_cache = None
        self.rebuilds = 0
        displayable = [r for r in records if r.get("messageType") != "reactionMessage"]
        self._sorted_messages = list(displayable)
        self._all_sorted_messages = list(displayable)
        self.messages_list = _FakeList(len(displayable))

    def _set_records(self, records):
        self.conversation = {
            "remoteJid": JID,
            "messages": {"messages": {"records": list(records)}},
        }

    # -- collaborators the methods under test reach for ---------------------
    def _is_separator(self, m):
        return isinstance(m, dict) and m.get("_type") in ("separator", "unread_separator")

    def _is_displayable_message(self, m):
        return isinstance(m, dict) and m.get("messageType") != "reactionMessage"

    def _get_message_content(self, m):
        return (m.get("message") or {}).get("conversation", "")

    def _reactor_key_from_msg(self, m):
        return (m.get("key") or {}).get("participant", "")

    def _render_message_line(self, m, index=None, total=None):
        mid = (m.get("key") or {}).get("id", "")
        emoji = "".join((self._reaction_map.get(mid) or {}).values())
        return f"{self._get_message_content(m)}|{m.get('status', '')}|{emoji}"

    def populate_messages(self, preserve_focus=False):
        self.rebuilds += 1
        self.messages_list.DeleteAllItems()

    def _repaint(self):
        """Drive the rung the way refresh_messages_if_changed() does."""
        old = self._messages_signature_cache
        new = self._messages_signature()
        return self._repaint_changed_rows_in_place(old, new)


def _panel_with_cache(records):
    p = _Panel(records)
    p._messages_signature_cache = p._messages_signature()
    p._reaction_map = p._reaction_map_from_sorted(p._sorted_deduped_records(records))
    return p


class TestAStatusChangeRepaintsOneRow:
    def test_it_does_not_rebuild(self):
        records = [_msg("a", 100), _msg("b", 200), _msg("c", 300)]
        p = _panel_with_cache(records)

        records[1]["status"] = "READ"
        p._set_records(records)

        assert p._repaint() is True
        assert p.rebuilds == 0
        assert p.messages_list.deleted_all == 0

    def test_only_the_changed_row_is_rewritten(self):
        records = [_msg("a", 100), _msg("b", 200), _msg("c", 300)]
        p = _panel_with_cache(records)

        records[1]["status"] = "READ"
        p._set_records(records)
        p._repaint()

        assert set(p.messages_list.texts) == {1}
        assert "READ" in p.messages_list.texts[1]

    def test_focus_is_never_touched(self):
        """The whole point: no Focus()/Select() means no EVT_LIST_ITEM_FOCUSED,
        so the screen reader stays quiet on a row the user never left."""
        records = [_msg("a", 100), _msg("b", 200)]
        p = _panel_with_cache(records)

        records[0]["status"] = "DELIVERY_ACK"
        p._set_records(records)
        p._repaint()

        assert p.messages_list.focused == []
        assert p.messages_list.selected == []

    def test_the_fingerprint_moves_forward(self):
        """Left behind, the next poll would find the same mismatch and rebuild
        anyway — the repaint would only have delayed the announcement."""
        records = [_msg("a", 100), _msg("b", 200)]
        p = _panel_with_cache(records)

        records[0]["status"] = "READ"
        p._set_records(records)
        p._repaint()

        assert p._messages_signature_cache == p._messages_signature()


class TestAReactionRepaintsTheRowItDecorates:
    def test_a_new_reaction_repaints_its_target_and_nothing_else(self):
        """A reaction is not a row. It changes the text of another one, and the
        signature does not say which — which is exactly why
        _append_new_tail_rows() refuses it and why this rung has to resolve the
        target itself."""
        records = [_msg("a", 100), _msg("b", 200)]
        p = _panel_with_cache(records)

        records.append(_reaction("r1", 300, target="a"))
        p._set_records(records)

        assert p._repaint() is True
        assert p.rebuilds == 0
        assert set(p.messages_list.texts) == {0}
        assert "👍" in p.messages_list.texts[0]

    def test_the_reaction_map_is_rebuilt_before_the_row_is_written(self):
        """Repainting first would write the OLD reaction back into the row that
        just changed."""
        records = [_msg("a", 100)]
        p = _panel_with_cache(records)

        records.append(_reaction("r1", 300, target="a", emoji="🎉"))
        p._set_records(records)
        p._repaint()

        assert p._reaction_map.get("a")
        assert "🎉" in p.messages_list.texts[0]

    def test_a_reaction_for_an_off_screen_target_falls_back(self):
        """Nothing to repaint — only the rebuild can show it."""
        records = [_msg("a", 100)]
        p = _panel_with_cache(records)

        records.append(_reaction("r1", 300, target="paginated-out"))
        p._set_records(records)

        assert p._repaint() is False


class TestItRefusesAnythingThatMovesARow:
    def test_a_new_displayable_message_falls_back(self):
        """A row appears; _append_new_tail_rows() owns the tail case and the
        rebuild owns the rest. Placing a row is never this method's job."""
        records = [_msg("a", 100), _msg("b", 200)]
        p = _panel_with_cache(records)

        records.append(_msg("c", 300))
        p._set_records(records)

        assert p._repaint() is False

    def test_a_removed_record_falls_back(self):
        records = [_msg("a", 100), _msg("b", 200)]
        p = _panel_with_cache(records)

        p._set_records(records[:1])

        assert p._repaint() is False

    def test_a_changed_timestamp_falls_back(self):
        """The list is sorted by timestamp, so a row whose sort key moved may
        not belong where it currently sits."""
        records = [_msg("a", 100), _msg("b", 200)]
        p = _panel_with_cache(records)

        records[0]["messageTimestamp"] = 500
        p._set_records(records)

        assert p._repaint() is False

    def test_a_list_out_of_step_with_the_control_falls_back(self):
        """A targeted SetItemText would write the right text into the wrong
        row. Same guard as _repaint_message_rows()."""
        records = [_msg("a", 100), _msg("b", 200)]
        p = _panel_with_cache(records)
        p.messages_list._count = 5

        records[0]["status"] = "READ"
        p._set_records(records)

        assert p._repaint() is False

    def test_the_placeholder_list_falls_back(self):
        records = [_msg("a", 100)]
        p = _panel_with_cache(records)
        p._sorted_messages = [{"_type": "empty_placeholder"}]
        p.messages_list._count = 1

        records[0]["status"] = "READ"
        p._set_records(records)

        assert p._repaint() is False

    def test_no_cached_signature_falls_back(self):
        """First refresh after a rebuild has nothing to diff against."""
        p = _Panel([_msg("a", 100)])
        assert p._repaint() is False


class TestTheExtractedHelpersMatchTheRebuild:
    """The rung derives its reaction map from the same two helpers
    populate_messages() uses, rather than a second opinion about them."""

    def test_records_are_sorted_by_timestamp(self):
        p = _Panel([])
        out = p._sorted_deduped_records([_msg("b", 300), _msg("a", 100)])
        assert [m["key"]["id"] for m in out] == ["a", "b"]

    def test_the_last_duplicate_wins(self):
        p = _Panel([])
        out = p._sorted_deduped_records(
            [_msg("a", 100, body="old"), _msg("a", 100, body="new")]
        )
        assert len(out) == 1 and out[0]["message"]["conversation"] == "new"

    def test_a_record_without_an_id_is_kept(self):
        p = _Panel([])
        anon = _msg("", 100)
        assert p._sorted_deduped_records([anon]) == [anon]

    def test_an_empty_emoji_removes_that_senders_reaction(self):
        p = _Panel([])
        records = p._sorted_deduped_records([
            _reaction("r1", 100, target="a", emoji="👍"),
            _reaction("r2", 200, target="a", emoji=""),
        ])
        assert p._reaction_map_from_sorted(records).get("a") == {}
