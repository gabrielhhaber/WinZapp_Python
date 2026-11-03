"""Voice-note channel preference (#677/#679), independent of capture devices.

Capture still asks for both inputs first: opening a two-input interface as
mono can discard INPUT 2 in the driver. Explicit mono averages the captured
channels, with headroom, before preview, noise reduction and encoding.
Automatic sends mono only when one side carries nothing at all
(core.voice_stereo.drop_empty_side, #679); stereo preserves channels when available.
"""

RECORDING_MODES = ("auto", "mono", "stereo")


def normalize_recording_mode(value):
    return value if isinstance(value, str) and value in RECORDING_MODES else "auto"


def voice_recording_mode(settings):
    general = settings.get("general", {}) if isinstance(settings, dict) else {}
    if not isinstance(general, dict):
        return "auto"
    # The removed voice_message_stereo boolean is deliberately not revived:
    # current installs keep their device-based behavior until they choose.
    return normalize_recording_mode(general.get("voice_recording_mode"))


def recording_output_channels(captured_channels, mode):
    # "auto" and "stereo" both keep what the device captured; only the label differs
    # (stereo is never forced on a mono-only device).
    return 1 if normalize_recording_mode(mode) == "mono" else captured_channels


def prepare_voice_pcm(pcm, captured_channels, mode):
    """Return little-endian int16 PCM and output channels; never alter *pcm*.

    floor((L + R) / 2) gives both inputs equal weight without clipping. It does not
    normalize volume, detect a silent channel or correct phase cancellation.
    Mono capture and modes other than explicit mono pass through byte-for-byte.
    """
    if normalize_recording_mode(mode) != "mono" or captured_channels == 1:
        return pcm, captured_channels
    if captured_channels != 2 or len(pcm) % 4:
        raise ValueError("Expected complete stereo int16 PCM frames")
    # numpy is already required by voice processing. Widen before adding to
    # avoid int16 overflow; vectorize so a long paused preview has no Python
    # per-sample loop on the UI thread.
    import numpy as np
    samples = np.frombuffer(pcm, dtype="<i2").reshape(-1, 2)
    mixed = (np.add(samples[:, 0], samples[:, 1], dtype=np.int32) // 2)
    return mixed.astype("<i2").tobytes(), 1
