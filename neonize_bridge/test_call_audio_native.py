"""Only the current, connected media generation may reserve native audio."""
import pytest

from test_call_pcm48 import connected


def owned():
    media, native, _ = connected()
    owner = media._calls['C']
    owner.update(media_epoch='a'*32)
    media._ingress_owner = owner
    opened = []
    native.open_audio_transport = lambda *args: opened.append(args) or {'protocol':1}
    return media, owner, opened


def body(**changes):
    return {'mediaProtocol':1, 'mediaEpoch':'a'*32, 'streamEpoch':123} | changes


def test_reservation_binds_current_native_record_and_resets_partial_capture():
    media, owner, opened = owned()
    media._mic.push(b'x'*1920)
    assert media.open_audio_transport(body()) == {'protocol':1}
    assert opened == [('C',7,123)]
    assert owner['mic_stream_epoch'] == owner['native_audio_stream_epoch'] == 123
    assert owner['media_protocol'] == 1 and media._mic_owner is None
    with pytest.raises(ValueError):
        media.open_audio_transport(body())


@pytest.mark.parametrize('change', [{'mediaEpoch':'b'*32}, {'mediaProtocol':True},
                                   {'streamEpoch':True}, {'streamEpoch':0}])
def test_wrong_owner_or_stream_is_rejected(change):
    media, _, opened = owned()
    with pytest.raises((LookupError,ValueError)):
        media.open_audio_transport(body(**change))
    assert not opened


@pytest.mark.parametrize('changes', [{'connected':False}, {'state':'ENDED'},
                                    {'mic_stream_epoch':124}, {'mic_stopped_epoch':123}])
def test_reservation_cannot_revive_stopped_stream(changes):
    media, owner, opened = owned()
    owner.update(changes)
    with pytest.raises((LookupError,ValueError)):
        media.open_audio_transport(body())
    assert not opened


def test_replaced_record_and_missing_dll_keep_socketio():
    media, owner, opened = owned()
    media._calls['C'] = dict(owner)
    with pytest.raises(LookupError):
        media.open_audio_transport(body())
    media._ingress_owner = media._calls['C']
    media.calls.open_audio_transport = lambda *args: None
    assert media.open_audio_transport(body()) is None
    assert not opened


def test_stop_revokes_native_lease_with_matching_generation():
    media, owner, opened = owned()
    media.open_audio_transport(body())
    closed = []
    media.calls.close_audio_transport = lambda *args: closed.append(args)
    media.stop_microphone(body(streamEpoch=122))
    assert not closed
    media.stop_microphone(body())
    assert closed == [('C',7,123)]


@pytest.mark.parametrize('result,expected', [({'protocol':1},200), (None,501),
                                            (LookupError(),409), (ValueError(),400),
                                            (RuntimeError('private secret'),503)])
def test_transport_route_returns_only_safe_control_result(monkeypatch, result, expected):
    import json
    from types import SimpleNamespace as N
    from aiohttp import web
    import calls
    def reserve(data):
        if isinstance(result, Exception):
            raise result
        assert data == body()
        return result
    session = N(client=object(), status='CONNECTED', call_media=N(open_audio_transport=reserve))
    app = web.Application()
    calls.register_routes(app.router, session=lambda req: session,
                          ok=lambda **kw: web.json_response(kw))
    handler = next(route.handler for route in app.router.routes()
                   if route.resource.canonical == '/api/{tok}/call/audio/transport')
    async def payload():
        return body()
    async def execute(executor, func, *args):
        return func(*args)
    monkeypatch.setattr(calls.asyncio, 'get_running_loop', lambda: N(run_in_executor=execute))
    coroutine = handler(N(json=payload))
    with pytest.raises(StopIteration) as done:
        coroutine.send(None)
    response = done.value.value
    assert response.status == expected
    assert 'private secret' not in response.text
    if expected == 200:
        assert json.loads(response.body)['response'] == result
