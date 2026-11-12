"""A dialog or panel that asks the server things off the main thread and
hears the answer only while it is still open and on the same connection.

A dialog lives for one connection. A panel of the main window (communities,
channels) lives as long as the window, across a new pairing or engine:
`_renew_connection()` adopts the new connection when it is shown, and an
answer asked for on the old one is dropped (each request keeps the
`_closed` event of its own generation)."""

import threading
import wx
from core.group_invitation import connection_identity


class BackgroundWork:
    """Mixed into a wx.Dialog. Call _init_background(main_window) before
    creating the window; bind _on_close and _on_destroy."""

    def _init_background(self, main_window):
        self._mw = main_window
        self._i18n = main_window.i18n
        self._connection = connection_identity(main_window)
        self._closed = threading.Event()
        self._busy = False

    def _on_close(self, event):
        self._closed.set()
        self.EndModal(wx.ID_CANCEL)

    def _on_destroy(self, event):
        if event.GetEventObject() is self:
            self._closed.set()
        event.Skip()

    def _current(self):
        return (not self._closed.is_set() and not getattr(self._mw, "_shutting_down", False)
                and connection_identity(self._mw) == self._connection)

    def _renew_connection(self) -> bool:
        """A panel being shown again: when the account's connection changed
        since (new pairing, another engine), work on the new one from now on.
        True when it changed; what was in flight on the old one is dropped."""
        current = connection_identity(self._mw)
        if current == self._connection:
            return False
        self._closed.set()
        self._closed = threading.Event()
        self._connection = current
        self._busy = False
        return True

    def _run_async(self, work, done):
        """*work* off the main thread, *done(result)* back on it, None when
        the work raised; a dialog closed meanwhile hears nothing, nor a panel
        that moved to another connection."""
        closed = self._closed

        def run():
            try:
                result = work()
            except Exception:
                result = None
            if not closed.is_set():
                wx.CallAfter(self._deliver, done, result, closed)
        try:
            threading.Thread(target=run, daemon=True).start()
        except Exception:
            self._deliver(done, None, closed)

    def _deliver(self, done, result, closed=None):
        if not (closed or self._closed).is_set():
            done(result)

    def _announce(self, text):
        self._status.SetLabel(text)
        self._mw.speak_output.output(text)
