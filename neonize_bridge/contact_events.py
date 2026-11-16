"""Contact updates of the bridge: the Socket.IO `contacts.update` event, which
the client reads in core/websocket_client.py (`on_contacts_update`) to keep the
contacts' name (`pushName`) and photo (`profilePicUrl`) up to date without a
full sync.

What the client reads from `data` (a dictionary or a list of them), and only
this: `remoteJid` (normalized by it; a `@lid` is tied to the number by its map),
`pushName` (replaces the stored name) and `profilePicUrl` (replaces the stored
URL). With neither of the two the event changes nothing, and a `pushName` of
someone it does not know creates the contact. That is why a `@lid` with no known
number never carries a name from here (it would create a contact with the LID
key), and the photo only goes with the URL.

Sources in neonize 0.5.2 (what the proto descriptor confirms):
- `PictureEv` (JID, Author, Timestamp, Remove) only says the photo changed, it
  does not bring the URL; that comes from `get_profile_picture` on a thread,
  outside the handler (it is a network call). Every valid person/group picture
  notice also emits `profile-picture-changed`, including removal, so an old URL
  is invalidated without guessing a new one or fetching an image in the handler.
- The name comes from the `Info.Pushname` of each received message
  (`announce_name`, which server.py's `MessageEv` handler calls: neonize keeps a
  single handler per event, so another cannot be registered here) and from the
  history package.
- The address book's names come from WinZapp's neonize build and are now
  forwarded through `contact-state-changed` after storage:
  `ContactUpdateEv` for a contact saved, renamed or removed on the phone, and
  `AppStateSyncCompleteEv` for the bulk of them right after pairing, which
  reloads neonize's contact store. `PushNameUpdateEv` keeps a nickname under
  both forms of the JID and emits a separate profile observation, without
  marking it as a saved contact.

Each fact is only emitted when it changes from the last one emitted
(`Announced`, in RAM): the bridge's store already keeps the names and is not
resent. The pure functions are at the top (tested in test_contact_events.py).
Nothing here was proven against a real account. No JID or name goes to the log.
"""
import logging
import threading

import msgmap
import contact_state_events
import profile_picture_events
from profile_privacy import photo_to_wpp, no_photo

log = logging.getLogger("bridge")

MAX_CONTACTS = 2000
# Photos fetched at the same time (each one is a network call).
CONCURRENT_FETCHES = 4
_fetches = threading.BoundedSemaphore(CONCURRENT_FETCHES)

PERSON_SERVERS = ("s.whatsapp.net", "c.us", "lid")


def is_person(jid) -> bool:
    """Is the neonize JID a person's (not a group, channel or list)?"""
    return bool(getattr(jid, "User", "")) and getattr(jid, "Server", "") in PERSON_SERVERS


def clean_name(name) -> str:
    """The nickname without spaces at the ends; '' if it is not text or is empty."""
    return name.strip() if isinstance(name, str) else ""


def contact_wpp(jid_wpp: str, name: str = "", photo: str = "") -> dict:
    """A contact of the `data`: only carries `pushName` and `profilePicUrl` when
    there is something to say (the client ignores an empty field)."""
    contact = {"remoteJid": jid_wpp}
    if name:
        contact["pushName"] = name
    if photo:
        contact["profilePicUrl"] = photo
    return contact


def contacts_event(session: str, contacts: list) -> dict:
    """The body of `contacts.update`: the session (the client discards an event
    from another) and the list of contacts."""
    return {"session": session, "data": contacts}


class Announced:
    """The last name and the last photo emitted for each contact, so as not to
    repeat. The photo is compared by its id (the URL expires and changes with
    every request)."""

    def __init__(self):
        self._seen = {}   # jid_wpp -> {"name": str, "photo": str}

    def _changed(self, jid_wpp, field, value) -> bool:
        if not value:
            return False
        record = self._seen.pop(jid_wpp, None) or {}   # pop+set: the newest stays at the end
        changed = record.get(field) != value
        record[field] = value
        self._seen[jid_wpp] = record
        while len(self._seen) > MAX_CONTACTS:
            del self._seen[next(iter(self._seen))]
        return changed

    def name_changed(self, jid_wpp, name) -> bool:
        return self._changed(jid_wpp, "name", name)

    def photo_changed(self, jid_wpp, photo_id) -> bool:
        return self._changed(jid_wpp, "photo", photo_id)


def _text(jid) -> str:
    return f"{jid.User}@{jid.Server}" if getattr(jid, "User", "") else ""


def jid_for_app(c, jid) -> str:
    """neonize JID -> the id the app uses ('...@c.us'). A `@lid` becomes the number
    when whatsmeow knows it; otherwise it stays `@lid` (the caller decides)."""
    text = _text(jid)
    if getattr(jid, "Server", "") == "lid":
        try:
            number = _text(c.get_pn_from_lid(jid))
        except Exception:
            number = ""
        if number:
            text = number
    return msgmap.jid_wpp(text)


