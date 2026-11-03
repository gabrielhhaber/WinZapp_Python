"""Background, account, ordering, legacy and stress scenarios without native IO."""
import ast
import hashlib
import json
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest
import wx

from core.websocket_client import WebSocketClient
from main_window import chat_events, typing_feedback as feedback
from tests.test_presence_speech_gating import _Stub
from tests.test_typing_feedback_integration import rig, GROUP_JID, PARTICIPANT_JID, OTHER  # noqa: F401


def apply_gate(window, gate):
    if gate == "hidden flag":
        window._window_hidden = True
    elif gate == "not shown":
        window.IsShown = lambda: False
    elif gate == "minimized":
        window.IsIconized = lambda: True
    elif gate == "inactive":
        window.IsActive = lambda: False
    elif gate == "offline":
        window._wa_connected = False
    elif gate == "shutdown":
        window._shutting_down = True
    elif gate == "detail hidden":
        window.conversations_panel.conversation_panel = SimpleNamespace(IsShown=lambda: False)
    elif gate == "panel hidden":
        window.conversations_panel.IsShown = lambda: False
    elif gate == "destroyed":
        def gone():
            raise RuntimeError("destroyed window")
        window.IsShown = gone


@pytest.mark.parametrize("gate", ["hidden flag", "not shown", "minimized", "inactive", "offline",
                                  "shutdown", "detail hidden", "panel hidden", "destroyed"])
@pytest.mark.parametrize("moment", ["before start", "queued repeat", "incoming update"])
def test_background_gate_matrix_has_no_new_playback(rig, gate, moment):
    w = rig.window
    old = None
    if moment != "before start":
        rig.presence("composing")
        old = w._typing_sound_cadence.timer
    apply_gate(w, gate)
    if moment == "queued repeat":
        rig.now = 3
        old.callback()
    else:
        # Shutdown rejects the entire input early; it does not run deferred
        # callbacks. Its dedicated lifecycle tests establish immediate Stop.
        if gate == "shutdown" and old is not None:
            feedback.stop_typing_sound(w)
        if gate == "destroyed":
            # MainWindow's legacy speech path also calls IsShown(). Test the
            # new sound gate directly rather than changing legacy behavior.
            feedback.observe_typing_batch(w, GROUP_JID, {PARTICIPANT_JID: "composing"})
        else:
            rig.presence("composing")
    assert rig.played == ([] if old is None else [0])
    controller = getattr(w, "_typing_sound_cadence", None)
    assert controller is None or controller.timer is None


def test_focus_return_never_revives_a_cancelled_callback(rig):
    w = rig.window
    rig.presence("composing")
    old = w._typing_sound_cadence.timer
    w.IsActive = lambda: False
    rig.bindings[wx.EVT_ACTIVATE](SimpleNamespace(GetActive=lambda: False, Skip=lambda: None))
    w.IsActive = lambda: True
    rig.now = 1
    old.callback()
    assert rig.played == [0]
    assert w._typing_sound_cadence.timer is None
    rig.presence("composing")  # A new foreground update can start a new cadence.
    assert rig.played == [0, 1]
    assert len(w.speak_output.outputs) == 1  # No extra legacy speech transition.


def test_switching_accounts_keeps_each_sound_and_callback_owned_by_its_window(rig):
    a = rig.window
    rig.presence("composing")
    stale = a._typing_sound_cadence.timer
    a.IsActive = lambda: False
    rig.bindings[wx.EVT_ACTIVATE](SimpleNamespace(GetActive=lambda: False, Skip=lambda: None))
    b = _Stub()
    b.settings["speech_content"]["typing_sound"] = True
    b.get_active_sound_pack = lambda: {"id": "default"}
    b.Bind = lambda *args: None
    played_b = []
    b.typing_sound = SimpleNamespace(is_playing=False, play=lambda: played_b.append(rig.now), stop=lambda: None)
    b.on_presence_update(GROUP_JID, {PARTICIPANT_JID: {"lastKnownPresence": "composing"}})
    active = b._typing_sound_cadence.timer
    stale.callback()
    assert b._typing_sound_cadence.timer is active and not active.stopped
    rig.now = 3
    active.callback()
    assert rig.played == [0] and played_b == [0, 3]


@pytest.mark.parametrize("engine", ["wppconnect", "presence.update"])
@pytest.mark.parametrize("rejection", ["generation", "session"])
def test_queued_events_from_old_connection_or_wrong_session_never_start_sound(rig, monkeypatch, engine, rejection):
    queue = []
    monkeypatch.setattr(wx, "CallAfter", lambda f, *a, **k: queue.append((f, a, k)))
    sock = SimpleNamespace(main_window=rig.window, _presence_delivery_generation=0,
                           _belongs_to_this_session=lambda info: rejection != "session")
    if engine == "wppconnect":
        WebSocketClient.on_wpp_presence_changed(sock, {"id": GROUP_JID, "isGroup": True,
            "participants": [{"id": PARTICIPANT_JID, "state": "typing"}]})
    else:
        WebSocketClient.on_presence_update(sock, {"data": {"id": GROUP_JID,
            "presences": {PARTICIPANT_JID: {"lastKnownPresence": "composing"}}}})
    sock._presence_delivery_generation += 1
    for function, args, kwargs in queue:
        function(*args, **kwargs)
    assert not rig.played
    assert not rig.window.speak_output.outputs
    assert not rig.window._presence_cache


