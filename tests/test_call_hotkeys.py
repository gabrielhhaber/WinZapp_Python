"""Answer, reject and hang up a call from any window (global keys).

The keys are Windows hotkeys held only while they can act: "answer" while a
call that can be answered rings, "end" while one rings or a call is in
progress. Nothing here registers a real hotkey or opens a window: the manager
is replaced by a recorder and the mixin runs on a plain stub.
"""

import threading
import types

import pytest

import ipc
import main_window.call_hotkeys as call_hotkeys
import main_window.calls as calls_module
from core.call_logic import (
    CALL_ANSWER_HOTKEY,
    CALL_END_HOTKEY,
    CALL_HOTKEY_DEFAULTS,
    call_hotkey_is_valid,
    call_hotkey_setting,
    first_answerable_call,
    saved_call_hotkey,
)
from main_window.call_hotkeys import CallHotkeysMixin
from main_window.calls import CallsMixin


class _Manager:
    created = []

    def __init__(self, vk, mod, callback, *, retry_seconds=None):
        self.vk, self.mod, self.callback = vk, mod, callback
        self.retry_seconds = retry_seconds
        self.stopped = False
        _Manager.created.append(self)

    def stop(self, wait=False):
        self.stopped = True


class _I18n:
    def t(self, key):
        return f"<{key}>"


class _Button:
    def __init__(self):
        self.focused = False

    def SetFocus(self):
        self.focused = True


class _CallWindow:
    def __init__(self, shown=True):
        self.shown = shown
        self.raised = False

    def IsShown(self):
        return self.shown

    def GetHandle(self):
        return 0x1234

    def Raise(self):
        self.raised = True


class _NoThread:
    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        pass


class _RunNow:
    def __init__(self, target=None, **kwargs):
        self._target = target

    def start(self):
        self._target()


class _Window(CallHotkeysMixin):
    _has_answerable_incoming_call = CallsMixin._has_answerable_incoming_call
    _first_incoming_call_identity = CallsMixin._first_incoming_call_identity

    def __init__(self, general=None):
        self._active_incoming_calls = {}
        self._incoming_call_details = {}
        self._active_voice_call = None
        self.settings = {"general": general or {}}
        self.i18n = _I18n()
        self.accepted, self.rejected, self.spoken, self.ended = [], [], [], []
        self.voice_call_window = _CallWindow()
        self.voice_call_window_mute_button = _Button()

    def ring(self, identity, **details):
        self._active_incoming_calls[identity] = "peer"
        self._incoming_call_details[identity] = details

    def accept_incoming_call(self, identity, *, with_video=None):
        self.accepted.append((identity, with_video))

    def reject_incoming_call(self, identity):
        self.rejected.append(identity)

    def end_active_call(self, _event=None):
        self.ended.append(True)

    def output(self, text, interrupt=False):
        self.spoken.append((text, interrupt))


class _AnsweringWindow(_Window):
    """The real answer path: the alert stops, then the call is recorded."""
    _uses_neonize = lambda self: False      # these tests are about the WPPConnect engine
    accept_incoming_call = CallsMixin.accept_incoming_call
    stop_incoming_call_alert = CallsMixin.stop_incoming_call_alert
    _sync_incoming_call_bar = CallsMixin._sync_incoming_call_bar
    _call_control_payload = CallsMixin._call_control_payload

    def _cancel_incoming_call_watchdog(self, identity):
        pass

    def _close_incoming_call_dialog(self, identity):
        pass

    def _stop_incoming_call_audio_monitor(self):
        pass

    def _sync_voice_call_bar(self):
        self._request_call_hotkey_sync()


@pytest.fixture(autouse=True)
def recorder(monkeypatch):
    _Manager.created = []
    monkeypatch.setattr(call_hotkeys, "_HotkeyManager", _Manager)
    return _Manager


def _held(window):
    return {name: key for name, (_m, key) in window._call_hotkey_managers.items()}


@pytest.fixture
def queued(monkeypatch):
    """wx.CallAfter recorded instead of posted; ``_drain`` runs it."""
    calls = []
    monkeypatch.setattr(call_hotkeys.wx, "CallAfter",
                        lambda fn, *a: calls.append((fn, a)))
    return calls


def _drain(queued):
    while queued:
        fn, args = queued.pop(0)
        fn(*args)


