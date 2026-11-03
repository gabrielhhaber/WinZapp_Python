"""Tests for MainWindow._is_group_send_restricted().

Regression coverage: opening a WhatsApp "announcement" group (only admins
can send messages) let a non-admin member type and attempt to send a
message that WhatsApp itself would silently reject — there was no local
signal at all that the group was even restricted.

This function is deliberately fail-open: whenever neither the local group
metadata nor the persisted verdict gives a clear answer (no announce flag, no
participants list, the current user not found among participants), it must
return False (message field stays writable) rather than risk locking out a
user who can actually post — see the function's own docstring for the
reasoning.

Four later defects in the same area are pinned here as well:

* the announce flag reached Python only through list-chats' groupMetadata and
  was stored nowhere, while the `chats` table has no group-metadata column at
  all — so `self.chats = self.get_chats()` rehydrated every group without it
  and the composer failed open for the whole of startup, permanently while
  offline. The verdict now rides in system_metadata (`group_send_perms`),
  written by _record_group_send_perms() and consulted here.
* the participant match read `admin`/`isAdmin` and never `isSuperAdmin`.
  WhatsApp Web reports a group's own creator as isAdmin=false,
  isSuperAdmin=true, so WinZapp locked the creator out of their own
  announcement group — the fail-*closed* direction. Upstream WPPConnect's
  getGroupInfo ORs all three fields for exactly this reason.
* the persisted verdict itself became a *new* way to lock a user out: its
  admin half could only ever be refreshed by a list-chats snapshot carrying
  groupMetadata.participants, so a member promoted to admin in an
  announcement group kept `{"announce": True, "am_admin": False}` on disk,
  across restarts, with the composer read-only and the screen reader saying
  only admins may post. Promote/demote notifications now update it, and a
  verdict older than _GROUP_SEND_PERMS_MAX_AGE_SECONDS is treated as no
  answer at all — fail-open again, which is the rule this area is built on.
* the composer read `subtype` raw while the timeline renderer lower-cases it,
  so an "Announce" notification rendered a correct timeline line and left the
  message field writable on a group that had just gone announcement-only.
"""

import time
import types

import pytest

from core.utils import group_setting_notif_value
from main import (
    MainWindow,
    group_participant_admin_flag,
    group_participant_is_me,
    group_send_permission_from_metadata,
    set_group_participant_admin,
    unexpired_group_send_verdict,
)


def _verdict(announce, am_admin, age_seconds=0):
    """A persisted verdict as _record_group_send_perms() writes it, *age*
    seconds old. Written relative to now because the verdict expires — a
    hardcoded "t" would silently be an ancient one."""
    return {"announce": announce, "am_admin": am_admin,
            "t": int(time.time()) - age_seconds}


class _Stub:
    def __init__(self, my_jid="5511999999999@s.whatsapp.net", my_lid="",
                 group_send_perms=None):
        self.my_jid = my_jid
        self.my_lid = my_lid
        # What prepare_sync() restores from system_metadata; empty on a stub
        # that only exercises the live-metadata path.
        self._group_send_perms = group_send_perms or {}

    _is_group_send_restricted = MainWindow._is_group_send_restricted
    _phone_digits_equivalent  = staticmethod(MainWindow._phone_digits_equivalent)
    _GROUP_SEND_PERMS_MAX_AGE_SECONDS = MainWindow._GROUP_SEND_PERMS_MAX_AGE_SECONDS


def _group(announce=True, participants=None, jid="123456-group@g.us"):
    return {
        "remoteJid": jid,
        "groupMetadata": {
            "announce": announce,
            "participants": participants or [],
        },
    }


class TestNotAGroup:
    def test_a_private_chat_is_never_restricted(self):
        mw = _Stub()
        chat = {"remoteJid": "5511988888888@s.whatsapp.net"}
        assert mw._is_group_send_restricted(chat) is False


class TestAnnounceFlag:
    def test_announce_off_is_never_restricted(self):
        mw = _Stub()
        chat = _group(announce=False, participants=[
            {"id": "5511999999999@s.whatsapp.net", "admin": None},
        ])
        assert mw._is_group_send_restricted(chat) is False

    def test_missing_announce_flag_is_not_restricted(self):
        mw = _Stub()
        chat = {"remoteJid": "123456-group@g.us", "groupMetadata": {"participants": []}}
        assert mw._is_group_send_restricted(chat) is False


