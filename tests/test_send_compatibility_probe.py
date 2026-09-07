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

    def __init__(self):
        self.wpp_server = "http://127.0.0.1"
        self.wpp_port = 6300
        self.token = "tok"
        self.i18n = _I18n()
        self.spoken = []

    def output(self, text, *args, **kwargs):
        self.spoken.append((text, args, kwargs))

    _check_send_capabilities = MainWindow._check_send_capabilities


@pytest.fixture
def stub(monkeypatch):
    monkeypatch.setattr(main.wx, "CallAfter", lambda fn, *a, **k: fn(*a, **k))
    return _Stub()


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

    connect = source[source.index("            first_ever_connect = not self._wa_connect_announced") :]
    connect = connect[: connect.index("self.connected_sound.play()")]
    assert "self._check_send_capabilities" in connect
