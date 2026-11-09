"""Who may react to a channel's updates (`channels/reactions`).

The same MEX mutation as editing a channel (channel_admin.UPDATE_QUERY,
xwa2_newsletter_update) with only `settings.reaction_codes`, as WhatsApp Web's
channel settings send it (WAWebMexUpdateNewsletterJob, read over CDP on
2026-11-09): {"newsletter_id", "updates": {"settings": {"reaction_codes":
{"value": ALL | BASIC | NONE}}}}.

Tested in test_channel_reactions.py.
"""
from channels import channel_id

VALUES = ("ALL", "BASIC", "NONE")


def reactions_request(body):
    """(channel, mutation variables) from the route's {id, value}, or None."""
    body = body if isinstance(body, dict) else {}
    channel = channel_id(body.get("id"))
    value = str(body.get("value") or "").upper()
    if not channel or value not in VALUES:
        return None
    return channel, {"newsletter_id": channel,
                     "updates": {"settings": {"reaction_codes": {"value": value}}}}
