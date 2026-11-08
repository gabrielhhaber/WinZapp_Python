"""Peer message aliases must not file incoming/outgoing traffic under self.

Real owning methods are loaded from this source tree and bound to plain stubs.
No MainWindow import, GUI, process, real thread, native audio, account or DB.
"""
import ast
from contextlib import nullcontext
import logging
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.jid_alias_guard import lid_phone_pair_allowed, retire_cross_self_aliases
from core.phone_variants import other_brazilian_mobile_jid
from main_window.chat_aliases import learn_snapshot_alias

ROOT = Path(os.environ.get("SENDER_SOURCE_ROOT", Path(__file__).parents[1]))
ME = "5511991234567@s.whatsapp.net"
MY_LID = "700000000000001@lid"
PEER = "905001112233@s.whatsapp.net"
PEER_LID = "700000000000002@lid"


def actual(path, cls, method):
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    owner = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls)
    fn = next(n for n in owner.body if isinstance(n, ast.FunctionDef) and n.name == method)
    fn.decorator_list = []
    ns = {"logging": logging, "lid_phone_pair_allowed": lid_phone_pair_allowed,
          "retire_cross_self_aliases": retire_cross_self_aliases,
          "other_brazilian_mobile_jid": other_brazilian_mobile_jid}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(ROOT / path), "exec"), ns)
    return ns[method]


resolve = actual("client/main_window/message_events.py", "MessageEventsMixin", "_resolve_message_chat_jid")
build = actual("client/main_window/identity.py", "IdentityMixin", "_build_lid_to_phone_cache")
find = actual("client/main_window/identity.py", "IdentityMixin", "_find_alt_jid_from_messages")
merge = actual("client/main_window/identity.py", "IdentityMixin", "_merge_lid_into_phone")
extract = actual("client/main_window/identity.py", "IdentityMixin", "_extract_lid_mapping")


class State:
    _resolve_message_chat_jid = resolve
    _build_lid_to_phone_cache = build
    _find_alt_jid_from_messages = find

    def __init__(self, mapping=None):
        self.my_jid, self.my_lid = ME, MY_LID
        self._lid_to_phone = dict(mapping or {})
        self._phone_to_lid = {p: l for l, p in self._lid_to_phone.items()}
        self._lid_mapping_lock = nullcontext()
        self.chats = {}
        self.merges, self.queued, self.registered, self.followed = [], [], [], []

    @staticmethod
    def _normalize_jid(jid):
        if ":" in jid:
            left, right = jid.split("@", 1)
            jid = left.split(":", 1)[0] + "@" + right
        return jid[:-5] + "@s.whatsapp.net" if jid.endswith("@c.us") else jid

    def _merge_lid_into_phone(self, lid, phone):
        self.merges.append((lid, phone))

    def _queue_lid_resolutions(self, lids):
        self.queued.extend(lids)

    def _follow_saved_contacts(self, pairs):
        self.followed.extend(pairs)

    def register_jid_mapping(self, lid, phone):
        # Model the already-existing registration refusal. A resolver must
        # not return a rejected alias merely because registration was attempted.
        if lid_phone_pair_allowed(self, lid, phone):
            self.registered.append((lid, phone))


def chat(jid, *keys):
    return {"remoteJid": jid, "messages": {"messages": {"records": [{"key": k} for k in keys]}}}


@pytest.mark.parametrize("from_me", [False, True])
def test_peer_bad_self_alt_uses_known_peer_for_both_directions(from_me):
    s = State({PEER_LID: PEER})
    key = {"remoteJid": PEER_LID, "remoteJidAlt": ME, "fromMe": from_me}
    assert s._resolve_message_chat_jid(PEER_LID, key) == PEER
    assert s.merges == [(PEER_LID, PEER)]


