"""A channel's statistics for its admins (`channels/insights`).

The MEX query WhatsApp Web runs for its channel Insights,
WAWebMexFetchNewsletterInsightsJob (read over CDP on 2026-11-09): id
9853618868050977, variables {"input": {"newsletter_id", "metrics": [...]}},
answer `xwa2_newsletter_admin_insights`, given to the client as it comes
(core/channel_insights.py reads it, and sends the metrics). WhatsApp answers
Bad Request for a channel under 100 followers.

Tested in test_channel_insights.py.
"""
from channels import channel_id

QUERY = "9853618868050977"
FIELD = "xwa2_newsletter_admin_insights"
#: at most this many metrics per request (WhatsApp Web asks 8)
MAX_METRICS = 20


def insights_request(body):
    """(channel, query variables) from the route's {id, metrics}, or None."""
    body = body if isinstance(body, dict) else {}
    channel = channel_id(body.get("id"))
    metrics = body.get("metrics")
    if not channel or not isinstance(metrics, list):
        return None
    metrics = [m for m in metrics if isinstance(m, dict) and m.get("id") and m.get("type")]
    if not metrics:
        return None
    return channel, {"input": {"newsletter_id": channel, "metrics": metrics[:MAX_METRICS]}}
