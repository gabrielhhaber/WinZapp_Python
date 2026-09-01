"""Tests for keeping user-loaded history alive across a message-list rebuild.

Reported as "a conversa some com as mensagens antigas": with
``user_interface.messages_page_size`` = 200 the user pressed Home to pull older
messages in, and a few seconds after the next message arrived the list snapped
back to exactly 200 rows. Two independent defects added up to that:

1. ``_load_older_messages()`` prepended what it read from the local DB only to
   the panel's in-memory lists, never into
   ``conversation["messages"]["messages"]["records"]`` — which is the only
   thing ``populate_messages()`` rebuilds from. The server path already merged
   (``MainWindow.fetch_older_messages()``), the local one did not, so that
   history existed nowhere a rebuild could find it.

2. ``populate_messages()`` recomputed the pagination window from the end of the
   list on every rebuild, so even history that *was* in ``records`` got cut back
   to the configured limit. ``_remember_expanded_window()`` now records how far
   the list was expanded (count + the oldest displayed message as the anchor)
   and ``history_window()`` turns that into the window the rebuild uses.

The rebuild became frequent (every new message, plus the 60s resync) when
``_refresh_open_conversation_after_sync()`` landed, which is why this only
started showing up recently — the destructive rebuild is the bug, not its
trigger.

3. The anchor was recorded only where history was pulled in by hand, so a
   conversation the user never expanded kept the old behaviour with the floor
   at the page size: it opens with 200 rows, the live-append paths grow it past
   that (nothing trims on append), and the first background rebuild snapped it
   back to 200 — one old row deleted under the reader per new message. That is
   how the report came back after (1) and (2) shipped. ``populate_messages()``
   now records the window it just painted, so the floor is what is on screen,
   not what Home loaded.

Two things the fix must NOT do are pinned here as well: the window must not be
capped, because a standing cap is this same bug with a higher floor (see
``test_a_huge_expansion_is_not_capped``); and only messages belonging to the
open conversation may reach ``records``, since the history query runs against
``_history_storage_jid()`` (possibly a stale @lid) and anything stored under
the wrong contact now survives the whole session.

ConversationsPanel is a wx.Panel and cannot be instantiated without a running
wx.App, so the methods under test are exercised as plain functions against a
small stub carrying just the attributes they touch — same approach as
tests/test_message_bookmarks.py.
"""

import inspect

import pytest

from core.utils import expanded_min_visible, history_window, paginated_window
from ui.conversations import ConversationsPanel

JID = "1234567890@s.whatsapp.net"
OTHER_JID = "1098765432@s.whatsapp.net"


def _msg(mid, ts=0, jid=JID):
    return {
        "key": {"id": mid, "fromMe": False, "remoteJid": jid},
        "timestamp": ts,
        "messageTimestamp": ts,
        "messageType": "conversation",
    }


def _chat(jid=JID, records=None):
    return {
        "remoteJid": jid,
        "messages": {"messages": {"records": list(records or []),
                                  "total": len(records or [])}},
    }


class _FakeList:
    """Just enough wx.ListCtrl for the prepend paths (Freeze/Thaw + rebuild)."""

    def __init__(self):
        self.rows = []

    def Freeze(self):
        pass

    def Thaw(self):
        pass

    def DeleteAllItems(self):
        self.rows = []

    def Append(self, row):
        self.rows.append(row)

    def Focus(self, idx):
        pass

    def Select(self, idx, on=True):
        pass

    def EnsureVisible(self, idx):
        pass


class _FakeDB:
    def __init__(self, pages):
        # pages: list of message lists, newest-first as the real DB returns them
        self.pages = list(pages)
        self.calls = []

    def get_messages(self, jid, limit=200, offset=0):
        self.calls.append((jid, limit, offset))
        return self.pages.pop(0) if self.pages else []


