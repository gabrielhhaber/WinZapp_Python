"""Keeping a restore point for the Chrome profile WhatsApp Web lives in.

WhatsApp's login is not in WinZapp's own storage. It is inside the Chrome
profile WPPConnect drives — `<global_dir>/api/userDataDir/<session>` — as an
IndexedDB (LevelDB) store. WPPConnect's own file token store, which exists to
carry credentials separately, is **empty on a real install even while a session
is healthy and connected**, so that profile is the sole carrier: damage it and
there is nothing to fall back on. The user re-pairs, which for a blind user
means the whole pairing flow again, and loses the local history that was tied
to the old session.

Damaging it is not rare. LevelDB is only consistent if the process that owns it
is allowed to finish writing, and a real `shutdown_audit.log` covering 159
launches holds seventeen runs that ended with no `_stop_wpp_server` line at all
— eleven of them overnight gaps of 7-12 hours, the shape of Windows ending the
session with WinZapp open. The last of those preceded exactly this failure: the
page loaded and even authenticated (the phone listed the linked device as
active) but wa-js never reached `WPP.isReady`, `injectApi()` timed out, and the
session cycled INITIALIZING -> CLOSED forever with the UI saying only
"offline".

So: keep a copy of the profile taken at a moment it is known to be consistent,
and put it back when the live one stops working.

**The snapshot is taken only after a clean close, and that is the whole design
rather than a detail.** The moment `_stop_wpp_server()` has POSTed
close-session, seen the session reach CLOSED, and watched Chrome release the
profile is the one moment WinZapp can prove the profile is both quiescent and
completely written. Copying a live profile would faithfully preserve a
half-written LevelDB — a restore point that restores the corruption. It also
means a snapshot only ever exists for an install that has shut down cleanly at
least once, which is exactly the population whose next dirty shutdown this
protects.

Everything here is filesystem-only and free of wx, so it is testable directly;
`main.py` owns the policy of when to call it.
"""

import json
import logging
import os
import shutil
import time


#: Directory name, under the same `api/` folder that holds `userDataDir/`, in
#: which snapshots are kept. Sitting beside the profiles rather than inside
#: them matters: anything inside `userDataDir/<session>/` is handed to Chrome,
#: which is free to walk, rewrite or delete what it finds there.
SNAPSHOT_DIR_NAME = "userDataDirSnapshots"

#: How long a snapshot may go without being refreshed before the next clean
#: shutdown takes a new one. A profile changes constantly (message history,
#: keys, media cache), so a very old restore point is a worse WhatsApp session
#: than a fresh pairing in some respects — but it still holds a *valid login*,
#: which is the thing being protected. A day is short enough to stay relevant
#: and long enough that a user who opens and closes WinZapp ten times in an
#: afternoon pays the copy once.
SNAPSHOT_MAX_AGE_SECONDS = 24 * 60 * 60

#: Hard cap on the copy, in seconds. Real profiles run to a couple of hundred
#: megabytes and this happens while the user is trying to close the app; a
#: snapshot is a nice-to-have and must never be the reason WinZapp appears to
#: hang on exit. Exceeding it abandons the attempt and leaves the previous
#: snapshot untouched.
SNAPSHOT_BUDGET_SECONDS = 25.0

#: Chrome's own single-instance markers. They are meaningless once the process
#: is gone and actively harmful inside a restored copy — a stale `SingletonLock`
#: is one of the things that makes a relaunch refuse the profile — so they are
#: never copied. `_kill_orphaned_chrome_for_session()` deletes the same set off
#: the live profile for the same reason.
_TRANSIENT_ENTRIES = frozenset({
    "lockfile", "SingletonLock", "SingletonCookie", "SingletonSocket",
})


def profile_dir(global_dir, session_name):
    """Where WPPConnect keeps this session's Chrome profile.

    Mirrors `_start_wpp_background()`'s WINZAPP_USER_DATA_DIR rather than
    `resource_path("api", ...)`: in a --onefile build the latter is an
    ephemeral extraction directory that is gone by the time any of this runs.
    """
    return os.path.join(global_dir, "api", "userDataDir", session_name)


