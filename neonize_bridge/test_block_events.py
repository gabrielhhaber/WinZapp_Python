from types import SimpleNamespace as N
import block_events
from test_group_receipts import session, jid_n


def test_explicit_block_unblock_and_opaque_modification():
    sent=[]
    s=N(name="s",emit=lambda *args:sent.append(args))
    block_events.changed(s,N(Action=1,Changes=[N(JID=jid_n("901@lid"),BlockAction=1),
        N(JID=jid_n("1@s.whatsapp.net"),BlockAction=2),N(JID=jid_n("7@g.us"),BlockAction=1)]))
    assert sent==[("block-state-changed",{"session":"s","kind":"contact","contactId":"901@lid","blocked":True}),
        ("block-state-changed",{"session":"s","kind":"contact","contactId":"1@c.us","blocked":False})]
    block_events.changed(s,N(Action=2,Changes=[]))
    assert sent[-1]==("block-state-changed",{"session":"s","kind":"refresh"})


def test_registered_blocklist_handler_delivers_without_a_second_client(session):
    import neonize.events as events
    session.handlers[events.BlocklistEv](session.client,N(Action=2,Changes=[]))
    assert session.emitted[-1]==("block-state-changed",{"session":"account1","kind":"refresh"})
