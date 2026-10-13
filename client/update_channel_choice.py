"""One explicitly requested alpha offer, without changing the saved channel.

The normal updater still owns release eligibility, signatures and installation.
This mixin only owns the additional consent dialog and its prompt claim.
"""
import logging

import wx

from core.dialog_foreground import message_box


class AlphaChannelChoiceMixin:
    def _queue_alpha_channel_choice(self, release: dict, changelog: str) -> None:
        with self._channel_choice_lock:
            if self._channel_choice_pending:
                return
            self._channel_choice_pending = True
            token = self._channel_choice_token = object()
            version = release["tag_name"].lstrip("vV")
            claimed = self._claim_prompt(version)
            if not claimed:
                self._channel_choice_pending = False
                self._channel_choice_token = None
        if not claimed:
            self._schedule_retry()
            wx.CallAfter(self._show_prompt_open_elsewhere)
            return
        try:
            wx.CallAfter(self._show_alpha_channel_choice, release, changelog, token)
        except Exception:
            self._finish_alpha_channel_choice(token)
            raise

    def _finish_alpha_channel_choice(self, token) -> None:
        with self._channel_choice_lock:
            if token is not self._channel_choice_token:
                return
            self._channel_choice_pending = False
            self._channel_choice_token = None
            if getattr(self, "_install_pending", False) or getattr(self, "_background_version", ""):
                return
            self._release_prompt()
            if not self._channel_choice_cancelled:
                self._schedule_retry()

    def _show_alpha_channel_choice(self, release: dict, changelog: str, token) -> None:
        with self._channel_choice_lock:
            if token is not self._channel_choice_token:
                return
        if getattr(self, "_install_pending", False) or getattr(self, "_background_version", ""):
            # An accepted installation now owns the claim; a stale consent
            # callback must not hand that installation's claim back.
            self._finish_alpha_channel_choice(token)
            return
        if self._channel_choice_cancelled or getattr(self._mw, "_shutting_down", False):
            self._finish_alpha_channel_choice(token)
            return
        if not self._main_window_ready():
            try:
                wx.CallLater(self._UI_NOT_READY_RETRY_MS, self._show_alpha_channel_choice,
                             release, changelog, token)
            except Exception:
                logging.exception("Auto-updater: could not defer the alpha choice")
                self._finish_alpha_channel_choice(token)
            return
        handed_over = False
        try:
            from updater import (__version__, find_zip_asset, _find_sha256sums_asset,
                                 _find_signature_asset, is_alpha_release)
            i18n = self._mw.i18n
            title = i18n.t("update_alpha_choice_title")
            message = i18n.t("update_alpha_choice_message").format(
                current=__version__, new=release["tag_name"].lstrip("vV"))
            answer = message_box(
                self._mw, message, title,
                wx.YES_NO | wx.NO_DEFAULT | wx.ICON_INFORMATION,
                announce=lambda: self._mw.output(f"{title}. {message}", interrupt=True),
            )
            if answer != wx.YES:
                return
            with self._channel_choice_lock:
                if self._channel_choice_cancelled or token is not self._channel_choice_token:
                    return
            # Consent covers this snapshot only. Do not turn the install-wide
            # checkbox on, and do not refetch a different alpha after consent.
            self._show_update_dialog(
                release["tag_name"].lstrip("vV"), changelog,
                find_zip_asset(release.get("assets", [])),
                _find_sha256sums_asset(release.get("assets", [])),
                signature_url=_find_signature_asset(release.get("assets", [])),
                is_alpha=is_alpha_release(release),
            )
            handed_over = True
            with self._channel_choice_lock:
                if token is self._channel_choice_token:
                    self._channel_choice_pending = False
                    self._channel_choice_token = None
        except Exception:
            logging.exception("Auto-updater: the alpha channel choice failed")
        finally:
            if not handed_over:
                self._finish_alpha_channel_choice(token)
