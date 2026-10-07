"""Unbound UI methods on plain widgets; no wx.App, window or WinEvent."""
from types import SimpleNamespace

import pytest

import core.contact_presence as model
from core.contact_presence import cached, merge
from main_window.chat_events import ChatEventsMixin
from main_window.identity import IdentityMixin
import main_window.identity as identity
from ui.conversation_panel.conversation_info import ConversationInfoMixin
from ui.dialogs.conversation_data_dialog import ConversationDataDialog
from tests.test_contact_presence_freshness import Owner, PN


class Text:
    def __init__(self):
        self.text, self.writes, self.focuses = "", 0, 0
    def GetNote(self):
        return self.text
    def SetNote(self, text):
        self.text, self.writes = text, self.writes + 1
    SetValue = SetNote
    def SetFocus(self):
        self.focuses += 1


class I18n:
    def t(self, key):
        return {"online_status": "Online", "presence_online": "Online",
                "presence_unavailable": "Unknown", "presence_last_seen": "Seen {time}",
                "last_seen_today": "Seen {time}", "last_seen_yesterday": "Seen {time}",
                "last_seen_date": "Seen {date} {time}", "time_fmt": "%H:%M",
                "group_size": "{count} members",
                "date_fmt": "%d/%m/%Y", "datetime_fmt": "%d/%m/%Y %H:%M"}.get(key, key)


def views(owner):
    owner.i18n = I18n()
    owner._resolve_contact_name = lambda conv: "Contact"
    owner.find_name_through_messages = lambda conv: ""
    owner._presence_label_for_chat = lambda jid, group: ""
    button, text = Text(), Text()
    layouts = []
    panel = SimpleNamespace(main_window=owner, conversation={"remoteJid": PN},
                            _conv_data_btn=button,
                            conversation_panel=SimpleNamespace(Layout=lambda: layouts.append(True)))
    dialog = SimpleNamespace(_mw=owner, _i18n=owner.i18n, _jid=PN, _name="Contact", _info_ctrl=text)
    return panel, dialog, button, text, layouts


@pytest.mark.parametrize("state,last_seen,expected", [("available", None, "Online"), ("composing", None, "Online"), ("recording", None, "Online"), ("unavailable", 1700000000, "Seen"), ("unavailable", None, "Unknown")])
def test_three_views_read_the_same_snapshot_without_network(state, last_seen, expected):
    owner = Owner()
    merge(owner, PN, {"lastKnownPresence": state, "lastSeen": last_seen})
    panel, dialog, button, text, layouts = views(owner)
    ConversationInfoMixin._refresh_presence_note(panel, PN)
    # Repeated ticks must not rewrite unchanged accessible button text.
    ConversationInfoMixin._refresh_presence_note(panel, PN)
    announcement = ChatEventsMixin._presence_announcement_for_chat(owner, panel.conversation)
    ConversationDataDialog._populate_personal_unsafe(dialog, {"lastSeenTs": 9999999999})
    if expected == "Unknown":
        assert button.text == "Contact" and announcement == "Unknown"
        assert "Seen" not in text.text and "Online" not in text.text
    else:
        assert expected in button.text and expected in announcement and expected in text.text
    assert button.writes == len(layouts) == 1 and button.focuses == 0
    assert text.focuses == 1  # Existing dialog's initial focus, never a timer tick.


def test_expired_snapshot_is_not_announced_or_resurrected_by_dialog_data(monkeypatch):
    owner = Owner()
    merge(owner, PN, {"lastSeen": 1000, "lastKnownPresence": "unavailable"}, now=100)
    monkeypatch.setattr(model.time, "monotonic", lambda: 191)
    panel, dialog, button, text, _ = views(owner)
    ConversationInfoMixin._refresh_presence_note(panel, PN)
    assert ChatEventsMixin._presence_announcement_for_chat(owner, panel.conversation) == "Unknown"
    ConversationDataDialog._populate_personal_unsafe(dialog, {"lastSeenTs": 1000})
    assert button.text == "Contact" and "Seen" not in text.text


def test_online_profile_does_not_wait_for_last_seen_query():
    owner = Owner()
    merge(owner, PN, {"lastKnownPresence": "available"})
    owner._fetch_contact_presence = lambda jid: pytest.fail("online rendering must not await HTTP")
    assert IdentityMixin.get_last_seen(owner, PN) is None


