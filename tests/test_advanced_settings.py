"""Settings > Advanced: the log level, a fixed port for the local API, and
offering every WPPConnect Server release.

The pure halves are called directly; the dialog's and the checker's methods
run unbound on plain stubs, so nothing here opens a window.
"""

import logging
import types

import pytest

import updater
from core import local_api_port, log_level, wpp_upstream
from core.utils import DEFAULT_SETTINGS
from ui.dialogs import advanced_tab
from ui.dialogs.advanced_tab import AdvancedTabMixin


# ── Log level ─────────────────────────────────────────────────────────────────

class TestLogLevel:
    def test_info_is_the_default_and_what_was_always_logged(self):
        assert DEFAULT_SETTINGS["advanced"]["log_level"] == log_level.DEFAULT == "info"
        assert log_level.level_name({}) == "info"

    @pytest.mark.parametrize("stored", [None, "", "verbose", 10, ["debug"], " Debug "])
    def test_a_hand_edited_value_never_silences_the_log(self, stored):
        name = log_level.level_name({"advanced": {"log_level": stored}})
        assert name == ("debug" if stored == " Debug " else "info")

    def test_every_offered_level_maps_to_logging(self):
        assert [log_level.numeric_level(n) for n in log_level.LEVELS] == [
            logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR]

    def test_apply_moves_only_the_given_logger(self):
        logger = logging.getLogger("winzapp.test.advanced")
        logger.setLevel(logging.INFO)
        assert log_level.apply_log_level({"advanced": {"log_level": "error"}}, logger) == "error"
        assert logger.level == logging.ERROR
        assert log_level.apply_log_level({}, logger) == "info"
        assert logger.level == logging.INFO

    def test_the_choice_lists_every_level_with_its_own_label(self):
        labels = advanced_tab.level_labels(lambda key: key)
        assert labels == [advanced_tab.LEVEL_LABEL_KEYS[n] for n in log_level.LEVELS]
        assert advanced_tab.level_index({"advanced": {"log_level": "warning"}}) == 2


# ── Fixed local API port ──────────────────────────────────────────────────────

class TestLocalApiPort:
    def test_automatic_by_default(self):
        assert DEFAULT_SETTINGS["advanced"]["local_api_port"] == 0
        assert local_api_port.fixed_port(DEFAULT_SETTINGS) is None

    @pytest.mark.parametrize("value,expected", [
        (7000, 7000), ("7000", 7000), (" 6400 ", 6400), (1024, 1024), (65535, 65535),
        (0, None), (1023, None), (65536, None), ("abc", None), ("70.0", None),
        ("", None), (True, None), (None, None), (-1, None),
    ])
    def test_parse(self, value, expected):
        assert local_api_port.parse_port(value) == expected

    def test_the_port_never_travels_to_another_machine(self):
        from core.settings_transfer import EXCLUDED_KEYS
        assert (local_api_port.SECTION, local_api_port.KEY) in EXCLUDED_KEYS


def _port_window(settings, conn, *, registry=True, free=True, ours=False, peers=()):
    saved = []
    window = types.SimpleNamespace(
        settings=settings, account_id="acc" if registry else None,
        registry=object() if registry else None, global_dir=None,
        save_settings=lambda: saved.append(dict(settings.get("connection", {}))),
        _is_port_free=(free if callable(free) else (lambda port: free)),
        _port_runs_our_api=lambda port: ours,
        _peer_node_ports=lambda any_port=False: list(peers),
    )
    from main_window.wpp_server import WppServerMixin
    window._resolve_wpp_port = WppServerMixin._resolve_wpp_port.__get__(window)
    return window, saved


