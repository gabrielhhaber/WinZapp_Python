from pathlib import Path
from tests.god_modules import status_panel_source


ROOT = Path(__file__).resolve().parents[1]


def test_text_status_has_bounded_ack_wait():
    source = (
        ROOT / "client" / "api_patches" / "src" / "controller"
        / "statusController.ts"
    ).read_text(encoding="utf-8")
    assert "waitForAck: true" in source
    assert "const sendPromise = sendStatus(req.client, 'text', text, {" in source
    assert "const timeoutPromise = new Promise(" in source
    assert "statusTimeoutHandled: true" in source
    assert "10000" in source
    assert "Promise.race([sendPromise, timeoutPromise])" in source


def test_status_http_timeout_allows_ack_fallback_to_finish():
    """Every status post waits longer than the server's 10 s ack fallback:
    text for 60 s, voice, a photo or a video for as long as their upload
    needs, never less (status_rules.status_upload_timeout())."""
    from status_tab.status_rules import status_upload_timeout
    source = status_panel_source()
    assert "api_post(url, json=payload, headers=headers, timeout=60)" in source
    assert source.count("timeout=status_upload_timeout(") >= 2
    assert status_upload_timeout(0) >= 60
