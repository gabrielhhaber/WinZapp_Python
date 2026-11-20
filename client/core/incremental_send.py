"""Consuming a recording's incremental noise-reduction session at send time.

The send thread of a voice note (``VoiceRecordingMixin._send_voice_message``)
hands the session that cleaned the audio while it was recorded
(``VoiceIncrementalSessionMixin._start_incremental_denoise``) to
:func:`consume_incremental_session`: it waits for the confirmed device close,
stops or cancels the drain, runs the worker's ``finish()`` under one bounded,
revoke-aware wait and either returns the cleaned audio or leaves the note to
the offline path. It is wx-free and touches no panel: everything it needs is
passed in, so the send path's whole incremental arbitration can be driven
from a test without a window.
"""

import logging
import os
import threading
import time
from dataclasses import dataclass

from core.voice_nr_options import voice_nr_engine, voice_nr_moment
from core.voice_stereo import drop_empty_side


#: The send path's SINGLE bounded wait (seconds) for the incremental
#: worker's final completion (drain exit + residual finish()). It is a
#: POLICY — prefer the existing basic gate over a longer wait — never
#: a hang diagnosis: is_alive() cannot tell a slow native call from a
#: stuck one. 2.0 s is an EXPERIMENTAL first value, not a validated
#: margin: the only residual measurements so far are feed+finish
#: microbenchmarks on the Linux development box, and earlier Windows
#: runs measured the TOTAL send path under different conditions, so no
#: headroom factor is claimed. The target-machine run may adjust it.
#: The device
#: close wait is deliberately NOT bounded: neither cleaned nor basic
#: audio may come from a frames list a callback can still append to.
#: Module-level so headless adapter tests can patch it without a real
#: panel class.
_INCREMENTAL_SEND_FINAL_WAIT = 2.0


def _reclaim_drain_remnant(session, controller, local_id):
    """Abort the worker a finished drain left behind, delete its spool and
    free the slot. Only for a drain that is no longer running (joined or
    seen dead): never aborts while native code may still run."""
    late_worker = session.get("worker")
    if late_worker is None:
        return
    try:
        late_worker.abort()
    except Exception:
        pass
    session["worker"] = None
    try:
        os.unlink(session["spool"])
    except OSError:
        pass
    if controller is not None:
        controller.release_slot(local_id)


def _discard_finished_result(worker, session, finish_out, controller, local_id):
    """Clean up a finish() result nobody will consume: abort the worker,
    delete the spool and the result file, free the slot. The caller holds
    the exclusive claim (``finish_out["claimed"]``), so this runs once."""
    try:
        worker.abort()
    except Exception:
        pass
    paths = {session.get("spool")}
    result = finish_out.get("result")
    if result:
        paths.add(result[0])
    for spool_path in paths:
        if not spool_path:
            continue
        try:
            os.unlink(spool_path)
        except OSError:
            pass
    if controller is not None:
        controller.release_slot(local_id)


@dataclass
class IncrementalSendOutcome:
    """What the send path continues with after the session was consumed.

    ``audio_data`` is the cleaned PCM (with ``channels``) when ``cleaned``;
    otherwise None, and the send joins the raw frames for the offline path.
    ``drain_thread`` is the session's drain snapshot, which the offline
    reduction's slot decision still reads.
    """
    audio_data: bytes = None
    channels: int = None
    cleaned: bool = False
    drain_thread: object = None


