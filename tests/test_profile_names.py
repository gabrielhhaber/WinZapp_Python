"""One profile name per person: a rename reaches every address and every row."""
from core.profile_names import apply_profile_name

PHONE = "5511999990000@s.whatsapp.net"
LID = "1234@lid"


def _world():
    contacts = {
        PHONE: {"remoteJid": PHONE, "name": "alex oliveira", "pushName": "alex oliveira"},
        LID: {"remoteJid": LID, "name": "alex oliveira", "pushName": "alex oliveira"},
    }
    return contacts, {LID: "alex oliveira"}, {}


def _apply(world, name, ts, authoritative=True, targets=(PHONE, LID), chats=None):
    contacts, ppm, stamps = world
    return apply_profile_name(contacts, ppm, stamps, list(targets), name, ts,
                              authoritative=authoritative, chats=chats)


def test_rename_reaches_every_address_and_the_name_field():
    world = _world()
    assert _apply(world, "alex oliveira grupos", 100)
    contacts, ppm, _ = world
    for jid in (PHONE, LID):
        assert contacts[jid]["name"] == contacts[jid]["pushName"] == "alex oliveira grupos"
    assert ppm[LID] == "alex oliveira grupos"


def test_address_book_name_is_kept_but_push_name_moves():
    world = _world()
    world[0][PHONE]["isMyContact"] = True
    world[0][PHONE]["name"] = "Alex (agenda)"
    _apply(world, "alex oliveira grupos", 100)
    assert world[0][PHONE]["name"] == "Alex (agenda)"
    assert world[0][PHONE]["pushName"] == "alex oliveira grupos"


def test_older_message_never_brings_the_previous_name_back():
    world = _world()
    _apply(world, "alex oliveira grupos", 200)
    assert not _apply(world, "alex oliveira", 100)
    assert world[0][LID]["name"] == "alex oliveira grupos"


def test_history_replay_only_fills_a_gap():
    world = _world()
    assert not _apply(world, "old name", 50, authoritative=False)
    assert world[0][LID]["name"] == "alex oliveira"
    fresh = ({}, {}, {})
    assert _apply(fresh, "first name", 50, authoritative=False)
    assert fresh[1][LID] == "first name"


def test_return_to_the_original_name_wins_when_newer():
    world = _world()
    _apply(world, "alex oliveira grupos", 100)
    _apply(world, "alex oliveira", 300)
    assert world[0][PHONE]["name"] == "alex oliveira"


def test_private_chat_named_after_the_old_profile_follows():
    world = _world()
    chats = {PHONE: {"name": "alex oliveira"}}
    _apply(world, "alex oliveira grupos", 100, chats=chats)
    assert chats[PHONE]["name"] == "alex oliveira grupos"


def _bad(name):
    return not name


def test_freshest_name_beats_a_stale_record_of_the_other_address():
    from core.profile_names import freshest_profile_name
    contacts = {PHONE: {"name": "alex grupos", "pushName": "alex grupos"}}
    ppm = {LID: "alex oliveira", PHONE: "alex grupos"}
    stamps = {LID: 200}
    assert freshest_profile_name(contacts.get, ppm, stamps, [PHONE, LID], _bad) == "alex oliveira"


def test_freshest_name_yields_to_an_address_book_name_and_to_no_stamp():
    from core.profile_names import freshest_profile_name
    saved = {PHONE: {"name": "Alex (agenda)", "isMyContact": True}}
    assert freshest_profile_name(saved.get, {LID: "alex"}, {LID: 5}, [PHONE, LID], _bad) == ""
    assert freshest_profile_name({}.get, {LID: "alex"}, {}, [PHONE, LID], _bad) == ""


def test_the_newest_stamp_wins_between_addresses():
    from core.profile_names import freshest_profile_name
    ppm = {LID: "novo", PHONE: "velho"}
    assert freshest_profile_name({}.get, ppm, {LID: 300, PHONE: 100}, [PHONE, LID], _bad) == "novo"
