"""Calls: offer/end events in the client's format and the refusal."""
import asyncio
from types import SimpleNamespace as N

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import calls


def jid(user, server="s.whatsapp.net"):
    return N(User=user, Server=server)


def node(tag="", attrs=None, children=()):
    return N(Tag=tag, Attrs=[N(name=k, text=v) for k, v in (attrs or {}).items()],
             Nodes=list(children))


def meta(call_id="C1", creator=None, alternative=None, ts=1700000000):
    return N(callID=call_id, timestamp=ts, callCreator=creator or jid("5511999"),
             callCreatorAlt=alternative or jid(""))


def offer(data=None, **kw):
    return N(basicCallMeta=meta(**kw), data=data or node("offer", children=[node("audio")]))


def test_attribute_and_tag_search_the_descendant_nodes():
    root = node("call", {"t": "1"}, [node("offer", {"group-jid": "123@g.us"}, [node("video")])])
    assert calls.attribute(root, "group-jid") == "123@g.us"
    assert calls.attribute(root, "nothing") == ""
    assert calls.has_tag(root, "video")
    assert not calls.has_tag(node("offer", children=[node("audio")]), "video")


def test_voice_offer_becomes_the_event_the_client_reads():
    data = calls.offer_event(offer(), 1700000005.7)
    assert data == {
        "event": "offer", "state": "INCOMING_RING", "peerJid": "5511999@c.us",
        "id": "C1", "isVideo": False, "isGroup": False, "groupJid": "", "isMultiparty": False,
        "timestamp": 1700000000, "observedAt": 1700000005,
        "offerReceivedWhileOffline": False}


def test_group_video_offer_from_the_queue():
    offer_node = node("offer", {"group-jid": "123-456@g.us", "offline": "1"}, [node("video")])
    data = calls.offer_event(offer(offer_node), 1700000005)
    assert data["isVideo"] and data["isGroup"]
    assert data["groupJid"] == "123-456@g.us"
    assert data["offerReceivedWhileOffline"]


def test_caller_prefers_the_number_over_the_lid():
    lid = jid("999", "lid")
    phone = jid("5511999")
    assert calls.caller(meta(creator=lid, alternative=phone)) == "5511999@c.us"
    assert calls.caller(meta(creator=lid)) == "999@lid"
    assert calls.caller(meta(creator=phone, alternative=jid("888", "lid"))) == "5511999@c.us"


def test_the_reason_for_the_end_becomes_event_and_state():
    assert calls.end_state("timeout") == ("timeout", "NOT_ANSWERED")
    assert calls.end_state("Rejected") == ("ended", "REJECTED")
    assert calls.end_state("accepted_elsewhere") == ("ended", "HANDLED_REMOTELY")
    assert calls.end_state("") == ("ended", "ENDED")


def test_end_carries_the_call_id_and_a_state_outside_the_ring():
    end = N(basicCallMeta=meta("C9"), reason="timeout", data=node("terminate"))
    data = calls.end_event(end, 1700000010)
    assert data["id"] == "C9" and data["state"] == "NOT_ANSWERED"
    assert data["state"] not in {"", "3", "8", "OFFER", "INCOMING_RING", "RINGING"}


def test_pending_refuses_the_requested_one_or_all():
    p = calls.Pending()
    p.remember("A", "creator-a", 100)
    p.remember("B", "creator-b", 101)
    assert p.to_refuse({"callId": "B"}) == ["B"]
    assert p.to_refuse({"callId": "Z"}) == []
    assert p.to_refuse({}) == ["A", "B"]
    assert p.to_refuse(None) == ["A", "B"]
    assert p.creator_of("A") == "creator-a"
    p.forget("A")
    assert p.creator_of("A") is None and p.to_refuse({}) == ["B"]


def test_pending_forgets_an_old_ring():
    p = calls.Pending()
    p.remember("old", "x", 0)
    p.remember("new", "y", calls.RING_TTL + 1)
    assert p.to_refuse({}) == ["new"]


def test_refusal_response_has_what_the_client_reads():
    response = calls.refusal_response(["A"])
    assert response["via"] == "neonize" and response["call"]["state"] == "REJECTED"


# ----------------------------------------------------------- the routes, without neonize
class FakeClient:
    def __init__(self, failure=None):
        self.rejected = []
        self.failure = failure

    def reject_call(self, source, call_id):
        if self.failure:
            raise self.failure
        self.rejected.append((source, call_id))


