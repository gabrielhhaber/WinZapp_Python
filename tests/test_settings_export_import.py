"""Exporting the settings to a file, importing one, and making an import take
effect immediately (MainWindow, client/main.py).

The file dialogs are not exercised here — MainWindow is a wx.Frame, so the
methods run against a stub, which is also what keeps this off the desktop. What
is covered is everything the user can lose: a file that carries the session, an
import that half-applies a bad file, and settings that only take effect after a
restart.
"""

import copy
import json
import threading
import types

import pytest

from core import settings_transfer as transfer
from core.utils import DEFAULT_SETTINGS
from main import MainWindow


class _Panel:
    def __init__(self):
        self._audio_speed_steps = [1.0, 1.5, 2.0]
        self._audio_speed_index = 0
        self.audio_speed_btn = types.SimpleNamespace(SetLabel=lambda label: None)
        self.conversation = {"remoteJid": "5511@s.whatsapp.net"}
        self.modes = []
        self.populated = 0

    def apply_message_list_mode(self, mode):
        self.modes.append(mode)

    def populate_messages(self, preserve_focus=False):
        self.populated += 1

    @staticmethod
    def _format_speed(speed):
        return f"{speed}x"


class _Tray:
    def __init__(self):
        self.removed = False
        self.destroyed = False

    def RemoveIcon(self):
        self.removed = True

    def Destroy(self):
        self.destroyed = True


class _Stub:
    export_settings_to_file = MainWindow.export_settings_to_file
    import_settings_from_file = MainWindow.import_settings_from_file
    apply_settings_live = MainWindow.apply_settings_live

    def __init__(self, settings=None):
        self.settings = settings if settings is not None else _settings()
        self.wpp_custom_api = False
        self.wpp_server = "http://127.0.0.1"
        self.wpp_ws_server = "ws://127.0.0.1"
        self.wpp_port = 6412
        self.wpp_api_key = "install-key"
        self._save_lock = threading.Lock()
        self.ws = None
        self.app_name = "WinZapp"
        self.conversations_panel = _Panel()
        self.tray_icon = None
        self._chats_ui_fp = "stale"
        self._notification_sound_cache = {"a": 1}
        self.i18n = types.SimpleNamespace(t=lambda key: key, get_language=lambda: None)
        self.saved = 0
        self.applied_live = 0
        self.steps = []
        self.hotkeys = []
        self.trays_created = 0

    # What apply_settings_live() drives.
    def _apply_configured_audio_devices(self):
        self.steps.append("audio devices")

    def load_sounds(self):
        self.steps.append("sounds")

    def apply_language_changes(self):
        self.steps.append("language")

    def _init_tray(self):
        self.trays_created += 1
        self.tray_icon = _Tray()

    def set_global_hotkey(self, vk, mod):
        self.hotkeys.append((vk, mod))

    def stop_all_incoming_call_alerts(self):
        self.steps.append("call alerts stopped")

    def _sync_incoming_call_bar(self, message=""):
        self.steps.append("call bar")

    def add_chats_to_ui(self):
        self.steps.append("chat list")

    def save_settings(self):
        self.saved += 1


class _ImportStub(_Stub):
    """Separates "was it applied" from "what did applying do"."""

    def apply_settings_live(self):
        self.applied_live += 1


def _settings():
    settings = copy.deepcopy(DEFAULT_SETTINGS)
    settings["privateinfo"] = {"WA_token_protected": "gAAAA-secret", "paired": True}
    settings["connection"].update({"wpp_port": 6412, "wpp_api_key": "install-key"})
    settings["general"]["language"] = "pt-BR"
    return settings


def _export_file(tmp_path, changes=None, name="exported.json"):
    """A real export file, optionally with some values changed first."""
    settings = _settings()
    for section, values in (changes or {}).items():
        settings.setdefault(section, {}).update(values)
    path = tmp_path / name
    path.write_text(json.dumps(transfer.build_export(settings, "1.1.2.0")), encoding="utf-8")
    return str(path)


