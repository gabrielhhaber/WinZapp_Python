"""Replying to a status must quote the live model, not a rebuilt copy of it.

This feature keeps breaking and un-breaking across WhatsApp Web builds, and the
reason is structural rather than bad luck. The reply was sent with wa-js's
``quotedMsgPayload``, whose own contract is "the JSON string representation of a
RAW message ... obtained when using getMessageById or getMessages" — while what
was passed is a *model's* ``toJSON()``, a different shape. wa-js answers
``quotedMsgPayload`` with ``rehydrateMessage(payload)``: it rebuilds a MsgModel
out of whichever fields happen to be present, then finishes
``prepareRawMessage`` with ``quotedMsg.msgContextInfo(chatId)``, which reads the
quoted message's getters. A field the rebuild did not carry across is undefined
by the time a getter asks for it — and the getter does not return undefined, it
throws. Every WhatsApp Web build that moves a field moves the breakage with it.

Measured 2026-09-10, from a reply that failed every attempt::

    error: "Getter was called with undefined data."
    stack: ... getIsBroadcast ... at t.prepareRawMessage
    matchedPoster: 68904344899801@lid, statusMessagesSeen: 1, quotedIsStatusV3: false

The status was found. Rebuilding it is what failed — so the fix is to stop
rebuilding it: wa-js accepts ``quotedMsg`` as ``string | MsgKey | MsgModel`` and
hands a MsgModel straight to ``msgContextInfo()``.

The payload stays as a second rung rather than being deleted, because it is not
strictly weaker: wa-js skips its ``canReplyMsg()`` gate when a payload was
supplied, so a status whose model fails that check can still go out that way.
That permissiveness is also why the live model goes first — it is what let this
failure reach ``msgContextInfo`` instead of being refused by name.

Asserted against the source, like tests/test_status_text_ack_static.py: the code
under test is a string evaluated inside a real WhatsApp Web page, so there is no
seam to call it through.
"""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONTROLLER = (
    ROOT / "client" / "api_patches" / "src" / "controller" / "messageController.ts"
)


@pytest.fixture(scope="module")
def source() -> str:
    return CONTROLLER.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def reply_block(source: str) -> str:
    """Just the status-reply evaluate, so a match elsewhere in this very large
    controller cannot pass for one here."""
    start = source.index("winzapp_status_reply_")
    end = source.index("stored status reply result", start)
    return source[start:end]


class TestTheLiveModelIsTheFirstChoice:
    def test_the_model_is_quoted_directly(self, reply_block):
        assert "quotedMsg: quoted" in reply_block, (
            "the reply is quoting a rebuilt copy again — every field the "
            "rehydration drops becomes a getter that throws"
        )

    def test_it_is_tried_before_the_payload(self, reply_block):
        live = reply_block.index("quotedMsg: quoted")
        payload = reply_block.index("quotedMsgPayload: quotedPayload")
        assert live < payload, (
            "the payload path skips wa-js's canReplyMsg() gate, so trying it "
            "first is what turns a named refusal into a crash inside "
            "msgContextInfo()"
        )

    def test_the_payload_is_still_available_as_a_fallback(self, reply_block):
        assert "quotedMsgPayload: quotedPayload" in reply_block, (
            "the payload rung is not strictly weaker — it is the only route "
            "for a status whose model fails canReplyMsg()"
        )


class TestAFailedRungDoesNotEndTheReply:
    def test_each_attempt_is_caught(self, reply_block):
        """A throw from the first strategy must reach the second, not the
        caller. Before this there was one call and any throw was terminal."""
        strategies = reply_block.index("const strategies")
        assert "catch (attemptError)" in reply_block[strategies:]

    def test_exhausting_every_rung_still_fails_loudly(self, reply_block):
        """Silently returning no result would be worse than the crash it
        replaces: the send contract reads a missing id as a real failure only
        because something raises here."""
        assert "Every status reply strategy failed" in reply_block

    def test_no_send_result_is_never_treated_as_success(self, reply_block):
        assert "if (!sendResult) {" in reply_block


class TestTheNextRegressionIsSelfDiagnosing:
    def test_the_winning_strategy_is_recorded(self, reply_block):
        assert "quotedVia: usedVia" in reply_block

    def test_failed_attempts_are_recorded_even_when_a_later_rung_wins(
            self, reply_block):
        """A first rung that starts failing is how the next WhatsApp Web
        change announces itself. Recording only the winner would hide it until
        both rungs were broken — which is exactly how long this one went
        unnoticed."""
        assert "quotedAttempts: attempts" in reply_block
        assign = reply_block.index("quotedVia: usedVia")
        guard = reply_block.index("if (!sendResult) {")
        assert assign < guard, (
            "the attempts must be recorded before the all-failed throw, or "
            "the total-failure case reports nothing about why"
        )

    def test_the_existing_status_probe_fields_are_kept(self, reply_block):
        """These named the poster and proved the status was found, which is
        what made this diagnosable at all."""
        for field in ("statusModelsSeen", "statusMessagesSeen",
                      "matchedPoster", "quotedIsStatusV3"):
            assert field in reply_block


def test_the_two_copies_of_the_controller_agree():
    """setup_api.py restores api_patches over client/api. A fix that lands in
    only one of them ships the other."""
    live = ROOT / "client" / "api" / "src" / "controller" / "messageController.ts"
    if not live.is_file():
        pytest.skip("client/api is not installed in this checkout")
    assert live.read_text(encoding="utf-8") == CONTROLLER.read_text(encoding="utf-8")
