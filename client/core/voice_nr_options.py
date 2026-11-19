"""Voice-note noise-reduction engine and processing-moment preferences.

These keys live in the existing "general" settings cluster, next to
noise_reduction_enabled, and apply only while that flag is on:

- voice_nr_engine: "neural" (the deep-filter model, default) or "basic"
  (the noise gate, core.dsp.dsp_noisereduce.apply_noise_gate).
- voice_nr_moment: "send" (offline pass when the recording is sent, the
  published behavior) or "record" (incremental pass while recording, so the
  note leaves already processed). The basic engine ignores the moment: it
  always runs as a gate when the recording is sent.
- voice_nr_on_failure: "basic" (default: a note the neural engine could not
  process is sent with the basic gate, or unprocessed when even that fails)
  or "keep_pending" (when BOTH neural processings — incremental and at-send
  — fail, the note is kept pending with its original audio so the user can
  try sending it again by hand; never retried automatically).

Defaults reproduce the published behavior exactly (neural, at send time),
so current installs change nothing until the user chooses otherwise.
"""

NR_ENGINES = ("neural", "basic")
NR_MOMENTS = ("send", "record")
NR_ON_FAILURE = ("basic", "keep_pending")


def normalize_nr_engine(value):
    return value if isinstance(value, str) and value in NR_ENGINES else "neural"


def voice_nr_engine(settings):
    general = settings.get("general", {}) if isinstance(settings, dict) else {}
    if not isinstance(general, dict):
        return "neural"
    # The current upstream preference is authoritative, including existing installs.
    if "noise_reduction_neural" in general:
        return "neural" if general["noise_reduction_neural"] is not False else "basic"
    return normalize_nr_engine(general.get("voice_nr_engine"))


def normalize_nr_moment(value):
    return value if isinstance(value, str) and value in NR_MOMENTS else "send"


def voice_nr_moment(settings):
    general = settings.get("general", {}) if isinstance(settings, dict) else {}
    if not isinstance(general, dict):
        return "send"
    if not recording_nr_available(general.get("noise_reduction_device", "cpu")):
        return "send"
    return normalize_nr_moment(general.get("voice_nr_moment"))


def normalize_nr_on_failure(value):
    return value if isinstance(value, str) and value in NR_ON_FAILURE else "basic"


def voice_nr_on_failure(settings):
    general = settings.get("general", {}) if isinstance(settings, dict) else {}
    if not isinstance(general, dict):
        return "basic"
    return normalize_nr_on_failure(general.get("voice_nr_on_failure"))


def recording_nr_available(device):
    """The streaming worker runs in-process, never in the CUDA subprocess."""
    return device in (None, "cpu", "coreml")
