"""One way in and out of WinZapp's embedded Node API.

Today there are 74 places in main.py that call the local WPPConnect server, 59
of which build the same Authorization header by hand. Each of them decides for
itself what to log, how long to wait, and what a failure means — which is why
the same class of problem keeps having to be diagnosed from scratch every time,
and why two of the bugs open right now (a status reply that failed once and
worked seconds later; a sync that is slow somewhere between Python and the
page) have no evidence to work from.

WHAT THIS ADDS

*Correlation.* Every request carries an `X-Request-Id`. The Node side already
generates one when the header is absent and threads it through its own logger
(see requestInstrumentation in src/middleware/instrumentation.ts) — it simply
never had one handed to it. With the header sent from here, a line in log.log
and a line in wppconnect.log can finally be matched up.

*Duration.* Each call logs how long it took, so "the sync is slow" can name
which endpoint.

*A redacted URL.* This matters more than it looks. WPPConnect authenticates by
putting `<session>:<token>` in the PATH, so every URL logged in full publishes
the token that authorises every other call. The current log has 2,360 such
lines — in the file users are asked to send when something breaks. Nothing here
ever logs a full URL.

WHAT THIS DELIBERATELY DOES NOT DO

No retries and no error translation. Callers already implement their own retry
policies, and those policies encode real knowledge — message_queue treats an
ambiguous timeout differently from a definite failure precisely because
retrying the ambiguous case used to duplicate real sends. A blanket retry here
would quietly undo that.
"""

import logging
import re
import time
import uuid

import requests

# The Node middleware only accepts an id matching /^[A-Za-z0-9._:-]{1,128}$/ and
# silently generates its own otherwise, which would break correlation without
# anything saying so. A uuid4 hex is inside that set by construction.
_REQUEST_ID_HEADER = "X-Request-Id"

# Anything slower than this is worth a warning rather than an info line: it is
# the local loopback, so a call this slow means the page (or Puppeteer) is
# busy, which is the thing worth noticing.
SLOW_REQUEST_SECONDS = 2.0


def new_request_id() -> str:
    """A correlation id both sides accept."""
    return uuid.uuid4().hex


# WPPConnect's GLOBAL secret key (config.json's SECRET_KEY — the credential
# that mints session tokens, and a real user secret whenever wpp_custom_api is
# on) also travels in the URL path, across six routes, in two different
# positions. Enumerated rather than inferred: only routes/index.ts knows which
# path segment is a credential and which is an id, so a new `:secretkey` route
# added there has to be added to the matching tuple here as well.
# Source: client/api_patches/src/routes/index.ts (lines 59, 63, 66, 111, 946, 948).
_SECRET_KEY_FIRST_ROUTES = (   # /api/<key>/<endpoint>
    "/show-all-sessions",
    "/start-all",
    "/backup-sessions",
    "/restore-sessions",
)
_SECRET_KEY_SECOND_ROUTES = (  # /api/<session>/<key>/<endpoint>
    "/generate-token",
    "/clear-session-data",
)


def redact_api_url(url: str) -> str:
    """A log-safe label for an API URL: no token, just the endpoint.

    WPPConnect's routes are /api/<session>:<token>/<endpoint>/..., so the
    credential sits in the path rather than a header. Everything before the
    endpoint is dropped, and what remains is what a reader actually wants.
    """
    if not url:
        return ""
    marker = "/api/"
    index = url.find(marker)
    if index == -1:
        # Not an API URL (health checks, /metrics, ...): keep the path only.
        without_scheme = url.split("://", 1)[-1]
        return "/" + without_scheme.split("/", 1)[1] if "/" in without_scheme else url
    rest = url[index + len(marker):]
    # rest is "<session>:<token>/<endpoint>/<...>" — drop the credential segment.
    parts = rest.split("/", 1)
    endpoint = parts[1] if len(parts) > 1 else ""
    # ...except for the routes that carry WPPConnect's GLOBAL secret key in a
    # *second* path segment (_SECRET_KEY_SECOND_ROUTES): dropping only the
    # first segment left that key in the label — logged at INFO on every
    # pairing and on every legacy plaintext-token migration. The routes that
    # carry it in the *first* segment instead (_SECRET_KEY_FIRST_ROUTES) are
    # already safe here, since that is the segment this drops anyway.
    if endpoint.endswith(_SECRET_KEY_SECOND_ROUTES):
        endpoint = endpoint.rsplit("/", 1)[-1]
    return "/" + endpoint if endpoint else "/api/"


