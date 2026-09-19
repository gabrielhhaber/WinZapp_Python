"""Which call a `callstate` event belongs to, and when the sync stands down.

Both pieces exist because of the same defect: a voice call whose peer JID
arrives in one address form on the offer and the other on the page's own
CallStore poll looked like two different calls, so the ACTIVE and the terminal
ENDED were both discarded — the call window stayed up, the microphone kept
streaming, and (because the pause flag is what background work reads) every
sync pass stood down for the rest of the session.
"""

import time

import pytest

from core.call_matching import call_event_matches_active, is_placeholder_call_id
from main import MainWindow


PHONE = "5511999999999@s.whatsapp.net"
LID = "77712345678901@lid"


def _bridged(lid_to_phone):
    """A `same_peer` that behaves like MainWindow's _chat_jids_equivalent."""
    phone_to_lid = {v: k for k, v in lid_to_phone.items()}

    def same(left, right):
        if left == right:
            return True
        return lid_to_phone.get(left) == right or phone_to_lid.get(left) == right

    return same


class TestMatchingByPeer:
    """Before WhatsApp mints a call id, the peer is all there is to go on."""

    def test_the_same_address_form_matches(self):
        active = {"identity": f"outgoing:{PHONE}", "call_id": f"outgoing:{PHONE}",
                  "peer_jid": PHONE}
        assert call_event_matches_active(active, "call-1", PHONE) is True

    def test_the_other_address_form_matches_once_bridged(self):
        active = {"identity": f"outgoing:{PHONE}", "call_id": f"outgoing:{PHONE}",
                  "peer_jid": PHONE}
        # This is the bug: raw string comparison says no.
        assert call_event_matches_active(active, "call-1", LID) is False
        assert call_event_matches_active(
            active, "call-1", LID, _bridged({LID: PHONE})
        ) is True

    def test_a_different_person_never_matches(self):
        active = {"identity": f"outgoing:{PHONE}", "call_id": f"outgoing:{PHONE}",
                  "peer_jid": PHONE}
        assert call_event_matches_active(
            active, "call-1", "5511888888888@s.whatsapp.net", _bridged({LID: PHONE})
        ) is False

    def test_no_active_call_matches_nothing(self):
        assert call_event_matches_active(None, "call-1", PHONE) is False
        assert call_event_matches_active({}, "call-1", PHONE) is False


class TestMatchingByCallId:
    """Once the held call has a real id, only that id may end it."""

    def test_a_stale_terminal_event_for_the_same_peer_is_ignored(self):
        active = {"identity": f"outgoing:{PHONE}", "call_id": "current-call",
                  "peer_jid": PHONE}
        assert call_event_matches_active(
            active, "old-call", PHONE, _bridged({LID: PHONE})
        ) is False

    def test_the_bound_id_matches(self):
        active = {"identity": f"outgoing:{PHONE}", "call_id": "current-call",
                  "peer_jid": PHONE}
        assert call_event_matches_active(active, "current-call", PHONE) is True

    def test_the_identity_still_matches_an_incoming_call(self):
        # An incoming call is tracked under WhatsApp's own id from the start,
        # so identity and call_id are the same value.
        active = {"identity": "call-9", "call_id": "call-9", "peer_jid": PHONE}
        assert call_event_matches_active(active, "call-9", "") is True

    def test_an_event_with_no_id_still_matches_on_the_peer(self):
        # The CallStore poll can report a model whose id has not serialized.
        active = {"identity": "call-9", "call_id": "call-9", "peer_jid": PHONE}
        assert call_event_matches_active(
            active, "", LID, _bridged({LID: PHONE})
        ) is True

    def test_the_placeholder_id_is_recognised_as_ours(self):
        assert is_placeholder_call_id(f"outgoing:{PHONE}") is True
        assert is_placeholder_call_id("3EB0AABBCC") is False
        assert is_placeholder_call_id("") is False


class _PauseStub:
    """Just enough of MainWindow for the background-work pause predicate."""

    _voice_call_in_progress = MainWindow._voice_call_in_progress
    _VOICE_CALL_PAUSE_MAX_SECONDS = MainWindow._VOICE_CALL_PAUSE_MAX_SECONDS

    def __init__(self, active=None):
        self._active_voice_call = active
        self._voice_call_pause_since = 0.0


class TestBackgroundWorkPause:
    def test_no_call_means_no_pause(self):
        assert _PauseStub()._voice_call_in_progress() is False

    def test_an_active_call_pauses(self):
        assert _PauseStub({"call_id": "x"})._voice_call_in_progress() is True

    def test_the_pause_is_bounded(self):
        """A call the phone tore down without a terminal event must not pause
        the periodic sync and the backfill for the rest of the session — that
        failure is invisible, the user simply stops receiving messages."""
        stub = _PauseStub({"call_id": "x"})
        assert stub._voice_call_in_progress() is True
        stub._voice_call_pause_since = (
            time.monotonic() - stub._VOICE_CALL_PAUSE_MAX_SECONDS - 1
        )
        assert stub._voice_call_in_progress() is False

    def test_the_clock_resets_when_the_call_ends(self):
        stub = _PauseStub({"call_id": "x"})
        stub._voice_call_in_progress()
        assert stub._voice_call_pause_since > 0
        stub._active_voice_call = None
        assert stub._voice_call_in_progress() is False
        assert stub._voice_call_pause_since == 0.0
        # A later call gets its own full budget, not the previous one's clock.
        stub._active_voice_call = {"call_id": "y"}
        assert stub._voice_call_in_progress() is True