#: The subtree WhatsApp Web keeps its login in. Fingerprinted rather than
#: read: the point is only whether it changed, and its contents are the user's
#: message history.
_LOGIN_STORE_RELPATH = os.path.join(
    "Default", "IndexedDB", "https_web.whatsapp.com_0.indexeddb.leveldb")


def _fingerprint_login_store(root):
    """The fingerprint of one profile root, or None when it has no login store.

    Split out of login_store_fingerprint() so the same reading can be taken of
    a SNAPSHOT rather than only of the live profile — see
    snapshot_matches_live_profile(), which is the difference between a restore
    that can help and one that provably cannot.
    """
    try:
        path = os.path.join(root, _LOGIN_STORE_RELPATH)
        newest = 0.0
        total = 0
        count = 0
        for entry in os.scandir(path):
            try:
                st = entry.stat()
            except OSError:
                continue
            if entry.is_file():
                count += 1
                total += st.st_size
                newest = max(newest, st.st_mtime)
        if not count:
            return None
        return "files=%d bytes=%d newest=%.0f" % (count, total, newest)
    except Exception:
        return None


#: How many rejected login-store fingerprints to remember per session. Small on
#: purpose: the point is to recognise the handful of states this account has
#: actually been logged out of, not to build a history. Oldest drops first.
_REJECTED_HISTORY = 8

_REJECTED_FILE = "rejected_profiles.json"


def _rejected_path(global_dir):
    return os.path.join(global_dir, "api", SNAPSHOT_DIR_NAME, _REJECTED_FILE)


def _read_rejected(global_dir):
    try:
        with open(_rejected_path(global_dir), "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return {k: [str(x) for x in v]
                for k, v in data.items() if isinstance(v, list)}
    except Exception:
        return {}


def note_profile_rejected(global_dir, session_name, fingerprint=None):
    """Remember that WhatsApp refused to restore a session from THIS profile
    state, so no later launch offers the same bytes back to it.

    The state is identified by its login-store fingerprint, the same reading
    `login_store_fingerprint()` already writes into `shutdown_audit.log` — so
    what is persisted here is exactly what a human reads off that file when
    diagnosing one of these by hand.

    Why it has to survive the launch: the in-launch check compares a snapshot
    against the profile currently on disk, which stops being the rejected one
    the moment a restore replaces it. Measured on a user's install
    (2026-09-10), the restore put an identical copy of the rejected state back
    and it was refused again eight seconds later; on the launch after that,
    the live profile was no longer evidence of anything, and only a persisted
    verdict can still say "we have tried these bytes, they do not work".

    Never raises. Being unable to record a verdict must not cost the recovery
    that is about to run.
    """
    try:
        if fingerprint is None:
            fingerprint = login_store_fingerprint(global_dir, session_name)
        if not fingerprint:
            return False
        data = _read_rejected(global_dir)
        seen = [f for f in data.get(session_name, []) if f != fingerprint]
        seen.append(fingerprint)
        data[session_name] = seen[-_REJECTED_HISTORY:]
        path = _rejected_path(global_dir)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
        return True
    except Exception:
        logging.debug("[profile-recovery] could not record a rejected profile",
                      exc_info=True)
        return False


def profile_state_was_rejected(global_dir, session_name, fingerprint):
    """Have we already offered these exact bytes to WhatsApp and been refused?

    False for an unknown or unreadable fingerprint: never let a missing record
    talk the caller out of a restore that might work.
    """
    if not fingerprint:
        return False
    try:
        return fingerprint in _read_rejected(global_dir).get(session_name, [])
    except Exception:
        return False


def snapshot_was_rejected(global_dir, session_name, prefer_previous=False):
    """Whether the saved generation holds a state already refused once."""
    source = (previous_snapshot_dir(global_dir, session_name) if prefer_previous
              else snapshot_dir(global_dir, session_name))
    return profile_state_was_rejected(
        global_dir, session_name, _fingerprint_login_store(source))


