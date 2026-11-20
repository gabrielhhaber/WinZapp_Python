from types import SimpleNamespace

import pytest

from core.voice_nr_options import recording_nr_available, voice_nr_engine, voice_nr_moment


@pytest.mark.parametrize('general,expected', [
    ({'noise_reduction_neural': False}, 'basic'),
    ({'noise_reduction_neural': True}, 'neural'),
    ({}, 'neural'),
    # A trial build's own key decides nothing: the published one is the only source.
    ({'noise_reduction_neural': True, 'voice_nr_engine': 'basic'}, 'neural'),
])
def test_the_published_key_is_the_only_engine_source(general, expected):
    assert voice_nr_engine({'general': general}) == expected


@pytest.mark.parametrize('device,available', [('cpu',True), ('coreml',True), ('cuda12',False), ('cuda13',False)])
def test_recording_moment_falls_back_to_send_for_subprocess_devices(device, available):
    assert recording_nr_available(device) is available
    assert voice_nr_moment({'general': {'noise_reduction_device': device,
                                       'voice_nr_moment': 'record'}}) == ('record' if available else 'send')


def _options_mixin():
    # The real class: importing the module needs wx, but nothing here
    # creates a window (the controls are plain doubles).
    from ui.dialogs.voice_recording_options import VoiceRecordingOptionsMixin
    return VoiceRecordingOptionsMixin


class _Control:
    def __init__(self, selection=0, value=True):
        self.selection, self.value = selection, value
        self.enabled = True

    def GetSelection(self): return self.selection
    def SetSelection(self, value): self.selection = value
    def GetValue(self): return self.value
    def Enable(self, value): self.enabled = value


def _panel(device, moment_selection=1):
    panel = _options_mixin()()
    panel.main_window = SimpleNamespace(settings={'general': {'noise_reduction_device': device}})
    panel._noise_reduction_check = _Control()
    panel._voice_nr_engine_combo = _Control(selection=0)
    panel._voice_nr_moment_combo = _Control(selection=moment_selection)
    panel._voice_nr_on_failure_combo = _Control()
    return panel


@pytest.mark.parametrize('device,enabled', [('cpu',True), ('coreml',True), ('cuda12',False), ('cuda13',False)])
def test_actual_settings_helper_disables_recording_for_cuda(device, enabled):
    panel = _panel(device)
    panel._update_voice_nr_options_state()
    assert panel._voice_nr_moment_combo.enabled is enabled
    assert panel._voice_nr_moment_combo.selection == (1 if enabled else 0)


def test_switching_to_a_card_and_back_keeps_while_recording():
    panel = _panel('cpu')
    panel._voice_nr_moment_wanted = 1          # "while recording", chosen
    panel._noise_reducer_device = 'cuda12'
    panel._update_voice_nr_options_state()
    assert panel._voice_nr_moment_combo.selection == 0
    panel._noise_reducer_device = 'cpu'
    panel._update_voice_nr_options_state()
    assert panel._voice_nr_moment_combo.selection == 1


def test_the_users_moment_is_saved_while_a_card_shows_when_sending():
    # voice_nr_moment() runs it at send time on a card; the stored choice
    # comes back with the processor.
    panel = _panel('cuda12')
    panel._voice_recording_mode_radio = _Control()
    panel._voice_bitrate_choice = _Control(selection=-1)
    panel._voice_bitrate_stereo_choice = _Control(selection=-1)
    panel._voice_nr_moment_wanted = 1
    panel._update_voice_nr_options_state()
    panel._save_voice_recording_options()
    general = panel.main_window.settings['general']
    assert general['voice_nr_moment'] == 'record'
    assert voice_nr_moment(panel.main_window.settings) == 'send'


def test_newly_selected_device_is_used_before_settings_are_saved():
    panel = _options_mixin()()
    panel.main_window = SimpleNamespace(settings={'general':{'noise_reduction_device':'cpu'}})
    panel._noise_reducer_device = 'cuda13'
    assert panel._voice_nr_selected_device() == 'cuda13'
