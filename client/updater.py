"""
Auto-updater for WinZapp.

Flow:
  1. UpdateChecker runs in a background thread at startup (if updates_enabled).
  2. If a newer version is found, show UpdateDialog on the main thread.
  3. User clicks "Sim" -> UpdateProgressDialog downloads the ZIP then installs.
  4. User clicks "Nao" -> retry in 3 hours.
  5. User clicks "Quais as novidades?" -> WhatsNewDialog shows changelog.
  6. After install: batch script waits for our PID, copies files, restarts.
"""

import hashlib
import os
import re
import sys
import time
import zipfile
import tempfile
import threading
import logging
import ctypes
import subprocess
import requests
import wx

from app_paths import _outer_exe_dir, _is_frozen, resource_path, log_path
from core.wpp_runtime import homologated_wpp_tag
from config import GITHUB_API_LATEST_RELEASE, GITHUB_API_LATEST_STABLE_RELEASE
from version import __version__


def _find_sha256sums_asset(assets: list) -> str:
    """Return the browser_download_url of a SHA256SUMS.txt asset in a GitHub
    release's asset list, or "" if the release predates this check (older
    releases published before CI started generating one)."""
    for asset in assets:
        if (asset.get("name") or "").lower() == "sha256sums.txt":
            return asset.get("browser_download_url", "")
    return ""


def _verify_sha256sums(file_path: str, filename: str, sha256sums_url: str) -> "tuple[bool, str]":
    """Verify file_path's SHA256 against the checksum manifest published
    alongside the GitHub release (see .github/workflows/release.yml's
    "Generate SHA256SUMS.txt" step). Returns (ok, detail).

    Fails OPEN (ok=True) only when sha256sums_url itself is empty — i.e. the
    release predates this feature and never published a manifest at all;
    there is nothing to compare against, so refusing to update forever on
    every pre-existing release would be worse than the risk it closes going
    forward. Any release that DOES publish a manifest is fully enforced:
    a fetch failure, a missing entry for our filename, or an actual hash
    mismatch all fail CLOSED and abort the install.
    """
    if not sha256sums_url:
        logging.warning(
            "Auto-updater: Release has no SHA256SUMS.txt asset (older release) — "
            "skipping checksum verification for %s.", filename,
        )
        return True, ""
    try:
        resp = requests.get(sha256sums_url, timeout=15)
        resp.raise_for_status()
    except Exception as exc:
        return False, f"Failed to download SHA256SUMS.txt: {exc}"

    expected = ""
    for line in resp.text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[1].strip().lstrip("*") == filename:
            expected = parts[0].strip().lower()
            break

    if not expected:
        return False, f"No checksum entry for {filename} in SHA256SUMS.txt"

    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            sha256.update(chunk)
    actual = sha256.hexdigest().lower()

    if actual != expected:
        return False, f"Checksum mismatch for {filename}: expected {expected}, got {actual}"

    logging.info("Auto-updater: Checksum verified for %s (%s).", filename, actual)
    return True, ""


def _safe_extract_zip(zf: zipfile.ZipFile, dest_dir: str) -> None:
    """Extract *zf* into *dest_dir*, rejecting any member whose path would
    land outside dest_dir (zip-slip: a member name like "../../evil.exe" or
    an absolute path). zipfile.ZipFile.extractall() sanitizes some of this on
    modern Python but not all of it, and this ZIP is untrusted content
    downloaded over the network (a GitHub release asset, but still an
    external input) rather than something WinZapp generated itself."""
    dest_dir = os.path.abspath(dest_dir)
    for member in zf.namelist():
        target = os.path.abspath(os.path.join(dest_dir, member))
        if not (target == dest_dir or target.startswith(dest_dir + os.sep)):
            raise ValueError(f"Refusing to extract unsafe zip member path: {member!r}")
    zf.extractall(dest_dir)


# ── Version helpers ───────────────────────────────────────────────────────────

_PRE_ORDER = {"dev": 0, "alpha": 1, "beta": 2, "": 3}

_VER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)\.(\d+)(dev|alpha|beta)?$", re.IGNORECASE)


def parse_version(v: str):
    """Parse "1.2.3.4suffix" -> ((1,2,3,4), suffix) or None on failure."""
    if not v:
        return None
    m = _VER_RE.match(v.strip())
    if not m:
        return None
    nums   = tuple(int(m.group(i)) for i in range(1, 5))
    suffix = (m.group(5) or "").lower()
    return (nums, suffix)


def is_newer(remote: str, local: str) -> bool:
    """Return True if remote version is strictly newer than local."""
    r = parse_version(remote)
    lo = parse_version(local)
    if r is None or lo is None:
        return False
    r_nums, r_suf = r
    l_nums, l_suf = lo
    r_key = (r_nums, _PRE_ORDER.get(r_suf, 0))
    l_key = (l_nums, _PRE_ORDER.get(l_suf, 0))
    return r_key > l_key


# ── Release channel selection ─────────────────────────────────────────────────
# Two release channels live in the same GitHub Releases list: stable releases
# (cut by hand) and alpha builds, which .github/workflows/alpha-release.yml
# publishes automatically for every commit that lands on main. The ONLY thing
# separating them is the literal word "alpha" in the tag/name — which is why
# that workflow bakes it into both. An alpha is offered exclusively to users
# who ticked "check for alpha updates" in Settings > General.


def is_alpha_release(release: dict) -> bool:
    """True if *release* (a GitHub Releases API object) is an alpha build.

    Matches on the literal word "alpha" in the tag or the release name, the
    marker alpha-release.yml guarantees on both. Deliberately not keyed on the
    API's own `prerelease` flag: that flag is also set on unrelated one-off
    test builds (see prerelease-test.yml), and the two channels must not be
    conflated — enabling alpha updates must not silently opt a user into every
    other prerelease anyone ever publishes.
    """
    tag  = (release.get("tag_name") or "").lower()
    name = (release.get("name") or "").lower()
    return "alpha" in tag or "alpha" in name


def find_zip_asset(assets: list) -> str:
    """Return the browser_download_url of a release's portable ZIP asset, or "".

    Prefers the exact WinZapp.zip and falls back to any other .zip — the same
    rule the two now-consolidated copies of this loop always used.
    """
    fallback = ""
    for asset in assets or []:
        name = (asset.get("name") or "").lower()
        url  = asset.get("browser_download_url", "")
        if name == "winzapp.zip":
            return url
        if name.endswith(".zip") and not fallback:
            fallback = url
    return fallback


