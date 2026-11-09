"""wa-js patch: posting to a channel finds the channel's chat.

Every post to a channel the account runs (text, photo, video, poll) failed on
WPPConnect with HTTP 500 before reaching WhatsApp:

    Error: Chat not found in ChatStore for <id>@newsletter
        at t.find -> assertFindChat -> sendRawMessage

wa-js's `chat.find()` asks WAWebFindChatAction.findOrCreateLatestChat() for
the chat and then looks it up again in ChatStore. WhatsApp Web keeps a
channel's chat in the newsletter collection instead (NewsletterStore in
wa-js): the action finds it, ChatStore never holds it, and the second lookup
throws. Measured over CDP on 2026-11-08 (wa-js 4.7.2): ChatStore held 484
chats and no channel; the action answered the channel's chat with
`created: false`, the very object NewsletterStore.get() returns, with
`isNewsletter` and its newsletterMetadata. wa-js's own `assertGetChat()`
already reads channels from NewsletterStore, and sendRawMessage() has a
newsletter branch (sendNewsletterMessageJob) for that chat: only `find()`
did not know.

Fix: when ChatStore does not hold a channel's chat, take it from
NewsletterStore. Any other chat is looked up exactly as before. The same
lookup in openChatBottom() and openChatFromUnread() gets the same fallback.

With the chat found, a text post or a poll still failed:

    TypeError: Cannot read properties of undefined (reading 'newsletterMetadata')
        at P (WAWebNewsletterSendMsgAction) <- sendRawMessage

wa-js binds `sendNewsletterMessageJob` to the first module exporting
`sendNewsletterMessageJob` OR `sendNewsletterMessage`. WAWebNewsletterSend
MsgAction exports the second: the UI's send action, which takes
`{chat, type, msg}` and enqueues on the chat, not the job wa-js calls with
`{type, msgData, msg, newsletterJid}`. Measured on the same page: the bound
function was the action's, and only WAWebNewsletterSendMessageJob exports the
job. Photos and videos went out (wa-js sends them another way); text and
polls did not. Fix: bind the job by its module's name, as the chat lookup
patch does for WAWebFindChatAction.

Both are matched by structure, not by the minifier's names, like the other
wa-js patches (wppconnect_wa_js_patch.py); idempotent.
"""
import os
import re

from core.wppconnect_wa_js_patch import (
    STATUS_ALREADY, STATUS_APPLIED, STATUS_NO_MATCH, WA_JS_BUNDLE_PARTS,
)

# const u=o.ChatStore.get(s.chat.id);if(!u)throw new Error(`Chat not found in ChatStore for
_LOOKUP = re.compile(
    r"const (?P<chat>[\w$]+)=(?P<store>[\w$]+)\.ChatStore\.get\((?P<found>[\w$]+)\.chat\.id\);"
    r"(?P<tail>if\(!(?P=chat)\)throw new Error\(`Chat not found in ChatStore for )"
)
_PATCHED = re.compile(
    r"const (?P<chat>[\w$]+)=(?P<store>[\w$]+)\.ChatStore\.get\((?P<found>[\w$]+)\.chat\.id\)"
    r"\|\|\((?P=found)\.chat\.id\.isNewsletter&&(?P=found)\.chat\.id\.isNewsletter\(\)"
    r"\?(?P=store)\.NewsletterStore\.get\((?P=found)\.chat\.id\):void 0\);"
    r"if\(!(?P=chat)\)throw new Error\(`Chat not found in ChatStore for "
)


# {sendNewsletterMessageJob:[...]},e=>e.sendNewsletterMessageJob||e.sendNewsletterMessage)
_JOB_HEAD = (
    r'(?P<head>\{sendNewsletterMessageJob:\["sendNewsletterMessageJob","sendNewsletterMessage"\]\},)'
)
_AMBIGUOUS_JOB = re.compile(
    _JOB_HEAD + r'(?P<m>[\w$]+)=>(?P=m)\.sendNewsletterMessageJob\|\|(?P=m)\.sendNewsletterMessage(?=\))'
)
_NAMED_JOB = re.compile(
    _JOB_HEAD + r'\([\w$]+,(?P<name>[\w$]+)\)=>"WAWebNewsletterSendMessageJob"===(?P=name)(?=\))'
)


def _with_fallback(match):
    chat, store, found = match.group("chat"), match.group("store"), match.group("found")
    return (f"const {chat}={store}.ChatStore.get({found}.chat.id)"
            f"||({found}.chat.id.isNewsletter&&{found}.chat.id.isNewsletter()"
            f"?{store}.NewsletterStore.get({found}.chat.id):void 0);" + match.group("tail"))


def patch_newsletter_chat_source(content: str) -> tuple[str, str]:
    """(*content* with the channel fallback in every ChatStore re-lookup and
    the send job bound by module name, status). Each fix applies on its own
    (an upstream bundle may have fixed one): STATUS_APPLIED when either
    changed, STATUS_ALREADY when nothing was left to change, STATUS_NO_MATCH
    when neither is there in either form."""
    patched, lookups = _LOOKUP.subn(_with_fallback, content)
    patched, jobs = _AMBIGUOUS_JOB.subn(
        lambda m: m.group("head") + '(module,name)=>"WAWebNewsletterSendMessageJob"===name', patched)
    if lookups or jobs:
        return patched, STATUS_APPLIED
    if _PATCHED.search(content) or _NAMED_JOB.search(content):
        return content, STATUS_ALREADY
    return content, STATUS_NO_MATCH


def patch_newsletter_chat_bundle(api_dir: str) -> tuple[bool, str]:
    """Patch the wa-js bundle under *api_dir* (the outer client/api)."""
    path = os.path.join(api_dir, *WA_JS_BUNDLE_PARTS)
    if not os.path.isfile(path):
        return False, "wppconnect-wa.js not found — skipping the channel chat patch."
    with open(path, encoding="utf-8", newline="") as stream:
        content = stream.read()
    patched, status = patch_newsletter_chat_source(content)
    if status == STATUS_APPLIED:
        with open(path, "w", encoding="utf-8", newline="") as stream:
            stream.write(patched)
        return True, ("Patched wppconnect-wa.js — posting to a channel finds the channel's chat "
                      "and its send job.")
    if status == STATUS_ALREADY:
        return True, "wppconnect-wa.js channel chat patch already applied."
    return False, ("wppconnect-wa.js: neither chat.find()'s ChatStore lookup nor the channel "
                   "send job binding matched the expected upstream source — skipping.")
