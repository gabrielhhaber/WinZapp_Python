"""Calls as messages: the pure half (core/call_log.py).

WhatsApp keeps every call as a ``call_log`` message in its chat; the record
this module reads is the ``callLogMessage`` content the WebSocket normalizer
builds from it. The fields and the outcome enum were read from a live WhatsApp
Web over CDP on 2026-09-24 (``WAWebCallLogMsgData.flow``).
"""

import pytest

from core.call_log import (
    ACCEPTED_ELSEWHERE,
    CANCELED,
    COMPLETED,
    FAILED,
    MISSED,
    ONGOING,
    REJECTED,
    UNKNOWN,
    call_log_candidate_ids,
    call_log_label,
    call_log_payload,
    call_log_refresh_delays,
    call_log_status,
    call_log_supersedes,
    is_call_log,
    is_call_log_pending,
    is_returnable_missed_call,
)


class _I18n:
    def t(self, key):
        return f"[{key}]"


def _fmt(seconds):
    return f"{seconds}s"


def _record(outcome=MISSED, from_me=False, video=False, duration=0,
            jid="5511999999999@s.whatsapp.net", group_call=False):
    return {
        "key": {"remoteJid": jid, "fromMe": from_me, "id": "00350D7FFB27065CBC5DF80E49E6B7CF"},
        "messageType": "callLogMessage",
        "message": {"callLogMessage": {
            "outcome": outcome, "isVideo": video, "durationSeconds": duration,
            "isGroupCall": group_call,
        }},
        "messageTimestamp": 1790222611,
    }


def _legacy():
    return {"key": {"remoteJid": "1@lid", "fromMe": False, "id": "X"},
            "messageType": "call_log", "message": {}, "messageTimestamp": 1}


class TestPayload:
    def test_the_fields_of_a_real_completed_call(self):
        """Shape measured on the live page (ids shortened)."""
        raw = {
            "type": "call_log", "callOutcome": "Completed", "finalCallOutcome": "Completed",
            "isVideoCall": False, "callDuration": 2241,
            "callParticipants": [{"participant": {"_serialized": "1@lid"}, "outcome": 1},
                                 {"participant": {"_serialized": "2@lid"}, "outcome": 1}],
        }
        assert call_log_payload(raw) == {
            "outcome": "Completed", "isVideo": False, "durationSeconds": 2241,
            "isGroupCall": False,
        }

    def test_a_settled_final_outcome_wins_over_ongoing(self):
        raw = {"callOutcome": ONGOING, "finalCallOutcome": COMPLETED, "callDuration": 5}
        assert call_log_payload(raw)["outcome"] == COMPLETED

    def test_more_than_two_participants_is_a_group_call(self):
        raw = {"callOutcome": MISSED, "callParticipants": [{}, {}, {}]}
        assert call_log_payload(raw)["isGroupCall"] is True

    @pytest.mark.parametrize("bad", [None, "", "abc", -3])
    def test_a_missing_or_bad_duration_is_zero(self, bad):
        assert call_log_payload({"callDuration": bad})["durationSeconds"] == 0


class TestStatus:
    @pytest.mark.parametrize("outcome,from_me,expected", [
        (MISSED, False, "missed"),
        (CANCELED, False, "missed"),
        (REJECTED, False, "declined"),
        (COMPLETED, False, "answered"),
        (ACCEPTED_ELSEWHERE, False, "answered_elsewhere"),
        (COMPLETED, True, "made"),
        (MISSED, True, "unanswered"),
        (CANCELED, True, "unanswered"),
        (REJECTED, True, "declined_by_peer"),
        (FAILED, True, "failed"),
        (ONGOING, True, "ongoing"),
        (UNKNOWN, False, "unknown"),
    ])
    def test_outcome_and_direction(self, outcome, from_me, expected):
        assert call_log_status(_record(outcome, from_me)) == expected


class TestLabel:
    def test_a_missed_voice_call(self):
        assert call_log_label(_record(MISSED), _I18n(), _fmt) == "[call_log_missed_voice]"

    def test_a_declined_video_call(self):
        rec = _record(REJECTED, video=True)
        assert call_log_label(rec, _I18n(), _fmt) == "[call_log_declined_video]"

    def test_an_answered_call_reads_its_duration(self):
        rec = _record(COMPLETED, duration=1859)
        assert call_log_label(rec, _I18n(), _fmt) == (
            "[call_log_answered_voice], [duration]: 1859s")

    def test_a_missed_call_never_reads_a_duration(self):
        rec = _record(MISSED, duration=12)
        assert call_log_label(rec, _I18n(), _fmt) == "[call_log_missed_voice]"

    def test_a_zero_duration_is_left_out(self):
        rec = _record(COMPLETED, from_me=True)
        assert call_log_label(rec, _I18n(), _fmt) == "[call_log_made_voice]"

    def test_a_legacy_record_reads_generically(self):
        assert call_log_label(_legacy(), _I18n(), _fmt) == "[call_log_generic]"

    def test_every_status_has_a_label_for_both_kinds(self):
        for outcome in (COMPLETED, MISSED, REJECTED, CANCELED, ACCEPTED_ELSEWHERE,
                        ONGOING, FAILED, UNKNOWN, ""):
            for from_me in (False, True):
                for video in (False, True):
                    label = call_log_label(_record(outcome, from_me, video), _I18n(), _fmt)
                    assert label.startswith("[call_log_")

    def test_anything_else_has_no_label(self):
        assert call_log_label({"messageType": "conversation"}, _I18n(), _fmt) == ""


