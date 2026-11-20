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


def _options_mixin():
    from ui.dialogs.voice_recording_options import VoiceRecordingOptionsMixin
    return VoiceRecordingOptionsMixin


class _Control:
    def __init__(self, selection=0, value=True):
        self.selection, self.value = selection, value
        self.enabled = True
        self.shown = True

    def GetSelection(self): return self.selection
    def SetSelection(self, value): self.selection = value
    def GetValue(self): return self.value
    def SetValue(self, value): self.value = value
    def Enable(self, value): self.enabled = value
    def Hide(self): self.shown = False


def _panel(device, monkeypatch, installed=True, selection=0):
    from core import noise_model
    monkeypatch.setattr(noise_model, "is_installed", lambda: installed)
    panel = _options_mixin()()
    panel.main_window = SimpleNamespace(settings={'general': {'noise_reduction_device': device}})
    panel._noise_reduction_check = _Control()
    panel._voice_nr_engine_combo = _Control(selection=selection)
    panel._voice_nr_moment_combo = _Control(selection=1)
    panel._voice_nr_on_failure_combo = _Control()
    panel._update_voice_nr_options_state()
    return panel


@pytest.mark.parametrize('device,enabled', [('cpu',True), ('coreml',True), ('cuda12',False), ('cuda13',False)])
def test_actual_settings_helper_disables_recording_for_cuda(device, enabled, monkeypatch):
    panel = _panel(device, monkeypatch)
    assert panel._voice_nr_moment_combo.enabled is enabled
    assert panel._voice_nr_moment_combo.selection == (1 if enabled else 0)
    assert panel._voice_nr_on_failure_combo.enabled


def test_moment_and_failure_combos_are_unavailable_without_the_model(monkeypatch):
    panel = _panel('cpu', monkeypatch, installed=False)
    assert not panel._voice_nr_moment_combo.enabled
    assert not panel._voice_nr_on_failure_combo.enabled
    assert panel._voice_nr_engine_combo.enabled


def test_basic_engine_disables_the_neural_only_combos(monkeypatch):
    panel = _panel('cpu', monkeypatch, selection=1)
    assert not panel._voice_nr_moment_combo.enabled
    assert not panel._voice_nr_on_failure_combo.enabled


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


def test_newly_selected_device_is_used_before_settings_are_saved():
    panel = _options_mixin()()
    panel.main_window = SimpleNamespace(settings={'general':{'noise_reduction_device':'cpu'}})
    panel._noise_reducer_device = 'cuda13'
    assert panel._voice_nr_selected_device() == 'cuda13'
