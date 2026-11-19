"""Edited messages: sounds, speech and notifications (issue #824). No window,
no audio device: the rules are plain functions and the mixin runs on a stub."""

import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core import edit_alerts
from core.sound_system import (
    SOUND_EVENTS, SOUND_EVENT_FALLBACKS, resolve_sound_event_path, sound_event_enabled,
    sound_event_override_path,
)
from main import MainWindow
from tests.test_archived_chat_sound_tts import _StubMainWindow

JID = "5511912345678@s.whatsapp.net"


# ── Sound events ────────────────────────────────────────────────────────────

def test_the_three_edit_events_are_sound_events():
    keys = [key for key, _file in SOUND_EVENTS]
    for scope in edit_alerts.SCOPES:
        assert edit_alerts.SOUND_EVENTS[scope] in keys
        assert edit_alerts.SOUND_EVENTS[scope] in SOUND_EVENT_FALLBACKS


def test_only_the_saved_edit_sound_is_on_by_default():
    assert sound_event_enabled(None, "message_edit_saved") is True
    assert sound_event_enabled({}, "message_edited_foreground") is False
    assert sound_event_enabled({"path": ""}, "message_edited_background") is False
    assert sound_event_enabled({"enabled": True}, "message_edited_background") is True
    assert sound_event_enabled({"enabled": False}, "message_edit_saved") is False
    assert sound_event_enabled(None, "message_sent") is True


def test_an_edit_event_inherits_its_parents_own_file():
    cfg = {"message_sent": {"path": "C:/mine/sent.ogg"}}
    assert sound_event_override_path(cfg, "message_edit_saved") == "C:/mine/sent.ogg"
    cfg["message_edit_saved"] = {"path": "C:/mine/edit.ogg"}
    assert sound_event_override_path(cfg, "message_edit_saved") == "C:/mine/edit.ogg"
    assert sound_event_override_path(cfg, "message_sent") == "C:/mine/sent.ogg"
    assert sound_event_override_path(None, "startup") == ""


def _pack(folder, events):
    folder.mkdir()
    for rel in events.values():
        (folder / rel).write_bytes(b"ogg")
    return {"id": folder.name, "dir": str(folder), "events": events, "alerts": {}}


def test_a_pack_without_the_edit_event_plays_its_parent(tmp_path):
    active = _pack(tmp_path / "mine", {"message_foreground": "fg.ogg"})
    default = _pack(tmp_path / "default", {"message_edited_foreground": "edited.ogg",
                                           "message_foreground": "default_fg.ogg"})
    # The active pack's parent sound wins over the default pack's own file.
    assert resolve_sound_event_path(active, default, "message_edited_foreground").endswith("fg.ogg")
    assert resolve_sound_event_path(default, default, "message_edited_foreground").endswith("edited.ogg")
    bare = _pack(tmp_path / "bare", {"startup": "s.ogg"})
    assert resolve_sound_event_path(bare, default, "message_edited_foreground").endswith("edited.ogg")


# ── Per-conversation choices ────────────────────────────────────────────────

def test_choices_default_and_are_dropped_when_back_to_default():
    settings = {}
    assert edit_alerts.sound_choice(settings, JID, edit_alerts.FOREGROUND) == ("default", "")
    edit_alerts.set_sound_choice(settings, JID, edit_alerts.FOREGROUND, "alert_2")
    edit_alerts.set_toggle_choice(settings, JID, edit_alerts.SPEAK_FOREGROUND, "off")
    assert edit_alerts.sound_choice(settings, JID, edit_alerts.FOREGROUND) == ("alert_2", "")
    assert edit_alerts.toggle_choice(settings, JID, edit_alerts.SPEAK_FOREGROUND) == "off"
    edit_alerts.set_sound_choice(settings, JID, edit_alerts.FOREGROUND, "default")
    edit_alerts.set_toggle_choice(settings, JID, edit_alerts.SPEAK_FOREGROUND, "default")
    assert settings[edit_alerts.SETTING] == {}


def test_toggles_follow_settings_unless_the_conversation_says_otherwise():
    settings = {"general": {}}
    assert edit_alerts.toggle_on(settings, JID, edit_alerts.SPEAK_FOREGROUND) is False
    settings["general"]["announce_edited_messages"] = True
    assert edit_alerts.toggle_on(settings, JID, edit_alerts.SPEAK_FOREGROUND) is True
    edit_alerts.set_toggle_choice(settings, JID, edit_alerts.SPEAK_FOREGROUND, "off")
    assert edit_alerts.toggle_on(settings, JID, edit_alerts.SPEAK_FOREGROUND) is False
    edit_alerts.set_toggle_choice(settings, JID, edit_alerts.NOTIFY_BACKGROUND, "on")
    assert edit_alerts.toggle_on(settings, JID, edit_alerts.NOTIFY_BACKGROUND) is True


