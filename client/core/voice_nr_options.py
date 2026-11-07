"""Voice-note noise-reduction engine and processing-moment preferences.

These keys live in the existing "general" settings cluster, next to
noise_reduction_enabled, and apply only while that flag is on:

- voice_nr_engine: "neural" (the deep-filter model, default) or "basic"
  (the noise gate, core.dsp.dsp_noisereduce.apply_noise_gate).
- voice_nr_moment: "send" (offline pass when the recording is sent, the
  published behavior) or "record" (incremental pass while recording, so the
  note leaves already processed). The basic engine ignores the moment: it
  always runs as a gate when the recording is sent.

Defaults reproduce the published behavior exactly (neural, at send time),
so current installs change nothing until the user chooses otherwise.
"""

NR_ENGINES = ("neural", "basic")
NR_MOMENTS = ("send", "record")


def normalize_nr_engine(value):
    return value if isinstance(value, str) and value in NR_ENGINES else "neural"


def voice_nr_engine(settings):
    general = settings.get("general", {}) if isinstance(settings, dict) else {}
    if not isinstance(general, dict):
        return "neural"
    return normalize_nr_engine(general.get("voice_nr_engine"))


def normalize_nr_moment(value):
    return value if isinstance(value, str) and value in NR_MOMENTS else "send"


def voice_nr_moment(settings):
    general = settings.get("general", {}) if isinstance(settings, dict) else {}
    if not isinstance(general, dict):
        return "send"
    return normalize_nr_moment(general.get("voice_nr_moment"))