class TestWhichCall:
    def test_the_oldest_answerable_call_is_chosen(self):
        details = {"a": {}, "b": {}}
        assert first_answerable_call({"a": "x", "b": "y"}, details) == "a"

    def test_a_group_offer_is_chosen_in_ring_order(self):
        details = {"g": {"is_group": True}, "a": {}}
        assert first_answerable_call({"g": "x", "a": "y"}, details) == "g"

    def test_only_a_group_offer_is_still_answerable(self):
        assert first_answerable_call({"g": "x"}, {"g": {"is_group": True}}) == "g"

    def test_no_call_leaves_nothing_to_answer(self):
        assert first_answerable_call({}, {}) == ""
        assert first_answerable_call(None, None) == ""


class TestTheSavedKeys:
    def test_the_shipped_keys(self):
        assert CALL_ANSWER_HOTKEY == (0x0D, 0x0007)   # Ctrl+Alt+Shift+Enter
        assert CALL_END_HOTKEY == (0x08, 0x0007)      # Ctrl+Alt+Shift+Backspace

    def test_nothing_saved_gives_the_shipped_key(self):
        assert call_hotkey_setting({}, "call_answer_hotkey") == CALL_ANSWER_HOTKEY
        assert call_hotkey_setting(None, "call_end_hotkey") == CALL_END_HOTKEY

    def test_a_saved_key_wins(self):
        general = {"call_answer_hotkey": {"vk": 0x41, "mod": 0x0003}}
        assert call_hotkey_setting(general, "call_answer_hotkey") == (0x41, 0x0003)

    def test_a_removed_key_is_none(self):
        general = {"call_end_hotkey": {"vk": 0, "mod": 0}}
        assert call_hotkey_setting(general, "call_end_hotkey") is None

    @pytest.mark.parametrize("junk", [
        None, "x", [], {}, {"vk": 13}, {"vk": "13", "mod": 7}, {"vk": True, "mod": 7},
        {"vk": 13, "mod": 0}, {"vk": 13, "mod": 4}, {"vk": 999, "mod": 7},
        {"vk": 13, "mod": 0x8000}, {"vk": 0, "mod": 7},
    ])
    def test_an_unusable_value_falls_back_to_the_shipped_key(self, junk):
        assert not call_hotkey_is_valid(junk)
        assert call_hotkey_setting({"call_answer_hotkey": junk}, "call_answer_hotkey") \
            == CALL_ANSWER_HOTKEY

    def test_a_show_winzapp_key_on_the_same_keys_keeps_them(self):
        """Set before the call keys shipped on that combination: the older key
        keeps it, the call key is off rather than never registering."""
        general = {"global_hotkey": {"vk": 0x0D, "mod": 0x0007}}
        assert call_hotkey_setting(general, "call_answer_hotkey") is None
        assert call_hotkey_setting(general, "call_end_hotkey") == CALL_END_HOTKEY
        assert saved_call_hotkey(general, "call_answer_hotkey") == CALL_ANSWER_HOTKEY

    def test_a_show_winzapp_key_elsewhere_changes_nothing(self):
        general = {"global_hotkey": {"vk": 0x57, "mod": 0x0003}}
        assert call_hotkey_setting(general, "call_answer_hotkey") == CALL_ANSWER_HOTKEY

    def test_every_shipped_key_is_valid(self):
        for vk, mod in CALL_HOTKEY_DEFAULTS.values():
            assert call_hotkey_is_valid({"vk": vk, "mod": mod})


