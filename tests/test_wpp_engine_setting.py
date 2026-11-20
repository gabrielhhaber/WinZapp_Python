"""The connection engine is a setting of its own (``connection.wpp_engine``,
install-wide), not something inferred from a port that belongs to one account.

Audit F4, items H3 and M6: with accounts A (port 6300) and B (automatic port)
on WPPConnect, A switching the install to Neonize made B read as "custom" and
start no bridge; and a settings file from another install moved the engine of
this one, live, without a restart.
"""
import copy
import json
import threading
import types

import pytest

from app_settings import AppSettings
from core import connection_engine as engines
from core import settings_transfer as transfer
from core.utils import DEFAULT_SETTINGS
from main import MainWindow
from main_window.neonize_bridge import NeonizeBridgeMixin
from main_window.wpp_server import WppServerMixin

LOCAL = {"wpp_server": "http://127.0.0.1", "wpp_ws_server": "ws://127.0.0.1"}


class TestTheEngineOfAConnectionBlock:
    def test_the_stored_engine_wins_over_the_port(self):
        conn = {"wpp_custom_api": True, "wpp_engine": "neonize", "wpp_port": 6341, **LOCAL}
        assert engines.engine_from_connection(conn) == engines.NEONIZE

    def test_a_real_wppconnect_typed_in_at_the_bridge_address_stays_custom(self):
        conn = {"wpp_custom_api": True, "wpp_engine": "custom", "wpp_port": 6300, **LOCAL}
        assert engines.engine_from_connection(conn) == engines.CUSTOM

    def test_an_install_older_than_the_setting_is_inferred(self):
        conn = {"wpp_custom_api": True, "wpp_port": 6300, **LOCAL}
        assert engines.engine_from_connection(conn) == engines.NEONIZE
        assert engines.engine_from_connection({"wpp_custom_api": False}) == engines.WPPCONNECT

    def test_a_stored_engine_that_contradicts_the_switch_is_not_believed(self):
        stale = {"wpp_custom_api": False, "wpp_engine": "neonize", "wpp_port": 6300, **LOCAL}
        assert engines.engine_from_connection(stale) == engines.WPPCONNECT
        assert engines.engine_from_connection(None) == engines.WPPCONNECT

    def test_the_migration_keeps_what_is_stored(self):
        assert engines.engine_to_keep("custom", True, *LOCAL.values(), 6300) == engines.CUSTOM
        assert engines.engine_to_keep("", True, *LOCAL.values(), 6300) == engines.NEONIZE
        assert engines.engine_to_keep("nonsense", False, *LOCAL.values(), 6300) == engines.WPPCONNECT


class _Window:
    def __init__(self, global_dir, connection):
        self.settings = {"general": {}, "connection": dict(connection)}
        self.global_dir = str(global_dir)

    _apply_global_settings = MainWindow._apply_global_settings
    _share_keyboard_shortcuts = MainWindow._share_keyboard_shortcuts


class TestTheMigration:
    def test_an_existing_neonize_install_keeps_its_engine_for_every_account(self, tmp_path):
        """A (port 6300) starts first and decides; B (automatic port) then reads
        the shared value instead of inferring "custom" from its own port."""
        app = AppSettings(str(tmp_path))
        app.set("wpp_custom_api", True)        # what the old build wrote; no wpp_engine
        a = _Window(tmp_path, {"wpp_port": 6300})
        b = _Window(tmp_path, {"wpp_port": 6341})
        a._apply_global_settings()
        b._apply_global_settings()
        assert a.settings["connection"]["wpp_engine"] == engines.NEONIZE
        assert b.settings["connection"]["wpp_engine"] == engines.NEONIZE
        assert engines.engine_from_connection(b.settings["connection"]) == engines.NEONIZE

    def test_a_wppconnect_install_stays_on_wppconnect(self, tmp_path):
        a = _Window(tmp_path, {"wpp_port": 6300})
        a._apply_global_settings()
        assert a.settings["connection"]["wpp_engine"] == engines.WPPCONNECT

    def test_a_stored_choice_is_never_migrated_over(self, tmp_path):
        app = AppSettings(str(tmp_path))
        app.set("wpp_custom_api", True)
        app.set("wpp_engine", "custom")
        a = _Window(tmp_path, {"wpp_port": 6300})
        a._apply_global_settings()
        assert a.settings["connection"]["wpp_engine"] == engines.CUSTOM


def _port_window(connection):
    window = types.SimpleNamespace(
        settings={"connection": connection}, account_id="acc", registry=object(),
        global_dir=None, save_settings=lambda: None, _is_port_free=lambda port: True,
        _port_runs_our_api=lambda port: False, _peer_node_ports=lambda any_port=False: [],
    )
    return WppServerMixin._resolve_wpp_port.__get__(window)


class TestThePort:
    def test_neonize_forces_the_bridge_port_for_an_account_that_had_another(self):
        conn = {"wpp_custom_api": True, "wpp_engine": "neonize", "wpp_port": 6341, **LOCAL}
        assert _port_window(conn)(conn) == engines.NEONIZE_PORT

    def test_a_custom_server_keeps_its_own_port(self):
        conn = {"wpp_custom_api": True, "wpp_engine": "custom", "wpp_port": 6341, **LOCAL}
        assert _port_window(conn)(conn) == 6341