class _FakeMainWindow:
    """Stands in for MainWindow: settings, the DB and the JID equivalence rule.

    _chat_jids_equivalent() is reimplemented rather than bound, because the real
    one pulls in _normalize_jid()/_jid_address_forms() and the whole @lid cache;
    all the merge needs from it is "same conversation or not".
    """

    def __init__(self, db=None, page_size=200, lid_map=None):
        self.db = db
        self.settings = {"user_interface": {"messages_page_size": page_size}}
        self._phone_to_lid = dict(lid_map or {})

    def _chat_jids_equivalent(self, left, right):
        def _forms(jid):
            jid = (jid or "").replace("@c.us", "@s.whatsapp.net")
            forms = {jid} if jid else set()
            mapped = self._phone_to_lid.get(jid)
            if mapped:
                forms.add(mapped)
            for phone, lid in self._phone_to_lid.items():
                if lid == jid:
                    forms.add(phone)
            return forms
        left_forms, right_forms = _forms(left), _forms(right)
        return bool(left_forms and right_forms and (left_forms & right_forms))


class _Stub:
    """Minimal stand-in for ConversationsPanel for the history-load paths."""

    def __init__(self, conversation=None, main_window=None, sorted_messages=None,
                 messages_offset=0, all_sorted_messages=None):
        self.conversation = conversation
        self.main_window = main_window if main_window is not None else _FakeMainWindow()
        self.messages_list = _FakeList()
        self._all_sorted_messages = list(
            all_sorted_messages if all_sorted_messages is not None
            else (sorted_messages or [])
        )
        self._sorted_messages = list(sorted_messages or [])
        self._messages_offset = messages_offset
        self._unread_sep_idx = -1
        self._is_loading_more = False
        self._expanded_visible_count = 0
        self._expanded_oldest_msg_id = ""
        self._server_history_anchor = {}
        self.server_fetch_calls = 0

    # Real implementations under test.
    _merge_history_into_records = ConversationsPanel._merge_history_into_records
    _remember_expanded_window = ConversationsPanel._remember_expanded_window
    _reset_expanded_window = ConversationsPanel._reset_expanded_window
    _history_window_for_rebuild = ConversationsPanel._history_window_for_rebuild
    _load_older_messages = ConversationsPanel._load_older_messages
    _on_older_messages_loaded = ConversationsPanel._on_older_messages_loaded
    _extract_timestamp = ConversationsPanel._extract_timestamp
    _load_more_messages = ConversationsPanel._load_more_messages
    _deduplicate_messages = ConversationsPanel._deduplicate_messages
    _history_storage_jid = ConversationsPanel._history_storage_jid
    _is_separator = ConversationsPanel._is_separator

    # Collaborators the methods call through self, stubbed to the bare minimum.
    def _is_displayable_message(self, msg):
        return not self._is_separator(msg)

    def _recompute_unread_sep_idx(self):
        pass

    def _render_message_line(self, msg):
        return ""

    def _load_older_messages_from_server(self):
        self.server_fetch_calls += 1
        self._is_loading_more = False


class TestMergeHistoryIntoRecords:
    def test_older_messages_are_prepended_and_total_updated(self):
        chat = _chat(records=[_msg("new-1", 20)])
        stub = _Stub(conversation=chat)
        stub._merge_history_into_records([_msg("old-1", 5), _msg("old-2", 10)])
        container = chat["messages"]["messages"]
        assert [r["key"]["id"] for r in container["records"]] == ["old-1", "old-2", "new-1"]
        assert container["total"] == 3

    def test_merge_is_idempotent(self):
        """self.conversation is usually the same dict as main_window.chats[jid],
        and both paths (local DB and server) can merge the same page."""
        chat = _chat(records=[_msg("new-1", 20)])
        stub = _Stub(conversation=chat)
        older = [_msg("old-1", 5)]
        stub._merge_history_into_records(older)
        stub._merge_history_into_records(older)
        container = chat["messages"]["messages"]
        assert [r["key"]["id"] for r in container["records"]] == ["old-1", "new-1"]
        assert container["total"] == 2

    def test_existing_records_are_not_reordered(self):
        chat = _chat(records=[_msg("b", 30), _msg("a", 10)])
        stub = _Stub(conversation=chat)
        stub._merge_history_into_records([_msg("z", 1)])
        assert [r["key"]["id"] for r in chat["messages"]["messages"]["records"]] == [
            "z", "b", "a",
        ]

    def test_missing_containers_are_created(self):
        chat = {"remoteJid": JID}
        stub = _Stub(conversation=chat)
        stub._merge_history_into_records([_msg("old-1", 5)])
        assert chat["messages"]["messages"]["total"] == 1

    def test_a_larger_stored_total_is_not_overwritten(self):
        """`total` is the chat's real message count (db.get_message_count()),
        which is legitimately larger than what is loaded — it only goes up."""
        chat = _chat(records=[_msg("new-1", 20)])
        chat["messages"]["messages"]["total"] = 4000
        stub = _Stub(conversation=chat)
        stub._merge_history_into_records([_msg("old-1", 5)])
        assert chat["messages"]["messages"]["total"] == 4000

    def test_no_conversation_or_no_messages_is_a_noop(self):
        stub = _Stub(conversation=None)
        stub._merge_history_into_records([_msg("old-1", 5)])  # must not raise
        chat = _chat(records=[_msg("new-1", 20)])
        stub2 = _Stub(conversation=chat)
        stub2._merge_history_into_records([])
        assert chat["messages"]["messages"]["total"] == 1


