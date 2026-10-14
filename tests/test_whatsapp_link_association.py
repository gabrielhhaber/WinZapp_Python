"""Registering WinZapp for whatsapp: links, and a link arriving from Windows.

Three pieces, each tested without touching the machine running the tests:

* core/whatsapp_link_association.py against a dict standing in for the
  registry (the real one is never opened here);
* the IPC command a second launch uses to pass the link to the running process;
* MainWindow's handling of that link, bound onto a stub.
"""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import ipc
from core import whatsapp_link_association as association
from core.group_invitation_link import MAX_LINK_LENGTH, invitation_code, invitation_link_argument
from main_window import group_invitation_links
from main_window.group_invitation_links import GroupInvitationLinksMixin

CODE = "AbCdEfGhIjKlMnOpQrStUv"
WEB_LINK = "https://chat.whatsapp.com/" + CODE
APP_LINK = "whatsapp://chat?code=" + CODE
COMMAND = '"C:\\Apps\\WinZapp\\WinZapp.exe" "%1"'
SCHEME_COMMAND = "Software\\Classes\\whatsapp\\shell\\open\\command"
HANDLER_COMMAND = "Software\\Classes\\WinZapp.WhatsAppLink\\shell\\open\\command"


class FakeRegistry:
    """{(key path, value name): text} with the three operations the module uses."""

    def __init__(self, values=None, machine=None):
        self.values = dict(values or {})
        self.machine = machine  # a handler installed for every user (HKLM)

    def merged_scheme_command(self):
        return self.values.get((SCHEME_COMMAND, "")) or self.machine

    def get(self, path, name):
        return self.values.get((path, name))

    def set(self, path, name, value):
        self.values[(path, name)] = value

    def delete(self, path, name=None):
        if name is not None:
            self.values.pop((path, name), None)
            return
        for key in [k for k in self.values if k[0] == path or k[0].startswith(path + "\\")]:
            del self.values[key]


# ── the command line ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("argv,expected", [
    (["WinZapp.exe", APP_LINK], APP_LINK),
    (["WinZapp.exe", "--startup-source", "user", WEB_LINK + "?mode=x"], WEB_LINK + "?mode=x"),
    (["python.exe", "main.py", "--account", "abc", " " + APP_LINK + " "], APP_LINK),
    (["WinZapp.exe"], ""),
    (["WinZapp.exe", "--background"], ""),
    (["WinZapp.exe", "whatsapp://send?phone=5511999999999"], ""),
    (["WinZapp.exe", "https://example.com/" + CODE], ""),
    # argv[0] is the program, never an argument, whatever it looks like.
    ([APP_LINK], ""),
])
def test_the_invitation_link_on_a_command_line(argv, expected):
    assert invitation_link_argument(argv) == expected


def test_an_absurdly_long_argument_is_not_a_link():
    assert invitation_code(WEB_LINK + "?" + "a" * MAX_LINK_LENGTH) == ""


def test_the_handler_command_of_a_compiled_build_and_of_a_source_checkout():
    assert association.handler_command(
        "C:\\Apps\\WinZapp\\WinZapp.exe", "ignored", frozen=True
    ) == '"C:\\Apps\\WinZapp\\WinZapp.exe" "%1"'
    command = association.handler_command("C:\\src\\client\\main.py", "C:\\py\\python.exe",
                                          frozen=False)
    assert command == '"C:\\py\\python.exe" "C:\\src\\client\\main.py" "%1"'


# ── the registry rules ───────────────────────────────────────────────────────


def test_registering_makes_winzapp_a_handler_and_lists_it_for_default_apps():
    registry = FakeRegistry()

    assert association.is_registered(COMMAND, registry) is False
    association.register(COMMAND, registry)

    assert association.is_registered(COMMAND, registry) is True
    assert registry.get(HANDLER_COMMAND, "") == COMMAND
    assert registry.get("Software\\Classes\\WinZapp.WhatsAppLink", "URL Protocol") == ""
    assert registry.get("Software\\WinZapp\\Capabilities\\URLAssociations", "whatsapp") \
        == "WinZapp.WhatsAppLink"
    assert registry.get("Software\\RegisteredApplications", "WinZapp") \
        == "Software\\WinZapp\\Capabilities"


