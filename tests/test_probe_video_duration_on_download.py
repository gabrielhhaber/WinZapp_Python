"""Tests for Settings > Armazenamento > "descobrir a duração ao baixar".

A video whose sender omitted the duration reads as a bare "vídeo" until it is
played — playback decodes the file anyway, so _learn_video_duration() fills
the gap there for free (see tests/test_video_duration_unknown.py). This option
trades that wait for one media decode per downloaded video, so the length is
already in the list without opening anything. Off by default: it is wasted
work for anyone who doesn't care, and it costs a full BASS decode of a file
that can be tens of megabytes.

The probe runs on its own thread. handle_media_message() is called from the UI
thread too (the play path downloads on demand before starting playback), so
doing it inline would freeze the window for the length of the decode.

MainWindow is a wx.Frame and cannot be instantiated without a running wx.App,
so the methods under test are bound to a small stub — same approach as
tests/test_lid_merge_keeps_messages.py.
"""

import json
import os
import pathlib
import sys
import types
from unittest.mock import MagicMock

try:
    import wx
    import wx.adv
except ImportError:
    for _mod in ("wx", "wx.adv"):
        if _mod not in sys.modules:
            mod = types.ModuleType(_mod)
            if "." not in _mod:
                mod.__path__ = []
            sys.modules[_mod] = mod
    class _FakeWxModule(types.ModuleType):
        ACC_OK = 0
        ACC_NOT_IMPLEMENTED = -1
        def __getattr__(self, name):
            if name == "__file__":
                return "<fake_wx>"
            if name == "CallAfter":
                return lambda fn, *a, **k: fn(*a, **k)
            if name.startswith("ID_") or name.startswith("wxID_") or name in ("HORIZONTAL", "VERTICAL", "EXPAND", "ALL"):
                return 1000
            if name in ("Frame", "Panel", "Dialog", "Accessible", "Timer", "App", "Window", "Control", "Button"):
                return object
            return MagicMock
    sys.modules["wx"].__class__ = _FakeWxModule
    sys.modules["wx.adv"].__class__ = _FakeWxModule
    wx = sys.modules["wx"]

import pytest

import main
from core.utils import MEASURED_SECONDS_KEY
from main import MainWindow
from tests.god_modules import patch_main_global


class _FakeDB:
    def __init__(self):
        self.inserted = []

    def insert_message(self, jid, msg):
        self.inserted.append((jid, msg.get("key", {}).get("id", "")))


class _FakeExecutor:
    def submit(self, fn, *a, **kw):
        fn(*a, **kw)


class _FakePanel:
    def __init__(self, open_jid=None):
        self.conversation = {"remoteJid": open_jid} if open_jid else None
        self.repainted = []

    def _repaint_message_rows(self, msg_ids):
        self.repainted.append(sorted(i for i in msg_ids if i))
        return True


class _SyncThread:
    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target, self._args, self._kwargs = target, args, kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


class _Stub:
    _maybe_probe_video_duration = MainWindow._maybe_probe_video_duration
    _apply_probed_video_duration = MainWindow._apply_probed_video_duration
    # staticmethod on the real class; attribute access unwraps it, so it has
    # to be re-wrapped here.
    _normalize_jid = staticmethod(MainWindow._normalize_jid)

    def __init__(self, enabled=False, open_jid=None):
        self.settings = {"storage": {"probe_video_duration_on_download": enabled}}
        self.db = _FakeDB()
        self._msg_bg_executor = _FakeExecutor()
        self.conversations_panel = _FakePanel(open_jid)
        self.saved = []

    def _schedule_save(self, dirty_jid=None):
        self.saved.append(dirty_jid)


JID = "grupo@g.us"


def _video_msg(seconds=0):
    return {
        "key": {"id": "VID1", "remoteJid": JID, "fromMe": False},
        "messageType": "videoMessage",
        "message": {"videoMessage": {"seconds": seconds, "mimetype": "video/mp4"}},
    }


@pytest.fixture
def probed(monkeypatch):
    """Record every path handed to the probe and answer with a set length."""
    calls = []

    def _probe(path, answer=[137]):
        calls.append(path)
        return answer[0]

    patch_main_global(monkeypatch, "probe_media_duration", _probe)
    monkeypatch.setattr(main.threading, "Thread", _SyncThread)
    monkeypatch.setattr(main.wx, "CallAfter", lambda fn, *a, **kw: fn(*a, **kw))
    return calls


