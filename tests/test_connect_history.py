"""How much history the Neonize pairing brings (ui/dialogs/connect_history.py):
a choice in the pairing dialog, sent with the pairing's start-session."""
import types

from core import connection_engine as engines
from ui.dialogs import connect_history as ch


def test_the_stored_choice_or_the_old_default():
    assert ch.stored_days({}) == 180
    assert ch.stored_days({"connection": {"neonize_history_days": 3650}}) == 3650
    for bad in (7, "365", True, None, [365]):
        assert ch.stored_days({"connection": {"neonize_history_days": bad}}) == 180


def test_only_neonize_sends_it_with_start_session():
    settings = {"connection": {"neonize_history_days": 365}}
    assert ch.with_history({"waitQrCode": False}, engines.NEONIZE, settings) == {
        "waitQrCode": False, "historyDays": 365}
    body = {"waitQrCode": False}
    assert ch.with_history(body, engines.WPPCONNECT, settings) is body


def test_the_choices_are_the_bridges():
    import importlib.util
    import pathlib
    path = pathlib.Path(__file__).resolve().parents[1] / "neonize_bridge" / "pairing_history.py"
    spec = importlib.util.spec_from_file_location("pairing_history_for_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert ch.CHOICES == module.CHOICES
    assert set(ch.LABEL_KEYS) == set(ch.CHOICES)


def test_a_change_is_stored_once_and_sent_to_the_bridge():
    saved = []
    window = types.SimpleNamespace(settings={}, save_settings=lambda: saved.append(1),
                                   token="acc:tok", wpp_server="http://127.0.0.1", wpp_port=6300,
                                   wpp_api_key="key")
    assert ch.remember(window, 3650) is True
    assert ch.remember(window, 3650) is False
    assert saved == [1] and window.settings["connection"]["neonize_history_days"] == 3650
    posted = []
    done = __import__("threading").Event()

    def post(url, json, headers, timeout):
        posted.append((url, json))
        done.set()
        return types.SimpleNamespace(status_code=200)

    ch.send_to_bridge(window, 3650, post)
    assert done.wait(5)
    assert posted == [("http://127.0.0.1:6300/api/acc:tok/pairing-history", {"days": 3650})]


def test_a_bad_index_reads_as_the_default():
    assert ch.days_at(4) == 3650 and ch.days_at(-1) == 180 and ch.days_at(None) == 180