@pytest.mark.parametrize("history", [False, True])
def test_unknown_peer_is_preserved_instead_of_redirected_or_dropped(history):
    s = State()
    assert s._resolve_message_chat_jid(PEER_LID, {"remoteJidAlt": ME}, resolve_unknown=not history) == PEER_LID
    assert s.merges == []
    assert s.queued == ([] if history else [PEER_LID])


def test_corrupt_cached_alias_is_not_a_routing_fallback():
    s = State({PEER_LID: ME})
    assert s._resolve_message_chat_jid(PEER_LID, {}) == PEER_LID
    assert not s.merges


def test_phone_peer_does_not_merge_the_accounts_lid():
    s = State()
    assert s._resolve_message_chat_jid(PEER, {"remoteJidAlt": MY_LID}) == PEER
    assert not s.merges


def test_history_cache_keeps_correct_mapping_and_cannot_learn_peer_as_self():
    s = State({PEER_LID: PEER, MY_LID: ME})
    s.chats[PEER_LID] = chat(PEER_LID, {"remoteJid": PEER_LID, "remoteJidAlt": ME})
    s._build_lid_to_phone_cache()
    assert s._lid_to_phone == {PEER_LID: PEER, MY_LID: ME}
    assert not s.followed


def test_history_cache_removes_only_cross_self_poison_and_keeps_live_peer_pair():
    s = State({PEER_LID: ME, MY_LID: ME, "700000000000003@lid": PEER})
    s._build_lid_to_phone_cache()
    assert PEER_LID not in s._lid_to_phone
    assert s._lid_to_phone[MY_LID] == ME
    assert s._lid_to_phone["700000000000003@lid"] == PEER
    assert s._phone_to_lid.get(ME) == MY_LID


def test_forwarded_participant_cannot_overwrite_peers_phone_with_self():
    s = State({MY_LID: ME})
    s.chats[PEER] = chat(PEER, {"remoteJid": PEER_LID, "remoteJidAlt": ME, "participant": MY_LID})
    s._build_lid_to_phone_cache()
    assert s._lid_to_phone == {MY_LID: ME}


def test_name_fallback_does_not_return_an_alias_the_registration_rejects():
    s = State()
    assert s._find_alt_jid_from_messages(chat(PEER_LID, {"remoteJid": PEER_LID, "remoteJidAlt": ME})) is None
    assert not s.registered


def test_cross_self_merge_refuses_before_touching_any_chat_or_database():
    class Untouchable:
        def __contains__(self, key):
            raise AssertionError("rejected alias reached the chat store")
    s = State()
    s.chats = Untouchable()
    merge(s, PEER_LID, ME)


@pytest.mark.parametrize("remote,alt,expected", [(PEER_LID, PEER, PEER), (PEER, PEER_LID, PEER), (MY_LID, ME, ME)])
def test_valid_peer_and_self_pairs_keep_existing_routing(remote, alt, expected):
    s = State()
    assert s._resolve_message_chat_jid(remote, {"remoteJidAlt": alt}) == expected
    assert len(s.merges) == 1


@pytest.mark.parametrize("lid,phone,allowed", [(PEER_LID, PEER, True), (PEER_LID, ME, False), (MY_LID, PEER, False), (MY_LID, ME, True), ("905001112233@g.us", PEER, False), (PEER_LID, "channel@newsletter", False)])
def test_identity_boundary_policy(lid, phone, allowed):
    assert lid_phone_pair_allowed(State(), lid, phone) is allowed


def test_normalized_device_suffixes_and_brazilian_self_variant():
    s = State()
    assert lid_phone_pair_allowed(s, MY_LID.replace("@", ":12@"), "551191234567@c.us")
    assert not lid_phone_pair_allowed(s, PEER_LID, "551191234567@c.us")


