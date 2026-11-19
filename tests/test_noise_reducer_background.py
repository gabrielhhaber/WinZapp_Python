"""The noise reducer window's downloads in the background (Settings >
General): refused out loud while another run goes, said once (by the main
window), and the window shows what is installed once the run ends. No
window is created: the mixin's methods on a stub."""
from types import SimpleNamespace

from ui.dialogs.noise_model_section import NoiseModelSectionMixin


class _Button:
    def __init__(self):
        self.enabled = True

    def Disable(self):
        self.enabled = False


class _Field:
    def __init__(self):
        self.focused = False

    def SetFocus(self):
        self.focused = True


class _Window(NoiseModelSectionMixin):
    def __init__(self, started=True, running=False):
        self.said, self.watched, self.refreshed = [], [], []
        self.main_window = SimpleNamespace(
            i18n=SimpleNamespace(t=lambda key: key),
            speak_output=SimpleNamespace(output=self.said.append),
            start_transcription_background_job=lambda make_job, status: started,
            watch_transcription_background=self.watched.append,
            transcription_background_running=lambda: running)

    def _refresh_components(self):
        self.refreshed.append("components")

    def _refresh_noise_model_section(self):
        self.refreshed.append("model")


def test_a_download_refused_while_another_runs_says_so():
    window, button, field = _Window(started=False), _Button(), _Field()
    assert window._start_in_background(lambda *a: None, "status", button, field) is False
    assert window.said == ["noise_reducer_background_busy"]
    assert button.enabled and not field.focused and window.watched == []


def test_a_started_download_is_said_once_and_followed_to_its_end():
    window, button, field = _Window(), _Button(), _Field()
    assert window._start_in_background(lambda *a: None, "status", button, field) is True
    assert window.said == []                      # the main window says it
    assert not button.enabled and field.focused
    listener = window.watched[0]
    assert listener(None, None, None, False) is False and window.refreshed == []
    assert listener(None, None, None, True) is False
    assert window.refreshed == ["components", "model"]


def test_removing_waits_for_a_background_download():
    assert _Window(running=True)._background_busy() is True
    assert _Window(running=False)._background_busy() is False
