"""Tests that a backgrounded message is announced exactly once.

Reported live: with WinZapp in the background, a single incoming message
arrived twice — first as "Nova mensagem de Vinícius: ..." spoken straight
through accessible_output2, and then again as the screen reader read the
Windows toast banner that had just appeared. Only the toast was wanted.

The AO2 call was added back when the banner could silently never render at
all (dev mode used an unregistered AUMID — see
NotificationManager._setup_toaster()). With the toast now showing reliably
from source and from a frozen build alike, speaking it unconditionally is
pure duplication: every screen reader reads the banner by itself.

So AO2 became a fallback rather than a companion, split across the two
places that can actually know a banner will not appear:

* ``should_speak_background_message()`` — no toast is even attempted (the
  tray icon, and with it the toast, is off; or there is no
  NotificationManager on the window).
* ``NotificationManager._dispatch()`` — a toast was attempted and produced
  nothing (no toaster at all, or ``show_toast()`` raised).

NotificationManager touches the registry and starts a worker thread in
__init__, so _dispatch() is exercised against a stub carrying only the
attributes it uses — same approach as tests/test_notifications.py.
"""

import queue

import pytest
import wx

from core.notification_manager import (
    NotificationManager,
    announce_background_message,
    should_speak_background_message,
)


class _FakeI18n:
    def get_language(self):
        pass

    def t(self, key):
        if key == "fg_new_msg":
            return "Nova mensagem de {name}"
        return f"[{key}]"


class _FakeMainWindow:
    def __init__(self, settings=None, chats=None):
        self.settings = {} if settings is None else settings
        self.chats = {} if chats is None else chats
        self.spoken = []

    def output(self, text, interrupt=False):
        self.spoken.append(text)


@pytest.fixture
def direct_callafter(monkeypatch):
    """announce_background_message() marshals onto the wx main thread; run it
    inline so the assertion sees the call."""
    monkeypatch.setattr(wx, "CallAfter", lambda fn, *a, **kw: fn(*a, **kw))
    monkeypatch.setattr("core.quiet_hours.is_quiet_hours_active", lambda: False)


class TestShouldSpeakBackgroundMessage:
    def test_a_toast_will_be_shown_so_ao2_stays_quiet(self):
        """The reported bug, pinned: the normal case must speak nothing."""
        assert should_speak_background_message({}, True) is False

    def test_tray_icon_off_means_no_toast_so_ao2_speaks(self):
        settings = {"general": {"show_tray_icon": False}}
        assert should_speak_background_message(settings, True) is True

    def test_no_notification_manager_means_ao2_speaks(self):
        assert should_speak_background_message({}, False) is True

    def test_tray_icon_explicitly_on_still_leaves_it_to_the_toast(self):
        settings = {"general": {"show_tray_icon": True}}
        assert should_speak_background_message(settings, True) is False

    def test_both_off_still_speaks_once(self):
        settings = {"general": {"show_tray_icon": False}}
        assert should_speak_background_message(settings, False) is True


class TestAnnounceBackgroundMessage:
    def test_speaks_sender_and_body(self, direct_callafter):
        mw = _FakeMainWindow()
        announce_background_message(mw, _FakeI18n(), "Vinícius", "bom dia")
        assert mw.spoken == ["Nova mensagem de Vinícius: bom dia"]

    def test_honours_speak_other_conv_messages_off(self, direct_callafter):
        mw = _FakeMainWindow({"speech_content": {"speak_other_conv_messages": False}})
        announce_background_message(mw, _FakeI18n(), "Vinícius", "bom dia")
        assert mw.spoken == []

    def test_defaults_to_speaking_when_the_setting_is_absent(self, direct_callafter):
        mw = _FakeMainWindow({"speech_content": {}})
        announce_background_message(mw, _FakeI18n(), "Ana", "oi")
        assert mw.spoken == ["Nova mensagem de Ana: oi"]

    def test_a_broken_window_never_propagates(self, direct_callafter):
        class _Broken:
            settings = {}

            def output(self, text, interrupt=False):
                raise RuntimeError("screen reader gone")

        announce_background_message(_Broken(), _FakeI18n(), "Ana", "oi")  # must not raise


class _Toaster:
    def __init__(self, fail_show=False):
        self.shown = []
        self.fail_show = fail_show

    def remove_toast_group(self, group):
        pass

    def remove_toast(self, toast):
        pass

    def show_toast(self, toast):
        if self.fail_show:
            raise RuntimeError("winrt pipeline unavailable")
        self.shown.append(toast)


class _Stub:
    TOAST_TAG = NotificationManager.TOAST_TAG
    TOAST_GRP = NotificationManager.TOAST_GRP
    _TOAST_LIKELY_GONE_SECONDS = NotificationManager._TOAST_LIKELY_GONE_SECONDS
    _TOAST_REACTIONS = NotificationManager._TOAST_REACTIONS

    _dispatch            = NotificationManager._dispatch
    _announce_unshown    = NotificationManager._announce_unshown
    _clear_active_toasts = NotificationManager._clear_active_toasts

    def _play_sound(self, remote_jid=""):
        pass

    def __init__(self, toaster=None):
        self._queue = queue.Queue()
        self._toaster = toaster
        self._last_toast = None
        self._last_shown_at = None
        self._interactable = False
        self.i18n = _FakeI18n()
        self.main_window = _FakeMainWindow()


class TestDispatchOnlySpeaksWhenNoBannerAppeared:
    def test_a_shown_toast_is_not_also_spoken(self, direct_callafter):
        """The duplication itself: banner on screen AND AO2 speech."""
        toaster = _Toaster()
        mgr = _Stub(toaster)

        mgr._dispatch("Vinícius", "bom dia", "j@s.whatsapp.net")

        assert len(toaster.shown) == 1
        assert mgr.main_window.spoken == []

    def test_no_toaster_at_all_falls_back_to_speech(self, direct_callafter):
        """_setup_toaster() exhausted every AUMID candidate (or
        windows_toasts is not importable): nothing will ever be on screen for
        a screen reader to read."""
        mgr = _Stub(None)

        mgr._dispatch("Vinícius", "bom dia", "j@s.whatsapp.net")

        assert mgr.main_window.spoken == ["Nova mensagem de Vinícius: bom dia"]

    def test_a_failing_show_toast_falls_back_to_speech(self, direct_callafter):
        toaster = _Toaster(fail_show=True)
        mgr = _Stub(toaster)

        mgr._dispatch("Vinícius", "bom dia", "j@s.whatsapp.net")

        assert toaster.shown == []
        assert mgr.main_window.spoken == ["Nova mensagem de Vinícius: bom dia"]

    def test_the_fallback_still_honours_the_speech_setting(self, direct_callafter):
        mgr = _Stub(None)
        mgr.main_window.settings = {
            "speech_content": {"speak_other_conv_messages": False}
        }

        mgr._dispatch("Vinícius", "bom dia", "j@s.whatsapp.net")

        assert mgr.main_window.spoken == []
