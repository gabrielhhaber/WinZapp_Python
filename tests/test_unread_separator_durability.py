"""O separador de não lidas (alvo do Alt+3 / Alt+U) contra os dois sintomas
relatados ao vivo, ambos ligados a minimizar o app com a conversa aberta:

1. "1 mensagem não lida" no preview da lista e NENHUM separador dentro da
   conversa — e, pior, a conversa continuava não lida no celular. O caminho ao
   vivo (``on_incoming_message()``) escrevia apenas ``_sorted_messages`` e
   ``_unread_sep_idx``, nunca ``_first_unread_msg_id``/``_first_unread_count``,
   que é o único par que ``populate_messages()`` lê para recriar o separador
   depois do seu ``DeleteAllItems()``. O primeiro rebuild (vários por minuto
   numa conversa aberta) apagava o separador e não o recriava; sem separador
   ``_unread_sep_idx`` é -1, então ``_on_message_focused()`` nunca disparava
   ``mark_conversation_as_read()``.
2. "O separador diz 1 e há duas mensagens abaixo dele": o rebuild recriava o
   separador na âncora ANTIGA com a contagem antiga, e ainda marcava o
   separador restaurado como "ancora posição já lida", o que fazia a mensagem
   ao vivo seguinte movê-lo e reiniciar a contagem em 1.

Regra do comportamento esperado, dita pelo usuário: enquanto o foco NÃO passou
pelo separador, mensagens novas somam nele e ele não se move; depois que o foco
passa (mark-as-read), ele continua visível com o valor antigo e só a próxima
mensagem nova o apaga e recomeça em 1.

``ConversationsPanel`` é um wx.Panel e não pode ser instanciado sem um wx.App,
então os métodos são exercidos como funções contra um stub que carrega só os
atributos que eles tocam — mesmo padrão de tests/test_message_bookmarks.py.
"""

import pytest

from ui.conversations import ConversationsPanel


class _FakeList:
    """wx.ListCtrl de mentira: guarda os textos das linhas e a linha focada,
    que é toda a superfície do controle que os métodos sob teste tocam."""

    def __init__(self):
        self.rows = []
        self.focused = -1

    def Freeze(self):
        pass

    def Thaw(self):
        pass

    def GetFocusedItem(self):
        return self.focused

    def Focus(self, idx):
        self.focused = idx

    def InsertItem(self, idx, text):
        self.rows.insert(idx, text)

    def DeleteItem(self, idx):
        self.rows.pop(idx)

    def SetItemText(self, idx, text):
        self.rows[idx] = text

    def Append(self, cols):
        self.rows.append(cols[0] if isinstance(cols, (list, tuple)) else cols)

    def GetItemCount(self):
        return len(self.rows)


class _Stub:
    """Mínimo de ConversationsPanel para a manutenção do separador."""

    def __init__(self):
        self.messages_list = _FakeList()
        self._sorted_messages = []
        self._unread_sep_idx = -1
        self._pending_open_unread = 0
        self._sep_anchors_read_position = False
        self._unread_sep_marked_read = False
        self._first_unread_msg_id = None
        self._first_unread_count = 0
        self._messages_signature_cache = None

    _is_separator = ConversationsPanel._is_separator
    _counts_toward_unread_separator = ConversationsPanel._counts_toward_unread_separator
    _update_unread_separator_for_incoming = (
        ConversationsPanel._update_unread_separator_for_incoming
    )
    _anchor_below_unread_separator = ConversationsPanel._anchor_below_unread_separator
    _place_unread_separator_for_rebuild = (
        ConversationsPanel._place_unread_separator_for_rebuild
    )

    def _render_message_line(self, msg):
        if isinstance(msg, dict) and msg.get("_type") == "unread_separator":
            return "--- %d nao lida(s) ---" % msg.get("count", 0)
        return msg.get("key", {}).get("id", "")


def _msg(msg_id, from_me=False, msg_type="conversation"):
    return {
        "key": {"remoteJid": "551199@s.whatsapp.net", "fromMe": from_me, "id": msg_id},
        "message": {"conversation": "oi"},
        "messageType": msg_type,
        "messageTimestamp": 1700000000,
    }


