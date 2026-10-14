"""An order: what was asked for, and, for a business account that received
it, the answer.

What the account may do comes from the server with the order (can_answer,
can_update). The seller's actions could not be run on a real order while this
was written, so their outcome is taken from a fresh reading of the order, and
an update of status, which leaves no state to read back, is announced as sent
and no more.
"""

import logging
import wx
from core import commerce
from core.commerce import ACCEPTED, DECLINED, CommerceError
from ui.dialogs.labelled_control import add_labelled
from ui.dialogs.background_work import BackgroundWork

_REFUSALS = {"not_business": "commerce_not_business", "own_order": "commerce_not_business",
             "not_an_order": "commerce_order_unavailable"}


class OrderDialog(BackgroundWork, wx.Dialog):
    def __init__(self, parent, main_window, message_id, chat_name):
        self._init_background(main_window)
        i18n = self._i18n
        self._message = message_id
        self._order = None
        super().__init__(parent, title=i18n.t("commerce_order_title").format(name=chat_name),
                         size=(560, 520), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        sizer = wx.BoxSizer(wx.VERTICAL)

        def labelled(key, make, proportion):
            return add_labelled(self, sizer, i18n.t(key), make, proportion)

        self._items = labelled("commerce_order_items", lambda: wx.ListBox(self, style=wx.LB_SINGLE), 2)
        self._details = labelled("commerce_order_details", lambda: wx.TextCtrl(
            self, style=wx.TE_READONLY | wx.TE_MULTILINE), 1)
        self._status = wx.StaticText(self, label="")
        sizer.Add(self._status, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        buttons = wx.WrapSizer(wx.HORIZONTAL)

        def button(key, handler, button_id=wx.ID_ANY):
            control = wx.Button(self, button_id, label=i18n.t(key))
            control.Bind(wx.EVT_BUTTON, handler)
            buttons.Add(control, 0, wx.ALL, 4)
            return control

        self._accept = button("commerce_order_accept", self._on_accept)
        self._decline = button("commerce_order_decline", self._on_decline)
        self._update = button("commerce_order_update_status", self._on_update)
        self._refresh = button("commerce_refresh", self._reload)
        button("close", self._on_close, wx.ID_CANCEL)
        sizer.Add(buttons, 0, wx.EXPAND | wx.ALL, 4)
        self.SetSizer(sizer)
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)
        self._update_buttons()
        self._items.SetFocus()
        self.CentreOnParent()
        wx.CallAfter(self._reload)

    # ── questions to the user (answered by a test without a window) ───────

    def _ask_note(self, prompt):
        """An optional note; None when the user cancelled."""
        dialog = wx.TextEntryDialog(self, prompt, self.GetTitle(), "")
        try:
            dialog.SetMaxLength(commerce.NOTE_LIMIT)
            return dialog.GetValue() if dialog.ShowModal() == wx.ID_OK else None
        finally:
            dialog.Destroy()

    def _choose(self, prompt, labels):
        dialog = wx.SingleChoiceDialog(self, prompt, self.GetTitle(), labels)
        try:
            return dialog.GetSelection() if dialog.ShowModal() == wx.ID_OK else None
        finally:
            dialog.Destroy()

    # ── state ─────────────────────────────────────────────────────────────

    def _set_busy(self, busy):
        self._busy = busy
        self._update_buttons()

    def _update_buttons(self):
        free = not self._busy and self._current()
        order = self._order if free else None
        self._refresh.Enable(free)
        lost_focus = False
        # Hidden, not greyed out, for anyone who can never use them.
        for control, shown in ((self._accept, bool(order and order.can_answer)),
                               (self._decline, bool(order and order.can_answer)),
                               (self._update, bool(order and order.can_update))):
            visible = shown or (self._busy and control.IsShown())
            lost_focus = lost_focus or (not visible and control.IsShown() and control.HasFocus())
            control.Show(visible)
            control.Enable(shown)
        self.Layout()
        if lost_focus:
            # An answered order hides Accept and Decline: the focus goes to
            # what it now says, not to a button nobody can see.
            self._details.SetFocus()

    def describe(self, order):
        i18n = self._i18n
        separator = i18n.t("decimal_separator")
        lines = [i18n.t("commerce_order_sent" if order.mine else "commerce_order_received")]
        state = {ACCEPTED: "commerce_order_state_accepted", DECLINED: "commerce_order_state_declined"}.get(
            order.status, "commerce_order_state_waiting")
        lines.append(i18n.t("commerce_order_state").format(state=i18n.t(state)))
        lines.append(commerce.items_label(sum(item.quantity for item in order.items), i18n))
        for key, value in (("commerce_subtotal", order.subtotal), ("commerce_total", order.total)):
            money = commerce.format_money(value, order.currency, separator)
            if money and (key == "commerce_total" or order.subtotal != order.total):
                lines.append(i18n.t(key).format(amount=money))
        if order.note:
            lines.append(i18n.t("commerce_order_note").format(note=order.note))
        return "\n".join(lines)

    def _show(self, order):
        self._order = order
        self._items.Freeze()
        try:
            self._items.Set([commerce.item_label(item, self._i18n) for item in order.items])
            if order.items:
                self._items.SetSelection(0)
        finally:
            self._items.Thaw()
        self._details.ChangeValue(self.describe(order))
        self._update_buttons()

    def _read(self):
        """The order, or the refusal's code."""
        try:
            return commerce.read_order(self._connection, self._message)
        except CommerceError as error:
            return error.code

    def _reload(self, event=None):
        if self._busy or not self._current():
            return
        self._set_busy(True)
        self._status.SetLabel(self._i18n.t("loading"))
        self._run_async(self._read, self._finish_reload)

    def _finish_reload(self, order):
        self._set_busy(False)
        if not isinstance(order, commerce.Order) or not self._current():
            logging.warning("[commerce] order_read_failed")
            self._announce(self._i18n.t(_REFUSALS.get(order, "commerce_unavailable")))
            return
        self._show(order)
        self._announce(self._details.GetValue().replace("\n", ". "))

    # ── the seller's answer ───────────────────────────────────────────────

    def _answer(self, work, expected, done_key):
        """Send the answer once, then read the order again: it is announced as
        given only when the order shows it."""
        if self._busy or not self._current():
            return
        self._set_busy(True)
        self._status.SetLabel(self._i18n.t("loading"))

        def run():
            try:
                work()
            except CommerceError as error:
                refused = error.code
            else:
                refused = ""
            return refused, self._read()
        self._run_async(run, lambda outcome: self._finish_answer(outcome, expected, done_key))

    def _finish_answer(self, outcome, expected, done_key):
        self._set_busy(False)
        refused, order = outcome if outcome else ("unavailable", None)
        i18n = self._i18n
        if isinstance(order, commerce.Order) and self._current():
            self._show(order)
        if isinstance(order, commerce.Order) and expected is not None and order.status == expected:
            self._announce(i18n.t(done_key))
        elif refused in _REFUSALS:
            self._announce(i18n.t(_REFUSALS[refused]))
        elif refused:
            logging.warning("[commerce] order_answer_failed")
            self._announce(i18n.t("commerce_order_answer_failed"))
        elif expected is None:
            # A status update leaves nothing in the order to read back.
            self._announce(i18n.t(done_key))
        else:
            logging.warning("[commerce] order_answer_unconfirmed")
            self._announce(i18n.t("commerce_order_answer_unconfirmed"))

    def _on_accept(self, event):
        if not (self._order and self._order.can_answer):
            return
        connection, message = self._connection, self._message
        self._answer(lambda: commerce.accept_order(connection, message), ACCEPTED, "commerce_order_accepted")

    def _on_decline(self, event):
        if not (self._order and self._order.can_answer):
            return
        note = self._ask_note(self._i18n.t("commerce_order_decline_note"))
        if note is None:
            return
        connection, message = self._connection, self._message
        self._answer(lambda: commerce.decline_order(connection, message, note), DECLINED,
                     "commerce_order_declined")

    def _on_update(self, event):
        if not (self._order and self._order.can_update):
            return
        i18n = self._i18n
        index = self._choose(i18n.t("commerce_order_status_prompt"),
                             [i18n.t(f"commerce_status_{status}") for status in commerce.ORDER_STATUSES])
        if index is None:
            return
        note = self._ask_note(i18n.t("commerce_order_status_note"))
        if note is None:
            return
        status = commerce.ORDER_STATUSES[index]
        connection, message = self._connection, self._message
        self._answer(lambda: commerce.update_order(connection, message, status, note), None,
                     "commerce_order_status_sent")