class TestResolveWithAFixedPort:
    def test_a_fixed_port_wins_over_the_allocation_and_is_saved(self):
        settings = {"advanced": {"local_api_port": 7000}, "connection": {"wpp_port": 6300}}
        window, saved = _port_window(settings, settings["connection"])
        assert window._resolve_wpp_port(settings["connection"]) == 7000
        assert settings["connection"]["wpp_port"] == 7000, "peers must see it"
        assert saved

    def test_also_without_a_registry(self):
        settings = {"advanced": {"local_api_port": 7001}, "connection": {}}
        window, _ = _port_window(settings, settings["connection"], registry=False)
        assert window._resolve_wpp_port(settings["connection"]) == 7001

    def test_a_custom_api_keeps_its_own_port(self):
        settings = {"advanced": {"local_api_port": 7000},
                    "connection": {"wpp_custom_api": True, "wpp_port": 8443}}
        window, _ = _port_window(settings, settings["connection"])
        assert window._resolve_wpp_port(settings["connection"]) == 8443

    def test_a_fixed_port_another_program_holds_is_not_used_this_run(self):
        """Reported by the reviewer: WinZapp "reused" that program as its API
        and could kill it at exit when it was a node.exe."""
        settings = {"advanced": {"local_api_port": 7000}, "connection": {"wpp_port": 6305}}
        window, _ = _port_window(settings, settings["connection"], registry=False, free=False)
        assert window._resolve_wpp_port(settings["connection"]) == 6305
        assert settings["advanced"]["local_api_port"] == 7000, "tried again next start"

    def test_a_fixed_port_held_by_our_own_node_is_reused(self):
        settings = {"advanced": {"local_api_port": 7000}, "connection": {}}
        window, _ = _port_window(settings, settings["connection"], registry=False,
                                 free=False, ours=True)
        assert window._resolve_wpp_port(settings["connection"]) == 7000

    def test_a_rejected_fixed_port_inside_the_window_is_not_kept_as_the_saved_one(self):
        """Reviewer, second round: the fixed port is also the saved
        connection port, and inside 6300-6349 the automatic path kept it."""
        settings = {"advanced": {"local_api_port": 6310}, "connection": {"wpp_port": 6310}}
        window, _ = _port_window(settings, settings["connection"], registry=False,
                                 free=lambda port: port != 6310)
        assert window._resolve_wpp_port(settings["connection"]) not in (6310, None)

    def test_keep_fixed_asks_for_the_identity_only_of_a_busy_port(self):
        asked = []
        ours = lambda port: asked.append(port) or True
        assert local_api_port.keep_fixed(7000, (), lambda port: True, ours) and asked == []
        assert local_api_port.keep_fixed(7000, (), lambda port: False, ours) and asked == [7000]
        assert not local_api_port.keep_fixed(7000, (), lambda port: False, lambda port: False)

    def test_another_accounts_port_is_never_kept(self):
        """Its Node answers /winzapp/identity with our installation id too."""
        assert not local_api_port.keep_fixed(6301, [6301], lambda port: True, lambda port: True)

    def test_identity_of_our_install_only(self):
        assert local_api_port.identity_is_ours(
            {"protocol_version": 1, "installation_id": "abc"}, "abc")
        assert not local_api_port.identity_is_ours(
            {"protocol_version": 1, "installation_id": "other"}, "abc")
        assert not local_api_port.identity_is_ours({"installation_id": "abc"}, "abc")
        assert not local_api_port.identity_is_ours(None, "abc")
        assert not local_api_port.identity_is_ours({"protocol_version": 1, "installation_id": ""}, "")

    def test_automatic_is_unchanged(self):
        settings = {"advanced": {"local_api_port": 0}, "connection": {"wpp_port": 6305}}
        window, _ = _port_window(settings, settings["connection"], registry=False)
        assert window._resolve_wpp_port(settings["connection"]) == 6305


# ── Every WPPConnect Server release ───────────────────────────────────────────

