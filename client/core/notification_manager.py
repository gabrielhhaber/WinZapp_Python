"""
WinZapp Notification Manager
-----------------------------
Sends native Windows 11 toast notifications for incoming messages.

Design decisions:
  - Uses windows-toasts (WinRT-based) for proper Win11 notifications.
  - Sets toast.audio = silent=True and plays message_background.ogg manually
    so the default Windows notification sound is suppressed.
  - Uses InteractableWindowsToaster so the quick-reply TextBox works and
    activations (click / reply) call back into the running Python process.
  - Only ever ONE WinZapp toast exists at a time: every notification uses the
    same tag/group and the previous one is explicitly removed before the new
    one is shown.  Windows otherwise *queues* banners it could not display —
    with the screen off or during Focus Assist they all pop in sequence the
    moment the machine wakes, which is what users hit as "a pile of
    notifications appears at once when I touch the keyboard".  The worker also
    coalesces anything still waiting in its own queue, so a burst of messages
    produces one current banner rather than a backlog to sit through.
  - SetCurrentProcessExplicitAppUserModelID("WinZapp") (called in main.py)
    ensures that Windows displays "WinZapp" — not the exe filename — as the
    sender inside the notification.
  - A single long-lived worker thread owns the Toaster object so that WinRT
    COM objects are always created and used on the same thread, avoiding the
    [WinError -2147417842] RPC_E_WRONG_THREAD error that occurs when the
    toaster is created in one thread and show_toast() called in another.
"""

import logging
import os
import queue
import re as _re
import sys
import threading
import time
import uuid
import wx
from app_paths import _is_frozen
from core.i18n import I18n
from core.message_queue import PendingMessage
from core.utils import looks_like_binary_blob


def _notif_duration(seconds) -> str:
    """Format seconds as M:SS or H:MM:SS for notification body."""
    try:
        s = int(seconds or 0)
    except (TypeError, ValueError):
        return "0:00"
    h   = s // 3600
    m   = (s % 3600) // 60
    sec = s % 60
    if h > 0:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


def _notif_filesize(size_bytes, decimal_separator=".") -> str:
    """Format bytes as human-readable size."""
    try:
        size = int(size_bytes or 0)
    except (TypeError, ValueError):
        return ""
    sep = decimal_separator
    if size < 1024:
        return f"{size} B"
    elif size < 1024 ** 2:
        return f"{size / 1024:.1f}".replace(".", sep) + " KB"
    else:
        return f"{size / (1024 ** 2):.2f}".replace(".", sep) + " MB"


def _extract_context_info(msg: dict) -> dict:
    """Return the first non-empty contextInfo found anywhere in the message."""
    ctx = msg.get("contextInfo")
    if ctx and isinstance(ctx, dict):
        return ctx
    msg_obj = msg.get("message") or {}
    for sub_key in ("extendedTextMessage", "audioMessage", "imageMessage",
                    "videoMessage", "documentMessage", "stickerMessage",
                    "contactMessage", "locationMessage"):
        sub = msg_obj.get(sub_key)
        if sub and isinstance(sub, dict):
            ctx = sub.get("contextInfo")
            if ctx and isinstance(ctx, dict):
                return ctx
    return {}


def _extract_msg_text(msg: dict) -> str:
    """Return the plain text body of a message (used for @all pattern detection)."""
    msg_obj = msg.get("message") or {}
    text = msg_obj.get("conversation") or ""
    if not text:
        text = (msg_obj.get("extendedTextMessage") or {}).get("text") or ""
    if looks_like_binary_blob(text):
        return ""
    return text


def _build_notif_prefix(msg: dict, main_window, i18n) -> str:
    """
    Return the appropriate context prefix for a toast notification title:
      notif_replied_to_you  — this message quotes one of the user's own messages
      notif_mentioned_you   — the user is directly @mentioned
      notif_mentioned_all   — @all / @todos / @everyone was used
      ''                    — no special context
    Only the first matching condition is returned (reply takes priority).
    """
    ctx = _extract_context_info(msg)
    if not ctx:
        return ""

    my_jid   = getattr(main_window, "my_jid", "")
    my_phone = my_jid.split("@")[0] if my_jid else ""

    # ── Reply to me? ──────────────────────────────────────────────────────────
    stanza_id = ctx.get("stanzaId", "")
    if stanza_id:
        # participant = JID of whoever sent the quoted message
        participant = (ctx.get("participant") or ctx.get("quotedParticipant") or "")
        remote_jid  = msg.get("key", {}).get("remoteJid", "")
        is_group    = remote_jid.endswith("@g.us")

        if my_jid:
            p_phone = participant.split("@")[0] if participant else ""
            replied_to_me = (
                participant == my_jid               # exact JID match
                or (p_phone and p_phone == my_phone)  # phone prefix match
                # In a private chat, participant may be absent — any reply comes from them to us
                or (not participant and not is_group)
            )
            if replied_to_me:
                return i18n.t("notif_replied_to_you")

    # ── Mentions ──────────────────────────────────────────────────────────────
    mentioned = ctx.get("mentionedJid") or []
    if not mentioned:
        return ""

    # @all / @todos / @everyone — check text for the keyword
    text = _extract_msg_text(msg)
    if _re.search(r"@(all|todos|everyone)\b", text, _re.IGNORECASE):
        return i18n.t("notif_mentioned_all")

    # Direct @mention of the user
    if my_jid and my_phone:
        for m_jid in mentioned:
            if m_jid == my_jid or m_jid.split("@")[0] == my_phone:
                return i18n.t("notif_mentioned_you")

    return ""


