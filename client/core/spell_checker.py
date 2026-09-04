"""Small adapter around the Windows spell-checking service.

WinZapp's message editor remains an ordinary ``wx.TextCtrl``. This module
checks the text after the user appends a word boundary and reports a detected
error through a callback. It neither changes the message nor exposes custom
UI Automation text attributes.

``comtypes`` is optional at import time so a missing Windows component can
never prevent WinZapp from starting. Spell checking is simply unavailable
when no suitable Windows dictionary can be opened.
"""

from __future__ import annotations

import os
import ctypes
from ctypes import POINTER, c_int, c_ulong, wintypes
from typing import Callable


try:
    import winsound
except ImportError:  # pragma: no cover - only relevant off Windows
    winsound = None


if os.name == "nt":
    try:
        import comtypes
        from comtypes import COMError, COMMETHOD, GUID, HRESULT, IUnknown
        from comtypes.automation import BSTR
        from comtypes.client import CreateObject
    except ImportError:  # pragma: no cover - exercised on minimal Windows venvs
        comtypes = None
else:
    comtypes = None


CLSID_SPELL_CHECKER_FACTORY = GUID(
    "{7AB36653-1796-484B-BDFA-E74F1DB7C1DC}"
) if comtypes is not None else None
IID_ISPELL_CHECKER_FACTORY = GUID(
    "{8E018A9D-2415-4677-BF08-794EA61F94BB}"
) if comtypes is not None else None
IID_ISPELL_CHECKER = GUID(
    "{B6FD0B71-E2BC-4653-8D05-F197E412770B}"
) if comtypes is not None else None
IID_IENUM_SPELLING_ERROR = GUID(
    "{803E3BD4-2828-4410-8290-418D1D73C762}"
) if comtypes is not None else None
IID_ISPELLING_ERROR = GUID(
    "{B7C82D61-FBE8-4B47-9B27-6C0D2E0DE0A3}"
) if comtypes is not None else None

S_FALSE = 1
CORRECTIVE_ACTION_DELETE = 3
LOCALE_NAME_MAX_LENGTH = 85

# WinZapp uses a short code for Polish while the Windows spell-checking API
# expects a BCP 47 language tag. The other currently bundled UI languages are
# already valid Windows tags, but keeping the whole mapping explicit makes the
# boundary between application locales and spell-checker locales clear.
_WINZAPP_LANGUAGE_TAGS = {
    "en-US": "en-US",
    "es-ES": "es-ES",
    "pl": "pl-PL",
    "pt-BR": "pt-BR",
    "pt-PT": "pt-PT",
}


