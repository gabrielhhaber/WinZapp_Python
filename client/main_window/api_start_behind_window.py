"""Start the local WPPConnect Server behind the main window (issue #407).

A foreground launch used to start Node inside the modal "Starting WPPConnect,
please wait" dialog (ApiStartupDialog) and build the main window only after
the port answered. Measured on a Mac launch (2026-10-05): the dialog held the
window back 2.5 s — 1.4 s of WhatsApp Web catalogue check before the spawn
(up to wa_version_refresh.JOIN_SECONDS, about once every 6 hours) and 1.1 s
for Node to listen. Nothing the window shows needs Node: the chat list comes
from the local database, the title already says "connecting" until WhatsApp
answers, and the MessageQueue already holds every send while _wa_connected is
False. So for an account that is already paired, the window is built at once
and Node starts behind it.

Only that case. Everything that genuinely needs an answer from Node before
there is anything to show keeps the dialog-first order, unchanged:

* no pairing yet (first run, a `pending` account, paired=False) — the pairing
  dialog comes next, and it needs the server;
* paired with no token, or a legacy token without its ":<hash>" half —
  _recover_active_session_token() and retrieve_token()'s migration both ask
  Node before __init__ can go on;
* a --background launch — it never had a dialog, and it waits for Node in
  __init__ on purpose (docs/traps/session-startup.md);
* a custom API, a Node already listening (adopted in milliseconds anyway) and
  an install whose API files are missing (ensure_wpp_running() skips those).

What the dialog order guaranteed, and how this keeps it:

* Nothing that startup itself launches talks to a dead port. Four background
  threads are started before Node can answer and do not wait for a
  connection on their own — post_ui_init, wait_messages_set()'s probe, the
  health checker and the abandoned-session cleanup — so each calls
  wait_for_api_start() first. Everything else that reaches Node is gated on
  _wa_connected, which only a live Node can make True, or on the user acting
  in the window, which the app already handles while WhatsApp is still
  connecting. The cleanup matters most: with Node down it skips the WhatsApp
  logout of a superseded session and deletes its profile anyway, leaving a
  linked device on the phone that nothing can remove.
* The pairing check __init__ makes before init_UI() is made here instead,
  once Node answers (_confirm_pairing_behind_window()), through the same
  _pair_account_at_startup(): switch-account offer, pairing dialog and
  registry promotion.
* Node never coming up — or any error in this start — ends exactly as the
  dialog's timeout did: the "API failed to start" error with the log tail,
  then a full quit through real_exit(), so no windowless process is left
  holding the instance lock. Not while a WPPConnect update owns the API, as
  in ensure_wpp_running().
* The spawn stays on the UI thread, checked against a quit in the same turn.
* Update prompts wait for Node: wpp_update_may_run_now() — which the app
  update prompt, the WPPConnect update check and the background install all
  ask — answers False until the start has settled, exactly as it used to
  answer False while __init__ was still inside the dialog.
"""

import logging
import threading
import time
from typing import Optional

import wx

from main_window.wpp_server import bundled_api_present


#: Same budget ApiStartupDialog and the background branch of
#: ensure_wpp_running() give Node: first launches and slow machines (HDD,
#: antivirus, a first-run Chrome download) need minutes.
API_START_TIMEOUT_SECONDS = 300


def may_start_api_behind_window(*, background_mode: bool, custom_api: bool,
                                api_listening: bool, api_files_present: bool,
                                resume_pending: bool, paired: bool,
                                token: str) -> bool:
    """Whether this launch may build the main window before Node answers.

    True only for a foreground launch of an account that is already paired and
    holds a complete local token, with a bundled API this process will spawn.
    Every other case needs the server before the window (see the module
    docstring) and keeps the startup dialog.
    """
    if background_mode or custom_api or api_listening or not api_files_present:
        return False
    if resume_pending or not paired:
        return False
    return ":" in (token or "")


