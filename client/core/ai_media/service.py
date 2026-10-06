"""Two running jobs maximum; deadline/cancellation never publish stale results."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time

import requests
from urllib3.util import Timeout
from core.ai_credentials import CredentialError
from core.bounded_http import BodyTooLarge, HTTPError, TimeoutError, read_bounded

from .config import MAX_RESPONSE_BYTES, PROVIDERS, valid_model
from .errors import DescriptionError, ErrorKey, status_error
from .providers import build_request, parse_answer
from .diagnostics import record

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
                   token, session_factory=requests.Session):
    request = build_request(provider, model, key, media, history, question, instructions, profile)
    token.check()
    record("request_started")
    deadline_timer = threading.Timer(max(0, token.deadline - token.clock()), token.expire)
    deadline_timer.daemon = True
    deadline_timer.start()
    try:
        with session_factory() as session:
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
                    raise status_error(response.status_code)
                data = read_bounded(response, MAX_RESPONSE_BYTES, token.check)
                record("response_read")
                token.check()
                answer = parse_answer(provider, json.loads(data), media.kind)
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


def probe_request(provider, model, key):
    """GET that proves the key (and, where the API allows, the model) works
    without sending media or generating a paid answer."""
    if provider not in PROVIDERS or not valid_model(model):
        raise DescriptionError("request")
    if not key:
        raise DescriptionError("credentials")
    if provider == "openai":
        return f"https://api.openai.com/v1/models/{model}", {"Authorization": f"Bearer {key}"}
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


def probe_connection(provider, model, key, token, session_factory=requests.Session):
    """Check access without sending media or generating a paid answer."""
    url, headers = probe_request(provider, model, key)
    token.check()
    try:
        with session_factory() as session:
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


#: Seconds one provider may take, by kind: a converted PDF or a long video
#: legitimately generates for much longer than a one-paragraph description.
ATTEMPT_SECONDS = {"image": 60, "sticker": 60, "video": 120, "audio": 90, "pdf": 150}
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

    def __init__(self, kind="image", clock=time.monotonic):
        self.kind = kind
        self.clock = clock
        self.attempt_seconds = ATTEMPT_SECONDS[kind]
        self.total_seconds = self.attempt_seconds * BUDGET_ATTEMPTS
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

    def attempt(self):
        """Token for the next provider; never outlives the overall budget."""
        self.check()
        remaining = self.deadline - self.clock()
        token = RequestToken(min(self.attempt_seconds, remaining), clock=self.clock)
        with self._lock:
            self._current = token
        if self.cancelled.is_set():
            token.cancel()
        return token


#: Categories that mean "this provider failed", so the next one is tried.
#: A local output limit also ends the action: do not spend another provider's
#: quota automatically after receiving a result that cannot be shown in full.
#: The other terminal errors include bad media, a cancel and the overall deadline.
_NEXT_PROVIDER = frozenset({"authentication", "quota", "server", "request", "refusal",
                            "response", "network", "timeout"})


def run_chain(operation, providers, models, key_for, media, history, question, instructions,
              profile, session_factory=requests.Session):
    """Try ``providers`` in order; return ``(answer, provider)`` of the first
    that answers. Raises ``ChainFailed`` with every attempt when none does."""
    attempts = []
    for provider in providers:
        operation.check()
        record("attempt_started")
        try:
            try:
                key = key_for(provider)
            except CredentialError:
                raise DescriptionError("credentials") from None
            if not key:
                raise DescriptionError("credentials")
            token = operation.attempt()
            answer = request_answer(provider, models[provider], key, media, history, question,
                                    instructions, profile, token, session_factory=session_factory)
            return answer, provider
        except DescriptionError as exc:
            operation.check()  # the whole action was cancelled or ran out of time
            if exc.category not in _NEXT_PROVIDER and exc.category != "credentials":
                raise
            attempts.append((provider, exc.category))
    raise ChainFailed(attempts)
