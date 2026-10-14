"""Keyboard shortcuts for catalogs, orders and communities: a default key each,
configurable on the Shortcuts tab, with the rules the menus follow."""

import pathlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.commerce import Account
from core.shortcut_catalog import SHORTCUTS
from ui.conversation_panel.commerce import CommercePanelMixin
from ui.conversation_panel.message_list import MessageListMixin

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONTACT = "5511900000001@s.whatsapp.net"
OWN = "5511900000009@s.whatsapp.net"


class _Panel:
    for _name in ("_commerce_chat", "_catalog_shortcut", "_order_create_shortcut",
                  "_on_accel_catalog_list", "_on_accel_catalog",
                  "_on_accel_order_create_list", "_on_accel_order_create"):
        locals()[_name] = getattr(CommercePanelMixin, _name)
    del _name

    def __init__(self, account=None, seller=False, jid=CONTACT, selected=None):
        self.spoken = []
        self.main_window = SimpleNamespace(
            i18n=SimpleNamespace(t=lambda key: key), output=self.spoken.append,
            commerce_account=lambda: account, is_seller_chat=lambda chat: seller,
            my_jid=OWN, _normalize_jid=lambda jid: jid)
        self.conversation = {"remoteJid": jid}
        self._selected = selected
        self._show_catalog = Mock()
        self._show_order_create = Mock()

    def _selected_chat_from_list(self):
        return self._selected


BUSINESS = Account(True, True, frozenset())
CONSUMER = Account(False, None, frozenset())


class TestTheCatalog:
    def test_in_a_chat_with_a_business_it_opens_from_the_conversation_and_the_list(self):
        panel = _Panel(CONSUMER, seller=True, selected={"remoteJid": CONTACT})
        panel._on_accel_catalog()
        panel._on_accel_catalog_list()
        assert panel._show_catalog.call_count == 2 and panel.spoken == []

    @pytest.mark.parametrize("jid", ["120363000000000000@g.us", OWN, ""])
    def test_a_group_the_own_chat_or_nothing_says_why(self, jid):
        panel = _Panel(CONSUMER, seller=True, jid=jid)
        panel._on_accel_catalog()
        panel._show_catalog.assert_not_called()
        assert panel.spoken == ["commerce_catalog_unavailable"]

    def test_a_chat_with_no_business_says_why(self):
        panel = _Panel(CONSUMER, seller=False)
        panel._on_accel_catalog()
        assert panel.spoken == ["commerce_catalog_unavailable"]

    def test_nothing_selected_in_the_list_says_why(self):
        panel = _Panel(CONSUMER, seller=True, selected=None)
        panel._on_accel_catalog_list()
        assert panel.spoken == ["commerce_catalog_unavailable"]


class TestCreatingAnOrder:
    def test_a_business_account_opens_it(self):
        panel = _Panel(BUSINESS, selected={"remoteJid": CONTACT})
        panel._on_accel_order_create()
        panel._on_accel_order_create_list()
        assert panel._show_order_create.call_count == 2

    @pytest.mark.parametrize("account", [CONSUMER, None])
    def test_any_other_account_hears_why(self, account):
        panel = _Panel(account)
        panel._on_accel_order_create()
        panel._show_order_create.assert_not_called()
        assert panel.spoken == ["commerce_order_create_unavailable"]

    def test_not_in_a_group(self):
        panel = _Panel(BUSINESS, jid="120363000000000000@g.us")
        panel._on_accel_order_create()
        assert panel.spoken == ["commerce_order_create_unavailable"]


class TestTheCatalogRows:
    def _rows(self, shortcut_id):
        return [s for s in SHORTCUTS if s.id == shortcut_id]

    def test_one_preference_for_the_list_and_the_conversation(self):
        for shortcut_id, mod, key in (("chats.ID_CATALOG", 5, ord("B")),
                                      ("chats.ID_ORDER_CREATE", 6, ord("N"))):
            rows = self._rows(shortcut_id)
            assert {row.scope for row in rows} == {"chats", "messages"}
            assert {(row.mod, row.key) for row in rows} == {(mod, key)}

    def test_communities_is_alt_8_from_the_whole_window(self):
        [row] = self._rows("main.ID_COMMUNITIES")
        assert (row.scope, row.mod, row.key) == ("main", 1, ord("8"))

    def test_no_other_command_of_those_scopes_has_the_same_default(self):
        mine = {"chats.ID_CATALOG", "chats.ID_ORDER_CREATE", "main.ID_COMMUNITIES"}
        together = {"chats", "messages", "main"}
        for row in (s for s in SHORTCUTS if s.id in mine):
            clashes = [s.id for s in SHORTCUTS if s.id not in mine and s.scope in together
                       and (s.mod, s.key) == (row.mod, row.key)]
            assert clashes == [], (row.id, clashes)

    def test_the_f1_list_names_them(self):
        from ui.dialogs.shortcuts_dialog import ShortcutsDialog
        text = ShortcutsDialog._build_text(SimpleNamespace(t=lambda key: key))
        for key in ("shortcut_alt8_communities_label", "shortcut_alt_shift_b_catalog_label",
                    "shortcut_ctrl_shift_n_order_label"):
            assert key in text


def test_alt_8_opens_communities(monkeypatch):
    from main_window.shortcuts import ShortcutsMixin
    shown = []
    monkeypatch.setattr("ui.dialogs.communities.show_communities",
                        lambda mw, parent: shown.append((mw, parent)))
    window = SimpleNamespace()
    ShortcutsMixin._on_global_communities(window)
    assert shown == [(window, window)]


class _MessagePanel:
    activate_message = MessageListMixin.activate_message

    def __init__(self, viewable):
        self._can_view_order = lambda msg: viewable
        self._show_order = Mock()


ORDER = {"key": {"id": "O1"}, "messageType": "orderMessage", "message": {"orderMessage": {}}}


def test_enter_on_an_order_opens_it():
    panel = _MessagePanel(viewable=True)
    panel.activate_message(ORDER)
    panel._show_order.assert_called_once_with(ORDER)


def test_enter_on_an_order_that_cannot_be_viewed_does_nothing():
    panel = _MessagePanel(viewable=False)
    panel.activate_message(ORDER)
    panel._show_order.assert_not_called()
