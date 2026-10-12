"""Tests for MainWindow's group-admin REST methods:
remove/promote/demote_group_members(), set_group_subject(), and
set_group_description().

These back the group-data dialog's new admin-only participant context menu
(remove/promote/demote a member) and "edit name"/"edit description" buttons.
All five are thin request wrappers following the same (True, "") /
(False, error_message) contract as the existing add_group_members().

MainWindow is a wx.Frame and cannot be instantiated without a running
wx.App, so the methods under test are exercised as plain functions against
a small stub — same approach as tests/test_restart_wpp_session.py.
"""

import pytest

from main import MainWindow


class _I18n:
    """Just enough to render add_group_members()'s per-participant messages
    — real key lookup would need a full main_window/settings chain this
    module has no other use for."""
    _TEMPLATES = {
        "group_members_invitable": "Could not add {names} directly. Invitation delivery was not confirmed.",
        "group_members_pending": "Awaiting admin approval: {names}.",
        "group_members_unconfirmed": "Result unconfirmed for {names}.",
        "add_member_could_not_add": "Could not add {names}.",
    }

    def t(self, key):
        return self._TEMPLATES.get(key, key)


class _Stub:
    _group_participant_action = MainWindow._group_participant_action
    add_group_members     = MainWindow.add_group_members
    remove_group_members  = MainWindow.remove_group_members
    promote_group_members = MainWindow.promote_group_members
    demote_group_members  = MainWindow.demote_group_members
    set_group_subject      = MainWindow.set_group_subject
    set_group_description  = MainWindow.set_group_description

    def __init__(self):
        self.wpp_server = "http://127.0.0.1"
        self.wpp_port = 6300
        self.token = "test-token"
        self.i18n = _I18n()


def _fake_post_ok(url, json=None, headers=None, timeout=None, **kw):
    class _Resp:
        status_code = 200
        text = ""
        def json(self):
            return {"status": "success", "response": {"result": [
                {jid: {"code": 200} for jid in json.get("phone", [])}
            ]}}
    _fake_post_ok.calls.append((url, json))
    return _Resp()
_fake_post_ok.calls = []


def _fake_post_fail(url, json=None, headers=None, timeout=None, **kw):
    class _Resp:
        status_code = 403
        text = "not an admin"
    return _Resp()


class TestParticipantActions:
    @pytest.mark.parametrize("method,endpoint", [
        ("remove_group_members", "remove-participant-group"),
        ("promote_group_members", "promote-participant-group"),
        ("demote_group_members", "demote-participant-group"),
    ])
    def test_success_hits_the_right_endpoint_with_groupid_and_phone(self, monkeypatch, method, endpoint):
        _fake_post_ok.calls = []
        monkeypatch.setattr("main.requests.post", _fake_post_ok)
        stub = _Stub()

        ok, err = getattr(stub, method)("123-456@g.us", ["5511999999999"])

        assert ok is True
        assert err == ""
        url, payload = _fake_post_ok.calls[0]
        assert url == f"http://127.0.0.1:6300/api/test-token/{endpoint}"
        assert payload["groupId"] == "123-456@g.us"
        # Bare digits get the @c.us suffix; an already-qualified jid is left alone.
        assert payload["phone"] == ["5511999999999@c.us"]

    def test_jid_already_qualified_is_not_double_suffixed(self, monkeypatch):
        _fake_post_ok.calls = []
        monkeypatch.setattr("main.requests.post", _fake_post_ok)
        stub = _Stub()

        stub.remove_group_members("123-456@g.us", ["5511999999999@lid"])

        _, payload = _fake_post_ok.calls[0]
        assert payload["phone"] == ["5511999999999@lid"]

    def test_failure_returns_false_and_error_text(self, monkeypatch):
        monkeypatch.setattr("main.requests.post", _fake_post_fail)
        stub = _Stub()

        ok, err = stub.remove_group_members("123-456@g.us", ["5511999999999"])

        assert ok is False
        assert "403" in err
        assert "not an admin" in err

    def test_request_exception_is_caught_and_reported(self, monkeypatch):
        def _raise(*a, **kw):
            raise ConnectionError("boom")
        monkeypatch.setattr("main.requests.post", _raise)
        stub = _Stub()

        ok, err = stub.promote_group_members("123-456@g.us", ["5511999999999"])

        assert ok is False
        assert "boom" in err


