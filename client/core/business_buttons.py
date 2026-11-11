"""The buttons of a business's message, and the one request that answers them.

A business sends buttons in three shapes, and WhatsApp Web answers each one
its own way (read over CDP on 2026-10-21, WAWebGetInteractiveActionsFromButtons
and WAWebSendButtonsMsgReplyChatAction):

* ``dynamicReplyButtons``: the reply buttons of a plain message. Tapping one
  sends a "buttons response" carrying the button's id.
* ``interactivePayload.buttons``: an interactive ("native flow") message. A
  quick reply is sent as a text message that quotes it, with the button's
  index and id; a link, a copied code and a phone number are never sent.
* ``hydratedButtons``: a template message, answered by index.
* ``list``: a list message; each row is answered with a list response that
  carries the row's id.

Only the server can send an answer (api_patches buttonReplyController.ts); here
the buttons are read off the message and the request is made. A link, a code
and a number are acted on by the window.

Pure: no wx, no main window.
"""

from __future__ import annotations

import json
import re
from urllib.parse import quote, urlsplit

from core.api_client import api_post

MAX_BUTTONS = 32
LABEL_LIMIT = 80
SOURCES = ("dynamic", "native", "template", "list")
REPLY, URL, COPY, CALL = "reply", "url", "copy", "call"

_MESSAGE = re.compile(r"(?:true|false)_\d+@(?:c\.us|lid)_[A-Za-z0-9]{8,64}")
_OLD_CARD_LABEL = re.compile(r"^([1-9]\d*)\. (.+): [^:\n]+$", re.DOTALL)


class ButtonReplyError(Exception):
    """A refusal with the server's own code, or "unavailable"."""

    def __init__(self, code="unavailable"):
        super().__init__(code)
        self.code = code


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def safe_business_url(value):
    """Only explicit web links; never a shell, file or arbitrary app URI."""
    if not isinstance(value, str) or len(value) > 8192 or any(ord(c) <= 32 for c in value):
        return False
    try:
        parsed = urlsplit(value)
        return (parsed.scheme.lower() in ("http", "https") and bool(parsed.hostname)
                and parsed.username is None and "\\" not in value)
    except ValueError:
        return False


def _button(kind, source, index, label, value="", *, full_label=False):
    label = _text(label)
    if not label:
        return None
    result = {"kind": kind, "source": source, "index": index,
            "label": label if full_label else label[:LABEL_LIMIT], "value": _text(value)}
    if not full_label and len(label) > LABEL_LIMIT:
        result["fullLabel"] = label
    return result


def _dicts(value):
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _slots(value):
    return [_dict if isinstance(_dict, dict) else {} for _dict in value[:MAX_BUTTONS]] if isinstance(value, list) else []


def _dynamic(wpp_msg):
    found = []
    for index, raw in enumerate(_slots(wpp_msg.get("dynamicReplyButtons"))):
        text = raw.get("buttonText")
        button = _button(REPLY, "dynamic", index, text.get("displayText") if isinstance(text, dict) else "",
                         raw.get("buttonId") if isinstance(raw.get("buttonId"), str) else "")
        if button and button["value"]:
            found.append(button)
    return found


def _native(wpp_msg):
    payload = wpp_msg.get("interactivePayload")
    found = []
    for index, raw in enumerate(_slots(payload.get("buttons") if isinstance(payload, dict) else None)):
        if not isinstance(raw.get("buttonParamsJson"), str) or len(raw["buttonParamsJson"]) > 65536:
            continue
        try:
            params = json.loads(raw.get("buttonParamsJson"))
        except (TypeError, ValueError):
            continue
        if not isinstance(params, dict) or params.get("disabled") is True:
            continue
        name, label = raw.get("name"), params.get("display_text")
        is_card = isinstance(raw.get("cardIndex"), int) and raw["cardIndex"] >= 0
        if is_card and name == "quick_reply":
            # The action text is usually the same "Select" on every card.
            # Its accessible name needs the whole trip, while its id still
            # identifies the selected option when the button is activated.
            label = _text(raw.get("cardText")) or _text(raw.get("cardTitle")) or label
        if name == "quick_reply":
            button = _button(REPLY, "native", index, label, params.get("id"), full_label=is_card)
        elif name == "cta_url":
            url = _text(params.get("url"))
            button = _button(URL, "native", index, label, url) if safe_business_url(url) else None
        elif name == "cta_copy":
            button = _button(COPY, "native", index, label, params.get("copy_code"))
        elif name == "cta_call":
            button = _button(CALL, "native", index, label, params.get("phone_number"))
        else:
            button = None
        if button and (button["kind"] == URL or button["value"] or button["kind"] == REPLY):
            found.append(button)
    return found


