"""Two running jobs maximum; deadline/cancellation never publish stale results."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time

import requests
from urllib3.util import Timeout
from core import tls_trust
from core.ai_credentials import CredentialError
from core.bounded_http import BodyTooLarge, HTTPError, TimeoutError, read_bounded

from .config import MAX_RESPONSE_BYTES, PROVIDERS, is_local, valid_model
from .errors import DescriptionError, ErrorKey, status_error
from .providers import build_request, parse_answer
from .streaming import read_stream, streaming_request, supports_streaming
from .diagnostics import record
from . import local_url, usage

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ai-media")
_slots = threading.BoundedSemaphore(2)


def submit(work, complete):
    """No unbounded queue; completion may run on a worker (UI must CallAfter)."""
    if not _slots.acquire(blocking=False):
        raise DescriptionError("busy")
    def run():
        try:
            record("worker_started")
            try:
                result, error = work(), None
            except DescriptionError as exc:
                result, error = None, exc.key
                record("worker_finished", category=exc.category)
            except Exception as exc:
                # Exception messages can include credentials or private text.
                result, error = None, "ai_error_response"
                record("worker_finished", category="response", exception_type=type(exc).__name__)
            else:
                record("worker_finished")
            try:
                complete(result, error)
                record("worker_dispatched")
            except Exception as exc:
                record("worker_dispatch_failed", exception_type=type(exc).__name__)
                raise
        finally:
            _slots.release()
    try:
        return _executor.submit(run)
    except Exception:
        _slots.release()
        raise


class RequestToken:
    def __init__(self, seconds=60, clock=time.monotonic):
        self.cancelled = threading.Event()
        self.clock = clock
        self.deadline = clock() + seconds
        self.response = None
        self._abort_started = False
        self._lock = threading.Lock()
        self.timed_out = False

    def cancel(self):
        self.cancelled.set()
        self._abort()

    def expire(self):
        self.timed_out = True
        self.cancel()

    def attach(self, response):
        self.response = response
        if self.cancelled.is_set():
            self._abort()

    def _abort(self):
        # Shutdown is best effort and off the GUI thread. Response.close()
        # belongs to the reader's context manager, never this other thread.
        with self._lock:
            response = self.response
            if response is None or self._abort_started:
                return
            self._abort_started = True
        def abort():
            try:
                response.raw.shutdown()
            except Exception:
                pass
        threading.Thread(target=abort, daemon=True, name="ai-request-cancel").start()

    def check(self):
        if self.timed_out or self.clock() >= self.deadline:
            raise DescriptionError("timeout")
        if self.cancelled.is_set():
            raise DescriptionError("cancelled")


def request_answer(provider, model, key, media, history, question, instructions, profile,
                   token, session_factory=tls_trust.create_session, on_partial=None,
                   base_url=""):
    request = build_request(provider, model, key, media, history, question, instructions, profile,
                            base_url)
    if on_partial is not None and supports_streaming(provider, media.kind):
        # Same request, asked as a stream; the answer is the same text, with
        # what has arrived so far offered to ``on_partial`` on the way. Any
        # other provider or kind takes the plain path below.
        request = streaming_request(provider, request)
        return post_answer(request, None, token, session_factory,
                           stream=lambda response: read_stream(
                               response, provider, media.kind, token.check, on_partial))
    return post_answer(request, lambda body: parse_answer(provider, body, media.kind),
                       token, session_factory)


def post_answer(request, parse, token, session_factory=tls_trust.create_session, stream=None):
    """Send one request using the shared AI deadline and response limits.
    ``stream(response)`` reads a streamed answer instead of the bounded body
    and ``parse``."""
    token.check()
    record("request_started")
    deadline_timer = threading.Timer(max(0, token.deadline - token.clock()), token.expire)
    deadline_timer.daemon = True
    deadline_timer.start()
    try:
        with session_factory() as session:
            if request.loopback:
                session.trust_env = False  # never through a proxy: it stays on this computer
            # No redirects (Authorization must never reach another host), TLS
            # verification on, no retry adapters; auth headers not query strings.
            remaining = token.deadline - token.clock()
            if remaining <= 0:
                raise DescriptionError("timeout")
            # Generation is not a quick metadata GET: allow the provider to
            # use the remaining 60s operation budget, deducting connect time
            # from the header wait. The token timer/GUI watchdog still bound
            # body reads and late results; no retry or longer global deadline.
            timeout = Timeout(total=remaining, connect=min(8, remaining))
            with session.post(request.url, headers=request.headers, json=request.json,
                              data=request.data, files=request.files, timeout=timeout,
                              allow_redirects=False, stream=True) as response:
                token.attach(response)
                record("response_headers", status=response.status_code)
                token.check()
                if response.status_code != 200:
                    headers = getattr(response, "headers", None) or {}
                    raise status_error(response.status_code, headers.get("Retry-After"))
                if stream is not None:
                    answer = stream(response)
                    record("response_read")
                    token.check()
                    record("response_parsed")
                    return answer
                data = read_bounded(response, MAX_RESPONSE_BYTES, token.check)
                record("response_read")
                token.check()
                answer = parse(json.loads(data))
                record("response_parsed")
                return answer
    except DescriptionError:
        raise
    except (requests.Timeout, TimeoutError) as exc:
        record("request_failed", category="timeout", exception_type=type(exc).__name__)
        raise DescriptionError("timeout") from None
    except (requests.RequestException, HTTPError) as exc:
        record("request_failed", category="network", exception_type=type(exc).__name__)
        token.check()
        raise DescriptionError("network") from None
    except (ValueError, TypeError, BodyTooLarge):
        raise DescriptionError("response") from None
    finally:
        deadline_timer.cancel()
        token.response = None


def probe_request(provider, model, key, base_url=""):
    """GET that proves the key (and, where the API allows, the model) works
    without sending media or generating a paid answer. A local server has no
    key and may have no model yet: its probe is the list of installed models."""
    if provider in PROVIDERS and PROVIDERS[provider].local:
        return f"{local_url.normalize_url(base_url)}/models", {}
    if provider not in PROVIDERS or not valid_model(model):
        raise DescriptionError("request")
    if not key:
        raise DescriptionError("credentials")
    if provider == "openai":
        return f"https://api.openai.com/v1/models/{model}", {"Authorization": f"Bearer {key}"}
    if provider == "mistral":
        return f"https://api.mistral.ai/v1/models/{model}", {"Authorization": f"Bearer {key}"}
    if provider == "gemini":
        return (f"https://generativelanguage.googleapis.com/v1beta/models/{model}",
                {"x-goog-api-key": key})
    if provider == "claude":
        return (f"https://api.anthropic.com/v1/models/{model}",
                {"x-api-key": key, "anthropic-version": "2023-06-01"})
    # Model ids of these two contain a slash, so the key alone is checked.
    if provider == "groq":
        return "https://api.groq.com/openai/v1/models", {"Authorization": f"Bearer {key}"}
    return "https://openrouter.ai/api/v1/key", {"Authorization": f"Bearer {key}"}


def probe_connection(provider, model, key, token, session_factory=tls_trust.create_session,
                     base_url=""):
    """Check access without sending media or generating a paid answer."""
    url, headers = probe_request(provider, model, key, base_url)
    token.check()
    try:
        with session_factory() as session:
            if local_url.is_loopback_url(url):
                session.trust_env = False  # a local server is not reached through a proxy
            with session.get(url, headers=headers, timeout=(8, 15), allow_redirects=False,
                             stream=True) as response:
                token.check()
                if response.status_code != 200:
                    raise status_error(response.status_code)
                return True
    except requests.Timeout:
        raise DescriptionError("timeout") from None
    except requests.RequestException:
        raise DescriptionError("network") from None


#: Seconds one provider may take, by kind: a converted PDF
#: legitimately generates for much longer than a one-paragraph description.
ATTEMPT_SECONDS = {"image": 60, "sticker": 60, "video": 60, "audio": 90, "pdf": 150,
                     "text": 60, "writing": 60}
#: A model the person runs may need to be loaded into memory first and answers
#: slowly on a computer without a graphics card: a local attempt gets longer
#: than an online one. Only an attempt at a local provider gets this; the
#: action's total grows by the difference, so the online providers behind it
#: keep the time they always had (``Operation(local=True)``).
LOCAL_ATTEMPT_SECONDS = {"image": 240, "sticker": 240, "video": 240, "text": 240,
                         "writing": 120}
#: One user action may use this many attempts' worth of time in total.
BUDGET_ATTEMPTS = 2.5


class ChainFailed(DescriptionError):
    """Every provider in the chain failed. ``attempts`` lists (provider, category)
    so the window can say which one failed how."""

    def __init__(self, attempts):
        self.attempts = tuple(attempts)
        super().__init__(self.attempts[-1][1] if self.attempts else "providers")

    @property
    def key(self):
        key = ErrorKey(str(self))
        key.attempts = self.attempts
        return key


class Operation:
    """One user action: one cancel, one overall deadline, one fresh
    ``RequestToken`` per provider attempt. Duck-types the token the UI and the
    loaders already use (``check``, ``cancel``)."""

    def __init__(self, kind="image", clock=time.monotonic, local=False, steps=1):
        self.kind = kind
        self.clock = clock
        self.attempt_seconds = ATTEMPT_SECONDS[kind]
        self.total_seconds = self.attempt_seconds * BUDGET_ATTEMPTS
        # ``local``: a local provider is among those that may be tried.
        self.local_seconds = LOCAL_ATTEMPT_SECONDS.get(kind, 0) if local else 0
        self.total_seconds += max(0, self.local_seconds - self.attempt_seconds)
        # ``steps``: a hierarchical summary (summary_parts.py) makes this many
        # requests in a row, each with a budget of its own (a child Operation);
        # this one only has to outlast all of them, and is what the window's
        # watchdog waits for.
        self.total_seconds *= max(1, steps)
        self.deadline = clock() + self.total_seconds
        self.cancelled = threading.Event()
        self.timed_out = False
        self._current = None
        self._lock = threading.Lock()

    def check(self):
        if self.timed_out or self.clock() >= self.deadline:
            raise DescriptionError("timeout")
        if self.cancelled.is_set():
            raise DescriptionError("cancelled")

    def expire(self):
        self.timed_out = True
        self.cancel()

    def cancel(self):
        self.cancelled.set()
        with self._lock:
            current = self._current
        if current is not None:
            current.cancel()

    def adopt(self, child):
        """Make ``child`` (an Operation of one step) what a cancel of this one
        reaches. A cancel that came before is passed on at once."""
        with self._lock:
            self._current = child
        if self.cancelled.is_set():
            child.cancel()

    def attempt(self, local=False):
        """Token for the next provider; never outlives the overall budget."""
        self.check()
        remaining = self.deadline - self.clock()
        seconds = max(self.local_seconds, self.attempt_seconds) if local else self.attempt_seconds
        token = RequestToken(min(seconds, remaining), clock=self.clock)
        with self._lock:
            self._current = token
        if self.cancelled.is_set():
            token.cancel()
        return token


#: Categories that mean "this provider failed", so the next one is tried.
#: A local output limit also ends the action: do not spend another provider's
#: quota automatically after receiving a result that cannot be shown in full.
#: The other terminal errors include bad media, a cancel and the overall deadline.
_NEXT_PROVIDER = frozenset({"authentication", "quota", "credits", "server", "request", "refusal",
                            "response", "network", "timeout", "local_url"})


#: A provider's ``Retry-After`` is honoured, once, only up to this many seconds.
MAX_RETRY_AFTER = 5.0
#: ...and only when that much of the action's budget would still be left for
#: the second attempt after the pause.
MIN_RETRY_SECONDS = 10.0


def retry_pause(operation, error):
    """Seconds to wait before the one retry of ``error``'s provider, or None
    when it is not worth it: no (or a long) ``Retry-After``, or too little of
    the action's time left for a second attempt."""
    pause = getattr(error, "retry_after", None)
    if pause is None or pause > MAX_RETRY_AFTER:
        return None
    if operation.deadline - operation.clock() < pause + MIN_RETRY_SECONDS:
        return None
    return pause


