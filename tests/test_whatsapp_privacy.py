"""File > WhatsApp settings > Privacy: what is shown, what is sent, and what
counts as done (core/whatsapp_privacy.py), the server calls
(main_window/whatsapp_privacy.py) and the people offered for a Status audience
(ui/dialogs/status_audience_dialog.audience_rows). No window is opened.
"""

import pytest

from core import whatsapp_privacy as privacy
from main_window import whatsapp_privacy as mixin_module
from main_window.contacts import ContactsMixin
from main_window.whatsapp_privacy import WhatsAppPrivacyMixin

SIX = {
    "lastSeen": "contacts", "online": "all", "about": "all",
    "profilePicture": "contacts", "readReceipts": "all", "groupAdd": "contacts",
}


def _field(setting):
    return next(f for f in privacy.PRIVACY_FIELDS if f.setting == setting)


# ── Account settings ─────────────────────────────────────────────────────────

class TestAccountSettings:
    def test_every_field_offers_only_values_the_server_accepts(self):
        # Mirrors ACCOUNT_SETTINGS in api_patches/src/controller/privacyController.ts.
        accepted = {
            "lastSeen": {"all", "contacts", "none"},
            "online": {"all", "match_last_seen"},
            "about": {"all", "contacts", "none"},
            "profilePicture": {"all", "contacts", "none"},
            "readReceipts": {"all", "none"},
            "groupAdd": {"all", "contacts"},
        }
        assert {f.setting: {v for v, _k in f.options} for f in privacy.PRIVACY_FIELDS} == accepted

    def test_an_unknown_value_leaves_the_dropdown_empty(self):
        assert privacy.option_index(_field("about"), "contact_blacklist") == -1
        assert privacy.option_index(_field("about"), "contacts") == 1

    def test_an_answer_with_no_known_value_is_unreadable(self):
        assert privacy.unreadable({"lastSeen": None})
        assert privacy.unreadable(None)
        assert not privacy.unreadable(SIX)

    def test_unrecognized_fields_are_named(self):
        settings = {**SIX, "lastSeen": "contact_blacklist"}
        assert [f.setting for f in privacy.unrecognized_fields(settings)] == ["lastSeen"]

    def test_only_what_changed_is_sent(self):
        chosen = {"lastSeen": 2, "online": 0, "about": -1}
        assert privacy.changed_privacy(SIX, chosen) == [("lastSeen", "none")]

    def test_a_setting_not_read_back_is_not_confirmed(self):
        readback = {**SIX, "lastSeen": "contacts"}
        assert privacy.not_confirmed([("lastSeen", "none")], readback) == ["lastSeen"]
        assert privacy.not_confirmed([("lastSeen", "contacts")], readback) == []
        assert privacy.not_confirmed([("about", "none")], None) == ["about"]


# ── Who can see my Status ────────────────────────────────────────────────────