def format_notification_body(msg: dict, main_window, i18n) -> str:
    """
    Build a compact notification body for any supported message type.
    Mirrors the display logic in ConversationsPanel._get_message_content
    but uses compact duration (M:SS) and avoids i18n verbose duration strings.
    """
    msg_type = msg.get("messageType", "conversation")
    msg_obj  = msg.get("message") or {}
    sep      = i18n.t("decimal_separator")

    if not isinstance(msg_obj, dict):
        return i18n.t("notif_unsupported")

    # ── Text ──────────────────────────────────────────────────────────────────
    if msg_type == "conversation":
        text = msg_obj.get("conversation") or ""
        return i18n.t("notif_unsupported") if looks_like_binary_blob(text) else text

    if msg_type == "extendedTextMessage":
        ext  = msg_obj.get("extendedTextMessage") or {}
        text = ext.get("text") or ""
        if looks_like_binary_blob(text):
            return i18n.t("notif_unsupported")
        mentioned = (
            (msg.get("contextInfo") or {}).get("mentionedJid")
            or (msg_obj.get("contextInfo") or {}).get("mentionedJid")
            or ext.get("contextInfo", {}).get("mentionedJid")
            or []
        )
        for jid in mentioned:
            if main_window and main_window._is_self_jid(jid):
                name = "eu"
            else:
                name = _resolve_participant_name(jid, "", main_window)
            
            lid_local = jid.rsplit("@", 1)[0]
            _lid_map = getattr(main_window, "_lid_to_phone", {})
            phone_jid = _lid_map.get(jid, "") if jid.endswith("@lid") else ""
            phone = phone_jid.split("@")[0] if phone_jid else jid.split("@")[0]
            
            placeholder = None
            if f"@{lid_local}" in text:
                placeholder = lid_local
            elif phone and f"@{phone}" in text:
                placeholder = phone
                
            if not placeholder:
                continue
                
            if name and name != placeholder and name != jid:
                text = text.replace(f"@{placeholder}", f"@{name}", 1)
        return text

    # ── Audio ─────────────────────────────────────────────────────────────────
    if msg_type == "audioMessage":
        audio = msg_obj.get("audioMessage") or {}
        dur   = _notif_duration(audio.get("seconds"))
        return f"{i18n.t('notif_voice_message')} ({dur})"

    # ── Video ─────────────────────────────────────────────────────────────────
    if msg_type == "videoMessage":
        video = msg_obj.get("videoMessage") or {}
        if video.get("gifPlayback"):
            return i18n.t("sticker")
        dur = _notif_duration(video.get("seconds"))
        if dur and dur != "0:00":
            return f"{i18n.t('video')} ({dur})"
        return i18n.t("video")

    # ── Image ─────────────────────────────────────────────────────────────────
    if msg_type == "imageMessage":
        img     = msg_obj.get("imageMessage") or {}
        caption = (img.get("caption") or "").strip()
        if caption:
            return f"{i18n.t('photo')}: {caption}"
        return i18n.t("photo_no_caption")

    # ── Document ──────────────────────────────────────────────────────────────
    if msg_type == "documentMessage":
        doc      = msg_obj.get("documentMessage") or {}
        filename = doc.get("fileName") or doc.get("title") or i18n.t("document")
        size_str = _notif_filesize(doc.get("fileLength"), sep)
        if size_str:
            return f"{i18n.t('document')}: {filename}, {size_str}"
        return f"{i18n.t('document')}: {filename}"

    # ── Sticker ───────────────────────────────────────────────────────────────
    if msg_type == "stickerMessage":
        return i18n.t("sticker")

    # ── Contact ───────────────────────────────────────────────────────────────
    if msg_type == "contactMessage":
        contact = msg_obj.get("contactMessage") or {}
        name = contact.get("displayName") or ""
        vcard = contact.get("vcard") or ""
        
        if not name or "BEGIN:VCARD" in name:
            vcard_to_parse = name if "BEGIN:VCARD" in name else vcard
            parsed_name = ""
            for line in vcard_to_parse.splitlines():
                if line.startswith("FN:"):
                    parsed_name = line[3:].strip()
                    break
            if parsed_name:
                name = parsed_name
            else:
                name = "Desconhecido"

        return i18n.t("contact_message").format(name=name)

    if msg_type == "contactsArrayMessage":
        arr = msg_obj.get("contactsArrayMessage") or {}
        contacts = arr.get("contacts") or []
        return i18n.t("contact_message").format(name=f"{len(contacts)} contatos")

    # ── Location ──────────────────────────────────────────────────────────────
    if msg_type in ("locationMessage", "liveLocationMessage"):
        return i18n.t("notif_location")

    # ── Poll ──────────────────────────────────────────────────────────────────
    if msg_type in ("pollCreationMessage", "pollCreationMessageV2", "pollCreationMessageV3", "pollUpdateMessage"):
        poll = (
            msg_obj.get("pollCreationMessage")
            or msg_obj.get("pollCreationMessageV2")
            or msg_obj.get("pollCreationMessageV3")
            or {}
        )
        name = (poll.get("name") or "").strip()
        return i18n.t("notif_poll").format(name=name) if name else i18n.t("notif_poll_no_name")

    # ── Buttons / list (message with selectable options) ────────────────────────
    if msg_type == "buttonsMessage":
        btns_msg = msg_obj.get("buttonsMessage") or {}
        return (btns_msg.get("contentText") or btns_msg.get("text") or "").strip() or i18n.t("interactive_message")

    if msg_type == "listMessage":
        list_msg = msg_obj.get("listMessage") or {}
        return (list_msg.get("title") or list_msg.get("description") or "").strip() or i18n.t("interactive_message")

    if msg_type == "templateMessage":
        return i18n.t("notif_template")

    if msg_type == "interactiveMessage":
        inter = msg_obj.get("interactiveMessage") or {}
        body = (inter.get("body") or {}).get("text", "")
        return body or i18n.t("interactive_message")

    if msg_type == "buttonsResponseMessage":
        btn = msg_obj.get("buttonsResponseMessage") or {}
        return btn.get("selectedDisplayText") or i18n.t("interactive_reply")

    if msg_type == "listResponseMessage":
        lst = msg_obj.get("listResponseMessage") or {}
        return (
            lst.get("title")
            or (lst.get("singleSelectReply") or {}).get("selectedRowId")
            or i18n.t("list_reply")
        )

    # ── Reaction ──────────────────────────────────────────────────────────────
    if msg_type == "reactionMessage":
        reaction = msg_obj.get("reactionMessage") or {}
        emoji    = reaction.get("text") or ""
        return i18n.t("notif_reaction").format(emoji=emoji)

    # ── Fallback ──────────────────────────────────────────────────────────────
    # Logged so a future report of a raw, untranslated messageType showing
    # up in a notification (e.g. a view-once/ephemeral audio wrapper, which
    # arrives under a DIFFERENT outer messageType than "audioMessage"
    # itself and isn't unwrapped by any branch above) can be traced to the
    # exact type instead of only reproducing "some notification looks wrong".
    logging.info("[format_notification_body] unhandled messageType=%r", msg_type)
    return i18n.t("notif_unsupported")


