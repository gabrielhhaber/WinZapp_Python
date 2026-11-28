"""What was typed in a conversation and not sent: one draft per conversation.

The message field is one control shared by every conversation. Opening another
one used to leave the text typed in the first in the field, where the next
Enter sent it to the wrong person. Like WhatsApp Web, each conversation now
keeps its own: left behind when the conversation is left, put back when it is
opened again.

A draft is message content: it never reaches the disk unencrypted. With
`persist_enabled()` (the default) the drafts also survive a restart or an
automatic update: `encode()` seals all of them, the open conversation's
included, into one Fernet token with the installation's key, kept in the
account's own database (ui/conversation_panel/draft_persistence.py writes it);
`decode()` reads it back and answers "no drafts" for anything it cannot read.
A locked conversation's draft is kept the same way, inside the one token: its
existence shows nowhere, and it is only handed back by take(), which runs when
the vault lets the conversation open.

Settings > Interface > Conversations chooses among three (`mode()`):

* KEEP_IN_FIELD — the field keeps its text across conversations, as it did
  before drafts existed;
* PER_CONVERSATION — the default: one draft per conversation;
* DISCARD — what was typed is thrown away when the conversation is left.

Pure, no wx, so it is tested directly.
"""

import json
from dataclasses import dataclass, field

from cryptography.fernet import Fernet

SECTION = "user_interface"
SETTING = "message_draft_mode"
#: Keep the drafts across restarts. On by default: they are encrypted like
#: every message in the database, and a blind user who loses a long unsent
#: text to an update has to retype it with a screen reader.
PERSIST_SETTING = "persist_message_drafts"
#: system_metadata key of the sealed token.
METADATA_KEY = "message_drafts_v1"
_FORMAT = 1
#: A bound on what a damaged or hostile file can make the app load.
MAX_PERSISTED = 500

KEEP_IN_FIELD = "keep_in_field"
PER_CONVERSATION = "per_conversation"
DISCARD = "discard"
#: In the order the Settings choice lists them.
MODES = (KEEP_IN_FIELD, PER_CONVERSATION, DISCARD)
DEFAULT = PER_CONVERSATION


def mode(settings) -> str:
    """The configured mode; DEFAULT for a missing or unknown one."""
    section = settings.get(SECTION) if isinstance(settings, dict) else None
    value = section.get(SETTING) if isinstance(section, dict) else None
    return value if value in MODES else DEFAULT


def persist_enabled(settings) -> bool:
    """Whether drafts are kept across restarts; on when missing or malformed,
    and never in DISCARD mode (nothing is kept at all there)."""
    section = settings.get(SECTION) if isinstance(settings, dict) else None
    value = section.get(PERSIST_SETTING, True) if isinstance(section, dict) else True
    return value is not False and mode(settings) == PER_CONVERSATION


@dataclass
class Draft:
    text: str
    #: The mentions typed into it (ConversationsPanel._pending_mentions) and the
    #: names they were written with, so "@Ana" still mentions Ana when sent.
    mentions: list = field(default_factory=list)
    mention_names: dict = field(default_factory=dict)


def to_payload(drafts: dict) -> str:
    """The plain JSON of {jid: Draft}; "" when there is nothing to keep."""
    kept = {
        jid: {"t": d.text, "m": list(d.mentions), "n": dict(d.mention_names)}
        for jid, d in list(drafts.items())[:MAX_PERSISTED]
        if jid and isinstance(d.text, str) and d.text.strip()
    }
    if not kept:
        return ""
    return json.dumps({"v": _FORMAT, "d": kept}, ensure_ascii=False, sort_keys=True)


def seal(payload: str, key: bytes) -> str:
    """*payload* as a Fernet token; "" stays "" (nothing stored)."""
    if not payload:
        return ""
    return Fernet(key).encrypt(payload.encode("utf-8")).decode("ascii")


def decode(token, key: bytes) -> dict:
    """{jid: Draft} out of a stored token. Anything unreadable (a truncated or
    foreign token, another key, bad JSON, wrong shapes) is "no drafts": a
    damaged file must never raise into the window."""
    if not token or not isinstance(token, str):
        return {}
    try:
        data = json.loads(Fernet(key).decrypt(token.encode("ascii")).decode("utf-8"))
        if data.get("v") != _FORMAT:
            return {}
        out = {}
        for jid, item in list(data["d"].items())[:MAX_PERSISTED]:
            text = item["t"]
            mentions = [m for m in item.get("m", []) if isinstance(m, str)]
            names = {k: v for k, v in item.get("n", {}).items()
                     if isinstance(k, str) and isinstance(v, str)}
            if isinstance(jid, str) and jid and isinstance(text, str) and text.strip():
                out[jid] = Draft(text, mentions, names)
        return out
    except Exception:
        return {}


class MessageDrafts:
    def __init__(self):
        self._drafts = {}

    def __len__(self):
        return len(self._drafts)

    def snapshot(self) -> dict:
        """A copy of the kept drafts, {jid: Draft}."""
        return dict(self._drafts)

    def load(self, drafts: dict) -> None:
        """Add drafts read from disk; one typed in this run is never replaced."""
        for jid, draft in drafts.items():
            self._drafts.setdefault(jid, draft)

    def stash(self, jid, text, mentions=(), mention_names=None) -> None:
        """Keep *text* as *jid*'s draft; a blank one forgets it (it was sent or
        erased)."""
        if not jid:
            return
        if not isinstance(text, str) or not text.strip():
            self._drafts.pop(jid, None)
            return
        self._drafts[jid] = Draft(text, list(mentions or ()), dict(mention_names or {}))

    def take(self, jid, same_chat=None):
        """*jid*'s draft, handed back and forgotten (it is in the field again);
        None when there is none.

        *same_chat(stored, jid)* finds a draft kept under another spelling of
        the chat: an @lid chat re-keyed to its phone JID (whatever forms the
        caller's equivalence bridges)."""
        if not jid:
            return None
        if jid in self._drafts:
            return self._drafts.pop(jid)
        if same_chat is not None:
            for stored in list(self._drafts):
                try:
                    match = same_chat(stored, jid)
                except Exception:
                    match = False
                if match:
                    return self._drafts.pop(stored)
        return None

    def clear(self) -> None:
        self._drafts.clear()
