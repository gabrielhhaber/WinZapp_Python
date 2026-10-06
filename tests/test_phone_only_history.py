"""Issue #220: a chat whose older history is only on the phone.

`endOfHistoryTransferType` 4
(COMPLETE_ON_DEMAND_SYNC_BUT_MORE_MSG_REMAIN_ON_PRIMARY) is what a chat
becomes once an on-demand sync has delivered all a linked device is given.
The phone still has older messages, so `primaryHasMoreMessagesReadyToLoad` is
true and the request used to go out — but WhatsApp Web's own banner offers no
request in that state, only "Use WhatsApp on your phone to see older
messages." The phone answers one with silence and tells its owner "Sync
paused", and the user scrolling up waited five minutes for nothing.

Measured over CDP on a live session, 2026-10-04: the page function below, run
against real chats with the send replaced by a counter, sent for a state-4
chat before the fix and refused it after; state 0 still sent, and a probe
never did. The Node tests here run that same function, taken from the .ts,
against a fake page.
"""

import inspect
import json
import pathlib
import re
import shutil
import subprocess
import types

import pytest

import main
from main import MainWindow
from main_window import history_boundary
from ui.conversation_panel.history_loading import HistoryLoadingMixin

ROOT = pathlib.Path(__file__).resolve().parents[1]
API_PATCHES = ROOT / "client" / "api_patches" / "src"
JID = "5511999999999@s.whatsapp.net"
ANCHOR = {"key": {"id": "m1", "remoteJid": JID}, "messageTimestamp": 1_700_000_000}


def _controller() -> str:
    source = (API_PATCHES / "controller" / "deviceController.ts").read_text(encoding="utf-8")
    return source.replace("\r\n", "\n")


def _request_older_messages_source() -> str:
    source = _controller()
    return source[source.index("export async function requestOlderMessages("):]


# --------------------------------------------------------------------------
# The page function, under Node
# --------------------------------------------------------------------------

def _node():
    bundled = ROOT / "client" / "node" / "node.exe"
    if bundled.exists():
        return str(bundled)
    return shutil.which("node")


def _page_function() -> str:
    """requestOlderMessages' page.evaluate callback, as plain JavaScript."""
    body = _request_older_messages_source()
    start = body.index("async ({ chatId, probe }) => {")
    end = body.index("\n      { chatId: phone, probe }\n")
    fn = body[start:end].rstrip().rstrip(",")
    fn = fn.replace("(window as any)", "window")
    fn = re.sub(r": any\[\]", "", fn)
    return re.sub(r": any\b", "", fn)


_HARNESS = r"""
const cfg = CONFIG;
const sent = [];
const E = { COMPLETE_BUT_MORE_MESSAGES_REMAIN_ON_PRIMARY: 0,
  COMPLETE_AND_NO_MORE_MESSAGE_REMAIN_ON_PRIMARY: 1, INCOMPLETE: 2,
  NOT_INCLUDED_IN_HIST_SYNC: 3,
  COMPLETE_ON_DEMAND_SYNC_BUT_MORE_MSG_REMAIN_ON_PRIMARY: cfg.phoneOnlyValue,
  COMPLETE_ON_DEMAND_SYNC_WITH_MORE_MSG_ON_PRIMARY_BUT_NO_ACCESS: 5 };
const modules = {
  WAWebSendNonMessageDataRequest: {
    sendPeerDataOperationRequest: async (kind, arg) => { sent.push(kind); } },
  WAWebSyncGatingUtils: { isHistorySyncOnDemandEnabled: () => true },
  WAWebNonMessageDataRequestHistorySyncOnDemandUtils: {
    historySyncOnDemandRequestsFailureRecord: { disableRequestSending: false },
    getOldestMsgInChatFromDB: async () => ({ id: { _serialized: 'false_chat_OLDEST' } }) },
  WAWebUserPrefsHistorySync: { getHistorySyncStatus: async () => ({ recentCompleted: true }) },
  // WhatsApp Web's own function: true for every state but 1 and 2.
  WAWebHistorySyncUtils: { primaryHasMoreMessagesReadyToLoad: (s) => {
    if (s === 1 || s === 2) return false;
    if (s == null) throw new Error('Match: No case succesfully matched');
    return true; } },
  WAWebChatConstants: { ConversationEndOfHistoryTransferModelPropType: E },
  'WAWebProtobufsE2E.pb': { Message$PeerDataOperationRequestType: { HISTORY_SYNC_ON_DEMAND: 3 } },
  WAWebCollections: { Chat: { get: () => ({ id: 'chat-model-id' }) } },
};
const window = {
  require: (name) => {
    if (cfg.missing.includes(name)) throw new Error('module not found: ' + name);
    return modules[name];
  },
  WAPI: { getChat: () => ({ endOfHistoryTransferType: cfg.state }) },
  WPP: { whatsapp: { WidFactory: { createWid: (id) => id } } },
};
const run = PAGE_FUNCTION;
run({ chatId: 'chat@c.us', probe: cfg.probe }).then(
  (out) => console.log(JSON.stringify({ out, sends: sent.length })),
  (error) => console.log(JSON.stringify({ thrown: String(error) })),
);
"""


