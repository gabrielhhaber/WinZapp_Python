"""A WhatsApp group invitation link inside a message opens WinZapp's own join
dialog instead of the browser.

Enter or Space on the link (reached with Tab), a click on it, Enter on the
message row and the link's context menu all end in the same place: the dialog
that previews the group and joins only when the user says so. The browser is
still one menu item away, and every other link behaves as before.

ConversationsPanel is a wx.Panel and cannot be built without a wx.App, so the
methods are bound onto a stub.
"""

import inspect
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import wx

from ui.conversations import ConversationsPanel
from ui.dialogs import group_join_dialog
from ui.dialogs.group_join_dialog import GroupJoinDialog, join_group_by_link

CODE = "AbCdEfGhIjKlMnOpQrStUv"
INVITE = "https://chat.whatsapp.com/" + CODE + "?mode=ems_copy_t"
OTHER = "https://example.com/page"


class _Stub:
    _activate_link = ConversationsPanel._activate_link
    _on_hyperlink_open = ConversationsPanel._on_hyperlink_open
    _on_link_key_down = ConversationsPanel._on_link_key_down
    _on_links_list_activated = ConversationsPanel._on_links_list_activated
    _on_links_list_key_down = ConversationsPanel._on_links_list_key_down
    _link_url_for = ConversationsPanel._link_url_for

    def __init__(self, links=()):
        self.main_window = SimpleNamespace(settings={})
        self._current_links = list(links)
        self._links_list = SimpleNamespace(GetFirstSelected=lambda: 0)
        self.opened = []
        self.joined = []

    def _open_link(self, url):
        self.opened.append(url)

    def _join_group_from_link(self, url):
        self.joined.append(url)


class _Key:
    def __init__(self, code, url=""):
        self._code, self._url = code, url
        self.skipped = False

    def GetKeyCode(self):
        return self._code

    def ControlDown(self):
        return False

    def GetEventObject(self):
        return SimpleNamespace(GetURL=lambda: self._url)

    def Skip(self):
        self.skipped = True


@pytest.fixture(autouse=True)
def _plain_keys(monkeypatch):
    """command_key_event() remaps configurable shortcuts; none is involved."""
    import ui.conversation_panel.links as links
    monkeypatch.setattr(links, "command_key_event", lambda owner, table, event: event)


def test_an_invitation_link_opens_the_join_dialog_not_the_browser():
    stub = _Stub()
    stub._activate_link(INVITE)
    assert stub.joined == [INVITE]
    assert stub.opened == []


def test_any_other_link_still_opens_in_the_default_application():
    stub = _Stub()
    stub._activate_link(OTHER)
    assert stub.opened == [OTHER]
    assert stub.joined == []


@pytest.mark.parametrize("key", [wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER, wx.WXK_SPACE])
def test_enter_and_space_on_the_single_link_control(key):
    stub = _Stub()
    stub._on_link_key_down(_Key(key, INVITE))
    assert stub.joined == [INVITE]


def test_a_click_on_the_single_link_control():
    stub = _Stub()
    stub._on_hyperlink_open(SimpleNamespace(GetURL=lambda: INVITE))
    assert stub.joined == [INVITE]


@pytest.mark.parametrize("key", [wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER, wx.WXK_SPACE])
def test_enter_and_space_on_a_row_of_the_links_list(key):
    stub = _Stub([INVITE, OTHER])
    stub._on_links_list_key_down(_Key(key))
    assert stub.joined == [INVITE]
    assert stub.opened == []


def test_activating_a_row_of_the_links_list():
    stub = _Stub([OTHER, INVITE])
    stub._on_links_list_activated(SimpleNamespace(GetIndex=lambda: 1))
    assert stub.joined == [INVITE]


def test_the_context_menu_of_an_invitation_link_offers_join_first_and_keeps_the_browser():
    """Structural: the menu is built with a real wx.Menu, which needs an app.
    Join comes first because it is what Enter does; "open link" stays, for
    someone who does want the web page."""
    source = inspect.getsource(ConversationsPanel._on_link_context_menu)
    join = source.index('i18n.t("group_join_title")')
    open_link = source.index('i18n.t("open_link")')
    assert "invitation_code(url)" in source
    assert join < open_link
    assert "self._open_link(u)" in source


def test_enter_on_the_message_row_takes_the_same_path():
    from ui.conversation_panel import message_list

    source = inspect.getsource(message_list)
    assert "if invitation_code(links[0]):" in source
    assert "self._join_group_from_link(links[0])" in source


