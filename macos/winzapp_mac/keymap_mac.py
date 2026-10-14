"""Mac keyboard conventions for WinZapp, by rule.

WinZapp's shortcuts are written for Windows. On the Mac wx already maps
"Ctrl" to Command, but "Alt" is Option — which types characters (é, ç, ®)
in text fields — and several combinations land on system commands
(Cmd-Shift-Q logs out, Cmd-Space is Spotlight, Cmd-Shift-3/4/5 take
screenshots, Cmd-M minimises).

One table, WIN -> MAC, drives everything:

  * key events: a physical Mac combo is rewritten into the Windows combo
    WinZapp's accelerator tables and key handlers expect (Cmd-Option-R
    arrives as Alt+R), in the app-wide event filter, before any of them
    see it. A bare Option+letter that WinZapp would read as a shortcut is
    neutralised so Option keeps typing characters.
  * menu labels: "\\tCtrl+Alt+Shift+Q" becomes the Mac combo, so menus show
    and natively answer to the Mac keys.
  * text: shortcuts named in translations and accessibility hints
    ("Alt+1", "Ctrl+Shift+R") are spoken as "Command-1",
    "Command-Shift-R".

The rule: Ctrl -> Command; Alt -> Command-Option; Ctrl+Alt -> Command-Control;
Shift carries over. EXCEPTIONS below cover combos the rule would send to a
system command or that have a stronger Mac convention. New upstream
shortcuts follow the rule automatically; tests/ in macos/ flag any that
land on RESERVED.
"""

import re

import wx

# Modifier names. Windows side: ctrl, alt, shift. Mac side: cmd, ctrl
# (the physical Control key), opt, shift.
W_CTRL, W_ALT, W_SHIFT = "ctrl", "alt", "shift"
M_CMD, M_CTRL, M_OPT, M_SHIFT = "cmd", "ctrl", "opt", "shift"

DIGITS = [str(d) for d in range(10)]


def _c(*mods):
    return frozenset(mods)


EXCEPTIONS = {}


def _exc(win_mods, keys, mac_mods, mac_key=None):
    for k in keys:
        EXCEPTIONS[(_c(*win_mods), k)] = (_c(*mac_mods), mac_key or k)


# Sections: Alt+1..8 -> Command-1..8, like Mail/Messages sidebars.
_exc([W_ALT], list("12345678"), [M_CMD])
# Bookmarks move to the physical Control key (Command-digit is sections;
# Command-Shift-3/4/5 are screenshots).
_exc([W_CTRL], DIGITS, [M_CTRL])
_exc([W_CTRL, W_SHIFT], DIGITS, [M_CTRL, M_SHIFT])
# Temporary bookmarks: Alt+Shift+n / Ctrl+Alt+Shift+n.
_exc([W_ALT, W_SHIFT], DIGITS, [M_CMD, M_OPT])
_exc([W_CTRL, W_ALT, W_SHIFT], DIGITS, [M_CMD, M_OPT, M_SHIFT])
# Selection: Command-Space is Spotlight; select-all is Command-A on a Mac.
_exc([W_CTRL], ["SPACE"], [M_CMD, M_SHIFT])
_exc([W_CTRL, W_SHIFT], ["SPACE"], [M_CMD], "A")
# Archive / end call: Command-Shift-Q is Log Out. Mail archives on Cmd-Ctrl-A.
_exc([W_CTRL, W_SHIFT], ["Q"], [M_CMD, M_CTRL], "A")
# Exit: Command-Option-Shift-Q logs out instantly.
_exc([W_CTRL, W_ALT, W_SHIFT], ["Q"], [M_CMD], "Q")
# Go to quoted message: Command-Option-Shift-Q logs out without asking.
_exc([W_ALT, W_SHIFT], ["Q"], [M_CMD, M_OPT], "G")
# Call window mute: Command-M minimises.
_exc([W_CTRL], ["M"], [M_CMD, M_CTRL])
# Delete: a Mac's delete key is Backspace; Command-Delete deletes items.
_exc([], ["DELETE"], [M_CMD], "BACK")
_exc([W_CTRL, W_SHIFT], ["DELETE"], [M_CMD, M_SHIFT], "BACK")
# Help: Command-? (F1 keeps working too).
_exc([], ["F1"], [M_CMD, M_SHIFT], "/")
# Close account: Command-Shift-W.
_exc([W_CTRL], ["F4"], [M_CMD, M_SHIFT], "W")
# Emoji picker: Command-. means Cancel on a Mac.
_exc([W_CTRL], ["."], [M_CMD, M_CTRL], "E")
# Tab switching stays on the physical Control key (Command-Tab switches apps).
_exc([W_CTRL], ["TAB"], [M_CTRL])
_exc([W_CTRL, W_SHIFT], ["TAB"], [M_CTRL, M_SHIFT])

