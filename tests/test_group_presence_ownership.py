"""Published provider binding plus real server/bridge code; fake pages/models only."""
import json
import hashlib
import ast
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMPILER = Path(os.environ.get("WINZAPP_TEST_TYPESCRIPT", ROOT / "client/api/node_modules/typescript/lib/typescript.js"))


def run(case, version="2.3.5"):
    node = shutil.which("node")
    if not node or not COMPILER.is_file():
        pytest.skip("Existing Node/TypeScript required; no install")
    providers = os.environ.get("WINZAPP_TEST_WPPCONNECT_ROOT")
    data = {"case": case, "compiler": str(COMPILER), "version": version}
    if providers:
        provider = Path(providers) / f"wppconnect-{version}/package"
        assert provider.is_dir(), "Explicit published provider artifact unavailable"
        data["provider"] = str(provider)
    result = subprocess.run([node, str(Path(__file__).with_name("group_presence_ownership_runtime.cjs")),
        json.dumps(data)],
        capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("version", ["2.3.5", "2.3.6"])
def test_reused_provider_binding_delivers_only_to_current_local_owner(version):
    result = run("owner", version)
    assert result["bindingSame"] is True
    assert [item["owner"] for item in result["io"]] == ["second"]
    assert not [item for item in result["hooks"] if item["owner"] == "first"]


def test_replaced_model_cannot_emit_a_group_activity_update():
    assert run("old_model")["io"] == []


@pytest.mark.parametrize("case", ["remove", "queued_remove", "reset", "close"])
def test_removed_reset_closed_or_queued_old_model_cannot_deliver(case):
    result = run(case)
    assert result["io"] == [] and result["hooks"] == []


@pytest.mark.parametrize("case,owner", [("reload", "first"), ("late_install", "second"),
                                       ("old_generation", "second"), ("replacement_store", "first")])
def test_current_generation_survives_reload_and_late_old_install(case, owner):
    result = run(case)
    assert [item["owner"] for item in result["io"]] == [owner]
    assert result["groupExposes"] == 1 and result["loadHooks"] == result["closeHooks"] == 2
    assert result["timers"] == 0
    if case == "replacement_store":
        assert result["oldStoreListeners"] == 0


@pytest.mark.parametrize("case,state", [("recording", "recording_audio"), ("duplicate", "typing")])
def test_group_state_and_duplicate_field_burst_are_one_local_delivery(case, state):
    result = run(case)
    assert len(result["io"]) == len(result["hooks"]) == 1
    event = result["io"][0]["event"]
    assert event["state"] == state and event["participants"][0]["state"] == state
    legacy = result["hooks"][0]["event"]
    assert set(legacy) == {"id", "isOnline", "isGroup", "isUser", "shortName", "state", "t", "participants"}
    assert "lastSeen" not in event and "lastSeen" not in legacy and "generation" not in legacy


def test_stopped_group_activity_sends_paused_once():
    result = run("paused")
    assert [item["event"]["state"] for item in result["io"]] == ["typing", "paused"]
    assert [item["event"]["state"] for item in result["hooks"]] == ["typing", "paused"]


def test_local_group_delivery_does_not_wait_for_provider_exposed_function():
    result = run("no_legacy")
    assert len(result["io"]) == 1 and result["hooks"] == []


def test_private_contact_does_not_enter_group_bridge():
    assert run("private_scope")["io"] == []


@pytest.mark.parametrize("case", ["retry", "replacement_retry", "close_retry"])
def test_readiness_retries_are_bounded_and_stale_chains_are_cancelled(case):
    result = run(case)
    assert result["timers"] == 0 and result["io"] == []
    assert result["evaluates"] < 180
    assert result["loadHooks"] == result["closeHooks"] == 2
    assert len(result["logs"]) <= 2


@pytest.mark.parametrize("case", ["unsupported", "partial_attach"])
def test_unsupported_or_partial_group_attach_preserves_legacy_fallback(case):
    result = run(case)
    assert len(result["io"]) == len(result["hooks"]) == 1
    if case == "partial_attach":
        assert result["listeners"] == 9  # Only the existing private bridge.


def test_group_socket_payload_is_scoped_and_duplicate_legacy_native_delivery_speaks_once(monkeypatch):
    from types import SimpleNamespace
    from core.contact_presence import cached
    from core.websocket_client import WebSocketClient
    import core.websocket_client as wire
    import main_window.chat_events as events
    from tests.test_presence_speech_gating import _Stub
    event = run("duplicate")["io"][0]["event"]
    owner = _Stub(conversation_jid=event["id"])
    monkeypatch.setattr(wire, "wx", SimpleNamespace(CallAfter=lambda fn: fn()))
    monkeypatch.setattr(events, "wx", SimpleNamespace(CallLater=lambda *a: SimpleNamespace(Stop=lambda: None)))
    peer = SimpleNamespace(main_window=owner, _belongs_to_this_session=lambda data: True)
    for _ in range(2):  # Also tolerate a legacy callback already queued before handover.
        WebSocketClient.on_wpp_presence_changed(peer, event)
    assert cached(owner, "123@s.whatsapp.net") == {}
    assert cached(owner, "123@s.whatsapp.net", chat=event["id"])["lastKnownPresence"] == "composing"
    assert len(owner.speak_output.outputs) == 1


def test_failed_native_delivery_retains_the_existing_legacy_fallback():
    result = run("local_failure")
    assert len(result["io"]) == len(result["hooks"]) == 1
    assert len(result["logs"]) == 1 and "PRIVATE_DELIVERY" not in result["logs"][0]


@pytest.mark.parametrize("variant", ["private", "state", "mismatch", "time", "participant", "name", "empty"])
def test_malformed_native_group_envelope_never_reaches_socket_or_webhook(variant):
    result = run("envelope_" + variant)
    assert result["io"] == result["hooks"] == []


def test_native_group_binding_copies_known_shape_only_and_does_not_call_webhook():
    result = run("envelope_extra")
    assert len(result["io"]) == 1 and result["hooks"] == []
    assert "PRIVATE_SECRET" not in json.dumps(result["io"])
    assert "lastSeen" not in result["io"][0]["event"]


def test_same_client_with_replacement_request_drops_old_webhook_callback():
    result = run("same_client_request")
    assert [item["owner"] for item in result["io"]] == ["replacement_req"]
    assert [item["request"] for item in result["hooks"]] == ["replacement_req"]


def test_store_reset_releases_native_legacy_suppression_without_new_data():
    result = run("reset_fallback")
    assert [item["event"]["state"] for item in result["io"]] == ["typing", "paused"]
    assert len(result["hooks"]) == 2


def test_newly_added_group_model_is_attached_without_an_initial_cache_dump():
    event = run("late_add")["io"][0]["event"]
    assert event["id"] == "456-789@g.us" and event["participants"][0]["id"] == "456@lid"


def test_portable_provider_fixtures_preserve_original_fragments_and_license():
    fixture = json.loads((ROOT / "tests/fixtures/group_presence_provider.json").read_text(encoding="utf-8"))
    assert "GNU LESSER GENERAL PUBLIC LICENSE" in (
        ROOT / "tests/fixtures/group_presence_provider.LICENSE.md").read_text(encoding="utf-8")
    assert set(fixture["versions"]) == {"2.3.5", "2.3.6"}
    for data in fixture["versions"].values():
        assert "Lesser General Public License" in data["license_header"]
        for name, text in data["fragments"].items():
            assert hashlib.sha256(text.encode()).hexdigest() == data["fragment_sha256"][name]


def test_mutated_client_page_cannot_deliver_old_group_or_webhook_data():
    result = run("page_move")
    assert result["io"] == result["hooks"] == []


def test_mutated_client_page_cannot_deliver_old_private_snapshot():
    from tests.test_contact_presence_bridge import run as run_private
    assert run_private("host", "page_move")["sent"] == []


def test_group_helper_is_in_all_four_shipping_lists():
    for filename, field in (("setup_api.py", "CUSTOM_SRC_FILES"), ("build.py", "API_CUSTOM_SRC_FILES"),
                            ("client/ui/dialogs/api_setup.py", "_CUSTOM_SRC_FILES"),
                            ("tests/test_api_patches_in_sync.py", "MIRRORED_FILES")):
        tree = ast.parse((ROOT / filename).read_text(encoding="utf-8-sig"))
        values = [ast.literal_eval(node.value) for node in ast.walk(tree) if isinstance(node, ast.Assign)
                  and any(isinstance(target, ast.Name) and target.id == field for target in node.targets)]
        assert len(values) == 1 and "src/util/groupPresenceBridge.ts" in values[0], filename
