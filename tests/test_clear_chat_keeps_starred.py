"""Tests for MainWindow.clear_chat_messages_local() preserving starred
messages instead of wiping the whole conversation.

Reported live: "clear conversation" deleted every message unconditionally,
including ones the user had explicitly starred/favorited to keep — starring
a message is supposed to make it durable, matching WhatsApp's own behavior.

MainWindow is a wx.Frame and can't be instantiated without a running wx.App,
so the methods under test are bound onto a plain stub — same approach as
the rest of this test suite.
"""

import time

from core.utils import clear_chat_applied, clear_chat_keep_starred_echo
from main import MainWindow


class _FakeDB:
    def __init__(self):
        self.delete_calls = []
        self.upsert_calls = []

    def delete_chat_messages_except(self, remote_jid, keep_message_ids):
        self.delete_calls.append((remote_jid, list(keep_message_ids or [])))

    def upsert_chat(self, jid, chat):
        self.upsert_calls.append(jid)


class _Stub:
    clear_chat_messages_local = MainWindow.clear_chat_messages_local
    _is_cleared_message = MainWindow._is_cleared_message
    _recompute_chat_last_message = MainWindow._recompute_chat_last_message
    _counts_as_last_message = MainWindow._counts_as_last_message
    _record_starred_clear_cutoff = MainWindow._record_starred_clear_cutoff

    def __init__(self, chat):
        self.chats = {"jid1": chat}
        self.settings = {}
        self.db = _FakeDB()
        self._schedule_save_calls = []

    def save_settings(self):
        pass

    def _schedule_save(self, dirty_jid=None):
        self._schedule_save_calls.append(dirty_jid)


def _msg(msg_id, ts, starred=False):
    return {
        "key": {"remoteJid": "jid1", "fromMe": False, "id": msg_id},
        "message": {"conversation": f"text {msg_id}"},
        "messageType": "conversation",
        "messageTimestamp": ts,
        "starred": starred,
    }


def _chat_with(records):
    return {
        "remoteJid": "jid1",
        "messages": {"messages": {"records": records}},
        "lastMessage": records[-1] if records else None,
        "t": records[-1]["messageTimestamp"] if records else 0,
        "unreadCount": 5,
    }


class TestClearChatKeepsStarredMessages:
    def test_non_starred_messages_are_removed(self):
        chat = _chat_with([_msg("a", 100), _msg("b", 200)])
        stub = _Stub(chat)

        stub.clear_chat_messages_local("jid1")

        records = chat["messages"]["messages"]["records"]
        assert records == []

    def test_starred_messages_survive(self):
        chat = _chat_with([_msg("a", 100), _msg("b", 200, starred=True), _msg("c", 300)])
        stub = _Stub(chat)

        stub.clear_chat_messages_local("jid1")

        records = chat["messages"]["messages"]["records"]
        assert [m["key"]["id"] for m in records] == ["b"]

    def test_db_delete_keeps_only_starred_message_ids(self):
        chat = _chat_with([_msg("a", 100), _msg("b", 200, starred=True)])
        stub = _Stub(chat)

        stub.clear_chat_messages_local("jid1")

        assert stub.db.delete_calls == [("jid1", ["b"])]

    def test_db_delete_with_no_starred_messages_keeps_nothing(self):
        chat = _chat_with([_msg("a", 100), _msg("b", 200)])
        stub = _Stub(chat)

        stub.clear_chat_messages_local("jid1")

        assert stub.db.delete_calls == [("jid1", [])]

    def test_last_message_recomputed_from_the_surviving_starred_message(self):
        chat = _chat_with([_msg("a", 100), _msg("b", 200, starred=True)])
        stub = _Stub(chat)

        stub.clear_chat_messages_local("jid1")

        assert chat["lastMessage"]["key"]["id"] == "b"
        assert chat["t"] == 200

    def test_last_message_is_none_when_nothing_survives(self):
        chat = _chat_with([_msg("a", 100), _msg("b", 200)])
        stub = _Stub(chat)

        stub.clear_chat_messages_local("jid1")

        assert chat["lastMessage"] is None
        # "t" keeps its pre-clear value (not reset to 0) so the chat stays at
        # its current position in the list instead of sorting to the bottom.
        assert chat["t"] == 200

    def test_unread_count_is_always_reset(self):
        chat = _chat_with([_msg("a", 100, starred=True)])
        stub = _Stub(chat)

        stub.clear_chat_messages_local("jid1")

        assert chat["unreadCount"] == 0

    def test_record_cutoff_still_recorded_when_starred_messages_survive(self):
        chat = _chat_with([_msg("a", 100, starred=True)])
        stub = _Stub(chat)

        before = int(time.time())
        stub.clear_chat_messages_local("jid1", record_cutoff=True)

        assert stub.settings["cleared_chats"]["jid1"] >= before


