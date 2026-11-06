"""Tests for outgoing link previews.

Reported live: WinZapp never sent a link preview on its own outgoing
messages (only ever showed one on messages it *received*) — `send_text_
message()` unconditionally set `options.linkPreview: False` (commit
6cec2d0e, "avoid timeouts": WPPConnect's own on-send preview fetch goes
through undocumented third-party proxy servers and used to hang the whole
send). This covers the fix in three layers:

1. core/link_preview.py — find_first_url()/fetch_link_preview(), the
   from-scratch OG-tag fetch that replaces WPPConnect's own mechanism,
   run ahead of time on a background thread instead of during send.
2. MainWindow._build_link_preview_options() — still `{"linkPreview": False}`
   with nothing resolved (preserves the timeout fix for every other send),
   but a resolved preview is passed through as an explicit object, which
   WA-JS uses as-is with no fetch of its own.
3. ConversationsPanel's composer wiring — detecting a URL as the user
   types, fetching in the background, and surfacing a "remove preview"
   button (_remove_link_preview_btn) — bound onto a plain stub, same
   approach as tests/test_recording_open_failure.py, since
   ConversationsPanel is a wx.Panel and can't be instantiated without a
   running wx.App.
"""

import threading
import types

import ui.conversations as conversations_module
from core.link_preview import fetch_link_preview, find_first_url
from main import MainWindow
from ui.conversations import ConversationsPanel
from tests.god_modules import patch_conversations_global


# ── core/link_preview.py ────────────────────────────────────────────────────

class TestFindFirstUrl:
    def test_finds_a_url_in_plain_text(self):
        assert find_first_url("check this out https://example.com/page") == (
            "https://example.com/page"
        )

    def test_no_url_returns_empty_string(self):
        assert find_first_url("just some text") == ""

    def test_empty_text_returns_empty_string(self):
        assert find_first_url("") == ""

    def test_returns_the_first_of_several_urls(self):
        text = "see https://a.example.com and also https://b.example.com"
        assert find_first_url(text) == "https://a.example.com"

    def test_stops_at_trailing_punctuation_whitespace(self):
        # A trailing quote/paren isn't part of the URL — the regex only
        # excludes whitespace and quote characters, so this documents what
        # it actually captures rather than claiming smarter trimming.
        assert find_first_url("link: https://example.com/page\nnext line") == (
            "https://example.com/page"
        )


class _FakeResponse:
    def __init__(self, body: bytes, content_type="text/html; charset=utf-8", status=200):
        self._body = body
        self.headers = {"Content-Type": content_type}
        self.status_code = status
        self.encoding = "utf-8"

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=16384):
        yield self._body

    def close(self):
        pass


class TestFetchLinkPreview:
    def test_returns_title_and_description_from_og_tags(self, monkeypatch):
        html = b"""<html><head>
            <meta property="og:title" content="Example Domain">
            <meta property="og:description" content="Example site used for illustration">
            <meta property="og:url" content="https://example.com/canonical">
        </head></html>"""
        monkeypatch.setattr(
            "core.link_preview.requests.get",
            lambda *a, **kw: _FakeResponse(html),
        )
        preview = fetch_link_preview("https://example.com")
        assert preview == {
            "title": "Example Domain",
            "description": "Example site used for illustration",
            "canonicalUrl": "https://example.com/canonical",
        }

    def test_falls_back_to_plain_title_tag_when_no_og_title(self, monkeypatch):
        html = b"<html><head><title>Plain Title</title></head></html>"
        monkeypatch.setattr(
            "core.link_preview.requests.get",
            lambda *a, **kw: _FakeResponse(html),
        )
        preview = fetch_link_preview("https://example.com")
        assert preview["title"] == "Plain Title"
        assert preview["canonicalUrl"] == "https://example.com"  # no og:url — falls back to the input URL

    def test_returns_none_when_neither_title_nor_description_present(self, monkeypatch):
        html = b"<html><head></head><body>hi</body></html>"
        monkeypatch.setattr(
            "core.link_preview.requests.get",
            lambda *a, **kw: _FakeResponse(html),
        )
        assert fetch_link_preview("https://example.com") is None

    def test_returns_none_on_network_error(self, monkeypatch):
        def _boom(*a, **kw):
            raise Exception("connection refused")
        monkeypatch.setattr("core.link_preview.requests.get", _boom)
        assert fetch_link_preview("https://example.com") is None

    def test_returns_none_for_non_html_response(self, monkeypatch):
        monkeypatch.setattr(
            "core.link_preview.requests.get",
            lambda *a, **kw: _FakeResponse(b"\x89PNG...", content_type="image/png"),
        )
        assert fetch_link_preview("https://example.com/pic.png") is None

    def test_returns_none_on_http_error_status(self, monkeypatch):
        monkeypatch.setattr(
            "core.link_preview.requests.get",
            lambda *a, **kw: _FakeResponse(b"", status=404),
        )
        assert fetch_link_preview("https://example.com/missing") is None


