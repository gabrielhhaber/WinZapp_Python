"""The search over every chat's stored text (core/history_search.py, its page
reader core/history_search_store.py and the rows of ui/dialogs/history_search.py).

Nothing here opens a window. The scan runs on fake pages, so batches,
cancellation, the limit and the locked chats are checked without a database;
the page reader runs against the real encrypted in-memory database.
"""
import pathlib
import types

from core import history_search as hs
from core.history_search_store import message_rows_page
from ui.dialogs import history_search as dialog

CLIENT = pathlib.Path(__file__).resolve().parents[1] / "client"
A, B, LOCKED = "a@s.whatsapp.net", "b@g.us", "secret@s.whatsapp.net"


def msg(mid, text="", ts=100, from_me=False, **extra):
    return {"key": {"id": mid, "fromMe": from_me}, "messageTimestamp": ts,
            "messageType": "conversation", "message": {"conversation": text}, **extra}


def media(kind, mid, **fields):
    return {"key": {"id": mid, "fromMe": False}, "messageTimestamp": 1, "messageType": kind,
            "message": {kind: fields}}


class TestWhatItIsFoundBy:
    def test_plain_text_and_extended_text(self):
        assert hs.message_texts(msg("1", "bom dia")) == ["bom dia"]
        ext = {"message": {"extendedTextMessage": {"text": "veja", "title": "Titulo"}}}
        assert hs.message_texts(ext) == ["veja", "Titulo"]

    def test_captions_and_file_names(self):
        doc = media("documentMessage", "d", caption="contrato", fileName="contrato.pdf")
        assert hs.message_texts(doc) == ["contrato", "contrato.pdf"]

    def test_a_poll_question_and_options(self):
        poll = media("pollCreationMessage", "p", name="Almoço?", options=[
            {"optionName": "Pizza"}, {"optionName": "Sushi"}])
        assert hs.message_texts(poll) == ["Almoço?", "Pizza", "Sushi"]

    def test_contact_and_place(self):
        assert hs.message_texts(media("contactMessage", "c", displayName="Ana")) == ["Ana"]
        assert hs.message_texts(media("locationMessage", "l", name="Praça", address="Rua 1")) == [
            "Praça", "Rua 1"]

    def test_the_saved_transcription_and_the_ai_description(self):
        audio = media("audioMessage", "a")
        audio["_transcription"] = {
            "text": "reunião às seis", "at": 5.0, "model_id": "m", "backend": "b"}
        assert hs.message_texts(audio) == ["reunião às seis"]
        video = media("videoMessage", "v")
        video["_transcription"] = {"text": "uma praia", "at": 5.0, "kind": "description"}
        assert hs.message_texts(video) == ["uma praia"]

    def test_a_deleted_transcription_and_junk_are_nothing(self):
        audio = media("audioMessage", "a")
        audio["_transcription"] = {"deleted": True, "at": 1.0}
        assert hs.message_texts(audio) == []
        assert hs.message_texts(None) == []
        assert hs.message_texts({"message": "not json"}) == []

    def test_a_body_stored_as_a_json_string(self):
        assert hs.message_texts({"message": '{"conversation": "oi"}'}) == ["oi"]


class TestMatching:
    def test_every_word_in_any_order_accents_and_case_folded(self):
        words = hs.query_words("REUNIAO seis")
        assert hs.find_in_message(msg("1", "A reunião é às seis"), words, "nfd") is not None

    def test_a_missing_word_does_not_match(self):
        words = hs.query_words("reuniao sete")
        assert hs.find_in_message(msg("1", "A reunião é às seis"), words, "nfd") is None

    def test_off_mode_keeps_accents_exact(self):
        words = hs.query_words("reuniao", "off")
        assert hs.find_in_message(msg("1", "reunião"), words, "off") is None

    def test_an_empty_query_matches_nothing(self):
        assert hs.find_in_message(msg("1", "oi"), [], "nfd") is None

    def test_a_name_completes_the_query_but_never_is_the_whole_of_it(self):
        words = hs.query_words("maria bom")
        assert hs.find_in_message(msg("1", "bom dia"), words, "nfd", chat_name="Maria") == "bom dia"
        only_name = hs.query_words("maria")
        assert hs.find_in_message(msg("1", "bom dia"), only_name, "nfd", chat_name="Maria") is None

    def test_the_senders_push_name_and_my_own_label(self):
        words = hs.query_words("ana oi")
        assert hs.find_in_message(msg("1", "oi", pushName="Ana"), words, "nfd") is not None
        mine = msg("2", "oi", from_me=True)
        assert hs.find_in_message(mine, hs.query_words("eu oi"), "nfd", self_label="Eu") is not None
        assert hs.find_in_message(mine, hs.query_words("ana oi"), "nfd", self_label="Eu") is None

    def test_a_short_text_is_the_whole_snippet(self):
        assert hs.find_in_message(msg("1", "a  b\nc oi"), ["oi"], "nfd") == "a b c oi"

    def test_a_long_text_is_cut_around_the_match(self):
        text = "x" * 400 + " reunião " + "y" * 400
        snippet = hs.find_in_message(msg("1", text), hs.query_words("reuniao"), "nfd")
        assert "reunião" in snippet and len(snippet) < 200
        assert snippet.startswith("...") and snippet.endswith("...")

    def test_the_piece_with_the_most_words_is_the_snippet(self):
        poll = media("pollCreationMessage", "p", name="Almoço", options=[{"optionName": "pizza grande"}])
        assert hs.find_in_message(poll, hs.query_words("pizza grande"), "nfd") == "pizza grande"


