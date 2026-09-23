"""Filling an "Aguardando mensagem" from a reply that quotes it.

Reported 2026-09-23 in the WinZapp group: every message from one member who had
changed phone stayed a `ciphertext` in WhatsApp Web, for hours, while the others
read them on their phones and answered. "Oxe" quoted one of them: its
contextInfo carried the original's id, author and text ("não chega nem a 1mb").
The placeholder can show that text, in memory and on disk, until the real
decrypted copy (if it ever comes) replaces it.
"""

import inspect

import pytest

import main
from core.quote_recovery import (
    RECOVERED_FROM_QUOTE,
    awaits_real_copy,
    carry_over_recovered_quotes,
    fill_placeholders_from_replies,
    recovered_message,
    reply_context,
)
from core.utils import QUOTED_TEXT_CAP, _slim_quoted_message
from main import MainWindow

GROUP = "120363409931936700@g.us"
AUTHOR = "111@lid"
ORIGINAL_ID = "3EB079236745EBC27DC09C"
# Real prose: one long unbroken token reads as a base64 blob and is dropped.
_LONG = "uma mensagem colada bem comprida " * 40


def _placeholder(mid=ORIGINAL_ID, participant=AUTHOR):
    return {
        "key": {"remoteJid": GROUP, "fromMe": False, "id": mid, "participant": participant},
        "message": {},
        "messageType": "ciphertext",
        "messageTimestamp": 1790159249,
    }


def _reply(quoted, stanza=ORIGINAL_ID, participant=AUTHOR, mid="3EB0806BD821BDAD8DFD43"):
    return {
        "key": {"remoteJid": GROUP, "fromMe": False, "id": mid, "participant": "222@lid"},
        "message": {"extendedTextMessage": {
            "text": "Oxe",
            "contextInfo": {"stanzaId": stanza, "participant": participant,
                            "quotedMessage": quoted},
        }},
        "messageType": "extendedTextMessage",
        "messageTimestamp": 1790159256,
    }


def _same(a, b):
    return a == b


class TestReadingTheQuote:
    def test_the_reply_context_is_found_inside_the_message(self):
        reply = _reply({"conversation": "oi"})
        assert reply_context(reply)["stanzaId"] == ORIGINAL_ID

    def test_a_message_that_quotes_nothing_has_no_reply_context(self):
        assert reply_context({"message": {"conversation": "oi"}}) is None
        assert reply_context(None) is None

    def test_a_text_quote_rebuilds_a_text_message(self):
        assert recovered_message({"conversation": "não chega nem a 1mb"}) == (
            "conversation", {"conversation": "não chega nem a 1mb"})

    def test_the_wppconnect_shape_is_read_too(self):
        assert recovered_message({"type": "chat", "body": "oi"}) == (
            "conversation", {"conversation": "oi"})

    def test_mentions_survive_as_an_extended_text(self):
        kind, message = recovered_message(
            {"conversation": "@333 olha", "mentionedJid": ["333@lid"]})
        assert kind == "extendedTextMessage"
        assert message["extendedTextMessage"]["contextInfo"]["mentionedJid"] == ["333@lid"]

    def test_a_quoted_photo_is_not_recovered(self):
        """Media is fetched by id, and WhatsApp Web still holds a ciphertext."""
        assert recovered_message({"type": "image", "caption": "olha"}) is None
        assert recovered_message({"imageMessage": {}}) is None

    def test_a_quote_the_slimming_may_have_cut_is_not_recovered(self):
        stored = _slim_quoted_message({"conversation": _LONG})
        assert len(stored["conversation"]) == QUOTED_TEXT_CAP
        assert recovered_message(stored) is None

    def test_a_long_quote_that_was_never_slimmed_is_recovered_whole(self):
        kind, message = recovered_message({"conversation": _LONG})
        assert message["conversation"] == _LONG

    def test_an_empty_quote_is_not_recovered(self):
        assert recovered_message({}) is None
        assert recovered_message(None) is None