class TestFailsOpenWithoutParticipantData:
    def test_announce_on_but_no_participants_list_fails_open(self):
        mw = _Stub()
        chat = _group(announce=True, participants=[])
        assert mw._is_group_send_restricted(chat) is False

    def test_current_user_not_found_in_participants_fails_open(self):
        mw = _Stub()
        chat = _group(announce=True, participants=[
            {"id": "5511911111111@s.whatsapp.net", "admin": "admin"},
        ])
        assert mw._is_group_send_restricted(chat) is False


class TestAdminStatus:
    def test_non_admin_member_is_restricted(self):
        mw = _Stub(my_jid="5511999999999@s.whatsapp.net")
        chat = _group(announce=True, participants=[
            {"id": "5511911111111@s.whatsapp.net", "admin": "superadmin"},
            {"id": "5511999999999@s.whatsapp.net", "admin": None},
        ])
        assert mw._is_group_send_restricted(chat) is True

    def test_admin_member_is_not_restricted(self):
        mw = _Stub(my_jid="5511999999999@s.whatsapp.net")
        chat = _group(announce=True, participants=[
            {"id": "5511999999999@s.whatsapp.net", "admin": "admin"},
        ])
        assert mw._is_group_send_restricted(chat) is False

    def test_superadmin_member_is_not_restricted(self):
        mw = _Stub(my_jid="5511999999999@s.whatsapp.net")
        chat = _group(announce=True, participants=[
            {"id": "5511999999999@s.whatsapp.net", "admin": "superadmin"},
        ])
        assert mw._is_group_send_restricted(chat) is False

    def test_matches_via_lid_when_phone_jid_not_the_participant_id(self):
        mw = _Stub(my_jid="5511999999999@s.whatsapp.net", my_lid="1234567890@lid")
        chat = _group(announce=True, participants=[
            {"id": "1234567890@lid", "admin": None},
        ])
        assert mw._is_group_send_restricted(chat) is True

    def test_isadmin_boolean_field_is_also_recognized(self):
        mw = _Stub(my_jid="5511999999999@s.whatsapp.net")
        chat = _group(announce=True, participants=[
            {"id": "5511999999999@s.whatsapp.net", "isAdmin": True},
        ])
        assert mw._is_group_send_restricted(chat) is False

    def test_superadmin_boolean_field_alone_is_recognized(self):
        # The group's creator: WhatsApp Web reports isAdmin=false alongside
        # isSuperAdmin=true, and reading isAdmin alone locked them out of
        # their own announcement group.
        mw = _Stub(my_jid="5511999999999@s.whatsapp.net")
        chat = _group(announce=True, participants=[
            {"id": "5511999999999@s.whatsapp.net",
             "isAdmin": False, "isSuperAdmin": True},
        ])
        assert mw._is_group_send_restricted(chat) is False

    def test_superadmin_with_device_suffix_on_the_participant_id(self):
        # WPPConnect's participant ids routinely carry the ":60" companion
        # suffix; it must not defeat the self-match.
        mw = _Stub(my_jid="5511999999999@s.whatsapp.net")
        chat = _group(announce=True, participants=[
            {"id": "5511999999999:60@s.whatsapp.net", "isSuperAdmin": True},
        ])
        assert mw._is_group_send_restricted(chat) is False


class TestPersistedVerdict:
    """The composer's answer before any list-chats snapshot has landed."""

    _JID = "123456-group@g.us"

    def test_persisted_verdict_restricts_a_chat_with_no_group_metadata(self):
        # Exactly what get_chats() rehydrates at startup: a group dict with no
        # groupMetadata whatsoever. Without the persisted verdict this failed
        # open and showed a writable message field to a non-admin. Still the
        # right rule — but only while the verdict is recent enough to be worth
        # trusting; see TestPersistedVerdictExpiry.
        mw = _Stub(group_send_perms={self._JID: _verdict(True, False)})
        assert mw._is_group_send_restricted({"remoteJid": self._JID}) is True

    def test_persisted_verdict_leaves_an_admin_writable(self):
        mw = _Stub(group_send_perms={self._JID: _verdict(True, True)})
        assert mw._is_group_send_restricted({"remoteJid": self._JID}) is False

    def test_no_metadata_and_no_persisted_verdict_still_fails_open(self):
        mw = _Stub()
        assert mw._is_group_send_restricted({"remoteJid": self._JID}) is False

    def test_live_participants_outrank_a_stale_persisted_verdict(self):
        # Promoted since the verdict was stored: this snapshot says admin, so
        # the composer must open back up.
        mw = _Stub(group_send_perms={self._JID: _verdict(True, False)})
        chat = _group(announce=True, jid=self._JID, participants=[
            {"id": "5511999999999@s.whatsapp.net", "admin": "admin"},
        ])
        assert mw._is_group_send_restricted(chat) is False

    def test_live_announce_reuses_the_persisted_admin_answer(self):
        # list-chats is called with ignoreGroupMetadata, so a group can
        # serialise with announce and no participants at all. The admin half
        # of the answer then has to come from the stored verdict, or the
        # restriction is lost the moment participants stop arriving.
        mw = _Stub(group_send_perms={self._JID: _verdict(False, False)})
        chat = {"remoteJid": self._JID, "groupMetadata": {"announce": True}}
        assert mw._is_group_send_restricted(chat) is True

    def test_live_announce_off_outranks_a_restricting_persisted_verdict(self):
        mw = _Stub(group_send_perms={self._JID: _verdict(True, False)})
        chat = {"remoteJid": self._JID, "groupMetadata": {"announce": False}}
        assert mw._is_group_send_restricted(chat) is False


