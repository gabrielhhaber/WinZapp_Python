"""How much a background notification says (issue #258, suggestion 1).

Agreed with the reporter: three levels, chosen in Settings > General —
1. sound only: no banner, nothing read;
2. sound and the chat or group name, without the text;
3. sound, name and the message text (what WinZapp always did, the default).

The on/off switch (general.notifications_enabled) still sits above all three.
Nothing here opens a window: NotificationManager runs against the stubs of
tests/test_background_notification_not_duplicated.py.
"""

import inspect

import pytest

from core.notification_manager import (
    DEFAULT_NOTIFICATION_CONTENT,
    NOTIFICATION_CONTENT_LEVELS,
    NotificationManager,
    announce_background_message,
    background_notification_content,
    notification_content_level,
)
from core.utils import DEFAULT_SETTINGS
from tests.god_modules import main_window_method_source
from tests.test_background_notification_not_duplicated import (
    _FakeI18n,
    _FakeMainWindow,
    _Stub,
    _Toaster,
    direct_callafter,  # noqa: F401 — pytest fixture
)


# ── The decision ─────────────────────────────────────────────────────────────


def test_the_default_is_what_winzapp_always_did():
    assert DEFAULT_NOTIFICATION_CONTENT == "full"
    assert DEFAULT_SETTINGS["general"]["notification_content"] == "full"
    assert notification_content_level({}) == "full"
    assert notification_content_level({"general": {}}) == "full"


def test_an_unknown_value_never_silences_anything():
    assert notification_content_level({"general": {"notification_content": "mute"}}) == "full"
    assert notification_content_level(None) == "full"


@pytest.mark.parametrize("level", NOTIFICATION_CONTENT_LEVELS)
def test_a_known_value_is_read_back(level):
    assert notification_content_level({"general": {"notification_content": level}}) == level


def test_full_keeps_the_text():
    assert background_notification_content("full", "Ana", "bom dia", "Nova mensagem") == ("Ana", "bom dia")


def test_name_keeps_who_but_not_what():
    assert background_notification_content("name", "Família", "segredo", "Nova mensagem") == (
        "Família", "Nova mensagem")


def test_sound_carries_no_text_at_all():
    assert background_notification_content("sound", "Ana", "bom dia", "Nova mensagem") is None


# ── Sound only, through the notification queue ───────────────────────────────


def test_sound_only_is_queued_like_a_toast():
    """Queued, not played on the spot: it coalesces with a burst and never
    jumps a banner still being shown."""
    stub = _Stub()
    NotificationManager.send_sound_only(stub, "j@s.whatsapp.net")
    title, body, jid, key, _ = stub._queue.get_nowait()
    assert (title, body, jid, key) == (None, None, "j@s.whatsapp.net", None)


def test_dispatching_it_plays_the_sound_and_shows_and_says_nothing(direct_callafter):
    toaster = _Toaster()
    stub = _Stub(toaster)
    played = []
    stub._play_sound = lambda jid="": played.append(jid)

    stub._dispatch(None, None, "j@s.whatsapp.net")

    assert played == ["j@s.whatsapp.net"]
    assert toaster.shown == []
    assert stub.main_window.spoken == []


def test_do_not_disturb_silences_it_too(monkeypatch, direct_callafter):
    monkeypatch.setattr("core.quiet_hours.is_quiet_hours_active", lambda: True)
    stub = _Stub(_Toaster())
    played = []
    stub._play_sound = lambda jid="": played.append(jid)

    stub._dispatch(None, None, "j@s.whatsapp.net")

    assert played == []


# ── Spoken fallback (tray icon off) ──────────────────────────────────────────


def test_a_name_only_announcement_does_not_repeat_itself(direct_callafter):
    """"New message from Ana" already says it all; "New message from Ana: New
    message" is what passing the neutral line through would have spoken."""
    mw = _FakeMainWindow()
    announce_background_message(mw, _FakeI18n(), "Ana", "")
    assert mw.spoken == ["Nova mensagem de Ana"]


def test_a_full_announcement_is_unchanged(direct_callafter):
    mw = _FakeMainWindow()
    announce_background_message(mw, _FakeI18n(), "Ana", "oi")
    assert mw.spoken == ["Nova mensagem de Ana: oi"]


