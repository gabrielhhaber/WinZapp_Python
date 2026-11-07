"""Nonsecret media ownership tags; no account, UI or device access."""
import threading
import time
from dataclasses import dataclass

_lock = threading.Lock()
_last_stream = 0


def valid_media_epoch(value):
    return (isinstance(value, str) and len(value) == 32
            and all(c in '0123456789abcdef' for c in value))


def next_stream_epoch():
    global _last_stream
    with _lock:
        _last_stream = max(_last_stream + 1, time.monotonic_ns() // 1_000_000)
        return _last_stream


def media_fields(epoch='', stream_epoch=None):
    fields = {'mediaProtocol': 1, 'mediaEpoch': epoch if valid_media_epoch(epoch) else ''}
    if stream_epoch is not None:
        fields['streamEpoch'] = stream_epoch
    return fields


def accepts_media_epoch(expected, data):
    """Legacy WPPConnect has no tags. Once bound, do not accept unowned PCM."""
    expected = expected if valid_media_epoch(expected) else ''
    if 'mediaEpoch' not in data:
        return not expected
    return bool(expected) and data['mediaEpoch'] == expected


@dataclass(frozen=True)
class CapturedPCM:
    samples: object
    epoch: str


@dataclass(frozen=True)
class PlaybackPCM:
    pcm: bytes
    rate: int
    epoch: str

    def __iter__(self):
        yield self.pcm
        yield self.rate


def retire_media_epoch(window, epoch):
    if valid_media_epoch(epoch):
        with _lock:
            epochs = getattr(window, '_retired_media_epochs', ())
            if not isinstance(epochs, tuple):
                epochs = ()
            window._retired_media_epochs = tuple(e for e in epochs if e != epoch)[-49:] + (epoch,)


def is_retired_media_epoch(window, epoch):
    epochs = getattr(window, '_retired_media_epochs', ())
    return valid_media_epoch(epoch) and isinstance(epochs, tuple) and epoch in epochs
