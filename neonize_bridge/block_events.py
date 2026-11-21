"""Whatsmeow Blocklist notifications contain deltas or request re-querying."""
import logging
import msgmap

log = logging.getLogger("bridge")


def changed(s, event):
    if event.Action == 2:  # MODIFY: Changes is empty; re-request the full list.
        s.emit("block-state-changed", {"session": s.name, "kind": "refresh"})
        return
    if event.Action != 1:
        return
    for item in event.Changes:
        jid = item.JID
        if not jid.User or jid.Server not in ("s.whatsapp.net", "c.us", "lid") or item.BlockAction not in (1, 2):
            continue
        s.emit("block-state-changed", {"session": s.name, "kind": "contact",
            "contactId": msgmap.jid_wpp(f"{jid.User}@{jid.Server}"), "blocked": item.BlockAction == 1})


def register_events(cli, s):
    import neonize.events as nev
    if not hasattr(nev, "BlocklistEv"):
        return
    @cli.event(nev.BlocklistEv)
    def on_blocklist(client, event):
        try:
            changed(s, event)
        except Exception:
            log.exception("block list changed")