class TestExporting:
    def test_it_writes_a_file_that_imports_back(self, tmp_path):
        stub = _Stub()
        path = str(tmp_path / "mine.json")

        assert stub.export_settings_to_file(path) == ""

        payload = json.loads((tmp_path / "mine.json").read_text(encoding="utf-8"))
        settings, error = transfer.read_export(payload)
        assert error == "" and settings["general"]["language"] == "pt-BR"

    def test_the_session_is_not_in_the_file(self, tmp_path):
        stub = _Stub()
        path = str(tmp_path / "mine.json")
        stub.export_settings_to_file(path)

        written = (tmp_path / "mine.json").read_text(encoding="utf-8")
        assert "secret" not in written and "privateinfo" not in written
        # The bundled API: this install’s own port and REST key.
        assert "install-key" not in written and "6412" not in written

    def test_a_folder_that_cannot_be_written_says_so(self, tmp_path):
        stub = _Stub()
        assert stub.export_settings_to_file(
            str(tmp_path / "nope" / "mine.json")) == "settings_export_failed"


class TestACustomApiTravels:
    def _custom_file(self, tmp_path):
        return _export_file(tmp_path, {"connection": {
            "wpp_custom_api": True, "wpp_server": "https://api.exemplo.com",
            "wpp_ws_server": "wss://api.exemplo.com", "wpp_port": 8443,
            "wpp_api_key": "minha-chave"}})

    def test_the_export_carries_the_server_the_person_runs(self, tmp_path):
        stub = _Stub()
        stub.settings["connection"].update({"wpp_custom_api": True,
                                            "wpp_api_key": "minha-chave"})
        path = str(tmp_path / "custom.json")
        stub.export_settings_to_file(path)

        written = (tmp_path / "custom.json").read_text(encoding="utf-8")
        assert "minha-chave" in written and "6412" in written

    def test_importing_it_points_this_install_at_that_server(self, tmp_path):
        stub = _Stub()      # the real apply_settings_live(), not the counter
        asked = []
        stub.import_settings_from_file(
            self._custom_file(tmp_path),
            confirm_api_change=lambda server: asked.append(server) or True)

        assert asked == [{"server": "https://api.exemplo.com:8443",
                          "ws_server": "wss://api.exemplo.com:8443"}]

        assert stub.wpp_server == "https://api.exemplo.com"
        assert stub.wpp_port == 8443
        assert stub.wpp_api_key == "minha-chave"
        connection = stub.settings["connection"]
        assert connection["wpp_custom_api"] is True
        assert connection["wpp_server"] == "https://api.exemplo.com"
        assert connection["wpp_port"] == 8443
        assert connection["wpp_api_key"] == "minha-chave"


class TestMovingToAnotherApiIsAskedSeparately:
    """Every request carries the session token to the API's server, so an
    import that changes it is confirmed on its own, naming the server — and a
    No keeps the connection while everything else is still imported."""

    def _custom_file(self, tmp_path):
        return TestACustomApiTravels()._custom_file(tmp_path)

    def test_declining_keeps_the_connection_and_imports_the_rest(self, tmp_path):
        stub = _Stub()
        before = copy.deepcopy(stub.settings["connection"])
        error, applied = stub.import_settings_from_file(
            self._custom_file(tmp_path), confirm_api_change=lambda server: False)

        assert error == "" and applied
        assert stub.settings["connection"] == before
        assert stub.wpp_server == "http://127.0.0.1"
        assert stub.wpp_api_key == "install-key"

    def test_without_a_callback_the_connection_is_never_moved(self, tmp_path):
        stub = _Stub()
        before = copy.deepcopy(stub.settings["connection"])
        stub.import_settings_from_file(self._custom_file(tmp_path))
        assert stub.settings["connection"] == before

    def test_a_callback_that_raises_counts_as_no(self, tmp_path):
        stub = _Stub()
        before = copy.deepcopy(stub.settings["connection"])

        def _boom(server):
            raise RuntimeError("dialog failed")

        error, _applied = stub.import_settings_from_file(
            self._custom_file(tmp_path), confirm_api_change=_boom)
        assert error == ""
        assert stub.settings["connection"] == before

    def test_accepting_it_reconnects_the_live_socket_to_the_new_address(self, tmp_path, monkeypatch):
        """REST reads the new address on the next request; the Socket.IO
        connection would otherwise stay on the old one until a restart."""
        started = []

        class _InlineThread:
            def __init__(self, target=None, daemon=None, **kw):
                self._target = target

            def start(self):
                started.append(self._target)

        monkeypatch.setattr("main.threading.Thread", _InlineThread)
        stub = _Stub()
        stub.ws = object()
        stub.connect_websocket = lambda: None
        stub.import_settings_from_file(self._custom_file(tmp_path),
                                       confirm_api_change=lambda change: True)
        assert started == [stub.connect_websocket]

    def test_declining_leaves_the_socket_alone(self, tmp_path, monkeypatch):
        started = []
        monkeypatch.setattr("main.threading.Thread",
                            lambda *a, **kw: types.SimpleNamespace(start=lambda: started.append(1)))
        stub = _Stub()
        stub.ws = object()
        stub.import_settings_from_file(self._custom_file(tmp_path),
                                       confirm_api_change=lambda change: False)
        assert started == []

    def test_a_file_that_changes_nothing_about_the_api_asks_nothing(self, tmp_path):
        stub = _Stub()
        asked = []
        stub.import_settings_from_file(
            _export_file(tmp_path), confirm_api_change=lambda s: asked.append(s) or True)
        assert asked == []


