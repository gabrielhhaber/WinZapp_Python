"""Tests for core.database.DatabaseManager — async edition."""

import json
import time
from typing import Any

import anyio
import pytest
from cryptography.fernet import Fernet


@pytest.fixture
def frozen_clock(monkeypatch):
    """Pin the clock core.database stamps blacklist entries with.

    The only way to age an unresolvable_lids row without waiting a week.
    core.database uses `time` for nothing else (see add_unresolvable_lid).
    """
    class _Clock:
        now = 0

        def time(self):
            return self.now

    from core import database

    clock = _Clock()
    monkeypatch.setattr(database, "time", clock)
    return clock


# =============================================================================
#  Chats
# =============================================================================


class TestChats:
    async def test_upsert_chat_creates_record(self, in_memory_db):
        await in_memory_db.upsert_chat("jid@w", {"remoteJid": "jid@w", "pushName": "Foo"})
        chats = await in_memory_db.get_chats()
        assert "jid@w" in chats
        assert chats["jid@w"]["pushName"] == "Foo"
        assert chats["jid@w"]["remoteJid"] == "jid@w"

    async def test_upsert_chat_updates_existing(self, in_memory_db):
        await in_memory_db.upsert_chat("jid@w", {"remoteJid": "jid@w", "pushName": "Foo"})
        await in_memory_db.upsert_chat("jid@w", {"remoteJid": "jid@w", "pushName": "Bar"})
        chats = await in_memory_db.get_chats()
        assert chats["jid@w"]["pushName"] == "Bar"

    async def test_get_chats_returns_empty_dict_when_empty(self, in_memory_db):
        assert await in_memory_db.get_chats() == {}

    async def test_get_chats_includes_message_wrapper(self, in_memory_db):
        chat = {
            "remoteJid": "jid@w",
            "unreadCount": 2,
            "pushName": "Test",
            "name": "Test User",
            "type": "chat",
        }
        await in_memory_db.upsert_chat("jid@w", chat)
        chats = await in_memory_db.get_chats()
        assert "messages" in chats["jid@w"]
        assert chats["jid@w"]["messages"]["messages"]["total"] == 0
        assert chats["jid@w"]["messages"]["messages"]["records"] == []

    async def test_get_chats_includes_message_count(self, in_memory_db):
        chat = {"remoteJid": "jid@w", "pushName": "Test"}
        await in_memory_db.upsert_chat("jid@w", chat)

        for i in range(5):
            await in_memory_db.insert_message(
                "jid@w",
                {
                    "key": {"remoteJid": "jid@w", "id": f"msg{i}"},
                    "messageTimestamp": i,
                    "message": {"conversation": f"msg{i}"},
                    "messageType": "conversation",
                },
            )

        chats = await in_memory_db.get_chats()
        assert chats["jid@w"]["messages"]["messages"]["total"] == 5

    async def test_get_chats_archived_flag(self, in_memory_db):
        chat = {"remoteJid": "jid@w", "archived": True}
        await in_memory_db.upsert_chat("jid@w", chat)
        chats = await in_memory_db.get_chats()
        assert chats["jid@w"]["archived"] is True
        assert chats["jid@w"]["archive"] is True

    async def test_upsert_chats_batch(self, in_memory_db):
        chats = {
            "a@w": {"remoteJid": "a@w", "pushName": "A"},
            "b@w": {"remoteJid": "b@w", "pushName": "B"},
        }
        await in_memory_db.upsert_chats_batch(chats)
        result = await in_memory_db.get_chats()
        assert set(result.keys()) == {"a@w", "b@w"}

    async def test_get_chat_jids(self, in_memory_db):
        await in_memory_db.upsert_chat("a@w", {"remoteJid": "a@w"})
        await in_memory_db.upsert_chat("b@w", {"remoteJid": "b@w"})
        jids = await in_memory_db.get_chat_jids()
        assert sorted(jids) == ["a@w", "b@w"]

    async def test_delete_chat_removes_chat_and_messages(self, in_memory_db):
        await in_memory_db.upsert_chat("jid@w", {"remoteJid": "jid@w"})
        await in_memory_db.insert_message("jid@w", {
            "key": {"remoteJid": "jid@w", "id": "m1"},
            "messageTimestamp": 1,
        })
        await in_memory_db.delete_chat("jid@w")
        chats = await in_memory_db.get_chats()
        assert "jid@w" not in chats
        msgs = await in_memory_db.get_messages("jid@w")
        assert msgs == []

    async def test_get_chats_default_limit_does_not_truncate_unread(self, in_memory_db):
        """Regression test: get_chats() used to default limit=5, so every
        unread badge/tray tooltip (which derive from len(records)) showed at
        most 5 even when the real unread count was higher."""
        await in_memory_db.upsert_chat("jid@w", {"remoteJid": "jid@w", "unreadCount": 12})
        for i in range(12):
            await in_memory_db.insert_message(
                "jid@w",
                {
                    "key": {"remoteJid": "jid@w", "id": f"m{i}"},
                    "messageTimestamp": i,
                },
            )
        chats = await in_memory_db.get_chats()
        assert len(chats["jid@w"]["messages"]["messages"]["records"]) == 12


# =============================================================================
#  Messages
# =============================================================================