class TestStatusPrivacy:
    @pytest.mark.parametrize("raw,expected", [
        ("contact", privacy.STATUS_CONTACTS),
        ("contacts", privacy.STATUS_CONTACTS),
        ("deny-list", privacy.STATUS_EXCEPT),
        ("blacklist", privacy.STATUS_EXCEPT),
        ("allow-list", privacy.STATUS_ONLY),
        ("whitelist", privacy.STATUS_ONLY),
    ])
    def test_known_spellings(self, raw, expected):
        assert privacy.parse_status_privacy({"setting": raw, "list": []}).setting == expected

    def test_an_unknown_setting_leaves_status_out(self):
        assert privacy.parse_status_privacy({"setting": "everyone"}) is None
        assert privacy.parse_status_privacy(None) is None

    def test_a_list_that_was_not_read_is_none_not_empty(self):
        parsed = privacy.parse_status_privacy({"setting": "deny-list", "list": None})
        assert parsed.people is None

    @pytest.mark.parametrize("jid,expected", [
        ("5551999998888@s.whatsapp.net", "5551999998888@c.us"),
        ("5551999998888:12@s.whatsapp.net", "5551999998888@c.us"),
        ("5551999998888@c.us", "5551999998888@c.us"),
        ("123456789012345@lid", "123456789012345@lid"),
        ("120363000000000000@g.us", ""),
        ("status@broadcast", ""),
        ("", ""),
    ])
    def test_status_list_ids(self, jid, expected):
        assert privacy.jid_for_status_list(jid) == expected

    def test_unchanged_choice_sends_nothing(self):
        loaded = privacy.StatusPrivacy(privacy.STATUS_EXCEPT, ("5551999998888@c.us",))
        assert privacy.status_change(loaded, privacy.STATUS_EXCEPT, None) is None
        assert privacy.status_change(
            loaded, privacy.STATUS_EXCEPT, ["5551999998888@s.whatsapp.net"]) is None

    def test_my_contacts_never_sends_a_list(self):
        loaded = privacy.StatusPrivacy(privacy.STATUS_ONLY, ("5551999998888@c.us",))
        assert privacy.status_change(loaded, privacy.STATUS_CONTACTS, None) == (
            privacy.STATUS_CONTACTS, [])

    def test_picked_people_are_sent_once_each(self):
        loaded = privacy.StatusPrivacy(privacy.STATUS_CONTACTS, ())
        change = privacy.status_change(loaded, privacy.STATUS_ONLY, [
            "5551999998888@s.whatsapp.net", "5551999998888@c.us", "120363@g.us"])
        assert change == (privacy.STATUS_ONLY, ["5551999998888@c.us"])

    def test_a_list_that_could_not_be_read_is_only_replaced_on_purpose(self):
        loaded = privacy.StatusPrivacy(privacy.STATUS_EXCEPT, None)
        assert privacy.status_change(loaded, privacy.STATUS_EXCEPT, None) is None
        assert privacy.status_change(loaded, privacy.STATUS_EXCEPT, ["5551999998888@c.us"]) == (
            privacy.STATUS_EXCEPT, ["5551999998888@c.us"])

    def test_only_share_with_nobody_is_refused(self):
        loaded = privacy.StatusPrivacy(privacy.STATUS_CONTACTS, ())
        change = privacy.status_change(loaded, privacy.STATUS_ONLY, None)
        assert privacy.status_needs_people(change)
        assert not privacy.status_needs_people(
            privacy.status_change(loaded, privacy.STATUS_EXCEPT, None))

    def test_a_person_goes_on_the_list_by_lid_when_one_is_known(self):
        bridge = {"5551999998888@s.whatsapp.net": "111111111111111@lid"}
        assert privacy.status_list_id("5551999998888@s.whatsapp.net", bridge) == "111111111111111@lid"
        assert privacy.status_list_id("5551999998888@c.us", bridge) == "111111111111111@lid"

    def test_the_lid_is_found_under_the_other_brazilian_form(self):
        nine = {"5551999998888@s.whatsapp.net": "111111111111111@lid"}
        eight = {"555199998888@s.whatsapp.net": "222222222222222@lid:3"}
        assert privacy.status_list_id("555199998888@s.whatsapp.net", nine) == "111111111111111@lid"
        assert privacy.status_list_id("5551999998888@s.whatsapp.net", eight) == "222222222222222@lid"

    def test_without_a_known_lid_the_phone_form_goes(self):
        assert privacy.status_list_id("5551999998888@s.whatsapp.net", {}) == "5551999998888@c.us"
        assert privacy.status_list_id("5551999998888@s.whatsapp.net", None) == "5551999998888@c.us"
        assert privacy.status_list_id("333333333333333@lid", {}) == "333333333333333@lid"
        assert privacy.status_list_id("120363000000000000@g.us", {}) == ""

    def test_the_change_uses_the_given_id_form_for_both_sides(self):
        bridge = {"5551999998888@s.whatsapp.net": "111111111111111@lid"}
        to_id = lambda jid: privacy.status_list_id(jid, bridge)  # noqa: E731
        loaded = privacy.StatusPrivacy(privacy.STATUS_EXCEPT, ("111111111111111@lid",))
        # Same person picked again by phone: nothing to send.
        assert privacy.status_change(loaded, privacy.STATUS_EXCEPT,
                                     ["5551999998888@s.whatsapp.net"], to_id=to_id) is None
        assert privacy.status_change(loaded, privacy.STATUS_ONLY,
                                     ["5551999998888@s.whatsapp.net"], to_id=to_id) == (
            privacy.STATUS_ONLY, ["111111111111111@lid"])

    def test_the_list_read_back_must_hold_exactly_the_people_sent(self):
        same = {"111111111111111@lid": "A", "5551999998888@c.us": "A",
                "222222222222222@lid": "B"}.get
        sent = ["5551999998888@c.us"]
        held = privacy.StatusPrivacy(privacy.STATUS_EXCEPT, ("111111111111111@lid",))
        assert privacy.status_list_confirmed(sent, held, privacy.STATUS_EXCEPT, same)
        # The setting stuck but the person was dropped (phone id on an @lid account).
        dropped = privacy.StatusPrivacy(privacy.STATUS_EXCEPT, ())
        assert not privacy.status_list_confirmed(sent, dropped, privacy.STATUS_EXCEPT, same)
        extra = privacy.StatusPrivacy(privacy.STATUS_EXCEPT, ("111111111111111@lid", "222222222222222@lid"))
        assert not privacy.status_list_confirmed(sent, extra, privacy.STATUS_EXCEPT, same)

    def test_a_wrong_setting_or_an_unread_list_confirms_nothing(self):
        same = lambda jid: jid  # noqa: E731
        assert not privacy.status_list_confirmed(["1@lid"], None, privacy.STATUS_EXCEPT, same)
        assert not privacy.status_list_confirmed(
            ["1@lid"], privacy.StatusPrivacy(privacy.STATUS_CONTACTS, ()), privacy.STATUS_EXCEPT, same)
        assert not privacy.status_list_confirmed(
            ["1@lid"], privacy.StatusPrivacy(privacy.STATUS_EXCEPT, None), privacy.STATUS_EXCEPT, same)
        assert privacy.status_list_confirmed(
            [], privacy.StatusPrivacy(privacy.STATUS_CONTACTS, ()), privacy.STATUS_CONTACTS, same)


