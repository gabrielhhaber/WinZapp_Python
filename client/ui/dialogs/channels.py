"""The channels window (Alt+9): the channels the account follows, the channel
directory, and every action WhatsApp Web's Channels tab offers on a channel.

The reading and the requests live in core/channels.py; this module lists,
describes and acts. Every request runs off the wx thread (BackgroundWork) and
a late answer never touches a closed window. Showing the channels in the
conversation list (Settings) is unrelated and keeps working as it did.
"""

import datetime
import logging

import wx

from core import channels as ch
from ui.conversation_panel.channel_conversation import remember_channel_roles
from ui.dialogs.background_work import BackgroundWork
from ui.dialogs.group_join_dialog import open_group_chat
from ui.dialogs.labelled_control import add_labelled

MINE, FIND = 0, 1


def show_channels(mw, parent):
    """Show the window and open the channel the user chose, if any."""
    dialog = ChannelsDialog(parent, mw)
    try:
        if dialog.ShowModal() != wx.ID_OK or dialog.result is None:
            return False
        channel = dialog.result
    finally:
        dialog.Destroy()
    open_group_chat(mw, channel.jid, channel.name or mw.i18n.t("channels_unnamed"))
    return True


def channel_label(channel, i18n, followed=()):
    """The list row: the name and what sets the channel apart, never its id."""
    parts = [channel.name or i18n.t("channels_unnamed")]
    if channel.verified:
        parts.append(i18n.t("channels_verified"))
    if channel.owner:
        parts.append(i18n.t("channels_role_owner"))
    elif channel.admin:
        parts.append(i18n.t("channels_role_admin"))
    elif channel.jid in followed:
        # only where it is news: the directory (followed is empty elsewhere)
        parts.append(i18n.t("channels_following"))
    if channel.muted:
        parts.append(i18n.t("channels_muted"))
    return ", ".join(parts)


def channel_details(channel, i18n, following):
    lines = [channel.description]
    if channel.followers is not None:
        lines.append(i18n.t("channels_followers").format(count=channel.followers))
    role = {"owner": "channels_you_are_owner", "admin": "channels_you_are_admin"}.get(channel.role)
    if role:
        lines.append(i18n.t(role))
    elif following:
        lines.append(i18n.t("channels_you_follow"))
    else:
        lines.append(i18n.t("channels_you_do_not_follow"))
    if channel.created:
        day = datetime.date.fromtimestamp(channel.created).strftime("%x")
        lines.append(i18n.t("channels_created_on").format(date=day))
    if channel.link:
        lines.append(i18n.t("channels_link_line").format(link=channel.link))
    return "\n\n".join(line for line in lines if line)


class ChannelEditDialog(wx.Dialog):
    """Name and description of a channel being created or edited."""

    def __init__(self, parent, i18n, title, name="", description=""):
        super().__init__(parent, title=title, style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        sizer = wx.BoxSizer(wx.VERTICAL)
        self._name = add_labelled(self, sizer, i18n.t("channels_name"),
                                  lambda: wx.TextCtrl(self, value=name))
        self._name.SetMaxLength(ch.NAME_LIMIT)
        self._description = add_labelled(self, sizer, i18n.t("channels_description"), lambda: wx.TextCtrl(
            self, value=description, style=wx.TE_MULTILINE), 1)
        # never cut without saying so: core/channels.py sends at most this
        self._description.SetMaxLength(ch.DESCRIPTION_LIMIT)
        buttons = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)
        sizer.Add(buttons, 0, wx.EXPAND | wx.ALL, 8)
        self.SetSizer(sizer)
        self.SetMinSize((420, 320))
        self.Fit()
        self.CentreOnParent()
        self._name.SetFocus()

    def values(self):
        return self._name.GetValue().strip(), self._description.GetValue().strip()


