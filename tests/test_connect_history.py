"""How much history the Neonize pairing brings (ui/dialogs/connect_history.py):
a choice in the pairing dialog, sent with the pairing's start-session."""
import types

from core import connection_engine as engines
from ui.dialogs import connect_history as ch


def test_the_stored_choice_or_the_old_default():
    assert ch.stored_days({}) == 180
    assert ch.stored_days({"connection": {"neonize_history_days": 3650}}) == 3650
    for bad in (-7, 99999, "365", True, None, [365]):
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


def test_a_custom_amount_is_validated():
    assert ch.parse_amount("18", "months") == (18, None)
    assert ch.parse_amount(" 3 ", "years") == (3, None)
    for bad in ("", "abc", "1.5", "-3", "١٢", "2e3"):
        assert ch.parse_amount(bad, "days")[1][0] == "pairing_history_amount_not_number"
    assert ch.parse_amount("0", "days")[1][0] == "pairing_history_amount_zero"
    assert ch.parse_amount("21", "years")[1] == ("pairing_history_amount_too_big_years", {"max": 20})


def test_months_and_years_never_fall_short_and_stay_within_the_bridges_limit():
    assert ch.days_for("days", 45) == 45
    assert ch.days_for("months", 18) >= 18 * 31 - 1
    assert ch.days_for("years", 2) >= 2 * 365
    for unit, (_per, largest) in ch.UNITS.items():
        assert ch.days_for(unit, largest) <= ch.MAX_DAYS


def test_a_custom_choice_is_shown_again_as_it_was_typed():
    window = types.SimpleNamespace(settings={}, save_settings=lambda: None)
    assert ch.remember(window, ch.days_for("months", 18), ("months", 18)) is True
    settings = window.settings
    assert ch.stored_custom(settings) == ("months", 18)
    assert ch.entries()[ch.selection_of(settings)] == ("custom", "months")
    # back to a ready-made period: the custom one is forgotten
    assert ch.remember(window, 365) is True
    assert ch.stored_custom(settings) is None
    assert ch.entries()[ch.selection_of(settings)] == ("preset", 365)


def test_every_entry_and_message_has_a_string():
    keys = (list(ch.LABEL_KEYS.values()) + list(ch.CUSTOM_LABEL_KEYS.values())
            + list(ch.AMOUNT_LABEL_KEYS.values())
            + ["pairing_history_amount_not_number", "pairing_history_amount_zero"]
            + [f"pairing_history_amount_too_big_{u}" for u in ch.UNITS])
    from tests.mnemonics import load_strings
    strings = load_strings("pt-BR")
    assert all(strings.get(k) for k in keys)
