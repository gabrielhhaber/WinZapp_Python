"""Polls: a new poll's validation, how a received one is read and shown, and
how one is sent.

WhatsApp Web's type for a poll is ``poll_creation`` (wa-js's
``sendCreatePollMessage`` builds exactly that). The normalizer used to wait for
``pollCreation``, so every real poll fell through to "Mensagem incompatível".

ConversationsPanel and MainWindow need a wx.App, so their methods run against
plain stubs.
"""

import types

import pytest

from core import polls
from core.message_queue import PendingMessage
from core.websocket_client import WebSocketClient
from main import MainWindow
from tests.locales import load_strings, registered_locale_codes
from ui.conversations import ConversationsPanel

PHONE = "5511999999999@s.whatsapp.net"
GROUP = "120363000000000001@g.us"


class _I18n:
    def __init__(self, strings=None):
        self._strings = strings or {
            "poll_option_votes": "{name}, {count} votes",
            "poll_option_vote_one": "{name}, {count} vote",
            "poll_option_votes_percent": "{name}, {count} votes ({percent}%)",
            "poll_option_vote_one_percent": "{name}, {count} vote ({percent}%)",
            "poll_voter_row": "{name}: {options}",
            "poll_voter_hidden": "Hidden name",
            "sender_you": "You",
            "poll_row_question": "Poll: {name}",
            "poll_row_options": "{question}. Options: {options}",
            "notif_poll_no_name": "Poll",
        }

    def t(self, key):
        return self._strings.get(key, key)


def _raw_poll(**kw):
    raw = {
        "id": f"false_{PHONE}_3EB0POLL",
        "type": "poll_creation",
        "t": 1790222611,
        "from": PHONE,
        "to": "5500000000000@s.whatsapp.net",
        "fromMe": False,
        "pollName": "Pizza ou sushi?",
        "pollOptions": [{"name": "Pizza", "localId": 0}, {"name": "Sushi", "localId": 1}],
        "pollSelectableOptionsCount": 1,
    }
    raw.update(kw)
    return raw


class _Normalizer:
    _normalize_wpp_message = WebSocketClient._normalize_wpp_message
    _clean_jid = WebSocketClient._clean_jid


class TestValidation:
    def test_a_good_poll_has_no_error(self):
        assert polls.poll_input_error("Q?", ["a", "b"]) is None

    def test_the_question_is_required(self):
        assert polls.poll_input_error("   ", ["a", "b"]) == "poll_error_no_question"

    def test_blank_options_do_not_count(self):
        assert polls.poll_input_error("Q?", ["a", "  ", ""]) == "poll_error_too_few_options"

    def test_twelve_options_is_the_limit(self):
        assert polls.poll_input_error("Q?", [str(i) for i in range(12)]) is None
        assert (
            polls.poll_input_error("Q?", [str(i) for i in range(13)])
            == "poll_error_too_many_options"
        )

    def test_duplicates_are_refused_ignoring_case(self):
        assert polls.poll_input_error("Q?", ["Sim", " sim "]) == "poll_error_duplicate_option"

    def test_input_is_trimmed(self):
        assert polls.clean_poll_input(" Q? ", [" a ", "", "b "]) == ("Q?", ["a", "b"])

    def test_selectable_count(self):
        assert polls.selectable_count(False) == 1
        assert polls.selectable_count(True) == 0


class TestSendPayload:
    def test_shape_matches_the_node_route(self):
        payload = polls.poll_send_payload(
            PHONE, {"name": "Q?", "options": ["a", "b"], "selectableCount": 0},
            is_group=False, is_lid=False,
        )
        assert payload == {
            "phone": [PHONE], "isGroup": False, "isLid": False,
            "name": "Q?", "choices": ["a", "b"], "options": {"selectableCount": 0},
        }


