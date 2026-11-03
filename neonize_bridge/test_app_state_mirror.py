"""The bridge's copy of the app state entries whatsmeow has no event for."""
import sys
import threading
import types
from types import SimpleNamespace as N

import pytest

import app_state_mirror as asm


def _has_remove():
    from neonize.proto.Neonize_pb2 import MutationInfo
    return "Remove" in MutationInfo.DESCRIPTOR.fields_by_name


# REMOVE mutations are a field only WinZapp's neonize build has
needs_remove = pytest.mark.skipif(not _has_remove(), reason="needs WinZapp's neonize build")


def test_only_lists_labels_and_status_privacy_are_kept():
    assert asm.wanted(["label_edit", "3"])
    assert asm.wanted(["label_jid", "3", "1@g.us"])
    assert asm.wanted(["status_privacy"])
    assert asm.wanted(["setting_unarchiveChats"])
    assert not asm.wanted(["mute", "1@s.whatsapp.net"])
    assert not asm.wanted([])


def test_the_log_shows_the_kind_and_only_the_server_of_an_address():
    kind = asm.describe("critical_unblock_low", ["contact", "5511999998888@c.us"], 2, True)
    assert kind == ("critical_unblock_low", "contact[c.us]", 2, True)
    assert "5511" not in repr(kind)
    assert asm.describe("regular", ["label_edit", "3"], 3, False)[1] == "label_edit"


def test_an_index_is_stored_as_its_json():
    assert asm.index_key(["label_jid", "3", "1@g.us"]) == '["label_jid","3","1@g.us"]'


def test_set_and_remove_and_read_back(tmp_path):
    m = asm.Mirror(str(tmp_path / "a.db"))
    m.apply("regular", ["label_edit", "3"], 3, False, b"one")
    m.apply("regular", ["label_edit", "4"], 3, False, b"two")
    m.apply("regular", ["mute", "1@s.whatsapp.net"], 2, False, b"ignored")
    m.apply("regular", ["label_edit", "3"], 3, False, b"three")
    assert m.entries("label_edit") == [(["label_edit", "3"], 3, b"three"),
                                       (["label_edit", "4"], 3, b"two")]
    m.apply("regular", ["label_edit", "4"], 3, True, b"")
    assert [i for i, _v, _b in m.entries("label_edit")] == [["label_edit", "3"]]
    assert m.entries("mute") == []


def test_an_index_name_with_an_underscore_is_not_a_wildcard(tmp_path):
    m = asm.Mirror(str(tmp_path / "a.db"))
    m.apply("regular", ["label_jid", "3", "1@g.us"], 3, False, b"x")
    assert m.entries("label_edit") == []
    assert len(m.entries("label_jid")) == 1


def test_a_full_sync_drops_what_it_did_not_resend(tmp_path):
    m = asm.Mirror(str(tmp_path / "a.db"))
    m.apply("regular", ["label_edit", "1"], 3, False, b"old")
    m.apply("regular", ["label_edit", "2"], 3, False, b"old")
    m.apply("regular_high", ["status_privacy"], 7, False, b"kept")
    assert not m.seeded("regular")
    m.apply("regular", ["label_edit", "2"], 3, False, b"new", full_sync=True)
    m.full_sync_completed("regular")
    assert m.entries("label_edit") == [(["label_edit", "2"], 3, b"new")]
    assert m.entries("status_privacy") == [(["status_privacy"], 7, b"kept")]
    assert m.seeded("regular") and not m.seeded("regular_high")


def test_a_full_sync_with_nothing_of_ours_empties_that_collection(tmp_path):
    m = asm.Mirror(str(tmp_path / "a.db"))
    m.apply("regular", ["label_edit", "1"], 3, False, b"gone")
    m.full_sync_completed("regular")
    assert m.entries("label_edit") == [] and m.seeded("regular")


def test_seeded_survives_a_restart(tmp_path):
    asm.Mirror(str(tmp_path / "a.db")).full_sync_completed("regular")
    again = asm.Mirror(str(tmp_path / "a.db"))
    assert again.seeded("regular") and again.wait_seeded("regular", 0)


class FakeClient:
    def __init__(self, refuse_first=None):
        self.patches = []
        self.refuse_first = refuse_first

    def send_app_state(self, patch):
        if self.refuse_first:
            error, self.refuse_first = self.refuse_first, None
            raise Exception(error)
        self.patches.append(patch)


def session(tmp_path, client=None, repaired=True):
    s = N(folder=str(tmp_path), client=client or FakeClient(), status="CONNECTED",
          repairs=[])

    def repair(name, force=True):
        s.repairs.append((name, force))
        return repaired
    s.repair_collection = repair
    return s


