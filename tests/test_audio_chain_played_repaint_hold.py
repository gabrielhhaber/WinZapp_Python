"""The "played" row repaint must never be written while the audio chain is
moving list focus to the next voice note.

The rule this enforces is NVDA's own (source/NVDAObjects/__init__.py)::

    def event_nameChange(self):
        if self is api.getFocusObject():
            speech.speakObjectProperties(self, name=True, reason=CHANGE)

A wx.ListCtrl row is a single MSAA object whose *name* is the whole rendered
line, so adding "reproduzido" to a row raises a name change and NVDA reads the
**entire row** back — but only while that row is the object it believes has
focus. So the only safe move is to not write that row at all until focus has
moved on for good; timing the write against the focus event is not a fix, it is
a bet.

The old protection made that bet and lost it in two distinct ways, both
reproduced below against the real code path with a real (never shown) list
control:

* refresh_message_status() does not write — it queues the row behind a 120 ms
  coalescing timer, so the write lands long after the callback that was
  supposed to be ordering it.
* the played receipt WhatsApp echoes back reaches on_message_status_update()
  with skip_panel_refresh=False and repaints the row without going near the
  chain, which can put the write *before* the focus move.

These tests run the actual methods, with a real wx event loop, and assert on
the observed order of Focus()/SetItemText() calls.
"""

import time
import types

import pytest

import wx

from tests.conftest import hidden_frame
from ui.conversations import ConversationsPanel


class _Recorder:
    """Records the real order of focus moves and row writes."""

    def __init__(self):
        self.events = []

    def focus(self, idx):
        self.events.append(("focus", idx))

    def write(self, idx, text):
        self.events.append(("write", idx, text))

    @property
    def writes(self):
        return [e for e in self.events if e[0] == "write"]


class _ChainStub:
    """The real chain/repaint methods on a stub carrying only what they touch.

    ConversationsPanel is a wx.Panel and cannot be built without a full app,
    so this follows the suite's usual unbound-method binding — but the list
    control is real, because the ordering being tested is the ordering of real
    control calls.
    """

    refresh_message_status = ConversationsPanel.refresh_message_status
    _flush_status_repaints = ConversationsPanel._flush_status_repaints
    _release_chain_held_repaints = ConversationsPanel._release_chain_held_repaints
    _hold_status_repaints_until_chain_ends = (
        ConversationsPanel._hold_status_repaints_until_chain_ends
    )
    _auto_chain_next_audio = ConversationsPanel._auto_chain_next_audio
    _cancel_pending_chain_timers = ConversationsPanel._cancel_pending_chain_timers
    _next_message_is_chainable_audio = ConversationsPanel._next_message_is_chainable_audio
    _STATUS_REPAINT_COALESCE_MS = ConversationsPanel._STATUS_REPAINT_COALESCE_MS

    def __init__(self, messages_list, recorder, ids):
        self.messages_list = messages_list
        self._recorder = recorder
        self.conversation = {"remoteJid": "j@s.whatsapp.net"}
        self._audio_conv_jid = "j@s.whatsapp.net"
        self._sorted_messages = [
            {"key": {"id": i}, "messageType": "audioMessage"} for i in ids
        ]
        self._played = set()
        self._is_in_audio_chain = False
        self._chain_play_timer = None
        self._chain_start_timer = None
        self._chain_end_timer = None
        self._pending_played_refresh_id = None
        self._hold_status_repaints_for_chain = False
        self._chain_held_status_repaints = set()
        self.main_window = types.SimpleNamespace(
            settings={"user_interface": {"auto_focus_next_audio": True}},
            audio_transition_next_sound=None,
            audio_transition_end_sound=None,
        )

    # — the bits the real methods lean on —
    def _is_separator(self, msg):
        return False

    def _is_voice_message(self, msg):
        return True

    def _render_message_line(self, msg):
        msg_id = msg["key"]["id"]
        return f"{msg_id} reproduzido" if msg_id in self._played else msg_id

    def _toggle_playback(self, *args, **kwargs):
        pass

    # — what on_audio_timer() does when an audio reaches its end —
    def finish_audio(self, msg_id):
        will_chain = self._next_message_is_chainable_audio(msg_id)
        if will_chain:
            self._hold_status_repaints_until_chain_ends()
        self._played.add(msg_id)
        if not will_chain:
            # mark_audio_message_played(skip_panel_refresh=False)
            self.refresh_message_status(msg_id, "5")
        self._auto_chain_next_audio(
            msg_id, pending_played_msg_id=msg_id if will_chain else None
        )


