"""Known native refusals retain their meaning without leaking native text."""
import pytest

from call_placement_errors import placement_failure


@pytest.mark.parametrize("text, expected", [
    ("meowcaller: get group info: info query returned status 429: rate-overlimit", (429, "rate_limited")),
    ("meowcaller: group call by ID requires at least two remote members", (400, "group_too_few_members")),
    ("meowcaller: group call by ID has 32 remote members; at most 31 can be called", (400, "group_too_many_members")),
    ("meowcaller: group call by ID has 150 remote members; at most 31 can be called", (400, "group_too_many_members")),
    ("private identity@example.invalid: 429 rate-overlimit", (500, "placement_failed")),
    ("meowcaller: get group info: info query returned status 500: other", (500, "placement_failed")),
    ("meowcaller: group call by ID requires at least two remote members\nprivate", (500, "placement_failed")),
])
def test_only_exact_known_errors_are_classified(text, expected):
    assert placement_failure(RuntimeError(text)) == expected
