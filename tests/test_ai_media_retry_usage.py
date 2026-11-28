"""One short retry on a transient 429/503, and the per-provider usage counters.

Synthetic transports only: nothing here reaches the network, sleeps or opens a window.
"""
import json
import threading

import pytest

from core.ai_media import config, service, usage
from core.ai_media.errors import DescriptionError, parse_retry_after, status_error
from tests.test_ai_media_core import FakeHTTP, FakeResponse
from tests.test_ai_media_providers import IMAGE, _answer


class Sequenced(FakeHTTP):
    """Answers each POST from a list of (status, headers) in order; 200 carries OpenAI's answer."""

    def __init__(self, outcomes):
        super().__init__()
        self.outcomes = list(outcomes)

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        status, headers = self.outcomes.pop(0)
        response = FakeResponse(status=status, data=_answer("gemini" if "googleapis" in url else "openai") if status == 200 else b"")
        response.headers = headers
        return response


class RecordingEvent(threading.Event):
    """An operation's cancel flag whose wait() records the pause instead of sleeping."""

    def __init__(self):
        super().__init__()
        self.pauses = []

    def wait(self, timeout=None):
        self.pauses.append(timeout)
        return self.is_set()


def run(http, providers=("openai",), operation=None, sink=None):
    operation = operation or service.Operation("image")
    if not isinstance(operation.cancelled, RecordingEvent):
        previous, operation.cancelled = operation.cancelled, RecordingEvent()
        if previous.is_set():
            operation.cancelled.set()
    models = {p: config.PROVIDERS[p].model for p in config.PROVIDERS}
    result = service.run_chain(operation, list(providers), models, lambda p: "key-" + p, IMAGE, (),
                               "q", "i", "balanced", session_factory=lambda: http, usage_sink=sink)
    return result, operation


@pytest.mark.parametrize("value,expected", [
    ("2", 2.0), (" 1.5 ", 1.5), ("0", 0.0), ("", None), (None, None), ("-1", None),
    ("Wed, 21 Oct 2026 07:28:00 GMT", None), ("1e3", None), ("5s", None)])
def test_retry_after_reads_only_delta_seconds(value, expected):
    assert parse_retry_after(value) == expected


def test_only_429_and_503_keep_the_pause():
    assert status_error(429, "2").retry_after == 2.0 and status_error(429, "2").category == "quota"
    assert status_error(503, "2").retry_after == 2.0 and status_error(503, "2").category == "server"
    assert status_error(500, "2").retry_after is None
    assert status_error(401, "2").retry_after is None
    assert status_error(429).retry_after is None


def test_a_short_retry_after_gets_one_more_attempt_at_the_same_provider():
    http = Sequenced([(429, {"Retry-After": "2"}), (200, {})])
    (answer, provider), operation = run(http)
    assert (answer, provider) == ("from openai", "openai")
    assert len(http.calls) == 2
    assert operation.cancelled.pauses == [2.0]


def test_a_503_with_a_short_retry_after_is_retried_too():
    http = Sequenced([(503, {"Retry-After": "1"}), (200, {})])
    (answer, _provider), _operation = run(http)
    assert answer == "from openai" and len(http.calls) == 2


def test_never_more_than_one_retry_then_the_next_provider_is_tried():
    http = Sequenced([(429, {"Retry-After": "1"}), (429, {"Retry-After": "1"}), (200, {})])
    (answer, provider), operation = run(http, providers=("openai", "gemini"))
    assert provider == "gemini" and len(http.calls) == 3
    assert [url.split("/")[2] for url, _ in http.calls] == [
        "api.openai.com", "api.openai.com", "generativelanguage.googleapis.com"]
    assert operation.cancelled.pauses == [1.0]


def test_a_failure_without_retry_after_is_not_retried():
    http = Sequenced([(429, {})])
    with pytest.raises(service.ChainFailed) as raised:
        run(http)
    assert len(http.calls) == 1 and raised.value.attempts == (("openai", "quota"),)


def test_a_long_retry_after_is_not_waited_for():
    http = Sequenced([(429, {"Retry-After": "30"})])
    with pytest.raises(service.ChainFailed):
        run(http)
    assert len(http.calls) == 1


def test_no_retry_when_too_little_of_the_budget_is_left():
    operation = service.Operation("image")
    operation.deadline = operation.clock() + service.MIN_RETRY_SECONDS + 1
    error = DescriptionError("quota", 2.0)
    assert service.retry_pause(operation, error) is None
    operation.deadline = operation.clock() + service.MIN_RETRY_SECONDS + 3
    assert service.retry_pause(operation, error) == 2.0


