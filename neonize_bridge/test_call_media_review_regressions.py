"""Review counterexamples: retain controls and don't undo camera-off."""
import threading
from types import SimpleNamespace as N

import pytest

from test_call_media_epoch import media, packet, A
from call_media_ingress import MediaIngress


def test_camera_start_cannot_remove_same_capture_stop_barrier():
    work = MediaIngress()
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    owner, actions = object(), []
    def blocked():
        entered.set()
        assert release.wait(2)
    try:
        work.submit('camera', blocked)
        assert entered.wait(1)
        work.submit('camera_stop', lambda: actions.append('off'), owner=owner, epoch=100)
        work.submit('camera_start', lambda: actions.append('on'), owner=owner, epoch=100)
        work.submit('camera', done.set)
        release.set()
        assert done.wait(1)
        assert actions == ['off']
    finally:
        release.set()
        work.close()
        work.join(1)


@pytest.mark.parametrize('kind', ['audio_stop', 'camera_stop'])
@pytest.mark.parametrize('replacement', [False, True])
def test_control_received_under_held_model_lock_is_retained_without_retargeting(kind, replacement):
    m, owner, _, _ = media()
    actions = []
    m.video = N(camera_stop=lambda *a, **k: actions.append('off'), end=lambda *a: None)
    locked, release = threading.Event(), threading.Event()
    def hold():
        with m._lock:
            locked.set()
            assert release.wait(2)
    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert locked.wait(1)
        data = packet()
        data.update(epoch=100, native=True)
        assert m.enqueue_media(kind, data) is True
        if replacement:
            # Model mutation normally holds this lock; the holder is paused
            # here, so this deterministic fixture models its replacement.
            m._calls[owner['id']] = dict(owner, media_epoch='b' * 32)
        release.set()
        holder.join(1)
        done = threading.Event()
        m._ingress.submit('mic' if kind == 'audio_stop' else 'camera', done.set)
        assert done.wait(1)
        if kind == 'audio_stop':
            assert bool(owner.get('mic_stopped_epoch')) is (not replacement)
        else:
            assert actions == ([] if replacement else ['off'])
    finally:
        release.set()
        holder.join(1)
        m.close()
        m._ingress.join(1)


def test_camera_resume_rejects_stopped_epoch_but_accepts_new_capture():
    m, owner, _, _ = media()
    actions = []
    m.video = N(camera_stop=lambda *a, **k: actions.append('off'),
                camera_start=lambda *a, **k: actions.append('on'), end=lambda *a: None)
    owner['camera_epoch'] = 100
    m.camera_stop({'mediaProtocol': 1, 'mediaEpoch': A, 'epoch': 100, 'native': True})
    m.camera_start({'mediaProtocol': 1, 'mediaEpoch': A, 'epoch': 100})
    assert actions == ['off']
    m.camera_start({'mediaProtocol': 1, 'mediaEpoch': A, 'epoch': 101})
    assert actions[-1] == 'on'
    m.close()
