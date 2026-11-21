"""UpdateChecker's last look at GitHub between the user's Yes and the download.

Alphas are published back to back, so the release the dialog offered may
already have a successor when the user answers. Reached from
UpdateChecker._show_update_dialog(); ends in _do_install().
"""

import logging
import threading

import wx

from version import __version__


class ReconfirmMixin:
    def _reconfirm_then_install(self, remote_version: str, zip_url: str, sha256sums_url: str,
                                signature_url: str, is_alpha: bool):
        """Ask GitHub again which release is the newest before downloading.
        The fetch runs off the UI thread; until _install_reconfirmed() hands
        over to _do_install(), _install_pending keeps a manual check from
        opening a second prompt beside this one."""
        self._install_pending = True
        offered = (remote_version, zip_url, sha256sums_url, signature_url, is_alpha)
        threading.Thread(target=self._reconfirm_worker, args=offered, daemon=True).start()

    def _reconfirm_worker(self, *offered):
        import updater
        try:
            newest = updater.newer_release_than_offered(
                self._fetch_releases(), self._alpha_enabled(), __version__, offered[0])
            if newest is not None:
                assets = newest.get("assets", [])
                offered = (
                    (newest.get("tag_name") or "").lstrip("vV"), updater.find_zip_asset(assets),
                    updater._find_sha256sums_asset(assets), updater._find_signature_asset(assets),
                    updater.is_alpha_release(newest),
                )
                logging.info("Auto-updater: %s superseded the offered release — installing it instead.",
                             offered[0])
        except Exception:
            # Cannot ask: the offered release is still a valid update.
            logging.exception("Auto-updater: could not re-confirm the latest release")
        wx.CallAfter(self._install_reconfirmed, *offered)

    def _install_reconfirmed(self, *args):
        self._install_pending = False   # _do_install() refuses while it is set
        if getattr(self._mw, "_shutting_down", False):
            from core.sound_selection import set_pending_mode
            set_pending_mode(None)   # the choice belonged to this install
            self._release_prompt()
            self._schedule_retry()
            return
        # The prompt claim stays held across the install, as in _show_update_dialog().
        self._do_install(*args)
