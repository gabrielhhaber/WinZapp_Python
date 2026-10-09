"""UpdatesMixin — part of MainWindow (see main_window/__init__.py).

Moved verbatim out of main.py. Methods run with ``self`` bound to the
MainWindow instance, so every attribute set in MainWindow.__init__ is
available here.
"""

import logging
import threading
import wx

from app_paths import resource_path
from core import api_staging
from core.dialog_foreground import bring_to_front_if_hidden, message_box
from core.wpp_runtime import homologated_wpp_tag
from core.wpp_update_health import wait_for_port_closed
from core.api_install_timing import timed_call, timed_step
from core.wpp_connection_recovery import begin_update_reconnection
from update_background import background_downloads_enabled
from main_window.wpp_update_validation import restart_api_after_update


def should_roll_back(server_built: bool, target_tag: str, minimum_tag: str) -> bool:
    """After a failed server install: reinstall the bundled minimum once?

    Only when the failure left no server to run (the install wipes api/ before
    it builds, so a failed build leaves none) and the minimum is a different
    tag than the one that just failed. A user cancel that left the old build in
    place needs nothing.
    """
    return bool(minimum_tag) and not server_built and minimum_tag != target_tag


def _server_is_built() -> bool:
    import os
    return os.path.isfile(resource_path("api", "dist", "server.js"))


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
        ready = getattr(self, "_ui_ready_event", None)
        if ready is not None and not ready.is_set():
            return False  # still inside MainWindow.__init__: no window to ask from
        api_start = getattr(self, "_api_start_settled", None)
        if api_start is not None and not api_start.is_set():
            # Node still starting behind the window (issue #407): the same
            # moment this used to be still inside __init__'s startup dialog.
            return False
        try:
            if self._is_pairing_dialog_active():
                return False
        except Exception:
            return False
        return not getattr(self, "_pairing_in_progress", False)

    def _start_wpp_update_checker(self, force: bool = False):
        updates_enabled = self.settings.get("general", {}).get("updates_enabled", True)
        if not updates_enabled and not force:
            return
        if not force and not self.wpp_update_may_run_now():
            logging.info(
                "[wpp_update] Pairing in progress or the window is not ready yet — "
                "deferring the WPPConnect update check by 5 minutes."
            )
            wx.CallLater(300000, self._start_wpp_update_checker)
            return
        from updater import WppUpdateChecker
        self._wpp_update_checker = WppUpdateChecker(self)
        if force:
            self._wpp_update_checker.force_check()
        else:
            sweep = getattr(self, "_discard_stale_wpp_servers_async", None)
            if sweep is not None:
                sweep()
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

    def _update_wpp_server(self, target_tag: str, on_finished=None, staged_dir=None,
                           in_place: bool = False):
        """
        Stop the running WPPConnect Server, reinstall it at *target_tag* and

        Returns False when it refused to start (an update is already running,
        or another account's Node is still running from the shared api/ that
        the reinstall wipes), True once started. *on_finished* is called, on
        the wx thread, when a started update ends, with True when it installed
        and False when it failed or was cancelled.

        All builds use a sibling directory, so the current API survives a
        failed build. With background downloads enabled it keeps running
        during the build; otherwise the build has a modal progress window.
        The replaced API is retained until HTTP validation succeeds and is
        restored if the new API cannot start. *in_place* selects foreground
        progress for the existing caller; it never overwrites the old tree.
        """
        if getattr(self, "_wpp_staging", None):
            # Being built in the background: say so, the user may have asked
            # for a reinstall from the menu and would otherwise hear nothing.
            logging.info("[wpp_update] An update is being built in the background — "
                         "ignoring the request to update to %s.", target_tag)
            self.output(self.i18n.t("wpp_update_background_running"), interrupt=True)
            return False
        if getattr(self, "_wpp_updating", False):
            logging.info("[wpp_update] An update is already running — ignoring "
                         "the request to update to %s.", target_tag)
            return False

        from core.wa_version_refresh import other_accounts_node_alive
        if other_accounts_node_alive(getattr(self, "global_dir", None),
                                     getattr(self, "account_id", None),
                                     ignore_corrupt=True):
            # The reinstall deletes the api/ folder in place; another account
            # whose Node runs from it would lose its server mid-session.
            logging.warning("[wpp_update] Refusing to update to %s: another "
                            "account's WPPConnect Server is still running.", target_tag)
            self.error_sound.play()
            message = self.i18n.t("wpp_update_other_account_msg")
            message_box(self, message, self.i18n.t("update_error_title"),
                        wx.OK | wx.ICON_INFORMATION,
                        announce=lambda: self.output(message, interrupt=True))
            return False

        stage = getattr(self, "_stage_wpp_update_in_background", None)
        if (staged_dir is None and not in_place and stage is not None
                and background_downloads_enabled(getattr(self, "settings", None))):
            return stage(target_tag, on_finished)
        api_dir = resource_path("api")
        build_dir = staged_dir or api_staging.staging_dir_for(api_dir)
        logging.info("[wpp_update] Stopping WPPConnect Server before update to %s...", target_tag)
        # Spoken in a background start too: the user just accepted the prompt,
        # so they are listening for what happens next.
        self.output(self.i18n.t("wpp_update_in_progress"), interrupt=True)
        # Set before stopping the server and only cleared in `finally` below —
        # the health checker (running on its own thread every 30s) would
        # otherwise catch the server mid-stop/reinstall/restart, fail its
        # status-session probe, and declare the app offline/disconnected even
        # though the actual WhatsApp session never dropped.
        self._wpp_updating = True
        backup = ""
        stop_failed = False
        no_room = False

        @timed_step("api_stop_phase")
        def _stop_phase():
            nonlocal stop_failed, no_room
            try:
                if not staged_dir:
                    # What an interrupted build left counts against the free
                    # space, so it goes first (as in the background path).
                    timed_call("staging_cleanup", self._discard_wpp_staging_leftovers)
                    if not api_staging.has_room_for_staging(api_dir):
                        # Building over the old API would lose the rollback
                        # copy. Refuse before stopping it when there is no room
                        # for a second build.
                        no_room = True
                        return
                timed_call("api_stop", self._stop_wpp_server)
                self.wpp_process = None

                # _stop_wpp_server() has already closed the session and waited
                # for Chrome to release the profile. Kill only what is still
                # holding it — same reasoning as the wake path above.
                released = timed_call("profile_release", self.wait_for_profile_release,
                    (getattr(self, "token", "") or "").split(":")[0], timeout=10.0)
                running = getattr(self, "_is_wpp_running", lambda: False)
                if released is False or not wait_for_port_closed(running):
                    raise TimeoutError("previous API or Chrome profile is still in use")
            except Exception:
                stop_failed = True
                logging.exception("[wpp_update] Could not stop the previous API; installation preserved")
            finally:
                try:
                    wx.CallAfter(_after_stop)
                except Exception:
                    logging.exception("[wpp_update] Could not resume the update "
                                      "on the wx main thread")
                    self._wpp_updating = False
                    if on_finished is not None:
                        on_finished(False)

        def _run_install(tag):
            nonlocal backup
            if staged_dir and tag == target_tag:
                result, user_cancelled = wx.ID_OK, False
            else:
                result, user_cancelled = _build_install(tag)
            if result == wx.ID_OK:
                try:
                    backup = timed_call("api_swap", api_staging.swap_in_staged_api,
                                        api_dir, build_dir, attempts=4, pause=0.5)
                except api_staging.SwapError:
                    logging.exception("[wpp_update] Could not install the staged API")
                    return wx.ID_CANCEL, False
            return result, user_cancelled

        def _build_install(tag):
            from ui.dialogs.api_setup import ApiSetupDialog
            dlg = ApiSetupDialog(
                self,
                title_override=self.i18n.t("api_update_dialog_title"),
                forced_tag=tag,
                api_dir=build_dir,
            )
            # The prompt that led here has closed, which hands the
            # foreground to some other window; a hidden main window needs
            # the progress dialog pulled forward again.
            bring_to_front_if_hidden(self, dlg)
            result = dlg.ShowModal()
            # ApiSetupDialog ends with ID_CANCEL both for a failure (after it
            # showed its own error box) and for the user's Cancel; only the
            # latter sets _cancelled.
            user_cancelled = bool(getattr(dlg, "_cancelled", False))
            dlg.Destroy()
            return result, user_cancelled

        def _after_stop():
            deferred = False
            try:
                if no_room:
                    logging.error("[wpp_update] Insufficient space to retain the current API")
                    self.error_sound.play()
                    text = self.i18n.t("wpp_update_not_enough_space")
                    message_box(self, text, self.i18n.t("update_error_title"), wx.OK | wx.ICON_ERROR,
                                announce=lambda: self.output(text, interrupt=True))
                    return
                if stop_failed:
                    restart_api_after_update(self, api_dir, "", lambda _ok: _validated(False))
                    deferred = True
                    return
                result, user_cancelled = _run_install(target_tag)

                if result != wx.ID_OK:
                    logging.warning("[wpp_update] Update to %s was %s.", target_tag,
                                    "cancelled by the user" if user_cancelled else "not completed")
                    if not user_cancelled:
                        # A cancel needs no "could not update" box; the user just
                        # asked for it. A failure does.
                        self.error_sound.play()
                        message_box(
                            self,
                            self.i18n.t("wpp_update_failed_msg"),
                            self.i18n.t("update_error_title"),
                            wx.OK | wx.ICON_ERROR,
                            announce=lambda: self.output(self.i18n.t("wpp_update_failed_msg"), interrupt=True),
                        )
                    # Normally the previous API is intact. The bundled minimum
                    # is still the last resort for an already-missing server.
                    minimum = homologated_wpp_tag(resource_path("wpp_minimum_version.txt"))
                    if should_roll_back(_server_is_built(), target_tag, minimum):
                        logging.error("[wpp_update] No server left after the failed "
                                      "update to %s - reinstalling the bundled "
                                      "minimum %s.", target_tag, minimum)
                        self.output(self.i18n.t("wpp_update_in_progress"), interrupt=True)
                        if _run_install(minimum)[0] == wx.ID_OK:
                            logging.info("[wpp_update] Restored WPPConnect Server %s.", minimum)
                        else:
                            logging.error("[wpp_update] Restoring %s failed too; the "
                                          "server stays down until the next start.", minimum)
                    # A failed or cancelled build leaves its half-built tree
                    # behind; it would count against the room check of every
                    # later update. After the rollback above, which builds
                    # into the same directory. Nothing is left to delete when
                    # that build was swapped in.
                    threading.Thread(target=api_staging.discard, args=(build_dir,),
                                     daemon=True).start()
                    restart_api_after_update(self, api_dir, backup,
                                             lambda _ok: _validated(False, report_failure=False))
                    deferred = True
                    return

                logging.info("[wpp_update] WPPConnect Server updated to %s — restarting...", target_tag)
                restart_api_after_update(self, api_dir, backup, _validated)
                deferred = True
            except Exception:
                logging.exception("[wpp_update] Installing the API failed")
            finally:
                if not deferred:
                    _finish(False)

        def _validated(succeeded, report_failure=True):
            begin_update_reconnection(self)
            def _recover_after_update():
                try:
                    self._reconnect_websocket_now()
                    self.check_wa_connection_http()
                except Exception:
                    logging.exception("[wpp_update] Post-update reconnection failed")
            try:
                if not succeeded:
                    if report_failure:
                        self.error_sound.play()
                        text = self.i18n.t("wpp_update_failed_msg")
                        message_box(self, text, self.i18n.t("update_error_title"), wx.OK | wx.ICON_ERROR,
                                    announce=lambda: self.output(text, interrupt=True))
                    return

                if getattr(self, "_window_hidden", False) and not self.background_mode:
                    wx.CallAfter(self.restore_window)

                self.output(self.i18n.t("wpp_update_complete"), interrupt=True)
            finally:
                _finish(succeeded)
                # Release the install guard before any worker can auto-start.
                threading.Thread(target=_recover_after_update, daemon=True).start()

        def _finish(succeeded):
            self._wpp_updating = False
            if on_finished is not None:
                on_finished(succeeded)

        threading.Thread(target=_stop_phase, daemon=True,
                         name="winzapp-wpp-update-stop").start()
        return True