class TestHoldingTheKeys:
    def test_nothing_is_registered_while_idle(self, recorder):
        window = _Window()
        window._sync_call_hotkeys()
        assert recorder.created == []

    def test_a_ringing_call_registers_both_keys(self, recorder):
        window = _Window()
        window.ring("a")
        window._sync_call_hotkeys()
        assert _held(window) == {"answer": CALL_ANSWER_HOTKEY, "end": CALL_END_HOTKEY}

    def test_a_call_in_progress_holds_only_the_end_key(self, recorder):
        window = _Window()
        window._active_voice_call = {"call_id": "c"}
        window._sync_call_hotkeys()
        assert _held(window) == {"end": CALL_END_HOTKEY}

    def test_answering_leaves_the_end_key_in_place(self, recorder, queued, monkeypatch):
        """The real answer path stops the alert (nothing rings) before it
        records the call; the key must not be dropped and taken again."""
        monkeypatch.setattr(calls_module, "threading",
                            types.SimpleNamespace(Thread=_NoThread))
        window = _AnsweringWindow()
        window.ring("a")
        window._sync_call_hotkeys()
        end_manager = window._call_hotkey_managers["end"][0]
        window.accept_incoming_call("a")
        _drain(queued)
        assert _held(window) == {"end": CALL_END_HOTKEY}
        assert window._call_hotkey_managers["end"][0] is end_manager
        assert not end_manager.stopped
        assert len(recorder.created) == 2

    def test_a_burst_of_changes_costs_one_sync(self, queued):
        window = _Window()
        window._request_call_hotkey_sync()
        window._request_call_hotkey_sync()
        assert len(queued) == 1
        _drain(queued)
        window._request_call_hotkey_sync()
        assert len(queued) == 1      # a later change is synced again

    def test_a_call_key_taken_elsewhere_is_retried_every_second(self, recorder):
        """Another account lets go of the key as soon as its call is decided."""
        window = _Window()
        window.ring("a")
        window._sync_call_hotkeys()
        assert {m.retry_seconds for m in recorder.created} == {1.0}

    def test_the_call_key_on_the_show_winzapp_key_is_not_registered(self, recorder):
        window = _Window({"global_hotkey": {"vk": 0x0D, "mod": 0x0007}})
        window.ring("a")
        window._sync_call_hotkeys()
        assert _held(window) == {"end": CALL_END_HOTKEY}

    def test_a_second_sync_does_not_register_again(self, recorder):
        window = _Window()
        window.ring("a")
        window._sync_call_hotkeys()
        window._sync_call_hotkeys()
        assert len(recorder.created) == 2

    def test_the_keys_are_released_when_the_call_is_over(self, recorder):
        window = _Window()
        window.ring("a")
        window._sync_call_hotkeys()
        window._active_incoming_calls.clear()
        window._sync_call_hotkeys()
        assert all(m.stopped for m in recorder.created)
        assert _held(window) == {}

    def test_a_group_offer_alone_takes_both_call_keys(self, recorder):
        """A group call can be answered and rejected like a one-to-one one."""
        window = _Window()
        window.ring("g", is_group=True)
        window._sync_call_hotkeys()
        assert _held(window) == {"answer": CALL_ANSWER_HOTKEY, "end": CALL_END_HOTKEY}

    def test_changed_settings_move_the_key_of_a_call_that_rings(self, recorder):
        window = _Window()
        window.ring("a")
        window._sync_call_hotkeys()
        window.settings["general"]["call_answer_hotkey"] = {"vk": 0x41, "mod": 0x0003}
        window._sync_call_hotkeys()
        assert _held(window)["answer"] == (0x41, 0x0003)
        assert recorder.created[0].stopped          # the old answer key is released
        assert _held(window)["end"] == CALL_END_HOTKEY

    def test_a_removed_key_is_not_registered(self, recorder):
        window = _Window({"call_answer_hotkey": {"vk": 0, "mod": 0}})
        window.ring("a")
        window._sync_call_hotkeys()
        assert _held(window) == {"end": CALL_END_HOTKEY}

    def test_shutting_down_releases_every_key(self, recorder):
        window = _Window()
        window.ring("a")
        window._sync_call_hotkeys()
        window._release_call_hotkeys()
        assert all(m.stopped for m in recorder.created)
        assert _held(window) == {}

    def test_nothing_is_taken_again_while_shutting_down(self, recorder):
        window = _Window()
        window.ring("a")
        window._shutting_down = True
        window._sync_call_hotkeys()
        assert recorder.created == []

    def test_release_before_any_sync_is_harmless(self):
        _Window()._release_call_hotkeys()

    def test_a_registration_error_does_not_break_the_call_alert(self, monkeypatch):
        def boom(*_args):
            raise OSError("no message loop")
        monkeypatch.setattr(call_hotkeys, "_HotkeyManager", boom)
        window = _Window()
        window.ring("a")
        window._sync_call_hotkeys()


