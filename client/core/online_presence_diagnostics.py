"""Bounded, content-free evidence for account presence decisions and writes."""
from dataclasses import dataclass
import logging
import time


@dataclass(frozen=True)
class PresenceDecision:
    sequence: int
    epoch: int
    online: bool
    trace: bool


class PresenceDiagnostics:
    """Decisions are captured on the UI thread, never by the HTTP worker."""
    def __init__(self):
        self.sequence = self.epoch = 0
        self.connected = None
        self.last_signature = None
        self.last_logged = float("-inf")

    def decide(self, *, connected, token, shutdown, background, hidden,
               shown=None, iconized=None, active=None, away=False, now=None):
        now = time.monotonic() if now is None else now
        if connected != self.connected:
            self.epoch = self.epoch % 9_999_999 + 1
            self.connected = connected
        self.sequence = self.sequence % 9_999_999 + 1
        online = bool(connected and not background and not hidden and shown
                      and not iconized and active and not away)
        signature = (connected, token, shutdown, background, hidden, shown, iconized, active,
                     away)
        elapsed = now - self.last_logged
        trace = elapsed >= 60 or (signature != self.last_signature and elapsed >= 1)
        if trace:
            logging.info(
                "[presence-diag] decision seq=%d epoch=%d online=%s connected=%s "
                "token_present=%s shutdown=%s background=%s hidden=%s shown=%s "
                "iconized=%s active=%s away=%s",
                self.sequence, self.epoch, online, connected, token, shutdown,
                background, hidden, shown, iconized, active, away,
            )
            self.last_signature, self.last_logged = signature, now
        return PresenceDecision(self.sequence, self.epoch, online, trace)


def diagnostic_headers(decision):
    if decision is None:
        return {}
    return {"X-WinZapp-Presence-Sequence": str(decision.sequence),
            "X-WinZapp-Presence-Epoch": str(decision.epoch),
            "X-WinZapp-Presence-Trace": "1" if decision.trace else "0"}


def record_write(decision, *, response=None, elapsed=0, failure=False, skipped=False):
    """Project only fixed enums/booleans; never log response/error text."""
    if decision is None or not decision.trace:
        return
    status = getattr(response, "status_code", None)
    status = status if isinstance(status, int) and 100 <= status <= 599 else 0
    result = "skipped" if skipped else "transport_error" if failure else "unknown"
    native, runtime, available = "unknown", "unknown", "unknown"
    if response is not None:
        try:
            body = response.json()
            if isinstance(body, dict):
                result = "success" if body.get("status") == "success" else "api_error"
                diag = body.get("diagnostic")
                if isinstance(diag, dict):
                    value = diag.get("native")
                    if value in ("true", "false", "void", "other", "error"):
                        native = value
                    observed = diag.get("runtime")
                    if isinstance(observed, dict):
                        value = observed.get("status")
                        if value in ("observed", "unavailable", "timeout", "failed"):
                            runtime = value
                        value = observed.get("available")
                        if isinstance(value, bool):
                            available = value
        except Exception:
            result = "invalid_response"
        if status >= 400:
            result = "http_error"
    logging.info(
        "[presence-diag] write seq=%d epoch=%d online=%s result=%s http=%d "
        "ms=%d native=%s runtime=%s local_available=%s receiver_confirmed=false",
        decision.sequence, decision.epoch, decision.online, result, status,
        max(0, min(int(elapsed * 1000), 9_999_999)), native, runtime, available,
    )
