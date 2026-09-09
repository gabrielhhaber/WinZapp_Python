"""Tests for is_windows_spellcheck_enabled() (core/spell_checker.py).

Reads Windows' own "Highlight misspelled words" setting (Settings > Time &
language > Typing > Spelling) so ConversationsPanel._spell_check_enabled()
can follow it — see tests/test_spell_check_setting.py for that side. Not a
documented Microsoft API: the value lives at HKCU\\Software\\Microsoft\\
Input\\Settings\\EnableSpellchecking, an implementation detail a Windows
Update has been reported to delete outright. Every failure mode here must
return None (unknown), never guess True or False, so the caller's fallback
to the stored WinZapp preference is the one deciding what happens next.

winreg is Windows-only, so it is monkeypatched with a small fake rather than
exercised against the real registry — deterministic regardless of which
machine (or OS) runs the suite, and covers the "not on Windows at all"
branch, which a real registry never can.
"""

import pytest

import core.spell_checker as spell_checker
from core.spell_checker import is_windows_spellcheck_enabled


class _FakeKey:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeWinreg:
    """Mirrors just the winreg surface is_windows_spellcheck_enabled() uses."""

    HKEY_CURRENT_USER = object()
    REG_DWORD = 4
    REG_SZ = 1

    def __init__(self, value=None, value_type=None, raise_on_open=None):
        self._value = value
        self._value_type = value_type if value_type is not None else self.REG_DWORD
        self._raise_on_open = raise_on_open

    def OpenKey(self, hkey, subkey):
        if self._raise_on_open is not None:
            raise self._raise_on_open
        assert hkey is self.HKEY_CURRENT_USER
        assert subkey == r"Software\Microsoft\Input\Settings"
        return _FakeKey()

    def QueryValueEx(self, key, name):
        assert name == "EnableSpellchecking"
        return (self._value, self._value_type)


class TestReadsAKnownValue:
    def test_dword_one_is_enabled(self, monkeypatch):
        monkeypatch.setattr(spell_checker, "winreg", _FakeWinreg(value=1))
        assert is_windows_spellcheck_enabled() is True

    def test_dword_zero_is_disabled(self, monkeypatch):
        monkeypatch.setattr(spell_checker, "winreg", _FakeWinreg(value=0))
        assert is_windows_spellcheck_enabled() is False

    def test_any_nonzero_dword_counts_as_enabled(self, monkeypatch):
        """The registry stores an int, not strictly a 0/1 boolean."""
        monkeypatch.setattr(spell_checker, "winreg", _FakeWinreg(value=2))
        assert is_windows_spellcheck_enabled() is True


class TestEveryFailureModeReturnsNoneRatherThanGuessing:
    """None must mean "unknown" all the way through — a caller that ever
    saw True/False here on a failure would silently force the setting one
    way regardless of what the user (or WinZapp's own preference) wants."""

    def test_key_does_not_exist(self, monkeypatch):
        monkeypatch.setattr(
            spell_checker, "winreg",
            _FakeWinreg(raise_on_open=FileNotFoundError()),
        )
        assert is_windows_spellcheck_enabled() is None

    def test_a_permission_error_reading_the_key(self, monkeypatch):
        monkeypatch.setattr(
            spell_checker, "winreg",
            _FakeWinreg(raise_on_open=PermissionError()),
        )
        assert is_windows_spellcheck_enabled() is None

    def test_value_of_the_wrong_type_is_not_trusted(self, monkeypatch):
        """A future Windows version storing this differently must not be
        silently misread as a boolean."""
        monkeypatch.setattr(
            spell_checker, "winreg",
            _FakeWinreg(value="1", value_type=_FakeWinreg.REG_SZ),
        )
        assert is_windows_spellcheck_enabled() is None

    def test_off_windows_winreg_is_unavailable(self, monkeypatch):
        monkeypatch.setattr(spell_checker, "winreg", None)
        assert is_windows_spellcheck_enabled() is None


class _CountingWinreg(_FakeWinreg):
    """_FakeWinreg that records how often the registry was actually read."""

    def __init__(self, value=None, **kw):
        super().__init__(value=value, **kw)
        self.reads = 0

    def QueryValueEx(self, key, name):
        self.reads += 1
        return super().QueryValueEx(key, name)


class TestTheReadingIsMemoised:
    """windows_spellcheck_enabled() is called from
    ConversationsPanel._spell_check_enabled(), which runs on every keystroke —
    an OpenKey/QueryValueEx pair per typed character is pure waste."""

    @pytest.fixture(autouse=True)
    def _clean_cache(self):
        spell_checker.forget_windows_spellcheck_cache()
        yield
        spell_checker.forget_windows_spellcheck_cache()

    def test_repeated_calls_read_the_registry_once(self, monkeypatch):
        fake = _CountingWinreg(value=1)
        monkeypatch.setattr(spell_checker, "winreg", fake)
        for _ in range(50):
            assert spell_checker.windows_spellcheck_enabled() is True
        assert fake.reads == 1

    def test_an_unknown_reading_is_cached_too(self, monkeypatch):
        """A machine where the value does not exist at all (a Windows Update
        has been reported to delete it) must not pay for a doomed lookup on
        every keystroke either."""
        fake = _CountingWinreg(raise_on_open=FileNotFoundError())
        monkeypatch.setattr(spell_checker, "winreg", fake)
        for _ in range(50):
            assert spell_checker.windows_spellcheck_enabled() is None
        assert fake.reads == 0  # never got past OpenKey

    def test_the_cache_expires_so_a_change_in_windows_is_picked_up(self, monkeypatch):
        """No restart: changing the setting in Windows has to take effect
        while the user watches."""
        fake = _CountingWinreg(value=1)
        monkeypatch.setattr(spell_checker, "winreg", fake)
        assert spell_checker.windows_spellcheck_enabled() is True

        fake._value = 0
        # max_age=0 stands in for "the TTL has elapsed" without sleeping.
        assert spell_checker.windows_spellcheck_enabled(max_age=0) is False
        assert fake.reads == 2

    def test_forgetting_the_cache_forces_a_fresh_read(self, monkeypatch):
        fake = _CountingWinreg(value=1)
        monkeypatch.setattr(spell_checker, "winreg", fake)
        assert spell_checker.windows_spellcheck_enabled() is True
        spell_checker.forget_windows_spellcheck_cache()
        assert spell_checker.windows_spellcheck_enabled() is True
        assert fake.reads == 2
