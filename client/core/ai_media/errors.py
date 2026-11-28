import re


class ErrorKey(str):
    """A failure's translation key. ``attempts`` is empty except after a chain
    of providers all failed: then it lists (provider, category) for each, so
    the window can say who failed how while every caller keeps treating the
    value as a plain key."""
    attempts = ()


class DescriptionError(RuntimeError):
    """Safe translation key only; no response bodies, keys or user content."""
    def __init__(self, category, retry_after=None):
        self.category = category
        #: Seconds the provider asked to wait (a ``Retry-After`` header on a
        #: 429/503), or None. Read by ``service.run_chain`` for its one retry.
        self.retry_after = retry_after
        super().__init__(f"ai_error_{category}")

    @property
    def key(self):
        return ErrorKey(str(self))


def parse_retry_after(value):
    """Seconds from a ``Retry-After`` header, or None. Only the delta-seconds
    form: an HTTP date needs the server's clock, and a pause that long is not
    a "short" one anyway."""
    if not isinstance(value, str) or not re.fullmatch(r"\d{1,6}(\.\d{1,3})?", value.strip()):
        return None
    return float(value)


def status_error(status, retry_after=None):
    """``retry_after`` (the raw header) is kept only for 429 and 503, the two
    answers that mean "try again shortly"."""
    if status in (429, 503):
        pause = parse_retry_after(retry_after)
        return DescriptionError("quota" if status == 429 else "server", pause)
    if status in (401, 403):
        return DescriptionError("authentication")
    if status == 402:
        # Payment Required: OpenRouter's answer to an account without the
        # credit a request needs. Read as "request" it said "check the model
        # identifier and settings", on a reported log with three 402s in a row.
        return DescriptionError("credits")
    if status >= 500:
        return DescriptionError("server")
    return DescriptionError("request")
