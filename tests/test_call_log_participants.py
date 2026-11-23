"""A one-to-one call that became a group call is named after its people.

WhatsApp keeps such a call as a ``call_log`` of the one-to-one chat (or of the
account's own chat, for one placed here) whose ``callParticipants`` lists more
than the two ends; the phone and WhatsApp Web title it "A", "A and B",
"A, B and C" or "A and N more people" (WAWebCallLogTitle /
formatParticipantWidsPreserveOrder, read over CDP 2026-11-17). WinZapp read
only the count, so the row named the chat — for a call placed here, the
account itself.
"""

from calls_panel import CallsPanel
from core.call_log import call_log_label, call_log_participants, call_log_payload
from core.call_log_participants import (
    call_participant_jids,
    is_upgraded_call,
    order_call_participants,
    other_call_participants,
    participants_title,
)
from main import MainWindow

ME_LID = "68900000000001@lid"
ME = "5511900000000@s.whatsapp.net"
ANA = "42900000000021@lid"
BIA = "22600000000014@lid"
CAIO = "18800000000012@lid"
DUDA = "83200000000077@lid"


def _t(key):
    return {
        "group_activity_and": "{first} e {second}",
        "group_activity_comma": "{first}, {second}",
        "call_log_participants_more": "{first} e mais {count} pessoas",
        "call_log_label_with_participants": "{label} com {names}",
        "call_log_made_voice": "Ligação de voz efetuada",
        "call_log_answered_voice": "Ligação de voz atendida",
        "duration": "duração",
    }.get(key, f"[{key}]")


class _I18n:
    t = staticmethod(_t)


def _raw(*jids, **extra):
    """A call_log as WPPConnect serializes it (measured: participant strings,
    numeric outcome)."""
    return {"type": "call_log", "callOutcome": "Completed", "finalCallOutcome": "Completed",
            "callDuration": 125, "isVideoCall": False,
            "callParticipants": [{"participant": j, "outcome": 1} for j in jids], **extra}


def _record(chat, *jids, from_me=False):
    return {"key": {"remoteJid": chat, "fromMe": from_me, "id": "C1"},
            "messageType": "callLogMessage",
            "message": {"callLogMessage": call_log_payload(_raw(*jids))},
            "messageTimestamp": 100}


# ── The record ───────────────────────────────────────────────────────────────

class TestPayload:
    def test_a_call_that_became_a_group_call_keeps_its_people(self):
        payload = call_log_payload(_raw(ME_LID, ANA, "5511988887777:12@c.us"))
        assert payload["isGroupCall"] is True
        assert payload["participants"] == [ME_LID, ANA, "5511988887777@s.whatsapp.net"]

    def test_a_plain_one_to_one_call_stores_no_list(self):
        assert "participants" not in call_log_payload(_raw(ME_LID, ANA))
        assert "participants" not in call_log_payload({"type": "call_log"})

    def test_the_jids_are_read_from_strings_and_objects_once_each(self):
        raw = [{"participant": ANA}, {"participant": {"_serialized": ANA}},
               {"participant": "x"}, {"participant": BIA}, "junk"]
        assert call_participant_jids(raw) == [ANA, BIA]

    def test_a_record_without_the_list_has_no_participants(self):
        assert call_log_participants({"messageType": "callLogMessage",
                                      "message": {"callLogMessage": {}}}) == []


# ── The pure rules ───────────────────────────────────────────────────────────

class TestTitle:
    def test_one_two_and_three_names(self):
        assert participants_title(["Ana"], _t) == "Ana"
        assert participants_title(["Ana", "Bia"], _t) == "Ana e Bia"
        assert participants_title(["Ana", "Bia", "Caio"], _t) == "Ana, Bia e Caio"

    def test_four_or_more_say_how_many_more(self):
        assert participants_title(["Ana", "Bia", "Caio", "Duda"], _t) == "Ana e mais 3 pessoas"
        assert participants_title(["A", "B", "C", "D", "E", "F"], _t) == "A e mais 5 pessoas"

    def test_the_account_and_repeated_people_are_left_out(self):
        mine = {"68900000000001", "5511900000000"}
        person = {"5511988887777@s.whatsapp.net": "5511988887777@s.whatsapp.net",
                  "55000000000099@lid": "5511988887777@s.whatsapp.net"}.get
        jids = [ME_LID, ANA, "5511988887777@s.whatsapp.net", "55000000000099@lid", ME]
        assert other_call_participants(jids, mine, lambda j: person(j) or j) == [
            ANA, "5511988887777@s.whatsapp.net"]

    def test_the_caller_first_then_saved_contacts_in_listed_order(self):
        saved = {CAIO}
        assert order_call_participants([ANA, BIA, CAIO, DUDA], caller=DUDA,
                                       is_saved=lambda j: j in saved) == [DUDA, CAIO, ANA, BIA]
        assert order_call_participants([ANA, BIA], caller="") == [ANA, BIA]

    def test_only_a_non_group_chat_with_two_or_more_others_is_renamed(self):
        assert is_upgraded_call(ANA, [ANA, BIA]) is True
        assert is_upgraded_call(ANA, [ANA]) is False
        assert is_upgraded_call("123@g.us", [ANA, BIA]) is False


