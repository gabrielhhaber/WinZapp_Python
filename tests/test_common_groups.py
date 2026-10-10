"""Groups in common for the chat-info dialog (core/common_groups.py).

The server route is never reached: api_get is replaced by a script of canned
answers, so these tests pin how the client reads and combines them.
"""

import logging

import pytest

from core import common_groups
from core.common_groups import (
    common_group_rows,
    fetch_common_groups,
    group_jids_from_reply,
    wid_candidates,
)

G1 = "120363000000000001@g.us"
G2 = "120363000000000002@g.us"
PHONE = "5511999990000@c.us"
LID = "99887766554433@lid"


class TestReadingTheReply:
    def test_a_bare_list_of_strings(self):
        assert group_jids_from_reply([G1, G2]) == [G1, G2]

    def test_wid_objects_carrying_serialized(self):
        payload = [{"_serialized": G1, "user": "x", "server": "g.us"}]
        assert group_jids_from_reply(payload) == [G1]

    def test_wid_objects_with_only_user_and_server(self):
        payload = [{"user": "120363000000000001", "server": "g.us"}]
        assert group_jids_from_reply(payload) == [G1]

    def test_an_envelope_around_the_list_is_tolerated(self):
        assert group_jids_from_reply({"status": "success", "response": [G1]}) == [G1]

    def test_an_empty_list_is_an_answer_not_a_failure(self):
        assert group_jids_from_reply([]) == []

    def test_only_groups_are_kept_and_repeats_dropped(self):
        payload = [G1, PHONE, LID, G1, G2, "", None, 5, {"user": 1, "server": "g.us"}]
        assert group_jids_from_reply(payload) == [G1, G2]

    @pytest.mark.parametrize(
        "payload",
        [None, "x", 5, {}, {"status": "error", "message": "boom"}, {"response": "x"}],
    )
    def test_anything_that_is_not_a_list_is_unusable(self, payload):
        assert group_jids_from_reply(payload) is None


class TestWhichFormsToAsk:
    def test_a_phone_chat_then_its_lid(self):
        assert wid_candidates(PHONE, {}, {PHONE: LID}) == [PHONE, LID]

    def test_a_lid_chat_then_its_phone(self):
        assert wid_candidates(LID, {LID: PHONE}, {}) == [LID, PHONE]

    def test_no_known_counterpart_asks_once(self):
        assert wid_candidates(PHONE, {}, {}) == [PHONE]

    def test_a_counterpart_equal_to_the_chat_is_not_repeated(self):
        assert wid_candidates(PHONE, {PHONE: PHONE}, {PHONE: PHONE}) == [PHONE]

    @pytest.mark.parametrize("jid", ["", None, G1, 5])
    def test_nothing_to_ask_about(self, jid):
        assert wid_candidates(jid, {}, {}) == []

    def test_missing_maps_are_fine(self):
        assert wid_candidates(PHONE, None, None) == [PHONE]


class _Reply:
    def __init__(self, body, status=200, boom=None):
        self._body, self.status_code, self._boom = body, status, boom

    def json(self):
        if self._boom:
            raise self._boom
        return self._body


class _Owner:
    token = "tok"
    wpp_server = "http://127.0.0.1"
    wpp_port = 21465
    _lid_to_phone = {}
    _phone_to_lid = {}


@pytest.fixture
def scripted(monkeypatch):
    """Replace api_get with answers handed out in order; record the URLs."""
    calls = []

    def install(*answers):
        queue = list(answers)

        def fake(url, **kwargs):
            calls.append(url)
            answer = queue.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer

        monkeypatch.setattr(common_groups, "api_get", fake)
        return calls

    return install


class TestFetching:
    def test_the_groups_come_back_and_the_route_is_the_documented_one(self, scripted):
        calls = scripted(_Reply([G1, G2]))
        assert fetch_common_groups(_Owner(), PHONE) == [G1, G2]
        assert calls == [f"http://127.0.0.1:21465/api/tok/common-groups/{PHONE}"]

    def test_an_empty_answer_tries_the_other_jid_form(self, scripted):
        owner = _Owner()
        owner._phone_to_lid = {PHONE: LID}
        calls = scripted(_Reply([]), _Reply([G2]))
        assert fetch_common_groups(owner, PHONE) == [G2]
        assert [c.rsplit("/", 1)[1] for c in calls] == [PHONE, LID]

    def test_empty_everywhere_is_none_in_common_not_a_failure(self, scripted):
        owner = _Owner()
        owner._phone_to_lid = {PHONE: LID}
        scripted(_Reply([]), _Reply([]))
        assert fetch_common_groups(owner, PHONE) == []

    def test_a_server_error_is_could_not_ask(self, scripted):
        scripted(_Reply({"status": "error"}, status=500))
        assert fetch_common_groups(_Owner(), PHONE) is None

    def test_a_network_error_is_could_not_ask(self, scripted):
        scripted(ConnectionError("down"))
        assert fetch_common_groups(_Owner(), PHONE) is None

    def test_a_reply_that_is_not_json_is_could_not_ask(self, scripted):
        scripted(_Reply(None, boom=ValueError("not json")))
        assert fetch_common_groups(_Owner(), PHONE) is None

    def test_one_failed_form_does_not_hide_an_answer_from_the_other(self, scripted):
        owner = _Owner()
        owner._phone_to_lid = {PHONE: LID}
        scripted(ConnectionError("down"), _Reply([G1]))
        assert fetch_common_groups(owner, PHONE) == [G1]

    def test_a_failure_then_an_empty_answer_is_still_an_answer(self, scripted):
        owner = _Owner()
        owner._phone_to_lid = {PHONE: LID}
        scripted(ConnectionError("down"), _Reply([]))
        assert fetch_common_groups(owner, PHONE) == []

    def test_a_group_chat_is_never_asked_about(self, scripted):
        calls = scripted()
        assert fetch_common_groups(_Owner(), G1) is None
        assert calls == []

    def test_the_log_carries_no_url_name_or_token(self, scripted, caplog):
        scripted(ConnectionError("http://127.0.0.1:21465/api/tok/common-groups/x"))
        with caplog.at_level(logging.DEBUG):
            fetch_common_groups(_Owner(), PHONE)
        text = caplog.text
        assert "tok" not in text and "common-groups" not in text and PHONE not in text


class TestRows:
    def test_sorted_by_name_ignoring_case(self):
        names = {G1: "zeta", G2: "Alpha"}
        rows = common_group_rows([G1, G2], names.get, "Unknown group")
        assert rows == [("Alpha", G2), ("zeta", G1)]

    def test_a_group_without_a_name_gets_the_fallback_never_its_jid(self):
        rows = common_group_rows([G1], lambda jid: "", "Unknown group")
        assert rows == [("Unknown group", G1)]

    def test_a_lookup_that_raises_gets_the_fallback(self):
        def boom(jid):
            raise RuntimeError("db closed")

        assert common_group_rows([G1], boom, "Unknown group") == [("Unknown group", G1)]

    def test_blank_names_count_as_missing_and_names_are_trimmed(self):
        names = {G1: "   ", G2: "  Team  "}
        rows = common_group_rows([G1, G2], names.get, "Unknown group")
        assert rows == [("Team", G2), ("Unknown group", G1)]

    def test_equal_names_keep_a_stable_order_by_jid(self):
        rows = common_group_rows([G2, G1], lambda jid: "Same", "x")
        assert rows == [("Same", G1), ("Same", G2)]
