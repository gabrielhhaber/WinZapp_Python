"""A transcription kept with its message: surviving every write, and deletable.

A transcription takes minutes, and part 7 keeps it inside the message record
(`core.transcription.stored.TRANSCRIPTION_KEY`) so it is never done twice. The
server knows nothing of it, and everything that refreshes a conversation
writes the server's copy over the record — which is how it gets lost, or worse,
comes back after being deleted:

* **A resync erases it.** `insert_message()`, `insert_messages_batch()` (the
  hot path, every sync round) and `import_from_dict()` (behind `save_data()`,
  the fallback when an incremental write fails) all `INSERT OR REPLACE` the
  whole row; and `sync_chat_messages()` replaces the records in memory. Both sides
  need the rule, or it survives on disk and leaves the screen at the first
  sync — the video-duration precedent (`test_video_duration_persists.py`) paid
  for learning that.

* **Storing it undoes something else.** The flow held its copy of the message
  for minutes; writing that copy back whole would revert a delivery status, a
  reaction or an edit a sync applied meanwhile. The dedicated write touches the
  key alone, under the same lock hold as its read.

* **A deleted transcription comes back.** The same message lives in several
  dicts at once, and every one of them can be written back later. Deleting
  therefore leaves a dated tombstone, and the later decision wins everywhere.
  Nor may a save that reached the database late win over a delete made after
  it: the two go through one single-thread queue, and a text older than the
  row's decision is not re-dated into the winner.

* **The text of a withdrawn message is kept.** The contact deletes the note
  for everyone while it is being transcribed; the record becomes a
  `protocolMessage` in place, and the finished text must go nowhere.

* **A merge drops the copy that had it.** The same voice note filed under an
  `@lid` and under the phone number until the two conversations are merged;
  the merge deletes one copy, in the database and in memory, and the one it
  deletes can be the only one holding the text.

* **An own message still being sent.** Its id is a local UUID that the send
  replaces; a transcription stored under it would be orphaned, so it is refused
  and the window says so.

* **The text leaks.** It may exist only inside the encrypted payload — never in
  a clear column, never in the log, never in a request to WPPConnect.
"""

import asyncio
import inspect
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

import main as main_module
from core.database import DatabaseManager
from core.transcription import narration, stored
from core.transcription.backend import TranscriptionResult
from core.transcription.stored import TRANSCRIPTION_KEY
from core.utils import MEASURED_SECONDS_KEY
from main import MainWindow
from main_window import transcription_store
from tests.god_modules import patch_main_global
from ui.conversation_panel.reading_position import ReadingPositionMixin
from tests.test_deep_sync import _MessagesStub, _Resp
from tests.test_historical_self_chat_guard import _DedupStub
from tests.test_lid_merge_keeps_messages import _Stub as _LidMergeStub
from tests.test_transcription_message_run import _scan, _scan_source
from ui.conversations import ConversationsPanel

_JID = "5511988887777@s.whatsapp.net"
_LID = "70000000000001@lid"
_ID = "3EB0C0FFEE00112233"
# Distinctive enough that finding it anywhere it should not be is unambiguous.
_SECRET = "zzqq o cofre fica na terceira gaveta yyww"


def _audio(mid=_ID, status=None, msg_type="audioMessage", jid=_JID, **extra):
    msg = {
        "key": {"id": mid, "remoteJid": jid, "fromMe": False},
        "messageType": msg_type,
        "messageTimestamp": 1_700_000_000,
        "message": {msg_type: {"seconds": 7, "mimetype": "audio/ogg; codecs=opus"}},
    }
    if status is not None:
        msg["status"] = status
    msg.update(extra)
    return msg


def _value(text=_SECRET, at=100.0, vad_used=True):
    return {
        "text": text, "language": "pt", "language_probability": 0.97,
        "model_id": "small", "backend": "faster_whisper", "vad_used": vad_used,
        "at": at,
    }


def _text_of(msg):
    saved = stored.saved_transcription(msg)
    return saved["text"] if saved else None


# =============================================================================
#  The rule itself
# =============================================================================


class TestWhichCopyWins:
    def test_a_copy_without_the_key_inherits_the_stored_one(self):
        merged = stored.with_known_transcription(_audio(), {TRANSCRIPTION_KEY: _value()})
        assert _text_of(merged) == _SECRET

    def test_a_newer_copy_with_the_key_wins(self):
        incoming = _audio(**{TRANSCRIPTION_KEY: _value("nova", at=200.0)})
        merged = stored.with_known_transcription(incoming, {TRANSCRIPTION_KEY: _value(at=100.0)})
        assert _text_of(merged) == "nova"

    def test_an_older_copy_with_the_key_loses(self):
        """A stale copy in memory must not undo a transcription made again."""
        incoming = _audio(**{TRANSCRIPTION_KEY: _value("velha", at=100.0)})
        merged = stored.with_known_transcription(incoming, {TRANSCRIPTION_KEY: _value("nova", at=200.0)})
        assert _text_of(merged) == "nova"

    def test_a_tombstone_beats_an_older_text(self):
        incoming = _audio(**{TRANSCRIPTION_KEY: _value(at=100.0)})
        merged = stored.with_known_transcription(incoming, {TRANSCRIPTION_KEY: stored.tombstone(150.0)})
        assert _text_of(merged) is None
        assert merged[TRANSCRIPTION_KEY]["deleted"] is True

    def test_a_newer_text_beats_a_tombstone(self):
        incoming = _audio(**{TRANSCRIPTION_KEY: _value(at=300.0)})
        merged = stored.with_known_transcription(incoming, {TRANSCRIPTION_KEY: stored.tombstone(150.0)})
        assert _text_of(merged) == _SECRET

    def test_a_copy_that_is_no_longer_audio_drops_the_key(self):
        """A message deleted for everyone comes back as a protocolMessage: the
        text of what its sender withdrew is not kept."""
        revoked = _audio(msg_type="protocolMessage", **{TRANSCRIPTION_KEY: _value()})
        merged = stored.with_known_transcription(revoked, {TRANSCRIPTION_KEY: _value()})
        assert TRANSCRIPTION_KEY not in merged

    def test_a_type_passed_through_unmapped_leaves_the_stored_one_as_it_is(self):
        """websocket_client hands on a type it does not map (a `ciphertext`
        placeholder of a note not decrypted yet): it may be the voice note
        itself, and writing it must not erase the transcription for good."""
        placeholder = _audio(msg_type="ciphertext")
        merged = stored.with_known_transcription(placeholder, {TRANSCRIPTION_KEY: _value()})
        assert merged[TRANSCRIPTION_KEY] == _value()

    def test_nor_does_it_drop_or_replace_with_its_own(self):
        """Neither adopted nor dropped: what is stored stays, and a copy of
        such a type holding one with nothing stored keeps it."""
        own = _audio(msg_type="ciphertext", **{TRANSCRIPTION_KEY: _value("da copia", at=900.0)})
        assert _text_of(stored.with_known_transcription(own, {TRANSCRIPTION_KEY: _value(at=1.0)})) == _SECRET
        assert _text_of(stored.with_known_transcription(own, None)) == "da copia"

    def test_a_known_other_kind_is_left_as_it_came(self):
        image = _audio(msg_type="contactMessage")
        assert stored.with_known_transcription(image, {TRANSCRIPTION_KEY: _value()}) is image

    @pytest.mark.parametrize("junk", [None, "texto", 42, {"text": "sem data"}, {"at": True}])
    def test_junk_under_the_key_never_outranks_a_real_value(self, junk):
        merged = stored.with_known_transcription(_audio(**{TRANSCRIPTION_KEY: junk}),
                                                 {TRANSCRIPTION_KEY: _value()})
        assert _text_of(merged) == _SECRET

    def test_the_callers_dict_is_never_changed(self):
        mine = _audio()
        stored.with_known_transcription(mine, {TRANSCRIPTION_KEY: _value()})
        assert TRANSCRIPTION_KEY not in mine

    def test_a_document_carries_it_by_type_even_without_a_mimetype(self):
        doc = _audio(msg_type="documentMessage")
        doc["message"]["documentMessage"] = {}
        assert _text_of(stored.with_known_transcription(doc, {TRANSCRIPTION_KEY: _value()})) == _SECRET


class TestTheTimeOfADecision:
    def test_the_clock_when_it_is_ahead_of_everything(self):
        assert stored.next_decision_time(500.0, _value(at=100.0), None) == 500.0

    def test_after_the_latest_when_the_clock_went_back(self):
        at = stored.next_decision_time(50.0, _value(at=100.0), stored.tombstone(300.0), "junk")
        assert at > 300.0
        assert stored.newer_decision(stored.tombstone(at), stored.tombstone(300.0))["at"] == at


class TestWhatIsStored:
    def test_the_fields_needed_to_reopen_it_truthfully_and_no_segments(self):
        result = TranscriptionResult(
            text=_SECRET, language="pl", language_probability=0.4, duration_seconds=9.0,
            segments=("x",), backend="faster_whisper", model_id="medium", device="cpu",
            compute_type="int8", vad_used=False,
        )
        value = stored.record_from_result(result, 123.5)
        assert value == {
            "text": _SECRET, "language": "pl", "language_probability": 0.4,
            "model_id": "medium", "backend": "faster_whisper", "vad_used": False,
            "at": 123.5,
        }

    def test_a_reopened_transcription_made_without_the_filter_still_warns(self):
        result = TranscriptionResult(text=_SECRET, language="pt", language_probability=0.9,
                                     duration_seconds=None, vad_used=False)
        reopened = stored.as_result(stored.record_from_result(result, 1.0))
        keys = [n.i18n_key for n in narration.result_notes(reopened, "pt")]
        assert narration.VAD_UNAVAILABLE_I18N_KEY in keys

    def test_a_value_that_lost_vad_used_is_treated_as_unfiltered(self):
        value = _value()
        del value["vad_used"]
        assert stored.as_result(value).vad_used is False

    @pytest.mark.parametrize("value", [stored.tombstone(5.0), _value(text="   "), "texto", None])
    def test_nothing_readable_is_not_a_saved_transcription(self, value):
        assert stored.saved_transcription(_audio(**{TRANSCRIPTION_KEY: value})) is None


