"""channel_updates.py: a channel's updates through get-messages, on Neonize."""
from types import SimpleNamespace as N

import pytest

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


class SilentAbove(Server):
    """WhatsApp's server: no answer above some hundreds (whatsmeow then
    raises GetNewsletterMessagesError after its IQ timeout)."""

    def get_newsletter_messages(self, jid, count, before):
        if count > 300:
            raise RuntimeError("info query timed out")
        return super().get_newsletter_messages(jid, count, before)


def test_the_clients_sync_of_1000_never_asks_more_than_whatsapp_answers():
    s = N(client=SilentAbove(2000))
    got = cu.read(s, CHANNEL, 1000, None, lambda t: t)
    assert len(got) == cu.PER_REQUEST * cu.MAX_REQUESTS
    assert max(count for count, _before in s.client.asked) <= 100


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


# ------------------------------------------------------------ the cache
from store import Store  # noqa: E402


class Counting(Server):
    """Server whose answers can be switched off (WhatsApp unreachable)."""

    def __init__(self, total):
        super().__init__(total)
        self.down = False

    def get_newsletter_messages(self, jid, count, before):
        if self.down:
            raise RuntimeError("timeout")
        return super().get_newsletter_messages(jid, count, before)


@pytest.fixture
def cached(tmp_path):
    db = Store(str(tmp_path / "bridge.db"))
    yield N(client=Counting(300), db=db)
    db.close()


def test_an_older_page_the_cache_holds_is_read_from_disk(cached):
    cu.read(cached, CHANNEL, 100, None, lambda t: t)            # 201..300, cached
    cu.read(cached, CHANNEL, 50, "M201", lambda t: t)            # 151..200, cached
    asked = list(cached.client.asked)
    again = cu.read(cached, CHANNEL, 50, "M201", lambda t: t)
    assert [w["id"]["id"] for w in again][:1] == ["M151"] and len(again) == 50
    assert cached.client.asked == asked                          # nothing asked of WhatsApp


def test_an_older_page_with_a_hole_in_the_cache_is_asked_of_whatsapp(cached):
    """Yesterday 281..300 were cached; today the channel has 500 more. The
    page before today's oldest (781) used to come from yesterday's rows, and
    301..780 were never shown."""
    cu.read(cached, CHANNEL, 20, None, lambda t: t)             # 281..300
    cached.client.total = 800
    cu.read(cached, CHANNEL, 20, None, lambda t: t)             # 781..800
    older = cu.read(cached, CHANNEL, 20, "M781", lambda t: t)
    assert [w["id"]["id"] for w in older][::19] == ["M761", "M780"]
    assert cached.client.asked[-1] == (20, 781)


def test_with_whatsapp_unreachable_the_cache_never_answers_past_a_hole(cached):
    cu.read(cached, CHANNEL, 20, None, lambda t: t)             # 281..300
    cached.client.total = 800
    cu.read(cached, CHANNEL, 20, None, lambda t: t)             # 781..800
    cached.client.down = True
    assert cu.read(cached, CHANNEL, 20, "M781", lambda t: t) == []
    newest = cu.read(cached, CHANNEL, 40, None, lambda t: t)
    assert [w["id"]["id"] for w in newest][::19] == ["M781", "M800"]


def test_the_cache_chain_breaks_where_an_answer_did_not_reach():
    rows = [({"id": {"id": f"M{n}"}}, n, upto) for n, upto in
            ((1, 3), (2, 3), (3, 5), (5, 9), (8, 9))]
    assert [sid for _w, sid in cu.contiguous(rows, 9)] == [1, 2, 3, 5, 8]
    assert [sid for _w, sid in cu.contiguous(rows, 10)] == []
    broken = rows[:2] + [({"id": {"id": "M3"}}, 3, 4)] + rows[3:]
    assert [sid for _w, sid in cu.contiguous(broken, 9)] == [5, 8]
    assert [sid for _w, sid in cu.contiguous(rows, 0)] == [1, 2, 3, 5, 8]


