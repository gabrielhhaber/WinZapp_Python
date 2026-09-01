"""Tests for the per-stage delivery/read/played timeline shown in the
"message data" dialog (ConversationsPanel._on_menu_message_data).

Each entry appended to a message's MessageUpdate list by
MainWindow.on_message_status_update() now carries a "ts" alongside "status"
(see client/main.py), so the dialog can show "Enviada: 14:29" / "Entregue:
14:30" / "Lida: 14:32" like the official WhatsApp client, instead of a
single collapsed "Status: Lida" line. Messages loaded before this change (or
from history sync, which only ever reports one aggregate ack with no
timeline) have MessageUpdate entries with no "ts" — those must fall back to
no history lines at all, letting the caller show the old single-line status.

ConversationsPanel is a wx.Panel and cannot be instantiated without a running
wx.App, so the methods under test are exercised as plain functions against a
small stub carrying just the attributes they touch — same approach as
tests/test_message_bookmarks.py.
"""

from datetime import datetime

import pytest

from ui.conversations import ConversationsPanel


@pytest.fixture(autouse=True)
def _fixed_locale_format(monkeypatch):
    """_format_date() resolves the OS's actual Windows regional format via
    core.locale_format.get_time_format/get_datetime_format, so on a real
    Windows runner it can render 12-hour "02:29 PM" instead of the language
    file's own %H:%M fallback — making the exact rendered string depend on
    whatever locale happens to be configured on the machine running the
    suite. Pin both to their fallback argument (the CI-runner-independent
    behavior _format_date already has off-Windows) so this test's expected
    strings are deterministic everywhere."""
    monkeypatch.setattr("ui.conversations.get_time_format", lambda fallback: fallback)
    monkeypatch.setattr("ui.conversations.get_datetime_format", lambda fallback: fallback)


class _FakeI18n:
    _STRINGS = {
        "status_sent": "Enviada",
        "status_delivered": "Entregue",
        "status_read": "Lida",
        "status_played": "Reproduzida",
        "status_failed": "Falha ao enviar",
        "status_pending": "Pendente",
        "time_fmt": "%H:%M",
        "datetime_fmt": "%d/%m/%Y %H:%M",
    }

    def t(self, key):
        return self._STRINGS[key]


class _FakeMainWindow:
    def __init__(self, self_jids=()):
        self.i18n = _FakeI18n()
        # JIDs that should be treated as "me" — the self-chat ("Me") case.
        self._self_jids = set(self_jids)

    def _is_self_jid(self, jid):
        return jid in self._self_jids


def _panel():
    return ConversationsPanel.__new__(ConversationsPanel)


def _stub(main_window=None):
    p = _panel()
    p.main_window = main_window or _FakeMainWindow()
    return p


def _fmt(ts):
    """Same formatting _format_date() applies for a non-today timestamp."""
    return datetime.fromtimestamp(ts).strftime("%d/%m/%Y %H:%M")


# A fixed, clearly-not-today timestamp so _format_date takes the "full
# datetime" branch deterministically regardless of when the suite runs.
_T0 = datetime(2024, 1, 1, 14, 29, 0).timestamp()
_T1 = _T0 + 60
_T2 = _T0 + 180


class TestClassifyStatusEntry:
    def test_recognises_text_and_numeric_forms(self):
        p = _stub()
        assert p._classify_status_entry("READ") == "read"
        assert p._classify_status_entry("4") == "read"
        assert p._classify_status_entry("DELIVERY_ACK") == "delivered"
        assert p._classify_status_entry("3") == "delivered"
        assert p._classify_status_entry("SERVER_ACK") == "sent"
        assert p._classify_status_entry("2") == "sent"
        assert p._classify_status_entry("PLAYED") == "played"
        assert p._classify_status_entry("5") == "played"
        assert p._classify_status_entry("-1") == "failed"

    def test_unrecognised_or_empty_yields_empty_string(self):
        p = _stub()
        assert p._classify_status_entry("") == ""
        assert p._classify_status_entry(None) == ""
        assert p._classify_status_entry("nonsense") == ""


class TestStatusHistoryLines:
    def _msg(self, updates, from_me=True):
        return {"key": {"fromMe": from_me}, "MessageUpdate": updates}

    def test_full_timeline_for_a_sent_message(self):
        p = _stub()
        msg = self._msg([
            {"status": "2", "ts": _T0},
            {"status": "3", "ts": _T1},
            {"status": "4", "ts": _T2},
        ])
        lines = p._status_history_lines(msg)
        assert lines == [
            f"Enviada: {_fmt(_T0)}",
            f"Entregue: {_fmt(_T1)}",
            f"Lida: {_fmt(_T2)}",
        ]

    def test_keeps_earliest_timestamp_per_stage(self):
        # A duplicate/retried ack for a stage already reached must not
        # overwrite the original time it was first reached.
        p = _stub()
        msg = self._msg([
            {"status": "4", "ts": _T0},
            {"status": "4", "ts": _T1},
        ])
        lines = p._status_history_lines(msg)
        assert lines == [f"Lida: {_fmt(_T0)}"]

    def test_entries_without_ts_are_ignored(self):
        # Messages loaded from history sync only ever carry one aggregate
        # ack, with no per-stage timestamp — no history line should render.
        p = _stub()
        msg = self._msg([{"status": "4"}])
        assert p._status_history_lines(msg) == []

    def test_no_message_update_list_yields_no_lines(self):
        p = _stub()
        assert p._status_history_lines({"key": {"fromMe": True}}) == []

    def test_failed_stage_reported_after_the_reached_stages(self):
        p = _stub()
        msg = self._msg([
            {"status": "2", "ts": _T0},
            {"status": "-1", "ts": _T1},
        ])
        lines = p._status_history_lines(msg)
        assert lines == [
            f"Enviada: {_fmt(_T0)}",
            f"Falha ao enviar: {_fmt(_T1)}",
        ]

    def test_received_message_only_ever_shows_played(self):
        # Delivered/read status is never tracked for messages WE received —
        # only whether we played a voice message.
        p = _stub()
        msg = self._msg([
            {"status": "4", "ts": _T0},
            {"status": "5", "ts": _T1},
        ], from_me=False)
        lines = p._status_history_lines(msg)
        assert lines == [f"Reproduzida: {_fmt(_T1)}"]


