"""Real unbound call methods against plain stubs: no wx imports or devices."""
import ast
import logging
from pathlib import Path
import threading
import types

import pytest
from core.call_media_ownership import valid_media_epoch, retire_media_epoch, is_retired_media_epoch


def methods(*names, thread_module=threading, wx_module=None):
    source = Path(__file__).parents[1] / 'client/main_window/calls.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'CallsMixin')
    selected = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert len(selected) == len(names)
    scope = {'logging': logging, 'threading': thread_module,
             'valid_media_epoch': valid_media_epoch, 'retire_media_epoch': retire_media_epoch,
             'is_retired_media_epoch': is_retired_media_epoch,
             'wx': wx_module or types.SimpleNamespace(CallAfter=lambda f, *a, **k: f(*a, **k))}
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(source), 'exec'), scope)
    return {name: scope[name] for name in names}


class Audio:
    def __init__(self, start=lambda: None):
        self._start = start
        self.stopped = []
        self.microphone_muted = False

    def start(self):
        self._start()

    def stop(self, **kwargs):
        self.stopped.append(kwargs)

    def set_microphone_muted(self, value):
        self.microphone_muted = value


def window():
    owner = {'identity': 'A', 'call_id': 'A', 'is_video': False}
    w = types.SimpleNamespace(_active_voice_call=owner, _call_audio_session=None,
                              _call_ring_audio_session=None,
                              _call_action_lock=threading.Lock(),
                              _voice_call_last_announced_state='ACTIVE', token='test',
                              _sync_voice_call_bar=lambda: None,
                              _raise_for_call_response=lambda r, a: r,
                              _post_call_control=lambda *a, **k: True)
    for name, fn in methods('_start_voice_call_audio', '_attach_audio_to_browser_call').items():
        setattr(w, name, types.MethodType(fn, w))
    return w, owner


@pytest.mark.parametrize('replacement', [None, {'call_id': 'B'}, {'call_id': 'A'}])
def test_delayed_attach_does_not_start_after_end_or_replacement(replacement):
    w, owner = window()
    started = []
    w._start_voice_call_audio = lambda *a, **k: started.append(a)
    def post(*a, **k):
        w._active_voice_call = replacement
        return True
    w._post_call_control = post
    w._attach_audio_to_browser_call(owner)
    assert not started
    assert w._active_voice_call is replacement


def test_attach_rechecks_after_camera_start_before_post():
    w, owner = window()
    owner['is_video'] = True
    replacement = {'call_id': 'B'}
    posted, started = [], []
    w._start_call_camera = lambda **kw: setattr(w, '_active_voice_call', replacement)
    w._post_call_control = lambda *a, **k: posted.append(a)
    w._start_voice_call_audio = lambda *a, **k: started.append(a)
    w._attach_audio_to_browser_call(owner)
    assert not posted and not started
    assert w._active_voice_call is replacement


@pytest.mark.parametrize('replacement', [None, {'call_id': 'B'}, {'call_id': 'A'}])
def test_device_open_cannot_attach_to_a_replacement_record(replacement):
    w, owner = window()
    audio = Audio(lambda: setattr(w, '_active_voice_call', replacement))
    w._build_call_audio_session = lambda: (audio, 'test')
    assert w._start_voice_call_audio('A', owner, keep_active_call=True) is False
    assert w._call_audio_session is None
    assert w._active_voice_call is replacement
    assert audio.stopped == [{'notify_bridge': False}]


def test_attaching_current_call_preserves_record_and_connected_state():
    w, owner = window()
    audio = Audio()
    w._build_call_audio_session = lambda: (audio, 'test')
    w._attach_audio_to_browser_call(owner)
    assert w._active_voice_call is owner
    assert w._call_audio_session is audio
    assert w._voice_call_last_announced_state == 'ACTIVE'


def test_queued_device_restart_cannot_stop_a_later_call():
    queued = []
    fake_threads = types.SimpleNamespace(Thread=lambda **k: types.SimpleNamespace(start=lambda: queued.append(k['target'])))
    fn = methods('_restart_active_voice_call_audio', thread_module=fake_threads)['_restart_active_voice_call_audio']
    w, _ = window()
    w._call_audio_session = Audio()
    stopped, started = [], []
    w._stop_voice_call_audio = lambda **k: stopped.append(k)
    w._start_voice_call_audio = lambda *a, **k: started.append(a)
    fn(w)
    replacement = w._active_voice_call = {'call_id': 'B'}
    queued.pop()()
    assert not stopped and not started
    assert w._active_voice_call is replacement


