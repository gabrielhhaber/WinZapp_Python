"""Enumeration and selection helpers for the Settings > Audio Devices tab.

Output (playback) device switching goes through sound_lib/BASS — a single
process-wide `Output` device, shared by every stream anywhere in the app
(program sounds, conversation audio, status audio). Input (recording) device
selection only affects PyAudio, used solely for voice-message recording in
`ui/conversations.py`.

Devices are stored in settings by friendly name, not raw index — BASS/PortAudio
device indices are not stable across reboots or device (dis)connections, so a
name is resolved back to the current index each time it's needed.
"""

import ctypes
import logging

try:
    import pyaudio
except ImportError:
    # No wheel exists for PyAudio on Python 3.14 at the time of writing (it
    # bundles a C extension nobody has published a matching build for yet —
    # see pyproject.toml's version marker), so uv skips it
    # entirely there rather than failing outright. Recording device
    # selection just degrades to "no input devices available" below instead
    # of crashing the whole Settings dialog / app startup over it.
    pyaudio = None


def _match_device(name: str, devices: list):
    """Return the index of the device named `name` in `devices`
    ([(index, friendly_name), ...]), or None if absent/empty."""
    if not name:
        return None
    for idx, dev_name in devices:
        if dev_name == name:
            return idx
    return None


def enumerate_output_devices() -> list:
    """[(bass_device_index, friendly_name), ...] for enabled BASS output
    devices (excludes BASS's special device 0, "No sound", and its "Default"
    pseudo-device — BASS always lists a device literally named "Default"
    ahead of the real hardware entries, which would otherwise show up as a
    second, redundant "use the default" choice alongside the combo's own
    sentinel first entry).

    Written directly against BASS_GetDeviceInfo rather than reusing
    sound_lib.output.Output.get_device_names()/find_device_by_name(): those
    assume the returned (gap-free) name list's position + 1 equals the real
    BASS device index, which only holds if no disabled device was skipped
    during enumeration — not something we can rely on.
    """
    from sound_lib.external.pybass import BASS_DEVICEINFO, BASS_GetDeviceInfo, BASS_DEVICE_ENABLED

    devices = []
    info = BASS_DEVICEINFO()
    count = 1
    while BASS_GetDeviceInfo(count, ctypes.byref(info)):
        if info.flags & BASS_DEVICE_ENABLED:
            name = info.name
            if isinstance(name, bytes):
                # Modern BASS returns device names as UTF-8; decoding as mbcs
                # (the Windows ANSI codepage) mangled Polish characters into
                # mojibake in the device combos. Try UTF-8 first, fall back to
                # mbcs only for a legacy BASS that really is ANSI.
                try:
                    name = name.decode("utf-8")
                except UnicodeDecodeError:
                    name = name.decode("mbcs", "replace")
            name = name.replace("(", "").replace(")", "").strip()
            if name.lower() != "default":
                devices.append((count, name))
        count += 1
    return devices


def _pyaudio_input_devices(pa: "pyaudio.PyAudio") -> list:
    """[(device_index, friendly_name), ...] for input-capable devices,
    preferring the WASAPI host API: its device names/order match Windows'
    own Sound control panel (mmsys.cpl), whereas MME truncates names to 31
    characters and DirectSound duplicates the whole device list."""
    try:
        host_api = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
    except Exception:
        # This fallback used to be unguarded — an error handler with an
        # unhandled error of its own. PortAudio can fail to resolve *any*
        # host API (Windows Audio service stopped, no sound hardware at
        # all), and that exception then escaped the entire enumeration
        # instead of degrading to "no input devices", which is what every
        # caller is written to expect. See enumerate_input_devices().
        try:
            host_api = pa.get_default_host_api_info()
        except Exception:
            logging.warning(
                "[audio] No usable PortAudio host API — reporting no input devices.",
                exc_info=True,
            )
            return []

    # host_api is a plain dict from PortAudio; a malformed/empty one must not
    # take the caller down either. "index" was read inside the loop before,
    # so a missing key silently produced an empty list N times over — same
    # outcome, but nothing said why.
    try:
        host_api_index = host_api["index"]
        device_count = host_api.get("deviceCount", 0)
    except Exception:
        logging.warning("[audio] Unusable host API info: %r.", host_api)
        return []

    devices = []
    for i in range(device_count):
        try:
            info = pa.get_device_info_by_host_api_device_index(host_api_index, i)
            if info.get("maxInputChannels", 0) > 0:
                # Inside the try: a device whose info dict is missing
                # index/name is one device to skip, not a reason to drop
                # every device found so far.
                devices.append((info["index"], str(info["name"]).strip()))
        except Exception:
            continue
    return devices