class TestPressingTheKeys:
    @pytest.fixture(autouse=True)
    def _no_win32(self, monkeypatch):
        self.fronted = []
        monkeypatch.setattr(call_hotkeys, "force_foreground",
                            lambda hwnd, topmost=False: self.fronted.append((hwnd, topmost)))
        self.queued = []
        monkeypatch.setattr(call_hotkeys.wx, "CallAfter",
                            lambda fn, *a: self.queued.append((fn, a)))

    def test_answer_accepts_the_ringing_call(self):
        window = _Window()
        window.ring("a")
        window._answer_call_from_hotkey()
        assert window.accepted == [("a", None)]

    def test_a_video_call_is_answered_like_its_answer_button(self):
        """None = the camera starts, exactly what the Answer button does."""
        window = _Window()
        window.ring("a", is_video=True)
        window._answer_call_from_hotkey()
        assert window.accepted == [("a", None)]

    def test_answer_then_brings_the_call_window_forward(self):
        window = _Window()
        window.ring("a")
        window._answer_call_from_hotkey()
        assert [fn.__name__ for fn, _a in self.queued] == ["_focus_call_window"]
        window._focus_call_window()
        assert self.fronted == [(0x1234, True)]
        assert window.voice_call_window_mute_button.focused

    def test_a_hidden_call_window_is_left_alone(self):
        window = _Window()
        window.voice_call_window.shown = False
        window._focus_call_window()
        assert self.fronted == []

    def test_answer_takes_the_oldest_group_offer(self):
        window = _Window()
        window.ring("g", is_group=True)
        window.ring("a")
        window._answer_call_from_hotkey()
        assert window.accepted == [("g", None)]

    def test_answer_after_the_call_ended_does_nothing(self):
        window = _Window()
        window._answer_call_from_hotkey()
        assert window.accepted == [] and self.queued == []

    def test_end_rejects_a_ringing_call_and_says_so(self):
        window = _Window()
        window.ring("a")
        window._end_call_from_hotkey()
        assert window.rejected == ["a"] and window.ended == []
        assert window.spoken == [("<incoming_call_rejected>", True)]

    def test_end_with_nothing_ringing_hangs_up_the_call_in_progress(self):
        window = _Window()
        window._active_voice_call = {"call_id": "c"}
        window._end_call_from_hotkey()
        assert window.ended == [True] and window.rejected == []

    def test_a_ringing_call_is_decided_before_the_one_in_progress(self):
        window = _Window()
        window._active_voice_call = {"call_id": "c"}
        window.ring("a")
        window._end_call_from_hotkey()
        assert window.rejected == ["a"] and window.ended == []

    def test_a_group_offer_ringing_during_a_call_is_rejected_not_the_call(self):
        window = _Window()
        window._active_voice_call = {"call_id": "c"}
        window.ring("g", is_group=True)
        window._end_call_from_hotkey()
        assert window.rejected == ["g"] and window.ended == []

    def test_a_lone_group_offer_is_rejected(self):
        window = _Window()
        window.ring("g", is_group=True)
        window._end_call_from_hotkey()
        assert window.rejected == ["g"]

    def test_the_keys_do_nothing_once_shutting_down(self):
        window = _Window()
        window.ring("a")
        window._active_voice_call = {"call_id": "c"}
        window._shutting_down = True
        window._answer_call_from_hotkey()
        window._end_call_from_hotkey()
        assert window.accepted == [] and window.rejected == [] and window.ended == []
        assert self.queued == []

    def test_end_with_no_call_at_all_does_nothing(self):
        window = _Window()
        window._end_call_from_hotkey()
        assert window.rejected == [] and window.ended == [] and window.spoken == []


