"""
WinZapp Message Queue
---------------------
Background queue for outgoing messages (text and voice).

Behaviour
---------
* Immediate first attempt: the worker wakes up as soon as a message is
  enqueued, so the first send attempt is nearly instantaneous.
* Retry every 3 seconds on failure.
* In offline mode the worker loop is suspended until connectivity is
  restored; call ``flush()`` to wake it immediately when going back online.
* On success the UI is notified via ``wx.CallAfter`` so status labels update.
"""

import logging
import threading
import time
import wx


class MessageCancelled(Exception):
    """Internal control-flow signal for a user-cancelled queued send."""


class PendingMessage:
    """Data object for a queued outgoing message."""

    def __init__(self, local_id: str, jid: str,
                 text: str = None,
                 audio_path: str = None,
                 ogg_bytes: bytes = None,
                 media_path: str = None,
                 media_type: str = None,
                 caption: str = None,
                 progress_callback=None,
                 contact_info: dict = None,
                 quoted: dict = None,
                 mentioned_jids: list = None,
                 link_preview: dict = None):
        # local_id matches the "_local_id" field in the virtual message dict
        # that was already added to the UI.
        self.local_id      = local_id
        self.jid           = jid
        self.text          = text           # plain-text body
        self.audio_path    = audio_path     # path to recorded WAV
        self.ogg_bytes     = ogg_bytes      # pre-encoded OGG Opus (skips encoding on send)
        self.media_path    = media_path     # path to attached file (image/video/doc/audio)
        self.media_type    = media_type     # "image"|"video"|"audio"|"document"
        self.caption       = caption or ""  # optional caption for media
        self.progress_callback = progress_callback
        self.contact_info  = contact_info   # dict for contact attachment
        self.quoted        = quoted         # quoted/replied-to message dict
        self.mentioned_jids = mentioned_jids or []  # JIDs @mentioned in text
        self.link_preview  = link_preview   # resolved {"title","description","canonicalUrl"} or None
        self.fail_count    = 0             # consecutive send failures
        self.last_error    = ""            # last send error shown if retries exhaust
        self.cancel_event  = threading.Event()