def enumerate_input_devices(pa: "pyaudio.PyAudio | None" = None) -> list:
    """[(device_index, friendly_name), ...] for input-capable devices. Opens
    a temporary PyAudio instance if `pa` isn't supplied.

    Never raises: an empty list means "no input device to offer", whether
    because PyAudio isn't installed (see the import at the top of this file),
    because PortAudio itself can't start, or because no host API resolved.
    That contract was documented here long before the code actually honoured
    it — two calls were left unguarded (the get_default_host_api_info()
    fallback in _pyaudio_input_devices(), and the pyaudio.PyAudio()
    construction just below), so a broken audio stack propagated an exception
    into all four call sites instead. Callers rely on the empty case: it
    resolves to input_device_index=None, i.e. the system default device.
    """
    if pyaudio is None:
        # WinZapp fork: PyAudio has no wheel on Python 3.14, so degrade to a
        # sounddevice query instead of reporting no input devices at all.
        try:
            import sounddevice as sd
            devs = []
            for idx, info in enumerate(sd.query_devices()):
                if info.get("max_input_channels", 0) > 0:
                    devs.append((idx, str(info.get("name", "")).strip()))
            return devs
        except Exception:
            return []
    owns_pa = pa is None
    if owns_pa:
        try:
            pa = pyaudio.PyAudio()
        except Exception:
            # Constructing PyAudio initialises PortAudio, which fails outright
            # when the Windows Audio service is stopped or no endpoint exists.
            # Every caller that passes no `pa` of its own lands here: the
            # Settings > Audio Devices combo (ui/dialogs/settings_dialog.py)
            # and apply_audio_devices() during startup (main.py).
            logging.warning(
                "[audio] Failed to initialise PyAudio for device enumeration.",
                exc_info=True,
            )
            return []
    try:
        return _pyaudio_input_devices(pa)
    except Exception:
        # Belt and braces: _pyaudio_input_devices() handles its own known
        # failure points, but this function's contract is "never raise", and
        # its callers run on the wx UI thread (Settings dialog) or inside a
        # daemon thread where an escaping exception dies unseen.
        logging.warning("[audio] Failed to enumerate input devices.", exc_info=True)
        return []
    finally:
        if owns_pa:
            try:
                pa.terminate()
            except Exception:
                pass


def find_output_device_index(name: str):
    """Resolve a stored output device name to its current BASS device index,
    or None if empty/not currently present."""
    if not name:
        return None
    return _match_device(name, enumerate_output_devices())


def find_default_output_device_index():
    """Return the CONCRETE BASS device index flagged as the system default
    (BASS_DEVICE_DEFAULT), or None if it can't be determined. Effects routing
    needs a real index — not the -1/"current device" sentinel — so effect
    sounds stay pinned to the default device even after the voice output is
    switched to a different one (switching Output re-inits that one device and
    would otherwise drag effects along with it)."""
    from sound_lib.external.pybass import (
        BASS_DEVICEINFO, BASS_GetDeviceInfo, BASS_DEVICE_ENABLED, BASS_DEVICE_DEFAULT,
    )
    info = BASS_DEVICEINFO()
    count = 1
    while BASS_GetDeviceInfo(count, ctypes.byref(info)):
        if (info.flags & BASS_DEVICE_ENABLED) and (info.flags & BASS_DEVICE_DEFAULT):
            return count
        count += 1
    return None


def find_input_device_index(name: str, pa: "pyaudio.PyAudio | None" = None):
    """Resolve a stored input device name to its current PyAudio device
    index, or None if empty/not currently present."""
    if not name:
        return None
    return _match_device(name, enumerate_input_devices(pa))


def fallback_input_device_indices(pa: "pyaudio.PyAudio | None" = None, exclude=()) -> list:
    """Input device indices still worth trying after the preferred open
    attempts — the pinned device, then `input_device_index=None` — have all
    failed.

    `input_device_index=None` does not mean "whichever device works": PortAudio
    resolves it to the default device of its *default host API*, which is MME
    on Windows. enumerate_input_devices() deliberately reads the WASAPI host
    API instead, so the indices it returns are a different set of handles,
    potentially onto the very same microphone reached by a different path.
    Host APIs fail independently — the same headset answered -9999
    "Unanticipated host error" through MME, DirectSound and WDM-KS but -9996
    "Invalid device" through WASAPI, three drivers disagreeing about one piece
    of hardware. "The default device refused every rate/channel combo" is
    therefore not evidence that nothing on this machine can record, which is
    the conclusion both recording paths used to draw.

    Costs nothing while recording works: callers only get here after every
    earlier attempt returned None — the path that previously just gave up.

    Never raises: enumerate_input_devices() already guarantees that, and both
    callers run inside a daemon thread where an escaping exception dies unseen.
    `exclude` drops indices already tried; None entries in it are ignored,
    being the system-default sentinel rather than an index.
    """
    skip = {idx for idx in exclude if idx is not None}
    return [idx for idx, _name in enumerate_input_devices(pa) if idx not in skip]


