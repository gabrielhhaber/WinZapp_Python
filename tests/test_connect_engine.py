import pytest

from core import connection_engine as engines
from ui.dialogs import connect_engine


def test_the_dialog_offers_neonize_only_with_a_bridge():
    # Neonize comes first, WPPConnect second
    assert connect_engine.offered_engines(True, engines.WPPCONNECT) == [engines.NEONIZE, engines.WPPCONNECT]
    assert connect_engine.offered_engines(True, engines.NEONIZE) == [engines.NEONIZE, engines.WPPCONNECT]
    assert connect_engine.offered_engines(False, engines.WPPCONNECT) == [engines.WPPCONNECT]
    assert connect_engine.offered_engines(False, engines.NEONIZE) == [engines.NEONIZE, engines.WPPCONNECT]


def test_no_engine_list_while_another_account_runs():
    assert connect_engine.offered_engines(True, engines.WPPCONNECT, True) == []


def test_a_custom_server_has_no_engine_list():
    assert connect_engine.offered_engines(True, engines.CUSTOM) == []


def test_the_stored_settings_name_the_engine_and_its_fixed_address():
    neonize = connect_engine.stored_settings(engines.NEONIZE)
    assert neonize["wpp_engine"] == engines.NEONIZE and neonize["wpp_custom_api"] is True
    assert neonize["wpp_port"] == engines.NEONIZE_PORT
    wppconnect = connect_engine.stored_settings(engines.WPPCONNECT)
    assert wppconnect["wpp_engine"] == engines.WPPCONNECT and wppconnect["wpp_custom_api"] is False


class _Window:
    def __init__(self):
        self.settings = {"connection": {"wpp_engine": engines.WPPCONNECT, "wpp_port": 6300}}
        self.saved = []

    def save_settings(self):
        self.saved.append(dict(self.settings["connection"]))


@pytest.fixture(autouse=True)
def _paths(monkeypatch):
    monkeypatch.setattr(connect_engine, "data_path", lambda *parts: "\\".join(("data",) + parts))
    monkeypatch.setattr(connect_engine, "log_path", lambda *parts: "\\".join(("logs",) + parts))


def test_the_engine_is_stored_before_the_restart():
    window = _Window()
    calls = []
    assert connect_engine.switch_and_restart(
        window, None, engines.NEONIZE, lambda script, log: calls.append(script) or True)
    assert window.settings["connection"]["wpp_engine"] == engines.NEONIZE
    assert calls and calls[0].endswith("winzapp_restart.bat")


def test_a_restart_that_cannot_start_puts_the_engine_back(monkeypatch):
    shown = []
    monkeypatch.setattr(connect_engine.wx, "MessageBox", lambda *a, **k: shown.append(a))

    class _I18n:
        def t(self, key):
            return key

    window = _Window()
    assert not connect_engine.switch_and_restart(window, _I18n(), engines.NEONIZE, lambda s, l: False)
    assert window.settings["connection"] == {"wpp_engine": engines.WPPCONNECT, "wpp_port": 6300}
    assert shown and shown[0][0] == "pairing_engine_restart_failed"
