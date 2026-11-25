"""Reactions use a configurable sound without opening windows or audio devices."""

import ast
import json
import logging
import queue
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import wx

from core.notification_manager import (
    REACTION_SOUND_EVENT, NotificationManager, reaction_notifications_enabled,
    reaction_silenced_now,
)
from core.sound_system import SOUND_EVENTS, Sound, resolve_sound_event_path
from main import MainWindow
from tests.test_archived_chat_sound_tts import _StubMainWindow
from tests.test_background_notification_not_duplicated import _Stub, _Toaster

JID = "test@s.whatsapp.net"


def reaction(*, from_me=False, target_from_me=True, emoji="👍"):
    return {"key": {"fromMe": from_me, "remoteJid": JID}, "message": {
        "reactionMessage": {"text": emoji, "key": {"id": "target", "fromMe": target_from_me}}
    }}


@pytest.fixture(autouse=True)
def no_windows(monkeypatch):
    monkeypatch.setattr("wx.CallAfter", lambda fn, *a, **kw: fn(*a, **kw))
    monkeypatch.setattr("core.quiet_hours.is_quiet_hours_active", lambda: False)


@pytest.mark.parametrize("current", [True, False])
def test_foreground_reaction_uses_only_its_own_sound(current):
    window = _StubMainWindow(open_jid=JID if current else "other@s.whatsapp.net")
    window._maybe_notify_reaction(JID, reaction())
    assert window.reaction_received_sound.play_count == 1
    assert window.message_current_sound.play_count == 0
    assert window.message_foreground_sound.play_count == 0
    assert len(window.spoken) == 1


@pytest.mark.parametrize("locked", [False, True])
@pytest.mark.parametrize("view", ["open", "other_chat", "hidden_detail", "hidden_panel", "background"])
def test_reaction_visibility_including_locked_chat(locked, view):
    window = _StubMainWindow(
        open_jid="other@s.whatsapp.net" if view == "other_chat" else JID,
        locked_jids=[JID] if locked else [], window_active=view != "background",
    )
    panel = window.conversations_panel
    panel.IsShown = lambda: view != "hidden_panel"
    panel.conversation_panel = SimpleNamespace(IsShown=lambda: view != "hidden_detail")
    window.notification_manager = Mock()
    window._maybe_notify_reaction(JID, reaction())
    if view == "background":
        assert window.reaction_received_sound.play_count == 0
        assert window.spoken == []
        assert window.notification_manager.send.call_count == int(not locked)
    else:
        should_play = not locked or view == "open"
        assert window.reaction_received_sound.play_count == int(should_play)
        assert len(window.spoken) == int(should_play)
        window.notification_manager.send.assert_not_called()
    assert window.message_current_sound.play_count == 0
    assert window.message_foreground_sound.play_count == 0


@pytest.mark.parametrize("locked", [False, True])
@pytest.mark.parametrize("keep_silent", [True, False])
@pytest.mark.parametrize("current", [True, False])
def test_muted_reaction_only_plays_when_open_and_user_allows_it(locked, keep_silent, current):
    window = _StubMainWindow(
        open_jid=JID if current else "other@s.whatsapp.net", muted_jids=[JID],
        locked_jids=[JID] if locked else [],
    )
    window.settings["general"]["keep_muted_chats_silent_when_open"] = keep_silent
    window._maybe_notify_reaction(JID, reaction())
    assert window.reaction_received_sound.play_count == int(current and not keep_silent)


def test_toast_failure_does_not_duplicate_the_reaction_sound():
    toaster = _Toaster(fail_show=True)
    stub = manager(toaster)
    stub._dispatch("Name", "Reaction", JID, sound_event="reaction_received")
    stub.main_window.reaction_received_sound.play.assert_called_once_with()
    stub.main_window.play_background_notification_sound.assert_not_called()
    assert len(stub.main_window.spoken) == 1


