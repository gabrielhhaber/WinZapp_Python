"""WindowLifecycleMixin — part of MainWindow (see main_window/__init__.py).

Moved verbatim out of main.py. Methods run with ``self`` bound to the
MainWindow instance, so every attribute set in MainWindow.__init__ is
available here.
"""

import ctypes
import logging
import os
import sys
import threading
import time
import wx
from core.api_client import api_post
if sys.platform == "win32":
    from core.tray_manager import TrayIcon


class WindowLifecycleMixin:
    """Window activation, presence heartbeat, tray, close/hide/restore, focus and
    the shutdown path.
    """

    # ── Tray / window lifecycle ───────────────────────────────────────────────

    # ── Online presence ───────────────────────────────────────────────────────

    def _on_window_activate(self, event):
        """
        Fired by wxPython when the main window gains or loses OS focus.
        - Gained focus  → send "available" immediately, then every 20 s
        - Lost focus    → stop the timer, send "unavailable" once

        Debounced: internal modal dialogs (pairing, settings, etc.) can toggle
        the main frame's activation state several times within milliseconds
        as focus moves to/from them, and each toggle used to fire its own
        presence POST. Only the state that's still current after a short
        quiet window gets sent, collapsing that flicker into a single request.
        """
        # Not debounced and deliberately ahead of every early return below:
        # holding a system-global hotkey is only acceptable while this window
        # really is the active one (see _set_bookmark_zero_hotkey).
        active = bool(event.GetActive())
        # Read by _window_can_ask() from the poll thread, which cannot ask wx.
        self._main_window_active = active
        self._set_bookmark_zero_hotkey(active)
        if active:
            # Disabling the popup means "do not interrupt what I am doing",
            # not "hide the call controls". Once the user deliberately comes
            # back with Alt+Tab, put keyboard and screen-reader focus directly
            # on the in-window Desligar button — but only for a call that is
            # still RINGING (the bar lives inside this window, so this only
            # ever moves focus within the window already being activated).
            #
            # Deliberately NOT done for a call already answered: that call
            # lives in its own top-level window (voice_call_window)
            # precisely so the user can move freely between it and the
            # conversation list. An earlier version of this branch treated
            # the two cases the same and stole focus into voice_call_window
            # every time MainWindow itself became active — so switching to
            # WinZapp to read a conversation while on a call immediately
            # bounced focus (and, since SetFocus on another top-level window
            # also raises it on Windows, the window itself) back onto the
            # call window. Reported live: "ao mover para a janela do
            # WinZapp, sempre cai na janela ligação de voz". See
            # voice_call_window's own construction comment for the other
            # half of that fix (it is no longer owned by this window either).
            answer_button = getattr(self, "incoming_call_answer_button", None)
            call_bar = getattr(self, "incoming_call_bar", None)
            if answer_button is not None and call_bar is not None and call_bar.IsShown():
                wx.CallAfter(answer_button.SetFocus)
        if self.background_mode:
            event.Skip()
            return
        token = getattr(self, "token", None)
        if not token:
            event.Skip()
            return
        if self._presence_debounce_timer is not None and self._presence_debounce_timer.IsRunning():
            self._presence_debounce_timer.Stop()
        self._presence_debounce_timer = wx.CallLater(300, self._apply_window_activate, active)
        event.Skip()

    def _apply_window_activate(self, active: bool):
        """Actually send the presence update after `_on_window_activate`'s debounce settles."""
        if active:
            self._last_activation_time = time.time()
            # The Accounts menu is built once at startup from a registry
            # snapshot and shows only 'paired' accounts. If another account's
            # process paired / changed state after this menu was built (or its
            # process was still coming up), this window's menu goes stale and an
            # account "disappears" from it until 'Switch account' is opened.
            # Rebuilding on focus-gain keeps the menu (and the Ctrl+Alt+1..9
            # hotkey slot map, rebuilt inside _build_menubar) in sync with the
            # live registry — this is the moment the user returns to the window.
            self._refresh_accounts_menu_if_stale()
            threading.Thread(
                target=self._send_presence, args=("available",), daemon=True
            ).start()
            if not self._presence_timer.IsRunning():
                self._presence_timer.Start(20_000)   # refresh every 20 s
        else:
            self._presence_timer.Stop()
            threading.Thread(
                target=self._send_presence, args=("unavailable",), daemon=True
            ).start()

    def _on_presence_timer(self, event):
        """Periodic keep-alive: resend 'available' while window is focused."""
        token = getattr(self, "token", None)
        if token:
            threading.Thread(
                target=self._send_presence, args=("available",), daemon=True
            ).start()

    def _send_presence(self, presence: str):
        """
        POST /api/{session}/set-online-presence
        Body: {"isOnline": true | false}

        Always runs on a background thread — never blocks the UI.
        """
        token = getattr(self, "token", None)
        if not token:
            return
        if not getattr(self, "_wa_connected", False):
            logging.debug(
                "[presence] skipping '%s' — no live WhatsApp connection", presence
            )
            return
        url = f"{self.wpp_server}:{self.wpp_port}/api/{token}/set-online-presence"
        is_online = presence == "available"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        try:
            resp = api_post(
                url, json={"isOnline": is_online}, headers=headers, timeout=5
            )
            status = getattr(resp, "status_code", None)
            if status is not None and status >= 400:
                logging.warning(
                    "[presence] set-online-presence('%s') returned HTTP %s",
                    presence, status,
                )
        except Exception:
            logging.debug(
                "[presence] set-online-presence('%s') failed", presence, exc_info=True
            )

    def _init_tray(self):
        """Create the system-tray icon if the setting is enabled."""
        show = self.settings.get("general", {}).get("show_tray_icon", True)
        if show:
            from core.tray_manager import TrayIcon
            self.tray_icon = TrayIcon(self)

    def _on_close(self, event):
        """
        Intercept the window-close button.
        If the tray icon is active, hide the window instead of exiting.

        Uses Win32 ShowWindow(SW_HIDE) directly so that the window is
        physically hidden even when wx's internal IsShown() state has drifted
        out of sync (e.g. after another process showed the window via Win32
        without going through wx's Show() path).
        """
        self.lock_chat_vault(silent=True, show_conversations=False)
        if self.tray_icon is not None:
            try:
                import ctypes
                ctypes.windll.user32.ShowWindow(self.GetHandle(), 0)  # SW_HIDE = 0
            except Exception:
                self.Hide()
            self._window_hidden = True
            # One authoritative tray update now that the window is hidden.
            self.tray_icon.update_tooltip()
            event.Veto()
        else:
            self.real_exit()

    def _on_iconize(self, event):
        if event.IsIconized():
            self.lock_chat_vault(silent=True, show_conversations=False)
        event.Skip()

    def hide_to_tray(self):
        """Hide this window to the tray WITHOUT quitting (the process keeps
        running so it still receives this account's messages/notifications).

        Used by the account switch (Option 2 UX): switching to another account
        brings that account's window forward and hides this one, so the user
        sees a single active window while every account stays live in the
        background. Mirrors _on_close's SW_HIDE path (bypasses wx state-drift).
        """
        if getattr(self, "tray_icon", None) is None:
            return
        self.lock_chat_vault(silent=True, show_conversations=False)
        try:
            import ctypes
            ctypes.windll.user32.ShowWindow(self.GetHandle(), 0)  # SW_HIDE
        except Exception:
            self.Hide()
        self._window_hidden = True
        try:
            self.tray_icon.update_tooltip()
        except Exception:
            pass

    @staticmethod
    def _hotkey_hides_window(has_tray: bool, window_hwnd, foreground_hwnd) -> bool:
        """Whether the global hotkey should send the window to the tray.

        Only when this very window is the one in front (issue #258): a window
        that is hidden, minimised or merely behind another program is brought
        forward, as the hotkey always did. A WinZapp dialog in front does not
        count -- hiding the frame under it would strand the dialog. Without a
        tray icon nothing could bring the window back but the hotkey itself,
        so it is never hidden then (hide_to_tray() refuses for that reason).
        """
        return bool(has_tray and window_hwnd and foreground_hwnd == window_hwnd)

    def toggle_window_from_hotkey(self):
        """Global hotkey: open WinZapp, or hide it to the tray when it is in front."""
        try:
            foreground = ctypes.windll.user32.GetForegroundWindow()
        except Exception:
            foreground = None
        if WindowLifecycleMixin._hotkey_hides_window(
                getattr(self, "tray_icon", None) is not None, self.GetHandle(), foreground):
            logging.info("[hotkey] window in front — hiding to the tray")
            self.hide_to_tray()
            return
        self.restore_window()

    def restore_window(self):
        """Bring the WinZapp window to the foreground.

        Uses Win32 ShowWindow + SetForegroundWindow directly to avoid wx
        state-drift: _on_close hides the window via SW_HIDE which bypasses
        wx's internal visibility tracking, so wx-level Show()/Raise() calls
        may silently no-op. SW_SHOWMAXIMIZED both un-hides/un-minimizes the
        window and (unlike SW_RESTORE, which reopens at whatever size it had
        before being hidden) always brings it back maximized — the size the
        app is meant to run at, and the one restore-from-tray is expected to
        return to.
        Also refreshes the chat list in case sync updates happened while the
        window was hidden.
        """
        import ctypes
        hwnd = self.GetHandle()
        SW_SHOWMAXIMIZED = 3
        user32 = ctypes.windll.user32
        user32.ShowWindow(hwnd, SW_SHOWMAXIMIZED)
        # SetForegroundWindow() alone silently fails when another application
        # holds the foreground lock (Win32 foreground-stealing prevention). When
        # that happened the window stayed hidden/behind and the global hotkey
        # appeared "dead" until the app was restarted. Briefly attaching our
        # input queue to the current foreground thread lifts the lock so the
        # restore is reliable.
        try:
            kernel32 = ctypes.windll.kernel32
            fg_hwnd = user32.GetForegroundWindow()
            fg_thread = user32.GetWindowThreadProcessId(fg_hwnd, None) if fg_hwnd else 0
            cur_thread = kernel32.GetCurrentThreadId()
            attached = False
            if fg_thread and fg_thread != cur_thread:
                attached = bool(user32.AttachThreadInput(fg_thread, cur_thread, True))
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            user32.SetActiveWindow(hwnd)
            if attached:
                user32.AttachThreadInput(fg_thread, cur_thread, False)
        except Exception:
            user32.SetForegroundWindow(hwnd)
        self._window_hidden = False
        # When started via --background the window was never shown; clear the
        # flag so _allow_ui_focus_changes(), _on_window_activate() and the
        # notification window_active check all work correctly from now on.
        self.background_mode = False
        # ShowWindow via Win32 does NOT update wx's internal m_isShown flag, so
        # IsShown() returns False even though the window is physically visible.
        # Calling Show(True) syncs the flag without causing flicker (the window
        # is already visible to Win32 so SW_SHOW is a no-op at the OS level).
        if not self.IsShown():
            self.Show(True)
        if hasattr(self, "conversations_panel"):
            wx.CallAfter(self.add_chats_to_ui)
        # Put keyboard focus on a real navigable control. After a
        # hide/restore (and especially repeated account switches) the frame can
        # come forward with focus sitting on the bare window, so arrow-key
        # navigation has nothing to act on until the user Tabs into a control.
        # Land focus on the conversation list so the keyboard works immediately.
        wx.CallAfter(self._focus_primary_control)

    def _focus_primary_control(self):
        """Move keyboard focus to the primary navigable list of whichever
        panel is actually on screen, so arrow keys work right after a window
        restore / account switch.

        `IsShown()` alone is not enough: switching tabs (Alt+1/2/4) hides the
        PANEL container (`conversations_panel.Hide()`, `status_panel.Hide()`,
        ...) but never explicitly hides the list widgets inside it, so a list
        keeps reporting its own `IsShown()` as True forever after the last
        time it was shown — even while a sibling panel is the one actually
        visible. That made the global hotkey always land focus on the
        conversations list, even when the Status (or Archived) tab was the
        one on screen when the window was hidden. `IsShownOnScreen()` walks
        the whole ancestor chain instead, so it reflects the panel's real
        Hide()/Show() state too.
        """
        try:
            panel = getattr(self, "conversations_panel", None)
            lst = getattr(panel, "conversations_list", None) if panel else None
            if lst is not None and lst.IsShownOnScreen():
                lst.SetFocus()
                return
            archived = getattr(self, "archived_conversations_panel", None)
            archived_lst = (
                getattr(archived, "conversations_list", None) if archived else None
            )
            if archived_lst is not None and archived_lst.IsShownOnScreen():
                archived_lst.SetFocus()
                return
            status = getattr(self, "status_panel", None)
            status_lst = getattr(status, "_status_list", None) if status else None
            if status_lst is not None and status_lst.IsShownOnScreen():
                status_lst.SetFocus()
                return
            calls = getattr(self, "calls_panel", None)
            calls_lst = calls.current_list() if calls else None
            if calls_lst is not None and calls_lst.IsShownOnScreen():
                calls_lst.SetFocus()
                return
            # Last resort, and only when none of the lists above is on screen:
            # an ARCHIVED conversation open. ArchivedConversationsPanel shows
            # conversations_panel but hides its conversations_list, and hides
            # itself -- so every check above was False and focus landed
            # nowhere, the exact "arrows do nothing after restoring the window"
            # symptom this method exists to prevent. The navigable control in
            # that state is the open conversation's message list.
            messages = getattr(panel, "messages_list", None) if panel else None
            if messages is not None and messages.IsShownOnScreen():
                messages.SetFocus()
                return
        except Exception:
            logging.exception("[focus] restoring primary control focus failed")

    # Bound for a caller that lost the _teardown_started_lock race to wait
    # for the winning path's _teardown_complete_event before
    # self-terminating. Sized above ipc.py's _QUIT_RELEASE_POLL_SECONDS
    # (75s — the winning path's own teardown is bounded by that budget).
    # Bounded rather than indefinite: if the event is somehow never set,
    # this caller must still terminate on the user's original quit request.
    _TEARDOWN_OWNED_ELSEWHERE_WAIT_SECONDS = 80.0

    def real_exit(self):
        """Completely close WinZapp: graceful teardown, then terminate.

        Split into _perform_shutdown() (reversible teardown — close session,
        flush, stop Node, close DB) and _terminate_process() (the terminal
        os._exit). The IPC quit handler needs the teardown WITHOUT the immediate
        os._exit so it can flag `released` and reply to the initiating account
        before the process vanishes (see _ipc_quit).

        The teardown runs on a background thread, and that is not incidental:
        _perform_shutdown() calls _stop_wpp_server(), which waits out the
        graceful-stop budget (an HTTP request to close WhatsApp Web cleanly
        rather than taskkill'ing Chrome and risking its LevelDB credentials).
        Running that on the wx main thread stops the message loop from pumping,
        and Windows tags any window whose loop goes quiet for a few seconds as
        "Not Responding" — reported live as "Sair" leaving a frozen window on
        screen for tens of seconds. The window is hidden first so quitting
        still *looks* instant while the teardown finishes behind it.

        _ipc_quit() keeps calling _perform_shutdown() directly: it is already
        off the UI thread and needs the teardown to complete before it replies.
        See tests/test_shutdown_wait.py.
        """
        try:
            self.Hide()
        except Exception:
            pass

        def _teardown():
            did_work = False
            try:
                did_work = self._perform_shutdown()
            finally:
                if not did_work:
                    # Another path (a concurrent _on_end_session(), or a
                    # second overlapping quit) already owns teardown —
                    # terminating immediately could os._exit() the process
                    # while it is still mid _stop_wpp_server().
                    self._teardown_complete_event.wait(
                        timeout=self._TEARDOWN_OWNED_ELSEWHERE_WAIT_SECONDS
                    )
                self._terminate_process()

        threading.Thread(target=_teardown, daemon=True, name="winzapp-shutdown").start()

    def _perform_shutdown(self) -> bool:
        """Reversible shutdown: stop timers/threads, gracefully close the WPP
        session (waiting for its flush), stop the Node, close the DB. Does NOT
        exit the process, so it is safe to call from the IPC quit handler.

        Returns True if THIS call actually performed the teardown, False if
        another path already owns it. Callers must not treat False the same
        as True and immediately self-terminate — see
        _teardown_complete_event's own comment for why."""
        # Set FIRST, before anything else: _stop_wpp_server() below closes
        # the WPPConnect session itself, which — while our WebSocket is
        # still connected — arrives as an ordinary "connection closed" event
        # indistinguishable from a real disconnect. _set_wa_connected()
        # checks this flag and skips entirely, so quitting never announces
        # "modo offline ativado" in the moment before the process exits.
        #
        # Locked (not a plain getattr-then-set) so this can never race
        # _on_end_session() or a second concurrent call into this method.
        with self._teardown_started_lock:
            if getattr(self, "_shutting_down", False):
                return False  # another path already owns teardown
            self._shutting_down = True
        try:
            # Stop the presence keep-alive timer before tearing down
            if hasattr(self, "_presence_timer") and self._presence_timer.IsRunning():
                self._presence_timer.Stop()
            # Also cancel a pending debounced activate/deactivate — otherwise it
            # can fire mid-shutdown and spawn a presence POST + restart the timer
            # just stopped above.
            if getattr(self, "_presence_debounce_timer", None) is not None and self._presence_debounce_timer.IsRunning():
                self._presence_debounce_timer.Stop()
            for identity in list(getattr(self, "_incoming_call_watchdogs", {})):
                self._cancel_incoming_call_watchdog(identity)
            for identity in list(getattr(self, "_incoming_call_dialogs", {})):
                self._close_incoming_call_dialog(identity)
            if hasattr(self, "call_incoming_sound"):
                self.call_incoming_sound.stop()
            self._stop_voice_call_audio()
            if getattr(self, "tray_icon", None) is not None:
                try:
                    self.tray_icon.RemoveIcon()
                    self.tray_icon.Destroy()
                except Exception:
                    pass
                self.tray_icon = None
            if hasattr(self, "message_queue"):
                self.message_queue.stop()
            if getattr(self, "_update_checker", None) is not None:
                self._update_checker.stop()
            if getattr(self, "_wpp_update_checker", None) is not None:
                self._wpp_update_checker.stop()
            self._stop_wpp_server()
            self._flush_pending_debounced_saves()
            if hasattr(self, "db") and self.db is not None:
                try:
                    self.db.close()
                except Exception:
                    pass
            return True
        finally:
            # Always, even on an exception above: a caller that lost the
            # lock race is blocked on this event before self-terminating —
            # leaving it unset would strand that caller for its full timeout.
            self._teardown_complete_event.set()

    def _flush_pending_debounced_saves(self):
        """Synchronously run any pending debounced save BEFORE it can be lost.

        _schedule_save() (0.15s debounce, chats/contacts -> self.db) and
        _schedule_save_settings() (2s debounce, settings.json) each leave a
        write sitting on its own daemon threading.Timer. Left alone, either
        DatabaseBridge.close() rejects it once ``_closing`` flips, or
        os._exit() kills the timer thread before it fires — os._exit()
        never waits for other threads. Cancelling and running the callback
        here, synchronously, beats both: the save runs on THIS thread before
        either death trap exists.

        Called from both _perform_shutdown() and _on_end_session()
        (WM_ENDSESSION) — the latter never calls _perform_shutdown(), so
        without this call here too a Windows shutdown/logoff would lose the
        same pending writes with no protection at all.

        Not airtight: the WebSocket stays connected throughout
        _stop_wpp_server(), so a live event landing between this call
        returning and db.close() running could still schedule a fresh save
        that loses the same race. Narrows the window, does not close it.
        """
        with self._save_timer_lock:
            pending_chat_save = self._save_timer is not None
            if pending_chat_save:
                self._save_timer.cancel()
                self._save_timer = None
            pending_settings_save = getattr(self, "_settings_save_timer", None) is not None
            if pending_settings_save:
                self._settings_save_timer.cancel()
                self._settings_save_timer = None
        if pending_chat_save:
            try:
                self._do_save()
            except Exception:
                logging.exception("[_flush_pending_debounced_saves] _do_save() failed")
        if pending_settings_save:
            try:
                self.save_settings()
            except Exception:
                logging.exception("[_flush_pending_debounced_saves] save_settings() failed")

    def _terminate_process(self):
        """Terminal exit — never returns.

        Hide immediately so quitting still feels instant to the user, then do
        the final wx ExitMainLoop + os._exit off the main thread so a slow
        teardown can't leave a window Windows would mark "Not Responding".
        """
        try:
            self.Hide()
        except Exception:
            pass

        def _finish_exit():
            try:
                wx.GetApp().ExitMainLoop()
            except Exception:
                pass
            import os
            os._exit(0)

        threading.Thread(target=_finish_exit, daemon=True).start()