# ── the dialog, opened with a link ───────────────────────────────────────────


def test_a_link_fills_the_field_and_asks_for_the_preview_without_joining():
    """Structural, for the same reason: the constructor builds real controls.
    ChangeValue() (not SetValue()) because EVT_TEXT clears the preview, and the
    preview is requested, never the join."""
    source = inspect.getsource(GroupJoinDialog.__init__)
    assert "self._input.ChangeValue(link)" in source
    assert "wx.CallAfter(self._on_preview, None)" in source
    assert "_on_join" not in source.split("if link:")[1]


@pytest.mark.parametrize("modal", [wx.ID_CANCEL, wx.ID_OK])
def test_the_entry_point_passes_the_link_and_opens_only_a_joined_group(monkeypatch, modal):
    preview = SimpleNamespace(jid="123-456@g.us", name="Test group")
    dialog = Mock(result=preview)
    dialog.ShowModal.return_value = modal
    factory = Mock(return_value=dialog)
    monkeypatch.setattr(group_join_dialog, "GroupJoinDialog", factory)
    after = Mock()
    monkeypatch.setattr(wx, "CallAfter", after)
    mw = SimpleNamespace(chats={}, _schedule_set_chats=Mock(), i18n=SimpleNamespace(t=str),
                         conversations_panel=SimpleNamespace(navigate_to_conversation=Mock()))

    joined = join_group_by_link(mw, mw, INVITE)

    factory.assert_called_once_with(mw, mw, INVITE)
    dialog.Destroy.assert_called_once()
    assert joined is (modal == wx.ID_OK)
    if modal == wx.ID_OK:
        after.assert_called_once_with(group_join_dialog._go_to_group, mw, preview.jid, preview.name)
        assert mw.chats[preview.jid]["remoteJid"] == preview.jid
    else:
        assert mw.chats == {}
        after.assert_not_called()


class _Panel:
    def __init__(self, shown=True):
        self.conversation = None
        self.navigate_to_conversation = Mock()
        self.IsShown = lambda: shown


def _window(chats, locked=()):
    mw = SimpleNamespace(chats=chats, conversations_panel=_Panel(),
                         is_chat_locked=lambda jid: jid in locked)
    mw.navigate_to_conversation_jid = Mock()
    return mw


def test_a_group_opens_through_the_window_s_own_way_in():
    """navigate_to_conversation_jid asks the PIN of a locked chat, opens an
    archived one from its list and shows the main panel first."""
    mw = _window({"123-456@g.us": {"remoteJid": "123-456@g.us"}})

    def opened(jid, name):
        mw.conversations_panel.conversation = {"remoteJid": jid}

    mw.navigate_to_conversation_jid.side_effect = opened
    group_join_dialog._go_to_group(mw, "123-456@g.us", "Test group")
    mw.navigate_to_conversation_jid.assert_called_once_with("123-456@g.us", "Test group")
    mw.conversations_panel.navigate_to_conversation.assert_not_called()


def test_a_locked_group_is_never_opened_around_the_pin():
    mw = _window({"123-456@g.us": {"remoteJid": "123-456@g.us"}}, locked={"123-456@g.us"})
    group_join_dialog._go_to_group(mw, "123-456@g.us", "Test group")
    mw.navigate_to_conversation_jid.assert_called_once()
    mw.conversations_panel.navigate_to_conversation.assert_not_called()


def test_a_group_with_no_row_yet_is_opened_once_the_panel_is_shown():
    chat = {"remoteJid": "123-456@g.us", "name": "New"}
    mw = _window({"123-456@g.us": chat})
    group_join_dialog._go_to_group(mw, "123-456@g.us", "New")
    mw.conversations_panel.navigate_to_conversation.assert_called_once_with(chat)


def test_nothing_is_opened_into_a_hidden_panel():
    """When the main panel did not come up (a refused PIN, a panel left
    hidden), opening the chat there would put focus on a control nobody
    can reach."""
    chat = {"remoteJid": "123-456@g.us", "name": "New"}
    mw = _window({"123-456@g.us": chat})
    mw.conversations_panel = _Panel(shown=False)
    group_join_dialog._go_to_group(mw, "123-456@g.us", "New")
    mw.navigate_to_conversation_jid.assert_called_once()
    mw.conversations_panel.navigate_to_conversation.assert_not_called()