class TestMergeRejectsForeignHistory:
    """A stale/wrong @lid mapping makes the history query answer with someone
    else's messages. They used to vanish on the next rebuild; stored in
    `records` they become that contact's permanent history, because
    sync_chat_messages() picks the local records back up without filtering."""

    def test_messages_from_another_conversation_are_dropped(self):
        chat = _chat(records=[_msg("new-1", 20)])
        stub = _Stub(conversation=chat)
        stub._merge_history_into_records(
            [_msg("old-1", 5), _msg("stranger", 6, jid=OTHER_JID)]
        )
        ids = [r["key"]["id"] for r in chat["messages"]["messages"]["records"]]
        assert ids == ["old-1", "new-1"]

    def test_a_page_entirely_from_another_conversation_changes_nothing(self):
        chat = _chat(records=[_msg("new-1", 20)])
        stub = _Stub(conversation=chat)
        stub._merge_history_into_records([_msg("stranger", 6, jid=OTHER_JID)])
        assert [r["key"]["id"] for r in chat["messages"]["messages"]["records"]] == ["new-1"]

    def test_the_matching_lid_form_is_still_accepted(self):
        """The local history genuinely lives under the @lid for many one-to-one
        chats — that is the mapping working, not a wrong one."""
        lid = "111122223333@lid"
        chat = _chat(records=[_msg("new-1", 20)])
        stub = _Stub(conversation=chat,
                     main_window=_FakeMainWindow(lid_map={JID: lid}))
        stub._merge_history_into_records([_msg("old-1", 5, jid=lid)])
        assert [r["key"]["id"] for r in chat["messages"]["messages"]["records"]] == [
            "old-1", "new-1",
        ]


class TestLoadOlderMessagesPersistsHistory:
    def _stub_with_local_history(self, page_size=2):
        resident = [_msg("new-1", 100), _msg("new-2", 110)]
        chat = _chat(records=list(resident))
        # The DB returns newest-first; _load_older_messages() reverses it.
        db = _FakeDB([[_msg("old-2", 20), _msg("old-1", 10)]])
        stub = _Stub(
            conversation=chat,
            main_window=_FakeMainWindow(db=db, page_size=page_size),
            sorted_messages=resident,
        )
        return stub, chat, db

    def test_locally_loaded_history_reaches_the_conversation_records(self):
        """Without this the prepend lived only in _sorted_messages, so the next
        populate_messages() rebuilt the conversation without it."""
        stub, chat, _db = self._stub_with_local_history()
        stub._load_older_messages()
        assert stub.server_fetch_calls == 0
        assert [r["key"]["id"] for r in chat["messages"]["messages"]["records"]] == [
            "old-1", "old-2", "new-1", "new-2",
        ]
        assert chat["messages"]["messages"]["total"] == 4

    def test_the_expanded_window_is_recorded(self):
        stub, _chat_dict, _db = self._stub_with_local_history()
        stub._load_older_messages()
        assert stub._expanded_visible_count == len(stub._sorted_messages) == 4
        assert stub._expanded_oldest_msg_id == "old-1"

    def test_a_duplicate_only_page_falls_through_to_the_server(self):
        """No new unique messages means nothing to protect — the expanded count
        must stay put rather than pinning a window that was never widened."""
        resident = [_msg("new-1", 100)]
        chat = _chat(records=list(resident))
        db = _FakeDB([[_msg("new-1", 100)]])
        stub = _Stub(
            conversation=chat,
            main_window=_FakeMainWindow(db=db),
            sorted_messages=resident,
        )
        stub._load_older_messages()
        assert stub.server_fetch_calls == 1
        assert stub._expanded_visible_count == 0
        assert stub._expanded_oldest_msg_id == ""


