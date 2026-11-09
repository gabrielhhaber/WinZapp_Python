"""A channel's statistics for its admins, read as WhatsApp Web's processors read them."""
import types

from core import channel_insights as ci

I18N = types.SimpleNamespace(t=lambda key: {
    "channel_insights_period": "Last {days} days:", "channel_insights_reached": "Reached: {count}",
    "channel_insights_followers": "Followers: {count}", "channel_insights_net": "Net: {count}",
    "channel_insights_follows": "New: {count}", "channel_insights_unfollows": "Left: {count}",
    "channel_insights_role_followers": "Followers reached: {count}",
    "channel_insights_role_others": "Others reached: {count}",
    "channel_insights_reach_country": "Reach by country: {countries}",
    "channel_insights_followers_country": "Followers by country: {countries}",
    "channel_insights_updated": "Updated {date}.", "channel_insights_missing": "No data yet.",
}.get(key, key))


def node(status="OK"):
    return {"newsletter_id": "1@newsletter", "metrics_status": status, "last_update_time": "1700000000",
            "result": [
                {"id": 1, "values": [{"value": 1520}]},
                {"id": 6, "values": [{"value": 300}]},
                {"id": 3, "values": [{"value": 25}]},
                {"id": 4, "values": [{"role": "SUBSCRIBER", "value": 900}, {"role": "GUEST", "value": 620}]},
                {"id": 5, "values": [{"country": "br", "value": 1200}, {"country": "pt", "value": 9},
                                     {"country": "us", "value": 300}]},
                {"id": 7, "values": [{"country": "BR", "value": 250}]},
                {"id": 8, "values": [{"value": 3, "timestamp": 1}, {"value": 4, "timestamp": 2}]},
                {"id": 9, "values": [{"value": 1, "timestamp": 1}]},
            ]}


def test_every_metric_is_read_as_whatsapp_web_reads_it():
    got = ci.parse_insights(node())
    assert got.available and (got.reached, got.followers, got.net_follows) == (1520, 300, 25)
    assert (got.follows, got.unfollows) == (7, 1)
    assert got.reach_by_role == {"subscriber": 900, "guest": 620}
    # under 10 is left out, as WhatsApp Web does; the biggest first
    assert got.reach_by_country == [("BR", 1200), ("US", 300)]
    assert got.followers_by_country == [("BR", 250)] and got.updated == 1_700_000_000


def test_the_lines_say_one_fact_each():
    lines = ci.insight_lines(I18N, ci.parse_insights(node()), lambda s: "today")
    assert lines == ["Last 30 days:", "Reached: 1520", "Followers: 300", "Net: 25", "New: 7", "Left: 1",
                     "Others reached: 620", "Followers reached: 900",
                     "Reach by country: BR: 1200; US: 300", "Followers by country: BR: 250", "Updated today."]


def test_no_data_yet_says_so():
    assert ci.insight_lines(I18N, ci.parse_insights(node("MISSING"))) == ["No data yet."]
    assert ci.parse_insights(None).available is False


def test_the_route_is_asked_with_whatsapp_webs_metrics():
    asked = []

    def post(connection, endpoint, payload, code, timeout):
        asked.append((endpoint, payload, code))
        return {"status": "success", "response": node()}
    assert ci.fetch_insights(post, None, "1@newsletter").followers == 300
    endpoint, payload, code = asked[0]
    assert endpoint == "channels/insights" and code == "channel_insights_unavailable"
    assert payload["id"] == "1@newsletter" and [m["id"] for m in payload["metrics"]] == [1, 4, 5, 6, 3, 7, 8, 9]


def test_an_answer_without_the_node_is_unavailable():
    import pytest
    with pytest.raises(ValueError):
        ci.fetch_insights(lambda *a: {"status": "success"}, None, "1@newsletter")