class TestWppUpstream:
    def test_off_by_default_and_install_wide(self):
        from app_settings import _DEFAULTS, _GENERAL_GLOBAL
        assert DEFAULT_SETTINGS["general"][wpp_upstream.SETTING] is False
        assert _DEFAULTS[wpp_upstream.SETTING] is False
        assert wpp_upstream.SETTING in _GENERAL_GLOBAL
        assert wpp_upstream.enabled(DEFAULT_SETTINGS) is False
        assert wpp_upstream.enabled({"general": {"wpp_all_updates": "yes"}}) is False
        assert wpp_upstream.enabled({"general": {"wpp_all_updates": True}}) is True

    def test_newest_tag_keeps_plain_releases_only(self):
        assert wpp_upstream.newest_tag("v2.10.59", "2.10.61", "") == "v2.10.61"
        assert wpp_upstream.newest_tag("v2.10.59", "../evil", "2.11.0-beta") == "v2.10.59"
        assert wpp_upstream.newest_tag("", None) == ""

    def test_fetch_answers_the_tag_or_nothing(self, monkeypatch):
        from core import tls_trust

        class _Resp:
            def __init__(self, data):
                self.data = data

            def raise_for_status(self):
                pass

            def json(self):
                return self.data

        monkeypatch.setattr(tls_trust, "get", lambda *a, **k: _Resp({"tag_name": "v2.10.70"}))
        assert wpp_upstream.fetch_latest_tag("ua") == "v2.10.70"
        monkeypatch.setattr(tls_trust, "get", lambda *a, **k: _Resp({"tag_name": "main"}))
        assert wpp_upstream.fetch_latest_tag("ua") == ""

        def _down(*a, **k):
            raise OSError("offline")
        monkeypatch.setattr(tls_trust, "get", _down)
        assert wpp_upstream.fetch_latest_tag("ua") == ""


def _checker(settings, latest="v2.10.70"):
    checker = types.SimpleNamespace(_mw=types.SimpleNamespace(settings=settings))
    checker._offered_tag = updater.WppUpdateChecker._offered_tag.__get__(checker)
    return checker


class TestOfferedTag:
    def test_off_offers_the_approved_release_and_never_asks_github(self, monkeypatch):
        monkeypatch.setattr(wpp_upstream, "fetch_latest_tag",
                            lambda *a, **k: pytest.fail("GitHub asked with the option off"))
        assert _checker({"general": {}})._offered_tag("v2.10.58", "v2.10.59") == "v2.10.59"

    def test_on_offers_the_newer_of_approved_and_upstream(self, monkeypatch):
        monkeypatch.setattr(wpp_upstream, "fetch_latest_tag", lambda *a, **k: "v2.10.70")
        on = _checker({"general": {"wpp_all_updates": True}})
        assert on._offered_tag("v2.10.58", "v2.10.59") == "v2.10.70"
        assert on._offered_tag("v2.10.58", "") == "v2.10.70"
        monkeypatch.setattr(wpp_upstream, "fetch_latest_tag", lambda *a, **k: "")
        assert on._offered_tag("v2.10.58", "v2.10.59") == "v2.10.59"

    def test_never_below_the_pin(self):
        # The offer goes through wpp_update_target(), which floors it at the pin.
        assert updater.wpp_update_target("2.10.58", "2.10.58", "v2.10.40") is None
        assert updater.wpp_update_target("2.10.58", "2.10.58", "v2.10.70") == ("v2.10.70", False)


class TestUnvettedPrompt:
    def test_an_unvetted_release_is_asked_about_with_its_own_warning(self, monkeypatch):
        asked = []
        mw = types.SimpleNamespace(
            _shutting_down=False, _wpp_updating=False, _wpp_staging=None,
            wpp_update_may_run_now=lambda: True,
            i18n=types.SimpleNamespace(t=lambda key: key),
            output=lambda *a, **k: None,
        )
        checker = types.SimpleNamespace(
            _mw=mw, _other_account_busy=lambda: False, _claim_prompt=lambda v: True,
            _release_prompt=lambda: None, _schedule_retry=lambda *a: None, _declined_tag=None)
        monkeypatch.setattr(updater, "message_box",
                            lambda parent, message, title, *a, **k: asked.append(message) or updater.wx.NO)
        prompt = updater.WppUpdateChecker._prompt_update.__get__(checker)
        prompt("2.10.58", "2.10.70", "v2.10.70", False, False)
        prompt("2.10.58", "2.10.59", "v2.10.59", False)
        assert asked == ["wpp_update_unvetted_msg", "wpp_update_available_msg"]


# ── The page ──────────────────────────────────────────────────────────────────

class _Value:
    def __init__(self, value=None):
        self.value = value
        self.enabled = True

    def GetValue(self):
        return self.value

    def SetValue(self, value):
        self.value = value

    def SetSelection(self, index):
        self.value = index

    def GetSelection(self):
        return self.value

    def Enable(self, on=True):
        self.enabled = on


