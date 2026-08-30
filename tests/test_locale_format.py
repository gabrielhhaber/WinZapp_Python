"""Tests for core.locale_format — GitHub issue #14.

WinZapp used to hardcode date/time display formats per UI language
("time_fmt"/"date_fmt"/"datetime_fmt" in client/languages/*.json), ignoring
whatever the user actually configured in Windows' Region settings. This
reads the real per-user pattern via GetLocaleInfoEx and translates the
Windows format tokens (yyyy/MM/dd, HH/mm/tt, ...) to Python strftime codes,
falling back to the language-file default whenever detection fails.

_translate_pattern() and the fallback behavior of get_*_format() are pure
functions — exercised directly. The actual ctypes/GetLocaleInfoEx call is
monkeypatched so these tests aren't dependent on this machine's Windows
Region settings.
"""

import pytest

from core import locale_format as lf


@pytest.fixture(autouse=True)
def _clear_cache():
    """The get_*_strftime() / get_country_or_region_iso2() /
    get_system_ui_language() helpers are all lru_cache'd (these Windows
    settings don't change mid-session) — clear between tests so
    monkeypatching their underlying raw getters actually takes effect each
    time."""
    lf._windows_date_strftime.cache_clear()
    lf._windows_time_strftime.cache_clear()
    lf.get_country_or_region_iso2.cache_clear()
    lf.get_system_ui_language.cache_clear()
    yield
    lf._windows_date_strftime.cache_clear()
    lf._windows_time_strftime.cache_clear()
    lf.get_country_or_region_iso2.cache_clear()
    lf.get_system_ui_language.cache_clear()


class TestTranslatePattern:
    def test_date_tokens(self):
        assert lf._translate_pattern("dd/MM/yyyy", lf._DATE_TOKENS) == "%d/%m/%Y"
        assert lf._translate_pattern("M/d/yyyy", lf._DATE_TOKENS) == "%m/%d/%Y"
        assert lf._translate_pattern("yyyy-MM-dd", lf._DATE_TOKENS) == "%Y-%m-%d"

    def test_longest_token_wins_yyyy_before_yy(self):
        assert lf._translate_pattern("yyyy", lf._DATE_TOKENS) == "%Y"
        assert lf._translate_pattern("yy", lf._DATE_TOKENS) == "%y"

    def test_month_name_variants(self):
        assert lf._translate_pattern("dd MMMM yyyy", lf._DATE_TOKENS) == "%d %B %Y"
        assert lf._translate_pattern("dd MMM yyyy", lf._DATE_TOKENS) == "%d %b %Y"

    def test_time_tokens_24h(self):
        assert lf._translate_pattern("HH:mm", lf._TIME_TOKENS) == "%H:%M"
        assert lf._translate_pattern("HH:mm:ss", lf._TIME_TOKENS) == "%H:%M:%S"

    def test_time_tokens_12h_with_am_pm(self):
        assert lf._translate_pattern("h:mm tt", lf._TIME_TOKENS) == "%I:%M %p"
        assert lf._translate_pattern("hh:mm:ss tt", lf._TIME_TOKENS) == "%I:%M:%S %p"

    def test_unrecognized_characters_pass_through_literally(self):
        assert lf._translate_pattern("yyyy年MM月dd日", lf._DATE_TOKENS) == "%Y年%m月%d日"


class TestGetFormatsUseWindowsWhenAvailable:
    def test_get_date_format_translates_the_detected_pattern(self, monkeypatch):
        monkeypatch.setattr(lf, "_get_windows_locale_pattern", lambda lctype: "M/d/yyyy")
        assert lf.get_date_format("fallback") == "%m/%d/%Y"

    def test_get_time_format_prefers_short_time_over_long_time(self, monkeypatch):
        def _fake(lctype):
            return "HH:mm" if lctype == lf.LOCALE_SSHORTTIME else "HH:mm:ss"
        monkeypatch.setattr(lf, "_get_windows_locale_pattern", _fake)
        assert lf.get_time_format("fallback") == "%H:%M"

    def test_get_time_format_falls_back_to_long_time_on_older_windows(self, monkeypatch):
        def _fake(lctype):
            return None if lctype == lf.LOCALE_SSHORTTIME else "h:mm:ss tt"
        monkeypatch.setattr(lf, "_get_windows_locale_pattern", _fake)
        assert lf.get_time_format("fallback") == "%I:%M:%S %p"

    def test_get_datetime_format_combines_date_and_time(self, monkeypatch):
        def _fake(lctype):
            if lctype == lf.LOCALE_SSHORTDATE:
                return "dd/MM/yyyy"
            return "HH:mm"
        monkeypatch.setattr(lf, "_get_windows_locale_pattern", _fake)
        assert lf.get_datetime_format("fallback") == "%d/%m/%Y %H:%M"