@pytest.mark.parametrize("choice,enabled,plan", [
    ("default", True, "event"), ("default", False, ""), ("off", True, ""),
    ("event", False, "event"), ("alert_1", False, "alert"), ("custom", True, "alert"),
])
def test_sound_plan(choice, enabled, plan):
    assert edit_alerts.sound_plan(choice, enabled) == plan


def test_stored_jid_finds_the_twin_the_dialog_used():
    settings = {edit_alerts.SETTING: {"123@lid": {"speak_foreground": "on"}}}
    assert edit_alerts.stored_jid(settings, [JID, "123@lid"]) == "123@lid"
    assert edit_alerts.stored_jid(settings, [JID, ""]) == JID


def test_an_edit_of_an_old_message_is_not_news():
    now = 2_000_000_000
    assert edit_alerts.edit_is_recent({"messageTimestamp": now - 600}, now)
    assert edit_alerts.edit_is_recent({"messageTimestamp": (now - 600) * 1000}, now)
    assert not edit_alerts.edit_is_recent({"messageTimestamp": now - 3 * 3600}, now)
    assert edit_alerts.edit_is_recent({}, now)


def test_global_defaults_are_off_in_both_default_files():
    from core.utils import DEFAULT_SETTINGS
    shipped_file = Path(__file__).resolve().parents[1] / "client" / "data" / "settings_default.json"
    with open(shipped_file, encoding="utf-8") as handle:
        shipped = json.load(handle)
    for key in edit_alerts.GLOBAL_KEYS.values():
        assert DEFAULT_SETTINGS["general"][key] is False
        assert shipped["general"][key] is False


def test_the_settings_dialog_reads_and_writes_the_same_global_keys():
    # The dialog spells the keys out so tests/test_settings_checkboxes_wiring.py
    # can parse them; they must stay the ones edit_alerts reads.
    source = (Path(__file__).resolve().parents[1] / "client" / "ui" / "dialogs"
              / "settings_dialog.py").read_text(encoding="utf-8")
    for key in edit_alerts.GLOBAL_KEYS.values():
        assert source.count(f'"{key}"') == 2, key  # one load, one save


# ── The mixin on a stub ─────────────────────────────────────────────────────

class _EditStub(_StubMainWindow):
    _maybe_notify_edit = MainWindow._maybe_notify_edit
    _edit_alert_jid = MainWindow._edit_alert_jid
    _edit_sound_path = MainWindow._edit_sound_path
    _edit_event_sound = MainWindow._edit_event_sound

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.played = []

    def _play_edit_sound(self, remote_jid, scope, respect_quiet_hours=False):
        self.played.append(scope)


def edited(*, from_me=False, age=60):
    return {"key": {"remoteJid": JID, "fromMe": from_me, "id": "ABC"},
            "messageType": "conversation", "message": {"conversation": "new text"},
            "messageTimestamp": int(time.time()) - age, "pushName": "Ana", "_edited": True}


@pytest.fixture(autouse=True)
def no_side_effects(monkeypatch):
    monkeypatch.setattr("wx.CallAfter", lambda fn, *a, **kw: fn(*a, **kw))
    monkeypatch.setattr("core.quiet_hours.is_quiet_hours_active", lambda: False)


def test_foreground_edit_plays_and_speaks_only_when_asked():
    window = _EditStub(open_jid=JID)
    window._maybe_notify_edit(JID, edited())
    assert window.played == [edit_alerts.FOREGROUND]
    assert window.spoken == []
    window.settings["general"]["announce_edited_messages"] = True
    window._maybe_notify_edit(JID, edited())
    assert len(window.spoken) == 1 and window.spoken[0].endswith(": new text")


def test_own_and_old_edits_are_ignored():
    window = _EditStub(open_jid=JID)
    window.settings["general"]["announce_edited_messages"] = True
    window._maybe_notify_edit(JID, edited(from_me=True))
    window._maybe_notify_edit(JID, edited(age=3 * 3600))
    assert window.played == [] and window.spoken == []


def test_background_edit_notifies_only_when_asked():
    window = _EditStub(window_active=False)
    window.notification_manager = MagicMock()
    window._maybe_notify_edit(JID, edited())
    window.notification_manager.send.assert_not_called()
    assert window.played == [edit_alerts.BACKGROUND]

    window.played.clear()
    edit_alerts.set_toggle_choice(window.settings, JID, edit_alerts.NOTIFY_BACKGROUND, "on")
    window._maybe_notify_edit(JID, edited())
    assert window.played == []
    _args, kwargs = window.notification_manager.send.call_args
    from core.notification_manager import EDIT_SOUND_EVENT
    assert kwargs["sound_event"] == EDIT_SOUND_EVENT


