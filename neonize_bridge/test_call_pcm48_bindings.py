"""Load calls.py against a fake ctypes target, never the installed DLL."""
import ctypes
import importlib.util
import json
from pathlib import Path
import struct
import sys
from types import ModuleType, SimpleNamespace
import pytest


def binding(monkeypatch, native):
    package = ModuleType("pcm48_binding_test")
    package.__path__ = []
    monkeypatch.setitem(sys.modules, package.__name__, package)
    fake_binder = ModuleType(package.__name__ + "._binder")
    fake_binder.gocode = native
    monkeypatch.setitem(sys.modules, fake_binder.__name__, fake_binder)
    path = Path(__file__).parent / 'neonize_build/neonize/neonize/calls.py'
    spec = importlib.util.spec_from_file_location(package.__name__ + '.calls', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_native_transport_binding_keeps_generation_and_stream(monkeypatch):
    opened, closed = [], []
    descriptor = {'protocol': 1, 'port': 1234, 'secret': 'ab'*32,
                  'sampleRate': 48000, 'generation': 7}
    def open_transport(uuid, call_id, generation, stream, buffer, capacity):
        opened.append((uuid, call_id, generation, stream))
        data = json.dumps(descriptor).encode()
        ctypes.memmove(buffer, data, len(data))
        return len(data)
    native = SimpleNamespace(CallAudioTransportOpen=open_transport,
                             CallAudioTransportClose=lambda *a: closed.append(a))
    calls = binding(monkeypatch, native).Calls(b'account')
    assert calls.open_audio_transport('C', 7, 123) == descriptor
    calls.close_audio_transport('C', 7, 123)
    assert opened == closed == [(b'account', b'C', 7, 123)]
    for generation, stream in [(0,123), (7,True), (7,2**53)]:
        with pytest.raises(ValueError):
            calls.open_audio_transport('C', generation, stream)


def test_native_transport_missing_or_failed_binding(monkeypatch):
    calls = binding(monkeypatch, SimpleNamespace()).Calls('account')
    assert calls.open_audio_transport('C', 7, 123) is None
    calls.close_audio_transport('C', 7, 123)
    native = SimpleNamespace(CallAudioTransportOpen=lambda *a: -2)
    module = binding(monkeypatch, native)
    with pytest.raises(module.CallError):
        module.Calls('account').open_audio_transport('C', 7, 123)


def test_native_timed_binding_selects_new_export_but_accepts_old_dll(monkeypatch):
    selected=[]
    def open_protocol(protocol):
        def call(uuid, call_id, generation, stream, buffer, capacity):
            selected.append(protocol)
            data=json.dumps({'protocol':protocol}).encode()
            ctypes.memmove(buffer,data,len(data))
            return len(data)
        return call
    old=SimpleNamespace(CallAudioTransportOpen=open_protocol(1))
    assert binding(monkeypatch,old).Calls(b'account').open_audio_transport('C',7,123,protocol=2)=={'protocol':1}
    old.CallAudioTransportOpenV2=open_protocol(2)
    assert binding(monkeypatch,old).Calls(b'account').open_audio_transport('C',7,124,protocol=2)=={'protocol':2}
    assert selected==[1,2]


class Native:
    def __init__(self):
        self.configured = []
        self.packet = struct.pack('<IQ', 48000, 17) + b'\x01\x02' * 2880
        self.writes = []

    def CallsConfigure(self, flag):
        self.configured.append(flag)

    def CallsConfigurePCM48(self, flag):
        self.configured.append(('pcm48', flag))

    def CallAudioFormat(self, uuid, call_id, buffer, capacity):
        data = json.dumps({'sampleRate': 48000, 'generation': 17}).encode()
        ctypes.memmove(buffer, data, len(data))
        return len(data)

    def CallReadAudioFrame(self, uuid, call_id, buffer, capacity, timeout):
        ctypes.memmove(buffer, self.packet, min(len(self.packet), capacity))
        return len(self.packet)

    def CallWriteAudioFrame(self, uuid, call_id, rate, generation, pcm, count):
        self.writes.append((rate, generation, pcm, count))
        return 0 if generation == 17 else -4


def test_binding_v2_pairs_format_with_pcm_and_validates_write(monkeypatch):
    native = Native()
    module = binding(monkeypatch, native)
    calls = module.Calls(b'test')
    assert calls.audio_format('C') == {'sampleRate': 48000, 'generation': 17}
    packet = calls.read_audio_frame('C')
    assert packet == {'sampleRate': 48000, 'generation': 17, 'pcm': b'\x01\x02' * 2880}
    assert calls.write_audio_frame('C', packet['pcm'], 48000, 17)
    assert not calls.write_audio_frame('C', packet['pcm'], 48000, 16)
    with pytest.raises(ValueError):
        calls.write_audio_frame('C', b'\x00' * 1920, 48000, 17)
    module.configure(True)
    assert native.configured[-1] == ('pcm48', 0)


@pytest.mark.parametrize('packet', [b'x', struct.pack('<IQ', 44100, 17)+b'xx',
                                   struct.pack('<IQ', 48000, 0)+b'xx',
                                   struct.pack('<IQ', 48000, 17)+b'x',
                                   struct.pack('<IQ', 16000, 17)+b'x'*5760])
def test_binding_refuses_malformed_native_frames(monkeypatch, packet):
    native = Native()
    native.packet = packet
    module = binding(monkeypatch, native)
    with pytest.raises(module.CallError):
        module.Calls(b'test').read_audio_frame('C')


def test_older_dll_keeps_16k_read_write_and_rejects_pcm48_before_configuring(monkeypatch):
    writes = []
    def read(uuid, call_id, buffer, capacity, timeout):
        ctypes.memmove(buffer, b'\x03\x04'*960, 1920)
        return 1920
    native = SimpleNamespace(CallsConfigure=lambda value: writes.append(value), CallReadAudio=read,
                             CallWriteAudio=lambda *args: 0)
    module = binding(monkeypatch, native)
    calls = module.Calls(b'test')
    assert calls.audio_format('C') == {'sampleRate': 16000, 'generation': 0}
    assert calls.read_audio_frame('C')['pcm'] == b'\x03\x04'*960
    assert calls.write_audio_frame('C', b'\x00'*1920, 16000, 0)
    assert not calls.write_audio_frame('C', b'\x00'*5760, 48000, 17)
    with pytest.raises(module.CallError):
        module.configure(True, pcm48=True)
    assert writes == []


def test_the_real_bindings_pcm48_exports_without_a_client():
    """Compiled-package CI contract; excluded from the local fake-only runner."""
    layer = pytest.importorskip('neonize.calls')
    if not callable(getattr(layer, 'pcm48_available', None)) or not layer.pcm48_available():
        pytest.skip('compiled PCM48 native exports are not present')
    calls = layer.Calls(b'pcm48-no-client')
    assert calls.audio_format('missing') is None
    assert calls.read_audio_frame('missing', 0) is None
    assert not calls.write_audio_frame('missing', b'\x00' * 5760, 48000, 17)