# =============================================================================
#  In memory — carry_over_transcriptions()
# =============================================================================


class TestCarryOverInMemory:
    def test_the_server_copy_replacing_the_record_keeps_it(self):
        server = [_audio()]
        assert stored.carry_over_transcriptions(server, [_audio(**{TRANSCRIPTION_KEY: _value()})]) == 1
        assert _text_of(server[0]) == _SECRET

    def test_a_tombstone_travels_too(self):
        server = [_audio()]
        stored.carry_over_transcriptions(server, [_audio(**{TRANSCRIPTION_KEY: stored.tombstone(9.0)})])
        assert server[0][TRANSCRIPTION_KEY]["deleted"] is True

    def test_the_count_is_of_transcriptions_not_of_tombstones(self):
        """It is what sync_chat_messages() logs as "saved transcription(s)"."""
        server = [_audio("A"), _audio("B")]
        carried = stored.carry_over_transcriptions(server, [
            _audio("A", **{TRANSCRIPTION_KEY: _value()}),
            _audio("B", **{TRANSCRIPTION_KEY: stored.tombstone(9.0)}),
        ])
        assert carried == 1
        assert server[1][TRANSCRIPTION_KEY]["deleted"] is True

    def test_the_newer_of_snapshot_and_live_records_is_kept(self):
        """sync_chat_messages() carries from the snapshot first and from the
        live records after; a delete landing between the two must win."""
        server = [_audio()]
        stored.carry_over_transcriptions(server, [_audio(**{TRANSCRIPTION_KEY: _value(at=1.0)})])
        stored.carry_over_transcriptions(server, [_audio(**{TRANSCRIPTION_KEY: stored.tombstone(2.0)})])
        assert _text_of(server[0]) is None

    @pytest.mark.parametrize("junk", [None, [], [None, "x", 3]])
    def test_junk_is_tolerated(self, junk):
        assert stored.carry_over_transcriptions(junk, junk) == 0

    def test_every_path_that_replaces_records_calls_it(self):
        """An extra net under the behaviour tests below: the sync (both
        merges) and the reload from the database when a conversation opens."""
        sync = inspect.getsource(MainWindow.sync_chat_messages)
        assert sync.count("carry_over_transcriptions(") == 2
        opening = inspect.getsource(ConversationsPanel.navigate_to_conversation)
        assert "self._load_conversation_page_from_db(conversation)" in opening


class TestTheSyncKeepsIt:
    """MainWindow.sync_chat_messages() itself, on test_deep_sync's stub."""

    def _stub(self, monkeypatch, local_records, api_records):
        monkeypatch.setattr(main_module.requests, "get",
                            lambda url, **kwargs: _Resp(200, {"response": api_records}))
        stub = _MessagesStub([])
        stub._normalize_fetched_messages = lambda raw, _jid: [dict(m) for m in raw]
        stub.chats = {_JID: {"remoteJid": _JID, "t": 100,
                             "messages": {"messages": {"records": local_records}}}}
        return stub

    @staticmethod
    def _synced(stub, mid=_ID):
        [record] = [r for r in stub.chats[_JID]["messages"]["messages"]["records"]
                    if r["key"]["id"] == mid]
        return record

    def test_the_server_copy_takes_the_local_text(self, monkeypatch):
        stub = self._stub(monkeypatch, [_audio(**{TRANSCRIPTION_KEY: _value()})], [_audio()])
        MainWindow.sync_chat_messages(stub, dict(stub.chats[_JID]))
        assert _text_of(self._synced(stub)) == _SECRET

    def test_the_snapshot_counts_even_when_the_live_chat_lost_it(self, monkeypatch):
        """Another write swapped the live chat for server copies during the
        fetch: only the snapshot still holds the text, so only the first
        carry-over can keep it."""
        stub = self._stub(monkeypatch,
                          [_audio(**{TRANSCRIPTION_KEY: _value()}), _audio("LOCAL-ONLY")],
                          [_audio()])

        def _swap(remote_jid, message):
            if message["key"]["id"] == "LOCAL-ONLY":
                stub.chats[_JID] = {"remoteJid": _JID, "messages": {"messages": {"records": [_audio()]}}}
            return False

        stub._is_cleared_message = _swap
        MainWindow.sync_chat_messages(stub, dict(stub.chats[_JID]))
        assert _text_of(self._synced(stub)) == _SECRET

    def test_a_delete_during_the_fetch_wins(self, monkeypatch):
        """The tombstone lands on the live record after the snapshot was
        carried over: _is_cleared_message() runs between the two."""
        live = _audio(**{TRANSCRIPTION_KEY: _value(at=100.0)})
        stub = self._stub(monkeypatch, [live, _audio("LOCAL-ONLY")], [_audio()])

        def _delete_meanwhile(remote_jid, message):
            if message["key"]["id"] == "LOCAL-ONLY":
                stored.set_on_copies([live], _ID, stored.tombstone(200.0))
            return False

        stub._is_cleared_message = _delete_meanwhile
        MainWindow.sync_chat_messages(stub, dict(stub.chats[_JID]))
        synced = self._synced(stub)
        assert _text_of(synced) is None
        assert synced[TRANSCRIPTION_KEY]["deleted"] is True


class _OpeningDb:
    def __init__(self, rows):
        self.rows = rows

    def get_messages(self, jid, limit=200):
        return [dict(r) for r in reversed(self.rows)]

    def get_message_count(self, jid):
        return len(self.rows)


class _OpeningWindow:
    def __init__(self, rows):
        self.settings = {"user_interface": {"messages_page_size": 200}}
        self.db = _OpeningDb(rows)


class _OpeningPanel(ReadingPositionMixin):
    _load_conversation_page_from_db = ConversationsPanel._load_conversation_page_from_db

    def __init__(self, rows):
        self.main_window = _OpeningWindow(rows)


class TestOpeningAConversationKeepsIt:
    """What navigate_to_conversation() reloads from the database, which can
    be a moment behind a decision still being written."""

    def test_the_records_in_hand_give_their_text_to_the_database_page(self):
        conversation = {"remoteJid": _JID, "messages": {"messages": {"records": [
            _audio(**{TRANSCRIPTION_KEY: _value()})]}}}
        _OpeningPanel([_audio()])._load_conversation_page_from_db(conversation)
        [record] = conversation["messages"]["messages"]["records"]
        assert _text_of(record) == _SECRET

    def test_and_a_delete_not_yet_written_is_not_undone(self):
        conversation = {"remoteJid": _JID, "messages": {"messages": {"records": [
            _audio(**{TRANSCRIPTION_KEY: stored.tombstone(200.0)})]}}}
        _OpeningPanel([_audio(**{TRANSCRIPTION_KEY: _value(at=100.0)})]) \
            ._load_conversation_page_from_db(conversation)
        [record] = conversation["messages"]["messages"]["records"]
        assert _text_of(record) is None


# =============================================================================
#  In the database
# =============================================================================


async def _row(db, mid=_ID):
    cursor = await db._conn.execute(
        "SELECT * FROM messages WHERE remote_jid=? AND message_id=?", (_JID, mid)
    )
    return await cursor.fetchone()


async def _stored_record(db, mid=_ID, jid=_JID):
    [msg] = [m for m in await db.get_messages(jid) if m["key"]["id"] == mid]
    return msg


async def _stored_text(db, mid=_ID, jid=_JID):
    return _text_of(await _stored_record(db, mid, jid))


def _count_message_selects(db, monkeypatch):
    """Every SELECT on the messages table from now on, as (sql, params),
    except the star read. The caller undoes the patch before reading the
    rows back.

    core.star_storage.preserve_stars() reads the same batch on its own, one
    SELECT per block of ids across every variant of the chat's JID
    (`remote_jid IN (...)`), for a different rule; what these tests count is
    the read the local fields need, which asks for the chat's exact JID."""
    conn = db._conn
    original = conn.execute
    selects = []

    def _counting(sql, *args, **kwargs):
        if (sql.lstrip().upper().startswith("SELECT") and "FROM messages" in sql
                and "remote_jid IN (" not in sql):
            selects.append((sql, tuple(args[0]) if args else ()))
        return original(sql, *args, **kwargs)

    monkeypatch.setattr(conn, "execute", _counting)
    return selects


