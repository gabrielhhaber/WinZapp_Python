"""The placeholder WhatsApp Web sends before it has decrypted a message.

Reported live on 2026-09-08: a voice message arrived in a group and WinZapp
announced nothing at all — no sound, no unread badge, no screen-reader
announcement — and the chat row read "Mensagem incompatível" until a later
poll rewrote it. The log holds the whole mechanism in four lines:

    18:30:49  on_messages_upsert id=ACBF…B49F type=ciphertext
    18:30:49  [unread] chats-update in: …936700@g.us unread=1 previous=0
    18:30:49  [unread] …936700@g.us: no change after discounting
                       non-countable messages (already 0, previous=0)
    18:30:51  on_messages_upsert id=ACBF…B49F type=audioMessage

WhatsApp delivers the message twice: a `ciphertext` envelope first, then the
decrypted copy under the *same* key.id, 2.5 s later (4.2 s in the other
occurrence that day). WinZapp stored the placeholder, correctly refused to
count it — it carries no content — and so discounted WhatsApp's own unread=1
back to zero. The real message then hit on_new_message()'s same-id dedup and
was routed into _apply_possible_edit(), which never announces anything.

So the placeholder must not be stored. Then the decrypted copy is what it
actually is: a new message, arriving through the full notify/count path.
"""

import pytest

import main
from main import MainWindow, is_countable_message


def _msg(message_type, message=None, mid="ACBF379ADE20FB1E6C64A2F72037B49F"):
    return {
        "key": {"remoteJid": "120363409931936700@g.us", "fromMe": False, "id": mid},
        "message": message if message is not None else {},
        "messageType": message_type,
        "messageTimestamp": 1788899448,
    }


class TestRecognisingThePlaceholder:
    def test_a_ciphertext_is_a_placeholder(self):
        assert MainWindow._is_undecrypted_placeholder(_msg("ciphertext")) is True

    def test_the_decrypted_copy_is_not(self):
        assert MainWindow._is_undecrypted_placeholder(
            _msg("audioMessage", {"audioMessage": {"seconds": 3}})) is False

    def test_an_ordinary_text_message_is_not(self):
        assert MainWindow._is_undecrypted_placeholder(
            _msg("conversation", {"conversation": "oi"})) is False

    def test_a_missing_type_is_not_a_placeholder(self):
        # Only the types WhatsApp Web actually uses for this may be dropped;
        # anything else must keep flowing, however odd it looks.
        assert MainWindow._is_undecrypted_placeholder({"key": {}}) is False
        assert MainWindow._is_undecrypted_placeholder({}) is False
        assert MainWindow._is_undecrypted_placeholder(None) is False

    def test_it_is_not_a_blocklist_of_system_types(self):
        # e2e_notification and friends are already excluded by
        # is_countable_message(); they are stored, this one is not, and
        # conflating the two lists is how a real type gets dropped.
        for other in ("e2e_notification", "groupNotification", "protocolMessage"):
            assert MainWindow._is_undecrypted_placeholder(_msg(other)) is False


class TestWhyTheBadgeVanished:
    def test_the_placeholder_could_never_have_counted(self):
        """Not a bug on its own — it carries no content, so refusing to count
        it is right. Storing it is what turned that into a silent message."""
        assert is_countable_message(_msg("ciphertext")) is False

    def test_the_decrypted_copy_does_count(self):
        assert is_countable_message(
            _msg("audioMessage", {"audioMessage": {"seconds": 3}})) is True


class TestTheDropIsWiredIntoTheLiveFunnel:
    def test_on_new_message_returns_before_storing_a_placeholder(self):
        import inspect
        src = inspect.getsource(MainWindow.on_new_message)
        assert "_is_undecrypted_placeholder" in src

    def test_the_lid_mapping_is_learned_before_the_drop(self):
        """The envelope's addressing is real even when its content is not, and
        _extract_lid_mapping() is the one thing that must still see it."""
        import inspect
        src = inspect.getsource(MainWindow.on_new_message)
        assert src.index("_extract_lid_mapping") < src.index("_is_undecrypted_placeholder")