class TestFallback:
    def test_get_date_format_falls_back_when_windows_detection_fails(self, monkeypatch):
        monkeypatch.setattr(lf, "_get_windows_locale_pattern", lambda lctype: None)
        assert lf.get_date_format("%d/%m/%Y") == "%d/%m/%Y"

    def test_get_time_format_falls_back_when_windows_detection_fails(self, monkeypatch):
        monkeypatch.setattr(lf, "_get_windows_locale_pattern", lambda lctype: None)
        assert lf.get_time_format("%H:%M") == "%H:%M"

    def test_get_datetime_format_falls_back_if_either_half_is_missing(self, monkeypatch):
        def _fake(lctype):
            return "dd/MM/yyyy" if lctype == lf.LOCALE_SSHORTDATE else None
        monkeypatch.setattr(lf, "_get_windows_locale_pattern", _fake)
        assert lf.get_datetime_format("fallback") == "fallback"

    def test_a_raising_pattern_source_never_propagates(self, monkeypatch):
        def _raise(lctype):
            raise OSError("no locale info")
        monkeypatch.setattr(lf, "_get_windows_locale_pattern", _raise)
        assert lf.get_date_format("fallback") == "fallback"
        assert lf.get_time_format("fallback") == "fallback"
        assert lf.get_datetime_format("fallback") == "fallback"


class TestGetWindowsLocalePatternItself:
    def test_missing_kernel32_returns_none(self, monkeypatch):
        class _NoWindll:
            def __getattr__(self, name):
                raise AttributeError(name)
        monkeypatch.setattr(lf.ctypes, "windll", _NoWindll(), raising=False)
        assert lf._get_windows_locale_pattern(lf.LOCALE_SSHORTDATE) is None


class TestGetCountryOrRegionIso2:
    """Regression: this used to read the "Regional format" locale
    (GetLocaleInfoEx(NULL, LOCALE_SISO3166CTRYNAME)) instead of the actual
    "Country or region" setting (GetUserDefaultGeoName) — the two are
    independent Windows settings, and a machine can have an English
    Regional format (it defaults to the install-time UI language) while
    Country or region is set to the user's real location. Reported live:
    Country or region = Bosnia and Herzegovina, but the pairing dialog's
    country selector still defaulted to the United States."""

    def test_uppercases_the_detected_code(self, monkeypatch):
        monkeypatch.setattr(lf, "_get_windows_geo_name", lambda: "ba")
        assert lf.get_country_or_region_iso2() == "BA"

    def test_falls_back_to_none_when_detection_fails(self, monkeypatch):
        monkeypatch.setattr(lf, "_get_windows_geo_name", lambda: None)
        assert lf.get_country_or_region_iso2() is None

    def test_missing_kernel32_returns_none(self, monkeypatch):
        class _NoWindll:
            def __getattr__(self, name):
                raise AttributeError(name)
        monkeypatch.setattr(lf.ctypes, "windll", _NoWindll(), raising=False)
        assert lf._get_windows_geo_name() is None

    def test_a_raising_getter_never_propagates(self, monkeypatch):
        def _raise():
            raise OSError("no geo name")
        monkeypatch.setattr(lf, "_get_windows_geo_name", _raise)
        assert lf.get_country_or_region_iso2() is None


class TestGetSystemUiLanguage:
    def test_returns_the_detected_tag(self, monkeypatch):
        monkeypatch.setattr(lf, "_get_windows_ui_language_raw", lambda: "en-US")
        assert lf.get_system_ui_language() == "en-US"

    def test_falls_back_to_none_when_detection_fails(self, monkeypatch):
        monkeypatch.setattr(lf, "_get_windows_ui_language_raw", lambda: None)
        assert lf.get_system_ui_language() is None

    def test_missing_kernel32_returns_none(self, monkeypatch):
        class _NoWindll:
            def __getattr__(self, name):
                raise AttributeError(name)
        monkeypatch.setattr(lf.ctypes, "windll", _NoWindll(), raising=False)
        assert lf._get_windows_ui_language_raw() is None

    def test_a_raising_getter_never_propagates(self, monkeypatch):
        def _raise():
            raise OSError("no UI languages")
        monkeypatch.setattr(lf, "_get_windows_ui_language_raw", _raise)
        assert lf.get_system_ui_language() is None
