"""Pure helpers for the local API/Node runtime setup (legacy state
migration, npm health marker, profile restore choice).

Moved verbatim out of main.py; main.py re-exports every name.
"""

import ctypes
import logging
import os
import shutil
import subprocess


# Marker file dropped in the new, persistent api/ root once the one-time move
# below has run, so the scan never repeats (and never fights a second
# account's process for the same folders on every launch).
LEGACY_API_STATE_MARKER = ".migrated_from_install_dir"

# The two folders WPPConnect writes a paired session into: the Chrome
# profile - the REAL credential store - and the REST-level token file.
LEGACY_API_STATE_DIRS = ("userDataDir", "tokens")


def shorten_windows_path(path: str) -> str:
    """Return the Windows 8.3 short form of an existing directory, or `path`
    unchanged when that is not available (non-Windows, path missing, 8.3
    generation disabled on the volume, or the API failing for any reason).

    This exists for exactly one caller: the Chrome profile root handed to
    WPPConnect as WINZAPP_USER_DATA_DIR. Chrome builds deep paths inside a
    profile, and the deepest of them are its Service Worker CacheStorage
    entries — two 32-character hashed directory names plus `index-dir\\
    the-real-index`, about 127 characters below the profile root, on top of
    the profile's own 32-character session name.

    Measured on the install this was found on:

        <global_dir>/api/userDataDir/<session>   profile root 135  -> 262  OVER
        8.3-shortened equivalent                 profile root  92  -> 219  ok

    MAX_PATH is 260. Over it, Chrome cannot create the CacheStorage entries,
    WhatsApp Web never gets the persistent storage bucket it needs, and it
    responds by logging ITSELF out: the page navigates to
    `/?post_logout=1&logout_reason=0`, wa-js is destroyed with it, and the
    session loops that roughly every 10s until WPPConnect force-kills it at
    notLogged. Neither the QR nor the pairing code is ever produced, and
    nothing in any log says "path too long" — the only visible symptom is the
    navigation itself.

    This was found by comparing two installs that differed in nothing but
    this path. The working one passed a cwd-RELATIVE './userDataDir/', which
    Chrome resolved against a working directory Windows had already handed
    back in 8.3 form (the install lives under a directory name containing a
    space), so it was accidentally 31 characters shorter and stayed under the
    limit. Its profile still *reports* a 262-character path when listed,
    because Windows reports the long form — which is why measuring the
    directory after the fact says the opposite of what Chrome experienced,
    and cost a wrong "MAX_PATH is ruled out" along the way.

    Shortening is best-effort on purpose. 8.3 generation can be turned off
    per volume (`fsutil 8dot3name`), in which case this returns the long path
    and the caller is no worse off than before.
    """
    if os.name != "nt" or not path:
        return path
    try:
        import ctypes
        from ctypes import wintypes

        _GetShortPathNameW = ctypes.windll.kernel32.GetShortPathNameW
        _GetShortPathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        _GetShortPathNameW.restype = wintypes.DWORD

        needed = _GetShortPathNameW(path, None, 0)
        if not needed:
            return path
        buf = ctypes.create_unicode_buffer(needed)
        if not _GetShortPathNameW(path, buf, needed):
            return path
        short = buf.value or path
        if short != path:
            logging.info("[paths] shortened %s -> %s (%d -> %d chars)",
                         path, short, len(path), len(short))
        return short
    except Exception:
        logging.exception("[paths] could not shorten %s — using it as-is", path)
        return path


def migrate_legacy_api_state(legacy_api_dir: str, persistent_api_dir: str) -> list:
    """Move a pre-existing install's WhatsApp session state from the old,
    cwd-relative location into the new persistent one. Returns what moved.

    Until WINZAPP_USER_DATA_DIR/WINZAPP_TOKEN_STORE_DIR existed, both folders
    were written relative to the Node process's cwd (``resource_path("api")``
    - the install dir in a --onedir build, which is what every released
    installer produces). Pointing them at ``<global_dir>/api`` fixes --onefile
    losing them on every launch, but on its own it also aims every ALREADY
    PAIRED install at an empty folder: Chrome starts on a virgin profile, the
    token store reads nothing, and WhatsApp correctly asks for a fresh QR.
    That is the exact "closed WinZapp, relaunched, told the device was
    disconnected" report the new path exists to fix, so shipping the move
    without this would deliver the bug as its own fix, once, to everybody.

    Deliberately conservative - this runs against a real paired session:
      * never overwrites anything already at the destination (a folder there
        means this install is already using the new location);
      * moves per folder, so a half-done previous attempt still completes;
      * writes the marker only after a successful pass, but writes it even
        when nothing needed moving, so a fresh install stops scanning;
      * a failure is logged and swallowed - the caller must still be able to
        start Node (the user re-pairs, which is no worse than not migrating).

    Must be called BEFORE Node is spawned: moving a userDataDir out from
    under a live Chrome would corrupt exactly what this is protecting.
    """
    moved = []
    if not (legacy_api_dir and persistent_api_dir):
        return moved
    if os.path.abspath(legacy_api_dir) == os.path.abspath(persistent_api_dir):
        return moved  # nothing to do: same folder (dev checkout edge case)
    marker = os.path.join(persistent_api_dir, LEGACY_API_STATE_MARKER)
    if os.path.exists(marker):
        return moved
    for name in LEGACY_API_STATE_DIRS:
        src = os.path.join(legacy_api_dir, name)
        dst = os.path.join(persistent_api_dir, name)
        try:
            if not os.path.isdir(src):
                continue
            if os.path.exists(dst):
                continue
            os.makedirs(persistent_api_dir, exist_ok=True)
            shutil.move(src, dst)
            moved.append(name)
            logging.warning("[api-migrate] moved %s -> %s", src, dst)
        except Exception:
            logging.exception("[api-migrate] could not move %s -> %s", src, dst)
            return moved  # leave the marker unwritten so the next launch retries
    try:
        os.makedirs(persistent_api_dir, exist_ok=True)
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write(legacy_api_dir)
    except Exception:
        logging.exception("[api-migrate] could not write the migration marker")
    return moved


