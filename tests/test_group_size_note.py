"""The conversation-data button of an open group says its current number of
participants.

Report of 2026-11-11: on entering a group whose participant list takes a
while to arrive, the button kept the count of the moment it opened. The note
was computed once (_fetch_and_update_profile); the participant fetch that
retries, and runs again when someone joins or leaves, never touched it.

Unbound methods on plain stubs, as the repository tests the panel."""
from types import SimpleNamespace

import pytest

from ui.conversations import ConversationsPanel
import ui.conversation_panel.mentions as mentions
from ui.conversation_panel.conversation_info import group_size_note

GROUP = "1203630000000001@g.us"


class _Button:
    def __init__(self, note=""):
        self.note = note

    def GetNote(self):
        return self.note

    def SetNote(self, note):
        self.note = note


class _Panel:
    _show_group_size = ConversationsPanel._show_group_size
    _fetch_group_participants = ConversationsPanel._fetch_group_participants

    def __init__(self, open_jid=GROUP, answers=()):
        self.conversation = {"remoteJid": open_jid, "name": "Turma"} if open_jid else None
        self._conv_data_btn = _Button("Turma")
        self.conversation_panel = SimpleNamespace(Layout=lambda: None)
        self.calls = []
        self._answers = list(answers)
        i18n = SimpleNamespace(t=lambda key: {"group_size": "{count} participantes"}[key])
        self.main_window = SimpleNamespace(
            i18n=i18n, my_jid="",
            _resolve_contact_name=lambda c: "",
            find_name_through_messages=lambda c: "",
            get_group_info_recent=lambda jid: self._answers.pop(0))

    def _set_group_call_limit(self, jid, count):
        self.calls.append(("limit", jid, count))

    def _set_group_participants_cache(self, cache, jid=""):
        self.calls.append(("cache", jid, len(cache)))

    def _get_participant_name(self, jid, p):
        return jid


def test_the_open_group_shows_the_new_count():
    panel = _Panel()
    panel._show_group_size(GROUP, 42)
    assert panel._conv_data_btn.note == "Turma, 42 participantes"


def test_another_group_or_none_open_is_left_alone():
    panel = _Panel(open_jid="999@g.us")
    panel._show_group_size(GROUP, 42)
    assert panel._conv_data_btn.note == "Turma"
    closed = _Panel(open_jid=None)
    closed._show_group_size(GROUP, 42)
    assert closed._conv_data_btn.note == "Turma"


@pytest.mark.parametrize("size", [0, None, "3"])
def test_an_unknown_count_shows_the_name_alone(size):
    panel = _Panel()
    assert group_size_note(panel.main_window, panel.conversation, size) == "Turma"


def test_the_participant_fetch_updates_the_count_after_its_retries(monkeypatch):
    scheduled, slept = [], []
    monkeypatch.setattr(mentions.wx, "CallAfter", lambda fn, *a: scheduled.append((fn, a)))
    monkeypatch.setattr(mentions.time, "sleep", slept.append)
    members = [{"id": f"55119999000{i}@s.whatsapp.net"} for i in range(3)]
    panel = _Panel(answers=[{}, {"participants": []}, {"participants": members}])
    panel._fetch_group_participants(GROUP)
    for fn, args in scheduled:
        fn(*args)
    assert panel._conv_data_btn.note == "Turma, 3 participantes"
    assert slept == list(mentions.GROUP_PARTICIPANT_RETRY_DELAYS[:2])


def test_the_retries_last_about_a_minute():
    assert 45 <= sum(mentions.GROUP_PARTICIPANT_RETRY_DELAYS) <= 90


def test_a_membership_change_reads_the_group_again():
    from main import MainWindow
    import main_window.groups as groups

    started = []

    class _Thread:
        def __init__(self, target=None, args=(), daemon=None):
            started.append(args)

        def start(self):
            pass
    mw = SimpleNamespace(
        _refresh_mention_cache_on_membership_change=None,
        _GROUP_MEMBERSHIP_NOTIF_SUBTYPES=MainWindow._GROUP_MEMBERSHIP_NOTIF_SUBTYPES,
        conversations_panel=SimpleNamespace(conversation={"remoteJid": GROUP},
                                            _fetch_group_participants=None),
        _normalize_jid=lambda jid: jid,
        _group_info_recent={GROUP: (0.0, {"participants": [{"id": "x"}]}), "other@g.us": (0.0, {})},
    )
    original = groups.threading.Thread
    groups.threading.Thread = _Thread
    try:
        MainWindow._refresh_mention_cache_on_membership_change(mw, GROUP, {
            "messageType": "groupNotification",
            "message": {"groupNotification": {"subtype": "add"}}})
    finally:
        groups.threading.Thread = original
    assert GROUP not in mw._group_info_recent and "other@g.us" in mw._group_info_recent
    assert started == [(GROUP,)]
