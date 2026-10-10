"""Optional sender-prefix suppression in message rows and open-chat speech.

Only identity labels pass here, never bodies, chat titles or toasts. Unnamed
senders have no substitute label; real names and the user's own prefix stay.
"""
from core.utils import contact_dedup_key, is_phone_like


def hide_unnamed_sender_numbers_enabled(settings) -> bool:
    ui = settings.get("user_interface") if isinstance(settings, dict) else None
    return isinstance(ui, dict) and ui.get("hide_unnamed_sender_numbers") is True


def message_sender_label(label, msg, main_window, i18n) -> str:
    """Leave names intact and drop missing/phone fallback labels on opt-in.

    The existing name resolvers own PN/LID bridges and saved-contact priority.
    A valid message pushName still wins when an older chat record returned a
    phone fallback before the resolver reached that name.
    """
    if not hide_unnamed_sender_numbers_enabled(getattr(main_window, "settings", None)):
        return label
    if (msg.get("key") or {}).get("fromMe"):
        return label
    missing = not label or label == i18n.t("unnamed_participant")
    if not missing and not is_phone_like(label) and not label.endswith(
        ("@s.whatsapp.net", "@c.us", "@lid", "@g.us")
    ):
        return label
    push = (msg.get("pushName") or "").strip()
    if push and not is_phone_like(push) and not getattr(
        main_window, "_is_bad_contact_name", lambda _name: False
    )(push):
        return push
    return ""


def _same_sender(left, right, main_window) -> bool:
    normalize = main_window._normalize_jid
    left, right = normalize(left or ""), normalize(right or "")
    if not left or not right or left.endswith("@g.us") or right.endswith("@g.us"):
        return False
    if main_window._chat_jids_equivalent(left, right):
        return True
    bridge = getattr(main_window, "_lid_to_phone", {})
    left = normalize(bridge.get(left, left)) if left.endswith("@lid") else left
    right = normalize(bridge.get(right, right)) if right.endswith("@lid") else right
    # Dedup also handles Brazilian 8/9-digit variants, but its domainless
    # result must never equate an unresolved LID's digits with a phone.
    if left.endswith("@s.whatsapp.net") and right.endswith("@s.whatsapp.net"):
        return contact_dedup_key(main_window, left) == contact_dedup_key(main_window, right)
    return False


def quoted_sender_label(label, ctx, msg, panel) -> str:
    """Apply the same row-only choice to the reply's sender prefix.

    Read a loaded quote's name only when its author matches the context. Never
    reuse the replying message's participant to identify a different sender.
    Other uses of _get_quoted_sender (reply UI, message data) stay unchanged.
    """
    mw = panel.main_window
    if not hide_unnamed_sender_numbers_enabled(getattr(mw, "settings", None)):
        return label
    conv = getattr(panel, "conversation", None) or {}
    remote = conv.get("remoteJid") or (msg.get("key") or {}).get("remoteJid", "")
    participant = ctx.get("participant", "")
    quoted = {"key": {"remoteJid": remote, "participant": participant}}
    stanza = ctx.get("stanzaId")
    original = next((m for m in getattr(panel, "_sorted_messages", ())
                     if isinstance(m, dict) and stanza
                     and (m.get("key") or {}).get("id") == stanza), None)
    if original:
        original_key = original.get("key") or {}
        author = (getattr(mw, "my_jid", "") if original_key.get("fromMe") else
                  original_key.get("participant") or original.get("participant")
                  or original_key.get("remoteJid", ""))
        if not participant or _same_sender(participant, author, mw):
            quoted = {**original, "key": dict(original_key)}
    if not participant and original is None:
        if "_quotedFromMe" in ctx:
            quoted["key"]["fromMe"] = ctx["_quotedFromMe"]
        elif not remote.endswith("@g.us"):
            quoted["key"]["fromMe"] = not (msg.get("key") or {}).get("fromMe")
    if participant and getattr(mw, "_is_self_jid", lambda _jid: False)(participant):
        quoted["key"]["fromMe"] = True
    return message_sender_label(label, quoted, mw, mw.i18n)