# Written beside node.exe, inside client/node/, so it is thrown away together
# with the runtime it describes (NodeDownloadDialog swaps the whole folder).
NPM_HEALTH_MARKER_NAME = ".winzapp-npm-ok"


def npm_health_recorded(marker_path: str, node_version: str) -> bool:
    """Whether *this exact* portable Node.js build already passed the npm probe.

    Booting npm to ask it for its own help text costs 1-2 seconds cold — far
    more with an antivirus inspecting node.exe — and it runs on the critical
    path of every launch, in a file that instruments [STARTUP_TIMING]. The
    answer only changes when the runtime itself changes, so it is recorded
    once and keyed by version. A missing, unreadable, empty or
    differently-versioned marker means "not answered yet", never "unhealthy".

    ValueError covers the "unreadable" half OSError misses: a truncated or
    half-written marker raises UnicodeDecodeError, a ValueError subclass. The
    call site is outside any local try block, so that would climb all the way
    to __init__'s blanket handler and skip ensure_wpp_version() /
    ensure_wpp_running() — the app opens, Node never starts, nothing is said.
    """
    if not node_version:
        return False
    try:
        with open(marker_path, "r", encoding="utf-8") as fh:
            return fh.read().strip() == node_version
    except (OSError, ValueError):
        return False


def record_npm_health(marker_path: str, node_version: str) -> None:
    """Remember a passing npm probe.

    Best effort: an install directory that cannot be written to just re-probes
    on the next launch, which is slow but never wrong.
    """
    try:
        with open(marker_path, "w", encoding="utf-8") as fh:
            fh.write(node_version)
    except OSError as exc:
        logging.warning("[node] Could not record the npm health marker: %s", exc)


def pick_restore_generation(profile_recovery, global_dir, session_name,
                             generation):
    """Which snapshot generation a restore would put back, and whether it can.

    Returns ``(prefer_previous, verdict, from_ladder)``. Reads the disk and
    nothing else — no latch, no counter, no announcement — so it can be
    asked before a recovery is committed to (MainWindow._profile_restore_worth_trying)
    as well as by the recovery itself, and both get the same answer.

    verdict is one of:
      "ok"       — restore the generation prefer_previous names;
      "climbed"  — the ladder's choice holds a refused state, `.prev` does
                   not, so prefer_previous is now True;
      "refused"  — every generation there is holds a refused state;
      "missing"  — no snapshot of the chosen generation exists.

    Which generation, first. A restore that did not hold means the newest
    snapshot is itself a profile that no longer authenticates — reachable
    from a shutdown that did everything right, see previous_snapshot_dir()
    — so the next launch climbs to the one before it rather than restoring
    the same failure again. `generation` is that persisted ladder
    (_profile_recovery_generation(), cleared the moment a session reports
    CONNECTED); from_ladder says it was what chose `.prev`.

    Then whether it can help. A snapshot identical to the profile that was
    just rejected cannot, and restoring it is worse than doing nothing: it
    reports success, spends the launch's one recovery attempt, and leaves
    the user offline until they happen to restart — because the ladder
    only climbs on the NEXT launch. This is not hypothetical. Measured on a
    real install (2026-09-10):

      10:52:45  Chrome released the profile  files=21 bytes=27793052 newest=1789048351
      10:52:46  profile snapshot refreshed
      10:53:46  STARTUP                      files=21 bytes=27793052 newest=1789048351
      10:53:56  Session Unpaired -> post_logout=1&logout_reason=0
      10:55:48  profile restored from snapshot
      10:55:56  Session Unpaired -> post_logout=1&logout_reason=0

    Byte-identical fingerprints, and the restored profile was rejected on
    the same 7.5 s timing as the one it replaced. The run that produced
    that snapshot HAD reported CONNECTED, which is capture_snapshot()'s
    gate — so "the session connected" is not evidence that the state it
    leaves behind will be accepted next time, exactly as
    previous_snapshot_dir() already says. Climbing happens here rather than
    next launch, and only when `.prev` is genuinely different: a `.prev`
    that also matches is no better, and "refused" is the honest answer.
    """
    prefer_previous = generation >= 1
    if prefer_previous and not profile_recovery.has_snapshot(
            global_dir, session_name, prefer_previous=True):
        prefer_previous = False
    from_ladder = prefer_previous

    def _known_bad(previous):
        # Would restoring this generation offer WhatsApp bytes it has
        # already refused? Two readings of the same question: identical to
        # what is on disk right now (this launch), or matching a
        # fingerprint an earlier launch recorded as rejected.
        return (profile_recovery.snapshot_matches_live_profile(
                    global_dir, session_name, prefer_previous=previous)
                or profile_recovery.snapshot_was_rejected(
                    global_dir, session_name, prefer_previous=previous))

    if _known_bad(prefer_previous):
        if (not prefer_previous
                and profile_recovery.has_snapshot(global_dir, session_name,
                                                  prefer_previous=True)
                and not _known_bad(True)):
            return True, "climbed", from_ladder
        return prefer_previous, "refused", from_ladder
    if not profile_recovery.has_snapshot(global_dir, session_name,
                                         prefer_previous=prefer_previous):
        return prefer_previous, "missing", from_ladder
    return prefer_previous, "ok", from_ladder


