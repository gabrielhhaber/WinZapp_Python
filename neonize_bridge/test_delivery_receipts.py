"""The bridge forces active delivery receipts on every client it creates.

whatsmeow answers every received message with a delivery receipt, but marks
it type="inactive" while the client is not online, and the sender's official
app does not render an inactive receipt (2026-11-15: a message received with
the WinZapp window out of focus stayed "sent" for the sender, and with read
receipts hidden nothing ever rendered, whatever the user did). Presence stays
bound to the window through set-online-presence; forcing the receipts
decouples the delivered tick from it and survives SendPresence in both
directions, because presence.go only compares against 0 and 1."""
import sys
import types

import server


class EventRegistry:
    """neonize's `cli.event`: keeps the handler of each event type."""

    def __init__(self):
        self.handlers = {}

    def __call__(self, kind):
        def keep(function):
            self.handlers[kind] = function
            return function
        return keep

    def qr(self, function):
        return function


class FakeEvents(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        kind = type(name, (), {})
        setattr(self, name, kind)
        return kind


class FakeDeviceProps:
    DESKTOP = 7

    def __init__(self, **kw):
        for key, value in kw.items():
            setattr(self, key, value)
        # the real proto creates the HistorySyncConfig submessage on access
        self.historySyncConfig = type("HistorySyncConfig", (), {})()


class FakeNewClient:
    """What `_run` asks of neonize at client creation, recorded."""

    def __init__(self, *args, **kw):
        self.event = EventRegistry()
        self.forced = []

    def set_force_activate_delivery_receipts(self, active):
        self.forced.append(active)

    def connect(self):
        pass

    def disconnect(self):
        pass

    def stop(self):
        pass


def _install_neonize_fakes(monkeypatch, client_class):
    neonize = types.ModuleType("neonize")
    events = FakeEvents("neonize.events")
    client = types.ModuleType("neonize.client")
    client.NewClient = client_class
    proto = types.ModuleType("neonize.proto")
    companion = types.ModuleType("neonize.proto.waCompanionReg")
    pb2 = types.ModuleType(
        "neonize.proto.waCompanionReg.WAWebProtobufsCompanionReg_pb2")
    pb2.DeviceProps = FakeDeviceProps
    for module in (neonize, events, client, proto, companion, pb2):
        monkeypatch.setitem(sys.modules, module.__name__, module)
    neonize.events, neonize.client = events, client
    proto.waCompanionReg = companion
    companion.WAWebProtobufsCompanionReg_pb2 = pb2
    monkeypatch.setitem(sys.modules, "neonize", neonize)


class FailingForceReceiptsClient(FakeNewClient):
    """An install whose neonize build lacks the export."""

    def set_force_activate_delivery_receipts(self, active):
        raise AttributeError("no such FFI export")


def test_the_client_is_created_with_active_delivery_receipts(tmp_path, monkeypatch):
    _install_neonize_fakes(monkeypatch, FakeNewClient)

    s = server.Session("account1", str(tmp_path / "account1"), None, None)
    s.emitted = []
    s.emit = lambda event, data: s.emitted.append((event, data))
    s._run(None, s.lifecycle.open())
    assert s.client.forced == [True]


def test_a_build_without_the_export_degrades_instead_of_killing_the_bridge(tmp_path, monkeypatch, caplog):
    _install_neonize_fakes(monkeypatch, FailingForceReceiptsClient)

    s = server.Session("account1", str(tmp_path / "account1"), None, None)
    s.emitted = []
    s.emit = lambda event, data: s.emitted.append((event, data))
    with caplog.at_level("ERROR", logger="bridge"):
        s._run(None, s.lifecycle.open())   # must not raise
    assert isinstance(s.client, FailingForceReceiptsClient)
    assert any("delivery receipts" in record.getMessage() for record in caplog.records)
