"""SemanticJumpMixin — part of ConversationsPanel (see ui/conversation_panel/__init__.py).

Jump to the next / previous message of a kind in the open conversation: a voice
message not played yet, an image/video/sticker, a document, a poll, a link. The
walk is what Alt+Shift+M/J (mentions) and Alt+Shift+P/O (replies to me) do, with
one difference on purpose: it does not wrap. Past the last one it says so,
because a tour that silently starts over cannot tell "there is one more" from
"that was all".

What is "not played yet": the app records a played voice message as a status 5
("played") on the message, the same one a played receipt from the other side
sets (MainWindow.mark_audio_message_played()). A received audioMessage with no
played status is unplayed. Two limits, both in the data and not fixable here:
with Settings > Audio playback > "mark audio as played in the list" off the
local status is never written (only the receipt goes to WhatsApp), so every
voice message listened to in the app stays "unplayed"; and a message played on
the phone before WinZapp saw it only counts if its receipt reached us.

The matching is pure (message dicts in, row numbers out) and tested directly.
"""

import json

from ui.conversation_panel.text_helpers import _URL_RE

AUDIO, MEDIA, DOCUMENT, POLL, LINK = "audio", "media", "document", "poll", "link"
KINDS = (AUDIO, MEDIA, DOCUMENT, POLL, LINK)

KIND_LABEL_KEYS = {
    AUDIO: "semantic_kind_audio",
    MEDIA: "semantic_kind_media",
    DOCUMENT: "semantic_kind_document",
    POLL: "semantic_kind_poll",
    LINK: "semantic_kind_link",
}

_MEDIA_TYPES = ("imageMessage", "videoMessage", "stickerMessage")
_POLL_TYPES = ("pollCreationMessage", "pollCreationMessageV2", "pollCreationMessageV3")
_CAPTIONED_TYPES = ("imageMessage", "videoMessage", "documentMessage")


def _is_played(msg: dict) -> bool:
    """Whether *msg* carries a played status anywhere _map_status() looks:
    a MessageUpdate entry, the root status, or the root ack (4 = played in
    WPPConnect's numbering)."""
    statuses = []
    updates = msg.get("MessageUpdate")
    if isinstance(updates, list):
        statuses.extend(u.get("status") for u in updates if isinstance(u, dict))
    statuses.append(msg.get("status"))
    for status in statuses:
        text = str(status or "").upper()
        if "PLAYED" in text or text == "5":
            return True
    return str(msg.get("ack")) == "4"


def _own_texts(msg: dict) -> list:
    """The text pieces of a text or captioned message that can hold a link."""
    inner = msg.get("message")
    if isinstance(inner, str):  # some paths store the body as a JSON string
        try:
            inner = json.loads(inner)
        except ValueError:
            return []
    if not isinstance(inner, dict):
        return []
    texts = [inner.get("conversation")]
    extended = inner.get("extendedTextMessage")
    if isinstance(extended, dict):
        texts += [extended.get("text"), extended.get("matchedText")]
    for key in _CAPTIONED_TYPES:
        media = inner.get(key)
        if isinstance(media, dict):
            texts.append(media.get("caption"))
    return [t for t in texts if isinstance(t, str) and t]


def message_matches_kind(msg, kind: str) -> bool:
    """Whether the message record *msg* is of *kind* (one of KINDS)."""
    if not isinstance(msg, dict):
        return False
    msg_type = msg.get("messageType", "")
    if kind == AUDIO:
        return (msg_type == "audioMessage"
                and not (msg.get("key") or {}).get("fromMe")
                and not _is_played(msg))
    if kind == MEDIA:
        return msg_type in _MEDIA_TYPES
    if kind == DOCUMENT:
        return msg_type == "documentMessage"
    if kind == POLL:
        return msg_type in _POLL_TYPES
    if kind == LINK:
        return any(_URL_RE.search(text) for text in _own_texts(msg))
    return False


def semantic_indices(messages, kind: str, is_separator) -> list:
    """Rows of *messages* of *kind*, oldest first; *is_separator* is the
    panel's test for the unread-separator and placeholder rows."""
    return [
        i for i, msg in enumerate(messages)
        if not is_separator(msg) and message_matches_kind(msg, kind)
    ]


def pick_semantic_position(indices: list, current: int, forward: bool) -> int:
    """Position *within indices* of the match after (forward) or before the row
    *current*, -1 when there is none that way. With no row focused (< 0) forward
    starts at the oldest and backward at the newest. No wrap-around."""
    if forward:
        for position, idx in enumerate(indices):
            if idx > current:
                return position
        return -1
    for position in range(len(indices) - 1, -1, -1):
        if current < 0 or indices[position] < current:
            return position
    return -1


class SemanticJumpMixin:
    """The ten keys (next / previous for each kind) of the open conversation."""

    def _on_accel_semantic_jump(self, kind: str, forward: bool):
        if not self.conversation:
            return
        i18n = self.main_window.i18n
        label = i18n.t(KIND_LABEL_KEYS[kind])
        indices = semantic_indices(self._sorted_messages, kind, self._is_separator)
        if not indices:
            self.main_window.output(i18n.t("semantic_jump_none").format(kind=label), interrupt=True)
            return
        position = pick_semantic_position(indices, self.messages_list.GetFocusedItem(), forward)
        if position < 0:
            key = "semantic_jump_no_next" if forward else "semantic_jump_no_previous"
            self.main_window.output(i18n.t(key).format(kind=label), interrupt=True)
            return
        self._focus_message_row(indices[position])
        self.main_window.output(
            i18n.t("semantic_jump_found").format(
                kind=label, position=position + 1, total=len(indices)),
            interrupt=True,
        )