def _deliver(panel, msg):
    """Os mesmos dois passos que ``on_incoming_message()`` executa dentro do
    seu Freeze()/Thaw(): o portão de contabilidade e o append na cauda."""
    if not (msg.get("key") or {}).get("fromMe") and panel._counts_toward_unread_separator(msg):
        panel._update_unread_separator_for_incoming(msg)
    panel._sorted_messages.append(msg)
    panel.messages_list.Append((panel._render_message_line(msg),))


def _rebuild(panel):
    """O que ``populate_messages()`` faz com o separador: descarta a lista
    inteira e a reconstrói só a partir dos registros reais mais o par
    âncora/contagem."""
    displayable = [m for m in panel._sorted_messages if not panel._is_separator(m)]
    panel._unread_sep_idx = -1
    panel._sorted_messages = panel._place_unread_separator_for_rebuild(displayable)
    panel.messages_list.rows = [
        panel._render_message_line(m) for m in panel._sorted_messages
    ]


def _separator(panel):
    for m in panel._sorted_messages:
        if isinstance(m, dict) and m.get("_type") == "unread_separator":
            return m
    return None


class TestLiveSeparatorSurvivesRebuild:
    def test_live_separator_survives_a_rebuild(self):
        """Sintoma 1: separador inserido ao vivo desaparecia no rebuild."""
        panel = _Stub()
        panel._sorted_messages = [_msg("old1"), _msg("old2", from_me=True)]
        panel.messages_list.rows = ["old1", "old2"]

        _deliver(panel, _msg("new1"))
        assert _separator(panel) == {"_type": "unread_separator", "count": 1}
        assert panel._first_unread_msg_id == "new1"
        assert panel._first_unread_count == 1

        _rebuild(panel)
        sep = _separator(panel)
        assert sep is not None, "o rebuild apagou o separador ao vivo"
        assert sep["count"] == 1
        assert panel._unread_sep_idx == 2
        assert panel._sorted_messages[3]["key"]["id"] == "new1"

    def test_two_live_messages_count_two_and_survive_the_rebuild(self):
        """Sintoma 2: contagem tem de acompanhar as mensagens abaixo dela."""
        panel = _Stub()
        panel._sorted_messages = [_msg("old1")]
        panel.messages_list.rows = ["old1"]

        _deliver(panel, _msg("new1"))
        _deliver(panel, _msg("new2"))

        sep = _separator(panel)
        assert sep["count"] == 2
        assert panel._first_unread_msg_id == "new1"
        assert panel._first_unread_count == 2

        _rebuild(panel)
        sep = _separator(panel)
        assert sep["count"] == 2
        below = [
            m["key"]["id"]
            for m in panel._sorted_messages[panel._unread_sep_idx + 1:]
        ]
        assert below == ["new1", "new2"]

    def test_live_message_does_not_move_the_separator_placed_on_open(self):
        """Conversa aberta com não lidas antigas: a mensagem nova SOMA no
        separador existente em vez de movê-lo e reiniciar em 1 — as mensagens
        abaixo dele ainda não foram lidas."""
        panel = _Stub()
        opened = [_msg("old1"), _msg("unread1")]
        panel._pending_open_unread = 1
        panel._sorted_messages = panel._place_unread_separator_for_rebuild(opened)
        panel.messages_list.rows = [
            panel._render_message_line(m) for m in panel._sorted_messages
        ]
        assert panel._unread_sep_idx == 1
        assert panel._sep_anchors_read_position is False

        _deliver(panel, _msg("new1"))

        sep = _separator(panel)
        assert sep["count"] == 2
        assert panel._unread_sep_idx == 1, "o separador não pode ter se movido"
        assert panel._first_unread_msg_id == "unread1"
        assert panel._first_unread_count == 2

        _rebuild(panel)
        assert _separator(panel)["count"] == 2
        assert panel._unread_sep_idx == 1

    def test_rebuild_preserves_the_read_anchor_flag(self):
        """(b) O rebuild não pode declarar "já lido" um separador que está
        apenas restaurando: isso reintroduzia o sintoma 2 por outro caminho."""
        panel = _Stub()
        panel._sorted_messages = [_msg("old1")]
        _deliver(panel, _msg("new1"))
        assert panel._sep_anchors_read_position is False

        _rebuild(panel)
        assert panel._sep_anchors_read_position is False

        _deliver(panel, _msg("new2"))
        assert _separator(panel)["count"] == 2


