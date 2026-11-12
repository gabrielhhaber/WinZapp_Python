"""Communities: the reading and action contracts, and what the dialog decides,
without creating a wx window.

The admin actions were run on a real account against a throwaway community
(2026-10-13, WhatsApp Web 2.3000.1049387914, wa-js 4.6.1). What they answered
there is what the fixtures below carry; what went wrong there is why an
action is believed only when a fresh reading shows its effect:

- creating with no group answered an error after the community existed;
- linking a group created a second earlier answered success and did nothing;
- the library's promotion threw for every member who was not an admin yet.
"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core import communities as api
from core.communities import Admin, Community, Subgroup
from tests.locales import registered_locale_codes
from tests.mnemonics import load_strings, mnemonic
from ui.dialogs.communities import CommunitiesPanel, community_label, subgroup_label
from core.participant_names import person_name
from ui.dialogs.community_admin import CommunityAdminActions

CONNECTION = ("http://localhost", 6300, "synthetic-token")
HEADERS = {"Authorization": "Bearer synthetic-token"}
BASE = "http://localhost:6300/api/synthetic-token/"
ANNOUNCEMENTS = Subgroup("111-1@g.us", "Notices", True, False)
PLAIN = Subgroup("111-3@g.us", "Study", False, False)
CREATOR = Admin("5511900000001@c.us", True)
OTHER_ADMIN = Admin("222@lid", False)
SCHOOL = Community("111-0@g.us", "School", "Class of 2026", True, (CREATOR, OTHER_ADMIN),
                   (ANNOUNCEMENTS, PLAIN))
CLUB = Community("333-0@g.us", "Club", "", False, (), ())
RAW = {"id": "111-0@g.us", "subject": "School", "description": " Class of 2026 ", "closed": True,
       "iAmAdmin": True, "iAmSuperAdmin": False,
       "admins": [{"id": CREATOR.jid, "superAdmin": True}, {"id": OTHER_ADMIN.jid, "superAdmin": False}],
       "subgroups": [{"id": "111-1@g.us", "subject": "Notices", "announcement": True, "general": False},
                     {"id": "111-3@g.us", "subject": "Study", "announcement": False, "general": False}]}


def answer(body, status=200):
    def json():
        if body is None:
            raise ValueError("no body")
        return body
    return SimpleNamespace(status_code=status, json=json)


# ── reading ────────────────────────────────────────────────────────────────

def test_a_reading_becomes_communities_with_their_admins_and_groups():
    assert api.parse_communities({"status": "success", "response": [RAW]}) == [SCHOOL]
    assert SCHOOL.announcement_group == ANNOUNCEMENTS and CLUB.announcement_group is None


@pytest.mark.parametrize("body", [None, [], {"status": "error", "response": []},
                                  {"status": "success"}, {"status": "success", "response": {}}])
def test_an_answer_that_is_not_a_reading_is_refused(body):
    with pytest.raises(ValueError):
        api.parse_communities(body)


def test_what_is_not_a_community_a_group_or_a_person_is_dropped_never_guessed():
    odd = dict(RAW, admins=[{"id": "not-a-person"}, {"id": 5}, "x", {"id": "9@g.us"}],
               subgroups=[{"id": "999@lid"}, None, {"id": "111-5@g.us", "subject": 7, "announcement": 1}],
               iAmAdmin="yes")
    [community] = api.parse_communities({"status": "success", "response": [odd, {"id": "x@lid"}, None]})
    assert community.admins == () and community.i_am_admin is False
    assert community.subgroups == (Subgroup("111-5@g.us", "", False, False),)


def test_a_raw_identifier_never_becomes_a_name():
    item = dict(RAW, subject="111-0@g.us", subgroups=[{"id": "111-1@g.us", "subject": "5511900000001@c.us"}])
    [community] = api.parse_communities({"status": "success", "response": [item]})
    assert community.name == "" and community.subgroups[0].name == ""


def test_listing_is_one_get(monkeypatch):
    get = Mock(return_value=answer({"status": "success", "response": [RAW]}))
    monkeypatch.setattr(api, "api_get", get)
    assert api.list_communities(CONNECTION) == [SCHOOL]
    get.assert_called_once_with(BASE + "communities", headers=HEADERS, timeout=20)


def test_an_unavailable_listing_is_an_error_not_an_empty_list(monkeypatch):
    monkeypatch.setattr(api, "api_get", Mock(return_value=answer(
        {"status": "error", "code": "communities_unavailable"}, 503)))
    with pytest.raises(ValueError):
        api.list_communities(CONNECTION)


# ── actions ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("call,endpoint,payload", [
    (lambda: api.create_community(CONNECTION, " School ", " About ", ["111-3@g.us"]), "create-community",
     {"name": "School", "description": "About", "groupIds": ["111-3@g.us"]}),
    (lambda: api.deactivate_community(CONNECTION, SCHOOL.jid), "deactivate-community", {"id": SCHOOL.jid}),
    (lambda: api.link_groups(CONNECTION, SCHOOL.jid, ["444-0@g.us"]), "add-community-subgroup",
     {"id": SCHOOL.jid, "groupsIds": ["444-0@g.us"]}),
    (lambda: api.unlink_groups(CONNECTION, SCHOOL.jid, [PLAIN.jid]), "remove-community-subgroup",
     {"id": SCHOOL.jid, "groupsIds": [PLAIN.jid]}),
    # WinZapp's own route: the library's cannot promote a member.
    (lambda: api.promote_admin(CONNECTION, SCHOOL.jid, "333@lid"), "community-admin/111-0@g.us/promote",
     {"participant": "333@lid"}),
    (lambda: api.promote_admin(CONNECTION, SCHOOL.jid, "5511900000003@s.whatsapp.net"),
     "community-admin/111-0@g.us/promote", {"participant": "5511900000003@c.us"}),
    (lambda: api.demote_admin(CONNECTION, SCHOOL.jid, OTHER_ADMIN.jid), "demote-community-participant",
     {"id": SCHOOL.jid, "participantsId": [OTHER_ADMIN.jid]}),
])
def test_each_action_is_the_upstream_route_with_its_own_field_names(monkeypatch, call, endpoint, payload):
    post = Mock(return_value=answer({"anything": "the library returned"}))
    monkeypatch.setattr(api, "api_post", post)
    call()
    post.assert_called_once_with(BASE + endpoint, json=payload, headers=HEADERS, timeout=60)


@pytest.mark.parametrize("call", [
    lambda: api.create_community(CONNECTION, "   ", "", []),
    lambda: api.create_community(CONNECTION, "x" * (api.NAME_LIMIT + 1), "", []),
    lambda: api.create_community(CONNECTION, "School", "", ["5511900000001@c.us"]),
    lambda: api.deactivate_community(CONNECTION, "5511900000001@c.us"),
    lambda: api.link_groups(CONNECTION, SCHOOL.jid, ["nope"]),
    lambda: api.promote_admin(CONNECTION, SCHOOL.jid, "111-3@g.us"),
    lambda: api.promote_admin(CONNECTION, SCHOOL.jid, "333:2@lid"),
    lambda: api.demote_admin(CONNECTION, SCHOOL.jid, None),
])
def test_a_malformed_request_is_never_sent(monkeypatch, call):
    post = Mock()
    monkeypatch.setattr(api, "api_post", post)
    with pytest.raises(ValueError):
        call()
    post.assert_not_called()


@pytest.mark.parametrize("response", [
    answer(None),  # a library function that returns nothing: 200 with no body
    # What the real routes answered: create, link, unlink, deactivate, demote.
    answer({"wid": "999-0@g.us", "subGroups": {"linkedGroupJids": ["111-3@g.us"], "failedGroups": []}}),
    answer({"linkedGroupJids": ["111-3@g.us"], "failedGroups": []}),
    answer({"unlinkedGroupJids": ["111-3@g.us"], "failedGroups": []}),
    answer({"parent_group_jid": "111-0@g.us"}),
    answer({"participants": ["222@lid"], "success": True}),
    answer({"status": "success", "response": {"promoted": True}}), answer([]), answer("ok")])
def test_an_answer_of_any_shape_is_only_not_a_refusal(monkeypatch, response):
    monkeypatch.setattr(api, "api_post", Mock(return_value=response))
    assert api.deactivate_community(CONNECTION, SCHOOL.jid) is None


@pytest.mark.parametrize("response", [
    answer({"status": "error", "message": "Error on create community"}, 500),
    answer({"status": "error", "code": "promotion_refused"}, 409),
    answer({"status": "error"}),
    # demote answers 200 with success: false when WhatsApp refuses.
    answer({"participants": ["222@lid"], "success": False, "error": {}})])
def test_a_refusal_is_an_error(monkeypatch, response):
    monkeypatch.setattr(api, "api_post", Mock(return_value=response))
    with pytest.raises(ValueError):
        api.demote_admin(CONNECTION, SCHOOL.jid, OTHER_ADMIN.jid)


# ── believing an action ────────────────────────────────────────────────────

def run(action, readings, achieved, attempts=3):
    read = Mock(side_effect=readings)
    sleep = Mock()
    outcome = api.run_action(CONNECTION, action, achieved, attempts=attempts, read=read, sleep=sleep)
    return outcome, read, sleep


def test_an_action_is_done_only_when_a_reading_shows_it():
    action = Mock()
    outcome, read, sleep = run(action, [[SCHOOL], [SCHOOL], []], api.gone(SCHOOL.jid))
    assert outcome == ("done", [])
    action.assert_called_once_with()  # never retried
    assert read.call_count == 3 and sleep.call_count == 2


def test_accepted_but_not_visible_is_unconfirmed_not_done():
    outcome, read, _sleep = run(Mock(), [[SCHOOL]] * 3, api.gone(SCHOOL.jid))
    assert outcome == ("unconfirmed", [SCHOOL]) and read.call_count == 3


def test_a_refused_action_is_failed_only_after_every_reading():
    outcome, read, sleep = run(Mock(side_effect=ValueError()), [[SCHOOL]] * 3, api.gone(SCHOOL.jid))
    assert outcome == ("failed", [SCHOOL])
    assert read.call_count == 3 and sleep.call_count == 2


def test_a_refusal_whose_effect_shows_up_a_moment_later_is_done():
    """Seen on a real account: creating with no group answers an error (the
    library links an empty list next, which WhatsApp refuses) after the
    community exists, and the page shows it about half a second later. The
    reading decides, not the error."""
    check = api.created(frozenset({SCHOOL.jid}), " Club ")
    outcome, read, _sleep = run(Mock(side_effect=ValueError()), [[SCHOOL], [SCHOOL, CLUB]], check)
    assert outcome == ("done", [SCHOOL, CLUB]) and read.call_count == 2


def test_readings_that_fail_leave_the_action_unconfirmed():
    outcome, _read, _sleep = run(Mock(), [OSError(), OSError(), OSError()], api.gone(SCHOOL.jid))
    assert outcome == ("unconfirmed", None)


def test_created_needs_a_new_community_with_that_name():
    check = api.created(frozenset({SCHOOL.jid}), "School")
    assert not check([SCHOOL])  # same name, but it was already there
    assert not check([SCHOOL, CLUB])
    assert check([SCHOOL, Community("999-0@g.us", "School", "", True, (), ())])


def test_linked_and_unlinked_look_at_every_group_asked_for():
    assert api.linked(SCHOOL.jid, [PLAIN.jid, ANNOUNCEMENTS.jid])([SCHOOL])
    assert not api.linked(SCHOOL.jid, [PLAIN.jid, "444-0@g.us"])([SCHOOL])
    assert api.linked(SCHOOL.jid, ["444-0@g.us"], present=False)([SCHOOL])
    assert not api.linked(SCHOOL.jid, [PLAIN.jid], present=False)([SCHOOL])
    # A community that is no longer readable confirms nothing, in either sense.
    assert not api.linked(SCHOOL.jid, [PLAIN.jid], present=False)([CLUB])


def test_admin_matches_one_person_across_identifier_forms():
    key = {"222@lid": "person-b", "5511900000002@c.us": "person-b",
           CREATOR.jid: "person-a"}.get
    assert api.admin(SCHOOL.jid, key("5511900000002@c.us"), key)([SCHOOL])
    assert not api.admin(SCHOOL.jid, "person-c", key)([SCHOOL])
    assert api.admin(SCHOOL.jid, "person-c", key, present=False)([SCHOOL])
    assert not api.admin(SCHOOL.jid, "person-c", key, present=False)([CLUB])


def test_groups_offered_for_linking_are_named_groups_in_no_community():
    chats = {"444-0@g.us": {"name": "zebra"}, "555-0@g.us": {"name": "Alpha"},
             PLAIN.jid: {"name": "Study"}, SCHOOL.jid: {"name": "School"},
             "666-0@g.us": {"name": ""}, "777-0@g.us": {"name": "777-0@g.us"},
             "5511900000001@s.whatsapp.net": {"name": "A person"}, "888-0@g.us": None}
    assert api.linkable_groups(chats, [SCHOOL, CLUB]) == [("555-0@g.us", "Alpha"), ("444-0@g.us", "zebra")]


# ── the panel, on a stub ───────────────────────────────────────────────────

#: the groups the panel opened (open_group_chat), as (jid, name)
OPENED = []


@pytest.fixture(autouse=True)
def _opened_groups(monkeypatch):
    OPENED.clear()
    monkeypatch.setattr("ui.dialogs.communities.open_group_chat",
                        lambda mw, jid, name: OPENED.append((jid, name)))


class ListBox:
    def __init__(self):
        self.items, self.selection = [], -1
        self.Freeze, self.Thaw, self.SetFocus = Mock(), Mock(), Mock()

    def Set(self, items):
        self.items, self.selection = list(items), -1

    def SetSelection(self, index):
        self.selection = index

    def GetSelection(self):
        return self.selection

    def GetCount(self):
        return len(self.items)


STRINGS = load_strings("en-US")


class Dialog:
    for _name in ("_selected", "_selected_group", "_set_busy", "_update_buttons", "_announce",
                  "_reload", "_finish_reload", "_show", "_show_selected", "_on_select", "_describe",
                  "_on_open", "_on_open_recent", "_open_group", "_recent_group", "_on_enter",
                  "_deliver", "_shown_groups", "_on_filter", "_finish_other_groups",
                  "_on_join", "_finish_join", "_replace_group"):
        locals()[_name] = getattr(CommunitiesPanel, _name)
    for _name in ("_admin_community", "_name_of", "_perform", "_finish_action", "_on_add_groups",
                  "_on_remove_group", "_on_demote", "_on_promote", "_promotion_candidates",
                  "_pick_promotion", "_on_deactivate", "_on_new"):
        locals()[_name] = getattr(CommunityAdminActions, _name)
    del _name

    # Asking for the other groups is tested on its own (TestOtherGroups):
    # everywhere else it would queue work the action tests pop.
    def _ask_for_other_groups(self, community):
        pass

    def __init__(self, communities=(SCHOOL, CLUB)):
        self._connection = CONNECTION
        self._filter, self._refreshed = "all", set()
        self._filter_radio = SimpleNamespace(GetSelection=lambda: 0)
        self._busy, self._loaded, self._communities = False, False, []
        self._closed = SimpleNamespace(is_set=lambda: False, set=Mock())
        self._i18n = SimpleNamespace(t=lambda key: STRINGS[key].replace("&", ""))
        names = {"5511900000001@s.whatsapp.net": "Creator", "222@lid": "Bea", "333@lid": "Caio",
                 "444@lid": "444@lid"}
        self._mw = SimpleNamespace(
            i18n=self._i18n, chats={}, speak_output=SimpleNamespace(output=Mock()),
            _chat_last_ts=lambda chat: chat.get("t", 0) or 1,
            is_chat_locked=lambda jid: False,
            _normalize_jid=lambda jid: jid.replace("@c.us", "@s.whatsapp.net"),
            _is_self_jid=lambda jid: jid == "999@lid", self_reference_label=lambda: "Me",
            _resolve_jid_name=lambda jid, resolve_missing=True: names.get(jid, ""),
            get_group_info_recent=Mock(return_value={}))
        self._list, self._groups = ListBox(), ListBox()
        self._open_recent = SimpleNamespace(Enable=Mock(), SetLabel=Mock())
        self._join = SimpleNamespace(Enable=Mock(), SetLabel=Mock())
        self._details = SimpleNamespace(ChangeValue=Mock())
        self._status = SimpleNamespace(SetLabel=Mock())
        for name in ("_open", "_add_groups", "_remove_group", "_promote", "_demote", "_deactivate",
                     "_new", "_refresh"):
            setattr(self, name, SimpleNamespace(Enable=Mock()))
        self.work = []
        self._choose_many = Mock(return_value=[])
        self._choose_one = Mock(return_value=None)
        self._confirm = Mock(return_value=False)
        self._ask_new_community = Mock(return_value=None)
        if communities is not None:
            self._loaded = True
            self._show(list(communities))

    def _current(self):
        return True

    def _person_key(self, jid):
        return jid.replace("@c.us", "@s.whatsapp.net")

    def _run_async(self, work, done):
        self.work.append((work, done))

    def enabled(self, name):
        return getattr(self, name).Enable.call_args.args[0]

    def said(self):
        return self._mw.speak_output.output.call_args.args[0]

    def select(self, community, group=None):
        self._list.SetSelection(self._communities.index(community))
        self._show_selected(group.jid if group else "")


def test_rows_say_what_a_screen_reader_user_cannot_see():
    i18n = SimpleNamespace(t=lambda key: STRINGS[key])
    assert community_label(SCHOOL, i18n) == "School, you are an admin"
    assert community_label(CLUB, i18n) == "Club"
    assert community_label(Community("1-0@g.us", "", "", False, (), ()), i18n) == "Unnamed community"
    assert subgroup_label(ANNOUNCEMENTS, i18n) == "Notices, announcement group"
    assert subgroup_label(Subgroup("1-2@g.us", "All", False, True), i18n) == "All, general group"
    assert subgroup_label(Subgroup("1-3@g.us", "", False, False), i18n) == "Unnamed group"


def test_a_participant_is_never_read_out_as_an_identifier():
    dialog = Dialog()
    assert person_name(dialog._mw, "222@lid") == "Bea"
    assert person_name(dialog._mw, "5511900000001@c.us") == "Creator"
    assert person_name(dialog._mw, "999@lid") == "Me"
    assert person_name(dialog._mw, "444@lid") == "Unnamed participant"  # the resolver gave the id back
    assert person_name(dialog._mw, "555@lid") == "Unnamed participant"


def test_the_list_is_sorted_and_the_first_community_described():
    dialog = Dialog()
    assert dialog._list.items == ["Club", "School, you are an admin"] and dialog._list.selection == 0
    assert dialog._groups.items == []
    text = dialog._details.ChangeValue.call_args.args[0]
    assert "not joined any group" in text and "not an admin" in text
    dialog.select(SCHOOL)
    assert dialog._groups.items == ["Notices, announcement group", "Study"]
    assert dialog._details.ChangeValue.call_args.args[0] == "Class of 2026\n\nAdmins: Bea, Creator"
    assert dialog._list.Freeze.call_count == dialog._list.Thaw.call_count
    assert dialog._groups.Freeze.call_count == dialog._groups.Thaw.call_count


def test_a_new_reading_keeps_the_selected_community_and_group():
    dialog = Dialog()
    dialog.select(SCHOOL, PLAIN)
    dialog._show([Community("000-0@g.us", "Aardvark", "", False, (), ()), CLUB, SCHOOL])
    assert dialog._selected() == SCHOOL and dialog._selected_group() == PLAIN


def test_admin_buttons_follow_the_selected_community():
    dialog = Dialog()
    for name in ("_add_groups", "_remove_group", "_promote", "_demote", "_deactivate", "_open"):
        assert dialog.enabled(name) is False  # Club: not an admin, no group
    assert dialog.enabled("_new") and dialog.enabled("_refresh")
    dialog.select(SCHOOL)
    for name in ("_add_groups", "_promote", "_demote", "_deactivate", "_open"):
        assert dialog.enabled(name) is True
    assert dialog.enabled("_remove_group") is False  # the announcement group is selected
    dialog.select(SCHOOL, PLAIN)
    assert dialog.enabled("_remove_group") is True
    dialog._set_busy(True)
    assert not any(dialog.enabled(name) for name in (
        "_add_groups", "_remove_group", "_promote", "_demote", "_deactivate", "_open", "_new", "_refresh"))


def test_a_community_cannot_be_created_over_a_list_that_was_never_read():
    dialog = Dialog(communities=None)
    dialog._update_buttons()
    assert dialog.enabled("_new") is False
    dialog._on_new(None)
    dialog._ask_new_community.assert_not_called()


def test_reading_announces_the_count_or_the_failure(monkeypatch):
    dialog = Dialog(communities=None)
    monkeypatch.setattr("ui.dialogs.communities.list_communities", Mock(return_value=[SCHOOL]))
    dialog._reload()
    assert dialog._busy
    dialog._reload()  # a second request while one runs is ignored
    [(work, done)] = dialog.work
    done(work())
    assert not dialog._busy and dialog._loaded and dialog.said() == "Communities: 1"
    dialog._finish_reload(None)
    assert dialog.said().startswith("The communities could not be read")
    assert dialog._communities == [SCHOOL]  # a refresh that failed keeps what was read before
    dialog._finish_reload([])
    assert dialog.said() == "You are not in any community." and dialog._communities == []


def test_opening_a_group_opens_its_conversation():
    dialog = Dialog()
    dialog._on_open(None)
    assert OPENED == []  # Club has no group to open
    dialog.select(SCHOOL, PLAIN)
    dialog._on_open(None)
    assert OPENED == [(PLAIN.jid, PLAIN.name)]


def test_most_recent_group_is_the_one_with_the_newest_activity():
    activity = {ANNOUNCEMENTS.jid: 100, PLAIN.jid: 200}
    assert api.most_recent_group(SCHOOL, lambda jid: activity.get(jid, 0)) == PLAIN
    activity[ANNOUNCEMENTS.jid] = 200  # a tie: the group listed first
    assert api.most_recent_group(SCHOOL, lambda jid: activity.get(jid, 0)) == ANNOUNCEMENTS
    assert api.most_recent_group(SCHOOL, lambda jid: 0) is None
    assert api.most_recent_group(CLUB, lambda jid: 100) is None
    assert api.most_recent_group(None, lambda jid: 100) is None


def test_most_recent_group_never_offers_a_group_the_account_is_not_in():
    """One it left keeps its conversation on file, and cannot be opened."""
    left = Subgroup("111-9@g.us", "Old", False, False, joined=False)
    community = Community("111-0@g.us", "School", "", True, (), (PLAIN, left))
    activity = {PLAIN.jid: 100, left.jid: 900}
    assert api.most_recent_group(community, lambda jid: activity.get(jid, 0)) == PLAIN
    only_left = Community("111-0@g.us", "School", "", True, (), (left,))
    assert api.most_recent_group(only_left, lambda jid: 900) is None


def test_an_empty_filter_says_why_it_is_empty():
    """"Groups I'm not in" left empty means the account is in every one."""
    assert api.empty_groups_key(SCHOOL, "not_joined") == "communities_in_every_group"
    assert api.empty_groups_key(SCHOOL, "joined") == "communities_no_groups"
    assert api.empty_groups_key(CLUB, "not_joined") == "communities_no_groups"
    assert api.empty_groups_key(CLUB, "all") == "communities_no_groups"
    assert api.empty_groups_key(None, "not_joined") == "communities_no_groups"


def test_the_recent_group_button_names_and_opens_the_newest_group():
    dialog = Dialog()
    dialog._mw.chats = {ANNOUNCEMENTS.jid: {"t": 100}, PLAIN.jid: {"t": 200}}
    dialog.select(SCHOOL)
    assert dialog.enabled("_open_recent") is True
    assert dialog._open_recent.SetLabel.call_args.args[0] == "Open the most recent group: Study"
    dialog._on_open_recent(None)
    assert OPENED == [(PLAIN.jid, PLAIN.name)]


def test_the_recent_group_button_never_offers_a_locked_or_unknown_group():
    dialog = Dialog()
    dialog._mw.chats = {PLAIN.jid: {"t": 200}}
    dialog._mw.is_chat_locked = lambda jid: jid == PLAIN.jid
    dialog.select(SCHOOL)
    assert dialog.enabled("_open_recent") is False
    assert dialog._open_recent.SetLabel.call_args.args[0] == "Open the most recent group"
    dialog._on_open_recent(None)
    assert OPENED == []


def test_enter_on_a_community_shows_its_groups_and_on_a_group_opens_it():
    dialog = Dialog()
    dialog._announce = Mock()
    assert dialog._on_enter(dialog._list) is True  # Club: no groups to move to
    dialog._groups.SetFocus.assert_not_called()
    assert "not joined any group" in dialog._announce.call_args.args[0]
    dialog.select(SCHOOL, PLAIN)
    assert dialog._on_enter(dialog._list) is True
    dialog._groups.SetFocus.assert_called_once()
    assert OPENED == []
    assert dialog._on_enter(dialog._groups) is True
    assert OPENED == [(PLAIN.jid, PLAIN.name)]
    assert dialog._on_enter(object()) is False  # a button handles its own Enter


def test_enter_with_every_group_joined_and_the_not_in_filter_says_so():
    dialog = Dialog()
    dialog._announce = Mock()
    dialog.select(SCHOOL)
    dialog._filter = "not_joined"
    dialog._groups.GetCount = lambda: 0
    assert dialog._on_enter(dialog._list) is True
    assert dialog._announce.call_args.args[0] == dialog._i18n.t("communities_in_every_group")
    assert "not joined any group" not in dialog._announce.call_args.args[0]


def finish(dialog, state, communities):
    """Answer the action the dialog queued, as run_action would."""
    work, done = dialog.work.pop()
    done((state, communities))
    return work


def test_adding_groups_sends_the_chosen_ones_and_believes_the_reading(monkeypatch):
    dialog = Dialog()
    dialog.select(SCHOOL)
    dialog._on_add_groups(None)
    assert dialog.said().startswith("There is no group to add")
    dialog._mw.chats = {"444-0@g.us": {"name": "Zebra"}, "555-0@g.us": {"name": "Alpha"}}
    dialog._on_add_groups(None)
    assert dialog._choose_many.call_args.args[1] == ["Alpha", "Zebra"] and not dialog.work
    dialog._choose_many.return_value = [1]
    link = Mock()
    monkeypatch.setattr(api, "link_groups", link)
    captured = {}
    monkeypatch.setattr(api, "run_action", lambda connection, action, achieved: captured.update(
        action=action, achieved=achieved) or ("done", [SCHOOL]))
    dialog._on_add_groups(None)
    assert dialog._busy
    work = dialog.work[-1][0]
    work()
    captured["action"]()
    link.assert_called_once_with(CONNECTION, SCHOOL.jid, ["444-0@g.us"])
    assert not captured["achieved"]([SCHOOL])
    finish(dialog, "done", [SCHOOL])
    assert not dialog._busy and dialog.said().startswith("Done.")


@pytest.mark.parametrize("state,start", [("unconfirmed", "The request was sent"),
                                         ("failed", "The action could not be completed")])
def test_an_action_that_is_not_confirmed_is_never_announced_as_done(state, start):
    dialog = Dialog()
    dialog.select(SCHOOL)
    dialog._finish_action((state, None))
    assert dialog.said().startswith(start)
    dialog._finish_action(None)  # the worker itself broke
    assert dialog.said().startswith("The action could not be completed")
    assert dialog._selected() == SCHOOL  # no reading: the list stays as it was


def test_the_announcement_group_is_never_unlinked_and_removal_is_confirmed(monkeypatch):
    unlink = Mock()
    monkeypatch.setattr(api, "unlink_groups", unlink)
    dialog = Dialog()
    dialog.select(SCHOOL, ANNOUNCEMENTS)
    dialog._on_remove_group(None)
    dialog._confirm.assert_not_called()
    dialog.select(SCHOOL, PLAIN)
    dialog._on_remove_group(None)
    assert "'Study'" in dialog._confirm.call_args.args[0] and not dialog.work  # answered No
    dialog._confirm.return_value = True
    dialog._on_remove_group(None)
    assert len(dialog.work) == 1


def test_nothing_is_asked_about_a_community_the_account_does_not_administer():
    dialog = Dialog()  # Club is selected
    for handler in (dialog._on_add_groups, dialog._on_remove_group, dialog._on_demote,
                    dialog._on_promote, dialog._on_deactivate):
        handler(None)
    assert not dialog.work and not dialog._confirm.called and not dialog._choose_one.called
    assert not dialog._choose_many.called


def test_demoting_offers_the_other_admins_never_the_creator(monkeypatch):
    demote = Mock()
    monkeypatch.setattr(api, "demote_admin", demote)
    captured = {}
    monkeypatch.setattr(api, "run_action", lambda connection, action, achieved: captured.update(
        action=action, achieved=achieved))
    dialog = Dialog()
    dialog.select(SCHOOL)
    dialog._choose_one.return_value = 0
    dialog._on_demote(None)
    assert dialog._choose_one.call_args.args[1] == ["Bea"]
    dialog.work[-1][0]()
    captured["action"]()
    demote.assert_called_once_with(CONNECTION, SCHOOL.jid, "222@lid")
    assert not captured["achieved"]([SCHOOL])  # still an admin: not done


def test_demoting_with_nobody_to_choose_says_so():
    alone = Community("111-0@g.us", "School", "", True, (CREATOR, Admin("999@lid", False)), ())
    dialog = Dialog(communities=[alone])
    dialog._on_demote(None)
    assert dialog.said() == "There is nobody to choose." and not dialog._choose_one.called


def test_promotion_candidates_are_the_announcement_group_minus_admins_and_me(monkeypatch):
    dialog = Dialog()
    dialog.select(SCHOOL)
    dialog._mw.get_group_info_recent.return_value = {"participants": [
        {"id": "333@lid"}, {"id": {"_serialized": "5511900000003@c.us"}}, {"id": "222@lid"},
        {"id": "5511900000001@c.us"}, {"id": "999@lid"}, {"id": "333@lid"}, {"id": "junk"}, None]}
    dialog._on_promote(None)
    assert dialog._busy
    work, done = dialog.work.pop()
    rows = work()
    dialog._mw.get_group_info_recent.assert_called_once_with(ANNOUNCEMENTS.jid)
    assert rows == [("333@lid", "Caio"), ("5511900000003@c.us", "Unnamed participant")]
    promote = Mock()
    monkeypatch.setattr(api, "promote_admin", promote)
    captured = {}
    monkeypatch.setattr(api, "run_action", lambda connection, action, achieved: captured.update(
        action=action, achieved=achieved))
    dialog._choose_one.return_value = 0
    done(rows)
    dialog.work[-1][0]()
    captured["action"]()
    promote.assert_called_once_with(CONNECTION, SCHOOL.jid, "333@lid")
    promoted = Community(SCHOOL.jid, "School", "", True, (CREATOR, Admin("333@lid", False)), ())
    assert captured["achieved"]([promoted]) and not captured["achieved"]([SCHOOL])


def test_participants_that_cannot_be_read_stop_the_promotion():
    dialog = Dialog()
    dialog.select(SCHOOL)
    dialog._on_promote(None)
    work, _done = dialog.work.pop()
    with pytest.raises(ValueError):
        work()  # an empty answer is "could not be read", not "nobody"
    dialog._pick_promotion(SCHOOL, None)
    assert not dialog._busy and dialog.said().startswith("The participants of the community")
    dialog._pick_promotion(SCHOOL, [])
    assert dialog.said() == "There is nobody to choose."
    assert not dialog._choose_one.called and not dialog.work


def test_deactivating_asks_first_and_is_done_when_the_community_is_gone(monkeypatch):
    deactivate = Mock()
    monkeypatch.setattr(api, "deactivate_community", deactivate)
    captured = {}
    monkeypatch.setattr(api, "run_action", lambda connection, action, achieved: captured.update(
        action=action, achieved=achieved))
    dialog = Dialog()
    dialog.select(SCHOOL)
    dialog._on_deactivate(None)
    assert "'School'" in dialog._confirm.call_args.args[0] and not dialog.work
    dialog._confirm.return_value = True
    dialog._on_deactivate(None)
    dialog.work[-1][0]()
    captured["action"]()
    deactivate.assert_called_once_with(CONNECTION, SCHOOL.jid)
    assert captured["achieved"]([CLUB]) and not captured["achieved"]([SCHOOL])


def test_creating_is_done_when_a_community_that_was_not_there_carries_the_name(monkeypatch):
    create = Mock()
    monkeypatch.setattr(api, "create_community", create)
    captured = {}
    monkeypatch.setattr(api, "run_action", lambda connection, action, achieved: captured.update(
        action=action, achieved=achieved))
    dialog = Dialog()
    dialog._mw.chats = {"444-0@g.us": {"name": "Zebra"}, PLAIN.jid: {"name": "Study"}}
    dialog._on_new(None)
    assert dialog._ask_new_community.call_args.args[0] == [("444-0@g.us", "Zebra")] and not dialog.work
    dialog._ask_new_community.return_value = ("Choir", "Sundays", ["444-0@g.us"])
    dialog._on_new(None)
    dialog.work[-1][0]()
    captured["action"]()
    create.assert_called_once_with(CONNECTION, "Choir", "Sundays", ["444-0@g.us"])
    assert not captured["achieved"]([SCHOOL, CLUB])
    assert captured["achieved"]([SCHOOL, CLUB, Community("999-0@g.us", "Choir", "", True, (), ())])


# ── every locale ───────────────────────────────────────────────────────────

#: CommunitiesPanel, CommunityCreateDialog and the New chat dialog's buttons.
WINDOWS = [
    ("communities_list", "communities_groups_list", "communities_details", "communities_open_group",
     "communities_add_groups", "communities_remove_group", "communities_promote",
     "communities_demote", "communities_deactivate", "communities_new", "communities_refresh",
     "close"),
    ("communities_create_name", "communities_create_description",
     "communities_create_choose_groups", "communities_create_action", "cancel"),
    ("search_name_or_number", "new_group", "new_contact", "communities_button", "close"),
]


@pytest.mark.parametrize("locale", registered_locale_codes())
@pytest.mark.parametrize("keys", WINDOWS)
def test_every_control_has_a_letter_of_its_own(locale, keys):
    strings = load_strings(locale)
    letters = {}
    for key in keys:
        assert mnemonic(strings[key]) is not None, key
        letters.setdefault(mnemonic(strings[key]), []).append(key)
    assert not {letter: found for letter, found in letters.items() if len(found) > 1}


def test_a_locked_group_is_never_offered_by_name():
    chats = {"555-0@g.us": {"name": "Alpha"}, "666-0@g.us": {"name": "Secret"}}
    rows = api.linkable_groups(chats, [], is_locked=lambda jid: jid == "666-0@g.us")
    assert rows == [("555-0@g.us", "Alpha")]


SPORTS = Subgroup("111-5@g.us", "Sports", False, False, joined=False)
BOARD = Subgroup("111-6@g.us", "Board", False, False, joined=False, approval=True)
SCHOOL_ALL = Community(SCHOOL.jid, SCHOOL.name, SCHOOL.description, True, SCHOOL.admins,
                       (ANNOUNCEMENTS, PLAIN, SPORTS, BOARD))


class TestOtherGroups:
    """The community's groups the account is not in: listed, filtered, and
    asked of WhatsApp once per community, as WhatsApp Web does on opening it."""

    def test_the_api_marks_the_groups_the_account_is_not_in(self):
        body = {"status": "success", "response": [{**RAW, "subgroups": [
            *RAW["subgroups"],
            {"id": "111-5@g.us", "subject": "Sports", "joined": False},
            {"id": "111-6@g.us", "subject": "Board", "joined": False, "approval": True}]}]}
        [community] = api.parse_communities(body)
        assert community.subgroups[-2:] == (SPORTS, BOARD)
        assert all(group.joined for group in community.subgroups[:-2])  # older API: joined

    def test_the_filter_keeps_all_joined_or_not_joined(self):
        assert api.filter_subgroups(SCHOOL_ALL, "all") == SCHOOL_ALL.subgroups
        assert api.filter_subgroups(SCHOOL_ALL, "joined") == (ANNOUNCEMENTS, PLAIN)
        assert api.filter_subgroups(SCHOOL_ALL, "not_joined") == (SPORTS, BOARD)
        assert api.filter_subgroups(None, "all") == ()

    def test_rows_say_the_account_is_not_in_it(self):
        i18n = SimpleNamespace(t=lambda key: STRINGS[key])
        assert subgroup_label(SPORTS, i18n) == "Sports, you are not in it"
        assert subgroup_label(BOARD, i18n) == "Board, you are not in it, joining needs approval"

    def test_the_radio_filters_the_list_and_keeps_the_group(self):
        dialog = Dialog(communities=(SCHOOL_ALL, CLUB))
        dialog.select(SCHOOL_ALL, PLAIN)
        assert len(dialog._groups.items) == 4
        dialog._filter_radio = SimpleNamespace(GetSelection=lambda: 2)
        dialog._on_filter(None)
        assert dialog._groups.items == ["Sports, you are not in it",
                                        "Board, you are not in it, joining needs approval"]
        dialog._filter_radio = SimpleNamespace(GetSelection=lambda: 1)
        dialog._on_filter(None)
        assert dialog._selected_group() == PLAIN

    def test_a_group_the_account_is_not_in_is_never_opened(self):
        dialog = Dialog(communities=(SCHOOL_ALL, CLUB))
        dialog._announce = Mock()
        dialog.select(SCHOOL_ALL, SPORTS)
        assert dialog.enabled("_open") is False
        dialog._on_open(None)
        assert OPENED == []
        assert "not in this group" in dialog._announce.call_args.args[0]

    def test_a_community_is_asked_about_once_and_the_answer_shown(self, monkeypatch):
        dialog = Dialog()
        dialog._ask_for_other_groups = CommunitiesPanel._ask_for_other_groups.__get__(dialog)
        refresh = Mock(return_value=SCHOOL_ALL)
        monkeypatch.setattr("ui.dialogs.communities.refresh_community", refresh)
        dialog.select(SCHOOL)
        dialog.select(SCHOOL)
        [(work, done)] = dialog.work
        done(work())
        refresh.assert_called_once_with(CONNECTION, SCHOOL.jid)
        assert dialog._selected() == SCHOOL_ALL and len(dialog._groups.items) == 4

    def test_a_failed_answer_is_asked_again_next_time(self):
        dialog = Dialog()
        dialog._refreshed.add(SCHOOL.jid)
        dialog._finish_other_groups(SCHOOL.jid, None)
        assert SCHOOL.jid not in dialog._refreshed
        assert dialog._selected() != SCHOOL_ALL

    def test_no_groups_joined_is_said_even_when_others_are_listed(self):
        only_others = Community("444-0@g.us", "Club", "", False, (), (SPORTS,))
        assert "not joined any group" in Dialog()._describe(only_others)


PENDING = Subgroup("111-6@g.us", "Board", False, False, joined=False, approval=True, requested=True)


class TestJoining:
    """Join, ask to join or withdraw the request, as WhatsApp Web's own
    button offers on a group of the community the account is not in."""

    def test_the_button_offers_what_whatsapp_offers(self):
        assert api.join_action(PLAIN) is None
        assert api.join_action(SPORTS) == "join"
        assert api.join_action(BOARD) == "request"
        assert api.join_action(PENDING) == "cancel"
        assert api.join_action(None) is None

    def test_the_api_reads_a_pending_request(self):
        body = {"status": "success", "response": [{**RAW, "subgroups": [
            {"id": "111-6@g.us", "subject": "Board", "joined": False, "approval": True,
             "requested": True}]}]}
        assert api.parse_communities(body)[0].subgroups == (PENDING,)

    @pytest.mark.parametrize("call, path", [
        (api.join_subgroup, "community-subgroups/111-0@g.us/111-5@g.us/join"),
        (api.cancel_join_request, "community-subgroups/111-0@g.us/111-5@g.us/cancel-request"),
    ])
    def test_one_post_answers_the_state(self, monkeypatch, call, path):
        post = Mock(return_value=SimpleNamespace(
            status_code=200, json=lambda: {"status": "success", "response": {"state": "joined"}}))
        monkeypatch.setattr(api, "api_post", post)
        assert call(CONNECTION, "111-0@g.us", "111-5@g.us") == "joined"
        post.assert_called_once()
        assert post.call_args.args[0].endswith(path)

    @pytest.mark.parametrize("reply, code", [
        (SimpleNamespace(status_code=409, json=lambda: {"status": "error", "code": "hidden_group"}),
         "hidden_group"),
        (SimpleNamespace(status_code=502, json=lambda: {}), "unavailable"),
        (SimpleNamespace(status_code=200, json=lambda: {"status": "success", "response": {"state": "?"}}),
         "unavailable"),
    ])
    def test_anything_but_a_known_state_is_an_error_never_retried(self, monkeypatch, reply, code):
        post = Mock(return_value=reply)
        monkeypatch.setattr(api, "api_post", post)
        with pytest.raises(ValueError, match=code):
            api.join_subgroup(CONNECTION, "111-0@g.us", "111-5@g.us")
        post.assert_called_once()

    def _dialog(self, monkeypatch, outcome):
        dialog = Dialog(communities=(SCHOOL_ALL, CLUB))
        dialog._announce = Mock()
        calls = []

        def answer(connection, community, group):
            calls.append((community, group))
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        monkeypatch.setattr("ui.dialogs.communities.join_subgroup", answer)
        monkeypatch.setattr("ui.dialogs.communities.cancel_join_request", answer)
        return dialog, calls

    def _press(self, dialog):
        dialog._on_join(None)
        [(work, done)] = dialog.work
        dialog.work.clear()
        done(work())

    def test_joining_opens_the_group(self, monkeypatch):
        dialog, calls = self._dialog(monkeypatch, api.JOINED)
        dialog.select(SCHOOL_ALL, SPORTS)
        assert dialog.enabled("_join") is True
        assert dialog._join.SetLabel.call_args.args[0] == "Join group"
        self._press(dialog)
        assert calls == [(SCHOOL.jid, SPORTS.jid)]
        assert OPENED == [(SPORTS.jid, SPORTS.name)]
        assert dialog._announce.call_args_list[0].args[0] == "You joined the group."

    def test_a_request_is_sent_and_the_button_becomes_cancel(self, monkeypatch):
        dialog, _calls = self._dialog(monkeypatch, api.REQUESTED)
        dialog.select(SCHOOL_ALL, BOARD)
        assert dialog._join.SetLabel.call_args.args[0] == "Request to join"
        self._press(dialog)
        assert OPENED == []
        assert dialog._selected_group().requested is True
        assert dialog._join.SetLabel.call_args.args[0] == "Cancel join request"
        assert dialog._announce.call_args.args[0].startswith("Request sent.")

    def test_cancelling_withdraws_the_request(self, monkeypatch):
        with_pending = Community(SCHOOL.jid, SCHOOL.name, SCHOOL.description, True, SCHOOL.admins,
                                 (ANNOUNCEMENTS, PLAIN, PENDING))
        dialog, calls = self._dialog(monkeypatch, api.CANCELLED)
        dialog._show([with_pending, CLUB])
        dialog.select(with_pending, PENDING)
        self._press(dialog)
        assert dialog._selected_group().requested is False
        assert dialog._announce.call_args.args[0] == "Request cancelled."

    @pytest.mark.parametrize("code, said", [
        ("hidden_group", "This group is hidden"),
        ("group_unavailable", "no longer available"),
        ("unavailable", "Could not join the group"),
    ])
    def test_a_refusal_is_said_and_nothing_changes(self, monkeypatch, caplog, code, said):
        dialog, _calls = self._dialog(monkeypatch, ValueError(code))
        dialog.select(SCHOOL_ALL, SPORTS)
        self._press(dialog)
        assert said in dialog._announce.call_args.args[0]
        assert dialog._selected_group() == SPORTS and not dialog._busy
        assert OPENED == []
        assert caplog.messages == [f"[communities] join_failed action=join code={code}"]

    def test_nothing_is_sent_for_a_group_the_account_is_in(self, monkeypatch):
        dialog, calls = self._dialog(monkeypatch, api.JOINED)
        dialog.select(SCHOOL_ALL, PLAIN)
        assert dialog.enabled("_join") is False
        dialog._on_join(None)
        assert dialog.work == [] and calls == []
