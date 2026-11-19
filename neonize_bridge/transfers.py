"""Sending and downloading media with real progress, using the DLL fork's calls
(`0.5.2-winzapp-teste.2`, see prepare.py): `upload_file`,
`transfer_progress`, `transfer_cancel` and `download_any_to_file`.

PyPI's neonize (0.5.2, an older install) does not have them. Everything here
checks `has_transfers` and, without them, falls back to the previous path
(`download_any`, the ordinary send) without emitting any event.

Upload: neonize's `build_*_message` calls `self.upload(bytes)`. Instead of
rewriting the four message builders (thumbnail, duration, waveform),
`ClientWithProgress` runs the same client methods with `upload` swapped for
`upload_file` (which reads from disk and reports progress: 0-10% encrypting,
10-99% on the network). Download: the DLL's progress stays at 0, so the bridge
measures the size of the output file against the message's `fileLength`.

Nothing here was tested against the real DLL (only against fakes); the pure
logic is in progress.py. Tested in test_transfers.py.
"""
import asyncio
import inspect
import logging
import os
import tempfile
import types
import uuid

import progress

log = logging.getLogger("bridge")

#: the Message fields that carry the `fileLength` of a media
MEDIA_FIELDS = ("imageMessage", "videoMessage", "audioMessage", "documentMessage",
                "stickerMessage", "ptvMessage")


def has_transfers(client):
    """True when the client is the fork's AND the loaded DLL exports the transfer
    calls (the fork's `tem_transferencias()` looks at the DLL)."""
    detect = getattr(client, "tem_transferencias", None)     # the name the fork exports
    if not callable(detect):
        return False
    for name in ("upload_file", "transfer_progress", "transfer_cancel",
                 "download_any_to_file"):
        if not callable(getattr(client, name, None)):
            return False
    try:
        return bool(detect())
    except Exception:
        return False


def new_key(prefix):
    """Key of a transfer in the DLL (unique, never the id the client gave)."""
    return "%s-%s" % (prefix, uuid.uuid4().hex)


def expected_size(message):
    """The message's media `fileLength`, 0 when there is none."""
    for field in MEDIA_FIELDS:
        media = getattr(message, field, None)
        try:
            size = int(getattr(media, "fileLength", 0) or 0)
        except (TypeError, ValueError):
            continue
        if size > 0:
            return size
    return 0


def _kind_from_content(binary):
    import media_probe
    return media_probe.media_type_of(binary)


class ClientWithProgress:
    """The neonize client with the `upload` of the file `path` swapped for
    `upload_file`. The client's methods are rebound to this object, so
    `build_*_message` and `send_*` call the `upload` from here; the rest
    (attributes, the internal `__client`) goes to the real client."""

    def __init__(self, client, path, key, kind_from_content=_kind_from_content):
        self._client, self._path, self._key = client, path, key
        self._kind = kind_from_content
        try:
            self._size = os.path.getsize(path)
        except OSError:
            self._size = -1

    def upload(self, binary, media_type=None):
        # only the upload of the file itself goes through the disk; thumbnails and
        # the like (another size) follow the ordinary path
        if self._size < 0 or len(binary) != self._size:
            return self._client.upload(binary, media_type)
        return self._client.upload_file(
            self._path, media_type or self._kind(binary), self._key)

    def __getattr__(self, name):
        # only what the class does not define gets here. What the session put
        # on the client itself wins over the class, as on the client: rebinding
        # the class's send_message skipped chat_timers.install's wrapper, and
        # every file sent with progress (an audio, a photo, a document) went
        # out without the chat's disappearing-messages timer.
        own = vars(self._client).get(name) if hasattr(self._client, "__dict__") else None
        if callable(own):
            return own
        func = getattr(type(self._client), name, None)
        if inspect.isfunction(func):
            return types.MethodType(func, self)
        return getattr(self._client, name)


def send_with_progress(client, path, upload_id, emit_event, session, send,
                       key=None, interval=progress.READ_INTERVAL,
                       minimum=progress.MIN_INTERVAL):
    """Runs `send(c)` (blocking; `c` is the client to use) and, when possible,
    emits `media-upload-progress` while the file uploads. Without `upload_id`,
    without a disk path or without the fork's calls, it is just `send(client)`."""
    if not (upload_id and isinstance(path, str) and has_transfers(client)):
        return send(client)
    key = key or new_key("up")
    proxy = ClientWithProgress(client, path, key)

    def read():
        return progress.fraction_from_percent(client.transfer_progress(key))

    def emit(fraction):
        emit_event("media-upload-progress",
                   progress.upload_event(session, upload_id, fraction))
    with progress.Tracker(read, emit, interval=interval, minimum=minimum) as tracker:
        response = send(proxy)
        tracker.finish()
    return response


async def send_with_progress_async(client, path, upload_id, emit_event, session,
                                   send):
    """`send_with_progress` on a thread; if the request is canceled (the client
    gave up), cancels the upload in the DLL."""
    key = new_key("up")
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(
            None, lambda: send_with_progress(
                client, path, upload_id, emit_event, session, send, key))
    except asyncio.CancelledError:
        _cancel(client, key)
        raise


def _cancel(client, key):
    if not has_transfers(client):
        return
    try:
        client.transfer_cancel(key)
    except Exception:
        log.exception("transfer_cancel")


def download_with_progress(client, message, progress_id, emit_event, session,
                           key=None, interval=progress.READ_INTERVAL,
                           minimum=progress.MIN_INTERVAL):
    """Bytes of `message`'s media. With the fork's calls, a `progress_id` and the
    known size, downloads to a temporary file measuring its size and emits
    `media-download-progress`; otherwise, `download_any` as before."""
    expected = expected_size(message)
    if not (progress_id and expected > 0 and has_transfers(client)):
        return client.download_any(message)
    key = key or new_key("down")
    descriptor, path = tempfile.mkstemp(prefix="winzapp-dl-")
    os.close(descriptor)

    def read():
        try:
            return progress.fraction_from_file(os.path.getsize(path), expected)
        except OSError:
            return None

    def emit(fraction):
        emit_event("media-download-progress",
                   progress.download_event(session, progress_id, fraction))
    try:
        with progress.Tracker(read, emit, interval=interval, minimum=minimum) as tracker:
            client.download_any_to_file(message, path, key)
            tracker.finish()
        with open(path, "rb") as f:
            return f.read()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


async def download_with_progress_async(client, message, body, emit_event, session):
    """`download_with_progress` on a thread (the `progressId` comes from the
    request's JSON `body`, if there is one); a canceled request cancels the download."""
    progress_id = progress.progress_id_of(body)
    key = new_key("down")
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(
            None, lambda: download_with_progress(
                client, message, progress_id, emit_event, session, key))
    except asyncio.CancelledError:
        _cancel(client, key)
        raise


def status_for_download_error(error) -> int:
    """HTTP status the bridge answers a failed media download with.

    410 for media that is gone from WhatsApp's server (404/410) or whose CDN
    link expired (403 / forbidden), which the client treats as "expired" and
    remembers instead of retrying. So does media that is still on the CDN but
    cannot be authenticated or located any more (after a phone re-upload the
    ciphertext hash or MAC no longer matches, or the message carries no URL;
    issue #756): retrying cannot fix it. Anything else is a real fault: 500.
    """
    text = str(error).lower()
    gone = ("404", "410", "403", "not found", "forbidden",
            "ciphertext doesn't match", "invalid media hmac", "no url present")
    return 410 if any(mark in text for mark in gone) else 500
