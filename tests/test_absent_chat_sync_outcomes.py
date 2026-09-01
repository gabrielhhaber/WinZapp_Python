"""A chat the store says does not exist is an answer, not a failed sync.

WPPConnect answers `404 {"reason":"chat_not_found"}` for a JID that has no
chat behind it — routinely, for a JID that only ever appeared in an
`e2e_notification` (an encryption housekeeping event, never a conversation)
and left an entry in self.chats anyway. That answer was being counted as a
failed message fetch, which held message_sync_ok False on EVERY round: seen
live as `last_success=never` with all 155 chats replanned as
`reasons={'forced-full': 155}` round after round, the health checker resyncing
and announcing itself to the screen reader each time.

Same distinction — and the same bounded treatment — as an empty incremental
delta, so these tests reuse that module's stubs rather than growing a third
copy of them. MainWindow is a wx.Frame and cannot be instantiated without a
running app.
"""

import main as main_module
from main import MainWindow
from tests.test_incremental_delta_outcomes import (
    A,
    B,
    _DeltaStub,
    _Resp,
    _RoundStub,
)

ABSENT = "133041125077153@lid"


class _AbsentStub(_DeltaStub):
    """The shared delta stub plus the two pieces only this path reaches: the
    absence latch itself, and the verified-activity note a fetch that finally
    succeeds writes right after the marker."""

    def __init__(self, normalized=None):
        super().__init__(normalized=normalized)
        self._absent_chats = set()
        self._absent_chat_attempts = {}

    def _note_verified_activity(self, remote_jid, chat):
        pass


_CHAT_NOT_FOUND = {
    "status": "error",
    "reason": "chat_not_found",
    "response": "Error on open list",
    "error": {"message": f"Chat not found for {ABSENT}"},
}


def _chat_not_found(monkeypatch):
    monkeypatch.setattr(
        main_module.requests, "get",
        lambda url, **kwargs: _Resp(404, _CHAT_NOT_FOUND),
    )


def _sync_absent(stub):
    return MainWindow.sync_chat_messages(stub, {"remoteJid": ABSENT, "t": 100})


class TestAChatNotFoundIsNotAFailedFetch:
    def test_the_round_is_still_reported_as_successful(self, monkeypatch):
        _chat_not_found(monkeypatch)
        assert _sync_absent(_AbsentStub()) is True

    def test_and_it_never_reaches_the_failed_chats_latch(self, monkeypatch):
        """_sync_failed_chats is what sync_remote_chats() turns into the
        caller's message_sync_ok — the regression that caused the loop."""
        _chat_not_found(monkeypatch)
        stub = _AbsentStub()
        _sync_absent(stub)
        assert stub._sync_failed_chats == set()

    def test_but_the_chat_is_queued_to_be_looked_at_again(self, monkeypatch):
        """It can come into existence later — the person finally writes."""
        _chat_not_found(monkeypatch)
        stub = _AbsentStub()
        _sync_absent(stub)
        assert stub._absent_chats == {ABSENT}

    def test_and_the_activity_marker_is_not_committed_yet(self, monkeypatch):
        _chat_not_found(monkeypatch)
        stub = _AbsentStub()
        _sync_absent(stub)
        assert stub.db.upserted == []

    def test_retrying_terminates_and_retires_the_chat(self, monkeypatch):
        """Every new e2e_notification mints another of these, so the retry
        budget has to be finite or the install pays a request per round per
        phantom JID forever."""
        _chat_not_found(monkeypatch)
        stub = _AbsentStub()
        for _ in range(main_module._MAX_ABSENT_CHAT_RETRIES):
            _sync_absent(stub)
        assert stub._absent_chats == set()
        assert stub._absent_chat_attempts == {}
        assert stub.db.upserted == [ABSENT]

    def test_a_chat_that_starts_existing_is_synced_again(self, monkeypatch):
        """Retiring must not mark the JID skippable: nothing else knows about
        it, so a real message brings it straight back."""
        _chat_not_found(monkeypatch)
        stub = _AbsentStub()
        _sync_absent(stub)
        assert stub._absent_chats == {ABSENT}

        stub._normalized = [{
            "key": {"remoteJid": ABSENT, "id": "m1", "fromMe": False},
            "message": {"conversation": "oi"},
            "messageType": "conversation",
            "messageTimestamp": 150,
        }]
        monkeypatch.setattr(
            main_module.requests, "get",
            lambda url, **kwargs: _Resp(200, {"response": [{"id": "m1"}]}),
        )
        assert _sync_absent(stub) is True
        assert stub._absent_chats == set()
        assert stub._absent_chat_attempts == {}
        assert stub.db.upserted == [ABSENT]

    def test_a_real_io_failure_is_still_a_failure(self, monkeypatch):
        """Without this the fix would mask exactly what it is built to keep
        reporting: messages that never arrived."""
        monkeypatch.setattr(main_module.time, "sleep", lambda *_: None)
        monkeypatch.setattr(
            main_module.requests, "get",
            lambda url, **kwargs: _Resp(500, {"error": "boom"}),
        )
        stub = _AbsentStub()
        assert _sync_absent(stub) is False
        assert stub._sync_failed_chats == {ABSENT}
        assert stub._absent_chats == set()


class TestAnAbsentChatDoesNotFailTheRound:
    """sync_remote_chats()'s return is read as message_sync_ok, and False there
    keeps _sync_completed False for the rest of the session — never committing
    the list-chats snapshot, resyncing on every health-check cooldown, and
    dropping every live chats.update unread event."""

    def test_the_round_reports_no_failures(self):
        stub = _RoundStub([{"remoteJid": A, "t": 100}, {"remoteJid": B, "t": 90}])
        stub._absent_chats = {A}
        assert MainWindow.sync_remote_chats(stub, incremental=True) == set()

    def test_but_the_chat_stays_on_the_durable_retry_list(self):
        stub = _RoundStub([{"remoteJid": A, "t": 100}, {"remoteJid": B, "t": 90}])
        stub._absent_chats = {A}
        MainWindow.sync_remote_chats(stub, incremental=True)
        assert stub._message_retry_jids == {A}

    def test_a_retired_chat_leaves_the_retry_list(self):
        """Once sync_chat_messages() has retired it, the round has to drain the
        latch or the chat is re-fetched forever anyway."""
        stub = _RoundStub([{"remoteJid": A, "t": 100}])
        stub._message_retry_jids = {A}
        MainWindow.sync_remote_chats(stub, incremental=True)
        assert stub._message_retry_jids == set()

    def test_a_real_failure_is_still_returned_as_one(self):
        stub = _RoundStub(
            [{"remoteJid": A, "t": 100}, {"remoteJid": B, "t": 90}],
            failing={B},
        )
        stub._absent_chats = {A}
        assert MainWindow.sync_remote_chats(stub, incremental=True) == {B}
