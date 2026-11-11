"""WinZapp updates downloaded on their own and installed when the app exits.

Settings > General > "download updates automatically and install them when I
exit WinZapp" (install-wide, off by default). With it, a newer release in the
selected channel is downloaded, signature-checked and extracted on a thread
with no window and no question (the same download_update_package() every other
path uses, so the release-integrity checks are identical,
docs/traps/release-integrity.md). The package then waits. The next time the
user chooses Exit (the menu item or the tray's), it is installed — with the
usual progress window, because that is what tells a screen reader what is
happening — and WinZapp is NOT started again afterwards: it stays closed and
the new version runs the next time the user opens it.

Closing the window to the tray is not an exit and installs nothing.
"""

import logging
import os
import threading

import wx

from update_package import discard_package, download_update_package

SETTING = "update_on_exit"


def update_on_exit_enabled(settings) -> bool:
    """Only an explicit True: a hand-edited value must not change how an
    update is installed."""
    try:
        return settings.get("general", {}).get(SETTING, False) is True
    except Exception:
        return False


class UpdateOnExitMixin:
    """UpdateChecker's download-now, install-on-exit path."""

    def _stage_for_exit(self, new_version: str, zip_url: str, sha256sums_url: str = "",
                        signature_url: str = "", is_alpha: bool = False,
                        announce: bool = False) -> None:
        """Start the silent download. The per-machine prompt claim and
        _background_version stay held until the update installs, so no second
        download or prompt starts beside it."""
        mw = self._mw
        self._background_version = new_version
        self._staged_update = None
        logging.info("Auto-updater: downloading %s to install when WinZapp exits.", new_version)
        if announce:
            wx.CallAfter(mw.output, mw.i18n.t("update_on_exit_started"), True)
        args = (new_version, zip_url, sha256sums_url, signature_url, is_alpha)
        threading.Thread(target=self._stage_worker, args=(args,), daemon=True,
                         name="winzapp-update-stage").start()

    def _stage_worker(self, args) -> None:
        new_version, zip_url, sha256sums_url, signature_url, is_alpha = args
        mw = self._mw
        try:
            package = download_update_package(
                zip_url, sha256sums_url, signature_url, new_version, is_alpha, mw.i18n,
                is_cancelled=lambda: bool(getattr(mw, "_shutting_down", False)),
            )
        except Exception:
            logging.exception("Auto-updater: download for install on exit failed")
            self._end_background_update()
            return
        if package.cancelled or package.error:
            if package.error:
                logging.warning("Auto-updater: download for install on exit failed: %s",
                                package.error)
            self._end_background_update()
            return
        self._staged_update = (args, package.extract_dir)
        logging.info("Auto-updater: %s is ready and will install when WinZapp exits.",
                     new_version)
        wx.CallAfter(mw.output, mw.i18n.t("update_on_exit_ready").format(version=new_version),
                     False)

    def _say_staged_or_background_update(self) -> None:
        """A check the user asked for while an update is held or downloading."""
        mw = self._mw
        if getattr(self, "_staged_update", None):
            mw.output(mw.i18n.t("update_on_exit_ready").format(
                version=self._background_update_version()), interrupt=True)
            return
        self._say_background_update_running()

    def _continue_exit_without_install(self) -> None:
        """The install an Exit started ended without launching (cancelled, or
        failed and not retried): the user still asked to exit, so quit as a
        normal Exit would, instead of leaving a window nobody expects."""
        callback = getattr(self, "_exit_fallback", None)
        self._exit_fallback = None
        if callback is not None:
            wx.CallAfter(callback)

    def install_staged_on_exit(self) -> bool:
        """Main thread, from the Exit command. True when the install took over
        the exit (its dialog ends in real_exit(), without relaunching); False
        when nothing is staged and the normal quit goes ahead."""
        staged = getattr(self, "_staged_update", None)
        if not staged or getattr(self, "_install_pending", False):
            return False
        args, extract_dir = staged
        self._staged_update = None
        self._background_version = ""
        if not update_on_exit_enabled(getattr(self._mw, "settings", None)):
            # Turned off after the download: no silent install.
            logging.info("Auto-updater: update on exit was turned off; discarding the package.")
            discard_package(extract_dir)
            self._release_prompt()
            return False
        if not extract_dir or not os.path.isdir(extract_dir):
            logging.warning("Auto-updater: the staged update is gone; quitting normally.")
            discard_package(extract_dir)
            self._release_prompt()
            return False
        logging.info("Auto-updater: installing %s on exit.", args[0])
        # A normal Exit (staged is empty now) when the install does not launch.
        self._exit_fallback = self._mw.quit_all_accounts
        self._do_install(*args, extracted_dir=extract_dir, relaunch_app=False)
        return True

    # A failed download is only logged: this mode is silent by design, and the
    # periodic check tries again.
