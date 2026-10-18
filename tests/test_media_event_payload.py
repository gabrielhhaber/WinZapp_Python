"""Documents must reach the local UI as metadata, not hidden base64 downloads.

The webhook default enables autoDownload even when no URL is configured.
That flag must not turn the separate received-message Socket.IO event into
a whole-file payload. All storage and network calls here are synthetic.
"""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def run(**options):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable")
    result = subprocess.run([
        node, str(ROOT / "tests/media_event_payload_runtime.cjs"),
        str(ROOT / "client/api_patches/src/util/functions.ts"),
        str(ROOT / "client/api_patches/src/util/createSessionUtil.ts"),
    ], input=json.dumps(options), capture_output=True, text=True,
        encoding="utf-8", timeout=10)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("from_me", [False, True])
def test_local_client_gets_metadata_without_downloading_the_file(from_me):
    result = run(fromMe=from_me)
    assert result["downloads"] == 0
    assert result["posts"] == []
    assert result["emitted"][0]["data"]["response"]["body"] == ""


def test_configured_webhook_downloads_only_for_its_own_delivery():
    result = run(webhook=True)
    assert result["downloads"] == 1
    assert len(result["posts"]) == 1
    assert result["posts"][0]["body"]
    assert result["emitted"][0]["data"]["response"]["body"] == ""


def test_explicit_websocket_download_is_still_honored():
    result = run(websocket=True)
    assert result["downloads"] == 1
    assert result["emitted"][0]["data"]["response"]["body"]
