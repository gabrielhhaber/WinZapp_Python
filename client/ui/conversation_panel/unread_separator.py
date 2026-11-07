"""UnreadSeparatorMixin — part of ConversationsPanel (see ui/conversation_panel/__init__.py).

Moved verbatim out of ui/conversations.py. Methods run with ``self`` bound to
the ConversationsPanel instance, so every attribute set in
ConversationsPanel.__init__/init_UI is available here.
"""

import logging

import wx

from core.utils import first_unread_index


class UnreadSeparatorMixin:
    """The unread-messages separator row: placing, moving and dismissing it.
    """

    @staticmethod
    def _should_dismiss_unread_separator(focused_idx: int, sep_idx: int) -> bool:
        """True once focus has moved past the unread separator, into the unread
        messages themselves.

        The separator used to be removed by a one-shot 2-second timer armed the
        moment focus merely *reached* it. So it disappeared out from under a
        user who was still sitting on it — and for a screen-reader user, who may
        well take longer than two seconds to hear the row and decide what to do,
        the one marker showing where the new messages start was simply gone,
        with nothing having happened to warrant it.

        Tie it to the action it is supposed to represent instead: the separator
        is dismissed when the user actually steps down past it. Landing on the
        separator row itself is explicitly not enough — that is where
        populate_messages() parks focus when the conversation is opened, i.e.
        before the user has read anything at all.
        """
        if sep_idx < 0 or focused_idx < 0:
            return False
        return focused_idx > sep_idx

    def _dismiss_unread_separator(self):
        """Remove the unread separator row without stealing focus."""
        if self._unread_sep_idx < 0:
            return
        sep_idx = self._unread_sep_idx
        focused = self.messages_list.GetFocusedItem()
        self.messages_list.Freeze()
        try:
            self._sorted_messages.pop(sep_idx)
            self.messages_list.DeleteItem(sep_idx)
        finally:
            self.messages_list.Thaw()
        self._unread_sep_idx = -1
        self._sep_anchors_read_position = False
        self._first_unread_msg_id = None
        self._first_unread_count = 0
        # The unread-summary button and Alt+Shift+U gate on this separator: a
        # dismissal (read past it, or an outgoing message) can drop the unread
        # tail below the configured minimum, so the button re-evaluates.
        # Tests bind this method without a wx.App: only schedule then.
        if wx.GetApp() is not None:
            wx.CallAfter(getattr(self, "_update_summary_button", lambda: None))
        # Restore the focused row (shifted by 1 if it was after the separator)
        if focused > sep_idx:
            focused -= 1
        elif focused == sep_idx:
            focused = max(0, sep_idx - 1)
        if 0 <= focused < self.messages_list.GetItemCount():
            self.messages_list.Focus(focused)

    def _clear_empty_placeholder(self):
        """Remove the 'no messages' placeholder from the list if it is present."""
        if self._sorted_messages and isinstance(self._sorted_messages[0], dict) and self._sorted_messages[0].get("_type") == "empty_placeholder":
            self._sorted_messages.pop(0)
            self.messages_list.DeleteItem(0)
            self._recompute_unread_sep_idx()

    def _is_separator(self, msg: dict) -> bool:
        """Return True if msg is a non-message sentinel row (unread separator
        or the "no messages" placeholder) rather than a real message — every
        activation/edit/delete/etc. handler guards on this before touching
        msg["key"], so a new sentinel type just needs to be added here."""
        return isinstance(msg, dict) and msg.get("_type") in ("unread_separator", "empty_placeholder")

    def _recompute_unread_sep_idx(self):
        """Re-locate the unread separator's row index by scanning
        ``_sorted_messages`` from scratch, setting ``_unread_sep_idx`` to -1
        if it isn't present. Call after prepending older history — the
        separator's row shifts by however many rows were inserted above it,
        and a plain offset adjustment isn't available in every caller.
        Duplicated as an identical inline loop in two separate call sites
        before this existed.
        """
        self._unread_sep_idx = -1
        for idx, msg in enumerate(self._sorted_messages):
            if self._is_separator(msg):
                self._unread_sep_idx = idx
                break

    def _counts_toward_unread_separator(self, msg: dict) -> bool:
        """Mesmo teste que main.py usa para incrementar o unreadCount do chat.

        O preview da lista de conversas e o separador têm de contar a MESMA
        coisa. main.py só sobe o unreadCount para mensagens que passam por
        is_countable_message(), enquanto este caminho olhava apenas fromMe: um
        evento de sistema (groupNotification, protocolMessage,
        e2e_notification, ...) chegando numa conversa aberta subia o separador
        sem subir o preview, e a divergência aparecia exatamente como os dois
        números discordando.

        Consequência aceita, e não descuido: um groupNotification é EXIBÍVEL
        mas não contável, então "o separador diz 1 e há duas linhas abaixo
        dele" continua possível — e agora está certo, porque bate com o badge
        da lista de conversas, que também não contou o evento de sistema. A
        descrição solta desse estado é idêntica à do bug relatado; a diferença
        é a linha extra ser um evento, não uma mensagem.

        O import é feito aqui dentro de propósito: main.py importa este módulo,
        então um import no topo seria circular. O fallback não protege a
        mensagem (o append acontece fora deste ramo, ela aparece de qualquer
        forma) — protege só a contagem, preferindo contar a mais a perder um
        separador. E um erro real na cadeia de import de main.py não pode ser
        enterrado mudo aqui, daí o log.
        """
        try:
            from main_window.message_rules import is_countable_message
        except Exception:
            logging.exception(
                "[_counts_toward_unread_separator] import de is_countable_message falhou"
            )
            return True
        return is_countable_message(msg)

    def _update_unread_separator_for_incoming(self, msg: dict) -> None:
        """Insere, move ou incrementa o separador para *msg*, que o chamador
        acrescenta à cauda logo em seguida.

        Grava sempre o par _first_unread_msg_id/_first_unread_count junto com a
        linha: é esse par, e só ele, que populate_messages() lê para recriar o
        separador depois do seu DeleteAllItems(). Enquanto este caminho
        escrevia apenas _sorted_messages e _unread_sep_idx, o primeiro rebuild
        (vários por minuto com a conversa aberta) apagava o separador e não o
        recriava — e sem separador _on_message_focused() nunca chamava
        mark_conversation_as_read(), então o chat ficava "1 mensagem não lida"
        no preview e não lido no celular até o usuário marcar à mão.

        Chamado de dentro do Freeze()/Thaw() de on_incoming_message(); não mexe
        em foco.
        """
        # Uma mensagem nova ao vivo sempre representa conteúdo genuinamente não
        # lido, mesmo que o usuário já tivesse chegado ao fim (e portanto
        # marcado como lida) antes nesta mesma sessão de conversa. Rearma a
        # trava para _on_message_focused() disparar o mark-as-read de novo
        # quando o foco alcançar/passar esta mensagem.
        self._unread_sep_marked_read = False
        msg_id = (msg.get("key") or {}).get("id") or None
        # _unread_sep_idx pode ficar velho para um _sorted_messages que foi
        # esvaziado/reconstruído por baixo dele sem voltar para -1 (ex.:
        # "Limpar conversa" no chat aberto) — todo ramo abaixo que indexa ou dá
        # pop() nele tem de tratar índice fora de faixa como "ainda não há
        # separador" em vez de estourar (visto ao vivo: "IndexError: pop from
        # empty list").
        sep_idx_valid = 0 <= self._unread_sep_idx < len(self._sorted_messages)
        if sep_idx_valid and not self._is_separator(
            self._sorted_messages[self._unread_sep_idx]
        ):
            sep_idx_valid = False
        if not sep_idx_valid:
            # Nenhum separador ainda — insere um antes desta mensagem nova.
            sep_pos = len(self._sorted_messages)
            sep = {"_type": "unread_separator", "count": 1}
            self._sorted_messages.insert(sep_pos, sep)
            self.messages_list.InsertItem(sep_pos, self._render_message_line(sep))
            self._unread_sep_idx = sep_pos
            self._sep_anchors_read_position = False
            self._first_unread_msg_id = msg_id
            self._first_unread_count = 1
        elif self._sep_anchors_read_position:
            # O foco do usuário já passou por este separador: ele ancora uma
            # posição lida. Move-o para antes desta mensagem e reinicia em 1.
            old_idx = self._unread_sep_idx
            self._sorted_messages.pop(old_idx)
            self.messages_list.DeleteItem(old_idx)
            sep_pos = len(self._sorted_messages)
            sep = {"_type": "unread_separator", "count": 1}
            self._sorted_messages.insert(sep_pos, sep)
            self.messages_list.InsertItem(sep_pos, self._render_message_line(sep))
            self._unread_sep_idx = sep_pos
            self._sep_anchors_read_position = False
            self._first_unread_msg_id = msg_id
            self._first_unread_count = 1
        else:
            # Separador ainda ancora conteúdo não lido (colocado na abertura da
            # conversa ou por uma mensagem ao vivo anterior, e o foco não passou
            # por ele): soma, sem mover. A âncora continua sendo a primeira
            # mensagem abaixo dele — só recalculada quando falta, para um
            # separador herdado de um estado que não gravava esse par.
            sep = self._sorted_messages[self._unread_sep_idx]
            sep["count"] = int(sep.get("count", 0) or 0) + 1
            # Reescrever esta linha tem custo de acessibilidade, e ele foi
            # pesado, não ignorado: a linha do wx.ListCtrl é um objeto MSAA
            # cujo nome é a linha inteira, e o event_nameChange do NVDA a fala
            # quando ela é a linha focada (ver "Played-row repaint hold" no
            # CLAUDE.md). Com focus_on_open == "unread_or_last" é exatamente
            # aqui que populate_messages() estaciona o foco de item, então cada
            # mensagem que chega pode fazer o leitor reler "N mensagens não
            # lidas". Não é regressão — o ramo antigo dava DeleteItem na mesma
            # linha focada, que também emite evento — e a alternativa (segurar
            # a escrita enquanto a linha está focada) é pior: um separador que
            # para de somar na tela mente sobre quantas mensagens chegaram, que
            # é o bug que este trecho existe para fechar. Fica a troca
            # registrada; a linha é reescrita, o número na tela é verdadeiro.
            self.messages_list.SetItemText(
                self._unread_sep_idx, self._render_message_line(sep)
            )
            # Recalculada SEMPRE, não só quando falta: _delete_message_rows()
            # ajusta _unread_sep_idx quando uma mensagem some, mas não toca na
            # âncora, então ela pode apontar para um id que não existe mais em
            # records. Sem recalcular aqui, o alinhamento de
            # _messages_signature_cache abaixo passaria a valer para um id
            # morto e _append_new_tail_rows() aceitaria uma tela que o rebuild
            # daquele instante não produziria. É idempotente no caso normal.
            self._first_unread_msg_id = (
                self._anchor_below_unread_separator() or msg_id
            )
            self._first_unread_count = sep["count"]
        # _first_unread_msg_id entra em _messages_signature() para que um
        # separador que mudou de lugar force um rebuild. Aqui quem o moveu foi
        # este método, na tela e em _sorted_messages ao mesmo tempo, então o
        # atalho de acrescentar na cauda continua correto — sem alinhar a
        # assinatura em cache, toda mensagem nova passaria a custar um rebuild
        # inteiro da lista debaixo do leitor de tela.
        cache = getattr(self, "_messages_signature_cache", None)
        if isinstance(cache, tuple) and len(cache) == 4:
            self._messages_signature_cache = (
                cache[0], self._first_unread_msg_id, cache[2], cache[3]
            )

    def _anchor_below_unread_separator(self):
        """Id da primeira mensagem de verdade abaixo do separador, ou None."""
        if not (0 <= self._unread_sep_idx < len(self._sorted_messages)):
            return None
        for m in self._sorted_messages[self._unread_sep_idx + 1:]:
            if not isinstance(m, dict) or self._is_separator(m):
                continue
            mid = (m.get("key") or {}).get("id")
            if mid:
                return mid
        return None

    def _place_unread_separator_for_rebuild(self, displayable: list) -> list:
        """Devolve *displayable* com o separador de não lidas na posição certa.

        Dois casos, e confundi-los é metade do bug do separador:

        - **Derivação**, quando _pending_open_unread traz a contagem tirada da
          lista de conversas no momento em que ela foi aberta. Aí o par
          âncora/contagem é calculado do zero e o separador NÃO ancora posição
          lida: as mensagens abaixo dele são genuinamente não lidas, então a
          próxima mensagem ao vivo tem de somar nele.
        - **Restauração**, quando o par já existe — tipicamente escrito pelo
          caminho ao vivo. Aí _sep_anchors_read_position e
          _unread_sep_marked_read são do separador que está sendo restaurado e
          têm de ser PRESERVADOS: marcá-los aqui fazia a próxima mensagem ao
          vivo mover o separador e reiniciar a contagem em 1 em vez de somar.

        Fora de populate_messages() (que precisa de um wx.ListCtrl de verdade)
        porque é exatamente este passo que desfazia o trabalho do caminho ao
        vivo, e ele precisava de teste.
        """
        unread_count = self._pending_open_unread
        self._pending_open_unread = 0
        first_unread_idx = first_unread_index(displayable, unread_count)
        if first_unread_idx >= 0:
            first_unread_msg = displayable[first_unread_idx]
            if isinstance(first_unread_msg, dict):
                self._first_unread_msg_id = first_unread_msg.get("key", {}).get("id")
                self._first_unread_count = unread_count
                self._sep_anchors_read_position = False
                self._unread_sep_marked_read = False

        if self._first_unread_msg_id:
            sep_pos = -1
            for idx, msg in enumerate(displayable):
                if isinstance(msg, dict) and msg.get("key", {}).get("id") == self._first_unread_msg_id:
                    sep_pos = idx
                    break
            if sep_pos >= 0:
                # max(1, ...) é puramente defensivo: um separador dizendo
                # "0 mensagens não lidas" seria uma linha sem sentido para
                # quem a ouvir. Consequência a saber caso alguém queira usar
                # 0 para "esconder o separador": aqui ele vira 1 em silêncio,
                # e o lugar de esconder é _dismiss_unread_separator().
                sep = {
                    "_type": "unread_separator",
                    "count": max(1, int(self._first_unread_count or 1)),
                }
                displayable = displayable[:sep_pos] + [sep] + displayable[sep_pos:]
                self._unread_sep_idx = sep_pos
        return displayable

    def _render_separator(self, count: int) -> str:
        i18n = self.main_window.i18n
        if count == 1:
            return i18n.t("unread_sep_singular")
        return i18n.t("unread_sep_plural").format(count=count)