def test_initial_audio_start_preserves_state_event_that_arrived_during_open():
    w, owner = window()
    epoch = 'a' * 32
    def arrived():
        owner.update(call_id='native-id', media_epoch=epoch, is_video=True)
    audio = Audio(arrived)
    bound = []
    audio.set_media_epoch = bound.append
    w._build_call_audio_session = lambda: (audio, 'test')
    assert w._start_voice_call_audio('A', dict(owner))
    assert w._active_voice_call is owner
    assert owner['call_id'] == 'native-id' and owner['is_video']
    assert bound[-1] == epoch


@pytest.mark.parametrize('replacement', [None, {'call_id': 'B'}])
def test_initial_audio_open_also_refuses_lost_owner(replacement):
    w, owner = window()
    audio = Audio(lambda: setattr(w, '_active_voice_call', replacement))
    w._build_call_audio_session = lambda: (audio, 'test')
    assert w._start_voice_call_audio('A', owner) is False
    assert w._active_voice_call is replacement and audio.stopped


def nested_worker(name, **scope):
    source = Path(__file__).parents[1] / 'client/main_window/calls.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'CallsMixin')
    outer = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == name)
    worker = next(n for n in outer.body if isinstance(n, ast.FunctionDef) and n.name == '_worker')
    namespace = {'logging': logging, **scope}
    exec(compile(ast.Module(body=[worker], type_ignores=[]), str(source), 'exec'), namespace)
    return namespace['_worker']


@pytest.mark.parametrize('entry', ['accept_incoming_call', '_start_call', 'promote_call_to_video'])
@pytest.mark.parametrize('replacement', [None, {'call_id': 'B'}, {'call_id': 'A'}])
def test_old_queued_call_action_cannot_start_or_post_for_a_replacement(entry, replacement):
    w, owner = window()
    started, posted = [], []
    w._active_voice_call = replacement
    w._start_voice_call_audio = lambda *a, **k: started.append(a)
    w._post_call_control = lambda *a, **k: posted.append(a)
    abandon = methods('_abandon_outgoing_call')['_abandon_outgoing_call']
    w._abandon_outgoing_call = types.MethodType(abandon, w)
    attempt = {'cancelled': False}
    newer_attempt = object()
    w._outgoing_call_attempt = w._promote_attempt = newer_attempt
    w._call_upgrade_pending = True
    worker = nested_worker(entry, self=w, expected_call=owner, initial_record=owner,
                           attempt=attempt, identity='A', details=owner)
    worker()
    assert not started and not posted
    assert w._active_voice_call is replacement
    assert w._outgoing_call_attempt is w._promote_attempt is newer_attempt
    assert w._call_upgrade_pending is True


def test_camera_job_checks_its_original_call_before_discovery_or_flag_changes():
    w, owner = window()
    w._active_voice_call = {'call_id': 'B'}
    w._call_camera_available = True
    start = methods('_start_call_camera')['_start_call_camera']
    assert start(w, expected_call=owner) is False
    assert w._call_camera_available is True


def test_video_queued_for_ui_cannot_be_rendered_in_a_replacement_call():
    queued, rendered = [], []
    wx_stub = types.SimpleNamespace(CallAfter=lambda f, *a, **k: queued.append((f, a, k)),
                                    Image=lambda *a: rendered.append(a))
    selected = methods('on_call_remote_video', '_show_call_remote_video', wx_module=wx_stub)
    owner = {'call_id': 'A', 'is_video': True, 'media_epoch': 'a' * 32}
    w = types.SimpleNamespace(_active_voice_call=owner)
    w._show_call_remote_video = types.MethodType(selected['_show_call_remote_video'], w)
    selected['on_call_remote_video'](w, b'fake-jpeg', media_epoch='a' * 32)
    w._active_voice_call = {'call_id': 'B', 'is_video': True, 'media_epoch': 'b' * 32}
    fn, args, kwargs = queued.pop()
    fn(*args, **kwargs)
    assert not rendered