def _run_page(state, probe=False, phone_only_value=4, missing=()):
    node = _node()
    if not node:
        pytest.skip("node not available")
    config = {"state": state, "probe": probe, "phoneOnlyValue": phone_only_value,
              "missing": list(missing)}
    script = (_HARNESS.replace("CONFIG", json.dumps(config))
              .replace("PAGE_FUNCTION", _page_function()))
    done = subprocess.run([node, "-e", script], capture_output=True, text=True,
                          timeout=60, check=True)
    return json.loads(done.stdout)


class TestThePageRefusesBeforeSending:
    def test_a_phone_only_chat_is_not_asked(self):
        result = _run_page(state=4)

        assert result["sends"] == 0
        assert result["out"]["phoneOnly"] is True
        assert "only on the phone" in result["out"]["error"]
        assert "requested" not in result["out"]

    def test_it_passed_the_gate_that_was_there(self):
        """primaryHasMore is true for it — which is why it was being asked."""
        assert _run_page(state=4)["out"]["primaryHasMore"] is True

    def test_a_chat_that_may_ask_still_does(self):
        result = _run_page(state=0)

        assert result["sends"] == 1
        assert result["out"]["requested"] is True
        assert result["out"]["phoneOnly"] is False

    @pytest.mark.parametrize("state", [3, 5, None])
    def test_the_other_states_are_left_as_they_were(self, state):
        """Their verdict still comes from the outcome, not from the number."""
        result = _run_page(state=state)

        assert result["sends"] == 1
        assert result["out"]["phoneOnly"] is False

    def test_nothing_older_is_still_refused_as_before(self):
        result = _run_page(state=1)

        assert result["sends"] == 0
        assert result["out"]["error"] == "primary has no older messages for this chat"

    def test_the_state_is_read_by_name_so_a_renumbering_does_not_move_the_gate(self):
        assert _run_page(state=7, phone_only_value=7)["out"]["phoneOnly"] is True
        assert _run_page(state=4, phone_only_value=7)["sends"] == 1

    def test_a_missing_constants_module_falls_back_to_the_literal(self):
        result = _run_page(state=4, missing=["WAWebChatConstants"])

        assert result["sends"] == 0
        assert result["out"]["phoneOnly"] is True


class TestAProbeNeverSends:
    @pytest.mark.parametrize("state", [0, 3, 5, None])
    def test_it_stops_before_the_send(self, state):
        result = _run_page(state=state, probe=True)

        assert result["sends"] == 0
        assert result["out"]["probe"] is True
        assert result["out"]["phoneOnly"] is False

    def test_it_reports_a_phone_only_chat(self):
        result = _run_page(state=4, probe=True)

        assert result["sends"] == 0
        assert result["out"]["phoneOnly"] is True