if comtypes is not None:

    class ISpellingError(IUnknown):
        _iid_ = IID_ISPELLING_ERROR
        _methods_ = [
            COMMETHOD([], HRESULT, "get_StartIndex", (['out'], POINTER(c_ulong), "value")),
            COMMETHOD([], HRESULT, "get_Length", (['out'], POINTER(c_ulong), "value")),
            COMMETHOD([], HRESULT, "get_CorrectiveAction", (['out'], POINTER(c_int), "value")),
            COMMETHOD([], HRESULT, "get_Replacement", (['out'], POINTER(wintypes.LPWSTR), "value")),
        ]


    class IEnumSpellingError(IUnknown):
        _iid_ = IID_IENUM_SPELLING_ERROR
        _methods_ = [
            COMMETHOD(
                [], HRESULT, "Next",
                (['out'], POINTER(POINTER(ISpellingError)), "value"),
            ),
        ]


    class ISpellChecker(IUnknown):
        _iid_ = IID_ISPELL_CHECKER
        _methods_ = [
            COMMETHOD([], HRESULT, "get_LanguageTag", (['out'], POINTER(wintypes.LPWSTR), "value")),
            COMMETHOD(
                [], HRESULT, "Check",
                (['in'], wintypes.LPCWSTR, "text"),
                (['out'], POINTER(POINTER(IEnumSpellingError)), "value"),
            ),
            COMMETHOD(
                [], HRESULT, "Suggest",
                (['in'], wintypes.LPCWSTR, "word"),
                (['out'], POINTER(POINTER(IUnknown)), "value"),
            ),
            COMMETHOD([], HRESULT, "Add", (['in'], wintypes.LPCWSTR, "word")),
            COMMETHOD([], HRESULT, "Ignore", (['in'], wintypes.LPCWSTR, "word")),
            COMMETHOD(
                [], HRESULT, "AutoCorrect",
                (['in'], wintypes.LPCWSTR, "fromWord"),
                (['in'], wintypes.LPCWSTR, "toWord"),
            ),
            COMMETHOD(
                [], HRESULT, "GetOptionValue",
                (['in'], wintypes.LPCWSTR, "optionId"),
                (['out'], POINTER(wintypes.BYTE), "value"),
            ),
            COMMETHOD([], HRESULT, "get_OptionIds", (['out'], POINTER(POINTER(IUnknown)), "value")),
            COMMETHOD([], HRESULT, "get_Id", (['out'], POINTER(wintypes.LPWSTR), "value")),
            COMMETHOD([], HRESULT, "get_LocalizedName", (['out'], POINTER(wintypes.LPWSTR), "value")),
            COMMETHOD(
                [], HRESULT, "add_SpellCheckerChanged",
                (['in'], POINTER(IUnknown), "handler"),
                (['out'], POINTER(wintypes.DWORD), "cookie"),
            ),
            COMMETHOD([], HRESULT, "remove_SpellCheckerChanged", (['in'], wintypes.DWORD, "cookie")),
            COMMETHOD(
                [], HRESULT, "GetOptionDescription",
                (['in'], wintypes.LPCWSTR, "optionId"),
                (['out'], POINTER(POINTER(IUnknown)), "value"),
            ),
            COMMETHOD(
                [], HRESULT, "ComprehensiveCheck",
                (['in'], wintypes.LPCWSTR, "text"),
                (['out'], POINTER(POINTER(IEnumSpellingError)), "value"),
            ),
        ]


    class ISpellCheckerFactory(IUnknown):
        _iid_ = IID_ISPELL_CHECKER_FACTORY
        _methods_ = [
            COMMETHOD(
                [], HRESULT, "get_SupportedLanguages",
                (['out'], POINTER(POINTER(IUnknown)), "value"),
            ),
            COMMETHOD(
                [], HRESULT, "IsSupported",
                (['in'], wintypes.LPCWSTR, "languageTag"),
                (['out'], POINTER(wintypes.BOOL), "value"),
            ),
            COMMETHOD(
                [], HRESULT, "CreateSpellChecker",
                (['in'], wintypes.LPCWSTR, "languageTag"),
                (['out'], POINTER(POINTER(ISpellChecker)), "value"),
            ),
        ]


def _unwrap_out(value):
    """comtypes zwraca parametry [out] jako wartość albo krotkę."""
    if isinstance(value, (tuple, list)):
        return value[-1] if value else None
    return value


def _normalize_language_tag(language: str | None) -> str:
    """Return a Windows-friendly BCP 47 tag for a WinZapp locale."""
    language = (language or "").strip()
    if not language:
        return ""
    mapped = _WINZAPP_LANGUAGE_TAGS.get(language)
    if mapped:
        return mapped
    parts = language.replace("_", "-").split("-")
    normalized = [parts[0].lower()]
    for part in parts[1:]:
        normalized.append(part.upper() if len(part) in (2, 3) else part.title())
    return "-".join(normalized)


def _windows_language_names() -> list[str]:
    """Return the active input and user-default Windows locale names."""
    if os.name != "nt":
        return []

    names = []
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        user32.GetKeyboardLayout.argtypes = [wintypes.DWORD]
        user32.GetKeyboardLayout.restype = ctypes.c_void_p
        kernel32.LCIDToLocaleName.argtypes = [
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.c_int,
            wintypes.DWORD,
        ]
        kernel32.LCIDToLocaleName.restype = ctypes.c_int
        kernel32.GetUserDefaultLocaleName.argtypes = [
            wintypes.LPWSTR,
            ctypes.c_int,
        ]
        kernel32.GetUserDefaultLocaleName.restype = ctypes.c_int

        keyboard_layout = user32.GetKeyboardLayout(0)
        if keyboard_layout:
            buffer = ctypes.create_unicode_buffer(LOCALE_NAME_MAX_LENGTH)
            language_id = int(keyboard_layout) & 0xFFFF
            if kernel32.LCIDToLocaleName(
                language_id, buffer, len(buffer), 0
            ):
                names.append(buffer.value)

        buffer = ctypes.create_unicode_buffer(LOCALE_NAME_MAX_LENGTH)
        if kernel32.GetUserDefaultLocaleName(buffer, len(buffer)):
            names.append(buffer.value)
    except (AttributeError, OSError, TypeError, ValueError):
        pass

    result = []
    for name in names:
        normalized = _normalize_language_tag(name)
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def _language_candidates(preferred_language: str | None) -> list[str]:
    """Return preferred-app then Windows-system languages, without repeats."""
    candidates = []
    preferred = _normalize_language_tag(preferred_language)
    if preferred:
        candidates.append(preferred)
    for language in _windows_language_names():
        if language not in candidates:
            candidates.append(language)
    return candidates


