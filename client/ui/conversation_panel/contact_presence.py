"""Refresh only the open contact; never change focus or announce a timer tick."""
import wx


class ContactPresencePanelMixin:
    def _stop_contact_presence(self):
        self._contact_presence_visit = getattr(self, "_contact_presence_visit", 0) + 1
        timer = getattr(self, "_contact_presence_timer", None)
        if timer is not None:
            timer.Stop()
        self._contact_presence_timer = None

    def _start_contact_presence(self):
        self._stop_contact_presence()
        if (self.conversation is None
                or not self.conversation.get("remoteJid", "").endswith(("@s.whatsapp.net", "@c.us", "@lid"))):
            return
        visit = self._contact_presence_visit

        def tick():
            if (visit != self._contact_presence_visit or self.conversation is None
                    or getattr(self.main_window, "_shutting_down", False)):
                return
            self.main_window._refresh_open_contact_presence()
            self._contact_presence_timer = wx.CallLater(30_000, tick)

        # Navigation finishes showing the panel before the first request.
        wx.CallAfter(tick)