class TestImporting:
    def test_it_applies_saves_and_takes_effect(self, tmp_path):
        stub = _ImportStub()
        path = _export_file(tmp_path, {"user_interface": {"messages_page_size": 42}})

        error, applied = stub.import_settings_from_file(path)

        assert error == "" and applied
        assert stub.settings["user_interface"]["messages_page_size"] == 42
        assert stub.saved == 1 and stub.applied_live == 1

    def test_the_settings_dict_is_the_same_object(self, tmp_path):
        """Panels and helpers hold a reference to it; replacing it would leave
        half the app reading the old one."""
        stub = _ImportStub()
        settings = stub.settings
        stub.import_settings_from_file(_export_file(tmp_path))
        assert stub.settings is settings

    def test_this_install_keeps_its_own_session(self, tmp_path):
        stub = _ImportStub()
        path = _export_file(tmp_path, {"general": {"language": "pl"}})

        stub.import_settings_from_file(path)

        assert stub.settings["general"]["language"] == "pl"
        assert stub.settings["privateinfo"] == {"WA_token_protected": "gAAAA-secret",
                                                "paired": True}
        assert stub.settings["connection"]["wpp_port"] == 6412
        assert stub.settings["connection"]["wpp_api_key"] == "install-key"

    @pytest.mark.parametrize("content, expected", [
        ("{}", "settings_import_not_an_export"),
        ('{"format": "other", "settings": {}}', "settings_import_not_an_export"),
        ("not json at all", "settings_import_unreadable"),
    ])
    def test_a_file_that_is_not_an_export_changes_nothing(self, tmp_path, content, expected):
        stub = _ImportStub()
        before = copy.deepcopy(stub.settings)
        path = tmp_path / "other.json"
        path.write_text(content, encoding="utf-8")

        error, applied = stub.import_settings_from_file(str(path))

        assert (error, applied) == (expected, 0)
        assert stub.settings == before and stub.saved == 0 and stub.applied_live == 0

    def test_a_missing_file_says_so(self, tmp_path):
        stub = _ImportStub()
        error, applied = stub.import_settings_from_file(str(tmp_path / "gone.json"))
        assert (error, applied) == ("settings_import_unreadable", 0)

    def test_a_newer_export_is_refused_rather_than_half_applied(self, tmp_path):
        stub = _ImportStub()
        payload = transfer.build_export(_settings())
        payload["version"] = transfer.EXPORT_VERSION + 1
        payload["settings"]["user_interface"]["messages_page_size"] = 42
        path = tmp_path / "newer.json"
        path.write_text(json.dumps(payload), encoding="utf-8")

        error, applied = stub.import_settings_from_file(str(path))

        assert (error, applied) == ("settings_import_newer_version", 0)
        assert stub.settings["user_interface"]["messages_page_size"] == 200
        assert stub.saved == 0

    def test_an_export_with_nothing_this_build_knows_saves_nothing(self, tmp_path):
        stub = _ImportStub()
        payload = {"format": transfer.EXPORT_FORMAT, "version": 1,
                   "settings": {"from_the_future": {"a": 1}}}
        path = tmp_path / "future.json"
        path.write_text(json.dumps(payload), encoding="utf-8")

        error, applied = stub.import_settings_from_file(str(path))

        assert (error, applied) == ("settings_import_nothing", 0)
        assert stub.saved == 0 and stub.applied_live == 0