class TestNormalizer:
    def test_poll_creation_becomes_a_poll_message(self):
        result = _Normalizer()._normalize_wpp_message(_raw_poll())
        assert result["messageType"] == "pollCreationMessage"
        assert result["message"]["pollCreationMessage"] == {
            "name": "Pizza ou sushi?",
            "options": [{"name": "Pizza", "localId": 0}, {"name": "Sushi", "localId": 1}],
            "selectableOptionsCount": 1,
        }

    def test_a_missing_selectable_count_stays_unknown(self):
        raw = _raw_poll()
        del raw["pollSelectableOptionsCount"]
        poll = _Normalizer()._normalize_wpp_message(raw)["message"]["pollCreationMessage"]
        assert "selectableOptionsCount" not in poll

    def test_the_old_type_name_still_works(self):
        result = _Normalizer()._normalize_wpp_message(_raw_poll(type="pollCreation"))
        assert result["messageType"] == "pollCreationMessage"

    def test_a_multiple_choice_poll_is_read_as_such(self):
        result = _Normalizer()._normalize_wpp_message(_raw_poll(pollSelectableOptionsCount=0))
        poll = result["message"]["pollCreationMessage"]
        assert polls.allows_multiple(poll) is True
        assert polls.allows_multiple({"selectableOptionsCount": 1}) is False
        assert polls.allows_multiple({}) is None


class TestRow:
    POLL = {"name": "Q?", "options": [{"name": "a", "votes": 0}, {"name": "b", "votes": 3}]}

    def test_question_first_then_options_most_voted_first(self):
        assert polls.poll_row_text(_I18n(), self.POLL) == (
            "Poll: Q?. Options: b, 3 votes; a, 0 votes"
        )

    def test_only_exactly_one_vote_is_singular(self):
        i18n = _I18n()
        label = lambda n: polls.option_label(i18n, {"name": "a", "votes": n})
        assert [label(0), label(1), label(2)] == ["a, 0 votes", "a, 1 vote", "a, 2 votes"]

    def test_unknown_votes_are_never_spoken_as_zero(self):
        poll = {"name": "Q?", "options": [{"name": "a"}, {"name": "b"}]}
        assert polls.poll_row_text(_I18n(), poll) == "Poll: Q?. Options: a; b"

    def test_a_poll_without_options_still_reads_as_a_poll(self):
        assert polls.poll_row_text(_I18n(), {"name": "Q?"}) == "Poll: Q?"
        assert polls.poll_row_text(_I18n(), {}) == "Poll"

    def test_the_message_list_uses_it(self):
        stub = type("Panel", (), {
            "_get_message_content": ConversationsPanel._get_message_content,
            "main_window": types.SimpleNamespace(i18n=_I18n()),
        })()
        msg = {
            "key": {"fromMe": False},
            "messageType": "pollCreationMessage",
            "message": {"pollCreationMessage": self.POLL},
        }
        assert stub._get_message_content(msg) == "Poll: Q?. Options: b, 3 votes; a, 0 votes"


class TestQueue:
    def test_pending_message_carries_the_poll(self):
        pm = PendingMessage("id", PHONE, poll={"name": "Q?", "options": ["a", "b"]})
        assert pm.poll["name"] == "Q?"


class _Response:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class _Sender:
    send_poll_message = MainWindow.send_poll_message
    _classify_send_exception = MainWindow._classify_send_exception

    def __init__(self, resolved=PHONE):
        self.wpp_server = "http://127.0.0.1"
        self.wpp_port = 6300
        self.token = "tok"
        self.i18n = _I18n()
        self._resolved = resolved

    def _resolve_jid_for_send(self, jid):
        return self._resolved

    def _legacy_phone_for_send(self, jid):
        return jid.replace("@lid", "@c.us")

    def _check_wa_connection_closed(self, response):
        return False


def _post(monkeypatch, *responses):
    calls = []
    queue = list(responses)

    def fake(url, **kwargs):
        calls.append((url, kwargs.get("json")))
        return queue.pop(0) if len(queue) > 1 else queue[0]

    from main_window import poll_sending
    monkeypatch.setattr(poll_sending, "api_post", fake)
    return calls


POLL = {"name": "Q?", "options": ["a", "b"], "selectableCount": 1}


