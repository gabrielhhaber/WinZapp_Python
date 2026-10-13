"""Pending requests to join a group: the contracts and what the dialog
decides, without creating a wx window.

No group of the account read over CDP had a pending request, so the shape of
one comes from the library's typing and WhatsApp Web's program text. What was
run for real: the list for a group the account administers (asked of the
server, empty) and for one it does not (refused with 403).
"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core import group_membership_requests as api
from core.group_membership_requests import MembershipRequest, NotAdmin, Pending
from tests.locales import registered_locale_codes
from tests.mnemonics import load_strings, mnemonic
from ui.dialogs.group_membership_requests import (
    GroupMembershipRequestsDialog, add_membership_requests_button, request_label, requester_name)

CONNECTION = ("http://localhost", 6300, "synthetic-token")
HEADERS = {"Authorization": "Bearer synthetic-token"}
BASE = "http://localhost:6300/api/synthetic-token/group-membership-requests/111-0@g.us"
GROUP = "111-0@g.us"
BEA = MembershipRequest("222@lid", "222@lid", "5511900000002@c.us", "Bea", "InviteLink", 1790000000)
CAIO = MembershipRequest("333@lid", "444@lid", "", "", "NonAdminAdd", 1790000100)
RAW = [{"id": "222@lid", "addedBy": "222@lid", "phone": "5511900000002@c.us", "name": " Bea ",
        "method": "InviteLink", "t": 1790000000},
       {"id": "333@lid", "addedBy": "444@lid", "phone": "", "name": "", "method": "NonAdminAdd",
        "t": 1790000100}]


def answer(body, status=200):
    return SimpleNamespace(status_code=status, json=lambda: body)


def listing(requests=RAW, refreshed=True):
    return {"status": "success", "response": {"refreshed": refreshed, "requests": requests}}


# ── contracts ──────────────────────────────────────────────────────────────

def test_a_listing_becomes_requests():
    assert api.parse_requests(listing()) == Pending((BEA, CAIO), True)
    assert api.parse_requests(listing([], refreshed=False)) == Pending((), False)
    assert api.parse_requests(listing([], refreshed="yes")).refreshed is False


@pytest.mark.parametrize("body", [None, [], {"status": "error"}, {"status": "success"},
                                  {"status": "success", "response": {"requests": "x"}}])
def test_an_answer_that_is_not_a_listing_is_refused(body):
    with pytest.raises(ValueError):
        api.parse_requests(body)


def test_what_is_not_a_person_a_method_or_a_time_is_dropped_never_guessed():
    odd = [{"id": "444-0@g.us"}, None, {"id": "222:1@lid"},
           {"id": "222@lid", "addedBy": "junk", "phone": "5511", "name": "222@lid",
            "method": "SomethingNew", "t": True}]
    assert api.parse_requests(listing(odd)).requests == (
        MembershipRequest("222@lid", "", "", "", "", None),)


def test_listing_is_one_get_and_403_is_not_an_empty_list(monkeypatch):
    get = Mock(return_value=answer(listing()))
    monkeypatch.setattr(api, "api_get", get)
    assert api.list_requests(CONNECTION, GROUP).requests == (BEA, CAIO)
    get.assert_called_once_with(BASE, headers=HEADERS, timeout=30)
    monkeypatch.setattr(api, "api_get", Mock(return_value=answer({"status": "error", "code": "not_admin"}, 403)))
    with pytest.raises(NotAdmin):
        api.list_requests(CONNECTION, GROUP)
    monkeypatch.setattr(api, "api_get", Mock(return_value=answer({"status": "error"}, 503)))
    with pytest.raises(ValueError):
        api.list_requests(CONNECTION, GROUP)


@pytest.mark.parametrize("approve,suffix", [(True, "/approve"), (False, "/reject")])
def test_an_answer_is_one_post_to_its_own_route(monkeypatch, approve, suffix):
    post = Mock(return_value=answer({"status": "success", "response": {"accepted": True}}))
    monkeypatch.setattr(api, "api_post", post)
    assert api.answer_requests(CONNECTION, GROUP, ["222@lid"], approve) is None
    post.assert_called_once_with(BASE + suffix, json={"participants": ["222@lid"]}, headers=HEADERS, timeout=60)


@pytest.mark.parametrize("group,jids", [(GROUP, []), (GROUP, ["444-0@g.us"]), (GROUP, ["222:1@lid"]),
                                        ("222@lid", ["222@lid"]), (GROUP, ["1@lid"] * 51)])
def test_a_malformed_answer_is_never_sent(monkeypatch, group, jids):
    post = Mock()
    monkeypatch.setattr(api, "api_post", post)
    with pytest.raises(ValueError):
        api.answer_requests(CONNECTION, group, jids, True)
    post.assert_not_called()


@pytest.mark.parametrize("response", [answer({"status": "error", "code": "membership_action_failed"}, 503),
                                      answer({"status": "error"}), answer([])])
def test_a_refusal_is_an_error(monkeypatch, response):
    monkeypatch.setattr(api, "api_post", Mock(return_value=response))
    with pytest.raises(ValueError):
        api.answer_requests(CONNECTION, GROUP, ["222@lid"], False)


def settle(monkeypatch, post, readings):
    monkeypatch.setattr(api, "api_post", post)
    read = Mock(side_effect=readings)
    monkeypatch.setattr(api, "list_requests", read)
    return api.settle(CONNECTION, GROUP, ["222@lid"], True, sleep=Mock()), read


ACCEPTED = Mock(return_value=answer({"status": "success", "response": {"accepted": True}}))


def test_an_answer_is_believed_when_the_person_left_a_refreshed_list(monkeypatch):
    outcome, read = settle(monkeypatch, ACCEPTED, [Pending((BEA, CAIO), True), Pending((CAIO,), True)])
    assert outcome == ("done", Pending((CAIO,), True)) and read.call_count == 2


def test_a_person_still_listed_is_not_answered(monkeypatch):
    outcome, read = settle(monkeypatch, ACCEPTED, [Pending((BEA,), True)] * 3)
    assert outcome[0] == "unconfirmed" and read.call_count == 3


def test_a_list_the_server_did_not_confirm_proves_nothing(monkeypatch):
    """The local table is only cleaned by asking the server: an old copy
    without the person must not pass for "approved"."""
    outcome, _read = settle(monkeypatch, ACCEPTED, [Pending((), False)] * 3)
    assert outcome == ("unconfirmed", Pending((), False))


def test_a_refused_answer_is_failed(monkeypatch):
    refused = Mock(return_value=answer({"status": "error"}, 503))
    outcome, _read = settle(monkeypatch, refused, [Pending((BEA,), True)] * 3)
    assert outcome == ("failed", Pending((BEA,), True))
    refused.assert_called_once()  # never retried


# ── labels ─────────────────────────────────────────────────────────────────

STRINGS = load_strings("en-US")


def main_window(names=None):
    names = names or {}
    i18n = SimpleNamespace(t=lambda key: STRINGS[key].replace("&", ""))
    return SimpleNamespace(
        i18n=i18n, chats={GROUP: {"name": "School"}}, speak_output=SimpleNamespace(output=Mock()),
        _normalize_jid=lambda jid: jid.replace("@c.us", "@s.whatsapp.net"),
        _is_self_jid=lambda jid: False, self_reference_label=lambda: "Me",
        _resolve_jid_name=lambda jid, resolve_missing=True: names.get(jid, ""))


def test_a_requester_is_named_in_the_most_telling_form_known():
    saved = main_window({"222@lid": "Beatriz (work)"})
    assert requester_name(saved, BEA) == "Beatriz (work)"
    number_only = main_window({"5511900000002@s.whatsapp.net": "+55 11 90000-0002"})
    assert requester_name(number_only, BEA) == "Bea (+55 11 90000-0002)"
    nameless = MembershipRequest("222@lid", "", "5511900000002@c.us", "", "", None)
    assert requester_name(number_only, nameless) == "+55 11 90000-0002"
    assert requester_name(main_window(), BEA) == "Bea"
    assert requester_name(main_window(), CAIO) == "Unnamed participant"
    # The resolver handing the identifier back is not a name.
    assert requester_name(main_window({"333@lid": "333@lid"}), CAIO) == "Unnamed participant"


def test_a_row_says_who_how_and_when():
    mw = main_window({"444@lid": "Dora"})
    when = datetime.fromtimestamp(1790000000).strftime("%Y-%m-%d %H:%M")
    assert request_label(mw, BEA, "%Y-%m-%d %H:%M") == f"Bea, asked to join through the invitation link, {when}"
    assert request_label(mw, CAIO, "%Y").startswith("Unnamed participant, added by Dora, awaiting approval, ")
    community = MembershipRequest("222@lid", "222@lid", "", "Bea", "LinkedGroupJoin", None)
    assert request_label(mw, community, "%Y") == "Bea, asked to join through the community"
    unknown = MembershipRequest("222@lid", "", "", "Bea", "", None)
    assert request_label(mw, unknown, "%Y") == "Bea, asked to join"


# ── the dialog, on a stub ──────────────────────────────────────────────────

class ListBox:
    def __init__(self):
        self.items, self.selection = [], -1
        self.Freeze, self.Thaw = Mock(), Mock()

    def Set(self, items):
        self.items, self.selection = list(items), -1

    def SetSelection(self, index):
        self.selection = index

    def GetSelection(self):
        return self.selection


class Dialog:
    for _name in ("_selected", "_set_busy", "_update_buttons", "_reload", "_finish_reload", "_summary",
                  "_show", "_answer", "_finish_answer", "_announce", "_deliver"):
        locals()[_name] = getattr(GroupMembershipRequestsDialog, _name)
    del _name

    def __init__(self):
        self._mw = main_window()
        self._i18n = self._mw.i18n
        self._connection, self._group = CONNECTION, GROUP
        self._busy, self._requests = False, ()
        self._closed = SimpleNamespace(is_set=lambda: False)
        self._list = ListBox()
        self._status = SimpleNamespace(SetLabel=Mock())
        for name in ("_approve", "_reject", "_refresh"):
            setattr(self, name, SimpleNamespace(Enable=Mock()))
        self.work = []

    def _current(self):
        return True

    def _run_async(self, work, done):
        self.work.append((work, done))

    def enabled(self, name):
        return getattr(self, name).Enable.call_args.args[0]

    def said(self):
        return self._mw.speak_output.output.call_args.args[0]


def test_reading_lists_the_oldest_request_first_and_says_how_many(monkeypatch):
    monkeypatch.setattr("ui.dialogs.group_membership_requests.list_requests",
                        Mock(return_value=Pending((CAIO, BEA), True)))
    dialog = Dialog()
    dialog._reload()
    assert dialog._busy and not dialog.enabled("_refresh")
    dialog._reload()  # a second request while one runs is ignored
    [(work, done)] = dialog.work
    done(work())
    assert [row.split(",")[0] for row in dialog._list.items] == ["Bea", "Unnamed participant"]
    assert dialog._list.selection == 0 and dialog.said() == "Pending requests: 2"
    assert dialog.enabled("_approve") and dialog.enabled("_reject") and dialog.enabled("_refresh")
    assert dialog._list.Freeze.call_count == dialog._list.Thaw.call_count == 1


def test_empty_stale_forbidden_and_failed_readings_are_told_apart(monkeypatch):
    dialog = Dialog()
    dialog._finish_reload(Pending((), True))
    assert dialog.said() == "There are no pending requests to join this group."
    assert not dialog.enabled("_approve") and not dialog.enabled("_reject")
    dialog._finish_reload(Pending((BEA,), False))
    assert dialog.said() == "Pending requests: 1 WhatsApp could not be asked, so this list may be out of date."
    dialog._finish_reload(None)
    assert dialog.said().startswith("The requests could not be read") and dialog._requests == (BEA,)
    monkeypatch.setattr("ui.dialogs.group_membership_requests.list_requests", Mock(side_effect=NotAdmin()))
    dialog._reload()
    work, done = dialog.work.pop()
    done(work())
    assert dialog.said() == "Only admins of the group can see the requests to join."
    assert dialog._requests == () and not dialog.enabled("_approve")


@pytest.mark.parametrize("approve,said", [(True, "Request approved."), (False, "Request rejected.")])
def test_an_answer_goes_for_the_selected_person_and_shows_the_new_list(monkeypatch, approve, said):
    asked = Mock(return_value=("done", Pending((CAIO,), True)))
    monkeypatch.setattr("ui.dialogs.group_membership_requests.settle", asked)
    dialog = Dialog()
    dialog._show((BEA, CAIO))
    dialog._answer(approve)
    assert dialog._busy and not dialog.enabled("_approve")
    dialog._answer(approve)  # ignored while the first is running
    [(work, done)] = dialog.work
    done(work())
    asked.assert_called_once_with(CONNECTION, GROUP, ["222@lid"], approve)
    assert dialog._requests == (CAIO,) and dialog.said() == said and not dialog._busy


@pytest.mark.parametrize("outcome,start", [
    (("unconfirmed", Pending((BEA,), True)), "The answer was sent, but WhatsApp still lists"),
    (("failed", None), "The request could not be answered"),
    (None, "The request could not be answered")])
def test_an_answer_that_is_not_confirmed_is_never_announced_as_given(outcome, start):
    dialog = Dialog()
    dialog._show((BEA, CAIO))
    dialog._finish_answer(outcome, True)
    assert dialog.said().startswith(start)
    assert BEA in dialog._requests


def test_nothing_is_answered_with_nobody_selected():
    dialog = Dialog()
    dialog._answer(True)
    assert not dialog.work and not dialog._busy


def test_the_data_dialog_button_starts_hidden(monkeypatch):
    button = Mock()
    monkeypatch.setattr("ui.dialogs.group_membership_requests.wx.Button", Mock(return_value=button))
    owner = SimpleNamespace(_i18n=SimpleNamespace(t=lambda key: key))
    sizer = Mock()
    assert add_membership_requests_button(owner, Mock(), sizer) is button
    button.Hide.assert_called_once_with()
    sizer.Add.assert_called_once()


# ── every locale ───────────────────────────────────────────────────────────

DIALOG_KEYS = ("group_requests_list", "group_requests_approve", "group_requests_reject",
               "group_requests_refresh", "close")


@pytest.mark.parametrize("locale", registered_locale_codes())
def test_every_control_has_a_letter_of_its_own(locale):
    strings = load_strings(locale)
    letters = {}
    for key in DIALOG_KEYS:
        assert mnemonic(strings[key]) is not None, key
        letters.setdefault(mnemonic(strings[key]), []).append(key)
    assert not {letter: found for letter, found in letters.items() if len(found) > 1}
