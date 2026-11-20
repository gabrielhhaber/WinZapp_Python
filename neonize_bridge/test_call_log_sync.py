from types import SimpleNamespace as N

import call_log_sync as cls


def placed(**over):
    info = {"id": "C1", "peer": "5511@s.whatsapp.net", "outgoing": True, "video": False,
            "group": "", "state": "ENDED", "activeAt": 778.0, "endedAt": 1000.0,
            "placedAt": 770.4}
    info.update(over)
    return info


def test_a_completed_call_placed_here_is_the_record_whatsapp_web_writes():
    # the record WhatsApp Web wrote for a 222 s call it placed (CDP, 2026-11-17)
    assert cls.record_fields(placed(), "Completed", 222, "7@lid") == {
        "callResult": "CONNECTED", "duration": 222, "startTime": 770,
        "isIncoming": True, "isVideo": False, "isCallLink": False,
        "callID": "C1", "callCreatorJID": "7@lid", "callType": "REGULAR"}
    assert cls.index("7@lid", "C1") == ["call_log", "7@lid", "C1", "1"]


def test_each_outcome_of_a_call_placed_here_has_its_result():
    results = {o: cls.record_fields(placed(), o, 0, "7@lid")["callResult"]
               for o in ("Completed", "Missed", "Rejected", "Canceled")}
    assert results == {"Completed": "CONNECTED", "Missed": "MISSED",
                       "Rejected": "REJECTED", "Canceled": "CANCELLED"}
    assert cls.record_fields(placed(), "Declined", 0, "7@lid") is None


def test_the_start_falls_back_to_the_end_less_the_duration():
    assert cls.record_fields(placed(placedAt=None), "Completed", 30, "7@lid")["startTime"] == 970


def test_the_phone_gets_no_record_of_what_was_not_a_one_to_one_call_placed_here():
    assert cls.record_fields(placed(outgoing=False), "Completed", 5, "7@lid") is None
    assert cls.record_fields(placed(group="1203@g.us"), "Completed", 5, "7@lid") is None
    assert cls.record_fields(placed(id=""), "Completed", 5, "7@lid") is None
    assert cls.record_fields(placed(), "Completed", 5, "") is None


def test_the_record_names_the_account_not_this_device():
    assert cls.own_user("7:27@lid") == "7@lid"
    assert cls.own_user("7@lid") == "7@lid"
    assert cls.own_user("5599@s.whatsapp.net") == ""
    assert cls.own_user("") == ""


def test_publish_sends_the_call_log_mutation_to_the_regular_collection():
    from neonize.proto.Neonize_pb2 import PatchInfo
    from neonize.proto.waSyncAction.WAWebProtobufSyncAction_pb2 import CallLogRecord
    sent = []
    s = N(lid="7:27@lid", send_mutation=lambda *a: sent.append(a) or True)
    assert cls.publish(s, placed(video=True), "Completed", 222)
    index, version, kind, value = sent[0]
    assert (index, version, kind) == (["call_log", "7@lid", "C1", "1"], 1, PatchInfo.REGULAR)
    record = value.callLogAction.callLogRecord
    assert record.callResult == CallLogRecord.CallResult.CONNECTED
    assert record.callType == CallLogRecord.CallType.REGULAR
    assert (record.duration, record.startTime, record.callID, record.callCreatorJID) == (
        222, 770, "C1", "7@lid")
    assert record.isIncoming and record.isVideo and not record.isCallLink
    assert not record.participants and not record.groupJID


def test_publish_never_raises_and_sends_nothing_without_a_record():
    def refuse(*a):
        raise RuntimeError("409")
    assert not cls.publish(N(lid="7@lid", send_mutation=refuse), placed(), "Completed", 5)
    assert not cls.publish(N(lid="7@lid", send_mutation=lambda *a: True),
                           placed(outgoing=False), "Completed", 5)
    assert not cls.publish(N(lid="", send_mutation=lambda *a: True), placed(), "Completed", 5)