def test_cancelling_during_the_pause_ends_the_action_without_a_second_request():
    operation = service.Operation("image")

    class CancelsDuringPause(RecordingEvent):
        def wait(self, timeout=None):
            self.set()
            return True
    operation.cancelled = CancelsDuringPause()
    http = Sequenced([(429, {"Retry-After": "2"}), (200, {})])
    with pytest.raises(DescriptionError, match="ai_error_cancelled"):
        run(http, operation=operation)
    assert len(http.calls) == 1


# ── the counters ─────────────────────────────────────────────────────────

def test_the_chain_reports_each_providers_final_outcome_only():
    seen = []
    http = Sequenced([(429, {"Retry-After": "1"}), (429, {"Retry-After": "1"}), (200, {})])
    run(http, providers=("openai", "gemini"), sink=lambda p, c: seen.append((p, c)))
    assert seen == [("openai", "quota"), ("gemini", usage.OK)]


def test_a_cancel_or_bad_media_is_not_counted_against_a_provider():
    seen = []
    operation = service.Operation("image")
    operation.cancel()
    with pytest.raises(DescriptionError):
        run(Sequenced([]), operation=operation, sink=lambda p, c: seen.append((p, c)))
    assert seen == []


def test_a_failing_sink_never_fails_the_request():
    def broken(provider, category):
        raise OSError("disk full")
    (answer, _provider), _operation = run(Sequenced([(200, {})]), sink=broken)
    assert answer == "from openai"


def test_counting_adds_up_and_keeps_the_rest_of_the_block():
    block = {"enabled": True, "order": ["openai"]}
    block = usage.counted(block, "openai", usage.OK)
    block = usage.counted(block, "openai", usage.OK)
    block = usage.counted(block, "openai", "quota")
    block = usage.counted(block, "gemini", "network")
    assert block["enabled"] is True and block["order"] == ["openai"]
    assert block["usage"]["openai"] == {"ok": 2, "failed": {"quota": 1}, "last": "quota"}
    assert block["usage"]["gemini"] == {"ok": 0, "failed": {"network": 1}, "last": "network"}


def test_unknown_providers_and_categories_are_not_stored():
    assert usage.counted({}, "nobody", usage.OK) == {}
    assert usage.counted({}, "openai", "my prompt text") == {}
    assert usage.counted("damaged", "openai", usage.OK)["usage"]["openai"]["ok"] == 1


def test_a_damaged_stored_block_reads_as_clean_counters():
    raw = {"usage": {"openai": {"ok": -3, "failed": {"quota": "many", "x": 5, "server": True}, "last": "x"},
                     "nobody": {"ok": 1}, "gemini": "bad"}}
    assert usage.validated(raw["usage"]) == {"openai": {"ok": 0, "failed": {}, "last": ""}}
    assert usage.validated(None) == {} and usage.validated([1]) == {}
    assert config.preferences({"ai_media": raw})["usage"] == {"openai": {"ok": 0, "failed": {}, "last": ""}}
    assert config.preferences({})["usage"] == {}


def test_a_counter_stops_at_its_ceiling():
    block = {"usage": {"openai": {"ok": usage.CEILING, "failed": {}, "last": ""}}}
    assert usage.counted(block, "openai", usage.OK)["usage"]["openai"]["ok"] == usage.CEILING


def test_only_counters_and_categories_are_stored():
    block = usage.counted({}, "openai", "quota")
    stored = json.dumps(block)
    assert set(block["usage"]["openai"]) == {"ok", "failed", "last"}
    assert stored == json.dumps({"usage": {"openai": {"ok": 0, "failed": {"quota": 1}, "last": "quota"}}})


def test_clearing_removes_only_the_counters():
    block = usage.counted({"enabled": True}, "openai", usage.OK)
    assert usage.cleared(block) == {"enabled": True}
    assert usage.cleared(None) == {}


def test_the_summary_has_one_line_per_provider_with_counters():
    counters = usage.validated(usage.counted(usage.counted({}, "openai", usage.OK), "gemini", "quota")["usage"])
    text = usage.summary_lines(counters, lambda name, ok, failed, last: f"{name}|{ok}|{failed}|{last}",
                               lambda category: "cat:" + category, "none")
    # In the catalogue's order, not the order they were counted in.
    assert text.splitlines() == [f"{config.PROVIDERS['gemini'].name}|0|1|cat:quota",
                                 f"{config.PROVIDERS['openai'].name}|1|0|"]
    assert usage.summary_lines({}, None, None, "none") == "none"


def test_the_page_text_names_the_last_failure_and_never_more():
    from ui.dialogs.ai_usage_options import usage_text
    counters = usage.validated(usage.counted({}, "openai", "quota")["usage"])
    t = lambda key: {"ai_usage_line": "{provider}: {ok} ok, {failed} failed.",
                     "ai_usage_last": "Last: {reason}", "ai_error_quota": "Quota.",
                     "ai_usage_none": "none"}[key]
    assert usage_text(t, counters) == f"{config.PROVIDERS['openai'].name}: 0 ok, 1 failed. Last: Quota."
