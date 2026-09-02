"""Pure, wx-free connection-state decision logic (client/connection_state.py).

Extracted from MainWindow.check_wa_connection_http so the "is this unlinked
status a real logout, a still-resuming session, or a resume that has failed for
too long" decision is unit-testable without the whole wx/requests stack.

Background (the bug this fixes): on launch WPPConnect restores a saved session
through INITIALIZING → (transient) QRCODE/notLogged → inChat. The client polls
status over HTTP and used to treat a transient QRCODE as a logout, wiping the
local database of a session the server was busy logging back in (confirmed in a
real log: node reported 'inChat' the same second the client wiped). The rule
below encodes: a logout is only real once we've actually been connected THIS run
and then go unlinked; before any connect this run, an unlinked status means the
session is still resuming — never wipe — and only after a long resume timeout do
we surface the pairing dialog WITHOUT wiping data.
"""

from __future__ import annotations

# Decision outcomes.
ONLINE = "online"                 # status is a connected/active-good state
RESUMING = "resuming"             # unlinked, never connected yet — keep waiting
RESUME_FAILED = "resume_failed"   # unlinked too long while resuming — pair, NO wipe
LOGOUT = "logout"                 # confirmed logout after being connected — wipe

# Outcomes of MainWindow._still_linked_on_server()'s host-device probe, the last
# gate before any destructive wipe. Deliberately THREE values and not a bool:
# the probe goes out through the very same local auth middleware that may have
# just answered 401/403, so "the probe itself failed" is a routine outcome on
# the path that needs this gate most — and collapsing it into "not linked" is
# what turned a rotated local token into a full database wipe 60s later. Only
# LINK_PROBE_UNLINKED (a session that answered, and could not name our phone)
# may ever authorise a wipe; LINK_PROBE_UNKNOWN falls back to the pairing
# dialog with the data kept.
LINK_PROBE_LINKED = "linked"      # answered with our own phone number — NOT unlinked
LINK_PROBE_UNLINKED = "unlinked"  # answered, and it holds no linked phone
LINK_PROBE_UNKNOWN = "unknown"    # the probe never got an answer it could read

# WPPConnect exposes the same permanent unlink through different channels:
# statusFind uses disconnectedMobile/notLogged, while onStateChange and the
# REST status endpoint use UNPAIRED variants.
#
# `disconnectedMobile` is the loose one and is only safe HERE. WPPConnect
# documents it as "Client has disconnected to the mobile device" — the phone
# became unreachable, which is routine (no signal, dead battery, the machine
# asleep) and is not the same thing as the user unlinking the device;
# `notLogged` is the one that means "scan the QR code again". Listing it in
# this tuple is defensible only because every consumer of it goes through
# MainWindow._act_on_unlink_decision(), which demands several independent
# things before anything is wiped: never before the session has actually
# connected THIS run (classify_unlink_candidate()'s ever_connected split),
# _LOGOUT_CONFIRM_STRIKES consecutive readings at least
# STRIKE_MIN_INTERVAL_SECONDS apart, and _still_linked_on_server() positively
# answering LINK_PROBE_UNLINKED — a probe that merely failed (LINK_PROBE_UNKNOWN)
# lands on the pairing dialog with the data kept, never on a wipe.
#
# It is NOT safe on an unguarded path. websocket_client.on_wpp_status_find()
# treats a single one of these as a permanent logout and wipes credentials and
# the local database immediately — which is why createSessionUtil.ts
# deliberately does not emit 'status-find' over Socket.IO (see the comment
# there). Anything new that reads this tuple has to go through the guarded
# path too.
UNLINKED_STATES = (
    "notLogged",
    "QRCODE",
    "disconnectedMobile",
    "UNPAIRED",
    "UNPAIRED_IDLE",
)

