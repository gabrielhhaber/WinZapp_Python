"""Real presence merge/socket adapters with fake UI, sound, timers and clock."""
from types import SimpleNamespace
import ast
from pathlib import Path
import pytest
import wx

from core.websocket_client import WebSocketClient
from main_window import typing_feedback as feedback
from core import typing_feedback as policy
from tests.test_presence_speech_gating import _Stub, GROUP_JID, PARTICIPANT_JID
from tests.test_typing_feedback import Timer

OTHER = "5511888888888@s.whatsapp.net"


@pytest.fixture
def rig(monkeypatch):
    r = SimpleNamespace(now=0, timers=[], played=[], stopped=[], bindings={})
    def schedule(delay, callback, *args, **kwargs):
        timer = Timer(delay, callback)
        r.timers.append(timer)
        return timer
    monkeypatch.setattr(wx, "CallLater", schedule)
    monkeypatch.setattr(wx, "CallAfter", lambda f, *a, **k: f(*a, **k))
    monkeypatch.setattr(policy.time, "monotonic", lambda: r.now)
    r.window = w = _Stub()
    w.Bind = lambda event, callback: r.bindings.setdefault(event, callback)
    w.get_active_sound_pack = lambda: {"id": "default"}
    w.typing_sound = SimpleNamespace(play=lambda: r.played.append(r.now), stop=lambda: r.stopped.append(r.now), is_playing=False)
    w.settings["speech_content"]["typing_sound"] = True
    def presence(state, person=PARTICIPANT_JID, chat=GROUP_JID, **fields):
        w.on_presence_update(chat, {person: {"lastKnownPresence": state, **fields}})
    r.presence = presence
    return r


@pytest.mark.parametrize(("selected", "sounds", "speech"), [
    ("off", 0, 0), ("speech", 0, 1), ("sound", 1, 0), ("both", 1, 1),
])
def test_mode_controls_the_actual_presence_outputs(rig, selected, sounds, speech):
    policy.save_preferences(rig.window.settings, selected, 3)
    rig.presence("composing")
    assert len(rig.played) == sounds
    assert len(rig.window.speak_output.outputs) == speech


def test_default_users_never_create_a_sound_controller_or_bind_handlers(rig):
    rig.window.settings["speech_content"].pop("typing_sound")
    rig.presence("composing")
    assert not hasattr(rig.window, "_typing_sound_cadence")
    assert not rig.played and not rig.bindings
    assert len(rig.window.speak_output.outputs) == 1


def test_sound_repeats_but_speech_stays_one_per_transition(rig):
    rig.presence("composing")
    controller = rig.window._typing_sound_cadence
    first = controller.timer
    rig.now = 2
    rig.presence("composing")
    assert controller.timer is first
    rig.now = 3
    first.callback()
    assert rig.played == [0, 3]
    assert len(rig.window.speak_output.outputs) == 1


@pytest.mark.parametrize("ending", ["paused", "recording", "available", "unavailable"])
def test_non_typing_state_stops_the_sound_immediately(rig, ending):
    rig.presence("composing")
    c = rig.window._typing_sound_cadence
    old = c.timer
    rig.presence(ending)
    assert c.timer is None and old.stopped
    assert rig.stopped
    old.callback()
    assert rig.played == [0]


@pytest.mark.parametrize("gate", ["hidden", "minimized", "inactive", "shutdown", "detail", "panel"])
def test_no_sound_in_hidden_or_inactive_context(rig, gate):
    w = rig.window
    if gate == "hidden":
        w._window_hidden = True
    elif gate == "minimized":
        w.IsIconized = lambda: True
    elif gate == "inactive":
        w.IsActive = lambda: False
    elif gate == "shutdown":
        w._shutting_down = True
    elif gate == "detail":
        w.conversations_panel.conversation_panel = SimpleNamespace(IsShown=lambda: False)
    else:
        w.conversations_panel.IsShown = lambda: False
    rig.presence("composing")
    assert not rig.played


@pytest.mark.parametrize("changed", ["epoch", "visit", "setting", "pack disabled", "sound replaced"])
def test_queued_timer_rechecks_all_playback_authority(rig, changed):
    rig.presence("composing")
    w = rig.window
    old = w._typing_sound_cadence.timer
    if changed == "epoch":
        w._contact_presence_epoch = 1
    elif changed == "visit":
        w.conversations_panel._contact_presence_visit = 1
    elif changed == "setting":
        w.settings["speech_content"]["typing_sound"] = False
    elif changed == "pack disabled":
        w.settings["sound_events"] = {"default": {"typing": {"enabled": False}}}
    else:
        w.typing_sound = SimpleNamespace(stop=lambda: None)
    old.callback()
    assert rig.played == [0]
    assert w._typing_sound_cadence.timer is None