def _list(wpp_msg):
    listing = wpp_msg.get("list")
    found, index = [], 0
    for section in _dicts(listing.get("sections") if isinstance(listing, dict) else None):
        for row in _slots(section.get("rows")):
            if index >= MAX_BUTTONS:
                return found
            label = "\n".join(_text(value) for value in (section.get("title"), row.get("title"), row.get("description")) if _text(value))
            button = _button(REPLY, "list", index, row.get("title"),
                             row.get("rowId") if isinstance(row.get("rowId"), str) else "") if _text(row.get("title")) else None
            if button and label != _text(row.get("title")):
                button["fullLabel"] = label
            if button and button["value"]:
                found.append(button)
            index += 1
    return found


def _template(wpp_msg):
    found = []
    for index, raw in enumerate(_slots(wpp_msg.get("hydratedButtons"))):
        quick, link, call = raw.get("quickReplyButton"), raw.get("urlButton"), raw.get("callButton")
        if isinstance(quick, dict):
            button = _button(REPLY, "template", index, quick.get("displayText"), quick.get("id"))
        elif isinstance(link, dict):
            url = _text(link.get("url"))
            button = _button(URL, "template", index, link.get("displayText"), url) \
                if safe_business_url(url) else None
        elif isinstance(call, dict):
            button = _button(CALL, "template", index, call.get("displayText"), call.get("phoneNumber"))
        else:
            button = None
        if button and button["value"]:
            found.append(button)
    return found


def business_buttons(wpp_msg):
    """The buttons to offer for a message as the server serialized it, in the
    order WhatsApp shows them: [{kind, source, index, label, value}]. `index`
    is the button's place in its own list, which is what the server answers
    by. Empty for a message without any, or for a button it cannot act on."""
    if not isinstance(wpp_msg, dict):
        return []
    return (_dynamic(wpp_msg) + _native(wpp_msg) + _template(wpp_msg) + _list(wpp_msg))[:MAX_BUTTONS]


def business_button_label(msg, button):
    """Name for a stored button, including carousels cached by the old build.

    Their labels were saved as "3. <first 32 characters>: Select" and a
    restart does not re-normalize that local message. Recover the complete
    card from the interactive message body, which still contains all cards.
    """
    label = button.get("fullLabel") or button.get("label") or ""
    if (msg.get("messageType") != "interactiveMessage"
            or button.get("kind") != REPLY or button.get("source") != "native"):
        return label
    old = _OLD_CARD_LABEL.fullmatch(label)
    if not old:
        return label
    preview = old.group(2)
    interactive = (msg.get("message") or {}).get("interactiveMessage") or {}
    body = (interactive.get("body") or {}).get("text") or ""
    if not isinstance(body, str) or not isinstance(msg.get("businessButtons"), list):
        return preview
    locations = []
    cursor = 0
    previous_number, previous_start = "", -1
    for action in msg["businessButtons"]:
        if not isinstance(action, dict):
            continue
        match = _OLD_CARD_LABEL.fullmatch(action.get("label") or "")
        if not match:
            continue
        number, candidate = match.group(1), match.group(2)
        if number == previous_number:
            start = previous_start
        else:
            start = body.find(candidate, cursor)
            if start < 0:
                continue
            cursor = start + len(candidate)
        locations.append((action, start))
        previous_number, previous_start = number, start
    for action, start in locations:
        if action is button or (action.get("source"), action.get("index")) == (button.get("source"), button.get("index")):
            end = next((pos for _, pos in locations if pos > start), len(body))
            return body[start:end].strip() or preview
    return preview


def reply_to_button(connection, message_id, button):
    """Answer a reply button, once. Never retried: a lost answer may follow
    one that was sent."""
    if not isinstance(message_id, str) or not _MESSAGE.fullmatch(message_id):
        raise ButtonReplyError("invalid_request")
    if (not isinstance(button, dict) or button.get("kind") != REPLY or button.get("source") not in SOURCES
            or isinstance(button.get("index"), bool) or not isinstance(button.get("index"), int)
            or not 0 <= button["index"] < MAX_BUTTONS):
        raise ButtonReplyError("invalid_request")
    server, port, token = connection
    url = f"{server}:{port}/api/{token}/reply-button/{quote(message_id, safe='')}"
    try:
        response = api_post(url, json={"source": button["source"], "index": button["index"]},
                            headers={"Authorization": f"Bearer {token}"}, timeout=45)
        body = response.json()
    except Exception:
        raise ButtonReplyError() from None
    if not isinstance(body, dict) or response.status_code != 200 or body.get("status") != "success":
        code = body.get("code") if isinstance(body, dict) else None
        raise ButtonReplyError(code if isinstance(code, str) and code != "reply_button_unavailable" else "unavailable")