def _resolve_participant_name(p_jid: str, push_name: str, main_window) -> str:
    """
    Return the best display name for a group participant.
    Priority: saved contact/chat name → WhatsApp pushName → phone number.
    """
    from core.utils import format_number, is_phone_like
    if p_jid:
        candidates = [p_jid]
        local = p_jid.rsplit("@", 1)[0]
        lid_to_phone = getattr(main_window, "_lid_to_phone", {})
        if p_jid.endswith("@lid"):
            phone = lid_to_phone.get(p_jid, "")
            if phone:
                candidates.append(phone)
                candidates.append(phone.rsplit("@", 1)[0] + "@c.us")
        elif p_jid.endswith("@s.whatsapp.net"):
            candidates.append(local + "@c.us")
            lid = getattr(main_window, "_phone_to_lid", {}).get(p_jid, "")
            if lid:
                candidates.append(lid)
        elif p_jid.endswith("@c.us"):
            candidates.append(local + "@s.whatsapp.net")

        for cjid in candidates:
            p_chat = (
                main_window.chats.get(cjid)
                or main_window.contacts.get(cjid)
                # Falls back to the Brazilian 8/9-digit interchangeable form
                # (e.g. a contact saved as 551199999999 while the message
                # arrives as 5511999999999) — a plain dict lookup above
                # misses that and used to show the raw number instead of the
                # saved contact name for these participants.
                or main_window._get_contact_tolerant(cjid)
                or {"remoteJid": cjid}
            )
            saved = main_window._resolve_contact_name(p_chat)
            if saved:
                return saved

    if push_name and not is_phone_like(push_name):
        return push_name

    ppm = getattr(main_window, "_presence_pushname_map", {})
    if p_jid:
        for cjid in candidates:
            pname = (ppm.get(cjid) or "").strip()
            if pname and not pname.isdigit() and not is_phone_like(pname):
                return pname

    if p_jid:
        if not p_jid.endswith("@lid"):
            return format_number(p_jid)
        phone = lid_to_phone.get(p_jid, "")
        if phone:
            return format_number(phone)
    return ""


