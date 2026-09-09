"""Tests for the wipe decision in _bg_pairing_flow(), and the split that made
it its own question — Connect._is_same_account().

_can_reuse_existing_session() used to answer two things at once: "can this
WPPConnect session be resumed?" and, by being the only input to
clear_local_data(), "does this database still belong to whoever is pairing?".
The first needs a token; the second does not. Asked together, a missing token
dragged the wipe along with it, and the round trip that produces exactly that
is ordinary: a paired account switches to QR mode and back to phone. Both
sides of the detour deliberately leave no reusable token —
_close_active_session() clears WA_token, and on_switch_to_qrcode() drops the
mode-switch capture on purpose (it would stand for a session that never
authenticated) — so Continue with the very same number still deleted the whole
local database, with `paired` and the stored WA_phone_number both saying it
was the same account.

The upfront decision is only ever a guess, on both paths: QR pairing cannot
know which phone is about to scan. MainWindow._wipe_local_data_if_another_number_linked()
asks WhatsApp itself once pairing closes and wipes then if the linked number
diverges (tests/test_another_number_linked_wipe.py). That is what makes the
asymmetry here correct rather than lax: guessing "preserve" wrongly costs one
deferred wipe, guessing "delete" wrongly costs history nothing can restore.

Connect is a plain class — same approach as
tests/test_qrcode_repair_preserves_local_data.py, whose fakes this reuses.
"""

import threading

import ui.dialogs.connect as connect_module
from ui.dialogs.connect import Connect

from tests.test_qrcode_repair_preserves_local_data import (
    _FakeMainWindow, _Panel, _Field, _Dial, _Button,
)


NUMBER = "5511999999999"


class TestIsSameAccount:
    """The question on its own: does this number own the local database?"""

    def _pi(self, **kw):
        base = {"paired": True, "WA_phone_number": NUMBER}
        base.update(kw)
        return base

    def test_same_number_on_a_paired_account(self):
        assert Connect._is_same_account(self._pi(), NUMBER) is True

    def test_no_token_is_needed_to_answer_it(self):
        """The whole point of the split — the answer does not depend on any
        session being alive or resumable."""
        assert Connect._is_same_account(self._pi(), NUMBER) is True
        assert Connect._can_reuse_existing_session(self._pi(), NUMBER, "") is False

    def test_a_different_number_is_a_different_account(self):
        assert Connect._is_same_account(self._pi(), "5511888888888") is False

    def test_a_never_paired_account_owns_nothing_to_keep(self):
        assert Connect._is_same_account(self._pi(paired=False), NUMBER) is False

    def test_the_comparison_stays_exact(self):
        """A tolerant comparison is what once let +49 211 1234567's session be
        resumed by somebody typing +49 211 234567."""
        assert Connect._is_same_account(
            {"paired": True, "WA_phone_number": "492111234567"}, "49211234567"
        ) is False

    def test_formatting_of_the_same_number_still_matches(self):
        assert Connect._is_same_account(
            {"paired": True, "WA_phone_number": "+55 (11) 99999-9999"}, NUMBER
        ) is True

    def test_nothing_stored_matches_nothing(self):
        assert Connect._is_same_account({"paired": True}, NUMBER) is False
        assert Connect._is_same_account(self._pi(), "") is False
        assert Connect._is_same_account(None, NUMBER) is False


class TestReuseStillNeedsAToken:
    """The refactor must not have loosened the resume test: everything
    _can_reuse_existing_session() rejected before, it still rejects."""

    def test_same_account_but_no_token_cannot_resume(self):
        pi = {"paired": True, "WA_phone_number": NUMBER}
        assert Connect._can_reuse_existing_session(pi, NUMBER, "") is False

    def test_token_but_a_different_number_cannot_resume(self):
        pi = {"paired": True, "WA_phone_number": NUMBER}
        assert Connect._can_reuse_existing_session(pi, "5511888888888", "t") is False

    def test_token_but_never_paired_cannot_resume(self):
        pi = {"paired": False, "WA_phone_number": NUMBER}
        assert Connect._can_reuse_existing_session(pi, NUMBER, "t") is False

    def test_same_account_with_a_token_resumes(self):
        pi = {"paired": True, "WA_phone_number": NUMBER}
        assert Connect._can_reuse_existing_session(pi, NUMBER, "sess1:h1") is True


def _connect_for(mw, typed_number=NUMBER):
    c = Connect(mw)
    c.qrcode_panel = _Panel()
    c.phone_panel = _Panel()
    c.phone_field = _Field(typed_number)
    c.connection_dial = _Dial()
    c.continue_btn = _Button()
    c._create_instance = lambda token: None
    return c