class TestTheProbeHasARouteOfItsOwn:
    """An API built before the probe existed must answer 404. A flag on the
    request route would be ignored by it — and the request sent for real."""

    def test_the_route_is_a_get_on_the_read_only_handler(self):
        routes = (API_PATCHES / "routes" / "index.ts").read_text(encoding="utf-8")
        routes = re.sub(r"\s+", " ", routes)

        assert ("routes.get( '/api/:session/older-history-state/:phone', verifyToken, "
                "statusConnection, DeviceController.olderHistoryState );") in routes

    def test_the_handler_probes(self):
        source = _controller()
        handler = source[source.index("export async function olderHistoryState("):]
        handler = handler[:handler.index("\n}\n")]

        assert "return requestOlderMessages(req, res, true);" in handler

    def test_express_passing_next_does_not_turn_a_request_into_a_probe(self):
        assert "const probe = probeOnly === true;" in _request_older_messages_source()

    def test_the_request_route_cannot_be_asked_to_probe(self):
        source = _request_older_messages_source()
        source = source[:source.index("export async function olderHistoryState(")]

        assert "req.query" not in source and "req.body" not in source


# --------------------------------------------------------------------------
# Python: the verdict
# --------------------------------------------------------------------------

class _Response:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload
        self.text = ""

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _Window:
    wpp_server = "http://127.0.0.1"
    wpp_port = 6300
    token = "tok"
    request_older_messages = MainWindow.request_older_messages
    _normalize_jid = staticmethod(MainWindow._normalize_jid)

    def __init__(self, connected=True):
        self._wa_connected = connected
        self._phone_to_lid = {}


def _answer(monkeypatch, verb, status, response):
    seen = []

    def call(url, *args, **kwargs):
        seen.append(url)
        return _Response(status, {"response": response})

    monkeypatch.setattr(main.requests, verb, call)
    return seen


def _forbid(monkeypatch, verb):
    def boom(*args, **kwargs):
        raise AssertionError(f"must not {verb.upper()}")

    monkeypatch.setattr(main.requests, verb, boom)


PHONE_ONLY = {"phoneOnly": True, "primaryHasMore": True,
              "error": "older messages for this chat are only on the phone"}


class TestRequestOlderMessages:
    def test_a_phone_only_refusal_is_terminal_and_remembered(self, monkeypatch):
        window = _Window()
        _answer(monkeypatch, "post", 500, PHONE_ONLY)

        assert window.request_older_messages(JID) is False
        assert history_boundary.is_only_on_phone(window, JID) is True

    def test_it_is_logged_as_what_it_is(self, monkeypatch, caplog):
        window = _Window()
        _answer(monkeypatch, "post", 500, PHONE_ONLY)

        with caplog.at_level("INFO"):
            window.request_older_messages(JID)

        assert any("only on the phone" in r.message for r in caplog.records)
        assert not any("did not go out" in r.message for r in caplog.records)

    def test_a_request_that_goes_out_clears_an_earlier_verdict(self, monkeypatch):
        window = _Window()
        _answer(monkeypatch, "post", 500, PHONE_ONLY)
        window.request_older_messages(JID)
        _answer(monkeypatch, "post", 200,
                {"phoneOnly": False, "primaryHasMore": True, "requested": True})

        assert window.request_older_messages(JID) is True
        assert history_boundary.is_only_on_phone(window, JID) is False

    def test_an_answer_without_the_verdict_is_not_phone_only(self, monkeypatch):
        """An API built before the verdict existed, or an error before it."""
        window = _Window()
        _answer(monkeypatch, "post", 500, PHONE_ONLY)
        window.request_older_messages(JID)
        _answer(monkeypatch, "post", 500, {"error": "recent history sync is not complete yet"})

        assert window.request_older_messages(JID) is None
        assert history_boundary.is_only_on_phone(window, JID) is False