def test_send_builds_one_patch_and_keeps_the_entries(tmp_path):
    from neonize.proto.Neonize_pb2 import PatchInfo
    from neonize.proto.waSyncAction.WAWebProtobufSyncAction_pb2 import (
        LabelEditAction, SyncActionValue)
    s = session(tmp_path)
    value = SyncActionValue(labelEditAction=LabelEditAction(name="Work", type=5))
    asm.send(s, "regular", [(["label_edit", "7"], 3, value, False)])
    (patch,) = s.client.patches
    assert patch.Type == PatchInfo.REGULAR
    (mutation,) = patch.Mutations
    assert list(mutation.Index) == ["label_edit", "7"] and mutation.Version == 3
    assert mutation.Value.labelEditAction.name == "Work"
    (stored,) = asm.mirror_of(s).entries("label_edit")
    assert SyncActionValue.FromString(stored[2]).labelEditAction.name == "Work"


def test_send_repairs_a_collection_out_of_sync_and_tries_once_more(tmp_path):
    from neonize.proto.waSyncAction.WAWebProtobufSyncAction_pb2 import SyncActionValue
    s = session(tmp_path, FakeClient(refuse_first="mismatching LTHash"))
    asm.send(s, "regular_high", [(["status_privacy"], 7, SyncActionValue(), False)])
    assert s.repairs == [("regular_high", True)] and len(s.client.patches) == 1


def test_send_raises_what_is_not_an_out_of_sync_collection(tmp_path):
    from neonize.proto.waSyncAction.WAWebProtobufSyncAction_pb2 import SyncActionValue
    s = session(tmp_path, FakeClient(refuse_first="server said no"))
    with pytest.raises(Exception, match="server said no"):
        asm.send(s, "regular", [(["label_edit", "1"], 3, SyncActionValue(), False)])
    assert asm.mirror_of(s).entries("label_edit") == []


@needs_remove
def test_send_marks_a_remove(tmp_path):
    from neonize.proto.waSyncAction.WAWebProtobufSyncAction_pb2 import SyncActionValue
    s = session(tmp_path)
    asm.mirror_of(s).apply("critical_unblock_low", ["contact", "1@c.us"], 2, False, b"x")
    asm.send(s, "critical_unblock_low", [(["contact", "1@c.us"], 2, SyncActionValue(), True)])
    assert s.client.patches[0].Mutations[0].Remove is True


def test_seeding_downloads_the_collection_once_and_waits_for_its_events(tmp_path):
    s = session(tmp_path)
    mirror = asm.mirror_of(s)

    def repair(name, force=True):
        s.repairs.append((name, force))
        # the download's completion arrives on neonize's thread
        threading.Timer(0.05, mirror.full_sync_completed, (name,)).start()
        return True
    s.repair_collection = repair
    assert asm.ensure_seeded(s, "regular", timeout=2)
    assert asm.ensure_seeded(s, "regular", timeout=2)
    assert s.repairs == [("regular", True)]


def test_seeding_needs_a_connection(tmp_path):
    s = session(tmp_path)
    s.status = "CLOSED"
    assert not asm.ensure_seeded(s, "regular", timeout=0)
    assert s.repairs == []


class _Cli:
    def __init__(self):
        self.handlers = {}

    def event(self, kind):
        def bind(f):
            self.handlers[kind] = f
            return f
        return bind


def test_every_mutation_event_reaches_the_mirror(monkeypatch, tmp_path):
    events = types.ModuleType("neonize.events")
    events.AppStateMutationEv = type("AppStateMutationEv", (), {})
    package = types.ModuleType("neonize")
    package.events = events
    monkeypatch.setitem(sys.modules, "neonize", package)
    monkeypatch.setitem(sys.modules, "neonize.events", events)
    cli, s = _Cli(), N(folder=str(tmp_path))
    asm.register_events(cli, s)
    handle = cli.handlers[events.AppStateMutationEv]
    action = N(SerializeToString=lambda: b"value")
    handle(cli, N(Name="regular", Index=["label_edit", "5"], Version=3, Remove=False,
                  Action=action, FromFullSync=True, HasField=lambda f: f == "Action"))
    assert asm.mirror_of(s).entries("label_edit") == [(["label_edit", "5"], 3, b"value")]
    s.app_state_synced("regular")
    assert asm.mirror_of(s).seeded("regular")


def test_an_older_build_registers_nothing(monkeypatch, tmp_path):
    events = types.ModuleType("neonize.events")
    package = types.ModuleType("neonize")
    package.events = events
    monkeypatch.setitem(sys.modules, "neonize", package)
    monkeypatch.setitem(sys.modules, "neonize.events", events)
    cli, s = _Cli(), N(folder=str(tmp_path))
    asm.register_events(cli, s)
    assert cli.handlers == {} and not hasattr(s, "app_state_synced")


def test_winzapp_build_event_carries_the_fields_the_handler_reads():
    events = pytest.importorskip("neonize.events")
    if not hasattr(events, "AppStateMutationEv"):
        pytest.skip("neonize without WinZapp's events (build_neonize.py builds them)")
    fields = set(events.AppStateMutationEv.DESCRIPTOR.fields_by_name)
    assert {"Name", "Index", "Version", "Remove", "Action", "FromFullSync"} <= fields
    assert events.EVENT_TO_INT[events.AppStateMutationEv] == 57