class TestPersistedVerdictExpiry:
    """An aged verdict is no answer, and no answer means fail open.

    The reported failure: a member of an announcement group is promoted to
    admin, no list-chats snapshot happens to carry participants afterwards,
    and the stored {"announce": True, "am_admin": False} keeps the composer
    read-only across restarts — a lockout from a group the user can post in.
    """

    _JID = "123456-group@g.us"
    _OLD = MainWindow._GROUP_SEND_PERMS_MAX_AGE_SECONDS + 60

    def test_an_expired_restricting_verdict_fails_open(self):
        mw = _Stub(group_send_perms={
            self._JID: _verdict(True, False, age_seconds=self._OLD)})
        assert mw._is_group_send_restricted({"remoteJid": self._JID}) is False

    def test_a_verdict_just_under_the_limit_is_still_trusted(self):
        mw = _Stub(group_send_perms={
            self._JID: _verdict(True, False, age_seconds=60)})
        assert mw._is_group_send_restricted({"remoteJid": self._JID}) is True

    def test_an_expired_verdict_does_not_fill_the_admin_gap_either(self):
        # A live snapshot stating announce with no participants used to reuse
        # the stored am_admin. Doing that with an expired record would keep
        # the lockout alive indefinitely, since re-observing the same answer
        # never rewrites "t".
        mw = _Stub(group_send_perms={
            self._JID: _verdict(True, False, age_seconds=self._OLD)})
        chat = {"remoteJid": self._JID, "groupMetadata": {"announce": True}}
        assert mw._is_group_send_restricted(chat) is False

    def test_a_verdict_with_no_timestamp_is_treated_as_expired(self):
        # Same reasoning unresolvable_lids records: an entry of unknown age is
        # better re-derived than trusted.
        mw = _Stub(group_send_perms={
            self._JID: {"announce": True, "am_admin": False}})
        assert mw._is_group_send_restricted({"remoteJid": self._JID}) is False


class TestUnexpiredGroupSendVerdict:
    """The expiry rule itself, with no stub at all."""

    _MAX = 24 * 3600

    def test_a_fresh_record_is_returned_unchanged(self):
        stored = {"announce": True, "am_admin": False, "t": 1000}
        assert unexpired_group_send_verdict(stored, 1000 + 60, self._MAX) is stored

    def test_an_old_record_is_none(self):
        stored = {"announce": True, "am_admin": False, "t": 1000}
        assert unexpired_group_send_verdict(stored, 1000 + self._MAX + 1, self._MAX) is None

    def test_exactly_at_the_limit_still_counts(self):
        stored = {"announce": True, "am_admin": False, "t": 1000}
        assert unexpired_group_send_verdict(stored, 1000 + self._MAX, self._MAX) is stored

    @pytest.mark.parametrize("stored", [
        None,
        "not a dict",
        {"announce": True, "am_admin": False},
        {"announce": True, "am_admin": False, "t": 0},
        {"announce": True, "am_admin": False, "t": "yesterday"},
        {"announce": True, "am_admin": False, "t": True},
    ])
    def test_anything_unusable_is_none(self, stored):
        assert unexpired_group_send_verdict(stored, 100000, self._MAX) is None


class TestGroupParticipantAdminFlag:
    """The module-level participant scan, tested without any stub at all."""

    def test_returns_none_when_the_list_cannot_answer(self):
        assert group_participant_admin_flag([], "5511999999999", "", lambda a, b: a == b) is None
        assert group_participant_admin_flag(None, "5511999999999", "", lambda a, b: a == b) is None
        assert group_participant_admin_flag(
            [{"id": "5511911111111@s.whatsapp.net", "admin": "admin"}],
            "5511999999999", "", lambda a, b: a == b,
        ) is None

    def test_serialized_participant_id_shape_is_accepted(self):
        # Some WPPConnect versions serialise the id as {"_serialized": ...}.
        assert group_participant_admin_flag(
            [{"id": {"_serialized": "5511999999999@s.whatsapp.net"}, "isSuperAdmin": True}],
            "5511999999999", "", lambda a, b: a == b,
        ) is True

    def test_plain_member_answers_false(self):
        assert group_participant_admin_flag(
            [{"id": "5511999999999@s.whatsapp.net", "admin": None}],
            "5511999999999", "", lambda a, b: a == b,
        ) is False