def consume_incremental_session(inc_session, *, settings, controller, local_id,
                                frames, actual_ch, channel_mode, mixed_audio, t0):
    """Run the send side of *inc_session* on the send thread.

    Returns an :class:`IncrementalSendOutcome`, or None when the send must
    stop here (revoked, cancelled, or the device close was never confirmed);
    the controller's send-authority entry is already forgotten then.
    """
    audio_data = None
    channels = None
    incremental_cleaned = False

    def _forget():
        # Terminal cleanup for a send that ends without transfer: drop the
        # controller's send-authority entry (and free the slot if it is
        # somehow still held by this id).
        if controller is not None:
            controller.forget(local_id)

    # A basic-only session (slot was busy at record time) has
    # no worker: only the confirmed-close stage applies.
    use_incremental = (
        not mixed_audio
        and not inc_session.get("basic_only", False)
        and settings.get("general", {}).get("noise_reduction_enabled", False)
        # The preference can change between recording and
        # sending; anything but neural-while-recording goes
        # through the published offline path below.
        and voice_nr_engine(settings) == "neural"
        and voice_nr_moment(settings) == "record"
    )
    drain_thread = inc_session.get("thread")

    def _wait_or_revoked(is_done, timeout=None):
        """Slice-wait until is_done(), honoring a user cancel of
        the pending row (controller revoke) even while a native
        call (device close, model load, feed, finish) sits
        blocked underneath. Native blocks cannot be preempted:
        a "timeout" result means the send FALLS BACK to the
        basic gate on the complete raw audio and the late
        worker result is discarded — it can never kill the
        thread. Device close (stage 1) has NO timeout: neither
        cleaned nor basic audio may be built from a frames
        list a callback can still append to. Returns "done" /
        "revoked" / "timeout"."""
        waited = 0.0
        while not is_done():
            if inc_session.get("revoked"):
                return "revoked"
            time.sleep(0.1)
            waited += 0.1
            if timeout is not None and waited >= timeout:
                return "timeout"
        return "done"

    # Stage 1 — confirmed device close BEFORE anything else: the
    # frames list is only stable once no capture callback can
    # append. There is no timeout that turns an unconfirmed close
    # into a send: a snapshot of a list that may still be growing
    # is not the recording, so this becomes the occupied path.
    stopped_ev = inc_session.get("stopped_event")
    if stopped_ev is not None:
        if _wait_or_revoked(stopped_ev.is_set) == "revoked":
            logging.info("[noise_reduction] send revoked while waiting "
                         "for the device close")
            _forget()
            return None
        if inc_session.get("device_failed"):
            # Close NOT confirmed: capture state unknown, frames
            # may still grow. Occupied until the user cancels or
            # the process ends; the drain is cancelled so it
            # cleans its own spool and releases the slot.
            logging.error("[noise_reduction] capture device close was not "
                          "confirmed; note stays pending until cancelled")
            inc_session["cancel"] = True
            while not inc_session.get("revoked"):
                time.sleep(0.1)
            _forget()
            return None
    # Stage 2 — ask the drain for its end state: stop → it
    # finishes the audio it has and hands worker+spool to this
    # send; cancel → it aborts and cleans up itself. The cancel
    # case needs NO wait: the raw frames are already stable
    # after the confirmed device close above, and the drain
    # self-cleans on the way out.
    inc_session["stop" if use_incremental else "cancel"] = True
    if (not use_incremental and not inc_session.get("basic_only", False)
            and (drain_thread is not None
                 or inc_session.get("worker") is not None)):
        # record→send preference switch with a native session
        # that may STILL hold its private model: the cancelled
        # drain cleans itself asynchronously, so the offline
        # pass below must never start while that model can be
        # loaded (review 18:47, contract 1 — the exclusion is
        # effective in both directions, not a boolean probe).
        # Wait — bounded and revoke-aware — for the drain's
        # exit, then reclaim an uncleaned remnant exactly once.
        # A drain that will not die keeps its model: mark the
        # session occupied so the note takes the published
        # gate / stays pending, never a second ONNX.
        if drain_thread is not None and drain_thread.is_alive():
            outcome = _wait_or_revoked(
                lambda: not drain_thread.is_alive(),
                timeout=_INCREMENTAL_SEND_FINAL_WAIT)
            if outcome == "revoked":
                logging.info("[noise_reduction] send revoked while "
                             "waiting for the cancelled drain")
                _forget()
                return None
            if outcome == "timeout":
                logging.info("[noise_reduction] cancelled drain "
                             "still busy; offline pass blocked "
                             "to avoid a second ONNX")
                inc_session["model_unavailable"] = True

                def _reap_switched_drain(session=inc_session,
                                         thread=drain_thread):
                    thread.join()
                    if session.get("drain_cleaned"):
                        return
                    _reclaim_drain_remnant(session, controller, local_id)

                threading.Thread(target=_reap_switched_drain,
                                 daemon=True).start()
        if (not inc_session.get("model_unavailable")
                and (drain_thread is None
                     or not drain_thread.is_alive())
                and not inc_session.get("drain_cleaned")):
            # The drain is dead (or was never snapshotted) but
            # left worker/spool/slot behind (normal stop-path
            # exit that raced past the cancel): reclaim the
            # remnant here, exactly once, before any offline
            # pass loads the singleton.
            _reclaim_drain_remnant(inc_session, controller, local_id)
    worker = inc_session.get("worker")
    if (use_incremental and worker is None
            and not inc_session.get("failed")
            and not inc_session.get("basic_only", False)
            and drain_thread is not None):
        # A drain EXISTS but there is no usable worker snapshot.
        # Policy: prefer the existing basic gate — never spin up
        # a SECOND ONNX instance through the offline path for
        # this note — whichever sub-state the drain is in.
        # (No drain thread at all means no incremental work was
        # ever started for this note: the published offline
        # path below is unchanged.)
        logging.info("[noise_reduction] no incremental worker ready at send; "
                     "falling back to the basic gate with the full raw audio")
        inc_session["fallback_chosen"] = True
        if drain_thread is not None and drain_thread.is_alive():
            # Model load still in progress: cancel so the late
            # constructor's drain aborts and self-cleans
            # (spool + slot) when it returns. But the drain's
            # last flag check may already have read False and
            # it can exit through the normal stop path WITHOUT
            # re-reading cancel — keeping worker/spool/slot for
            # a send that went basic. A reaper joins the drain
            # OFF the send path and claims whatever the drain
            # did not clean (flag set only where cleanup
            # actually ran), exactly once: never aborting while
            # native code runs (the join completes first),
            # never making the send wait, never a second ONNX.
            inc_session["cancel"] = True

            def _reap_late_drain(session=inc_session,
                                 thread=drain_thread):
                thread.join()
                if session.get("drain_cleaned"):
                    return
                _reclaim_drain_remnant(session, controller, local_id)

            threading.Thread(target=_reap_late_drain,
                             daemon=True).start()
        elif not inc_session.get("drain_cleaned"):
            # The drain EXITED between the worker snapshot and
            # the aliveness check. Cleanup authority keys on
            # what the drain ACTUALLY did — drain_cleaned is
            # written only where its cleanup really ran — never
            # on the cancel flag: a cancel/revoke can land
            # between the snapshot and this check while the
            # drain already left through its normal stop path
            # without re-reading it, keeping worker+spool+slot
            # for a send that holds no worker reference (a
            # drain that OBSERVED the cancel cleaned itself
            # and set the flag, so this branch then skips; the
            # alive branch above is mutually exclusive by
            # snapshot). The drain is dead, so the session
            # fields are stable — clean the remnant here,
            # exactly once.
            _reclaim_drain_remnant(inc_session, controller, local_id)
    if (use_incremental and worker is not None
            and not inc_session.get("failed")):
        # Stage 3 — finish() runs the remaining model hops and
        # can block natively just like load/feed: run it in a
        # helper that first joins the drain, under ONE
        # revocation-aware bounded wait for the whole final
        # completion (see _INCREMENTAL_SEND_FINAL_WAIT) — never
        # inline here, and never unbounded.
        #
        # The helper is created for EVERY session that reached
        # the drain stop holding its own published worker —
        # even one already cancelled (user revoke / shutdown /
        # discard between the stop and this gate): a cancelled
        # session is never consumed, but the helper is also the
        # FINALIZER that joins the drain and reclaims a remnant
        # the drain left uncleaned (its normal stop exit may
        # have raced past the cancel without re-reading it).
        # Skipping the helper on cancel leaked worker/spool/
        # slot: no reaper exists for a session holding its own
        # worker (Milo 17:50). A FAILED drain owns its cleanup
        # (drain_cleaned), so it is the one case excluded here.
        #
        # Ownership of a late result is indivisible: the
        # finisher's cleanup decision and this thread's
        # abandon/consume decision are serialized under
        # finish_lock, so exactly one party cleans up — a
        # finish() returning between the wait's timeout and the
        # abandonment mark can no longer slip through with
        # nobody aborting the worker, deleting the spool and
        # releasing the slot.
        finish_out = {}
        finish_lock = threading.Lock()

        def _finish_incremental():
            superseded = False
            try:
                if drain_thread is not None:
                    drain_thread.join()
                # Re-validate AFTER the join, not the pre-join
                # state: never call finish() on a worker the
                # drain may have aborted. A FAILED drain owns
                # the cleanup; a CANCELLED one may NOT have
                # cleaned (normal stop-path exit without
                # re-reading the flag) — the finally below
                # reclaims that remnant via drain_cleaned.
                if (inc_session.get("failed")
                        or inc_session.get("cancel")):
                    superseded = True
                elif not (inc_session.get("revoked")
                          or inc_session.get("fallback_chosen")):
                    finish_out["result"] = worker.finish()
            except BaseException as exc:
                finish_out["error"] = exc
            finally:
                finish_out["superseded"] = superseded
                with finish_lock:
                    finish_out["ready"] = True
                    # Exactly one claimant ever cleans up, and
                    # the claim is taken under the same lock
                    # that publishes readiness, so the revoked
                    # branch of the send thread can never clean
                    # it a second time. Authority keys on what
                    # the drain ACTUALLY did — drain_cleaned
                    # is written only where its cleanup really
                    # ran — never on session flags alone: a
                    # drain can exit through its normal stop
                    # path without re-reading a cancel/revoke
                    # that landed under the join, keeping
                    # worker+spool+slot for a send that no
                    # longer wants them. Whenever this helper
                    # will NOT process the worker (superseded,
                    # revoked or fallback) and the drain left
                    # the remnant uncleaned, claim it here.
                    # (A drain that failed or observed the
                    # cancel cleaned itself and set the flag:
                    # this then skips.)
                    abandoned = (not finish_out.get("claimed")
                                 and not inc_session.get("drain_cleaned")
                                 and (superseded
                                      or inc_session.get("revoked")
                                      or inc_session.get("fallback_chosen")))
                    if abandoned:
                        finish_out["claimed"] = True
                if abandoned:
                    # The helper owns the cleanup when the send
                    # thread walked away (revoked, or the
                    # bounded wait chose the basic fallback) or
                    # when the worker was superseded under the
                    # join but the drain left it uncleaned: a
                    # late result is deleted, the worker
                    # aborted and the slot released WHEN the
                    # native call actually returns — never left
                    # dangling.
                    _discard_finished_result(worker, inc_session, finish_out,
                                             controller, local_id)

        def _claim_completed_result():
            """Arbitrate a cancel/revoke against a finisher that
            may have ALREADY published a completed result: its
            post-join flag check ran before the cancel landed,
            so it exited expecting this thread to consume the
            result — and nobody else will clean it. The claim is
            taken under the same lock that publishes readiness,
            so exactly one party ever cleans up: a finished,
            unclaimed result is claimed and cleaned HERE; a
            finisher still in flight observes the cancel after
            its join (superseded) and reclaims an uncleaned
            remnant itself via drain_cleaned/claimed."""
            with finish_lock:
                finished = (finish_out.get("ready", False)
                            and not finish_out.get("superseded")
                            and not finish_out.get("claimed"))
                if finished:
                    finish_out["claimed"] = True
            if not finished:
                return
            _discard_finished_result(worker, inc_session, finish_out,
                                     controller, local_id)

        finisher = threading.Thread(target=_finish_incremental, daemon=True)
        finisher.start()
        if inc_session["cancel"]:
            # A cancel landed before this gate (user revoke,
            # shutdown, or a panel-level discard): the note is
            # never consumed or enqueued — transfer() would
            # refuse it anyway — so there is nothing to wait
            # for. But the send can NOT just walk away: the
            # finisher may already hold a completed result.
            # Same arbitration as the stage-3 revoked branch
            # below; a finisher still in flight owns the
            # remnant after its join (never aborting
            # native-active code — the join completes first).
            logging.info("[noise_reduction] send cancelled before finish(); "
                         "arbitrating the incremental remnant")
            _claim_completed_result()
            _forget()
            return None
        stage3 = _wait_or_revoked(lambda: finish_out.get("ready", False),
                                  timeout=_INCREMENTAL_SEND_FINAL_WAIT)
        if stage3 == "revoked":
            # finish() may have completed BEFORE the revoke
            # landed but after its own flag check: nobody
            # cleaned up — same arbitration as the pre-gate
            # cancel branch above, exactly once.
            logging.info("[noise_reduction] send revoked during finish()")
            _claim_completed_result()
            _forget()
            return None
        if stage3 == "timeout":
            with finish_lock:
                if finish_out.get("ready", False):
                    # Finished between the wait's last check
                    # and this claim: consume it normally.
                    stage3 = "done"
                else:
                    # Policy: prefer the basic gate on the
                    # complete raw over a longer wait; the
                    # finisher cleans up on return. cancel too:
                    # a drain that CAN observe flags between
                    # chunks aborts and self-cleans now instead
                    # of finishing work nobody will consume.
                    inc_session["fallback_chosen"] = True
                    inc_session["cancel"] = True
            if stage3 == "timeout":
                logging.warning("[noise_reduction] incremental finish still busy; "
                                "falling back to the basic gate with the full raw audio")
        if not inc_session.get('fallback_chosen'):
            try:
                _t_inc = time.perf_counter()
                with finish_lock:
                    already_claimed = finish_out.get("claimed")
                if already_claimed:
                    # The wait observed ready, but the revoke
                    # landed before the finisher's flag check:
                    # the finisher already discarded the late
                    # result under the same lock. Nothing left
                    # to consume; the message is cancelled.
                    _forget()
                    return None
                if finish_out.get("superseded"):
                    # The drain failed or was cancelled under
                    # the join and owns worker/spool/slot:
                    # basic gate on the raw audio; nobody else
                    # touches them.
                    audio_data = None
                else:
                    if "error" in finish_out:
                        raise finish_out["error"]
                    spool_path, engine = finish_out["result"]
                    try:
                        with open(spool_path, "rb") as spool_file:
                            cleaned_pcm = spool_file.read()
                    finally:
                        os.unlink(spool_path)
                    if inc_session["worker_ch"] == 1 and actual_ch == 2:
                        # Explicit mono: averaged before the model per chunk.
                        audio_data, channels = cleaned_pcm, 1
                    elif actual_ch == 2 and channel_mode == "auto":
                        # Same empty-side decision as the offline path, on
                        # the same raw audio; the kept side comes from the
                        # cleaned spool (channels are model-independent).
                        import numpy as np
                        raw_joined = b"".join(frames)
                        dropped_pcm, dropped_ch = drop_empty_side(raw_joined, 2)
                        if dropped_ch == 2:
                            audio_data, channels = cleaned_pcm, 2
                        else:
                            _raw = np.frombuffer(raw_joined, dtype="<i2")
                            if _raw[0::2].tobytes() == dropped_pcm:
                                _side = 0
                            elif _raw[1::2].tobytes() == dropped_pcm:
                                _side = 1
                            else:
                                # Never guess the side: fall back to the
                                # offline path rather than send the wrong one.
                                raise RuntimeError("empty-side selection did not match either channel")
                            audio_data = np.ascontiguousarray(
                                np.frombuffer(cleaned_pcm, dtype="<i2").reshape(-1, 2)[:, _side]
                            ).tobytes()
                            channels = 1
                    else:
                        audio_data, channels = cleaned_pcm, actual_ch
                    incremental_cleaned = True
                    if controller is not None:
                        # Worker finished, spool consumed and deleted:
                        # the slot is free. The send-authority entry
                        # stays until transfer (or a terminal failure).
                        controller.release_slot(local_id)
                    logging.info("[VOICE_TIMING] T+%.3fs — incremental noise reduction done "
                                 "(engine: %s; finish() residual %.3fs)",
                                 time.perf_counter() - t0, engine, time.perf_counter() - _t_inc)
            except Exception:
                logging.exception("[VOICE_TIMING] incremental noise reduction failed; "
                                  "falling back to the offline path")
                audio_data = None
                # finish() may have died with the spool still open or
                # half-written: close the handle, then delete the file.
                try:
                    worker.abort()
                except Exception:
                    pass
                try:
                    os.unlink(inc_session["spool"])
                except OSError:
                    pass
                if controller is not None:
                    controller.release_slot(local_id)
    return IncrementalSendOutcome(audio_data, channels, incremental_cleaned,
                                  drain_thread)