class TestSending:
    def test_a_confirmed_send_returns_the_message_id(self, monkeypatch):
        calls = _post(monkeypatch, _Response(201, {
            "status": "success", "response": [{"id": "true_x_3EB0ABC", "ack": 0}],
        }))
        assert _Sender().send_poll_message(PHONE, POLL) == "3EB0ABC"
        assert calls[0][0].endswith("/send-poll-message")
        assert calls[0][1]["choices"] == ["a", "b"]

    def test_a_group_goes_without_its_suffix(self, monkeypatch):
        calls = _post(monkeypatch, _Response(201, {
            "status": "success", "response": [{"id": "true_x_3EB0ABC"}],
        }))
        _Sender(GROUP).send_poll_message(GROUP, POLL)
        assert calls[0][1]["phone"] == ["120363000000000001"]
        assert calls[0][1]["isGroup"] is True

    def test_an_audited_rejection_is_permanent(self, monkeypatch):
        _post(monkeypatch, _Response(201, {
            "status": "success",
            "response": [{"id": "x", "ack": -1, "error": "send-poll-message was rejected (ack=-1)"}],
        }))
        result = _Sender().send_poll_message(PHONE, POLL)
        assert result["ok"] is False and result["retry"] is False

    def test_a_server_error_is_never_resent(self, monkeypatch):
        calls = _post(monkeypatch, _Response(500, {"status": "error"}))
        result = _Sender().send_poll_message(PHONE, POLL)
        assert result["ambiguous"] is True and result["retry"] is False
        assert len(calls) == 1

    def test_a_refused_lid_falls_back_to_the_phone_form(self, monkeypatch):
        calls = _post(
            monkeypatch,
            _Response(400, {"status": "error"}),
            _Response(201, {"status": "success", "response": [{"id": "true_x_3EB0ABC"}]}),
        )
        lid = "10000000000001@lid"
        assert _Sender(lid).send_poll_message(lid, POLL) == "3EB0ABC"
        assert calls[1][1]["phone"] == ["10000000000001@c.us"]
        assert calls[1][1]["isLid"] is False


@pytest.mark.parametrize("locale", registered_locale_codes())
def test_every_locale_has_the_poll_strings(locale):
    strings = load_strings(locale)
    for key in (
        "attachment_poll", "new_poll_title", "poll_option_votes", "poll_row_question",
        "poll_option_vote_one", "poll_row_options", "poll_dialog_title", "poll_error_duplicate_option",
    ):
        assert strings.get(key), (locale, key)
    assert "{count}" in strings["poll_option_votes"] and "{name}" in strings["poll_option_votes"]


class TestVoteBars:
    OPTIONS = [{"name": "a", "votes": 3}, {"name": "b", "votes": 1}, {"name": "c", "votes": 0}]

    def test_percent_is_the_share_of_all_votes(self):
        assert [polls.vote_percent(o, self.OPTIONS) for o in self.OPTIONS] == [75, 25, 0]

    def test_no_votes_is_zero_everywhere(self):
        empty = [{"name": "a", "votes": 0}]
        assert polls.vote_percent(empty[0], empty) == 0

    def test_unknown_votes_have_no_percent(self):
        unknown = [{"name": "a", "votes": None}]
        assert polls.vote_percent(unknown[0], unknown) is None


class TestVotes:
    POLL = {"name": "Q?", "options": [{"name": "a", "localId": 0}, {"name": "b", "localId": 1}]}
    RESULT = {"voters": 2, "options": [
        {"name": "a", "localId": 0, "votes": 2, "mine": False},
        {"name": "b", "localId": 1, "votes": 0, "mine": True},
    ]}

    def test_applied_votes_become_known_counts(self):
        poll = {"name": "Q?", "options": [dict(o) for o in self.POLL["options"]]}
        assert polls.apply_vote_result(poll, self.RESULT) is True
        assert [(o["name"], o["votes"], o["mine"]) for o in polls.poll_options(poll)] == [
            ("a", 2, False), ("b", 0, True)]
        assert polls.apply_vote_result(poll, self.RESULT) is False  # nothing new

    def test_a_result_for_other_options_is_ignored(self):
        poll = {"name": "Q?", "options": [{"name": "x"}, {"name": "y"}]}
        assert polls.apply_vote_result(poll, self.RESULT) is False
        assert polls.poll_options(poll)[0]["votes"] is None

    def test_checked_names_become_local_ids(self):
        options = polls.poll_options(self.POLL)
        assert polls.vote_poll_ids(options, ["b"]) == [1]
        assert polls.vote_poll_ids(options, []) == []

    def test_options_without_a_local_id_are_numbered_in_order(self):
        assert [o["localId"] for o in polls.poll_options({"options": [{"name": "a"}, {"name": "b"}]})] == [0, 1]


