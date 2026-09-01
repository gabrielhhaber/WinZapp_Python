"""Tests for the guard that stops background refreshes rebuilding the message list.

Reported live: "o programa desvia o foco da lista de mensagens do nada" — every
so often, mid-read, focus jumped to a random message in the middle of the
conversation. The 60-second poll (start_periodic_contacts_sync) and the history
backfill both called populate_messages(preserve_focus=True) unconditionally, and
that does a full DeleteAllItems() + re-Append() of the native ListView. Even with
preserve_focus it can only restore focus to the *message* it saved — the moment
that message is no longer in the paginated window, or the list was sitting on the
unread separator, focus lands somewhere else entirely.

refresh_messages_if_changed() compares a fingerprint of what would be rendered
and skips the rebuild when nothing changed, so periodic polls stop touching the
list at all.

The case that stayed expensive was the commonest one: a new message. The panel
already appends it live in on_incoming_message(), and the background refresh
seconds later rebuilt the whole list to paint what was already on screen — over
a window that, since it stopped being capped at messages_page_size, can hold
thousands of rows. _append_new_tail_rows() now covers "rows added at the end and
nothing else" by appending them (usually none, the live path having painted
them). Everything else still rebuilds; TestTailAppendInsteadOfRebuild pins where
the line is.

ConversationsPanel is a wx.Panel and cannot be instantiated without a running
wx.App, so the methods under test are exercised as plain functions against a
small stub — same approach as tests/test_message_bookmarks.py.
"""

import pytest

from ui.conversations import ConversationsPanel


class _FakeList:
    """Só o que o caminho incremental toca de um wx.ListCtrl."""

    def __init__(self):
        self.rows = []

    def GetItemCount(self):
        return len(self.rows)

    def Append(self, row):
        self.rows.append(row[0] if isinstance(row, tuple) else row)

    def Freeze(self):
        pass

    def Thaw(self):
        pass


class _Stub:
    _messages_signature = ConversationsPanel._messages_signature
    # staticmethod no painel real; sem reembrulhar, o self entraria como
    # primeiro argumento.
    _signature_changed_ids = staticmethod(ConversationsPanel._signature_changed_ids)
    _append_new_tail_rows = ConversationsPanel._append_new_tail_rows
    _row_position_suffix_active = ConversationsPanel._row_position_suffix_active
    _remember_expanded_window = ConversationsPanel._remember_expanded_window
    _is_separator = ConversationsPanel._is_separator
    _is_displayable_message = ConversationsPanel._is_displayable_message
    refresh_messages_if_changed = ConversationsPanel.refresh_messages_if_changed

    def __init__(self, records=None, jid="g@g.us"):
        self.conversation = {
            "remoteJid": jid,
            "messages": {"messages": {"records": records if records is not None else []}},
        }
        self._first_unread_msg_id = None
        self._pending_open_unread = 0
        self._messages_signature_cache = None
        self.populate_calls = []
        self.messages_list = _FakeList()
        self._sorted_messages = []
        self._all_sorted_messages = []
        self._expanded_visible_count = 0
        self._expanded_oldest_msg_id = ""
        self._unread_sep_idx = -1

    # The panel's own helpers, kept trivial here: what is under test is how the
    # signature is composed and what it decides, not their formatting.
    @staticmethod
    def _extract_timestamp(msg):
        return msg.get("messageTimestamp", 0)

    @staticmethod
    def _get_message_content(msg):
        return (msg.get("message") or {}).get("conversation", "")

    def _render_message_line(self, msg, index=None, total=None):
        return self._get_message_content(msg)

    def populate_messages(self, preserve_focus=False):
        self.populate_calls.append(preserve_focus)
        # O rebuild de verdade, no essencial que estes testes checam: ordena os
        # exibíveis por timestamp, refaz a lista do zero e só então tira a
        # fotografia da assinatura (o finally do método real, tolerância a uma
        # assinatura que levanta incluída).
        displayable = sorted(
            (m for m in self._records
             if isinstance(m, dict) and self._is_displayable_message(m)),
            key=lambda m: self._extract_timestamp(m) or 0,
        )
        self._all_sorted_messages = list(displayable)
        self._sorted_messages = list(displayable)
        self.messages_list.rows = [self._render_message_line(m) for m in displayable]
        try:
            self._messages_signature_cache = self._messages_signature()
        except Exception:
            self._messages_signature_cache = None

    @property
    def _records(self):
        return self.conversation["messages"]["messages"]["records"]


