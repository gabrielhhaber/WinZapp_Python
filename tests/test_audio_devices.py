"""Tests for the Settings > Audio Devices tab's underlying device-selection
and failure-fallback logic.

Devices are stored in settings by friendly name (not raw index — BASS/
PortAudio indices aren't stable across reboots or device (dis)connections),
so device-name matching (`_match_device`) is the piece most worth covering in
isolation. `SoundSystem.apply_output_device`/`handle_playback_failure` are
exercised against a stub BASS `Output` object standing in for
sound_lib.output.Output, since a real one needs an actual audio device.
"""

import builtins
import sys
import types
import ctypes

import core.audio_devices as audio_devices_module
import core.sound_system as sound_system_module
from core.audio_devices import (
    _match_device,
    enumerate_input_devices,
    enumerate_output_devices,
    fallback_input_device_indices,
    test_input_device as _test_input_device,
)
from core.sound_system import SoundSystem


class TestEnumerateOutputDevices:
    """BASS always lists a device literally named "Default" (with
    BASS_DEVICE_DEFAULT set) ahead of the real hardware entries — confirmed
    against a real machine's device list. Left unfiltered, that showed up in
    the Settings > Audio Devices output combo as a second, redundant "use
    the default" choice alongside the combo's own sentinel first entry."""

    def test_skips_the_bass_default_pseudo_device(self, monkeypatch):
        import sound_lib.external.pybass as pybass

        # (enabled, name) per 1-based BASS device index; a falsy entry (or a
        # missing one) ends the enumeration, like the real BASS_GetDeviceInfo
        # returning 0 past the last device.
        fake_devices = {
            1: (True, b"Default"),
            2: (True, b"Fone de ouvido do headset (CORSAIR HS80)"),
            3: (False, b"Some Disabled Device"),
        }

        def fake_get_device_info(count, info_ref):
            entry = fake_devices.get(count)
            if entry is None:
                return 0
            enabled, name = entry
            info = info_ref._obj
            info.flags = pybass.BASS_DEVICE_ENABLED if enabled else 0
            info.name = name
            return 1

        monkeypatch.setattr(pybass, "BASS_GetDeviceInfo", fake_get_device_info)

        devices = enumerate_output_devices()

        assert devices == [(2, "Fone de ouvido do headset CORSAIR HS80")]


class TestMatchDevice:
    def test_finds_matching_name(self):
        devices = [(1, "Speakers"), (2, "Headphones")]
        assert _match_device("Headphones", devices) == 2

    def test_no_match_returns_none(self):
        assert _match_device("USB Mic", [(1, "Speakers")]) is None

    def test_empty_name_is_system_default_sentinel(self):
        # "" means "use the Windows default" — never something to look up.
        assert _match_device("", [(1, "Speakers")]) is None
        assert _match_device(None, [(1, "Speakers")]) is None


class _StubOutput:
    """Stands in for sound_lib.output.Output: records set_device() calls and
    raises for indices in `fail_indices`, like a device that fails to open.
    Also exposes _device/free()/init_device() — SoundSystem._switch_to_default_device()
    uses those directly instead of set_device(-1), which real BASS rejects
    outright (see the docstring on that method)."""

    def __init__(self, fail_indices=None):
        self.fail_indices = fail_indices or set()
        self.current = -1
        self._device = -1
        self.set_device_calls = []
        self.free_calls = 0

    def set_device(self, device):
        self.set_device_calls.append(device)
        if device in self.fail_indices:
            raise Exception("simulated BASS device error")
        self.current = device
        self._device = device

    def free(self):
        self.free_calls += 1

    def init_device(self, device=None):
        self._device = device
        self.current = device


class _StubMainWindow:
    def __init__(self):
        # background_mode=True keeps _warn_device_failure from touching wx
        # (no wx.App is running in these tests).
        self.background_mode = True
        self.app_name = "WinZapp"
        self.load_sounds_calls = 0

    def load_sounds(self):
        self.load_sounds_calls += 1


def _make_sound_system(monkeypatch, name_to_index, fail_indices=None):
    ss = SoundSystem(_StubMainWindow(), sound_dir="C:\\nonexistent")
    ss.output = _StubOutput(fail_indices=fail_indices)
    monkeypatch.setattr(
        sound_system_module, "find_output_device_index", lambda name: name_to_index.get(name)
    )
    return ss