class TestTheDatabaseKeepsIt:
    async def test_a_batch_resync_without_the_key_keeps_it(self, in_memory_db):
        await in_memory_db.insert_message(_JID, _audio())
        assert await in_memory_db.set_message_transcription(_JID, _ID, _value())

        await in_memory_db.insert_messages_batch(_JID, [_audio(), _audio("OUTRO")])

        assert await _stored_text(in_memory_db) == _SECRET

    async def test_a_single_insert_keeps_it(self, in_memory_db):
        await in_memory_db.insert_message(_JID, _audio())
        await in_memory_db.set_message_transcription(_JID, _ID, _value())

        await in_memory_db.insert_message(_JID, _audio(status=4))

        assert await _stored_text(in_memory_db) == _SECRET

    async def test_the_full_state_save_keeps_it(self, in_memory_db):
        """save_data() goes through import_from_dict(clear_first=False) with
        whatever memory holds — which may be a copy older than the row."""
        await in_memory_db.insert_message(_JID, _audio())
        await in_memory_db.set_message_transcription(_JID, _ID, _value())

        await in_memory_db.import_from_dict({"chats": {_JID: {
            "remoteJid": _JID, "messages": {"messages": {"records": [_audio()]}},
        }}}, clear_first=False)

        assert await _stored_text(in_memory_db) == _SECRET

    async def test_a_newer_copy_with_the_key_wins(self, in_memory_db):
        await in_memory_db.insert_message(_JID, _audio(**{TRANSCRIPTION_KEY: _value("velha", at=1.0)}))
        await in_memory_db.insert_message(_JID, _audio(**{TRANSCRIPTION_KEY: _value("nova", at=2.0)}))
        assert await _stored_text(in_memory_db) == "nova"

    async def test_storing_does_not_undo_a_status_that_arrived_meanwhile(self, in_memory_db):
        """The flow's copy said "sent"; a sync stored "read" during the run."""
        await in_memory_db.insert_message(_JID, _audio(status=2))
        await in_memory_db.insert_message(_JID, _audio(status=4))

        await in_memory_db.set_message_transcription(_JID, _ID, _value())

        row = await _row(in_memory_db)
        assert row["status"] == 4
        [msg] = await in_memory_db.get_messages(_JID)
        assert msg["status"] == 4 and _text_of(msg) == _SECRET

    async def test_a_missing_row_is_an_answer(self, in_memory_db):
        assert await in_memory_db.set_message_transcription(_JID, _ID, _value()) is False
        assert await in_memory_db.delete_message_transcription(_JID, _ID, 5.0) is False
        assert await in_memory_db.get_messages(_JID) == []

    async def test_a_promoted_own_message_is_not_re_keyed(self, in_memory_db):
        """update_message_id() renames the column only; the JSON still says
        the local id. Writing through INSERT OR REPLACE would re-create the row
        under that old id."""
        own = _audio("LOCAL-UUID")
        own["key"]["fromMe"] = True
        await in_memory_db.insert_message(_JID, own)
        await in_memory_db.update_message_id(_JID, "LOCAL-UUID", "REAL")

        assert await in_memory_db.set_message_transcription(_JID, "REAL", _value())

        cursor = await in_memory_db._conn.execute("SELECT message_id FROM messages")
        assert [r["message_id"] for r in await cursor.fetchall()] == ["REAL"]

    async def test_one_read_per_block_and_none_for_text(self, in_memory_db, monkeypatch):
        """insert_messages_batch() is the hot path — every sync round — and a
        SELECT per voice note there doubled its cost; the rows are read in
        one SELECT per block of ids, and text asks for none."""
        text = {"key": {"id": "T1", "remoteJid": _JID}, "messageType": "conversation",
                "messageTimestamp": 1, "message": {"conversation": "oi"}}
        selects = _count_message_selects(in_memory_db, monkeypatch)
        await in_memory_db.insert_messages_batch(_JID, [_audio("A1"), _audio("A2"), text])
        monkeypatch.undo()
        [(_, params)] = selects
        assert sorted(params[1:]) == ["A1", "A2"]

    async def test_a_batch_larger_than_a_block_reads_one_per_block(self, in_memory_db, monkeypatch):
        from core.database import _IN_CHUNK

        records = [_audio(f"A{i}") for i in range(_IN_CHUNK + 1)]
        await in_memory_db.insert_messages_batch(_JID, records)
        await in_memory_db.set_message_transcription(_JID, f"A{_IN_CHUNK}", _value())
        selects = _count_message_selects(in_memory_db, monkeypatch)
        await in_memory_db.insert_messages_batch(_JID, records)
        monkeypatch.undo()
        assert len(selects) == 2
        # Read directly: get_messages() pages, and this row is past its first page.
        last = await in_memory_db._stored_message(in_memory_db._conn, _JID, f"A{_IN_CHUNK}")
        assert _text_of(last) == _SECRET

    async def test_a_second_copy_in_the_same_batch_sees_the_first(self, in_memory_db):
        """With one read per message, a later copy of the same id read the
        row the earlier copy had just written; reading ahead must not lose
        that, or the second copy (without the key) would erase the first's."""
        await in_memory_db.insert_messages_batch(_JID, [
            _audio(**{TRANSCRIPTION_KEY: _value()}), _audio(status=4),
        ])
        record = await _stored_record(in_memory_db)
        assert _text_of(record) == _SECRET and record["status"] == 4

    async def test_the_read_and_the_write_are_one_lock_hold(self, in_memory_db, monkeypatch):
        """A sync writing the row while the transcription is being stored:
        with the lock released in between, the store would write back the
        row it read before the sync and the new status would be gone."""
        await in_memory_db.insert_message(_JID, _audio(status=2))
        original = DatabaseManager._stored_message
        state = {"sync": None}

        async def _slow_read(self, conn, remote_jid, message_id):
            found = await original(self, conn, remote_jid, message_id)
            if state["sync"] is None:
                state["sync"] = asyncio.ensure_future(self.insert_message(_JID, _audio(status=4)))
                for _ in range(20):
                    await asyncio.sleep(0)
            return found

        monkeypatch.setattr(DatabaseManager, "_stored_message", _slow_read)
        await in_memory_db.set_message_transcription(_JID, _ID, _value())
        await state["sync"]

        [msg] = await in_memory_db.get_messages(_JID)
        assert msg["status"] == 4
        assert _text_of(msg) == _SECRET


    async def test_an_unmapped_copy_does_not_erase_it(self, in_memory_db):
        await in_memory_db.insert_message(_JID, _audio())
        await in_memory_db.set_message_transcription(_JID, _ID, _value())

        await in_memory_db.insert_message(_JID, _audio(msg_type="ciphertext"))
        await in_memory_db.insert_message(_JID, _audio())

        assert await _stored_text(in_memory_db) == _SECRET

    async def test_a_withdrawn_copy_still_drops_it(self, in_memory_db):
        await in_memory_db.insert_message(_JID, _audio())
        await in_memory_db.set_message_transcription(_JID, _ID, _value())
        await in_memory_db.insert_message(_JID, _audio(msg_type="protocolMessage"))
        assert TRANSCRIPTION_KEY not in await _stored_record(in_memory_db)

    async def test_what_is_read_before_a_write(self, in_memory_db, monkeypatch):
        """Audio, documents and unmapped types need the row; a known other
        kind cannot be the same message and costs no read."""
        selects = _count_message_selects(in_memory_db, monkeypatch)
        await in_memory_db.insert_messages_batch(_JID, [
            _audio("IMG", msg_type="contactMessage"), _audio("CIPHER", msg_type="ciphertext"),
            _audio("DOC", msg_type="documentMessage"), _audio("GONE", msg_type="protocolMessage"),
        ])
        monkeypatch.undo()
        [(_, params)] = selects
        assert sorted(params[1:]) == ["CIPHER", "DOC"]

    async def test_the_full_state_save_reads_per_chat_not_per_message(self, in_memory_db, monkeypatch):
        """save_data() — the fallback when an incremental write fails — reads
        its rows in blocks the way insert_messages_batch() does: one SELECT
        per conversation here, not one per voice note."""
        records = [_audio(f"A{i}") for i in range(40)]
        await in_memory_db.insert_messages_batch(_JID, records)
        await in_memory_db.set_message_transcription(_JID, "A7", _value())
        selects = _count_message_selects(in_memory_db, monkeypatch)
        await in_memory_db.import_from_dict({"chats": {_JID: {
            "remoteJid": _JID, "messages": {"messages": {"records": records}},
        }}}, clear_first=False)
        monkeypatch.undo()

        assert len(selects) == 1
        assert await _stored_text(in_memory_db, "A7") == _SECRET

    async def test_the_full_state_save_sees_its_own_earlier_copy(self, in_memory_db):
        """The same id twice in one chat's records: the rows are read once,
        before the loop, so without handing each write back to what was read
        the second copy (without the key) would be merged against the row as
        it was before the first, and erase the text the first just wrote."""
        await in_memory_db.insert_message(_JID, _audio())
        await in_memory_db.import_from_dict({"chats": {_JID: {
            "remoteJid": _JID, "messages": {"messages": {"records": [
                _audio(**{TRANSCRIPTION_KEY: _value()}), _audio(status=4),
            ]}},
        }}}, clear_first=False)
        record = await _stored_record(in_memory_db)
        assert _text_of(record) == _SECRET and record["status"] == 4

    async def test_a_write_that_fails_on_the_second_jid_leaves_nothing_behind(
            self, in_memory_db, monkeypatch):
        """The first row's UPDATE opens a transaction; the second JID's write
        fails. Unrolled, the first UPDATE would be committed by the next
        unrelated write — after "não foi possível apagar" had been said."""
        await in_memory_db.insert_message(_JID, _audio())
        await in_memory_db.insert_message(_LID, _audio(jid=_LID))
        await in_memory_db.insert_message(_JID, _audio("OUTRA"))
        original = DatabaseManager._stored_message

        async def _second_fails(self, conn, remote_jid, message_id):
            if remote_jid == _LID:
                raise OSError("disk I/O error")
            return await original(self, conn, remote_jid, message_id)

        monkeypatch.setattr(DatabaseManager, "_stored_message", _second_fails)
        with pytest.raises(OSError):
            await in_memory_db.set_message_transcription([_JID, _LID], _ID, _value())
        monkeypatch.undo()
        # Something unrelated, which commits whatever transaction is open.
        await in_memory_db.update_message_status(_JID, "OUTRA", 4)

        assert TRANSCRIPTION_KEY not in await _stored_record(in_memory_db)

    async def test_the_full_state_save_keeps_a_measured_duration_too(self, in_memory_db):
        video = {"key": {"id": "V1", "remoteJid": _JID, "fromMe": False},
                 "messageType": "videoMessage", "messageTimestamp": 1,
                 "message": {"videoMessage": {"seconds": 0}}}
        measured = {**video, "message": {"videoMessage": {"seconds": 0, MEASURED_SECONDS_KEY: 42}}}
        await in_memory_db.insert_message(_JID, measured)
        await in_memory_db.import_from_dict({"chats": {_JID: {
            "remoteJid": _JID, "messages": {"messages": {"records": [video]}},
        }}}, clear_first=False)
        stored_video = (await _stored_record(in_memory_db, "V1"))["message"]["videoMessage"]
        assert stored_video[MEASURED_SECONDS_KEY] == 42

    async def test_an_explicit_decision_is_later_than_the_row_whatever_the_clock(self, in_memory_db):
        """The clock went back between transcribing and deleting: a tombstone
        dated before the text would lose to the next stale copy written."""
        await in_memory_db.insert_message(_JID, _audio())
        await in_memory_db.set_message_transcription(_JID, _ID, _value(at=100.0))

        assert await in_memory_db.delete_message_transcription(_JID, _ID, 50.0)
        await in_memory_db.insert_message(_JID, _audio(**{TRANSCRIPTION_KEY: _value(at=100.0)}))

        record = await _stored_record(in_memory_db)
        assert _text_of(record) is None
        assert record[TRANSCRIPTION_KEY]["at"] > 100.0

    async def test_a_text_older_than_the_rows_decision_is_not_re_dated_into_the_winner(
            self, in_memory_db):
        """A text is written only when it is at least as recent as the row's
        decision. The clock-set-back case is dated where the decision is
        made, after every copy in memory (see
        TestStoringFromMainWindow.test_it_is_dated_after_what_the_copies_hold)."""
        await in_memory_db.insert_message(_JID, _audio())
        await in_memory_db.set_message_transcription(_JID, _ID, _value("nova", at=200.0))

        assert await in_memory_db.set_message_transcription(_JID, _ID, _value("velha", at=100.0))

        record = await _stored_record(in_memory_db)
        assert _text_of(record) == "nova"
        assert record[TRANSCRIPTION_KEY]["at"] == 200.0

    async def test_a_text_at_least_as_recent_replaces_it(self, in_memory_db):
        await in_memory_db.insert_message(_JID, _audio())
        await in_memory_db.set_message_transcription(_JID, _ID, _value("velha", at=100.0))
        await in_memory_db.set_message_transcription(_JID, _ID, _value("nova", at=100.0))
        assert await _stored_text(in_memory_db) == "nova"
        await in_memory_db.set_message_transcription(_JID, _ID, _value("mais nova", at=300.0))
        assert await _stored_text(in_memory_db) == "mais nova"


