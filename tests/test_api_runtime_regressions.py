"""Structural guards for runtime-only WPPConnect integration contracts."""

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]


def _patch(relative_path):
    return (ROOT / "client" / "api_patches" / relative_path).read_text(
        encoding="utf-8"
    )


def test_message_edit_accepts_current_and_legacy_callback_shapes():
    source = _patch("src/util/createSessionUtil.ts")

    assert "legacyMessage ?? eventOrChat?.msg" in source
    assert "onMessageEdit emitted without a serialized message" in source
    assert "session: client.session" in source


def test_force_kill_filter_is_separator_agnostic_and_skips_itself():
    """forceKillByUserDataDir()'s PowerShell filter has to match the *real*
    Chrome CommandLine, and both halves of that used to be wrong.

    Both call sites pass `userDataDir/<session>` with a forward slash, but
    that string never reaches Chrome verbatim: puppeteer path.resolve()s the
    relative './userDataDir/<session>' before building --user-data-dir, so the
    live process reads `...\\userDataDir\\<session>` (backslashes, and 8.3 short
    names for the parent directories). PowerShell's `-like` treats `\\` and `/`
    as ordinary, non-interchangeable characters, so a filter that keeps the
    literal separator matched the browser zero times — while still matching
    the powershell.exe running the query, whose own -Command argument does
    contain the forward-slash text, which is what made the script Stop-Process
    itself on every invocation.
    """
    source = _patch("src/util/createSessionUtil.ts")

    # Every run of separators collapses to a `*`, so only the tail of the path
    # is matched — immune to the separator flavour and to 8.3 shortening.
    assert r".replace(/[\\/]+/g, '*')" in source
    # The old backslash-doubling escape must not come back: `-like` reads `\\`
    # as two literal backslashes, which no command line ever contains.
    assert r"replace(/\\/g, '\\\\')" not in source
    # Wildcard metacharacters are neutralised before the separator wildcards
    # are introduced, so a session id can never act as a pattern.
    assert r".replace(/[`*?[\]]/g, '`$&')" in source
    assert "$_.ProcessId -ne $mypid" in source


def test_status_probe_distinguishes_not_ready_from_disconnected():
    source = _patch("src/middleware/statusConnection.ts")

    assert "connected !== true" in source
    assert "new SessionNotReadyError(detail)" in source
    assert "WAPI is not defined" in source
    assert "next(error)" in source


def test_both_page_error_classifiers_agree_on_a_dead_page():
    """wppconnect 2.3.2 made isConnected() await waitForPageLoad(), which
    throws instead of hanging: "Page closed before WAPI injection completed"
    when the document died mid-injection, "Waiting failed: 30000ms exceeded"
    when it loaded but never reached WPP.isReady.

    statusConnection.ts named the first one and nothing named the second, so
    the same string meant Disconnected/404 in one file and internal_error/500
    in the other — and classifyPageError() is errorHandler's general net for
    everything thrown outside a route that declares statusConnection. Widening
    is safe here in a way `/not found/i` is not: no puppeteer or wppconnect
    message carries "Page closed" for anything but a dead page, whereas "not
    found" also matches "Session not found".
    """
    status_connection = _patch("src/middleware/statusConnection.ts")
    domain = _patch("src/errors/domain.ts")

    for source in (status_connection, domain):
        assert "Page closed" in source
        assert r"Waiting failed: \d+ms exceeded" in source