class _VoteSender(_Sender):
    fetch_poll_votes = MainWindow.fetch_poll_votes
    fetch_poll_voters = MainWindow.fetch_poll_voters
    send_poll_vote = MainWindow.send_poll_vote

    def _serialize_msg_id(self, jid, key, msg=None):
        return f"false_{jid}_{key['id']}"


MSG = {"key": {"id": "3EB0POLL", "remoteJid": PHONE}}


class TestVoteRequests:
    def test_votes_come_back_keyed_by_the_plain_message_id(self, monkeypatch):
        calls = _post(monkeypatch, _Response(200, {"status": "success", "response": {
            f"false_{PHONE}_3EB0POLL": {"voters": 1, "options": []}}}))
        assert _VoteSender().fetch_poll_votes([MSG]) == {"3EB0POLL": {"voters": 1, "options": []}}
        assert calls[0][0].endswith("/poll-votes")
        assert calls[0][1] == {"messageIds": [f"false_{PHONE}_3EB0POLL"]}

    def test_an_unavailable_runtime_reads_as_no_votes_not_zero(self, monkeypatch):
        _post(monkeypatch, _Response(503, {"status": "error"}))
        assert _VoteSender().fetch_poll_votes([MSG]) == {}

    def test_a_vote_posts_the_chosen_local_ids(self, monkeypatch):
        calls = _post(monkeypatch, _Response(200, {"status": "success"}))
        assert _VoteSender().send_poll_vote(MSG, [1]) is True
        assert calls[0][0].endswith("/vote-poll")
        assert calls[0][1] == {"messageId": f"false_{PHONE}_3EB0POLL", "localIds": [1]}

    def test_a_refused_vote_is_a_failure(self, monkeypatch):
        _post(monkeypatch, _Response(503, {"status": "error"}))
        assert _VoteSender().send_poll_vote(MSG, [1]) is False


class _FakeList:
    def __init__(self, checked):
        self.checked = list(checked)

    def GetItemCount(self):
        return len(self.checked)

    def IsItemChecked(self, index):
        return self.checked[index]

    def CheckItem(self, index, state):
        self.checked[index] = state


class _Event:
    def __init__(self, index):
        self._index = index

    def GetIndex(self):
        return self._index

    def Skip(self):
        pass


def _inline_panel(checked, sent):
    from ui.conversation_panel.poll_inline import PollInlineMixin

    msg = {"key": {"id": "P"}, "message": {"pollCreationMessage": {
        "name": "Q", "selectableOptionsCount": 1,
        "options": [{"name": "A", "localId": 0}, {"name": "B", "localId": 1}]}}}
    panel = types.SimpleNamespace(
        _poll_inline_list=_FakeList(checked), _poll_inline_msg=msg,
        _poll_inline_order=["A", "B"], _poll_inline_loading=False,
        _poll_inline_jid=PHONE, _poll_inline_seq=0,
        conversation={"remoteJid": PHONE}, _sorted_messages=[msg],
        _hide_inline_poll=lambda: setattr(panel, "_poll_inline_msg", None),
        _restore_inline_ticks=lambda m: PollInlineMixin._restore_inline_ticks(panel, m),
        main_window=types.SimpleNamespace(
            i18n=_I18n(), speak_output=types.SimpleNamespace(output=lambda _t: None)),
        _send_poll_vote=lambda m, ids, done: sent.append(ids),
    )
    return panel, PollInlineMixin._on_inline_poll_checked


