"""Re-upload of a media the CDN no longer serves (issue #747, item 3).

A media's link expires after a few weeks (the download fails with 403, 404 or
410) but the account's own phone still holds the file and can upload it again:
WhatsApp Web sends a *media retry receipt* and the phone answers with a
notification, encrypted with the media key, carrying the new `directPath`.
whatsmeow has both halves; WinZapp's neonize build exports them
(`send_media_retry`, `decrypt_media_retry`, `MediaRetryEv`; patch neonize 0023).

`download_async` is what `GET /message/get-media` (server.py) calls: it tries the
download as before and, only when it failed with an expired-link answer,
asks the phone, waits for its answer (`TIMEOUT`), opens it and downloads again
from the new path. A phone that no longer has the file answers "not found", and
the error then carries a 404 so `transfers.status_for_download_error` answers
410 (the client remembers the media as expired). Any other failure is raised
as it was. A build without the calls (an older neonize) never retries.

Pure functions at the top (tested in test_media_retry.py). Nothing here logs a
JID, a name or a key.
"""
import asyncio
import base64
import logging
import threading

import msgmap

log = logging.getLogger("bridge")

#: how long to wait for the phone's answer
TIMEOUT = 25.0
#: the answers of a link that expired (the others are real faults)
EXPIRED_MARKS = ("404", "410", "403", "not found", "forbidden")
MEDIA_FIELDS = ("imageMessage", "videoMessage", "audioMessage", "documentMessage",
                "stickerMessage", "ptvMessage")


class MediaGone(Exception):
    """The phone no longer has the media; the text makes the route answer 410."""


def is_expired_error(error) -> bool:
    text = str(error).lower()
    return any(mark in text for mark in EXPIRED_MARKS)


def parse_serialized(msgid: str):
    """'false_<chat>_<ID>[_author]' -> (from_me, chat, id, author), or None."""
    parts = (msgid or "").split("_")
    if len(parts) < 3 or parts[0] not in ("true", "false") or "@" not in parts[1]:
        return None
    author = parts[3] if len(parts) > 3 and "@" in parts[3] else ""
    return parts[0] == "true", parts[1], parts[2], author


def media_of(message):
    """The media sub-message (the one with `mediaKey`) or None."""
    for name in MEDIA_FIELDS:
        try:
            if message.HasField(name):
                media = getattr(message, name)
                if media.mediaKey:
                    return media
        except ValueError:
            continue
    return None


def retry_request(parsed, media_key: bytes) -> dict:
    """The body of `send_media_retry`."""
    from_me, chat, msg_id, author = parsed
    chat_jid = msgmap.jid_whatsmeow(chat)
    return {
        "chat": chat_jid, "id": msg_id, "from_me": from_me,
        "is_group": chat_jid.endswith("@g.us"),
        "sender": msgmap.jid_whatsmeow(author) if author else "",
        "media_key": base64.b64encode(media_key).decode(),
    }


def decrypt_request(event, media_key: bytes) -> dict:
    """The body of `decrypt_media_retry` for a `MediaRetryEv`."""
    has_error = event.HasField("ErrorCode")
    return {
        "media_key": base64.b64encode(media_key).decode(),
        "message_id": event.MessageID,
        "ciphertext": base64.b64encode(bytes(event.Ciphertext)).decode(),
        "iv": base64.b64encode(bytes(event.IV)).decode(),
        "has_error": has_error,
        "error_code": event.ErrorCode if has_error else 0,
    }


def with_new_path(message, direct_path: str):
    """A copy of `message` pointing at `direct_path`; the old `URL` is cleared
    (whatsmeow downloads from it before it looks at the direct path)."""
    fresh = type(message)()
    fresh.CopyFrom(message)
    media = media_of(fresh)
    media.directPath = direct_path
    media.ClearField("URL")
    return fresh


def has_retry(client) -> bool:
    return callable(getattr(client, "send_media_retry", None)) and \
        callable(getattr(client, "decrypt_media_retry", None))


#: the message field of each kind `download_retried_media` knows
KINDS = {"imageMessage": "image", "videoMessage": "video", "ptvMessage": "video",
         "audioMessage": "audio", "documentMessage": "document",
         "stickerMessage": "sticker"}


def has_retried_download(client) -> bool:
    return callable(getattr(client, "download_retried_media", None))


def kind_of(message):
    """('image' | 'video' | ...) of the message's media, or None."""
    for name, kind in KINDS.items():
        try:
            if message.HasField(name) and getattr(message, name).mediaKey:
                return kind
        except ValueError:
            continue
    return None


