from store import Store


def _msg(i, ts):
    return {"id": {"id": i}, "t": ts, "body": i}


def test_latest_and_previous(tmp_path):
    a = Store(str(tmp_path / "x.db"))
    a.messages_in_batch([("c", _msg(f"m{n}", 100 + n)) for n in range(10)])
    assert [m["body"] for m in a.messages("c", 3)] == ["m7", "m8", "m9"]
    assert [m["body"] for m in a.messages("c", 3, "m7")] == ["m4", "m5", "m6"]
    assert a.last("c")["body"] == "m9"
    assert a.messages("other", 5) == []


def test_chat_counts_unread_and_sorts(tmp_path):
    a = Store(str(tmp_path / "x.db"))
    a.chat("a", name="A", ts=5)
    a.on_arrival("a", 9, False)
    a.on_arrival("a", 7, True)
    a.on_arrival("b", 20, False)
    listing = a.chats()
    assert [c["jid"] for c in listing] == ["b", "a"]
    assert listing[1]["unread"] == 1 and listing[1]["ts"] == 9 and listing[1]["name"] == "A"


def test_media_record_by_id(tmp_path):
    a = Store(str(tmp_path / "x.db"))
    a.message("c", _msg("m1", 1), b"record")
    a.message("c", _msg("m2", 2))
    assert a.media_record("m1") == b"record"
    assert a.media_record("m2") is None and a.media_record("does-not-exist") is None


def test_contact_of_merges_the_forms_of_the_jid(tmp_path):
    a = Store(str(tmp_path / "x.db"))
    a.contact("1@lid", pushname="Ana")
    a.contact("5551@s.whatsapp.net", name="Ana Silva")
    assert a.contact_of("1@lid", "5551@s.whatsapp.net") == {"pushname": "Ana", "name": "Ana Silva"}
    assert a.contact_of("x@lid", "") == {}


def test_received_to_read_only_the_last_unread(tmp_path):
    a = Store(str(tmp_path / "x.db"))
    for n in range(10):
        w = _msg(f"m{n}", 100 + n)
        w.update(fromMe=(n == 9), **{"from": "x@s.whatsapp.net"})
        a.message("c", w)
    a.chat("c", unread=3)
    found = a.received_to_read("c")
    assert [i for i, _, _ in found] == ["m6", "m7", "m8"]     # m9 is mine
    assert a.chats()[0]["unread"] == 0
    assert a.received_to_read("c") == []                          # already read


def test_last_received_ignores_mine(tmp_path):
    a = Store(str(tmp_path / "x.db"))
    for n, mine in ((1, False), (2, False), (3, True)):
        w = _msg(f"m{n}", 100 + n)
        w["fromMe"] = mine
        a.message("c", w)
    assert a.last_received("c") == "m2"
    assert a.last_received("empty") == ""


def test_apply_revocations_marks_the_original_as_deleted(tmp_path):
    a = Store(str(tmp_path / "x.db"))
    a.message("c", {"id": {"id": "ORIG"}, "t": 1, "type": "chat", "body": "oi"})
    a.message("c", {"id": {"id": "NOTICE"}, "t": 2, "type": "revoked", "body": "",
                    "protocolMessageKey": {"id": "ORIG"}})
    assert a.apply_revocations() == 1
    original = a.wpp_message("ORIG")
    assert original["type"] == "revoked" and original["body"] == ""


def test_reactions_change_and_disappear(tmp_path):
    a = Store(str(tmp_path / "x.db"))
    a.react("m1", "me", "A", 1)
    a.react("m1", "5551@c.us", "B", 2)
    a.react("m1", "me", "C", 3)                 # changed the own reaction
    assert dict(a.reactions_of("m1")) == {"me": "C", "5551@c.us": "B"}
    a.react("m1", "me", "", 4)                  # removed
    assert a.reactions_of("m1") == [("5551@c.us", "B")]
    assert a.reactions_of("other") == []


def test_votes_change_and_disappear(tmp_path):
    a = Store(str(tmp_path / "x.db"))
    a.vote("p1", "me", [0], 1)
    a.vote("p1", "5551@c.us", [1, 2], 2)
    a.vote("p1", "me", [2], 3)                  # changed the vote
    assert dict(a.votes_of("p1")) == {"me": [2], "5551@c.us": [1, 2]}
    a.vote("p1", "me", [], 4)                   # removed the vote
    assert a.votes_of("p1") == [("5551@c.us", [1, 2])]


