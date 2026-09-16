"""The native Save dialog selects the WHOLE suggested filename for editing,
extension included — so a user who starts retyping the name to fix/shorten
it loses the extension along with it unless they notice and retype that too.
Windows re-appends the extension from the wildcard's own first filter when
none is typed, so leaving it off the suggested name changes nothing about
what actually gets saved; it only stops the extension from being sitting
inside the initial selection where it can be typed over by mistake.

Three "Save as" dialogs build a suggested filename this way (conversations.py,
status_panel.py, media_viewer.py) — one test per call site, each mocking
wx.FileDialog to capture the constructor's own defaultFile/wildcard kwargs
without ever opening a real dialog window.
"""

import os

import wx

from status_panel import StatusPanel, _status_media_save_info
from ui.conversations import ConversationsPanel
from ui.media_viewer import MediaViewerDialog


class _CapturingFileDialog:
    """Records the kwargs it was constructed with; ShowModal() always
    cancels, so nothing past dialog construction ever has to be stubbed."""

    captured = None

    def __init__(self, *args, **kwargs):
        type(self).captured = kwargs

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def ShowModal(self):
        return wx.ID_CANCEL

    def GetPath(self):
        return ""


class _FakeI18n:
    def t(self, key):
        return {
            "save_as": "Salvar como",
            "save_audio_as": "Salvar áudio como",
            "all_files": "Todos os arquivos",
            "file_filter_audio": "Áudio",
            "file_filter_images": "Imagens",
            "file_filter_videos": "Vídeos",
            "file_filter_documents": "Documentos",
            "media_viewer_default_filename": "midia",
            "photo": "Foto",
            "video": "Vídeo",
            "message_type_audio": "Áudio",
        }.get(key, key)


class _FakeMainWindow:
    def __init__(self):
        self.i18n = _FakeI18n()
        self.settings = {}
        self.app_name = "WinZapp"


class TestConversationsPanelSaveMediaMessage:
    _stub_cls = type(
        "_Stub",
        (),
        {
            "_is_separator": ConversationsPanel._is_separator,
            "_resolve_media_filename": ConversationsPanel._resolve_media_filename,
            "save_media_message": ConversationsPanel.save_media_message,
        },
    )

    def _panel(self):
        panel = self._stub_cls()
        panel.main_window = _FakeMainWindow()
        return panel

    def _msg(self, mimetype="image/jpeg"):
        return {
            "key": {"id": "ABC123", "fromMe": False},
            "message": {"imageMessage": {"mimetype": mimetype}},
            "messageType": "imageMessage",
            "messageTimestamp": 1700000000,
        }

    def test_default_file_has_no_extension(self, monkeypatch):
        monkeypatch.setattr(wx, "FileDialog", _CapturingFileDialog)
        panel = self._panel()

        panel.save_media_message(self._msg())

        default_file = _CapturingFileDialog.captured["defaultFile"]
        assert os.path.splitext(default_file)[1] == ""

    def test_wildcards_first_filter_still_names_the_real_extension(self, monkeypatch):
        """The whole point: what Windows re-appends when nothing is typed
        must be the SAME extension that was left off defaultFile."""
        monkeypatch.setattr(wx, "FileDialog", _CapturingFileDialog)
        panel = self._panel()

        panel.save_media_message(self._msg())

        wildcard = _CapturingFileDialog.captured["wildcard"]
        assert wildcard.split("|")[0].upper().startswith("JPG")


class TestStatusPanelSaveStatusMedia:
    _stub_cls = type(
        "_Stub",
        (),
        {"_on_save_status_media": StatusPanel._on_save_status_media},
    )

    def test_default_file_has_no_extension(self, monkeypatch):
        monkeypatch.setattr(wx, "FileDialog", _CapturingFileDialog)
        panel = self._stub_cls()
        panel.main_window = _FakeMainWindow()
        panel._current_status = {
            "messageType": "imageMessage",
            "message": {"imageMessage": {"mimetype": "image/png"}},
        }

        panel._on_save_status_media(None)

        assert _CapturingFileDialog.captured["defaultFile"] == "status"
        wildcard = _CapturingFileDialog.captured["wildcard"]
        assert wildcard.split("|")[1].lower() == "*.png"


class TestMediaViewerSave:
    _stub_cls = type(
        "_Stub",
        (),
        {"_on_save": MediaViewerDialog._on_save},
    )

    def _panel(self, tmp_path):
        panel = self._stub_cls()
        panel.i18n = _FakeI18n()
        panel.main_window = _FakeMainWindow()
        media_path = tmp_path / "source.jpg"
        media_path.write_bytes(b"fake")
        panel._current_path = str(media_path)
        panel._loaded_paths = {}
        panel.index = 0
        panel._current_item = lambda: {"filename": "IMG-20250101-WA0001.jpg"}
        return panel

    def test_default_file_has_no_extension_and_wildcard_matches(self, monkeypatch, tmp_path):
        monkeypatch.setattr(wx, "FileDialog", _CapturingFileDialog)
        panel = self._panel(tmp_path)

        panel._on_save(None)

        default_file = _CapturingFileDialog.captured["defaultFile"]
        assert os.path.splitext(default_file)[1] == ""
        assert default_file == "IMG-20250101-WA0001"
        wildcard = _CapturingFileDialog.captured["wildcard"]
        assert wildcard.split("|")[1].lower() == "*.jpg"

    def test_a_filename_with_no_extension_falls_back_to_all_files(self, monkeypatch, tmp_path):
        """No extension to safely re-append means the old "All files" wildcard
        is still the honest answer — nothing here should invent one."""
        monkeypatch.setattr(wx, "FileDialog", _CapturingFileDialog)
        panel = self._panel(tmp_path)
        panel._current_item = lambda: {"filename": "status_media"}

        panel._on_save(None)

        assert _CapturingFileDialog.captured["defaultFile"] == "status_media"
        assert _CapturingFileDialog.captured["wildcard"] == "Todos os arquivos (*.*)|*.*"