class MessageQueue:
    """Thread-safe outgoing-message queue with automatic retry."""

    _RETRY_INTERVAL = 3   # seconds between retry cycles
    # Give up after this many consecutive failures per message.  Kept small on
    # purpose: every retry of a send that WhatsApp Web may have silently
    # accepted is a potential duplicate delivered to the recipient, so only
    # genuinely-not-sent failures (explicit 5xx) are retried, and only a few
    # times.  Connection losses and timeouts never reach this counter — they
    # are handled as "queued" / "unknown outcome" above.
    _MAX_RETRIES    = 4

    def __init__(self, main_window):
        self.main_window = main_window
        self._pending: dict = {}          # local_id → PendingMessage
        # local_ids a worker has picked up and not yet reported an outcome for.
        # Guarded by _lock together with _pending, which is what lets cancel()
        # answer "is this stopped for good?" without racing the worker.
        self._in_flight: set = set()
        self._lock   = threading.Lock()
        self._stop   = threading.Event()
        self._quick_event = threading.Event()
        self._media_event = threading.Event()
        self._workers = (
            threading.Thread(
                target=self._run, args=(False,), daemon=True,
                name="winzapp-message-quick",
            ),
            threading.Thread(
                target=self._run, args=(True,), daemon=True,
                name="winzapp-message-media",
            ),
        )
        for worker in self._workers:
            worker.start()

    # ── Public API ────────────────────────────────────────────────────────────

    def enqueue(self, msg: PendingMessage):
        """Add *msg* to the queue and trigger an immediate send attempt."""
        with self._lock:
            self._pending[msg.local_id] = msg
        self._event_for(msg).set()

    def flush(self):
        """
        Wake the worker immediately (call when going back online so queued
        messages are retried without waiting the full 3-second interval).
        """
        self._quick_event.set()
        self._media_event.set()

    def cancel(self, local_id: str) -> bool:
        """Cancel a queued or in-flight message by its local UI identifier.

        Returns True only when the message was still waiting in the queue and no
        worker had picked it up yet — the one case where cancelling is a
        guarantee that nothing was, or will be, sent. That case is reported here
        (a drop, which the panel has nothing left to undo but which disposes of
        the temporary WAV, known nowhere else), so that every cancellation
        produces exactly one report no matter which answer this gives.

        False means a worker already owns this message: it is inside the send
        call, or has finished it and is about to report the outcome. The cancel
        flag is set either way, but nothing can recall a request already on the
        wire, so the caller must be ready for the message to arrive at the
        recipient anyway — the worker reports every such message through
        MainWindow._on_cancelled_message_delivered()/_on_cancelled_message_
        dropped(), and ConversationsPanel._cancel_pending_message() keeps what
        it needs to finish the cancellation once it knows which one happened.
        """
        with self._lock:
            msg = self._pending.pop(local_id, None)
            if msg is None:
                # Already off the queue: a worker took it and will report back.
                return False
            msg.cancel_event.set()
            stopped = local_id not in self._in_flight
        # Wake the owning worker so a queued cancellation is observed now.
        self._event_for(msg).set()
        if stopped:
            # No worker will ever touch this message again, so this is the only
            # place its outcome can be reported — and the temporary WAV a voice
            # recording was sent from is only known here.
            self._report_cancelled_drop(msg)
        return stopped

    # Bounded wait, in stop(), for a send genuinely on the wire to finish
    # before returning. _perform_shutdown() calls stop() BEFORE
    # _stop_wpp_server(), which taskkills the same Node process a worker's
    # HTTP request is talking to — cutting that off mid-flight turns a send
    # seconds from succeeding into an ambiguous/lost one for no reason.
    # Bounded (a stuck upload must never block shutdown forever) and kept
    # short since it is one of the terms ipc.py's _QUIT_RELEASE_POLL_SECONDS
    # is sized against — raising it requires raising that too.
    _STOP_DRAIN_SECONDS = 4.0
    _STOP_DRAIN_POLL_SECONDS = 0.1

    def stop(self):
        """Signal the worker to exit cleanly (call at app shutdown).

        Waits briefly (see _STOP_DRAIN_SECONDS) for any in-flight send to
        finish before returning."""
        self._stop.set()
        self.flush()
        deadline = time.monotonic() + self._STOP_DRAIN_SECONDS
        while time.monotonic() < deadline:
            with self._lock:
                if not self._in_flight:
                    return
            time.sleep(self._STOP_DRAIN_POLL_SECONDS)
        with self._lock:
            remaining = len(self._in_flight)
        if remaining:
            logging.warning(
                "[MessageQueue] stop() gave up waiting for %d in-flight "
                "send(s) after %.1fs — proceeding with shutdown anyway",
                remaining, self._STOP_DRAIN_SECONDS,
            )

    # ── Worker thread ─────────────────────────────────────────────────────────

    @staticmethod
    def _is_media(msg: PendingMessage) -> bool:
        return bool(msg.media_path)

    def _event_for(self, msg: PendingMessage) -> threading.Event:
        return self._media_event if self._is_media(msg) else self._quick_event

    def _remember_own_sent_id(self, real_id):
        """Register a real WhatsApp message ID as "sent by this instance".

        The WebSocket echo (messages.upsert with fromMe=True) carries no
        correlation ID, so this set is the only thing telling the echo of our
        own send apart from a message the user sent on another device.  It has
        to be filled here, on the worker thread, before the UI is notified —
        the echo routinely arrives first.
        """
        if not isinstance(real_id, str):
            return
        with self.main_window._own_sent_ids_lock:
            self.main_window._own_sent_ids.add(real_id)
            # Prevent unbounded growth — keep at most 500 IDs.
            if len(self.main_window._own_sent_ids) > 500:
                self.main_window._own_sent_ids.discard(
                    next(iter(self.main_window._own_sent_ids))
                )

    def _report_cancelled_outcome(self, msg: PendingMessage, real_id,
                                  ambiguous: bool, quote_lost: bool):
        """Hand a cancelled message's outcome to the UI, exactly once.

        `real_id` being falsy is NOT the same as "it never went out": it also
        covers the ambiguous outcome — a timeout or dropped connection, where
        WhatsApp Web may well have taken the message into its own outbox and
        will flush it on reconnect (that is why the ambiguous branch never
        retries). Treating that as "dropped" would release the record the panel
        is holding, and a message that then does go out arrives as an echo with
        no anchor left, taking the identity of the next pending send of its
        type. So the unknown outcome is reported the same way a delivery with no
        ID is: the row goes back, still pending, for the echo to claim.
        """
        if not real_id and not ambiguous:
            logging.info(
                "[MessageQueue] cancelled %s never reached WhatsApp", msg.local_id
            )
            self._report_cancelled_drop(msg)
            return
        # Register the real ID: the echo carries no correlation ID, so this is
        # what stops it from being handled as a message from another device.
        self._remember_own_sent_id(real_id)
        logging.warning(
            "[MessageQueue] cancelled %s was not stopped in time (id=%s, "
            "ambiguous=%s) — handing it over to be revoked if it is known to "
            "have gone out, or marked unconfirmed if that is not knowable",
            msg.local_id, real_id, ambiguous,
        )
        wx.CallAfter(
            self.main_window._on_cancelled_message_delivered,
            msg.local_id,
            real_id if isinstance(real_id, str) else None,
            msg.jid,
            msg.audio_path,
            quote_lost,
            # Passed on rather than folded into "no ID": the UI restores an
            # unknown outcome differently from a confirmed one.
            bool(ambiguous),
        )

    def _report_cancelled_drop(self, msg: PendingMessage):
        """Tell the UI a cancelled message is definitively not going out.

        Releases the record ConversationsPanel holds as the anchor for an echo
        that will never arrive (when a worker had already claimed the message),
        and disposes of the temporary WAV a voice recording was sent from, whose
        path is known here and nowhere else.
        """
        wx.CallAfter(
            self.main_window._on_cancelled_message_dropped,
            msg.local_id,
            msg.audio_path,
        )

    def _run(self, media_only: bool):
        wake_event = self._media_event if media_only else self._quick_event
        while not self._stop.is_set():
            # Wait up to RETRY_INTERVAL seconds, or until woken early.
            wake_event.wait(timeout=self._RETRY_INTERVAL)
            wake_event.clear()

            if self._stop.is_set():
                break

            # While offline or WhatsApp disconnected: skip this cycle.
            if self.main_window.offline_mode:
                continue
            if not getattr(self.main_window, "_wa_connected", True):
                continue

            with self._lock:
                items = [
                    msg for msg in self._pending.values()
                    if self._is_media(msg) == media_only
                ]

            for msg in items:
                if self._stop.is_set():
                    break
                if self.main_window.offline_mode:
                    break
                if not getattr(self.main_window, "_wa_connected", True):
                    break
                # Claiming the message and re-checking the cancel flag under the
                # same lock cancel() takes is what makes cancel()'s answer
                # trustworthy: without it, a cancel landing between the check and
                # the send would be told "stopped for good" while this thread
                # went on to send the message anyway.
                with self._lock:
                    if msg.cancel_event.is_set():
                        continue
                    self._in_flight.add(msg.local_id)
                # Re-initialised per message: the finally below reads them to
                # decide what an unreported cancellation actually was, and a
                # value left over from the previous message would answer for the
                # wrong send.
                real_id    = None
                ambiguous  = False
                quote_lost = False
                reported   = False
                try:
                    if msg.audio_path:
                        real_id = self.main_window.send_audio_message(
                            msg.jid, msg.audio_path, quoted=msg.quoted,
                            ogg_bytes=msg.ogg_bytes,
                        )
                    elif msg.media_path:
                        def _media_progress(progress, pending=msg):
                            if pending.cancel_event.is_set():
                                raise MessageCancelled(pending.local_id)
                            if pending.progress_callback is not None:
                                pending.progress_callback(progress)

                        real_id = self.main_window.send_media_attachment(
                            msg.jid, msg.media_path, msg.media_type, msg.caption,
                            quoted=msg.quoted, upload_id=msg.local_id,
                            progress_callback=_media_progress,
                        )
                    elif msg.contact_info:
                        real_id = self.main_window.send_contact_attachment(
                            msg.jid, msg.contact_info, quoted=msg.quoted
                        )
                    else:
                        real_id = self.main_window.send_text_message(
                            msg.jid, msg.text, quoted=msg.quoted,
                            mentioned_jids=msg.mentioned_jids or None,
                            link_preview=msg.link_preview,
                        )
                    retryable_failure = False
                    disconnected      = False
                    if isinstance(real_id, dict):
                        if real_id.get("ok"):
                            quote_lost = bool(real_id.get("quote_lost", False))
                            real_id = real_id.get("id") or True
                        else:
                            msg.last_error = real_id.get("error") or ""
                            retryable_failure = bool(real_id.get("retry", True))
                            disconnected      = bool(real_id.get("disconnected"))
                            ambiguous         = bool(real_id.get("ambiguous"))
                            real_id = False

                    if msg.cancel_event.is_set():
                        # The user cancelled while this send was in flight.  None
                        # of the send_* calls above can be interrupted once the
                        # request is on the wire, so by the time the flag becomes
                        # visible here the outcome is already decided.
                        #
                        # This is checked BEFORE the outcome branches below
                        # because every one of them reports on a row the cancel
                        # already removed: the ambiguous branch has NVDA speak
                        # "send not confirmed" for a message the user just
                        # deleted, and the failure branch pops a modal error
                        # dialog for an upload that was abandoned on purpose.
                        with self._lock:
                            self._pending.pop(msg.local_id, None)
                        self._report_cancelled_outcome(
                            msg, real_id, ambiguous, quote_lost
                        )
                        reported = True
                        continue

                    if not real_id and disconnected:
                        # WhatsApp is down and told us so explicitly (HTTP 404
                        # "Disconnected"): the message was definitely not sent,
                        # so keep it queued — but stop the 3-second retry loop
                        # right here. main_window._wa_connected was just set to
                        # False by the send call, so the next loop iteration
                        # parks the whole queue until the connection is back.
                        logging.info(
                            "[MessageQueue] %s stays queued — WhatsApp disconnected", msg.local_id
                        )
                        break

                    if not real_id and ambiguous:
                        # Timeout / dropped connection: we do NOT know whether
                        # WhatsApp Web accepted the message into its outbox.
                        # Resending would duplicate it (and did: users saw 30+
                        # copies delivered at once when connectivity returned),
                        # so hand it off and let the WebSocket echo resolve the
                        # pending bubble if it does go out.
                        logging.warning(
                            "[MessageQueue] send outcome unknown for %s jid=%s (%s) — "
                            "not retrying to avoid duplicate delivery",
                            msg.local_id, msg.jid, msg.last_error,
                        )
                        with self._lock:
                            self._pending.pop(msg.local_id, None)
                        wx.CallAfter(self.main_window._on_message_unconfirmed, msg.local_id)
                        # From that pop on, cancel() answers False and the panel
                        # holds this message's record. _on_message_unconfirmed()
                        # routes a cancelled message into the cancelled path
                        # itself, so this counts as its one report.
                        reported = True
                        continue

                    if real_id:
                        msg.fail_count = 0
                        with self._lock:
                            self._pending.pop(msg.local_id, None)
                        # Register the real ID immediately so the WebSocket echo
                        # (messages.upsert with fromMe=True) is recognised as
                        # "sent by this instance" and not shown as a new message.
                        self._remember_own_sent_id(real_id)
                        wx.CallAfter(
                            self.main_window._on_message_sent,
                            msg.local_id,
                            msg.audio_path,
                            real_id if isinstance(real_id, str) else None,
                            msg.jid,
                            quote_lost,
                        )
                        # _on_message_sent() routes a message cancelled after this
                        # point into the cancelled path itself, so this counts as
                        # the one report the finally below must not duplicate.
                        reported = True
                    else:
                        msg.fail_count += 1
                        if not msg.last_error:
                            msg.last_error = getattr(self.main_window, "_last_send_error", "") or ""
                        logging.warning("[MessageQueue] send failed for %s jid=%s attempt=%s/%s",
                                        msg.local_id, msg.jid, msg.fail_count, self._MAX_RETRIES)
                        if (not retryable_failure) or msg.fail_count >= self._MAX_RETRIES:
                            logging.error("[MessageQueue] giving up on %s jid=%s after %s attempt(s). last_error=%s",
                                          msg.local_id, msg.jid, msg.fail_count, msg.last_error)
                            with self._lock:
                                self._pending.pop(msg.local_id, None)
                            wx.CallAfter(
                                self.main_window._on_message_failed,
                                msg.local_id,
                                msg.last_error,
                                bool(msg.media_path),  # show dialog for media failures
                            )
                            # Same as the ambiguous branch: _on_message_failed()
                            # is cancel-aware, so this is this message's report.
                            reported = True
                except MessageCancelled:
                    with self._lock:
                        self._pending.pop(msg.local_id, None)
                    logging.info("[MessageQueue] cancelled by user: %s", msg.local_id)
                    self._report_cancelled_drop(msg)
                    reported = True
                    continue
                except Exception as exc:
                    # requests/urllib3 may wrap MessageCancelled raised by the
                    # streaming body's progress callback in a transport error.
                    # The cancellation flag is authoritative: never retry it or
                    # surface a false send-failed dialog.
                    if msg.cancel_event.is_set():
                        with self._lock:
                            self._pending.pop(msg.local_id, None)
                        logging.info("[MessageQueue] cancelled during transport: %s", msg.local_id)
                        self._report_cancelled_drop(msg)
                        reported = True
                        continue
                    # Only unexpected programming errors reach here — transport
                    # failures are classified inside the send_* methods.
                    msg.fail_count += 1
                    logging.error("[MessageQueue] exception for %s jid=%s attempt=%s/%s: %s",
                                  msg.local_id, msg.jid, msg.fail_count, self._MAX_RETRIES, exc)
                    if msg.fail_count >= self._MAX_RETRIES:
                        logging.error("[MessageQueue] giving up on %s jid=%s after %s attempt(s)",
                                      msg.local_id, msg.jid, msg.fail_count)
                        with self._lock:
                            self._pending.pop(msg.local_id, None)
                        wx.CallAfter(
                            self.main_window._on_message_failed,
                            msg.local_id,
                            str(exc),
                            bool(msg.media_path),
                        )
                        reported = True
                finally:
                    # Released only once this message's outcome has been decided
                    # (delivered, dropped, or put back for a retry), so a cancel
                    # arriving anywhere in between is answered with "a worker
                    # owns this, wait for its report" rather than "stopped".
                    with self._lock:
                        self._in_flight.discard(msg.local_id)
                        # Every branch above passes through here, which is what
                        # makes "a cancel answered False always gets exactly one
                        # report" a property of the code rather than of having
                        # remembered each branch. It has to be: the cancel can
                        # land in the gap between a branch's own _pending.pop()
                        # and its wx.CallAfter reaching the main thread — after
                        # the pop, cancel() answers False and the panel starts
                        # holding this message's record, and only a report
                        # releases it. Enumerating the branches missed the
                        # ambiguous and give-up ones twice.
                        orphaned = (
                            msg.cancel_event.is_set()
                            and msg.local_id not in self._pending
                            and not reported
                        )
                    if orphaned:
                        # Outside the try/except above, so it needs its own:
                        # an exception raised here is on the worker thread with
                        # nothing left to catch it, and the thread dying takes
                        # the whole queue with it — nothing would ever be sent
                        # again, silently.
                        try:
                            self._report_cancelled_outcome(
                                msg, real_id, ambiguous, quote_lost
                            )
                        except Exception:
                            logging.exception(
                                "[MessageQueue] could not report the cancellation "
                                "of %s", msg.local_id,
                            )
