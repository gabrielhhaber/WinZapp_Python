"""Building the settings window a piece at a time.

Ctrl+, took 4 to 5 seconds: every control of every page and of every window
behind a button was created, filled and given its device lists before the
window showed. Now only the General page is. Each of the others is a *unit*,
built the first time it is shown (a category chosen, a button pressed), and the
search, which walks every control, builds what is left.

What a unit that was never built means, everywhere:

* Its values cannot have changed, so `_validate()` and `_apply_values()` skip
  it: what is stored stays as it is, which is exactly what writing back an
  untouched control would have done.
* Its labels need no translating: it reads the language when it is built.

`ready()` is that test, and `wanted()` the one for loading, where a unit loads
only itself. Both answer yes on an object with no `_built_units`, which is every
test stub that binds these methods: there, everything exists, as it always did.

No control can be missing. A method that reaches one before its unit is built
gets here, through `__getattr__`, and the unit is built on the spot: the worst a
path nobody thought of can do is cost a moment, never raise. The attributes each
unit creates are read from the bytecode of its builder, so the map cannot fall
out of step with the code and also works in the frozen build, which has no
sources. `tests/test_settings_lazy.py` keeps that fallback a fallback: the
always-on paths go through `ready()`.
"""

import dis
import logging
import time
import types

import wx

#: unit -> the method that builds it. The order is the one the search builds in.
UNITS = {
    "general": "_build_general_controls",
    "notifications": "_build_notifications_controls",
    "interface": "_build_interface_controls",
    "messages": "_build_messages_controls",
    "reactions": "_build_reactions_controls",
    "chats": "_build_chats_controls",
    "chat_lock": "_build_chat_lock_controls",
    "confirmations": "_build_confirmations_controls",
    "shortcuts": "_build_shortcuts_page",
    "accessibility": "_build_accessibility_controls",
    "speech": "_build_speech_controls",
    "audio": "_build_audio_devices_controls",
    "audio_playback": "_build_audio_playback_controls",
    "sounds": "_build_sound_events_controls",
    "storage": "_build_storage_controls",
    "backup": "_build_backup_controls",
    "calls": "_build_calls_controls",
    "ai": "_build_ai_controls",
    "transcription": "_build_transcription_controls",
    "whatsapp": "_build_whatsapp_controls",
    "whatsapp_privacy": "_build_whatsapp_privacy_controls",
    "whatsapp_profile": "_build_whatsapp_profile_controls",
    "connection": "_build_connection_controls",
    "advanced": "_build_advanced_page",
}

#: The unit of each category of the list, by the attribute of its page.
PAGE_UNITS = (
    ("_general_page", "general"),
    ("_ui_page", "interface"),
    ("_accessibility_page", "accessibility"),
    ("_audio_devices_page", "audio"),
    ("_sound_events_page", "sounds"),
    ("_storage_page", "storage"),
    ("_calls_page", "calls"),
    ("_ai_holder", "ai"),
    ("_transcription_holder", "transcription"),
    ("_whatsapp_page", "whatsapp"),
    ("_conn_page", "connection"),
    ("_advanced_page", "advanced"),
)


def ready(owner, unit) -> bool:
    """Whether `unit` has controls to read. Yes on a stub (see the module's docstring)."""
    built = getattr(owner, "_built_units", None)
    return built is None or unit in built


def wanted(loading, unit) -> bool:
    """Whether a load for `loading` covers `unit`; None loads everything."""
    return loading is None or loading == unit


def _names_stored(code) -> set:
    names = {i.argval for i in dis.get_instructions(code) if i.opname == "STORE_ATTR"}
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            names |= _names_stored(const)
    return names


def _names_called(code) -> set:
    names = set(code.co_names)
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            names |= _names_called(const)
    return names