# Deliberate: WinZapp's Exit becomes the Mac's Quit.
INTENTIONAL = {(_c(M_CMD), "Q")}

# Combos macOS or VoiceOver own; a translated shortcut must never land here.
RESERVED = {
    (_c(M_CMD), k) for k in ("H", "M", "Q", "TAB", "SPACE", "`")
} | {
    (_c(M_CMD, M_SHIFT), k) for k in ("3", "4", "5", "Q")
} | {
    (_c(M_CMD, M_OPT), k) for k in ("D", "H", "SPACE", "ESCAPE")
} | {
    (_c(M_CMD, M_OPT, M_SHIFT), "Q"),
    (_c(M_CMD, M_CTRL), "Q"), (_c(M_CMD, M_CTRL), "F"),
    (_c(M_CMD, M_CTRL), "SPACE"), (_c(M_CMD, M_CTRL), "D"),
    (_c(M_CMD, M_CTRL, M_SHIFT), "3"), (_c(M_CMD, M_CTRL, M_SHIFT), "4"),
}


def win_to_mac(mods, key):
    mods = frozenset(mods)
    hit = EXCEPTIONS.get((mods, key))
    if hit:
        return hit
    out = set()
    if W_SHIFT in mods:
        out.add(M_SHIFT)
    if W_CTRL in mods and W_ALT in mods:
        out |= {M_CMD, M_CTRL}
    elif W_CTRL in mods:
        out.add(M_CMD)
    elif W_ALT in mods:
        out |= {M_CMD, M_OPT}
    return frozenset(out), key


# Physical Mac combos that map back to a Windows combo. Built lazily from
# every Windows combo that could plausibly exist (all modifier sets x keys).
_MAC_TO_WIN = None
_WIN_MOD_SETS = [_c(), _c(W_CTRL), _c(W_ALT), _c(W_SHIFT), _c(W_CTRL, W_SHIFT),
                 _c(W_ALT, W_SHIFT), _c(W_CTRL, W_ALT), _c(W_CTRL, W_ALT, W_SHIFT)]


def _all_keys():
    keys = [chr(c) for c in range(ord("A"), ord("Z") + 1)] + DIGITS
    keys += [f"F{n}" for n in range(1, 13)]
    keys += ["SPACE", "DELETE", "BACK", "TAB", "RETURN", "ESCAPE", "HOME", "END",
             "PAGEUP", "PAGEDOWN", "UP", "DOWN", "LEFT", "RIGHT", ",", ".", "/", "-", "="]
    return keys


def mac_to_win_table():
    """Physical Mac combo -> the Windows combo WinZapp listens for.
    Exceptions take priority over the rule where both produce one combo."""
    global _MAC_TO_WIN
    if _MAC_TO_WIN is None:
        table = {}
        for (win_mods, key), mac in EXCEPTIONS.items():
            table.setdefault(mac, (win_mods, key))
        for mods in _WIN_MOD_SETS:
            if not mods or mods == _c(W_SHIFT):
                continue  # unmodified / Shift-only keys pass straight through
            for key in _all_keys():
                if (mods, key) in EXCEPTIONS:
                    continue
                table.setdefault(win_to_mac(mods, key), (mods, key))
        _MAC_TO_WIN = table
    return _MAC_TO_WIN