def run(s, requests):
    """Starts an aiohttp server with only the call routes and makes the requests."""
    async def main():
        app = web.Application()
        calls.register_routes(
            app.router, session=lambda req: s,
            ok=lambda **kw: web.json_response(kw))
        async with TestClient(TestServer(app)) as http:
            outputs = []
            for method, path, body in requests:
                response = await http.request(method, "/api/tok" + path, json=body)
                outputs.append((response.status, await response.json()))
            return outputs
    return asyncio.run(main())


def session_with_ring(failure=None):
    s = N(client=FakeClient(failure), status="CONNECTED",
          ringing_calls=calls.Pending())
    s.ringing_calls.remember("C1", "creator-1", 100)
    return s


def test_reject_calls_reject_call_with_the_creator_of_the_offer():
    s = session_with_ring()
    (status, body), = run(s, [("POST", "/call/reject", {"callId": "C1"})])
    assert status == 200 and body["status"] == "success"
    assert body["response"]["call"]["state"] == "REJECTED"
    assert s.client.rejected == [("creator-1", "C1")]
    assert s.ringing_calls.creator_of("C1") is None


def test_reject_with_an_empty_body_refuses_the_ringing_one_and_reject_call_is_the_same():
    s = session_with_ring()
    (status, _), = run(s, [("POST", "/reject-call", {})])
    assert status == 200 and s.client.rejected == [("creator-1", "C1")]


def test_rejecting_an_unknown_call_is_404_and_disconnected_is_400():
    s = session_with_ring()
    (status, body), = run(s, [("POST", "/call/reject", {"callId": "no"})])
    assert status == 404 and body["status"] == "error" and not s.client.rejected
    s.status = "CLOSED"
    (status, _), = run(s, [("POST", "/call/reject", {})])
    assert status == 400


def test_whatsmeow_error_on_refusal_is_500_and_the_call_stays_pending():
    s = session_with_ring(failure=RuntimeError("boom"))
    (status, body), = run(s, [("POST", "/call/reject", {"callId": "C1"})])
    assert status == 500 and body["status"] == "error"
    assert s.ringing_calls.creator_of("C1") == "creator-1"


def test_impossible_routes_answer_501_in_the_error_format():
    # a session without the call layer (an older neonize build): every call
    # route but reject and diagnostics answers 501
    s = session_with_ring()
    routes = calls.MEDIA_ROUTES
    outputs = run(s, [("POST", "/" + route, {}) for route in routes])
    assert len(outputs) == 8
    for status, body in outputs:
        assert status == 501 and body["status"] == "error" and body["message"]
    # a call that is still ringing is not touched by them
    assert s.ringing_calls.creator_of("C1") == "creator-1"
    # the text is not confused with a failure to initialize Chrome's VoIP
    for reason in (calls.NO_MEDIA_REASON, calls.NO_VIDEO_REASON, calls.NO_GROUP_REASON):
        assert "voip" not in reason.lower()


# ---------------------------------------- call answered on another device
def end_of(reason="", ts=1700000060, call_id="C1"):
    return N(basicCallMeta=meta(call_id, ts=ts), reason=reason, data=node("terminate"))


def test_end_without_a_reason_after_an_accept_is_answered_on_another_device():
    p = calls.Pending()
    p.remember("C1", "creator-1", 1700000000)
    p.accept("C1", 1700000010, 1700000011)          # the phone answered
    data = calls.refine_end(calls.end_event(end_of(), 1700000061), p.accepted("C1"))
    assert data["state"] == "HANDLED_REMOTELY"
    assert data["callDuration"] == 50               # 1700000060 - 1700000010


def test_end_without_a_reason_and_without_an_accept_is_still_just_an_end():
    data = calls.end_event(end_of(), 1700000061)
    assert calls.refine_end(data, None) is data
    assert data["state"] == "ENDED"


def test_the_reason_for_the_end_outweighs_the_accept():
    for reason, state in (("timeout", "NOT_ANSWERED"), ("reject", "REJECTED")):
        data = calls.end_event(end_of(reason), 1700000061)
        assert calls.refine_end(data, (1700000010, 1700000011)) is data
        assert data["state"] == state


