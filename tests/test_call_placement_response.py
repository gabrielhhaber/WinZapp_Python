"""Recognized bridge reasons produce short, localized spoken errors."""
from types import SimpleNamespace as N

import pytest

from core.call_placement_errors import CallPlacementError
from main_window.calls import CallsMixin


@pytest.mark.parametrize("reason, key, status", [
    ("rate_limited", "voice_call_rate_limited", 429),
    ("group_too_few_members", "group_call_too_few_members", 400),
    ("group_too_many_members", "group_call_too_many_members", 400),
])
def test_bridge_reason_is_spoken_through_the_locale(reason, key, status):
    response = N(status_code=status, text="safe error",
                 json=lambda: {"status": "error", "reasonCode": reason})
    with pytest.raises(CallPlacementError) as caught:
        CallsMixin._raise_for_call_response(object(), response, "offer")
    stub = N(i18n=N(t=lambda k: "translated:" + k), _CALL_ERROR_MAX_SPOKEN=120)
    assert CallsMixin._call_error_text(stub, caught.value) == "translated:" + key


@pytest.mark.parametrize("body", [None, [], {}, {"reasonCode": "private"},
    {"status": "error", "reasonCode": "unknown"}])
def test_custom_and_legacy_failures_still_have_the_plain_http_error(body):
    response = N(status_code=500, text="safe", json=lambda: body)
    with pytest.raises(RuntimeError) as caught:
        CallsMixin._raise_for_call_response(object(), response, "offer")
    assert type(caught.value) is RuntimeError and str(caught.value) == "HTTP 500"
