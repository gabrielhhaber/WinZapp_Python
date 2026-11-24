"""Native PCM ownership and fallback, with fake sockets and no audio devices."""
import struct
import threading
from types import SimpleNamespace

import pytest

import core.call_audio_native as native


class Fallback:
    def __init__(self):
        self.events = []

    def start(self):
        self.events.append('start')

    def send_microphone(self, pcm, **owner):
        self.events.append((pcm, owner))

    def stop(self, **owner):
        self.events.append(('stop', owner))


def descriptor(**changes):
    return dict(protocol=1, port=1234, secret='ab' * 32, sampleRate=48000,
                generation=7) | changes


class Connection:
    def __init__(self, data):
        self.data = bytearray(data)
        self.writes = []
        self.closed = False

    def recv(self, size):
        # Deliberately fragmented TCP, including the handshake and length header.
        result = bytes(self.data[:min(size, 3)])
        del self.data[:len(result)]
        return result

    def sendall(self, data):
        self.writes.append(data)

    def setsockopt(self, *args):
        pass

    def shutdown(self, *args):
        self.closed = True

    def close(self):
        self.closed = True


def response(value=None, status=200):
    return SimpleNamespace(status_code=status, json=lambda: {'response': value or descriptor()})


def configured(negotiate=lambda body: response(), receiver=lambda *a, **k: None):
    fallback = Fallback()
    transport = native.NeonizeCallAudioTransport(fallback, negotiate)
    transport.configure(receiver, 123)
    transport._epoch = 'a' * 32
    return transport, fallback


@pytest.fixture
def no_writer(monkeypatch):
    # Receive tests drive fragmented bytes synchronously. The writer has its
    # own direct test; don't wait through its real heartbeat queue timeout.
    class Worker:
        def __init__(self, **kwargs):
            pass
        def start(self):
            pass
        def join(self, timeout):
            pass
    monkeypatch.setattr(native.threading, 'Thread', Worker)


def test_binary_frame_is_not_base64_and_has_bound_owner(monkeypatch, no_writer):
    pcm = b'\x80\xff' * 960
    packet = struct.pack('<IQ', 48000, 7) + pcm
    connection = Connection(b'NZA1' + struct.pack('<I', len(packet)) + packet)
    targets, received, bodies = [], [], []
    def connect(target, **kwargs):
        targets.append(target)
        return connection
    monkeypatch.setattr(native.socket, 'create_connection', connect)
    transport, fallback = configured(lambda body: (bodies.append(body), response())[1],
                                     lambda *a, **k: received.append((a, k)))
    transport._run()
    assert targets == [('127.0.0.1', 1234)]
    assert connection.writes[0] == bytes.fromhex('ab' * 32)
    assert bodies == [{'mediaProtocol': 1, 'mediaEpoch': 'a' * 32, 'streamEpoch': 123}]
    assert received == [((pcm, 48000), {'media_epoch': 'a' * 32})]
    assert connection.closed and not transport.receiving_native
    transport.send_microphone(pcm, sample_rate=48000, media_epoch='a' * 32, stream_epoch=123)
    assert fallback.events[-1][0] == pcm


@pytest.mark.parametrize('size,rate,generation', [(0,48000,7), (2**32-1,48000,7),
                                               (14,16000,7), (14,48000,8), (13,48000,7)])
def test_malformed_or_wrong_owner_never_reaches_receiver(monkeypatch, no_writer, size, rate, generation):
    connection = Connection(b'NZA1' + struct.pack('<IIQ', size, rate, generation) + b'xx')
    monkeypatch.setattr(native.socket, 'create_connection', lambda *a, **k: connection)
    received = []
    transport, _ = configured(receiver=lambda *a, **k: received.append(a))
    transport._run()
    assert not received and connection.closed


@pytest.mark.parametrize('changes', [{'port':True}, {'port':65536}, {'protocol':True},
                                     {'generation':0}, {'sampleRate':44100},
                                     {'secret':'x'*64}, {'secret':' '*64}])
def test_invalid_descriptors_are_rejected(changes):
    with pytest.raises(ValueError):
        native.validate_descriptor(descriptor(**changes))


@pytest.mark.parametrize('status', [404, 501, 503])
def test_old_or_unavailable_bridge_keeps_socketio(status):
    transport, fallback = configured(lambda body: response(status=status))
    transport._run()
    transport.send_microphone(b'pcm', sample_rate=48000, media_epoch='a'*32, stream_epoch=123)
    assert fallback.events[0][0] == b'pcm'


def test_capture_queue_is_bounded_and_rejects_old_session():
    transport, fallback = configured()
    transport._active = True
    def send(byte, **changes):
        owner = dict(sample_rate=48000, media_epoch='a'*32, stream_epoch=123) | changes
        transport.send_microphone(bytes([byte])*1920, **owner)
    for i in range(5):
        send(i)
    send(9, media_epoch='b'*32)
    send(9, stream_epoch=122)
    assert [transport._queue.get_nowait()[1][0] for _ in range(3)] == [2,3,4]
    assert not fallback.events