# Minimum wall-clock gap between two counted unlinked strikes. The strike
# thresholds (_LOGOUT_CONFIRM_STRIKES, _RESUME_FAIL_STRIKES) were tuned for the
# ~30s health-check cadence, i.e. "N strikes ≈ N×30s of real time". But several
# code paths poll check_wa_connection_http() in tight loops (e.g. _run_sync's
# `for _ in range(25): sleep(0.2)`), and strikes count READINGS, not time — so a
# fast loop raced through 20 unlinked readings in ~6s and force-killed a session
# that WhatsApp had actually just logged back in (node log showed inChat the same
# second the client wiped). Gating strikes to at most one per this interval makes
# the thresholds mean real elapsed time again: kept just under the 30s health
# cadence so every ordinary health-check reading still counts (restoring
# RESUME_FAIL≈10min, LOGOUT≈2min), while a burst of sub-second readings in a
# tight loop collapses to a single strike.
STRIKE_MIN_INTERVAL_SECONDS = 20.0


def should_count_strike(now: float, last_strike_ts: float,
                        min_interval: float = STRIKE_MIN_INTERVAL_SECONDS) -> bool:
    """True if enough wall-clock time has passed since the last counted strike
    that this unlinked reading should count as a new one. A missing/zero
    last_strike_ts (first reading of a run) always counts."""
    if not last_strike_ts:
        return True
    return (now - last_strike_ts) >= min_interval


def is_zombie_session_after_resume(wa_connected: bool, status: str,
                                   host_reachable: bool) -> bool:
    """Decide whether a post-wake session is a 'zombie CONNECTED' worth an
    active restart.

    After a long hibernation WhatsApp Web inside Chrome drops its stream and
    does not rebuild it, yet WPPConnect keeps a stale 'CONNECTED' status — so
    the app shows offline forever until a manual restart. This is that case:

    * not actually connected (isConnected()==false → wa_connected False), AND
    * WPPConnect still reports the stale CONNECTED/open status, AND
    * the network is up (WhatsApp's host is reachable) — so it's a dead stream,
      not a plain 'no internet' outage (which must be left to recover itself).

    Any other status (CLOSED/DESTROYED auto-starts; QRCODE/notLogged is a real
    pairing need) is not a zombie.
    """
    if wa_connected:
        return False
    if status not in ("CONNECTED", "open"):
        return False
    return host_reachable


def is_wake_from_suspend(elapsed: float, expected_interval: float,
                         gap_threshold: float) -> bool:
    """True when a periodic sleep overran so far it can only mean the process
    was frozen across a system suspend/hibernate.

    The health-check loop sleeps ``expected_interval`` seconds each cycle; a
    cycle whose real elapsed time exceeds ``gap_threshold`` did not merely run
    slow — the machine was asleep. This is the wx-free, unit-testable core of
    the clock-gap wake detector (wx's EVT_POWER_RESUME is not delivered to a
    tray-resident window on Windows, so this, not the event, is what reliably
    fires post-wake recovery).
    """
    return elapsed > gap_threshold and gap_threshold >= expected_interval


def chrome_cmdline_owns_session(cmdline: str, session_name: str) -> bool:
    """True when a chrome.exe command line belongs to *this* account's WPPConnect
    browser — i.e. its ``--user-data-dir`` points at this session's profile.

    The match is deliberately narrow: it requires the session name to appear in
    a ``userDataDir`` path segment, so it can never match the user's own regular
    Chrome (different profile dir) or another WinZapp account's browser. This is
    the wx-free, unit-testable core of the post-suspend orphan-Chrome killer:
    after hibernation a suspended chrome.exe keeps a lock on its userDataDir, so
    WPPConnect's start-session cannot relaunch ("browser is already running")
    and the session hangs in INITIALIZING forever. Recovery must kill exactly
    that process before restarting — never anything else.
    """
    if not session_name or not cmdline:
        return False
    low = cmdline.lower()
    if "userdatadir" not in low:
        return False
    return session_name.lower() in low