def test_a_free_scheme_is_taken_and_marked_as_winzapps():
    registry = FakeRegistry()
    association.register(COMMAND, registry)

    assert registry.get(SCHEME_COMMAND, "") == COMMAND
    assert registry.get("Software\\Classes\\whatsapp", "URL Protocol") == ""
    assert registry.get("Software\\Classes\\whatsapp", "WinZappOwned") == "1"


def test_another_programs_handler_is_never_overwritten():
    """Someone else already answers whatsapp: links. WinZapp still registers
    itself as a candidate, and Windows' "Default apps" is where the user picks."""
    other = '"C:\\Other\\WhatsApp.exe" "%1"'
    registry = FakeRegistry({(SCHEME_COMMAND, ""): other})

    association.register(COMMAND, registry)

    assert registry.get(SCHEME_COMMAND, "") == other
    assert registry.get("Software\\Classes\\whatsapp", "WinZappOwned") is None
    assert association.is_registered(COMMAND, registry) is True

    association.unregister(registry)
    assert registry.get(SCHEME_COMMAND, "") == other, "not ours: left alone"


def test_a_handler_installed_for_every_user_is_never_overridden():
    """WhatsApp Desktop registers in HKLM; this user's key would hide it."""
    registry = FakeRegistry(machine='"C:\\Program Files\\WhatsApp\\WhatsApp.exe" "%1"')

    association.register(COMMAND, registry)

    assert registry.get(SCHEME_COMMAND, "") is None
    assert registry.get("Software\\Classes\\whatsapp", "WinZappOwned") is None
    assert association.is_registered(COMMAND, registry) is True


def test_unregistering_removes_exactly_what_registering_wrote():
    before = {("Software\\RegisteredApplications", "SomeOtherApp"): "Software\\Other\\Capabilities",
              ("Software\\Classes\\.txt", ""): "txtfile"}
    registry = FakeRegistry(before)

    association.register(COMMAND, registry)
    association.unregister(registry)

    assert registry.values == before
    assert association.is_registered(COMMAND, registry) is False


def test_a_moved_installation_reads_as_not_registered_and_registering_repairs_it():
    registry = FakeRegistry()
    association.register('"C:\\Old\\WinZapp.exe" "%1"', registry)

    assert association.is_registered(COMMAND, registry) is False
    association.register(COMMAND, registry)

    assert association.is_registered(COMMAND, registry) is True
    assert registry.get(SCHEME_COMMAND, "") == COMMAND, "its own scheme key follows"


def test_the_fake_registry_is_what_is_used_never_the_real_one(monkeypatch):
    """A passed registry also means "do not notify the shell": the tests must
    not poke Explorer on the developer's desktop."""
    notified = Mock()
    monkeypatch.setattr(association, "_notify_shell", notified)
    monkeypatch.setattr(association, "_UserRegistry",
                        Mock(side_effect=AssertionError("real registry opened")))

    registry = FakeRegistry()
    association.register(COMMAND, registry)
    association.unregister(registry)

    notified.assert_not_called()


# ── passing the link to the running process ──────────────────────────────────


def _listener(ready=True, **kwargs):
    return ipc.IpcListener("unused", "account", on_activate=Mock(), on_quit=Mock(),
                           window_ready_predicate=lambda: ready, **kwargs)


def test_a_link_from_a_second_launch_is_acknowledged_and_delivered():
    opened = Mock()
    listener = _listener(on_open_link=opened)

    replies = listener._handle_message(
        json.dumps({"cmd": "open_link", "request_id": "r1", "link": APP_LINK}))

    assert [json.loads(r) for r in replies] == [{"request_id": "r1", "ack": True}]
    opened.assert_called_once_with(APP_LINK)