class TestSeveralAccounts:
    """Each account is its own process and Windows gives a combination to one
    registration: while account A holds the end key for its call, account B
    ringing cannot take it, and the press meant for B arrives at A."""

    @pytest.fixture(autouse=True)
    def _accounts(self, monkeypatch):
        self.queued = []
        monkeypatch.setattr(call_hotkeys.wx, "CallAfter",
                            lambda fn, *a: self.queued.append((fn, a)))
        monkeypatch.setattr(call_hotkeys, "threading",
                            types.SimpleNamespace(Thread=_RunNow))
        self.asked = []
        self.ringing_in = set()

        def ask(gd, account_id, timeout=1.0):
            self.asked.append(account_id)
            return account_id in self.ringing_in

        monkeypatch.setattr(ipc, "request_reject_ringing_call", ask)

    def _window(self, others=("b", "c")):
        window = _Window()
        window.global_dir, window.account_id = "gd", "a"
        window._other_running_account_ids = lambda: list(others)
        window._active_voice_call = {"call_id": "c1"}
        return window

    def test_another_account_ringing_is_rejected_there_not_the_call_here(self):
        self.ringing_in = {"b"}
        window = self._window()
        window._end_call_from_hotkey()
        _drain(self.queued)
        assert self.asked == ["b"]
        assert window.ended == []

    def test_with_nothing_ringing_anywhere_the_call_here_is_hung_up(self):
        window = self._window()
        window._end_call_from_hotkey()
        _drain(self.queued)
        assert self.asked == ["b", "c"]
        assert window.ended == [True]

    def test_a_call_that_ended_meanwhile_is_not_followed_by_another(self):
        window = self._window()
        window._end_call_from_hotkey()
        window._active_voice_call = {"call_id": "c2"}
        _drain(self.queued)
        assert window.ended == []

    def test_a_call_ringing_here_is_rejected_without_asking_anyone(self):
        window = self._window()
        window.ring("x")
        window._end_call_from_hotkey()
        assert window.rejected == ["x"] and self.asked == []

    def test_an_unreachable_account_still_lets_the_call_be_hung_up(self, monkeypatch):
        def boom(*_a, **_k):
            raise OSError("pipe")
        monkeypatch.setattr(ipc, "request_reject_ringing_call", boom)
        window = self._window(others=("b",))
        window._end_call_from_hotkey()
        _drain(self.queued)
        assert window.ended == [True]

    def test_asked_by_another_account_a_ringing_call_is_rejected(self):
        window = _Window()
        window.ring("x")
        assert window._ipc_reject_ringing_call() is True
        _drain(self.queued)
        assert window.rejected == ["x"]
        assert window.spoken == [("<incoming_call_rejected>", True)]

    def test_asked_with_nothing_ringing_the_answer_is_no(self):
        window = _Window()
        assert window._ipc_reject_ringing_call() is False
        assert self.queued == []

    def test_asked_while_shutting_down_the_answer_is_no(self):
        window = _Window()
        window.ring("x")
        window._shutting_down = True
        assert window._ipc_reject_ringing_call() is False


class _FunnelWindow(_Window):
    """Records the sync requests the call code makes."""
    _sync_incoming_call_bar = CallsMixin._sync_incoming_call_bar
    _sync_voice_call_bar = CallsMixin._sync_voice_call_bar
    _stop_voice_call_audio = CallsMixin._stop_voice_call_audio

    def __init__(self):
        super().__init__()
        self.requests = 0
        self.voice_call_window = None

    def _request_call_hotkey_sync(self):
        self.requests += 1

    def _stop_incoming_call_audio_monitor(self):
        pass

    def _stop_call_camera(self, reset_availability=False):
        pass


class TestEveryChangeOfTheCallsAsksForASync:
    def test_a_change_of_the_ringing_set(self):
        window = _FunnelWindow()
        window._sync_incoming_call_bar()
        assert window.requests == 1

    def test_a_change_of_the_call_in_progress(self):
        window = _FunnelWindow()
        window._sync_voice_call_bar()
        assert window.requests == 1

    def test_the_end_of_a_call(self, queued):
        window = _FunnelWindow()
        window._active_voice_call = {"call_id": "c"}
        window._stop_voice_call_audio()
        _drain(queued)
        assert window._active_voice_call is None
        assert window.requests == 1


def test_shutdown_releases_the_call_keys_before_stopping_anything():
    from main import MainWindow

    order = []

    class _Stub:
        _perform_shutdown = MainWindow._perform_shutdown

        def __init__(self):
            self._teardown_started_lock = threading.Lock()
            self._teardown_complete_event = threading.Event()
            self._shutting_down = False

        def _release_call_hotkeys(self):
            order.append("release keys")

        def _stop_voice_call_audio(self, grace_seconds=0.0):
            order.append("stop call audio")

        def _stop_wpp_server(self):
            order.append("stop server")

        def _stop_default_output_follow(self):
            order.append("stop default-output watcher")

        def _flush_pending_debounced_saves(self):
            pass

    _Stub()._perform_shutdown()
    assert order[0] == "release keys"
    assert {"stop call audio", "stop server"} <= set(order)


def test_the_bar_sync_still_works_on_a_window_without_the_mixin():
    class _Bare(CallsMixin):
        _active_incoming_calls = {}

        def Layout(self):
            pass

    _Bare()._sync_incoming_call_bar()