class TestApplyOutputDevice:
    def test_empty_name_means_default(self, monkeypatch):
        ss = _make_sound_system(monkeypatch, {})
        assert ss.apply_output_device("") is True
        assert ss.output.current == -1

    def test_known_device_switches_to_it(self, monkeypatch):
        ss = _make_sound_system(monkeypatch, {"Speakers": 1})
        assert ss.apply_output_device("Speakers") is True
        assert ss.output.current == 1
        assert ss._configured_output_device == "Speakers"

    def test_unknown_device_falls_back_to_default(self, monkeypatch):
        ss = _make_sound_system(monkeypatch, {})
        assert ss.apply_output_device("Ghost Device") is False
        assert ss.output.current == -1

    def test_device_that_fails_to_open_falls_back_to_default(self, monkeypatch):
        ss = _make_sound_system(monkeypatch, {"Broken": 2}, fail_indices={2})
        assert ss.apply_output_device("Broken") is False
        assert ss.output.current == -1

    def test_switching_resets_the_warned_flag(self, monkeypatch):
        # A fresh device choice (e.g. the user picked a different one in
        # Settings) should get its own chance to warn on a later failure,
        # not inherit "already warned" from whatever was configured before.
        ss = _make_sound_system(monkeypatch, {"Speakers": 1})
        ss._warned_output_failure = True
        ss.apply_output_device("Speakers")
        assert ss._warned_output_failure is False

    def test_switching_back_to_default_uses_free_and_init_not_set_device(self, monkeypatch):
        # Regression: sound_lib.output.Output.set_device(-1) always raised
        # "illegal device number" on real BASS — it calls BASS_SetDevice(-1)
        # internally, and -1 is only ever valid as a BASS_Init() argument.
        # That exception used to be silently swallowed, so "switch back to
        # default" was a no-op lying about success: the previously-selected
        # device stayed active. Confirmed live against a real BASS device
        # before writing this fix.
        ss = _make_sound_system(monkeypatch, {"Speakers": 1})
        ss.apply_output_device("Speakers")
        assert ss.output.current == 1

        ss.apply_output_device("")

        assert ss.output.current == -1
        assert ss.output.free_calls == 1
        # -1 must never reach set_device() — only real device indices do.
        assert -1 not in ss.output.set_device_calls


class TestHandlePlaybackFailure:
    def test_a_default_device_user_is_recovered_too(self, monkeypatch):
        """This used to return False for anyone on "system default", on the
        reasoning that there is nothing to fall back *to*.

        That is precisely the reported failure. The default device changed
        under BASS — a wireless dongle unplugged, a USB device taking over as
        default — BASS stayed bound to a device that no longer exists, and
        nothing played: not voice messages, not UI effects. The only thing
        that repaired it was switching the output device away and back, which
        is the one path that reached the free/init this performs.

        The recovery those users need is exactly the same reinit; only the
        *warning* about a named device that could not be opened depends on
        there being a name."""
        ss = _make_sound_system(monkeypatch, {})
        assert ss.handle_playback_failure() is True
        assert ss.output.free_calls == 1

    def test_the_default_case_says_nothing_to_the_user(self, monkeypatch):
        """Falling back from a default that moved is not something to
        apologise for — there is no device name that failed to open."""
        ss = _make_sound_system(monkeypatch, {})
        warned = []
        ss._warn_device_failure = lambda kind, name: warned.append(name)
        ss.handle_playback_failure()
        assert warned == []

    def test_a_named_device_that_failed_is_still_reported(self, monkeypatch):
        ss = _make_sound_system(monkeypatch, {"Speakers": 3})
        ss.apply_output_device("Speakers")
        warned = []
        ss._warn_device_failure = lambda kind, name: warned.append(name)
        ss.handle_playback_failure()
        assert warned == ["Speakers"]

    def test_first_failure_falls_back_and_reports_true(self, monkeypatch):
        ss = _make_sound_system(monkeypatch, {"Speakers": 1})
        ss.apply_output_device("Speakers")
        assert ss.handle_playback_failure() is True
        assert ss.output.current == -1

    def test_reloads_cached_sound_events_after_falling_back(self, monkeypatch):
        # BASS_Free()/BASS_Init() during the fallback invalidates every
        # cached Settings > Sound Events stream (startup_sound, error_sound,
        # ...) that existed before it — reloading them here is what lets
        # future calls to those cached objects recover on their own, instead
        # of every one of their many call sites needing its own retry logic.
        ss = _make_sound_system(monkeypatch, {"Speakers": 1})
        ss.apply_output_device("Speakers")
        ss.handle_playback_failure()
        assert ss.main_window.load_sounds_calls == 1

    def test_repeated_failures_only_warn_once_per_session(self, monkeypatch):
        ss = _make_sound_system(monkeypatch, {"Speakers": 1})
        ss.apply_output_device("Speakers")
        assert ss.handle_playback_failure() is True
        # Same broken device failing again and again shouldn't keep
        # popping message boxes for the rest of the session.
        assert ss.handle_playback_failure() is False
        assert ss.handle_playback_failure() is False


