"""WppServerMixin — part of MainWindow (see main_window/__init__.py).

Moved verbatim out of main.py. Methods run with ``self`` bound to the
MainWindow instance, so every attribute set in MainWindow.__init__ is
available here.
"""

import socket as _socket
import atexit
import logging
import os
import shutil
import subprocess
import sys
import time
import uuid
import wx
from main_window.runtime_setup import (
    NPM_HEALTH_MARKER_NAME,
    migrate_legacy_api_state,
    node_runtime_needs_download,
    shorten_windows_path,
)
from core.wpp_runtime import (
    WPPCONNECT_PACKAGE,
    read_homologated_wpp_version,
    wppconnect_library_drift,
)
from main_window.win32_helpers import _get_short_path_name
from core.api_client import (
    api_get,
    api_post,
)
from core import browser_payload
from app_paths import resource_path


class WppServerMixin:
    """The local WPPConnect Server process: browser payload, npm modules, version
    pin, ports, background start, stop and ensure_wpp_running.
    """

    # ── First-run module installation ──────────────────────────────────────

    # Full Chrome is deliberately preferred on Windows: chrome-headless-shell
    # is a console-subsystem binary and its renderer children can open visible
    # console windows. Puppeteer's headless mode keeps full Chrome's GUI hidden.
    _HEADLESS_SHELL_NAMES = ("chrome-headless-shell.exe", "chrome-headless-shell")
    _WINDOWS_CHROME_NAMES = ("chrome.exe",)

    def find_headless_shell(self):
        """Path to a *usable* browser inside client/api/.cache, or None.

        Usable, not merely present. This used to return the first matching
        executable it walked past, and that is the check every "is the API set
        up?" path relies on — so an install whose payload is incomplete was
        accepted forever. Measured on 2026-09-10: a Chromium missing
        `icudtl.dat` aborted with STATUS_BREAKPOINT before it could report
        anything, on every session start, while this logged "Already
        installed" on every launch. See core/browser_payload.py for what is
        checked and why the check is deliberately narrow.

        A binary that fails the check is skipped rather than returned, so the
        walk keeps looking: a cache holding both a broken version directory
        and a good one still starts.
        """
        cache_dir = resource_path("api", ".cache")
        if not os.path.isdir(cache_dir):
            return None
        preferred_names = (
            self._WINDOWS_CHROME_NAMES
            if sys.platform == "win32"
            else self._HEADLESS_SHELL_NAMES
        )
        for root, _dirs, files in os.walk(cache_dir):
            for name in files:
                if name not in preferred_names:
                    continue
                candidate = os.path.join(root, name)
                problem = browser_payload.payload_problem(candidate)
                if problem:
                    logging.warning(
                        "[headless-shell] ignoring an incomplete browser at %s "
                        "(%s) — it cannot start, so it does not count as "
                        "installed.", candidate, problem,
                    )
                    continue
                return candidate
        return None

    def iter_incomplete_browsers(self):
        """Every browser in the cache that exists but cannot start.

        Kept apart from find_headless_shell() because the two answer opposite
        questions and one caller needs both: "have I got a browser?" and "is
        the reason I have not got one that a broken one is sitting in its
        place?". Yields (binary_path, problem).

        All of them, not the first: nothing ever removes an old version
        directory, so a cache can hold several, and repairing only one leaves
        the next launch doing this again.
        """
        cache_dir = resource_path("api", ".cache")
        if not os.path.isdir(cache_dir):
            return
        preferred_names = (
            self._WINDOWS_CHROME_NAMES
            if sys.platform == "win32"
            else self._HEADLESS_SHELL_NAMES
        )
        try:
            for root, _dirs, files in os.walk(cache_dir):
                for name in files:
                    if name not in preferred_names:
                        continue
                    candidate = os.path.join(root, name)
                    problem = browser_payload.payload_problem(candidate)
                    if problem:
                        yield candidate, problem
        except OSError:
            return

    def find_incomplete_browser(self):
        """The first browser in the cache that exists but cannot start, or
        (None, None). A convenience over iter_incomplete_browsers()."""
        for candidate in self.iter_incomplete_browsers():
            return candidate
        return None, None

    def browser_payload_blocks_startup(self):
        """Is a broken browser the reason nothing can start?

        Not the same question as "is there a broken browser". A cache holding
        a damaged version directory AND a working one starts perfectly well —
        find_headless_shell() skips the damaged one and keeps walking, and it
        is reachable straight out of this class's own repair path: a delete
        that antivirus blocks leaves the old directory behind while puppeteer
        installs a fresh, complete one beside it.

        Answering "yes, broken" there would be wrong in an expensive way. The
        one caller that reads this refuses to recover the WhatsApp profile on
        it, so a healthy install would silently lose profile recovery for the
        rest of its life over a directory nothing is using.

        Returns (binary_path, problem), or (None, None) when a usable browser
        exists or nothing is wrong.
        """
        if self.find_headless_shell():
            return None, None
        return self.find_incomplete_browser()

    @staticmethod
    def _clear_broken_browser_dir(version_dir: str) -> bool:
        """Get an unusable browser out of @puppeteer/browsers' way.

        Deleting is the intent; getting the directory out of the *path* is the
        requirement, and those come apart exactly when it matters. A plain
        rmtree() stops at the first entry it cannot remove — and the reason it
        cannot is usually the same antivirus that damaged the payload, still
        holding a handle. That leaves the tree half-deleted AND the version
        directory still present, which is precisely the condition this exists
        to break: the installer skips its download outright while that
        directory exists, so the install ends up emptier than it started and
        stays that way for good, one file per launch.

        So: sweep what will go, and if anything survives, rename the directory
        aside. Windows renames a directory holding a locked .exe where it
        refuses to delete it, so the rename succeeds in the very case the
        delete fails. Same idea as `<profile>.broken` in profile_recovery.py —
        keep the evidence, free the name.

        Never raises: this runs on the startup path, and a browser that cannot
        be tidied is still a reason to try the download, not to give up.
        """
        try:
            if not os.path.isdir(version_dir):
                return True
            shutil.rmtree(version_dir, ignore_errors=True)
            if not os.path.isdir(version_dir):
                return True
            aside = "%s.broken.%d" % (version_dir, os.getpid())
            shutil.rmtree(aside, ignore_errors=True)
            os.replace(version_dir, aside)
            logging.warning(
                "[headless-shell] %s could not be deleted (something is holding "
                "it open) — moved to %s so the download is not skipped.",
                version_dir, aside,
            )
            return True
        except OSError as exc:
            logging.error(
                "[headless-shell] could not clear %s: %s — the download below "
                "will probably be skipped.", version_dir, exc,
            )
            return False

    def ensure_headless_shell_installed(self) -> bool:
        """Download chrome-headless-shell if client/api/.cache has none.

        This exists because of *where* the download would otherwise happen.
        start.js has its own execSync fallback, but that runs inside the API
        process at boot — so the download eats ApiStartupDialog's 300 s budget
        while the server has not started listening yet, and a slow connection
        turns a one-off download into "WinZapp failed to start the API".
        Running it here instead puts it before that timer starts.

        Who actually hits this: not fresh installs (ApiSetupDialog step 4.5
        downloads the shell), but everyone *updating* from a build that shipped
        full Chrome. Their client/api/.cache already holds a chrome.exe, so
        every "is the API set up?" check above passes and nothing would fetch
        the shell — the browser is the one piece of the install those checks
        never looked at.

        Best-effort by design: a failure here is logged and start.js's own
        fallback still gets its turn. Returns True when a shell is present
        afterwards.
        """
        existing = self.find_headless_shell()
        if existing:
            logging.info("[headless-shell] Already installed: %s", existing)
            return True

        # Nothing usable — but "nothing usable" and "nothing there" are
        # different, and the difference decides whether the download below can
        # help at all. @puppeteer/browsers skips its install outright while the
        # version directory exists, so a payload that is present and broken
        # would survive every retry for the life of the install. Take it out
        # of the way first, and say so: this is the one line that tells a user
        # (or a log) that the browser, not WhatsApp, is what is wrong.
        cache_dir = resource_path("api", ".cache")
        cleared = set()
        for broken, problem in self.iter_incomplete_browsers():
            version_dir = browser_payload.installed_version_dir(broken, cache_dir)
            if not version_dir or version_dir in cleared:
                continue
            cleared.add(version_dir)
            logging.error(
                "[headless-shell] the installed browser cannot start (%s): %s "
                "— clearing %s so it can be downloaded again.",
                problem, broken, version_dir,
            )
            self._clear_broken_browser_dir(version_dir)

        browser_product = "chrome" if sys.platform == "win32" else "chrome-headless-shell"
        logging.info(
            "[headless-shell] %s not found in api/.cache — downloading it now "
            "(before the API startup timer begins).", browser_product
        )
        if sys.platform == "win32":
            node_exe = resource_path("node", "node.exe")
            npm_cli = resource_path("node", "node_modules", "npm", "bin", "npm-cli.js")
            npm_cmd = [node_exe, npm_cli]
            node_dir = resource_path("node")
            path_env = node_dir + os.pathsep + os.environ.get("PATH", "")
        else:
            local_node = resource_path("node", "node")
            node_exe = local_node if os.path.isfile(local_node) else (shutil.which("node") or "node")
            local_npm = resource_path("node", "node_modules", "npm", "bin", "npm-cli.js")
            npm_cmd = [node_exe, local_npm] if os.path.isfile(local_npm) else [shutil.which("npm") or "npm"]
            node_dir = os.path.dirname(node_exe) if os.path.isabs(node_exe) else ""
            path_env = (node_dir + os.pathsep + os.environ.get("PATH", "")) if node_dir else os.environ.get("PATH", "")

        api_dir = resource_path("api")
        if not os.path.isdir(api_dir):
            logging.info("[headless-shell] api/ not present yet — skipping.")
            return False

        npm_env = {
            **os.environ,
            "PATH": path_env,
            "PUPPETEER_CACHE_DIR": resource_path("api", ".cache"),
        }
        creation_flags = 0
        if sys.platform == "win32" and hasattr(subprocess, "CREATE_NO_WINDOW"):
            creation_flags = subprocess.CREATE_NO_WINDOW
        try:
            proc = subprocess.Popen(
                npm_cmd + ["exec", "puppeteer", "browsers", "install", browser_product],
                cwd=api_dir,
                env=npm_env,
                creationflags=creation_flags,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout_bytes, stderr_bytes = proc.communicate(timeout=1800)
            if proc.returncode != 0:
                logging.error(
                    "[headless-shell] install failed (exit %s): %s",
                    proc.returncode,
                    (stderr_bytes or b"").decode("utf-8", errors="replace")[:500],
                )
            else:
                logging.info(
                    "[headless-shell] installed: %s",
                    (stdout_bytes or b"").decode("utf-8", errors="replace").strip()[:300],
                )
        except Exception as exc:
            logging.error("[headless-shell] install error: %s", exc)

        found = self.find_headless_shell()
        if not found:
            logging.warning(
                "[headless-shell] still missing after the install attempt — start.js "
                "will retry, and falls back to any full Chrome already in the cache."
            )
        return bool(found)

    def ensure_api_modules_installed(self):
        """Ensure the API is installed, then that a headless browser exists.

        Split in two on purpose: every check in _ensure_api_modules_installed()
        looks at api/ (dist/server.js, node_modules) and none of them look at
        the browser, so "the API is fully set up" was true for an install with
        no browser at all — see ensure_headless_shell_installed().
        """
        self._ensure_api_modules_installed()
        self.ensure_headless_shell_installed()

    def _ensure_api_modules_installed(self):
        """
        Ensure the WPPConnect is cloned, compiled, and has its node_modules.

        node/node.exe is mandatory in all scenarios — it is the portable Node.js
        runtime bundled with WinZapp that drives both npm and the API itself.
        Its absence is always a fatal error.

        Depending on what is present inside api/:

          dist/server.js absent →  API not yet cloned/compiled (or the whole
                                    api/ folder was deleted). Show
                                    ApiSetupDialog, which clones + npm installs
                                    + builds. Expected state for a fresh
                                    install or first developer run.

          dist/server.js present
          node_modules absent   →  API already cloned/built, just node_modules
                                    is missing — the normal state of every
                                    fresh WinZapp.zip extract, since
                                    node_modules isn't bundled. Still shows
                                    ApiSetupDialog (the ONE setup dialog this
                                    app has — used to be a second, separately
                                    titled ModuleInstallDialog doing
                                    practically the same thing, which was
                                    confusing and had its own bugs), which
                                    detects dist/server.js already exists and
                                    runs only the npm-install portion of its
                                    flow internally.

          Both present          →  Nothing to do.

        In background mode dialogs are never shown; if the setup is incomplete
        the process exits silently.
        """
        import sys
        import shutil
        if sys.platform == "win32":
            node_exe = resource_path("node", "node.exe")
        else:
            local_node = resource_path("node", "node")
            if os.path.isfile(local_node):
                node_exe = local_node
            else:
                node_exe = shutil.which("node") or "node"

        dist_server  = resource_path("api",  "dist", "server.js")
        node_modules = resource_path("api",  "node_modules")

        # start.js ships bundled with WinZapp itself — it is NOT fetched from
        # WPPConnect's own repo by either install flow below. Its absence
        # means this WinZapp installation itself is incomplete or corrupted
        # (e.g. a partial/interrupted ZIP extraction), not just "WPPConnect
        # hasn't been cloned yet" — attempting either install flow would not
        # fix it (ApiSetupDialog only ever downloads WPPConnect's own source)
        # and would just fail confusingly deep inside npm/WPPConnect startup
        # instead. Fail fast with a clear, actionable message instead of
        # trying anything.
        #
        # api/.env is deliberately NOT checked here: nothing reads it. The
        # WPPConnect side has its dotenv load commented out (api/src/index.ts),
        # start.js takes its settings from config.json plus the environment
        # variables _start_wpp_background() injects (AUTHENTICATION_API_KEY,
        # PORT, ...), and api/.gitignore excludes it so it never shipped in
        # the ZIP either. Requiring it only ever aborted startup on a
        # perfectly good install.
        start_js = resource_path("api", "start.js")
        if not os.path.isfile(start_js):
            logging.error(
                "[ensure_api_modules_installed] Missing required WinZapp file "
                "api/start.js — installation appears incomplete.",
            )
            if not self.background_mode:
                wx.MessageBox(
                    self.i18n.t("api_files_missing_error"),
                    self.i18n.t("error").format(app_name=self.app_name),
                    wx.OK | wx.ICON_ERROR,
                )
            sys.exit(1)

        # Node.js is mandatory. An existing portable binary can also be too
        # old for the WPPConnect release (npm only warns on engines mismatch,
        # then runtime features fail later), so upgrade it before starting.
        # The whole verdict is node_runtime_needs_download()'s: it is pure
        # decision logic on top of two subprocess probes, and the only way to
        # test it is to reach it without a wx.Frame around it.
        if sys.platform == "win32":
            node_needs_download, installed_node_version = node_runtime_needs_download(
                node_exe,
                resource_path("node", "node_modules", "npm", "bin", "npm-cli.js"),
                resource_path("node", NPM_HEALTH_MARKER_NAME),
            )
        else:
            node_needs_download = not os.path.isfile(node_exe)
            installed_node_version = ""
        if node_needs_download:
            if self.background_mode:
                logging.error(
                    "[ensure_api_modules_installed] Node.js missing/outdated (%s) "
                    "and cannot show download dialog in background mode",
                    installed_node_version or "missing",
                )
                sys.exit(0)
            logging.info(
                "[ensure_api_modules_installed] Node.js missing/outdated (%s) — "
                "downloading the homologated portable version...",
                installed_node_version or "missing",
            )
            from ui.dialogs.node_download import NodeDownloadDialog
            def _show_node_download():
                dlg = NodeDownloadDialog(self)
                res = dlg.ShowModal()
                dlg.Destroy()
                return res
            result = self.run_on_main_thread(_show_node_download)
            if result != wx.ID_OK:
                sys.exit(1)
            # Re-resolve path after download
            if sys.platform == "win32":
                node_exe = resource_path("node", "node.exe")
            # If still missing after download, abort
            if not os.path.isfile(node_exe):
                logging.error("[ensure_api_modules_installed] Node.js download failed — node.exe still missing")
                sys.exit(1)

        # Detect and clean legacy node_modules from WPPConnect to force a clean install of WPPConnect
        wpp_marker = os.path.join(node_modules, "@wppconnect-team")
        if os.path.isdir(node_modules) and not os.path.isdir(wpp_marker):
            logging.info("[ensure_api_modules_installed] Legacy node_modules detected. Cleaning for WPPConnect...")
            try:
                import shutil
                shutil.rmtree(node_modules, ignore_errors=True)
            except Exception as e:
                logging.error("[ensure_api_modules_installed] Failed to remove legacy node_modules: %s", e)

        # ── Check for new required packages in an existing node_modules ──────
        # When we add a new npm dependency (e.g. @ffmpeg-installer/ffmpeg) the
        # user's node_modules is already installed from a previous run, so the
        # normal "node_modules absent" gate never fires. We compare a list of
        # required package markers and run `npm install` silently in the
        # background if any are missing — no dialog needed.
        # Resolved rather than just probed for a directory: @ffmpeg-installer
        # unpacks its binary under a platform-specific subfolder, and WinZapp
        # also accepts a bundled lib/ copy or one on PATH — so "is the package
        # folder there?" is the wrong question, "can we actually find an
        # ffmpeg to run?" is the right one.
        ffmpeg_bin = self._find_api_ffmpeg()
        _REQUIRED_MARKERS = [
            os.path.join(node_modules, "@babel", "runtime"),
        ]
        if os.path.isfile(dist_server) and os.path.isdir(node_modules):
            missing = [m for m in _REQUIRED_MARKERS if not os.path.isdir(m)]
            # Without ffmpeg, _convert_wav_to_ogg() returns None and voice
            # messages go out as raw WAV, which WhatsApp often rejects — a core
            # accessibility feature degrading silently, with only a warning in
            # the log. node_modules existing is not evidence this specific
            # package inside it does, so it gets its own check.
            if not ffmpeg_bin:
                missing.append(os.path.join(node_modules, "@ffmpeg-installer", "ffmpeg"))
            if missing:
                logging.info(
                    "[ensure_api_modules_installed] Missing packages detected: %s — running npm install",
                    missing,
                )
                if sys.platform == "win32":
                    node_exe = resource_path("node", "node.exe")
                    npm_cli  = resource_path("node", "node_modules", "npm", "bin", "npm-cli.js")
                    npm_cmd  = [node_exe, npm_cli]
                    node_dir = resource_path("node")
                    path_env = node_dir + os.pathsep + os.environ.get("PATH", "")
                else:
                    local_node = resource_path("node", "node")
                    if os.path.isfile(local_node):
                        node_exe = local_node
                    else:
                        node_exe = shutil.which("node") or "node"
                    local_npm = resource_path("node", "node_modules", "npm", "bin", "npm-cli.js")
                    if os.path.isfile(local_npm):
                        npm_cmd = [node_exe, local_npm]
                    else:
                        npm_cmd = [shutil.which("npm") or "npm"]
                    node_dir = os.path.dirname(node_exe) if os.path.isabs(node_exe) else ""
                    path_env = (node_dir + os.pathsep + os.environ.get("PATH", "")) if node_dir else os.environ.get("PATH", "")

                npm_env  = {
                    **os.environ,
                    "PATH": path_env,
                    # client/api/.cache — the tree start.js searches for
                    # chrome-headless-shell and re-exports as PUPPETEER_CACHE_DIR.
                    # Pointing at the .cache/puppeteer subfolder instead makes
                    # this installer download into one tree while the server
                    # looks in another.
                    "PUPPETEER_CACHE_DIR": resource_path("api", ".cache"),
                }
                api_dir  = resource_path("api")
                creation_flags = 0
                if sys.platform == "win32" and hasattr(subprocess, "CREATE_NO_WINDOW"):
                    creation_flags = subprocess.CREATE_NO_WINDOW

                try:
                    proc = subprocess.Popen(
                        npm_cmd + ["install", "--no-audit", "--no-fund", "--include=optional", "--legacy-peer-deps"],
                        cwd=api_dir,
                        env=npm_env,
                        creationflags=creation_flags,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                    )
                    _, stderr_bytes = proc.communicate()
                    if proc.returncode != 0:
                        logging.error(
                            "[ensure_api_modules_installed] npm install failed: %s",
                            (stderr_bytes or b"").decode("utf-8", errors="replace"),
                        )
                    else:
                        logging.info("[ensure_api_modules_installed] npm install completed OK")
                        # This npm install (unlike the main ApiSetupDialog
                        # flow) rebuilt node_modules outside that dialog, so
                        # nothing else re-applies WinZapp's node_modules-level
                        # patches (decrypt.js, host.layer.js pairing-code fix)
                        # afterwards — without this they'd silently regress
                        # every time this "missing package" repair path runs.
                        try:
                            from ui.dialogs.api_setup import ApiSetupDialog
                            ApiSetupDialog._apply_node_modules_patches(api_dir)
                        except Exception as exc:
                            logging.warning(
                                "[ensure_api_modules_installed] Failed to apply node_modules patches: %s", exc
                            )
                except Exception as exc:
                    logging.error("[ensure_api_modules_installed] npm install error: %s", exc)
            else:
                # No new package was missing, so npm install above never ran —
                # but WinZapp's own node_modules-level patches (host.layer.js
                # pairing-code fix, status.layer.js posting-result fix,
                # sender.layer.js sendFile error-detail fix, ...) can still be
                # stale on their own: a WinZapp update that only ADDS or
                # CHANGES one of these patches, without adding a new npm
                # dependency, never reruns npm install, so the patch-reapply
                # above (which only fires as a side effect of that install)
                # never fires either — an already-fully-installed node_modules
                # would otherwise only pick it up via a full API reinstall.
                # Each _patch_* method is idempotent (a cheap string search,
                # no-op if already applied), so checking on every startup is
                # safe.
                try:
                    from ui.dialogs.api_setup import ApiSetupDialog
                    api_dir = resource_path("api")
                    ApiSetupDialog._apply_node_modules_patches(api_dir)
                except Exception as exc:
                    logging.warning(
                        "[ensure_api_modules_installed] Failed to verify/reapply node_modules patches: %s", exc
                    )
            return

        # Everything already set up — nothing to do.
        if os.path.isfile(dist_server) and os.path.isdir(node_modules):
            return

        if self.background_mode:
            sys.exit(0)

        # One dialog for both cases — it detects internally whether
        # dist/server.js already exists and runs only the npm-install portion
        # of its flow when so, instead of a second dialog for that case.
        from ui.dialogs.api_setup import ApiSetupDialog
        def _show_api_setup():
            dlg = ApiSetupDialog(self)
            res = dlg.ShowModal()
            dlg.Destroy()
            return res
        result = self.run_on_main_thread(_show_api_setup)

        if result != wx.ID_OK:
            sys.exit(0)

    # ── WPPConnect version gate ───────────────────────────────────────────────

    def _read_wpp_minimum_version(self) -> str:
        """Read the minimum WPPConnect Server version this build was tested
        against from the bundled wpp_minimum_version.txt (plain text, just
        the version string) — a committed file, copied next to the exe by
        build.py, and absent only from an install that predates it.

        It used to be written by build-windows.yml out of whatever
        client/api/ happened to hold, which made it an *output* of the build:
        self-consistent, and unable to disagree with anything. It is an input
        now, and the workflow verifies it instead.

        This used to live as a WPP_MINIMUM_VERSION key inside a bundled
        client/.env file, which meant every build (even ones with no other
        use for it) shipped a .env alongside the exe. A dedicated file
        carries the exact same one value without needing a whole
        key=value config file format, or the general dotenv-override
        mechanism (config.py's own _load_dotenv(), which still lets a user
        manually drop a real .env next to the exe for WINZAPP_GITHUB_REPO —
        that stays; it's just never build-injected any more).
        """
        return read_homologated_wpp_version(resource_path("wpp_minimum_version.txt"))

    def _get_installed_wpp_version(self) -> str:
        """Read the WPPConnect Server version from api/package.json."""
        pkg_path = resource_path("api", "package.json")
        try:
            with open(pkg_path, encoding="utf-8") as fh:
                import json as _json
                pkg = _json.load(fh)
            return pkg.get("version", "")
        except Exception:
            return ""


    def _server_version_below_minimum(self):
        """(installed, minimum) when the WPPConnect Server itself is too old,
        else None.

        Worth knowing what this can and cannot see: both numbers come out of
        the release ZIP — api/package.json's own "version" field and
        wpp_minimum_version.txt — so an update rewrites the two together and
        they agree by construction afterwards. This catches an install that
        has NOT been updated (a server left behind by an older WinZapp, a
        hand-built api/), never a drift the update itself introduced. That
        second case is _wppconnect_library_drift()'s, and it is the one that
        was going unnoticed.
        """
        minimum = self._read_wpp_minimum_version()
        installed = self._get_installed_wpp_version() if minimum else ""
        if not minimum or not installed:
            return None  # Nothing pinned, or unreadable — skip silently
        return (installed, minimum) if self._version_is_below(installed, minimum) else None

    def _wppconnect_library_drift(self):
        """(installed, pinned) when node_modules holds a wppconnect other than
        the one api/package.json pins, else None.

        The whole reasoning lives in core/wpp_runtime.wppconnect_library_drift()
        — this only supplies the api/ path and never lets a failure here stop
        the server from starting.
        """
        try:
            return wppconnect_library_drift(resource_path("api"))
        except Exception:
            logging.exception("[ensure_wpp_version] library drift check failed")
            return None

    @staticmethod
    def _version_is_below(installed: str, minimum: str) -> bool:
        """
        Return True when *installed* is strictly older than *minimum*.
        Handles standard semver and pre-release suffixes (e.g. "2.4.0-rc2").
        Returns False on any parsing error so the check never blocks startup
        due to an unexpected version string format.
        """
        if not installed or not minimum:
            return False
        try:
            from packaging.version import Version
            return Version(installed) < Version(minimum)
        except Exception:
            return False

    def ensure_wpp_version(self):
        """
        Two independent checks, either of which offers the same repair:

        * the WPPConnect Server itself older than this build's minimum
          (_server_version_below_minimum());
        * node_modules holding a wppconnect other than the one
          api/package.json pins (_wppconnect_library_drift()) — the drift an
          update introduces on its own, because the release ZIP ships
          dist/server.js and package.json but NOT node_modules, and which
          silently un-patches the pairing-code path.

        If the installed version is older the user is prompted to:
          • Update now   — re-download + rebuild via ApiSetupDialog, then continue
          • Exit         — terminate WinZapp
          • Continue     — proceed without updating (not recommended)

        The check is skipped when:
          - Running in background mode (no UI)
          - api/package.json is absent (setup not done yet)
          - wpp_minimum_version.txt is not bundled (plain dev checkout)
        """
        if self.background_mode:
            return

        # dist/server.js, which is what `npm run build` actually produces and
        # what package.json's start script runs.
        #
        # This read `dist/main.js` for as long as the check has existed, and
        # WPPConnect Server has never built a file by that name — so the guard
        # was always false, the method always returned here, and the whole
        # outdated-version prompt below has never run for anyone. Found by a
        # user who set client/wpp_minimum_version.txt to a newer release,
        # restarted, and watched WinZapp come up on the old one without a word.
        #
        # Note what that means for the code below: it is being reached for the
        # first time now, not merely fixed.
        dist_server = resource_path("api", "dist", "server.js")
        if not os.path.isfile(dist_server):
            return  # API not installed yet — setup dialog will handle it

        outdated = self._server_version_below_minimum()
        drifted = self._wppconnect_library_drift()

        if outdated:
            installed, minimum = outdated
        elif drifted:
            # The server itself is fine; what is wrong is the library it runs
            # on. Same prompt, same repair — the reinstall runs npm install,
            # which brings node_modules to the pinned version, and re-applies
            # the node_modules patches against source they will now match.
            installed, minimum = drifted
            logging.warning(
                "[ensure_wpp_version] node_modules holds %s %s but "
                "api/package.json pins %s — the compiled-output patches are "
                "matched against the pinned version's source, so offering the "
                "reinstall.", WPPCONNECT_PACKAGE, installed, minimum,
            )
        else:
            return  # Server and library both as expected — nothing to do

        # ── Something is older/other than what this build expects ─────────────
        from ui.dialogs.api_version_check import (
            ApiVersionOutdatedDialog,
            RESULT_UPDATE, RESULT_EXIT, RESULT_CONTINUE,
        )

        def _show_outdated_dlg():
            dlg = ApiVersionOutdatedDialog(self, self.i18n, installed, minimum)
            res = dlg.ShowModal()
            dlg.Destroy()
            return res
        result = self.run_on_main_thread(_show_outdated_dlg)

        if result == RESULT_EXIT:
            sys.exit(0)

        if result == RESULT_CONTINUE:
            return  # Proceed with the outdated version — user's choice

        # RESULT_UPDATE: re-download and rebuild using the minimum-version tag.
        #
        # As a TAG, not the bare version. `minimum` is what
        # read_homologated_wpp_version() returns — "2.10.18" — while the
        # release is tagged "v2.10.18", and ApiSetupDialog drops forced_tag
        # straight into .../archive/refs/tags/{tag}.zip. Passing the bare
        # number built a 404 URL, so this button could never have worked.
        # Nobody found out because the guard at the top of this method named a
        # file WPPConnect Server does not build, so nothing below it ever ran.
        # homologated_wpp_tag() is the shared helper every other install path
        # already uses for exactly this conversion.
        from core.wpp_runtime import homologated_wpp_tag
        from ui.dialogs.api_setup import ApiSetupDialog
        minimum_tag = homologated_wpp_tag(resource_path("wpp_minimum_version.txt"))
        if not minimum_tag and outdated:
            # Only the server branch may fall back to `minimum` here: it IS a
            # server version. The library branch's is a wppconnect version
            # ("2.3.3"), and there is no wppconnect-server release tagged
            # v2.3.3 — passing it would build a 404 archive URL, the same
            # failure the comment above describes. With no tag at all,
            # ApiSetupDialog resolves the latest release itself, which is the
            # right answer when we cannot name a better one.
            minimum_tag = f"v{minimum.lstrip('vV')}"
        def _show_update_dlg():
            update_dlg = ApiSetupDialog(
                self,
                title_override=self.i18n.t("api_update_dialog_title"),
                forced_tag=minimum_tag,
            )
            res = update_dlg.ShowModal()
            update_dlg.Destroy()
            return res
        update_result = self.run_on_main_thread(_show_update_dlg)

        if update_result != wx.ID_OK:
            # Update was cancelled or failed — exit to avoid running an
            # incompatible API version
            sys.exit(0)

    # ── WPPConnect lifecycle ─────────────────────────────────────────────────

    def _peer_node_ports(self) -> list[int]:
        """Node ports OTHER accounts have persisted, so this account allocates a
        distinct one. Best-effort: reads each peer account's settings.json
        connection.wpp_port via the registry; any unreadable peer is skipped."""
        ports: list[int] = []
        registry = getattr(self, "registry", None)
        acc_id = getattr(self, "account_id", None)
        if registry is None:
            return ports
        try:
            import json as _json
            import node_ports
            for acc in registry.list():
                aid = acc.get("id")
                if not aid or aid == acc_id:
                    continue
                try:
                    sp = os.path.join(registry.data_dir_for(aid), "settings.json")
                    with open(sp, "r", encoding="utf-8") as f:
                        p = _json.load(f).get("connection", {}).get("wpp_port")
                    p = node_ports.sanitize_saved_port(p)
                    if p is not None:
                        ports.append(p)
                except Exception:
                    continue
        except Exception:
            logging.exception("[node-port] peer port enumeration failed (non-fatal)")
        return ports

    @staticmethod
    def _is_port_free(port: int) -> bool:
        try:
            with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", port))
                return True
        except OSError:
            return False

    def _ensure_wpp_port_still_free(self):
        """Re-verify self.wpp_port right before Node actually spawns, and
        re-resolve if something already grabbed it.

        _resolve_wpp_port() checks/reserves the port during __init__, but
        the real Node spawn (_start_wpp_background(), this method's only
        caller) happens much later in startup — settings/UI/sync all run in
        between, a real gap of actual wall-clock time, not a single atomic
        step. Anything can take that exact port in that window: another
        program, or even this same account's own previous Node still
        winding down. Node would then simply fail to bind it, surfaced only
        as a generic "API failed to start in time" with an EADDRINUSE
        buried in wppconnect.log. Only re-checks in the same case
        _resolve_wpp_port() itself allocates a port for — a custom/remote
        API's port, or single-account/legacy mode, is left untouched.
        """
        conn = self.settings.get("connection", {})
        if conn.get("wpp_custom_api"):
            return
        if getattr(self, "registry", None) is None or getattr(self, "account_id", None) is None:
            return
        if self._is_port_free(self.wpp_port):
            return

        import node_ports
        from coord_locks import node_port_lock
        logging.warning(
            "[node-port] port %s no longer free right before starting Node "
            "— re-resolving", self.wpp_port,
        )
        with node_port_lock(self.global_dir):
            # allocate_port_for_account(), not resolve_account_port(): the
            # latter deliberately keeps a valid saved port even when
            # is_free() is momentarily False (see its own docstring) — the
            # right call for a quick restart racing our own still-dying
            # Node, but here we already know the port is genuinely taken
            # and need a real, different one.
            new_port = node_ports.allocate_port_for_account(
                self.account_id, self._peer_node_ports(), is_free=self._is_port_free,
            )
            self.settings.setdefault("connection", {})["wpp_port"] = new_port
            try:
                self.save_settings()
            except Exception:
                logging.exception(
                    "[node-port] persisting re-resolved port failed (non-fatal)"
                )
        # Ask the question that matters — "is the port we settled on actually
        # free?" — rather than "did it change?".
        #
        # allocate_port_for_account() never raises: with every port in its range
        # busy it returns deterministic_port(account_id). That is NOT necessarily
        # the port we came here to escape — a legacy install whose saved port is
        # 6300 gets 6341 back, a different number that is equally occupied, so an
        # equality test misses the exhaustion it was written for. And it fires
        # when nothing is wrong: node_port_lock is a cross-process lock, so real
        # time passes between the pre-lock probe and the allocation, which is
        # exactly the window in which our own dying Node releases the port — the
        # allocator then hands the same number back through its ordinary
        # is_free() path, and an equality test calls a perfectly healthy startup
        # a failure in a log users are asked to send in.
        if not self._is_port_free(new_port):
            logging.error(
                "[node-port] account %s: port %s is taken and no free port was "
                "available — Node will fail to bind",
                self.account_id, new_port,
            )
        else:
            logging.info("[node-port] account %s → re-resolved WPPConnect port %s",
                         self.account_id, new_port)
        self.wpp_port = new_port

    def _resolve_wpp_port(self, conn: dict) -> int:
        """Resolve THIS account's stable WPPConnect Node port.

        Custom API: honour the user's configured port untouched. Otherwise keep
        a valid saved port (stable Node/userDataDir across launches), or allocate
        the lowest free port not used by a peer on first run — then persist it so
        it never drifts. This is what gives each account its own isolated Node.
        """
        import node_ports
        if conn.get("wpp_custom_api"):
            saved = conn.get("wpp_port")
            return saved if isinstance(saved, int) and not isinstance(saved, bool) else node_ports.BASE_PORT
        # Single-account / legacy fallback (no registry context): behave as before.
        if getattr(self, "registry", None) is None or getattr(self, "account_id", None) is None:
            return node_ports.sanitize_saved_port(conn.get("wpp_port")) or node_ports.BASE_PORT

        # Two accounts can start simultaneously. Keep peer discovery, free-port
        # selection and persistence in one cross-process critical section so
        # they cannot both claim the same port.
        from coord_locks import node_port_lock
        with node_port_lock(self.global_dir):
            port = node_ports.resolve_account_port(
                self.account_id,
                conn.get("wpp_port"),
                self._peer_node_ports(),
                is_free=self._is_port_free,
            )
            # Persist so the choice is stable next launch.
            if conn.get("wpp_port") != port:
                self.settings.setdefault("connection", {})["wpp_port"] = port
                try:
                    self.save_settings()
                except Exception:
                    logging.exception(
                        "[node-port] persisting resolved port failed (non-fatal)"
                    )
        logging.info("[node-port] account %s → WPPConnect port %s",
                     getattr(self, "account_id", None), port)
        return port

    def _is_wpp_running(self):
        """Return True if the WPPConnect is already listening on the configured server/port."""
        import urllib.parse
        try:
            parsed = urllib.parse.urlparse(self.wpp_server)
            host = parsed.hostname or "127.0.0.1"
            port = parsed.port or self.wpp_port
            with _socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            return False

    def _start_wpp_background(self):
        """
        Launch the bundled WPPConnect Server node process in the background.
        stdout and stderr are redirected to api/wppconnect.log so that startup
        errors can be shown to the user if the port never opens.
        Does nothing if the node or start.js files are not present (dev mode).

        When the current process is elevated (run as Administrator) the child
        is spawned using the non-elevated linked token via CreateProcessWithTokenW
        so that PostgreSQL's initdb can start (it refuses to run as root/admin).
        """
        self._ensure_wpp_port_still_free()
        import sys
        import shutil

        if sys.platform == "win32":
            node_exe = resource_path("node", "node.exe")
        else:
            local_node = resource_path("node", "node")
            if os.path.isfile(local_node):
                node_exe = local_node
            else:
                node_exe = shutil.which("node") or "node"

        start_js = resource_path("api", "start.js")
        patched_start_js = resource_path("api_patches", "start.js")
        try:
            if os.path.isfile(patched_start_js):
                with open(patched_start_js, "rb") as source:
                    patched_content = source.read()
                current_content = b""
                if os.path.isfile(start_js):
                    with open(start_js, "rb") as current:
                        current_content = current.read()
                if current_content != patched_content:
                    shutil.copy2(patched_start_js, start_js)
                    logging.info("[startup] Restored current api_patches/start.js")
        except Exception as exc:
            logging.warning("[startup] Could not restore api_patches/start.js: %s", exc)
        if not os.path.isfile(node_exe) or not os.path.isfile(start_js):
            return  # Not bundled — developer runs WPPConnect separately
        try:
            from app_paths import log_path
            self._wpp_log_path = log_path("wppconnect.log")
            try:
                if os.path.exists(self._wpp_log_path):
                    prev = self._wpp_log_path + ".1"
                    if os.path.exists(prev):
                        os.remove(prev)
                    os.replace(self._wpp_log_path, prev)
            except Exception as e:
                logging.info("[startup] could not rotate wppconnect.log: %s", e)
            log_fh = open(self._wpp_log_path, "w",
                          encoding="utf-8", errors="replace")
            # Use the short (8.3) path so PostgreSQL's initdb doesn't choke on
            # accented characters in the install path (e.g. "Área de Trabalho").
            cwd = _get_short_path_name(resource_path("api"))
            self.wpp_process = None

            # Guarantee that the child Node process inherits the correct API key
            # regardless of whether the local start.js or .env has been preserved.
            os.environ["AUTHENTICATION_API_KEY"] = self.wpp_api_key
            os.environ["WPP_LID_MODE"] = "false"
            os.environ["PORT"] = str(self.wpp_port)
            os.environ["PUPPETEER_CACHE_DIR"] = resource_path("api", ".cache")

            # Multi-account: expose OUR identity to the Node instance so any
            # WinZapp process can verify (via GET /winzapp/identity) that the
            # server on the port is the one we started, and recover its pid
            # (plan Zad 3.0/3.1b). Best-effort — never blocks a legacy start.
            try:
                import node_coord
                gd = getattr(self, "global_dir", None)
                if gd:
                    self._node_instance_id = getattr(self, "_node_instance_id", None) or uuid.uuid4().hex
                    os.environ["WINZAPP_INSTALLATION_ID"] = node_coord.installation_id(gd)
                    os.environ["WINZAPP_INSTANCE_ID"] = self._node_instance_id
            except Exception:
                logging.exception("[startup] node identity env injection failed (non-fatal)")

            # WPPConnect's own defaults for the auth token store and the
            # Chrome profile (customUserDataDir) are RELATIVE paths, resolved
            # against the Node process's cwd (resource_path("api")). That is
            # stable in --onedir/dev, but in --onefile it is PyInstaller's
            # per-launch extraction temp dir — a fresh folder every launch —
            # so anything written there is silently orphaned the moment the
            # process exits, and the next launch's empty tokens/userDataDir
            # folder finds nothing (WhatsApp then correctly asks for a fresh
            # QR: the "closed WinZapp, relaunched, told the device was
            # disconnected" report). Pointing both at an absolute,
            # install-writable location (via config.ts/fileTokenStory.ts,
            # which read these two env vars) fixes this for every build mode.
            try:
                # self.global_dir, not a "..", ".." walk up from data_path():
                # this root is intentionally SHARED across accounts —
                # WPPConnect isolates each session under its own
                # userDataDir/<session_name> subfolder — and a relative
                # walk-up silently breaks if the accounts/<id>/ nesting depth
                # ever changes.
                _persistent_api_dir = os.path.join(self.global_dir, "api")
                _token_dir = os.path.join(_persistent_api_dir, "tokens")
                _udd = os.path.join(_persistent_api_dir, "userDataDir")
                # The long forms go in FIRST and unconditionally. Everything
                # below this line can raise, and a launch that reaches Node
                # with neither variable set is the worst outcome available:
                # config.ts falls back to './userDataDir/' relative to Node's
                # cwd, which in a --onefile build is PyInstaller's per-launch
                # temp dir, so the paired session is orphaned on exit. Being
                # long is survivable; being unset is not.
                os.environ["WINZAPP_USER_DATA_DIR"] = _udd + os.sep
                os.environ["WINZAPP_TOKEN_STORE_DIR"] = _token_dir
                # One-time move of an ALREADY PAIRED install's session state
                # from the old cwd-relative location. Without it, every
                # existing user is pointed at an empty folder on their first
                # launch after this change and is asked to pair again - the
                # very symptom the new path exists to remove. Runs here, with
                # Node not yet spawned, because moving a userDataDir out from
                # under a live Chrome would corrupt it.
                migrate_legacy_api_state(resource_path("api"), _persistent_api_dir)
                # Only NOW may these be created. migrate_legacy_api_state()
                # skips any folder that already exists at the destination —
                # deliberately, since one being there means this install is
                # already on the new location — and then writes its
                # run-once marker even when nothing needed moving. Creating
                # them beforehand therefore does not just skip the move, it
                # makes the skip PERMANENT: an already-paired install is
                # pointed at a virgin profile, asked to pair again, and the
                # real one is stranded under resource_path("api") with the
                # marker guaranteeing no retry. That is the exact report the
                # migration exists to prevent, delivered by its own fix.
                #
                # They have to exist at all because shorten_windows_path()
                # wraps a Win32 call that resolves a real directory entry and
                # returns the long path untouched for one that is not there.
                os.makedirs(_udd, exist_ok=True)
                os.makedirs(_token_dir, exist_ok=True)
                # Shorten only the ANCESTOR, never the whole path: 8.3
                # rewrites every component over 8 characters, and
                # "userDataDir" is 11. Losing that literal breaks the two
                # places that identify a session's Chrome by matching it in a
                # command line — chrome_cmdline_owns_session()
                # (client/connection_state.py) and forceKillByUserDataDir()
                # (api_patches/src/util/createSessionUtil.ts) — and both fail
                # by matching NOTHING, so a stale Chrome holding the profile
                # lock is never killed and the session hangs in INITIALIZING.
                # Keeping the component costs 3 characters of the 43 saved.
                os.environ["WINZAPP_USER_DATA_DIR"] = os.path.join(
                    shorten_windows_path(_persistent_api_dir), "userDataDir"
                ) + os.sep
                os.environ["WINZAPP_TOKEN_STORE_DIR"] = shorten_windows_path(_token_dir)
            except Exception:
                logging.exception("[startup] persistent userDataDir/token-store env injection failed (non-fatal)")

            # Ensure dist/config.js has useChrome:false so WPPConnect always uses
            # Puppeteer's own bundled Chrome/Chromium instead of searching for a
            # system Chrome installation. Patched here at runtime so existing users
            # with a pre-built dist/ benefit immediately without a full rebuild.
            try:
                _dist_cfg = resource_path("api", "dist", "config.js")
                if os.path.isfile(_dist_cfg):
                    with open(_dist_cfg, "r", encoding="utf-8") as _f:
                        _cfg_src = _f.read()
                    if "useChrome" not in _cfg_src:
                        _cfg_src = _cfg_src.replace(
                            "createOptions: {",
                            "createOptions: { useChrome: false,",
                            1,
                        )
                        with open(_dist_cfg, "w", encoding="utf-8") as _f:
                            _f.write(_cfg_src)
                        logging.info("[startup] Patched dist/config.js: useChrome → false")
            except Exception as _e:
                logging.warning("[startup] Could not patch dist/config.js: %s", _e)

            # WPPConnect uses Puppeteer/Chrome which already includes --no-sandbox
            # in its config (see api/src/config.ts), so Chrome runs correctly even
            # when the parent process is elevated.  De-elevation via the Safer API
            # is therefore not needed and would prevent Node.js from writing session
            # tokens/cache to the installation directory, breaking admin users.
            creation_flags = 0
            if sys.platform == "win32" and hasattr(subprocess, "CREATE_NO_WINDOW"):
                creation_flags = subprocess.CREATE_NO_WINDOW

            self.wpp_process = subprocess.Popen(
                [node_exe, "--max-old-space-size=4096", start_js],
                cwd=cwd,
                creationflags=creation_flags,
                stdout=log_fh,
                stderr=log_fh,
            )
            # Release Python's file handle now that node.exe has inherited it.
            # This avoids a double-lock on wppconnect.log so an update extraction
            # can overwrite the file once WinZapp exits (only node.exe holds a
            # lock while it is running — we don't need it on the Python side).
            log_fh.close()
            self._wpp_log_fh = None
            atexit.register(self._stop_wpp_server)

            # Register our per-account node-lease so shutdown coordination knows
            # this account is a live client of the shared Node (plan Zad 3.1b).
            self._register_node_lease()
        except Exception:
            pass

    def _register_node_lease(self):
        """Register this account's node-lease on the shared WPPConnect Node.

        A node-lease is the marker "this account still needs the shared Node";
        _stop_wpp_server() only kills the Node when no OTHER account holds a live
        lease. This MUST run on BOTH startup paths — the one that spawns the Node
        AND the one that adopts an already-running Node (ensure_wpp_running()
        early-returns when the port is already open). Registering only on spawn
        left every adopting account leaseless, so when the account that spawned
        the Node exited, the shutdown guard saw no other live lease and tree-killed
        the shared server out from under the still-running account — which then
        polled QRCODE and wiped its own database. Idempotent (atomic overwrite).
        """
        try:
            import node_coord
            import update_coord
            gd = getattr(self, "global_dir", None)
            acc_id = getattr(self, "account_id", None)
            if gd and acc_id:
                _pid = os.getpid()
                _ct = update_coord._default_proc_create_time(_pid) or 0.0
                node_coord.add_node_lease(gd, acc_id, pid=_pid, create_time=_ct)
                logging.info("[node-lease] registered lease for account %s (pid=%s)", acc_id, _pid)
        except Exception:
            logging.exception("[node-lease] registration failed (non-fatal)")

    # Emitted by WPPConnect (controllers/browser.js) when the WhatsApp Web build it
    # pins is not present in the installed @wppconnect/wa-version package. It is a
    # plain log line, not an error the API ever returns, so nothing downstream sees
    # it — yet it is the direct predecessor of a nasty silent failure: WhatsApp Web
    # then serves its newest build, which the bundled wa-js may not support, and
    # 1:1 sends start failing inside the browser (isSendFailure, ack 0) while the
    # REST call still answers 200 and groups keep working.
    _WPP_VERSION_FALLBACK_MARKER = "using latest as fallback"

    def _check_wpp_version_pin(self):
        """Warn when WPPConnect could not pin the WhatsApp Web version.

        Reads the WPPConnect log written during this startup. Best-effort: any
        problem reading it is logged and otherwise ignored, since this is a
        diagnostic, never a reason to block startup.
        """
        log_file = getattr(self, "_wpp_log_path", None)
        if not log_file or not os.path.isfile(log_file):
            return
        try:
            with open(log_file, "r", encoding="utf-8", errors="replace") as fh:
                # The marker is printed during browser init, well inside the first
                # few dozen lines; reading the whole file would drag in megabytes.
                head = fh.read(200_000)
        except Exception as exc:
            logging.warning("[startup] Could not read %s to check the version pin: %s",
                            log_file, exc)
            return

        for line in head.splitlines():
            if self._WPP_VERSION_FALLBACK_MARKER in line:
                logging.error(
                    "[startup] WPPConnect could not pin the WhatsApp Web version and fell back "
                    "to the live build: %s | Sending to individual contacts may fail silently "
                    "(groups keep working). Fix: npm update @wppconnect/wa-version in client/api/.",
                    line.strip(),
                )
                wx.CallAfter(self.output, self.i18n.t("wpp_version_unpinned_warning"))
                return
        logging.info("[startup] WhatsApp Web version pin OK (no fallback reported by WPPConnect).")

    def _stop_wpp_server(self, budget: float = None):
        """Terminate the WPPConnect Server process and all its children.

        `budget` caps the WHOLE teardown, in seconds, by shrinking each phase's
        own timeout to whatever is left. Pass it only when something else owns
        the deadline — Windows during WM_ENDSESSION (_WINDOWS_SHUTDOWN_BUDGET).
        A normal quit passes nothing and keeps the generous per-phase timeouts,
        which only ever elapse when something is genuinely wrong.

        Ordering matters (multi-account): FIRST gracefully close THIS account's
        own WhatsApp session (browser.close() → WhatsApp Web flushes its session
        state to userDataDir), THEN decide the shared Node's fate. Previously the
        node-lease guard returned BEFORE close-session, so a process that left
        the shared Node up for other accounts never closed its own session
        gracefully — and when the LAST account later killed the Node with
        `taskkill /F /T`, every still-open Chrome (including sessions that were
        CONNECTED) was hard-killed mid-flight. WhatsApp Web treats that abrupt
        kill as a broken link, so the account came back to a pairing screen on
        next launch (reported live: closed both windows seconds apart, one
        account then demanded re-pairing).

        Each account now runs its own Node on its own port, so we only ever kill
        the Node on THIS account's port.

        Should not be called on the wx main thread when avoidable: step 1 can
        block for the whole grace budget, and a blocked main thread stops the
        message loop from pumping (Windows tags it "Not Responding"). Exactly
        two callers do anyway, both deliberately: _update_wpp_server(), which
        needs to show a modal dialog immediately after, and _on_end_session(),
        where returning from the handler is what lets Windows kill us — and
        that one passes an explicit `budget` to cap the wait. Every other
        caller (real_exit's teardown thread, _ipc_quit's own thread) runs
        this off the main thread; check that before adding a new one.
        """
        if budget is None:
            # Skipped under a budget: this wait sits outside it, so honoring
            # it during WM_ENDSESSION could add its full 5s on top of a 4s
            # budget — past what triggers a "Not Responding" kill.
            self._yield_to_in_progress_self_restart()
        # STEP 1: gracefully close our own session so its state is persisted,
        # regardless of whether we go on to stop the Node or leave it up. This
        # must happen for EVERY closing process, not just the last one.
        deadline = (time.monotonic() + budget) if budget else None

        def _phase_timeout(default: float) -> float:
            """This phase's timeout, clipped to what the overall budget has
            left. Never returns 0: every phase must get at least one real
            attempt, otherwise a tight budget silently degrades into the
            no-wait kill this routine exists to avoid."""
            if deadline is None:
                return default
            return max(0.5, min(default, deadline - time.monotonic()))

        token = getattr(self, "token", "")
        proc = getattr(self, "wpp_process", None)
        browser_closed_cleanly = False
        session_name = (token or "").split(":")[0]
        # Log the REAL session status BEFORE close-session. This is the missing
        # piece: WPPConnect's closeSession force-kills (no auth flush) any session
        # whose status is not exactly CONNECTED/open, which corrupts the profile
        # into 'Session Unpaired' on next launch. If a failing account shows a
        # non-CONNECTED status here, that force-kill path is the culprit.
        pre_status = self._raw_session_status() if token else "(no token)"
        self._shutdown_audit(f"_stop_wpp_server START account={getattr(self,'account_id','?')} "
                             f"session={session_name!r} port={getattr(self,'wpp_port','?')} "
                             f"pre_close_status={pre_status!r}")
        if token:
            # Decided from two independent signals, not one: pre_status is a
            # single HTTP probe that returns "" on ANY failure (timeout,
            # non-200, connection error), and a transient hiccup on exactly
            # that probe would silently skip the token-persist wait below for
            # a session that really was CONNECTED. self._wa_connected is a
            # race-free second opinion updated continuously by
            # _set_wa_connected(). Either saying "yes" is enough — a false
            # positive costs a few extra seconds of wait, a false negative
            # costs a lost/corrupted session.
            session_was_connected = (
                pre_status in ("CONNECTED", "open")
                or getattr(self, "_wa_connected", False)
            )
            try:
                url = (
                    f"{self.wpp_server}:{self.wpp_port}"
                    f"/api/{token}/close-session"
                )
                logging.info("[shutdown] close-session sent — waiting for flush")
                self._shutdown_audit("close-session POSTed — waiting for flush")
                resp = api_post(
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=_phase_timeout(self._WPP_GRACEFUL_STOP_SECONDS),
                )
                if resp.status_code == 200:
                    logging.info("[_stop_wpp_server] WPPConnect accepted the close-session.")
                else:
                    logging.warning(
                        "[_stop_wpp_server] close-session returned HTTP %s — "
                        "Chrome may not have closed cleanly.",
                        resp.status_code,
                    )
                # Wait for WPPConnect to finish its own teardown before we
                # taskkill the Node. This is the first of TWO gates — the second
                # (wait_for_profile_release, further down) is the one that says
                # Chrome stopped writing. Neither substitutes for the other.
                if self._wait_for_session_flushed(
                    token, timeout=_phase_timeout(self._SHUTDOWN_FLUSH_TIMEOUT)
                ):
                    browser_closed_cleanly = True
                    logging.info("[shutdown] session flushed cleanly (CLOSED)")
                    self._shutdown_audit("FLUSH OK — session reached CLOSED")
                else:
                    logging.warning("[shutdown] session did not confirm CLOSED "
                                    "before timeout — proceeding to stop Node")
                    self._shutdown_audit("FLUSH FAIL — timed out, killing Node anyway "
                                         "(risk of Session Unpaired next launch)")
            except Exception as e:
                logging.warning(
                    "[_stop_wpp_server] close-session request failed or timed out (%s) — "
                    "Chrome may still be running.",
                    e,
                )
                self._shutdown_audit(f"close-session EXCEPTION ({e!r})")

            # The literal "session is OK but the app is closing — do not let
            # it fully close until the session is properly stored"
            # requirement: runs UNCONDITIONALLY here, whether the try above
            # succeeded or raised, as long as this run ever looked
            # connected. Previously this lived inside the try block, so a
            # close-session timeout/exception -- precisely the case where
            # Chrome is most likely still mid-write -- skipped the wait
            # entirely and went straight to taskkill. Regardless of whether
            # the flush wait above already succeeded, since that polls the
            # BROWSER's status, not the separate token file this app does
            # not otherwise verify. Skipped entirely for a session that was
            # never actually connected: there is nothing meaningful to wait
            # for, same "nothing to lose" reasoning _on_disconnect() already
            # applies to an unpaired account.
            if session_was_connected:
                self._wait_for_token_persisted(token)

        # STEP 2: stop OUR OWN Node. Each account now runs its own Node on its
        # own port (revised architecture), so there is no shared server to spare
        # and no cross-account lease to check — we only ever kill the Node on
        # THIS account's port. Release our lease (kept purely for diagnostics /
        # orphan attribution) and tear our Node down.
        gd = getattr(self, "global_dir", None)
        acc_id = getattr(self, "account_id", None)
        if gd and acc_id:
            try:
                import node_coord
                node_coord.release_node_lease(gd, acc_id)
                logging.info("[node-lease] released lease for account %s", acc_id)
            except Exception:
                logging.exception("[shutdown] lease release failed (non-fatal)")

        # STEP 1.5 (custom API / shared server): after closing OUR OWN session,
        # sweep the server for ORPHANED sessions — ones whose token exists but
        # that are not CONNECTED (QR pending, abandoned, or left over from a
        # crash). On a shared/custom server those otherwise pile up as live
        # Chrome processes forever (observed: 22 chrome-headless shells after a
        # few pairings). Never touch another account's ACTIVE session: we only
        # close sessions whose own status is not connected.
        if getattr(self, "wpp_custom_api", False):
            try:
                self._close_orphaned_server_sessions()
            except Exception:
                logging.exception("[shutdown] orphaned-session sweep failed (non-fatal)")

        pid = None
        if proc and proc.poll() is None:
            pid = proc.pid
        elif proc is None:
            # This session never spawned WPPConnect itself — it found the port
            # already open (e.g. a previous session was force-quit and its
            # node.exe never got killed). Locate the orphaned process by the
            # port it's listening on so it doesn't leak across restarts.
            pid = self._find_pid_listening_on_port(self.wpp_port)

        if pid:
            if not browser_closed_cleanly:
                logging.warning(
                    "[_stop_wpp_server] Force-killing WPPConnect (and any Chrome still "
                    "running under it) — the graceful close-session above didn't confirm "
                    "success, so its profile may not have finished flushing."
                )
            if session_name:
                if self.wait_for_profile_release(
                    session_name, timeout=_phase_timeout(15.0)
                ):
                    try:
                        released_fp = self._login_store_fingerprint(session_name)
                    except Exception:
                        released_fp = "unknown"
                    self._shutdown_audit(
                        "Chrome released the profile before the kill "
                        f"login_store={released_fp}")
                    self._capture_profile_snapshot(session_name,
                                                   browser_closed_cleanly, budget)
                else:
                    self._shutdown_audit(
                        "Chrome STILL held the profile — killing anyway, its "
                        "leveldb may be incomplete")
            self._shutdown_audit(f"taskkill /F /T node pid={pid} (flush done above)")
            try:
                import sys
                if sys.platform == "win32":
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(pid)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
                    )
                elif proc is not None:
                    proc.terminate()
            except Exception:
                if proc is not None:
                    try:
                        proc.terminate()
                    except Exception:
                        pass
        else:
            self._shutdown_audit("no node pid to kill (proc gone / port free)")

    def _close_orphaned_server_sessions(self):
        """Close orphaned sessions on a shared/custom WPPConnect server.

        Called on shutdown when wpp_custom_api is active. A session is orphaned
        when its token exists server-side but its connection status is NOT
        connected (QR pending, abandoned, or leftover from a crash) — such
        sessions keep a live Chrome process on the server forever otherwise.

        We only ever close sessions that report a non-connected status, so an
        ACTIVE session of another account is never touched. Runs on the
        shutdown worker thread; best-effort, never raises.
        """
        base = f"{self.wpp_server}:{self.wpp_port}"
        api_key = getattr(self, "wpp_api_key", "") or ""
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        current = (getattr(self, "token", "") or "").split(":")[0]
        try:
            resp = api_get(
                f"{base}/api/{api_key}/show-all-sessions",
                headers=headers, timeout=10,
            )
            if resp.status_code not in (200, 201):
                logging.info("[orphan-sweep] show-all-sessions HTTP %s — skipping",
                             resp.status_code)
                return
            data = resp.json()
            sessions = data.get("response", []) if isinstance(data, dict) else []
        except Exception as exc:
            logging.info("[orphan-sweep] could not list server sessions: %s", exc)
            return

        closed = 0
        for sess in sessions:
            # Each entry is a session NAME (token id). getAllTokens returns
            # the stored token keys; guard both dict and plain-string shapes.
            name = sess.get("session") if isinstance(sess, dict) else str(sess)
            if not name:
                continue
            if name == current:
                continue  # ours — already closed in STEP 1
            # Check the session's real status: only close non-connected ones.
            try:
                st = api_get(
                    f"{base}/api/{name}/check-connection-session",
                    headers=headers, timeout=8,
                )
                payload = st.json() if st.headers.get("content-type", "").startswith("application/json") else {}
                status = (payload.get("status") if isinstance(payload, dict) else None)
                if status is True:
                    continue  # genuinely connected — another account's live session
            except Exception:
                continue  # unreadable status → leave it alone (fail-safe)
            try:
                api_post(
                    f"{base}/api/{name}/close-session",
                    headers=headers, timeout=8,
                )
                closed += 1
                logging.info("[orphan-sweep] closed non-connected session %s", name[:12])
                self._shutdown_audit(f"orphan-sweep closed {name[:12]}")
            except Exception:
                continue
        if closed:
            logging.info("[orphan-sweep] closed %d orphaned server session(s)", closed)

    def _find_pid_listening_on_port(self, port):
        """Return the PID of the node.exe process listening on *port* (Windows only).

        Used when this session reused an already-running WPPConnect Server
        left behind by a previous session that was force-quit, so we still
        have a way to terminate it instead of leaving it running forever.
        """
        import sys
        if sys.platform != "win32":
            return None
        no_window = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
        try:
            out = subprocess.check_output(
                ["netstat", "-ano", "-p", "TCP"],
                creationflags=no_window,
                text=True,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            return None

        port_suffix = f":{port}"
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 5 or parts[0] != "TCP" or parts[3] != "LISTENING":
                continue
            if not parts[1].endswith(port_suffix):
                continue
            try:
                candidate_pid = int(parts[-1])
            except ValueError:
                continue
            try:
                tasklist_out = subprocess.check_output(
                    ["tasklist", "/FI", f"PID eq {candidate_pid}", "/FO", "CSV", "/NH"],
                    creationflags=no_window,
                    text=True,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:
                continue
            if "node.exe" in tasklist_out.lower():
                return candidate_pid
        return None

    def ensure_wpp_running(self):
        """
        Start the local WPPConnect Server if it is not already listening.

        Normal mode   — shows a progress dialog while waiting (up to 5 min).
        Background mode — polls silently; exits with code 1 on timeout.

        On first launch the database initialisation and migrations can take
        60-90 s; subsequent starts are much faster.  On slower machines (HDD,
        antivirus scanning, or a first-run Puppeteer/Chrome download in
        start.js) startup can take well over 2 minutes, hence the 5-minute
        budget below.
        """
        if self._is_wpp_running():
            # ADOPT path: the shared Node is already listening (spawned by another
            # account, or left running from a previous session). We won't spawn it,
            # but we MUST still register our node-lease — otherwise the shutdown
            # guard in _stop_wpp_server() can't see that this account needs the
            # Node and will tree-kill it when the spawning account exits, wiping
            # our live WhatsApp session (root cause of the multi-account session
            # loss). Registering only on the spawn path was the bug.
            self._register_node_lease()
            return  # Already up (e.g. left running from a previous session)

        import sys
        import shutil

        if sys.platform == "win32":
            node_exe = resource_path("node", "node.exe")
        else:
            local_node = resource_path("node", "node")
            if os.path.isfile(local_node):
                node_exe = local_node
            else:
                node_exe = shutil.which("node") or "node"

        start_js  = resource_path("api",  "start.js")
        dist_server = resource_path("api",  "dist", "server.js")

        # All three files are required to start the bundled API.
        # If any is missing (setup incomplete or not yet run), skip silently —
        # ensure_api_modules_installed() already handled the missing node.exe
        # case; dist/server.js absence means setup was cancelled or not done yet.
        if not (os.path.isfile(node_exe)
                and os.path.isfile(start_js)
                and os.path.isfile(dist_server)):
            return

        self._wpp_log_path = None
        self._wpp_log_fh   = None

        # A WPPConnect left listening by a previous run (a crash, or an exit
        # that never got to force-kill it) is already serving this port.
        # _start_wpp_background() has no guard of its own, so without this the
        # launch below spawns a second node that cannot bind 6300 and dies —
        # and the dialog then "succeeds" against the *old* server, which may be
        # holding a long-dead Chrome. Reuse what is up, or nothing is.
        #
        # Checked before the background branch as well as the foreground one:
        # that branch used to spawn unconditionally, so a leftover Node meant a
        # second one launched only to die on EADDRINUSE while the poll below
        # reported success against the first. Same false success, no dialog to
        # show it.
        if self._is_wpp_running():
            logging.info("[ensure_wpp_running] WPPConnect already listening on %s — reusing it.",
                         self.wpp_port)
            self._check_wpp_version_pin()
            return

        if self.background_mode:
            # No dialog to show and no port for it to capture, so the
            # foreground path's _ensure_wpp_port_still_free() dance is left to
            # _start_wpp_background()'s own idempotent call. The wait is the
            # point: __init__ blocks here until Node answers, which is what
            # keeps the tray icon and the connect sequence from starting
            # against a dead port.
            self._start_wpp_background()
            deadline = time.time() + 300
            while time.time() < deadline:
                if self._is_wpp_running():
                    self._check_wpp_version_pin()
                    return
                time.sleep(1)
            logging.error("[ensure_wpp_running] WPPConnect never came up within "
                          "300s in background mode — exiting.")
            sys.exit(1)

        # Settle the port BEFORE the dialog captures it. _start_wpp_background()
        # calls this too, but it runs from the wx.CallAfter below — i.e. after
        # ApiStartupDialog.__init__ has already stored self.wpp_port and started
        # polling it every 500ms to decide the API came up.
        #
        # Without this, the fix defeats itself on its own success path: a
        # squatter takes 6301, the re-check correctly moves Node to 6302, and
        # the dialog goes on polling 6301 — which the squatter is answering, so
        # it reports success immediately while Node is still booting elsewhere.
        # If the squatter releases the port instead, the dialog polls a dead one
        # for the full 5 minutes and reports "API failed to start in time" while
        # Node is running fine. Either way a clean EADDRINUSE becomes a false
        # success or a false timeout, and the port announced to the screen
        # reader is the wrong one.
        #
        # Safe here specifically because the _is_wpp_running() check above just
        # returned False: that is a TCP connect and _is_port_free() is a bind,
        # exact logical complements — so our own still-listening Node has
        # already been adopted and cannot be moved out from under itself.
        # The call in _start_wpp_background() stays; it is idempotent.
        self._ensure_wpp_port_still_free()

        from ui.dialogs.api_startup import ApiStartupDialog
        def _show_startup_dlg():
            dlg = ApiStartupDialog(self, self.wpp_port)
            # Queued rather than called inline so the dialog is painted (and
            # announced by the screen reader) before the launch work begins.
            wx.CallAfter(self._start_wpp_background)
            res = dlg.ShowModal()
            dlg.Destroy()
            return res
        result = self.run_on_main_thread(_show_startup_dlg)

        if result != wx.ID_OK:
            details = ""
            log_path = getattr(self, "_wpp_log_path", None)
            if log_path and os.path.isfile(log_path):
                try:
                    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                        lines = f.readlines()
                    details = "".join(lines[-40:]).strip()
                except Exception:
                    pass
            msg = self.i18n.t("api_startup_warning")
            if details:
                msg = f"{msg}\n\n{details}"
            self.run_on_main_thread(wx.MessageBox, msg, self.app_name, wx.OK | wx.ICON_ERROR)
            sys.exit(1)

        self._check_wpp_version_pin()
