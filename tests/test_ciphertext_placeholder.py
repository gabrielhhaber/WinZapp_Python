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
