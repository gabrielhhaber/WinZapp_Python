"""Correções das issues #84, #85, #86, #87, #88 e #91.

Cada bloco é uma issue. ``ConversationsPanel`` é um wx.Panel e não pode ser
instanciado sem um wx.App, então os métodos são exercidos como funções contra
um stub que carrega só os atributos que eles tocam — mesmo padrão de
tests/test_message_bookmarks.py e tests/test_unread_separator_durability.py.
"""

import importlib.util
import os
import sys
import tempfile

import pytest

from core.utils import contact_search_matches
from ui.conversations import ConversationsPanel


class _FakeList:
    """wx.ListCtrl de mentira, com só a superfície que os métodos tocam."""

    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.focused = -1
        self.selected = -1
        self.had_focus = False

    def GetItemCount(self):
        return len(self.rows)

    def Focus(self, idx):
        self.focused = idx

    def Select(self, idx, on=True):
        self.selected = idx

    def EnsureVisible(self, idx):
        pass

    def SetFocus(self):
        self.had_focus = True

    def GetItemText(self, idx, col=0):
        return self.rows[idx]


class _I18n:
    def t(self, key):
        return key


class _MW:
    def __init__(self):
        self.i18n = _I18n()
        self.spoken = []

    def output(self, text, interrupt=False):
        self.spoken.append(text)


class _Stub:
    """Stub mínimo de ConversationsPanel."""

    def __init__(self, conversation=None, messages=None, chats=None):
        self.main_window = _MW()
        self.conversation = conversation
        self._sorted_messages = list(messages or [])
        self.messages_list = _FakeList([str(m) for m in self._sorted_messages])
        self.chats_list = list(chats or [])
        self.conversations_list = _FakeList([str(c) for c in self.chats_list])
        self.selected_chats = set()
        self._unread_sep_idx = -1
        self._last_open_jid = ""
        self._last_list_focus_jid = ""

    _is_separator = ConversationsPanel._is_separator
    _no_conversation_open_announced = ConversationsPanel._no_conversation_open_announced
    _on_accel_jump_last = ConversationsPanel._on_accel_jump_last
    _on_accel_jump_unread = ConversationsPanel._on_accel_jump_unread
    _restore_conversation_selection = ConversationsPanel._restore_conversation_selection
    _on_conversation_focused = ConversationsPanel._on_conversation_focused
    # staticmethod: taken off the class it is a plain function, so binding it
    # directly onto the stub would make `self` its first argument.
    _vcard_phone_numbers = staticmethod(ConversationsPanel._vcard_phone_numbers)
    _contact_message_numbers = ConversationsPanel._contact_message_numbers
    _jid_from_vcard = ConversationsPanel._jid_from_vcard


def _msg(mid="m1"):
    return {
        "key": {"id": mid, "remoteJid": "5551@s.whatsapp.net"},
        "message": {"conversation": "oi"},
    }


class _FocusEvent:
    def __init__(self, index):
        self._index = index

    def GetIndex(self):
        return self._index


# ── #86: dizer que não há conversa aberta ────────────────────────────────────

class TestNoChatOpenIsAnnounced:
    """Alt+2 / Alt+3 / Ctrl+W sem conversa aberta ficavam totalmente calados, o
    que para quem usa leitor de tela é indistinguível de atalho quebrado."""

    def test_alt_2_says_no_chat_open(self):
        panel = _Stub(conversation=None)
        panel._on_accel_jump_last(None)
        assert panel.main_window.spoken == ["no_chat_open"]

    def test_alt_3_says_no_chat_open(self):
        panel = _Stub(conversation=None)
        panel._on_accel_jump_unread(None)
        assert panel.main_window.spoken == ["no_chat_open"]

    def test_with_a_conversation_open_it_says_nothing_and_proceeds(self):
        panel = _Stub(
            conversation={"remoteJid": "x@s.whatsapp.net"},
            messages=[_msg("a"), _msg("b")],
        )
        panel._on_accel_jump_last(None)
        assert panel.main_window.spoken == []
        assert panel.messages_list.focused == 1


# ── #87: Alt+2 numa conversa vazia ───────────────────────────────────────────

