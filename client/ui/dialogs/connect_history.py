"""How much history to bring when pairing, in the pairing dialog (Neonize).

The phone sends a linked device its history once, at pairing, sized by what
the device asks for; afterwards WhatsApp gives a linked device about a year
more at most, on demand. Reported: a chat's 2024 messages never reached
WinZapp, paired asking for 180 days, while TeleZapp, paired asking for
everything, showed them. So the dialog asks, as TeleZapp does: ready-made
periods, or a custom number of days, months or years typed in a field that
appears under the list.

The choice is stored in the account's settings (`connection.neonize_history_days`,
and `connection.neonize_history_custom` for a custom one, to show it again)
and sent with the pairing's start-session; changed while a QR code is on
screen, it goes to the bridge's `pairing-history`, which redoes the code so
the one the user scans carries it (neonize_bridge/pairing_history.py).
WPPConnect has no such choice: WhatsApp Web decides its own window.
"""

import logging
import threading

import wx

from core import connection_engine as engines

#: The ready-made periods, in the list's order; 0 is WhatsApp's own default
#: (recent messages only). Must match pairing_history.CHOICES of the bridge.
CHOICES = (0, 30, 180, 365, 3650)
LABEL_KEYS = {
    0: "pairing_history_recent",
    30: "pairing_history_30_days",
    180: "pairing_history_6_months",
    365: "pairing_history_1_year",
    3650: "pairing_history_all",
}
#: The custom entries, after the ready-made ones: unit -> (days per unit,
#: largest amount). A month counts 31 days and a year 366, so the period
#: asked for never falls short of the one typed. Twenty years at most, the
#: bridge's pairing_history.MAX_DAYS.
UNITS = {"days": (1, 7320), "months": (31, 236), "years": (366, 20)}
CUSTOM_ORDER = ("days", "months", "years")
CUSTOM_LABEL_KEYS = {"days": "pairing_history_custom_days",
                     "months": "pairing_history_custom_months",
                     "years": "pairing_history_custom_years"}
AMOUNT_LABEL_KEYS = {"days": "pairing_history_amount_days",
                     "months": "pairing_history_amount_months",
                     "years": "pairing_history_amount_years"}
MAX_DAYS = 7320
#: What the bridge asked for before there was a choice.
DEFAULT_DAYS = 180
SETTING = "neonize_history_days"
CUSTOM_SETTING = "neonize_history_custom"


def _connection(settings):
    connection = settings.get("connection") if isinstance(settings, dict) else None
    return connection if isinstance(connection, dict) else {}


def _whole(value):
    return isinstance(value, int) and not isinstance(value, bool)


def stored_days(settings) -> int:
    """The days kept in the settings, or DEFAULT_DAYS."""
    value = _connection(settings).get(SETTING)
    return value if _whole(value) and 0 <= value <= MAX_DAYS else DEFAULT_DAYS


def stored_custom(settings):
    """(unit, amount) of a custom choice kept in the settings, or None."""
    custom = _connection(settings).get(CUSTOM_SETTING)
    if not isinstance(custom, dict):
        return None
    unit, amount = custom.get("unit"), custom.get("amount")
    if unit not in UNITS or not _whole(amount) or not 0 < amount <= UNITS[unit][1]:
        return None
    return unit, amount


def entries() -> list:
    """The list's entries, in order: ("preset", days) then ("custom", unit)."""
    return [("preset", d) for d in CHOICES] + [("custom", u) for u in CUSTOM_ORDER]


def selection_of(settings) -> int:
    """The list's entry for what is stored."""
    custom = stored_custom(settings)
    if custom is not None and stored_days(settings) == days_for(*custom):
        return entries().index(("custom", custom[0]))
    days = stored_days(settings)
    return CHOICES.index(days if days in CHOICES else DEFAULT_DAYS)


def days_for(unit: str, amount: int) -> int:
    return UNITS[unit][0] * amount


def parse_amount(text, unit):
    """(amount, None) for a valid amount of `unit` typed in the field, or
    (None, (i18n key, format fields)) saying what is wrong with it."""
    text = str(text or "").strip()
    if not text.isdigit() or not text.isascii():
        return None, ("pairing_history_amount_not_number", {})
    amount = int(text)
    if amount == 0:
        return None, ("pairing_history_amount_zero", {})
    largest = UNITS[unit][1]
    if amount > largest:
        return None, (f"pairing_history_amount_too_big_{unit}", {"max": largest})
    return amount, None


