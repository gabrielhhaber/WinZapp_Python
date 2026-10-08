"""LinksMixin — part of ConversationsPanel (see ui/conversation_panel/__init__.py).

Moved verbatim out of ui/conversations.py. Methods run with ``self`` bound to
the ConversationsPanel instance, so every attribute set in
ConversationsPanel.__init__/init_UI is available here.
"""

from ui.shortcut_bindings import command_key_event
import os
import pyperclip
import wx
import wx.adv
from ui.conversation_panel.text_helpers import _URL_RE


class LinksMixin:
    """The links panel of the focused message.
    """

    # ── URL / link helpers ───────────────────────────────────────────────────

    @staticmethod
    def _extract_links(text: str) -> list:
        """Return deduplicated list of URLs found in *text*."""
        matches = _URL_RE.findall(text)
        seen = set()
        out  = []
        for m in matches:
            # Strip trailing punctuation that is not part of the URL
            m = m.rstrip('.,;:!?)\'"\\>]')
            if m and m not in seen:
                seen.add(m)
                out.append(m)
        return out

    def _message_own_links(self, msg) -> list:
        """Links *msg* itself carries, never the ones inside the quote it replies to.

        A reply's rendered row ends with the quoted message's preview, so a
        link in the message being answered used to become a Tab stop of the
        reply — and, with one link of its own, pushed the pair into the
        two-or-more links list. Both belong to the quoted message's own row.
        """
        return self._extract_links(
            self._render_message_line(msg, include_quoted_preview=False)
        )

    def _update_links_panel(self, links: list):
        """Rebuild the link controls below the messages list.

        A single link keeps the existing HyperlinkCtrl tab-stop. Two or
        more are shown as one navigable list instead (issue #65) — users
        previously had to Tab/Shift+Tab through every link in a message as
        its own separate stop. Up/Down move between them (native ListCtrl
        behaviour also gives Home/End for free), and Ctrl+C copies just the
        focused link.
        """
        # Destroy all child controls except the static label (first item)
        for child in list(self._links_panel.GetChildren()):
            if child is not self._links_label:
                child.Destroy()
        # Remove all items except the first (label) from the sizer
        while self._links_sizer.GetItemCount() > 1:
            self._links_sizer.Remove(1)
        self._links_list = None
        self._link_ctrl = None

        if not links:
            self._links_panel.Hide()
            self._current_links = []
            if self.conversation_panel.IsShown():
                self.conversation_panel.Layout()
            return

        self._current_links = links
        i18n = self.main_window.i18n

        if len(links) == 1:
            self._links_label.SetLabel(i18n.t("links_section_label"))
            url = links[0]
            ctrl = wx.adv.HyperlinkCtrl(
                self._links_panel,
                id=wx.ID_ANY,
                label=url,
                url=url,
                style=wx.adv.HL_DEFAULT_STYLE,
            )
            ctrl.Bind(wx.adv.EVT_HYPERLINK, self._on_hyperlink_open)
            ctrl.Bind(wx.EVT_KEY_DOWN,  self._on_link_key_down)
            ctrl.Bind(wx.EVT_CONTEXT_MENU, self._on_link_context_menu)
            self._links_sizer.Add(ctrl, 0, wx.LEFT | wx.BOTTOM, 3)
            self._link_ctrl = ctrl
        else:
            self._links_label.SetLabel(i18n.t("links_list_label"))
            lst = wx.ListCtrl(
                self._links_panel,
                style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.LC_NO_HEADER,
            )
            lst.InsertColumn(0, i18n.t("links_list_label"), width=400)
            for url in links:
                lst.Append((url,))
            lst.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self._on_links_list_activated)
            lst.Bind(wx.EVT_KEY_DOWN, self._on_links_list_key_down)
            lst.Bind(wx.EVT_CONTEXT_MENU, self._on_link_context_menu)
            lst.Focus(0)
            lst.Select(0)
            self._links_sizer.Add(lst, 0, wx.EXPAND | wx.LEFT | wx.BOTTOM, 3)
            self._links_list = lst

        self._links_panel.Show()
        self._links_panel.Layout()
        if self.conversation_panel.IsShown():
            self.conversation_panel.Layout()

    def _link_url_for(self, window) -> str:
        """The URL a link control is showing, or "" when it is not one.

        Two shapes to recognise, because _update_links_panel() builds two: the
        HyperlinkCtrl a single link gets, and the list two or more share —
        where the URL is whichever row is selected, not the control itself.

        Identity comparison rather than isinstance: only the controls THIS
        panel built for the focused message count, and both attributes are
        cleared on every rebuild, so a stale reference can never match a live
        window.
        """
        if window is None:
            return ""
        lst = getattr(self, "_links_list", None)
        if lst is not None and window is lst:
            links = getattr(self, "_current_links", None) or []
            try:
                index = lst.GetFirstSelected()
            except Exception:
                return ""
            return links[index] if 0 <= index < len(links) else ""
        ctrl = getattr(self, "_link_ctrl", None)
        if ctrl is not None and window is ctrl:
            try:
                return ctrl.GetURL()
            except Exception:
                return ""
        return ""

    def _focused_link_url(self) -> str:
        """The URL of the link control that currently has keyboard focus, or
        "" when focus is anywhere else.

        This is what lets Ctrl+C mean the link rather than the message. The
        shortcut is an accelerator (ID_CTRL_C -> _on_accel_copy_message), and
        wxMSW translates accelerators before the focused control ever sees a
        key event, so the links list's own Ctrl+C handler below could not
        win — it was written and then silently outranked. Asking who has
        focus is the only reading available to a handler that runs first.

        Never raises. This is now the first statement of the Ctrl+C handler,
        so anything escaping it would take copying with it — and answering ""
        degrades to exactly the behaviour this replaces (copy the message),
        which is the safe direction to fail in.
        """
        try:
            return self._link_url_for(wx.Window.FindFocus())
        except Exception:
            return ""

    def _copy_focused_link(self, url: str) -> None:
        """Copy one link and say so, naming it.

        The address is spoken because a bare "link copiado" is ambiguous
        exactly where this is used: on a message carrying several links, the
        confirmation is the only way a screen-reader user can tell which of
        them landed on the clipboard.
        """
        i18n = self.main_window.i18n
        try:
            pyperclip.copy(url)
        except Exception:
            self.main_window.output(i18n.t("msg_copy_error"))
            return
        self.main_window.output(i18n.t("link_copied").format(url=url))

    def _on_link_context_menu(self, event):
        """The context menu of a focused link: open it, or copy it.

        Bound on the link controls themselves so it answers before the event
        reaches anything else — without it the menu that opened belonged to
        the focused *message*, offering forward/reply/delete for a row the
        user was not on any more.
        """
        url = self._link_url_for(event.GetEventObject())
        if not url:
            # Not one of ours after all — let it go wherever it would have.
            event.Skip()
            return
        i18n = self.main_window.i18n
        menu = wx.Menu()
        open_item = menu.Append(wx.ID_ANY, i18n.t("open_link"))
        self.Bind(wx.EVT_MENU, lambda e, u=url: self._open_link(u), open_item)
        copy_item = menu.Append(wx.ID_ANY, f"{i18n.t('copy_link')}\tCtrl+C")
        self.Bind(wx.EVT_MENU, lambda e, u=url: self._copy_focused_link(u),
                  copy_item)
        self.PopupMenu(menu)
        menu.Destroy()

    @staticmethod
    def _open_link(url: str):
        """Open a link URL in the system's default application."""
        try:
            os.startfile(url)
        except Exception:
            wx.LaunchDefaultBrowser(url)

    def _on_hyperlink_open(self, event):
        self._open_link(event.GetURL())

    def _on_link_key_down(self, event):
        """Ensure Space and Enter activate a focused HyperlinkCtrl, and that
        Ctrl+C copies the link rather than the message.

        The Ctrl+C branch is a fallback, not the mechanism: the accelerator
        normally consumes the key before this handler runs (see
        _focused_link_url()). It is here so the behaviour does not depend on
        that ordering, and so removing the accelerator would not silently take
        the feature with it."""
        event = command_key_event(self, 'links', event)
        kc = event.GetKeyCode()
        if kc in (wx.WXK_RETURN, wx.WXK_SPACE, wx.WXK_NUMPAD_ENTER):
            self._open_link(event.GetEventObject().GetURL())
            return
        if event.ControlDown() and kc == ord("C"):
            url = self._link_url_for(event.GetEventObject())
            if url:
                self._copy_focused_link(url)
                return
        event.Skip()

    def _on_links_list_activated(self, event):
        """Enter (or a double-click) on a link row opens it."""
        idx = event.GetIndex()
        if 0 <= idx < len(self._current_links):
            self._open_link(self._current_links[idx])

    def _on_links_list_key_down(self, event):
        """Space also opens the focused link; Ctrl+C copies just its URL."""
        event = command_key_event(self, 'links', event)
        kc = event.GetKeyCode()
        if kc in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER, wx.WXK_SPACE):
            idx = self._links_list.GetFirstSelected()
            if 0 <= idx < len(self._current_links):
                self._open_link(self._current_links[idx])
            return
        if event.ControlDown() and kc == ord("C"):
            # Same fallback status as _on_link_key_down()'s: the accelerator
            # gets the key first, so this rarely runs.
            url = self._link_url_for(self._links_list)
            if url:
                self._copy_focused_link(url)
            return
        event.Skip()