def test_other_chats_do_not_start_or_cancel_the_open_chat_sound(rig):
    rig.presence("composing", chat="other@g.us")
    assert not rig.played
    rig.presence("composing")
    c = rig.window._typing_sound_cadence
    old = c.timer
    rig.presence("paused", chat="other@g.us")
    assert c.timer is old
    assert rig.played == [0]


def test_group_message_clears_only_its_author_and_own_messages_do_not_clear(rig):
    rig.presence("composing")
    rig.presence("composing", person=OTHER)
    w = rig.window
    c = w._typing_sound_cadence
    feedback.dismiss_typing_message(w, GROUP_JID, {"key": {"participant": PARTICIPANT_JID, "fromMe": True}})
    assert len(c.people) == 2
    feedback.dismiss_typing_message(w, GROUP_JID, {"key": {"participant": PARTICIPANT_JID}})
    assert list(c.people) == [OTHER]
    feedback.dismiss_typing_message(w, GROUP_JID, {"key": {"participant": OTHER}})
    assert c.timer is None


def test_phone_and_lid_aliases_share_one_cadence_and_message_dismissal(rig):
    w = rig.window
    lid = "123456@lid"
    w._lid_to_phone[lid] = PARTICIPANT_JID
    w._phone_to_lid[PARTICIPANT_JID] = lid
    w.conversations_panel.conversation = {"remoteJid": PARTICIPANT_JID}
    rig.presence("composing", chat=lid, person=lid)
    rig.presence("composing", chat=PARTICIPANT_JID)
    assert rig.played == [0]
    feedback.dismiss_typing_message(w, PARTICIPANT_JID, {"key": {"remoteJid": lid}})
    assert w._typing_sound_cadence.timer is None


@pytest.mark.parametrize("event_name", ["activate", "hide", "destroy"])
def test_ui_events_stop_existing_sound_and_stale_callbacks(rig, event_name):
    rig.presence("composing")
    w = rig.window
    c = w._typing_sound_cadence
    old = c.timer
    event = SimpleNamespace(GetActive=lambda: False, IsShown=lambda: False,
                            GetEventObject=lambda: w, Skip=lambda: None)
    kind = {"activate": wx.EVT_ACTIVATE, "hide": wx.EVT_SHOW, "destroy": wx.EVT_WINDOW_DESTROY}[event_name]
    rig.bindings[kind](event)
    old.callback()
    assert rig.played == [0] and c.timer is None


def test_long_custom_sound_is_not_restarted_or_overlapped(rig):
    rig.presence("composing")
    rig.window.typing_sound.is_playing = True
    rig.now = 3
    rig.window._typing_sound_cadence.timer.callback()
    assert rig.played == [0]


@pytest.mark.parametrize("engine", ["wppconnect", "neonize", "presence.update"])
@pytest.mark.parametrize("group", [False, True])
def test_both_engines_real_formatters_reach_the_shared_sound_policy(rig, engine, group):
    w = rig.window
    chat = GROUP_JID if group else PARTICIPANT_JID
    w.conversations_panel.conversation = {"remoteJid": chat}
    sock = SimpleNamespace(main_window=w, _belongs_to_this_session=lambda info: True)
    if engine == "presence.update":
        WebSocketClient.on_presence_update(sock, {"data": {"id": chat, "presences": {
            PARTICIPANT_JID: {"lastKnownPresence": "composing"}}}})
    else:
        if engine == "neonize":
            path = Path(__file__).resolve().parents[1] / "neonize_bridge/presence.py"
            # The bridge has a separate aiohttp/neonize environment. Execute
            # its actual dependency-free formatter, without booting its server
            # or installing bridge dependencies in the shared client venv.
            tree = ast.parse(path.read_text(encoding="utf-8"))
            function = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                            and n.name == "activity_event")
            scope = {}
            exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), "exec"), scope)
            payload = scope["activity_event"](chat, PARTICIPANT_JID, "composing", 0, group)
        else:
            payload = {"id": chat, "state": "typing", "isGroup": group,
                       "participants": [{"id": PARTICIPANT_JID, "state": "typing"}]}
        WebSocketClient.on_wpp_presence_changed(sock, payload)
    assert rig.played == [0]
    assert len(w.speak_output.outputs) == 1


def test_older_presence_does_not_restart_sound_after_message(rig):
    rig.presence("composing", eventTimestamp=20)
    feedback.dismiss_typing_message(rig.window, GROUP_JID,
                                   {"key": {"participant": PARTICIPANT_JID}})
    rig.presence("composing", eventTimestamp=10)
    assert rig.played == [0]
    assert rig.window._typing_sound_cadence.timer is None
