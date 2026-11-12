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
from ui.conversation_panel.channel_conversation import (fetch_channel_updates,
                                                        remember_channel_roles)
from ui.dialogs.background_work import BackgroundWork
from ui.dialogs.channels_return import restore, snapshot
from ui.shortcut_bindings import make_shortcut_table
from ui.dialogs.group_join_dialog import open_group_chat
from ui.dialogs.labelled_control import add_labelled

MINE, FIND = 0, 1


class CreatedChannel(str):
    """A new channel's id. *settings_saved* is False when a setting chosen
    with it (who may react, admin profiles) was refused after the channel was
    made: the channel exists, and saying "could not create" made the user
    create it again."""
    settings_saved = True


def create_with_reactions(connection, name, description, reactions, admin_profiles=None):
    """Create the channel, then set who may react and the admin profiles when
    they are not WhatsApp's defaults (any emoji, profiles off). Runs off the
    main thread. "" when the channel was not created."""
    jid = ch.create_channel(connection, name, description)
    if not jid:
        return jid
    created = CreatedChannel(jid)
    settings = []
    if reactions and reactions != "ALL":
        settings.append(lambda: ch.set_channel_reactions(connection, jid, reactions))
    if admin_profiles:
        settings.append(lambda: ch.set_channel_admin_profiles(connection, jid, True))
    for setting in settings:
        try:
            setting()
        except Exception as error:      # refused, or no answer: the channel stays
            logging.warning("[channels] setting after create failed: %s", type(error).__name__)
            created.settings_saved = False
    return created


def edit_with_reactions(connection, channel, name, description, reactions, admin_profiles=None):
    """Edit the channel; its reactions and admin profiles only when they were
    chosen and differ."""
    ch.edit_channel(connection, channel.jid, name, description)
    if reactions and reactions != channel.reactions:
        ch.set_channel_reactions(connection, channel.jid, reactions)
    if admin_profiles is not None and admin_profiles != channel.admin_profiles:
        ch.set_channel_admin_profiles(connection, channel.jid, admin_profiles)


def show_channels(mw, parent, keep="", state=None):
    """Show the window and open the channel the user chose, if any. *keep* is
    the channel selected once the list arrives (coming back from it); *state*,
    the window as it was left to open it (channels_return.py)."""
    dialog = ChannelsDialog(parent, mw)
    dialog._keep_first = keep
    if state is not None:
        restore(dialog, state)
    # where a channels-changed event finds it (WebSocketClient.on_channels_changed)
    mw._channels_dialog = dialog
    try:
        if dialog.ShowModal() != wx.ID_OK or dialog.result is None:
            return False
        channel = dialog.result
        mw._channels_return_state = snapshot(dialog)
    finally:
        mw._channels_dialog = None
        dialog.Destroy()
    # Esc in that channel comes back here (channel_closed()), not to the
    # chat list: it was opened from this window, not from the list.
    mw._channel_opened_from_window = channel.jid
    open_group_chat(mw, channel.jid, channel.name or mw.i18n.t("channels_unnamed"))
    fetch_channel_updates(mw, channel.jid)
    return True


def channel_closed(mw, closed_jid) -> bool:
    """The open conversation *closed_jid* was just closed with Esc: when it is
    the channel the Channels window opened, show the window again, on that
    channel, and say so to the caller (True). The mark is used once."""
    opened = getattr(mw, "_channel_opened_from_window", "")
    state = getattr(mw, "_channels_return_state", None)
    mw._channel_opened_from_window, mw._channels_return_state = "", None
    if not opened or opened != closed_jid or getattr(mw, "_shutting_down", False):
        return False
    wx.CallAfter(show_channels, mw, mw, opened, state)
    return True


def forget_channel_return(mw):
    """A conversation was closed by WinZapp, not by the user (a full resync,
    an account switch, the channel archived or deleted): nothing goes back to
    the Channels window, and the mark is dropped so no later close does."""
    mw._channel_opened_from_window, mw._channels_return_state = "", None


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