class _FakePyAudioStream:
    def close(self):
        pass


class _FakePyAudio:
    """Stands in for pyaudio.PyAudio: accepts open() only for the exact
    (rate, channels) pairs in `accepted_configs`, like a real device that
    only supports its own native sample rate."""

    def __init__(self, accepted_configs):
        self.accepted_configs = accepted_configs
        self.opened_with = []

    def open(self, rate, channels, format, input, input_device_index, frames_per_buffer, start):
        self.opened_with.append((rate, channels))
        if (rate, channels) not in self.accepted_configs:
            raise OSError(-9997, "Invalid sample rate")
        return _FakePyAudioStream()

    def terminate(self):
        pass


class TestTestInputDevice:
    """Regression coverage for a real bug: hardcoding a single (44100, 1)
    test config rejected every device on a machine where every WASAPI
    device's native rate was 48000 — "Invalid sample rate" for every one of
    them, regardless of the device actually being fine. The test now walks
    the same fallback chain _start_voice_recording() itself uses."""

    def test_succeeds_on_the_first_supported_combo(self, monkeypatch):
        fake_pa = _FakePyAudio(accepted_configs={(48000, 1)})
        monkeypatch.setattr(audio_devices_module.pyaudio, "PyAudio", lambda: fake_pa)
        assert audio_devices_module.test_input_device(5) is True
        assert fake_pa.opened_with == [(48000, 1)]

    def test_falls_back_to_a_later_combo(self, monkeypatch):
        # Only the very last combo in the fallback chain works.
        fake_pa = _FakePyAudio(accepted_configs={(44100, 2)})
        monkeypatch.setattr(audio_devices_module.pyaudio, "PyAudio", lambda: fake_pa)
        assert audio_devices_module.test_input_device(5) is True
        assert fake_pa.opened_with == audio_devices_module.RECORDING_SAMPLE_CONFIGS

    def test_fails_only_when_no_combo_is_supported(self, monkeypatch):
        fake_pa = _FakePyAudio(accepted_configs=set())
        monkeypatch.setattr(audio_devices_module.pyaudio, "PyAudio", lambda: fake_pa)
        assert audio_devices_module.test_input_device(5) is False
        assert fake_pa.opened_with == audio_devices_module.RECORDING_SAMPLE_CONFIGS


