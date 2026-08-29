import os
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

try:
    import accessible_output2
    from accessible_output2 import outputs
except ImportError:
    if "accessible_output2" not in sys.modules:
        sys.modules["accessible_output2"] = types.ModuleType("accessible_output2")
    sys.modules["accessible_output2.outputs"] = types.ModuleType("accessible_output2.outputs")
    sys.modules["accessible_output2"].outputs = sys.modules["accessible_output2.outputs"]

try:
    import sound_lib
    from sound_lib import stream, output, main, effects
except ImportError:
    for _mod in (
        "sound_lib",
        "sound_lib.output",
        "sound_lib.stream",
        "sound_lib.main",
        "sound_lib.effects",
    ):
        if _mod not in sys.modules:
            mod = types.ModuleType(_mod)
            if "." not in _mod:
                mod.__path__ = []
            sys.modules[_mod] = mod

    sys.modules["sound_lib.main"].bass_call = lambda *a, **k: None
    sys.modules["sound_lib.stream"].FileStream = object
    sys.modules["sound_lib.output"].Output = object
    sys.modules["sound_lib.effects"].Tempo = object

import ui.conversations as conversations_module
from core.accessible_speech import AccessibleSpeechOutput
from ui.accessible import (
    AccessibleDiscardVoiceMessage,
    AccessiblePauseResumeRecording,
    AccessibleSendVoiceMessage,
)
from ui.conversations import ConversationsPanel


class _FakeOutput:
    def __init__(self):
        self.spoken = []
        self.silenced = False

    def speak(self, text, **options):
        self.spoken.append(text)

    def silence(self):
        self.silenced = True


class _FakeAuto:
    def __init__(self, output):
        self._output = output
        self.outputs = [output]

    def get_first_available_output(self):
        return self._output


class TestAccessibleSpeechOutputSuppression:
    def test_output_dropped_while_suppressed(self):
        fake = _FakeOutput()
        speech = AccessibleSpeechOutput(
            _FakeAuto(fake), lambda: {}, suppressed_getter=lambda: True
        )
        speech.output("hello")
        assert fake.spoken == []

    def test_output_spoken_when_not_suppressed(self):
        fake = _FakeOutput()
        speech = AccessibleSpeechOutput(
            _FakeAuto(fake), lambda: {}, suppressed_getter=lambda: False
        )
        speech.output("hello")
        assert fake.spoken == ["hello"]

    def test_output_spoken_when_no_suppressed_getter_given(self):
        fake = _FakeOutput()
        speech = AccessibleSpeechOutput(_FakeAuto(fake), lambda: {})
        speech.output("hello")
        assert fake.spoken == ["hello"]

    def test_silence_bypasses_suppression(self):
        fake = _FakeOutput()
        speech = AccessibleSpeechOutput(
            _FakeAuto(fake), lambda: {}, suppressed_getter=lambda: True
        )
        speech.silence()
        assert fake.silenced is True

    def test_silence_noop_when_output_lacks_silence(self):
        class _NoSilenceOutput:
            def speak(self, text, **options):
                pass

        speech = AccessibleSpeechOutput(_FakeAuto(_NoSilenceOutput()), lambda: {})
        speech.silence()  # must not raise

    def test_silence_is_a_no_op_when_extended_sr_compat_disabled(self):
        """This asserted the opposite, and the opposite was the bug.

        extended_sr_compat_enabled off means WinZapp never calls into
        accessible_output2 at all (see AccessibleSpeechOutput's docstring).
        silence() reaches into the screen reader to cancel speech already in
        flight — including speech WinZapp never produced — so honouring it
        while the master switch is off cut off NVDA for a user who had asked
        the app to stay out of their screen reader entirely."""
        fake = _FakeOutput()
        speech = AccessibleSpeechOutput(
            _FakeAuto(fake),
            lambda: {"accessibility": {"extended_sr_compat_enabled": False}},
        )
        speech.silence()
        assert fake.silenced is False


class TestVoiceRecordingSilenceActive:
    def _make_stub(self, silence_setting, is_recording, has_panel=True):
        stub = types.SimpleNamespace()
        stub.settings = {"speech_content": {"silence_while_recording": silence_setting}}
        if has_panel:
            stub.conversations_panel = types.SimpleNamespace(_is_recording=is_recording)
        from main import MainWindow
        stub._voice_recording_silence_active = types.MethodType(
            MainWindow._voice_recording_silence_active, stub
        )
        return stub

    def test_false_when_setting_disabled(self):
        stub = self._make_stub(silence_setting=False, is_recording=True)
        assert stub._voice_recording_silence_active() is False

    def test_false_when_setting_enabled_but_not_recording(self):
        stub = self._make_stub(silence_setting=True, is_recording=False)
        assert stub._voice_recording_silence_active() is False

    def test_true_when_setting_enabled_and_recording(self):
        stub = self._make_stub(silence_setting=True, is_recording=True)
        assert stub._voice_recording_silence_active() is True

    def test_false_when_no_conversations_panel_yet(self):
        stub = self._make_stub(silence_setting=True, is_recording=True, has_panel=False)
        assert stub._voice_recording_silence_active() is False


