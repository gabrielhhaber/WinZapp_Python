"""Status posts: WhatsApp's status sender must accept WA-JS's positional call.

Reported 2026-09-23: every status post (text, video, audio) failed with
messageSendResult=ERROR_UNKNOWN. WA-JS 4.6.0 wraps
WAWebSendMsgJob.encryptAndSendMsg and, for status@broadcast, calls
encryptAndSendStatusMsg(msg, proto, reporter) positionally inside a try/catch
that returns null on any error. Current WhatsApp takes one object. The first
shim wrapped encryptAndSendMsg, which only works while it is the outermost
wrapper; measured over CDP, WA-JS's wrapper had ended up outside it and a fake
record reached the sender with 3 positional arguments. The adapter now sits
on encryptAndSendStatusMsg itself, which WA-JS reads live, so wrapper order no
longer matters.

The code runs inside the WhatsApp page, so these are source assertions on the
patch, in the style of tests/test_call_control_api_patch.py.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "client/api_patches/src/util/createSessionUtil.ts").read_text(encoding="utf-8")


def _adapter():
    start = SOURCE.index("async function restoreStatusSender(")
    return SOURCE[start: SOURCE.index("\n}\n", start)]


def test_the_adapter_sits_on_the_status_sender_itself():
    body = _adapter()
    assert "moduleRequire(\n            'WAWebEncryptAndSendStatusMsg'" in body
    assert "statusModule.encryptAndSendStatusMsg = adapted;" in body


def test_it_no_longer_depends_on_wrapping_encrypt_and_send_msg():
    """Wrapping encryptAndSendMsg only works while ours is the outermost
    wrapper, and WA-JS's own wrapper ended up outside it."""
    body = _adapter()
    assert "WAWebSendMsgJob" not in body
    assert "encryptAndSendMsg =" not in body


def test_a_positional_call_becomes_the_object_whatsapp_expects():
    body = _adapter()
    assert "const [msgProtobuf, metricsReporter] = rest;" in body
    assert "sendMsgRecord: first," in body


def test_whatsapps_own_object_call_passes_through():
    body = _adapter()
    assert "'sendMsgRecord' in first" in body
    assert "return sendStatus.call(this, first, ...rest);" in body


def test_it_installs_once_and_leaves_a_positional_build_alone():
    body = _adapter()
    assert "if (sendStatus.__winzappPositionalAdapter) return true;" in body
    assert "if (sendStatus.length !== 1) return true;" in body
    assert "(adapted as any).__winzappPositionalAdapter = true;" in body


def test_it_is_still_installed_at_session_start():
    assert SOURCE.count("restoreStatusSender(client.page, req.logger, session)") == 2