class _CountingChunkedResponse(_FakeResponse):
    """Serves the body in real chunks and records how many were consumed, so
    a test can tell "stopped at </head>" apart from "read the whole page"."""

    def __init__(self, body: bytes, chunk_size=64, **kwargs):
        super().__init__(body, **kwargs)
        self._chunk_size = chunk_size
        self.chunks_served = 0

    def iter_content(self, chunk_size=16384):
        for start in range(0, len(self._body), self._chunk_size):
            self.chunks_served += 1
            yield self._body[start:start + self._chunk_size]


class TestFetchLinkPreviewStopsAtHeadClose:
    """The read loop keeps pulling chunks until it has actually seen the end
    of <head> — a fixed low byte cap silently produced no preview at all on
    JS-heavy pages (YouTube inlines ~680KB of hydration script before its own
    og:title). The cap that remains is a ceiling against pathological markup,
    not the expected stopping point.
    """

    _HEAD = (
        b"<html><head><meta property=\"og:title\" content=\"Depois do lixo\">"
    )
    _FILLER = b"<!--" + (b"x" * 4000) + b"-->"

    def test_a_preview_past_the_old_256kb_cap_is_still_found(self, monkeypatch):
        html = (
            b"<html><head>" + (b"<!--" + b"j" * 400000 + b"-->")
            + b"<meta property=\"og:title\" content=\"Tarde demais\">"
            + b"</head><body></body></html>"
        )
        monkeypatch.setattr(
            "core.link_preview.requests.get",
            lambda *a, **kw: _FakeResponse(html),
        )
        assert fetch_link_preview("https://example.com")["title"] == "Tarde demais"

    def test_reading_stops_once_head_closes_instead_of_draining_the_body(
        self, monkeypatch
    ):
        body_filler = b"<p>" + (b"z" * 20000) + b"</p>"
        html = self._HEAD + b"</head><body>" + body_filler + b"</body></html>"
        response = _CountingChunkedResponse(html, chunk_size=64)
        monkeypatch.setattr(
            "core.link_preview.requests.get", lambda *a, **kw: response
        )

        preview = fetch_link_preview("https://example.com")

        assert preview["title"] == "Depois do lixo"
        # The <head> ends well inside the first few hundred bytes; the ~20KB
        # body must never have been pulled.
        assert response.chunks_served * 64 < len(html) / 2

    def test_an_uppercase_head_close_also_stops_the_read(self, monkeypatch):
        """`</HEAD>` is valid HTML and still turns up on older pages. Missing
        it wouldn't corrupt the preview — the parser finds the tags either
        way — but it would drop the early exit and drain up to the 3MB
        ceiling."""
        body_filler = b"<p>" + (b"z" * 20000) + b"</p>"
        html = self._HEAD + b"</HEAD><BODY>" + body_filler + b"</BODY></html>"
        response = _CountingChunkedResponse(html, chunk_size=64)
        monkeypatch.setattr(
            "core.link_preview.requests.get", lambda *a, **kw: response
        )

        preview = fetch_link_preview("https://example.com")

        assert preview["title"] == "Depois do lixo"
        assert response.chunks_served * 64 < len(html) / 2

    def test_head_close_split_across_a_chunk_boundary_is_still_detected(
        self, monkeypatch
    ):
        """Only the freshly-arrived tail is searched each iteration (scanning
        the whole buffer every chunk is quadratic in the 3MB ceiling), so the
        overlap has to cover a `</head>` straddling two chunks. A chunk size
        of 1 puts every one of its bytes on its own boundary."""
        body_filler = b"<p>" + (b"z" * 500) + b"</p>"
        html = self._HEAD + b"</head><body>" + body_filler + b"</body></html>"
        response = _CountingChunkedResponse(html, chunk_size=1)
        monkeypatch.setattr(
            "core.link_preview.requests.get", lambda *a, **kw: response
        )

        preview = fetch_link_preview("https://example.com")

        assert preview["title"] == "Depois do lixo"
        assert response.chunks_served < len(html) / 2

    def test_a_page_that_never_closes_head_is_capped(self, monkeypatch):
        from core.link_preview import _MAX_BYTES_READ

        html = b"<html><head><title>Sem fim</title>" + (b"y" * (_MAX_BYTES_READ + 5000))
        response = _CountingChunkedResponse(html, chunk_size=16384)
        monkeypatch.setattr(
            "core.link_preview.requests.get", lambda *a, **kw: response
        )

        preview = fetch_link_preview("https://example.com")

        assert preview["title"] == "Sem fim"
        assert response.chunks_served * 16384 <= _MAX_BYTES_READ + 16384