@pytest.mark.parametrize("payload", [reaction(from_me=True), reaction(target_from_me=False), reaction(emoji="")])
def test_own_other_message_and_removed_reactions_are_silent(payload):
    window = _StubMainWindow(open_jid=JID)
    window._maybe_notify_reaction(JID, payload)
    assert window.reaction_received_sound.play_count == 0
    assert window.spoken == []


@pytest.mark.parametrize("level", ["full", "name", "sound"])
def test_background_reaction_keeps_sound_identity_and_notification_content(level):
    window = _StubMainWindow(window_active=False)
    window.settings["general"]["notification_content"] = level
    window.notification_manager = SimpleNamespace(_queue=queue.Queue())
    window.notification_manager.send = lambda *a, **kw: NotificationManager.send(window.notification_manager, *a, **kw)
    window.notification_manager.send_sound_only = lambda *a, **kw: NotificationManager.send_sound_only(window.notification_manager, *a, **kw)
    window._maybe_notify_reaction(JID, reaction())
    item = window.notification_manager._queue.get_nowait()
    assert item[-1] == "reaction_received"
    assert item[2] == JID
    if level == "sound":
        assert item[:2] == (None, None)
    elif level == "name":
        assert item[1] == window.i18n.t("notif_hidden_reaction")
    else:
        assert "Mensagem original" in item[1]


@pytest.mark.parametrize("setting", ["notifications_enabled", "show_tray_icon"])
def test_background_reaction_respects_notification_switches(setting):
    window = _StubMainWindow(window_active=False)
    window.settings["general"][setting] = False
    window.notification_manager = Mock()
    window._maybe_notify_reaction(JID, reaction())
    window.notification_manager.send.assert_not_called()
    window.notification_manager.send_sound_only.assert_not_called()


@pytest.mark.parametrize("kind", ["muted", "archived", "locked"])
def test_background_reaction_respects_chat_silence(kind):
    window = _StubMainWindow(window_active=False, **{f"{kind}_jids": [JID]})
    window.notification_manager = Mock()
    window._maybe_notify_reaction(JID, reaction())
    window.notification_manager.send.assert_not_called()
    window.notification_manager.send_sound_only.assert_not_called()


def manager(toaster=None):
    stub = _Stub(toaster)
    stub.main_window.reaction_received_sound = Mock()
    stub.main_window.play_background_notification_sound = Mock()
    stub._play_sound = lambda *a: NotificationManager._play_sound(stub, *a)
    return stub


@pytest.mark.parametrize("sound_only", [True, False])
@pytest.mark.parametrize("has_toaster", [True, False])
def test_reaction_dispatch_plays_once_and_never_uses_message_tone(sound_only, has_toaster):
    stub = manager(_Toaster() if has_toaster else None)
    stub._dispatch(None if sound_only else "Name", None if sound_only else "Reaction", JID,
                   sound_event="reaction_received")
    stub.main_window.reaction_received_sound.play.assert_called_once_with()
    stub.main_window.play_background_notification_sound.assert_not_called()


@pytest.mark.parametrize("sound_only", [True, False])
def test_do_not_disturb_suppresses_reaction_sound_banner_and_speech(monkeypatch, sound_only):
    monkeypatch.setattr("core.quiet_hours.is_quiet_hours_active", lambda: True)
    toaster = _Toaster()
    stub = manager(toaster)
    stub._dispatch(None if sound_only else "Name", "Reaction", JID, sound_event="reaction_received")
    stub.main_window.reaction_received_sound.play.assert_not_called()
    assert toaster.shown == []
    assert stub.main_window.spoken == []


@pytest.mark.parametrize("check", ["is_chat_locked", "is_chat_archived", "is_chat_muted"])
@pytest.mark.parametrize("sound_only", [True, False])
def test_chat_silenced_while_queued_stays_silent(check, sound_only):
    toaster = _Toaster()
    stub = manager(toaster)
    setattr(stub.main_window, check, lambda jid: True)
    stub._dispatch(None if sound_only else "Name", "Reaction", JID, sound_event="reaction_received")
    stub.main_window.reaction_received_sound.play.assert_not_called()
    assert toaster.shown == []
    assert stub.main_window.spoken == []


