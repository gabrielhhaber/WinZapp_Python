"""Issue #65: a message with 2+ links exposed each one as its own separate
Tab stop, forcing users to Tab/Shift+Tab through every one. Two or more are
now shown as a single navigable wx.ListCtrl instead — Up/Down move between
them (Home/End come for free from the native control), Enter/Space opens
the focused one, and Ctrl+C copies just its URL. A single link keeps the
previous plain HyperlinkCtrl behaviour unchanged.

ConversationsPanel is a wx.Panel and can't be instantiated without a
running wx.App, so the pure extraction helper and the new key-handling
methods are exercised directly / against a small stub — same approach used
throughout this suite.
"""

import wx

from ui.conversations import ConversationsPanel


class TestExtractLinks:
    def test_single_link(self):
        assert ConversationsPanel._extract_links("check this out https://example.com/x") == [
            "https://example.com/x"
        ]

    def test_multiple_links_in_order(self):
        text = "https://a.com https://b.com https://c.com"
        assert ConversationsPanel._extract_links(text) == [
            "https://a.com", "https://b.com", "https://c.com",
        ]

    def test_duplicates_are_collapsed(self):
        text = "https://a.com see https://a.com again"
        assert ConversationsPanel._extract_links(text) == ["https://a.com"]

    def test_trailing_punctuation_is_stripped(self):
        text = "Look: https://example.com/page, and (https://other.com)."
        assert ConversationsPanel._extract_links(text) == [
            "https://example.com/page", "https://other.com",
        ]

    def test_no_links_returns_empty_list(self):
        assert ConversationsPanel._extract_links("no links here") == []


class _FakeListCtrl:
    def __init__(self, focused=0):
        self._focused = focused

    def GetFirstSelected(self):
        return self._focused


class _FakeMainWindow:
    def __init__(self):
        self.i18n = _FakeI18n()
        self.announced = []

    def output(self, text, interrupt=False):
        self.announced.append(text)


class _FakeI18n:
    def t(self, key):
        return key


class _FakeKeyEvent:
    def __init__(self, key_code, ctrl=False):
        self._key_code = key_code
        self._ctrl = ctrl
        self.skipped = False

    def GetKeyCode(self):
        return self._key_code

    def ControlDown(self):
        return self._ctrl

    def Skip(self):
        self.skipped = True


class _Stub:
    _on_links_list_key_down  = ConversationsPanel._on_links_list_key_down
    _on_links_list_activated = ConversationsPanel._on_links_list_activated
    # The Ctrl+C branch now routes through the shared pair that also
    # serves the accelerator and the context menu — see
    # tests/test_focused_link_actions.py.
    _link_url_for            = ConversationsPanel._link_url_for
    _copy_focused_link       = ConversationsPanel._copy_focused_link

    def __init__(self, links, focused=0):
        self.main_window = _FakeMainWindow()
        self._current_links = list(links)
        self._links_list = _FakeListCtrl(focused=focused)
        self.opened = []

    def _open_link(self, url):
        self.opened.append(url)


LINKS = ["https://a.com", "https://b.com", "https://c.com"]


class TestLinksListKeyHandling:
    def test_enter_opens_the_focused_link(self, monkeypatch):
        stub = _Stub(LINKS, focused=1)
        event = _FakeKeyEvent(wx.WXK_RETURN)

        stub._on_links_list_key_down(event)

        assert stub.opened == ["https://b.com"]

    def test_space_also_opens_the_focused_link(self):
        stub = _Stub(LINKS, focused=2)
        event = _FakeKeyEvent(wx.WXK_SPACE)

        stub._on_links_list_key_down(event)

        assert stub.opened == ["https://c.com"]

    def test_ctrl_c_copies_only_the_focused_link(self, monkeypatch):
        copied = []
        monkeypatch.setattr("ui.conversations.pyperclip.copy", lambda t: copied.append(t))
        stub = _Stub(LINKS, focused=1)
        event = _FakeKeyEvent(ord("C"), ctrl=True)

        stub._on_links_list_key_down(event)

        assert copied == ["https://b.com"]
        assert stub.main_window.announced == ["link_copied"]
        assert stub.opened == []

    def test_an_unrelated_key_is_skipped(self):
        stub = _Stub(LINKS, focused=0)
        event = _FakeKeyEvent(ord("X"))

        stub._on_links_list_key_down(event)

        assert event.skipped is True
        assert stub.opened == []

    def test_activated_event_opens_the_link_at_that_index(self):
        stub = _Stub(LINKS)

        class _FakeActivateEvent:
            def GetIndex(self):
                return 2

        stub._on_links_list_activated(_FakeActivateEvent())

        assert stub.opened == ["https://c.com"]
