"""Unknown/custom server errors cannot invent a localized placement refusal."""
import pytest

from core.call_placement_errors import CallPlacementError, placement_reason


@pytest.mark.parametrize("reason", ["rate_limited", "group_too_few_members", "group_too_many_members"])
def test_known_placement_reasons(reason):
    assert placement_reason({"status": "error", "reasonCode": reason}) == reason
    error = CallPlacementError(reason, 429)
    assert error.reason == reason and str(error) == "HTTP 429"


@pytest.mark.parametrize("body", [None, [], "rate_limited", {},
    {"status": "success", "reasonCode": "rate_limited"},
    {"status": "error", "reasonCode": "unknown"},
    {"status": "error", "reasonCode": ["rate_limited"]},
    {"status": "error", "reasonCode": {"private": "data"}},
])
def test_unrecognized_responses_keep_the_existing_error_path(body):
    assert placement_reason(body) is None
