"""Optional FIR binding against a fake C boundary and the compiled package in CI."""
import ctypes
from types import SimpleNamespace

import pytest

from call_audio_pcm import PCM48To16
from test_call_pcm48_bindings import binding


def test_native_converter_binding_has_bounded_input_and_exact_output(monkeypatch):
    seen = []
    def native(window, size, buffer, capacity):
        seen.append((window, size, capacity))
        output = PCM48To16._convert(window)
        ctypes.memmove(buffer, output, len(output))
        return len(output)
    module = binding(monkeypatch, SimpleNamespace(CallResamplePCM48To16=native))
    window = bytes(192) + b"\1\0" * 960
    assert module.Calls.resample_pcm48_to16(window) == PCM48To16().push(window[192:])
    assert seen == [(window, 2112, 640)]


@pytest.mark.parametrize("size", [0, 191, 193, 194, 9798])
def test_binding_rejects_invalid_window_before_crossing_the_dll(monkeypatch, size):
    module = binding(monkeypatch, SimpleNamespace(
        CallResamplePCM48To16=lambda *a: pytest.fail("invalid C call")))
    with pytest.raises(ValueError):
        module.Calls.resample_pcm48_to16(bytes(size))


@pytest.mark.parametrize("returned", [-1, 0, 2, 641])
def test_binding_rejects_native_failures_and_wrong_byte_counts(monkeypatch, returned):
    module = binding(monkeypatch, SimpleNamespace(CallResamplePCM48To16=lambda *a: returned))
    with pytest.raises(module.CallError):
        module.Calls.resample_pcm48_to16(bytes(2112))


def test_older_dll_requests_python_fallback(monkeypatch):
    module = binding(monkeypatch, SimpleNamespace())
    assert module.Calls.resample_pcm48_to16(bytes(2112)) is None


@pytest.mark.parametrize("split", [1920, 2048, 9600])
def test_real_compiled_converter_matches_python_without_a_client(split):
    layer = pytest.importorskip("neonize.calls")
    native = getattr(layer.Calls, "resample_pcm48_to16", None)
    if native is None:
        pytest.skip("installed package predates native FIR")
    source = b"".join(((i*7919) % 65536 - 32768).to_bytes(2, "little", signed=True)
                      for i in range(4800))
    output = native(bytes(192) + source)
    if output is None:
        pytest.skip("installed DLL predates native FIR")
    assert output == PCM48To16().push(source)
    converter = PCM48To16(native)
    chunked = b"".join(converter.push(source[i:i+split]) for i in range(0, len(source), split))
    assert chunked == output
    converter.clear()
    assert converter.push(bytes(1920)) == bytes(640)
