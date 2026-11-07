"""Call/stream envelopes and delayed delivery, generated PCM and fake calls."""
import base64
import threading
from types import SimpleNamespace as N

import pytest

import call_media as cm

A, B = 'a' * 32, 'b' * 32


def packet(epoch=A, stream=100, samples=2880, value=b'\x10\x00'):
    return {'mediaProtocol': 1, 'mediaEpoch': epoch, 'streamEpoch': stream,
            'sampleRate': 48000, 'pcm': base64.b64encode(value * samples).decode()}


def media(epoch=A, call_id='same-id'):
    writes, emitted = [], []
    native = N(audio_format=lambda cid: {'sampleRate': 48000, 'generation': 0},
               write_audio_frame=lambda *args: writes.append(args) or True)
    m = cm.CallMedia('test', native, lambda *args: emitted.append(args))
    record = {'id': call_id, 'peer': 'fake', 'state': 'ACTIVE', 'connected': True,
              'outgoing': False, 'media_epoch': epoch}
    m._calls[call_id] = record
    with m._lock:
        m._publish_ingress_owner_locked()
    return m, record, writes, emitted


@pytest.mark.parametrize('call_id', ['different-id', 'same-id'])
def test_stale_wire_mic_cannot_enter_new_call_even_when_native_id_is_reused(call_id):
    m, _, writes, _ = media(B, call_id)
    m.microphone(packet(A))
    assert writes == []
    m.microphone(packet(B))
    assert len(writes) == 1 and writes[0][0] == call_id


def test_old_stop_cannot_erase_new_calls_partial_frame():
    m, _, writes, _ = media(B)
    m.microphone(packet(B, samples=1920))
    m.stop_microphone(packet(A))
    m.microphone(packet(B, samples=960))
    assert len(writes) == 1


def test_old_stream_cannot_reenter_after_device_restart_or_stop():
    m, _, writes, _ = media()
    m.microphone(packet(stream=100, samples=960))
    m.microphone(packet(stream=101, samples=960))
    m.microphone(packet(stream=100, samples=1920))
    assert not writes
    m.microphone(packet(stream=101, samples=1920))
    assert len(writes) == 1
    m.stop_microphone(packet(stream=101))
    m.microphone(packet(stream=101))
    assert len(writes) == 1
    m.microphone(packet(stream=102))
    assert len(writes) == 2


def test_older_stop_does_not_clear_newer_stream():
    m, _, writes, _ = media()
    m.microphone(packet(stream=101, samples=1920))
    m.stop_microphone(packet(stream=100))
    m.microphone(packet(stream=101, samples=960))
    assert len(writes) == 1


def test_preanswer_unbound_new_protocol_is_dropped():
    m, _, writes, _ = media()
    m.microphone(packet(epoch=''))
    assert not writes


def test_legacy_is_compatible_until_new_protocol_binds_this_call():
    m, _, writes, _ = media()
    legacy = packet()
    for key in ('mediaProtocol', 'mediaEpoch', 'streamEpoch'):
        legacy.pop(key)
    m.microphone(legacy)
    m.microphone(packet())
    assert len(writes) == 2
    m.microphone(legacy)
    assert len(writes) == 2


@pytest.mark.parametrize('stream', [None, False, -1, 0, 2**53, '100', 1.5])
def test_invalid_stream_epoch_never_reaches_native(stream):
    m, _, writes, _ = media()
    m.microphone(packet(stream=stream))
    assert not writes


def test_queued_work_is_bound_to_the_record_at_arrival():
    m, owner, writes, _ = media()
    pending = []
    m._ingress = N(submit=lambda kind, fn, **kw: pending.append(fn) or True)
    assert m.enqueue_media('mic', packet())
    replacement = dict(owner, media_epoch=B)
    m._calls[owner['id']] = replacement
    assert pending.pop()() is False and not writes


def test_ingress_does_not_wait_for_model_mutex():
    m, _, _, _ = media()
    locked, release = threading.Event(), threading.Event()
    def hold():
        with m._lock:
            locked.set()
            assert release.wait(2)
    worker = threading.Thread(target=hold)
    worker.start()
    try:
        assert locked.wait(1)
        assert m.enqueue_media('mic', packet()) is True
    finally:
        release.set()
        worker.join(1)
        m.close()
        m._ingress.join(1)


def test_remote_emission_failure_does_not_permanently_lose_reader(monkeypatch, caplog):
    m, owner, _, emitted = media()
    units = iter((b'\x01\x00' * 2880, b'\x02\x00' * 2880, None))
    def read(*args):
        pcm = next(units)
        return None if pcm is None else {'pcm': pcm, 'sampleRate': 48000, 'generation': 0}
    m.calls.read_audio_frame = read
    monkeypatch.setattr(cm, 'layer_error', lambda: LookupError)
    first = [True]
    def emit(event, data):
        if first[0]:
            first[0] = False
            raise RuntimeError('private error detail')
        emitted.append((event, data))
    m.emit = emit
    claim = (object(), owner)
    m._audio_readers[owner['id']] = claim
    m._pump_audio(owner['id'], claim)
    assert len(emitted) == 1 and emitted[0][1]['mediaEpoch'] == A
    assert owner['audio_delivery'].summary()['remote_emit_errors'] == 1
    assert 'private error detail' not in caplog.text


def test_close_invalidates_queued_work_and_reader_ownership():
    m, owner, _, _ = media()
    claim = (object(), owner)
    m._audio_readers[owner['id']] = claim
    m.close()
    assert not m.enqueue_media('mic', packet())
    assert not m._owns_audio(owner['id'], claim)


def test_frame_expired_while_waiting_for_lifecycle_gate_is_dropped():
    m, owner, writes, _ = media()
    assert m._dispatch_media('mic', packet(), owner, expires=0.) is False
    assert not writes


def test_video_encoder_work_does_not_hold_audio_lifecycle_gate():
    m, owner, writes, _ = media()
    entered, release = threading.Event(), threading.Event()
    def camera(*args, **kwargs):
        entered.set()
        assert release.wait(2)
    m.video = N(camera_frame=camera)
    work = threading.Thread(target=m._dispatch_media, args=('camera', {}, owner))
    work.start()
    try:
        assert entered.wait(1)
        assert m._lifecycle.acquire(blocking=False)
        try:
            m.microphone(packet())
        finally:
            m._lifecycle.release()
        assert len(writes) == 1
    finally:
        release.set()
        work.join(1)