# Attribute names on MainWindow that a wake-from-suspend must reset so the
# resume is classified like a fresh session start, never a mid-session drop.
# Kept here (next to classify_unlinked) so the "what counts as connection
# state" definition lives in one wx-free, unit-testable place.
_RESET_ZERO_ATTRS = (
    "_wa_http_fail_strikes",
    "_offline_probe_strikes",
    "_logout_strikes",
    "_resume_fail_strikes",
    # The host-device veto run (MainWindow._STILL_LINKED_VETO_LIMIT) counts
    # CONSECUTIVE vetoes, and a suspend/resume is as much a break in that run
    # as a healthy status reading is: the session on the other side of the
    # wake is a fresh attempt, not more of the same one.
    "_still_linked_vetoes",
)
_RESET_FALSE_ATTRS = (
    "_logout_handled",
    "_wa_connect_announced",
)


def reset_state_for_resume(obj, now: float) -> None:
    """Reset the connection/logout latches on ``obj`` so a wake-from-suspend
    is treated like a fresh session start, NOT a mid-session drop.

    The token-level twin of the never-wipe-on-resume database guard. After a
    long hibernation WhatsApp Web inside Chrome has disconnected, so WPPConnect
    reports QRCODE/notLogged for a while until the saved profile re-attaches.
    classify_unlinked() escalates to a *logout* (dropping the token and the
    paired flag) only when we were connected THIS run — the
    ``_wa_connect_announced`` latch. Across a suspend that latch is still True
    from before the machine slept, so a transient post-wake QRCODE looked like
    "we were online, now logged out" and, after the logout-confirm strikes,
    wiped the token of every account that did not re-attach instantly.

    Clearing the latch (plus the strike tallies and the one-shot logout latch,
    and re-arming the startup grace window via ``_wa_startup_time``) makes the
    resume behave like launch: an unlinked reading is RESUMING, and only a
    genuinely dead session eventually surfaces the pairing dialog via
    RESUME_FAILED — which never wipes.

    Pure/wx-free (mutates a plain object), so it is unit-testable without the
    whole wx/requests stack.
    """
    for attr in _RESET_ZERO_ATTRS:
        setattr(obj, attr, 0)
    for attr in _RESET_FALSE_ATTRS:
        setattr(obj, attr, False)
    obj._wa_startup_time = now
    obj._last_strike_ts = 0.0



def classify_unlink_candidate(
    *,
    ever_connected: bool,
    logout_strikes: int,
    resume_strikes: int,
    logout_confirm_strikes: int,
    resume_fail_strikes: int,
) -> str:
    """The strike/timing decision shared by every "this might be an unlink"
    signal, once the caller has already decided the signal itself counts
    (a WPPConnect unlinked status string, or a local HTTP 401/403 — see
    classify_unlinked() and MainWindow.check_wa_connection_http()).

    Args mirror MainWindow state at the time of the reading:
      ever_connected    — have we announced a live connection THIS run?
      logout_strikes    — consecutive unlink-candidate readings AFTER being connected.
      resume_strikes    — consecutive unlink-candidate readings while never connected.
      *_strikes thresholds — the confirm limits.

    Returns one of RESUMING / RESUME_FAILED / LOGOUT. Callers pass the strike
    counts they will have AFTER incrementing for this reading, so the
    thresholds are simple >= comparisons.
    """
    if not ever_connected:
        # Session still restoring from its saved profile — a failed resume is
        # NOT proof of an unlink, so we never wipe. Only after a long timeout do
        # we surface the pairing dialog (without wiping).
        if resume_strikes >= resume_fail_strikes:
            return RESUME_FAILED
        return RESUMING
    # We were connected this run and now read unlinked → a real logout, once
    # confirmed by enough consecutive readings.
    if logout_strikes >= logout_confirm_strikes:
        return LOGOUT
    return RESUMING  # connected before, brief blip — keep waiting (no wipe)