def redact_token(token: str) -> str:
    """A log-safe label for a WPPConnect token: session name, secret masked.

    Tokens are "<session>:<secret>". The session name alone is enough to
    trace which session a log line refers to; the secret half is the same
    credential Fernet-protects at rest and must never reach a log file.
    """
    if not token:
        return ""
    session_name, sep, _secret = token.partition(":")
    return f"{session_name}:***" if sep else "***"


# The same "/api/<session>:<secret>/" shape redact_api_url() splits on, matched
# by regex because here it is embedded in prose rather than being the whole
# string: requests and urllib3 copy the failed URL verbatim into their own
# exception messages ("Max retries exceeded with url: /api/<session>:<secret>/
# close-session"), so logging that exception publishes the credential exactly
# the way logging the URL would.
#
# The secret half is consumed as [^/\s'"),] — i.e. "up to the next slash". That
# is only safe because the token is a bcrypt hash that encryptController.ts
# already rewrites (`hash.replace(/\//g, '_').replace(/\+/g, '-')`) before
# handing it over, so it can never itself contain a slash. If that rewrite ever
# went away the match would stop mid-secret and publish the tail — and nothing
# would catch it: that controller is upstream-only, with no copy in
# client/api_patches/, so a re-clone that changed it is invisible to the suite.
_URL_CREDENTIAL = re.compile(r"(/api/)([^/\s:]+):[^/\s'\"),]+")

# The global secret key in either of its two path positions — see the route
# tuples above for why both exist and why they are enumerated.
_URL_SECRET_KEY_FIRST = re.compile(
    r"(/api/)[^/\s]+(/(?:%s)\b)"
    % "|".join(re.escape(r.lstrip("/")) for r in _SECRET_KEY_FIRST_ROUTES)
)
_URL_SECRET_KEY_SECOND = re.compile(
    r"(/api/[^/\s]+/)[^/\s]+(/(?:%s)\b)"
    % "|".join(re.escape(r.lstrip("/")) for r in _SECRET_KEY_SECOND_ROUTES)
)


def redact_credentials(text: str) -> str:
    """Mask WPPConnect credentials appearing in *URL form* inside `text`.

    Covers what actually leaks in practice: the session token and the global
    secret key as they sit in a request path, which is how requests/urllib3
    quote a failed call back at us. Deliberately not a general secret
    scrubber — a credential reaching a log any *other* way is not caught here
    and needs its own fix at its own call site. Specifically NOT covered,
    all verified: an `Authorization: Bearer <session>:<secret>` header, a
    `?token=<session>:<secret>` query parameter, and any path whose literal
    is not lowercase `/api/` (the match is case-sensitive).

    Keeps the session name the way redact_token() does, so the line stays
    traceable. Idempotent — an already-masked string comes back unchanged.
    """
    if not text:
        return ""
    masked = _URL_CREDENTIAL.sub(r"\1\2:***", text)
    masked = _URL_SECRET_KEY_FIRST.sub(r"\1***\2", masked)
    return _URL_SECRET_KEY_SECOND.sub(r"\1***\2", masked)


def redact_api_error(exc: BaseException) -> str:
    """A log-safe rendering of an exception raised by a call to the API.

    The type name is what actually separates the failures worth telling apart
    here (ConnectTimeout vs ConnectionError vs ReadTimeout); the message keeps
    the remaining detail minus the credential.
    """
    return f"{type(exc).__name__}: {redact_credentials(str(exc))}"


