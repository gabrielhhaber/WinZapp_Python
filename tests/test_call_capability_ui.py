"""Call controls follow bridge capabilities without touching the desktop."""
from itertools import product
from types import SimpleNamespace

import pytest

from ui.conversation_panel.composer import ComposerMixin
from ui.conversation_panel.group_calls import GroupCallJoinPanelMixin

PERSON = "1@s.whatsapp.net"
GROUP = "1@g.us"


class Widget:
    def __init__(self, shown=False):
        self.shown = shown
        self.focused = False
        self.focus_count = self.layouts = self.label_changes = 0
        self.label = ""

    def IsShown(self):
        return self.shown

    def Show(self, shown):
        self.shown = shown

    def HasFocus(self):
        return self.focused

    def SetFocus(self):
        self.focus_count += 1

    def Layout(self):
        self.layouts += 1

    def GetLabel(self):
        return self.label

    def SetLabel(self, label):
        self.label = label
        self.label_changes += 1


class Panel(ComposerMixin, GroupCallJoinPanelMixin):
    def __init__(self, jid=PERSON, *, neonize=True, flags=(False, False, False)):
        self.conversation = {"remoteJid": jid}
        self.conversation_panel = Widget(True)
        self._voice_call_btn = Widget()
        self._video_call_btn = Widget()
        self._group_call_unavailable_btn = Widget()
        self._group_call_join_btn = Widget()
        self.message_field = Widget(True)
        self.layouts = 0
        self.main_window = SimpleNamespace(
            _uses_neonize=lambda: neonize,
            _neonize_calls=flags[0], _neonize_video_calls=flags[1],
            _neonize_group_calls=flags[2],
            _is_self_jid=lambda value: value == "self@s.whatsapp.net",
            _allow_ui_focus_changes=lambda: True,
            i18n=SimpleNamespace(t=lambda key: key),
        )

    def IsShown(self):
        return True

    def Layout(self):
        self.layouts += 1

    def _watch_group_calls(self, _jid):
        raise AssertionError("a capability refresh must reuse the cached call")


@pytest.mark.parametrize("flags", list(product((False, True), repeat=3)))
@pytest.mark.parametrize("jid", [PERSON, GROUP])
def test_neonize_start_buttons_follow_each_capability(flags, jid):
    panel = Panel(jid, flags=flags)
    panel._update_call_start_buttons(jid)
    voice = flags[0] and (jid != GROUP or flags[2])
    assert panel._voice_call_btn.shown == voice
    assert panel._video_call_btn.shown == (voice and flags[1])


@pytest.mark.parametrize("jid", [PERSON, GROUP])
def test_wppconnect_and_custom_api_keep_call_buttons_without_bridge_flags(jid):
    panel = Panel(jid, neonize=False)
    panel._update_call_start_buttons(jid)
    assert panel._voice_call_btn.shown and panel._video_call_btn.shown


@pytest.mark.parametrize("jid", ["", "self@s.whatsapp.net", "1@newsletter", "1@broadcast"])
def test_capabilities_cannot_make_an_uncallable_chat_callable(jid):
    panel = Panel(jid, flags=(True, True, True))
    panel._update_call_start_buttons(jid)
    assert not panel._voice_call_btn.shown and not panel._video_call_btn.shown


@pytest.mark.parametrize("supported", [False, True])
def test_large_group_note_requires_group_call_support(supported):
    panel = Panel(GROUP, flags=(True, True, supported))
    panel._group_call_blocked_jid = GROUP
    panel._update_call_start_buttons(GROUP)
    assert panel._group_call_unavailable_btn.shown == supported
    assert not panel._voice_call_btn.shown and not panel._video_call_btn.shown