class TestALateSaveDoesNotUndoADelete:
    """The user reads the result and deletes it before the save has reached a
    database busy with a sync. The save used to be re-dated after the row —
    the tombstone at 200 — and land as the text at 200.001: the tombstone in
    memory (200) did not beat it, and the next opening of the conversation
    brought the text back after "Transcrição apagada" had been said."""

    async def test_the_tombstone_stays(self, in_memory_db):
        await in_memory_db.insert_message(_JID, _audio())
        assert await in_memory_db.delete_message_transcription(_JID, _ID, 200.0)

        assert await in_memory_db.set_message_transcription(_JID, _ID, _value(at=100.0))

        record = await _stored_record(in_memory_db)
        assert _text_of(record) is None
        assert record[TRANSCRIPTION_KEY] == stored.tombstone(200.0)
        # What the next opening reads, against the tombstone memory holds.
        [page] = await in_memory_db.get_messages(_JID)
        page_copy = _audio(**{TRANSCRIPTION_KEY: stored.tombstone(200.0)})
        assert stored.carry_over_transcriptions([page], [page_copy]) == 0
        assert _text_of(page) is None

    async def test_every_jid_of_a_decision_is_one_lock_hold(self, in_memory_db, monkeypatch):
        """With a call per JID, a save and a delete could each reach one of
        the two rows first, and the rows would disagree."""
        await in_memory_db.insert_message(_JID, _audio())
        await in_memory_db.insert_message(_LID, _audio(jid=_LID))
        real_lock = in_memory_db._write_lock
        holds = []

        class _CountingLock:
            async def __aenter__(self):
                holds.append(1)
                return await real_lock.__aenter__()

            async def __aexit__(self, *exc):
                return await real_lock.__aexit__(*exc)

        monkeypatch.setattr(in_memory_db, "_write_lock", _CountingLock())
        assert await in_memory_db.set_message_transcription([_JID, _LID], _ID, _value())
        assert len(holds) == 1
        monkeypatch.undo()

        assert await _stored_text(in_memory_db) == _SECRET
        assert await _stored_text(in_memory_db, jid=_LID) == _SECRET

    async def test_a_jid_without_the_row_is_routine_not_a_failure(self, in_memory_db, caplog):
        """The @lid half of a conversation usually has no row; a line per
        such JID on every save read like a failure in log.log."""
        caplog.set_level(logging.INFO)
        await in_memory_db.insert_message(_JID, _audio())
        await in_memory_db.set_message_transcription([_JID, _LID], _ID, _value())
        assert not [r for r in caplog.records if "not written" in r.getMessage()]

        await in_memory_db.set_message_transcription([_JID, _LID], "NAO-EXISTE", _value())
        [line] = [r.getMessage() for r in caplog.records if "not written" in r.getMessage()]
        assert "no stored row" in line

    async def test_no_row_under_any_jid_is_the_only_false(self, in_memory_db):
        await in_memory_db.insert_message(_LID, _audio(jid=_LID))
        assert await in_memory_db.set_message_transcription([_JID, _LID], _ID, _value())
        assert await in_memory_db.set_message_transcription([_JID], _ID, _value()) is False


class TestAWithdrawnMessageKeepsNoText:
    """The contact deleted the note for everyone while it was being
    transcribed. The revoke may reach the disk before the save does."""

    async def _withdrawn(self, db, jid=_JID):
        await db.insert_message(jid, _audio(jid=jid))
        await db.insert_message(jid, _audio(jid=jid, msg_type="protocolMessage"))

    async def test_a_text_is_not_written_over_it(self, in_memory_db):
        await self._withdrawn(in_memory_db)

        answer = await in_memory_db.set_message_transcription(_JID, _ID, _value())

        # True: the row answered. False would send MainWindow to its
        # fallback, which writes the audio record back whole over the revoke.
        assert answer is True
        row = await _row(in_memory_db)
        assert row["message_type"] == "protocolMessage"
        assert TRANSCRIPTION_KEY not in await _stored_record(in_memory_db)

    async def test_only_the_withdrawn_row_refuses_it(self, in_memory_db):
        await in_memory_db.insert_message(_JID, _audio())
        await self._withdrawn(in_memory_db, jid=_LID)

        assert await in_memory_db.set_message_transcription([_JID, _LID], _ID, _value())

        assert await _stored_text(in_memory_db) == _SECRET
        assert TRANSCRIPTION_KEY not in await _stored_record(in_memory_db, jid=_LID)


class TestTheChatPreviewNeverHoldsIt:
    """chats.last_message_json is written by the debounced upsert_chat(),
    which can land after "Transcrição apagada" has been said."""

    @staticmethod
    async def _preview(db):
        cursor = await db._conn.execute("SELECT last_message_json FROM chats WHERE jid=?", (_JID,))
        row = await cursor.fetchone()
        return db._decrypt_json(row["last_message_json"])

    async def test_no_chat_write_puts_it_there(self, in_memory_db):
        chat = {"remoteJid": _JID, "lastMessage": _audio(**{TRANSCRIPTION_KEY: _value()})}
        await in_memory_db.upsert_chat(_JID, chat)
        assert TRANSCRIPTION_KEY not in await self._preview(in_memory_db)
        await in_memory_db.upsert_chats_batch({_JID: chat})
        assert TRANSCRIPTION_KEY not in await self._preview(in_memory_db)
        await in_memory_db.import_from_dict({"chats": {_JID: chat}}, clear_first=False)
        assert TRANSCRIPTION_KEY not in await self._preview(in_memory_db)
        assert TRANSCRIPTION_KEY in chat["lastMessage"], "the caller's dict is untouched"

    async def test_deleting_scrubs_a_preview_written_before(self, in_memory_db):
        await in_memory_db.insert_message(_JID, _audio())
        await in_memory_db.set_message_transcription(_JID, _ID, _value())
        await in_memory_db.upsert_chat(_JID, {"remoteJid": _JID})
        await in_memory_db._conn.execute(
            "UPDATE chats SET last_message_json=? WHERE jid=?",
            (in_memory_db._encrypt_json(_audio(**{TRANSCRIPTION_KEY: _value()})), _JID),
        )
        await in_memory_db._conn.commit()

        assert await in_memory_db.delete_message_transcription(_JID, _ID, 500.0)

        assert TRANSCRIPTION_KEY not in await self._preview(in_memory_db)
        assert (await self._preview(in_memory_db))["key"]["id"] == _ID


