"""ConversationSyncMixin — part of MainWindow (see main_window/__init__.py).

Moved verbatim out of main.py. Methods run with ``self`` bound to the
MainWindow instance, so every attribute set in MainWindow.__init__ is
available here.
"""

import logging
import os
import threading
import time
import wx
from main_window.message_rules import (
    _MAX_ABSENT_CHAT_RETRIES,
    _MAX_EMPTY_DELTA_RETRIES,
    _message_ts,
    apply_history_sync_unread_correction,
    history_gap_closed,
    history_gap_detected,
    is_countable_message,
    unread_after_history_sync,
)
from core.remote_reconcile import (
    add_rollback_gap as _add_rollback_gap,
    deletions_within_remote_window as _deletions_within_remote_window,
    normalize_rollback_gaps as _normalize_rollback_gaps,
    observe_deletions as _observe_deletions,
    older_than_window as _older_than_window,
    oldest_anchor as _oldest_anchor,
    outside_rollback_gaps as _outside_rollback_gaps,
    split_deletions as _split_deletions,
)
from core.incremental_sync import (
    chat_message_records as _chat_message_records,
    messages_overlap as _messages_overlap,
    next_incremental_limit as _next_incremental_limit,
)
from urllib.parse import quote as _url_quote
from core.api_client import api_get
from core.message_edit import (
    carry_over_edited_marker,
    is_edit_event,
)
from core.quote_recovery import (
    carry_over_recovered_quotes,
    fill_placeholders_from_replies,
)
from core.utils import (
    carry_over_video_durations,
    display_page_fetch_limit,
    prune_message_record,
)
from core.remote_deletions import (
    comparable_local_ids,
    comparable_local_records,
    message_timestamp_seconds,
)


