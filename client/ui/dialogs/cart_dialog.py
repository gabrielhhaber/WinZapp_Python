"""A business's catalog and the buyer's cart for it.

The cart lives in WhatsApp Web's own table; every change here asks the server
and shows the cart it answers. Sending is done once: a lost answer may follow
an order that was sent, so the dialog never offers a second try on its own.
"""

import logging
import wx
from core import commerce
from core.commerce import CommerceError
from ui.dialogs.labelled_control import add_labelled
from ui.dialogs.background_work import BackgroundWork

_REFUSALS = {"cart_disabled": "commerce_cart_disabled", "product_unavailable": "commerce_product_unavailable",
             "cart_empty": "commerce_cart_empty"}


class CartDialog(BackgroundWork, wx.Dialog):
    def __init__(self, parent, main_window, chat_id, seller_name):
        self._init_background(main_window)
        i18n = self._i18n
        self._chat = chat_id
        self._seller = seller_name
        self._catalog = None
        self._cart = commerce.Cart((), None, "")
        self._sent = False
        self._unconfirmed = False
        super().__init__(parent, title=i18n.t("commerce_catalog_title").format(name=seller_name),
                         size=(620, 620), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        sizer = wx.BoxSizer(wx.VERTICAL)

        def labelled(key, make, proportion):
            return add_labelled(self, sizer, i18n.t(key), make, proportion)

        self._products = labelled("commerce_products_list", lambda: wx.ListBox(self, style=wx.LB_SINGLE), 2)
        self._products.Bind(wx.EVT_LISTBOX, lambda event: self._update_buttons())
        self._items = labelled("commerce_cart_list", lambda: wx.ListBox(self, style=wx.LB_SINGLE), 1)
        self._items.Bind(wx.EVT_LISTBOX, lambda event: self._update_buttons())
        self._note = labelled("commerce_cart_note", lambda: wx.TextCtrl(self), 0)
        self._note.SetMaxLength(commerce.NOTE_LIMIT)
        self._status = wx.StaticText(self, label="")
        sizer.Add(self._status, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        buttons = wx.WrapSizer(wx.HORIZONTAL)

        def button(key, handler, button_id=wx.ID_ANY):
            control = wx.Button(self, button_id, label=i18n.t(key))
            control.Bind(wx.EVT_BUTTON, handler)
            buttons.Add(control, 0, wx.ALL, 4)
            return control

        self._add = button("commerce_cart_add", self._on_add)
        self._change = button("commerce_cart_change", self._on_change)
        self._remove = button("commerce_cart_remove", self._on_remove)
        self._clear = button("commerce_cart_clear", self._on_clear)
        self._send = button("commerce_cart_send", self._on_send)
        self._refresh = button("commerce_refresh", self._reload)
        button("close", self._on_close, wx.ID_CANCEL)
        sizer.Add(buttons, 0, wx.EXPAND | wx.ALL, 4)
        self.SetSizer(sizer)
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)
        self._update_buttons()
        self._products.SetFocus()
        self.CentreOnParent()
        wx.CallAfter(self._reload)

    # ── questions to the user (answered by a test without a window) ───────

    def _ask_quantity(self, name, current):
        dialog = wx.NumberEntryDialog(
            self, self._i18n.t("commerce_quantity_prompt").format(name=name),
            self._i18n.t("commerce_quantity_label"), self._i18n.t("commerce_quantity_title"),
            current, 1, commerce.QUANTITY_LIMIT)
        try:
            return dialog.GetValue() if dialog.ShowModal() == wx.ID_OK else None
        finally:
            dialog.Destroy()

    def _confirm(self, text):
        dialog = wx.MessageDialog(self, text, self.GetTitle(), wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION)
        try:
            return dialog.ShowModal() == wx.ID_YES
        finally:
            dialog.Destroy()

    # ── state ─────────────────────────────────────────────────────────────

    def _product(self):
        index = self._products.GetSelection()
        products = self._catalog.products if self._catalog else ()
        return products[index] if 0 <= index < len(products) else None

    def _item(self):
        index = self._items.GetSelection()
        return self._cart.items[index] if 0 <= index < len(self._cart.items) else None

    def _set_busy(self, busy):
        self._busy = busy
        self._update_buttons()

    def _carts(self):
        """A cart is only offered to a business that takes carts, and not
        after an order went out from this window."""
        return bool(not self._busy and self._current() and self._catalog is not None
                    and self._catalog.cart_enabled and not self._sent)

    def _update_buttons(self):
        free = not self._busy and self._current()
        carts = self._carts()
        product = self._product()
        self._refresh.Enable(free)
        self._add.Enable(bool(carts and product is not None and product.available))
        self._change.Enable(bool(carts and self._item() is not None))
        self._remove.Enable(bool(carts and self._item() is not None))
        self._clear.Enable(bool(carts and self._cart.items))
        self._send.Enable(bool(carts and self._cart.items))
        self._note.Enable(bool(carts))

    def _fill(self, control, labels, keep):
        selected = control.GetSelection()
        control.Freeze()
        try:
            control.Set(labels)
            if labels:
                control.SetSelection(keep if 0 <= keep < len(labels) else min(max(selected, 0), len(labels) - 1))
        finally:
            control.Thaw()

    def _show_cart(self, cart, keep_id=""):
        self._cart = cart
        keep = next((position for position, item in enumerate(cart.items) if item.id == keep_id), -1)
        self._fill(self._items, [commerce.item_label(item, self._i18n) for item in cart.items], keep)
        self._update_buttons()

    # ── reading ───────────────────────────────────────────────────────────

    def _reload(self, event=None):
        if self._busy or not self._current():
            return
        self._set_busy(True)
        self._status.SetLabel(self._i18n.t("loading"))
        connection, chat = self._connection, self._chat
        self._run_async(lambda: commerce.read_catalog(connection, chat), self._finish_reload)

    def _finish_reload(self, catalog):
        self._set_busy(False)
        i18n = self._i18n
        if catalog is None or not self._current():
            logging.warning("[commerce] catalog_read_failed")
            self._announce(i18n.t("commerce_unavailable"))
            return
        product = self._product()
        self._catalog = catalog
        keep = next((position for position, entry in enumerate(catalog.products)
                     if product is not None and entry.id == product.id), -1)
        self._fill(self._products, [commerce.product_label(entry, i18n) for entry in catalog.products], keep)
        # An order whose sending was not confirmed stays blocked across a
        # refresh: the server may have sent it, and a second one is worse than none.
        self._sent = self._unconfirmed
        self._show_cart(catalog.cart)
        if self._unconfirmed and catalog.cart_enabled:
            if not catalog.cart.items:
                # A cart is emptied when its order goes out, so reading it back
                # empty is the confirmation the first answer could not give.
                self._mw.speak_output.output(i18n.t("commerce_order_sent_done"))
                self._closed.set()
                self.EndModal(wx.ID_OK)
            else:
                self._announce(i18n.t("commerce_order_send_unconfirmed"))
        elif not catalog.has_catalog:
            self._announce(i18n.t("commerce_no_catalog"))
        elif not catalog.cart_enabled:
            self._announce(i18n.t("commerce_products_count").format(count=len(catalog.products))
                           + " " + i18n.t("commerce_cart_disabled"))
        else:
            self._announce(i18n.t("commerce_products_count").format(count=len(catalog.products))
                           + " " + commerce.cart_summary(catalog.cart, i18n))

    # ── cart ──────────────────────────────────────────────────────────────

    def _change_cart(self, work, keep_id=""):
        """Run one cart change off the main thread and show the cart the
        server answers, refusal or not."""
        if self._busy or not self._current():
            return
        self._set_busy(True)
        self._status.SetLabel(self._i18n.t("loading"))

        def run():
            try:
                return ("ok", work())
            except CommerceError as error:
                return (error.code, error.cart)
        self._run_async(run, lambda outcome: self._finish_change(outcome, keep_id))

    def _finish_change(self, outcome, keep_id):
        self._set_busy(False)
        code, cart = outcome if outcome else ("unavailable", None)
        if cart is not None and self._current():
            self._show_cart(cart, keep_id)
        if code == "ok":
            self._announce(commerce.cart_summary(self._cart, self._i18n))
        else:
            logging.warning("[commerce] cart_change code=%s", code if code in _REFUSALS else "unavailable")
            self._announce(self._i18n.t(_REFUSALS.get(code, "commerce_unavailable")))

    def _set_quantity(self, product_id, name, current):
        quantity = self._ask_quantity(name or self._i18n.t("commerce_unnamed_product"), current)
        if quantity is None:
            return
        connection, chat = self._connection, self._chat
        self._change_cart(lambda: commerce.set_cart_quantity(connection, chat, product_id, quantity), product_id)

    def _on_add(self, event):
        product = self._product()
        if not self._carts() or product is None or not product.available:
            return
        in_cart = next((item.quantity for item in self._cart.items if item.id == product.id), 0)
        self._set_quantity(product.id, product.name, min(in_cart + 1, commerce.QUANTITY_LIMIT))

    def _on_change(self, event):
        item = self._item()
        if self._carts() and item is not None:
            self._set_quantity(item.id, item.name, item.quantity)

    def _on_remove(self, event):
        item = self._item()
        if not self._carts() or item is None:
            return
        connection, chat = self._connection, self._chat
        self._change_cart(lambda: commerce.remove_from_cart(connection, chat, item.id))

    def _on_clear(self, event):
        if (not self._carts() or not self._cart.items
                or not self._confirm(self._i18n.t("commerce_cart_clear_confirm"))):
            return
        connection, chat = self._connection, self._chat
        self._change_cart(lambda: commerce.clear_cart(connection, chat))

    def _on_send(self, event):
        if not self._carts() or not self._cart.items:
            return
        i18n = self._i18n
        if not self._confirm(i18n.t("commerce_cart_send_confirm").format(
                name=self._seller, summary=commerce.cart_summary(self._cart, i18n))):
            return
        # One attempt: whatever comes back, sending again is the user's call,
        # after looking at the chat.
        self._sent = True
        self._set_busy(True)
        self._status.SetLabel(i18n.t("loading"))
        connection, chat, note = self._connection, self._chat, self._note.GetValue()

        def run():
            try:
                return ("ok", commerce.submit_cart(connection, chat, note))
            except CommerceError as error:
                return (error.code, error.cart)
        self._run_async(run, self._finish_send)

    def _finish_send(self, outcome):
        self._set_busy(False)
        code, cart = outcome if outcome else ("unavailable", None)
        i18n = self._i18n
        if code == "ok":
            self._mw.speak_output.output(i18n.t("commerce_order_sent_done"))
            self._closed.set()
            self.EndModal(wx.ID_OK)
            return
        if cart is not None and self._current():
            self._show_cart(cart)
        if code in _REFUSALS:
            # A definite no from the server: nothing was sent, trying again is fine.
            self._sent = False
            self._update_buttons()
            self._announce(i18n.t(_REFUSALS[code]))
        else:
            logging.warning("[commerce] order_send_unconfirmed")
            self._unconfirmed = True
            self._announce(i18n.t("commerce_order_send_unconfirmed"))
            # Look at the cart once: empty means the order did go out.
            self._reload()
