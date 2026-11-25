"""Compiled NetEq C ABI, without importing the client or opening a device."""
import ctypes as c
import math
from pathlib import Path

import pytest


@pytest.fixture
def component():
    path=Path(__file__).parent/'neonize_build/dist/neonize/winzapp_call_neteq.dll'
    if not path.is_file():
        pytest.skip('build_neonize.py all prepares the NetEq component')
    lib=c.CDLL(str(path.resolve()),winmode=0x1100)
    lib.winzapp_neteq_abi.restype=c.c_uint32
    lib.winzapp_neteq_create.argtypes=[c.c_uint32]
    lib.winzapp_neteq_create.restype=c.c_void_p
    lib.winzapp_neteq_insert.argtypes=[c.c_void_p,c.c_uint32,c.c_void_p,c.c_uint32]
    lib.winzapp_neteq_insert.restype=c.c_int
    lib.winzapp_neteq_render.argtypes=[c.c_void_p,c.c_void_p,c.c_uint32,c.c_uint32]
    lib.winzapp_neteq_render.restype=c.c_int
    lib.winzapp_neteq_stats.argtypes=[c.c_void_p,c.POINTER(c.c_uint64)]
    lib.winzapp_neteq_stats.restype=c.c_int
    lib.winzapp_neteq_destroy.argtypes=[c.c_void_p]
    lib.winzapp_neteq_destroy.restype=None
    return lib


def test_compiled_neteq_abi_and_bad_rates(component):
    lib=component
    assert lib.winzapp_neteq_abi()==1
    assert not lib.winzapp_neteq_create(44100)
    assert lib.winzapp_neteq_insert(None,0,None,960)==-1
    assert lib.winzapp_neteq_render(None,None,160,1)==-1


@pytest.mark.parametrize('rate',[16000,48000])
def test_compiled_loss_and_burst_use_real_native_processing(component,rate):
    lib=component
    handle=lib.winzapp_neteq_create(rate)
    assert handle
    count=rate//100
    output=(c.c_float*(count*2))()
    stats=(c.c_uint64*4)()
    next_ts=0
    audible=False
    try:
        for tick in range(500):
            if tick%6==0:
                packets=6 if tick==120 else 1
                for _ in range(packets):
                    pcm=(c.c_float*(count*6))(*(.3*math.sin((next_ts+i)*2*math.pi*440/rate) for i in range(count*6)))
                    if not 240<=tick<264:
                        assert lib.winzapp_neteq_insert(handle,next_ts,pcm,len(pcm))==0
                    next_ts+=count*6
            output[:]=[.75]*len(output)
            assert lib.winzapp_neteq_render(handle,output,count,2)==0
            assert all(output[i]==.75 for i in range(1,len(output),2))
            audible |= any(abs(output[i])>.01 for i in range(0,len(output),2))
        assert audible
        assert lib.winzapp_neteq_stats(handle,stats)==0
        assert stats[0]>0 and stats[1]>0 # concealed and accelerated samples
    finally:
        lib.winzapp_neteq_destroy(handle)
