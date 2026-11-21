"""Forward only privacy fields the notification explicitly says changed."""
import logging
from profile_privacy import settings_to_wpp

log = logging.getLogger("bridge")

CHANGED = {"LastSeenChanged": "lastSeen", "OnlineChanged": "online", "StatusChanged": "about",
           "ProfileChanged": "profilePicture", "ReadReceiptsChanged": "readReceipts", "GroupAddChanged": "groupAdd"}


def changed(s, event):
    values = settings_to_wpp(event.NewSettings)
    fields = {name: values[name] for flag, name in CHANGED.items()
              if getattr(event, flag, False) and values.get(name) is not None}
    if fields.get("readReceipts") in ("all", "none"):
        s._read_receipts = fields["readReceipts"]
    if fields:
        s.emit("privacy-settings-changed", {"session": s.name, "settings": fields})


def register_events(cli, s):
    import neonize.events as nev
    if hasattr(nev, "PrivacySettingsEv"):
        @cli.event(nev.PrivacySettingsEv)
        def on_privacy(client, event):
            try:
                changed(s, event)
            except Exception:
                log.exception("privacy settings changed")