class TestAudienceSelection:
    ROWS = [("a", "Ana", "a@s.whatsapp.net"), ("b", "Bruno", "b@s.whatsapp.net"),
            ("c", "Carla", "c@s.whatsapp.net")]

    @staticmethod
    def _matches(query, label):
        return query.lower() in label.lower()

    def test_starts_with_the_people_already_on_the_list(self):
        sel = privacy.AudienceSelection(self.ROWS, {"b"}, self._matches)
        assert sel.filter("") == [("Ana", False), ("Bruno", True), ("Carla", False)]
        assert sel.chosen() == ["b@s.whatsapp.net"]

    def test_filtering_never_adds_or_drops_a_ticked_person(self):
        sel = privacy.AudienceSelection(self.ROWS, set(), self._matches)
        sel.filter("ana")
        assert sel.set_checked(0, True)
        assert sel.filter("bru") == [("Bruno", False)]
        assert sel.set_checked(0, True)
        assert sel.filter("carla") == [("Carla", False)]
        assert sel.chosen() == ["a@s.whatsapp.net", "b@s.whatsapp.net"]
        sel.filter("")
        assert sel.set_checked(0, False)
        assert sel.chosen() == ["b@s.whatsapp.net"]

    def test_the_placeholder_row_is_not_a_person(self):
        sel = privacy.AudienceSelection(self.ROWS, set(), self._matches)
        assert sel.filter("zzz") == []
        assert not sel.set_checked(0, True)
        assert sel.chosen() == []