# ── MainWindow._build_link_preview_options() ───────────────────────────────

class TestBuildLinkPreviewOptions:
    def test_none_disables_link_preview(self):
        assert MainWindow._build_link_preview_options(None) == {"linkPreview": False}

    def test_empty_dict_disables_link_preview(self):
        assert MainWindow._build_link_preview_options({}) == {"linkPreview": False}

    def test_resolved_preview_is_passed_through_as_an_object(self):
        preview = {
            "title": "Example Domain",
            "description": "Example site used for illustration",
            "canonicalUrl": "https://example.com",
        }
        options = MainWindow._build_link_preview_options(preview)
        assert options == {
            "linkPreview": {
                "title": "Example Domain",
                "description": "Example site used for illustration",
                "canonicalUrl": "https://example.com",
                "matchedText": "https://example.com",
            }
        }


# ── ConversationsPanel composer wiring ──────────────────────────────────────

class _FakeWidget:
    def __init__(self):
        self.shown = False

    def Show(self, show=True):
        self.shown = show

    def Hide(self):
        self.shown = False

    def IsShown(self):
        return self.shown

    def Layout(self):
        pass


class _FakeMessageField:
    def __init__(self, value=""):
        self._value = value

    def GetValue(self):
        return self._value

    def SetValue(self, value):
        self._value = value

    def SetFocus(self):
        pass


class _Stub:
    _schedule_link_preview_check = ConversationsPanel._schedule_link_preview_check
    _check_link_preview_for_current_text = (
        ConversationsPanel._check_link_preview_for_current_text
    )
    _on_link_preview_fetched = ConversationsPanel._on_link_preview_fetched
    _announce_link_preview = ConversationsPanel._announce_link_preview
    _on_remove_link_preview = ConversationsPanel._on_remove_link_preview
    _clear_link_preview = ConversationsPanel._clear_link_preview
    _LINK_PREVIEW_DEBOUNCE_MS = ConversationsPanel._LINK_PREVIEW_DEBOUNCE_MS

    def __init__(self, text=""):
        self.message_field = _FakeMessageField(text)
        self.conversation_panel = _FakeWidget()
        self._remove_link_preview_btn = _FakeWidget()
        self._pending_link_preview = None
        self._link_preview_source_url = ""
        self._link_preview_dismissed_url = ""
        self._link_preview_fetch_token = 0
        self._link_preview_debounce_timer = None
        self.main_window = _FakeMainWindow()