class TestMessages:
    async def test_insert_and_retrieve(self, in_memory_db):
        msg = {
            "key": {"remoteJid": "jid@w", "id": "msg1"},
            "messageTimestamp": 1000,
            "message": {"conversation": "hello"},
            "messageType": "conversation",
        }
        await in_memory_db.insert_message("jid@w", msg)
        msgs = await in_memory_db.get_messages("jid@w")
        assert len(msgs) == 1
        assert msgs[0]["key"]["id"] == "msg1"
        assert msgs[0]["message"]["conversation"] == "hello"

    async def test_get_messages_newest_first(self, in_memory_db):
        for i in range(10):
            await in_memory_db.insert_message(
                "jid@w",
                {
                    "key": {"remoteJid": "jid@w", "id": f"msg{i}"},
                    "messageTimestamp": i,
                    "message": {"conversation": f"text{i}"},
                    "messageType": "conversation",
                },
            )
        msgs = await in_memory_db.get_messages("jid@w", limit=3)
        assert len(msgs) == 3
        assert msgs[0]["key"]["id"] == "msg9"
        assert msgs[-1]["key"]["id"] == "msg7"

    async def test_get_messages_asc_oldest_first(self, in_memory_db):
        for i in range(5):
            await in_memory_db.insert_message(
                "jid@w",
                {
                    "key": {"remoteJid": "jid@w", "id": f"msg{i}"},
                    "messageTimestamp": i,
                },
            )
        msgs = await in_memory_db.get_messages_asc("jid@w", limit=3)
        assert len(msgs) == 3
        assert msgs[0]["key"]["id"] == "msg0"
        assert msgs[-1]["key"]["id"] == "msg2"

    async def test_get_messages_pagination(self, in_memory_db):
        for i in range(50):
            await in_memory_db.insert_message(
                "jid@w",
                {
                    "key": {"remoteJid": "jid@w", "id": f"msg{i:02d}"},
                    "messageTimestamp": i,
                },
            )
        page1 = await in_memory_db.get_messages("jid@w", limit=20, offset=0)
        page2 = await in_memory_db.get_messages("jid@w", limit=20, offset=20)
        assert len(page1) == 20
        assert len(page2) == 20
        assert page1[0]["key"]["id"] != page2[0]["key"]["id"]

    async def test_get_message_count(self, in_memory_db):
        count = await in_memory_db.get_message_count("jid@w")
        assert count == 0
        for i in range(7):
            await in_memory_db.insert_message(
                "jid@w",
                {
                    "key": {"remoteJid": "jid@w", "id": f"m{i}"},
                    "messageTimestamp": i,
                },
            )
        count = await in_memory_db.get_message_count("jid@w")
        assert count == 7

    def test_jid_variants_expands_s_whatsapp_net_to_c_us(self, in_memory_db):
        variants = in_memory_db._jid_variants("5511999999999@s.whatsapp.net")
        assert variants == [
            "5511999999999@s.whatsapp.net",
            "5511999999999@c.us",
        ]

    def test_jid_variants_expands_c_us_to_s_whatsapp_net(self, in_memory_db):
        variants = in_memory_db._jid_variants("5511999999999@c.us")
        assert variants == [
            "5511999999999@c.us",
            "5511999999999@s.whatsapp.net",
        ]

    def test_jid_variants_leaves_other_suffixes_untouched(self, in_memory_db):
        assert in_memory_db._jid_variants("120363@g.us") == ["120363@g.us"]

    async def test_get_messages_finds_message_stored_under_c_us_variant(
        self, in_memory_db
    ):
        # A message inserted under the legacy @c.us form must still be found
        # by a @s.whatsapp.net lookup (and vice versa) — see _jid_variants().
        await in_memory_db.insert_message(
            "5511999999999@c.us",
            {
                "key": {"remoteJid": "5511999999999@c.us", "id": "m1"},
                "messageTimestamp": 1,
            },
        )
        msgs = await in_memory_db.get_messages("5511999999999@s.whatsapp.net")
        assert len(msgs) == 1
        assert msgs[0]["key"]["id"] == "m1"

        msgs_asc = await in_memory_db.get_messages_asc(
            "5511999999999@s.whatsapp.net"
        )
        assert len(msgs_asc) == 1

        count = await in_memory_db.get_message_count(
            "5511999999999@s.whatsapp.net"
        )
        assert count == 1

    async def test_insert_updates_existing_message(self, in_memory_db):
        msg1 = {
            "key": {"remoteJid": "jid@w", "id": "m1"},
            "messageTimestamp": 1,
            "message": {"conversation": "first"},
            "messageType": "conversation",
        }
        msg2 = {
            "key": {"remoteJid": "jid@w", "id": "m1"},
            "messageTimestamp": 2,
            "message": {"conversation": "second"},
            "messageType": "conversation",
        }
        await in_memory_db.insert_message("jid@w", msg1)
        await in_memory_db.insert_message("jid@w", msg2)
        msgs = await in_memory_db.get_messages("jid@w")
        assert len(msgs) == 1
        assert msgs[0]["message"]["conversation"] == "second"

    async def test_update_message_status(self, in_memory_db):
        await in_memory_db.insert_message(
            "jid@w",
            {
                "key": {"remoteJid": "jid@w", "id": "m1"},
                "messageTimestamp": 1,
            },
        )
        await in_memory_db.update_message_status("jid@w", "m1", 3)
        msgs = await in_memory_db.get_messages("jid@w")
        assert len(msgs) == 1

    async def test_delete_chat_messages(self, in_memory_db):
        await in_memory_db.insert_message(
            "jid@w",
            {
                "key": {"remoteJid": "jid@w", "id": "m1"},
                "messageTimestamp": 1,
            },
        )
        await in_memory_db.delete_chat_messages("jid@w")
        count = await in_memory_db.get_message_count("jid@w")
        assert count == 0

    async def test_delete_single_message(self, in_memory_db):
        for i in range(3):
            await in_memory_db.insert_message(
                "jid@w",
                {
                    "key": {"remoteJid": "jid@w", "id": f"msg{i}"},
                    "messageTimestamp": i,
                },
            )
        await in_memory_db.delete_message("jid@w", "msg1")
        remaining = await in_memory_db.get_messages_asc("jid@w")
        ids = [m["key"]["id"] for m in remaining]
        assert ids == ["msg0", "msg2"]
        assert len(ids) == 2

    async def test_insert_messages_batch(self, in_memory_db):
        msgs = [
            {
                "key": {"remoteJid": "jid@w", "id": f"m{i}"},
                "messageTimestamp": i,
                "message": {"conversation": f"text{i}"},
                "messageType": "conversation",
            }
            for i in range(10)
        ]
        await in_memory_db.insert_messages_batch("jid@w", msgs)
        count = await in_memory_db.get_message_count("jid@w")
        assert count == 10

    async def test_get_messages_empty_chat(self, in_memory_db):
        msgs = await in_memory_db.get_messages("nonexistent@w")
        assert msgs == []

    async def test_insert_message_with_empty_id_is_dropped_not_overwritten(self, in_memory_db):
        """Two different id-less messages used to collide under the same
        (message_id='', remote_jid) primary key and silently overwrite each
        other via INSERT OR REPLACE — neither should ever be stored."""
        await in_memory_db.insert_message("jid@w", {
            "key": {"remoteJid": "jid@w", "id": ""},
            "messageTimestamp": 1,
            "message": {"conversation": "first, id-less"},
        })
        await in_memory_db.insert_message("jid@w", {
            "key": {"remoteJid": "jid@w", "id": ""},
            "messageTimestamp": 2,
            "message": {"conversation": "second, id-less"},
        })
        msgs = await in_memory_db.get_messages("jid@w")
        assert msgs == []

    async def test_insert_message_missing_key_id_is_dropped(self, in_memory_db):
        await in_memory_db.insert_message("jid@w", {
            "key": {"remoteJid": "jid@w"},
            "messageTimestamp": 1,
        })
        count = await in_memory_db.get_message_count("jid@w")
        assert count == 0

    async def test_insert_messages_batch_skips_id_less_without_dropping_rest(self, in_memory_db):
        msgs = [
            {"key": {"remoteJid": "jid@w", "id": "keep1"}, "messageTimestamp": 1},
            {"key": {"remoteJid": "jid@w", "id": ""}, "messageTimestamp": 2},
            {"key": {"remoteJid": "jid@w", "id": "keep2"}, "messageTimestamp": 3},
            {"key": {"remoteJid": "jid@w", "id": ""}, "messageTimestamp": 4},
        ]
        await in_memory_db.insert_messages_batch("jid@w", msgs)
        remaining = await in_memory_db.get_messages_asc("jid@w")
        ids = [m["key"]["id"] for m in remaining]
        assert ids == ["keep1", "keep2"]

    async def test_encrypted_message_json(self, in_memory_db, fernet_key):
        """Verify that message content is encrypted at rest."""
        msg = {
            "key": {"remoteJid": "jid@w", "id": "sec1"},
            "messageTimestamp": 42,
            "message": {"conversation": "sensitive data"},
            "messageType": "conversation",
        }
        await in_memory_db.insert_message("jid@w", msg)

        # Access internal connection to verify raw storage
        assert in_memory_db._conn is not None
        cursor = await in_memory_db._conn.execute(
            "SELECT message_json FROM messages WHERE message_id='sec1'"
        )
        row = await cursor.fetchone()
        raw = row["message_json"]
        assert "sensitive data" not in raw
        assert raw.startswith("gAAAAA")  # Fernet base64 token prefix