# ------------------------------------------------------------ key events --

_WXK_NAMES = {
    wx.WXK_SPACE: "SPACE", wx.WXK_DELETE: "DELETE", wx.WXK_BACK: "BACK",
    wx.WXK_TAB: "TAB", wx.WXK_RETURN: "RETURN", wx.WXK_ESCAPE: "ESCAPE",
    wx.WXK_HOME: "HOME", wx.WXK_END: "END", wx.WXK_PAGEUP: "PAGEUP",
    wx.WXK_PAGEDOWN: "PAGEDOWN", wx.WXK_UP: "UP", wx.WXK_DOWN: "DOWN",
    wx.WXK_LEFT: "LEFT", wx.WXK_RIGHT: "RIGHT",
}
_NAME_TO_WXK = {v: k for k, v in _WXK_NAMES.items()}
for _n in range(1, 13):
    _WXK_NAMES[getattr(wx, f"WXK_F{_n}")] = f"F{_n}"
    _NAME_TO_WXK[f"F{_n}"] = getattr(wx, f"WXK_F{_n}")


def _key_name(code):
    if code in _WXK_NAMES:
        return _WXK_NAMES[code]
    if 32 < code < 127:
        return chr(code).upper()
    return None


def _key_code(name):
    if name in _NAME_TO_WXK:
        return _NAME_TO_WXK[name]
    return ord(name)


def physical_mods(evt):
    mods = set()
    if evt.ControlDown():      # Command on macOS
        mods.add(M_CMD)
    if evt.RawControlDown():   # the Control key
        mods.add(M_CTRL)
    if evt.AltDown():
        mods.add(M_OPT)
    if evt.ShiftDown():
        mods.add(M_SHIFT)
    return frozenset(mods)


def _apply(evt, win_mods, key_name):
    evt.SetControlDown(W_CTRL in win_mods)
    evt.SetRawControlDown(False)
    evt.SetAltDown(W_ALT in win_mods)
    evt.SetShiftDown(W_SHIFT in win_mods)
    code = _key_code(key_name)
    if code != evt.GetKeyCode():
        evt.SetKeyCode(code)


# In a text field these keep their native editing meaning (Command-Delete
# deletes to the line start, Command-A selects all, Command-arrows move).
_TEXT_NATIVE = {"BACK", "DELETE", "A", "LEFT", "RIGHT", "UP", "DOWN", "HOME", "END"}
_TEXT_CLASSES = (wx.TextCtrl, wx.ComboBox, wx.SearchCtrl)


def _in_text_field(evt):
    win = evt.GetEventObject()
    if not isinstance(win, wx.Window):
        win = wx.Window.FindFocus()
    return isinstance(win, _TEXT_CLASSES)


def translate_key_event(evt):
    """Rewrite a key event from Mac to Windows conventions in place."""
    if getattr(evt, "_wz_translated", False):
        return
    evt._wz_translated = True
    key = _key_name(evt.GetKeyCode())
    if key is None:
        return
    mods = physical_mods(evt)
    if key in _TEXT_NATIVE and M_OPT not in mods and _in_text_field(evt):
        return
    hit = mac_to_win_table().get((mods, key))
    if hit is not None:
        _apply(evt, hit[0], hit[1])
        return
    # Physical combos that WinZapp would read with a different meaning:
    if mods and M_CMD not in mods and M_CTRL not in mods and M_OPT in mods:
        # Option(+Shift)+key is typing (é, ç, ®...). Hide it from WinZapp's
        # Alt shortcuts; the character is still inserted natively.
        evt.SetAltDown(False)
        evt.SetMetaDown(True)
    elif M_CTRL in mods and M_CMD not in mods:
        # A bare Control combo that isn't a mapped one would otherwise look
        # like nothing to WinZapp; leave it as-is (native text editing).
        pass
    elif (M_CMD in mods and M_OPT in mods and M_CTRL not in mods) or \
            (M_CMD in mods and M_CTRL in mods):
        # Unmapped Command-Option / Command-Control combos must not reach
        # WinZapp looking like a plain Ctrl shortcut.
        evt.SetMetaDown(True)
    else:
        # Command(+Shift)+key: reject combos that a Windows Ctrl shortcut
        # would reach only through an exception (e.g. Ctrl+Space).
        win = (_c(W_CTRL, W_SHIFT) if M_SHIFT in mods else _c(W_CTRL)) if M_CMD in mods else None
        if win is not None and (win, key) in EXCEPTIONS:
            evt.SetMetaDown(True)


