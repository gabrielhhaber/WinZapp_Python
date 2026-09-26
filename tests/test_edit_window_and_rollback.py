"""Editing: offer it only while WhatsApp accepts it, and undo it when refused.

Reproduced 2026-09-14: a 59-minute-old own message was edited with Alt+E.
WinZapp allowed edits for three hours, rewrote the row and marked it "Editada"
— and WhatsApp answered ``Cannot edit this message``. The edit existed only on
the editor's screen, with nothing saying so.

The window was then read out of the running WhatsApp Web over DevTools (see
core.message_edit.EDIT_UI_WINDOW_SECONDS): it offers editing for 900 s and
accepts it for 1200 s (remote config). Three defects follow, pinned here:

- the three-hour gate offered edits WhatsApp refuses;
- Alt+E past the window did nothing, silently;
- a refused edit kept its optimistic rewrite forever.
"""

import copy
import inspect

import pytest

from core.message_edit import (
    EDIT_UI_WINDOW_SECONDS,
    edit_window_open,
    restore_edit_state,
    snapshot_edit_state,
)
from ui.conversations import ConversationsPanel
from tests.god_modules import patch_main_global

NOW = 1_789_360_000


class TestEditWindow:
    def test_the_window_is_what_whatsapp_web_measured(self):
        assert EDIT_UI_WINDOW_SECONDS == 900

    @pytest.mark.parametrize("age, expected", [
        (0, True), (899, True), (900, False), (3599, False), (2 * 86400, False),
    ])
    def test_boundary(self, age, expected):
        assert edit_window_open(NOW - age, now=NOW) is expected

    def test_milliseconds_and_numeric_strings_are_understood(self):
        assert edit_window_open((NOW - 60) * 1000, now=NOW) is True
        assert edit_window_open(str(NOW - 60), now=NOW) is True

    @pytest.mark.parametrize("ts", [None, "", "x", 0, -5])
    def test_an_unreadable_timestamp_closes_the_window(self, ts):
        assert edit_window_open(ts, now=NOW) is False

    def test_no_three_hour_gate_is_left(self):
        src = inspect.getsource(ConversationsPanel)
        assert "10800" not in src.replace("(\"mute_3h\", 10800)", "")


class TestSnapshotRestore:
    def _reply(self):
        return {
            "key": {"id": "m1", "fromMe": True},
            "messageType": "extendedTextMessage",
            "message": {"extendedTextMessage": {
                "text": "antes", "contextInfo": {"stanzaId": "Q1"}}},
        }

    def test_restores_text_type_quote_and_clears_the_edited_marker(self):
        msg = self._reply()
        original = copy.deepcopy(msg)
        snap = snapshot_edit_state(msg)
        msg["message"] = {"conversation": "depois"}
        msg["messageType"] = "conversation"
        msg["contextInfo"] = {"mentionedJid": ["x"]}
        msg["_edited"] = True
        applied = copy.deepcopy(msg["message"])

        assert restore_edit_state(msg, snap, applied) is True
        assert msg == original

    def test_a_newer_state_is_not_overwritten(self):
        msg = self._reply()
        snap = snapshot_edit_state(msg)
        applied = {"conversation": "depois"}
        # A sync replaced the record with something else meanwhile.
        msg["message"] = {"conversation": "outra coisa"}
        assert restore_edit_state(msg, snap, applied) is False
        assert msg["message"] == {"conversation": "outra coisa"}


# ── ConversationsPanel, bound onto a stub ───────────────────────────────────

class _I18n:
    def t(self, key):
        return key + ("{minutes}" if key == "edit_window_expired" else "")


class _List:
    def __init__(self, selected=0, focused=-1):
        self.selected = selected
        self.focused = focused
        self.texts = {}

    def GetFirstSelected(self):
        return self.selected

    def GetFocusedItem(self):
        return self.focused

    def SetItemText(self, idx, text):
        self.texts[idx] = text


class _MainWindow:
    def __init__(self, records=None, edit_result=True):
        self.i18n = _I18n()
        self.spoken = []
        self.saves = []
        self.set_chats_calls = 0
        self.edit_result = edit_result
        self.records = records if records is not None else []

    def output(self, text, interrupt=False):
        self.spoken.append(text)

    def edit_message(self, remote_jid, message_id, new_text, mentioned_jids=None):
        return self.edit_result

    def get_chat(self, jid):
        return {"messages": {"messages": {"records": self.records}}}

    def _schedule_save(self, dirty_jid=None):
        self.saves.append(dirty_jid)

    def _schedule_set_chats(self):
        self.set_chats_calls += 1


