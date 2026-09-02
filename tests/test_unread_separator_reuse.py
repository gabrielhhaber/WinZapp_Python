"""Tests for ConversationsPanel.on_incoming_message() not reusing a stale
unread separator.

Reported live: once the user had read past the unread separator (focus
already passed it, mark-as-read fired), a subsequent new message in the same
open conversation just bumped that SAME separator's count instead of
replacing it — so the separator kept sitting above messages the user had
already read, with a count that kept accumulating (e.g. showing "3 unread"
after the user had genuinely only left 1 message unread). The fix flips
``_sep_anchors_read_position`` to True the moment the separator is marked read
(_on_message_focused()), so the next live message takes the
"replace-with-a-fresh-separator-reset-to-1" branch instead of the
"just-increment" branch. That is now the ONLY situation that resets the
count: a separator placed at conversation-open time still sits above
genuinely unread messages, so there a new message adds to it — see
tests/test_unread_separator_durability.py.

ConversationsPanel is a wx.Panel and cannot be instantiated without a running
wx.App, so on_incoming_message() is exercised against a small stub carrying
fake list-widget methods — same approach as tests/test_message_bookmarks.py.
"""

from ui.conversations import ConversationsPanel


class _FakeMessagesList:
    def __init__(self):
        self.items = []  # list of rendered strings, index-aligned with _sorted_messages

    def Freeze(self):
        pass

    def Thaw(self):
        pass

    def InsertItem(self, pos, text):
        self.items.insert(pos, text)

    def DeleteItem(self, pos):
        del self.items[pos]

    def Append(self, row):
        self.items.append(row[0])

    def SetItemText(self, pos, text):
        self.items[pos] = text


class _FakeMainWindow:
    def _allow_ui_focus_changes(self):
        return False


def _msg(mid, from_me=False, msg_type="conversation"):
    return {"key": {"id": mid, "fromMe": from_me}, "messageType": msg_type,
            "message": {"conversation": "hi"}}


class _Stub:
    on_incoming_message = ConversationsPanel.on_incoming_message
    _matches_open_conversation = ConversationsPanel._matches_open_conversation
    _is_separator = ConversationsPanel._is_separator
    # Bound from the real class rather than faked: on_incoming_message() calls
    # these, and a hand-written stand-in would be free to drift from what the
    # panel actually does. _FakeMessagesList already provides the DeleteItem
    # the first one needs.
    _clear_empty_placeholder = ConversationsPanel._clear_empty_placeholder
    _recompute_unread_sep_idx = ConversationsPanel._recompute_unread_sep_idx
    _counts_toward_unread_separator = ConversationsPanel._counts_toward_unread_separator
    _update_unread_separator_for_incoming = (
        ConversationsPanel._update_unread_separator_for_incoming
    )
    _anchor_below_unread_separator = ConversationsPanel._anchor_below_unread_separator

    def __init__(self):
        self.main_window = _FakeMainWindow()
        self.messages_list = _FakeMessagesList()
        self._sorted_messages = []
        self._unread_sep_idx = -1
        self._sep_anchors_read_position = False
        self._unread_sep_marked_read = False
        self._first_unread_msg_id = None
        self._first_unread_count = 0
        self._messages_signature_cache = None
        self.conversation = {"remoteJid": "5511999999999@s.whatsapp.net"}
        self._current_audio_id = None
        self._reaction_map = {}
        self._render_separator = lambda count: f"__sep__{count}"
        self._render_message_line = (
            lambda msg, *a, **kw: self._render_separator(msg.get("count", 1))
            if self._is_separator(msg)
            else msg["key"]["id"]
        )


def _sep(stub):
    assert stub._unread_sep_idx >= 0
    return stub._sorted_messages[stub._unread_sep_idx]


