"""Outgoing link-preview generation for the message composer.

WPPConnect/WA-JS *can* generate a preview itself when sending
(``options.linkPreview: true``), but that fetches through undocumented
third-party proxy servers (``linkPreviewApiServers`` in WA-JS's own config)
and used to make the whole send call hang until timeout — see
``git log -S '"linkPreview": False'`` (commit 6cec2d0e), which is why
``MainWindow.send_text_message()`` unconditionally disables it.

This module fetches the preview ourselves instead: a plain HTTP GET of the
pasted URL's HTML, parsed for Open Graph tags, run on a background thread and
fully independent of WhatsApp Web's own machinery. The composer
(ConversationsPanel) calls ``find_first_url()`` while the user types and, on
a debounce, ``fetch_link_preview()`` off the UI thread; a resolved preview is
then passed to ``send_text_message()`` as an explicit
``options.linkPreview`` object, which WA-JS uses as-is without doing any
fetch of its own (see ``prepareLinkPreview`` in wa-js — an object skips the
network call entirely, only ``true`` triggers it).
"""

from __future__ import annotations

import html
import logging
import re
from html.parser import HTMLParser

import requests

_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)

_REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) WinZapp/LinkPreview",
}

# An Open Graph/title tag lives in <head>, but "near the top of the
# document" does not hold for JS-heavy pages: YouTube's watch page, for
# example, inlines ~680KB of hydration JSON/script before its own og:title
# meta tag. A fixed low byte cap silently produced no preview at all for
# those sites (no error — fetch_link_preview() just legitimately found
# nothing in what it read). Instead, keep reading chunks until </head> has
# actually been seen, so the cap below is a safety ceiling against
# pathological/never-closing markup, not the expected stopping point.
_MAX_BYTES_READ = 3 * 1024 * 1024

# Matched case-folded: `</HEAD>` is valid HTML and still turns up on older
# pages. Missing it wouldn't corrupt the preview — the parser below finds the
# tags either way — but it would drop the early exit and download the whole
# 3MB ceiling before giving up, which is precisely the cost this stop
# condition exists to avoid.
_HEAD_CLOSE = b"</head>"
_CHUNK_SIZE = 16384


def decode_html_entities(text: str) -> str:
    """Turn HTML entities in *text* into the real characters.

    Sites often double-escape their Open Graph text (``&amp;eacute;``), so the
    one pass ``HTMLParser`` already did leaves ``&eacute;`` behind. Unescape
    until nothing changes (bounded), so ``&amp;amp;`` and ``&amp;eacute;``
    both end as the plain character.
    """
    text = text or ""
    for _ in range(4):
        decoded = html.unescape(text)
        if decoded == text:
            break
        text = decoded
    return text


def find_first_url(text: str) -> str:
    """Return the first http(s) URL in *text*, or "" if there is none."""
    match = _URL_RE.search(text or "")
    return match.group(0) if match else ""


class _MetaTagParser(HTMLParser):
    """Pulls <title> and the og:title/og:description/og:url meta tags."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.og: dict[str, str] = {}
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            prop = (attrs_dict.get("property") or attrs_dict.get("name") or "").lower()
            if prop in ("og:title", "og:description", "og:url"):
                self.og[prop] = attrs_dict.get("content") or ""

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title and not self.title:
            self.title = data


def fetch_link_preview(url: str, timeout: float = 6.0) -> dict | None:
    """Fetch Open Graph link-preview metadata for *url*.

    Returns ``{"title", "description", "canonicalUrl"}`` or ``None`` on any
    failure — network error, non-HTML response, or a page with neither a
    title nor a description to show. Always call this off the UI thread; it
    blocks on network I/O.
    """
    try:
        response = requests.get(
            url, timeout=timeout, stream=True, headers=_REQUEST_HEADERS,
        )
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "")
        if "html" not in content_type.lower():
            return None
        raw = b""
        for chunk in response.iter_content(chunk_size=_CHUNK_SIZE):
            if not chunk:
                continue
            # Search only the freshly-arrived tail, plus the few bytes a
            # `</head>` split across the chunk boundary could have left behind:
            # everything before that was already searched on an earlier
            # iteration. Re-scanning the whole buffer each time is quadratic in
            # the ceiling above (~192 passes over up to 3MB), for no more
            # information than this.
            tail_start = max(0, len(raw) - (len(_HEAD_CLOSE) - 1))
            raw += chunk
            if _HEAD_CLOSE in raw[tail_start:].lower():
                break
            if len(raw) >= _MAX_BYTES_READ:
                break
        html_text = raw.decode(response.encoding or "utf-8", errors="ignore")
    except Exception as exc:
        logging.info("[link_preview] fetch failed for %r: %s", url, exc)
        return None
    finally:
        try:
            response.close()
        except Exception:
            pass

    parser = _MetaTagParser()
    try:
        parser.feed(html_text)
    except Exception as exc:
        logging.info("[link_preview] HTML parse failed for %r: %s", url, exc)
        return None

    title = decode_html_entities(parser.og.get("og:title") or parser.title or "").strip()
    description = decode_html_entities(parser.og.get("og:description") or "").strip()
    if not title and not description:
        return None

    return {
        "title": title[:300],
        "description": description[:600],
        "canonicalUrl": (parser.og.get("og:url") or url).strip(),
    }
