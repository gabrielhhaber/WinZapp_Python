"""Regression test: resolving a Node port and actually spawning Node on it
used to be two steps separated by a real gap of wall-clock time, not one
atomic operation.

_resolve_wpp_port() checks/reserves the port during MainWindow.__init__,
but _start_wpp_background() — the thing that actually spawns Node bound to
that port — runs much later in startup, after settings/UI/sync all have
their turn. Anything can take that exact port in that window: another
program, or even this same account's own previous Node still winding down.
Node then simply fails to bind it, surfaced only as a generic "API failed
to start in time" with an EADDRINUSE buried in wppconnect.log.

MainWindow._ensure_wpp_port_still_free() re-checks right before the real
spawn and re-resolves (through the same cross-process lock + deterministic
per-account allocator client/node_ports.py already provides) if the
originally chosen port is no longer free.

MainWindow is a wx.Frame and cannot be instantiated without a running
wx.App, so the method is exercised bound to a plain stub — same approach as
tests/test_lid_merge_keeps_messages.py. node_ports.allocate_port_for_account
itself is the real, already pure-tested implementation (tests/test_node_ports.py)
— only its is_free() predicate and the account's own state are faked here.
"""

from main import MainWindow
from tests.god_modules import patch_main_global

_ensure_wpp_port_still_free = MainWindow._ensure_wpp_port_still_free


class _Stub:
    _ensure_wpp_port_still_free = MainWindow._ensure_wpp_port_still_free

    def __init__(self, wpp_port, global_dir, custom_api=False, has_registry=True,
                 account_id="acc1", busy_ports=()):
        self.settings = {"connection": {"wpp_custom_api": custom_api}}
        self.wpp_port = wpp_port
        self.global_dir = global_dir
        self.registry = object() if has_registry else None
        self.account_id = account_id if has_registry else None
        self.save_calls = 0
        self.peer_ports = []
        self._busy_ports = set(busy_ports)

    def _is_port_free(self, port):
        return port not in self._busy_ports

    def _peer_node_ports(self):
        return self.peer_ports

    def save_settings(self):
        self.save_calls += 1


class TestPortStillFree:
    def test_nothing_changes_when_the_port_is_still_bindable(self, tmp_path):
        stub = _Stub(wpp_port=6301, global_dir=str(tmp_path))

        stub._ensure_wpp_port_still_free()

        assert stub.wpp_port == 6301
        assert stub.save_calls == 0


class TestPortNowTaken:
    def test_re_resolves_to_a_different_free_port(self, tmp_path):
        stub = _Stub(wpp_port=6301, global_dir=str(tmp_path), busy_ports={6301})

        stub._ensure_wpp_port_still_free()

        assert stub.wpp_port != 6301
        assert stub._is_port_free(stub.wpp_port)

    def test_the_new_port_is_persisted(self, tmp_path):
        stub = _Stub(wpp_port=6301, global_dir=str(tmp_path), busy_ports={6301})

        stub._ensure_wpp_port_still_free()

        assert stub.save_calls == 1
        assert stub.settings["connection"]["wpp_port"] == stub.wpp_port

    def test_a_peer_ports_clash_is_still_avoided(self, tmp_path):
        """The re-resolve must still respect other accounts' persisted
        ports, not just "any bindable port" — otherwise it could hand this
        account a port a peer already reserved for itself."""
        stub = _Stub(wpp_port=6301, global_dir=str(tmp_path), busy_ports={6301})
        stub.peer_ports = [6302, 6303]

        stub._ensure_wpp_port_still_free()

        assert stub.wpp_port not in {6301, 6302, 6303}


class TestSkipsWhenNotApplicable:
    def test_custom_api_is_left_untouched(self, tmp_path):
        stub = _Stub(wpp_port=6301, global_dir=str(tmp_path), custom_api=True,
                     busy_ports={6301})

        stub._ensure_wpp_port_still_free()

        assert stub.wpp_port == 6301
        assert stub.save_calls == 0

    def test_single_account_legacy_mode_is_left_untouched(self, tmp_path):
        stub = _Stub(wpp_port=6301, global_dir=str(tmp_path), has_registry=False,
                     busy_ports={6301})

        stub._ensure_wpp_port_still_free()

        assert stub.wpp_port == 6301
        assert stub.save_calls == 0


class TestThePortIsSettledBeforeTheDialogSeesIt:
    """ApiStartupDialog stores the port handed to its constructor and polls it
    every 500ms to decide the API came up. So the re-check has to have already
    run by then, or the fix defeats itself on its own success path.

    _start_wpp_background() also calls _ensure_wpp_port_still_free(), but it is
    reached through the wx.CallAfter *inside* the dialog helper — after the
    constructor captured self.wpp_port. With a squatter on the old port the
    dialog polls the squatter and reports success while Node is still booting
    elsewhere; if the squatter leaves, it polls a dead port for the full
    5-minute budget and reports a timeout while Node is running fine. It is
    also the wrong port announced to the screen reader.
    """

    @staticmethod
    def _source():
        import inspect
        from main import MainWindow
        return inspect.getsource(MainWindow.ensure_wpp_running)

    def test_ensure_wpp_running_settles_the_port_itself(self):
        assert "self._ensure_wpp_port_still_free()" in self._source(), (
            "leaving it to _start_wpp_background() alone runs it too late"
        )

    def test_it_runs_before_the_dialog_is_constructed(self):
        src = self._source()
        recheck = src.index("self._ensure_wpp_port_still_free()")
        dialog = src.index("ApiStartupDialog(self, self.wpp_port)")
        assert recheck < dialog

    def test_it_runs_after_the_adopt_check(self):
        """_is_wpp_running() is a TCP connect and _is_port_free() a bind —
        exact complements. Re-allocating before the adopt check could move us
        off a port our own still-listening Node is on."""
        src = self._source()
        recheck = src.index("self._ensure_wpp_port_still_free()")
        # Anchored on the reuse check's own log string, not on a count of
        # `if self._is_wpp_running():` — there are three of those (the adopt,
        # the background_mode poll loop, and this one), and a plain rindex()
        # would still pass with the recheck moved ABOVE this one. That is the
        # dangerous placement: with our own previous Node still listening,
        # _is_port_free is False, so we would move to another port and then
        # spawn a SECOND Node beside the old one.
        assert src.index("already listening on %s — reusing it") < recheck


