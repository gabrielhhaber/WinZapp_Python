"""Independent-review counterexamples: ownership cannot change under PCM."""
import queue
import base64
import time
from types import SimpleNamespace as N

import numpy as np

from core.call_audio import CallAudioConfig, CallAudioSession
from tests.test_call_audio_lifecycle_ownership import methods
from core.call_media_ownership import retire_media_epoch, is_retired_media_epoch

A, B = 'a' * 32, 'b' * 32


def test_bound_audio_session_rejects_a_different_call_epoch():
    session = CallAudioSession(N(), CallAudioConfig('test'))
    session.set_media_epoch(A)
    accepted = session.set_media_epoch(B)
    assert accepted is False and session.media_epoch == A


def test_first_owner_binding_discards_unowned_pending_capture_and_playout():
    session = CallAudioSession(N(), CallAudioConfig('test'))
    session._on_microphone_frame(48000)(np.ones((960, 1), dtype=np.float32), 960, None, None)
    session.enqueue_remote_audio(b'\1\0' * 960, 48000)
    session._output_buffer.configure(48000)
    session._output_buffer.push(np.ones(2880, dtype=np.float32))
    session.set_media_epoch(A)
    assert session._mic_queue.empty() and session._output_queue.empty()
    assert session._output_buffer.buffered_samples == 0


def test_inflight_unowned_capture_is_not_retagged_after_first_binding():
    emitted = []
    session = CallAudioSession(N(emit=lambda *args: emitted.append(args)), CallAudioConfig('test'))
    session._input_rate = 48000
    reads = [0]
    def capture():
        reads[0] += 1
        if reads[0] > 1:
            session._stop_event.set()
            raise queue.Empty
        session._dequeued_media_epoch = ''
        return np.ones(960, dtype=np.float32) * .1, 0, 0
    session._dequeue_fresh_microphone_frame = capture
    session._processor = object()
    def processing(processor, pcm):
        session.set_media_epoch(A)
        return pcm
    session._process_microphone = processing
    session._send_microphone_loop()
    assert not emitted


def test_dequeued_unowned_playout_cannot_cross_first_owner_binding():
    processed = []
    session = CallAudioSession(N(), CallAudioConfig('test'))
    session._output_stream, session._output_rate = object(), 48000
    session._output_resampler = N(reset=lambda: None, process=lambda *a: processed.append(a) or a[0])
    session.enqueue_remote_audio(b'\1\0' * 960, 48000)
    pending = session._output_queue.get_nowait()
    class Inflight:
        first = True
        def get(self, **kwargs):
            if self.first:
                self.first = False
                session.set_media_epoch(A)
                return pending
            session._stop_event.set()
            raise queue.Empty
        def get_nowait(self):
            raise queue.Empty
    session._output_queue = Inflight()
    session._play_remote_loop()
    assert not processed


def test_partial_microphone_frame_history_cannot_cross_first_binding():
    emitted = []
    session = CallAudioSession(N(emit=lambda *a: emitted.append(a)), CallAudioConfig('test'))
    session._input_rate = 44100
    steps = [0]
    def capture():
        steps[0] += 1
        if steps[0] == 1:
            session._dequeued_media_epoch = ''
            return np.ones(441, dtype=np.float32) * .1, 0, 0
        if steps[0] == 2:
            session.set_media_epoch(A)
            session._dequeued_media_epoch = A
            return np.ones(882, dtype=np.float32) * .2, 0, 0
        session._stop_event.set()
        raise queue.Empty
    session._dequeue_fresh_microphone_frame = capture
    session._send_microphone_loop()
    assert len(emitted) == 1
    samples = np.frombuffer(base64.b64decode(emitted[0][1]['pcm']), dtype='<i2')
    assert np.all(np.abs(samples[256:] - 6553) <= 2)


def test_conflicting_or_retired_state_cannot_rebind_current_call():
    scope = methods('on_voice_call_state_event')
    fn = scope['on_voice_call_state_event']
    # Valid matching identifiers would otherwise reach the existing mutation.
    fn.__globals__.update(time=time, call_event_matches_active=lambda *a: True)
    audio = CallAudioSession(N(), CallAudioConfig('test'))
    audio.set_media_epoch(A)
    active = {'identity': 'same', 'call_id': 'same', 'peer_jid': '', 'media_epoch': A}
    window = N(_active_voice_call=active, _call_audio_session=audio,
               _normalize_jid=lambda value: value, _chat_jids_equivalent=lambda *a: True,
               _voice_call_last_announced_state='ACTIVE')
    fn(window, {'id': 'same', 'state': 'ACTIVE', 'mediaEpoch': B})
    assert active['media_epoch'] == audio.media_epoch == A
    retire_media_epoch(window, B)
    window._active_voice_call = {'identity': 'same', 'call_id': 'same', 'media_epoch': ''}
    fn(window, {'id': 'same', 'state': 'ACTIVE', 'mediaEpoch': B})
    assert window._active_voice_call['media_epoch'] == ''


def test_retired_epoch_memory_is_bounded():
    window = N()
    for n in range(60):
        retire_media_epoch(window, f'{n:032x}')
    assert len(window._retired_media_epochs) == 50
    assert not is_retired_media_epoch(window, f'{0:032x}')
    assert is_retired_media_epoch(window, f'{59:032x}')


def test_capture_copy_uses_owner_from_callback_entry(monkeypatch):
    session = CallAudioSession(N(), CallAudioConfig('test'))
    source = np.ones((960, 1), dtype=np.float32)
    original = np.array
    def copy_then_bind(*args, **kwargs):
        copied = original(*args, **kwargs)
        session.set_media_epoch(A)
        return copied
    monkeypatch.setattr(np, 'array', copy_then_bind)
    session._on_microphone_frame(48000)(source, 960, None, None)
    captured = session._mic_queue.get_nowait()
    assert session.media_epoch == A and captured.epoch == ''


def test_remote_rate_conversion_uses_owner_from_enqueue_entry():
    session = CallAudioSession(N(), CallAudioConfig('test'))
    class BindDuringConversion:
        def __int__(self):
            session.set_media_epoch(A)
            return 48000
    session.enqueue_remote_audio(b'\1\0' * 960, BindDuringConversion())
    captured = session._output_queue.get_nowait()
    assert session.media_epoch == A and captured.epoch == ''