def test_pinned_messages_respect_the_deadline(tmp_path):
    a = Store(str(tmp_path / "x.db"))
    a.messages_in_batch([("c", _msg("m1", 10)), ("c", _msg("m2", 20)), ("d", _msg("m3", 30))])
    a.pin_message("m1", "c", True, ts=100, duration=50)     # expires at 150
    a.pin_message("m2", "c", True, ts=100, duration=0)      # no known deadline
    a.pin_message("m3", "d", True, ts=100, duration=500)
    assert [m["body"] for m in a.pinned_of(["c"], 120)] == ["m1", "m2"]
    assert [m["body"] for m in a.pinned_of(["c"], 200)] == ["m2"]
    assert [m["body"] for m in a.pinned_of(["c", "d"], 200)] == ["m2", "m3"]
    a.pin_message("m2", "c", False)
    assert a.pinned_of(["c"], 200) == []


def test_unknown_anchor_does_not_return_the_newest(tmp_path):
    """direction=before with an id the bridge does not have was read, in the client, as
    'previous page' the newest messages, repeated."""
    a = Store(str(tmp_path / "x.db"))
    a.messages_in_batch([("111@lid", _msg(f"M{n}", 100 + n)) for n in range(1, 5)])
    assert [m["body"] for m in a.messages("111@lid", 3, "M3")] == ["M1", "M2"]
    assert a.messages("111@lid", 3, "ZZZ") == []


def test_anchor_found_under_the_other_address_of_the_chat(tmp_path):
    a = Store(str(tmp_path / "x.db"))
    a.messages_in_batch([("111@lid", _msg("M1", 101)), ("111@lid", _msg("M2", 102))])
    a.messages_in_batch([("55@s.whatsapp.net", _msg("M3", 103))])
    # asked for by only one address, the anchor is in the other: it still finds its instant
    assert [m["body"] for m in a.messages("55@s.whatsapp.net", 5, "M2")] == []
    assert [m["body"] for m in a.messages(("55@s.whatsapp.net", "111@lid"), 5, "M3")] == ["M1", "M2"]


def test_messages_joins_the_two_addresses_in_order(tmp_path):
    a = Store(str(tmp_path / "x.db"))
    a.messages_in_batch([("111@lid", _msg("M1", 101)), ("55@s.whatsapp.net", _msg("M2", 102)),
                         ("111@lid", _msg("M3", 103)), ("other@lid", _msg("X", 104))])
    assert [m["body"] for m in a.messages(["55@s.whatsapp.net", "111@lid"], 10)] == [
        "M1", "M2", "M3"]
    assert [m["body"] for m in a.messages("111@lid", 10)] == ["M1", "M3"]


def test_for_response_always_sets_a_boolean_star(tmp_path):
    a = Store(str(tmp_path / "x.db"))
    without = _msg("M1", 1)
    with_star = {**_msg("M2", 2), "star": True}
    output = a.for_response([without, with_star])
    assert [w["star"] for w in output] == [False, True]
    assert "star" not in without                    # does not touch the stored ones


def test_for_response_sets_ack_on_mine_from_the_receipts(tmp_path):
    a = Store(str(tmp_path / "x.db"))

    def mine(i):
        return {**_msg(i, 1), "fromMe": True}
    a.receipt("A", "5@c.us", "delivered", 5)
    a.receipt("B", "5@c.us", "delivered", 5)
    a.receipt("B", "5@c.us", "read", 6)
    a.receipt("C", "5@c.us", "played", 7)
    output = {w["id"]["id"]: w for w in a.for_response(
        [mine("A"), mine("B"), mine("C"), mine("D"), _msg("E", 1)])}
    assert [output[i]["ack"] for i in "ABCD"] == [2, 3, 4, 1]     # without a receipt: sent
    assert "ack" not in output["E"]                               # received has none


def test_ack_from_receipts_ignores_an_unknown_stage():
    from store import receipts_ack
    assert receipts_ack([]) == 1
    assert receipts_ack([("a", "sent", 1)]) == 1


def test_copy_state_follows_the_chat_under_two_addresses(tmp_path):
    a = Store(str(tmp_path / "x.db"))
    a.chat("111@lid", archived=1, pinned=77, muted=9)
    a.chat("55@s.whatsapp.net", name="Ana")
    a.copy_state("111@lid", "55@s.whatsapp.net")
    pair = {c["jid"]: c for c in a.chats()}["55@s.whatsapp.net"]
    assert (pair["archived"], pair["pinned"], pair["muted"], pair["name"]) == (1, 77, 9, "Ana")
    a.copy_state("does-not-exist", "55@s.whatsapp.net")          # missing source: nothing changes
    assert {c["jid"]: c for c in a.chats()}["55@s.whatsapp.net"]["archived"] == 1


