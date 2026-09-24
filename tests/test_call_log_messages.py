"""Calls shown as messages in the conversation and the chat list.

WhatsApp Web keeps each call as a ``call_log`` message (see core/call_log.py).
Before this, the normalizer had no branch for it: every call reached the list as
"Mensagem incompatível" with its outcome and duration dropped (45 such rows in
one log.log on 2026-09-24).

ConversationsPanel and MainWindow need a wx.App, so their methods run against
plain stubs.
"""

import time

import main as main_module
from core.websocket_client import WebSocketClient
from main import MainWindow, is_countable_message
from ui.conversations import ConversationsPanel

PEER_LID = "68904344899801@lid"
PHONE = "5511999999999@s.whatsapp.net"
ME_LID = "242558294872167@lid"


class _Normalizer:
    _normalize_wpp_message = WebSocketClient._normalize_wpp_message
    _clean_jid = WebSocketClient._clean_jid


def _raw_call(outcome="Missed", from_me=False, duration=0, video=False,
              final=None, call_id="00350D7FFB27065CBC5DF80E49E6B7CF"):
    """A serialized call_log as WPPConnect's getMessages returns it (measured)."""
    return {
        "id": f"{'true' if from_me else 'false'}_{PEER_LID}_{call_id}",
        "type": "call_log", "t": 1790222611, "timestamp": 1790222611,
        "from": ME_LID if from_me else PEER_LID, "to": PEER_LID if from_me else ME_LID,
        "fromMe": from_me, "invis": True,
        "callOutcome": outcome, "finalCallOutcome": final, "isVideoCall": video,
        "callDuration": duration, "callSilenceReason": None,
        "callCreator": {"server": "lid", "user": "242558294872167", "device": 1,
                        "_serialized": "242558294872167:1@lid"},
        "callParticipants": [
            {"participant": {"_serialized": PEER_LID}, "outcome": 3},
            {"participant": {"_serialized": ME_LID}, "outcome": 7},
        ],
        "chatId": {"server": "lid", "user": "68904344899801", "_serialized": PEER_LID},
    }


def _normalized(**kw):
    return _Normalizer()._normalize_wpp_message(_raw_call(**kw))


class _I18n:
    def t(self, key):
        return f"[{key}]"


class _MW:
    app_name = "WinZapp"

    def __init__(self):
        self.i18n = _I18n()
        self.calls = []
        self.spoken = []

    def start_voice_call(self, jid, name=""):
        self.calls.append(("voice", jid, name))

    def start_video_call(self, jid, name=""):
        self.calls.append(("video", jid, name))

    def output(self, text, interrupt=False):
        self.spoken.append(text)


class _Button:
    def __init__(self):
        self.shown = False

    def Show(self, show=True):
        self.shown = bool(show)

    def Hide(self):
        self.shown = False


class _Layoutable:
    def Layout(self):
        pass


class _List:
    def __init__(self, selected):
        self.selected = selected

    def GetFirstSelected(self):
        return self.selected


class _Panel:
    _get_message_content = ConversationsPanel._get_message_content
    _is_displayable_message = ConversationsPanel._is_displayable_message
    _is_system_event = staticmethod(ConversationsPanel._is_system_event)
    _reject_system_event_action = ConversationsPanel._reject_system_event_action
    _format_duration = ConversationsPanel._format_duration
    _update_return_call_button = ConversationsPanel._update_return_call_button
    _return_call = ConversationsPanel._return_call
    _on_return_call = ConversationsPanel._on_return_call
    _on_accel_react = ConversationsPanel._on_accel_react
    _on_menu_react = ConversationsPanel._on_menu_react
    _on_menu_reply = ConversationsPanel._on_menu_reply
    _is_separator = staticmethod(lambda m: isinstance(m, dict) and m.get("_type") == "separator")

    def __init__(self, messages=(), jid="5511999999999@s.whatsapp.net"):
        self.main_window = _MW()
        self._download_progress = {}
        self._sorted_messages = list(messages)
        self.conversation = {"remoteJid": jid, "name": "Maria"}
        self.conversation_name = "Maria"
        self._return_call_btn = _Button()
        self._return_call_msg = None
        self.conversation_panel = _Layoutable()
        self.messages_list = _List(0)


class TestNormalizer:
    def test_a_call_becomes_a_call_log_message(self):
        result = _normalized(outcome="Completed", duration=2241, from_me=True)
        assert result["messageType"] == "callLogMessage"
        assert result["message"] == {"callLogMessage": {
            "outcome": "Completed", "isVideo": False, "durationSeconds": 2241,
            "isGroupCall": False,
        }}
        assert result["key"]["id"] == "00350D7FFB27065CBC5DF80E49E6B7CF"
        assert result["key"]["fromMe"] is True

    def test_a_video_call_keeps_its_kind(self):
        assert _normalized(video=True)["message"]["callLogMessage"]["isVideo"] is True