class TestSilenceSendVoiceFocusIfEnabled:
    class _FakeSpeakOutput:
        def __init__(self):
            self.focus_silence_calls = 0

        def silence_screen_reader_focus(self):
            self.focus_silence_calls += 1

    class _FakeMainWindow:
        def __init__(self, silence_enabled=False, extended_enabled=True):
            self.settings = {
                "speech_content": {"silence_while_recording": silence_enabled},
                "accessibility": {"extended_sr_compat_enabled": extended_enabled},
            }
            self.speak_output = TestSilenceSendVoiceFocusIfEnabled._FakeSpeakOutput()

    def _make_stub(self, silence_enabled=False, extended_enabled=True):
        stub = types.SimpleNamespace()
        stub.main_window = self._FakeMainWindow(silence_enabled, extended_enabled)
        stub._silence_send_voice_focus_if_enabled = types.MethodType(
            ConversationsPanel._silence_send_voice_focus_if_enabled, stub
        )
        return stub

    @staticmethod
    def _capture_deferred_calls(monkeypatch):
        deferred = []
        monkeypatch.setattr(
            conversations_module.wx, "CallAfter",
            lambda func: deferred.append((0, func)),
        )
        monkeypatch.setattr(
            conversations_module.wx, "CallLater",
            lambda delay, func: deferred.append((delay, func)),
        )
        return deferred

    def test_noop_when_silence_setting_is_off_and_extended_compat_is_on(self, monkeypatch):
        deferred = self._capture_deferred_calls(monkeypatch)
        stub = self._make_stub(silence_enabled=False, extended_enabled=True)

        stub._silence_send_voice_focus_if_enabled()

        assert stub.main_window.speak_output.focus_silence_calls == 0
        assert deferred == []

    def test_fires_when_silence_while_recording_is_enabled(self, monkeypatch):
        deferred = self._capture_deferred_calls(monkeypatch)
        stub = self._make_stub(silence_enabled=True, extended_enabled=True)

        stub._silence_send_voice_focus_if_enabled()

        assert stub.main_window.speak_output.focus_silence_calls == 1
        assert [delay for delay, _ in deferred] == [0, 80]
        for _, func in deferred:
            func()
        assert stub.main_window.speak_output.focus_silence_calls == 3

    def test_does_not_fire_merely_because_extended_sr_compat_is_off(self, monkeypatch):
        """Turning extended screen-reader compatibility OFF means "stop talking
        to my screen reader", not "start interrupting it". Only the dedicated
        silence-while-recording toggle may cancel the focus announcement."""
        deferred = self._capture_deferred_calls(monkeypatch)
        stub = self._make_stub(silence_enabled=False, extended_enabled=False)

        stub._silence_send_voice_focus_if_enabled()

        assert stub.main_window.speak_output.focus_silence_calls == 0
        assert deferred == []

    def test_fires_on_the_silence_toggle_even_with_extended_compat_off(self, monkeypatch):
        """The two settings are independent: the silence toggle is what arms
        this, whatever extended compatibility is set to."""
        deferred = self._capture_deferred_calls(monkeypatch)
        stub = self._make_stub(silence_enabled=True, extended_enabled=False)

        stub._silence_send_voice_focus_if_enabled()

        assert stub.main_window.speak_output.focus_silence_calls == 1
        for _, func in deferred:
            func()
        assert stub.main_window.speak_output.focus_silence_calls == 3


class TestVoiceButtonAccessibleName:
    """Voice-recording controls keep their real MSAA name and shortcut.

    Muting is a one-shot focus action now; it must never make the Send,
    Discard or Pause/Resume controls anonymous while the user navigates them.
    """

    class _FakeMainWindow:
        def __init__(self, silence_enabled=False, extended_enabled=True):
            self.settings = {
                "speech_content": {"silence_while_recording": silence_enabled},
                "accessibility": {"extended_sr_compat_enabled": extended_enabled},
            }

    def test_name_and_shortcut_remain_available_for_every_setting_combination(self):
        import wx

        for silence_enabled in (False, True):
            for extended_enabled in (False, True):
                main_window = self._FakeMainWindow(silence_enabled, extended_enabled)
                send = AccessibleSendVoiceMessage(main_window)
                discard = AccessibleDiscardVoiceMessage(main_window)
                pause = AccessiblePauseResumeRecording(main_window)

                assert send.GetName(0) == (wx.ACC_NOT_IMPLEMENTED, "")
                assert discard.GetName(0) == (wx.ACC_NOT_IMPLEMENTED, "")
                assert pause.GetName(0) == (wx.ACC_NOT_IMPLEMENTED, "")
                assert send.GetKeyboardShortcut(0) == (wx.ACC_OK, "Ctrl+R")
                assert discard.GetKeyboardShortcut(0) == (wx.ACC_OK, "Ctrl+Shift+D")
                assert pause.GetKeyboardShortcut(0) == (wx.ACC_OK, "Ctrl+Shift+P")

