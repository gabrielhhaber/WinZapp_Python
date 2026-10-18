"""A model graph that CDP cannot transfer must not become an empty chat list.

Measured on a live 715-chat session: the page's list was complete and ready,
but returning WAPI model graphs yielded undefined; JSON snapshots returned
all 715 chats. Exercise the actual controller callback without a browser.
"""

import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("recovery", [False, True])
def test_models_with_json_hooks_cross_the_page_boundary(recovery):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable")
    result = subprocess.run(
        [node, str(ROOT / "tests/chat_store_serialization.cjs"),
         str(ROOT / "client/api_patches/src/controller/deviceController.ts")],
        input=json.dumps({"recovery": recovery}), capture_output=True,
        text=True, encoding="utf-8", timeout=10,
    )
    assert result.returncode == 0, result.stderr
    answer = json.loads(result.stdout)
    assert answer["status"] == 200
    assert answer["body"]["response"] == [{
        "id": {"_serialized": "synthetic@g.us"}, "t": 123,
        "contact": {"isMyContact": True},
    }]
