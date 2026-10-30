"""A session that links before its state listener attaches must still finalize.

`createSessionUtil.start()` subscribes to onStateChange only after create()
returns, and on a fast link WhatsApp Web has already reported CONNECTED by then
(seen live: login at 20:11:11, inChat at 20:11:13, no "State Change" line at
all). That leaves start()'s isConnected() probe as the only thing that can call
markConnected(). The probe used to promote only from INITIALIZING, but pairing
leaves the session on PHONECODE or QRCODE, so session-logged never went out:
the phone listed the device as connected and the pairing dialog stayed open.

The TypeScript runs inside Node, so the set and the guard are read from source.
"""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = (
    ROOT / "client" / "api_patches" / "src" / "util" / "createSessionUtil.ts"
).read_text(encoding="utf-8")


def _pre_connected_statuses():
    match = re.search(
        r"const PRE_CONNECTED_STATUSES = new Set\(\[(.*?)\]\);", SOURCE, re.S
    )
    assert match, "PRE_CONNECTED_STATUSES not found"
    return set(re.findall(r"'([^']+)'", match.group(1)))


def _start_body():
    begin = SOURCE.index("async start(req: Request, client: WhatsAppServer)")
    end = SOURCE.index("Register message/ack/presence listeners", begin)
    return SOURCE[begin:end]


def test_probe_can_promote_from_every_pairing_status():
    assert {"INITIALIZING", "QRCODE", "PHONECODE", "notLogged"} <= (
        _pre_connected_statuses()
    )


def test_probe_never_overrides_a_terminal_state():
    terminal = {
        "UNPAIRED",
        "UNPAIRED_IDLE",
        "TIMEOUT",
        "DEPRECATED_VERSION",
        "PROXYBLOCK",
        "TOS_BLOCK",
        "SMB_TOS_BLOCK",
        "CLOSED",
    }
    assert not terminal & _pre_connected_statuses()


def test_start_guards_the_probe_with_the_set():
    body = _start_body()
    assert re.search(
        r"if \(PRE_CONNECTED_STATUSES\.has\(client\.status\)\) \{\s*"
        r"this\.markConnected\(client, req\);",
        body,
    )
    assert "client.status === 'INITIALIZING'" not in body
