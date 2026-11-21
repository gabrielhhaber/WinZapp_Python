from types import SimpleNamespace

import pytest

from core.voice_nr_options import recording_nr_available, voice_nr_engine, voice_nr_moment


@pytest.mark.parametrize('neural,legacy,expected', [(False,'neural','basic'), (True,'basic','neural')])
def test_upstream_engine_is_authoritative(neural, legacy, expected):
    assert voice_nr_engine({'general': {'noise_reduction_neural': neural,
                                       'voice_nr_engine': legacy}}) == expected


@pytest.mark.parametrize('device,available', [('cpu',True), ('coreml',True), ('cuda12',False), ('cuda13',False)])
def test_recording_moment_falls_back_to_send_for_subprocess_devices(device, available):
    assert recording_nr_available(device) is available
    assert voice_nr_moment({'general': {'noise_reduction_device': device,
                                       'voice_nr_moment': 'record'}}) == ('record' if available else 'send')


def test_old_trial_settings_still_choose_basic_without_upstream_key():
    assert voice_nr_engine({'general': {'voice_nr_engine':'basic'}}) == 'basic'


class _Control:
    def __init__(self, selection=0):
        self.selection = selection
        self.enabled = True

    def GetSelection(self): return self.selection
    def SetSelection(self, value): self.selection = value
    def Enable(self, value): self.enabled = value


def _dialog(device, monkeypatch, installed=True, engine=0, moment=1):
    from core import noise_model
    from ui.dialogs.noise_reducer_options import NoiseReducerOptionsMixin
    monkeypatch.setattr(noise_model, "is_installed", lambda: installed)
    dialog = NoiseReducerOptionsMixin()
    dialog.device = device
    dialog._voice_nr_engine_combo = _Control(engine)
    dialog._voice_nr_moment_combo = _Control(moment)
    dialog._voice_nr_on_failure_combo = _Control(0)
    dialog._update_voice_nr_options_state()
    return dialog


@pytest.mark.parametrize('device,enabled', [('cpu',True), ('coreml',True), ('cuda12',False), ('cuda13',False)])
def test_moment_is_disabled_for_the_cuda_process(device, enabled, monkeypatch):
    dialog = _dialog(device, monkeypatch)
    assert dialog._voice_nr_moment_combo.enabled is enabled
    assert dialog._voice_nr_moment_combo.selection == (1 if enabled else 0)
    assert dialog._voice_nr_on_failure_combo.enabled
    assert dialog.options["moment"] == ("record" if enabled else "send")


def test_moment_and_failure_combos_are_unavailable_without_the_model(monkeypatch):
    dialog = _dialog('cpu', monkeypatch, installed=False)
    assert not dialog._voice_nr_moment_combo.enabled
    assert not dialog._voice_nr_on_failure_combo.enabled
    assert dialog._voice_nr_engine_combo.enabled


def test_basic_engine_disables_the_neural_only_combos(monkeypatch):
    dialog = _dialog('cpu', monkeypatch, engine=1)
    assert not dialog._voice_nr_moment_combo.enabled
    assert not dialog._voice_nr_on_failure_combo.enabled
    assert dialog.options["engine"] == "basic"


def test_options_from_settings_follow_the_upstream_engine():
    from ui.dialogs.noise_reducer_options import nr_options_from_settings
    assert nr_options_from_settings({'general': {'noise_reduction_neural': False}})["engine"] == "basic"
    assert nr_options_from_settings({})["engine"] == "neural"


def test_the_window_choices_are_saved_by_the_settings_page_only(monkeypatch):
    from types import SimpleNamespace
    from ui.dialogs.noise_reducer_dialog import NoiseReducerButtonMixin
    page = SimpleNamespace(
        main_window=SimpleNamespace(settings={'general': {}}),
        _noise_reducer_device=None,
        _noise_reducer_options={'engine': 'basic', 'moment': 'record', 'on_failure': 'keep_pending'})
    NoiseReducerButtonMixin._save_noise_reducer_device(page)
    assert page.main_window.settings['general'] == {
        'voice_nr_engine': 'basic', 'noise_reduction_neural': False,
        'voice_nr_moment': 'record', 'voice_nr_on_failure': 'keep_pending'}
    page._noise_reducer_options = None
    page.main_window.settings['general'].clear()
    NoiseReducerButtonMixin._save_noise_reducer_device(page)
    assert page.main_window.settings['general'] == {}


def test_a_missing_model_never_keeps_notes_pending():
    from core.voice_nr_options import effective_nr_on_failure
    settings = {'general': {'voice_nr_on_failure': 'keep_pending'}}
    assert effective_nr_on_failure(settings, True) == 'keep_pending'
    assert effective_nr_on_failure(settings, False) == 'basic'


def test_nr_local_id_prefers_the_local_id():
    from core.voice_nr_options import nr_local_id
    assert nr_local_id({'_local_id': 'a', 'key': {'id': 'b'}}) == 'a'
    assert nr_local_id({'key': {'id': 'b'}}) == 'b'
    assert nr_local_id({}) == '' and nr_local_id(None) == ''