def select_release(releases: list, include_alpha: bool) -> "dict | None":
    """Pick the newest release the user is eligible to receive, or None.

    *releases* is a collection of GitHub release objects. Alpha builds are
    skipped entirely unless *include_alpha*; drafts always are.

    The newest ELIGIBLE release is chosen by comparing parsed versions rather
    than trusting the list order, because the list is ordered by publish date
    and mixes both channels: with alphas filtered out, the first entry is
    frequently an alpha, and picking blindly used to mean the newest stable —
    sitting a few entries down — was never even considered. Releases whose tag
    isn't a parseable WinZapp version are ignored for the same reason; they
    could never be compared against the running version anyway.

    A release with no ZIP asset is also skipped rather than selected-then-
    rejected. Both callers used to take the very first entry and give up
    entirely if it had no ZIP; with alphas published automatically that stops
    being a theoretical case (a build whose upload was cut short by a cancelled
    workflow run), and one such release must not block updates for everyone
    until the next one is cut.
    """
    best     = None
    best_key = None
    for release in releases:
        if not isinstance(release, dict) or release.get("draft"):
            continue
        if is_alpha_release(release) and not include_alpha:
            continue
        parsed = parse_version((release.get("tag_name") or "").lstrip("vV"))
        if parsed is None:
            continue
        if not find_zip_asset(release.get("assets", [])):
            continue
        key = (parsed[0], _PRE_ORDER.get(parsed[1], 0))
        if best_key is None or key > best_key:
            best, best_key = release, key
    return best


# ── Changelog parser ──────────────────────────────────────────────────────────

_HDR_RE = re.compile(r"^V(\d+\.\d+\.\d+\.(?:\d+)(?:dev|alpha|beta)?)\s*$", re.IGNORECASE)


def get_changelog_for_update(changelog_text: str, current: str, new: str) -> str:
    """
    Extract changelog entries for all versions > current and <= new.
    Returns empty string if no relevant entries found.
    """
    c_parsed = parse_version(current)
    n_parsed = parse_version(new)
    if c_parsed is None or n_parsed is None:
        return ""

    c_key = (c_parsed[0], _PRE_ORDER.get(c_parsed[1], 0))
    n_key = (n_parsed[0], _PRE_ORDER.get(n_parsed[1], 0))

    # Split into sections by "V1.2.3.4" header lines
    sections = []
    cur_ver   = None
    cur_lines = []
    for line in changelog_text.splitlines():
        m = _HDR_RE.match(line.strip())
        if m:
            if cur_ver is not None:
                sections.append((cur_ver, cur_lines))
            cur_ver   = m.group(1)
            cur_lines = []
        else:
            if cur_ver is not None:
                cur_lines.append(line)
    if cur_ver is not None:
        sections.append((cur_ver, cur_lines))

    result_parts = []
    for ver_str, lines in sections:
        parsed = parse_version(ver_str)
        if parsed is None:
            continue
        key = (parsed[0], _PRE_ORDER.get(parsed[1], 0))
        if c_key < key <= n_key:
            body = "\n".join(lines).strip()
            if body:
                result_parts.append(f"V{ver_str}\n{body}")

    return "\n\n".join(result_parts)


def _read_changelog_file(lang_code: str) -> str:
    """Return the raw text of changelog_<lang_code>.txt shipped next to the
    app, or "" if it doesn't exist / can't be read."""
    path = resource_path(f"changelog_{lang_code}.txt")
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        logging.warning("Auto-updater: Failed to read changelog file %s", path, exc_info=True)
        return ""


def load_changelog_text(lang_code: str) -> str:
    """
    Resolve the changelog text to show in "What's new".

    Looks for changelog_<lang_code>.txt next to the app first (kept current
    without a WinZapp rebuild, just like language_map.json is for language
    names), falls back to changelog_en-US.txt if the user's configured
    language has none, and returns "" if neither exists so the caller can
    fall back to the GitHub release notes as a last resort.
    """
    text = _read_changelog_file(lang_code)
    if text:
        return text
    if lang_code != "en-US":
        text = _read_changelog_file("en-US")
        if text:
            return text
    return ""


def resolve_changelog(local_version: str, remote_version: str, lang_code: str, release_body: str = "") -> str:
    """
    Resolve the text to show in the "What's new" dialog for an update from
    *local_version* to *remote_version*.

    Preference order:
      1. changelog_<lang_code>.txt (or changelog_en-US.txt as fallback),
         filtered down to just the entries between local_version (exclusive)
         and remote_version (inclusive) via get_changelog_for_update(). If a
         file was found but has no version-tagged entries in that range
         (e.g. it predates the "V1.2.3.4" header convention), its raw
         content is shown as-is rather than being silently discarded.
      2. The raw GitHub release body — used only when neither changelog
         file exists at all, since it's written per-release rather than
         per-version and isn't guaranteed to describe every version in the
         jump when several were skipped between checks.
    """
    raw = load_changelog_text(lang_code)
    if raw:
        filtered = get_changelog_for_update(raw, local_version, remote_version)
        return filtered if filtered else raw.strip()
    return (release_body or "").strip()


def _wrap_changelog_text(text: str, width: int = 100) -> str:
    """Word-wrap *text* to *width* columns per line for display in the
    What's New TextCtrl, never breaking a word mid-way. Blank lines
    (paragraph/section breaks) are preserved as-is."""
    import textwrap
    out_lines = []
    for line in text.splitlines():
        if not line.strip():
            out_lines.append("")
            continue
        wrapped = textwrap.wrap(line, width=width, break_long_words=False, break_on_hyphens=False)
        out_lines.extend(wrapped if wrapped else [""])
    return "\n".join(out_lines)


# ── Install helpers ───────────────────────────────────────────────────────────

def _oem_encoding() -> str:
    """The code page cmd.exe decodes a .bat file with.

    Not the one Python writes text in by default, and not UTF-8: a console
    reads a batch script in the OEM code page (CP852 on a Polish Windows,
    CP850 on a Portuguese one, ...). GetOEMCP() is what actually answers
    that; cp850 is a last-resort guess if the call is unavailable.
    """
    try:
        return f"cp{ctypes.windll.kernel32.GetOEMCP()}"
    except Exception:
        return "cp850"