# Why check_wa_connection_http() may NOT answer a CLOSED/DESTROYED/'' status
# with /start-session. A CLOSED reading is normally an invitation to bring the
# session back, and three of these four guards exist because something else
# already owns the browser at that moment; the fourth (a halted unattended QR
# flood) exists because bringing it back is the whole thing being prevented.
# The strings are the reason logged by the caller, one per guard, so the log
# still says which of the four fired.
AUTO_START_BLOCKED_PAIRING_DIALOG = "pairing dialog is active"
AUTO_START_BLOCKED_QR_FLOOD = (
    "session halted after an unattended QR flood; waiting for the user to re-pair"
)
AUTO_START_BLOCKED_RECOVERY_RESTART = "recovery restart sequence in progress owns it"
AUTO_START_BLOCKED_SELF_INFLICTED = "our own close-session teardown is in flight"


def auto_start_block_reason(*, pairing_dialog_active: bool,
                            qr_flood_halted: bool,
                            recovery_restart_active: bool,
                            self_inflicted_teardown: bool) -> str | None:
    """Return the reason /start-session must be skipped for a CLOSED status, or
    None when starting a fresh session is the right answer.

    Extracted from MainWindow.check_wa_connection_http()'s CLOSED branch — a
    four-way condition buried in a method that cannot be driven end to end in a
    test (HTTP, wx, half a dozen other subsystems), while each guard is a
    one-account-losing-or-not decision:

    * pairing_dialog_active — the pairing flow manages its own session; starting
      one here spawns a duplicate Chrome alongside it. (Unreachable in practice
      now that check_wa_connection_http() returns early while the dialog is up,
      kept as a defensive fallback.)
    * qr_flood_halted — this CLOSED is MainWindow._halt_unattended_qr_session()'s
      own doing. Restarting is exactly what that call exists to prevent: the
      session comes back and resumes asking WhatsApp for a code every ~30s that
      nobody can scan, which is what got an account banned. Only re-pairing or a
      genuine reconnect clears the latch.
    * recovery_restart_active — an active close/kill/start sequence owns the
      browser; the CLOSED we see is likely ITS close-session in flight, and
      racing it can spawn a duplicate Chrome / detached frame.
    * self_inflicted_teardown — the expected result of our own close-session
      (shutdown, WPPConnect update, session restart): starting here would revive
      the browser moments before taskkill force-kills it.

    Order matters only for which reason gets logged — any one of them blocks.
    """
    if pairing_dialog_active:
        return AUTO_START_BLOCKED_PAIRING_DIALOG
    if qr_flood_halted:
        return AUTO_START_BLOCKED_QR_FLOOD
    if recovery_restart_active:
        return AUTO_START_BLOCKED_RECOVERY_RESTART
    if self_inflicted_teardown:
        return AUTO_START_BLOCKED_SELF_INFLICTED
    return None


def classify_unlinked(
    status: str,
    *,
    ever_connected: bool,
    logout_strikes: int,
    resume_strikes: int,
    logout_confirm_strikes: int,
    resume_fail_strikes: int,
) -> str:
    """Classify a WPPConnect status reading into an action.

    status — the WPPConnect status string just read. Only an unlinked state
    (UNLINKED_STATES) is meaningful here; anything else is ONLINE (the caller
    resets both strike counters). See classify_unlink_candidate() for what
    happens once a reading counts.
    """
    if status not in UNLINKED_STATES:
        return ONLINE
    return classify_unlink_candidate(
        ever_connected=ever_connected,
        logout_strikes=logout_strikes,
        resume_strikes=resume_strikes,
        logout_confirm_strikes=logout_confirm_strikes,
        resume_fail_strikes=resume_fail_strikes,
    )


