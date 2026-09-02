"""Tests for ConversationsPanel.on_audio_timer()'s "audio reached the end"
branch calling MainWindow.mark_audio_message_played().

Feature: reaching the end of in-app audio playback — the same moment the
playback controls get hidden — must mark a received voice message as
played (locally + a real receipt to WhatsApp, see
tests/test_mark_audio_played.py for that half). Must never fire for a
message that isn't found in the currently-open conversation's message list
(e.g. it scrolled out / the conversation changed underneath playback).

ConversationsPanel is a wx.Panel and can't be instantiated without a
running wx.App, so on_audio_timer() is bound onto a plain stub — same
approach as tests/test_conversation_video_playback.py's _ControlsStub.
"""

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
    class _FakeWindow:
        @staticmethod
        def FindFocus():
            return None

    class _FakeWxModule(types.ModuleType):
        ACC_OK = 0
        ACC_NOT_IMPLEMENTED = -1
        Window = _FakeWindow
        def __getattr__(self, name):
            if name == "__file__":
                return "<fake_wx>"
            if name == "CallAfter":
                return lambda fn, *a, **k: fn(*a, **k)
            if name.startswith("ID_") or name.startswith("wxID_") or name in ("HORIZONTAL", "VERTICAL", "EXPAND", "ALL"):
                return 1000
            if name in ("Frame", "Panel", "Dialog", "Accessible", "Timer", "App", "Control", "Button"):
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
from ui.conversations import ConversationsPanel


class _FakeAudioStream:
    def __init__(self, position, length):
        self._position = position
        self._length = length

    def get_position(self):
        return self._position

    def get_length(self):
        return self._length


class _FakeSlider:
    def SetValue(self, value):
        pass

    def Refresh(self):
        pass


class _FakeMainWindow:
    def __init__(self):
        self.mark_played_calls = []
        self.skip_panel_refresh_calls = []

    def mark_audio_message_played(self, msg, skip_panel_refresh=False):
        self.mark_played_calls.append(msg)
        self.skip_panel_refresh_calls.append(skip_panel_refresh)


def _audio_msg(msg_id, from_me=False, ptt=False):
    return {
        "key": {"id": msg_id, "fromMe": from_me},
        "messageType": "audioMessage",
        "message": {"audioMessage": {"ptt": ptt}},
    }


class _Stub:
    on_audio_timer = ConversationsPanel.on_audio_timer
    _next_message_is_chainable_audio = ConversationsPanel._next_message_is_chainable_audio
    _is_separator = ConversationsPanel._is_separator
    _is_voice_message = ConversationsPanel._is_voice_message

    def __init__(self, sorted_messages, current_audio_id, position, length):
        self.main_window = _FakeMainWindow()
        self._sorted_messages = sorted_messages
        self._current_video_msg_id = None
        self._current_audio_id = current_audio_id
        self._audio_stream = _FakeAudioStream(position, length)
        self._audio_tempo_ctrl = None
        self.audio_slider = _FakeSlider()
        self._in_auto_timer_stop = False
        self.conversation = None
        self._audio_conv_jid = ""
        self.stop_audio_calls = 0
        self.hide_audio_controls_calls = 0
        self.auto_chain_calls = []
        self.hold_armed_calls = []

    def _stop_audio(self):
        self.stop_audio_calls += 1
        self._current_audio_id = None

    def _hide_audio_controls(self):
        self.hide_audio_controls_calls += 1

    def _auto_chain_next_audio(self, finished_id, pending_played_msg_id=None):
        self.auto_chain_calls.append((finished_id, pending_played_msg_id))

    def _hold_status_repaints_until_chain_ends(self):
        # Real method is a one-line flag set; recorded here so the tests can
        # assert the hold is armed BEFORE mark_audio_message_played() runs —
        # the played receipt it sends echoes back on its own schedule.
        self.hold_armed_calls.append(len(self.main_window.mark_played_calls))