def test_duration_uses_the_servers_clock_and_is_never_negative():
    assert calls.call_duration((100, 5000), 130, 9999) == 30
    assert calls.call_duration((0, 5000), 130, 5042) == 42      # without the server's time
    assert calls.call_duration((100, 5000), 90, 9999) == 0
    assert calls.call_duration((0, 5000), 0, 4000) == 0


def test_pending_forgets_the_accept_along_with_the_ring_and_the_old_accept():
    p = calls.Pending()
    p.accept("A", 1, 100)
    assert p.accepted("A") == (1, 100)
    p.forget("A")
    assert p.accepted("A") is None
    p.accept("old", 1, 0)
    p.accept("new", 2, calls.RING_TTL + 1)
    assert p.accepted("old") is None and p.accepted("new") is not None


def test_diagnostics_say_what_the_call_layer_is():
    s = session_with_ring()
    (status, body), = run(s, [("GET", "/call/diagnostics", None)])
    assert status == 200
    assert body["response"] == {"engine": "neonize", "callLayer": False, "connected": True,
                                "voice": False, "video": False, "groupCalls": False,
                                "liveCall": False, "ringing": 1}


def test_diagnostics_with_a_live_call():
    media = N(live_call=lambda: "CALL1")
    assert calls.diagnostics(media, True, 0)["liveCall"] is True
    assert calls.diagnostics(media, True, 0)["voice"] is True


def test_ring_left_counts_from_the_offer_s_own_time():
    assert calls.ring_left(1000, 1000) == calls.RING_LIMIT
    assert calls.ring_left(1000, 1100) == calls.RING_LIMIT - 100
    assert calls.ring_left(1000, 1000 + 9 * 60) == 0.0       # the 9-minute ghost
    assert calls.ring_left(0, 500) == calls.RING_LIMIT          # no offer time
    assert calls.ring_left(2000, 1000) == calls.RING_LIMIT      # a clock ahead of ours


class Media:
    def __init__(self, state):
        self.state, self.hung = state, []

    def status(self, call_id):
        return {"id": call_id, "state": self.state}

    def hangup(self, call_id):
        self.hung.append(call_id)
        return True


def test_only_a_call_still_ringing_unanswered_is_ended():
    ringing = Media("INCOMING_RING")
    assert calls.drop_if_unanswered(ringing, "C1") and ringing.hung == ["C1"]
    for state in ("ACTIVE", "CONNECTING", "ENDED"):
        media = Media(state)
        assert not calls.drop_if_unanswered(media, "C1") and media.hung == []
    assert not calls.drop_if_unanswered(None, "C1")


def ended(state="ENDED", video=False, observed=1000):
    return {"state": state, "isVideo": video, "observedAt": observed, "id": "C1",
            "peerJid": "5511@c.us", "groupJid": "", "timestamp": observed}


def test_a_call_answered_here_is_completed_not_canceled():
    answered = {"at": 640.0, "video": True}
    assert calls.answered_outcome(ended(), answered) == ("Completed", 360, True)
    assert calls.answered_outcome(ended(video=True), {"at": 990.0, "video": False}) == (
        "Completed", 10, True)
    assert calls.answered_outcome(ended(), None) is None
    # a call rejected or missed was not answered here, whatever is remembered
    assert calls.answered_outcome(ended("REJECTED"), answered) is None
    assert calls.answered_outcome(ended("NOT_ANSWERED"), answered) is None


def test_record_end_writes_the_answered_call_as_completed(monkeypatch):
    stored = []
    monkeypatch.setattr(calls, "call_chat", lambda db, meta, group: "5511@s.whatsapp.net")
    monkeypatch.setattr(calls.system_messages, "store_and_emit",
                        lambda s, chat, w, now: stored.append(w) or True)
    s = N(me="5599@s.whatsapp.net", lid="", db=None)
    e = N(basicCallMeta=None)
    assert calls.record_end(s, e, ended(), True, 1000, {"at": 640.0, "video": True})
    w = stored[0]
    assert (w["callOutcome"], w["callDuration"], w["isVideoCall"]) == ("Completed", 360, True)
    # without the answer it stays what it was: the caller gave up
    assert calls.record_end(s, e, ended(), True, 1000)
    assert stored[1]["callOutcome"] == "Canceled"


def test_a_group_call_without_a_group_is_still_multiparty():
    offer_node = node("offer", {}, [node("group_info", {}, [node("user")])])
    data = calls.offer_event(offer(offer_node), 1700000005)
    assert data["isMultiparty"] and not data["isGroup"]