def attributes_created(cls, method) -> set:
    """Everything `method` of `cls` stores on an object, following the helpers
    of `cls` it mentions."""
    seen = set()
    created = set()
    pending = [method]
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        function = getattr(cls, name, None)
        code = getattr(function, "__code__", None)
        if code is None:
            continue
        created |= _names_stored(code)
        # Not the handlers: what they store is state, not controls.
        pending += [n for n in _names_called(code)
                    if n.startswith("_") and not n.startswith("_on_") and hasattr(cls, n)]
    return created


_owners_of_class = {}


def unit_of_attribute(cls, name):
    """The unit whose builder creates attribute `name`; None for any other name."""
    owners = _owners_of_class.get(cls)
    if owners is None:
        owners = {}
        # Reversed, so a name two units store (a shared helper's) goes to the
        # first, which is the one that does not depend on the other.
        for unit, method in reversed(UNITS.items()):
            for attribute in attributes_created(cls, method):
                owners[attribute] = unit
        _owners_of_class[cls] = owners
    return owners.get(name)


class LazyUnitsMixin:
    """`SettingsDialog`'s units: which are built, and building them."""

    def __getattr__(self, name):
        # Only for names Python did not find, so a built control never gets
        # here. `__dict__` directly: asking `self` would land here again.
        built = self.__dict__.get("_built_units")
        unit = unit_of_attribute(type(self), name) if built is not None else None
        if unit is None or unit in built or unit in self.__dict__["_building_units"]:
            raise AttributeError(name)
        logging.info("[settings] %s needed before its page was opened: building %s.", name, unit)
        self._ensure_unit(unit)
        return self.__dict__[name]

    def _ensure_unit(self, unit):
        """Build `unit` and show the current settings in it, once."""
        if unit in self._built_units or unit in self._building_units:
            return
        self._building_units.add(unit)
        started = time.perf_counter()
        loading = self._loading_values
        self._loading_values = True
        try:
            getattr(self, UNITS[unit])()
            self._built_units.add(unit)
            self._load_values(unit)
            if unit == "shortcuts":
                self._refresh_shortcut_rows()
        except Exception:
            # Not raised: this runs under a key press in the category list,
            # and the rest of the window still works. Not built again either:
            # a second run would put a second set of controls on the page.
            logging.exception("[settings] could not build %s.", unit)
            self._built_units.discard(unit)
            self._failed_units = getattr(self, "_failed_units", set()) | {unit}
        finally:
            self._loading_values = loading
            self._building_units.discard(unit)
        logging.info("[settings] %s built in %.0f ms.", unit, (time.perf_counter() - started) * 1000)

    def _ensure_page_of_index(self, index):
        """Called before a category is shown."""
        page = self._notebook.GetPage(index)
        for attribute, unit in PAGE_UNITS:
            if self.__dict__.get(attribute) is page:
                if unit not in self._built_units and unit not in self._failed_units_so_far():
                    self._ensure_unit(unit)
                    self._fit_page(page)
                return

    def _failed_units_so_far(self):
        return getattr(self, "_failed_units", ())

    def _fit_page(self, page):
        """A page built now was not there when the window took its size: lay it
        out and let the window grow for it, up to the screen. Never smaller: a
        window that changes size at every arrow key in the list is no use."""
        page.Layout()
        current = self.GetSize()
        best = self.GetBestSize()
        # A hidden window is on no display: NOT_FOUND, and the first one answers.
        area = wx.Display(max(wx.Display.GetFromWindow(self), 0)).GetClientArea()
        wanted_size = wx.Size(min(max(current.x, best.x), area.width),
                              min(max(current.y, best.y), area.height))
        if wanted_size != current:
            self.SetSize(wanted_size)
        self.Layout()

    def _ensure_all_units(self):
        """Everything: what the search walks."""
        missing = [u for u in UNITS if u not in self._built_units
                   and u not in self._failed_units_so_far()]
        if not missing:
            return
        with wx.BusyCursor():
            for unit in missing:
                self._ensure_unit(unit)
        self._fit_page(self._notebook.GetCurrentPage())
