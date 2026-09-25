"""Meta AI chat helpers: which JIDs are the assistant, and its terms state.

WhatsApp refuses every message to Meta AI (the send comes back ``ack=-1``,
``ackErrorCode 488``) until the account has accepted Meta AI's own terms of
service (user notice 20250502). WhatsApp Web's own send function fails the
same way, so this is not a send bug -- the user has to accept first. Measured
over CDP on a live session, 2026-09-25: ``TosManager.getState('20250502')``
was ``NOT_ACCEPTED`` on the account that got the 488.
"""

import re

# Meta AI's terms of service, the page WhatsApp's own prompt links to.
META_AI_TERMS_URL = "https://www.facebook.com/legal/ai-terms"

STATE_ACCEPTED = "accepted"
STATE_NOT_ACCEPTED = "not_accepted"
STATE_UNKNOWN = "unknown"

# WhatsApp's assistant is the phone-number account 13135550002 (its chat is
# still keyed that way in the store, though messages to it are addressed to
# 867051314767696@bot). The whole 1313555000x block is reserved for WhatsApp's
# own bots, which is also how WhatsApp Web's Wid.isBot() recognises them.
_META_AI_USER = re.compile(r"^1313555000\d$")


def is_meta_ai_jid(jid) -> bool:
    """Whether *jid* is a chat with WhatsApp's Meta AI assistant."""
    if not isinstance(jid, str) or "@" not in jid:
        return False
    user, server = jid.split("@", 1)
    user = user.split(":", 1)[0]
    if server == "bot":
        return True
    return server in ("s.whatsapp.net", "c.us") and bool(_META_AI_USER.match(user))


def terms_state(body) -> str:
    """Read the Node ``/meta-ai-terms`` response.

    Anything that is not an explicit "not accepted" answer counts as unknown or
    accepted, so a probe that fails never blocks a send that might have worked.
    """
    result = body.get("response") if isinstance(body, dict) else None
    state = str((result or {}).get("state") or "").upper()
    if state == "ACCEPTED":
        return STATE_ACCEPTED
    if state == "NOT_ACCEPTED":
        return STATE_NOT_ACCEPTED
    return STATE_UNKNOWN


def rich_response_text(wpp_msg) -> str:
    """The text of one of Meta AI's replies.

    A reply is not a ``chat`` message: it arrives as ``type: "rich_response"``
    with no ``body``, and its words live in ``richResponse.fragments`` (one
    ``{"type": "Text", "text": ...}`` per block) and, in the newer layout, in
    ``unifiedResponse.sections[].view_model.primitive.text`` (markdown).
    Measured over CDP on 2026-09-25 -- WPPConnect's serializer
    (WAPI.getMessageById) carries both, and without this the reply showed as a
    row with only the sender's name and the time.

    Fragments win because they are already plain text; the unified layout is
    the fallback when a build sends only that one. A reply with neither (an
    image-only answer, say) gives "" and is left to the caller.
    """
    if not isinstance(wpp_msg, dict):
        return ""
    rich = wpp_msg.get("richResponse")
    fragments = rich.get("fragments") if isinstance(rich, dict) else None
    texts = [
        fragment["text"].strip()
        for fragment in fragments or ()
        if isinstance(fragment, dict) and isinstance(fragment.get("text"), str)
        and fragment["text"].strip()
    ]
    if texts:
        return "\n\n".join(texts)
    unified = wpp_msg.get("unifiedResponse")
    sections = unified.get("sections") if isinstance(unified, dict) else None
    for section in sections or ():
        model = section.get("view_model") if isinstance(section, dict) else None
        primitive = model.get("primitive") if isinstance(model, dict) else None
        text = primitive.get("text") if isinstance(primitive, dict) else None
        if isinstance(text, str) and text.strip():
            texts.append(text.strip())
    return "\n\n".join(texts)
