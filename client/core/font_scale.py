"""Font scale for low-vision users: the choices, the arithmetic and the walk
that gives every native control its scaled font.

Off by default (100 %): nothing in this module touches a control until the
person picks another size, so the app looks and reads exactly as before. Only
the font of native controls changes; no control is drawn by hand, and colours
are never set, so the Windows high-contrast theme keeps applying by itself.

The walk is duck-typed (GetFont/SetFont/GetChildren), so it is tested with
plain stubs and never needs a window.
"""

#: Percentages offered in Settings > Interface, smallest first.
SCALES = (100, 110, 125, 150, 175, 200)
DEFAULT = 100

SECTION = "user_interface"
SETTING = "font_scale_percent"

#: Attribute that remembers a window's own point size at 100 %, so changing the
#: scale twice never compounds (110 % of 110 %).
_BASE_ATTR = "_winzapp_font_base_pt"


def normalize(value) -> int:
    """The stored value as one of SCALES; anything unusable is the default.

    A hand-edited 130 snaps to the nearest offered size instead of being
    ignored, and a bool (JSON true) is not a number here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return DEFAULT
    return min(SCALES, key=lambda scale: (abs(scale - value), scale))


def scale_of(settings) -> int:
    """The scale saved in *settings* (the whole settings dict)."""
    section = settings.get(SECTION) if isinstance(settings, dict) else None
    if not isinstance(section, dict):
        return DEFAULT
    return normalize(section.get(SETTING, DEFAULT))


def step(current, direction) -> int:
    """The next scale up (direction > 0) or down (direction < 0) from
    *current*; the same scale at either end. direction 0 restores the default."""
    current = normalize(current)
    if direction == 0:
        return DEFAULT
    index = SCALES.index(current) + (1 if direction > 0 else -1)
    return SCALES[max(0, min(len(SCALES) - 1, index))]


def scaled_point_size(base_pt, percent) -> float:
    """*base_pt* at *percent*, in quarter points: wx keeps fractional sizes and
    quarters are enough to tell 110 % of 9 pt from 100 %."""
    return round(float(base_pt) * normalize(percent) / 100 * 4) / 4


def _get_pt(font) -> float:
    getter = getattr(font, "GetFractionalPointSize", None)
    return float(getter() if getter is not None else font.GetPointSize())


def _set_pt(font, points) -> None:
    setter = getattr(font, "SetFractionalPointSize", None)
    if setter is not None:
        setter(points)
    else:
        font.SetPointSize(max(1, round(points)))


def _scale_window(window, percent) -> None:
    """Children first: a parent's SetFont is inherited by the children that
    have no font of their own, and one that was already given its scaled font
    must not be read as if that were its base."""
    for child in list(window.GetChildren()):
        try:
            _scale_window(child, percent)
        except RuntimeError:     # destroyed while being walked
            continue
    font = window.GetFont()
    if font is None or not font.IsOk():
        return
    base = getattr(window, _BASE_ATTR, None)
    if base is None:
        if percent == DEFAULT:
            return               # never scaled: leave it alone
        base = _get_pt(font)
        try:
            setattr(window, _BASE_ATTR, base)
        except AttributeError:
            return               # a window that takes no attributes: unscaled
    wanted = scaled_point_size(base, percent)
    if abs(_get_pt(font) - wanted) < 0.01:
        return
    _set_pt(font, wanted)
    window.SetFont(font)


def _position_pairs(window):
    """(getter, setter) of what a list keeps: the selected row of a list box or
    choice, the focused row of a list control. A text control's GetSelection
    returns a range, not a row, so only an int counts."""
    pairs = []
    if hasattr(window, "SetSelection") and hasattr(window, "GetCount"):
        pairs.append((window.GetSelection, window.SetSelection))
    if hasattr(window, "GetFocusedItem") and hasattr(window, "Focus"):
        pairs.append((window.GetFocusedItem, window.Focus))
    return pairs


def _positions(window):
    """[(setter, value)] of every list under *window* (itself included)."""
    found = []
    try:
        for getter, setter in _position_pairs(window):
            value = getter()
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                found.append((setter, value, getter))
        children = list(window.GetChildren())
    except RuntimeError:
        return found
    for child in children:
        found += _positions(child)
    return found


def apply_scale(window, percent, find_focus=None) -> None:
    """Give *window* and everything inside it the font for *percent*.

    Frozen while it runs. The control that had the keyboard focus and each
    list's selected or focused row are put back if changing the font moved
    them: the person must land where they were, with nothing read out over it.
    *find_focus* is wx.Window.FindFocus, a parameter so a stub can stand in."""
    percent = normalize(percent)
    focused = find_focus() if find_focus is not None else None
    positions = _positions(window)
    window.Freeze()
    try:
        _scale_window(window, percent)
    finally:
        window.Thaw()
    for setter, value, getter in positions:
        try:
            if getter() != value:
                setter(value)
        except (RuntimeError, TypeError):
            continue
    if find_focus is not None and focused is not None:
        try:
            if find_focus() is not focused:
                focused.SetFocus()
        except RuntimeError:
            pass


def fit_to_content(window, screen_size=None) -> bool:
    """Grow *window* when its larger text no longer fits; True when it grew.

    Only ever grows, and never past *screen_size* (width, height) when given:
    a dialog sized by hand keeps the size the person gave it."""
    window.Layout()
    needed = window.GetEffectiveMinSize()
    current = window.GetSize()
    width = max(current.GetWidth(), needed.GetWidth())
    height = max(current.GetHeight(), needed.GetHeight())
    if screen_size is not None:
        width = min(width, screen_size[0])
        height = min(height, screen_size[1])
    if (width, height) == (current.GetWidth(), current.GetHeight()):
        return False
    window.SetSize((width, height))
    window.Layout()
    return True