def clear_rejected_profiles(global_dir, session_name):
    """Forget this session's verdicts. Called when a session reaches CONNECTED:
    whatever was wrong is over, and a state that authenticates now must not be
    refused by a record from before it did."""
    try:
        data = _read_rejected(global_dir)
        if session_name not in data:
            return False
        data.pop(session_name, None)
        path = _rejected_path(global_dir)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def snapshot_matches_live_profile(global_dir, session_name, prefer_previous=False):
    """Would restoring this snapshot put back exactly what is already there?

    True means the restore is a no-op in content terms and cannot possibly
    change the outcome — the profile WhatsApp just rejected would be replaced
    by an identical copy of itself.

    Measured on a real install (2026-09-10), from one launch:

        10:52:45  Chrome released the profile   files=21 bytes=27793052 newest=1789048351
        10:52:46  profile snapshot refreshed
        10:53:46  STARTUP                       files=21 bytes=27793052 newest=1789048351
        10:53:56  Session Unpaired -> post_logout=1&logout_reason=0
        10:55:48  profile restored from snapshot
        10:55:56  Session Unpaired -> post_logout=1&logout_reason=0

    The two fingerprints are identical, so the "successful" restore restored
    the failure, and the second rejection landed eight seconds later with the
    same timing as the first. It also spent the launch's one recovery attempt:
    the generation ladder only climbs to `.prev` on the NEXT launch, so the
    user sat offline until they happened to restart.

    The comparison is safe because both sides are read the same way and
    `_copy_tree_bounded()`/`restore_snapshot()` copy with `shutil.copy2`, which
    preserves mtimes — so a faithful copy fingerprints identically, and any
    real difference in the login store shows up. `_TRANSIENT_ENTRIES` are
    excluded from snapshots but all live at the profile root, never inside the
    login store, so they cannot make a matching snapshot look different.

    Answers False whenever either side cannot be read: an unknown fingerprint
    must never talk the caller out of attempting a restore that might work.
    """
    live = _fingerprint_login_store(profile_dir(global_dir, session_name))
    if live is None:
        return False
    source = (previous_snapshot_dir(global_dir, session_name) if prefer_previous
              else snapshot_dir(global_dir, session_name))
    saved = _fingerprint_login_store(source)
    if saved is None:
        return False
    return saved == live


def login_store_fingerprint(global_dir, session_name):
    """A cheap "is this the same bytes as last time" reading of the profile.

    Exists to settle one question the logs cannot currently answer. Across one
    day on a real install, four clean shutdowns — close-session acknowledged,
    the session observed CLOSED, Chrome confirmed to have released the profile,
    5-6 s of genuine waiting in every one of them — produced two launches where
    WhatsApp Web logged itself out seven seconds into the page load, and two
    that were fine. Nothing in `shutdown_audit.log` distinguishes them, and
    `log.log` is truncated on the launch that would report it.

    Recorded at the end of a shutdown and again at the start of the next
    launch, this separates the two remaining explanations: a fingerprint that
    moved in between means something wrote to the profile after WinZapp let go
    of it, and one that is identical means the profile WinZapp left is exactly
    the one WhatsApp Web then rejected — which is a stale credential, not a
    lost write, and would rule out the whole shutdown path.

    Never raises: this is a diagnostic, and a shutdown must not die in one.
    The path build is inside the guard too — profile_dir() joins its arguments,
    so a None global_dir raises there rather than in the scan below.
    """
    try:
        root = profile_dir(global_dir, session_name)
    except Exception:
        return None
    return _fingerprint_login_store(root)


def snapshot_dir(global_dir, session_name):
    return os.path.join(global_dir, "api", SNAPSHOT_DIR_NAME, session_name)


def previous_snapshot_dir(global_dir, session_name):
    """The snapshot the last refresh replaced.

    Kept because a clean close is not proof that the profile it wrote will
    still authenticate. Measured across one day on a real install: four clean
    shutdowns, each with close-session acknowledged, the session observed
    CLOSED and Chrome confirmed to have released the profile — and two of the
    four were followed by a launch where WhatsApp Web logged itself out seven
    seconds into the page load. The two hard-killed runs that day both came
    back fine, which is the opposite of what the module docstring above
    predicts.

    So `capture_snapshot()`'s conditions can all hold and still write a
    restore point that does not work, and on that install the good one was
    22.7 h old against a 24 h refresh window: one more hour and the refresh
    would have overwritten the only profile that still authenticated. Keeping
    the generation it replaces costs one extra copy on disk and is the
    difference between a recoverable session and a re-pairing.
    """
    return snapshot_dir(global_dir, session_name) + ".prev"