# =============================================================================
#  merge_or_rename_chat — used to dedupe a chat that exists under both an
#  @lid and a resolved phone JID into one.
# =============================================================================


class TestMergeOrRenameChat:
    async def test_rename_when_new_jid_does_not_exist(self, in_memory_db):
        await in_memory_db.upsert_chat("old@lid", {"remoteJid": "old@lid"})
        await in_memory_db.insert_message("old@lid", {
            "key": {"remoteJid": "old@lid", "id": "m1"},
            "messageTimestamp": 1,
        })
        await in_memory_db.merge_or_rename_chat("old@lid", "new@w")

        chats = await in_memory_db.get_chats()
        assert "old@lid" not in chats
        assert "new@w" in chats
        msgs = await in_memory_db.get_messages("new@w")
        assert [m["key"]["id"] for m in msgs] == ["m1"]

    async def test_merge_moves_non_colliding_messages(self, in_memory_db):
        """A message under old_jid whose id does not already exist under
        new_jid must survive the merge, not just the colliding ones."""
        await in_memory_db.upsert_chat("old@lid", {"remoteJid": "old@lid"})
        await in_memory_db.upsert_chat("new@w", {"remoteJid": "new@w"})
        await in_memory_db.insert_message("old@lid", {
            "key": {"remoteJid": "old@lid", "id": "only_in_old"},
            "messageTimestamp": 1,
        })
        await in_memory_db.insert_message("new@w", {
            "key": {"remoteJid": "new@w", "id": "only_in_new"},
            "messageTimestamp": 2,
        })

        await in_memory_db.merge_or_rename_chat("old@lid", "new@w")

        msgs = await in_memory_db.get_messages_asc("new@w")
        ids = {m["key"]["id"] for m in msgs}
        assert ids == {"only_in_old", "only_in_new"}
        old_msgs = await in_memory_db.get_messages("old@lid")
        assert old_msgs == []

    async def test_merge_with_colliding_message_id_keeps_new_jid_copy(self, in_memory_db):
        """Same message_id under both JIDs (the same real WhatsApp message,
        filed under its @lid form and its resolved phone form before the
        chats were merged) must not be lost — one copy survives under
        new_jid, and old_jid ends up with nothing left under that id."""
        await in_memory_db.upsert_chat("old@lid", {"remoteJid": "old@lid"})
        await in_memory_db.upsert_chat("new@w", {"remoteJid": "new@w"})
        await in_memory_db.insert_message("old@lid", {
            "key": {"remoteJid": "old@lid", "id": "shared"},
            "messageTimestamp": 1,
            "message": {"conversation": "from old"},
        })
        await in_memory_db.insert_message("new@w", {
            "key": {"remoteJid": "new@w", "id": "shared"},
            "messageTimestamp": 2,
            "message": {"conversation": "from new"},
        })

        await in_memory_db.merge_or_rename_chat("old@lid", "new@w")

        msgs = await in_memory_db.get_messages("new@w")
        assert len(msgs) == 1
        assert msgs[0]["key"]["id"] == "shared"
        old_msgs = await in_memory_db.get_messages("old@lid")
        assert old_msgs == []

    async def test_a_message_left_under_an_lid_is_invisible_from_the_phone_jid(
        self, in_memory_db
    ):
        """Why the merge is mandatory rather than cosmetic: _jid_variants()
        bridges @c.us <-> @s.whatsapp.net and nothing else, so a message still
        filed under the contact's @lid cannot be read back through the phone
        JID — which is the only one navigate_to_conversation() ever asks for
        once the chats have been merged in memory."""
        await in_memory_db.insert_message("old@lid", {
            "key": {"remoteJid": "old@lid", "id": "m1"},
            "messageTimestamp": 1,
        })

        assert await in_memory_db.get_messages("new@s.whatsapp.net") == []

        await in_memory_db.merge_or_rename_chat("old@lid", "new@s.whatsapp.net")

        msgs = await in_memory_db.get_messages("new@s.whatsapp.net")
        assert [m["key"]["id"] for m in msgs] == ["m1"]

    async def test_merge_deletes_old_chat_row_when_new_exists(self, in_memory_db):
        await in_memory_db.upsert_chat("old@lid", {"remoteJid": "old@lid"})
        await in_memory_db.upsert_chat("new@w", {"remoteJid": "new@w", "pushName": "Kept"})
        await in_memory_db.merge_or_rename_chat("old@lid", "new@w")
        chats = await in_memory_db.get_chats()
        assert "old@lid" not in chats
        assert chats["new@w"]["pushName"] == "Kept"


