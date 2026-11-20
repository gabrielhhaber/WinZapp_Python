"""VoiceNrPendingMixin — part of ConversationsPanel (see ui/conversation_panel/__init__.py).

A voice note kept pending after its neural noise reduction failed (Settings >
Audio, "if neural reduction fails": keep pending) and the explicit actions on
it: "Try sending again" re-runs the neural reduction, "Send without noise
reduction" sends the original, and a processed copy that later failed in
transport is resent as is. Every action holds a per-note attempt token on the
panel and hands the audio to the ordinary voice-note queue
(_nr_pending_handoff). The note is kept pending by voice_nr_send.py.
"""

import hashlib
import io
import logging
import os
import tempfile
import threading
import time
import uuid
import wave
import wx

from app_paths import data_path
from core.message_queue import PendingMessage
from core.message_resend import RESEND_PATH_KEY
from core.pending_timestamp import pending_send_timestamp
from core.utils import encrypt
from core.voice_stereo import STEREO_MIMETYPE
from ui.conversation_panel.typing_row import append_message_row


class VoiceNrPendingMixin:
    """Kept-pending voice notes and the actions that send them."""

    def _nr_pending_row_alive(self, local_id):
        """UI-thread liveness of the kept-pending row (delete wins races)."""
        if not local_id:
            return True
        return any(
            isinstance(m, dict) and m.get("key", {}).get("id") == local_id
            for m in self._sorted_messages)

    # --- Fourth-option attempt authority (review 18:47, contract 2) ---
    # An attempt is registered per NOTE (local_id) on the PANEL, never as
    # a flag on the row dict: a row rehydrated as a new dict with the
    # same id cannot slip a second attempt past a per-dict flag, and a
    # panel destroy invalidates every live attempt even when the message
    # list survives the destroy.

    def _nr_attempt_begin(self, local_id, remote_jid):
        """Take attempt authority for a kept-pending/handoff-produced
        note. Returns the attempt token, or None when the panel is dead
        or an attempt is already live for this note — said aloud then, so
        a second press is never a key that silently does nothing. The
        token pins the attempt's context (this panel, this conversation);
        both are re-validated on the UI thread before any handoff."""
        if getattr(self, "_nr_panel_dead", False):
            return None
        registry = getattr(self, "_nr_attempts", None)
        if registry is None:
            registry = self._nr_attempts = {}
        if local_id in registry:
            logging.info("[noise_reduction] an action on this pending note "
                         "is already in flight; ignoring the extra request")
            mw = self.main_window
            mw.output(mw.i18n.t("noise_reduction_retry_busy"), interrupt=True)
            return None
        token = uuid.uuid4().hex
        registry[local_id] = (token, remote_jid)
        return token

    def _nr_attempt_end(self, local_id, token):
        registry = getattr(self, "_nr_attempts", None)
        if registry is not None and registry.get(local_id, (None,))[0] == token:
            registry.pop(local_id, None)

    def _nr_attempt_state(self, local_id, token, remote_jid):
        """UI-thread re-validation before touching widgets.

        "alive": the panel is alive, THIS attempt is still the current one
        for the note, its conversation is open and the row is there.
        "elsewhere": the same, except the user opened another conversation
        meanwhile and the note's record still lives in its own chat — the
        outcome is still worth saying, but no row may be touched.
        "dead": panel destroyed, attempt superseded, or the note deleted.
        """
        if getattr(self, "_nr_panel_dead", False):
            return "dead"
        registry = getattr(self, "_nr_attempts", None)
        if registry is None or registry.get(local_id, (None,))[0] != token:
            return "dead"
        mw = self.main_window
        open_jid = (getattr(self, "conversation", None) or {}).get("remoteJid")
        # An @lid row in a chat stored under its phone JID is the same
        # conversation: a raw string compare took it for a switch.
        if open_jid and not mw._chat_jids_equivalent(open_jid, remote_jid):
            chat = mw.get_chat(remote_jid)
            return "elsewhere" if self._record_position(chat, local_id) >= 0 else "dead"
        return "alive" if self._nr_pending_row_alive(local_id) else "dead"

    def _nr_attempt_context_alive(self, local_id, token, remote_jid):
        return self._nr_attempt_state(local_id, token, remote_jid) == "alive"

    def _nr_invalidate_attempts(self):
        """Panel destroy / teardown: every live attempt loses its
        context. In-flight workers still finish their native call, but
        their UI dispatch observes the dead panel and cleans up quietly —
        never a callback into destroyed controls (review 18:47)."""
        self._nr_panel_dead = True
        registry = getattr(self, "_nr_attempts", None)
        if registry is not None:
            registry.clear()

    def _nr_pending_announce(self, local_id, token, remote_jid, key):
        """End the attempt and announce — unless the note was deleted or
        the panel died meanwhile (checked on the UI thread, where
        deletions run). A note whose chat is no longer open is still
        announced: the outcome is about a note the user acted on."""
        mw = self.main_window

        def _do():
            state = self._nr_attempt_state(local_id, token, remote_jid)
            self._nr_attempt_end(local_id, token)
            if state != "dead":
                mw.output(mw.i18n.t(key), interrupt=True)
        wx.CallAfter(_do)

    @staticmethod
    def _nr_note_id(msg):
        """The id a kept-pending or handoff-produced row is tracked under."""
        return str(msg.get("_local_id") or (msg.get("key") or {}).get("id") or "")

    @staticmethod
    def _stage_temp_wav(data, rate, channels):
        """Worker: write *data* to a new temp WAV. Returns its path, or None
        when it could not be written — no partial file is left behind."""
        tmp_name = None
        try:
            tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            tmp_name = tmp.name
            tmp.close()
            with wave.open(tmp_name, "wb") as wf:
                wf.setnchannels(channels)
                wf.setsampwidth(2)
                wf.setframerate(rate)
                wf.writeframes(data)
            return tmp_name
        except Exception as exc:
            logging.error("[noise_reduction] could not stage the pending "
                          "voice note: %s", exc)
            if tmp_name is not None:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
            return None

    def _read_nr_pending(self, msg, path):
        """Fingerprint-checked read of a kept-pending WAV.

        ONE stable copy: the bytes are read a single time and the size /
        sha256 check AND the WAV parse both run over THAT copy — a file
        replaced between two opens can never pass validation with one
        content and be processed as another (review 18:47). The
        fingerprint applies WHEN one was recorded at keep time; a note
        without it is processed unchecked — the fingerprint is not
        claimed mandatory. Returns (pcm, rate, channels, seconds); raises
        on any mismatch or unreadable file — the caller announces and
        keeps the row pending.
        """
        expected_size = msg.get("_nr_pending_size")
        expected_hash = msg.get("_nr_pending_sha256")
        with open(path, "rb") as fh:
            raw = fh.read()
        if expected_size is not None and len(raw) != expected_size:
            raise ValueError("kept note size changed on disk")
        if expected_hash is not None and \
                hashlib.sha256(raw).hexdigest() != expected_hash:
            raise ValueError("kept note content changed on disk")
        with wave.open(io.BytesIO(raw), "rb") as wf:
            channels = wf.getnchannels()
            rate = wf.getframerate()
            if wf.getsampwidth() != 2:
                raise ValueError("unexpected sample width")
            seconds = int(wf.getnframes() / float(rate)) if rate else 0
            data = wf.readframes(wf.getnframes())
        return data, rate, channels, seconds

    def _nr_pending_handoff(self, msg, remote_jid, wav_path,
                            channels, rate, seconds, token,
                            fail_key="noise_reduction_kept_pending"):
        """Hand a fourth-option note's audio to the voice-note pipeline.

        Runs on a worker thread; ends in a UI-thread dispatch. The audio
        goes out AS A VOICE NOTE — same encode the recording would use
        (OGG Opus mono / AAC M4A stereo), ptt, the recording's channels,
        the original reply quote — never as a generic file attachment.

        Honest handoff (review 18:47, contracts 3-5): the REPRESENTATION
        is registered before the queue is activated, so even a
        synchronous echo finds the row — never a late duplicate pending;
        a rejected enqueue rolls that representation back. The kept
        original and the old row are dropped only AFTER the queue
        accepted the copy (queued LOCALLY, not delivered). The new row
        carries RESEND_PATH_KEY to the persisted copy and a
        processed-voice marker, so a later transport failure resends
        THOSE bytes as a voice note again (_resend_processed_voice) —
        never re-reduced, never a generic attachment. The copy's
        ownership then passes to the send path (recording_path reaches
        _on_message_sent; its exact cleanup moment is not asserted here).
        Every exit without delivery removes ALL artifacts this handoff
        created (staged WAV, persisted copy, .msv playback copy) — never
        the valid kept original.
        """
        mw = self.main_window
        local_id = self._nr_note_id(msg)
        # Encode off the UI thread exactly like the recording path.
        ogg_bytes = None
        voice_mimetype = ""
        if channels >= 2:
            ogg_bytes = self._encode_stereo_voice(wav_path)
            if not ogg_bytes:
                try:
                    os.unlink(wav_path)
                except OSError:
                    pass
                self._nr_pending_announce(local_id, token, remote_jid, fail_key)
                return
            voice_mimetype = STEREO_MIMETYPE
        else:
            ogg_path = None
            try:
                ogg_path = mw._convert_wav_to_ogg(wav_path)
                if ogg_path and os.path.isfile(ogg_path):
                    with open(ogg_path, "rb") as f_in:
                        ogg_bytes = f_in.read()
            except Exception as exc:
                logging.warning("[noise_reduction] pending handoff OGG "
                                "pre-encode failed: %s", exc)
                ogg_bytes = None
            finally:
                # The converter's temp OGG dies here either way — a read
                # failure must not leave it behind (review 18:59).
                if ogg_path:
                    try:
                        os.unlink(ogg_path)
                    except OSError:
                        pass
        # A persistent home for the copy: the new row's RESEND_PATH_KEY
        # points at it; remove_messages_by_id disposes of it with its row.
        new_local_id = str(uuid.uuid4())
        persisted = None
        msv_path = None
        try:
            os.makedirs(data_path("voice_messages"), exist_ok=True)
            persisted = os.path.join(
                data_path("voice_messages"), new_local_id + ".resend.wav")
            os.replace(wav_path, persisted)
        except OSError as exc:
            logging.error("[noise_reduction] could not persist the pending "
                          "handoff: %s", exc)
            try:
                os.unlink(wav_path)
            except OSError:
                pass
            self._nr_pending_announce(local_id, token, remote_jid, fail_key)
            return
        # Local playback copy, same encryption as any recorded note — and
        # the same payload: the encoded bytes, or the raw PCM when the OGG
        # pre-encode was unavailable (never the WAV container). A partial
        # write dies here, not as an orphan (review 18:47, contract 5).
        try:
            with wave.open(persisted, "rb") as wf:
                pcm_bytes = wf.readframes(wf.getnframes())
            msv_path = os.path.join(
                data_path("voice_messages"), new_local_id + ".msv")
            with open(msv_path, "wb") as f_out:
                f_out.write(encrypt(ogg_bytes or pcm_bytes, mw.key))
        except Exception as exc:
            logging.warning("[noise_reduction] could not save the local "
                            "copy of the pending handoff: %s", exc)
            if msv_path is not None:
                try:
                    os.unlink(msv_path)
                except OSError:
                    pass
                msv_path = None
        # Fingerprint of the PROCESSED copy: its own later resend
        # validates it with the same single-stable-read rule. Skipped
        # quietly when unreadable — never claimed mandatory.
        try:
            with open(persisted, "rb") as fh:
                processed_raw = fh.read()
            processed_hash = hashlib.sha256(processed_raw).hexdigest()
            processed_size = len(processed_raw)
        except OSError:
            processed_hash = processed_size = None
        quoted = None
        ctx = msg.get("contextInfo")
        if isinstance(ctx, dict) and ctx.get("stanzaId"):
            quoted = {"key": {"id": ctx.get("stanzaId", ""),
                              "participant": ctx.get("participant", ""),
                              "fromMe": bool(ctx.get("_quotedFromMe", False)),
                              "remoteJid": remote_jid},
                      "message": ctx.get("quotedMessage") or {}}
        pm = PendingMessage(new_local_id, remote_jid,
                            audio_path=persisted,
                            ogg_bytes=ogg_bytes, quoted=quoted,
                            voice_mimetype=voice_mimetype)

        def _cleanup_artifacts():
            # Every artifact THIS handoff created — never the valid kept
            # original, which the old row still owns.
            for artifact in (persisted, msv_path):
                if artifact:
                    try:
                        os.unlink(artifact)
                    except OSError:
                        pass

        def _dispatch():
            # UI thread. The attempt's context must still be alive: a
            # deleted row or a destroyed panel ends the attempt quietly and
            # the staged artifacts die here. A switched conversation ends
            # it too (the new row would land in the wrong chat), but the
            # user is told the note stayed pending in its own chat.
            state = self._nr_attempt_state(local_id, token, remote_jid)
            if state != "alive":
                self._nr_attempt_end(local_id, token)
                _cleanup_artifacts()
                if state == "elsewhere":
                    mw.output(mw.i18n.t("noise_reduction_retry_chat_changed"),
                              interrupt=True)
                return
            new_msg = {
                "_local_pending": True,
                "_local_id": new_local_id,
                "_is_voice_recording": True,
                # Handoff product: a later "Try sending again" on THIS row
                # resends these exact bytes as a voice note
                # (_resend_processed_voice) — never re-reduced, never a
                # generic attachment (review 18:47, contract 4).
                "_nr_processed_voice": True,
                "key": {"id": new_local_id, "fromMe": True,
                        "remoteJid": remote_jid},
                "messageType": "audioMessage",
                "message": {"audioMessage": {
                    "seconds": seconds,
                    "ptt": True,
                    **({"mimetype": "audio/mp4",
                        "fileName": new_local_id + ".m4a"}
                       if channels >= 2 else {})}},
                "messageTimestamp": pending_send_timestamp(
                    time.time(), self._sorted_messages, quoted),
                "pushName": "",
                RESEND_PATH_KEY: persisted,
            }
            if processed_hash is not None:
                new_msg["_nr_pending_sha256"] = processed_hash
                new_msg["_nr_pending_size"] = processed_size
            if quoted:
                new_msg["contextInfo"] = ctx
            # Representation BEFORE queue activation (review 18:47,
            # contract 3): enqueue() wakes the sender, and an echo that
            # arrives synchronously must already find the row and its
            # virtual registration — or it lands as a late duplicate
            # pending row.
            self._clear_empty_placeholder()
            self._sorted_messages.append(new_msg)
            append_message_row(self, self._render_message_line(new_msg))
            self._register_virtual_msg(new_msg)
            try:
                mw.message_queue.enqueue(pm)
            except Exception as exc:
                logging.error("[noise_reduction] could not queue the pending "
                              "handoff: %s", exc)
                # Roll the representation back, then the artifacts; the
                # old row stays pending, untouched.
                self._outgoing_virtual_messages.pop(new_local_id, None)
                self.remove_messages_by_id({new_local_id}, focus_previous=False)
                self._nr_attempt_end(local_id, token)
                _cleanup_artifacts()
                mw.output(mw.i18n.t(fail_key), interrupt=True)
                return
            # The queue owns the copy now — queued LOCALLY, not delivered:
            # the new row reads as an ordinary pending voice note. Only now
            # does the old failed row go away, taking the kept ORIGINAL
            # with it.
            old_local_id = str(msg.get("_local_id") or "")
            old_ids = {i for i in (old_local_id,
                                   str((msg.get("key") or {}).get("id") or "")) if i}
            if old_local_id:
                mw.message_queue.cancel(old_local_id)
                self._outgoing_virtual_messages.pop(old_local_id, None)
            if old_ids:
                self.remove_messages_by_id(old_ids, focus_previous=True)
            if msg.get("_nr_processed_voice"):
                # The old row was itself a handoff product: its local
                # playback cache (.msv) is still named after the LOCAL id
                # (only a DELIVERED row's cache is renamed to the real
                # WhatsApp id) and remove_messages_by_id does not collect
                # it — it dies with its row here, or a second handoff
                # leaves it orphaned (review 18:59). Valid caches of rows
                # that stay are never touched.
                from ui.conversation_panel.media_paths import \
                    discard_local_media_cache
                discard_local_media_cache(
                    data_path("voice_messages"), data_path("media"),
                    old_local_id)
            self._nr_attempt_end(local_id, token)
            mw._schedule_set_chats()
            mw.output(mw.i18n.t("resend_started"), interrupt=True)

        wx.CallAfter(_dispatch)

    def _retry_nr_pending(self, msg, remote_jid, path):
        """Retry the PROCESSING of a fourth-option kept-pending note.

        The user chose voice_nr_on_failure=keep_pending: when both neural
        processings failed, the note stayed pending instead of going out
        with the basic gate or unprocessed. "Try sending again" therefore
        re-runs the NEURAL reduction on the kept original (off the UI
        thread) and hands the PROCESSED audio to the ordinary voice-note
        pipeline (_nr_pending_handoff). Audited contracts (reviews 18:28
        and 18:47), all local:

        - Never automatic: only this explicit user action runs it.
        - Attempt authority per NOTE, registered on the panel with a
          token + context (panel, conversation), re-validated on the UI
          thread before any handoff: ONE live attempt per note — a second
          action, even from a rehydrated row dict with the same id, is
          ignored; a panel destroy invalidates the attempt.
        - Never the gate: engine != "neural" leaves the row pending,
          untouched. (Sending the ORIGINAL is a separate, equally
          explicit user action: _send_nr_pending_raw.)
        - One model at a time, by RESERVATION not probe: the retry
          reserves the offline side under the controller's lock before
          waiting on the singleton's inference lock, and acquire_slot()
          refuses a capture while the reservation stands — the exclusion
          is effective in both directions (review 18:47, contract 1).
        - Immutability: the kept file is read ONCE and must still match
          the size/sha256 recorded when it was kept (when a fingerprint
          was recorded at all), or nothing is processed.
        - Delete wins: the attempt's context is re-checked on the UI
          thread before any announcement and before the handoff.
        - Destination/account: identical to the existing attachment
          resend (the row's own remoteJid through this panel's live
          session); no new destination surface.
        """
        mw = self.main_window
        local_id = self._nr_note_id(msg)
        token = self._nr_attempt_begin(local_id, remote_jid)
        if token is None:
            return
        # The reduction can take a while: say it started.
        mw.output(mw.i18n.t("noise_reduction_retry_started"), interrupt=True)

        def _work():
            controller = getattr(mw, "_incremental_controller", None)
            reserved = False
            if controller is not None:
                if not controller.try_reserve_offline(local_id):
                    logging.info("[noise_reduction] kept-pending retry "
                                 "deferred: incremental slot or another "
                                 "offline pass busy")
                    self._nr_pending_announce(local_id, token, remote_jid,
                                              "noise_reduction_kept_pending")
                    return
                reserved = True
            try:
                try:
                    data, rate, channels, seconds = self._read_nr_pending(msg, path)
                except Exception as exc:
                    logging.warning("[noise_reduction] kept-pending retry could "
                                    "not read %s: %s", path, exc)
                    self._nr_pending_announce(local_id, token, remote_jid,
                                              "resend_not_available")
                    return
                engine = None
                try:
                    from core.neural_noise_reduction import reduce_voice_noise
                    data, engine = reduce_voice_noise(data, rate, channels)
                except Exception as exc:
                    logging.info("[noise_reduction] kept-pending retry reduction "
                                 "failed again: %s", exc)
                if engine != "neural":
                    self._nr_pending_announce(local_id, token, remote_jid,
                                              "noise_reduction_kept_pending")
                    return
            finally:
                # The model is no longer in use past the reduction: the
                # reservation ends here, before staging/handoff.
                if reserved:
                    controller.release_offline(local_id)
            tmp_name = self._stage_temp_wav(data, rate, channels)
            if tmp_name is None:
                self._nr_pending_announce(local_id, token, remote_jid,
                                          "noise_reduction_kept_pending")
                return
            self._nr_pending_handoff(msg, remote_jid, tmp_name,
                                     channels, rate, seconds, token)

        threading.Thread(target=_work, daemon=True).start()

    def _stage_pending_and_handoff(self, msg, remote_jid, path, local_id,
                                   token, fail_key):
        """Worker: read the kept/processed WAV (single stable copy,
        fingerprint validated when one was recorded), stage it as a temp
        WAV and hand it to the voice-note pipeline. Shared by the raw
        send of a kept-pending note and the resend of an already
        processed one."""
        def _work():
            try:
                data, rate, channels, seconds = self._read_nr_pending(msg, path)
            except Exception as exc:
                logging.warning("[noise_reduction] pending voice send could "
                                "not read %s: %s", path, exc)
                self._nr_pending_announce(local_id, token, remote_jid,
                                          "resend_not_available")
                return
            tmp_name = self._stage_temp_wav(data, rate, channels)
            if tmp_name is None:
                self._nr_pending_announce(local_id, token, remote_jid, fail_key)
                return
            self._nr_pending_handoff(msg, remote_jid, tmp_name,
                                     channels, rate, seconds, token,
                                     fail_key=fail_key)

        threading.Thread(target=_work, daemon=True).start()

    def _send_nr_pending_raw(self, msg, remote_jid, path):
        """Send a fourth-option kept-pending note AS RECORDED — no
        reduction at all. The other half of Fran's 18:35 decision: the
        choice between reprocessing and sending the original is ALWAYS
        the user's, action by action; nothing ever sends the raw audio on
        its own. Same per-note attempt authority, single-stable-read
        fingerprint check, delete-wins and voice-note handoff as the
        reprocessing retry. No model is touched, so the incremental slot
        does not gate this path.
        """
        local_id = self._nr_note_id(msg)
        token = self._nr_attempt_begin(local_id, remote_jid)
        if token is None:
            return
        self._stage_pending_and_handoff(msg, remote_jid, path, local_id,
                                        token, "noise_reduction_kept_pending")

    def _resend_processed_voice(self, msg, remote_jid, path):
        """"Try sending again" on a note that ALREADY went through the
        pipeline (a handoff product marked _nr_processed_voice): resend
        THOSE bytes as a voice note — ptt, quote, channels — never
        re-reduced and never through the generic attachment path (review
        18:47, contract 4). Same per-note attempt authority and handoff
        as every other fourth-option action."""
        local_id = self._nr_note_id(msg)
        token = self._nr_attempt_begin(local_id, remote_jid)
        if token is None:
            return
        self._stage_pending_and_handoff(msg, remote_jid, path, local_id,
                                        token, "resend_not_available")