def group_offer_data():
    offer_node = node("offer", {"group-jid": "1203@g.us"}, [node("video"), node("group_info")])
    return calls.offer_event(offer(offer_node), 1700000005)


def test_the_end_of_a_group_call_keeps_the_group_of_its_offer():
    ended = calls.end_event(end_of(), 1700000100)
    assert ended["groupJid"] == "" and not ended["isVideo"]     # what <terminate> says
    fixed = calls.with_offer(ended, {"groupJid": "1203@g.us", "isVideo": True})
    assert fixed["groupJid"] == "1203@g.us" and fixed["isGroup"] and fixed["isVideo"]
    assert calls.with_offer(ended, None) is ended
    private = calls.with_offer(ended, {"groupJid": "", "isVideo": False})
    assert private["groupJid"] == "" and not private["isGroup"]


def test_the_offer_outlives_the_ring_for_a_long_group_call():
    p = calls.Pending()
    p.remember("C1", "creator-1", 1700000000, group_offer_data())
    p.remember("C2", "creator-2", 1700000000 + calls.RING_TTL + 1)    # sweeps old rings
    assert p.creator_of("C1") is None
    assert p.offer_of("C1") == {"groupJid": "1203@g.us", "isVideo": True}
    p.forget("C1")
    assert p.offer_of("C1") is None
    p.remember("C3", "creator-3", 1700000000)
    assert p.offer_of("C3") is None                                    # no offer given


def test_a_missed_group_call_is_recorded_in_the_group_not_with_the_caller(monkeypatch):
    stored = []
    monkeypatch.setattr(calls.system_messages, "store_and_emit",
                        lambda s, chat, w, now: stored.append((chat, w)) or True)
    db = N(chat_exists=lambda jid: True)
    s = N(me="5599@s.whatsapp.net", lid="", db=db)
    p = calls.Pending()
    p.remember("C1", "creator-1", 1700000000, group_offer_data())
    e = end_of(reason="timeout")
    data = calls.with_offer(calls.end_event(e, 1700000060), p.offer_of("C1"))
    assert calls.record_end(s, e, data, True, 1700000060)
    chat, w = stored[0]
    assert chat == "1203@g.us"
    assert w["callOutcome"] == "Missed" and w["isVideoCall"] is True


class FakeMedia:
    def __init__(self, failure=None):
        self.hung_up = []
        self.failure = failure

    def hangup(self, call_id=""):
        if self.failure:
            raise self.failure
        self.hung_up.append(call_id)
        return True


def test_ending_a_call_works_while_the_socket_is_down():
    # a network drop in the middle of a call used to answer 400 "Disconnected"
    # to call/end and call/ensure-ended, and the call could not be ended
    s = N(client=None, status="CLOSED", ringing_calls=calls.Pending(), call_media=FakeMedia())
    outputs = run(s, [("POST", "/call/end", {"callId": "C1"}),
                      ("POST", "/call/ensure-ended", {"callId": "C1"})])
    assert [status for status, _ in outputs] == [200, 200]
    assert s.call_media.hung_up == ["C1", "C1"]
    assert outputs[0][1]["response"]["ended"] is True


def test_a_local_hangup_failure_while_disconnected_is_not_an_error():
    s = N(client=None, status="CLOSED", ringing_calls=calls.Pending(),
          call_media=FakeMedia(RuntimeError("no socket")))
    (status, body), = run(s, [("POST", "/call/end", {"callId": "C1"})])
    assert status == 200 and body["response"]["ended"] is False


def test_the_other_call_routes_still_need_the_connection():
    s = N(client=None, status="CLOSED", ringing_calls=calls.Pending(), call_media=FakeMedia())
    (status, _), = run(s, [("POST", "/call/accept", {"callId": "C1"})])
    assert status == 400


def test_the_end_of_a_group_call_placed_here_keeps_the_group_and_is_ours():
    data = {**ended(), "peerJid": "75db@lid", "isGroup": False, "outgoing": False}
    done = calls.with_placed_group(data, {"groupJid": "1203@g.us", "isVideo": True})
    assert (done["peerJid"], done["groupJid"], done["isGroup"]) == ("1203@g.us", "1203@g.us", True)
    assert done["outgoing"] is True and done["isVideo"] is True
    # not a group call placed here: the end stays what the native layer said
    assert calls.with_placed_group(data, None) is data
    assert calls.with_placed_group(data, {"groupJid": ""}) is data