def snapshot_age_seconds(global_dir, session_name, now=None):
    """Seconds since this session's snapshot was completed, or None if there
    is none. Read off the directory itself, which `capture_snapshot()` renames
    into place as its last step, so the timestamp always means "finished"."""
    path = snapshot_dir(global_dir, session_name)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    return max(0.0, (time.time() if now is None else now) - mtime)


def snapshot_is_fresh(global_dir, session_name, max_age=SNAPSHOT_MAX_AGE_SECONDS,
                      now=None):
    age = snapshot_age_seconds(global_dir, session_name, now=now)
    return age is not None and age < max_age


def _copy_tree_bounded(source, destination, deadline):
    """Copy `source` to `destination`, giving up if `deadline` passes.

    Written out rather than delegating to `shutil.copytree` because the budget
    has to be checked *during* the walk: a copytree of a 200 MB profile on a
    slow disk is a single uninterruptible call, and the whole point of the
    budget is that the user is waiting for the app to close.
    """
    for root, dirs, files in os.walk(source):
        if time.monotonic() > deadline:
            raise TimeoutError("snapshot budget exhausted")
        relative = os.path.relpath(root, source)
        target_root = (destination if relative == "."
                       else os.path.join(destination, relative))
        os.makedirs(target_root, exist_ok=True)
        for name in files:
            if name in _TRANSIENT_ENTRIES:
                continue
            if time.monotonic() > deadline:
                raise TimeoutError("snapshot budget exhausted")
            try:
                shutil.copy2(os.path.join(root, name),
                             os.path.join(target_root, name))
            except OSError:
                # One unreadable file must not lose the whole restore point.
                # Chrome leaves sockets and transient caches behind that copy
                # badly and that nothing needs; the login does not live in
                # them.
                continue


def _replace_directory(staged, final):
    """Move `staged` onto `final`, removing whatever was there.

    Deliberately not a rename over the top: Windows refuses to rename onto an
    existing directory, and a delete-then-rename has a window where neither
    exists. The old copy is moved aside first so that window is instead one
    where BOTH exist, and it is cleaned up afterwards — a crash mid-way leaves
    a `.old` directory to sweep, never a missing profile.
    """
    displaced = final + ".old"
    if os.path.exists(displaced):
        shutil.rmtree(displaced, ignore_errors=True)
    if os.path.exists(final):
        os.replace(final, displaced)
    os.makedirs(os.path.dirname(final), exist_ok=True)
    os.replace(staged, final)
    if os.path.exists(displaced):
        shutil.rmtree(displaced, ignore_errors=True)


def capture_snapshot(global_dir, session_name, budget=SNAPSHOT_BUDGET_SECONDS,
                     max_age=SNAPSHOT_MAX_AGE_SECONDS):
    """Take a restore point, if one is due. Returns True if one was written.

    **Only ever call this after a confirmed clean close** — close-session
    acknowledged, the session observed CLOSED, and Chrome no longer holding the
    profile. See the module docstring: a snapshot of a live profile preserves
    whatever half-written state it was in, which is a restore point that
    restores the corruption.

    Never raises. A failed snapshot is a missing nicety; a failed *shutdown* is
    the corruption this exists to protect against, so nothing here may put the
    caller's teardown at risk.
    """
    source = profile_dir(global_dir, session_name)
    if not os.path.isdir(source):
        return False
    if snapshot_is_fresh(global_dir, session_name, max_age=max_age):
        return False

    final = snapshot_dir(global_dir, session_name)
    staged = final + ".partial"
    deadline = time.monotonic() + budget
    try:
        shutil.rmtree(staged, ignore_errors=True)
        os.makedirs(staged, exist_ok=True)
        _copy_tree_bounded(source, staged, deadline)
        # Demote the snapshot being replaced rather than dropping it. See
        # previous_snapshot_dir(): a clean close is not proof that what it
        # wrote will authenticate on the next load, and the generation being
        # overwritten is the one already known to have worked. Everything above
        # happened in `staged`, so a failure before this point still leaves
        # both generations untouched.
        previous = previous_snapshot_dir(global_dir, session_name)
        if os.path.isdir(final):
            shutil.rmtree(previous, ignore_errors=True)
            try:
                os.replace(final, previous)
            except OSError as exc:
                # Not fatal: losing the older generation costs a fallback,
                # while refusing to refresh at all costs the restore point.
                logging.warning("[profile-snapshot] could not keep the previous "
                                "generation for %s: %s", session_name[:12], exc)
        _replace_directory(staged, final)
        logging.info("[profile-snapshot] restore point written for session %s",
                     session_name[:12])
        return True
    except Exception as exc:
        # Including TimeoutError. The previous snapshot, if any, is untouched:
        # everything above happens in `staged` until the final replace.
        logging.warning("[profile-snapshot] no restore point written for %s: %s",
                        session_name[:12], exc)
        shutil.rmtree(staged, ignore_errors=True)
        return False


