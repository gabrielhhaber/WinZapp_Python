"""Searching the local message history of every chat (no wx, no database).

The window is ui/dialogs/history_search.py. What lives here is the part worth
testing alone: what a stored message can be found by, how a query matches it,
and the scan that walks the database page by page.

messages.message_json is encrypted, so no SQL can look inside it: the scan
reads a page of rows, decrypts them one by one, keeps the few that match and
drops the rest before reading the next page. Memory is one page plus the hits
(capped at `limit`), however large the history; the caller runs it off the
window's thread and can cancel it between messages.

A chat the vault hides is never decrypted: `is_hidden(jid)` is asked before the
row is opened, and nothing in a result or in a status line counts what it
skipped, so the search cannot tell that the vault exists or how much it holds.

What a message is found by: its text, the caption of a photo, video or
document, a document's file name, a poll's question and options, a contact
card's name, a place's name and address, the saved transcription or AI
description (what the window of a voice message or a picture saved), and the
name of its sender and of its chat. Every word of the query must be somewhere
in all of that, and at least one in the message itself: a chat's name alone
selects every message of that chat, which is a list, not a search.

Tested in test_history_search.py. Never log the query or a result.
"""
import json
from dataclasses import dataclass

from core.transcription import stored
from core.utils import normalize_for_search

#: rows read and decrypted per step of the scan
BATCH_SIZE = 300
#: the most results a search returns
RESULT_LIMIT = 200
#: characters of a message kept in a result's row
SNIPPET_LENGTH = 140

DONE, LIMIT_REACHED, CANCELLED = "done", "limit", "cancelled"

#: Types with nothing to search in (a reaction, a deletion notice, a vote, a
#: message not decrypted yet); the SQL leaves them out so they cost no read.
SKIPPED_TYPES = ("reactionMessage", "protocolMessage", "pollUpdateMessage", "ciphertext")


@dataclass(frozen=True)
class Hit:
    jid: str
    message: dict
    timestamp: int
    snippet: str


@dataclass(frozen=True)
class ScanResult:
    status: str
    count: int


def query_words(query, mode="nfd") -> list:
    """The words of *query*, case and (with the default mode) accents folded."""
    return normalize_for_search(query or "", mode).split()


def _body(msg: dict) -> dict:
    inner = msg.get("message")
    if isinstance(inner, str):  # some paths store the body as a JSON string
        try:
            inner = json.loads(inner)
        except ValueError:
            return {}
    return inner if isinstance(inner, dict) else {}


def _strings(*values) -> list:
    return [v for v in values if isinstance(v, str) and v.strip()]


def message_texts(msg) -> list:
    """Every piece of text *msg* can be found by (never its sender or chat)."""
    if not isinstance(msg, dict):
        return []
    body = _body(msg)
    texts = _strings(body.get("conversation"))
    extended = body.get("extendedTextMessage")
    if isinstance(extended, dict):
        texts += _strings(extended.get("text"), extended.get("title"),
                          extended.get("description"))
    for key in ("imageMessage", "videoMessage", "documentMessage"):
        media = body.get(key)
        if isinstance(media, dict):
            texts += _strings(media.get("caption"), media.get("fileName"))
    for key in ("pollCreationMessage", "pollCreationMessageV2", "pollCreationMessageV3"):
        poll = body.get(key)
        if isinstance(poll, dict):
            texts += _strings(poll.get("name"))
            for option in poll.get("options") or []:
                texts += _strings(option.get("optionName") if isinstance(option, dict) else option)
    contact = body.get("contactMessage")
    if isinstance(contact, dict):
        texts += _strings(contact.get("displayName"))
    for key in ("locationMessage", "liveLocationMessage"):
        place = body.get(key)
        if isinstance(place, dict):
            texts += _strings(place.get("name"), place.get("address"), place.get("caption"))
    # What was saved on the message: a transcription, an AI description.
    for record in stored.results_of(msg.get(stored.TRANSCRIPTION_KEY)):
        texts += _strings(record.get("text"))
    return texts


