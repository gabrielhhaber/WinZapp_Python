"""The Neonize engine's life cycle in the client: starting the bridge at launch
(including its failure and the background mode), stopping it at shutdown, and the
restart flag the settings dialog raises when the engine is changed."""
from types import SimpleNamespace

import pytest

from core import connection_engine as engines
from core import neonize_bridge_process as bridge_process
from main_window import neonize_bridge as mixin_module
from main_window.neonize_bridge import NeonizeBridgeMixin
from ui.dialogs.settings_dialog import SettingsDialog

LOCAL = dict(wpp_server="http://127.0.0.1", wpp_ws_server="ws://127.0.0.1", wpp_port=6300)


class _I18n:
    @staticmethod
    def t(key):
        return key


def _window(custom_api=True, background_mode=False, **overrides):
    window = SimpleNamespace(
        wpp_custom_api=custom_api, background_mode=background_mode, i18n=_I18n(),
        wpp_api_key="wz-local-api-key", **{**LOCAL, **overrides})
    window._uses_neonize = lambda: NeonizeBridgeMixin._uses_neonize(window)
    window._use_neonize_bridge_secret = lambda: window.__dict__.setdefault("secret_used", True)
    window.output = lambda text, interrupt=False: window.__dict__.setdefault("spoken", []).append(text)
    window._stop_neonize_bridge = lambda force=False: None
    window._register_node_lease = lambda: window.__dict__.setdefault("leases", []).append(1)
    window._finish_pending_deletions_on_bridge = lambda: window.__dict__.setdefault(
        "pending_deletions_retried", True)
    return window


class _FakeBridge:
    instances = []

    def __init__(self, port):
        self.port, self.started, self.stopped = port, False, False
        self.bridge = "somewhere"
        self.raises, self.result = False, True
        _FakeBridge.instances.append(self)

    def start(self):
        self.started = True
        if self.raises:
            raise RuntimeError("boom")
        return self.result

    def stop(self):
        self.stopped = True


@pytest.fixture
def boxes(monkeypatch):
    _FakeBridge.instances = []
    monkeypatch.setattr(bridge_process, "NeonizeBridge", _FakeBridge)
    shown = []
    monkeypatch.setattr(mixin_module.wx, "MessageBox", lambda *a, **k: shown.append(a))
    # the box waits for the main loop in the app; here it is run at once
    monkeypatch.setattr(mixin_module.wx, "CallAfter", lambda f, *a, **k: f(*a, **k))
    return shown


def test_other_engines_never_start_a_bridge(boxes):
    window = _window(custom_api=False)
    NeonizeBridgeMixin._start_neonize_bridge_if_needed(window)
    assert _FakeBridge.instances == [] and boxes == []
    assert "leases" not in window.__dict__


def test_a_custom_server_on_another_port_is_not_neonize(boxes):
    window = _window(wpp_port=8443)
    NeonizeBridgeMixin._start_neonize_bridge_if_needed(window)
    assert _FakeBridge.instances == []


def test_the_neonize_engine_starts_the_bridge_on_its_port_and_takes_its_secret(boxes):
    window = _window()
    NeonizeBridgeMixin._start_neonize_bridge_if_needed(window)
    bridge, = _FakeBridge.instances
    assert bridge.started and bridge.port == 6300
    assert window.neonize_bridge is bridge and window.secret_used is True
    assert window.pending_deletions_retried is True      # removals waiting for the bridge
    assert window.leases == [1]      # the other accounts and the updater see this one running
    assert boxes == []


@pytest.mark.parametrize("breaks", ["not_up", "raises"])
def test_a_bridge_that_does_not_come_up_is_reported_once_and_keeps_the_default_key(
        boxes, monkeypatch, breaks):
    window = _window()
    original = _FakeBridge.start

    def start(self):
        self.result = breaks != "not_up"
        self.raises = breaks == "raises"
        return original(self)
    monkeypatch.setattr(_FakeBridge, "start", start)

    NeonizeBridgeMixin._start_neonize_bridge_if_needed(window)

    assert "secret_used" not in window.__dict__
    (message, title, _style), = boxes
    assert message == "neonize_bridge_unavailable_message"
    assert title == "neonize_bridge_unavailable_title"
    assert window.spoken[-1] == "neonize_bridge_unavailable_message"


def test_background_mode_does_not_open_a_message_box(boxes, monkeypatch):
    window = _window(background_mode=True)
    monkeypatch.setattr(_FakeBridge, "start", lambda self: False)
    NeonizeBridgeMixin._start_neonize_bridge_if_needed(window)
    assert boxes == []


def test_stopping_asks_the_bridge_to_stop_and_forgets_it():
    window = SimpleNamespace(neonize_bridge=_FakeBridge(6300))
    bridge = window.neonize_bridge
    NeonizeBridgeMixin._stop_neonize_bridge(window)
    assert bridge.stopped and window.neonize_bridge is None


def test_stopping_without_a_bridge_is_a_no_op_and_a_failing_stop_is_swallowed():
    NeonizeBridgeMixin._stop_neonize_bridge(SimpleNamespace())
    bridge = _FakeBridge(6300)
    bridge.stop = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    window = SimpleNamespace(neonize_bridge=bridge)
    NeonizeBridgeMixin._stop_neonize_bridge(window)
    assert window.neonize_bridge is None