def restore_snapshot(global_dir, session_name, prefer_previous=False):
    """Put the saved profile back. Returns True if the live profile was replaced.

    `prefer_previous` restores the generation before the newest one — the
    ladder for the case the newest snapshot is itself a profile that no longer
    authenticates. See previous_snapshot_dir() for why that is reachable from
    a shutdown that did everything right.

    The caller must have stopped Chrome first — restoring under a running
    browser would be overwriting a LevelDB while its owner holds it open, i.e.
    manufacturing the exact corruption this recovers from.

    The broken profile is moved to `<profile>.broken` rather than deleted. It
    is the only evidence of what went wrong, and deleting the one copy of a
    failure nobody has reproduced is how a bug survives another release.
    """
    source = (previous_snapshot_dir(global_dir, session_name) if prefer_previous
              else snapshot_dir(global_dir, session_name))
    if not os.path.isdir(source):
        return False
    live = profile_dir(global_dir, session_name)
    broken = live + ".broken"
    try:
        shutil.rmtree(broken, ignore_errors=True)
        if os.path.exists(live):
            os.replace(live, broken)
        staged = live + ".restoring"
        shutil.rmtree(staged, ignore_errors=True)
        shutil.copytree(source, staged)
        os.makedirs(os.path.dirname(live), exist_ok=True)
        os.replace(staged, live)
        logging.warning("[profile-restore] session %s restored from the last "
                        "clean-shutdown snapshot; the broken profile is kept "
                        "at %s", session_name[:12], broken)
        return True
    except Exception as exc:
        logging.error("[profile-restore] could not restore session %s: %s",
                      session_name[:12], exc)
        # Put the original back rather than leaving the account with neither.
        try:
            if not os.path.exists(live) and os.path.isdir(broken):
                os.replace(broken, live)
        except Exception:
            pass
        return False


def has_snapshot(global_dir, session_name, prefer_previous=False):
    return os.path.isdir(previous_snapshot_dir(global_dir, session_name)
                         if prefer_previous
                         else snapshot_dir(global_dir, session_name))