class TestVerdictFromMetadata:
    def test_undecidable_when_announce_is_not_stated(self):
        assert group_send_permission_from_metadata(
            {"remoteJid": "g@g.us"}, "5511999999999", "", lambda a, b: a == b) is None

    def test_undecidable_when_announce_is_on_and_admin_status_unknown(self):
        # Never guessed: a guess written into the persisted verdict becomes
        # authoritative on the next launch.
        assert group_send_permission_from_metadata(
            {"remoteJid": "g@g.us", "groupMetadata": {"announce": True}},
            "5511999999999", "", lambda a, b: a == b,
        ) is None

    def test_known_am_admin_fills_the_gap(self):
        assert group_send_permission_from_metadata(
            {"remoteJid": "g@g.us", "groupMetadata": {"announce": True}},
            "5511999999999", "", lambda a, b: a == b, known_am_admin=False,
        ) == {"announce": True, "am_admin": False}

    def test_announce_off_decides_without_participants(self):
        assert group_send_permission_from_metadata(
            {"remoteJid": "g@g.us", "groupMetadata": {"announce": False}},
            "5511999999999", "", lambda a, b: a == b,
        ) == {"announce": False, "am_admin": False}


class _FakeDb:
    def __init__(self):
        self.metadata = {}

    def set_metadata_json(self, key, value):
        self.metadata[key] = value


class _RecorderStub(_Stub):
    """Carries what the recording/notification funnels touch on top of _Stub.

    conversations_panel stays None on purpose: the wx.CallAfter that refreshes
    the open composer needs a running wx.App, and the panel side of that is
    covered by tests/test_composer_permissions.py instead.
    """

    _record_group_send_perms = MainWindow._record_group_send_perms
    _persist_group_send_perms = MainWindow._persist_group_send_perms
    _apply_group_settings_change = MainWindow._apply_group_settings_change
    _apply_group_admin_change = MainWindow._apply_group_admin_change
    _group_admin_notif_targets_me = MainWindow._group_admin_notif_targets_me
    _is_self_jid = MainWindow._is_self_jid
    _GROUP_ANNOUNCE_NOTIF_SUBTYPES = MainWindow._GROUP_ANNOUNCE_NOTIF_SUBTYPES
    _GROUP_RESTRICT_NOTIF_SUBTYPES = MainWindow._GROUP_RESTRICT_NOTIF_SUBTYPES
    _GROUP_ADMIN_NOTIF_SUBTYPES = MainWindow._GROUP_ADMIN_NOTIF_SUBTYPES

    def __init__(self, lid_to_phone=None, **kwargs):
        super().__init__(**kwargs)
        self.db = _FakeDb()
        self.conversations_panel = None
        # _is_self_jid() bridges an @lid through this cache before comparing.
        self._lid_to_phone = lid_to_phone or {}


_JID = "123456-group@g.us"


def _notif_msg(subtype, value=None, body="", recipients=None):
    return {
        "key": {"remoteJid": _JID, "fromMe": False, "id": "N1"},
        "messageType": "groupNotification",
        "message": {"groupNotification": {
            "subtype": subtype, "value": value, "body": body,
            "recipients": recipients or [],
        }},
        "messageTimestamp": 1700000000,
    }