class TestFocusPassingTheSeparator:
    def test_next_live_message_restarts_at_one_after_focus_passed(self):
        """Depois do mark-as-read (foco passou pelo separador), o separador
        antigo continua visível com o valor antigo, e só a PRÓXIMA mensagem
        nova o apaga e recomeça em 1, reancorada nela."""
        panel = _Stub()
        panel._sorted_messages = [_msg("old1")]
        _deliver(panel, _msg("new1"))
        _deliver(panel, _msg("new2"))
        assert _separator(panel)["count"] == 2

        # O que _on_message_focused() faz quando o foco alcança/passa o
        # separador: marca lido uma vez e anota que ele ancora posição lida.
        panel._unread_sep_marked_read = True
        panel._sep_anchors_read_position = True

        # O separador continua na lista, com o valor antigo, até aqui.
        _rebuild(panel)
        assert _separator(panel)["count"] == 2
        assert panel._sep_anchors_read_position is True

        _deliver(panel, _msg("new3"))
        sep = _separator(panel)
        assert sep["count"] == 1
        assert panel._first_unread_msg_id == "new3"
        assert panel._first_unread_count == 1
        assert panel._sep_anchors_read_position is False
        assert panel._unread_sep_marked_read is False
        # Uma única linha de separador, imediatamente acima de new3.
        assert sum(1 for m in panel._sorted_messages if panel._is_separator(m)) == 1
        assert panel._sorted_messages[-1]["key"]["id"] == "new3"
        assert panel._unread_sep_idx == len(panel._sorted_messages) - 2

        _rebuild(panel)
        assert _separator(panel)["count"] == 1


class TestSystemEventsDoNotTouchTheSeparator:
    @pytest.mark.parametrize("msg_type", ["groupNotification", "protocolMessage"])
    def test_system_event_is_not_countable(self, msg_type):
        """(d) main.py só sobe o unreadCount para is_countable_message(); o
        separador tem de usar o mesmo teste, ou os dois números divergem."""
        panel = _Stub()
        assert panel._counts_toward_unread_separator(_msg("sys1", msg_type=msg_type)) is False
        assert panel._counts_toward_unread_separator(_msg("real1")) is True

    def test_system_event_does_not_create_or_bump_the_separator(self):
        panel = _Stub()
        panel._sorted_messages = [_msg("old1")]
        panel.messages_list.rows = ["old1"]

        _deliver(panel, _msg("sys1", msg_type="groupNotification"))
        assert _separator(panel) is None
        assert panel._unread_sep_idx == -1
        assert panel._first_unread_msg_id is None

        _deliver(panel, _msg("new1"))
        assert _separator(panel)["count"] == 1

        _deliver(panel, _msg("sys2", msg_type="protocolMessage"))
        assert _separator(panel)["count"] == 1, "evento de sistema subiu o separador"
        assert panel._first_unread_count == 1

    def test_own_message_never_touches_the_separator(self):
        panel = _Stub()
        panel._sorted_messages = [_msg("old1")]
        _deliver(panel, _msg("mine", from_me=True))
        assert _separator(panel) is None