class TestLoadMoreMessages:
    """Paging back through history already in memory expands the window too —
    without recording it, the next rebuild undid the page the user just asked
    for, exactly the same way."""

    def _stub(self):
        history = [_msg(f"m-{i}", i) for i in range(10)]
        return _Stub(
            conversation=_chat(records=history),
            main_window=_FakeMainWindow(page_size=4),
            sorted_messages=history[6:],
            all_sorted_messages=history,
            messages_offset=6,
        )

    def test_paging_back_records_the_new_window(self):
        stub = self._stub()
        stub._load_more_messages()
        assert stub._messages_offset == 2
        assert stub._expanded_visible_count == len(stub._sorted_messages) == 8
        assert stub._expanded_oldest_msg_id == "m-2"

    def test_nothing_left_to_page_leaves_the_window_alone(self):
        stub = self._stub()
        stub._messages_offset = 0
        stub._sorted_messages = list(stub._all_sorted_messages)
        stub._load_more_messages()
        assert stub._expanded_visible_count == 0


class TestOlderMessagesFromTheServer:
    """The deep-scrollback path, and the worse of the two: it runs where the
    history is genuinely new and has never been in `records`, so losing it on
    the next rebuild means fetching it from the phone all over again."""

    def _stub(self):
        resident = [_msg("new-1", 100), _msg("new-2", 110)]
        return _Stub(
            conversation=_chat(records=list(resident)),
            sorted_messages=resident,
        )

    def test_fetched_history_reaches_the_conversation_records(self):
        stub = self._stub()
        # fetch_older_messages() answers newest-first, like the REST endpoint.
        stub._on_older_messages_loaded([_msg("old-2", 20), _msg("old-1", 10)], JID)
        records = stub.conversation["messages"]["messages"]["records"]
        assert [r["key"]["id"] for r in records] == ["old-1", "old-2", "new-1", "new-2"]

    def test_the_expanded_window_is_recorded(self):
        stub = self._stub()
        stub._on_older_messages_loaded([_msg("old-2", 20), _msg("old-1", 10)], JID)
        assert stub._expanded_visible_count == len(stub._sorted_messages) == 4
        assert stub._expanded_oldest_msg_id == "old-1"

    def test_a_page_from_another_conversation_never_reaches_the_records(self):
        """fetch_older_messages() has a fallback that re-queries under the
        alternate JID and does not filter what comes back."""
        stub = self._stub()
        stub._on_older_messages_loaded([_msg("stranger", 10, jid=OTHER_JID)], JID)
        records = stub.conversation["messages"]["messages"]["records"]
        assert [r["key"]["id"] for r in records] == ["new-1", "new-2"]

    def test_a_duplicate_only_page_leaves_the_window_alone(self):
        stub = self._stub()
        stub._on_older_messages_loaded([_msg("new-1", 100)], JID)
        assert stub._expanded_visible_count == 0