class TestMergingTwoChatsKeepsIt:
    """The blocker: a voice note filed under the @lid (live) and under the
    phone (sync), transcribed in the @lid conversation, then the @lid
    resolved and the two merged — the row holding the text was the one
    dropped for "having a twin"."""

    async def _both(self, db, lid_value=None, phone_value=None):
        await db.insert_message(_LID, _audio(jid=_LID))
        await db.insert_message(_JID, _audio())
        if lid_value is not None:
            assert await db.set_message_transcription(_LID, _ID, lid_value)
        if phone_value is not None:
            assert await db.set_message_transcription(_JID, _ID, phone_value)

    async def test_the_text_on_the_dropped_copy_survives(self, in_memory_db):
        await self._both(in_memory_db, lid_value=_value())
        await in_memory_db.merge_or_rename_chat(_LID, _JID)
        assert await in_memory_db.get_messages(_LID) == []
        assert await _stored_text(in_memory_db) == _SECRET

    async def test_a_tombstone_on_the_dropped_copy_still_wins(self, in_memory_db):
        await self._both(in_memory_db, lid_value=stored.tombstone(200.0),
                         phone_value=_value(at=100.0))
        await in_memory_db.merge_or_rename_chat(_LID, _JID)
        record = await _stored_record(in_memory_db)
        assert _text_of(record) is None and record[TRANSCRIPTION_KEY]["deleted"] is True

    async def test_the_later_of_two_texts_is_kept(self, in_memory_db):
        await self._both(in_memory_db, lid_value=_value("velha", at=100.0),
                         phone_value=_value("nova", at=300.0))
        await in_memory_db.merge_or_rename_chat(_LID, _JID)
        assert await _stored_text(in_memory_db) == "nova"

    async def test_a_measured_video_duration_survives_too(self, in_memory_db):
        def _video(jid, measured=None):
            video = {"seconds": 0}
            if measured is not None:
                video[MEASURED_SECONDS_KEY] = measured
            return {"key": {"id": "V1", "remoteJid": jid, "fromMe": False},
                    "messageType": "videoMessage", "messageTimestamp": 1,
                    "message": {"videoMessage": video}}

        await in_memory_db.insert_message(_LID, _video(_LID, measured=42))
        await in_memory_db.insert_message(_JID, _video(_JID))
        await in_memory_db.merge_or_rename_chat(_LID, _JID)
        stored_video = (await _stored_record(in_memory_db, "V1"))["message"]["videoMessage"]
        assert stored_video[MEASURED_SECONDS_KEY] == 42

    async def test_a_twin_with_no_message_type_is_still_read(self, in_memory_db):
        """NOT IN is never true for a NULL, so the SQL filter alone would
        skip a row whose type column was never filled."""
        await self._both(in_memory_db, lid_value=_value())
        await in_memory_db._conn.execute("UPDATE messages SET message_type=NULL")
        await in_memory_db._conn.commit()
        await in_memory_db.merge_or_rename_chat(_LID, _JID)
        assert await _stored_text(in_memory_db) == _SECRET

    async def test_the_survivor_is_read_only_when_the_twin_carries_something(
            self, in_memory_db, monkeypatch):
        """The step runs on every @lid resolution."""
        for mid in ("A", "B", "C"):
            await in_memory_db.insert_message(_LID, _audio(mid, jid=_LID))
            await in_memory_db.insert_message(_JID, _audio(mid))
        await in_memory_db.set_message_transcription(_LID, "B", _value())
        reads = []
        original = DatabaseManager._stored_message

        async def _counting(self, conn, remote_jid, message_id):
            reads.append(message_id)
            return await original(self, conn, remote_jid, message_id)

        monkeypatch.setattr(DatabaseManager, "_stored_message", _counting)
        await in_memory_db.merge_or_rename_chat(_LID, _JID)
        assert reads == ["B"]
        assert await _stored_text(in_memory_db, "B") == _SECRET

    async def test_text_twins_are_not_even_decrypted(self, in_memory_db, monkeypatch):
        """message_type is in clear: a long chat's text rows cost nothing."""
        for mid in ("T1", "T2"):
            for jid in (_LID, _JID):
                await in_memory_db.insert_message(jid, {
                    "key": {"id": mid, "remoteJid": jid, "fromMe": False},
                    "messageType": "conversation", "messageTimestamp": 1,
                    "message": {"conversation": "oi"},
                })
        decrypted = []
        original = DatabaseManager._decrypt_json

        def _counting(self, blob):
            decrypted.append(blob)
            return original(self, blob)

        monkeypatch.setattr(DatabaseManager, "_decrypt_json", _counting)
        await in_memory_db.merge_or_rename_chat(_LID, _JID)
        assert decrypted == []
        assert await in_memory_db.get_messages(_LID) == []


@pytest.fixture
def synchronous_merge(monkeypatch):
    class _SyncThread:
        def __init__(self, target=None, args=(), kwargs=None, daemon=None):
            self._run = lambda: target(*args, **(kwargs or {}))

        def start(self):
            self._run()

    monkeypatch.setattr(main_module.threading, "Thread", _SyncThread)


def _chat_of(jid, *records):
    return {"remoteJid": jid, "messages": {"messages": {"records": list(records)}}}


class TestMergingInMemoryKeepsIt:
    def test_the_lid_copy_gives_its_text_to_the_phone_record(self, synchronous_merge):
        phone_record = _audio()
        stub = _LidMergeStub({
            _LID: _chat_of(_LID, _audio(jid=_LID, **{TRANSCRIPTION_KEY: _value()})),
            _JID: _chat_of(_JID, phone_record),
        })
        stub._merge_lid_into_phone(_LID, _JID)
        assert _LID not in stub.chats
        [record] = stub.chats[_JID]["messages"]["messages"]["records"]
        assert record is phone_record, "the dict the panel may hold, changed in place"
        assert _text_of(record) == _SECRET

    def test_and_a_tombstone_on_it_wins(self, synchronous_merge):
        phone_record = _audio(**{TRANSCRIPTION_KEY: _value(at=100.0)})
        stub = _LidMergeStub({
            _LID: _chat_of(_LID, _audio(jid=_LID, **{TRANSCRIPTION_KEY: stored.tombstone(200.0)})),
            _JID: _chat_of(_JID, phone_record),
        })
        stub._merge_lid_into_phone(_LID, _JID)
        assert _text_of(phone_record) is None

    def test_a_measured_duration_travels_the_same_way(self, synchronous_merge):
        def _video(jid, **video):
            return {"key": {"id": "V1", "remoteJid": jid}, "messageType": "videoMessage",
                    "message": {"videoMessage": {"seconds": 0, **video}}}

        phone_record = _video(_JID)
        stub = _LidMergeStub({
            _LID: _chat_of(_LID, _video(_LID, **{MEASURED_SECONDS_KEY: 42})),
            _JID: _chat_of(_JID, phone_record),
        })
        stub._merge_lid_into_phone(_LID, _JID)
        assert phone_record["message"]["videoMessage"][MEASURED_SECONDS_KEY] == 42

    def test_deduplicate_chats_keeps_it_too(self):
        cus = _JID.replace("@s.whatsapp.net", "@c.us")
        phone_record = _audio()
        out = _DedupStub().deduplicate_chats({
            cus: _chat_of(cus, _audio(jid=cus, **{TRANSCRIPTION_KEY: _value()})),
            _JID: _chat_of(_JID, phone_record),
        })
        [record] = out[_JID]["messages"]["messages"]["records"]
        assert record is phone_record
        assert _text_of(record) == _SECRET


class TestDeletingDoesNotResurrect:
    async def _deleted(self, db):
        await db.insert_message(_JID, _audio())
        await db.set_message_transcription(_JID, _ID, _value(at=100.0))
        assert await db.delete_message_transcription(_JID, _ID, 200.0)

    async def test_it_is_gone(self, in_memory_db):
        await self._deleted(in_memory_db)
        assert await _stored_text(in_memory_db) is None

    async def test_a_stale_copy_written_whole_does_not_bring_it_back(self, in_memory_db):
        """A star toggled on the panel's old dict, a status echo — any path
        that rewrites the record from memory."""
        await self._deleted(in_memory_db)
        await in_memory_db.insert_message(_JID, _audio(**{TRANSCRIPTION_KEY: _value(at=100.0)}))
        assert await _stored_text(in_memory_db) is None

    async def test_nor_does_a_batch_or_a_full_save(self, in_memory_db):
        await self._deleted(in_memory_db)
        stale = _audio(**{TRANSCRIPTION_KEY: _value(at=100.0)})
        await in_memory_db.insert_messages_batch(_JID, [stale])
        await in_memory_db.import_from_dict({"chats": {_JID: {
            "remoteJid": _JID, "messages": {"messages": {"records": [stale]}},
        }}}, clear_first=False)
        assert await _stored_text(in_memory_db) is None

    async def test_transcribing_again_after_deleting_is_kept(self, in_memory_db):
        await self._deleted(in_memory_db)
        await in_memory_db.set_message_transcription(_JID, _ID, _value("de novo", at=300.0))
        assert await _stored_text(in_memory_db) == "de novo"


class TestTheTextIsOnlyEverEncrypted:
    async def test_no_column_holds_it_in_the_clear(self, in_memory_db):
        await in_memory_db.insert_message(_JID, _audio())
        await in_memory_db.set_message_transcription(_JID, _ID, _value())

        row = await _row(in_memory_db)
        for column in row.keys():
            assert "cofre" not in str(row[column]), column

    async def test_nor_does_the_database_file(self, tmp_path, fernet_key):
        path = tmp_path / "messages.db"
        async with DatabaseManager(str(path), fernet_key) as db:
            await db.insert_message(_JID, _audio())
            await db.set_message_transcription(_JID, _ID, _value())
            assert await _stored_text(db) == _SECRET
        blob = b"".join(p.read_bytes() for p in tmp_path.iterdir() if p.name.startswith("messages.db"))
        assert b"cofre" not in blob
        assert "cofre".encode("utf-16-le") not in blob


# =============================================================================
#  MainWindow: finding the message again, and every copy of it
# =============================================================================


class _Inline:
    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)


class _RecordingDb:
    """The bridge's calls, recorded. insert_message is here only to catch a
    regression that writes the whole record: the store runs on a background
    executor that logs and swallows, so a missing method would pass silently."""

    def __init__(self):
        self.calls = []
        self.values = []
        self.fail = False
        self.fail_insert = False
        #: What clear_model_transcriptions() answers.
        self.cleared = 0
        #: The (jid, id) rows that exist; None means every row does.
        self.rows = None

    def set_message_transcription(self, jids, msg_id, value):
        self.calls.append(("set", tuple(jids), msg_id))
        self.values.append(value)
        if self.fail:
            raise TimeoutError("busy")
        return any(self.has_row(jid, msg_id) for jid in jids)

    def delete_message_transcription(self, jids, msg_id, deleted_at):
        self.calls.append(("delete", tuple(jids), msg_id))
        self.values.append(deleted_at)
        if self.fail:
            raise TimeoutError("busy")
        return any(self.has_row(jid, msg_id) for jid in jids)

    def clear_all_transcriptions(self, decided_at):
        self.calls.append(("clear_all",))
        if self.fail:
            raise TimeoutError("busy")
        return self.cleared

    def clear_model_transcriptions(self, model_id, backend_id, decided_at):
        self.calls.append(("clear", model_id, backend_id))
        if self.fail:
            raise TimeoutError("busy")
        return self.cleared

    def insert_message(self, jid, msg):
        self.calls.append(("insert", jid, (msg.get("key") or {}).get("id")))
        self.values.append(msg)
        if self.fail_insert:
            raise TimeoutError("busy")

    def has_row(self, jid, msg_id):
        return self.rows is None or (jid, msg_id) in self.rows


