"""Ctrl+C and the context menu, while a link inside a message has focus.

Tab from the messages list reaches the links of the focused message —
a HyperlinkCtrl when there is one, a navigable list when there are several
(issue #65) — and both used to answer Ctrl+C by copying the *message*. That is
what the shortcut means everywhere else in the conversation and nothing anyone
wants while standing on a link; the context menu had the same problem, opening
the focused message's forward/reply/delete for a row the user had left.

The links list did have a Ctrl+C handler of its own. It never ran: the shortcut
is an accelerator (ID_CTRL_C -> _on_accel_copy_message) and wxMSW translates
accelerators before the focused control sees a key event, so the handler was
written and then silently outranked. That is why the decision has to live in
_on_accel_copy_message(), and why "who has focus" is the only reading available
to it.

The announcement names the address, because on a message carrying several links
a bare "link copiado" cannot tell a screen-reader user which one landed on the
clipboard.

ConversationsPanel is a wx.Panel and cannot be instantiated without a running
wx.App, so its methods are exercised unbound against a stub carrying only what
they touch.
"""

import types

import pytest
import wx

import ui.conversations as conversations_module
from ui.conversations import ConversationsPanel


ONE = "https://example.com/one"
TWO = "https://example.com/two"


class _FakeList:
    """The wx.ListCtrl _update_links_panel() builds for 2+ links."""

    def __init__(self, selected=0):
        self._selected = selected

    def GetFirstSelected(self):
        return self._selected


class _FakeHyperlink:
    """The wx.adv.HyperlinkCtrl a single link gets instead."""

    def __init__(self, url=ONE):
        self._url = url

    def GetURL(self):
        return self._url


class _FakeI18n:
    def t(self, key):
        return {
            "link_copied": "Link copiado: {url}",
            "msg_copy_error": "erro ao copiar",
            "open_link": "Abrir link",
            "copy_link": "Copiar link",
        }.get(key, key)


class _FakeMainWindow:
    def __init__(self):
        self.i18n = _FakeI18n()
        self.spoken = []
        self.settings = {"user_interface": {"bulk_action_shortcuts": True}}

    def output(self, text, interrupt=False):
        self.spoken.append(text)


class _Stub:
    _link_url_for           = ConversationsPanel._link_url_for
    _focused_link_url       = ConversationsPanel._focused_link_url
    _copy_focused_link      = ConversationsPanel._copy_focused_link
    _on_link_context_menu   = ConversationsPanel._on_link_context_menu
    _on_accel_copy_message  = ConversationsPanel._on_accel_copy_message
    _bulk_shortcuts_enabled = ConversationsPanel._bulk_shortcuts_enabled

    def __init__(self, links=None, links_list=None, link_ctrl=None):
        self.main_window = _FakeMainWindow()
        self._current_links = list(links or [])
        self._links_list = links_list
        self._link_ctrl = link_ctrl
        self.selected_messages = []
        self._sorted_messages = []
        self.messages_list = types.SimpleNamespace(GetFirstSelected=lambda: -1)
        self.popped = []
        self.bound = []
        # Anything reached only when the link check does NOT fire.
        self.fell_through = []

    # --- collaborators the message-copy path would reach ------------------
    def _on_mass_copy_messages(self, event):
        self.fell_through.append("mass")

    def _on_menu_copy_message(self, msg):
        self.fell_through.append("message")

    def _is_separator(self, msg):
        return False

    # --- wx surface _on_link_context_menu() uses --------------------------
    def PopupMenu(self, menu):
        # Read now, not later: _on_link_context_menu() destroys the menu as
        # soon as this returns, and touching it afterwards raises.
        self.popped.append([item.GetItemLabelText()
                            for item in menu.GetMenuItems()])

    def Bind(self, event, handler, source=None):
        self.bound.append((handler, source))


def _copies(monkeypatch):
    """Capture what reaches the clipboard without touching the real one."""
    copied = []
    monkeypatch.setattr(conversations_module.pyperclip, "copy", copied.append)
    return copied


class TestResolvingWhichLinkAControlIsShowing:
    def test_the_list_answers_with_its_selected_row(self):
        stub = _Stub(links=[ONE, TWO], links_list=_FakeList(selected=1))
        assert stub._link_url_for(stub._links_list) == TWO

    def test_a_selection_outside_the_links_answers_nothing(self):
        """The list is rebuilt per message; a stale index must not index into
        another message's links."""
        stub = _Stub(links=[ONE], links_list=_FakeList(selected=5))
        assert stub._link_url_for(stub._links_list) == ""

    def test_nothing_selected_answers_nothing(self):
        stub = _Stub(links=[ONE, TWO], links_list=_FakeList(selected=-1))
        assert stub._link_url_for(stub._links_list) == ""

    def test_the_single_link_control_answers_with_its_own_url(self):
        stub = _Stub(links=[ONE], link_ctrl=_FakeHyperlink(ONE))
        assert stub._link_url_for(stub._link_ctrl) == ONE

    def test_any_other_window_answers_nothing(self):
        stub = _Stub(links=[ONE, TWO], links_list=_FakeList())
        assert stub._link_url_for(object()) == ""

    def test_no_window_answers_nothing(self):
        assert _Stub()._link_url_for(None) == ""

    def test_a_message_with_no_links_answers_nothing(self):
        """Both attributes are cleared on every rebuild, so this is the state
        between one message's links and the next."""
        assert _Stub()._link_url_for(_FakeList()) == ""


