"""Merged group activation must publish the owner used by bounded ingress."""
import base64
import threading

from test_call_media import GroupRoster, group_member, media


def test_group_roster_activation_opens_only_its_live_media_ingress():
    fake = GroupRoster([group_member("111@lid")])
    call, _sent = media(fake)
    call._start_audio = lambda _id, _claim: None
    call.group_calls = True
    call.own_users = lambda: {"111"}
    written = threading.Event()
    original = fake.write_audio
    def write(*args):
        result = original(*args)
        written.set()
        return result
    fake.write_audio = write
    try:
        assert call.group_dial("1203@g.us") == "GP1"
        record = call._calls["GP1"]
        payload = {"pcm": base64.b64encode(b"\x01\x00" * 2880).decode(), "sampleRate": 48000,
                   "mediaProtocol": 1, "mediaEpoch": record["media_epoch"], "streamEpoch": 1}
        assert not call.enqueue_media("mic", payload)
        fake.people = [group_member("111@lid"), group_member("222@lid")]
        call.handle({"ev": "group", "call": "GP1", "participants": 2})
        assert call._ingress_owner is record
        assert call.enqueue_media("mic", payload)
        assert written.wait(1), "group activation did not open the media ingress"
        assert len(fake.written) == 1 and fake.written[0][0] == "GP1"
        call.handle({"ev": "end", "call": "GP1", "reason": "hangup"})
        assert call._ingress_owner is None
        assert not call.enqueue_media("mic", payload)
        assert call.placed_group("GP1")["groupJid"] == "1203@g.us"
    finally:
        call.close()
        call._ingress.join(1)
