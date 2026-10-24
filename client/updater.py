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
import uuid
import wx

from app_paths import _outer_exe_dir, _is_frozen, resource_path, log_path
from main_window.win32_helpers import _is_elevated
from core import release_keys
from core import tls_trust
from core.background_priority import background_creationflags
from core.release_signature import SIGNATURE_ASSET_NAME, check_release_manifest
from core.dialog_foreground import bring_to_front_if_hidden, message_box, parent_is_hidden
from core.wpp_runtime import homologated_wpp_tag
from update_channel_choice import AlphaChannelChoiceMixin
from config import GITHUB_API_LATEST_RELEASE, GITHUB_API_LATEST_STABLE_RELEASE
from update_background import BackgroundDownloadMixin, background_downloads_enabled
from update_package import discard_package, download_update_package, extraction_root
from core.update_handoff import InstallHandoff
from update_progress import UpdateProgressLifecycle, update_control_if_alive
from ui.modal_utils import end_modal_if_running
from update_node_kill import node_kill_lines, own_node_pids
from update_relaunch import (relaunch_environment, relaunch_lines, run_elevated,
                             start_relaunch_waiter)
from version import __version__
from core.update_install_guard import batch_gate, batch_done
from core.update_outcome import batch_receipt, batch_success, RESULT_NAME


def _find_named_asset(assets: list, name: str) -> str:
    """browser_download_url of the asset called *name* (case-insensitive), or ""."""
    for asset in assets or []:
        if (asset.get("name") or "").lower() == name.lower():
            return asset.get("browser_download_url", "")
    return ""


def _find_sha256sums_asset(assets: list) -> str:
    """Return the browser_download_url of a SHA256SUMS.txt asset in a GitHub
    release's asset list, or "" if the release predates this check (older
    releases published before CI started generating one)."""
    return _find_named_asset(assets, "SHA256SUMS.txt")


def _find_signature_asset(assets: list) -> str:
    """URL of the release's SHA256SUMS.txt.sig (see core/release_signature.py), or ""."""
    return _find_named_asset(assets, SIGNATURE_ASSET_NAME)


def _verify_sha256sums(file_path: str, filename: str, sha256sums_url: str,
                       signature_url: str = "", expected_version: str = "",
                       is_alpha: bool = False,
                       stable_keys=None, alpha_keys=None) -> "tuple[bool, str]":
    """Verify file_path's SHA256 against the checksum manifest published
    alongside the GitHub release (see .github/workflows/build-windows.yml's
    "Generate SHA256SUMS.txt" step), and — once this build trusts any release
    keys — that the manifest carries a valid signature for *expected_version*.
    Returns (ok, detail).

    Before release keys exist (core/release_keys.py empty), fails OPEN only when
    sha256sums_url itself is empty — the release predates the manifest and
    there is nothing to compare against. Once keys exist, that same absence
    fails CLOSED, as does a missing or wrong signature: after signing is set up,
    an unsigned release is what a forged one looks like. A fetch failure, a
    missing entry for our filename, or a hash mismatch always fail CLOSED.

    *stable_keys*/*alpha_keys* default to the ones compiled into this build;
    tests pass their own.
    """
    if stable_keys is None:
        stable_keys = release_keys.STABLE_PUBLIC_KEYS
    if alpha_keys is None:
        alpha_keys = release_keys.ALPHA_PUBLIC_KEYS

    manifest = None
    if sha256sums_url:
        try:
            resp = tls_trust.get(sha256sums_url, timeout=15)
            resp.raise_for_status()
        except Exception as exc:
            return False, f"Failed to download SHA256SUMS.txt: {exc}"
        manifest = resp.content

    signature_text = None
    if manifest is not None and signature_url:
        try:
            sig_resp = tls_trust.get(signature_url, timeout=15)
            sig_resp.raise_for_status()
        except Exception as exc:
            return False, f"Failed to download {SIGNATURE_ASSET_NAME}: {exc}"
        signature_text = sig_resp.text

    ok, detail = check_release_manifest(
        manifest, signature_text, expected_version, is_alpha,
        stable_keys, alpha_keys,
    )
    if not ok:
        return False, detail
    if manifest is not None and signature_text is not None:
        logging.info("Auto-updater: Release signature verified for version %s.", expected_version)

    if manifest is None:
        logging.warning(
            "Auto-updater: Release has no SHA256SUMS.txt asset (older release) — "
            "skipping checksum verification for %s.", filename,
        )
        return True, ""

    expected = ""
    for line in manifest.decode("utf-8", errors="replace").splitlines():
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


def _parse_wpp_version(tag: str):
    """packaging Version of a wppconnect-server tag ("v2.10.16"), or None."""
    try:
        from packaging.version import Version
        return Version((tag or "").lstrip("vV"))
    except Exception:
        return None


def wpp_update_target(installed: str, minimum: str, approved: str = ""):
    """(tag, required) for the periodic WPPConnect check, or None.

    WinZapp's patches follow the server by hand, so the newest upstream release
    is only offered to a user who asked for it, passed in as *approved* (see
    WppUpdateChecker._offered_tag()). Two kinds exist:

    * *required* (True): the installed server is below the bundled minimum
      (wpp_minimum_version.txt), the case ensure_wpp_version() repairs at a
      foreground start;
    * *recommended* (False): the maintainers approved a newer release for this
      build's pin (core/wpp_approved.py), usually a dependency-only upstream
      release, and the installed server is behind it.

    *approved* is best_approved()'s answer, "" when nothing is approved or the
    list could not be read. A required update goes straight to an approved
    release newer than the minimum, so the user does not reinstall twice.
    Unparseable input, or no minimum bundled (a dev checkout), never prompts.
    """
    have = _parse_wpp_version(installed)
    floor = _parse_wpp_version(minimum)
    if have is None or floor is None:
        return None
    wanted = _parse_wpp_version(approved) if approved else None
    target = approved if wanted is not None and wanted > floor else minimum
    if have < floor:
        return target, True
    if _parse_wpp_version(target) > have:
        return target, False
    return None


def _winzapp_satisfies(min_winzapp: str) -> bool:
    """Whether the running WinZapp is at least *min_winzapp* (an approved
    release's optional requirement). A requirement that does not parse as a
    WinZapp version is not met: a typo must not offer a server to everyone."""
    return parse_version(min_winzapp) is not None and not is_newer(min_winzapp, __version__)


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
# that workflow bakes it into both. Automatic alpha offers require the
# Settings opt-in. A manual check on an alpha may ask for one-time consent.


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


def newer_excluded_alpha(releases: list, local_version: str,
                         include_alpha: bool) -> "dict | None":
    """The alpha a disabled alpha channel is keeping from an alpha build, or None.

    Only an alpha build is ever offered one (a stable build that never opted in
    stays on the stable line), and only when nothing the user is eligible for
    is newer: a newer stable is the normal update and wins.
    """
    if include_alpha:
        return None
    parsed = parse_version(local_version)
    if parsed is None or parsed[1] != "alpha":
        return None
    eligible = select_release(releases, False)
    if eligible is not None and is_newer(
            (eligible.get("tag_name") or "").lstrip("vV"), local_version):
        return None
    alpha = select_release([r for r in releases if is_alpha_release(r)], True)
    if alpha is None or not is_newer(alpha["tag_name"].lstrip("vV"), local_version):
        return None
    return alpha


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


def _version_order(version: str):
    """A sort key that ranks versions the way is_newer() compares them."""
    parsed = parse_version(version)
    return (parsed[0], _PRE_ORDER.get(parsed[1], 0)) if parsed else ((0, 0, 0, 0), 0)


def whats_new_text(local_version: str, release: dict, releases: list, lang_code: str,
                   t, fetch) -> str:
    """The "What's new?" text for an update to *release* (#488): the offered
    tag's changelog compared with the installed one, and for an alpha the
    GitHub notes of every alpha since the installed version
    (core/release_notes.py). Falls back to resolve_changelog() when neither
    part is available (offline, a fork without the file)."""
    from config import GITHUB_REPO
    from core import release_notes
    tag = release.get("tag_name", "")
    remote_version = tag.lstrip("vV")
    changes = ""
    fetched = release_notes.changelog_at(fetch, GITHUB_REPO, tag, lang_code) if tag else None
    if fetched is not None:
        locale, offered = fetched
        changes = release_notes.changelog_changes(_read_changelog_file(locale), offered, t)
    github = ""
    if is_alpha_release(release):
        github = release_notes.github_notes(
            releases or [release], is_alpha_release,
            lambda version: is_newer(version, local_version),
            lambda version: not is_newer(version, remote_version), t, _version_order)
    text = release_notes.compose(changes, github, t)
    return text or resolve_changelog(local_version, remote_version, lang_code,
                                     release.get("body", ""))


