"""Regression contracts for the Node-side ChatStore recovery.

Captured on a 937-chat account: WhatsApp Web's IndexedDB remained complete
while the in-memory ChatStore disappeared. list-chats consequently returned an
empty array 99 times and getAllContacts crashed while mapping an undefined chat
list. These checks keep the recovery in the tracked API patch, where end-user
installs actually receive it.
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / "client" / "api_patches" / "src" / "controller" / "deviceController.ts"


def _source() -> str:
    return PATCH.read_text(encoding="utf-8")


def test_list_chats_recovers_ids_from_indexeddb_before_retrying():
    source = _source()
    assert "listChatsWithStoreRecovery" in source
    assert "indexedDB.open('model-storage')" in source
    assert "db.transaction('chat', 'readonly')" in source
    assert "WPP?.chat?.find" in source
    assert "const second = await serialise()" in source


def test_store_recovery_is_bounded_chunked_and_single_flight():
    source = _source()
    assert "__winzappChatStoreRecoveryPromise" in source
    assert "found.size >= 5000" in source
    assert "const batchSize = 24" in source
    assert "Promise.allSettled" in source


def test_list_chats_route_uses_the_recovering_reader():
    source = _source()
    route_start = source.index("export async function listChats")
    route_end = source.index("export async function getAllChatsWithMessages", route_start)
    route = source[route_start:route_end]
    # listChatsWithDiag is the recovering reader itself; listChatsWithStoreRecovery
    # is its array-only wrapper for the callers that do not log diagnostics.
    assert "await listChatsWithDiag(req, options)" in route


def test_contacts_never_maps_a_non_array_chat_result():
    source = _source()
    contacts_start = source.index("export async function getAllContacts")
    contacts = source[contacts_start:]
    assert contacts.count("await listChatsWithStoreRecovery(req,") >= 2
    assert ".listChats({ ignoreGroupMetadata: true } as any)" not in contacts