class _Panel:
    _on_accel_edit_message = ConversationsPanel._on_accel_edit_message
    _apply_message_edit = ConversationsPanel._apply_message_edit
    _send_message_edit = ConversationsPanel._send_message_edit
    _rollback_message_edit = ConversationsPanel._rollback_message_edit
    _message_own_links = ConversationsPanel._message_own_links

    def __init__(self, messages, edit_result=True, records=None, focused=-1):
        self._sorted_messages = messages
        self.main_window = _MainWindow(
            records=messages if records is None else records, edit_result=edit_result)
        self.messages_list = _List(focused=focused)
        self._editing_message_id = "m1"
        self.entered_edit = []
        self.cancelled = 0
        self.links_panels = []
        self.mentions_panels = []

    def _extract_links(self, line):
        return []

    def _extract_mentions(self, msg):
        return list((msg.get("contextInfo") or {}).get("mentionedJid") or [])

    def _update_links_panel(self, links):
        self.links_panels.append(links)

    def _update_mentions_panel(self, mentions):
        self.mentions_panels.append(mentions)

    def _is_separator(self, msg):
        return False

    def _on_menu_edit_message(self, index, msg):
        self.entered_edit.append(index)

    def _build_mention_payload(self, text):
        return text, None

    def _render_message_line(self, msg, index=None, total=None, include_quoted_preview=True):
        body = msg.get("message") or {}
        return body.get("conversation") or (body.get("extendedTextMessage") or {}).get("text", "")

    def _on_cancel_edit(self):
        self.cancelled += 1


class _InlineThread:
    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._run = lambda: target(*args, **(kwargs or {}))

    def start(self):
        self._run()


@pytest.fixture(autouse=True)
def _inline(monkeypatch):
    monkeypatch.setattr("ui.conversations.threading.Thread", _InlineThread)
    monkeypatch.setattr("ui.conversations.wx.CallAfter", lambda fn, *a, **kw: fn(*a, **kw))


def _own(age, text="antes", mid="m1"):
    import time
    return {"key": {"id": mid, "fromMe": True},
            "messageType": "conversation", "message": {"conversation": text},
            "messageTimestamp": int(time.time()) - age}


class TestAltE:
    def test_within_the_window_enters_edit_mode(self):
        panel = _Panel([_own(60)])
        panel._on_accel_edit_message(None)
        assert panel.entered_edit == [0]
        assert panel.main_window.spoken == []

    def test_past_the_window_says_so_instead_of_doing_nothing(self):
        panel = _Panel([_own(59 * 60)])
        panel._on_accel_edit_message(None)
        assert panel.entered_edit == []
        assert panel.main_window.spoken == ["edit_window_expired15"]

    def test_someone_elses_message_stays_silent(self):
        msg = _own(60)
        msg["key"]["fromMe"] = False
        panel = _Panel([msg])
        panel._on_accel_edit_message(None)
        assert panel.entered_edit == [] and panel.main_window.spoken == []

    def test_the_context_menu_uses_the_same_window(self):
        src = inspect.getsource(ConversationsPanel)
        assert "_can_edit    = edit_window_open(" in src


class TestRefusedEditIsRolledBack:
    def test_a_refusal_restores_the_row_and_announces_it(self):
        panel = _Panel([_own(60)], edit_result=False)

        panel._apply_message_edit("depois", "grupo@g.us")

        msg = panel._sorted_messages[0]
        assert msg["message"] == {"conversation": "antes"}
        assert "_edited" not in msg
        assert panel.messages_list.texts[0] == "antes"
        assert panel.main_window.spoken == ["edit_message_failed"]

    def test_a_success_keeps_the_edit_and_says_nothing(self):
        panel = _Panel([_own(60)], edit_result=True)

        panel._apply_message_edit("depois", "grupo@g.us")

        assert panel._sorted_messages[0]["message"] == {"conversation": "depois"}
        assert panel._sorted_messages[0]["_edited"] is True
        assert panel.main_window.spoken == []

    def test_an_unknown_result_is_not_treated_as_a_refusal(self):
        panel = _Panel([_own(60)], edit_result=None)
        panel._apply_message_edit("depois", "grupo@g.us")
        assert panel._sorted_messages[0]["message"] == {"conversation": "depois"}
        assert panel.main_window.spoken == []

    def test_a_row_gone_from_the_list_still_reports_the_failure(self):
        panel = _Panel([_own(60, mid="other")], edit_result=False)
        panel._apply_message_edit("depois", "grupo@g.us")
        assert panel.main_window.spoken == ["edit_message_failed"]

    def test_a_record_paginated_out_of_the_list_is_still_restored(self):
        msg = _own(60)
        panel = _Panel([msg], edit_result=None)
        panel._apply_message_edit("depois", "grupo@g.us")
        applied = copy.deepcopy(msg["message"])
        snapshot = {"message": {"conversation": "antes"}, "messageType": "conversation",
                    "contextInfo": "__absent__", "_edited": "__absent__"}
        # The row left the paginated window; the record is still resident.
        panel._sorted_messages = []
        panel.main_window.records = [msg]

        panel._rollback_message_edit("grupo@g.us", "m1", snapshot, applied)

        assert msg["message"] == {"conversation": "antes"}
        assert "_edited" not in msg
        assert panel.main_window.saves[-1] == "grupo@g.us"

    def test_a_record_changed_meanwhile_is_left_alone(self):
        msg = _own(60)
        panel = _Panel([msg], edit_result=None)
        panel._apply_message_edit("depois", "grupo@g.us")
        applied = copy.deepcopy(msg["message"])
        snapshot = {"message": {"conversation": "antes"}, "messageType": "conversation",
                    "contextInfo": "__absent__", "_edited": "__absent__"}
        msg["message"] = {"conversation": "algo mais novo"}

        panel._rollback_message_edit("grupo@g.us", "m1", snapshot, applied)

        assert msg["message"] == {"conversation": "algo mais novo"}
        assert panel.main_window.spoken == ["edit_message_failed"]

    def test_the_focused_rows_panels_follow_the_restore(self):
        msg = _own(60)
        msg["contextInfo"] = {"mentionedJid": []}
        panel = _Panel([msg], edit_result=False, focused=0)
        panel._build_mention_payload = lambda text: (text, ["5511777777777@s.whatsapp.net"])

        panel._apply_message_edit("oi @Ana", "grupo@g.us")

        # Last refresh reflects the restored message, without the new mention.
        assert panel.mentions_panels[-1] == []

    def test_an_edit_whose_echo_came_before_a_timeout_stays_applied(self):
        """The echo of a successful edit is consumed before a slow HTTP
        response times out; a timeout must therefore not roll back."""
        panel = _Panel([_own(60)], edit_result=None)
        panel._apply_message_edit("depois", "grupo@g.us")
        assert panel._sorted_messages[0]["message"] == {"conversation": "depois"}
        assert panel.main_window.spoken == []


