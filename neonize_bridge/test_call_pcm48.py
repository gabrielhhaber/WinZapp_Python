"""PCM48 bridge contracts against plain fakes; no threads/devices/bindings."""
import base64
import pytest
import call_media as cm
from call_audio_mode import configure_call_audio


class NativeFrames:
    def __init__(self):
        self.generation = 7
        self.written = []
        self.remote = []

    def audio_format(self, call_id):
        return {"sampleRate": 48000, "generation": self.generation}

    def write_audio_frame(self, call_id, frame, rate, generation):
        if generation != self.generation:
            return False
        self.written.append((frame, rate, generation))
        return True

    def read_audio_frame(self, call_id, timeout):
        return self.remote.pop(0) if self.remote else None


def connected(native=None):
    native = native or NativeFrames()
    sent = []
    media = cm.CallMedia("test", native, lambda event, data: sent.append((event, data)))
    media._calls["C"] = {"id": "C", "peer": "test", "state": "ACTIVE", "connected": True,
                           "outgoing": True, "native_generation": native.generation}
    media._start_audio = lambda *args: None
    return media, native, sent


def capture(media, pcm):
    media.microphone({"sampleRate": 48000, "pcm": base64.b64encode(pcm).decode()})


def test_pcm48_assembler_preserves_every_microphone_byte():
    assembler = cm.FrameAssembler(48000)
    pcm = bytes(range(256)) * 22 + b'x' * 128  # exactly 5760 bytes
    assert len(pcm) == 5760
    assert assembler.push(pcm[:1920]) == []
    assert assembler.push(pcm[1920:3840]) == []
    assert assembler.push(pcm[3840:]) == [pcm]
    with pytest.raises(ValueError):
        cm.FrameAssembler(44100)


def test_pcm48_microphone_passes_original_pcm_and_generation():
    media, native, _ = connected()
    pcm = b'\x11\x22' * 2880
    for i in range(3):
        capture(media, pcm[i*1920:(i+1)*1920])
    assert native.written == [(pcm, 48000, 7)]


def test_remote_format_and_stale_generation_are_not_guessed_from_size():
    media, native, sent = connected()
    # 1920 bytes can be PCM48/20ms or PCM16/60ms. Metadata owns its meaning.
    pcm = b'\x01\x02' * 960
    native.remote = [{"sampleRate": 48000, "generation": 6, "pcm": pcm},
                     {"sampleRate": 16000, "generation": 7, "pcm": pcm},
                     {"sampleRate": 48000, "generation": 7, "pcm": pcm}, None]
    media._ensure_audio("C")
    claim = media._audio_readers["C"]
    media._pump_audio("C", claim)
    audio = [data for event, data in sent if event == "call:audio:remote"]
    assert len(audio) == 1 and audio[0]["sampleRate"] == 48000
    assert base64.b64decode(audio[0]["pcm"]) == pcm


def test_same_id_replacement_discards_partial_capture_and_stale_end():
    media, native, _ = connected()
    capture(media, b'\x01\x02' * 1920)  # old owner's 40ms, not enough to send
    old = media._calls["C"]
    media.handle({"ev": "end", "call": "C", "generation": 6})
    assert media._calls["C"] is old
    native.generation = 8
    media.handle({"ev": "incoming", "call": "C", "generation": 8})
    assert media._calls["C"] is not old
    media._calls["C"].update(state="ACTIVE", connected=True)
    capture(media, b'\x03\x04' * 960)
    assert native.written == []
    capture(media, b'\x03\x04' * 1920)
    assert native.written == [(b'\x03\x04' * 2880, 48000, 8)]


def test_invalid_native_format_never_emits_or_sends():
    media, native, sent = connected()
    native.audio_format = lambda call_id: {"sampleRate": 48000, "generation": True}
    with pytest.raises(ValueError):
        capture(media, b'\x00\x00' * 2880)
    assert not native.written and not sent


def test_native_record_replaced_before_microphone_write_cannot_receive_old_capture():
    media, native, _ = connected()
    assert media._format_for(media._calls["C"])["generation"] == 7
    native.generation = 8
    capture(media, b'\x11\x22' * 2880)
    assert native.written == [] and media._mic._pending == b''


def test_pcm48_configuration_is_off_by_default_and_capability_gated():
    class Layer:
        def __init__(self):
            self.flags = []
        def pcm48_available(self):
            return True
        def configure(self, enabled, **kwargs):
            self.flags.append((enabled, kwargs))
    layer = Layer()
    configure_call_audio(layer)
    configure_call_audio(layer, True)
    assert layer.flags == [(True, {'pcm48': False}), (True, {'pcm48': True})]
    layer.pcm48_available = lambda: False
    configure_call_audio(layer, True)
    assert layer.flags[-1] == (True, {})


def test_native_format_failure_releases_reader_without_emitting_audio():
    media, native, sent = connected()
    def failed(*args):
        raise cm.layer_error()("synthetic invalid PCM header")
    native.read_audio_frame = failed
    media._ensure_audio('C')
    media._pump_audio('C', media._audio_readers['C'])
    assert 'C' not in media._audio_readers and not sent