def with_history(payload: dict, engine: str, settings) -> dict:
    """A start-session body with the history choice, for Neonize; the same
    body for any other engine."""
    if engine != engines.NEONIZE:
        return payload
    return {**payload, "historyDays": stored_days(settings)}


def remember(main_window, days: int, custom=None) -> bool:
    """Store the choice (`custom`: (unit, amount) or None); True when the days
    asked for changed."""
    connection = main_window.settings.setdefault("connection", {})
    changed = connection.get(SETTING) != days
    wanted = {"unit": custom[0], "amount": custom[1]} if custom else None
    if not changed and connection.get(CUSTOM_SETTING) == wanted:
        return False
    connection[SETTING] = days
    if wanted is None:
        connection.pop(CUSTOM_SETTING, None)
    else:
        connection[CUSTOM_SETTING] = wanted
    main_window.save_settings()
    return changed


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


class HistoryRow:
    """The list, and the amount field shown under it for a custom entry. A
    ready-made entry applies at once; a custom amount when the field is left
    or Enter is pressed, and only when valid (an invalid one is said and the
    previous choice stays)."""

    def __init__(self, parent, sizer, i18n, main_window, post):
        self._parent, self._i18n = parent, i18n
        self._main_window, self._post = main_window, post
        label = wx.StaticText(parent, label=i18n.t("pairing_history_label"))
        sizer.Add(label, 0, wx.LEFT | wx.TOP | wx.RIGHT, 10)
        names = ([i18n.t(LABEL_KEYS[d]) for d in CHOICES]
                 + [i18n.t(CUSTOM_LABEL_KEYS[u]) for u in CUSTOM_ORDER])
        self.choice = wx.Choice(parent, choices=names,
                                name=i18n.t("pairing_history_label").replace("&", ""))
        sizer.Add(self.choice, 0, wx.EXPAND | wx.ALL, 10)
        self.amount_label = wx.StaticText(parent, label=i18n.t(AMOUNT_LABEL_KEYS["days"]))
        sizer.Add(self.amount_label, 0, wx.LEFT | wx.RIGHT, 10)
        self.amount = wx.TextCtrl(parent, style=wx.TE_PROCESS_ENTER)
        sizer.Add(self.amount, 0, wx.EXPAND | wx.ALL, 10)
        settings = main_window.settings
        self.choice.SetSelection(selection_of(settings))
        custom = stored_custom(settings)
        if custom is not None:
            self.amount.SetValue(str(custom[1]))
        self._show_amount(self._unit())
        self.choice.Bind(wx.EVT_CHOICE, self._on_choice)
        self.amount.Bind(wx.EVT_TEXT_ENTER, self._on_amount_done)
        self.amount.Bind(wx.EVT_KILL_FOCUS, self._on_amount_left)

    @classmethod
    def build(cls, parent, sizer, i18n, engine, main_window, post):
        """The row for Neonize; None for any other engine."""
        if engine != engines.NEONIZE:
            return None
        return cls(parent, sizer, i18n, main_window, post)

    def _unit(self):
        index = self.choice.GetSelection()
        all_entries = entries()
        if 0 <= index < len(all_entries) and all_entries[index][0] == "custom":
            return all_entries[index][1]
        return None

    def _show_amount(self, unit):
        shown = unit is not None
        if shown:
            name = self._i18n.t(AMOUNT_LABEL_KEYS[unit])
            self.amount_label.SetLabel(name)
            self.amount.SetName(name.replace("&", ""))
        self.amount_label.Show(shown)
        self.amount.Show(shown)
        self._parent.Layout()

    def _apply(self, days, custom=None):
        if remember(self._main_window, days, custom):
            send_to_bridge(self._main_window, days, self._post)

    def _on_choice(self, event):
        unit = self._unit()
        self._show_amount(unit)
        if unit is None:
            self._apply(CHOICES[self.choice.GetSelection()])
        # a custom entry waits for its amount
        event.Skip()

    def _commit_amount(self):
        unit = self._unit()
        if unit is None or not self.amount.GetValue().strip():
            return
        amount, problem = parse_amount(self.amount.GetValue(), unit)
        if problem is not None:
            key, fields = problem
            self._main_window.output(self._i18n.t(key).format(**fields), interrupt=True)
            return
        self._apply(days_for(unit, amount), (unit, amount))

    def _on_amount_done(self, event):
        self._commit_amount()

    def _on_amount_left(self, event):
        self._commit_amount()
        event.Skip()