class TestTheDecryptedCopyOfAStoredPlaceholder:
    """The live funnel drops a placeholder, but a get-messages sync stores
    whatever WhatsApp Web holds, and a message that device has not decrypted
    ("Aguardando mensagem") is held as a ciphertext. Reported 2026-09-23: a
    reply in a group quoted a message that never showed up. It was stored as a
    ciphertext (hidden: not a displayable type), and when its real copy
    arrived it hit the same-id dedup and _apply_possible_edit() dropped it,
    because a placeholder has no text to compare."""

    CONNECTED_AT = 1_790_000_000

    def _records(self, *types):
        return [_msg(t, mid=f"ID{i}") for i, t in enumerate(types)]

    def test_an_older_placeholder_is_filled_in_place(self):
        records = self._records("ciphertext", "conversation")
        incoming = _msg("conversation", {"conversation": "oi"}, mid="ID0")
        incoming["messageTimestamp"] = self.CONNECTED_AT
        assert MainWindow._resolved_placeholder_is_fresh(
            records, 0, incoming, self.CONNECTED_AT) is False

    def test_the_newest_placeholder_decrypted_just_now_is_a_new_message(self):
        """A sync raced the few seconds decryption takes: the copy must still be
        announced, which only the new-message path does."""
        records = self._records("conversation", "ciphertext")
        incoming = _msg("conversation", {"conversation": "oi"}, mid="ID1")
        incoming["messageTimestamp"] = self.CONNECTED_AT + 30
        assert MainWindow._resolved_placeholder_is_fresh(
            records, 1, incoming, self.CONNECTED_AT) is True

    def test_the_newest_placeholder_decrypted_hours_later_is_filled_in_place(self):
        records = self._records("conversation", "ciphertext")
        incoming = _msg("conversation", {"conversation": "oi"}, mid="ID1")
        incoming["messageTimestamp"] = self.CONNECTED_AT - 3600
        assert MainWindow._resolved_placeholder_is_fresh(
            records, 1, incoming, self.CONNECTED_AT) is False

    def test_an_unreadable_timestamp_is_filled_in_place(self):
        records = self._records("ciphertext")
        incoming = _msg("conversation", {"conversation": "oi"}, mid="ID0")
        incoming["messageTimestamp"] = "not a number"
        assert MainWindow._resolved_placeholder_is_fresh(
            records, 0, incoming, self.CONNECTED_AT) is False

    def test_filling_replaces_the_content_on_the_same_record(self, monkeypatch):
        monkeypatch.setattr(main.wx, "CallAfter", lambda fn, *a, **k: fn(*a, **k))
        window = _FillStub()
        existing = _msg("ciphertext", mid="ID0")
        existing["_local_flag"] = True
        incoming = _msg("extendedTextMessage",
                        {"extendedTextMessage": {"text": "não chega nem a 1mb"}}, mid="ID0")

        window._fill_stored_placeholder(existing, incoming, "g@g.us")

        assert existing["messageType"] == "extendedTextMessage"
        assert existing["message"]["extendedTextMessage"]["text"] == "não chega nem a 1mb"
        assert existing["_local_flag"] is True
        assert MainWindow._is_undecrypted_placeholder(existing) is False
        assert window.db.inserted == [("g@g.us", existing)]
        # mid-history row: a rebuild, not a repaint of existing rows
        assert window.conversations_panel.refreshes == 1
        assert window.saved == ["g@g.us"]
        assert window.set_chats == 1

    def test_the_dedup_checks_for_a_placeholder_before_treating_it_as_an_edit(self):
        import inspect
        src = inspect.getsource(MainWindow.on_new_message)
        # awaits_real_copy() is the placeholder test widened to a record filled
        # from a reply's quote (core/quote_recovery.py).
        assert "awaits_real_copy(existing)" in src
        assert src.index("awaits_real_copy(existing)") < src.index(
            "self._apply_possible_edit(existing")
        assert "_fill_stored_placeholder(existing" in src


class _InlineExecutor:
    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)


class _Db:
    def __init__(self):
        self.inserted = []

    def insert_message(self, remote_jid, message):
        self.inserted.append((remote_jid, message))


class _Panel:
    def __init__(self):
        self.refreshes = 0

    def refresh_messages_if_changed(self):
        self.refreshes += 1


class _FillStub:
    _fill_stored_placeholder = MainWindow._fill_stored_placeholder

    def __init__(self):
        self.db = _Db()
        self._msg_bg_executor = _InlineExecutor()
        self.conversations_panel = _Panel()
        self.saved = []
        self.set_chats = 0

    def _schedule_save(self, dirty_jid=None):
        self.saved.append(dirty_jid)

    def _schedule_set_chats(self):
        self.set_chats += 1