class ProfileHealthTracker:
    """Decides when a paired session's failure to start means a broken profile.

    The signature is: a **paired** session that has been observed trying to
    start, that has reached CLOSED that many times, and that has never once
    reported CONNECTED in between. That is the browser starting, failing to
    bring WhatsApp Web up, and being torn down — repeatedly.

    A plain network outage does not look like this: an established session
    reports CONNECTED, and any CONNECTED resets everything, so a single good
    connection clears whatever came before it.

    **What "observed trying to start" must not mean is "we caught a status
    reading that said INITIALIZING".** That was the original rule and it made
    the whole recovery unreachable in the field. Measured on a real broken
    install: `check_wa_connection_http()` polls every 30 s while one failed
    cycle takes ~60 s (start-session, browser up, wa-js never ready,
    `injectApi()` times out at 30 s, CLOSED), and the poll only ever landed on
    `disconnectedMobile` and `CLOSED`. INITIALIZING was seen exactly once, at
    launch, and the old code cleared its arming flag on every CLOSED — so the
    counter reached 1 and stayed there for the entire life of the process. The
    log carried twelve minutes of that loop and not one `[profile-health]`
    line, with a perfectly good snapshot sitting on disk. WhatsApp Web then
    logged *itself* out of the unusable profile (`post_logout=1`,
    `logout_reason=0`), and the user was left re-pairing an account whose
    phone still listed the device as linked — restoring that snapshot by hand
    brought the same login straight back, connected and syncing.

    So arming is latched until CONNECTED, and `note_session_start_requested()`
    arms it too: the health checker POSTing /start-session is direct evidence
    that a start is being attempted, and it does not depend on a poll landing
    inside a window narrower than the poll interval.

    Three cycles rather than one, because the first can be a slow machine and
    the second the wake-from-hibernate path that has its own recovery; a
    profile that is genuinely unreadable fails every time. At roughly a minute
    per cycle that fires about three minutes in — comfortably before the
    logged-out page starts pushing QR codes at the user, which on the measured
    install took seven.

    One case is deliberately accepted rather than excluded: a user who unlinks
    the device from their phone produces the same readings, and this will
    restore a snapshot whose credentials the server has already revoked. That
    costs one cycle — WhatsApp Web rejects it and asks to pair again, which is
    where they were going anyway.

    `_recover_suspect_profile()` latches, and only a session reporting
    CONNECTED hands that latch back. Do not read that as "a revoked snapshot
    can never restore twice", which is one step stronger than the code
    actually guarantees: the re-arm in `_note_status_for_profile_health()`
    reads the status *string* alone, and createSessionUtil.start() promotes a
    session to CONNECTED off its own state listener without the live
    isConnected() probe ever agreeing ("the event wins"). A revoked snapshot
    that gets that far does hand the latch back — the same seam
    websocket_client.py's `_REPAIR_DIALOG_CONFIRM_EVENTS` comment describes
    for the QR-flood counter, which needs the probe to agree and so is *not*
    given back by the same reading. The generation ladder does not close it
    either: it has two rungs, and that CONNECTED resets it to the newest one.

    So the real guarantee is a rate limit, not impossibility — before
    anything can be restored again, either the tracker counts
    FAILED_CYCLES_BEFORE_SUSPECT more failed start cycles at roughly a minute
    each, or a fresh flood of codes clears both of _handle_unattended_qr()'s
    gates (past the startup grace, _REPAIR_DIALOG_CONFIRM_EVENTS consecutive
    readings) while _auto_repair_dialog_shown is still False. The QR route is
    the faster of the two and does not consult this tracker at all — that is
    the seam the paragraph above describes, seen from the other side. Being
    wrong in that direction costs a minute; being wrong in the other
    direction is what this class was written for, and it cost a re-pairing.
    """

    #: Consecutive failed start cycles before the profile is suspect.
    FAILED_CYCLES_BEFORE_SUSPECT = 3

    def __init__(self, threshold=None):
        self.threshold = threshold or self.FAILED_CYCLES_BEFORE_SUSPECT
        self.failed_cycles = 0
        self._armed = False
        self._ever_connected = False

    def note_session_start_requested(self, paired=True):
        """The health checker just POSTed /start-session for this session.

        Arms the tracker without waiting for a poll to catch INITIALIZING —
        see the class docstring for why that catch cannot be relied on.
        """
        if paired:
            self._armed = True

    def note_status(self, status, paired=True):
        """Feed one status-session reading. Returns True the moment the profile
        should be treated as suspect — once per crossing, so a caller polling
        every 30s does not act on it repeatedly."""
        normalized = (status or "").upper()
        if normalized == "CONNECTED":
            self._ever_connected = True
            self._armed = False
            self.failed_cycles = 0
            return False
        if not paired:
            # Mid-pairing there is no profile worth preserving and no login to
            # lose, and the QR/code flow drives the session through these very
            # states on purpose.
            self._armed = False
            self.failed_cycles = 0
            return False
        if normalized == "INITIALIZING":
            self._armed = True
            return False
        if normalized == "CLOSED" and self._armed:
            # Deliberately stays armed. Clearing it here is the original bug:
            # the next cycle's INITIALIZING falls between two polls and the
            # count never advances again.
            self.failed_cycles += 1
            if self.failed_cycles == self.threshold:
                return True
        return False

    def ever_connected(self):
        """Whether this session reported CONNECTED at least once this run.

        Read by the snapshot side, not by the recovery side: a run that never
        connected must not be allowed to overwrite the restore point that
        would have rescued it. See _capture_profile_snapshot().
        """
        return self._ever_connected

    def reset(self):
        self.failed_cycles = 0
        self._armed = False
