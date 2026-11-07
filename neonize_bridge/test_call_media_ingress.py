"""FIFO, bounded backlog and loop liveness with fake media, no native I/O."""
import ast
from pathlib import Path
import threading
from types import SimpleNamespace as N

import pytest

from call_media_ingress import MediaIngress


@pytest.fixture
def ingress():
    work = MediaIngress()
    yield work
    work.close()
    work.join(1)


def block_first(work, kind='mic'):
    entered, release = threading.Event(), threading.Event()
    def blocked():
        entered.set()
        assert release.wait(2)
    work.submit(kind, blocked, weight=1920)
    assert entered.wait(1)
    return release


def test_audio_fifo_and_video_start_does_not_hold_audio_lane(ingress):
    release = block_first(ingress, 'camera')
    audio = threading.Event()
    try:
        assert ingress.submit('mic', audio.set, weight=1920)
        assert audio.wait(1)
    finally:
        release.set()


def test_backlog_keeps_only_recent_bounded_audio_in_order(ingress):
    release = block_first(ingress)
    delivered, done = [], threading.Event()
    try:
        for n in range(20):
            ingress.submit('mic', lambda n=n: delivered.append(n), weight=1920)
        assert ingress.audio.summary()['queued'] == 10
        assert ingress.audio.summary()['bytes'] == 19200
        ingress.submit('mic', done.set, weight=0)
        release.set()
        assert done.wait(1)
        assert delivered == list(range(11, 20))
    finally:
        release.set()


def test_stop_discards_pending_frames_and_is_fifo_before_restart(ingress):
    release = block_first(ingress)
    seen, done = [], threading.Event()
    try:
        ingress.submit('mic', lambda: seen.append('stale'), weight=1920)
        ingress.submit('audio_stop', lambda: seen.append('stop'))
        ingress.submit('mic', lambda: seen.append('new'), weight=1920)
        ingress.submit('mic', done.set)
        release.set()
        assert done.wait(1)
        assert seen == ['stop', 'new']
    finally:
        release.set()


def test_expired_packets_are_not_sent_late():
    now = [0.]
    work = MediaIngress(clock=lambda: now[0])
    release = block_first(work)
    seen, done = [], threading.Event()
    try:
        work.submit('mic', lambda: seen.append('expired'), weight=1920)
        now[0] = 1.
        work.submit('mic', done.set)
        release.set()
        assert done.wait(1)
        assert not seen and work.audio.summary()['expired'] == 1
    finally:
        release.set()
        work.close()
        work.join(1)


def test_worker_failure_drops_one_frame_and_continues_without_exception_text(ingress, caplog):
    done = threading.Event()
    def fail():
        raise RuntimeError('private synthetic exception content')
    ingress.submit('mic', fail)
    ingress.submit('mic', done.set)
    assert done.wait(1)
    assert ingress.audio.summary()['failed'] == 1
    assert 'private synthetic exception content' not in caplog.text


def test_close_invalidates_pending_work_and_rejects_new(ingress):
    release = block_first(ingress)
    seen = []
    ingress.submit('mic', lambda: seen.append('old'))
    ingress.close()
    release.set()
    ingress.join(1)
    assert not seen and not ingress.submit('mic', lambda: None)


def test_old_stop_preserves_newer_stream_and_other_call_backlog(ingress):
    release = block_first(ingress)
    owner, other = object(), object()
    seen, done = [], threading.Event()
    try:
        ingress.submit('mic', lambda: seen.append('old'), owner=owner, epoch=99)
        ingress.submit('mic', lambda: seen.append('new'), owner=owner, epoch=101)
        ingress.submit('mic', lambda: seen.append('other'), owner=other, epoch=1)
        ingress.submit('audio_stop', lambda: seen.append('stop'), owner=owner, epoch=100)
        ingress.submit('mic', done.set)
        release.set()
        assert done.wait(1)
        assert seen == ['new', 'other', 'stop']
    finally:
        release.set()


def test_frame_overflow_cannot_evict_pending_stop_barrier(ingress):
    release = block_first(ingress)
    seen, done = [], threading.Event()
    try:
        ingress.submit('audio_stop', lambda: seen.append('stop'))
        for _ in range(30):
            ingress.submit('mic', lambda: None, weight=1920)
        ingress.submit('mic', done.set)
        release.set()
        assert done.wait(1)
        assert seen == ['stop']
    finally:
        release.set()


@pytest.mark.parametrize('name,kind', [('call_audio_mic', 'mic'), ('call_audio_stop', 'audio_stop'),
                                     ('call_video_camera', 'camera'), ('call_video_camera_stop', 'camera_stop'),
                                     ('call_video_camera_start', 'camera_start')])
def test_real_async_socket_handlers_only_enqueue_without_waiting_for_native(name, kind):
    source = Path(__file__).with_name('server.py')
    tree = ast.parse(source.read_text(encoding='utf-8'))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == name)
    requests = []
    async def media_of_socket(sid):
        return N(enqueue_media=lambda *args: requests.append(args))
    scope = {'media_of_socket': media_of_socket, 'sio': N(on=lambda event: lambda f: f)}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(source), 'exec'), scope)
    coroutine = scope[name]('fake-sid', {'sample': 1})
    # The awaited fake socket lookup completes immediately. Drive the actual
    # handler directly: a Windows event loop opens a loopback self-pipe, which
    # is unnecessary for this no-I/O coroutine contract.
    with pytest.raises(StopIteration):
        coroutine.send(None)
    assert requests == [(kind, {'sample': 1})]