class FakeHistory:
    """Pages of (rowid, jid, token) newest first; the token is the message itself."""

    def __init__(self, rows):
        self.rows = sorted(rows, key=lambda r: -r[0])
        self.reads, self.decrypted = [], []

    def read_page(self, before, size):
        self.reads.append(before)
        return [r for r in self.rows if before is None or r[0] < before][:size]

    def decrypt(self, token):
        self.decrypted.append(token["key"]["id"])
        return token


def history(count, jid=A, text="oi {n}"):
    return FakeHistory([(n, jid, msg(f"m{n}", text.format(n=n))) for n in range(1, count + 1)])


def scan(fake, query="oi", **kw):
    hits = []
    kw.setdefault("is_hidden", lambda jid: False)
    result = hs.scan_history(fake.read_page, fake.decrypt, hs.query_words(query),
                             on_hits=hits.extend, **kw)
    return result, hits


class TestScan:
    def test_it_reads_in_batches_and_reports_every_page_with_hits(self):
        fake = history(10)
        calls = []
        result = hs.scan_history(fake.read_page, fake.decrypt, ["oi"], is_hidden=lambda j: False,
                                 batch_size=4, on_hits=calls.append)
        assert result == hs.ScanResult(hs.DONE, 10)
        assert [len(c) for c in calls] == [4, 4, 2]
        assert fake.reads == [None, 7, 3, 1]            # keyset by rowid, one page at a time

    def test_results_come_newest_row_first(self):
        _result, hits = scan(history(5), batch_size=2)
        assert [h.message["key"]["id"] for h in hits] == ["m5", "m4", "m3", "m2", "m1"]

    def test_only_matching_messages_are_hits(self):
        fake = FakeHistory([(1, A, msg("a", "gato")), (2, A, msg("b", "cachorro"))])
        _result, hits = scan(fake, "gato")
        assert [h.message["key"]["id"] for h in hits] == ["a"]

    def test_the_limit_stops_the_scan_mid_page(self):
        fake = history(50)
        result, hits = scan(fake, limit=7, batch_size=5)
        assert result == hs.ScanResult(hs.LIMIT_REACHED, 7)
        assert len(hits) == 7
        assert len(fake.reads) == 2          # no third page read

    def test_cancelling_before_the_first_page_reads_nothing(self):
        fake = history(5)
        result, hits = scan(fake, cancelled=lambda: True)
        assert result == hs.ScanResult(hs.CANCELLED, 0) and hits == [] and fake.reads == []

    def test_cancelling_between_messages_stops_inside_a_page(self):
        fake = history(20)
        state = {"left": 3}

        def cancelled():
            state["left"] -= 1
            return state["left"] < 0

        result, _hits = scan(fake, cancelled=cancelled, batch_size=20)
        assert result.status == hs.CANCELLED
        assert len(fake.decrypted) < 20

    def test_the_same_message_under_two_addresses_is_one_hit(self):
        fake = FakeHistory([(1, "a@c.us", msg("m", "oi")), (2, A, msg("m", "oi"))])
        _result, hits = scan(fake)
        assert len(hits) == 1

    def test_unreadable_rows_are_skipped(self):
        fake = FakeHistory([(1, A, msg("a", "oi")), (2, A, msg("b", "oi"))])
        fake.decrypt = lambda token: None if token["key"]["id"] == "b" else token
        _result, hits = scan(fake)
        assert [h.message["key"]["id"] for h in hits] == ["a"]

    def test_a_scan_of_an_empty_history_is_done(self):
        assert scan(FakeHistory([]))[0] == hs.ScanResult(hs.DONE, 0)

    def test_the_names_of_chats_complete_a_query(self):
        fake = FakeHistory([(1, A, msg("a", "bom dia")), (2, B, msg("b", "bom dia"))])
        _result, hits = scan(fake, "maria bom", chat_name=lambda jid: "Maria" if jid == A else "Rua")
        assert [h.jid for h in hits] == [A]