class TestStarredMessagesSurviveTheClearCutoff:
    """clear_chat_messages_local() keeping the starred records in memory and
    in the database was only half the job: every path that rebuilds a
    conversation afterwards — the history sync, the on-disk cache merge, a
    WebSocket re-delivery — runs its candidates through
    _is_cleared_message() and drops anything older than the clear cutoff.
    Starred survivors are older than the cutoff by definition, so they were
    filtered straight back out and vanished on the next sync or restart —
    reported live as "limpar uma conversa tambem apaga as mensagens
    favoritas".
    """

    def _stub_with_cutoff(self, cutoff=500):
        stub = _Stub(_chat_with([_msg("a", 100)]))
        stub.settings = {"cleared_chats": {"jid1": cutoff}}
        return stub

    def test_an_ordinary_pre_clear_message_is_still_dropped(self):
        stub = self._stub_with_cutoff()

        assert stub._is_cleared_message("jid1", _msg("a", 100)) is True

    def test_a_starred_pre_clear_message_is_kept(self):
        stub = self._stub_with_cutoff()

        assert stub._is_cleared_message("jid1", _msg("a", 100, starred=True)) is False

    def test_a_post_clear_message_is_kept_whether_starred_or_not(self):
        stub = self._stub_with_cutoff()

        assert stub._is_cleared_message("jid1", _msg("b", 900)) is False
        assert stub._is_cleared_message("jid1", _msg("b", 900, starred=True)) is False

    def test_a_chat_with_no_cutoff_keeps_everything(self):
        stub = _Stub(_chat_with([_msg("a", 100)]))
        stub.settings = {}

        assert stub._is_cleared_message("jid1", _msg("a", 100)) is False

    def test_a_non_dict_message_does_not_crash_the_starred_check(self):
        stub = self._stub_with_cutoff()

        assert stub._is_cleared_message("jid1", {}) is False

    def test_the_survivors_of_a_real_clear_all_pass_the_cutoff(self):
        """The two halves together: clear the chat, then re-run every record
        it kept through the filter the next sync would apply."""
        chat = _chat_with([_msg("a", 100), _msg("b", 200, starred=True)])
        stub = _Stub(chat)

        stub.clear_chat_messages_local("jid1")

        survivors = chat["messages"]["messages"]["records"]
        assert [m["key"]["id"] for m in survivors] == ["b"]
        assert [m for m in survivors if stub._is_cleared_message("jid1", m)] == []



