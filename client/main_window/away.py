"""AwayMixin — part of MainWindow (see main_window/__init__.py).

Away mode (core/away_mode.py): after a configurable time without a keystroke or
a click in WinZapp, the person is away — shown offline, and what lands in the
open conversation is left unread. This mixin feeds the tracker from every
interaction with the program, runs the countdown and reacts to its changes.
"""

import logging

import wx

from core.away_mode import AwayTracker, away_enabled, away_minutes
from core.conversation_view import conversation_in_view

# Seconds between two looks at the countdown; the minute setting is far coarser.
AWAY_CHECK_MS = 5_000


def _interaction_types():
    """Event types that mean a person is using WinZapp. Mouse movement is left
    out on purpose: a pointer resting on the window is not someone present."""
    names = (
        "wxEVT_KEY_DOWN", "wxEVT_CHAR_HOOK", "wxEVT_LEFT_DOWN", "wxEVT_RIGHT_DOWN",
        "wxEVT_MIDDLE_DOWN", "wxEVT_MOUSEWHEEL", "wxEVT_MENU", "wxEVT_BUTTON",
        "wxEVT_HOTKEY",
    )
    return frozenset(getattr(wx, name) for name in names if hasattr(wx, name))


class _InteractionFilter(wx.EventFilter):
    """Sees every event of the program, in every window, before it is handled."""

    def __init__(self, types, on_interaction):
        super().__init__()
        self._types = types
        self._on_interaction = on_interaction

    def FilterEvent(self, event):
        try:
            if event.GetEventType() in self._types:
                self._on_interaction()
        except Exception:  # never let the filter break event delivery
            logging.exception("[away] interaction filter failed")
        return wx.EventFilter.Event_Skip


class AwayMixin:
    """Away mode: the countdown, the interaction filter and what changes."""

    def _init_away(self):
        """Start the countdown; MainWindow.__init__ calls it once the window exists."""
        self._away_tracker = AwayTracker()
        self._away_filter = None
        try:
            self._away_filter = _InteractionFilter(
                _interaction_types(), self._away_interaction)
            wx.EvtHandler.AddFilter(self._away_filter)
        except Exception:
            # Without the filter only the main window's own keys would count.
            logging.exception("[away] could not install the interaction filter")
            self._away_filter = None
            self.Bind(wx.EVT_CHAR_HOOK, self._on_away_char_hook)
        self._away_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_away_timer, self._away_timer)
        self._away_timer.Start(AWAY_CHECK_MS)

    def _stop_away(self):
        """Shutdown: no more events, no more ticks."""
        timer = getattr(self, "_away_timer", None)
        if timer is not None and timer.IsRunning():
            timer.Stop()
        away_filter = getattr(self, "_away_filter", None)
        if away_filter is not None:
            try:
                wx.EvtHandler.RemoveFilter(away_filter)
            except Exception:
                logging.exception("[away] could not remove the interaction filter")
            self._away_filter = None

    def _away_settings(self) -> dict:
        return (getattr(self, "settings", None) or {}).get("user_interface", {})

    def is_user_away(self) -> bool:
        """True while the person counts as away (and the option is still on)."""
        tracker = getattr(self, "_away_tracker", None)
        return bool(tracker is not None and tracker.away
                    and away_enabled(self._away_settings()))

    def _away_interaction(self):
        """One interaction: the person is here. Cheap — runs for every click."""
        tracker = getattr(self, "_away_tracker", None)
        if tracker is not None and tracker.touch():
            wx.CallAfter(self._on_away_ended)

    def _on_away_char_hook(self, event):
        self._away_interaction()
        event.Skip()

    def _on_away_timer(self, event):
        self._refresh_away_state()

    def _away_busy(self) -> bool:
        """A call, a recording or a playing audio: present without touching a key."""
        if getattr(self, "_active_voice_call", None):
            return True
        for attr in ("conversations_panel", "archived_conversations_panel",
                     "locked_conversations_panel"):
            if getattr(getattr(self, attr, None), "_is_recording", False):
                return True
        playback = getattr(self, "_voice_playback_active", None)
        return bool(playback is not None and playback())

    def _refresh_away_state(self):
        """Look at the countdown now; also called when the setting changes."""
        tracker = getattr(self, "_away_tracker", None)
        if tracker is None:
            return
        settings = self._away_settings()
        change = tracker.update(
            enabled=away_enabled(settings), minutes=away_minutes(settings),
            busy=self._away_busy(),
        )
        if change == "away":
            logging.info("[away] no interaction for %d min: away", away_minutes(settings))
            self._sync_online_presence()
        elif change == "back":
            self._on_away_ended()

    def _on_away_ended(self):
        """The person is back: online again, and the open conversation is read."""
        logging.info("[away] back")
        self._sync_online_presence()
        self._read_open_conversation_after_away()

    def _read_open_conversation_after_away(self):
        """What landed in the open conversation while away is read now."""
        if (getattr(self, "_window_hidden", False) or self.IsIconized()
                or not self.IsActive()):
            return
        panel = getattr(self, "conversations_panel", None)
        if not conversation_in_view(panel):
            return
        jid = panel.conversation.get("remoteJid", "")
        chat = self.chats.get(jid) if jid else None
        if chat and int(chat.get("unreadCount") or 0) > 0:
            self.mark_conversation_as_read(jid)