def node_runtime_needs_download(node_exe, npm_cli, marker_path):
    """Whether the portable Node.js runtime has to be replaced wholesale.

    Returns ``(needs_download, installed_version)``. Two separate faults are
    checked, because either one makes `npm install` fail much later and far
    less legibly: a node.exe older than the homologated build (npm only
    *warns* on an engines mismatch, then runtime features break), and a
    versioned node.exe sitting on a broken npm tree.

    The npm half is deliberately asymmetric. A probe that answers is
    believed in both directions; a probe that never answers changes nothing
    and keeps the installed runtime. A slow first launch after boot — cold
    file cache, an antivirus inspecting node.exe — used to be swallowed by a
    blanket ``except Exception`` that then reported "unhealthy", so a
    perfectly good Node.js was thrown away and re-downloaded behind a dialog.
    The marker simply stays unwritten and the next launch asks again.
    """
    if not os.path.isfile(node_exe):
        return True, ""

    installed_version = ""
    try:
        from node_download_config import NODE_VERSION
        from packaging.version import Version
        probe = subprocess.run(
            [node_exe, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        installed_version = probe.stdout.strip().lstrip("vV")
        if probe.returncode != 0 or Version(installed_version) < Version(NODE_VERSION):
            return True, installed_version
    except Exception as exc:
        logging.warning(
            "[ensure_api_modules_installed] Could not validate Node.js version: %s",
            exc,
        )
        return True, installed_version

    # Booting npm to ask for its own help text costs 1-2 s on the UI thread,
    # so the verdict is remembered beside node.exe and only re-asked when the
    # runtime itself changes.
    if npm_health_recorded(marker_path, installed_version):
        return False, installed_version

    needs_download = False
    if not os.path.isfile(npm_cli):
        needs_download = True
    else:
        try:
            npm_probe = subprocess.run(
                [node_exe, npm_cli, "install", "--help"],
                capture_output=True,
                text=True,
                timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logging.warning(
                "[ensure_api_modules_installed] npm health probe did not "
                "finish (%s) — keeping the installed runtime.", exc,
            )
        else:
            needs_download = npm_probe.returncode != 0
            if not needs_download:
                record_npm_health(marker_path, installed_version)
    if needs_download:
        logging.warning(
            "[ensure_api_modules_installed] Portable npm is missing "
            "or unhealthy; replacing the complete Node.js runtime."
        )
    return needs_download, installed_version


def _looks_like_json_response(response) -> bool:
    """Did the server answer with the historical ``{base64, mimetype}`` JSON?

    Read off Content-Type rather than by sniffing the body: sniffing means
    touching the body, and the whole point of the binary path is that the body
    may be hundreds of megabytes that must be read exactly once.

    Answers True when the header is missing or unreadable — an unknown shape is
    treated as the old one, so a server that says nothing keeps the behaviour
    it has always had instead of having raw JSON written to disk as if it were
    a file.
    """
    try:
        content_type = (response.headers.get("Content-Type") or "").lower()
    except Exception:
        return True
    if not content_type:
        return True
    return "json" in content_type
