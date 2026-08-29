import logging
import threading
import time
import socketio
import wx
import requests
from core.api_client import api_get, api_post
from core.i18n import I18n
from core.sync_contracts import observe_payload
from core.utils import looks_like_binary_blob, looks_like_jid, _slim_quoted_message, parse_bool_flag as _parse_bool_flag

# ── Message delivery status ──────────────────────────────────────────────────
# The app's own scale (Baileys-shaped, what messages.status stores and what
# ui/conversations.py::_map_status renders): 2=sent, 3=delivered, 4=read,
# 5=played.  Two extra values make the states WhatsApp reports but the app used
# to swallow explicit:
STATUS_FAILED  = -1   # WhatsApp Web gave up on the send
STATUS_PENDING = 0    # created locally, not acked by the server yet

# WhatsApp's own ACK scale is WPP.whatsapp.enums.ACK:
#   -7 MD_DOWNGRADE, -6 INACTIVE, -5 CONTENT_UNUPLOADABLE, -4 CONTENT_TOO_BIG,
#   -3 CONTENT_GONE, -2 EXPIRED, -1 FAILED, 0 CLOCK, 1 SENT, 2 RECEIVED,
#   3 READ, 4 PLAYED, 5 PEER (acked by another of our own devices).
_ACK_TO_STATUS = {
    0: STATUS_PENDING,
    1: 2,
    2: 3,
    3: 4,
    4: 5,
    5: 2,
}


def ack_to_status(wpp_ack):
    """Translate a WhatsApp ACK into the app's status scale.

    Returns None when the ack is not one we understand — the caller must then
    leave the message's status alone. This used to be ``mapping.get(ack, 2)``,
    which reported *every* unrecognised ack as "sent": a FAILED (-1) ack, i.e.
    WhatsApp Web telling us the message will never leave the outbox, showed up
    in the UI as "Enviada". Silence and failure both have to be distinguishable
    from success here, since this is the app's only delivery feedback.
    """
    if not isinstance(wpp_ack, int) or isinstance(wpp_ack, bool):
        return None
    if wpp_ack < 0:
        # -1 FAILED and every more specific failure below it (expired, content
        # gone, too big, …) all mean the same thing to the user: not delivered.
        return STATUS_FAILED
    return _ACK_TO_STATUS.get(wpp_ack)


def _media_seconds(wpp_msg: dict):
    """Duration in whole seconds, or None when the payload never stated one.

    None and 0 are different answers and must stay that way. A voice note
    shorter than a second legitimately reports 0 — WhatsApp itself shows
    "0:00" for those — while a message whose duration simply never arrived
    (a forwarded media message echoed over the live socket, issue #43) has
    no duration at all. Writing 0 for both made the second case look like
    the first, so the UI stated a length that was certainly wrong instead of
    staying quiet about one it did not know.
    """
    dur = wpp_msg.get("duration")
    if dur is None:
        dur = wpp_msg.get("seconds")
    if dur is None and isinstance(wpp_msg.get("mediaData"), dict):
        dur = wpp_msg["mediaData"].get("duration")
    if dur is None or dur == "":
        return None
    try:
        return int(float(dur))
    except (TypeError, ValueError):
        return None