@pytest.mark.parametrize("muted,archived,locked", [(True, False, False),
                                                   (False, True, False),
                                                   (False, False, True)])
def test_silenced_chats_stay_silent_in_the_background(muted, archived, locked):
    window = _EditStub(window_active=False, muted_jids=[JID] if muted else [],
                       archived_jids=[JID] if archived else [],
                       locked_jids=[JID] if locked else [])
    window.notification_manager = MagicMock()
    window.settings["general"]["notify_edited_messages"] = True
    window._maybe_notify_edit(JID, edited())
    assert window.played == []
    window.notification_manager.send.assert_not_called()


def _sound_stub(tmp_path, events_cfg=None, conv=None):
    pack = _pack(tmp_path / "default", {"message_foreground": "fg.ogg",
                                         "message_sent": "sent.ogg"})
    (tmp_path / "default" / "alert1.ogg").write_bytes(b"ogg")
    pack["alerts"] = {"alert_1": "alert1.ogg"}
    stub = SimpleNamespace(
        settings={"sound_events": {"default": events_cfg or {}},
                  edit_alerts.SETTING: conv or {}},
        get_active_sound_pack=lambda: pack, _default_sound_pack=pack,
        _lid_to_phone={}, _phone_to_lid={})
    for name in ("_edit_alert_jid", "_edit_sound_path", "_edit_event_sound"):
        setattr(stub, name, getattr(MainWindow, name).__get__(stub))
    return stub


def test_received_edit_sound_is_silent_until_turned_on(tmp_path):
    stub = _sound_stub(tmp_path)
    assert stub._edit_sound_path(JID, edit_alerts.FOREGROUND) == ""
    assert stub._edit_sound_path(JID, edit_alerts.SAVED).endswith("sent.ogg")
    stub.settings["sound_events"]["default"] = {"message_edited_foreground": {"enabled": True}}
    assert stub._edit_sound_path(JID, edit_alerts.FOREGROUND).endswith("fg.ogg")


def test_a_conversation_can_turn_the_sound_on_off_or_change_it(tmp_path):
    conv = {JID: {"foreground": {"choice": "alert_1", "custom_path": ""},
                  "saved": {"choice": "off", "custom_path": ""}}}
    stub = _sound_stub(tmp_path, conv=conv)
    assert stub._edit_sound_path(JID, edit_alerts.FOREGROUND).endswith("alert1.ogg")
    assert stub._edit_sound_path(JID, edit_alerts.SAVED) == ""


# ── Hooks ───────────────────────────────────────────────────────────────────

def test_a_live_text_edit_by_someone_else_is_announced():
    calls = []
    stub = SimpleNamespace(
        _apply_remote_revoke=lambda *a: False,
        _persist_and_repaint_star=lambda *a: None,
        _persist_and_repaint_edit=lambda *a: None,
        _msg_bg_executor=MagicMock(), db=MagicMock(),
        _schedule_set_chats=lambda: None,
        _maybe_notify_edit=lambda jid, record: calls.append(record["message"]))
    existing = {"key": {"id": "ABC", "fromMe": False}, "messageType": "conversation",
                "message": {"conversation": "old"}}
    incoming = {"key": {"id": "ABC", "fromMe": False}, "messageType": "conversation",
                "message": {"conversation": "new"}}
    MainWindow._apply_possible_edit(stub, existing, incoming, JID)
    assert calls == [{"conversation": "new"}]


def test_the_notification_sound_of_an_edit_is_its_own():
    from core.notification_manager import EDIT_SOUND_EVENT, NotificationManager
    window = MagicMock()
    manager = SimpleNamespace(main_window=window)
    NotificationManager._play_sound(manager, JID, EDIT_SOUND_EVENT)
    window.play_edited_background_sound.assert_called_once_with(JID)
    window.play_background_notification_sound.assert_not_called()


def test_a_confirmed_edit_of_yours_plays_the_saved_sound(monkeypatch):
    from ui.conversations import ConversationsPanel
    monkeypatch.setattr("wx.CallAfter", lambda fn, *a, **kw: fn(*a, **kw))
    for answer, plays in ((True, 1), (None, 0), (False, 0)):
        main_window = MagicMock()
        main_window.edit_message.return_value = answer
        panel = SimpleNamespace(main_window=main_window, _rollback_message_edit=lambda *a: None)
        ConversationsPanel._send_message_edit(panel, JID, "ABC", "text", None, None, None)
        assert main_window.play_edit_saved_sound.call_count == plays
