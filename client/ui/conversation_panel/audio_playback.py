"""AudioPlaybackMixin — part of ConversationsPanel (see ui/conversation_panel/__init__.py).

Moved verbatim out of ui/conversations.py. Methods run with ``self`` bound to
the ConversationsPanel instance, so every attribute set in
ConversationsPanel.__init__/init_UI is available here.
"""

import logging
import os
import sound_lib.stream as sl_stream
import tempfile
import threading
import wx
from sound_lib.effects import Tempo
from app_paths import data_path
from core.utils import (
    decrypt_bytes,
    is_voice_message,
)
from core.audio_transcode import transcode_audio_to_wav


class AudioPlaybackMixin:
    """Playing voice/audio messages: play/pause, chaining, speed, seek and the
    audio controls.
    """

    # ── Audio / video playback ──────────────────────────────────────────────

    def toggle_current_audio_playback(self):
        """Ctrl+Alt+Shift+P: pause/resume whatever voice or audio message is
        currently loaded, without needing a specific message/row — unlike
        _toggle_playback(), which always needs one to know what to switch
        TO. A no-op when nothing is loaded (nothing paused, nothing to
        resume)."""
        if self._current_audio_id is None or self._audio_stream is None:
            return
        _ctrl = self._audio_tempo_ctrl if self._audio_tempo_ctrl is not None else self._audio_stream
        if self._is_audio_playing:
            try:
                _ctrl.pause()
            except Exception:
                # The stream is dead (e.g. the output device was switched in
                # Settings while this was playing — BASS_Free()/BASS_Init()
                # during that switch invalidates it), not "already paused".
                # Reopen fresh rather than leaving _is_audio_playing=False
                # pointed at a channel that will also fail the next play().
                if self._recover_audio_stream_after_device_switch():
                    self._is_audio_playing = True
                    self._audio_timer.Start(30)
                else:
                    self._stop_audio()
                return
            self._is_audio_playing = False
            self._audio_timer.Stop()
            # Same as the pause in _toggle_playback(): a paused sequence moves
            # no focus, so nothing it parked has a reason to stay unwritten.
            self._release_chain_held_repaints()
        else:
            try:
                _ctrl.play()
            except Exception:
                if self._recover_audio_stream_after_device_switch():
                    self._is_audio_playing = True
                    self._audio_timer.Start(30)
                else:
                    self._stop_audio()
                return
            self._is_audio_playing = True
            self._audio_timer.Start(30)

    def _toggle_playback(self, msg_id, duration_seconds, msg, file_path, audio_ext):
        """
        Generic play/pause toggle for both audio messages (voice_messages/)
        and video messages (media/).
        """
        # Same item: toggle play / pause
        if msg_id == self._current_audio_id and self._audio_stream is not None:
            _ctrl = self._audio_tempo_ctrl if self._audio_tempo_ctrl is not None else self._audio_stream
            if self._is_audio_playing:
                try:
                    _ctrl.pause()
                except Exception:
                    # Distinguish "not playing yet" (BASS's own report if the
                    # user switches messages faster than the backend updates
                    # state — harmless) from a genuinely dead channel (the
                    # output device was switched in Settings while this was
                    # playing, which frees + reinits BASS and invalidates
                    # every stream that existed before it). The latter must
                    # reopen fresh, or the next play() attempt on the same
                    # dead object fails too — reported live as "doesn't play
                    # the first time after switching output device, only the
                    # second".
                    if self._recover_audio_stream_after_device_switch():
                        self._is_audio_playing = True
                        self._audio_timer.Start(30)
                    else:
                        self._stop_audio()
                    return
                self._is_audio_playing = False
                self._audio_timer.Stop()
                # A paused sequence moves no focus and plays nothing a row
                # read aloud could talk over: what it parked is written now
                # instead of staying stale for as long as the pause lasts. The
                # next voice note to end arms the hold again.
                self._release_chain_held_repaints()
            else:
                try:
                    _ctrl.play()
                except Exception:
                    if self._recover_audio_stream_after_device_switch():
                        self._is_audio_playing = True
                        self._audio_timer.Start(30)
                    else:
                        self._stop_audio()
                    return
                self._is_audio_playing = True
                self._audio_timer.Start(30)
            return

        # Save position of the outgoing audio before the stream is destroyed so
        # the user can resume it later if they come back to that message.
        if self._current_audio_id is not None and self._audio_stream is not None:
            try:
                _ctrl = self._audio_tempo_ctrl if self._audio_tempo_ctrl is not None else self._audio_stream
                pos   = _ctrl.get_position()
                total = _ctrl.get_length()
                if 0 < pos < total:
                    self._audio_positions[self._current_audio_id] = pos
            except Exception:
                pass
        self._stop_audio()

        if os.path.isfile(file_path):
            self._play_audio(msg_id, duration_seconds, file_path, audio_ext)
        else:
            if not getattr(self.main_window, "_wa_connected", False):
                # Attempting the HTTP call while disconnected/still-connecting
                # just burns the request timeout for a guaranteed failure —
                # refuse up front with a message that tells the user why,
                # instead of "baixando..." followed by a generic failure a
                # minute later.
                self.main_window.output(self.main_window.i18n.t("media_download_offline"))
                return
            if not hasattr(self, "_downloading_audio_ids"):
                self._downloading_audio_ids = set()

            if msg_id in self._downloading_audio_ids:
                self.main_window.output(self.main_window.i18n.t("downloading"))
                return

            logging.info(f"[UI Audio Playback] File not found local, launching download thread. file_path={file_path}")
            self.main_window.output(self.main_window.i18n.t("downloading"))
            self._downloading_audio_ids.add(msg_id)

            def _download_and_play():
                try:
                    msg_type = msg.get("messageType", "") if msg else ""
                    try:
                        if msg_type == "audioMessage":
                            if msg is not None:
                                logging.info(f"[UI Audio Playback] Calling handle_audio_message for {msg_id}")
                                self.main_window.handle_audio_message(msg)
                        else:
                            if msg is not None:
                                logging.info(f"[UI Audio Playback] Calling handle_media_message for {msg_id}")
                                self.main_window.handle_media_message(msg)
                    except Exception as e:
                        logging.warning(
                            "[_download_and_play] download failed for %s: %s", msg_id, e,
                            exc_info=True,
                        )
                    # Only play if the file was actually downloaded (non-empty)
                    exists = os.path.isfile(file_path)
                    size = os.path.getsize(file_path) if exists else 0
                    logging.info(f"[UI Audio Playback] Finished download try. exists={exists}, size={size}")
                    if exists and size > 16:
                        wx.CallAfter(
                            self._play_audio, msg_id, duration_seconds, file_path, audio_ext
                        )
                    else:
                        # Download silently failed (timeout, expired CDN link,
                        # WPPConnect error) — the user's last feedback was
                        # "baixando..." with no follow-up; tell them it failed
                        # instead of leaving that as the final word.
                        wx.CallAfter(
                            self.main_window.output,
                            self.main_window.i18n.t("media_download_failed"),
                        )
                finally:
                    self._downloading_audio_ids.discard(msg_id)

            threading.Thread(target=_download_and_play, daemon=True).start()

    def _open_audio_stream_from_temp_file(self):
        """Open a fresh BASS stream on the already-decrypted
        self._audio_temp_file, wrapped in Tempo FX (enables speed control).

        A decoded stream (BASS_STREAM_DECODE) cannot be played directly; it
        must be wrapped by a BASS FX processor such as Tempo. If the FX
        plugin is unavailable, falls back to a plain stream without the
        effect. A method rather than a local closure inside _play_audio() so
        the toggle-play recovery paths (_toggle_playback(),
        toggle_current_audio_playback()) can reopen a fresh stream too — an
        output device switch (Settings) frees + reinits BASS, invalidating
        whatever stream/Tempo control was already loaded, and resuming that
        SAME dead object on the next play() always failed silently: only
        _play_audio() (a brand new message) reopened a fresh stream, so
        toggling play/pause on the message that was already loaded when the
        device switch happened never recovered — reported live as "doesn't
        play the first time after switching output device, only the
        second" (the second attempt worked only once _current_audio_id had
        been reset by some other path, landing back on _play_audio()).
        """
        try:
            s = sl_stream.FileStream(file=self._audio_temp_file, decode=True)
            tempo = Tempo(s)
            _speed = self._audio_speed_steps[self._audio_speed_index]
            tempo.tempo = self._audio_tempo_map.get(_speed, 0)
            return s, tempo
        except Exception as e:
            logging.info(f"[UI Audio Playback] Decode/Tempo stream failed ({e}), falling back to direct stream: {self._audio_temp_file}")
            return sl_stream.FileStream(file=self._audio_temp_file), None

    def _recover_audio_stream_after_device_switch(self) -> bool:
        """Reopen self._audio_stream/_audio_tempo_ctrl from the still-valid
        decrypted temp file and resume playback near where it was.

        Called when a play()/pause() on the currently loaded stream raises
        outside of _play_audio() (which already handles this) — see
        _open_audio_stream_from_temp_file()'s docstring for why that
        happens. Returns whether it succeeded; a caller whose recovery
        fails should fall back to _stop_audio() so state doesn't stay
        pointed at a permanently dead stream.
        """
        if not self._audio_temp_file or not os.path.isfile(self._audio_temp_file):
            return False
        old_ctrl = self._audio_tempo_ctrl if self._audio_tempo_ctrl is not None else self._audio_stream
        pos = None
        if old_ctrl is not None:
            try:
                pos = old_ctrl.get_position()
            except Exception:
                pos = None
        try:
            self._audio_stream, self._audio_tempo_ctrl = self._open_audio_stream_from_temp_file()
            playback_ctrl = self._audio_tempo_ctrl if self._audio_tempo_ctrl is not None else self._audio_stream
            if pos:
                try:
                    playback_ctrl.set_position(pos)
                except Exception:
                    pass
            playback_ctrl.play()
        except Exception as e:
            logging.exception(f"[UI Audio Playback] Recovery after device switch failed: {e}")
            return False
        return True

    def _play_audio(self, msg_id, duration_seconds, file_path, audio_ext=".ogg"):
        if not os.path.isfile(file_path):
            return

        # This can be reached two ways: synchronously from _toggle_playback
        # (file already local), or via wx.CallAfter from a background download
        # thread once a fetch finishes. The two can interleave — the user
        # taps audio A (triggers a download), then before it finishes taps
        # already-local audio B, which plays immediately; A's download then
        # completes and lands here. Without stopping whatever is currently
        # playing first, this unconditionally overwrote _audio_stream/
        # _audio_temp_file with A's — leaking B's still-running BASS channel
        # (its reference was just overwritten, so _stop_audio() could never
        # reach it again) and its decrypted temp file (never unlinked) every
        # single time this race happened.
        if self._audio_stream is not None and self._current_audio_id != msg_id:
            self._stop_audio()

        # ── Decrypt and write to a temp file ────────────────────────────────
        try:
            with open(file_path, "rb") as fh:
                content = decrypt_bytes(fh.read(), self.main_window.key)
            logging.info(
                f"[UI Audio Playback] Decrypted {len(content)} bytes. "
                f"Header hex: {content[:16].hex()} "
                f"(OGG magic = 4f676753, Opus head = 4f707573)"
            )
            if content.startswith(b"RIFF"):
                actual_ext = ".wav"
            elif content.startswith(b"ID3") or (len(content) > 2 and content[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")):
                actual_ext = ".mp3"
            elif b"ftyp" in content[:32]:
                actual_ext = ".m4a"
            elif content.startswith(b"OggS"):
                actual_ext = ".ogg"
            else:
                actual_ext = audio_ext
            tmp = tempfile.NamedTemporaryFile(suffix=actual_ext, delete=False)
            tmp.write(content)
            tmp.close()
            self._audio_temp_file = tmp.name
            if actual_ext == ".m4a":
                wav_path = transcode_audio_to_wav(
                    self.main_window._find_api_ffmpeg(),
                    self._audio_temp_file,
                )
                if wav_path:
                    os.unlink(self._audio_temp_file)
                    self._audio_temp_file = wav_path
                    logging.info(
                        "[UI Audio Playback] Converted MP4/M4A audio to WAV: %s",
                        wav_path,
                    )
                else:
                    logging.warning(
                        "[UI Audio Playback] MP4/M4A audio could not be converted; "
                        "BASS playback may be unavailable"
                    )
        except Exception as e:
            logging.exception(f"[UI Audio Playback] Error decrypting or creating temp audio file: {e}")
            self._stop_audio()
            return

        try:
            self._audio_stream, self._audio_tempo_ctrl = self._open_audio_stream_from_temp_file()
        except Exception as e:
            # Both the decode+Tempo stream and the plain direct stream failed
            # (_open_audio_stream_from_temp_file()'s own fallback) — e.g. an
            # OGG whose codec isn't Opus, or whose bassopus.dll plugin failed
            # to register, which BASS rejects for both attempts with error 41
            # "unsupported file format". Re-encode through ffmpeg to PCM WAV,
            # which sidesteps BASS's codec support entirely, and retry once
            # from that file rather than giving up on the message.
            logging.info(
                "[UI Audio Playback] Direct stream also failed (%s); "
                "trying ffmpeg WAV fallback for %s", e, self._audio_temp_file,
            )
            wav_path = transcode_audio_to_wav(
                self.main_window._find_api_ffmpeg(),
                self._audio_temp_file,
            )
            if wav_path is None:
                logging.exception(f"[UI Audio Playback] Error creating stream: {e}")
                self._stop_audio()
                return
            os.unlink(self._audio_temp_file)
            self._audio_temp_file = wav_path
            try:
                self._audio_stream, self._audio_tempo_ctrl = self._open_audio_stream_from_temp_file()
            except Exception as e2:
                logging.exception(
                    f"[UI Audio Playback] Error creating stream from converted WAV: {e2}"
                )
                self._stop_audio()
                return

        # ── Start playback ───────────────────────────────────────────────────
        # When Tempo FX is active the decode stream has no audio output of its
        # own; playback must be started on the Tempo wrapper instead.
        self._audio_stream_duration = int(duration_seconds)
        self._current_audio_id = msg_id
        self._audio_conv_jid   = (
            self.conversation.get("remoteJid", "") if self.conversation else ""
        )
        playback_ctrl = self._audio_tempo_ctrl if self._audio_tempo_ctrl is not None else self._audio_stream
        # Restore saved position (e.g. when another audio preempted this one)
        saved_pos = self._audio_positions.pop(msg_id, None)
        if saved_pos:
            try:
                playback_ctrl.set_position(saved_pos)
            except Exception:
                pass

        try:
            playback_ctrl.play()
        except Exception as e:
            logging.exception(f"[UI Audio Playback] Error starting playback: {e}")
            # The output device may have just been switched (Settings, or a
            # fallback at startup) — BASS_Free()/BASS_Init() during that
            # switch invalidates the stream we just tried to play on (a
            # confirmed BASS behaviour, not a one-off glitch), so retrying
            # play() on the SAME playback_ctrl would fail again with the
            # same error. Reopen a fresh stream from the same (still valid)
            # decrypted temp file instead, and play that.
            if self.main_window.sound_system.handle_playback_failure():
                try:
                    self._audio_stream, self._audio_tempo_ctrl = self._open_audio_stream_from_temp_file()
                    playback_ctrl = (
                        self._audio_tempo_ctrl if self._audio_tempo_ctrl is not None else self._audio_stream
                    )
                    if saved_pos:
                        try:
                            playback_ctrl.set_position(saved_pos)
                        except Exception:
                            pass
                    playback_ctrl.play()
                except Exception as e2:
                    logging.exception(f"[UI Audio Playback] Retry after device fallback also failed: {e2}")
                    self._stop_audio()
                    return
            else:
                self._stop_audio()
                return

        self._is_audio_playing = True
        self._audio_timer.Start(30)
        # Show controls only if the playing message is currently focused in the list.
        _speed = self._audio_speed_steps[self._audio_speed_index]
        if self._focused_msg_id() == msg_id:
            self._show_audio_controls()
            self.audio_speed_btn.SetLabel(self._format_speed(_speed))

    def _stop_audio(self):
        # A manual stop (or a different audio taking over) invalidates any
        # still-pending auto-chain timers — never let them start a stale audio.
        self._cancel_pending_chain_timers()
        if self._audio_timer.IsRunning():
            self._audio_timer.Stop()
        # Stop the Tempo FX controller first (it owns the audio output channel)
        if self._audio_tempo_ctrl is not None:
            try:
                self._audio_tempo_ctrl.stop()
            except Exception:
                pass
            self._audio_tempo_ctrl = None
        if self._audio_stream is not None:
            try:
                self._audio_stream.stop()
            except Exception:
                pass
            self._audio_stream = None
        self._is_audio_playing = False
        self._current_audio_id = None
        if not getattr(self, "_in_auto_chain_transition", False) and not getattr(self, "_in_auto_timer_stop", False):
            self._is_in_audio_chain = False
            # The sequence is over (user stopped it, started a different audio,
            # or left the conversation): no further focus move is coming, so
            # the rows held back during it are safe to write.
            self._release_chain_held_repaints()
        if self._audio_temp_file and os.path.exists(self._audio_temp_file):
            try:
                os.unlink(self._audio_temp_file)
            except Exception:
                pass
            self._audio_temp_file = None

    def _stop_playback_for_removed_messages(self, msg_ids: set):
        """Stop any in-app audio/video playback belonging to a message that
        is about to disappear from the list — deleted locally, deleted for
        everyone, mass-deleted, or mirrored in from a phone-side deletion
        the periodic poll picked up (MainWindow._mirror_remote_deletions()).

        Audio is allowed to keep playing in the background while the user
        scrolls/selects elsewhere (see _hide_all_media_controls()'s own
        comment), so it's matched purely by _current_audio_id — not by
        whether its row is currently focused or even still loaded in
        _sorted_messages (pagination can scroll it out of view while it
        keeps playing). Video (in-app playback via Enter, core/video_player.py
        — a live ffmpeg subprocess) is matched by _current_video_msg_id the
        same way; before this, remove_messages_by_id() never checked either
        one at all, so deleting a message that was actively playing left it
        looping/streaming with no row left in the UI to stop it from.
        """
        if self._current_audio_id in msg_ids and self._audio_stream is not None:
            self._stop_audio()
            self._hide_audio_controls()
        if self._current_video_msg_id in msg_ids:
            self._hide_all_media_controls()

    def on_message_revoked(self, msg_id: str):
        """A message was deleted for everyone by its sender, detected live
        (see MainWindow._apply_remote_revoke()). The official client swaps
        it for "Mensagem apagada" instantly, including stopping playback if
        you were mid-listen — WinZapp used to leave the original audio/
        video/text/media on screen (and audio/video still playing) until
        the next periodic remote-deletion poll, which only removes the row
        outright rather than marking it deleted, and can take a while to
        even notice.
        """
        if msg_id:
            self._stop_playback_for_removed_messages({msg_id})
        if msg_id and self._focused_msg_id() == msg_id:
            self._hide_all_media_controls()
        # Only the revoked message's own row changes text (it keeps its row —
        # a revoke protocolMessage is still displayable), so re-rendering every
        # row of the conversation for it was disproportionate.
        if not self._repaint_message_rows([msg_id]):
            self.refresh_active_conversation_messages()

    def on_audio_timer(self, event):
        if self._current_video_msg_id is not None:
            if not self._video_player.is_playing:
                # Reached EOF or was stopped elsewhere (e.g. _hide_all_media_
                # controls() already cleared this — belt and suspenders for
                # any path that stops the player without going through it).
                self._current_video_msg_id = None
                self._hide_audio_controls()
                return
            try:
                pos   = self._video_player.get_position()
                total = self._video_player.get_length()
                if total > 0:
                    self.audio_slider.SetValue(int(pos / total * 1000))
                    self.audio_slider.Refresh()
            except Exception:
                pass
            return
        if self._audio_stream is None:
            return
        try:
            _ctrl = self._audio_tempo_ctrl if self._audio_tempo_ctrl is not None else self._audio_stream
            pos   = _ctrl.get_position()
            total = _ctrl.get_length()
            if total > 0:
                if pos >= total:
                    # Save the ID/message before _stop_audio() clears it
                    finished_id  = self._current_audio_id
                    finished_msg = next(
                        (m for m in self._sorted_messages
                         if m.get("key", {}).get("id") == finished_id),
                        None,
                    )
                    self._in_auto_timer_stop = True
                    try:
                        self._stop_audio()
                    finally:
                        self._in_auto_timer_stop = False
                    self._hide_audio_controls()
                    # Reaching the end of playback — right where the controls
                    # get hidden — is "played" for a received voice message:
                    # mark it locally and tell WhatsApp so the sender sees it
                    # too. See MainWindow.mark_audio_message_played()'s own
                    # docstring for why this never applies to our own sends.
                    will_chain = bool(finished_id) and self._next_message_is_chainable_audio(finished_id)
                    if will_chain:
                        # Armed HERE, before anything can queue a row repaint —
                        # not inside _auto_chain_next_audio() below. The played
                        # receipt this send-off triggers echoes back from
                        # WhatsApp onto on_message_status_update() on its own
                        # schedule, and that path knows nothing about the chain;
                        # the hold is what catches it. See
                        # _release_chain_held_repaints().
                        self._hold_status_repaints_until_chain_ends()
                    if finished_msg is not None:
                        self.main_window.mark_audio_message_played(
                            finished_msg,
                            # See mark_audio_message_played()'s own docstring:
                            # when the chain is about to move focus onto the
                            # next voice note, the row refresh is held back
                            # and fired by _auto_chain_next_audio() itself
                            # right after that focus move actually happens —
                            # not on a fixed timeout guess, which in practice
                            # could still lose the race against however long
                            # the chain's own transition actually takes.
                            skip_panel_refresh=will_chain,
                        )
                    # Try to auto-play the next consecutive audio message
                    if finished_id:
                        self._auto_chain_next_audio(
                            finished_id,
                            pending_played_msg_id=finished_id if will_chain else None,
                        )
                    return
                self.audio_slider.SetValue(int(pos / total * 1000))
                self.audio_slider.Refresh()
        except Exception:
            pass

    def _cancel_pending_chain_timers(self):
        """Cancel any still-scheduled auto-chain wx.CallLater timers.

        The chain schedules _play_next/_start_audio (and _play_end) with
        wx.CallLater; if the user stops playback, starts a different audio, or
        navigates away before those fire, the pending timers would otherwise
        still run and start audio from an earlier point in the sequence.
        """
        for attr in ("_chain_play_timer", "_chain_start_timer", "_chain_end_timer"):
            timer = getattr(self, attr, None)
            if timer is not None:
                try:
                    timer.Stop()
                except Exception:
                    pass
                setattr(self, attr, None)
        # A "played" row refresh _auto_chain_next_audio() deferred onto the
        # (now-cancelled) chain step must still happen — otherwise the row
        # is left showing stale status until something unrelated refreshes
        # it. It just can no longer wait for the focus move that isn't
        # going to happen any more, so it fires right here instead.
        pending = getattr(self, "_pending_played_refresh_id", None)
        if pending:
            self._pending_played_refresh_id = None
            self.refresh_message_status(pending, "5")

    def _voice_sequence_enabled(self) -> bool:
        """The "play voice messages in sequence" setting (Settings > Interface).
        On by default; off, a voice note ends without starting the next one."""
        return bool(
            self.main_window.settings.get("user_interface", {}).get(
                "auto_play_voice_in_sequence", True
            )
        )

    def _next_message_is_chainable_audio(self, finished_id: str) -> bool:
        """Read-only peek at what _auto_chain_next_audio(finished_id) is
        about to do: True if it will auto-play a next voice note and move
        list focus onto it. Mirrors that method's own eligibility checks
        without any side effect — used by on_audio_timer() to decide whether
        the "played" row refresh for finished_id needs to be delayed (see
        the call site's comment)."""
        if not self._voice_sequence_enabled():
            return False
        current_jid = self.conversation.get("remoteJid", "") if self.conversation else ""
        if current_jid != self._audio_conv_jid:
            return False
        current_idx = -1
        finished_msg = None
        for i, msg in enumerate(self._sorted_messages):
            if not self._is_separator(msg) and msg.get("key", {}).get("id") == finished_id:
                current_idx = i
                finished_msg = msg
                break
        if current_idx < 0 or finished_msg is None or not self._is_voice_message(finished_msg):
            return False
        next_idx = current_idx + 1
        while next_idx < len(self._sorted_messages):
            candidate = self._sorted_messages[next_idx]
            if self._is_separator(candidate):
                next_idx += 1
                continue
            return candidate.get("messageType") == "audioMessage" and self._is_voice_message(candidate)
        return False

    def _auto_chain_next_audio(self, finished_id: str, pending_played_msg_id: str = None):
        """
        After an audio message finishes playing, automatically start the next
        consecutive audio message if one exists immediately after in the list.
        Stops at the first non-audio (or separator) message.

        pending_played_msg_id: when on_audio_timer() skipped the "played" row
        refresh for finished_id (see its own call site comment), this is
        finished_id again — this method fires that refresh itself, exactly
        once, at whichever point it's actually safe: right after the chain
        moves focus onto the next voice note if it does, or immediately if it
        turns out there's nothing to chain into after all.

        Note this ordering is NOT what keeps the screen reader quiet, and it
        never was — refresh_message_status() only queues the row and starts a
        coalescing timer, so the write lands well after this callback returns,
        and a played receipt echoing back from WhatsApp can write the same row
        without passing through here at all. _release_chain_held_repaints() is
        what actually guarantees no row is rewritten while the chain is moving
        focus; this just keeps the queued refresh from being dropped.
        """
        # Cancel any timers left over from a previous chain step before
        # scheduling new ones — a stale timer must never start audio after
        # the user has already stopped/jumped elsewhere. Also flushes
        # whatever pending "played" refresh that previous step was carrying
        # (see _cancel_pending_chain_timers()'s own comment), so it never
        # gets silently dropped by being overwritten below.
        self._cancel_pending_chain_timers()
        self._pending_played_refresh_id = pending_played_msg_id

        def _flush_pending_played_refresh():
            pending = self._pending_played_refresh_id
            if pending:
                self._pending_played_refresh_id = None
                self.refresh_message_status(pending, "5")

        def _end_without_chaining():
            # The sequence stops here with no focus move left to stay clear
            # of. Releasing in these early exits too is what keeps the hold
            # from outliving a sequence the user walked away from.
            _flush_pending_played_refresh()
            self._release_chain_held_repaints()

        # Don't chain if the user has navigated to a different conversation —
        # _sorted_messages belongs to the current conversation, not the one
        # where the audio was playing.
        current_jid = self.conversation.get("remoteJid", "") if self.conversation else ""
        if current_jid != self._audio_conv_jid:
            _end_without_chaining()
            return

        # Find the index of the just-finished message
        current_idx = -1
        finished_msg = None
        for i, msg in enumerate(self._sorted_messages):
            if not self._is_separator(msg) and msg.get("key", {}).get("id") == finished_id:
                current_idx = i
                finished_msg = msg
                break
        if current_idx < 0 or finished_msg is None:
            _end_without_chaining()
            return

        # Sequential playback and transition sounds ONLY apply to voice notes (PTT),
        # not to generic attached audio/music files.
        if not self._is_voice_message(finished_msg):
            self._is_in_audio_chain = False
            _end_without_chaining()
            return

        # The user turned sequential playback off (Settings > Interface): the
        # sequence ends with the voice note that just finished.
        if not self._voice_sequence_enabled():
            self._is_in_audio_chain = False
            _end_without_chaining()
            return

        # Walk forward, skipping separators, to find the next message
        next_idx = current_idx + 1
        has_next_audio = False
        target_msg = None
        target_idx = -1
        while next_idx < len(self._sorted_messages):
            candidate = self._sorted_messages[next_idx]
            if self._is_separator(candidate):
                next_idx += 1
                continue
            if candidate.get("messageType") == "audioMessage" and self._is_voice_message(candidate):
                has_next_audio = True
                target_msg = candidate
                target_idx = next_idx
            break

        if has_next_audio and target_msg is not None:
            self._is_in_audio_chain = True
            # Normally already armed by on_audio_timer(); repeated here so a
            # direct caller of this method gets the same protection.
            self._hold_status_repaints_until_chain_ends()
            def _play_next():
                snd = getattr(self.main_window, "audio_transition_next_sound", None)
                if snd is not None:
                    try:
                        snd.play()
                    except Exception as e:
                        logging.exception(f"[UI Audio Chaining] Error playing audio_transition_next_sound: {e}")

                def _start_audio():
                    msg_id   = target_msg.get("key", {}).get("id", "")
                    duration = (
                        (target_msg.get("message") or {}).get("audioMessage") or {}
                    ).get("seconds", 0) or 0
                    # Only move list focus to the next audio when the user is
                    # EXCLUSIVELY focused on the audio that just finished
                    # playing (current_idx). If they've moved focus one row
                    # above/below (or anywhere else) while listening, keep the
                    # chain playing but never steal their focus back. And
                    # regardless of that, respect the user's own preference
                    # (Settings > Interface) to never have the chain move
                    # focus at all — audio keeps auto-advancing either way,
                    # this only controls whether the list selection follows it.
                    auto_focus = self.main_window.settings.get("user_interface", {}).get(
                        "auto_focus_next_audio", True
                    )
                    current_focus = self.messages_list.GetFocusedItem()
                    if auto_focus and current_focus == current_idx:
                        # Told to the hold before the move, not after: the row
                        # stops being "the focused one" the instant Focus()
                        # returns, and must stay parked all the same.
                        self._chain_moving_focus_off(finished_id)
                        self.messages_list.Focus(target_idx)
                        self.messages_list.Select(target_idx, True)
                        self.messages_list.EnsureVisible(target_idx)
                    # Queue the finished row's "played" refresh. It will not
                    # be written now if the chain moved focus off it or it
                    # still has focus: the hold armed for this chain parks it
                    # (other rows are written as usual) until
                    # _release_chain_held_repaints() runs at the end. Trying to
                    # win the race by ordering the two events here is what used
                    # to be attempted, and it could not work — see that
                    # method's docstring for the measurements.
                    wx.CallAfter(_flush_pending_played_refresh)
                    clean_msg_id = msg_id
                    if "_" in msg_id:
                        parts = msg_id.split("_")
                        clean_msg_id = parts[2] if len(parts) > 2 else parts[-1]
                    self._in_auto_chain_transition = True
                    try:
                        self._toggle_playback(
                            msg_id, duration, target_msg,
                            file_path=data_path("voice_messages", f"{clean_msg_id}.msv"),
                            audio_ext=".ogg",
                        )
                    finally:
                        self._in_auto_chain_transition = False
                self._chain_start_timer = wx.CallLater(100, _start_audio)
            self._chain_play_timer = wx.CallLater(100, _play_next)
        else:
            # No next voice note to chain into — nothing else is ever going
            # to move focus away from the finished row, so the "played"
            # refresh (if any) is safe to fire right now, and every repaint
            # held back during the sequence can finally be written.
            _flush_pending_played_refresh()
            self._release_chain_held_repaints()
            if getattr(self, "_is_in_audio_chain", False):
                def _play_end():
                    snd = getattr(self.main_window, "audio_transition_end_sound", None)
                    if snd is not None:
                        try:
                            snd.play()
                        except Exception as e:
                            logging.exception(f"[UI Audio Chaining] Error playing audio_transition_end_sound: {e}")
                    self._is_in_audio_chain = False
                self._chain_end_timer = wx.CallLater(100, _play_end)
            else:
                self._is_in_audio_chain = False

    def _is_voice_message(self, msg: dict) -> bool:
        """Return True if msg is a voice note (PTT / mensagem de voz), not a generic audio file."""
        return is_voice_message(msg)


    def on_audio_speed_btn(self, event):
        self._audio_speed_index = (self._audio_speed_index + 1) % len(
            self._audio_speed_steps
        )
        self._apply_audio_speed()

    def _on_audio_speed_decrease(self, event):
        """Alt+, — step down one speed level (wraps at minimum)."""
        if self._audio_speed_index > 0:
            self._audio_speed_index -= 1
            self._apply_audio_speed()

    def _on_audio_speed_increase(self, event):
        """Alt+. — step up one speed level (wraps at maximum)."""
        if self._audio_speed_index < len(self._audio_speed_steps) - 1:
            self._audio_speed_index += 1
            self._apply_audio_speed()

    def _apply_audio_speed(self):
        """Apply the current speed index to the active stream and persist it."""
        speed = self._audio_speed_steps[self._audio_speed_index]
        self.audio_speed_btn.SetLabel(self._format_speed(speed))
        if self._current_video_msg_id is not None and self._video_player.is_playing:
            self._video_player.set_speed(speed)
        elif self._audio_tempo_ctrl is not None:
            try:
                self._audio_tempo_ctrl.tempo = self._audio_tempo_map[speed]
            except Exception:
                pass
        self.main_window.settings.setdefault("audio_playback", {})["audio_default_speed"] = speed
        self.main_window.save_settings()

    def on_audio_slider(self, event):
        if self._current_video_msg_id is not None and self._video_player.is_playing:
            try:
                val   = self.audio_slider.GetValue()
                total = self._video_player.get_length()
                if total > 0:
                    self._video_player.set_position(int(val / 1000 * total))
            except Exception:
                pass
            return
        if self._audio_stream is None:
            return
        try:
            # Seek on the same control that's actually playing and that
            # on_audio_timer() reads position back from — when Tempo FX is
            # active, _audio_stream is a decode-only source with no direct
            # audio output; playback runs through _audio_tempo_ctrl instead.
            # Setting position on the raw decode stream still "worked" in
            # that it eventually reached the new position, but only once
            # Tempo's own already-decoded-ahead buffer finished draining
            # first — reported live as audio taking a long time to resume
            # after a slider seek.
            _ctrl = self._audio_tempo_ctrl if self._audio_tempo_ctrl is not None else self._audio_stream
            val   = self.audio_slider.GetValue()
            total = _ctrl.get_length()
            _ctrl.set_position(int(val / 1000 * total))
        except Exception:
            pass

    def _has_active_audio_or_video(self) -> bool:
        if self._current_video_msg_id is not None and self._video_player.is_playing:
            return True
        return self._audio_stream is not None

    def seek_active_playback_by(self, delta_seconds: float) -> bool:
        """Seek the currently playing voice message or video by *delta_seconds*
        (negative = backward), clamped to [0, length - 1]. Returns False when
        nothing is playing, so callers (keyboard shortcuts) can fall through
        to their normal behavior instead. Issue #17.

        The upper clamp is `length - 1`, not `length`: landing exactly on the
        reported length is what BASS_ChannelSetPosition can reject with a
        BassError for a decoded stream (issue #305, see
        seek_active_playback_to_edge()'s own docstring) — seeking far enough
        forward (Shift+Right/PageDown near the end of a track) used to hit
        that same boundary and silently do nothing instead of clamping to the
        end."""
        if self._current_video_msg_id is not None and self._video_player.is_playing:
            try:
                total = self._video_player.get_length()
                if total <= 0:
                    return False
                pos = self._video_player.get_position()
                delta_bytes = self._video_player.seconds_to_bytes(abs(delta_seconds))
                if delta_seconds < 0:
                    delta_bytes = -delta_bytes
                new_pos = max(0, min(total - 1, pos + delta_bytes))
                self._video_player.set_position(new_pos)
                return True
            except Exception:
                return False
        if self._audio_stream is None:
            return False
        try:
            _ctrl = self._audio_tempo_ctrl if self._audio_tempo_ctrl is not None else self._audio_stream
            total = _ctrl.get_length()
            if total <= 0:
                return False
            pos = _ctrl.get_position()
            delta_bytes = _ctrl.seconds_to_bytes(abs(delta_seconds))
            if delta_seconds < 0:
                delta_bytes = -delta_bytes
            new_pos = max(0, min(total - 1, pos + delta_bytes))
            _ctrl.set_position(new_pos)
            return True
        except Exception:
            return False

    def seek_active_playback_to_edge(self, to_end: bool) -> bool:
        """Seek the currently playing voice message or video to its very
        start (to_end=False) or end (to_end=True). Issue #17.

        Landing exactly on the reported length is what BASS_ChannelSetPosition
        can reject with a BassError for a decoded stream (issue #305: Shift+End
        raised, was swallowed by the except below, and the keypress fell
        through to "select to end of list" instead of seeking) — Shift+Home's
        `0` never hits that edge case, which is why only one direction failed.
        `total - 1` is one BASS byte short of the true end, imperceptible to a
        listener, and never hands BASS the rejected boundary value.

        The video branch goes through the same kind of BASS channel
        underneath (video_player._audio_ctrl()), but video_player.set_position()
        already swallows that exception itself and returns silently — so it
        never actually hit issue #305's symptom (the failure never reached
        this method's own except, and this method kept returning True). Its
        bug was a different, quieter one: the seek to the exact end silently
        did nothing while still reporting success. The same clamp fixes that
        too, so it gets it for consistency, not because it shared the
        reported bug."""
        if self._current_video_msg_id is not None and self._video_player.is_playing:
            try:
                total = self._video_player.get_length()
                if total <= 0:
                    return False
                self._video_player.set_position(max(0, total - 1) if to_end else 0)
                return True
            except Exception:
                return False
        if self._audio_stream is None:
            return False
        try:
            _ctrl = self._audio_tempo_ctrl if self._audio_tempo_ctrl is not None else self._audio_stream
            total = _ctrl.get_length()
            if total <= 0:
                return False
            _ctrl.set_position(max(0, total - 1) if to_end else 0)
            return True
        except Exception:
            return False

    def _show_audio_controls(self):
        self.audio_speed_btn.Show()
        self.audio_progress_label.Show()
        self.audio_slider.Show()
        self.conversation_panel.Layout()

    def _hide_audio_controls(self):
        focused = wx.Window.FindFocus()
        audio_ctrls = (
            getattr(self, "audio_speed_btn", None),
            getattr(self, "audio_slider", None),
            getattr(self, "audio_progress_label", None),
        )
        if focused is not None and any(focused == c for c in audio_ctrls if c is not None):
            if hasattr(self, "messages_list") and self.messages_list.IsShown():
                self.messages_list.SetFocus()
        self.audio_speed_btn.Hide()
        self.audio_progress_label.Hide()
        self.audio_slider.Hide()
        if self.conversation_panel.IsShown():
            self.conversation_panel.Layout()

    def _format_speed(self, speed):
        sep = self.main_window.i18n.t("decimal_separator")
        return f"{speed:.1f}".replace(".", sep) + "×"