class TestSeparatorStateEdges:
    def test_stale_separator_index_is_treated_as_no_separator(self):
        """_unread_sep_idx pode ficar fora de faixa quando a lista é esvaziada
        por baixo dele ("Limpar conversa"); isso já estourou ao vivo com
        "IndexError: pop from empty list"."""
        panel = _Stub()
        panel._unread_sep_idx = 7
        panel._sep_anchors_read_position = True

        _deliver(panel, _msg("new1"))

        assert panel._unread_sep_idx == 0
        assert _separator(panel)["count"] == 1
        assert panel._first_unread_msg_id == "new1"

    def test_rebuild_drops_the_separator_when_its_anchor_is_gone(self):
        """Âncora apagada (mensagem removida remotamente): nada de separador
        fantasma, e o estado volta para "sem separador"."""
        panel = _Stub()
        _deliver(panel, _msg("new1"))
        panel._sorted_messages = [m for m in panel._sorted_messages
                                  if panel._is_separator(m) or m["key"]["id"] != "new1"]
        _rebuild(panel)
        assert _separator(panel) is None
        assert panel._unread_sep_idx == -1

    def test_signature_cache_keeps_the_tail_append_shortcut_usable(self):
        """A âncora entra em _messages_signature() para forçar um rebuild
        quando o separador se move. Quem o moveu aqui foi o próprio caminho ao
        vivo, na tela e em _sorted_messages ao mesmo tempo — sem alinhar o
        cache, toda mensagem nova custaria um rebuild inteiro."""
        panel = _Stub()
        panel._messages_signature_cache = ("551199@s.whatsapp.net", None, 0, ())
        _deliver(panel, _msg("new1"))
        assert panel._messages_signature_cache == (
            "551199@s.whatsapp.net", "new1", 0, (),
        )


class _FakeEvent:
    def __init__(self, idx):
        self._idx = idx
        self.skipped = False

    def GetIndex(self):
        return self._idx

    def Skip(self):
        self.skipped = True


class _FocusMainWindow:
    def __init__(self):
        self.marked_read = []

    def mark_conversation_as_read(self, jid):
        self.marked_read.append(jid)


class _FocusStub(_Stub):
    """_Stub mais a superfície que _on_message_focused() toca — separado para
    a base continuar carregando só o que a manutenção do separador usa."""

    _on_message_focused = ConversationsPanel._on_message_focused
    _should_dismiss_unread_separator = staticmethod(
        ConversationsPanel._should_dismiss_unread_separator
    )

    def __init__(self):
        super().__init__()
        self.main_window = _FocusMainWindow()
        self.conversation = {"remoteJid": "551199@s.whatsapp.net"}
        self.selected_messages = set()
        self.selection_sound = None
        self._current_audio_id = None
        self._audio_stream = None
        self._is_loading_more = True   # desliga o page-load no índice 0
        self._messages_offset = 0
        self._read_more_calls = []

    def _update_read_more_button(self, idx):
        self._read_more_calls.append(idx)

    def _update_reactions_button(self, idx):
        pass


class TestFocusOnlyArmsTheResetWhenItStepsPast:
    """Bloqueante do review: sob a semântica nova o flag é a ÚNICA coisa que
    escolhe entre "soma" e "move e reinicia em 1", então ligá-lo ao POUSAR no
    separador (idx >= sep_idx, o critério do mark-as-read) é frouxo demais.
    Alt+3 / Alt+U pousam ali de propósito, e populate_messages() estaciona o
    foco ali na abertura: o usuário que apenas se situa veria o separador de
    3 virar 1 na mensagem seguinte."""

    def _panel_with_separator(self):
        panel = _FocusStub()
        panel._sorted_messages = [_msg("old1")]
        _deliver(panel, _msg("new1"))
        _deliver(panel, _msg("new2"))
        assert _separator(panel)["count"] == 2
        return panel

    def test_landing_on_the_separator_does_not_arm_the_reset(self):
        panel = self._panel_with_separator()
        panel._on_message_focused(_FakeEvent(panel._unread_sep_idx))

        assert panel._sep_anchors_read_position is False
        # Marcar como lida ao alcançar o separador continua certo.
        assert panel.main_window.marked_read == ["551199@s.whatsapp.net"]

        # E a próxima mensagem soma, em vez de reiniciar em 1.
        _deliver(panel, _msg("new3"))
        assert _separator(panel)["count"] == 3

    def test_stepping_past_the_separator_arms_the_reset(self):
        panel = self._panel_with_separator()
        panel._on_message_focused(_FakeEvent(panel._unread_sep_idx + 1))

        assert panel._sep_anchors_read_position is True

        _deliver(panel, _msg("new3"))
        assert _separator(panel)["count"] == 1
        assert panel._first_unread_msg_id == "new3"

    def test_focus_above_the_separator_arms_nothing(self):
        panel = self._panel_with_separator()
        panel._on_message_focused(_FakeEvent(0))

        assert panel._sep_anchors_read_position is False
        assert panel.main_window.marked_read == []


