"""The Channels panel comes back as it was left to open a channel.

Esc in a channel opened from the Channels panel shows the panel again
(channels.channel_closed). It was a modal window until 2026-11-12, destroyed
when the channel opened, so what the person had (the mode, the search, the
category and list, the selection) had to be saved and put back. The panel
is the same one when it comes back: nothing is put back, and its list of
followed channels is only read again, quietly.
"""
import types

import ui.dialogs.channels as dialogs
from main_window import content_panels


def _channel(jid):
    return types.SimpleNamespace(jid=jid, name=jid)


def test_opening_a_channel_marks_it_and_reads_its_updates(monkeypatch):
    opened, fetched = [], []
    monkeypatch.setattr(dialogs, "open_group_chat", lambda mw, jid, name: opened.append(jid))
    monkeypatch.setattr(dialogs, "fetch_channel_updates", lambda mw, jid: fetched.append(jid))
    mw = types.SimpleNamespace(i18n=types.SimpleNamespace(t=lambda key: key))
    dialogs.open_channel(mw, _channel("2@newsletter"))
    assert opened == ["2@newsletter"] and mw._channel_opened_from_window == "2@newsletter"
    # its updates are read at once, not by the next sync round
    assert fetched == ["2@newsletter"]


class _Panel:
    """ChannelsPanel.on_show on a stub: what it shows and reads."""
    on_show = dialogs.ChannelsPanel.on_show

    def __init__(self, renewed=False):
        self._mode, self._loaded, self._searched = dialogs.FIND, True, True
        self._mine, self._found = [_channel("1@newsletter")], [_channel("2@newsletter")]
        self._search_again = self._reload_again = False
        self.renewed, self.shown, self.reloads, self.synced = renewed, [], [], 0
        self._list = types.SimpleNamespace(SetFocus=lambda: None)

    def _renew_connection(self):
        return self.renewed

    def _sync_mode(self):
        self.synced += 1

    def _show(self, channels, keep=""):
        self.shown.append(([c.jid for c in channels], keep))

    def _reload(self, keep="", quiet=False):
        self.reloads.append((keep, quiet))


def test_coming_back_keeps_the_directory_and_selects_the_channel():
    panel = _Panel()
    panel.on_show(keep="2@newsletter")
    assert panel._mode == dialogs.FIND and panel._found[0].jid == "2@newsletter"
    assert panel.shown == [(["2@newsletter"], "2@newsletter")]
    # read again without saying the count: it was said when first shown
    assert panel.reloads == [("2@newsletter", True)]


def test_another_connection_starts_the_panel_over():
    """A new pairing or engine: the old lists are another account's view."""
    panel = _Panel(renewed=True)
    panel.on_show()
    assert panel._mode == dialogs.MINE and panel._mine == [] and panel._found == []
    assert panel.synced == 1 and panel.reloads == [("", False)]


# ------------------------------------- only the user's close goes back
def _closing_panel(monkeypatch, shown):
    import ui.conversation_panel.conversation_navigation as nav
    from ui.conversations import ConversationsPanel
    # one wx module for both: what was scheduled tells them apart
    monkeypatch.setattr(nav.wx, "CallAfter", lambda fn, *a: shown.append(
        ("channels" if fn is content_panels.show_channels else "list", a)))
    mw = types.SimpleNamespace(_channel_opened_from_window="1@newsletter")
    panel = types.SimpleNamespace(main_window=mw, _conversation_origin=None,
                                  _close_conversation_core=lambda: (True, "1@newsletter"),
                                  _restore_conversation_selection=lambda: None)
    panel.close_conversation = types.MethodType(ConversationsPanel.close_conversation, panel)
    return panel, mw


def test_esc_or_ctrl_w_on_the_channel_goes_back_to_the_channels_panel(monkeypatch):
    shown = []
    panel, _mw = _closing_panel(monkeypatch, shown)
    panel.close_conversation(None, from_user=True)
    assert [kind for kind, _a in shown] == ["channels"]


def test_winzapp_closing_the_channel_never_brings_the_panel_back(monkeypatch):
    """A full resync (F5), an account switch, or the channel archived or
    deleted close the conversation too: the Channels window popped up,
    modal, while the database was being wiped."""
    shown = []
    panel, mw = _closing_panel(monkeypatch, shown)
    panel.close_conversation()
    assert [kind for kind, _a in shown] == ["list"]
    assert mw._channel_opened_from_window == ""


def test_ctrl_w_in_the_conversation_is_the_users_close(monkeypatch):
    """The conversation's own Ctrl+W called close_conversation(event) with
    no from_user, and landed on the chat list."""
    from ui.conversations import ConversationsPanel
    shown = []
    panel, _mw = _closing_panel(monkeypatch, shown)
    ConversationsPanel._on_ctrl_w_conversation(panel, None)
    assert [kind for kind, _a in shown] == ["channels"]