class TestTheSettingGatesIt:
    def test_disabled_by_default_nothing_is_probed(self, probed):
        stub = _Stub(enabled=False)
        msg = _video_msg()

        stub._maybe_probe_video_duration(msg, b"fake mp4 bytes")

        assert probed == []
        assert msg["message"]["videoMessage"]["seconds"] == 0

    def test_a_missing_storage_section_is_treated_as_disabled(self, probed):
        stub = _Stub()
        stub.settings = {}

        stub._maybe_probe_video_duration(_video_msg(), b"bytes")

        assert probed == []

    def test_enabled_probes_and_stores_the_length(self, probed):
        stub = _Stub(enabled=True)
        msg = _video_msg()

        stub._maybe_probe_video_duration(msg, b"fake mp4 bytes")

        assert len(probed) == 1
        assert msg["message"]["videoMessage"][MEASURED_SECONDS_KEY] == 137


class TestWhatIsWorthProbing:
    def test_a_video_that_already_states_its_length_is_skipped(self, probed):
        stub = _Stub(enabled=True)
        msg = _video_msg(seconds=29)

        stub._maybe_probe_video_duration(msg, b"bytes")

        assert probed == []
        assert msg["message"]["videoMessage"]["seconds"] == 29

    def test_non_video_media_is_skipped(self, probed):
        stub = _Stub(enabled=True)
        msg = {"key": {"id": "IMG1", "remoteJid": JID},
               "messageType": "imageMessage",
               "message": {"imageMessage": {"caption": ""}}}

        stub._maybe_probe_video_duration(msg, b"bytes")

        assert probed == []

    def test_a_probe_that_answers_nothing_leaves_the_record_alone(self, monkeypatch):
        patch_main_global(monkeypatch, "probe_media_duration", lambda path: None)
        monkeypatch.setattr(main.threading, "Thread", _SyncThread)
        monkeypatch.setattr(main.wx, "CallAfter", lambda fn, *a, **kw: fn(*a, **kw))
        stub = _Stub(enabled=True)
        msg = _video_msg()

        stub._maybe_probe_video_duration(msg, b"bytes")

        assert MEASURED_SECONDS_KEY not in msg["message"]["videoMessage"]
        assert stub.db.inserted == []

    def test_the_temporary_file_is_cleaned_up(self, probed):
        stub = _Stub(enabled=True)

        stub._maybe_probe_video_duration(_video_msg(), b"bytes")

        assert len(probed) == 1
        assert not pathlib.Path(probed[0]).exists(), (
            "a video can be tens of megabytes — the temp copy must not be left behind"
        )


class TestApplyingTheResult:
    def test_the_record_is_persisted_and_the_chat_marked_dirty(self, probed):
        stub = _Stub(enabled=True)

        stub._maybe_probe_video_duration(_video_msg(), b"bytes")

        assert stub.db.inserted == [(JID, "VID1")]
        assert stub.saved == [JID]

    def test_the_open_conversation_row_is_repainted(self, probed):
        stub = _Stub(enabled=True, open_jid=JID)

        stub._maybe_probe_video_duration(_video_msg(), b"bytes")

        assert stub.conversations_panel.repainted == [["VID1"]]

    def test_another_open_conversation_is_not_touched(self, probed):
        stub = _Stub(enabled=True, open_jid="outro@s.whatsapp.net")

        stub._maybe_probe_video_duration(_video_msg(), b"bytes")

        assert stub.conversations_panel.repainted == []

    def test_a_length_filled_in_meanwhile_is_not_overwritten(self):
        """Playback probes the same file (_learn_video_duration). Whichever
        lands first wins; the other must not rewrite the record."""
        stub = _Stub(enabled=True)
        msg = _video_msg(seconds=42)

        stub._apply_probed_video_duration(msg, 137)

        assert MEASURED_SECONDS_KEY not in msg["message"]["videoMessage"]
        assert msg["message"]["videoMessage"]["seconds"] == 42
        assert stub.db.inserted == []


def test_the_setting_ships_in_the_defaults():
    defaults = json.loads(
        (pathlib.Path(__file__).resolve().parents[1] / "client" / "data" /
         "settings_default.json").read_text(encoding="utf-8")
    )
    assert defaults["storage"]["probe_video_duration_on_download"] is False