def format_foreground_sender(msg: dict, main_window, i18n) -> str:
    """
    Sender label for foreground (scenario 1 — active conversation).
    Private chat  → sender name only.
    Group message → participant name only (no group name).
    """
    from core.utils import format_number
    key        = msg.get("key", {})
    remote_jid = key.get("remoteJid", "")
    push_name  = msg.get("pushName", "")

    if remote_jid.endswith("@g.us"):
        p_jid = key.get("participant") or msg.get("participant") or ""
        if (not p_jid or p_jid.endswith("@g.us")) and push_name and push_name.isdigit():
            p_jid = f"{push_name}@s.whatsapp.net"
        return _resolve_participant_name(p_jid, push_name, main_window) or i18n.t("unnamed_participant") or "Participante sem nome"

    chat = main_window.chats.get(remote_jid) or {"remoteJid": remote_jid}
    return (
        main_window._resolve_contact_name(chat)
        or _resolve_participant_name(remote_jid, push_name, main_window)
        or format_number(remote_jid)
    )


def format_toast_unread_suffix(unread_count: int, i18n) -> str:
    """Return the '✉️ N mensagens não lidas' line WhatsApp appends to the end
    of its own toast notifications, for the conversation the message arrived
    in. Toast-only — never shown in the foreground sound/AO2 announcements.

    Returns "" for a genuinely-zero count — callers treat an empty string as
    "omit this line" (see _dispatch()'s `if unread_suffix else` check).
    `count <= 1` used to fold 0 into the same "1 unread" singular branch as
    a real single unread message, so this line showed up on every toast
    that had a synced (but possibly zero) unread count — reported live as
    a reaction to your own already-read message still announcing "1 não
    lida" with no unread message actually behind it.
    """
    count = int(unread_count or 0)
    if count <= 0:
        return ""
    if count == 1:
        text = i18n.t("unread_sep_singular")
    else:
        text = i18n.t("unread_sep_plural").format(count=count)
    return f"✉️ {text}"


def format_notification_title(msg: dict, main_window, i18n) -> str:
    """
    Build the notification title for a toast notification.

    Private chat  → [prefix] sender name
    Group message → [prefix] 'Participant em GroupName'

    prefix is one of notif_replied_to_you / notif_mentioned_you /
    notif_mentioned_all, or absent when none of those conditions apply.
    """
    from core.utils import format_number

    key        = msg.get("key", {})
    remote_jid = key.get("remoteJid", "")
    push_name  = msg.get("pushName", "")

    if remote_jid.endswith("@g.us"):
        chat = main_window.chats.get(remote_jid) or {"remoteJid": remote_jid}
        # _resolve_contact_name() always returns None for a group (its own
        # docstring: "Groups are skipped") — calling it here was dead code.
        # chat.get("name", "") alone also misses WPPConnect's raw chat shape,
        # which nests a group's real name under groupMetadata.subject rather
        # than a flat "name"/"subject" key (see _group_name_from_chat_dict()'s
        # own docstring) — routine right after a fresh pairing, before
        # WhatsApp Web finishes hydrating group metadata. Without this, a
        # notification could say "Grupo sem nome" for a group whose name the
        # chat list itself already displays correctly via the same fallback.
        group_name = (
            chat.get("name", "")
            or main_window._group_name_from_chat_dict(chat)
            or chat.get("pushName", "")
        )
        if not group_name or not group_name.strip():
            group_name = i18n.t("unknown_group") or "Grupo sem nome"

        # Resolve participant name — saved name takes priority over pushName
        p_jid = key.get("participant") or msg.get("participant") or ""
        if (not p_jid or p_jid.endswith("@g.us")) and push_name and push_name.isdigit():
            p_jid = f"{push_name}@s.whatsapp.net"
        participant_name = _resolve_participant_name(p_jid, push_name, main_window)
        if not participant_name:
            participant_name = i18n.t("unnamed_participant") or "Participante sem nome"
        base = i18n.t("notif_in_group").format(
            participant=participant_name, group=group_name
        )
    else:
        chat = main_window.chats.get(remote_jid) or {"remoteJid": remote_jid}
        # format_number(remote_jid) must NEVER run on a bare, unresolved
        # @lid: a LID's digits are an internal WhatsApp identifier, not a
        # phone number, and formatting them as one produced notifications
        # reading "Nova mensagem de 133041125077153" — meaningless to the
        # user and not even a real phone number they could recognize.
        # _resolve_participant_name() already refuses to do this (an
        # unresolved @lid makes it return "" rather than format the raw
        # digits), so only fall through to format_number() for a JID that
        # is actually phone-shaped; otherwise show "Contato sem nome",
        # exactly like the chat list already does when it can't name a chat.
        can_format_as_phone = (
            remote_jid.endswith(("@s.whatsapp.net", "@c.us"))
            or remote_jid in getattr(main_window, "_lid_to_phone", {})
        )
        base = (
            main_window._resolve_contact_name(chat)
            or _resolve_participant_name(remote_jid, push_name, main_window)
            or (format_number(remote_jid) if can_format_as_phone else "")
            or i18n.t("unknown_contact")
        )

    prefix = _build_notif_prefix(msg, main_window, i18n)
    title = f"{prefix} {base}" if prefix else base

    # Multi-account: a toast is an ASYNC alert whose source (which account) is
    # otherwise ambiguous, so lead with the account name (plan Zad 4.4 / GPT r4
    # #10 — prefix async alerts, NOT every message). Single/legacy account has
    # account_name == "WinZapp" (or unset) → no prefix. With a single connected
    # account the name is redundant → no prefix either.
    acc_name = getattr(main_window, "account_name", None)
    if (acc_name and acc_name != "WinZapp"
            and getattr(main_window, "_is_multi_account", lambda: False)()):
        title = f"[{acc_name}] {title}"
    return title


