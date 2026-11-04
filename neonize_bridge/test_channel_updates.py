"""channel_updates.py: a channel's updates through get-messages, on Neonize."""
from types import SimpleNamespace as N

from neonize.proto.Neonize_pb2 import NewsletterMessage

import channel_updates as cu

CHANNEL = "120363000000000001@newsletter"


def update(server_id, mid, ts, text=None):
    u = NewsletterMessage(MessageServerID=server_id, ViewsCount=7, MessageID=mid, Timestamp=ts)
    if text is not None:
        u.Message.conversation = text
    return u


def test_updates_go_out_like_any_message_oldest_first():
    pairs = cu.updates_to_wpp(CHANNEL, [update(12, "B", 200, "nova"), update(11, "A", 100, "velha"),
                                        update(13, "C", 300)])
    assert [(w["id"]["id"], w["body"], w["t"], sid) for w, sid in pairs] == [
        ("A", "velha", 100, 11), ("B", "nova", 200, 12)]
    assert pairs[0][0]["viewCount"] == 7 and pairs[0][0]["fromMe"] is False


def test_an_older_build_uses_the_server_id_as_the_id():
    u = NewsletterMessage(MessageServerID=55, ViewsCount=0)
    u.Message.conversation = "x"
    assert cu.update_id(u) == "55"


class Client:
    def __init__(self):
        self.asked = []

    def get_newsletter_messages(self, jid, count, before):
        self.asked.append((jid, count, before))
        if before == 0:
            return [update(12, "B", 200, "b"), update(11, "A", 100, "a")]
        return [update(10, "Z", 50, "z")]


def test_paging_back_anchors_on_the_server_id_of_the_oldest():
    s = N(client=Client())
    first = cu.read(s, CHANNEL, 2, None, lambda t: t)
    assert [w["id"]["id"] for w in first] == ["A", "B"]
    older = cu.read(s, CHANNEL, 2, "A", lambda t: t)
    assert [w["id"]["id"] for w in older] == ["Z"]
    assert s.client.asked == [(CHANNEL, 2, 0), (CHANNEL, 2, 11)]
    assert cu.read(s, CHANNEL, 2, "UNKNOWN", lambda t: t) == []


def test_the_remembered_ids_are_bounded():
    ids = cu.ServerIds(limit=2)
    ids.remember([({"id": {"id": k}}, n) for n, k in enumerate("abc")])
    assert ids.of("a") is None and ids.of("c") == 2