def test_the_newest_page_is_always_asked_of_whatsapp(cached):
    cu.read(cached, CHANNEL, 20, None, lambda t: t)
    cu.read(cached, CHANNEL, 20, None, lambda t: t)
    assert cached.client.asked == [(20, 0), (20, 0)]


def test_with_whatsapp_unreachable_the_cache_answers(cached):
    cu.read(cached, CHANNEL, 30, None, lambda t: t)
    cached.client.down = True
    newest = cu.read(cached, CHANNEL, 30, None, lambda t: t)
    assert [w["id"]["id"] for w in newest][-1] == "M300" and len(newest) == 30


def test_after_a_restart_the_cache_finds_the_anchor(cached):
    cu.read(cached, CHANNEL, 100, None, lambda t: t)
    cached._channel_server_ids = None                            # a new bridge process
    older = cu.read(cached, CHANNEL, 20, "M201", lambda t: t)
    assert [w["id"]["id"] for w in older][:1] == ["M181"] and len(older) == 20


def test_a_deleted_update_leaves_the_cache(cached):
    cu.read(cached, CHANNEL, 10, None, lambda t: t)
    assert cached.db.channel_post("M300") is not None
    gone = NewsletterMessage(MessageServerID=300, MessageID="M300")
    cached.db.channel_posts_forget(CHANNEL, cu.deleted_ids([gone, update(299, "M299", 1, "x")]))
    assert cached.db.channel_post("M300") is None and cached.db.channel_post("M299") is not None


def test_a_channel_never_becomes_a_conversation(cached):
    cu.read(cached, CHANNEL, 10, None, lambda t: t)
    assert cached.db.messages(CHANNEL, 10) == [] and cached.db.wpp_message("M300") is None


def test_the_caption_of_a_cached_media_update_is_edited(cached):
    class Media(Counting):
        def get_newsletter_messages(self, jid, count, before):
            photo = NewsletterMessage(MessageServerID=9, MessageID="PHOTO", Timestamp=1000,
                                      IsSender=True)
            photo.Message.imageMessage.caption = "antes"
            photo.Message.imageMessage.directPath = "/v/t62/x"
            return [photo]

        def edit_message(self, chat, mid, new):
            self.edited = (mid, new.imageMessage.caption, new.imageMessage.directPath)
    cached.client = Media(1)
    cu.read(cached, CHANNEL, 1, None, lambda t: t)
    import message_rules as mr
    details = cu.details_of(cached, "PHOTO")
    assert mr.channel_edit_refusal(True, details, 1060, media_kept=True) == ""
    assert mr.channel_edit_refusal(True, details, 1060, media_kept=False) == "not_stored"
    assert cu.edit(cached, CHANNEL, "PHOTO", "depois") is True
    assert cached.client.edited == ("PHOTO", "depois", "/v/t62/x")
    assert cached.db.channel_post("PHOTO")[1]["caption"] == "depois"


def test_a_post_just_sent_is_known_with_its_server_id(tmp_path):
    """Editing a post right after sending it was refused as not stored: the
    bridge only learnt its server id by reading the channel again."""
    from types import SimpleNamespace
    from store import Store
    import channel_updates as cu
    db = Store(str(tmp_path / "bridge.db"))
    try:
        s = SimpleNamespace(db=db)
        w = {"id": {"id": "3EB0X"}, "type": "chat", "t": 100, "fromMe": True, "body": "oi"}
        assert cu.remember_sent(s, "1@newsletter", w, 105) is True
        assert cu.details_of(s, "3EB0X")["server_id"] == 105
        # after a restart, from the cache
        assert cu.details_of(SimpleNamespace(db=db), "3EB0X")["server_id"] == 105
        # a photo's caption can be edited at once too: its media is cached
        photo = {"id": {"id": "3EB0P"}, "type": "image", "t": 100, "fromMe": True}
        assert cu.remember_sent(s, "1@newsletter", photo, 106, b"media") is True
        assert db.media_record("3EB0P") == b"media"
        assert cu.remember_sent(s, "5511@s.whatsapp.net", w, 105) is False
        assert cu.remember_sent(s, "1@newsletter", w, 0) is False
    finally:
        db.close()