# Sample-rate/channel combinations to try opening an input device with, in
# preference order — must mirror ui/conversations.py's _start_voice_recording()
# fallback chain exactly. A single hardcoded (rate, channels) pair doesn't
# work as a general "can this device open" test: most WASAPI devices only
# accept their own native rate (48000 on every device tested), rejecting
# 44100 outright with "Invalid sample rate" — every non-default recording
# device used to fail Settings validation for exactly this reason, the
# device itself was never actually at fault.
RECORDING_SAMPLE_CONFIGS = [(48000, 1), (48000, 2), (44100, 1), (44100, 2)]


def recording_configs_for(device_index, pa=None) -> list:
    """RECORDING_SAMPLE_CONFIGS with `device_index`'s own native rate first.

    The fixed list above was written for WASAPI devices, whose native rate is
    48000 on everything tested, and it leaves out the one family that never
    offers either rate: a Bluetooth headset used as a *microphone*. Recording
    takes it out of A2DP and into the Hands-Free Profile, whose SCO link is
    mono at 8000 Hz (CVSD) or 16000 Hz (mSBC) — so every combination above is
    refused and the device looks broken.

    Reported from a real install on 2026-09-09, four times across two sessions:

        [audio_devices] Input device test failed for every sample-rate/channel
        combo (index=14)

    (index 19 in the later session — a Bluetooth device's index is not stable,
    which is why devices are stored by name.) Note the failure was never
    confined to Settings validation: the constant above says it must mirror
    _start_voice_recording()'s chain and does, so recording with that headset
    could not have worked either. The dialog was telling the truth about a
    limit that is WinZapp's, not the device's.

    Asking the device rather than widening the list is what makes this general:
    PortAudio already reports `defaultSampleRate`, which is the rate Windows
    has the endpoint opened at, so this covers 8000 and 16000 without naming
    them and covers whatever comes next for free. The fixed list stays as the
    tail, because `defaultSampleRate` is a *default*, not the only rate a
    device accepts, and a driver that reports one thing and accepts another
    was the situation this whole chain exists for.

    Never raises, and never returns an empty list: a device whose info cannot
    be read falls back to exactly the previous behaviour.
    """
    if pyaudio is None:
        return list(RECORDING_SAMPLE_CONFIGS)
    owns_pa = pa is None
    try:
        if owns_pa:
            pa = pyaudio.PyAudio()
        info = pa.get_device_info_by_index(int(device_index))
        native = int(round(float(info.get("defaultSampleRate") or 0)))
        max_channels = int(info.get("maxInputChannels") or 0)
    except Exception:
        logging.info(
            "[audio_devices] Could not read the native rate of input device %s "
            "— falling back to the fixed combinations.", device_index,
        )
        return list(RECORDING_SAMPLE_CONFIGS)
    finally:
        if owns_pa and pa is not None:
            try:
                pa.terminate()
            except Exception:
                pass

    configs = []
    if native > 0:
        # Mono first for the same reason the fixed list does it: WhatsApp voice
        # messages are mono, and a stereo capture costs a downmix loop in pure
        # Python. A device reporting a single input channel never gets asked
        # for two — an HFP microphone is exactly that.
        for channels in (1, 2):
            if max_channels and channels > max_channels:
                continue
            configs.append((native, channels))
    for combo in RECORDING_SAMPLE_CONFIGS:
        if combo not in configs:
            configs.append(combo)
    return configs


def test_input_device(device_index: int) -> bool:
    """Try to briefly open (without starting) an input stream on
    `device_index`, across every sample-rate/channel combo recording itself
    would try. Returns True if any combo was accepted — or False outright
    if PyAudio isn't installed at all (see the import at the top of this
    file)."""
    if pyaudio is None:
        return False
    pa = pyaudio.PyAudio()
    try:
        for rate, channels in recording_configs_for(device_index, pa):
            try:
                stream = pa.open(
                    rate=rate,
                    channels=channels,
                    format=pyaudio.paInt16,
                    input=True,
                    input_device_index=device_index,
                    frames_per_buffer=1024,
                    start=False,
                )
                stream.close()
                return True
            except Exception:
                continue
        logging.warning(
            "[audio_devices] Input device test failed for every sample-rate/channel combo (index=%s)",
            device_index,
        )
        return False
    finally:
        try:
            pa.terminate()
        except Exception:
            pass