class ChannelsDialog(BackgroundWork, wx.Dialog):
    def __init__(self, parent, main_window):
        self._init_background(main_window)
        i18n = self._i18n
        self._mode = MINE
        self._mine, self._found, self._shown = [], [], []
        self._loaded = False
        self.result = None
        super().__init__(parent, title=i18n.t("channels_title"), size=(620, 640),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        sizer = wx.BoxSizer(wx.VERTICAL)
        self._mode_radio = wx.RadioBox(
            self, label=i18n.t("channels_show"),
            choices=[i18n.t("channels_mine"), i18n.t("channels_find")],
            majorDimension=1, style=wx.RA_SPECIFY_ROWS)
        self._mode_radio.Bind(wx.EVT_RADIOBOX, self._on_mode)
        sizer.Add(self._mode_radio, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(wx.StaticText(self, label=i18n.t("channels_search")), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self._search = wx.TextCtrl(self, style=wx.TE_PROCESS_ENTER)
        self._search.SetName(i18n.t("channels_search").replace("&", ""))
        self._search.Bind(wx.EVT_TEXT_ENTER, self._on_search)
        row.Add(self._search, 1, wx.EXPAND)
        self._search_btn = wx.Button(self, label=i18n.t("channels_search_button"))
        self._search_btn.Bind(wx.EVT_BUTTON, self._on_search)
        row.Add(self._search_btn, 0, wx.LEFT, 8)
        sizer.Add(row, 0, wx.EXPAND | wx.ALL, 8)
        # The directory's category and list, as WhatsApp Web's pills: read by
        # Search (or Enter), never on each arrow press inside the choice.
        row = wx.BoxSizer(wx.HORIZONTAL)
        # literal keys, in the order of ch.CATEGORIES and ch.VIEWS
        self._category = self._labelled_choice(row, i18n.t("channels_category"), [
            i18n.t("channels_category_all"), i18n.t("channels_category_business"),
            i18n.t("channels_category_entertainment"), i18n.t("channels_category_lifestyle"),
            i18n.t("channels_category_news"), i18n.t("channels_category_organizations"),
            i18n.t("channels_category_people"), i18n.t("channels_category_sports")])
        self._view = self._labelled_choice(row, i18n.t("channels_view"), [
            i18n.t("channels_view_recommended"), i18n.t("channels_view_trending"),
            i18n.t("channels_view_popular"), i18n.t("channels_view_new")])
        sizer.Add(row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        self._list = add_labelled(self, sizer, i18n.t("channels_list"),
                                  lambda: wx.ListBox(self, style=wx.LB_SINGLE), 2)
        self._list.Bind(wx.EVT_LISTBOX, lambda event: self._show_selected())
        self._list.Bind(wx.EVT_LISTBOX_DCLICK, self._on_open)
        self._details = add_labelled(self, sizer, i18n.t("channels_details"), lambda: wx.TextCtrl(
            self, style=wx.TE_READONLY | wx.TE_MULTILINE), 1)
        self._status = wx.StaticText(self, label="")
        sizer.Add(self._status, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        buttons = wx.WrapSizer(wx.HORIZONTAL)

        def button(key, handler, button_id=wx.ID_ANY):
            control = wx.Button(self, button_id, label=i18n.t(key))
            control.Bind(wx.EVT_BUTTON, handler)
            buttons.Add(control, 0, wx.ALL, 4)
            return control

        self._open = button("channels_open", self._on_open)
        self._follow = button("channels_follow", self._on_follow)
        self._mute = button("channels_mute", self._on_mute)
        self._copy = button("channels_copy_link", self._on_copy)
        self._by_link = button("channels_follow_by_link", self._on_by_link)
        self._create = button("channels_create", self._on_create)
        self._edit = button("channels_edit", self._on_edit)
        self._delete = button("channels_delete", self._on_delete)
        self._refresh = button("channels_refresh", self._reload)
        button("close", self._on_close, wx.ID_CANCEL)
        sizer.Add(buttons, 0, wx.EXPAND | wx.ALL, 4)
        self.SetSizer(sizer)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)
        self._sync_mode()
        self._update_buttons()
        self._list.SetFocus()
        self.CentreOnParent()
        wx.CallAfter(self._reload)

    def _labelled_choice(self, row, label, items):
        row.Add(wx.StaticText(self, label=label), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        choice = wx.Choice(self, choices=items)
        choice.SetName(label.replace("&", ""))
        choice.SetSelection(0)
        row.Add(choice, 1, wx.RIGHT, 8)
        return choice

    def _directory_filters(self):
        """(category, view) picked in the two choices: "" for all categories."""
        category = self._category.GetSelection()
        view = self._view.GetSelection()
        return (ch.CATEGORIES[category - 1] if 1 <= category <= len(ch.CATEGORIES) else "",
                ch.VIEWS[view] if 0 <= view < len(ch.VIEWS) else "")

    # ── state ─────────────────────────────────────────────────────────────

    def _followed_ids(self):
        return {c.jid for c in self._mine}

    def _selected(self):
        index = self._list.GetSelection()
        return self._shown[index] if 0 <= index < len(self._shown) else None

    def _is_following(self, channel):
        return channel is not None and (channel.following or channel.jid in self._followed_ids())

    def _mine_version(self, channel):
        """The followed list's copy of *channel* (it knows the role and mute)."""
        if channel is None:
            return None
        return next((c for c in self._mine if c.jid == channel.jid), channel)

    def _set_busy(self, busy):
        self._busy = busy
        self._update_buttons()

    def _update_buttons(self):
        # Not disabled while busy: the handlers refuse a second press, and a
        # disabled button that has the focus hands it to Close, where the
        # next Space would close the window (call_participants.py).
        free = self._current()
        channel = self._mine_version(self._selected()) if free else None
        following = self._is_following(channel)
        i18n = self._i18n
        self._refresh.Enable(free)
        self._search_btn.Enable(free and self._mode == FIND)
        self._by_link.Enable(free)
        self._create.Enable(free and self._loaded)
        self._open.Enable(channel is not None)
        self._follow.Enable(channel is not None and not channel.owner)
        self._follow.SetLabel(i18n.t("channels_unfollow" if following else "channels_follow"))
        self._mute.Enable(channel is not None and following)
        self._mute.SetLabel(i18n.t("channels_unmute" if channel and channel.muted else "channels_mute"))
        self._copy.Enable(channel is not None and bool(channel.link))
        self._edit.Enable(channel is not None and channel.admin)
        self._delete.Enable(channel is not None and channel.owner)

    def _sync_mode(self):
        self._mode_radio.SetSelection(self._mode)
        self._search.Enable(self._mode == FIND)
        self._category.Enable(self._mode == FIND)
        self._view.Enable(self._mode == FIND)

    # ── reading ───────────────────────────────────────────────────────────

    def _reload(self, event=None, keep="", quiet=False):
        if self._busy or not self._current():
            return
        self._set_busy(True)
        self._status.SetLabel(self._i18n.t("loading"))
        connection = self._connection
        self._run_async(lambda: ch.list_channels(connection),
                        lambda channels: self._finish_reload(channels, keep, quiet))

    def _finish_reload(self, channels, keep="", quiet=False):
        self._set_busy(False)
        if not self._current():
            return
        if channels is None:
            logging.warning("[channels] read_failed")
            self._announce(self._i18n.t("channels_unavailable"))
            return
        self._loaded = True
        self._mine = ch.sorted_channels(channels)
        remember_channel_roles(self._mw, self._mine)
        if self._mode == MINE:
            self._show(self._mine, keep)
            if not quiet:       # after an action, its own result was just said
                self._announce(self._i18n.t("channels_count").format(count=len(channels))
                               if channels else self._i18n.t("channels_none"))
        else:
            self._show(self._found, keep)

    def _on_mode(self, event=None):
        self._mode = FIND if self._mode_radio.GetSelection() == FIND else MINE
        self._sync_mode()
        if self._mode == MINE:
            self._show(self._mine)
            return
        self._show(self._found)
        if not self._found:
            self._on_search()           # the recommended channels, as WhatsApp opens on

    def _on_search(self, event=None):
        if self._busy or not self._current() or self._mode != FIND:
            return
        query = self._search.GetValue().strip()
        category, view = self._directory_filters()
        self._set_busy(True)
        self._status.SetLabel(self._i18n.t("loading"))
        connection = self._connection
        key = ch.directory_announcement(query, category, view)
        self._run_async(lambda: ch.directory(connection, query, category=category, view=view),
                        lambda found: self._finish_search(found, key))

    def _finish_search(self, found, key):
        self._set_busy(False)
        if not self._current():
            return
        if found is None:
            logging.warning("[channels] directory_failed")
            self._announce(self._i18n.t("channels_directory_unavailable"))
            return
        self._found = found
        if self._mode == FIND:
            self._show(found)
        self._announce(self._i18n.t(key).format(count=len(found)) if found
                       else self._i18n.t("channels_nothing_found"))

    def _show(self, channels, keep=""):
        """Replace the list, keeping the selected channel."""
        kept = keep or (self._selected().jid if self._selected() else "")
        self._shown = list(channels)
        # "following" only in the directory: in My channels every row is one
        followed = self._followed_ids() if self._mode == FIND else set()
        self._list.Freeze()
        try:
            self._list.Set([channel_label(self._mine_version(c), self._i18n, followed)
                            for c in self._shown])
            if self._shown:
                self._list.SetSelection(next(
                    (i for i, c in enumerate(self._shown) if c.jid == kept), 0))
        finally:
            self._list.Thaw()
        self._show_selected()

    def _show_selected(self):
        channel = self._mine_version(self._selected())
        self._details.ChangeValue(
            channel_details(channel, self._i18n, self._is_following(channel)) if channel else "")
        self._update_buttons()

    # ── keyboard ──────────────────────────────────────────────────────────

    def _on_char_hook(self, event):
        enter = event.GetKeyCode() in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER) and not event.HasAnyModifiers()
        focus = wx.Window.FindFocus()
        if enter and focus is self._list:
            self._on_open()
            return
        if enter and focus in (self._category, self._view):
            self._on_search()
            return
        event.Skip()

    # ── actions ───────────────────────────────────────────────────────────

    def _on_open(self, event=None):
        channel = self._selected()
        if self._busy or channel is None:
            return
        self.result = self._mine_version(channel)
        self._closed.set()
        self.EndModal(wx.ID_OK)

    def _act(self, work, done_key, failed_key, keep=""):
        """Run one action, then read the followed list again."""
        if self._busy or not self._current():
            return
        self._set_busy(True)
        self._status.SetLabel(self._i18n.t("loading"))

        def run():
            try:
                result = work()
                return result if isinstance(result, str) and result else True
            except ValueError:
                return False

        def finish(outcome):
            self._set_busy(False)
            if not self._current():
                return
            if not outcome:
                logging.warning("[channels] action_failed key=%s", failed_key)
                self._announce(self._i18n.t(failed_key))
                return
            self._announce(self._i18n.t(done_key))
            # a channel just created comes back selected (its id is the result)
            self._reload(keep=outcome if isinstance(outcome, str) else keep, quiet=True)
            self._list.SetFocus()

        self._run_async(run, finish)

    def _on_follow(self, event=None):
        channel = self._mine_version(self._selected())
        if channel is None or channel.owner:
            return
        connection, jid = self._connection, channel.jid
        if self._is_following(channel):
            self._act(lambda: ch.unfollow(connection, jid), "channels_unfollowed",
                      "channel_unfollow_failed", jid)
        else:
            self._act(lambda: ch.follow(connection, jid), "channels_followed",
                      "channel_follow_failed", jid)

    def _on_mute(self, event=None):
        channel = self._mine_version(self._selected())
        if channel is None or not self._is_following(channel):
            return
        connection, jid, mute = self._connection, channel.jid, not channel.muted
        self._act(lambda: ch.set_muted(connection, jid, mute),
                  "channels_muted_done" if mute else "channels_unmuted_done", "channel_mute_failed", jid)

    def _on_copy(self, event=None):
        channel = self._selected()
        if channel is None or not channel.link:
            return
        if wx.TheClipboard.Open():
            try:
                wx.TheClipboard.SetData(wx.TextDataObject(channel.link))
                wx.TheClipboard.Flush()
            finally:
                wx.TheClipboard.Close()
            self._announce(self._i18n.t("channels_link_copied"))

    def _on_by_link(self, event=None):
        if self._busy or not self._current():
            return
        i18n = self._i18n
        with wx.TextEntryDialog(self, i18n.t("channels_link_prompt"), i18n.t("channels_title")) as ask:
            if ask.ShowModal() != wx.ID_OK:
                return
            code = ch.invite_code(ask.GetValue())
        if not code:
            self._announce(i18n.t("channels_link_invalid"))
            return
        self._set_busy(True)
        self._status.SetLabel(i18n.t("loading"))
        connection = self._connection
        self._run_async(lambda: ch.channel_info(connection, invite=code), self._finish_by_link)

    def _finish_by_link(self, channel):
        self._set_busy(False)
        if not self._current():
            return
        if channel is None:
            self._announce(self._i18n.t("channel_not_found"))
            return
        self._mode = FIND
        self._sync_mode()
        self._found = [channel]
        self._show(self._found, channel.jid)
        self._list.SetFocus()
        self._announce(self._i18n.t("channels_link_found").format(
            name=channel.name or self._i18n.t("channels_unnamed")))

    def _on_create(self, event=None):
        if self._busy or not self._current():
            return
        i18n = self._i18n
        with ChannelEditDialog(self, i18n, i18n.t("channels_create_title")) as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            name, description = dialog.values()
        if not name:
            self._announce(i18n.t("channels_name_required"))
            return
        connection = self._connection
        self._act(lambda: ch.create_channel(connection, name, description), "channels_created",
                  "channel_create_failed")

    def _on_edit(self, event=None):
        channel = self._mine_version(self._selected())
        if channel is None or not channel.admin:
            return
        i18n = self._i18n
        with ChannelEditDialog(self, i18n, i18n.t("channels_edit_title"), channel.name,
                               channel.description) as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            name, description = dialog.values()
        if not name:
            self._announce(i18n.t("channels_name_required"))
            return
        connection, jid = self._connection, channel.jid
        self._act(lambda: ch.edit_channel(connection, jid, name, description), "channels_edited",
                  "channel_edit_failed", jid)

    def _on_delete(self, event=None):
        channel = self._mine_version(self._selected())
        if channel is None or not channel.owner:
            return
        i18n = self._i18n
        text = i18n.t("channels_delete_confirm").format(
            name=channel.name or i18n.t("channels_unnamed"))
        with wx.MessageDialog(self, text, i18n.t("channels_title"),
                              wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING) as ask:
            if ask.ShowModal() != wx.ID_YES:
                return
        connection, jid = self._connection, channel.jid
        self._act(lambda: ch.delete_channel(connection, jid), "channels_deleted", "channel_delete_failed")
