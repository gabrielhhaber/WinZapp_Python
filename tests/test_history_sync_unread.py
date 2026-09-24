"""Regression tests for unread recovery after an offline history sync."""

from main import apply_history_sync_unread_correction, unread_after_history_sync


def _message(mid, timestamp, *, from_me=False, message_type="conversation"):
    return {
        "key": {"id": mid, "fromMe": from_me},
        "messageTimestamp": timestamp,
        "messageType": message_type,
    }


def test_new_incoming_after_last_local_message_becomes_unread():
    local = [_message("old", 100)]
    fetched = local + [_message("new-1", 101), _message("new-2", 102)]
    assert unread_after_history_sync(0, 0, fetched, local) == 2


def test_own_messages_and_technical_notifications_do_not_become_unread():
    local = [_message("old", 100)]
    fetched = local + [
        _message("mine", 101, from_me=True),
        _message("technical", 102, message_type="e2e_notification"),
    ]
    assert unread_after_history_sync(0, 0, fetched, local) == 0


def test_old_backfill_does_not_become_unread():
    local = [_message("known", 100)]
    fetched = [_message("older", 50)] + local
    assert unread_after_history_sync(0, 0, fetched, local) == 0


def test_existing_local_unread_is_preserved_and_new_arrivals_are_added():
    local = [_message("old", 100)]
    fetched = local + [_message("new", 101)]
    assert unread_after_history_sync(0, 3, fetched, local) == 4


class TestTheBadgeIsRediscountedOnceMessagesArrive:
    """get_remote_chats() discounts own sends and system events out of the
    server's unread count, but it runs while the chat still has no messages:
    the list-chats snapshot is merged before any get-messages call answers
    (measured live — chats at 21:17:30, first messages at 21:17:36). The
    discount bails out on its `not records` guard and the server's number
    survives whole.

    Reported live: "Bruna — 1 mensagem não lida" whose newest line was the
    user's own "Eu: áudio 0:03, Entregue". No chats-update was involved; the
    badge came from the snapshot merge.
    """

    @staticmethod
    def _chat(records, unread):
        # Marked raw, as get_remote_chats() leaves a count it merged before
        # the chat had any records — the only count this correction may touch.
        return {
            "unreadCount": unread,
            "messages": {"messages": {"records": records}},
            "_unread_undiscounted": True,
        }

    def test_our_own_send_stops_counting_as_unread(self):
        chat = self._chat([_message("theirs", 100),
                           _message("mine", 101, from_me=True)], 1)

        assert apply_history_sync_unread_correction("5511@s.whatsapp.net", chat)
        assert chat["unreadCount"] == 0

    def test_a_system_event_stops_counting_as_unread(self):
        chat = self._chat(
            [_message("joined", 101, message_type="groupNotification")], 1)

        assert apply_history_sync_unread_correction("120@g.us", chat)
        assert chat["unreadCount"] == 0

    def test_a_genuine_unread_keeps_its_badge(self):
        chat = self._chat([_message("old", 100), _message("theirs", 101)], 1)

        assert not apply_history_sync_unread_correction("5511@s.whatsapp.net", chat)
        assert chat["unreadCount"] == 1

    def test_only_the_tail_our_own_send_covers_is_discounted(self):
        """Two unread, the newer of which is ours: one real unread remains."""
        chat = self._chat([_message("theirs", 100),
                           _message("mine", 101, from_me=True)], 2)

        apply_history_sync_unread_correction("5511@s.whatsapp.net", chat)
        assert chat["unreadCount"] == 1

    def test_a_chat_with_no_records_is_left_alone(self):
        """The pre-fetch state this correction exists to come back for — there
        is still nothing to judge the badge against, so it must not guess."""
        chat = self._chat([], 3)

        assert not apply_history_sync_unread_correction("5511@s.whatsapp.net", chat)
        assert chat["unreadCount"] == 3

    def test_a_zero_badge_is_never_touched(self):
        chat = self._chat([_message("mine", 101, from_me=True)], 0)

        assert not apply_history_sync_unread_correction("5511@s.whatsapp.net", chat)
        assert chat["unreadCount"] == 0