@pytest.mark.parametrize("link", ["https://example.com/x", "whatsapp://send?phone=1", "", None,
                                  123, WEB_LINK + "/extra"])
def test_only_a_group_invitation_link_is_passed_on(link):
    """The pipe is restricted to the current user, and the command still only
    opens a dialog the user has to confirm; anything else is dropped unacked,
    so the second launch falls back to a plain activation."""
    opened = Mock()
    listener = _listener(on_open_link=opened)

    replies = listener._handle_message(
        json.dumps({"cmd": "open_link", "request_id": "r1", "link": link}))

    assert replies == []
    opened.assert_not_called()


def test_a_listener_without_the_callback_ignores_the_command():
    assert _listener()._handle_message(
        json.dumps({"cmd": "open_link", "request_id": "r1", "link": APP_LINK})) == []


def test_a_link_that_arrives_before_the_window_exists_is_kept_for_it():
    opened = Mock()
    state = {"ready": False}
    listener = ipc.IpcListener("unused", "account", on_activate=Mock(), on_quit=Mock(),
                               window_ready_predicate=lambda: state["ready"],
                               on_open_link=opened)

    listener._handle_message(json.dumps({"cmd": "open_link", "request_id": "r1", "link": APP_LINK}))
    opened.assert_not_called()

    state["ready"] = True
    listener.flush_queue()
    opened.assert_called_once_with(APP_LINK)
    listener.flush_queue()
    opened.assert_called_once()


def test_the_request_carries_the_link_and_reports_the_acknowledgement(monkeypatch):
    sent = {}

    def fake_send(global_dir, account_id, req, timeout):
        sent.update(req, account=account_id)
        return [{"request_id": req["request_id"], "ack": True}]

    monkeypatch.setattr(ipc, "_send", fake_send)
    assert ipc.request_open_link("gd", "account", APP_LINK) is True
    assert sent["cmd"] == "open_link" and sent["link"] == APP_LINK and sent["account"] == "account"

    monkeypatch.setattr(ipc, "_send", lambda *args: None)
    assert ipc.request_open_link("gd", "account", APP_LINK) is False


def test_the_start_passes_the_link_on_and_falls_back_to_activating():
    """Structural: __main__ cannot run in a test. A second launch hands the
    link over (plain activation when it is refused or there is none); a first
    launch gives it to its own window; a source checkout moves to its folder
    first, because its data lives under the working directory."""
    source = (Path(__file__).resolve().parents[1] / "client" / "main.py").read_text(
        encoding="utf-8")
    assert "_invitation_link = invitation_link_argument(sys.argv)" in source
    assert "ipc.request_open_link(" in source
    assert "ipc.request_activate(gd, _account_id, source=startup_source)" in source
    assert "wx.CallAfter(frame.open_group_invitation_link, _invitation_link)" in source
    chdir = source.index("os.chdir(os.path.dirname(os.path.abspath(__file__)))")
    assert chdir < source.index("gd = app_paths.global_dir()")


# ── the running window receiving a link ──────────────────────────────────────


class _Window:
    open_group_invitation_link = GroupInvitationLinksMixin.open_group_invitation_link
    _apply_whatsapp_link_association = GroupInvitationLinksMixin._apply_whatsapp_link_association
    _INVITATION_LINK_WAIT_SECONDS = GroupInvitationLinksMixin._INVITATION_LINK_WAIT_SECONDS
    _INVITATION_LINK_POLL_MS = GroupInvitationLinksMixin._INVITATION_LINK_POLL_MS

    def __init__(self, connected=True):
        self._wa_connected = connected
        self.i18n = SimpleNamespace(t=lambda key: key)
        self.app_name = "WinZapp"
        self.spoken = []
        self.restored = 0

    def output(self, text):
        self.spoken.append(text)

    def restore_window(self):
        self.restored += 1


@pytest.fixture
def join(monkeypatch):
    opened = Mock(return_value=True)
    import ui.dialogs.group_join_dialog as dialog_module
    monkeypatch.setattr(dialog_module, "join_group_by_link", opened)
    return opened