class _Panel:
    def __init__(self, conversation, all_sorted=(), sorted_=()):
        self.conversation = conversation
        self._all_sorted_messages = list(all_sorted)
        self._sorted_messages = list(sorted_)


class _Sound:
    def __init__(self):
        self.played = 0

    def play(self):
        self.played += 1


class _KeysI18n:
    @staticmethod
    def t(key):
        return key


class _Window:
    def __init__(self, records, panel=None):
        self.chats = {_JID: {"remoteJid": _JID, "messages": {"messages": {"records": records}}}}
        self.conversations_panel = panel
        self.settings = {}
        self.db = _RecordingDb()
        self._transcription_write_queue = _Inline()
        self._phone_to_lid = {}
        self.i18n = _KeysI18n()
        self.error_sound = _Sound()
        self.spoken = []
        self.saves = []

    _normalize_jid = staticmethod(MainWindow._normalize_jid)
    get_chat = MainWindow.get_chat
    _transcription_copies = MainWindow._transcription_copies
    _transcription_storage_jids = MainWindow._transcription_storage_jids
    _say_transcription_not_stored = MainWindow._say_transcription_not_stored
    store_message_transcription = MainWindow.store_message_transcription
    delete_message_transcription = MainWindow.delete_message_transcription
    _all_transcription_copies = MainWindow._all_transcription_copies
    clear_model_transcriptions = MainWindow.clear_model_transcriptions
    clear_all_transcriptions = MainWindow.clear_all_transcriptions
    _clear_transcriptions = MainWindow._clear_transcriptions

    def _schedule_save(self, dirty_jid=None, contacts_dirty=False):
        self.saves.append(dirty_jid)

    def output(self, text, interrupt=False):
        self.spoken.append(text)


@pytest.fixture
def inline_call_after(monkeypatch):
    monkeypatch.setattr(main_module.wx, "CallAfter", lambda fn, *a, **k: fn(*a, **k))


class TestStoringFromMainWindow:
    def test_the_current_record_gets_it_not_the_one_the_flow_held(self):
        """A sync replaced the dict during the run; the new one is what the
        menu reads now."""
        held_by_the_flow = _audio()
        current = _audio(status=4)
        window = _Window([current])

        answer = window.store_message_transcription(_JID, _ID, _value())

        assert answer == stored.SAVE_STORED
        assert _text_of(current) == _SECRET
        assert TRANSCRIPTION_KEY not in held_by_the_flow
        assert window.db.calls == [("set", (_JID,), _ID)]
        assert window.saves == [_JID]

    def test_every_copy_is_updated(self):
        in_chat = _audio()
        panel_copy = _audio()
        page_copy = _audio()
        window = _Window([in_chat])
        window.chats[_JID]["lastMessage"] = in_chat
        window.conversations_panel = _Panel(
            {"remoteJid": _JID, "messages": {"messages": {"records": [panel_copy]}}},
            all_sorted=[panel_copy, page_copy], sorted_=[page_copy],
        )

        window.store_message_transcription(_JID, _ID, _value())

        for copy in (in_chat, panel_copy, page_copy):
            assert _text_of(copy) == _SECRET

    def test_another_conversations_panel_is_left_alone(self):
        other = _audio()
        window = _Window([_audio()])
        window.conversations_panel = _Panel({"remoteJid": "outro@g.us"}, all_sorted=[other])
        window.store_message_transcription(_JID, _ID, _value())
        assert TRANSCRIPTION_KEY not in other

    def test_a_message_still_being_sent_is_refused(self):
        pending = _audio("LOCAL-UUID", _local_pending=True, _local_id="LOCAL-UUID")
        window = _Window([pending])

        assert window.store_message_transcription(_JID, "LOCAL-UUID", _value()) == stored.SAVE_UNSENT
        assert TRANSCRIPTION_KEY not in pending
        assert window.db.calls == []

    def test_the_pending_flag_alone_is_enough(self):
        """A record that says it is still being sent is believed even when it
        lacks the _local_id the other check compares against."""
        pending = _audio(_local_pending=True)
        window = _Window([pending])
        assert window.store_message_transcription(_JID, _ID, _value()) == stored.SAVE_UNSENT
        assert TRANSCRIPTION_KEY not in pending

    def test_a_record_still_keyed_by_its_local_id_is_refused_too(self):
        """No longer flagged pending, but the send never gave it a WhatsApp
        id: `key.id` is still the `_local_id`, which is what refuses it."""
        unconfirmed = _audio("LOCAL-UUID", _local_pending=False, _local_id="LOCAL-UUID")
        window = _Window([unconfirmed])
        assert window.store_message_transcription(_JID, "LOCAL-UUID", _value()) == stored.SAVE_UNSENT
        assert TRANSCRIPTION_KEY not in unconfirmed
        assert window.db.calls == []

    def test_a_message_sent_during_the_run_is_stored_under_its_real_id(self):
        """The flow captured the local UUID; by the end the send gave the
        record its real id. Found through _local_id, written under the real one."""
        promoted = _audio("REAL", _local_pending=False, _local_id="LOCAL-UUID")
        window = _Window([promoted])

        assert window.store_message_transcription(_JID, "LOCAL-UUID", _value()) == stored.SAVE_STORED
        assert _text_of(promoted) == _SECRET
        assert window.db.calls == [("set", (_JID,), "REAL")]

    def test_a_message_deleted_for_everyone_during_the_run_is_refused(self):
        """_apply_remote_revoke() turned the very dict find_record() finds
        into a protocolMessage; the finished text goes nowhere."""
        record = _audio()
        window = _Window([record])
        _revoke(record)

        assert window.store_message_transcription(_JID, _ID, _value()) == stored.SAVE_WITHDRAWN

        assert TRANSCRIPTION_KEY not in record
        assert window.db.calls == []
        assert window.saves == []

    def test_a_message_gone_from_the_chat_is_missing(self):
        window = _Window([_audio("OUTRO")])
        assert window.store_message_transcription(_JID, _ID, _value()) == stored.SAVE_MISSING
        assert window.db.calls == []

    def test_a_failed_write_keeps_the_copy_in_memory(self, caplog, inline_call_after):
        """The copies in memory keep the text for the session, and the menu
        with them. Nothing rewrites the row on its own afterwards:
        _schedule_save(dirty_jid=jid) writes only the chat's row, whose
        preview never carries the key. The text reaches the disk only if a
        later path writes this record whole (a star, a status echo,
        save_data()), through the rule — which is why the failure is said
        (next test)."""
        record = _audio()
        window = _Window([record])
        window.db.fail = True
        with caplog.at_level(logging.DEBUG):
            assert window.store_message_transcription(_JID, _ID, _value()) == stored.SAVE_STORED
        assert _text_of(record) == _SECRET

    def test_a_failed_write_is_said_in_one_sentence(self, inline_call_after):
        window = _Window([_audio()])
        window.db.fail = True
        window.store_message_transcription(_JID, _ID, _value())
        assert window.spoken == ["transcription_store_failed"]
        assert window.error_sound.played == 1

    def test_a_row_filed_under_the_lid_gets_it(self, inline_call_after):
        """History loaded through _history_storage_jid() lives in the
        database under the @lid; a write keyed on the phone alone found no
        row and was only a log line."""
        window = _Window([_audio()])
        window._phone_to_lid = {_JID: _LID}
        window.db.rows = {(_LID, _ID)}

        window.store_message_transcription(_JID, _ID, _value())

        assert window.db.calls == [("set", (_JID, _LID), _ID)]
        assert window.spoken == []

    def test_no_row_anywhere_writes_the_record_whole_through_the_rule(self, inline_call_after):
        record = _audio(status=4)
        window = _Window([record])
        window._phone_to_lid = {_JID: _LID}
        window.db.rows = set()

        window.store_message_transcription(_JID, _ID, _value())

        assert window.db.calls[-1] == ("insert", _JID, _ID)
        written = window.db.values[-1]
        assert _text_of(written) == _SECRET and written["status"] == 4
        assert written is not record, "a snapshot, never the dict still in use"
        assert window.spoken == []

    def test_and_if_that_fails_too_it_is_said(self, inline_call_after):
        window = _Window([_audio()])
        window.db.rows = set()
        window.db.fail_insert = True
        window.store_message_transcription(_JID, _ID, _value())
        assert window.spoken == ["transcription_store_failed"]

    def test_it_is_dated_after_what_the_copies_hold(self):
        """The flow dated it when the run started; the clock went back since."""
        record = _audio(**{TRANSCRIPTION_KEY: _value("velha", at=2_000_000_000.0)})
        window = _Window([record])
        window.store_message_transcription(_JID, _ID, _value("nova", at=1_000_000_000.0))
        assert _text_of(record) == "nova"
        assert record[TRANSCRIPTION_KEY]["at"] > 2_000_000_000.0
        assert window.db.values[-1]["at"] == record[TRANSCRIPTION_KEY]["at"]


