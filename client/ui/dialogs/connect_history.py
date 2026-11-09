"""How much history to bring when pairing, in the pairing dialog (Neonize).

The phone sends a linked device its history once, at pairing, sized by what
the device asks for; afterwards WhatsApp gives a linked device about a year
more at most, on demand. Reported: a chat's 2024 messages never reached
WinZapp, paired asking for 180 days, while TeleZapp, paired asking for
everything, showed them. So the dialog asks, as TeleZapp does.

The choice is stored in the account's settings (`connection.neonize_history_days`)
and sent with the pairing's start-session; changed while a QR code is on
screen, it goes to the bridge's `pairing-history`, which redoes the code so
the one the user scans carries it (neonize_bridge/pairing_history.py).
WPPConnect has no such choice: WhatsApp Web decides its own window.
"""

import logging
import threading

import wx

from core import connection_engine as engines

#: The days asked for, in the list's order; 0 is WhatsApp's own default
#: (recent messages only). Must match pairing_history.CHOICES of the bridge.
CHOICES = (0, 30, 180, 365, 3650)
LABEL_KEYS = {
    0: "pairing_history_recent",
    30: "pairing_history_30_days",
    180: "pairing_history_6_months",
    365: "pairing_history_1_year",
    3650: "pairing_history_all",
}
#: What the bridge asked for before there was a choice.
DEFAULT_DAYS = 180
SETTING = "neonize_history_days"


def stored_days(settings) -> int:
    """The choice kept in the settings, or DEFAULT_DAYS."""
    connection = settings.get("connection") if isinstance(settings, dict) else None
    value = connection.get(SETTING) if isinstance(connection, dict) else None
    return value if (isinstance(value, int) and not isinstance(value, bool)
                     and value in CHOICES) else DEFAULT_DAYS


def with_history(payload: dict, engine: str, settings) -> dict:
    """A start-session body with the history choice, for Neonize; the same
    body for any other engine."""
    if engine != engines.NEONIZE:
        return payload
    return {**payload, "historyDays": stored_days(settings)}


def build_row(parent, sizer, i18n, engine: str, settings):
    """The label and the list, for Neonize only; returns the list or None."""
    if engine != engines.NEONIZE:
        return None
    label = wx.StaticText(parent, label=i18n.t("pairing_history_label"))
    sizer.Add(label, 0, wx.LEFT | wx.TOP | wx.RIGHT, 10)
    choice = wx.Choice(parent, choices=[i18n.t(LABEL_KEYS[d]) for d in CHOICES],
                       name=i18n.t("pairing_history_label").replace("&", ""))
    choice.SetSelection(CHOICES.index(stored_days(settings)))
    sizer.Add(choice, 0, wx.EXPAND | wx.ALL, 10)
    return choice


def days_at(index) -> int:
    """The days of the list's `index` (a bad index reads as the default)."""
    return CHOICES[index] if isinstance(index, int) and 0 <= index < len(CHOICES) else DEFAULT_DAYS


def remember(main_window, days: int) -> bool:
    """Store the choice; True when it changed."""
    connection = main_window.settings.setdefault("connection", {})
    if connection.get(SETTING) == days:
        return False
    connection[SETTING] = days
    main_window.save_settings()
    return True


def send_to_bridge(main_window, days: int, post) -> None:
    """Tell the bridge, off the wx thread: a QR code on screen is redone with
    the choice. `post(url, json=, headers=, timeout=)` is api_post."""
    token = getattr(main_window, "token", "") or ""
    if not token:
        return          # no session yet: start-session will carry it
    url = f"{main_window.wpp_server}:{main_window.wpp_port}/api/{token}/pairing-history"
    headers = {"Authorization": f"Bearer {getattr(main_window, 'wpp_api_key', '') or token}",
               "Content-Type": "application/json"}

    def _send():
        try:
            response = post(url, json={"days": days}, headers=headers, timeout=60)
            if response.status_code not in (200, 201):
                logging.warning("[pairing-history] bridge answered HTTP %s", response.status_code)
        except Exception as exc:
            logging.warning("[pairing-history] could not reach the bridge: %s", type(exc).__name__)

    threading.Thread(target=_send, daemon=True, name="pairing-history").start()