@pytest.fixture
def chain(wx_app):
    """A real, never-shown wx.ListCtrl driven by the real chain methods."""
    frame = hidden_frame()
    lst = wx.ListCtrl(frame, style=wx.LC_REPORT)
    lst.InsertColumn(0, "msg")
    ids = ["m0", "m1", "m2"]
    for i, msg_id in enumerate(ids):
        lst.InsertItem(i, msg_id)

    recorder = _Recorder()
    real_set_item_text = lst.SetItemText
    real_focus = lst.Focus

    def traced_set_item_text(idx, text):
        recorder.write(idx, text)
        real_set_item_text(idx, text)

    def traced_focus(idx):
        recorder.focus(idx)
        real_focus(idx)

    lst.SetItemText = traced_set_item_text
    lst.Focus = traced_focus
    lst.Focus(0)
    recorder.events.clear()

    stub = _ChainStub(lst, recorder, ids)
    yield stub, recorder, lst
    frame.Destroy()


@pytest.fixture(autouse=True)
def _no_account_paths(monkeypatch):
    """_start_audio() builds a data_path() for the next voice note; the real
    one needs an active account, which no test bootstraps."""
    import ui.conversations as conversations_module

    monkeypatch.setattr(
        conversations_module, "data_path", lambda *parts: "/".join(parts)
    )


def _pump(app, ms):
    """Run the real wx event loop for ms milliseconds, so the chain's
    wx.CallLater timers actually fire the way they do in the app."""
    wx.CallLater(ms, app.ExitMainLoop)
    app.MainLoop()


# ── The bug, and that it is gone ─────────────────────────────────────────────


def test_no_row_is_written_while_the_chain_moves_focus(chain, wx_app):
    """The core invariant: between one voice note ending and the next taking
    focus, the list must not be written to at all."""
    stub, recorder, _ = chain

    stub.finish_audio("m0")
    _pump(wx_app, 400)

    assert ("focus", 1) in recorder.events, "the chain must move focus to m1"
    assert recorder.writes == [], (
        "a row was rewritten while the chain was moving focus — NVDA reads the "
        f"whole row back at that moment: {recorder.writes}"
    )


def test_the_whatsapp_played_echo_cannot_slip_in_either(chain, wx_app):
    """mark_audio_message_played() POSTs a played receipt, and WhatsApp echoes
    it back onto on_message_status_update() with skip_panel_refresh=False.

    That path never passes through the chain, so ordering the chain's own
    refresh against the focus move could not protect it. Measured before the
    hold existed, the echo wrote the row 95 ms BEFORE the focus move — the
    reported symptom exactly: the whole finished row read out, then the new
    one.
    """
    stub, recorder, _ = chain

    stub.finish_audio("m0")
    stub.refresh_message_status("m0", "5")  # the echo landing mid-transition
    _pump(wx_app, 400)

    assert ("focus", 1) in recorder.events
    assert recorder.writes == []


def test_held_rows_are_written_once_the_sequence_ends(chain, wx_app):
    """Held, not dropped — the row must still end up showing "reproduzido"."""
    stub, recorder, lst = chain

    stub.finish_audio("m0")
    _pump(wx_app, 400)
    stub.finish_audio("m1")
    _pump(wx_app, 400)
    # m2 is last: nothing to chain into, so the sequence ends here.
    stub.finish_audio("m2")
    _pump(wx_app, 400)

    assert lst.GetItemText(0) == "m0 reproduzido"
    assert lst.GetItemText(1) == "m1 reproduzido"
    assert lst.GetItemText(2) == "m2 reproduzido"