def test_normal_message_keeps_its_conversation_tone():
    stub = manager()
    stub._play_sound(JID)
    stub.main_window.play_background_notification_sound.assert_called_once_with(JID)
    stub.main_window.reaction_received_sound.play.assert_not_called()


def test_worker_preserves_reaction_sound_identity_without_a_background_thread():
    stub = manager(_Toaster())
    stub._setup_toaster = lambda: None
    stub._coalesce_pending = lambda item: (item, 0)
    NotificationManager.send(stub, "Name", "Reaction", JID, sound_event="reaction_received")
    stub._queue.put(None)
    NotificationManager._worker_loop(stub)
    stub.main_window.reaction_received_sound.play.assert_called_once_with()
    stub.main_window.play_background_notification_sound.assert_not_called()


def test_coalesced_notification_retains_latest_sound_identity():
    stub = manager()
    stub._COALESCE_SETTLE_SECONDS = 0
    NotificationManager.send_sound_only(stub, JID)
    first = stub._queue.get_nowait()
    NotificationManager.send_sound_only(stub, JID, sound_event="reaction_received")
    newest, dropped = NotificationManager._coalesce_pending(stub, first)
    assert dropped == 1
    assert newest[-1] == "reaction_received"
    NotificationManager._dispatch(stub, *newest[:4], sound_event=newest[-1])
    stub.main_window.reaction_received_sound.play.assert_called_once_with()


def test_load_sounds_binds_reaction_event_to_the_active_pack(monkeypatch):
    folder = Path(__file__).resolve().parents[1] / "client/sounds/default"
    manifest = json.loads((folder / "default.pack.json").read_text(encoding="utf-8"))
    default = {"id": "default", "dir": str(folder), **manifest}
    window = SimpleNamespace(
        get_active_sound_pack=lambda: {"id": "older_pack", "events": {}},
        _default_sound_pack=default, sound_system=object(), settings={"sound_events": {}},
    )
    loaded = Mock(side_effect=lambda system, path, **kw: SimpleNamespace(path=path, **kw))
    monkeypatch.setattr("main_window.settings.load_sound", loaded)
    MainWindow.load_sounds(window)
    assert window.reaction_received_sound.event_key == "reaction_received"
    assert window.reaction_received_sound.pack_id == "older_pack"
    assert Path(window.reaction_received_sound.path).name == "reaction_received.ogg"


@pytest.mark.parametrize("enabled", [True, False])
def test_reaction_event_enabled_setting_gates_playback_without_audio(monkeypatch, enabled):
    class SilentSound(Sound):
        def __init__(self):
            self.event_key, self.pack_id = "reaction_received", "default"
            self.sound_system = SimpleNamespace(_effects_device=None, main_window=SimpleNamespace(
                settings={"sound_events": {"default": {"reaction_received": {"enabled": enabled}}}}))

        def __del__(self):
            pass

    played = Mock()
    monkeypatch.setattr(Sound.__bases__[0], "stop", lambda self: None)
    monkeypatch.setattr(Sound.__bases__[0], "play", played)
    sound = SilentSound()
    sound.play()
    assert played.call_count == int(enabled)


def test_bundled_reaction_sound_falls_back_from_old_pack_and_accepts_custom_file(tmp_path):
    assert (REACTION_SOUND_EVENT, "reaction_received.ogg") in SOUND_EVENTS
    folder = Path(__file__).resolve().parents[1] / "client/sounds/default"
    manifest = json.loads((folder / "default.pack.json").read_text(encoding="utf-8"))
    default = {"dir": str(folder), **manifest}
    resolved = resolve_sound_event_path({"events": {}}, default, "reaction_received")
    assert Path(resolved).read_bytes().startswith(b"OggS")
    custom = tmp_path / "custom.mp3"
    custom.write_bytes(b"custom")
    assert resolve_sound_event_path(None, default, "reaction_received", str(custom)) == str(custom)


