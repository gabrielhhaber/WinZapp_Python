"""Run the actual page adapter in Node on synthetic stores, no browser/API."""

import json
from pathlib import Path
import shutil
import subprocess
import pytest

ROOT = Path(__file__).resolve().parents[1]
PN = "551199990001@c.us"
LOCKED = "551199990002@c.us"
GROUP = "120363000000001@g.us"


def run(commands, **options):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable; no dependency is installed by tests")
    payload = {"lists": [{"id": "42", "name": "Friends"}, {"id": "1", "name": "Business", "type": 1}],
               "chats": [{"id": PN, "labels": ["42"]}, {"id": LOCKED, "labels": ["42"]},
                         {"id": GROUP, "labels": ["1"]}], "commands": commands, **options}
    result = subprocess.run(
        [node, str(ROOT / "tests/chat_lists_runtime.cjs"),
         str(ROOT / "client/api_patches/src/util/chatListsRuntime.ts")],
        input=json.dumps(payload), capture_output=True, text=True, encoding="utf-8", timeout=10,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_read_contains_only_custom_lists_and_exact_member_ids_without_writes():
    result = run([{"action": "read"}])
    assert result["calls"] == []
    assert result["results"][0]["value"] == {"canEdit": True,
        "lists": [{"id": "42", "name": "Friends", "members": [PN, LOCKED]}]}


def test_read_handles_overlapping_labels_without_duplicate_members_or_nonchat_addresses():
    result = run([{"action": "read"}],
        lists=[{"id": "42", "name": "Friends"}, {"id": "43", "name": "Work"}],
        chats=[{"id": PN, "labels": ["42", "42", "43"]},
               {"id": GROUP, "labels": ["43"]}, {"id": "status@broadcast", "labels": ["42"]}])
    assert result["results"][0]["value"]["lists"] == [
        {"id": "42", "name": "Friends", "members": [PN]},
        {"id": "43", "name": "Work", "members": [PN, GROUP]}]
    assert not result["calls"]


def test_member_delta_preserves_unselected_members_and_unrelated_labels():
    result = run([{"action": "removeChats", "id": "42", "chatIds": [PN]},
                  {"action": "addChats", "id": "42", "chatIds": [GROUP, GROUP]}, {"action": "read"}])
    assert result["calls"] == [["removeChats", "42", [PN]], ["addChats", "42", [GROUP]]]
    assert result["results"][-1]["value"]["lists"][0]["members"] == [LOCKED, GROUP]


def test_create_rename_delete_use_native_list_functions_and_read_resulting_state():
    result = run([{"action": "create", "name": " Work "},
                  {"action": "rename", "id": "100", "name": "Colleagues"},
                  {"action": "read"}, {"action": "remove", "id": "100"}, {"action": "read"}])
    assert result["results"][0]["value"] == {"action": "create", "createdId": "100"}
    assert result["results"][2]["value"]["lists"][-1]["name"] == "Colleagues"
    assert [item["id"] for item in result["results"][-1]["value"]["lists"]] == ["42"]
    assert result["calls"] == [["create", "Work"], ["rename", "100", "Colleagues"], ["remove", "100"]]


@pytest.mark.parametrize("command,expected", [
    ({"action": "eraseEverything"}, "list_command_invalid"),
    ({"action": "rename", "id": "../42", "name": "X"}, "list_command_invalid"),
    ({"action": "remove", "id": "1"}, "list_not_found"),
    ({"action": "remove", "id": "missing"}, "list_not_found"),
    ({"action": "create", "name": " "}, "list_name_required"),
    ({"action": "create", "name": "a" * 101}, "list_name_required"),
    ({"action": "addChats", "id": "42", "chatIds": []}, "list_command_invalid"),
    ({"action": "addChats", "id": "42", "chatIds": ["status@broadcast"]}, "list_command_invalid"),
    ({"action": "addChats", "id": "42", "chatIds": ["551199990099@c.us"]}, "list_chat_not_found"),
])
def test_invalid_or_noncustom_target_never_reaches_mutator(command, expected):
    result = run([command])
    assert result["results"] == [{"error": expected}]
    assert result["calls"] == []


def test_account_without_editing_remains_readable_but_cannot_write():
    result = run([{"action": "read"}, {"action": "rename", "id": "42", "name": "X"}], editable=False)
    assert result["results"][0]["value"]["canEdit"] is False
    assert result["results"][1] == {"error": "list_editing_not_available"}
    assert not result["calls"]


def test_missing_mutator_disables_editing_and_never_tries_an_alternative():
    result = run([{"action": "read"}, {"action": "create", "name": "X"}], missingMethod="create")
    assert result["results"][0]["value"]["canEdit"] is False
    assert result["results"][1] == {"error": "list_editing_not_available"}
    assert result["calls"] == []


def test_runtime_without_lists_is_reported_as_unsupported():
    assert run([{"action": "read"}], missing="lists")["results"] == [{"error": "lists_not_available"}]