class TestLabel:
    def test_the_sentence_says_who_the_call_was_with(self):
        msg = _record(ANA, ME_LID, ANA, BIA)
        label = call_log_label(msg, _I18n(), lambda s: f"{s}s", "Ana e Bia")
        assert label == "Ligação de voz atendida com Ana e Bia, duração: 125s"

    def test_without_people_the_sentence_is_unchanged(self):
        msg = _record(ANA, ME_LID, ANA, BIA)
        assert call_log_label(msg, _I18n(), lambda s: f"{s}s") == (
            "Ligação de voz atendida, duração: 125s")


# ── MainWindow: names from the account's own data ────────────────────────────

class _MW:
    call_log_participants_text = MainWindow.call_log_participants_text
    ongoing_call_people_text = MainWindow.ongoing_call_people_text
    _call_person = MainWindow._call_person

    def __init__(self, saved=()):
        self.my_jid, self.my_lid = ME, ME_LID
        self.i18n = _I18n()
        self._lid_to_phone = {}
        self._saved = set(saved)
        self.names = {ANA: "Ana", BIA: "Bia", CAIO: "Caio", DUDA: "Duda"}

    def chat_display_name(self, chat):
        return self.names.get(chat["remoteJid"], "?")

    def _is_self_jid(self, jid):
        return jid in (ME, ME_LID)

    def _get_contact_tolerant(self, jid):
        return {"name": self.names[jid], "isMyContact": True} if jid in self._saved else None


class TestMainWindowText:
    def test_an_incoming_call_names_the_caller_first(self):
        msg = _record(BIA, ME_LID, ANA, BIA, CAIO)
        assert _MW().call_log_participants_text(msg, BIA) == "Bia, Ana e Caio"

    def test_a_call_placed_here_filed_under_the_own_chat(self):
        msg = _record(ME_LID, ME_LID, ANA, BIA, CAIO, DUDA, from_me=True)
        assert _MW().call_log_participants_text(msg) == "Ana e mais 3 pessoas"

    def test_a_plain_one_to_one_call_keeps_the_chat_name(self):
        msg = _record(ANA, ME_LID, ANA)
        assert _MW().call_log_participants_text(msg, ANA) == ""

    def test_a_group_chat_call_keeps_the_group_name(self):
        msg = _record("120363000000000000@g.us", ME_LID, ANA, BIA)
        assert _MW().call_log_participants_text(msg, "120363000000000000@g.us") == ""


class TestLiveCallText:
    """A live call (Calls tab, Ongoing) is named as the phone names it."""

    def test_a_call_started_on_the_phone_is_named_after_who_is_in_it(self):
        # filed under this account's own chat: never "Me"
        live = {"id": "C1", "chatJid": ME_LID, "isVideo": False, "people": [ANA]}
        assert _MW().ongoing_call_people_text(live) == "Ana"
        live["people"] = [ANA, BIA, CAIO, DUDA]
        assert _MW().ongoing_call_people_text(live) == "Ana e mais 3 pessoas"

    def test_a_private_call_that_became_a_group_call_names_its_creator_first(self):
        live = {"id": "C1", "chatJid": BIA, "isVideo": False, "people": [ANA, BIA]}
        assert _MW().ongoing_call_people_text(live) == "Bia e Ana"

    def test_a_call_with_one_other_person_or_a_groups_keeps_the_chat_name(self):
        assert _MW().ongoing_call_people_text(
            {"id": "C1", "chatJid": BIA, "isVideo": False, "people": [BIA]}) == ""
        assert _MW().ongoing_call_people_text(
            {"id": "C1", "chatJid": "120363000000000000@g.us", "isVideo": False,
             "people": [ANA, BIA]}) == ""
        assert _MW().ongoing_call_people_text({"id": "C1", "chatJid": ME_LID, "isVideo": False}) == ""


class TestCallsTabRow:
    class _Stub:
        _row_text = CallsPanel._row_text

        def __init__(self):
            conv = type("CP", (), {
                "_format_duration": staticmethod(lambda s: f"{s}s"),
                "_extract_timestamp": staticmethod(lambda m: m.get("messageTimestamp")),
                "_format_date": staticmethod(lambda ts: "01:03"),
            })()
            self.main_window = _MW()
            self.main_window.conversations_panel = conv

    def test_the_row_is_named_after_the_people_not_the_chat(self):
        msg = _record(ME_LID, ME_LID, ANA, BIA, CAIO, DUDA, from_me=True)
        text = self._Stub()._row_text({"jid": ME_LID, "msg": msg})
        assert text == "Ana e mais 3 pessoas: Ligação de voz efetuada, duração: 125s, 01:03"