class TestRecordGroupSendPerms:
    def test_records_a_decidable_verdict(self):
        mw = _RecorderStub()
        chat = _group(announce=True, jid=_JID, participants=[
            {"id": "5511999999999@s.whatsapp.net", "admin": None},
        ])
        verdict = mw._record_group_send_perms(_JID, chat)
        assert verdict["announce"] is True and verdict["am_admin"] is False
        assert mw._group_send_perms[_JID]["t"] > 0

    def test_an_unchanged_verdict_does_not_rewrite_the_timestamp(self):
        # The periodic list-chats refresh runs every 60s; rewriting here would
        # be one DB write per group per minute for no new information.
        mw = _RecorderStub()
        chat = _group(announce=True, jid=_JID, participants=[
            {"id": "5511999999999@s.whatsapp.net", "admin": None},
        ])
        first = dict(mw._record_group_send_perms(_JID, chat))
        again = mw._record_group_send_perms(_JID, chat)
        assert again["t"] == first["t"]

    def test_an_undecidable_snapshot_records_nothing(self):
        mw = _RecorderStub()
        chat = {"remoteJid": _JID, "groupMetadata": {"announce": True}}
        assert mw._record_group_send_perms(_JID, chat) is None
        assert mw._group_send_perms == {}

    def test_persist_writes_through_the_db_metadata_store(self):
        mw = _RecorderStub(group_send_perms={_JID: _verdict(True, False)})
        mw._persist_group_send_perms()
        assert mw.db.metadata["group_send_perms"][_JID]["announce"] is True

    def test_an_expired_verdict_is_re_stamped_when_re_confirmed(self):
        # Treating an expired record as "previous" would return it unchanged
        # here, so its "t" would never move again and it would stay expired
        # for good — the composer failing open on every launch for a group
        # whose answer simply never changes.
        old = MainWindow._GROUP_SEND_PERMS_MAX_AGE_SECONDS + 60
        mw = _RecorderStub(group_send_perms={
            _JID: _verdict(True, False, age_seconds=old)})
        chat = _group(announce=True, jid=_JID, participants=[
            {"id": "5511999999999@s.whatsapp.net", "admin": None},
        ])
        verdict = mw._record_group_send_perms(_JID, chat)
        assert verdict["announce"] is True and verdict["am_admin"] is False
        assert mw._is_group_send_restricted({"remoteJid": _JID}) is True


class TestLiveSettingsNotification:
    """An announce toggle arriving over the socket, not through list-chats."""

    def test_announce_on_updates_group_metadata_and_the_verdict(self):
        mw = _RecorderStub(group_send_perms={_JID: _verdict(False, False)})
        chat = {"remoteJid": _JID, "groupMetadata": {"announce": False}}
        mw._apply_group_settings_change(_JID, chat, _notif_msg("announce", value="on"))
        assert chat["groupMetadata"]["announce"] is True
        assert mw._group_send_perms[_JID]["announce"] is True
        assert mw.db.metadata["group_send_perms"][_JID]["announce"] is True

    def test_a_capitalised_subtype_is_recognised(self):
        # The timeline renderer lower-cases this field and the composer did
        # not, so an "Announce" notification rendered a correct line in the
        # conversation and left the message field writable.
        mw = _RecorderStub(group_send_perms={_JID: _verdict(False, False)})
        chat = {"remoteJid": _JID, "groupMetadata": {"announce": False}}
        mw._apply_group_settings_change(_JID, chat, _notif_msg("Announce", value="on"))
        assert chat["groupMetadata"]["announce"] is True
        assert mw._group_send_perms[_JID]["announce"] is True

    def test_announce_off_reopens_the_verdict(self):
        mw = _RecorderStub(group_send_perms={_JID: _verdict(True, False)})
        chat = {"remoteJid": _JID, "groupMetadata": {"announce": True}}
        mw._apply_group_settings_change(_JID, chat, _notif_msg("announce", value="off"))
        assert mw._group_send_perms[_JID]["announce"] is False

    def test_a_notification_with_no_stated_value_changes_nothing(self):
        # Guessing "on" here would lock the composer off a payload that never
        # said so — the fail-closed direction.
        mw = _RecorderStub(group_send_perms={_JID: _verdict(False, False)})
        chat = {"remoteJid": _JID, "groupMetadata": {"announce": False}}
        mw._apply_group_settings_change(_JID, chat, _notif_msg("announce"))
        assert chat["groupMetadata"]["announce"] is False
        assert mw._group_send_perms[_JID]["announce"] is False

    def test_restrict_subtype_touches_restrict_and_leaves_announce_alone(self):
        # "restrict" is who may edit the group's info, not who may send.
        mw = _RecorderStub()
        chat = {"remoteJid": _JID, "groupMetadata": {"announce": False}}
        mw._apply_group_settings_change(_JID, chat, _notif_msg("locked", value="on"))
        assert chat["groupMetadata"]["restrict"] is True
        assert chat["groupMetadata"]["announce"] is False

    def test_unrelated_subtypes_are_ignored(self):
        mw = _RecorderStub()
        chat = {"remoteJid": _JID, "groupMetadata": {"announce": False}}
        mw._apply_group_settings_change(_JID, chat, _notif_msg("subject", body="Novo nome"))
        assert chat["groupMetadata"] == {"announce": False}
        assert mw._group_send_perms == {}

    def test_a_private_chat_is_never_touched(self):
        mw = _RecorderStub()
        jid = "5511988888888@s.whatsapp.net"
        chat = {"remoteJid": jid}
        mw._apply_group_settings_change(jid, chat, _notif_msg("announce", value="on"))
        assert "groupMetadata" not in chat