class WebSocketClient:
    # How long on_disconnect() waits, with the socket still down, before
    # declaring the app auto-offline. Was 5.0s — too short to ride out an
    # ordinary Wi-Fi power-save/NAT-rebind blip or a brief hiccup against the
    # local WPPConnect server (reported live as flipping to "modo offline"
    # far more often than any real WhatsApp outage would). python-socketio's
    # own reconnection backoff above (2s, doubling up to 60s) gets through
    # several attempts within this window before the app gives up and
    # believes it — a genuine outage is still caught either by this (a
    # little later) or by the 30-second HTTP health check regardless.
    _DISCONNECT_CONFIRM_SECONDS = 20.0

    def __init__(self, main_window, connect, instance_name):
        self.main_window = main_window
        self.connect = connect
        self.instance_name = instance_name.split(":")[0]
        #Initialize i18n
        self.i18n = I18n(self.main_window)
        self.i18n.get_language()

        self.sio = socketio.Client(
            reconnection=True,
            reconnection_attempts=0,      # 0 = unlimited
            reconnection_delay=2,
            reconnection_delay_max=60,
            logger=False,
            engineio_logger=False,
            handle_sigint=False,
        )
        # WPPConnect Server emits all events on root "/" namespace via req.io.emit().
        # Registering handlers without namespace defaults them to "/" (root).
        self.sio.on("connect", self.on_connect)
        self.sio.on("disconnect", self.on_disconnect)
        self.sio.on("qrCode", self.on_wpp_qrcode)
        self.sio.on("session-logged", self.on_wpp_session_logged)
        self.sio.on("received-message", self.on_wpp_message_received)
        self.sio.on("onack", self.on_wpp_ack)
        self.sio.on("phoneCode", self.on_wpp_phone_code)
        self.sio.on("phoneCodeError", self.on_wpp_phone_code_error)
        self.sio.on("status-find", self.on_wpp_status_find)
        self.sio.on("onpresencechanged", self.on_wpp_presence_changed)
        self.sio.on("chats-update", self.on_chats_update)
        self.sio.on("messages.update", self.on_messages_update)
        self.sio.on("onreactionmessage", self.on_wpp_reaction)
        self.sio.on("media-upload-progress", self.on_media_upload_progress)
        self.sio.on("incomingcall", self.on_wpp_incoming_call)
        # These two handlers existed but were never registered — contact
        # name/photo updates and presence changes only ever reached the app
        # through onpresencechanged and the 5-minute contacts poll, so a
        # renamed contact or a fresh presence event could sit stale for
        # minutes. Registering them is a no-op if WPPConnect never actually
        # emits these two event names (both bodies are already wrapped in
        # try/except), so there is nothing to lose by listening for them too.
        self.sio.on("contacts.update", self.on_contacts_update)
        self.sio.on("presence.update", self.on_presence_update)
        self.sio.on("groups.update", self.on_groups_update)

        # threading.Event used by on_continue() to wait for the phoneCode that
        # WPPConnect emits asynchronously via Socket.IO after /start-session.
        self._phone_code_event = threading.Event()
        self._phone_code_value: str = ""

        self._phone_code_error: str = ""

        # Debounce timer for on_disconnect() — see that method.
        self._disconnect_timer = None
        self._locally_sent_reaction_ids = {}

    def _consume_own_reaction_echo(self, msg):
        """True only for an echo of a reaction just sent by WinZapp."""
        reaction = (msg.get("message") or {}).get("reactionMessage") or {}
        signature = (str((reaction.get("key") or {}).get("id", "")),
                     (reaction.get("text") or "").strip())
        reaction_id = str((msg.get("key") or {}).get("id", ""))
        now = time.monotonic()
        local_ids = getattr(self, "_locally_sent_reaction_ids", None)
        if local_ids is None:
            local_ids = self._locally_sent_reaction_ids = {}
        self._locally_sent_reaction_ids = {
            event_id: created_at
            for event_id, created_at in local_ids.items()
            if now - created_at <= 60
        }
        if reaction_id and reaction_id in self._locally_sent_reaction_ids:
            return True
        pending = getattr(self.main_window, "_pending_own_reactions", None)
        lock = getattr(self.main_window, "_pending_own_reactions_lock", None)
        if pending is None or lock is None:
            return False
        with lock:
            created_at = pending.get(signature)
            if created_at is None:
                return False
            if now - created_at > 60:
                pending.pop(signature, None)
                return False
            pending.pop(signature, None)
            if reaction_id:
                self._locally_sent_reaction_ids[reaction_id] = now
            return True

    def _clean_jid(self, jid_val):
        if not jid_val:
            return ""
        if isinstance(jid_val, dict):
            jid_val = jid_val.get("_serialized") or jid_val.get("id") or ""
        if not isinstance(jid_val, str):
            jid_val = str(jid_val)
        return jid_val.replace("@c.us", "@s.whatsapp.net")

    def on_connect(self):
        logging.info("[WebSocketClient] WebSocket connected.")
        # Cancel any pending "confirm still disconnected" check from
        # on_disconnect() — we just reconnected, so that transient blip
        # never needs to be declared offline at all.
        if self._disconnect_timer is not None:
            self._disconnect_timer.cancel()
            self._disconnect_timer = None
        # Record when we connected so on_messages_upsert can use a stable
        # cutoff time rather than the ever-advancing time.time().
        self._connect_time = time.time()
        # This fires both on the initial connect and on every automatic
        # reconnect after a transport-level drop. on_disconnect() pauses the
        # MessageQueue by setting _wa_connected = False, but nothing else
        # reliably flips it back to True on a plain reconnect (WPPConnect
        # only re-emits "session-logged" around pairing/login, not on every
        # reconnect) — so a brief network blip could pause sending forever
        # even though WhatsApp itself never actually disconnected. Re-check
        # via HTTP and flush the queue so it self-heals like a manual resync.
        threading.Thread(target=self._recheck_connection_after_connect, daemon=True).start()

    def _recheck_connection_after_connect(self):
        try:
            self.main_window.check_wa_connection_http()
            if getattr(self.main_window, "_wa_connected", False):
                if hasattr(self.main_window, "message_queue"):
                    self.main_window.message_queue.flush()
                # Every Socket.IO (re)connect — not only the ones where
                # check_wa_connection_http() above also flips _wa_connected
                # from False to True — gets a catch-up sync opportunity.
                # WPPConnect's HTTP status can stay "CONNECTED" throughout a
                # purely transport-level Socket.IO drop (a brief network
                # blip too short for the 30s health check to ever see it as
                # down), so live messages.upsert events emitted during that
                # gap are simply gone — nothing else re-delivers them. This
                # is the client-side half of the "connection looks perfectly
                # stable yet a message silently never arrives, and F5 fixes
                # it" reports: was a live delivery gap, not a bug in how an
                # arrived message got processed. _sync_completed is reset so
                # trigger_sync_if_needed() is willing to run again; the
                # existing cooldown/backoff in that method still protects
                # against a flaky connection reconnecting every few seconds
                # turning this into a sync storm.
                self.main_window._sync_completed = False
                if hasattr(self.main_window, "trigger_sync_if_needed"):
                    self.main_window.trigger_sync_if_needed()
        except Exception:
            logging.exception("[WebSocketClient] _recheck_connection_after_connect error")

    def on_disconnect(self):
        logging.info("[WebSocketClient] WebSocket disconnected.")
        # Debounced: python-socketio auto-reconnects on its own within a few
        # seconds for an ordinary transient blip (Wi-Fi/NAT power-save churn,
        # a brief hiccup against the local WPPConnect server) — declaring the
        # app offline immediately for every one of those used to flicker the
        # title/tray between connected/disconnected and, once
        # _recheck_connection_after_connect() saw the reconnect, force a full
        # resync every time — even though WhatsApp itself never actually went
        # down and outgoing sends (which go over the REST API, not this
        # socket) were never actually blocked. Wait _DISCONNECT_CONFIRM_SECONDS
        # and only declare it if the socket is STILL down by then; a genuine
        # outage is still caught either by this (a little later) or by the
        # 30-second health check regardless.
        if self._disconnect_timer is not None:
            self._disconnect_timer.cancel()

        def _confirm_still_disconnected():
            if not self.sio.connected:
                self.main_window._set_wa_connected(False, "socket disconnected", False)

        self._disconnect_timer = threading.Timer(
            self._DISCONNECT_CONFIRM_SECONDS, lambda: wx.CallAfter(_confirm_still_disconnected)
        )
        self._disconnect_timer.daemon = True
        self._disconnect_timer.start()

    def on_connection_update(self, info):
        logging.debug(f"[WebSocketClient] event payload: {info}")
        #Checks the new connection state
        data             = info.get("data", {})
        connection_state = data.get("state", "")
        if connection_state == "open":
            # A confirmed live connection means any earlier logout is done
            # and re-pairing succeeded — clear the _handle_logout guard so a
            # genuinely new future logout is handled again instead of being
            # silently ignored as a stale duplicate.
            self._logout_handled = False
            # Store the user's own JID so self-chat detection and group-admin
            # checks have access to it throughout the session.
            wuid = data.get("wuid", "")
            if wuid:
                self.main_window.my_jid = wuid
                self.main_window.resolve_self_lid()
            # Mark WhatsApp as connected: this leaves automatic offline mode,
            # resumes the MessageQueue, clears the status text and retriggers a
            # sync that was skipped while the connection was down.
            self.main_window._set_wa_connected(True, "session-logged")
            if hasattr(self.main_window, "message_queue"):
                self.main_window.message_queue.flush()

            # Save the paired status so next startup knows pairing was fully completed.
            pi = self.main_window.settings.setdefault("privateinfo", {})
            if not pi.get("paired"):
                pi["paired"] = True
                self.main_window.save_settings()

            self.on_pairing_complete()

            # A pairing in progress that reaches "open" isn't necessarily
            # safe yet — WPPConnect's own Puppeteer/Chrome session can crash
            # moments later (confirmed live via wppconnect.log: a
            # "browserClose" event followed by taskkill errors for Chrome's
            # already-dead child processes) without Node.js itself going
            # down and, critically, without ever telling WinZapp anything
            # went wrong: the Socket.IO connection to the still-alive
            # WPPConnect Server process never drops, so on_connection_update
            # never gets a "close" to react to and the app just sits there
            # believing it's connected forever, with no window ever shown
            # and no error. This watchdog is the only check independent of
            # WPPConnect telling us anything: if this attempt hasn't
            # received real chat data by the time it fires, it treats the
            # pairing as failed on its own.
            if getattr(self.main_window, "_pairing_in_progress", False):
                self._start_pairing_watchdog()
        elif connection_state == "close":
            was_connected = self.main_window._wa_connected
            self.main_window._set_wa_connected(False, "session closed")

            # Detect permanent WhatsApp logout (status 401 = loggedOut).
            status_code  = (
                data.get("statusCode")
                or data.get("status")
                or (data.get("lastDisconnect") or {}).get("statusCode")
            )
            is_logout = (
                data.get("loggedOut", False)
                or status_code == 401
            )

            if self.main_window._self_inflicted_teardown_expected():
                # We are mid our own deliberate shutdown or a WPPConnect
                # Server auto-update — both POST /close-session themselves
                # with the WebSocket deliberately left connected throughout,
                # so this "close" is the direct, expected result of that
                # call, not WhatsApp unlinking anything. A self-inflicted
                # close is reported the exact same way as a real phone-side
                # logout (loggedOut=True / statusCode 401).
                return

            # A connection that closes again — for ANY reason, not just an
            # explicit 401/loggedOut — while a pairing attempt is still in
            # progress and before WPPConnect ever delivered real chat data
            # means WhatsApp never actually finished linking the device, even
            # though it may have briefly reported "open" and made WinZapp
            # announce itself as connected. _pairing_in_progress is a narrow
            # window (set when Connect.on_continue() starts, cleared once
            # messages.set arrives) specifically so this never fires for an
            # ordinary reconnect hiccup on an already-established, already-
            # synced account — only for a pairing that never truly completed.
            pairing_failed = (
                not is_logout
                and getattr(self.main_window, "_pairing_in_progress", False)
                and not getattr(self.main_window, "messages_set_completed", False)
            )
            if is_logout or pairing_failed:
                if pairing_failed:
                    logging.warning(
                        "[WebSocketClient] Connection closed during an active pairing "
                        "before the initial sync ever started (statusCode=%s) — "
                        "treating as a failed pairing.", status_code,
                    )
                    wx.CallAfter(self._handle_pairing_failed)
                else:
                    # Permanent logout: clear credentials and redirect to pairing.
                    wx.CallAfter(self._handle_logout)
            else:
                # Temporary disconnection (network glitch, WhatsApp session interrupted).
                # Mark WA as disconnected so the MessageQueue stops trying to send.
                # Do NOT show a blocking dialog — Baileys reconnects automatically and
                # fires connection.update(state=open) when it succeeds.  A blocking
                # dialog would freeze the UI and prevent that recovery.
                def _notify_disconnection():
                    mw = self.main_window
                    mw._set_wa_connected(False, "temporary disconnection")
                    mw.error_sound.play()
                    mw.output(self.i18n.t("wa_disconnected_temp"), interrupt=False)
                    mw._set_status(self.i18n.t("tray_wa_disconnected"))
                wx.CallAfter(_notify_disconnection)

    def _start_pairing_watchdog(self, timeout: float = 45.0):
        """
        Safety net for a pairing that reached "open" and is then never heard
        from again — no further Socket.IO event at all, not even a "close".

        Runs on a plain threading.Timer, independent of both the wx main
        thread and the Socket.IO background thread, specifically so it still
        fires even if one of those is stuck: the failure mode this exists
        for (WPPConnect's own Puppeteer/Chrome session crashing right after
        briefly reporting "open", confirmed live via wppconnect.log showing
        a "browserClose" event) leaves Node.js itself running and the
        Socket.IO connection to it intact, so nothing ever tells
        on_connection_update's "close" branch to react — the ordinary
        recovery path never gets a chance to run at all.
        """
        my_attempt = self.connect._pairing_attempt_id

        def _check():
            if self.connect._pairing_attempt_id != my_attempt:
                return  # superseded — cancelled, or a newer attempt started
            if not getattr(self.main_window, "_pairing_in_progress", False):
                return  # already resolved: synced, cancelled, or already recovered
            logging.warning(
                "[WebSocketClient] Pairing attempt still hadn't received real "
                "chat data %.0fs after appearing to open — treating as a "
                "failed pairing (watchdog).", timeout,
            )
            wx.CallAfter(self._handle_pairing_failed)

        t = threading.Timer(timeout, _check)
        t.daemon = True
        t.start()

    def _handle_logout(self):
        """Handle a permanent WhatsApp logout (device removed from account)."""
        self._reset_credentials_and_show_pairing("device_logged_out")

    def _handle_pairing_failed(self):
        """
        A pairing attempt appeared to succeed — WPPConnect briefly reported
        the connection as "open" and WinZapp announced it as connected — but
        it closed again before the initial sync ever started, meaning
        WhatsApp itself never actually finished linking the device (a
        rejected/timed-out pairing, reported live as the phone showing "Não
        foi possível conectar o dispositivo" seconds after WinZapp had
        already played the connected sound).

        Recovers exactly like a permanent logout: without this, the "close"
        branch of on_connection_update only recognizes explicit 401/loggedOut
        signals as a reason to reset and re-show the pairing dialog, so this
        kind of failure — which carries neither — left the app sitting
        indefinitely on a half-finished pairing with no error, no window,
        and no way back to the connection dialog.
        """
        self._reset_credentials_and_show_pairing("pairing_failed_msg")

    def _reset_credentials_and_show_pairing(self, message_key: str):
        """Shared recovery for _handle_logout()/_handle_pairing_failed().

        Runs on the wx main thread (via wx.CallAfter).  Shows an informative
        dialog, wipes the now-invalid credentials from settings, disconnects
        the socket, and opens the connection dialog so the user can re-pair.

        Multiple independent event paths can decide the same underlying
        problem happened (on_connection_update's 401/loggedOut check, its new
        failed-pairing check, and on_wpp_status_find's notLogged/
        disconnectedMobile check) and all schedule one of the two methods
        above via wx.CallAfter before any has run. Since CallAfter callbacks
        are dispatched one at a time on this same thread, a simple flag
        checked at entry is enough to make every call after the first a
        no-op — without it the error dialog appeared twice, credentials were
        wiped twice, and two pairing dialogs could end up stacked on screen.
        """
        if getattr(self, "_logout_handled", False):
            return
        self._logout_handled = True

        mw = self.main_window
        mw._wa_connected = False
        mw._pairing_in_progress = False
        mw.error_sound.play()

        wx.MessageBox(
            self.i18n.t(message_key),
            self.i18n.t("error").format(app_name=mw.app_name),
            wx.OK | wx.ICON_ERROR,
        )

        # Wipe the invalidated credentials so next startup goes to pairing.
        old_token = mw._get_wa_token()
        pi = mw.settings.setdefault("privateinfo", {})
        mw._set_wa_token("")
        pi.pop("WA_phone_number", None)
        pi.pop("paired", None)
        mw.messages_set_completed = False
        mw.token = ""
        mw.save_settings()

        # Wipe all cached chats/contacts/media to avoid cross-account data leakage
        mw.clear_local_data()

        # Best-effort: close the WPPConnect session so Chrome is released.
        if old_token:
            def _close():
                try:
                    api_post(
                        f"{mw.wpp_server}:{mw.wpp_port}/api/{old_token}/close-session",
                        headers={"Authorization": f"Bearer {old_token}", "Content-Type": "application/json"},
                        timeout=5,
                    )
                except Exception:
                    pass
            threading.Thread(target=_close, daemon=True).start()

        # Disconnect the socket (may already be disconnecting).
        try:
            self.sio.disconnect()
        except Exception:
            pass

        # Reset connection state as if this were a fresh launch — see the
        # matching comment in main.py's _on_disconnect() for why: without
        # this, _set_wa_connected()'s startup grace window stays permanently
        # disabled after re-pairing (it only applies while
        # "never _wa_connect_announced"), so the first not-yet-settled check
        # right after the new pairing completes gets mistaken for a real
        # outage.
        mw._wa_connected = False
        mw._wa_connect_announced = False
        mw._auto_offline = False
        mw._wa_offline_strikes = 0
        mw._wa_startup_time = time.time()

        # Redirect to pairing dialog.
        self.connect.show_connection_dial()

    def on_pairing_complete(self):
        # End the dialogs' modal loops on the main thread to avoid wx
        # thread-safety issues. Guards against the case where the app is
        # already paired (no dialogs open).
        #
        # connection_dial (and pairing_dial, its child) are shown via
        # ShowModal() — Destroy()ing a dialog directly while its modal loop
        # is still running never signals that loop to unwind, so wx never
        # re-enables the parent window ShowModal() disabled when it started.
        # The dialog object goes away but the main window stays blocked for
        # input — reported live as "reconnected successfully, but the main
        # window was frozen/unusable and kept announcing 'connection
        # restored' in the background".
        #
        # EndModal() ONLY here — never Destroy(). Both dialogs already
        # Destroy() themselves right after their own ShowModal() call
        # returns (show_pairing_dial() / show_connection_dial() in
        # connect.py).
        #
        # Close ONLY the innermost modal here. pairing_dial is nested INSIDE
        # connection_dial's own modal loop (on_continue() opens it from a
        # button handler running inside connection_dial.ShowModal()), and wx
        # only allows EndModal() on the loop that is actually running.
        #
        # EndModal() does NOT unwind its loop immediately — it merely signals
        # it — and that still-running loop keeps dispatching pending events,
        # including any wx.CallAfter queued from within it. So closing
        # connection_dial from here was impossible: both inline and via a
        # CallAfter chained off this same handler ran while pairing_dial's
        # loop was still the running one, and wx rejected it with a hard
        # assertion ("IsRunning()" failed ... "Use ScheduleExit() on not
        # running loop"). log.log confirmed the ordering — the parent's close
        # attempt logged BEFORE "pairing_dial modal loop returned".
        #
        # connection_dial is therefore closed by show_pairing_dial() itself,
        # right after its own ShowModal() returns (see connect.py), which is
        # the only point where control is provably back in the parent's loop.
        def _end_innermost_dialog():
            # Phone-pairing flow: pairing_dial is on top, and closing it lets
            # show_pairing_dial() resume and close connection_dial in turn.
            if hasattr(self.connect, 'pairing_dial'):
                try:
                    dlg = self.connect.pairing_dial
                    # `if dlg` — not wx.IsDestroyed(dlg), which does not exist:
                    # that call raised AttributeError on every single pairing,
                    # so this whole block fell straight into the except below and
                    # neither EndModal() nor anything else ever ran. A wxPython
                    # wrapper whose C++ window is gone is falsy, which is the
                    # check connect.py already uses for this same dialog.
                    if dlg and dlg.IsModal():
                        logging.info("[on_pairing_complete] Ending pairing_dial modal loop.")
                        dlg.EndModal(wx.ID_OK)
                        return
                    logging.info("[on_pairing_complete] pairing_dial not modal — falling through to connection_dial.")
                except Exception:
                    logging.exception("[on_pairing_complete] Failed to end pairing_dial.")
                    return
            else:
                logging.info("[on_pairing_complete] No pairing_dial attribute — closing connection_dial directly.")
            # QR-code flow (or pairing_dial already gone): connection_dial is
            # itself the innermost running loop, so it can be ended here.
            if hasattr(self.connect, 'connection_dial'):
                try:
                    dlg = self.connect.connection_dial
                    # See the pairing_dial guard above for why this is `if dlg`.
                    if dlg and dlg.IsModal():
                        logging.info("[on_pairing_complete] Ending connection_dial modal loop.")
                        dlg.EndModal(wx.ID_OK)
                    else:
                        logging.info("[on_pairing_complete] connection_dial not modal — nothing to end.")
                except Exception:
                    logging.exception("[on_pairing_complete] Failed to end connection_dial.")
            else:
                logging.info("[on_pairing_complete] No connection_dial attribute — nothing to close.")

        logging.info("[on_pairing_complete] Scheduling dialog close via CallAfter.")
        wx.CallAfter(_end_innermost_dialog)


    @staticmethod
    def _extract_qr_payload(info) -> tuple:
        """(base64_image, pairing_code) from a 'qrCode' event, whatever its shape.

        WPPConnect Server emits, from exportQR() in createSessionUtil.ts:

            req.io.emit('qrCode', {data: 'data:image/png;base64,…', session: …})

        i.e. ``info["data"]`` is a *string*. This used to be read as
        ``info["data"]["qrcode"]["base64"]``, so every single event raised
        AttributeError on the string — swallowed by python-socketio, which runs
        with logging disabled, so nothing ever appeared in the log.

        The damage was not cosmetic: this handler is the only thing that
        refreshes the QR. WhatsApp rotates it roughly every 20 s, so the dialog
        was left showing the one-shot copy fetched from status-session when it
        opened, long expired by the time anyone pointed a phone at it — read as
        an invalid code. The pairing-code refresh path rode on the same event
        and was equally dead.

        Nested shapes are still accepted in case a different WPPConnect build
        wraps it, and top-level keys are used as a last resort.
        """
        if not isinstance(info, dict):
            return "", ""
        base64_img, pairing_code = "", ""
        raw = info.get("data")
        if isinstance(raw, str):
            base64_img = raw
        elif isinstance(raw, dict):
            inner = raw.get("qrcode")
            if isinstance(inner, dict):
                base64_img = inner.get("base64") or ""
                pairing_code = inner.get("pairingCode") or ""
            elif isinstance(inner, str):
                base64_img = inner
            base64_img = base64_img or raw.get("base64") or ""
            pairing_code = pairing_code or raw.get("pairingCode") or ""
        base64_img = base64_img or info.get("qrcode") or info.get("base64") or ""
        pairing_code = pairing_code or info.get("pairingCode") or ""
        return (base64_img if isinstance(base64_img, str) else "",
                str(pairing_code) if pairing_code else "")

    def on_qrcode_update(self, info):
        logging.debug(f"[WebSocketClient] event payload: {info}")
        # A live connection makes any QR event stale by definition — but being
        # *paired* deliberately does not, and testing it here disabled this
        # method's whole reason for existing. `paired` is written once on the
        # first successful pairing and only cleared on an explicit logout, so
        # on every install that has ever paired it is permanently True: the
        # early return fired unconditionally, and the proactive pairing dialog
        # below could never open again.
        #
        # That dialog exists for exactly the case this guard excluded — the
        # stored session can no longer be restored, so WPPConnect starts
        # emitting fresh QR codes while `paired` is still True. Without it the
        # user sits on "offline" with no explanation for minutes, until the
        # much slower confirmed-logout path finally reacts. See
        # tests/test_qrcode_auto_repair_dialog.py.
        #
        # Stale events from a previous session, which is what that guard was
        # reaching for, are already filtered upstream by
        # _belongs_to_this_session() and on_wpp_qrcode()'s own instance_name
        # check — this method never needed to re-decide it.
        if getattr(self.main_window, "_wa_connected", False):
            logging.info("[on_qrcode_update] WhatsApp already connected — ignoring qrCode event.")
            return
        base64_img, pairing_code = self._extract_qr_payload(info)
        if not base64_img and not pairing_code:
            logging.warning("[on_qrcode_update] qrCode event carried nothing usable: %r",
                            list(info.keys()) if isinstance(info, dict) else type(info).__name__)
            return
        logging.info("[on_qrcode_update] Refreshed QR (%d bytes of image, pairing_code=%s).",
                     len(base64_img), bool(pairing_code))

        def _update_ui():
            # Use connection_mode to determine which mode we're in
            if self.connect.connection_mode == "qrcode" and base64_img:
                # QR-CODE mode: update the image
                self.main_window.pairing_code_updated_sound.play()
                self.main_window.speak_output.output(self.i18n.t("qrcode_image_updated"))
                self.connect.display_qrcode_image(base64_img)
            elif self.connect.connection_mode == "phone" and pairing_code:
                # Pairing code mode: update the text field only if it still exists.
                # `if field` — not wx.IsDestroyed(field), which does not exist and
                # raised AttributeError here on every single rotated code. Caught
                # by the bare `except Exception: pass` this used to have, it made
                # WPPConnect's whole qrCode-carrying-a-pairingCode refresh path
                # silently dead: the dialog kept showing the first code while
                # WhatsApp had already rotated it. A destroyed wx wrapper is falsy
                # and any call on it raises RuntimeError, so the `and` below is the
                # whole guard needed (same idiom as connect.py's update_pairing_code).
                field = getattr(self.connect, "pairing_code_field", None)
                if field:
                    try:
                        current_val = field.GetValue().strip()
                        if current_val != pairing_code.strip():
                            self.main_window.pairing_code_updated_sound.play()
                            self.main_window.speak_output.output(self.i18n.t("qrcode_updated"))
                            field.SetValue(pairing_code)
                    except RuntimeError:
                        pass  # dialog destroyed between the check and the call
                    except Exception:
                        # Never let a refresh failure go unnoticed again — this
                        # bug hid behind a silent `pass` for exactly that reason.
                        logging.exception("[on_qrcode_update] Failed to refresh the pairing code field.")
            elif (
                base64_img
                and not self.main_window._is_pairing_dialog_active()
                and self.main_window.settings.get("privateinfo", {}).get("paired")
                and not getattr(self.main_window, "_auto_repair_dialog_shown", False)
            ):
                # WPPConnect just generated a real QR/pairing code with no
                # pairing dialog open at all — it only does this once it has
                # already decided the stored session can't be restored, so
                # this is a reliable "you need to re-pair" signal on its own,
                # unlike the coarse status-session string the health-check
                # poll watches (which needs several minutes of confirmation
                # to rule out a normal slow boot). Surfacing the pairing
                # dialog immediately — instead of leaving the user staring at
                # "offline" for however long _AUTO_RESTART_LOGOUT_GRACE_SECONDS
                # or the multi-minute unlink confirmation takes with no
                # explanation —
                # was an explicit, accepted tradeoff: this dialog's own
                # Cancel/close buttons quit the app / drop the WebSocket for
                # good, which is fine here specifically because a session
                # that reached this point has nothing left to lose by
                # closing — see the conversation this was decided in.
                # _auto_repair_dialog_shown latches so a 20-30s QR refresh
                # while the user is still deciding what to do doesn't pop
                # a second nested dialog.
                self.main_window._auto_repair_dialog_shown = True
                logging.warning(
                    "[on_qrcode_update] Session needs re-pairing while "
                    "previously paired, with no pairing dialog open — "
                    "showing it proactively instead of waiting for the "
                    "slower confirmed-logout detection."
                )
                # Same sound + MessageBox as the confirmed-logout path's own
                # _logout_with_warning() (main.py) — recognisable, expected
                # feedback that something happened, just without that path's
                # _on_disconnect() call (no data wipe). Also bring the window
                # to the foreground first: if it was minimized to the tray
                # (background mode), a modal dialog appearing behind/under
                # everything with no prior audible cue is easy to miss
                # entirely — reported live as exactly that.
                self.main_window.restore_window()
                self.main_window.error_sound.play()
                wx.MessageBox(
                    self.i18n.t("device_logged_out"),
                    self.i18n.t("error").format(app_name=self.main_window.app_name),
                    wx.OK | wx.ICON_ERROR,
                )
                self.connect.show_connection_dial()

        wx.CallAfter(_update_ui)

    def on_messages_set(self, info):
        self.main_window.messages_set_completed = True
        # Real chat data has arrived — this pairing (if one was in progress)
        # has genuinely succeeded, so it's no longer at risk of being treated
        # as a failed pairing by on_connection_update if the socket later
        # drops for an ordinary/unrelated reason.
        self.main_window._pairing_in_progress = False
        # _try_start_sync_thread() atomically checks "already running or
        # already completed" and starts self.sync_thread under a lock —
        # WPPConnect sends messages.set in multiple batches during initial
        # sync, and this same method also gets called directly (not via a
        # real messages.set event) elsewhere, so more than one caller can
        # race to start a sync within milliseconds of each other. A plain
        # is_alive() check here (the old code) has a gap between checking
        # and starting that another thread's own check can land in — two
        # sync threads running at once was reported live as "sincronizando
        # conversas" announced twice and, worse, concurrent writes to the
        # single DatabaseBridge connection failing outright and flooding the
        # screen with error dialogs.
        self.main_window._try_start_sync_thread()

    def on_messages_upsert(self, info):
        """
        Handle real-time incoming messages from the WPPConnect.

        In WPPConnect v2 the websocket envelope is
          {"event": "messages.upsert", "instance": ..., "data": {<message>}, ...}
        where "data" is a single message object (key, pushName, message,
        messageType, messageTimestamp, ...).
        """
        try:
            # Process real-time messages directly. Main window's on_new_message
            # already deduplicates based on the message ID to prevent duplicate historical entries.

            msg = info.get("data", {})
            if not isinstance(msg, dict) or not msg.get("key"):
                return

            # See on_wpp_message_received()'s own breadcrumb for why this is
            # unconditional — pins down exactly which branch below (history-
            # sync echo, own-send echo, or a genuine live dispatch) a given
            # message id took, instead of only knowing the event arrived.
            key = msg.get("key", {})
            logging.info(
                "[WebSocketClient] on_messages_upsert: id=%s remoteJid=%s fromMe=%s "
                "type=%s isMdHistoryMsg=%s",
                key.get("id", ""), key.get("remoteJid", ""), key.get("fromMe"),
                msg.get("messageType", ""), msg.get("isMdHistoryMsg"),
            )

            # ── Skip history-sync echoes ───────────────────────────────────────
            # WPPConnect/Baileys fires messages.upsert for historical messages
            # (isMdHistoryMsg=True) during its initial sync phase. These are
            # normally the same records already fetched by sync_chat_messages via
            # the REST API and placed in the correct chronological position, so
            # treating them as live new messages would append them at the bottom
            # of the conversation as if they had just been sent — dispatch them
            # to the historical handler to be saved silently instead.
            #
            # WPPConnect can also tag a genuinely new message with this flag.
            # Use the stable socket connection time to distinguish it from a
            # replayed chunk: old timestamps stay silent even after list-chats
            # has already populated self.chats, while current messages retain
            # notifications, sound and unread handling.
            if msg.get("isMdHistoryMsg"):
                key = msg.get("key", {})
                remote_jid = self.main_window._normalize_jid(key.get("remoteJid", ""))
                raw_timestamp = msg.get("messageTimestamp") or msg.get("timestamp") or 0
                try:
                    message_timestamp = float(raw_timestamp)
                    if message_timestamp > 1_000_000_000_000:
                        message_timestamp /= 1000
                except (TypeError, ValueError):
                    message_timestamp = 0
                predates_connection = (
                    message_timestamp > 0
                    and message_timestamp < getattr(self, "_connect_time", 0) - 5
                )
                if predates_connection or remote_jid not in self.main_window.chats:
                    wx.CallAfter(self.main_window.on_historical_message, msg)
                    return
                # Chat already known/synced — fall through to live handling below.

            # Extract JID mapping from WebSocket message
            self.main_window._extract_lid_mapping(msg)
            # fromMe=True can mean two things:
            #   (a) WinZapp sent this message via MessageQueue — already rendered
            #       in the UI; the WebSocket echo must be ignored.
            #   (b) The user sent this message from another device (phone, official
            #       Windows app) — must be added to the conversation like any
            #       incoming message (but without playing a notification sound).
            # We distinguish the two cases via _own_sent_ids, which is populated
            # by MessageQueue immediately after the API returns the real message ID.
            if msg.get("key", {}).get("fromMe", False):
                if msg.get("messageType") == "reactionMessage":
                    # fromMe also covers reactions made on linked devices. Only
                    # suppress one that matches a reaction WinZapp just sent.
                    if self._consume_own_reaction_echo(msg):
                        return
                msg_id = msg.get("key", {}).get("id", "")
                _lock = getattr(self.main_window, "_own_sent_ids_lock", None)
                if _lock is not None:
                    with _lock:
                        _is_own = msg_id and msg_id in self.main_window._own_sent_ids
                else:
                    _is_own = msg_id and msg_id in getattr(self.main_window, "_own_sent_ids", set())
                if _is_own:
                    return  # echo of our own send — skip
                # Otherwise: sent from another device — fall through to on_new_message
            wx.CallAfter(self.main_window.on_new_message, msg)

        except Exception:
            logging.exception("[WebSocketClient] on_messages_upsert error")

    def on_groups_update(self, info):
        """
        Handle group metadata updates from WPPConnect.
        """
        try:
            if not isinstance(info, dict) or not self._belongs_to_this_session(info):
                return
            updates = info.get("data", [])
            if not isinstance(updates, list):
                updates = [updates]

            for update in updates:
                if not isinstance(update, dict):
                    continue
                jid = self._clean_jid(update.get("id") or update.get("remoteJid") or "")
                subject = update.get("subject") or update.get("name") or ""

                if jid.endswith("@g.us") and isinstance(subject, str) and subject.strip():
                    remote_jid = self.main_window._normalize_jid(jid)
                    wx.CallAfter(
                        self.main_window.on_group_subject_updated,
                        remote_jid,
                        subject.strip(),
                    )
        except Exception:
            logging.exception("[WebSocketClient] on_groups_update error")

    def on_messages_update(self, info):
        """
        Handle messages.update — delivery/read status changes for sent messages.

        WPPConnect v2 sends:
          {"data": [{"key": {"id": ..., "remoteJid": ..., "fromMe": true},
                     "status": "READ"|"DELIVERY_ACK"|"SERVER_ACK",
                     "update": {"status": 4}}]}
        """
        try:
            if not isinstance(info, dict) or not self._belongs_to_this_session(info):
                return
            data = info.get("data", [])
            if isinstance(data, dict):
                data = [data]
            if not isinstance(data, list):
                return
            for update in data:
                if not isinstance(update, dict):
                    continue
                if not update.get("key", {}).get("fromMe"):
                    continue
                wx.CallAfter(self.main_window.on_message_status_update, update)
        except Exception:
            logging.exception("[WebSocketClient] on_messages_update error")

    def on_chats_update(self, info):
        """
        Handle chats.update — partial chat state changes (e.g. unreadCount reset
        when the user reads messages on another device via app-state sync).

        WPPConnect emits:
          {"data": [{"remoteJid": ..., "unreadCount": 0, ...}]}
        """
        try:
            if not isinstance(info, dict) or not self._belongs_to_this_session(info):
                return
            note_live = getattr(self.main_window, "_note_live_wpp_event", None)
            if note_live:
                note_live()
            data = info.get("data", [])
            if isinstance(data, dict):
                data = [data]
            if not isinstance(data, list):
                return
            for chat_update in data:
                if not isinstance(chat_update, dict):
                    continue
                jid = chat_update.get("remoteJid") or chat_update.get("id", "")
                if not jid:
                    continue
                unread = chat_update.get("unreadCount")
                if unread is not None:
                    try:
                        unread = int(unread)
                    except (TypeError, ValueError):
                        continue
                    # What the chat held before this change, when the page was
                    # able to tell us (see createSessionUtil.ts's
                    # onUnreadCountChanged). It is what separates "the user
                    # read this on their phone" (fell from N>0 to 0) from a
                    # chat merely being loaded into WhatsApp Web's Store,
                    # which reports 0 with nothing behind it. None = unknown.
                    previous = chat_update.get("previousUnreadCount")
                    try:
                        previous = int(previous) if previous is not None else None
                    except (TypeError, ValueError):
                        previous = None
                    # WhatsApp uses -1 as a sentinel for a chat manually
                    # marked unread. Downstream code works with badge counts,
                    # where that state is one unread item. Normalize both the
                    # new and previous values so a subsequent -1 -> 0 is also
                    # recognized as a confirmed remote read.
                    if unread < 0:
                        unread = 1
                    if previous is not None and previous < 0:
                        previous = 1
                    # Logged on arrival, before any of MainWindow's guards can
                    # discard it: this is the only place that can tell "WhatsApp
                    # Web never emitted the event" apart from "it arrived and
                    # something dropped it", and the whole path used to be
                    # silent — a read made on the phone that never reached the
                    # badge left no trace at all in log.log to work from.
                    logging.info(
                        "[unread] chats-update in: %s unread=%s previous=%s",
                        jid, unread, previous,
                    )
                    wx.CallAfter(
                        self.main_window.on_chat_unread_update,
                        jid, unread, previous,
                    )
                
                archive = chat_update.get("archive") if chat_update.get("archive") is not None else chat_update.get("archived")
                if archive is not None:
                    # bool("false") is True — parsing this the naive way is how
                    # conversations that were never archived on WhatsApp kept
                    # jumping into the Archived tab. Only act on a value we can
                    # actually interpret.
                    archived_flag = _parse_bool_flag(archive)
                    if archived_flag is not None:
                        wx.CallAfter(self.main_window.on_chat_archive_update, jid, archived_flag)

                # Handle pin/unpin updates in real-time
                pin = chat_update.get("pin")
                if pin is not None:
                    if isinstance(pin, str):
                        if pin.lower() == "true": pin = True
                        elif pin.lower() == "false": pin = False
                        else:
                            try: pin = float(pin)
                            except ValueError: pin = None
                    is_pinned = False
                    if isinstance(pin, bool):
                        is_pinned = pin
                    elif isinstance(pin, (int, float)):
                        # Matches the threshold get_remote_chats() (main.py)
                        # uses for the same field from the polled list-chats
                        # response — pin is a pin-timestamp in real WhatsApp
                        # data, so any genuine value is always far above this,
                        # but keeping both call sites on the same threshold
                        # avoids the two ever disagreeing on a borderline value.
                        is_pinned = pin > 1_000_000
                    wx.CallAfter(self.main_window.on_chat_pin_update, jid, is_pinned)
        except Exception:
            logging.exception("[WebSocketClient] on_chats_update error")

    def on_presence_update(self, info):
        """
        Handle presence.update — online/typing/last-seen changes for contacts.

        WPPConnect wraps the Baileys payload as:
          {"data": {"id": "55XXX@s.whatsapp.net",
                    "presences": {"55XXX@s.whatsapp.net": {
                        "lastKnownPresence": "available"|"unavailable"|"composing"|...,
                        "lastSeen": <unix_ts>|null}}}}
        """
        try:
            if not isinstance(info, dict) or not self._belongs_to_this_session(info):
                return
            note_live = getattr(self.main_window, "_note_live_wpp_event", None)
            if note_live:
                note_live()
            data      = info.get("data", {})
            jid       = data.get("id", "")
            presences = data.get("presences", {})
            if not jid or not isinstance(presences, dict):
                return
            wx.CallAfter(self.main_window.on_presence_update, jid, presences)
        except Exception:
            logging.exception("[WebSocketClient] on_presence_update error")

    def on_wpp_presence_changed(self, info):
        """
        Handle WPPConnect onpresencechanged event.
        Payload format matches PresenceChangeEvent from WPPConnect.
        """
        if not info or not isinstance(info, dict):
            return
        try:
            # Ignore presence events for other sessions (multi-session server).
            if not self._belongs_to_this_session(info):
                return
            # The id can be a string or a dict/object (Wid)
            raw_id = info.get("id")
            if isinstance(raw_id, dict):
                chat_jid = raw_id.get("_serialized", "")
            else:
                chat_jid = str(raw_id or "")

            if not chat_jid:
                return

            # WPPConnect's own `isGroup` flag has been observed false for a
            # real group event (id ending @g.us) right after a fresh pairing
            # — reported live as EVERY group's typing indicator showing
            # "Participante sem nome" for everyone, even after re-pairing
            # from scratch: with is_group wrongly False, this fell into the
            # single-chat branch below and keyed presences by the group's
            # own jid instead of by participant, which is exactly the shape
            # _resolve_jid_name() (main.py) already has a guard for — that
            # guard just means "no participant identified", not "not a
            # group". The @g.us suffix is authoritative and doesn't depend
            # on wa-js's own state being warmed up yet, so it's checked
            # first regardless of what isGroup says.
            is_group = chat_jid.endswith("@g.us") or bool(info.get("isGroup", False))
            
            # We want to format this into the presences dict that main.py expects:
            # presences: {participant_jid: {"lastKnownPresence": state, "lastSeen": timestamp}}
            presences = {}
            
            # Map state to expected values (available, unavailable, composing, recording).
            # Per WPPConnect's own PresenceEvent type, "state" is one of:
            # 'available' | 'composing' | 'recording' | 'unavailable'. Older/alternate
            # builds have been seen using "online"/"offline"/"typing" instead, so those
            # are normalised here too.
            def map_state(s):
                if not s:
                    return "unavailable"
                s = s.strip().lower()
                if s == "online":
                    return "available"
                if s == "offline":
                    return "unavailable"
                # WPPConnect/WhatsApp Web uses "typing" where Baileys uses "composing"
                if s == "typing":
                    return "composing"
                # Map WPPConnect recording_audio to recording
                if s == "recording_audio":
                    return "recording"
                if s not in ("available", "unavailable", "composing", "recording", "paused"):
                    # Unknown/unexpected chat-state value — log it so a real-world
                    # mismatch (e.g. a different literal used for audio recording)
                    # can be diagnosed from the logs instead of failing silently.
                    logging.warning(f"[WebSocketClient] Unrecognized presence state: {s!r} (raw info: {info})")
                return s

            timestamp = info.get("t")

            if is_group:
                participants = info.get("participants", [])
                if isinstance(participants, list):
                    for p in participants:
                        if not isinstance(p, dict):
                            continue
                        p_raw_id = p.get("id")
                        if isinstance(p_raw_id, dict):
                            p_jid = p_raw_id.get("_serialized", "")
                        else:
                            p_jid = str(p_raw_id or "")
                        if p_jid:
                            p_state = map_state(p.get("state"))
                            presences[p_jid] = {
                                "lastKnownPresence": p_state,
                                "lastSeen": timestamp
                            }
            else:
                state = map_state(info.get("state"))
                presences[chat_jid] = {
                    "lastKnownPresence": state,
                    "lastSeen": timestamp
                }

            if presences:
                logging.info(f"[WebSocketClient] on_wpp_presence_changed JID: {chat_jid}, presences: {presences}")
                wx.CallAfter(self.main_window.on_presence_update, chat_jid, presences)
        except Exception:
            logging.exception("[WebSocketClient] on_wpp_presence_changed error")

    def on_contacts_update(self, info):
        """
        Handle contacts.update to keep contact names and pictures fresh.

        WPPConnect v2 emits this event with "data" being either a single
        contact dict or a list of contact dicts:
          {"remoteJid": ..., "pushName": ..., "profilePicUrl": ..., "instanceId": ...}
        New messages (1:1 and group) arrive via messages.upsert.
        """
        try:
            if not isinstance(info, dict) or not self._belongs_to_this_session(info):
                return
            data = info.get("data", [])
            if isinstance(data, dict):
                data = [data]
            if not isinstance(data, list):
                return
            updated = False
            for contact in data:
                if not isinstance(contact, dict):
                    continue
                # Normalise @c.us → @s.whatsapp.net so the lookup matches the
                # contacts dict, which always stores entries under the modern
                # @s.whatsapp.net format.
                jid = self.main_window._normalize_jid(contact.get("remoteJid", ""))
                if not jid:
                    continue
                existing = self.main_window.contacts.get(jid)
                # Bridge @lid JIDs to their canonical phone JID before giving up.
                if existing is None and jid.endswith("@lid"):
                    phone_jid = getattr(self.main_window, "_lid_to_phone", {}).get(jid, "")
                    if phone_jid:
                        existing = self.main_window.contacts.get(phone_jid)
                        if existing is not None:
                            jid = phone_jid
                if existing is None:
                    # Contact was absent from self.contacts (filtered out by
                    # get_remote_contacts because it had no pushName in the DB
                    # at sync time). If this event carries a name, create the
                    # entry now so future lookups can find it.
                    push = contact.get("pushName", "")
                    if push:
                        entry = {
                            "remoteJid": jid,
                            "pushName": push,
                            "profilePicUrl": contact.get("profilePicUrl") or "",
                            "type": "contact",
                        }
                        self.main_window.contacts[jid] = entry
                        updated = True
                        try:
                            self.main_window.db.upsert_contact(jid, entry)
                        except Exception:
                            pass
                    continue
                if contact.get("pushName") and contact["pushName"] != existing.get("pushName"):
                    existing["pushName"] = contact["pushName"]
                    updated = True
                if contact.get("profilePicUrl") and contact["profilePicUrl"] != existing.get("profilePicUrl"):
                    existing["profilePicUrl"] = contact["profilePicUrl"]
                    updated = True
                if updated and hasattr(self.main_window, "db"):
                    try:
                        self.main_window.db.upsert_contact(jid, existing)
                    except Exception:
                        pass
            if updated:
                # Refresh conversation names shown in the UI (debounced —
                # contacts.update can fire in bursts for many contacts at once)
                wx.CallAfter(self.main_window._schedule_set_chats)
        except Exception:
            logging.exception("[WebSocketClient] on_contacts_update error")

    # ── WPPConnect Event Handlers ─────────────────────────────────────────────

    def on_wpp_qrcode(self, data):
        try:
            if not isinstance(data, dict) or not self._belongs_to_this_session(data):
                return
            # WPPConnect emits: {"data": "data:image/png;base64,...", "session": "..."}
            # Ignore events for other sessions (multi-session server scenario,
            # same guard already used in on_wpp_session_logged/on_wpp_status_
            # find below) — observed live: /close-session answered 200 for a
            # stale token, but the old session's Puppeteer browser/QR-polling
            # loop kept running for many more minutes regardless, still
            # emitting fresh qrCode events the whole time. Without this check
            # those reached the SAME on_qrcode_update() the real,
            # already-paired session's own events do, re-triggering the QR
            # dialog (and, once its widgets were destroyed when the user
            # actually finished pairing, an unhandled "wrapped C/C++ object
            # ... has been deleted" RuntimeError on every single stale event
            # after that) long after pairing had genuinely succeeded.
            session = data.get("session", "")
            if session and session != self.instance_name:
                return
            qrcode_base64 = data.get("data")
            if qrcode_base64:
                self.on_qrcode_update({
                    "data": {
                        "qrcode": {
                            "base64": qrcode_base64
                        }
                    }
                })
        except Exception:
            logging.exception("[WebSocketClient] on_wpp_qrcode error")

    def on_wpp_session_logged(self, data):
        try:
            if not isinstance(data, dict):
                return
            status = data.get("status", False)
            session = data.get("session", "")

            # Ignore events for other sessions (multi-session server scenario)
            if session and session != self.instance_name:
                return

            # Notify the connection state immediately (non-blocking).
            self.on_connection_update({
                "data": {
                    "state": "open" if status else "close"
                }
            })

            if status:
                # Fetch host-device JID and raise WA file limits on a background
                # thread so we don't block the Socket.IO event loop.
                threading.Thread(target=self._fetch_host_device_jid, daemon=True).start()
                threading.Thread(target=self._set_wpp_limits, daemon=True).start()
                # WPPConnect does not emit messages.set; trigger sync here instead,
                # using the same guards as on_messages_set to prevent double-sync.
                self.on_messages_set({})
        except Exception:
            logging.exception("[WebSocketClient] on_wpp_session_logged error")

    def _fetch_host_device_jid(self):
        try:
            url = f"{self.main_window.wpp_server}:{self.main_window.wpp_port}/api/{self.main_window.token}/host-device"
            headers = {
                "Authorization": f"Bearer {self.main_window.token}",
                "Content-Type": "application/json",
            }
            res = api_get(url, headers=headers, timeout=5)
            if res.status_code in (200, 201):
                res_data = res.json()
                resp = res_data.get("response", res_data)
                phone_obj = resp.get("phoneNumber", {}) if isinstance(resp, dict) else {}
                wuid = ""
                if isinstance(phone_obj, dict):
                    wuid = phone_obj.get("_serialized", "")
                elif isinstance(phone_obj, str):
                    wuid = phone_obj
                if not wuid and isinstance(resp, dict):
                    wid = resp.get("wid")
                    wuid = wid.get("_serialized", "") if isinstance(wid, dict) else ""
                if wuid:
                    self.main_window.my_jid = wuid
                    wx.CallAfter(self.main_window.resolve_self_lid)
        except Exception:
            logging.exception("[WebSocketClient] Failed to fetch host device JID")

    def _set_wpp_limits(self):
        """Raise WhatsApp Web's effective document file-size limit to 1 GB.

        WA-JS 4.6 marks maxMediaSize as deprecated and its implementation is
        a no-op that rejects values above 70 MB. Media limits now come from
        WhatsApp's MediaGatingUtils, so only maxFileSize remains meaningful.
        """
        mw = self.main_window
        url = f"{mw.wpp_server}:{mw.wpp_port}/api/{mw.token}/set-limit"
        headers = {
            "Authorization": f"Bearer {mw.token}",
            "Content-Type": "application/json",
        }
        try:
            api_post(
                url,
                json={"type": "maxFileSize", "value": 1 * 1024 * 1024 * 1024},
                headers=headers,
                timeout=10,
            )
        except Exception:
            pass

    def on_wpp_status_find(self, data):
        try:
            if not isinstance(data, dict):
                return
            status = data.get("status")
            session = data.get("session")
            logging.info(f"[WebSocketClient] Received status-find: {status}, session: {session}")
            
            # If session is provided in the payload, ignore it if it is not ours
            if session and session != self.instance_name:
                return
                
            if status in ("disconnectedMobile", "notLogged", "UNPAIRED", "UNPAIRED_IDLE"):
                # Handle permanent WhatsApp logout / disconnection.
                # Only trigger if we were previously fully connected (preventing startup false positives).
                if self.main_window._self_inflicted_teardown_expected():
                    # Same self-inflicted-close reasoning as on_connection_update's
                    # "close" branch: never treat this as a real unlink while we
                    # are the ones tearing the session down, whether that's a
                    # full app shutdown or a WPPConnect Server auto-update.
                    pass
                elif self.main_window._wa_connected and self.main_window.settings.get("privateinfo", {}).get("paired"):
                    wx.CallAfter(self._handle_logout)
        except Exception:
            logging.exception("[WebSocketClient] on_wpp_status_find error")

    def on_wpp_phone_code(self, data):
        """Handle the 'phoneCode' Socket.IO event emitted by WPPConnect Server.

        WPPConnect does NOT return the pairing code in the HTTP response of
        /start-session — it emits it asynchronously via Socket.IO.  We store
        the code and set a threading.Event so that on_continue() in connect.py
        can unblock its wait loop and immediately show the pairing dialog.
        """
        try:
            if not isinstance(data, dict) or not self._belongs_to_this_session(data):
                return
            # Same stale-session guard as on_wpp_qrcode — a closed-but-still-
            # running session could otherwise keep feeding a rotated pairing
            # code into a dialog for an account that already finished
            # pairing through a different, current session.
            session = data.get("session", "")
            if session and session != self.instance_name:
                return
            code = data.get("data") or data.get("phoneCode") or ""
            if code:
                # Diagnostic: WPPConnect only emits this event when WhatsApp
                # Web itself fires its internal conn.auth_code_change (see
                # host.layer.js) — there's no client-side timer forcing this.
                # Logged with the previous value + a timestamp so a real
                # pairing session's log file can show definitively whether
                # consecutive events really carry different codes (WhatsApp
                # genuinely rotating it) or the same one repeated (which
                # would point to a bug — none found by reading the code, but
                # worth being able to confirm from a real run instead of
                # just trusting that reading).
                logging.info(
                    "[WebSocketClient] phoneCode event: new=%s previous=%s at %s",
                    code, self._phone_code_value, time.strftime("%H:%M:%S"),
                )
                self._phone_code_value = str(code)
                self._phone_code_event.set()
                # WPPConnect requests a fresh pairing code whenever WhatsApp
                # rotates the auth ref, invalidating the previous one. Refresh
                # the pairing dialog (if open) so the user never types a stale
                # code.
                if self.connect:
                    wx.CallAfter(self.connect.update_pairing_code, str(code))
        except Exception:
            logging.exception("[WebSocketClient] on_wpp_phone_code error")

    def on_wpp_phone_code_error(self, data):
        try:
            if not isinstance(data, dict) or not self._belongs_to_this_session(data):
                return
            name = str(data.get("name") or "Error")
            message = str(data.get("message") or "")
            detail = name if (not message or message == name) else f"{name}: {message}"
            self._phone_code_error = detail
            attempt = data.get("attempt")
            retry_in = data.get("retryInSeconds")
            if attempt and retry_in:
                logging.warning(
                    "[WebSocketClient] pairing-code request failed: %s "
                    "(attempt %s, next retry in %ss)", detail, attempt, retry_in,
                )
            else:
                logging.warning(
                    "[WebSocketClient] pairing-code request failed: %s", detail
                )
            stack = data.get("stack")
            if stack:
                logging.warning(
                    "[WebSocketClient] pairing-code failure stack: %s", stack
                )
            details = data.get("details")
            if details:
                logging.warning(
                    "[WebSocketClient] pairing-code failure details: %r", details
                )
        except Exception:
            logging.exception("[WebSocketClient] on_wpp_phone_code_error error")

    def _belongs_to_this_session(self, info) -> bool:
        if not isinstance(info, dict):
            return True
        sess = info.get("session") or info.get("sessionName") or info.get("instanceName")
        # received-message shape: {response: message} where the server sets
        # message.session = client.session *inside* the response object. Look
        # there too so the guard works even if the top level ever lacks it.
        if not sess:
            resp = info.get("response")
            if isinstance(resp, dict):
                sess = resp.get("session")
        if not sess:
            return True
        sess_clean = str(sess).split(":")[0]
        if not self.instance_name:
            # No identity yet (first pairing): accept everything rather than
            # deadlocking the flow on events whose session we cannot know.
            return True
        if sess_clean != self.instance_name:
            logging.info("[WebSocketClient] Ignored event for session '%s' (this client is '%s')", sess_clean, self.instance_name)
            return False
        return True

    def on_wpp_message_received(self, data):
        try:
            note_live = getattr(self.main_window, "_note_live_wpp_event", None)
            if note_live:
                note_live()
            # Unconditional breadcrumb, logged before any filtering below —
            # reported live: a message shows up in the chat list (unread
            # count bumped) with no toast/sound/local notification at all,
            # only surfacing minutes later once a periodic get_remote_chats()
            # poll catches the stale unread count. This method previously
            # logged nothing on its normal path, so there was no way to tell
            # "the received-message Socket.IO event never arrived at all"
            # apart from "it arrived and was silently filtered somewhere in
            # here" after the fact — both looked identical: nothing in
            # log.log. This line alone answers that: its absence around the
            # time a message went missing means the event never reached this
            # handler in the first place (a WPPConnect/Baileys-side delivery
            # gap, not a WinZapp bug in this file); its presence, combined
            # with which of the checks below actually returns early, pins
            # down exactly where the drop happened instead.
            is_dict = isinstance(data, dict)
            session_ok = is_dict and self._belongs_to_this_session(data)
            has_response = is_dict and bool(data.get("response"))
            logging.info(
                "[WebSocketClient] received-message event: session_ok=%s has_response=%s",
                session_ok, has_response,
            )
            if not session_ok:
                return
            wpp_msg = data.get("response")
            if not wpp_msg:
                return
            normalized = self._normalize_wpp_message(wpp_msg)
            # When this message reached WinZapp, so on_new_message() can say how
            # much of the delay before the screen reader speaks was its own.
            # Kept out of the stored record by prune_message_record()'s caller
            # popping it at the notification point.
            normalized["_arrived_at"] = time.monotonic()
            self.on_messages_upsert({"data": normalized})
        except Exception:
            # A message is dropped entirely if this raises — log the full
            # traceback (not just str(e)) so a future normalization bug is
            # diagnosable from the logs instead of a message just vanishing
            # with no trace of why.
            logging.exception("[WebSocketClient] on_wpp_message_received error")

    def on_media_upload_progress(self, data):
        if not isinstance(data, dict) or not self._belongs_to_this_session(data):
            return
        try:
            upload_id = str(data.get("uploadId") or "")
            progress = float(data.get("progress"))
            if upload_id and 0 <= progress <= 1:
                wx.CallAfter(self.main_window.on_media_upload_progress, upload_id, progress)
        except (TypeError, ValueError):
            return

    def on_wpp_reaction(self, data):
        try:
            if not isinstance(data, dict) or not self._belongs_to_this_session(data):
                return
            payload = data.get("response") if isinstance(data.get("response"), dict) else data
            emoji = (payload.get("reactionText") or payload.get("text") or "").strip()
            target_serialized = payload.get("msgId")
            if isinstance(target_serialized, dict):
                target_serialized = target_serialized.get("_serialized", "")
            reaction_serialized = payload.get("id")
            if isinstance(reaction_serialized, dict):
                reaction_serialized = reaction_serialized.get("_serialized", "")
            if not target_serialized:
                return

            def _split(serialized):
                parts = str(serialized).split("_")
                from_me = parts[0] == "true"
                chat = self._clean_jid(parts[1]) if len(parts) > 1 else ""
                clean_id = parts[2] if len(parts) > 2 else (parts[-1] if parts else "")
                participant = self._clean_jid(parts[3]) if len(parts) > 3 else ""
                return from_me, chat, clean_id, participant

            target_from_me, chat_jid, target_id, _ = _split(target_serialized)
            reactor_from_me, r_chat, reaction_id, reactor_participant = _split(
                reaction_serialized or ""
            )
            if not chat_jid:
                chat_jid = r_chat

            normalized = {
                "key": {
                    "remoteJid": chat_jid,
                    "fromMe": reactor_from_me,
                    "id": reaction_id,
                },
                "pushName": "",
                "message": {
                    "reactionMessage": {
                        "text": emoji,
                        "key": {
                            "id": target_id,
                            "fromMe": target_from_me,
                            "remoteJid": chat_jid,
                        },
                    }
                },
                "messageType": "reactionMessage",
                "messageTimestamp": (payload.get("timestamp") // 1000 if (payload.get("timestamp") or 0) > 1_000_000_000_000 else (payload.get("timestamp") or int(time.time()))),
            }
            if reactor_participant:
                normalized["key"]["participant"] = reactor_participant
            if reactor_from_me and self._consume_own_reaction_echo(normalized):
                return
            wx.CallAfter(self.main_window.on_new_message, normalized)
        except Exception:
            logging.exception("[WebSocketClient] on_wpp_reaction error")

    @staticmethod
    def _call_timestamp_seconds(value):
        """Normalize WhatsApp call timestamps to epoch seconds.

        The public IncomingCall contract uses seconds, but internal CallStore
        models and custom bridges may expose milliseconds or microseconds.
        Invalid/relative values intentionally become zero so they cannot be
        mistaken for a trustworthy session-age signal.
        """
        if isinstance(value, bool):
            return 0
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return 0
        if numeric >= 1_000_000_000_000_000:
            numeric /= 1_000_000
        elif numeric >= 1_000_000_000_000:
            numeric /= 1_000
        if numeric < 1_000_000_000:
            return 0
        return int(numeric)

    def on_wpp_incoming_call(self, data):
        """Forward WPPConnect call lifecycle events to the wx main thread."""
        try:
            if not isinstance(data, dict) or not self._belongs_to_this_session(data):
                return
            payload = data.get("data") if isinstance(data.get("data"), dict) else data
            call_timestamp = self._call_timestamp_seconds(
                payload.get("timestamp")
                or payload.get("offerTime")
                or payload.get("t")
                or payload.get("startTime")
            )
            observed_at = self._call_timestamp_seconds(payload.get("observedAt"))
            normalized = {
                "event": str(payload.get("event") or payload.get("status") or "offer"),
                "state": str(payload.get("state") or ""),
                "id": str(payload.get("id") or ""),
                "peerJid": self._clean_jid(
                    payload.get("peerJid") or payload.get("sender") or payload.get("from")
                ),
                "groupJid": self._clean_jid(payload.get("groupJid")),
                "isVideo": bool(payload.get("isVideo", False)),
                "isGroup": bool(payload.get("isGroup", False)),
                "timestamp": call_timestamp,
                "observedAt": observed_at,
                "receivedWhileOffline": bool(
                    payload.get("offerReceivedWhileOffline", False)
                ),
            }
            if not normalized["id"] and not normalized["peerJid"]:
                logging.warning("[WebSocketClient] incomingcall without id or peer: %r", data)
                return
            logging.info("[WebSocketClient] incomingcall event: %r", normalized)
            wx.CallAfter(self.main_window.on_incoming_call_event, normalized)
        except Exception:
            logging.exception("[WebSocketClient] on_wpp_incoming_call error")

    def on_wpp_ack(self, data):
        try:
            if not isinstance(data, dict) or not self._belongs_to_this_session(data):
                return
            note_live = getattr(self.main_window, "_note_live_wpp_event", None)
            if note_live:
                note_live()
            wpp_ack = data.get("ack")
            status = ack_to_status(wpp_ack)
            if status is None:
                logging.warning("[WebSocketClient] on_wpp_ack: unrecognised ack %r — "
                                "leaving the message status untouched", wpp_ack)
                return
            msg_id = data.get("id", {}).get("_serialized") if isinstance(data.get("id"), dict) else data.get("id")
            parts = msg_id.split("_") if msg_id else []
            clean_id = parts[2] if len(parts) > 2 else (parts[-1] if parts else msg_id)
            if not clean_id:
                logging.warning("[WebSocketClient] on_wpp_ack: ack %r with no usable message id "
                                "(raw id=%r) — dropping", wpp_ack, data.get("id"))
                return

            remote_jid = data.get("to")
            if not remote_jid and isinstance(data.get("id"), dict):
                remote_jid = data.get("id", {}).get("remote")
            if not remote_jid and len(parts) > 1:
                remote_jid = parts[1]
            if remote_jid:
                remote_jid = self._clean_jid(remote_jid)

            self.on_messages_update({
                "data": {
                    "key": {
                        "id": clean_id,
                        "remoteJid": remote_jid or "",
                        "fromMe": True
                    },
                    "update": {
                        "status": status
                    }
                }
            })
        except Exception:
            logging.exception("[WebSocketClient] on_wpp_ack error")

    def _normalize_wpp_message(self, wpp_msg):
        # Everything below reads this payload with .get()/defaults, which is
        # the only way to survive WhatsApp Web — but it also means a field that
        # stops arriving looks exactly like one that was never set. Checked
        # here, on the way in, so the log names the field instead of leaving a
        # symptom to be diagnosed three layers away. Observation only: the
        # payload is returned untouched and nothing below changes behaviour.
        observe_payload([wpp_msg], "get-messages", where="wpp message")
        msg_id = wpp_msg.get("id")
        if isinstance(msg_id, dict):
            msg_id = msg_id.get("_serialized", "")
        elif not isinstance(msg_id, str):
            msg_id = ""

        parts = msg_id.split("_") if msg_id else []
        clean_id = parts[2] if len(parts) > 2 else (parts[-1] if parts else msg_id)

        from_jid = wpp_msg.get("from", "")
        to_jid = wpp_msg.get("to", "")

        # Safely parse fromMe supporting boolean, string representation, or ID prefix fallback
        from_me_val = wpp_msg.get("fromMe")
        if from_me_val is not None:
            if isinstance(from_me_val, bool):
                from_me = from_me_val
            else:
                from_me = (str(from_me_val).lower() == "true")
        else:
            from_me = (parts[0] == "true") if parts else False

        # Detect status/story messages: WPPConnect sends them with to="status@broadcast"
        # or sets isStatus=True.  The real sender is in the "from" field.
        is_status = "broadcast" in (to_jid or "") or wpp_msg.get("isStatus", False)

        if is_status:
            remote_jid = "status@broadcast"
            status_participant = self._clean_jid(from_jid)
        else:
            key_remote = ""
            wpp_id_obj = wpp_msg.get("id")
            if isinstance(wpp_id_obj, dict):
                key_remote = wpp_id_obj.get("remote", "")
            if not key_remote and len(parts) > 1:
                key_remote = parts[1]

            if key_remote:
                remote_jid = self._clean_jid(key_remote)
            else:
                remote_jid = self._clean_jid(to_jid if from_me else from_jid)
            status_participant = ""

        ts = wpp_msg.get("timestamp") or wpp_msg.get("t", int(time.time()))
        if ts > 1_000_000_000_000:
            ts //= 1000

        msg_type = wpp_msg.get("type", "chat")
        conversation = wpp_msg.get("body", "") or wpp_msg.get("text", "")

        # WPPConnect's own message model flattens WhatsApp's OG link-preview
        # metadata (title/description/canonicalUrl — same proto fields
        # sender.layer.js's own sendLinkPreview() writes on the way out)
        # straight onto the raw message object rather than nesting it, and
        # most link-preview text still arrives with type "chat" like any
        # other plain text message — not "extendedText". Promoting those to
        # extendedTextMessage (like a real quote/mention already does further
        # down) is what makes _get_message_content() render the preview at
        # all; a "chat" message never carries anything but conversation text.
        _link_preview_title = wpp_msg.get("title") or ""
        _link_preview_description = wpp_msg.get("description") or ""
        _link_preview_url = wpp_msg.get("canonicalUrl") or wpp_msg.get("matchedText") or ""
        _has_link_preview = bool(_link_preview_title or _link_preview_description)

        def _with_link_preview(ext):
            if _has_link_preview:
                ext["title"] = _link_preview_title
                ext["description"] = _link_preview_description
                if _link_preview_url:
                    ext["canonicalUrl"] = _link_preview_url
            return ext

        def _safe_media_key(val):
            if not val:
                return ""
            if isinstance(val, (bytes, bytearray)):
                import base64
                return base64.b64encode(val).decode("utf-8")
            if isinstance(val, dict) and "data" in val:
                return val
            if isinstance(val, str):
                return val
            return ""

        message_content = {}
        _promoted_to_extended_text = False
        if msg_type == "chat":
            if _has_link_preview:
                message_content = {
                    "extendedTextMessage": _with_link_preview({"text": conversation})
                }
                _promoted_to_extended_text = True
            else:
                message_content = {"conversation": conversation}
        elif msg_type == "extendedText":
            message_content = {
                "extendedTextMessage": _with_link_preview({"text": conversation})
            }
        elif msg_type in ("audio", "ptt"):
            media_data = wpp_msg.get("mediaData") if isinstance(wpp_msg.get("mediaData"), dict) else {}
            seconds_val = _media_seconds(wpp_msg)

            # Keep the original audio metadata.  Save As resolves the filename
            # extension from these values; dropping them here made every
            # received non-PTT audio with no local filename fall through to
            # the old hard-coded .mp3 fallback even when WPPConnect reported
            # audio/ogg, audio/mp4 (M4A), audio/aac, audio/wav, etc.
            audio_mimetype = wpp_msg.get("mimetype") or media_data.get("mimetype") or ""
            audio_file_name = (
                wpp_msg.get("filename") or wpp_msg.get("fileName") or wpp_msg.get("title")
                or media_data.get("filename") or media_data.get("fileName") or media_data.get("title")
                or ""
            )
            audio_message = {
                "url": wpp_msg.get("clientUrl", ""),
                "seconds": seconds_val,
                "mediaKey": _safe_media_key(wpp_msg.get("mediaKey")),
            }
            if audio_mimetype:
                audio_message["mimetype"] = audio_mimetype
            if audio_file_name:
                audio_message["fileName"] = audio_file_name

            message_content = {"audioMessage": audio_message}
            # Preserve the PTT/voice-note flag: WPPConnect reports voice notes
            # with type="ptt", but that raw type is later mapped to
            # "audioMessage" (see type_mapping below) and the flag would be
            # silently dropped. ConversationsPanel._is_voice_message() decides
            # whether an audio chains into the sequential PTT playback (and
            # how it is labelled/saved) from this very flag — without it,
            # received voice notes never chained, while own recordings (whose
            # virtual message always carries ptt=True) did.
            is_ptt = (
                msg_type == "ptt"
                or bool(wpp_msg.get("isPtt"))
                or bool((wpp_msg.get("mediaData") or {}).get("ptt"))
            )
            if is_ptt:
                message_content["audioMessage"]["ptt"] = True
        elif msg_type == "image":
            # NOTE: do NOT fall back to wpp_msg["body"] for the caption — for
            # media messages WPPConnect puts the base64 JPEG thumbnail in `body`,
            # which then showed up as raw base64 instead of the caption.
            img_caption = wpp_msg.get("caption", "") or ""
            if looks_like_binary_blob(img_caption):
                img_caption = ""
            message_content = {
                "imageMessage": {
                    "caption": img_caption,
                    "url": wpp_msg.get("clientUrl", ""),
                    "mimetype": wpp_msg.get("mimetype", "image/jpeg"),
                    "mediaKey": _safe_media_key(wpp_msg.get("mediaKey"))
                }
            }
        elif msg_type == "video":
            seconds_val = _media_seconds(wpp_msg)
            vid_caption = wpp_msg.get("caption", "") or ""
            if looks_like_binary_blob(vid_caption):
                vid_caption = ""
            message_content = {
                "videoMessage": {
                    "caption": vid_caption,
                    "seconds": seconds_val,
                    "gifPlayback": wpp_msg.get("isGif", False) or wpp_msg.get("gifPlayback", False),
                    "url": wpp_msg.get("clientUrl", ""),
                    "mimetype": wpp_msg.get("mimetype", "video/mp4"),
                    "mediaKey": _safe_media_key(wpp_msg.get("mediaKey"))
                }
            }
        elif msg_type == "document":
            # A normal send/receive populates the top-level convenience
            # fields (filename/size) WPPConnect's wa-js model sets on the
            # raw message object. A FORWARDED document doesn't get those
            # top-level fields populated the same way — only the nested
            # mediaData carries the real name/size — so without this
            # fallback (mirroring the audio/video branches above, which
            # already fall back into mediaData for duration) every forwarded
            # document rendered as the generic "Document" placeholder with a
            # 0-byte size.
            media_data = wpp_msg.get("mediaData") if isinstance(wpp_msg.get("mediaData"), dict) else {}
            doc_file_name = (
                wpp_msg.get("filename") or wpp_msg.get("fileName") or wpp_msg.get("title")
                or media_data.get("filename") or media_data.get("fileName") or media_data.get("title")
                or "Document"
            )
            doc_file_length = (
                wpp_msg.get("size") or wpp_msg.get("fileLength")
                or media_data.get("size") or media_data.get("fileLength")
                or 0
            )
            # Like image/video, WPPConnect exposes the real caption in the
            # top-level caption field. `body` may be binary thumbnail data and
            # must not be used as a fallback.
            doc_caption = wpp_msg.get("caption", "") or ""
            if looks_like_binary_blob(doc_caption):
                doc_caption = ""
            message_content = {
                "documentMessage": {
                    "caption": doc_caption,
                    "fileName": doc_file_name,
                    "fileLength": doc_file_length,
                    "url": wpp_msg.get("clientUrl", ""),
                    "mimetype": wpp_msg.get("mimetype") or media_data.get("mimetype", ""),
                    "mediaKey": _safe_media_key(wpp_msg.get("mediaKey"))
                }
            }
        elif msg_type == "sticker":
            message_content = {
                "stickerMessage": {
                    "url": wpp_msg.get("clientUrl", ""),
                    "mimetype": wpp_msg.get("mimetype", "image/webp"),
                    "mediaKey": _safe_media_key(wpp_msg.get("mediaKey"))
                }
            }
        elif msg_type in ("location", "liveLocation"):
            # main.py/conversations.py both already handle locationMessage /
            # liveLocationMessage (rendered as a static "📍 Localização" bubble
            # today, coordinates kept here for when that changes) — this type
            # had no branch here at all, so a shared live location silently
            # fell through with no message_content and no matching entry in
            # type_mapping below, meaning it rendered as nothing.
            loc_key = "liveLocationMessage" if msg_type == "liveLocation" else "locationMessage"
            message_content = {
                loc_key: {
                    "degreesLatitude": wpp_msg.get("lat"),
                    "degreesLongitude": wpp_msg.get("lng"),
                    "name": wpp_msg.get("loc") or wpp_msg.get("body") or "",
                }
            }
        elif msg_type == "vcard":
            message_content = {
                "contactMessage": {
                    "displayName": wpp_msg.get("displayName") or wpp_msg.get("body") or "Contato",
                }
            }
        elif msg_type == "pollCreation":
            message_content = {
                "pollCreationMessage": {
                    "name": wpp_msg.get("pollName") or wpp_msg.get("body") or ""
                }
            }
        elif msg_type == "buttons":
            message_content = {
                "buttonsMessage": {}
            }
        elif msg_type == "list":
            message_content = {
                "listMessage": {}
            }
        elif msg_type == "template":
            message_content = {
                "templateMessage": {}
            }
        elif msg_type == "revoked":
            message_content = {
                "protocolMessage": {
                    "type": 3
                }
            }
        elif msg_type == "gp2":
            # Group membership/settings notifications (join, leave, removed,
            # promoted, subject/description/picture change, …). WPPConnect
            # carries the specific action in "subtype" and the affected
            # participants in "recipients".
            sender_obj = wpp_msg.get("sender")
            author_raw = wpp_msg.get("author") or (
                sender_obj.get("id", "") if isinstance(sender_obj, dict) else ""
            )
            # "recipients" entries can be raw JID strings or WPPConnect Wid
            # objects ({"server":..., "user":..., "_serialized":...}) — always
            # normalize to plain strings here so downstream UI code (which
            # expects to call string methods like .endswith() on each one)
            # never has to guard against a dict slipping through.
            raw_recipients = wpp_msg.get("recipients") or []
            clean_recipients = [
                self._clean_jid(r) for r in raw_recipients if self._clean_jid(r)
            ]
            # "body" carries the payload of the change — the new group name for
            # a subject change, the new description for a description change,
            # or the on/off value for a settings change. Dropping it (as this
            # did) is why those events could only be rendered as a vague
            # "group update" with no indication of what was actually changed.
            message_content = {
                "groupNotification": {
                    "subtype": wpp_msg.get("subtype", ""),
                    "recipients": clean_recipients,
                    "author": self._clean_jid(author_raw) if author_raw else "",
                    "body": wpp_msg.get("body") or wpp_msg.get("subject") or "",
                    "value": wpp_msg.get("value"),
                }
            }

        # Fallback to plain text if the message type is unsupported/unmapped but contains body text.
        # Excludes a bare JID: WPPConnect's raw payload for internal
        # notification-only events (observed for a contact's security-code/
        # E2E-identity-change notice, "type" unmapped above and no real
        # message content at all) stuffs the JID of whoever triggered it
        # into this same "body" field — treating that as chat text leaked
        # the contact's raw @lid into the conversation as if they had sent
        # it as a message. Leaving message_content empty here instead falls
        # through to the unmapped-type handling below, which is already
        # correctly excluded from display/unread/notifications elsewhere
        # (see is_countable_message()'s docstring in main.py).
        if not message_content and conversation and not looks_like_jid(conversation):
            msg_type = "chat"
            message_content = {"conversation": conversation}

        type_mapping = {
            "chat": "conversation",
            "audio": "audioMessage",
            "ptt": "audioMessage",
            "image": "imageMessage",
            "video": "videoMessage",
            "document": "documentMessage",
            "sticker": "stickerMessage",
            "vcard": "contactMessage",
            "pollCreation": "pollCreationMessage",
            "buttons": "buttonsMessage",
            "list": "listMessage",
            "template": "templateMessage",
            "revoked": "protocolMessage",
            "extendedText": "extendedTextMessage",
            "gp2": "groupNotification",
            "location": "locationMessage",
            "liveLocation": "liveLocationMessage",
        }
        mapped_type = type_mapping.get(msg_type, msg_type)
        if _promoted_to_extended_text:
            mapped_type = "extendedTextMessage"

        ack = wpp_msg.get("ack")
        message_updates = []
        if ack is not None:
            # Same translation as on_wpp_ack — a negative ack here means the
            # send failed and must not be passed through raw (it rendered as no
            # status at all, indistinguishable from a message still in flight).
            mapped_status = ack_to_status(ack)
            if mapped_status is not None:
                message_updates.append({"status": str(mapped_status)})

        normalized = {
            "key": {
                "remoteJid": remote_jid,
                "fromMe": from_me,
                "id": clean_id
            },
            "pushName": (wpp_msg.get("sender") or {}).get("pushname") or wpp_msg.get("notifyName") or "",
            "message": message_content,
            "messageTimestamp": ts,
            "messageType": mapped_type,
            "MessageUpdate": message_updates
        }

        # Status messages: include the real sender as participant
        if status_participant:
            normalized["key"]["participant"] = status_participant

        participant = (
            wpp_msg.get("author")
            or wpp_msg.get("participant")
            or (wpp_msg.get("key") or {}).get("participant")
            or (wpp_msg.get("sender") or {}).get("id")
            or ""
        )
        if participant:
            normalized["key"]["participant"] = self._clean_jid(participant)

        quoted_msg = wpp_msg.get("quotedMsg")
        quoted_msg_obj = wpp_msg.get("quotedMsgObj")
        quoted_stanza_id = wpp_msg.get("quotedStanzaID") or wpp_msg.get("quotedStanzaId")
        quoted_participant = wpp_msg.get("quotedParticipant")

        # Fallback to WPPConnect/Baileys contextInfo if WPPConnect quote fields are missing
        ctx_info = wpp_msg.get("contextInfo")
        if not ctx_info and isinstance(wpp_msg.get("message"), dict):
            sub_msg = wpp_msg.get("message")
            for sub_key in ("extendedTextMessage", "imageMessage", "videoMessage", "audioMessage", "documentMessage"):
                if isinstance(sub_msg.get(sub_key), dict):
                    ctx_info = sub_msg[sub_key].get("contextInfo")
                    if ctx_info:
                        break
        # isForwarded is a real WhatsApp protocol field (contextInfo.isForwarded
        # in the raw proto; WPPConnect's own Message model also exposes it as a
        # convenience top-level boolean — see node_modules/@wppconnect-team/
        # wppconnect's message.d.ts) present for ANY message that was forwarded,
        # from anyone, not just ones this app itself forwarded. It used to be
        # silently dropped here since this function only ever built a
        # contextInfo dict when there was a quote or a mention.
        is_forwarded = bool(wpp_msg.get("isForwarded"))
        if isinstance(ctx_info, dict):
            if not quoted_stanza_id:
                quoted_stanza_id = ctx_info.get("stanzaId")
            if not quoted_participant:
                quoted_participant = ctx_info.get("participant")
            if not quoted_msg:
                quoted_msg = ctx_info.get("quotedMessage")
            if not is_forwarded:
                is_forwarded = bool(ctx_info.get("isForwarded"))


        # Determine if there is any quoted context
        has_quote = False
        clean_quoted_id = ""
        participant_jid = ""
        quoted_body = ""

        # 1. Start with the top-level keys which are the most reliable in WPPConnect
        if quoted_stanza_id:
            has_quote = True
            clean_quoted_id = quoted_stanza_id
            if isinstance(clean_quoted_id, str) and "_" in clean_quoted_id:
                parts = clean_quoted_id.split("_")
                clean_quoted_id = parts[2] if len(parts) > 2 else parts[-1]

        if quoted_participant:
            has_quote = True
            participant_jid = self._clean_jid(quoted_participant)

        # 2. Extract content from quotedMsg (dictionary or string)
        if isinstance(quoted_msg, dict):
            has_quote = True
            if not quoted_body:
                quoted_body = (
                    quoted_msg.get("body")
                    or quoted_msg.get("caption")
                    or quoted_msg.get("conversation")
                    or (quoted_msg.get("extendedTextMessage") or {}).get("text")
                    or ""
                )
            
            # Fallbacks if top-level fields were missing
            if not clean_quoted_id:
                quoted_id = quoted_msg.get("id")
                if isinstance(quoted_id, dict):
                    quoted_id = quoted_id.get("_serialized", "")
                if quoted_id:
                    parts = quoted_id.split("_")
                    clean_quoted_id = parts[2] if len(parts) > 2 else parts[-1]
            
            if not participant_jid:
                author = quoted_msg.get("author") or (quoted_msg.get("sender") or {}).get("id") or ""
                if author:
                    # author (and .sender.id) can be a raw WPPConnect Wid
                    # object ({"server":..., "user":..., "_serialized":...})
                    # rather than a plain string — the sibling quotedMsgObj
                    # branch below already accounts for this via _clean_jid();
                    # calling .replace() directly here raised AttributeError,
                    # which _normalize_wpp_message's only caller swallows with
                    # a bare `except Exception: print(...)` — silently
                    # dropping the entire live message, not just its quote.
                    participant_jid = self._clean_jid(author)

        elif isinstance(quoted_msg, str) and quoted_msg:
            has_quote = True
            if not clean_quoted_id:
                clean_quoted_id = quoted_msg
                if "_" in clean_quoted_id:
                    parts = clean_quoted_id.split("_")
                    clean_quoted_id = parts[2] if len(parts) > 2 else parts[-1]

        # 3. Extract content from quotedMsgObj (alternative dictionary)
        if isinstance(quoted_msg_obj, dict):
            has_quote = True
            if not quoted_body:
                quoted_body = (
                    quoted_msg_obj.get("body")
                    or quoted_msg_obj.get("caption")
                    or quoted_msg_obj.get("conversation")
                    or (quoted_msg_obj.get("extendedTextMessage") or {}).get("text")
                    or ""
                )
            
            if not clean_quoted_id:
                quoted_id = quoted_msg_obj.get("id")
                if isinstance(quoted_id, dict):
                    quoted_id = quoted_id.get("_serialized", "")
                if quoted_id:
                    parts = quoted_id.split("_")
                    clean_quoted_id = parts[2] if len(parts) > 2 else parts[-1]
            
            if not participant_jid:
                author = quoted_msg_obj.get("author") or (quoted_msg_obj.get("sender") or {}).get("id") or ""
                if author:
                    participant_jid = self._clean_jid(author)

        wpp_mentioned = wpp_msg.get("mentionedJidList") or []
        mentioned_jids = [
            self._clean_jid(m)
            for m in wpp_mentioned
            if m
        ]

        if has_quote or mentioned_jids or is_forwarded:
            # Store only a slim quoted preview — never the full quoted message,
            # whose thumbnail/mediaKey/directPath/hashes bloat messages.dat and
            # slow conversation loading without ever being read by the UI.
            if isinstance(quoted_msg, dict):
                quoted_msg_payload = _slim_quoted_message(quoted_msg)
            else:
                quoted_msg_payload = {"conversation": (quoted_body or "")[:300]}
            context_info = {}
            if has_quote:
                context_info["stanzaId"] = clean_quoted_id
                context_info["participant"] = participant_jid
                context_info["quotedMessage"] = quoted_msg_payload
            if mentioned_jids:
                context_info["mentionedJid"] = mentioned_jids
            if is_forwarded:
                context_info["isForwarded"] = True

            # If msg_type is conversation, promote it to extendedTextMessage
            if mapped_type == "conversation":
                mapped_type = "extendedTextMessage"
                normalized["messageType"] = "extendedTextMessage"
                normalized["message"] = {
                    "extendedTextMessage": {
                        "text": conversation,
                        "contextInfo": context_info
                    }
                }
            else:
                # Put under specific sub-keys (e.g. imageMessage, videoMessage) if they exist
                for sub_key in (
                    "extendedTextMessage", "imageMessage", "videoMessage", "audioMessage",
                    "documentMessage", "stickerMessage", "locationMessage", "contactMessage"
                ):
                    if sub_key in normalized["message"] and isinstance(normalized["message"][sub_key], dict):
                        normalized["message"][sub_key]["contextInfo"] = context_info

        return normalized
