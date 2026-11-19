"""The content panels that are not chat lists: Status, Calls, Communities and
Channels. One content panel is on screen at a time.

Every place that puts a chat list (or a conversation) on screen hides these
first; it used to name Status and Calls one by one in eight places, and a
panel missing from one of them stayed on screen under the conversation.
`hide_side_panels()` is that list, kept here once. `show_side_panel()` is
the switch Alt+8, Alt+9 and the navigation list make to Communities and
Channels: the same as Alt+6 makes to Calls.
"""

#: attribute names on the main window, in the content sizer's order
SIDE_PANELS = ("status_panel", "calls_panel", "communities_panel", "channels_panel")
#: the chat lists, which show_chat_panel() switches between
CHAT_LIST_PANELS = ("archived_conversations_panel", "locked_conversations_panel")


def side_panels(mw):
    """The side panels this window has, in order."""
    return [panel for panel in (getattr(mw, name, None) for name in SIDE_PANELS)
            if panel is not None]


def hide_side_panels(mw, keep=None):
    """Hide every side panel but *keep*."""
    for panel in side_panels(mw):
        if panel is not keep:
            panel.Hide()


def show_side_panel(mw, name):
    """Make the side panel *name* the one on screen and return it (None when
    the window has none): the locked chats lock again, an open conversation
    closes as a panel switch closes it (its typing and presence stop), and
    every other panel hides. The caller calls its on_show()."""
    panel = getattr(mw, name, None)
    if panel is None:
        return None
    lock = getattr(mw, "lock_chat_vault", None)
    if lock is not None:
        lock(silent=True, show_conversations=False)
    conversations = mw.conversations_panel
    if conversations.conversation is not None:
        conversations.close_conversation_for_panel_switch()
        # left for a panel, not with Esc: nothing comes back to Channels later
        mw._channel_opened_from_window = ""
    conversations.Hide()
    for other in CHAT_LIST_PANELS:
        chat_list = getattr(mw, other, None)
        if chat_list is not None:
            chat_list.Hide()
    hide_side_panels(mw, keep=panel)
    panel.Show()
    mw.content_panel.Layout()
    return panel


def show_communities(mw):
    """Alt+8 and the navigation list's Communities."""
    panel = show_side_panel(mw, "communities_panel")
    if panel is not None:
        panel.on_show()
    return panel


def show_channels(mw, keep=""):
    """Alt+9, the navigation list's Channels, and Esc in a channel opened
    from there (*keep*: that channel, selected again)."""
    panel = show_side_panel(mw, "channels_panel")
    if panel is not None:
        panel.on_show(keep=keep)
    return panel