class TestAdminNotification:
    """promote/demote: the only live event that refreshes the admin half of
    the verdict.

    The reported failure: a member of an announcement group is promoted to
    admin and nothing here reacted, so the persisted
    {"announce": True, "am_admin": False} kept the composer read-only across
    restarts — with the screen reader announcing that only admins may post in
    a group the user had just been given the right to post in.
    """

    _ME = "5511999999999@s.whatsapp.net"

    def _announce_group(self, am_admin=False):
        return _group(announce=True, jid=_JID, participants=[
            {"id": "5511911111111@s.whatsapp.net", "admin": "superadmin"},
            {"id": self._ME, "admin": "admin" if am_admin else None},
        ])

    def test_being_promoted_unlocks_the_composer(self):
        mw = _RecorderStub(group_send_perms={_JID: _verdict(True, False)})
        chat = self._announce_group()
        assert mw._is_group_send_restricted(chat) is True
        mw._apply_group_settings_change(
            _JID, chat, _notif_msg("promote", recipients=[self._ME]))
        assert mw._group_send_perms[_JID]["am_admin"] is True
        assert mw.db.metadata["group_send_perms"][_JID]["am_admin"] is True
        assert mw._is_group_send_restricted(chat) is False

    def test_the_stale_participants_entry_is_corrected_too(self):
        # group_send_permission_from_metadata() prefers a live participants
        # list over the stored verdict, so leaving the last snapshot's
        # "admin: None" in place would overrule the promotion and keep the
        # composer read-only exactly as before.
        mw = _RecorderStub(group_send_perms={_JID: _verdict(True, False)})
        chat = self._announce_group()
        mw._apply_group_settings_change(
            _JID, chat, _notif_msg("promote", recipients=[self._ME]))
        me = chat["groupMetadata"]["participants"][1]
        assert me["admin"] == "admin" and me["isAdmin"] is True

    def test_being_demoted_restricts_an_announcement_group(self):
        mw = _RecorderStub(group_send_perms={_JID: _verdict(True, True)})
        chat = self._announce_group(am_admin=True)
        mw._apply_group_settings_change(
            _JID, chat, _notif_msg("demote", recipients=[self._ME]))
        assert mw._group_send_perms[_JID]["am_admin"] is False
        assert mw._is_group_send_restricted(chat) is True

    def test_demotion_clears_every_admin_flag(self):
        # They are read with OR, so one left set still reads as admin.
        mw = _RecorderStub(group_send_perms={_JID: _verdict(True, True)})
        chat = _group(announce=True, jid=_JID, participants=[
            {"id": self._ME, "admin": "superadmin",
             "isAdmin": True, "isSuperAdmin": True},
        ])
        mw._apply_group_settings_change(
            _JID, chat, _notif_msg("demote", recipients=[self._ME]))
        me = chat["groupMetadata"]["participants"][0]
        assert not (me["admin"] or me["isAdmin"] or me["isSuperAdmin"])

    def test_someone_elses_promotion_leaves_the_verdict_alone(self):
        mw = _RecorderStub(group_send_perms={_JID: _verdict(True, False)})
        chat = self._announce_group()
        mw._apply_group_settings_change(
            _JID, chat, _notif_msg("promote", recipients=["5511911111111@s.whatsapp.net"]))
        assert mw._group_send_perms[_JID]["am_admin"] is False
        assert mw.db.metadata == {}

    def test_a_capitalised_subtype_is_recognised(self):
        mw = _RecorderStub(group_send_perms={_JID: _verdict(True, False)})
        chat = self._announce_group()
        mw._apply_group_settings_change(
            _JID, chat, _notif_msg("Promote", recipients=[self._ME]))
        assert mw._group_send_perms[_JID]["am_admin"] is True

    def test_a_device_suffix_on_the_recipient_still_matches(self):
        mw = _RecorderStub(group_send_perms={_JID: _verdict(True, False)})
        chat = self._announce_group()
        mw._apply_group_settings_change(
            _JID, chat, _notif_msg("promote", recipients=["5511999999999:60@s.whatsapp.net"]))
        assert mw._group_send_perms[_JID]["am_admin"] is True

    def test_a_bridged_lid_recipient_matches(self):
        mw = _RecorderStub(group_send_perms={_JID: _verdict(True, False)},
                           lid_to_phone={"1234567890@lid": self._ME})
        chat = self._announce_group()
        mw._apply_group_settings_change(
            _JID, chat, _notif_msg("promote", recipients=["1234567890@lid"]))
        assert mw._group_send_perms[_JID]["am_admin"] is True

    def test_a_notification_naming_nobody_invalidates_the_verdict(self):
        # Undecidable, and this area fails open when it cannot decide:
        # keeping the record would be trusting an am_admin a promotion we
        # could not read may well have just contradicted.
        mw = _RecorderStub(group_send_perms={_JID: _verdict(True, False)})
        chat = self._announce_group()
        mw._apply_group_settings_change(_JID, chat, _notif_msg("promote"))
        assert _JID not in mw._group_send_perms
        assert mw.db.metadata["group_send_perms"] == {}

    def test_an_unbridgeable_lid_recipient_invalidates_the_verdict(self):
        # Neither bridged nor comparable to a known my_lid: this may or may
        # not be us, and "not us" is the answer that keeps the lockout.
        mw = _RecorderStub(group_send_perms={_JID: _verdict(True, False)})
        chat = self._announce_group()
        mw._apply_group_settings_change(
            _JID, chat, _notif_msg("promote", recipients=["1234567890@lid"]))
        assert _JID not in mw._group_send_perms

    def test_an_unknown_announce_value_drops_the_verdict_rather_than_invent_one(self):
        # Nothing here states announce, and the stored verdict's admin half is
        # what this notification just contradicted, so there is no verdict
        # left to state.
        mw = _RecorderStub(group_send_perms={
            _JID: _verdict(True, False,
                           age_seconds=MainWindow._GROUP_SEND_PERMS_MAX_AGE_SECONDS + 60)})
        chat = {"remoteJid": _JID}
        mw._apply_group_settings_change(
            _JID, chat, _notif_msg("promote", recipients=[self._ME]))
        assert _JID not in mw._group_send_perms
        assert mw._is_group_send_restricted(chat) is False

    def test_announce_from_the_stored_verdict_survives_the_promotion(self):
        # The notification says nothing about announce; only the admin half
        # changed.
        mw = _RecorderStub(group_send_perms={_JID: _verdict(True, False)})
        chat = {"remoteJid": _JID}
        mw._apply_group_settings_change(
            _JID, chat, _notif_msg("promote", recipients=[self._ME]))
        assert mw._group_send_perms[_JID] == {
            "announce": True, "am_admin": True,
            "t": mw._group_send_perms[_JID]["t"]}
        assert mw._is_group_send_restricted(chat) is False


