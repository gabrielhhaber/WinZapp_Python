"""Windows regional date/time format detection.

WinZapp used to hardcode a date/time format per UI language (see
"time_fmt"/"date_fmt"/"datetime_fmt" in client/languages/*.json) — e.g.
en-US always got 12-hour "06:15 PM" and M/d/yyyy dates, regardless of what
the user actually configured in Windows' Region settings (Settings > Time &
Language > Language & region > Regional format). This reads the real
per-user format strings from Windows via GetLocaleInfoEx and translates them
to Python strftime patterns, falling back to the language-file default
whenever Windows detection isn't available (non-Windows, API failure, or a
pattern this translator doesn't recognize).
"""

import ctypes
import functools

# LOCALE_NAME_USER_DEFAULT: GetLocaleInfoEx's first argument is a locale
# name; passing NULL (None in ctypes) means "the current user's default
# locale" — exactly the "Regional format" the user picked in Windows.
LOCALE_SSHORTDATE  = 0x0000001F
# LOCALE_SSHORTTIME ("short time", e.g. "HH:mm" / "h:mm tt" — no seconds) is
# what Windows' own clock and the Region settings' "Time formats > Short
# time" field show, matching the "06:15 PM" style example from the feature
# request. LOCALE_STIMEFORMAT ("long time") usually adds seconds and is the
# wrong one to use here. LOCALE_SSHORTTIME needs Windows 10 1903+; fall back
# to LOCALE_STIMEFORMAT on anything older that doesn't recognize it.
LOCALE_SSHORTTIME  = 0x00000079
LOCALE_STIMEFORMAT = 0x00001003
# MUI_LANGUAGE_NAME: makes GetUserPreferredUILanguages() return BCP-47 tags
# ("en-US") instead of LANGIDs — for get_system_ui_language() below.
MUI_LANGUAGE_NAME = 0x8

# Longest-match-first per family, so e.g. "yyyy" is consumed before "yy".
_DATE_TOKENS = [
    ("yyyy", "%Y"), ("yy", "%y"),
    ("MMMM", "%B"), ("MMM", "%b"), ("MM", "%m"), ("M", "%m"),
    ("dddd", "%A"), ("ddd", "%a"), ("dd", "%d"), ("d", "%d"),
]
_TIME_TOKENS = [
    ("HH", "%H"), ("H", "%H"),
    ("hh", "%I"), ("h", "%I"),
    ("mm", "%M"), ("m", "%M"),
    ("ss", "%S"), ("s", "%S"),
    ("tt", "%p"), ("t", "%p"),
]


def _get_windows_locale_pattern(lctype: int) -> str | None:
    """Return the raw Windows format pattern (e.g. "dd/MM/yyyy") for
    *lctype*, or None if unavailable (not on Windows, or the call failed)."""
    try:
        kernel32 = ctypes.windll.kernel32
    except (AttributeError, OSError):
        return None  # not running on Windows
    try:
        size = kernel32.GetLocaleInfoEx(None, lctype, None, 0)
        if size <= 0:
            return None
        buf = ctypes.create_unicode_buffer(size)
        if kernel32.GetLocaleInfoEx(None, lctype, buf, size) <= 0:
            return None
        return buf.value or None
    except Exception:
        return None


def _translate_pattern(pattern: str, tokens: list) -> str:
    """Translate a Windows format pattern into a Python strftime pattern.

    Windows format strings can quote literal text in single quotes (e.g.
    "h 'h' mm"); that's rare enough in real locale data that a literal
    passthrough (quotes included) is an acceptable simplification here —
    worst case a quote character shows up verbatim, never a crash.
    """
    result = []
    i, n = 0, len(pattern)
    while i < n:
        for tok, repl in tokens:
            if pattern.startswith(tok, i):
                result.append(repl)
                i += len(tok)
                break
        else:
            result.append(pattern[i])
            i += 1
    return "".join(result)


@functools.lru_cache(maxsize=1)
def _windows_date_strftime() -> str | None:
    try:
        pattern = _get_windows_locale_pattern(LOCALE_SSHORTDATE)
        if not pattern:
            return None
        return _translate_pattern(pattern, _DATE_TOKENS)
    except Exception:
        return None


@functools.lru_cache(maxsize=1)
def _windows_time_strftime() -> str | None:
    try:
        pattern = (
            _get_windows_locale_pattern(LOCALE_SSHORTTIME)
            or _get_windows_locale_pattern(LOCALE_STIMEFORMAT)
        )
        if not pattern:
            return None
        return _translate_pattern(pattern, _TIME_TOKENS)
    except Exception:
        return None