# ── Server calls ─────────────────────────────────────────────────────────────

class _Response:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


class _Stub(WhatsAppPrivacyMixin):
    wpp_server = "http://127.0.0.1"
    wpp_port = 6300
    token = "SESSION:TOKEN"


@pytest.fixture
def posts(monkeypatch):
    calls = []
    answers = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append((url.rsplit("/api/SESSION:TOKEN/", 1)[1], json))
        return answers.pop(0)

    monkeypatch.setattr(mixin_module, "api_post", fake_post)
    return calls, answers


class TestServerCalls:
    def test_read_returns_settings_and_status(self, posts):
        calls, answers = posts
        answer = {"settings": SIX, "status": {"setting": "contact", "list": []}}
        answers.append(_Response(200, {"status": "success", "response": answer}))
        assert _Stub().fetch_privacy_settings() == answer
        assert calls == [("privacy", {})]

    def test_a_failed_read_is_none(self, posts):
        _calls, answers = posts
        answers.append(_Response(503, {"status": "error", "code": "privacy_unavailable"}))
        assert _Stub().fetch_privacy_settings() is None

    def test_a_read_without_settings_is_none(self, posts):
        _calls, answers = posts
        answers.append(_Response(200, {"status": "success", "response": {"status": None}}))
        assert _Stub().fetch_privacy_settings() is None

    def test_a_failed_setter_reports_its_code_and_kind(self, posts):
        calls, answers = posts
        answers.append(_Response(502, {"code": "privacy_setting_failed", "kind": "TypeError"}))
        assert _Stub().set_privacy_setting("about", "none") == "privacy_setting_failed (TypeError)"
        assert calls == [("privacy/set", {"setting": "about", "value": "none"})]

    def test_status_audience_is_confirmed_by_reading_the_list_back(self, posts):
        calls, answers = posts
        answers.append(_Response(200, {"status": "success", "response": {"setting": "deny-list"}}))
        answers.append(_Response(200, {"status": "success", "response": {
            "settings": SIX, "status": {"setting": "deny-list", "list": ["111111111111111@lid"]}}}))
        assert _Stub().set_status_privacy("deny-list", ["111111111111111@lid"]) == (True, None)
        assert calls == [("privacy/status", {"setting": "deny-list", "list": ["111111111111111@lid"]}),
                         ("privacy", {})]

    def test_status_audience_dropped_by_whatsapp_is_not_confirmed(self, posts):
        _calls, answers = posts
        answers.append(_Response(200, {"status": "success", "response": {"setting": "deny-list"}}))
        answers.append(_Response(200, {"status": "success", "response": {
            "settings": SIX, "status": {"setting": "deny-list", "list": []}}}))
        assert _Stub().set_status_privacy("deny-list", ["5551999998888@c.us"]) == (False, None)

    def test_status_list_id_uses_the_bridge(self):
        stub = _Stub()
        stub._phone_to_lid = {"5551999998888@s.whatsapp.net": "111111111111111@lid"}
        assert stub.status_list_id("5551999998888@s.whatsapp.net") == "111111111111111@lid"
        assert _Stub().status_list_id("5551999998888@s.whatsapp.net") == "5551999998888@c.us"

    def test_an_exception_is_an_error_string_not_a_crash(self, monkeypatch):
        def boom(*args, **kwargs):
            raise ConnectionError("refused")

        monkeypatch.setattr(mixin_module, "api_post", boom)
        assert _Stub().fetch_privacy_settings() is None
        assert _Stub().set_privacy_setting("about", "all") == "ConnectionError"
        assert _Stub().set_status_privacy("contact", []) == (False, "ConnectionError")


# ── People offered for a Status audience ─────────────────────────────────────

class _I18n:
    def t(self, key):
        return {"wa_privacy_status_unknown_contact": "Not in your contacts ({number})"}.get(key, key)