class TestHistoryWindowForRebuild:
    """What populate_messages() actually calls. Driven as the real method
    against the stub, so replacing the anchor arguments with empty values —
    the silent revert that reintroduces the whole bug — fails here."""

    def test_the_expanded_history_survives_the_rebuild(self):
        displayable = [_msg(f"m-{i}", i) for i in range(400)]
        stub = _Stub(conversation=_chat(records=displayable))
        stub._expanded_visible_count = 400
        stub._expanded_oldest_msg_id = "m-0"
        assert stub._history_window_for_rebuild(displayable, 200) == (0, -1)

    def test_messages_arriving_after_the_expansion_do_not_push_it_out(self):
        displayable = [_msg(f"m-{i}", i) for i in range(410)]
        stub = _Stub(conversation=_chat(records=displayable))
        stub._expanded_visible_count = 400
        stub._expanded_oldest_msg_id = "m-0"
        assert stub._history_window_for_rebuild(displayable, 200)[0] == 0

    def test_a_conversation_that_was_never_expanded_uses_the_page_size(self):
        displayable = [_msg(f"m-{i}", i) for i in range(500)]
        stub = _Stub(conversation=_chat(records=displayable))
        assert stub._history_window_for_rebuild(displayable, 200)[0] == 300

    def test_the_unread_separator_is_rebased_onto_the_window(self):
        displayable = [_msg(f"m-{i}", i) for i in range(500)]
        stub = _Stub(conversation=_chat(records=displayable))
        stub._unread_sep_idx = 450
        offset, sep = stub._history_window_for_rebuild(displayable, 200)
        assert (offset, sep) == (300, 150)


class TestExpandedWindowIsScopedToOneConversation:
    def test_reset_clears_both_halves_of_the_anchor(self):
        stub = _Stub(conversation=_chat())
        stub._expanded_visible_count = 900
        stub._expanded_oldest_msg_id = "m-0"
        stub._reset_expanded_window()
        assert stub._expanded_visible_count == 0
        assert stub._expanded_oldest_msg_id == ""

    def test_navigating_to_another_conversation_resets_it(self):
        """navigate_to_conversation() cannot be driven from a stub (it rebuilds
        the whole conversation panel), so this is the one place left where the
        call itself is what gets checked — without it the next conversation
        opens already rendering the previous one's thousands of rows.
        _close_conversation_core() is covered for real in
        tests/test_close_conversation_for_panel_switch.py."""
        src = inspect.getsource(ConversationsPanel.navigate_to_conversation)
        assert "self._reset_expanded_window()" in src