class TestEmptyChatIsAnnounced:
    """A conversa vazia mostra só a linha de placeholder, que não é uma
    mensagem — Alt+2 ("ir para a última mensagem") não deve focá-la, e ficar
    calado foi o que o relator abriu como bug."""

    def test_alt_2_on_an_empty_chat_says_it_is_empty(self):
        panel = _Stub(
            conversation={"remoteJid": "x@s.whatsapp.net"},
            messages=[{"_type": "empty_placeholder"}],
        )
        panel._on_accel_jump_last(None)
        assert panel.main_window.spoken == ["chat_is_empty"]
        assert panel.messages_list.focused == -1

    def test_a_list_holding_only_a_separator_counts_as_empty_too(self):
        panel = _Stub(
            conversation={"remoteJid": "x@s.whatsapp.net"},
            messages=[{"_type": "unread_separator", "count": 1}],
        )
        panel._on_accel_jump_last(None)
        assert panel.main_window.spoken == ["chat_is_empty"]

    def test_the_separator_is_skipped_but_a_real_message_still_wins(self):
        panel = _Stub(
            conversation={"remoteJid": "x@s.whatsapp.net"},
            messages=[_msg("a"), {"_type": "unread_separator", "count": 1}],
        )
        panel._on_accel_jump_last(None)
        assert panel.main_window.spoken == []
        assert panel.messages_list.focused == 0


# ── #91: a lista de conversas guarda onde o foco estava ──────────────────────

class TestChatListFocusIsPreserved:
    """Voltar para a lista devolvia sempre a conversa ABERTA, perdendo a linha
    em que o usuário tinha parado sem abrir."""

    def _panel(self):
        chats = [
            {"remoteJid": "c1@s.whatsapp.net"},
            {"remoteJid": "c2@s.whatsapp.net"},
            {"remoteJid": "c3@s.whatsapp.net"},
        ]
        return _Stub(chats=chats)

    def test_focus_returns_to_the_row_the_user_left_it_on(self):
        panel = self._panel()
        panel._last_open_jid = "c1@s.whatsapp.net"      # Conversa 1 aberta
        panel._on_conversation_focused(_FocusEvent(1))  # usuário desce até a 2
        panel._restore_conversation_selection()
        assert panel.conversations_list.focused == 1

    def test_closing_a_conversation_returns_to_that_row_too(self):
        panel = self._panel()
        panel._last_open_jid = "c2@s.whatsapp.net"
        panel._on_conversation_focused(_FocusEvent(2))
        panel._restore_conversation_selection()
        assert panel.conversations_list.focused == 2

    def test_it_falls_back_to_the_open_chat_when_that_row_is_gone(self):
        panel = self._panel()
        panel._last_open_jid = "c3@s.whatsapp.net"
        panel._last_list_focus_jid = "deleted@s.whatsapp.net"
        panel._restore_conversation_selection()
        assert panel.conversations_list.focused == 2

    def test_it_falls_back_to_the_first_row_when_neither_is_present(self):
        panel = self._panel()
        panel._last_open_jid = "gone@s.whatsapp.net"
        panel._last_list_focus_jid = "also-gone@s.whatsapp.net"
        panel._restore_conversation_selection()
        assert panel.conversations_list.focused == 0


# ── #84: números de um cartão de contato ─────────────────────────────────────

_VCARD_TWO = (
    "BEGIN:VCARD\n"
    "VERSION:3.0\n"
    "FN:Maria Assunção\n"
    "TEL;type=CELL;waid=5551999990000:+55 51 99999-0000\n"
    "TEL;type=WORK:+55 51 3333-1111\n"
    "END:VCARD"
)
_VCARD_WAID_ONLY = (
    "BEGIN:VCARD\nVERSION:3.0\nFN:Sem Tel\n"
    "X-WA-BIZ-NAME:x\nitem1.TEL;waid=5551988887777:\nEND:VCARD"
)


class TestContactCardNumbers:
    """O cartão de contato só mostrava o nome: não dava para ver nem copiar o
    número, que é a metade que faltava da issue #84."""

    def test_every_tel_line_is_returned_in_card_order_with_its_label(self):
        assert ConversationsPanel._vcard_phone_numbers(_VCARD_TWO) == [
            ("CELL", "+55 51 99999-0000"),
            ("WORK", "+55 51 3333-1111"),
        ]

    def test_the_same_number_written_twice_is_returned_once(self):
        card = (
            "BEGIN:VCARD\nTEL;type=CELL:+55 51 99999-0000\n"
            "TEL;type=HOME:+5551999990000\nEND:VCARD"
        )
        assert ConversationsPanel._vcard_phone_numbers(card) == [
            ("CELL", "+55 51 99999-0000")
        ]

    def test_a_card_with_no_tel_line_falls_back_to_the_waid(self):
        panel = _Stub()
        msg = {
            "messageType": "contactMessage",
            "message": {"contactMessage": {"vcard": _VCARD_WAID_ONLY}},
        }
        numbers = panel._contact_message_numbers(msg)
        assert len(numbers) == 1
        digits = "".join(c for c in numbers[0][1] if c.isdigit())
        assert "5551988887777" in digits

    def test_a_card_with_nothing_at_all_yields_no_numbers(self):
        panel = _Stub()
        msg = {
            "messageType": "contactMessage",
            "message": {"contactMessage": {"vcard": "BEGIN:VCARD\nFN:X\nEND:VCARD"}},
        }
        assert panel._contact_message_numbers(msg) == []

    def test_an_empty_card_is_not_an_error(self):
        assert ConversationsPanel._vcard_phone_numbers("") == []
        assert ConversationsPanel._vcard_phone_numbers(None) == []


