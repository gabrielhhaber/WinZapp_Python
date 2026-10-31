"""Mac equivalents for smaller Windows-only pieces of WinZapp.

* Show in folder: Finder (`open -R`) instead of `explorer.exe /select,`.
* Regional settings: the short date/time format (System Settings, General,
  Language & Region, Date format), country or region and UI language come
  from macOS (NSLocale / NSDateFormatter) instead of the
  Windows locale APIs, which simply return nothing off Windows.
* Recording microphone + computer audio: WinZapp captures computer audio with
  Windows' WASAPI (and a separate NVDA volume). The Mac needs its own
  recorder (Core Audio process taps, macOS 14.2+); until then the button is
  shown dimmed, named "not yet available on Mac", and its shortcut says so.
"""

import functools
import logging
import os
import subprocess

import wx


# ------------------------------------------------------------ show in Finder --

def reveal_file_in_folder(filepath):
    if not filepath or not os.path.isfile(filepath):
        return False
    subprocess.Popen(["open", "-R", os.path.abspath(filepath)])
    return True


# ---------------------------------------------------------- regional settings --

def _icu_to_windows(pattern):
    """NSDateFormatter uses ICU patterns; WinZapp's translator reads
    Windows ones. They share d/M/y/h/H/m/s; ICU writes AM/PM as 'a'."""
    out, quoted = [], False
    for ch in pattern:
        if ch == "'":
            quoted = not quoted
            out.append(ch)
        elif ch == "a" and not quoted:
            out.append("tt")
        else:
            out.append(ch)
    return "".join(out).replace("tttt", "tt")


def _mac_pattern(date_style, time_style):
    from Foundation import NSDateFormatter, NSLocale
    f = NSDateFormatter.alloc().init()
    f.setLocale_(NSLocale.currentLocale())
    f.setDateStyle_(date_style)
    f.setTimeStyle_(time_style)
    return str(f.dateFormat() or "")


def _cached_success(fn):
    """Cache a result, but not a failure: a formatter that raised (or found
    no pattern) is tried again on the next call, and the error is logged
    once, not on every formatted date."""
    box = []
    logged = []

    @functools.wraps(fn)
    def wrapper():
        if box:
            return box[0]
        try:
            value = fn()
        except Exception:
            if not logged:
                logged.append(True)
                logging.exception("[platform_mac] %s failed; WinZapp's default is used", fn.__name__)
            return None
        if value:
            box.append(value)
        return value

    wrapper.cache_clear = lambda: (box.clear(), logged.clear())
    return wrapper


@_cached_success
def mac_date_strftime():
    from core import locale_format as lf
    pattern = _mac_pattern(1, 0)   # NSDateFormatterShortStyle, NoStyle
    return lf._translate_pattern(_icu_to_windows(pattern), lf._DATE_TOKENS) if pattern else None


@_cached_success
def mac_time_strftime():
    from core import locale_format as lf
    pattern = _mac_pattern(0, 1)
    return lf._translate_pattern(_icu_to_windows(pattern), lf._TIME_TOKENS) if pattern else None


@functools.lru_cache(maxsize=1)
def mac_country_iso2():
    try:
        from Foundation import NSLocale
        code = NSLocale.currentLocale().countryCode()
        return str(code).upper() if code else None
    except Exception:
        return None


def mac_ui_language():
    try:
        from Foundation import NSLocale
        langs = NSLocale.preferredLanguages()
        return str(langs[0]) if langs else None
    except Exception:
        return None


# ---------------------------------------------------- computer-audio recording --

def _unavailable_label(panel):
    i18n = panel.main_window.i18n
    return f"{i18n.t('record_voice_message_system_audio')} ({i18n.t('mac_not_yet_available')})"


def _dim_system_audio_button(panel):
    btn = getattr(panel, "_record_voice_system_btn", None)
    if btn is None:
        return
    btn.SetLabel(_unavailable_label(panel))
    btn.Disable()


def _on_record_system_audio(self, event=None):
    try:
        self.main_window.output(_unavailable_label(self))
    except Exception:
        logging.debug("[platform_mac] could not announce", exc_info=True)


def install():
    from ui.conversation_panel import media_paths
    media_paths.reveal_file_in_folder = reveal_file_in_folder

    from core import locale_format as lf
    lf._windows_date_strftime = mac_date_strftime
    lf._windows_time_strftime = mac_time_strftime
    lf.get_country_or_region_iso2 = mac_country_iso2
    lf._get_windows_ui_language_raw = mac_ui_language

    from . import system_audio_mac
    if system_audio_mac.available():
        return

    from ui.conversation_panel import system_audio_recording as sar
    sar.SystemAudioRecordingMixin._on_record_system_audio = _on_record_system_audio

    from ui import conversations
    cls = conversations.ConversationsPanel
    orig_init_ui = cls.init_UI
    orig_refresh = cls.refresh_labels

    def init_ui(self, *a, **k):
        result = orig_init_ui(self, *a, **k)
        _dim_system_audio_button(self)
        return result

    def refresh_labels(self, *a, **k):
        result = orig_refresh(self, *a, **k)
        _dim_system_audio_button(self)
        return result

    cls.init_UI = init_ui
    cls.refresh_labels = refresh_labels