def empty_list_text(mode, loaded, searched, i18n):
    """The one row an empty channel list shows, so reaching it says why it is
    empty instead of nothing (the message list does the same for an empty
    conversation). It is not a channel: `_shown` stays empty under it."""
    if mode == MINE:
        return i18n.t("channels_none") if loaded else i18n.t("loading")
    return i18n.t("channels_nothing_found") if searched else i18n.t("loading")


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
    if channel.reactions in ch.REACTION_SETTINGS:
        setting = ChannelEditDialog.REACTION_KEYS[ch.REACTION_SETTINGS.index(channel.reactions)]
        lines.append(i18n.t("channels_reactions_line").format(setting=i18n.t(setting)))
    return "\n\n".join(line for line in lines if line)


class ChannelEditDialog(wx.Dialog):
    """Name, description and who may react, of a channel being created or
    edited. *reactions* is the channel's setting (core.channels
    REACTION_SETTINGS), "" when unknown: then nothing is chosen, and nothing
    is sent unless the admin chooses."""

    REACTION_KEYS = ("channels_reactions_all", "channels_reactions_basic", "channels_reactions_none")

    def __init__(self, parent, i18n, title, name="", description="", reactions="ALL",
                 admin_profiles=None):
        super().__init__(parent, title=title, style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        sizer = wx.BoxSizer(wx.VERTICAL)
        self._name = add_labelled(self, sizer, i18n.t("channels_name"),
                                  lambda: wx.TextCtrl(self, value=name))
        self._name.SetMaxLength(ch.NAME_LIMIT)
        self._description = add_labelled(self, sizer, i18n.t("channels_description"), lambda: wx.TextCtrl(
            self, value=description, style=wx.TE_MULTILINE), 1)
        # never cut without saying so: core/channels.py sends at most this
        self._description.SetMaxLength(ch.DESCRIPTION_LIMIT)
        self._reactions = add_labelled(self, sizer, i18n.t("channels_reactions"), lambda: wx.Choice(
            self, choices=[i18n.t(key) for key in self.REACTION_KEYS]))
        if reactions in ch.REACTION_SETTINGS:
            self._reactions.SetSelection(ch.REACTION_SETTINGS.index(reactions))
        # WhatsApp Web's "Show admin profile", shown as it does only when
        # WhatsApp offers it to this account: the engine then says its state
        # (WPPConnect; None on Neonize and when not offered)
        self._admin_profiles = wx.CheckBox(self, label=i18n.t("channels_admin_profiles"))
        self._admin_profiles.SetValue(bool(admin_profiles))
        self._admin_profiles.Show(admin_profiles is not None)
        sizer.Add(self._admin_profiles, 0, wx.ALL, 8)
        buttons = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)
        sizer.Add(buttons, 0, wx.EXPAND | wx.ALL, 8)
        self.SetSizer(sizer)
        self.SetMinSize((420, 320))
        self.Fit()
        self.CentreOnParent()
        self._name.SetFocus()

    def values(self):
        """(name, description, reactions, admin_profiles): reactions "" when
        none was chosen, admin_profiles None when the setting is not offered."""
        index = self._reactions.GetSelection()
        reactions = ch.REACTION_SETTINGS[index] if 0 <= index < len(ch.REACTION_SETTINGS) else ""
        admin_profiles = self._admin_profiles.GetValue() if self._admin_profiles.IsShown() else None
        return (self._name.GetValue().strip(), self._description.GetValue().strip(), reactions,
                admin_profiles)


