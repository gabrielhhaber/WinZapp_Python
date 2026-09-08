"""The probe that tells a blind user their installation is incompatible.

Two separate bugs are pinned here.

The Node half: no send handler may turn a *post-send* validation verdict into
an exception. Every one of them runs after `await req.client.sendX(...)` has
resolved, so the message is already on the network; throwing lands in
returnError, i.e. HTTP 500, which main.py classifies as retryable and
MessageQueue then resends up to four times. The verdict rides back inside the
ordinary 201 body instead, where core/send_contract.py — the only side of this
that is non-retryable by construction — makes it permanent.

The Python half: _check_send_capabilities() used to run from
_check_wpp_version_pin(), i.e. from ensure_wpp_running(), before the session
was paired. The route sits behind statusConnection, which answers 404
{"response": null, "status": "Disconnected"} until a session is attached — and
that answer was read as a verdict, so every single cold start announced, out
loud and with interrupt=True, that the installation was incompatible.
"""

from pathlib import Path

import pytest

import main
from main import MainWindow


ROOT = Path(__file__).resolve().parents[1]
DEVICE = ROOT / "client/api_patches/src/controller/deviceController.ts"
ROUTES = ROOT / "client/api_patches/src/routes/index.ts"
MESSAGES = ROOT / "client/api_patches/src/controller/messageController.ts"


def test_probe_covers_every_send_primitive_and_reaction_signature():
    source = DEVICE.read_text(encoding="utf-8")
    probe = source[source.index("export async function getSendCapabilities") :]
    for capability in (
        "sendTextMessage",
        "sendFileMessage",
        "sendTextStatus",
        "sendImageStatus",
        "sendVideoStatus",
        "sendStatusReaction",
        "mintStatusReactionKey",
        "applyOptimisticStatusReaction",
    ):
        assert capability in probe
    # A probe that gives up on the reaction module earlier than reactMessage()
    # does reports "incompatible" for a like that would have worked.
    assert "ensureLazyModule" in probe
    assert "'/api/:session/send-capabilities'" in ROUTES.read_text(encoding="utf-8")


def test_no_send_handler_turns_a_post_send_verdict_into_a_500():
    source = MESSAGES.read_text(encoding="utf-8")
    for operation in (
        "send-message",
        "send-file",
        "send-voice-base64",
        "send-reply",
        "send-mentioned",
    ):
        assert f"'{operation}'" in source
    assert source.count("auditSendResult(") >= 7  # the definition plus 6 uses

    # describeSendRejection() returns the reason; nothing on this path throws.
    verdict = source[source.index("function describeSendRejection") :]
    verdict = verdict[: verdict.index("async function watchMediaUpload")]
    code = [
        line for line in verdict.splitlines()
        if not line.strip().startswith(("//", "/*", "*"))
    ]
    assert not [line for line in code if "throw" in line], code
    assert "res.status(500)" not in verdict


class _I18n:
    def t(self, key):
        return f"<{key}>"


class _Response:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


class _Stub:
    """Minimal stand-in for MainWindow for the capabilities probe."""

    _SEND_CAPABILITIES_RETRY_DELAYS = MainWindow._SEND_CAPABILITIES_RETRY_DELAYS

    def __init__(self):
        self.wpp_server = "http://127.0.0.1"
        self.wpp_port = 6300
        self.token = "tok"
        self.i18n = _I18n()
        self.spoken = []
        self.sleeps = []
        self._send_capabilities_checked = True
        # The probe only ever runs from a confirmed connection, and the retry
        # loop below reads this before waiting.
        self._wa_connected = True

    def output(self, text, *args, **kwargs):
        self.spoken.append((text, args, kwargs))

    _check_send_capabilities = MainWindow._check_send_capabilities


@pytest.fixture
def stub(monkeypatch):
    monkeypatch.setattr(main.wx, "CallAfter", lambda fn, *a, **k: fn(*a, **k))
    stub = _Stub()
    # The retry delays are minutes long; record them instead of living through
    # them, the way the other main.py tests do.
    monkeypatch.setattr(main.time, "sleep", lambda seconds: stub.sleeps.append(seconds))
    return stub


def _answer(monkeypatch, status_code, body):
    monkeypatch.setattr(
        main, "api_get", lambda *a, **k: _Response(status_code, body)
    )


