"""Tests for group sender-name resolution.

These cover the "Participante sem nome" bug: in a group, key.participant is
usually a bare @lid that bridges to no phone number, so contact lookups fail and
the only name available is the pushName riding on the message itself.

MainWindow is a wx.Frame and cannot be instantiated without a running app, so
the methods under test are exercised as plain functions against a small stub
that carries just the attributes they touch.  That is deliberate: it keeps these
tests fast and headless, and it also documents exactly which state each method
depends on.
"""

import pytest

from main import MainWindow


class _Stub:
    """Minimal stand-in for MainWindow for name-resolution methods."""

    def __init__(self, **kwargs):
        self.contacts = {}
        self._presence_pushname_map = {}
        self._lid_to_phone = {}
        self._phone_to_lid = {}
        self._unresolvable_lids = set()
        for key, value in kwargs.items():
            setattr(self, key, value)

    # Bound under their real names too, because these methods call each other
    # through self (_learn_sender_names_bulk → _learn_sender_name).
    _normalize_jid = staticmethod(MainWindow._normalize_jid)
    _learn_sender_name = MainWindow._learn_sender_name
    _learn_sender_names_bulk = MainWindow._learn_sender_names_bulk
    _needs_sender_resolution = MainWindow._needs_sender_resolution

    # Short aliases used by the tests.
    learn = _learn_sender_name
    learn_bulk = _learn_sender_names_bulk
    needs_resolution = _needs_sender_resolution


def _group_msg(participant, push_name="", from_me=False, msg_id="A1"):
    return {
        "key": {
            "remoteJid": "1234567890@g.us",
            "participant": participant,
            "fromMe": from_me,
            "id": msg_id,
        },
        "pushName": push_name,
        "message": {"conversation": "oi"},
        "messageType": "conversation",
        "messageTimestamp": 1700000000,
    }


class TestLearnSenderName:
    def test_learns_pushname_for_lid_participant(self):
        mw = _Stub()
        assert mw.learn(_group_msg("55555@lid", "Carlos")) is True
        assert mw._presence_pushname_map["55555@lid"] == "Carlos"

    def test_indexes_both_jid_forms_when_bridge_is_known(self):
        phone = "5511999999999@s.whatsapp.net"
        mw = _Stub(_lid_to_phone={"55555@lid": phone})
        mw.learn(_group_msg("55555@lid", "Carlos"))
        assert mw._presence_pushname_map["55555@lid"] == "Carlos"
        assert mw._presence_pushname_map[phone] == "Carlos"

    def test_indexes_lid_when_learning_from_phone_jid(self):
        phone = "5511999999999@s.whatsapp.net"
        mw = _Stub(_phone_to_lid={phone: "55555@lid"})
        mw.learn(_group_msg(phone, "Carlos"))
        assert mw._presence_pushname_map["55555@lid"] == "Carlos"

    def test_normalizes_legacy_cus_participant(self):
        mw = _Stub()
        mw.learn(_group_msg("5511999999999@c.us", "Carlos"))
        assert "5511999999999@s.whatsapp.net" in mw._presence_pushname_map

    def test_ignores_own_messages(self):
        mw = _Stub()
        assert mw.learn(_group_msg("55555@lid", "Carlos", from_me=True)) is False
        assert mw._presence_pushname_map == {}

    def test_ignores_missing_pushname(self):
        mw = _Stub()
        assert mw.learn(_group_msg("55555@lid", "")) is False
        assert mw._presence_pushname_map == {}

    @pytest.mark.parametrize("push", ["5511999999999", "+55 11 99999-9999", "   "])
    def test_ignores_phone_like_pushname(self, push):
        """A phone number is not a name — storing it would show digits to a
        screen reader instead of a person."""
        mw = _Stub()
        assert mw.learn(_group_msg("55555@lid", push)) is False
        assert mw._presence_pushname_map == {}

    def test_never_attributes_a_name_to_the_group_itself(self):
        """With no participant the lookup falls back to remoteJid; for a group
        that would label every message with the group's name."""
        mw = _Stub()
        msg = _group_msg("", "Carlos")
        del msg["key"]["participant"]
        assert mw.learn(msg) is False
        assert mw._presence_pushname_map == {}

    def test_reports_no_change_when_already_known(self):
        mw = _Stub(_presence_pushname_map={"55555@lid": "Carlos"})
        assert mw.learn(_group_msg("55555@lid", "Carlos")) is False

    def test_reports_change_when_pushname_was_updated(self):
        mw = _Stub(_presence_pushname_map={"55555@lid": "Carlos"})
        # a replayed (non-live) message only fills a gap: it never renames
        assert mw.learn(_group_msg("55555@lid", "Carlos Silva")) is False
        fresh = _group_msg("55555@lid", "Carlos Silva")
        fresh["messageTimestamp"] = int(__import__("time").time())
        assert mw.learn(fresh, live=True) is True
        assert mw._presence_pushname_map["55555@lid"] == "Carlos Silva"

    def test_learns_from_a_private_chat_sender(self):
        mw = _Stub()
        msg = {
            "key": {"remoteJid": "5511999999999@s.whatsapp.net", "fromMe": False, "id": "B1"},
            "pushName": "Alice",
        }
        assert mw.learn(msg) is True
        assert mw._presence_pushname_map["5511999999999@s.whatsapp.net"] == "Alice"

    def test_ignores_status_broadcast(self):
        mw = _Stub()
        msg = {"key": {"remoteJid": "status@broadcast", "fromMe": False, "id": "C1"},
               "pushName": "Alice"}
        assert mw.learn(msg) is False


