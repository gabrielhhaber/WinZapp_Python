"""FontScaleMixin — part of MainWindow (see main_window/__init__.py).

Settings > Interface > Font size and its three shortcuts (bigger, smaller,
default) for low-vision users. The arithmetic and the walk over the native
controls are in core/font_scale.py; this mixin owns the settings value, the
windows it is applied to and the spoken confirmation.

Windows opened later get the scale when they are shown: wx has no "after
create" hook for dialogs, and EVT_INIT_DIALOG is consumed by the dialog's own
default handler before it could reach the application, so one EVT_SHOW handler
on the application sees every top-level window as it appears. At 100 % (the
default) that handler returns at once and nothing is changed anywhere.
"""

import logging

import wx

from core import font_scale


class FontScaleMixin:
    def install_font_scale(self):
        """Once, after the window is built: follow every window that is shown
        and apply the saved scale."""
        app = wx.GetApp()
        if app is not None and not getattr(self, "_font_scale_hooked", False):
            app.Bind(wx.EVT_SHOW, self._on_font_scale_show)
            self._font_scale_hooked = True
        self.apply_font_scale()

    def _on_font_scale_show(self, event):
        event.Skip()
        try:
            window = event.GetEventObject()
            if (not event.IsShown() or window is None or window is self
                    or not window.IsTopLevel()
                    or font_scale.scale_of(self.settings) == font_scale.DEFAULT):
                return
            self._scale_top_level(window, font_scale.scale_of(self.settings))
        except Exception:
            logging.exception("[font_scale] scaling a window that was just shown failed")

    def _screen_size(self, window):
        """(width, height) of the work area the window is on, or None."""
        try:
            display = wx.Display.GetFromWindow(window)
            area = wx.Display(display if display != wx.NOT_FOUND else 0).GetClientArea()
            return area.GetWidth(), area.GetHeight()
        except Exception:
            return None

    def _scale_top_level(self, window, percent):
        font_scale.apply_scale(window, percent, wx.Window.FindFocus)
        if window is self:
            window.Layout()
        elif font_scale.fit_to_content(window, self._screen_size(window)):
            window.CentreOnParent()

    def apply_font_scale(self):
        """Give every window that exists now the saved font scale."""
        percent = font_scale.scale_of(self.settings)
        if percent == font_scale.DEFAULT and not getattr(self, "_font_scale_active", False):
            return      # never scaled in this run: not even a walk over the controls
        self._font_scale_active = percent != font_scale.DEFAULT
        for window in list(wx.GetTopLevelWindows()):
            try:
                self._scale_top_level(window, percent)
            except RuntimeError:     # destroyed while being walked
                continue
            except Exception:
                logging.exception("[font_scale] scaling a window failed")

    def change_font_scale(self, direction):
        """Bigger (1), smaller (-1) or the default (0), saved and applied now,
        confirmed in a few words."""
        current = font_scale.scale_of(self.settings)
        chosen = font_scale.step(current, direction)
        if chosen != current:
            self.settings.setdefault(font_scale.SECTION, {})[font_scale.SETTING] = chosen
            self.save_settings()
            self.apply_font_scale()
        self.speak_output.output(self.i18n.t("font_scale_announce").format(percent=chosen))

    def _on_font_bigger(self, event=None):
        self.change_font_scale(1)

    def _on_font_smaller(self, event=None):
        self.change_font_scale(-1)

    def _on_font_reset(self, event=None):
        self.change_font_scale(0)