def run_chain(operation, providers, models, key_for, media, history, question, instructions,
              profile, session_factory=tls_trust.create_session, usage_sink=None,
              on_partial=None, endpoints=None):
    """Try ``providers`` in order; return ``(answer, provider)`` of the first
    that answers. Raises ``ChainFailed`` with every attempt when none does.

    ``on_partial(text, final)`` (optional, called on the worker thread) hears
    the text a streaming provider has sent so far; the returned answer is still
    the only complete one. When an attempt fails after text was offered,
    ``on_partial("", False)`` tells the listener to drop it before the next
    attempt starts.

    A 429 or 503 that carries a short ``Retry-After`` gets one more attempt at
    the same provider after that pause (never more than one); the pause ends at
    once when the action is cancelled. ``usage_sink(provider, category)`` hears
    each provider's final outcome (``core/ai_media/usage.py``).

    A local provider (Ollama, LM Studio) holds no key: ``key_for`` is never
    asked for it, and its address comes from ``endpoints`` (``config.endpoints``)."""
    attempts = []
    offered = []

    def relay(text, final=False):
        offered.append(True)
        on_partial(text, final)

    for provider in providers:
        retried = False
        while True:
            operation.check()
            record("attempt_started")
            try:
                local = is_local(provider)
                if local:
                    key = ""
                else:
                    try:
                        key = key_for(provider)
                    except CredentialError:
                        raise DescriptionError("credentials") from None
                    if not key:
                        raise DescriptionError("credentials")
                token = operation.attempt(local)
                streaming = {} if on_partial is None else {"on_partial": relay}
                answer = request_answer(provider, models[provider], key, media, history, question,
                                        instructions, profile, token,
                                        session_factory=session_factory,
                                        base_url=(endpoints or {}).get(provider, ""), **streaming)
                usage.note(usage_sink, provider, usage.OK)
                return answer, provider
            except DescriptionError as exc:
                if offered:
                    offered.clear()
                    on_partial("", False)
                operation.check()  # the whole action was cancelled or ran out of time
                if exc.category not in _NEXT_PROVIDER and exc.category != "credentials":
                    raise
                pause = None if retried else retry_pause(operation, exc)
                if pause is not None:
                    retried = True
                    record("retry_waiting", category=exc.category)
                    operation.cancelled.wait(pause)
                    continue
                attempts.append((provider, exc.category))
                usage.note(usage_sink, provider, exc.category)
                break
    raise ChainFailed(attempts)