class _FakeI18n:
    def t(self, key):
        return key


class _FakeMainWindow:
    def __init__(self, **speech):
        self.settings = {"speech_content": speech}
        self.i18n = _FakeI18n()
        self.spoken = []

    def output(self, text, interrupt=False):
        self.spoken.append(text)


def _run_debounced_check(monkeypatch, stub):
    """Runs _schedule_link_preview_check()'s wx.CallLater callback
    synchronously instead of waiting out the real debounce."""
    captured = {}

    def _fake_call_later(delay, func):
        captured["func"] = func
        return types.SimpleNamespace(Stop=lambda: None)

    monkeypatch.setattr(conversations_module.wx, "CallLater", _fake_call_later)
    stub._schedule_link_preview_check()
    captured["func"]()


def _run_fetch_synchronously(monkeypatch):
    """Runs the background-thread fetch inline instead of on a real thread,
    and wx.CallAfter's callback immediately — both so the test doesn't need
    a running wx.App or to wait on anything."""
    class _ImmediateThread:
        def __init__(self, target=None, daemon=None):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr(threading, "Thread", _ImmediateThread)
    monkeypatch.setattr(
        conversations_module.wx, "CallAfter",
        lambda func, *args, **kw: func(*args, **kw),
    )


class TestLinkPreviewComposerFlow:
    def test_url_with_a_preview_shows_the_remove_button(self, monkeypatch):
        stub = _Stub(text="check this out https://example.com")
        _run_fetch_synchronously(monkeypatch)
        patch_conversations_global(monkeypatch, "fetch_link_preview",
            lambda url, **kw: {"title": "Example Domain", "description": "desc",
                                "canonicalUrl": url},
        )
        _run_debounced_check(monkeypatch, stub)

        assert stub._pending_link_preview == {
            "title": "Example Domain", "description": "desc",
            "canonicalUrl": "https://example.com",
        }
        assert stub._remove_link_preview_btn.IsShown() is True

    def test_url_with_no_preview_available_does_not_show_the_button(self, monkeypatch):
        stub = _Stub(text="https://example.com")
        _run_fetch_synchronously(monkeypatch)
        patch_conversations_global(monkeypatch, "fetch_link_preview", lambda url, **kw: None,
        )
        _run_debounced_check(monkeypatch, stub)

        assert stub._pending_link_preview is None
        assert stub._remove_link_preview_btn.IsShown() is False

    def test_no_url_never_triggers_a_fetch(self, monkeypatch):
        stub = _Stub(text="just some text, no link")
        calls = []
        _run_fetch_synchronously(monkeypatch)
        patch_conversations_global(monkeypatch, "fetch_link_preview",
            lambda url, **kw: calls.append(url) or None,
        )
        _run_debounced_check(monkeypatch, stub)
        assert calls == []

    def test_removing_the_preview_clears_state_and_hides_the_button(self, monkeypatch):
        stub = _Stub(text="https://example.com")
        stub._pending_link_preview = {"title": "t", "description": "d", "canonicalUrl": "https://example.com"}
        stub._link_preview_source_url = "https://example.com"
        stub._remove_link_preview_btn.Show()
        monkeypatch.setattr(
            conversations_module.wx, "CallAfter",
            lambda func, *args, **kw: func(*args, **kw),
        )

        stub._on_remove_link_preview()

        assert stub._pending_link_preview is None
        assert stub._remove_link_preview_btn.IsShown() is False
        assert stub._link_preview_dismissed_url == "https://example.com"

    def test_dismissed_url_is_not_refetched_while_still_in_the_field(self, monkeypatch):
        stub = _Stub(text="https://example.com")
        stub._link_preview_dismissed_url = "https://example.com"
        calls = []
        _run_fetch_synchronously(monkeypatch)
        patch_conversations_global(monkeypatch, "fetch_link_preview",
            lambda url, **kw: calls.append(url) or {"title": "t", "description": "", "canonicalUrl": url},
        )
        _run_debounced_check(monkeypatch, stub)
        assert calls == []
        assert stub._remove_link_preview_btn.IsShown() is False

    def test_changing_the_url_after_dismissal_fetches_the_new_one(self, monkeypatch):
        stub = _Stub(text="https://other.example.com")
        stub._link_preview_dismissed_url = "https://example.com"
        _run_fetch_synchronously(monkeypatch)
        patch_conversations_global(monkeypatch, "fetch_link_preview",
            lambda url, **kw: {"title": "t", "description": "", "canonicalUrl": url},
        )
        _run_debounced_check(monkeypatch, stub)
        assert stub._pending_link_preview is not None
        assert stub._remove_link_preview_btn.IsShown() is True

    def test_editing_away_the_url_clears_an_already_resolved_preview(self, monkeypatch):
        stub = _Stub(text="no link here anymore")
        stub._pending_link_preview = {"title": "t", "description": "d", "canonicalUrl": "https://example.com"}
        stub._link_preview_source_url = "https://example.com"
        stub._remove_link_preview_btn.Show()
        _run_fetch_synchronously(monkeypatch)
        patch_conversations_global(monkeypatch, "fetch_link_preview", lambda url, **kw: None)

        _run_debounced_check(monkeypatch, stub)

        assert stub._pending_link_preview is None
        assert stub._remove_link_preview_btn.IsShown() is False

    def test_stale_fetch_result_is_ignored_after_the_field_changed(self, monkeypatch):
        """A slow fetch for an old URL must not clobber state once the user
        already moved on to different text — guarded by the fetch token."""
        stub = _Stub(text="https://example.com")
        stub._link_preview_fetch_token = 5
        stub.message_field._value = "the url is gone now"

        stub._on_link_preview_fetched(
            token=1,  # stale — current token is 5
            url="https://example.com",
            preview={"title": "t", "description": "d", "canonicalUrl": "https://example.com"},
        )
        assert stub._pending_link_preview is None