class TestInlinePollVote:
    def test_only_a_confirmed_single_answer_poll_is_voted_inline(self):
        from ui.conversation_panel.poll_inline import inline_poll_applies

        base = {"message": {"pollCreationMessage": {"name": "Q", "options": [{"name": "A"}]}}}
        single = {"message": {"pollCreationMessage": {
            "name": "Q", "options": [{"name": "A"}], "selectableOptionsCount": 1}}}
        several = {"message": {"pollCreationMessage": {
            "name": "Q", "options": [{"name": "A"}], "selectableOptionsCount": 0}}}
        assert inline_poll_applies(single)
        assert not inline_poll_applies(several)
        assert not inline_poll_applies(base)   # unknown: opens in the window
        assert not inline_poll_applies({**single, "_local_pending": True})
        assert not inline_poll_applies({"message": {"conversation": "hi"}})

    def test_ticking_an_option_votes_for_it_and_unticks_the_other(self):
        sent = []
        panel, handler = _inline_panel([True, True], sent)
        handler(panel, _Event(1))
        assert sent == [[1]]
        assert panel._poll_inline_list.checked == [False, True]

    def test_unticking_the_option_clears_the_vote(self):
        sent = []
        panel, handler = _inline_panel([False, False], sent)
        handler(panel, _Event(0))
        assert sent == [[]]

    def test_loading_the_list_never_casts_a_vote(self):
        sent = []
        panel, handler = _inline_panel([True, False], sent)
        panel._poll_inline_loading = True
        handler(panel, _Event(0))
        assert sent == []

    def test_a_poll_the_user_has_left_never_votes(self):
        sent = []
        panel, handler = _inline_panel([True, False], sent)
        panel._sorted_messages = []
        handler(panel, _Event(0))
        assert sent == []
        assert panel._poll_inline_msg is None

    def test_a_poll_of_another_conversation_never_votes(self):
        sent = []
        panel, handler = _inline_panel([True, False], sent)
        panel.conversation = {"remoteJid": GROUP}
        handler(panel, _Event(0))
        assert sent == []

    def test_a_failed_vote_restores_the_recorded_ticks(self):
        from ui.conversation_panel.poll_inline import PollInlineMixin

        done_calls = []
        panel, handler = _inline_panel([False, True], [])
        panel._send_poll_vote = lambda m, ids, done: done_calls.append(done)
        spoken = []
        panel.main_window.speak_output.output = spoken.append
        handler(panel, _Event(1))
        done_calls[0](False)
        assert spoken == ["poll_vote_failed"]
        assert panel._poll_inline_list.checked == [False, False]   # nothing recorded as mine

    def test_an_older_vote_result_is_ignored_once_a_newer_tick_started(self):
        done_calls = []
        spoken = []
        panel, handler = _inline_panel([True, False], [])
        panel._send_poll_vote = lambda m, ids, done: done_calls.append(done)
        panel.main_window.speak_output.output = spoken.append
        handler(panel, _Event(0))
        panel._poll_inline_list.checked = [False, True]
        handler(panel, _Event(1))
        done_calls[0](True)
        assert spoken == []
        done_calls[1](True)
        assert spoken == ["poll_vote_sent"]


class _TextList:
    def __init__(self, texts):
        self.texts = list(texts)
        self.writes = []

    def Freeze(self):
        pass

    def Thaw(self):
        pass

    def GetItemText(self, index):
        return self.texts[index]

    def SetItemText(self, index, text):
        self.writes.append(index)
        self.texts[index] = text