class TestLockedChats:
    def fake(self):
        return FakeHistory([(1, A, msg("a", "segredo aberto")),
                            (2, LOCKED, msg("l", "segredo trancado")),
                            (3, A, msg("c", "segredo de novo"))])

    def test_a_hidden_chat_is_never_decrypted_nor_reported(self):
        fake = self.fake()
        result, hits = scan(fake, "segredo", is_hidden=lambda jid: jid == LOCKED)
        assert [h.jid for h in hits] == [A, A]
        assert "l" not in fake.decrypted
        assert result == hs.ScanResult(hs.DONE, 2)      # the count is of what is shown

    def test_with_the_vault_open_the_chat_is_searched_like_any_other(self):
        _result, hits = scan(self.fake(), "segredo")
        assert LOCKED in {h.jid for h in hits}

    def test_the_answer_is_asked_once_per_chat(self):
        asked = []
        scan(history(30), is_hidden=lambda jid: asked.append(jid) or False)
        assert asked == [A]

    def test_a_search_of_only_hidden_chats_looks_like_an_empty_history(self):
        fake = FakeHistory([(1, LOCKED, msg("l", "segredo"))])
        result, hits = scan(fake, "segredo", is_hidden=lambda jid: True)
        assert result == hs.ScanResult(hs.DONE, 0) and hits == []


class TestPageReader:
    async def test_pages_are_cut_by_rowid_newest_first_and_stay_encrypted(self, in_memory_db):
        db = in_memory_db
        for n in range(1, 6):
            await db.insert_message(A, msg(f"m{n}", f"texto {n}", ts=n))
        first = await db.get_message_rows_page(None, 2)
        assert len(first) == 2 and first[0][0] > first[1][0]
        assert "texto" not in first[0][2]                       # message_json is encrypted
        assert db._decrypt_json(first[0][2])["message"]["conversation"] == "texto 5"
        second = await db.get_message_rows_page(first[-1][0], 10)
        assert len(second) == 3 and second[0][0] < first[-1][0]
        assert await db.get_message_rows_page(second[-1][0], 10) == []

    async def test_types_with_nothing_to_search_are_left_out(self, in_memory_db):
        db = in_memory_db
        await db.insert_message(A, msg("keep", "oi"))
        for kind in hs.SKIPPED_TYPES:
            await db.insert_message(A, {**msg(f"x-{kind}", "oi"), "messageType": kind})
        rows = await message_rows_page(db, None, 50)
        assert [db._decrypt_json(r[2])["key"]["id"] for r in rows] == ["keep"]

    async def test_the_whole_chain_finds_a_saved_transcription(self, in_memory_db):
        db = in_memory_db
        audio = media("audioMessage", "voz")
        audio["_transcription"] = {"text": "levar o guarda-chuva", "at": 5.0,
                                   "model_id": "m", "backend": "b"}
        await db.insert_message(A, audio)
        await db.insert_message(A, msg("outra", "nada a ver"))
        rows = await db.get_message_rows_page(None, 10)
        hits = []
        hs.scan_history(lambda before, size: rows if before is None else [],
                        db._decrypt_json, hs.query_words("guarda-chuva"),
                        is_hidden=lambda jid: False, on_hits=hits.extend)
        assert [h.message["key"]["id"] for h in hits] == ["voz"]


class _I18n:
    def t(self, key):
        return {"history_search_row": "{chat} — {sender} — {date} — {text}",
                "starred_no_text": "(no text)"}[key]


class _Panel:
    def _format_date(self, ts):
        return f"d{ts}"

    def _get_participant_name(self, participant, msg, resolve_missing=True):
        assert resolve_missing is False
        return {"p1@s.whatsapp.net": "Paulo"}.get(participant, "")


