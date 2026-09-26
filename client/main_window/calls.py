"""CallsMixin — part of MainWindow (see main_window/__init__.py).

Moved verbatim out of main.py. Methods run with ``self`` bound to the
MainWindow instance, so every attribute set in MainWindow.__init__ is
available here.
"""

import io
import logging
import threading
import time
import wx
from urllib.parse import quote as _url_quote
from core.call_logic import (
    active_call_label_key,
    incoming_call_can_answer,
)
from core.api_client import (
    api_get,
    api_post,
)
from core.call_matching import call_event_matches_active
from core.call_log import (
    call_log_candidate_ids,
    call_log_refresh_delays,
    is_call_log,
    is_call_log_pending,
    refile_call_log,
)
from core.audio_devices import enumerate_input_devices


class CallsMixin:
    """Voice and video calls: incoming-call alerts, call audio/camera, the call
    bar, call control requests and the call-log watcher.
    """

    # ── Incoming real-time messages ───────────────────────────────────────────

    # `3` and `8` are WhatsApp Web's newer numeric states ReceivedCall and
    # ReceivedCallWithoutOffer. Keep them here as a defensive fallback even
    # though the Node bridge normally converts both to INCOMING_RING.
    _CALL_RINGING_STATES = frozenset({
        "", "3", "8", "OFFER", "INCOMING_RING", "RINGING",
    })
    _CALL_ALERT_MAX_SECONDS = 120
    # A call may start a few seconds before the local socket/listener finishes
    # connecting. Keep that startup race valid, but never alert for an offer
    # whose WhatsApp timestamp clearly predates this WinZapp session.
    _CALL_EVENT_START_GRACE_SECONDS = 5

    def _cancel_incoming_call_watchdog(self, identity: str):
        timer = self._incoming_call_watchdogs.pop(identity, None)
        if timer is not None:
            timer.cancel()

    def _arm_incoming_call_watchdog(self, identity: str):
        """Guarantee a lost WPP lifecycle event cannot leave audio looping."""
        self._cancel_incoming_call_watchdog(identity)
        timer = threading.Timer(
            self._CALL_ALERT_MAX_SECONDS,
            lambda: wx.CallAfter(self._expire_incoming_call_alert, identity),
        )
        timer.daemon = True
        self._incoming_call_watchdogs[identity] = timer
        timer.start()

    def _expire_incoming_call_alert(self, identity: str):
        self._cancel_incoming_call_watchdog(identity)
        if self._active_incoming_calls.pop(identity, None) is None:
            return
        close_dialog = getattr(self, "_close_incoming_call_dialog", None)
        if close_dialog is not None:
            close_dialog(identity)
        logging.warning("[incoming_call] lifecycle timeout id=%s", identity)
        getattr(self, "_incoming_call_details", {}).pop(identity, None)
        if not self._active_incoming_calls:
            if hasattr(self, "call_incoming_sound"):
                self.call_incoming_sound.stop()
        # Keyed to an ANSWERABLE call, not to "any alert is still up" -- see
        # stop_incoming_call_alert(). Group offers sit in the dictionary too
        # and never open a monitor, so the old emptiness test held the speaker
        # for as long as one kept ringing beside the expired one-to-one call.
        if not self._has_answerable_incoming_call():
            self._stop_incoming_call_audio_monitor()
        self._sync_incoming_call_bar()

    def _close_incoming_call_dialog(self, identity: str):
        dialog = getattr(self, "_incoming_call_dialogs", {}).pop(identity, None)
        if dialog is not None:
            try:
                dialog.close_from_call_lifecycle()
            except Exception:
                logging.exception("[incoming_call] could not close popup id=%s", identity)

    def _forget_incoming_call_dialog(self, identity: str):
        """Forget a popup and keep the call controls available in WinZapp."""
        getattr(self, "_incoming_call_dialogs", {}).pop(identity, None)
        self._sync_incoming_call_bar()

    def _show_incoming_call_dialog(self, identity: str, message: str):
        from ui.dialogs.incoming_call import IncomingCallDialog

        # Keep the call surface visibly inside WinZapp.  When the application
        # is hidden in the tray, restoring its main frame first prevents the
        # dialog from looking like an unrelated floating Windows popup.
        if getattr(self, "_window_hidden", False):
            self.restore_window()
        self._close_incoming_call_dialog(identity)
        details = getattr(self, "_incoming_call_details", {}).get(identity, {})
        can_answer = incoming_call_can_answer(details)
        is_video = bool(details.get("is_video"))
        dialog = IncomingCallDialog(
            self,
            message,
            on_answer=lambda: self.accept_incoming_call(identity),
            on_reject=lambda: self.reject_incoming_call(identity),
            on_stop=lambda: self.stop_incoming_call_alert(identity),
            on_closed=lambda: self._forget_incoming_call_dialog(identity),
            can_answer=can_answer,
            is_video=is_video,
            on_answer_without_video=(
                lambda: self.accept_incoming_call(identity, with_video=False)
            ),
        )
        self._incoming_call_dialogs[identity] = dialog
        dialog.show_accessibly()

    # A voice call is the one thing in WinZapp that wants the network and the
    # CPU to itself, so the *recurring* background work stands down while one
    # is up. Bounded on purpose, for the reason in _voice_call_in_progress().
    _VOICE_CALL_PAUSE_MAX_SECONDS = 2 * 60 * 60

    def _voice_call_in_progress(self) -> bool:
        """Is a voice call up, for the purpose of standing background work down?

        Bounded deliberately. ``_active_voice_call`` is cleared by a terminal
        ``callstate`` event or by hanging up inside WinZapp; a call torn down on
        the phone in a way that produces neither would otherwise pause the
        periodic poll and the history backfill for the rest of the session —
        and a paused sync is invisible, the user simply stops receiving
        messages. The clock starts the first time this is asked rather than
        where the call is registered, because that flag is assigned from four
        separate places and one of them forgetting to stamp it is exactly the
        silent failure this bound exists to prevent.
        """
        if not getattr(self, "_active_voice_call", None):
            self._voice_call_pause_since = 0.0
            return False
        since = getattr(self, "_voice_call_pause_since", 0.0) or 0.0
        now = time.monotonic()
        if not since:
            self._voice_call_pause_since = now
            return True
        return (now - since) <= self._VOICE_CALL_PAUSE_MAX_SECONDS

    def _first_incoming_call_identity(self) -> str:
        calls = getattr(self, "_active_incoming_calls", {})
        return next(iter(calls), "")

    def _on_answer_incoming_call_bar(self, _event=None):
        """Answer the first visible incoming call from the in-window bar."""
        identity = self._first_incoming_call_identity()
        if identity:
            self.accept_incoming_call(identity)

    def _on_reject_incoming_call_bar(self, _event=None):
        """Reject the first visible incoming call from the in-window bar."""
        identity = self._first_incoming_call_identity()
        if identity:
            self.reject_incoming_call(identity)

    def _on_stop_incoming_call_bar(self, _event=None):
        """Handle the native in-window silence-alert button."""
        self.stop_all_incoming_call_alerts()

    def _sync_incoming_call_bar(self, message: str = ""):
        """Show the local stop control while a non-popup call alert is active."""
        bar = getattr(self, "incoming_call_bar", None)
        label = getattr(self, "incoming_call_label", None)
        if bar is None or label is None:
            return

        calls = getattr(self, "_active_incoming_calls", {})
        call_settings = getattr(self, "settings", {}).get("calls", {})
        dialogs = getattr(self, "_incoming_call_dialogs", {})
        should_show = bool(calls) and (
            not call_settings.get("popup_enabled", True) or not dialogs
        )
        if should_show:
            identity = self._first_incoming_call_identity()
            details = getattr(self, "_incoming_call_details", {}).get(identity, {})
            if not message:
                message = str(details.get("message") or "")
            if message:
                label.SetLabel(message)
            answer = getattr(self, "incoming_call_answer_button", None)
            if answer is not None:
                answer.Enable(incoming_call_can_answer(details))
            bar.Show()
        else:
            bar.Hide()
        self.Layout()

    def stop_all_incoming_call_alerts(self):
        """Stop local call UI/audio, without claiming WhatsApp rejected a call."""
        for identity in list(getattr(self, "_incoming_call_watchdogs", {})):
            self._cancel_incoming_call_watchdog(identity)
        for identity in list(getattr(self, "_incoming_call_dialogs", {})):
            self._close_incoming_call_dialog(identity)
        self._active_incoming_calls.clear()
        getattr(self, "_incoming_call_details", {}).clear()
        if hasattr(self, "call_incoming_sound"):
            self.call_incoming_sound.stop()
        self._stop_incoming_call_audio_monitor()
        self._sync_incoming_call_bar()

    def stop_incoming_call_alert(
        self, identity: str, *, keep_audio_monitor: bool = False
    ):
        """Stop one incoming-call alert locally; the phone keeps ringing."""
        self._active_incoming_calls.pop(identity, None)
        getattr(self, "_incoming_call_details", {}).pop(identity, None)
        self._cancel_incoming_call_watchdog(identity)
        self._close_incoming_call_dialog(identity)
        if not self._active_incoming_calls:
            if hasattr(self, "call_incoming_sound"):
                self.call_incoming_sound.stop()
        # The receive-only monitor is keyed to a call that can actually be
        # ANSWERED, not to "any alert is still up". Group offers now sit in
        # _active_incoming_calls too (they are announced, just not answerable),
        # and they never start a monitor -- so gating this on the dictionary
        # being empty left the speaker held open after the one-to-one call was
        # dismissed, for as long as a group offer kept ringing beside it.
        if not keep_audio_monitor and not self._has_answerable_incoming_call():
            self._stop_incoming_call_audio_monitor()
        self._sync_incoming_call_bar()

    def _has_answerable_incoming_call(self) -> bool:
        """Whether any still-ringing offer could be accepted (i.e. not a group)."""
        details_map = getattr(self, "_incoming_call_details", {})
        return any(
            incoming_call_can_answer(details_map.get(identity))
            for identity in self._active_incoming_calls
        )

    def _call_control_payload(self, identity: str) -> dict:
        details = getattr(self, "_incoming_call_details", {}).get(identity, {})
        call_id = str(details.get("call_id") or "")
        if call_id:
            return {"callId": call_id}
        # When only a peer JID is known, leave the payload empty.  WA-JS/WhatsApp
        # can then act on the native active call instead of receiving a contact
        # JID where a call id is expected.
        return {}

    def _post_call_control(self, endpoint: str, payload: dict, *, timeout: float = 15):
        url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/call/{endpoint}"
        return api_post(url, token=self.token, json=payload, timeout=timeout)

    # Every failure in this section is spoken, so it has to be short enough to
    # be worth listening to.
    _CALL_ERROR_MAX_SPOKEN = 120

    def _raise_for_call_response(self, response, action: str):
        """Turn a failed call-control response into a speakable error.

        The body is up to 500 characters of JSON wrapping a minified browser
        stack trace, and it used to go straight into the spoken message: a
        failed answer read the whole thing aloud. The status code is what the
        user can act on; the body belongs in log.log, which is the file we ask
        for anyway.
        """
        if response.status_code < 400:
            return response
        logging.warning("[call] %s failed: HTTP %s %s", action,
                        response.status_code, response.text[:500])
        raise RuntimeError(f"HTTP {response.status_code}")

    @staticmethod
    def _call_response_route(response) -> str:
        """Which path the Node side took for a call action, for the log.

        Only the route and the call state are read: the body also carries the
        peer JID, which must not reach log.log (docs/traps/log-pii.md).
        """
        try:
            body = response.json()
        except Exception:
            return "unknown"
        data = body.get("response") if isinstance(body, dict) else None
        if not isinstance(data, dict) or not data.get("via"):
            return "wa-js"
        call = data.get("call") if isinstance(data.get("call"), dict) else {}
        state = str(call.get("state") or "")
        via = str(data.get("via"))
        return f"{via} state={state}" if state else via

    def _call_error_text(self, error) -> str:
        """Collapse any call failure into one short line fit for speech."""
        logging.info("[call] failure detail: %r", error)
        text = " ".join(str(error or "").split())
        if not text:
            return self.i18n.t("voice_call_error_unknown")
        if len(text) > self._CALL_ERROR_MAX_SPOKEN:
            text = text[: self._CALL_ERROR_MAX_SPOKEN].rstrip() + "..."
        return text

    def _build_call_audio_session(self):
        ws = getattr(self, "ws", None)
        sio = getattr(ws, "sio", None)
        if sio is None:
            raise RuntimeError(self.i18n.t("voice_call_error_no_connection"))
        from core.call_audio import CallAudioConfig, CallAudioSession

        audio_settings = self.settings.get("call_audio_devices", {})
        input_name = audio_settings.get("input_device_name", "")
        output_name = audio_settings.get("output_device_name", "")
        exclusive_input = bool(audio_settings.get("exclusive_input", False))
        exclusive_output = bool(audio_settings.get("exclusive_output", False))
        echo_cancellation = bool(audio_settings.get("echo_cancellation", False))
        session_name = str(getattr(ws, "instance_name", "") or self.token).split(":", 1)[0]
        audio = CallAudioSession(
            sio,
            CallAudioConfig(
                session=session_name,
                input_device_name=input_name,
                output_device_name=output_name,
                exclusive_input=exclusive_input,
                exclusive_output=exclusive_output,
                echo_cancellation=echo_cancellation,
            ),
        )
        return audio, session_name

    def _start_incoming_call_audio_monitor(self, identity: str):
        """Open only the receive side while an incoming call is ringing."""
        if getattr(self, "_call_audio_session", None) is not None:
            return True
        if getattr(self, "_call_ring_audio_session", None) is not None:
            return True
        try:
            audio, session_name = self._build_call_audio_session()
            # Shared while ringing, even with exclusive output on: the ring
            # tone and the screen reader must still be heard (see
            # CallAudioSession.start_output_only).
            audio.start_output_only(allow_exclusive=False)
            self._call_ring_audio_session = audio
            logging.info(
                "[call_audio] ringing monitor started identity=%s session=%s",
                identity, session_name,
            )
            return True
        except Exception:
            logging.exception("[call_audio] failed to start ringing monitor")
            return False

    def _stop_incoming_call_audio_monitor(self):
        monitor = getattr(self, "_call_ring_audio_session", None)
        self._call_ring_audio_session = None
        if monitor is None:
            return
        try:
            monitor.stop()
        except Exception:
            logging.exception("[call_audio] failed to stop ringing monitor")

    def _start_voice_call_audio(self, identity: str, details: dict | None = None,
                                *, keep_active_call: bool = False,
                                microphone_muted: bool = False):
        """Open Python's call audio and record the call it belongs to.

        ``keep_active_call`` is for a device switch inside the same call: the
        existing ``_active_voice_call`` object stays in place. A camera being
        opened concurrently compares that object by identity to tell "same
        call" from "another call", so replacing it here would make it discard
        a perfectly good capture.

        ``microphone_muted`` carries the mute of the session a device switch
        replaced. It is applied before the microphone opens: a fresh session
        starts unmuted, so a user who had muted and then changed device was
        heard again with nothing spoken to say so.
        """
        logging.info("[call_audio] starting session identity=%s", identity)
        if getattr(self, "_call_audio_session", None) is not None:
            return True

        audio = getattr(self, "_call_ring_audio_session", None)
        if audio is None:
            audio, session_name = self._build_call_audio_session()
        else:
            ws = getattr(self, "ws", None)
            session_name = str(getattr(ws, "instance_name", "") or self.token).split(":", 1)[0]

        if microphone_muted:
            audio.set_microphone_muted(True)
        try:
            audio.start()
        except Exception:
            if getattr(self, "_call_ring_audio_session", None) is audio:
                self._call_ring_audio_session = None
                try:
                    audio.stop()
                except Exception:
                    pass
            raise

        self._call_ring_audio_session = None
        logging.info("[call_audio] streams started session=%s", session_name)
        self._call_audio_session = audio
        if keep_active_call:
            if getattr(self, "_active_voice_call", None) is None:
                # The call ended (ENDED arrives without _call_action_lock)
                # while its devices were being switched: do not reopen the
                # microphone and speaker for a call that is already over.
                self._call_audio_session = None
                try:
                    audio.stop()
                except Exception:
                    logging.exception("[call_audio] failed to stop audio of an ended call")
                logging.info("[call_audio] call ended during a device switch; audio not reopened")
                return False
            wx.CallAfter(self._sync_voice_call_bar)
            return True
        details = details or getattr(self, "_incoming_call_details", {}).get(identity, {})
        self._active_voice_call = {
            "identity": identity,
            "call_id": details.get("call_id") or identity,
            "peer_jid": details.get("peer_jid") or "",
            "name": details.get("name") or "",
            "outgoing": bool(details.get("outgoing", False)),
            "is_video": bool(details.get("is_video", False)),
        }
        self._voice_call_last_announced_state = ""
        wx.CallAfter(self._sync_voice_call_bar)
        return True

    def _stop_voice_call_audio(self, grace_seconds: float = 0.0, *, keep_call: bool = False):
        """Stop Python's call audio; by default the call's whole local state.

        ``keep_call`` stops the audio streams ONLY -- the camera, the active
        call record, the announced state and the page bridge are left alone.
        It exists for _restart_active_voice_call_audio(): switching the
        microphone in the middle of a video call used to go through the
        end-of-call path, which stopped the camera, and the capture reopened
        afterwards was discarded as belonging to "another call", so the other
        person saw black for the rest of the call with nothing spoken. The
        bridge is not told either: ``call:audio:stop`` disables it and nothing
        in a device switch enables it again, so the other person stopped
        hearing the user (see CallAudioSession.stop).
        """
        session = getattr(self, "_call_audio_session", None)
        if session is None:
            self._stop_incoming_call_audio_monitor()
        if session is not None and grace_seconds > 0:
            pending = getattr(self, "_call_audio_stop_timer", None)
            if pending is not None and pending.is_alive():
                return

            # WhatsApp renders its disconnect tone immediately after the
            # terminal call state. Keep both the remote Pulse monitor and the
            # local output stream alive long enough to deliver that tail.
            def _finish_after_tail():
                if getattr(self, "_call_audio_session", None) is session:
                    self._stop_voice_call_audio()

            timer = threading.Timer(grace_seconds, _finish_after_tail)
            timer.daemon = True
            self._call_audio_stop_timer = timer
            timer.start()
            return

        pending = getattr(self, "_call_audio_stop_timer", None)
        if pending is not None:
            pending.cancel()
            self._call_audio_stop_timer = None
        self._call_audio_session = None
        if not keep_call:
            self._stop_call_camera(reset_availability=True)
            self._active_voice_call = None
            self._voice_call_last_announced_state = ""
        if session is not None:
            try:
                session.stop(notify_bridge=not keep_call)
            except Exception:
                logging.exception("[call] failed to stop Python call audio")
        if not keep_call:
            ws = getattr(self, "ws", None)
            stop = getattr(ws, "stop_call_audio_stream", None)
            if stop is not None:
                stop()
        if hasattr(self, "voice_call_window"):
            wx.CallAfter(self._sync_voice_call_bar)

    def _restart_active_voice_call_audio(self):
        """Apply changed call devices without ending the WhatsApp call."""
        active = dict(getattr(self, "_active_voice_call", {}) or {})
        if (
            not active
            or getattr(self, "_call_audio_session", None) is None
            or getattr(self, "_call_audio_restart_pending", False)
        ):
            return
        self._call_audio_restart_pending = True

        def _worker():
            with self._call_action_lock:
                try:
                    # Read at the moment the old session stops, not when the
                    # switch was asked for: a mute toggled in between belongs
                    # to the session being replaced and must carry over too.
                    muted = bool(getattr(self._call_audio_session, "microphone_muted", False))
                    self._stop_voice_call_audio(keep_call=True)
                    last_error = None
                    for attempt in range(3):
                        try:
                            # PortAudio may release the old device on a
                            # background callback immediately after close.
                            if attempt:
                                time.sleep(0.25)
                            if self._start_voice_call_audio(
                                str(active.get("identity") or active.get("call_id") or "call"),
                                active,
                                keep_active_call=True,
                                microphone_muted=muted,
                            ):
                                logging.info("[call_audio] active call devices switched")
                            last_error = None
                            break
                        except Exception as exc:
                            last_error = exc
                            self._stop_voice_call_audio(keep_call=True)
                    if last_error is not None:
                        # No audio device would open: tear the local call
                        # down as before, camera included.
                        self._stop_voice_call_audio()
                        raise last_error
                except Exception:
                    logging.exception("[call_audio] failed to switch active call devices")
                    wx.CallAfter(
                        self.output,
                        self.i18n.t("voice_call_device_switch_failed"),
                        True,
                    )
                finally:
                    self._call_audio_restart_pending = False

        threading.Thread(target=_worker, daemon=True).start()

    def _stop_active_voice_call_if_matches(self, identity: str, peer_jid: str):
        active = getattr(self, "_active_voice_call", None)
        if not active:
            return
        if not identity and not peer_jid:
            self._stop_voice_call_audio()
            return
        if identity and identity in {active.get("identity"), active.get("call_id")}:
            self._stop_voice_call_audio()
            return
        if peer_jid and peer_jid == active.get("peer_jid"):
            self._stop_voice_call_audio()

    def on_call_remote_audio(self, pcm: bytes, sample_rate: int):
        session = (
            getattr(self, "_call_audio_session", None)
            or getattr(self, "_call_ring_audio_session", None)
        )
        if session is not None:
            session.enqueue_remote_audio(pcm, sample_rate)

    def on_call_remote_video(self, jpeg: bytes):
        if not (getattr(self, "_active_voice_call", None) or {}).get("is_video"):
            self._call_remote_video_gate_blocked += 1
            if (
                self._call_remote_video_gate_blocked == 1
                or self._call_remote_video_gate_blocked % 100 == 0
            ):
                logging.info(
                    "[call_video] remote frame reached on_call_remote_video but "
                    "is_video gate blocked it (count=%s)",
                    self._call_remote_video_gate_blocked,
                )
            return
        wx.CallAfter(self._show_call_remote_video, jpeg)

    def _show_call_remote_video(self, jpeg: bytes):
        if not (getattr(self, "_active_voice_call", None) or {}).get("is_video"):
            return
        import io
        try:
            image = wx.Image(io.BytesIO(jpeg), wx.BITMAP_TYPE_JPEG)
            if image.IsOk():
                image.Rescale(640, 360, wx.IMAGE_QUALITY_HIGH)
                self.call_video_image.SetBitmap(wx.Bitmap(image))
                self.voice_call_window.Layout()
                if not self._call_remote_video_rendered:
                    self._call_remote_video_rendered = True
                    logging.info(
                        "[call_video] remote-video-rendered first frame decoded "
                        "and drawn successfully"
                    )
            else:
                logging.warning(
                    "[call_video] remote-video-render failed: wx.Image not ok"
                )
        except Exception:
            logging.exception(
                "[call_video] remote-video-render failed to display remote frame"
            )

    def _start_call_camera(self, *, announce_failure: bool = True, transmit: bool = True):
        """Start local camera capture without making video calls depend on it.

        A video call is still useful on a PC with no camera: audio continues and
        remote JPEG frames can still be displayed. Camera discovery/opening is
        therefore a local capability, not a prerequisite for the call itself.

        ``announce_failure`` is off only for the "answer without video" probe
        (accept_incoming_call(with_video=False)): the user deliberately chose
        no video there, so a spoken camera error would be about a device they
        never asked for. Every other call site — an outgoing video call, a
        normal "answer with video", and toggle_call_video()'s own "turn camera
        back on" — asked for the camera, so a failure is worth announcing.

        ``transmit`` is off for that same probe: the camera is opened only to
        prove it works and set ``_call_camera_available``, and must never send
        a single real frame to the peer. Stopping the capture right after
        starting it is not enough on its own — CameraCapture._run() sets its
        ready event before its first send_frame() call, so a caller relying
        on stop-right-after-start alone would be racing an in-flight send.
        """
        from core.call_video import CameraCapture

        self._call_camera_available = False
        self._call_camera_enabled = False
        ws = getattr(self, "ws", None)
        sender = getattr(ws, "send_call_camera_frame", None)
        if sender is None:
            logging.warning("[call_video] local camera transport is unavailable")
            if hasattr(self, "voice_call_window"):
                wx.CallAfter(self._sync_voice_call_bar)
            if announce_failure:
                wx.CallAfter(self.output, self.i18n.t("call_video_no_camera_error"), True)
            return False

        camera_name = self.settings.get("call_video_devices", {}).get("camera_name", "")
        # Every capture carries its own epoch on each frame, and every stop
        # names the epoch it stops. The page drops frames from a stopped epoch,
        # which is what closes the two late-frame leaks: a send racing the stop
        # (CameraCapture.stop() does not join its thread), and a capture that
        # finished opening after the call ended and is discarded below. Either
        # used to repaint a live picture of the user onto the page's canvas
        # after video was off -- and the next call inherited it.
        epoch = self._next_call_camera_epoch()

        def send_frame(frame, _send=sender, _epoch=epoch):
            _send(frame, epoch=_epoch)

        capture = CameraCapture(self._find_api_ffmpeg(), send_frame, transmit=transmit)
        # Captured BEFORE the blocking start below, and compared by identity
        # afterwards. `_active_voice_call` is mutated in place and only ever
        # replaced wholesale by a new call (a device switch keeps it: see
        # keep_active_call in _start_voice_call_audio), so identity -- not truthiness --
        # is what distinguishes "still the same call" from "that call ended
        # and another one began while the camera was opening".
        expected_call = getattr(self, "_active_voice_call", None)
        # The identity test below treats None-is-None as "same call", so the
        # no-call case must be refused up front -- otherwise a camera opened
        # after the call's grace cleanup already ran stays on, transmitting,
        # with no call at all.
        if expected_call is None:
            return False
        try:
            capture.start(camera_name)
        except Exception:
            try:
                capture.stop()
            except Exception:
                pass
            logging.warning(
                "[call_video] local camera unavailable; continuing receive-only video call",
                exc_info=True,
            )
            if hasattr(self, "voice_call_window"):
                wx.CallAfter(self._sync_voice_call_bar)
            if announce_failure:
                wx.CallAfter(self.output, self.i18n.t("call_video_no_camera_error"), True)
            return False

        # Opening the camera blocks for up to several seconds, and it now runs
        # OUTSIDE _call_action_lock (holding that across it blocked hang-up).
        # So the call can end, or be replaced, while we are in here. A plain
        # truthiness test let both slip through: after Ctrl+Shift+Q the grace
        # path of _stop_voice_call_audio() returns without clearing
        # _active_voice_call, so the webcam lit up seconds AFTER the user hung
        # up; and if they started another call in that window, this line
        # overwrote the new call's capture and leaked the old ffmpeg process
        # with the webcam still open and no reference left to stop it.
        if getattr(self, "_active_voice_call", None) is not expected_call:
            capture.stop()
            # This capture was already sending while start() blocked; stop its
            # epoch on the page too, or its in-flight frames land after the
            # call's reset() and leave the user's picture on the canvas.
            self._send_call_camera_stop(epoch)
            logging.info(
                "[call_video] call ended or changed while the camera was opening; "
                "discarding the capture"
            )
            return False

        self._call_camera_capture = capture
        self._call_camera_epoch = epoch
        self._call_camera_available = True
        self._call_camera_enabled = True
        if hasattr(self, "voice_call_window"):
            wx.CallAfter(self._sync_voice_call_bar)
        return True

    def _stop_call_camera(self, *, reset_availability: bool = False, native: bool = False):
        """Stop only local video capture, leaving audio and remote video alive."""
        camera = getattr(self, "_call_camera_capture", None)
        self._call_camera_capture = None
        self._call_camera_enabled = False
        if camera is not None:
            try:
                camera.stop()
            except Exception:
                logging.exception("[call_video] failed to stop local camera")
            # Killing ffmpeg only stops US sending. The page draws our frames
            # onto a canvas and hands WhatsApp a captureStream() of it, which
            # goes on emitting whatever that canvas last held at 10 fps -- so
            # without telling the page, the peer kept seeing a frozen picture
            # of the user for the rest of the call while WinZapp announced
            # video was off. Only when a capture actually existed: a no-op
            # stop must not blank a canvas this call never drew on.
            self._send_call_camera_stop(getattr(self, "_call_camera_epoch", None), native=native)
        if reset_availability:
            self._call_camera_available = None
        if hasattr(self, "voice_call_window"):
            wx.CallAfter(self._sync_voice_call_bar)

    def _next_call_camera_epoch(self) -> int:
        """A strictly increasing id for one camera capture.

        Milliseconds of monotonic time rather than a plain counter: the page
        (and the Node process behind it) can outlive a WinZapp restart, and a
        counter starting over at 0 would sit below the stopped epoch the page
        still remembers, so every frame of the new session would be dropped.
        Monotonic time keeps rising across restarts within a boot, and the
        page does not survive a reboot.
        """
        epoch = time.monotonic_ns() // 1_000_000
        previous = getattr(self, "_call_camera_last_epoch", 0)
        if epoch <= previous:
            epoch = previous + 1
        self._call_camera_last_epoch = epoch
        return epoch

    def _send_call_camera_stop(self, epoch, native: bool = False):
        stop_remote = getattr(getattr(self, "ws", None), "send_call_camera_stop", None)
        if stop_remote is None:
            return
        try:
            stop_remote(epoch=epoch, native=native)
        except Exception:
            logging.exception("[call_video] failed to stop page-side camera")

    def toggle_call_video(self, _event=None):
        """Enable/disable local camera video without changing the call itself."""
        active = getattr(self, "_active_voice_call", None) or {}
        if not active.get("is_video"):
            return
        if getattr(self, "_call_camera_available", None) is not True:
            # This runs on the UI thread (a button/menu handler), so speaking
            # directly is safe — no wx.CallAfter needed here.
            self.output(self.i18n.t("call_video_no_camera_error"), interrupt=True)
            return
        if getattr(self, "_call_camera_capture", None) is not None:
            # native=True: the user asked for video off, so WhatsApp's own call
            # engine is told as well (its camera-button toggle), not just the
            # page's canvas blanked.
            self._stop_call_camera(native=True)
            return

        # A second press while the camera is still opening would start a
        # second capture: with a camera that allows two readers, the first
        # ffmpeg is left running with the webcam light on and no reference to
        # stop it. The first press is already doing what was asked.
        if getattr(self, "_call_camera_resuming", False):
            return
        self._call_camera_resuming = True

        # Opening DirectShow can block for a moment; never do it on the wx UI
        # thread. _start_call_camera() re-probes the camera and refreshes the
        # button when capture is ready (or hides it if the device disappeared).
        threading.Thread(target=self._resume_call_camera, daemon=True).start()

    def _resume_call_camera(self):
        """Turn video back on: restart the capture, then tell WhatsApp.

        WhatsApp Web transmits our camera through its own call engine, which
        treated the camera as off once the picture went black and never came
        back on its own -- re-enabled frames kept reaching the page while the
        peer stayed on black. The engine is told only once the capture is
        really running, so it never resumes onto a canvas nothing feeds.
        """
        try:
            if not self._start_call_camera():
                return
            resume = getattr(getattr(self, "ws", None), "send_call_camera_start", None)
            if resume is None:
                return
            try:
                resume()
            except Exception:
                logging.exception("[call_video] failed to resume page-side camera")
        finally:
            self._call_camera_resuming = False

    def accept_incoming_call(self, identity: str, *, with_video: bool | None = None):
        if getattr(self, "_active_voice_call", None) is not None:
            self.output(self.i18n.t("voice_call_already_active"), interrupt=True)
            return
        details = dict(getattr(self, "_incoming_call_details", {}).get(identity, {}))
        # The call TYPE never changes with `with_video` — a video offer stays
        # a video call (remote video keeps arriving, the video window still
        # opens) whether or not the local camera happens to be on. Only the
        # local camera's starting state is a WinZapp-side choice.
        is_video = bool(details.get("is_video"))
        # `with_video=None` (the default, used by the ordinary answer button)
        # keeps today's behaviour: the camera starts and is left sending.
        # "Answer without video" passes False explicitly so the camera is
        # probed (so _call_camera_available becomes True and the manual
        # video-toggle button works for the rest of the call) but never
        # transmits — see _start_call_camera()'s ``transmit`` docstring for
        # why probe-then-stop alone cannot guarantee that on its own.
        start_camera_enabled = True if with_video is None else bool(with_video)
        payload = self._call_control_payload(identity)
        # Keep the receive-only monitor alive so accepting can promote the
        # same CallAudioSession to full duplex without reopening the speaker.
        self.stop_incoming_call_alert(identity, keep_audio_monitor=True)
        self._active_voice_call = {
            "identity": identity,
            "call_id": details.get("call_id") or identity,
            "peer_jid": details.get("peer_jid") or "",
            "name": details.get("name") or "",
            "outgoing": False,
            "is_video": is_video,
        }
        wx.CallAfter(self._sync_voice_call_bar)

        def _worker():
            accepted = False
            with self._call_action_lock:
                try:
                    self._start_voice_call_audio(identity, details)
                    self._raise_for_call_response(
                        self._post_call_control("accept", payload), "accept"
                    )
                    wx.CallAfter(
                        self.output,
                        self.i18n.t("incoming_call_answered"),
                        True,
                    )
                    accepted = True
                except Exception as exc:
                    self._stop_voice_call_audio()
                    logging.exception("[call] accept failed")
                    wx.CallAfter(
                        self.output,
                        self.i18n.t("incoming_call_answer_failed").format(
                            error=self._call_error_text(exc)
                        ),
                        True,
                    )

            # Camera work happens AFTER the peer is answered -- opening
            # DirectShow costs an ffmpeg device enumeration plus the capture
            # start timeout, and paying that before the accept POST, with the
            # ring tone already stopped, left a blind user in total silence
            # for seconds after pressing Answer.
            #
            # It is also OUTSIDE _call_action_lock, which end_active_call()
            # takes as well: holding the lock across those seconds while the
            # call is already live and the user has just been told so made
            # Ctrl+Shift+Q (hang up) block with no spoken feedback at all.
            # The camera needs no serialisation against other call actions --
            # _start_call_camera() already re-checks _active_voice_call and
            # stops itself if the call went away underneath it.
            if accepted and is_video:
                try:
                    # transmit=False means the probe never sends a real frame
                    # to the peer while proving the camera works, so
                    # _stop_call_camera() below is cleanup rather than a race
                    # against an in-flight frame.
                    if start_camera_enabled:
                        self._start_call_camera()
                    else:
                        self._probe_call_camera()
                except Exception:
                    # The call is already accepted; a camera problem must not
                    # be reported as "answer failed" nor tear the audio down.
                    logging.exception("[call_video] camera setup failed after accept")

        threading.Thread(target=_worker, daemon=True).start()

    def reject_incoming_call(self, identity: str):
        payload = self._call_control_payload(identity)
        self.stop_incoming_call_alert(identity)
        # A reject that never reached WhatsApp (the caller's phone kept ringing)
        # could not be told apart in the log from one WhatsApp ignored: this
        # line against the POST's own line shows time spent waiting for the
        # call-action lock, and the answer below names the route taken.
        logging.info("[call] reject requested has_call_id=%s", bool(payload))

        def _worker():
            with self._call_action_lock:
                try:
                    response = self._raise_for_call_response(
                        self._post_call_control("reject", payload), "reject"
                    )
                    logging.info(
                        "[call] reject answered via %s",
                        self._call_response_route(response),
                    )
                except Exception as exc:
                    logging.exception("[call] reject failed")
                    wx.CallAfter(
                        self.output,
                        self.i18n.t("incoming_call_reject_failed").format(
                            error=self._call_error_text(exc)
                        ),
                        True,
                    )

        threading.Thread(target=_worker, daemon=True).start()

    def end_active_call(self, _event=None):
        # Marked here, on the UI thread, before the worker even starts: an
        # outgoing offer no longer holds _call_action_lock while it blocks
        # (#275), so hanging up can reach WhatsApp before the offer does and
        # the "end" below then names a call the server has not created yet.
        # _start_individual_call()'s worker reads this when its offer lands and
        # ends the call it just created instead of ringing the peer.
        attempt = getattr(self, "_outgoing_call_attempt", None)
        if attempt is not None:
            attempt["cancelled"] = True
        promote = getattr(self, "_promote_attempt", None)
        if promote is not None:
            promote["cancelled"] = True

        # Read now, on the UI thread: by the time the worker runs, the grace
        # teardown or a terminal event may already have cleared the record.
        ended_call_id = str((getattr(self, "_active_voice_call", None) or {}).get("call_id") or "")

        def _worker():
            with self._call_action_lock:
                try:
                    self._raise_for_call_response(
                        self._post_call_control("end", {}), "end"
                    )
                except Exception as exc:
                    logging.exception("[call] end failed")
                    wx.CallAfter(
                        self.output,
                        self.i18n.t("incoming_call_end_failed").format(
                            error=self._call_error_text(exc)
                        ),
                        True,
                    )
                finally:
                    self._stop_voice_call_audio(grace_seconds=1.25)
            # A failed or ignored `end` must not leave the page holding the call
            # the user was just told is over.
            self._ensure_page_call_ended(ended_call_id, delay=2.5)

        threading.Thread(target=_worker, daemon=True).start()

    def _ensure_page_call_ended(self, call_id: str, *, delay: float = 1.5):
        """End *call_id* on the page if WinZapp declared it over and it is not.

        WinZapp announcing "call ended" while WhatsApp Web still holds the call
        is the worst failure a blind user can meet: the other person keeps
        talking out of the speaker, and the call window -- the only way to hang
        up -- is gone. Scoped on the Node side to this exact call id and to a
        call that is still connected or dialling, so it can never end a newer
        call. The delay lets an ordinary hang-up finish on its own first.
        """
        call_id = str(call_id or "")
        if not call_id or call_id.startswith("outgoing:"):
            return  # WinZapp's own placeholder, never WhatsApp's call id

        def _worker():
            time.sleep(delay)
            if getattr(self, "_shutting_down", False):
                return
            with self._call_action_lock:
                try:
                    response = self._raise_for_call_response(
                        self._post_call_control("ensure-ended", {"callId": call_id}),
                        "ensure-ended",
                    )
                    body = response.json() if response is not None else {}
                    result = (body or {}).get("response") or {}
                    if result.get("stillLive"):
                        # Never quietly: this is the defect's signature.
                        logging.warning(
                            "[call] page still held a call WinZapp had ended; ended it now"
                        )
                except Exception:
                    logging.exception("[call] ensure-ended failed")

        threading.Thread(target=_worker, daemon=True, name="call-ensure-ended").start()

    def _probe_call_camera(self):
        """Prove the camera works without it ever sending a frame.

        Sets ``_call_camera_available`` so the "turn video on" button appears.
        Only a capture this probe actually started is stopped: when
        _start_call_camera() returns False the call changed while the camera
        was opening, and stopping would hit the NEW call's capture.
        """
        if self._start_call_camera(announce_failure=False, transmit=False):
            self._stop_call_camera()

    def _on_call_upgraded_to_video(self):
        """The ongoing voice call became a video call without ending.

        The call window turns into the video call window in place: its title,
        the video area and the video buttons follow is_video through
        _sync_voice_call_bar(). It is deliberately NOT hidden and re-shown:
        that raised it and took focus while the user was reading a
        conversation mid-call, and the focus change cut this announcement off
        (docs/traps/voice-calls.md: focus goes to the window once, when it
        appears). Only when focus was on the promote button, which is now
        hidden, does it move -- to the mute button (_sync_voice_call_bar()).

        The camera is only probed: when the other person upgrades, WhatsApp
        leaves this side's camera off until the user turns it on. An upgrade
        the user asked for (promote_call_to_video) starts its own camera.
        """
        self.output(self.i18n.t("voice_call_upgraded_to_video"), interrupt=True)
        # Hides the promote button, moving focus off it first if it had it.
        self._sync_voice_call_bar()
        if getattr(self, "_call_camera_capture", None) is not None:
            return  # promote_call_to_video() already has the camera running
        if getattr(self, "_call_upgrade_pending", False):
            return  # ... or is opening it right now

        def _probe_camera():
            try:
                self._probe_call_camera()
            except Exception:
                logging.exception("[call_video] camera probe after upgrade failed")

        threading.Thread(target=_probe_camera, daemon=True).start()

    def _mark_call_upgraded_to_video(self, expected_call):
        active = getattr(self, "_active_voice_call", None)
        if active is None or active is not expected_call or active.get("is_video"):
            return
        active["is_video"] = True
        self._sync_voice_call_bar()
        self._on_call_upgraded_to_video()

    def promote_call_to_video(self, _event=None):
        """Ctrl+P: turn the ongoing voice call into a video call."""
        active = getattr(self, "_active_voice_call", None)
        if not active or active.get("is_video"):
            return
        if getattr(self, "_voice_call_last_announced_state", "") != "ACTIVE":
            return  # still ringing: there is no connected call to switch yet
        if getattr(self, "_call_upgrade_pending", False):
            return
        self._call_upgrade_pending = True
        expected_call = active
        # Marked cancelled by end_active_call() on the UI thread, the same
        # shape as _outgoing_call_attempt: a hang-up while this POST is in
        # flight must not be followed by "the call is now a video call" and
        # a camera start inside the hang-up's grace period.
        attempt = {"cancelled": False}
        self._promote_attempt = attempt

        def _worker():
            try:
                # OUTSIDE _call_action_lock, like the offer POST and for the
                # same reason (#275): the lock serialises state transitions,
                # never a blocking POST -- held across this one's 20 s timeout,
                # Ctrl+Shift+Q right after Ctrl+P sat there with nothing
                # spoken. It changes no WinZapp state; the identity check
                # below is what the lock would otherwise have protected.
                try:
                    self._raise_for_call_response(
                        self._post_call_control("upgrade-video", {}, timeout=20),
                        "upgrade-video",
                    )
                except Exception as exc:
                    logging.exception("[call] upgrade to video failed")
                    wx.CallAfter(
                        self.output,
                        self.i18n.t("voice_call_upgrade_failed").format(
                            error=self._call_error_text(exc)
                        ),
                        True,
                    )
                    return
                with self._call_action_lock:
                    still_this_call = (
                        getattr(self, "_active_voice_call", None) is expected_call
                        and not attempt["cancelled"]
                    )
                if not still_this_call:
                    return  # the call ended while WhatsApp was switching it
                # Switch the window now rather than waiting for the page's
                # isVideo event; when that arrives the call is already video
                # and nothing is announced twice.
                wx.CallAfter(self._mark_call_upgraded_to_video, expected_call)
                # Outside the lock, like every camera start (it blocks for
                # seconds and hang-up takes the same lock).
                if self._start_call_camera():
                    resume = getattr(getattr(self, "ws", None), "send_call_camera_start", None)
                    if resume is not None:
                        try:
                            resume()
                        except Exception:
                            logging.exception("[call_video] failed to resume page-side camera")
            finally:
                self._call_upgrade_pending = False

        threading.Thread(target=_worker, daemon=True).start()

    def open_call_audio_settings(self, _event=None, parent=None, *,
                                 include_audio: bool = True, include_camera: bool = False):
        """Choose call devices without changing the global audio/camera settings.

        ``parent`` is passed by whoever opened this. It matters: parented to the
        main window while the Settings dialog is what opened it, the chooser has
        an enabled sibling that can sit on top of it.

        ``include_audio``/``include_camera`` pick which rows the dialog shows —
        this one method backs three call sites: the Settings dialog's audio-only
        button, its camera-only button, and the in-call window's own settings
        button (audio always, camera only while the current call is video).
        """
        import sounddevice as sd
        from core.call_video import list_camera_devices
        from ui.call_audio_options import confirm_exclusive_output
        if parent is None:
            call_window = getattr(self, "voice_call_window", None)
            parent = call_window if (call_window is not None and call_window.IsShown()) else self
        title_key = "voice_call_video_settings_title" if (include_camera and not include_audio) else "voice_call_settings_title"
        dialog = wx.Dialog(
            parent, title=self.i18n.t(title_key),
            size=(560, 520 if include_audio else 390),
        )
        root = wx.BoxSizer(wx.VERTICAL)
        default_name = self.i18n.t("audio_device_default")

        def add_combo(label_key, names, selected):
            root.Add(wx.StaticText(dialog, label=self.i18n.t(label_key)),
                     0, wx.LEFT | wx.TOP | wx.RIGHT, 8)
            combo = wx.ComboBox(dialog, style=wx.CB_READONLY,
                                choices=[default_name] + names)
            combo.SetSelection(1 + names.index(selected) if selected in names else 0)
            root.Add(combo, 0, wx.EXPAND | wx.ALL, 8)
            return combo

        input_combo = output_combo = None
        if include_audio:
            audio_cfg = self.settings.setdefault("call_audio_devices", {})
            try:
                output_names = [str(d.get("name", "")).strip() for d in sd.query_devices()
                                if d.get("max_output_channels", 0) > 0]
            except Exception:
                output_names = []
            input_names = [name for _, name in enumerate_input_devices()]
            input_combo = add_combo("voice_call_recording_devices",
                                    input_names, audio_cfg.get("input_device_name", ""))
            output_combo = add_combo("voice_call_playback_devices",
                                     output_names, audio_cfg.get("output_device_name", ""))
            # Native checkboxes, one per option, so each is announced by its
            # own label. Two exclusive boxes, not one: holding the MICROPHONE
            # exclusively takes a device nothing else is using mid-call, while
            # holding the SPEAKER exclusively silences every other application
            # on it -- the screen reader included. Only the output one warns.
            exclusive_input_check = wx.CheckBox(
                dialog, label=self.i18n.t("calls_exclusive_input_label"))
            exclusive_input_check.SetValue(bool(audio_cfg.get("exclusive_input", False)))
            exclusive_output_check = wx.CheckBox(
                dialog, label=self.i18n.t("calls_exclusive_output_label"))
            exclusive_output_check.SetValue(bool(audio_cfg.get("exclusive_output", False)))
            echo_check = wx.CheckBox(
                dialog, label=self.i18n.t("calls_echo_cancellation_label"))
            echo_check.SetValue(bool(audio_cfg.get("echo_cancellation", False)))
            for check in (exclusive_input_check, exclusive_output_check, echo_check):
                root.Add(check, 0, wx.ALL, 8)
            exclusive_output_check.Bind(
                wx.EVT_CHECKBOX,
                lambda evt: confirm_exclusive_output(
                    dialog, self.i18n, evt, exclusive_output_check),
            )

        camera_combo = None
        if include_camera:
            video_cfg = self.settings.setdefault("call_video_devices", {})
            camera_combo = add_combo("voice_call_camera_devices",
                                     list_camera_devices(self._find_api_ffmpeg()),
                                     video_cfg.get("camera_name", ""))

        buttons = wx.StdDialogButtonSizer()
        cancel_button = wx.Button(dialog, wx.ID_CANCEL, self.i18n.t("cancel"))
        apply_button = wx.Button(dialog, wx.ID_APPLY, self.i18n.t("apply"))
        ok_button = wx.Button(dialog, wx.ID_OK, self.i18n.t("ok"))
        buttons.AddButton(cancel_button); buttons.AddButton(apply_button); buttons.AddButton(ok_button); buttons.Realize()
        root.Add(buttons, 0, wx.ALIGN_RIGHT | wx.ALL, 8)
        first_combo = input_combo if input_combo is not None else camera_combo

        def apply(_evt=None):
            if include_audio:
                audio_cfg["input_device_name"] = "" if input_combo.GetStringSelection() == default_name else input_combo.GetStringSelection()
                audio_cfg["output_device_name"] = "" if output_combo.GetStringSelection() == default_name else output_combo.GetStringSelection()
                audio_cfg["exclusive_input"] = exclusive_input_check.GetValue()
                audio_cfg["exclusive_output"] = exclusive_output_check.GetValue()
                audio_cfg["echo_cancellation"] = echo_check.GetValue()
            if include_camera:
                video_cfg["camera_name"] = "" if camera_combo.GetStringSelection() == default_name else camera_combo.GetStringSelection()
            self.save_settings()
            if include_audio and getattr(self, "_call_audio_session", None) is not None:
                self._restart_active_voice_call_audio()
            if (
                include_camera
                and getattr(self, "_call_camera_capture", None) is not None
                and not getattr(self, "_call_camera_resuming", False)
            ):
                self._stop_call_camera()
                # Same guard as toggle_call_video(): a video-on press while
                # this reopen runs must not start a second capture.
                self._call_camera_resuming = True
                threading.Thread(target=self._resume_call_camera, daemon=True).start()
            if first_combo is not None:
                wx.CallAfter(first_combo.SetFocus)
        apply_button.Bind(wx.EVT_BUTTON, apply)
        ok_button.Bind(wx.EVT_BUTTON, lambda evt: (apply(), dialog.EndModal(wx.ID_OK)))
        cancel_button.Bind(wx.EVT_BUTTON, lambda evt: dialog.EndModal(wx.ID_CANCEL))
        dialog.SetSizer(root)
        if first_combo is not None:
            first_combo.SetFocus()
        dialog.ShowModal()
        dialog.Destroy()

    def _open_active_call_settings(self, _event=None):
        """In-call settings button/menu: camera only while the call is video."""
        active = getattr(self, "_active_voice_call", None) or {}
        self.open_call_audio_settings(include_camera=bool(active.get("is_video")))

    def _on_voice_call_window_close(self, event):
        if getattr(self, "_active_voice_call", None):
            self.end_active_call()
        else:
            event.Skip()

    def start_voice_call(self, peer_jid: str, name: str = ""):
        self._start_individual_call(peer_jid, name, is_video=False)

    def start_video_call(self, peer_jid: str, name: str = ""):
        self._start_individual_call(peer_jid, name, is_video=True)

    def _start_individual_call(self, peer_jid: str, name: str, *, is_video: bool):
        """Start a one-to-one WhatsApp voice/video call using Python-owned media."""
        peer_jid = self._normalize_jid(str(peer_jid or ""))
        # The self-chat belongs on this list with the other unreachable kinds.
        # Hiding the call buttons for it (#268) covers the mouse and the Tab
        # order, but not the keyboard: the accelerators reach _on_voice_call()
        # directly, so from the message list of "Me" a shortcut still spoke
        # "calling Me..." and POSTed an offer to the user's own JID -- with the
        # button hidden, a blind user had no way to know the action even
        # existed there. This is the layer every path goes through.
        if (
            not peer_jid
            or peer_jid.endswith(("@g.us", "@newsletter", "@broadcast"))
            or self._is_self_jid(peer_jid)
        ):
            self.output(self.i18n.t("voice_call_individual_only"), interrupt=True)
            return
        if (
            getattr(self, "_active_voice_call", None) is not None
            or bool(getattr(self, "_active_incoming_calls", {}))
        ):
            self.output(self.i18n.t("voice_call_already_active"), interrupt=True)
            return
        identity = f"outgoing:{peer_jid}"
        details = {
            "call_id": identity,
            "peer_jid": peer_jid,
            "name": name or self._preview_sender_from_jid(peer_jid) or peer_jid,
            "outgoing": True,
            "is_video": is_video,
        }
        self.output(
            self.i18n.t("voice_call_starting").format(name=details["name"]),
            interrupt=True,
        )
        self._active_voice_call = dict(details)
        self._voice_call_last_announced_state = ""
        wx.CallAfter(self._sync_voice_call_bar)
        # One token per outgoing attempt, kept on the window rather than on the
        # call record: _start_voice_call_audio() below REPLACES
        # _active_voice_call with a record of its own, so a flag written onto
        # the record while the offer is in flight could land on a dict nobody
        # reads again. end_active_call() sets ``cancelled`` here.
        attempt = {"cancelled": False}
        self._outgoing_call_attempt = attempt

        def _worker():
            offered = False
            # The record this attempt owns, compared by identity from here on
            # the way _start_call_camera() does: while the offer is in flight a
            # terminal callstate event, or an incoming call the user answered,
            # can replace _active_voice_call with a different call, and that
            # call must not be torn down by this attempt's failure.
            call_record = getattr(self, "_active_voice_call", None)
            try:
                with self._call_action_lock:
                    self._start_voice_call_audio(identity, details)
                    # Taken after the audio start, which is what installs the
                    # record this attempt owns.
                    call_record = getattr(self, "_active_voice_call", None)
                    dial_jid = self._resolve_jid_for_send(peer_jid) or peer_jid

                # The offer POST runs OUTSIDE _call_action_lock, for the same
                # reason as the camera below: it blocks for up to 75 seconds
                # and end_active_call() needs that lock, so holding it here
                # left "end call" dead for the whole timeout (#275). The price
                # is that a hang-up can now reach WhatsApp while the offer is
                # still in flight -- that is what ``cancelled`` settles.
                response = self._raise_for_call_response(
                    self._post_call_control(
                        "offer",
                        {"to": dial_jid, "isVideo": is_video},
                        timeout=75,
                    ),
                    "offer",
                )
                try:
                    body = response.json().get("response") or {}
                except Exception:
                    body = {}
                call_id = str(body.get("id") or "") if isinstance(body, dict) else ""
                with self._call_action_lock:
                    # Everything this decision needs is re-read HERE, under the
                    # lock, never carried in from before the POST: a hang-up, a
                    # terminal event or a whole new call can have landed while
                    # the offer blocked, and every branch below is about which
                    # of those happened.
                    cancelled = attempt["cancelled"]
                    active = getattr(self, "_active_voice_call", None)
                    still_ours = active is call_record
                    offered = still_ours and not cancelled
                    # Adopting WhatsApp's real id happens only when the call
                    # actually went live. On a cancel the record is about to be
                    # dropped, and leaving the provisional ``outgoing:<jid>``
                    # id on it means a terminal callstate for the real id
                    # cannot match a call nobody holds any more.
                    if offered and call_id:
                        call_record["call_id"] = call_id
                    if cancelled and (
                        # A newer attempt owns the page now; its call is not
                        # ours to hang up.
                        getattr(self, "_outgoing_call_attempt", None) is attempt
                        # Either this attempt's call is still the active one,
                        # or there is no call at all. NOT "some other call is
                        # active": the "end" action is unscoped on the Node
                        # side -- callController.ts's 'end' branch uses the
                        # requested callId only to flag userEndedCall, while
                        # the action itself is voipStack.endCall(2, true) /
                        # WPP.call.end() on whatever call the page holds -- so
                        # posting it then would kill a second, live call a few
                        # seconds in with nothing spoken.
                        and (still_ours or active is None)
                    ):
                        # The user hung up before the offer landed, so their
                        # "end" POST named a call WhatsApp did not have yet and
                        # the peer is ringing right now for a call WinZapp
                        # believes is over. End the call this attempt just
                        # created, and never start the camera for it.
                        logging.info(
                            "[call] outgoing offer landed after the call was cancelled; ending it"
                        )
                        try:
                            self._raise_for_call_response(
                                self._post_call_control("end", {}), "end"
                            )
                        except Exception:
                            logging.exception("[call] failed to end a cancelled outgoing call")
            except Exception as exc:
                logging.exception("[call] outgoing call failed")
                wx.CallAfter(
                    self.output,
                    self.i18n.t("voice_call_start_failed").format(
                        error=self._call_error_text(exc)
                    ),
                    True,
                )
            finally:
                # In a finally, not in the except: a refused offer, a hang-up
                # that beat it and a record replaced underneath us all leave
                # this attempt owning no live call, and the one path that
                # forgets to clear _active_voice_call costs an orphaned call
                # window, a "call already active" refusal of every later call
                # and up to _VOICE_CALL_PAUSE_MAX_SECONDS of stood-down
                # background sync (#275).
                if not offered:
                    self._abandon_outgoing_call(call_record)
                if getattr(self, "_outgoing_call_attempt", None) is attempt:
                    self._outgoing_call_attempt = None

            # Camera after the offer, and outside _call_action_lock, for the
            # same two reasons as accept_incoming_call(): device enumeration
            # must not sit between the user pressing "video call" and the
            # offer being placed, and it must not hold the lock that
            # end_active_call() needs while the call is already live. The call
            # is a video call either way -- the camera only decides whether WE
            # transmit.
            if offered and is_video:
                try:
                    self._start_call_camera()
                except Exception:
                    logging.exception("[call_video] camera setup failed after offer")

        threading.Thread(target=_worker, daemon=True).start()

    def _abandon_outgoing_call(self, call_record):
        """Drop the local state of an outgoing call that never went live.

        ``call_record`` is the ``_active_voice_call`` object the attempt owned,
        and the comparison is by identity rather than truthiness for the reason
        _start_call_camera() spells out: a call that replaced this one while
        the offer was in flight is a different call, and tearing its audio,
        camera and window down here would end a call the user is in.

        ``None`` is refused up front for the reason _start_call_camera() gives
        at its own identity test: None-is-None reads as "same call", so an
        attempt that never got a record would otherwise tear down whatever
        state happens to exist when it fails.
        """
        if call_record is None:
            return
        if getattr(self, "_active_voice_call", None) is not call_record:
            return
        self._stop_voice_call_audio()
        # _stop_voice_call_audio() clears these itself on the path taken here
        # (no grace period, so it never returns early), but the state this
        # method exists to guarantee is written where it can be read, not left
        # to a side effect of the audio teardown.
        self._active_voice_call = None
        self._voice_call_last_announced_state = ""
        wx.CallAfter(self._sync_voice_call_bar)

    def _sync_voice_call_bar(self):
        """Keep the modeless call window in step without repeatedly stealing focus."""
        window = getattr(self, "voice_call_window", None)
        if window is None:
            return
        active = getattr(self, "_active_voice_call", None)
        if not active:
            window.Hide()
            self.call_video_image.Hide()
            return
        muted = bool(
            getattr(getattr(self, "_call_audio_session", None), "microphone_muted", False)
        )
        mute_label = self.i18n.t(
            "voice_call_unmute_button" if muted else "voice_call_mute_button"
        )
        button = getattr(self, "voice_call_window_mute_button", None)
        if button is not None:
            button.SetLabel(mute_label)
        name = active.get("name") or active.get("peer_jid") or self.i18n.t("unknown_contact")
        active_text = self.i18n.t(active_call_label_key(active)).format(name=name)
        is_video = bool(active.get("is_video"))
        self.call_video_image.Show(is_video)
        promote_button = getattr(self, "voice_call_window_promote_button", None)
        if promote_button is not None:
            promote_button.SetLabel(self.i18n.t("voice_call_promote_video_button"))
            show_promote = (
                not is_video
                and getattr(self, "_voice_call_last_announced_state", "") == "ACTIVE"
            )
            if not show_promote and promote_button.IsShown() and promote_button.HasFocus():
                # Moved BEFORE the button is hidden: a hidden control's focus
                # is not something to rely on. The mute button is where focus
                # goes when a call window appears.
                mute_focus = getattr(self, "voice_call_window_mute_button", None)
                if mute_focus is not None:
                    mute_focus.SetFocus()
            promote_button.Show(show_promote)

        # Local-camera controls are independent from remote video reception.
        # Until camera probing succeeds the button stays hidden; on a PC with
        # no camera it never appears, while the remote image above remains
        # visible for the duration of the video call.
        video_button = getattr(self, "voice_call_window_video_button", None)
        local_camera_available = getattr(self, "_call_camera_available", None) is True
        local_camera_enabled = bool(getattr(self, "_call_camera_enabled", False))
        if video_button is not None:
            video_button.Show(is_video and local_camera_available)
            if is_video and local_camera_available:
                video_button.SetLabel(
                    self.i18n.t(
                        "voice_call_video_off_button"
                        if local_camera_enabled
                        else "voice_call_video_on_button"
                    )
                )
        window.SetTitle(
            self.i18n.t(
                "video_call_window_title" if is_video else "voice_call_window_title"
            )
        )
        window_label = getattr(self, "voice_call_window_label", None)
        if window_label is not None:
            window_label.SetLabel(active_text)
        window.Layout()
        # Fitted to whatever is shown -- the video area only on video calls,
        # the fourth button only with a camera, and the label's actual text --
        # instead of fixed sizes that were right for one language and one name
        # length. After SetLabel, so the fit measures the name being shown.
        sizer = getattr(self, "_call_window_sizer", None)
        if sizer is not None:
            sizer.Fit(window)
        if not window.IsShown():
            window.Show()
            window.Raise()
            if button is not None:
                button.SetFocus()

    def toggle_call_microphone(self, _event=None):
        session = getattr(self, "_call_audio_session", None)
        if session is None:
            return
        session.set_microphone_muted(not session.microphone_muted)
        self._sync_voice_call_bar()

    def on_voice_call_state_event(self, event: dict):
        """Apply the complete call lifecycle emitted by the page CallStore."""
        if not isinstance(event, dict):
            return
        state = str(event.get("state") or "").upper()
        call_id = str(event.get("id") or "")
        peer_jid = self._normalize_jid(str(event.get("peerJid") or ""))
        # A real group JID, for the same reason as on_incoming_call_event():
        # the Node side infers isGroup from participant count, so the flag can
        # be asserted for a one-to-one call. Trusting it alone here meant a
        # call answered on the phone or from the page controls arrived as
        # ACTIVE, was discarded as a group event, and WinZapp never adopted
        # it -- no "call connected", no Python audio attached, nothing on
        # screen. The user was in a call their PC pretended not to see.
        is_group_event = bool(
            peer_jid.endswith("@g.us")
            or str(event.get("groupJid") or "").endswith("@g.us")
        )
        active = getattr(self, "_active_voice_call", None)
        if not active:
            # A group call is never adopted as the active call. This guard
            # deliberately sits INSIDE the "no active call" branch: applied
            # before the match below, a single false-positive isGroup (the Node
            # side now also infers it from participant count) would swallow the
            # terminal ENDED of a real one-to-one call, leaving the microphone
            # open and the call window on screen after the other side hung up.
            if is_group_event:
                return
            if state != "ACTIVE" or not call_id:
                return
            active = {
                "identity": call_id,
                "call_id": call_id,
                "peer_jid": peer_jid,
                "name": self._preview_sender_from_jid(peer_jid) or peer_jid,
                "outgoing": bool(event.get("outgoing", False)),
                "is_video": bool(event.get("isVideo")),
            }
            self._active_voice_call = active
            self._voice_call_last_announced_state = ""
            wx.CallAfter(self._sync_voice_call_bar)
        if not call_event_matches_active(
            active, call_id, peer_jid, self._chat_jids_equivalent
        ):
            return
        if call_id:
            active["call_id"] = call_id
        # A voice call the other person (or this user, Ctrl+P) upgraded to
        # video: WhatsApp keeps the same call and id and only flips isVideo.
        # Deliberately one-way -- isVideo also drops back to false when both
        # cameras are off, and that is still a video call to the user.
        upgraded_to_video = bool(event.get("isVideo")) and not active.get("is_video")
        if event.get("isVideo"):
            active["is_video"] = True
        if peer_jid:
            active["peer_jid"] = peer_jid
        if not active.get("name") and peer_jid:
            active["name"] = self._preview_sender_from_jid(peer_jid) or peer_jid
        self._sync_voice_call_bar()

        terminal_states = {
            "ENDED", "REJECTED", "FAILED", "NOT_ANSWERED",
            "HANDLED_REMOTELY", "REMOTE_CALL_IN_PROGRESS",
        }
        if state in terminal_states or event.get("event") in {"ended", "timeout"}:
            self._confirm_call_ended(
                active, call_id, peer_jid, upgraded_to_video=upgraded_to_video)
            return
        if upgraded_to_video:
            self._on_call_upgraded_to_video()
        if state == "REJOINING":
            if self._voice_call_last_announced_state != "REJOINING":
                self._voice_call_last_announced_state = "REJOINING"
                self.output(self.i18n.t("voice_call_reconnecting"), interrupt=True)
            return
        if state == "ACTIVE" and self._voice_call_last_announced_state != "ACTIVE":
            self._voice_call_last_announced_state = "ACTIVE"
            self._sync_voice_call_bar()  # the promote button needs a connected call
            if getattr(self, "_call_audio_session", None) is None:
                details = dict(active)
                threading.Thread(
                    target=self._attach_audio_to_browser_call,
                    args=(details,),
                    daemon=True,
                ).start()
            self.output(self.i18n.t("voice_call_connected"), interrupt=True)

    def _confirm_call_ended(self, active: dict, call_id: str, peer_jid: str,
                            *, upgraded_to_video: bool = False):
        """Ask the page whether the call is really over before saying so.

        A terminal event is not proof on its own: when the other person
        switched a voice call to video, WinZapp received one while the call
        went on (same id, still ACTIVE on the page), announced "call ended",
        closed the window and left the user inside a live call. So the page is
        asked about THAT call id first. Only a clear "still connected, and
        the engine agrees" keeps the call; no answer at all ends it, because
        a call wrongly kept up can still be hung up from the window, while a
        live call wrongly declared over cannot.
        """
        call_id = str(call_id or active.get("call_id") or "")
        checks = self.__dict__.setdefault("_call_end_checks", {})
        check = checks.get(call_id)
        if check is not None:
            # A terminal event while this call is already being checked. The
            # Node side emits each terminal event only once, so dropping this
            # one could keep a call that has really ended: if the check in
            # flight answers "live", it is asked again.
            check["recheck"] = True
            check["upgraded"] = check["upgraded"] or upgraded_to_video
            return
        checks[call_id] = {"recheck": False, "upgraded": bool(upgraded_to_video)}

        def _worker():
            status = None
            try:
                if call_id and not call_id.startswith("outgoing:"):
                    response = self._post_call_control("status", {"callId": call_id}, timeout=8)
                    if response.status_code < 400:
                        status = (response.json() or {}).get("response") or None
            except Exception:
                logging.info("[call] could not confirm the end with the page", exc_info=True)
            wx.CallAfter(self._apply_confirmed_call_end, active, call_id, peer_jid, status)

        threading.Thread(target=_worker, daemon=True, name="call-end-confirm").start()

    def _apply_confirmed_call_end(self, active: dict, call_id: str, peer_jid: str, status):
        check = getattr(self, "_call_end_checks", {}).pop(call_id, None) or {}
        if getattr(self, "_active_voice_call", None) is not active:
            return  # already ended (the user hung up) or replaced meanwhile
        if isinstance(status, dict) and status.get("live") is True:
            if check.get("recheck"):
                # Another terminal event arrived while this answer was on its
                # way; it may be the real end. Ask once more.
                self._confirm_call_ended(
                    active, call_id, peer_jid, upgraded_to_video=bool(check.get("upgraded")))
                return
            logging.warning(
                "[call] ignored a terminal event: the page still holds this call "
                "live (state=%s engine=%s video=%s)",
                status.get("state"), status.get("engine"), bool(status.get("isVideo")),
            )
            # The event handler already set is_video from the event itself, so
            # the upgrade is carried here explicitly rather than re-derived.
            upgraded = bool(check.get("upgraded"))
            if status.get("isVideo") and not active.get("is_video"):
                active["is_video"] = True
                upgraded = True
            if upgraded:
                self._sync_voice_call_bar()
                self._on_call_upgraded_to_video()
            return
        self._end_active_call_locally(active, call_id, peer_jid)

    def _end_active_call_locally(self, active: dict, call_id: str, peer_jid: str):
        self._stop_voice_call_audio(grace_seconds=1.25)
        self.output(self.i18n.t("voice_call_ended"), interrupt=True)
        self._watch_ended_call_log(
            call_id, active.get("peer_jid") or peer_jid, bool(active.get("outgoing")))
        # The user has just been told the call is over. If the page still
        # holds it live, end it there too -- otherwise they are left in a call
        # with nothing on screen to hang up with.
        self._ensure_page_call_ended(call_id or active.get("call_id") or "")

    def _attach_audio_to_browser_call(self, details: dict):
        """Attach Python-owned audio/video when the page handled the answer."""
        with self._call_action_lock:
            if self._call_audio_session is not None:
                return
            try:
                if details.get("is_video"):
                    self._start_call_camera()
                self._raise_for_call_response(
                    self._post_call_control("audio/enable", {}, timeout=20),
                    "audio/enable",
                )
                self._start_voice_call_audio(
                    str(details.get("identity") or details.get("call_id") or "call"),
                    details,
                )
            except Exception:
                logging.exception("[call_audio] failed to attach to browser call")

    # How long a call record is re-read after the call it describes ended, and
    # how long a record still stored as Ongoing is followed (calls run long).
    _CALL_LOG_AFTER_END_WATCH_SECONDS = 10 * 60
    _CALL_LOG_PENDING_WATCH_SECONDS = 3 * 3600

    def _refresh_calls_tab(self):
        """Reload the Calls tab (debounced) when it is on screen."""
        calls = getattr(self, "calls_panel", None)
        if calls is not None:
            calls.schedule_refresh()

    def _watch_ended_call_log(self, call_id: str, peer_jid: str, outgoing: bool):
        """Re-read the call record of a call that just ended.

        WhatsApp keeps a call as a message whose id is the call id, and writes
        its outcome when the call ends -- either as a new record or as an
        update of the Ongoing one, which WPPConnect never forwards
        (core/call_log.py). One-to-one calls only: a group call's record is
        keyed on a participant too.
        """
        peer_jid = str(peer_jid or "")
        if not call_id or not peer_jid or peer_jid.endswith(("@g.us", "@broadcast")):
            return
        if str(call_id).startswith("outgoing:"):
            return  # WinZapp's own placeholder, never WhatsApp's call id
        # Call events name the peer in either form (core/call_matching.py),
        # and WhatsApp may have filed the record under the other one.
        peer_jid = self._normalize_jid(peer_jid)
        if peer_jid.endswith("@lid"):
            lid = peer_jid
            phone = getattr(self, "_lid_to_phone", {}).get(lid, "")
        else:
            phone = peer_jid
            lid = getattr(self, "_phone_to_lid", {}).get(phone, "")
        self._start_call_log_watch(
            call_log_candidate_ids(call_id, outgoing, [lid, phone]),
            self._CALL_LOG_AFTER_END_WATCH_SECONDS,
            chat_jid=phone or lid,
        )

    def _watch_pending_call_log(self, remote_jid: str, msg: dict):
        """Follow a stored call record whose outcome is not settled yet.

        Only a recent one: a record left Ongoing by a call long over (a group
        call nobody closed) is refreshed by the next sync of its chat, not
        polled for hours after every restart.
        """
        if not is_call_log_pending(msg):
            return
        try:
            ts = int(msg.get("messageTimestamp") or 0)
        except (TypeError, ValueError):
            ts = 0
        if ts <= 0 or time.time() - ts > self._CALL_LOG_PENDING_WATCH_SECONDS:
            return
        serialized = self._serialize_msg_id(remote_jid, msg.get("key") or {}, msg)
        if serialized:
            self._start_call_log_watch([serialized], self._CALL_LOG_PENDING_WATCH_SECONDS,
                                       chat_jid=remote_jid)

    def _start_call_log_watch(self, candidates: list, window_seconds: int, chat_jid: str = ""):
        """Poll message-by-id for a call record until its outcome is settled.

        A local read of WhatsApp Web's own store, bounded by *window_seconds*;
        every copy found goes through on_historical_message(), which inserts it
        or replaces the stored older state (call_log_supersedes()).

        Every copy is filed under *chat_jid*, the chat the record belongs in.
        WhatsApp keys it by whichever form it chose (usually the @lid), and
        on_historical_message() -- unlike on_new_message() -- does not bridge
        an @lid to its phone chat: it would open a second, nameless chat with
        the settled record while the real one kept "em andamento".
        """
        candidates = [c for c in (candidates or []) if c]
        if not candidates:
            return
        watched = self.__dict__.setdefault("_watched_call_logs", {})
        watch_key = candidates[0]
        state = watched.get(watch_key)
        if state is not None:
            # Already followed -- e.g. since the offer stopped ringing, and the
            # call has only now ended. Start its schedule over from here, or a
            # call longer than the first window would end unwatched.
            state["window"] = window_seconds
            state["restart"] = True
            return
        state = watched[watch_key] = {"window": window_seconds, "restart": False}

        def _worker():
            try:
                while True:
                    state["restart"] = False
                    for delay in call_log_refresh_delays(state["window"]):
                        time.sleep(delay)
                        if getattr(self, "_shutting_down", False):
                            return
                        if state["restart"]:
                            break
                        raw = self._fetch_call_log_record(candidates)
                        if raw is None:
                            continue
                        ws = getattr(self, "ws", None)
                        if ws is None:
                            return
                        normalized = ws._normalize_wpp_message(raw)
                        if not is_call_log(normalized):
                            return
                        refile_call_log(normalized, chat_jid)
                        wx.CallAfter(self.on_historical_message, normalized)
                        if not is_call_log_pending(normalized):
                            return
                    else:
                        if not state["restart"]:
                            return
            except Exception:
                logging.exception("[call_log] watch failed")
            finally:
                watched.pop(watch_key, None)

        threading.Thread(target=_worker, daemon=True, name="call-log-watch").start()

    def _fetch_call_log_record(self, candidates: list):
        """The raw WPPConnect message for the first id WhatsApp knows, or None.

        Ids are never logged: they carry the peer's JID (docs/traps/log-pii.md).
        """
        for serialized in candidates:
            url = (f"{self.wpp_server}:{self.wpp_port}/api/{self.token}"
                   f"/message-by-id/{_url_quote(serialized, safe='@_.:')}")
            try:
                response = api_get(url, token=self.token, timeout=10)
            except Exception as e:
                logging.info("[call_log] message-by-id unavailable: %s", type(e).__name__)
                return None
            if response.status_code >= 400:
                continue
            try:
                body = response.json()
            except ValueError:
                continue
            data = ((body or {}).get("response") or {}).get("data") if isinstance(body, dict) else None
            if isinstance(data, dict) and data.get("type") == "call_log":
                return data
        return None

    def on_incoming_call_event(self, event: dict):
        """Announce an incoming call and keep its tone playing until it ends.

        WhatsApp Web provides signaling while WinZapp owns the accessible call
        controls and Python audio. Duplicate offer events
        are expected because the Node bridge has both the public WA-JS event and
        a direct CallStore fallback.
        """
        if not isinstance(event, dict):
            return
        call_id = str(event.get("id") or "")
        peer_jid = self._normalize_jid(str(event.get("peerJid") or ""))
        event_name = str(event.get("event") or "offer").strip().lower()
        state = str(event.get("state") or "").strip().upper()
        is_ringing = state in self._CALL_RINGING_STATES and (
            bool(state) or event_name in ("offer", "ringing", "incoming")
        )

        if is_ringing:
            try:
                call_timestamp = int(event.get("timestamp") or 0)
            except (TypeError, ValueError):
                call_timestamp = 0
            try:
                session_started_at = float(getattr(self, "_wa_startup_time", 0) or 0)
            except (TypeError, ValueError):
                session_started_at = 0
            received_while_offline = bool(event.get("receivedWhileOffline", False))
            predates_session = (
                call_timestamp > 0
                and session_started_at > 0
                and call_timestamp
                < session_started_at - self._CALL_EVENT_START_GRACE_SECONDS
            )
            if received_while_offline or predates_session:
                logging.info(
                    "[incoming_call] ignoring historical offer id=%s "
                    "call_ts=%s session_started=%.0f offline=%s",
                    call_id,
                    call_timestamp,
                    session_started_at,
                    received_while_offline,
                )
                return

        # State changes away from INCOMING_RING mean the call was answered on
        # another device, rejected, missed, failed, or otherwise ended.
        if not is_ringing:
            # The record WhatsApp writes for it is what shows the call in the
            # conversation ("Ligação de voz perdida").
            self._watch_ended_call_log(call_id, peer_jid, False)
            if call_id:
                self._active_incoming_calls.pop(call_id, None)
                getattr(self, "_incoming_call_details", {}).pop(call_id, None)
                self._cancel_incoming_call_watchdog(call_id)
                close_dialog = getattr(self, "_close_incoming_call_dialog", None)
                if close_dialog is not None:
                    close_dialog(call_id)
            elif peer_jid:
                ended_ids = [
                    cid for cid, jid in self._active_incoming_calls.items()
                    if jid == peer_jid
                ]
                self._active_incoming_calls = {
                    cid: jid for cid, jid in self._active_incoming_calls.items()
                    if jid != peer_jid
                }
                for identity in ended_ids:
                    getattr(self, "_incoming_call_details", {}).pop(identity, None)
                    self._cancel_incoming_call_watchdog(identity)
                    close_dialog = getattr(self, "_close_incoming_call_dialog", None)
                    if close_dialog is not None:
                        close_dialog(identity)
            else:
                self._active_incoming_calls.clear()
                getattr(self, "_incoming_call_details", {}).clear()
                for identity in list(self._incoming_call_watchdogs):
                    self._cancel_incoming_call_watchdog(identity)
                for identity in list(getattr(self, "_incoming_call_dialogs", {})):
                    close_dialog = getattr(self, "_close_incoming_call_dialog", None)
                    if close_dialog is not None:
                        close_dialog(identity)
            if not self._active_incoming_calls:
                if hasattr(self, "call_incoming_sound"):
                    self.call_incoming_sound.stop()
            # Same rule as the two paths above: released once nothing that
            # could be answered is still ringing.
            if not self._has_answerable_incoming_call():
                self._stop_incoming_call_audio_monitor()
            self._sync_incoming_call_bar()
            return

        call_settings = getattr(self, "settings", {}).get("calls", {})
        if not call_settings.get("alerts_enabled", True):
            return

        identity = call_id or peer_jid
        if not identity or identity in self._active_incoming_calls:
            return
        # WPPConnect cannot answer a group call, so `is_group` keeps the Answer
        # button disabled (incoming_call_can_answer()). It must NOT silence the
        # offer: the page's own ringtone is muted by callMediaBridge, so
        # dropping the event here left a blind user with no signal at all that
        # their phone was ringing -- and no log line to explain it afterwards.
        # The alert follows the same two Calls settings as a one-to-one offer.
        group_jid = self._normalize_jid(str(event.get("groupJid") or ""))
        # A real group JID is required, not merely the isGroup flag. The Node
        # side now infers isGroup from participant count too
        # (groupParticipantCountOf(call) > 1 in createSessionUtil.ts), so the
        # flag can be asserted for a one-to-one call -- which is exactly why
        # on_voice_call_state_event's own group guard had to move inside its
        # "no active call" branch. Trusting it here costs more, not less: a
        # false positive would announce "incoming group call in Unnamed group"
        # instead of the caller's name AND disable the Answer button via
        # incoming_call_can_answer(), so the user simply could not answer a
        # real call from a friend. CLAUDE.md's rule for the other direction
        # applies here too: a @g.us is not trustworthy alone, and neither is
        # a group claim with no @g.us behind it.
        is_group = group_jid.endswith("@g.us") or peer_jid.endswith("@g.us")
        if event.get("isGroup") and not is_group:
            logging.info(
                "[incoming_call] isGroup asserted with no group JID; treating as "
                "individual id=%s peer=%s", call_id, peer_jid,
            )
        if is_group:
            chat = getattr(self, "chats", {}).get(group_jid, {}) if group_jid else {}
            group_name = self._group_name_from_chat_dict(chat) if chat else ""
            if not group_name and group_jid:
                group_name = getattr(self, "_group_name_cache", {}).get(group_jid, "")
            if not group_name:
                group_name = self.i18n.t("unknown_group")
            caller_name = group_name
            message = self.i18n.t("incoming_group_call_announcement").format(name=group_name)
        else:
            # Keep the proven one-to-one call path unchanged: peerJid is the
            # caller and resolves through the existing contact-name machinery.
            caller_name = self._preview_sender_from_jid(peer_jid) if peer_jid else ""
            if not caller_name:
                caller_name = self.i18n.t("unknown_contact")
            announcement_key = (
                "incoming_video_call_announcement" if event.get("isVideo")
                else "incoming_call_announcement"
            )
            message = self.i18n.t(announcement_key).format(name=caller_name)
        self._active_incoming_calls[identity] = peer_jid
        self._incoming_call_details[identity] = {
            "call_id": call_id,
            "peer_jid": peer_jid,
            "group_jid": group_jid,
            "is_video": bool(event.get("isVideo")),
            "is_group": is_group,
            "name": caller_name,
        }
        self._arm_incoming_call_watchdog(identity)
        self._incoming_call_details[identity]["message"] = message
        # Only for a call that CAN be answered: the monitor exists so accepting
        # can promote the same session to full duplex without reopening the
        # speaker, and a group offer never gets that far. Opening an output
        # stream for it would hold the device for nothing.
        if not is_group:
            self._start_incoming_call_audio_monitor(identity)
        self.output(message, interrupt=True)
        if hasattr(self, "call_incoming_sound"):
            self.call_incoming_sound.play()
        if call_settings.get("popup_enabled", True):
            show_popup = getattr(self, "_show_incoming_call_dialog", None)
            if show_popup is not None:
                show_popup(identity, message)
        self._sync_incoming_call_bar(message)
        logging.info(
            "[incoming_call] ringing id=%s peer=%s video=%s group=%s group_jid=%s",
            call_id, peer_jid, bool(event.get("isVideo")), is_group, group_jid,
        )
