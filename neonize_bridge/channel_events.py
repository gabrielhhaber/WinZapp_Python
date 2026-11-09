"""What WhatsApp tells this device about the channels the account follows:
following one, leaving one and muting one (on this or any other device of the
account), as a `channels-changed` socket event. The Channels window reads its
list again on it instead of waiting for Refresh, as on WPPConnect (where the
page's newsletter collection announces the same: createSessionUtil.ts).

The events are neonize's own (NewsletterJoin 32, NewsletterLeave 33,
NewsletterMuteChange 34); an older build without one simply never sends it.
"""
import logging

log = logging.getLogger("bridge")

#: neonize's event class -> what the payload's `kind` says
EVENTS = (("NewsletterJoinEv", "joined"), ("NewsletterLeaveEv", "left"),
          ("NewsletterMuteChangeEv", "muted"))


def channel_of(kind, event):
    """The channel's id ("<digits>@newsletter") an event is about, or ""."""
    jid = event.NewsletterMetadata.ID if kind == "joined" else event.ID
    user, server = getattr(jid, "User", ""), getattr(jid, "Server", "")
    return f"{user}@{server}" if user and server == "newsletter" else ""


def payload(session, kind, channel):
    return {"session": session, "kind": kind, "id": channel}


def register_events(cli, s):
    """Wires the client's channel events to `s.emit` (neonize's thread)."""
    import neonize.events as nev

    for name, kind in EVENTS:
        event_class = getattr(nev, name, None)
        if event_class is None:
            continue

        def handler(_client, event, kind=kind):
            try:
                channel = channel_of(kind, event)
                if channel:
                    s.emit("channels-changed", payload(s.name, kind, channel))
            except Exception:
                log.exception("channel event")

        cli.event(event_class)(handler)