def test_the_is_connected_retry_window_is_bounded_by_wall_clock():
    """`maxAttempts * sleep` stopped being the total the moment isConnected()
    started awaiting waitForPageLoad(): one attempt can cost puppeteer's full
    30s default, which turned the documented "~10s" into ~10 minutes and left
    onParticipantsChanged / onReactionMessage / onRevokedMessage /
    onPollResponse unregistered for all of it.
    """
    source = _patch("src/util/createSessionUtil.ts")

    assert "const RETRY_WINDOW_MS = 10000;" in source
    assert "for (let attempt = 1; Date.now() < deadline; attempt++)" in source
    # Each probe is raced against what is left of the budget, and the losing
    # probe keeps a rejection handler — an unhandled rejection exits Node.
    assert "await probeIsConnected(" in source
    assert "probe.catch(() => undefined);" in _patch("src/util/functions.ts")
    # The listeners that must never wait behind it are still wired first.
    wire = source.index("await this.wireListeners(req, client);")
    loop = source.index("const RETRY_WINDOW_MS = 10000;")
    assert wire < loop


def test_the_connection_probe_in_front_of_every_send_is_bounded_too():
    """The same 30s cost, in the one place that answers a client already on a
    clock. statusConnection fronts every send route; main.py posts text with
    timeout=25 and a voice message with timeout=30, so a WhatsApp Web reload
    made the client give up before the middleware did — and a ReadTimeout is
    classified *ambiguous*, so MessageQueue drops the message without retrying
    and the user is told "unconfirmed" about a message that never reached a
    controller at all.

    Reported as Disconnected rather than SessionNotReadyError on purpose: 404
    leaves the message queued for the reconnection, where the retryable 503
    would spend MessageQueue's four attempts against a page still reloading.
    """
    source = _patch("src/middleware/statusConnection.ts")

    assert "const CONNECTION_PROBE_BUDGET_MS = 8000;" in source
    assert "await probeIsConnected(" in source
    assert "CONNECTION_PROBE_BUDGET_MS" in source
    assert "await req.client.isConnected()" not in source
    probe = source.index("await probeIsConnected(")
    verdict = source.index("if (connected !== true) return disconnected(res);")
    assert probe < verdict


def test_an_unanswered_probe_is_named_apart_from_a_real_disconnection():
    """Both answers are 404 Disconnected, but only the second is about the
    network — and this middleware fronts list-chats as well as every send, so
    Python was announcing "modo offline" (sound and speech) and then "conexão
    restaurada" for an ordinary WhatsApp Web reload that overlapped a
    background sync round. `reason` is what
    MainWindow._check_wa_connection_closed() reads to keep the send verdict
    without the offline one; the Python half is
    tests/test_probe_timeout_is_not_offline.py.
    """
    source = _patch("src/middleware/statusConnection.ts")

    assert "function disconnected(res: Response, reason?: string) {" in source
    assert "if (connected === undefined) return disconnected(res, 'probe_timeout');" in source
    # Ordered: undefined must be answered before the `!== true` catch-all,
    # which would otherwise swallow it into the unnamed 404 again.
    timeout = source.index("return disconnected(res, 'probe_timeout');")
    verdict = source.index("if (connected !== true) return disconnected(res);")
    assert timeout < verdict
    # A probe that answers false, and every non-probe path, stays unnamed.
    assert source.count("disconnected(res, 'probe_timeout')") == 1


def test_the_bounded_probe_has_exactly_one_definition():
    """Both callers share it from util/functions.ts. A second copy is how the
    two budgets start disagreeing about what an unanswered probe means."""
    functions = _patch("src/util/functions.ts")
    session_util = _patch("src/util/createSessionUtil.ts")
    status = _patch("src/middleware/statusConnection.ts")

    assert functions.count("export async function probeIsConnected(") == 1
    for caller in (session_util, status):
        assert "probeIsConnected" in caller
        assert "async probeIsConnected(" not in caller


def test_set_limit_route_authenticates_and_checks_connection():
    source = _patch("src/routes/index.ts")
    route = re.search(
        r"routes\.post\(\s*'/api/:session/set-limit',(?P<body>.*?)\);",
        source,
        re.DOTALL,
    )

    assert route is not None
    assert re.search(
        r"verifyToken,\s*statusConnection,\s*MiscController\.setLimit",
        route.group("body"),
    )
