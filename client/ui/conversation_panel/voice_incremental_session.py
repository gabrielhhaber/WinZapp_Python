"""VoiceIncrementalSessionMixin — part of ConversationsPanel (see ui/conversation_panel/__init__.py).

Neural noise reduction WHILE a voice note is recorded (Settings > Audio,
neural engine, "while recording"): the recording's incremental session is
started here when the capture opens, its drain thread cleans the frames as
they arrive into a temporary spool, and a discard cancels it. The send side
of the session lives in core/incremental_send.py; the wx-free engine and the
slot/send-authority controller in core/incremental_denoise.py.
"""

import logging
import os
import tempfile
import threading
import uuid

from core.voice_nr_options import voice_nr_engine, voice_nr_moment
from core.voice_recording_mode import prepare_voice_pcm


class VoiceIncrementalSessionMixin:
    """The recording-time incremental noise-reduction session."""

    def _start_incremental_denoise(self, frames_list, rate, ch):
        """Start cleaning the recording through the model as frames arrive.

        Storage strategy A: the raw frames stay in memory exactly as before
        and the cleaned PCM16 is spooled to a temp file off the UI thread,
        so recording-time RAM matches the current profile; the engine itself
        retains no per-recording growth. Any failure marks the session
        failed and _write_and_enqueue falls back to the existing offline
        path (that fallback also pays the abandoned session's spent work —
        no worst-case claim until measured). Explicit-mono mode averages the
        two captured channels per chunk BEFORE the model, mirroring the
        offline prepare_voice_pcm step; automatic mode's empty-side drop is
        resolved at send time from the cleaned spool (the model handles
        channels independently, so selecting one cleaned side is bit-exact).

        One incremental slot per process (== per account): the slot is taken
        without waiting BEFORE any model load or thread start; when another
        incremental operation holds it, this recording keeps the current
        offline path. The session's local_id is generated here so the
        controller's send-authority registry can key it from the start.
        """
        # A previous session that was never cancelled (a path that missed
        # _cancel_incremental_denoise) must not be orphaned: its drain would
        # spin on a dead frames list forever.
        previous = getattr(self, "_incremental_session", None)
        if previous is not None:
            previous["cancel"] = True
            controller = getattr(self.main_window, "_incremental_controller", None)
            if controller is not None:
                controller.discard(previous.get("local_id", ""))
        self._incremental_session = None
        try:
            if not self.main_window.settings.get("general", {}).get("noise_reduction_enabled", False):
                return
            # The incremental pass only runs for the neural engine while
            # recording. Neural at send time (default) and the basic engine
            # keep the published flow: no session, no slot, no registry, and
            # the send path reduces offline.
            if (voice_nr_engine(self.main_window.settings) != "neural"
                    or voice_nr_moment(self.main_window.settings) != "record"):
                return
            controller = getattr(self.main_window, "_incremental_controller", None)
            if controller is None:
                return
            mode = getattr(self, "_recording_channel_mode", "auto")
            worker_ch = 1 if (mode == "mono" and ch == 2) else ch
            # Identity first: EVERY note gets its own close confirmation,
            # send authority, cancel and echo exclusion — the neural slot
            # only decides whether a worker/drain/spool exist at all.
            session = {
                "local_id": str(uuid.uuid4()),
                "frames": frames_list, "rate": rate, "ch": ch, "worker_ch": worker_ch,
                "worker": None, "thread": None,
                "stop": False, "cancel": False, "failed": False, "revoked": False,
                # Per-session close confirmation, set by
                # _stop_recording_stream: the send thread waits on it
                # before using the raw frames, so the last callback chunks
                # cannot land after the audio was joined. Kept INSIDE the
                # session: a panel attribute would be overwritten by the
                # next recording while a previous send still waits on it.
                "stopped_event": threading.Event(),
                "device_failed": False,
                # Set when the neural slot is occupied by a previous note:
                # the send goes STRAIGHT to the existing basic gate — no
                # wait, no second ONNX engine while the slot is occupied.
                "basic_only": False,
            }
            if not controller.acquire_slot(session["local_id"], session):
                # Slot busy (typically a previous note's drain/finish that
                # has not yet confirmed exit/cleanup): no model, no thread,
                # no spool — but the note KEEPS its registered identity.
                controller.register(session["local_id"], session)
                session["basic_only"] = True
                self._incremental_session = session
                logging.info("[noise_reduction] incremental slot busy; recording falls back to the basic gate")
                return
            try:
                # EVERYTHING after acquire_slot and before the drain owns
                # the session must clean up on failure: a temp-file error
                # (disk/permissions) or a thread start error must not leak
                # the slot and the send-authority registry entry.
                tmp = tempfile.NamedTemporaryFile(prefix="winzapp-inc-", suffix=".pcm", delete=False)
                tmp.close()
                session["spool"] = tmp.name
                thread = threading.Thread(target=self._incremental_drain, args=(session,), daemon=True)
                session["thread"] = thread
                self._incremental_session = session
                thread.start()
            except Exception:
                self._incremental_session = None
                controller.release_slot(session["local_id"])
                controller.forget(session["local_id"])
                spool = session.get("spool")
                if spool:
                    try:
                        os.unlink(spool)
                    except OSError:
                        pass
                raise
            logging.info("[noise_reduction] incremental session started (%d Hz, %d ch -> %d ch)",
                         rate, ch, worker_ch)
        except Exception:
            logging.exception("[noise_reduction] incremental session could not start; offline path remains")

    def _incremental_drain(self, session):
        """Drain thread: load a PRIVATE model instance off the UI thread, then
        run the wx-free controller (core.incremental_denoise.drain_capture_frames,
        which owns the lag/abandonment policy and the spool cleanup).

        The instance is private to this session because sharing one
        InferenceSession between the drain and the offline path would rely
        on concurrent run() being safe in our pinned onnxruntime — a
        property we have NOT verified (neither serialized calls nor native
        fault isolation are claimed). The single slot bounds this to at
        most one incremental instance at a time; the offline singleton
        (nnr._denoiser) stays untouched.
        """
        from core import neural_noise_reduction as nnr
        from core.incremental_denoise import IncrementalDenoiser, drain_capture_frames
        controller = getattr(self.main_window, "_incremental_controller", None)
        try:
            if not os.path.isfile(nnr.model_path()):
                raise FileNotFoundError(nnr.model_path())
            denoiser = nnr.NeuralDenoiser()
            worker = IncrementalDenoiser(session["rate"], session["worker_ch"], denoiser,
                                         spool_path=session["spool"])
            session["worker"] = worker
            transform = None
            if session["worker_ch"] != session["ch"]:
                # Same int16 (L+R)//2 as the offline prepare_voice_pcm.
                transform = lambda chunk: prepare_voice_pcm(chunk, session["ch"], "mono")[0]
            drain_capture_frames(session, worker, transform)
        except Exception:
            logging.exception("[noise_reduction] incremental drain failed; offline path remains")
            session["failed"] = True
            worker = session.get("worker")
            if worker is None:
                # Failed BEFORE the worker existed: the model could not be
                # loaded (missing file or load/ctor error). Per the agreed
                # contract this is "model unavailable": the send goes
                # STRAIGHT to the basic gate — the offline path must not
                # retry the ONNX load for this note.
                session["model_unavailable"] = True
            else:
                try:
                    worker.abort()
                except Exception:
                    pass
                session["worker"] = None
            # The spool is this session's own: remove it even when the
            # failure happened before the worker (and its handle) existed
            # (e.g. model file missing) — drain_capture_frames's own cleanup
            # makes this a no-op when it already ran.
            try:
                os.unlink(session["spool"])
            except OSError:
                pass
            # This failure path OWNED the cleanup (abort above, spool,
            # and the slot in the finally below): a send-time reaper must
            # never repeat it.
            session["drain_cleaned"] = True
        finally:
            # On a cancel/failure exit the cleanup above already ran (worker
            # dead, spool deleted): the slot is released here. On a normal
            # stop the send thread owns worker+spool and releases instead.
            # The send-authority entry is KEPT while PREPARING: a recording
            # whose drain failed mid-capture still sends (offline fallback)
            # and needs its transfer to pass. A discarded/revoked session has
            # no send coming — forget its entry here.
            if session.get("drain_cleaned") and controller is not None:
                controller.release_slot(session["local_id"])
                if not controller.is_preparing(session["local_id"]):
                    controller.forget(session["local_id"])

    def _cancel_incremental_denoise(self):
        """Ask the drain thread to abort and delete its spool (non-blocking)."""
        session = getattr(self, "_incremental_session", None)
        if session is not None:
            session["cancel"] = True
            controller = getattr(self.main_window, "_incremental_controller", None)
            if controller is not None:
                controller.discard(session.get("local_id", ""))
            self._incremental_session = None
