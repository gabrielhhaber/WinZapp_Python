"""``is_chat_archived()`` tem de dar a MESMA resposta que o construtor da lista.

Relatado ao vivo: o programa anunciava "Nova mensagem de X" com a janela
aberta para uma conversa que estava na aba Arquivadas. O guard já existia —
``on_new_message()`` faz ``if archived and not is_current_conv: return`` — e a
proteção do toast em segundo plano também. O que falhava era a resposta: a aba
Arquivadas decide por ``arch_flag if arch_flag is not None else (chave in
_archived_chats)``, usando a CHAVE de ``self.chats``, enquanto
``is_chat_archived()`` decidia a partir da string de JID que recebia,
normalizada. As duas divergem em dois casos alcançáveis:

1. ``normalize_chats()`` põe no conjunto a chave exatamente como a encontrou,
   e ``_set_archived_state()`` põe a forma normalizada. Uma conversa ainda
   chaveada por ``@c.us`` (ou com sufixo de dispositivo ``:N``) fica no
   conjunto sob uma string que ``_normalize_jid()`` reescreve, então a busca
   normalizada nunca a achava.
2. A chave de uma conversa e o ``chat["remoteJid"]`` dela nem sempre são a
   mesma string (uma conversa mesclada/renomeada no lugar mantém o dict que já
   tinha). A aba Arquivadas decide pela chave; a mensagem chega resolvida para
   o remoteJid, e a busca por essa string não achava registro nenhum.

``MainWindow`` é um wx.Frame e não pode ser instanciado sem um wx.App, então os
métodos são exercidos como funções contra um stub que carrega só os atributos
que eles tocam — mesmo padrão de tests/test_sender_names.py.
"""

import pytest

from main import MainWindow


class _Stub:
    """Stub mínimo de MainWindow para a decisão de arquivamento."""

    _normalize_jid = staticmethod(MainWindow._normalize_jid)
    _chat_archive_flag = staticmethod(MainWindow._chat_archive_flag)
    _archived_lookup_jids = MainWindow._archived_lookup_jids
    _chat_entry_for_archive = MainWindow._chat_entry_for_archive
    is_chat_archived = MainWindow.is_chat_archived

    def __init__(self, chats=None, archived=(), lid_to_phone=None, phone_to_lid=None):
        self.chats = dict(chats or {})
        self._archived_chats = set(archived)
        self._lid_to_phone = dict(lid_to_phone or {})
        self._phone_to_lid = dict(phone_to_lid or {})


def _chat(remote_jid, **extra):
    record = {"remoteJid": remote_jid}
    record.update(extra)
    return record


def _list_builder_says_archived(stub, key):
    """A regra literal de _compute_chat_lists(), para comparar as duas
    respostas em vez de afirmar só uma delas."""
    flag = stub._chat_archive_flag(stub.chats[key])
    return flag if flag is not None else (key in stub._archived_chats)


PHONE = "5551999990000@s.whatsapp.net"
LEGACY = "5551999990000@c.us"
LID = "77778888@lid"


class TestTheTwoAnswersAgree:
    """Cada caso monta um estado alcançável, pergunta às duas rotas e exige a
    mesma resposta. É a invariante; os testes abaixo prendem os casos
    específicos que a quebravam."""

    @pytest.mark.parametrize(
        "key, archived_entry",
        [
            (PHONE, PHONE),      # caminho comum
            (LEGACY, LEGACY),    # chave legada, conjunto escrito por normalize_chats()
            (LID, LID),          # @lid sem ponte
        ],
    )
    def test_the_notification_path_agrees_with_the_archived_tab(self, key, archived_entry):
        stub = _Stub(chats={key: _chat(key)}, archived={archived_entry})
        assert _list_builder_says_archived(stub, key) is True
        assert stub.is_chat_archived(key) is True


class TestLegacyKeyInTheArchivedSet:
    """Caso 1: o conjunto guarda a chave crua, a leitura normalizava."""

    def test_a_chat_keyed_by_c_us_is_archived_for_both(self):
        stub = _Stub(chats={LEGACY: _chat(LEGACY)}, archived={LEGACY})
        assert stub.is_chat_archived(LEGACY) is True

    def test_and_it_is_still_archived_when_asked_by_the_modern_jid(self):
        """É assim que a mensagem chega: on_new_message() normaliza antes."""
        stub = _Stub(chats={LEGACY: _chat(LEGACY)}, archived={LEGACY})
        assert stub.is_chat_archived(PHONE) is True

    def test_a_device_suffix_does_not_hide_the_archive_state_either(self):
        keyed = "5551999990000:12@c.us"
        stub = _Stub(chats={keyed: _chat(keyed)}, archived={keyed})
        assert stub.is_chat_archived(PHONE) is True