def test_for_response_leaves_the_group_ack_to_ack_of(tmp_path):
    a = Store(str(tmp_path / "x.db"))
    a.receipt("A", "5@c.us", "read", 6)

    def mine(i):
        return {**_msg(i, 1), "fromMe": True}
    output = a.for_response([mine("A"), mine("B")],
                            ack_of=lambda w: 2 if w["id"]["id"] == "A" else None)
    # "A" has a read receipt, but ack_of decided 2; "B" (None) uses the receipts
    assert [w["ack"] for w in output] == [2, 1]


def test_contacts_in_batch_writes_only_what_changed_and_counts_saved_names(tmp_path):
    a = Store(str(tmp_path / "x.db"))
    a.contact("p1", pushname="Nick")                   # known only by the nickname
    learned = a.contacts_in_batch([("p1", "Ana", ""), ("p2", "", "Beto"), ("p3", "Caio", "C")])
    assert learned == 2
    assert a.contact_of("p1") == {"name": "Ana", "pushname": "Nick"}   # empty keeps
    assert a.contact_of("p2") == {"pushname": "Beto"}
    assert a.contacts_in_batch([("p1", "Ana", "Nick"), ("p3", "Caio", "C")]) == 0
    assert a.contacts_in_batch([("p3", "Caio Lima", "")]) == 1
    assert a.contact_of("p3")["name"] == "Caio Lima"


def test_set_contact_name_clears_a_name_the_phone_no_longer_has(tmp_path):
    a = Store(str(tmp_path / "x.db"))
    a.contact("p1", name="Ana", pushname="ana")
    a.set_contact_name("p1", "")
    assert a.contact_of("p1") == {"pushname": "ana"}
    a.set_contact_name("p2", "Bia")
    assert a.contact_of("p2") == {"name": "Bia"}


def test_a_date_lifted_by_a_group_notice_goes_back_to_the_real_message(tmp_path):
    import sqlite3

    import store
    path = str(tmp_path / "b.db")
    db = Store(path)
    real = {"id": {"id": "R"}, "t": 100, "type": "chat", "body": "oi"}
    notice = {"id": {"id": "N"}, "t": 500, "type": "gp2", "subtype": "leave"}
    for chat in ("lifted@g.us", "notice-only@g.us", "newer@g.us"):
        if chat != "notice-only@g.us":
            db.message(chat, dict(real))
        db.message(chat, dict(notice))
        db.chat(chat, ts=500)                     # what the old bridge wrote
    db.message("newer@g.us", {"id": {"id": "R2"}, "t": 500, "body": "no type"})
    db.chat("history@g.us", ts=900)               # a date from a history sync
    db.close()
    assert store.lower_dates_raised_by_notices(sqlite3.connect(path)) == 2
    dates = {c["jid"]: c["ts"] for c in Store(path).chats()}
    # with only the notice, 0: the client sorts it by the messages it holds
    assert dates == {"lifted@g.us": 100, "notice-only@g.us": 0, "newer@g.us": 500,
                     "history@g.us": 900}


def test_chats_in_batch_writes_like_chat(tmp_path):
    db = Store(str(tmp_path / "b.db"))
    db.chat("1@s.whatsapp.net", unread=5, archived=1)
    db.chats_in_batch([("1@s.whatsapp.net", {"ts": 50, "nope": 1}), ("2@g.us", {"name": "G"}),
                       ("3@s.whatsapp.net", {})])
    chats = {c["jid"]: c for c in db.chats()}
    assert chats["1@s.whatsapp.net"]["ts"] == 50 and chats["1@s.whatsapp.net"]["unread"] == 5
    assert chats["1@s.whatsapp.net"]["archived"] == 1
    assert chats["2@g.us"]["name"] == "G" and "3@s.whatsapp.net" in chats


def test_a_message_is_looked_up_by_its_id_through_an_index(tmp_path):
    db = Store(str(tmp_path / "b.db"))
    db.messages_in_batch([("1@s.whatsapp.net", _msg("A", 10))])
    plan = db._conn.execute("EXPLAIN QUERY PLAN SELECT data FROM msgs WHERE id=?", ("A",)).fetchall()
    assert any("msgs_by_id" in str(row[-1]) for row in plan), plan
    assert db.wpp_message("A")["id"]["id"] == "A"