def _switch_to_qrcode_and_back(c):
    """The detour that leaves no reusable token behind.

    start_qrcode_connection() is stubbed out: it goes to the network and, on
    failure, straight to a wx.MessageBox that needs a real wx.App. What this
    file is about is the state the detour leaves — WA_token cleared by
    _close_active_session(), the mode-switch capture dropped on purpose — and
    both of those come from the switch methods themselves. What
    start_qrcode_connection() does with preserve_local_data is
    tests/test_qrcode_repair_preserves_local_data.py's subject.
    """
    c.start_qrcode_connection = lambda preserve_local_data=False: None
    c.on_switch_to_qrcode(None)
    c.on_switch_to_phone(None)


def _run_pairing_flow(c, monkeypatch):
    """Drive on_continue() through the wipe decision and back out again.

    The decision runs before any network call, so failing every request is
    enough to keep the test to the branch under test instead of waiting 90 s
    for a phoneCode that will never arrive; _bg_pairing_flow()'s own except
    clause handles it.

    on_continue() rebinds wx.GetApp on the module itself — going through
    monkeypatch keeps that out of the rest of the suite, the same reason
    tests/test_qrcode_repair_preserves_local_data.py gives. CallAfter goes
    with it: there is no wx.App in this file, so the marshalling
    _bg_pairing_flow()'s failure path does would assert inside the pairing
    thread and surface as a PytestUnhandledThreadExceptionWarning that has
    nothing to do with what is being tested.
    """
    monkeypatch.setattr(connect_module.wx, "GetApp", lambda: None)
    monkeypatch.setattr(connect_module.wx, "CallAfter", lambda *a, **kw: None)
    monkeypatch.setattr(
        connect_module, "api_post",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("no network in tests")),
    )
    decided = threading.Event()
    real_same_account = c._is_same_account

    def _tripwire(privateinfo, phone_number):
        # Wraps rather than replaces: the real answer is what the assertions
        # are about; this only records that the decision was reached at all,
        # so a flow that died earlier fails loudly instead of silently
        # asserting "clear_local_data was not called".
        answer = real_same_account(privateinfo, phone_number)
        decided.set()
        return answer

    c._is_same_account = _tripwire
    c.on_continue(None)
    assert decided.wait(5), "the pairing flow never reached the wipe decision"


class TestTheRoundTripKeepsItsHistory:
    """paired → QR → back to phone → the same number typed in."""

    def test_no_wipe_when_the_number_has_not_changed(self, monkeypatch):
        mw = _FakeMainWindow(paired=True, token="sess1:hash1")
        mw.settings["privateinfo"]["WA_phone_number"] = NUMBER
        c = _connect_for(mw)

        _switch_to_qrcode_and_back(c)
        assert mw._get_wa_token() == "", "precondition: no token to resume"

        before = mw.clear_local_data_calls
        _run_pairing_flow(c, monkeypatch)

        assert mw.clear_local_data_calls == before

    def test_a_different_number_still_wipes(self, monkeypatch):
        """The protection is about identity, not about being lenient."""
        mw = _FakeMainWindow(paired=True, token="sess1:hash1")
        mw.settings["privateinfo"]["WA_phone_number"] = NUMBER
        c = _connect_for(mw, typed_number="5511888888888")

        _switch_to_qrcode_and_back(c)

        before = mw.clear_local_data_calls
        _run_pairing_flow(c, monkeypatch)

        assert mw.clear_local_data_calls == before + 1
        # The whole safety argument for splitting the two flags is that the
        # wipe branch is a SUBSET of the reset branch — a different account
        # can never have a resumable session, so everything that reaches the
        # wipe has already passed the reset. This is that proof as a test:
        # it is what fails if someone later re-keys the reset on
        # _is_same_account() for symmetry.
        assert mw.messages_set_completed is False

    def test_a_never_paired_account_still_wipes(self, monkeypatch):
        """Nothing to lose, and a leftover from an abandoned pairing is not
        history worth keeping."""
        mw = _FakeMainWindow(paired=False, token="")
        mw.settings["privateinfo"]["WA_phone_number"] = NUMBER
        c = _connect_for(mw)

        before = mw.clear_local_data_calls
        _run_pairing_flow(c, monkeypatch)

        assert mw.clear_local_data_calls == before + 1


