"""Tests for the "auto_focus_next_audio" setting gating whether
_auto_chain_next_audio() moves list focus onto the next voice note.

Feature request: some users want sequential voice-note playback (auto-chain)
to keep advancing audio automatically without also moving list focus away
from wherever they left it — a new Settings > Interface checkbox
("Mover o foco automaticamente para o próximo áudio ao reproduzir áudios em
sequência"), defaulting to the pre-existing behavior (checked/True).

ConversationsPanel is a wx.Panel and can't be instantiated without a running
wx.App, so _auto_chain_next_audio() is bound onto a plain stub — same
approach as tests/test_audio_finish_marks_played.py. wx.CallLater is
monkeypatched to run its callback immediately (as the real event loop
eventually would), so the whole two-step chain (play transition sound, then
start the next audio) resolves synchronously within one call.
"""

import pytest

from ui.conversations import ConversationsPanel


class _FakeMessagesList:
    def __init__(self, focused, events=None):
        self._focused = focused
        self.focus_calls = []
        self.select_calls = []
        self.ensure_visible_calls = []
        self._events = events

    def GetFocusedItem(self):
        return self._focused

    def Focus(self, idx):
        self.focus_calls.append(idx)
        self._focused = idx
        if self._events is not None:
            self._events.append(("focus", idx))

    def Select(self, idx, on=True):
        self.select_calls.append((idx, on))

    def EnsureVisible(self, idx):
        self.ensure_visible_calls.append(idx)


class _FakeMainWindow:
    def __init__(self, auto_focus_next_audio=True):
        self.settings = {"user_interface": {"auto_focus_next_audio": auto_focus_next_audio}}
        self.audio_transition_next_sound = None


def _voice_msg(msg_id):
    return {
        "key": {"id": msg_id},
        "messageType": "audioMessage",
        "message": {"audioMessage": {"ptt": True, "seconds": 5}},
    }


class _Stub:
    _auto_chain_next_audio = ConversationsPanel._auto_chain_next_audio
    _cancel_pending_chain_timers = ConversationsPanel._cancel_pending_chain_timers
    _hold_status_repaints_until_chain_ends = (
        ConversationsPanel._hold_status_repaints_until_chain_ends
    )
    _release_chain_held_repaints = ConversationsPanel._release_chain_held_repaints
    _flush_status_repaints = ConversationsPanel._flush_status_repaints
    _is_separator = ConversationsPanel._is_separator
    _is_voice_message = ConversationsPanel._is_voice_message

    def __init__(self, sorted_messages, focused, auto_focus_next_audio=True):
        finished_jid = "grupo@g.us"
        self.main_window = _FakeMainWindow(auto_focus_next_audio)
        self._sorted_messages = sorted_messages
        self.conversation = {"remoteJid": finished_jid}
        self._audio_conv_jid = finished_jid
        self.events = []  # ordered log shared across focus/select/refresh calls
        self.messages_list = _FakeMessagesList(focused, events=self.events)
        self._is_in_audio_chain = False
        self._chain_play_timer = None
        self._chain_start_timer = None
        self._chain_end_timer = None
        self._pending_played_refresh_id = None
        self._hold_status_repaints_for_chain = False
        self._chain_held_status_repaints = set()
        self._pending_status_repaints = set()
        self._status_repaint_timer = None
        self.toggle_calls = []

    def _toggle_playback(self, msg_id, duration, msg, file_path, audio_ext):
        self.toggle_calls.append(msg_id)

    def refresh_message_status(self, msg_id, status):
        self.events.append(("refresh", msg_id))


@pytest.fixture
def call_later_now(monkeypatch):
    """Run wx.CallLater's callback immediately instead of actually waiting —
    same approach as tests/test_refresh_messages_debounce.py."""
    class _Immediate:
        def Stop(self):
            pass

    def _call_later(delay, fn, *a, **kw):
        fn(*a, **kw)
        return _Immediate()

    monkeypatch.setattr("ui.conversations.wx.CallLater", _call_later)

    def _call_after(fn, *a, **kw):
        fn(*a, **kw)

    monkeypatch.setattr("ui.conversations.wx.CallAfter", _call_after)
    # _start_audio() (inside the chain) builds a voice_messages/ path via
    # data_path(), which needs an active multi-account context this test
    # never sets up — irrelevant to what's under test here (focus movement).
    monkeypatch.setattr("ui.conversations.data_path", lambda *a, **k: "unused.msv")