class TestSendingDismissesTheSeparator:
    """Item 3 do review: _dismiss_unread_separator() era o único ponto do
    caminho de envio que largava a âncora, e ela deixou de ser volátil."""

    class _SendStub(_Stub):
        _register_virtual_msg = ConversationsPanel._register_virtual_msg
        _dismiss_unread_separator = ConversationsPanel._dismiss_unread_separator

        def __init__(self):
            super().__init__()
            # get_chat devolvendo None faz _register_virtual_msg() retornar
            # logo depois da parte que interessa aqui (largar o separador).
            self.main_window = type("_MW", (), {"get_chat": lambda self, jid: None})()

    def test_sending_clears_a_visible_separator(self):
        panel = self._SendStub()
        panel._sorted_messages = [_msg("old1")]
        _deliver(panel, _msg("new1"))
        assert panel._unread_sep_idx >= 0

        panel._register_virtual_msg(
            {"key": {"remoteJid": "551199@s.whatsapp.net", "fromMe": True, "id": "v1"}}
        )

        assert _separator(panel) is None
        assert panel._unread_sep_idx == -1
        assert panel._first_unread_msg_id is None
        assert panel._first_unread_count == 0

    def test_sending_clears_an_anchor_left_without_a_visible_row(self):
        """O caso que o if escondia: âncora gravada e _unread_sep_idx == -1
        (o rebuild não achou a linha). Sem largar a âncora aqui, o rebuild
        seguinte ressuscitava o separador ACIMA da mensagem recém-enviada."""
        panel = self._SendStub()
        panel._sorted_messages = [_msg("old1"), _msg("new1")]
        panel._unread_sep_idx = -1
        panel._first_unread_msg_id = "new1"
        panel._first_unread_count = 1

        panel._register_virtual_msg(
            {"key": {"remoteJid": "551199@s.whatsapp.net", "fromMe": True, "id": "v1"}}
        )

        assert panel._first_unread_msg_id is None
        assert panel._first_unread_count == 0
        _rebuild(panel)
        assert _separator(panel) is None


class TestAnchorIsRecomputedOnEveryIncrement:
    """Item 4 do review: a mensagem-âncora pode ser apagada remotamente
    (_delete_message_rows() ajusta _unread_sep_idx mas não a âncora). Se o
    ramo de soma não recalculasse, a âncora ficaria apontando para um id que
    não existe mais em records, o alinhamento do cache de assinatura passaria
    a valer para esse id morto, e a tela deixaria de ser a que o rebuild
    daquele instante produziria."""

    def test_increment_recovers_from_a_deleted_anchor(self):
        panel = _Stub()
        panel._sorted_messages = [_msg("old1")]
        _deliver(panel, _msg("new1"))
        assert panel._first_unread_msg_id == "new1"

        # A mensagem-âncora some (revogada para todos), como em
        # _delete_message_rows(): a linha sai e o índice do separador é
        # ajustado, mas nada toca na âncora.
        sep_idx = panel._unread_sep_idx
        panel._sorted_messages = [m for m in panel._sorted_messages
                                  if panel._is_separator(m) or m["key"]["id"] != "new1"]
        panel.messages_list.rows = [
            panel._render_message_line(m) for m in panel._sorted_messages
        ]
        panel._unread_sep_idx = sep_idx

        _deliver(panel, _msg("new2"))

        assert panel._first_unread_msg_id == "new2", "âncora morta não foi recalculada"
        assert panel._messages_signature_cache is None
        _rebuild(panel)
        sep = _separator(panel)
        assert sep is not None, "o rebuild descartou o separador que a tela mostrava"
        assert sep["count"] == 2
