"""Ownership reaches the actual client queue/socket; no UI or native imports."""
import ast
import base64
import logging
from pathlib import Path
from types import SimpleNamespace as N
import types

import pytest

from core.call_media_ownership import accepts_media_epoch, media_fields, next_stream_epoch

A, B = 'a' * 32, 'b' * 32


def socket_methods(*names):
    source = Path(__file__).parents[1] / 'client/core/websocket_client.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'WebSocketClient')
    nodes = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in names]
    nodes += [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_socketio_binary_to_bytes']
    scope = {'base64': base64, 'logging': logging, 'accepts_media_epoch': accepts_media_epoch,
             'media_fields': media_fields, 'next_stream_epoch': next_stream_epoch}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), 'exec'), scope)
    return scope


@pytest.mark.parametrize('name,key', [('on_call_audio_remote', 'pcm'), ('on_call_video_remote', 'jpeg')])
def test_late_remote_from_other_call_is_not_forwarded(name, key):
    scope = socket_methods(name)
    forwarded = []
    main = N(_active_voice_call={'media_epoch': B},
             on_call_remote_audio=lambda *a, **k: forwarded.append(a),
             on_call_remote_video=lambda *a, **k: forwarded.append(a))
    ws = N(main_window=main, _belongs_to_this_session=lambda data: True,
           _call_remote_video_frames_received=0)
    scope[name](ws, {key: base64.b64encode(b'synthetic').decode(), 'mediaEpoch': A})
    assert not forwarded
    scope[name](ws, {key: base64.b64encode(b'synthetic').decode(), 'mediaEpoch': B})
    assert len(forwarded) == 1


def test_audio_session_rechecks_owner_after_socket_handler():
    from core.call_audio import CallAudioConfig, CallAudioSession
    session = CallAudioSession(N(emit=lambda *a: None), CallAudioConfig('fake'))
    session.set_media_epoch(B)
    session.enqueue_remote_audio(b'\0\0', 48000, media_epoch=A)
    session.enqueue_remote_audio(b'\0\0', 48000)
    assert session._output_queue.empty()
    session.enqueue_remote_audio(b'\0\0', 48000, media_epoch=B)
    assert session._output_queue.qsize() == 1


def test_legacy_remote_is_accepted_only_without_an_owned_protocol():
    assert accepts_media_epoch('', {})
    assert not accepts_media_epoch(A, {})
    assert not accepts_media_epoch('', {'mediaEpoch': A})


def test_helper_microphone_and_stop_carry_same_call_and_stream():
    scope = socket_methods('send_call_microphone_audio', 'stop_call_audio_stream')
    sent = []
    ws = N(main_window=N(_active_voice_call={'media_epoch': A}), instance_name='test',
           sio=N(emit=lambda *a: sent.append(a)))
    scope['send_call_microphone_audio'](ws, b'\1\0')
    scope['stop_call_audio_stream'](ws)
    mic, stop = sent
    assert mic[1]['mediaEpoch'] == stop[1]['mediaEpoch'] == A
    assert mic[1]['streamEpoch'] == stop[1]['streamEpoch'] > 0
    scope['send_call_microphone_audio'](ws, b'\2\0')
    assert sent[-1][1]['streamEpoch'] > mic[1]['streamEpoch']


def test_ambiguous_wasapi_prefix_cannot_choose_an_unrelated_endpoint():
    from core.call_audio import CallAudioConfig, CallAudioSession
    session = CallAudioSession(N(), CallAudioConfig('fake'))
    prefix = 'Headset Earphone (shared-prefix'
    devices = [{'name': prefix, 'hostapi': 0, 'max_output_channels': 1},
               {'name': prefix + ' one)', 'hostapi': 1, 'max_output_channels': 1},
               {'name': prefix + ' two)', 'hostapi': 1, 'max_output_channels': 1}]
    session._sd = N(query_devices=lambda index=None: devices if index is None else devices[index],
                    query_hostapis=lambda index: {'name': 'MME' if index == 0 else 'Windows WASAPI'})
    assert session._wasapi_twin(0, input_device=False) is None
    devices.append({'name': prefix, 'hostapi': 1, 'max_output_channels': 1})
    assert session._wasapi_twin(0, input_device=False) == 3