# =============================================================================
#  Contacts
# =============================================================================


class TestContacts:
    async def test_upsert_contact_creates(self, in_memory_db):
        await in_memory_db.upsert_contact("jid@w", {
            "id": "jid@w", "remoteJid": "jid@w", "name": "John",
        })
        contacts = await in_memory_db.get_contacts()
        assert contacts["jid@w"]["name"] == "John"

    async def test_upsert_contact_updates(self, in_memory_db):
        await in_memory_db.upsert_contact("jid@w", {
            "id": "jid@w", "remoteJid": "jid@w", "name": "John",
        })
        await in_memory_db.upsert_contact("jid@w", {
            "id": "jid@w", "remoteJid": "jid@w", "name": "Johnny",
        })
        contacts = await in_memory_db.get_contacts()
        assert contacts["jid@w"]["name"] == "Johnny"

    async def test_get_contacts_empty(self, in_memory_db):
        contacts = await in_memory_db.get_contacts()
        assert contacts == {}

    async def test_get_contacts_multiple(self, in_memory_db):
        await in_memory_db.upsert_contact("a@w", {"id": "a@w", "remoteJid": "a@w"})
        await in_memory_db.upsert_contact("b@w", {"id": "b@w", "remoteJid": "b@w"})
        contacts = await in_memory_db.get_contacts()
        assert set(contacts.keys()) == {"a@w", "b@w"}

    async def test_contact_isSaved_bool(self, in_memory_db):
        await in_memory_db.upsert_contact("jid@w", {
            "id": "jid@w", "remoteJid": "jid@w", "isSaved": True,
        })
        contacts = await in_memory_db.get_contacts()
        assert contacts["jid@w"]["isSaved"] is True

        await in_memory_db.upsert_contact("jid@w", {
            "id": "jid@w", "remoteJid": "jid@w", "isSaved": False,
        })
        contacts = await in_memory_db.get_contacts()
        assert contacts["jid@w"]["isSaved"] is False

    async def test_upsert_contacts_batch(self, in_memory_db):
        contacts = {
            "a@w": {"id": "a@w", "remoteJid": "a@w", "name": "A"},
            "b@w": {"id": "b@w", "remoteJid": "b@w", "name": "B"},
        }
        await in_memory_db.upsert_contacts_batch(contacts)
        result = await in_memory_db.get_contacts()
        assert result["a@w"]["name"] == "A"
        assert result["b@w"]["name"] == "B"


# =============================================================================
#  LID Mappings
# =============================================================================


