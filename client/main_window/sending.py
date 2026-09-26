"""SendingMixin — part of MainWindow (see main_window/__init__.py).

Moved verbatim out of main.py. Methods run with ``self`` bound to the
MainWindow instance, so every attribute set in MainWindow.__init__ is
available here.
"""

import base64
import json
import logging
import os
import requests
import shutil
import subprocess
import sys
import threading
import time
import wx
from core.message_queue import MessageCancelled
from core.meta_ai import (
    STATE_ACCEPTED,
    STATE_NOT_ACCEPTED,
    is_meta_ai_jid,
    terms_state,
)
from core.send_contract import (
    accepted_message_id,
    send_failure_is_ambiguous,
)
from core.api_client import (
    api_get,
    api_post,
)
from app_paths import (
    data_path,
    resource_path,
)
from core.utils import encrypt
from core.voice_stereo import opus_encode_args

# The directory layout _find_api_ffmpeg() searches is relative to main.py.
_MAIN_PY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")



class SendingMixin:
    """Sending messages: text, audio, media, contacts, reactions, pins; send
    capability checks, Meta AI terms and the message-queue callbacks.
    """

    # Delays between attempts when the route could not answer, and the whole
    # budget: ~2.5 minutes, then the latch is re-armed and a later confirmed
    # connection may try again. Bounded and short on purpose — all this decides
    # is whether one incompatibility warning is spoken, so it must never turn
    # into a background poller of a route that runs page.evaluate work.
    _SEND_CAPABILITIES_RETRY_DELAYS = (30.0, 120.0)

    def meta_ai_terms_state(self) -> str:
        """Whether this account accepted Meta AI's terms: accepted / not_accepted
        / unknown. Unknown (probe failed, session detached) never blocks a send."""
        try:
            url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/meta-ai-terms"
            return terms_state(api_get(url, token=self.token, timeout=8).json())
        except Exception:
            logging.warning("[meta_ai] terms state probe failed", exc_info=True)
            return "unknown"

    def accept_meta_ai_terms(self) -> bool:
        """Record the user's acceptance with WhatsApp. True only when it stuck."""
        try:
            url = (f"{self.wpp_server}:{self.wpp_port}/api/{self.token}"
                   "/meta-ai-terms/accept")
            response = api_post(url, token=self.token, json={}, timeout=20)
            return response.status_code == 200 and (
                terms_state(response.json()) == STATE_ACCEPTED)
        except Exception:
            logging.warning("[meta_ai] accepting the terms failed", exc_info=True)
            return False

    def ensure_meta_ai_terms(self, remote_jid: str) -> bool:
        """Before a message to Meta AI: True when it may be sent.

        WhatsApp refuses every message to Meta AI until its terms are accepted
        (core/meta_ai.py). If they are not, the user is asked, with the terms a
        link away and a checkbox that must be ticked; nothing is accepted for
        them otherwise. Declining keeps the text in the composer.
        """
        if not is_meta_ai_jid(remote_jid):
            return True
        if getattr(self, "_meta_ai_terms_accepted", False):
            return True
        # A probe that could not answer costs up to 8 s on the UI thread; do
        # not repeat it on every message while the session is wedged.
        if time.monotonic() < getattr(self, "_meta_ai_probe_retry_at", 0.0):
            return True
        state = self.meta_ai_terms_state()
        if state != STATE_NOT_ACCEPTED:
            if state == STATE_ACCEPTED:
                self._meta_ai_terms_accepted = True
            else:
                self._meta_ai_probe_retry_at = time.monotonic() + 60.0
            return True
        from ui.dialogs.meta_ai_terms import MetaAiTermsDialog
        dialog = MetaAiTermsDialog(self, self.i18n)
        try:
            agreed = dialog.ShowModal() == wx.ID_OK
        finally:
            dialog.Destroy()
        if not agreed:
            return False
        if not self.accept_meta_ai_terms():
            wx.MessageBox(
                self.i18n.t("meta_ai_terms_failed"),
                self.i18n.t("meta_ai_terms_title"),
                wx.OK | wx.ICON_ERROR,
            )
            return False
        self._meta_ai_terms_accepted = True
        return True

    def _check_send_capabilities(self):
        """Warn once when an update changed a send API WinZapp depends on.

        Only a real verdict from the probe counts. `statusConnection` fronts
        this route and answers 404 {"response": null, "status": "Disconnected"}
        whenever the session is not attached yet — which says nothing about
        compatibility, and used to be read as one: the probe ran from
        _check_wpp_version_pin(), i.e. from ensure_wpp_running(), before the
        session was even paired, so every single cold start told a blind user
        out loud that their installation was incompatible. That answer has the
        same meaning as the request having failed outright, so it takes the
        same branch. This now runs from the first confirmed connection instead
        (see _set_wa_connected), which is the earliest point the probe can
        answer at all.

        An unavailable answer is retried here, on this thread, along
        _SEND_CAPABILITIES_RETRY_DELAYS, and only then re-arms the probe
        (`_send_capabilities_checked`) for a later confirmed connection. On
        wppconnect 2.3.2 the route can be fronted by a statusConnection probe
        that is itself waiting on a reloading page, and a connection whose
        CONNECTED came from the state listener rather than from isConnected()
        is exactly when that happens — one shot per process would spend it
        there and never warn.

        The retry is what covers that case, and the re-arm alone does not:
        _set_wa_connected() returns early when nothing changed, so the latch is
        only ever re-read on a real offline→online transition — and the
        "the event wins" session this is written for is precisely the one whose
        connection may never oscillate again. The latch stays set while the
        retries run, so a reconnection in the middle of them cannot start a
        second probe alongside this one.
        """
        for delay in (0.0,) + self._SEND_CAPABILITIES_RETRY_DELAYS:
            if delay:
                # Checked before sleeping, not after: a connection that has
                # already dropped will re-arm the latch and start a fresh probe
                # of its own when it comes back, so waiting here would only
                # race it — and this thread would sit through the whole delay
                # for nothing on the way to a shutdown.
                if not getattr(self, "_wa_connected", False):
                    break
                if getattr(self, "_shutting_down", False):
                    break
                time.sleep(delay)
            try:
                url = (
                    f"{self.wpp_server}:{self.wpp_port}/api/{self.token}"
                    "/send-capabilities"
                )
                response = api_get(url, token=self.token, timeout=10)
                body = response.json()
                details = body.get("response") if isinstance(body, dict) else None
                if not isinstance(details, dict) or "compatible" not in details:
                    # 404/Disconnected, a 500 from a page.evaluate that could
                    # not run, anything else without a verdict: unavailable,
                    # not incompatible.
                    logging.warning(
                        "[startup] Send compatibility probe unavailable (HTTP %s): %s",
                        response.status_code, str(body)[:300],
                    )
                    continue
                if details.get("compatible") is True:
                    logging.info("[startup] Send compatibility probe passed: %s", details)
                    return
                signature = json.dumps(details, ensure_ascii=False, sort_keys=True)[:1000]
                if getattr(self, "_send_capabilities_warning", "") == signature:
                    return
                self._send_capabilities_warning = signature
                logging.error("[startup] Send compatibility probe failed: %s", signature)
                # statusReaction is a private, reverse-engineered module lookup
                # (see deviceController.ts) that WhatsApp Web breaks on its own
                # schedule, independent of the public send primitives below it
                # in the same probe. When it is the *only* thing missing, real
                # sending is unaffected — announcing the generic warning here
                # was a chronic false positive that told a blind user their
                # whole connection was suspect over a feature they may never
                # touch. Anything else missing still means real sending is at
                # risk, so it keeps the broader warning.
                missing = details.get("missing")
                if missing == ["statusReaction"]:
                    key = "status_reaction_capability_incompatible"
                else:
                    key = "send_capabilities_incompatible"
                # Deliberately NOT interrupt=True: the unpinned-version warning
                # is queued moments earlier on the one path where both fire, and
                # interrupting cut it off mid-sentence — leaving the user with
                # neither message.
                wx.CallAfter(self.output, self.i18n.t(key))
                return
            except Exception as exc:
                logging.warning("[startup] Send compatibility probe unavailable: %s", exc)
        # Nothing answered within the budget — hand the next confirmed
        # connection its own chance.
        self._send_capabilities_checked = False

    def _check_wa_connection_closed(self, response) -> bool:
        """Detect a response that means "WhatsApp is not connected".

        Two shapes matter:

        * HTTP 404 with ``{"status": "Disconnected"}`` — WPPConnect's
          statusConnection middleware answers this for *every* route (send,
          list-chats, …) whenever ``isConnected()`` is false, i.e. whenever the
          machine has no internet.  It is the single most reliable offline
          signal the API gives us.
        * a 'Connection Closed' error message from Baileys.

        Marks the connection as down (which pauses the MessageQueue and turns
        on automatic offline mode) and returns True when either is seen.

        **``reason: "probe_timeout"`` is the exception, and the only one.**
        That 404 says the middleware's own bounded ``isConnected()`` probe went
        unanswered inside its 8 s budget — proof that the request never reached
        a controller, and no evidence at all about WhatsApp.  It is still
        "disconnected" for the caller (every send returns
        ``{"disconnected": True}`` so MessageQueue keeps the message queued
        rather than dropping it as ambiguous, and every sync caller leaves its
        retry ladder), but it must not flip the connection state: this
        middleware also fronts ``list-chats``, whose callers
        (``get_remote_chats`` from ``start_sync``, the post-sync settling pass,
        ``_probe_chats_and_start_sync``) are background work nobody asked for.
        An ordinary WhatsApp Web reload overlapping a sync round would then
        announce "modo offline" with sound and speech and, once the next probe
        found the page healthy again, "conexão restaurada" seconds later — the
        exact outcome ``_OFFLINE_PROBE_STRIKES`` was added for, over the
        session probe, after a measured 28 s reload did it.  Nothing is lost by
        staying quiet: ``check-connection-session`` is deliberately *not* behind
        this middleware, and it runs a bounded probe of its own (8 s, under the
        10 s ``check_whatsapp_reachable()`` gives that request) that answers
        ``status: false`` when the probe goes unanswered — so a page that really
        is stuck still reaches the consecutive-strike tally there on the next
        health-check tick.  That budget on the Node side is what makes this
        sentence true, and it is not decoration: unbounded, this client timed
        out first, and a client-side timeout raises into an ``except`` that
        counts no strike at all — a page that never reaches ``WPP.isReady``
        would stay "connected" forever with every send quietly requeued.
        """
        disconnected = False
        probe_timeout = False
        try:
            body = response.json()
        except Exception:
            body = {}
        try:
            if response.status_code == 404 and isinstance(body, dict):
                if str(body.get("status", "")).lower() == "disconnected":
                    disconnected = True
                    probe_timeout = str(body.get("reason", "")) == "probe_timeout"
            if response.status_code in (500, 502, 503) and isinstance(body, dict):
                err_obj = body.get("error", {})
                err_name = str(err_obj.get("name", "")) if isinstance(err_obj, dict) else ""
                if "TargetCloseError" in err_name or "ProtocolError" in err_name or "TargetCloseError" in str(body):
                    disconnected = True
            if isinstance(body, dict):
                messages = body.get("response", {})
                messages = messages.get("message", []) if isinstance(messages, dict) else []
                if any("Connection Closed" in str(m) for m in messages):
                    disconnected = True
        except Exception:
            pass
        if disconnected and probe_timeout:
            logging.info(
                "[send] The connection probe in front of this route went unanswered "
                "(HTTP 404 probe_timeout) — treating the call as not delivered, but "
                "leaving the connection state alone; the health checker decides that."
            )
            return True
        if disconnected:
            logging.warning("[send] WhatsApp reported Disconnected or TargetCloseError — pausing queue and triggering session recovery")
            self._set_wa_connected(False, "API answered Disconnected or TargetCloseError")
            # Proactively schedule connection check to auto-recover session via HTTP
            wx.CallAfter(self.check_wa_connection_http)
        return disconnected

    def _classify_send_exception(self, exc, where: str) -> dict:
        """Turn a transport-level send failure into a queue instruction.

        A read timeout or a dropped connection is **not** evidence that the
        message was not sent: WPPConnect drives WhatsApp Web, which accepts an
        outgoing message into its own outbox and flushes it as soon as the
        phone/network is back.  Retrying such a send is what produced the
        reported "30 copies of the same message arrive when the internet comes
        back" — every retry queued another genuine copy inside WhatsApp Web.

        So these are reported as *ambiguous*: the queue drops the message
        instead of resending it, and the WebSocket echo of the real send (which
        is matched against the pending virtual message) resolves the UI if and
        when WhatsApp actually delivers it.
        """
        err = str(exc)[:200]
        ambiguous = isinstance(exc, (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
            requests.exceptions.ChunkedEncodingError,
        ))
        logging.error("[%s] request exception (ambiguous=%s): %s", where, ambiguous, err)
        if ambiguous:
            return {"ok": False, "error": err, "retry": False, "ambiguous": True}
        return {"ok": False, "error": err, "retry": True}

    def _serialize_quoted_id(self, quoted: dict, fallback_jid: str = None) -> str:
        """Serialize a quoted message key into the format expected by WPPConnect.

        Delegates to _serialize_msg_id, which keeps whatever JID variant
        (@lid or phone) the message was actually keyed under in WPPConnect's
        internal store — rewriting @lid to phone here makes the lookup miss
        and the reply fail (same root cause as the media-download bug).
        """
        if not quoted or not isinstance(quoted, dict):
            return None
        raw_key = quoted.get("key", {})
        if not isinstance(raw_key, dict) or not raw_key.get("id"):
            return None
        # key.remoteJid can be empty for own messages in local cache, fallback to current conversation JID
        remote_jid = raw_key.get("remoteJid") or fallback_jid or ""
        # Swap self-JID with fallback_jid (the other person in the 1-on-1 chat) to prevent WPPConnect lookup fail
        if self._is_self_jid(remote_jid) and fallback_jid:
            remote_jid = fallback_jid
        return self._serialize_msg_id(remote_jid, raw_key)

    def _canonical_mention_jids(self, mentioned_jids):
        """Return mention JIDs in the phone-number format Baileys/WPPConnect can tag."""
        out = []
        seen = set()
        lid_to_phone = getattr(self, "_lid_to_phone", {})
        for raw_jid in mentioned_jids or []:
            jid = self._normalize_jid(str(raw_jid or ""))
            if not jid:
                continue
            if jid.endswith("@lid"):
                jid = lid_to_phone.get(jid, jid)
            if jid not in seen:
                seen.add(jid)
                out.append(jid)
        return out

    def _resolve_jid_for_send(self, jid: str) -> str:
        """
        Destination JID for the WPPConnect *send* endpoints: @lid whenever the
        cache knows one, otherwise the @c.us phone form.

        This deliberately prefers @lid, same policy as
        _resolve_jid_for_chat_state (typing/presence) and as the message-key
        serialization in _serialize_msg_id — WhatsApp Web keys the chat, and
        every message in it, under the @lid once the account is on LID
        addressing, and reports the phone JID only as the chat's legacy
        `historyChatId`. Sending to that legacy address does NOT fail loudly:
        WhatsApp Web creates the message, hands back a real 3EB0… id (so the
        HTTP call looks like a success) and then never gets it acked by the
        server — ack stays 0/CLOCK, the message sits in the browser's outbox
        forever and reaches neither the phone nor the recipient. Groups and
        broadcast lists keep their own address, which is canonical everywhere.

        The @c.us form is still used, as a *fallback*, by every send method
        whenever the @lid destination is refused with a definite HTTP error
        (the pre-LID behaviour, which existed because a @lid chat that
        Puppeteer has not loaded yet answers 400 "o número não existe"); see
        _legacy_phone_for_send.
        """
        return self._resolve_jid_for_chat_state(jid)

    def _legacy_phone_for_send(self, jid: str) -> str:
        """Legacy @c.us address to retry a failed send on, or '' when there is none.

        Only meaningful for private chats: groups/broadcast lists have a single
        canonical address and nothing to fall back to.
        """
        if not jid or jid.endswith(("@g.us", "@broadcast")):
            return ""
        if jid.endswith("@lid"):
            phone_net = getattr(self, "_lid_to_phone", {}).get(jid, "")
            return phone_net.replace("@s.whatsapp.net", "@c.us") if phone_net else ""
        return jid.replace("@s.whatsapp.net", "@c.us")

    def _resolve_jid_for_msg_key(self, jid: str) -> str:
        """
        Phone/@c.us form of a JID, translating @lid back through the cache.

        Kept separate from _resolve_jid_for_send: fetch_older_messages uses this
        both as the /get-messages/:phone URL parameter and as the chat segment of
        the serialized message id it asks for, and that pair has to stay on the
        phone form — see the comment at its call site.
        """
        if not jid:
            return jid
        if jid.endswith(("@g.us", "@broadcast")):
            return jid
        if jid.endswith("@lid"):
            phone_net = getattr(self, "_lid_to_phone", {}).get(jid, jid)
            if phone_net:
                return phone_net.replace("@s.whatsapp.net", "@c.us")
            return jid
        if jid.endswith("@s.whatsapp.net"):
            return jid.replace("@s.whatsapp.net", "@c.us")
        return jid



    @staticmethod
    def _build_link_preview_options(link_preview: dict | None) -> dict:
        """The "options" value every send-message/-reply/-mentioned payload
        below carries for its link preview.

        ``linkPreview: False`` (no argument) keeps WPPConnect from ever
        generating one itself on send — that used to make sending hang until
        timeout, since WA-JS's own on-send fetch goes through undocumented
        third-party proxy servers (see send_text_message()'s own comment,
        commit 6cec2d0e). When the composer already resolved a preview via
        core/link_preview.py (independent of that mechanism, run ahead of
        time on a background thread), passing it here as an object instead
        of `True` makes WA-JS use it as-is with no fetch of its own — see
        `prepareLinkPreview` in wa-js: an object skips the network call
        entirely, only literal `true` triggers it.
        """
        if not link_preview:
            return {"linkPreview": False}
        return {
            "linkPreview": {
                "title": link_preview.get("title", ""),
                "description": link_preview.get("description", ""),
                "canonicalUrl": link_preview.get("canonicalUrl", ""),
                "matchedText": link_preview.get("canonicalUrl", ""),
            }
        }

    def send_text_message(self, remote_jid, text, quoted=None, mentioned_jids=None, link_preview=None):
        """Send a plain-text message via the WPPConnect Server API."""
        # Canonical destination: @lid when known, else the @c.us phone form —
        # see _resolve_jid_for_send's docstring for why @lid has to win here.
        remote_jid = self._resolve_jid_for_send(remote_jid)
        is_lid_target = remote_jid.endswith("@lid")
        logging.info("[send_text_message] destination resolved to %s (isLid=%s)", remote_jid, is_lid_target)

        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }

        quoted_id = None
        quote_stripped = False
        is_status_reply = False
        link_preview_options = self._build_link_preview_options(link_preview)

        if mentioned_jids:
            url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/send-mentioned"
            phone_net = remote_jid
            if phone_net.endswith("@s.whatsapp.net"):
                phone_net = phone_net.replace("@s.whatsapp.net", "@c.us")
            
            mentioned = self._canonical_mention_jids(mentioned_jids)
            mentioned_clean = [m.replace("@s.whatsapp.net", "@c.us") if m.endswith("@s.whatsapp.net") else m for m in mentioned]
            
            payload = {
                "phone": [phone_net],
                "message": text,
                "mentioned": mentioned_clean,
                "isGroup": phone_net.endswith("@g.us"),
                "isLid": is_lid_target,
                "options": link_preview_options
            }
        else:
            quoted_id = self._serialize_quoted_id(quoted, fallback_jid=remote_jid) if quoted else None
            # A status quote that failed to serialize (e.g. incomplete status
            # metadata missing key.id) must never silently fall through to a
            # plain DM below — that's the exact "reply degrades to a normal
            # message" bug this is meant to fix, just triggered by malformed
            # data instead of a WPPConnect failure.
            quoted_is_status = (
                bool(quoted) and isinstance(quoted, dict)
                and (quoted.get("key") or {}).get("remoteJid") == "status@broadcast"
            )
            if quoted_id:
                is_status_reply = "status@broadcast" in quoted_id
                url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/send-reply"
                phone_net = remote_jid
                if phone_net.endswith("@s.whatsapp.net"):
                    phone_net = phone_net.replace("@s.whatsapp.net", "@c.us")
                payload = {
                    "phone": [phone_net],
                    "message": text,
                    "messageId": quoted_id,
                    "isGroup": phone_net.endswith("@g.us"),
                    "isLid": is_lid_target,
                    "options": link_preview_options
                }
                # INFO, not DEBUG: log.log runs at INFO, and a "reply sent
                # without its quote" report is undiagnosable without the id
                # and type that were actually quoted (Pedro's log had the
                # 201 and the echo, and nothing about what was quoted).
                logging.info(
                    "[send_text_message] sending quoted reply via send-reply to %s, "
                    "quoted id=%s type=%s", phone_net, quoted_id,
                    (quoted.get("messageType") or "?") if isinstance(quoted, dict) else "?",
                )
            elif quoted_is_status:
                logging.error("[send_text_message] status reply has no serializable quote id (key.id missing?) — refusing to send as a plain message")
                return {"ok": False, "error": "status reply missing a serializable message id", "retry": False}
            else:
                phone_net = remote_jid
                if phone_net.endswith("@s.whatsapp.net"):
                    phone_net = phone_net.replace("@s.whatsapp.net", "@c.us")
                url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/send-message"
                payload = {
                    "phone": [phone_net],
                    "message": text,
                    "isGroup": phone_net.endswith("@g.us"),
                    "isLid": is_lid_target,
                    "options": link_preview_options
                }
        try:
            # 25s (not 15s): WPPConnect can take longer to ack under load (e.g.
            # concurrent media sync). A client-side timeout here is indistinguishable
            # from a real failure to MessageQueue, which then retries — if the
            # original request actually went through server-side, that retry sends
            # a genuine duplicate message to the recipient. A more generous timeout
            # reduces how often that false-timeout/duplicate-send scenario happens.
            response = api_post(url, json=payload, headers=headers, timeout=25)
            active_dest = phone_net
            if response.status_code not in (200, 201):
                # 1. The @lid destination was refused: fall back to the legacy
                #    @c.us address. Historically this fired only on the 400
                #    "o número não existe" that a @lid chat Puppeteer has not
                #    loaded yet answers with, but any definite 4xx/5xx on a @lid
                #    destination is worth one legacy attempt — that address is
                #    what WinZapp used before and it still works for chats
                #    WhatsApp has not moved to LID addressing.
                #    Skipped when the API reports the session as disconnected:
                #    nothing was sent, and the message must stay queued as-is
                #    instead of burning a retry (see MessageQueue).
                #
                #    A 5xx is excluded, and that is not a narrowing of "any
                #    definite 4xx/5xx" — it is the word *definite* being
                #    honoured. See send_failure_is_ambiguous(): the controller
                #    threw after handing the message to WhatsApp Web, so a
                #    second attempt here is a second message.
                fb_phone = self._legacy_phone_for_send(remote_jid) if is_lid_target else ""
                if (fb_phone and not self._check_wa_connection_closed(response)
                        and not send_failure_is_ambiguous(response.status_code)):
                    logging.warning(
                        "[send_text_message] @lid destination %s refused (HTTP %s: %s) — retrying with legacy %s",
                        remote_jid, response.status_code, response.text[:200], fb_phone,
                    )
                    if mentioned_jids:
                        retry_url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/send-mentioned"
                        retry_payload = {
                            "phone": [fb_phone], "message": text,
                            "mentioned": mentioned_clean,
                            "isGroup": fb_phone.endswith("@g.us"),
                            "isLid": False,
                            "options": link_preview_options
                        }
                    elif quoted_id:
                        retry_url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/send-reply"
                        retry_payload = {
                            "phone": [fb_phone], "message": text,
                            "messageId": quoted_id, "isGroup": fb_phone.endswith("@g.us"),
                            "isLid": False,
                            "options": link_preview_options
                        }
                    else:
                        retry_url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/send-message"
                        retry_payload = {
                            "phone": [fb_phone], "message": text,
                            "isGroup": fb_phone.endswith("@g.us"),
                            "isLid": False,
                            "options": link_preview_options
                        }
                    active_dest = fb_phone
                    response = api_post(retry_url, json=retry_payload, headers=headers, timeout=25)
                    if response.status_code in (200, 201):
                        logging.info("[send_text_message] legacy retry with %s succeeded", fb_phone)

                # 2. If it's still failing and we had an ordinary chat quote,
                #    strip the quote and try a plain send.  A status reply must
                #    never take this fallback: a plain DM is observably the
                #    wrong operation, so report failure and let the user retry
                #    instead of claiming that an unquoted reply succeeded.
                #
                #    Nor may it take it after an ambiguous failure. This is the
                #    duplicate a user reported as "the reply shows up correctly
                #    and is then duplicated": /send-reply answered 500, the
                #    quote was stripped, the plain copy was sent — and the echo
                #    of the ORIGINAL reply had already arrived 10 ms before that
                #    500 (see send_failure_is_ambiguous() for the measurement).
                #    Two messages on WhatsApp for one action, the second one
                #    quietly missing the quote, which is what makes it read as
                #    the same message sent twice.
                if (response.status_code not in (200, 201) and quoted_id
                        and not is_status_reply
                        and not send_failure_is_ambiguous(response.status_code)):
                    logging.warning("[send_text_message] Quoted send failed (HTTP %s). Retrying without quote on %s...",
                                    response.status_code, active_dest)
                    url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/send-message"
                    payload = {
                        "phone": [active_dest],
                        "message": text,
                        "isGroup": active_dest.endswith("@g.us"),
                        "isLid": active_dest.endswith("@lid"),
                        "options": link_preview_options
                    }
                    response = api_post(url, json=payload, headers=headers, timeout=25)
                    if response.status_code in (200, 201):
                        wx.CallAfter(self.output, self.i18n.t("reply_quote_lost"))
                        quote_stripped = True

                # 3. Final error handling if all retries failed
                if response.status_code not in (200, 201):
                    err = f"HTTP {response.status_code}: {response.text[:300]}"
                    logging.error("[send_text_message] All send attempts failed: %s for %s", err, remote_jid)
                    if self._check_wa_connection_closed(response):
                        # WhatsApp is down: the message was definitely NOT sent,
                        # so it stays queued — but never retried in a loop while
                        # the connection is out (see MessageQueue).
                        return {"ok": False, "error": err, "retry": False, "disconnected": True}
                    if send_failure_is_ambiguous(response.status_code):
                        # Skipping the fallbacks above only moves the duplicate
                        # if this hands the same send back to the queue as
                        # retryable — MessageQueue would then resend a message
                        # that may already be on its way, which is the failure
                        # _classify_send_exception() exists to prevent for a
                        # timeout. Same evidence, same answer: drop it here and
                        # let the WebSocket echo resolve the pending row if
                        # WhatsApp really delivered it.
                        logging.warning(
                            "[send_text_message] HTTP %s is ambiguous — the message "
                            "may already be on its way, so it is NOT resent. Body: %s",
                            response.status_code, response.text[:300],
                        )
                        return {"ok": False, "error": err, "retry": False,
                                "ambiguous": True}
                    # If it's a transient error, mark retryable
                    is_retryable = response.status_code in (408, 429)
                    return {"ok": False, "error": err, "retry": is_retryable}


            self._set_wa_connected(True, "send succeeded")
            try:
                clean_id = accepted_message_id(response.json())
            except (ValueError, TypeError) as exc:
                # str(exc) is an English developer diagnostic (see
                # SendContractError) and this string reaches the user — it
                # ends up in msg.last_error and, for media, in a MessageBox.
                # The log gets the detail, the user gets a translated reason.
                logging.error("[send_text_message] invalid success response: %s", exc)
                # A refusal (negative ACK) from Meta AI's chat means its terms
                # were not accepted (core/meta_ai.py) -- say so, rather than
                # sending the user off to check a conversation for a message
                # that never left.
                if getattr(exc, "reason", "") == "rejected" and is_meta_ai_jid(remote_jid):
                    return {
                        "ok": False,
                        "error": self.i18n.t("meta_ai_send_rejected_error"),
                        "retry": False,
                    }
                return {
                    "ok": False,
                    "error": self.i18n.t("send_not_confirmed_error"),
                    "retry": False,
                }
            if quote_stripped:
                return {"ok": True, "id": clean_id, "quote_lost": True}
            return clean_id
        except Exception as exc:
            return self._classify_send_exception(exc, "send_text_message")

    @staticmethod
    def _find_api_ffmpeg() -> str:
        """Locate ffmpeg binary: check bundled lib/ first, then node_modules, then system PATH."""
        import glob as _glob
        import shutil
        # 1. Check bundled lib/ directory first (client/lib in dev mode, lib/ in compiled mode)
        lib_dirs = [
            resource_path("lib"),
            resource_path("client", "lib"),
            os.path.join(os.path.dirname(_MAIN_PY), "lib"),
            os.path.join(os.path.dirname(os.path.dirname(_MAIN_PY)), "client", "lib"),
        ]
        for lib_dir in lib_dirs:
            for name in ["ffmpeg.exe", "ffmpeg"]:
                path = os.path.join(lib_dir, name)
                if os.path.isfile(path):
                    return path

        # 2. Bundled npm package (local API dev/run mode)
        installer_roots = [
            resource_path("api", "node_modules", "@ffmpeg-installer"),
            resource_path("client", "api", "node_modules", "@ffmpeg-installer"),
            os.path.join(os.path.dirname(_MAIN_PY), "api", "node_modules", "@ffmpeg-installer"),
            os.path.join(os.path.dirname(os.path.dirname(_MAIN_PY)), "client", "api", "node_modules", "@ffmpeg-installer"),
            os.path.join(os.path.dirname(os.path.dirname(_MAIN_PY)), "node_modules", "@ffmpeg-installer"),
        ]
        for installer_root in installer_roots:
            if not os.path.isdir(installer_root):
                continue
            explicit_paths = [
                os.path.join(installer_root, "win32-x64", "ffmpeg.exe"),
                os.path.join(installer_root, "win32-ia32", "ffmpeg.exe"),
                os.path.join(installer_root, "win32-arm64", "ffmpeg.exe"),
                os.path.join(installer_root, "ffmpeg", "bin", "ffmpeg.exe"),
                os.path.join(installer_root, "ffmpeg", "bin", "ffmpeg"),
                os.path.join(installer_root, "linux-x64", "ffmpeg"),
            ]
            for ep in explicit_paths:
                if os.path.isfile(ep):
                    return ep

            hits = _glob.glob(os.path.join(installer_root, "**", "ffmpeg.exe"), recursive=True)
            if not hits:
                hits = _glob.glob(os.path.join(installer_root, "**", "ffmpeg"), recursive=True)
            if hits:
                return hits[0]

        # 3. Fallback: ffmpeg on the system PATH (user-installed)
        system_ffmpeg = shutil.which("ffmpeg")
        if system_ffmpeg:
            return system_ffmpeg
        return None

    def _convert_wav_to_ogg(self, wav_path: str, stereo: bool = False) -> str | None:
        """
        Convert a WAV file to OGG/Opus using the bundled ffmpeg binary.
        Returns the path to the new .ogg file, or None on failure.

        ``stereo`` keeps two channels (issue #82, core/voice_stereo.py); by
        default every recording is downmixed to mono, as before. Decided by
        the caller, never read off the WAV: a mono message recorded on a
        microphone that only opens with two channels has a stereo WAV.
        """
        ffmpeg = self._find_api_ffmpeg()
        if not ffmpeg or not os.path.isfile(ffmpeg):
            logging.warning("[audio] ffmpeg not found — sending WAV (may fail). Searched: %s",
                            resource_path("api", "node_modules", "@ffmpeg-installer", "ffmpeg", "bin"))
            return None
        ogg_path = wav_path + ".ogg"
        try:
            creationflags = 0
            if sys.platform == "win32" and hasattr(subprocess, "CREATE_NO_WINDOW"):
                creationflags = subprocess.CREATE_NO_WINDOW

            result = subprocess.run(
                [ffmpeg, "-y", "-i", wav_path,
                 *opus_encode_args(stereo),
                 "-vbr", "on", "-compression_level", "10",
                 ogg_path],
                capture_output=True,
                timeout=60,
                creationflags=creationflags,
            )
            if result.returncode == 0 and os.path.isfile(ogg_path) and os.path.getsize(ogg_path) > 0:
                logging.debug("[audio] WAV→OGG conversion succeeded: %s", ogg_path)
                return ogg_path
            logging.error("[audio] ffmpeg WAV→OGG failed (rc=%s): %s",
                          result.returncode,
                          (result.stderr or b"").decode("utf-8", errors="replace")[-800:])
        except Exception as exc:
            logging.error("[audio] ffmpeg conversion exception: %s", exc)
        return None

    def send_audio_message(self, remote_jid: str, wav_path: str, quoted=None,
                           ogg_bytes: bytes = None, stereo: bool = False) -> bool:
        """
        Encode a recorded WAV file to OGG Opus via FFmpeg (or pre-encoded ogg_bytes)
        and send it as a PTT voice message using /send-voice-base64.

        ogg_bytes: if provided (pre-encoded in background thread), skip the
                   disk read and OGG encoding entirely — just base64 + POST.
                   On retry (ogg_bytes=None) falls back to reading wav_path.
        """
        # Canonical destination: @lid when known, else the @c.us phone form —
        # see _resolve_jid_for_send's docstring for why @lid has to win here.
        import time as _time
        _tsend0 = _time.perf_counter()
        remote_jid = self._resolve_jid_for_send(remote_jid)
        is_lid_target = remote_jid.endswith("@lid")
        logging.info("[VOICE_TIMING] send_audio_message started for %s (isLid=%s, ogg_bytes=%s) — jid resolved in %.3fs",
                     remote_jid, is_lid_target, "yes" if ogg_bytes else "NO", _time.perf_counter() - _tsend0)

        if ogg_bytes is None:
            # Fallback path: convert WAV to OGG using ffmpeg and read the bytes
            _t_fallback = _time.perf_counter()
            logging.info("[VOICE_TIMING] ogg_bytes is None — running ffmpeg AGAIN as fallback (this should NOT happen!)")
            ogg_path = self._convert_wav_to_ogg(wav_path, stereo=stereo)
            if ogg_path and os.path.isfile(ogg_path):
                try:
                    with open(ogg_path, "rb") as fh:
                        ogg_bytes = fh.read()
                except Exception as exc:
                    logging.error("[send_audio_message] cannot read OGG file %s: %s", ogg_path, exc)
                finally:
                    try:
                        os.unlink(ogg_path)
                    except Exception:
                        pass

            if ogg_bytes is None:
                # If conversion failed, do not send raw WAV as it breaks WhatsApp (silent failure)
                err_msg = self.i18n.t("audio_convert_failed")
                logging.error("[send_audio_message] %s", err_msg)
                return {"ok": False, "error": err_msg, "retry": False}
            logging.info("[VOICE_TIMING] fallback encode+read done in %.3fs",
                         _time.perf_counter() - _t_fallback)

        audio_b64 = base64.b64encode(ogg_bytes).decode("utf-8")

        url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/send-voice-base64"
        phone_net = remote_jid
        if phone_net.endswith("@s.whatsapp.net"):
            phone_net = phone_net.replace("@s.whatsapp.net", "@c.us")
        quoted_id = self._serialize_quoted_id(quoted, fallback_jid=phone_net) if quoted else None
        payload = {
            "phone": [phone_net],
            "base64Ptt": f"data:audio/ogg;codecs=opus;base64,{audio_b64}",
            "isGroup": phone_net.endswith("@g.us"),
            "isLid": is_lid_target,
        }
        if quoted_id:
            payload["quotedMessageId"] = quoted_id
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }
        _t_post = _time.perf_counter()
        logging.info("[VOICE_TIMING] POSTing to send-voice-base64 (payload size ~%d bytes b64)",
                     len(audio_b64))
        try:
            response = api_post(url, json=payload, headers=headers, timeout=30)
            logging.info("[VOICE_TIMING] POST returned HTTP %s in %.3fs",
                         response.status_code, _time.perf_counter() - _t_post)
            if response.status_code not in (200, 201):
                err = f"HTTP {response.status_code}: {response.text[:300]}"
                logging.error("[send_audio_message] %s for %s", err, remote_jid)

                # Fallback: the @lid destination was refused — the chat may just
                # not be loaded in Puppeteer yet (classic 400 "o número não
                # existe"), but any definite failure on a @lid destination earns
                # one attempt on the legacy @c.us address. Skipped when the API
                # reports the session as disconnected: nothing was sent.
                fb_phone = self._legacy_phone_for_send(remote_jid) if is_lid_target else ""
                disc = self._check_wa_connection_closed(response)
                if fb_phone and not disc:
                    logging.warning("[send_audio_message] @lid destination %s refused (HTTP %s) — retrying with legacy %s",
                                    remote_jid, response.status_code, fb_phone)
                    retry_payload = {
                        "phone": [fb_phone],
                        "base64Ptt": f"data:audio/ogg;codecs=opus;base64,{audio_b64}",
                        "isGroup": fb_phone.endswith("@g.us"),
                        "isLid": False,
                    }
                    if quoted_id:
                        retry_payload["quotedMessageId"] = quoted_id
                    response = api_post(url, json=retry_payload, headers=headers, timeout=30)
                    if response.status_code in (200, 201):
                        logging.info("[send_audio_message] legacy retry with %s succeeded", fb_phone)
                        # fall through to normal response parsing below
                    else:
                        err = f"HTTP {response.status_code}: {response.text[:300]}"
                        logging.error("[send_audio_message] legacy retry also failed: %s", err)
                        if self._check_wa_connection_closed(response):
                            return {"ok": False, "error": err, "retry": False, "disconnected": True}
                        return {"ok": False, "error": err, "retry": True}
                else:
                    return {"ok": False, "error": err, "retry": False, "disconnected": disc}

            self._set_wa_connected(True, "audio send succeeded")
            try:
                return accepted_message_id(response.json())
            except (ValueError, TypeError) as exc:
                logging.error("[send_audio_message] invalid success response: %s", exc)
                return {
                    "ok": False,
                    "error": self.i18n.t("send_not_confirmed_error"),
                    "retry": False,
                }
        except Exception as e:
            return self._classify_send_exception(e, "send_audio_message")


    def _serialize_msg_id(self, remote_jid: str, msg_key: dict, full_msg: dict = None) -> str:
        """
        Build the full serialized WhatsApp message ID expected by WPPConnect
        (`WPP.chat.getMessageById`).  The bare key.id is not enough — the library
        needs `<fromMe>_<chatId>_<id>` and, for group messages, a trailing
        `_<participant>` — including for our own group messages (`fromMe=True`).
        """
        def _resolve_to_lid_if_available(jid: str) -> str:
            """Resolve JID to cached @lid if available, keeping @g.us / @broadcast, and formatting to @c.us otherwise."""
            if not jid:
                return jid
            if jid.endswith(("@g.us", "@broadcast")):
                return jid
            if jid.endswith("@lid"):
                return jid
            clean = jid.replace("@c.us", "@s.whatsapp.net")
            lid = getattr(self, "_phone_to_lid", {}).get(clean, "")
            if lid:
                return lid
            return jid.replace("@s.whatsapp.net", "@c.us")

        msg_id = msg_key.get("id", "")
        if not msg_id:
            return ""
        # A serialized id may already have been stored as the key id.
        if msg_id.startswith(("true_", "false_")):
            # If it's a 1-on-1 message (not @g.us or @broadcast), truncate any 4th trailing participant segment
            parts = msg_id.split("_")
            if len(parts) > 3 and not ("@g.us" in parts[1] or "@broadcast" in parts[1]):
                return "_".join(parts[:3])
            return msg_id
        from_me = bool(msg_key.get("fromMe", False))
        prefix = "true" if from_me else "false"

        raw_remote = remote_jid or msg_key.get("remoteJid", "") or (full_msg.get("from") if isinstance(full_msg, dict) else "") or ""
        if raw_remote.endswith("@g.us"):
            chat = raw_remote
        else:
            # Own 1-on-1 messages sent through WinZapp are built from
            # self.conversation["remoteJid"] at compose time, which can still
            # be the phone/@c.us form even though _resolve_jid_for_send()
            # (send_text_message() et al.) prefers @lid for the actual send
            # whenever the cache knows one — WhatsApp Web indexes every
            # message of a @lid-addressed chat, including our own outgoing
            # ones, under that @lid in its internal Store. Building the
            # quoted-id's chat segment from the phone form here then no
            # longer matches what Store actually has, and the quote silently
            # fails with a lookup miss (reported as "não foi possível citar
            # a mensagem" — private chats only; groups keep their own
            # canonical @g.us address and never hit this). A message just
            # RECEIVED from the other party doesn't have this problem: its
            # remoteJid, as reported live for an already-@lid chat, already
            # arrives in @lid form. Preferring the cached @lid here — same
            # policy as the group "participant" segment below and as
            # _resolve_jid_for_send() itself — keeps quoting an own private
            # message pointed at whatever address WhatsApp Web actually
            # filed it under.
            chat = _resolve_to_lid_if_available(raw_remote)

        # Group messages — and status updates (status@broadcast is a shared
        # "chat" the same way a group is: WPPConnect/Baileys need the actual
        # poster's JID as the trailing participant segment to look up a
        # specific status in Store, exactly like a specific group message) —
        # always carry the sender's JID in the serialized id, even for our
        # own (fromMe=True). 1-on-1 keys have no participant.
        #
        # Dropping this segment for @broadcast used to make every status
        # video/audio silently fail to play and every status "like" fail
        # with a generic server error: WPPConnect's getMessageById() (media
        # download) and its reaction endpoint both look up
        # status@broadcast messages by <chat>_<id>_<participant> — the
        # 2-segment id this produced without a participant never matched
        # anything in Store, so both requests failed on a status update that
        # was otherwise perfectly available.
        participant = ""
        if chat.endswith(("@g.us", "@broadcast")):
            if from_me:
                raw = (
                    getattr(self, "my_lid", "")
                    or getattr(self, "my_jid", "")
                    or msg_key.get("participant")
                    or (full_msg.get("participant") if isinstance(full_msg, dict) else "")
                    or (full_msg.get("author") if isinstance(full_msg, dict) else "")
                    or ""
                )
            else:
                raw = (
                    msg_key.get("participant")
                    or msg_key.get("author")
                    or (full_msg.get("participant") if isinstance(full_msg, dict) else "")
                    or (full_msg.get("author") if isinstance(full_msg, dict) else "")
                    or (full_msg.get("from") if isinstance(full_msg, dict) and not str(full_msg.get("from", "")).endswith("@g.us") else "")
                    or msg_key.get("remoteJidAlt")
                    or ""
                )
            participant = _resolve_to_lid_if_available(raw)
            if ":" in participant and "@" in participant:
                # Strip device port suffix (e.g. 62655318482954:94@lid -> 62655318482954@lid)
                u, s = participant.split("@", 1)
                if ":" in u:
                    participant = f"{u.split(':', 1)[0]}@{s}"
        if participant:
            return f"{prefix}_{chat}_{msg_id}_{participant}"
        return f"{prefix}_{chat}_{msg_id}"

    def send_reaction(self, remote_jid: str, msg_key: dict, emoji: str) -> bool:
        """Send a reaction to a message via the WPPConnect Server API."""
        # Resolve the @lid chat to its phone JID the same way deletes do, so the
        # serialized id matches the chat WPPConnect actually has loaded.
        lid_jid = getattr(self, "_phone_to_lid", {}).get(remote_jid, "")
        if lid_jid:
            remote_jid = lid_jid
        url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/react-message"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }
        payload = {
            "msgId": self._serialize_msg_id(remote_jid, msg_key),
            "reaction": emoji
        }
        reaction_signature = (str(msg_key.get("id", "")), emoji)
        with self._pending_own_reactions_lock:
            now = time.monotonic()
            for key, created_at in list(self._pending_own_reactions.items()):
                if now - created_at > 60:
                    self._pending_own_reactions.pop(key, None)
            self._pending_own_reactions[reaction_signature] = now
        try:
            response = api_post(
                url,
                json=payload,
                headers=headers,
                timeout=15,
                # Every reaction, not just a status like: setting or removing
                # the same emoji on the same message is idempotent, so a
                # duplicate arrival is a no-op rather than a second delivery —
                # which is what makes retrying safe here and not in the send_*
                # paths above. Needed because the local WPPConnect process
                # drops stale keep-alive sockets under media-processing load,
                # and a reaction lost that way is silent.
                retry_stale_socket=True,
            )
            if response.status_code not in (200, 201):
                # 6000 chars (not 500, and not the original 1500) —
                # deviceController.ts's reactMessage includes a real error
                # message + stack trace in the body (see its own comment on
                # why a bare `error: e` used to serialize down to almost
                # nothing), and on a failed Bootloader component search it
                # also appends a sample of scanned component names for
                # diagnosis. 1500 chars cut that sample off alphabetically
                # before it ever reached a name starting with "WAWebSta..."
                # or "WAWebReact...", live-confirmed 2026-09-20.
                logging.error("[send_reaction] HTTP %s: %s",
                              response.status_code, response.text[:6000])
                with self._pending_own_reactions_lock:
                    self._pending_own_reactions.pop(reaction_signature, None)
                return False
            return True
        except Exception as exc:
            logging.error("[send_reaction] exception: %s", exc)
            with self._pending_own_reactions_lock:
                self._pending_own_reactions.pop(reaction_signature, None)
            return False

    def pin_message(self, remote_jid: str, msg_key: dict, pin: bool = True) -> bool:
        """Pin/unpin a single message in a chat via the WPPConnect Server API.

        This is WhatsApp's own message-pin feature (visible to every other
        participant) — a separate custom `/pin-message` endpoint added in
        api_patches/, since @wppconnect-team/wppconnect only wraps pinning a
        whole *chat* (see pin_chat/unpin_chat below), not an individual
        message within it.
        """
        lid_jid = getattr(self, "_phone_to_lid", {}).get(remote_jid, "")
        if lid_jid:
            remote_jid = lid_jid
        url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/pin-message"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }
        payload = {
            "messageId": self._serialize_msg_id(remote_jid, msg_key),
            "pin": pin,
        }
        try:
            response = api_post(url, json=payload, headers=headers, timeout=15)
            if response.status_code not in (200, 201):
                logging.error("[pin_message] HTTP %s: %s",
                              response.status_code, response.text[:500])
                return False
            return True
        except Exception as exc:
            logging.error("[pin_message] exception: %s", exc)
            return False

    def _on_message_sent(self, local_id: str, audio_path: str = None, real_id: str = None, remote_jid: str = None, quote_lost: bool = False):
        """
        Called on the main thread after a queued message is successfully sent.
        Updates the UI status label and cleans up any temporary audio file.
        real_id is the WhatsApp message ID returned by the API; it replaces the
        local UUID in the virtual message so playback can find the message in the DB.
        quote_lost is True when the original send-with-quote failed and the message
        went out as a plain send instead (see send_text_message's fallback) — the
        UI must then drop the reply contextInfo so the row stops reading as a reply.
        """
        # The user deleted this message while it was still pending, but by then
        # the worker had already taken it off the queue (message_queue.cancel()
        # returned False, so it never even got the cancel flag) and the send went
        # out anyway — its real ID only arrives here, after the row is gone.
        # Marking a message the user just deleted as sent is not an option; the
        # cancellation is completed instead.
        if self._is_cancelled_send(local_id):
            self._on_cancelled_message_delivered(
                local_id, real_id, remote_jid, audio_path, quote_lost
            )
            return
        import time as _time
        logging.info("[VOICE_TIMING] _on_message_sent — message LEFT pending state. local_id=%s real_id=%s",
                     local_id, real_id)
        if real_id and remote_jid:
            def _bg_update_db():
                try:
                    self.db.update_message_id(remote_jid, local_id, real_id)
                except Exception as e:
                    logging.error("[_on_message_sent] failed to update database message ID: %s", e)
            threading.Thread(target=_bg_update_db, daemon=True).start()

        # Save or copy the local audio copy under the real ID *before* calling _mark_message_sent
        # to prevent background media sync from downloading a file we already have.
        if audio_path and os.path.isfile(audio_path):
            if real_id and isinstance(real_id, str):
                try:
                    voice_messages_dir = data_path("voice_messages")
                    os.makedirs(voice_messages_dir, exist_ok=True)
                    local_audio_path = os.path.join(voice_messages_dir, f"{local_id}.msv")
                    real_audio_path = os.path.join(voice_messages_dir, f"{real_id}.msv")
                    
                    if os.path.isfile(local_audio_path):
                        import shutil
                        shutil.copy2(local_audio_path, real_audio_path)
                    else:
                        with open(audio_path, "rb") as f:
                            wav_data = f.read()
                        with open(real_audio_path, "wb") as f_out:
                            f_out.write(encrypt(wav_data, self.key))
                except Exception as e:
                    print(f"[_on_message_sent] error saving sent audio locally: {e}")
            try:
                os.unlink(audio_path)
            except Exception:
                pass

        # For audio messages, play the sent sound HERE — not only inside
        # _mark_message_sent — because the upload can take several seconds.
        # If the user navigated to another conversation before the API
        # confirmed the send, local_id is no longer in _sorted_messages and
        # _mark_message_sent would silently skip the sound.  Playing it here
        # guarantees the "tac" always fires the moment the API says "sent".
        if audio_path and hasattr(self, "message_sent_sound"):
            self.message_sent_sound.play()

        if hasattr(self, "conversations_panel"):
            self.conversations_panel._mark_message_sent(local_id, real_id=real_id, quote_lost=quote_lost)

    def _on_cancelled_message_delivered(self, local_id: str, real_id: str = None,
                                        remote_jid: str = None, audio_path: str = None,
                                        quote_lost: bool = False,
                                        ambiguous: bool = False):
        """Called on the main thread when a message the user cancelled turned out
        to have reached WhatsApp anyway.

        Cancelling an in-flight send is best-effort by nature: none of the send_*
        calls can be interrupted once the request is on the wire, so the queue
        can learn "delivered" for a message whose row the UI already removed.
        Silently dropping it is the one outcome that is a lie — the message IS on
        the recipient's phone. So the cancellation is completed the only way it
        still can be, by revoking it for everyone, and if that revoke fails the
        row comes back rather than the app claiming a deletion that never
        happened (see ConversationsPanel.complete_cancelled_message_delivery()).
        """
        logging.warning(
            "[_on_cancelled_message_delivered] local_id=%s real_id=%s jid=%s ambiguous=%s",
            local_id, real_id, remote_jid, ambiguous,
        )
        # Same temporary WAV cleanup _on_message_sent() does — the recording's
        # permanent copy is voice_messages/<local_id>.msv, which the panel
        # renames or deletes depending on how the revoke goes.
        self._discard_temp_recording(audio_path)
        if not hasattr(self, "conversations_panel"):
            # Nothing else can revoke it: the panel owns both the record this
            # message is still held by and the API call. Loud, because it means
            # a message the user cancelled stays on the recipient's phone.
            logging.error(
                "[_on_cancelled_message_delivered] no conversations panel — %s "
                "(id=%s) stays delivered and unrevoked", local_id, real_id,
            )
            return
        self.conversations_panel.complete_cancelled_message_delivery(
            local_id, real_id, remote_jid, quote_lost, ambiguous
        )

    def _on_cancelled_message_dropped(self, local_id: str, audio_path: str = None):
        """Called on the main thread when a cancelled message is confirmed to
        have never reached WhatsApp.

        The queue answers every message it had already claimed when the cancel
        arrived (cancel() returned False), because the panel deliberately keeps
        that message's record around as the anchor the WebSocket echo binds to.
        This is what releases it when there will be no echo.
        """
        logging.info("[_on_cancelled_message_dropped] local_id=%s", local_id)
        self._discard_temp_recording(audio_path)
        if hasattr(self, "conversations_panel"):
            self.conversations_panel.discard_cancelled_message(local_id)

    def _is_cancelled_send(self, local_id: str) -> bool:
        """True while a message the user deleted mid-send is still being resolved.

        Every one of the queue's three ordinary outcome callbacks has to ask,
        and each of them completes the cancellation instead of doing its usual
        job. Two reasons, and the second is the one that bites: they report on a
        row that is already gone (a send-failed dialog for an upload abandoned
        on purpose, "envio não confirmado" spoken for a deleted message), and
        they are the ONLY thing that still runs for a cancel that landed after
        their own branch took the message off the queue — from that moment
        cancel() answers False and the panel is holding this message's record,
        waiting for a report that no worker will make.
        """
        panel = getattr(self, "conversations_panel", None)
        return panel is not None and panel._is_cancelled_pending(local_id)

    @staticmethod
    def _discard_temp_recording(audio_path: str):
        """Delete the temporary WAV a voice recording was sent from."""
        if audio_path and os.path.isfile(audio_path):
            try:
                os.unlink(audio_path)
            except Exception:
                pass

    def _on_message_unconfirmed(self, local_id: str):
        """Called when a send timed out and its outcome cannot be determined.

        The message is NOT retried (that is what used to flood conversations
        with duplicates), and WhatsApp Web may still deliver it on reconnect —
        in which case the echo arriving over the WebSocket resolves this very
        bubble. Until that happens the row carries an explicit "unconfirmed"
        status: it used to be left in "sending" forever, which a user reasonably
        reads as sent, and that is exactly how a message that never left the
        browser passed for delivered.
        """
        if self._is_cancelled_send(local_id):
            # Deleted mid-send, and the outcome is unknown — which is neither
            # "it never went out" (that is why this branch does not retry) nor
            # "it did". ambiguous=True carries that third state through: the row
            # goes back marked unconfirmed rather than sending, so it stops
            # being a pending anchor the next message's echo would match first.
            self._on_cancelled_message_delivered(local_id, ambiguous=True)
            return
        if hasattr(self, "conversations_panel"):
            self.conversations_panel._mark_message_unconfirmed(local_id)
        if not self.background_mode:
            self.output(self.i18n.t("message_send_unconfirmed"), interrupt=False)

    def _on_message_failed(self, local_id: str, error: str = "", show_dialog: bool = False):
        """
        Called on the main thread after a queued message exhausts all retries.
        Marks the virtual message as failed in the UI and, for media attachments,
        shows an error dialog so the user knows the file was not delivered.
        """
        if self._is_cancelled_send(local_id):
            # Deleted mid-send, and the send then definitively failed: no error
            # dialog for an upload the user abandoned on purpose, and the record
            # the panel is holding for the echo is released — no echo is coming.
            self._on_cancelled_message_dropped(local_id)
            return
        if hasattr(self, "conversations_panel"):
            self.conversations_panel._mark_message_failed(local_id)
        # _mark_message_failed() only updates the row inside the open
        # conversation — the chat-list preview reads the same underlying
        # record via _last_msg_preview()/_counts_as_last_message() (which
        # now excludes a failed send), but the list widget itself was never
        # told to re-render, so the stale preview sat there until the user
        # happened to reopen the conversation for an unrelated reason.
        self._schedule_set_chats()
        if show_dialog:
            self.error_sound.play()
            detail = error[:300] if error else self.i18n.t("error").format(app_name=self.app_name)
            wx.MessageBox(
                self.i18n.t("media_send_failed").format(error=detail),
                self.i18n.t("error").format(app_name=self.app_name),
                wx.OK | wx.ICON_ERROR,
            )

    # ── Media / contact attachments ───────────────────────────────────────────

    def send_media_attachment(
        self, remote_jid: str, file_path: str,
        media_type: str, caption: str = "", quoted: dict = None,
        progress_callback=None, upload_id: str = "",
        custom_filename: str = "",
    ) -> bool:
        """
        Upload a file as a media message via multipart/form-data.
        Avoids base64 encoding so payloads stay at true file size
        (no 33 % overhead, no JSON body-size limit).
        media_type: 'image' | 'video' | 'audio' | 'document'
        """
        # Canonical destination: @lid when known, else the @c.us phone form —
        # see _resolve_jid_for_send's docstring for why @lid has to win here.
        remote_jid = self._resolve_jid_for_send(remote_jid)
        is_lid_target = remote_jid.endswith("@lid")
        logging.info("[send_media] destination resolved to %s (isLid=%s)", remote_jid, is_lid_target)
        import mimetypes
        from core.audio_transcode import prepare_audio_for_whatsapp
        from core.video_transcode import prepare_video_for_whatsapp
        from core.multipart_stream import StreamingMultipartBody
        try:
            file_size = os.path.getsize(file_path)
        except Exception as exc:
            logging.error("[send_media] failed to stat file %s: %s", file_path, exc)
            return False
        # WhatsApp allows 2 GB for documents and 1 GB for photos/videos/audio.
        # Kept in step with ConversationsPanel._on_send_attachment()'s own
        # pre-check — see the comment there for the four gates this is one of.
        MAX_FILE_SIZE = (
            2 * 1024 * 1024 * 1024 if media_type == "document"
            else 1 * 1024 * 1024 * 1024
        )
        if file_size > MAX_FILE_SIZE:
            limit_gb = MAX_FILE_SIZE // (1024 ** 3)
            err_msg = (
                f"File size ({file_size / (1024*1024):.1f} MB) exceeds the "
                f"{limit_gb} GB WhatsApp attachment limit for {media_type}."
            )
            logging.error("[send_media] %s", err_msg)
            return {"ok": False, "error": err_msg, "retry": False}
        mime = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
        filename = custom_filename or os.path.basename(file_path)
        upload_path = file_path
        converted_audio_path = None
        converted_video_path = None
        if media_type == "audio":
            prepared = prepare_audio_for_whatsapp(self._find_api_ffmpeg(), file_path)
            if prepared is None:
                return {
                    "ok": False,
                    "error": self.i18n.t("media_audio_convert_failed"),
                    "retry": False,
                }
            upload_path, mime = prepared
            if upload_path != file_path:
                converted_audio_path = upload_path
                filename = custom_filename or os.path.basename(upload_path)
                file_size = os.path.getsize(upload_path)
        elif media_type == "video":
            # WhatsApp's own pipeline expects H.264/AAC MP4 — anything else
            # (.mkv, .webm, .avi, ...) used to go straight to WPPConnect's
            # upload as-is and fail server-side with a bare 500. See
            # core/video_transcode.py's own docstring for the full reasoning
            # (mirrors the audio branch just above).
            prepared = prepare_video_for_whatsapp(self._find_api_ffmpeg(), file_path)
            if prepared is None:
                return {
                    "ok": False,
                    "error": "Não foi possível converter o vídeo para um formato aceito pelo WhatsApp.",
                    "retry": False,
                }
            upload_path, mime = prepared
            if upload_path != file_path:
                converted_video_path = upload_path
                filename = custom_filename or os.path.basename(upload_path)
                file_size = os.path.getsize(upload_path)
        url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/send-file"
        # Authorization only — Content-Type is set automatically by requests
        # when using files= (multipart/form-data with correct boundary).
        headers = {"Authorization": f"Bearer {self.token}"}
        phone_val = remote_jid
        if phone_val.endswith("@s.whatsapp.net"):
            phone_val = phone_val.replace("@s.whatsapp.net", "@c.us")
        # Force WPPConnect to send the chosen WhatsApp message type instead of
        # its mimetype-based "auto-detect", which otherwise sends e.g. an .mp3
        # picked from the "Document" menu as a playable audio message, or a
        # .jpg/.png as a photo, regardless of what the user actually selected.
        _wpp_type = {
            "image": "image", "video": "video",
            "audio": "audio", "document": "document",
        }.get(media_type, "document")
        data = {
            "filename": filename,
            "caption":  caption,
            "type":     _wpp_type,
            "uploadId": upload_id,
        }
        if quoted:
            quoted_id = self._serialize_quoted_id(quoted, fallback_jid=remote_jid)
            if quoted_id:
                data["quotedMessageId"] = quoted_id
        # WPPConnect accepts attachments up to 1 GB. Its WinZapp patch moves
        # large payloads into Chromium in bounded chunks instead of one
        # oversized CDP argument, for document/image/video/audio alike.
        if file_size > MAX_FILE_SIZE:
            err_msg = f"File size ({file_size / (1024*1024):.1f} MB) exceeds the 1 GB WhatsApp attachment limit."
            logging.error("[send_media] %s", err_msg)
            for converted_path in (converted_audio_path, converted_video_path):
                if converted_path:
                    try:
                        os.unlink(converted_path)
                    except OSError:
                        pass
            return {"ok": False, "error": err_msg, "retry": False}
        timeout = max(120, file_size // (100 * 1024))
        timeout = min(timeout, 1800)

        def _post(dest: str):
            """POST the upload to `dest`, reopening the file (a retry cannot
            reuse the already-consumed handle).

            multipart/form-data has no booleans: requests serializes False as
            the string "False", which is *truthy* in JavaScript — so a plain
            `"isGroup": False` made WPPConnect's statusConnection/contactToArray
            treat every media send as a group send and rewrite the destination
            to `<digits>@g.us`. Only send these flags when they are true.
            """
            post_data = dict(data, phone=[dest])
            if dest.endswith("@g.us"):
                post_data["isGroup"] = "true"
            if dest.endswith("@lid"):
                post_data["isLid"] = "true"
            body = StreamingMultipartBody(
                file_path=upload_path,
                filename=filename,
                mime_type=mime,
                fields=post_data,
                progress_callback=progress_callback,
            )
            stream_headers = dict(headers, **{"Content-Type": body.content_type})
            return api_post(
                url,
                headers=stream_headers,
                data=body,
                timeout=timeout,
            )

        try:
            r = _post(phone_val)
            if r.status_code not in (200, 201):
                # Same legacy fallback as the text/audio paths: a @lid
                # destination that WhatsApp Web refuses gets one attempt on the
                # old @c.us address, unless the session is simply disconnected.
                fb_phone = self._legacy_phone_for_send(remote_jid) if is_lid_target else ""
                if fb_phone and not self._check_wa_connection_closed(r):
                    logging.warning("[send_media] @lid destination %s refused (HTTP %s) — retrying with legacy %s",
                                    remote_jid, r.status_code, fb_phone)
                    r = _post(fb_phone)
                    if r.status_code in (200, 201):
                        logging.info("[send_media] legacy retry with %s succeeded", fb_phone)
            if r.status_code in (200, 201):
                try:
                    return accepted_message_id(r.json())
                except (ValueError, TypeError) as exc:
                    logging.error("[send_media] invalid success response for %s: %s", filename, exc)
                    # Switch on the machine-readable reason, not on the English
                    # text: only a negative ACK means WhatsApp examined the file
                    # and refused it, which is what media_unsupported_error says.
                    if getattr(exc, "reason", "") == "rejected":
                        error_text = self.i18n.t("media_unsupported_error")
                    else:
                        error_text = self.i18n.t("send_not_confirmed_error")
                    return {"ok": False, "error": error_text, "retry": False}
            err = f"HTTP {r.status_code}"
            inner_error_name = ""
            try:
                body = r.json()
                inner_error = body.get("error")
                inner_message = ""
                if isinstance(inner_error, dict):
                    inner_error_name = inner_error.get("name") or ""
                    inner_message = inner_error.get("message") or ""
                elif isinstance(inner_error, str):
                    inner_message = inner_error
                detail = inner_message or (body.get("message") or "")
                if detail:
                    err = f"{err}: {detail}"
            except Exception:
                if r.text:
                    err = f"{err}: {r.text[:200]}"
            logging.error("[send_media] %s for %s (%s, %.1f MB): %s",
                          err, remote_jid, filename, file_size / (1024*1024), r.text[:300])
            # 5xx responses are transient server/puppeteer hiccups — notably the
            # WPPConnect "ProtocolError: Promise was collected" that strikes large
            # uploads under load. Retry those; treat 4xx as permanent.
            if self._check_wa_connection_closed(r):
                return {"ok": False, "error": err, "retry": False, "disconnected": True}
            # MediaUnsupportedError (observed as "video loaded with duration
            # but no dims") means WhatsApp Web's own upload pipeline rejected
            # THIS specific file as malformed/unprocessable — retrying sends
            # the exact same bytes again and fails identically every time, so
            # unlike a generic 5xx this is never transient. Skip the retry
            # loop and surface a clear reason immediately instead of the
            # generic "Erro ao enviar a mensagem" after 4 wasted attempts.
            if inner_error_name == "MediaUnsupportedError":
                return {
                    "ok": False,
                    "error": self.i18n.t("media_unsupported_error"),
                    "retry": False,
                }
            retryable = r.status_code >= 500
            return {"ok": False, "error": err, "retry": retryable}
        except MessageCancelled:
            # Must propagate, not be classified as a send failure: this is
            # what MessageQueue's own `except MessageCancelled:` handler
            # (message_queue.py) is waiting for to drop the message
            # without retrying or reporting a failure to the user.
            raise
        except Exception as exc:
            return self._classify_send_exception(exc, "send_media")
        finally:
            if converted_audio_path:
                try:
                    os.unlink(converted_audio_path)
                except OSError:
                    pass
            if converted_video_path:
                try:
                    os.unlink(converted_video_path)
                except OSError:
                    pass

    def on_media_upload_progress(self, upload_id: str, progress, stage: str = ""):
        if hasattr(self, "conversations_panel"):
            self.conversations_panel.update_media_upload_progress(
                upload_id, progress, stage)

    def on_media_download_progress(self, progress_id: str, progress: float):
        """Server-side CDN download progress, keyed by the id we sent with the
        request — the message's own `key.id` (see the `progressId` the
        get-media request carries), which is what the panel matches rows by."""
        if hasattr(self, "conversations_panel"):
            self.conversations_panel.update_message_download_progress(
                progress_id, progress)

    def send_contact_attachment(self, remote_jid: str, contact_info: dict,
                                quoted: dict = None):
        """Send a contact card as an attachment.

        Returns whatever the send contract makes of the response, exactly like
        its three siblings: the confirmed message id, or the failure dict
        MessageQueue turns into a translated reason, or None when the request
        itself never got an answer worth reading.
        """
        # Canonical destination: @lid when known, else the @c.us phone form —
        # see _resolve_jid_for_send's docstring for why @lid has to win here.
        remote_jid = self._resolve_jid_for_send(remote_jid)
        is_lid_target = remote_jid.endswith("@lid")
        logging.info("[send_contact_attachment] destination resolved to %s (isLid=%s)", remote_jid, is_lid_target)
        is_group = remote_jid.endswith("@g.us")
        if is_group:
            remote_jid = remote_jid.split("@")[0]
        name = contact_info.get("pushName") or ""
        jid = contact_info.get("remoteJid", "")
        phone_raw = jid.split("@")[0] if "@" in jid else jid
        url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/contact-vcard"
        payload = {
            "phone":       [remote_jid],
            "isGroup":     is_group,
            "isLid":       is_lid_target,
            "contactsId":  [f"{phone_raw}@c.us"],
        }
        if quoted:
            quoted_id = self._serialize_quoted_id(quoted, fallback_jid=remote_jid)
            if quoted_id:
                payload["quotedMessageId"] = quoted_id
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

        def _parse(r):
            """Confirm the card the same way the other three send paths do.

            Returns the same failure dict as its siblings rather than None, so
            MessageQueue reports an unconfirmable contact card with a
            translated reason instead of a blank one — and, like them, never
            retries a card that may already be on the recipient's screen.
            """
            try:
                return accepted_message_id(r.json())
            except (ValueError, TypeError) as exc:
                logging.error("[send_contact_attachment] invalid success response: %s", exc)
                return {
                    "ok": False,
                    "error": self.i18n.t("send_not_confirmed_error"),
                    "retry": False,
                }

        try:
            r = api_post(url, json=payload, headers=headers, timeout=15)
            if r.status_code in (200, 201):
                return _parse(r)
            # Same legacy fallback as the other send paths.
            fb_phone = self._legacy_phone_for_send(remote_jid) if is_lid_target else ""
            if fb_phone and not self._check_wa_connection_closed(r):
                logging.warning("[send_contact_attachment] @lid destination %s refused (HTTP %s) — retrying with legacy %s",
                                remote_jid, r.status_code, fb_phone)
                payload["phone"] = [fb_phone]
                payload["isLid"] = False
                r = api_post(url, json=payload, headers=headers, timeout=15)
                if r.status_code in (200, 201):
                    logging.info("[send_contact_attachment] legacy retry with %s succeeded", fb_phone)
                    return _parse(r)
            logging.error("[send_contact_attachment] HTTP %s for %s: %s", r.status_code, remote_jid, r.text[:300])
            return None
        except Exception as exc:
            logging.error("[send_contact_attachment] exception for %s: %s", remote_jid, exc)
            return None
