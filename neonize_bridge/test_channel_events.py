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


def test_a_channel_change_names_its_op_and_its_channel():
    change = N(ID=_jid("777"), Op="admin_promote", Payload="{}")
    assert ce.kind_of("", change) == "admin_promote"
    assert ce.channel_of("admin_promote", change) == "777@newsletter"
    # the channel deeper in the payload when the event names none
    deeper = N(ID=_jid("", ""), Op="on_metadata_update",
               Payload='{"newsletter":{"id":"120363000000000001@newsletter"}}')
    assert ce.channel_of("on_metadata_update", deeper) == "120363000000000001@newsletter"
    assert ce.channel_of("on_state_change", N(ID=_jid("", ""), Op="x", Payload="")) == ""


def test_only_what_the_channels_windows_show_is_sent(monkeypatch):
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
    handle = handlers[nev.NewsletterChangeEv]
    handle(None, N(ID=_jid("5"), Op="admin_demote", Payload=""))
    handle(None, N(ID=_jid("6"), Op="on_metadata_update", Payload=""))
    handle(None, N(ID=_jid("7"), Op="milestone", Payload=""))           # not shown anywhere
    assert sent == [
        ("channels-changed", {"session": "acct", "kind": "admin_demote", "id": "5@newsletter"}),
        ("channels-changed", {"session": "acct", "kind": "on_metadata_update", "id": "6@newsletter"})]
    assert {"admin_promote", "admin_demote", "on_metadata_update"} <= ce.WINDOW_OPS