class TestAudioFinishMarksPlayed:
    def test_reaching_the_end_marks_the_finished_message_played(self):
        msg = _audio_msg("m1")
        stub = _Stub([msg], current_audio_id="m1", position=1000, length=1000)

        stub.on_audio_timer(None)

        assert stub.main_window.mark_played_calls == [msg]
        assert stub.main_window.skip_panel_refresh_calls == [False]
        assert stub.stop_audio_calls == 1
        assert stub.hide_audio_controls_calls == 1
        assert stub.auto_chain_calls == [("m1", None)]

    def test_still_playing_does_not_mark_anything(self):
        msg = _audio_msg("m1")
        stub = _Stub([msg], current_audio_id="m1", position=500, length=1000)

        stub.on_audio_timer(None)

        assert stub.main_window.mark_played_calls == []
        assert stub.stop_audio_calls == 0

    def test_finished_message_not_in_the_open_conversation_is_skipped_safely(self):
        """The finished id doesn't match anything in _sorted_messages (e.g.
        conversation changed underneath playback) — must not crash, and
        must not call mark_audio_message_played with nothing to mark."""
        stub = _Stub([], current_audio_id="m1", position=1000, length=1000)

        stub.on_audio_timer(None)  # must not raise

        assert stub.main_window.mark_played_calls == []
        assert stub.stop_audio_calls == 1

    def test_own_sent_audio_message_is_still_passed_through(self):
        """The from_me exclusion lives in mark_audio_message_played() itself
        (tests/test_mark_audio_played.py) — on_audio_timer() just calls
        through unconditionally whenever a message is found."""
        msg = _audio_msg("m1", from_me=True)
        stub = _Stub([msg], current_audio_id="m1", position=1000, length=1000)

        stub.on_audio_timer(None)

        assert stub.main_window.mark_played_calls == [msg]
        assert stub.main_window.skip_panel_refresh_calls == [False]


class TestPendingPlayedRefreshHandoffWhenChainingIntoTheNextVoiceNote:
    """Reported live, twice: playing several voice notes back to back, NVDA
    kept announcing the just-finished note's "played" status-icon change
    before it got to announce focus landing on the next one. A first fix
    delayed the refresh by a fixed 300ms timeout — still lost the race in
    practice, because the real gap before the chain moves focus isn't a
    fixed number. The handoff to _auto_chain_next_audio() asserted here was
    the second attempt, and it did not fix it either: refresh_message_status()
    only queues the row behind a coalescing timer, and the played receipt this
    very mark sends echoes back from WhatsApp onto a path that repaints the row
    without touching the chain at all.

    What actually silences it is ConversationsPanel._release_chain_held_repaints()
    — no row is written while the chain is moving focus, full stop. See
    tests/test_audio_chain_played_repaint_hold.py, which measures the real
    ordering. The handoff still earns its place: it keeps the queued refresh
    from being dropped, and arming the hold here is what covers the echo."""

    def test_skips_the_refresh_and_hands_it_to_the_chain_when_chainable(self):
        finished = _audio_msg("m1", ptt=True)
        next_note = _audio_msg("m2", ptt=True)
        stub = _Stub([finished, next_note], current_audio_id="m1", position=1000, length=1000)
        stub.conversation = {"remoteJid": "grupo@g.us"}
        stub._audio_conv_jid = "grupo@g.us"

        stub.on_audio_timer(None)

        assert stub.main_window.skip_panel_refresh_calls == [True]
        assert stub.auto_chain_calls == [("m1", "m1")]
        # Armed before the message was marked played, i.e. before anything —
        # including the WhatsApp echo that mark triggers — can queue a repaint.
        assert stub.hold_armed_calls == [0]

    def test_refreshes_immediately_when_nothing_chainable_follows(self):
        finished = _audio_msg("m1", ptt=True)
        stub = _Stub([finished], current_audio_id="m1", position=1000, length=1000)
        stub.conversation = {"remoteJid": "grupo@g.us"}
        stub._audio_conv_jid = "grupo@g.us"

        stub.on_audio_timer(None)

        assert stub.main_window.skip_panel_refresh_calls == [False]
        assert stub.auto_chain_calls == [("m1", None)]

    def test_refreshes_immediately_for_a_generic_audio_file_not_a_voice_note(self):
        """Sequential chaining/transition sounds only apply to PTT voice
        notes, never to generic attached audio — same restriction
        _auto_chain_next_audio() itself already applies."""
        finished = _audio_msg("m1", ptt=False)
        next_note = _audio_msg("m2", ptt=True)
        stub = _Stub([finished, next_note], current_audio_id="m1", position=1000, length=1000)
        stub.conversation = {"remoteJid": "grupo@g.us"}
        stub._audio_conv_jid = "grupo@g.us"

        stub.on_audio_timer(None)

        assert stub.main_window.skip_panel_refresh_calls == [False]
        assert stub.auto_chain_calls == [("m1", None)]