class TestDeletingFromMainWindow:
    def test_memory_follows_the_database(self, inline_call_after):
        record = _audio(**{TRANSCRIPTION_KEY: _value()})
        window = _Window([record])
        answers = []

        window.delete_message_transcription(_JID, _ID, answers.append)

        assert answers == [True]
        assert window.db.calls == [("delete", (_JID,), _ID)]
        assert _text_of(record) is None
        assert record[TRANSCRIPTION_KEY]["deleted"] is True

    def test_a_failed_delete_leaves_it_and_says_so(self, inline_call_after):
        record = _audio(**{TRANSCRIPTION_KEY: _value()})
        window = _Window([record])
        window.db.fail = True
        answers = []

        window.delete_message_transcription(_JID, _ID, answers.append)

        assert answers == [False]
        assert _text_of(record) == _SECRET

    def test_the_tombstone_is_dated_after_the_text_whatever_the_clock(
            self, inline_call_after, monkeypatch):
        monkeypatch.setattr(main_module.time, "time", lambda: 1_000_000_000.0)
        record = _audio(**{TRANSCRIPTION_KEY: _value(at=2_000_000_000.0)})
        window = _Window([record])

        window.delete_message_transcription(_JID, _ID, lambda ok: None)

        assert record[TRANSCRIPTION_KEY]["deleted"] is True
        assert record[TRANSCRIPTION_KEY]["at"] > 2_000_000_000.0
        assert window.db.values == [record[TRANSCRIPTION_KEY]["at"]]

    def test_every_jid_the_chat_is_stored_under_gets_the_tombstone(self, inline_call_after):
        window = _Window([_audio(**{TRANSCRIPTION_KEY: _value()})])
        window._phone_to_lid = {_JID: _LID}
        answers = []
        window.delete_message_transcription(_JID, _ID, answers.append)
        assert window.db.calls == [("delete", (_JID, _LID), _ID)]
        assert answers == [True]

    def test_the_tombstone_reaches_the_panels_old_copy(self, inline_call_after):
        in_chat = _audio(**{TRANSCRIPTION_KEY: _value()})
        panel_copy = _audio(**{TRANSCRIPTION_KEY: _value()})
        window = _Window([in_chat])
        window.conversations_panel = _Panel({"remoteJid": _JID}, all_sorted=[panel_copy])

        window.delete_message_transcription(_JID, _ID, lambda ok: None)

        assert _text_of(panel_copy) is None

    def test_a_newer_transcription_made_while_the_delete_waited_is_kept(
            self, inline_call_after, monkeypatch):
        """The delete waits in the queue behind a sync; the user asks for
        "Transcrever novamente" meanwhile. The queue writes the tombstone and
        then the new text (the disk ends right), and the delete's answer must
        not put the tombstone over that new text in memory, nor say
        "apagada" with its window open."""
        monkeypatch.setattr(main_module.time, "time", lambda: 1_000.0)
        record = _audio(**{TRANSCRIPTION_KEY: _value("primeira", at=100.0)})
        window = _Window([record])
        held = []
        window._transcription_write_queue = type(
            "_Held", (), {"submit": lambda self, fn, *a, **k: held.append(fn)})()
        answers = []

        window.delete_message_transcription(_JID, _ID, answers.append)
        window.store_message_transcription(_JID, _ID, _value("segunda", at=2_000.0))
        for job in held:
            job()

        assert [call[0] for call in window.db.calls] == ["delete", "set"]
        tombstone_at = window.db.values[0]
        assert tombstone_at < 2_000.0
        assert _text_of(record) == "segunda"
        assert answers == [], "nothing is said about a deletion already superseded"
        assert window.spoken == []

    def test_one_copy_with_a_newer_transcription_is_enough_to_say_nothing(
            self, inline_call_after, monkeypatch):
        """A partial acceptance: the chat's copy still holds the old text and
        takes the tombstone, while a copy reloaded from the database already
        carries a newer transcription and refuses it. "Apagada" said then
        would be false for the text still on that copy — and the copy that
        took the tombstone gets the newer text back, as the disk has it, so
        memory does not end half deleted until the next reload."""
        monkeypatch.setattr(main_module.time, "time", lambda: 1_000.0)
        in_chat = _audio(**{TRANSCRIPTION_KEY: _value("primeira", at=100.0)})
        reloaded = _audio(**{TRANSCRIPTION_KEY: _value("primeira", at=100.0)})
        window = _Window([in_chat])
        window.conversations_panel = _Panel({"remoteJid": _JID}, all_sorted=[reloaded])
        held = []
        window._transcription_write_queue = type(
            "_Held", (), {"submit": lambda self, fn, *a, **k: held.append(fn)})()
        answers = []

        window.delete_message_transcription(_JID, _ID, answers.append)
        # The newer text reaches only the reloaded copy before the delete runs.
        reloaded[TRANSCRIPTION_KEY] = _value("segunda", at=5_000.0)
        for job in held:
            job()

        assert _text_of(in_chat) == "segunda"
        assert _text_of(reloaded) == "segunda"
        assert in_chat[TRANSCRIPTION_KEY] is reloaded[TRANSCRIPTION_KEY]
        assert answers == []
        assert window.spoken == []

    def test_a_message_no_longer_in_memory_is_still_said_deleted(self, inline_call_after):
        """No copy to disagree with: the disk has the tombstone, which is
        what the user asked for."""
        window = _Window([_audio(**{TRANSCRIPTION_KEY: _value()})])
        answers = []
        window.chats[_JID]["messages"]["messages"]["records"] = []
        window.delete_message_transcription(_JID, _ID, answers.append)
        assert answers == [True]


class TestSetWhereNewer:
    def test_only_copies_it_is_not_older_than(self):
        old = _audio(**{TRANSCRIPTION_KEY: _value(at=100.0)})
        newer = _audio(**{TRANSCRIPTION_KEY: _value(at=300.0)})
        bare = _audio()
        other = _audio("OUTRO")
        tomb = stored.tombstone(200.0)
        assert stored.set_where_newer([old, newer, bare, other, old], _ID, tomb) == (2, 3)
        assert old[TRANSCRIPTION_KEY] is tomb and bare[TRANSCRIPTION_KEY] is tomb
        assert _text_of(newer) == _SECRET
        assert TRANSCRIPTION_KEY not in other

    def test_the_same_decision_twice_is_accepted(self):
        tomb = stored.tombstone(200.0)
        record = _audio(**{TRANSCRIPTION_KEY: stored.tombstone(200.0)})
        assert stored.set_where_newer([record], _ID, tomb) == (1, 1)

    def test_none_found(self):
        assert stored.set_where_newer([_audio("OUTRO"), None], _ID, stored.tombstone(1.0)) == (0, 0)


class _RevokeStub:
    def __init__(self):
        self.db = _RecordingDb()
        self._msg_bg_executor = _Inline()

    def _schedule_set_chats(self):
        pass

    def _revoke_stored_copies(self, remote_jid, msg_id, message):
        # The other stored copies are tests/test_remote_revoke.py's.
        pass

    _apply_remote_revoke = MainWindow._apply_remote_revoke


def _revoke(existing):
    """A "delete for everyone" of *existing*, applied in place by the real
    _apply_remote_revoke(). True when it was applied."""
    revoke = {"key": dict(existing["key"]), "messageType": "protocolMessage",
              "message": {"protocolMessage": {"type": 3}}}
    return _RevokeStub()._apply_remote_revoke(existing, revoke, _JID)


class TestTheWriteQueue:
    """Saving and deleting go through one queue of one thread: the order they
    reach the database in is the order they were decided in."""

    def test_it_is_one_thread(self):
        source = inspect.getsource(MainWindow.__init__)
        assert re.search(
            r"self\._transcription_write_queue = ThreadPoolExecutor\(\s*max_workers=1\b", source
        ), "MainWindow.__init__ must build the transcription queue with one worker"

    def test_a_save_held_up_by_the_database_still_lands_before_the_delete(self, inline_call_after):
        """The save waits on a database busy with a sync; the user reads the
        result and deletes it meanwhile. On four threads the delete
        overtook the save."""
        window = _Window([_audio()])
        window._transcription_write_queue = ThreadPoolExecutor(max_workers=1)
        release = threading.Event()
        real_set = window.db.set_message_transcription

        def _busy_set(jids, msg_id, value):
            release.wait(5)
            return real_set(jids, msg_id, value)

        window.db.set_message_transcription = _busy_set
        answered = threading.Event()
        try:
            window.store_message_transcription(_JID, _ID, _value())
            window.delete_message_transcription(_JID, _ID, lambda ok: answered.set())
            release.set()
            assert answered.wait(5)
        finally:
            release.set()
            window._transcription_write_queue.shutdown(wait=True)
        assert [call[0] for call in window.db.calls] == ["set", "delete"]

    def test_a_failed_write_does_not_stop_it(self, inline_call_after):
        """The store's own except answers the failure and the one thread
        goes on to the delete queued behind it: the failure is the
        database's, raised inside _bg_persist(), not one planted in the
        queue — that would only test the standard library."""
        window = _Window([_audio()])
        window._transcription_write_queue = ThreadPoolExecutor(max_workers=1)
        answers = []
        try:
            window.db.fail = True
            window.store_message_transcription(_JID, _ID, _value())
            window._transcription_write_queue.submit(lambda: setattr(window.db, "fail", False))
            window.delete_message_transcription(_JID, _ID, answers.append)
        finally:
            window._transcription_write_queue.shutdown(wait=True)
        assert window.spoken == ["transcription_store_failed"]
        assert answers == [True]


class TestOtherPathsThatHoldTheRecord:
    def test_a_message_deleted_for_everyone_loses_its_transcription(self):
        existing = _audio(**{TRANSCRIPTION_KEY: _value()})
        assert _revoke(existing) is True
        assert TRANSCRIPTION_KEY not in existing

    def test_the_media_request_to_wppconnect_never_carries_it(self, monkeypatch):
        """get_base64_from_media() posts the record itself as the body."""
        sent = []

        def _fake_post(url, json=None, **kwargs):
            sent.append(json)
            raise main_module.MediaExpiredError()

        # get_base64_from_media() looks api_post up in the module its mixin
        # lives in, not in main: patch every module MainWindow is built from.
        patch_main_global(monkeypatch, "api_post", _fake_post)

        class _Stub:
            _phone_to_lid = {}
            wpp_server = "http://127.0.0.1"
            wpp_port = 6300
            token = "t"
            _normalize_jid = staticmethod(MainWindow._normalize_jid)
            _serialize_msg_id = MainWindow._serialize_msg_id
            get_base64_from_media = MainWindow.get_base64_from_media

        record = _audio(**{TRANSCRIPTION_KEY: _value()})
        with pytest.raises(main_module.MediaExpiredError):
            _Stub().get_base64_from_media(record)
        assert sent and TRANSCRIPTION_KEY not in sent[0]
        assert "cofre" not in repr(sent[0])
        assert TRANSCRIPTION_KEY in record, "the caller's record itself is untouched"

    def test_pruning_leaves_it_alone(self):
        from core.utils import prune_message_record

        record = _audio(**{TRANSCRIPTION_KEY: _value()})
        prune_message_record(record)
        assert _text_of(record) == _SECRET