@pytest.mark.parametrize("flags", list(product((False, True), repeat=3)))
@pytest.mark.parametrize("video", [False, True])
def test_join_button_requires_the_existing_calls_capabilities(flags, video):
    panel = Panel(GROUP, flags=flags)
    panel._show_ongoing_group_call({"id": "C1", "groupJid": GROUP, "isVideo": video})
    joinable = flags[0] and flags[2] and (not video or flags[1])
    assert panel._group_call_join_btn.shown == joinable
    if joinable:
        assert not panel._voice_call_btn.shown and not panel._video_call_btn.shown
    else:
        # A call this device cannot join (a video call without video support)
        # does not take the start buttons' place: they follow the capabilities.
        assert panel._voice_call_btn.shown == (flags[0] and flags[2])
        assert panel._video_call_btn.shown == (flags[0] and flags[1] and flags[2])


def test_unknown_capabilities_hide_calls_until_a_real_verdict_arrives():
    panel = Panel()
    for attr in ("_neonize_calls", "_neonize_video_calls", "_neonize_group_calls"):
        delattr(panel.main_window, attr)
    panel._update_call_start_buttons(PERSON)
    assert not panel._voice_call_btn.shown and not panel._video_call_btn.shown
    panel.main_window._neonize_calls = True
    panel._update_call_start_buttons(PERSON)
    assert panel._voice_call_btn.shown and not panel._video_call_btn.shown


@pytest.mark.parametrize("control", ["_voice_call_btn", "_video_call_btn", "_group_call_join_btn"])
@pytest.mark.parametrize("visible,active", [(True, True), (False, True), (True, False)])
def test_hiding_a_focused_control_only_repairs_focus_in_the_active_chat(control, visible, active):
    panel = Panel(GROUP, flags=(True, True, True))
    call = {"id": "C1", "groupJid": GROUP, "isVideo": False}
    if control == "_group_call_join_btn":
        panel._show_ongoing_group_call(call)
    else:
        panel._update_call_start_buttons(GROUP)
    getattr(panel, control).focused = True
    panel.conversation_panel.shown = visible
    panel.main_window._allow_ui_focus_changes = lambda: active
    panel.main_window._neonize_calls = False
    panel._show_ongoing_group_call(call if control == "_group_call_join_btn" else None)
    assert panel.message_field.focus_count == int(visible and active)


def test_repeated_capability_refresh_does_not_relayout_or_rename_controls():
    panel = Panel(GROUP, flags=(True, True, True))
    call = {"id": "C1", "groupJid": GROUP, "isVideo": True}
    panel._show_ongoing_group_call(call)
    before = (panel.layouts, panel.conversation_panel.layouts,
              panel._group_call_join_btn.label_changes)
    panel._show_ongoing_group_call(dict(call))
    assert (panel.layouts, panel.conversation_panel.layouts,
            panel._group_call_join_btn.label_changes) == before


def test_join_support_can_arrive_after_an_ongoing_call_was_discovered():
    panel = Panel(GROUP)
    call = {"id": "C1", "groupJid": GROUP, "isVideo": True}
    panel._show_ongoing_group_call(call)
    assert not panel._group_call_join_btn.shown
    panel.main_window._neonize_calls = True
    panel.main_window._neonize_video_calls = True
    panel.main_window._neonize_group_calls = True
    panel._show_ongoing_group_call(panel._ongoing_group_call)
    assert panel._group_call_join_btn.shown


@pytest.mark.parametrize("jid", [PERSON, "2@g.us", ""])
def test_an_old_groups_cached_call_never_shows_join_in_the_current_chat(jid):
    panel = Panel(GROUP, flags=(True, True, True))
    call = {"id": "C1", "groupJid": GROUP, "isVideo": True}
    panel._show_ongoing_group_call(call)
    panel.conversation = {"remoteJid": jid}
    panel._show_ongoing_group_call(panel._ongoing_group_call)
    assert not panel._group_call_join_btn.shown


def test_shutting_down_never_relocates_focus():
    panel = Panel(GROUP, flags=(True, True, True))
    panel._update_call_start_buttons(GROUP)
    panel._voice_call_btn.focused = True
    panel.main_window._shutting_down = True
    panel.main_window._neonize_calls = False
    panel._show_ongoing_group_call(None)
    assert panel.message_field.focus_count == 0
