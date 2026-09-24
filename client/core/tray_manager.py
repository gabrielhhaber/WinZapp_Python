"""
WinZapp System Tray Icon
------------------------
Manages the notification-area icon that persists while WinZapp is running,
including the animated tooltip with unread-message counts and the
right-click context menu (Open / Exit).
"""

import os
import time
import wx
import wx.adv
from core.i18n import I18n
from app_paths import resource_path

_ID_OPEN    = wx.NewIdRef(count=1)
_ID_OFFLINE = wx.NewIdRef(count=1)
_ID_EXIT    = wx.NewIdRef(count=1)


class TrayIcon(wx.adv.TaskBarIcon):
    """Persistent system-tray icon for WinZapp."""

    def __init__(self, main_window):
        super().__init__(wx.adv.TBI_DOCK)
        self.main_window = main_window
        self.i18n = I18n(main_window)
        self.i18n.get_language()

        self._icon = self._load_icon()
        self._last_activate = 0.0  # debounce timestamp

        self.Bind(wx.adv.EVT_TASKBAR_LEFT_DCLICK, self._on_activate)
        self.Bind(wx.adv.EVT_TASKBAR_LEFT_UP,    self._on_activate)
        self.Bind(wx.EVT_MENU, self._on_open,           id=_ID_OPEN)
        self.Bind(wx.EVT_MENU, self._on_toggle_offline, id=_ID_OFFLINE)
        self.Bind(wx.EVT_MENU, self._on_exit,           id=_ID_EXIT)

        self.update_tooltip()

    # ── Icon loading ──────────────────────────────────────────────────────────

    def _load_icon(self):
        """Try to load WinZapp.ico; fall back to a generated 'W' bitmap."""
        for candidate in [
            resource_path("WinZapp.ico"),
            resource_path("data", "WinZapp.ico"),
        ]:
            if os.path.isfile(candidate):
                icon = wx.Icon(candidate, wx.BITMAP_TYPE_ICO)
                if icon.IsOk():
                    return icon
        return self._make_fallback_icon()

    @staticmethod
    def _make_fallback_icon():
        """Create a simple 16×16 icon with a white 'W' on green background."""
        size = 16
        bmp  = wx.Bitmap(size, size, 24)
        dc   = wx.MemoryDC(bmp)
        dc.SetBackground(wx.Brush(wx.Colour(37, 211, 102)))  # WhatsApp green
        dc.Clear()
        dc.SetTextForeground(wx.WHITE)
        dc.SetFont(wx.Font(
            9, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD
        ))
        tw, th = dc.GetTextExtent("W")
        dc.DrawText("W", (size - tw) // 2, (size - th) // 2)
        dc.SelectObject(wx.NullBitmap)
        icon = wx.Icon()
        icon.CopyFromBitmap(bmp)
        return icon

    # ── Tooltip ───────────────────────────────────────────────────────────────

    def update_tooltip(self):
        """Rebuild the tooltip from current unread counts and refresh the icon.

        On Windows 11, NVDA accumulates tooltip text from successive SetIcon
        calls on the same icon slot.  Removing the icon first ensures only the
        new tooltip text is announced.  Skip the Remove/Set cycle when the
        tooltip text is unchanged to avoid unnecessary flicker and NVDA chatter.
        """
        self.i18n.get_language()
        total, names = self._get_unread_info()
        tooltip = self._build_tooltip(total, names)
        if tooltip == getattr(self, "_last_tooltip", None):
            return
        self._last_tooltip = tooltip
        try:
            self.RemoveIcon()
        except Exception:
            pass
        try:
            self.SetIcon(self._icon, tooltip)
        except Exception:
            pass

    def _get_unread_info(self):
        """Return (total_unread_count, [chat_names_with_unread])."""
        from core.utils import effective_unread_count
        total = 0
        names = []
        mw = self.main_window
        # Only return unread info if initial sync has completed
        if getattr(mw, "_sync_completed", False):
            deleted = getattr(mw, "_deleted_chats", set())
            for jid, chat in mw.chats.items():
                # Archived conversations get their own unread indicator on the
                # "Conversas arquivadas" nav item — same exclusion as the
                # window title (_update_title()), so the tray tooltip/icon
                # doesn't announce unread messages for chats the user can't
                # see anywhere in the main conversation list.
                if (
                    jid in deleted
                    or mw.is_chat_archived(jid)
                    or getattr(mw, "is_chat_locked", lambda _jid: False)(jid)
                ):
                    continue
                unread = effective_unread_count(chat)
                if unread > 0:
                    total += unread
                    name = (
                        mw._resolve_contact_name(chat)
                        or mw.find_name_through_messages(chat)
                        or chat.get("name", "")
                        or chat.get("pushName", "")
                        or mw.find_jid_through_messages(chat)
                        or jid.split("@")[0]
                    )
                    if name:
                        names.append(name)
        return total, names

    def _build_tooltip(self, total, names):
        """
        Build a tooltip like:
          'WinZapp | sincronizando | 35 mensagens não lidas de mãe, jogos'
          'WinZapp | 1 mensagem não lida de mãe'
          'WinZapp | nenhuma mensagem não lida'
        Truncated to 127 characters (Windows NOTIFYICONDATA.szTip limit).
        """
        i18n   = self.i18n
        mw     = self.main_window
        # Multi-account: lead with the account name so the screen reader and the
        # tray tooltip make clear WHICH account this icon is (plan Zad 4.4).
        # With a single connected account the name is redundant (nothing to
        # distinguish), so it is only shown when more than one account is paired.
        app_label = "WinZapp"
        if getattr(mw, "_is_multi_account", lambda: False)():
            app_label = getattr(mw, "account_name", None) or "WinZapp"
        if app_label != "WinZapp":
            app_label = f"WinZapp — {app_label}"
        parts  = [app_label]
        if getattr(self.main_window, "offline_mode", False):
            parts.append(i18n.t("tray_offline_mode"))
        status = getattr(self.main_window, "_tray_status", "")
        if status:
            parts.append(status)
        prefix = " | ".join(parts)
        prefix += " | "

        if total == 0:
            return (prefix + i18n.t("tray_no_unread"))[:127]

        and_word = i18n.t("and")
        if len(names) == 1:
            names_str = names[0]
        elif len(names) == 2:
            names_str = f"{names[0]} {and_word} {names[1]}"
        else:
            names_str = ", ".join(names[:-1]) + f" {and_word} {names[-1]}"

        if total == 1:
            text = i18n.t("tray_unread_singular").format(names=names_str)
        else:
            text = i18n.t("tray_unread_plural").format(count=total, names=names_str)

        full = prefix + text
        if len(full) > 127:
            return full[:125] + "…"
        return full

    # ── Context menu ──────────────────────────────────────────────────────────

    def CreatePopupMenu(self):
        """Called by wx when the user right-clicks the tray icon."""
        i18n = self.i18n
        menu = wx.Menu()
        menu.Append(_ID_OPEN, i18n.t("tray_open"))
        offline_item = menu.AppendCheckItem(_ID_OFFLINE, i18n.t("tray_offline_mode"))
        offline_item.Check(bool(self.main_window.offline_mode))
        menu.AppendSeparator()
        menu.Append(_ID_EXIT, i18n.t("tray_exit"))
        return menu

    # ── Event handlers ────────────────────────────────────────────────────────

    def _on_activate(self, event):
        """Left-click, double-click, or keyboard Enter on tray icon.

        Both EVT_TASKBAR_LEFT_UP and EVT_TASKBAR_LEFT_DCLICK fire on a
        double-click, so debounce to a 400ms window to avoid restoring twice.
        """
        now = time.monotonic()
        if now - self._last_activate < 0.4:
            return
        self._last_activate = now
        wx.CallAfter(self.main_window.restore_window)

    def _on_open(self, event):
        wx.CallAfter(self.main_window.restore_window)

    def _on_toggle_offline(self, event):
        wx.CallAfter(self.main_window.toggle_offline_mode)

    def _on_exit(self, event):
        wx.CallAfter(self.main_window.quit_all_accounts)

    # ── Language refresh ──────────────────────────────────────────────────────

    def refresh_labels(self):
        """Called after a language change."""
        self.i18n.get_language()
        self.update_tooltip()
