"""BackfillMixin — part of MainWindow (see main_window/__init__.py).

Moved verbatim out of main.py. Methods run with ``self`` bound to the
MainWindow instance, so every attribute set in MainWindow.__init__ is
available here.
"""

import logging
import threading
import time
import wx
from concurrent.futures import (
    ThreadPoolExecutor,
    as_completed,
)
from core.api_client import (
    api_get,
    api_post,
)
from main_window.message_rules import describe_history_sync_health
from main_window.history import HistoryMixin


class BackfillMixin:
    """Background history backfill of empty or incomplete chats, history-sync
    status and deferred media sync.
    """

    # Backfill pacing.  Adaptive rather than a fixed schedule: WhatsApp Web
    # loads each chat's history into its store at its own pace, so how long the
    # whole account takes is not knowable up front.  Measured on a real 539-chat
    # account, one pass recovered 203 of 463 chats — a fixed five-pass schedule
    # would have abandoned the rest while they were still arriving.  So: retry
    # quickly while passes keep recovering chats, back off when one recovers
    # nothing, and stop at an overall budget so this can never poll forever.
    _BACKFILL_FIRST_DELAY = 30     # seconds before the first sweep, and after a
                                   # complete sweep that made progress
    _BACKFILL_CHUNK_DELAY = 5      # pause between chunks in the same full sweep
    _BACKFILL_MAX_DELAY   = 300    # ceiling once passes stop recovering anything
    _BACKFILL_BUDGET      = 45 * 60  # total wall-clock the backfill may run for
    # Renewal ceiling for a session whose history is still arriving — a fresh
    # pairing delivers and decodes for far longer than the ordinary budget.
    _BACKFILL_LANDING_BUDGET = 4 * 60 * 60
    # Deliberately below sync_remote_chats()'s 6 workers and capped per pass:
    # this is background history nobody is waiting on, so it must not add a
    # burst of automation traffic on top of the media phase.
    _BACKFILL_WORKERS     = 3
    _BACKFILL_CHUNK       = 60     # chats re-queried per pass
    # Phone-history requests per pass. **One**, not a batch, and the reason is
    # not load: every one of these lights up the user's phone with a sync
    # notification (see request_older_messages()). Ten per pass meant four of
    # them inside 900 ms on a real install — the phone stacks four
    # notifications, which reads as WinZapp spamming even though each request
    # was productive. One per pass is the same throughput spread over the pass
    # loop, and the user sees one notification resolve before the next starts.
    _OLDER_REQUESTS_PER_PASS = 1

    #: Floor between any two phone-history requests, whatever the pass loop is
    #: doing. The backoff below is not a substitute: it collapses back to
    #: _BACKFILL_FIRST_DELAY the moment a pass makes progress, and a chunk
    #: landing *is* progress — so a productive request guarantees the next pass
    #: 30 s later, which is precisely the burst being removed. Measured on a
    #: real install: passes settled at the 5-minute ceiling, then ran at 32 s
    #: intervals for three passes as soon as chunks started landing.
    #:
    #: Two minutes keeps the total unchanged (16 requests in 20 minutes on that
    #: install) while never bunching them. Nothing here is urgent — this is
    #: history the user is not looking at yet.
    _PHONE_REQUEST_MIN_GAP = 120

    @classmethod
    def _initial_backfill_delay(cls, short_chats_pending: bool) -> int:
        """Start short-page confirmation now; defer background-only work."""
        return 0 if short_chats_pending else cls._BACKFILL_FIRST_DELAY

    @staticmethod
    def _background_backfill_work_allowed(short_chats_pending: bool,
                                          continuing_short_sweep: bool) -> bool:
        """Names and deep history must not block short-page recovery."""
        return not short_chats_pending and not continuing_short_sweep

    @staticmethod
    def _phone_request_gap_elapsed(last_at, now_monotonic, min_gap) -> bool:
        """Whether enough time has passed since the last phone-history request.

        A floor that the pass loop cannot talk its way out of. Every request
        this gates is a notification on the user's phone, and the pass cadence
        is driven by whether the *queue* is advancing — which a successful
        request makes true, so the requests kept pulling their own next round
        forward. Monotonic on purpose: a clock change must not open the gate.
        """
        if last_at is None:
            return True
        return (now_monotonic - last_at) >= min_gap

    @classmethod
    def _backfill_short_queue_delays(cls, retry_delay: int, sweep_finished: bool,
                                     sweep_made_progress: bool) -> tuple[int, int]:
        """Return (next sleep, retained retry backoff) for the short-chat queue."""
        if not sweep_finished:
            return cls._BACKFILL_CHUNK_DELAY, retry_delay
        if sweep_made_progress:
            retry_delay = cls._BACKFILL_FIRST_DELAY
        else:
            retry_delay = min(retry_delay * 2, cls._BACKFILL_MAX_DELAY)
        return retry_delay, retry_delay

    @staticmethod
    def _server_claims_content(chat: dict) -> bool:
        """True when list-chats says this chat has history, whatever we fetched.

        `unreadCount` and `t` (last-activity timestamp) both come straight from
        WhatsApp Web's chat record and are populated even when its message store
        is not — which is exactly the state that needs a backfill.
        """
        try:
            if int(chat.get("unreadCount", 0) or 0) > 0:
                return True
        except (TypeError, ValueError):
            pass
        try:
            return int(chat.get("t", 0) or 0) > 0
        except (TypeError, ValueError):
            return False

    def history_page_target(self) -> int:
        """How many messages per chat a finished sync should leave behind.

        The same messages_page_size the conversation view pages by, so "synced"
        means "opening the conversation shows a full page without going back to
        the network".
        """
        try:
            return max(1, int(getattr(self, "settings", {})
                              .get("user_interface", {})
                              .get("messages_page_size", 200)))
        except (AttributeError, TypeError, ValueError):
            return 200

    def _history_session_is_gone(self) -> bool:
        """Whether an unreadable /history-sync-status means "stop", not "retry".

        fetch_history_sync_status() answers None for two very different
        things, and every caller has to tell them apart the same way:

          * the session really is gone — WPPConnect reported "disconnected",
            our own connection flag is down, or the user went offline. Nothing
            more is coming; give up.
          * a transient read failure — most commonly a 503
            ``session_not_ready`` / "WAPI is not defined" from the
            statusConnection middleware, which exists precisely to say "the
            page is not ready *yet*" rather than "the session is dead" (see
            api_patches/src/middleware/statusConnection.ts). WhatsApp Web
            re-injects WAPI routinely while a big history transfer runs, so
            this shows up mid-wait on exactly the accounts the wait matters
            for. Also covers an HTTP timeout to the local API.

        Shared so the two callers cannot drift: they did, and the one that
        treated a transient failure as fatal took the whole message phase down
        with it.
        """
        return bool(
            getattr(self, "_history_status_disconnected", False)
            or not getattr(self, "_wa_connected", False)
            or getattr(self, "offline_mode", False)
        )

    def refresh_history_still_landing(self, context: str = "") -> bool:
        """Note whether more history is still on its way into WhatsApp Web.

        Everything about how hard the backfill should try depends on this. A
        chat that answers with 15 messages while history is still arriving is a
        chat whose history is *on its way*; the same 15 once everything has
        landed is simply a short conversation. Kept as an attribute because
        _note_backfill_state() runs on sync worker threads that must not each
        make their own status call.

        Three signals, because each covers a different stage:

          * unprocessedChunks > 0 — chunks delivered and waiting to be decoded.
          * initialSyncComplete is not true — a freshly paired session, where
            the phone has not even delivered the bulk of the history yet. This
            is the one that matters for a new pairing: at the moment the first
            sync runs there is often nothing in the queue *yet*, and reading
            only the chunk count would conclude that every short conversation
            is genuinely short at exactly the moment that is least true.
          * recentCompleted is false — the RECENT history pass has not yet
            finished, even if its decoder queue happens to be empty now.
        """
        status = self.fetch_history_sync_status()
        if status is None:
            disconnected = self._history_session_is_gone()
            if disconnected:
                self._history_still_landing = False
                logging.warning(
                    "[history-sync] %s: session disconnected; stopping history wait.",
                    context or "check")
                return False
            landing = bool(getattr(self, "_history_still_landing", True))
            self._history_still_landing = landing
            logging.warning(
                "[history-sync] %s: status unavailable; preserving landing=%s.",
                context or "check", landing)
            return landing
        unprocessed = status.get("unprocessedChunks")
        queued = isinstance(unprocessed, int) and unprocessed > 0
        first_sync = status.get("initialSyncComplete") is not True
        recent_incomplete = status.get("recentCompleted") is False
        landing = queued or first_sync or recent_incomplete
        self._history_still_landing = landing
        logging.info(
            "[history-sync] %s: unprocessed=%s initial_complete=%s recent_complete=%s "
            "[%s] — short chats %s.",
            context or "check", unprocessed, status.get("initialSyncComplete"),
            status.get("recentCompleted"),
            describe_history_sync_health(status),
            "will be re-queried as history lands" if landing
            else "will get one confirming retry only",
        )
        return landing

    #: How often the wait below reports what it is seeing. It polls every two
    #: seconds for up to ten minutes and used to log NOTHING unless a read
    #: failed, so "the phone is still transferring" and "this is wedged" looked
    #: identical from log.log — the only observable was a wall of
    #: `GET /history-sync-status -> 200`. That ambiguity is what made a normal
    #: (if slow) first pairing get reported as a sync regression.
    _HISTORY_WAIT_PROGRESS_SECONDS = 15

    def wait_for_restarted_history_sync(self, timeout: int = 600,
                                        should_stop=None) -> bool:
        """Wait until a manually restarted RECENT pass is actually complete.

        A read that merely *failed* must not be mistaken for a pass that will
        not finish. A single 503 ``session_not_ready`` used to end the wait
        outright: in the captured session it fired 5m20s into a 10-minute
        budget and deferred a sync whose RECENT pass went on to complete 10
        minutes later. Only a session that is actually gone
        (_history_session_is_gone()) ends the wait early now; anything else
        keeps polling until the deadline, which is what bounds this at all.

        Sets ``self._history_wait_outcome`` to why it stopped, because the two
        False cases are not the same thing to the caller:

          * ``"completed"`` — the RECENT pass finished (returns True).
          * ``"timeout"``   — the budget ran out while the transfer was still
            making progress. The phone still has history to push; that is a
            reason to expect short chats, NOT a reason to skip the message
            phase (see _run_sync()).
          * ``"session_gone"`` — offline, or the session is really gone. There
            is nothing to query and the caller should stop.
          * ``"superseded"`` — ``should_stop()`` answered True: the round
            waiting here is no longer current (issue #198), and nothing it
            does next matters. Asked once per poll, before the request.
        """
        self._history_wait_outcome = ""
        deadline = time.monotonic() + timeout
        last_progress_log = 0.0
        while time.monotonic() < deadline:
            if should_stop is not None and should_stop():
                self._history_wait_outcome = "superseded"
                return False
            if self._should_abort_sync_for_offline():
                self._history_wait_outcome = "session_gone"
                return False
            status = self.fetch_history_sync_status(timeout=10)
            if not isinstance(status, dict):
                if self._history_session_is_gone():
                    logging.warning(
                        "[history-sync] Restarted RECENT wait: session is gone; "
                        "stopping the wait.")
                    self._history_wait_outcome = "session_gone"
                    return False
                logging.warning(
                    "[history-sync] Restarted RECENT wait: status unreadable but "
                    "the session is still up — treating it as transient, %.0fs of "
                    "budget left.", max(0.0, deadline - time.monotonic()))
                time.sleep(2)
                continue
            counts = status.get("storeCounts") or {}
            message_count = counts.get("message")
            queue_empty = status.get("unprocessedChunks") == 0
            if queue_empty and status.get("recentCompleted") is True:
                logging.info(
                    "[history-sync] Restarted RECENT history completed at %s messages.",
                    message_count,
                )
                self._history_wait_outcome = "completed"
                return True
            now = time.monotonic()
            if now - last_progress_log >= self._HISTORY_WAIT_PROGRESS_SECONDS:
                last_progress_log = now
                logging.info(
                    "[history-sync] Restarted RECENT wait: messages=%s chats=%s "
                    "unprocessed=%s recent_complete=%s initial_complete=%s "
                    "[%s] — %.0fs of budget left.",
                    message_count, counts.get("chat"),
                    status.get("unprocessedChunks"), status.get("recentCompleted"),
                    status.get("initialSyncComplete"),
                    describe_history_sync_health(status),
                    max(0.0, deadline - time.monotonic()),
                )
            time.sleep(2)
        logging.warning(
            "[history-sync] Restarted RECENT history did not complete within %ds — "
            "the phone is still pushing history.", timeout,
        )
        self._history_wait_outcome = "timeout"
        return False

    @staticmethod
    def _recent_history_needs_wait(unblock_result) -> bool:
        """Whether the phone's RECENT transfer must finish before REST sync."""
        return isinstance(unblock_result, dict) and (
            unblock_result.get("restarted") is True
            or unblock_result.get("recentCompleted") is False
        )

    def _note_backfill_state(self, remote_jid: str, chat: dict, api_ok: bool) -> None:
        """Track chats that still owe us history.

        Two cases, and they used to be one. The original: get-messages reads
        WhatsApp Web's store, which right after pairing is empty for most chats,
        so it answers 200 with an empty list — indistinguishable from a
        genuinely empty conversation. Measured on a real 539-chat account, 514
        came back empty during the sync, and the sync never ran again because
        _sync_completed gates it. That left the list permanently missing
        history, unread badges clamped to zero by effective_unread_count(), and
        chats with no other identity dropped entirely.

        The second case is the same problem one step later: a chat that answers
        with *some* messages but fewer than a page. While WhatsApp Web is still
        decoding history-sync chunks, that number keeps growing for minutes
        after the sync ends — the account measured here went from 1.2k to 60k
        messages over about ten minutes — so a chat that returned 15 messages
        was not short, it was early. Those get re-queried too, until they either
        reach a full page or stop growing.

        Chats already holding a full page are done, and so is a short chat seen
        for the first time with the chunk queue already drained: get-messages
        reads the same store the chunks feed, so with nothing left to decode an
        immediate re-query returns the identical list. (Zero records is the
        exception and stays unconditional — that one really is "WhatsApp Web has
        not materialised this chat yet".) A chat that grew during the backfill
        keeps its slot for one more pass, so the ramp finishes, and is dropped
        the first pass that adds nothing.

        Called from sync_chat_messages() on its worker threads. The whole
        read/decide/write transition is protected because individual atomic
        set operations do not make that compound transition atomic.
        """
        with self._backfill_state_guard():
            pending = getattr(self, "_chats_awaiting_messages", None)
            if pending is None:
                pending = self._chats_awaiting_messages = set()
            counts = getattr(self, "_partial_history_counts", None)
            if counts is None:
                counts = self._partial_history_counts = {}

            canonical = self._canonical_backfill_jid(remote_jid)
            forms = set(self._jid_address_forms(remote_jid))
            forms.update(self._jid_address_forms(canonical))

            def _done():
                # Discard every address form: the chat may have been marked
                # under its @lid and re-synced under its phone JID (or reverse).
                for form in forms:
                    pending.discard(form)
                    counts.pop(form, None)

            records = (chat.get("messages", {}).get("messages", {}).get("records")) or []
            if not api_ok:
                # The API never really answered. This used to retire the chat,
                # on the reasoning that a failed call was "the retry loop's
                # business, not the backfill's" — but by the time this runs,
                # sync_chat_messages() has already exhausted its own retries.
                # Nothing else was coming back for it: the chat left the queue,
                # sync_remote_chats() only counts futures that RAISE (this one
                # returns normally), and _run_sync() declares the sync complete
                # from the connection and the chat list alone. A chat whose
                # messages never arrived was indistinguishable from one that
                # had nothing to fetch.
                #
                # Keeping it queued is what makes the backfill pass come back
                # for it. It costs at most a re-query per sweep, bounded by the
                # loop's own budget, and the alternative is losing a whole
                # conversation's history silently.
                _done()
                # Only when the failure left the chat with nothing at all.
                #
                # Retiring it unconditionally — the old behaviour — meant a
                # conversation whose messages never arrived was dropped from
                # the queue with nothing coming back for it: sync_chat_messages()
                # had already exhausted its retries, sync_remote_chats() only
                # counts futures that RAISE (this path returns normally), and
                # _run_sync() declares success from the chat list alone.
                #
                # But requeueing every failure would undo an invariant that
                # earned its own test: a chat that already holds history must
                # stay out of the queue even when a later refresh fails, or a
                # transient error puts fully-synced chats back in the loop
                # forever. Failing to refresh loses nothing; failing to fetch
                # the first page loses the conversation.
                if not records:
                    pending.add(canonical)
                return
            if not records:
                if self._server_claims_content(chat):
                    _done()
                    pending.add(canonical)
                else:
                    _done()
                return

            count = len(records)
            # A full page used to retire the chat unconditionally, and that is
            # the one signal that most deserves a second look: the page is full
            # because the window saturated, which is exactly when there can be
            # more history behind it. Gap chats stay queued until growth stops.
            gaps = set(getattr(self, "_history_gap_jids", ()) or ())
            gap = any(form in gaps for form in forms)
            if gap:
                # A known disjoint block is not "complete" merely because the
                # local union already contains >= one visible page: keep it on
                # the repair queue while there is any reason to think another
                # pass can close the hole.
                #
                # "Any reason" has to be a finite condition, and the growth rule
                # the general path below already uses is the one available.
                # Queueing gap chats unconditionally — until overlap is proven
                # or the phone says there is no older history — sounds stricter
                # but has no third outcome: WhatsApp Web routinely never
                # decodes a middle stretch for a large group, and neither of
                # those two exits ever fires. That chat then sits in
                # _history_gap_jids (persisted, so it survives restarts) and in
                # the pending queue forever, and _plan_message_sync() reads
                # either set as repair_needed → a FULL re-sync of the single
                # most expensive conversation on the account, every round of
                # every session. The incremental sync this whole change exists
                # for would never apply to it again.
                #
                # So: a pass that adds nothing is the answer. Drop the repair
                # marker with the queue slot — a later live message or a real
                # growth signal re-detects the gap through history_gap_detected()
                # the same way it was found the first time.
                previous_values = [counts[f] for f in forms if f in counts]
                previous = max(previous_values) if previous_values else None
                grew = previous is not None and count > previous
                first_sighting = previous is None
                still_landing = getattr(self, "_history_still_landing", False)
                _done()
                if grew or first_sighting or still_landing:
                    counts[canonical] = count
                    pending.add(canonical)
                    return
                self._history_gap_jids.difference_update(forms)
                logging.info(
                    "[history-gap] %s: repair pass added nothing (%d records) — "
                    "releasing it from the repair queue.", canonical, count,
                )
                return
            if count >= self.history_page_target():
                _done()
                return

            previous_values = [counts[f] for f in forms if f in counts]
            previous = max(previous_values) if previous_values else None
            grew = previous is not None and count > previous
            still_landing = getattr(self, "_history_still_landing", False)
            _done()
            # A short first page is ambiguous: it may be a genuinely short
            # conversation, or merely the bounded linked-device window before
            # older phone history arrives. Queue one background confirmation;
            # this no longer blocks the initial-sync completion path.
            first_short_page = previous is None
            if still_landing or grew or first_short_page:
                counts[canonical] = count
                pending.add(canonical)

    def _backfill_state_guard(self):
        """Return the queue lock, lazily for lightweight test/legacy objects."""
        lock = getattr(self, "_backfill_state_lock", None)
        if lock is None:
            lock = self._backfill_state_lock = threading.RLock()
        return lock

    def _canonical_backfill_jid(self, jid: str) -> str:
        """Prefer the stable phone JID once the LID bridge is known."""
        if not jid:
            return jid
        if jid.endswith("@lid"):
            return getattr(self, "_lid_to_phone", {}).get(jid, jid)
        return jid

    def _collapse_and_list_backfill_pending(self) -> list:
        """Rewrite the queue under canonical JIDs and return it, sorted.

        This MUTATES `_chats_awaiting_messages` and `_partial_history_counts`:
        a chat queued under its @lid before the bridge was known collapses onto
        the phone JID that names it now, so the same conversation stops
        occupying two slots. It was called `_backfill_pending_snapshot()` — a
        read-only name for a call that rewrites the queue from inside the
        scheduling loop.
        """
        with self._backfill_state_guard():
            pending = getattr(self, "_chats_awaiting_messages", None)
            if pending is None:
                pending = self._chats_awaiting_messages = set()
            counts = getattr(self, "_partial_history_counts", None)
            if counts is None:
                counts = self._partial_history_counts = {}

            canonical_pending = {self._canonical_backfill_jid(jid) for jid in pending}
            canonical_counts = {}
            for jid, count in counts.items():
                key = self._canonical_backfill_jid(jid)
                if key in canonical_pending:
                    canonical_counts[key] = max(count, canonical_counts.get(key, count))
            pending.clear()
            pending.update(canonical_pending)
            counts.clear()
            counts.update(canonical_counts)
            return sorted(canonical_pending)

    def _persist_message_retry_jids(self) -> None:
        """Persist changed chats whose message query has not succeeded yet."""
        try:
            with self._sync_failures_lock:
                payload = sorted(getattr(self, "_message_retry_jids", set()) or set())
            if getattr(self, "db", None) is not None:
                self.db.set_metadata_json("message_retry_jids_v1", payload)
        except Exception as exc:
            logging.warning("[sync] Could not persist message retry state: %s", exc)

    def _persist_history_gap_jids(self) -> None:
        """Persist known disjoint-history gaps so restart cannot forget them."""
        try:
            with self._backfill_state_guard():
                payload = sorted(getattr(self, "_history_gap_jids", set()) or set())
            if getattr(self, "db", None) is not None:
                self.db.set_metadata_json("history_gap_jids_v1", payload)
        except Exception as exc:
            logging.warning("[history-gap] Could not persist gap state: %s", exc)

    def _persist_backfill_pending_state(self) -> None:
        """Persist only the chats that still need short-history repair."""
        try:
            with self._backfill_state_guard():
                pending = set(getattr(self, "_chats_awaiting_messages", set()) or set())
                counts = dict(getattr(self, "_partial_history_counts", {}) or {})
                payload = {}
                for jid in pending:
                    canonical = self._canonical_backfill_jid(jid)
                    if not canonical:
                        continue
                    count = counts.get(jid, counts.get(canonical, 0))
                    try:
                        count = max(0, int(count or 0))
                    except (TypeError, ValueError):
                        count = 0
                    payload[canonical] = max(count, payload.get(canonical, 0))
            if getattr(self, "db", None) is not None:
                self.db.set_metadata_json("backfill_pending_v1", payload)
        except Exception as exc:
            logging.warning("[backfill] Could not persist pending history state: %s", exc)

    def _remove_backfill_pending(self, jid: str) -> None:
        """Retire a pending conversation under every known address form."""
        with self._backfill_state_guard():
            pending = getattr(self, "_chats_awaiting_messages", set())
            counts = getattr(self, "_partial_history_counts", {})
            forms = set(self._jid_address_forms(jid))
            forms.update(self._jid_address_forms(self._canonical_backfill_jid(jid)))
            for form in forms:
                pending.discard(form)
                counts.pop(form, None)

    def _keep_backfill_pending(self, jid: str, count: int) -> None:
        """Keep a short chat eligible while phone history is in flight."""
        with self._backfill_state_guard():
            canonical = self._canonical_backfill_jid(jid)
            self._chats_awaiting_messages.add(canonical)
            self._partial_history_counts[canonical] = count

    def _note_conversation_opened(self, remote_jid: str) -> None:
        """Remember that the user opened this conversation, durably.

        Gates the *phone* request in _backfill_empty_chats(). Everything else
        the backfill does is local and free; asking the phone is the one step
        that puts a notification on the user's own device, and until this
        existed it was spent on every chat in the account.

        Measured on the reporting install: 82 chats short of the 200-message
        target, marching one phone request every two minutes for hours, for
        conversations the user had never opened — on an account synced for
        weeks. His words, twice: it should be following new messages, not
        fetching old history for other conversations in the background.

        Opening a conversation is the signal that its history is worth a
        notification, and it is exactly the moment the user would accept one.
        Scrolling up (fetch_older_messages) is unaffected and always was —
        that request is attended by definition.
        """
        jid = self._normalize_jid(remote_jid or "")
        if not jid:
            return
        opened = getattr(self, "_opened_conversations", None)
        if not isinstance(opened, set):
            opened = self._opened_conversations = set()
        forms = {jid}
        try:
            forms.update(f for f in self._jid_address_forms(jid) if f)
        except Exception:
            pass
        if forms <= opened:
            return
        opened.update(forms)
        try:
            if getattr(self, "db", None) is not None:
                self.db.set_metadata_json(
                    "opened_conversations_v1", sorted(opened))
        except Exception as exc:
            logging.warning("[history-sync] could not persist opened conversations: %s", exc)

    def _user_has_opened(self, jid: str) -> bool:
        """Whether the phone may be asked about this chat's older history."""
        opened = getattr(self, "_opened_conversations", None)
        if not isinstance(opened, set) or not opened:
            return False
        forms = {jid}
        try:
            forms.update(f for f in self._jid_address_forms(jid) if f)
            forms.add(self._canonical_backfill_jid(jid))
        except Exception:
            pass
        return bool(forms & opened)

    def _retire_chat_without_older_history(self, jid: str) -> None:
        """Record, durably, that the phone has no older history for this chat.

        The backfill asked, spent its budget, and nothing came back. Until
        this existed that verdict lived only in `_older_request_attempts`,
        which is in memory — so every launch handed the same chat a fresh
        budget and asked again. Each of those asks is a notification on the
        user's phone, and the phone answers a request it cannot satisfy with
        "Sync paused. Open WhatsApp to resume." — an *error*, on an account
        that has been fully synced for weeks, for a conversation the user
        never opened. Reported exactly that way.

        Measured on a real install: two groups holding 1 and 2 messages, each
        asked twice, `oldestMsgKey` byte-identical across both asks, twelve
        get-messages rounds in between, nothing ever delivered. Their
        `endOfHistoryTransferType` was 4 and null, where every ask that *did*
        deliver came back as 0 — worth knowing, but the verdict here is taken
        from the outcome rather than from an undocumented enum value, so it
        stays right if WhatsApp renumbers them.

        `_exhausted_chats` is the same set fetch_older_messages() writes and
        the deep walk reads, so this also stops the chat being re-queried from
        the other direction. The user scrolling up clears it — see
        _forget_history_exhaustion() and the F5 resync.
        """
        if not hasattr(self, "_exhausted_chats"):
            self._exhausted_chats = set()
        if jid in self._exhausted_chats:
            return
        self._exhausted_chats.add(jid)
        self._persist_exhausted_chats()
        self._remove_backfill_pending(jid)
        with self._backfill_state_guard():
            gap_forms = set(self._jid_address_forms(jid))
            gap_forms.update(
                self._jid_address_forms(self._canonical_backfill_jid(jid)))
            self._history_gap_jids.difference_update(gap_forms)
        logging.info(
            "[history-sync] The phone answered nothing for %s after %d request(s) "
            "— retiring it for good so it is never asked again.",
            jid, self._MAX_PHONE_HISTORY_REQUESTS)

    def _is_backfill_pending(self, jid: str) -> bool:
        """Whether a conversation is queued under either known address."""
        with self._backfill_state_guard():
            pending = getattr(self, "_chats_awaiting_messages", set())
            forms = set(self._jid_address_forms(jid))
            forms.update(self._jid_address_forms(self._canonical_backfill_jid(jid)))
            return any(form in pending for form in forms)

    def _completed_backfill_targets(self, window) -> int:
        """Count only this pass's targets, independent of concurrent arrivals."""
        return sum(1 for jid in window if not self._is_backfill_pending(jid))

    def _jid_address_forms(self, jid: str) -> tuple:
        """The JID plus its counterpart across the @lid ↔ phone bridge.

        deduplicate_chats() re-keys self.chats from @lid to phone JIDs *after*
        sync_remote_chats() has run, so a JID recorded during the message sync
        can be absent from self.chats minutes later under a different address.
        """
        if not jid:
            return ()
        alt = (getattr(self, "_lid_to_phone", {}).get(jid)
               or getattr(self, "_phone_to_lid", {}).get(jid))
        return (jid, alt) if alt and alt != jid else (jid,)

    def _chat_jids_equivalent(self, left: str, right: str) -> bool:
        """Whether two JIDs identify the same canonical conversation.

        A returning session stores one-to-one chats under their phone JID, but
        sync_chat_messages() deliberately queries the matching @lid because
        that is the address WhatsApp Web reliably indexes. The returned message
        keeps that @lid in key.remoteJid. Comparing the two raw strings made the
        phantom-chat defense discard every legitimate result before saving it,
        leaving hundreds of chats in the backfill forever.

        Groups do not have an alternate form, so a group message returned by a
        participant lookup still fails this comparison and remains filtered.
        """
        left = self._normalize_jid(left or "")
        right = self._normalize_jid(right or "")
        if not left or not right:
            return False
        if left == right:
            return True
        return bool(set(self._jid_address_forms(left)) &
                    set(self._jid_address_forms(right)))

    def _resolve_backfill_target(self, jid: str):
        """Live (key, chat) for a pending JID, or (None, None) if it is gone.

        Without this the backfill silently did nothing for individual chats:
        it recorded them under their @lid during the sync, deduplicate_chats()
        then re-keyed self.chats to phone JIDs, and every later `jid in
        self.chats` lookup missed. Only groups — which dedup never renames —
        were ever retried, which is why a real account sat at 399 visible
        conversations while pass after pass reported progress.
        """
        for form in self._jid_address_forms(jid):
            chat = self.chats.get(form)
            if chat is not None:
                return form, chat
        return None, None

    def _local_record_count(self, jid: str) -> int:
        """How many messages we currently hold for a chat, across both JID forms."""
        _key, chat = self._resolve_backfill_target(jid)
        if chat is None:
            return 0
        return len((chat.get("messages", {}).get("messages", {}).get("records")) or [])

    def _pending_name_resolution(self) -> list:
        """@lid chats still bridged to no phone number, i.e. still unnamed.

        Same rule _run_sync() uses, but callable again later. That single pass
        runs while WhatsApp Web is still warming up, so LIDs it could not map
        then stayed unmapped for the rest of the session and their chats kept
        showing a bare @lid or a raw phone number instead of a name.
        """
        resolved = getattr(self, "_lid_to_phone", {})
        unresolvable = getattr(self, "_unresolvable_lids", set())
        return [jid for jid in list(self.chats.keys())
                if jid.endswith("@lid") and jid not in resolved and jid not in unresolvable]

    def _backfill_names(self) -> int:
        """Retry name resolution for chats still lacking one. Returns how many
        got bridged this round."""
        pending = self._pending_name_resolution()
        if not pending:
            return 0
        before = len(getattr(self, "_lid_to_phone", {}))
        # Same chunk as the message backfill, and for the same reason: this all
        # funnels through the one Puppeteer page.
        logging.info("[backfill] Resolving names for %d unresolved @lid chat(s) "
                     "(%d still pending).", min(len(pending), self._BACKFILL_CHUNK), len(pending))
        self.resolve_lid_jids_via_api(pending[:self._BACKFILL_CHUNK])
        gained = len(getattr(self, "_lid_to_phone", {})) - before
        if gained > 0:
            self.chats = self.deduplicate_chats(self.chats)
            self._build_lid_to_phone_cache()
            logging.info("[backfill] Name resolution bridged %d new LID(s).", gained)
        return gained

    def _backfill_empty_chats(self):
        """Re-fetch messages for chats whose history WhatsApp Web had not loaded.

        Runs on its own daemon thread after the initial message sync. Each pass
        retries only the chats still missing history, so the work shrinks as the
        store warms up, and stops early once nothing is pending.

        On a freshly paired session this loop is also the only thing watching
        the history-sync queue. Nothing pushes decoded history to WinZapp:
        measured during a live drain, 93,000 messages entered WhatsApp Web's
        store and not one socket event came out of it — the messages exist only
        for whoever asks again. So each pass, while history is still landing,
        re-reads the queue and re-kicks it if needed.
        """
        my_run = getattr(self, "_sync_run_id", 0)
        deadline = time.monotonic() + self._BACKFILL_BUDGET
        # A fresh pairing can take longer to deliver and decode its history than
        # the ordinary budget allows, and stopping halfway leaves conversations
        # permanently short. So the deadline is renewed while history is
        # demonstrably still arriving — never past this ceiling, so a session
        # that never settles still ends.
        hard_deadline = time.monotonic() + self._BACKFILL_LANDING_BUDGET
        delay = self._initial_backfill_delay(
            bool(self._collapse_and_list_backfill_pending()))
        retry_delay = self._BACKFILL_FIRST_DELAY
        attempt = 0
        attempted: set[str] = set()
        sweep_made_progress = False
        try:
            while time.monotonic() < deadline:
                # Sleep in slices so a shutdown or a newer sync is noticed
                # quickly instead of after the whole delay.
                for _ in range(delay):
                    if not self._ui_ready_event.is_set():
                        return
                    if getattr(self, "_sync_run_id", 0) != my_run:
                        logging.info("[backfill] A newer sync took over — stopping.")
                        return
                    time.sleep(1)

                if self._voice_call_in_progress():
                    # Logged once per call, not once per second: this loop
                    # re-checks every second and the line was filling the log
                    # file users are asked to send.
                    if not getattr(self, "_backfill_call_pause_logged", False):
                        self._backfill_call_pause_logged = True
                        logging.info("[backfill] paused during active voice call")
                    delay = 1
                    continue
                self._backfill_call_pause_logged = False

                if not getattr(self, "_wa_connected", False):
                    # Not a wasted pass: nothing was attempted, so just wait
                    # again rather than spending part of the budget on it.
                    logging.info("[backfill] Offline — waiting before retrying.")
                    delay = retry_delay
                    continue

                attempt += 1
                # Re-read the queue once per pass (not once per chat): it is
                # what decides whether a chat that came back short is still owed
                # history or has simply told us everything it has.
                # Query/unblock the history-sync machinery once per complete
                # sweep, not once per chunk. Large queues now move through
                # several chunks quickly and do not need to hammer this status
                # endpoint between each one.
                landing_now = getattr(self, "_history_still_landing", False)
                if not attempted:
                    landing_now = self.refresh_history_still_landing(
                        context=f"backfill pass {attempt}")
                if not landing_now:
                    self._start_deferred_media_sync()
                if not attempted and landing_now:
                    # Two things can stall the queue, and both are silent: a
                    # chunk the processing loop will never accept parked at the
                    # head of it, and a loop that simply stopped scheduling
                    # itself (seen live at 20 of 22 chunks, with the last two
                    # sitting untouched). One call handles both, and no-ops when
                    # neither applies.
                    self.unblock_history_sync()
                    deadline = min(hard_deadline,
                                   max(deadline, time.monotonic() + self._BACKFILL_BUDGET))

                # Read the pending set *after* the queue check: a chat can join
                # it as history lands, and the sync that added it may have
                # finished while this pass was sleeping.
                pending = self._collapse_and_list_backfill_pending()
                pending_set = set(pending)
                attempted = {
                    canonical for jid in attempted
                    if (canonical := self._canonical_backfill_jid(jid)) in pending_set
                }
                continuing_short_sweep = bool(
                    attempted and any(jid not in attempted for jid in pending))
                names_pending = self._pending_name_resolution()
                # Chats that already hold a full page but whose older history
                # has never been walked. Without this the loop declared itself
                # finished the moment every chat had 200 messages, which for a
                # 20,000-message conversation is 1% of it.
                deep_pending = self._chats_needing_deep_history()
                if not pending and not names_pending and not deep_pending:
                    if getattr(self, "_history_still_landing", False):
                        # Nothing to re-query yet, but history is still arriving
                        # — keep the loop alive to notice when it does.
                        logging.info("[backfill] Nothing pending yet, but history is still "
                                     "landing — staying on watch.")
                        attempted.clear()
                        retry_delay = self._BACKFILL_FIRST_DELAY
                        delay = retry_delay
                        continue
                    logging.info("[backfill] Nothing pending — every chat is walked back to "
                                 "its beginning (or all there is) and has a name.")
                    return
                # Names get the same second chance as messages. _run_sync()
                # resolves LIDs exactly once, while WhatsApp Web is still warming
                # up, so anything it could not map then stayed a bare @lid or a
                # raw phone number for the whole session.
                # Name and deep-history work run once per complete short-chat
                # sweep. Repeating them between every fast queue chunk would
                # merely move the old 30-second bottleneck to another endpoint.
                background_work_allowed = self._background_backfill_work_allowed(
                    bool(pending), continuing_short_sweep)
                named = self._backfill_names() if background_work_allowed else 0
                if named:
                    wx.CallAfter(self._schedule_set_chats)

                # ── Deep history ─────────────────────────────────────────
                # Runs once per complete short-chat sweep, before the
                # short-page work's own early exit below, so a session whose
                # chats all hold a full page still makes progress. Each visit
                # pages a few chats further back; a chat that needs more comes
                # back on the next sweep, anchored on what is now oldest on
                # disk, so the walk resumes rather than restarting.
                deep_stored = 0
                if deep_pending and background_work_allowed:
                    window = deep_pending[:self._DEEP_CHATS_PER_PASS]
                    logging.info(
                        "[deep-backfill] Pass %d: walking %d of %d chat(s) further back.",
                        attempt, len(window), len(deep_pending))
                    for jid in window:
                        if self._voice_call_in_progress():
                            break
                        if getattr(self, "_sync_run_id", 0) != my_run:
                            return
                        try:
                            deep_stored += self.deep_backfill_chat(jid)
                        except Exception as exc:
                            logging.warning("[deep-backfill] %s failed: %s", jid, exc)
                    if deep_stored:
                        logging.info(
                            "[deep-backfill] Pass %d stored %d older message(s); "
                            "%d chat(s) still to walk.",
                            attempt, deep_stored, len(self._chats_needing_deep_history()))
                        # Real progress renews the clock, capped by the same
                        # ceiling everything else respects. Deliberately not an
                        # unbounded loop: the walk is durable — exhausted_chats
                        # is persisted and the anchor is read from the database
                        # — so a budget that runs out costs a resume on the next
                        # launch, not a restart from the newest page.
                        deadline = min(hard_deadline,
                                       max(deadline, time.monotonic() + self._BACKFILL_BUDGET))

                if not pending:
                    # No chat is short of a page; names and/or deep history
                    # were this round's work.
                    attempted.clear()
                    if named or deep_stored:
                        retry_delay = self._BACKFILL_FIRST_DELAY
                    else:
                        retry_delay = min(retry_delay * 2, self._BACKFILL_MAX_DELAY)
                    delay = retry_delay
                    continue

                before = len(pending)
                # Sweep by remembering what has been tried, not by advancing an
                # index: `pending` shrinks as chats recover, so index arithmetic
                # over it skipped entries outright — some chats were never
                # retried at all. Once every pending chat has had a turn, the
                # record clears and the next cycle begins.
                untried = [j for j in pending if j not in attempted]
                if not untried:
                    attempted.clear()
                    untried = pending
                window = untried[:self._BACKFILL_CHUNK]
                finishes_sweep = len(untried) <= self._BACKFILL_CHUNK
                attempted.update(window)

                # Resolve through the @lid ↔ phone bridge: deduplicate_chats()
                # re-keys self.chats after the sync that recorded these JIDs.
                # The window is already capped at _BACKFILL_CHUNK — deliberately
                # gentler than sync_remote_chats(), because an unchunked pass
                # fired 463 get-messages calls in ~6 s through the one Puppeteer
                # page, on top of the media phase. None of this is urgent.
                targets, missing = [], []
                for j in window:
                    _key, chat = self._resolve_backfill_target(j)
                    if chat is None:
                        missing.append(j)
                    else:
                        targets.append(chat.copy())
                if missing:
                    # The chat is gone for good (deleted, or merged away) —
                    # stop asking about it.
                    logging.info("[backfill] Dropping %d pending JID(s) with no chat left.",
                                 len(missing))
                    for j in missing:
                        self._remove_backfill_pending(j)
                # Chunked and deliberately gentler than sync_remote_chats().
                # An unchunked pass fired 463 get-messages calls in ~6 s through
                # the one Puppeteer page — on top of the media downloads running
                # in parallel — which is a lot of automation traffic for an
                # account WhatsApp is already watching.  Nothing here is urgent:
                # this is history the user is not looking at yet, so it costs
                # nothing to spread it out.
                if targets:
                    logging.info("[backfill] Pass %d: retrying %d of %d chat(s) short of a "
                                 "full page (%d msg).",
                                 attempt, len(targets), before, self.history_page_target())
                # One line per pass saying how much work is left and how much
                # of the budget is gone. "The backfill is slow" and "the
                # backfill will not finish" look identical without it — and the
                # second is the case that silently truncates history, because
                # the budget expires with pending chats still queued and
                # nothing says so.
                logging.info(
                    "[backfill-queue] pass=%d short=%d deep=%d unnamed=%d "
                    "elapsed=%.0fs of %ds budget",
                    attempt, before, len(deep_pending), len(names_pending),
                    time.monotonic() - (deadline - self._BACKFILL_BUDGET),
                    self._BACKFILL_BUDGET,
                )
                # Snapshot what each target holds so progress can be measured in
                # messages, not just in chats that finished. A chat that went
                # from 15 to 90 messages made real progress and must not read as
                # a wasted pass — that is what backs the delay off.
                counts_before = {j: self._local_record_count(j) for j in window}
                # ...and which message is the oldest one on disk, which is the
                # only signal that separates "the phone sent us older history"
                # from "someone wrote in this chat". See the phone-request
                # block below for why that distinction is load-bearing.
                #
                # Only for the chats that could possibly ask the phone this
                # pass: this is a SQLite read each, and a window is up to
                # _BACKFILL_CHUNK chats while the short queue is typically a
                # handful. A chat already holding a full page is not a
                # candidate and is not read.
                _target = self.history_page_target()
                oldest_before = {
                    j: self._anchor_identity(self._oldest_stored_message(j))
                    for j, c in counts_before.items() if c < _target
                }
                if targets:
                    with ThreadPoolExecutor(max_workers=self._BACKFILL_WORKERS) as pool:
                        futs = [pool.submit(
                            self.sync_chat_messages, c, my_run) for c in targets]
                        for fut in as_completed(futs):
                            try:
                                fut.result()
                            except Exception as exc:
                                logging.warning("[backfill] chat sync failed: %s", exc)

                if getattr(self, "_sync_run_id", 0) != my_run:
                    logging.info("[backfill] A newer sync took over — stopping before phone requests.")
                    return

                # An unchanged short page is not proof that the conversation
                # only contains that many messages. It often means the linked
                # device exhausted its bounded local window (reported live as
                # dozens of chats stuck at exactly one message). Ask the phone
                # for older history, a few chats per pass, and keep every such
                # chat queued while its asynchronous reply is pending.
                phone_requests_left = self._OLDER_REQUESTS_PER_PASS
                attempts = getattr(self, "_older_request_attempts", None)
                if attempts is None:
                    attempts = self._older_request_attempts = {}
                older_arrived = set()
                for jid, was in counts_before.items():
                    now = self._local_record_count(jid)
                    if jid in oldest_before and (
                            self._anchor_identity(self._oldest_stored_message(jid))
                            != oldest_before[jid]):
                        # *Older* history arrived, so the ask this chat spent
                        # its budget on worked and the budget starts over.
                        #
                        # Deliberately not "the record count grew". A chat also
                        # grows when a message is sent or received in it, and
                        # reading that as backfill progress hands the chat two
                        # more phone requests — so every message the user sends
                        # into a short chat buys itself a round of sync
                        # notifications. The oldest stored message can only stay
                        # put or move further back (see _anchor_identity), which
                        # is exactly the question being asked here.
                        attempts.pop(jid, None)
                        older_arrived.add(jid)
                    if now >= self.history_page_target() or now > was:
                        continue
                    if jid in getattr(self, "_exhausted_chats", set()):
                        # Already answered, durably: the phone was asked and
                        # had nothing older. Re-queuing it here is what made
                        # that answer worthless — see the retirement below.
                        self._remove_backfill_pending(jid)
                        continue
                    if not self._user_has_opened(jid):
                        # Never opened, so nobody is waiting on its history and
                        # nobody would welcome a notification about it. The
                        # local fetch above still runs and still stores whatever
                        # WhatsApp Web has; only the phone is left alone.
                        continue
                    asked_at = getattr(self, "_older_requested_chats", {}).get(jid)
                    if HistoryMixin._older_history_is_exhausted(
                            asked_at, attempts.get(jid, 0), time.time(),
                            self._OLDER_REQUEST_GRACE,
                            self._MAX_PHONE_HISTORY_REQUESTS):
                        self._retire_chat_without_older_history(jid)
                        continue
                    self._keep_backfill_pending(jid, now)
                    if not HistoryMixin._phone_history_request_due(
                            asked_at, attempts.get(jid, 0), time.time(),
                            self._OLDER_REQUEST_GRACE,
                            self._MAX_PHONE_HISTORY_REQUESTS):
                        continue
                    if phone_requests_left <= 0:
                        continue
                    if not BackfillMixin._phone_request_gap_elapsed(
                            getattr(self, "_last_phone_request_at", None),
                            time.monotonic(), self._PHONE_REQUEST_MIN_GAP):
                        continue
                    phone_requests_left -= 1
                    self._last_phone_request_at = time.monotonic()
                    attempts[jid] = attempts.get(jid, 0) + 1
                    requested = self.request_older_messages(jid)
                    if requested is True:
                        if not hasattr(self, "_older_requested_chats"):
                            self._older_requested_chats = {}
                        self._older_requested_chats[jid] = time.time()
                        self._persist_older_requested()
                    elif requested is False:
                        # The API answered definitively that it did not send a
                        # request (normally primaryHasMore=false). This is the
                        # evidence that distinguishes a genuinely short chat —
                        # and it is also the terminal answer for a persisted gap
                        # whose phone no longer has any older page to provide.
                        self._remove_backfill_pending(jid)
                        # ...and record it as asked. _keep_backfill_pending()
                        # above runs before this decision on every pass, so the
                        # removal is undone by the next sweep and the chat comes
                        # straight back. Without a timestamp its asked_at stays
                        # None, the request is therefore always due, and a chat
                        # the API has already refused is re-asked every ~30 s
                        # for the whole backfill budget — measured at 40+ round
                        # trips for one @lid chat in a single 46-minute run.
                        if not hasattr(self, "_older_requested_chats"):
                            self._older_requested_chats = {}
                        self._older_requested_chats[jid] = time.time()
                        self._persist_older_requested()
                        with self._backfill_state_guard():
                            gap_forms = set(self._jid_address_forms(jid))
                            gap_forms.update(
                                self._jid_address_forms(self._canonical_backfill_jid(jid))
                            )
                            self._history_gap_jids.difference_update(gap_forms)

                completed = self._completed_backfill_targets(window)
                grew = sum(1 for j, was in counts_before.items()
                           if self._local_record_count(j) > was)
                logging.info(
                    "[backfill] Pass %d: %d chat(s) gained messages (%d of them older "
                    "history), %d no longer pending (of %d).",
                    attempt, grew, len(older_arrived), completed, before)
                made_progress = (grew > 0 or completed > 0 or named > 0
                                 or deep_stored > 0)
                # What may pull the *next* pass forward is narrower than what
                # justifies a repaint. A live message arriving in a short chat
                # is real progress for the UI and no evidence at all that the
                # phone has more history to give — resetting the backoff on it
                # is how an ordinary conversation drags the whole queue back to
                # a 30 s cadence, and with it the sync notifications.
                queue_advanced = (len(older_arrived) > 0 or completed > 0
                                  or named > 0 or deep_stored > 0)
                sweep_made_progress = sweep_made_progress or queue_advanced
                if made_progress:
                    # Unread badges, the "is this chat worth showing" decision and
                    # the displayed name all depend on this, so rebuild the list.
                    self._schedule_save()
                    wx.CallAfter(self._schedule_set_chats)
                # An unfinished sweep continues promptly; the retained retry
                # backoff changes only after every pending chat has had a turn.
                delay, retry_delay = BackfillMixin._backfill_short_queue_delays(
                    retry_delay, finishes_sweep, sweep_made_progress)
                if finishes_sweep:
                    attempted.clear()
                    sweep_made_progress = False
            still = len(self._collapse_and_list_backfill_pending())
            unnamed = len(self._pending_name_resolution())
            if still or unnamed:
                logging.info(
                    "[backfill] Budget spent with %d chat(s) still short of a full page "
                    "and %d still unnamed — WhatsApp Web never resolved them this session.",
                    still, unnamed)
        except Exception:
            logging.exception("[backfill] Unhandled error in the backfill loop")
        finally:
            self._persist_backfill_pending_state()
            self._persist_history_gap_jids()

    # ── History-sync health ─────────────────────────────────────────────────
    # WhatsApp's multi-device design keeps older history on the phone and only
    # pushes a bounded window to a linked device; that window arrives as
    # "history sync" chunks which WhatsApp Web decodes inside a Web Worker.
    # When that worker's bridge fails to come up the chunks pile up untouched
    # and WhatsApp Web ends up holding roughly one message per chat — which
    # get-messages then reports faithfully, so from WinZapp's side the sync
    # looks like it worked and simply found short conversations. That is
    # exactly the shape of the "only 15-20 messages per group" reports.
    #
    # There is nothing WinZapp can do about it at runtime, so these two
    # methods only observe and log. What they buy is that the next report of
    # this comes with the answer already in log.log instead of needing a
    # CDP session against the live page to find.

    def fetch_history_sync_status(self, timeout: int = 30):
        """Raw /history-sync-status payload, or None when it can't be read."""
        if not getattr(self, "_wa_connected", False):
            self._history_status_disconnected = True
            return None
        url = (f"{self.wpp_server}:{self.wpp_port}"
               f"/api/{self.token}/history-sync-status")
        try:
            response = api_get(
                url,
                headers={"Authorization": f"Bearer {self.token}",
                         "Content-Type": "application/json"},
                timeout=timeout,
            )
            if response.status_code not in (200, 201):
                response_text = response.text[:500]
                self._history_status_disconnected = (
                    "disconnected" in response_text.lower()
                    or "sessão do whatsapp não está ativa" in response_text.lower()
                )
                logging.warning("[history-sync] status endpoint returned %s: %s",
                                response.status_code, response_text[:200])
                return None
            body = response.json()
            self._history_status_disconnected = False
            return body.get("response") if isinstance(body, dict) else None
        except Exception as exc:
            # An older client/api/ build simply has no such route. Not worth a
            # warning on its own — the caller logs the miss once.
            logging.info("[history-sync] status endpoint unavailable: %s", exc)
            return None

    def log_history_sync_status(self, context: str = "") -> dict | None:
        """Log WhatsApp Web's history-sync health. Returns the payload."""
        status = self.fetch_history_sync_status()
        if not status:
            logging.info("[history-sync] No status available (%s).", context or "n/a")
            return None

        counts = status.get("storeCounts") or {}
        stored_msgs = counts.get("message")
        stored_chats = counts.get("chat")
        bridge_ready = status.get("backendWorkerBridgeReady")
        unprocessed = status.get("unprocessedChunks")

        logging.info(
            "[history-sync] %s: bridge_ready=%s persisted_storage=%s "
            "notification_api=%s unprocessed_chunks=%s wa_web_messages=%s "
            "wa_web_chats=%s on_demand=%s initial_complete=%s",
            context or "status", bridge_ready, status.get("persistedStorage"),
            status.get("notificationApi"), unprocessed, stored_msgs,
            stored_chats, status.get("onDemandEnabled"),
            status.get("initialSyncComplete"),
        )

        # The chunk map is the smoking gun when things are wrong: every entry
        # stuck at 'notification_stored' means nothing was ever decoded.
        chunk_status = status.get("chunkStatus")
        if isinstance(chunk_status, dict) and chunk_status:
            tally = {}
            for state in chunk_status.values():
                tally[state] = tally.get(state, 0) + 1
            logging.info("[history-sync] chunk states: %s", tally)

        if bridge_ready is False:
            logging.error(
                "[history-sync] WhatsApp Web's backend worker bridge is DOWN. "
                "History-sync chunks cannot be decoded, so WhatsApp Web itself "
                "holds almost no history (%s message(s) across %s chat(s)) and "
                "no sync, backfill or older-message request can recover it. "
                "This is an environment problem in the Chrome the API launches, "
                "not a WinZapp sync bug.", stored_msgs, stored_chats,
            )
        elif isinstance(unprocessed, int) and unprocessed > 0:
            logging.info(
                "[history-sync] %d chunk(s) still being decoded — history will "
                "keep growing for a while yet.", unprocessed,
            )
        return status

    def unblock_history_sync(self, timeout: int = 60) -> dict | None:
        """Free a history-sync queue stuck behind an unprocessable chunk.

        WhatsApp Web processes its notification queue by descending syncType,
        so an ON_DEMAND chunk always sorts ahead of every RECENT one — while
        being gated on the recent sync having *finished*. An on-demand request
        sent too early therefore parks a chunk at the head of a queue it can
        never leave, and the whole backlog behind it stops moving with no error
        anywhere. WinZapp used to create that state itself, from
        request_older_messages() during the initial sync; the API now refuses
        those, and this clears a queue already in that state.

        Safe to call unconditionally — the endpoint no-ops on a healthy queue.
        """
        if not getattr(self, "_wa_connected", False):
            return None
        url = (f"{self.wpp_server}:{self.wpp_port}"
               f"/api/{self.token}/unblock-history-sync")
        try:
            response = api_post(
                url,
                headers={"Authorization": f"Bearer {self.token}",
                         "Content-Type": "application/json"},
                timeout=timeout,
            )
            if response.status_code not in (200, 201):
                logging.warning("[history-sync] unblock endpoint returned %s: %s",
                                response.status_code, response.text[:200])
                return None
            body = response.json()
            payload = body.get("response") if isinstance(body, dict) else None
            if not isinstance(payload, dict):
                return None
            removed = payload.get("removed") or []
            if removed:
                logging.warning(
                    "[history-sync] Dropped %d on-demand chunk(s) that were "
                    "blocking %s pending recent chunk(s) and restarted the "
                    "processing loop. History should start filling in now.",
                    len(removed), payload.get("recentWaiting"),
                )
            else:
                logging.info(
                    "[history-sync] Queue not blocked (recent_completed=%s, "
                    "unprocessed=%s, on_demand_pending=%s).",
                    payload.get("recentCompleted"), payload.get("unprocessed"),
                    payload.get("onDemandPending"),
                )
            return payload
        except Exception as exc:
            # An older client/api/ build simply has no such route.
            logging.info("[history-sync] unblock endpoint unavailable: %s", exc)
            return None

    def request_older_messages(self, remote_jid: str, timeout: int = 60) -> bool | None:
        """Ask the phone for history older than what this device holds.

        Fire-and-forget by nature: the phone answers with a history-sync chunk
        minutes later, never in this response, so a True here means "the
        request went out", not "there are new messages now".

        The 60 s timeout is not generosity — a timeout here returns False, and
        the caller in fetch_older_messages() reads False as "the request never
        went out" and drops the chat into _exhausted_chats, which stops it from
        ever being re-queried. Timing out early therefore writes off exactly the
        chats that still have history coming.

        That used to be bounded by the session, because _exhausted_chats died
        with the process. It no longer is: the set is persisted, so the write-off
        outlives the run and takes the user's own scroll-up with it. What keeps
        the two apart now is _OLDER_REQUEST_GRACE — the write-off is only made
        durable once this request has had far longer than its reply window to be
        answered. See that constant.
        """
        if not getattr(self, "_wa_connected", False):
            return False
        jid = self._normalize_jid(remote_jid)
        # Same @lid-preferred addressing sync_chat_messages() uses, so a chat
        # the store only knows under its @lid still resolves.
        lid = getattr(self, "_phone_to_lid", {}).get(jid, "")
        if lid:
            phone = lid
        elif jid.endswith("@s.whatsapp.net"):
            phone = jid.split("@")[0] + "@c.us"
        else:
            phone = jid

        url = (f"{self.wpp_server}:{self.wpp_port}"
               f"/api/{self.token}/request-older-messages/{phone}")
        try:
            response = api_post(
                url,
                headers={"Authorization": f"Bearer {self.token}",
                         "Content-Type": "application/json"},
                timeout=timeout,
            )
            body = {}
            try:
                body = response.json()
            except Exception:
                pass
            payload = body.get("response") if isinstance(body, dict) else None
            if response.status_code in (200, 201) and isinstance(payload, dict) \
                    and payload.get("requested"):
                logging.info(
                    "[history-sync] Requested older messages from the phone for "
                    "%s (primary_has_more=%s).", jid, payload.get("primaryHasMore"),
                )
                return True
            if isinstance(payload, dict) and payload.get("primaryHasMore") is False:
                # Not a failure, and the commonest answer there is: WhatsApp
                # Web checked and the phone has nothing older for this chat, so
                # the request was deliberately not sent (see requestOlderMessages
                # in deviceController.ts — every one that IS sent lights up the
                # phone's lock screen with a sync notification).
                #
                # Logged apart from the generic "did not go out" line below
                # because it is the ordinary, expected outcome for roughly half
                # the queue, and a log full of 500s that are really "nothing to
                # do" costs a diagnosis the next time something is genuinely
                # wrong here.
                #
                # Still False, which is the terminal verdict the caller wants:
                # this chat leaves the backfill queue and is not asked again.
                logging.info(
                    "[history-sync] The phone has no older messages for %s — "
                    "not asking, and retiring it from the backfill queue.", jid,
                )
                return False
            if isinstance(payload, dict) and "recent history sync" in str(
                    payload.get("error", "")).lower():
                logging.info(
                    "[history-sync] Deferring older-message request for %s "
                    "until RECENT history finishes.", jid,
                )
                return None
            logging.info(
                "[history-sync] Older-message request for %s did not go out "
                "(status=%s, payload=%s).", jid, response.status_code,
                payload if payload else response.text[:200],
            )
            return False
        except Exception as exc:
            logging.warning(
                "[history-sync] Older-message request failed for %s: %s", jid, exc)
            return None

    def _start_deferred_media_sync(self) -> None:
        """Start media downloads after RECENT stops using the browser page."""
        if not getattr(self, "_media_sync_deferred", False):
            return
        if getattr(self, "_media_sync_running", False):
            return
        self._media_sync_deferred = False

        def _run():
            self._media_sync_running = True
            announced = False
            try:
                wx.CallAfter(self._set_status, self.i18n.t("downloading_media"))
                if not self.background_mode and self._announce_sync_events_enabled():
                    announced = True
                    wx.CallAfter(self.output, self.i18n.t("sync_media_started"))
                count = self.sync_media_for_all_chats()
                logging.info(
                    "[history-sync] Deferred media phase downloaded %d file(s).",
                    count)
                if announced:
                    connected = getattr(self, "_wa_connected", False)
                    offline = getattr(self, "offline_mode", False)
                    result = "sync_media_completed" if connected and not offline else "sync_media_failed"
                    wx.CallAfter(self.output, self.i18n.t(result))
            except Exception:
                logging.exception("[history-sync] Deferred media phase failed")
                if announced:
                    wx.CallAfter(self.output, self.i18n.t("sync_media_failed"))
            finally:
                self._media_sync_running = False
                wx.CallAfter(self._set_status, "")
                wx.CallAfter(self.set_chats)

        threading.Thread(
            target=_run, daemon=True, name="deferred-media-sync").start()

    def sync_media_for_all_chats(self, jids=None, should_stop=None) -> int:
        """Download not-yet-stored media, optionally limited to changed chats.

        Returns the number of files **actually downloaded**, not the number of
        candidate messages considered. That distinction is the whole point: it
        used to return len(tasks), which counts every media message in the
        cache whether or not anything was fetched for it. When the app was
        offline, sync_if_media() returned at its first line for all of them —
        1579 "tasks" completed in 71 ms having downloaded nothing — and the
        caller, seeing a count above zero, announced "download de mídias
        concluído" to the user. See start_sync()'s Phase 2.

        ``should_stop``, when given, is asked before each download starts and
        once more at the end (issue #198): a queue of thousands left running
        for a round that has been superseded fills media/ with files nothing on
        disk refers to. Downloads already running finish.
        """
        if self._voice_call_in_progress():
            logging.info("[sync_media_for_all_chats] paused during active voice call")
            return 0

        _MEDIA_TYPES = {"audioMessage", "documentMessage", "imageMessage",
                        "stickerMessage", "videoMessage",
                        "audio", "ptt", "document", "doc", "image", "sticker", "video"}
        allowed = None if jids is None else {self._normalize_jid(j) for j in jids if j}
        tasks = [
            msg
            for key, chat in self.chats.items()
            if (allowed is None or self._normalize_jid(chat.get("remoteJid") or key) in allowed)
            for msg in chat.get("messages", {}).get("messages", {}).get("records", [])
            if (msg.get("messageType") in _MEDIA_TYPES or msg.get("type") in _MEDIA_TYPES)
        ]
        if not tasks:
            return 0

        downloaded = 0
        timeout = self._MEDIA_SYNC_TIMEOUT
        def _download(msg):
            if self._voice_call_in_progress():
                return False
            if should_stop is not None and should_stop():
                return False
            return self.sync_if_media(msg, timeout)

        with ThreadPoolExecutor(max_workers=self._MEDIA_SYNC_WORKERS) as pool:
            futs = {pool.submit(_download, msg): msg for msg in tasks}
            for fut in as_completed(futs):
                try:
                    if fut.result():
                        downloaded += 1
                except Exception:
                    pass

        if should_stop is not None and should_stop():
            # Not saved: the run that superseded this one has emptied or
            # replaced media_failed.json for its own data, and this run's
            # expired ids stay in memory for the next save that is current.
            logging.info(
                "[sync_media_for_all_chats] Superseded — stopped after %d "
                "download(s) of %d candidate(s).", downloaded, len(tasks))
            return downloaded

        # Persist the set of expired IDs accumulated during this sync run.
        self._save_media_failed_ids()
        logging.info(
            "[sync_media_for_all_chats] Downloaded %d of %d candidate media message(s).",
            downloaded, len(tasks),
        )
        return downloaded

    def _refresh_open_conversation_after_sync(self, remote_jid: str, chat: dict) -> None:
        """Repaint the open conversation after sync_chat_messages() replaces
        its backing dict.

        sync_chat_messages() does `self.chats[remote_jid] = chat` — a NEW
        dict object, not a mutation of the old one. ConversationsPanel.
        conversation, if this chat happens to be open, is still holding that
        now-orphaned old object: its message list kept showing exactly what
        was loaded before this sync (e.g. before the app went offline) no
        matter how many messages this sync just merged in, until the user
        closed (Esc) and reopened the conversation to pick up
        self.chats[remote_jid] fresh. Point it at the new object and force a
        repaint instead — same pattern as the @lid-merge case elsewhere.

        Runs on a ThreadPoolExecutor worker thread (see sync_remote_chats()),
        so the swap-and-repaint is marshalled onto the main thread as one
        unit, re-checking the panel still has this jid open at that point
        (the user may have navigated away in the meantime).
        """
        cp = getattr(self, "conversations_panel", None)
        if cp is None or cp.conversation is None:
            return
        active_jid = cp.conversation.get("remoteJid", "")
        if not self._chat_jids_equivalent(active_jid, remote_jid):
            return

        def _apply(new_chat=chat, expected_jid=active_jid):
            current_jid = (
                cp.conversation.get("remoteJid", "")
                if cp.conversation is not None else ""
            )
            if self._chat_jids_equivalent(current_jid, expected_jid):
                cp.conversation = new_chat
                cp.refresh_messages_if_changed()
        wx.CallAfter(_apply)