class TestInlineReload:
    def test_the_voted_row_shows_its_new_count_and_only_changed_rows_are_written(self):
        from ui.conversation_panel.poll_inline import PollInlineMixin

        msg = {"key": {"id": "P"}, "message": {"pollCreationMessage": {
            "name": "Q", "selectableOptionsCount": 1,
            "options": [{"name": "A", "localId": 0, "votes": 1},
                        {"name": "B", "localId": 1, "votes": 1}]}}}
        panel = types.SimpleNamespace(
            _poll_inline_list=_TextList(["A, 1 vote (50%)", "B, 1 vote (50%)"]),
            _poll_inline_msg=msg, _poll_inline_order=["A", "B"], _poll_inline_loading=False,
            main_window=types.SimpleNamespace(i18n=_I18n()),
        )
        msg["message"]["pollCreationMessage"]["options"][1]["votes"] = 2
        PollInlineMixin._reload_inline_poll(panel)
        assert panel._poll_inline_list.texts == ["A, 1 vote (33%)", "B, 2 votes (67%)"]
        assert panel._poll_inline_list.writes == [0, 1]
        PollInlineMixin._reload_inline_poll(panel)
        assert panel._poll_inline_list.writes == [0, 1]   # unchanged: nothing rewritten
        assert panel._poll_inline_loading is False


class TestPollInMessages:
    def test_same_id_same_conversation(self):
        from ui.conversation_panel.poll_inline import poll_in_messages

        msg = {"key": {"id": "P"}}
        assert poll_in_messages(msg, [{"key": {"id": "P"}}], PHONE, PHONE)
        assert not poll_in_messages(msg, [{"key": {"id": "X"}}], PHONE, PHONE)
        assert not poll_in_messages(msg, [{"key": {"id": "P"}}], GROUP, PHONE)
        assert not poll_in_messages({}, [{"key": {"id": "P"}}], PHONE, PHONE)


class TestOptionPercent:
    def test_the_share_is_in_the_label_and_only_one_is_singular(self):
        i18n = _I18n()
        assert polls.option_label(i18n, {"name": "A", "votes": 0}, 0) == "A, 0 votes (0%)"
        assert polls.option_label(i18n, {"name": "A", "votes": 1}, 25) == "A, 1 vote (25%)"
        assert polls.option_label(i18n, {"name": "A", "votes": 3}, 75) == "A, 3 votes (75%)"

    def test_unknown_votes_never_get_a_percent(self):
        assert polls.option_label(_I18n(), {"name": "A", "votes": None}, None) == "A"


class TestVoterRows:
    OPTIONS = [{"name": "Sim", "localId": 0}, {"name": "Não", "localId": 1}]

    def test_one_row_per_voter_ordered_by_name_with_the_user_as_you(self):
        voters = [
            {"sender": "g", "mine": False, "localIds": [0]},
            {"sender": "me", "mine": True, "localIds": [1]},
            {"sender": "a", "mine": False, "localIds": [0, 1]},
        ]
        rows = polls.voter_rows(_I18n(), voters, self.OPTIONS, {"g": "gustavo", "a": "Ana"}.get)
        assert rows == ["Ana: Sim, Não", "gustavo: Sim", "You: Não"]

    def test_a_voter_the_client_cannot_name_gets_the_name_the_page_knows(self):
        voters = [{"sender": "x@lid", "mine": False, "name": "Ana P", "localIds": [0]},
                  {"sender": "y@lid", "mine": False, "name": "", "localIds": [1]}]
        rows = polls.voter_rows(_I18n(), voters, self.OPTIONS, lambda s: "")
        assert rows == ["Ana P: Sim", "unknown_contact: Não"]

    def test_the_clients_own_name_wins_over_the_pages(self):
        voters = [{"sender": "x@lid", "mine": False, "name": "Ana P", "localIds": [0]}]
        assert polls.voter_rows(_I18n(), voters, self.OPTIONS, lambda s: "Ana") == ["Ana: Sim"]

    def test_voters_without_a_known_option_are_dropped(self):
        voters = [{"sender": "a", "mine": False, "localIds": [9]}, {"sender": "b", "localIds": []}]
        assert polls.voter_rows(_I18n(), voters, self.OPTIONS, lambda s: s) == []


