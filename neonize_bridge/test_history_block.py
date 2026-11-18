"""A history block is stored in a few transactions (Session._store_history_block),
not a commit per chat and per name: those held the store's lock long enough
to stall every route and drop the client's socket."""
from types import SimpleNamespace as N

from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message

import server
from store import Store


class Counting:
    """The store, counting the per-item writes the block must not use."""

    def __init__(self, store):
        self.store, self.single = store, 0

    def chat(self, *a, **k):
        self.single += 1
        return self.store.chat(*a, **k)

    def contact(self, *a, **k):
        self.single += 1
        return self.store.contact(*a, **k)

    def __getattr__(self, name):
        return getattr(self.store, name)


def conversation(jid, n, unread=None):
    messages = [N(message=N(key=N(fromMe=False, ID=f"{jid[:3]}{i}", participant=""),
                            participant="", messageTimestamp=100 + i,
                            message=Message(conversation=f"m{i}"), pushName="Ana",
                            starred=False)) for i in range(n)]
    c = N(ID=jid, name="Chat " + jid[:3], displayName="", conversationTimestamp=200,
          lastMsgTimestamp=0, messages=messages,
          HasField=lambda f, u=unread: f == "unreadCount" and u is not None, unreadCount=unread)
    return c


def test_a_block_is_stored_without_a_write_per_chat(tmp_path):
    db = Counting(Store(str(tmp_path / "b.db")))
    data = N(pushnames=[N(ID="5511@s.whatsapp.net", pushname="Ana"), N(ID="", pushname="x")],
             conversations=[conversation("5511@s.whatsapp.net", 3, unread=2),
                            conversation("1203@g.us", 2), conversation("0@broadcast", 1)],
             progress=40)
    assert server.Session._store_history_block(N(db=db), data) == 40
    assert db.single == 0
    chats = {c["jid"]: c for c in db.chats()}
    assert set(chats) == {"5511@s.whatsapp.net", "1203@g.us"}
    assert chats["5511@s.whatsapp.net"]["unread"] == 2 and chats["1203@g.us"]["ts"] == 200
    assert len(db.messages("5511@s.whatsapp.net")) == 3 and len(db.messages("1203@g.us")) == 2
    assert db.contact_of("5511@s.whatsapp.net")["pushname"] == "Ana"


def test_omitted_metadata_batch_stores_chats_and_skips_parent_groups_and_progress(tmp_path):
    from neonize.proto.waHistorySync.WAWebProtobufsHistorySync_pb2 import Conversation, HistorySync
    import history_progress
    import history_resume
    import omitted_history as oh

    db = Store(str(tmp_path / "omitted.db"))
    s = N(db=db, _history_progress=None, _history_resume=None)
    s._store_history_block = lambda d: server.Session._store_history_block(s, d)
    parent = Conversation(ID="999@g.us", name="Community Parent", isParentGroup=True)
    older_group = Conversation(ID="888@g.us", name="Old Group", lastMsgTimestamp=1690000000)
    older_dm = Conversation(
        ID="111@lid",
        name="Older Contact",
        conversationTimestamp=1700000000,
        archived=True,
        pnJID="5511999990001@s.whatsapp.net",
        lidJID="111@lid",
        accountLid="111@lid",
    )
    batch = oh.encode_omitted_batch(
        [parent, older_group, older_dm],
        sync_type=HistorySync.RECENT,
        chunk_order=1,
        progress=100,
    )
    server.Session._store_history(s, batch)

    # Progress and history_resume must NOT falsely treat omitted metadata as 100% synced
    assert history_progress.progress_of(s, 0).percent is None
    assert history_resume.status_of(s, 0)["historyTransfer"] is None

    chats = {c["jid"]: c for c in db.chats()}
    assert set(chats) == {"111@lid", "888@g.us"}
    assert chats["111@lid"]["archived"] == 1 and chats["111@lid"]["ts"] == 1700000000
    assert db.chat_omitted_info(["111@lid"]) == {"chat": "111@lid", "account_lid": "111@lid"}
    assert db.phone_of_lid("111@lid") == "5511999990001@s.whatsapp.net"


def test_load_names_skips_community_parent_groups(tmp_path):
    db = Store(str(tmp_path / "groups.db"))
    community = N(
        JID=N(User="999", Server="g.us"),
        GroupName=N(Name="My Community"),
        GroupParent=N(IsParent=True),
        Participants=[],
    )
    subgroup = N(
        JID=N(User="888", Server="g.us"),
        GroupName=N(Name="Subgroup"),
        GroupParent=N(IsParent=False),
        Participants=[],
    )
    client = N(
        contact=N(get_all_contacts=lambda: []),
        get_joined_groups=lambda: [community, subgroup],
        get_newsletter_subscribed=lambda: [],
    )
    s = N(db=db, client=client, reload_names=lambda: 0, roster_register=lambda g: None)
    server.Session._load_names(s)
    assert [c["jid"] for c in db.chats()] == ["888@g.us"]
    assert "999@g.us" in s._community_ids
