"""The call_log record a call placed here leaves on the account's other devices.

A call reaches only the devices of the person called: the account's own phone
learns of a call placed from a linked device when that device, at the end of
the call, writes a `call_log` record to the `regular` app-state collection.
Without it the call works and the phone's call history never shows it (user
report, 2026-11-16, iPhone).

What WhatsApp Web 2.3000.1052669929 writes (captured over CDP on 2026-11-17,
WAWebCallLogSync.getCallLogMutation, called from the call-ending handler when
`fromMe` and not a call link):

    collection "regular", version 1, operation SET
    index ["call_log", <own LID, no device>, <call id>, "1"]
    callLogAction.callLogRecord {callResult, duration (s connected),
        startTime (unix s, the offer), isIncoming (= fromMe, true), isVideo,
        isCallLink false, callID, callCreatorJID (own LID), participants []
        (one-to-one), callType REGULAR}

The phone reads the direction from callCreatorJID; isIncoming is copied as
WhatsApp Web fills it. Only one-to-one calls: a group call's record carries the
group and every participant's outcome, never captured yet.
"""
import logging

log = logging.getLogger("bridge")

# finished_outcome's outcomes -> CallLogRecord.CallResult names
RESULTS = {"Completed": "CONNECTED", "Missed": "MISSED",
           "Rejected": "REJECTED", "Canceled": "CANCELLED"}
VERSION = 1


def own_user(lid) -> str:
    """'user[:device]@lid' -> 'user@lid': the index and the creator name the
    account, not this device."""
    user, at, server = str(lid or "").partition("@")
    if not at or server != "lid" or not user:
        return ""
    return user.split(":")[0] + "@lid"


def record_fields(info, outcome, duration, creator):
    """The CallLogRecord fields of a call that ended here (call_media's
    finished_info, calls.finished_outcome's outcome and duration), or None when
    the phone gets no record: a call received here, a group call, no own LID.
    A one-to-one call that grew into a group call here still goes as the
    one-to-one call it was placed as (with whom it was placed): the phone then
    lists it under that person instead of not at all."""
    if not info.get("outgoing") or info.get("group") or not info.get("id") or not creator:
        return None
    result = RESULTS.get(outcome)
    if result is None:
        return None
    duration = max(0, int(duration or 0))
    ended = float(info.get("endedAt") or 0)
    started = info.get("placedAt") or (ended - duration)
    return {"callResult": result, "duration": duration, "startTime": int(started),
            "isIncoming": True, "isVideo": bool(info.get("video")), "isCallLink": False,
            "callID": str(info["id"]), "callCreatorJID": creator, "callType": "REGULAR"}


def index(creator, call_id) -> list:
    return ["call_log", creator, str(call_id), "1"]


def publish(s, info, outcome, duration) -> bool:
    """Writes the record of a call placed here for the account's other
    devices. True if WhatsApp accepted it; never raises (the local record of
    the call does not depend on it)."""
    creator = own_user(getattr(s, "lid", ""))
    fields = record_fields(info, outcome, duration, creator)
    if fields is None:
        return False
    try:
        from neonize.proto.Neonize_pb2 import PatchInfo
        from neonize.proto.waSyncAction.WAWebProtobufSyncAction_pb2 import (
            CallLogAction, CallLogRecord, SyncActionValue)
        record = CallLogRecord(**{
            **fields,
            "callResult": CallLogRecord.CallResult.Value(fields["callResult"]),
            "callType": CallLogRecord.CallType.Value(fields["callType"])})
        value = SyncActionValue(callLogAction=CallLogAction(callLogRecord=record))
        accepted = s.send_mutation(index(creator, fields["callID"]), VERSION,
                                   PatchInfo.REGULAR, value)
    except Exception:
        log.exception("call history record not sent")
        return False
    log.info("call history record %s (%s, %ss)",
             "sent" if accepted else "not accepted", fields["callResult"], fields["duration"])
    return bool(accepted)