SELF_JID = "5511999999999@s.whatsapp.net"
OTHER_JID = "5511888888888@s.whatsapp.net"


class TestStatusHistoryLinesSelfChat:
    """Issue #95: the "Me" chat has only one participant, so
    sent/delivered/read are never a real receipt there — only the "played"
    stage (and a genuine failure) can still appear in the timeline."""

    def _msg(self, updates, from_me=True, remote_jid=SELF_JID):
        return {
            "key": {"fromMe": from_me, "remoteJid": remote_jid},
            "MessageUpdate": updates,
        }

    def test_sent_delivered_read_are_suppressed_in_the_self_chat(self):
        p = _stub(_FakeMainWindow(self_jids={SELF_JID}))
        msg = self._msg([
            {"status": "2", "ts": _T0},
            {"status": "3", "ts": _T1},
            {"status": "4", "ts": _T2},
        ])
        assert p._status_history_lines(msg) == []

    def test_played_still_shows_in_the_self_chat(self):
        p = _stub(_FakeMainWindow(self_jids={SELF_JID}))
        msg = self._msg([
            {"status": "2", "ts": _T0},
            {"status": "5", "ts": _T1},
        ])
        assert p._status_history_lines(msg) == [f"Reproduzida: {_fmt(_T1)}"]

    def test_failure_still_shows_in_the_self_chat(self):
        p = _stub(_FakeMainWindow(self_jids={SELF_JID}))
        msg = self._msg([
            {"status": "2", "ts": _T0},
            {"status": "-1", "ts": _T1},
        ])
        assert p._status_history_lines(msg) == [f"Falha ao enviar: {_fmt(_T1)}"]

    def test_a_different_chat_still_shows_the_full_timeline(self):
        """Only the actual self-chat is affected — _is_self_jid returning
        False for a normal 1:1/group chat must not change anything."""
        p = _stub(_FakeMainWindow(self_jids={SELF_JID}))
        msg = self._msg([
            {"status": "2", "ts": _T0},
            {"status": "3", "ts": _T1},
            {"status": "4", "ts": _T2},
        ], remote_jid=OTHER_JID)
        assert p._status_history_lines(msg) == [
            f"Enviada: {_fmt(_T0)}",
            f"Entregue: {_fmt(_T1)}",
            f"Lida: {_fmt(_T2)}",
        ]


class TestMapStatusSelfChat:
    """Same issue #95 exception applied to _map_status() — the single-line
    status shown in the message list, the chat list preview and (as a
    fallback) the message data dialog."""

    def _msg(self, status, from_me=True, remote_jid=SELF_JID):
        return {"key": {"fromMe": from_me, "remoteJid": remote_jid}, "status": status}

    @pytest.mark.parametrize("status", ["READ", "DELIVERED", "SENT"])
    def test_receipt_statuses_are_suppressed_in_the_self_chat(self, status):
        p = _stub(_FakeMainWindow(self_jids={SELF_JID}))
        assert p._map_status(self._msg(status)) == ""

    def test_failed_status_still_shows_in_the_self_chat(self):
        p = _stub(_FakeMainWindow(self_jids={SELF_JID}))
        assert p._map_status(self._msg("-1")) == "Falha ao enviar"

    def test_played_status_still_shows_in_the_self_chat(self):
        p = _stub(_FakeMainWindow(self_jids={SELF_JID}))
        assert p._map_status(self._msg("PLAYED")) == "Reproduzida"

    def test_pending_message_still_shows_in_the_self_chat(self):
        p = _stub(_FakeMainWindow(self_jids={SELF_JID}))
        msg = self._msg("READ")
        msg["_local_pending"] = True
        assert p._map_status(msg) == "Pendente"

    def test_a_different_chat_still_shows_read(self):
        p = _stub(_FakeMainWindow(self_jids={SELF_JID}))
        msg = self._msg("READ", remote_jid=OTHER_JID)
        assert p._map_status(msg) == "Lida"

    def test_received_message_in_the_self_chat_is_unaffected(self):
        """not from_me already returns "" before the self-chat check is ever
        reached — this only pins that the two don't interact oddly."""
        p = _stub(_FakeMainWindow(self_jids={SELF_JID}))
        msg = self._msg("READ", from_me=False)
        assert p._map_status(msg) == ""
