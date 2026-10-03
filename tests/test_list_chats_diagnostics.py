"""POST /list-chats must explain itself in wppconnect.log, without personal data.

A tester's server answered 200 with 0 chats on every one of 30+ attempts and
the log could not say whether the raw ChatStore list was empty or non-empty and
emptied by the visibleChats filter. client/api_patches/src/util/listChatsDiag.ts
builds the counts and the one-line form; deviceController.listChats logs it
AFTER the response. Logging only: the filter, the response body, the recovery
trigger and the status codes are untouched.

Two kinds of check: source contracts on the controller (like
tests/test_forward_messages_lazy_resources.py) and, when ``node`` can strip
types, the very same .ts file run against fake chat arrays.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "client" / "api_patches" / "src"
DIAG = SRC / "util" / "listChatsDiag.ts"
CONTROLLER = SRC / "controller" / "deviceController.ts"


@pytest.fixture(scope="module")
def controller() -> str:
    return CONTROLLER.read_text(encoding="utf-8").replace("\r\n", "\n")


def _function(source: str, header: str) -> str:
    start = source.index(header)
    match = re.search(r"\n(?:export )?(?:async )?function ", source[start + len(header):])
    end = start + len(header) + match.start() if match else len(source)
    return source[start:end]


class TestControllerWiring:
    def test_the_response_path_is_untouched(self, controller):
        handler = _function(controller, "export async function listChats(")
        assert (
            "res.status(200).json(\n      observePayload(SyncChatListSchema, visibleChats, {"
            in handler
        )
        assert "endpoint: 'list-chats'," in handler
        assert "Error on get all chats" in handler

    def test_the_filter_is_unchanged(self, controller):
        handler = _function(controller, "export async function listChats(")
        for part in (
            "if (!jid || jid.endsWith('@g.us')) return true;",
            "Boolean(chat?.t) ||",
            "Boolean(chat?.lastMessage) ||",
            "Number(chat?.unreadCount || 0) > 0;",
            "chat?.hasChatBeenOpened === true || chat?.contact?.isMyContact === true;",
            "return hasActivity || isKnownConversation;",
        ):
            assert part in handler

    def test_the_diagnostic_runs_after_the_response_and_is_not_awaited(self, controller):
        handler = _function(controller, "export async function listChats(")
        sent = handler.index("res.status(200).json(")
        logged = handler.index("void logListChatsDiag(req, chats, visibleChats, diag);")
        assert logged > sent

    def test_the_recovery_trigger_is_unchanged(self, controller):
        body = _function(controller, "async function listChatsWithDiag(")
        assert "if (first.chats.length > 0) {" in body
        assert "const coolingDown = Date.now() - recoveredAt < RECOVERY_COOLDOWN_MS;" in body
        assert body.count("page.evaluate(") == 1

    def test_the_other_callers_still_get_the_plain_array(self, controller):
        wrapper = _function(controller, "async function listChatsWithStoreRecovery(")
        assert "return (await listChatsWithDiag(req, options)).chats;" in wrapper
        assert controller.count("await listChatsWithStoreRecovery(req") == 2

    def test_the_idb_count_is_read_only_and_bounded(self, controller):
        body = _function(controller, "async function countIdbChats(")
        assert "'chat', 'readonly'" in body
        assert ".count()" in body
        assert "setTimeout(() => resolve('timeout'), 3000)" in body
        assert "setTimeout(() => resolve('timeout'), 5000)" in body
        assert "transaction?.abort()" in body, "must never create the database"
        assert "put(" not in body and "add(" not in body and "delete(" not in body

    def test_the_idb_count_only_runs_when_nothing_is_visible(self, controller):
        body = _function(controller, "async function logListChatsDiag(")
        assert "counts.visible === 0 && claimIdbCount(state, now)" in body
        assert "diagStateFor(session)" in body

    def test_the_idb_count_has_its_own_catch_and_never_throws(self, controller):
        body = _function(controller, "async function countIdbChats(")
        outer = body[body.index("  try {\n    const evaluation"):]
        assert "  } catch (_error) {\n    return 'unavailable';\n  } finally {" in outer
        assert "clearTimeout(timer)" in outer

    def test_the_logging_never_throws_into_the_caller(self, controller):
        body = _function(controller, "async function logListChatsDiag(")
        assert body.rstrip().endswith("}")
        assert "catch (_error)" in body

    def test_the_file_is_in_every_list(self):
        name = "src/util/listChatsDiag.ts"
        for path in ("setup_api.py", "build.py", "client/ui/dialogs/api_setup.py",
                     "tests/test_api_patches_in_sync.py"):
            assert name in (ROOT / path).read_text(encoding="utf-8"), path

    def test_the_python_warning_points_at_the_server_log(self):
        sync = (ROOT / "client" / "main_window" / "sync.py").read_text(encoding="utf-8")
        start = sync.index("Chat list still empty after every attempt")
        assert "'[listChats] diag'" in sync[start:start + 900]

    def test_no_log_line_names_a_chat(self, controller):
        body = _function(controller, "async function logListChatsDiag(")
        assert "formatDiagLine(" in body
        assert "JSON.stringify" not in body


HARNESS = r"""
import { pathToFileURL } from 'node:url';
const mod = await import(pathToFileURL(process.argv[2]).href);
const scenarios = JSON.parse(process.env.SCENARIOS);
const out = [];
for (const s of scenarios) {
  const raw = s.raw === 'notarray' ? 'x' : (s.raw || []).map((c) => c);
  let visible = s.visibleRule === 'all' ? raw : [];
  if (s.visibleRule === 'activity') {
    visible = raw.filter((c) => c && (c.t || (c.id && c.id._serialized && c.id._serialized.endsWith('@g.us'))));
  }
  const counts = mod.buildDiag(raw, visible);
  const line = mod.formatDiagLine({ ...counts, ...(s.extra || {}) });
  out.push({ counts, line });
}
const hostile = [
  undefined, null, 5, 'x', {}, [],
  { raw: 1e30, visible: -5, groups: NaN, users: Infinity, lids: '9'.repeat(40),
    other: {}, droppedShells: 123456789012, recovered: 'yes', firstError: 'a\nb\r"c\'',
    storeReady: 1, idbChats: 987654321098, ms: 1e99, rawFirst: {}, rawSecond: [] },
  { firstError: '12345678901234567890@lid failed for 5511999998888@s.whatsapp.net\nsecond line' },
  { idbChats: 'timeout' }, { idbChats: 'evil\nvalue' }, { firstError: 'é中文' },
  { get raw() { throw new Error('boom'); } },
];
const errors = [
  'Joao Silva said "hi" 5511999998888@s.whatsapp.net',
  "Cannot read properties of undefined (reading Joo)",
  "x is not a function for 120363012345678901@g.us",
  "WPP.chat.list returned a non-array value",
  "WAPI._serializeChatObj is unavailable",
  "Navigation timed out after 30000 ms",
  42, "", undefined,
].map((e) => {
  const m = /firstError=(\S*)/.exec(mod.formatDiagLine({ firstError: e }));
  return m[1];
});
out.push({ hostile: hostile.map((h) => mod.formatDiagLine(h)), errors });
const state = mod.diagStateFor('s1');
const c = (visible, raw = visible) => ({ raw, visible, groups: 0, users: 0, lids: 0, droppedShells: raw - visible });
const rate = [];
rate.push(mod.shouldLogDiag(state, c(0), 1000));
rate.push(mod.shouldLogDiag(state, c(0), 2000));
rate.push(mod.shouldLogDiag(state, c(0, 3), 3000));
rate.push(mod.shouldLogDiag(state, c(0, 3), 4000));
rate.push(mod.shouldLogDiag(state, c(0, 3), 33001));
rate.push(mod.shouldLogDiag(state, c(5), 34000));
rate.push(mod.shouldLogDiag(state, c(5), 99000));
rate.push(mod.shouldLogDiag(state, c(6), 99500));
const idb = [mod.claimIdbCount(state, 100000), mod.claimIdbCount(state, 150000), mod.claimIdbCount(state, 160001)];
const other = mod.diagStateFor('s2');
const sessions = [mod.shouldLogDiag(other, c(0), 1000), mod.diagStateFor('s1') === state];
out.push({ rate, idb, sessions });
console.log(JSON.stringify(out));
"""


def _node_can_strip_types() -> bool:
    node = shutil.which("node")
    if not node:
        return False
    try:
        done = subprocess.run(
            [node, "--experimental-strip-types", "-e", "0"],
            capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0


needs_node = pytest.mark.skipif(
    not _node_can_strip_types(), reason="node with type stripping is not available"
)


ERROR_WORDS = {"none", "non_array", "serializer_unavailable", "js_type_error",
               "timeout", "other"}


def _jid(kind, n):
    return {"g": f"1203630{n:07d}-1700000000@g.us", "u": f"55119999{n:05d}@s.whatsapp.net",
            "c": f"55119998{n:05d}@c.us", "l": f"9876543210{n:05d}@lid"}[kind]


def _chat(kind, n, **fields):
    return {"id": {"_serialized": _jid(kind, n)}, **fields}


def _run(tmp_path, scenarios):
    module = tmp_path / "listChatsDiag.mts"
    module.write_text(DIAG.read_text(encoding="utf-8"), encoding="utf-8")
    script = tmp_path / "harness.mjs"
    script.write_text(HARNESS, encoding="utf-8")
    import os
    env = dict(os.environ, SCENARIOS=json.dumps(scenarios))
    done = subprocess.run(
        ["node", "--experimental-strip-types", str(script), str(module)],
        capture_output=True, text=True, timeout=20, env=env,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def _assert_safe(line):
    assert "\n" not in line and "\r" not in line
    assert line.isascii()
    assert "@" not in line
    assert not re.search(r"\d{7,}", line), line
    assert line.startswith("[listChats] diag")


@pytest.fixture(scope="module")
def results(tmp_path_factory):
    if not _node_can_strip_types():
        pytest.skip("node with type stripping is not available")
    shells = [_chat("u", i, t=0, lastMessage=None, unreadCount=0,
                    hasChatBeenOpened=False, contact={"isMyContact": False})
              for i in range(3)]
    mixed = [
        _chat("g", 1, t=5), _chat("g", 2),
        _chat("u", 1, t=9, lastMessage={"x": 1}), _chat("c", 2, unreadCount=2),
        _chat("l", 1, t=3), _chat("l", 2), _chat("u", 3),
    ]
    sloppy = [{}, {"id": None}, {"id": 7}, {"id": {"_serialized": 7}}, None, "x",
              {"id": {"_serialized": "weird@broadcast"}}]
    scenarios = [
        {"raw": []},
        {"raw": shells},
        {"raw": mixed, "visibleRule": "activity",
         "extra": {"rawFirst": 0, "rawSecond": 7, "recovered": True,
                   "firstError": "WPP.chat.list returned a non-array value",
                   "storeReady": True, "storeChats": 7, "idbChats": 41, "ms": 183}},
        {"raw": sloppy},
        {"raw": 'notarray'},
    ]
    return _run(tmp_path_factory.mktemp("diag"), scenarios)


@needs_node
class TestDiagnosticsRunUnderNode:
    def test_empty_raw(self, results):
        counts = results[0]["counts"]
        assert counts["raw"] == 0 and counts["visible"] == 0
        assert counts["droppedShells"] == 0
        line = results[0]["line"]
        _assert_safe(line)
        assert " raw=0 " in line and " visible=0 " in line
        assert "rawSecond=none" in line and "idbChats=unavailable" in line

    def test_everything_dropped_says_why(self, results):
        counts = results[1]["counts"]
        assert (counts["raw"], counts["visible"], counts["users"]) == (3, 0, 3)
        assert counts["droppedShells"] == 3
        assert counts["droppedNoT"] == counts["droppedNoLastMsg"] == 3
        assert counts["droppedNoUnread"] == counts["droppedNoOpened"] == 3
        assert counts["droppedNoContact"] == 3
        line = results[1]["line"]
        _assert_safe(line)
        assert "raw=3 " in line and "visible=0 " in line and "droppedShells=3 " in line
        assert "droppedNoT=3 droppedNoLastMsg=3" in line

    def test_mixed_groups_users_and_lids(self, results):
        counts = results[2]["counts"]
        assert counts["groups"] == 2 and counts["lids"] == 2 and counts["users"] == 3
        assert counts["raw"] == 7 and counts["visible"] == 4
        assert counts["droppedShells"] == 3
        # the "activity" rule above keeps groups and chats with t: 4 of 7 stay
        line = results[2]["line"]
        _assert_safe(line)
        assert "rawFirst=0 rawSecond=7" in line
        assert "recovered=true" in line
        assert "firstError=non_array " in line
        assert "storeReady=true storeChats=7 idbChats=41 ms=183" in line

    def test_undefined_fields_and_odd_ids(self, results):
        counts = results[3]["counts"]
        assert counts["raw"] == 7
        assert counts["groups"] == counts["lids"] == counts["users"] == 0
        assert counts["other"] == 7
        _assert_safe(results[3]["line"])

    def test_a_non_array_raw_is_an_empty_list(self, results):
        assert results[4]["counts"]["raw"] == 0
        _assert_safe(results[4]["line"])

    def test_hostile_values_stay_one_ascii_line_without_ids(self, results):
        for line in results[5]["hostile"]:
            _assert_safe(line)
        huge = results[5]["hostile"][6]
        assert "raw=999999 " in huge and "visible=0 " in huge
        assert "droppedShells=999999 " in huge and "idbChats=999999 " in huge
        assert "recovered=false" in huge and "storeReady=false" in huge

    def test_an_error_text_becomes_a_short_code(self, results):
        line = results[5]["hostile"][7]
        match = re.search(r"firstError=(\S*) ", line)
        assert match and match.group(1) in ERROR_WORDS
        assert results[5]["hostile"][-1] == "[listChats] diag unavailable"

    def test_a_hostile_error_text_maps_to_one_of_the_six_words(self, results):
        words = results[5]["errors"]
        assert [w for w in words] == [
            "other", "js_type_error", "js_type_error", "non_array",
            "serializer_unavailable", "timeout", "none", "none", "none",
        ]
        for word in words:
            assert word in ERROR_WORDS

    def test_idb_words_are_whitelisted(self, results):
        assert "idbChats=timeout" in results[5]["hostile"][8]
        assert "idbChats=unavailable" in results[5]["hostile"][9]

    def test_rate_limit(self, results):
        rate = results[6]["rate"]
        # first empty answer logs, same numbers within 30 s do not, changed numbers do,
        # the same numbers again after 30 s do; the first answer with chats logs once.
        assert rate == [True, False, True, False, True, True, False, False]

    def test_the_idb_count_is_claimed_once_a_minute(self, results):
        assert results[6]["idb"] == [True, False, True]

    def test_state_is_per_session(self, results):
        assert results[6]["sessions"] == [True, True]