def _console_safe_path(path: str) -> str:
    """*path* in a form a batch script can carry safely — its 8.3 short form
    when the long one has characters outside ASCII.

    A path like ``C:\\Users\\Paweł\\AppData\\Local\\WinZapp`` written into a
    .bat as UTF-8 reaches cmd.exe as mojibake, because cmd decodes the file in
    the OEM code page (see _oem_encoding()). Every command that path appears
    in then addresses a directory that does not exist — which is exactly how
    an update could copy nothing, write no failure marker (the marker path
    contains the same character), and never relaunch the app (so does the
    exe path), leaving no trace of why. Reported from a Windows account whose
    name contains "ł" (issue #83).

    GetShortPathNameW returns the 8.3 alias, which is pure ASCII, so the
    script becomes code-page-independent. It needs the path to exist and 8.3
    generation to be enabled on the volume; when either isn't true it returns
    the long path unchanged and the caller falls back to writing the script
    in the OEM code page instead.
    """
    if not path or path.isascii() or sys.platform != "win32":
        return path
    try:
        buf = ctypes.create_unicode_buffer(32768)
        length = ctypes.windll.kernel32.GetShortPathNameW(path, buf, len(buf))
        if length and buf.value and buf.value.isascii():
            return buf.value
        logging.info(
            "Auto-updater: no ASCII 8.3 name for %r (8.3 generation disabled?) "
            "— falling back to the OEM code page for the script.", path,
        )
    except Exception:
        logging.exception("Auto-updater: GetShortPathNameW failed for %r", path)
    return path


def _build_installer_script(source_dir: str, install_dir: str, exe_path: str,
                            log_path: str, marker_path: str, pid: int,
                            api_port: int) -> str:
    """The batch script text. Pure — every path is already console-safe.

    Kept apart from _run_batch_installer() so what the script says can be
    asserted without launching anything.
    """
    return (
        "@echo off\n"
        # Keep one previous run's log (as .old) before truncating: this file
        # is the only record of what the installer actually did, and an
        # update that goes wrong right as the app exits (issue: a Node/
        # WPPConnect crash mid-update, "desconexão ao atualizar") is exactly
        # the case where the CURRENT run's log alone can't show whether this
        # same update flow has misbehaved before. A silent `> "{log_path}"`
        # below would erase that history on every single update, healthy or
        # not — so move it aside first (best-effort; a locked/missing file
        # from the very first update ever just does nothing here).
        f'move /Y "{log_path}" "{log_path}.old" >NUL 2>&1\n'
        f'> "{log_path}" echo [WinZapp] update started %DATE% %TIME%\n'
        # The active code page goes into the log first: when a path does come
        # out wrong, this is the single fact that explains it, and the old
        # script left no record of anything at all.
        f'>> "{log_path}" chcp\n'
        f'>> "{log_path}" echo source: {source_dir}\n'
        f'>> "{log_path}" echo target: {install_dir}\n'
        ":WAIT\n"
        f'tasklist /FI "PID eq {pid}" 2>NUL | find "{pid}" >NUL\n'
        "if not errorlevel 1 (\n"
        "    timeout /t 1 /nobreak >NUL\n"
        "    goto WAIT\n"
        ")\n"
        # Give child processes a moment to exit, then kill stragglers holding file locks.
        "timeout /t 2 /nobreak >NUL\n"
        f"for /f \"tokens=5\" %%a in ('netstat -aon ^| findstr :{api_port} ^| findstr LISTENING') do taskkill /F /PID %%a >NUL 2>&1\n"
        "for /f \"tokens=5\" %%a in ('netstat -aon ^| findstr :5433 ^| findstr LISTENING') do taskkill /F /PID %%a >NUL 2>&1\n"
        "timeout /t 1 /nobreak >NUL\n"
        # xcopy's exit code was previously never checked, so a failed copy
        # (locked file, disk full, permissions) silently relaunched whatever
        # was already in install_dir — the user saw the app come back and
        # assumed the update worked. errorlevel 4+ means xcopy itself failed
        # (as opposed to 0/1, which just mean "nothing to copy"/"success");
        # leave a marker file WinZapp checks on next startup so the user is
        # told instead of silently running a stale/partial install.
        f'xcopy /E /Y /I /H "{source_dir}\\*" "{install_dir}\\" >> "{log_path}" 2>&1\n'
        "if errorlevel 4 (\n"
        f'    >> "{log_path}" echo xcopy FAILED\n'
        f'    echo update failed > "{marker_path}"\n'
        f'    if exist "{exe_path}" start "" "{exe_path}"\n'
        # Deliberately not deleted on failure: the script and its log are the
        # only evidence of what went wrong, and erasing them is what made the
        # original report impossible to diagnose from the user's machine.
        "    exit /b 1\n"
        ")\n"
        f'>> "{log_path}" echo xcopy OK\n'
        f'if exist "{exe_path}" start "" "{exe_path}"\n'
        'del "%~f0"\n'
    )


def _write_installer_script(bat_path: str, script: str) -> bool:
    """Write *script* so cmd.exe reads it back correctly. Returns success.

    ASCII (the normal case, and what _console_safe_path() aims for) is written
    as-is. Anything left over is written in the code page cmd will decode it
    with — never UTF-8, which is what made a non-ASCII install path unusable.
    A script that cannot be represented at all is refused rather than written
    corrupt: the caller then reports a failed update instead of closing the
    app for an installer that would quietly do nothing.
    """
    encoding = "ascii" if script.isascii() else _oem_encoding()
    try:
        with open(bat_path, "w", encoding=encoding, errors="strict", newline="\r\n") as f:
            f.write(script)
    except (UnicodeEncodeError, LookupError) as exc:
        logging.error(
            "Auto-updater: installer script cannot be written in %s (%s) — "
            "the install path has characters cmd.exe cannot read back. "
            "Aborting instead of running a script with broken paths.",
            encoding, exc,
        )
        return False
    logging.info("Auto-updater: Wrote batch installer script to %s (encoding=%s)",
                 bat_path, encoding)
    return True


def _needs_admin() -> bool:
    """Return True if the install directory is not writable by the current user."""
    install_dir = _outer_exe_dir()
    test_path   = os.path.join(install_dir, ".wz_write_test")
    try:
        with open(test_path, "w") as f:
            f.write("x")
        os.remove(test_path)
        return False
    except OSError:
        return True