class TestWhichEngineThisProcessUses:
    def test_the_started_engine_decides_not_the_port(self):
        window = types.SimpleNamespace(wpp_engine="neonize", wpp_custom_api=True,
                                       wpp_port=6341, **LOCAL)
        assert NeonizeBridgeMixin._uses_neonize(window) is True

    def test_a_custom_wppconnect_at_the_bridge_address_does_not_start_a_bridge(self):
        window = types.SimpleNamespace(wpp_engine="custom", wpp_custom_api=True,
                                       wpp_port=6300, **LOCAL)
        assert NeonizeBridgeMixin._uses_neonize(window) is False


# -- importing settings (M6) --------------------------------------------------

def _install(engine, port):
    settings = copy.deepcopy(DEFAULT_SETTINGS)
    settings["connection"].update({
        "wpp_custom_api": engines.uses_custom_api(engine), "wpp_engine": engine,
        "wpp_port": port, "wpp_api_key": "install-key", **LOCAL})
    return settings


def _file(tmp_path, settings):
    path = tmp_path / "export.json"
    path.write_text(json.dumps(transfer.build_export(settings)), encoding="utf-8")
    return str(path)


def _window(settings, engine):
    from tests.test_settings_export_import import _Stub
    window = _Stub(settings)
    window.wpp_engine = engine
    window.wpp_custom_api = engines.uses_custom_api(engine)
    window.wpp_port = settings["connection"]["wpp_port"]
    window.wpp_server, window.wpp_ws_server = LOCAL["wpp_server"], LOCAL["wpp_ws_server"]
    return window


class TestImportingNeverMovesTheEngine:
    def test_an_export_never_carries_the_engine_of_the_bundled_or_neonize_install(self):
        for engine in (engines.WPPCONNECT, engines.NEONIZE):
            exported = transfer.exportable_settings(_install(engine, 6300))
            assert "connection" not in exported

    def test_a_wppconnect_file_leaves_a_neonize_install_on_neonize(self, tmp_path):
        path = _file(tmp_path, _install(engines.WPPCONNECT, 6412))
        window = _window(_install(engines.NEONIZE, 6300), engines.NEONIZE)
        error, _applied = window.import_settings_from_file(path)
        assert error == ""
        assert window.settings["connection"]["wpp_custom_api"] is True
        assert window.settings["connection"]["wpp_engine"] == engines.NEONIZE
        assert window.wpp_custom_api is True and window.wpp_port == 6300
        assert not window._engine_restart_pending

    def test_a_file_from_the_old_neonize_build_does_not_move_a_wppconnect_install(self, tmp_path):
        old = _install(engines.NEONIZE, 6300)
        old["connection"].pop("wpp_engine")      # exported before the setting existed
        incoming = {"connection": dict(old["connection"])}
        window = _window(_install(engines.WPPCONNECT, 6412), engines.WPPCONNECT)
        assert transfer.api_change(window.settings, incoming) is None
        merged, _applied, ignored = transfer.merge_settings(window.settings, incoming)
        assert merged["connection"] == window.settings["connection"]
        assert "connection.wpp_custom_api" in ignored

    def test_a_custom_server_file_is_confirmed_and_waits_for_a_restart(self, tmp_path):
        custom = _install(engines.CUSTOM, 8443)
        custom["connection"].update({"wpp_server": "https://api.exemplo.com",
                                     "wpp_ws_server": "wss://api.exemplo.com"})
        path = _file(tmp_path, custom)
        window = _window(_install(engines.NEONIZE, 6300), engines.NEONIZE)
        asked = []

        error, _applied = window.import_settings_from_file(
            path, confirm_api_change=lambda change: asked.append(change) or True)

        assert error == "" and asked and asked[0]["server"] == "https://api.exemplo.com:8443"
        assert window.settings["connection"]["wpp_engine"] == engines.CUSTOM
        # Saved for the next start, but this process keeps the bridge it runs on.
        assert window._engine_restart_pending is True
        assert window.wpp_engine == engines.NEONIZE
        assert window.wpp_server == "http://127.0.0.1" and window.wpp_port == 6300
        assert window.wpp_custom_api is True

    def test_a_custom_server_at_the_bridge_address_is_still_a_change(self, tmp_path):
        """api_change() compared only the five keys: a WPPConnect typed in at
        127.0.0.1:6300 equals the stored bridge address, so nothing was asked."""
        custom = _install(engines.CUSTOM, 6300)
        incoming, _ = transfer.read_export(transfer.build_export(custom))
        assert transfer.api_change(_install(engines.NEONIZE, 6300), incoming) is not None
        assert transfer.api_change(_install(engines.CUSTOM, 6300), incoming) is None

    def test_a_new_address_for_the_same_custom_server_follows_live(self, tmp_path):
        custom = _install(engines.CUSTOM, 8443)
        custom["connection"].update({"wpp_server": "https://api.exemplo.com",
                                     "wpp_ws_server": "wss://api.exemplo.com"})
        path = _file(tmp_path, custom)
        mine = _install(engines.CUSTOM, 7000)
        window = _window(mine, engines.CUSTOM)

        window.import_settings_from_file(path, confirm_api_change=lambda change: True)

        assert not window._engine_restart_pending
        assert window.wpp_server == "https://api.exemplo.com" and window.wpp_port == 8443
