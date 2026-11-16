"""Fetch a chat's or group's profile picture through WPPConnect.

`GET /api/<session>/profile-pic/<id>` answers with WA-JS's profile-picture
record (`{eurl, img, imgFull, id, tag, raw}`), or with nothing when the
picture is absent or hidden from this account by the contact's privacy
settings. The URLs inside point at WhatsApp's CDN and are signed, so they are
downloaded right away rather than stored. A group is asked with
`isGroup=true`, which makes the route suffix the id `@g.us`; anything else
keeps its `@lid` or becomes `@c.us` on the server side.

Nothing here touches wx: the dialog calls `fetch_profile_picture()` on a
background thread and only shows the result.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from urllib.parse import quote

# The record's keys in the order they are worth trying: the full-size picture
# first, then the thumbnail, then the legacy "eurl" both point back to.
_URL_KEYS = ("imgFull", "img", "eurl")

# A profile picture is a small JPEG; anything past this is not one.
MAX_PICTURE_BYTES = 10 * 1024 * 1024


def picture_url_from_response(payload) -> str:
    """The best picture URL in a /profile-pic answer, or "" when it has none.

    Accepts the whole JSON body (`{"status": ..., "response": ...}`) or just
    its `response`, which WA-JS has returned both as the record and as a bare
    URL string depending on the version.
    """
    if isinstance(payload, dict) and "response" in payload:
        payload = payload.get("response")
    if isinstance(payload, str):
        return payload if payload.startswith("https://") else ""
    if not isinstance(payload, dict):
        return ""
    for key in _URL_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.startswith("https://"):
            return value
    return ""


def picture_extension(data: bytes) -> str:
    """File extension for an image's bytes, or "" when they are not one the
    viewers open (WhatsApp serves JPEG; PNG and WebP are accepted too)."""
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ""


def profile_pic_api_url(base_url: str, token: str, jid: str) -> str:
    """The /profile-pic URL for `jid`, with `isGroup=true` for a group."""
    url = f"{base_url}/api/{token}/profile-pic/{quote(jid, safe='@.')}"
    if jid.endswith("@g.us"):
        url += "?isGroup=true"
    return url


def query_jid(jid: str, lid_to_phone) -> str:
    """The phone JID behind an @lid when it is known, as get_contact_profile()
    asks; the @lid itself otherwise, which the route also accepts."""
    if jid.endswith("@lid"):
        return (lid_to_phone or {}).get(jid) or jid
    return jid


def stored_picture_url(contacts, jid: str) -> str:
    """The picture URL the contact list last stored for `jid`, or ""."""
    entry = (contacts or {}).get(jid) or {}
    url = entry.get("profilePicUrl") if isinstance(entry, dict) else ""
    return url if isinstance(url, str) else ""


def picture_file_name(jid: str, extension: str) -> str:
    """A file name for the saved picture that does not carry the number: the
    folder shows up in Explorer and in the external viewer's title bar."""
    digest = hashlib.sha256(jid.encode("utf-8")).hexdigest()[:16]
    return f"profile_{digest}{extension}"


def fetch_profile_picture(base_url, token, jid, api_get, http_get, fallback_url=""):
    """Download `jid`'s profile picture; returns its bytes or None.

    `api_get` calls the local WPPConnect server, `http_get` the CDN (both are
    `requests.get`-shaped, injected so the logic is testable). `fallback_url`
    is the URL the contact list last stored, tried when the server gives none:
    it may have expired, in which case the CDN refuses it and the answer is
    still None.
    """
    candidates = []
    try:
        r = api_get(
            profile_pic_api_url(base_url, token, jid),
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        if r.status_code in (200, 201):
            payload = r.json()
            # A definitive absent/hidden picture must not revive a cached URL.
            if (isinstance(payload, dict) and payload.get("status") in (None, "success")
                    and "response" in payload and payload["response"] is None):
                return None
            url = picture_url_from_response(payload)
            if url:
                candidates.append(url)
        else:
            logging.info("[profile_picture] server answered %s", r.status_code)
    except Exception as exc:
        logging.warning("[profile_picture] server request failed: %s", type(exc).__name__)
    if fallback_url and fallback_url.startswith("https://") and fallback_url not in candidates:
        candidates.append(fallback_url)

    for url in candidates:
        try:
            r = http_get(url, timeout=20)
        except Exception as exc:
            logging.warning("[profile_picture] download failed: %s", type(exc).__name__)
            continue
        if r.status_code != 200:
            logging.info("[profile_picture] download answered %s", r.status_code)
            continue
        data = r.content or b""
        if 0 < len(data) <= MAX_PICTURE_BYTES and picture_extension(data):
            return data
    return None


def save_picture(folder: str, jid: str, data: bytes) -> str:
    """Write the picture under `folder` and return its path."""
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, picture_file_name(jid, picture_extension(data)))
    try:
        with open(path, "wb") as fh:
            fh.write(data)
    except PermissionError:
        # An external viewer still holds the previous copy open.
        root, ext = os.path.splitext(path)
        path = f"{root}_{time.time_ns()}{ext}"
        with open(path, "wb") as fh:
            fh.write(data)
    return path