class TestHistoryWindow:
    """The whole decision populate_messages() delegates, in one call.

    Kept as one function on purpose: with the panel state and paginated_window()
    covered only separately, deleting the single line that carried the expanded
    window between them reintroduced the entire bug with the suite still green.
    """

    def test_a_background_rebuild_keeps_the_loaded_history(self):
        # 200 older messages loaded on top of the 200 already shown.
        displayable = [_msg(f"m-{i}", i) for i in range(400)]
        offset, sep = history_window(displayable, "m-0", 400, 200, -1)
        assert (offset, sep) == (0, -1), "the rebuild must not cut back to 200"

    def test_new_messages_do_not_slide_the_window_forward(self):
        """Three messages arrived after the expansion: the list must grow at the
        bottom, not lose its three oldest rows at the top."""
        displayable = [_msg(f"m-{i}", i) for i in range(403)]
        assert history_window(displayable, "m-0", 400, 200, -1)[0] == 0

    def test_a_deleted_anchor_falls_back_to_the_count(self):
        """The oldest displayed message was deleted remotely — the window may
        end up one row narrower, but must not collapse back to the page size."""
        displayable = [_msg(f"m-{i}", i) for i in range(1, 400)]
        assert history_window(displayable, "m-0", 400, 200, -1)[0] == 0

    def test_no_expansion_leaves_the_page_size_alone(self):
        displayable = [_msg(f"m-{i}", i) for i in range(500)]
        assert history_window(displayable, "", 0, 200, -1)[0] == 300

    def test_a_huge_expansion_is_not_capped(self):
        """Twenty Page Ups in a 6000-message group stay on screen. A standing
        cap looked prudent and is the original bug with a higher floor: the
        first message to arrive would cut the window back to it, wiping 2200
        rows out from under the reader, every later rebuild would repeat the
        cut, and the Home that follows would be undone again — history past the
        cap becomes permanently unreachable."""
        displayable = [_msg(f"m-{i}", i) for i in range(6000)]
        # 4200 rows expanded, so the oldest one on screen is m-1800.
        assert history_window(displayable, "m-1800", 4200, 200, -1)[0] == 1800

    def test_the_cap_is_off_by_default_but_still_available(self):
        displayable = [_msg(f"m-{i}", i) for i in range(1000)]
        assert history_window(displayable, "m-0", 1000, 200, -1)[0] == 0
        assert history_window(displayable, "m-0", 1000, 200, -1, cap=500)[0] == 500
        # 0 and anything non-positive mean "no ceiling", not "no widening".
        assert history_window(displayable, "m-0", 1000, 200, -1, cap=0)[0] == 0
        assert history_window(displayable, "m-0", 1000, 200, -1, cap=-5)[0] == 0

    def test_a_cap_never_cuts_into_the_unread_separator(self):
        """A caller passing one bounds the history the user asked for, not the
        separator — cutting above it would drop every genuinely unread message,
        which is what paginated_window()'s own widening exists to prevent."""
        displayable = [_msg(f"m-{i}", i) for i in range(6000)]
        sep_idx = 6000 - 2500
        offset, sep = history_window(displayable, "m-0", 4200, 200, sep_idx, cap=2000)
        assert offset == sep_idx
        assert sep == 0

    def test_a_non_numeric_expanded_count_falls_back_to_the_page_size(self):
        """settings.json is hand-editable and this state is read defensively
        with getattr() — a junk value must not take the whole rebuild down."""
        displayable = [_msg(f"m-{i}", i) for i in range(500)]
        assert history_window(displayable, "", None, 200, -1)[0] == 300
        assert history_window(displayable, "", "abc", 200, -1)[0] == 300

    def test_it_agrees_with_the_pieces_it_composes(self):
        displayable = [_msg(f"m-{i}", i) for i in range(900)]
        min_visible = expanded_min_visible(displayable, "m-100", 300)
        assert history_window(displayable, "m-100", 300, 200, -1) == paginated_window(
            900, 200, -1, min_visible=min_visible
        )