class TestLidMappings:
    async def test_get_lid_mappings_empty(self, in_memory_db):
        mappings = await in_memory_db.get_lid_mappings()
        assert mappings == {}

    async def test_set_and_get_lid_mapping(self, in_memory_db):
        await in_memory_db.set_lid_mapping("lid@lid", "phone@s.whatsapp.net")
        mappings = await in_memory_db.get_lid_mappings()
        assert mappings == {"lid@lid": "phone@s.whatsapp.net"}

    async def test_set_lid_mapping_updates(self, in_memory_db):
        await in_memory_db.set_lid_mapping("lid@lid", "old@w")
        await in_memory_db.set_lid_mapping("lid@lid", "new@w")
        mappings = await in_memory_db.get_lid_mappings()
        assert mappings["lid@lid"] == "new@w"

    async def test_multiple_lid_mappings(self, in_memory_db):
        await in_memory_db.set_lid_mapping("l1@lid", "p1@w")
        await in_memory_db.set_lid_mapping("l2@lid", "p2@w")
        mappings = await in_memory_db.get_lid_mappings()
        assert len(mappings) == 2
        assert mappings["l1@lid"] == "p1@w"

    async def test_unresolvable_lids_empty(self, in_memory_db):
        lids, names = await in_memory_db.get_unresolvable_lids()
        assert lids == set()
        assert names == set()

    async def test_add_unresolvable_lid(self, in_memory_db):
        await in_memory_db.add_unresolvable_lid("bad@lid")
        lids, names = await in_memory_db.get_unresolvable_lids()
        assert "bad@lid" in lids
        assert len(names) == 0

    async def test_add_unresolvable_name(self, in_memory_db):
        await in_memory_db.add_unresolvable_name("no_name@lid")
        lids, names = await in_memory_db.get_unresolvable_lids()
        assert "no_name@lid" in names
        assert len(lids) == 0

    async def test_add_unresolvable_lid_idempotent(self, in_memory_db):
        await in_memory_db.add_unresolvable_lid("bad@lid")
        await in_memory_db.add_unresolvable_lid("bad@lid")  # should not error
        lids, _ = await in_memory_db.get_unresolvable_lids()
        assert len(lids) == 1

    async def test_both_marks_survive_for_the_same_jid(self, in_memory_db):
        """Under the old jid-only primary key the second INSERT was silently
        dropped, so one of the two marks was gone after a restart."""
        await in_memory_db.add_unresolvable_lid("both@lid")
        await in_memory_db.add_unresolvable_name("both@lid")
        lids, names = await in_memory_db.get_unresolvable_lids()
        assert "both@lid" in lids
        assert "both@lid" in names

    async def test_delete_unresolvable_lid(self, in_memory_db):
        await in_memory_db.add_unresolvable_lid("bad@lid")
        await in_memory_db.delete_unresolvable_lid("bad@lid")
        lids, _ = await in_memory_db.get_unresolvable_lids()
        assert lids == set()

    async def test_delete_unresolvable_lid_keeps_the_name_mark(self, in_memory_db):
        """Learning the phone number says nothing about the name."""
        await in_memory_db.add_unresolvable_lid("both@lid")
        await in_memory_db.add_unresolvable_name("both@lid")
        await in_memory_db.delete_unresolvable_lid("both@lid")
        lids, names = await in_memory_db.get_unresolvable_lids()
        assert lids == set()
        assert names == {"both@lid"}

    async def test_delete_unresolvable_name(self, in_memory_db):
        await in_memory_db.add_unresolvable_name("no_name@lid")
        await in_memory_db.delete_unresolvable_name("no_name@lid")
        _, names = await in_memory_db.get_unresolvable_lids()
        assert names == set()

    async def test_delete_expired_unresolvable_keeps_fresh_entries(self, in_memory_db):
        await in_memory_db.add_unresolvable_lid("bad@lid")
        deleted = await in_memory_db.delete_expired_unresolvable(int(time.time()) - 3600)
        assert deleted == 0
        lids, _ = await in_memory_db.get_unresolvable_lids()
        assert lids == {"bad@lid"}

    async def test_delete_expired_unresolvable_drops_old_entries(self, in_memory_db, frozen_clock):
        """The whole point of the change: a blacklist entry gets a way out."""
        frozen_clock.now = 1_700_000_000
        await in_memory_db.add_unresolvable_lid("bad@lid")
        await in_memory_db.add_unresolvable_name("bad@lid")
        deleted = await in_memory_db.delete_expired_unresolvable(
            frozen_clock.now + 7 * 24 * 3600)
        assert deleted == 2
        lids, names = await in_memory_db.get_unresolvable_lids()
        assert lids == set()
        assert names == set()

    async def test_re_recording_restarts_the_retry_clock(self, in_memory_db, frozen_clock):
        """A LID retried and found unresolvable again must not stay
        stale-dated, or it would be retried on every launch from then on."""
        frozen_clock.now = 1_700_000_000
        await in_memory_db.add_unresolvable_lid("bad@lid")
        frozen_clock.now += 8 * 24 * 3600
        await in_memory_db.add_unresolvable_lid("bad@lid")
        deleted = await in_memory_db.delete_expired_unresolvable(
            frozen_clock.now - 7 * 24 * 3600)
        assert deleted == 0

    async def test_imported_entries_expire_on_the_first_sweep(self, in_memory_db):
        """import_from_dict carries no timestamps (neither did the pre-expiry
        schema), and an entry of unknown age is better retried than trusted."""
        await in_memory_db.import_from_dict({"unresolvable_lids": ["old@lid"]})
        deleted = await in_memory_db.delete_expired_unresolvable(1)
        assert deleted == 1


# =============================================================================
#  Status Updates
# =============================================================================