def _run_batch_installer(extracted_dir: str, install_dir: str, exe_name: str, pid: int, api_port: int = 6300) -> bool:
    """
    Write a batch script that:
      1. Waits for PID to exit.
      2. Kills any leftover WPPConnect Server (api_port) and PostgreSQL (5433) processes.
      3. Copies all extracted files to install_dir.
      4. Restarts the client executable.
    Then launches it (elevated if the directory needs admin).

    Returns True if the script was actually launched. Callers must check
    this — previously it was ignored, so a declined UAC prompt (ShellExecuteW
    returns an error code <= 32, e.g. ERROR_CANCELLED when the user clicks
    "No") still reported the update as successful and closed the app, even
    though the batch script never ran and nothing was actually installed.
    """
    source_dir = extracted_dir
    winzapp_sub = os.path.join(extracted_dir, "WinZapp")
    if os.path.isdir(winzapp_sub):
        source_dir = winzapp_sub

    bat_fd, bat_path = tempfile.mkstemp(suffix=".bat", prefix="winzapp_upd_")
    os.close(bat_fd)

    # Every path the script carries is made console-safe: a non-ASCII install
    # path (a Windows account named "Paweł", issue #83) reached cmd.exe as
    # mojibake and every command in the script then addressed a directory that
    # does not exist. Only the DIRECTORIES are converted — GetShortPathNameW
    # answers for paths that exist, and the exe/marker/log are files the
    # script is about to create — so the ASCII file names are joined onto the
    # already-safe directory afterwards.
    safe_source  = _console_safe_path(source_dir)
    safe_install = _console_safe_path(install_dir)

    # The log goes into the current account's own logs/ folder (same place
    # log.log and shutdown_audit.log live), not loose next to the exe —
    # multi-account installs share one exe dir across every account's
    # process, and dropping an unaccounted-for file there is exactly the
    # "solto com os arquivos principais do programa" complaint this fixes.
    # update_failed.marker stays in the install dir on purpose: main.py
    # reads it at the very start of __init__, before an account is even
    # chosen, so it has to live somewhere account-agnostic.
    try:
        account_log_dir = log_path()
        os.makedirs(account_log_dir, exist_ok=True)
        safe_log_dir = _console_safe_path(account_log_dir)
    except Exception:
        # No active account yet (shouldn't happen — this runs from a live
        # MainWindow instance — but the update must not be blocked by it).
        safe_log_dir = safe_install

    script = _build_installer_script(
        safe_source,
        safe_install,
        os.path.join(safe_install, exe_name),
        os.path.join(safe_log_dir, "update_install.log"),
        os.path.join(safe_install, "update_failed.marker"),
        pid,
        api_port,
    )
    if not _write_installer_script(bat_path, script):
        return False

    if sys.platform == "win32":
        needs_admin = _needs_admin()
        if needs_admin:
            # ShellExecuteW returns an HINSTANCE-shaped value that is > 32 on
            # success and an SE_ERR_* code <= 32 on failure — notably
            # ERROR_CANCELLED (1223) when the user clicks "No" on the UAC
            # consent prompt. That return value used to be discarded, so a
            # declined prompt still left the caller believing the update had
            # been launched (self._install_ok = True), when in fact nothing
            # ran and the app was about to close having done nothing.
            result = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", "cmd.exe", f'/c "{bat_path}"', None, 0
            )
            if result <= 32:
                logging.warning(
                    "Auto-updater: ShellExecuteW('runas', ...) failed or was "
                    "declined by the user (result=%s); batch installer was "
                    "not launched.", result,
                )
                return False
        else:
            # DETACHED_PROCESS is deliberately NOT used here: it gives the child no
            # console at all, and a console-less cmd.exe decodes the batch
            # script's text with the ANSI code page instead of the OEM one
            # _oem_encoding()/_write_installer_script() use — reintroducing
            # issue #83 (non-ASCII path mojibake) for any user whose ANSI and
            # OEM code pages differ, which is most non-English Windows
            # installs. CREATE_NO_WINDOW alone still gives the child a
            # hidden console using the system's default OEM code page, which
            # is exactly what matches _oem_encoding()'s GetOEMCP() call —
            # confirmed empirically (see tests/test_updater_unicode_paths.py).
            #
            # This used to read `| getattr(subprocess, "DETACH_PROCESS", 0)`,
            # which only ever contributed 0 because the real constant is
            # DETACHED_PROCESS — the correct behaviour depended on the typo
            # surviving, which is the kind of thing a linter or a helpful
            # reviewer "fixes" straight back into issue #83.
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.Popen(
                ["cmd.exe", "/c", bat_path],
                creationflags=flags,
            )
        return True
    else:
        logging.warning("Auto-updater: Platform %s is not supported for batch installer execution.", sys.platform)
        return False


# ── WhatsNewDialog ────────────────────────────────────────────────────────────

class WhatsNewDialog(wx.Dialog):
    """Shows the changelog entries between the current and new version."""

    def __init__(self, parent, changelog: str):
        # parent is the UpdateDialog, which stores the real MainWindow as
        # _main_window (not main_window) and never kept its own .i18n
        # attribute — looking those up directly raised an AttributeError
        # every time "Quais as novidades?" was clicked.
        mw = getattr(parent, "main_window", None) or getattr(parent, "_main_window", None) or parent
        i18n = mw.i18n
        super().__init__(
            parent,
            title=i18n.t("whats_new_title"),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self._build(parent, changelog, i18n)
        self.SetMinSize((400, 300))
        self.SetSize((520, 400))
        self.Centre()

    def _build(self, parent, changelog, i18n):
        sizer = wx.BoxSizer(wx.VERTICAL)

        text_ctrl = wx.TextCtrl(
            self,
            value=_wrap_changelog_text(changelog),
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP,
        )
        sizer.Add(text_ctrl, 1, wx.EXPAND | wx.ALL, 8)

        close_btn = wx.Button(self, wx.ID_CLOSE, label=i18n.t("whats_new_close"))
        sizer.Add(close_btn, 0, wx.ALIGN_CENTER | wx.BOTTOM, 8)
        close_btn.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_CLOSE))

        self.SetSizer(sizer)


# ── UpdateProgressDialog ──────────────────────────────────────────────────────

