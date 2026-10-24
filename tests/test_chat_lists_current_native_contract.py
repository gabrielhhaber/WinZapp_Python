"""Current consumer list modules work when WA-JS 4.6.1's legacy gate is gone.

Synthetic stores only: no page, account, wx window or network is opened.
"""

import json

import pytest

from tests.test_whatsapp_chat_lists_api import GROUP, LOCKED, PN, ROOT, run


def modern(commands, **options):
    return run(commands, modern=True, capabilityMode="missing", **options)


def test_current_consumer_contract_creates_first_list_with_native_id_and_null_color():
    result = modern([{"action": "read"}, {"action": "create", "name": " Synthetic "},
                     {"action": "read"}], lists=[], chats=[])
    assert result["results"][0]["value"] == {"canEdit": True, "editingReason": "", "lists": []}
    assert result["results"][1]["value"] == {"action": "create", "createdId": "100"}
    assert result["results"][2]["value"]["lists"] == [{"id": "100", "name": "Synthetic", "members": []}]
    assert result["calls"] == [["nativeCreate", "Synthetic", None]]


@pytest.mark.parametrize("version", ["4.7.1", "4.7.2"])
def test_wa_js_4_7_x_uses_the_same_native_contract_as_4_6_1(version):
    """4.7.x tolerates a missing legacy gate for its own calls, but WinZapp's
    writes go through the verified native actions on both versions."""
    result = modern([{"action": "read"}, {"action": "create", "name": "Synthetic"}],
                    lists=[], chats=[], version=version)
    assert result["results"][0]["value"]["canEdit"] is True
    assert result["results"][1]["value"] == {"action": "create", "createdId": "100"}
    assert result["calls"] == [["nativeCreate", "Synthetic", None]]


@pytest.mark.parametrize("version", ["4.7.0", "4.7.3", "4.8.0"])
def test_other_wa_js_versions_stay_unverified(version):
    result = modern([{"action": "read"}], lists=[], chats=[], version=version)
    assert result["results"][0]["value"]["editingReason"] == "runtime_incomplete"


def test_current_rename_and_delete_preserve_native_metadata_and_object_delete_contract():
    result = modern([{"action": "rename", "id": "42", "name": " Changed "},
                     {"action": "remove", "id": "42"}],
                    lists=[{"id": "42", "name": "Before", "predefinedId": 7,
                            "colorIndex": 3, "isActive": True, "type": 5}])
    assert result["calls"] == [["nativeRename", "42", "Changed", 7, 3, True, 5],
                               ["nativeRemove", {"labelId": "42", "name": "Changed", "color": 3}]]
    assert all("error" not in item for item in result["results"])


def test_current_member_changes_preserve_unselected_members_and_use_one_delta():
    result = modern([{"action": "removeChats", "id": "42", "chatIds": [PN]},
                     {"action": "addChats", "id": "42", "chatIds": [GROUP, GROUP]},
                     {"action": "read"}])
    assert result["calls"] == [["removeChats", "42", [PN]], ["addChats", "42", [GROUP]]]
    assert result["results"][-1]["value"]["lists"][0]["members"] == [LOCKED, GROUP]


@pytest.mark.parametrize("options,reason", [
    ({"business": True}, "runtime_incomplete"),
    ({"filters": False}, "account_disabled"),
    ({"filters": "true"}, "capability_check_failed"),
    ({"moduleThrows": True}, "capability_check_failed"),
    ({"ready": False}, "capability_check_failed"),
    ({"version": "4.6.2"}, "runtime_incomplete"),
    ({"loaderType": "webpack"}, "runtime_incomplete"),
    ({"wrongNativeArity": True}, "runtime_incomplete"),
    ({"legacyNativeGate": True}, "runtime_incomplete"),
    *[({"missingModule": module}, "runtime_incomplete") for module in (
        "WAWebBizLabelEditingAction", "WAWebListsActions", "WAWebMobilePlatforms",
        "WAWebInboxFiltersGatingUtils", "WAWebListsLabelGatingUtils")],
    *[({"missingNativeAction": name}, "runtime_incomplete") for name in (
        "labelAddAction", "labelEditAction", "labelDeleteAction")],
])
def test_unknown_or_incomplete_current_contract_stays_readable_without_writes(options, reason):
    result = modern([{"action": "read"}, {"action": "create", "name": "Synthetic"}], **options)
    assert result["results"][0]["value"]["editingReason"] == reason
    assert result["results"][0]["value"]["canEdit"] is False
    assert result["results"][1] == {"error": "list_editing_not_available"}
    assert result["calls"] == []


@pytest.mark.parametrize("mode,reason", [(None, "account_disabled"),
                                         ("throws", "capability_check_failed"),
                                         ("invalid", "capability_check_failed")])
def test_legacy_refusal_or_error_never_switches_to_current_native_actions(mode, reason):
    result = run([{"action": "read"}, {"action": "create", "name": "Synthetic"}],
                 modern=True, editable=False, capabilityMode=mode)
    assert result["results"][0]["value"]["editingReason"] == reason
    assert result["calls"] == []


def test_present_nonfunction_legacy_gate_is_not_assumed_to_be_removed():
    result = run([{"action": "read"}, {"action": "create", "name": "Synthetic"}],
                 modern=True, capabilityMode="not_function")
    assert result["results"][0]["value"]["editingReason"] == "runtime_incomplete"
    assert result["calls"] == []


@pytest.mark.parametrize("command,call", [
    ({"action": "create", "name": "Synthetic"}, "nativeCreate"),
    ({"action": "rename", "id": "42", "name": "Synthetic"}, "nativeRename"),
    ({"action": "remove", "id": "42"}, "nativeRemove"),
])
def test_failed_native_write_has_one_attempt_without_legacy_retry(command, call):
    result = modern([command], nativeThrows=True)
    assert len(result["calls"]) == 1
    assert result["calls"][0][0] == call
    assert "error" in result["results"][0]


def test_native_empty_id_is_unconfirmed_without_retry_or_invented_id():
    result = modern([{"action": "create", "name": "Synthetic"}], nativeEmptyId=True)
    assert result["results"] == [{"error": "list_operation_unconfirmed"}]
    assert result["calls"] == [["nativeCreate", "Synthetic", None]]


def test_native_path_accepts_the_pinned_wa_js_version():
    package = json.loads((ROOT / "client/api_patches/package.json").read_text(encoding="utf-8"))
    pin = package["dependencies"]["@wppconnect/wa-js"]
    assert modern([{"action": "read"}], version=pin)["results"][0]["value"]["canEdit"] is True


@pytest.mark.parametrize("command,expected", [
    ({"action": "remove", "id": "1"}, "list_not_found"),
    ({"action": "create", "name": " "}, "list_name_required"),
    ({"action": "addChats", "id": "42", "chatIds": ["551199990099@c.us"]}, "list_chat_not_found"),
])
def test_current_contract_keeps_custom_list_and_known_chat_guards(command, expected):
    result = modern([command])
    assert result["results"] == [{"error": expected}]
    assert result["calls"] == []