class TestApplyEffectsDevice:
    """The effects-output device pins effect sounds to a concrete device so they
    don't follow the voice output when it's switched. Empty name = the resolved
    system-default index (NOT None), a named device = its own index."""

    def _make(self, monkeypatch, name_to_index, default_idx=1, init_ok=True):
        ss = _make_sound_system(monkeypatch, name_to_index)
        monkeypatch.setattr(ss, "_ensure_device_inited", lambda idx: init_ok)
        monkeypatch.setattr(
            sound_system_module, "find_default_output_device_index", lambda: default_idx
        )
        return ss

    def test_empty_name_pins_to_resolved_default_index(self, monkeypatch):
        ss = self._make(monkeypatch, {}, default_idx=1)
        assert ss.apply_effects_device("") is True
        assert ss._effects_device == 1
        assert ss._configured_effects_device == ""

    def test_empty_name_falls_back_to_none_if_default_unresolvable(self, monkeypatch):
        ss = self._make(monkeypatch, {}, default_idx=None)
        assert ss.apply_effects_device("") is False
        assert ss._effects_device is None

    def test_known_device_is_routed(self, monkeypatch):
        ss = self._make(monkeypatch, {"Speakers": 3})
        assert ss.apply_effects_device("Speakers") is True
        assert ss._effects_device == 3
        assert ss._configured_effects_device == "Speakers"

    def test_unknown_device_falls_back(self, monkeypatch):
        ss = self._make(monkeypatch, {})
        assert ss.apply_effects_device("Ghost") is False
        assert ss._effects_device is None

    def test_device_that_fails_to_init_falls_back(self, monkeypatch):
        ss = self._make(monkeypatch, {"Broken": 4}, init_ok=False)
        assert ss.apply_effects_device("Broken") is False
        assert ss._effects_device is None

    def test_switching_from_named_back_to_default_repins_not_clears(self, monkeypatch):
        ss = self._make(monkeypatch, {"Speakers": 3}, default_idx=1)
        ss.apply_effects_device("Speakers")
        assert ss._effects_device == 3
        assert ss.apply_effects_device("") is True
        assert ss._effects_device == 1  # pinned to default, NOT None


class TestPyAudioUnavailable:
    """No wheel exists for PyAudio on Python 3.14 at the time of writing
    (see requirements.txt's version marker), so audio_devices.py imports
    pyaudio inside a try/except and leaves the module-level name None
    rather than failing outright. These paths must degrade gracefully
    instead of raising AttributeError on `pyaudio.PyAudio`."""

    def test_enumerate_input_devices_falls_back_to_sounddevice(self, monkeypatch):
        """No longer "returns []": with no PyAudio the function now queries
        sounddevice instead, because reporting zero input devices on Python
        3.14 (where PyAudio has no wheel) would leave the user unable to pick
        a microphone at all. It must return what sounddevice reports."""
        monkeypatch.setattr(audio_devices_module, "pyaudio", None)

        fake_sd = types.SimpleNamespace(query_devices=lambda: [
            {"name": "Mic A", "max_input_channels": 2},
            {"name": "Speakers", "max_input_channels": 0},   # output-only
            {"name": "Mic B", "max_input_channels": 1},
        ])
        monkeypatch.setitem(sys.modules, "sounddevice", fake_sd)

        assert enumerate_input_devices() == [(0, "Mic A"), (2, "Mic B")]

    def test_enumerate_input_devices_returns_empty_when_nothing_is_available(self, monkeypatch):
        """Both backends missing is the only case that still yields [] — and it
        must not raise. This is what the old assertion was really protecting."""
        monkeypatch.setattr(audio_devices_module, "pyaudio", None)

        real_import = builtins.__import__

        def _no_sounddevice(name, *a, **kw):
            if name == "sounddevice":
                raise ImportError("no sounddevice either")
            return real_import(name, *a, **kw)

        monkeypatch.delitem(sys.modules, "sounddevice", raising=False)
        monkeypatch.setattr(builtins, "__import__", _no_sounddevice)

        assert enumerate_input_devices() == []

    def test_test_input_device_returns_false(self, monkeypatch):
        monkeypatch.setattr(audio_devices_module, "pyaudio", None)
        assert _test_input_device(5) is False


class _BrokenPyAudio:
    """Stands in for pyaudio.PyAudio on a machine whose audio stack is down
    (Windows Audio service stopped, no endpoint at all): PortAudio resolves
    no host API, so both the WASAPI query and the default-host-API fallback
    raise instead of returning info."""

    def __init__(self, default_raises=True, host_api=None):
        self.default_raises = default_raises
        self.host_api = host_api
        self.terminated = False

    def get_host_api_info_by_type(self, _type):
        raise OSError(-9998, "Invalid host API")

    def get_default_host_api_info(self):
        if self.default_raises:
            raise OSError(-9998, "Invalid host API")
        return self.host_api

    def get_device_info_by_host_api_device_index(self, _api_index, index):
        return {"index": index, "name": f"Mic {index}", "maxInputChannels": 1}

    def terminate(self):
        self.terminated = True