def _get_windows_geo_name() -> str | None:
    """Raw two-letter code from GetUserDefaultGeoName (Windows 10 1709+),
    or None off-Windows, on an older Windows build, or on API failure."""
    try:
        kernel32 = ctypes.windll.kernel32
    except (AttributeError, OSError):
        return None
    try:
        size = kernel32.GetUserDefaultGeoName(None, 0)
        if size <= 0:
            return None
        buf = ctypes.create_unicode_buffer(size)
        if kernel32.GetUserDefaultGeoName(buf, size) <= 0:
            return None
        return buf.value or None
    except Exception:
        return None


@functools.lru_cache(maxsize=1)
def get_country_or_region_iso2() -> str | None:
    """ISO 3166-1 alpha-2 code (e.g. "US", "BA") of the user's Windows
    "Country or region" setting — Settings > Time & Language > Language &
    region > Country or region, the field Windows describes as "Windows and
    apps might use your country or region to give you local content". None
    off-Windows or on API failure.

    Deliberately NOT the "Regional format" locale that get_date_format() /
    get_time_format() below use (GetUserDefaultLocaleName, reached via
    GetLocaleInfoEx(NULL, ...)): the two Windows settings are independent.
    A machine can have an English "Regional format" (it defaults to
    whatever the UI language was at install time and most people never
    change it) while "Country or region" is set to the user's actual
    location. Reading Regional format here — an earlier version of this
    function did — silently ignored a correctly-configured Country or
    region and kept resolving to the Regional format's country instead;
    reported live as the pairing dialog's country selector defaulting to
    the United States on a machine whose Country or region was set to
    Bosnia and Herzegovina. GetUserDefaultGeoName reads the Country or
    region field directly, with no dependency on any language setting."""
    try:
        code = _get_windows_geo_name()
        return code.upper() if code else None
    except Exception:
        return None


def _get_windows_ui_language_raw() -> str | None:
    """Raw top-priority tag from GetUserPreferredUILanguages, or None
    off-Windows or on API failure."""
    try:
        kernel32 = ctypes.windll.kernel32
    except (AttributeError, OSError):
        return None
    try:
        num_languages = ctypes.c_ulong(0)
        buf_len = ctypes.c_ulong(0)
        if not kernel32.GetUserPreferredUILanguages(
            MUI_LANGUAGE_NAME, ctypes.byref(num_languages), None, ctypes.byref(buf_len)
        ) or buf_len.value == 0:
            return None
        buf = ctypes.create_unicode_buffer(buf_len.value)
        if not kernel32.GetUserPreferredUILanguages(
            MUI_LANGUAGE_NAME, ctypes.byref(num_languages), buf, ctypes.byref(buf_len)
        ) or num_languages.value == 0:
            return None
        # buf is a MULTI_SZ (a list of NUL-terminated strings, double-NUL
        # terminated); wstring_at() with no length reads up to the first
        # embedded NUL, i.e. just the top-priority language.
        return ctypes.wstring_at(buf) or None
    except Exception:
        return None


@functools.lru_cache(maxsize=1)
def get_system_ui_language() -> str | None:
    """BCP-47 tag (e.g. "en-US") of the user's top-priority Windows display
    language (GetUserPreferredUILanguages), or None off-Windows or on API
    failure.

    Also independent of get_country_or_region_iso2() and of "Regional
    format" above — this is the actual "Windows display language" setting.
    Used only to pick a sensible default in the first-run language picker
    (ui/dialogs/language_dialog.py), which has no saved language to read
    from I18n yet."""
    try:
        return _get_windows_ui_language_raw()
    except Exception:
        return None


def get_date_format(fallback: str) -> str:
    """strftime pattern for a short date, from Windows' Regional format
    settings if available, else *fallback* (the app's per-language default)."""
    return _windows_date_strftime() or fallback


def get_time_format(fallback: str) -> str:
    """strftime pattern for a time-of-day, from Windows' Regional format
    settings if available, else *fallback*."""
    return _windows_time_strftime() or fallback


def get_datetime_format(fallback: str) -> str:
    """strftime pattern combining Windows' short date + time formats, else
    *fallback* when either half couldn't be detected."""
    date_fmt = _windows_date_strftime()
    time_fmt = _windows_time_strftime()
    if not date_fmt or not time_fmt:
        return fallback
    return f"{date_fmt} {time_fmt}"
