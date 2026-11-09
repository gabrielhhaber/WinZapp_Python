"""channel_events.py: following, leaving and muting a channel reach the client."""
from types import SimpleNamespace as N

import channel_events as ce


def _jid(user, server="newsletter"):
    return N(User=user, Server=server)


def test_each_event_names_its_channel():
    assert ce.channel_of("joined", N(NewsletterMetadata=N(ID=_jid("123")))) == "123@newsletter"
    assert ce.channel_of("left", N(ID=_jid("123"))) == "123@newsletter"
    assert ce.channel_of("muted", N(ID=_jid("5511", "s.whatsapp.net"))) == ""


def test_the_events_become_channels_changed():
    handlers = {}

    class Client:
        def event(self, cls):
            def register(fn):
                handlers[cls] = fn
                return fn
            return register

    sent = []
    s = N(name="acct", emit=lambda event, data: sent.append((event, data)))
    ce.register_events(Client(), s)
    import neonize.events as nev
    assert set(handlers) == {getattr(nev, name) for name, _kind in ce.EVENTS}
    handlers[nev.NewsletterLeaveEv](None, N(ID=_jid("9")))
    handlers[nev.NewsletterJoinEv](None, N(NewsletterMetadata=N(ID=_jid("8"))))
    handlers[nev.NewsletterMuteChangeEv](None, N(ID=_jid("x", "g.us")))
    assert sent == [("channels-changed", {"session": "acct", "kind": "left", "id": "9@newsletter"}),
                    ("channels-changed", {"session": "acct", "kind": "joined", "id": "8@newsletter"})]