class TestStatusUpdates:
    async def test_get_status_updates_empty(self, in_memory_db):
        updates = await in_memory_db.get_status_updates()
        assert updates == {}

    async def test_upsert_and_get_status(self, in_memory_db):
        msg = {
            "key": {
                "remoteJid": "status@broadcast",
                "id": "s1",
                "participant": "me@w",
            },
            "message": {"conversation": "my status"},
            "messageTimestamp": 1000,
            "messageType": "conversation",
        }
        await in_memory_db.upsert_status_update("me@w", msg)
        updates = await in_memory_db.get_status_updates()
        assert "me@w" in updates
        assert len(updates["me@w"]) == 1
        assert updates["me@w"][0]["message"]["conversation"] == "my status"

    async def test_multiple_statuses_same_user(self, in_memory_db):
        for i in range(3):
            await in_memory_db.upsert_status_update(
                "me@w",
                {
                    "key": {
                        "remoteJid": "status@broadcast",
                        "id": f"s{i}",
                        "participant": "me@w",
                    },
                    "message": {"conversation": f"s{i}"},
                    "messageTimestamp": i,
                    "messageType": "conversation",
                },
            )
        updates = await in_memory_db.get_status_updates()
        assert len(updates["me@w"]) == 3

    async def test_multiple_users_statuses(self, in_memory_db):
        await in_memory_db.upsert_status_update("a@w", {
            "key": {"id": "a1", "participant": "a@w"},
            "messageTimestamp": 1,
            "messageType": "conversation",
        })
        await in_memory_db.upsert_status_update("b@w", {
            "key": {"id": "b1", "participant": "b@w"},
            "messageTimestamp": 1,
            "messageType": "conversation",
        })
        updates = await in_memory_db.get_status_updates()
        assert set(updates.keys()) == {"a@w", "b@w"}

    async def test_delete_expired_status_updates_removes_only_old(self, in_memory_db):
        """Stories never expired before — this is the fix for the database
        growing forever, one row per status ever seen, for months of use."""
        await in_memory_db.upsert_status_update("old@w", {
            "key": {"id": "old1", "participant": "old@w"},
            "messageTimestamp": 1000,
            "messageType": "conversation",
        })
        await in_memory_db.upsert_status_update("new@w", {
            "key": {"id": "new1", "participant": "new@w"},
            "messageTimestamp": 500_000,
            "messageType": "conversation",
        })
        deleted = await in_memory_db.delete_expired_status_updates(cutoff_ts=100_000)
        assert deleted == 1
        updates = await in_memory_db.get_status_updates()
        assert set(updates.keys()) == {"new@w"}

    async def test_delete_expired_status_updates_none_expired(self, in_memory_db):
        await in_memory_db.upsert_status_update("a@w", {
            "key": {"id": "a1", "participant": "a@w"},
            "messageTimestamp": 500_000,
            "messageType": "conversation",
        })
        deleted = await in_memory_db.delete_expired_status_updates(cutoff_ts=100)
        assert deleted == 0
        updates = await in_memory_db.get_status_updates()
        assert "a@w" in updates

    async def test_delete_status_update_removes_only_requested_message(self, in_memory_db):
        for message_id in ("keep", "remove"):
            await in_memory_db.upsert_status_update("me@w", {
                "key": {"id": message_id, "participant": "me@w"},
                "messageTimestamp": 500_000,
                "messageType": "conversation",
            })

        deleted = await in_memory_db.delete_status_update("remove")

        assert deleted == 1
        updates = await in_memory_db.get_status_updates()
        assert [m["key"]["id"] for m in updates["me@w"]] == ["keep"]
        assert await in_memory_db.delete_status_update("missing") == 0
        assert await in_memory_db.delete_status_update("") == 0


# =============================================================================
#  Bulk Import / Export (Migration)
# =============================================================================


class TestBulkImportExport:
    async def test_import_from_dict_basic(self, in_memory_db, sample_data):
        count = await in_memory_db.import_from_dict(sample_data)
        assert count > 0

        chats = await in_memory_db.get_chats()
        assert len(chats) == len(sample_data["chats"])

        contacts = await in_memory_db.get_contacts()
        assert len(contacts) == len(sample_data["contacts"])

    async def test_import_export_roundtrip(self, in_memory_db, sample_data):
        await in_memory_db.import_from_dict(sample_data)
        exported = await in_memory_db.export_as_dict()

        assert set(exported["chats"].keys()) == set(sample_data["chats"].keys())
        assert set(exported["contacts"].keys()) == set(sample_data["contacts"].keys())
        assert exported["lid_to_phone"] == sample_data["lid_to_phone"]
        assert set(exported["status_updates"].keys()) == set(
            sample_data["status_updates"].keys()
        )

    async def test_export_empty_db(self, in_memory_db):
        exported = await in_memory_db.export_as_dict()
        assert exported["chats"] == {}
        assert exported["contacts"] == {}
        assert exported["lid_to_phone"] == {}

    async def test_import_preserves_message_content(self, in_memory_db, sample_data):
        await in_memory_db.import_from_dict(sample_data)
        exported = await in_memory_db.export_as_dict()

        for chat_jid, chat in exported["chats"].items():
            records = chat.get("messages", {}).get("messages", {}).get("records", [])
            if records:
                msg = records[0]
                orig_jid = list(sample_data["chats"].keys())[0]
                orig_records = (
                    sample_data["chats"][orig_jid]
                    .get("messages", {})
                    .get("messages", {})
                    .get("records", [])
                )
                if orig_records:
                    assert msg["key"]["id"] == orig_records[0]["key"]["id"]
                    if "conversation" in msg.get("message", {}):
                        assert (
                            msg["message"]["conversation"]
                            == orig_records[0]["message"]["conversation"]
                        )
                break

    async def test_import_is_idempotent(self, in_memory_db, sample_data):
        c1 = await in_memory_db.import_from_dict(sample_data)
        c2 = await in_memory_db.import_from_dict(sample_data)

        chats = await in_memory_db.get_chats()
        assert len(chats) == len(sample_data["chats"])

        for jid in sample_data["chats"]:
            orig_count = len(
                sample_data["chats"][jid]
                .get("messages", {})
                .get("messages", {})
                .get("records", [])
            )
            count = await in_memory_db.get_message_count(jid)
            assert count == orig_count

    async def test_clear_first_wipes_metadata_by_default(self, in_memory_db, sample_data):
        """clear_metadata defaults to True — an account switch/logout must
        not leak cleared/deleted/archived/muted/blocked state into the next
        account paired."""
        await in_memory_db.set_metadata_json("deleted_chats", ["a@s.whatsapp.net"])
        await in_memory_db.import_from_dict(sample_data, clear_first=True)

        assert await in_memory_db.get_metadata_json("deleted_chats", None) is None

    async def test_clear_first_with_clear_metadata_false_preserves_metadata(
        self, in_memory_db, sample_data
    ):
        """Reported live: F5 (resync) reused the same clear_first=True wipe
        as logout and silently discarded every cleared/deleted/archived/
        muted/blocked-contact action the user had taken — clear_metadata=
        False is what MainWindow.clear_local_data(wipe_metadata=False) (the
        resync path) now passes through to keep that table intact while
        still wiping chats/messages/contacts for the actual resync."""
        await in_memory_db.set_metadata_json("deleted_chats", ["a@s.whatsapp.net"])
        await in_memory_db.set_metadata_json("archived_chats", ["b@s.whatsapp.net"])
        await in_memory_db.set_metadata_json("blocked_contacts", ["5511999999999"])

        await in_memory_db.import_from_dict(
            sample_data, clear_first=True, clear_metadata=False
        )

        assert await in_memory_db.get_metadata_json("deleted_chats", None) == ["a@s.whatsapp.net"]
        assert await in_memory_db.get_metadata_json("archived_chats", None) == ["b@s.whatsapp.net"]
        assert await in_memory_db.get_metadata_json("blocked_contacts", None) == ["5511999999999"]
        # The actual point of the resync still happened — chats/contacts
        # really were replaced with the freshly imported set.
        chats = await in_memory_db.get_chats()
        assert len(chats) == len(sample_data["chats"])

    async def test_clear_metadata_false_still_wipes_chats_and_messages(
        self, in_memory_db, sample_data
    ):
        """clear_metadata only carves out system_metadata — everything else
        clear_first normally wipes must still be wiped."""
        await in_memory_db.import_from_dict(sample_data)  # seed with data first
        other_data = {"chats": {}, "contacts": {}}

        await in_memory_db.import_from_dict(
            other_data, clear_first=True, clear_metadata=False
        )

        chats = await in_memory_db.get_chats()
        assert chats == {}
        contacts = await in_memory_db.get_contacts()
        assert contacts == {}