@pytest.fixture
def mac_dispatch():
    # Execute the real function with framework fakes. Importing the whole Mac
    # module would require PyObjC; no Apple or desktop APIs are invoked here.
    path = Path(__file__).resolve().parents[1] / "macos/winzapp_mac/notify_mac.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_dispatch")
    center, framework = Mock(), Mock()
    namespace = {"wx": wx, "time": time, "logging": logging, "json": json,
                 "_ensure_center": lambda self: center, "_un": lambda: framework,
                 "CAT_PLAIN": "plain", "CAT_INTERACTIVE": "interactive"}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), "exec"), namespace)
    return namespace["_dispatch"], center


@pytest.mark.parametrize("sound_only", [True, False])
def test_mac_dispatch_accepts_queued_sound_identity(mac_dispatch, sound_only):
    dispatch, center = mac_dispatch
    stub = manager()
    dispatch(stub, None if sound_only else "Name", "Reaction", JID, sound_event="reaction_received")
    stub.main_window.reaction_received_sound.play.assert_called_once_with()
    stub.main_window.play_background_notification_sound.assert_not_called()
    assert center.addNotificationRequest_withCompletionHandler_.call_count == int(not sound_only)


def test_mac_focus_keeps_background_banner_but_silences_reaction(monkeypatch, mac_dispatch):
    monkeypatch.setattr("core.quiet_hours.is_quiet_hours_active", lambda: True)
    dispatch, center = mac_dispatch
    stub = manager()
    dispatch(stub, "Name", "Reaction", JID, sound_event="reaction_received")
    stub.main_window.reaction_received_sound.play.assert_not_called()
    center.addNotificationRequest_withCompletionHandler_.assert_called_once()


def test_mac_late_locked_reaction_shows_and_plays_nothing(mac_dispatch):
    dispatch, center = mac_dispatch
    stub = manager()
    stub.main_window.is_chat_locked = lambda jid: True
    dispatch(stub, "Name", "Reaction", JID, sound_event="reaction_received")
    stub.main_window.reaction_received_sound.play.assert_not_called()
    center.addNotificationRequest_withCompletionHandler_.assert_not_called()


@pytest.mark.parametrize("check", ["is_chat_locked", "is_chat_muted", "is_chat_archived"])
def test_reaction_is_silenced_by_each_late_chat_state(check):
    window = SimpleNamespace(**{check: lambda jid: jid == JID})
    assert reaction_silenced_now(window, JID)
    assert not reaction_silenced_now(window, "other@s.whatsapp.net")


def test_reaction_silence_check_tolerates_a_window_without_predicates():
    assert not reaction_silenced_now(SimpleNamespace(), JID)
    assert not reaction_silenced_now(None, JID)


# ── Settings > Notifications > "Notify reactions to your messages" (#922) ──

def test_reaction_notifications_are_on_by_default():
    assert reaction_notifications_enabled({}) is True
    assert reaction_notifications_enabled(None) is True
    assert reaction_notifications_enabled({"general": {"notify_reactions": True}}) is True
    assert reaction_notifications_enabled({"general": {"notify_reactions": False}}) is False


@pytest.mark.parametrize("active", [True, False])
def test_turned_off_a_reaction_is_neither_spoken_played_nor_notified(active):
    window = _StubMainWindow(open_jid=JID, window_active=active)
    window.settings["general"]["notify_reactions"] = False
    window.notification_manager = Mock()
    window._maybe_notify_reaction(JID, reaction())
    assert window.reaction_received_sound.play_count == 0
    assert window.spoken == []
    window.notification_manager.send.assert_not_called()
    window.notification_manager.send_sound_only.assert_not_called()


def test_turned_on_a_background_reaction_is_still_notified():
    window = _StubMainWindow(window_active=False)
    window.settings["general"]["notify_reactions"] = True
    window.notification_manager = Mock()
    window._maybe_notify_reaction(JID, reaction())
    assert window.notification_manager.send.call_count == 1
