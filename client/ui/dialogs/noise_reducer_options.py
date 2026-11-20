"""The noise reduction choices inside the noise reducer's own window:
engine (neural or basic), when to process a voice message (while recording or
when sending) and what to do when neural reduction fails.

They live here, next to the processor and the model they depend on, so the
Audio tab keeps fewer controls (each needs its own Alt letter) and the
conditional enabling reads the processor and the model of this very window.
The window returns the choices as :attr:`options`; the settings page saves
them with the processor (``NoiseReducerButtonMixin._save_noise_reducer_device``).
"""

from core.voice_nr_options import (
    NR_ENGINES, NR_MOMENTS, NR_ON_FAILURE, recording_nr_available,
    voice_nr_engine, voice_nr_moment, voice_nr_on_failure)


def nr_options_from_settings(settings):
    return {"engine": voice_nr_engine(settings),
            "moment": voice_nr_moment(settings),
            "on_failure": voice_nr_on_failure(settings)}


def combo_value(values, index, default):
    return values[index] if 0 <= index < len(values) else default


def options_state(engine, device, model_installed):
    """(moment_enabled, on_failure_enabled) for the window's combos.

    Both mean something only for the neural engine with its model there; the
    moment also needs a processor that can run while recording (never the
    CUDA process).
    """
    neural = engine == "neural" and bool(model_installed)
    return neural and recording_nr_available(device), neural


class NoiseReducerOptionsMixin:
    """The behaviour of the window's three combos. They are CREATED by
    NoiseReducerDialog._build_voice_nr_options, in the window's own class, so
    tests/settings_controls.py sees the labels as the window's controls."""

    @property
    def options(self) -> dict:
        """The choices as they stand now (a moment the processor cannot run
        while recording is kept as "send")."""
        engine = combo_value(NR_ENGINES, self._voice_nr_engine_combo.GetSelection(), "neural")
        moment = combo_value(NR_MOMENTS, self._voice_nr_moment_combo.GetSelection(), "send")
        if not recording_nr_available(self.device):
            moment = "send"
        on_failure = combo_value(
            NR_ON_FAILURE, self._voice_nr_on_failure_combo.GetSelection(), "basic")
        return {"engine": engine, "moment": moment, "on_failure": on_failure}

    def _update_voice_nr_options_state(self):
        if getattr(self, "_voice_nr_on_failure_combo", None) is None:
            return  # the model section refreshes while the window is being built
        from core import noise_model
        try:
            installed = noise_model.is_installed()
        except Exception:
            installed = False
        engine = combo_value(NR_ENGINES, self._voice_nr_engine_combo.GetSelection(), "neural")
        moment_on, failure_on = options_state(engine, self.device, installed)
        if not recording_nr_available(self.device):
            self._voice_nr_moment_combo.SetSelection(NR_MOMENTS.index("send"))
        self._voice_nr_moment_combo.Enable(moment_on)
        self._voice_nr_on_failure_combo.Enable(failure_on)

    def _on_voice_nr_engine_changed(self, event):
        self._update_voice_nr_options_state()
        event.Skip()
