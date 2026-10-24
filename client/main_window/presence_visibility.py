"""UI-thread read eligibility, separate from own online-presence policy."""
from core.contact_presence import cached
from core.contact_presence_refresh import needs_snapshot, same_read_contact
from core.conversation_view import conversation_in_view
from ui.dialogs.personal_presence_reader import active_personal_reader


def contact_presence_in_view(owner):
    panel = getattr(owner, "conversations_panel", None)
    jid = (getattr(panel, "conversation", None) or {}).get("remoteJid", "")
    try:
        return bool(jid.endswith(("@s.whatsapp.net", "@c.us", "@lid"))
                    and not getattr(owner, "_shutting_down", False)
                    and getattr(owner, "_wa_connected", False)
                    and not getattr(owner, "background_mode", False)
                    and not getattr(owner, "_window_hidden", False)
                    and conversation_in_view(panel) and owner.IsShown()
                    and not owner.IsIconized()
                    and (owner.IsActive() or active_personal_reader(owner, jid)))
    except RuntimeError:
        return False  # Native window/control already destroyed.


def refresh_missing_presence(owner, jid=None):
    """One external demand, retaining coordinator cooldown/cap/deny guards."""
    refresh = getattr(owner, "_refresh_open_contact_presence", None)
    if refresh is None or not contact_presence_in_view(owner):
        return
    current = owner.conversations_panel.conversation["remoteJid"]
    if jid is not None and not same_read_contact(owner, jid, current):
        return
    # Denial remains authoritative after receipt freshness expires.
    if not cached(owner, current, fresh=False).get("restricted") and needs_snapshot(owner):
        refresh()
