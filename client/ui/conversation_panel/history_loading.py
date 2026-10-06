"""HistoryLoadingMixin — part of ConversationsPanel (see ui/conversation_panel/__init__.py).

Moved verbatim out of ui/conversations.py. Methods run with ``self`` bound to
the ConversationsPanel instance, so every attribute set in
ConversationsPanel.__init__/init_UI is available here.
"""

import logging
import threading
import wx
from core.utils import history_window
from main_window import history_boundary


class HistoryLoadingMixin:
    """Loading older history into the open conversation, locally and from the
    server.
    """

    def _deduplicate_messages(self, messages_list: list) -> list:
        seen = set()
        result = []
        for m in reversed(messages_list):
            if not isinstance(m, dict):
                result.append(m)
                continue
            mid = m.get("key", {}).get("id", "")
            if not mid:
                result.append(m)
                continue
            if mid not in seen:
                seen.add(mid)
                result.append(m)
        result.reverse()
        return result

    def _history_storage_jid(self, remote_jid: str) -> str:
        """Return the JID under which this conversation is stored locally."""
        phone_to_lid = getattr(self.main_window, "_phone_to_lid", {})
        mapped_lid = phone_to_lid.get(remote_jid, "")
        if mapped_lid:
            logging.info(
                "[_load_older_messages] Using mapped local-history JID %s for %s",
                mapped_lid,
                remote_jid,
            )
            return mapped_lid
        return remote_jid

    def _reset_expanded_window(self) -> None:
        """Volta a lista ao messages_page_size normal.

        A janela expandida pertence à conversa em que o usuário pediu o
        histórico; abrir outra (ou fechar esta) tem de recomeçar do limite
        configurado, senão o chat seguinte já nasce renderizando milhares de
        linhas por causa de uma conversa anterior.
        """
        self._expanded_visible_count = 0
        self._expanded_oldest_msg_id = ""

    def _refresh_expanded_window_before_rebuild(self) -> None:
        """Registra de novo, logo antes do rebuild, a janela que está na tela.

        O registro no fim de populate_messages() não basta sozinho, porque a
        lista muda entre dois rebuilds por caminhos que não registram nada: os
        appends ao vivo (on_incoming_message(), o envio otimista) aumentam a
        contagem, e remove_messages_by_id() pode tirar exatamente a mensagem que
        era a âncora. Juntos, os dois produzem o corte: a âncora some, a
        janela cai para o piso por contagem, e esse piso é o da abertura (200),
        não o da tela (201, 204...). Medido em 2026-09-16: 200 -> 201 linhas
        com uma mensagem nova, a mensagem mais antiga removida pelo espelhamento
        de apagadas, e o rebuild seguinte de volta a 200 com o offset andando.

        Só vale para a conversa que já foi pintada: depois de
        _reset_expanded_window() (troca ou fechamento de conversa) a contagem é
        0 e _sorted_messages ainda pode ser da conversa anterior, então não se
        registra nada e a abertura segue respeitando messages_page_size.
        """
        if getattr(self, "_expanded_visible_count", 0) <= 0:
            return
        self._remember_expanded_window()

    def _history_window_for_rebuild(self, displayable: list, limit: int) -> tuple:
        """(offset, sep_idx) da janela que populate_messages() vai reconstruir.

        Método próprio, e não duas linhas dentro do rebuild, porque é aqui que
        o histórico que o usuário carregou à mão sobrevive: a âncora diz até
        onde a janela tem de continuar aberta (ver _remember_expanded_window()).
        populate_messages() não é testável sem um wx.ListCtrl de verdade, então
        sem isto o passo que corrige o bug ficava sem teste nenhum — dava para
        apagar o argumento da âncora com a suíte inteira verde.
        """
        return history_window(
            displayable,
            getattr(self, "_expanded_oldest_msg_id", ""),
            getattr(self, "_expanded_visible_count", 0),
            limit,
            self._unread_sep_idx,
        )

    def _remember_expanded_window(self) -> None:
        """Registra até onde a lista está materializada agora.

        A contagem é o piso e o id da mensagem mais antiga exibida é a âncora:
        cada mensagem nova aumenta o total de exibíveis, e uma janela definida
        só por contagem andaria uma linha para frente a cada chegada, comendo
        de volta justamente o histórico que o usuário pediu. Quando esse id
        some (mensagem apagada remotamente), a contagem ainda segura a janela.

        Chamado por quem carrega histórico (Home) E pelo fim de
        populate_messages(): o piso não é "o que o Home trouxe", é "o que já
        está na tela". Sem a segunda chamada, uma conversa aberta e nunca
        expandida abre com messages_page_size linhas, cresce por append a cada
        mensagem que chega ou que o usuário manda (on_incoming_message() e os
        caminhos de envio otimista apendam sem cortar nada), e o primeiro
        rebuild de fundo recalcula a janela do fim e devolve a lista a
        exatamente o page size — apagando da lista, sob o leitor de tela, uma
        linha antiga por mensagem nova. Foi assim que o bug reapareceu depois
        da âncora: com 200 linhas na tela, 4 mensagens enviadas e o repaint
        seguinte, as 4 mais antigas sumiram.

        "O que está na tela" inclui o alargamento que paginated_window() faz
        por causa do separador de não lidas, e não só o histórico que o usuário
        puxou: um grupo com 4000 não lidas abre com 4001 linhas, e essas 4001
        viram o piso da sessão inteira — o separador ser descartado depois não
        as estreita. É de propósito. Apará-las é apagar da lista, sob o leitor
        de tela, exatamente as mensagens que ele acabou de ler, que é o bug.
        Quem for medir custo de rebuild em conversa nunca expandida precisa
        saber que essa janela larga é esperada, não defeito.

        Uma lista só com sentinela (o placeholder de "sem mensagens") não
        registra nada: um piso de 1 linha não significa nada e a âncora vazia
        não prenderia coisa alguma. Também não zera o que já estava gravado, e
        isso é escolha, não esquecimento: "Limpar conversa" esvazia records e
        cai aqui, mas um records momentaneamente vazio no meio de um resync
        cairia igual, e zerar ali derrubaria um piso legítimo — de novo linhas
        sumindo sob o leitor. O custo de manter é o oposto e é suportável: se o
        chat limpo voltar a encher na mesma sessão, o rebuild seguinte pinta
        uma janela mais larga que o page size até a conversa ser fechada.
        """
        oldest_id = ""
        found_real_row = False
        for msg in self._sorted_messages:
            if isinstance(msg, dict) and not self._is_separator(msg):
                found_real_row = True
                # A primeira linha COM id, não simplesmente a primeira linha:
                # populate_messages() mantém registros sem key.id, e parar no
                # primeiro deles deixava a âncora vazia para sempre naquela
                # conversa. Só o piso por contagem sobraria — e ele escorrega
                # uma linha para frente a cada mensagem que chega ao vivo
                # (on_incoming_message() não registra a janela), reintroduzindo
                # em silêncio o mesmo sintoma que esta janela existe para
                # corrigir. Pegar uma linha mais nova como âncora só pode
                # alargar a janela, nunca estreitá-la: expanded_min_visible()
                # aplica max(piso, âncora).
                mid = (msg.get("key") or {}).get("id", "") or ""
                if mid:
                    oldest_id = mid
                    break
        if not found_real_row:
            return
        self._expanded_visible_count = len(self._sorted_messages)
        self._expanded_oldest_msg_id = oldest_id

    def _merge_history_into_records(self, older_messages: list) -> None:
        """Guarda no chat aberto o histórico recém-carregado.

        O prepend feito por _load_older_messages()/_on_older_messages_loaded()
        só vive nas listas em memória do painel, mas populate_messages()
        reconstrói a conversa inteira a partir de
        conversation["messages"]["messages"]["records"] — então um rebuild de
        fundo redesenhava a conversa sem essas mensagens. É o mesmo merge que
        MainWindow.fetch_older_messages() faz do lado do servidor, repetido
        aqui porque self.conversation nem sempre é o mesmo dict que
        main_window.chats[jid] (o resync da conversa aberta troca o dict), e é
        idempotente: dedup por key.id, sem reordenar o que já existe
        (populate_messages() ordena por timestamp de qualquer forma).

        Só entra o que pertence mesmo à conversa aberta: a consulta local usa
        _history_storage_jid() (que pode ser um @lid) e o fallback do servidor
        reconsulta sob o JID alternativo, então um mapeamento @lid errado ou
        velho traz mensagens de outro contato. Antes elas sumiam no rebuild
        seguinte; guardadas nos records elas viram história permanente daquele
        contato — sync_chat_messages() recolhe os records locais sem filtrar.
        """
        if not self.conversation or not older_messages:
            return
        try:
            jid = self.conversation.get("remoteJid", "")
            own = [
                m for m in older_messages
                if isinstance(m, dict) and self.main_window._chat_jids_equivalent(
                    (m.get("key") or {}).get("remoteJid", ""), jid
                )
            ]
            if len(own) != len(older_messages):
                logging.warning(
                    "[_merge_history_into_records] %d de %d mensagens descartadas "
                    "por não pertencerem a %s",
                    len(older_messages) - len(own), len(older_messages), jid,
                )
            if not own:
                return
            container = self.conversation.setdefault("messages", {}).setdefault(
                "messages", {}
            )
            records = container.get("records") or []
            existing_ids = {
                (r.get("key") or {}).get("id")
                for r in records
                if isinstance(r, dict) and (r.get("key") or {}).get("id")
            }
            # Mensagem sem key.id fica de fora: um "" nos records faz
            # _signature_changed_ids() devolver None para sempre, e aí todo
            # repaint local (estrela, fixar) vira rebuild completo — sai mais
            # caro do que perder uma linha que nem dá para endereçar.
            new_records = [
                m for m in own
                if (m.get("key") or {}).get("id")
                and (m.get("key") or {}).get("id") not in existing_ids
            ]
            if not new_records:
                return
            container["records"] = new_records + records
            # "total" pode ser a contagem real do chat (db.get_message_count()),
            # maior do que o que está carregado, então aqui só sobe. Vale até o
            # próximo sync_chat_messages(), que reescreve com len(all_messages).
            container["total"] = max(
                int(container.get("total") or 0), len(container["records"])
            )
            logging.info(
                "[_merge_history_into_records] %d mensagem(ns) antigas guardadas "
                "nos records (total agora %d)",
                len(new_records), container["total"],
            )
        except Exception:
            logging.exception(
                "[_merge_history_into_records] falha ao mesclar o histórico carregado"
            )

    def _load_older_messages(self):
        """Load older messages from the local database, or fall back to the server if none remain locally."""
        if not self.conversation or not self._all_sorted_messages:
            logging.info(f"[_load_older_messages] Aborting: conversation={self.conversation is not None}, all_sorted={len(self._all_sorted_messages) if self._all_sorted_messages else 0}")
            return

        self._is_loading_more = True
        try:
            remote_jid = self.conversation.get("remoteJid", "")
            limit = int(
                self.main_window.settings.get("user_interface", {}).get("messages_page_size", 200)
            )
            # Count separator objects to get the actual database message count currently in memory.
            loaded_db_count = sum(1 for m in self._all_sorted_messages if not self._is_separator(m))
            storage_jid = self._history_storage_jid(remote_jid)
            logging.info(f"[_load_older_messages] Querying local DB for {storage_jid} with count={loaded_db_count}")
            
            # Fetch from local DB
            local_msgs = self.main_window.db.get_messages(storage_jid, limit=limit, offset=loaded_db_count)
            logging.info(f"[_load_older_messages] Local DB returned {len(local_msgs) if local_msgs else 0} messages")
            
            if local_msgs:
                # We found older messages in the local DB!
                # Reverse them so they are in ascending chronological order (older first)
                local_msgs.reverse()
                displayable = [m for m in local_msgs if self._is_displayable_message(m)]
                logging.info(f"[_load_older_messages] Displayable local messages count: {len(displayable)}")
                if displayable:
                    oldest_in_mem = self._all_sorted_messages[0] if self._all_sorted_messages else None
                    oldest_in_mem_id = oldest_in_mem.get("key", {}).get("id") if oldest_in_mem else "None"
                    oldest_in_mem_ts = oldest_in_mem.get("timestamp") if oldest_in_mem else "None"
                    logging.info(f"[_load_older_messages] Oldest in memory: id={oldest_in_mem_id}, ts={oldest_in_mem_ts}")
                    logging.info(f"[_load_older_messages] Returned oldest from DB: id={displayable[0].get('key', {}).get('id')}, ts={displayable[0].get('timestamp')}")
                    logging.info(f"[_load_older_messages] Returned newest from DB: id={displayable[-1].get('key', {}).get('id')}, ts={displayable[-1].get('timestamp')}")
                    self.messages_list.Freeze()
                    try:
                        _old_rows = list(self._sorted_messages)
                        old_count = len(self._sorted_messages)
                        self._all_sorted_messages = self._deduplicate_messages(displayable + self._all_sorted_messages)
                        self._sorted_messages     = self._deduplicate_messages(displayable + self._sorted_messages)
                        self._messages_offset     = 0
                        n_new = len(self._sorted_messages) - old_count
                        logging.info(f"[_load_older_messages] Prepend finished. Added {n_new} new unique messages. Rebuilding UI list.")
                        
                        if n_new > 0:
                            self._merge_history_into_records(displayable)
                            self._remember_expanded_window()
                            self._recompute_unread_sep_idx()
                                
                            self._sync_message_rows(_old_rows, self._sorted_messages)

                            self.messages_list.Focus(n_new)
                            self.messages_list.Select(n_new, True)
                            self.messages_list.EnsureVisible(n_new)
                            self._is_loading_more = False
                            return
                        else:
                            logging.info("[_load_older_messages] No new unique messages found in local DB chunk. Falling through to server fetch.")
                    finally:
                        self.messages_list.Thaw()
            
            # No older messages in local DB, fetch from server
            self._load_older_messages_from_server()
        except Exception as e:
            logging.exception(f"[_load_older_messages] error: {e}")
            self._is_loading_more = False

    def _load_older_messages_from_server(self):
        """Fetch older messages from server when the beginning of local history is reached."""
        if not self.conversation or not self._all_sorted_messages:
            logging.info(f"[_load_older_messages_from_server] Aborting: conversation={self.conversation is not None}, all_sorted={len(self._all_sorted_messages) if self._all_sorted_messages else 0}")
            self._is_loading_more = False
            return
        
        phone_jid = self.conversation.get("remoteJid", "")
        reached_start = phone_jid in getattr(self, "_reached_server_start", {})
        logging.info(f"[_load_older_messages_from_server] phone_jid={phone_jid}, reached_start={reached_start}")
        if phone_jid and reached_start:
            self._is_loading_more = False
            return
        
        # A duplicate-only page still advances the server cursor. Prefer that
        # cursor on the next attempt so we do not request the same 200 rows.
        oldest_msg = self._server_history_anchor.get(phone_jid)
        if oldest_msg is None:
            # Get oldest non-separator and non-pending message ID
            for m in self._all_sorted_messages:
                if m.get("_type") == "unread_separator":
                    continue
                m_id = m.get("key", {}).get("id", "")
                # Skip local pending/virtual messages (UUIDs contain hyphens or start with 'pending-')
                if m.get("_local_pending") or m_id.startswith("pending-") or "-" in m_id:
                    continue
                oldest_msg = m
                break

        if oldest_msg is None:
            # Fallback to the first message if all are pending/separators
            oldest_msg = self._all_sorted_messages[0]

        oldest_id = oldest_msg.get("key", {}).get("id", "")
        logging.info(f"[_load_older_messages_from_server] oldest_id={oldest_id}")
        if not oldest_id:
            self._is_loading_more = False
            return

        self._is_loading_more = True
        
        def _fetch():
            phone_jid_val = self.conversation.get("remoteJid", "") if self.conversation else ""
            try:
                logging.info(f"[_load_older_messages_from_server thread] Launching fetch_older_messages for {phone_jid_val}")
                fetched = self.main_window.fetch_older_messages(phone_jid_val, oldest_msg)
                logging.info(f"[_load_older_messages_from_server thread] fetch_older_messages returned {len(fetched) if fetched is not None else 'None'}")
                if fetched is None:
                    fetched = self.main_window.wait_for_older_messages(
                        phone_jid_val,
                        oldest_msg,
                        should_continue=lambda: bool(
                            self.conversation
                            and self.conversation.get("remoteJid") == phone_jid_val
                        ),
                    )
                    logging.info(
                        "[_load_older_messages_from_server thread] "
                        "wait_for_older_messages returned %s",
                        len(fetched) if fetched is not None else "None")
                if fetched is not None:
                    if fetched:
                        wx.CallAfter(self._on_older_messages_loaded, fetched, phone_jid_val, oldest_msg)
                    else:
                        wx.CallAfter(self._set_reached_start, phone_jid_val)
                else:
                    wx.CallAfter(self._clear_loading_more, phone_jid_val)
            except Exception as e:
                logging.exception(f"[_load_older_messages_from_server] thread error: {e}")
                wx.CallAfter(self._clear_loading_more, phone_jid_val)

        threading.Thread(target=_fetch, daemon=True).start()

    def _set_reached_start(self, requested_jid):
        if not self.conversation or self.conversation.get("remoteJid") != requested_jid:
            logging.info(f"[_set_reached_start] Ignoring call for {requested_jid} (current active: {self.conversation.get('remoteJid') if self.conversation else 'None'})")
            return
        self._reached_server_start[requested_jid] = True
        self._is_loading_more = False
        # WhatsApp Web shows a banner at this point; without it the top of a
        # long conversation is indistinguishable from its real beginning.
        if history_boundary.is_only_on_phone(self.main_window, requested_jid):
            self.main_window.speak_output.output(
                self.main_window.i18n.t("older_messages_on_phone"))

    def _clear_loading_more(self, requested_jid):
        if not self.conversation or self.conversation.get("remoteJid") != requested_jid:
            logging.info(f"[_clear_loading_more] Ignoring call for {requested_jid} (current active: {self.conversation.get('remoteJid') if self.conversation else 'None'})")
            return
        self._is_loading_more = False

    def _on_older_messages_loaded(self, fetched_messages, requested_jid, requested_anchor=None):
        """Prepend fetched history to UI message list."""
        if not self.conversation or self.conversation.get("remoteJid") != requested_jid:
            logging.info(f"[_on_older_messages_loaded] Ignoring fetched messages for {requested_jid} (current active: {self.conversation.get('remoteJid') if self.conversation else 'None'})")
            return
        self._is_loading_more = False
        if not fetched_messages:
            return
            
        # Reverse to oldest-first order (ascending chronological) to match client storage
        fetched_messages.reverse()
            
        displayable = [
            m for m in fetched_messages if self._is_displayable_message(m)
        ]
        if not displayable:
            return
            
        # Sort displayable older messages
        try:
            displayable = sorted(
                displayable, key=lambda m: self._extract_timestamp(m) or 0
            )
        except Exception:
            pass
            
        self.messages_list.Freeze()
        try:
            _old_rows = list(self._sorted_messages)
            old_count = len(self._sorted_messages)
            self._all_sorted_messages = self._deduplicate_messages(displayable + self._all_sorted_messages)
            self._sorted_messages     = self._deduplicate_messages(displayable + self._sorted_messages)
            self._messages_offset     = 0
            n_new = len(self._sorted_messages) - old_count
            logging.info(f"[_on_older_messages_loaded] n_new={n_new}, displayable_count={len(displayable)}, total={len(self._sorted_messages)}")
            
            if n_new == 0:
                # Overlap is not proof of the beginning. LID/phone aliases can
                # make a valid older page look entirely duplicated locally.
                # Advance to the oldest row returned and keep the chat retryable.
                phone_jid_val = self.conversation.get("remoteJid", "") if self.conversation else ""
                next_anchor = displayable[0]
                previous_id = (requested_anchor or {}).get("key", {}).get("id", "")
                next_id = next_anchor.get("key", {}).get("id", "")
                if phone_jid_val and next_id and next_id != previous_id:
                    self._server_history_anchor[phone_jid_val] = next_anchor
                    logging.info(
                        "[_on_older_messages_loaded] Duplicate page advanced anchor %s -> %s; keeping history retryable",
                        previous_id,
                        next_id,
                    )
                else:
                    logging.warning(
                        "[_on_older_messages_loaded] Duplicate page did not advance anchor for %s; not treating overlap as server start",
                        phone_jid_val,
                    )
                return

            self._server_history_anchor.pop(requested_jid, None)

            self._merge_history_into_records(displayable)
            self._remember_expanded_window()
            
            self._recompute_unread_sep_idx()

            self._sync_message_rows(_old_rows, self._sorted_messages)

            self.messages_list.Focus(n_new)
            self.messages_list.Select(n_new, True)
            self.messages_list.EnsureVisible(n_new)
        finally:
            self.messages_list.Thaw()


    def _load_more_messages(self):
        """Prepend the previous page of messages to the list."""
        self._is_loading_more = True
        self.messages_list.Freeze()
        try:
            limit = int(
                self.main_window.settings.get("user_interface", {}).get("messages_page_size", 200)
            )
            new_start = max(0, self._messages_offset - limit)
            new_msgs  = self._all_sorted_messages[new_start:self._messages_offset]
            if not new_msgs:
                return

            n_new = len(new_msgs)

            # Extend the in-memory list and update the offset
            _old_rows = list(self._sorted_messages)
            self._sorted_messages   = new_msgs + self._sorted_messages
            self._messages_offset   = new_start
            self._remember_expanded_window()
            if self._unread_sep_idx >= 0:
                self._unread_sep_idx += n_new

            # Insert only the older page above what is already listed.
            self._sync_message_rows(_old_rows, self._sorted_messages)

            # Keep the previously-first item in view (now at index n_new)
            self.messages_list.Focus(n_new)
            self.messages_list.Select(n_new, True)
            self.messages_list.EnsureVisible(n_new)
        finally:
            self.messages_list.Thaw()
            self._is_loading_more = False