def announce_name(c, s, jid, name) -> bool:
    """Emits `contacts.update` with `jid`'s nickname if it changed since the last
    one emitted. For server.py's MessageEv handler to call with the sender and
    the `Info.Pushname` of another person's message. True if it emitted."""
    name = clean_name(name)
    if not name or not is_person(jid):
        return False
    wpp_id = jid_for_app(c, jid)
    if wpp_id.endswith("@lid"):     # no number: it would create a contact with an LID key
        return False
    if not s.announced_contacts.name_changed(wpp_id, name):
        return False
    log.info("contact: nickname changed")
    s.emit("contacts.update", contacts_event(
        s.name, [contact_wpp(wpp_id, name=name)]))
    return True


def announce_photo(c, s, jid) -> bool:
    """Fetches `jid`'s current photo and emits `contacts.update` with the URL if the
    photo's id changed since the last one emitted. Network call: never in the
    event handler. True if it emitted."""
    with _fetches:
        try:
            info = c.get_profile_picture(jid)
        except Exception as error:
            if not no_photo(error):      # no photo is a definitive answer, not a failure
                log.info("contact photo: %s", type(error).__name__)
            return False
    photo = photo_to_wpp(info, "")
    if photo is None:
        return False
    wpp_id = jid_for_app(c, jid)
    if not s.announced_contacts.photo_changed(wpp_id, photo["tag"] or photo["eurl"]):
        return False
    log.info("contact: photo changed")
    s.emit("contacts.update", contacts_event(
        s.name, [contact_wpp(wpp_id, photo=photo["eurl"])]))
    return True


def _in_background(func, *args):
    threading.Thread(target=func, args=args, daemon=True, name="contact-photo").start()


def _announce_photo_safely(c, s, jid):
    try:
        announce_photo(c, s, jid)
    except Exception:
        log.exception("contact photo")


#: The app state collection that carries the address book's names.
ADDRESS_BOOK_COLLECTION = "critical_unblock_low"


def saved_name(action) -> str:
    """The address-book name of a ContactAction: full name, else first name.
    '' means the phone no longer has a name for the contact."""
    if action is None:
        return ""
    return clean_name(getattr(action, "fullName", "")) or clean_name(getattr(action, "firstName", ""))


def address_book_synced(collection) -> bool:
    """Did this full app state sync bring the address book?"""
    return str(collection) == ADDRESS_BOOK_COLLECTION


def _reload_names_safely(s):
    try:
        s.reload_names()
    except Exception:
        log.exception("contact names")


def register_events(cli, s):
    """Wires the neonize client's `cli` contact events to the session `s` (needs
    `s.name`, `s.emit`, `s.db` and `s.reload_names`) and creates
    `s.announced_contacts`, which `announce_name` uses. Called on neonize's thread.

    ContactUpdateEv, PushNameUpdateEv and AppStateSyncCompleteEv come from
    WinZapp's neonize build (neonize_patches/): an older build lacks them, and
    the bridge then relies on Session.reload_names() alone."""
    import neonize.events as nev

    s.announced_contacts = Announced()

    if hasattr(nev, "ContactUpdateEv"):
        @cli.event(nev.ContactUpdateEv)
        def _address_book(c, e):
            # saved, renamed or removed on the phone; a full sync's contacts do not
            # come this way (AppStateSyncCompleteEv below)
            try:
                if not is_person(e.JID):
                    return
                name = saved_name(e.Action)
                if contact_state_events.address_book(s, e, name, _text(e.JID)):
                    log.info("contact: address book entry %s", "saved" if name else "removed")
            except Exception:
                log.exception("address book changed")

    if hasattr(nev, "AppStateSyncCompleteEv"):
        @cli.event(nev.AppStateSyncCompleteEv)
        def _state_synced(c, e):
            # right after pairing the names land in neonize's store in bulk, with
            # no ContactUpdateEv: this is when they are there to read
            try:
                if address_book_synced(e.Name):
                    _in_background(_reload_names_safely, s)
                # app_state_mirror's copy of the collection is complete now
                synced = getattr(s, "app_state_synced", None)
                if synced is not None:
                    synced(e.Name)
            except Exception:
                log.exception("app state synced")

    if hasattr(nev, "PushNameUpdateEv"):
        @cli.event(nev.PushNameUpdateEv)
        def _nickname(c, e):
            # MessageEv already stores and announces the nickname under the sender;
            # this keeps it under the other form of the JID too (number <-> LID)
            try:
                name = clean_name(e.NewPushName)
                if name and is_person(getattr(e, "JID", None)):
                    contact_state_events.profile(s, _text(e.JID), name)
                if name and e.HasField("JIDAlt") and is_person(e.JIDAlt):
                    s.db.contact(_text(e.JIDAlt), pushname=name)
            except Exception:
                log.exception("nickname changed")

    @cli.event(nev.PictureEv)
    def _photo(c, e):
        try:
            profile_picture_events.changed(s, _text(e.JID))
            if e.Remove or not is_person(e.JID):
                return
            _in_background(_announce_photo_safely, c, s, e.JID)
        except Exception:
            log.exception("photo changed")