class UpdateProgressDialog(wx.Dialog):
    """
    Shows download + install progress.
    Runs the download in a background thread, updates gauge via CallAfter.
    """

    def __init__(self, parent, new_version: str, main_window, zip_url: str, sha256sums_url: str = ""):
        i18n = main_window.i18n
        super().__init__(
            parent,
            title=i18n.t("update_progress_title"),
            style=wx.DEFAULT_DIALOG_STYLE,
        )
        self._main_window    = main_window
        self._new_version    = new_version
        self._zip_url        = zip_url
        self._sha256sums_url = sha256sums_url
        self._cancelled      = False
        self._install_ok     = False
        self._error_msg      = ""
        self._build(i18n)
        self.SetMinSize((400, -1))
        self.Fit()
        self.Centre()

    def _build(self, i18n):
        sizer = wx.BoxSizer(wx.VERTICAL)

        self._status_label = wx.StaticText(self, label=i18n.t("update_downloading"))
        sizer.Add(self._status_label, 0, wx.ALL, 12)

        self._gauge = wx.Gauge(self, range=100, style=wx.GA_HORIZONTAL | wx.GA_SMOOTH)
        sizer.Add(self._gauge, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)

        self._cancel_btn = wx.Button(self, wx.ID_CANCEL, label=i18n.t("cancel"))
        self._cancel_btn.Bind(wx.EVT_BUTTON, self._on_cancel)
        sizer.Add(self._cancel_btn, 0, wx.ALIGN_CENTER | wx.BOTTOM, 12)

        self.SetSizer(sizer)

    def _on_cancel(self, event):
        self._cancelled = True
        self.EndModal(wx.ID_CANCEL)

    def run(self):
        """Start the download thread and show the dialog modally."""
        t = threading.Thread(target=self._worker, daemon=True)
        t.start()
        return self.ShowModal()

    def _worker(self):
        """Download, extract, and launch installer — all in a background thread."""
        try:
            # ── Download ──────────────────────────────────────────────────────
            zip_fd, zip_path = tempfile.mkstemp(suffix=".zip", prefix="winzapp_upd_")
            os.close(zip_fd)

            logging.info("Auto-updater: Downloading ZIP from %s to %s", self._zip_url, zip_path)
            resp = requests.get(self._zip_url, stream=True, timeout=60)
            resp.raise_for_status()

            total = int(resp.headers.get("content-length", 0))
            downloaded = 0
            with open(zip_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    if self._cancelled:
                        logging.info("Auto-updater: Download cancelled by user.")
                        return
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        pct = min(int(downloaded * 100 / total), 99)
                        wx.CallAfter(self._gauge.SetValue, pct)

            if self._cancelled:
                logging.info("Auto-updater: Download cancelled by user.")
                return

            logging.info("Auto-updater: Download completed successfully.")

            # ── Verify integrity ─────────────────────────────────────────────────
            # Before this ZIP is trusted with elevated write access to the
            # install directory (below), confirm it's byte-for-byte what CI
            # actually built — not a MITM'd download or a tampered/hijacked
            # release edit. See _verify_sha256sums()'s docstring for the
            # fail-open/fail-closed policy.
            filename = os.path.basename(self._zip_url.split("?")[0])
            ok, detail = _verify_sha256sums(zip_path, filename, self._sha256sums_url)
            if not ok:
                logging.error("Auto-updater: Checksum verification failed for %s: %s", filename, detail)
                try:
                    os.remove(zip_path)
                except OSError:
                    pass
                self._error_msg = self._main_window.i18n.t("update_checksum_mismatch").format(detail=detail)
                wx.CallAfter(self.EndModal, wx.ID_ABORT)
                return

            # ── Extract ───────────────────────────────────────────────────────
            extract_dir = tempfile.mkdtemp(prefix="winzapp_ext_")
            logging.info("Auto-updater: Extracting update to %s", extract_dir)
            with zipfile.ZipFile(zip_path, "r") as zf:
                _safe_extract_zip(zf, extract_dir)
            os.remove(zip_path)

            # If the ZIP placed all files inside a single top-level folder,
            # point extract_dir at that folder so xcopy copies the contents.
            _entries = [e for e in os.listdir(extract_dir) if not e.startswith(".")]
            if len(_entries) == 1 and os.path.isdir(
                os.path.join(extract_dir, _entries[0])
            ):
                extract_dir = os.path.join(extract_dir, _entries[0])

            if self._cancelled:
                logging.info("Auto-updater: Extraction cancelled by user.")
                return

            # ── Install ───────────────────────────────────────────────────────
            wx.CallAfter(
                self._status_label.SetLabel,
                self._main_window.i18n.t("update_installing"),
            )
            wx.CallAfter(self._gauge.SetValue, 100)

            if not _is_frozen():
                logging.info("Auto-updater: Dev mode detected. Skipping real installation.")
                time.sleep(1)
                # _install_ok means "the batch installer is running and will
                # relaunch us", which is the caller's licence to quit the app.
                # Nothing was launched here, so claiming it made _do_install()
                # call real_exit() on a dev run: the app killed itself, took
                # the WPPConnect server with it, and no installer existed to
                # bring anything back. Reported live as a pairing that died
                # mid-flow — the code arrived, the process was already gone.
                # Same lie the declined-UAC path used to tell; see the
                # ShellExecuteW comment above.
                self._install_ok = False
                wx.CallAfter(self.EndModal, wx.ID_OK)
                return

            install_dir = _outer_exe_dir()
            exe_name    = os.path.basename(sys.argv[0]) if sys.argv else "WinZapp.exe"
            pid         = os.getpid()

            logging.info("Auto-updater: Launching batch installer from %s (PID %d)", install_dir, pid)
            launched = _run_batch_installer(extract_dir, install_dir, exe_name, pid, api_port=getattr(self._main_window, "wpp_port", 6300))
            if not launched:
                self._error_msg = self._main_window.i18n.t("update_uac_declined")
                wx.CallAfter(self.EndModal, wx.ID_ABORT)
                return
            self._install_ok = True
            wx.CallAfter(self.EndModal, wx.ID_OK)

        except Exception as exc:
            logging.exception("Auto-updater: Exception during update installation")
            self._error_msg = str(exc)
            wx.CallAfter(self.EndModal, wx.ID_ABORT)


# ── UpdateDialog ──────────────────────────────────────────────────────────────

class UpdateDialog(wx.Dialog):
    """
    Prompts the user to install an available update.
    Buttons: Sim | Nao | Quais as novidades? (hidden when no changelog)
    """

    def __init__(self, parent, new_version: str, changelog: str):
        self._main_window = parent
        i18n = parent.i18n
        super().__init__(
            parent,
            title=i18n.t("update_available_title"),
            style=wx.DEFAULT_DIALOG_STYLE,
        )
        self._new_version = new_version
        self._changelog   = changelog
        self._build(i18n)
        self.Fit()
        self.SetMinSize((360, -1))
        self.Centre()

    def _build(self, i18n):
        sizer = wx.BoxSizer(wx.VERTICAL)

        msg = i18n.t("update_available_msg").format(new_version=self._new_version)
        label = wx.StaticText(self, label=msg)
        label.Wrap(380)
        sizer.Add(label, 0, wx.ALL, 12)

        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)

        self._yes_btn = wx.Button(self, wx.ID_YES, label=i18n.t("update_yes"))
        self._no_btn  = wx.Button(self, wx.ID_NO,  label=i18n.t("update_no"))
        btn_sizer.Add(self._yes_btn, 0, wx.RIGHT, 4)
        btn_sizer.Add(self._no_btn,  0, wx.RIGHT, 4)

        if self._changelog:
            self._news_btn = wx.Button(self, wx.ID_MORE, label=i18n.t("whats_new_btn"))
            btn_sizer.Add(self._news_btn, 0)
            self._news_btn.Bind(wx.EVT_BUTTON, self._on_whats_new)
        else:
            self._news_btn = None

        sizer.Add(btn_sizer, 0, wx.ALIGN_CENTER | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        self.SetSizer(sizer)

        self._yes_btn.Bind(wx.EVT_BUTTON, self._on_yes)
        self._no_btn.Bind(wx.EVT_BUTTON,  self._on_no)
        # Not is the default (Enter-activated) button on purpose: this dialog
        # can pop up while the user is typing a message, and Space is how
        # NVDA/JAWS/Narrator users activate the focused button — landing on
        # Yes by default risked installing an update from an accidental
        # keystroke instead of a deliberate choice.
        self._no_btn.SetDefault()

    def _on_yes(self, event):
        self.EndModal(wx.ID_YES)

    def _on_no(self, event):
        self.EndModal(wx.ID_NO)

    def _on_whats_new(self, event):
        dlg = WhatsNewDialog(self, self._changelog)
        dlg.ShowModal()
        dlg.Destroy()


# ── UpdateChecker ─────────────────────────────────────────────────────────────

class UpdateChecker:
    """
    Runs version checks in a background thread.
    Shows UpdateDialog on the main thread when a newer version is found.
    Retries every 3 hours on decline or when already up-to-date.
    """

    _RETRY_INTERVAL = 3 * 60 * 60  # 3 hours in seconds

    def __init__(self, main_window):
        self._mw           = main_window
        self._retry_timer  = None
        self._force        = False

    def _alpha_enabled(self) -> bool:
        """Whether the user opted into alpha builds (Settings > General).

        Read fresh on every check rather than cached at construction: the
        checker is created once at startup and lives for the whole session,
        so a user who ticks the box mid-session would otherwise not see an
        alpha until the next launch. Off unless explicitly enabled.
        """
        try:
            return bool(
                self._mw.settings.get("general", {}).get("alpha_updates_enabled", False)
            )
        except Exception:
            return False

    def _get_json(self, url: str, params: "dict | None" = None):
        resp = requests.get(
            url,
            headers={"User-Agent": f"WinZapp/{__version__}"},
            params=params,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def _fetch_releases(self) -> list:
        """Fetch the candidate releases to choose from. Raises if nothing at
        all could be fetched.

        Reads TWO endpoints and merges them, because neither alone is
        sufficient once alpha builds are published per commit:

        - `/releases` is the only place alpha builds appear, but it is PAGED
          (30 per page by default — hence per_page=100 here). Alphas land far
          more often than stable releases, so given enough of them the newest
          stable release falls off the first page entirely and a user on the
          stable channel would silently stop being offered any update at all.
        - `/releases/latest` is defined by GitHub as the newest release that is
          neither draft nor prerelease. Alphas are published as prereleases, so
          this always resolves to the current stable release no matter how many
          alphas were published since — the exact guarantee paging can't give.

        Either request failing on its own is survivable (a rate-limited listing
        still leaves the stable release reachable; `/releases/latest` 404s on a
        repo that has only ever published prereleases), so only a total failure
        propagates.
        """
        releases: list = []
        first_error = None

        try:
            data = self._get_json(GITHUB_API_LATEST_RELEASE, params={"per_page": 100})
            # A fork could point WINZAPP_GITHUB_REPO-derived URLs at a single
            # release object rather than a listing.
            if isinstance(data, dict):
                releases.append(data)
            elif isinstance(data, list):
                releases.extend(d for d in data if isinstance(d, dict))
        except Exception as exc:
            first_error = exc
            logging.warning("Auto-updater: Could not fetch the releases listing: %s", exc)

        try:
            data = self._get_json(GITHUB_API_LATEST_STABLE_RELEASE)
            if isinstance(data, dict):
                releases.append(data)
        except Exception as exc:
            logging.warning("Auto-updater: Could not fetch the latest stable release: %s", exc)
            if first_error is None:
                first_error = exc

        if not releases:
            raise first_error or RuntimeError("No releases could be fetched")

        # The same release legitimately arrives from both endpoints — dedupe so
        # the log's candidate count reflects reality.
        deduped, seen = [], set()
        for release in releases:
            marker = release.get("id") or release.get("tag_name")
            if marker in seen:
                continue
            seen.add(marker)
            deduped.append(release)
        return deduped

    def start(self):
        """Launch the first check in a background thread."""
        t = threading.Thread(target=self._check_once, daemon=True)
        t.start()

    def force_check(self):
        """Called from the Help > Check for Updates menu item."""
        self._force = True
        if self._retry_timer is not None:
            self._retry_timer.cancel()
            self._retry_timer = None
        t = threading.Thread(target=self._check_once, daemon=True)
        t.start()

    def force_reinstall(self):
        """
        Called from the Help > Force Reinstall from ZIP menu item.
        Unlike force_check(), this skips the version comparison entirely and
        always re-downloads and reinstalls whatever ZIP is attached to the
        latest GitHub release — used to recover a broken install without
        waiting for a newer version to exist.
        """
        if self._retry_timer is not None:
            self._retry_timer.cancel()
            self._retry_timer = None
        t = threading.Thread(target=self._fetch_latest_release_for_reinstall, daemon=True)
        t.start()

    # ── Internal ──────────────────────────────────────────────────────────────

    def _check_once(self):
        include_alpha = self._alpha_enabled()
        logging.info(
            "Auto-updater: Checking GitHub Releases for updates (alpha channel: %s)...",
            "on" if include_alpha else "off",
        )
        try:
            releases = self._fetch_releases()
        except Exception:
            logging.exception("Auto-updater: Exception checking for updates")
            self._schedule_retry()
            return

        data = select_release(releases, include_alpha)
        if data is None:
            logging.warning(
                "Auto-updater: No eligible release found among %d listed "
                "(alpha channel: %s).", len(releases), "on" if include_alpha else "off",
            )
            self._schedule_retry()
            return

        tag_name       = data.get("tag_name", "")
        remote_version = tag_name.lstrip("vV")
        logging.info(
            "Auto-updater: Selected release tag=%s version=%s alpha=%s",
            tag_name, remote_version, is_alpha_release(data),
        )

        if not remote_version:
            logging.warning("Auto-updater: Could not parse version from tag_name=%r", tag_name)
            self._schedule_retry()
            return

        # Find the portable ZIP asset (prefer WinZapp.zip by exact name).
        # select_release() already refuses a release without one, so this only
        # fires if that invariant is ever broken.
        zip_url = find_zip_asset(data.get("assets", []))
        if not zip_url:
            logging.warning("Auto-updater: No ZIP asset found in release %s", tag_name)
            self._schedule_retry()
            return

        sha256sums_url = _find_sha256sums_asset(data.get("assets", []))

        local_version = __version__
        logging.info("Auto-updater: Local version is %s", local_version)

        if not is_newer(remote_version, local_version):
            logging.info("Auto-updater: WinZapp is already up-to-date.")
            if self._force:
                self._force = False
                wx.CallAfter(self._show_no_update)
            else:
                self._schedule_retry()
            return

        logging.info("Auto-updater: Newer version %s is available!", remote_version)
        self._force = False

        # Prefer a local, per-version changelog file (see resolve_changelog())
        # over the GitHub release body — only used as a last resort.
        lang_code = self._mw.i18n.get_language() if hasattr(self._mw, "i18n") else "pt-BR"
        changelog = resolve_changelog(local_version, remote_version, lang_code, data.get("body", ""))

        wx.CallAfter(self._show_update_dialog, remote_version, changelog, zip_url, sha256sums_url)

    def _show_no_update(self):
        i18n = self._mw.i18n
        wx.MessageBox(
            i18n.t("update_not_available"),
            i18n.t("update_not_available_title"),
            wx.OK | wx.ICON_INFORMATION,
            self._mw,
        )

    def _fetch_latest_release_for_reinstall(self):
        logging.info("Auto-updater: Fetching latest GitHub release for forced ZIP reinstall...")
        try:
            releases = self._fetch_releases()
        except Exception as exc:
            logging.exception("Auto-updater: Exception fetching latest release for forced reinstall")
            wx.CallAfter(self._show_reinstall_error, str(exc))
            return

        # Same channel filter as the periodic check: a user who never opted
        # into alpha builds must not be handed one by "reinstall from ZIP"
        # either, just because an alpha happens to sit at the top of the list.
        data = select_release(releases, self._alpha_enabled())
        if data is None:
            logging.warning("Auto-updater: No eligible release found for forced reinstall.")
            wx.CallAfter(self._show_reinstall_error, self._mw.i18n.t("update_no_zip_asset"))
            return

        tag_name       = data.get("tag_name", "")
        remote_version = tag_name.lstrip("vV") or tag_name

        zip_url = find_zip_asset(data.get("assets", []))
        if not zip_url:
            logging.warning("Auto-updater: No ZIP asset found in latest release %s", tag_name)
            wx.CallAfter(self._show_reinstall_error, self._mw.i18n.t("update_no_zip_asset"))
            return

        sha256sums_url = _find_sha256sums_asset(data.get("assets", []))

        wx.CallAfter(self._confirm_and_reinstall, remote_version, zip_url, sha256sums_url)

    def _confirm_and_reinstall(self, remote_version: str, zip_url: str, sha256sums_url: str = ""):
        i18n = self._mw.i18n
        if wx.MessageBox(
            i18n.t("force_reinstall_confirm_msg").format(version=remote_version),
            i18n.t("force_reinstall_confirm_title"),
            wx.YES_NO | wx.ICON_WARNING,
            self._mw,
        ) != wx.YES:
            return
        self._do_install(remote_version, zip_url, sha256sums_url)

    def _show_reinstall_error(self, error_msg: str):
        i18n = self._mw.i18n
        wx.MessageBox(
            i18n.t("update_error_msg").format(error=error_msg),
            i18n.t("update_error_title"),
            wx.OK | wx.ICON_ERROR,
            self._mw,
        )

    def _show_update_dialog(self, remote_version: str, changelog: str, zip_url: str, sha256sums_url: str = ""):
        dlg    = UpdateDialog(self._mw, remote_version, changelog)
        result = dlg.ShowModal()
        dlg.Destroy()

        if result == wx.ID_YES:
            self._do_install(remote_version, zip_url, sha256sums_url)
        else:
            # User said No — retry in 3 hours
            self._schedule_retry()

    def _do_install(self, new_version: str, zip_url: str, sha256sums_url: str = ""):
        while True:
            prog = UpdateProgressDialog(self._mw, new_version, self._mw, zip_url, sha256sums_url)
            result = prog.run()
            # Read before Destroy(): this is the dialog's answer to "is a batch
            # installer now running and waiting for this process to exit?", and
            # it is the only thing that justifies quitting.
            install_launched = bool(getattr(prog, "_install_ok", False))
            prog.Destroy()

            if result == wx.ID_OK:
                if not install_launched:
                    # Downloaded and extracted, but nothing was installed and
                    # nothing is waiting to relaunch us — the dev-mode path.
                    # Quitting here would kill a perfectly healthy app (and the
                    # WPPConnect server under it) to complete an update that
                    # never happened.
                    logging.info(
                        "Auto-updater: nothing was installed and no installer is "
                        "waiting — staying open instead of exiting."
                    )
                    return
                # Install launched — quit the app so the batch script can run
                self._mw.real_exit()
                return

            if result == wx.ID_CANCEL:
                # User cancelled
                self._schedule_retry()
                return

            # wx.ID_ABORT: error occurred
            error_msg = prog._error_msg
            i18n = self._mw.i18n
            retry = wx.MessageBox(
                i18n.t("update_error_msg").format(error=error_msg),
                i18n.t("update_error_title"),
                wx.YES_NO | wx.ICON_ERROR,
                self._mw,
            )
            if retry != wx.YES:
                self._schedule_retry()
                return
            # else: loop and retry the download

    def _schedule_retry(self):
        self._retry_timer = threading.Timer(self._RETRY_INTERVAL, self._check_once)
        self._retry_timer.daemon = True
        self._retry_timer.start()

    def stop(self):
        """Cancel any pending retry timer."""
        if self._retry_timer is not None:
            self._retry_timer.cancel()
            self._retry_timer = None


# ── WppUpdateChecker ───────────────────────────────────────────────────────────

class WppUpdateChecker:
    """
    Periodically checks whether a newer wppconnect-server release exists than
    the one currently installed in client/api/ — independent of WinZapp's own
    release cycle. WPPConnect breaks upstream between WinZapp releases too,
    and until this existed the only fix was a user manually deleting
    client/api/ and node_modules so WinZapp would reinstall from scratch.

    Unlike UpdateChecker above, accepting the prompt here doesn't just
    download a new WinZapp build — it stops the *running* API session,
    reinstalls it in place (reusing ApiSetupDialog's ZIP-download flow, no
    git required, works in a compiled build), and restarts it. That's why the
    retry interval is much longer and the first check is delayed well past
    startup by the caller (see MainWindow._start_wpp_update_checker): this
    must never fire while pairing or the initial sync is still settling.

    Version comparison uses packaging.version.Version (plain semver), not
    parse_version()/is_newer() above — those are tailored to WinZapp's own
    hybrid date/semver tag scheme, which wppconnect-server's plain-semver
    tags (e.g. "2.5.3") don't follow.
    """

    _RETRY_INTERVAL = 12 * 60 * 60  # 12 hours
    _PAIRING_RETRY_INTERVAL = 5 * 60  # 5 minutes

    def __init__(self, main_window):
        self._mw          = main_window
        self._retry_timer = None

    def start(self):
        """Launch the first check in a background thread."""
        t = threading.Thread(target=self._check_once, daemon=True)
        t.start()

    def force_check(self):
        """Called from a forced re-check (mirrors UpdateChecker.force_check)."""
        if self._retry_timer is not None:
            self._retry_timer.cancel()
            self._retry_timer = None
        t = threading.Thread(target=self._check_once, daemon=True)
        t.start()

    def force_reinstall(self):
        """
        Called from Help > Force Reinstall WPPConnect. Skips the version
        comparison entirely — always fetches whatever is currently the
        latest release and replaces the installed one with it, to recover a
        broken/corrupted API install without waiting for a real version
        bump to be detected.
        """
        if self._retry_timer is not None:
            self._retry_timer.cancel()
            self._retry_timer = None
        t = threading.Thread(target=self._force_reinstall_worker, daemon=True)
        t.start()

    # ── Internal ──────────────────────────────────────────────────────────────

    @staticmethod
    def _fetch_latest_tag() -> str:
        homologated = homologated_wpp_tag(resource_path("wpp_minimum_version.txt"))
        if homologated:
            return homologated
        from ui.dialogs.api_setup import fetch_latest_wpp_tag
        return fetch_latest_wpp_tag()

    def _check_once(self):
        logging.info("[WppUpdateChecker] Checking for wppconnect-server updates...")
        installed = self._mw._get_installed_wpp_version()
        if not installed:
            # Not installed yet (or version unreadable) — the normal
            # first-run setup / version-gate flow owns that case, not this
            # checker.
            self._schedule_retry()
            return

        tag = self._fetch_latest_tag()
        if not tag:
            self._schedule_retry()
            return
        remote_version = tag.lstrip("vV")

        try:
            from packaging.version import Version
            newer_available = Version(remote_version) > Version(installed)
        except Exception:
            logging.warning(
                "[WppUpdateChecker] Could not compare versions (installed=%r, remote=%r)",
                installed, remote_version,
            )
            self._schedule_retry()
            return

        if not newer_available:
            logging.info("[WppUpdateChecker] wppconnect-server is up to date (%s).", installed)
            self._schedule_retry()
            return

        logging.info(
            "[WppUpdateChecker] Newer wppconnect-server release available: %s -> %s",
            installed, remote_version,
        )
        wx.CallAfter(self._prompt_update, installed, remote_version, tag)

    def _prompt_update(self, installed: str, remote_version: str, tag: str):
        if not self._mw.wpp_update_may_run_now():
            logging.info(
                "[WppUpdateChecker] Pairing in progress — not prompting for "
                "the %s update yet.", remote_version,
            )
            self._schedule_retry(self._PAIRING_RETRY_INTERVAL)
            return

        i18n = self._mw.i18n
        # wx.NO_DEFAULT: this can pop up while the user is typing a message,
        # and Space is how NVDA/JAWS/Narrator users activate the focused
        # button — defaulting to Yes risked reinstalling the API session
        # from an accidental keystroke instead of a deliberate choice.
        if wx.MessageBox(
            i18n.t("wpp_update_available_msg").format(current=installed, new=remote_version),
            i18n.t("wpp_update_available_title"),
            wx.YES_NO | wx.NO_DEFAULT | wx.ICON_INFORMATION,
            self._mw,
        ) == wx.YES:
            self._mw._update_wpp_server(tag)
        else:
            self._schedule_retry()

    def _force_reinstall_worker(self):
        logging.info("[WppUpdateChecker] Force-reinstall requested — fetching latest release tag...")
        tag = self._fetch_latest_tag()
        if not tag:
            wx.CallAfter(
                wx.MessageBox,
                self._mw.i18n.t("wpp_update_fetch_failed_msg"),
                self._mw.i18n.t("update_error_title"),
                wx.OK | wx.ICON_ERROR,
                self._mw,
            )
            return
        remote_version = tag.lstrip("vV")
        wx.CallAfter(self._confirm_force_reinstall, remote_version, tag)

    def _confirm_force_reinstall(self, remote_version: str, tag: str):
        i18n = self._mw.i18n
        if wx.MessageBox(
            i18n.t("wpp_force_reinstall_confirm_msg").format(version=remote_version),
            i18n.t("wpp_force_reinstall_confirm_title"),
            wx.YES_NO | wx.ICON_WARNING,
            self._mw,
        ) != wx.YES:
            return
        self._mw._update_wpp_server(tag)

    def _schedule_retry(self, interval: float = None):
        if interval is None:
            interval = self._RETRY_INTERVAL
        if self._retry_timer is not None:
            self._retry_timer.cancel()
        self._retry_timer = threading.Timer(interval, self._check_once)
        self._retry_timer.daemon = True
        self._retry_timer.start()

    def stop(self):
        """Cancel any pending retry timer."""
        if self._retry_timer is not None:
            self._retry_timer.cancel()
            self._retry_timer = None
