"""Weak, visit-scoped active-reader ownership; no new timer or own-status write."""
from weakref import ref

import wx

from core.contact_presence_refresh import same_read_contact
from core.conversation_view import conversation_in_view


def capture_personal_reader_visit(dialog):
    panel = getattr(dialog._mw, "conversations_panel", None)
    dialog._personal_presence_visit = id(panel), getattr(panel, "_contact_presence_visit", 0)
    # Capture wx visibility on the constructing UI thread, never in the worker.
    dialog._personal_presence_in_view = conversation_in_view(panel)
    dialog._personal_presence_context = (
        getattr(dialog._mw, "_contact_presence_epoch", 0),
        tuple(getattr(dialog._mw, key, None) for key in ("wpp_server", "wpp_port", "token")))


class PersonalPresenceReader:
    def __init__(self, view, dialog):
        self.view, self.dialog = ref(view), ref(dialog)
        # Never acquire a successor visit from a late initial population or
        # another tab/profile rerender of an old dialog.
        self.visit = getattr(dialog, "_personal_presence_visit", None)
        self.handler = self.on_activate
        dialog.Bind(wx.EVT_ACTIVATE, self.handler)

    def stop(self):
        dialog = self.dialog()
        if dialog is not None:
            try:
                dialog.Unbind(wx.EVT_ACTIVATE, handler=self.handler)
            except RuntimeError:
                pass

    def matches(self, jid):
        view, dialog = self.view(), self.dialog()
        if (view is None or not view.active or dialog is None
                or (view.closed is not None and view.closed.is_set())):
            return False
        owner = view.owner
        panel = getattr(owner, "conversations_panel", None)
        if self.visit != (id(panel), getattr(panel, "_contact_presence_visit", 0)):
            return False
        try:
            return bool(dialog.IsShown() and dialog.IsActive()
                        and same_read_contact(owner, view.jid, jid))
        except RuntimeError:
            return False

    def request(self):
        view = self.view()
        if view is not None and self.matches(view.jid):
            from main_window.presence_visibility import refresh_missing_presence
            refresh_missing_presence(view.owner, view.jid)

    def on_activate(self, event):
        active = event.GetActive()
        event.Skip()
        if active:
            wx.CallAfter(self.request)


def track_personal_reader(view, dialog):
    # Old plain render-only fixtures have no window lifecycle. They cannot
    # authorize a read. A real wx dialog is weak-referenceable and binds events.
    if not view.active or not callable(getattr(dialog, "Bind", None)):
        return
    reader = view.reader = PersonalPresenceReader(view, dialog)
    wx.CallAfter(reader.request)  # Also cover activation before first populate.


def active_personal_reader(owner, jid):
    return any(getattr(view, "reader", None) is not None and view.reader.matches(jid)
               for view in tuple(getattr(owner, "_personal_presence_views", ())))