def _fetch_text(url: str) -> str:
    resp = tls_trust.get(url, headers={"User-Agent": f"WinZapp/{__version__}"}, timeout=15)
    resp.raise_for_status()
    return resp.text


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


def cleanup_lines(cleanup_dir: str, log_path: str) -> str:
    """The script lines that delete the extracted update. "" when there is none.

    Best-effort and silent about failure: the update is installed by now, and a
    folder that cannot be removed (an antivirus still scanning it) is swept at
    the next start (update_temp_sweep.py). The path is quoted and the command
    guarded by `if exist`, so a directory that is already gone is not an error.
    """
    if not cleanup_dir:
        return ""
    return (
        f'if exist "{cleanup_dir}" rmdir /S /Q "{cleanup_dir}" >> "{log_path}" 2>&1\n'
        f'>> "{log_path}" echo temporary update folder cleaned up (or left for the next start)\n'
    )


def _build_installer_script(source_dir: str, install_dir: str, exe_path: str,
                            log_path: str, marker_path: str, pid: int,
                            api_port: int, extra_pids: "list[int] | tuple" = (),
                            node_pids: "list[int] | tuple" = (),
                            relaunch_handoff: str = "",
                            cleanup_dir: str = "", install_guard=None,
                            result_path: str = "", run_id: str = "") -> str:
    """The batch script text. Pure — every path is already console-safe.

    Kept apart from _run_batch_installer() so what the script says can be
    asserted without launching anything.

    ``extra_pids`` are the other accounts' processes: the script must not
    copy over WinZapp.exe and its DLLs while any of them still has those
    files mapped — that xcopy fails with a sharing violation and relaunches
    the old build, which the next update check offers again (the "atualiza
    e não muda" loop with two accounts open). They have already been asked
    to quit over IPC by the time this runs; waiting here covers the seconds
    between their ACK and the process actually being gone.

    ``node_pids`` are WinZapp's own Node processes on ``api_port``, found
    by update_node_kill.own_node_pids() while WinZapp was still running;
    nothing else listening anywhere is ever killed.

    ``relaunch_handoff`` is set only for the elevated install: the marker
    that says a non-elevated waiter will relaunch WinZapp
    (update_relaunch.py). Without it every relaunch is the original line.

    ``cleanup_dir`` is the temporary directory the update was extracted into
    (update_package.extraction_root()). The script removes it once the copy
    has succeeded — and only then: nothing else ever did, so every update left
    a full copy of the release in the user's %TEMP%. A failed copy keeps it
    (like the script itself, it is evidence, and the retry may want it).
    """
    relaunch = relaunch_lines(exe_path, log_path, relaunch_handoff,
                              waiter_required=bool(install_guard))
    source_node = os.path.join(source_dir, "node", "node.exe")
    target_node = os.path.join(install_dir, "node", "node.exe")
    # Keep the held payload beside, not inside, source_dir.  xcopy walks the
    # source tree recursively, so an in-tree staging name would still be copied.
    held_node = source_dir.rstrip("\\/") + ".node.exe.winzapp-unchanged"
    wait_blocks = "".join(
        f"set /a WAIT{i}_SECONDS=0\n"
        f":WAIT{i}\n"
        f'tasklist /FI "PID eq {p}" 2>NUL | find "{p}" >NUL\n'
        "if not errorlevel 1 (\n"
        f"    set /a WAIT{i}_SECONDS+=1\n"
        f"    if !WAIT{i}_SECONDS! GEQ 60 goto OTHER_ACCOUNT_TIMEOUT\n"
        "    timeout /t 1 /nobreak >NUL\n"
        f"    goto WAIT{i}\n"
        ")\n"
        for i, p in enumerate(extra_pids, start=1)
    )
    return (
        "@echo off\n"
        "setlocal EnableDelayedExpansion\n"
        "set WZ_COPY_STARTED=0\n"
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
        + batch_gate(install_guard, log_path) +
        "set /a PARENT_WAIT_SECONDS=0\n"
        ":WAIT\n"
        f'tasklist /FI "PID eq {pid}" 2>NUL | find "{pid}" >NUL\n'
        "if not errorlevel 1 (\n"
        "    set /a PARENT_WAIT_SECONDS+=1\n"
        "    if !PARENT_WAIT_SECONDS! GEQ 300 goto PARENT_TIMEOUT\n"
        "    timeout /t 1 /nobreak >NUL\n"
        "    goto WAIT\n"
        ")\n"
        + wait_blocks +
        batch_receipt(result_path, "pending", run_id) +
        ("if errorlevel 1 goto UPDATE_RESULT_FAILED\n" if result_path else "") +
        # Give child processes a moment to exit, then kill stragglers holding file locks.
        "timeout /t 2 /nobreak >NUL\n"
        + node_kill_lines(node_pids, api_port, log_path) +
        "timeout /t 1 /nobreak >NUL\n"
        # xcopy's exit code was previously never checked, so a failed copy
        # (locked file, disk full, permissions) silently relaunched whatever
        # was already in install_dir — the user saw the app come back and
        # assumed the update worked. Only exit 0 is success: exit 1 means no
        # files found and exit 2 means interruption. Record every nonzero
        # result so an incomplete release cannot be reported as installed.
        "set NODE_PAYLOAD_HELD=0\n"
        f'if exist "{source_node}" if exist "{target_node}" (\n'
        f'    fc /B "{source_node}" "{target_node}" >NUL 2>&1\n'
        # fc: 0 identical, 1 different, 2 missing, -1 bad syntax. "if not
        # errorlevel 1" would count -1 as identical and keep the old Node.
        '    if "!ERRORLEVEL!"=="0" (\n'
        f'        move /Y "{source_node}" "{held_node}" >NUL 2>&1\n'
        "        if not errorlevel 1 (\n"
        "            set NODE_PAYLOAD_HELD=1\n"
        f'            >> "{log_path}" echo node.exe unchanged - skipping locked replacement\n'
        "        )\n"
        "    )\n"
        ")\n"
        "set WZ_COPY_STARTED=1\n"
        f'xcopy /E /Y /I /H "{source_dir}\\*" "{install_dir}\\" >> "{log_path}" 2>&1\n'
        # Retain the existing single retry for copy errors. A sharing
        # violation does not identify the lock holder or prove it will
        # release the file within five seconds.
        # Safe to repeat: xcopy /E /Y /I /H is idempotent — every file it
        # already wrote is overwritten with the same bytes — so the second
        # pass either finishes the copy or fails the same way, and only
        # then is the update declared failed.
        "if errorlevel 4 (\n"
        f'    >> "{log_path}" echo xcopy failed - retrying once in 5s\n'
        "    timeout /t 5 /nobreak >NUL\n"
        f'    xcopy /E /Y /I /H "{source_dir}\\*" "{install_dir}\\" >> "{log_path}" 2>&1\n'
        ")\n"
        # Restoring the held source changes ERRORLEVEL. Save xcopy's result
        # first and put it back so the existing verdict still sees the copy.
        "set XCOPY_RESULT=!ERRORLEVEL!\n"
        'if "!NODE_PAYLOAD_HELD!"=="1" (\n'
        f'    move /Y "{held_node}" "{source_node}" >NUL 2>&1\n'
        f'    if errorlevel 1 >> "{log_path}" echo could not restore the held node.exe\n'
        ")\n"
        "cmd /c exit !XCOPY_RESULT!\n"
        f'>> "{log_path}" echo xcopy exit code: !XCOPY_RESULT!\n'
        'if not "!XCOPY_RESULT!"=="0" (\n'
        f'    >> "{log_path}" echo xcopy FAILED\n'
        f'    echo update failed > "{marker_path}"\n'
        + batch_receipt(result_path, "failed", run_id, "    ")
        + batch_done(install_guard, "failed", "    ")
        + relaunch_lines(exe_path, log_path, relaunch_handoff, indent="    ",
                         waiter_required=bool(install_guard)) +
        # Deliberately not deleted on failure: the script and its log are the
        # only evidence of what went wrong, and erasing them is what made the
        # original report impossible to diagnose from the user's machine.
        "    exit /b 1\n"
        ")\n"
        + batch_success(result_path, marker_path, run_id, log_path)
        + batch_done(install_guard, "success") +
        f'>> "{log_path}" echo xcopy OK\n'
        + relaunch +
        # After the relaunch, not before: removing a few thousand files takes
        # seconds and the user is waiting for the app to come back.
        cleanup_lines(cleanup_dir, log_path) +
        'del "%~f0"\n'
        "goto :EOF\n"
        + (
            ":OTHER_ACCOUNT_TIMEOUT\n"
            f'>> "{log_path}" echo timed out waiting for another WinZapp account to exit\n'
            f'echo update failed: another WinZapp account did not exit > "{marker_path}"\n'
            + batch_receipt(result_path, "failed", run_id)
            + batch_done(install_guard, "unchanged")
            + relaunch +
            "exit /b 1\n"
            if extra_pids else ""
        )
        + ":PARENT_TIMEOUT\n"
        + f'>> "{log_path}" echo ERROR: PID {pid} did not exit after 300 checks; aborting update\n'
        + f'echo update failed: WinZapp did not exit > "{marker_path}"\n'
        + batch_receipt(result_path, "failed", run_id)
        + batch_done(install_guard, "aborted")
        # Unlike copy/peer failures, the parent is still running: neither this
        # script nor the elevated path's waiter may relaunch a second copy.
        + (f'echo aborted> "{relaunch_handoff}.abort"\n' if relaunch_handoff else "")
        + "exit /b 1\n"
        + (":UPDATE_RESULT_FAILED\n"
           + f'>> "{log_path}" echo could not record update result - installation not successful\n'
           + f'> "{marker_path}" echo update failed: result could not be recorded\n'
           + ('if "!WZ_COPY_STARTED!"=="0" (\n'
              + batch_done(install_guard, "unchanged", "    ")
              + ") else (\n" + batch_done(install_guard, "failed", "    ") + ")\n"
              if install_guard else "")
           + relaunch + "exit /b 1\n"
           if result_path else "")
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


def _keep_newer_installed_server(source_dir: str, install_dir: str) -> None:
    """The xcopy below must not put back an older WPPConnect Server than the
    one installed (core/update_keeps_server.py). Never fails the update."""
    try:
        from core.update_keeps_server import keep_newer_installed_server
        from ui.dialogs.api_setup import _PATCHED_DEPENDENCY_KEYS
        keep_newer_installed_server(os.path.join(source_dir, "api"),
                                    os.path.join(install_dir, "api"),
                                    _PATCHED_DEPENDENCY_KEYS)
    except Exception:
        logging.exception("Auto-updater: could not check the installed WPPConnect Server")


def _run_batch_installer(extracted_dir: str, install_dir: str, exe_name: str, pid: int, api_port: int = 6300,
                         extra_pids: "list[int] | tuple" = (), install_guard=None,
                         recovery_text: str = "") -> bool:
    """
    Write a batch script that:
      1. Waits for PID (and every other account's PID in extra_pids) to exit.
      2. Kills WinZapp's own Node if it is still listening on api_port
         (update_node_kill.py).
      3. Copies all extracted files to install_dir.
      4. Restarts the client executable — through a non-elevated waiter
         when the script itself runs elevated (update_relaunch.py).
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
    _keep_newer_installed_server(source_dir, install_dir)

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

    exe_path = os.path.join(safe_install, exe_name)
    install_log = os.path.join(safe_log_dir, "update_install.log")
    # The folder the package was extracted into, which the script deletes once
    # the copy has worked. Empty (so nothing is deleted) for any directory not
    # named like one of ours — see update_package.extraction_root().
    cleanup_dir = _console_safe_path(extraction_root(extracted_dir))
    # Asked now, while this process and its Node are still running: only a
    # process listening on exactly api_port whose executable is WinZapp's own
    # node.exe may be killed by the script (update_node_kill.py). Both
    # locations, because onefile runs Node from its extraction directory.
    node_pids = own_node_pids(api_port, (resource_path("node", "node.exe"),
                                         os.path.join(install_dir, "node", "node.exe")))
    needs_admin = sys.platform == "win32" and _needs_admin()
    if sys.platform == "win32" and not needs_admin and _is_elevated():
        # Not de-elevated on purpose (update_relaunch.py): some people run
        # WinZapp as administrator, and nothing tells that apart from a
        # WinZapp an older build's installer relaunched elevated. Its npm
        # still keeps out of the user's cache (core/npm_environment.py).
        logging.info("Auto-updater: WinZapp is running elevated, so the update "
                     "installs and relaunches it elevated, as before")
    # Guarded/elevated installs: the waiter's relaunch marker. Unlike the
    # script's own path (%~f0),
    # this one is written INTO the script, so its directory — the user's
    # %TEMP%, under a profile that may be "Paweł" — gets the same
    # console-safe treatment as every other path (issue #83). A guarded
    # install requires the outside waiter even when no UAC is needed.
    # Only the legacy unguarded path can fall back to a direct relaunch.
    relaunch_handoff = ""
    if needs_admin or install_guard:
        relaunch_handoff = os.path.join(_console_safe_path(os.path.dirname(bat_path)),
                                        os.path.basename(bat_path) + ".relaunch")
        if not relaunch_handoff.isascii() and not install_guard:
            logging.warning("Auto-updater: the temporary folder has no plain-ASCII "
                            "path; the elevated installer relaunches WinZapp itself")
            relaunch_handoff = ""
    if install_guard and not relaunch_handoff:
        return False  # No direct child relaunch inside the guardian's job.
    guarded_paths = None
    if install_guard:
        guarded_paths = {key: os.path.join(_console_safe_path(os.path.dirname(path)),
                                          os.path.basename(path))
                         for key, path in install_guard.items()}
        if any(any(c in path for c in ('%', '!', '"', '\r', '\n')) for path in
               (safe_source, safe_install, safe_log_dir, relaunch_handoff,
                *guarded_paths.values())):
            logging.error("Auto-updater: installer path cannot be represented safely")
            return False
    script = _build_installer_script(
        safe_source,
        safe_install,
        exe_path,
        install_log,
        os.path.join(safe_install, "update_failed.marker"),
        pid,
        api_port,
        extra_pids=extra_pids,
        node_pids=node_pids,
        relaunch_handoff=relaunch_handoff,
        cleanup_dir=cleanup_dir,
        install_guard=guarded_paths,
        result_path=os.path.join(safe_install, RESULT_NAME) if install_guard else "",
        run_id=uuid.uuid4().hex if install_guard else "",
    )
    if not _write_installer_script(bat_path, script):
        return False

    if install_guard:
        from core.update_guard_launch import launch_guarded
        return launch_guarded(
            bat_path, install_guard, extraction_root(extracted_dir), relaunch_handoff,
            exe_path, install_log, pid, needs_admin, _write_installer_script,
            run_elevated, subprocess.Popen, start_relaunch_waiter,
            relaunch_environment(os.environ), recovery_text=recovery_text)

    if sys.platform == "win32":
        if needs_admin:
            # A declined UAC prompt (ERROR_CANCELLED when the user clicks
            # "No") must come back as False. That answer used to be
            # discarded, so a declined prompt still left the caller
            # believing the update had been launched (self._install_ok =
            # True), when in fact nothing ran and the app was about to close
            # having done nothing.
            launched, installer_pid = run_elevated(bat_path)
            if not launched:
                return False
            if relaunch_handoff:
                try:
                    start_relaunch_waiter(bat_path, installer_pid, exe_path, install_log,
                                          relaunch_handoff, _write_installer_script,
                                          parent_pid=pid)
                except Exception:
                    # The installer ALREADY exists. An auxiliary waiter failure
                    # must never release its slot or tell the caller to stay open.
                    logging.exception("Auto-updater: relaunch waiter failed after installer launch")
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
            # xcopy inherits the priority, so the copy does not freeze the PC (#564).
            flags = background_creationflags(getattr(subprocess, "CREATE_NO_WINDOW", 0))
            subprocess.Popen(
                ["cmd.exe", "/c", bat_path],
                creationflags=flags,
                # What a fresh start would get, as the elevated path's
                # waiter does (update_relaunch.relaunch_environment()).
                env=relaunch_environment(os.environ),
            )
        return True
    else:
        logging.warning("Auto-updater: Platform %s is not supported for batch installer execution.", sys.platform)
        return False


# ── WhatsNewDialog ────────────────────────────────────────────────────────────

class WhatsNewDialog(wx.Dialog):
    """Shows the changelog entries between the current and new version."""

    def __init__(self, parent, changelog: str, title: str = ""):
        # parent is the UpdateDialog, which stores the real MainWindow as
        # _main_window (not main_window) and never kept its own .i18n
        # attribute — looking those up directly raised an AttributeError
        # every time "Quais as novidades?" was clicked.
        mw = getattr(parent, "main_window", None) or getattr(parent, "_main_window", None) or parent
        i18n = mw.i18n
        super().__init__(
            parent,
            title=title or i18n.t("whats_new_title"),
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

class UpdateProgressDialog(UpdateProgressLifecycle, wx.Dialog):
    """
    Shows download + install progress.
    Runs the download in a background thread, updates gauge via CallAfter.
    """

    def __init__(self, parent, new_version: str, main_window, zip_url: str, sha256sums_url: str = "",
                 signature_url: str = "", is_alpha: bool = False, extracted_dir: str = ""):
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
        self._signature_url  = signature_url
        self._is_alpha       = is_alpha
        # A package a background download already fetched, verified and
        # extracted (update_background.py): only the install phase is left.
        self._extracted_dir  = extracted_dir
        self._cancelled      = False
        self._install_ok     = False
        self._error_msg      = ""
        self._handoff        = InstallHandoff()
        self._thread         = None
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)
        self.Bind(wx.EVT_BUTTON, self._on_cancel, id=wx.ID_CANCEL)
        self.SetEscapeId(wx.ID_CANCEL)
        self._build(i18n)
        self.SetMinSize((400, -1))
        self.Fit()
        self.Centre()

    def _build(self, i18n):
        sizer = wx.BoxSizer(wx.VERTICAL)

        # A package fetched in the background has nothing left to download.
        self._status_label = wx.StaticText(self, label=i18n.t(
            "update_installing" if self._extracted_dir else "update_downloading"))
        sizer.Add(self._status_label, 0, wx.ALL, 12)

        self._gauge = wx.Gauge(self, range=100, style=wx.GA_HORIZONTAL | wx.GA_SMOOTH)
        sizer.Add(self._gauge, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)

        self._cancel_btn = wx.Button(self, wx.ID_CANCEL, label=i18n.t("cancel"))
        sizer.Add(self._cancel_btn, 0, wx.ALIGN_CENTER | wx.BOTTOM, 12)

        self.SetSizer(sizer)

    def _worker(self):
        """Download, extract, and launch installer — all in a background thread."""
        update_token = None
        installer_handed_off = False
        extract_dir = getattr(self, "_extracted_dir", "")
        result = wx.ID_CANCEL
        try:
            # ── Download, verify, extract ─────────────────────────────────────
            # Skipped when a background download already did it. The steps
            # themselves are update_package.download_update_package(), shared
            # with that path so both install exactly the same verified bytes.
            if not extract_dir:
                package = download_update_package(
                    self._zip_url, self._sha256sums_url, self._signature_url,
                    self._new_version, self._is_alpha, self._main_window.i18n,
                    on_progress=lambda pct: wx.CallAfter(update_control_if_alive, self._gauge.SetValue, pct),
                    is_cancelled=self._handoff.cancelled.is_set,
                )
                if package.cancelled:
                    return
                if package.error:
                    self._error_msg = package.error
                    result = wx.ID_ABORT
                    return
                extract_dir = package.extract_dir

            if self._handoff.cancelled.is_set():
                logging.info("Auto-updater: Cancelled by user before installing.")
                return

            # ── Install ───────────────────────────────────────────────────────
            wx.CallAfter(
                update_control_if_alive, self._status_label.SetLabel,
                self._main_window.i18n.t("update_installing"),
            )
            wx.CallAfter(update_control_if_alive, self._gauge.SetValue, 100)

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
                # run_elevated() call in _run_batch_installer().
                self._install_ok = False
                result = wx.ID_OK
                return

            install_dir = _outer_exe_dir()
            exe_name    = os.path.basename(sys.argv[0]) if sys.argv else "WinZapp.exe"
            pid         = os.getpid()

            # ── Other accounts ────────────────────────────────────────────────
            # Every account is its own process on the same install dir, so
            # any other one still running keeps WinZapp.exe and its DLLs
            # mapped and xcopy fails on them. Ask them to quit (each closes
            # its WhatsApp session gracefully on the way out, same as the
            # Exit menu's quit_all_accounts()), then claim the install slot,
            # which refuses while any of them is still alive.
            other_pids = self._quit_other_accounts()
            if self._handoff.cancelled.is_set():
                return
            update_token = self._claim_install_slot()
            if update_token is None:
                self._error_msg = self._claim_error
                result = wx.ID_ABORT
                return
            if not self._request_install_handoff():
                return

            logging.info("Auto-updater: Launching batch installer from %s (PID %d, also waiting on %s)",
                         install_dir, pid, other_pids or "no other account")
            from core.update_guard_launch import prepare_guard
            install_guard = prepare_guard(getattr(self._main_window, "global_dir", None),
                                          update_token)
            launched = _run_batch_installer(extract_dir, install_dir, exe_name, pid,
                                            api_port=getattr(self._main_window, "wpp_port", 6300),
                                            extra_pids=other_pids, install_guard=install_guard,
                                            recovery_text=self._main_window.i18n.t("update_recovery_instructions"))
            if not launched:
                self._error_msg = self._main_window.i18n.t(
                    install_guard.get("error", "update_coordination_failed"))
                result = wx.ID_ABORT
                return
            self._install_ok = True
            installer_handed_off = True
            result = wx.ID_OK

        except Exception as exc:
            logging.exception("Auto-updater: Exception during update installation")
            self._error_msg = str(exc)
            result = wx.ID_ABORT
        finally:
            try:
                if not installer_handed_off:
                    try:
                        if update_token is not None:
                            self._end_install_slot(update_token)
                    finally:
                        # A pre-fetched package belongs to the caller (it may retry).
                        if not getattr(self, "_extracted_dir", ""):
                            discard_package(extract_dir)
            finally:
                self._handoff.settled.set()
                wx.CallAfter(end_modal_if_running, self, result)


    # ── Multi-account coordination ───────────────────────────────────────────
    def _coord(self):
        """(global_dir, account_id) or None when there is nothing to
        coordinate with — a single-account/dev run has no second process."""
        gd = getattr(self._main_window, "global_dir", None)
        acc = getattr(self._main_window, "account_id", None)
        return (gd, acc) if gd and acc else None

    def _quit_other_accounts(self) -> list:
        """Ask every other live account to quit and return their PIDs.

        Same peer walk as MainWindow.quit_all_accounts(): sequential, each
        request waits for the peer to confirm it RELEASED its session, so no
        WhatsApp session is hard-killed by the update's own taskkill. Best
        effort — a peer that never answers is left for _claim_install_slot()
        to refuse on, and for the batch script to wait on if it does exit.
        Never raises: the update must not fail on a diagnostic of its peers.
        """
        coord = self._coord()
        if coord is None:
            return []
        gd, acc_id = coord
        pids: list = []
        try:
            import ipc
            import node_coord
            import update_coord
            node_leases = node_coord.live_node_leases(gd, is_alive=update_coord.lease_alive)
            pids = update_coord.peer_pids(update_coord.other_live_leases(gd), node_leases,
                                          acc_id, os.getpid())
            others = [l.get("account_id") for l in node_leases if not l.get("_corrupt")]
            others = [a for a in others if a and a != acc_id]
            if others:
                wx.CallAfter(
                    update_control_if_alive, self._status_label.SetLabel,
                    self._main_window.i18n.t("update_closing_other_accounts"),
                )
            for other in others:
                if self._handoff.cancelled.is_set():
                    break
                try:
                    logging.info("Auto-updater: asking account %s to quit before installing", other)
                    ipc.request_quit(gd, other)
                except Exception:
                    logging.exception("Auto-updater: request_quit failed for %s", other)
        except Exception:
            logging.exception("Auto-updater: enumerating other accounts failed (non-fatal)")
        return pids

    def _claim_install_slot(self):
        """The update_coord owner-token, or None with a specific refusal reason.
        Without a global dir there is no one to coordinate with,
        so a placeholder token lets the install go ahead."""
        coord = self._coord()
        if coord is None:
            return {}
        try:
            import update_coord
            claim = update_coord.try_begin_update(coord[0], explain=True)
            i18n = self._main_window.i18n
            if claim.reason == "other_accounts":
                self._claim_error = i18n.t("update_other_accounts_running")
            elif claim.reason in ("same_process", "other_updater"):
                self._claim_error = i18n.t("update_install_in_progress")
            else:
                self._claim_error = i18n.t("update_coordination_failed")
            return claim.token
        except Exception:
            # Unlike a prompt claim, an install must never proceed unless it
            # can prove no other account still maps the program files.
            logging.exception("Auto-updater: try_begin_update failed — blocking installation")
            self._claim_error = self._main_window.i18n.t("update_coordination_failed")
            return None

    def _end_install_slot(self, token) -> None:
        coord = self._coord()
        if coord is None or not token:
            return
        try:
            import update_coord
            update_coord.end_update(coord[0], token)
        except Exception:
            logging.exception("Auto-updater: end_update failed")


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
        # A hidden parent has no meaningful rectangle to centre on.
        if parent_is_hidden(parent):
            self.CentreOnScreen()
        else:
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

class UpdateChecker(AlphaChannelChoiceMixin, BackgroundDownloadMixin):
    """
    Runs version checks in a background thread.
    Shows UpdateDialog on the main thread when a newer version is found.
    Retries every 3 hours on decline or when already up-to-date.
    """

    _RETRY_INTERVAL = 3 * 60 * 60  # 3 hours in seconds
    # After a failed fetch (network not up yet at login): bounded quick retries
    # in seconds, then back to _RETRY_INTERVAL.
    _FETCH_RETRY_DELAYS = (30, 60, 120, 300, 600)

    def __init__(self, main_window):
        self._mw           = main_window
        self._fetch_failures = 0
        self._retry_timer  = None
        self._force        = False
        self._channel_choice_lock = threading.Lock()
        self._channel_choice_pending = False
        self._channel_choice_cancelled = False
        self._channel_choice_token = None
        # Owner-token from update_coord.try_claim_update_prompt() while this
        # process is the one asking the user about an update; None otherwise.
        self._prompt_token = None

    def _global_dir(self):
        """The multi-account global dir, or None in a single-account/dev run.

        None disables the cross-account prompt claim entirely, which is the
        right degradation: with no shared directory there is no second account
        to duplicate the dialog for.
        """
        return getattr(self._mw, "global_dir", None) or None

    def _claim_prompt(self, remote_version: str) -> bool:
        """Become the one process that asks about this update.

        Every account runs its own UpdateChecker in its own process, so without
        this each of them found the same release and opened its own dialog —
        two accounts, two "a new version is available" windows for one update.
        Only one of them can install it anyway: UpdateProgressDialog asks the
        other accounts to quit and then claims the install slot through
        try_begin_update(), which refuses while any other account's runtime
        lease is still live. The duplicate dialogs were never a second chance
        at anything, just a second thing to dismiss.

        Fails OPEN on any error. A prompt that cannot be coordinated is worth
        far more than a prompt suppressed by a bug in the coordination.
        """
        gd = self._global_dir()
        if not gd:
            return True
        try:
            import update_coord
            token = update_coord.try_claim_update_prompt(gd, remote_version)
        except Exception:
            logging.exception("Auto-updater: prompt claim failed — asking anyway")
            return True
        if token is None:
            return False
        self._prompt_token = token
        return True

    def _release_prompt(self) -> None:
        """Hand the prompt back, so the next account may ask when its own timer
        comes round. Never raises: it runs on the way out of a dialog, and an
        exception here would swallow the user's answer."""
        token, self._prompt_token = self._prompt_token, None
        gd = self._global_dir()
        if not (gd and token):
            return
        try:
            import update_coord
            update_coord.release_update_prompt(gd, token)
        except Exception:
            logging.exception("Auto-updater: releasing the prompt claim failed")

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

    def _translate(self, key: str) -> str:
        i18n = getattr(self._mw, "i18n", None)
        return i18n.t(key) if i18n is not None else key

    def _get_json(self, url: str, params: "dict | None" = None):
        resp = tls_trust.get(
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
        propagates — except that with the alpha channel on, a failed listing
        propagates too: a stable-only result cannot prove an alpha user current.
        """
        releases: list = []
        first_error = None
        listing_error = None

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
            listing_error = exc
            logging.warning("Auto-updater: Could not fetch the releases listing: %s", exc)

        try:
            data = self._get_json(GITHUB_API_LATEST_STABLE_RELEASE)
            if isinstance(data, dict):
                releases.append(data)
        except Exception as exc:
            logging.warning("Auto-updater: Could not fetch the latest stable release: %s", exc)
            if first_error is None:
                first_error = exc

        if listing_error is not None and self._alpha_enabled():
            # A stable-only fallback cannot establish that an alpha user is
            # current. Keep the existing fetch retry instead of reporting so.
            raise listing_error
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
        self._channel_choice_cancelled = False
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
        if getattr(self, "_channel_choice_pending", False):
            self._force = False
            return  # The queued choice owns the prompt and its next retry.
        if getattr(self, "_install_pending", False):
            self._force = False
            self._schedule_retry()
            return
        if getattr(self, "_background_version", ""):
            # Already accepted and downloading in the background: asking again
            # would open a second prompt, and a Yes a second download.
            forced, self._force = self._force, False
            if forced:
                wx.CallAfter(self._say_background_update_running)
            self._schedule_retry()
            return
        include_alpha = self._alpha_enabled()
        logging.info(
            "Auto-updater: Checking GitHub Releases for updates (alpha channel: %s)...",
            "on" if include_alpha else "off",
        )
        try:
            releases = self._fetch_releases()
        except Exception as exc:
            logging.exception("Auto-updater: Exception checking for updates")
            if self._force:
                # A check the user asked for by hand gets an answer; the quick
                # retries below run as automatic checks and stay silent.
                wx.CallAfter(self._show_reinstall_error, str(exc))
            # At a Windows login the network is often not up yet when the first
            # check runs 15 s after launch; waiting the full retry interval
            # would put the next attempt hours away. A few quick retries first.
            attempt = self._fetch_failures
            self._fetch_failures += 1
            if attempt < len(self._FETCH_RETRY_DELAYS):
                self._schedule_retry(self._FETCH_RETRY_DELAYS[attempt])
            else:
                self._schedule_retry()
            return
        self._fetch_failures = 0

        data = select_release(releases, include_alpha)
        alpha = newer_excluded_alpha(releases, __version__, include_alpha)
        if alpha is not None:
            logging.info("Auto-updater: Newer alpha %s is excluded by the disabled alpha channel.",
                         alpha["tag_name"])
            if self._force:
                self._force = False
                language = self._mw.i18n.language if hasattr(self._mw, "i18n") else "pt-BR"
                changelog = whats_new_text(__version__, alpha, releases, language,
                                           self._translate, _fetch_text)
                self._queue_alpha_channel_choice(alpha, changelog)
                return
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
        signature_url  = _find_signature_asset(data.get("assets", []))
        release_is_alpha = is_alpha_release(data)

        local_version = __version__
        logging.info("Auto-updater: Local version is %s", local_version)

        if not is_newer(remote_version, local_version):
            logging.info("Auto-updater: No newer release is available in the selected channel.")
            if self._force:
                self._force = False
                wx.CallAfter(self._show_no_update)
            else:
                self._schedule_retry()
            return

        logging.info("Auto-updater: Newer version %s is available!", remote_version)
        was_forced = self._force
        self._force = False

        # Prefer a local, per-version changelog file (see resolve_changelog())
        # over the GitHub release body — only used as a last resort.
        # The language the window shows, not re-read from settings: that is
        # install-wide and another account may have changed it since (see
        # core.i18n.I18n.get_language()), and re-reading it on the window's own
        # instance switched every string drawn from here on.
        lang_code = self._mw.i18n.language if hasattr(self._mw, "i18n") else "pt-BR"
        changelog = whats_new_text(local_version, data, releases, lang_code,
                                   self._translate, _fetch_text)

        if not self._claim_prompt(remote_version):
            # Another account is already asking. Do NOT install behind its back
            # and do not stack a second dialog — just come back later, by which
            # time either the update happened or that dialog was dismissed and
            # the claim released.
            logging.info(
                "Auto-updater: another account is already showing the update "
                "prompt for this machine — skipping this one's dialog."
            )
            if was_forced:
                wx.CallAfter(self._show_prompt_open_elsewhere)
            else:
                self._schedule_retry()
            return

        wx.CallAfter(
            self._show_update_dialog, remote_version, changelog, zip_url, sha256sums_url,
            signature_url=signature_url, is_alpha=release_is_alpha,
        )

    def _show_no_update(self):
        i18n = self._mw.i18n
        wx.MessageBox(
            i18n.t("update_not_available"),
            i18n.t("update_not_available_title"),
            wx.OK | wx.ICON_INFORMATION,
            self._mw,
        )

    def _fetch_latest_release_for_reinstall(self):
        if getattr(self, "_channel_choice_pending", False) or getattr(self, "_install_pending", False):
            return
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

        wx.CallAfter(
            self._confirm_and_reinstall, remote_version, zip_url, sha256sums_url,
            signature_url=_find_signature_asset(data.get("assets", [])),
            is_alpha=is_alpha_release(data),
        )

    def _confirm_and_reinstall(self, remote_version: str, zip_url: str, sha256sums_url: str = "",
                               signature_url: str = "", is_alpha: bool = False):
        i18n = self._mw.i18n
        warning = ""
        if is_newer(__version__, remote_version):
            warning = i18n.t("update_reinstall_downgrade_warning").format(
                current=__version__, new=remote_version) + "\n\n"
        message = warning + i18n.t("force_reinstall_confirm_msg").format(version=remote_version)
        title = i18n.t("force_reinstall_confirm_title")
        if message_box(
            self._mw, message, title, wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING,
            announce=lambda: self._mw.output(f"{title}. {message}", interrupt=True),
        ) != wx.YES:
            return
        self._do_install(remote_version, zip_url, sha256sums_url, signature_url, is_alpha)

    def _show_reinstall_error(self, error_msg: str):
        i18n = self._mw.i18n
        message = i18n.t("update_error_msg").format(error=error_msg)
        title = i18n.t("update_error_title")
        message_box(self._mw, message, title, wx.OK | wx.ICON_ERROR,
                    announce=lambda: self._mw.output(f"{title}. {message}", interrupt=True))

    def _show_prompt_open_elsewhere(self):
        """Only for a check the user asked for by hand (Help > Check for
        updates). An automatic check that loses the claim stays silent and
        retries; a manual one that stayed silent would just look broken."""
        i18n = self._mw.i18n
        wx.MessageBox(
            i18n.t("update_prompt_open_elsewhere"),
            i18n.t("update_available_title"),
            wx.OK | wx.ICON_INFORMATION,
            self._mw,
        )

    _UI_NOT_READY_RETRY_MS = 2000

    def _show_update_dialog(self, remote_version: str, changelog: str, zip_url: str, sha256sums_url: str = "",
                            signature_url: str = "", is_alpha: bool = False):
        if getattr(self, "_install_pending", False):
            return  # A queued prompt must not release the active install's claim.
        if getattr(self._mw, "_shutting_down", False):
            # Quitting: no dialog on a dying app. The retry covers a shutdown
            # Windows cancelled, after which the app is still here.
            self._release_prompt()
            self._schedule_retry()
            return
        if not self._main_window_ready():
            # The check runs 15 s after launch, which a first start or a slow
            # machine spends inside the "starting the API" dialog. A second
            # modal loop on top of that one is where the app used to end up
            # windowless: answering it left the startup loop and the exit
            # path fighting over the same event loop, and the process lived
            # on with no window and the instance lock held. The prompt keeps
            # its claim and simply waits for the main window (other accounts'
            # prompts stay suppressed meanwhile; the retry is cheap and ends
            # with the launch either way).
            wx.CallLater(self._UI_NOT_READY_RETRY_MS, self._show_update_dialog,
                         remote_version, changelog, zip_url, sha256sums_url,
                         signature_url=signature_url, is_alpha=is_alpha)
            return
        dlg    = UpdateDialog(self._mw, remote_version, changelog)
        bring_to_front_if_hidden(
            self._mw, dlg,
            announce=lambda: self._mw.output(
                self._mw.i18n.t("update_available_msg").format(new_version=remote_version),
                interrupt=True))
        result = dlg.ShowModal()
        dlg.Destroy()

        if result == wx.ID_YES:
            # Deliberately still held across the install: releasing here would
            # let another account open its own dialog while this one is already
            # downloading and about to relaunch the whole install directory.
            # _do_install() releases it on every path that does not end in
            # real_exit() (which takes the claim's owner process with it, so a
            # crashed-owner recovery clears it for free).
            self._do_install(remote_version, zip_url, sha256sums_url, signature_url, is_alpha)
        else:
            # User said No — retry in 3 hours
            self._release_prompt()
            self._schedule_retry()

    def _main_window_ready(self) -> bool:
        """False while MainWindow.__init__ is still running (no window yet) or
        another modal flow (pairing) owns the screen — the same rule the
        WPPConnect update check follows."""
        ready = getattr(self._mw, "_ui_ready_event", None)
        if ready is not None and not ready.is_set():
            return False
        may_run = getattr(self._mw, "wpp_update_may_run_now", None)
        return may_run is None or bool(may_run())

    def _do_install(self, new_version: str, zip_url: str, sha256sums_url: str = "",
                    signature_url: str = "", is_alpha: bool = False, extracted_dir: str = ""):
        """Download and install, or — given *extracted_dir*, a package a
        background download already prepared — only install."""
        if getattr(self, "_install_pending", False):
            logging.info("Auto-updater: an installation is still settling — refusing a second attempt")
            return
        if not extracted_dir and getattr(self, "_background_version", ""):
            # A background download is under way (or waiting to install): a
            # forced reinstall must not start a second one beside it.
            self._say_background_update_running()
            return
        if not extracted_dir and background_downloads_enabled(
                getattr(self._mw, "settings", None)):
            # Settings > General: no progress window during the download. It
            # comes back here, with the package, once it is ready to install.
            self._download_in_background(new_version, zip_url, sha256sums_url,
                                         signature_url, is_alpha)
            return
        self._install_pending = True
        args = (new_version, zip_url, sha256sums_url, signature_url, is_alpha, extracted_dir)
        prog = None
        try:
            prog = UpdateProgressDialog(
                self._mw, new_version, self._mw, zip_url, sha256sums_url,
                signature_url=signature_url, is_alpha=is_alpha,
                extracted_dir=extracted_dir,
            )
            bring_to_front_if_hidden(self._mw, prog)
            result = prog.run()
        except Exception as exc:
            if prog is None:
                self._install_pending = False
                self._release_prompt()
                raise
            logging.exception("Auto-updater: update dialog ended unexpectedly")
            prog._handoff.cancel()
            if getattr(prog, "_thread", None) is None:
                prog._handoff.settled.set()
            prog._error_msg = str(exc)
            result = wx.ID_ABORT
        complete = lambda: UpdateChecker._complete_install(self, prog, result, args)
        defer = getattr(prog, "defer_until_settled", None)
        if defer is not None and defer(complete):
            return
        complete()

    def _complete_install(self, prog, result, args):
        """UI thread, only after the worker has released or handed off its token."""
        install_launched = bool(prog._install_ok)
        error_msg = prog._error_msg
        try:
            prog.Destroy()
        except RuntimeError:
            logging.info("Auto-updater: progress dialog was already destroyed")
        if install_launched:
            # Keep the in-process gate held through shutdown. CANCEL/ABORT can
            # be a stale modal answer, not an answer about the child process.
            self._mw.real_exit()
            return
        extracted_dir = args[-1]
        if result in (wx.ID_OK, wx.ID_CANCEL):
            self._install_pending = False
            discard_package(extracted_dir)
            self._release_prompt()
            if result == wx.ID_CANCEL:
                self._schedule_retry()
            return
        i18n = self._mw.i18n
        try:
            retry = message_box(
                self._mw, i18n.t("update_error_msg").format(error=error_msg),
                i18n.t("update_error_title"), wx.YES_NO | wx.ICON_ERROR,
                announce=lambda: self._mw.output(
                    i18n.t("update_error_msg").format(error=error_msg), interrupt=True),
            )
        finally:
            self._install_pending = False
        if retry != wx.YES:
            discard_package(extracted_dir)
            self._release_prompt()
            self._schedule_retry()
            return
        wx.CallAfter(self._do_install, *args)

    def _schedule_retry(self, interval: float = None):
        if interval is None:
            interval = self._RETRY_INTERVAL
        # A check that ends here ended without a prompt; a forced one must not
        # leave _force armed for the automatic retry, which would then answer
        # "no update available" to a check nobody asked for.
        self._force = False
        if self._retry_timer is not None:
            self._retry_timer.cancel()
        self._retry_timer = threading.Timer(interval, self._check_once)
        self._retry_timer.daemon = True
        self._retry_timer.start()

    def stop(self):
        """Cancel any pending retry timer."""
        with self._channel_choice_lock:
            self._channel_choice_cancelled = True
            self._channel_choice_token = None
            if self._channel_choice_pending:
                self._channel_choice_pending = False
                if not (getattr(self, "_install_pending", False)
                        or getattr(self, "_background_version", "")):
                    self._release_prompt()
        if self._retry_timer is not None:
            self._retry_timer.cancel()
            self._retry_timer = None


# ── WppUpdateChecker ───────────────────────────────────────────────────────────

class WppUpdateChecker:
    """
    Offers the WPPConnect Server version this WinZapp build was validated
    against (client/wpp_minimum_version.txt) when the installed server is older,
    and a newer release the maintainers approved for this build's pin
    (client/wpp_approved_versions.json on main, core/wpp_approved.py).

    By default it never asks GitHub for the newest upstream release: that is a
    version the patches may not have been ported to. Only the approved list can
    move a server past the pin, and the check (the one the startup gate cannot
    make in --background mode) offers the pin itself when the installed server
    is older. Settings > Advanced > "Show every WPPConnect Server update" opts
    back into the newest upstream release (_offered_tag(), core/wpp_upstream.py),
    asked about with a warning that the maintainers have not checked it.

    Unlike UpdateChecker above, accepting the prompt here doesn't just
    download a new WinZapp build — it stops the *running* API session,
    reinstalls it (reusing ApiSetupDialog's ZIP-download flow, no git
    required, works in a compiled build), and restarts it. That's why the
    first check is delayed well past startup by the caller (see
    MainWindow._start_wpp_update_checker): this must never fire while pairing
    or the initial sync is still settling.

    Version comparison uses packaging.version.Version (plain semver), not
    parse_version()/is_newer() above — those are tailored to WinZapp's own
    hybrid date/semver tag scheme, which wppconnect-server's plain-semver
    tags (e.g. "2.5.3") don't follow.
    """

    _RETRY_INTERVAL = 12 * 60 * 60  # 12 hours
    _PAIRING_RETRY_INTERVAL = 5 * 60  # 5 minutes
    #: Retry after an unexpected error in the check itself.
    _ERROR_RETRY_INTERVAL = 15 * 60

    def __init__(self, main_window):
        self._mw          = main_window
        self._retry_timer = None
        self._stopped = False
        self._timer_lock = threading.RLock()
        # A recommended release the user already said No to this session.
        self._declined_tag = None
        # force_reinstall() cancelled a pending periodic check, which its
        # worker must put back. False when there was none: with updates
        # turned off this checker exists only for Force Reinstall.
        self._resume_periodic = False
        # Owner-token of the per-machine WPPConnect prompt claim, or None.
        self._prompt_token = None

    def _global_dir(self):
        return getattr(self._mw, "global_dir", None) or None

    def _claim_prompt(self, version: str) -> bool:
        """One WPPConnect prompt per machine. Several autostarted accounts would
        each ask at 90 s, and accepting in one rewrites the shared api/ while
        another account's Node still runs from it. Fails open, like the
        WinZapp prompt's claim: a prompt that cannot be coordinated is worth
        more than one suppressed by a bug in the coordination."""
        gd = self._global_dir()
        if not gd:
            return True
        try:
            import update_coord
            token = update_coord.try_claim_update_prompt(
                gd, version, name=update_coord.WPP_PROMPT_FILE)
        except Exception:
            logging.exception("[WppUpdateChecker] prompt claim failed - asking anyway")
            return True
        if token is None:
            return False
        self._prompt_token = token
        return True

    def _release_prompt(self) -> None:
        """Never raises: it runs on the way out of a dialog."""
        token, self._prompt_token = self._prompt_token, None
        gd = self._global_dir()
        if not (gd and token):
            return
        try:
            import update_coord
            update_coord.release_update_prompt(gd, token, name=update_coord.WPP_PROMPT_FILE)
        except Exception:
            logging.exception("[WppUpdateChecker] releasing the prompt claim failed")

    def start(self):
        """Launch the first check in a background thread."""
        self._stopped = False
        t = threading.Thread(target=self._check_once, daemon=True)
        t.start()

    def force_check(self):
        """Called from a forced re-check (mirrors UpdateChecker.force_check)."""
        if self._retry_timer is not None:
            self._retry_timer.cancel()
            self._retry_timer = None
        self._stopped = False
        t = threading.Thread(target=self._check_once, daemon=True)
        t.start()

    def force_reinstall(self):
        """
        Called from Help > Force Reinstall WPPConnect. Skips the version
        comparison entirely — reinstalls the newest release this build
        supports (_reinstall_tag()) over the installed one, to recover a
        broken/corrupted API install without waiting for a real version
        bump to be detected.
        """
        if self._retry_timer is not None:
            self._retry_timer.cancel()
            self._retry_timer = None
            self._resume_periodic = True
        self._stopped = False
        t = threading.Thread(target=self._force_reinstall_worker, daemon=True)
        t.start()

    # ── Internal ──────────────────────────────────────────────────────────────

    @staticmethod
    def _pinned_tag() -> str:
        """The tag WinZapp is pinned to: the floor of every offer, and what
        Force Reinstall falls back to when nothing newer is approved.

        A user forcing a reinstall to repair a broken install must get a
        version the patches were written for; "" when no pin is bundled (a dev
        checkout), which the caller reports as an error instead of guessing.
        """
        return homologated_wpp_tag(resource_path("wpp_minimum_version.txt"))

    @staticmethod
    def _approved_tag(pin: str):
        """(tag, reachable): the newest release approved for *pin*, "" when
        none is newer than it; *reachable* is False when the list could not be
        read, which the caller treats as "nothing approved" and checks again
        sooner. Never raises."""
        if not pin:
            return "", True
        from config import GITHUB_REPO
        from core import wpp_approved
        try:
            manifest = wpp_approved.fetch_manifest(GITHUB_REPO, f"WinZapp/{__version__}")
            entries = wpp_approved.parse_manifest(manifest)
        except Exception as exc:
            logging.warning("[WppUpdateChecker] Could not read the approved "
                            "WPPConnect Server list: %s", type(exc).__name__)
            return "", False
        return wpp_approved.best_approved(entries, pin, _winzapp_satisfies), True

    def _reinstall_tag(self) -> str:
        """The tag Force Reinstall installs: the newest release approved for
        this build, else the pin. Never the newest upstream release, and never
        below the pin; "" with no pin (a dev checkout), reported as an error."""
        pin = self._pinned_tag()
        if not pin:
            return ""
        return self._approved_tag(pin)[0] or pin

    def _offered_tag(self, pin: str, approved: str) -> str:
        """The release a *recommended* update may offer above the pin: the
        approved one, or, with Settings > Advanced > "Show every WPPConnect
        Server update" on (core/wpp_upstream.py), the newer of it and the
        newest upstream release. "" when there is none; wpp_update_target()
        still never offers anything below the pin.

        Never the target of Force Reinstall (_reinstall_tag()) or of a
        required update: a repair, or an install below the pin, must land on
        a release the patches were written for (docs/traps/updater-channels.md)."""
        from core import wpp_upstream
        if not wpp_upstream.enabled(getattr(getattr(self, "_mw", None), "settings", None)):
            return approved
        latest = wpp_upstream.fetch_latest_tag(f"WinZapp/{__version__}")
        return wpp_upstream.newest_tag(approved, latest) or approved

    def _check_once(self):
        if getattr(self, "_stopped", False):
            return
        try:
            logging.info("[WppUpdateChecker] Checking for wppconnect-server updates...")
            installed = self._mw._get_installed_wpp_version()
            if not installed:
                # Not installed yet (or version unreadable) — the normal
                # first-run setup / version-gate flow owns that case, not this
                # checker.
                self._schedule_retry()
                return

            minimum = homologated_wpp_tag(resource_path("wpp_minimum_version.txt"))
            if not minimum:
                # A dev checkout: no pin, nothing to compare, nothing approved.
                return
            approved, reachable = self._approved_tag(minimum)
            # Below the pin the update is required, and goes to the pin or an
            # approved release only. Above it, the recommended offer may also
            # be the newest upstream release when the user asked for that.
            target = wpp_update_target(installed, minimum, approved)
            if target is None or not target[1]:
                upstream = wpp_update_target(installed, minimum,
                                             self._offered_tag(minimum, approved))
                if upstream is not None and not upstream[1]:
                    target = upstream
            if target is None:
                # The approved list can grow while this process runs; an
                # unreachable one (no network yet at a Windows login) is read
                # again sooner.
                logging.info("[WppUpdateChecker] wppconnect-server needs no update (%s).", installed)
                self._schedule_retry(None if reachable else self._ERROR_RETRY_INTERVAL)
                return
            tag, required = target
            if required and tag == self._declined_tag and tag != minimum:
                # The approved release this required update aimed at already
                # failed here this session: offer the pin, which this build was
                # tested with, instead of the same failing install every 12 h.
                tag = minimum
            remote_version = tag.lstrip("vV")

            if not required and tag == self._declined_tag:
                # Said No to this very release earlier in the session; the 12 h
                # retry must not ask again. A required update is never silenced.
                logging.info("[WppUpdateChecker] %s was declined this session - not asking again.", tag)
                self._schedule_retry()
                return

            if self._other_account_busy():
                logging.info("[WppUpdateChecker] another account's WPPConnect Server is "
                             "running - not offering %s from this account.", tag)
                self._schedule_retry()
                return

            # Neither the pin nor an approved release: only offered because the
            # user asked for every upstream release, and said so in the prompt.
            vetted = tag in (minimum, approved)
            logging.info(
                "[WppUpdateChecker] wppconnect-server %s: %s -> %s",
                "update required" if required
                else "approved update available" if vetted
                else "unapproved upstream update available",
                installed, remote_version,
            )
            wx.CallAfter(self._prompt_update, installed, remote_version, tag, required, vetted)
        except Exception:
            logging.exception("[WppUpdateChecker] Unexpected error during wppconnect-server update check")
            self._schedule_retry(self._ERROR_RETRY_INTERVAL)

    def _prompt_update(self, installed: str, remote_version: str, tag: str, required: bool = True,
                       vetted: bool = True):
        if getattr(self._mw, "_shutting_down", False):
            return   # quitting: no dialog on a dying app (UpdateChecker does the same)
        if (getattr(self._mw, "_wpp_updating", False)
                or getattr(self._mw, "_wpp_staging", None)):
            # An update (this one's retry, a forced reinstall, or one being
            # built in the background) is mid-flight.
            self._schedule_retry(self._PAIRING_RETRY_INTERVAL)
            return
        if not self._mw.wpp_update_may_run_now():
            logging.info(
                "[WppUpdateChecker] Pairing in progress — not prompting for "
                "the %s update yet.", remote_version,
            )
            self._schedule_retry(self._PAIRING_RETRY_INTERVAL)
            return
        if self._other_account_busy():
            # Nothing to ask: the update would be refused after a Yes, and the
            # same prompt would come back. Stay silent until the next check.
            logging.info("[WppUpdateChecker] another account's WPPConnect Server is "
                         "running - not prompting for %s.", remote_version)
            self._schedule_retry()
            return
        if not self._claim_prompt(remote_version):
            logging.info("[WppUpdateChecker] another account is already asking about "
                         "the %s update - checking again soon.", remote_version)
            self._schedule_retry(self._PAIRING_RETRY_INTERVAL)
            return

        # Held from here: any exception below must hand the claim back, or no
        # account would be asked again until this process exits. Only a started
        # update keeps it, and releases it itself when it ends.
        handed_over = False
        try:
            i18n = self._mw.i18n
            # Below the minimum this build was validated against: the mandatory
            # gate's wording (ensure_wpp_version, which a background start
            # skips). At or above it the update is an approved one, only
            # recommended.
            if required:
                title = i18n.t("api_update_outdated_title")
                message = i18n.t("api_update_outdated_message").format(
                    current=installed, required=remote_version)
            else:
                title = i18n.t("wpp_update_available_title")
                message = i18n.t(
                    "wpp_update_available_msg" if vetted else "wpp_update_unvetted_msg"
                ).format(current=installed, new=remote_version)
            # wx.NO_DEFAULT: this can pop up while the user is typing a message,
            # and Space is how NVDA/JAWS/Narrator users activate the focused
            # button — defaulting to Yes risked reinstalling the API session
            # from an accidental keystroke instead of a deliberate choice.
            if message_box(
                self._mw, message, title,
                wx.YES_NO | wx.NO_DEFAULT | wx.ICON_INFORMATION,
                announce=lambda: self._mw.output(f"{title}. {message}", interrupt=True),
            ) == wx.YES:
                started = self._mw._update_wpp_server(
                    tag, on_finished=lambda ok: self._update_finished(tag, ok))
                if started is False:
                    # Refused after a Yes (another account's Node came up, or an
                    # update is mid-flight): do not ask again for this release
                    # this session, the refusal already explained itself.
                    if not required:
                        self._declined_tag = tag
                    self._schedule_retry()
                else:
                    handed_over = True
            else:
                if not required:
                    self._declined_tag = tag
                self._schedule_retry()
        finally:
            if not handed_over:
                self._release_prompt()

    def _update_finished(self, tag: str, ok: bool) -> None:
        """The accepted update ended. The claim is handed back either way; a
        failed one is remembered for the session so an approved release that
        does not install is not re-attempted every 12 hours (a required
        update is still asked about again, it is never silenced)."""
        if not ok:
            self._declined_tag = tag
        self._release_prompt()
        self._schedule_retry()

    def _other_account_busy(self) -> bool:
        from core.wa_version_refresh import other_accounts_node_alive
        return other_accounts_node_alive(
            self._global_dir(), getattr(self._mw, "account_id", None), ignore_corrupt=True)

    def _force_reinstall_worker(self):
        if getattr(self, "_stopped", False):
            return
        try:
            logging.info("[WppUpdateChecker] Force-reinstall requested - using the "
                         "newest release this build supports.")
            tag = self._reinstall_tag()
            if not tag:
                wx.CallAfter(self._show_force_reinstall_error)
                return
            remote_version = tag.lstrip("vV")
            wx.CallAfter(self._confirm_force_reinstall, remote_version, tag)
        except Exception:
            logging.exception("[WppUpdateChecker] Unexpected error during forced reinstall check")
            if not getattr(self, "_stopped", False):
                wx.CallAfter(self._show_force_reinstall_error)
        finally:
            # The periodic timer force_reinstall() cancelled is how a release
            # approved later in this session reaches the user. Only that one
            # comes back: a checker that had none (updates turned off, or the
            # startup check not started yet) must not begin prompting.
            if getattr(self, "_resume_periodic", False):
                self._resume_periodic = False
                self._schedule_retry()

    def _show_force_reinstall_error(self):
        if getattr(self, "_stopped", False) or getattr(self._mw, "_shutting_down", False):
            return
        i18n = self._mw.i18n
        title = i18n.t("update_error_title")
        message = i18n.t("wpp_update_fetch_failed_msg")
        message_box(self._mw, message, title, wx.OK | wx.ICON_ERROR,
                    announce=lambda: self._mw.output(f"{title}. {message}", interrupt=True))

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
        with self._timer_lock:
            if self._stopped:
                return
            if interval is None:
                interval = self._RETRY_INTERVAL
            if self._retry_timer is not None:
                self._retry_timer.cancel()
            timer = threading.Timer(interval, self._check_once)
            # Also tolerate reentrant stop from a timer factory/hook before
            # publication. A concurrent stop waits for this same lock.
            if self._stopped:
                timer.cancel()
                return
            self._retry_timer = timer
            timer.daemon = True
            timer.start()

    def stop(self):
        """Cancel any pending retry timer and hand the prompt claim back, so a
        quitting account does not hold the machine's WPPConnect prompt."""
        with self._timer_lock:
            self._stopped = True
            if self._retry_timer is not None:
                self._retry_timer.cancel()
                self._retry_timer = None
        self._release_prompt()