@pytest.mark.parametrize("loaded,selected,restart", [
    (engines.WPPCONNECT, engines.WPPCONNECT, False),
    (engines.WPPCONNECT, engines.NEONIZE, True),
    (engines.NEONIZE, engines.CUSTOM, True),
])
def test_the_settings_dialog_flags_a_restart_only_when_the_engine_changed(
        loaded, selected, restart):
    dialog = SimpleNamespace(
        _engine_choice=SimpleNamespace(GetSelection=lambda: engines.index_of(selected)),
        _loaded_engine=loaded, _restart_required=False)
    SettingsDialog._flag_restart_if_engine_changed(dialog)
    assert dialog._restart_required is restart
    assert dialog._loaded_engine == selected


def test_a_neonize_account_shows_up_among_the_running_accounts(boxes, tmp_path):
    """Audit F4, M3: only the Node path wrote a lease, so with the bridge
    "Quit all accounts" and the updater's list of peers were always empty."""
    import node_coord
    from main_window.wpp_server import WppServerMixin

    window = _window()
    window.global_dir, window.account_id = str(tmp_path), "a" * 32
    window._register_node_lease = lambda: WppServerMixin._register_node_lease(window)

    NeonizeBridgeMixin._start_neonize_bridge_if_needed(window)

    leases = node_coord.live_node_leases(str(tmp_path), is_alive=lambda pid, created: True)
    assert [lease.get("account_id") for lease in leases] == ["a" * 32]


# -- audit F4, M7: Settings > Connection does not offer Neonize without a bridge --

def _choice_dialog(selected, shown, loaded, monkeypatch, bridge_installed):
    from ui.dialogs import settings_dialog as module
    boxes, picked = [], []
    monkeypatch.setattr(module.wx, "MessageBox", lambda *a, **k: boxes.append(a))
    monkeypatch.setattr(bridge_process, "command",
                        lambda *a, **k: ["bridge"] if bridge_installed else None)
    dialog = SimpleNamespace(
        _engine_choice=SimpleNamespace(
            GetSelection=lambda: engines.index_of(selected),
            SetSelection=lambda index: picked.append(engines.engine_at(index))),
        _loaded_engine=loaded, _engine_shown=shown, main_window=SimpleNamespace(i18n=_I18n()),
        _custom_api_check=SimpleNamespace(SetValue=lambda v: picked.append(("api", v))),
        _server_field=SimpleNamespace(SetValue=lambda v: None),
        _ws_server_field=SimpleNamespace(SetValue=lambda v: None),
        _port_field=SimpleNamespace(SetValue=lambda v: None),
        _update_fields_state=lambda: None, _sync_fixed_port_field=lambda: None)
    dialog._bridge_allows = lambda engine: SettingsDialog._bridge_allows(dialog, engine)
    return dialog, boxes, picked


def test_neonize_is_refused_when_no_bridge_is_installed(monkeypatch):
    dialog, boxes, picked = _choice_dialog(
        engines.NEONIZE, engines.WPPCONNECT, engines.WPPCONNECT, monkeypatch, False)
    skipped = []
    SettingsDialog._on_engine_choice(dialog, SimpleNamespace(Skip=lambda: skipped.append(1)))
    assert len(boxes) == 1 and boxes[0][0] == "neonize_bridge_unavailable_message"
    assert picked == [engines.WPPCONNECT]          # back to what it showed
    assert dialog._engine_shown == engines.WPPCONNECT


def test_neonize_is_chosen_when_a_bridge_is_installed(monkeypatch):
    dialog, boxes, picked = _choice_dialog(
        engines.NEONIZE, engines.WPPCONNECT, engines.WPPCONNECT, monkeypatch, True)
    SettingsDialog._on_engine_choice(dialog, SimpleNamespace(Skip=lambda: None))
    assert boxes == [] and ("api", True) in picked
    assert dialog._engine_shown == engines.NEONIZE


def test_an_install_already_on_neonize_keeps_it_while_its_bridge_is_missing():
    assert engines.can_choose(engines.NEONIZE, False, engines.NEONIZE) is True
    assert engines.can_choose(engines.NEONIZE, False, engines.CUSTOM) is False
    assert engines.can_choose(engines.CUSTOM, False, engines.WPPCONNECT) is True


# -- audit F4, M4b and M4c ------------------------------------------------

def test_a_slow_start_is_announced_before_the_wait(boxes, monkeypatch):
    monkeypatch.setattr(bridge_process, "port_open", lambda *a, **k: False)
    window = _window()
    NeonizeBridgeMixin._start_neonize_bridge_if_needed(window)
    assert window.spoken == ["starting_api"]


def test_nothing_is_announced_when_the_bridge_is_already_up_or_in_the_background(boxes, monkeypatch):
    monkeypatch.setattr(bridge_process, "port_open", lambda *a, **k: True)
    up = _window()
    NeonizeBridgeMixin._start_neonize_bridge_if_needed(up)
    monkeypatch.setattr(bridge_process, "port_open", lambda *a, **k: False)
    quiet = _window(background_mode=True)
    NeonizeBridgeMixin._start_neonize_bridge_if_needed(quiet)
    assert "spoken" not in up.__dict__ and "spoken" not in quiet.__dict__


def test_the_bridge_is_left_at_interpreter_exit_too(boxes, monkeypatch):
    registered = []
    monkeypatch.setattr(mixin_module.atexit, "register",
                        lambda fn, *a, **k: registered.append((fn, a, k)))
    window = _window()
    NeonizeBridgeMixin._start_neonize_bridge_if_needed(window)
    assert registered == [(window._stop_neonize_bridge, (), {"force": True})]