def _page(settings):
    page = types.SimpleNamespace(
        main_window=types.SimpleNamespace(settings=settings),
        _log_level_choice=_Value(), _fixed_port_check=_Value(False),
        _fixed_port_field=_Value(""),
    )
    for name in ("_load_advanced_values", "_apply_advanced_values", "_sync_fixed_port_field",
                 "_custom_api_on"):
        setattr(page, name, getattr(AdvancedTabMixin, name).__get__(page))
    return page


class TestThePage:
    def test_loads_and_saves_the_same_keys(self, monkeypatch):
        monkeypatch.setattr(log_level, "apply_log_level", lambda s, logger=None: "debug")
        settings = {"advanced": {"log_level": "warning", "local_api_port": 7000}}
        page = _page(settings)
        page._load_advanced_values()
        assert page._log_level_choice.value == 2
        assert page._fixed_port_check.value is True and page._fixed_port_field.value == "7000"
        assert page._fixed_port_field.enabled

        page._log_level_choice.value = 0
        page._fixed_port_check.value = False
        page._apply_advanced_values()
        assert settings["advanced"] == {"log_level": "debug", "local_api_port": 0}

    def test_automatic_shows_an_empty_disabled_field(self):
        page = _page({})
        page._load_advanced_values()
        assert page._fixed_port_check.value is False
        assert page._fixed_port_field.value == "" and not page._fixed_port_field.enabled

    def test_apply_sets_the_log_level_at_once(self, monkeypatch):
        applied = []
        monkeypatch.setattr(log_level, "apply_log_level", lambda s, logger=None: applied.append(
            s["advanced"]["log_level"]))
        page = _page({})
        page._log_level_choice.value = 3
        page._fixed_port_check.value = True
        page._fixed_port_field.value = "6400"
        page._apply_advanced_values()
        assert applied == ["error"]
        assert page.main_window.settings["advanced"]["local_api_port"] == 6400


class TestTheFixedPortFollowsTheCustomApi:
    def test_off_with_the_custom_api(self):
        page = _page({})
        page._fixed_port_check = _Value(True)
        page._custom_api_check = _Value(True)
        page._sync_fixed_port_field()
        assert not page._fixed_port_check.enabled and not page._fixed_port_field.enabled
        page._custom_api_check.value = False
        page._sync_fixed_port_field()
        assert page._fixed_port_check.enabled and page._fixed_port_field.enabled


    def test_a_connection_page_never_opened_is_read_from_the_store(self):
        """Opening Advanced no longer builds Connection to read one box."""
        page = _page({})
        page.main_window.install_wide_values = lambda: {"connection": {"wpp_custom_api": True}}
        page._sync_fixed_port_field()
        assert not page._fixed_port_check.enabled
        page.main_window.install_wide_values = lambda: {}
        page._sync_fixed_port_field()
        assert page._fixed_port_check.enabled


def test_a_question_opens_over_the_window_the_user_is_in():
    from ui.dialogs.settings_pages import owner_window
    settings, sub = object(), object()
    assert owner_window(sub, settings) is sub
    assert owner_window(None, settings) is settings


def test_the_privacy_notice_names_fields_without_their_mnemonic():
    import inspect
    from ui.dialogs import whatsapp_privacy_panel
    source = inspect.getsource(whatsapp_privacy_panel)
    line = source[source.index('"wa_privacy_unrecognized_fields"'):][:200]
    assert '.replace("&", "")' in line


def test_with_accounts_a_rejected_fixed_port_is_taken_off_the_allocation(tmp_path):
    """The registry path: the rejected port goes to resolve_account_port() as
    a peer's, so even saved and inside the window it is not kept busy."""
    settings = {"advanced": {"local_api_port": 6310}, "connection": {"wpp_port": 6310}}
    window, _ = _port_window(settings, settings["connection"],
                             free=lambda port: port != 6310)
    window.global_dir = str(tmp_path)
    port = window._resolve_wpp_port(settings["connection"])
    assert port != 6310 and 6300 <= port < 6350
    assert settings["connection"]["wpp_port"] == port
