"""
api_setup.py — WinZapp's single WPPConnect Server setup/repair dialog.

One dialog, two possible operations — decided automatically at the start of
_run_setup() by checking whether api/dist/server.js already exists, never by
which code path constructed the dialog. This used to be two near-identical
dialogs (ApiSetupDialog here, plus a separate ModuleInstallDialog) with
different titles for what a user experienced as "the same install screen" —
consolidated into this one so there is only ever one thing shown, whatever
state api/ is actually in:

  api/dist/server.js absent → full setup (nothing cloned yet, or the whole
  api/ folder was deleted):
    1. Download the WPPConnect Server source ZIP from GitHub (no git required)
         • No tag  → branch main  archive
         • With tag → specific tag archive
    2. Extract into client/api/, keeping the upstream ZIP away from our own
       root files (start.js, .env, config.json), then restoring every WinZapp
       patch from api_patches/ and merging our pinned dependencies into
       package.json — the same patch set setup_api.py applies, so an end-user
       install and a developer one end up with identical API code
    3. npm install --no-audit --no-fund      (install all dependencies)
    4. npm exec puppeteer browsers install <chrome on Windows, chrome-headless-shell
       elsewhere — must match start.js's findPreferredChrome()> (best-effort)
    5. npm run db:generate                   (only if the script is defined)
    6. npm run build                         (compile TypeScript → dist/server.js)

  api/dist/server.js present → modules-only repair (already cloned/built,
  just node_modules is missing — the normal state of every fresh WinZapp.zip
  extract, since node_modules isn't bundled to keep the download small):
    Steps 3-5 above only — no download/extract/build, since there is nothing
    to fetch and what's already built doesn't need rebuilding.

Note: db:deploy / db:deploy:win are NOT run here.  Prisma migrations require
a live PostgreSQL connection, which is only available at runtime.  The
migrations are applied automatically by start.js every time the API starts.

The WPPCONNECT_TAG_VERSION variable in the root .env optionally pins a specific
tag.  When unset the latest main branch is downloaded.

Uses only the standard library (zipfile, io, tempfile) plus the already-present
requests package for the HTTP download — no git installation required.

Modal result:
  wx.ID_OK     — setup succeeded; caller may proceed
  wx.ID_CANCEL — user cancelled or an error occurred; caller should exit
"""

import io
import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import zipfile

import requests
import wx

from app_paths import resource_path

# GitHub download URLs — no git required
_REPO_ZIP_MAIN = (
    "https://github.com/wppconnect-team/wppconnect-server"
    "/archive/refs/heads/main.zip"
)
_REPO_ZIP_TAG  = (
    "https://github.com/wppconnect-team/wppconnect-server"
    "/archive/refs/tags/{tag}.zip"
)
WPP_GITHUB_API_LATEST_RELEASE = (
    "https://api.github.com/repos/wppconnect-team/wppconnect-server/releases/latest"
)