def test_writer_drops_expired_capture_and_writes_binary(monkeypatch):
    transport, _ = configured()
    transport._queue.put((0, b'old'))
    transport._queue.put((1, b'new'))
    monkeypatch.setattr(native.time, 'monotonic', lambda: 1)
    done = threading.Event()
    connection = Connection(b'')
    def write(data):
        connection.writes.append(data)
        done.set()
    connection.sendall = write
    transport._write(connection, done)
    assert connection.writes == [struct.pack('<I', 3)+b'new']


def test_device_switch_closes_local_socket_without_bridge_stop():
    transport, fallback = configured()
    connection = Connection(b'')
    transport._conn = connection
    transport._active = True
    transport.close()
    transport.send_microphone(b'pcm', sample_rate=48000, media_epoch='a'*32, stream_epoch=123)
    assert connection.closed and not transport.receiving_native and not fallback.events
    transport.stop(media_epoch='a'*32, stream_epoch=123)
    assert fallback.events == [('stop', {'media_epoch':'a'*32, 'stream_epoch':123})]


def test_late_negotiation_after_stop_cannot_open_socket(monkeypatch):
    transport = None
    def negotiate(body):
        transport.close()
        return response()
    transport, _ = configured(negotiate)
    monkeypatch.setattr(native.socket, 'create_connection', lambda *a, **k: pytest.fail('late connect'))
    transport._run()
    assert not transport.receiving_native


def test_start_waits_for_owner_and_launches_only_once(monkeypatch):
    starts = []
    class Thread:
        def __init__(self, **kwargs):
            pass
        def start(self):
            starts.append(True)
    monkeypatch.setattr(native.threading, 'Thread', Thread)
    transport, fallback = configured()
    transport._epoch = ''
    transport.start()
    assert not starts
    transport.bind_owner('a'*32)
    transport.bind_owner('b'*32)
    transport.start()
    assert len(starts) == 1 and transport._epoch == 'a'*32


@pytest.mark.parametrize('neonize', [False, True])
def test_calls_choose_native_only_for_neonize(monkeypatch, neonize):
    from tests.test_call_audio_lifecycle_ownership import methods
    import core.call_audio as audio
    captured = {}
    def session(sio, config, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(config=config)
    monkeypatch.setattr(audio, 'CallAudioSession', session)
    window = SimpleNamespace(ws=SimpleNamespace(sio=object(), instance_name='account'),
                             token='account', wpp_server='http://127.0.0.1', wpp_port=6300,
                             settings={}, call_audio_device=lambda direction: None,
                             _uses_neonize=lambda: neonize)
    builder = methods('_build_call_audio_session')['_build_call_audio_session']
    posts = []
    builder.__globals__['api_post'] = lambda *a, **k: posts.append((a,k))
    result, name = builder(window)
    assert name == 'account' and result.config.neonize_audio == neonize
    assert isinstance(captured['transport'], native.NeonizeCallAudioTransport) == neonize
    if neonize:
        captured['transport']._negotiate({'mediaEpoch':'a'*32})
        assert posts == [(('http://127.0.0.1:6300/api/account/call/audio/transport',),
                          {'token':'account', 'json':{'mediaEpoch':'a'*32}, 'timeout':3})]


def test_session_binds_receiver_and_closes_transport_on_device_switch():
    from core.call_audio import CallAudioConfig, CallAudioSession
    from tests.test_call_audio_session import _SoundDevice
    transport, fallback = configured()
    transport._epoch = ''
    session = CallAudioSession(None, CallAudioConfig(session='account'),
                               sounddevice_module=_SoundDevice(), transport=transport)
    assert transport._receiver.__self__ is session
    assert session.set_media_epoch('a'*32)
    assert transport._epoch == session.media_epoch and transport._stream == session._stream_epoch
    session.stop(notify_bridge=False)
    assert transport._closed.is_set() and not fallback.events


def test_socketio_receive_resumes_only_after_native_releases_session():
    from tests.test_call_audio_lifecycle_ownership import methods
    received = []
    session = SimpleNamespace(native_audio_active=True,
                              enqueue_remote_audio=lambda *a, **k: received.append((a,k)))
    window = SimpleNamespace(_call_audio_session=session, _call_ring_audio_session=None)
    receive = methods('on_call_remote_audio')['on_call_remote_audio']
    receive(window, b'pcm', 48000, media_epoch='a'*32)
    assert not received
    session.native_audio_active = False
    receive(window, b'pcm', 48000, media_epoch='a'*32)
    assert received == [((b'pcm',48000), {'media_epoch':'a'*32})]
