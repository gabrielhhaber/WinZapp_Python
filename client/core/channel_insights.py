"""A channel's statistics for its admins (WhatsApp Web's channel "Insights").

WhatsApp Web asks them with the MEX query WAWebMexFetchNewsletterInsightsJob
(id 9853618868050977): {"input": {"newsletter_id", "metrics": [...]}}, answer
`xwa2_newsletter_admin_insights` {metrics_status, last_update_time, result:
[{id, values: [{value, country, role, timestamp}]}]}. The metrics below are the
ones its Insights tab asks for (WAWebPopulateNewsletterInsightsAction, read over
CDP on 2026-11-09, with its "hide deltas" setting on): reach, followers, net
follows over the last 30 days, reach by role and by country, followers by
country, follows and unfollows per day. Read the same way as its processors
(WAWebNewsletterInsight*Processors): the first value of each metric, and by
country only the countries with at least 10. WhatsApp offers the statistics
from 100 followers on (channels_producer_insights_min_followers); below that
the server answers Bad Request.

Both engines send `METRICS` as the client gives them (`channels/insights`).
Tested in test_channel_insights.py.
"""
from dataclasses import dataclass, field

DAYS = 30
MIN_FOLLOWERS = 100
#: by country, WhatsApp Web leaves out the countries under this
COUNTRY_MINIMUM = 10

REACH, NET_FOLLOWS, REACH_BY_ROLE, REACH_BY_COUNTRY = 1, 3, 4, 5
FOLLOWERS, FOLLOWERS_BY_COUNTRY, FOLLOWS_PER_DAY, UNFOLLOWS_PER_DAY = 6, 7, 8, 9

METRICS = (
    {"id": REACH, "type": "UNIQUE_VISITORS", "group_by": {"number_of_days": DAYS}},
    {"id": REACH_BY_ROLE, "type": "UNIQUE_VISITORS", "group_by": {"number_of_days": DAYS, "role": True}},
    {"id": REACH_BY_COUNTRY, "type": "UNIQUE_VISITORS",
     "group_by": {"number_of_days": DAYS, "country": True}, "limit": 10},
    {"id": FOLLOWERS, "type": "FOLLOWER", "group_by": {"number_of_days": DAYS}},
    {"id": NET_FOLLOWS, "type": "NET_FOLLOWS", "group_by": {"number_of_days": DAYS}},
    {"id": FOLLOWERS_BY_COUNTRY, "type": "FOLLOWER",
     "group_by": {"number_of_days": DAYS, "country": True}, "limit": 10},
    {"id": FOLLOWS_PER_DAY, "type": "FOLLOWS", "group_by": {"number_of_days": 1}, "limit": DAYS},
    {"id": UNFOLLOWS_PER_DAY, "type": "UNFOLLOWS", "group_by": {"number_of_days": 1}, "limit": DAYS},
)


@dataclass
class Insights:
    available: bool                 # metrics_status OK (False: "MISSING", no data yet)
    reached: int | None = None
    followers: int | None = None
    net_follows: int | None = None
    follows: int | None = None      # summed over the days answered
    unfollows: int | None = None
    reach_by_role: dict = field(default_factory=dict)     # role -> accounts
    reach_by_country: list = field(default_factory=list)  # [(country, accounts)]
    followers_by_country: list = field(default_factory=list)
    updated: int = 0                # Unix seconds of WhatsApp's last update


def _number(value):
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _values(result, metric):
    for entry in result:
        if isinstance(entry, dict) and str(entry.get("id")) == str(metric):
            values = entry.get("values")
            return [v for v in values if isinstance(v, dict)] if isinstance(values, list) else []
    return []


def _first(result, metric):
    values = _values(result, metric)
    return _number(values[0].get("value")) if values else None


def _by_country(result, metric):
    rows = []
    for value in _values(result, metric):
        country, count = str(value.get("country") or "").upper(), _number(value.get("value"))
        if country and count is not None and count >= COUNTRY_MINIMUM:
            rows.append((country, count))
    return sorted(rows, key=lambda row: -row[1])


def _summed(result, metric):
    values = [_number(v.get("value")) for v in _values(result, metric)]
    values = [v for v in values if v is not None]
    return sum(values) if values else None


def parse_insights(node) -> Insights:
    """An Insights from the `xwa2_newsletter_admin_insights` node."""
    node = node if isinstance(node, dict) else {}
    result = node.get("result") if isinstance(node.get("result"), list) else []
    by_role = {}
    for value in _values(result, REACH_BY_ROLE):
        role, count = str(value.get("role") or "").lower(), _number(value.get("value"))
        if role and count is not None:
            by_role[role] = count
    return Insights(
        available=str(node.get("metrics_status") or "").upper() == "OK",
        reached=_first(result, REACH), followers=_first(result, FOLLOWERS),
        net_follows=_first(result, NET_FOLLOWS), follows=_summed(result, FOLLOWS_PER_DAY),
        unfollows=_summed(result, UNFOLLOWS_PER_DAY), reach_by_role=by_role,
        reach_by_country=_by_country(result, REACH_BY_COUNTRY),
        followers_by_country=_by_country(result, FOLLOWERS_BY_COUNTRY),
        updated=_number(node.get("last_update_time")) or 0)


def insight_lines(i18n, insights, format_date=str) -> list:
    """The lines the statistics window reads, one fact each."""
    t = i18n.t
    if not insights.available:
        return [t("channel_insights_missing")]
    lines = [t("channel_insights_period").format(days=DAYS)]
    for key, value in (("channel_insights_reached", insights.reached),
                       ("channel_insights_followers", insights.followers),
                       ("channel_insights_net", insights.net_follows),
                       ("channel_insights_follows", insights.follows),
                       ("channel_insights_unfollows", insights.unfollows)):
        if value is not None:
            lines.append(t(key).format(count=value))
    for role, count in sorted(insights.reach_by_role.items()):
        # WAWebNewsletterRoleDataProcessors: SUBSCRIBER and GUEST
        label = {"subscriber": "channel_insights_role_followers",
                 "guest": "channel_insights_role_others"}.get(role)
        if label:
            lines.append(t(label).format(count=count))
    for key, rows in (("channel_insights_reach_country", insights.reach_by_country),
                      ("channel_insights_followers_country", insights.followers_by_country)):
        if rows:
            lines.append(t(key).format(countries="; ".join(f"{c}: {n}" for c, n in rows)))
    if insights.updated:
        lines.append(t("channel_insights_updated").format(date=format_date(insights.updated)))
    return lines


def failure_key(followers) -> str:
    """What is said when the statistics could not be read: a channel known
    to be under MIN_FOLLOWERS is too small for them (WhatsApp answers Bad
    Request), anything else is "unavailable" (not an admin, offline, or the
    count unknown)."""
    if (isinstance(followers, int) and not isinstance(followers, bool)
            and followers < MIN_FOLLOWERS):
        return "channel_insights_too_small"
    return "channel_insights_unavailable"


def fetch_insights(post, connection, jid) -> Insights:
    """`channels/insights` through *post* (core.channels._post). ValueError
    "channel_insights_unavailable" when it fails (fewer than 100 followers,
    not an admin, offline)."""
    body = post(connection, "channels/insights", {"id": jid, "metrics": list(METRICS)},
                "channel_insights_unavailable", 60)
    node = body.get("response") if isinstance(body, dict) else None
    if not isinstance(node, dict):
        raise ValueError("channel_insights_unavailable")
    return parse_insights(node)