class TestSetGroupParticipantAdmin:
    """The participants-list rewrite, tested without any stub at all."""

    _EQ = staticmethod(lambda a, b: a == b)

    def test_returns_false_when_the_user_is_not_in_the_list(self):
        participants = [{"id": "5511911111111@s.whatsapp.net", "admin": None}]
        assert set_group_participant_admin(
            participants, True, "5511999999999", "", self._EQ) is False
        assert participants[0]["admin"] is None

    def test_returns_false_for_a_missing_list(self):
        assert set_group_participant_admin(None, True, "5511999999999", "", self._EQ) is False

    def test_matches_the_serialized_id_shape(self):
        participants = [{"id": {"_serialized": "5511999999999@s.whatsapp.net"}}]
        assert set_group_participant_admin(
            participants, True, "5511999999999", "", self._EQ) is True
        assert group_participant_admin_flag(
            participants, "5511999999999", "", self._EQ) is True

    def test_a_promotion_leaves_a_superadmin_flag_alone(self):
        participants = [{"id": "5511999999999@s.whatsapp.net", "isSuperAdmin": True}]
        set_group_participant_admin(participants, True, "5511999999999", "", self._EQ)
        assert participants[0]["isSuperAdmin"] is True


class TestGroupParticipantIsMe:
    _EQ = staticmethod(lambda a, b: a == b)

    def test_device_suffix_is_stripped(self):
        assert group_participant_is_me(
            {"id": "5511999999999:60@s.whatsapp.net"}, "5511999999999", "", self._EQ) is True

    def test_lid_digits_match_exactly_only(self):
        assert group_participant_is_me(
            {"id": "1234567890@lid"}, "", "1234567890", self._EQ) is True
        assert group_participant_is_me(
            {"id": "1234567890@lid"}, "", "9999999999", self._EQ) is False

    def test_a_malformed_entry_is_never_me(self):
        assert group_participant_is_me("nonsense", "5511999999999", "", self._EQ) is False
        assert group_participant_is_me({}, "5511999999999", "", self._EQ) is False