class TestFillingThePlaceholder:
    def test_the_quoted_placeholder_takes_the_quoted_text(self):
        placeholder = _placeholder()
        records = [placeholder, _reply({"conversation": "não chega nem a 1mb"})]

        filled = fill_placeholders_from_replies(records, _same)

        assert filled == [placeholder]
        assert placeholder["messageType"] == "conversation"
        assert placeholder["message"] == {"conversation": "não chega nem a 1mb"}
        assert placeholder[RECOVERED_FROM_QUOTE] is True

    def test_a_quote_naming_another_author_is_refused(self):
        """The quote is the replier's claim; it must at least name the
        placeholder's own author."""
        placeholder = _placeholder()
        records = [placeholder, _reply({"conversation": "forjado"}, participant="999@lid")]

        assert fill_placeholders_from_replies(records, _same) == []
        assert placeholder["messageType"] == "ciphertext"

    def test_the_author_is_compared_across_address_forms(self):
        placeholder = _placeholder(participant="5511999999999@s.whatsapp.net")
        records = [placeholder, _reply({"conversation": "oi"}, participant=AUTHOR)]

        filled = fill_placeholders_from_replies(records, lambda a, b: {a, b} == {
            AUTHOR, "5511999999999@s.whatsapp.net"})

        assert filled == [placeholder]

    def test_in_a_one_to_one_chat_the_author_is_the_chat(self):
        placeholder = _placeholder(participant="")
        placeholder["key"]["remoteJid"] = "5511@s.whatsapp.net"
        reply = _reply({"conversation": "oi"}, participant="5511@s.whatsapp.net")

        assert fill_placeholders_from_replies([placeholder, reply], _same) == [placeholder]

    def test_an_own_placeholder_is_left_alone(self):
        placeholder = _placeholder(participant="")
        placeholder["key"]["fromMe"] = True
        reply = _reply({"conversation": "oi"}, participant="me@s.whatsapp.net")

        assert fill_placeholders_from_replies([placeholder, reply], _same) == []

    def test_a_decrypted_message_is_never_overwritten(self):
        original = _placeholder()
        original.update(messageType="conversation", message={"conversation": "real"})
        records = [original, _reply({"conversation": "outro"})]

        assert fill_placeholders_from_replies(records, _same) == []
        assert original["message"] == {"conversation": "real"}

    def test_only_the_given_replies_are_read(self):
        placeholder = _placeholder()
        records = [placeholder, _reply({"conversation": "oi"})]

        assert fill_placeholders_from_replies(records, _same, replies=[]) == []


class TestTheRealCopyStillWins:
    def test_a_recovered_record_still_awaits_the_real_copy(self):
        placeholder = _placeholder()
        fill_placeholders_from_replies([placeholder, _reply({"conversation": "oi"})], _same)
        assert awaits_real_copy(placeholder) is True

    def test_a_sync_copy_still_undecrypted_keeps_the_recovered_text(self):
        local = _placeholder()
        fill_placeholders_from_replies([local, _reply({"conversation": "oi"})], _same)
        server_copy = _placeholder()

        assert carry_over_recovered_quotes([server_copy], [local]) == 1
        assert server_copy["message"] == {"conversation": "oi"}
        assert server_copy[RECOVERED_FROM_QUOTE] is True

    def test_a_decrypted_sync_copy_is_left_as_the_server_sent_it(self):
        local = _placeholder()
        fill_placeholders_from_replies([local, _reply({"conversation": "oi"})], _same)
        server_copy = _placeholder()
        server_copy.update(messageType="conversation", message={"conversation": "real"})

        assert carry_over_recovered_quotes([server_copy], [local]) == 0
        assert server_copy["message"] == {"conversation": "real"}

    def test_filling_with_the_real_copy_drops_the_marker(self, monkeypatch):
        monkeypatch.setattr(main.wx, "CallAfter", lambda fn, *a, **k: fn(*a, **k))
        window = _Window()
        record = _placeholder()
        fill_placeholders_from_replies([record, _reply({"conversation": "oi"})], _same)

        window._fill_stored_placeholder(
            record, {"key": dict(record["key"]), "messageType": "conversation",
                     "message": {"conversation": "real"}}, GROUP)

        assert RECOVERED_FROM_QUOTE not in record
        assert record["message"] == {"conversation": "real"}

    def test_the_dedup_treats_a_recovered_record_like_a_placeholder(self):
        src = inspect.getsource(MainWindow.on_new_message)
        assert "if not awaits_real_copy(existing):" in src


