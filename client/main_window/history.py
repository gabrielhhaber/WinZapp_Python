"""HistoryMixin — part of MainWindow (see main_window/__init__.py).

Moved verbatim out of main.py. Methods run with ``self`` bound to the
MainWindow instance, so every attribute set in MainWindow.__init__ is
available here.
"""

import logging
import time
import wx
from core.api_client import api_get
from core.message_edit import is_edit_event


class HistoryMixin:
    """On-demand older history: deep backfill, fetch_older_messages and the
    exhaustion bookkeeping.
    """

    # ── Deep history backfill ────────────────────────────────────────────────
    # Pages a single chat backwards until WhatsApp Web has nothing older. The
    # ordinary backfill stops the moment a chat holds one page
    # (_note_backfill_state: `count >= history_page_target()` retires it), so
    # a 20,000-message conversation kept exactly 200 and the rest only ever
    # arrived if the user scrolled up to it by hand.
    #
    # Pages per chat per visit. Not unbounded: one enormous conversation must
    # not hold the queue while 900 others wait, and every page is a request
    # through the single Puppeteer page the whole app shares. A chat that
    # needs more comes back on the next pass, from where it stopped — the
    # anchor is whatever is oldest in the database, so progress is durable
    # across passes and across restarts.
    _DEEP_PAGES_PER_VISIT = 5
    # Chats given a deep visit per backfill pass. Small on purpose: with
    # _DEEP_PAGES_PER_VISIT pages each and a pause between pages, a pass is
    # already tens of seconds of background traffic, and the ordinary
    # short-page backfill shares the same pass and the same page.
    _DEEP_CHATS_PER_PASS = 8
    # Pause between pages of the same chat. The scroll-up path has a user
    # waiting and takes none; this has nobody waiting and is competing with
    # sends, media and live traffic for the page, so it yields.
    _DEEP_PAGE_DELAY = 1.0
    # A non-advancing page is paused rather than retried on every backfill
    # pass. It cannot be paused forever, though: on-demand history usually
    # lands in WhatsApp Web's Store without a socket event that would write it
    # into SQLite. Re-query after this cooldown so the newly arrived page can
    # move the durable database anchor.
    _DEEP_STALL_RETRY_SECONDS = 120

    def _oldest_stored_message(self, remote_jid: str) -> "dict | None":
        """The oldest message on disk for a chat — the anchor to page before.

        Read from SQLite rather than from self.chats: the in-memory record
        list is the newest page only, so anchoring on it would re-request the
        same window on every visit and never advance.
        """
        try:
            rows = self.db.get_messages_asc(remote_jid, limit=1, offset=0)
        except Exception as exc:
            logging.warning("[deep-backfill] Could not read oldest message for %s: %s",
                            remote_jid, exc)
            return None
        return rows[0] if rows else None

    @staticmethod
    def _anchor_identity(msg: "dict | None") -> tuple:
        """Which message an anchor read actually landed on.

        Identity rather than timestamp order because two messages can share a
        second, and get_messages_asc() breaks that tie on message_id. It only
        ever has to answer "is this still the same message as before": the
        oldest row can only stay put or move further back, since storing a
        page adds rows and never removes any.
        """
        key = (msg or {}).get("key") or {}
        return (int((msg or {}).get("messageTimestamp") or 0), str(key.get("id") or ""))

    def deep_backfill_chat(self, remote_jid: str) -> int:
        """Page one chat backwards. Returns how many new messages were stored.

        Stops on the first page that comes back empty — fetch_older_messages()
        marks the chat exhausted there (or asks the phone for older history
        and leaves it re-queryable, in which case the next pass picks up the
        reply). Also stops when the page brings back nothing older than we
        already had, when the per-visit page budget runs out, and when the
        connection drops.
        """
        stored = 0
        for _ in range(self._DEEP_PAGES_PER_VISIT):
            # A call can begin while this method is already walking one chat.
            # The outer backfill loop checks before starting a chat, but without
            # this inner gate an in-flight visit could still fetch several more
            # 200-message pages after audio became active.
            if self._voice_call_in_progress():
                logging.info(
                    "[deep-backfill] Pausing %s during active voice call.",
                    remote_jid,
                )
                break
            if not getattr(self, "_wa_connected", False) or getattr(self, "offline_mode", False):
                break
            if remote_jid in getattr(self, "_exhausted_chats", set()):
                break
            anchor = self._oldest_stored_message(remote_jid)
            if not anchor:
                # Nothing stored at all: this is the ordinary backfill's job,
                # not ours — it has no anchor to page before.
                break
            anchor_identity = self._anchor_identity(anchor)
            stalled = getattr(self, "_deep_stalled_anchors", None)
            if stalled is None:
                stalled = self._deep_stalled_anchors = {}
            stalled_entry = stalled.get(remote_jid)
            stalled_identity = stalled_entry[0] if stalled_entry else None
            retry_at = stalled_entry[1] if stalled_entry else 0.0
            if (stalled_identity == anchor_identity
                    and time.monotonic() < retry_at):
                # We already asked the phone about this exact non-advancing
                # anchor. Stay pending, but do not fetch the same page on every
                # pass. The cooldown is finite because history landing in the
                # browser Store does not itself move the SQLite anchor.
                break
            if stalled_identity == anchor_identity:
                stalled.pop(remote_jid, None)
            try:
                count_before = self.db.get_message_count(remote_jid)
            except Exception:
                count_before = None
            page = self.fetch_older_messages(
                remote_jid, anchor, store_only=True, allow_phone_request=False)
            if not page:
                break
            next_anchor = self._oldest_stored_message(remote_jid)
            next_identity = self._anchor_identity(next_anchor)
            try:
                count_after = self.db.get_message_count(remote_jid)
            except Exception:
                count_after = None
            added = (max(0, count_after - count_before)
                     if count_before is not None and count_after is not None
                     else 0)

            # The API fallback can return the same 200-message page when its
            # requested anchor is absent from the browser Store.  Counting
            # len(page) called that progress, renewed the global deadline and
            # created an endless history loop even though SQLite deduplicated
            # every row. Progress exists only when the on-disk oldest anchor
            # moved backwards *and* genuinely new IDs increased the row count.
            if (next_anchor is None or next_identity == anchor_identity or added <= 0):
                stalled[remote_jid] = (
                    anchor_identity,
                    time.monotonic() + self._DEEP_STALL_RETRY_SECONDS,
                )
                logging.warning(
                    "[deep-backfill] Page for %s did not advance the oldest "
                    "database anchor (%s; %d new row(s)). Pausing passive "
                    "background paging; no phone-history request was sent.",
                    remote_jid, anchor_identity, added,
                )
                break
            stalled.pop(remote_jid, None)
            stored += added
            time.sleep(self._DEEP_PAGE_DELAY)
        return stored

    def _chats_needing_deep_history(self) -> list:
        """Chats holding a full page whose older history has not been walked.

        The complement of the ordinary backfill's queue: that one handles
        chats *short* of a page, this one handles the chats it retires.
        """
        exhausted = getattr(self, "_exhausted_chats", set())
        target = self.history_page_target()
        active = []
        waiting = []
        stalled = getattr(self, "_deep_stalled_anchors", {})
        for jid, chat in list(self.chats.items()):
            if jid in exhausted or jid in self._deleted_chats:
                continue
            records = (chat.get("messages", {}).get("messages", {}).get("records")) or []
            if len(records) >= target:
                stalled_entry = stalled.get(jid)
                if stalled_entry is not None:
                    stalled_at = stalled_entry[0]
                    current = self._anchor_identity(
                        self._oldest_stored_message(jid))
                    if current == stalled_at:
                        # Keep it pending so the watcher remains alive, but put
                        # it behind chats that can still make immediate progress.
                        waiting.append(jid)
                        continue
                    stalled.pop(jid, None)
                active.append(jid)
        return active + waiting

    # How long request_older_messages() gets to be answered before an empty
    # page is durable evidence that a chat really has no more history.
    #
    # The request is fire-and-forget: the phone replies with a history-sync
    # chunk minutes later, never in the response (see that method's own
    # docstring). The backfill revisits a chat about 30 seconds after the ask,
    # so the second empty page — which is what writes the chat off — arrives an
    # order of magnitude sooner than the answer it is judging. That was
    # survivable while _exhausted_chats died with the process; now that it is
    # persisted, a write-off made inside that window is permanent, and
    # fetch_older_messages()' early return means the user scrolling up in that
    # conversation gets nothing, in every future session.
    #
    # Set well past "minutes later" on purpose. Waiting costs a handful of
    # empty round-trips per chat; being wrong costs the conversation's history
    # for good. _older_requested_chats is persisted alongside the exhausted
    # set, so in practice the bar is cleared on the next launch rather than
    # inside one session: one extra walk of an already-complete account buys
    # evidence that outlived the reply window.
    _OLDER_REQUEST_GRACE = 15 * 60

    # How many times the *backfill* may ask the phone about one chat in a
    # single run before giving up on it.
    #
    # request_older_messages() sends a peer-data-operation the phone tells its
    # owner about: iOS puts "Synchronizing WhatsApp with Google Chrome
    # (Windows)…" on the lock screen and, when the request yields nothing,
    # follows it with "Sync paused. Open WhatsApp to resume." (issue #108).
    #
    # The primaryHasMore gate stopped the requests the phone itself refuses.
    # It cannot stop these: a chat whose endOfHistoryTransferType claims more
    # history, that is asked, and that gains nothing, stays short of
    # history_page_target() forever — so the every-_OLDER_REQUEST_GRACE re-ask
    # never retires. Measured on a real account: the same four groups (holding
    # 1, 2, 26 and 82 messages against a 200-message target) asked at 10:08,
    # 10:23 and 10:38, twelve lock-screen notifications, and only the 45-minute
    # backfill budget expiring ended it. That is the "it still happens at
    # random moments" report — random because it is 15 minutes into a run, not
    # at startup.
    #
    # Two is a genuine retry, not a loop: the first ask can be lost, the
    # second answers it. A chat that gains messages has its counter cleared,
    # so this only ever bites where asking has already been shown to achieve
    # nothing. The user scrolling up (fetch_older_messages) is unaffected —
    # that request is attended, and a notification the user just caused is not
    # the problem being fixed here.
    _MAX_PHONE_HISTORY_REQUESTS = 2

    @staticmethod
    def _older_history_is_exhausted(asked_at, attempts, now_ts,
                                    grace, max_attempts) -> bool:
        """Whether the phone has answered, by silence, that it has no more.

        True once a chat has spent its whole request budget and the reply
        window has closed on the last of those asks with no older history
        having arrived — because gaining any would have cleared the budget
        (see the caller). That is the phone's answer; it just arrives as
        nothing rather than as a refusal.

        The grace is the same one fetch_older_messages() writes its own
        write-off behind, and for the same reason: the request is
        fire-and-forget and the reply is a history-sync chunk minutes later,
        so a verdict reached inside that window is a guess. Here it makes the
        verdict *durable*, which is the whole point — without it the in-memory
        budget resets on every launch and the chat is asked twice again,
        forever.
        """
        if attempts < max_attempts or asked_at is None:
            return False
        return (now_ts - asked_at) >= grace

    @staticmethod
    def _phone_history_request_due(asked_at, attempts, now_ts,
                                   grace, max_attempts) -> bool:
        """Whether the backfill may ask the phone about this chat right now."""
        if attempts >= max_attempts:
            return False
        return asked_at is None or (now_ts - asked_at) >= grace

    def _persist_exhausted_chats(self):
        """Write the exhausted-chat set to DB metadata. Best effort — losing it
        only costs one wasted round-trip per chat on the next launch."""
        try:
            if getattr(self, "db", None) is not None:
                self.db.set_metadata_json(
                    "exhausted_chats", sorted(getattr(self, "_exhausted_chats", set())))
        except Exception as exc:
            logging.warning("[history] Could not persist exhausted_chats: %s", exc)

    def _forget_history_exhaustion(self):
        """Drop everything remembered about chats having no older history.

        Called by the F5 resync. clear_local_data(wipe_metadata=False) keeps
        system_metadata on purpose — that is where the user's own
        cleared/deleted/archived/muted state lives — but these two entries are
        not user state: they are a cached conclusion about what WhatsApp Web
        was willing to hand over, and the messages they gate were just wiped
        along with everything else. Keeping them would leave every walked-out
        chat holding nothing but the page this resync re-fetches, with the deep
        backfill skipping it and fetch_older_messages() early-returning on it.

        It is also the only escape from a conclusion reached wrongly, and
        "resync everything" is where a user would look for it. Pulled out of
        _resync_all_worker() so it can be tested without a wx.App — that method
        is otherwise all UI teardown.
        """
        self._exhausted_chats = set()
        self._older_requested_chats = {}
        self._older_request_attempts = {}
        self._persist_exhausted_chats()
        self._persist_older_requested()

    def _persist_older_requested(self):
        """Write the "already asked the phone for older history" map to DB
        metadata. Best effort, same as _persist_exhausted_chats().

        Persisted for one reason only: it is what lets an empty page be told
        apart from an empty page *that has already outlived the phone's reply
        window*. Kept in memory alone — as it was — that distinction cannot
        survive the session, and the exhausted set it gates is durable.
        """
        try:
            if getattr(self, "db", None) is not None:
                self.db.set_metadata_json(
                    "older_history_requested",
                    dict(getattr(self, "_older_requested_chats", {})))
        except Exception as exc:
            logging.warning("[history] Could not persist older_history_requested: %s", exc)

    def fetch_older_messages(
        self, remote_jid, oldest_msg, store_only: bool = False,
        allow_phone_request: bool = True,
    ):
        """Fetch older messages from server starting before the oldest_msg.

        `store_only` writes the page to SQLite without growing the chat's
        in-memory record list. The scroll-up path needs that list to grow —
        the open conversation renders from it — but the deep backfill walks
        chats back to their very beginning, and appending there would hold
        every message of every chat in RAM at once. A 935-chat account with
        20k-message conversations is millions of dicts; nothing needs them
        resident, and navigate_to_conversation() reloads the page it renders
        from the database anyway (see conversations.py).
        """
        remote_jid = self._normalize_jid(remote_jid)

        # Check if history is already marked as exhausted in-memory
        if remote_jid in getattr(self, "_exhausted_chats", set()):
            if store_only or not allow_phone_request:
                logging.info(f"[fetch_older_messages] History already marked as exhausted in-memory for {remote_jid}, skipping background API query.")
                return []
            # An explicit user scroll is stronger evidence than a cached
            # conclusion from an earlier session. History may have arrived or
            # been restored on the phone since then, so challenge it once more.
            self._exhausted_chats.discard(remote_jid)
            self._older_requested_chats.pop(remote_jid, None)
            self._persist_exhausted_chats()
            self._persist_older_requested()
            logging.info(
                "[fetch_older_messages] User scroll reopened exhausted history "
                "for %s.", remote_jid,
            )

        # Resolved phone/@c.us form of the chat JID — used both as the URL
        # parameter (WPPConnect has a special evaluate-bypass in
        # /get-messages/:phone for @lid JIDs) and as the chat segment of the
        # serialized message ID below, since the message ID key in
        # WPPConnect's browser store also matches the chat JID (LID if
        # available). These used to be computed twice under two different
        # names for no reason.
        # Handle group JIDs (@g.us) vs user JIDs (@c.us / @s.whatsapp.net)
        if remote_jid.endswith("@g.us"):
            phone = remote_jid
        else:
            phone = self._resolve_jid_for_msg_key(remote_jid).replace("@s.whatsapp.net", "@c.us")
        resolved_phone = phone

        _key = oldest_msg.get("key", {})
        serialized_key = _key.get("_serialized", "") or oldest_msg.get("_serialized", "")
        if serialized_key:
            serialized_id = serialized_key
        else:
            serialized_id = self._serialize_msg_id(remote_jid, _key if _key else oldest_msg)

        # The serialized anchor identifies the Store chat that owns the
        # message. Current multi-device sessions commonly keep private chats
        # only under @lid; querying @c.us with an @lid anchor always returns
        # Chat not found before the fallback can do useful work.
        serialized_parts = serialized_id.split("_", 2)
        if len(serialized_parts) == 3:
            anchor_jid = serialized_parts[1]
            if anchor_jid.endswith(("@lid", "@c.us", "@g.us")):
                phone = anchor_jid

        limit = int(self.settings.get("user_interface", {}).get("messages_page_size", 200))
        url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/get-messages/{phone}?count={limit}&direction=before&id={serialized_id}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }

        try:
            response = api_get(url, headers=headers, timeout=30)
            
            # Alternate JID query fallback (resolves 401/TypeError or Chat not found errors)
            if response.status_code not in (200, 201):
                alternate_jid = ""
                if remote_jid.endswith("@lid"):
                    # Primary query used resolved phone JID, so fallback to original LID JID
                    alternate_jid = remote_jid
                else:
                    alt_lid = getattr(self, "_phone_to_lid", {}).get(remote_jid, "")
                    if alt_lid:
                        alternate_jid = alt_lid

                if alternate_jid and alternate_jid != phone:
                    alt_serialized_id = serialized_id
                    if "_" in serialized_id:
                        parts = serialized_id.split("_")
                        if len(parts) >= 3:
                            parts[1] = alternate_jid
                            alt_serialized_id = "_".join(parts)
                    alt_url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/get-messages/{alternate_jid}?count={limit}&direction=before&id={alt_serialized_id}"
                    logging.info(f"[fetch_older_messages] Primary query failed. Retrying with alternate JID {alternate_jid}...")
                    try:
                        alt_response = api_get(alt_url, headers=headers, timeout=30)
                        if alt_response.status_code in (200, 201):
                            response = alt_response
                            logging.info("[fetch_older_messages] Fallback alternate JID query succeeded!")
                    except Exception as alt_e:
                        logging.warning(f"[fetch_older_messages] Fallback alternate JID query failed: {alt_e}")

            if response.status_code in (200, 201):
                body = response.json()
                wpp_messages = body.get("response", []) if isinstance(body, dict) else []
                if not isinstance(wpp_messages, list):
                    wpp_messages = []
                
                # No messages left locally — but "locally" is the operative
                # word. WhatsApp only pushes a bounded window of history to a
                # linked device and keeps the rest on the phone, so running
                # out here usually means "this device has no more", not "this
                # conversation has no more". WhatsApp Web's own UI handles it
                # with the "get older messages from your phone" banner; do the
                # same thing, once per chat, before writing the chat off.
                #
                # The phone answers asynchronously (a history-sync chunk,
                # minutes later), so the request cannot help *this* call. What
                # it must not do is leave the chat in _exhausted_chats, or the
                # early-return at the top of this method would refuse to look
                # again even after the messages have landed.
                history_pending = False
                if not wpp_messages:
                    if not hasattr(self, "_exhausted_chats"):
                        self._exhausted_chats = set()
                    if not hasattr(self, "_older_requested_chats"):
                        self._older_requested_chats = {}
                    asked_at = self._older_requested_chats.get(remote_jid)
                    asked_now = asked_at is None
                    requested = False
                    if not allow_phone_request:
                        history_pending = True
                    if asked_now and not allow_phone_request:
                        history_pending = True
                        logging.info(
                            "[fetch_older_messages] Background paging reached "
                            "the browser-store edge for %s; deferring the phone "
                            "request to interactive scroll.", remote_jid)
                    elif asked_now:
                        asked_at = time.time()
                        self._older_requested_chats[remote_jid] = asked_at
                        self._persist_older_requested()
                        request_result = self.request_older_messages(remote_jid)
                        requested = request_result is True
                        if request_result is None:
                            # The API deliberately refuses ON_DEMAND while the
                            # RECENT queue is incomplete. Nothing was sent, so
                            # do not start the grace clock and never interpret
                            # this temporary refusal as end-of-history.
                            self._older_requested_chats.pop(remote_jid, None)
                            self._persist_older_requested()
                            history_pending = True
                    waited = max(0.0, time.time() - (asked_at or time.time()))
                    if requested:
                        history_pending = True
                        logging.info(
                            "[fetch_older_messages] No local history left for %s — "
                            "asked the phone for older messages; leaving the chat "
                            "re-queryable so the reply can be picked up.", remote_jid,
                        )
                    elif history_pending or waited < self._OLDER_REQUEST_GRACE:
                        history_pending = True
                        logging.info(
                            "[fetch_older_messages] History for %s is still "
                            "pending (waited %.0fs); keeping it re-queryable.",
                            remote_jid, waited,
                        )
                    elif allow_phone_request:
                        # In-memory always, so the chat leaves the deep-backfill
                        # queue at the same rate it always did. Persisted only
                        # once the phone has genuinely had its chance: the
                        # request above is answered by a history-sync chunk
                        # minutes later, and _exhausted_chats is now durable, so
                        # a write-off made 30 seconds after the ask (the backfill
                        # pass cadence) used to become permanent — the chat was
                        # early-returned at the top of this method for ever,
                        # which also silently killed the user scrolling up in it.
                        # `waited` clears the bar on the next session rather than
                        # inside this one, since _older_requested_chats is
                        # persisted too: one extra walk of an already-complete
                        # account, in exchange for never writing a chat off on
                        # evidence younger than the reply it is waiting for.
                        self._exhausted_chats.add(remote_jid)
                        durable = waited >= self._OLDER_REQUEST_GRACE
                        if durable:
                            self._persist_exhausted_chats()
                        logging.info(
                            "[fetch_older_messages] Marked history as exhausted for %s "
                            "(%s). The phone was asked %.0fs ago; older-message request "
                            "%s.",
                            remote_jid,
                            "persisted" if durable else "this session only",
                            waited,
                            "just attempted and failed" if asked_now else "sent earlier",
                        )

                fetched_messages = []
                edit_event_ids = set()
                for wm in wpp_messages:
                    if isinstance(wm, dict) and self.ws:
                        try:
                            normalized = self.ws._normalize_wpp_message(wm)
                            self._extract_lid_mapping(normalized)
                            # Same filter as _normalize_fetched_messages() —
                            # scrolling up must not store edit events either.
                            if is_edit_event(normalized):
                                event_id = (normalized.get("key") or {}).get("id")
                                if event_id:
                                    edit_event_ids.add(event_id)
                                continue
                            fetched_messages.append(normalized)
                        except Exception:
                            pass
                if edit_event_ids:
                    self._remember_dropped_edit_events(edit_event_ids)
                    wx.CallAfter(self._purge_materialized_edit_rows, remote_jid, edit_event_ids)
                
                if fetched_messages:
                    if store_only:
                        # Straight to disk, nothing kept resident. Deliberately
                        # not routed through the branch below even for the
                        # dedup: that one dedups against the in-memory page,
                        # which is the newest 200 and cannot overlap a window
                        # anchored strictly before the oldest we hold. The
                        # message table's own (message_id, remote_jid) primary
                        # key is what makes a repeat harmless anyway.
                        try:
                            self.db.insert_messages_batch(remote_jid, fetched_messages)
                        except Exception as e:
                            logging.warning(
                                "[fetch_older_messages] store-only save failed for %s: %s",
                                remote_jid, e)
                            return []
                        return fetched_messages
                    # Update local database/memory
                    chat = self.chats.get(remote_jid, {})
                    if chat:
                        local_records = chat.get("messages", {}).get("messages", {}).get("records", [])
                        existing_ids = {r.get("key", {}).get("id") for r in local_records}
                        new_records = [m for m in fetched_messages if m.get("key", {}).get("id") not in existing_ids]
                        if new_records:
                            all_records = new_records + local_records
                            chat.setdefault("messages", {}).setdefault("messages", {})["records"] = all_records
                            chat["messages"]["messages"]["total"] = len(all_records)
                            try:
                                self.db.upsert_chat(remote_jid, chat)
                                self.db.insert_messages_batch(remote_jid, new_records)
                            except Exception as e:
                                logging.error(f"[fetch_older_messages] Incremental save failed: {e}")
                                self.save_data(self.chats, self.contacts)
                    return fetched_messages
                else:
                    return None if history_pending else []
            else:
                err_msg = response.text[:300]
                try:
                    body = response.json()
                    if isinstance(body, dict) and "error" in body:
                        err_obj = body["error"]
                        if isinstance(err_obj, dict) and "message" in err_obj:
                            err_msg = f"{err_obj.get('message')} - {err_obj.get('stack', '')[:200]}"
                except Exception:
                    pass
                logging.warning(
                    f"[fetch_older_messages] API returned status {response.status_code} for {remote_jid}: {err_msg}"
                )
                return None
        except Exception as e:
            logging.error(f"[fetch_older_messages] failed to get older messages for {remote_jid}: {e}")
            return None

    def wait_for_older_messages(
        self, remote_jid, oldest_msg, timeout: float = 300.0,
        poll_interval: float = 2.0, retry_request_every: float = 10.0,
        should_continue=None,
    ):
        """Poll one interactive phone-history request until its page arrives."""
        jid = self._normalize_jid(remote_jid)
        deadline = time.monotonic() + max(1.0, float(timeout))
        delay = max(0.5, float(poll_interval))
        retry_delay = max(delay, float(retry_request_every))
        next_interactive_retry = time.monotonic() + retry_delay
        logging.info(
            "[history-scroll] Waiting up to %.0fs for phone history for %s.",
            timeout, jid)
        while time.monotonic() < deadline:
            if callable(should_continue):
                try:
                    if not should_continue():
                        logging.info(
                            "[history-scroll] Cancelled wait for %s because the "
                            "conversation is no longer active.", jid)
                        return None
                except Exception:
                    return None
            if not getattr(self, "_wa_connected", False) or getattr(
                self, "offline_mode", False
            ):
                return None
            time.sleep(delay)
            page = self.fetch_older_messages(
                jid, oldest_msg, store_only=False, allow_phone_request=False)
            if page is not None:
                logging.info(
                    "[history-scroll] Phone history became available for %s "
                    "(%d message(s)).", jid, len(page))
                return page
            now = time.monotonic()
            if now >= next_interactive_retry:
                next_interactive_retry = now + retry_delay
                page = self.fetch_older_messages(
                    jid, oldest_msg, store_only=False, allow_phone_request=True)
                if page is not None:
                    logging.info(
                        "[history-scroll] Interactive retry produced history "
                        "for %s (%d message(s)).", jid, len(page))
                    return page
        logging.warning(
            "[history-scroll] Timed out waiting for phone history for %s; "
            "the chat remains re-queryable.", jid)
        return None