class TestCopyingSaysWhichLinkItCopied:
    def test_the_address_is_spoken(self, monkeypatch):
        copied = _copies(monkeypatch)
        stub = _Stub()
        stub._copy_focused_link(TWO)
        assert copied == [TWO]
        assert stub.main_window.spoken == [f"Link copiado: {TWO}"]

    def test_a_clipboard_failure_says_so_instead(self, monkeypatch):
        def boom(_value):
            raise RuntimeError("clipboard busy")

        monkeypatch.setattr(conversations_module.pyperclip, "copy", boom)
        stub = _Stub()
        stub._copy_focused_link(TWO)
        assert stub.main_window.spoken == ["erro ao copiar"]


class TestCtrlCPrefersTheFocusedLink:
    """_on_accel_copy_message() is the accelerator handler, and it runs before
    any focused control sees the key — so this is the only place the choice
    can be made."""

    def _focus(self, monkeypatch, window):
        monkeypatch.setattr(wx.Window, "FindFocus", staticmethod(lambda: window))

    def test_a_focused_list_row_is_copied_instead_of_the_message(
            self, monkeypatch):
        copied = _copies(monkeypatch)
        stub = _Stub(links=[ONE, TWO], links_list=_FakeList(selected=1))
        self._focus(monkeypatch, stub._links_list)

        stub._on_accel_copy_message(None)

        assert copied == [TWO]
        assert stub.fell_through == []

    def test_a_focused_single_link_is_copied_instead_of_the_message(
            self, monkeypatch):
        copied = _copies(monkeypatch)
        stub = _Stub(links=[ONE], link_ctrl=_FakeHyperlink(ONE))
        self._focus(monkeypatch, stub._link_ctrl)

        stub._on_accel_copy_message(None)

        assert copied == [ONE]
        assert stub.fell_through == []

    def test_it_wins_over_a_bulk_selection(self, monkeypatch):
        """A selection can be left behind in a conversation the user has gone
        on reading; focus on a link is a statement about right now."""
        copied = _copies(monkeypatch)
        stub = _Stub(links=[ONE, TWO], links_list=_FakeList(selected=0))
        stub.selected_messages = [{"key": {"id": "m1"}}]
        self._focus(monkeypatch, stub._links_list)

        stub._on_accel_copy_message(None)

        assert copied == [ONE]
        assert stub.fell_through == []

    def test_with_no_link_focused_the_message_path_still_runs(self, monkeypatch):
        copied = _copies(monkeypatch)
        stub = _Stub(links=[ONE, TWO], links_list=_FakeList(selected=1))
        stub.selected_messages = [{"key": {"id": "m1"}}]
        self._focus(monkeypatch, object())

        stub._on_accel_copy_message(None)

        assert copied == []
        assert stub.fell_through == ["mass"]


class TestTheContextMenuOfAFocusedLink:
    def test_it_offers_open_and_copy_for_that_link(self, wx_app, monkeypatch):
        copied = _copies(monkeypatch)
        stub = _Stub(links=[ONE, TWO], links_list=_FakeList(selected=1))
        event = types.SimpleNamespace(
            GetEventObject=lambda: stub._links_list,
            Skip=lambda: stub.fell_through.append("skip"),
        )

        stub._on_link_context_menu(event)

        assert stub.fell_through == [], "the event was passed on instead"
        assert stub.popped == [["Abrir link", "Copiar link"]]

        # The copy entry acts on the link that was focused, not on the message.
        copy_handler = stub.bound[1][0]
        copy_handler(None)
        assert copied == [TWO]

    def test_an_unrelated_control_is_passed_on_untouched(self, wx_app):
        """Bound only on the link controls, but a context-menu event is a
        command event and travels — anything else must reach whatever would
        have handled it."""
        stub = _Stub(links=[ONE], links_list=_FakeList())
        event = types.SimpleNamespace(
            GetEventObject=lambda: object(),
            Skip=lambda: stub.fell_through.append("skip"),
        )

        stub._on_link_context_menu(event)

        assert stub.fell_through == ["skip"]
        assert stub.popped == []


def test_every_locale_carries_the_three_strings():
    """The address is interpolated, so a locale missing the placeholder would
    announce a link without saying which one."""
    import json
    from pathlib import Path

    languages = Path(__file__).resolve().parents[1] / "client" / "languages"
    for locale in ("pt-BR", "pt-PT", "en-US", "es-ES", "pl"):
        data = json.loads((languages / f"{locale}.json").read_text(encoding="utf-8"))
        for key in ("link_copied", "open_link", "copy_link"):
            assert data.get(key), f"{locale} is missing {key}"
        assert "{url}" in data["link_copied"], (
            f"{locale}'s link_copied does not name the address"
        )