def retried_request(message, direct_path: str, dest: str) -> dict:
    """The body of `download_retried_media`: the phone's new upload may encrypt
    to other bytes, so the build authenticates it by the media key's HMAC and
    the plaintext hash instead of the message's old `fileEncSHA256`."""
    media = media_of(message)
    b64 = lambda raw: base64.b64encode(bytes(raw)).decode()
    return {
        "direct_path": direct_path, "media_key": b64(media.mediaKey),
        "enc_sha256": b64(media.fileEncSHA256), "file_sha256": b64(media.fileSHA256),
        "kind": kind_of(message), "dest": dest, "timeout_ms": 120000,
    }


def fetch_retried(client, message, direct_path: str) -> bytes:
    """Downloads the re-uploaded media through the build's own call (blocking).
    The error text is kept: 'invalid media hmac' and the like answer 410."""
    import os
    import tempfile
    descriptor, dest = tempfile.mkstemp(prefix="winzapp-retry-")
    os.close(descriptor)
    try:
        answer = client.download_retried_media(retried_request(message, direct_path, dest))
        if answer.get("error"):
            raise RuntimeError(answer["error"])
        with open(dest, "rb") as f:
            return f.read()
    finally:
        try:
            os.unlink(dest)
        except OSError:
            pass


class Answers:
    """The phone's answers by message id: `expect` before asking, `deliver` from
    neonize's thread (`MediaRetryEv`), read on a worker thread."""

    def __init__(self):
        self._lock = threading.Lock()
        self._waiting = {}

    def expect(self, msg_id):
        slot = {"event": threading.Event(), "answer": None}
        with self._lock:
            self._waiting[msg_id] = slot
        return slot

    def deliver(self, msg_id, answer):
        with self._lock:
            slot = self._waiting.get(msg_id)
        if slot is not None:
            slot["answer"] = answer
            slot["event"].set()

    def forget(self, msg_id):
        with self._lock:
            self._waiting.pop(msg_id, None)


def ask_phone(client, answers, parsed, media, timeout=TIMEOUT):
    """Asks the phone to upload the media again and returns the new direct path;
    raises `MediaGone` when it no longer has the file, RuntimeError when it did
    not answer or answered with a failure."""
    media_key = bytes(media.mediaKey)
    slot = answers.expect(parsed[2])
    try:
        sent = client.send_media_retry(retry_request(parsed, media_key))
        if sent.get("error"):
            raise RuntimeError("media retry not sent")
        if not slot["event"].wait(timeout):
            raise RuntimeError("the phone did not answer the media retry")
        opened = client.decrypt_media_retry(decrypt_request(slot["answer"], media_key))
    finally:
        answers.forget(parsed[2])
    if opened.get("error"):
        raise RuntimeError("media retry answer could not be opened")
    result = opened.get("result")
    if result == "NOT_FOUND":
        raise MediaGone("404 not found: the phone no longer has this media")
    if result != "SUCCESS" or not opened.get("direct_path"):
        raise RuntimeError("media retry failed (%s)" % result)
    return opened["direct_path"]


async def download_async(s, message, msgid, download):
    """`await download(message)`; when it fails with an expired link, asks the
    phone for the media again (session `s`) and downloads from the new path.
    `download` is a coroutine function taking the message."""
    try:
        return await download(message)
    except Exception as error:
        answers = getattr(s, "media_retries", None)
        parsed = parse_serialized(msgid)
        media = media_of(message)
        if (answers is None or parsed is None or media is None
                or not is_expired_error(error) or not has_retry(s.client)):
            raise
        log.info("media retry: asking the phone")
        loop = asyncio.get_running_loop()
        try:
            path = await loop.run_in_executor(
                None, ask_phone, s.client, answers, parsed, media)
        except MediaGone:
            raise
        except Exception as retry_error:
            log.info("media retry gave up: %s", retry_error)
            raise error
        if has_retried_download(s.client):
            return await loop.run_in_executor(
                None, fetch_retried, s.client, message, path)
        return await download(with_new_path(message, path))


def register_events(cli, s):
    """Wires the phone's answer (`MediaRetryEv`, WinZapp's neonize build) to the
    session `s` and creates `s.media_retries`. Called on neonize's thread."""
    import neonize.events as nev

    s.media_retries = Answers()
    if not hasattr(nev, "MediaRetryEv"):
        return

    @cli.event(nev.MediaRetryEv)
    def _media_retry(c, e):
        try:
            s.media_retries.deliver(e.MessageID, e)
        except Exception:
            log.exception("media retry answer")
