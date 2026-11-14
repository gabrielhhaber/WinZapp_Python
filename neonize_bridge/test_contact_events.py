"""contacts.update: payload in the format the client reads, deduplication and the
handlers, all without neonize (a fake `neonize.events` and SimpleNamespace events)."""
import sys
import types
from types import SimpleNamespace as N

import contact_events as ce


def jid(user, server="s.whatsapp.net"):
    return N(User=user, Server=server)


# ------------------------------------------------------------------ pure
def test_only_a_person_counts():
    assert ce.is_person(jid("5511"))
    assert ce.is_person(jid("99", "lid"))
    assert not ce.is_person(jid("120", "g.us"))
    assert not ce.is_person(jid("x", "newsletter"))
    assert not ce.is_person(jid("", "s.whatsapp.net"))
    assert not ce.is_person(None)


def test_clean_name():
    assert ce.clean_name("  Ana ") == "Ana"
    assert ce.clean_name("   ") == ""
    assert ce.clean_name(None) == ""


def test_contact_only_carries_what_exists():
    assert ce.contact_wpp("5511@c.us") == {"remoteJid": "5511@c.us"}
    assert ce.contact_wpp("5511@c.us", name="Ana") == {
        "remoteJid": "5511@c.us", "pushName": "Ana"}
    assert ce.contact_wpp("5511@c.us", photo="https://x/y.jpg") == {
        "remoteJid": "5511@c.us", "profilePicUrl": "https://x/y.jpg"}
    assert ce.contacts_event("s1", [{"remoteJid": "5511@c.us"}]) == {
        "session": "s1", "data": [{"remoteJid": "5511@c.us"}]}


def test_does_not_repeat_what_did_not_change():
    a = ce.Announced()
    assert a.name_changed("5511@c.us", "Ana") is True
    assert a.name_changed("5511@c.us", "Ana") is False
    assert a.name_changed("5511@c.us", "Ana Maria") is True
    assert a.name_changed("5511@c.us", "") is False
    # name and photo are independent
    assert a.photo_changed("5511@c.us", "f1") is True
    assert a.photo_changed("5511@c.us", "f1") is False
    assert a.name_changed("5511@c.us", "Ana Maria") is False
    assert a.photo_changed("5511@c.us", "f2") is True


def test_memory_of_announced_has_a_limit(monkeypatch):
    monkeypatch.setattr(ce, "MAX_CONTACTS", 3)
    a = ce.Announced()
    for i in range(5):
        a.name_changed(f"{i}@c.us", "n")
    assert a.name_changed("0@c.us", "n") is True     # forgotten: counts again
    assert a.name_changed("4@c.us", "n") is False


# --------------------------------------------- handlers, without neonize
class _Cli:
    def __init__(self, pairs=None, photos=None):
        self.handlers = {}
        self.pairs = pairs or {}
        self.photos = photos or {}

    def event(self, kind):
        def bind(f):
            self.handlers[kind] = f
            return f
        return bind

    def get_pn_from_lid(self, j):
        return self.pairs[f"{j.User}@{j.Server}"]

    def get_profile_picture(self, j):
        result = self.photos[f"{j.User}@{j.Server}"]
        if isinstance(result, Exception):
            raise result
        return result


class _Session:
    name = "s1"

    def __init__(self):
        self.sent = []

    def emit(self, event, data):
        self.sent.append((event, data))


def _bind(monkeypatch, **kw):
    events = types.ModuleType("neonize.events")
    events.PictureEv = type("PictureEv", (), {})
    package = types.ModuleType("neonize")
    package.events = events
    monkeypatch.setitem(sys.modules, "neonize", package)
    monkeypatch.setitem(sys.modules, "neonize.events", events)
    # the photo fetch runs right away, instead of on a thread
    monkeypatch.setattr(ce, "_in_background", lambda f, *a: f(*a))
    cli, s = _Cli(**kw), _Session()
    ce.register_events(cli, s)
    return cli, s, events


def _photo(url, id_="F1"):
    return N(URL=url, ID=id_)


def test_photo_changed_emits_the_url_by_number(monkeypatch):
    cli, s, ev = _bind(
        monkeypatch, pairs={"99@lid": jid("5511")},
        photos={"99@lid": _photo("https://pps.whatsapp.net/a.jpg")})
    cli.handlers[ev.PictureEv](
        cli, N(JID=jid("99", "lid"), Author=jid("99", "lid"), Timestamp=1, Remove=False))
    assert s.sent[0] == ("profile-picture-changed", {"session":"s1", "chatId":"99@lid"})
    assert s.sent[1:] == [("contacts.update", {"session": "s1", "data": [{
        "remoteJid": "5511@c.us", "profilePicUrl": "https://pps.whatsapp.net/a.jpg"}]})]