class TestKeyDiffersFromRemoteJid:
    """Caso 2: registro guardado sob uma chave, exibido pelo remoteJid."""

    def test_the_record_is_found_by_its_own_remote_jid(self):
        stub = _Stub(chats={LID: _chat(PHONE)}, archived={LID})
        # A aba Arquivadas mostra essa conversa (decide pela chave)...
        assert _list_builder_says_archived(stub, LID) is True
        # ...e a mensagem chega resolvida para o remoteJid.
        assert stub.is_chat_archived(PHONE) is True

    def test_a_stated_flag_on_that_record_still_wins(self):
        stub = _Stub(chats={LID: _chat(PHONE, archive=False)}, archived={LID})
        assert _list_builder_says_archived(stub, LID) is False
        assert stub.is_chat_archived(PHONE) is False


class TestPrecedenceIsUnchanged:
    """O que já funcionava tem de continuar igual — esta correção só pode
    tornar a leitura tão completa quanto a da lista, nunca mais agressiva:
    silenciar uma conversa NÃO arquivada seria pior que o bug relatado."""

    def test_an_explicit_false_on_the_record_beats_the_persisted_set(self):
        stub = _Stub(chats={PHONE: _chat(PHONE, archive=False)}, archived={PHONE})
        assert stub.is_chat_archived(PHONE) is False

    def test_an_explicit_true_on_the_record_needs_no_set_entry(self):
        stub = _Stub(chats={PHONE: _chat(PHONE, archive=True)})
        assert stub.is_chat_archived(PHONE) is True

    def test_the_archived_key_is_honoured_under_its_alternate_spelling(self):
        stub = _Stub(chats={PHONE: _chat(PHONE, archived=True)})
        assert stub.is_chat_archived(PHONE) is True

    def test_an_ordinary_chat_is_not_archived(self):
        stub = _Stub(chats={PHONE: _chat(PHONE)})
        assert stub.is_chat_archived(PHONE) is False

    def test_an_unknown_jid_is_not_archived(self):
        stub = _Stub(chats={PHONE: _chat(PHONE)}, archived={PHONE})
        assert stub.is_chat_archived("999@s.whatsapp.net") is False

    def test_an_empty_jid_is_not_archived(self):
        assert _Stub().is_chat_archived("") is False

    def test_a_chat_nothing_knows_about_is_not_archived(self):
        assert _Stub().is_chat_archived(PHONE) is False


class TestLidPhoneBridgeStillWorks:
    def test_archived_under_the_phone_jid_is_seen_from_the_lid(self):
        stub = _Stub(
            chats={PHONE: _chat(PHONE, archive=True)},
            lid_to_phone={LID: PHONE},
        )
        assert stub.is_chat_archived(LID) is True

    def test_archived_under_the_lid_is_seen_from_the_phone_jid(self):
        stub = _Stub(
            chats={LID: _chat(LID, archive=True)},
            phone_to_lid={PHONE: LID},
        )
        assert stub.is_chat_archived(PHONE) is True


class TestLookupCandidates:
    """A ordem é a ordem em que as respostas são consultadas, e a primeira
    definitiva vence — então ela faz parte do comportamento."""

    def test_normalized_comes_before_raw(self):
        stub = _Stub()
        assert stub._archived_lookup_jids(LEGACY) == [PHONE, LEGACY]

    def test_an_already_normalized_jid_is_not_listed_twice(self):
        stub = _Stub()
        assert stub._archived_lookup_jids(PHONE) == [PHONE]

    def test_the_alternate_jid_comes_after_the_chat_s_own(self):
        stub = _Stub(phone_to_lid={PHONE: LID})
        assert stub._archived_lookup_jids(PHONE) == [PHONE, LID]


class TestArchiveFlagParsing:
    """O tri-state é agora uma função só, usada pelas duas rotas."""

    @pytest.mark.parametrize("value", [True, "true", "True", 1])
    def test_truthy_forms_read_as_archived(self, value):
        assert MainWindow._chat_archive_flag({"archive": value}) is True

    @pytest.mark.parametrize("value", [False, "false", "False", 0])
    def test_falsy_forms_read_as_not_archived(self, value):
        assert MainWindow._chat_archive_flag({"archive": value}) is False

    def test_a_record_that_says_nothing_reads_as_unknown(self):
        assert MainWindow._chat_archive_flag({"remoteJid": PHONE}) is None

    def test_archived_is_consulted_when_archive_is_absent(self):
        assert MainWindow._chat_archive_flag({"archived": True}) is True

    def test_a_missing_record_reads_as_unknown(self):
        assert MainWindow._chat_archive_flag(None) is None