# ── ConversationsPanel._send_new_text_message() ─────────────────────────────

class _FakeMessagesList:
    def __init__(self):
        self.appended = []

    def Append(self, row):
        self.appended.append(row)

    def GetItemCount(self):
        return len(self.appended)

    def EnsureVisible(self, index):
        pass


class _FakeMessageQueue:
    def __init__(self):
        self.enqueued = []

    def enqueue(self, pm):
        self.enqueued.append(pm)


class _FakeMainWindowForSend:
    def __init__(self):
        self.message_queue = _FakeMessageQueue()
        self.mark_conversation_as_read_calls = []

    def mark_conversation_as_read(self, jid):
        self.mark_conversation_as_read_calls.append(jid)

    def _schedule_set_chats(self):
        pass


class _SendStub:
    _send_new_text_message = ConversationsPanel._send_new_text_message
    _build_mention_payload = ConversationsPanel._build_mention_payload
    _clear_link_preview = ConversationsPanel._clear_link_preview

    def __init__(self, pending_link_preview=None):
        self.main_window = _FakeMainWindowForSend()
        self.message_field = _FakeMessageField()
        self.messages_list = _FakeMessagesList()
        self.conversation_panel = _FakeWidget()
        self._remove_link_preview_btn = _FakeWidget()
        self._sorted_messages = []
        self._quoted_message = None
        self._pending_mentions = []
        self._pending_mention_display_names = {}
        self._pending_link_preview = pending_link_preview
        self._link_preview_source_url = (
            pending_link_preview.get("canonicalUrl", "") if pending_link_preview else ""
        )
        self._link_preview_dismissed_url = ""
        self._link_preview_fetch_token = 0

    def _render_message_line(self, msg):
        return ""

    def _clear_empty_placeholder(self):
        pass

    def _hide_mention_suggestions(self):
        pass

    def _rebuild_mention_pills(self):
        pass

    def _register_virtual_msg(self, virtual_msg):
        pass

    def _on_cancel_reply(self, event=None):
        pass