class TestEditMessageReportsTheOutcome:
    class _Resp:
        def __init__(self, code, text="Cannot edit this message"):
            self.status_code = code
            self.text = text

    class _Stub:
        def __init__(self):
            self.wpp_server = "http://127.0.0.1"
            self.wpp_port = 6300
            self.token = "t"
            self._phone_to_lid = {}
            self.chats = {}

        def _serialize_msg_id(self, jid, key):
            return f"true_{jid}_{key['id']}"

    @pytest.mark.parametrize("code, body, expected", [
        (201, "", True),
        (200, "", True),
        # Refused before anything was sent — the measured WhatsApp answer.
        (500, '{"error":{"message":"Cannot edit this message"}}', False),
        # Any other error may come after the edit went out: unknown.
        (500, '{"error":{"message":"something else"}}', None),
    ])
    def test_http_status(self, monkeypatch, code, body, expected):
        from main import MainWindow
        patch_main_global(monkeypatch, "api_post", lambda *a, **k: self._Resp(code, body))
        assert MainWindow.edit_message(self._Stub(), "g@g.us", "m1", "x") is expected

    def test_a_read_timeout_is_unknown_not_a_refusal(self, monkeypatch):
        from requests.exceptions import ReadTimeout

        from main import MainWindow

        def slow(*a, **k):
            raise ReadTimeout("slow")

        patch_main_global(monkeypatch, "api_post", slow)
        assert MainWindow.edit_message(self._Stub(), "g@g.us", "m1", "x") is None

    def test_an_offline_session_is_a_refusal(self, monkeypatch):
        """statusConnection.ts answers before any controller runs — nothing was
        sent, so the optimistic edit must come back off the row."""
        from main import MainWindow
        body = '{"status":"Disconnected","message":"A sessão do WhatsApp não está ativa."}'
        patch_main_global(monkeypatch, "api_post", lambda *a, **k: self._Resp(404, body))
        assert MainWindow.edit_message(self._Stub(), "g@g.us", "m1", "x") is False

    def test_a_refused_connection_is_a_failure(self, monkeypatch):
        from requests.exceptions import ConnectionError as ReqConnectionError
        from urllib3.exceptions import MaxRetryError, NewConnectionError

        from main import MainWindow

        def down(*a, **k):
            raise ReqConnectionError(MaxRetryError(
                None, "/api/edit-message", reason=NewConnectionError(None, "refused")))

        patch_main_global(monkeypatch, "api_post", down)
        assert MainWindow.edit_message(self._Stub(), "g@g.us", "m1", "x") is False

    def test_a_connect_timeout_is_a_failure(self, monkeypatch):
        from requests.exceptions import ConnectTimeout

        from main import MainWindow

        def slow_connect(*a, **k):
            raise ConnectTimeout("no socket")

        patch_main_global(monkeypatch, "api_post", slow_connect)
        assert MainWindow.edit_message(self._Stub(), "g@g.us", "m1", "x") is False

    def test_a_connection_dropped_after_sending_is_unknown(self, monkeypatch):
        """RemoteDisconnected also arrives as requests' ConnectionError, after
        the edit may already be on WhatsApp — not proof of a refusal."""
        from http.client import RemoteDisconnected

        from requests.exceptions import ConnectionError as ReqConnectionError
        from urllib3.exceptions import ProtocolError

        from main import MainWindow

        def dropped(*a, **k):
            raise ReqConnectionError(ProtocolError(
                "Connection aborted.", RemoteDisconnected("closed")))

        patch_main_global(monkeypatch, "api_post", dropped)
        assert MainWindow.edit_message(self._Stub(), "g@g.us", "m1", "x") is None
