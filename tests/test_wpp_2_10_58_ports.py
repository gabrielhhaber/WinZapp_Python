"""The 2.10.58 upstream changes WinZapp's overridden files had to carry.

The TypeScript only compiles inside client/api/, which does not exist in the
test job, so these assertions read the patched sources, as
tests/test_wedged_initializing_session.py does for the same files.

* createSessionUtil.ts: upstream gates every session start behind
  `tryBeginSessionStart()` (a backup or restore owns the profile directories).
  WinZapp keeps its own ownership logic inside createSessionUtil() and gates
  in opendata(), the only caller, so the gate must release in a `finally`.
* sessionController.ts: start-all and show-all answered a wrong secret key
  and then kept running; starts are refused during maintenance.
* auth.ts: the verified-token cache must sit beside, not replace, the
  timing-safe comparison WinZapp already had.
"""

from pathlib import Path

PATCHES = Path(__file__).resolve().parents[1] / "client" / "api_patches" / "src"


def _read(rel: str) -> str:
    return (PATCHES / rel).read_text(encoding="utf-8")


def test_opendata_gates_starts_and_always_releases():
    source = _read("util/createSessionUtil.ts")
    start = source.index("  async opendata(")
    body = source[start : source.index("\n  }\n", start)]
    assert "tryBeginSessionStart()" in body
    assert "status(503)" in body
    assert "finally" in body and "releaseStart()" in body
    assert "import { tryBeginSessionStart } from './sessionMaintenance';" in source


def test_session_token_write_is_awaited():
    source = _read("util/createSessionUtil.ts")
    assert "await myTokenStore.setToken(session, tokenData);" in source


def test_wrong_secret_key_returns_before_listing_sessions():
    source = _read("controller/sessionController.ts")
    assert "return res.status(400).json({\n      response: 'error'," in source
    assert "return res.status(400).json({\n      response: false," in source
    assert "rejectStartDuringMaintenance(res)" in source


def test_auth_keeps_timing_safe_compare_next_to_the_token_cache():
    source = _read("middleware/auth.ts")
    assert "function timingSafeEqualStr(" in source
    assert "timingSafeEqualStr(" in source.split("const verifyToken", 1)[1]
    assert "rememberVerified(cacheKey);" in source
    assert "export function clearVerifiedTokens" in source