class NotificationManager:
    """Manages Windows 11 toast notifications for incoming WinZapp messages."""

    APP_ID    = "WinZapp"
    TOAST_TAG = "winzapp_active"
    TOAST_GRP = "winzapp_msgs"
    # ToastDuration.Short keeps a banner on screen for ~5s. A little safety
    # margin past that (Windows' own dismiss timing isn't perfectly exact)
    # before we stop bothering to clear it first — see _last_shown_at.
    _TOAST_LIKELY_GONE_SECONDS = 8

    def __init__(self, main_window):
        self.main_window = main_window
        self.i18n = I18n(main_window)
        self.i18n.get_language()
        self._toaster      = None
        self._interactable = False
        # Last toast handed to Windows, kept so it can be removed before the
        # next one is shown (see _clear_active_toasts).  Touched only by the
        # worker thread.
        self._last_toast   = None
        # monotonic() timestamp of the last show_toast() call, or None.  Lets
        # _dispatch() skip _clear_active_toasts()'s blocking WinRT/COM
        # round-trip when the previous toast has almost certainly already
        # auto-dismissed on its own (see _TOAST_LIKELY_GONE_SECONDS) — that
        # round-trip was sitting in front of every single show_toast() call
        # even when there was nothing to clear, which is real, measurable
        # latency between "message arrived" and the banner actually
        # appearing on screen. Touched only by the worker thread.
        self._last_shown_at = None
        # Register the AUMID on the main thread before starting the worker so
        # the registry key exists before any WinRT notifier is created.
        self._register_aumid_registry()
        # Queue consumed by a single long-lived worker thread so that the
        # WinRT/COM toaster object is always created and used in the same
        # thread (avoids [WinError -2147417842] RPC_E_WRONG_THREAD).
        self._queue  = queue.Queue()
        self._thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._thread.start()

    # ── Worker thread (owns the toaster for its whole lifetime) ───────────────

    def _worker_loop(self):
        """Long-lived thread: creates the toaster once, then drains the queue."""
        self._setup_toaster()
        while True:
            item = self._queue.get()
            if item is None:
                break
            item, dropped = self._coalesce_pending(item)
            if item is None:
                break
            if dropped:
                print(f"[NotificationManager] coalesced {dropped} queued toast(s)")
            title, body, remote_jid, msg_key = item
            self._dispatch(title, body, remote_jid, msg_key)

    def _coalesce_pending(self, item):
        """Collapse everything already queued down to the newest notification.

        If more notifications piled up while the toaster was busy (a reconnect
        delivering a burst of messages, say), only the most recent one is worth
        showing — the rest would queue up behind it and be displayed one after
        another, which is the behaviour being fixed.  Anything dropped here is
        still reflected in the app's unread state and chat list.

        Returns ``(item, dropped_count)``; item is None when a shutdown signal
        was found in the queue.
        """
        dropped = 0
        while True:
            try:
                newer = self._queue.get_nowait()
            except queue.Empty:
                return item, dropped
            if newer is None:
                # Shutdown signal arrived mid-burst: honour it.
                return None, dropped
            item = newer
            dropped += 1

    @staticmethod
    def _outer_exe_path() -> str:
        """Return the user-facing exe path (outer exe, not the Nuitka temp extraction)."""
        if sys.argv and sys.argv[0]:
            return os.path.abspath(sys.argv[0])
        return sys.executable

    @staticmethod
    def _register_aumid_registry():
        """Write HKCU\\SOFTWARE\\Classes\\AppUserModelId\\WinZapp to the registry.

        The windows-toasts library (and WinRT in general) requires the AUMID to be
        registered in the user-hive registry before a toast can be sent from an
        unpackaged app.  Installed builds have this key written by the NSIS
        installer; portable/zip builds have no installer, so we register it here
        at runtime.  Writing the same values twice is harmless.

        We use sys.argv[0] (not sys.executable) for the IconUri because in Nuitka
        onefile mode sys.executable points to the inner extracted exe in a temp
        directory, while sys.argv[0] always points to the user-visible outer exe.
        """
        try:
            import winreg
            key_path = r"SOFTWARE\Classes\AppUserModelId\WinZapp"
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path,
                                    0, winreg.KEY_WRITE) as key:
                winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, "WinZapp")
                if _is_frozen():
                    exe = sys.argv[0] if sys.argv and sys.argv[0] else sys.executable
                    winreg.SetValueEx(key, "IconUri", 0, winreg.REG_SZ,
                                      os.path.abspath(exe))
        except Exception as e:
            print(f"[NotificationManager] AUMID registry write failed: {e}")

    def _setup_toaster(self):
        # Ensure the AUMID is registered in the current-user registry so that
        # portable builds (which have no NSIS installer) can send toasts.
        self._register_aumid_registry()

        # Build a prioritised list of AUMID candidates to try.
        # Installed build: registered AUMID "WinZapp" first, outer exe as fallback.
        # Dev build / portable: outer exe path (always available to Windows).
        # _is_frozen() handles both PyInstaller (sys.frozen) and Nuitka (__compiled__).
        # Use sys.argv[0] as the exe fallback — in Nuitka onefile sys.executable
        # points to a temp-dir extraction; sys.argv[0] is the user-visible path.
        if _is_frozen():
            outer_exe = self._outer_exe_path()
            candidates = [self.APP_ID, outer_exe]
        else:
            candidates = [sys.executable]

        for app_id in candidates:
            # Try interactable first (supports inline reply text box).
            try:
                from windows_toasts import InteractableWindowsToaster
                # Pass notifierAUMID explicitly — without it the library defaults
                # to cmd.exe's AUMID, which causes Windows to label the
                # notification as "Prompt de Comando" / "Command Prompt".
                self._toaster      = InteractableWindowsToaster(app_id, notifierAUMID=app_id)
                self._interactable = True
                print(f"[NotificationManager] interactable toaster ready (app_id={app_id!r})")
                return
            except Exception as e:
                print(f"[NotificationManager] InteractableWindowsToaster({app_id!r}) failed: {e}")
            # Fall back to basic toaster (no inline reply).
            try:
                from windows_toasts import WindowsToaster
                self._toaster = WindowsToaster(app_id)
                print(f"[NotificationManager] basic toaster ready (app_id={app_id!r})")
                return
            except Exception as e:
                print(f"[NotificationManager] WindowsToaster({app_id!r}) failed: {e}")

        print("[NotificationManager] toast system unavailable — all candidates failed")

    def _clear_active_toasts(self):
        """Remove any WinZapp toast currently displayed or pending.

        Called from the worker thread only, right before showing a new toast,
        so exactly one notification of ours exists at any moment.  Every step is
        best-effort: the WinRT history calls raise when there is nothing to
        remove (or when the notification platform is unavailable), and that must
        never stop the new notification from being shown.
        """
        toaster = self._toaster
        if toaster is None:
            return
        try:
            toaster.remove_toast_group(self.TOAST_GRP)
            return
        except Exception:
            pass
        # Older/basic toasters may not expose group removal — fall back to
        # removing the single tag we always reuse.
        try:
            last = self._last_toast
            if last is not None:
                toaster.remove_toast(last)
        except Exception:
            pass

    # Curated quick-reaction set for the toast's "Reagir" action — mirrors
    # the emojis WhatsApp's own official client offers from a notification
    # (a subset of the fuller picker in ConversationsPanel._on_menu_react,
    # which doesn't fit/isn't needed in a one-line toast dropdown).
    # Windows toasts realistically support ~5 actions total (buttons +
    # inputs combined) before the shell starts clipping/misbehaving — kept
    # to 2 so "1 reply input + 1 send button + N reaction buttons" has
    # comfortable margin.
    _TOAST_REACTIONS = ["👍", "❤️"]

    def _dispatch(self, title: str, body: str, remote_jid: str, msg_key: dict = None):
        if not self._toaster:
            return
        try:
            from windows_toasts import (
                Toast, ToastInputTextBox, ToastButton, ToastAudio, ToastDuration,
            )
            from core.utils import effective_unread_count

            self.i18n.get_language()
            reply_hint = self.i18n.t("notif_reply_hint")

            chat = self.main_window.chats.get(remote_jid)
            # A chat WinZapp just discovered via this very live message (see
            # on_new_message()) starts its local unreadCount at an assumed 0
            # and counts up from there — the phone may already have a much
            # larger real backlog for it that no chat-list sync has fetched
            # yet. Showing that assumed-low count ("1 unread"/"2 unread") is
            # actively misleading, not just imprecise, so the badge is
            # omitted entirely until a real sync backs the number (see
            # get_remote_chats(), which clears this flag). It's still only a
            # snapshot taken right before this toast is shown — a burst still
            # arriving after that (each message bumping the real count
            # further) can leave the toast reporting fewer unread than what
            # Alt+3/the chat itself shows moments later; there is no single
            # later point to re-sample from once the banner is already up.
            unread_suffix = ""
            if chat is not None and not chat.get("_unread_count_unsynced"):
                unread_suffix = format_toast_unread_suffix(effective_unread_count(chat), self.i18n)

            # Fire the custom sound BEFORE any of the WinRT/COM work below
            # (clearing the previous toast, show_toast() itself). Both of
            # those are genuine round-trips into the Windows notification
            # platform with their own, mostly-uncontrollable rendering
            # latency (Action Center staging, banner animation) — queuing the
            # sound after them used to add that latency on top of whatever
            # the audio engine itself needs, widening the gap users noticed
            # between hearing the notification and seeing the banner. Not
            # perfect — Windows' own toast pipeline still isn't instant — but
            # this removes the part of the delay that was our own doing.
            wx.CallAfter(self._play_sound, remote_jid)

            # Clear whatever WinZapp notification is currently on screen (or
            # waiting to be shown) before posting the new one.  This is what
            # makes a new message *replace* the current banner instead of
            # lining up behind it: Windows holds undisplayed toasts and pops
            # them in sequence once it can, so with the screen off a burst of
            # messages turned into a burst of banners on wake.
            #
            # It also restores the fresh-banner behaviour that a shared tag
            # alone does not give: reusing a tag while the toast is still
            # visible makes Windows treat show_toast() as an in-place update,
            # which neither resets the on-screen timer nor re-raises the
            # banner.  Removing first, then showing, always pops.
            #
            # BUT: remove_toast_group() is a blocking WinRT/COM round-trip,
            # and it used to run before EVERY show_toast() call regardless of
            # whether anything was actually still showing — sitting directly
            # in front of the call that puts the banner on screen, on every
            # single notification, for no benefit the vast majority of the
            # time (messages usually arrive more than 5s apart). Skip it once
            # the previous toast has almost certainly auto-dismissed on its
            # own; still run it for the case it actually exists to fix — a
            # burst of messages arriving within the same few seconds.
            since_last = (
                None if self._last_shown_at is None
                else time.monotonic() - self._last_shown_at
            )
            if since_last is None or since_last < self._TOAST_LIKELY_GONE_SECONDS:
                self._clear_active_toasts()

            toast          = Toast()
            toast.tag      = self.TOAST_TAG
            toast.group    = self.TOAST_GRP
            toast.duration = ToastDuration.Short   # ~5 seconds on screen
            # Each toast.text_fields entry becomes its own <text> element in
            # the notification's XML — an embedded "\n" inside a single entry
            # does NOT force a line break (Windows' toast renderer collapses
            # it), which is why the "✉️ N não lidas" line used to run
            # straight into the message body instead of appearing below it.
            # A separate list entry is a real second line.
            toast.text_fields = [title, body, unread_suffix] if unread_suffix else [title, body]
            toast.audio    = ToastAudio(silent=True)  # suppress Windows sound

            jid_snapshot = remote_jid
            msg_key_snapshot = msg_key

            if self._interactable:
                # `caption` (-> the toast XML's `title` attribute) renders as
                # its own visible line ABOVE the field — setting it to the
                # same text as `placeholder` (-> `placeHolderContent`, the
                # text shown inside the empty box, and what Windows' toast/
                # Action Center accessibility tree exposes as the edit
                # control's accessible Name) doubled up "Responder..." on
                # screen once the real accessibility fix — a non-empty
                # placeholder — was in place. Leave caption unset; the
                # "Enviar resposta" button right next to it already gives
                # sighted users a visible label for what the field is for.
                reply_box = ToastInputTextBox("reply_box", "", reply_hint)
                toast.AddInput(reply_box)
                # Without an explicit action tied to it (relatedInput), the
                # reply box had no real button in the toast's XML at all —
                # nothing for Tab to land on, just whatever bare Enter-to-
                # submit behaviour Windows happens to give a lone input.
                toast.AddAction(ToastButton(
                    content=self.i18n.t("notif_send_reply"),
                    arguments="do_reply",
                    relatedInput=reply_box,
                    tooltip=self.i18n.t("notif_send_reply"),
                ))

                # One button per emoji rather than a second input (a
                # selection dropdown) alongside the reply text box: combining
                # two different input controls in the same toast was the
                # prime suspect for a live regression reported right after
                # this shipped — notifications came back with no accessible
                # content at all (NVDA fell back to reading the generic "New
                # notification" wrapper) and Windows' own default sound
                # instead of the app's, i.e. the whole custom toast getting
                # rejected/replaced wholesale rather than one control simply
                # rendering oddly. A row of plain ToastButtons is the
                # standard, widely-supported "quick react" shape (same idea
                # as the reply button above) and keeps the action count (1
                # input + 1 button + N reaction buttons) comfortably under
                # the ~5 actions a toast can show.
                if msg_key_snapshot:
                    for emoji in self._TOAST_REACTIONS:
                        toast.AddAction(ToastButton(
                            content=emoji,
                            arguments=f"do_react:{emoji}",
                            tooltip=self.i18n.t("notif_react_with").format(emoji=emoji),
                        ))

                def on_activated(event):
                    args   = getattr(event, "arguments", "") or ""
                    inputs = getattr(event, "inputs", {}) or {}
                    if args.startswith("do_react:"):
                        emoji = args.split(":", 1)[1]
                        if emoji and msg_key_snapshot:
                            wx.CallAfter(self._do_react, jid_snapshot, msg_key_snapshot, emoji)
                        return
                    reply_text = (inputs.get("reply_box") or "").strip()
                    if reply_text:
                        wx.CallAfter(self._do_reply, jid_snapshot, reply_text, msg_key_snapshot)
                    else:
                        wx.CallAfter(self._do_open, jid_snapshot)

                toast.on_activated = on_activated
            else:
                def on_activated(event):
                    wx.CallAfter(self._do_open, jid_snapshot)

                toast.on_activated = on_activated

            self._toaster.show_toast(toast)
            self._last_toast    = toast
            self._last_shown_at = time.monotonic()

        except Exception as e:
            print(f"[NotificationManager] send_worker error: {e}")

    # ── Public API ────────────────────────────────────────────────────────────

    def send(self, title: str, body: str, remote_jid: str, msg_key: dict = None):
        """Enqueue a toast notification (non-blocking).

        msg_key: the triggering message's `key` dict, if available — lets the
        toast's "Reagir" action react to that specific message instead of
        needing to look one up later.
        """
        self._queue.put((title, body, remote_jid, msg_key))

    # ── Callbacks (called on wx main thread via CallAfter) ────────────────────

    def _play_sound(self, remote_jid: str = ""):
        if hasattr(self.main_window, "play_background_notification_sound"):
            self.main_window.play_background_notification_sound(remote_jid)
        elif hasattr(self.main_window, "message_background_sound"):
            self.main_window.message_background_sound.play()

    def _do_reply(self, jid: str, text: str, msg_key: dict = None):
        if not text:
            return
        local_id = str(uuid.uuid4())
        # quoted needs a "message"-shaped dict wrapping the key — matches
        # what _serialize_quoted_id()/send_text_message() (main.py) expect;
        # msg_key alone (a bare key.id/remoteJid/fromMe dict) previously went
        # straight into PendingMessage with no "quoted" at all, so replying
        # from a toast always sent a brand new message instead of an actual
        # WhatsApp reply.
        quoted = {"key": msg_key} if msg_key else None
        pm = PendingMessage(local_id=local_id, jid=jid, text=text, quoted=quoted)
        self.main_window.message_queue.enqueue(pm)

    def _do_react(self, jid: str, msg_key: dict, emoji: str):
        threading.Thread(
            target=self.main_window.send_reaction,
            args=(jid, msg_key, emoji),
            daemon=True,
        ).start()

    def _do_open(self, jid: str):
        self.main_window.restore_window()
        # navigate_to_conversation captures unreadCount BEFORE starting its own
        # mark-as-read thread, so we must not start a competing thread here —
        # that race condition would zero unreadCount before the snapshot is taken
        # and suppress the unread separator.
        wx.CallAfter(self.main_window.navigate_to_conversation_jid, jid)

    def refresh_language(self):
        self.i18n.get_language()