def _word_ended(previous_text: str, current_text: str) -> bool:
    """Return whether one trailing whitespace character was just appended.

    Requiring an exact one-character append is important: Backspace can expose
    a space that was already present before the deleted word. Treating that as
    a newly typed boundary would replay the error sound while deleting text.
    """
    return bool(
        current_text
        and len(current_text) == len(previous_text) + 1
        and current_text.startswith(previous_text)
        and current_text[-1].isspace()
        and (len(current_text) == 1 or not current_text[-2].isspace())
    )


class WindowsSpellChecker:
    """Lightweight checker invoked by the message editor after whitespace."""

    def __init__(
        self,
        language: str | None = None,
        on_error: Callable[[], None] | None = None,
    ):
        self.preferred_language = _normalize_language_tag(language)
        self.language = ""
        self._on_error = on_error
        self._checker = None
        self._initialized = False
        self._last_text = ""

    def set_language(self, language: str | None) -> None:
        """Change the preferred language and reopen Windows' checker lazily."""
        language = _normalize_language_tag(language)
        if language == self.preferred_language:
            return
        self.preferred_language = language
        self.language = ""
        self._checker = None
        self._initialized = False

    def _play_error_sound(self) -> None:
        """Notify the host, falling back to the old Windows system cue."""
        if self._on_error is not None:
            try:
                self._on_error()
                return
            except Exception:
                # Spell checking is optional feedback; a broken output device
                # must never interrupt typing or message sending.
                pass
        if winsound is not None:
            try:
                winsound.MessageBeep(winsound.MB_OK)
            except (RuntimeError, OSError):
                pass

    def _get_checker(self):
        if self._initialized:
            return self._checker
        self._initialized = True
        if comtypes is None:
            return None
        try:
            try:
                comtypes.CoInitialize()
            except (AttributeError, OSError):
                pass
            factory = CreateObject(
                CLSID_SPELL_CHECKER_FACTORY,
                interface=ISpellCheckerFactory,
            )
            for language in _language_candidates(self.preferred_language):
                if not bool(_unwrap_out(factory.IsSupported(language))):
                    continue
                self._checker = _unwrap_out(
                    factory.CreateSpellChecker(language)
                )
                if self._checker is not None:
                    self.language = language
                    break
        except (COMError, OSError, RuntimeError, TypeError):
            self._checker = None
        return self._checker

    def errors_for_text(self, text: str) -> list[tuple[int, int]]:
        """Return spelling-error ranges as start/end text offsets."""
        text = text or ""
        if not text:
            return []
        checker = self._get_checker()
        if checker is None:
            return []
        try:
            enumeration = _unwrap_out(checker.Check(text))
        except (COMError, OSError, RuntimeError, TypeError):
            return []

        errors = []
        while enumeration:
            try:
                item = _unwrap_out(enumeration.Next())
            except COMError as error:
                if getattr(error, "hresult", None) == S_FALSE:
                    break
                break
            except (OSError, RuntimeError, TypeError):
                break
            if not item:
                break
            try:
                start = int(_unwrap_out(item.get_StartIndex()))
                length = int(_unwrap_out(item.get_Length()))
                action = int(_unwrap_out(item.get_CorrectiveAction()))
            except (COMError, OSError, RuntimeError, TypeError, AttributeError):
                break
            # Powtórzenie poprawnego słowa bywa zwracane jako sugestia usunięcia;
            # traktujemy je jako sugestię gramatyczną, nie błąd pisowni.
            if action == CORRECTIVE_ACTION_DELETE:
                continue
            if length > 0:
                errors.append((start, start + length))
        return errors

    def text_changed(self, text: str) -> list[tuple[int, int]]:
        """Check after appended whitespace and cue a just-completed error."""
        text = text or ""
        previous_text = self._last_text
        self._last_text = text
        if not _word_ended(previous_text, text):
            return []

        errors = self.errors_for_text(text)
        previous_character = len(text) - 2
        if any(start <= previous_character < end for start, end in errors):
            self._play_error_sound()
        return errors
