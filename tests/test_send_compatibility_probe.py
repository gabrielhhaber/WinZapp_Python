from pathlib import Path


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
    assert "'/api/:session/send-capabilities'" in ROUTES.read_text(encoding="utf-8")


def test_all_primary_send_handlers_reject_false_success():
    source = MESSAGES.read_text(encoding="utf-8")
    for operation in (
        "send-message",
        "send-file",
        "send-voice-base64",
        "send-reply",
        "send-mentioned",
    ):
        assert f"'{operation}'" in source
    assert source.count("assertSendAccepted(") >= 6