# ── Wiring in MainWindow ─────────────────────────────────────────────────────


class TestWiring:
    def test_background_messages_apply_the_level(self):
        src = main_window_method_source("on_new_message")
        assert "notification_content_level(self.settings)" in src
        assert "self.notification_manager.send_sound_only(remote_jid)" in src
        assert 'self.i18n.t("notif_hidden_message")' in src
        # Only after the on/off switch: off still means nothing at all.
        assert src.index('"notifications_enabled"') < src.index("notification_content_level(")
        # Sound only is decided before a locked chat's private wording.
        assert src.index('if level == "sound"') < src.rindex("format_locked_notification(")

    def test_the_spoken_fallback_drops_the_neutral_line_at_name(self):
        src = main_window_method_source("on_new_message")
        assert 'spoken_body = "" if (level == "name" and not locked) else body' in src

    def test_background_reactions_apply_the_level(self):
        src = main_window_method_source("_maybe_notify_reaction")
        assert "notification_content_level(self.settings)" in src
        assert "send_sound_only(" in src
        assert 'self.i18n.t("notif_hidden_reaction")' in src


# ── Settings > General ───────────────────────────────────────────────────────


class TestSettings:
    def test_one_label_per_level_in_order(self):
        from ui.dialogs.settings_dialog import NOTIFICATION_CONTENT_LABEL_KEYS
        assert [k.rsplit("_", 1)[1] for k in NOTIFICATION_CONTENT_LABEL_KEYS] == list(
            NOTIFICATION_CONTENT_LEVELS)

    def test_loaded_saved_and_relabelled(self):
        from ui.dialogs import settings_dialog
        src = inspect.getsource(settings_dialog.SettingsDialog)
        assert '.get("notification_content", "full")' in src
        assert ('["notification_content"] = (\n'
                "                NOTIFICATION_CONTENT_LEVELS[self._notification_content_radio.GetSelection()]"
                ) in src.replace("\r\n", "\n")
        assert 'self._notification_content_radio.SetLabel(i18n.t("notification_content_label"))' in src

    def test_it_follows_the_notifications_switch(self):
        from ui.dialogs.settings_dialog import SettingsDialog

        class _Ctl:
            def __init__(self, value=True):
                self.value, self.enabled = value, None

            def GetValue(self):
                return self.value

            def Enable(self, on=True):
                self.enabled = on

        class _S:
            _sync_notification_content_enabled = SettingsDialog._sync_notification_content_enabled

        s = _S()
        s._notifications_check, s._notification_content_radio = _Ctl(False), _Ctl()
        s._group_toast_order_radio = _Ctl()
        s._sync_notification_content_enabled()
        assert s._notification_content_radio.enabled is False
        assert s._group_toast_order_radio.enabled is False
        s._notifications_check.value = True
        s._sync_notification_content_enabled()
        assert s._notification_content_radio.enabled is True
        assert s._group_toast_order_radio.enabled is True


def test_the_no_banner_fallback_does_not_repeat_itself_at_name(direct_callafter):
    """Found in review: with no toaster, _dispatch() spoke "New message from
    Ana: New message" at the "name" level."""
    stub = _Stub(None)
    stub.i18n.t = lambda key: {"fg_new_msg": "Nova mensagem de {name}",
                               "notif_hidden_message": "Nova mensagem",
                               "notif_hidden_reaction": "Nova reação"}.get(key, key)
    stub._dispatch("Ana", "Nova mensagem", "j@s.whatsapp.net")
    assert stub.main_window.spoken == ["Nova mensagem de Ana"]


def test_a_hidden_reaction_is_not_announced_as_a_message(direct_callafter):
    """The fallback always opens with "New message from"; stripping the
    reaction line too would announce a message that is not there."""
    stub = _Stub(None)
    stub.i18n.t = lambda key: {"fg_new_msg": "Nova mensagem de {name}",
                               "notif_hidden_message": "Nova mensagem",
                               "notif_hidden_reaction": "Nova reação"}.get(key, key)
    stub._dispatch("Ana", "Nova reação", "j@s.whatsapp.net")
    assert stub.main_window.spoken == ["Nova mensagem de Ana: Nova reação"]
