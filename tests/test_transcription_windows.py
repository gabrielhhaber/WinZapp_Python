"""The Transcription tab's options in windows of their own
(ui/dialogs/transcription_windows.py), so every control has an Alt key.

Where each control lands is checked by tests/test_settings_mnemonics.py
through tests/settings_controls.py; this checks the windows themselves.
"""

from types import SimpleNamespace
from unittest.mock import Mock

from tests.settings_controls import collect
from ui.dialogs.transcription_windows import TranscriptionWindowsMixin, window_title

WINDOWS = collect()


def _keys(window):
    return {k for k, _ctor, _where in WINDOWS[window]}


def test_a_title_is_the_button_without_its_alt_key_or_ellipsis():
    assert window_title("&Saved transcriptions...") == "Saved transcriptions"
    assert window_title("Bibliotecas CUDA para a &GPU…") == "Bibliotecas CUDA para a GPU"


def test_each_group_is_in_its_window_and_off_the_tab():
    tab = _keys("page self._transcription_holder")
    groups = {
        "self._transcription_advanced_dialog": {"transcription_precision_label",
                                                "transcription_keep_loaded_check",
                                                "transcription_no_dialog_check"},
        "self._transcription_saved_dialog": {"transcription_cache_check",
                                             "transcription_clear_all_cache_btn"},
        "self._transcription_statuses_dialog": {"transcription_status_auto_check",
                                                "transcription_status_clear_btn"},
        "self._transcription_folders_dialog": {"transcription_models_dir_label",
                                               "transcription_external_add_btn"},
        "self._transcription_cuda_dialog": {"transcription_cuda_runtime_label",
                                            "transcription_reuse_cuda_label"},
        "self._transcription_whisper_dialog": {"transcription_whisper_cpp_label",
                                               "transcription_reuse_whisper_cpp_label"},
    }
    for window, keys in groups.items():
        assert keys <= _keys(f"window {window}"), window
        assert not keys & tab, window
    # What stays on the tab: the model, where it runs, the language.
    assert {"transcription_model_label", "transcription_device_label",
            "transcription_language_label", "transcription_auto_transcribe_check"} <= tab


def test_the_two_reuse_rows_are_never_in_one_window():
    """Their buttons share their words, and so their letters."""
    cuda = _keys("window self._transcription_cuda_dialog")
    whisper = _keys("window self._transcription_whisper_dialog")
    assert "transcription_reuse_whisper_cpp_label" not in cuda
    assert "transcription_reuse_cuda_label" not in whisper


class _Windows:
    for _name in ("_transcription_windows", "_register_transcription_windows",
                  "_refresh_transcription_window_labels", "_finish_transcription_windows"):
        locals()[_name] = getattr(TranscriptionWindowsMixin, _name)
    del _name

    def __init__(self):
        strings = {"transcription_advanced_button": "&Advanced options..."}
        self.main_window = SimpleNamespace(i18n=SimpleNamespace(t=lambda k: strings.get(k, f"&{k}...")))
        for name in ("advanced", "saved", "statuses", "folders", "cuda", "whisper"):
            setattr(self, f"_transcription_{name}_dialog", Mock(name=name))
            setattr(self, f"_transcription_{name}_panel", Mock())
            setattr(self, f"_transcription_{name}_sizer", Mock())
            setattr(self, f"_transcription_{name}_btn", Mock())
        self._transcription_page = object()
        self._notebook = Mock()
        self._notebook.FindPage.return_value = 7


def test_every_window_is_offered_to_the_settings_search_and_opens_on_the_option():
    windows = _Windows()
    windows._register_transcription_windows()
    calls = windows._notebook.add_subpage.call_args_list
    assert len(calls) == 6
    index, caption, panel, opener = calls[0].args
    assert (index, caption, panel) == (7, "Advanced options", windows._transcription_advanced_panel)
    control = object()
    opener(control)
    windows._transcription_advanced_dialog.open.assert_called_once_with(control)


def test_the_buttons_and_titles_follow_the_language():
    windows = _Windows()
    windows._refresh_transcription_window_labels()
    windows._transcription_advanced_btn.SetLabel.assert_called_once_with("&Advanced options...")
    windows._transcription_advanced_dialog.SetTitle.assert_called_once_with("Advanced options")


def test_each_panel_gets_its_sizer_and_becomes_its_windows_body():
    windows = _Windows()
    windows._finish_transcription_windows()
    windows._transcription_cuda_panel.SetSizer.assert_called_once_with(windows._transcription_cuda_sizer)
    windows._transcription_cuda_dialog.set_body.assert_called_once_with(windows._transcription_cuda_panel)