class TestClearingWithoutKeepingStarred:
    """The confirmation now carries WhatsApp Web's "keep starred messages"
    checkbox. Unticked, starred messages go too — locally, in the database,
    and (once the server confirms) through the cutoff every later sync applies."""

    def test_starred_messages_are_removed_too(self):
        chat = _chat_with([_msg("a", 100), _msg("b", 200, starred=True)])
        stub = _Stub(chat)

        stub.clear_chat_messages_local("jid1", keep_starred=False)

        assert chat["messages"]["messages"]["records"] == []
        assert stub.db.delete_calls == [("jid1", [])]
        assert chat["lastMessage"] is None
        # Position in the list is kept, same as a clear with no survivors.
        assert chat["t"] == 200

    def test_the_local_clear_returns_its_cutoff_but_records_no_starred_one(self):
        """The starred cutoff waits for the server — see
        TestClearChatForwardsTheChoiceToTheServer."""
        stub = _Stub(_chat_with([_msg("b", 200, starred=True)]))

        before = int(time.time())
        cutoff = stub.clear_chat_messages_local("jid1", keep_starred=False)

        assert cutoff >= before
        assert stub.settings["cleared_chats"]["jid1"] == cutoff
        assert "cleared_starred_chats" not in stub.settings

    def test_mirroring_a_phone_clear_returns_no_cutoff(self):
        stub = _Stub(_chat_with([_msg("a", 100)]))

        assert stub.clear_chat_messages_local("jid1", record_cutoff=False) is None

    def test_once_recorded_the_next_sync_does_not_bring_them_back(self):
        chat = _chat_with([_msg("a", 100), _msg("b", 200, starred=True)])
        stub = _Stub(chat)

        cutoff = stub.clear_chat_messages_local("jid1", keep_starred=False)
        stub._record_starred_clear_cutoff("jid1", cutoff)

        assert stub._is_cleared_message("jid1", _msg("b", 200, starred=True)) is True
        assert stub._is_cleared_message("jid1", _msg("a", 100)) is True

    def test_a_later_clear_that_keeps_starred_does_not_resurrect_them(self):
        stub = _Stub(_chat_with([_msg("a", 100)]))
        stub.settings = {
            "cleared_chats": {"jid1": 500},
            "cleared_starred_chats": {"jid1": 500},
        }
        stub.chats["jid1"] = _chat_with([_msg("c", 600, starred=True)])

        stub.clear_chat_messages_local("jid1", keep_starred=True)

        # Dropped by the first clear: stays dropped.
        assert stub._is_cleared_message("jid1", _msg("b", 200, starred=True)) is True
        # Starred after it and kept by the second clear: survives.
        assert stub._is_cleared_message("jid1", _msg("c", 600, starred=True)) is False

    def test_the_starred_cutoff_never_moves_backwards(self):
        stub = _Stub(_chat_with([_msg("a", 100)]))
        stub.settings = {"cleared_starred_chats": {"jid1": 900}}

        stub._record_starred_clear_cutoff("jid1", 500)

        assert stub.settings["cleared_starred_chats"]["jid1"] == 900

    def test_a_starred_message_newer_than_the_starred_cutoff_is_kept(self):
        stub = _Stub(_chat_with([_msg("a", 100)]))
        stub.settings = {"cleared_chats": {"jid1": 500}, "cleared_starred_chats": {"jid1": 500}}

        assert stub._is_cleared_message("jid1", _msg("n", 900, starred=True)) is False

    def test_a_non_dict_message_is_never_cleared(self):
        stub = _Stub(_chat_with([_msg("a", 100)]))
        stub.settings = {"cleared_chats": {"jid1": 500}}

        assert stub._is_cleared_message("jid1", None) is False


