"""Mac wording for WinZapp's user-facing strings, in every locale.

Strings that describe Windows ("Start WinZapp with Windows") have a Mac
variant in WinZapp's own language files under the same key plus
MAC_SUFFIX ("autostart_ask_title_macos": "Start WinZapp at login"), so
translators see them beside the Windows text and
tests/test_language_files_in_sync.py keeps them in every locale. On the Mac
the variant is used in place of the Windows text.

Any other string that still names Windows gets "macOS" as a fallback, and one
that names NVDA gets VoiceOver, the screen reader there.
"""

import re

MAC_SUFFIX = "_macos"
_WINDOWS = re.compile(r"\bWindows\b")
_NVDA = re.compile(r"\bNVDA\b")


def mac_wording(data):
    """Give one locale's translations their Mac wording, in place: the dict
    is the one core.i18n caches, so every later lookup sees it too."""
    variants = {k[:-len(MAC_SUFFIX)]: v for k, v in data.items() if k.endswith(MAC_SUFFIX)}
    for k, v in list(data.items()):
        if k in variants:
            data[k] = variants[k]
        elif isinstance(v, str) and not k.endswith(MAC_SUFFIX):
            data[k] = _NVDA.sub("VoiceOver", _WINDOWS.sub("macOS", v))
    return data


def install():
    from core import i18n
    orig = i18n._load_translations

    def load(lang_code):
        return mac_wording(orig(lang_code))

    i18n._load_translations = load