class TestProbe:
    def test_it_asks_the_read_only_route_and_never_posts(self, monkeypatch):
        window = _Window()
        _forbid(monkeypatch, "post")
        seen = _answer(monkeypatch, "get", 500, PHONE_ONLY)

        assert history_boundary.probe(window, JID) is True
        assert seen == ["http://127.0.0.1:6300/api/tok/older-history-state/5511999999999@c.us"]
        assert history_boundary.is_only_on_phone(window, JID) is True

    def test_it_prefers_the_lid_form_when_one_is_mapped(self, monkeypatch):
        window = _Window()
        window._phone_to_lid = {JID: "12345@lid"}
        seen = _answer(monkeypatch, "get", 200, {"phoneOnly": False, "probe": True})

        assert history_boundary.probe(window, JID) is False
        assert seen[0].endswith("/older-history-state/12345@lid")

    def test_an_api_without_the_route_is_not_known_to_be(self, monkeypatch):
        window = _Window()
        monkeypatch.setattr(main.requests, "get", lambda *a, **k: _Response(404))

        assert history_boundary.probe(window, JID) is False

    def test_a_transport_error_is_not_known_to_be(self, monkeypatch):
        window = _Window()

        def refuse(*args, **kwargs):
            raise OSError("connection refused")

        monkeypatch.setattr(main.requests, "get", refuse)

        assert history_boundary.probe(window, JID) is False

    def test_a_disconnected_session_never_calls_the_api(self, monkeypatch):
        window = _Window(connected=False)
        _forbid(monkeypatch, "get")

        assert history_boundary.probe(window, JID) is False


# --------------------------------------------------------------------------
# Python: the user scrolling up
# --------------------------------------------------------------------------

class _DB:
    def __init__(self):
        self.metadata = {}

    def set_metadata_json(self, key, value):
        self.metadata[key] = value


class _Scroller:
    """fetch_older_messages() at the edge of what the browser store holds."""

    def __init__(self, ask_result=False, ask_verdict=None, probe_verdict=None):
        self.db = _DB()
        self._wa_connected = True
        self._exhausted_chats = set()
        self._older_requested_chats = {}
        self._phone_to_lid = {}
        self._lid_to_phone = {}
        self.settings = {"user_interface": {"messages_page_size": 200}}
        self.wpp_server = "http://127.0.0.1"
        self.wpp_port = 6300
        self.token = "tok"
        self.ws = None
        self._ask_result = ask_result
        self._ask_verdict = ask_verdict
        self._probe_verdict = probe_verdict
        self.asks = []
        self.probes = []

    def request_older_messages(self, jid, timeout=60):
        self.asks.append(jid)
        history_boundary.note_verdict(self, jid, self._ask_verdict)
        return self._ask_result

    def _resolve_jid_for_msg_key(self, jid):
        return jid

    def _serialize_msg_id(self, jid, key):
        return f"false_{jid}_{key.get('id', '')}"


def _scroller(**kwargs):
    stub = _Scroller(**kwargs)
    for name in ("fetch_older_messages", "_persist_exhausted_chats",
                 "_persist_older_requested", "_normalize_jid"):
        raw = inspect.getattr_static(MainWindow, name)
        if isinstance(raw, (staticmethod, classmethod)):
            setattr(stub, name, getattr(MainWindow, name))
        else:
            setattr(stub, name, types.MethodType(raw, stub))
    stub._OLDER_REQUEST_GRACE = MainWindow._OLDER_REQUEST_GRACE
    return stub


@pytest.fixture
def edge(monkeypatch):
    """get-messages answers an empty page; the probe route answers
    `stub._probe_verdict` and is counted."""
    def install(stub):
        def get(url, *args, **kwargs):
            if "/older-history-state/" in url:
                stub.probes.append(url)
                return _Response(200, {"response": stub._probe_verdict})
            return _Response(200, {"response": []})

        monkeypatch.setattr(main.requests, "get", get)
        return stub

    return install