def wait_for_api_start(window, timeout: Optional[float] = None) -> bool:
    """Block until a start running behind the window has settled.

    True when Node is listening (or no such start is running — every launch
    that went through ensure_wpp_running() already waited); False when it never
    came up, in which case the app is already quitting and the caller should
    simply stop. Works on any object, so a test stub without the attributes
    behaves like an ordinary launch.
    """
    settled = getattr(window, "_api_start_settled", None)
    if settled is None:
        return True
    if not settled.wait(timeout):
        return False
    return not getattr(window, "_api_start_failed", False)


class ApiStartBehindWindowMixin:
    """MainWindow methods that start the local API while the window is shown."""

    def _may_start_api_behind_window(self) -> bool:
        try:
            private_info = self.settings.get("privateinfo", {}) or {}
            token = self._get_wa_token() or ""
        except Exception:
            # Anything unreadable here is a reason for the old, dialog-first
            # order, never for skipping the start.
            logging.exception("[api-start-behind-window] could not read the pairing state")
            return False
        return may_start_api_behind_window(
            background_mode=self.background_mode,
            custom_api=bool(self.wpp_custom_api),
            api_listening=self._is_wpp_running(),
            api_files_present=bundled_api_present(),
            resume_pending=bool(getattr(self, "resume_pending", False)),
            paired=bool(private_info.get("paired")),
            token=token,
        )

    def _start_api_behind_window(self):
        """Spawn Node and wait for its port on a worker; return at once.

        Called from __init__ in place of ensure_wpp_running(), so the port is
        settled here, synchronously, for the same reason that method settles
        it before its dialog: everything __init__ builds next reads
        self.wpp_port.
        """
        # First, so that if it raises nothing below claims a start is running:
        # __init__ logs the error and goes on as it did when
        # ensure_wpp_running() raised from the same call.
        self._ensure_wpp_port_still_free()
        self._api_started_behind_window = True
        self._api_start_failed = False
        self._api_start_settled = threading.Event()
        self._wpp_log_path = None
        self._wpp_log_fh = None
        threading.Thread(target=self._api_start_behind_window_worker,
                         name="api-start-behind-window", daemon=True).start()

    #: How often the worker re-checks that the app is not quitting while the
    #: spawn it queued on the UI thread has not run yet.
    _SPAWN_WAIT_POLL_SECONDS = 0.5

    def _api_start_behind_window_worker(self):
        """wait for the catalogue, spawn, poll for the port.

        Whatever happens in here ends in _api_start_settled being set: every
        thread in wait_for_api_start() blocks on it with no timeout, and an
        exception that skipped the set would leave the app on "connecting"
        for good, with the update prompts gated behind it as well.
        """
        try:
            if self._run_api_start_behind_window():
                return
        except Exception:
            logging.exception("[api-start-behind-window] the start failed")
        self._fail_api_start_behind_window()

    def _run_api_start_behind_window(self) -> bool:
        """The start itself; True once Node answers, False when it did not.

        Only the waiting happens here. The spawn goes back to the UI thread,
        where it has always run (_start_wpp_background_after_catalogue()): it
        rewrites os.environ, and a setenv from a worker while Cocoa/wx builds
        the window can crash on macOS. Queued with wx.CallAfter it cannot
        overlap init_UI(), which is still running when this is queued.
        """
        try:
            from core.wa_version_refresh import wait_for_refresh
            wait_for_refresh()
        except Exception:
            logging.exception("[wa-version] pre-spawn refresh failed (non-fatal)")
        spawned = threading.Event()
        outcome = {}

        def _spawn():
            try:
                # Checked on the UI thread right before the spawn: a quit or a
                # Windows shutdown that began meanwhile has already looked for
                # a Node to stop, and one spawned now would outlive the
                # process.
                if getattr(self, "_shutting_down", False):
                    outcome["skipped"] = True
                    return
                self._start_wpp_background()
            except Exception as exc:
                outcome["error"] = exc
            finally:
                spawned.set()

        wx.CallAfter(_spawn)
        deadline = time.time() + API_START_TIMEOUT_SECONDS
        # A quit can end the main loop before the queued spawn ever runs.
        # Nothing is spawned then, and this must not wait on it forever.
        while not spawned.wait(self._SPAWN_WAIT_POLL_SECONDS):
            if getattr(self, "_shutting_down", False) or time.time() >= deadline:
                logging.info("[api-start-behind-window] the spawn never ran (quitting or no main loop).")
                return False
        if "error" in outcome:
            raise outcome["error"]
        # A skipped spawn still polls: a shutdown another program cancels
        # restarts Node itself (_restart_wpp_after_cancelled_shutdown), and
        # this start must then release everything waiting on it.
        while time.time() < deadline:
            if self._is_wpp_running():
                logging.info(
                    "[STARTUP_TIMING] T+%.3fs — WPPConnect Server process ready (behind the main window)!",
                    time.perf_counter() - getattr(self, "_t_app_start", time.perf_counter()))
                self._check_wpp_version_pin()
                # The grace before "offline" may be shown is measured from
                # _wa_startup_time, which __init__ set while Node was still
                # starting. Re-armed here so it starts when Node does, as it
                # always has — the same re-arm _on_disconnect does before a
                # re-pair.
                self._wa_startup_time = time.time()
                self._reset_startup_probe()
                self._api_start_settled.set()
                return True
            time.sleep(1)
        logging.error("[api-start-behind-window] WPPConnect never came up within %ss.",
                      API_START_TIMEOUT_SECONDS)
        return False

    def _fail_api_start_behind_window(self):
        """Release every waiter, then end the way the startup dialog ends.

        Two exceptions, both from ensure_wpp_running(). A quit already under
        way says nothing. A WPPConnect update or reinstall in progress
        (_wpp_updating — Help > forced reinstall is not gated on this start)
        owns the API: it restores its predecessor itself, reports its own
        outcome and reconnects afterwards, so this neither shows "API failed
        to start" nor quits. Its waiters are then released as an ordinary
        start, not a failed one — "failed" tells them the app is quitting, and
        the health checker among them is what the update's recovery leans on.
        """
        if getattr(self, "_wpp_updating", False) and not getattr(self, "_shutting_down", False):
            logging.info("[api-start-behind-window] a WPPConnect update owns the API — "
                         "leaving the failure to it.")
            while getattr(self, "_wpp_updating", False) and not getattr(self, "_shutting_down", False):
                time.sleep(1)
            self._api_start_failed = bool(getattr(self, "_shutting_down", False))
            self._api_start_settled.set()
            return
        self._api_start_failed = True
        self._api_start_settled.set()
        if getattr(self, "_shutting_down", False):
            return
        wx.CallAfter(self._report_api_start_failure)

    def _report_api_start_failure(self):
        """UI thread: the startup error, then a full quit."""
        try:
            self._show_api_startup_failure()
        except Exception:
            logging.exception("[api-start-behind-window] could not show the startup error")
        self.real_exit()

    def _confirm_pairing_behind_window(self) -> bool:
        """post_ui_init's STEP 1 for a launch whose window opened before Node.

        The same _pair_account_at_startup() __init__ runs before init_UI() on
        every other launch, moved to the first moment it can be answered. For
        a paired account with a token check_connection_status() is True on
        almost every answer — a session still warming up included — so this
        is nearly always a single request; the rest is the rare server that
        disowns the session.

        Returns False when this process is going away (switching to another
        account, or the user closed the pairing dialog without pairing): the
        caller stops, and real_exit() is already queued. __init__ ends the
        process with sys.exit() there; on this thread that would only end the
        thread and leave the window up, connected to nothing. A pairing
        finished here closes through the dialog's own mid-session path, which
        also runs the another-number check (connect.py, _ui_ready_event is
        set).
        """
        logging.info("[post_ui_init] STEP 1 — checking pairing now that WPPConnect answers...")
        pairing = self._pair_account_at_startup()
        if pairing == "switching":
            return False  # real_exit() already queued
        if pairing == "unpaired":
            logging.info("[post_ui_init] STEP 1 — connection dialog closed without pairing. Exiting application.")
            wx.CallAfter(self.real_exit)
            return False
        if pairing == "paired":
            # init_UI() shows this after a pairing made in __init__; this one
            # happened after it.
            wx.CallAfter(self._check_quick_tip)
        return True
