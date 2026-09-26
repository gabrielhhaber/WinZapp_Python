import os
import sys
import types
from unittest.mock import MagicMock

try:
    import wx
    import wx.adv
except ImportError:
    for _mod in ("wx", "wx.adv"):
        if _mod not in sys.modules:
            mod = types.ModuleType(_mod)
            if "." not in _mod:
                mod.__path__ = []
            sys.modules[_mod] = mod
    class _FakeWxModule(types.ModuleType):
        def __getattr__(self, name):
            if name == "__file__":
                return "<fake_wx>"
            if name == "CallAfter":
                return lambda fn, *a, **k: fn(*a, **k)
            if name.startswith("ID_") or name.startswith("wxID_") or name in ("HORIZONTAL", "VERTICAL", "EXPAND", "ALL"):
                return 1000
            if name in ("Frame", "Panel", "Dialog", "Accessible", "Timer", "App", "Window", "Control"):
                return object
            return MagicMock
    sys.modules["wx"].__class__ = _FakeWxModule
    sys.modules["wx.adv"].__class__ = _FakeWxModule

try:
    import accessible_output2
    from accessible_output2 import outputs
except ImportError:
    if "accessible_output2" not in sys.modules:
        sys.modules["accessible_output2"] = types.ModuleType("accessible_output2")
    sys.modules["accessible_output2.outputs"] = types.ModuleType("accessible_output2.outputs")
    sys.modules["accessible_output2"].outputs = sys.modules["accessible_output2.outputs"]

try:
    import sound_lib
    from sound_lib import stream, output, main, effects
except ImportError:
    for _mod in (
        "sound_lib",
        "sound_lib.output",
        "sound_lib.stream",
        "sound_lib.main",
        "sound_lib.effects",
    ):
        if _mod not in sys.modules:
            mod = types.ModuleType(_mod)
            if "." not in _mod:
                mod.__path__ = []
            sys.modules[_mod] = mod

    sys.modules["sound_lib.main"].bass_call = lambda *a, **k: None
    sys.modules["sound_lib.stream"].FileStream = object
    sys.modules["sound_lib.output"].Output = object
    sys.modules["sound_lib.effects"].Tempo = object

from main import MainWindow
from tests.god_modules import patch_main_global


def test_resend_media_message_preserves_document_filename(tmp_path, monkeypatch):
    stub = MainWindow.__new__(MainWindow)
    stub.key = b"dummy_key_32_bytes_long_12345678"
    stub.token = "test_token"
    stub.wpp_server = "http://127.0.0.1"
    stub.wpp_port = 6300
    stub.output = MagicMock()
    stub.handle_media_message = MagicMock(return_value=True)

    # Mock data_path and decrypt_bytes
    temp_media = tmp_path / "3EB0F5F2800B0C540313D1.wzmedia"
    temp_media.write_bytes(b"encrypted_content")

    patch_main_global(monkeypatch, "data_path", lambda folder, fname: str(tmp_path / fname))
    monkeypatch.setattr("core.utils.decrypt_bytes", lambda data, key: b"decrypted_file_bytes")

    sent_calls = []
    def _mock_send_media(target_jid, file_path, media_type, caption="", custom_filename=""):
        sent_calls.append({
            "target_jid": target_jid,
            "file_path": file_path,
            "media_type": media_type,
            "caption": caption,
            "custom_filename": custom_filename,
        })
        return True

    stub.send_media_attachment = _mock_send_media

    msg = {
        "key": {
            "id": "false_120363409652783723@g.us_3EB0F5F2800B0C540313D1",
            "remoteJid": "120363409652783723@g.us",
        },
        "message": {
            "documentMessage": {
                "fileName": "relatorio_final.zip",
                "caption": "Segue o arquivo",
                "mimetype": "application/zip",
            }
        }
    }

    success = stub.resend_media_message_with_caption(msg, "5511999999999@s.whatsapp.net")
    assert success is True
    assert len(sent_calls) == 1
    assert sent_calls[0]["custom_filename"] == "relatorio_final.zip"
    assert sent_calls[0]["caption"] == "Segue o arquivo"
    assert sent_calls[0]["media_type"] == "document"