# ── #85: o filtro do campo de busca da lista de contatos ─────────────────────

class TestContactSearchMatches:
    """A lista de contatos só tinha navegação por letra inicial, que busca do
    começo do nome exibido e por isso não acha ninguém pelo sobrenome."""

    def test_an_empty_query_matches_everything(self):
        assert contact_search_matches("", "Maria", "+55 51 99999-0000")
        assert contact_search_matches("   ", "Maria", "+55 51 99999-0000")

    def test_it_finds_by_surname_which_is_the_whole_point(self):
        assert contact_search_matches("silva", "Maria Silva", "+5551999990000")

    def test_it_finds_by_first_name_and_by_full_name(self):
        assert contact_search_matches("maria", "Maria Silva", "+5551999990000")
        assert contact_search_matches("maria silva", "Maria Silva", "+5551999990000")

    def test_accents_are_folded_both_ways(self):
        assert contact_search_matches("assuncao", "Maria Assunção", "")
        assert contact_search_matches("assunção", "Maria Assuncao", "")

    def test_the_number_matches_however_the_user_types_the_formatting(self):
        assert contact_search_matches("51 99999", "Maria", "+55 51 99999-0000")
        assert contact_search_matches("999990000", "Maria", "+55 51 99999-0000")

    def test_a_name_that_does_not_contain_the_query_is_filtered_out(self):
        assert not contact_search_matches("joao", "Maria Silva", "+5551999990000")

    def test_digits_in_the_query_do_not_match_a_contact_with_no_number(self):
        assert not contact_search_matches("999", "Maria Silva", "")


# ── #88: o recurso VERSIONINFO gerado pelo build ─────────────────────────────

def _load_build_module():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location(
        "winzapp_build", os.path.join(root, "build.py")
    )
    module = importlib.util.module_from_spec(spec)
    argv = sys.argv
    sys.argv = ["build.py"]
    try:
        spec.loader.exec_module(module)
    finally:
        sys.argv = argv
    return module


class TestVersionInfoResource:
    """O .exe não carregava VERSIONINFO nenhum, então Propriedades > Detalhes
    vinha em branco. Os números saem de client/version.py, para não poderem
    divergir da versão que o próprio app mostra."""

    def test_the_numeric_tuple_comes_from_version_py(self):
        build = _load_build_module()
        from version import __version__ as app_version

        display, parts = build._app_version_tuple()
        assert display == app_version
        assert len(parts) == 4
        assert all(isinstance(p, int) and 0 <= p <= 65535 for p in parts)

    def test_the_generated_file_carries_every_field_the_issue_asks_for(self):
        build = _load_build_module()
        from version import __version__ as app_version

        with tempfile.TemporaryDirectory() as tmp:
            path = build._write_version_file(tmp)
            with open(path, encoding="utf-8") as fh:
                content = fh.read()
        for field in (
            "FileDescription",
            "FileVersion",
            "ProductName",
            "ProductVersion",
            "OriginalFilename",
            "LegalCopyright",
        ):
            assert field in content
        assert app_version in content

    def test_pyinstaller_can_parse_what_we_generate(self):
        pytest.importorskip("PyInstaller")
        from PyInstaller.utils.win32 import versioninfo as vi

        build = _load_build_module()
        with tempfile.TemporaryDirectory() as tmp:
            with open(build._write_version_file(tmp), encoding="utf-8") as fh:
                content = fh.read()
        env = {
            n: getattr(vi, n)
            for n in (
                "VSVersionInfo",
                "FixedFileInfo",
                "StringFileInfo",
                "StringTable",
                "StringStruct",
                "VarFileInfo",
                "VarStruct",
            )
        }
        assert isinstance(eval(content, env), vi.VSVersionInfo)

    def test_the_build_passes_the_generated_file_to_pyinstaller(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "build.py"), encoding="utf-8") as fh:
            source = fh.read()
        assert '"--version-file", _write_version_file(work_dir),' in source