def test_a_chat_the_history_gave_no_date_takes_the_date_of_its_newest_real_message(tmp_path):
    a = Store(str(tmp_path / "x.db"))
    a.chat("old@lid", name="", ts=0)             # history: no activity time
    a.chat("dated@lid", ts=50)                   # a date the history gave stays
    a.chat("empty@lid", ts=0)                    # no message either: stays undated
    a.chat("notice@g.us", ts=0)
    a.message("old@lid", _msg("m1", 700))
    a.message("old@lid", _msg("m2", 900))
    a.message("dated@lid", _msg("m3", 60))
    a.message("notice@g.us", {"id": {"id": "n1"}, "t": 800, "type": "gp2", "subtype": "leave"})

    listing = {c["jid"]: c["ts"] for c in a.chats()}

    assert listing == {"old@lid": 900, "dated@lid": 50, "empty@lid": 0, "notice@g.us": 0}
    assert [c["jid"] for c in a.chats()][0] == "old@lid"


def test_the_date_taken_from_the_messages_is_kept(tmp_path):
    path = str(tmp_path / "x.db")
    a = Store(path)
    a.chat("old@lid", ts=0)
    a.message("old@lid", _msg("m1", 700))
    a.chats()
    a.close()

    assert {c["jid"]: c["ts"] for c in Store(path).chats()} == {"old@lid": 700}


def test_a_notice_newer_than_the_last_real_message_does_not_date_the_chat(tmp_path):
    a = Store(str(tmp_path / "x.db"))
    a.chat("g@g.us", ts=0)
    a.message("g@g.us", _msg("real", 300))
    a.message("g@g.us", {"id": {"id": "n"}, "t": 900, "type": "gp2", "subtype": "join"})

    assert {c["jid"]: c["ts"] for c in a.chats()} == {"g@g.us": 300}


def _dated_one_chat_at_a_time(conn):
    """The dating chats() did before it became one query: the reference."""
    out = {}
    for jid, ts in conn.execute("SELECT jid, ts FROM chats").fetchall():
        if int(ts or 0) > 0:
            out[jid] = int(ts)
            continue
        found = conn.execute(
            "SELECT ts FROM msgs WHERE chat=? "
            "AND IFNULL(json_extract(data, '$.type'), '') <> 'gp2' "
            "ORDER BY ts DESC LIMIT 1", (jid,)).fetchone()
        out[jid] = int(found[0]) if found and found[0] else int(ts or 0)
    return out


def test_one_query_dates_the_chats_exactly_as_one_query_per_chat_did(tmp_path):
    """REGRESSION: list-chats spent 1.1 s in chats() on a real account (6,760
    dateless chats), one query each, holding the store's lock: the bridge's
    loop stood still and sends waited. The single query must give the same
    dates."""
    import random
    import sqlite3
    rng = random.Random(7)
    path = str(tmp_path / "x.db")
    a = Store(path)
    for n in range(300):
        jid = f"{n}@{'g.us' if n % 3 == 0 else 'lid'}"
        a.chat(jid, ts=rng.choice([0, 0, 0, rng.randint(1, 999)]))
        for m in range(rng.randint(0, 4)):
            kind = rng.choice(["chat", "chat", "gp2", "call_log", "revoked"])
            a.message(jid, {"id": {"id": f"{n}-{m}"}, "t": rng.randint(1, 999), "type": kind})
    a.close()
    reference = _dated_one_chat_at_a_time(sqlite3.connect(path))

    listing = {c["jid"]: c["ts"] for c in Store(path).chats()}

    assert listing == reference


def test_the_receipts_of_one_event_are_stored_like_one_receipt_each(tmp_path):
    a = Store(str(tmp_path / "x.db"))
    a.receipt("M1", "x@lid", "delivered", 10)
    a.receipts(["M1", "M2", "M3"], "x@lid", "delivered", 20)
    a.receipts(["M2"], "x@lid", "read", 30)
    a.receipts([], "x@lid", "read", 40)
    # the first time a stage was seen is kept, as receipt() keeps it
    assert a.receipts_of("M1") == [("x@lid", "delivered", 10)]
    assert a.receipts_of("M2") == [("x@lid", "delivered", 20), ("x@lid", "read", 30)]
    assert a.receipts_of("M3") == [("x@lid", "delivered", 20)]


def test_the_store_keeps_nothing_of_a_channel_whatever_the_path(tmp_path):
    s = Store(str(tmp_path / "x.db"))
    ch = "120363111@newsletter"
    s.chat(ch, name="News")
    s.chats_in_batch([(ch, {"name": "News"})])
    s.message(ch, _msg("m1", 100))
    s.messages_in_batch([(ch, _msg("m2", 101))])
    s.on_arrival(ch, 102, False)
    s.chat("5551@s.whatsapp.net", name="Ann")
    assert [c["jid"] for c in s.chats()] == ["5551@s.whatsapp.net"]
    assert s.messages(ch, 10) == []