class TestReturnable:
    def test_an_incoming_missed_one_to_one_call(self):
        assert is_returnable_missed_call(_record(MISSED)) is True

    @pytest.mark.parametrize("outcome,from_me", [
        (COMPLETED, False), (REJECTED, False), (MISSED, True), (ACCEPTED_ELSEWHERE, False),
    ])
    def test_answered_declined_or_outgoing_calls_are_not(self, outcome, from_me):
        assert is_returnable_missed_call(_record(outcome, from_me)) is False

    def test_a_group_call_is_not(self):
        assert is_returnable_missed_call(_record(MISSED, group_call=True)) is False
        assert is_returnable_missed_call(_record(MISSED, jid="1@g.us")) is False

    def test_the_open_chat_decides_over_the_key(self):
        assert is_returnable_missed_call(_record(MISSED), "1@g.us") is False

    def test_a_legacy_record_is_not(self):
        assert is_returnable_missed_call(_legacy()) is False


class TestSupersedes:
    def test_the_final_outcome_replaces_ongoing(self):
        assert call_log_supersedes(_record(ONGOING), _record(COMPLETED, duration=30)) is True

    def test_real_data_replaces_a_legacy_record(self):
        assert call_log_supersedes(_legacy(), _record(MISSED)) is True

    def test_an_identical_copy_does_not(self):
        assert call_log_supersedes(_record(MISSED), _record(MISSED)) is False

    def test_a_settled_outcome_never_steps_back_to_pending(self):
        assert call_log_supersedes(_record(COMPLETED, duration=3), _record(ONGOING)) is False

    def test_missed_becomes_answered_elsewhere(self):
        """WhatsApp's own late correction (WAWebVoipPendingCallLogOutcome)."""
        assert call_log_supersedes(_record(MISSED), _record(ACCEPTED_ELSEWHERE)) is True

    def test_only_between_call_records(self):
        assert call_log_supersedes({"messageType": "conversation"}, _record()) is False
        assert call_log_supersedes(_record(), _legacy()) is False


class TestPending:
    def test_ongoing_and_unknown_are_pending(self):
        assert is_call_log_pending(_record(ONGOING)) is True
        assert is_call_log_pending(_record(UNKNOWN)) is True

    def test_a_settled_record_or_a_legacy_one_is_not(self):
        assert is_call_log_pending(_record(MISSED)) is False
        assert is_call_log_pending(_legacy()) is False


class TestCandidateIds:
    def test_every_known_form_of_the_peer_in_whatsapp_spelling(self):
        ids = call_log_candidate_ids("ABC", False, ["9@lid", "5511@s.whatsapp.net"])
        assert ids == ["false_9@lid_ABC", "false_5511@c.us_ABC"]

    def test_outgoing_calls_are_from_me(self):
        assert call_log_candidate_ids("ABC", True, ["9@lid"]) == ["true_9@lid_ABC"]

    def test_duplicates_and_blanks_are_dropped(self):
        assert call_log_candidate_ids("ABC", True, ["9@lid", "", "9@lid"]) == ["true_9@lid_ABC"]

    def test_no_call_id_no_candidates(self):
        assert call_log_candidate_ids("", True, ["9@lid"]) == []


class TestRefreshDelays:
    def test_soon_after_the_call_then_every_five_minutes(self):
        assert list(call_log_refresh_delays(1000)) == [3, 10, 30, 90, 300, 300]

    def test_bounded_by_the_window(self):
        assert sum(call_log_refresh_delays(600)) <= 600
        assert list(call_log_refresh_delays(2)) == []


def test_is_call_log():
    assert is_call_log(_record()) and is_call_log(_legacy())
    assert not is_call_log({"messageType": "conversation"})
    assert not is_call_log(None)


class TestRefile:
    def test_a_record_named_by_the_lid_moves_to_the_phone_chat(self):
        from core.call_log import refile_call_log
        msg = {"key": {"remoteJid": "9@lid", "id": "X"}}
        refile_call_log(msg, "5511@s.whatsapp.net")
        assert msg["key"] == {"remoteJid": "5511@s.whatsapp.net", "remoteJidAlt": "9@lid",
                              "id": "X"}

    def test_two_spellings_of_one_phone_are_not_a_mapping(self):
        from core.call_log import refile_call_log
        msg = {"key": {"remoteJid": "5511@s.whatsapp.net", "id": "X"}}
        refile_call_log(msg, "5511@s.whatsapp.net")
        assert "remoteJidAlt" not in msg["key"]

    def test_no_chat_leaves_it_alone(self):
        from core.call_log import refile_call_log
        msg = {"key": {"remoteJid": "9@lid", "id": "X"}}
        refile_call_log(msg, "")
        assert msg["key"] == {"remoteJid": "9@lid", "id": "X"}