def try_begin_resume_recovery(obj, lock) -> bool:
    """Atomic compare-and-set that makes wake-from-suspend recovery
    single-flight. Returns True if the caller acquired the recovery slot (and
    must call end_resume_recovery when done), False if a recovery is already in
    progress and this trigger should be dropped.

    The two wake triggers — EVT_POWER_RESUME (its own thread) and the
    health-check clock-gap detector — both fire for the SAME wake a few seconds
    apart. Two overlapping _recover_from_suspend runs interleave their
    close/kill-orphan/start-session steps, and the later run's kill-orphan sweep
    (chrome_cmdline_owns_session matches by session NAME only) kills the fresh
    Chrome the earlier run's start-session just spawned — hanging the session in
    INITIALIZING or bouncing it to QRCODE/unpaired. Observed live after an ~8h
    hibernation across 3 accounts: same PIDs killed twice, "start-session sent"
    logged twice, only 1 of 3 recovered cleanly.

    Pure/wx-free (a plain object flag + a caller-supplied lock), so the
    single-flight decision is unit-testable without the whole wx stack.
    """
    with lock:
        if getattr(obj, "_resume_recovery_active", False):
            return False
        obj._resume_recovery_active = True
        return True


def end_resume_recovery(obj, lock) -> None:
    """Release the recovery slot taken by try_begin_resume_recovery so a LATER
    wake can recover again. Must run in a finally so a failed recovery does not
    permanently wedge the app offline."""
    with lock:
        obj._resume_recovery_active = False


# Status strings WPPConnect reports once a session's browser has fully shut
# down after a close-session: the WhatsApp Web page is gone and its auth state
# has been flushed to userDataDir. An empty string is a session no longer known
# to the server (also closed).
_CLOSED_AFTER_FLUSH_STATES = ("CLOSED", "DESTROYED", "")


def session_closed_after_flush(status: str) -> bool:
    """True once WPPConnect has finished its own teardown of the session.

    This is HALF the shutdown gate, not the whole one. It says the server let
    go of the session; it does NOT say Chrome has finished writing the profile.
    Only `MainWindow.wait_for_profile_release()` — which watches for the
    chrome.exe owning userDataDir/<session> to actually exit — means that, and
    `_stop_wpp_server()` waits on both, in that order.

    Getting this wrong is what corrupts a pairing. `taskkill /F` landing before
    the profile is released cuts the leveldb mid-write, and the account comes
    back to a pairing screen ("Session Unpaired") on the next launch while the
    phone still lists the device under Linked Devices.

    The reason this predicate can be trusted at all is a WinZapp patch in
    closeSession (api_patches/src/controller/sessionController.ts): the
    placeholder it parks in clientsArray during the close is 'CLOSING', not
    `{status: null}`. Upstream's null makes getSessionState answer 'CLOSED'
    immediately — before `await client.close()` has done anything — so this
    returned True on the first poll, ~0.1s in, on every shutdown ever audited.
    'CLOSING' is deliberately absent from the tuple below so the poll waits for
    the real transition.

    Pure/wx-free so the decision is unit-testable without the requests/wx stack.
    """
    return (status or "") in _CLOSED_AFTER_FLUSH_STATES


# A wake-recovery restart is only a SUCCESS when the session reaches a truly
# connected state. QRCODE/UNPAIRED/notLogged means "waiting for the user" (a
# real pairing need handled elsewhere — must NOT trigger a restart loop), and
# CLOSED/DESTROYED/INITIALIZING/'' are NOT success. This is intentionally
# narrower than "left INITIALIZING": the old `settled = status != INITIALIZING`
# wrongly treated QRCODE and even CLOSED as done (GPT r2 #5).
_RECOVERY_CONNECTED_STATES = ("CONNECTED", "open")
# States that mean the session is waiting on the human, not on us — recovery
# must stop (not loop restarts) but it is NOT a connection success either.
_RECOVERY_USER_ACTION_STATES = ("QRCODE", "UNPAIRED", "UNPAIRED_IDLE", "notLogged", "PHONECODE")


def recovery_connected(status: str) -> bool:
    """True once a post-wake restart reached a genuinely connected state.
    Only CONNECTED/open count — the sole signal worth calling recovery a success."""
    return (status or "") in _RECOVERY_CONNECTED_STATES


def recovery_needs_user_action(status: str) -> bool:
    """True when the session is parked waiting for the user (QR/pairing). Recovery
    should STOP restarting and let the pairing UI take over — never loop here."""
    return (status or "") in _RECOVERY_USER_ACTION_STATES


