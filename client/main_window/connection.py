"""ConnectionMixin — part of MainWindow (see main_window/__init__.py).

Moved verbatim out of main.py. Methods run with ``self`` bound to the
MainWindow instance, so every attribute set in MainWindow.__init__ is
available here.
"""

import logging
import os
import requests
import subprocess
import sys
import threading
import time
import wx
from main_window.http_pool import _http_session
from core.api_client import (
    api_get,
    api_post,
    redact_credentials,
)
from main_window.identity_rules import record_linked_phone_if_unknown
from app_paths import resource_path


class ConnectionMixin:
    """WhatsApp connection state: suspend/resume, zombie-session restarts,
    _set_wa_connected, the WebSocket client, reachability probes and
    check_wa_connection_http.
    """

    def _on_power_suspended(self, event):
        logging.info("[power] System is suspending.")
        event.Skip()

    def _reset_connection_state_for_resume(self):
        """Reset the connection/logout latches so a wake-from-suspend is
        treated like a fresh session start, NOT like a mid-session drop.

        Thin wx-side wrapper over connection_state.reset_state_for_resume,
        which holds the pure (unit-testable) logic and the full rationale:
        across a suspend the ``_wa_connect_announced`` latch stayed True, so a
        transient post-wake QRCODE was misread as a logout and wiped the token
        of accounts that did not re-attach instantly.
        """
        import connection_state as cs
        cs.reset_state_for_resume(self, time.time())

    def _on_power_resume(self, event):
        """Force a reconnection check right after the system wakes up.

        Left alone, the app relied on the 30s health-check loop to
        eventually notice the socket/session died across the suspend —
        which observed live routinely never actually recovered, leaving
        the app permanently in offline mode until manually restarted from
        the tray. Resetting the connection state (see
        _reset_connection_state_for_resume) and re-probing immediately
        (rather than waiting up to 30s, plus however many strikes are now
        required) gives both the HTTP health check and the WebSocket a
        clean slate to reconnect against right away — crucially without a
        transient post-wake QRCODE being mistaken for a logout.
        """
        logging.info("[power] System resumed from suspend — forcing reconnection check.")
        self._reset_connection_state_for_resume()
        threading.Thread(target=self._recover_from_suspend, daemon=True).start()
        event.Skip()

    def _recover_from_suspend(self):
        # Single-flight: EVT_POWER_RESUME (a dedicated thread) and the
        # health-check clock-gap detector can both fire for the SAME wake, a few
        # seconds apart. Two overlapping recoveries interleave their
        # close/kill-orphan/start-session steps, and the later run's kill-orphan
        # sweep (chrome_cmdline_owns_session matches by session name only) kills
        # the fresh Chrome the earlier run's start-session just launched — the
        # session then hangs in INITIALIZING or bounces to QRCODE/unpaired.
        # Observed live after an 8h hibernation: same PIDs killed twice,
        # "start-session sent" logged twice, 1 of 3 accounts recovered cleanly.
        # If a recovery is already in progress, this trigger is redundant — drop
        # it rather than pile a second pass on top. try_begin_resume_recovery
        # holds the pure single-flight logic (connection_state, unit-tested).
        import connection_state as cs
        if not cs.try_begin_resume_recovery(self, self._resume_recovery_lock):
            logging.info("[power] recovery already in progress — "
                         "skipping duplicate wake trigger.")
            return
        try:
            if self.ws is not None and not getattr(self.ws.sio, "connected", False):
                self._reconnect_websocket_now()
            self.check_wa_connection_http()
            self.trigger_sync_if_needed()
            # After a wake, WhatsApp Web inside the (suspended) Chrome loses its
            # stream and does NOT rebuild it on its own, yet WPPConnect keeps a
            # stale "CONNECTED" status string — so check_wa_connection_http()
            # above lands in the CONNECTED branch, sees isConnected()==false,
            # flips the UI to offline and returns WITHOUT ever calling
            # start-session (that path is gated to CLOSED/DESTROYED only). The
            # session is a zombie: alive object, dead stream. Nothing recovers it
            # but a full app restart — which is exactly the symptom the user hit.
            # Actively restart the WhatsApp session once here to force Chrome to
            # reload web.whatsapp.com and rebuild the stream.
            self._restart_session_if_zombie_after_resume()
        except Exception:
            logging.exception("[power] _recover_from_suspend failed")
        finally:
            cs.end_resume_recovery(self, self._resume_recovery_lock)

    # Post-resume recovery timing. After the WAPI-race fix a plain INITIALIZING
    # right after wake is usually the session coming up on its own (onStateChange
    # promotes it to CONNECTED without our help), so we no longer force-restart on
    # a raw probe count. Instead we OBSERVE for up to _RESUME_OBSERVE_SECONDS,
    # polling every _RESUME_PROBE_INTERVAL, and only force a restart if the
    # session stays INITIALIZING with NO progress for _RESUME_INITIALIZING_GRACE
    # (GPT r2: time-without-progress, not attempt count). The zombie-CONNECTED
    # path still needs its own independent confirmation (dead stream, network up).
    _RESUME_PROBE_INTERVAL = 3.0
    _RESUME_OBSERVE_SECONDS = 150.0       # observation window (NOT a total recovery budget)
    _RESUME_INITIALIZING_GRACE = 90.0     # no-progress INITIALIZING before restart
    _RESUME_ZOMBIE_CONFIRM_SECONDS = 12.0  # CONNECTED-but-dead-stream must persist this long
    # MVP gate (GPT r5 #1): auto-restarting a stuck INITIALIZING session calls
    # close-session, which in WPPConnect force-kills (pkill -9, NO auth flush) any
    # non-CONNECTED session — risking a re-pair. Until the Node close path is made
    # graceful-first, we OBSERVE/log a stuck INITIALIZING but do NOT restart it.
    # The zombie-CONNECTED recovery stays ON: it only ever closes a genuinely
    # CONNECTED session, which takes WPPConnect's graceful client.close() (flush)
    # path, not the force-kill branch. Flip back to True once Node is graceful.
    _RESUME_RESTART_STUCK_INITIALIZING = False

    def _restart_session_if_zombie_after_resume(self):
        """Detect and repair a post-wake session that cannot recover on its own.

        Runs on a daemon thread (see _on_power_resume), never the wx GUI thread,
        so the polling sleeps here do not block the UI.

        Conservative by design (GPT r2/r3): the WAPI-race fix made status reliable
        enough that most post-wake INITIALIZING sessions now finish connecting by
        themselves via onStateChange. So we do NOT restart on a raw probe count.
        We observe for _RESUME_OBSERVE_SECONDS and act only on genuinely stuck
        cases, gated on the network actually being up:

        1. 'Stuck INITIALIZING' — MVP: OBSERVED and logged, but NOT auto-restarted
           (_RESUME_RESTART_STUCK_INITIALIZING=False). Restarting it would call
           WPPConnect's close-session, which force-kills a non-CONNECTED session
           without flushing auth and can re-pair the account. Re-enable once the
           Node close path is graceful-first (GPT r5 #1).
        2. 'Zombie CONNECTED' — status CONNECTED but the stream is dead
           (isConnected()==false) with the network up, PERSISTING for
           _RESUME_ZOMBIE_CONFIRM_SECONDS. This IS restarted: closing a genuinely
           CONNECTED session takes WPPConnect's graceful client.close() (flush)
           path, so it does not risk the force-kill re-pair.

        A genuinely healthy CONNECTED or a user-action state (QRCODE/UNPAIRED)
        ends observation immediately — we never loop restarts on a pairing screen
        nor bounce a working session.
        """
        if getattr(self, "_wa_connected", False):
            return  # recovered normally — nothing to do
        import connection_state as cs
        self._logged_stuck_initializing = False  # per-observation, so each wake logs once
        deadline = time.monotonic() + self._RESUME_OBSERVE_SECONDS
        prev_status = None
        initializing_since = None  # monotonic time the current INITIALIZING run began
        zombie_since = None        # monotonic time CONNECTED-but-dead-stream began
        while time.monotonic() < deadline:
            if getattr(self, "_wa_connected", False):
                return  # came up on its own — no restart needed
            status = self._raw_session_status()
            network_up = self._probe_whatsapp_host()  # probe once per iteration
            now = time.monotonic()

            # User-action state (QR/pairing) is handled by the pairing UI — stop,
            # never restart-loop. Checked FIRST (GPT r3 #1).
            if cs.recovery_needs_user_action(status):
                return

            if not network_up:
                # No internet: not a zombie, just an outage. The normal health
                # loop will reconnect once the network is back.
                logging.info("[power] post-resume offline but network is down — "
                             "leaving session alone to recover naturally.")
                return

            # CONNECTED path: distinguish a HEALTHY session (done) from a ZOMBIE
            # (CONNECTED object, dead stream). Only a zombie that PERSISTS for the
            # confirm window is restarted — not a single post-wake blip (GPT r3 #2).
            if cs.recovery_connected(status):
                if not cs.is_zombie_session_after_resume(
                    getattr(self, "_wa_connected", False), status, network_up,
                ):
                    return  # healthy CONNECTED — leave it alone
                if zombie_since is None:
                    zombie_since = now
                elif (now - zombie_since) >= self._RESUME_ZOMBIE_CONFIRM_SECONDS:
                    # Final re-check right before the destructive restart. Order
                    # matters (GPT r5 #4): do the slow network probe FIRST, then
                    # read a fresh status, then the _wa_connected flag last — so a
                    # stream that revived DURING the probe is not clobbered.
                    fresh_net = self._probe_whatsapp_host()
                    final_status = self._raw_session_status()
                    if getattr(self, "_wa_connected", False):
                        return
                    if not (fresh_net and cs.recovery_connected(final_status)
                            and cs.is_zombie_session_after_resume(
                                getattr(self, "_wa_connected", False), final_status,
                                fresh_net)):
                        # No longer a confirmed zombie — reset and keep observing.
                        zombie_since = None
                        prev_status = status
                        time.sleep(self._RESUME_PROBE_INTERVAL)
                        self.check_wa_connection_http()
                        continue
                    logging.warning("[power] Confirmed zombie session after resume "
                                    "(CONNECTED but stream dead >%.0fs, network up) — "
                                    "forcing restart to rebuild the stream.",
                                    self._RESUME_ZOMBIE_CONFIRM_SECONDS)
                    self._force_whatsapp_session_restart()
                    return
                initializing_since = None  # not initializing while (zombie) connected
            elif status == "INITIALIZING":
                zombie_since = None
                # Start the no-progress clock once; a readable non-INITIALIZING
                # status resets it below. Using "is None" (not prev_status) means
                # a transient unreadable '' between two INITIALIZING reads does
                # NOT restart the clock (GPT r4 #1).
                if initializing_since is None:
                    initializing_since = now
                if cs.initializing_restart_due(initializing_since, now,
                                               self._RESUME_INITIALIZING_GRACE):
                    if not self._RESUME_RESTART_STUCK_INITIALIZING:
                        # MVP: auto-restart of stuck INITIALIZING is gated OFF
                        # (would hit WPPConnect's force-kill-without-flush path
                        # and risk a re-pair). Log once and keep observing; the
                        # normal health loop / a manual restart handles it until
                        # the Node close path is graceful-first (GPT r5 #1).
                        if not getattr(self, "_logged_stuck_initializing", False):
                            self._logged_stuck_initializing = True
                            logging.warning("[power] Session stuck in INITIALIZING with no "
                                            "progress for %.0fs after resume — auto-restart "
                                            "is disabled (would risk re-pair via force-kill); "
                                            "leaving to health loop.",
                                            self._RESUME_INITIALIZING_GRACE)
                            self._shutdown_audit("WAKE stuck INITIALIZING — auto-restart gated OFF (MVP)")
                        # Do not spin the clock: keep observing without restart.
                        if status:
                            prev_status = status
                        time.sleep(self._RESUME_PROBE_INTERVAL)
                        self.check_wa_connection_http()
                        continue
                    # Re-check immediately before the destructive restart: only
                    # restart if it's STILL exactly INITIALIZING (GPT r4 #2).
                    if getattr(self, "_wa_connected", False):
                        return
                    if self._raw_session_status() != "INITIALIZING":
                        # Progress (or a user-action/closed state) appeared — let
                        # the loop re-evaluate on the next iteration instead.
                        prev_status = status
                        time.sleep(self._RESUME_PROBE_INTERVAL)
                        self.check_wa_connection_http()
                        continue
                    logging.warning("[power] Session stuck in INITIALIZING with no "
                                    "progress for %.0fs after resume (network up) — "
                                    "one force restart to clear a hung/detached browser.",
                                    self._RESUME_INITIALIZING_GRACE)
                    self._force_whatsapp_session_restart()
                    return
            else:
                # Any other readable, non-terminal status = progress; reset clocks.
                # An unreadable '' falls here too but must NOT count as progress,
                # so only reset when the status is genuinely readable (GPT r3 #8).
                zombie_since = None
                if cs.is_progress_status(status):
                    initializing_since = None

            # Do not let an unreadable '' overwrite the last real status we saw —
            # otherwise INITIALIZING -> '' -> INITIALIZING would look like a fresh
            # run (GPT r4 #1). Only remember readable statuses.
            if status:
                prev_status = status
            time.sleep(self._RESUME_PROBE_INTERVAL)
            self.check_wa_connection_http()
        logging.info("[power] post-resume observation window elapsed without a "
                     "definitive stuck/zombie signal — leaving session to the "
                     "normal health loop.")

    def _raw_session_status(self) -> str:
        """Return WPPConnect's current status-session string ('' on failure)."""
        try:
            resp = api_get(
                f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/status-session",
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=10,
            )
            if resp.status_code in (200, 201):
                return resp.json().get("status", "") or ""
        except Exception as e:
            logging.info("[_raw_session_status] probe failed: %s", e)
        return ""

    def _chrome_pids_owning_session(self, session_name: str):
        """PIDs of chrome.exe processes holding this session's profile.

        A list — possibly empty, which is an answer — or None when the
        process list could not be read at all, which is not.
        """
        import sys
        if sys.platform != "win32" or not session_name:
            return []
        import connection_state as cs
        no_window = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
        try:
            out = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process -Filter \"name='chrome.exe'\" | "
                 "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }"],
                creationflags=no_window, text=True, stderr=subprocess.DEVNULL, timeout=15,
            )
        except Exception as e:
            # None, not []. An empty list is an *answer* — "nothing holds this
            # profile" — and wait_for_profile_release() acts on it by letting
            # the taskkill proceed. A failed query knows nothing, and reading
            # it as the answer turns a slow or refused PowerShell spawn into
            # "Chrome released the profile", audited as such, immediately
            # before the tree kill that then hits a browser still writing.
            #
            # Measured on the reporting machine this query runs in 0.21 s idle
            # and 0.31 s under six competing PowerShell processes, with no
            # failures in 65 runs — so this is a latent fault, not the cause of
            # the losses that prompted the review. It is still the wrong
            # default, and its only trace today is a logging.info into log.log,
            # which the next launch truncates.
            logging.warning("[profile-lock] could not list chrome processes: %s", e)
            return None
        pids = []
        for line in out.splitlines():
            pid, _, cmdline = line.partition("\t")
            if pid.strip() and cs.chrome_cmdline_owns_session(cmdline, session_name):
                pids.append(pid.strip())
        return pids

    def _login_store_fingerprint(self, session_name: str = None) -> str:
        """Fingerprint of the store WhatsApp Web keeps its login in.

        Written into shutdown_audit.log at the end of a shutdown and again at
        the start of the next launch, because that file survives and log.log
        does not. Two clean shutdowns on the reporting install were followed by
        a launch that had already logged itself out, and two identical ones
        were fine — with nothing in the audit telling them apart. This does:
        a fingerprint that moved between the two lines means something wrote to
        the profile after WinZapp let go of it, and one that is identical means
        the profile WinZapp left is exactly the one WhatsApp Web rejected,
        which clears the shutdown path entirely.

        Never raises and never blocks — a diagnostic must not be able to cost
        a teardown.
        """
        try:
            from core import profile_recovery
            global_dir = getattr(self, "global_dir", None)
            name = session_name or (getattr(self, "token", "") or "").split(":")[0]
            if not global_dir or not name:
                return "unknown"
            return profile_recovery.login_store_fingerprint(global_dir, name) or "absent"
        except Exception:
            return "unknown"

    def wait_for_profile_release(self, session_name: str, timeout: float = 20.0) -> bool:
        import sys
        if sys.platform != "win32" or not session_name:
            return True
        deadline = time.monotonic() + timeout
        started = time.monotonic()
        polls = 0
        unreadable = 0
        while time.monotonic() < deadline:
            polls += 1
            holders = self._chrome_pids_owning_session(session_name)
            if holders is None:
                # Could not tell. Keep waiting rather than assuming the best:
                # the only thing after this gate is a /T kill of the process
                # tree Chrome is in.
                unreadable += 1
                time.sleep(0.5)
                continue
            if not holders:
                # Into the audit, not just log.log: log.log is truncated by the
                # launch that would report the damage, so "Chrome exited after
                # 6 s of real waiting" and "the very first poll said nothing
                # was there" are indistinguishable the morning after. They are
                # opposite diagnoses.
                self._shutdown_audit(
                    "profile released after %d poll(s) in %.1fs "
                    "(%d unreadable)" % (polls, time.monotonic() - started, unreadable))
                if polls > 1:
                    logging.info(
                        "[profile-lock] %s released after %d poll(s)",
                        session_name[:12], polls,
                    )
                return True
            time.sleep(0.5)
        logging.warning(
            "[profile-lock] %s still held after %.0fs — killing the holder(s)",
            session_name[:12], timeout,
        )
        self._kill_orphaned_chrome_for_session(session_name)
        grace_deadline = time.monotonic() + 5.0
        for _ in range(10):
            if self._chrome_pids_owning_session(session_name) == []:
                return True
            if time.monotonic() >= grace_deadline:
                break
            time.sleep(0.5)
        logging.warning(
            "[profile-lock] %s is STILL held after the kill — starting the "
            "session anyway; it will likely be refused.", session_name[:12],
        )
        return False

    def _kill_orphaned_chrome_for_session(self, session_name: str = None):
        """Kill a suspended chrome.exe still holding a session's userDataDir
        lock, then drop its stale lockfile, so WPPConnect can relaunch.

        After hibernation the WhatsApp Web chrome.exe is suspended, not killed:
        it keeps the lock on ./userDataDir/<session>, so the post-wake
        start-session fails with "The browser is already running" and the
        session is stuck in INITIALIZING forever (only a full app restart, which
        kills the orphan, cleared it — exactly the reported symptom). Matching is
        narrow (connection_state.chrome_cmdline_owns_session): only a chrome
        whose --user-data-dir carries THIS session name is touched — never the
        user's own Chrome, never another account's browser.
        """
        import sys
        if sys.platform != "win32":
            return
        import connection_state as cs
        session_name = session_name or (getattr(self, "token", "") or "").split(":")[0]
        if not session_name:
            return
        no_window = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
        killed = False
        try:
            # WMIC-free: PowerShell CIM gives us PID + full command line.
            out = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process -Filter \"name='chrome.exe'\" | "
                 "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }"],
                creationflags=no_window, text=True, stderr=subprocess.DEVNULL, timeout=15,
            )
        except Exception as e:
            logging.info("[wake-recover] could not list chrome processes: %s", e)
            out = ""
        for line in out.splitlines():
            pid, _, cmdline = line.partition("\t")
            if not cs.chrome_cmdline_owns_session(cmdline, session_name):
                continue
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", pid.strip()],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=no_window)
                logging.warning("[wake-recover] killed orphaned Chrome pid=%s holding "
                                "userDataDir lock for session %s", pid.strip(), session_name)
                killed = True
            except Exception as e:
                logging.info("[wake-recover] taskkill pid=%s failed: %s", pid.strip(), e)
        # Drop the stale lockfile so a relaunch is not refused even if the
        # process was already gone but left the file behind.
        try:
            # Matches _start_wpp_background()'s WINZAPP_USER_DATA_DIR, not
            # resource_path("api", "userDataDir") — the latter is a
            # --onefile launch's ephemeral extraction dir, gone by the time
            # this wake-recovery path runs.
            gd = getattr(self, "global_dir", None)
            udd = (os.path.join(gd, "api", "userDataDir", session_name) if gd
                   else resource_path("api", "userDataDir", session_name))
            for name in ("lockfile", "SingletonLock", "SingletonCookie", "SingletonSocket"):
                p = os.path.join(udd, name)
                if os.path.exists(p):
                    try:
                        os.remove(p)
                        logging.info("[wake-recover] removed stale %s", name)
                    except Exception:
                        pass
        except Exception:
            pass
        return killed

    # Wake-recovery restart policy. A hibernation-suspended Chrome, once resumed,
    # can hand puppeteer a dead page context ("Attempted to use detached Frame"
    # in the node log) — start-session then throws inside waitForLogin and the
    # session hangs. One clean close (wait for CLOSED) before re-starting gives a
    # fresh frame. GPT r2: prefer ONE restart + re-diagnose + cooldown over a
    # tight 3x loop, because a rapid close/start loop can itself generate locks
    # and detached frames. We allow a small number of attempts but SPACE them
    # with a cooldown and stop the moment the session connects or needs the user.
    # GPT r5 #2: for the MVP a SINGLE restart attempt. A second attempt would
    # fire only ~50s after the first (settle+cooldown), cutting the agreed 90s
    # no-progress grace short and possibly bouncing a session that was still
    # coming up. A further restart instead comes from a fresh 90s no-progress
    # window (or a hard Puppeteer error) on the next observation pass.
    _RECOVERY_MAX_ATTEMPTS = 1
    _RECOVERY_CLOSE_WAIT = 12.0      # wait for the browser to reach CLOSED
    _RECOVERY_SETTLE_TIMEOUT = 40.0  # wait for the restart to connect
    _RECOVERY_COOLDOWN = 10.0        # pause between attempts to let things settle
    _RECOVERY_POLL = 2.0

    def _wait_for_status(self, predicate, timeout: float,
                         stop_when_connected: bool = True) -> str:
        """Poll status-session until predicate(status) is True or timeout.
        Returns the last status string seen ('' on repeated failure).

        stop_when_connected: short-circuit as soon as _wa_connected flips True.
        Correct when WAITING FOR A CONNECTION, but WRONG when waiting for CLOSED
        (a still-connected flag would abort the close-wait and let kill/start run
        before the browser actually shut down — GPT r3 #5). Callers waiting for
        CLOSED must pass False.
        """
        deadline = time.monotonic() + timeout
        last = ""
        while time.monotonic() < deadline:
            last = self._raw_session_status()
            if predicate(last):
                return last
            if stop_when_connected and getattr(self, "_wa_connected", False):
                return last
            time.sleep(self._RECOVERY_POLL)
        return last

    def _force_whatsapp_session_restart(self):
        """Close then re-start the WhatsApp session so Chrome reloads
        web.whatsapp.com and rebuilds a dead stream. Safe on a live session:
        close-session persists state, and the token/paired flags are untouched
        (this is a stream refresh, NOT a logout).

        GPT r2: one restart, re-diagnose, cooldown, at most a couple of attempts.
        Success = a genuinely CONNECTED session; a user-action state (QRCODE/
        UNPAIRED) also STOPS the loop (the pairing UI takes over — never keep
        restarting a session that's waiting on the human).
        """
        token = getattr(self, "token", "")
        if not token:
            return
        import connection_state as cs
        # Mark the ACTIVE restart sequence so the health loop won't fire a
        # competing start-session while we own the close/kill/start cycle
        # (GPT r5 #3). Scoped tightly to this method — NOT the whole observation.
        self._recovery_restart_active = True
        try:
            self._run_recovery_attempts(token, cs)
        finally:
            self._recovery_restart_active = False

    def _run_recovery_attempts(self, token, cs):
        for attempt in range(1, self._RECOVERY_MAX_ATTEMPTS + 1):
            if getattr(self, "_profile_restore_in_flight", False):
                # A profile restore took the session over (it closes it first,
                # which is exactly the CLOSED this loop would otherwise answer
                # with a restart). Restarting now would start Chrome over a
                # profile being copied back; the restore hands the session to
                # the normal health loop when it finishes.
                logging.info("[power] recovery: a profile restore owns the "
                             "session — stopping before attempt %d.", attempt)
                return
            # Before each attempt re-check: the session may have connected or
            # dropped to a user-action state during the previous settle+cooldown
            # (GPT r4 #3). Never restart something that's already good/waiting.
            if attempt > 1:
                if getattr(self, "_wa_connected", False):
                    return
                pre = self._raw_session_status()
                if cs.recovery_connected(pre):
                    logging.info("[power] recovery: session CONNECTED before attempt %d "
                                 "— no further restart.", attempt)
                    return
                if cs.recovery_needs_user_action(pre):
                    logging.info("[power] recovery: session needs user action (%s) before "
                                 "attempt %d — stopping.", pre, attempt)
                    return
            self._restart_session_once(token, attempt)
            settled = self._wait_for_status(cs.recovery_should_stop,
                                            self._RECOVERY_SETTLE_TIMEOUT)
            if getattr(self, "_wa_connected", False) or cs.recovery_connected(settled):
                logging.info("[power] recovery attempt %d CONNECTED (status=%s)",
                             attempt, settled or "?")
                self._shutdown_audit(f"WAKE recovery attempt {attempt} CONNECTED status={settled or '?'}")
                return
            if cs.recovery_needs_user_action(settled):
                logging.info("[power] recovery attempt %d -> user action needed "
                             "(status=%s) — stopping, pairing UI takes over.",
                             attempt, settled or "?")
                self._shutdown_audit(f"WAKE recovery attempt {attempt} USER-ACTION status={settled or '?'}")
                return
            logging.warning("[power] recovery attempt %d did not connect "
                            "(status=%s) — %s", attempt, settled or "?",
                            "cooldown then retry" if attempt < self._RECOVERY_MAX_ATTEMPTS
                            else "giving up to normal health loop")
            self._shutdown_audit(f"WAKE recovery attempt {attempt} NOT CONNECTED status={settled or '?'}")
            if attempt < self._RECOVERY_MAX_ATTEMPTS:
                time.sleep(self._RECOVERY_COOLDOWN)
        logging.warning("[power] recovery exhausted %d attempts — leaving session "
                        "to the normal health loop.", self._RECOVERY_MAX_ATTEMPTS)
        self._shutdown_audit(f"WAKE recovery EXHAUSTED {self._RECOVERY_MAX_ATTEMPTS} attempts")

    def _restart_session_once(self, token: str, attempt: int = 1):
        """One close -> wait-for-CLOSED -> kill-orphan -> start-session cycle.
        Waiting for CLOSED (not a blind sleep) is what makes the re-start get a
        fresh browser frame instead of reusing a hibernation-detached one.

        NOTE: close-session in WPPConnect force-kills (no auth flush) any session
        whose status is not CONNECTED/open, so a restart from a non-CONNECTED
        state can still lose auth until the WPPConnect close path is made
        graceful-first. The conservative trigger upstream keeps us off this path
        in the common case; a full fix belongs in the Node close handler.
        """
        headers = {"Authorization": f"Bearer {token}"}
        close_ok = False
        try:
            resp = api_post(
                f"{self.wpp_server}:{self.wpp_port}/api/{token}/close-session",
                headers=headers, timeout=10,
            )
            close_ok = resp.status_code in (200, 201)
            if close_ok:
                logging.info("[power] zombie recovery: close-session sent (attempt %d)", attempt)
            else:
                logging.warning("[power] zombie recovery: close-session HTTP %s (attempt %d)",
                                resp.status_code, attempt)
        except Exception as e:
            logging.warning("[power] zombie recovery: close-session failed: %s", e)
        # Wait for the browser to actually tear down (CLOSED) before re-starting,
        # so puppeteer gets a fresh page frame — reusing a half-closed,
        # hibernation-resumed browser is what throws "detached Frame" and hangs
        # in INITIALIZING. Do NOT short-circuit on _wa_connected here: we are
        # waiting for the browser to go DOWN, so a stale connected flag must not
        # abort the wait (GPT r3 #5). Fall back to a short sleep if the
        # close-session request itself failed and CLOSED never confirms.
        import connection_state as cs
        closed = self._wait_for_status(cs.session_closed_after_flush,
                                       self._RECOVERY_CLOSE_WAIT,
                                       stop_when_connected=False)
        confirmed_closed = cs.session_closed_after_flush(closed)
        # Only proceed to the destructive kill/start if the browser is genuinely
        # down: either close-session was accepted OR the status actually reached
        # CLOSED despite an endpoint error. Bailing here avoids killing/starting
        # on top of a still-live browser after a failed close (GPT r4 #4).
        if not close_ok and not confirmed_closed:
            logging.warning("[power] recovery: close-session not accepted (HTTP/err) "
                            "and status not CLOSED (status=%s) — skipping kill/start "
                            "this attempt.", closed or "?")
            return False
        if not confirmed_closed:
            logging.info("[power] recovery: browser did not confirm CLOSED "
                         "(status=%s) — proceeding after close was accepted", closed or "?")
            time.sleep(2)
        # A hibernation-suspended chrome.exe may still hold the userDataDir lock,
        # which makes the start-session below fail with "browser is already
        # running" and hangs the session in INITIALIZING forever. Clear that
        # orphan before starting — but through wait_for_profile_release(),
        # which is "wait for it to let go, and kill only if it never does".
        #
        # The bare kill this replaces ran unconditionally, moments after a
        # close-session that had usually already worked: a SIGKILL delivered to
        # a Chrome that was in the middle of flushing WhatsApp Web's IndexedDB,
        # for no gain, on every wake. That database is the only carrier of the
        # login, and the losses it produces do not look like corruption — the
        # profile comes back structurally perfect and simply stops being
        # accepted. See closeBrowserGracefully() in createSessionUtil.ts.
        self.wait_for_profile_release(
            (getattr(self, "token", "") or "").split(":")[0], timeout=10.0)
        if getattr(self, "_profile_restore_in_flight", False):
            # A profile restore began during the close or the release wait
            # above; starting now would open Chrome over the copy. The restore
            # hands the session back to the health loop when it is done.
            logging.info("[power] zombie recovery: a profile restore took the "
                         "session over — not starting it (attempt %d).", attempt)
            return
        try:
            api_post(
                f"{self.wpp_server}:{self.wpp_port}/api/{token}/start-session",
                json={"waitQrCode": False}, headers=headers, timeout=15,
            )
            logging.info("[power] zombie recovery: start-session sent — "
                         "WhatsApp Web reloading (attempt %d)", attempt)
        except Exception as e:
            logging.warning("[power] zombie recovery: start-session failed: %s", e)

    # How long the very first connection attempt of a session gets before the
    # UI is allowed to declare "offline". WPPConnect/Chrome routinely take
    # several seconds to finish booting, during which every status probe looks
    # identical to a real outage (connection refused, CLOSED/INITIALIZING
    # status, etc.) — without this grace window the app announced itself
    # offline within the first second or two of every single launch.
    #
    # A companion cap on the *number* of not-yet-connected readings used to sit
    # alongside this and was removed: see _set_wa_connected(), where callers
    # polling at different rates made a reading count mean different amounts of
    # time and silently shrank this window to about a second.
    #
    # The window can stay generous because it is no longer the only thing
    # separating "still booting" from "no internet" — see
    # _startup_offline_confirmed(), which ends it early once the machine is
    # shown to have no network at all.
    _WA_STARTUP_GRACE_SECONDS = 45

    # How often the network itself may be probed while the startup grace is
    # open, and how many consecutive failures confirm the machine really has no
    # connectivity. Two, because a single failure a second after login is just
    # as likely to be DNS not being warm yet as a genuine outage.
    _STARTUP_PROBE_INTERVAL = 3.0
    _STARTUP_PROBE_STRIKES = 2

    def _startup_offline_confirmed(self) -> bool:
        """True once the machine has been shown to have no internet at all
        while the startup grace is still open.

        The grace exists because a booting WPPConnect looks exactly like an
        outage from the status endpoint, so the app must not shout "offline"
        over what is really a slow launch. But the reverse case — the user
        genuinely opened WinZapp with no connection — then waits out the whole
        window before being told anything, and a screen-reader user staring at
        "conectando" for 45 seconds is badly served by that.

        Those two cases are not actually ambiguous: they only look alike
        through WPPConnect's status string. Asking the network directly tells
        them apart, so the grace can stay long for a slow boot and end almost
        immediately when there is nothing to connect to.

        Never blocks the caller. _set_wa_connected() runs on whatever thread
        happened to poll — including the 0.2 s wait loop in _run_sync() — so
        the probe is fired into the background and only its cached verdict is
        read here; the next poll picks the answer up. Rate-limited so that loop
        cannot spawn one probe every 200 ms.
        """
        if getattr(self, "_startup_probe_offline", False):
            return True
        now = time.time()
        if getattr(self, "_startup_probe_running", False):
            return False
        if now - getattr(self, "_startup_probe_at", 0.0) < self._STARTUP_PROBE_INTERVAL:
            return False
        self._startup_probe_at = now
        self._startup_probe_running = True

        def _probe():
            try:
                if self._probe_whatsapp_host():
                    self._startup_probe_fails = 0
                    return
                fails = getattr(self, "_startup_probe_fails", 0) + 1
                self._startup_probe_fails = fails
                if fails >= self._STARTUP_PROBE_STRIKES:
                    self._startup_probe_offline = True
                    logging.warning(
                        "[connection] No route to WhatsApp on %d consecutive probes "
                        "— ending the startup grace early.", fails)
            except Exception:
                logging.exception("[connection] startup network probe failed")
            finally:
                self._startup_probe_running = False

        threading.Thread(target=_probe, daemon=True, name="startup-net-probe").start()
        return False

    def _reset_startup_probe(self) -> None:
        """Forget the startup network verdict, so a later grace window (a
        re-pair, a reconnect) starts from a clean slate instead of inheriting
        a stale "this machine is offline"."""
        self._startup_probe_offline = False
        self._startup_probe_fails = 0
        self._startup_probe_at = 0.0

    def _set_wa_connected(self, connected: bool, reason: str = "", announce: bool = True,
                           confirmed: bool = False):
        """Single entry point for every WhatsApp connection-state transition.

        Keeps three things in lockstep that used to drift apart: the
        ``_wa_connected`` flag the MessageQueue and the sync gate read, the
        automatic offline mode, and the "connected" sound/announcement — which
        previously fired on startup just because the *local* WPPConnect API had
        answered, even with no internet at all.

        ``confirmed`` marks a *definite* negative signal (WhatsApp itself says
        the device is logged out / needs pairing) — those skip the startup
        grace window below and go straight to the offline UI, same as before.
        Everything else (network hiccups, the local API not answering yet,
        WPPConnect still initializing) is ambiguous during the first
        connection attempt of a session and gets the grace window instead.
        """
        if bool(getattr(self, "_shutting_down", False)):
            # We just closed this session ourselves as part of quitting —
            # looks identical, at this layer, to WhatsApp dropping the
            # connection. The process exits moments later regardless, so
            # nothing here needs to touch offline state, sound, or speech.
            # NOT the same as the branch below: an update/self-restart still
            # reconnects afterward, so those genuinely engage offline mode
            # and only suppress the announcement.
            return
        connected = bool(connected)
        was = bool(getattr(self, "_wa_connected", False))
        self._wa_connected = connected
        if not connected and getattr(self, "_active_voice_call", None):
            # A call cannot outlive its signaling. No terminal `callstate` can
            # reach us over a dead socket, so without this the call window
            # stayed up and CallAudioSession went on reading the microphone
            # until the app was restarted.
            logging.info("[call] ending the active call: WhatsApp went offline")
            try:
                self._stop_voice_call_audio()
            except Exception:
                logging.exception("[call] could not stop call audio while going offline")
        # Nothing to do only when the flag *and* the derived offline state are
        # both already consistent with `connected`. _shutting_down already
        # returned above, so the check below can only mean still-updating or
        # still-self-restarting.
        if connected == was and self._auto_offline == (not connected):
            still_owed = (
                not connected
                and not self._self_inflicted_teardown_expected()
                and getattr(self, "_offline_announce_deferred", False)
            )
            if not still_owed:
                return

        if connected:
            if not self.token:
                # _on_disconnect() (Arquivo > Desconectar) clears self.token
                # and self._wa_connected together, but a check_wa_connection_
                # http()/pairing request already in flight at that moment
                # can still land afterwards still reporting CONNECTED — it
                # was issued against the session that just got disconnected,
                # before the server-side close-session even ran. Without
                # this guard that stale report looked exactly like a real
                # offline→online transition (was=False after the reset
                # above, connected=True now) and replayed the whole "just
                # reconnected" sequence — sound, forced WebSocket reconnect,
                # trigger_sync_if_needed() — seconds after the user
                # explicitly asked to disconnect, against a session with no
                # token left to use (measured live: the forced resync then
                # 404'd on /api//list-chats, the double slash being the
                # empty token). An empty self.token means there is no
                # session to be validly "connected" to, full stop.
                logging.info(
                    "[connection] Ignoring a stale 'connected' report (%s) — "
                    "no session token (disconnected intentionally).",
                    reason or "checked",
                )
                return
            # True exactly when the connection just came back from being down
            # (auto-offline or the app never having connected this session) —
            # NOT when this call merely re-confirms an already-known-good
            # connection (health check ticks fire this constantly while
            # online; only a real transition should force anything below).
            was_offline = not was
            self._wa_offline_strikes = 0
            self._reset_startup_probe()
            self._dead_browser_strikes = 0
            self._auto_repair_dialog_shown = False
            self._unattended_qr_events = 0
            self._qr_flood_halted = False
            self._auto_offline = False
            self._offline_announce_deferred = False
            # Re-arm profile recovery in the same event that zeroes the QR
            # counter above, rather than on the bare status string
            # _note_status_for_profile_health() used to read for this earlier
            # in the same poll. createSessionUtil.start() can promote a session to
            # CONNECTED on its own state listener before isConnected() ever
            # agrees ("the event wins") — reading the string there re-armed
            # recovery on a CONNECTED the probe was about to refuse, without
            # resetting the counter this method only just zeroed above. A
            # second recovery could then start mid-flood, on an event the
            # counter had already counted, and the caller (on_qrcode_update())
            # returns as soon as recovery starts — landing exactly on the
            # event where seen == _UNATTENDED_QR_LIMIT stopped the halt from
            # ever being evaluated for the rest of that flood, since seen only
            # grows from there (issue #202; the halt now reads `>=` and its
            # latch, issue #203, which closes that class of seam on its own
            # side too). Living here instead means both
            # only ever happen together, on the one event this method already
            # treats as the real online transition.
            # Wrapped, and not defensively: its old home was inside
            # _note_status_for_profile_health()'s blanket handler AND
            # check_wa_connection_http()'s own wrapper, whose comment states
            # the invariant — a bug in a diagnostic must never change the
            # connection verdict. Here it would: this runs inside the same
            # try: whose handler ends in _set_wa_connected(False, ...), so a
            # raise would flip a healthy connection to offline AND abandon
            # the rest of this branch (offline state, connected sound,
            # _wa_connect_announced, trigger_sync_if_needed) halfway. The
            # invariant was structural before the move; this keeps it.
            #
            # Only the recovery budget is re-armed here. The generation
            # ladder stays on the status-string reading in
            # _note_status_for_profile_health() — see the comment there for
            # what it would cost to move it.
            try:
                if getattr(self, "_profile_recovery_attempted", False):
                    # A restore that reached here has proved the snapshot
                    # good, so the once-per-launch budget it spent is earned
                    # back — see _recover_suspect_profile()'s own docstring
                    # for the measured case this relaxes (11 s between a
                    # restored profile connecting and a superseded session
                    # start force-killing its browser).
                    logging.info("[profile-recovery] the restored profile "
                                 "connected — allowing another recovery if it "
                                 "breaks again this launch.")
                    self._profile_recovery_attempted = False
            except Exception:
                logging.exception("[profile-recovery] re-arm failed (non-fatal)")
            try:
                # Whatever WhatsApp was refusing is over: a state that
                # authenticates now must not go on being refused by a verdict
                # recorded before it did. Clearing on CONNECTED — rather than
                # ageing the entries out — keeps the record meaning exactly
                # "states this account has been logged out of since it last
                # worked", which is the only question it is consulted for.
                from core import profile_recovery as _pr
                gd = getattr(self, "global_dir", None)
                name = (getattr(self, "token", "") or "").split(":")[0]
                if gd and name:
                    _pr.clear_rejected_profiles(gd, name)
            except Exception:
                logging.exception("[profile-recovery] clearing the rejected "
                                  "states failed (non-fatal)")
            self._apply_offline_state()
            logging.info("[connection] WhatsApp connection is up (%s)", reason or "checked")
            # Earliest moment /send-capabilities can answer anything: the
            # route sits behind statusConnection, which 404s "Disconnected"
            # until the session is attached. On its own thread because this
            # method also runs on the message-queue worker.
            #
            # Gated on its own latch rather than on first_ever_connect, which
            # gave the probe exactly one attempt per process: CONNECTED can be
            # promoted by the state listener without isConnected() ever having
            # succeeded (createSessionUtil.start()'s "The event wins"), and in
            # that state the probe queues behind the same statusConnection
            # probe, gives up unanswered and logs "unavailable" — silencing the
            # incompatibility warning for the rest of the session, in the very
            # release whose point is that the runtime changed.
            # _check_send_capabilities() retries an unavailable answer on
            # its own thread first (this latch is only re-read on a real
            # offline→online transition, and the session that case describes
            # may never oscillate again) and clears the latch only once that
            # budget is spent; _send_capabilities_warning still dedupes the
            # announcement, so a later attempt cannot speak twice.
            #
            # Read and written without a lock, deliberately and in the same
            # shape as _wa_connect_announced right below it: this method runs
            # both on the wx thread and on the MessageQueue worker, and the
            # worst a lost race can cost is one extra probe and a duplicate log
            # line — the dedupe above already owns what the user hears.
            if not self._send_capabilities_checked:
                self._send_capabilities_checked = True
                threading.Thread(
                    target=self._check_send_capabilities,
                    daemon=True,
                    name="wpp-send-capabilities",
                ).start()
            first_ever_connect = not self._wa_connect_announced
            if first_ever_connect:
                self._wa_connect_announced = True
                if not self.background_mode:
                    self.connected_sound.play()
            elif announce and not self.background_mode:
                self.output(self.i18n.t("connection_restored"), interrupt=False)
            if first_ever_connect and not self.background_mode:
                # The "Conectando..."/tray_connecting title must hold until
                # this exact moment — the sound firing IS the signal that the
                # connection is real, so the status advancing any earlier (as
                # it briefly did: prepare_sync() set "preparing_to_sync"
                # synchronously during __init__, well before the connection
                # was actually confirmed and this branch got to run) made the
                # title claim progress the app hadn't made yet.
                # wait_messages_set() no longer sets this status itself —
                # this is the one place that does, in lockstep with the sound.
                wx.CallAfter(self._set_preparing_status_if_idle)
            elif self._tray_status in (self.i18n.t("tray_wa_disconnected"), self.i18n.t("tray_connecting")):
                # Reconnect (not the first-ever connect, handled above) —
                # clear only the transient "connecting"/"disconnected" text; a
                # sync running in parallel owns the status line otherwise
                # ("sincronizando", "baixando mídias").
                wx.CallAfter(self._set_status, "")
            self._sync_retry_count = 0
            if was_offline:
                # Every offline→online transition forces a fresh sync, not
                # just the first one. trigger_sync_if_needed() alone only
                # acts while _sync_completed is False — once a session has
                # synced successfully and then loses connectivity for a
                # while, that flag stays True, so reconnecting silently did
                # nothing: messages.upsert events that WhatsApp never had a
                # live channel to deliver over were never picked up any other
                # way, and the conversation just quietly stayed stale until
                # the user noticed and pressed F5.
                self._sync_completed = False
                # Also clear the retry cooldown timestamp — without this, a
                # sync attempt made shortly before the connection dropped
                # could still be inside its own backoff window and silently
                # swallow this forced resync for up to another 10 minutes.
                self._last_sync_attempt_ts = 0
                # The WebSocket client auto-reconnects on its own (unlimited
                # retries), but its backoff can be up to 60s between
                # attempts — after a longer outage that means "online" could
                # sit for the better part of a minute with the live message
                # channel still down. Nudge it explicitly instead of waiting.
                if self.ws is not None and not getattr(self.ws.sio, "connected", False):
                    threading.Thread(target=self._reconnect_websocket_now, daemon=True).start()
            self.trigger_sync_if_needed()
            return

        self._wa_offline_strikes += 1

        if self._self_inflicted_teardown_expected():
            logging.info(
                "[connection] Self-inflicted session teardown in progress "
                "(%s) — engaging offline mode and showing 'connecting' "
                "instead of 'offline'.",
                reason or "checked",
            )
            self._auto_offline = True
            self._offline_announce_deferred = True
            self._apply_offline_state()
            wx.CallAfter(self._set_status, self.i18n.t("tray_connecting"))
            return

        if not confirmed:
            never_connected_yet = not self._wa_connect_announced
            # Bounded by elapsed time alone, deliberately. This used to also
            # cap the number of not-yet-connected readings, and the two bounds
            # were measured in incompatible units: the window was sized for the
            # health checker's 30 s cadence (6 readings would span 3 minutes,
            # so the 45 s clock always ran out first), but _run_sync() polls
            # check_wa_connection_http() every 0.2 s while waiting for the
            # connection. Under that loop the same 6 readings were spent in
            # 1.2 s, collapsing a 45-second grace into barely one second.
            # Observed live: six "INITIALIZING" readings 265 ms apart, then a
            # full "modo offline" announcement with sound and speech — and the
            # session reported itself connected 54 ms later. A reading count is
            # a proxy for elapsed time that breaks the moment anything polls at
            # a different rate; the clock is the thing actually meant here.
            within_grace = (
                never_connected_yet
                and (time.time() - self._wa_startup_time) < self._WA_STARTUP_GRACE_SECONDS
            )
            if within_grace and self._startup_offline_confirmed():
                # Not a slow boot — there is no network to boot onto. Say so
                # now instead of holding "conectando" for the rest of the
                # window.
                logging.warning(
                    "[connection] Startup grace cut short (%s): the machine cannot "
                    "reach WhatsApp at all.", reason or "checked")
                within_grace = False
            if within_grace:
                logging.info(
                    "[connection] Not connected yet during startup grace "
                    "(%s, strike %d) — showing 'connecting' instead of 'offline'.",
                    reason or "checked", self._wa_offline_strikes,
                )
                wx.CallAfter(self._set_status, self.i18n.t("tray_connecting"))
                return

        self._auto_offline = True
        self._offline_announce_deferred = False
        self._apply_offline_state()
        logging.warning("[connection] WhatsApp connection is down (%s)", reason or "checked")
        wx.CallAfter(self._set_status, self.i18n.t("tray_wa_disconnected"))
        # Sound + speech on every genuine transition INTO offline — including
        # the very first one, e.g. starting the app with no internet at all.
        # This used to require `was` (a prior *confirmed* online connection)
        # before announcing anything, so the common "opened with no internet"
        # case went auto-offline in total silence: no sound, no speech in any
        # of the four languages, nothing telling the user why. The early-return
        # guard above (`connected == was and _auto_offline == (not connected)`)
        # already keeps this from repeating on every failed health-check retry
        # — it only reaches here on an actual state change — so this is safe
        # to fire unconditionally, matching the manual toggle's own behaviour
        # (offline_mode_sound + speech) exactly.
        if announce and not self.background_mode and self._announce_sync_events_enabled():
            self.offline_mode_sound.play()
            self.output(self.i18n.t("offline_mode_auto_enabled"), interrupt=False)

    def connect_websocket(self):
        """Connect to the WPPConnect Server WebSocket.

        Connects to both the session namespace and root namespace so that
        global events (qrCode, phoneCode, session-logged) are received.
        Retries up to 6 times with a 2-second delay to handle the brief
        window after session creation where the namespace isn't ready yet.
        """
        import time
        max_attempts = 6
        delay = 2
        last_exc = None
        for attempt in range(1, max_attempts + 1):
            try:
                logging.info("connect_websocket: Attempting connection %d/%d...", attempt, max_attempts)
                if self.ws.sio.connected:
                    self.ws.sio.disconnect()
                # WPPConnect Server only uses the root Socket.IO namespace.
                # All events (qrCode, phoneCode, received-message, etc.) are
                # emitted via req.io.emit() on root "/".
                self.ws.sio.connect(
                    f"{self.wpp_ws_server}:{self.wpp_port}/",
                    socketio_path="socket.io",
                    headers={"apikey": self.token},
                    auth={
                        "token": self.token,
                        "session": self.token.split(":", 1)[0],
                    },
                    namespaces=["/"],
                    transports=["websocket"],
                )
                logging.info("connect_websocket: Connected successfully on attempt %d.", attempt)
                return
            except Exception as exc:
                logging.warning("connect_websocket: Attempt %d failed: %s", attempt, exc)
                last_exc = exc
                if attempt < max_attempts:
                    time.sleep(delay)
        raise last_exc

    def _reconnect_websocket_now(self):
        """Force the Socket.IO client to reconnect right away.

        python-socketio already retries forever on its own (reconnection=True,
        reconnection_attempts=0), but its backoff grows up to 60s between
        tries — after an outage long enough to trip auto-offline, "online"
        could otherwise sit for the better part of a minute with the live
        message channel still down. Called on a background thread (blocks on
        the actual handshake) whenever an offline→online transition finds the
        socket not connected.
        """
        try:
            if self.ws is None:
                return
            if getattr(self.ws.sio, "connected", False):
                return
            logging.info("[connection] Forcing WebSocket reconnect after coming back online...")
            self.connect_websocket()
        except Exception as exc:
            logging.warning("[connection] Forced WebSocket reconnect failed (will keep retrying on its own): %s", exc)

    def run_on_main_thread(self, func, *args, **kwargs):
        """
        Execute a callable on the wx main thread using wx.CallAfter if invoked
        from a background thread, blocking until the callable finishes and returning its result.
        If called from the main thread, execute directly.
        """
        if wx.IsMainThread():
            return func(*args, **kwargs)

        result_container = []
        exception_container = []
        event = threading.Event()

        def _wrapper():
            try:
                res = func(*args, **kwargs)
                result_container.append(res)
            except Exception as exc:
                exception_container.append(exc)
            finally:
                event.set()

        wx.CallAfter(_wrapper)
        event.wait()

        if exception_container:
            raise exception_container[0]
        return result_container[0]

    def _probe_whatsapp_host(self) -> bool:
        """True if WhatsApp's own servers answer over the network.

        Any HTTP answer counts as reachable — we only care about whether the
        machine can talk to WhatsApp at all, not what it replies.  Only a
        transport-level failure (no DNS, no route, timeout) means offline.
        No third-party host is contacted: this is the same service the app
        already talks to through the browser session.
        """
        try:
            # Reuses the module-level pooled session (requests.head is not one
            # of the patched, pooled helpers).
            _http_session.head("https://web.whatsapp.com", timeout=6,
                               allow_redirects=False)
            return True
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            logging.info("[_probe_whatsapp_host] network unreachable: %s", e)
            return False
        except Exception:
            # Anything else (odd TLS/proxy behaviour) still proves we reached
            # something — do not call that an outage.
            return True

    def _nudge_whatsapp_socket_stream(self) -> bool:
        """Ask WPPConnect to fire WPP.whatsapp.Cmd.openSocketStream() inside
        the page — the same internal trigger a real, focused browser tab
        fires on its own via visibility/focus/online DOM events after the OS
        resumes from sleep.

        This session's Chrome runs headless and is never focused, so nothing
        ever fires that trigger by itself. That alone was the first symptom
        reported: stuck offline forever after a suspend/resume cycle, even
        though WPPConnect's own cached session status keeps saying CONNECTED.

        Returns False when the request itself fails (network) or the server
        reports a non-2xx status — which turned out to mean something worse
        than "no trigger fired": WPPConnect's log showed
        ``Attempted to use detached Frame '<id>'`` from Puppeteer every time
        this endpoint was hit after a suspend/resume cycle. The Chrome tab's
        page/frame had structurally died (crashed or been replaced) during
        sleep, and Puppeteer's cached Page/Frame handle for it is now
        permanently unusable — no in-page command can ever succeed again on
        it, by definition. See _restart_wpp_session(), which the caller
        escalates to after enough consecutive failures here.
        """
        try:
            url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/reconnect-socket-stream"
            resp = api_post(
                url,
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=10,
            )
            if resp.status_code not in (200, 201):
                logging.warning(
                    "[_nudge_whatsapp_socket_stream] HTTP %s: %s",
                    resp.status_code, resp.text[:300],
                )
                return False
            return True
        except Exception as exc:
            logging.warning("[_nudge_whatsapp_socket_stream] request failed: %s", exc)
            return False

    # How many *consecutive* failed nudges (see _nudge_whatsapp_socket_stream)
    # before assuming the browser page itself is structurally dead — rather
    # than a transient blip — and restarting the WPPConnect session.
    _DEAD_BROWSER_RESTART_STRIKES = 3
    # Minimum time between automatic session restarts, so a persistently
    # broken state can't retrigger this every health-check cycle forever.
    _WPP_SESSION_RESTART_COOLDOWN = 120

    # How long after an automatic _restart_wpp_session() the "confirmed
    # logout" path (below, in check_wa_connection_http) must stay suppressed.
    # See _restart_wpp_session()'s docstring for why this exists at all.
    _AUTO_RESTART_LOGOUT_GRACE_SECONDS = 600

    def _auto_restart_grace_active(self) -> bool:
        """True while an automatic session restart is still settling — see
        _AUTO_RESTART_LOGOUT_GRACE_SECONDS."""
        ts = getattr(self, "_auto_session_restart_ts", 0)
        return bool(ts) and (time.time() - ts) < self._AUTO_RESTART_LOGOUT_GRACE_SECONDS

    #: How long to wait for Chrome to release userDataDir between the close
    #: and the restart. Sized like _stop_wpp_server()'s own wait rather than
    #: the 12 s close wait beside it: the measured worst case on this path was
    #: a browser resumed from a long suspend, which took 20 s to let go.
    _RESTART_PROFILE_RELEASE_WAIT = 25.0

    def _restart_wpp_session(self, on_profile_released=None, reason=None):
        """Recreate the WPPConnect Chrome session in place (close-session +
        start-session), without touching the Node process or WinZapp itself.

        `on_profile_released`, when given, runs between the close and the
        start — only once the session reached CLOSED AND Chrome released the
        profile, the two gates a restore point needs (see
        _refresh_profile_snapshot_live()). If Chrome never lets go it is not
        run, and the session starts again either way. `reason` is only logged.

        Returns True once start-session was requested, False when it bailed
        out before that (cooldown, re-entry, no CLOSED, a restore or a
        shutdown taking over). Existing callers ignore it.

        Used automatically when the Puppeteer page has structurally died
        (detached frame after a suspend/resume cycle — see
        _nudge_whatsapp_socket_stream()); a dead page can never satisfy
        isConnected() again on its own, no matter how many times the health
        check retries it. start-session on a session with a stored, valid
        token silently restores the existing WhatsApp session (no new QR
        code) — exactly what already happens on every normal app restart,
        just without restarting the whole app or Node process.

        Reported live the first time this was wired up unguarded: the
        stored token had already gone bad for an unrelated reason (possibly
        the very same crash that killed the page), so start-session's
        create() came back needing a fresh QR scan instead — with nobody
        there to scan it. The pre-existing (and, on its own, entirely
        correct) "confirmed logout" detection further down
        check_wa_connection_http() then saw that QRCODE state hold for its
        normal confirm-strikes threshold and treated it exactly like a real
        phone-side unlink, calling _on_disconnect() — which wipes the
        *entire* local database via clear_local_data(). _auto_session_restart_ts
        (set below, checked via _auto_restart_grace_active()) is what now
        keeps that destructive path from firing as a side effect of this
        one: for _AUTO_RESTART_LOGOUT_GRACE_SECONDS after a restart, a
        QRCODE/notLogged reading is treated as "still settling", not "phone
        confirmed unlinked". A genuine phone-side unlink happening to
        coincide with that window is only delayed, not missed — trading a
        possible few extra minutes before a real unlink is detected for
        never again wiping local data over an artifact of our own restart.
        """
        # Sets the grace window immediately, synchronously, before the
        # cooldown/re-entrancy checks below can bail out early — a health
        # check landing on another thread between "decided to restart" and
        # this function's body actually running must not see the old,
        # expired window and treat a QRCODE reading as confirmable.
        self._auto_session_restart_ts = time.time()
        if getattr(self, "_restarting_wpp_session", False):
            return False
        now = time.time()
        last = getattr(self, "_last_wpp_session_restart_ts", 0)
        if now - last < self._WPP_SESSION_RESTART_COOLDOWN:
            return False
        # Deliberately does NOT also set _recovery_restart_active, even though
        # that is the flag check_wa_connection_http()'s CLOSED branch reads
        # first: _force_whatsapp_session_restart() owns that one for the whole
        # of _run_recovery_attempts(), and neither of this method's two call
        # sites (the dead-browser strike escalation, and start_sync's store
        # rebuild) checks whether a recovery is already running. Setting it
        # here means the finally below clears a flag this method never owned,
        # reopening the competing-start-session window mid-recovery -- and,
        # since _self_inflicted_teardown_expected() reads it, letting the
        # recovery's OWN close-session be handled as a real phone-side unlink.
        # Nothing is lost: _restarting_wpp_session is itself part of
        # _self_inflicted_teardown_expected(), so the auto-start is already
        # suppressed by the elif immediately after that _recovery_restart_active
        # check, for exactly this method's duration.
        self._restarting_wpp_session = True
        self._last_wpp_session_restart_ts = now
        try:
            if reason:
                logging.warning(
                    "[_restart_wpp_session] Restarting the WPPConnect session "
                    "in place: %s.", reason)
            else:
                logging.warning(
                    "[_restart_wpp_session] Browser page appears dead (detached "
                    "frame) after suspend/resume — restarting the WPPConnect "
                    "session in place."
                )
            headers = {"Authorization": f"Bearer {self.token}"}
            close_url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/close-session"
            close_accepted = False
            try:
                response = api_post(close_url, headers=headers, timeout=15)
                close_accepted = response.status_code in (200, 201)
                if not close_accepted:
                    logging.warning(
                        "[_restart_wpp_session] close-session returned HTTP %s.",
                        response.status_code,
                    )
            except Exception as exc:
                logging.warning("[_restart_wpp_session] close-session failed: %s", exc)

            # A 200 only means the controller accepted the request; it is not
            # permission to start a new browser on top of a CLOSING slot. The
            # old fixed sleep reproduced a field failure where the close's
            # eight-second watchdog killed the replacement browser just after
            # it connected. Wait for the server's honest CLOSED transition.
            import connection_state as cs
            closed_status = self._wait_for_status(
                cs.session_closed_after_flush,
                self._RECOVERY_CLOSE_WAIT,
                stop_when_connected=False,
            )
            if not cs.session_closed_after_flush(closed_status):
                # Bailing leaves the session closed-but-not-restarted, which
                # is recoverable rather than terminal: the Node side's own 8s
                # watchdog force-kills and clears the slot, status-session then
                # answers CLOSED, and the next health cycle's CLOSED branch
                # auto-starts it (that branch has no cooldown of its own). Since
                # _RECOVERY_CLOSE_WAIT is 12s and that watchdog is 8s, reaching
                # this at all means the Node handler itself is wedged.
                logging.error(
                    "[_restart_wpp_session] Session did not reach CLOSED after "
                    "close-session (accepted=%s, status=%s) — refusing to start "
                    "a replacement browser on top of the old one. The health "
                    "loop's CLOSED auto-start is what picks this up.",
                    close_accepted,
                    closed_status or "?",
                )
                return False

            # CLOSED is the FIRST of two gates and never the second. It says
            # WPPConnect's own state machine finished; it says nothing about
            # Chrome having let go of userDataDir. _stop_wpp_server() has
            # always waited for both ("Neither substitutes for the other"),
            # and this path waited only for the first — so the replacement
            # browser opened the login database while the outgoing Chrome was
            # still flushing it.
            #
            # That is how a SUSPEND destroyed a profile, which is otherwise
            # hard to credit — nothing is killed by suspending. Measured
            # 2026-09-10, after 5.5 hours asleep:
            #
            #   18:34:09.496  close-session -> 200
            #   18:34:09.536  status-session -> CLOSED   (first gate, 40 ms)
            #   18:34:09.576  start-session  -> 200      (80 ms after close)
            #   18:34:18      Session Unpaired -> post_logout=1&logout_reason=0
            #
            # Eighty milliseconds. The Chrome that had just been resumed from
            # a 5.5-hour suspend, with a whole session's worth of state to
            # write back, had not finished — and the same wait that was
            # skipped here took 20 s when the recovery finally ran it at
            # 18:36:01 ("still held after 20s — killing the holder(s)"). The
            # session then looked logged out, the QR handler read that as a
            # broken profile, and a 21-hour-old snapshot was restored over it.
            #
            # wait_for_profile_release() is the same helper _stop_wpp_server()
            # uses, and it kills an orphaned Chrome as its fallback, so a
            # browser that never lets go still ends with a startable profile.
            # Not fatal if it times out: starting anyway is exactly what this
            # did before, and createSessionUtil's stale-lock recovery is the
            # net under it.
            session_name = (getattr(self, "token", "") or "").split(":")[0]
            released = False
            if session_name:
                released = self.wait_for_profile_release(
                    session_name, timeout=self._RESTART_PROFILE_RELEASE_WAIT
                )
                if not released:
                    logging.warning(
                        "[_restart_wpp_session] Chrome still holds %s after %ss "
                        "— starting anyway; the stale-lock recovery is the net.",
                        session_name[:12], self._RESTART_PROFILE_RELEASE_WAIT,
                    )

            if getattr(self, "_profile_restore_in_flight", False):
                # Same reason as _restart_session_once(): a restore that began
                # during the close or the release wait owns the profile now.
                logging.info("[_restart_wpp_session] a profile restore took "
                             "the session over — not starting it.")
                return False

            if on_profile_released is not None:
                if released:
                    try:
                        on_profile_released()
                    except Exception:
                        logging.exception("[_restart_wpp_session] the step between "
                                          "close and start failed (%s)", reason)
                else:
                    logging.warning("[_restart_wpp_session] not running %s: Chrome "
                                    "did not release the profile.", reason)
                if getattr(self, "_profile_restore_in_flight", False):
                    logging.info("[_restart_wpp_session] a profile restore took "
                                 "the session over during %s — not starting it.",
                                 reason)
                    return False
            if getattr(self, "_shutting_down", False) or getattr(self, "_wpp_updating", False):
                # WinZapp began closing (or WPPConnect updating) while this
                # waited or copied. A browser opened now would be killed by
                # the teardown's taskkill mid-write — how profiles break.
                logging.info("[_restart_wpp_session] WinZapp is closing or "
                             "WPPConnect is updating — not starting a browser "
                             "under the teardown.")
                return False
            start_url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/start-session"
            try:
                api_post(start_url, json={"waitQrCode": False}, headers=headers, timeout=15)
                logging.info("[_restart_wpp_session] start-session requested.")
            except Exception as exc:
                logging.warning("[_restart_wpp_session] start-session failed: %s", exc)
            return True
        finally:
            self._restarting_wpp_session = False

    # A live WPPConnect Socket.IO event this recent is treated as direct
    # proof of connectivity — see _note_live_wpp_event() and the top of
    # check_whatsapp_reachable(). Set well above the ~15-20s cadence
    # chats-update/presence events already show on an active account, so a
    # single missed tick can't cause a false "still online" reading right as
    # a real outage begins.
    _LIVE_WPP_EVENT_FRESHNESS_SECONDS = 45.0

    def _note_live_wpp_event(self):
        """Record that a WPPConnect Socket.IO event was just received.

        Called by WebSocketClient for events that could only have been
        emitted by a genuinely live, connected WhatsApp session (new
        messages, delivery acks, chats/presence updates). Safe to call from
        any thread — a plain float write.
        """
        self._last_live_wpp_event_ts = time.time()

    def check_whatsapp_reachable(self) -> bool:
        """Decide whether WhatsApp traffic can actually flow right now.

        ``/status-session`` cannot answer this: it just echoes the session
        status string WPPConnect cached when the session was created, so it
        keeps saying CONNECTED with the network cable unplugged — which is why
        the app used to announce itself connected, start a sync and fire the
        send queue with no internet at all.

        Three sources are combined, checked in order of directness:

        * A WPPConnect Socket.IO event received within the last
          _LIVE_WPP_EVENT_FRESHNESS_SECONDS — undeniable proof traffic is
          flowing right now, checked first and short-circuits everything
          else. WinZapp's Socket.IO client talks to the LOCAL WPPConnect
          server over loopback, which stays up regardless of the machine's
          own internet route, so it keeps delivering events even when the
          probe below is the one that's actually broken (observed live: a
          switched Wi-Fi/mobile network left this machine's own outbound
          reachability check failing — the classic symptom of a stale
          negative DNS cache entry surviving a network change — for several
          minutes while messages kept arriving, sound and all, in an open
          group the whole time; the app insisted it was offline throughout
          because this signal didn't exist yet).
        * ``/check-connection-session``, which reports the session as
          Disconnected when WhatsApp Web itself has gone down inside the
          browser.  Only an explicit ``true`` from the page counts as
          Connected there: a thrown ``isConnected()`` (a reload with the WAPI
          namespace gone), a ``false`` one, and one still unanswered after the
          route's own 8 s budget all answer False — so a False here is
          meaningful but a True is not conclusive.  That budget matters: it is
          what keeps the answer inside the 10 s allowed below, and a request
          that times out instead raises into the ``except`` and counts no
          strike at all.
        * a direct reachability probe against WhatsApp's servers, which is what
          catches the plain "this machine has no internet" case.

        Both negatives require _OFFLINE_PROBE_STRIKES *consecutive* readings
        before they count — see the session-probe branch below for why the
        first one is not proof of anything. While an initial sync is running
        that budget is widened through connection_state.probe_strike_budget(),
        each branch for its own reason (a WhatsApp Web page reload for the
        session probe, resource/DNS contention for the host probe — both
        spelled out at the branches themselves), and the widening is capped at
        SYNC_TOLERANCE_MAX_SECONDS of wall clock so a real outage mid-sync is
        not held invisible for the ten minutes the raw factor would buy.

        Note what this widening is NOT for, because the obvious guess is
        wrong: a Node too busy to answer at all never reaches either branch. A
        request that times out raises, lands in the `except` below, and falls
        through to _probe_whatsapp_host() without setting session_down or
        counting a strike at all. Getting here means the local API *answered*.
        """
        import connection_state as cs
        last_live = getattr(self, "_last_live_wpp_event_ts", 0.0)
        if last_live and (time.time() - last_live) < self._LIVE_WPP_EVENT_FRESHNESS_SECONDS:
            self._offline_probe_strikes = 0
            self._offline_probe_first_strike_ts = 0.0
            return True
        url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/check-connection-session"
        session_down = False
        try:
            resp = api_get(
                url,
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=10,
            )
            if resp.status_code in (200, 201):
                data = resp.json()
                if not data.get("status"):
                    session_down = True
            elif resp.status_code == 404:
                session_down = True
        except Exception as e:
            logging.warning("[check_whatsapp_reachable] session probe failed: %s", e)

        if session_down:
            # This used to skip the strike tally entirely — it set
            # _offline_probe_strikes to the limit and returned False on the
            # spot, so one negative reading was enough to declare an outage.
            # But the commonest cause of a negative here is not an outage at
            # all: WhatsApp Web reloads its own page from time to time, and
            # while it does, WPPConnect's isConnected() evaluates
            # WAPI.isConnected() against a page where the injected WAPI
            # namespace no longer exists — it throws ReferenceError, and the
            # endpoint duly answers "not connected".
            #
            # Observed live: a 28-second reload window (wppconnect.log:
            # "Execution context was destroyed, most likely because of a
            # navigation", then ~700 "WAPI is not defined", then "State
            # Change CONNECTED") announced a full "modo offline" with sound
            # and speech, aborted LID resolution, marked a complete 574-chat
            # sync as incomplete and skipped the whole media phase — with the
            # machine's internet perfectly fine throughout, and with the page
            # already healthy again 4 seconds later.
            #
            # Requiring the same consecutive strikes the host probe already
            # requires rides out a reload; a genuine outage still registers
            # on the next health-check tick ~30 s later.
            #
            # And during an initial sync, two strikes / ~60 s is not enough of
            # a window for it. This branch is reached only when the local API
            # *answered* — with status:false, i.e. isConnected() threw inside
            # the page, answered false, or was still unanswered when the
            # route's own budget ran out — so the reason to be more patient
            # here is the reload above, not a busy Node (a Node too busy to
            # answer raises in the `except` and never gets this far). A reload
            # is both likelier and slower to finish while WhatsApp Web is being
            # driven through a long history download than on an idle session,
            # and the measured one already lasted 28 s on its own. Observed
            # live: a long initial sync ending in "modo offline" and then a full
            # disconnect a health-check cycle or two later, with no real
            # network interruption. The widened budget is capped in wall-clock
            # time — see probe_strike_budget() for why ~10 minutes of holding
            # a stale "connected" is its own bug.
            now = time.time()
            allowed_strikes = cs.probe_strike_budget(
                self._OFFLINE_PROBE_STRIKES,
                initial_sync_running=getattr(self, "_initial_sync_running", False),
                first_strike_ts=getattr(self, "_offline_probe_first_strike_ts", 0.0),
                now=now,
            )
            if not getattr(self, "_offline_probe_first_strike_ts", 0.0):
                self._offline_probe_first_strike_ts = now
            self._offline_probe_strikes = getattr(self, "_offline_probe_strikes", 0) + 1
            if self._offline_probe_strikes >= allowed_strikes:
                return False
            logging.info(
                "[check_whatsapp_reachable] session probe reports disconnected "
                "(strike %d/%d) — holding the current state until the budget is spent.",
                self._offline_probe_strikes, allowed_strikes,
            )
            return bool(getattr(self, "_wa_connected", False))

        if self._probe_whatsapp_host():
            self._offline_probe_strikes = 0
            self._offline_probe_first_strike_ts = 0.0
            return True
        # This one is a HEAD straight at web.whatsapp.com — the local Node
        # process is not on that route at all, so "Node is busy" explains
        # nothing here. What an initial sync does explain is the machine
        # itself being loaded: hundreds of media downloads in flight compete
        # for sockets, DNS and CPU with a probe that has a short timeout, and
        # a probe that loses that race is not an outage. That is a much
        # weaker excuse than the session branch's reload, which is exactly
        # why the ceiling matters more here: if the network really is gone,
        # this is the signal that says so, and it must not stay muffled for
        # the length of a sync.
        now = time.time()
        allowed_strikes = cs.probe_strike_budget(
            self._OFFLINE_PROBE_STRIKES,
            initial_sync_running=getattr(self, "_initial_sync_running", False),
            first_strike_ts=getattr(self, "_offline_probe_first_strike_ts", 0.0),
            now=now,
        )
        if not getattr(self, "_offline_probe_first_strike_ts", 0.0):
            self._offline_probe_first_strike_ts = now
        self._offline_probe_strikes = getattr(self, "_offline_probe_strikes", 0) + 1
        if self._offline_probe_strikes >= allowed_strikes:
            return False
        # Budget not spent yet: hold the current state and let the next
        # health-check tick decide. Outside a sync that is the single
        # extra cycle _OFFLINE_PROBE_STRIKES buys; during one it is as
        # many cycles as the widened budget still allows, which the
        # SYNC_TOLERANCE_MAX_SECONDS ceiling above bounds in real time.
        return bool(getattr(self, "_wa_connected", False))

    def _is_pairing_dialog_active(self) -> bool:
        """True while the connection/pairing dialog is on screen — i.e. the
        user has never actually paired yet (or is re-pairing) and is looking
        straight at it."""
        dial = getattr(self.connect, "connection_dial", None)
        return bool(dial) and dial.IsShown()

    def _handle_local_auth_rejected(self, http_status: int) -> None:
        """A /status-session poll came back 401/403 — our OWN local Node
        server's auth middleware rejected the request.

        This used to call _on_disconnect() (drop the token, wipe the whole
        local database) the very first time this happened, with none of the
        protection the sibling "unlinked status string" path below applies
        (_auto_restart_grace_active(), several consecutive confirmations
        spread over minutes, and the ever_connected split that keeps a run
        which has never actually connected from ever wiping). A 401/403 here
        is if anything WEAKER evidence of a real WhatsApp-side unlink than a
        notLogged/QRCODE reading: it never even reaches WhatsApp, since our
        own auth middleware refused the request before WPPConnect saw it —
        which can just as easily mean the local session/secret-key state on
        a freshly started or just-switched-to Node isn't ready yet. Reported
        live: the app wiped a paired account before it had even loaded the
        chat list on a cold start. Routing this through the exact same
        grace/strike machinery the string-status path uses closes that gap.

        Note what that costs the final gate on this path in particular:
        _act_on_unlink_decision()'s host-device probe leaves through the very
        middleware that is refusing us, so it usually comes back
        LINK_PROBE_UNKNOWN rather than proving anything. That is why UNKNOWN
        is not "could not prove it is linked, wipe anyway" — a Node restarted
        under a rotated local token would otherwise reach the same full wipe
        as before, merely a minute later.
        """
        import connection_state as cs
        logging.warning(
            "[check_wa_connection_http] status-session returned HTTP %s "
            "(local auth rejected the request).", http_status,
        )
        # Through the funnel, not a bare flag write: _set_wa_connected() is
        # what also engages _auto_offline, repaints the tray text and speaks
        # the offline announcement. Writing _wa_connected directly stopped the
        # MessageQueue (which reads it) while leaving the UI claiming to be
        # connected, so typed messages sat in the queue looking sent and a
        # screen-reader user was told nothing at all — for the ≥60s this takes
        # to confirm, and far longer behind a veto. `confirmed` stays False on
        # purpose: this whole method exists because a local 401 is exactly the
        # kind of ambiguous negative the startup grace is for.
        self._set_wa_connected(False, f"status-session HTTP {http_status}")
        if not self.settings.get("privateinfo", {}).get("paired"):
            # Not paired: there is nothing to lose, and _on_disconnect() is
            # what puts the pairing dialog on screen.
            wx.CallAfter(self._on_disconnect)
            return

        with self._unlink_decision_lock:
            if self._auto_restart_grace_active():
                logging.info(
                    "[check_wa_connection_http] HTTP %s seen while an automatic "
                    "session restart is still settling — not confirming a "
                    "logout yet.", http_status,
                )
                self._logout_strikes = 0
                self._resume_fail_strikes = 0
                self._last_strike_ts = 0.0
                # The restart breaks the veto run too: _STILL_LINKED_VETO_LIMIT
                # counts consecutive vetoes, and a session torn down and rebuilt
                # in between is not the same run of readings.
                self._still_linked_vetoes = 0
                return

            now = time.time()
            if not cs.should_count_strike(now, getattr(self, "_last_strike_ts", 0.0)):
                logging.info(
                    "[check_wa_connection_http] HTTP %s within strike interval — "
                    "not counting; data preserved.", http_status,
                )
                return
            self._last_strike_ts = now
            ever = bool(getattr(self, "_wa_connect_announced", False))
            if ever:
                self._logout_strikes = getattr(self, "_logout_strikes", 0) + 1
            else:
                self._resume_fail_strikes = getattr(self, "_resume_fail_strikes", 0) + 1
            decision = cs.classify_unlink_candidate(
                ever_connected=ever,
                logout_strikes=getattr(self, "_logout_strikes", 0),
                resume_strikes=getattr(self, "_resume_fail_strikes", 0),
                logout_confirm_strikes=self._LOGOUT_CONFIRM_STRIKES,
                resume_fail_strikes=self._RESUME_FAIL_STRIKES,
            )
            self._act_on_unlink_decision(decision, log_label=f"HTTP {http_status}")

    def check_wa_connection_http(self):
        """Query the WPPConnect API via HTTP to check if the instance is already connected to WhatsApp."""
        if self._is_pairing_dialog_active() or getattr(self, "_pairing_in_progress", False):
            # Nothing below is meaningful yet: WPPConnect reporting
            # CLOSED/QRCODE/notLogged while the user is actively pairing is
            # completely normal — that IS what "not paired yet" / "waiting for
            # the QR or phone code to be entered" looks like, not an outage.
            #
            # The guard checks BOTH the on-screen dialog AND _pairing_in_progress
            # (set by connect.py for the whole pairing flow). Relying on the
            # dialog alone missed the case that actually bit a live multi-account
            # user: a freshly-switched-to pending account pairing where the
            # health poll saw QRCODE (the expected "scan me" state) and fired
            # _on_disconnect() → clear_local_data() mid-pairing, ~1s after the QR
            # appeared, so pairing could never complete. _pairing_in_progress is
            # the authoritative "do not touch the session" signal.
            #
            # This whole HTTP poll exists to detect/recover from outages *after*
            # pairing; pairing's own completion is driven by WebSocket events
            # (on_connection_update/session-logged), so skipping it here loses
            # nothing.
            return
        url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/status-session"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }
        try:
            response = api_get(url, headers=headers, timeout=10)
            # The local API answered at all — whatever it says below, the
            # request-level failure streak that would otherwise declare an
            # outage is not applicable here.
            self._wa_http_fail_strikes = 0
            if response.status_code in (401, 403):
                self._handle_local_auth_rejected(response.status_code)
                return

            if response.status_code in (200, 201):
                data = response.json()
                # WPPConnect /status-session returns {"status": "CONNECTED"} — the key is
                # "status", not "state".  Reading "state" always yields "" which incorrectly
                # triggers /start-session even when a session is already alive.
                status = (
                    data.get("status")
                    or data.get("state")
                    or data.get("response", {}).get("status")
                    or data.get("response", {}).get("state")
                    or ""
                )

                logging.info("[check_wa_connection_http] Instance status: %s", status)

                # Wrapped here as well as inside the method. Everything from
                # the `try` above down to the request handler is what decides
                # whether WinZapp believes it is online, and an exception
                # escaping this line would be caught there and reported as
                # "[check_wa_connection_http] Request failed" — a probe that
                # answered perfectly well, recorded as a strike against the
                # connection, because of a bug in a diagnostic. Profile health
                # is an observer of this poll and must never be able to change
                # its verdict.
                try:
                    self._note_status_for_profile_health(status)
                except Exception:
                    logging.exception("[profile-health] observer failed (non-fatal)")

                # Any status other than the two unlinked ones clears the logout
                # tally, so only *consecutive* readings can ever confirm one —
                # see _LOGOUT_CONFIRM_STRIKES.
                import connection_state as cs
                if status not in cs.UNLINKED_STATES:
                    # Under the same lock the counting paths take: this writes
                    # the very attributes their count-then-decide sequence
                    # reads, and a reset landing in the middle of one would
                    # decide on a tally half of which had just been cleared.
                    # (Only ever clears, so the risk is a missed confirmation
                    # rather than a spurious wipe — but the atomicity is the
                    # property the tests assert, so make it real.)
                    with self._unlink_decision_lock:
                        self._logout_strikes = 0
                        self._resume_fail_strikes = 0
                        self._last_strike_ts = 0.0
                        # A run of host-device vetoes only counts while it is
                        # unbroken — see _STILL_LINKED_VETO_LIMIT.
                        self._still_linked_vetoes = 0

                # Robust check: Only call start-session if the instance is explicitly CLOSED, DESTROYED, or completely inactive.
                # WPPConnect status values include: CONNECTED, open, INITIALIZING, QRCODE, PHONECODE, notLogged, inChat, PAIRED, etc.
                if status in ("CONNECTED", "open"):
                    # "CONNECTED" only means the WPPConnect session object is
                    # alive — it is a cached string that stays put when the
                    # machine loses internet.  Confirm against the live
                    # isConnected() probe before declaring ourselves online,
                    # otherwise the app plays the "connected" sound, starts a
                    # sync and lets the send queue fire with no connectivity.
                    if not self.check_whatsapp_reachable():
                        # See _nudge_whatsapp_socket_stream(): a headless,
                        # never-focused page has no natural trigger left to
                        # reopen WhatsApp Web's own socket after a
                        # suspend/resume cycle — without this, this branch
                        # (and therefore offline mode) can persist forever,
                        # since nothing else ever pokes the page to retry.
                        if self._nudge_whatsapp_socket_stream():
                            self._dead_browser_strikes = 0
                        else:
                            # The nudge request itself failed — not just "no
                            # trigger fired", but the page/frame Puppeteer
                            # holds is gone. Escalate to a full session
                            # restart after enough consecutive failures — see
                            # _restart_wpp_session() and
                            # _auto_restart_grace_active() (checked in the
                            # confirmed-logout branch further down this same
                            # function) for why this is safe to do
                            # automatically now.
                            self._dead_browser_strikes = getattr(self, "_dead_browser_strikes", 0) + 1
                            if self._dead_browser_strikes >= self._DEAD_BROWSER_RESTART_STRIKES:
                                self._dead_browser_strikes = 0
                                threading.Thread(target=self._restart_wpp_session, daemon=True).start()
                        self._set_wa_connected(False, "status-session CONNECTED but isConnected() false")
                        return
                    self._dead_browser_strikes = 0
                    self._set_wa_connected(True, "status-session CONNECTED")
                    try:
                        dev_url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/host-device"
                        dev_resp = api_get(dev_url, headers=headers, timeout=5)
                        if dev_resp.status_code in (200, 201):
                            dev_data = dev_resp.json()
                            phoneNumberObj = dev_data.get("response", {}).get("phoneNumber", {})
                            wuid = ""
                            if isinstance(phoneNumberObj, dict):
                                wuid = phoneNumberObj.get("_serialized", "")
                            elif isinstance(phoneNumberObj, str):
                                wuid = phoneNumberObj
                            if wuid:
                                self.my_jid = wuid
                                if hasattr(self, "db") and self.db is not None:
                                    self.db.set_metadata("my_jid", wuid)
                                self.resolve_self_lid()
                                # Mark as paired on successful HTTP host check too
                                pi = self.settings.setdefault("privateinfo", {})
                                settings_changed = False
                                if not pi.get("paired"):
                                    pi["paired"] = True
                                    settings_changed = True
                                # Same answer, read for a second purpose: this
                                # is the ordinary, non-pairing moment at which
                                # WhatsApp tells us which phone is linked, so
                                # an install that has never recorded one learns
                                # it here rather than during the divergent
                                # pairing it is meant to catch. Write-only —
                                # see record_linked_phone_if_unknown().
                                if record_linked_phone_if_unknown(self, wuid):
                                    settings_changed = True
                                if settings_changed:
                                    self.save_settings()
                    except Exception as e:
                        # Same host-device URL, same token in its path — see
                        # _still_linked_on_server() for why the exception has
                        # to be redacted before it reaches log.log.
                        logging.error(
                            "[check_wa_connection_http] Failed to fetch host device JID: %s: %s",
                            type(e).__name__, redact_credentials(str(e)),
                        )
                elif status in ("CLOSED", "DESTROYED", ""):
                    self._set_wa_connected(False, f"status-session {status or 'unknown'}")
                    # Status is CLOSED or unknown: normally safe to start a new
                    # session — but four different things can own the browser (or
                    # deliberately want it left closed) at this exact moment. That
                    # four-way decision lives in connection_state so each guard is
                    # testable on its own; it hands back the reason to log, one per
                    # guard, or None when starting is the right answer.
                    block = cs.auto_start_block_reason(
                        pairing_dialog_active=self._is_pairing_dialog_active(),
                        qr_flood_halted=getattr(self, "_qr_flood_halted", False),
                        recovery_restart_active=self._session_restart_owned(),
                        self_inflicted_teardown=self._self_inflicted_teardown_expected(),
                    )
                    if block:
                        logging.info("[check_wa_connection_http] Skipping auto-start — %s.", block)
                    else:
                        try:
                            start_url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/start-session"
                            api_post(start_url, json={"waitQrCode": False}, headers=headers, timeout=10)
                            logging.info("[check_wa_connection_http] Sent auto-start session command")
                            # Direct evidence that a start is being attempted.
                            # The tracker used to depend on a 30 s poll landing
                            # on INITIALIZING inside a ~60 s cycle, which in
                            # the field it never did after launch — see
                            # ProfileHealthTracker's own docstring.
                            self._note_session_start_for_profile_health()
                        except Exception as e:
                            logging.error("[check_wa_connection_http] Failed to auto-start session: %s", e)
                else:
                    # Instance is in some active state (e.g. notLogged, inChat, QRCODE, INITIALIZING, etc.)
                    # We should NOT call start-session to avoid launching duplicate Puppeteer tabs.
                    # None of these states can carry WhatsApp traffic, so the
                    # app must consider itself offline while they last.
                    # notLogged/QRCODE are a definite, explained signal (the
                    # dialog below tells the user pairing is needed) — skip the
                    # startup grace for those. Anything else here (inChat,
                    # INITIALIZING, PAIRED, ...) is exactly the kind of normal
                    # mid-boot status that must NOT flip the UI to "offline".
                    self._set_wa_connected(
                        False, f"status-session {status}",
                        confirmed=status in cs.UNLINKED_STATES,
                    )
                    logging.info(
                        "[check_wa_connection_http] Session is in active state '%s' — skipping /start-session to avoid browser conflict.",
                        status,
                    )
                    if status in cs.UNLINKED_STATES:
                        self._wa_connected = False

                        if self.settings.get("privateinfo", {}).get("paired"):
                            # Destructive decisions go through the pure classifier
                            # (connection_state.classify_unlinked) so the "logout
                            # vs still-resuming vs resume-failed" rule is one
                            # tested place. The key invariant: never wipe a
                            # session that has not been connected THIS run — it's
                            # resuming from its saved profile, and a transient
                            # QRCODE during resume is NOT a logout (the bug that
                            # kept wiping accounts; a real log showed the server
                            # reaching 'inChat' the same second the client wiped).
                            import connection_state as cs
                            with self._unlink_decision_lock:
                                # An automatic _restart_wpp_session() (dead-browser
                                # recovery) legitimately re-shows a fresh QR itself
                                # whenever the stored token turns out to already be
                                # bad — that is NOT a phone-side unlink, and must
                                # never be confirmed as one (a real incident: it
                                # wiped a user's local database).
                                if self._auto_restart_grace_active():
                                    logging.info(
                                        "[check_wa_connection_http] %s seen while an "
                                        "automatic session restart is still settling — "
                                        "not confirming a logout yet.", status,
                                    )
                                    self._logout_strikes = 0
                                    self._resume_fail_strikes = 0
                                    self._last_strike_ts = 0.0
                                    # Same reasoning as the tally above: a
                                    # restart in the middle breaks the veto
                                    # run — see _STILL_LINKED_VETO_LIMIT.
                                    self._still_linked_vetoes = 0
                                    return
                                ever = bool(getattr(self, "_wa_connect_announced", False))
                                # Count at most one strike per STRIKE_MIN_INTERVAL so
                                # the thresholds mean real elapsed time, not raw
                                # reading count: tight poll loops (e.g. _run_sync's
                                # 0.2s cadence) used to race through 20 unlinked
                                # readings in ~6s and wipe a session WhatsApp had just
                                # logged back in. A reading inside the interval is
                                # still "resuming, data preserved" — never escalates.
                                now = time.time()
                                if not cs.should_count_strike(now, getattr(self, "_last_strike_ts", 0.0)):
                                    logging.info(
                                        "[check_wa_connection_http] Unlinked '%s' "
                                        "(ever_connected=%s) — within strike interval, "
                                        "not counting; data preserved.", status, ever)
                                    return
                                self._last_strike_ts = now
                                if ever:
                                    self._logout_strikes = getattr(self, "_logout_strikes", 0) + 1
                                else:
                                    self._resume_fail_strikes = getattr(self, "_resume_fail_strikes", 0) + 1
                                decision = cs.classify_unlinked(
                                    status,
                                    ever_connected=ever,
                                    logout_strikes=getattr(self, "_logout_strikes", 0),
                                    resume_strikes=getattr(self, "_resume_fail_strikes", 0),
                                    logout_confirm_strikes=self._LOGOUT_CONFIRM_STRIKES,
                                    resume_fail_strikes=self._RESUME_FAIL_STRIKES,
                                )
                                self._act_on_unlink_decision(decision, log_label=status)
                            return
                        else:
                            # Not paired: there is nothing to lose (the database
                            # is empty by definition) and _on_disconnect() is
                            # what puts the pairing dialog on screen. Delaying it
                            # behind the confirmation left the app sitting on
                            # "sem conexão com o WhatsApp / modo offline" with no
                            # way to connect — the guard was protecting data that
                            # does not exist, at the cost of the one action the
                            # user actually needed.
                            wx.CallAfter(self._on_disconnect)
        except Exception as e:
            # The local API itself did not answer — we certainly cannot reach
            # WhatsApp through it either, but only once this has happened
            # _HTTP_PROBE_STRIKES times in a row (see that constant): a lone
            # failed request is far more often a briefly-busy local Node
            # process than a real outage.
            self._wa_http_fail_strikes = getattr(self, "_wa_http_fail_strikes", 0) + 1

            # If the initial sync is running and downloading history for hundreds of chats,
            # the Node.js server event loop can be blocked for >30s. Don't falsely declare
            # an offline outage just because of these timeouts.
            #
            # max_seconds=None keeps this branch's behaviour exactly as it has
            # shipped: no wall-clock ceiling, unlike check_whatsapp_reachable()'s
            # two branches. Deliberate asymmetry — this x10 is the one already
            # proven in the field against a Node blocked by a history download,
            # and tightening a fix nobody has reported a problem with would be
            # trading a known-good behaviour for a hypothetical one.
            import connection_state as cs
            allowed_strikes = cs.probe_strike_budget(
                self._HTTP_PROBE_STRIKES,
                initial_sync_running=getattr(self, "_initial_sync_running", False),
                max_seconds=None,
            )

            logging.warning(
                "[check_wa_connection_http] Request failed (strike %d/%d): %s",
                self._wa_http_fail_strikes, allowed_strikes, e,
            )
            if self._wa_http_fail_strikes < allowed_strikes:
                return
            self._set_wa_connected(False, f"status-session request failed: {e}")
            logging.error("[check_wa_connection_http] Error checking connection state: %s", e)
