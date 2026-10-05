"""WinZapp updates downloaded in the background.

Settings > General > "download updates in the background" (install-wide, off
by default). Without it an accepted update opens UpdateProgressDialog at once:
a modal window that keeps the user out of the app for the whole download. With
it the download, the signature check and the extraction happen on a thread
with no window, the user keeps working, and only once the package is ready
does WinZapp say it is about to install — then the usual install phase runs
(other accounts are asked to quit, the batch installer is launched, the app
exits).

Nothing about WHAT is installed changes: the same download_update_package()
runs either way, so the release-integrity checks are identical
(docs/traps/release-integrity.md), and the per-machine prompt claim is held
across the background download exactly as it is across the dialog
(docs/traps/updater-channels.md).

The WPPConnect Server half of the same option lives in
main_window/wpp_background_update.py.
"""

import logging
import threading

import wx

from core.dialog_foreground import message_box
from update_package import discard_package, download_update_package

SETTING = "background_update_downloads"


def background_downloads_enabled(settings) -> bool:
    """Only an explicit True: a hand-edited value must not change how an
    update is installed."""
    try:
        return settings.get("general", {}).get(SETTING, False) is True
    except Exception:
        return False


def may_interrupt_now(main_window) -> bool:
    """Whether the app may be taken down (or its API restarted) right now.

    A background download ends at a moment the user did not choose, so the
    install waits for one that costs nothing: not before the main window
    exists, not over a pairing (the same rule both update prompts follow), and
    never in the middle of a voice or video call, which the restart would cut
    — one in progress, or one still ringing. Not while the WPPConnect Server
    is being reinstalled in place either: quitting then would leave api/
    half-built with npm still running. And not over a voice message being
    recorded, which the notice's only button would throw away.
    """
    ready = getattr(main_window, "_ui_ready_event", None)
    if ready is not None and not ready.is_set():
        return False
    may_run = getattr(main_window, "wpp_update_may_run_now", None)
    if callable(may_run) and not may_run():
        return False
    in_call = getattr(main_window, "_voice_call_in_progress", None)
    if callable(in_call) and in_call():
        return False
    if getattr(main_window, "_incoming_call_dialogs", None):
        return False
    if getattr(main_window, "_wpp_updating", False):
        return False
    panel = getattr(main_window, "conversations_panel", None)
    if getattr(panel, "_is_recording", False) or getattr(panel, "_recording_starting", False):
        return False
    return True


class BackgroundDownloadMixin:
    """UpdateChecker's background download. Reached from _do_install() when
    the option is on; hands back to _do_install() with the package ready."""

    #: How often the finished download asks whether it may install yet.
    _READY_RETRY_MS = 5000

    def _background_update_version(self) -> str:
        """The version being downloaded in the background or waiting to be
        installed, or "". While it is set no second download may start and no
        second prompt may open: with the main window free, "check for
        updates" is within reach the whole time."""
        return getattr(self, "_background_version", "") or ""

    def _say_background_update_running(self) -> None:
        mw = self._mw
        mw.output(mw.i18n.t("update_background_running").format(
            version=self._background_update_version()), interrupt=True)

    def _end_background_update(self, retry: bool = True) -> None:
        """The background update is over without an install: forget it, hand
        the per-machine prompt back and, unless the app is going away for
        good, look again later. A shutdown Windows cancelled leaves the app
        running, and without the retry nothing would ever offer the update
        again until the next launch."""
        self._background_version = ""
        self._release_prompt()
        if retry:
            self._schedule_retry()

    def _download_in_background(self, new_version: str, zip_url: str, sha256sums_url: str = "",
                                signature_url: str = "", is_alpha: bool = False):
        mw = self._mw
        self._background_version = new_version
        logging.info("Auto-updater: downloading %s in the background.", new_version)
        # The user just said Yes and is listening for what happens next.
        mw.output(mw.i18n.t("update_background_started"), interrupt=True)
        threading.Thread(
            target=self._background_download_worker,
            args=(new_version, zip_url, sha256sums_url, signature_url, is_alpha),
            daemon=True, name="winzapp-update-download",
        ).start()

    def _background_download_worker(self, *args):
        new_version, zip_url, sha256sums_url, signature_url, is_alpha = args
        mw = self._mw
        try:
            package = download_update_package(
                zip_url, sha256sums_url, signature_url, new_version, is_alpha, mw.i18n,
                is_cancelled=lambda: bool(getattr(mw, "_shutting_down", False)),
            )
        except Exception as exc:
            logging.exception("Auto-updater: background download failed")
            wx.CallAfter(self._background_download_failed, args, str(exc))
            return
        if package.cancelled:
            # Only quitting cancels a background download.
            self._end_background_update()
            return
        if package.error:
            wx.CallAfter(self._background_download_failed, args, package.error)
            return
        wx.CallAfter(self._install_downloaded_update, args, package.extract_dir)

    def _background_download_failed(self, args, error: str):
        """Main thread. The same question the progress dialog's failure asks."""
        mw = self._mw
        if getattr(mw, "_shutting_down", False):
            self._end_background_update()
            return
        i18n = mw.i18n
        text = i18n.t("update_error_msg").format(error=error)
        retry = message_box(
            mw, text, i18n.t("update_error_title"), wx.YES_NO | wx.ICON_ERROR,
            announce=lambda: mw.output(text, interrupt=True),
        )
        if retry == wx.YES:
            self._download_in_background(*args)
            return
        self._end_background_update()

    def _install_downloaded_update(self, args, extract_dir: str):
        """Main thread. Say the update is about to be installed, then install."""
        mw = self._mw
        if getattr(mw, "_shutting_down", False):
            discard_package(extract_dir)
            self._end_background_update()
            return
        if not may_interrupt_now(mw):
            wx.CallLater(self._READY_RETRY_MS, self._install_downloaded_update,
                         args, extract_dir)
            return
        text = mw.i18n.t("update_background_ready_msg").format(version=args[0])
        message_box(
            mw, text, mw.i18n.t("update_progress_title"), wx.OK | wx.ICON_INFORMATION,
            announce=lambda: mw.output(text, interrupt=True),
        )
        try:
            self._do_install(*args, extracted_dir=extract_dir)
        finally:
            # Still here: the install did not take the app down (cancelled,
            # failed, or a dev run). _do_install() already released the prompt.
            self._background_version = ""