class TestConversationRow:
    def test_it_is_a_row(self):
        assert _Panel()._is_displayable_message(_normalized()) is True

    def test_a_legacy_record_is_a_row_too(self):
        legacy = {"key": {"id": "X"}, "messageType": "call_log", "message": {}}
        assert _Panel()._is_displayable_message(legacy) is True
        assert _Panel()._get_message_content(legacy) == "[call_log_generic]"

    def test_a_missed_call_reads_as_such(self):
        assert _Panel()._get_message_content(_normalized()) == "[call_log_missed_voice]"

    def test_an_answered_call_reads_its_duration_like_a_voice_message(self):
        text = _Panel()._get_message_content(_normalized(outcome="Completed", duration=1859))
        assert text == "[call_log_answered_voice], [duration]: 30 [minutes] [and] 59 [seconds]"

    def test_it_takes_no_sender_prefix(self):
        """A system event: the sentence says what happened, no "Maria: ..."."""
        assert _Panel._is_system_event(_normalized()) is True


class TestChatList:
    def test_it_can_be_the_chat_preview_but_never_counts(self):
        msg = _normalized()
        assert MainWindow._counts_as_last_message(msg) is True
        assert is_countable_message(msg) is False

    def test_the_preview_reads_the_row_sentence_without_a_self_prefix(self):
        class _MWStub:
            _counts_as_last_message = classmethod(MainWindow._counts_as_last_message.__func__)
            _last_msg_preview = MainWindow._last_msg_preview
            _PREVIEW_MESSAGE_TYPES = MainWindow._PREVIEW_MESSAGE_TYPES

            def __init__(self):
                self.i18n = _I18n()
                self.settings = {"user_interface": {"show_delivery_status_in_chat_list": False}}
                self.conversations_panel = _Panel()

            def self_reference_label(self):
                return "Eu"

        msg = _normalized(outcome="Missed", from_me=True)
        chat = {"remoteJid": "5511@s.whatsapp.net",
                "messages": {"messages": {"records": [msg]}}}
        preview = _MWStub()._last_msg_preview(chat)
        assert preview.startswith("[call_log_unanswered_voice]")
        assert "Eu:" not in preview


class TestReturnCall:
    def test_the_button_shows_only_on_a_missed_call(self):
        panel = _Panel([_normalized(), _normalized(outcome="Completed", duration=3)])
        panel._update_return_call_button(0)
        assert panel._return_call_btn.shown is True
        panel._update_return_call_button(1)
        assert panel._return_call_btn.shown is False
        assert panel._return_call_msg is None

    def test_the_button_hides_on_an_ordinary_message(self):
        text = {"key": {"id": "T"}, "messageType": "conversation",
                "message": {"conversation": "oi"}}
        panel = _Panel([_normalized(), text])
        panel._update_return_call_button(0)
        panel._update_return_call_button(1)
        assert panel._return_call_btn.shown is False

    def test_not_in_a_group(self):
        panel = _Panel([_normalized()], jid="1203@g.us")
        panel._update_return_call_button(0)
        assert panel._return_call_btn.shown is False

    def test_the_button_calls_back_with_the_same_kind(self):
        panel = _Panel([_normalized(video=True)])
        panel._update_return_call_button(0)
        panel._on_return_call()
        assert panel.main_window.calls == [("video", "5511999999999@s.whatsapp.net", "Maria")]

    def test_ctrl_shift_r_returns_a_missed_call(self):
        panel = _Panel([_normalized()])
        panel._on_accel_react(None)
        assert panel.main_window.calls == [("voice", "5511999999999@s.whatsapp.net", "Maria")]

    def test_ctrl_shift_r_on_another_call_refuses_the_reaction(self):
        panel = _Panel([_normalized(outcome="Completed", duration=3)])
        panel._on_accel_react(None)
        assert panel.main_window.calls == []
        assert panel.main_window.spoken == ["[call_log_action_unavailable]"]

    def test_reply_is_refused_with_the_call_wording(self):
        panel = _Panel([_normalized()])
        panel._on_menu_reply(_normalized())
        assert panel.main_window.spoken == ["[call_log_action_unavailable]"]


# ── MainWindow: the record is followed until WhatsApp settles its outcome ──