# ----------------------------------------------------------- menu labels --

_ACCEL_TO_MODS = [(wx.ACCEL_CTRL, W_CTRL), (wx.ACCEL_ALT, W_ALT), (wx.ACCEL_SHIFT, W_SHIFT)]


def mac_accel_label(accel):
    """'Ctrl+Alt+Shift+Q' (Windows) -> wx accelerator string for the Mac."""
    entry = wx.AcceleratorEntry()
    if not entry.FromString(accel):
        return accel
    flags, code = entry.GetFlags(), entry.GetKeyCode()
    key = _key_name(code)
    if key is None:
        return accel
    win_mods = frozenset(n for f, n in _ACCEL_TO_MODS if flags & f)
    mac_mods, mac_key = win_to_mac(win_mods, key)
    parts = []
    if M_CTRL in mac_mods:
        parts.append("RawCtrl")
    if M_OPT in mac_mods:
        parts.append("Alt")
    if M_SHIFT in mac_mods:
        parts.append("Shift")
    if M_CMD in mac_mods:
        parts.append("Ctrl")
    names = {"BACK": "Back", "DELETE": "Delete", "SPACE": "Space", "TAB": "Tab",
             "RETURN": "Return", "ESCAPE": "Esc", "PAGEUP": "PgUp", "PAGEDOWN": "PgDn",
             "HOME": "Home", "END": "End", "UP": "Up", "DOWN": "Down",
             "LEFT": "Left", "RIGHT": "Right"}
    parts.append(names.get(mac_key, mac_key))
    return "+".join(parts)


def mac_menu_text(text):
    if not isinstance(text, str) or "\t" not in text:
        return text
    label, _, accel = text.partition("\t")
    return f"{label}\t{mac_accel_label(accel.strip())}" if accel.strip() else text


# ------------------------------------------------------------------ text --

_MOD_WORD = r"(?:ctrl|control|alt|shift|option|cmd|command)"
_KEY_WORD = r"(?:f(?:1[0-2]|[1-9])|delete|del|space|tab|enter|return|esc|escape|home|end|" \
            r"page ?up|page ?down|pgup|pgdn|up|down|left|right|backspace|comma|period|" \
            r"[a-z0-9]|[,.\-=/?]|\{[a-z]+\})"
_SHORTCUT = re.compile(
    rf"(?<![\w+])((?:{_MOD_WORD}\s*\+\s*)+)({_KEY_WORD})(?![\w])", re.IGNORECASE)
_SPOKEN_MODS = [(M_CTRL, "Control"), (M_OPT, "Option"), (M_SHIFT, "Shift"), (M_CMD, "Command")]
_KEY_ALIASES = {"DEL": "DELETE", "ENTER": "RETURN", "ESC": "ESCAPE", "PGUP": "PAGEUP",
                "PGDN": "PAGEDOWN", "PAGE UP": "PAGEUP", "PAGE DOWN": "PAGEDOWN",
                "BACKSPACE": "BACK", "COMMA": ",", "PERIOD": "."}
_SPOKEN_KEYS = {"BACK": "Delete", "DELETE": "Forward Delete", "RETURN": "Return",
                "ESCAPE": "Escape", "SPACE": "Space", "TAB": "Tab", ",": "comma",
                ".": "period", "/": "slash", "PAGEUP": "Page Up", "PAGEDOWN": "Page Down"}


