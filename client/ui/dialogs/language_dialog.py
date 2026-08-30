"""
WinZapp – Language Selection Dialog
=====================================
Shown on first launch, before any API module installation or initial setup.
The user picks a language and clicks OK to proceed, or Cancel to exit.

This dialog runs before core.i18n.I18n exists — there is no saved language
in settings yet for it to read — so it can't call I18n.t(). Its own UI
strings (title, prompt, OK/Cancel) are instead read straight out of the
matching languages/<code>.json file for whichever language
_detect_system_language() below resolves to (the user's Windows display
language if it's one of ours, else English), rather than being hardcoded in
one fixed language regardless of the machine's actual settings. The list of
languages itself is read from language_map.json — the same file
core/i18n.py's LANGUAGE_NAMES loads from — so a new locale dropped in there
shows up here too without a rebuild, and is sorted alphabetically (no
language pinned first) the same way client/countries.py sorts its own list.
"""

import json
import wx

from app_paths import resource_path
from core.utils import normalize_for_search
from core.combo_search import bind_incremental_search

# Fallback used only if languages/language_map.json is missing or unreadable.
_FALLBACK_LANGUAGE_CHOICES = [
    ("Português (Brasil)",      "pt-BR"),
    ("English (United States)", "en-US"),
]

# The handful of keys this dialog needs. Used both to validate a loaded
# languages/<code>.json has all of them, and as the last-resort fallback if
# even languages/en-US.json can't be read (should never happen in practice).
_BOOTSTRAP_KEYS = ("language_select_title", "language_select_prompt", "ok", "cancel")
_HARDCODED_BOOTSTRAP_STRINGS = {
    "language_select_title":  "Select a language | WinZapp",
    "language_select_prompt": "Select a language",
    "ok":                     "&OK",
    "cancel":                 "&Cancel",
}


def _load_language_choices():
    """Return [(display_name, lang_code), ...] from language_map.json,
    sorted alphabetically (diacritics ignored) by display name — no
    language pinned first, same sort as client/countries.py's country
    list."""
    try:
        with open(resource_path("languages", "language_map.json"), "r", encoding="utf-8") as f:
            data = json.load(f)
        choices = [(name, code) for code, name in data.items()] if isinstance(data, dict) and data else None
    except Exception:
        choices = None
    if not choices:
        choices = list(_FALLBACK_LANGUAGE_CHOICES)
    choices.sort(key=lambda item: normalize_for_search(item[0], mode="nfkd"))
    return choices


# Maps human-readable name → language code, alphabetically sorted.
_LANGUAGE_CHOICES = _load_language_choices()


def _load_bootstrap_strings(lang_code: str) -> dict:
    """Read this dialog's own UI strings straight out of
    languages/<lang_code>.json, bypassing core.i18n.I18n (which reads the
    active language from settings — not written yet on a first run). Falls
    back to languages/en-US.json, then to a hardcoded copy of the same
    English strings, if even that can't be read."""
    for code in (lang_code, "en-US"):
        try:
            with open(resource_path("languages", f"{code}.json"), "r", encoding="utf-8") as f:
                data = json.load(f)
            if all(key in data for key in _BOOTSTRAP_KEYS):
                return data
        except Exception:
            continue
    return _HARDCODED_BOOTSTRAP_STRINGS


def _detect_system_language(available_codes, fallback: str = "en-US") -> str:
    """Best-effort match between the Windows UI/display language and one of
    *available_codes*: an exact match (system "pt-BR" -> our "pt-BR") first,
    then a same-language match ignoring region (system "pt-AO" -> our
    "pt-BR", the first matching entry in *available_codes*' own order), else
    *fallback*.

    Deliberately based on core.locale_format.get_system_ui_language() — the
    actual Windows display-language setting — and nothing else. This is the
    one place in the app where the display language IS the right signal;
    contrast client/countries.py's get_default_country_index(), which must
    NOT use it (see that function's own docstring for why a display
    language and a physical location are independent Windows settings that
    can disagree)."""
    try:
        from core.locale_format import get_system_ui_language
        detected = get_system_ui_language()
    except Exception:
        detected = None
    if not detected:
        return fallback
    if detected in available_codes:
        return detected
    prefix = detected.split("-")[0].lower()
    for code in available_codes:
        if code.split("-")[0].lower() == prefix:
            return code
    return fallback


class LanguageSelectionDialog(wx.Dialog):
    """
    First-run language picker shown before i18n is fully initialised.

    Attributes
    ----------
    selected_language : str
        BCP-47 language code chosen by the user (e.g. ``"pt-BR"``).
        Only valid after the dialog returns ``wx.ID_OK``.
    """

    def __init__(self, parent=None):
        self._lang_codes = [code for _, code in _LANGUAGE_CHOICES]
        default_code = _detect_system_language(self._lang_codes, fallback="en-US")
        strings = _load_bootstrap_strings(default_code)

        super().__init__(
            parent,
            title=strings["language_select_title"],
            style=wx.DEFAULT_DIALOG_STYLE,
        )
        self.selected_language: str = default_code

        self._build_ui(strings, default_code)
        self.Fit()
        self.SetMinSize((360, -1))
        self.Centre()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self, strings, default_code):
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        lbl = wx.StaticText(panel, label=strings["language_select_prompt"])
        sizer.Add(lbl, 0, wx.LEFT | wx.TOP | wx.RIGHT, 12)

        self._combo = wx.ComboBox(
            panel,
            style=wx.CB_READONLY,
            choices=[name for name, _ in _LANGUAGE_CHOICES],
        )
        default_idx = (
            self._lang_codes.index(default_code) if default_code in self._lang_codes else 0
        )
        self._combo.SetSelection(default_idx)
        bind_incremental_search(self._combo)
        sizer.Add(self._combo, 0, wx.EXPAND | wx.ALL, 8)

        btn_sizer = wx.StdDialogButtonSizer()
        ok_btn     = wx.Button(panel, wx.ID_OK,     label=strings["ok"])
        cancel_btn = wx.Button(panel, wx.ID_CANCEL, label=strings["cancel"])
        ok_btn.Bind(wx.EVT_BUTTON, self._on_ok)
        cancel_btn.Bind(wx.EVT_BUTTON, self._on_cancel)
        btn_sizer.AddButton(ok_btn)
        btn_sizer.AddButton(cancel_btn)
        btn_sizer.Realize()
        sizer.Add(btn_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        panel.SetSizer(sizer)
        dlg_sizer = wx.BoxSizer(wx.VERTICAL)
        dlg_sizer.Add(panel, 1, wx.EXPAND)
        self.SetSizer(dlg_sizer)

    # ── Events ────────────────────────────────────────────────────────────────

    def _on_ok(self, event):
        sel = self._combo.GetSelection()
        if sel != wx.NOT_FOUND:
            self.selected_language = self._lang_codes[sel]
        self.EndModal(wx.ID_OK)

    def _on_cancel(self, event):
        self.EndModal(wx.ID_CANCEL)