def _scrub_exception_args(exc: BaseException, _depth: int = 0) -> None:
    """Mask the credential inside an exception's own message, in place.

    api_request() re-raises whatever requests raised, and ~50 call sites in
    main.py and connect.py then log that exception — message_queue even puts
    str(exc) into a user-facing "message failed" dialog. Scrubbing it once
    here, at the only door to the API, is what keeps all of them safe instead
    of every call site having to remember.

    The exception object itself is kept and only str args are rewritten, so its
    type, its .request/.response attributes and any isinstance-based handling
    survive untouched — _classify_send_exception() in main.py still sees a
    Timeout/ConnectionError and still answers ambiguous, which is the invariant
    that keeps an ambiguous send from being retried into a duplicate. requests
    nests the real message one exception deep (ConnectionError(MaxRetryError(
    ...))), hence the recursion.

    What this does NOT reach: `exc.request.url` still holds the raw URL, token
    and all. Nothing logs it today, and rewriting a PreparedRequest would be a
    far bigger promise than masking a message — so if you ever add a log line
    that touches exc.request or exc.response.url, redact it yourself.
    """
    if _depth > 3:
        return
    scrubbed = []
    for arg in exc.args:
        if isinstance(arg, str):
            scrubbed.append(redact_credentials(arg))
        else:
            if isinstance(arg, BaseException):
                _scrub_exception_args(arg, _depth + 1)
            scrubbed.append(arg)
    # Left strictly alone when nothing matched, so an exception that never
    # carried a credential comes out bit-identical to the one requests raised.
    if scrubbed == list(exc.args):
        return
    try:
        exc.args = tuple(scrubbed)
    except Exception:
        # Only reachable for an exotic exception type with a read-only args.
        # A scrub that raised here would replace the very failure the caller
        # is about to classify, which is far worse than an unmasked message —
        # but failing open *and* silently is how a leak goes unnoticed, so say
        # so. Never the exception text itself: that is the unmasked thing.
        #
        # WARNING, not DEBUG: main.py sets the root logger to INFO, so a DEBUG
        # breadcrumb here would never reach log.log — the one file that would
        # tell whoever reads a leaked token afterwards that the scrub had not
        # run at all.
        logging.warning("[api] could not scrub %s args", type(exc).__name__)


def api_headers(token: str, *, json_body: bool = True,
                request_id: str = "") -> dict:
    """The headers every call to the Node API should carry."""
    headers = {
        "Authorization": f"Bearer {token}",
        _REQUEST_ID_HEADER: request_id or new_request_id(),
    }
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


# Ceiling for the second attempt after a stale keep-alive socket. See the
# retry's own comment for why it is not simply the caller's timeout.
_STALE_RETRY_TIMEOUT = 2.0


