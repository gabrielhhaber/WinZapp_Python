"""Regression coverage for ConversationsPanel._start_voice_recording()'s
graceful degradation when PyAudio isn't installed (no wheel exists for it on
Python 3.14 at the time of writing — see pyproject.toml's version marker
and conversations.py's own `import pyaudio` try/except).

ConversationsPanel is a wx.Panel and can't be instantiated without a running
wx.App, so the method under test is bound onto a plain stub carrying only the
attributes it touches, matching the pattern used throughout this test suite
(see test_sender_names.py).
"""

import ui.conversations as conversations_module
from ui.conversations import ConversationsPanel


class _FakeI18n:
    def t(self, key):
        return key


class _FakeMainWindow:
    def __init__(self):
        self.i18n = _FakeI18n()
        self.output_calls = []

    def output(self, text):
        self.output_calls.append(text)


class _Stub:
    _start_voice_recording = ConversationsPanel._start_voice_recording

    def __init__(self):
        self.conversation = {"id": "jid@w"}
        self.main_window = _FakeMainWindow()


def test_no_op_when_no_conversation_is_open():
    stub = _Stub()
    stub.conversation = None
    stub._start_voice_recording()
    assert stub.main_window.output_calls == []


def test_reports_unavailable_message_when_pyaudio_missing(monkeypatch):
    monkeypatch.setattr(conversations_module, "pyaudio", None)
    stub = _Stub()
    stub._start_voice_recording()
    assert stub.main_window.output_calls == ["voice_recording_unavailable"]
    # Must return before touching any recording state.
    assert not hasattr(stub, "_recording_frames")
