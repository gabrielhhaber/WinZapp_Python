"""SessionLifecycleMixin — part of MainWindow (see main_window/__init__.py).

Moved verbatim out of main.py. Methods run with ``self`` bound to the
MainWindow instance, so every attribute set in MainWindow.__init__ is
available here.
"""

import ctypes
import logging
import os
import threading
import time
import wx
from core.profile_backup import (
    close_snapshot_max_age as _close_snapshot_max_age,
    live_snapshot_due as _live_snapshot_due,
    live_snapshot_policy as _live_snapshot_policy,
)
from core.api_client import (
    api_get,
    api_post,
    redact_credentials,
)
from ui.dialogs.checkbox_confirm import confirm_with_checkbox
from main_window.runtime_setup import pick_restore_generation


class SessionLifecycleMixin:
    """Session teardown and profile health: shutdown audit, Windows end-session
    handling, unattended QR guard, profile recovery and live snapshots (see
    docs/traps/profile-recovery.md).
    """

    # How long to wait for a close-session to actually flush WhatsApp Web's auth
    # state to disk before we hard-kill the Node. Generous because a large
    # profile's leveldb flush can take well over the old fixed 2s — and cutting
    # it short is exactly what corrupted the profile into a re-pair.
    _SHUTDOWN_FLUSH_TIMEOUT = 15.0
    _SHUTDOWN_FLUSH_POLL = 0.3

    # Total budget for the teardown when WINDOWS is shutting us down, as opposed
    # to the user quitting. The two are not the same deadline at all.
    #
    # On a normal quit nothing is racing us, so the generous per-phase timeouts
    # above are free: they only ever elapse when something is genuinely wrong.
    # On WM_ENDSESSION we are on a clock we do not control. _on_query_end_session
    # registers a ShutdownBlockReason but still lets the shutdown proceed
    # (handling the event without skipping answers TRUE to WM_QUERYENDSESSION —
    # registering a reason without ALSO vetoing buys no extra time; see that
    # method), so what we really have is Windows' hung-app timeout, ~5s by
    # default.
    #
    # Left unbounded, the phases below sum to ~40s (10 POST + 15 flush + 15
    # profile release). Windows would cut that off partway — which is the very
    # mid-write kill the whole routine exists to prevent, now with a frozen UI
    # on the way out. Every shutdown_audit.log from the field completes the
    # clean path inside one second, so 4s is the real case with room to spare;
    # the cases that would need longer are a suspended chrome.exe that is not
    # writing anyway, and that the OS is about to kill regardless.
    _WINDOWS_SHUTDOWN_BUDGET = 4.0

    def _shutdown_audit(self, msg: str):
        """Append one line to a PERSISTENT shutdown-audit log that (unlike
        log.log, opened mode='w') survives across launches, so the teardown of
        one run can still be read after the next launch has started. This is the
        only way to prove what actually happened during a close that leads to a
        'Session Unpaired' on the following start."""
        try:
            from app_paths import log_path
            line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [pid={os.getpid()}] {msg}\n"
            with open(log_path("shutdown_audit.log"), "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass

    def _self_inflicted_teardown_expected(self) -> bool:
        """True while WE are deliberately closing this account's own
        WPPConnect session ourselves — a full app shutdown (_shutting_down),
        a WPPConnect Server auto-update (_wpp_updating), a power-resume
        zombie-session recovery restart (_recovery_restart_active), or an
        in-place session restart after a detached Puppeteer page
        (_restarting_wpp_session).

        All four call POST /close-session on this account's own session
        while the WebSocket stays deliberately connected throughout, so a
        WebSocket "close" carrying loggedOut/401, an unlinked status-find
        reading, or a status-session CLOSED reading during any of these four
        windows is the direct, expected result of that call — never WhatsApp
        actually unlinking the device. The two wake-from-sleep triggers
        (_recovery_restart_active, _restarting_wpp_session) matter most in
        practice: laptops sleep far more often than WinZapp is quit or
        WPPConnect updated. Shared by on_connection_update()'s close branch,
        on_wpp_status_find(), and check_wa_connection_http()'s CLOSED
        auto-start guard so all three treat every self-inflicted path the
        same way."""
        return (
            bool(getattr(self, "_shutting_down", False))
            or bool(getattr(self, "_wpp_updating", False))
            or bool(getattr(self, "_recovery_restart_active", False))
            or bool(getattr(self, "_restarting_wpp_session", False))
            # A profile restore closes this session itself, and normally sets
            # _recovery_restart_active too — but that flag has a second owner
            # (_force_whatsapp_session_restart), whose finally can clear it
            # mid-restore. This one only the restore clears (issue #203 review).
            or bool(getattr(self, "_profile_restore_in_flight", False))
        )

    def _session_restart_owned(self) -> bool:
        """Whether a close/start cycle of ours owns this session right now.

        Not just _recovery_restart_active: that flag has two owners — the
        profile restore and the power-resume restart — and the latter's
        finally clears it while a restore it overlapped is still copying the
        profile back (found reviewing issue #203). _profile_restore_in_flight
        is cleared by the restore alone, so reading both is what keeps the
        health loop's CLOSED auto-start from opening Chrome over that copy.
        """
        return bool(getattr(self, "_recovery_restart_active", False)
                    or getattr(self, "_profile_restore_in_flight", False))

    def _reset_unattended_qr_guards(self) -> None:
        """Drop both unattended-QR guards: a human is looking at the pairing
        UI, so the codes WPPConnect produces are wanted again.

        Called by Connect.show_connection_dial() immediately before its modal
        loop opens. The counter goes back to 0 so this attempt's own rotations
        are never counted as a flood, and the latch clears so
        check_wa_connection_http() stops blocking the session this attempt is
        about to start — without that the user sits in the dialog they just
        opened with the halt still in force. A method of its own only so a
        test can call it: show_connection_dial() builds real wx dialogs and
        ends in ShowModal().
        """
        self._unattended_qr_events = 0
        self._qr_flood_halted = False

    def _halt_unattended_qr_session(self) -> None:
        """Close a WPPConnect session that is minting QR/pairing codes with
        nobody watching, and keep the health loop from restarting it.

        Triggered by WebSocketClient._handle_unattended_qr() — see there for
        why an unattended code stream is a real hazard rather than a cosmetic
        one, and for the ban that motivated this. `autoClose`/
        `deviceSyncTimeout` are pinned to 0 in client/api_patches/src/config.ts
        precisely so WPPConnect never closes a code-producing session by
        itself, so nothing below the Python layer will ever stop this.

        The latch is what makes it stick. check_wa_connection_http()'s CLOSED
        branch fires /start-session on the very next poll otherwise, which
        revives the browser and restarts the same code stream about 30s later
        — the close alone would have bought one poll cycle, not a fix.
        Deliberately NOT folded into _self_inflicted_teardown_expected(): that
        makes _set_wa_connected() show "connecting" and swallow the offline
        announcement, and here the session is genuinely gone and the user
        needs to hear so. The token and the `paired` flag are untouched (this
        is not a logout and wipes nothing); the latch clears on a real
        reconnect and when the pairing dialog opens, both of which imply a
        session we did not halt.

        One knock-on effect, deliberate but worth writing down since the rest
        of this machinery is: after the halt, status-session answers CLOSED,
        which is NOT in cs.UNLINKED_STATES, so the confirmed-logout path
        (_act_on_unlink_decision) stops being fed strikes and never fires
        again this run. On a genuine WhatsApp-side unlink that means the
        dialog + wipe it would eventually have reached is replaced by the
        announcement below plus a re-pairing the user drives themselves —
        which loses nothing (a re-pair rebuilds the session either way) and
        is the price of not asking WhatsApp for codes for hours.

        Second knock-on, relying on an invariant documented elsewhere (see
        _restart_session_once): close-session force-kills, with no auth flush,
        any session whose status is not CONNECTED/open, and a session that got
        here is never CONNECTED. Harmless here specifically — it only reached
        this path because WhatsApp had already dropped its auth, which is what
        makes it mint codes in the first place, so there is nothing left to
        flush — but worth writing down, since a caller that ever reached this
        with live auth WOULD lose it.

        _qr_within_startup_grace() (websocket_client.py) looks like it
        contradicts that: it withholds judgment on a code arriving seconds
        into a boot, on the grounds that the session may still be coming up.
        It does not, and the distinction is worth keeping straight. That
        grace protects the USER from acting on one reading — the re-pairing
        dialog that wipes history — and says nothing about whether the code
        is real. By the time any code exists, it has come through one of two
        routes, and each proves the same thing by its own means. A QR event
        reaches catchQR only once getQrCode() returned a urlCode, and that
        urlCode *is* the code. A pairing code — on a session started with a
        phone number, host.layer.js never registers checkQrCode at all, so
        catchQR is never reached — is minted by
        WPP.conn.startLinkDeviceCodeForPhoneNumber() behind loginByCode's
        own gate, which is a wait for WhatsApp Web's auth state (probed
        through getQrCode(), but as a readiness check; the urlCode is thrown
        away and is not the code) and which returns without minting anything
        the moment needsToScan() says the session is registered. wa-js
        produces neither while paired and authenticated, so on either route
        the stored auth has already failed to restore.
        Reaching _UNATTENDED_QR_LIMIT codes inside that window is stronger
        evidence than the two readings the dialog itself asks for, so the
        halt stays deliberately outside the grace.
        """
        if getattr(self, "_qr_flood_halted", False):
            return
        self._qr_flood_halted = True
        token = getattr(self, "token", "")
        # Say it out loud, because nothing else will. By construction this only
        # runs while already disconnected (on_qrcode_update returns early when
        # _wa_connected), so _auto_offline is already True and the next poll's
        # _set_wa_connected(False, "status-session CLOSED") hits its own
        # no-change early return — silently. Deliberately not gated on
        # background_mode either: an app sitting in the tray is exactly the
        # case this fires in, and going permanently offline with no route back
        # and no word said is what the whole fix is about.
        self.error_sound.play()
        self.output(self.i18n.t("unattended_qr_session_closed"), interrupt=False)
        if not token:
            # Said explicitly, because the caller has already logged that it
            # is "closing the session" and nothing here would contradict it:
            # with no token there is no session to address. The latch is still
            # set and the announcement above still ran, which is the whole
            # user-visible half — only the HTTP call is skipped.
            logging.warning("[qr-flood] No token — nothing to close; the latch "
                            "alone now blocks the auto-start.")
            return

        # Audited only from here on: with no token there is no session to
        # address, and an audit line claiming a close that never left the
        # process is exactly the kind of entry these logs are read to trust.
        self._shutdown_audit("QR flood: closing unattended code-producing session")

        def _close():
            try:
                resp = api_post(
                    f"{self.wpp_server}:{self.wpp_port}/api/{token}/close-session",
                    headers={"Authorization": f"Bearer {token}"}, timeout=10,
                )
                logging.warning("[qr-flood] Halted unattended code-producing "
                                "session: close-session HTTP %s", resp.status_code)
            except Exception as e:
                # Never the raw exception: requests puts the whole URL, token
                # included, into the message it would have logged.
                logging.warning("[qr-flood] close-session failed: %s: %s",
                                type(e).__name__, redact_credentials(str(e)))

        threading.Thread(target=_close, daemon=True).start()

    def _wait_for_session_flushed(self, token: str, timeout: float = None) -> bool:
        """Poll status-session until WPPConnect reports the session closed, or
        the timeout elapses. Returns True if it confirmed, False on timeout.

        This confirms only the SERVER's half of the teardown — see
        connection_state.session_closed_after_flush for what it does and does
        not promise, and why the caller also waits on the Chrome process."""
        import connection_state as cs
        if timeout is None:
            timeout = self._SHUTDOWN_FLUSH_TIMEOUT
        deadline = time.monotonic() + timeout
        headers = {"Authorization": f"Bearer {token}"}
        url = f"{self.wpp_server}:{self.wpp_port}/api/{token}/status-session"
        started = time.monotonic()
        polls = 0
        while time.monotonic() < deadline:
            polls += 1
            try:
                resp = api_get(url, headers=headers, timeout=5)
                if resp.status_code in (200, 201):
                    status = resp.json().get("status", "") or ""
                    self._shutdown_audit(
                        f"flush poll #{polls} status={status!r} "
                        f"elapsed={time.monotonic()-started:.1f}s")
                    if cs.session_closed_after_flush(status):
                        return True
                else:
                    self._shutdown_audit(
                        f"flush poll #{polls} HTTP {resp.status_code}")
            except Exception as e:
                # A connection error here usually means the Node already tore the
                # session/HTTP server down — treat as flushed rather than block.
                self._shutdown_audit(
                    f"flush poll #{polls} conn-error ({e!r}) — treating as closed")
                return True
            time.sleep(self._SHUTDOWN_FLUSH_POLL)
        self._shutdown_audit(
            f"flush TIMEOUT after {polls} polls / {timeout:.1f}s "
            "— never saw CLOSED")
        return False

    def _on_query_end_session(self, event):
        """Windows is asking whether it may shut down. We say yes.

        Saying yes is the whole job, and it is what this handler exists to do:
        left to wxApp's own default (see the Bind in init_UI) WinZapp answered
        FALSE, because that default closes the top window and _on_close() vetoes
        to hide to the tray. Windows then blocks, times out, and kills us
        mid-flush — the corruption this path exists to prevent.

        Returning WITHOUT event.Skip() is what answers TRUE. Not skipping means
        the event is fully handled here, so wx reports mayEnd = !GetVeto() =
        true; skipping would resume the handler search, reach
        wxApp::OnQueryEndSession, and put the veto straight back.

        The ShutdownBlockReason registered here does NOT buy extra time, and it
        is important not to believe otherwise: Windows only holds a shutdown for
        an app that ALSO answers FALSE. All the reason string does is name us on
        the blocking-apps screen if something else vetoes. It is kept for that,
        and because _on_end_session has to destroy it either way.

        THE TEARDOWN RUNS HERE, NOT IN _on_end_session, AND THAT IS THE WHOLE
        POINT OF THIS HANDLER. Answering the query is what releases Windows to
        start ending processes, and our WPPConnect Node is one of them: it is a
        separate console process, so CSRSS terminates it during the end phase,
        concurrently with our own WM_ENDSESSION handler and with no ordering
        guarantee whatsoever. Measured on 2026-09-10, from one shutdown:

            02:20:14  node answering /list-chats in 79ms, session CONNECTED
            02:20:18  WM_QUERYENDSESSION, answered TRUE
            02:20:18  WM_ENDSESSION — teardown starts, capped at 4s
            02:20:20  close-session -> ConnectTimeout on 127.0.0.1:6300
            02:20:27  "no node pid to kill (proc gone / port free)"

        Node was dead within two seconds of us saying yes, so the graceful
        close-session had nothing left to talk to; pre_close_status was already
        '' at the top of _stop_wpp_server. Chrome went down with it, its last
        write to userDataDir landing at 02:20:18 — and the profile came back
        the next morning unable to restore the session, which is the exact
        corruption the graceful close exists to prevent. The budget was never
        the constraint here. The ORDERING was: by WM_ENDSESSION there is
        nothing left to close.

        During the query phase Windows has terminated nothing — it is still
        polling applications — so this is the only moment where our Node and
        its Chrome are guaranteed alive. _stop_wpp_server() therefore runs
        below, before we answer, and _on_end_session() finds the work already
        done and returns straight away.

        Blocking here does NOT veto: not answering yet is not answering FALSE.
        Windows waits, and if we overrun its hung-app timeout (~5s) it puts us
        on the blocking-apps screen under the reason string registered above
        rather than killing anything — which is why the reason is created
        BEFORE the teardown and destroyed after it. Every clean teardown in the
        field completes inside one second, and _WINDOWS_SHUTDOWN_BUDGET caps it
        at 4s regardless.

        The one case this gives up: a shutdown another application cancels
        after we have already closed the session. wx does not deliver
        EVT_END_SESSION when bEnding is FALSE, so nothing tells us — the
        _END_SESSION_UNSTICK_SECONDS timer below is what notices (it can only
        ever fire in a process that outlived the shutdown) and it restarts
        WPPConnect. Being offline for a minute after a cancelled shutdown is a
        trade the corruption above wins easily."""
        # First statement, before anything that can throw or return early.
        #
        # A real shutdown_audit.log covering 159 launches carries seventeen
        # runs that ended with no _stop_wpp_server line at all — eleven of
        # them overnight gaps of 7-12h, i.e. exactly the shape of Windows
        # ending the session with WinZapp open — and NOT ONE line from either
        # of these two handlers. That is the whole diagnosis stuck: it cannot
        # be told apart from "Windows never asked us" (power loss, a forced
        # Update restart that skips the polite path, a kill), and the two want
        # opposite fixes. The existing audit line in _on_end_session is too
        # late to answer it — it sits after the lock, after the
        # already-tearing-down branch that returns without auditing, and after
        # the timer. These two lines cost nothing and make the next occurrence
        # self-diagnosing.
        self._shutdown_audit("WM_QUERYENDSESSION — Windows is asking to shut down")
        try:
            import ctypes
            ctypes.windll.user32.ShutdownBlockReasonCreate(
                self.GetHandle(),
                ctypes.c_wchar_p("Closing the WhatsApp session safely..."),
            )
        except Exception:
            pass
        try:
            # While Node is still alive. See the docstring — this is the only
            # phase of a Windows shutdown where that is true.
            self._run_windows_session_teardown("WM_QUERYENDSESSION")
        except Exception:
            logging.exception("[_on_query_end_session] Teardown failed")
        try:
            import ctypes
            ctypes.windll.user32.ShutdownBlockReasonDestroy(self.GetHandle())
        except Exception:
            pass
        # No event.Skip() — see the docstring. Skipping hands the event on to
        # wxApp::OnQueryEndSession, which is the veto.

    # How long to wait after WM_ENDSESSION before assuming the shutdown was
    # cancelled and undoing _shutting_down. Per Windows docs bEnding can in
    # principle still be FALSE (another app vetoed the shutdown) — rare, but
    # this flag is never reset anywhere else, and every logout-detection
    # guard trusts it permanently once set. In the ordinary case the process
    # is long gone within this window; this is a self-healing fallback for
    # when it is not. Deliberately BELOW
    # _TEARDOWN_OWNED_ELSEWHERE_WAIT_SECONDS (80s): a loser blocked on
    # _teardown_complete_event must never outlive the timer that would have
    # freed it. Raise one and re-check the other.
    _END_SESSION_UNSTICK_SECONDS = 60.0

    def _on_end_session(self, event):
        """Windows is ending the session. The teardown has normally already run.

        _on_query_end_session() does the work, because by the time this fires
        Windows may already have terminated our WPPConnect Node — see that
        method's docstring for the measured shutdown where it had. This handler
        therefore almost always takes the already-tearing-down branch below and
        returns immediately.

        It still runs the teardown itself when nothing else has, because
        WM_ENDSESSION can arrive with no query before it: a forced shutdown
        (`shutdown /f`), some logoff paths, and a session end that another
        top-level window answered on our behalf all skip the query. In that
        case this is the last chance to close the session, late as it is.
        """
        # Before the lock, and before the already-tearing-down branch that
        # returns without reaching the audit line further down. See
        # _on_query_end_session() for why this has to be the first statement:
        # log.log is truncated every launch, so this file is the only place a
        # previous run's ending survives, and its silence is currently
        # unreadable.
        self._shutdown_audit("WM_ENDSESSION — Windows is ending the session")
        logging.warning("[_on_end_session] Windows is ending the session — stopping WPPConnect.")
        try:
            self._run_windows_session_teardown("WM_ENDSESSION")
        except Exception:
            logging.exception("[_on_end_session] Failed to stop WPPConnect cleanly")
        try:
            import ctypes
            ctypes.windll.user32.ShutdownBlockReasonDestroy(self.GetHandle())
        except Exception:
            pass
        # No Skip: wxApp::OnEndSession would run DeleteAllTLWs(), OnExit() and
        # exit() after we have already spent the Windows budget, and the
        # process is terminated the moment this returns anyway.

    def _run_windows_session_teardown(self, phase: str):
        """Stop WPPConnect for a Windows session end, once per shutdown.

        Called from _on_query_end_session() (the normal path, and the only one
        where Node is guaranteed alive) and from _on_end_session() (when no
        query preceded it). Both may also race a local quit or an IPC "quit"
        from another account, so everything is guarded by the same
        _teardown_started_lock _perform_shutdown() uses — without it two paths
        would call _stop_wpp_server() concurrently.

        A caller that finds the teardown already owned elsewhere waits
        (bounded by _WINDOWS_SHUTDOWN_BUDGET) on that path's
        _teardown_complete_event rather than returning at once, and does not
        arm its own unstick timer: resetting _shutting_down while another path
        is still tearing down would reopen the self-inflicted-logout window.

        Returns True when this call owned and performed the teardown.
        """
        with self._teardown_started_lock:
            already_tearing_down = getattr(self, "_shutting_down", False)
            self._shutting_down = True

        if already_tearing_down:
            # Do NOT just get out of the way: returning here hands control
            # straight back to Windows, which terminates the process at once
            # - potentially two seconds into a teardown whose flush wait
            # alone is budgeted at _SHUTDOWN_FLUSH_TIMEOUT. That kills the
            # session mid-write and leaves the profile unusable, which is the
            # STARTUP-with-no-_stop_wpp_server corruption pattern this whole
            # area exists to prevent. Hold the shutdown block open for the
            # same budget the owning path would have got here, and let
            # _teardown_complete_event release us the moment it is genuinely
            # done (usually well inside it).
            logging.warning("[%s] teardown already owned elsewhere - "
                            "waiting up to %ss for it to finish.",
                            phase, self._WINDOWS_SHUTDOWN_BUDGET)
            finished = self._teardown_complete_event.wait(
                timeout=self._WINDOWS_SHUTDOWN_BUDGET)
            if not finished:
                logging.warning("[%s] the owning teardown did not finish "
                                "within the Windows budget - going anyway.",
                                phase)
            return False

        def _unstick_if_still_running():
            # Reaching this at all means the process outlived the shutdown by
            # a full minute, i.e. the shutdown was cancelled — on a real one we
            # are terminated within seconds of answering the query. So this is
            # not only a flag reset any more: the teardown has already closed
            # the session and killed Node, and nothing else in the app restarts
            # a dead Node PROCESS (the health checker only ever re-issues
            # /start-session, which needs a server to talk to).
            #
            # Under the same lock as every other mutation of these two: an
            # unlocked reset can land between a genuinely-new teardown taking
            # the flag and its finally setting the event, clearing an event
            # that belongs to a teardown which really did finish and leaving
            # that teardown's loser blocked for its whole timeout.
            with self._teardown_started_lock:
                if not getattr(self, "_shutting_down", False):
                    return  # somebody already reset it
                self._shutting_down = False
                # Left set, a later genuinely-new teardown's loser would see
                # this abandoned attempt's event and wrongly assume it
                # finished.
                self._teardown_complete_event.clear()
            self._shutdown_audit("shutdown was cancelled — restarting WPPConnect")
            try:
                self._restart_wpp_after_cancelled_shutdown()
            except Exception:
                logging.exception("[%s] Failed to restart WPPConnect after a "
                                  "cancelled shutdown", phase)

        try:
            t = threading.Timer(self._END_SESSION_UNSTICK_SECONDS, _unstick_if_still_running)
            t.daemon = True
            t.start()
        except Exception:
            logging.exception("[%s] Failed to arm the _shutting_down safety timer",
                              phase)

        try:
            # Deliberately still on this thread: the caller is answering
            # Windows, and Windows may terminate the process the moment it
            # does, so a background thread doing the teardown would be killed
            # mid-flush.
            self._shutdown_audit(
                f"{phase} — teardown capped at {self._WINDOWS_SHUTDOWN_BUDGET}s")
            self._stop_wpp_server(budget=self._WINDOWS_SHUTDOWN_BUDGET)
        except Exception:
            logging.exception("[%s] Failed to stop WPPConnect cleanly", phase)
        try:
            # This path never calls _perform_shutdown(), so without this
            # call it has none of that method's write protection. Does NOT
            # also close the DB here: the shutdown can still turn out to be one
            # another app cancels, and closing the DB now would leave it
            # unusable if the user goes back to using WinZapp.
            self._flush_pending_debounced_saves()
        except Exception:
            logging.exception("[%s] Failed to flush pending debounced saves", phase)
        # A caller that lost the lock race above waits on this before
        # self-terminating — without it, it would sit out its full bounded
        # wait instead of noticing this path already finished.
        self._teardown_complete_event.set()
        return True

    def _restart_wpp_after_cancelled_shutdown(self):
        """Bring WPPConnect back after a Windows shutdown that never happened.

        _on_query_end_session() closes the session and kills Node before
        answering, because that is the only moment Node is still alive (see its
        docstring). When another application then cancels the shutdown, wx
        never delivers EVT_END_SESSION — bEnding is FALSE and wxApp drops the
        message — so the only thing that notices is the unstick timer, a minute
        later.

        Deliberately does NOT go through ensure_wpp_running(): that shows
        ApiStartupDialog, a modal that would take focus away from whatever the
        user went back to doing, and re-runs install/version checks that were
        already done at launch. Just the spawn, the wait, and a reconnect.
        """
        if getattr(self, "wpp_custom_api", False):
            return          # not ours to start
        if self._is_wpp_running():
            return
        self._start_wpp_background()
        deadline = time.time() + 120
        while time.time() < deadline:
            if self._is_wpp_running():
                break
            time.sleep(1)
        else:
            logging.error("[shutdown-cancelled] WPPConnect did not come back up.")
            return
        logging.info("[shutdown-cancelled] WPPConnect is listening again — reconnecting.")
        try:
            self._reconnect_websocket_now()
        except Exception:
            logging.exception("[shutdown-cancelled] WebSocket reconnect failed")
        try:
            self.check_wa_connection_http()
        except Exception:
            logging.exception("[shutdown-cancelled] Connection re-check failed")

    # How long to wait for WPPConnect's /close-session request to confirm
    # Chrome closed gracefully before giving up and force-killing.
    #
    # This used to be a flat 2-second sleep, and that is very likely how a
    # perfectly valid WhatsApp Web session ended up "logged out" after a
    # restart. The linked-device credentials do NOT live in WinZapp's own
    # settings — they live in Chrome's profile (userDataDir), inside IndexedDB,
    # which is a LevelDB store. `taskkill /F /T` on the Node process kills
    # every child, Chrome included, without giving it a chance to flush and
    # close those files. A LevelDB torn mid-write comes back corrupted, and a
    # WhatsApp Web that cannot read its own key material behaves exactly like a
    # device that was unlinked — while the phone, which was never told
    # anything, keeps listing the session as active. That is precisely the
    # reported symptom.
    #
    # The budget below is only this request's own timeout. A 200 from
    # /close-session is NOT proof Chrome is down, and used to be treated as
    # such: wppconnect's own close() (node_modules, api/whatsapp.js) returns
    # true without closing anything when the page is already closed, and wraps
    # both page.close() and browser.close() in `.catch(() => null)`, so a
    # failure or a timeout also reports success. That is why the caller waits
    # on two further signals — the CLOSING→CLOSED transition, then the Chrome
    # process itself releasing userDataDir — instead of the HTTP status.
    _WPP_GRACEFUL_STOP_SECONDS = 10

    # Extra grace on top of _SHUTDOWN_FLUSH_TIMEOUT for the auth token file
    # to land on disk before killing Node. Bounded like every shutdown wait
    # here: 5s covers an ordinary small-file write without stalling a user
    # who genuinely wants out.
    _TOKEN_PERSIST_GRACE_SECONDS = 5.0
    _TOKEN_PERSIST_POLL_SECONDS = 0.2

    def _wait_for_token_persisted(self, token: str) -> bool:
        """Block (briefly, boundedly) until the auth token for `token`'s
        session actually exists on disk at WINZAPP_TOKEN_STORE_DIR, or the
        grace window runs out.

        Guards the SECONDARY, REST-API-level token file
        (<session>.data.json), not the primary credential store — for a
        modern multi-device session that file is close to a placeholder
        (WASecretBundle reports the literal string 'MultiDevice', not real
        secret material). The REAL credential store is Chrome's own
        userDataDir/IndexedDB (LevelDB) profile, protected by
        _wait_for_session_flushed()'s wait for a genuine CLOSED status —
        treat that as the function actually carrying the weight, this one as
        a cheap, harmless second check in case some path still depends on
        this file.

        Returns True if found, False if the grace window ran out — callers
        must treat False as "log it and proceed anyway", never as a reason
        to keep the process alive forever.
        """
        token_dir = os.environ.get("WINZAPP_TOKEN_STORE_DIR")
        session_name = (token or "").split(":")[0]
        if not token_dir or not session_name:
            return True  # nothing to check — never block on missing config
        token_file = os.path.join(token_dir, f"{session_name}.data.json")
        deadline = time.monotonic() + self._TOKEN_PERSIST_GRACE_SECONDS
        while True:
            try:
                if os.path.isfile(token_file) and os.path.getsize(token_file) > 2:
                    return True
            except OSError:
                pass
            if time.monotonic() >= deadline:
                return False
            time.sleep(self._TOKEN_PERSIST_POLL_SECONDS)

    # Short, bounded grace given to an already-running self-inflicted
    # session restart (_recovery_restart_active / _restarting_wpp_session)
    # to finish before _stop_wpp_server() proceeds. Neither holds
    # _teardown_started_lock (they are a close+start cycle on their own
    # thread, not teardown), so a quit landing mid-restart would otherwise
    # race this method's own close-session/flush-wait against the restart's
    # start-session on the same session — the restart's start-session can
    # flip status back to INITIALIZING mid-flush-wait, starving it of the
    # CLOSED reading and risking a "Session Unpaired" taskkill. Deliberately
    # short, not their own ~30-60s worst case: this narrows the race rather
    # than closing it.
    _SELF_RESTART_YIELD_SECONDS = 5.0
    _SELF_RESTART_YIELD_POLL_SECONDS = 0.2

    def _yield_to_in_progress_self_restart(self):
        if not (self._session_restart_owned()
                or getattr(self, "_restarting_wpp_session", False)):
            return
        deadline = time.monotonic() + self._SELF_RESTART_YIELD_SECONDS
        while time.monotonic() < deadline:
            if not (self._session_restart_owned()
                    or getattr(self, "_restarting_wpp_session", False)):
                return
            time.sleep(self._SELF_RESTART_YIELD_POLL_SECONDS)

    def _note_status_for_profile_health(self, status):
        """Watch for a paired session that starts and dies without connecting.

        A wedged Chrome profile does not announce itself. WhatsApp Web loads
        and even authenticates — the phone lists the linked device as active —
        but wa-js never reaches WPP.isReady, wppconnect's injectApi() times
        out, and the session cycles INITIALIZING -> CLOSED with the UI saying
        only "offline". Nothing in that loop ever recovers, and until this
        existed the only way out was clearing all local data and pairing again.

        See core/profile_recovery.py for why the signature is exactly this
        shape and why three cycles rather than one.
        """
        try:
            tracker = getattr(self, "_profile_health", None)
            if tracker is None:
                from core.profile_recovery import ProfileHealthTracker
                tracker = self._profile_health = ProfileHealthTracker()
            paired = bool(self.settings.get("privateinfo", {}).get("paired"))
            # Only HALF of what used to happen here moved out. Clearing
            # _profile_recovery_attempted went to _set_wa_connected()'s own
            # "connection just came back up" branch, because this status
            # string alone does not mean the live isConnected() probe agrees,
            # and re-arming the recovery budget on it regardless let a second
            # recovery start mid QR-flood on an event the flood counter had
            # already counted (issue #202).
            #
            # The generation ladder deliberately stays. It never interacts
            # with _unattended_qr_events at all — it only chooses WHICH
            # snapshot a restore reaches for — so #202's argument does not
            # reach it, and moving it costs a property it depends on. It is
            # re-asserted on every CONNECTED poll rather than once per
            # transition, which is what lets it self-heal on a launch where
            # the write could not land: a manual re-pair completes inside
            # Connect.show_connection_dial(), which runs BEFORE
            # prepare_sync() opens the database, so the write
            # _set_profile_recovery_generation() would do there is a silent
            # no-op (see its own db guard) — and a transition-only reset
            # never runs again for the rest of that launch, leaving a stale
            # ladder to send the next break at a day-old .prev snapshot
            # instead of the newest one. Idempotent and cheap, so paying for
            # it every poll is the right trade.
            if (status or "").upper() == "CONNECTED" and self._profile_recovery_generation():
                # Whatever was put back is working. The ladder starts over,
                # so a future break restores the newest snapshot first again.
                self._set_profile_recovery_generation(0)
            if tracker.note_status(status, paired=paired):
                self._recover_suspect_profile()
        except Exception:
            logging.exception("[profile-health] check failed (non-fatal)")

    def _note_session_start_for_profile_health(self):
        """Arm the profile-health tracker when we ask for a session start.

        Wrapped like its sibling: profile health observes the connection poll
        and must never be able to change that poll's verdict.
        """
        try:
            tracker = getattr(self, "_profile_health", None)
            if tracker is None:
                from core.profile_recovery import ProfileHealthTracker
                tracker = self._profile_health = ProfileHealthTracker()
            paired = bool(self.settings.get("privateinfo", {}).get("paired"))
            tracker.note_session_start_requested(paired=paired)
        except Exception:
            logging.exception("[profile-health] start note failed (non-fatal)")

    def _recover_suspect_profile(self, reason="session started and died 3x "
                                              "without connecting",
                                 on_give_up=None):
        """Put the last clean-shutdown profile back, or say why we cannot.

        Returns True when a restore was actually started, and `on_give_up` is
        called — on the UI thread — if that restore then fails. A False return
        means nothing was started and the caller should handle it inline;
        `on_give_up` is deliberately *not* called in that case, so a caller
        that both passes it and falls through on False cannot act twice. The
        QR caller is exactly that shape: it is choosing between repairing the
        profile and sending the user off to re-pair by hand, and only one of
        those may happen.

        Runs at most once per launch. The retry it triggers is the ordinary
        health-checker /start-session on the next poll, so if the restore did
        not help, the tracker simply never fires again this run and the user
        is left exactly where they were — offline, but told about it.

        The order is load-bearing: close the session BEFORE touching the
        profile. Restoring under a running browser overwrites a leveldb while
        its owner holds it open, which manufactures the very corruption this
        recovers from. wait_for_profile_release() is what makes "closed"
        mean the files are actually free, and it kills an orphaned Chrome as
        its fallback — the same helper _stop_wpp_server() relies on.
        """
        if getattr(self, "_profile_recovery_attempted", False):
            return False

        # A browser that cannot start is not a profile that cannot connect,
        # and the tracker that calls this cannot tell them apart: it counts
        # sessions that died without connecting, which is exactly what a
        # failed Chrome launch produces. Checked BEFORE the once-per-launch
        # latch is taken, so a launch spent refusing here still has its one
        # real recovery left for the fault this exists to fix.
        #
        # The cost of getting it wrong is not a wasted restore, it is the
        # account. Measured on 2026-09-10 on an install whose Chromium was
        # missing icudtl.dat: the recovery fired, spent both snapshot
        # generations on a profile that was never at fault, and left the user
        # unpaired — then unable to pair, because the QR needs the same
        # browser that will not start. Restoring a snapshot cannot put an
        # `icudtl.dat` back.
        broken, problem = self.browser_payload_blocks_startup()
        if broken:
            logging.error(
                "[profile-recovery] refusing to restore: the browser itself "
                "cannot start (%s: %s). The profile is not the fault here.",
                problem, broken,
            )
            self._shutdown_audit(
                "profile recovery refused — browser payload incomplete (%s)" % problem
            )
            wx.CallAfter(self._announce_browser_beyond_repair)
            return False

        self._profile_recovery_attempted = True

        session_name = (getattr(self, "token", "") or "").split(":")[0]
        global_dir = getattr(self, "global_dir", None)
        if not session_name or not global_dir:
            return False

        from core import profile_recovery
        self._shutdown_audit("profile suspect — %s" % reason)

        # Which generation to put back — see pick_restore_generation() (module
        # level), which holds the whole decision so
        # _profile_restore_worth_trying() can ask the same question without
        # spending anything.
        generation = self._profile_recovery_generation()
        self._set_profile_recovery_generation(generation + 1)

        # WhatsApp refused to restore a session from the profile currently on
        # disk. Write that down before anything moves it: after the restore
        # below, the live profile stops being evidence of anything, and the
        # next launch would have nothing left to reason from.
        profile_recovery.note_profile_rejected(global_dir, session_name)

        prefer_previous, verdict, from_ladder = pick_restore_generation(
            profile_recovery, global_dir, session_name, generation)
        if from_ladder:
            logging.warning("[profile-recovery] the newest snapshot did not hold "
                            "— restoring the generation before it.")
        if verdict in ("climbed", "refused"):
            logging.warning(
                "[profile-recovery] the %s snapshot holds a profile state "
                "WhatsApp has already refused — restoring it would restore "
                "the failure.",
                "previous" if from_ladder else "newest",
            )
        if verdict == "climbed":
            logging.warning("[profile-recovery] climbing to the generation "
                            "before it in this same launch.")
        elif verdict == "refused":
            self._shutdown_audit(
                "profile suspect — every snapshot holds a state WhatsApp "
                "has already refused, nothing to restore")
            logging.error("[profile-recovery] no snapshot holds a state "
                          "that has not already been refused — cannot "
                          "recover session %s.", session_name[:12])
            wx.CallAfter(self._announce_profile_beyond_repair)
            return False

        if verdict == "missing":
            # Nothing to restore. Say so plainly rather than leaving the user
            # staring at "offline": this is the one outcome where the only fix
            # is a human deciding to pair again, and a blind user has no way to
            # discover that from silence.
            logging.error("[profile-recovery] session %s looks broken and there "
                          "is no snapshot to restore.", session_name[:12])
            wx.CallAfter(self._announce_profile_beyond_repair)
            return False

        def _restore():
            try:
                token = getattr(self, "token", "")
                if token:
                    try:
                        api_post(
                            f"{self.wpp_server}:{self.wpp_port}/api/{token}/close-session",
                            headers={"Authorization": f"Bearer {token}"}, timeout=10,
                        )
                    except Exception as e:
                        logging.warning("[profile-recovery] close-session failed: %s: %s",
                                        type(e).__name__, redact_credentials(str(e)))
                self.wait_for_profile_release(session_name, timeout=20.0)
                if (getattr(self, "_qr_flood_halted", False)
                        or self._is_pairing_dialog_active()
                        or getattr(self, "_pairing_in_progress", False)):
                    # Only reachable through a restore that overstayed
                    # _RESTORE_FLIGHT_IGNORE_SECONDS (websocket_client.py): its
                    # codes were counted again, the halt fired, and on a paired
                    # install the pairing dialog opened. Copying now would write
                    # the profile a new pairing is about to use — or, once one
                    # has succeeded, fail on its open files and announce "no
                    # saved copy" over a freshly paired session. Leaving the
                    # profile as it is costs nothing: the user is re-pairing.
                    logging.warning("[profile-recovery] the session was halted "
                                    "or re-pairing began while the restore was "
                                    "stalled — not restoring over it.")
                    self._shutdown_audit("profile restore abandoned — halted or "
                                         "re-pairing began first")
                    # Give back the rung this call climbed: no restore
                    # happened, so the next launch must not reach for `.prev`
                    # on the strength of one. (The rejected fingerprint stays
                    # recorded — WhatsApp really did refuse that profile.)
                    self._set_profile_recovery_generation(generation)
                    return
                taken_at = profile_recovery.snapshot_taken_at(
                    global_dir, session_name, prefer_previous=prefer_previous)
                if profile_recovery.restore_snapshot(
                        global_dir, session_name, prefer_previous=prefer_previous):
                    self._shutdown_audit("profile restored from snapshot")
                    # The suspension below lasts one launch; the gap the restore
                    # leaves in WhatsApp Web's own database does not. Recorded
                    # for good (core/remote_reconcile.py, "Periods a profile
                    # restore rolled back").
                    try:
                        self._record_rollback_gap(taken_at, time.time())
                    except Exception:
                        logging.exception("[profile-recovery] could not record "
                                          "the rolled-back period")
                    # The restore rolled WhatsApp Web's OWN store back to
                    # whenever the snapshot was taken — up to
                    # SNAPSHOT_MAX_AGE_SECONDS. Measured 2026-09-10: the
                    # snapshot was from 09-09 21:20 and the restore ran at
                    # 09-10 18:36, so the browser came back knowing 21 hours
                    # less than WinZapp's own database did.
                    #
                    # That matters far beyond a stale view, because
                    # _reconcile_active_conversation_with_remote() reads "the
                    # server does not have this message" as "the phone deleted
                    # it" and mirrors it — deleting, from the only complete
                    # copy, messages that were correctly synced before the
                    # rollback. The server cannot be the source of truth about
                    # deletions while it is behind us; nothing here can tell a
                    # real deletion from a message the rolled-back store simply
                    # has not heard of yet.
                    #
                    # So mirroring is suspended for the rest of the launch. A
                    # genuine phone-side deletion missed until the next launch
                    # is a cosmetic staleness; a mirrored rollback is
                    # irreversible data loss, and only one of those is worth
                    # risking.
                    self._remote_deletions_untrusted = True
                    logging.warning(
                        "[profile-recovery] the restored profile is older than "
                        "the local history — not mirroring remote deletions for "
                        "the rest of this launch."
                    )
                    # The QR burst that triggered this was produced by the
                    # profile now moved aside; counting it against the flood
                    # ceiling would halt a session that is about to be fine.
                    self._unattended_qr_events = 0
                    wx.CallAfter(self._announce_profile_restored)
                else:
                    wx.CallAfter(self._announce_profile_beyond_repair)
                    if on_give_up is not None:
                        wx.CallAfter(on_give_up)
            except Exception:
                logging.exception("[profile-recovery] restore failed")
                wx.CallAfter(self._announce_profile_beyond_repair)
                if on_give_up is not None:
                    wx.CallAfter(on_give_up)
            finally:
                # Released only once the profile is back in place, so the very
                # next health poll starts a session on the restored profile
                # rather than on the broken one.
                self._recovery_restart_active = False
                self._profile_restore_in_flight = False

        # This sequence is a close/kill/restore cycle that owns the browser and
        # the profile for as long as it runs — up to ~25 s of it spent inside
        # wait_for_profile_release() while Chrome still holds the directory.
        # It is exactly what _recovery_restart_active exists to announce, and
        # not setting it cost a session on 2026-09-09: the 30 s health poll
        # landed 14 s in, read CLOSED, and fired its own /start-session into a
        # profile that was still locked. That start failed with "The browser is
        # already running", which (before the createSessionUtil.ts fix that
        # ships with this change) left the session wedged in INITIALIZING for
        # good — so the restore completed onto a profile nothing could start
        # any more, and the app sat offline in silence until it was restarted
        # by hand.
        #
        # Setting it also makes _self_inflicted_teardown_expected() true for
        # the duration, which is correct on its own terms: the close-session
        # above is ours, so the CLOSED/loggedOut readings that follow it are
        # the expected result of this call and not WhatsApp unlinking the
        # device. And _yield_to_in_progress_self_restart() will now give a quit
        # landing mid-restore a few seconds to let the profile finish being put
        # back, instead of tearing down on top of a half-copied leveldb.
        self._recovery_restart_active = True
        self._profile_restore_in_flight = True
        self._profile_restore_started_at = time.monotonic()
        try:
            threading.Thread(target=_restore, daemon=True).start()
        except Exception:
            # A flag nobody clears blocks every future auto-start for the life
            # of the process — worse than the race it guards against.
            self._recovery_restart_active = False
            self._profile_restore_in_flight = False
            raise
        return True

    def _profile_restore_worth_trying(self) -> bool:
        """Would _recover_suspect_profile() start a restore right now?

        Asked by _handle_unattended_qr() (websocket_client.py) before it tries
        a repair on a code that has not yet cleared the startup grace or the
        confirmation count (issue #203). The distinction that matters there is
        not "is the profile broken" — a code already proves that — but what the
        attempt would *cost* if it cannot help: with nothing restorable,
        _recover_suspect_profile() answers with
        _announce_profile_beyond_repair() (error sound, speech and a modal
        MessageBox), spends the once-per-launch latch and climbs the persisted
        generation ladder, all before the gates that exist to keep a
        conclusion like that off a single unconfirmed reading. So only a
        restore that can actually start is attempted early; everything else
        waits for the gates exactly as before.

        Side-effect free, and conservative: any failure to read reads as
        False, which only means "wait for the gates", never a lost repair —
        the confirmed path still calls _recover_suspect_profile() itself.
        """
        try:
            if (getattr(self, "_profile_recovery_attempted", False)
                    or getattr(self, "_profile_restore_in_flight", False)):
                return False
            # Another close/start cycle already owns the session: the
            # power-resume restart (_recovery_restart_active) or the in-place
            # restart after a detached page (_restarting_wpp_session). Starting
            # a restore under either gives the session two owners, and each
            # one's next step (a restart, a /start-session once its flag
            # clears) lands on a profile the other is copying back. Both stop
            # on their own once the session asks for a code, so waiting costs
            # at most the next code — the review of issue #203 found this.
            if (getattr(self, "_recovery_restart_active", False)
                    or getattr(self, "_restarting_wpp_session", False)):
                return False
            session_name = (getattr(self, "token", "") or "").split(":")[0]
            global_dir = getattr(self, "global_dir", None)
            if not session_name or not global_dir:
                return False
            from core import profile_recovery
            _, verdict, _ = pick_restore_generation(
                profile_recovery, global_dir, session_name,
                self._profile_recovery_generation())
            return verdict in ("ok", "climbed")
        except Exception:
            logging.exception("[profile-recovery] could not tell whether a "
                              "restore is worth trying — waiting for the gates")
            return False

    _PROFILE_RECOVERY_GENERATION_KEY = "profile_recovery_generation"

    def _profile_recovery_generation(self) -> int:
        """How many recoveries have been attempted since the last CONNECTED."""
        try:
            if getattr(self, "db", None) is None:
                return 0
            return max(0, int(self.db.get_metadata_json(
                self._PROFILE_RECOVERY_GENERATION_KEY, 0) or 0))
        except Exception:
            return 0

    def _set_profile_recovery_generation(self, value: int) -> None:
        """Best effort, like every other persist on this path: losing it costs
        a repeated restore attempt, never the profile."""
        try:
            if getattr(self, "db", None) is not None:
                self.db.set_metadata_json(
                    self._PROFILE_RECOVERY_GENERATION_KEY, max(0, int(value)))
        except Exception as exc:
            logging.warning("[profile-recovery] could not persist the generation: %s", exc)

    def _announce_profile_restored(self):
        try:
            self.output(self.i18n.t("profile_restored_from_snapshot"), interrupt=False)
        except Exception:
            logging.exception("[profile-recovery] announcement failed")

    def _announce_browser_beyond_repair(self):
        """The browser WinZapp bundles cannot start, so nothing else can work.

        Once per launch. Its sibling below is bounded by the once-per-launch
        recovery latch; this one deliberately fires BEFORE that latch is taken
        (a launch spent refusing must keep its real recovery), so nothing else
        bounds it. Both triggers behind it reset within a session — the QR
        route clears its own dialog latch, and ProfileHealthTracker.reset()
        re-arms the count — so a second flood would otherwise replay the error
        sound and a modal box over a user who has already been told.

        Spoken as well as shown, with the error sound, for the same reason as
        _announce_profile_beyond_repair(): this only ever happens while already
        offline, where _set_wa_connected(False, ...) has hit its no-change early
        return and said nothing at all. A blind user has no way to discover any
        of this from silence — and here silence is worse than usual, because the
        thing that looks broken (WhatsApp) is not the thing that is.
        """
        if getattr(self, "_browser_payload_announced", False):
            logging.info("[browser-payload] already announced this launch")
            return
        self._browser_payload_announced = True
        try:
            self.error_sound.play()
        except Exception:
            pass
        try:
            self.output(self.i18n.t("browser_install_broken"), interrupt=False)
            if not getattr(self, "background_mode", False):
                wx.MessageBox(
                    self.i18n.t("browser_install_broken"),
                    self.i18n.t("error").format(app_name=self.app_name),
                    wx.OK | wx.ICON_ERROR,
                )
        except Exception:
            logging.exception("[browser-payload] announcement failed")

    def _announce_profile_beyond_repair(self):
        """The dead end: the profile is unusable and there is no restore point.

        Spoken as well as shown, and with the error sound, because by
        construction this only happens while already offline — where
        _set_wa_connected(False, ...) has long since hit its no-change early
        return and said nothing. Same reasoning as _halt_unattended_qr_session().
        """
        try:
            self.error_sound.play()
        except Exception:
            pass
        try:
            self.output(self.i18n.t("profile_corrupted_repair_needed"), interrupt=False)
            if not getattr(self, "background_mode", False):
                wx.MessageBox(
                    self.i18n.t("profile_corrupted_repair_needed"),
                    self.i18n.t("error").format(app_name=self.app_name),
                    wx.OK | wx.ICON_ERROR,
                )
        except Exception:
            logging.exception("[profile-recovery] announcement failed")

    def _capture_profile_snapshot(self, session_name, browser_closed_cleanly, budget):
        """Keep a restore point for this session's Chrome profile.

        Called only from _stop_wpp_server(), right after
        wait_for_profile_release() confirmed Chrome let go, on a close that
        WPPConnect acknowledged. (_refresh_profile_snapshot_live() is the one
        other place a snapshot is taken, behind the same gates, and it calls
        capture_snapshot() itself.) That is the only moment WinZapp can prove the
        profile is both quiescent and completely written — see
        core/profile_recovery.py for why a snapshot of a live profile is worse
        than no snapshot at all.

        Three refusals, all deliberate:

        * `browser_closed_cleanly` False means the graceful close-session never
          confirmed, so the leveldb may be mid-write. That is precisely the
          state a restore point must never capture.
        * A `budget` means Windows owns the clock (WM_ENDSESSION, ~5s before
          the process is killed as hung). Spending it copying hundreds of
          megabytes would take the time away from the flush that prevents the
          corruption in the first place — and the run that most needs a
          snapshot is the one before, not this one.
        * The session never reported CONNECTED in this run. A profile can be
          quiescent, completely written, closed in perfect order — and hold no
          login at all, because WhatsApp Web logged itself out of a profile it
          could not use (`post_logout=1`) hours earlier. Snapshotting that
          overwrites the one restore point that would have rescued the account
          with a copy of the failure. It came within hours of happening on a
          real install: the profile broke, the good snapshot was 16.5 h old,
          and the 24 h refresh window was the only thing standing between a
          successful hand-restore and a permanently lost session. Closing
          cleanly is evidence about *how* the profile was written, never about
          whether what was written is worth keeping.

        Never raises: a missing restore point is a nicety lost, while a
        teardown that dies here is the corruption itself.
        """
        if not session_name or not browser_closed_cleanly or budget is not None:
            return
        tracker = getattr(self, "_profile_health", None)
        if tracker is not None and not tracker.ever_connected():
            # Absent tracker means no connection poll ever ran, which is not
            # evidence of anything — fall through and behave as before.
            logging.info(
                "[profile-snapshot] Not refreshing the restore point: this run "
                "never reached CONNECTED, so the profile on disk may be the "
                "broken one.")
            self._shutdown_audit("profile snapshot skipped — never connected this run")
            return
        global_dir = getattr(self, "global_dir", None)
        if not global_dir:
            return
        try:
            from core import profile_recovery
            # How old the snapshot may be before a clean close refreshes it is
            # the user's choice (Settings > Cópia de segurança), 24 h by default.
            if profile_recovery.capture_snapshot(
                    global_dir, session_name,
                    max_age=_close_snapshot_max_age(getattr(self, "settings", {})),
                    lock_wait=2.0):
                self._shutdown_audit("profile snapshot refreshed")
            # A copy staged by a backup with WinZapp open that never got to be
            # confirmed (the app is closing) is dead weight the size of the
            # profile; nothing will promote it any more.
            profile_recovery.discard_pending_snapshot(global_dir, session_name)
        except Exception:
            logging.exception("[profile-snapshot] failed (non-fatal)")

    # ── Profile backup while WinZapp is open (Settings > Cópia de segurança) ──
    # A restore point of the Chrome profile can only be copied from a profile
    # Chrome has released (core/profile_recovery.py), and the one thing the age
    # of that copy decides is how much WhatsApp Web forgets if it is ever put
    # back. Refreshing it only at close leaves an app that stays open for days
    # with an old copy, so this optionally closes the session on a schedule,
    # copies, and starts it again.

    # The copy is roughly a gigabyte; the one at close is capped at 25 s
    # because the user is waiting for the app to exit. Here the user chose (or
    # agreed to) the disconnection, and a cap that a slow disk always exceeds
    # would mean the backup never happens. Past it the attempt is abandoned and
    # the previous snapshot kept. It must stay well inside
    # _AUTO_RESTART_LOGOUT_GRACE_SECONDS: that grace starts with the restart,
    # and a QRCODE reading after it expires counts toward a real logout.
    _LIVE_SNAPSHOT_BUDGET_SECONDS = 180.0

    # A copy only becomes the restore point once the session restarted on those
    # exact bytes reaches CONNECTED and is still CONNECTED a little later. A
    # clean close is no evidence the profile will be accepted (see
    # previous_snapshot_dir()), and a copy WhatsApp then refuses must not push
    # the known-good generation out of `.prev`. The rejection signature lands
    # ~7 s into the page load, so the stability wait covers it.
    _LIVE_SNAPSHOT_CONFIRM_SECONDS = 120.0
    _LIVE_SNAPSHOT_STABLE_SECONDS = 30.0

    # How long to wait for a send already on the wire before giving up on this
    # round. Nothing can recall a request in flight, and cutting one off with
    # close-session is what turns a send seconds from succeeding into an
    # unconfirmed one the queue deliberately never retries.
    # Sized above the send timeouts themselves (25 s for text, 30 s for a
    # voice message): nobody is waiting on this thread, and giving up early
    # only throws the round away. stop()'s 4 s drain is the opposite case —
    # there the user is waiting for the app to quit.
    _LIVE_SNAPSHOT_DRAIN_SECONDS = 35.0

    def _live_snapshot_cancelled(self) -> bool:
        """Stop the copy: WinZapp is closing or WPPConnect is updating."""
        return bool(getattr(self, "_shutting_down", False)
                    or getattr(self, "_wpp_updating", False))

    def _window_can_ask(self) -> bool:
        """Whether a confirmation would be seen and heard: the main window is
        the active one. A modal over a window in the tray, or behind another
        app, is not announced — and would hold every later backup until
        someone found and answered it.

        Reads the flag _on_window_activate() keeps, never IsActive(): this runs
        on the poll thread, where Windows answers "no active window" for a
        thread that owns none — which made the question never appear at all.
        """
        return bool(getattr(self, "_main_window_active", False))

    def _live_snapshot_session_accepted(self) -> bool:
        """After the restart: did the session come back on the copied bytes?"""
        import connection_state as cs
        settled = self._wait_for_status(
            cs.recovery_settled, self._LIVE_SNAPSHOT_CONFIRM_SECONDS,
            stop_when_connected=False)
        if not cs.recovery_connected(settled):
            logging.warning("[profile-backup] the session did not come back "
                            "connected (%s) — the copy is not kept.", settled or "?")
            return False
        time.sleep(self._LIVE_SNAPSHOT_STABLE_SECONDS)
        if self._live_snapshot_cancelled() or getattr(self, "_profile_restore_in_flight", False):
            return False
        status = self._raw_session_status()
        if not cs.recovery_connected(status):
            logging.warning("[profile-backup] the session connected and then left "
                            "(%s) — the copy is not kept.", status or "?")
            return False
        return True

    def _live_snapshot_session(self):
        """(global_dir, session_name), or (None, None) when there is none."""
        session_name = (getattr(self, "token", "") or "").split(":")[0]
        global_dir = getattr(self, "global_dir", None)
        if not session_name or not global_dir:
            return None, None
        return global_dir, session_name

    def _live_snapshot_blocked_reason(self, check_queue: bool = True) -> str:
        """'' when the session may be closed for a backup right now, else why not.

        Every refusal is a "not now", never a "no": the next poll asks again.
        """
        if not getattr(self, "_wa_connected", False):
            return "WhatsApp is not connected"
        if self._live_snapshot_session() == (None, None):
            return "no session"
        if getattr(self, "_initial_sync_running", False):
            return "the initial sync is running"
        if getattr(self, "_media_sync_running", False):
            return "a media sync is running"
        if self._self_inflicted_teardown_expected() or self._session_restart_owned():
            return "another close/start cycle owns the session"
        if (time.time() - getattr(self, "_last_wpp_session_restart_ts", 0)
                < self._WPP_SESSION_RESTART_COOLDOWN):
            return "the session was restarted moments ago"
        if getattr(self, "_pairing_in_progress", False) or self._is_pairing_dialog_active():
            return "pairing is in progress"
        queue = getattr(self, "message_queue", None)
        if (check_queue and queue is not None
                and not getattr(self, "offline_mode", False)
                and queue.has_work()):
            # Closing the session under a message the user just sent is how a
            # send becomes "not confirmed", so the free check before the
            # question waits for a quiet moment; the next poll costs nothing.
            # Asked again by the worker it would instead throw a whole interval
            # away for a message the hold there already protects — and in
            # manual offline mode the queue never empties at all, so a single
            # message left in it would stop every backup for as long as the
            # switch is on.
            return "messages are still being sent"
        tracker = getattr(self, "_profile_health", None)
        if tracker is not None and not tracker.ever_connected():
            # Same refusal as _capture_profile_snapshot(): a profile that never
            # authenticated this run may be the broken one.
            return "this run never reached CONNECTED"
        return ""

    def _maybe_refresh_profile_snapshot_live(self, now=None):
        """Once per periodic poll: start (or ask about) a backup when it is due.

        The interval counts from the last attempt, and the first call only
        starts the clock, so nothing closes the session right after launch.
        """
        now = time.monotonic() if now is None else now
        last = getattr(self, "_live_snapshot_last_attempt", None)
        if last is None:
            self._live_snapshot_last_attempt = now
            return
        enabled, interval, confirm = _live_snapshot_policy(getattr(self, "settings", {}))
        if not enabled or getattr(self, "_live_snapshot_pending", False):
            return
        global_dir, session_name = self._live_snapshot_session()
        if session_name is None:
            return
        from core import profile_recovery
        age = profile_recovery.snapshot_age_seconds(global_dir, session_name)
        if not _live_snapshot_due(enabled, interval, now - last, age):
            return
        reason = self._live_snapshot_blocked_reason()
        if reason:
            logging.info("[profile-backup] a backup is due but not now: %s.", reason)
            return
        if confirm and not self._window_can_ask():
            # Not consumed: asked at the next poll with the window in front.
            logging.info("[profile-backup] a backup is due; waiting for the "
                         "WinZapp window to be active to ask about it.")
            return
        self._live_snapshot_previous_attempt = last
        self._live_snapshot_last_attempt = now
        self._live_snapshot_pending = True
        if confirm:
            wx.CallAfter(self._ask_live_profile_snapshot)
        else:
            self._start_live_snapshot_worker()

    def _postpone_live_snapshot(self):
        """Give the interval back after a round that closed nothing.

        _maybe_refresh_profile_snapshot_live() spends the interval before the
        question is even asked, so a worker that then bows out (another cycle
        took the session, a send is still on the wire) would otherwise cost a
        whole interval — a day on the shipped settings — for a backup the user
        just agreed to. Putting the previous mark back leaves it due again at
        the next poll, and uses that poll's own clock rather than a second
        reading of this one.

        A Yes is deliberately not carried over: the next poll asks again, since
        by then the answer may have changed (the user is mid-call, or writing).
        """
        previous = getattr(self, "_live_snapshot_previous_attempt", None)
        if previous is not None:
            self._live_snapshot_last_attempt = previous

    def _ask_live_profile_snapshot(self):
        """Main thread. The same dialog as marking every chat as read: No is
        the default, because it pops up over whatever the user is doing and a
        habitual Enter must not disconnect WhatsApp. "Don't ask again" only
        counts together with Yes; the settings tab turns asking back on."""
        try:
            t = self.i18n.t
            confirmed, dont_ask_again = confirm_with_checkbox(
                self,
                t("profile_backup_live_confirm"),
                t("profile_backup_live_confirm_title"),
                t("profile_backup_live_dont_ask_again"),
                yes_label=t("yes_button"),
                no_label=t("no_button"),
                checked=False,
                default_yes=False,
            )
        except Exception:
            logging.exception("[profile-backup] could not ask about the backup")
            self._live_snapshot_pending = False
            return
        if not confirmed:
            # Asked again after another interval, not at the next poll.
            logging.info("[profile-backup] the user postponed the backup.")
            self._live_snapshot_pending = False
            return
        if dont_ask_again:
            self.settings.setdefault("profile_backup", {})["live_snapshot_confirm"] = False
            self.save_settings()
        self._start_live_snapshot_worker()

    def _start_live_snapshot_worker(self):
        threading.Thread(target=self._refresh_profile_snapshot_live, daemon=True).start()

    def _refresh_profile_snapshot_live(self):
        """Worker thread: close the session, copy the released profile, start it.

        Goes through _restart_wpp_session(), so it inherits every gate that
        path already has — CLOSED, then the profile release, the restore
        ownership check, and _restarting_wpp_session, which keeps the health
        loop from starting a competing session and the disconnection from
        being announced as a real one.
        """
        from core import profile_recovery
        global_dir, session_name = self._live_snapshot_session()
        staged = {}
        queue = getattr(self, "message_queue", None)
        held = False
        try:
            if not _live_snapshot_policy(getattr(self, "settings", {}))[0]:
                return
            # Not the queue here: that is what the hold below is for, and
            # refusing would cost the whole interval for a message the user
            # sent while the question was on screen.
            reason = self._live_snapshot_blocked_reason(check_queue=False)
            if reason:
                # The situation changed while the question was on screen.
                logging.info("[profile-backup] not backing up after all: %s.", reason)
                self._postpone_live_snapshot()
                return
            if queue is not None:
                # Nothing may be attempted against the session this is about to
                # close. Held, the queue keeps every message — including one the
                # user writes during the backup — and sends it when the session
                # is back, instead of spending its retries against a session
                # that is deliberately gone. A send already on the wire is
                # waited for rather than cut off.
                queue.hold()
                held = True
                if not queue.wait_until_idle(self._LIVE_SNAPSHOT_DRAIN_SECONDS):
                    logging.info("[profile-backup] a message is still being sent "
                                 "— leaving the session alone this round.")
                    self._postpone_live_snapshot()
                    return

            def _copy_released_profile():
                # Announced here, not before the restart: a restart held back
                # by its own cooldown or re-entry guard disconnects nothing,
                # and must not be announced as a backup that then "failed".
                staged["announced"] = True
                wx.CallAfter(self.output, self.i18n.t("profile_backup_live_started"),
                             interrupt=False)
                # Staged, not in place: see _LIVE_SNAPSHOT_CONFIRM_SECONDS.
                staged["copy"] = profile_recovery.capture_snapshot(
                    global_dir, session_name,
                    budget=self._LIVE_SNAPSHOT_BUDGET_SECONDS, max_age=0,
                    cancel=self._live_snapshot_cancelled, stage_only=True)

            restarted = self._restart_wpp_session(on_profile_released=_copy_released_profile,
                                                  reason="profile backup")
            if held:
                # The session is starting again; queued messages go out as soon
                # as it reports connected, without waiting for the copy to be
                # confirmed below.
                queue.release()
                held = False
            if (restarted and staged.get("copy")
                    and self._live_snapshot_session_accepted()
                    and profile_recovery.promote_pending_snapshot(global_dir, session_name)):
                staged.clear()
                self._shutdown_audit("profile snapshot refreshed with WinZapp open")
                wx.CallAfter(self.output, self.i18n.t("profile_backup_live_done"), interrupt=False)
                return
            if not restarted and not staged.get("announced"):
                # The restart did not get far enough to release the profile
                # (its own cooldown or re-entry guard, or a session that never
                # reported CLOSED), so this round cost nothing and must not
                # cost the interval either. It cannot loop: the close stamps
                # _last_wpp_session_restart_ts, whose cooldown refuses the next
                # polls for free.
                self._postpone_live_snapshot()
            logging.warning("[profile-backup] the backup was not kept (restarted=%s, "
                            "copied=%s); the previous snapshot stays.",
                            restarted, bool(staged.get("copy")))
            if staged.get("announced"):
                wx.CallAfter(self.output, self.i18n.t("profile_backup_live_failed"),
                             interrupt=False)
        except Exception:
            logging.exception("[profile-backup] backup with WinZapp open failed")
        finally:
            if held:
                # Every path that leaves before the restart releases here: a
                # queue left held sends nothing for the rest of the launch, and
                # a raise here must not skip what follows either.
                try:
                    queue.release()
                except Exception:
                    logging.exception("[profile-backup] could not release the message queue")
            if staged.get("copy"):
                try:
                    profile_recovery.discard_pending_snapshot(global_dir, session_name)
                except Exception:
                    logging.exception("[profile-backup] could not discard the staged copy")
            self._live_snapshot_pending = False