class ChannelsDialog(BackgroundWork, wx.Dialog):
    def __init__(self, parent, main_window):
        self._init_background(main_window)
        i18n = self._i18n
        self._mode = MINE
        self._mine, self._found, self._shown = [], [], []
        self._loaded = self._searched = False
        self._filter_timer = None
        self._search_again = self._reload_again = False
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
        # The directory's category and list, as WhatsApp Web's pills: picking
        # one reads the directory again, a moment after the last change, so
        # arrowing through the choice does not send one read per item passed.
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
        self._admins = button("channels_people", self._on_admins)
        self._insights = button("channels_insights", self._on_insights)
        button("close", self._on_close, wx.ID_CANCEL)
        sizer.Add(buttons, 0, wx.EXPAND | wx.ALL, 4)
        self.SetSizer(sizer)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)
        self.ID_F5 = wx.NewIdRef()
        self.SetAcceleratorTable(make_shortcut_table(self, "channels", [
            (wx.ACCEL_NORMAL, wx.WXK_F5, self.ID_F5)]))
        self.Bind(wx.EVT_MENU, self._on_read_again, id=self.ID_F5)
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)
        self._sync_mode()
        self._show(self._mine)          # "loading" until the first answer
        self._list.SetFocus()
        self.CentreOnParent()
        # coming back from a channel (channels_return.restore): the list is
        # already there, so it is read again without announcing its count
        wx.CallAfter(lambda: self._reload(keep=getattr(self, "_keep_first", ""),
                                          quiet=getattr(self, "_restored", False)))

    #: how long after the last change of a category or list the directory is read
    FILTER_DELAY_MS = 700

    def _on_filter_changed(self, event=None):
        """A category or list was picked: read the directory with it, once the
        choice has settled (core/channels.py names what each one asks)."""
        if self._mode != FIND:
            return
        if self._filter_timer is not None and self._filter_timer.IsRunning():
            self._filter_timer.Stop()
        self._filter_timer = wx.CallLater(self.FILTER_DELAY_MS, self._filter_settled)

    def _filter_settled(self):
        self._filter_timer = None
        if not self._current():
            return
        if self._busy:
            self._search_again = True   # read again when the one in flight answers
            return
        self._on_search()

    def _labelled_choice(self, row, label, items):
        row.Add(wx.StaticText(self, label=label), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        choice = wx.Choice(self, choices=items)
        choice.SetName(label.replace("&", ""))
        choice.SetSelection(0)
        choice.Bind(wx.EVT_CHOICE, self._on_filter_changed)
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

    def _refused_while_busy(self) -> bool:
        """True (and says so) while an answer is awaited: a window that asks
        something first must refuse before asking, not after the Yes."""
        if not self._busy:
            return False
        self._announce(self._i18n.t("loading"))
        return True

    def _set_busy(self, busy):
        self._busy = busy
        self._update_buttons()
        if not busy and (self._reload_again or self._search_again):
            # asked for while a read or an action was in flight, whichever it
            # was: a category picked during a quiet reload used to be lost
            wx.CallAfter(self._run_pending)

    def _run_pending(self):
        """Runs what was asked for while the window was busy: the followed
        list first (a change WhatsApp announced), then the directory (a
        category or list picked, or Find chosen). One at a time: the read it
        starts makes the window busy, and its end comes back here."""
        if self._busy or not self._current():
            return
        if self._reload_again:
            self._reload_again = False
            self._reload(quiet=True)
        elif self._search_again:
            self._search_again = False
            if self._mode == FIND:
                self._on_search()

    def _update_buttons(self):
        # Not disabled while busy: the handlers refuse a second press, and a
        # disabled button that has the focus hands it to Close, where the
        # next Space would close the window (call_participants.py).
        free = self._current()
        channel = self._mine_version(self._selected()) if free else None
        following = self._is_following(channel)
        i18n = self._i18n
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
        self._admins.Enable(channel is not None and channel.admin)
        self._insights.Enable(channel is not None and channel.admin)

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

    def on_channels_changed(self):
        """WhatsApp said a followed channel changed (followed, left, muted,
        role, name): read the list again, quietly, as F5 would. F5 stays for
        a change no event announces (a channel's picture)."""
        if not self._current():
            return
        if self._busy:
            self._reload_again = True
            return
        self._reload(quiet=True)

    def _finish_reload(self, channels, keep="", quiet=False):
        self._set_busy(False)
        if not self._current():
            return
        if self._reload_again:
            # a change was announced while this read was in flight
            self._reload_again = False
            self._reload(keep=keep, quiet=True)
            return
        if channels is None:
            logging.warning("[channels] read_failed")
            self._show_failure(self._i18n.t("channels_unavailable"), MINE)
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
            # the recommended channels, as WhatsApp opens on; after the read
            # in flight when there is one (it used to be refused silently,
            # the list saying "Loading" with nothing loading)
            if self._busy:
                self._search_again = True
            else:
                self._on_search()

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
        if self._search_again:
            # the category or list changed while this read was in flight:
            # its answer is for the old one
            self._search_again = False
            self._on_search()
            return
        if found is None:
            logging.warning("[channels] directory_failed")
            self._show_failure(self._i18n.t("channels_directory_unavailable"), FIND)
            self._announce(self._i18n.t("channels_directory_unavailable"))
            return
        self._found = found
        self._searched = True
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
                            for c in self._shown]
                           or [empty_list_text(self._mode, self._loaded, self._searched, self._i18n)])
            if self._list.GetCount():
                self._list.SetSelection(next(
                    (i for i, c in enumerate(self._shown) if c.jid == kept), 0))
        finally:
            self._list.Thaw()
        self._show_selected()

    def _show_failure(self, text, mode):
        """An empty list of *mode* says the read failed, not "loading"."""
        if self._mode == mode and not self._shown:
            self._list.Set([text])
            self._list.SetSelection(0)

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

    def _on_read_again(self, event=None):
        """F5 (Settings > Shortcuts, "channels.ID_F5"). No Refresh button: the
        list follows WhatsApp's own events (on_channels_changed); this reads
        again what is shown, for what no event announces and after a read
        that failed."""
        if self._mode == FIND:
            self._on_search()
        else:
            self._reload()

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
            self._announce(self._i18n.t(done_key(outcome) if callable(done_key) else done_key))
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
        if self._refused_while_busy() or not self._current():
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
        if self._refused_while_busy() or not self._current():
            return
        i18n = self._i18n
        with ChannelEditDialog(self, i18n, i18n.t("channels_create_title")) as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            name, description, reactions, admin_profiles = dialog.values()
        if not name:
            self._announce(i18n.t("channels_name_required"))
            return
        connection = self._connection
        self._act(lambda: create_with_reactions(connection, name, description, reactions,
                                                admin_profiles),
                  lambda created: ("channels_created"
                                   if getattr(created, "settings_saved", True)
                                   else "channels_created_settings_not_saved"),
                  "channel_create_failed")

    def _on_edit(self, event=None):
        channel = self._mine_version(self._selected())
        if channel is None or not channel.admin or self._refused_while_busy():
            return
        i18n = self._i18n
        with ChannelEditDialog(self, i18n, i18n.t("channels_edit_title"), channel.name,
                               channel.description, channel.reactions,
                               channel.admin_profiles) as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            name, description, reactions, admin_profiles = dialog.values()
        if not name:
            self._announce(i18n.t("channels_name_required"))
            return
        connection, jid = self._connection, channel.jid
        self._act(lambda: edit_with_reactions(connection, channel, name, description, reactions,
                                              admin_profiles),
                  "channels_edited", "channel_edit_failed", jid)

    def _on_insights(self, event=None):
        """The channel's statistics (channel_insights_dialog.py), for its admins."""
        channel = self._mine_version(self._selected())
        if channel is None or not channel.admin or self._refused_while_busy():
            return
        from ui.dialogs.channel_insights_dialog import show_channel_insights
        show_channel_insights(self._mw, self, channel)

    def _on_admins(self, event=None):
        channel = self._mine_version(self._selected())
        if channel is None or not channel.admin or self._refused_while_busy() or not self._current():
            return
        from ui.dialogs.channel_admins import ChannelAdminsDialog
        dialog = ChannelAdminsDialog(self, self._mw, channel)
        self._mw._channel_people_dialog = dialog    # for channels-changed
        try:
            dialog.ShowModal()
            changed = dialog.changed
        finally:
            self._mw._channel_people_dialog = None
            dialog.Destroy()
        if changed:     # handed over or left: the list's role is stale
            self._reload(keep=channel.jid, quiet=True)

    def _on_delete(self, event=None):
        channel = self._mine_version(self._selected())
        if channel is None or not channel.owner or self._refused_while_busy():
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