class TestWaitingForTheSyncIsStillKeyedOnTheSession:
    """The two flags were split apart on purpose: a session that cannot be
    resumed syncs from scratch and must still wait for messages.set, whether
    or not the account changed."""

    def test_the_same_account_on_a_new_session_still_waits(self, monkeypatch):
        mw = _FakeMainWindow(paired=True, token="sess1:hash1")
        mw.settings["privateinfo"]["WA_phone_number"] = NUMBER
        mw.messages_set_completed = True
        c = _connect_for(mw)

        _switch_to_qrcode_and_back(c)
        _run_pairing_flow(c, monkeypatch)

        assert mw.messages_set_completed is False
        assert mw.clear_local_data_calls == 0


class TestAnAbandonedAttemptDoesNotRenameTheDatabasesOwner:
    """_bg_pairing_flow() writes privateinfo["WA_phone_number"] the instant a
    phone code arrives — long before the pairing concludes — and nothing used
    to put the previous value back when the attempt was abandoned.

    Harmless while that key only fed _can_reuse_existing_session(), which also
    required a token the abandonment had cleared. Not harmless once
    _is_same_account() decides the wipe from it alone: account A is paired,
    the user types B, gets a code, abandons, then pairs B for real — and the
    stale key says "same account", so B's sync lands on top of A's chats,
    media and voice notes. The backstop only catches that when
    WA_phone_number_linked already exists (main.py's another_number_check
    learns the number instead of deleting when it does not), so this must not
    depend on it.
    """

    def _dialog_that_got_a_code_for(self, mw, number):
        """The state an attempt leaves behind once a code has arrived."""
        c = _connect_for(mw, typed_number=number)
        privateinfo = mw.settings["privateinfo"]
        c._phone_number_before_attempt = privateinfo.get("WA_phone_number") or ""
        privateinfo["WA_phone_number"] = number
        return c

    def test_the_previous_number_comes_back_when_the_attempt_is_abandoned(self):
        mw = _FakeMainWindow(paired=True, token="sess1:hash1")
        mw.settings["privateinfo"]["WA_phone_number"] = NUMBER
        c = self._dialog_that_got_a_code_for(mw, "5511888888888")

        c._close_active_session()

        assert mw.settings["privateinfo"]["WA_phone_number"] == NUMBER

    def test_the_other_number_is_then_read_as_another_account(self):
        """The point of restoring it: the wipe decision has to see the number
        the database actually belongs to."""
        mw = _FakeMainWindow(paired=True, token="sess1:hash1")
        mw.settings["privateinfo"]["WA_phone_number"] = NUMBER
        c = self._dialog_that_got_a_code_for(mw, "5511888888888")

        c._close_active_session()

        assert c._is_same_account(
            mw.settings["privateinfo"], "5511888888888") is False
        assert c._is_same_account(mw.settings["privateinfo"], NUMBER) is True

    def test_a_completed_pairing_keeps_its_own_number(self):
        """_wa_connected is what separates "abandoned" from "succeeded": the
        number a successful attempt wrote is the correct one, and restoring
        over it would rename a database that had just been paired."""
        mw = _FakeMainWindow(paired=True, token="sess1:hash1")
        mw.settings["privateinfo"]["WA_phone_number"] = NUMBER
        c = self._dialog_that_got_a_code_for(mw, "5511888888888")
        mw._wa_connected = True

        c._close_active_session()

        assert mw.settings["privateinfo"]["WA_phone_number"] == "5511888888888"

    def test_an_account_that_had_no_number_gets_the_key_removed_again(self):
        """Restoring "" as a value would leave a key that reads as a real
        stored number to anything comparing digits."""
        mw = _FakeMainWindow(paired=False, token="")
        c = self._dialog_that_got_a_code_for(mw, "5511888888888")

        c._close_active_session()

        assert "WA_phone_number" not in mw.settings["privateinfo"]

    def test_the_restore_happens_only_once(self):
        """One-shot, like the mode-switch capture: a second close must not
        put the number back over whatever legitimately replaced it."""
        mw = _FakeMainWindow(paired=True, token="sess1:hash1")
        mw.settings["privateinfo"]["WA_phone_number"] = NUMBER
        c = self._dialog_that_got_a_code_for(mw, "5511888888888")

        c._close_active_session()
        mw.settings["privateinfo"]["WA_phone_number"] = "5511777777777"
        c._close_active_session()

        assert mw.settings["privateinfo"]["WA_phone_number"] == "5511777777777"

    def test_nothing_is_touched_when_no_attempt_wrote_anything(self):
        mw = _FakeMainWindow(paired=True, token="sess1:hash1")
        mw.settings["privateinfo"]["WA_phone_number"] = NUMBER
        c = _connect_for(mw)

        c._close_active_session()

        assert mw.settings["privateinfo"]["WA_phone_number"] == NUMBER