def test_repeated_photo_is_not_emitted_again(monkeypatch):
    cli, s, ev = _bind(monkeypatch, photos={"5511@s.whatsapp.net": _photo("https://x/a.jpg")})
    event = N(JID=jid("5511"), Author=jid("5511"), Timestamp=1, Remove=False)
    cli.handlers[ev.PictureEv](cli, event)
    cli.handlers[ev.PictureEv](cli, event)
    assert len([e for e in s.sent if e[0] == "contacts.update"]) == 1
    cli.photos["5511@s.whatsapp.net"] = _photo("https://x/b.jpg", "F2")
    cli.handlers[ev.PictureEv](cli, event)
    updates = [e for e in s.sent if e[0] == "contacts.update"]
    assert len(updates) == 2
    assert updates[1][1]["data"][0]["profilePicUrl"] == "https://x/b.jpg"


def test_photo_removal_and_group_changes_invalidate_without_guessing_a_url(monkeypatch):
    cli, s, ev = _bind(monkeypatch, photos={
        "5511@s.whatsapp.net": _photo(""),
        "5522@s.whatsapp.net": RuntimeError("profile picture not found"),
        "5533@s.whatsapp.net": RuntimeError("timeout")})
    handle = cli.handlers[ev.PictureEv]
    handle(cli, N(JID=jid("5511"), Remove=True))                    # removed
    handle(cli, N(JID=jid("120", "g.us"), Remove=False))            # group
    handle(cli, N(JID=jid("5511"), Remove=False))                   # without a URL
    handle(cli, N(JID=jid("5522"), Remove=False))                   # without a photo
    handle(cli, N(JID=jid("5533"), Remove=False))                   # network failure
    assert len(s.sent) == 5 and all(e[0] == "profile-picture-changed" for e in s.sent)


def test_new_nickname_emits_once(monkeypatch):
    cli, s, _ = _bind(monkeypatch)
    assert ce.announce_name(cli, s, jid("5511"), " Ana ") is True
    assert s.sent == [("contacts.update", {"session": "s1", "data": [{
        "remoteJid": "5511@c.us", "pushName": "Ana"}]})]
    assert ce.announce_name(cli, s, jid("5511"), "Ana") is False
    assert ce.announce_name(cli, s, jid("5511"), "") is False
    assert ce.announce_name(cli, s, jid("120", "g.us"), "Group") is False
    assert ce.announce_name(cli, s, jid("5511"), "Ana B") is True
    assert len(s.sent) == 2


def test_lid_nickname_only_goes_with_the_number(monkeypatch):
    cli, s, _ = _bind(monkeypatch, pairs={"99@lid": jid("5511")})
    assert ce.announce_name(cli, s, jid("99", "lid"), "Ana") is True
    assert s.sent[0][1]["data"][0]["remoteJid"] == "5511@c.us"
    # LID without a known number: the client would create a contact with a LID key
    assert ce.announce_name(cli, s, jid("77", "lid"), "Bob") is False
    assert len(s.sent) == 1


# ------------------------------------------- address book (WinZapp's build)
def test_saved_name_prefers_the_full_name_and_empty_means_removed():
    assert ce.saved_name(N(fullName=" Ana Souza ", firstName="Ana")) == "Ana Souza"
    assert ce.saved_name(N(fullName="", firstName="Ana")) == "Ana"
    assert ce.saved_name(N(fullName="", firstName="")) == ""
    assert ce.saved_name(None) == ""


def test_only_the_address_book_collection_reloads_the_names():
    assert ce.address_book_synced("critical_unblock_low")
    assert not ce.address_book_synced("regular_low")


def test_an_older_neonize_build_registers_only_the_photo(monkeypatch):
    cli, _s, ev = _bind(monkeypatch)
    assert list(cli.handlers) == [ev.PictureEv]


class _AddressBookSession(_Session):
    def __init__(self, db):
        super().__init__()
        self.db, self.reloads = db, 0

    def reload_names(self):
        self.reloads += 1


def _bind_winzapp_build(monkeypatch, tmp_path):
    from store import Store
    events = types.ModuleType("neonize.events")
    for name in ("PictureEv", "ContactUpdateEv", "PushNameUpdateEv", "AppStateSyncCompleteEv"):
        setattr(events, name, type(name, (), {}))
    package = types.ModuleType("neonize")
    package.events = events
    monkeypatch.setitem(sys.modules, "neonize", package)
    monkeypatch.setitem(sys.modules, "neonize.events", events)
    monkeypatch.setattr(ce, "_in_background", lambda f, *a: f(*a))
    cli, s = _Cli(), _AddressBookSession(Store(str(tmp_path / "bridge.db")))
    ce.register_events(cli, s)
    return cli, s, events