class TestHideAudioControlsFocus:
    class _FakeControl:
        def __init__(self):
            self.hidden = False
            self.focused = False

        def Hide(self):
            self.hidden = True

        def SetFocus(self):
            self.focused = True

        def IsShown(self):
            return not self.hidden

        def Layout(self):
            pass

    def test_restores_focus_to_messages_list_when_audio_speed_btn_focused(self, monkeypatch):
        speed_btn = self._FakeControl()
        slider = self._FakeControl()
        progress_lbl = self._FakeControl()
        messages_list = self._FakeControl()
        conv_panel = self._FakeControl()

        panel = types.SimpleNamespace()
        panel.audio_speed_btn = speed_btn
        panel.audio_slider = slider
        panel.audio_progress_label = progress_lbl
        panel.messages_list = messages_list
        panel.conversation_panel = conv_panel
        panel._hide_audio_controls = types.MethodType(
            ConversationsPanel._hide_audio_controls, panel
        )

        class _Window:
            @staticmethod
            def FindFocus():
                return speed_btn

        monkeypatch.setattr(conversations_module.wx, "Window", _Window)

        panel._hide_audio_controls()

        assert messages_list.focused is True
        assert speed_btn.hidden is True
        assert slider.hidden is True
        assert progress_lbl.hidden is True

    def test_no_focus_change_when_other_control_focused(self, monkeypatch):
        other_ctrl = self._FakeControl()
        speed_btn = self._FakeControl()
        messages_list = self._FakeControl()
        conv_panel = self._FakeControl()

        panel = types.SimpleNamespace()
        panel.audio_speed_btn = speed_btn
        panel.audio_slider = self._FakeControl()
        panel.audio_progress_label = self._FakeControl()
        panel.messages_list = messages_list
        panel.conversation_panel = conv_panel
        panel._hide_audio_controls = types.MethodType(
            ConversationsPanel._hide_audio_controls, panel
        )

        class _Window:
            @staticmethod
            def FindFocus():
                return other_ctrl

        monkeypatch.setattr(conversations_module.wx, "Window", _Window)

        panel._hide_audio_controls()

        assert messages_list.focused is False


class TestTheMarkPlayedSettingGatesTheLocalHalf:
    """Configurações > Reprodução de áudio > "Mudar status dos áudios para
    reproduzidos nas conversas e disparar anúncio ao leitor de tela".

    On by default (the existing behaviour). Off skips the LOCAL half only —
    the row is never rewritten, so nothing announces a change on a voice note
    the user has usually already moved off, which is what the chain does the
    moment the previous one ends. The played receipt still goes to WhatsApp:
    the sender is entitled to know their message was heard, and that is not
    what this setting is about.
    """

    class _Stub:
        from main import MainWindow as _MW
        mark_audio_message_played = _MW.mark_audio_message_played

        def __init__(self, enabled=None):
            audio = {} if enabled is None else {"mark_audio_played_in_list": enabled}
            self.settings = {"audio_playback": audio}
            self.status_updates = []
            self.receipts = []

        def on_message_status_update(self, payload, skip_panel_refresh=False):
            self.status_updates.append(payload)

        def _send_mark_played_request(self, remote_jid, msg_key):
            self.receipts.append((remote_jid, msg_key))

    @staticmethod
    def _msg():
        return {"key": {"id": "a1", "remoteJid": "5511@s.whatsapp.net",
                        "fromMe": False}}

    def _run(self, stub):
        import threading as _t
        real = _t.Thread
        started = []

        class _Inline:
            def __init__(self, target=None, args=(), daemon=None):
                self._t, self._a = target, args

            def start(self):
                started.append(1)
                self._t(*self._a)

        _t.Thread = _Inline
        try:
            stub.mark_audio_message_played(self._msg())
        finally:
            _t.Thread = real
        return started

    def test_on_by_default_when_the_key_is_absent(self):
        """A settings.json predating this option keeps the old behaviour."""
        stub = self._Stub(enabled=None)
        self._run(stub)
        assert len(stub.status_updates) == 1
        assert stub.status_updates[0]["status"] == "5"

    def test_enabled_marks_the_row(self):
        stub = self._Stub(enabled=True)
        self._run(stub)
        assert len(stub.status_updates) == 1

    def test_disabled_never_touches_the_row(self):
        stub = self._Stub(enabled=False)
        self._run(stub)
        assert stub.status_updates == []

    def test_the_played_receipt_is_sent_either_way(self):
        for enabled in (True, False):
            stub = self._Stub(enabled=enabled)
            self._run(stub)
            assert len(stub.receipts) == 1, enabled

    def test_our_own_send_is_still_never_marked(self):
        stub = self._Stub(enabled=True)
        mine = {"key": {"id": "a1", "remoteJid": "5511@s.whatsapp.net",
                        "fromMe": True}}
        stub.mark_audio_message_played(mine)
        assert stub.status_updates == [] and stub.receipts == []