class TestBrokenAudioStack:
    """enumerate_input_devices() documents "never raises", but two calls were
    left unguarded and broke that contract on a machine with no working audio
    stack: the get_default_host_api_info() fallback inside the WASAPI handler
    (an error handler with an unhandled error of its own), and the
    pyaudio.PyAudio() construction.

    All four call sites are hurt by an escaping exception — the Settings >
    Audio Devices combo and apply_audio_devices() run it on the wx UI thread,
    and ui/conversations.py runs it inside a daemon thread where the traceback
    dies unseen and used to leave the record button permanently dead (see
    tests/test_recording_open_failure.py).
    """

    def test_returns_empty_when_no_host_api_resolves(self, monkeypatch):
        fake_pa = _BrokenPyAudio()
        monkeypatch.setattr(audio_devices_module.pyaudio, "PyAudio", lambda: fake_pa)
        assert enumerate_input_devices() == []
        # The temporary instance must still be cleaned up on the failure path.
        assert fake_pa.terminated is True

    def test_returns_empty_when_pyaudio_cannot_be_constructed(self, monkeypatch):
        def _boom():
            raise OSError(-9996, "Device unavailable")

        monkeypatch.setattr(audio_devices_module.pyaudio, "PyAudio", _boom)
        assert enumerate_input_devices() == []

    def test_falls_back_to_the_default_host_api_when_wasapi_is_absent(self, monkeypatch):
        """The fallback is guarded now, but it must still *work* — WASAPI is
        Windows-only, and the default host API is the whole point of it."""
        fake_pa = _BrokenPyAudio(
            default_raises=False,
            host_api={"index": 0, "deviceCount": 2},
        )
        monkeypatch.setattr(audio_devices_module.pyaudio, "PyAudio", lambda: fake_pa)
        assert enumerate_input_devices() == [(0, "Mic 0"), (1, "Mic 1")]

    def test_returns_empty_on_host_api_info_without_an_index(self, monkeypatch):
        fake_pa = _BrokenPyAudio(default_raises=False, host_api={"deviceCount": 3})
        monkeypatch.setattr(audio_devices_module.pyaudio, "PyAudio", lambda: fake_pa)
        assert enumerate_input_devices() == []

    def test_a_single_malformed_device_does_not_drop_the_others(self, monkeypatch):
        """One bad device info dict is one device to skip, not a reason to
        report none — the index/name lookup used to sit outside the per-device
        try, so a single malformed entry took the whole list with it."""
        class _OneBadDevice(_BrokenPyAudio):
            def get_device_info_by_host_api_device_index(self, _api_index, index):
                if index == 1:
                    return {"maxInputChannels": 1}      # no index, no name
                return {"index": index, "name": f"Mic {index}", "maxInputChannels": 1}

        fake_pa = _OneBadDevice(default_raises=False, host_api={"index": 0, "deviceCount": 3})
        monkeypatch.setattr(audio_devices_module.pyaudio, "PyAudio", lambda: fake_pa)
        assert enumerate_input_devices() == [(0, "Mic 0"), (2, "Mic 2")]

    def test_find_input_device_index_degrades_to_the_system_default(self, monkeypatch):
        """What the callers actually consume: None means "use whatever
        PyAudio's own default is", which is how recording keeps working at
        all on a machine where enumeration fails."""
        fake_pa = _BrokenPyAudio()
        monkeypatch.setattr(audio_devices_module.pyaudio, "PyAudio", lambda: fake_pa)
        assert audio_devices_module.find_input_device_index("Microfone USB") is None


