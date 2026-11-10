"""What WhatsApp tells this device about the channels the account follows:
following one, leaving one and muting one (on this or any other device of the
account), and a change to one's name, description, picture, admins, owner,
state or settings, as a `channels-changed` socket event. The Channels window
and the "Admins and followers" window read again on it, as on WPPConnect
(where the page's newsletter collections announce the same:
channelsWatchBridge.ts).

The events are neonize's own (NewsletterJoin 32, NewsletterLeave 33,
NewsletterMuteChange 34) and WinZapp's NewsletterChange (58: whatsmeow patch
0013, neonize patch 0019), every other channel notification WhatsApp Web
handles; an older build without one simply never sends it.
"""
import logging
import re

log = logging.getLogger("bridge")

#: neonize's event class -> what the payload's `kind` says ("" for
#: NewsletterChange: its own op name)
EVENTS = (("NewsletterJoinEv", "joined"), ("NewsletterLeaveEv", "left"),
          ("NewsletterMuteChangeEv", "muted"), ("NewsletterChangeEv", ""))

#: NewsletterChange ops (xwa2_notify_newsletter_<op>) that change what the
#: channels windows show; the rest (milestones, paid partnerships, AI content,
#: question responses...) are only logged by name
WINDOW_OPS = frozenset((
    "on_metadata_update", "owner_on_metadata_update", "on_admin_metadata_update",
    "admin_promote", "admin_demote", "admin_invite_revoke", "on_state_change",
    "on_user_setting_change"))

_CHANNEL_IN_TEXT = re.compile(r"(?<![0-9])([0-9]{5,})@newsletter")


def kind_of(default, event):
    return default or str(getattr(event, "Op", "") or "")


def channel_of(kind, event):
    """The channel's id ("<digits>@newsletter") an event is about, or ""."""
    jid = event.NewsletterMetadata.ID if kind == "joined" else event.ID
    user, server = getattr(jid, "User", ""), getattr(jid, "Server", "")
    if user and server == "newsletter":
        return f"{user}@{server}"
    # a NewsletterChange whose payload names the channel deeper than "id"
    found = _CHANNEL_IN_TEXT.search(str(getattr(event, "Payload", "") or ""))
    return f"{found.group(1)}@newsletter" if found else ""


def wanted(kind):
    return kind in ("joined", "left", "muted") or kind in WINDOW_OPS


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
                kind = kind_of(kind, event)
                if not wanted(kind):
                    log.info("channel notification %s", kind or "?")
                    return
                channel = channel_of(kind, event)
                if channel:
                    s.emit("channels-changed", payload(s.name, kind, channel))
            except Exception:
                log.exception("channel event")

        cli.event(event_class)(handler)
