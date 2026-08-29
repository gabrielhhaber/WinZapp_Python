"""Tests for AccessibleSpeechOutput (client/core/accessible_speech.py) — the
wrapper around accessible_output2's Auto output that gates every call on the
two Settings > Acessibilidade toggles:

- extended_sr_compat_enabled: master switch, nothing is spoken when off.
- sapi_fallback_enabled: when off, only ever speak through a real screen
  reader, never the system SAPI voice Auto() would otherwise fall back to.

Fakes stand in for accessible_output2's Auto and its individual outputs —
no real screen reader or SAPI COM object is touched.
"""

from core.accessible_speech import AccessibleSpeechOutput


class _FakeOutput:
    def __init__(self, name, active=True, system_output=False):
        self.name = name
        self._active = active
        self._system_output = system_output
        self.spoken = []

    def is_active(self):
        return self._active

    def is_system_output(self):
        return self._system_output

    def speak(self, text, **options):
        self.spoken.append((text, options))

    def silence(self):
        self.silenced = getattr(self, "silenced", 0) + 1


class _FakeAuto:
    """Mimics accessible_output2.outputs.auto.Auto's public surface: a
    priority-sorted `.outputs` list and `get_first_available_output()`
    (first output in that list whose is_active() is True)."""

    def __init__(self, outputs):
        self.outputs = outputs

    def get_first_available_output(self):
        for output in self.outputs:
            if output.is_active():
                return output
        return None


def _settings(extended=True, sapi_fallback=True, present=True):
    if not present:
        return {}
    return {"accessibility": {
        "extended_sr_compat_enabled": extended,
        "sapi_fallback_enabled": sapi_fallback,
    }}


class TestExtendedCompatibilityMasterSwitch:
    def test_speaks_normally_when_enabled(self):
        nvda = _FakeOutput("nvda", active=True)
        auto = _FakeAuto([nvda])
        out = AccessibleSpeechOutput(auto, lambda: _settings(extended=True))
        out.output("hello")
        assert nvda.spoken == [("hello", {})]

    def test_never_speaks_when_disabled_even_with_a_screen_reader_active(self):
        nvda = _FakeOutput("nvda", active=True)
        auto = _FakeAuto([nvda])
        out = AccessibleSpeechOutput(auto, lambda: _settings(extended=False))
        out.output("hello")
        assert nvda.spoken == []

    def test_never_speaks_when_disabled_even_with_sapi_available(self):
        sapi = _FakeOutput("sapi5", active=True, system_output=True)
        auto = _FakeAuto([sapi])
        out = AccessibleSpeechOutput(auto, lambda: _settings(extended=False))
        out.output("hello")
        assert sapi.spoken == []


class TestSapiFallback:
    def test_falls_back_to_sapi_when_enabled_and_no_screen_reader_active(self):
        sapi = _FakeOutput("sapi5", active=True, system_output=True)
        auto = _FakeAuto([sapi])
        out = AccessibleSpeechOutput(auto, lambda: _settings(sapi_fallback=True))
        out.output("hello")
        assert sapi.spoken == [("hello", {})]

    def test_prefers_screen_reader_over_sapi_when_both_active_and_fallback_enabled(self):
        nvda = _FakeOutput("nvda", active=True)
        sapi = _FakeOutput("sapi5", active=True, system_output=True)
        auto = _FakeAuto([nvda, sapi])  # priority order matches accessible_output2's own sort
        out = AccessibleSpeechOutput(auto, lambda: _settings(sapi_fallback=True))
        out.output("hello")
        assert nvda.spoken == [("hello", {})]
        assert sapi.spoken == []

    def test_uses_the_screen_reader_when_fallback_disabled_and_it_is_active(self):
        nvda = _FakeOutput("nvda", active=True)
        sapi = _FakeOutput("sapi5", active=True, system_output=True)
        auto = _FakeAuto([nvda, sapi])
        out = AccessibleSpeechOutput(auto, lambda: _settings(sapi_fallback=False))
        out.output("hello")
        assert nvda.spoken == [("hello", {})]
        assert sapi.spoken == []

    def test_never_falls_back_to_sapi_when_disabled_and_no_screen_reader_active(self):
        """The whole point of the setting: SAPI must stay silent even though
        it reports is_active() == True, same as accessible_output2's own
        Auto would otherwise always pick it."""
        sapi = _FakeOutput("sapi5", active=True, system_output=True)
        auto = _FakeAuto([sapi])
        out = AccessibleSpeechOutput(auto, lambda: _settings(sapi_fallback=False))
        out.output("hello")
        assert sapi.spoken == []

    def test_screen_reader_turned_off_mid_session_stays_silent_on_the_next_call(self):
        """is_active() is queried live on every call, not cached at
        construction — turning off NVDA/JAWS while WinZapp is running must
        silence it immediately, not just on the next restart."""
        nvda = _FakeOutput("nvda", active=True)
        sapi = _FakeOutput("sapi5", active=True, system_output=True)
        auto = _FakeAuto([nvda, sapi])
        out = AccessibleSpeechOutput(auto, lambda: _settings(sapi_fallback=False))
        out.output("first")
        assert nvda.spoken == [("first", {})]

        nvda._active = False  # user quit NVDA
        out.output("second")
        assert nvda.spoken == [("first", {})]  # unchanged
        assert sapi.spoken == []  # never falls back


