"""Settings > Audio's noise reducer (ui/dialogs/noise_reducer_dialog.py,
core/cuda_components/runtime.py).

Reports of 2026-11-12: the noise reducer's settings button showed with the
noise reduction box unticked, and its settings needed a WinZapp restart."""
import sys
from types import SimpleNamespace

import pytest

from core.cuda_components import runtime
from ui.dialogs.noise_reducer_dialog import NoiseReducerButtonMixin


class _Ctrl:
    def __init__(self, value=False, shown=True):
        self.value, self.shown = value, shown

    def GetValue(self):
        return self.value

    def IsShown(self):
        return self.shown

    def Show(self, on=True):
        self.shown = on


def _panel(reduction_on, monkeypatch, installed=True):
    from core import noise_model
    monkeypatch.setattr(noise_model, "is_installed", lambda: installed)
    panel = SimpleNamespace(_noise_reduction_check=_Ctrl(reduction_on),
                            _noise_reducer_neural_check=_Ctrl(), _noise_reducer_btn=_Ctrl(),
                            _noise_reducer_page=SimpleNamespace(Layout=lambda: None))
    NoiseReducerButtonMixin._sync_noise_reducer_neural_box(panel)
    return panel


def test_the_settings_button_follows_the_noise_reduction_box(monkeypatch):
    off = _panel(False, monkeypatch)
    assert not off._noise_reducer_btn.shown and not off._noise_reducer_neural_check.shown
    on = _panel(True, monkeypatch)
    assert on._noise_reducer_btn.shown and on._noise_reducer_neural_check.shown
    no_model = _panel(True, monkeypatch, installed=False)
    assert no_model._noise_reducer_btn.shown and not no_model._noise_reducer_neural_check.shown


@pytest.fixture
def fresh_runtime(monkeypatch):
    monkeypatch.setattr(runtime, "_active", None)
    monkeypatch.setattr(runtime, "_chosen", None)
    dropped = []
    import core.neural_noise_reduction as nnr
    monkeypatch.setattr(nnr, "forget_denoiser", lambda: dropped.append(1))
    return dropped


def _settings(device):
    return {"general": {"noise_reduction_device": device}}


def test_the_processor_applies_at_once_even_from_a_gpu_build(fresh_runtime, monkeypatch):
    monkeypatch.setattr(runtime, "_active", "cuda12")
    assert runtime.apply_now(_settings("cpu")) is True
    assert runtime.providers(["CUDAExecutionProvider", "CPUExecutionProvider"]) == ["CPUExecutionProvider"]
    assert fresh_runtime == [1]            # the loaded model with the old provider is dropped


def test_a_gpu_build_applies_at_once_while_onnxruntime_is_not_loaded(fresh_runtime, monkeypatch):
    monkeypatch.delitem(sys.modules, "onnxruntime", raising=False)
    activated = []
    monkeypatch.setattr(runtime, "activate", lambda s: activated.append(1) or "cuda13")
    assert runtime.apply_now(_settings("cuda13")) is True and activated == [1]
    assert runtime.needs_restart("cuda13", loaded=False) is False


def test_a_gpu_build_after_onnxruntime_loaded_needs_a_restart(fresh_runtime, monkeypatch):
    monkeypatch.setitem(sys.modules, "onnxruntime", SimpleNamespace())
    assert runtime.apply_now(_settings("cuda12")) is False
    assert runtime.needs_restart("cuda12") is True
    assert runtime.needs_restart("cpu") is False


def test_the_components_field_says_restart_only_when_it_is_so():
    from core.cuda_components import text as ctext
    i18n = SimpleNamespace(t=lambda key: key, language="pt-BR")
    with_note = ctext.components_text(i18n, "cpu", [], 0, "cuda12", restart_needed=True)
    without = ctext.components_text(i18n, "cpu", [], 0, "cuda12", restart_needed=False)
    assert "noise_reducer_device_next_start" in with_note
    assert "noise_reducer_device_next_start" not in without