class TestTheExhaustionErrorIsAboutThePortNotAboutTheNumberChanging:
    """The ERROR must mean "the port we settled on is occupied", which is not
    the same question as "did the number change?".

    allocate_port_for_account() never raises: with its whole range busy it
    returns deterministic_port(account_id) — a number that need not equal the
    port we were escaping, so an equality test misses the real exhaustion. And
    equality fires when nothing is wrong: node_port_lock is a cross-process
    lock, so time passes between the pre-lock probe and the allocation, which
    is exactly when our own dying Node releases the port and the allocator
    hands the same number straight back.
    """

    def test_a_port_freed_in_the_meantime_is_not_an_error(self, tmp_path, caplog):
        """The false positive. The port looks busy at the probe, is free by the
        time the allocator asks, and the same number comes back — a healthy
        startup that must not shout ERROR into a diagnostic log."""
        class _TransientlyBusy(_Stub):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self._probes = 0

            def _is_port_free(self, port):
                self._probes += 1
                return self._probes > 1   # busy only on the very first probe

        stub = _TransientlyBusy(wpp_port=6341, global_dir=str(tmp_path))

        with caplog.at_level("INFO"):
            stub._ensure_wpp_port_still_free()

        assert stub.wpp_port == 6341
        errors = [r for r in caplog.records if r.levelname == "ERROR"]
        assert not errors, f"healthy startup logged an error: {[r.message for r in errors]}"

    def test_a_genuinely_exhausted_range_is_an_error(self, tmp_path, caplog):
        """The false negative. Saved port 6300, whole range busy: the allocator
        returns deterministic_port('acc1') == 6341, a DIFFERENT number that is
        equally unusable — so an equality test stays quiet and logs success."""
        stub = _Stub(wpp_port=6300, global_dir=str(tmp_path),
                     busy_ports=range(6300, 6350))

        with caplog.at_level("INFO"):
            stub._ensure_wpp_port_still_free()

        errors = [r for r in caplog.records if r.levelname == "ERROR"]
        assert errors, "an exhausted range must be diagnosable, not logged as success"
        assert "will fail to bind" in errors[0].message

    def test_an_ordinary_move_to_a_free_port_is_not_an_error(self, tmp_path, caplog):
        stub = _Stub(wpp_port=6301, global_dir=str(tmp_path), busy_ports={6301})

        with caplog.at_level("INFO"):
            stub._ensure_wpp_port_still_free()

        assert stub.wpp_port != 6301
        assert not [r for r in caplog.records if r.levelname == "ERROR"]


class TestTheDialogIsHandedTheReResolvedPort:
    """The behavioural counterpart of the source-reading tests above.

    Those assert an ordering by matching strings, so they break on a harmless
    rename and — worse — would keep passing if ApiStartupDialog started taking
    the port some other way. This one drives ensure_wpp_running() for real and
    checks the only thing that matters: the number the dialog was handed.
    """

    def test_the_dialog_receives_the_port_node_will_actually_bind(
        self, monkeypatch, tmp_path
    ):
        import sys
        import types

        import main

        captured = {}

        class _FakeDialog:
            def __init__(self, parent, port):
                captured["port"] = port

            def ShowModal(self):
                import wx
                return wx.ID_OK   # the API came up; take the success path

            def Destroy(self):
                pass

        fake_mod = types.ModuleType("ui.dialogs.api_startup")
        fake_mod.ApiStartupDialog = _FakeDialog
        monkeypatch.setitem(sys.modules, "ui.dialogs.api_startup", fake_mod)
        monkeypatch.setattr(main.os.path, "isfile", lambda *_a, **_k: True)
        patch_main_global(monkeypatch, "resource_path", lambda *parts: str(tmp_path / "x"))
        monkeypatch.setattr(main.wx, "CallAfter", lambda fn, *a, **k: None)

        class _RunStub:
            ensure_wpp_running = MainWindow.ensure_wpp_running

            def __init__(self):
                self.background_mode = False
                self.wpp_port = 6341
                self.started = False

            def _is_wpp_running(self):
                return False

            def _ensure_wpp_port_still_free(self):
                # Stands in for the real re-check finding the port taken.
                self.wpp_port = 6342

            def _start_wpp_background(self):
                self.started = True

            def _register_node_lease(self):
                pass

            def _check_wpp_version_pin(self):
                pass

            def run_on_main_thread(self, fn, *a, **kw):
                return fn(*a, **kw)

        stub = _RunStub()
        stub.ensure_wpp_running()

        assert captured.get("port") == 6342, (
            "the dialog polls the port it was constructed with — handing it the "
            "pre-recheck one makes it watch a port Node will never bind"
        )
