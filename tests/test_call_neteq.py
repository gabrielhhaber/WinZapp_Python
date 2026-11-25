"""Real stream ownership and timeline/fallback with a fake native NetEq ABI."""
from types import SimpleNamespace

import numpy as np
import pytest

from core.call_audio import _OutputJitterBuffer
from core.call_neteq_buffer import NetEqOutputBuffer


class Native:
    def __init__(self):
        self.packets = []
        self.closed = False
        self.fail = False

    def insert(self, ts, samples):
        self.packets.append((ts, samples.copy()))

    def render(self, out):
        out[:] = .25
        return not self.fail

    def close(self):
        self.closed = True


def buffer(rate=48000, available=True):
    native = []
    def create(rate):
        result = Native()
        native.append(result)
        return result
    legacy = _OutputJitterBuffer(rate, neonize=True)
    result = NetEqOutputBuffer(legacy, rate,
                              library_loader=lambda: SimpleNamespace(create=create) if available else None)
    return result, native, legacy


def test_native_owns_the_reservoir_and_uses_source_timestamps():
    b, native, legacy = buffer()
    pcm = np.full(2880,.2,dtype=np.float32)
    b.push_timed(pcm, 1000,16000,42,16000)
    b.push_timed(pcm, 2920,16000,42,16000) # missing 60 ms: do not compress the gap
    assert [p[0] for p in native[0].packets] == [0,5760]
    assert legacy.buffered_samples == 0
    out = np.empty(137,dtype=np.float32)
    assert b.fill(out) and np.all(out == .25) # no second 120 ms prefill


@pytest.mark.parametrize('source', [(16000,43,16000),(16000,42,48000),(48000,42,16000)])
def test_new_source_or_codec_clock_discards_old_neteq(source):
    b, native, _ = buffer()
    b.push_timed(np.zeros(2880),1000,16000,42,16000)
    b.push_timed(np.zeros(2880),0,*source)
    assert len(native) == 2 and native[0].closed
    assert native[1].packets[0][0] == 0


def test_rtp_wrap_retains_source_epoch_and_exact_clock_conversion():
    b, native, _ = buffer(48000)
    b.push_timed(np.zeros(2880),2**32-960,16000,42,16000)
    b.push_timed(np.zeros(2880),0,16000,42,16000)
    assert len(native) == 1
    assert [p[0] for p in native[0].packets] == [0,2880]


def test_retired_output_callback_cannot_tick_replacement_neteq():
    b, native, _ = buffer()
    old,new=object(),object()
    b.set_owner(old)
    b.push_timed(np.zeros(2880),0,16000,42,16000)
    b.set_owner(new)
    out=np.ones(137,dtype=np.float32)
    assert not b.fill(out,owner=old) and not np.any(out)
    assert b.fill(out,owner=new) and np.all(out==.25)


def test_device_rate_reset_closes_handle_and_refuses_unsupported_rate():
    b,native,_=buffer()
    b.push_timed(np.zeros(2880),0,16000,42,16000)
    b.configure(44100)
    assert native[0].closed and not b.native_available
    b.push_timed(np.zeros(2646),960,16000,42,16000)
    assert len(native)==1


def test_missing_component_and_group_pcm_keep_legacy_reservoir():
    b,_,legacy=buffer(available=False)
    b.push_timed(np.zeros(2880),0,16000,42,16000)
    assert legacy.buffered_samples==2880
    b,native,legacy=buffer()
    b.push_timed(np.zeros(2880),0,16000,42,16000)
    b.push(np.zeros(2880))
    assert native[0].closed and legacy.buffered_samples==2880


def test_callback_failure_defers_handle_cleanup_to_worker():
    b,native,legacy=buffer()
    b.push_timed(np.zeros(2880),0,16000,42,16000)
    native[0].fail=True
    out=np.ones(137,dtype=np.float32)
    assert b.fill(out) and not np.any(out)
    assert b.native_failed and not native[0].closed
    b.release_failed()
    assert native[0].closed
    b.push_timed(np.zeros(2880),960,16000,42,16000)
    assert legacy.buffered_samples==2880


def test_compiled_python_binding_with_multichannel_view():
    from pathlib import Path
    from core.call_neteq import NetEqLibrary
    dll=Path(__file__).parents[1]/'neonize_bridge/neonize_build/dist/neonize/winzapp_call_neteq.dll'
    if not dll.is_file():
        pytest.skip('NetEq component is built by the Neonize job')
    instance=NetEqLibrary(dll).create(48000)
    try:
        pcm=np.sin(np.arange(2880,dtype=np.float32)*.0576)*.2
        instance.insert(0,pcm)
        out=np.full((137,8),.75,dtype=np.float32)
        assert instance.render(out[:,0])
        assert np.all(out[:,1:]==.75) and np.any(out[:,0]!=.75)
        assert len(instance.stats())==4
        assert instance.render(np.empty(0,dtype=np.float32))
        assert not instance.render(np.empty(137,dtype=np.float64))
    finally:
        instance.close()
