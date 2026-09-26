"""AccountLinkMixin — part of MainWindow (see main_window/__init__.py).

Moved verbatim out of main.py. Methods run with ``self`` bound to the
MainWindow instance, so every attribute set in MainWindow.__init__ is
available here.
"""

import logging
import threading
import time
import wx
from core.api_client import (
    api_get,
    redact_credentials,
)
from main_window.identity_rules import (
    linked_number_differs,
    linked_phone_digits,
)


class AccountLinkMixin:
    """Periodic connection health check and the 'another number was linked'
    detection and local-data wipe.
    """

    # How long a single health-check sleep may overrun before we treat the gap
    # as "the machine was suspended and just resumed". The loop sleeps 30s, so
    # anything past ~90s of real elapsed time can only mean the thread was
    # frozen across a system sleep/hibernate — clocks don't otherwise skip
    # minutes. See _HEALTH_CHECK_INTERVAL below.
    _HEALTH_CHECK_INTERVAL = 30
    _WAKE_DETECT_GAP = 90

    def start_connection_health_checker(self):
        """Periodically verify session health and auto-restart Puppeteer if closed.

        Doubles as the reliable wake detector. wx's EVT_POWER_RESUME is bound in
        __init__, but on Windows it is NOT delivered to a frame that lives in the
        system tray (hidden top-level window) — observed live as zero "[power]"
        log lines across real hibernations, so the post-wake zombie-session
        repair never ran and the app stayed offline until manually restarted.
        This loop measures how long each sleep *actually* took: a 30s sleep that
        really took minutes means the process was frozen across a suspend, so we
        fire the exact same recovery path EVT_POWER_RESUME was supposed to.
        """
        def _loop():
            # Wait a bit after startup before starting checks
            time.sleep(self._HEALTH_CHECK_INTERVAL)
            while True:
                slept_at = time.time()
                try:
                    # Only a *user-requested* offline pauses the checker.  When
                    # offline mode was entered automatically (connection lost)
                    # this loop is precisely what notices the connection coming
                    # back, so skipping it there would make the state permanent.
                    # A WPPConnect reinstall in progress also pauses it — the
                    # server is deliberately down for a few seconds there, and
                    # that is not a real connection loss (see _wpp_updating).
                    if not getattr(self, "_user_offline", False) and not getattr(self, "_wpp_updating", False):
                        self.check_wa_connection_http()
                        # Safety net for a sync that failed or never started
                        # while the connection was down: retry it as soon as
                        # WhatsApp is reachable again, for as long as it takes.
                        self.trigger_sync_if_needed()
                except Exception as e:
                    logging.warning(f"[health_checker] Error checking connection in background: {e}")
                time.sleep(self._HEALTH_CHECK_INTERVAL)
                # Clock-gap wake detection: if the sleep above overran massively,
                # the machine was suspended and just came back. Trigger recovery
                # here because EVT_POWER_RESUME is unreliable for a tray app.
                import connection_state as cs
                elapsed = time.time() - slept_at
                if cs.is_wake_from_suspend(elapsed, self._HEALTH_CHECK_INTERVAL,
                                           self._WAKE_DETECT_GAP) \
                        and not getattr(self, "_user_offline", False):
                    logging.info(
                        "[wake-detect] Health-check sleep overran (%.0fs elapsed vs %ss expected) "
                        "— treating as resume from suspend, forcing recovery.",
                        elapsed, self._HEALTH_CHECK_INTERVAL,
                    )
                    try:
                        self._reset_connection_state_for_resume()
                        self._recover_from_suspend()
                    except Exception:
                        logging.exception("[wake-detect] recovery after detected wake failed")

        threading.Thread(target=_loop, daemon=True).start()

    # A single failed network probe is not proof of an outage (a slow proxy, a
    # captive portal check, a momentary DNS hiccup); two in a row is.  Keeping
    # this at 2 means an outage is detected within one health-check cycle
    # (~30-60 s) while a blip never drops the app into offline mode.
    _OFFLINE_PROBE_STRIKES = 2

    # Same idea, but for the local WPPConnect API request itself (the
    # `except` branch of check_wa_connection_http() below) rather than the
    # WhatsApp-reachability probe. The local Node/Puppeteer process can be
    # briefly unresponsive to its own HTTP server under load (GC pause, a
    # heavy history-sync page load, disk I/O) without WhatsApp itself having
    # dropped at all — that used to flip straight to offline on the very
    # first missed 10s-timeout request, then back online on the next
    # successful poll 30s later, over and over. Reported live as the app
    # repeatedly flickering between online/offline for no apparent reason.
    # Requiring consecutive failures before believing it is a real outage
    # fixes the flapping and, as a side effect, gives a slower/busier machine
    # more time before offline mode kicks in at all.
    _HTTP_PROBE_STRIKES = 2

    # Consecutive notLogged/QRCODE readings required before believing the device
    # was really unlinked.  The health checker polls every ~30 s.
    _LOGOUT_CONFIRM_STRIKES = 4

    # How many consecutive unlinked readings to tolerate while a paired session
    # is still trying to resume (never connected yet this run) before offering
    # the pairing dialog WITHOUT wiping data. The health checker polls ~30 s, so
    # 20 ≈ 10 minutes — long enough for a slow WhatsApp Web resume, short enough
    # that a genuinely dead session eventually surfaces a QR the user can scan.
    _RESUME_FAIL_STRIKES = 20

    # Consecutive times a confirmed-LOGOUT decision may be vetoed by
    # _still_linked_on_server() before the veto itself stops being believed.
    # The veto is a safety net against wiping a live session, but on its own it
    # has no way out: every veto resets the tally, so a session that keeps
    # answering host-device from a stale cache after a real unlink would park
    # the app offline forever — never sending, never reaching the pairing
    # dialog, with nothing in the log to distinguish it from a quiet outage.
    # After this many in a row we stop arguing and fall back to
    # _on_disconnect(wipe=False): the pairing dialog, history preserved, which
    # is the safe outcome whichever side the probe got wrong. 5 vetoes is ~5
    # confirmed logouts apart, i.e. many minutes of a session insisting it is
    # both linked and unusable.
    _STILL_LINKED_VETO_LIMIT = 5

    def _still_linked_on_server(self) -> str:
        """Ask WPPConnect whether this session still holds a linked phone.

        Thin wrapper: the probe itself also reports WHICH phone answered, and
        only _wipe_local_data_if_another_number_linked() cares about that.
        Every caller of this one is deciding a logout, where the identity of
        the phone is irrelevant and the three-way outcome is the whole
        answer.
        """
        return self._host_device_link_probe()[0]

    def _host_device_link_probe(self) -> tuple:
        """(outcome, phone) for this session's linked phone, from host-device.

        Returns one of connection_state's LINK_PROBE_* outcomes: LINKED
        (host-device answered with our own phone number), UNLINKED (it
        answered, and holds none — including the missing-key shape a real
        unlink produces, see below) or UNKNOWN (the probe itself failed — a
        non-2xx answer, a network/transport error, or a body we could not
        read).

        The three-way answer is the whole point. This runs as the last gate
        before a destructive wipe, and the signal that gets it there is often
        a local HTTP 401/403 — which this very probe is subject to as well,
        since it goes out through the same local auth middleware that just
        rejected the status poll. A boolean collapsed that self-inflicted
        401 into "could not prove it is linked", i.e. into permission to
        wipe: a Node restart under a rotated token produced 401s, four
        strikes, a LOGOUT, then a 401 here too, and the database went anyway.
        UNKNOWN keeps ambiguous evidence from ever being destructive — see
        _act_on_unlink_decision(), which routes it to the pairing dialog with
        the data intact.
        """
        import connection_state as cs
        try:
            resp = api_get(
                f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/host-device",
                headers={"Authorization": f"Bearer {self.token}",
                         "Content-Type": "application/json"},
                timeout=10,
            )
        except Exception as exc:
            # redact_credentials(): requests copies the failed URL verbatim
            # into its own message, and this one carries the token in its
            # path. This probe only started being called at all in this
            # change, so the leak it would publish into the log.log users
            # paste into bug reports is new even though the line is not.
            logging.info(
                "[_host_device_link_probe] host-device probe failed: %s: %s",
                type(exc).__name__, redact_credentials(str(exc)),
            )
            return cs.LINK_PROBE_UNKNOWN, ""
        if resp.status_code not in (200, 201):
            # Notably 401/403: our own middleware refused the probe, so it
            # never reached WhatsApp and says nothing about the link at all.
            logging.info(
                "[_host_device_link_probe] host-device answered HTTP %s — the "
                "probe proves nothing either way.", resp.status_code,
            )
            return cs.LINK_PROBE_UNKNOWN, ""
        try:
            body = resp.json().get("response")
        except Exception as exc:
            logging.info(
                "[_host_device_link_probe] host-device body unreadable: %s", exc)
            return cs.LINK_PROBE_UNKNOWN, ""
        if not isinstance(body, dict):
            # No "response" object at all, or one that is not a mapping: a
            # shape we do not understand, which may not be read as any verdict
            # about the link.
            logging.info(
                "[_host_device_link_probe] host-device answered 2xx with no "
                "readable response object — the probe proves nothing either way.")
            return cs.LINK_PROBE_UNKNOWN, ""
        # Deliberately keyed on the VALUE being falsy, not on the key being
        # absent, because absent is exactly the shape a real unlink produces:
        # deviceController's host-device sends `{...hostDevice, phoneNumber}`
        # with phoneNumber = getWid(), and getWid() is undefined once the
        # session holds no linked user — JSON.stringify then drops the key
        # entirely. Treating a missing key as "unreadable" would make
        # LINK_PROBE_UNLINKED unreachable against the real payload, leaving
        # the wipe branch dead code: an unlinked-then-repaired phone would
        # sync a different account's chats straight into this one's database,
        # which is the merge clear_local_data() exists to prevent. The other
        # three UNKNOWN returns above still keep ambiguous evidence (a 401 on
        # the probe itself, a transport failure, an unreadable body) away from
        # the destructive path — that is where the three-way answer earns its
        # keep, not here.
        phone = body.get("phoneNumber", "")
        if isinstance(phone, dict):
            phone = phone.get("_serialized", "")
        if not isinstance(phone, str):
            phone = str(phone) if phone else ""
        return ((cs.LINK_PROBE_LINKED, phone) if phone
                else (cs.LINK_PROBE_UNLINKED, ""))

    def _wipe_local_data_if_another_number_linked(self) -> None:
        """Wipe this account's local data when a DIFFERENT phone just linked.

        The QR flow cannot ask which phone is about to scan the code, so
        Connect.start_qrcode_connection() decides its wipe from
        `preserve_local_data` — "this installation had a working history a
        moment ago", not "this is the same number". Scan that code with
        another phone and the history of the first account survives while the
        second one's sync merges on top of it, which is exactly the merge
        clear_local_data() exists to prevent.

        So the comparison happens here instead, once pairing has closed and
        WPPConnect can be asked which phone it actually holds. Every step
        refuses to act on anything short of proof, because a false positive
        deletes a history a blind user pays for with the whole pairing flow
        again, while a false negative is a wipe they can still ask for:

          * no open database — the startup dialog runs before prepare_sync(),
            where clear_local_data() would delete media/ and voice_messages/
            and leave every message in messages.db behind. The call right
            after prepare_sync() owns that case;
          * no session token — nothing to ask, and the normal state of a
            freshly created multi-account entry (`pending`, empty
            privateinfo);
          * anything but LINK_PROBE_LINKED — a failed, refused or unreadable
            probe proves nothing (see _host_device_link_probe);
          * an answer we cannot read as a phone number — an unbridged @lid, a
            group, a truncated field;
          * linked_number_differs() False — the same number, or the Brazilian
            8/9-digit variant of it.

        **What it compares is a phone WhatsApp itself confirmed as linked, on
        both sides.** The recorded value lives under its own key,
        WA_phone_number_linked, written by this method and by
        record_linked_phone_if_unknown(), which only ever fills it in when it
        is absent and reads the same host-device answer this does.
        It is deliberately NOT privateinfo["WA_phone_number"], which holds
        what the user typed into the pairing dialog: connect.py writes that
        the instant a phone code arrives — before the pairing concludes — and
        nothing restores the previous value when the attempt is abandoned. A
        user who mistyped their number, got a code for it, went back to QR and
        scanned with their own correct phone would have had every message,
        every downloaded file and every voice note of their own account
        deleted, by a check that exists to protect them.

        An install that has never been through this check carries no such key,
        and that absence means "learn this number now", never "it diverged" —
        the same non-destructive branch a QR-only install has always taken.
        Recording a number it deleted nothing over is harmless by itself and
        arms the comparison from the second pairing onwards. After a wipe it
        is replaced for the mirror-image reason: left at the old number, the
        next pairing of THIS one would look like another divergence and wipe a
        second time. The same reason keeps it on the new number when
        _restart_sync_after_another_number_wipe() gives up its second pass —
        at a spent wait or a shutdown: the next launch refills the database
        with the new account, and a key moved back would have the next pairing
        delete that.

        The write comes last on purpose, and only happens when the wipe really
        emptied the database. If the process is killed mid-wipe (this runs on a
        daemon thread, and the shutdown does not wait for it), the key still
        names the previous number in every reachable state, so the next pass or
        the next pairing always reads the same divergence and finishes the job.
        clear_local_data() commits the database emptying first, THEN sweeps
        media/ and voice_messages/, and only after both of those does it drop
        the recorded number — deliberately in that order, because a media sweep
        that ran after the drop used to leave orphaned files with nothing left
        able to see them once the key was already gone (issue #200: the
        divergence check that would otherwise clean them up never fires again
        once the key no longer names the account they belong to). With the
        sweep moved ahead of the drop, a kill between the two leaves the key
        still armed and the media already gone — self-healing, not orphaning:
        the next pass re-detects the divergence, finds the database and media
        already empty (both idempotent no-ops), and drops the key. A kill
        before the sweep leaves the key armed and the media still on disk,
        which the next pass sweeps and then drops as normal. Either way the
        key is never left naming an account whose database or media a kill
        left non-empty, which is what makes a partial wipe self-healing and
        lets this thread's shutdown go unwaited.

        Mid-session there is almost always a sync already in flight when this
        starts, claim or no claim — it was started synchronously by the event
        that concluded the pairing, before the dialog even closed. The wipe
        still runs immediately; everything about outliving that round is in
        _restart_sync_after_another_number_wipe().
        """
        import connection_state as cs

        privateinfo = self.settings.get("privateinfo")
        if not isinstance(privateinfo, dict):
            return
        if getattr(self, "db", None) is None:
            logging.info(
                "[another_number_check] Database not open yet — deferring to "
                "the check that runs after prepare_sync().")
            return
        if not getattr(self, "token", ""):
            logging.info(
                "[another_number_check] No session token — the probe could "
                "not prove anything, nothing deleted.")
            return

        # `live` is the mid-session case: the repair dialog reopened over a
        # running app, so there is a chat list on screen, an audio player that
        # may hold a .msv open, and syncs firing on their own. At startup this
        # runs inside __init__ before init_UI(), where none of that exists yet
        # and the first sync is still ahead of us.
        live = self._ui_ready_event.is_set()
        handed_off = False
        if live:
            # Claim the sync slot for the whole probe-and-wipe window, exactly
            # as _resync_all_worker() claims it before its own
            # clear_local_data() and for the same reason. The health checker
            # and websocket_client's _recheck_connection_after_connect() both
            # call trigger_sync_if_needed() on their own — and the reconnect
            # that follows a repaired session fires the second one — so a sync
            # can start inside the (up to 10 s) probe below, capture self.chats
            # while it still holds the previous account's chats, and write them
            # straight back into the new account's database after the wipe:
            # the merge this method exists to prevent, with the user believing
            # the protection ran.
            #
            # The flag is a claim, not a lock: a sync that started before this
            # already holds it, which is why the release in the finally below
            # has to ask whether it is still ours to release.
            self._initial_sync_running = True
        try:
            outcome, linked = self._host_device_link_probe()
            if outcome != cs.LINK_PROBE_LINKED:
                logging.info(
                    "[another_number_check] host-device returned %s — no verdict "
                    "on which number is linked, nothing deleted.", outcome)
                return
            new_digits = linked_phone_digits(self, linked)
            if not new_digits:
                logging.info(
                    "[another_number_check] host-device answered with a value this "
                    "cannot read as a phone number — nothing deleted.")
                return
            stored = privateinfo.get("WA_phone_number_linked") or ""
            if not stored:
                # First time this account is told, by WhatsApp, which phone it
                # holds. Nothing to compare against, so nothing is deleted —
                # this only arms the comparison for the next pairing.
                logging.info(
                    "[another_number_check] No confirmed phone number recorded "
                    "for this account — recording the linked one (...%s), "
                    "nothing deleted.", new_digits[-4:])
                privateinfo["WA_phone_number_linked"] = new_digits
                self.save_settings()
                return
            try:
                differs = linked_number_differs(stored, new_digits)
            except Exception:
                # A bug in the comparison must not be able to delete anything.
                logging.exception(
                    "[another_number_check] Could not compare the linked number — "
                    "nothing deleted.")
                return
            if not differs:
                logging.info(
                    "[another_number_check] The linked phone is this account's own "
                    "number — keeping the local history.")
                return

            logging.warning(
                "[another_number_check] A different phone is linked to this "
                "account (recorded ...%s, linked ...%s) — wiping the local data "
                "the previous number left behind.", stored[-4:], new_digits[-4:])
            if live:
                # Said out loud before anything disappears, so the reason
                # arrives ahead of the effect. Nothing else would say it: the
                # list simply empties, which a screen-reader user does not see
                # at all, and the sync that follows announces a
                # synchronization rather than a deletion. Same reasoning as
                # _halt_unattended_qr_session() — speech only, no message box,
                # because this lands on a thread with the pairing flow just
                # closed and a modal here would take the focus off whatever
                # the user moved to next.
                try:
                    self.output(
                        self.i18n.t("another_number_linked_data_cleared"),
                        interrupt=False)
                except Exception:
                    logging.exception(
                        "[another_number_check] announcement failed")

            # Captured BEFORE the wipe: a sync already in flight keeps running
            # right through it (see _restart_sync_after_another_number_wipe()).
            in_flight = getattr(self, "sync_thread", None) if live else None
            self._apply_another_number_wipe(new_digits, teardown_ui=live,
                                            previous_digits=stored)

            if live:
                # The database of the account that is actually linked is now
                # empty, and the sync that would have filled it either never
                # ran or ran against the previous account. Ask for a fresh full
                # one. _try_start_sync_thread() rather than
                # trigger_sync_if_needed(), because the claim above is still
                # held and that method's own guard would refuse — start_sync()
                # takes the claim over from here, exactly as it does for
                # _resync_all_worker().
                self._sync_completed = False
                self._force_full_sync = True
                # Latched on disk too, exactly as _resync_all_worker() does
                # for F5 and for the same reason: closing the app during this
                # corrective round would otherwise have the next launch read
                # force_full_pending=False out of the table the wipe just
                # emptied, and run an incremental round over an empty
                # database.
                self._persist_full_sync_pending(self._ANOTHER_NUMBER_WIPE_REASON)
                handed_off = True
                try:
                    if in_flight is not None and in_flight.is_alive():
                        # …except that _try_start_sync_thread() answers "there
                        # is already one running" and starts nothing at all
                        # while that thread lives, and the round it refers to
                        # is the contaminated one. Hand the restart to a
                        # thread that outlives it.
                        threading.Thread(
                            target=self._restart_sync_after_another_number_wipe,
                            args=(in_flight, new_digits, stored),
                            name="another-number-resync", daemon=True,
                        ).start()
                    else:
                        self._try_start_sync_thread()
                except Exception:
                    # The claim was handed over one statement too early to be
                    # safe against the race, so take it back here: leaked, it
                    # blocks every sync for the rest of the session.
                    handed_off = False
                    logging.exception(
                        "[another_number_check] Could not start the sync that "
                        "refills the emptied database.")
        finally:
            if live and not handed_off:
                # Only if nobody else holds it. By the time this runs, the
                # pairing that opened the dialog has usually already started
                # the post-pairing sync of its own (on_wpp_session_logged →
                # on_messages_set → _try_start_sync_thread, which never looks
                # at this flag), and that sync set the very same flag on its
                # way in. Clearing it here left the initial sync running with
                # its claim gone: the 60 s incremental poll and F5 both consult
                # nothing else, so both would start a second round writing
                # self.chats underneath the first.
                existing = getattr(self, "sync_thread", None)
                if existing is None or not existing.is_alive():
                    self._initial_sync_running = False

    def _apply_another_number_wipe(self, new_digits: str,
                                   teardown_ui: bool = True,
                                   previous_digits: str = "") -> None:
        """Tear the visible half down (with the UI up), wipe, record the number.

        Split out of _wipe_local_data_if_another_number_linked() only because
        _restart_sync_after_another_number_wipe() has to run this exact
        sequence a second time — see its docstring for why once is not enough.

        ``teardown_ui`` is the mid-session case, where there are panels on
        screen to empty first — the same teardown F5 needs, which is why both
        go through _teardown_conversation_ui(). At startup this runs inside
        __init__ before init_UI() and there is nothing yet to tear down, so
        that caller passes False; the resync thread below always runs with the
        UI up, hence the default. A future startup caller that forgets to pass
        False sits out _teardown_conversation_ui()'s full 5 s ui_ready.wait()
        — before MainLoop() nothing dispatches the wx.CallAfter — and then
        _prepare_ui() raises on self.conversations_panel, which does not exist
        yet.

        ``previous_digits`` is the number the key named before this pass, and
        the key is put back on it BEFORE anything is deleted, so that through
        the whole pass it names whichever account the messages on disk belong
        to. The direct caller can hand it over for free — it read exactly that
        value to decide there was a divergence at all — and
        _restart_sync_after_another_number_wipe() carries it down to the second
        pass, which is the one that needs it: when the first pass got as far as
        recording the new number, a second pass that empties nothing would
        otherwise leave the key naming the new account over rows the
        contaminated round committed on its way out. Nothing conditions that
        second pass on the first having succeeded, and the `!=` guard covers
        that case for free — a first pass that recorded nothing left the key on
        the previous number, so the write is skipped.

        The number is recorded last, after the wipe rather than before it,
        and only when the wipe really emptied the database:
        clear_local_data(wipe_metadata=True) drops WA_phone_number_linked
        along with the data it describes whenever it emptied it, and what is
        written here describes the empty database the next sync is about to
        fill. When it emptied nothing, the key simply stays on the previous
        number — see the branch below.
        """
        privateinfo = self.settings.setdefault("privateinfo", {})
        if (previous_digits
                and privateinfo.get("WA_phone_number_linked") != previous_digits):
            # Re-armed BEFORE the deletion starts rather than repaired after
            # it, which is the same argument clear_local_data() makes one level
            # up about dropping this key only once the database is really
            # empty. The second pass enters with the key naming the NEW account
            # (the first pass recorded it) while the previous account's
            # messages may still be in messages.db, and everything that can
            # fail from here on is slow: save_full_state() raises, and
            # clear_local_data() then sweeps media/ and voice_messages/ entry
            # by entry — seconds on a large install — before it finally answers
            # False. This runs on a daemon thread the shutdown does not wait
            # for, and the user closing WinZapp mid-switch is exactly what
            # produces that failure, so repairing afterwards left the whole of
            # that window open: a process killed inside it kept a settings.json
            # naming B over A's rows, every later pass compared the key against
            # the linked phone, found them equal, and the merge happened with
            # nothing left pointing at it. Written first, the key describes
            # what is on disk for the whole pass instead of only at the end of
            # it, and the one extra settings write it costs lands on a path
            # that is about to delete an account's history anyway.
            #
            # The first pass and every startup call are untouched: there the
            # key already names previous_digits, so the guard skips the write.
            #
            # A write that fails here fails silently — save_settings() catches
            # everything, plays the error sound and marshals a MessageBox
            # rather than propagating — so the worst it can leave behind is the
            # state the old order left open for the whole window (the previous
            # number in memory, the new one in settings.json), and never an
            # exception, which is what would cost the caller the corrective
            # full sync it starts after this.
            privateinfo["WA_phone_number_linked"] = previous_digits
            self.save_settings()

        # After the re-arming above, never before it: _teardown_conversation_ui()
        # ends in a 5 s ui_ready.wait(), and the case that spends all five is
        # the very one this key protects against — the user closing WinZapp, so
        # the MainLoop dies, the wx.CallAfter is never dispatched, and the
        # shutdown does not wait for this daemon thread. Torn down first, that
        # was five seconds of the second pass with the key naming the new
        # account over rows the contaminated round had committed, and a process
        # killed inside it leaves no divergence for any later pass to find.
        if teardown_ui:
            self._teardown_conversation_ui()

        if not self.clear_local_data():
            # The wipe emptied no database, and clear_local_data() swallows the
            # reason — no database open, or a save_full_state() that raised
            # DatabaseBridgeTimeout/Closed, which is what the user closing
            # WinZapp during a mid-session wipe produces, since the shutdown
            # does not wait for this daemon thread. The previous number's
            # messages are therefore still in messages.db, so the key has to go
            # on naming it: recording the new one here would tell every later
            # pass there is no divergence left, and the new account's first sync
            # would write over rows the old account still owns — the merge this
            # check exists to prevent, reached after the user has already been
            # told the previous number's conversations were deleted. Left armed,
            # the next pass or the next pairing re-detects it and finishes the
            # job.
            #
            # There is nothing to repair here, because the block above already
            # made sure of it: the key names previous_digits either because
            # nobody had moved it (the first pass, and every startup call,
            # where clear_local_data() skipped its own drop for the same
            # reason) or because it was put back there before the deletion
            # started. What the key has to name is whichever account the
            # messages on disk belong to, and in both passes that is the
            # previous one.
            logging.error(
                "[another_number_check] The wipe emptied no database — the "
                "recorded number goes on naming the account whose messages are "
                "still on disk, so the divergence is found again.")
            return
        privateinfo["WA_phone_number_linked"] = new_digits
        self.save_settings()

    # The reason string _persist_full_sync_pending() records for this wipe.
    # Named because both halves of it — the check and the resync thread that
    # repeats the wipe — write it, and a log field that only matches in one of
    # them is worse than useless when reading a field report.
    _ANOTHER_NUMBER_WIPE_REASON = "another-number-wipe"

    # How many times _restart_sync_after_another_number_wipe() will wait for
    # "one more" sync thread before giving up and restarting anyway. Bounded
    # because an account whose syncs keep restarting must not park this thread
    # forever — the wipe has already happened, so what is at stake here is
    # only whether the refill starts now or on the next reconnect.
    _ANOTHER_NUMBER_SYNC_JOIN_ROUNDS = 3

    # How long _restart_sync_after_another_number_wipe() goes on waiting — for
    # a round it has superseded (issue #199), or for a shutdown to finish or be
    # cancelled — before it gives up. Sized on the longest stretch with no
    # supersession check that a slow but still answering server produces: one
    # get_remote_chats() whose five attempts all time out (30+45+60+90+120 s
    # plus four 5 s sleeps, ~365 s), with the phase-1 get-messages workers
    # already in flight (30 s a request) inside the margin. The RECENT wait
    # and the media phase stop on the run id themselves since #198, so neither
    # sets it any more.
    #
    # It is not a bound on every case, and does not pretend to be: after the
    # message phase a round runs group-info lookups (10 s each, six at a time,
    # as many as there are unnamed groups), get_remote_contacts() (up to five
    # 90 s attempts) and one more list-chats before its next check, so a round
    # whose every request times out there outlasts this and gets the fallback.
    # The slice is how often the wait looks again; both are class attributes
    # only so the tests can shrink them.
    _ANOTHER_NUMBER_SYNC_EXIT_WAIT = 480
    _ANOTHER_NUMBER_SYNC_EXIT_POLL = 1.0

    def _restart_sync_after_another_number_wipe(self, in_flight, new_digits: str,
                                                previous_digits: str) -> None:
        """Wait out the sync that was already running, wipe again, then resync.

        The mid-session check runs behind a pairing dialog, and by the time
        that dialog closes a sync is usually already in flight: the event that
        concludes pairing (websocket_client's on_wpp_session_logged) calls
        on_messages_set() → _try_start_sync_thread() synchronously, and
        _set_wa_connected(True) fires trigger_sync_if_needed() beside it —
        both well before show_connection_dial()'s ShowModal() returns and
        starts this check at all. That round captured self.chats while it
        still held the previous account's chats and is merging the new
        account's list on top of them.

        Two things follow, and neither is fixed by the wipe itself.
        _try_start_sync_thread() sees that thread alive and returns True
        having started nothing, so the corrective full sync never happens;
        and the round keeps writing until it exits, so anything it commits
        after the wipe survives it — including into self.chats, which the
        corrective round would then merge onto rather than replace, leaving
        the merge permanently.

        So the wipe runs immediately (the protection cannot wait on a round
        that may take minutes, and a process killed in between still finds
        the divergence armed, since the recorded number is only rewritten at
        the end of a completed pass), and this thread runs the same pass again
        once the contaminated round has genuinely exited. The second pass is
        cheap by then: an empty database and two empty directories.

        The second pass takes ``previous_digits`` for the reason the first
        one does not have to. Whenever the first pass got as far as recording
        the new account, this one starts with the key naming it — so a wipe
        that empties nothing here (this thread is a daemon too, and the
        shutdown does not wait for it either) would leave that name standing
        over the rows the contaminated round committed while it was exiting:
        an account switch left half done with nothing able to see it any more,
        since every later pass compares the key against the linked phone and
        finds them equal. Handing the previous number back down keeps the key
        describing whichever account the messages on disk belong to, which is
        the invariant the whole check is written around. When the first pass
        emptied nothing, the key never moved off the previous account and the
        `!=` guard skips the write instead — nothing here is conditional on
        that pass having succeeded, and neither is this one running at all.

        _initial_sync_running is retaken after the join because the round we
        waited for cleared it in its own finally, and the loop exists for the
        gap between that clear and this retake, in which yet another trigger
        can have started one more round.

        _run_sync() now refuses to commit _sync_completed either way once its
        own _sync_run_id has been superseded (see the guard just before
        "Mark sync as done", added for this same issue — #198/#199) — that
        was the dangerous half of the problem: a contaminated round
        reaching that point used to overwrite this method's own
        _sync_completed=False back to True, which is not a cosmetic glitch
        but a permanent one, since trigger_sync_if_needed() would then never
        see a reason to run the corrective full sync at all.

        The mid-round writes are guarded too now (issue #198). _run_sync()
        checks its run id before announcing anything, after every list-chats
        fetch, before each self.chats assignment and set_chats(), and after
        the media phase. The RECENT wait and the media downloads stop on it
        by themselves, and sync_remote_chats() hands it to every
        sync_chat_messages() task, which checks it again once its request is
        back and before its first write. So the round being waited out stops
        within seconds of the wipe instead of minutes, no longer refills the
        list with the PREVIOUS account's conversations between the spoken
        "as conversas foram apagadas" and the second wipe, records no message
        failures and commits nothing either way.

        That shortens the window. It does not close it, which is why the join
        and the second wipe stay:

          * the thread is still is_alive() while it unwinds, and
            _try_start_sync_thread() answers True without starting anything
            in that gap;
          * a request already out when the bump lands still writes what it
            brings back, because no check can interrupt a call: an in-flight
            get_remote_chats() persists mute/pin/archive metadata through its
            own set_metadata_json() whatever the round then does with the
            list, get_remote_contacts() fills self.contacts, and a media
            download already running saves its file into media/. A
            sync_chat_messages() task discards its result instead — unless the
            bump falls in the moment between its last check and its database
            write, which does no I/O.

        Only clear_local_data() empties what escapes, and here that is the
        second wipe below. Its other callers have no second wipe: a confirmed
        logout (_on_disconnect(), websocket_client's logout handling), F5, and
        the pairing dialog's own wipes. For those, whatever escaped stays until
        the next clear — contacts and chat metadata of the previous session,
        and media files nothing refers to.

        When _ANOTHER_NUMBER_SYNC_JOIN_ROUNDS is spent with a round still alive
        (issue #199), this no longer wipes beside it. Leaving it to the health
        checker would have been slow, conditional and not clean:
        trigger_sync_if_needed() does start the corrective full sync once that
        round exits — it is superseded by the wipe, commits nothing, and only a
        commit clears _force_full_sync — but only while connected, only outside
        manual offline mode, only after its cooldown (_SYNC_RETRY_COOLDOWN ×
        _sync_retry_count, capped at 600 s, counted from that round's exit),
        and on top of whatever the round wrote after the wipe, which is what
        the second wipe exists to remove. So the round is superseded instead
        (the same _sync_run_id bump clear_local_data() makes), waited for
        within _ANOTHER_NUMBER_SYNC_EXIT_WAIT, and only then wiped over and
        replaced by a sync that really starts.

        A shutdown is waited out the same way rather than raced: the pass is a
        5 s UI wait and a clear_local_data() that rewrites the database and
        sweeps media/, which inside a close competes with db.close()'s drain
        and with the Chrome profile flush WM_QUERYENDSESSION exists to protect.
        And _shutting_down does not mean the process is ending — a shutdown
        another app cancels resets it — so this waits for it to clear and then
        carries on. It writes nothing while it waits. The full-sync latch is
        already in the database from the first pass, and nothing has emptied
        it since. The key stays on the new number the first pass recorded, and
        must: the next launch does NOT act on it, because the startup check
        runs only after a pairing (_just_paired). Put back on
        ``previous_digits``, it would sit there while the latched full sync
        refills the database with the new account, and this account's next
        pairing — with the same new phone, after any drop that does not wipe —
        would read a divergence and delete that whole history. What the second
        pass would have removed is what the fallback below leaves too, and is
        accepted for the same reason. Nothing in the teardown reads
        _initial_sync_running or joins this thread, so holding the claim
        through the wait delays no part of the close; when the shutdown is
        cancelled, the claim is what keeps trigger_sync_if_needed() from
        starting a round ahead of the second wipe, and the sync this thread
        then starts is the resumed one.

        Still open, deliberately:

          * a round that does not exit inside the bound gets the old
            behaviour: the wipe beside it and a warning in log.log. What that
            leaves is a COMPLETE sync, not a CLEAN one. Whatever the round
            brings back after the wipe survives it, the key already names the
            new number, so nothing will find it, and the corrective full sync
            merges on top. After #198 that is what an in-flight request writes
            — contacts, mute/pin/archive metadata, media files — rather than
            conversations, which sync_chat_messages() discards once
            superseded. The key is not held on the previous number for it:
            that would have this account's next pairing delete the new
            account's whole history to remove files nothing shows. The
            corrective sync then starts only through the health checker, under
            every condition above; the latch makes the next launch run it in
            full regardless.
          * a shutdown that outlasts the bound, or begins during the wipe
            itself, ends the pass with no corrective sync started this
            session. The next launch runs a full one either way: from the
            latch the first pass wrote, or, when the second wipe emptied the
            database, from "empty-local-cache". Anything that escaped the first
            wipe stays under the new number, exactly as in the fallback above.
          * a round that on_messages_set() starts during the second wipe
            makes _try_start_sync_thread() below answer True without starting
            anything. That round is superseded by the wipe, so since #198 it
            leaves within about a second and releases the claim, and the
            health checker starts the corrective sync after its cooldown.
        """
        try:
            for _ in range(self._ANOTHER_NUMBER_SYNC_JOIN_ROUNDS):
                in_flight.join()
                self._initial_sync_running = True
                nxt = getattr(self, "sync_thread", None)
                if nxt is None or nxt is in_flight or not nxt.is_alive():
                    break
                in_flight = nxt
            else:
                # Every round of the bound spent on yet another sync starting
                # in the gap. Practically unreachable, which is precisely why
                # it needs a line: without one the only way to diagnose it is
                # to guess. The wait below takes it from here.
                still = getattr(self, "sync_thread", None)
                logging.warning(
                    "[another_number_check] All %d joins spent and %s is still "
                    "running — superseding it and waiting up to %ss for it to "
                    "exit before wiping again.",
                    self._ANOTHER_NUMBER_SYNC_JOIN_ROUNDS,
                    getattr(still, "name", still),
                    self._ANOTHER_NUMBER_SYNC_EXIT_WAIT)

            # Two things must be over before the second pass may run: every
            # sync round (issue #199) and any shutdown in progress. On the
            # ordinary path both already are, and the first iteration breaks
            # without waiting, superseding or logging anything.
            #
            # A live round is ended rather than waited on: every round alive
            # now is one the wipe below empties anyway, and bumping
            # _sync_run_id is what clear_local_data() does to mark a round
            # stale, minus the deletion — so since #198 it leaves at its next
            # check. Bumped on every slice, not once: start_sync() and
            # clear_local_data() both read the counter and write it back
            # unlocked, so a round starting at that instant can overwrite a
            # single bump and run on as current. Bounded by time rather than by
            # rounds, so a newer round born in the gap is superseded too.
            #
            # A shutdown is waited out rather than raced — see the docstring
            # for what the pass would compete with, and why _shutting_down
            # being set is not proof the process is ending.
            deadline = time.monotonic() + self._ANOTHER_NUMBER_SYNC_EXIT_WAIT
            waited = False
            shutdown_logged = False
            while True:
                # Claim first, then look: a round alive at this point clears
                # the claim in its own finally, and the next pass takes it
                # back — the same order the join loop above keeps.
                self._initial_sync_running = True
                nxt = getattr(self, "sync_thread", None)
                alive = nxt is not None and nxt.is_alive()
                shutting_down = bool(getattr(self, "_shutting_down", False))
                if not alive and not shutting_down:
                    if waited:
                        logging.info(
                            "[another_number_check] Nothing is running or "
                            "closing any more — wiping again and starting the "
                            "corrective full sync.")
                    break
                waited = True
                if shutting_down and not shutdown_logged:
                    shutdown_logged = True
                    # Nothing is written while this waits — not the full-sync
                    # latch (the first pass wrote it and nothing has emptied it
                    # since) and not the key. Putting the key back on
                    # previous_digits here would outlive the close: the next
                    # launch refills the database with the new account under a
                    # key naming the old one, and this account's next pairing
                    # then deletes that whole history. See the docstring.
                    logging.info(
                        "[another_number_check] Shutting down — the second wipe "
                        "waits for the close to finish or be cancelled.")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    if shutting_down:
                        logging.warning(
                            "[another_number_check] Still shutting down after "
                            "%ss — the second wipe is not run and no sync is "
                            "started; this account's next pairing finds the "
                            "divergence again.",
                            self._ANOTHER_NUMBER_SYNC_EXIT_WAIT)
                        # Leaked, the claim would block every sync for the
                        # rest of a session that turns out to go on.
                        if not alive:
                            self._initial_sync_running = False
                        return
                    # The one case left as it was before #199: wipe beside the
                    # live round. See the docstring for what survives that
                    # and when the health checker starts the corrective sync.
                    logging.warning(
                        "[another_number_check] %s is still running after the "
                        "wait — wiping beside it; the health checker starts "
                        "the corrective full sync once it exits.",
                        getattr(nxt, "name", nxt))
                    break
                slice_seconds = min(self._ANOTHER_NUMBER_SYNC_EXIT_POLL, remaining)
                if alive:
                    self._sync_run_id = getattr(self, "_sync_run_id", 0) + 1
                    nxt.join(timeout=slice_seconds)
                else:
                    time.sleep(slice_seconds)
            self._apply_another_number_wipe(new_digits,
                                            previous_digits=previous_digits)
            self._sync_completed = False
            self._force_full_sync = True
            if getattr(self, "_shutting_down", False):
                # A close that began during the wipe. No latch and no start:
                # both are work inside the close, the "Sincronizando" sound
                # included, and the database just emptied latches full mode by
                # itself on the next launch ("empty-local-cache").
                logging.info(
                    "[another_number_check] Shutting down — the database was "
                    "wiped again, but no corrective sync is started.")
                existing = getattr(self, "sync_thread", None)
                if existing is None or not existing.is_alive():
                    self._initial_sync_running = False
                return
            # Same latch F5 sets, for the same reason — see the first call
            # site in _wipe_local_data_if_another_number_linked().
            self._persist_full_sync_pending(self._ANOTHER_NUMBER_WIPE_REASON)
            self._try_start_sync_thread()
        except Exception:
            # Only if nobody else holds it, exactly as the check's own finally
            # decides it — and for the same reason. This raising does not mean
            # the slot is free: _apply_another_number_wipe() can raise (a wx
            # call from _teardown_conversation_ui() after the MainLoop is gone,
            # clear_local_data() failing outside the two faults it swallows
            # itself — not save_settings(), which catches everything and
            # marshals a MessageBox rather than propagating) while
            # on_messages_set() → _try_start_sync_thread()
            # has already started a round of its own, whose claim this would
            # clear from underneath it — releasing the 60 s incremental poll
            # and F5 to write self.chats while it runs.
            existing = getattr(self, "sync_thread", None)
            if existing is None or not existing.is_alive():
                self._initial_sync_running = False
            logging.exception(
                "[another_number_check] Could not restart the sync after the "
                "wipe; the database is empty and the next reconnect will "
                "refill it.")

    def _act_on_unlink_decision(self, decision: str, *, log_label: str) -> None:
        """Common epilogue for connection_state.classify_unlinked()/
        classify_unlink_candidate(): act on a LOGOUT / RESUME_FAILED /
        RESUMING decision once the caller has already updated the strike
        counters for this reading. Shared by the WPPConnect status-string
        path (check_wa_connection_http()) and the local-401/403 path
        (_handle_local_auth_rejected()) so a destructive wipe is decided in
        exactly one place, with exactly one safety net — ``log_label`` is
        only for logging (a WPPConnect status string, or an "HTTP 401"-style
        label), the decision itself already reflects what it means.

        LOGOUT is the only outcome that can wipe data, and even then only
        against a probe that positively answered "no linked phone":
        _still_linked_on_server() asks WPPConnect directly for the host
        device, and its three outcomes are acted on differently.

          * LINKED   — the session named our own phone number, so it was
            never unlinked and whatever triggered this call was wrong. Veto:
            reset the tally, touch nothing. This is the "minha conta foi
            desconectada, mas o celular ainda mostra a sessão aberta"
            report, a session wiped on a couple of transient readings.
          * UNLINKED — the wipe goes ahead.
          * UNKNOWN  — the probe itself failed and proves nothing. Fall back
            to the pairing dialog WITHOUT wiping: on the 401/403 path this is
            the common case, since the probe leaves through the same local
            auth middleware that just rejected the status poll, and treating
            that as permission to wipe is exactly the bug the gate exists to
            stop.

        A LINKED veto is also counted, and only tolerated
        _STILL_LINKED_VETO_LIMIT times in a row — see that constant.

        Must be called with self._unlink_decision_lock already held — both
        callers acquire it before counting a strike, and the check-then-act
        on _logout_handled below only stays atomic across threads if the
        whole count-then-decide sequence is one critical section. This
        method does not acquire the lock itself (it would deadlock re-taking
        a plain, non-reentrant Lock the caller already holds).
        """
        import connection_state as cs

        def _logout_with_warning():
            self.error_sound.play()
            wx.MessageBox(
                self.i18n.t("device_logged_out"),
                self.i18n.t("error").format(app_name=self.app_name),
                wx.OK | wx.ICON_ERROR,
            )
            self._on_disconnect()

        if decision == cs.LOGOUT and not getattr(self, "_logout_handled", False):
            probe = self._still_linked_on_server()
            if probe == cs.LINK_PROBE_LINKED:
                vetoes = getattr(self, "_still_linked_vetoes", 0) + 1
                self._still_linked_vetoes = vetoes
                if vetoes < self._STILL_LINKED_VETO_LIMIT:
                    logging.warning(
                        "[_act_on_unlink_decision] %s confirmed by strikes, but "
                        "host-device still reports a linked phone — NOT a logout "
                        "(veto %d/%d). Resetting the tally.",
                        log_label, vetoes, self._STILL_LINKED_VETO_LIMIT,
                    )
                    self._logout_strikes = 0
                    self._resume_fail_strikes = 0
                    self._last_strike_ts = 0.0
                    return
                # Out of patience: the session insists it is linked while
                # nothing works. Stop vetoing, but still never wipe on
                # evidence this contradictory — the same non-destructive
                # landing RESUME_FAILED below uses gives the user a way out
                # and keeps the history whichever side the probe got wrong.
                self._logout_handled = True
                logging.warning(
                    "[_act_on_unlink_decision] %s confirmed by strikes and "
                    "vetoed by host-device %d times in a row — the session "
                    "claims to be linked but never recovers. Pairing dialog "
                    "WITHOUT wiping.", log_label, vetoes,
                )
                wx.CallAfter(lambda: self._on_disconnect(wipe=False))
                return
            if probe == cs.LINK_PROBE_UNKNOWN:
                # Ambiguous evidence must never be destructive: the probe
                # failing is itself the expected outcome of the local-401
                # path that most often gets here.
                self._logout_handled = True
                logging.warning(
                    "[_act_on_unlink_decision] %s confirmed by strikes, but the "
                    "host-device probe could not reach a verdict either way — "
                    "pairing dialog WITHOUT wiping.", log_label,
                )
                wx.CallAfter(lambda: self._on_disconnect(wipe=False))
                return
            self._logout_handled = True
            logging.warning(
                "[_act_on_unlink_decision] Confirmed logout (%s, %d strikes, "
                "host-device answered with no linked phone) — disconnecting "
                "and wiping.", log_label, self._logout_strikes,
            )
            wx.CallAfter(_logout_with_warning)
        elif decision == cs.RESUME_FAILED and not getattr(self, "_logout_handled", False):
            self._logout_handled = True
            logging.warning(
                "[_act_on_unlink_decision] Session did not resume after %d "
                "%s readings — pairing dialog WITHOUT wiping.",
                self._resume_fail_strikes, log_label,
            )
            wx.CallAfter(lambda: self._on_disconnect(wipe=False))
        elif decision in (cs.LOGOUT, cs.RESUME_FAILED):
            # Only reachable with _logout_handled already True: this reading
            # DID call for an action, one just fired earlier this run and
            # latched. Logged apart from the branch below because "data
            # preserved" would read as a deliberate verdict on this reading,
            # when the truth is that no verdict was reached at all — and
            # log.log is the primary tool for reconstructing a lost session
            # after the fact, where those two mean very different things.
            logging.info(
                "[_act_on_unlink_decision] Unlinked signal '%s' → %s — already "
                "handled earlier this run, ignoring.", log_label, decision,
            )
        else:
            logging.info(
                "[_act_on_unlink_decision] Unlinked signal '%s' → %s — data "
                "preserved.", log_label, decision,
            )
