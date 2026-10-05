"""WppBackgroundUpdateMixin — part of MainWindow (see main_window/__init__.py).

The WPPConnect Server half of Settings > General > "download updates in the
background" (the WinZapp half is update_background.py).

In place, an update stops the server first and keeps WinZapp offline behind a
modal progress window for the whole download, `npm install` and build. Here the
new server is built in a directory next to the running one, by the same
ApiSetupDialog code run with no window; the user keeps working, online. Only
when the build is done does WinZapp say it is about to install, and only then
does _update_wpp_server() stop the server — its install step being two renames
(core/api_staging.py) instead of a rebuild — and start it again.

A build that fails never touched the installed server, which the in-place
update cannot say: it wipes api/ before it builds.
"""

import logging
import threading

import wx

from app_paths import resource_path
from core import api_staging
from core.dialog_foreground import message_box
from update_background import may_interrupt_now


class WppBackgroundUpdateMixin:
    """Build a WPPConnect Server update in the background, then swap it in."""

    #: How often a finished build asks whether it may install yet.
    _WPP_READY_RETRY_MS = 5000

    def _discard_async(self, path: str) -> None:
        """Delete a staging or replaced server off the main thread: it is
        around a gigabyte of small files."""
        if path:
            threading.Thread(target=api_staging.discard, args=(path,), daemon=True,
                             name="winzapp-wpp-staging-discard").start()

    def _discard_wpp_staging_leftovers_async(self) -> None:
        threading.Thread(target=self._discard_wpp_staging_leftovers, daemon=True,
                         name="winzapp-wpp-staging-sweep").start()

    def _discard_wpp_staging_leftovers(self) -> None:
        """Drop what an interrupted background build or swap left on disk.
        Called when an update starts, which is when this process holds the
        machine's WPPConnect update, so no other account can be building."""
        api_dir = resource_path("api")
        for path in api_staging.leftovers_for(api_dir):
            api_staging.discard(path)

    def _stage_wpp_update_in_background(self, target_tag: str, on_finished=None) -> bool:
        """Start building *target_tag* next to the running server. Nothing is
        stopped here. Returns True: from now on the update is this method's,
        including the fall back to an in-place update when the disk has no
        room for a second server.
        """
        api_dir = resource_path("api")
        staged = api_staging.staging_dir_for(api_dir)
        self._wpp_staging = {"tag": target_tag, "dialog": None}

        def _give_up():
            self._wpp_staging = None
            if on_finished is not None:
                on_finished(False)

        def _start(has_room: bool):
            if getattr(self, "_shutting_down", False) or not getattr(self, "_wpp_staging", None):
                _give_up()
                return
            if not has_room:
                logging.warning("[wpp_update] Not enough free space to build %s next to "
                                "the running server - updating in place.", target_tag)
                self._wpp_staging = None
                if self._update_wpp_server(target_tag, on_finished=on_finished,
                                           in_place=True) is False and on_finished is not None:
                    on_finished(False)
                return
            logging.info("[wpp_update] Building %s in the background in %s.", target_tag, staged)
            # The user accepted the prompt and is listening for what is next.
            self.output(self.i18n.t("wpp_update_background_started"), interrupt=True)
            try:
                from ui.dialogs.api_setup import ApiSetupDialog
                # Never shown: on_done makes it run with no window (see its
                # docstring). It is a dialog only because the setup steps live there.
                self._wpp_staging["dialog"] = ApiSetupDialog(
                    self, forced_tag=target_tag, api_dir=staged,
                    on_done=lambda ok, details, cancelled: self._wpp_staging_done(
                        target_tag, staged, on_finished, ok, cancelled),
                )
            except Exception:
                # Without this the build would count as running for the rest of
                # the session and no WPPConnect update could start again.
                logging.exception("[wpp_update] Could not start the background build")
                self._wpp_staging_done(target_tag, staged, on_finished, False, False)

        def _prepare():
            # What an interrupted build left counts against the free space, so
            # it goes first; both are slow enough to stay off the main thread.
            has_room = False
            try:
                self._discard_wpp_staging_leftovers()
                has_room = api_staging.has_room_for_staging(api_dir)
            finally:
                wx.CallAfter(_start, has_room)

        threading.Thread(target=_prepare, daemon=True,
                         name="winzapp-wpp-staging-prepare").start()
        return True

    def _wpp_staging_done(self, target_tag: str, staged: str, on_finished, ok: bool,
                          cancelled: bool) -> None:
        """wx thread: the background build ended."""
        if getattr(self, "_wpp_staging", None):
            self._wpp_staging["dialog"] = None
        if ok:
            self._install_staged_wpp_when_ready(target_tag, staged, on_finished)
            return
        logging.warning("[wpp_update] Background build of %s %s; the installed "
                        "server was not touched.", target_tag,
                        "was cancelled" if cancelled else "failed")
        self._wpp_staging = None
        self._discard_async(staged)
        if not cancelled and not getattr(self, "_shutting_down", False):
            self.error_sound.play()
            text = self.i18n.t("wpp_update_failed_msg")
            message_box(self, text, self.i18n.t("update_error_title"),
                        wx.OK | wx.ICON_ERROR,
                        announce=lambda: self.output(text, interrupt=True))
        if on_finished is not None:
            on_finished(False)

    def _install_staged_wpp_when_ready(self, target_tag: str, staged: str, on_finished) -> None:
        """wx thread: say the update is about to be installed, then hand over
        to _update_wpp_server(), which stops the server, swaps and restarts."""
        if getattr(self, "_shutting_down", False):
            self._wpp_staging = None
            self._discard_async(staged)
            if on_finished is not None:
                on_finished(False)
            return
        if not may_interrupt_now(self):
            wx.CallLater(self._WPP_READY_RETRY_MS, self._install_staged_wpp_when_ready,
                         target_tag, staged, on_finished)
            return
        text = self.i18n.t("wpp_update_background_ready_msg").format(
            version=target_tag.lstrip("vV"))
        message_box(self, text, self.i18n.t("api_update_dialog_title"),
                    wx.OK | wx.ICON_INFORMATION,
                    announce=lambda: self.output(text, interrupt=True))
        self._wpp_staging = None
        started = self._update_wpp_server(target_tag, on_finished=on_finished,
                                          staged_dir=staged)
        if started is False:
            # Refused, and it said why (another account's server came up while
            # this one was building). Nothing was stopped; drop the build.
            self._discard_async(staged)
            if on_finished is not None:
                on_finished(False)

    def _install_staged_wpp(self, staged_dir: str) -> int:
        """The install step of a background update: the server is stopped, so
        the staged build is renamed into place. Answers like
        ApiSetupDialog.ShowModal(): wx.ID_OK, or wx.ID_CANCEL with the old
        server still in place."""
        try:
            replaced = api_staging.swap_in_staged_api(
                resource_path("api"), staged_dir, attempts=4, pause=0.5)
        except api_staging.SwapError as exc:
            logging.error("[wpp_update] Could not put the new server in place: %s", exc)
            self._discard_async(staged_dir)
            return wx.ID_CANCEL
        self._discard_async(replaced)
        return wx.ID_OK

    def cancel_wpp_background_update(self) -> None:
        """Quitting: stop a background build, which would otherwise leave npm
        and Node running after WinZapp is gone. What it wrote is removed by
        the next update. Called on the shutdown thread, so only through
        cancel_background(), which kills npm here and leaves wx to the main
        thread."""
        staging = getattr(self, "_wpp_staging", None)
        dialog = staging.get("dialog") if staging else None
        if dialog is None:
            return
        try:
            dialog.cancel_background()
        except Exception:
            logging.exception("[wpp_update] Could not cancel the background build")
