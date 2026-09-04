"""Text status posting must not depend on a DOM-created status chat.

WA-JS 4.6.0 asks ChatStore for status@broadcast before sending, but current
WhatsApp Web no longer inserts that virtual chat when StatusV3Store syncs.
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTROLLER = (
    ROOT / "client" / "api_patches" / "src" / "controller" / "statusController.ts"
)


def test_status_warmup_seeds_the_virtual_chat_model():
    source = CONTROLLER.read_text(encoding="utf-8")
    warmup = source[source.index("async function ensureStatusChat") :]
    warmup = warmup[: warmup.index("export async function sendTextStorie")]

    assert "WidFactory?.createWid?.('status@broadcast')" in warmup
    assert "!whatsapp.ChatStore.get(statusWid)" in warmup
    assert "whatsapp.ChatStore.add(new whatsapp.ChatModel({ id: statusWid }))" in warmup