class _Watcher:
    _watch_ended_call_log = MainWindow._watch_ended_call_log
    _watch_pending_call_log = MainWindow._watch_pending_call_log
    _CALL_LOG_AFTER_END_WATCH_SECONDS = MainWindow._CALL_LOG_AFTER_END_WATCH_SECONDS
    _CALL_LOG_PENDING_WATCH_SECONDS = MainWindow._CALL_LOG_PENDING_WATCH_SECONDS

    def __init__(self):
        self.started = []
        self._phone_to_lid = {"5511@s.whatsapp.net": "9@lid"}
        self._lid_to_phone = {"9@lid": "5511@s.whatsapp.net"}

    def _normalize_jid(self, jid):
        return jid.replace("@c.us", "@s.whatsapp.net")

    def _serialize_msg_id(self, remote_jid, key, full_msg=None):
        return f"{'true' if key.get('fromMe') else 'false'}_{remote_jid}_{key.get('id')}"

    def _start_call_log_watch(self, candidates, window, chat_jid=""):
        self.started.append((candidates, window, chat_jid))


class TestWatch:
    def test_an_ended_call_is_looked_up_under_every_form_of_the_peer(self):
        w = _Watcher()
        w._watch_ended_call_log("CALLID", "5511@c.us", True)
        assert w.started == [(["true_9@lid_CALLID", "true_5511@c.us_CALLID"], 600,
                              "5511@s.whatsapp.net")]

    def test_a_peer_named_by_its_lid_is_also_looked_up_by_phone(self):
        """Review: incoming-call events often carry the @lid."""
        w = _Watcher()
        w._watch_ended_call_log("CALLID", "9@lid", False)
        assert w.started == [(["false_9@lid_CALLID", "false_5511@c.us_CALLID"], 600,
                              "5511@s.whatsapp.net")]

    def test_an_unmapped_lid_stays_the_chat(self):
        w = _Watcher()
        w._watch_ended_call_log("CALLID", "7@lid", False)
        assert w.started == [(["false_7@lid_CALLID"], 600, "7@lid")]

    def test_not_for_a_group_or_without_a_real_call_id(self):
        w = _Watcher()
        w._watch_ended_call_log("CALLID", "1@g.us", False)
        w._watch_ended_call_log("", "5511@c.us", False)
        w._watch_ended_call_log("outgoing:5511@s.whatsapp.net", "5511@c.us", True)
        assert w.started == []

    def test_a_recent_ongoing_record_is_followed(self):
        w = _Watcher()
        msg = _normalized(outcome="Ongoing")
        msg["messageTimestamp"] = int(time.time()) - 60
        w._watch_pending_call_log(PEER_LID, msg)
        assert w.started == [([f"false_{PEER_LID}_00350D7FFB27065CBC5DF80E49E6B7CF"], 10800,
                              PEER_LID)]

    def test_an_old_or_settled_record_is_not(self):
        w = _Watcher()
        old = _normalized(outcome="Ongoing")
        old["messageTimestamp"] = int(time.time()) - 4 * 3600
        w._watch_pending_call_log(PEER_LID, old)
        w._watch_pending_call_log(PEER_LID, _normalized(outcome="Missed"))
        assert w.started == []


class _Response:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


class TestFetch:
    class _Stub:
        _fetch_call_log_record = MainWindow._fetch_call_log_record
        wpp_server = "http://127.0.0.1"
        wpp_port = 6300
        token = "sess"

    def test_the_first_id_whatsapp_knows_wins(self, monkeypatch):
        asked = []

        def fake_get(url, **kw):
            asked.append(url)
            if len(asked) == 1:
                return _Response(500, {"status": "error"})
            return _Response(201, {"response": {"data": _raw_call()}})

        monkeypatch.setattr(main_module, "api_get", fake_get)
        raw = self._Stub()._fetch_call_log_record(["false_9@lid_X", "false_5511@c.us_X"])
        assert raw["type"] == "call_log"
        assert asked[1].endswith("/api/sess/message-by-id/false_5511@c.us_X")

    def test_a_record_that_is_not_a_call_is_ignored(self, monkeypatch):
        monkeypatch.setattr(main_module, "api_get", lambda url, **kw: _Response(
            201, {"response": {"data": {"type": "chat"}}}))
        assert self._Stub()._fetch_call_log_record(["false_9@lid_X"]) is None