class TestLearnSenderNamesBulk:
    def test_learns_every_name_in_a_synced_history(self):
        mw = _Stub()
        records = [
            _group_msg("111@lid", "Ana", msg_id="1"),
            _group_msg("222@lid", "Bruno", msg_id="2"),
            _group_msg("111@lid", "Ana", msg_id="3"),
        ]
        assert mw.learn_bulk(records) is True
        assert mw._presence_pushname_map == {"111@lid": "Ana", "222@lid": "Bruno"}

    def test_returns_false_when_nothing_new(self):
        mw = _Stub(_presence_pushname_map={"111@lid": "Ana"})
        assert mw.learn_bulk([_group_msg("111@lid", "Ana")]) is False

    def test_tolerates_junk_records(self):
        mw = _Stub()
        assert mw.learn_bulk([None, "nonsense", 42, {}]) is False

    def test_tolerates_empty_input(self):
        mw = _Stub()
        assert mw.learn_bulk([]) is False
        assert mw.learn_bulk(None) is False


class TestNeedsSenderResolution:
    def test_unknown_lid_needs_resolution(self):
        mw = _Stub()
        assert mw.needs_resolution("55555@lid") is True

    def test_phone_jid_never_needs_resolution(self):
        mw = _Stub()
        assert mw.needs_resolution("5511999999999@s.whatsapp.net") is False

    @pytest.mark.parametrize("value", ["", None, 42, "1234567890@g.us"])
    def test_non_lid_values_are_rejected(self, value):
        mw = _Stub()
        assert mw.needs_resolution(value) is False

    def test_bridged_lid_does_not_need_resolution(self):
        mw = _Stub(_lid_to_phone={"55555@lid": "5511999999999@s.whatsapp.net"})
        assert mw.needs_resolution("55555@lid") is False

    def test_lid_with_contact_name_does_not_need_resolution(self):
        mw = _Stub(contacts={"55555@lid": {"name": "Carlos"}})
        assert mw.needs_resolution("55555@lid") is False

    def test_lid_with_contact_pushname_does_not_need_resolution(self):
        mw = _Stub(contacts={"55555@lid": {"pushName": "Carlos"}})
        assert mw.needs_resolution("55555@lid") is False

    def test_lid_with_learned_pushname_does_not_need_resolution(self):
        """The whole point of learning names from messages: it keeps these
        participants off the API resolver queue."""
        mw = _Stub(_presence_pushname_map={"55555@lid": "Carlos"})
        assert mw.needs_resolution("55555@lid") is False

    def test_blacklisted_lid_is_not_retried(self):
        mw = _Stub(_unresolvable_lids={"55555@lid"})
        assert mw.needs_resolution("55555@lid") is False

    def test_blank_contact_name_still_needs_resolution(self):
        mw = _Stub(contacts={"55555@lid": {"name": "   ", "pushName": ""}})
        assert mw.needs_resolution("55555@lid") is True

    def test_learning_a_name_clears_the_need_for_an_api_lookup(self):
        mw = _Stub()
        assert mw.needs_resolution("55555@lid") is True
        mw.learn(_group_msg("55555@lid", "Carlos"))
        assert mw.needs_resolution("55555@lid") is False


class TestLearnSenderNameStamps:
    LID = "9@lid"

    def _msg(self, name, ts):
        m = _group_msg(self.LID, name)
        m["messageTimestamp"] = ts
        return m

    def test_live_rename_then_old_replay_keeps_new_name(self):
        import time
        stub = _Stub()
        now = int(time.time())
        assert stub.learn(self._msg("alex grupos", now), live=True)
        assert not stub.learn(self._msg("alex", now - 5000))
        assert stub._presence_pushname_map[self.LID] == "alex grupos"

    def test_millisecond_timestamp_does_not_freeze_the_name(self):
        import time
        stub = _Stub()
        now = int(time.time())
        stub.learn(self._msg("alex", now * 1000), live=True)
        assert stub.learn(self._msg("alex grupos", now + 5), live=True)
        assert stub._presence_pushname_map[self.LID] == "alex grupos"

    def test_stale_queued_live_message_does_not_replace_a_known_name(self):
        import time
        stub = _Stub()
        stub._presence_pushname_map[self.LID] = "alex grupos"
        assert not stub.learn(self._msg("alex", int(time.time()) - 86400), live=True)
        assert stub._presence_pushname_map[self.LID] == "alex grupos"


class TestRowsRepaintOnRename:
    LID = "9@lid"

    def test_a_learned_rename_asks_for_the_senders_rows_to_be_redrawn(self):
        import time
        stub = _Stub()
        asked = []
        stub._repaint_rows_for_name_change = asked.append
        msg = _group_msg(self.LID, "Ramiro")
        msg["messageTimestamp"] = int(time.time())
        stub.learn(msg, live=True)
        assert asked and self.LID in asked[0]
        asked.clear()
        stub.learn(msg, live=True)          # same name again: nothing to redraw
        assert not asked

    def test_repaint_request_is_scoped_to_the_jids_and_scheduled_on_the_ui_thread(self, monkeypatch):
        import main_window.message_events as module
        calls = []
        monkeypatch.setattr(module.wx, "CallAfter", lambda f, *a: calls.append((f, a)))
        refresh = object()
        stub = type("S", (), {"_schedule_refresh_active_messages": refresh})()
        module.MessageEventsMixin._repaint_rows_for_name_change(stub, [self.LID, "", "1@s.whatsapp.net"])
        assert calls == [(refresh, ({self.LID, "1@s.whatsapp.net"},))]
