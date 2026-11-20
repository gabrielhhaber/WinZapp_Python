"""VoiceNrSendMixin — part of ConversationsPanel (see ui/conversation_panel/__init__.py).

The noise-reduction step of a voice note's send thread when the audio was
not already cleaned while recording: which engine may run now (the basic
gate, the neural model, or none next to a native session that may still
hold its model), and — when the person chose to keep a note pending after a
neural failure — writing the original audio aside for a manual retry
(voice_nr_pending.py).
"""

import hashlib
import io
import logging
import os
import wave
import wx

from app_paths import data_path
from core.message_resend import RESEND_PATH_KEY
from core.utils import encrypt
from core.voice_nr_options import voice_nr_engine, voice_nr_on_failure


class VoiceNrSendMixin:
    """Send-time noise reduction of a recorded voice note."""

    def _reduce_for_send(self, mw, audio_data, rate, channels, basic_only,
                         on_failure="basic"):
        """Noise-reduction step of the send path.

        basic_only=True means the incremental slot was occupied (the model
        busy with the previous note) when this note was recorded: go
        STRAIGHT to the existing basic gate — no waiting and no second
        ONNX engine while the slot is occupied. basic_only=False is the
        published path unchanged: the neural model with the existing gate
        fallback inside reduce_voice_noise. If the chosen engine fails the
        note goes out unprocessed with the published announcement — there
        is no further escalation step. Returns (audio_data, engine) with
        engine None when both failed.

        on_failure="keep_pending" (the user's choice, only meaningful with
        basic_only=False) changes none of the processing — only the
        announcements: the caller keeps the note pending instead of sending
        the gate/unprocessed result, so neither "basic was used" nor
        "sent unprocessed" may be announced here.
        """
        keep_pending = on_failure == "keep_pending" and not basic_only
        try:
            if basic_only:
                logging.info("[VOICE_TIMING] Applying the basic noise gate "
                             "(incremental slot was occupied)...")
                from core.audio_processing import apply_noise_gate
                audio_data = apply_noise_gate(audio_data, rate, channels, strict=True)
                engine = "gate"
            else:
                logging.info("[VOICE_TIMING] Applying microphone noise reduction...")
                from core.neural_noise_reduction import reduce_voice_noise
                audio_data, engine = reduce_voice_noise(audio_data, rate, channels)
            logging.info("[VOICE_TIMING] Noise reduction engine: %s", engine)
            if engine != "neural" and not keep_pending:
                # Never let the plain gate pass for the neural model.
                announce = getattr(mw, "output", None)
                if announce is not None:
                    wx.CallAfter(announce, mw.i18n.t("noise_reduction_fallback"))
            return audio_data, engine
        except Exception as ex:
            logging.error("[VOICE_TIMING] Failed to apply noise reduction: %s", ex)
            if keep_pending:
                return audio_data, None
            # Both engines failed: the note goes out unprocessed, say so.
            announce = getattr(mw, "output", None)
            if announce is not None:
                wx.CallAfter(announce, mw.i18n.t("noise_reduction_failed"))
            return audio_data, None

    def _reduce_or_keep_pending(self, audio_data, channels, actual_rate, *,
                                inc_session, controller, drain_thread,
                                local_id, virtual_msg, forget):
        """Send thread: reduce *audio_data* for sending, or keep the note
        pending. Returns ``(True, audio)`` to go on with the send, or
        ``(False, None)`` when the note stays pending (or was revoked
        meanwhile) and the send stops here; *forget* drops the note's
        send-authority entry on the way out."""
        mw = self.main_window
        # Chosen-basic and temporarily-impossible-neural are NOT
        # the same state (review 18:28, contract 1): the gate is
        # the published fallback for both, but keep_pending only
        # exists for a user who chose NEURAL — a basic engine chosen
        # on purpose sends gated exactly as published.
        chosen_basic = voice_nr_engine(mw.settings) == "basic"
        session_blocked = inc_session is not None and (
            inc_session.get("basic_only", False)
            or inc_session.get("model_unavailable", False)
            or inc_session.get("fallback_chosen", False))
        on_failure = voice_nr_on_failure(mw.settings)
        want_pending = on_failure == "keep_pending" and not chosen_basic
        # No second ONNX next to a live native session: with
        # keep_pending, when the incremental side could not clean
        # this note and its model may still be loaded (load failed,
        # finish timed out, or the slot busy at record is STILL
        # busy), the send does NOT run the offline reduction — the
        # raw note goes straight to pending. A slot freed meanwhile
        # makes the posterior pass possible; if even that is not
        # neural, the note stays pending.
        skip_posterior = want_pending and inc_session is not None and (
            inc_session.get("model_unavailable", False)
            or inc_session.get("fallback_chosen", False)
            or (inc_session.get("basic_only", False)
                and controller is not None and controller.slot_busy()))
        raw_unprocessed = audio_data if want_pending else None
        reduce_basic_only = chosen_basic or (session_blocked and not want_pending)
        offline_reserved = False
        if (inc_session is not None and controller is not None
                and inc_session.get("worker") is None
                and drain_thread is None
                and not inc_session.get("basic_only", False)
                and not inc_session.get("model_unavailable", False)
                and not inc_session.get("fallback_chosen", False)):
            # moment=send from the start: this session NEVER started
            # incremental work, so no private model can be loaded and
            # the slot it holds is only send-side authority. The
            # caller confirms the cleanup HERE and releases its own
            # slot, so the reservation below stays a pure exclusion
            # (review 19:07: caller + confirmed cleanup, NEVER ID
            # equality alone — a retained slot after a native timeout
            # means the private model may still be alive).
            controller.release_slot(local_id)
        if not skip_posterior and not reduce_basic_only \
                and controller is not None:
            # The NORMAL offline pass takes the same reservation as
            # the retry (review 18:59, residual 1): while it loads /
            # runs the singleton, no capture starts a private model
            # next to it (acquire_slot refuses), and a capture
            # already holding the slot makes THIS note take the
            # occupied policy — published
            # gate, or pending when the user chose keep_pending.
            if controller.try_reserve_offline(local_id):
                offline_reserved = True
            elif want_pending:
                skip_posterior = True
            else:
                reduce_basic_only = True
        if skip_posterior:
            _nr_engine = None
            logging.info("[noise_reduction] exclusion busy (native "
                         "session may still hold its model or another "
                         "note owns the slot); no second ONNX pass")
        else:
            try:
                audio_data, _nr_engine = self._reduce_for_send(
                    mw, audio_data, actual_rate, channels,
                    reduce_basic_only,
                    on_failure=on_failure)
            finally:
                if offline_reserved:
                    controller.release_offline(local_id)
        logging.info("[VOICE_TIMING] Noise reduction finished (engine: %s)", _nr_engine)
        if raw_unprocessed is not None and _nr_engine != "neural":
            self._keep_note_pending(raw_unprocessed, channels, actual_rate,
                                    inc_session=inc_session, local_id=local_id,
                                    virtual_msg=virtual_msg, forget=forget)
            return False, None
        return True, audio_data

    def _keep_note_pending(self, raw_unprocessed, channels, actual_rate, *,
                           inc_session, local_id, virtual_msg, forget):
        """Send thread: write the ORIGINAL audio aside and leave the row
        pending for a manual "Try sending again" (voice_nr_pending.py)."""
        mw = self.main_window
        # Fourth option: BOTH neural processings failed (the
        # incremental one did not clean this note and the
        # at-send one failed or was impossible without a second
        # ONNX). Keep the note pending with its ORIGINAL audio
        # for a manual "Try sending again" — never gated, never
        # unprocessed, never automatic. Revoke arbitration
        # (contract 5): a delete of the pending row during the
        # processing or the write stops everything — no orphan
        # WAV, no late announcement on a dead row.
        if inc_session is not None and inc_session.get("revoked"):
            logging.info("[noise_reduction] send revoked before "
                         "the keep-pending write")
            forget()
            return
        logging.info("[noise_reduction] neural reduction failed; "
                     "keeping the note pending for a manual resend")
        pending_path = None
        try:
            os.makedirs(data_path("voice_messages"), exist_ok=True)
            pending_path = os.path.join(
                data_path("voice_messages"), local_id + ".resend.wav")
            # Encrypted at rest with the account key, like the .msv copy
            # of every recorded note: a kept note can sit on disk for days.
            # _read_nr_pending decrypts it.
            buffer = io.BytesIO()
            with wave.open(buffer, "wb") as wf:
                wf.setnchannels(channels)
                wf.setsampwidth(2)
                wf.setframerate(actual_rate)
                wf.writeframes(raw_unprocessed)
            with open(pending_path, "wb") as fh:
                fh.write(encrypt(buffer.getvalue(), mw.key))
        except Exception as exc:
            logging.error("[_send_voice_message] could not keep the "
                          "pending WAV: %s", exc)
            # Never leave a partial keep behind (review 18:47,
            # contract 5): the row falls to the save-failed
            # state with NO resend artifact at all.
            if pending_path is not None:
                try:
                    os.unlink(pending_path)
                except OSError:
                    pass
                pending_path = None
        kept = {}
        if pending_path is not None:
            kept[RESEND_PATH_KEY] = pending_path
            # Marks this row for the media-resend hook in
            # message_actions: "Try sending again" must RETRY
            # THE PROCESSING (_retry_nr_pending), not resend the
            # original audio unprocessed.
            kept["_nr_pending_retry"] = True
            # Fingerprint of what was kept: when it can be
            # recorded, the retry validates these exact bytes
            # (single stable read) before processing. An
            # unreadable file at keep time means NO fingerprint
            # — the check is then skipped, not claimed
            # mandatory (review 18:47).
            try:
                with open(pending_path, "rb") as fh:
                    kept["_nr_pending_sha256"] = hashlib.sha256(fh.read()).hexdigest()
                kept["_nr_pending_size"] = os.path.getsize(pending_path)
            except OSError:
                pass
        virtual_msg.update(kept)
        if pending_path is not None:
            announce_key = "noise_reduction_kept_pending"
        else:
            # The note stays FAILED but nothing was kept: never
            # announce a retry that cannot happen.
            announce_key = "noise_reduction_pending_save_failed"
        remote_jid = (virtual_msg.get("key") or {}).get("remoteJid", "")

        def _discard_kept(path=pending_path):
            if path is not None:
                try:
                    os.unlink(path)
                except OSError:
                    pass

        def _mark_kept_pending(key=announce_key):
            # UI thread — the same one deletions run on. The send
            # authority is dropped HERE, not on the send thread, so a
            # delete that reached revoke() before this point still wins:
            # the kept file dies and nothing is announced.
            if inc_session is not None and inc_session.get("revoked"):
                forget()
                _discard_kept()
                return
            forget()
            row = self._kept_pending_record(local_id, remote_jid)
            if row is None:
                # The row is gone, from the open chat and from its own.
                _discard_kept()
                return
            # A chat reloaded meanwhile may hold its own copy of the record.
            row.update(kept)
            for target in (virtual_msg, row):
                target.pop("_incremental_preparing", None)
            if self._nr_pending_row_alive(local_id):
                self._mark_message_failed(local_id)
            else:
                # The user moved to another chat meanwhile: the row is not
                # on screen, so _mark_message_failed() cannot see it. Mark
                # the stored record itself, or it stays "sending" forever
                # and the echo matcher may hand it another note's echo.
                row["_local_pending"] = False
                row["_send_failed"] = True
                self._outgoing_virtual_messages.pop(local_id, None)
                if remote_jid:
                    mw._schedule_save(dirty_jid=remote_jid)
                mw._schedule_set_chats()
            announce = getattr(mw, "output", None)
            if announce is not None:
                announce(mw.i18n.t(key))

        wx.CallAfter(_mark_kept_pending)

    def _kept_pending_record(self, local_id, remote_jid):
        """UI thread: the dict of the kept-pending note — the open chat's
        row, else the record in its own chat (the user may have switched
        chats while the reduction ran) — or None when it was deleted."""
        for m in self._sorted_messages:
            if isinstance(m, dict) and (m.get("key") or {}).get("id") == local_id:
                return m
        chat = self.main_window.get_chat(remote_jid) if remote_jid else None
        position = self._record_position(chat, local_id)
        if position < 0:
            return None
        return chat["messages"]["messages"]["records"][position]
