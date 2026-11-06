"""Group refusal reasons survive HTTP without forwarding private native errors."""
import json
from types import SimpleNamespace as N
from unittest.mock import patch

import pytest
from aiohttp import web

from test_call_media import FakeCalls, media, calls


def group_offer(session):
    app = web.Application()
    calls.register_routes(app.router, session=lambda req: session,
                          ok=lambda **kw: web.json_response(kw))
    handler = next(route.handler for route in app.router.routes()
                   if route.resource.canonical == "/api/{tok}/call/group/offer")
    async def body():
        return {"groupJid": "1203@g.us"}
    async def execute(_executor, function, *args):
        return function(*args)
    # Route semantics need no TCP server or event-loop self-pipe. The executor
    # is a synchronous fake; concurrency is covered by the lifecycle tests.
    with patch.object(calls.asyncio, "get_running_loop", return_value=N(run_in_executor=execute)):
        coroutine = handler(N(json=body))
        try:
            coroutine.send(None)
        except StopIteration as done:
            response = done.value
        else:
            coroutine.close()
            raise AssertionError("unexpected asynchronous I/O in the fake route")
    return response.status, json.loads(response.body)


@pytest.mark.parametrize("message, expected_status, expected_reason", [
    ("meowcaller: get group info: info query returned status 429: rate-overlimit", 429, "rate_limited"),
    ("meowcaller: group call by ID requires at least two remote members", 400, "group_too_few_members"),
    ("meowcaller: group call by ID has 32 remote members; at most 31 can be called", 400, "group_too_many_members"),
    ("private identity@example.invalid", 500, "placement_failed"),
])
def test_native_group_refusals_have_safe_reason_codes(message, expected_status, expected_reason):
    fake = FakeCalls()
    def refuse(*args, **kwargs):
        raise RuntimeError(message)
    fake.group_dial = refuse
    m, _ = media(fake)
    m.group_calls = True
    s = N(client=object(), status="CONNECTED", call_media=m, ringing_calls=calls.Pending())
    status, body = group_offer(s)
    assert status == expected_status
    assert body["reasonCode"] == expected_reason
    assert body["message"] == "the call could not be placed"
    assert message not in str(body)
    assert m.live_call() == ""


@pytest.mark.parametrize("value, expected", [
    ({"generation": 7, "peerAccepted": True, "ended": False, "phase": 4}, "ACTIVE"),
    (None, "ENDED"),
])
def test_success_response_reports_the_recovered_call_state(value, expected):
    from test_call_placement_state import prepared
    fake, m, _, _ = prepared(value)
    fake.group_dial = lambda *a, **kw: {"call": "C", "generation": 7}
    session = N(client=object(), status="CONNECTED", call_media=m, ringing_calls=calls.Pending())
    status, body = group_offer(session)
    assert status == 200 and body["response"]["call"]["state"] == expected