class TestScrollingUpIntoAPhoneOnlyChat:
    def test_the_first_ask_ends_the_search_at_once(self, edge):
        """[] is "this is the start": no five-minute wait for a reply that the
        phone was never asked for."""
        stub = edge(_scroller(ask_result=False, ask_verdict=PHONE_ONLY))

        assert stub.fetch_older_messages(JID, ANCHOR) == []
        assert stub.asks == [JID]
        assert stub.db.metadata["exhausted_chats"] == [JID]

    def test_a_chat_asked_earlier_is_probed_not_asked_again(self, edge):
        """The reported sequence: the request worked, delivered what it would,
        and left the chat phone-only."""
        stub = edge(_scroller(probe_verdict=PHONE_ONLY))
        stub._older_requested_chats[JID] = main.time.time() - 30

        assert stub.fetch_older_messages(JID, ANCHOR) == []
        assert stub.asks == []
        assert len(stub.probes) == 1
        assert JID in stub._exhausted_chats

    def test_a_chat_that_is_not_phone_only_keeps_waiting_for_its_reply(self, edge):
        stub = edge(_scroller(probe_verdict={"phoneOnly": False, "probe": True}))
        stub._older_requested_chats[JID] = main.time.time() - 30

        assert stub.fetch_older_messages(JID, ANCHOR) is None
        assert JID not in stub._exhausted_chats

    def test_a_request_that_went_out_is_still_awaited(self, edge):
        stub = edge(_scroller(ask_result=True, ask_verdict={"phoneOnly": False, "requested": True}))

        assert stub.fetch_older_messages(JID, ANCHOR) is None
        assert stub.probes == []

    def test_background_paging_does_not_probe(self, edge):
        """It runs every two seconds while a reply is awaited."""
        stub = edge(_scroller(probe_verdict=PHONE_ONLY))
        stub._older_requested_chats[JID] = main.time.time() - 30

        assert stub.fetch_older_messages(JID, ANCHOR, allow_phone_request=False) is None
        assert stub.probes == []

    def test_scrolling_up_again_asks_again_and_hears_the_same(self, edge):
        """Durable, but never a dead end: the state can move."""
        stub = edge(_scroller(ask_result=False, ask_verdict=PHONE_ONLY))
        stub.fetch_older_messages(JID, ANCHOR)

        assert stub.fetch_older_messages(JID, ANCHOR) == []
        assert stub.asks == [JID, JID]


# --------------------------------------------------------------------------
# The conversation panel says so
# --------------------------------------------------------------------------

class _Speech:
    def __init__(self):
        self.said = []

    def output(self, text, **options):
        self.said.append(text)


class _I18n:
    @staticmethod
    def t(key):
        return f"<{key}>"


class _MainWindow:
    _normalize_jid = staticmethod(MainWindow._normalize_jid)

    def __init__(self):
        self.speak_output = _Speech()
        self.i18n = _I18n()


class _Panel:
    _set_reached_start = HistoryLoadingMixin._set_reached_start

    def __init__(self, open_jid=JID):
        self.main_window = _MainWindow()
        self.conversation = {"remoteJid": open_jid}
        self._reached_server_start = {}
        self._is_loading_more = True


class TestTheStartOfAPhoneOnlyChatIsAnnounced:
    def test_it_is_spoken_through_speak_output(self):
        panel = _Panel()
        history_boundary.note_verdict(panel.main_window, JID, PHONE_ONLY)

        panel._set_reached_start(JID)

        assert panel.main_window.speak_output.said == ["<older_messages_on_phone>"]
        assert panel._reached_server_start == {JID: True}
        assert panel._is_loading_more is False

    def test_a_real_beginning_is_not_announced(self):
        panel = _Panel()

        panel._set_reached_start(JID)

        assert panel.main_window.speak_output.said == []
        assert panel._reached_server_start == {JID: True}

    def test_nothing_is_said_about_a_chat_that_is_no_longer_open(self):
        panel = _Panel(open_jid="5511888888888@s.whatsapp.net")
        history_boundary.note_verdict(panel.main_window, JID, PHONE_ONLY)

        panel._set_reached_start(JID)

        assert panel.main_window.speak_output.said == []
