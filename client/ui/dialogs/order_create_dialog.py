"""A business account writes an order for a customer: items with a name, a
price and a quantity. Sent once through the library's own route."""

import logging
import wx
from core import commerce
from core.commerce import CommerceError
from ui.dialogs.labelled_control import add_labelled
from ui.accessible import split_mnemonic
from ui.dialogs.background_work import BackgroundWork


def new_item_label(item, i18n):
    name, price, quantity = item
    money = commerce.format_money(price, "", i18n.t("decimal_separator"))
    return f"{quantity} × {name}, {i18n.t('commerce_each').format(amount=money)}"


class OrderItemDialog(wx.Dialog):
    """Name, unit price and quantity of one item."""

    def __init__(self, parent, main_window):
        self._mw = main_window
        self._i18n = i18n = main_window.i18n
        self.item = None
        super().__init__(parent, title=i18n.t("commerce_item_title"), style=wx.DEFAULT_DIALOG_STYLE)
        sizer = wx.BoxSizer(wx.VERTICAL)

        def field(key, make):
            return add_labelled(self, sizer, i18n.t(key), make)

        self._name = field("commerce_item_name", lambda: wx.TextCtrl(self))
        self._name.SetMaxLength(120)
        self._price = field("commerce_item_price", lambda: wx.TextCtrl(self))
        self._quantity = field("commerce_item_quantity", lambda: wx.SpinCtrl(
            self, min=1, max=commerce.QUANTITY_LIMIT, initial=1))
        buttons = wx.StdDialogButtonSizer()
        ok = wx.Button(self, wx.ID_OK, label=i18n.t("commerce_item_add"))
        ok.Bind(wx.EVT_BUTTON, self._on_ok)
        buttons.AddButton(ok)
        buttons.AddButton(wx.Button(self, wx.ID_CANCEL, label=i18n.t("cancel")))
        buttons.Realize()
        sizer.Add(buttons, 0, wx.EXPAND | wx.ALL, 8)
        self.SetSizerAndFit(sizer)
        self._name.SetFocus()
        self.CentreOnParent()

    def _on_ok(self, event):
        name = self._name.GetValue().strip()
        price = commerce.parse_money(self._price.GetValue())
        if not name:
            self._mw.speak_output.output(self._i18n.t("commerce_item_name_required"))
            self._name.SetFocus()
            return
        if not price:
            self._mw.speak_output.output(self._i18n.t("commerce_item_price_invalid"))
            self._price.SetFocus()
            return
        self.item = (name, price, self._quantity.GetValue())
        self.EndModal(wx.ID_OK)


class OrderCreateDialog(BackgroundWork, wx.Dialog):
    def __init__(self, parent, main_window, chat_id, customer_name):
        self._init_background(main_window)
        i18n = self._i18n
        self._chat = chat_id
        self._customer = customer_name
        self._new_items = []
        self._attempted = False
        super().__init__(parent, title=i18n.t("commerce_create_order_title").format(name=customer_name),
                         size=(520, 440), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(wx.StaticText(self, label=i18n.t("commerce_new_order_items")), 0, wx.LEFT | wx.TOP | wx.RIGHT, 8)
        self._list = wx.ListBox(self, style=wx.LB_SINGLE)
        self._list.SetName(split_mnemonic(i18n.t("commerce_new_order_items"))[0])
        self._list.Bind(wx.EVT_LISTBOX, lambda event: self._update_buttons())
        sizer.Add(self._list, 1, wx.EXPAND | wx.ALL, 8)
        self._status = wx.StaticText(self, label="")
        sizer.Add(self._status, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        buttons = wx.WrapSizer(wx.HORIZONTAL)

        def button(key, handler, button_id=wx.ID_ANY):
            control = wx.Button(self, button_id, label=i18n.t(key))
            control.Bind(wx.EVT_BUTTON, handler)
            buttons.Add(control, 0, wx.ALL, 4)
            return control

        self._add = button("commerce_new_order_add", self._on_add)
        self._remove = button("commerce_new_order_remove", self._on_remove)
        self._send = button("commerce_new_order_send", self._on_send)
        button("close", self._on_close, wx.ID_CANCEL)
        sizer.Add(buttons, 0, wx.EXPAND | wx.ALL, 4)
        self.SetSizer(sizer)
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)
        self._update_buttons()
        self._add.SetFocus()
        self.CentreOnParent()

    # ── questions to the user (answered by a test without a window) ───────

    def _ask_item(self):
        dialog = OrderItemDialog(self, self._mw)
        try:
            return dialog.item if dialog.ShowModal() == wx.ID_OK else None
        finally:
            dialog.Destroy()

    def _confirm(self, text):
        dialog = wx.MessageDialog(self, text, self.GetTitle(), wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION)
        try:
            return dialog.ShowModal() == wx.ID_YES
        finally:
            dialog.Destroy()

    # ── state ─────────────────────────────────────────────────────────────

    def _update_buttons(self):
        free = not self._busy and not self._attempted and self._current()
        self._add.Enable(free)
        self._remove.Enable(free and 0 <= self._list.GetSelection() < len(self._new_items))
        self._send.Enable(free and bool(self._new_items))

    def _total(self):
        return commerce.format_money(commerce.order_total(self._new_items), "",
                                     self._i18n.t("decimal_separator"))

    def _refill(self, select):
        self._list.Freeze()
        try:
            self._list.Set([new_item_label(item, self._i18n) for item in self._new_items])
            if self._new_items:
                self._list.SetSelection(min(max(select, 0), len(self._new_items) - 1))
        finally:
            self._list.Thaw()
        self._update_buttons()
        if self._new_items:
            self._announce(self._i18n.t("commerce_new_order_summary").format(
                count=len(self._new_items), amount=self._total()))
        else:
            self._announce(self._i18n.t("commerce_new_order_empty"))

    def _on_add(self, event):
        if self._busy or self._attempted:
            return
        item = self._ask_item()
        if item is None:
            return
        self._new_items.append(item)
        self._refill(len(self._new_items) - 1)

    def _on_remove(self, event):
        index = self._list.GetSelection()
        if self._busy or self._attempted or not 0 <= index < len(self._new_items):
            return
        del self._new_items[index]
        self._refill(index)

    def _on_send(self, event):
        if self._busy or self._attempted or not self._new_items or not self._current():
            return
        i18n = self._i18n
        if not self._confirm(i18n.t("commerce_new_order_confirm").format(
                name=self._customer, count=len(self._new_items), amount=self._total())):
            return
        # One attempt: an order may have gone out even when the answer was lost.
        self._attempted = True
        self._busy = True
        self._update_buttons()
        self._status.SetLabel(i18n.t("loading"))
        connection, chat, items = self._connection, self._chat, list(self._new_items)

        def run():
            try:
                commerce.send_order(connection, chat, items)
                return "ok"
            except CommerceError as error:
                return error.code
        self._run_async(run, self._finish_send)

    def _finish_send(self, outcome):
        self._busy = False
        self._update_buttons()
        if outcome == "ok":
            self._mw.speak_output.output(self._i18n.t("commerce_new_order_sent"))
            self._closed.set()
            self.EndModal(wx.ID_OK)
            return
        logging.warning("[commerce] order_create_unconfirmed")
        self._announce(self._i18n.t("commerce_new_order_unconfirmed"))
