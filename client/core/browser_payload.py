"""Is the bundled Chromium actually complete enough to start?

WinZapp ships its own Chromium under ``api/.cache`` and every check that asks
"is the API set up?" looks only for the *executable*. That is not the same
question, and the gap is not theoretical — measured on a real install
(2026-09-10, WinZapp 1.1.0.2681alpha), where every session start died before a
browser existed::

    error: [<session>:browser] Failed to launch the browser process:  Code: 2147483651
    stderr:
    [0910/103543.323:ERROR:base\\i18n\\icu_util.cc:232] Invalid file descriptor to ICU data received.

``2147483651`` is ``0x80000003`` (STATUS_BREAKPOINT): Chromium aborted during
startup, before it could report anything of its own. The line above it says
why — it could not read ``icudtl.dat``, its internationalisation data, which
Chromium loads before almost anything else and cannot run without. The
executable was right there, so ``find_headless_shell()`` logged
``[headless-shell] Already installed: ...`` on every launch and nothing ever
looked again. An incomplete download, or an antivirus quarantining the file
(``icudtl.dat`` is a routine false positive), is therefore permanent.

Two things then made it much worse than "WhatsApp will not connect". The
session never reaching a real state is exactly what ``ProfileHealthTracker``
counts, so the profile recovery fired for a fault that has nothing to do with
the profile, spent both snapshot generations on it, and left the account
unpaired — with no way back, because pairing needs the same browser::

    10:26:07  STARTUP ... paired=True  login_store=files=113 bytes=208775977
    10:27:39  profile suspect - session started and died 3x without connecting
    10:27:40  profile restored from snapshot
    10:30:20  Chrome released the profile before the kill  login_store=absent
    10:35:41  MainWindow: WhatsApp connection not paired. Showing connection dialog...
    10:36:14  [display_qrcode_image] No QR reached the screen (no QR within 25s)

So this module answers one narrow question — *can this binary start at all?* —
and its callers use the answer twice: to refuse to accept a broken install as
"already installed", and to refuse to let the profile recovery run when the
browser is what is broken.

**Only ``icudtl.dat`` is required, and the narrowness is the point.** Both
flavours WinZapp may have on disk ship it (full ``chrome.exe`` and
``chrome-headless-shell``), it is the file the observed failure names, and
Chromium genuinely cannot start without it. Adding ``resources.pak`` and
friends would cover more corruption in theory and risks the far worse bug in
practice: a file that one flavour does not ship would condemn a perfectly good
browser, and the caller's response to "incomplete" is to delete it and download
it again. Rejecting a working install in a loop is worse than missing an exotic
half-corruption, so when in doubt this module says nothing is wrong.

A zero-length file counts as missing — that is what a quarantine stub looks
like. A file that is present, non-empty and *truncated* is not detectable here
without a checksum nobody publishes, so it stays out of scope; the launch
failure it produces is reported by the log, not by this.
"""

import os


#: Files the browser cannot start without, whichever flavour is installed.
#: See the module docstring before adding to this — the cost of a false
#: positive here is a delete-and-redownload loop on a healthy install.
REQUIRED_BROWSER_FILES = ("icudtl.dat",)


def payload_problem(binary_path):
    """Describe what is missing beside ``binary_path``, or None if it is fine.

    Returns a short human-readable string naming the first problem found, so
    the caller can put a real reason in the log instead of "something is
    wrong". An unreadable directory, a missing binary, or any OS error answers
    None: this is a *disqualifier*, and it must only ever disqualify on
    evidence. Failing to look is not evidence.
    """
    if not binary_path:
        return None
    try:
        if not os.path.isfile(binary_path):
            return None
        payload_dir = os.path.dirname(binary_path)
        for name in REQUIRED_BROWSER_FILES:
            path = os.path.join(payload_dir, name)
            if not os.path.isfile(path):
                return "%s is missing" % name
            if os.path.getsize(path) <= 0:
                return "%s is empty" % name
    except OSError:
        return None
    return None


def installed_version_dir(binary_path, cache_dir):
    """The versioned browser directory under ``cache_dir`` holding this binary.

    ``@puppeteer/browsers`` lays the cache out as
    ``<cache>/chrome/win64-<version>/chrome-win64/chrome.exe``, and re-running
    its installer does nothing while that version directory exists — so
    repairing a broken payload means removing the version directory, not just
    the file that went missing.

    Returns None unless the path really does sit under ``cache_dir``. That
    containment check is the whole safety of the delete the caller performs
    with this: nothing outside WinZapp's own browser cache may ever be named
    here, however odd the inputs get.
    """
    if not binary_path or not cache_dir:
        return None
    try:
        cache_root = os.path.realpath(cache_dir)
        payload_dir = os.path.realpath(os.path.dirname(binary_path))
        # ValueError on Windows when the two are on different drives, which is
        # itself the answer: not under the cache.
        relative = os.path.relpath(payload_dir, cache_root)
    except (OSError, ValueError):
        return None
    if relative.startswith(os.pardir) or os.path.isabs(relative):
        return None
    parts = [p for p in relative.split(os.sep) if p not in ("", os.curdir)]
    if len(parts) < 2:
        # <cache>/<product> or the cache itself. Neither is a version
        # directory, and removing either would take more than this install.
        return None
    return os.path.join(cache_root, parts[0], parts[1])