class TestTakingEffect:
    def test_every_part_of_the_app_is_told(self):
        stub = _Stub()
        stub.settings["general"]["global_hotkey"] = {"vk": 0x4B, "mod": 3}
        stub.settings["user_interface"]["message_list_mode"] = "listbox"
        stub.settings["audio_playback"]["audio_default_speed"] = 2.0

        stub.apply_settings_live()

        assert "audio devices" in stub.steps and "sounds" in stub.steps
        assert "language" in stub.steps and "chat list" in stub.steps
        assert stub.hotkeys == [(0x4B, 3)]
        assert stub.conversations_panel.modes == ["listbox"]
        assert stub.conversations_panel._audio_speed_index == 2
        assert stub.conversations_panel.populated == 1
        assert stub._notification_sound_cache == {}
        assert stub._chats_ui_fp is None

    def test_the_tray_icon_follows_the_setting(self):
        stub = _Stub()
        stub.settings["general"]["show_tray_icon"] = True
        stub.apply_settings_live()
        assert stub.trays_created == 1

        tray = stub.tray_icon
        stub.settings["general"]["show_tray_icon"] = False
        stub.apply_settings_live()
        assert stub.tray_icon is None and tray.removed and tray.destroyed

    def test_call_alerts_turned_off_are_stopped_now(self):
        stub = _Stub()
        stub.settings["calls"]["alerts_enabled"] = False
        stub.apply_settings_live()
        assert "call alerts stopped" in stub.steps and "call bar" in stub.steps

    def test_the_api_the_account_talks_to_is_applied_whole(self):
        """Every URL is server and port together, authenticated with the key:
        moving one and not the others points the app at the new host on the old
        port — working again only after the restart an import promises to spare."""
        stub = _Stub()
        stub.settings["connection"].update({"wpp_server": "https://api.exemplo.com",
                                            "wpp_ws_server": "wss://api.exemplo.com",
                                            "wpp_custom_api": True,
                                            "wpp_port": 8443,
                                            "wpp_api_key": "minha-chave"})
        stub.apply_settings_live()
        assert stub.wpp_server == "https://api.exemplo.com"
        assert stub.wpp_ws_server == "wss://api.exemplo.com"
        assert stub.wpp_custom_api is True
        assert stub.wpp_port == 8443
        assert stub.wpp_api_key == "minha-chave"

    def test_the_bundled_api_keeps_the_port_this_account_was_given(self):
        """Read from the settings when they have it, kept from the running app
        when they do not — a port this account resolved but has not persisted
        must not be lost by applying an import."""
        stub = _Stub()
        stub.apply_settings_live()
        assert stub.wpp_port == 6412 and stub.wpp_api_key == "install-key"

        stub.settings["connection"].pop("wpp_port")
        stub.wpp_port = 6501
        stub.apply_settings_live()
        assert stub.wpp_port == 6501

    def test_one_step_that_fails_never_stops_the_others(self):
        """The settings are already saved by the time this runs: a part of the
        app that cannot take them must not hold back the rest."""
        stub = _Stub()

        def _boom():
            raise RuntimeError("no sound card")

        stub.load_sounds = _boom
        stub.apply_settings_live()

        assert "language" in stub.steps and "chat list" in stub.steps

    def test_it_works_before_the_window_has_a_conversation_panel(self):
        stub = _Stub()
        stub.conversations_panel = None
        stub.apply_settings_live()
        assert "chat list" in stub.steps
