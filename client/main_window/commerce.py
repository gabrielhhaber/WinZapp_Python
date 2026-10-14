"""What the account may do with carts and orders, kept fresh in the background."""

import logging
import threading
import time

import wx

from core import commerce, read_back
from core.group_invitation import connection_identity
from core.utils import contact_dedup_key

#: Between two readings of the cart setting after changing it.
_CART_READ_PAUSE = 1.5

_REFRESH_SECONDS = 300


class CommerceMixin:
    """The account's commerce facts: whether it is a WhatsApp Business account
    and which chats are with a business. Read from the server, never guessed:
    until a reading arrives nothing commercial is offered."""

    def refresh_commerce_account(self, force=False):
        """Read the facts again off the main thread, at most once in a while
        unless *force*. Safe to call from anywhere, any number of times."""
        now = time.monotonic()
        with self.__dict__.setdefault("_commerce_lock", threading.Lock()):
            started = self.__dict__.get("_commerce_started", 0.0)
            if self.__dict__.get("_commerce_running") or (not force and started and now - started < _REFRESH_SECONDS):
                return
            self._commerce_running = True
            self._commerce_started = now
        connection = connection_identity(self)

        def run():
            account = None
            try:
                if all(connection):
                    account = commerce.read_account(connection)
            except Exception:
                logging.warning("[commerce] account_read_failed")
            finally:
                self._commerce_running = False
            if account is not None and connection_identity(self) == connection:
                self._commerce_account = account
                key = lambda jid: contact_dedup_key(self, jid)
                self._commerce_seller_keys = frozenset(key(jid) for jid in account.sellers)
            elif account is None:
                # Ask again soon rather than in five minutes.
                self._commerce_started = 0.0
        threading.Thread(target=run, daemon=True).start()

    def commerce_account(self):
        """The last reading, or None; asks for a fresh one when it is old."""
        self.refresh_commerce_account()
        return self.__dict__.get("_commerce_account")

    def is_business_account(self) -> bool:
        account = self.commerce_account()
        return bool(account and account.business)

    def is_seller_chat(self, jid: str) -> bool:
        """The chat is with a business, by any of the forms its id takes."""
        self.refresh_commerce_account()
        keys = self.__dict__.get("_commerce_seller_keys") or frozenset()
        return bool(jid) and not jid.endswith(("@g.us", "@broadcast")) and contact_dedup_key(self, jid) in keys

    def commerce_chat_id(self, jid: str) -> str:
        """The chat's id in a form WhatsApp Web uses: its @lid when known,
        else the number as @c.us."""
        norm = self._normalize_jid(jid)
        if norm.endswith("@lid"):
            return norm
        return getattr(self, "_phone_to_lid", {}).get(norm, "") or norm.replace("@s.whatsapp.net", "@c.us")

    def toggle_cart_enabled(self):
        """Business only: flip whether the account's catalog takes carts, then
        say what the server reports, not what was asked for."""
        account = self.__dict__.get("_commerce_account")
        if account is None or not account.business or account.cart_enabled is None:
            return
        wanted = not account.cart_enabled
        connection = connection_identity(self)

        def run():
            # Sent once, then read back: an answer lost after the change took
            # effect must not be announced as a failure (core/read_back.py).
            state, now = read_back.run_action(
                lambda: commerce.set_cart_enabled(connection, wanted),
                lambda: commerce.read_account(connection),
                lambda reading: reading is not None and reading.cart_enabled is wanted,
                pause=_CART_READ_PAUSE)
            if state != "done":
                logging.warning("[commerce] cart_setting_%s", state)
            wx.CallAfter(self._finish_cart_toggle, connection, wanted, now)
        threading.Thread(target=run, daemon=True).start()

    def _finish_cart_toggle(self, connection, wanted, account):
        if connection_identity(self) != connection:
            return
        if account is not None:
            self._commerce_account = account
        if account is None or account.cart_enabled is not wanted:
            self.speak_output.output(self.i18n.t("commerce_cart_setting_failed"))
        else:
            self.speak_output.output(self.i18n.t(
                "commerce_cart_setting_on" if wanted else "commerce_cart_setting_off"))