class TestAnUnavailableProbeSaysNothing:
    def test_the_disconnected_404_every_cold_start_returns(self, stub, monkeypatch):
        _answer(monkeypatch, 404, {"response": None, "status": "Disconnected"})

        stub._check_send_capabilities()

        assert stub.spoken == []
        assert not hasattr(stub, "_send_capabilities_warning")

    def test_a_probe_that_could_not_run_in_the_page(self, stub, monkeypatch):
        _answer(monkeypatch, 500, {"status": "error", "message": "Execution context"})

        stub._check_send_capabilities()

        assert stub.spoken == []

    def test_a_request_that_failed_outright(self, stub, monkeypatch):
        def _boom(*args, **kwargs):
            raise OSError("connection refused")

        monkeypatch.setattr(main, "api_get", _boom)

        stub._check_send_capabilities()

        assert stub.spoken == []


class TestAnUnansweredProbeIsAskedAgain:
    """The probe used to get one attempt per process, tied to the same latch as
    the connected sound. On wppconnect 2.3.2 that attempt can be spent on
    nothing: CONNECTED is promoted by the state listener even when
    isConnected() never succeeded, and the route is fronted by a
    statusConnection probe waiting on the same reloading page — so the one shot
    times out, "unavailable" goes to the log, and the incompatibility warning
    is silenced for the session that most needed it.
    """

    def test_an_answer_without_a_verdict_re_arms_the_probe(
        self, stub, monkeypatch
    ):
        _answer(monkeypatch, 404, {"response": None, "status": "Disconnected"})

        stub._check_send_capabilities()

        assert stub._send_capabilities_checked is False

    def test_a_request_that_failed_outright_re_arms_the_probe(
        self, stub, monkeypatch
    ):
        def _boom(*args, **kwargs):
            raise OSError("timed out")

        monkeypatch.setattr(main, "api_get", _boom)

        stub._check_send_capabilities()

        assert stub._send_capabilities_checked is False

    def test_a_real_verdict_does_not_re_arm_it(self, stub, monkeypatch):
        """Compatible or not, the runtime answered — asking again on every
        reconnection would be a request per connection drop for no new
        information."""
        _answer(monkeypatch, 200, {"response": {"compatible": True, "missing": []}})

        stub._check_send_capabilities()

        assert stub._send_capabilities_checked is True

    def test_an_incompatible_verdict_does_not_re_arm_it_either(
        self, stub, monkeypatch
    ):
        _answer(monkeypatch, 409, {"response": {"compatible": False}})

        stub._check_send_capabilities()

        assert stub._send_capabilities_checked is True

    def test_a_retry_after_an_unavailable_answer_still_only_speaks_once(
        self, stub, monkeypatch
    ):
        """Re-arming must not turn into repeating: _send_capabilities_warning
        is what keeps a screen reader from hearing the same verdict on every
        reconnection."""
        _answer(monkeypatch, 404, {"response": None, "status": "Disconnected"})
        stub._check_send_capabilities()

        _answer(monkeypatch, 409, {"response": {"compatible": False}})
        stub._check_send_capabilities()
        stub._send_capabilities_checked = False
        stub._check_send_capabilities()

        assert len(stub.spoken) == 1