class _Mw:
    _get_contact_tolerant = ContactsMixin._get_contact_tolerant

    def __init__(self, contacts):
        self.contacts = contacts
        self.chats = {}
        self._lid_to_phone = {"999999999999999@lid": "5551999998888@s.whatsapp.net"}
        self.i18n = _I18n()

    def _normalize_jid(self, jid):
        return jid.replace("@c.us", "@s.whatsapp.net")


class TestAudienceRows:
    def test_contacts_then_people_only_whatsapp_knows(self):
        from ui.dialogs.status_audience_dialog import audience_rows

        mw = _Mw({
            "5551999998888@s.whatsapp.net": {"name": "Bruno", "isMyContact": True},
            "5551988887777@s.whatsapp.net": {"name": "Ana", "isMyContact": True},
        })
        rows = audience_rows(mw, ["999999999999999@lid", "5511912345678@c.us"])
        jids = [jid for _key, _label, jid in rows]
        # Ana and Bruno once each (Bruno's @lid is the same person), then the
        # one person on WhatsApp's list who is not a synchronised contact.
        assert jids == ["5551988887777@s.whatsapp.net", "5551999998888@s.whatsapp.net",
                        "5511912345678@c.us"]
        assert rows[-1][1].startswith("Not in your contacts (")

    @pytest.mark.parametrize("reported, stored", [
        ("5511912345678@c.us", "5511912345678@s.whatsapp.net"),
        ("999999999999999@lid", "5551999998888@s.whatsapp.net"),
        # Brazilian mobile reported with 8 digits, stored with the 9.
        ("551199998888@c.us", "5511999998888@s.whatsapp.net"),
    ])
    def test_a_known_non_contact_is_named_whatever_form_whatsapp_reports(self, reported, stored):
        from ui.dialogs.status_audience_dialog import audience_rows

        mw = _Mw({stored: {"pushName": "Carla"}})
        [(_key, label, jid)] = audience_rows(mw, [reported])
        assert label == "Carla" and jid == reported

    def test_a_nameless_lid_record_does_not_hide_the_phone_s_name(self):
        from ui.dialogs.status_audience_dialog import audience_rows

        mw = _Mw({"999999999999999@lid": {"isMyContact": False},
                  "5551999998888@s.whatsapp.net": {"pushName": "Carla"}})
        [(_key, label, _jid)] = audience_rows(mw, ["999999999999999@lid"])
        assert label == "Carla"

    def test_an_unbridged_lid_never_shows_its_digits(self):
        from ui.dialogs.status_audience_dialog import audience_rows

        mw = _Mw({"444444444444444@lid": {"pushName": "Duda"}})
        rows = audience_rows(mw, ["444444444444444@lid", "555555555555555@lid"])
        labels = [label for _key, label, _jid in rows]
        assert labels == ["Duda", "unknown_contact"]
        assert not any("555555555555555" in label for label in labels)


class TestApplyOutcome:
    CHANGES = [("lastSeen", "contacts"), ("about", "none"), ("online", "match_last_seen")]

    def test_only_what_the_read_back_showed_counts_as_confirmed(self):
        confirmed, status_ok = privacy.apply_outcome(
            self.CHANGES, {"about": "failed"}, ["online"], None, None, None)
        # A setting that failed or did not stick keeps its last known value,
        # so the next Apply sends it again.
        assert confirmed == {"lastSeen": "contacts"}
        assert status_ok is False

    def test_everything_confirmed(self):
        confirmed, _ = privacy.apply_outcome(self.CHANGES, {}, [], None, None, None)
        assert confirmed == dict(self.CHANGES)

    @pytest.mark.parametrize("status_error, status_unconfirmed, expected", [
        (None, False, True),
        ("status_privacy_failed", False, False),
        (None, True, False),
    ])
    def test_the_status_change_counts_only_when_it_took_effect(self, status_error,
                                                               status_unconfirmed, expected):
        _, status_ok = privacy.apply_outcome(
            [], {}, [], ("contact", []), status_error, status_unconfirmed)
        assert status_ok is expected