class TestBackwardCompatibleDefaults:
    def test_missing_accessibility_section_defaults_to_current_behavior(self):
        """An existing settings.json predating this feature has no
        "accessibility" key at all — both toggles must default to their
        current (enabled) behavior rather than going silent."""
        sapi = _FakeOutput("sapi5", active=True, system_output=True)
        auto = _FakeAuto([sapi])
        out = AccessibleSpeechOutput(auto, lambda: _settings(present=False))
        out.output("hello")
        assert sapi.spoken == [("hello", {})]


class TestOutputOptions:
    def test_interrupt_kwarg_is_forwarded(self):
        nvda = _FakeOutput("nvda", active=True)
        auto = _FakeAuto([nvda])
        out = AccessibleSpeechOutput(auto, lambda: _settings())
        out.output("hello", interrupt=True)
        assert nvda.spoken == [("hello", {"interrupt": True})]


class TestSilenceObeysTheSameGatesAsOutput:
    """silence() reaches into the screen reader to cancel speech already in
    flight — including speech WinZapp did not produce. It therefore has to
    respect exactly the switches that govern speaking.

    It briefly did not: a separate resolver skipped
    extended_sr_compat_enabled entirely and fell through to
    get_first_available_output(). A user who turned the master switch off
    (WinZapp must not talk to my screen reader) but still ran NVDA for the
    rest of Windows had NVDA cut off by WinZapp anyway — and with
    sapi_fallback_enabled off, SAPI reached too.
    """

    def test_the_master_switch_off_means_no_silence_call_at_all(self):
        nvda = _FakeOutput("nvda", active=True)
        auto = _FakeAuto([nvda])
        out = AccessibleSpeechOutput(auto, lambda: _settings(extended=False))

        out.silence()

        assert getattr(nvda, "silenced", 0) == 0

    def test_it_silences_normally_when_the_master_switch_is_on(self):
        nvda = _FakeOutput("nvda", active=True)
        auto = _FakeAuto([nvda])
        out = AccessibleSpeechOutput(auto, lambda: _settings())

        out.silence()

        assert nvda.silenced == 1

    def test_sapi_is_not_silenced_when_the_fallback_is_off(self):
        """Same rule output() follows: with the fallback off, only a real
        screen reader is ever touched."""
        sapi = _FakeOutput("sapi", active=True, system_output=True)
        auto = _FakeAuto([sapi])
        out = AccessibleSpeechOutput(auto, lambda: _settings(sapi_fallback=False))

        out.silence()

        assert getattr(sapi, "silenced", 0) == 0

    def test_sapi_is_silenced_when_the_fallback_is_on(self):
        sapi = _FakeOutput("sapi", active=True, system_output=True)
        auto = _FakeAuto([sapi])
        out = AccessibleSpeechOutput(auto, lambda: _settings(sapi_fallback=True))

        out.silence()

        assert sapi.silenced == 1


class TestSilenceScreenReaderFocus:
    def test_it_can_cancel_native_focus_speech_when_extended_compat_is_off(self):
        nvda = _FakeOutput("nvda", active=True)
        auto = _FakeAuto([nvda])
        out = AccessibleSpeechOutput(auto, lambda: _settings(extended=False))

        out.silence_screen_reader_focus()

        assert nvda.silenced == 1

    def test_it_never_touches_sapi(self):
        sapi = _FakeOutput("sapi", active=True, system_output=True)
        auto = _FakeAuto([sapi])
        out = AccessibleSpeechOutput(auto, lambda: _settings())

        out.silence_screen_reader_focus()

        assert getattr(sapi, "silenced", 0) == 0

    def test_it_ignores_inactive_screen_readers(self):
        nvda = _FakeOutput("nvda", active=False)
        auto = _FakeAuto([nvda])
        out = AccessibleSpeechOutput(auto, lambda: _settings())

        out.silence_screen_reader_focus()

        assert getattr(nvda, "silenced", 0) == 0
