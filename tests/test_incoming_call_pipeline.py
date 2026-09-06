"""Regression coverage for the native incoming-call pipeline.

(This file used to also carry test_wa_js_dependency_is_pinned_to_an_exact_
revision, a guard that could not fail — see
tests/test_wpp_homologated_runtime_pin.py, which replaced it.)
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_api_patch_forwards_native_call_events_to_socket_io():
    source = _source("client/api_patches/src/util/createSessionUtil.ts")
    assert "WPP.on('call.incoming_call'" in source
    assert "req.io.emit('incomingcall'" in source
    assert "ignored historical offer" in source


def test_websocket_normalizes_calls_before_dispatching_to_ui():
    source = _source("client/core/websocket_client.py")
    assert 'self.sio.on("incomingcall", self.on_wpp_incoming_call)' in source
    assert "self.main_window.on_incoming_call_event" in source
    assert "normalized" in source


def test_ui_has_native_alert_lifecycle_and_user_stop_controls():
    main = _source("client/main.py")
    dialog = _source("client/ui/dialogs/incoming_call.py")
    assert "_arm_incoming_call_watchdog" in main
    assert "stop_all_incoming_call_alerts" in main
    assert "_show_incoming_call_dialog" in main
    assert "IncomingCallDialog" in dialog