def recovery_should_stop(status: str) -> bool:
    """Stop the recovery attempt loop on either a real success OR a user-action
    state. Keep going only while still churning (INITIALIZING/'' /transient)."""
    return recovery_connected(status) or recovery_needs_user_action(status)


def initializing_restart_due(first_seen_monotonic: float, now_monotonic: float,
                             grace_seconds: float) -> bool:
    """Decide whether a session that has been stuck in INITIALIZING warrants a
    (single) force-restart, based on ELAPSED TIME WITHOUT PROGRESS rather than a
    raw probe count (GPT r2 #1).

    After the WAPI-race fix, a plain INITIALIZING right after wake is usually the
    session coming up on its own (onStateChange promotes it to CONNECTED without
    our help). Only a session that stays INITIALIZING with no state change for a
    long grace window is a genuine hang worth restarting. Callers reset
    ``first_seen_monotonic`` whenever the status changes, so any progress
    restarts the clock.
    """
    return (now_monotonic - first_seen_monotonic) >= grace_seconds


def is_progress_status(status: str) -> bool:
    """True if ``status`` is a real, readable state that counts as progress for
    the no-progress INITIALIZING clock. An empty/unreadable status ('') is a
    failed REST probe, NOT progress — treating it as progress would let flaky
    probes reset the clock forever and mask a genuine hang (GPT r3 #8)."""
    return bool(status) and status != "INITIALIZING"


# A wake-recovery restart has "settled" once the session leaves INITIALIZING for
# ANY resolved state: CONNECTED/open (recovered), or QRCODE/notLogged (a real
# pairing need handled elsewhere). Staying in INITIALIZING (or an unreadable
# empty status) means the restart landed on a hibernation-detached browser
# frame and hung — the caller retries a full close/start cycle.
_RECOVERY_UNSETTLED_STATES = ("INITIALIZING", "")


def recovery_settled(status: str) -> bool:
    """True if a post-wake restart reached a resolved state (worth stopping the
    retry loop); False while still INITIALIZING/unreadable (retry).

    DEPRECATED for the restart-success decision — prefer recovery_connected()
    (success) / recovery_should_stop() (stop looping). Kept for the shutdown
    flush gate wiring that still imports it.
    """
    return (status or "") not in _RECOVERY_UNSETTLED_STATES


def _valid_session_name(name: str) -> bool:
    """True only for a well-formed WPPConnect session name safe to use in a
    filesystem path. Names are random 32-hex-ish tokens; reject anything with
    separators, drive prefixes, dots-only, or NTFS stream markers so a bad
    sessions.json entry can never be turned into a path outside userDataDir
    (GPT r1 #3). Conservative allowlist: letters, digits, dash, underscore."""
    import re
    if not name or not isinstance(name, str):
        return False
    if name in (".", ".."):
        return False
    return re.fullmatch(r"[A-Za-z0-9_-]{1,128}", name) is not None


def safe_session_dir_to_delete(udd_root: str, name: str):
    """Return the absolute userDataDir path for ``name`` under ``udd_root`` ONLY
    if it is safe to delete, else None.

    Guards a destructive rmtree against a malicious/garbage session name (one
    containing '/', '..', absolute paths, drive prefixes, NTFS streams, etc.):
    the name must pass _valid_session_name AND the resolved path's parent must be
    exactly ``udd_root`` (case-insensitive on Windows) and its basename must
    equal ``name`` and not be the root itself. Pure/wx-free so the safety check
    is unit testable without touching the filesystem or requests/wx (GPT r1 #3/#d).
    """
    import os
    if not name or not udd_root or not _valid_session_name(name):
        return None
    root = os.path.abspath(udd_root)
    target = os.path.abspath(os.path.join(root, name))
    if target == root:
        return None  # never the root itself
    parent = os.path.dirname(target)
    same_parent = (os.path.normcase(parent) == os.path.normcase(root))
    if os.path.basename(target) == name and same_parent:
        return target
    return None
