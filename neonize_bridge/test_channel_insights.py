"""channels/insights: WhatsApp Web's insights query with the client's metrics."""
import channel_insights as ci


def test_the_metrics_go_into_the_query_input():
    metrics = [{"id": 1, "type": "UNIQUE_VISITORS", "group_by": {"number_of_days": 30}}]
    channel, variables = ci.insights_request({"id": "120363000000000001@newsletter", "metrics": metrics})
    assert channel == "120363000000000001@newsletter"
    assert variables == {"input": {"newsletter_id": channel, "metrics": metrics}}
    assert ci.QUERY == "9853618868050977" and ci.FIELD == "xwa2_newsletter_admin_insights"


def test_anything_else_is_refused():
    for body in ({"id": "1@newsletter", "metrics": []}, {"id": "1@newsletter", "metrics": [{"x": 1}]},
                 {"id": "5511@s.whatsapp.net", "metrics": [{"id": 1, "type": "FOLLOWER"}]}, None):
        assert ci.insights_request(body) is None