def _msg(mid, text="oi", ts=1000, **extra):
    m = {
        "key": {"id": mid, "fromMe": False},
        "messageType": "conversation",
        "message": {"conversation": text},
        "messageTimestamp": ts,
    }
    m.update(extra)
    return m


class TestRefreshMessagesIfChanged:
    def test_the_first_call_always_rebuilds(self):
        """Nothing has been rendered yet — there is no cached signature to trust."""
        s = _Stub([_msg("a")])
        s.refresh_messages_if_changed()
        assert s.populate_calls == [True]

    def test_a_repeat_poll_over_unchanged_records_does_nothing(self):
        """This is the whole point: the 60s poll must not touch the list."""
        s = _Stub([_msg("a"), _msg("b")])
        s.refresh_messages_if_changed()
        for _ in range(10):
            s.refresh_messages_if_changed()
        assert s.populate_calls == [True], "only the initial render should have run"

    def test_a_new_message_appends_instead_of_rebuilding(self):
        """Era um rebuild inteiro por mensagem nova. Ver
        TestTailAppendInsteadOfRebuild para o resto do contrato."""
        s = _Stub([_msg("a")])
        s.refresh_messages_if_changed()
        s._records.append(_msg("b", text="nova", ts=2000))
        s.refresh_messages_if_changed()
        assert s.populate_calls == [True]
        assert s.messages_list.rows == ["oi", "nova"]

    def test_a_deleted_message_rebuilds(self):
        s = _Stub([_msg("a"), _msg("b")])
        s.refresh_messages_if_changed()
        s._records.pop()
        s.refresh_messages_if_changed()
        assert len(s.populate_calls) == 2

    def test_an_edited_body_rebuilds(self):
        s = _Stub([_msg("a", text="oi")])
        s.refresh_messages_if_changed()
        s._records[0]["message"]["conversation"] = "oi, tudo bem?"
        s.refresh_messages_if_changed()
        assert len(s.populate_calls) == 2

    def test_a_delivery_status_change_rebuilds(self):
        s = _Stub([_msg("a", status="SENT")])
        s.refresh_messages_if_changed()
        s._records[0]["status"] = "READ"
        s.refresh_messages_if_changed()
        assert len(s.populate_calls) == 2

    def test_starring_rebuilds(self):
        s = _Stub([_msg("a")])
        s.refresh_messages_if_changed()
        s._records[0]["starred"] = True
        s.refresh_messages_if_changed()
        assert len(s.populate_calls) == 2

    def test_pinning_rebuilds(self):
        s = _Stub([_msg("a")])
        s.refresh_messages_if_changed()
        s._records[0]["pinInChat"] = True
        s.refresh_messages_if_changed()
        assert len(s.populate_calls) == 2

    def test_the_edited_marker_rebuilds(self):
        s = _Stub([_msg("a")])
        s.refresh_messages_if_changed()
        s._records[0]["_edited"] = True
        s.refresh_messages_if_changed()
        assert len(s.populate_calls) == 2

    def test_a_pending_message_being_confirmed_rebuilds(self):
        s = _Stub([_msg("a", _local_pending=True)])
        s.refresh_messages_if_changed()
        s._records[0]["_local_pending"] = False
        s.refresh_messages_if_changed()
        assert len(s.populate_calls) == 2

    def test_switching_conversation_rebuilds(self):
        s = _Stub([_msg("a")])
        s.refresh_messages_if_changed()
        s.conversation["remoteJid"] = "other@g.us"
        s.refresh_messages_if_changed()
        assert len(s.populate_calls) == 2

    def test_a_moved_unread_separator_rebuilds(self):
        s = _Stub([_msg("a"), _msg("b")])
        s.refresh_messages_if_changed()
        s._first_unread_msg_id = "b"
        s.refresh_messages_if_changed()
        assert len(s.populate_calls) == 2

    def test_no_open_conversation_is_a_no_op(self):
        s = _Stub([_msg("a")])
        s.conversation = None
        s.refresh_messages_if_changed()
        assert s.populate_calls == []

    def test_a_broken_signature_falls_back_to_rebuilding(self):
        """A fingerprinting hiccup must never swallow a real refresh."""
        s = _Stub([_msg("a")])

        def _boom(self):
            raise RuntimeError("nope")

        s._messages_signature = _boom.__get__(s)
        s.refresh_messages_if_changed()
        assert s.populate_calls == [True]

    def test_reordering_records_rebuilds(self):
        s = _Stub([_msg("a", ts=1), _msg("b", ts=2)])
        s.refresh_messages_if_changed()
        s._records.reverse()
        s.refresh_messages_if_changed()
        assert len(s.populate_calls) == 2