# =============================================================================
#  Privacy — the log
# =============================================================================

_NEW_CODE = (
    MainWindow._transcription_copies,
    MainWindow._transcription_storage_jids,
    MainWindow._say_transcription_not_stored,
    MainWindow.store_message_transcription,
    MainWindow.delete_message_transcription,
    ConversationsPanel._load_conversation_page_from_db,
    DatabaseManager._with_known_local_fields,
    DatabaseManager._needs_stored_row,
    DatabaseManager._apply_known_local_fields,
    DatabaseManager._carries_local_fields,
    DatabaseManager._stored_message,
    DatabaseManager._stored_rows_by_id,
    DatabaseManager._apply_stored_row,
    DatabaseManager.set_message_transcription,
    DatabaseManager.delete_message_transcription,
    DatabaseManager._rewrite_transcription,
    DatabaseManager._scrub_transcription_from_preview,
)


def test_the_new_code_logs_nothing_private():
    # The whole of main_window/transcription_store.py is this feature's code,
    # so the file is scanned as well as its methods: one added there later is
    # held to the rule without being listed in _NEW_CODE.
    offenders = _scan(stored.__file__) + _scan(transcription_store.__file__)
    for function in _NEW_CODE:
        offenders += _scan_source(inspect.getsource(function), function.__qualname__)
    assert offenders == [], offenders


def test_the_scan_would_see_an_id_in_a_log_line():
    """The scan above passing proves nothing unless it can fail."""
    bad = 'def f(message_id):\n    log.info("x %s", message_id)\n'
    assert _scan_source(bad, "probe")


async def test_the_video_duration_line_still_names_its_message(in_memory_db, caplog):
    """_with_known_video_duration() is not in _NEW_CODE on purpose: its line is
    about a video's duration, never its transcription (whose text, contact and
    id are logged nowhere), and the id is what ties it to
    _apply_probed_video_duration()'s own line when a video's length "goes back
    to 0 seconds" (488571e7)."""
    caplog.set_level(logging.INFO)
    video = {"key": {"id": "V1", "remoteJid": _JID, "fromMe": False},
             "messageType": "videoMessage", "messageTimestamp": 1,
             "message": {"videoMessage": {"seconds": 0}}}
    measured = {**video, "message": {"videoMessage": {"seconds": 0, MEASURED_SECONDS_KEY: 42}}}
    await in_memory_db.insert_message(_JID, measured)
    await in_memory_db.insert_message(_JID, video)
    [line] = [r.getMessage() for r in caplog.records if "keeping the measured" in r.getMessage()]
    assert "V1" in line and "42" in line


def test_the_sync_count_line_names_no_chat():
    source = inspect.getsource(MainWindow.sync_chat_messages)
    [line] = [ln for ln in source.splitlines() if "saved transcription(s)" in ln]
    assert "remote_jid" not in line


async def test_nothing_private_reaches_the_log_at_run_time(in_memory_db, caplog, inline_call_after):
    # INFO, the level setup_logging() gives the app: aiosqlite's own DEBUG
    # lines quote every SQL parameter and never reach log.log.
    caplog.set_level(logging.INFO)
    await in_memory_db.insert_message(_JID, _audio())
    await in_memory_db.set_message_transcription(_JID, _ID, _value())
    await in_memory_db.set_message_transcription(_JID, "NAO-EXISTE", _value())
    await in_memory_db.delete_message_transcription(_JID, _ID, 500.0)
    # The two refusals log too: a decision older than the row's, and a row
    # deleted for everyone.
    await in_memory_db.set_message_transcription([_JID, _LID], _ID, _value(at=1.0))
    await in_memory_db.insert_message(_JID, _audio(msg_type="protocolMessage"))
    await in_memory_db.set_message_transcription(_JID, _ID, _value(at=900.0))
    await in_memory_db.insert_messages_batch(_JID, [_audio(**{TRANSCRIPTION_KEY: _value(at=1.0)})])

    window = _Window([_audio(), _audio("P", _local_pending=True, _local_id="P")])
    window.store_message_transcription(_JID, _ID, _value())
    window.store_message_transcription(_JID, "P", _value())
    window.db.fail = True
    window.store_message_transcription(_JID, _ID, _value())
    window.delete_message_transcription(_JID, _ID, lambda ok: None)

    for record in caplog.records:
        text = record.getMessage()
        for private in ("cofre", _ID, "5511988887777", "NAO-EXISTE"):
            assert private not in text, text


async def test_the_merge_logs_neither_the_text_nor_the_id(in_memory_db, caplog):
    """merge_or_rename_chat() already names the two chats (its own line,
    from before); what it now carries over is counted, never named."""
    caplog.set_level(logging.INFO)
    await in_memory_db.insert_message(_JID, _audio())
    await in_memory_db.insert_message(_LID, _audio(jid=_LID, **{TRANSCRIPTION_KEY: _value(at=900.0)}))
    await in_memory_db.merge_or_rename_chat(_LID, _JID)
    lines = [r.getMessage() for r in caplog.records]
    assert any("kept local fields of 1 duplicate(s)" in line for line in lines), lines
    for line in lines:
        assert "cofre" not in line and _ID not in line, line


class TestClearingAModelFromMainWindow:
    @staticmethod
    def _two_models():
        return _audio(**{TRANSCRIPTION_KEY: stored.with_result(
            {"text": "a", "model_id": "base", "backend": "faster_whisper", "at": 10.0},
            {"text": "b", "model_id": "small", "backend": "faster_whisper", "at": 20.0},
        )})

    def test_memory_is_cleared_after_the_database_answers(self, inline_call_after):
        record = self._two_models()
        window = _Window([record])
        window.db.cleared = 1
        answers = []

        window.clear_model_transcriptions("small", "faster_whisper", answers.append)

        assert window.db.calls == [("clear", "small", "faster_whisper")]
        assert answers == [1]
        assert record[TRANSCRIPTION_KEY]["model_id"] == "base"

    def test_a_database_failure_leaves_memory_alone_and_says_so(self, inline_call_after):
        record = self._two_models()
        window = _Window([record])
        window.db.fail = True
        answers = []

        window.clear_model_transcriptions("small", None, answers.append)

        assert answers == [None]
        assert record[TRANSCRIPTION_KEY]["model_id"] == "small"

    def test_every_copy_of_every_chat_is_cleared(self, inline_call_after):
        in_chat, panel_copy = self._two_models(), self._two_models()
        other = self._two_models()
        window = _Window([in_chat])
        window.chats["outro@s.whatsapp.net"] = {
            "remoteJid": "outro@s.whatsapp.net",
            "messages": {"messages": {"records": [other]}},
        }
        window.conversations_panel = _Panel(
            {"remoteJid": _JID, "messages": {"messages": {"records": [panel_copy]}}})

        window.clear_model_transcriptions("small", None, lambda count: None)

        for copy in (in_chat, panel_copy, other):
            assert copy[TRANSCRIPTION_KEY]["model_id"] == "base"


    def test_clearing_everything_leaves_a_tombstone_on_every_copy(self, inline_call_after):
        in_chat, panel_copy = self._two_models(), self._two_models()
        window = _Window([in_chat])
        window.db.cleared = 4
        window.conversations_panel = _Panel(
            {"remoteJid": _JID, "messages": {"messages": {"records": [panel_copy]}}})
        answers = []

        window.clear_all_transcriptions(answers.append)

        assert window.db.calls == [("clear_all",)]
        assert answers == [4]
        for copy in (in_chat, panel_copy):
            assert copy[TRANSCRIPTION_KEY].get("deleted") is True

    def test_clearing_everything_fails_cleanly(self, inline_call_after):
        record = self._two_models()
        window = _Window([record])
        window.db.fail = True
        answers = []
        window.clear_all_transcriptions(answers.append)
        assert answers == [None]
        assert record[TRANSCRIPTION_KEY]["model_id"] == "small"


class TestTheCacheBeingOff:
    @staticmethod
    def _value(model="base"):
        return {"text": _SECRET, "model_id": model, "backend": "faster_whisper",
                "language": "pt", "vad_used": True, "at": 10.0}

    def test_a_result_of_a_model_with_the_cache_off_is_not_kept(self):
        record = _audio()
        window = _Window([record])
        window.settings = {"transcription": {"cache_by_model": {"base": False}}}

        answer = window.store_message_transcription(_JID, _ID, self._value("base"))

        assert answer == stored.SAVE_DISABLED
        assert TRANSCRIPTION_KEY not in record
        assert window.db.calls == [] and window.saves == []

    def test_another_model_is_still_kept(self):
        record = _audio()
        window = _Window([record])
        window.settings = {"transcription": {"cache_by_model": {"base": False}}}
        assert window.store_message_transcription(_JID, _ID, self._value("small")) == stored.SAVE_STORED
        assert _text_of(record) == _SECRET

    def test_the_general_switch_off_keeps_nothing_unless_a_model_says_so(self):
        record = _audio()
        window = _Window([record])
        window.settings = {"transcription": {"cache_enabled": False,
                                             "cache_by_model": {"small": True}}}
        assert window.store_message_transcription(_JID, _ID, self._value("base")) == stored.SAVE_DISABLED
        assert window.store_message_transcription(_JID, _ID, self._value("small")) == stored.SAVE_STORED

    def test_a_withdrawn_message_is_still_a_withdrawal_not_a_cache_note(self):
        record = _audio(messageType="protocolMessage")
        window = _Window([record])
        window.settings = {"transcription": {"cache_enabled": False}}
        assert window.store_message_transcription(_JID, _ID, self._value()) == stored.SAVE_WITHDRAWN