# ── MainWindow wiring, on a stub ──────────────────────────────────────────────


class _InlineExecutor:
    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)


class _Db:
    def __init__(self, stored=None):
        self.stored = stored or {}
        self.inserted = []

    def get_message_by_id(self, remote_jid, message_id):
        return self.stored.get(message_id)

    def insert_message(self, remote_jid, message):
        self.inserted.append((remote_jid, (message.get("key") or {}).get("id")))


class _Panel:
    def __init__(self):
        self.refreshes = 0

    def refresh_messages_if_changed(self):
        self.refreshes += 1


class _Window:
    _recover_placeholders_from_replies = MainWindow._recover_placeholders_from_replies
    _recover_quoted_placeholder = MainWindow._recover_quoted_placeholder
    _run_quote_recovery_write = MainWindow._run_quote_recovery_write
    _fill_stored_placeholder = MainWindow._fill_stored_placeholder

    def __init__(self, stored=None):
        self.db = _Db(stored)
        self._msg_bg_executor = _InlineExecutor()
        self.conversations_panel = _Panel()

    def _chat_jids_equivalent(self, left, right):
        return left == right

    def _schedule_save(self, dirty_jid=None):
        pass

    def _schedule_set_chats(self):
        pass


@pytest.fixture
def _call_after_inline(monkeypatch):
    monkeypatch.setattr(main.wx, "CallAfter", lambda fn, *a, **k: fn(*a, **k))


class TestTheLiveReply:
    def test_a_resident_placeholder_is_filled_persisted_and_repainted(self, _call_after_inline):
        window = _Window()
        placeholder = _placeholder()
        reply = _reply({"conversation": "não chega nem a 1mb"})
        records = [placeholder, reply]

        window._recover_quoted_placeholder(GROUP, records, reply)

        assert placeholder["message"] == {"conversation": "não chega nem a 1mb"}
        assert window.db.inserted == [(GROUP, ORIGINAL_ID)]
        assert window.conversations_panel.refreshes == 1

    def test_an_older_placeholder_is_filled_on_disk_by_id(self, _call_after_inline):
        """Only the newest messages are resident: the database is where an
        older "Aguardando mensagem" lives, and where reopening reads it."""
        stored = _placeholder()
        window = _Window(stored={ORIGINAL_ID: stored})
        reply = _reply({"conversation": "não chega nem a 1mb"})

        window._recover_quoted_placeholder(GROUP, [reply], reply)

        assert stored["message"] == {"conversation": "não chega nem a 1mb"}
        assert window.db.inserted == [(GROUP, ORIGINAL_ID)]

    def test_a_reply_to_an_ordinary_message_writes_nothing(self, _call_after_inline):
        original = _placeholder()
        original.update(messageType="conversation", message={"conversation": "real"})
        window = _Window(stored={ORIGINAL_ID: original})
        reply = _reply({"conversation": "real"})

        window._recover_quoted_placeholder(GROUP, [reply], reply)
        window._recover_quoted_placeholder(GROUP, [original, reply], reply)

        assert window.db.inserted == []
        assert window.conversations_panel.refreshes == 0

    def test_a_message_that_is_not_a_reply_does_not_touch_the_database(self):
        window = _Window()
        window.db.get_message_by_id = None  # would raise if called

        window._recover_quoted_placeholder(GROUP, [], {"message": {"conversation": "oi"}})

    def test_a_failing_lookup_never_escapes(self, _call_after_inline):
        window = _Window()

        def _boom(*_a):
            raise TimeoutError("db stuck")
        window.db.get_message_by_id = _boom
        reply = _reply({"conversation": "oi"})

        window._recover_quoted_placeholder(GROUP, [reply], reply)


