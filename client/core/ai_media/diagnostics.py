"""Allowlisted operational stages only: never content, identifiers or keys."""
import logging


LOGGER = logging.getLogger("winzapp.ai_media")
_STAGES = frozenset({
    "worker_started", "worker_finished", "worker_dispatched", "worker_dispatch_failed",
    "media_load_started", "media_ready", "attempt_started", "request_started", "response_headers",
    "response_read", "response_parsed", "ui_start", "ui_waiting", "ui_completed",
    "ui_completion_ignored", "ui_error", "ui_watchdog", "ui_cancel", "ui_close",
    "ui_close_finished", "request_failed",
})
_CATEGORIES = frozenset({
    "authentication", "quota", "server", "request", "media_size", "media_format",
    "refusal", "response", "network", "timeout", "cancelled", "busy", "question",
    "limit", "output_limit", "media", "credentials", "providers",
})
_EXCEPTION_TYPES = frozenset({
    "TypeError", "ValueError", "AttributeError", "RuntimeError", "OSError",
    "TimeoutError", "ConnectionError", "UnicodeDecodeError", "Timeout",
    "ConnectTimeout", "ReadTimeout", "ReadTimeoutError", "ConnectTimeoutError",
})


def record(stage, *, status=None, generation=None, category=None, exception_type=None):
    if stage not in _STAGES:
        return
    fields = [stage]
    if type(status) is int and 100 <= status <= 599:
        fields.append(f"http={status}")
    if type(generation) is int and generation >= 0:
        fields.append(f"generation={generation}")
    if category in _CATEGORIES:
        fields.append(f"category={category}")
    if exception_type is not None:
        fields.append("exception=" + (exception_type if exception_type in _EXCEPTION_TYPES else "other"))
    LOGGER.info("%s", " ".join(fields))


def configure_demo_log(path):
    """Only the explicitly launched manual demo installs this private logger."""
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    LOGGER.addHandler(handler)
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False