# =============================================================================
#  Structured Concurrency (replaces old thread-safety tests)
# =============================================================================


class TestStructuredConcurrency:
    async def test_concurrent_writes_task_group(self, tmp_path, fernet_key):
        """Concurrent writes via anyio TaskGroup — all must succeed."""
        from core.database import DatabaseManager

        db_path = str(tmp_path / "test.db")
        async with DatabaseManager(db_path, fernet_key) as db:
            async with anyio.create_task_group() as tg:
                for letter in ("a", "b", "c"):
                    tg.start_soon(self._write_chats, db, letter, 10)

            chats = await db.get_chats()
            assert "a@a" in chats
            assert "b@b" in chats
            assert "c@c" in chats

    async def _write_chats(
        self, db, name: str, count: int
    ) -> None:
        """Helper: write *count* chats with prefix *name*."""
        for i in range(count):
            await db.upsert_chat(
                f"{name}@{name}",
                {"remoteJid": f"{name}@{name}", "pushName": f"{name}{i}"},
            )

    async def test_concurrent_read_and_write(self, tmp_path, fernet_key):
        """Concurrent readers and writers via TaskGroup — no errors."""
        from core.database import DatabaseManager

        db_path = str(tmp_path / "test.db")
        errors: list[Exception] = []

        async def reader():
            try:
                for _ in range(30):
                    await db.get_chats()
            except Exception as e:
                errors.append(e)

        async def writer():
            try:
                for i in range(30):
                    await db.upsert_chat(
                        f"jid{i}@w", {"remoteJid": f"jid{i}@w"}
                    )
            except Exception as e:
                errors.append(e)

        async with DatabaseManager(db_path, fernet_key) as db:
            async with anyio.create_task_group() as tg:
                tg.start_soon(reader)
                tg.start_soon(writer)

        assert not errors, f"Errors during concurrent access: {errors}"


# =============================================================================
#  Maintenance (vacuum, schema migration safety)
# =============================================================================


class TestMaintenance:
    async def test_vacuum_does_not_raise(self, in_memory_db):
        """vacuum() used to have no _write_lock, which raises 'cannot VACUUM
        from within a transaction' if it ever ran concurrently with a write.
        Plain smoke test that it runs cleanly on its own."""
        await in_memory_db.upsert_chat("jid@w", {"remoteJid": "jid@w"})
        await in_memory_db.vacuum()
        chats = await in_memory_db.get_chats()
        assert "jid@w" in chats

    async def test_connect_twice_on_same_file_does_not_raise(self, tmp_path, fernet_key):
        """connect() runs 'ALTER TABLE chats ADD COLUMN t' every time, which
        only succeeds on a database that doesn't have the column yet. Every
        connection after the first must see 'duplicate column' and swallow
        only that — not raise, and not swallow some other real error."""
        from core.database import DatabaseManager

        db_path = str(tmp_path / "test.db")
        async with DatabaseManager(db_path, fernet_key) as db1:
            await db1.upsert_chat("jid@w", {"remoteJid": "jid@w"})

        async with DatabaseManager(db_path, fernet_key) as db2:
            chats = await db2.get_chats()
            assert "jid@w" in chats

    async def test_connect_upgrades_a_pre_expiry_unresolvable_table(
        self, tmp_path, fernet_key
    ):
        """The old table had jid as its sole primary key and no recorded_at,
        neither of which ALTER TABLE can add. Opening such a database must not
        raise, and must leave a table that holds both marks for one JID."""
        import aiosqlite

        from core.database import DatabaseManager

        db_path = str(tmp_path / "old.db")
        async with aiosqlite.connect(db_path) as raw:
            await raw.execute(
                "CREATE TABLE unresolvable_lids ("
                "jid TEXT PRIMARY KEY, type TEXT DEFAULT 'lid')"
            )
            await raw.execute(
                "INSERT INTO unresolvable_lids (jid, type) VALUES ('old@lid', 'lid')"
            )
            await raw.commit()

        async with DatabaseManager(db_path, fernet_key) as db:
            # Old rows are dropped, not copied: they carry no timestamp, so
            # they would count as expired on the first sweep anyway. That one
            # retry is exactly what unsticks the contacts blacklisted forever.
            lids, _names = await db.get_unresolvable_lids()
            assert lids == set()

            await db.add_unresolvable_lid("both@lid")
            await db.add_unresolvable_name("both@lid")
            lids, names = await db.get_unresolvable_lids()
            assert lids == {"both@lid"}
            assert names == {"both@lid"}

    async def test_the_upgrade_runs_only_once(self, tmp_path, fernet_key):
        """Re-running the rebuild on every launch would silently mean "retry
        every blacklisted LID, always" — the loop the table exists to stop."""
        from core.database import DatabaseManager

        db_path = str(tmp_path / "keep.db")
        async with DatabaseManager(db_path, fernet_key) as db1:
            await db1.add_unresolvable_lid("bad@lid")

        async with DatabaseManager(db_path, fernet_key) as db2:
            lids, _ = await db2.get_unresolvable_lids()
            assert lids == {"bad@lid"}


