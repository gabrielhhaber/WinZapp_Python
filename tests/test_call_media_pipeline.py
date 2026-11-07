"""Actual Python sender/bridge/receiver, synthetic PCM and fake native queue.

This is deliberately NOT a codec, socket/network, hardware or audible-call test.
"""
import ast
import base64
import importlib
from pathlib import Path
import types
from types import SimpleNamespace as N

import numpy as np
import pytest

from core.call_audio import CallAudioConfig, CallAudioSession
from tests.test_call_media_epoch import socket_methods

TOKEN = 'a' * 32


@pytest.fixture
def bridge_media(monkeypatch):
    # Root CI supplies only client/ on pythonpath. Import the bridge during
    # this test's fixture, restoring its path afterwards; collection must not
    # depend on a developer runner or a different shard importing it first.
    bridge_dir = Path(__file__).resolve().parents[1] / 'neonize_bridge'
    monkeypatch.syspath_prepend(str(bridge_dir))
    module = importlib.import_module('call_media')
    assert Path(module.__file__).resolve() == bridge_dir / 'call_media.py'
    return module


@pytest.mark.parametrize('native_rate', [16000, 48000])
@pytest.mark.parametrize('muted', [False, True])
def test_generated_capture_crosses_owned_python_pipeline(native_rate, muted, bridge_media):
    captured, writes, remote = [], [], []
    def send(event, data):
        if event == 'call:audio:mic':
            captured.append(data)
            if len(captured) == 3:
                sender._stop_event.set()
    sender = CallAudioSession(N(emit=send), CallAudioConfig('test'))
    sender.set_media_epoch(TOKEN)
    sender.set_microphone_muted(muted)
    sender._input_rate = 48000
    for n in range(3):
        t = (np.arange(960) + n * 960) / 48000
        sender._mic_queue.put((.2 * np.sin(2 * np.pi * 440 * t)).astype(np.float32))
    sender._send_microphone_loop()
    assert len(captured) == 3 and all(p['mediaEpoch'] == TOKEN for p in captured)
    native = N(audio_format=lambda cid: {'sampleRate': native_rate, 'generation': 0},
               write_audio_frame=lambda *a: writes.append(a) or True)
    media = bridge_media.CallMedia('test', native, lambda *a: remote.append(a))
    record = {'id': 'synthetic', 'peer': 'fake', 'state': 'ACTIVE', 'connected': True,
              'outgoing': False, 'media_epoch': TOKEN}
    media._calls['synthetic'] = record
    for payload in captured:
        media.microphone(payload)
    assert len(writes) == 1 and len(writes[0][1]) == native_rate * 60 // 1000 * 2
    pcm = writes[0][1]
    assert (not any(pcm)) is muted

    # Fake the missing native codec/network stage by feeding its queued PCM
    # to the real bridge output method; no claimed MLow round trip here.
    claim = (object(), record)
    media._audio_readers['synthetic'] = claim
    media._emit_audio('synthetic', claim, pcm, native_rate, 0)
    receiver = CallAudioSession(N(emit=lambda *a: None), CallAudioConfig('test'))
    receiver.set_media_epoch(TOKEN)
    tree = ast.parse((Path(__file__).parents[1] / 'client/main_window/calls.py').read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'CallsMixin')
    fn = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'on_call_remote_audio')
    scope = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), '<real-main-audio-method>', 'exec'), scope)
    main = N(_active_voice_call={'media_epoch': TOKEN}, _call_audio_session=receiver)
    main.on_call_remote_audio = types.MethodType(scope['on_call_remote_audio'], main)
    ws = N(main_window=main, _belongs_to_this_session=lambda d: True)
    handler = socket_methods('on_call_audio_remote')['on_call_audio_remote']
    handler(ws, remote[0][1])
    queued_pcm, rate = receiver._output_queue.get_nowait()
    assert queued_pcm == pcm and rate == native_rate
    # Delayed A after adopting B must not be pushed into B's reservoir.
    main._active_voice_call = {'media_epoch': 'b' * 32}
    receiver.set_media_epoch('b' * 32)
    handler(ws, remote[0][1])
    assert receiver._output_queue.empty()
    media.close()