class TestSendNewTextMessageWithLinkPreview:
    def test_virtual_message_carries_the_preview_fields(self):
        preview = {
            "title": "Example Domain", "description": "desc",
            "canonicalUrl": "https://example.com",
        }
        stub = _SendStub(pending_link_preview=preview)

        stub._send_new_text_message(
            "check this out https://example.com", "5511999999999@s.whatsapp.net"
        )

        virtual_msg = stub._sorted_messages[0]
        assert virtual_msg["messageType"] == "extendedTextMessage"
        ext = virtual_msg["message"]["extendedTextMessage"]
        assert ext["title"] == "Example Domain"
        assert ext["description"] == "desc"
        assert ext["canonicalUrl"] == "https://example.com"
        assert ext["text"] == "check this out https://example.com"

    def test_the_pending_message_carries_the_preview(self):
        preview = {"title": "t", "description": "d", "canonicalUrl": "https://example.com"}
        stub = _SendStub(pending_link_preview=preview)

        stub._send_new_text_message("https://example.com", "5511999999999@s.whatsapp.net")

        pm = stub.main_window.message_queue.enqueued[0]
        assert pm.link_preview == preview

    def test_composer_state_is_reset_after_sending(self):
        preview = {"title": "t", "description": "d", "canonicalUrl": "https://example.com"}
        stub = _SendStub(pending_link_preview=preview)
        stub._remove_link_preview_btn.Show()

        stub._send_new_text_message("https://example.com", "5511999999999@s.whatsapp.net")

        assert stub._pending_link_preview is None
        assert stub._link_preview_dismissed_url == ""
        assert stub._remove_link_preview_btn.IsShown() is False

    def test_plain_message_without_a_preview_stays_a_conversation(self):
        stub = _SendStub(pending_link_preview=None)

        stub._send_new_text_message("just some text", "5511999999999@s.whatsapp.net")

        virtual_msg = stub._sorted_messages[0]
        assert virtual_msg["messageType"] == "conversation"
        assert virtual_msg["message"] == {"conversation": "just some text"}
        pm = stub.main_window.message_queue.enqueued[0]
        assert pm.link_preview is None


# ── spoken feedback: loading, added, failed ─────────────────────────────────

class TestFetchReportsWhyItFailed:
    def _reasons(self, monkeypatch, response=None, error=None):
        seen = []

        def _get(*a, **kw):
            if error is not None:
                raise error
            return response

        monkeypatch.setattr("core.link_preview.requests.get", _get)
        assert fetch_link_preview(
            "https://example.com", report=lambda reason, status=0: seen.append((reason, status))
        ) is None
        return seen

    def test_a_connection_error(self, monkeypatch):
        assert self._reasons(monkeypatch, error=OSError("down")) == [("network", 0)]

    def test_an_http_error_carries_the_status(self, monkeypatch):
        import requests

        error = requests.HTTPError("404")
        error.response = types.SimpleNamespace(status_code=404)
        assert self._reasons(monkeypatch, error=error) == [("http", 404)]

    def test_a_page_that_is_not_html(self, monkeypatch):
        response = _FakeResponse(b"x", content_type="image/png")
        assert self._reasons(monkeypatch, response=response) == [("not_html", 0)]

    def test_a_page_without_title_or_description(self, monkeypatch):
        response = _FakeResponse(b"<html><head></head><body>hi</body></html>")
        assert self._reasons(monkeypatch, response=response) == [("empty", 0)]

    def test_success_reports_nothing(self, monkeypatch):
        monkeypatch.setattr(
            "core.link_preview.requests.get",
            lambda *a, **kw: _FakeResponse(b"<html><head><title>T</title></head></html>"),
        )
        seen = []
        assert fetch_link_preview("https://example.com", report=lambda *a: seen.append(a))
        assert seen == []