def fetch_latest_wpp_tag(timeout: float = 15) -> str:
    """Return the tag name of the latest wppconnect-server GitHub release, or "".

    Used so a fresh install (and the update flows) always land on whatever is
    actually the newest tagged release instead of a version number baked into
    WinZapp's own .env at build time — a WPPCONNECT_TAG_VERSION pin only ever
    gets updated when WinZapp itself ships a new build, so a version fixed
    that way is stale from the day it's set. Returns "" on any failure so
    callers can fall back to the previous fixed-tag/main-branch behaviour
    instead of failing setup outright over a GitHub API hiccup.
    """
    try:
        resp = requests.get(
            WPP_GITHUB_API_LATEST_RELEASE,
            headers={"User-Agent": "WinZapp"},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        tag = (data.get("tag_name") or "").strip()
        return tag
    except Exception as exc:
        logging.warning("[api_setup] Failed to fetch latest wppconnect-server release tag: %s", exc)
        return ""

# Root-level files whose pre-included content always takes precedence.
_PRESERVE = {"start.js", ".env", "config.json"}

# Root-level files WinZapp owns outright: they carry no per-install state (the
# API key and port both come from environment variables _start_wpp_background()
# injects, never from config.json's own values, and nothing writes either file
# at runtime), so they are restored from api_patches/ rather than merely
# preserved. _PRESERVE already stops the upstream ZIP from overwriting them;
# without this restore step they were simply frozen at whatever an older
# WinZapp install happened to leave on disk, so a user updating from an old
# version would silently keep its config.json forever — including any
# createOptions a newer release changed. `.env` is deliberately absent: it is
# the one root file that could be genuinely local, and nothing reads it anyway.
_CUSTOM_ROOT_FILES = [
    "start.js",
    "config.json",
    ".eslintrc.json",
    ".prettierrc",
    ".prettierignore",
    "jest.config.js",
]

# Only these keys are copied from api_patches/package.json onto whatever the
# downloaded ZIP produced — same list, and same reasoning, as setup_api.py's
# _PATCHED_DEPENDENCY_KEYS. Merging the whole "dependencies" block would also
# roll every OTHER dependency back to whatever was frozen in api_patches/ at
# some earlier point, and overwriting the file wholesale would freeze
# WPPConnect's own "version" field — which is what WppUpdateChecker compares
# against the latest GitHub release — at a value that has nothing to do with
# the tag actually downloaded here.
#
# @wppconnect-team/wppconnect is deliberately NOT in this list. It used to be,
# pinned to an exact version that went stale within days — this dependency
# releases multiple times a week, and wppconnect-server's own package.json can
# (and did) move on to requiring a newer one than whatever WinZapp had frozen,
# silently running an incompatible pairing with no error anywhere. Leaving it
# out means upstream's own declared range wins, same as every other unpinned
# dependency — and @wppconnect/wa-js / @wppconnect/wa-version, which WinZapp
# never pins directly either, come along transitively at whichever paired
# version @wppconnect-team/wppconnect itself resolves to.
_PATCHED_DEPENDENCY_KEYS = [
    "prom-client",  # imported by src/middleware/instrumentation.ts, which is
                    # WinZapp's own patch. Upstream happens to declare it too,
                    # but under devDependencies — so our production import is
                    # satisfied today only because both installers run a plain
                    # `npm install`. Listing it here means the merge writes it
                    # into dependencies regardless of what upstream does with
                    # its own copy. npm accepts the entry appearing in both
                    # blocks (verified with `npm install --dry-run`).
    "zod",  # runtime schema for the sync endpoints' response contracts
            # (src/dto/sync.ts). Declared here because the controllers import
            # it at runtime — it is not a build-only tool.
    "@ffmpeg-installer/ffmpeg",  # vendors a real ffmpeg binary — WinZapp's own
                                  # Python side shells out to it directly to
                                  # encode voice messages to OGG/Opus.
]

# Runtime state dirs/files that should survive a re-download.
#
# "tokens" is the one that actually holds the paired WhatsApp session:
# wppconnect-server's file token store (config.ts `tokenStoreType: 'file'`)
# writes to `./tokens` relative to process.cwd(), which is client/api/ — see
# src/util/tokenStore/FileTokenStore/FileTokenStore.ts (`path: './tokens'`).
# It was missing from this set, so the clean step below deleted every stored
# session token on each full reinstall/update while userDataDir (the Chrome
# profile) survived. What that costs is spelled out by WinZapp's own patch
# in api_patches/src/util/createSessionUtil.ts, which exists to stop the
# same token being destroyed by a different route: "The saved WhatsApp Web
# credentials are then gone for good and the next start looks like a logout,
# even though the phone still lists the linked device." Nothing recreates
# the file, so whatever state that leaves the session in survives every
# restart — which is what "ao reinstalar a WPPConnect o programa fica em
# modo offline permanente, mesmo ao reiniciar" describes.
#
# "wppconnect_tokens" is kept alongside it purely for older installs. It is
# the folder name upstream used before "tokens" (both are still listed in
# api/.gitignore) and nothing writes to it any more — it is empty on a
# current install — but dropping it here would delete whatever an old
# install still has in it.
_KEEP_RUNTIME = {"tokens", "wppconnect_tokens", "userDataDir", "wppconnect.log"}

# WinZapp's patches on top of upstream wppconnect-server — same list as
# setup_api.py's custom_files and build.py's API_CUSTOM_SRC_FILES. Unlike
# _PRESERVE (root-level files skipped outright during extraction), these live
# under src/ and get deleted by the "clean previous partial setup" step and
# then are not part of _PRESERVE's skip-list during extraction, so a vanilla
# ZIP re-download used to silently replace them with unpatched upstream code
# — every call to this dialog (first-run setup AND the update/force-reinstall
# flow) stripped WinZapp's own controller/util/middleware fixes with no
# restoration step. _run_setup() now stashes their content before the clean
# step and restores it after extraction, mirroring setup_api.py's approach.
_CUSTOM_SRC_FILES = [
    "src/config.ts",
    "src/index.ts",
    "src/util/createSessionUtil.ts",
    "src/util/sessionUtil.ts",
    "src/util/functions.ts",
    "src/util/tokenStore/fileTokenStory.ts",
    "src/middleware/statusConnection.ts",
    "src/middleware/auth.ts",
    "src/dto/sync.ts",
    "src/middleware/instrumentation.ts",
    "src/errors/domain.ts",
    "src/middleware/errorHandler.ts",
    "src/services/messageResolver.ts",
    "src/types/express/index.d.ts",
    "src/tests/middleware/instrumentation.test.ts",
    "src/tests/dto/sync.test.ts",
    "src/tests/middleware/errorHandler.test.ts",
    "src/controller/deviceController.ts",
    "src/controller/messageController.ts",
    "src/controller/sessionController.ts",
    "src/controller/statusController.ts",
    "src/routes/index.ts",
    "dist/middleware/auth.js",
    "decrypt.js",
]


class ApiSetupDialog(wx.Dialog):
    """Progress dialog for the WPPConnect Server download + build setup.

    Parameters
    ----------
    parent        : wx.Window
    title_override: str | None
        When provided, overrides the default hardcoded dialog title.
        Used by the update flow to show a different title.
    forced_tag    : str | None
        When provided, this tag is used for the GitHub download instead
        of reading WPPCONNECT_TAG_VERSION from the .env file.  Used by
        the update flow to pin the exact minimum-version tag.
    """

    _PULSE_MS = 80

    # Percentage ranges assigned to each named stage, so the progress bar
    # moves through the install with a rough sense of "how far along", not
    # just an indeterminate bounce — users reported a multi-minute npm
    # install (routinely the longest single step) looking identical to a
    # frozen/broken install with nothing to tell them otherwise. Real
    # byte-level progress is only available for the ZIP download (it has a
    # content-length header); every other step "trickles" toward its
    # range's end while it runs (see _on_pulse) since neither npm install's
    # nor npm run build's output is a reliable, version-stable source of
    # real completion percentage to parse.
    _STAGES_FULL = {
        "resolve_tag": (0, 2),
        "download":    (2, 25),
        "clean":       (25, 28),
        "extract":     (28, 35),
        "npm_install": (35, 70),
        "chrome":      (70, 82),
        "db_generate": (82, 90),
        "build":       (90, 99),
    }
    _STAGES_MODULES_ONLY = {
        "npm_install": (0, 55),
        "chrome":      (55, 80),
        "db_generate": (80, 99),
    }

    def __init__(self, parent, title_override=None, forced_tag=None):
        self._i18n = parent.i18n
        title = title_override or self._i18n.t("api_setup_dialog_title")
        style = wx.DEFAULT_DIALOG_STYLE & ~wx.CLOSE_BOX
        super().__init__(parent, title=title, style=style)

        self._proc        = None   # active npm subprocess (for kill on cancel)
        self._cancelled   = False
        self._finished    = False  # exactly one of success/error/cancel may win
        self._forced_tag  = forced_tag   # overrides .env WPPCONNECT_TAG_VERSION

        # Progress-bar state — see _STAGES_FULL/_STAGES_MODULES_ONLY and
        # _set_stage()/_on_pulse().
        self._stage_end_pct = 0
        self._trickling      = False

        self._build_ui()

        self._timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_pulse, self._timer)
        self.Bind(wx.EVT_CLOSE, self._on_cancel)

        t = threading.Thread(target=self._run_setup, daemon=True)
        t.start()

        self._timer.Start(self._PULSE_MS)

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        self._status_lbl = wx.StaticText(self, label=self._i18n.t("api_setup_status_label"))

        self._gauge = wx.Gauge(self, range=100,
                               style=wx.GA_HORIZONTAL | wx.GA_SMOOTH)

        cancel_btn = wx.Button(self, wx.ID_CANCEL, label=self._i18n.t("api_setup_cancel"))
        cancel_btn.Bind(wx.EVT_BUTTON, self._on_cancel)

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(self._status_lbl, 0, wx.ALL | wx.EXPAND, 12)
        sizer.Add(self._gauge,      0, wx.ALL | wx.EXPAND, 12)
        sizer.Add(cancel_btn,       0, wx.ALIGN_CENTER | wx.BOTTOM, 12)

        self.SetSizer(sizer)
        sizer.Fit(self)
        self.SetMinSize((520, -1))
        self.Centre()

    def _set_status(self, text: str):
        wx.CallAfter(self._status_lbl.SetLabel, text)
        wx.CallAfter(self.Layout)

    # ── Timer / gauge ─────────────────────────────────────────────────────────

    def _set_stage(self, text: str, start_pct: int, end_pct: int, trickle: bool = True):
        """Enter a new named stage: update the status text, jump the gauge to
        start_pct, and — unless the caller has real progress data to report
        itself (see _download_zip) — let _on_pulse() creep it the rest of the
        way toward end_pct while the stage's subprocess/step runs."""
        self._set_status(text)
        self._stage_end_pct = end_pct
        self._trickling = trickle
        wx.CallAfter(self._gauge.SetValue, start_pct)

    def _on_pulse(self, _event):
        if not self._trickling:
            return
        try:
            current = self._gauge.GetValue()
        except RuntimeError:
            return
        # Never let the trickle reach the stage's own ceiling — that's
        # reserved for the moment the stage actually completes and the next
        # _set_stage()/_finish_success() call moves the gauge itself.
        ceiling = max(current, self._stage_end_pct - 1)
        if current >= ceiling:
            return
        remaining = ceiling - current
        step = max(1, remaining // 40)
        self._gauge.SetValue(min(ceiling, current + step))

    # ── .env reader ───────────────────────────────────────────────────────────

    def _read_env_value(self, key: str, default: str = "") -> str:
        """Read a value from the nearest .env file; fall back to default."""
        for env_path in [
            resource_path(".env"),
            os.path.join(os.path.dirname(resource_path()), ".env"),
        ]:
            if not os.path.isfile(env_path):
                continue
            try:
                with open(env_path, encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        k, _, v = line.partition("=")
                        if k.strip() == key:
                            return v.strip()
            except Exception:
                pass
        return default

    # ── Download helper ───────────────────────────────────────────────────────

    def _download_zip(self, url: str, dest_path: str, start_pct: int = 2, end_pct: int = 25) -> bool:
        """
        Stream-download the ZIP at *url* to *dest_path*.
        Updates the status label with download progress.
        The response carries a real content-length, so this reports genuine
        byte-level progress mapped into [start_pct, end_pct] instead of
        trickling — the timer-driven trickle is switched off for the
        duration of the call so it can't fight with real numbers.
        Returns True on success, False on failure/cancel.
        """
        self._trickling = False
        wx.CallAfter(self._gauge.SetValue, start_pct)
        try:
            response = requests.get(url, stream=True, timeout=(30, 300))
            response.raise_for_status()
        except requests.RequestException as exc:
            if not self._cancelled:
                wx.CallAfter(
                    self._finish_error,
                    self._i18n.t("api_setup_error_download_start").format(details=exc),
                )
            return False

        total     = int(response.headers.get("content-length", 0))
        downloaded = 0
        chunk_size = 512 * 1024   # 512 KB

        try:
            with open(dest_path, "wb") as fh:
                for chunk in response.iter_content(chunk_size=chunk_size):
                    if self._cancelled:
                        return False
                    if not chunk:
                        continue
                    fh.write(chunk)
                    downloaded += len(chunk)
                    mb_down = downloaded / (1024 * 1024)
                    if total:
                        mb_total = total / (1024 * 1024)
                        pct = start_pct + (end_pct - start_pct) * (downloaded / total)
                        wx.CallAfter(self._gauge.SetValue, int(min(end_pct, pct)))
                        self._set_status(
                            self._i18n.t("api_setup_downloading").format(
                                downloaded=f"{mb_down:.1f}", total=f"{mb_total:.1f}"
                            )
                        )
                    else:
                        self._set_status(
                            self._i18n.t("api_setup_downloading_no_total").format(downloaded=f"{mb_down:.1f}")
                        )
        except Exception as exc:
            if not self._cancelled:
                wx.CallAfter(
                    self._finish_error,
                    self._i18n.t("api_setup_error_download").format(details=exc),
                )
            return False

        return not self._cancelled

    # ── Extract helper ────────────────────────────────────────────────────────

    @staticmethod
    def _merge_package_json_dependencies(api_dir: str, patches_dir: str) -> None:
        """Apply WinZapp's dependency patches onto the downloaded package.json.

        Port of setup_api.py's function of the same name — until this existed,
        an install done through this dialog (i.e. every end-user install) kept
        the vanilla upstream package.json, so `npm install` below never
        installed @ffmpeg-installer/ffmpeg at all (upstream wppconnect-server
        does not declare it). Only setup_api.py — a developer tool nobody but
        us runs — applied it.

        Deliberately a merge and not an overwrite; see _PATCHED_DEPENDENCY_KEYS.
        Never fatal: a missing or unreadable package.json is left exactly as it
        was, because npm install can still succeed on the upstream file, while
        writing a half-merged one could not.
        """
        pkg_path = os.path.join(api_dir, "package.json")
        patch_path = os.path.join(patches_dir, "package.json")
        if not (os.path.isfile(pkg_path) and os.path.isfile(patch_path)):
            logging.warning(
                "[api_setup] Skipping package.json dependency merge — "
                "pkg=%s patch=%s",
                os.path.isfile(pkg_path), os.path.isfile(patch_path),
            )
            return
        try:
            with open(pkg_path, encoding="utf-8") as fh:
                pkg = json.load(fh)
            with open(patch_path, encoding="utf-8") as fh:
                patch = json.load(fh)
        except Exception as exc:
            logging.warning("[api_setup] Failed to read package.json for merge: %s", exc)
            return

        patch_deps = patch.get("dependencies", {})
        deps = pkg.setdefault("dependencies", {})
        applied = []
        for key in _PATCHED_DEPENDENCY_KEYS:
            if key in patch_deps:
                deps[key] = patch_deps[key]
                applied.append(f"{key}@{patch_deps[key]}")
        if not applied:
            return
        try:
            with open(pkg_path, "w", encoding="utf-8") as fh:
                json.dump(pkg, fh, indent=2)
                fh.write("\n")
        except Exception as exc:
            logging.warning("[api_setup] Failed to write merged package.json: %s", exc)
            return
        logging.info(
            "[api_setup] Applied patched dependencies into package.json: %s "
            "(WPPConnect version kept at %s)",
            ", ".join(applied), pkg.get("version", "?"),
        )

    @staticmethod
    def _apply_node_modules_patches(api_dir: str) -> None:
        """Apply WinZapp's patches to files INSIDE node_modules (a vendored
        dependency of WPPConnect Server, not WPPConnect Server itself), which
        `npm install` just rebuilt from scratch a few lines above this call.

        Port of setup_api.py's post-`npm install` patch block (decrypt.js
        copy + _patch_wppconnect_host_layer() + _patch_wppconnect_status_layer())
        — until this existed, ONLY a developer running setup_api.py directly
        ever got these patches; every real end-user install goes through
        this dialog instead, which restored the patched decrypt.js/*.ts
        files into client/api/ itself (see the ZIP-extract step above) but
        never copied/patched anything inside node_modules, so the
        decrypt.js RangeError/memory-leak fix, the host.layer.js
        pairing-code-rotation fix (#8), and the status.layer.js
        posting-result fix all silently never applied for any shipped
        install.

        Best-effort and never fatal: node_modules is deleted/rebuilt by npm
        on every install, but a failure to patch it should not block the
        rest of setup — the app still works, just without these fixes,
        exactly like the pre-existing decrypt.js situation this replaces.
        """
        node_modules_wppconnect = os.path.join(
            api_dir, "node_modules", "@wppconnect-team", "wppconnect", "dist", "api",
        )

        # decrypt.js — RangeError/memory-leak patch.
        try:
            custom_decrypt = os.path.join(api_dir, "decrypt.js")
            decrypt_dest = os.path.join(node_modules_wppconnect, "helpers", "decrypt.js")
            if os.path.isfile(custom_decrypt):
                os.makedirs(os.path.dirname(decrypt_dest), exist_ok=True)
                shutil.copy2(custom_decrypt, decrypt_dest)
                logging.info("[api_setup] Copied decrypt.js patch into node_modules.")
            else:
                logging.warning("[api_setup] decrypt.js not found in api_dir — skipping node_modules patch.")
        except Exception as exc:
            logging.warning("[api_setup] Failed to copy decrypt.js patch into node_modules: %s", exc)

        # host.layer.js — pairing-code rotation fix (issue #8).
        try:
            ApiSetupDialog._patch_wppconnect_host_layer(node_modules_wppconnect)
        except Exception as exc:
            logging.warning("[api_setup] Failed to patch host.layer.js: %s", exc)

        # status.layer.js — status-posting always reported success fix.
        try:
            ApiSetupDialog._patch_wppconnect_status_layer(node_modules_wppconnect)
        except Exception as exc:
            logging.warning("[api_setup] Failed to patch status.layer.js: %s", exc)

        # sender.layer.js — failed video sends only logged opaque minified junk.
        try:
            ApiSetupDialog._patch_wppconnect_sender_layer(node_modules_wppconnect)
        except Exception as exc:
            logging.warning("[api_setup] Failed to patch sender.layer.js: %s", exc)

        # welcome.js — latest-version ESM require crashes startup on Node 20+.
        try:
            ApiSetupDialog._patch_wppconnect_welcome_layer(node_modules_wppconnect)
        except Exception as exc:
            logging.warning("[api_setup] Failed to patch welcome.js: %s", exc)

    @staticmethod
    def _patch_wppconnect_status_layer(wppconnect_api_dir: str) -> bool:
        """Patch @wppconnect-team/wppconnect's compiled status.layer.js so
        posting a status (text/image/video) actually reports whether it
        succeeded. Port of setup_api.py's function of the same name — see
        client/core/wppconnect_status_layer_patch.py's module docstring for
        the root cause.

        Idempotent (each of the three patches is independently a no-op once
        applied) and best-effort.
        """
        from core.wppconnect_status_layer_patch import ALL_PATCHES
        status_layer_path = os.path.join(wppconnect_api_dir, "layers", "status.layer.js")
        if not os.path.isfile(status_layer_path):
            logging.warning("[api_setup] status.layer.js not found — skipping status-posting-result patch.")
            return False

        with open(status_layer_path, encoding="utf-8") as f:
            content = f.read()

        applied = 0
        already = 0
        missing = 0
        for original, patched in ALL_PATCHES:
            if patched in content:
                already += 1
            elif original in content:
                content = content.replace(original, patched, 1)
                applied += 1
            else:
                missing += 1

        if applied:
            with open(status_layer_path, "w", encoding="utf-8") as f:
                f.write(content)
            logging.info("[api_setup] Patched status.layer.js — %d status-posting method(s) now correctly report success/failure.", applied)
        elif already == len(ALL_PATCHES):
            logging.info("[api_setup] status.layer.js status-posting-result patch already applied.")
        if missing:
            logging.warning(
                "[api_setup] status.layer.js: %d status-posting method(s) did not match the "
                "expected upstream source — skipping those.", missing,
            )
        return missing == 0

    @staticmethod
    def _patch_wppconnect_sender_layer(wppconnect_api_dir: str) -> bool:
        """Patch @wppconnect-team/wppconnect's compiled sender.layer.js so a
        failed sendFile() (every video-message send, currently) reports the
        real browser-side error instead of the opaque, minified
        {"name":"t","message":"t"} that crossing the CDP exception boundary
        raw currently produces. Port of setup_api.py's function of the same
        name — see client/core/wppconnect_sender_layer_patch.py's module
        docstring for the root cause.

        Idempotent and best-effort, same pattern as
        _patch_wppconnect_status_layer just above.
        """
        from core.wppconnect_sender_layer_patch import ALL_PATCHES, patch_sender_layer_source
        sender_layer_path = os.path.join(wppconnect_api_dir, "layers", "sender.layer.js")
        if not os.path.isfile(sender_layer_path):
            logging.warning("[api_setup] sender.layer.js not found — skipping sendFile error-detail patch.")
            return False

        with open(sender_layer_path, encoding="utf-8") as f:
            content = f.read()

        applied = 0
        already = 0
        missing = 0
        for original, patched in ALL_PATCHES:
            if patched in content:
                already += 1
            elif original in content:
                content = content.replace(original, patched, 1)
                applied += 1
            else:
                missing += 1

        # Also migrates a couple of transitional intermediate states from
        # this patch's own iterative development not covered by the literal
        # ALL_PATCHES pairs above — see patch_sender_layer_source()'s own
        # docstring (setup_api.py's copy of this function does the same).
        migrated = patch_sender_layer_source(content)
        if migrated != content:
            content = migrated
            applied += 1

        if applied:
            with open(sender_layer_path, "w", encoding="utf-8") as f:
                f.write(content)
            logging.info("[api_setup] Patched sender.layer.js — %d sendFile error(s) now report real detail.", applied)
        elif already == len(ALL_PATCHES):
            logging.info("[api_setup] sender.layer.js sendFile error-detail patch already applied.")
        if missing:
            logging.warning(
                "[api_setup] sender.layer.js: %d method(s) did not match the "
                "expected upstream source — skipping those.", missing,
            )
        return missing == 0

    @staticmethod
    def _patch_wppconnect_welcome_layer(wppconnect_api_dir: str) -> bool:
        """Patch @wppconnect-team/wppconnect's compiled controllers/
        welcome.js so a plain CommonJS require() of the ESM-only
        `latest-version` package doesn't crash the whole server on startup
        (Node 20+). Port of setup_api.py's function of the same name — see
        client/core/wppconnect_welcome_layer_patch.py's module docstring.

        *wppconnect_api_dir* is .../node_modules/@wppconnect-team/wppconnect/dist/api
        (same as every other patch method here); welcome.js lives one level
        up, under dist/controllers/ instead of dist/api/layers/.

        Idempotent and best-effort, same pattern as the other layer patches
        above.
        """
        from core.wppconnect_welcome_layer_patch import ALL_PATCHES
        welcome_layer_path = os.path.join(wppconnect_api_dir, "..", "controllers", "welcome.js")
        if not os.path.isfile(welcome_layer_path):
            logging.warning("[api_setup] welcome.js not found — skipping latest-version ESM patch.")
            return False

        with open(welcome_layer_path, encoding="utf-8") as f:
            content = f.read()

        applied = 0
        already = 0
        missing = 0
        for original, patched in ALL_PATCHES:
            if patched in content:
                already += 1
            elif original in content:
                content = content.replace(original, patched, 1)
                applied += 1
            else:
                missing += 1

        if applied:
            with open(welcome_layer_path, "w", encoding="utf-8") as f:
                f.write(content)
            logging.info("[api_setup] Patched welcome.js — latest-version ESM require no longer crashes startup on Node 20+.")
        elif already == len(ALL_PATCHES):
            logging.info("[api_setup] welcome.js latest-version ESM patch already applied.")
        if missing:
            logging.warning(
                "[api_setup] welcome.js: %d pattern(s) did not match the "
                "expected upstream source — skipping those.", missing,
            )
        return missing == 0

    @staticmethod
    def _patch_wppconnect_host_layer(wppconnect_api_dir: str) -> bool:
        """Patch @wppconnect-team/wppconnect's compiled host.layer.js: the

        *wppconnect_api_dir* is .../node_modules/@wppconnect-team/wppconnect/dist/api
        (i.e. the caller has already descended into node_modules — unlike
        setup_api.py's copy of this function, which is given the outer
        client/api/ directory instead).
        """
        from core.wppconnect_host_layer_patch import patch_host_layer_source
        host_layer_path = os.path.join(wppconnect_api_dir, "layers", "host.layer.js")
        if not os.path.isfile(host_layer_path):
            logging.warning("[api_setup] host.layer.js not found — skipping pairing-code patch.")
            return False

        with open(host_layer_path, encoding="utf-8") as f:
            content = f.read()

        patched, notes, ok = patch_host_layer_source(content)
        if patched != content:
            with open(host_layer_path, "w", encoding="utf-8") as f:
                f.write(patched)

        for note in notes:
            if "DID NOT MATCH" in note:
                logging.warning(
                    "[api_setup] host.layer.js — %s The installed "
                    "@wppconnect-team/wppconnect version may have changed "
                    "this file.", note,
                )
            else:
                logging.info("[api_setup] host.layer.js — %s", note)
        return ok

    def _extract_zip(self, zip_path: str, api_dir: str) -> bool:
        """
        Extract the GitHub source ZIP into *api_dir*.

        GitHub archives wrap all content in a single top-level directory
        (e.g. "wppconnect-server-main/" or "wppconnect-server-2.10.0/").
        That prefix is stripped so files land directly in api_dir.

        Root-level entries matching _PRESERVE are skipped so our pre-included
        start.js and .env are never overwritten.
        """
        self._set_status(self._i18n.t("api_setup_extracting"))
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                members = zf.infolist()

                # Detect the top-level directory from the first entry
                first_name = members[0].filename if members else ""
                top_dir    = first_name.split("/")[0] + "/"  # e.g. "wppconnect-server-main/"

                for member in members:
                    if self._cancelled:
                        return False

                    # Strip the top-level directory prefix
                    rel = member.filename
                    if rel.startswith(top_dir):
                        rel = rel[len(top_dir):]
                    if not rel:
                        # This was the top-level dir entry itself
                        continue

                    # Normalise to OS separators
                    rel_os = rel.replace("/", os.sep)

                    # Skip root-level preserved files
                    root_component = rel.split("/")[0]
                    if root_component in _PRESERVE and "/" not in rel.rstrip("/"):
                        continue

                    dest = os.path.join(api_dir, rel_os)
                    # Guard against a malicious/corrupted zip entry (e.g.
                    # "../../evil.dll") writing outside api_dir — GitHub's
                    # own archive generator shouldn't produce these, but this
                    # is still external network content, not something
                    # WinZapp generated itself.
                    dest_abs = os.path.abspath(dest)
                    api_dir_abs = os.path.abspath(api_dir)
                    if dest_abs != api_dir_abs and not dest_abs.startswith(api_dir_abs + os.sep):
                        raise ValueError(f"Unsafe zip member path: {member.filename!r}")

                    if member.is_dir() or rel.endswith("/"):
                        os.makedirs(dest, exist_ok=True)
                    else:
                        os.makedirs(os.path.dirname(dest), exist_ok=True)
                        with zf.open(member) as src_fh, open(dest, "wb") as dst_fh:
                            shutil.copyfileobj(src_fh, dst_fh)

        except Exception as exc:
            if not self._cancelled:
                wx.CallAfter(
                    self._finish_error,
                    self._i18n.t("api_setup_error_extract").format(details=exc),
                )
            return False

        return not self._cancelled

    # ── npm subprocess helper ─────────────────────────────────────────────────

    def _run_subprocess(self, cmd, cwd=None, env=None):
        """
        Run a subprocess and wait for it to finish.
        Returns (success: bool, stderr: str).
        """
        import sys
        creation_flags = 0
        if sys.platform == "win32" and hasattr(subprocess, "CREATE_NO_WINDOW"):
            creation_flags = subprocess.CREATE_NO_WINDOW
        try:
            self._proc = subprocess.Popen(
                cmd, cwd=cwd, env=env,
                creationflags=creation_flags,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            _, stderr_bytes = self._proc.communicate()
        except FileNotFoundError as exc:
            return False, str(exc)

        if self._cancelled:
            return False, ""
        rc     = self._proc.returncode
        stderr = (stderr_bytes or b"").decode("utf-8", errors="replace").strip()
        return (rc == 0), stderr

    # ── Background setup thread ───────────────────────────────────────────────

    def _run_setup(self):
        import sys
        import shutil

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

        api_dir  = resource_path("api")
        # Must be the directory start.js searches and exports as
        # PUPPETEER_CACHE_DIR (client/api/.cache), not the .cache/puppeteer
        # subfolder — otherwise the browser this installer downloads and the
        # browser the server looks for are two different trees.
        puppeteer_cache = resource_path("api", ".cache")
        npm_env  = {
            **os.environ,
            "PATH": path_env,
            "PUPPETEER_CACHE_DIR": puppeteer_cache
        }

        # The one thing that decides which operation this run actually
        # performs — see the module docstring. dist/server.js already
        # existing means api/ was already cloned and built; all that's
        # missing is node_modules (never bundled in WinZapp.zip), so there's
        # nothing to download/extract/rebuild, just dependencies to install.
        #
        # That heuristic breaks for the update/force-reinstall flow though —
        # _update_wpp_server() and WppUpdateChecker always pass forced_tag
        # for a specific *newer* version, but dist/server.js from the
        # currently-installed *older* version is still sitting right there,
        # so modules_only used to come back True and the download/extract/
        # build steps below (guarded by `if not modules_only`) were skipped
        # entirely — npm install just reinstalled dependencies for the same
        # old source tree. The user would confirm the update, WinZapp would
        # restart the API, and the "new version available" prompt came right
        # back because nothing had actually changed. A forced_tag always
        # means "fetch and build this specific version", never "just repair
        # node_modules for whatever happens to be on disk".
        dist_server   = os.path.join(api_dir, "dist", "server.js")
        modules_only  = os.path.isfile(dist_server) and self._forced_tag is None
        stages        = self._STAGES_MODULES_ONLY if modules_only else self._STAGES_FULL

        try:
            if not modules_only:
                self._set_stage(self._i18n.t("api_setup_resolving_tag"), *stages["resolve_tag"])
                tag = self._forced_tag if self._forced_tag is not None else self._read_env_value("WPPCONNECT_TAG_VERSION")
                if not tag:
                    # No explicit tag was requested and .env doesn't pin one — resolve
                    # the actual latest GitHub release instead of defaulting straight
                    # to the main branch head. A fixed WPPCONNECT_TAG_VERSION only
                    # ever changes when WinZapp itself ships a new build, so it is
                    # stale from the moment it's set; a brand-new install deserves
                    # whatever is genuinely newest today, not a version already
                    # behind by the time the user installs it. Falls through to the
                    # main branch (the previous behaviour) if the API is unreachable.
                    tag = fetch_latest_wpp_tag()
                    if tag:
                        logging.info("[api_setup] Using latest released wppconnect-server tag: %s", tag)
                    else:
                        logging.warning("[api_setup] Could not resolve latest release tag — falling back to main branch")

                # ── Step 1: download source ZIP ───────────────────────────────
                if tag:
                    url = _REPO_ZIP_TAG.format(tag=tag)
                else:
                    url = _REPO_ZIP_MAIN

                tmp_zip = tempfile.mktemp(suffix=".zip", prefix="winzapp_api_")
                try:
                    ok = self._download_zip(url, tmp_zip, *stages["download"])
                    if not ok:
                        return  # error already reported or cancelled

                    if self._cancelled:
                        return

                    # Stash WinZapp's own patches before they get wiped below —
                    # see _CUSTOM_SRC_FILES for why this is needed. This is only
                    # a FALLBACK source (used below if api_patches/ — the
                    # pristine reference copy build.py ships alongside api/,
                    # never touched by this dialog — is missing, e.g. an old
                    # portable build made before api_patches/ existed). Stashing
                    # from api_dir itself is not enough on its own: if a *prior*
                    # reinstall already left api_dir with an outdated/broken
                    # patch (as happened before this stash/restore existed at
                    # all), re-stashing that same broken copy on every future
                    # reinstall would preserve the breakage forever, with no way
                    # for a newer WinZapp release's improved patches to ever
                    # reach an existing install.
                    custom_contents = {}
                    for rel_path in _CUSTOM_SRC_FILES:
                        full_path = os.path.join(api_dir, rel_path.replace("/", os.sep))
                        if os.path.isfile(full_path):
                            try:
                                with open(full_path, "rb") as fh:
                                    custom_contents[rel_path] = fh.read()
                            except Exception as exc:
                                logging.warning(
                                    "[api_setup] Failed to stash custom file %s: %s",
                                    rel_path, exc,
                                )

                    # ── Step 2: clean previous partial setup ──────────────────
                    self._set_stage(self._i18n.t("api_setup_preparing_folder"), *stages["clean"])
                    # api_dir won't exist at all if the user (or a broken
                    # install) deleted the whole api/ folder — os.listdir() on a
                    # missing directory raises FileNotFoundError ([WinError 3] on
                    # Windows), which crashed this dialog with no useful recovery
                    # even though this is exactly the "nothing installed yet"
                    # case this dialog exists to handle. Reported live: deleting
                    # api/ entirely and reopening WinZapp failed setup with
                    # "[WinError 3] O sistema não pode encontrar o caminho
                    # especificado: '...\\api'".
                    os.makedirs(api_dir, exist_ok=True)
                    for item in os.listdir(api_dir):
                        if item in _PRESERVE or item in _KEEP_RUNTIME:
                            continue
                        target = os.path.join(api_dir, item)
                        try:
                            if os.path.isdir(target):
                                shutil.rmtree(target, ignore_errors=True)
                            else:
                                os.remove(target)
                        except Exception:
                            pass

                    if self._cancelled:
                        return

                    # ── Step 3: extract ZIP into client/api/ ──────────────────
                    self._set_stage(self._i18n.t("api_setup_extracting"), *stages["extract"])
                    ok = self._extract_zip(tmp_zip, api_dir)
                    if not ok:
                        return

                    # Restore WinZapp's patches over the freshly-extracted
                    # vanilla upstream files. Prefer the pristine reference copy
                    # bundled at api_patches/ (always matches whatever WinZapp
                    # version is currently running) over the pre-wipe stash from
                    # api_dir (which may itself be an outdated/broken copy left
                    # by an older install) — see the comment above custom_contents.
                    patches_dir = resource_path("api_patches")
                    for rel_path in _CUSTOM_SRC_FILES + _CUSTOM_ROOT_FILES:
                        pristine_path = os.path.join(patches_dir, rel_path.replace("/", os.sep))
                        if os.path.isfile(pristine_path):
                            try:
                                with open(pristine_path, "rb") as fh:
                                    content = fh.read()
                                source = "api_patches/ (current WinZapp build)"
                            except Exception as exc:
                                logging.warning(
                                    "[api_setup] Failed to read pristine patch %s: %s",
                                    rel_path, exc,
                                )
                                content = custom_contents.get(rel_path)
                                source = "pre-wipe stash (pristine read failed)"
                        else:
                            content = custom_contents.get(rel_path)
                            source = "pre-wipe stash (no api_patches/ shipped)"
                        if content is None:
                            continue
                        full_path = os.path.join(api_dir, rel_path.replace("/", os.sep))
                        try:
                            os.makedirs(os.path.dirname(full_path), exist_ok=True)
                            with open(full_path, "wb") as fh:
                                fh.write(content)
                            logging.info("[api_setup] Restored custom file: %s (source: %s)", rel_path, source)
                        except Exception as exc:
                            logging.warning(
                                "[api_setup] Failed to restore custom file %s: %s",
                                rel_path, exc,
                            )

                    self._merge_package_json_dependencies(api_dir, patches_dir)

                finally:
                    try:
                        os.remove(tmp_zip)
                    except Exception:
                        pass

                if self._cancelled:
                    return

            # ── Step 4: npm install ───────────────────────────────────────
            self._set_stage(self._i18n.t("api_setup_npm_install"), *stages["npm_install"])
            # puppeteer's own postinstall script (node_modules/puppeteer/install.mjs)
            # otherwise downloads Chrome silently as part of `npm install` — a
            # multi-hundred-MB download with zero progress feedback, hidden
            # behind the generic "Instalando dependências" label and this
            # stage's indeterminate trickle. On a slow connection or an old
            # HDD this was reported as the install "hanging" for anywhere from
            # a couple of minutes to 30+, always around the same 51-54% mark —
            # right in the middle of the npm_install stage's progress range.
            # Skip it here; step 4.5 below downloads Chrome explicitly, with
            # its own status text, right after npm install actually finishes.
            npm_install_env = {**npm_env, "PUPPETEER_SKIP_DOWNLOAD": "true"}
            ok, err = self._run_subprocess(
                npm_cmd + ["install", "--no-audit", "--no-fund", "--include=optional", "--legacy-peer-deps"],
                cwd=api_dir,
                env=npm_install_env,
            )
            if not ok:
                if not self._cancelled:
                    wx.CallAfter(self._finish_error,
                                 self._i18n.t("api_setup_error_npm_install").format(details=err))
                return

            if self._cancelled:
                return

            # node_modules was just (re)built from scratch by npm install
            # above — apply WinZapp's patches to the files inside it now,
            # same as setup_api.py does for a developer install.
            self._apply_node_modules_patches(api_dir)

            if self._cancelled:
                return

            # ── Step 4.5: download the browser start.js will launch ───────
            # Must match start.js's findPreferredChrome(), which is
            # platform-dependent: chrome-headless-shell is a console-subsystem
            # executable on Windows and its renderer/GPU children each allocate
            # a visible console window, so Windows prefers full Chrome and
            # every other platform prefers the shell (smaller download, faster
            # start, no windowing layer at all). See tests/test_headless_shell.py.
            #
            # Fetching the other one is not a crash, just waste that is easy to
            # miss: start.js finds its preferred binary absent on first launch
            # and downloads a second browser on top of this one, which is what
            # happened while this step was hardcoded to the shell.
            import sys

            browser_product = (
                "chrome" if sys.platform == "win32" else "chrome-headless-shell"
            )
            self._set_stage(self._i18n.t("api_setup_downloading_chrome"), *stages["chrome"])
            ok, err = self._run_subprocess(
                npm_cmd + ["exec", "puppeteer", "browsers", "install", browser_product],
                cwd=api_dir,
                env=npm_env,
            )
            # Do not fail hard if this step fails (e.g. offline setup where Chrome was pre-copied)
            if not ok:
                logging.warning(f"Failed to install Chrome browser via Puppeteer CLI: {err}")

            if self._cancelled:
                return

            # ── Step 4.6: npm run db:generate (only if the script is defined) ──
            # Documented as part of this flow but previously never actually
            # implemented here (only the separate ModuleInstallDialog this
            # dialog now absorbs did it) — added so both operations genuinely
            # match what the module docstring always claimed.
            pkg_json_path = os.path.join(api_dir, "package.json")
            has_db_generate = False
            try:
                with open(pkg_json_path, encoding="utf-8") as fh:
                    pkg = json.load(fh)
                has_db_generate = "db:generate" in pkg.get("scripts", {})
            except Exception:
                pass

            if has_db_generate:
                self._set_stage(self._i18n.t("api_setup_db_generate"), *stages["db_generate"])
                db_env = {**npm_env, "DATABASE_PROVIDER": "postgresql"}
                ok, err = self._run_subprocess(
                    npm_cmd + ["run", "db:generate"],
                    cwd=api_dir,
                    env=db_env,
                )
                if not ok:
                    if not self._cancelled:
                        wx.CallAfter(self._finish_error,
                                     self._i18n.t("api_setup_error_db_generate").format(details=err))
                    return

                if self._cancelled:
                    return

            if not modules_only:
                # ── Step 5: npm run build ─────────────────────────────────────
                self._set_stage(self._i18n.t("api_setup_building"), *stages["build"])
                ok, err = self._run_subprocess(
                    npm_cmd + ["run", "build"],
                    cwd=api_dir,
                    env=npm_env,
                )
                if not ok:
                    if not self._cancelled:
                        wx.CallAfter(self._finish_error,
                                     self._i18n.t("api_setup_error_build").format(details=err))
                    return

            if not self._cancelled:
                wx.CallAfter(self._finish_success)

        except Exception as exc:
            if not self._cancelled:
                wx.CallAfter(self._finish_error, str(exc))

    # ── Process-tree kill ─────────────────────────────────────────────────────

    def _kill_proc_tree(self):
        """Kill the active npm process and all its spawned children."""
        if self._proc and self._proc.poll() is None:
            import sys
            try:
                if sys.platform == "win32":
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(self._proc.pid)],
                        creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                else:
                    self._proc.terminate()
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass

    # ── Event handlers ────────────────────────────────────────────────────────

    def _is_modal_active(self) -> bool:
        """Whether this dialog still owns a running modal event loop.

        Worker completion is delivered through ``wx.CallAfter``. A cancel or
        close event can end ``ShowModal()`` after the worker queues that
        callback but before the callback runs. Calling ``EndModal()`` in that
        state triggers wxWidgets' ``IsRunning()`` assertion, so every terminal
        path checks the actual dialog state instead of trusting the worker's
        earlier ``_cancelled`` snapshot.
        """
        try:
            return bool(self.IsModal())
        except RuntimeError:
            # The native wx object is already being destroyed.
            return False

    def _end_modal_safely(self, result: int) -> bool:
        """End the modal loop once, ignoring callbacks that arrived too late."""
        if not self._is_modal_active():
            logging.info(
                "[api_setup] ignoring late dialog completion result=%s; modal loop is no longer running",
                result,
            )
            return False
        try:
            self.EndModal(result)
            return True
        except (RuntimeError, AssertionError):
            # Defensive second check: a nested native dialog (the success/error
            # MessageBox) can pump pending wx events before control returns here.
            logging.info(
                "[api_setup] modal loop ended before completion result=%s could be applied",
                result,
                exc_info=True,
            )
            return False

    def _on_cancel(self, _event=None):
        if self._cancelled or self._finished:
            return
        self._cancelled = True
        self._finished = True
        self._timer.Stop()
        self._kill_proc_tree()
        self._end_modal_safely(wx.ID_CANCEL)

    def _finish_success(self):
        if self._cancelled or self._finished:
            logging.info("[api_setup] ignoring late/duplicate success callback")
            return
        if not self._is_modal_active():
            logging.info("[api_setup] ignoring success callback after modal loop ended")
            self._finished = True
            return
        self._finished = True
        self._timer.Stop()
        self._trickling = False
        self._gauge.SetValue(100)
        wx.MessageBox(
            self._i18n.t("api_setup_success_message"),
            self._i18n.t("api_setup_success_title"),
            wx.OK | wx.ICON_INFORMATION,
            self,
        )
        self._end_modal_safely(wx.ID_OK)

    def _finish_error(self, details: str = ""):
        if self._cancelled or self._finished:
            logging.info("[api_setup] ignoring late/duplicate error callback")
            return
        if not self._is_modal_active():
            logging.info("[api_setup] ignoring error callback after modal loop ended")
            self._finished = True
            return
        self._finished = True
        self._timer.Stop()
        msg = self._i18n.t("api_setup_error_generic")
        if details:
            msg = f"{msg}\n\n{details}"
        wx.MessageBox(msg, self._i18n.t("api_setup_error_title"), wx.OK | wx.ICON_ERROR, self)
        self._end_modal_safely(wx.ID_CANCEL)
