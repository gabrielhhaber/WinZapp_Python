"""MediaViewerDialog._on_save(): a failed copy used to show the bare OS
exception text (str(exc), English or the Windows UI language) with no
sentence saying what failed. It now goes through media_viewer_save_failed,
keeping the exception detail in {error}.

Same unbound-method-on-a-stub route as tests/test_save_as_default_filename_has_no_extension.py;
wx.FileDialog and wx.MessageBox are replaced, so no window is created.
"""

import wx

from ui.media_viewer import MediaViewerDialog


class _FakeI18n:
    def t(self, key):
        return {"media_viewer_save_failed": "SAVE FAILED:\n{error}"}.get(key, key)


class _FakeMainWindow:
    app_name = "WinZapp"
    settings = {}

    def remember_save_folder(self, path):
        pass


class _OkFileDialog:
    target = ""

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def ShowModal(self):
        return wx.ID_OK

    def GetPath(self):
        return type(self).target


class _Stub:
    _on_save = MediaViewerDialog._on_save


def test_copy_failure_shows_translated_message_with_the_os_detail(tmp_path, monkeypatch):
    src = tmp_path / "source.jpg"
    src.write_bytes(b"fake")
    _OkFileDialog.target = str(tmp_path / "missing_dir" / "out.jpg")
    monkeypatch.setattr(wx, "FileDialog", _OkFileDialog)
    monkeypatch.setattr("ui.media_viewer.schedule_deselect_extension", lambda name: None)
    shown = []
    monkeypatch.setattr(wx, "MessageBox", lambda *a, **k: shown.append(a))

    panel = _Stub()
    panel.i18n = _FakeI18n()
    panel.main_window = _FakeMainWindow()
    panel._current_path = str(src)
    panel._loaded_paths = {}
    panel.index = 0
    panel._current_item = lambda: {"filename": "IMG.jpg"}

    panel._on_save(None)

    assert len(shown) == 1
    message, title = shown[0][0], shown[0][1]
    assert message.startswith("SAVE FAILED:\n")
    assert "out.jpg" in message  # the OSError detail is kept
    assert title == "WinZapp"