class TestFallbackInputDeviceIndices:
    """The candidates a recording path may still try once its preferred
    attempts have failed.

    `input_device_index=None` is not "any device that works": PortAudio maps
    it to the default device of its default host API (MME on Windows), while
    enumerate_input_devices() reads WASAPI. They are different handles, and
    host APIs fail independently — so the default refusing every rate/channel
    combo does not mean nothing on the machine can record, which is exactly
    the conclusion both recording paths used to draw before giving up.
    """

    def test_offers_every_enumerated_index(self, monkeypatch):
        monkeypatch.setattr(audio_devices_module, "enumerate_input_devices",
                            lambda pa=None: [(12, "Headset"), (18, "Webcam")])
        assert fallback_input_device_indices() == [12, 18]

    def test_skips_indices_already_tried(self, monkeypatch):
        """The pinned device was tried first and failed; retrying it would
        just cost another round of driver negotiation for a known answer."""
        monkeypatch.setattr(audio_devices_module, "enumerate_input_devices",
                            lambda pa=None: [(12, "Headset"), (18, "Webcam")])
        assert fallback_input_device_indices(exclude=(12,)) == [18]

    def test_none_in_exclude_is_ignored(self, monkeypatch):
        """None is the system-default sentinel, not a device index — dropping
        entries equal to it would silently remove device 0 on any machine
        where PortAudio numbers from zero."""
        monkeypatch.setattr(audio_devices_module, "enumerate_input_devices",
                            lambda pa=None: [(0, "Onboard"), (12, "Headset")])
        assert fallback_input_device_indices(exclude=(None,)) == [0, 12]

    def test_no_devices_means_nothing_left_to_try(self, monkeypatch):
        monkeypatch.setattr(audio_devices_module, "enumerate_input_devices",
                            lambda pa=None: [])
        assert fallback_input_device_indices() == []

    def test_a_broken_audio_stack_yields_no_candidates(self, monkeypatch):
        """Not a separate guard, just the consequence of going through
        enumerate_input_devices(): its "never raises" contract carries over,
        which matters because both callers run in a daemon thread where an
        escaping exception dies unseen."""
        monkeypatch.setattr(audio_devices_module.pyaudio, "PyAudio", lambda: _BrokenPyAudio())
        assert fallback_input_device_indices() == []


class TestReinitialisingTheOutputDevice:
    """The two lines behind both reported audio bugs.

    `Output.free()` is BASS_Free(), which frees only the *current* device, and
    `init_device(-1)` then BASS_Init's the system default — which
    apply_effects_device() has usually already initialised, since it pins
    effects to the concrete default index. BASS answers 14, "already
    initialized", and the exception escaped apply_output_device() (only the
    set_device() branch was ever wrapped) all the way to the global handler,
    twice captured on a live install:

        File "core/sound_system.py", line 203, in _switch_to_default_device
        File "sound_lib/output.py", line 65, in init_device
        sound_lib.main.BassError: 14, already initialized/paused/whatever

    The old device was freed and no new one initialised, so every later play()
    raised "invalid handle" or "BASS_Start has not been successfully called" —
    the endless errors while arrowing through voice messages.
    """

    def test_already_initialised_is_success_not_failure(self, monkeypatch):
        ss = _make_sound_system(monkeypatch, {})

        def _boom(device=None):
            raise Exception("14, already initialized/paused/whatever")

        ss.output.init_device = _boom
        assert ss.apply_output_device("") is True

    def test_a_real_init_failure_is_reported_not_raised(self, monkeypatch):
        ss = _make_sound_system(monkeypatch, {})

        def _boom(device=None):
            raise Exception("23, illegal device number")

        ss.output.init_device = _boom
        assert ss.apply_output_device("") is False

    def test_the_switch_never_raises_out_of_apply(self, monkeypatch):
        """It escaped through settings_dialog._validate/_apply_values/_on_ok
        and killed the dialog, leaving BASS freed and nothing initialised."""
        ss = _make_sound_system(monkeypatch, {})

        def _boom(*_a, **_kw):
            raise Exception("14, already initialized/paused/whatever")

        ss.output.free = _boom
        ss.output.init_device = _boom
        ss.apply_output_device("")          # must not raise

    def test_error_fourteen_is_recognised_by_text_and_by_code(self):
        from core.sound_system import SoundSystem
        assert SoundSystem._is_already_initialised(
            Exception("14, already initialized/paused/whatever")) is True
        assert SoundSystem._is_already_initialised(
            Exception("already initialized")) is True
        assert SoundSystem._is_already_initialised(
            Exception("5, invalid handle")) is False