class TestAddGroupMembers:
    """Regression: every add-member attempt failed with a server-side 500.
    groupController.ts's addParticipant() (upstream, unpatched) reads
    req.body.phone — add_group_members() used to send "participantId"
    instead, a field the controller never reads at all, so phone came
    through as undefined and contactToArray(undefined) blew up server-side.
    remove/promote/demote_group_members() already used "phone" correctly
    (see TestParticipantActions above), which is why only adding was ever
    reported broken."""

    def test_posts_phone_not_participantid(self, monkeypatch):
        _fake_post_ok.calls = []
        monkeypatch.setattr("main.requests.post", _fake_post_ok)
        stub = _Stub()

        ok, err = stub.add_group_members("123-456@g.us", ["5511999999999"])

        assert ok is True
        assert err == ""
        url, payload = _fake_post_ok.calls[0]
        assert url == "http://127.0.0.1:6300/api/test-token/add-participant-group"
        assert payload["groupId"] == "123-456@g.us"
        assert payload["phone"] == ["5511999999999@c.us"]
        assert "participantId" not in payload

    def test_jid_already_qualified_is_not_double_suffixed(self, monkeypatch):
        _fake_post_ok.calls = []
        monkeypatch.setattr("main.requests.post", _fake_post_ok)
        stub = _Stub()

        stub.add_group_members("123-456@g.us", ["5511999999999@lid"])

        _, payload = _fake_post_ok.calls[0]
        assert payload["phone"] == ["5511999999999@lid"]

    def test_failure_returns_false_and_error_text(self, monkeypatch):
        monkeypatch.setattr("main.requests.post", _fake_post_fail)
        stub = _Stub()

        ok, err = stub.add_group_members("123-456@g.us", ["5511999999999"])

        assert ok is False
        assert "403" in err


def _fake_post_json(result_groups):
    """A 201 'success' response carrying wa-js's per-participant result —
    the shape add-participant-group actually returns (see
    add_group_members()'s own docstring)."""
    class _Resp:
        status_code = 201
        text = ""

        def json(self):
            return {"status": "success", "response": {"result": result_groups}}

    def _post(url, json=None, headers=None, timeout=None, **kw):
        return _Resp()
    return _post


