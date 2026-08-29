import os
import sys
import time
import threading
import socketio
import wx
import requests
from core.api_client import (api_get, api_post, redact_api_error,
                             redact_api_url, redact_token)
from core.i18n import I18n
from core.websocket_client import WebSocketClient
from app_paths import data_path, resource_path
from traceback import format_exc
import json
import base64
from io import BytesIO
from countries import get_countries
import logging


# Events forwarded to the WinZapp client via Socket.IO
_WEBSOCKET_EVENTS = [
    "CALL", "APPLICATION_STARTUP", "QRCODE_UPDATED",
    "MESSAGES_SET", "MESSAGES_UPSERT", "MESSAGES_UPDATE", "MESSAGES_DELETE",
    "SEND_MESSAGE", "CONTACTS_SET", "CONTACTS_UPSERT", "CONTACTS_UPDATE",
    "PRESENCE_UPDATE", "CHATS_SET", "CHATS_UPSERT", "CHATS_UPDATE", "CHATS_DELETE",
    "CONNECTION_UPDATE", "GROUPS_UPSERT", "GROUP_UPDATE", "GROUP_PARTICIPANTS_UPDATE",
]


class Connect:
    def __init__(self, main_window):
        self.main_window = main_window
        #initialize i18n
        self.i18n = I18n(self.main_window)
        self.i18n.get_language()
        self.connection_mode = "phone"  # Default mode: qrcode or phone

        # Phone-field state (formatter + country selector)
        self._current_dial_code: str = "55"   # Brazil default
        self._phone_updating:    bool = False  # reentrancy guard for EVT_TEXT

        # Incremented on every new pairing attempt and every cancel/close, so
        # a _bg_pairing_flow() background thread from an attempt the user
        # already abandoned (Cancel, then "try again") can tell it's stale
        # and stop touching shared main_window.ws/token state or popping up
        # dialogs — see on_continue()'s docstring for why this exists.
        self._pairing_attempt_id: int = 0

    # ── Helpers ────────────────────────────────────────────────────────────

    def _wpp_headers(self, use_global_key=False):
        """Return headers for WPPConnect Server API requests."""
        apikey = (
            self.main_window.wpp_api_key
            if use_global_key
            else self.main_window.token
        )
        return {"Authorization": f"Bearer {apikey}", "Content-Type": "application/json"}

    def _create_instance(self, token):
        """
        Start/Create a WhatsApp session in the local WPPConnect Server.
        """
        url = (
            f"{self.main_window.wpp_server}"
            f":{self.main_window.wpp_port}/api/{token}/start-session"
        )
        payload = {
            "waitQrCode": False
        }
        headers = self._wpp_headers(use_global_key=True)

        try:
            response = api_post(url, json=payload, headers=headers, timeout=15)
            # 200, 201 are success. 400 might mean session already active which is fine.
            if response.status_code in (200, 201, 400):
                return token
            
            # Any other status is a real failure
            try:
                detail = response.json()
            except Exception:
                detail = response.text
            raise RuntimeError(f"HTTP {response.status_code}: {detail}")
        except Exception as exc:
            if "already" in str(exc).lower() or "active" in str(exc).lower():
                return token
            raise exc

    def _setup_websocket_for_instance(self, token):
        """
        No-op for WPPConnect Server as Socket.io events are active by default.
        """
        return True

    def _cleanup_orphan_sessions(self, keep_token: str = "") -> None:
        """Close ONLY this account's own, explicitly-abandoned WPPConnect
        sessions (plan Zad 3.2, GPT r2/r5). Never touches another account's
        session, never the current/active one, never one mid-pairing.

        This replaces the old "close every session on the server except one"
        behaviour, which was catastrophic under multi-account: pairing a second
        account would tear down the FIRST account's live session (its Chrome
        page / WhatsApp Web link), disconnecting a working account. The set of
        closable sessions now comes exclusively from THIS account's
        sessions.json via session_store.sessions_to_close() — a session whose
        ownership can't be proven from our own store is left strictly alone.
        """
        import session_store
        mw = self.main_window
        store = None
        try:
            store = mw._get_session_store()
        except Exception:
            store = None
        if store is None:
            # Legacy single-account mode: no per-account store, so there is no
            # provable ownership. Do NOT blast every session (that's the bug).
            return

        current = (mw.token or "").split(":")[0]
        try:
            closable = session_store.sessions_to_close(store.list(), current)
        except Exception:
            logging.exception("[cleanup_orphan_sessions] listing sessions failed")
            return

        for sess in closable:
            full_token = sess.get("token") or sess["name"]
            sess_name = sess["name"]

            def _clean_single(tok, name):
                try:
                    url = (
                        f"{mw.wpp_server}:{mw.wpp_port}/api/{tok}/close-session"
                    )
                    api_post(
                        url,
                        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
                        timeout=5,
                    )
                    logging.info("[cleanup_orphan_sessions] Closed own abandoned session: %s", name)
                    try:
                        store.remove(name)
                    except Exception:
                        pass
                except Exception:
                    pass

            threading.Thread(target=_clean_single, args=(full_token, sess_name), daemon=True).start()



    # ── Connection status ──────────────────────────────────────────────────

    def check_connection_status(self):
        """Return True only if there is a saved token AND the API confirms the session is connected.

        A token is written to settings as soon as the user clicks "Connect" — before
        pairing is actually completed.  If the app is closed mid-pairing or an error
        occurs, the stale token remains in settings.  On the next launch we must
        validate with the server that the session is genuinely connected; otherwise
        the connection dialog is never shown and the user is stuck with a broken state.
        """
        private_info = self.main_window.settings.get("privateinfo", {})
        token = self.main_window._get_wa_token()

        # Legacy fallback: token.tk file means old-format paired session.
        if not token:
            return os.path.exists(data_path("token.tk"))

        # Validate with the API that this token's session is actually connected.
        # We query the specific check-connection-session endpoint which tells us if the WhatsApp
        # account is genuinely authenticated/linked.
        try:
            _session_name = token.split(':')[0]
            _bearer = token.split(':')[1] if ':' in token else token
            check_url = (
                f"{self.main_window.wpp_server}"
                f":{self.main_window.wpp_port}/api/{_session_name}/check-connection-session"
            )
            headers = {"Authorization": f"Bearer {_bearer}", "Content-Type": "application/json"}
            check_resp = api_get(check_url, headers=headers, timeout=5)
            is_paired = private_info.get("paired", False)
            if check_resp.status_code in (401, 403):
                logging.warning("[check_connection_status] check-connection-session returned unauthorized (HTTP %s).", check_resp.status_code)
                if is_paired:
                    self.main_window.error_sound.play()
                    def _msg1():
                        wx.MessageBox(
                            self.i18n.t("device_logged_out"),
                            self.i18n.t("error").format(app_name=self.main_window.app_name),
                            wx.OK | wx.ICON_ERROR,
                        )
                    if wx.IsMainThread():
                        _msg1()
                    else:
                        wx.CallAfter(_msg1)
                self.main_window._set_wa_token("")
                self.main_window.settings.setdefault("privateinfo", {}).pop("paired", None)
                self.main_window.settings.setdefault("privateinfo", {}).pop("WA_phone_number", None)
                self.main_window.save_settings()
                self.main_window.clear_local_data()
                return False

            if check_resp.status_code in (200, 201):
                check_data = check_resp.json()
                # Only trust check-connection-session if the user actually completed pairing.
                # The WPPConnect browser can stay alive for a while after the app closes
                # mid-pairing, causing this endpoint to return status:true even though the
                # WhatsApp account was never linked. Requiring paired=True prevents that false positive.
                if check_data.get("status") is True:
                    if not is_paired:
                        logging.info(
                            "[check_connection_status] check-connection-session returned true and paired=False. "
                            "Marking session as paired locally."
                        )
                        self.main_window.settings.setdefault("privateinfo", {})["paired"] = True
                        self.main_window.save_settings()
                    return True

                # status:false — session offline or unauthenticated.
                # Do NOT immediately return True. Instead, fall through to check status-session
                # so we can confirm if it is temporarily offline (reconnecting) or permanently unlinked.
                if not is_paired:
                    logging.warning(
                        "[check_connection_status] check-connection-session returned false and paired=False. "
                        "Session is unlinked or incomplete. Clearing WA_token and wiping local data."
                    )
                    self.main_window._set_wa_token("")
                    self.main_window.settings.setdefault("privateinfo", {}).pop("paired", None)
                    self.main_window.save_settings()
                    # Clear all cached chats/contacts/media to avoid cross-account data leakage
                    self.main_window.clear_local_data()
                    # Best-effort delete of orphaned instance
                    if token:
                        _s = token.split(':')[0]
                        def _close(s=_s):
                            try:
                                api_post(
                                    f"{self.main_window.wpp_server}:{self.main_window.wpp_port}/api/{s}/close-session",
                                    headers=self._wpp_headers(use_global_key=True),
                                    timeout=5,
                                )
                                logging.info("[check_connection_status] Closed orphaned session: %s", s)
                            except Exception:
                                pass
                        threading.Thread(target=_close, daemon=True).start()
                    return False

            # Fallback/Safety Check: also check general status-session
            url = (
                f"{self.main_window.wpp_server}"
                f":{self.main_window.wpp_port}/api/{token}/status-session"
            )
            resp = api_get(url, headers=headers, timeout=5)
            if resp.status_code in (401, 403):
                logging.warning("[check_connection_status] status-session returned unauthorized (HTTP %s).", resp.status_code)
                if is_paired:
                    self.main_window.error_sound.play()
                    def _msg2():
                        wx.MessageBox(
                            self.i18n.t("device_logged_out"),
                            self.i18n.t("error").format(app_name=self.main_window.app_name),
                            wx.OK | wx.ICON_ERROR,
                        )
                    if wx.IsMainThread():
                        _msg2()
                    else:
                        wx.CallAfter(_msg2)
                self.main_window._set_wa_token("")
                self.main_window.settings.setdefault("privateinfo", {}).pop("paired", None)
                self.main_window.settings.setdefault("privateinfo", {}).pop("WA_phone_number", None)
                self.main_window.save_settings()
                self.main_window.clear_local_data()
                return False

            if resp.status_code in (200, 201):
                data = resp.json()
                status = (
                    data.get("status")
                    or data.get("state")
                    or data.get("response", {}).get("status")
                    or ""
                )
                if status == "CONNECTED":
                    if not is_paired:
                        logging.info(
                            "[check_connection_status] status-session returned CONNECTED and paired=False. "
                            "Marking session as paired locally."
                        )
                        self.main_window.settings.setdefault("privateinfo", {})["paired"] = True
                        self.main_window.save_settings()
                    return True
                _INCOMPLETE = {"INITIALIZING", "QRCODE", "PHONECODE", "notLogged", ""}
                if status not in _INCOMPLETE and is_paired:
                    # Session is connected or closed (but closed is allowed if still paired)
                    return True
                # A transient warm-up status right after launch (the WPPConnect
                # browser is still re-attaching the session) must NOT be treated
                # as a logout for an already-paired account: doing so wiped a
                # perfectly valid session on a cold start (the session comes up
                # as INITIALIZING/notLogged for ~30s). Only an explicit 401/403
                # (handled above) or a user disconnect may drop a paired token.
                # Keep it and let the normal connect/reconnect flow settle it.
                if is_paired:
                    logging.info(
                        "[check_connection_status] Token exists, session status is "
                        "'%s' (transient warm-up) and paired=True — keeping the "
                        "paired session; connect/reconnect flow will settle it.",
                        status,
                    )
                    return True
                # Stale token — pairing was never finished. Clear it so the
                # connection dialog is shown on this and future launches.
                logging.warning(
                    "[check_connection_status] Token exists but session status is '%s' "
                    "and paired=%s (pairing incomplete). Clearing stale WA_token and wiping local data.",
                    status,
                    is_paired,
                )
                self.main_window._set_wa_token("")
                self.main_window.settings.setdefault("privateinfo", {}).pop("paired", None)
                self.main_window.save_settings()
                if not is_paired:
                    # Only wipe local cached data when the session was never
                    # really paired (a genuine orphan). A paired session that
                    # reached this branch earlier was already kept above via the
                    # warm-up guard; this 'if is_paired' used to be dead code
                    # because paired sessions return True before reaching here.
                    self.main_window.clear_local_data()
                # Best-effort delete of orphaned instance
                if token:
                    _s = token.split(':')[0]
                    def _close(s=_s):
                        try:
                            api_post(
                                f"{self.main_window.wpp_server}:{self.main_window.wpp_port}/api/{s}/close-session",
                                headers=self._wpp_headers(use_global_key=True),
                                timeout=5,
                            )
                        except Exception:
                            pass
                    threading.Thread(target=_close, daemon=True).start()
                return False
        except Exception as exc:
            # Connection-level failures (timeout, refused, DNS) almost always
            # mean the WPPConnect server is still starting up — common on
            # Windows boot when the app autostarts alongside the server, where
            # Chrome headless + profile load can take 10-30s+. That is NOT
            # evidence of a logout: destroying the token here makes the very
            # next launch show the pairing dialog even though the session was
            # perfectly fine. Preserve the token and let the normal flow
            # connect via WebSocket once the server finishes starting.
            import requests as _requests
            if isinstance(exc, (_requests.exceptions.ConnectionError, _requests.exceptions.Timeout)):
                logging.warning(
                    "[check_connection_status] API not reachable yet (%s) — "
                    "assuming server still starting; preserving token.",
                    exc,
                )
                return True
            # Non-connection error (e.g. AttributeError, parsing failure): fall
            # back to the previous behaviour — only trust the token when the
            # user has previously completed pairing.
            logging.warning("[check_connection_status] Could not reach API to validate token: %s", exc)
            is_paired = self.main_window.settings.get("privateinfo", {}).get("paired", False)
            if is_paired:
                return True
            logging.warning(
                "[check_connection_status] API unreachable and paired=False — clearing stale token and showing connection dialog."
            )
            self.main_window._set_wa_token("")
            self.main_window.save_settings()
            return False

        return False

    # ── Connection dialog ──────────────────────────────────────────────────

    def show_connection_dial(self):
        if not wx.IsMainThread():
            logging.info("[show_connection_dial] Called from non-main thread. Dispatching to main thread via CallAfter...")
            evt = threading.Event()
            def _show():
                try:
                    self.show_connection_dial()
                finally:
                    evt.set()
            wx.CallAfter(_show)
            evt.wait()
            return

        # Wide enough to fit the instructions/QR-CODE side by side (like the
        # official WhatsApp Web/Desktop layout) — users coming from there are
        # used to finding the QR-CODE on the right, with instructions on the
        # left, rather than centered below the text.
        self.connection_dial = wx.Dialog(None, title=self.i18n.t("connect_phone").format(app_name=self.main_window.app_name), size=(640, 480))

        # QR-CODE Panel
        self.qrcode_panel = wx.Panel(self.connection_dial)
        self.qrcode_instructions = wx.StaticText(
            self.qrcode_panel, label=self.i18n.t("qrcode_instructions"), style=wx.ST_NO_AUTORESIZE,
        )
        self.qrcode_instructions.Wrap(260)
        self.qrcode_image = wx.StaticBitmap(self.qrcode_panel, size=(300, 300))
        self.switch_to_phone_btn = wx.Button(self.qrcode_panel, label=self.i18n.t("connect_with_phone"))
        self.switch_to_phone_btn.Bind(wx.EVT_BUTTON, self.on_switch_to_phone)

        # Left column: instructions text, then the phone-number fallback
        # button — mirrors the official layout's text column, but keeps
        # switch_to_phone_btn as our own addition (WhatsApp Web doesn't need
        # one; WinZapp does).
        qrcode_left_sizer = wx.BoxSizer(wx.VERTICAL)
        qrcode_left_sizer.Add(self.qrcode_instructions, 0, wx.ALL | wx.EXPAND, 10)
        qrcode_left_sizer.Add(self.switch_to_phone_btn, 0, wx.ALL, 10)

        # proportion=1 already makes the left column stretch to fill the
        # remaining horizontal space in this HORIZONTAL sizer — wx.EXPAND
        # would additionally stretch it vertically too, which conflicts with
        # (and is rejected by wx for) the wx.ALIGN_CENTER_VERTICAL below.
        qrcode_sizer = wx.BoxSizer(wx.HORIZONTAL)
        qrcode_sizer.Add(qrcode_left_sizer, 1, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 10)
        qrcode_sizer.Add(self.qrcode_image, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 10)
        self.qrcode_panel.SetSizer(qrcode_sizer)

        # Hide QR-CODE panel by default
        self.qrcode_panel.Hide()

        # Phone Number Panel
        self.phone_panel = wx.Panel(self.connection_dial)

        # ── Country selector ──────────────────────────────────────────────
        self.country_label_ctrl = wx.StaticText(
            self.phone_panel, label=self.i18n.t("country_label")
        )
        self._countries = get_countries(self.i18n.language)
        self.country_combo = wx.ComboBox(
            self.phone_panel,
            style=wx.CB_READONLY,
            choices=[c[0] for c in self._countries],
        )
        self.country_combo.SetSelection(0)   # Brazil
        self.country_combo.Bind(wx.EVT_COMBOBOX, self.on_country_changed)

        # ── Phone number field ────────────────────────────────────────────
        self.phone_number_label = wx.StaticText(
            self.phone_panel, label=self.i18n.t("enter_phone")
        )
        self.phone_field = wx.TextCtrl(
            self.phone_panel,
            value=f"+{self._current_dial_code} ",
            style=wx.TE_CENTER | wx.TE_PROCESS_ENTER | wx.TE_DONTWRAP,
        )
        self.phone_field.Bind(wx.EVT_CHAR,       self.on_phone_char)
        self.phone_field.Bind(wx.EVT_TEXT,       self.on_phone_text_changed)
        self.phone_field.Bind(wx.EVT_TEXT_ENTER, self.on_continue)
        self.phone_field.SetInsertionPointEnd()

        self.continue_btn = wx.Button(self.phone_panel, label=self.i18n.t("continue"))
        self.continue_btn.Bind(wx.EVT_BUTTON, self.on_continue)
        self.switch_to_qrcode_btn = wx.Button(
            self.phone_panel, label=self.i18n.t("connect_with_qrcode")
        )
        self.switch_to_qrcode_btn.Bind(wx.EVT_BUTTON, self.on_switch_to_qrcode)

        phone_sizer = wx.BoxSizer(wx.VERTICAL)
        phone_sizer.Add(self.country_label_ctrl,  0, wx.LEFT | wx.TOP,        10)
        phone_sizer.Add(self.country_combo,        0, wx.ALL | wx.EXPAND,     10)
        phone_sizer.Add(self.phone_number_label,   0, wx.LEFT | wx.TOP,       10)
        phone_sizer.Add(self.phone_field,          0, wx.ALL | wx.EXPAND,     10)
        phone_sizer.Add(self.continue_btn,         0, wx.ALL | wx.CENTER,     10)
        phone_sizer.Add(self.switch_to_qrcode_btn, 0, wx.ALL | wx.CENTER,     10)
        self.phone_panel.SetSizer(phone_sizer)

        # Quit button
        self.quit_btn = wx.Button(self.connection_dial, wx.ID_CANCEL, "&Sair")
        self.quit_btn.Bind(wx.EVT_BUTTON, self.on_quit_from_connect)

        # Bind close event
        self.connection_dial.Bind(wx.EVT_CLOSE, self.on_dialog_close)

        # Main sizer
        main_sizer = wx.BoxSizer(wx.VERTICAL)
        main_sizer.Add(self.qrcode_panel, 1, wx.ALL | wx.EXPAND, 5)
        main_sizer.Add(self.phone_panel, 1, wx.ALL | wx.EXPAND, 5)
        main_sizer.Add(self.quit_btn, 0, wx.ALL | wx.CENTER, 5)
        self.connection_dial.SetSizer(main_sizer)

        # No parent (main window isn't shown yet on first run) means wx
        # otherwise places this at the OS default position — usually the
        # screen's top-left corner — instead of where the user is looking,
        # which is part of why the QR-code was reported as hard to find/scan.
        self.connection_dial.CentreOnScreen()

        logging.info("[show_connection_dial] Entering connection_dial modal loop.")
        if hasattr(self.main_window, "play_startup_sound"):
            self.main_window.play_startup_sound()
        self.connection_dial.ShowModal()
        logging.info("[show_connection_dial] connection_dial modal loop returned.")
        try:
            self.connection_dial.Destroy()
        except Exception:
            pass

    def _close_active_session(self, sync=False):
        # Retrieve the active token from the dialog state
        token = getattr(self, 'raw_token', '')
        if not token:
            token = getattr(self.main_window, 'token', '')
        if not token:
            token = getattr(self, '_last_started_qr_token', '')
        logging.info("[_close_active_session] Active token retrieved: %s (sync=%s)", redact_token(token), sync)
        if token:
            session_name = token.split(':')[0]
            headers = self._wpp_headers(use_global_key=False)
            # Clear reference so we don't try to reuse/double-close this token
            self.raw_token = None
            self._last_started_qr_token = None
            mw_token = getattr(self.main_window, 'token', '')
            if mw_token.startswith(session_name):
                if hasattr(self.main_window, 'token'):
                    logging.info("[_close_active_session] Clearing self.main_window.token")
                    self.main_window.token = ""
            
            # Clear from settings as well to prevent stale reuse on switch
            if self.main_window._get_wa_token().startswith(session_name):
                logging.info("[_close_active_session] Clearing WA_token in settings")
                self.main_window._set_wa_token("")
            
            def _close_api_session():
                try:
                    close_url = (
                        f"{self.main_window.wpp_server}"
                        f":{self.main_window.wpp_port}/api/{token}/close-session"
                    )
                    logging.info("[_close_active_session] Sending close-session request to: %s",
                                 redact_api_url(close_url))
                    resp = api_post(close_url, headers=headers, timeout=5)
                    logging.info("[_close_active_session] close-session response status: %s", resp.status_code)
                except Exception as e:
                    # Never `e` raw: close-session runs while switching account
                    # or after Node is already down, so a timeout here is the
                    # normal case — and requests puts the whole URL, token
                    # included, into the message it would have logged.
                    logging.error("[_close_active_session] Error sending close-session request: %s",
                                  redact_api_error(e))

            if sync:
                _close_api_session()
            else:
                threading.Thread(target=_close_api_session, daemon=True).start()

    def on_switch_to_phone(self, event):
        # Close the active QR code session first
        self._close_active_session()

        # Set connection mode to phone
        self.connection_mode = "phone"

        # Disconnect WebSocket when switching to phone mode
        if hasattr(self.main_window, 'ws') and self.main_window.ws and self.main_window.ws.sio.connected:
            self.main_window.ws.sio.disconnect()

        self.qrcode_panel.Hide()
        self.phone_panel.Show()
        self.connection_dial.Layout()
        self.phone_field.SetFocus()
        self.phone_field.SetInsertionPointEnd()

    def on_switch_to_qrcode(self, event):
        # Close the active phone code session first
        self._close_active_session()

        # Set connection mode to qrcode
        self.connection_mode = "qrcode"

        self.phone_panel.Hide()
        self.qrcode_panel.Show()
        self.connection_dial.Layout()

        # Always start a fresh QR-CODE connection
        self.start_qrcode_connection()

        self.main_window.qrcode_loaded_sound.play()
        self.main_window.output(self.i18n.t("qrcode_instructions"))

    def start_qrcode_connection(self):
        """Initiates QR-CODE connection without user interaction."""
        self.qrcode_connection_started = True
        # Mark pairing as actively in flight for the WHOLE QR flow. Without this,
        # the ~30s health poll (check_wa_connection_http) saw the expected QRCODE
        # "scan me" state as a logout and wiped the session ~1s after the QR
        # appeared — pairing by QR could never complete on a pending account.
        # (Phone-code mode already sets this in on_continue.) Cleared by
        # on_pairing_complete / dialog close, same as the phone-code path.
        self.main_window._pairing_in_progress = True
        try:
            # Determine whether a token has been saved from a previous session.
            # We still always call _create_instance to (re)start the WPPConnect
            # session in case the API was restarted since the last connection.
            existing_token = self.main_window._get_wa_token()
            _instance_exists = bool(existing_token)

            server_base = f"{self.main_window.wpp_server}:{self.main_window.wpp_port}"
            api_key = self.main_window.wpp_api_key

            def _generate_hash(raw: str) -> str:
                """Call generate-token and return 'raw:hash'. Raises on failure."""
                url = f"{server_base}/api/{raw}/{api_key}/generate-token"
                res = api_post(url, timeout=10)
                if res.status_code in (200, 201):
                    hash_token = res.json().get("token") or ""
                    if hash_token:
                        return f"{raw}:{hash_token}"
                    raise RuntimeError(
                        f"generate-token returned empty hash (HTTP {res.status_code})"
                    )
                raise RuntimeError(
                    f"generate-token failed: HTTP {res.status_code} — {res.text[:200]}"
                )

            if _instance_exists:
                # Re-generate hash if the stored token has no colon (legacy or corrupt).
                # Without a hash the auth middleware returns 401 on every API call.
                if ":" not in existing_token:
                    try:
                        existing_token = _generate_hash(existing_token)
                    except Exception as exc:
                        logging.warning(
                            "[start_qrcode_connection] Could not refresh token hash: %s", exc
                        )
                self.main_window.token = existing_token
            else:
                # New pairing: reset sync flag so we wait for messages.set
                self.main_window.messages_set_completed = False
                self.main_window.clear_local_data()
                raw_token = self.generate_random_token()
                # Raise on failure so the outer except shows a meaningful message
                # instead of an opaque 401 from _create_instance.
                self.main_window.token = _generate_hash(raw_token)
                self.main_window._set_wa_token(self.main_window.token)

            # Close any previous session that may still be alive on the server
            # (different token from the one we're about to start). This prevents
            # orphaned Chrome processes when reopening the dialog or switching modes.
            _prev_token = getattr(self, '_last_started_qr_token', '')
            if _prev_token and _prev_token != self.main_window.token:
                _prev_session = _prev_token.split(':')[0]
                def _close_prev():
                    try:
                        api_post(
                            f"{server_base}/api/{_prev_session}/close-session",
                            headers=self._wpp_headers(use_global_key=True),
                            timeout=5
                        )
                        logging.info("[start_qrcode_connection] Closed previous QR session: %s", _prev_session)
                    except Exception:
                        pass
                threading.Thread(target=_close_prev, daemon=True).start()
            self._last_started_qr_token = self.main_window.token

            # Always (re)start the session so WPPConnect launches Chrome and
            # emits qrCode. WPPConnect tolerates a start-session when a session
            # already exists — it will simply resume it (or show a new QR if
            # the session was invalidated).
            self._create_instance(self.main_window.token)

            # Save settings
            self.main_window.save_settings()

            # Set websocket client and connect BEFORE querying status so we
            # don't miss the qrCode Socket.IO event that comes asynchronously.
            if hasattr(self.main_window, 'ws') and self.main_window.ws:
                try:
                    self.main_window.ws.sio.disconnect()
                except Exception:
                    pass
                self.main_window.ws = None
            self.main_window.ws = WebSocketClient(self.main_window, self, self.main_window.token)

            try:
                self.main_window.connect_websocket()
            except Exception:
                self.main_window.error_sound.play()
                wx.MessageBox(self.i18n.t("websocket_failed_reconnect"), self.i18n.t("connection_error"), wx.OK | wx.ICON_WARNING)
                self.show_connection_dial()
                return

            # Poll status-session to pick up a QR code that may already be ready.
            url = (
                f"{self.main_window.wpp_server}"
                f":{self.main_window.wpp_port}/api/{self.main_window.token}/status-session"
            )
            try:
                response = api_get(
                    url,
                    headers=self._wpp_headers(),
                    timeout=5,
                )
                response_data = response.json()
                # `urlcode` is deliberately NOT accepted here: it is the QR's
                # raw payload string ("2@abc…"), not an image. Feeding it to
                # display_qrcode_image() base64-decodes gibberish and paints
                # either nothing or noise — which looks exactly like the broken
                # QR this was meant to show.
                qrcode_base64 = (
                    response_data.get("qrcode")
                    or (response_data.get("response") or {}).get("qrcode")
                )
                if qrcode_base64:
                    wx.CallAfter(self.display_qrcode_image, qrcode_base64)
                else:
                    # Not an error: the session may simply not have produced a
                    # QR yet. The qrCode socket event delivers it when it does.
                    logging.info("[show_connection_dial] No QR in status-session yet — "
                                 "waiting for the qrCode event.")
            except Exception:
                logging.exception("[show_connection_dial] Failed to poll status-session for a QR.")

        except Exception:
            self.main_window.error_sound.play()
            wx.MessageBox(f"{self.i18n.t('connection_failed').format(app_name=self.main_window.app_name)} {format_exc()}", self.i18n.t("connection_error").format(app_name=self.main_window.app_name), wx.OK | wx.ICON_ERROR)

    # Side of the square the QR is drawn into (matches the StaticBitmap above).
    _QR_BOX = 300

    # White border added around the QR, as a fraction of its side.
    #
    # The QR standard requires a quiet zone of 4 modules on every side, and
    # WPPConnect's PNG ships with NONE: measured on a real code, the symbol runs
    # from x=-1 to x=228 of a 228px image. A decoder fed a clean file copes (jsQR
    # reads it fine), but a phone camera uses that border to separate the finder
    # patterns from whatever surrounds them — here, the dialog's own background.
    # Without it the camera never locks on, which is the "o QR não lê" nobody
    # could explain from the image alone, because the image really was valid.
    #
    # The ratio has to clear 4 modules for the *coarsest* code that could show
    # up, since a coarser code has bigger modules and therefore needs a wider
    # border. The worst realistic case is a low-version symbol: 29 modules means
    # 4/29 = 13.8% of the side. 15% covers it with room to spare, and still fits
    # the display box (228 + 2x34 = 296 of 300). WhatsApp's own codes measure
    # version 11 — 61 modules, 3.74px each — where 15% is about 9 modules.
    _QR_QUIET_ZONE_RATIO = 0.15

    @staticmethod
    def _qr_quiet_zone(side: int, ratio: float = _QR_QUIET_ZONE_RATIO) -> int:
        """Border in pixels to add on each side of a `side`-px QR."""
        if side <= 0:
            return 0
        return max(8, int(round(side * ratio)))

    @staticmethod
    def _qr_scale_factor(src: int, box: int) -> int:
        """Whole-number magnification that keeps a QR of `src` px inside `box`.

        Never returns 0: a QR larger than the box is left at its own size rather
        than shrunk, because shrinking merges neighbouring modules and destroys
        the code outright. At worst it overflows the panel and stays readable.
        """
        if src <= 0:
            return 1
        return max(1, box // src)

    def display_qrcode_image(self, base64_string):
        """Decodes and displays the base64 QR-CODE image.

        Scaling is nearest-neighbour and by a whole-number factor. Both matter:
        this used to call Scale(300, 300, wx.IMAGE_QUALITY_HIGH), which resamples
        with interpolation and stretched the source (264 px from WPPConnect) by
        a fractional 1.14×. That blurs the black/white module edges and makes
        their widths uneven — the phone camera then reads it as a damaged code
        and simply refuses it, which is the "QR Code inválido" users reported.
        A QR must be magnified in whole pixels with no smoothing, or not at all.

        Repeats are suppressed by comparing the payload, not by a timer. A 15 s
        cooldown used to sit here instead, and it could drop a genuine rotation:
        WhatsApp invalidates the previous QR when it issues a new one, so a
        dropped update leaves an expired code on screen that the phone will
        never accept. The timestamp also lived on `self`, which outlives the
        dialog — closing and reopening pairing inside the window rendered no QR
        at all, just an empty box.
        """
        if base64_string == getattr(self, "_last_qr_payload", None):
            logging.info("[display_qrcode_image] Identical QR payload re-emitted — not redrawing.")
            return

        try:
            self._last_qr_payload = base64_string
            # Remove data URI prefix if present
            if "," in base64_string:
                base64_string = base64_string.split(",")[1]

            image_data = base64.b64decode(base64_string)
            image = wx.Image(BytesIO(image_data))
            if not image.IsOk():
                logging.warning("[display_qrcode_image] Decoded data is not a valid image.")
                return

            src_w, src_h = image.GetWidth(), image.GetHeight()
            # Quiet zone first, magnification second: padding then scaling keeps
            # the border proportional and every edge on a whole pixel.
            pad = self._qr_quiet_zone(min(src_w, src_h))
            if pad:
                image = image.Size(wx.Size(src_w + 2 * pad, src_h + 2 * pad),
                                   wx.Point(pad, pad), 255, 255, 255)
            padded = min(image.GetWidth(), image.GetHeight())
            factor = self._qr_scale_factor(padded, self._QR_BOX)
            if factor > 1:
                image = image.Scale(image.GetWidth() * factor, image.GetHeight() * factor,
                                    wx.IMAGE_QUALITY_NEAREST)
            logging.info(
                "[display_qrcode_image] QR %dx%d + quiet zone %dpx -> %dx%d "
                "(factor %d), alpha=%s, %d bytes in.",
                src_w, src_h, pad, image.GetWidth(), image.GetHeight(), factor,
                image.HasAlpha(), len(image_data),
            )

            self.qrcode_image.SetBitmap(wx.Bitmap(image))
            # The bitmap is no longer forced to 300x300, so the panel has to
            # re-measure or the image is clipped to the old placeholder size.
            self.qrcode_panel.Layout()

            self.main_window.pairing_code_updated_sound.play()

        except Exception:
            # Never silently: a QR that fails to render leaves the user staring
            # at an empty box with no idea why.
            logging.exception("[display_qrcode_image] Failed to render the QR code.")

    def reconnect_websocket(self):
        """Reconnects WebSocket for QR-CODE mode (instance already created)."""
        try:
            self.main_window.connect_websocket()
        except Exception:
            self.main_window.error_sound.play()
            wx.MessageBox(f"{self.i18n.t('websocket_init_failed')} {format_exc()}", self.i18n.t("connection_error"), wx.OK | wx.ICON_ERROR)

    @staticmethod
    def _can_reuse_existing_session(privateinfo: dict, phone_number: str,
                                    existing_token: str) -> bool:
        """True when a stored WPPConnect token is worth reusing for this number.

        The token is passed in rather than read off `privateinfo` here: it lives
        Fernet-protected under WA_token_protected and only
        MainWindow._get_wa_token() can read it (see core/token_vault.py).
        Reading privateinfo["WA_token"] directly would find an empty string on
        every already-migrated install and silently reject every reusable
        session.

        Requires `paired`, not merely a stored token. WA_token is persisted
        optimistically by _bg_pairing_flow() the moment a phoneCode arrives —
        long before the pairing completes — so a pairing that was cancelled, or
        one whose app was killed before it finished, leaves a token behind whose
        WPPConnect session no longer exists. Reusing it means calling
        /start-session on a token WPPConnect has already dropped from
        clientsArray: no pairing code is ever emitted and the dialog sits on
        "Conectando..." until the 90 s wait expires. Reported live as "cancelei
        o pareamento, cliquei em continuar e o código não aparece mais".

        `paired` is only ever set once WhatsApp is genuinely linked (the
        connection update in websocket_client.py, check_wa_connection_http()'s
        host-device fetch, and check_connection_status()), never by merely
        showing a code — which is what makes it the honest test here.
        """
        if not isinstance(privateinfo, dict):
            return False
        stored_raw = "".join(
            c for c in (privateinfo.get("WA_phone_number") or "") if c.isdigit()
        )
        phone_digits = "".join(c for c in (phone_number or "") if c.isdigit())
        if not phone_digits or stored_raw != phone_digits:
            return False
        return bool(existing_token) and bool(privateinfo.get("paired", False))

    def on_continue(self, event):
        """Phone-number pairing flow (asynchronous to prevent GUI freeze).

        _bg_pairing_flow() below waits up to 90s for a phoneCode. If the user
        cancels (or closes the dialog) while that wait is still in progress
        and starts a new attempt, the old thread kept running to completion
        with no way to know it had been abandoned — it would still go on to
        touch main_window.ws/token and could pop up a pairing_dial *after*
        the user had already moved on, racing the new attempt's own
        WebSocketClient/session and effectively running two pairing flows
        (and two WPPConnect sessions, each independently rotating its own
        code) at once. my_attempt/self._pairing_attempt_id lets the thread
        recognize it's been superseded and bail out silently instead.
        """
        self.phone_number = "".join(
            c for c in self.phone_field.GetValue() if c.isdigit()
        )
        if not self.phone_number:
            return

        self._pairing_attempt_id += 1
        my_attempt = self._pairing_attempt_id
        # Narrow "a pairing is actively in flight" window used by
        # WebSocketClient.on_connection_update to tell a failed pairing
        # (connection opens then closes again before real data ever arrives)
        # apart from an ordinary reconnect hiccup on an already-paired
        # account — see main.py's _pairing_in_progress for the full story.
        self.main_window._pairing_in_progress = True

        # Disable continue button and show connecting status to user
        self.continue_btn.Disable()
        self.continue_btn.SetLabel(self.i18n.t("connecting") or "Conectando...")
        self.main_window.output(self.i18n.t("connecting") or "Conectando...")

        # Monkey-patch wx.GetApp to ensure background threads can access the app instance
        # even before the MainLoop is entered (which is blocked by ShowModal).
        app = wx.GetApp()
        if app:
            wx.GetApp = lambda: app

        def _bg_pairing_flow():
            _attempt_token = ""
            try:
                # Capture the old token to close it, preventing conflict
                _old_token = self.main_window.token or ""
                # Reuse the stored session only when it belongs to this same
                # number AND the pairing actually completed — see
                # _can_reuse_existing_session() for why the token alone is not
                # enough. It normalises the stored number to digits itself.
                _privateinfo = self.main_window.settings.get("privateinfo", {})
                existing_token = self.main_window._get_wa_token()
                _instance_exists = self._can_reuse_existing_session(
                    _privateinfo, self.phone_number, existing_token
                )
                if not _instance_exists:
                    # New pairing: reset sync flag so we wait for messages.set
                    self.main_window.messages_set_completed = False
                    self.main_window.clear_local_data()

                if _instance_exists:
                    self.main_window.token = existing_token
                else:
                    # Kill any leftover Chromium sessions from previous failed attempts
                    # so only ONE browser runs at a time (prevents Auto Close race).
                    self._cleanup_orphan_sessions(keep_token="")
                    raw_token = self.generate_random_token()
                    url = f"{self.main_window.wpp_server}:{self.main_window.wpp_port}/api/{raw_token}/{self.main_window.wpp_api_key}/generate-token"
                    try:
                        res = api_post(url, timeout=10)
                        if res.status_code in (200, 201):
                            hash_token = res.json().get("token")
                            self.main_window.token = f"{raw_token}:{hash_token}"
                        else:
                            self.main_window.token = raw_token
                    except Exception:
                        self.main_window.token = raw_token

                _attempt_token = self.main_window.token or ""

                # Terminate any existing session running on the server. If a session is already
                # active/initializing in QR code mode (e.g. from the startup check), WPPConnect
                # will ignore new start-session requests, and the pairing code will never generate.
                # We fire close-session and immediately set up the WebSocket in parallel to avoid
                # the 2s blocking wait — the Node side handles the close asynchronously.
                _current_token = self.main_window.token or ""
                # Close the actual old session instead of the new session
                _session_name = _old_token.split(':')[0] if _old_token else ""
                _close_headers = self._wpp_headers(use_global_key=True)
                close_done = threading.Event()

                def _close_and_signal():
                    if not _session_name:
                        close_done.set()
                        return
                    try:
                        close_url = (
                            f"{self.main_window.wpp_server}"
                            f":{self.main_window.wpp_port}/api/{_session_name}/close-session"
                        )
                        api_post(close_url, headers=_close_headers, timeout=10)
                        logging.info("[_bg_pairing_flow] Closed existing session to prepare for pairing code: %s", _session_name)
                        if _old_token:
                            self.main_window._wait_for_session_flushed(_old_token)
                        self.main_window.wait_for_profile_release(_session_name, timeout=15.0)
                    except Exception as e:
                        logging.warning("[_bg_pairing_flow] Failed to close existing session: %s", e)
                    finally:
                        close_done.set()

                threading.Thread(target=_close_and_signal, daemon=True).start()

                close_done.wait(timeout=45)

                if my_attempt != self._pairing_attempt_id:
                    # Superseded — the user cancelled and/or started a newer
                    # attempt while this one was waiting. Stop here instead
                    # of creating a WebSocketClient/session for an attempt
                    # nobody is looking at anymore.
                    logging.info("[_bg_pairing_flow] Attempt %d superseded before start-session — aborting.", my_attempt)
                    return

                # Set up the websocket client (but do not connect yet)
                if hasattr(self.main_window, 'ws') and self.main_window.ws:
                    try:
                        self.main_window.ws.sio.disconnect()
                    except Exception:
                        pass
                    self.main_window.ws = None
                self.main_window.ws = WebSocketClient(self.main_window, self, self.main_window.token)
                if self.main_window.ws:
                    self.main_window.ws._phone_code_event.clear()
                    self.main_window.ws._phone_code_value = ""

                # Call /start-session in a background thread. This immediately registers the namespace on Node side.
                url = (
                    f"{self.main_window.wpp_server}"
                    f":{self.main_window.wpp_port}/api/{self.main_window.token}/start-session"
                )
                payload = {"phone": self.phone_number, "waitQrCode": False}
                ws_ref = self.main_window.ws  # capture before thread starts
                headers = self._wpp_headers(use_global_key=True)

                def _call_start_session():
                    try:
                        resp = api_post(url, json=payload, headers=headers, timeout=120)
                        # Diagnostics only — deliberately does not change the
                        # control flow below (the fallback event.set() still
                        # covers a non-2xx response exactly as before): this
                        # whole pairing flow is fragile enough (see connect.py's
                        # module-level notes on the nested-modal-dialog timing)
                        # that a silent failure here used to be genuinely hard
                        # to diagnose from a user's log.log alone.
                        if resp.status_code not in (200, 201):
                            logging.warning(
                                "[_call_start_session] start-session returned HTTP %s: %s",
                                resp.status_code, resp.text[:300],
                            )
                        # If the code came back inline (rare), unblock the wait loop.
                        inline_code = resp.json().get("phoneCode", "")
                        if inline_code and not ws_ref._phone_code_event.is_set():
                            ws_ref._phone_code_value = str(inline_code)
                            ws_ref._phone_code_event.set()
                    except Exception as exc:
                        logging.warning("[_call_start_session] start-session request failed: %s", exc)
                        # Signal the event so the main thread doesn't wait forever.
                        ws_ref._phone_code_event.set()

                threading.Thread(target=_call_start_session, daemon=True).start()

                # Connect the WebSocket
                try:
                    self.main_window.connect_websocket()
                except Exception:
                    pass

                # Wait up to 90 s for WPPConnect to emit the phoneCode via Socket.IO.
                got_code = self.main_window.ws._phone_code_event.wait(timeout=90)
                pairing_code = self.main_window.ws._phone_code_value if got_code else ""

                if my_attempt != self._pairing_attempt_id:
                    # Superseded while waiting for the code — don't persist
                    # this attempt's token/settings or pop up pairing_dial
                    # behind whatever the user is now looking at.
                    logging.info("[_bg_pairing_flow] Attempt %d superseded after phoneCode wait — discarding result.", my_attempt)
                    self.main_window._register_abandoned_session(_attempt_token)
                    return

                if pairing_code:
                    # Only now persist the token — pairing has actually started.
                    if "privateinfo" not in self.main_window.settings:
                        self.main_window.settings["privateinfo"] = {}
                    self.main_window.settings["privateinfo"]["WA_phone_number"] = self.phone_number
                    self.main_window._set_wa_token(self.main_window.token)
                    wx.CallAfter(self._on_pairing_code_success, pairing_code)
                else:
                    # No code received — clear any partially-saved token so next
                    # launch shows the connection dialog instead of acting connected.
                    self.main_window._register_abandoned_session(_attempt_token)
                    self.main_window._set_wa_token("")
                    self.main_window.save_settings()
                    reason = getattr(self.main_window.ws, "_phone_code_error", "")
                    wx.CallAfter(self._on_pairing_code_error, reason)

            except Exception as exc:
                # On any unexpected error, clear the token so next launch works correctly.
                self.main_window._register_abandoned_session(_attempt_token)
                self.main_window._set_wa_token("")
                self.main_window.save_settings()
                wx.CallAfter(self._on_pairing_code_exception, str(exc))

        threading.Thread(target=_bg_pairing_flow, daemon=True).start()

    def _on_pairing_code_success(self, pairing_code):
        if not self or not isinstance(self, wx.Window) or RuntimeError:
            # Check if wx object is deleted
            try:
                if not self.continue_btn:
                    return
            except RuntimeError:
                return
        self.continue_btn.Enable()
        self.continue_btn.SetLabel(self.i18n.t("continue"))
        self.show_pairing_dial(pairing_code)

    def _on_pairing_code_error(self, reason: str = ""):
        try:
            if not self or not self.continue_btn:
                return
        except RuntimeError:
            return
        self.continue_btn.Enable()
        self.continue_btn.SetLabel(self.i18n.t("continue"))
        if reason:
            message = self.i18n.t("no_pairing_code_received_reason").format(
                app_name=self.main_window.app_name, reason=reason,
            )
        else:
            message = self.i18n.t("no_pairing_code_received").format(
                app_name=self.main_window.app_name,
            )
        wx.MessageBox(
            message,
            self.i18n.t("connection_error"),
            wx.OK | wx.ICON_ERROR,
        )

    def _on_pairing_code_exception(self, err_msg):
        try:
            if not self or not self.continue_btn:
                return
        except RuntimeError:
            return
        self.continue_btn.Enable()
        self.continue_btn.SetLabel(self.i18n.t("continue"))
        self.main_window.error_sound.play()
        wx.MessageBox(
            f"{self.i18n.t('connection_failed').format(app_name=self.main_window.app_name)} {err_msg}",
            self.i18n.t('connection_error').format(app_name=self.main_window.app_name),
            wx.OK | wx.ICON_ERROR,
        )


    # ── Phone formatter ────────────────────────────────────────────────────

    def on_country_changed(self, event):
        """Update the dial code and reformat the phone field."""
        idx = self.country_combo.GetSelection()
        if idx == wx.NOT_FOUND:
            return
        _, new_code = self._countries[idx]

        # Preserve the local digits already typed (strip old country code prefix)
        text       = self.phone_field.GetValue()
        all_digits = "".join(c for c in text if c.isdigit())
        old_cc     = self._current_dial_code
        local_digits = (
            all_digits[len(old_cc):]
            if all_digits.startswith(old_cc)
            else all_digits
        )

        self._current_dial_code = new_code

        self._phone_updating = True
        try:
            self.phone_field.ChangeValue(
                self._format_phone_display(new_code + local_digits)
            )
            self.phone_field.SetInsertionPointEnd()
        finally:
            self._phone_updating = False

    def on_phone_char(self, event):
        """
        Filter individual keystrokes in the phone field.

        Digits (0-9 and numpad), navigation keys and Ctrl+key combinations
        pass through.  Everything else (letters, punctuation, @, _, …) is
        consumed and the screen reader announces "Caractere inválido".
        """
        key = event.GetKeyCode()

        # Navigation / editing keys always pass through
        _NAV = {
            wx.WXK_BACK, wx.WXK_DELETE,
            wx.WXK_LEFT, wx.WXK_RIGHT, wx.WXK_HOME, wx.WXK_END,
            wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER,
            wx.WXK_TAB, wx.WXK_ESCAPE,
        }
        if key in _NAV:
            event.Skip()
            return

        # Any Ctrl+key or Alt+key combo (clipboard shortcuts, select-all,
        # Alt+F4, system accelerators, …) always pass through
        if event.ControlDown() or event.AltDown() or event.CmdDown():
            event.Skip()
            return

        # Bare modifier keys and function keys are not typed characters
        if key in (wx.WXK_ALT, wx.WXK_CONTROL, wx.WXK_SHIFT, wx.WXK_WINDOWS_LEFT,
                   wx.WXK_WINDOWS_RIGHT, wx.WXK_WINDOWS_MENU) or wx.WXK_F1 <= key <= wx.WXK_F24:
            event.Skip()
            return

        # Main keyboard digits
        if ord("0") <= key <= ord("9"):
            event.Skip()
            return

        # Numpad digits
        if wx.WXK_NUMPAD0 <= key <= wx.WXK_NUMPAD9:
            event.Skip()
            return

        # Anything else → reject and announce
        self.main_window.speak_output.output(
            self.main_window.i18n.t("invalid_char")
        )
        # Do NOT call event.Skip() — the character is swallowed

    def on_phone_text_changed(self, event):
        """
        Reformat the phone field after every text change (including paste).

        If the new text contains characters that are not digits and not our
        formatting symbols (+, -, space), the screen reader announces
        "Caractere inválido" and those characters are silently stripped.
        """
        if self._phone_updating:
            return
        self._phone_updating = True
        try:
            text = self.phone_field.GetValue()

            # Detect truly invalid chars coming from paste
            _fmt = set("+- ")
            if any(c not in _fmt and not c.isdigit() for c in text):
                self.main_window.speak_output.output(
                    self.main_window.i18n.t("invalid_char")
                )

            digits    = "".join(c for c in text if c.isdigit())
            formatted = self._format_phone_display(digits)
            if formatted != text:
                self.phone_field.ChangeValue(formatted)
                self.phone_field.SetInsertionPointEnd()
        finally:
            self._phone_updating = False

    def _format_phone_display(self, digits: str) -> str:
        """Convert a raw digit string (including country code) to display format.

        Brazil (CC=55): +55 DD XXXXX-XXXX or +55 DD XXXX-XXXX
        All other countries: +CC local  (no area-code split, no hyphen)
        """
        cc    = self._current_dial_code
        local = digits[len(cc):] if digits.startswith(cc) else digits

        result = f"+{cc}"
        if not local:
            return result

        if cc == "55":
            # Brazil: 2-digit DDD + body with hyphen
            area = local[:2]
            rest = local[2:]
            result += f" {area}"
            if not rest:
                return result
            if len(rest) < 7:
                result += f" {rest}"
            elif len(rest) == 9:
                result += f" {rest[:5]}-{rest[5:]}"
            else:
                split = len(rest) - 4
                result += f" {rest[:split]}-{rest[split:]}"
        else:
            # Generic international: just append local digits with a space
            result += f" {local}"

        return result

    def generate_random_token(self):
        return os.urandom(16).hex()

    def show_pairing_dial(self, pairing_code):
        # WPPConnect may have already rotated the code between the moment the
        # background thread captured it and this CallAfter running — always
        # open the dialog with the most recent code received.
        ws = getattr(self.main_window, "ws", None)
        latest_code = str(getattr(ws, "_phone_code_value", "") or "")
        pairing_code = latest_code or pairing_code

        self.pairing_dial = wx.Dialog(self.connection_dial, title=self.i18n.t("pairing_dial_intro"), size=(300, 150))
        self.pairing_instructions = wx.StaticText(self.pairing_dial, label=self.i18n.t("pairing_instructions"))
        self.pairing_code_label = wx.StaticText(self.pairing_dial, label=self.i18n.t("pairing_code_label"))
        self.pairing_code_field = wx.TextCtrl(self.pairing_dial, style=wx.TE_CENTER | wx.TE_READONLY | wx.TE_DONTWRAP, value=pairing_code)
        self.cancel_btn = wx.Button(self.pairing_dial, label=self.i18n.t("cancel_pairing"))
        self.cancel_btn.Bind(wx.EVT_BUTTON, self.on_cancel_pairing)

        self.pairing_dial.CentreOnParent()

        self.main_window.waiting_pairing_sound.play()
        logging.info("[show_pairing_dial] Entering pairing_dial modal loop.")
        result = self.pairing_dial.ShowModal()
        logging.info("[show_pairing_dial] pairing_dial modal loop returned (result=%s).", result)
        try:
            self.pairing_dial.Destroy()
        except Exception:
            pass
        # ROOT CAUSE of "connected sound plays, then nothing — no window, no
        # tray icon, no error, forever" (confirmed live via log.log):
        # ShowModal()'s return value used to be discarded, so this ran
        # cleanup_pairing_session() — which disconnects the socket AND calls
        # /close-session, i.e. tells WPPConnect to close the WhatsApp Web
        # session it had JUST finished linking — unconditionally, even when
        # the dialog closed because pairing *succeeded*
        # (WebSocketClient.on_pairing_complete() ends this modal with
        # wx.ID_OK). Only clean up when it closed for any other reason
        # (Cancel, window close) — a session that just succeeded must be
        # left alone.
        if result != wx.ID_OK:
            self.cleanup_pairing_session()
            return

        # Pairing succeeded. Close the parent connection_dial from HERE, not
        # from WebSocketClient.on_pairing_complete().
        #
        # pairing_dial runs as a modal nested inside connection_dial's own
        # ShowModal() loop. EndModal() does not unwind its loop immediately —
        # it only signals it — and that still-running loop goes on dispatching
        # pending events, including any wx.CallAfter queued from within it. So
        # on_pairing_complete() could not close connection_dial itself: doing
        # it inline, or from a CallAfter chained off the same handler, both
        # ran while pairing_dial's loop was still the running one, and wx
        # rejected EndModal() on the (suspended) parent loop with a hard
        # assertion — "IsRunning() failed ... Use ScheduleExit() on not
        # running loop", confirmed in log.log with the parent's close logged
        # BEFORE "pairing_dial modal loop returned". connection_dial then
        # stayed open forever, so MainWindow.__init__ never got past
        # show_connection_dial() — no main window, no tray icon, no sync.
        #
        # Reaching this line proves pairing_dial's ShowModal() has genuinely
        # returned and control is back inside connection_dial's own loop,
        # which is therefore the running one EndModal() is allowed to target.
        try:
            if self.connection_dial.IsModal():
                logging.info("[show_pairing_dial] Ending connection_dial modal loop after successful pairing.")
                self.connection_dial.EndModal(wx.ID_OK)
            else:
                logging.info("[show_pairing_dial] connection_dial not modal — nothing to end.")
        except Exception:
            logging.exception("[show_pairing_dial] Failed to end connection_dial.")

    def update_pairing_code(self, code):
        """Refresh the pairing dialog when WPPConnect emits a new phoneCode.

        WhatsApp rotates the pairing code periodically while the session is
        unauthenticated, invalidating the previous one. Without this refresh
        the dialog keeps showing the first (stale) code, the user types it,
        pairing fails and they retry — multiplying code requests until
        WhatsApp's anti-abuse blocks the account.
        """
        code = str(code or "")
        if not code:
            return
        dial = getattr(self, "pairing_dial", None)
        # A destroyed wx.Dialog evaluates to False, covering cancel/close.
        if not dial:
            return
        # Deliberately NOT a time-based cooldown. A 15 s one used to sit here,
        # meant to stop flicker from rapid rotations — but dropping an update
        # because it arrived too soon leaves the dialog showing the previous,
        # now-invalid code, which is precisely the failure this method's
        # docstring exists to describe: the user types a stale code, pairing
        # fails, they request another, and WhatsApp's anti-abuse eventually
        # blocks the account.
        #
        # The equality check below already suppresses every *duplicate* emit
        # (the only thing that can actually flicker) with none of that risk,
        # and it does so without any state that outlives the dialog — a
        # timestamp on `self` survives closing and reopening the dialog, so a
        # retry within the cooldown got no code at all.
        try:
            if not dial.IsShown() or self.pairing_code_field.GetValue() == code:
                return
            self.pairing_code_field.ChangeValue(code)
        except RuntimeError:
            return  # dialog destroyed between the checks
        self.main_window.pairing_code_updated_sound.play()
        self.main_window.output(
            f"{self.i18n.t('qrcode_updated')} {self.i18n.t('pairing_code_label')} {code}"
        )

    def on_cancel_pairing(self, event):
        # Invalidate any in-flight _bg_pairing_flow() — see on_continue()'s
        # docstring. This dialog only appears after a phoneCode was already
        # received, so the thread's own check has already passed by the time
        # this button exists, but bumping it here still stops it from acting
        # on a stale attempt if the user cancels and retries fast enough to
        # overlap with the tail of the current one (e.g. persisting settings).
        self._pairing_attempt_id += 1
        self.main_window._pairing_in_progress = False
        try:
            if hasattr(self, "pairing_dial") and self.pairing_dial:
                self.pairing_dial.EndModal(wx.ID_CANCEL)
        except RuntimeError:
            pass

    def cleanup_pairing_session(self):
        logging.info("[cleanup_pairing_session] Cleanup pairing session triggered.")
        # Drop the optimistically-persisted token BEFORE anything else.
        # _bg_pairing_flow() writes WA_token to settings as soon as a phoneCode
        # arrives — long before the pairing actually completes — and the only
        # caller of this method runs when the pairing dialog closed with
        # anything other than wx.ID_OK, i.e. the pairing definitively did not
        # complete and the close-session below is about to destroy that
        # session for good. Leaving the token behind made the very next
        # "Continuar" take _bg_pairing_flow()'s "instance already exists"
        # branch and call /start-session on this dead token instead of minting
        # a fresh one: WPPConnect had already removed it from clientsArray, so
        # no code was ever emitted and the dialog sat on "Conectando..." for
        # the full 90 s wait. Reported live as "cancelei o pareamento, cliquei
        # em continuar e o código não aparece mais".
        if self.main_window._get_wa_token():
            logging.info("[cleanup_pairing_session] Clearing WA_token of the cancelled attempt.")
            # _set_wa_token() clears both the protected and legacy fields and
            # saves; `paired` is popped separately, then saved with it.
            self.main_window.settings.setdefault("privateinfo", {}).pop("paired", None)
            self.main_window._set_wa_token("")

        # Disconnect WebSocket
        if hasattr(self.main_window, 'ws') and self.main_window.ws:
            try:
                logging.info("[cleanup_pairing_session] Disconnecting WebSocket...")
                self.main_window.ws.sio.disconnect()
            except Exception as e:
                logging.error("[cleanup_pairing_session] Error disconnecting WebSocket: %s", e)
            self.main_window.ws = None

        # Call close-session API endpoint to terminate the headless browser and clear state
        token = getattr(self.main_window, 'token', '')
        logging.info("[cleanup_pairing_session] Retrieved token: %s", redact_token(token))
        if token:
            headers = self._wpp_headers(use_global_key=False)
            def _close_api_session():
                try:
                    close_url = (
                        f"{self.main_window.wpp_server}"
                        f":{self.main_window.wpp_port}/api/{token}/close-session"
                    )
                    logging.info("[cleanup_pairing_session] Sending close-session request to: %s",
                                 redact_api_url(close_url))
                    resp = api_post(close_url, headers=headers, timeout=5)
                    logging.info("[cleanup_pairing_session] close-session response status: %s", resp.status_code)
                except Exception as e:
                    # Same leak as in _close_active_session(): the cancelled
                    # pairing attempt is exactly when this call times out, and
                    # the exception message carries the token-bearing URL.
                    logging.error("[cleanup_pairing_session] Error sending close-session request: %s",
                                  redact_api_error(e))
            threading.Thread(target=_close_api_session, daemon=True).start()

    # Bounded grace given to a JUST-STARTED pairing attempt before honoring
    # a Quit/close that lands while Chrome is still opening WhatsApp Web for
    # the first time. Without this, closing a few seconds after clicking
    # "Conectar com QR code" (or entering a phone number) killed Chrome
    # mid-launch, before it produced anything to scan/type. Chrome's first
    # real page load routinely takes 10-25s; 30s covers that without
    # stalling a user who wants out anywhere near the pairing flow's own
    # ~90s wait.
    _PAIRING_STARTUP_GRACE_SECONDS = 30.0
    _PAIRING_STARTUP_POLL_SECONDS = 0.5

    def _wait_for_pairing_startup_settled(self):
        """Block (briefly, boundedly) so Chrome finishes its FIRST attempt at
        producing a QR/pairing code before we close on the user's way out.

        No-op unless a pairing attempt is actually in flight AND has not yet
        produced anything — an attempt that never started, or one that
        already has its QR/code on screen, returns immediately. Must be
        called BEFORE the caller disconnects the WebSocket or clears
        _pairing_in_progress, since both are read here.
        """
        if not getattr(self.main_window, "_pairing_in_progress", False):
            return
        mode = getattr(self, "connection_mode", None)
        deadline = time.monotonic() + self._PAIRING_STARTUP_GRACE_SECONDS
        if mode == "phone":
            ws = getattr(self.main_window, "ws", None)
            event = getattr(ws, "_phone_code_event", None) if ws else None
            if event is None or event.is_set():
                return
            logging.info("[_wait_for_pairing_startup_settled] phone pairing in flight, "
                         "waiting up to %.0fs for a code before closing",
                         self._PAIRING_STARTUP_GRACE_SECONDS)
            # Blocks the wx main thread with no repaint/accessibility events
            # pumped — silent dead air looks like a hang and invites a
            # force-kill mid-wait, the outcome this method exists to
            # prevent. Reuses the existing "connecting" string.
            self.main_window.output(self.i18n.t("connecting") or "Conectando...")
            event.wait(timeout=max(0.0, deadline - time.monotonic()))
            return
        if mode == "qrcode":
            token = getattr(self.main_window, "token", "") or getattr(self, "_last_started_qr_token", "")
            if not token:
                return
            logging.info("[_wait_for_pairing_startup_settled] QR pairing in flight, "
                         "waiting up to %.0fs for a QR before closing",
                         self._PAIRING_STARTUP_GRACE_SECONDS)
            self.main_window.output(self.i18n.t("connecting") or "Conectando...")
            url = f"{self.main_window.wpp_server}:{self.main_window.wpp_port}/api/{token}/status-session"
            headers = self._wpp_headers()
            while time.monotonic() < deadline:
                try:
                    resp = api_get(url, headers=headers, timeout=5)
                    if resp.status_code in (200, 201):
                        data = resp.json()
                        payload = data.get("response") if isinstance(data.get("response"), dict) else data
                        qr = data.get("qrcode") or (payload or {}).get("qrcode")
                        status = data.get("status") or (payload or {}).get("status")
                        if qr or status in ("CONNECTED", "qrReadSuccess", "inChat"):
                            return
                except Exception:
                    pass
                time.sleep(self._PAIRING_STARTUP_POLL_SECONDS)

    def on_dialog_close(self, event):
        logging.info("[on_dialog_close] Dialog close event triggered.")
        self._wait_for_pairing_startup_settled()
        # Invalidate any in-flight _bg_pairing_flow() — see on_continue().
        self._pairing_attempt_id += 1
        self.main_window._pairing_in_progress = False
        # Disconnect WebSocket if connected
        if hasattr(self.main_window, 'ws') and self.main_window.ws:
            try:
                logging.info("[on_dialog_close] Disconnecting WebSocket...")
                self.main_window.ws.sio.disconnect()
            except Exception as e:
                logging.error("[on_dialog_close] Error disconnecting WebSocket: %s", e)
            self.main_window.ws = None

        self._close_active_session()
        event.Skip()

    def on_quit_from_connect(self, event):
        logging.info("[on_quit_from_connect] Quit requested from connection dialog.")
        self._wait_for_pairing_startup_settled()
        self._pairing_attempt_id += 1
        self.main_window._pairing_in_progress = False
        if hasattr(self.main_window, 'ws') and self.main_window.ws:
            try:
                logging.info("[on_quit_from_connect] Disconnecting WebSocket...")
                self.main_window.ws.sio.disconnect()
            except Exception as e:
                logging.error("[on_quit_from_connect] Error disconnecting WebSocket: %s", e)
            self.main_window.ws = None

        self._close_active_session(sync=True)
        sys.exit()