def api_request(method: str, url: str, *, token: str = "", request_id: str = "",
                timeout: float = 30, session: requests.Session = None,
                retry_stale_socket: bool = False,
                **kwargs) -> requests.Response:
    """Perform one call to the Node API, logged and correlated.

    Raises whatever requests raises — the caller's own error handling stays in
    charge, and every existing call site already has one.
    """
    request_id = request_id or new_request_id()
    headers = dict(kwargs.pop("headers", None) or {})
    if token and "Authorization" not in headers:
        headers["Authorization"] = f"Bearer {token}"
    headers.setdefault(_REQUEST_ID_HEADER, request_id)

    label = redact_api_url(url)
    started = time.monotonic()
    # Dispatched as requests.get/requests.post rather than requests.request on
    # purpose: that is the seam the existing suite already patches
    # (monkeypatch.setattr(main.requests, "post", ...)), and routing through
    # .request() instead would have quietly slipped past every one of those
    # stubs — turning unit tests into real HTTP calls against a local server
    # that may or may not be running. Caught exactly that way: the suite went
    # from 24s to 177s and 21 tests started failing on timeouts.
    caller = getattr(session or requests, method.lower())
    try:
        response = caller(url, headers=headers, timeout=timeout, **kwargs)
    except Exception as exc:
        # `label` was already safe, but `exc` was not: requests carries the
        # full URL in its own message, so this line published the token it
        # exists to keep out of the log. Scrub before logging *and* before
        # re-raising, so the callers that log the exception are covered too.
        _scrub_exception_args(exc)

        # Retry a stale keep-alive socket ONCE — but only for a method that is
        # safe to repeat, or when the caller explicitly guarantees that its
        # operation is idempotent (status reaction set/remove is one).
        #
        # A connection dropped before any response is ambiguous by nature: the
        # server may have processed the request and lost the socket while
        # answering. For a POST that means WhatsApp Web can already hold the
        # message in its own outbox, so resending here delivers it twice. That
        # is not hypothetical — CLAUDE.md records it as shipped and fixed once,
        # and MessageQueue._classify_send_exception() is where the decision
        # belongs precisely because it is the layer that knows a send from a
        # poll. Retrying down here hides the failure from it entirely.
        #
        # It also has to stay cheap: _stop_wpp_server() polls /status-session
        # inside a 4s total budget during a Windows shutdown, and the Node
        # tearing down keep-alive connections is exactly what produces these
        # errors — a blanket retry doubles every poll's worst case inside that
        # budget, which is what keeps taskkill off a half-written profile.
        is_stale_socket = (
            (method.lower() in ("get", "head") or retry_stale_socket)
            and (time.monotonic() - started) < 2.0
            and any(err in str(exc) for err in (
                "RemoteDisconnected", "Connection aborted", "ConnectionResetError",
                "BadStatusLine", "Remote end closed connection"
            ))
        )
        if is_stale_socket:
            logging.info(
                "[api] rid=%s %s %s stale socket reset after %.0fms (%s) — "
                "retrying once on fresh connection",
                request_id, method.upper(), label,
                (time.monotonic() - started) * 1000, redact_api_error(exc),
            )
            try:
                # Same seam as the first attempt: reusing `caller` would drop a
                # caller-supplied session (and its adapters, and the stubs the
                # suite patches onto it) on the retry only.
                #
                # Capped, because the retry must not double a caller's budget.
                # _wait_for_session_flushed() polls /status-session with
                # timeout=5 inside a _WINDOWS_SHUTDOWN_BUDGET of 4s, and the
                # Node tearing down keep-alives is exactly what produces the
                # errors retried here — so an uncapped retry could sit in a
                # single call for 5s past a 4s deadline, which is the overrun
                # that budget exists to prevent (taskkill landing mid-leveldb-
                # write). The first attempt already proved the socket answers
                # fast or not at all: it only qualifies as stale after failing
                # in under 2s.
                response = caller(
                    url, headers=headers, timeout=min(timeout, _STALE_RETRY_TIMEOUT),
                    **kwargs,
                )
            except Exception as retry_exc:
                _scrub_exception_args(retry_exc)
                logging.warning(
                    "[api] rid=%s %s %s retry failed after %.0fms: %s",
                    request_id, method.upper(), label,
                    (time.monotonic() - started) * 1000, redact_api_error(retry_exc),
                )
                raise
        else:
            logging.warning(
                "[api] rid=%s %s %s failed after %.0fms: %s",
                request_id, method.upper(), label,
                (time.monotonic() - started) * 1000, redact_api_error(exc),
            )
            raise
    elapsed = time.monotonic() - started
    log = logging.warning if elapsed >= SLOW_REQUEST_SECONDS else logging.info
    log(
        "[api] rid=%s %s %s -> %s in %.0fms",
        request_id, method.upper(), label, response.status_code, elapsed * 1000,
    )
    return response


def api_get(url: str, **kwargs) -> requests.Response:
    return api_request("GET", url, **kwargs)


def api_post(url: str, **kwargs) -> requests.Response:
    return api_request("POST", url, **kwargs)