class TestMessagesSignature:
    def test_malformed_records_do_not_raise(self):
        s = _Stub([None, "junk", _msg("a")])
        assert s._messages_signature()

    def test_a_missing_records_container_is_tolerated(self):
        s = _Stub()
        s.conversation = {"remoteJid": "g@g.us"}
        assert s._messages_signature()


class TestTailAppendInsteadOfRebuild:
    """Mensagem nova deixa de reconstruir a lista inteira.

    Era o caso mais comum que ainda passava pelo rebuild: o painel já
    acrescenta a mensagem ao vivo em on_incoming_message(), e o refresh de
    fundo que vem segundos depois (sync_chat_messages() ->
    _refresh_open_conversation_after_sync(), o backfill de histórico, a rodada
    de 60s) fazia DeleteAllItems() mais um Append() por linha só para pintar de
    novo o que já estava na tela — numa janela que, desde que ela deixou de ser
    limitada ao messages_page_size, pode ter milhares de linhas.

    O critério que governa o atalho é que a lista tem de terminar exatamente
    como o rebuild a deixaria; tudo que não for "linha nova no fim, nada mais
    mudou" continua reconstruindo."""

    def _rendered(self, records):
        s = _Stub(list(records))
        s.refresh_messages_if_changed()   # rebuild inicial
        return s

    def _paint_live(self, s, msg):
        """O que on_incoming_message() faz, e SÓ o que ele faz.

        Ele não toca _all_sorted_messages (conversations.py, o append no fim
        de on_incoming_message): é justamente esse desalinhamento que a guarda
        in_step do atalho tem de enxergar, e um fixture que o "consertasse"
        deixaria a guarda sem teste nenhum."""
        s._records.append(msg)
        s._sorted_messages.append(msg)
        s.messages_list.rows.append(s._render_message_line(msg))

    def test_the_common_case_appends_nothing_because_the_live_path_painted_it(self):
        s = self._rendered([_msg("a")])
        self._paint_live(s, _msg("b", text="nova", ts=2000))
        s.refresh_messages_if_changed()
        assert s.populate_calls == [True], "o rebuild por mensagem nova sumiu"
        assert s.messages_list.rows == ["oi", "nova"], "não pode duplicar a linha"

    def test_the_poll_after_that_does_nothing_at_all(self):
        """A assinatura tem de ser adotada, senão o rebuild só foi adiado."""
        s = self._rendered([_msg("a")])
        self._paint_live(s, _msg("b", text="nova", ts=2000))
        s.refresh_messages_if_changed()
        for _ in range(10):
            s.refresh_messages_if_changed()
        assert s.populate_calls == [True]
        assert s.messages_list.rows == ["oi", "nova"]

    def test_a_message_that_only_reached_records_is_appended(self):
        """Chegou pelo sync, sem passar pelo caminho ao vivo: a linha não está
        na tela e é este método que tem de pintá-la."""
        s = self._rendered([_msg("a")])
        s._records.append(_msg("b", text="nova", ts=2000))
        s.refresh_messages_if_changed()
        assert s.populate_calls == [True]
        assert s.messages_list.rows == ["oi", "nova"]
        assert [m["key"]["id"] for m in s._sorted_messages] == ["a", "b"]
        assert [m["key"]["id"] for m in s._all_sorted_messages] == ["a", "b"]

    def test_several_at_once_are_appended_in_timestamp_order(self):
        s = self._rendered([_msg("a", ts=1000)])
        s._records.append(_msg("c", text="terceira", ts=3000))
        s._records.append(_msg("b", text="segunda", ts=2000))
        s.refresh_messages_if_changed()
        assert s.populate_calls == [True]
        assert s.messages_list.rows == ["oi", "segunda", "terceira"]

    def test_a_message_older_than_the_last_row_rebuilds(self):
        """Ela entra no meio da lista ordenada por timestamp, não no fim — só o
        rebuild sabe onde."""
        s = self._rendered([_msg("a", ts=5000)])
        s._records.append(_msg("b", text="antiga", ts=1000))
        s.refresh_messages_if_changed()
        assert len(s.populate_calls) == 2

    def test_a_new_record_that_is_not_a_row_rebuilds(self):
        """A reação é o caso comum: ela muda o texto de OUTRA linha, e a
        assinatura não diz de qual."""
        s = self._rendered([_msg("a")])
        s._records.append(_msg("r", ts=2000, messageType="reactionMessage"))
        s.refresh_messages_if_changed()
        assert len(s.populate_calls) == 2

    def test_a_row_changing_alongside_a_new_one_rebuilds(self):
        s = self._rendered([_msg("a")])
        s._records[0]["status"] = "READ"
        s._records.append(_msg("b", ts=2000))
        s.refresh_messages_if_changed()
        assert len(s.populate_calls) == 2

    def test_records_reordered_under_a_new_message_rebuilds(self):
        """Reordenação passa pela comparação por id, mas o sort estável do
        rebuild trocaria de lugar duas mensagens de mesmo timestamp."""
        s = self._rendered([_msg("a", ts=1000), _msg("b", ts=1000)])
        s._records[0], s._records[1] = s._records[1], s._records[0]
        s._records.append(_msg("c", ts=2000))
        s.refresh_messages_if_changed()
        assert len(s.populate_calls) == 2

    def test_a_list_out_of_step_with_the_control_rebuilds(self):
        """Um Append() com a lista desalinhada gruda texto e registro errados."""
        s = self._rendered([_msg("a")])
        s.messages_list.rows.append("linha fantasma")
        s._records.append(_msg("b", ts=2000))
        s.refresh_messages_if_changed()
        assert len(s.populate_calls) == 2

    def test_the_placeholder_list_rebuilds(self):
        s = self._rendered([_msg("a")])
        s._sorted_messages = [{"_type": "empty_placeholder"}]
        s.messages_list.rows = ["sem mensagens"]
        s._records.append(_msg("b", ts=2000))
        s.refresh_messages_if_changed()
        assert len(s.populate_calls) == 2

    def test_an_empty_list_rebuilds(self):
        """Append() na lista vazia com foco reproduz o pulo de foco para a
        linha 0 que o Freeze() de populate_messages() documenta."""
        s = self._rendered([])
        s._records.append(_msg("b", ts=2000))
        s.refresh_messages_if_changed()
        assert len(s.populate_calls) == 2

    def test_the_item_count_suffix_forces_the_rebuild(self):
        """Com ", N de M" ligado, acrescentar muda o M de todas as linhas já
        renderizadas — o leitor de tela passaria a anunciar total velho."""
        s = self._rendered([_msg("a")])
        s._message_list_mode = "listbox"
        s.main_window = type("_MW", (), {})()
        s.main_window.settings = {"user_interface": {"show_listbox_item_count": True}}
        s._records.append(_msg("b", ts=2000))
        s.refresh_messages_if_changed()
        assert len(s.populate_calls) == 2

    def test_the_same_setting_off_still_takes_the_shortcut(self):
        s = self._rendered([_msg("a")])
        s._message_list_mode = "listbox"
        s.main_window = type("_MW", (), {})()
        s.main_window.settings = {"user_interface": {"show_listbox_item_count": False}}
        s._records.append(_msg("b", text="nova", ts=2000))
        s.refresh_messages_if_changed()
        assert s.populate_calls == [True]
        assert s.messages_list.rows == ["oi", "nova"]

    def test_the_history_window_floor_follows_the_appended_rows(self):
        """O piso da janela é o que está na tela; acrescentar sem atualizá-lo
        deixaria o rebuild seguinte livre para cortar as linhas novas."""
        s = self._rendered([_msg("a")])
        s._records.append(_msg("b", ts=2000))
        s.refresh_messages_if_changed()
        assert s._expanded_visible_count == 2
        assert s._expanded_oldest_msg_id == "a"

    def test_a_failure_in_the_shortcut_falls_back_to_the_rebuild(self):
        """O atalho é otimização: uma falha nele não pode custar a
        atualização."""
        s = self._rendered([_msg("a")])

        def _boom(self, old_sig, new_sig):
            raise RuntimeError("nope")

        s._append_new_tail_rows = _boom.__get__(s)
        s._records.append(_msg("b", ts=2000))
        s.refresh_messages_if_changed()
        assert len(s.populate_calls) == 2

    def test_all_sorted_messages_is_left_alone_when_it_is_out_of_step(self):
        """on_incoming_message() acrescenta só em _sorted_messages, então as
        duas listas terminam diferentes. O atalho não pode inventar um
        alinhamento que não tem como verificar — a metade contrária está em
        test_a_message_that_only_reached_records_is_appended."""
        s = self._rendered([_msg("a")])
        self._paint_live(s, _msg("b", text="nova", ts=2000))
        s._records.append(_msg("c", text="terceira", ts=3000))
        s.refresh_messages_if_changed()
        assert s.populate_calls == [True]
        assert [m["key"]["id"] for m in s._sorted_messages] == ["a", "b", "c"]
        assert [m["key"]["id"] for m in s._all_sorted_messages] == ["a"]
        assert s.messages_list.rows == ["oi", "nova", "terceira"]

    def test_the_live_unread_separator_survives_the_shortcut(self):
        """Consequência declarada da mudança, não efeito colateral.

        on_incoming_message() insere o separador ao vivo mas nunca escreve
        _first_unread_msg_id, então o rebuild não o renderizava de volta e ele
        sumia segundos depois de aparecer — junto com o alvo do Alt+3. O atalho
        o mantém, e mantém _unread_sep_idx apontando para ele."""
        s = self._rendered([_msg("a")])
        sep = {"_type": "unread_separator", "count": 1}
        s._sorted_messages.append(sep)
        s.messages_list.rows.append("--- não lidas ---")
        s._unread_sep_idx = len(s._sorted_messages) - 1
        self._paint_live(s, _msg("b", text="nova", ts=2000))
        s.refresh_messages_if_changed()
        assert s.populate_calls == [True]
        assert s.messages_list.rows == ["oi", "--- não lidas ---", "nova"]
        assert s._sorted_messages[s._unread_sep_idx] is sep

    def test_a_list_of_separators_alone_rebuilds(self):
        """Sem nenhuma mensagem na tela não há cauda contra a qual comparar o
        timestamp do recém-chegado."""
        s = self._rendered([_msg("a")])
        s._sorted_messages = [{"_type": "unread_separator", "count": 1}]
        s.messages_list.rows = ["--- não lidas ---"]
        s._records.append(_msg("b", ts=2000))
        s.refresh_messages_if_changed()
        assert len(s.populate_calls) == 2