class TestSyncIsNeverGatedOnACall:
    """The gate belongs where a round is *decided*, never inside one.

    `sync_remote_chats()` returns the set of chats that FAILED and
    `sync_chat_messages()` reports failure only by returning False, so an
    early return from either reads as "everything succeeded":
    `_persist_successful_sync_state()` then clears the force-full latch and
    commits the chat-list snapshot for chats nothing read. An account could be
    marked fully synced having fetched nothing, and only F5 repairs that.
    """

    @staticmethod
    def _body(name, end_marker):
        import pathlib
        source = (pathlib.Path(__file__).parents[1] / "client" / "main.py").read_text(
            encoding="utf-8"
        )
        start = source.index(f"def {name}(")
        return source[start: source.index(end_marker, start)]

    def test_neither_sync_entry_point_bails_out_on_a_call(self):
        for name, end in (
            ("sync_chat_messages", "def _get_remote_messages"),
            ("sync_remote_chats", "def _persist_message_retry_jids"),
        ):
            body = self._body(name, end)
            # Comments are stripped: both methods now carry one explaining why
            # the gate is deliberately absent, and it names the predicate.
            code = "\n".join(
                line for line in body.splitlines()
                if not line.lstrip().startswith("#")
            )
            assert "_voice_call_in_progress" not in code, name
            assert "_active_voice_call" not in code, name

    def test_the_periodic_poll_is_where_the_pause_lives(self):
        body = self._body("start_periodic_contacts_sync", "def _phone_digits_equivalent")
        gate = body.index("self._voice_call_in_progress()")
        first_fetch = body.index("self.get_remote_chats(")
        assert gate < first_fetch


class TestCallWindowDoesNotFightMainWindowForFocus:
    """voice_call_window must behave like a genuinely independent window.

    Two things fought that, reported live as "moving back to WinZapp's own
    window always falls back onto the call window":

    - It was parented to MainWindow (`wx.Frame(self, ...)`), which on Windows
      makes it an OWNED window. An owned window is kept unconditionally above
      its owner in the Z-order by the OS itself — clicking, Alt-Tabbing to, or
      SetForegroundWindow()-ing MainWindow does not change that, so no amount
      of focus code on either side can make MainWindow appear "on top" while
      the call is up.
    - _on_window_activate() additionally re-stole focus into the call window
      every time MainWindow itself became the active window (not just for a
      still-ringing call, where the answer button lives inside this same
      window and grabbing it doesn't fight anything).

    Checked statically, without constructing a real wx.Frame: this suite never
    opens a real window unless explicitly asked to (see tests/conftest.py and
    CLAUDE.md's Tests section — the risk is a screen reader announcing a
    throwaway test window on someone's actual desktop).
    """

    @staticmethod
    def _main_py_source():
        import pathlib
        return (pathlib.Path(__file__).parents[1] / "client" / "main.py").read_text(
            encoding="utf-8"
        )

    def test_the_call_window_is_not_owned_by_main_window(self):
        source = self._main_py_source()
        start = source.index("self.voice_call_window = wx.Frame(")
        construction = source[start: source.index(")", start) + 1]
        parent_arg = construction.split("(", 1)[1].split(",", 1)[0].strip()
        assert parent_arg == "None", (
            f"voice_call_window is constructed with parent={parent_arg!r}. "
            "Parenting it to another top-level frame makes it a Win32 OWNED "
            "window, which Windows keeps unconditionally above its owner in "
            "the Z-order — see the construction site's own comment for what "
            "that broke."
        )

    def test_activating_main_window_never_steals_focus_into_the_call_window(self):
        import inspect

        from main import MainWindow

        body = inspect.getsource(MainWindow._on_window_activate)
        # The method's own docstring/comments now explain this fix and
        # therefore mention voice_call_window by name — checked against the
        # CODE only, same as TestSyncIsNeverGatedOnACall's _body() helper.
        code = "\n".join(
            line for line in body.splitlines() if not line.lstrip().startswith("#")
        )
        assert "voice_call_window" not in code, (
            "MainWindow becoming active must not move focus into the "
            "separate call window for an already-answered call — that is "
            "what pinned it visually on top of MainWindow every time the "
            "user switched back to browse conversations mid-call."
        )
        # The still-ringing case is unaffected: that bar lives INSIDE this
        # same window, so focusing its own Answer button doesn't fight
        # anything and must stay.
        assert "incoming_call_answer_button" in code
        assert "incoming_call_bar" in code