@pytest.mark.parametrize("remote,alt", [(PEER_LID, ME), (ME, PEER_LID), (MY_LID, PEER), (PEER, MY_LID)])
def test_snapshot_cross_self_alias_never_changes_maps_or_saved_names(remote, alt):
    s = State({PEER_LID: PEER, MY_LID: ME})
    before = dict(s._lid_to_phone), dict(s._phone_to_lid)
    assert not learn_snapshot_alias(s, {"remoteJid": remote, "remoteJidAlt": alt})
    assert (s._lid_to_phone, s._phone_to_lid) == before
    assert not s.followed


@pytest.mark.parametrize("remote,alt,lid,phone", [(PEER_LID, PEER, PEER_LID, PEER), (PEER, PEER_LID, PEER_LID, PEER), (MY_LID, ME, MY_LID, ME)])
def test_snapshot_valid_alias_follows_saved_contact_once(remote, alt, lid, phone):
    s = State()
    key = {"remoteJid": remote, "remoteJidAlt": alt}
    before = dict(key)
    assert learn_snapshot_alias(s, key)
    assert s._lid_to_phone == {lid: phone}
    assert s.followed == [(lid, phone)]
    assert not learn_snapshot_alias(s, key)
    assert s.followed == [(lid, phone)]
    assert key == before


def test_snapshot_rechecks_identity_after_waiting_without_a_real_thread():
    s = State()
    s.my_jid = ""
    class AccountBecomesKnown:
        def __enter__(self):
            s.my_jid = ME
        def __exit__(self, *args):
            return False
    s._lid_mapping_lock = AccountBecomesKnown()
    assert not learn_snapshot_alias(s, {"remoteJid": PEER_LID, "remoteJidAlt": ME})
    assert not s._lid_to_phone
    assert not s.followed


@pytest.mark.parametrize("from_me", [False, True])
def test_rejected_registration_cannot_be_bypassed_by_message_routing(from_me):
    s = State({PEER_LID: PEER, MY_LID: ME})
    s._ui_ready_event = SimpleNamespace(is_set=lambda: True)
    s._is_self_jid = lambda jid: jid in (ME, MY_LID)
    s._needs_sender_resolution = lambda jid: False
    msg = {"key": {"remoteJid": PEER_LID, "remoteJidAlt": ME, "fromMe": from_me}}
    extract(s, msg)
    assert s._lid_to_phone[PEER_LID] == PEER
    assert s._resolve_message_chat_jid(PEER_LID, msg["key"]) == PEER
    assert s.merges == [(PEER_LID, PEER)]


@pytest.mark.parametrize("source,destination", [
    ("551198765432@s.whatsapp.net", "5511998765432@s.whatsapp.net"),
    ("5511998765432@s.whatsapp.net", "551198765432@s.whatsapp.net"),
    (PEER_LID, PEER), (MY_LID, ME),
])
def test_real_merge_keeps_brazilian_phone_variants_and_valid_lids(source, destination):
    s = State()
    original = chat(source, {"id": "synthetic-preserved-message"})
    s.chats = {source: original}
    s._pending_lid_inserts = {}
    moved = []
    s.db = SimpleNamespace(merge_or_rename_chat=lambda old, new: moved.append((old, new)))
    # Execute the production closure synchronously against a plain DB stub.
    merge.__globals__["threading"] = SimpleNamespace(
        Thread=lambda *, target, daemon: SimpleNamespace(start=target))
    merge(s, source, destination)
    assert s.chats == {destination: original}
    assert original["remoteJid"] == destination
    assert original["messages"]["messages"]["records"][0]["key"]["id"] == "synthetic-preserved-message"
    assert moved == [(source, destination)]


def test_unrelated_phone_merge_refuses_before_accessing_the_chat_store():
    s = State()
    class Untouchable:
        def __contains__(self, key):
            raise AssertionError("unrelated phone merge reached chat storage")
    s.chats = Untouchable()
    merge(s, ME, PEER)