class TestTheRetryInsideOneConnection:
    """Re-arming the latch is not enough on its own, and this is the case it
    misses. _set_wa_connected() returns early when nothing changed, so the
    latch is only re-read on a real offline→online transition — while the
    session this exists for ("the event wins": CONNECTED promoted by the state
    listener with isConnected() never having succeeded) is exactly the one
    whose connection may never oscillate again. Without the retry the warning
    is dropped for the whole session, in the release whose point is that the
    runtime changed.
    """

    def _answers(self, monkeypatch, *responses):
        """Return each response in turn, the last one repeating."""
        queue = list(responses)
        calls = []

        def _fake_get(*args, **kwargs):
            calls.append(kwargs)
            return queue.pop(0) if len(queue) > 1 else queue[0]

        monkeypatch.setattr(main, "api_get", _fake_get)
        return calls

    def test_an_unanswered_probe_is_asked_again_without_a_reconnection(
        self, stub, monkeypatch
    ):
        calls = self._answers(
            monkeypatch, _Response(404, {"response": None, "status": "Disconnected"})
        )

        stub._check_send_capabilities()

        assert len(calls) == 1 + len(_Stub._SEND_CAPABILITIES_RETRY_DELAYS)
        assert stub.sleeps == list(_Stub._SEND_CAPABILITIES_RETRY_DELAYS)
        # Spent the budget without an answer: hand the next confirmed
        # connection its own chance.
        assert stub._send_capabilities_checked is False

    def test_a_verdict_arriving_on_a_retry_is_still_announced(
        self, stub, monkeypatch
    ):
        calls = self._answers(
            monkeypatch,
            _Response(404, {"response": None, "status": "Disconnected"}),
            _Response(409, {"response": {"compatible": False, "missing": ["x"]}}),
        )

        stub._check_send_capabilities()

        assert len(calls) == 2
        assert [text for text, _, _ in stub.spoken] == [
            "<send_capabilities_incompatible>"
        ]
        # A real verdict, so nothing is owed to a later connection.
        assert stub._send_capabilities_checked is True

    def test_a_dropped_connection_ends_the_retries_instead_of_waiting(
        self, stub, monkeypatch
    ):
        """The reconnection re-arms the latch and starts a fresh probe of its
        own, so waiting here would only race it — and the delay is checked
        before it is slept through, not after."""
        self._answers(
            monkeypatch, _Response(404, {"response": None, "status": "Disconnected"})
        )
        stub._wa_connected = False

        stub._check_send_capabilities()

        assert stub.sleeps == []
        assert stub._send_capabilities_checked is False

    def test_a_shutdown_ends_the_retries_too(self, stub, monkeypatch):
        self._answers(
            monkeypatch, _Response(404, {"response": None, "status": "Disconnected"})
        )
        stub._shutting_down = True

        stub._check_send_capabilities()

        assert stub.sleeps == []

    def test_a_compatible_answer_never_retries(self, stub, monkeypatch):
        calls = self._answers(
            monkeypatch, _Response(200, {"response": {"compatible": True}})
        )

        stub._check_send_capabilities()

        assert len(calls) == 1
        assert stub.sleeps == []


class TestOnlyARealVerdictIsAnnounced:
    def test_a_compatible_answer_is_silent(self, stub, monkeypatch):
        _answer(monkeypatch, 200, {"response": {"compatible": True, "missing": []}})

        stub._check_send_capabilities()

        assert stub.spoken == []

    def test_an_incompatible_answer_is_announced(self, stub, monkeypatch):
        _answer(
            monkeypatch,
            409,
            {"response": {"compatible": False, "missing": ["statusReaction"]}},
        )

        stub._check_send_capabilities()

        assert [text for text, _, _ in stub.spoken] == [
            "<send_capabilities_incompatible>"
        ]

    def test_it_does_not_interrupt_the_warning_queued_before_it(
        self, stub, monkeypatch
    ):
        """On the one path where both fire — an unpinned WhatsApp Web build,
        the documented cause of silent send failure — interrupting cut the
        first warning off mid-sentence and the user heard neither."""
        _answer(monkeypatch, 409, {"response": {"compatible": False}})

        stub._check_send_capabilities()

        _, args, kwargs = stub.spoken[0]
        assert args == ()
        assert kwargs == {}

    def test_the_same_verdict_is_only_announced_once(self, stub, monkeypatch):
        _answer(monkeypatch, 409, {"response": {"compatible": False}})

        stub._check_send_capabilities()
        stub._check_send_capabilities()

        assert len(stub.spoken) == 1


def test_the_probe_runs_from_the_first_confirmed_connection():
    """Not from _check_wpp_version_pin(): ensure_wpp_running() calls that from
    MainWindow.__init__, before init_UI and before the session is paired."""
    source = (ROOT / "client/main.py").read_text(encoding="utf-8")

    pin = source[source.index("    def _check_wpp_version_pin(") :]
    pin = pin[: pin.index("\n    def _check_send_capabilities(")]
    assert "_check_send_capabilities" not in pin

    connect = source[
        source.index('logging.info("[connection] WhatsApp connection is up') :
    ]
    connect = connect[: connect.index("self.connected_sound.play()")]
    assert "self._check_send_capabilities" in connect
    # Its own latch, not the one that also gates the connected sound.
    assert "if not self._send_capabilities_checked:" in connect
