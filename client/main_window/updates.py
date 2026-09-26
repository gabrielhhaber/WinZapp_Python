"""UpdatesMixin — part of MainWindow (see main_window/__init__.py).

Moved verbatim out of main.py. Methods run with ``self`` bound to the
MainWindow instance, so every attribute set in MainWindow.__init__ is
available here.
"""

import logging
import threading
import wx


class UpdatesMixin:
    """App and WPPConnect Server update checks triggered from the menu or the
    background checkers.
    """

    def _on_force_update(self, event):
        if self._update_checker is None:
            self._start_update_checker(force=True)
        else:
            self._update_checker.force_check()

    def _on_force_reinstall_zip(self, event):
        """
        Help > Force Reinstall from ZIP: always downloads and reinstalls the
        latest GitHub release's ZIP, regardless of whether it's actually
        newer than the running version — unlike _on_force_update(), which
        only checks and installs when a newer version exists.
        """
        if self._update_checker is None:
            from updater import UpdateChecker
            self._update_checker = UpdateChecker(self)
        self._update_checker.force_reinstall()

    # ── Auto-updater ──────────────────────────────────────────────────────────

    def _announce_previous_update_failure(self):
        """Tell the user the last update never landed.

        Spoken, shown and with the error sound, like the other startup dead
        ends: the batch installer failed after this process had already
        exited, so nothing else in the app ever had a chance to say so.
        """
        try:
            self.error_sound.play()
        except Exception:
            pass
        try:
            self.output(self.i18n.t("update_failed_previous"), interrupt=False)
            wx.MessageBox(
                self.i18n.t("update_failed_previous"),
                self.i18n.t("update_error_title"),
                wx.OK | wx.ICON_ERROR,
            )
        except Exception:
            logging.exception("[UPDATER_STATUS] announcing the failed update failed")

    def _start_update_checker(self, force: bool = False):
        updates_enabled = self.settings.get("general", {}).get("updates_enabled", True)
        if not updates_enabled and not force:
            return
        from updater import UpdateChecker
        self._update_checker = UpdateChecker(self)
        if force:
            self._update_checker.force_check()
        else:
            self._update_checker.start()

    # ── WPPConnect Server updater ───────────────────────────────────────────────
    # Independent of WinZapp's own auto-updater above: the WPPConnect Server
    # WinZapp bundles breaks upstream between WinZapp releases too, and the
    # only fix used to be a user manually deleting client/api/ and node_modules
    # and letting WinZapp reinstall from scratch. This checks the actual
    # wppconnect-team/wppconnect-server GitHub releases directly.

    def wpp_update_may_run_now(self) -> bool:
        try:
            if self._is_pairing_dialog_active():
                return False
        except Exception:
            return False
        return not getattr(self, "_pairing_in_progress", False)

    def _start_wpp_update_checker(self, force: bool = False):
        if self.background_mode:
            return
        updates_enabled = self.settings.get("general", {}).get("updates_enabled", True)
        if not updates_enabled and not force:
            return
        if not force and not self.wpp_update_may_run_now():
            logging.info(
                "[wpp_update] Pairing in progress — deferring the WPPConnect "
                "update check by 5 minutes."
            )
            wx.CallLater(300000, self._start_wpp_update_checker)
            return
        from updater import WppUpdateChecker
        self._wpp_update_checker = WppUpdateChecker(self)
        if force:
            self._wpp_update_checker.force_check()
        else:
            self._wpp_update_checker.start()



    def _on_force_reinstall_wpp(self, event):
        """
        Ajuda > Forçar reinstalação da WPPConnect: always fetches whatever is
        currently the latest wppconnect-server release and replaces the
        installed one with it, regardless of version — the same
        stop → reinstall → restart flow the periodic checker uses, just
        without the "is it actually newer" comparison first. Meant to recover
        a broken/corrupted API install without waiting for a real version
        bump upstream.
        """
        if self._wpp_update_checker is None:
            from updater import WppUpdateChecker
            self._wpp_update_checker = WppUpdateChecker(self)
        self._wpp_update_checker.force_reinstall()

    def _update_wpp_server(self, target_tag: str):
        """
        Stop the running WPPConnect Server, reinstall it at *target_tag* and
        """
        if getattr(self, "_wpp_updating", False):
            logging.info("[wpp_update] An update is already running — ignoring "
                         "the request to update to %s.", target_tag)
            return

        logging.info("[wpp_update] Stopping WPPConnect Server before update to %s...", target_tag)
        if not self.background_mode:
            self.output(self.i18n.t("wpp_update_in_progress"), interrupt=True)
        # Set before stopping the server and only cleared in `finally` below —
        # the health checker (running on its own thread every 30s) would
        # otherwise catch the server mid-stop/reinstall/restart, fail its
        # status-session probe, and declare the app offline/disconnected even
        # though the actual WhatsApp session never dropped.
        self._wpp_updating = True

        def _stop_phase():
            try:
                self._stop_wpp_server()
                self.wpp_process = None

                # _stop_wpp_server() has already closed the session and waited
                # for Chrome to release the profile. Kill only what is still
                # holding it — same reasoning as the wake path above.
                self.wait_for_profile_release(
                    (getattr(self, "token", "") or "").split(":")[0], timeout=10.0)
            except Exception:
                logging.exception("[wpp_update] Stopping the server before the "
                                  "update failed — reinstalling anyway, which is "
                                  "what the user asked for")
            finally:
                try:
                    wx.CallAfter(_after_stop)
                except Exception:
                    logging.exception("[wpp_update] Could not resume the update "
                                      "on the wx main thread")
                    self._wpp_updating = False

        def _after_stop():
            try:
                from ui.dialogs.api_setup import ApiSetupDialog
                dlg = ApiSetupDialog(
                    self,
                    title_override=self.i18n.t("api_update_dialog_title"),
                    forced_tag=target_tag,
                )
                result = dlg.ShowModal()
                dlg.Destroy()

                if result != wx.ID_OK:
                    logging.warning("[wpp_update] Update to %s was cancelled or failed.", target_tag)
                    self.error_sound.play()
                    wx.MessageBox(
                        self.i18n.t("wpp_update_failed_msg"),
                        self.i18n.t("update_error_title"),
                        wx.OK | wx.ICON_ERROR,
                        self,
                    )
                    self.ensure_wpp_running()
                    return

                logging.info("[wpp_update] WPPConnect Server updated to %s — restarting...", target_tag)
                self.ensure_wpp_running()

                def _recover_after_update():
                    try:
                        self._reconnect_websocket_now()
                        self.check_wa_connection_http()
                        self.trigger_sync_if_needed()
                    except Exception:
                        logging.exception("[wpp_update] Post-update reconnection failed")
                threading.Thread(target=_recover_after_update, daemon=True).start()

                if getattr(self, "_window_hidden", False) and not self.background_mode:
                    wx.CallAfter(self.restore_window)

                if not self.background_mode:
                    self.output(self.i18n.t("wpp_update_complete"), interrupt=True)
            finally:
                self._wpp_updating = False

        threading.Thread(target=_stop_phase, daemon=True,
                         name="winzapp-wpp-update-stop").start()
