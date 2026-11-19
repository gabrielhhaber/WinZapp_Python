"""The own profile photo notice refreshes only its open panel's baseline."""
from types import SimpleNamespace as N

from core import remote_own_profile as events
from core.whatsapp_profile import CurrentProfile


class Panel:
    def __init__(self, mw):
        self.main_window = mw
        self._current = CurrentProfile("typed name", "typed about", False)
        self._name_field = N(value="typed name")
        self._status_field = N(value="typed about")
        self._photo_path = "chosen.jpg"
        self.shown = []

    def _show_photo_state(self):
        self.shown.append(self._current.photo)


def _window():
    return N(token="one", wpp_server="local", wpp_port=6300, _sync_run_id=1,
             _is_self_jid=lambda jid: jid == "self@s.whatsapp.net", _shutting_down=False)


def test_photo_refresh_only_changes_photo_and_preserves_profile_drafts():
    mw = _window()
    panel = Panel(mw)
    events.register_panel(panel)
    context = panel._own_profile_photo_context
    events.apply_photo(mw, True, context, 0)
    assert panel._current == CurrentProfile("typed name", "typed about", True)
    assert panel._name_field.value == "typed name"
    assert panel._status_field.value == "typed about"
    assert panel._photo_path == "chosen.jpg"
    assert panel.shown == [True]


def test_photo_notice_requires_self_and_an_open_panel(monkeypatch):
    started = []

    class Thread:
        def __init__(self, **kw): self.kw = kw
        def start(self): started.append(self)

    monkeypatch.setattr(events.threading, "Thread", Thread)
    mw = _window()
    events.photo_changed(mw, "self@s.whatsapp.net")
    panel = Panel(mw)
    events.register_panel(panel)
    events.photo_changed(mw, "peer@s.whatsapp.net")
    assert started == []
    events.photo_changed(mw, "self@s.whatsapp.net")
    assert len(started) == 1


def test_burst_drops_old_read_then_runs_one_fresh_read(monkeypatch):
    started, callbacks = [], []

    class Thread:
        def __init__(self, **kw): self.kw = kw
        def start(self): started.append(self)

    monkeypatch.setattr(events.threading, "Thread", Thread)
    monkeypatch.setattr(events.wx, "CallAfter", lambda fn, *a: callbacks.append((fn, a)))
    mw, panel = _window(), None
    photo = [False]
    mw.fetch_my_profile = lambda: CurrentProfile("current", "about", photo[0])
    panel = Panel(mw)
    events.register_panel(panel)
    events.photo_changed(mw, "self@s.whatsapp.net")
    events.photo_changed(mw, "self@s.whatsapp.net")
    assert len(started) == 1
    # The first read was overtaken and cannot roll back the newer observation.
    started[0].kw["target"]()
    fn, args = callbacks.pop()
    fn(*args)
    assert panel.shown == []
    assert len(started) == 2
    photo[0] = True
    started[1].kw["target"]()
    fn, args = callbacks.pop()
    fn(*args)
    assert panel.shown == [True]


def test_old_account_read_is_discarded(monkeypatch):
    started, callbacks = [], []

    class Thread:
        def __init__(self, **kw): self.kw = kw
        def start(self): started.append(self)

    monkeypatch.setattr(events.threading, "Thread", Thread)
    monkeypatch.setattr(events.wx, "CallAfter", lambda fn, *a: callbacks.append((fn, a)))
    mw, panel = _window(), None
    mw.fetch_my_profile = lambda: CurrentProfile("name", "about", True)
    panel = Panel(mw)
    events.register_panel(panel)
    events.photo_changed(mw, "self@s.whatsapp.net")
    started[0].kw["target"]()
    mw.token = "two"
    fn, args = callbacks.pop()
    fn(*args)
    assert panel.shown == [] and panel._current.photo is False


def test_thread_start_failure_does_not_leave_photo_refresh_stuck(monkeypatch):
    class BrokenThread:
        def __init__(self, **kwargs): pass
        def start(self): raise RuntimeError("cannot start")
    monkeypatch.setattr(events.threading, "Thread", BrokenThread)
    mw = _window()
    panel = Panel(mw)
    events.register_panel(panel)
    events.photo_changed(mw, "self@s.whatsapp.net")
    assert not mw._own_profile_photo_events["running"]


def test_worker_does_not_read_after_account_changed(monkeypatch):
    started, calls = [], []
    class Thread:
        def __init__(self, **kwargs): self.kw = kwargs
        def start(self): started.append(self)
    monkeypatch.setattr(events.threading, "Thread", Thread)
    monkeypatch.setattr(events.wx, "CallAfter", lambda fn, *a: fn(*a))
    mw = _window()
    mw.fetch_my_profile = lambda: calls.append(True)
    panel = Panel(mw)
    events.register_panel(panel)
    events.photo_changed(mw, "self@s.whatsapp.net")
    mw.token = "second"
    started[0].kw["target"]()
    assert calls == [] and panel.shown == []