class TestAddGroupMembersPerParticipantResult:
    """WPPConnect's addParticipant controller answers HTTP 201 'success'
    unconditionally, even when WhatsApp itself never added the participant —
    reported live: a target whose privacy settings required an invite came
    back as an ordinary success, and was never actually in the group. The
    real per-participant outcome is wa-js's `code` field buried in
    response.result, which add_group_members() must now inspect instead of
    trusting the HTTP status alone."""

    def test_code_200_is_a_real_success(self, monkeypatch):
        post = _fake_post_json([{"5511999999999@c.us": {
            "code": 200, "message": "", "wid": "5511999999999@c.us",
            "invite_code": None, "invite_code_exp": None,
        }}])
        monkeypatch.setattr("main.requests.post", post)
        stub = _Stub()

        ok, err = stub.add_group_members("123-456@g.us", ["5511999999999"])

        assert ok is True
        assert err == ""

    def test_code_409_already_a_participant_counts_as_success(self, monkeypatch):
        post = _fake_post_json([{"5511999999999@c.us": {
            "code": 409, "message": "already in group", "wid": "5511999999999@c.us",
            "invite_code": None, "invite_code_exp": None,
        }}])
        monkeypatch.setattr("main.requests.post", post)
        stub = _Stub()

        ok, err = stub.add_group_members("123-456@g.us", ["5511999999999"])

        assert ok is True

    def test_code_403_with_invite_code_is_not_reported_as_a_plain_success(self, monkeypatch):
        """The exact bug: a privacy-restricted target used to report success
        even though WhatsApp sent an invite instead of adding them."""
        post = _fake_post_json([{"5511999999999@c.us": {
            "code": 403, "message": "", "wid": "5511999999999@c.us",
            "invite_code": "abc123", "invite_code_exp": 1700000000,
        }}])
        monkeypatch.setattr("main.requests.post", post)
        stub = _Stub()

        ok, err = stub.add_group_members("123-456@g.us", ["5511999999999"])

        assert ok is False
        assert "5511999999999" in err

    def test_failure_reports_the_contact_name_instead_of_the_raw_number(self, monkeypatch):
        """A number the user has as a saved contact must be announced by
        name in the failure message — NVDA users otherwise hear a bare
        phone number read out digit by digit with no idea whose it is."""
        post = _fake_post_json([{"5511999999999@c.us": {
            "code": 408, "message": "blocked", "wid": "5511999999999@c.us",
            "invite_code": None, "invite_code_exp": None,
        }}])
        monkeypatch.setattr("main.requests.post", post)
        stub = _Stub()
        stub.contacts = {"5511999999999@c.us": {"name": "Maria Silva"}}

        ok, err = stub.add_group_members("123-456@g.us", ["5511999999999"])

        assert ok is False
        assert "Maria Silva" in err
        assert "5511999999999" not in err

    def test_failure_falls_back_to_the_raw_number_when_no_contact_is_known(self, monkeypatch):
        post = _fake_post_json([{"5511999999999@c.us": {
            "code": 408, "message": "blocked", "wid": "5511999999999@c.us",
            "invite_code": None, "invite_code_exp": None,
        }}])
        monkeypatch.setattr("main.requests.post", post)
        stub = _Stub()  # no .contacts at all — must not raise

        ok, err = stub.add_group_members("123-456@g.us", ["5511999999999"])

        assert ok is False
        assert "5511999999999" in err

    def test_failure_code_with_no_invite_code_is_still_reported(self, monkeypatch):
        post = _fake_post_json([{"5511999999999@c.us": {
            "code": 408, "message": "blocked", "wid": "5511999999999@c.us",
            "invite_code": None, "invite_code_exp": None,
        }}])
        monkeypatch.setattr("main.requests.post", post)
        stub = _Stub()

        ok, err = stub.add_group_members("123-456@g.us", ["5511999999999"])

        assert ok is False
        assert "5511999999999" in err

    def test_mixed_results_report_only_the_one_that_actually_failed(self, monkeypatch):
        post = _fake_post_json([{
            "5511999999999@c.us": {"code": 200, "message": "", "wid": "5511999999999@c.us",
                                    "invite_code": None, "invite_code_exp": None},
            "5511888888888@c.us": {"code": 403, "message": "", "wid": "5511888888888@c.us",
                                    "invite_code": "xyz", "invite_code_exp": 1},
        }])
        monkeypatch.setattr("main.requests.post", post)
        stub = _Stub()

        ok, err = stub.add_group_members(
            "123-456@g.us", ["5511999999999", "5511888888888"])

        assert ok is False
        assert "5511888888888" in err
        assert "5511999999999" not in err

    def test_empty_result_is_unconfirmed_instead_of_plain_http_success(self, monkeypatch):
        post = _fake_post_json([])
        monkeypatch.setattr("main.requests.post", post)
        stub = _Stub()

        ok, err = stub.add_group_members("123-456@g.us", ["5511999999999"])

        assert ok is False
        assert "unconfirmed" in err


class TestGroupSubjectAndDescription:
    def test_set_group_subject_posts_title(self, monkeypatch):
        _fake_post_ok.calls = []
        monkeypatch.setattr("main.requests.post", _fake_post_ok)
        stub = _Stub()

        ok, err = stub.set_group_subject("123-456@g.us", "New Name")

        assert ok is True
        url, payload = _fake_post_ok.calls[0]
        assert url == "http://127.0.0.1:6300/api/test-token/group-subject"
        assert payload == {"groupId": "123-456@g.us", "title": "New Name"}

    def test_set_group_description_posts_description(self, monkeypatch):
        _fake_post_ok.calls = []
        monkeypatch.setattr("main.requests.post", _fake_post_ok)
        stub = _Stub()

        ok, err = stub.set_group_description("123-456@g.us", "New description")

        assert ok is True
        url, payload = _fake_post_ok.calls[0]
        assert url == "http://127.0.0.1:6300/api/test-token/group-description"
        assert payload == {"groupId": "123-456@g.us", "description": "New description"}

    def test_set_group_description_failure_is_reported(self, monkeypatch):
        monkeypatch.setattr("main.requests.post", _fake_post_fail)
        stub = _Stub()

        ok, err = stub.set_group_description("123-456@g.us", "x")

        assert ok is False
        assert "403" in err