class TestAnnouncementText:
    class _I18n:
        TEXTS = {
            "link_preview_loading": "Loading link preview...",
            "link_preview_added": "Link preview added.",
            "link_preview_failed": "Could not load the link preview: {reason}.",
            "link_preview_error_network": "no connection",
            "link_preview_error_http": "error {status}",
            "link_preview_error_not_html": "not a page",
            "link_preview_error_empty": "empty page",
        }

        def t(self, key):
            return self.TEXTS[key]

    def test_each_kind(self):
        from core.link_preview import link_preview_announcement as say

        i18n = self._I18n()
        assert say(i18n, "loading") == "Loading link preview..."
        assert say(i18n, "added") == "Link preview added."
        assert say(i18n, "failed", "http", 503) == "Could not load the link preview: error 503."
        assert say(i18n, "failed", "not_html") == "Could not load the link preview: not a page."
        assert say(i18n, "failed", "empty") == "Could not load the link preview: empty page."
        assert say(i18n, "failed", "network") == "Could not load the link preview: no connection."
        assert say(i18n, "failed") == "Could not load the link preview: no connection."

    def test_the_setting_defaults_on_and_off_silences_everything(self):
        from core.link_preview import announce_link_previews_enabled as enabled
        from core.utils import DEFAULT_SETTINGS

        assert DEFAULT_SETTINGS["speech_content"]["announce_link_previews"] is True
        assert enabled({}) is True
        assert enabled({"speech_content": {}}) is True
        assert enabled({"speech_content": {"announce_link_previews": False}}) is False


class TestComposerSpeaks:
    def _typed(self, monkeypatch, fetch, **speech):
        stub = _Stub(text="look https://example.com")
        stub.main_window = _FakeMainWindow(**speech)
        _run_fetch_synchronously(monkeypatch)
        patch_conversations_global(monkeypatch, "fetch_link_preview", fetch)
        _run_debounced_check(monkeypatch, stub)
        return stub.main_window.spoken

    def test_loading_then_added(self, monkeypatch):
        spoken = self._typed(monkeypatch, lambda url, **kw: {
            "title": "t", "description": "", "canonicalUrl": url})
        assert spoken == ["link_preview_loading", "link_preview_added"]

    def test_loading_then_the_reason_it_failed(self, monkeypatch):
        def _fail(url, report=None, **kw):
            report("http", 404)

        spoken = self._typed(monkeypatch, _fail)
        assert spoken == ["link_preview_loading", "link_preview_failed"]

    def test_the_setting_off_says_nothing(self, monkeypatch):
        spoken = self._typed(monkeypatch, lambda url, **kw: None,
                             announce_link_previews=False)
        assert spoken == []

    def test_a_superseded_fetch_says_nothing_more(self, monkeypatch):
        stub = _Stub(text="https://example.com")
        stub._link_preview_fetch_token = 5
        stub._on_link_preview_fetched(1, "https://example.com", None, {"reason": "network"})
        assert stub.main_window.spoken == []

    def test_typing_on_after_a_failure_does_not_repeat_the_speech(self, monkeypatch):
        stub = _Stub(text="look https://example.com")
        _run_fetch_synchronously(monkeypatch)
        calls = []

        def _fail(url, report=None, **kw):
            calls.append(url)
            report("network", 0)

        patch_conversations_global(monkeypatch, "fetch_link_preview", _fail)
        _run_debounced_check(monkeypatch, stub)
        stub.message_field.SetValue("look https://example.com and more words")
        _run_debounced_check(monkeypatch, stub)

        assert calls == ["https://example.com"]
        assert stub.main_window.spoken == ["link_preview_loading", "link_preview_failed"]

    def test_a_new_url_is_announced_again(self, monkeypatch):
        stub = _Stub(text="https://example.com")
        _run_fetch_synchronously(monkeypatch)
        patch_conversations_global(monkeypatch, "fetch_link_preview", lambda url, **kw: {
            "title": "t", "description": "", "canonicalUrl": url})
        _run_debounced_check(monkeypatch, stub)
        stub.message_field.SetValue("https://other.example.com")
        _run_debounced_check(monkeypatch, stub)

        assert stub.main_window.spoken == ["link_preview_loading", "link_preview_added"] * 2