class TestFetchVoters:
    def test_the_voters_come_back_for_one_poll(self, monkeypatch):
        calls = _post(monkeypatch, _Response(200, {"status": "success", "response": {
            "ok": True, "options": [], "voters": [{"sender": "x", "mine": False, "localIds": [0]}]}}))
        sender = _VoteSender()
        data = sender.fetch_poll_voters(MSG)
        assert data["voters"][0]["localIds"] == [0]
        assert calls[0][0].endswith("/poll-voters")
        assert calls[0][1] == {"messageId": f"false_{PHONE}_3EB0POLL"}

    def test_an_unavailable_runtime_gives_nothing(self, monkeypatch):
        _post(monkeypatch, _Response(503, {"status": "error"}))
        sender = _VoteSender()
        assert sender.fetch_poll_voters(MSG) == {}


def test_the_inline_poll_is_the_same_poll_by_id_not_by_dict():
    """Reviewer: each rebuild re-decides the focused row, and a sync replaces
    the record dicts; compared by identity, the list refilled and jumped to
    its first item while the user was in it."""
    from ui.conversation_panel.poll_inline import same_inline_poll
    old = {"key": {"id": "P1"}}
    fresh = {"key": {"id": "P1"}}
    assert same_inline_poll(old, "g@g.us", fresh, "g@g.us")
    assert not same_inline_poll(old, "g@g.us", {"key": {"id": "P2"}}, "g@g.us")
    assert not same_inline_poll(old, "g@g.us", fresh, "other@g.us")
    assert not same_inline_poll(None, "g@g.us", fresh, "g@g.us")
    assert not same_inline_poll({"key": {}}, "g@g.us", {"key": {}}, "g@g.us")


# -- hide voter names and time limit -----------------------------------------


def test_time_limit_choices_start_with_no_limit_and_map_to_seconds():
    assert polls.TIME_LIMITS[0] == ("poll_time_limit_none", 0)
    assert [polls.time_limit_seconds(i) for i in range(len(polls.TIME_LIMITS))] == [
        0, 3600, 8 * 3600, 24 * 3600, 3 * 86400, 7 * 86400]
    assert polls.time_limit_seconds(-1) == polls.time_limit_seconds(99) == 0
    assert polls.time_limit_seconds(None) == 0


def test_send_body_only_carries_the_settings_that_were_asked_for():
    plain = polls.poll_send_payload(PHONE, {"name": "Q", "options": ["a", "b"]},
                                    is_group=False, is_lid=False)
    assert plain["options"] == {"selectableCount": 1}
    both = polls.poll_send_payload(
        PHONE, {"name": "Q", "options": ["a", "b"], "hideVoterNames": True, "durationSeconds": 3600},
        is_group=False, is_lid=False)
    assert both["options"] == {"selectableCount": 1, "hideVoterNames": True, "durationSeconds": 3600}
    off = polls.poll_send_payload(
        PHONE, {"name": "Q", "options": ["a", "b"], "hideVoterNames": False, "durationSeconds": 0},
        is_group=False, is_lid=False)
    assert off["options"] == {"selectableCount": 1}


def test_a_received_poll_keeps_hidden_names_and_end_time():
    poll = polls.poll_message_from_wpp(_raw_poll(pollHideVoterNames=True, pollEndTime=1893456000000))
    assert polls.hides_voter_names(poll) is True
    assert poll["endTime"] == 1893456000000
    plain = polls.poll_message_from_wpp(_raw_poll())
    assert polls.hides_voter_names(plain) is False and "endTime" not in plain


def test_voters_of_a_hiding_poll_read_as_hidden_name_but_you_stay_you():
    i18n = _I18n({"sender_you": "You", "poll_voter_hidden": "Hidden name",
                  "poll_voter_row": "{name}: {options}"})
    options = [{"name": "Sim", "localId": 0}, {"name": "Não", "localId": 1}]
    voters = [{"sender": "g", "mine": False, "name": "Gus", "localIds": [0]},
              {"sender": "me", "mine": True, "localIds": [1]}]
    rows = polls.voter_rows(i18n, voters, options, {"g": "gustavo"}.get, hide_names=True)
    assert rows == ["Hidden name: Sim", "You: Não"]
    assert "gustavo" not in " ".join(rows) and "Gus" not in " ".join(rows)
