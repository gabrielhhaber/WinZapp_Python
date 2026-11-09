"""Chat links (wa.me, api.whatsapp.com, whatsapp://send) handed to WinZapp.

The parser is pure; the window's handling is the unbound mixin methods on a
stub, so no window exists here.
"""

import json
from types import SimpleNamespace

import pytest

import ipc
from core import whatsapp_link_association as association
from core.whatsapp_chat_link import (
    MAX_CHAT_LINK_LENGTH, chat_target, is_whatsapp_link, whatsapp_link_argument)
from main_window import group_invitation_links
from main_window.group_invitation_links import GroupInvitationLinksMixin

AIRLINE = ("https://api.whatsapp.com/send/?phone=5491131260531&type=phone_number"
           "&app_absent=0&text=ADA,+quiero+recibir+notificaciones+del+vuelo+WJ%203230."
           "+Despega%20desde+Ezeiza+el+09/11/2026")
CODE = "AbCdEfGhIjKlMnOpQrStUv"


def test_api_whatsapp_link_with_mixed_plus_and_percent_encoding():
    phone, text = chat_target(AIRLINE)
    assert phone == "5491131260531"
    assert text == ("ADA, quiero recibir notificaciones del vuelo WJ 3230. "
                    "Despega desde Ezeiza el 09/11/2026")


def test_wa_me_link_decodes_the_text():
    assert chat_target("https://wa.me/15555550100?text=Ola%2C%20gostaria") == (
        "15555550100", "Ola, gostaria")


@pytest.mark.parametrize("link", [
    "whatsapp://send?phone=5511999998888",
    "whatsapp://send/?phone=%2B55%2011%2099999-8888",
    "https://wa.me/5511999998888",
    "https://api.whatsapp.com/send?phone=5511999998888",
    "https://web.whatsapp.com/send?phone=5511999998888",
])
def test_a_link_with_only_a_number_has_empty_text(link):
    assert chat_target(link) == ("5511999998888", "")


@pytest.mark.parametrize("value", [
    None, "", 5, "whatsapp://send", "whatsapp://send?text=oi",
    "whatsapp://send?phone=123", "whatsapp://chat?phone=5511999998888",
    "https://wa.me/message/ABCDEF", "https://wa.me/",
    "https://evil.example/send?phone=5511999998888",
    "http://wa.me/5511999998888", "5511999998888",
    "https://wa.me/5511999998888\nrm",
    "https://wa.me/5511999998888?text=" + "a" * MAX_CHAT_LINK_LENGTH,
])
def test_anything_else_is_not_a_chat_link(value):
    assert chat_target(value) is None


def test_the_text_is_bounded_and_groups_still_count_as_links():
    _, text = chat_target("https://wa.me/5511999998888?text=" + "a" * 4050 + "b")
    assert len(text) == 4000
    assert is_whatsapp_link("https://chat.whatsapp.com/" + CODE)
    assert not is_whatsapp_link("https://example.com")


def test_the_command_line_finds_a_chat_link_among_flags():
    argv = ["WinZapp.exe", "--background", "whatsapp://send?phone=5511999998888&text=oi"]
    assert whatsapp_link_argument(argv) == argv[2]
    assert whatsapp_link_argument(["WinZapp.exe", "--background"]) == ""


def test_ipc_passes_on_a_chat_link_and_refuses_other_text():
    opened = []
    listener = ipc.IpcListener("gd", "acc", on_activate=lambda s: None,
                               on_quit=lambda: None, on_open_link=opened.append)
    for link, ok in ((AIRLINE, True), ("https://example.com", False)):
        replies = listener._handle_message(
            json.dumps({"cmd": "open_link", "request_id": "1", "link": link}))
        assert bool(replies) is ok
    assert opened == [AIRLINE]


def test_installed_build_is_the_one_with_the_file_list(tmp_path):
    assert association.is_installed_build(str(tmp_path)) is False
    (tmp_path / "installed_files.dat").write_text("x")
    assert association.is_installed_build(str(tmp_path)) is True