@pytest.fixture
def later(monkeypatch):
    calls = []
    monkeypatch.setattr(group_invitation_links.wx, "CallLater",
                        lambda ms, fn, *args: calls.append((ms, fn, args)))
    return calls


def test_a_connected_window_comes_forward_and_shows_the_join_dialog(join, later):
    window = _Window()

    window.open_group_invitation_link(APP_LINK)

    assert window.restored == 1
    join.assert_called_once_with(window, window, APP_LINK)
    assert later == []


@pytest.mark.parametrize("text", ["https://example.com", "whatsapp://send?phone=1", "", None])
def test_text_that_is_not_an_invitation_link_does_nothing(join, later, text):
    window = _Window()

    window.open_group_invitation_link(text)

    join.assert_not_called()
    assert window.restored == 0 and window.spoken == [] and later == []


def test_a_link_that_starts_winzapp_waits_for_the_connection(join, later):
    window = _Window(connected=False)

    window.open_group_invitation_link(APP_LINK)

    join.assert_not_called()
    assert window.spoken == ["group_join_link_waiting"]
    ms, fn, args = later.pop()
    assert ms == window._INVITATION_LINK_POLL_MS and args[0] == APP_LINK

    # Still down on the next look: waits again, without saying it twice.
    window.open_group_invitation_link(*args)
    assert window.spoken == ["group_join_link_waiting"]
    ms, fn, args = later.pop()

    window._wa_connected = True
    window.open_group_invitation_link(*args)
    join.assert_called_once_with(window, window, APP_LINK)


def test_the_wait_ends_and_says_so(join, later, monkeypatch):
    window = _Window(connected=False)
    clock = {"now": 1000.0}
    monkeypatch.setattr(group_invitation_links.time, "monotonic", lambda: clock["now"])

    window.open_group_invitation_link(APP_LINK)
    _ms, _fn, args = later.pop()
    clock["now"] += window._INVITATION_LINK_WAIT_SECONDS + 1
    window.open_group_invitation_link(*args)

    assert window.spoken == ["group_join_link_waiting", "group_join_link_not_connected"]
    assert later == []
    join.assert_not_called()


def test_a_second_link_does_not_stack_a_dialog_on_the_open_one(later, monkeypatch):
    window = _Window()
    inner = []

    def fake_join(mw, parent, link):
        window.open_group_invitation_link(WEB_LINK)  # arrives while the dialog is up
        inner.append(link)

    import ui.dialogs.group_join_dialog as dialog_module
    monkeypatch.setattr(dialog_module, "join_group_by_link", fake_join)

    window.open_group_invitation_link(APP_LINK)

    assert inner == [APP_LINK]
    assert window._invitation_link_dialog_open is False


def test_the_settings_switch_registers_and_says_windows_may_ask(monkeypatch):
    window = _Window()
    register, unregister, box = Mock(), Mock(), Mock()
    monkeypatch.setattr(group_invitation_links.whatsapp_link_association, "register", register)
    monkeypatch.setattr(group_invitation_links.whatsapp_link_association, "unregister", unregister)
    monkeypatch.setattr(group_invitation_links.wx, "MessageBox", box)

    assert window._apply_whatsapp_link_association(True) is True
    register.assert_called_once_with()
    assert box.call_args[0][0] == "whatsapp_links_enabled_message"

    box.reset_mock()
    assert window._apply_whatsapp_link_association(False) is True
    unregister.assert_called_once_with()
    box.assert_not_called()


def test_a_registry_failure_is_reported_not_raised(monkeypatch):
    window = _Window()
    window.i18n = SimpleNamespace(t=lambda key: "{app_name}" if key == "error" else key)
    box = Mock()
    monkeypatch.setattr(group_invitation_links.whatsapp_link_association, "register",
                        Mock(side_effect=OSError("denied")))
    monkeypatch.setattr(group_invitation_links.wx, "MessageBox", box)

    assert window._apply_whatsapp_link_association(True) is False
    assert box.call_args[0][0] == "whatsapp_links_error_message"
