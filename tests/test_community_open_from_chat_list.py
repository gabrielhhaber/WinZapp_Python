"""A community opened from the chat list shows its groups in the Communities
panel (Settings > Interface > Chats, on by default)."""

from types import SimpleNamespace
from unittest.mock import Mock

from core.communities import is_community_chat
from tests.test_communities import CLUB, SCHOOL, Dialog
from ui.conversation_panel import conversation_navigation as nav
from ui.dialogs.communities import CommunitiesPanel


def test_the_community_chat_is_recognised_by_its_metadata():
    assert is_community_chat({"id": "1@g.us", "groupMetadata": {"isParentGroup": True}})


def test_a_group_of_a_community_is_not_the_community():
    assert not is_community_chat({"id": "2@g.us",
                                  "groupMetadata": {"isParentGroup": False, "parentGroup": "1@g.us"}})


def test_a_chat_without_metadata_is_recognised_by_the_communities_already_read():
    assert is_community_chat({"remoteJid": "1@g.us"}, known_jids=["1@g.us"])
    assert not is_community_chat({"remoteJid": "1@s.whatsapp.net"}, known_jids=["1@s.whatsapp.net"])
    assert not is_community_chat(None)


def _navigation(setting=True, known=()):
    panel = SimpleNamespace(_communities=[SimpleNamespace(jid=j) for j in known])
    mw = SimpleNamespace(settings={"user_interface": {"open_communities_in_panel": setting}},
                         communities_panel=panel)
    return SimpleNamespace(main_window=mw)


def test_the_community_goes_to_the_panel(monkeypatch):
    shown = Mock(return_value=object())
    monkeypatch.setattr("main_window.content_panels.show_communities", shown)
    chat = {"id": "1@g.us", "groupMetadata": {"isParentGroup": True}}
    assert nav.ConversationNavigationMixin._open_community_in_panel(_navigation(), chat) is True
    shown.assert_called_once()
    assert shown.call_args.kwargs == {"select": "1@g.us"}


def test_with_the_setting_off_the_community_opens_as_a_chat(monkeypatch):
    shown = Mock(return_value=object())
    monkeypatch.setattr("main_window.content_panels.show_communities", shown)
    cls = nav.ConversationNavigationMixin
    chat = {"id": "1@g.us", "groupMetadata": {"isParentGroup": True}}
    assert cls._open_community_in_panel(_navigation(setting=False), chat) is False
    shown.assert_not_called()


def test_a_normal_group_opens_as_a_chat(monkeypatch):
    shown = Mock(return_value=object())
    monkeypatch.setattr("main_window.content_panels.show_communities", shown)
    cls = nav.ConversationNavigationMixin
    assert cls._open_community_in_panel(_navigation(), {"id": "9@g.us"}) is False
    shown.assert_not_called()


class _Panel(Dialog):
    _apply_pending_select = CommunitiesPanel._apply_pending_select


def test_the_panel_selects_the_community_and_focuses_its_groups():
    panel = _Panel()
    panel._pending_select = SCHOOL.jid
    assert panel._apply_pending_select() is True
    assert panel._selected().jid == SCHOOL.jid
    panel._groups.SetFocus.assert_called_once()
    assert panel._pending_select == ""


def test_a_community_not_read_yet_waits_for_the_reload():
    panel = _Panel(communities=None)
    panel._pending_select = CLUB.jid
    assert panel._apply_pending_select() is False
    assert panel._pending_select == CLUB.jid
    panel._finish_reload([SCHOOL, CLUB])
    assert panel._selected().jid == CLUB.jid
    assert panel._pending_select == ""


def test_a_failed_reading_drops_the_pending_community():
    # Left pending, the next unrelated reading (Refresh, after a join) would
    # suddenly move the selection and the focus to it.
    panel = _Panel(communities=None)
    panel._announce_if_shown = Mock()
    panel._pending_select = SCHOOL.jid
    panel._finish_reload(None)
    assert panel._pending_select == ""
    panel._announce_if_shown.assert_called_once()
    panel._finish_reload([SCHOOL, CLUB])
    assert panel._selected().jid == CLUB.jid  # the first by name, not the old jump
    panel._groups.SetFocus.assert_not_called()


def test_a_community_the_reading_does_not_bring_is_said_not_kept():
    panel = _Panel(communities=None)
    panel._announce_if_shown = Mock()
    panel._pending_select = "120363000000000999@g.us"
    panel._finish_reload([SCHOOL, CLUB], quiet=True)
    assert panel._pending_select == ""
    panel._announce_if_shown.assert_called_once()  # the count, not silence


def test_a_reading_that_cannot_start_drops_the_pending_community():
    panel = _Panel(communities=None)
    panel._current = lambda: False
    panel._pending_select = CLUB.jid
    panel._reload()
    assert panel._pending_select == ""


def _archived(community):
    conversations = SimpleNamespace(_open_community_in_panel=Mock(return_value=community),
                                    navigate_to_conversation=Mock(), Show=Mock(),
                                    conversations_label=Mock(), conversations_list=Mock())
    mw = SimpleNamespace(conversations_panel=conversations, archived_conversations_panel=Mock(),
                         content_panel=Mock())
    return SimpleNamespace(main_window=mw, chats_list=[{"id": "1@g.us"}])


def test_an_archived_community_goes_to_the_panel_too():
    from ui.conversation_panel.archived_panel import ArchivedConversationsPanel
    stub = _archived(community=True)
    ArchivedConversationsPanel.on_conversation_selected(stub, SimpleNamespace(GetIndex=lambda: 0))
    stub.main_window.archived_conversations_panel.Hide.assert_not_called()
    stub.main_window.conversations_panel.navigate_to_conversation.assert_not_called()


def test_an_archived_chat_still_opens_as_a_chat():
    from ui.conversation_panel.archived_panel import ArchivedConversationsPanel
    stub = _archived(community=False)
    ArchivedConversationsPanel.on_conversation_selected(stub, SimpleNamespace(GetIndex=lambda: 0))
    stub.main_window.conversations_panel.navigate_to_conversation.assert_called_once()


def test_a_community_already_listed_moves_the_focus_once():
    panel = _Panel()
    panel._renew_connection = lambda: False
    panel._reload = Mock()
    CommunitiesPanel.on_show(panel, select=SCHOOL.jid)
    panel._groups.SetFocus.assert_called_once()
    panel._list.SetFocus.assert_not_called()  # no stop on the list first
    panel._reload.assert_called_once_with(quiet=True)