class TestGroupSettingNotifValue:
    """The value field of a group-settings notification, as WPPConnect spells
    it across versions. Strictly tri-state, unlike parse_bool_flag(""), which
    answers False for a payload that said nothing at all."""

    @pytest.mark.parametrize("raw", ["on", "announcement", "locked", True, 1, "true"])
    def test_on_shapes(self, raw):
        assert group_setting_notif_value({"value": raw}) is True

    @pytest.mark.parametrize("raw", ["off", "unlocked", False, 0, "false"])
    def test_off_shapes(self, raw):
        assert group_setting_notif_value({"value": raw}) is False

    def test_falls_back_to_the_notification_body(self):
        assert group_setting_notif_value({"body": "on"}) is True

    def test_nothing_stated_is_none(self):
        assert group_setting_notif_value({}) is None
        assert group_setting_notif_value({"body": "   "}) is None
        assert group_setting_notif_value(None) is None



class _InfoStub(_Stub):
    """What get_group_info() calls with a group-info answer."""

    _remember_group_send_perms = MainWindow._remember_group_send_perms
    _record_group_send_perms = MainWindow._record_group_send_perms

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.persisted = 0
        self.refreshed = []
        self.conversations_panel = types.SimpleNamespace(refresh_composer_permissions=None)

    def _persist_group_send_perms(self):
        self.persisted += 1


def _info(announce=True, me_admin=False):
    return {"id": "123456-group@g.us", "announce": announce, "participants": [
        {"id": "5511999999999@s.whatsapp.net", "isAdmin": me_admin},
        {"id": "5511888888888@s.whatsapp.net", "isAdmin": True}]}


class TestGroupInfoClosesTheField:
    """REGRESSION (Neonize): list-chats carries no groupMetadata there, so a
    group closed to members kept its message field open until a notice that
    its setting changed. Opening the group asks for group-info, which knows."""

    JID = "123456-group@g.us"

    @pytest.fixture(autouse=True)
    def _calls(self, monkeypatch):
        import main_window.groups as groups
        self.calls = []
        monkeypatch.setattr(groups.wx, "CallAfter",
                            lambda fn, *args: self.calls.append(args))

    def test_a_closed_group_restricts_a_member(self):
        stub = _InfoStub()
        stub._remember_group_send_perms(self.JID, _info())
        assert stub._is_group_send_restricted({"remoteJid": self.JID})
        assert stub.persisted == 1
        assert self.calls == [(self.JID, False)]

    def test_an_admin_keeps_the_field(self):
        stub = _InfoStub()
        stub._remember_group_send_perms(self.JID, _info(me_admin=True))
        assert not stub._is_group_send_restricted({"remoteJid": self.JID})
        assert self.calls == []

    def test_a_group_opened_again_changes_nothing(self):
        stub = _InfoStub(group_send_perms={self.JID: _verdict(True, False)})
        stub._remember_group_send_perms(self.JID, _info())
        assert stub.persisted == 0 and self.calls == []

    def test_a_group_reopened_to_members_frees_the_field(self):
        stub = _InfoStub(group_send_perms={self.JID: _verdict(True, False)})
        stub._remember_group_send_perms(self.JID, _info(announce=False))
        assert not stub._is_group_send_restricted({"remoteJid": self.JID})
        assert self.calls == [(self.JID, True)]

    def test_an_answer_without_the_setting_decides_nothing(self):
        stub = _InfoStub()
        stub._remember_group_send_perms(self.JID, {"participants": []})
        assert stub._group_send_perms == {} and self.calls == []

    def test_an_expired_verdict_is_replaced_saved_and_shown(self):
        """REGRESSION (review): compared with the expired verdict, the fresh
        one looked unchanged and was neither saved nor shown, so the field
        stayed open at every first open after a day."""
        stub = _InfoStub(group_send_perms={self.JID: _verdict(True, False, age_seconds=25 * 3600)})
        assert not stub._is_group_send_restricted({"remoteJid": self.JID})
        stub._remember_group_send_perms(self.JID, _info())
        assert stub._is_group_send_restricted({"remoteJid": self.JID})
        assert stub.persisted == 1
        assert self.calls == [(self.JID, False)]

    def test_get_group_info_hands_its_answer_over(self, monkeypatch):
        import main_window.groups as groups
        stub = _InfoStub()
        stub.wpp_server, stub.wpp_port, stub.token = "http://127.0.0.1", 6300, "T"
        stub._reconcile_group_info_name = lambda jid, info: None
        stub._remember_group_kind = lambda jid, info: None
        monkeypatch.setattr(groups, "api_get", lambda url, **kw: types.SimpleNamespace(
            status_code=200, json=lambda: {"response": _info()}))
        assert MainWindow.get_group_info(stub, self.JID)["announce"] is True
        assert stub._is_group_send_restricted({"remoteJid": self.JID})