class TestClearChatForwardsTheChoiceToTheServer:
    """An older client/api ignores keepStarred and keeps the starred messages
    on the phone. Recording the starred cutoff anyway would hide them in
    WinZapp for good while they are still on the phone, so it is recorded
    only when the server echoes keepStarred=false."""

    class _ApiStub(_Stub):
        clear_chat = MainWindow.clear_chat
        _announce_starred_clear_unsupported = MainWindow._announce_starred_clear_unsupported

        def __init__(self, chat):
            super().__init__(chat)
            self.wpp_server = "http://127.0.0.1"
            self.wpp_port = 6300
            self.token = "tok"
            self.i18n = type("I18n", (), {"t": lambda self, key: key})()
            self.announced = []

        def output(self, text, *a, **kw):
            self.announced.append(text)

    def _run(self, monkeypatch, response_body=None, ok=True, **kwargs):
        posted = []

        class _Resp:
            status_code = 201 if ok else 500
            text = ""

            def __init__(self):
                self.ok = ok

            def json(self):
                if response_body is None:
                    raise ValueError("no json")
                return response_body

        def _fake_post(url, json=None, **kw):
            posted.append((url, json))
            return _Resp()

        class _SyncThread:
            def __init__(self, target=None, daemon=None, **kw):
                self._target = target

            def start(self):
                self._target()

        monkeypatch.setattr("main.api_post", _fake_post)
        monkeypatch.setattr("main.threading.Thread", _SyncThread)
        monkeypatch.setattr("main.wx.CallAfter", lambda fn, *a, **kw: fn(*a, **kw))
        stub = self._ApiStub(_chat_with([_msg("a", 100), _msg("b", 200, starred=True)]))
        stub.clear_chat("jid1", **kwargs)
        return stub, posted

    def test_confirmed_by_the_server_records_the_starred_cutoff(self, monkeypatch):
        stub, posted = self._run(
            monkeypatch, {"response": {"keepStarred": False, "data": {"jid1": True}}},
            keep_starred=False,
        )

        (url, body), = posted
        assert url.endswith("/clear-chat")
        assert body["keepStarred"] is False
        assert stub.chats["jid1"]["messages"]["messages"]["records"] == []
        assert stub.settings["cleared_starred_chats"]["jid1"] == stub.settings["cleared_chats"]["jid1"]
        assert stub.announced == []

    def test_an_outdated_server_records_no_starred_cutoff_and_says_so(self, monkeypatch):
        stub, _ = self._run(
            monkeypatch, {"response": {"data": {}}}, keep_starred=False,
        )

        assert "cleared_starred_chats" not in stub.settings
        # So the next sync brings them back, matching the phone.
        assert stub._is_cleared_message("jid1", _msg("b", 200, starred=True)) is False
        assert stub.announced == ["clear_chat_starred_kept_on_phone"]

    def test_an_unconfirmed_clear_records_no_starred_cutoff(self, monkeypatch):
        """The controller echoes keepStarred=false even when WhatsApp Web
        answered the clear itself with a failure (data[phone] False, nothing
        thrown, still HTTP 201). Found in review: the echo alone recorded the
        cutoff and hid starred messages still on the phone for good."""
        stub, _ = self._run(
            monkeypatch, {"response": {"keepStarred": False, "data": {"jid1": False}}},
            keep_starred=False,
        )

        assert "cleared_starred_chats" not in stub.settings
        assert stub._is_cleared_message("jid1", _msg("b", 200, starred=True)) is False
        # Not the "outdated server" sentence: the server is fine, the clear failed.
        assert stub.announced == []

    def test_a_failed_request_records_no_starred_cutoff(self, monkeypatch):
        stub, _ = self._run(monkeypatch, None, ok=False, keep_starred=False)

        assert "cleared_starred_chats" not in stub.settings

    def test_default_keeps_starred_on_both_sides(self, monkeypatch):
        stub, posted = self._run(monkeypatch, {"response": {"keepStarred": True}})

        (_, body), = posted
        assert body["keepStarred"] is True
        assert [m["key"]["id"] for m in stub.chats["jid1"]["messages"]["messages"]["records"]] == ["b"]
        assert "cleared_starred_chats" not in stub.settings
        assert stub.announced == []

    def test_a_bulk_clear_on_an_outdated_server_announces_only_once(self, monkeypatch):
        """20 chats selected must not read the same long sentence 20 times."""
        stub, _ = self._run(monkeypatch, {"response": {"data": {}}}, keep_starred=False)
        stub.chats["jid1"] = _chat_with([_msg("c", 300, starred=True)])

        stub.clear_chat("jid1", keep_starred=False)

        assert stub.announced == ["clear_chat_starred_kept_on_phone"]

    def test_the_announcement_names_the_real_menu_path_without_mnemonics(self):
        texts = {
            "clear_chat_starred_kept_on_phone": "use {menu} > {option}",
            "menu_help": "A&juda",
            "menu_force_reinstall_wpp": "Forçar reinstalação da &WPPConnect",
        }
        stub = self._ApiStub(_chat_with([_msg("a", 100)]))
        stub.i18n = type("I18n", (), {"t": lambda self, key: texts[key]})()

        stub._announce_starred_clear_unsupported()

        assert stub.announced == ["use Ajuda > Forçar reinstalação da WPPConnect"]


class TestClearChatApplied:
    def test_only_an_explicit_true_for_that_phone_counts(self):
        assert clear_chat_applied({"response": {"data": {"5511@c.us": True}}}, "5511@c.us") is True
        for body in (None, {}, {"response": {}}, {"response": {"data": None}},
                     {"response": {"data": {"5511@c.us": False}}},
                     {"response": {"data": {"5511@c.us": "true"}}},
                     {"response": {"data": {"other@c.us": True}}}):
            assert clear_chat_applied(body, "5511@c.us") is False


class TestClearChatKeepStarredEcho:
    def test_explicit_values_are_returned(self):
        assert clear_chat_keep_starred_echo({"response": {"keepStarred": False}}) is False
        assert clear_chat_keep_starred_echo({"response": {"keepStarred": True}}) is True

    def test_anything_else_is_none(self):
        for body in (None, [], "x", {}, {"response": None}, {"response": {}},
                     {"response": {"keepStarred": "false"}}, {"response": {"keepStarred": 0}}):
            assert clear_chat_keep_starred_echo(body) is None