class TestContactsSyncedToPhone:
    """contacts.synced_to_phone: a contact that lives in the phone's address
    book keeps that across a restart. Without the column the mark was lost,
    and until the next contact sync such a contact was handled as a local one:
    deleting it removed it from WinZapp only."""

    async def test_the_mark_survives_a_reopen(self, tmp_path, fernet_key):
        from core.database import DatabaseManager
        from core.phone_contacts import is_phone_synced, synced_entry

        db_path = str(tmp_path / "contacts.db")
        async with DatabaseManager(db_path, fernet_key) as db:
            await db.upsert_contacts_batch({"a@w": synced_entry("a@w", "Ana")})

        async with DatabaseManager(db_path, fernet_key) as db:
            contact = (await db.get_contacts())["a@w"]
            assert is_phone_synced(contact) is True
            assert contact["isSaved"] is True and contact["name"] == "Ana"

    async def test_a_contact_whatsapp_reports_as_synced_is_kept_as_one(self, in_memory_db):
        """Added on the phone: WinZapp wrote no marker, WhatsApp's flags say it."""
        from core.phone_contacts import is_phone_synced

        await in_memory_db.upsert_contact("a@w", {
            "remoteJid": "a@w", "isMyContact": True, "syncToAddressbook": True})
        assert is_phone_synced((await in_memory_db.get_contacts())["a@w"]) is True

    async def test_a_local_contact_is_not_marked(self, in_memory_db):
        from core.phone_contacts import is_phone_synced, local_entry

        await in_memory_db.upsert_contacts_batch({
            "a@w": local_entry("a@w", "Ana"),
            "b@w": {"remoteJid": "b@w", "isMyContact": True, "syncToAddressbook": False},
        })
        contacts = await in_memory_db.get_contacts()
        assert is_phone_synced(contacts["a@w"]) is False
        assert is_phone_synced(contacts["b@w"]) is False

    async def test_clearing_the_mark_is_persisted(self, in_memory_db):
        from core.phone_contacts import SYNCED_KEY, synced_entry

        entry = synced_entry("a@w", "Ana")
        await in_memory_db.upsert_contact("a@w", entry)
        entry.update({SYNCED_KEY: False, "isMyContact": False, "syncToAddressbook": False})
        await in_memory_db.upsert_contact("a@w", entry)
        assert (await in_memory_db.get_contacts())["a@w"][SYNCED_KEY] is False

    async def test_the_bulk_import_keeps_it_too(self, in_memory_db):
        from core.phone_contacts import is_phone_synced, synced_entry

        await in_memory_db.import_from_dict({"contacts": {"a@w": synced_entry("a@w", "Ana")}})
        assert is_phone_synced((await in_memory_db.get_contacts())["a@w"]) is True

    async def test_a_database_from_before_the_column_is_upgraded(self, tmp_path, fernet_key):
        """The table as every existing install has it. Opening it must add the
        column, keep the rows, and start them unmarked."""
        import aiosqlite

        from core.database import DatabaseManager
        from core.phone_contacts import SYNCED_KEY, synced_entry

        db_path = str(tmp_path / "old.db")
        async with aiosqlite.connect(db_path) as raw:
            await raw.execute(
                "CREATE TABLE contacts ("
                "jid TEXT PRIMARY KEY, remote_jid TEXT NOT NULL, name TEXT DEFAULT '', "
                "push_name TEXT DEFAULT '', profile_pic_url TEXT DEFAULT '', "
                "is_saved INTEGER DEFAULT 0, updated_at TEXT DEFAULT (datetime('now')))"
            )
            await raw.execute(
                "INSERT INTO contacts (jid, remote_jid, name, is_saved) "
                "VALUES ('old@w', 'old@w', 'Antigo', 1)"
            )
            await raw.commit()

        async with DatabaseManager(db_path, fernet_key) as db:
            contacts = await db.get_contacts()
            assert contacts["old@w"]["name"] == "Antigo"
            assert contacts["old@w"]["isSaved"] is True
            assert contacts["old@w"][SYNCED_KEY] is False
            await db.upsert_contact("new@w", synced_entry("new@w", "Novo"))

        # A second open finds the column and must not try to add it again.
        async with DatabaseManager(db_path, fernet_key) as db:
            contacts = await db.get_contacts()
            assert contacts["new@w"][SYNCED_KEY] is True
            assert set(contacts) == {"old@w", "new@w"}
