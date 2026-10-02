"""Two running jobs maximum; deadline/cancellation never publish stale results."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time

import requests
from urllib3.util import Timeout
from core.bounded_http import BodyTooLarge, HTTPError, TimeoutError, read_bounded

from .errors import DescriptionError, status_error
from .providers import build_request, parse_answer
from .diagnostics import record

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="photo-description")
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
                result, error = None, str(exc)
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
        threading.Thread(target=abort, daemon=True, name="photo-request-cancel").start()

    def check(self):
        if self.timed_out or self.clock() >= self.deadline:
            raise DescriptionError("timeout")
        if self.cancelled.is_set():
            raise DescriptionError("cancelled")


def request_answer(provider, model, key, image, history, question, instructions, profile,
                   token, session_factory=requests.Session):
    url, headers, body = build_request(provider, model, key, image, history, question, instructions, profile)
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
            with session.post(url, headers=headers, json=body, timeout=timeout,
                              allow_redirects=False, stream=True) as response:
                token.attach(response)
                record("response_headers", status=response.status_code)
                token.check()
                if response.status_code != 200:
                    raise status_error(response.status_code)
                data = read_bounded(response, 256 * 1024, token.check)
                record("response_read")
                token.check()
                answer = parse_answer(provider, json.loads(data))
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


def probe_connection(provider, model, key, token, session_factory=requests.Session):
    """Check model access without sending a photo or generating a paid answer."""
    from .config import valid_model
    if not valid_model(model) or provider not in ("openai", "gemini"):
        raise DescriptionError("request")
    if not key:
        raise DescriptionError("credentials")
    url = (f"https://api.openai.com/v1/models/{model}" if provider == "openai" else
           f"https://generativelanguage.googleapis.com/v1beta/models/{model}")
    headers = {"Authorization": f"Bearer {key}"} if provider == "openai" else {"x-goog-api-key": key}
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
