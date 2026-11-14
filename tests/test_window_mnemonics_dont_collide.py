"""No two controls on one screen share an Alt+letter, in any locale.

A shared letter only moves the focus between the two controls, one press at a
time, and no screen reader says why. Reported in the call participants dialog
("&Atualizar" and "&Adicionar participantes", Alt+A twice in pt-BR, pt-PT,
es-ES and it, Alt+K twice in tr-TR); the same scan then found 35 more across
the account manager, the error window, the Help menu, the status tab, the new
group, poll, emoji, communities and connection dialogs.

The screens are read from the sources (tests/window_mnemonics.py), never
listed by hand: a window added tomorrow is checked the day it lands. Which
letter a locale gives a label is still its own choice.
"""

import pytest

from tests import window_mnemonics as wm
from tests.locales import registered_locale_codes
from tests.mnemonics import load_strings, mnemonic

LOCALES = list(registered_locale_codes())


@pytest.fixture(scope="module")
def screens():
    return wm.all_screens(frozenset(load_strings("en-US")))


def test_the_scan_sees_the_windows_it_has_to(screens):
    """A parser change that silently found nothing would pass the check below
    for every locale. These screens are known to carry several labels."""
    found = {(where.split("::", 1)[1], screen) for where, screen, _slots in screens}
    for expected in [("CallParticipantsDialog", "panel"), ("ChannelsPanel", "self"),
                     ("ChannelAdminsDialog", "self"),
                     ("WindowChromeMixin._build_menubar", "menu:help_menu"),
                     ("WindowChromeMixin._build_menubar", "menu:menubar"),
                     ("StatusPanel.init_UI", "self._post_panel")]:
        assert expected in found, expected
    channels = next(slots for where, screen, slots in screens
                    if where.endswith("::ChannelsPanel") and screen == "self")
    keys = {k for slot in channels for k in slot}
    # the buttons the panel's own helper creates from a bare key
    assert {"channels_open", "channels_create", "channels_mine", "channels_category"} <= keys


def test_an_alternative_label_of_one_control_is_not_a_collision():
    import ast
    call = ast.parse('wx.Button(self, label=i18n.t("a" if x else "b"))').body[0].value
    assert wm._slots(call.keywords[0].value) == [frozenset({"a", "b"})]
    call = ast.parse('wx.Button(self, label=i18n.t("a") if x else i18n.t("b"))').body[0].value
    assert wm._slots(call.keywords[0].value) == [frozenset({"a", "b"})]
    strings = {"a": "&Play", "b": "&Pause", "c": "&Copy", "d": "&Paste"}
    assert wm.collisions({frozenset({"a", "b"}), frozenset({"c"})}, strings, mnemonic) == {}
    clash = wm.collisions({frozenset({"a", "b"}), frozenset({"d"})}, strings, mnemonic)
    assert list(clash) == ["p"] and len(clash["p"]) == 2


def test_the_event_dialog_labels_are_seen(screens):
    """EventDialog builds its fields with a `_text(panel, sizer, t("key"))`
    helper: the scan has to read the `t` alias for them to be checked."""
    slots = next(slots for where, screen, slots in screens
                 if where.endswith("::EventDialog") and screen == "panel")
    keys = {k for slot in slots for k in slot}
    assert {"event_name_label", "event_location_label", "event_call_label",
            "event_extra_guests_label"} <= keys


@pytest.mark.parametrize("code", LOCALES)
def test_event_details_buttons_do_not_share_a_letter(code):
    """The answer buttons are built in a loop over core.events.RESPONSE_KEYS,
    so their keys are not literals the scan can read (Romanian "Po&ate" and
    "&Anulează evenimentul" were both Alt+A)."""
    from core import events as ev
    strings = load_strings(code)
    keys = [ev.RESPONSE_KEYS[r] for r in ev.RESPONSES] + ["event_menu_edit", "event_menu_cancel", "close"]
    slots = [frozenset({k}) for k in keys]
    found = wm.collisions(slots, strings, mnemonic)
    assert not found, {letter: [sorted(s) for s in group] for letter, group in found.items()}


@pytest.mark.parametrize("code", LOCALES)
def test_no_screen_shares_a_letter(code, screens):
    strings = load_strings(code)
    found = []
    for where, screen, slots in screens:
        for letter, group in wm.collisions(slots, strings, mnemonic).items():
            found.append(f"{where} [{screen}] Alt+{letter}: "
                         + ", ".join("/".join(sorted(slot)) for slot in group))
    assert not found, "\n".join(found)
