"""CommercePanelMixin — part of ConversationsPanel (see ui/conversation_panel/__init__.py).

The ways into carts and orders: a business's catalog and cart from its chat,
an order from its message, and a business account's own tools.
"""

import wx

from core.commerce import is_order_message


class CommercePanelMixin:
    def _append_commerce_items(self, menu, chat):
        """Chat menu: what applies to this chat and this account, nothing else.
        Offered only on facts the server gave (MainWindow.commerce_account)."""
        mw = self.main_window
        i18n = mw.i18n
        jid = chat.get("remoteJid", "")
        if mw.is_seller_chat(jid):
            item = menu.Append(wx.ID_ANY, f"{i18n.t('commerce_catalog_menu')}\tAlt+Shift+B")
            self.Bind(wx.EVT_MENU, lambda e, c=chat: self._show_catalog(c), item)
        account = mw.commerce_account()
        if account is None or not account.business:
            return
        tools = wx.Menu()
        create = tools.Append(wx.ID_ANY, f"{i18n.t('commerce_create_order_menu')}\tCtrl+Shift+N")
        self.Bind(wx.EVT_MENU, lambda e, c=chat: self._show_order_create(c), create)
        if account.cart_enabled is not None:
            carts = tools.AppendCheckItem(wx.ID_ANY, i18n.t("commerce_cart_setting_menu"))
            carts.Check(account.cart_enabled)
            self.Bind(wx.EVT_MENU, lambda e: mw.toggle_cart_enabled(), carts)
        menu.AppendSubMenu(tools, i18n.t("commerce_business_tools"))

    # -- shortcuts: the same rules as the menu, said when they do not apply --

    def _commerce_chat(self, chat):
        """The chat the commerce items would be offered for: one person's,
        never a group's or the account's own."""
        jid = (chat or {}).get("remoteJid", "")
        if not jid or jid.endswith(("@g.us", "@broadcast", "@newsletter")):
            return None
        mw = self.main_window
        own = getattr(mw, "my_jid", "") or ""
        if own and mw._normalize_jid(jid) == mw._normalize_jid(own):
            return None
        return chat

    def _catalog_shortcut(self, chat):
        chat = self._commerce_chat(chat)
        mw = self.main_window
        if chat is None or not mw.is_seller_chat(chat.get("remoteJid", "")):
            mw.output(mw.i18n.t("commerce_catalog_unavailable"))
            return
        self._show_catalog(chat)

    def _order_create_shortcut(self, chat):
        chat = self._commerce_chat(chat)
        mw = self.main_window
        account = mw.commerce_account()
        if chat is None or account is None or not account.business:
            mw.output(mw.i18n.t("commerce_order_create_unavailable"))
            return
        self._show_order_create(chat)

    def _on_accel_catalog_list(self, event=None):
        self._catalog_shortcut(self._selected_chat_from_list())

    def _on_accel_catalog(self, event=None):
        self._catalog_shortcut(self.conversation)

    def _on_accel_order_create_list(self, event=None):
        self._order_create_shortcut(self._selected_chat_from_list())

    def _on_accel_order_create(self, event=None):
        self._order_create_shortcut(self.conversation)

    def _chat_name(self, chat):
        return self.main_window._resolve_contact_name(chat) or self.main_window.i18n.t("unknown_contact")

    def _show_modal(self, dialog):
        try:
            dialog.ShowModal()
        finally:
            dialog.Destroy()

    def _show_catalog(self, chat):
        from ui.dialogs.cart_dialog import CartDialog
        mw = self.main_window
        self._show_modal(CartDialog(mw, mw, mw.commerce_chat_id(chat.get("remoteJid", "")), self._chat_name(chat)))

    def _show_order_create(self, chat):
        from ui.dialogs.order_create_dialog import OrderCreateDialog
        mw = self.main_window
        if not mw.is_business_account():
            return
        self._show_modal(OrderCreateDialog(mw, mw, mw.commerce_chat_id(chat.get("remoteJid", "")),
                                           self._chat_name(chat)))

    def _can_view_order(self, msg):
        jid = (self.conversation or {}).get("remoteJid", "")
        return bool(jid) and not jid.endswith(("@g.us", "@broadcast")) and is_order_message(msg)

    def _show_order(self, msg):
        if not self._can_view_order(msg):
            return
        from ui.dialogs.order_dialog import OrderDialog
        mw = self.main_window
        jid = self.conversation.get("remoteJid", "")
        message_id = mw._serialize_msg_id(jid, msg.get("key") or {}, msg)
        self._show_modal(OrderDialog(mw, mw, message_id, self._chat_name(self.conversation)))