class TestTheRecoveryCooldown:
    """A reinit invalidates every existing stream, so each one produces a
    fresh crop of failures from whatever was mid-play. Without a floor between
    attempts those failures drive the next reinit."""

    def test_a_second_failure_straight_away_is_not_another_reinit(self, monkeypatch):
        ss = _make_sound_system(monkeypatch, {})
        assert ss.handle_playback_failure() is True
        assert ss.handle_playback_failure() is False
        assert ss.output.free_calls == 1

    def test_the_cooldown_expiring_allows_another(self, monkeypatch):
        ss = _make_sound_system(monkeypatch, {})
        ss.handle_playback_failure()
        ss._last_recovery_at -= ss._RECOVERY_COOLDOWN_SECONDS
        assert ss.handle_playback_failure() is True
        assert ss.output.free_calls == 2

    def test_the_user_changing_the_setting_is_never_held_off(self, monkeypatch):
        """A deliberate change is not a recovery; the user is entitled to be
        listened to immediately."""
        ss = _make_sound_system(monkeypatch, {})
        ss.handle_playback_failure()
        ss.apply_output_device("")
        assert ss._recovery_cooldown_elapsed() is True


class TestAHealthyDeviceIsLeftAlone:
    """The restraint the removed `if self.output._device == -1: return`
    sentinel was really providing, and whose loss shipped a regression.

    MainWindow.__init__ calls apply_output_device() at line 1694, load_sounds()
    at 1702, and _apply_configured_audio_devices() calls apply_output_device()
    a second time at 1741. For a user on "system default" that second call used
    to be a no-op. Unguarded it frees and re-initialises BASS, invalidating
    every Sound stream load_sounds() had just built — so the startup sound and
    every effect afterwards raised "5, invalid handle". Reported by a user on a
    fresh install within hours of the alpha:

        [sound] Error playing startup sound: 5, invalid handle

    The sentinel could not see the case it was removed for: a device unplugged
    while BASS stays bound to its cached index. Hence a health check rather
    than a sentinel — already on the requested device AND that device still
    exists.
    """

    def _healthy(self, ss, healthy):
        ss._output_device_is_healthy = lambda device: healthy

    def test_a_second_apply_does_not_tear_bass_down(self, monkeypatch):
        ss = _make_sound_system(monkeypatch, {})
        self._healthy(ss, True)
        ss.apply_output_device("")
        ss.apply_output_device("")
        assert ss.output.free_calls == 0

    def test_an_unhealthy_device_is_still_reinitialised(self, monkeypatch):
        ss = _make_sound_system(monkeypatch, {})
        self._healthy(ss, False)
        ss.apply_output_device("")
        assert ss.output.free_calls == 1

    def test_the_recovery_path_reinitialises_even_when_bass_looks_fine(self, monkeypatch):
        """handle_playback_failure() is reached *because* a stream just failed
        to play. BASS answering "all good" is exactly the case that needs
        forcing — otherwise the recovery is a no-op and nothing ever plays."""
        ss = _make_sound_system(monkeypatch, {})
        self._healthy(ss, True)
        assert ss.handle_playback_failure() is True
        assert ss.output.free_calls == 1


class TestStoppingAStaleChannelIsNotFatal:
    """Sound.play()'s first act is a BASS_ChannelStop on a handle that may have
    been freed underneath it. Outside the try it escaped play() entirely, so
    the device recovery never ran and the error reached the global handler —
    which plays a sound of its own and raised again from inside sys.excepthook,
    producing two tracebacks per QR refresh, forever."""

    def test_play_survives_a_stop_that_raises(self, monkeypatch):
        import core.sound_system as mod

        base = mod.Sound.__mro__[1]          # what super() in Sound.play() reaches
        played = []
        monkeypatch.setattr(
            base, "stop",
            lambda self: (_ for _ in ()).throw(Exception("5, invalid handle")),
            raising=False)
        monkeypatch.setattr(
            base, "play",
            lambda self, restart=False: played.append(restart), raising=False)

        snd = mod.Sound.__new__(mod.Sound)
        snd.sound_system = _make_sound_system(monkeypatch, {})
        snd.sound_system._effects_device = None
        snd.event_key = None
        snd.pack_id = None
        snd.file = "x.ogg"

        mod.Sound.play(snd)                  # must not raise
        assert played == [True]

    def test_the_stop_is_inside_the_guarded_region(self):
        """Structural, because the ordering is the whole bug: a stop() above
        the try cannot be recovered by the except below it."""
        import inspect
        src = inspect.getsource(__import__("core.sound_system", fromlist=["Sound"]).Sound.play)
        assert src.index("try:") < src.index("super().stop()")