def test_every_write_lands_after_the_last_focus_move(chain, wx_app):
    """Stated as the property that matters rather than as a count: no write may
    be interleaved between focus moves, because that is the window in which
    NVDA still believes the finished row is the focused one."""
    stub, recorder, _ = chain

    stub.finish_audio("m0")
    _pump(wx_app, 400)
    stub.finish_audio("m1")
    _pump(wx_app, 400)
    stub.finish_audio("m2")
    _pump(wx_app, 400)

    kinds = [e[0] for e in recorder.events]
    last_focus = len(kinds) - 1 - kinds[::-1].index("focus")
    first_write = kinds.index("write")
    assert first_write > last_focus, (
        f"a row was written before the chain finished moving focus: {recorder.events}"
    )


def test_the_rows_written_at_release_are_not_the_focused_row(chain, wx_app):
    """NVDA stays silent for a name change on a non-focused object. At release
    the list is focused on the last voice note, so every earlier row written
    then is silent by NVDA's own rule — no timing assumption involved."""
    stub, recorder, lst = chain

    stub.finish_audio("m0")
    _pump(wx_app, 400)
    stub.finish_audio("m1")
    _pump(wx_app, 400)

    focused = lst.GetFocusedItem()
    assert focused == 2, "focus should have chained through to the last audio"
    written_rows = {e[1] for e in recorder.writes}
    assert focused not in written_rows, (
        f"row {focused} has list focus and was rewritten: {recorder.writes}"
    )


# ── Behaviour that must NOT change ───────────────────────────────────────────


def test_a_single_audio_with_nothing_to_chain_still_repaints_normally(chain, wx_app):
    """No chain means no focus move to stay clear of, so the "played" mark
    must appear the way it always has. Suppressing it here would delete real
    feedback for the ordinary one-audio case."""
    stub, recorder, lst = chain
    stub._sorted_messages = stub._sorted_messages[:1]  # m0 alone

    stub.finish_audio("m0")
    _pump(wx_app, 400)

    assert lst.GetItemText(0) == "m0 reproduzido"
    assert recorder.writes == [("write", 0, "m0 reproduzido")]


def test_stopping_playback_mid_sequence_releases_the_held_rows(chain, wx_app):
    """The user stopping (or leaving the conversation) ends the sequence too —
    _stop_audio() calls the release, so held rows are never stranded stale."""
    stub, recorder, lst = chain

    stub.finish_audio("m0")
    _pump(wx_app, 400)
    assert lst.GetItemText(0) == "m0", "still held while the chain runs"

    stub._release_chain_held_repaints()  # what _stop_audio() reaches
    assert lst.GetItemText(0) == "m0 reproduzido"


def test_release_is_idempotent(chain, wx_app):
    """Several places can end a sequence — the last voice note, the user
    stopping playback, leaving the conversation — and more than one of them can
    fire for the same sequence. A second release must not rewrite the rows: a
    redundant write is another name change, i.e. another chance to interrupt
    the screen reader for nothing."""
    stub, recorder, lst = chain

    stub.finish_audio("m0")
    _pump(wx_app, 400)

    stub._release_chain_held_repaints()
    after_first = list(recorder.writes)
    assert after_first == [("write", 0, "m0 reproduzido")]

    stub._release_chain_held_repaints()
    stub._release_chain_held_repaints()
    assert recorder.writes == after_first


def test_holding_never_loses_a_repaint_for_an_unrelated_message(chain, wx_app):
    """A delivery receipt for some other row arriving mid-sequence is held too
    (any row could be the one NVDA has focus on), but it must still be written
    when the sequence ends."""
    stub, recorder, lst = chain

    stub.finish_audio("m0")
    stub._played.add("m2")  # unrelated status change for a different row
    stub.refresh_message_status("m2", "5")
    _pump(wx_app, 400)
    assert recorder.writes == []

    stub._release_chain_held_repaints()
    assert lst.GetItemText(2) == "m2 reproduzido"