def _action(**fields):
    return N(**fields, HasField=lambda name: name in fields)


def test_a_contact_saved_renamed_and_removed_on_the_phone(monkeypatch, tmp_path):
    cli, s, ev = _bind_winzapp_build(monkeypatch, tmp_path)
    handle = cli.handlers[ev.ContactUpdateEv]
    s.db.contact("5511@s.whatsapp.net", pushname="ana")
    handle(cli, N(JID=jid("5511"), Action=_action(fullName="Ana Souza", firstName="Ana"), FromFullSync=False))
    assert s.db.contact_of("5511@s.whatsapp.net") == {"name": "Ana Souza", "pushname": "ana"}
    handle(cli, N(JID=jid("5511"), Action=_action(fullName="Ana S.", firstName=""), FromFullSync=False))
    assert s.db.contact_of("5511@s.whatsapp.net")["name"] == "Ana S."
    handle(cli, N(JID=jid("5511"), Action=_action(fullName="", firstName=""), FromFullSync=False))
    assert s.db.contact_of("5511@s.whatsapp.net") == {"pushname": "ana"}    # removed
    handle(cli, N(JID=jid("120", "g.us"), Action=_action(fullName="Group", firstName=""), FromFullSync=False))
    assert s.db.contact_of("120@g.us") == {}


def test_the_address_book_full_sync_reloads_the_names(monkeypatch, tmp_path):
    cli, s, ev = _bind_winzapp_build(monkeypatch, tmp_path)
    handle = cli.handlers[ev.AppStateSyncCompleteEv]
    handle(cli, N(Name="regular_low", Version=3, Recovery=False))
    assert s.reloads == 0
    handle(cli, N(Name="critical_unblock_low", Version=7, Recovery=False))
    assert s.reloads == 1


def test_a_full_sync_completes_the_app_state_mirror_of_that_collection(monkeypatch, tmp_path):
    cli, s, ev = _bind_winzapp_build(monkeypatch, tmp_path)
    completed = []
    s.app_state_synced = completed.append     # set by app_state_mirror.register_events
    cli.handlers[ev.AppStateSyncCompleteEv](cli, N(Name="regular", Version=9, Recovery=False))
    assert completed == ["regular"]


def test_a_new_nickname_is_kept_under_the_other_form_of_the_jid(monkeypatch, tmp_path):
    cli, s, ev = _bind_winzapp_build(monkeypatch, tmp_path)
    handle = cli.handlers[ev.PushNameUpdateEv]
    handle(cli, N(JID=jid("99", "lid"), JIDAlt=jid("5511"), OldPushName="", NewPushName=" Ana ",
                  HasField=lambda field: field == "JIDAlt"))
    assert s.db.contact_of("5511@s.whatsapp.net") == {"pushname": "Ana"}
    handle(cli, N(JID=jid("77", "lid"), OldPushName="", NewPushName="Bob",
                  HasField=lambda field: False))
    assert {row["jid"]: (row["name"], row["pushname"]) for row in s.db.contacts()} == {
        "5511@s.whatsapp.net": ("", "Ana"), "99@lid": ("", "Ana"), "77@lid": ("", "Bob")}
    assert s.sent[-1] == ("contact-state-changed", {"session": "s1", "contactId": "77@lid",
        "kind": "profile", "name": "Bob"})


def test_winzapp_build_events_carry_the_fields_the_handlers_read():
    import pytest
    events = pytest.importorskip("neonize.events")
    if not hasattr(events, "ContactUpdateEv"):
        pytest.skip("neonize without WinZapp's events (build_neonize.py builds them)")
    fields = lambda ev: set(ev.DESCRIPTOR.fields_by_name)
    assert {"JID", "Action", "FromFullSync"} <= fields(events.ContactUpdateEv)
    assert {"fullName", "firstName"} <= set(events.ContactUpdateEv.DESCRIPTOR
                                            .fields_by_name["Action"].message_type.fields_by_name)
    assert {"JID", "JIDAlt", "NewPushName"} <= fields(events.PushNameUpdateEv)
    assert {"Name", "Version", "Recovery"} <= fields(events.AppStateSyncCompleteEv)
    assert {events.EVENT_TO_INT[e] for e in (events.ContactUpdateEv, events.PushNameUpdateEv,
                                             events.AppStateSyncCompleteEv)} == {54, 55, 56}