def test_record_end_does_not_file_the_call_this_client_placed(monkeypatch):
    stored = []
    monkeypatch.setattr(calls, "call_chat", lambda db, meta, group: "75db@lid")
    monkeypatch.setattr(calls.system_messages, "store_and_emit",
                        lambda s, chat, w, now: stored.append(w) or True)
    s = N(me="5599@s.whatsapp.net", lid="", db=None)
    mine = calls.with_placed_group(ended(), {"groupJid": "1203@g.us"})
    assert not calls.record_end(s, N(basicCallMeta=None), mine, False, 1000)
    assert stored == []


def test_the_lobby_names_the_group_of_an_invitation_that_came_without_one():
    assert calls.lobby_group({"groupJid": "1203@g.us"}) == "1203@g.us"
    assert calls.lobby_group({"groupJid": ""}) == ""
    assert calls.lobby_group({"groupJid": "5511@s.whatsapp.net"}) == ""
    assert calls.lobby_group(None) == ""


def test_the_group_found_by_the_lobby_reaches_every_place_that_remembers_the_call():
    pending = calls.Pending()
    pending.remember("C1", "100:2@lid", 1000, {"groupJid": "", "isVideo": False})
    seen = []

    class Media:
        ongoing_calls = None

        def lobby(self, call_id, creator):
            seen.append((call_id, creator))
            return {"groupJid": "1203@g.us"}

        def set_call_group(self, call_id, group):
            seen.append(("group", call_id, group))

    media = Media()
    creator = N(User="100", Device=2, Server="lid")
    s = N(ringing_calls=pending)
    assert calls.resolve_multiparty_group(s, media, {"id": "C1", "isVideo": False}, creator) == "1203@g.us"
    assert seen == [("C1", "100:2@lid"), ("group", "C1", "1203@g.us")]
    assert pending.offer_of("C1")["groupJid"] == "1203@g.us"
    assert media.ongoing_calls.get("C1")["chat"] == "1203@g.us"


def test_a_lobby_that_fails_leaves_the_call_as_it_was():
    pending = calls.Pending()
    pending.remember("C1", "100@lid", 1000, {"groupJid": ""})

    class Media:
        def lobby(self, call_id, creator):
            raise OSError("down")

    s = N(ringing_calls=pending)
    assert calls.resolve_multiparty_group(
        s, Media(), {"id": "C1"}, N(User="100", Device=0, Server="lid")) == ""
    assert pending.offer_of("C1")["groupJid"] == ""


def test_a_lobby_answer_that_comes_after_the_call_ended_registers_nothing():
    pending = calls.Pending()                      # the end already forgot the call

    class Media:
        ongoing_calls = None

        def lobby(self, call_id, creator):
            return {"groupJid": "1203@g.us"}

        def set_call_group(self, call_id, group):
            raise AssertionError("a dead call must not be touched")

    media = Media()
    s = N(ringing_calls=pending)
    assert calls.resolve_multiparty_group(
        s, media, {"id": "C1"}, N(User="100", Device=0, Server="lid")) == ""
    assert media.ongoing_calls is None


def finished(**over):
    info = {"id": "C1", "peer": "5511@s.whatsapp.net", "outgoing": False, "video": False,
            "group": "", "state": "ENDED", "activeAt": None, "endedAt": 1000.0}
    info.update(over)
    return info


def test_a_call_that_became_active_is_completed_with_its_duration():
    assert calls.finished_outcome(finished(activeAt=640.0)) == ("Completed", 360)
    assert calls.finished_outcome(finished(activeAt=640.0, outgoing=True)) == ("Completed", 360)


def test_a_call_placed_here_and_not_answered_says_how_it_ended():
    placed = {"outgoing": True}
    assert calls.finished_outcome(finished(state="NOT_ANSWERED", **placed)) == ("Missed", 0)
    assert calls.finished_outcome(finished(state="REJECTED", **placed)) == ("Rejected", 0)
    assert calls.finished_outcome(finished(state="ENDED", **placed)) == ("Canceled", 0)
    assert calls.finished_outcome(finished(state="FAILED", **placed)) is None


def test_a_received_call_never_answered_here_is_left_to_record_end():
    assert calls.finished_outcome(finished(state="ENDED")) is None
    assert calls.finished_outcome(finished(state="NOT_ANSWERED")) is None