def test_sound_replacement_does_not_multiply_handlers_or_let_old_callbacks_stop_the_new_owner(rig):
    w = rig.window
    bind_calls = []
    w.Bind = lambda kind, handler: (bind_calls.append(kind), rig.bindings.setdefault(kind, handler))
    stale = []
    latest = []
    for index in range(200):
        latest = []
        w.typing_sound = SimpleNamespace(is_playing=False, play=lambda result=latest: result.append("play"),
                                         stop=lambda result=latest: result.append("stop"))
        rig.presence("composing")
        stale.append(w._typing_sound_cadence.timer)
    current = w._typing_sound_cadence.timer
    for timer in stale[:-1]:
        assert timer.stopped
        timer.callback()
    assert w._typing_sound_cadence.timer is current and not current.stopped
    assert len(bind_calls) == 4
    assert latest.count("play") == 1
    rig.bindings[wx.EVT_ACTIVATE](SimpleNamespace(GetActive=lambda: False, Skip=lambda: None))
    assert current.stopped and w._typing_sound_cadence.timer is None
    assert latest.count("stop") == 2  # Initial reset and this owner's deactivation.


def test_large_group_uses_one_cue_cadence_and_expires_without_any_final_push(rig):
    w = rig.window
    w.settings["speech_content"]["announce_typing"] = False
    people = {f"5511{index:09d}@s.whatsapp.net": {"lastKnownPresence": "composing"} for index in range(2048)}
    w.on_presence_update(GROUP_JID, people)
    controller = w._typing_sound_cadence
    assert len(controller.people) == 2048
    assert rig.played == [0]
    assert len(rig.timers) == 2049  # Existing participant expiries + ONE sound timer.
    rig.now = 10
    controller.timer.callback()
    assert rig.played == [0] and controller.timer is None


@pytest.mark.parametrize("failure", ["state", "play", "stop"])
def test_device_failure_does_not_escape_or_interrupt_existing_screen_reader_speech(rig, failure):
    class BrokenSound:
        @property
        def is_playing(self):
            if failure == "state":
                raise RuntimeError("invalid handle")
            return False

        def play(self):
            if failure == "play":
                raise RuntimeError("device unavailable")

        def stop(self):
            if failure == "stop":
                raise RuntimeError("device already gone")
    rig.window.typing_sound = BrokenSound()
    rig.presence("composing")
    controller = rig.window._typing_sound_cadence
    if failure == "stop":
        rig.presence("paused")
    assert controller.timer is None
    assert rig.window.speak_output.outputs == ["Fulano está digitando..."]


@pytest.mark.parametrize("spoken", [True, False])
@pytest.mark.parametrize("recording", [True, False])
@pytest.mark.parametrize("group", [True, False])
@pytest.mark.parametrize("foreground", [True, False])
def test_disabled_new_sound_matches_the_unmodified_public_source_behavior(rig, spoken, recording, group, foreground):
    source = Path(__file__).resolve().parents[1]
    fixture = source / "tests/fixtures/typing_presence_baseline.py"
    provenance = json.loads(fixture.with_suffix(".json").read_text(encoding="utf-8"))
    assert provenance["source_commit"] == "4118f4f4e2f98cc722c91c8452c1722c7b94c664"
    assert hashlib.sha256(fixture.read_bytes()).hexdigest() == provenance["fixture_sha256"]
    original = fixture.read_text(encoding="utf-8")
    tree = ast.parse(original)
    method = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "on_presence_update")
    namespace = dict(vars(chat_events))
    exec(compile(ast.Module(body=[method], type_ignores=[]), "verified-public-source:on_presence_update", "exec"), namespace)
    old, new = _Stub(), _Stub()
    old.on_presence_update = MethodType(namespace["on_presence_update"], old)
    chat = GROUP_JID if group else PARTICIPANT_JID
    for window in (old, new):
        window.settings["speech_content"].update(announce_typing=spoken, announce_recording=recording)
        window.conversations_panel.conversation = {"remoteJid": chat}
        window.IsActive = lambda: foreground
        for state in ("composing", "composing", "recording", "paused", "composing", "unavailable"):
            window.on_presence_update(chat, {PARTICIPANT_JID: {"lastKnownPresence": state}})
    assert old.speak_output.outputs == new.speak_output.outputs
    assert old.refreshed_rows == new.refreshed_rows
    assert old._composing_chats == new._composing_chats
    assert old._presence_cache == new._presence_cache
    assert not hasattr(new, "_typing_sound_cadence")