class TestAutoFocusNextAudioEnabled:
    def test_default_setting_moves_focus_onto_the_next_voice_note(self, call_later_now):
        msgs = [_voice_msg("m1"), _voice_msg("m2")]
        stub = _Stub(msgs, focused=0, auto_focus_next_audio=True)

        stub._auto_chain_next_audio("m1")

        assert stub.messages_list.focus_calls == [1]
        assert stub.messages_list.select_calls == [(1, True)]
        assert stub.toggle_calls == ["m2"]


class TestAutoFocusNextAudioDisabled:
    def test_disabled_setting_keeps_focus_in_place_but_still_chains_playback(self, call_later_now):
        """The checkbox only controls whether focus follows — audio must
        still auto-advance either way."""
        msgs = [_voice_msg("m1"), _voice_msg("m2")]
        stub = _Stub(msgs, focused=0, auto_focus_next_audio=False)

        stub._auto_chain_next_audio("m1")

        assert stub.messages_list.focus_calls == []
        assert stub.messages_list.select_calls == []
        assert stub.toggle_calls == ["m2"]


class TestPendingPlayedRefreshFiresAfterTheFocusMove:
    """Reported live, twice over: the "played" status refresh for the
    finished voice note kept landing before (or racing) the chain's own
    focus move onto the next one, so NVDA announced the stale change first.
    A first attempt used a fixed 300ms wx.CallLater guess and still lost the
    race. _auto_chain_next_audio() now takes the pending refresh as an
    explicit argument and fires it itself, in the same callback, right after
    the focus decision — never on a timer that can be outrun."""

    def test_the_refresh_is_logged_strictly_after_the_focus_move(self, call_later_now):
        msgs = [_voice_msg("m1"), _voice_msg("m2")]
        stub = _Stub(msgs, focused=0, auto_focus_next_audio=True)

        stub._auto_chain_next_audio("m1", pending_played_msg_id="m1")

        assert stub.events == [("focus", 1), ("refresh", "m1")]

    def test_the_refresh_still_fires_when_auto_focus_is_disabled(self, call_later_now):
        """Focus never moves in this mode, so there's no focus event to
        order against — the refresh must still happen, just without racing
        anything."""
        msgs = [_voice_msg("m1"), _voice_msg("m2")]
        stub = _Stub(msgs, focused=0, auto_focus_next_audio=False)

        stub._auto_chain_next_audio("m1", pending_played_msg_id="m1")

        assert stub.events == [("refresh", "m1")]

    def test_the_refresh_fires_immediately_when_there_is_nothing_to_chain_into(self, call_later_now):
        msgs = [_voice_msg("m1")]
        stub = _Stub(msgs, focused=0, auto_focus_next_audio=True)

        stub._auto_chain_next_audio("m1", pending_played_msg_id="m1")

        assert stub.events == [("refresh", "m1")]

    def test_no_pending_refresh_means_no_refresh_call_at_all(self, call_later_now):
        """The everyday case (no chain in flight) must not call
        refresh_message_status a second time — on_audio_timer() already did
        it itself before ever reaching _auto_chain_next_audio()."""
        msgs = [_voice_msg("m1"), _voice_msg("m2")]
        stub = _Stub(msgs, focused=0, auto_focus_next_audio=True)

        stub._auto_chain_next_audio("m1")

        assert stub.events == [("focus", 1)]

    def test_cancelling_the_chain_still_flushes_a_pending_refresh(self):
        """If the chain gets cancelled (conversation switch, user stops
        playback) before its timers fire, the pending refresh must not be
        silently lost — _cancel_pending_chain_timers() flushes it."""
        stub = _Stub([_voice_msg("m1"), _voice_msg("m2")], focused=0)
        stub._pending_played_refresh_id = "m1"

        stub._cancel_pending_chain_timers()

        assert stub.events == [("refresh", "m1")]
        assert stub._pending_played_refresh_id is None