def mac_shortcut_text(text):
    """'Alt+1: go to chats' -> 'Command-1: go to chats'."""
    if not isinstance(text, str) or "+" not in text:
        return text

    def repl(m):
        mods_raw = [p.strip().lower() for p in m.group(1).split("+") if p.strip()]
        win = set()
        for p in mods_raw:
            if p in ("ctrl", "control", "cmd", "command"):
                win.add(W_CTRL)
            elif p in ("alt", "option"):
                win.add(W_ALT)
            elif p == "shift":
                win.add(W_SHIFT)
        key_raw = m.group(2)
        if key_raw.startswith("{"):          # "Alt+{letter}" placeholders
            key_disp, key = key_raw, None
        else:
            key = _KEY_ALIASES.get(key_raw.upper(), key_raw.upper())
        if key is None:
            mac_mods = win_to_mac(frozenset(win), "Z")[0]
            key_name = key_disp
        else:
            mac_mods, mac_key = win_to_mac(frozenset(win), key)
            key_name = _SPOKEN_KEYS.get(mac_key, mac_key if len(mac_key) > 1 else mac_key.upper())
            if mac_key == "/" and M_SHIFT in mac_mods:
                return "-".join(n for k, n in _SPOKEN_MODS if k in mac_mods and k != M_SHIFT) + "-?"
        names = [n for k, n in _SPOKEN_MODS if k in mac_mods]
        return "-".join(names + [key_name])

    return _SHORTCUT.sub(repl, text)


# --------------------------------------------------------------- install --

def _patch_menus():
    orig_append = wx.Menu.Append
    orig_insert = wx.Menu.Insert
    orig_prepend = wx.Menu.Prepend
    orig_set_label = wx.Menu.SetLabel
    orig_item_set = wx.MenuItem.SetItemLabel
    orig_check = wx.Menu.AppendCheckItem
    orig_radio = wx.Menu.AppendRadioItem

    def _fix_args(args, kwargs, idx):
        args = list(args)
        if len(args) > idx and isinstance(args[idx], str):
            args[idx] = mac_menu_text(args[idx])
        for k in ("item", "text"):
            if isinstance(kwargs.get(k), str):
                kwargs[k] = mac_menu_text(kwargs[k])
        return args, kwargs

    def append(self, *args, **kwargs):
        args, kwargs = _fix_args(args, kwargs, 1)
        return orig_append(self, *args, **kwargs)

    def insert(self, *args, **kwargs):
        args, kwargs = _fix_args(args, kwargs, 2)
        return orig_insert(self, *args, **kwargs)

    def prepend(self, *args, **kwargs):
        args, kwargs = _fix_args(args, kwargs, 1)
        return orig_prepend(self, *args, **kwargs)

    def set_label(self, id, label):
        return orig_set_label(self, id, mac_menu_text(label))

    def item_set(self, label):
        return orig_item_set(self, mac_menu_text(label))

    def check(self, id, item, *a, **k):
        return orig_check(self, id, mac_menu_text(item), *a, **k)

    def radio(self, id, item, *a, **k):
        return orig_radio(self, id, mac_menu_text(item), *a, **k)

    wx.Menu.Append = append
    wx.Menu.Insert = insert
    wx.Menu.Prepend = prepend
    wx.Menu.SetLabel = set_label
    wx.MenuItem.SetItemLabel = item_set
    wx.Menu.AppendCheckItem = check
    wx.Menu.AppendRadioItem = radio


def _patch_translations():
    from core import i18n
    orig = i18n._load_translations

    def load(lang_code):
        data = orig(lang_code)
        for k, v in list(data.items()):
            if isinstance(v, str) and "+" in v:
                # A "label\taccel" value keeps its accelerator for the menu
                # patch, which rewrites it in wx syntax.
                head, tab, tail = v.partition("\t")
                data[k] = mac_shortcut_text(head) + tab + tail
        return data

    i18n._load_translations = load


def install():
    _patch_menus()
    _patch_translations()
    from . import accessibility_mac
    accessibility_mac.KEY_FILTERS.append(translate_key_event)
    accessibility_mac.TEXT_FILTERS.append(mac_shortcut_text)