class TestTheRenderedWindowIsTheFloor:
    """A segunda metade do mesmo bug, relatada depois da âncora: sem nunca
    pressionar Home, a lista abre com 200 linhas, cresce por append a cada
    mensagem, e o rebuild seguinte a devolve a 200 — sumindo com uma linha
    antiga por mensagem nova, exatamente o sintoma original com o piso no page
    size. O log da sessão que reproduziu isto mostra a sequência inteira:
    200 -> 201 -> 202 -> 204 linhas e, 17 segundos depois, 200 de novo.

    A correção é o piso deixar de ser "o que o Home trouxe" e passar a ser "o
    que já está na tela": populate_messages() registra a janela que acabou de
    pintar."""

    def _rendered(self, n, offset=0):
        stub = _Stub(sorted_messages=[_msg(f"m-{i}", i) for i in range(offset, offset + n)])
        stub._remember_expanded_window()
        return stub

    def test_four_sent_messages_do_not_drop_four_rows(self):
        """O relato, ponta a ponta: 200 linhas na tela, 4 mensagens enviadas,
        repaint. As 4 linhas mais antigas têm de continuar lá."""
        stub = self._rendered(200, offset=300)
        # Os appends ao vivo (on_incoming_message() e o envio otimista) não
        # passam por populate_messages(); o rebuild é que vem depois.
        displayable = [_msg(f"m-{i}", i) for i in range(300, 504)]
        offset, sep = stub._history_window_for_rebuild(displayable, 200)
        assert (offset, sep) == (0, -1), "o rebuild cortou de volta ao page size"
        assert len(displayable[offset:]) == 204

    def test_the_window_only_grows_while_the_conversation_stays_open(self):
        """Cada rebuild registra a janela que pintou, então o piso acompanha as
        chegadas em vez de ficar preso na contagem da abertura."""
        stub = self._rendered(200, offset=300)
        displayable = [_msg(f"m-{i}", i) for i in range(300, 504)]
        stub._sorted_messages = displayable[stub._history_window_for_rebuild(displayable, 200)[0]:]
        stub._remember_expanded_window()
        assert stub._expanded_visible_count == 204
        assert stub._expanded_oldest_msg_id == "m-300"
        displayable = [_msg(f"m-{i}", i) for i in range(300, 510)]
        assert stub._history_window_for_rebuild(displayable, 200)[0] == 0

    def test_opening_a_conversation_still_honours_the_page_size(self):
        """O piso é o que foi pintado, não tudo que existe: uma conversa recém
        aberta continua rendendo messages_page_size linhas, e só a partir daí
        para de encolher."""
        stub = _Stub()
        displayable = [_msg(f"m-{i}", i) for i in range(500)]
        assert stub._history_window_for_rebuild(displayable, 200)[0] == 300

    def test_populate_messages_records_the_window_it_rendered(self):
        """populate_messages() precisa de um wx.ListCtrl de verdade, então o
        que dá para prender aqui é a chamada — sem ela o piso volta a ser só o
        que o Home carregou e o corte a cada mensagem nova volta junto."""
        src = inspect.getsource(ConversationsPanel.populate_messages)
        assert "self._remember_expanded_window()" in src

    def test_a_list_with_only_a_placeholder_records_nothing(self):
        """Conversa sem histórico exibível: um piso de 1 linha não significa
        nada e a âncora vazia não prenderia coisa alguma."""
        stub = _Stub(sorted_messages=[{"_type": "empty_placeholder"}])
        stub._remember_expanded_window()
        assert stub._expanded_visible_count == 0
        assert stub._expanded_oldest_msg_id == ""

    def test_the_unread_separator_is_never_the_anchor(self):
        """O separador não tem key.id; ancorar nele perderia a âncora inteira."""
        sep = {"_type": "unread_separator", "count": 3}
        stub = _Stub(sorted_messages=[sep] + [_msg(f"m-{i}", i) for i in range(10)])
        stub._remember_expanded_window()
        assert stub._expanded_oldest_msg_id == "m-0"
        assert stub._expanded_visible_count == 11

    def test_a_record_without_a_key_does_not_take_the_rebuild_down(self):
        """key ausente ou None aparece em registro malformado, e isto agora roda
        no finally de todo rebuild — levantar aqui derrubaria a lista inteira."""
        stub = _Stub(sorted_messages=[{"timestamp": 1}, _msg("m-1", 1)])
        stub._remember_expanded_window()
        assert stub._expanded_visible_count == 2
        stub = _Stub(sorted_messages=[{"key": None, "timestamp": 1}])
        stub._remember_expanded_window()
        assert stub._expanded_oldest_msg_id == ""

    def test_a_malformed_record_on_top_does_not_cost_the_anchor(self):
        """Um registro sem key.id no topo não pode zerar a âncora.

        Parar nele deixava só o piso por contagem, e o piso sozinho escorrega
        uma linha para frente a cada mensagem que chega ao vivo — que é
        exatamente o sintoma que esta janela existe para corrigir, reaparecendo
        por outra porta. A âncora passa a ser a primeira linha que TEM id.
        """
        stub = _Stub(sorted_messages=[
            {"timestamp": 1},
            {"key": None, "timestamp": 2},
            {"key": {"id": ""}, "timestamp": 3},
            _msg("m-4", 4),
            _msg("m-5", 5),
        ])
        stub._remember_expanded_window()
        assert stub._expanded_oldest_msg_id == "m-4"
        assert stub._expanded_visible_count == 5

    def test_an_anchor_further_in_only_widens_the_window(self):
        """A âncora mais nova nunca estreita: expanded_min_visible() aplica
        max(piso, âncora), e o piso é a contagem inteira da lista."""
        displayable = [_msg(f"m-{i}", i) for i in range(500)]
        # Âncora em m-100 => 400 linhas; piso (contagem registrada) => 450.
        assert expanded_min_visible(displayable, "m-100", 450) == 450
        # E com a âncora mais antiga que o piso, é ela que manda.
        assert expanded_min_visible(displayable, "m-100", 10) == 400