class TestTheStartupPass:
    """Regression, 2026-09-23: WinZapp would not start. prepare_sync() runs
    from __init__ BEFORE _msg_bg_executor exists, and the first launch with
    pairs to recover submitted its database write to it: AttributeError out of
    __init__, "O WinZapp encontrou um erro crítico ao iniciar"."""

    def test_the_executor_really_is_created_after_prepare_sync(self):
        """The precondition that made it crash. If this ever flips, the inline
        write below becomes unreachable at startup, not wrong."""
        src = inspect.getsource(MainWindow.__init__)
        assert src.index("self.prepare_sync()") < src.index("self._msg_bg_executor =")

    def test_without_the_executor_the_write_runs_now(self, _call_after_inline):
        window = _Window()
        del window._msg_bg_executor
        del window.conversations_panel  # the UI does not exist yet either
        placeholder = _placeholder()
        records = [placeholder, _reply({"conversation": "não chega nem a 1mb"})]

        assert window._recover_placeholders_from_replies(GROUP, records) == 1

        assert placeholder["message"] == {"conversation": "não chega nem a 1mb"}
        assert window.db.inserted == [(GROUP, ORIGINAL_ID)]

    def test_a_failure_to_schedule_the_write_never_escapes(self, _call_after_inline):
        window = _Window()

        class _Broken:
            def submit(self, fn):
                raise RuntimeError("cannot schedule new futures after shutdown")
        window._msg_bg_executor = _Broken()
        records = [_placeholder(), _reply({"conversation": "oi"})]

        window._recover_placeholders_from_replies(GROUP, records)  # must not raise

    def test_the_database_lookup_path_works_without_the_executor(self, _call_after_inline):
        stored = _placeholder()
        window = _Window(stored={ORIGINAL_ID: stored})
        del window._msg_bg_executor
        reply = _reply({"conversation": "oi"})

        window._recover_quoted_placeholder(GROUP, [reply], reply)

        assert window.db.inserted == [(GROUP, ORIGINAL_ID)]


class TestEveryPathIsWired:
    def test_the_live_funnel_reads_each_new_reply(self):
        src = inspect.getsource(MainWindow.on_new_message)
        assert "self._recover_quoted_placeholder(remote_jid, records, msg)" in src

    def test_the_history_funnel_reads_both_directions(self):
        src = inspect.getsource(MainWindow.on_historical_message)
        assert "self._recover_quoted_placeholder(remote_jid, records, msg)" in src
        assert "self._recover_placeholders_from_replies(remote_jid, records)" in src

    def test_the_sync_carries_and_fills_before_its_batch_write(self):
        src = inspect.getsource(MainWindow.sync_chat_messages)
        write = src.index("insert_messages_batch")
        assert src.index("carry_over_recovered_quotes(") < write
        assert src.index("fill_placeholders_from_replies(") < write

    def test_the_sync_fill_cannot_mark_the_fetch_failed(self):
        """sync-completion trap: an exception inside the persist try reports a
        good fetch as failed, and one failed chat holds the account unsynced."""
        src = inspect.getsource(MainWindow.sync_chat_messages)
        fill = src.index("fill_placeholders_from_replies(")
        assert src.index("persist_ok = True") > fill
        assert "quote recovery failed" in src

    def test_startup_repairs_what_is_already_on_disk(self):
        src = inspect.getsource(MainWindow.prepare_sync)
        assert "self._recover_placeholders_from_replies(jid, records)" in src


async def test_the_database_finds_a_message_by_id(in_memory_db):
    await in_memory_db.insert_message(GROUP, _placeholder())

    found = await in_memory_db.get_message_by_id(GROUP, ORIGINAL_ID)

    assert found["key"]["id"] == ORIGINAL_ID
    assert await in_memory_db.get_message_by_id(GROUP, "OTHER") is None
    assert await in_memory_db.get_message_by_id("other@g.us", ORIGINAL_ID) is None
    assert await in_memory_db.get_message_by_id(GROUP, "") is None