class ConversationSyncMixin:
    """Per-conversation message sync (sync_chat_messages), remote message windows
    and remote deletion/rollback reconciliation.
    """

    # Multipliers applied to the page size when re-querying a chat whose newest
    # page turned out to be disjoint from stored history. The API pages
    # backwards on its own (deviceController.ts walks up to 10 extra pages
    # under one `count`), so each step here is a single request, not N.
    _HISTORY_GAP_FACTORS = (4, 10)
    # Ceiling for one gap-closing request, matching what the API can actually
    # deliver: its internal loop stops at 10 extra pages.
    _HISTORY_GAP_MAX_COUNT = 2000

    def _normalize_fetched_messages(self, raw_messages, remote_jid: str) -> list:
        """WPPConnect get-messages payload -> WinZapp's canonical message dicts."""
        out = []
        edit_event_ids = set()
        for wm in raw_messages or []:
            if isinstance(wm, dict) and self.ws:
                try:
                    normalized = self.ws._normalize_wpp_message(wm)
                    # Never a row: the message it edits carries its current
                    # text wherever it is fetched from (core/message_edit.py).
                    if is_edit_event(normalized):
                        event_id = (normalized.get("key") or {}).get("id")
                        if event_id:
                            edit_event_ids.add(event_id)
                        continue
                    prune_message_record(normalized)
                    out.append(normalized)
                except Exception as e:
                    logging.error(f"[sync_chat_messages] Failed to normalize message in {remote_jid}: {e}")
        if edit_event_ids:
            self._remember_dropped_edit_events(edit_event_ids)
            wx.CallAfter(self._purge_materialized_edit_rows, remote_jid, edit_event_ids)
        return out

    @classmethod
    def _needs_display_page_refill(cls, raw_count: int, messages: list,
                                   page_size: int) -> bool:
        """Whether hidden records consumed slots in a saturated API page."""
        if raw_count < page_size:
            return False
        visible_count = sum(1 for message in messages
                            if cls._counts_as_last_message(message))
        return visible_count < page_size

    def _refetch_history_gap(self, remote_jid: str, phone: str, headers: dict,
                             page_size: int, local_records: list,
                             hole_top_ts: int = 0) -> list:
        """Re-query a chat with a wider window until it reaches stored history.

        Only called once history_gap_detected() has said the newest page is
        disjoint from what is on disk. Escalates `count` instead of walking
        page by page because the API already does the walking internally, and
        stops as soon as the widened window overlaps stored history — or as
        soon as the store stops yielding more, which is the honest signal that
        WhatsApp Web simply does not have the missing stretch (yet).

        Returns the widest set of messages it managed to fetch, or [] when the
        first widened request failed outright — never a *narrower* set than the
        caller already had.
        """
        widest = []
        best_len = page_size
        for factor in self._HISTORY_GAP_FACTORS:
            count = min(page_size * factor, self._HISTORY_GAP_MAX_COUNT)
            if count <= best_len:
                break
            url = (f"{self.wpp_server}:{self.wpp_port}/api/{self.token}"
                   f"/get-messages/{phone}?count={count}")
            try:
                # Deliberately longer than the 30s of the normal page: this
                # request makes WhatsApp Web walk back through up to ten pages.
                resp = api_get(url, headers=headers, timeout=90)
            except Exception as exc:
                logging.warning("[history-gap] %s: request failed at count=%d: %s",
                                remote_jid, count, exc)
                break
            if resp.status_code not in (200, 201):
                logging.warning("[history-gap] %s: HTTP %s at count=%d",
                                remote_jid, resp.status_code, count)
                break
            try:
                body = resp.json()
            except Exception:
                break
            raw = body.get("response", []) if isinstance(body, dict) else []
            if not isinstance(raw, list) or len(raw) <= best_len:
                logging.info(
                    "[history-gap] %s: store returned %d at count=%d — no more history "
                    "available, leaving the gap for a later pass.",
                    remote_jid, len(raw) if isinstance(raw, list) else 0, count)
                break
            best_len = len(raw)
            widest = self._normalize_fetched_messages(raw, remote_jid)
            if history_gap_closed(widest, local_records, hole_top_ts):
                logging.info("[history-gap] %s: closed at count=%d (%d messages).",
                             remote_jid, count, len(widest))
                break
        return widest

    def fetch_message_reactions(self, msg_id: str) -> "dict | None":
        """GET /reactions/{msgId} — DeviceController.getReactions(), already
        registered by wppconnect-server itself as
        /api/:session/reactions/:id (client/api_patches/src/routes/index.ts,
        unmodified from upstream). WinZapp never called it before this.

        Exists because reactions have no backfill path of their own: a
        reactionMessage only ever arrives as a live WebSocket event
        (WebSocketClient.on_wpp_reaction() / on_messages_upsert()), and
        get-messages — what every normal sync round re-fetches — replays
        WhatsApp Web's own message *history*, which does not include past
        reactions on messages it already has (confirmed against this
        endpoint's own response shape, retriever.layer.d.ts:
        getReactions() -> {reactionByMe, reactions: [{aggregateEmoji,
        hasReactionByMe, senders: [...]}]} — a live, current snapshot, not a
        history entry). So a reaction added while WinZapp was disconnected —
        the WebSocket never delivered it — is invisible forever unless
        something asks for it explicitly, per message, after the fact. See
        ConversationsPanel._backfill_reactions_for_open_conversation(),
        which does exactly that, bounded to the messages currently on
        screen — asking for every message in an account's whole history
        would be thousands of extra requests for nothing most of them ever
        had a reaction to find.

        Returns the raw `response` object on success, or None on any
        failure (offline, timeout, malformed body) — the caller treats
        "nothing found" and "could not check" identically, since neither
        should ever wipe an already-known reaction.
        """
        if not msg_id or not getattr(self, "_wa_connected", False):
            return None
        url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/reactions/{msg_id}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        try:
            response = api_get(url, headers=headers, timeout=8)
        except Exception:
            logging.exception(
                "[fetch_message_reactions] request failed for %s", msg_id)
            return None
        if response.status_code != 200:
            return None
        try:
            body = response.json()
        except Exception:
            return None
        payload = body.get("response") if isinstance(body, dict) else None
        return payload if isinstance(payload, dict) else None

    def sync_chat_messages(self, chat, expected_run_id=None, sync_mode="full",
                           fetched_ids_out=None):
        # fetched_ids_out: an optional set that receives the ids get-messages
        # actually returned for this chat, before they are merged with local
        # records -- the only way a caller can tell the server's answer apart
        # from what was already stored (Shift+F5, core/conversation_resync.py).
        # Deliberately NOT gated on an active voice call. sync_remote_chats()
        # counts only a False return as a failure, so bailing out here reported
        # every skipped chat as a *successful* fetch: message_sync_ok stayed
        # true, _persist_successful_sync_state() cleared the force_full latch
        # and committed the list-chats snapshot for chats nothing had read.
        # An account could be marked fully synced having fetched nothing, which
        # only F5 repairs. The call stands background work down where a *new*
        # round is decided instead — see _voice_call_in_progress().
        if (expected_run_id is not None
                and getattr(self, "_sync_run_id", 0) != expected_run_id):
            logging.info(
                "[sync_chat_messages] Skipping stale backfill/sync task from sync run %s.",
                expected_run_id)
            return False
        remote_jid = self._normalize_jid(chat.get("remoteJid", ""))
        chat["remoteJid"] = remote_jid
        
        user_part = remote_jid.split("@")[0] if "@" in remote_jid else remote_jid
        if not user_part or user_part == "0" or len(user_part) < 5:
            logging.warning(f"[sync_chat_messages] Aborting sync for invalid JID: {remote_jid}")
            return
            
        # Formata o JID corretamente para o WPPConnect.
        #
        # Prefer the @lid when the cache knows one. Measured against a live
        # session, get-messages on 12 private chats that exist under both
        # forms: @lid answered 200 with messages 12/12, the phone form
        # answered 401 12/12. Starting from the phone form does not lose
        # anything — the alternate-JID fallback below recovers — but it spends
        # one guaranteed-failing request per chat, which on an account with
        # hundreds of private chats is hundreds of wasted round-trips in the
        # very sync this is meant to make lighter.
        lid = getattr(self, "_phone_to_lid", {}).get(remote_jid, "")
        if lid:
            phone = lid
        elif remote_jid.endswith("@s.whatsapp.net"):
            phone = remote_jid.replace("@s.whatsapp.net", "@c.us")
        else:
            phone = remote_jid

        page_size = int(self.settings.get("user_interface", {}).get("messages_page_size", 200))
        local_chat_before = self.chats.get(remote_jid)
        if local_chat_before is None:
            try:
                local_chat_before = next(
                    (self.chats.get(form) for form in self._jid_address_forms(remote_jid)
                     if form in self.chats),
                    None,
                )
            except Exception:
                local_chat_before = None
        local_records_before = _chat_message_records(local_chat_before or {})
        incremental = sync_mode == "incremental" and bool(local_records_before)
        incremental_window = max(1, int(getattr(self, "_INCREMENTAL_MESSAGE_WINDOW", 50)))
        limit = min(page_size, incremental_window) if incremental else page_size
        refill_limit = display_page_fetch_limit(page_size)
        incremental_no_overlap = False
        url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/get-messages/{phone}?count={limit}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }

        # Always sync with WPPConnect API to ensure no messages are lost or missed due to stale lastMessage cache.

        all_messages = []
        api_ok = False
        fetched_payload_has_messages = False
        # The store answered "there is no such chat", for every JID form we
        # know. Deliberately NOT the same thing as api_ok being False: that is
        # an I/O fault to report, this is an answer. See the _absent_chats
        # block near the end of this method.
        chat_absent = False
        # The JID form that actually answered. Starts as the one built above and
        # is corrected when the alternate-JID fallback below is what worked —
        # the history-gap re-query has to reuse the form the store recognises,
        # not the one that just 401'd.
        fetch_jid = phone
        # Skip API call entirely if session is known disconnected
        if getattr(self, "_wa_connected", False):
            max_retries = 3
            for attempt in range(max_retries):
                if (expected_run_id is not None
                        and getattr(self, "_sync_run_id", 0) != expected_run_id):
                    logging.info(
                        "[sync_chat_messages] Cancelling stale backfill/sync retry for %s.",
                        remote_jid)
                    return False
                if not getattr(self, "_wa_connected", False):
                    logging.info(f"[sync_chat_messages] Connection lost during sync retry loop for {remote_jid}, aborting sync.")
                    break
                try:
                    logging.info(
                        "[sync_chat_messages] Fetching %s (attempt %d/%d)",
                        remote_jid, attempt + 1, max_retries)
                    # api_get logs the endpoint, the status, the duration and a
                    # correlation id the Node side reuses — and never the URL,
                    # which carries <session>:<token> in its path. The two
                    # lines this replaces printed it in full, twice per chat.
                    response = api_get(url, headers=headers, timeout=30)

                    # Alternate JID query fallback (resolves 401/TypeError or Chat not found errors)
                    both_jid_forms_failed = False
                    if response.status_code not in (200, 201):
                        alternate_jid = ""
                        if remote_jid.endswith("@lid"):
                            resolved = getattr(self, "_lid_to_phone", {}).get(remote_jid, "")
                            if resolved:
                                alternate_jid = resolved.replace("@s.whatsapp.net", "@c.us")
                        else:
                            # `phone` (the JID actually just queried) is the @lid
                            # form whenever a phone->LID mapping exists — see the
                            # "usamos o LID" preference above. Re-deriving the
                            # alternate from the SAME map here reproduces that
                            # identical @lid and silently retries the exact URL
                            # that just failed (observed live: a chat whose @lid
                            # form WA-JS has no store entry for — "Chat not found
                            # for X@lid" — kept re-querying that same @lid on
                            # every retry AND on the later backfill pass, never
                            # once trying the @c.us form). Try the @c.us form
                            # first since that's guaranteed to differ from a
                            # lid-preferred primary; only fall back to a fresh
                            # phone->LID lookup when the primary wasn't the LID
                            # form to begin with (no mapping existed yet).
                            cus_form = (
                                remote_jid.split("@")[0] + "@c.us"
                                if remote_jid.endswith("@s.whatsapp.net")
                                else remote_jid
                            )
                            if cus_form != phone:
                                alternate_jid = cus_form
                            else:
                                alt_lid = getattr(self, "_phone_to_lid", {}).get(remote_jid, "")
                                if alt_lid and alt_lid != phone:
                                    alternate_jid = alt_lid

                        if alternate_jid and alternate_jid != phone:
                            alt_url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/get-messages/{alternate_jid}?count={limit}"
                            logging.info(f"[sync_chat_messages] Primary query failed. Retrying with alternate JID {alternate_jid}...")
                            try:
                                alt_response = api_get(alt_url, headers=headers, timeout=30)
                                if alt_response.status_code in (200, 201):
                                    response = alt_response
                                    fetch_jid = alternate_jid
                                    logging.info("[sync_chat_messages] Fallback alternate JID query succeeded!")
                                else:
                                    both_jid_forms_failed = True
                            except Exception as alt_e:
                                logging.warning(f"[sync_chat_messages] Fallback alternate JID query failed: {alt_e}")
                                both_jid_forms_failed = True

                    if response.status_code in (200, 201):
                        body = response.json()
                        wpp_messages = body.get("response", []) if isinstance(body, dict) else []
                        logging.info(
                            "[sync_chat_messages] Fetched %d messages from API for %s "
                            "(mode=%s, count=%d)",
                            len(wpp_messages) if isinstance(wpp_messages, list) else 0,
                            remote_jid, "incremental" if incremental else "full", limit,
                        )
                        if not isinstance(wpp_messages, list):
                            wpp_messages = []
                        normalized_messages = self._normalize_fetched_messages(
                            wpp_messages, remote_jid)

                        # Warm-cache rounds start with a small newest-message
                        # window. If that window does not touch any locally
                        # known message and it saturated the requested count,
                        # grow geometrically until we either find overlap or
                        # reach the normal page size. This is the closest thing
                        # the current WPPConnect endpoint can provide to an
                        # `after_id` query: common reconnects cost 50 messages
                        # for only the chats that changed, while long outages
                        # automatically widen enough to prove/repair a gap.
                        while (incremental and local_records_before
                               and normalized_messages
                               and not _messages_overlap(normalized_messages, local_records_before)
                               and len(wpp_messages) >= limit
                               and limit < page_size):
                            next_limit = _next_incremental_limit(
                                limit, page_size, len(wpp_messages), False
                            )
                            if next_limit == limit:
                                break
                            expand_url = (
                                f"{self.wpp_server}:{self.wpp_port}/api/{self.token}"
                                f"/get-messages/{fetch_jid}?count={next_limit}"
                            )
                            logging.info(
                                "[sync_chat_messages] %s incremental window has no "
                                "local overlap; expanding %d -> %d.",
                                remote_jid, limit, next_limit,
                            )
                            try:
                                expanded_response = api_get(
                                    expand_url, headers=headers, timeout=30)
                                if expanded_response.status_code not in (200, 201):
                                    break
                                expanded_body = expanded_response.json()
                                expanded_messages = (
                                    expanded_body.get("response", [])
                                    if isinstance(expanded_body, dict) else [])
                                if not isinstance(expanded_messages, list):
                                    break
                                wpp_messages = expanded_messages
                                normalized_messages = self._normalize_fetched_messages(
                                    wpp_messages, remote_jid)
                                limit = next_limit
                            except Exception as expand_error:
                                logging.warning(
                                    "[sync_chat_messages] Incremental expansion failed "
                                    "for %s: %s", remote_jid, expand_error)
                                break

                        incremental_no_overlap = bool(
                            incremental and local_records_before and normalized_messages
                            and not _messages_overlap(normalized_messages, local_records_before)
                        )

                        # A full/new-chat fetch still guarantees one complete
                        # visible page. Incremental chats already have that page
                        # in the local cache, so re-fetching an oversized raw
                        # window merely to compensate for filtered protocol rows
                        # would defeat the point of the fast path.
                        if (not incremental and refill_limit > limit
                                and self._needs_display_page_refill(
                                    len(wpp_messages), normalized_messages, page_size)):
                            refill_url = (
                                f"{self.wpp_server}:{self.wpp_port}/api/{self.token}"
                                f"/get-messages/{fetch_jid}?count={refill_limit}"
                            )
                            logging.info(
                                "[sync_chat_messages] %s needs visible-page refill; "
                                "expanding raw window from %d to %d.",
                                remote_jid, limit, refill_limit)
                            try:
                                refill_response = api_get(
                                    refill_url, headers=headers, timeout=30)
                                if refill_response.status_code in (200, 201):
                                    refill_body = refill_response.json()
                                    refill_messages = (
                                        refill_body.get("response", [])
                                        if isinstance(refill_body, dict) else [])
                                    if (isinstance(refill_messages, list)
                                            and len(refill_messages) > len(wpp_messages)):
                                        normalized_messages = self._normalize_fetched_messages(
                                            refill_messages, remote_jid)
                            except Exception as refill_error:
                                logging.warning(
                                    "[sync_chat_messages] Visible-page refill failed "
                                    "for %s: %s", remote_jid, refill_error)
                        fetched_payload_has_messages = bool(normalized_messages)
                        all_messages.extend(normalized_messages)
                        api_ok = True
                        break
                    elif response.status_code in (401, 404, 500):
                        # 401 = "Error on open list" (Baileys not ready yet)
                        # 404 = session not active
                        # 500 = transient WPPConnect internal error
                        # All are retryable — wait briefly and try again. But
                        # WPPConnect flattens every internal exception (including
                        # a hard, non-transient one like "Chat not found for
                        # <jid>@lid" — the live session lost track of that chat
                        # entirely, no amount of retrying fixes it) into this
                        # same generic 401, so we can't tell hard failures apart
                        # from "session still warming up" by status code alone.
                        # If BOTH the primary and the only known alternate JID
                        # form have now failed twice, that's a strong enough
                        # signal to stop early — otherwise a handful of these
                        # permanently-broken chats can each burn a worker slot
                        # for over a minute, making the whole sync feel stuck.
                        logging.warning(f"[sync_chat_messages] Retryable error {response.status_code} for {remote_jid} (attempt {attempt+1}/{max_retries}): {response.text[:120]}")
                        hard_chat_miss = "chat not found" in response.text.lower()
                        if hard_chat_miss:
                            chat_absent = True
                            logging.warning(
                                "[sync_chat_messages] Giving up immediately for %s — "
                                "the store definitively reported chat_not_found.",
                                remote_jid,
                            )
                            break
                        if both_jid_forms_failed and attempt >= 1:
                            logging.warning(f"[sync_chat_messages] Giving up early for {remote_jid} — both JID forms failed on attempt {attempt+1}.")
                            break
                        if attempt < max_retries - 1:
                            sleep_time = min(5 * (attempt + 1), 20)
                            logging.info(f"[sync_chat_messages] Sleeping {sleep_time} seconds before attempt {attempt+2} for {remote_jid}...")
                            # Check connection repeatedly while sleeping
                            for _ in range(sleep_time):
                                if not getattr(self, "_wa_connected", False):
                                    break
                                time.sleep(1)
                        continue
                    else:
                        logging.error(f"[sync_chat_messages] API returned error status {response.status_code} for {remote_jid}: {response.text}")
                        break
                except Exception as e:
                    logging.error(f"[sync_chat_messages] failed to get messages for {remote_jid}: {e}")
                    break
        else:
            logging.info(f"[sync_chat_messages] Session disconnected, using cached messages for {remote_jid}")

        # Re-checked once the fetch is back (issue #198). The checks above only
        # stop a task that has not fetched yet, and everything from here on
        # writes: the gap set, sender names and the @lid bridge, self.chats,
        # the backfill queues and their files, the failure, empty-delta and
        # absent-chat sets, and last the database. A task whose round was
        # superseded while its request was out would put the previous
        # account's chat back into all of them — and on a confirmed logout
        # there is no second wipe to take it out again.
        def _superseded_while_fetching():
            if (expected_run_id is None
                    or getattr(self, "_sync_run_id", 0) == expected_run_id):
                return False
            logging.info(
                "[sync_chat_messages] Discarding %s: sync run %s was superseded "
                "while it was being fetched.", remote_jid, expected_run_id)
            return True

        if _superseded_while_fetching():
            return False

        # ── History-gap repair ───────────────────────────────────────────────
        # The page above is always the newest `limit` messages and nothing
        # else, so a chat that outran that window while WinZapp was closed
        # comes back describing a stretch of time we have no messages for, and
        # nothing downstream ever asks for the middle: the merge below unions
        # two disjoint blocks, and _note_backfill_state() reads a full page as
        # proof the chat is complete. See history_gap_detected().
        with self._backfill_state_guard():
            gap_jids = getattr(self, "_history_gap_jids", None)
            if gap_jids is None:
                gap_jids = self._history_gap_jids = set()
            gap_forms = set(self._jid_address_forms(remote_jid))
            gap_forms.update(self._jid_address_forms(self._canonical_backfill_jid(remote_jid)))
            was_gap = bool(gap_forms & gap_jids)

        gap_open = was_gap
        if api_ok and all_messages:
            # Snapshot before the merge below folds the fetched page into it.
            gap_reference = (self.chats.get(remote_jid, {})
                             .get("messages", {})
                             .get("messages", {})
                             .get("records") or [])
            saturated_incremental_gap = bool(
                incremental_no_overlap and limit >= page_size
            )
            detected_gap = (
                history_gap_detected(all_messages, gap_reference, page_size)
                or saturated_incremental_gap
            )
            gap_open = False
            if detected_gap:
                # Ceiling of the hole: the oldest message the narrow page
                # reached. Everything stored below it is the far side we are
                # trying to get back down to.
                hole_top_ts = min(_message_ts(message) for message in all_messages)
                logging.warning(
                    "[history-gap] %s: newest window of %d is disjoint from %d stored "
                    "message(s) — widening the window.",
                    remote_jid, len(all_messages), len(gap_reference))
                wider = self._refetch_history_gap(
                    remote_jid, fetch_jid, headers, page_size, gap_reference, hole_top_ts)
                # The widening is more requests. Nothing after this line waits
                # on the network before the database write at the end.
                if _superseded_while_fetching():
                    return False
                if len(wider) > len(all_messages):
                    all_messages = wider
                if not history_gap_closed(all_messages, gap_reference, hole_top_ts):
                    gap_open = True
                    logging.warning(
                        "[history-gap] %s: gap still open after widening — queued "
                        "for the backfill.", remote_jid)
            elif incremental_no_overlap:
                # A changed chat returned a short provisional window that does
                # not touch our cache. Do not delete local history and do not
                # pretend the chat is complete: WhatsApp Web is still filling
                # this store entry, so the normal background backfill should
                # confirm it later without turning the whole client back into a
                # full-sync loop.
                gap_open = True
                logging.info(
                    "[history-gap] %s: short incremental window has no local "
                    "overlap — queued for background confirmation.", remote_jid)

            with self._backfill_state_guard():
                self._history_gap_jids.difference_update(gap_forms)
                if gap_open:
                    self._history_gap_jids.add(self._canonical_backfill_jid(remote_jid))
        elif not api_ok or not all_messages:
            # No successful message evidence means an already-known gap must
            # survive this attempt. In particular, an empty 200 response while
            # WhatsApp Web is warming up is not proof that the missing stretch
            # vanished. A new incremental empty response is handled by the
            # durable message-retry latch returned below rather than inventing
            # a history gap from no data.
            if was_gap:
                with self._backfill_state_guard():
                    self._history_gap_jids.difference_update(gap_forms)
                    self._history_gap_jids.add(self._canonical_backfill_jid(remote_jid))

        # NOTE on "conversation cleared from the phone": there is deliberately
        # no automatic mirroring here.  The only local evidence would be
        # get-messages answering 200 with an empty list, and that is
        # indistinguishable from "WhatsApp Web has not loaded this chat's
        # history into its store yet" — which is routine right after pairing or
        # a reconnect.  Acting on it would silently destroy the user's local
        # history.  (list-chats cannot help either: WPPConnect serialises the
        # raw ChatModel with msgs:null, so it carries no last-message data at
        # all.)  A clear made on the phone therefore only reaches WinZapp when
        # the user clears the conversation here as well.

        # Drop messages the user cleared (older than the clear-chat cutoff) so a
        # cleared conversation does not silently repopulate on the next sync.
        if all_messages:
            all_messages = [m for m in all_messages
                            if not self._is_cleared_message(remote_jid, m)]

        # Keep only messages that actually belong to THIS chat. WPPConnect's
        # get-messages can answer a group-participant @lid/@c.us chat with
        # messages from the GROUPS where that participant wrote (the browser
        # store indexes those messages under the participant's id too) —
        # storing them as 1:1 history "confirms" the phantom conversation and
        # pushes its preview into the chat list. Real 1:1 messages normalize
        # to the chat's own remote_jid; group messages normalize to @g.us and
        # are dropped here. (Defense in depth behind the get_remote_chats
        # lastReceivedKey filter — a phantom chat that somehow slipped in must
        # not be able to accumulate history.)
        if all_messages:
            matching_messages = []
            for message in all_messages:
                key = message.get("key") or {}
                message_jid = self._normalize_jid(key.get("remoteJid", ""))
                if not self._chat_jids_equivalent(message_jid, remote_jid):
                    continue
                # Keep the database canonical too. Downstream code expects a
                # message stored under a phone chat to carry that same phone JID,
                # not the @lid form that happened to answer the API request.
                key["remoteJid"] = remote_jid
                matching_messages.append(message)
            all_messages = matching_messages

        if fetched_ids_out is not None and api_ok:
            fetched_ids_out.update(
                (m.get("key") or {}).get("id") for m in all_messages
                if (m.get("key") or {}).get("id"))

        # After fetching, update chat messages
        for msg in all_messages:
            self._extract_lid_mapping(msg)
        # Learn sender names from the synced history as well.  Without this,
        # every group message fetched by the initial sync had no resolvable
        # sender (its participant is usually a bare @lid), and only messages
        # arriving live afterwards ever got a name.
        if self._learn_sender_names_bulk(all_messages):
            self._schedule_save(contacts_dirty=True)
        # Preserve any messages received via WebSocket during this sync that
        # the API hasn't indexed yet (they arrived after the API snapshot).
        local_chat    = self.chats.get(remote_jid, {})
        local_records = (local_chat.get("messages", {})
                         .get("messages", {})
                         .get("records", []))
        if api_ok and all_messages and local_records:
            chat["unreadCount"] = unread_after_history_sync(
                chat.get("unreadCount", 0),
                local_chat.get("unreadCount", 0),
                all_messages,
                local_records,
            )

        if local_records:
            # A duration WinZapp measured from the file itself is not
            # something the server knows, so the API copy of that same video
            # arrives stating none — carry it across before the API copy
            # replaces the record, or the length disappears from the list on
            # every sync. The database keeps the same rule on its own side
            # (DatabaseManager._with_known_video_duration).
            carried = carry_over_video_durations(all_messages, local_records)
            if carried:
                logging.info("[sync_chat_messages] %s: kept %d measured video duration(s)",
                             remote_jid, carried)
            # Same shape for the "Editada" marker, which the server copy may
            # not restate (core/message_edit.carry_over_edited_marker()).
            carried_edits = carry_over_edited_marker(all_messages, local_records)
            if carried_edits:
                logging.info("[sync_chat_messages] %s: kept %d edited marker(s)",
                             remote_jid, carried_edits)
            # And for text recovered from a reply's quote, which the server
            # copy -- still a ciphertext -- would otherwise wipe.
            carried_quotes = carry_over_recovered_quotes(all_messages, local_records)
            if carried_quotes:
                logging.info("[sync_chat_messages] %s: kept %d recovered quote(s)",
                             remote_jid, carried_quotes)
            api_ids = {r.get("key", {}).get("id") for r in all_messages}
            # A copy an edit event was once stored as is local-only by
            # construction — keeping it is the duplicate (core/message_edit.py).
            dropped_edit_ids = getattr(self, "_dropped_edit_event_ids", set())
            extra   = [r for r in local_records
                       if r.get("key", {}).get("id") and
                          r.get("key", {}).get("id") not in api_ids
                          and r.get("key", {}).get("id") not in dropped_edit_ids
                          # Also apply the clear-chat cutoff here: local_records
                          # comes from the on-disk cache, which can still hold
                          # pre-clear messages if the app was closed before the
                          # debounced save after clear_chat_messages_local() ran.
                          # Without this check those stale records get merged
                          # right back in, making "clear chat" undone by the
                          # next sync / app restart.
                          and not self._is_cleared_message(remote_jid, r)]
            if extra:
                all_messages = all_messages + extra

        # Deduplicate: when the same message exists as both an API copy (real
        # WhatsApp ID) and a pending virtual copy (local UUID), keep the API
        # version and drop the pending one.  The hash-set approach below ensures
        # the first occurrence (API) survives, removing the pending dup.
        seen = set()
        deduped = []
        for m in all_messages:
            mid = m.get("key", {}).get("id", "")
            if mid and mid in seen:
                continue
            if mid:
                seen.add(mid)
            deduped.append(m)
        all_messages = deduped

        # Sort by timestamp so the conversation always shows the most recent
        # messages at the bottom. The user scrolls up to see older history.
        all_messages.sort(
            key=lambda m: int(
                m.get("messageTimestamp") or m.get("timestamp") or m.get("t") or 0
            )
        )

        # ── Late-arriving race-condition fix ─────────────────────────────────
        # on_historical_message() and on_new_message() run on the wx main thread
        # and may have inserted messages into self.chats[remote_jid] AFTER we
        # took the local_records snapshot above but BEFORE we write back below.
        # Do a second merge against the live chat to ensure none of those
        # messages are silently discarded by our final self.chats assignment.
        live_chat    = self.chats.get(remote_jid, {})
        live_records = (live_chat.get("messages", {})
                        .get("messages", {})
                        .get("records", []))
        if live_records:
            current_ids = {r.get("key", {}).get("id") for r in all_messages}
            dropped_edit_ids = getattr(self, "_dropped_edit_event_ids", set())
            late_extra  = [r for r in live_records
                           if r.get("key", {}).get("id") and
                              r.get("key", {}).get("id") not in current_ids
                              and r.get("key", {}).get("id") not in dropped_edit_ids
                              and not self._is_cleared_message(remote_jid, r)]
            if late_extra:
                all_messages = all_messages + late_extra
                # Re-sort to keep chronological order
                all_messages.sort(
                    key=lambda m: int(
                        m.get("messageTimestamp") or m.get("timestamp") or m.get("t") or 0
                    )
                )

        # A reply fetched here -- or one already stored -- may quote an
        # "Aguardando mensagem" in this chat; the batch write below persists
        # what it fills. Guarded: this is not an I/O fault and must never be
        # able to report the fetch as a failed one (docs/traps/sync-completion.md).
        try:
            recovered = fill_placeholders_from_replies(all_messages, self._chat_jids_equivalent)
            if recovered:
                logging.info("[sync_chat_messages] %s: recovered %d message(s) from "
                             "reply quotes", remote_jid, len(recovered))
        except Exception:
            logging.exception("[sync_chat_messages] %s: quote recovery failed", remote_jid)

        # Update records: accept API data only when it actually returned some
        # messages, or fall back to preserving whatever we have in memory.
        # An empty API response (200 OK with no messages) must NOT wipe the
        # cached records, otherwise conversations appear empty after sync.
        has_records = bool(chat.get("messages", {}).get("messages", {}).get("records"))
        if api_ok and all_messages:
            if "messages" not in chat:
                chat["messages"] = {}
            chat["messages"]["messages"] = {
                "total": len(all_messages),
                "pages": 1,
                "currentPage": 1,
                "records": all_messages
            }
        elif not has_records:
            if "messages" not in chat:
                chat["messages"] = {}

        # The chat-list snapshot set this badge before a single message of this
        # conversation had been fetched, so its own discount could not run.
        # Now it can.
        apply_history_sync_unread_correction(remote_jid, chat)

        # Update lastMessage from the newest displayable message, so the chat
        # list preview matches what opening the conversation actually shows.
        #
        # `t` is only ever raised here, never lowered to the message's own
        # timestamp. The two are not the same clock: `t` is the server's
        # activity marker, and it legitimately sits ABOVE the newest displayable
        # message whenever the latest thing that happened in the chat was a
        # system event — a group join, a settings change, a revoke — which
        # _counts_as_last_message() excludes from `candidates` by design.
        # Overwriting `t` downward in that case costs twice: the conversation
        # sorts to a position that contradicts the server's own ordering, and
        # _capture_chat_sync_baseline() then stores a marker that can never
        # match the next list-chats snapshot, so chat_sync_marker_changed()
        # reports a change on every round and that chat is never skipped again —
        # the one outcome the incremental planner exists to produce.
        candidates = [m for m in all_messages if self._counts_as_last_message(m)]
        if candidates:
            def _get_m_ts(m):
                val = int(m.get("messageTimestamp") or m.get("timestamp") or m.get("t") or 0)
                return val // 1000 if val > 1_000_000_000_000 else val
            last_m = max(candidates, key=_get_m_ts)
            chat["lastMessage"] = last_m
            last_ts = _get_m_ts(last_m)
            try:
                current_t = int(chat.get("t") or 0)
            except (TypeError, ValueError):
                current_t = 0
            if last_ts > current_t:
                chat["t"] = last_ts

        self.chats[remote_jid] = chat
        pending_before = self._is_backfill_pending(remote_jid)
        self._note_backfill_state(remote_jid, chat, api_ok)
        pending_after = self._is_backfill_pending(remote_jid)
        if was_gap or gap_open:
            self._persist_history_gap_jids()
        if pending_before or pending_after:
            self._persist_backfill_pending_state()
        if not api_ok and not chat_absent:
            # Counted so the sync stops reporting a clean run over chats whose
            # messages never arrived. sync_remote_chats() cannot see this from
            # the future alone: exhausting the retries returns normally, it
            # does not raise. A chat the store says does not exist is excluded
            # here on purpose — nothing failed, so there is nothing to report.
            with self._sync_failures_lock:
                self._sync_failed_chats.add(remote_jid)

        # This replaces self.chats[remote_jid] with a NEW dict object rather
        # than mutating the old one in place — if the conversation is open
        # right now, ConversationsPanel.conversation is still holding that
        # now-orphaned old object, so its message list kept showing exactly
        # what was loaded before this sync (e.g. before the app went
        # offline) no matter how many messages this sync just merged in,
        # until the user closed (Esc) and reopened the conversation to pick
        # up self.chats[remote_jid] fresh. Point it at the new object and
        # force a repaint here, same as the @lid-merge case above.
        self._refresh_open_conversation_after_sync(remote_jid, chat)

        if not getattr(self, "_initial_sync_running", False):
            wx.CallAfter(self._schedule_set_chats)

        # A changed warm chat answering 200/201 with an empty/filtered payload
        # is not a successful delta fetch: list-chats already told us activity
        # advanced, so keep that chat on the durable retry list. Full/new-chat
        # rounds retain the historical behaviour where an empty store page can
        # be provisional and is handled by the short-history backfill.
        #
        # Bounded, and separated from real failures, because neither property is
        # optional here. The server answered 200 — there is no I/O fault to
        # report — but "activity advanced" and "the delta is empty" is a state
        # WhatsApp Web reaches routinely and permanently: a reaction, a
        # groupNotification, any event _normalize_fetched_messages() filters out
        # entirely will bump `t` and yield nothing to fetch, forever. Treated as
        # a failure, one such chat was enough to hold message_sync_ok False,
        # which keeps _sync_completed False, which (a) never commits the
        # list-chats snapshot of unread/pin/archive for EVERY chat, (b) leaves
        # the health checker resyncing — announcing itself — on every cooldown,
        # and (c) makes the live-event gate drop every chats.update unread event
        # for the rest of the session. So: look again a couple of times in case
        # the store was merely slow, then accept the marker and move on.
        incremental_satisfied = not incremental or fetched_payload_has_messages
        if api_ok:
            # getattr-guarded like every other lazily-present sync attribute
            # here: the test stubs that bind this method carry only what the
            # path under test touches.
            # Under _sync_failures_lock, the same lock clear_local_data() holds
            # while emptying these: F5 can land between the read and the write
            # here and leave a jid from the discarded run behind — one wasted
            # get-messages, but the asymmetry is the kind that reads as a bug
            # to whoever touches this next. The lock is not reentrant and this
            # block calls nothing that takes it.
            with self._sync_failures_lock:
                # The chat answered, so it exists: forget any earlier absence,
                # and let a future disappearance start counting from scratch.
                if getattr(self, "_absent_chats", None):
                    self._absent_chats.discard(remote_jid)
                if getattr(self, "_absent_chat_attempts", None):
                    self._absent_chat_attempts.pop(remote_jid, None)
                attempts_by_jid = getattr(self, "_delta_unsatisfied_attempts", None)
                if attempts_by_jid is None:
                    attempts_by_jid = self._delta_unsatisfied_attempts = {}
                unsatisfied_jids = getattr(self, "_delta_unsatisfied_chats", None)
                if unsatisfied_jids is None:
                    unsatisfied_jids = self._delta_unsatisfied_chats = set()
                if incremental_satisfied:
                    attempts_by_jid.pop(remote_jid, None)
                    unsatisfied_jids.discard(remote_jid)
                else:
                    attempts = attempts_by_jid.get(remote_jid, 0) + 1
                    if attempts >= _MAX_EMPTY_DELTA_RETRIES:
                        attempts_by_jid.pop(remote_jid, None)
                        unsatisfied_jids.discard(remote_jid)
                        incremental_satisfied = True
                        logging.info(
                            "[sync_chat_messages] %s: delta still empty after %d "
                            "attempts — accepting the activity marker.",
                            remote_jid, attempts,
                        )
                    else:
                        attempts_by_jid[remote_jid] = attempts
                        unsatisfied_jids.add(remote_jid)
        # chat_not_found is the store ANSWERING, not failing — the same
        # distinction the empty-delta block above exists for, reached through
        # the other door. It arrives as a 404 whose body says
        # {"reason":"chat_not_found"}, and it is a stable fact: WhatsApp Web
        # looked, under every JID form we know (the alternate-JID fallback runs
        # before this), and there is no such chat.
        #
        # Recognised by the literal phrase in the body, not by the status code
        # or by that `reason` field, and deliberately so in both directions.
        # An older/unpatched server flattens the same failure into a 401 or a
        # 500 (see hard_chat_miss above), so the status is not reliable; and
        # deviceController's own classifier stamps `chat_not_found` on anything
        # matching /not found/i, which "Session not found" and "Message not
        # found" also match — trusting it would let a genuine transient fault
        # be recorded as a chat that does not exist, which is the one way this
        # relaxation could mask a real failure. If WPPConnect ever reworded the
        # message, this stops matching and the chat goes back to being counted
        # as a failed fetch: the old behaviour, not a false success. It happens routinely for a
        # JID that only ever appeared in an e2e_notification — an encryption
        # housekeeping event, never a conversation. is_countable_message()
        # already keeps one of those off the badge and out of the sort order,
        # but the entry it leaves in self.chats is still a sync target, so
        # every round asked for its messages and every round got a 404.
        # Counted as a failure, that single phantom chat held message_sync_ok
        # False forever: last_success stayed "never", force_full_pending was
        # never released, and the health checker resynced — announcing itself
        # to the screen reader — on every cooldown, with all 155 chats replanned
        # as forced-full each time. Observed live, exactly as described above.
        #
        # Bounded rather than latched forever: a JID with no chat behind it
        # today can get one tomorrow (the person finally writes), so it is
        # worth a few more looks, but the latch itself has to terminate —
        # every new e2e_notification mints another such entry, and an entry
        # that never leaves _absent_chats never leaves _message_retry_jids
        # either, which is a durable, persisted list.
        #
        # What retiring buys is exactly that drain, and nothing more. It does
        # NOT stop the chat being queried: a phantom has no stored records and
        # a nonzero `t`, which is _plan_message_sync()'s "missing-local-history"
        # case, so it is re-selected as a full target every round regardless of
        # this latch. That costs one get-messages per round per phantom and is
        # deliberately left alone — narrowing missing-local-history is a change
        # to the planner, and the planner is not what was broken here.
        #
        # Retiring is also safe in the other direction: nothing marks the chat
        # as skippable, so if it ever becomes real, both its activity marker
        # moving and that same missing-local-history rule bring it straight
        # back.
        absent_satisfied = False
        if chat_absent:
            with self._sync_failures_lock:
                absent_attempts = getattr(self, "_absent_chat_attempts", None)
                if absent_attempts is None:
                    absent_attempts = self._absent_chat_attempts = {}
                absent_jids = getattr(self, "_absent_chats", None)
                if absent_jids is None:
                    absent_jids = self._absent_chats = set()
                attempts = absent_attempts.get(remote_jid, 0) + 1
                if attempts >= _MAX_ABSENT_CHAT_RETRIES:
                    absent_attempts.pop(remote_jid, None)
                    absent_jids.discard(remote_jid)
                    absent_satisfied = True
                    logging.info(
                        "[sync_chat_messages] %s: still chat_not_found after %d "
                        "attempts — retiring it from the retry list.",
                        remote_jid, attempts,
                    )
                else:
                    absent_attempts[remote_jid] = attempts
                    absent_jids.add(remote_jid)

        message_fetch_satisfied = bool(
            (api_ok and incremental_satisfied) or (chat_absent and absent_satisfied)
        )

        # Incremental DB save: write only this chat + its messages. The chat-list
        # activity marker is committed only after the message request that marker
        # selected has succeeded. Otherwise a process crash could persist the new
        # `t`/lastReceivedKey without its message and make the next startup skip it.
        persist_ok = True
        try:
            # Commit message rows first and the chat activity marker last. If
            # the process dies between those writes, the safe failure mode is
            # an old marker with already-present messages (which causes one
            # harmless re-fetch), never a new marker without its message.
            if api_ok and all_messages:
                self.db.insert_messages_batch(remote_jid, all_messages)
            if message_fetch_satisfied:
                self.db.upsert_chat(remote_jid, chat)
                # Same condition as committing the marker, for the same reason:
                # this chat has now been queried up to the activity it claims,
                # so local_history_behind_server() may stop asking about it for
                # the rest of this session even if the tail never materialised
                # as a stored message.
                self._note_verified_activity(remote_jid, chat)
        except Exception as exc:
            persist_ok = False
            logging.warning("[sync_chat_messages] incremental DB save failed for %s: %s",
                            remote_jid, exc)

        # Outside the block above, and guarded on its own. This is bookkeeping
        # for the staleness net and nothing reads it to decide correctness —
        # letting it raise in there would set persist_ok False and report a
        # perfectly good fetch as a failed one, which is how a diagnostic
        # starts causing the resync loop it was added to help diagnose.
        if message_fetch_satisfied:
            try:
                self._note_chat_verified_now(remote_jid)
            except Exception as exc:
                logging.warning("[sync_chat_messages] could not record the "
                                "verification time for %s: %s", remote_jid, exc)

        # Reports whether this chat's sync FAILED, which neither an empty delta
        # nor a chat_not_found did: the retry for those is carried by
        # _delta_unsatisfied_chats/_absent_chats, which sync_remote_chats()
        # folds into the durable retry list without letting either count as a
        # failed run.
        return bool((api_ok or chat_absent) and persist_ok)

    # ── Phone-side deletions/clears — active conversation only ──────────────
    # sync_chat_messages() above deliberately never removes anything: its
    # "extra"/"late_extra" merges exist specifically to protect messages that
    # arrived live but the API snapshot hasn't indexed yet, so reusing it here
    # would silently undo real deletions. Detecting a message (or a whole
    # conversation) that vanished from the phone needs its own comparison —
    # kept scoped to the conversation the user has open right now: diffing
    # every chat's messages against the server on every 60s poll would turn
    # one cheap GET into dozens/hundreds against a local API that is already
    # doing real work, for a benefit (a stale bubble the user probably
    # wouldn't notice) that doesn't justify the cost. For the open
    # conversation specifically, the cost is one extra GET per poll and the
    # payoff (not staring at a message that no longer exists, or a "cleared"
    # conversation that stays full until F5) is worth it.

    def _get_remote_messages(self, remote_jid: str,
                             extra_query: str = "") -> "tuple[list, int, int | None] | None":
        """Best-effort GET of get-messages for remote_jid.

        Returns ((normalised, raw) pairs, number of raw items in the answer,
        oldest timestamp among the RAW items or None), or None on ANY
        failure/ambiguity — a failed fetch must never be read as "the phone
        deleted everything". Messages go through the same
        _normalize_wpp_message() sync_chat_messages() uses, so their key.id
        compares equal to what's stored locally; the raw item is kept because
        only it carries the full serialized id an anchored query needs.

        The oldest timestamp is read off the raw items, before and regardless
        of the normaliser: an answer is bounded by everything the server
        returned, including entries the normaliser cannot map (see
        core/remote_deletions.py).
        """
        if not self.ws:
            return None
        lid = getattr(self, "_phone_to_lid", {}).get(remote_jid, "")
        if lid:
            phone = lid
        elif remote_jid.endswith("@s.whatsapp.net"):
            phone = remote_jid.split("@")[0] + "@c.us"
        else:
            phone = remote_jid
        limit = int(self.settings.get("user_interface", {}).get("messages_page_size", 200))
        url = (f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/get-messages/"
               f"{phone}?count={limit}{extra_query}")
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        try:
            response = api_get(url, headers=headers, timeout=15)
            if response.status_code not in (200, 201):
                return None
            body = response.json()
            wpp_messages = body.get("response", []) if isinstance(body, dict) else []
            if not isinstance(wpp_messages, list):
                return None
            pairs = []
            oldest = None
            for wm in wpp_messages:
                if not isinstance(wm, dict):
                    continue
                ts = message_timestamp_seconds(
                    {"messageTimestamp": wm.get("t") or wm.get("timestamp")}
                )
                if ts and (oldest is None or ts < oldest):
                    oldest = ts
                try:
                    normalized = self.ws._normalize_wpp_message(wm)
                except Exception:
                    continue
                if normalized.get("key", {}).get("id", ""):
                    pairs.append((normalized, wm))
            return pairs, len(wpp_messages), oldest
        except Exception as e:
            logging.warning(f"[_get_remote_messages] failed for {remote_jid}: {e}")
            return None

    def _fetch_remote_message_window(self, remote_jid: str) -> "tuple[set[str], int | None, str] | None":
        """The newest messages WhatsApp Web has for remote_jid:
        (message ids, oldest timestamp in seconds — None when the answer is
        empty, serialized id of the oldest message — '' when none), or None.

        The oldest timestamp bounds which local messages this answer can say
        anything about: only the ones WhatsApp Web has loaded, never older
        history. The serialized id is where _deletions_before_remote_window()
        continues from (see core/remote_reconcile.py).
        """
        answer = self._get_remote_messages(remote_jid)
        if answer is None:
            return None
        pairs, raw_count, oldest = answer
        ids = {n["key"]["id"] for n, _raw in pairs}
        # The server answered with entries, yet they yielded no id or no
        # timestamp: the normaliser or the payload shape broke, not the
        # conversation. Reading that as data is how it would go wrong —
        # no ids is the shape of a phone-side clear (three polls later the
        # whole conversation is wiped), and ids with no timestamp leave the
        # comparison unbounded (the oldest local messages deleted again).
        # Only a genuinely empty answer may count toward a clear.
        if raw_count and (not ids or oldest is None):
            logging.warning(
                "[_fetch_remote_message_window] %s: %d entr(ies) answered but "
                "%d id(s), oldest timestamp %s — treating as ambiguous.",
                remote_jid, raw_count, len(ids), oldest,
            )
            return None
        return ids, oldest, _oldest_anchor(pairs)

    def _fetch_remote_messages_before(self, remote_jid: str,
                                      anchor_id: str) -> "tuple[set[str], int | None, str] | None":
        """The page of messages WhatsApp Web's database holds before anchor_id:
        (ids, oldest timestamp, serialized id of the oldest), or None.

        An EMPTY id set with a successful answer means the database has nothing
        earlier (msgFindBefore's own "end of history"). Raw items that came back
        but could not be read are None, never an empty page.

        A page is used even when it holds messages newer than the anchor: the
        server swaps an anchor it cannot find for another one without saying so
        (deviceController.ts getMessages, originalOldestId), but what it returns
        is still one contiguous run of history, so everything after its oldest
        message is covered.
        """
        answer = self._get_remote_messages(
            remote_jid, f"&direction=before&id={_url_quote(anchor_id, safe='')}")
        if answer is None:
            return None
        pairs, raw_count, oldest = answer
        if raw_count and (not pairs or oldest is None):
            return None
        return ({n["key"]["id"] for n, _raw in pairs}, oldest, _oldest_anchor(pairs))

    # How many anchored pages one poll may walk back into older history. Local
    # candidates are only the last messages_page_size records, so one page
    # normally settles all of them; the bound keeps a chat that never settles
    # from costing more than a few GETs a minute.
    _REMOTE_BEFORE_PAGES = 5

    def _deletions_before_remote_window(self, remote_jid: str, candidates: list,
                                        window_ids: set, window_oldest_ts: int,
                                        anchor_id: str) -> set:
        """Ids of local messages OLDER than the newest window that a page of
        WhatsApp Web's database proves deleted.

        The newest window says nothing about them: it is cut by count, and the
        server's own paging behind it can stop early. So ask WhatsApp Web's
        database again, anchored on the oldest message the window returned: a
        message found in any page is kept, and a page proves deleted only the
        ones inside the period it covers that are absent.

        Whatever the walk cannot account for — an empty page (the database has
        nothing earlier), a failed page, no anchor, a page that does not move
        back, the page budget spent — is KEPT. A message WhatsApp Web's
        database has lost answers exactly like a deleted one, and so does one
        its store never held (a profile paired after the message arrived, a
        store that simply holds little history). A missed phone-side deletion
        is cosmetic and fixes itself on the next F5; a false one removes the
        message from the only complete copy there is (core/remote_deletions.py).
        The accepted cost: a chat cleared on the phone while WinZapp was closed,
        with new messages since, is not mirrored.

        Nothing returned here is mirrored from a single read either — the
        caller confirms it on consecutive polls (split_deletions()), and a poll
        that finds the message again drops it from the run (observe_deletions()).
        """
        unresolved = _older_than_window(candidates, window_ids, window_oldest_ts)
        if not unresolved:
            return set()
        known = set(window_ids)
        found = set()
        anchor = anchor_id
        cause = "page budget spent" if anchor else "no anchor"
        for _page in range(self._REMOTE_BEFORE_PAGES):
            if not anchor:
                break
            page = self._fetch_remote_messages_before(remote_jid, anchor)
            if page is None:
                cause = "page fetch failed"
                break
            page_ids, page_oldest_ts, page_anchor = page
            if not page_ids:
                cause = "history ends"
                break
            known |= page_ids
            found |= _deletions_within_remote_window(unresolved, known, page_oldest_ts)
            unresolved = _older_than_window(unresolved, known, page_oldest_ts)
            if not unresolved:
                break
            if not page_anchor or page_anchor == anchor:
                cause = "page did not move back"
                break
            anchor = page_anchor
        if unresolved:
            logging.info(
                "[_deletions_before_remote_window] %s: %d older message(s) not "
                "accounted for (%s) — kept.",
                remote_jid, len(unresolved), cause,
            )
        return found

    # Consecutive polls a conversation must look fully cleared server-side
    # (see _reconcile_active_conversation_with_remote) before it's actually
    # mirrored locally — a single valid-but-empty read is not enough.
    _REMOTE_CLEAR_CONFIRM_STRIKES = 3

    _ROLLBACK_GAPS_METADATA_KEY = "remote_rollback_gaps"

    def _record_rollback_gap(self, taken_at, restored_at) -> None:
        """Remember, persistently, the period a profile restore rolled
        WhatsApp Web's database back. Kept in memory first: a restore can run
        before prepare_sync() has created self.db, and losing the record to that
        would bring back exactly the deletions it exists to prevent — it is
        written to the database the next time _rollback_gaps() finds one."""
        gaps = _add_rollback_gap(self._rollback_gaps(), taken_at, restored_at)
        self._rollback_gaps_cache = gaps
        self._rollback_gaps_dirty = True
        logging.info("[profile-recovery] rolled-back period recorded: %s", gaps[-1:])
        self._rollback_gaps()

    def _rollback_gaps(self) -> list:
        """The recorded rolled-back periods (see _record_rollback_gap())."""
        lock = self.__dict__.setdefault("_rollback_gaps_lock", threading.Lock())
        with lock:
            cached = getattr(self, "_rollback_gaps_cache", None)
            db = getattr(self, "db", None)
            if cached is None and db is not None:
                try:
                    stored = db.get_metadata_json(self._ROLLBACK_GAPS_METADATA_KEY, None)
                except Exception:
                    logging.exception("[reconcile] could not read rolled-back periods")
                    return []
                if stored is None:
                    # Never written: this is the first launch of a build that
                    # records restores. One done earlier left its hole too, and
                    # nothing recorded it (see _legacy_restore_gap()).
                    cached = self._legacy_restore_gap()
                    self._rollback_gaps_dirty = True
                else:
                    cached = _normalize_rollback_gaps(stored)
                self._rollback_gaps_cache = cached
            if getattr(self, "_rollback_gaps_dirty", False) and db is not None:
                try:
                    stored = _normalize_rollback_gaps(
                        db.get_metadata_json(self._ROLLBACK_GAPS_METADATA_KEY, []) or [])
                    cached = _normalize_rollback_gaps(stored + list(cached or []))
                    db.set_metadata_json(self._ROLLBACK_GAPS_METADATA_KEY, cached)
                    self._rollback_gaps_cache = cached
                    self._rollback_gaps_dirty = False
                except Exception:
                    logging.exception("[reconcile] could not store rolled-back periods")
            return list(cached or [])

    def _legacy_restore_gap(self) -> list:
        """The period a restore made BEFORE rolled-back periods were recorded
        left behind, or [].

        The walk back into older history ships in the same release as the
        recording, so a profile restored on an earlier build has a hole nothing
        knows about — and the first time that chat is opened it would be
        confirmed away (reproduced in review). What survives of such a restore
        is the broken profile it moved aside, `<profile>.broken`, whose mtime
        is about when it happened. When the restore started from is not known,
        so the period covers everything before it: that keeps messages, which
        is the direction to be wrong in.
        """
        from core import profile_recovery
        session_name = (getattr(self, "token", "") or "").split(":")[0]
        global_dir = getattr(self, "global_dir", None)
        if not session_name or not global_dir:
            return []
        try:
            restored_at = os.path.getmtime(
                profile_recovery.profile_dir(global_dir, session_name) + ".broken")
        except OSError:
            return []
        except Exception:
            logging.exception("[reconcile] could not look for an earlier restore")
            return []
        logging.info("[reconcile] an earlier profile restore (%s) left an unrecorded "
                     "period — not judging anything before it.",
                     time.strftime("%Y-%m-%d %H:%M", time.localtime(restored_at)))
        return _add_rollback_gap([], None, restored_at)

    def _reconcile_active_conversation_with_remote(self):
        """Detect a phone-side clear or individual message deletions in
        whichever conversation is currently open, and mirror them locally.
        Called once per periodic-poll cycle (start_periodic_contacts_sync);
        a no-op — no HTTP call at all — whenever no conversation is open.
        """
        if not hasattr(self, "_remote_clear_strikes"):
            self._remote_clear_strikes = {}
        if not hasattr(self, "_remote_deletion_strikes"):
            self._remote_deletion_strikes = {}
        cp = getattr(self, "conversations_panel", None)
        if cp is None or cp.conversation is None:
            return
        if getattr(self, "_remote_deletions_untrusted", False):
            # A profile restore rolled WhatsApp Web's store back behind our own
            # database, so "the server has not got this message" no longer means
            # the phone deleted it. See where the flag is set.
            return
        remote_jid = self._normalize_jid(cp.conversation.get("remoteJid", ""))
        if not remote_jid or not getattr(self, "messages_set_completed", False):
            return
        chat = self.chats.get(remote_jid)
        if not chat:
            return
        records = chat.get("messages", {}).get("messages", {}).get("records", [])
        # _fetch_remote_message_window() only asks WhatsApp Web for its last
        # `limit` messages (same messages_page_size setting) — comparing the
        # FULL local history against that limited remote window meant any
        # older local message, once a busy conversation pushed it past the
        # server's last-`limit` cutoff, looked "missing" and got deleted
        # locally even though it was never actually removed anywhere. This
        # was reported live as a message that had demonstrably been
        # delivered (visible to other group members) vanishing from
        # WinZapp's own local history shortly after being sent.
        #
        # The slice alone was not enough, and the rest of the bounding lives in
        # core/remote_deletions.py (which records are real, stable content)
        # and core/remote_reconcile.py (which of them an answer covers): the
        # server's last `limit` entries include edit events and placeholders
        # WinZapp never stores as messages, so they reach less far back than
        # the last `limit` local records — measured 2026-09-16, one removal per
        # round — and an answer can shrink to a couple of messages, which is
        # what deleted 199 messages at once from an open group on 2026-09-15.
        limit = int(self.settings.get("user_interface", {}).get("messages_page_size", 200))

        # Also exclude anything sent/received in roughly the last two
        # minutes: WhatsApp Web's own /get-messages can lag behind a message
        # actually reaching the server by a few seconds, so a fetch that
        # hasn't caught up yet would otherwise flag a message as "missing"
        # (and delete it) purely because of that race, not a real deletion.
        _stable_cutoff = time.time() - 120

        # Too little history for "the server has fewer messages" to mean
        # anything other than "this is just a short conversation". Checked
        # before the fetch, with no remote bound yet, so a short chat costs
        # no request at all.
        if len(comparable_local_ids(records, limit, _stable_cutoff, None,
                                    is_countable_message)) < 2:
            return
        remote = self._fetch_remote_message_window(remote_jid)
        if remote is None:
            return
        remote_ids, remote_oldest_ts, anchor_id = remote
        # Messages with no bound would be the old unbounded comparison; the
        # fetch already refuses that, and this keeps any other source honest.
        if remote_ids and not remote_oldest_ts:
            return
        # Messages already waiting for confirmation stay judged even once new
        # messages push them out of the last-`limit` slice. Otherwise a bulk
        # deletion in a busy chat would drop out of its own confirmation run
        # and never be mirrored.
        pending_run = self._remote_deletion_strikes.get(remote_jid)
        candidates = comparable_local_records(
            records, limit, _stable_cutoff, None, is_countable_message,
            extra_ids=pending_run[0] if pending_run else (),
        )
        # Nothing a profile restore rolled back is ever judged: WhatsApp Web's
        # database has a hole there that answers exactly like a deletion.
        candidates = _outside_rollback_gaps(candidates, self._rollback_gaps())
        if remote_ids:
            # The answer is cut by count, so it only proves deletions inside the
            # period it covers. Older local messages are asked about again,
            # anchored on its oldest message (_deletions_before_remote_window).
            # A big batch, and anything about older history, waits for the
            # confirmation strikes a clear does, and only what was missing on
            # every one of those polls is mirrored.
            self._remote_clear_strikes.pop(remote_jid, None)
            direct = _deletions_within_remote_window(candidates, remote_ids, remote_oldest_ts)
            inferred = self._deletions_before_remote_window(
                remote_jid, candidates, remote_ids, remote_oldest_ts, anchor_id)
            immediate, to_confirm = _split_deletions(direct, inferred)
            confirmed = _observe_deletions(self._remote_deletion_strikes, remote_jid,
                                           to_confirm, self._REMOTE_CLEAR_CONFIRM_STRIKES)
            run = self._remote_deletion_strikes.get(remote_jid)
            if run:
                logging.info(
                    "[_reconcile_active_conversation_with_remote] %s: %d message(s) "
                    "look deleted on the phone (confirmation %d/%d) — waiting.",
                    remote_jid, len(run[0]), run[1], self._REMOTE_CLEAR_CONFIRM_STRIKES,
                )
            missing_ids = immediate | confirmed
            if missing_ids:
                wx.CallAfter(self._mirror_remote_deletions, remote_jid, missing_ids)
            return
        self._remote_deletion_strikes.pop(remote_jid, None)
        if not candidates:
            self._remote_clear_strikes.pop(remote_jid, None)
            return
        # The answer is empty — every local message is gone server-side,
        # which is what a clear looks like. Require this to hold for
        # _REMOTE_CLEAR_CONFIRM_STRIKES consecutive polls before actually
        # wiping anything: a valid-but-empty answer (as opposed to None,
        # which already bails out above) is indistinguishable from a real
        # clear, but can also come from a transient server-side hiccup —
        # reported live as an actively-open group conversation briefly
        # clearing to "no messages available" mid-read, only to "recover"
        # once a new live message forced a repaint. A single bad read must
        # never be enough to nuke a conversation's entire visible history.
        #
        # Only an EMPTY answer counts. A non-empty one that shares no id with
        # local history used to count too, and that is exactly the shape of a
        # window that shrank to a few new messages.
        strikes = self._remote_clear_strikes.get(remote_jid, 0) + 1
        self._remote_clear_strikes[remote_jid] = strikes
        if strikes < self._REMOTE_CLEAR_CONFIRM_STRIKES:
            logging.info(
                "[_reconcile_active_conversation_with_remote] %s looks fully "
                "cleared server-side (strike %d/%d) — waiting for confirmation.",
                remote_jid, strikes, self._REMOTE_CLEAR_CONFIRM_STRIKES,
            )
            return
        self._remote_clear_strikes.pop(remote_jid, None)
        wx.CallAfter(self._mirror_remote_clear, remote_jid)

    def _mirror_remote_clear(self, remote_jid: str):
        """Mirror a conversation cleared on the phone. Runs on the main thread."""
        cp = getattr(self, "conversations_panel", None)
        # Re-check the conversation is still the one open and still looks
        # cleared — time passed between the background fetch and this
        # CallAfter actually running (user could have switched away, or a
        # new message could have arrived in the meantime).
        if cp is None or cp.conversation is None:
            return
        if self._normalize_jid(cp.conversation.get("remoteJid", "")) != remote_jid:
            return
        logging.info("[_mirror_remote_clear] %s appears cleared on the phone — mirroring locally.", remote_jid)
        # record_cutoff=False: this isn't a cutoff WE are choosing to
        # remember, the server is already the source of truth going forward.
        self.clear_chat_messages_local(remote_jid, record_cutoff=False)
        cp.conversation = self.chats.get(remote_jid, cp.conversation)
        cp.selected_messages.clear()
        cp.populate_messages()
        self._schedule_set_chats()

    def _mirror_remote_deletions(self, remote_jid: str, msg_ids: set):
        """Mirror one or more messages deleted on the phone from the
        currently open conversation. Runs on the main thread."""
        cp = getattr(self, "conversations_panel", None)
        if cp is None or cp.conversation is None:
            return
        if self._normalize_jid(cp.conversation.get("remoteJid", "")) != remote_jid:
            return
        logging.info("[_mirror_remote_deletions] %d message(s) in %s no longer on the phone — removing locally.",
                     len(msg_ids), remote_jid)
        cp.remove_messages_by_id(msg_ids, focus_previous=True)
