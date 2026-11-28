"""Unsent drafts survive a restart, and only ever encrypted (ACC-06).

core/message_drafts.py (format), the mixin's unbound methods on a plain stub,
and the database bridge's queued write on a real temporary database. No window
opens.
"""

import json
import logging
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from core import message_drafts
from core.message_drafts import Draft, MessageDrafts
from ui.conversation_panel.message_drafts import MessageDraftsMixin

KEY = Fernet.generate_key()
ANA, BIA = "ana@s.whatsapp.net", "bia@s.whatsapp.net"
SECRET = "senha do cofre 12345"


def _token(drafts, key=KEY):
    return message_drafts.seal(message_drafts.to_payload(drafts), key)


class TestFormat:
    def test_roundtrip_keeps_text_and_mentions(self):
        drafts = {ANA: Draft("oi @Carla\nlinha 2 ação", ["carla@lid"], {"carla@lid": "Carla"})}
        back = message_drafts.decode(_token(drafts), KEY)
        assert back[ANA].text == "oi @Carla\nlinha 2 ação"
        assert back[ANA].mentions == ["carla@lid"]
        assert back[ANA].mention_names == {"carla@lid": "Carla"}

    def test_the_token_never_holds_the_text_or_the_jid(self):
        token = _token({ANA: Draft(SECRET, ["carla@lid"], {"carla@lid": "Carla"})})
        for plain in (SECRET, ANA, "carla", "Carla"):
            assert plain not in token
        assert SECRET.encode() not in token.encode()

    def test_nothing_to_keep_is_an_empty_token(self):
        assert message_drafts.to_payload({}) == ""
        assert message_drafts.to_payload({ANA: Draft("  \n")}) == ""
        assert message_drafts.seal("", KEY) == ""
        assert message_drafts.decode("", KEY) == {}
        assert message_drafts.decode(None, KEY) == {}

    @pytest.mark.parametrize("bad", [
        "not a token", "gAAAAA", "x" * 300, "é", 12, b"bytes", ["list"]])
    def test_a_damaged_token_is_no_drafts(self, bad):
        assert message_drafts.decode(bad, KEY) == {}

    def test_a_truncated_token_is_no_drafts(self):
        token = _token({ANA: Draft("texto")})
        assert message_drafts.decode(token[: len(token) // 2], KEY) == {}

    def test_another_key_is_no_drafts(self):
        assert message_drafts.decode(_token({ANA: Draft("texto")}), Fernet.generate_key()) == {}

    @pytest.mark.parametrize("payload", [
        "[]", "null", "{}", json.dumps({"v": 99, "d": {}}),
        json.dumps({"v": 1, "d": []}), json.dumps({"v": 1, "d": {ANA: "text"}}),
        json.dumps({"v": 1, "d": {ANA: {"t": 5}}}), json.dumps({"v": 1}),
    ])
    def test_sealed_but_misshapen_is_no_drafts(self, payload):
        assert message_drafts.decode(message_drafts.seal(payload, KEY), KEY) == {}

    def test_bad_mentions_are_dropped_not_fatal(self):
        payload = json.dumps({"v": 1, "d": {ANA: {"t": "oi", "m": ["a@lid", 3], "n": {"a@lid": "A", "b": 1}}}})
        back = message_drafts.decode(message_drafts.seal(payload, KEY), KEY)
        assert back[ANA].mentions == ["a@lid"] and back[ANA].mention_names == {"a@lid": "A"}

    def test_a_file_cannot_make_the_app_load_more_than_the_bound(self):
        many = {f"{n}@s.whatsapp.net": Draft("x") for n in range(message_drafts.MAX_PERSISTED + 20)}
        assert len(message_drafts.decode(_token(many), KEY)) == message_drafts.MAX_PERSISTED


class TestSetting:
    def test_on_by_default(self):
        from core.utils import DEFAULT_SETTINGS
        assert DEFAULT_SETTINGS["user_interface"][message_drafts.PERSIST_SETTING] is True
        assert message_drafts.persist_enabled({}) is True
        assert message_drafts.persist_enabled(None) is True

    def test_off_and_never_without_per_conversation_mode(self):
        ui = message_drafts.PERSIST_SETTING
        assert message_drafts.persist_enabled({"user_interface": {ui: False}}) is False
        for mode in (message_drafts.DISCARD, message_drafts.KEEP_IN_FIELD):
            assert message_drafts.persist_enabled(
                {"user_interface": {ui: True, "message_draft_mode": mode}}) is False


class _Db:
    def __init__(self, token=""):
        self.token = token
        self.queued = []
        self.fail_write = False
        self.fail_read = False

    def get_metadata(self, key, default=None):
        assert key == message_drafts.METADATA_KEY
        if self.fail_read:
            raise RuntimeError("disk")
        return self.token

    def queue_message_drafts(self, token):
        if self.fail_write:
            raise RuntimeError("closed")
        self.queued.append(token)
        self.token = token


class _Field:
    def __init__(self, value=""):
        self.value = value

    def GetValue(self):
        return self.value

    def SetValue(self, value):
        self.value = value

    def SetInsertionPointEnd(self):
        pass


class _Panel(MessageDraftsMixin):
    def __init__(self, db=None, settings=None):
        self.main_window = SimpleNamespace(
            db=db, key=KEY, settings=settings or {},
            _normalize_jid=lambda jid: jid, _chat_jids_equivalent=lambda a, b: a == b)
        self.message_field = _Field()
        self.conversation = None
        self._editing_message_id = None
        self._pending_mentions = []
        self._pending_mention_display_names = {}

    def _on_cancel_edit(self):
        pass

    def _rebuild_mention_pills(self):
        pass

    def type(self, text):
        """What the user does: the field changes (the debounce is not armed)."""
        self.message_field.value = text
        self._draft_timer = SimpleNamespace(Restart=lambda ms: None)
        self._note_draft_changed(text)

    def open(self, jid):
        self._stash_message_draft()
        self.conversation = {"remoteJid": jid}
        self._restore_message_draft()

    def stored(self):
        return message_drafts.decode(self.main_window.db.token, KEY)


class TestPersisting:
    def test_a_draft_left_behind_is_on_disk_encrypted(self):
        db = _Db()
        panel = _Panel(db)
        panel.open(ANA)
        panel.type(SECRET)
        panel._pending_mentions = ["carla@lid"]
        panel._pending_mention_display_names = {"carla@lid": "Carla"}
        panel.open(BIA)
        assert db.queued and SECRET not in db.queued[-1]
        assert panel.stored()[ANA].text == SECRET
        assert panel.stored()[ANA].mentions == ["carla@lid"]

    def test_the_open_conversations_text_is_saved_too(self):
        """A restart with the conversation still open must not lose its text."""
        db = _Db()
        panel = _Panel(db)
        panel.open(ANA)
        panel.type("ainda digitando")
        panel.flush_message_drafts()
        assert panel.stored()[ANA].text == "ainda digitando"

    def test_the_next_run_gets_it_back_in_the_conversation(self):
        db = _Db()
        first = _Panel(db)
        first.open(ANA)
        first.type("para depois")
        first._pending_mentions = ["carla@lid"]
        first.flush_message_drafts()
        second = _Panel(db)
        second.open(ANA)
        assert second.message_field.value == "para depois"
        assert second._pending_mentions == ["carla@lid"]
        # Another conversation shows none of it.
        second.open(BIA)
        assert second.message_field.value == ""

    def test_a_draft_restored_and_left_alone_is_still_saved(self):
        db = _Db(_token({ANA: Draft("guardado")}))
        panel = _Panel(db)
        panel.open(ANA)
        panel.flush_message_drafts()
        assert panel.stored()[ANA].text == "guardado"

    def test_sent_or_erased_leaves_nothing(self):
        db = _Db()
        panel = _Panel(db)
        panel.open(ANA)
        panel.type("vou apagar")
        panel.flush_message_drafts()
        assert db.token
        panel.type("")
        panel.flush_message_drafts()
        assert db.token == "" and panel.stored() == {}

    def test_nothing_is_written_when_nothing_changed(self):
        db = _Db()
        panel = _Panel(db)
        panel.open(ANA)
        panel.flush_message_drafts()
        panel.flush_message_drafts()
        assert db.queued == []

    def test_a_message_being_edited_is_not_a_draft(self):
        db = _Db()
        panel = _Panel(db)
        panel.open(ANA)
        panel._editing_message_id = "MSG"
        panel.type("texto editado")
        panel.flush_message_drafts()
        assert panel.stored() == {}

    def test_a_locked_conversations_draft_is_saved_encrypted_and_restored_when_it_opens(self):
        """The vault closing closes the conversation (stash); the draft is in the
        one opaque token, and only opening the chat (which needs the vault open)
        hands it back."""
        db = _Db()
        panel = _Panel(db)
        locked = "cofre@s.whatsapp.net"
        panel.open(locked)
        panel.type(SECRET)
        panel._stash_message_draft()
        panel.conversation = None
        assert SECRET not in db.token and locked not in db.token
        again = _Panel(db)
        assert again.message_field.value == ""
        again.open(locked)
        assert again.message_field.value == SECRET

    def test_deleting_the_conversation_deletes_its_draft_on_disk(self):
        db = _Db()
        panel = _Panel(db)
        panel.open(ANA)
        panel.type("texto de Ana")
        panel.open(BIA)
        panel.type("texto de Bia")
        panel.flush_message_drafts()
        panel.forget_message_draft(ANA)
        assert set(panel.stored()) == {BIA}
        panel.forget_message_draft(BIA)
        assert db.token == ""

    def test_logout_or_account_switch_forgets_everything(self):
        db = _Db()
        panel = _Panel(db)
        panel.open(ANA)
        panel.type("conta antiga")
        panel.open(BIA)
        written = len(db.queued)
        panel.discard_message_drafts()
        assert len(panel._message_drafts()) == 0
        # The table itself is wiped by the caller (save_full_state); nothing
        # of the old account is written back after it.
        panel.flush_message_drafts()
        assert len(db.queued) == written
        panel.open(ANA)
        assert panel.message_field.value == ""

    def test_turning_it_off_erases_what_is_on_disk(self):
        db = _Db()
        panel = _Panel(db)
        panel.open(ANA)
        panel.type("texto")
        panel.flush_message_drafts()
        assert db.token
        panel.main_window.settings = {"user_interface": {message_drafts.PERSIST_SETTING: False}}
        panel.flush_message_drafts()
        assert db.token == ""

    def test_off_at_start_erases_what_the_last_run_left(self):
        db = _Db(_token({ANA: Draft("velho")}))
        panel = _Panel(db, {"user_interface": {message_drafts.PERSIST_SETTING: False}})
        panel.open(ANA)
        assert panel.message_field.value == ""
        panel.flush_message_drafts()
        assert db.token == ""

    def test_a_corrupt_stored_token_is_no_drafts_and_gets_replaced(self):
        db = _Db("lixo que nao e um token")
        panel = _Panel(db)
        panel.open(ANA)
        assert panel.message_field.value == ""
        panel.type("novo")
        panel.flush_message_drafts()
        assert panel.stored()[ANA].text == "novo"

    def test_a_database_that_fails_never_raises_into_the_window(self, caplog):
        db = _Db(_token({ANA: Draft("x")}))
        db.fail_read = True
        db.fail_write = True
        panel = _Panel(db)
        with caplog.at_level(logging.WARNING):
            panel.open(ANA)
            panel.type(SECRET)
            panel.open(BIA)
            panel.flush_message_drafts()
        assert panel.message_field.value == ""
        # The log names the exception type, never the text.
        assert SECRET not in caplog.text

    def test_no_database_is_plain_ram_drafts(self):
        panel = _Panel(db=None)
        panel.open(ANA)
        panel.type("só na memória")
        panel.open(BIA)
        panel.open(ANA)
        assert panel.message_field.value == "só na memória"
        panel.flush_message_drafts()

    def test_a_draft_typed_this_run_is_not_replaced_by_the_stored_one(self):
        store = MessageDrafts()
        store.stash(ANA, "novo")
        store.load({ANA: Draft("velho"), BIA: Draft("outro")})
        assert store.snapshot()[ANA].text == "novo" and store.snapshot()[BIA].text == "outro"

    def test_typing_arms_one_debounced_write(self, monkeypatch):
        from ui.conversation_panel import draft_persistence
        made = []

        class Timer:
            def __init__(self, ms, fn):
                made.append((ms, fn))
                self.restarts = 0

            def Restart(self, ms):
                self.restarts += 1

        monkeypatch.setattr(draft_persistence.wx, "CallLater", Timer)
        panel = _Panel(_Db())
        panel.open(ANA)
        panel._note_draft_changed("a")
        panel._note_draft_changed("ab")
        assert len(made) == 1 and panel._draft_timer.restarts == 1


class TestStaysOutOfPlainDisk:
    def test_the_only_thing_handed_to_the_database_is_a_token(self):
        db = _Db()
        panel = _Panel(db)
        panel.open(ANA)
        panel.type(SECRET)
        panel.flush_message_drafts()
        for queued in db.queued:
            assert queued == "" or Fernet(KEY).decrypt(queued.encode())


class TestBridge:
    def test_a_queued_token_is_in_the_database_after_close_and_gone_after_a_wipe(self, tmp_path):
        from core.database_bridge import DatabaseBridge
        path = str(tmp_path / "m.db")
        token = _token({ANA: Draft(SECRET)})
        bridge = DatabaseBridge(path, KEY)
        bridge.queue_message_drafts(token)
        bridge.close()
        bridge = DatabaseBridge(path, KEY)
        try:
            assert bridge.get_metadata(message_drafts.METADATA_KEY) == token
            assert message_drafts.decode(token, KEY)[ANA].text == SECRET
            bridge.save_full_state({"chats": {}, "contacts": {}}, clear_metadata=True)
            assert not bridge.get_metadata(message_drafts.METADATA_KEY)
        finally:
            bridge.close()

    def test_the_file_on_disk_never_holds_the_text(self, tmp_path):
        from core.database_bridge import DatabaseBridge
        path = tmp_path / "m.db"
        bridge = DatabaseBridge(str(path), KEY)
        bridge.queue_message_drafts(_token({ANA: Draft(SECRET)}))
        bridge.close()
        for f in tmp_path.iterdir():
            assert SECRET.encode() not in f.read_bytes()
            assert ANA.encode() not in f.read_bytes()

    def test_the_resync_that_keeps_metadata_keeps_the_drafts(self, tmp_path):
        from core.database_bridge import DatabaseBridge
        bridge = DatabaseBridge(str(tmp_path / "m.db"), KEY)
        try:
            token = _token({ANA: Draft("fica")})
            bridge.queue_message_drafts(token)
            bridge.close()
            bridge = DatabaseBridge(str(tmp_path / "m.db"), KEY)
            bridge.save_full_state({"chats": {}, "contacts": {}}, clear_metadata=False)
            assert bridge.get_metadata(message_drafts.METADATA_KEY) == token
        finally:
            bridge.close()


class TestComposerHook:
    def test_typing_in_the_real_field_handler_reaches_the_draft(self):
        """on_change_message_field is what wx calls: it must feed the draft."""
        from types import SimpleNamespace
        from ui.conversation_panel.composer import ComposerMixin

        noted = []
        field = SimpleNamespace(GetValue=lambda: "hello")
        btn = SimpleNamespace(Show=lambda: None, Hide=lambda: None)
        stub = SimpleNamespace(
            _is_recording=False,
            _attachment_panel=SimpleNamespace(IsShown=lambda: False),
            message_field=field, send_message_btn=btn,
            record_voice_message_btn=btn, conversation=None,
            _note_draft_changed=noted.append,
            _on_text_changed_mention_check=lambda: None,
            _schedule_link_preview_check=lambda: None,
        )
        ComposerMixin.on_change_message_field(stub, None)
        assert noted == ["hello"]
        stub._restoring_draft = True
        ComposerMixin.on_change_message_field(stub, None)
        assert noted == ["hello"]
