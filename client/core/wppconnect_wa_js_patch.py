"""Patch for @wppconnect/wa-js's compiled bundle (dist/wppconnect-wa.js, the
script wppconnect injects into the WhatsApp Web page) — every message to Meta
AI failing with HTTP 500 before it is sent (WinZapp issue #365):

    TypeError: Cannot read properties of undefined (reading 'personaId')
        at prepareRawMessage (wppconnect-wa.js)

Bug: for a bot chat, prepareRawMessage() fills the outgoing message with

    botPersonaId: BotProfileStore.get(chat.id?.toString())!.personaId

The `!` only silences the TypeScript compiler. BotProfileStore is a cache
WhatsApp fills on its own schedule (history sync, a bot-profile notification,
the copy restored from IndexedDB), never on demand: `get()` is a plain map
lookup, and the collection's `find()` refuses the phone-number form of the
assistant (`13135550002@c.us`, the id its chat is keyed by) because it "only
supports FBID bots". An account whose default bot profile has not arrived yet
therefore cannot send to Meta AI at all, and retrying never helps.

Measured over CDP on a live session, 2026-10-04 (wa-js 4.6.1, WhatsApp Web
2.3000.1049220475): with the profile present prepareRawMessage() returned
botPersonaId "867051314767696"; with it taken out of the collection the very
same call threw the TypeError above, and `find()` on the phone-number wid
rejected without putting anything back.

Fix: do what WhatsApp Web itself does. Its own send path reads
`BotProfileCollection.get(wid)?.personaId` and leaves the field out when
there is no profile, so the patched lookup falls back to an empty object and
the message goes out without a persona id instead of not going out.

The lookup has had the same shape since wa-js 3.23.3 and differs between
builds only in the minifier's temporary (`g` up to 4.4.3, `m` from 4.5.0), so
it is matched by structure instead of by one literal per build: node_modules
is not moved by a WinZapp update, and the patch runs on every launch against
whatever bundle is on disk.

Unlike the four wppconnect_*_layer_patch modules this file is not inside
@wppconnect-team/wppconnect, so the three call sites (setup_api.py,
build_api.py and ApiSetupDialog._apply_node_modules_patches()) hand
patch_wa_js_bundle() the outer api directory and it finds the bundle itself.
"""

import os
import re

WA_JS_BUNDLE_PARTS = ("node_modules", "@wppconnect", "wa-js", "dist", "wppconnect-wa.js")

# `<store>.BotProfileStore.get(null===(<tmp>=<chat>.id)||void 0===<tmp>?void 0:<tmp>.toString())`
_PROFILE_LOOKUP = (
    r"[\w$]+\.BotProfileStore\.get\("
    r"null===\((?P<tmp>[\w$]+)=[\w$]+\.id\)\|\|void 0===(?P=tmp)\?void 0:(?P=tmp)\.toString\(\)"
    r"\)"
)
_UNGUARDED_PERSONA_ID = re.compile(
    r"botPersonaId:(?P<lookup>" + _PROFILE_LOOKUP + r")\.personaId"
)
_GUARDED_PERSONA_ID = re.compile(
    r"botPersonaId:\(" + _PROFILE_LOOKUP + r"\|\|\{\}\)\.personaId"
)

STATUS_APPLIED = "applied"
STATUS_ALREADY = "already"
STATUS_NO_MATCH = "no_match"
STATUS_NO_FILE = "no_file"


def patch_wa_js_source(content: str) -> tuple[str, str]:
    """Return (*content* with the persona-id lookup guarded, status).

    Idempotent: guarded text no longer matches the unguarded pattern, so a
    second pass reports STATUS_ALREADY and changes nothing. A bundle holding
    neither form (an upstream rewrite) comes back untouched as
    STATUS_NO_MATCH.
    """
    patched, count = _UNGUARDED_PERSONA_ID.subn(
        lambda match: f"botPersonaId:({match.group('lookup')}||{{}}).personaId",
        content,
    )
    if count:
        return patched, STATUS_APPLIED
    if _GUARDED_PERSONA_ID.search(content):
        return content, STATUS_ALREADY
    return content, STATUS_NO_MATCH


def patch_wa_js_bundle(api_dir: str) -> tuple[bool, str]:
    """Patch the wa-js bundle under *api_dir* (the outer client/api
    directory). Returns (ok, note); *ok* is False when the bundle is missing
    or holds neither form of the lookup, and *note* is the line to log.
    """
    bundle_path = os.path.join(api_dir, *WA_JS_BUNDLE_PARTS)
    if not os.path.isfile(bundle_path):
        return False, "wppconnect-wa.js not found — skipping the Meta AI persona-id patch."

    # newline="" both ways: the bundle is rewritten byte for byte apart from
    # the one expression, whatever line endings npm unpacked it with.
    with open(bundle_path, encoding="utf-8", newline="") as f:
        content = f.read()

    patched, status = patch_wa_js_source(content)
    if status == STATUS_APPLIED:
        with open(bundle_path, "w", encoding="utf-8", newline="") as f:
            f.write(patched)
        return True, (
            "Patched wppconnect-wa.js — a message to Meta AI no longer fails "
            "when its bot profile has not loaded."
        )
    if status == STATUS_ALREADY:
        return True, "wppconnect-wa.js Meta AI persona-id patch already applied."
    return False, (
        "wppconnect-wa.js: the bot persona-id lookup did not match the "
        "expected upstream source — skipping (the installed @wppconnect/wa-js "
        "version may have changed it)."
    )