def test_group_note_still_uses_participant_count_not_contact_presence(monkeypatch):
    owner = Owner()
    panel, _, button, _, _ = views(owner)
    panel.conversation = {"remoteJid": "123@g.us"}
    panel._contact_presence_visit = 1
    owner.get_group_info_recent = lambda jid: {"participants": [{}, {}]}
    owner._fetch_contact_presence = lambda jid: pytest.fail("group presence query")
    monkeypatch.setattr(identity.wx, "CallAfter", lambda fn: fn())
    ConversationInfoMixin._fetch_and_update_profile(panel, panel.conversation, 1)
    assert button.text == "Contact, 2 members"
    ConversationInfoMixin._refresh_presence_note(panel, "123@g.us")
    assert button.text == "Contact, 2 members"


def test_old_group_callback_does_not_update_reopened_chat(monkeypatch):
    owner = Owner()
    panel, _, button, _, _ = views(owner)
    panel.conversation = {"remoteJid": "123@g.us"}
    panel._contact_presence_visit = 1
    owner.get_group_info_recent = lambda jid: {"size": 2}
    queued = []
    monkeypatch.setattr(identity.wx, "CallAfter", queued.append)
    ConversationInfoMixin._fetch_and_update_profile(panel, panel.conversation, 1)
    panel._contact_presence_visit = 2
    queued.pop()()
    assert button.writes == 0


def test_profile_response_cannot_replace_live_event(monkeypatch):
    owner = Owner()
    queued = []
    monkeypatch.setattr(identity.wx, "CallAfter", lambda fn: queued.append(fn))
    owner._fetch_contact_presence = lambda jid: {"lastKnownPresence": "unavailable", "lastSeen": 1000}
    assert IdentityMixin.get_last_seen(owner, PN) == 1000
    merge(owner, PN, {"lastKnownPresence": "available"})
    queued.pop()()
    assert cached(owner, PN)["lastKnownPresence"] == "available"


def test_profile_response_from_previous_visit_is_not_committed(monkeypatch):
    owner = Owner()
    queued = []
    monkeypatch.setattr(identity.wx, "CallAfter", queued.append)
    owner._fetch_contact_presence = lambda jid: {"lastKnownPresence": "unavailable", "lastSeen": 1000}
    IdentityMixin.get_last_seen(owner, PN)
    owner.conversations_panel._contact_presence_visit += 1
    queued.pop()()
    assert cached(owner, PN, fresh=False) == {}


@pytest.mark.parametrize("status,payload", [(500, {}), (200, {"status": "error"}), (200, None)])
def test_failed_subscription_does_not_keep_success_throttle(monkeypatch, status, payload):
    owner = Owner()
    owner.wpp_server, owner.wpp_port, owner.token = "http://invalid.test", 1, "synthetic"
    calls = []
    class Thread:
        def __init__(self, target, daemon):
            self.target = target
        def start(self):
            self.target()
    monkeypatch.setattr(identity.threading, "Thread", Thread)
    monkeypatch.setattr(identity.time, "time", lambda: 100)
    def post(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(status_code=status, json=lambda: payload)
    monkeypatch.setattr(identity, "api_post", post)
    IdentityMixin.subscribe_presence(owner, PN)
    IdentityMixin.subscribe_presence(owner, PN)
    assert len(calls) == 2


def test_successful_subscription_is_throttled(monkeypatch):
    owner = Owner()
    owner.wpp_server, owner.wpp_port, owner.token = "http://invalid.test", 1, "synthetic"
    calls = []
    monkeypatch.setattr(identity.threading, "Thread", lambda target, daemon: SimpleNamespace(start=target))
    monkeypatch.setattr(identity.time, "time", lambda: 100)
    monkeypatch.setattr(identity, "api_post", lambda *a, **kw: calls.append(kw) or SimpleNamespace(status_code=200, json=lambda: {"status": "success"}))
    IdentityMixin.subscribe_presence(owner, PN)
    IdentityMixin.subscribe_presence(owner, PN)
    assert len(calls) == 1


@pytest.mark.parametrize("days,key", [(0, "last_seen_today"), (1, "last_seen_yesterday"), (2, "last_seen_date")])
def test_last_seen_local_calendar_and_milliseconds(monkeypatch, days, key):
    import datetime
    from ui.conversation_panel.text_helpers import _fmt_last_seen
    real = datetime.datetime
    frozen = real(2026, 10, 7, 15, 30)
    class Clock(real):
        @classmethod
        def now(cls, tz=None):
            return frozen
    monkeypatch.setattr(datetime, "datetime", Clock)
    i18n = SimpleNamespace(t=lambda name: {"time_fmt": "%H:%M", "date_fmt": "%d/%m/%Y"}.get(name, name + " {time}"))
    ts = int((frozen - datetime.timedelta(days=days)).timestamp())
    assert _fmt_last_seen(ts, i18n) == _fmt_last_seen(ts * 1000, i18n) == key + " 15:30"