def sender_names(msg, self_label="") -> str:
    """The sender names a message carries without any lookup: the push name,
    or the account's own label for a message of mine."""
    if not isinstance(msg, dict):
        return ""
    if (msg.get("key") or {}).get("fromMe"):
        return self_label or ""
    return msg.get("pushName") or ""


def _fold_with_map(text, mode):
    """*text* folded for matching, and for each folded character the index of
    the character of *text* it came from (folding can drop or add characters)."""
    folded, where = [], []
    for index, char in enumerate(text):
        for piece in normalize_for_search(char, mode):
            folded.append(piece)
            where.append(index)
    return "".join(folded), where


def _snippet(text, words, mode) -> str:
    flat = " ".join(text.split())
    if len(flat) <= SNIPPET_LENGTH:
        return flat
    folded, where = _fold_with_map(flat, mode)
    positions = [folded.find(w) for w in words]
    positions = [p for p in positions if p >= 0]
    start = where[min(positions)] if positions else 0
    start = max(0, start - SNIPPET_LENGTH // 4)
    piece = flat[start:start + SNIPPET_LENGTH].strip()
    return ("..." if start else "") + piece + ("..." if start + SNIPPET_LENGTH < len(flat) else "")


def find_in_message(msg, words, mode, chat_name="", self_label=""):
    """The snippet to show when *msg* matches *words*, None when it does not.

    Every word has to be in the message's texts, its sender's or its chat's
    name, and at least one in the texts (see the module docstring)."""
    if not words:
        return None
    texts = message_texts(msg)
    if not texts:
        return None
    content = [normalize_for_search(t, mode) for t in texts]
    joined = " ".join(content)
    if not any(w in joined for w in words):
        return None
    names = normalize_for_search(f"{chat_name} {sender_names(msg, self_label)}", mode)
    if not all(w in joined or w in names for w in words):
        return None
    best = max(range(len(texts)), key=lambda i: (sum(w in content[i] for w in words), -i))
    return _snippet(texts[best], words, mode)


def _timestamp(msg) -> int:
    for value in (msg.get("messageTimestamp"), msg.get("timestamp")):
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return 0


def scan_history(read_page, decrypt, words, *, mode="nfd", is_hidden, chat_name=None,
                 self_label="", limit=RESULT_LIMIT, cancelled=lambda: False,
                 on_hits=None, batch_size=BATCH_SIZE) -> ScanResult:
    """Walk the history newest row first and report what matches *words*.

    read_page(before_rowid, size) -> [(rowid, jid, token)] (before_rowid None
    for the first page; an empty page ends the scan); decrypt(token) -> message
    dict or falsy; is_hidden(jid) -> True for a chat that must not be opened;
    chat_name(jid) -> its name for the name match. on_hits(list of Hit) is
    called after each page that had any. Stops at *limit* hits (LIMIT_REACHED)
    or when cancelled() turns true (CANCELLED); a message found under two
    addresses of a chat is reported once.
    """
    chat_name = chat_name or (lambda _jid: "")
    seen, found, before = set(), 0, None
    hidden = {}
    while True:
        if cancelled():
            return ScanResult(CANCELLED, found)
        page = read_page(before, batch_size)
        if not page:
            return ScanResult(DONE, found)
        before = page[-1][0]
        hits = []
        for _rowid, jid, token in page:
            if cancelled():
                break
            if jid not in hidden:
                hidden[jid] = bool(is_hidden(jid))
            if hidden[jid]:
                continue
            msg = decrypt(token)
            if not isinstance(msg, dict):
                continue
            snippet = find_in_message(msg, words, mode, chat_name(jid), self_label)
            if snippet is None:
                continue
            mid = (msg.get("key") or {}).get("id") or ""
            identity = (jid.split("@")[0], mid)
            if mid and identity in seen:
                continue
            seen.add(identity)
            hits.append(Hit(jid, msg, _timestamp(msg), snippet))
            if found + len(hits) >= limit:
                break
        found += len(hits)
        if hits and on_hits is not None:
            on_hits(hits)
        if found >= limit:
            return ScanResult(LIMIT_REACHED, found)
        if cancelled():
            return ScanResult(CANCELLED, found)