class TestSeparatorNotReusedAfterBeingRead:
    def test_a_fresh_separator_still_just_increments_normally(self):
        """Baseline: no read has happened yet — a second live message should
        still accumulate onto the same live separator, as before."""
        stub = _Stub()
        stub.on_incoming_message(stub.conversation["remoteJid"], _msg("m1"))
        stub.on_incoming_message(stub.conversation["remoteJid"], _msg("m2"))

        assert _sep(stub)["count"] == 2

    def test_separator_is_replaced_once_the_user_has_read_past_it(self):
        """User reads past the separator (simulated: mark-as-read fired, the
        flags _on_message_focused() sets). A new message arrives — it must
        get its OWN fresh separator (count=1), not bump the old one to 2."""
        stub = _Stub()
        stub.on_incoming_message(stub.conversation["remoteJid"], _msg("m1"))
        assert _sep(stub)["count"] == 1

        # Simulate _on_message_focused() having marked the separator read.
        stub._unread_sep_marked_read = True
        stub._sep_anchors_read_position = True

        stub.on_incoming_message(stub.conversation["remoteJid"], _msg("m2"))

        sep = _sep(stub)
        assert sep["count"] == 1, "must reset to 1, not accumulate to 2"
        # The old separator row is gone; only the new one remains in the list.
        assert sum(1 for m in stub._sorted_messages if stub._is_separator(m)) == 1

    def test_marking_read_re_arms_for_the_very_next_message_too(self):
        """The re-arm (_unread_sep_marked_read reset to False) happens inside
        on_incoming_message() itself for EVERY new message, so a second
        message right after the read-past one still increments normally
        (it hasn't been read yet)."""
        stub = _Stub()
        stub.on_incoming_message(stub.conversation["remoteJid"], _msg("m1"))
        stub._unread_sep_marked_read = True
        stub._sep_anchors_read_position = True
        stub.on_incoming_message(stub.conversation["remoteJid"], _msg("m2"))
        assert stub._unread_sep_marked_read is False

        stub.on_incoming_message(stub.conversation["remoteJid"], _msg("m3"))
        assert _sep(stub)["count"] == 2


class TestOwnMessagesNeverGetSeparatorTreatment:
    """Regression: on_incoming_message() also runs for the WebSocket echo of
    OUR OWN just-sent message (when it isn't matched to its pending row by
    main.py's by-type matching, e.g. sent from another linked device). An
    own message must never insert/relocate/increment the unread separator —
    it broke Alt+2 ("jump to last message"), which could land on a
    separator/stale row placed just above the user's own just-sent message
    instead of the message itself."""

    def test_an_own_message_appends_without_touching_the_separator(self):
        stub = _Stub()
        stub.on_incoming_message(stub.conversation["remoteJid"], _msg("m1"))  # from someone else
        assert stub._unread_sep_idx >= 0
        sep_idx_before = stub._unread_sep_idx
        sep_count_before = _sep(stub)["count"]

        stub.on_incoming_message(stub.conversation["remoteJid"], _msg("own1", from_me=True))

        assert stub._unread_sep_idx == sep_idx_before
        assert _sep(stub)["count"] == sep_count_before
        # The own message is still appended as the true last row.
        assert stub._sorted_messages[-1]["key"]["id"] == "own1"

    def test_an_own_message_never_creates_a_separator_from_scratch(self):
        stub = _Stub()
        stub.on_incoming_message(stub.conversation["remoteJid"], _msg("own1", from_me=True))
        assert stub._unread_sep_idx == -1

    def test_an_own_message_does_not_replace_a_sep_anchors_read_position_separator(self):
        stub = _Stub()
        stub.on_incoming_message(stub.conversation["remoteJid"], _msg("m1"))
        stub._sep_anchors_read_position = True  # simulate: this separator was from conversation-open

        stub.on_incoming_message(stub.conversation["remoteJid"], _msg("own1", from_me=True))

        assert stub._sep_anchors_read_position is True  # untouched
        assert stub._sorted_messages[-1]["key"]["id"] == "own1"