class _Window:
    open_whatsapp_link = GroupInvitationLinksMixin.open_whatsapp_link
    open_chat_link = GroupInvitationLinksMixin.open_chat_link
    _wait_for_whatsapp = GroupInvitationLinksMixin._wait_for_whatsapp
    _finish_chat_link = GroupInvitationLinksMixin._finish_chat_link
    _fill_message_field = GroupInvitationLinksMixin._fill_message_field
    _INVITATION_LINK_WAIT_SECONDS = 180
    _INVITATION_LINK_POLL_MS = 1000

    def __init__(self, connected=True, existing=None):
        self._wa_connected = connected
        self.i18n = SimpleNamespace(t=lambda key: key)
        self.spoken = []
        self.restored = 0
        self.chats = {}
        self.field = SimpleNamespace(
            value="", focused=False,
            GetValue=lambda: self.field.value,
            SetValue=lambda v: setattr(self.field, "value", v),
            SetInsertionPointEnd=lambda: None,
            SetFocus=lambda: setattr(self.field, "focused", True))
        self.navigated = []
        self.conversations_panel = SimpleNamespace(
            message_field=self.field,
            navigate_to_conversation=self.navigated.append)
        self._existing = existing
        self.scheduled = 0

    def output(self, text):
        self.spoken.append(text)

    def restore_window(self):
        self.restored += 1

    def _schedule_set_chats(self):
        self.scheduled += 1


@pytest.fixture
def calls(monkeypatch):
    later, after = [], []
    monkeypatch.setattr(group_invitation_links.wx, "CallLater",
                        lambda ms, fn, *a: later.append((fn, a)))
    monkeypatch.setattr(group_invitation_links.wx, "CallAfter",
                        lambda fn, *a: after.append((fn, a)))
    return SimpleNamespace(later=later, after=after)


@pytest.fixture
def find_chat(monkeypatch):
    import ui.dialogs.new_conversation as module

    def fake(mw, jid):
        return jid, mw._existing

    monkeypatch.setattr(module.NewConversationDialog, "_find_existing_chat",
                        staticmethod(fake))


def test_a_chat_link_while_offline_waits_and_says_so(calls):
    window = _Window(connected=False)
    window.open_whatsapp_link(AIRLINE)
    assert window.spoken == ["chat_link_waiting"]
    assert len(calls.later) == 1 and window.restored == 0


def test_the_chat_link_wait_ends_and_says_so(calls):
    window = _Window(connected=False)
    window.open_chat_link(AIRLINE, 0.0)
    assert window.spoken == ["chat_link_not_connected"]
    assert not calls.later


def test_a_new_number_opens_a_chat_with_the_text_unsent(calls, find_chat, monkeypatch):
    import core.number_lookup as lookup
    monkeypatch.setattr(lookup, "resolve_typed_number", lambda mw, digits: "")
    monkeypatch.setattr(group_invitation_links.threading, "Thread",
                        lambda target, **kw: SimpleNamespace(start=target))
    window = _Window()
    window.open_whatsapp_link("https://wa.me/5511999998888?text=ola")
    (finish, args), = calls.after
    assert args == ("5511999998888@s.whatsapp.net", "ola")
    calls.after.clear()
    finish(*args)
    assert window.chats["5511999998888@s.whatsapp.net"]["remoteJid"]
    assert window.navigated and window.scheduled == 1
    (fill, fill_args), = calls.after
    fill(*fill_args)
    assert window.field.value == "ola" and window.field.focused


def test_an_existing_draft_is_kept_not_overwritten(calls, find_chat):
    chat = {"remoteJid": "5511999998888@s.whatsapp.net"}
    window = _Window(existing=chat)
    window.field.value = "rascunho"
    window._finish_chat_link("5511999998888@s.whatsapp.net", "ola")
    assert window.navigated == [chat] and not window.chats
    (fill, args), = calls.after
    fill(*args)
    assert window.field.value == "rascunho ola"


def test_a_link_with_no_text_only_focuses_the_field(calls, find_chat):
    window = _Window(existing={"remoteJid": "x"})
    window._finish_chat_link("x", "")
    (fill, args), = calls.after
    fill(*args)
    assert window.field.value == "" and window.field.focused


def test_non_links_are_ignored(calls):
    window = _Window()
    window.open_whatsapp_link("https://example.com")
    assert not calls.after and not calls.later and not window.spoken