def test_retired_alias_reaches_real_queue_admission_and_no_longer_looks_like_self():
    s = State({PEER_LID: ME, MY_LID: ME})
    # Keep the valid reverse own pair; retiring the peer must not remove it.
    s._phone_to_lid[ME] = MY_LID
    s._lid_resolution_queue = set()
    s._lid_resolution_queue_lock = nullcontext()
    s._lid_resolution_queue_running = True  # Exercise admission, no worker.
    s._queue_lid_resolutions = actual(
        "client/main_window/identity.py", "IdentityMixin", "_queue_lid_resolutions").__get__(s)
    is_self = actual("client/main_window/contacts.py", "ContactsMixin", "_is_self_jid")
    s._phone_digits_equivalent = lambda a, b: a == b
    assert s._resolve_message_chat_jid(PEER_LID, {}) == PEER_LID
    assert s._lid_resolution_queue == {PEER_LID}
    assert not is_self(s, PEER_LID)
    assert s._phone_to_lid[ME] == MY_LID
    assert s._lid_to_phone == {MY_LID: ME}


@pytest.mark.parametrize("jid", [PEER_LID, ME])
def test_targeted_retirement_removes_both_poisoned_directions_only(jid):
    s = State({PEER_LID: ME, MY_LID: ME, "700000000000003@lid": PEER})
    s._phone_to_lid[ME] = PEER_LID
    assert retire_cross_self_aliases(s, jid)
    assert s._lid_to_phone == {MY_LID: ME, "700000000000003@lid": PEER}
    assert ME not in s._phone_to_lid
    assert s._phone_to_lid[PEER] == "700000000000003@lid"
    assert not retire_cross_self_aliases(s, jid)


@pytest.mark.parametrize("history", [False, True])
def test_event_retires_poison_before_first_cache_based_self_inference(history):
    s = State({PEER_LID: ME})
    s._live_events_ready = lambda: True
    class ReachedInference(BaseException):
        pass
    def inference(*args):
        assert PEER_LID not in s._lid_to_phone
        assert ME not in s._phone_to_lid
        raise ReachedInference()
    s._is_self_jid = inference
    s._redirect_self_chat_artifact = inference
    name = "on_historical_message" if history else "on_new_message"
    event = actual("client/main_window/message_events.py", "MessageEventsMixin", name)
    with pytest.raises(ReachedInference):
        event(s, {"key": {"remoteJid": PEER_LID, "id": "synthetic-event", "fromMe": False}})


@pytest.mark.parametrize("last_message", [None, {"key": {"remoteJid": PEER_LID}}])
def test_real_chat_snapshot_retires_poison_without_an_alternate_hint(last_message):
    s = State({PEER_LID: ME})
    s.wpp_server, s.wpp_port, s.token = "synthetic", 0, "not-a-token"
    s._deleted_chats, s.settings, s.contacts = set(), {}, {}
    s._lift_contact_identity = lambda chat: None
    snapshot = {"remoteJid": PEER_LID, "lastMessage": last_message}
    fetch = actual("client/main_window/chats_store.py", "ChatsStoreMixin", "get_remote_chats")
    fetch.__globals__.update(
        capture_sync_context=lambda owner: None,
        sync_context_is_current=lambda owner, context: True,
        api_post=lambda *args, **kwargs: SimpleNamespace(status_code=200, text="synthetic", json=lambda: [snapshot]),
        observe_payload=lambda *args: None,
        learn_snapshot_alias=learn_snapshot_alias,
        is_listed_chat=lambda *args: True, channels_listed=lambda settings: False,
        looks_like_binary_blob=lambda value: False, learn_chat_names=lambda *args, **kwargs: None,
    )
    class ReachedUnread(BaseException):
        pass
    observed = []
    def note(jid, count):
        observed.append((jid, dict(s._lid_to_phone), dict(s._phone_to_lid)))
        raise ReachedUnread()
    s._note_server_unread = note
    with pytest.raises(ReachedUnread):
        fetch(s, {ME: chat(ME)}, persist_full=False, notify_errors=False)
    assert observed == [(PEER_LID, {}, {})]