def window():
    return types.SimpleNamespace(
        i18n=_I18n(), conversations_panel=_Panel(), self_reference_label=lambda: "Eu",
        chat_display_name=lambda jid: {A: "Maria", B: "Grupo"}.get(jid, "Sem nome"))


class TestRows:
    def test_the_row_reads_chat_sender_date_and_excerpt_by_name(self):
        hit = hs.Hit(A, msg("1", "oi", pushName="Maria"), 77, "oi tudo bem")
        assert dialog.row_text(window(), hit) == "Maria — Maria — d77 — oi tudo bem"

    def test_my_own_message_is_by_my_label(self):
        hit = hs.Hit(A, msg("1", "oi", from_me=True), 5, "oi")
        assert dialog.row_text(window(), hit).startswith("Maria — Eu — ")

    def test_a_group_member_is_by_name_never_by_jid(self):
        message = msg("1", "oi")
        message["key"]["participant"] = "p1@s.whatsapp.net"
        row = dialog.row_text(window(), hs.Hit(B, message, 5, "oi"))
        assert row == "Grupo — Paulo — d5 — oi"
        assert "@" not in row

    def test_an_unknown_member_falls_back_to_the_push_name_then_the_chat(self):
        message = msg("1", "oi", pushName="Zeca")
        message["key"]["participant"] = "p9@s.whatsapp.net"
        assert "— Zeca —" in dialog.row_text(window(), hs.Hit(B, message, 5, "oi"))
        message.pop("pushName")
        assert "Grupo — Grupo —" in dialog.row_text(window(), hs.Hit(B, message, 5, "oi"))

    def test_a_row_without_text_says_so(self):
        assert dialog.row_text(window(), hs.Hit(A, msg("1"), 5, "")).endswith("(no text)")

    def test_the_names_snapshot_leaves_out_hidden_chats(self):
        mw = types.SimpleNamespace(
            chats={A: {"remoteJid": A}, LOCKED: {"remoteJid": LOCKED}},
            chat_display_name=lambda chat: chat["remoteJid"].split("@")[0],
            is_chat_hidden_by_vault=lambda jid: jid == LOCKED)
        assert dialog.chat_names_snapshot(mw) == {A: "a"}

    def test_run_scan_reads_the_accounts_database_and_honours_the_vault(self):
        fake = FakeHistory([(1, A, msg("a", "segredo")), (2, LOCKED, msg("l", "segredo"))])
        db = types.SimpleNamespace(get_message_rows_page=fake.read_page,
                                   decrypt_message_json=fake.decrypt)
        mw = types.SimpleNamespace(db=db, is_chat_hidden_by_vault=lambda jid: jid == LOCKED)
        hits = []
        result = dialog.run_scan(mw, ["segredo"], "nfd", {}, "Eu", lambda: False, hits.extend)
        assert result == hs.ScanResult(hs.DONE, 1)
        assert [h.jid for h in hits] == [A]
        assert fake.decrypted == ["a"]


class TestWiring:
    def test_the_shortcut_is_in_the_catalogue_and_on_the_file_menu(self):
        from core.shortcut_catalog import SHORTCUTS
        from ui.shortcut_bindings import MENU_COMMANDS
        [row] = [s for s in SHORTCUTS if s.id == "main.history_search"]
        assert (row.scope, row.mod, row.key, row.label) == ("main", 6, ord("G"), "menu_history_search")
        assert MENU_COMMANDS["_ID_HISTORY_SEARCH"] == "main.history_search"
        chrome = (CLIENT / "main_window/window_chrome.py").read_text(encoding="utf-8")
        assert "self._ID_HISTORY_SEARCH," in chrome and "def _on_menu_history_search" in chrome

    def test_the_f1_list_describes_it(self):
        source = (CLIENT / "ui/dialogs/shortcuts_dialog.py").read_text(encoding="utf-8")
        assert 'i18n.t("shortcut_history_search_label")' in source

    def test_no_log_line_carries_the_query_or_a_result(self):
        for path in ("core/history_search.py", "core/history_search_store.py",
                     "ui/dialogs/history_search.py"):
            for line in (CLIENT / path).read_text(encoding="utf-8").splitlines():
                if "logging." in line or "log." in line:
                    assert "words" not in line and "query" not in line and "hit" not in line
