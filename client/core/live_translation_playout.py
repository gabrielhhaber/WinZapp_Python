"""PCM playback buffer for Gemini audio arriving in irregular bursts."""


class TranslationPlayout:
    # A little startup delay gives the next burst time to arrive. A short
    # utterance or final partial frame still plays after an idle interval.
    PREBUFFER_SECONDS = 0.32
    TAIL_SECONDS = 0.30

    def __init__(self, sample_rate, *, limit_seconds=4):
        self._prebuffer = round(sample_rate * self.PREBUFFER_SECONDS) * 2
        self._limit = sample_rate * limit_seconds * 2
        self._pcm = bytearray()
        self._last_push = 0.0
        self._playing = False

    def clear(self):
        self._pcm.clear()
        self._playing = False
        self._last_push = 0.0

    def push(self, pcm, now):
        if pcm:
            self._last_push = now
            self._pcm.extend(pcm)
            excess = len(self._pcm) - self._limit
            if excess > 0:
                del self._pcm[:excess]

    def take(self, size, now):
        available = len(self._pcm)
        if not available:
            self._playing = False
            return None
        tail = now - self._last_push >= self.TAIL_SECONDS
        if not self._playing and available < self._prebuffer and not tail:
            return None
        if available < size and not tail:
            self._playing = False
            return None
        audio = bytes(self._pcm[:size])
        del self._pcm[:size]
        self._playing = available >= size
        return audio.ljust(size, b"\0")