class TestStaleSeparatorIndexDoesNotCrash:
    """Reported live: "Limpar conversa" on the currently open chat reset
    _sorted_messages to [] without also resetting _unread_sep_idx/
    _sep_anchors_read_position back to -1/False. A live message arriving right after hit
    the _sep_anchors_read_position branch with old_idx still pointing at the pre-clear
    position, and _sorted_messages.pop(old_idx) on the now-empty list raised
    "IndexError: pop from empty list". Covers both the direct crash (list
    emptied out) and the general case (index merely out of range) — every
    branch that reads/pops _unread_sep_idx must treat a stale index the same
    as "no separator yet" instead of trusting it blindly."""

    def test_index_pointing_into_a_since_emptied_list_does_not_crash(self):
        stub = _Stub()
        stub.on_incoming_message(stub.conversation["remoteJid"], _msg("m1"))
        assert stub._unread_sep_idx >= 0
        stub._sep_anchors_read_position = True

        # Simulate "Limpar conversa" clearing the list without resetting the
        # separator bookkeeping (the actual bug, now fixed at the clear
        # site too — this test covers the on_incoming_message() side).
        stub._sorted_messages = []
        stub.messages_list.items = []

        stub.on_incoming_message(stub.conversation["remoteJid"], _msg("m2"))  # must not raise

        assert _sep(stub)["count"] == 1
        assert stub._sorted_messages[-1]["key"]["id"] == "m2"

    def test_index_merely_out_of_range_does_not_crash(self):
        stub = _Stub()
        stub.on_incoming_message(stub.conversation["remoteJid"], _msg("m1"))
        stub._sep_anchors_read_position = True
        stub._unread_sep_idx = 99  # stale — nowhere near len(_sorted_messages)

        stub.on_incoming_message(stub.conversation["remoteJid"], _msg("m2"))  # must not raise

        assert _sep(stub)["count"] == 1
class TestSystemEventsThroughTheRealPath:
    """O portão de contabilidade, exercitado pelo on_incoming_message() de
    verdade e não por uma reimplementação dos seus passos.

    main.py só sobe o unreadCount de um chat para mensagens que passam por
    is_countable_message() (:5813); enquanto este caminho olhava apenas
    fromMe, um evento de sistema chegando numa conversa aberta subia o
    separador sem subir o preview — mais uma fonte da divergência entre os
    dois números exatamente do tipo relatado. Sem um caso que atravesse o
    método real, dá para apagar o `and self._counts_toward_unread_separator(
    msg)` da chamada e a suíte fica verde.
    """

    def test_a_system_event_gets_a_row_but_no_separator(self):
        stub = _Stub()
        stub.on_incoming_message(
            stub.conversation["remoteJid"], _msg("sys1", msg_type="groupNotification")
        )

        assert stub._unread_sep_idx == -1
        assert stub._first_unread_msg_id is None
        # A mensagem em si não é engolida: o append acontece fora do ramo do
        # separador, e a linha continua na lista (é exibível, só não contável).
        assert stub.messages_list.items == ["sys1"]
        assert [m["key"]["id"] for m in stub._sorted_messages] == ["sys1"]

    def test_a_system_event_does_not_bump_an_existing_separator(self):
        stub = _Stub()
        stub.on_incoming_message(stub.conversation["remoteJid"], _msg("m1"))
        assert _sep(stub)["count"] == 1

        stub.on_incoming_message(
            stub.conversation["remoteJid"], _msg("sys1", msg_type="protocolMessage")
        )

        assert _sep(stub)["count"] == 1
        assert stub._first_unread_count == 1
        # Consequência aceita e documentada em _counts_toward_unread_separator():
        # o separador diz 1 com DUAS linhas abaixo dele, porque a segunda é um
        # evento de sistema que o badge do preview também não contou.
        assert stub._sorted_messages[stub._unread_sep_idx + 1:][0]["key"]["id"] == "m1"
        assert len(stub._sorted_messages) - stub._unread_sep_idx - 1 == 2
