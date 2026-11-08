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


def test_an_update_this_account_posted_is_sent():
    mine = update(5, "M", 100, "meu post")
    mine.IsSender = True
    (w, _), (theirs, _) = cu.updates_to_wpp(CHANNEL, [mine, update(6, "T", 200, "outro")])
    assert w["fromMe"] is True and w["id"]["fromMe"] is True
    assert theirs["fromMe"] is False


def test_a_zero_date_is_no_date():
    (w, _), = cu.updates_to_wpp(CHANNEL, [update(1, "A", -62135596800, "x")])
    assert w["t"] == 0


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


def test_an_update_an_admin_posts_is_never_stored():
    assert not cu.kept_in_store("120363000000000001@newsletter")
    assert cu.kept_in_store("5511999998888@s.whatsapp.net") and cu.kept_in_store("1203@g.us")


class Server:
    """A channel whose updates are server ids 1..total, newest first."""

    def __init__(self, total):
        self.total, self.asked = total, []

    def get_newsletter_messages(self, jid, count, before):
        self.asked.append((count, before))
        top = (before - 1) if before else self.total
        return [update(n, f"M{n}", n * 10, f"t{n}") for n in range(top, max(0, top - count), -1)]


def test_a_large_count_is_asked_in_pages_of_whatsapps_ceiling():
    s = N(client=Server(1000))
    got = cu.read(s, CHANNEL, 250, None, lambda t: t)
    assert len(got) == 250 and got[0]["id"]["id"] == "M751" and got[-1]["id"]["id"] == "M1000"
    assert s.client.asked == [(100, 0), (100, 901), (50, 801)]


def test_a_sync_asking_for_everything_stays_bounded():
    s = N(client=Server(100000))
    assert len(cu.read(s, CHANNEL, 100000, None, lambda t: t)) == cu.PER_REQUEST * cu.MAX_REQUESTS


def test_the_channels_start_ends_the_pages():
    s = N(client=Server(130))
    assert len(cu.read(s, CHANNEL, 500, None, lambda t: t)) == 130
    assert s.client.asked == [(100, 0), (100, 31)]


def test_after_a_restart_the_anchor_is_found_by_the_server_id_the_client_sent():
    s = N(client=Server(300))      # a fresh bridge: nothing remembered
    assert cu.read(s, CHANNEL, 20, "M120", lambda t: t) == []
    older = cu.read(s, CHANNEL, 20, "M120", lambda t: t, "120")
    assert [w["id"]["id"] for w in older][:1] == ["M100"] and len(older) == 20
    assert s.client.asked == [(20, 120)]
    assert cu.read(s, CHANNEL, 20, "M120", lambda t: t, "junk") == []


def test_a_refused_later_page_keeps_what_came_before():
    class Flaky(Server):
        def get_newsletter_messages(self, jid, count, before):
            if before:
                raise RuntimeError("rate limited")
            return super().get_newsletter_messages(jid, count, before)
    s = N(client=Flaky(500))
    assert len(cu.read(s, CHANNEL, 300, None, lambda t: t)) == 100


def test_what_an_edit_needs_is_remembered_with_the_server_id():
    s = N(client=Server(5))
    cu.read(s, CHANNEL, 2, None, lambda t: t)
    details = cu.server_ids_of(s).details("M5")
    assert details == {"server_id": 5, "type": "chat", "t": 50, "fromMe": False}
    assert cu.server_ids_of(s).of("M5") == 5 and cu.server_ids_of(s).details("X") is None


def test_the_channel_edit_rules():
    import message_rules as mr
    mine = {"type": "chat", "t": 1000, "fromMe": True}
    assert mr.channel_edit_refusal(True, mine, 1000 + 60) == ""
    assert mr.channel_edit_refusal(True, mine, 1000 + mr.EDIT_WINDOW + 1) == "outside_window"
    assert mr.channel_edit_refusal(False, mine, 1000) == "not_mine"
    assert mr.channel_edit_refusal(True, dict(mine, fromMe=False), 1000) == "not_mine"
    assert mr.channel_edit_refusal(True, dict(mine, type="image"), 1000) == "not_stored"
    assert mr.channel_edit_refusal(True, None, 1000) == "not_stored"
