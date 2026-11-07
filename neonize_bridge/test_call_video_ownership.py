"""Fakes only: no ffmpeg, native DLL, camera, network or real threads."""
from types import SimpleNamespace as N

import call_video as cv


class Pipe:
    def __init__(self, callback):
        self.callback, self.pushed, self.closed = callback, [], False
    def push(self, value):
        self.pushed.append(value)
    def close(self):
        self.closed = True


class Codec:
    def __init__(self):
        self.encoders, self.decoders = [], []
    def encoder(self, callback):
        pipe = Pipe(callback)
        self.encoders.append(pipe)
        return pipe
    def decoder(self, callback):
        pipe = Pipe(callback)
        self.decoders.append(pipe)
        return pipe


def make_video(monkeypatch):
    queued, sent, shown = [], [], []
    monkeypatch.setattr(cv.threading, 'Thread', lambda **k: N(start=lambda: queued.append((k['target'], k['args']))))
    calls = N(read_video=lambda *a: None, write_video=lambda *a: sent.append(a), video=lambda *a: True)
    video = cv.CallVideo('test', calls, lambda *a: shown.append(a), Codec())
    return video, calls, queued, sent, shown


def test_read_returning_after_end_cannot_recreate_decoder(monkeypatch):
    video, calls, queued, _, shown = make_video(monkeypatch)
    video.start_receiving('A')
    def read(*args):
        video.end('A')
        calls.read_video = lambda *a: None
        return b'old-unit'
    calls.read_video = read
    fn, args = queued.pop()
    fn(*args)
    assert video.codec.decoders == [] and not shown


def test_old_reader_finally_cannot_release_same_id_rejoin(monkeypatch):
    video, calls, queued, _, _ = make_video(monkeypatch)
    video.start_receiving('A')
    old_fn, old_args = queued.pop()
    def read(*args):
        video.end('A')
        video.start_receiving('A')
        return None
    calls.read_video = read
    old_fn(*old_args)
    assert 'A' in video._receiving
    assert len(queued) == 1


def test_old_decoder_output_is_discarded_after_rejoin(monkeypatch):
    video, calls, queued, _, shown = make_video(monkeypatch)
    video.start_receiving('A')
    units = iter((b'unit', None))
    calls.read_video = lambda *a: next(units)
    fn, args = queued.pop()
    fn(*args)
    old = video.codec.decoders[0]
    video.end('A')
    video.start_receiving('A')
    old.callback(b'old-jpeg')
    assert not shown


def test_old_encoder_output_is_discarded_after_stop_and_restart(monkeypatch):
    video, _, _, sent, _ = make_video(monkeypatch)
    video.start_receiving('A')
    video.camera_frame('A', {'jpeg': b'\xff\xd8one'})
    old = video.codec.encoders[0]
    video.camera_stop('A')
    video.camera_frame('A', {'jpeg': b'\xff\xd8two'})
    old.callback(b'old-unit')
    video.codec.encoders[-1].callback(b'new-unit')
    assert [item[1] for item in sent] == [b'new-unit']


def test_thread_start_failure_releases_receive_claim(monkeypatch):
    video, _, _, _, _ = make_video(monkeypatch)
    def fail():
        raise RuntimeError('synthetic thread start failure')
    monkeypatch.setattr(cv.threading, 'Thread', lambda **k: N(start=fail))
    try:
        video.start_receiving('A')
    except RuntimeError:
        pass
    assert 'A' not in video._receiving