class TestWatchLoop:
    """_start_call_log_watch() with its thread run inline and no real waits."""

    class _Stub:
        _start_call_log_watch = MainWindow._start_call_log_watch

        def __init__(self, answers):
            self.answers = list(answers)
            self.fetches = 0
            self.ws = _Normalizer()

        def _fetch_call_log_record(self, candidates):
            self.fetches += 1
            return self.answers.pop(0) if self.answers else None

        def on_historical_message(self, msg):
            pass

    def _run(self, monkeypatch, stub, window=600):
        delivered = []

        class _InlineThread:
            def __init__(self, target, **kw):
                self.target = target

            def start(self):
                self.target()

        monkeypatch.setattr(main_module.threading, "Thread", _InlineThread)
        monkeypatch.setattr(main_module.time, "sleep", lambda s: None)
        monkeypatch.setattr(main_module.wx, "CallAfter",
                            lambda fn, msg: delivered.append(msg))
        stub._start_call_log_watch(["false_9@lid_X"], window, chat_jid=PHONE)
        return delivered

    def test_stops_once_the_outcome_is_settled(self, monkeypatch):
        stub = self._Stub([None, _raw_call(outcome="Ongoing"), _raw_call(outcome="Completed", duration=9)])
        delivered = self._run(monkeypatch, stub)
        assert [m["message"]["callLogMessage"]["outcome"] for m in delivered] == [
            "Ongoing", "Completed"]
        assert stub.fetches == 3
        assert stub._watched_call_logs == {}

    def test_every_copy_is_filed_under_the_chat_it_belongs_in(self, monkeypatch):
        stub = self._Stub([_raw_call(outcome="Missed")])
        (delivered,) = self._run(monkeypatch, stub)
        assert delivered["key"]["remoteJid"] == PHONE
        assert delivered["key"]["remoteJidAlt"] == PEER_LID

    def test_gives_up_at_the_end_of_its_window(self, monkeypatch):
        stub = self._Stub([])
        assert self._run(monkeypatch, stub, window=50) == []
        assert stub.fetches == 3  # 3 + 10 + 30 s fit in 50 s, the 90 s wait does not


# ── The settled record replaces the stored one, in the phone chat ──────────

class _Executor:
    def submit(self, fn):
        return None


class _HistoricalStub:
    """on_historical_message() for real, as in test_historical_self_chat_guard."""
    on_historical_message = MainWindow.on_historical_message
    _fill_stored_placeholder = MainWindow._fill_stored_placeholder
    _adopt_decrypted_copy = staticmethod(MainWindow._adopt_decrypted_copy)
    _is_undecrypted_placeholder = staticmethod(MainWindow._is_undecrypted_placeholder)
    _recover_quoted_placeholder = MainWindow._recover_quoted_placeholder
    _recover_placeholders_from_replies = MainWindow._recover_placeholders_from_replies
    _drop_protocol_edit = MainWindow._drop_protocol_edit
    _redirect_self_chat_artifact = MainWindow._redirect_self_chat_artifact
    _phone_digits_equivalent = staticmethod(MainWindow._phone_digits_equivalent)
    _is_self_jid = MainWindow._is_self_jid

    def __init__(self):
        self.my_jid = "5500000000000@s.whatsapp.net"
        self.my_lid = ME_LID
        self._lid_to_phone = {}
        self.chats = {}
        self._msg_bg_executor = _Executor()
        self.watched = []

    def _normalize_jid(self, jid):
        return MainWindow._normalize_jid(jid)

    def _live_events_ready(self):
        return True

    def _extract_lid_mapping(self, msg):
        pass

    def _learn_sender_name(self, msg):
        return False

    def _apply_group_subject_change(self, remote_jid, chat, msg):
        pass

    def _fill_group_name(self, remote_jid):
        return ""

    def _is_cleared_message(self, remote_jid, msg):
        return False

    def _schedule_save(self, dirty_jid=None):
        pass

    def _schedule_set_chats(self):
        pass

    def _watch_pending_call_log(self, remote_jid, msg):
        self.watched.append(remote_jid)


class TestSettledRecordReplacesTheStoredOne:
    def _stub_with_ongoing_record(self):
        from core.call_log import refile_call_log
        stub = _HistoricalStub()
        ongoing = refile_call_log(_normalized(outcome="Ongoing", from_me=True), PHONE)
        stub.on_historical_message(ongoing)
        assert list(stub.chats) == [PHONE]
        return stub

    def test_no_second_chat_and_the_row_settles(self):
        """Review: the copy fetched by id names the @lid; filed as-is it opened
        a nameless @lid chat while the phone chat kept "em andamento"."""
        from core.call_log import refile_call_log
        stub = self._stub_with_ongoing_record()

        settled = refile_call_log(
            _normalized(outcome="Completed", from_me=True, duration=42), PHONE)
        stub.on_historical_message(settled)

        assert list(stub.chats) == [PHONE]
        (record,) = stub.chats[PHONE]["messages"]["messages"]["records"]
        assert record["message"]["callLogMessage"]["outcome"] == "Completed"
        assert record["message"]["callLogMessage"]["durationSeconds"] == 42

    def test_an_identical_copy_changes_nothing(self):
        from core.call_log import refile_call_log
        stub = self._stub_with_ongoing_record()
        before = dict(stub.chats[PHONE]["messages"]["messages"]["records"][0])

        stub.on_historical_message(
            refile_call_log(_normalized(outcome="Ongoing", from_me=True), PHONE))

        assert stub.chats[PHONE]["messages"]["messages"]["records"] == [before]
