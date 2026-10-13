"""A dialog that asks the server things off the main thread and hears the
answer only while it is still open and on the same connection."""

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

    def _run_async(self, work, done):
        """*work* off the main thread, *done(result)* back on it, None when
        the work raised; a dialog closed meanwhile hears nothing."""
        def run():
            try:
                result = work()
            except Exception:
                result = None
            if not self._closed.is_set():
                wx.CallAfter(self._deliver, done, result)
        try:
            threading.Thread(target=run, daemon=True).start()
        except Exception:
            self._deliver(done, None)

    def _deliver(self, done, result):
        if not self._closed.is_set():
            done(result)

    def _announce(self, text):
        self._status.SetLabel(text)
        self._mw.speak_output.output(text)