def test_the_peer_chat_is_the_one_that_exists_and_the_number_first():
    class Db:
        def __init__(self, *chats):
            self.chats = chats

        def chat_exists(self, jid):
            return jid in self.chats

    phone = lambda lid: "5511@s.whatsapp.net"
    assert calls.peer_chat(Db("75db@lid"), "75db@lid", phone) == "75db@lid"
    assert calls.peer_chat(Db(), "75db@lid", phone) == "5511@s.whatsapp.net"
    assert calls.peer_chat(Db(), "5511@s.whatsapp.net", None) == "5511@s.whatsapp.net"


def test_record_finished_files_the_call_placed_here_as_mine(monkeypatch):
    stored = []
    monkeypatch.setattr(calls.system_messages, "store_and_emit",
                        lambda s, chat, w, now: stored.append((chat, w)) or True)
    s = N(me="5599@s.whatsapp.net", lid="", db=N(chat_exists=lambda jid: True))
    assert calls.record_finished(s, finished(outgoing=True, activeAt=900.0, video=True), 1000)
    chat, w = stored[0]
    assert chat == "5511@s.whatsapp.net" and w["fromMe"] and w["id"]["fromMe"]
    assert w["id"]["_serialized"].startswith("true_")
    assert (w["callOutcome"], w["callDuration"], w["isVideoCall"]) == ("Completed", 100, True)


def test_record_finished_files_a_group_call_in_the_group(monkeypatch):
    stored = []
    monkeypatch.setattr(calls.system_messages, "store_and_emit",
                        lambda s, chat, w, now: stored.append((chat, w)) or True)
    s = N(me="5599@s.whatsapp.net", lid="", db=N(chat_exists=lambda jid: False))
    info = finished(outgoing=True, activeAt=900.0, group="1203@g.us", peer="1203@g.us")
    assert calls.record_finished(s, info, 1000)
    assert stored[0][0] == "1203@g.us"


def test_record_finished_ignores_what_has_no_record():
    s = N(me="5599@s.whatsapp.net", lid="", db=None)
    assert not calls.record_finished(s, finished(state="ENDED"), 1000)
    assert not calls.record_finished(s, finished(id="", activeAt=1.0), 1000)


def test_record_finished_does_not_name_the_group_as_the_caller(monkeypatch):
    stored = []
    monkeypatch.setattr(calls.system_messages, "store_and_emit",
                        lambda s, chat, w, now: stored.append(w) or True)
    s = N(me="5599@s.whatsapp.net", lid="9@lid", db=N(chat_exists=lambda jid: False))
    info = finished(activeAt=900.0, group="1203@g.us", peer="1203@g.us")
    assert calls.record_finished(s, info, 1000)
    assert stored[0]["callCreator"] == ""


def test_record_finished_leaves_out_a_call_of_this_account_seen_as_received():
    s = N(me="5599@s.whatsapp.net", lid="9@lid", db=N(chat_exists=lambda jid: True))
    assert not calls.record_finished(s, finished(activeAt=900.0, peer="5599@s.whatsapp.net"), 1000)


def test_peer_chat_drops_the_device_of_the_peer():
    db = N(chat_exists=lambda jid: False)
    assert calls.peer_chat(db, "5511:12@s.whatsapp.net", None) == "5511@s.whatsapp.net"


def test_the_end_of_a_call_placed_here_is_ours_and_names_who_was_called():
    """WhatsApp's <terminate> names the creator: for a call placed here, this
    account. Told as a received call, the client wrote it over the call's
    peer and the record landed in the user's own chat."""
    mine = {"5599@c.us", "81@lid"}
    data = {**ended(), "peerJid": "81@lid", "isGroup": False, "outgoing": False}
    done = calls.with_own_call(data, mine, "5511999990000@s.whatsapp.net")
    assert done["outgoing"] is True and done["peerJid"] == "5511999990000@c.us"
    # placed from another device of this account: no peer, never this account
    assert calls.with_own_call(data, mine, "")["peerJid"] == ""
    # someone else's call, a group's, one already ours: as it came
    other = {**data, "peerJid": "69@lid"}
    assert calls.with_own_call(other, mine, "x@c.us") is other
    group = {**data, "isGroup": True}
    assert calls.with_own_call(group, mine, "x@c.us") is group
