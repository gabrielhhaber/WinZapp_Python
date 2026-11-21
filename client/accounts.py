"""
Process-safe account registry for WinZapp multi-account (client/accounts.py)
============================================================================

Stores the set of WhatsApp accounts and which one was last brought to the
foreground, in ``<global_dir>/accounts.json``. Every mutation runs inside the
process-wide ``registry_lock`` (see coord_locks.py) as a single
read-modify-write transaction with an atomic tmp+os.replace, so two WinZapp
processes racing on the file never lose each other's changes.

Design points (from the reviewed plan, Zadanie 1.2):
  * Account id = full uuid4 hex (32 chars). Validated with ^[0-9a-f]{32}$ on
    every load so a hand-edited/corrupt entry can never be used to build a path
    outside accounts/ (path-traversal guard).
  * States: pending / paired / deleting / archived.
  * order: explicit, STABLE integer used for the Ctrl+Shift+1..9 switch
    shortcuts; assigned max+1 on add, never reshuffled by rename/last_used.
  * last_foreground: the account last consciously raised to the foreground by
    the user. Only mutated by a deliberate user action (never by --background).
  * Corrupt accounts.json is NOT treated as empty: the registry enters
    read-only recovery mode (backs the file up as .corrupt-<ts>, blocks all
    mutations AND the automatic first-run) so a parse error can't spawn a new
    account that overwrites the registry and orphans existing account data.

This module never imports wx — it is pure logic and fully unit-testable.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from typing import Optional

from coord_locks import registry_lock

VALID_STATES = ("pending", "paired", "deleting", "archived")
_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SCHEMA = 1
_RECOVERY_MARKER = "accounts.recovery"


def is_valid_account_id(account_id) -> bool:
    """True iff account_id is a canonical 32-hex uuid (fullmatch, no trailing \\n)."""
    return isinstance(account_id, str) and _ID_RE.fullmatch(account_id) is not None


class RegistryCorruptError(RuntimeError):
    """Raised when a mutation is attempted while in read-only recovery mode."""


class AccountRegistry:
    def __init__(self, global_dir: str, lock_factory=registry_lock):
        self.global_dir = os.path.abspath(global_dir)
        self._path = os.path.join(self.global_dir, "accounts.json")
        self._marker = os.path.join(self.global_dir, _RECOVERY_MARKER)
        self._lock_factory = lock_factory
        self._recovery = False
        os.makedirs(self.global_dir, exist_ok=True)
        # Probe once (under lock) so is_recovery_mode() is meaningful right away.
        with self._lock_factory(self.global_dir):
            self._read_locked()

    # ── recovery flag ────────────────────────────────────────────────────
    def is_recovery_mode(self) -> bool:
        # Re-check under the lock (GPT r3 #6): another process may have entered
        # recovery after this instance was constructed, so a cached flag would
        # be stale. _read_locked() refreshes self._recovery from the marker.
        with self._lock_factory(self.global_dir):
            self._read_locked()
        return self._recovery

    # ── low-level read — MUST be called while holding registry_lock ───────
    def _read_locked(self) -> dict:
        empty = {"schema": _SCHEMA, "last_foreground": None, "accounts": []}
        # Persistent recovery marker: recovery is sticky ACROSS processes
        # (GPT r1-code #3), so another instance that starts after the corrupt
        # file was moved aside never treats the registry as fresh and never
        # runs first-run/add(). The marker lives beside accounts.json. Use
        # lexists so a broken/dangling symlink at the marker path still counts
        # as "recovery present" (GPT r6 #1) — not silently treated as absent.
        if os.path.lexists(self._marker):
            self._recovery = True
            return empty
        if not os.path.lexists(self._path):
            self._recovery = False
            return empty
        if not os.path.isfile(self._path):
            # A dir / broken symlink / other non-regular file where accounts.json
            # should be is NOT an empty registry — it's corrupt (GPT r5 #4).
            self._enter_recovery_locked()
            return empty
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, ValueError, OSError):
            self._enter_recovery_locked()
            return empty

        if not self._validate_model(data):
            self._enter_recovery_locked()
            return empty

        self._recovery = False
        return data

    def _validate_model(self, data) -> bool:
        """Validate the WHOLE model, not just ids (GPT r1-code #5).

        Any invariant violation -> caller enters recovery instead of trusting a
        half-broken registry. Checks: dict shape, schema not newer than ours,
        each account has valid id/state/order/autostart, unique ids & orders,
        last_foreground (if set) references an existing account.
        """
        if not isinstance(data, dict) or not isinstance(data.get("accounts"), list):
            return False
        schema = data.get("schema", _SCHEMA)
        # bool is an int subclass in Python, so exclude it explicitly (GPT r4 #4);
        # also reject anything below our minimum supported schema.
        if (not isinstance(schema, int) or isinstance(schema, bool)
                or schema < 1 or schema > _SCHEMA):
            return False
        ids, orders = set(), set()
        for a in data["accounts"]:
            if not isinstance(a, dict):
                return False
            if not is_valid_account_id(a.get("id")):
                return False
            if not isinstance(a.get("name"), str):
                return False  # a nameless account would break the UI
            if a.get("state") not in VALID_STATES:
                return False
            if not isinstance(a.get("order"), int) or isinstance(a.get("order"), bool):
                return False
            if not isinstance(a.get("autostart"), bool):
                return False
            if a["id"] in ids or a["order"] in orders:
                return False  # duplicate id / order
            ids.add(a["id"])
            orders.add(a["order"])
        lf = data.get("last_foreground")
        # last_foreground must be None or a valid id referencing an account.
        # Guard the type first so a list/dict value can't raise TypeError here
        # (GPT r2-code #4) — an invalid value means the model is corrupt.
        if lf is not None:
            if not is_valid_account_id(lf) or lf not in ids:
                return False
        return True

    def _enter_recovery_locked(self) -> None:
        """Enter read-only recovery. Caller holds registry_lock.

        COPY the corrupt file to a .corrupt-<ts> backup (don't move it), and
        drop a persistent recovery marker so every process — including ones
        that started earlier — refuses mutations/first-run until an operator
        restores a good accounts.json and removes the marker.
        """
        self._recovery = True
        try:
            if os.path.isfile(self._path):
                backup = f"{self._path}.corrupt-{int(time.time())}"
                if not os.path.exists(backup):
                    import shutil
                    shutil.copy2(self._path, backup)
        except OSError:
            pass
        try:
            # Write the marker WITHOUT following a symlink at that path (GPT r6
            # #1): a pre-existing symlink must not redirect the write outside
            # global_dir. O_NOFOLLOW is honored where available (POSIX); on
            # Windows os.open lacks it but symlink-at-path attacks need an
            # attacker in the same per-user dir, and lexists already flagged it.
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
            flags |= getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(self._marker, flags, 0o600)
            try:
                os.write(fd, str(int(time.time())).encode("ascii"))
            finally:
                os.close(fd)
        except OSError:
            pass

    # ── low-level atomic write (caller holds lock) ───────────────────────
    def _write(self, data: dict) -> None:
        data["schema"] = _SCHEMA
        tmp = f"{self._path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._path)

    # ── transaction helper ───────────────────────────────────────────────
    def _txn(self, fn):
        """Run fn(data)->result under registry_lock as one RMW transaction."""
        with self._lock_factory(self.global_dir):
            data = self._read_locked()
            if self._recovery:
                raise RegistryCorruptError(
                    "accounts.json is corrupt — registry is in read-only recovery mode"
                )
            result, changed = fn(data)
            if changed:
                # Defensive: never persist a model that our own read would
                # reject as corrupt (GPT r2-code #3). A failing invariant here
                # is a programming error, surfaced loudly rather than silently
                # tripping recovery on the next launch.
                if not self._validate_model(data):
                    raise ValueError("refusing to write invalid registry model")
                self._write(data)
            return result

    def _read(self) -> dict:
        """Locked read for public accessors (GPT r1-code #4: reads must hold
        the lock so a concurrent os.replace() can't be observed half-written,
        and so recovery detection is serialized)."""
        with self._lock_factory(self.global_dir):
            return self._read_locked()

    # ── reads ────────────────────────────────────────────────────────────
    def list(self) -> list[dict]:
        return self._read()["accounts"]

    def get(self, account_id: str) -> Optional[dict]:
        for a in self._read()["accounts"]:
            if a["id"] == account_id:
                return a
        return None

    def last_foreground(self) -> Optional[str]:
        return self._read().get("last_foreground")

    def list_paired(self) -> list[dict]:
        return sorted(
            [a for a in self.list() if a["state"] == "paired"],
            key=lambda a: a["order"],
        )

    def list_autostart_paired(self) -> list[dict]:
        return [a for a in self.list_paired() if a.get("autostart")]

    def data_dir_for(self, account_id: str) -> str:
        # accounts/<id> lives as a SIBLING of global/, never inside it.
        # Validate the id regardless of origin (GPT r1-code #6) so a crafted
        # value can never escape the accounts/ root via path traversal.
        if not is_valid_account_id(account_id):
            raise ValueError(f"invalid account id {account_id!r}")
        accounts_root = os.path.join(os.path.dirname(self.global_dir), "accounts")
        return os.path.join(accounts_root, account_id)

    # ── mutations (all under registry_lock) ──────────────────────────────
    def add(self, name: str, state: str = "pending") -> dict:
        if state not in VALID_STATES:
            raise ValueError(f"invalid state {state!r}")

        def _fn(data):
            now = int(time.time())
            next_order = max((a["order"] for a in data["accounts"]), default=0) + 1
            acc = {
                "id": uuid.uuid4().hex,
                "name": name,
                "state": state,
                "autostart": False,
                "order": next_order,
                "created_at": now,
                "last_used_at": now,
            }
            data["accounts"].append(acc)
            return acc, True

        return self._txn(_fn)

    def adopt(self, account_id: str, name: str) -> dict:
        """Register an already paired account under its existing id: one
        copied from the installed WinZapp into a portable copy, whose data
        folder (accounts/<id>) already holds it (installed_account_copy.py)."""
        if not is_valid_account_id(account_id):
            raise ValueError(f"invalid account id {account_id!r}")
        if not isinstance(name, str):
            raise ValueError("name must be a string")

        def _fn(data):
            if any(a["id"] == account_id for a in data["accounts"]):
                raise ValueError(f"account {account_id} already registered")
            now = int(time.time())
            acc = {
                "id": account_id,
                "name": name,
                "state": "paired",
                "autostart": False,
                "order": max((a["order"] for a in data["accounts"]), default=0) + 1,
                "created_at": now,
                "last_used_at": now,
            }
            data["accounts"].append(acc)
            return acc, True

        return self._txn(_fn)

    def rename(self, account_id: str, name: str) -> None:
        def _fn(data):
            for a in data["accounts"]:
                if a["id"] == account_id:
                    a["name"] = name
                    return None, True
            return None, False

        self._txn(_fn)

    def set_state(self, account_id: str, state: str) -> None:
        if state not in VALID_STATES:
            raise ValueError(f"invalid state {state!r}")

        def _fn(data):
            for a in data["accounts"]:
                if a["id"] == account_id:
                    a["state"] = state
                    return None, True
            return None, False

        self._txn(_fn)

    def set_autostart(self, account_id: str, value: bool) -> None:
        def _fn(data):
            for a in data["accounts"]:
                if a["id"] == account_id:
                    a["autostart"] = bool(value)
                    return None, True
            return None, False

        self._txn(_fn)

    def set_order(self, account_id: str, order: int) -> None:
        if not isinstance(order, int) or isinstance(order, bool):
            raise ValueError("order must be an int")
        def _fn(data):
            # Reject a duplicate order up-front (GPT r2-code #3) so we never
            # persist a model that the next read would reject as corrupt.
            if any(a["order"] == order and a["id"] != account_id for a in data["accounts"]):
                raise ValueError(f"order {order} already in use")
            for a in data["accounts"]:
                if a["id"] == account_id:
                    a["order"] = int(order)
                    return None, True
            return None, False

        self._txn(_fn)

    def set_last_foreground(self, account_id: Optional[str]) -> None:
        # GPT r1-code #6: never store an arbitrary/nonexistent id. None is
        # allowed (clears the field); any other value must reference a real
        # account, else the model invariant (validated on read) breaks.
        def _fn(data):
            if account_id is not None and not any(a["id"] == account_id for a in data["accounts"]):
                raise ValueError(f"last_foreground references unknown account {account_id!r}")
            data["last_foreground"] = account_id
            return None, True

        self._txn(_fn)

    def touch_last_used(self, account_id: str) -> None:
        def _fn(data):
            for a in data["accounts"]:
                if a["id"] == account_id:
                    a["last_used_at"] = int(time.time())
                    return None, True
            return None, False

        self._txn(_fn)

    def update_fields(self, account_id: str, **fields) -> None:
        """Atomically set several fields of one account in a single transaction.

        Used e.g. by archiving where state + autostart + last_foreground must
        change together (plan Zad. 4.5). ``last_foreground`` is a registry-level
        field and is handled specially when passed.
        """
        allowed = {"name", "state", "autostart", "order"}
        lf_sentinel = object()
        new_lf = fields.pop("last_foreground", lf_sentinel)
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"unknown fields {bad}")
        if "state" in fields and fields["state"] not in VALID_STATES:
            raise ValueError(f"invalid state {fields['state']!r}")
        # Validate field TYPES up-front (GPT r2-code #3) so update_fields can
        # never persist a model the next read would reject as corrupt.
        if "name" in fields and not isinstance(fields["name"], str):
            raise ValueError("name must be a string")
        if "autostart" in fields and not isinstance(fields["autostart"], bool):
            raise ValueError("autostart must be a bool")
        if "order" in fields and (not isinstance(fields["order"], int)
                                   or isinstance(fields["order"], bool)):
            raise ValueError("order must be an int")

        def _fn(data):
            changed = False
            ids = {a["id"] for a in data["accounts"]}
            # The target account must exist BEFORE we touch anything (GPT r3 #5):
            # otherwise e.g. archiving an already-removed account with
            # last_foreground=None would wrongly clear the CURRENT foreground.
            if account_id not in ids:
                raise KeyError(f"account {account_id} not found")
            # last_foreground must reference an existing account (or None).
            if new_lf is not lf_sentinel and new_lf is not None:
                if not is_valid_account_id(new_lf) or new_lf not in ids:
                    raise ValueError(f"last_foreground references unknown account {new_lf!r}")
            # a new order must stay unique
            if "order" in fields and any(
                a["order"] == fields["order"] and a["id"] != account_id
                for a in data["accounts"]
            ):
                raise ValueError(f"order {fields['order']} already in use")
            for a in data["accounts"]:
                if a["id"] == account_id:
                    a.update(fields)
                    changed = True
                    break
            if new_lf is not lf_sentinel:
                data["last_foreground"] = new_lf
                changed = True
            return None, changed

        self._txn(_fn)

    def remove(self, account_id: str) -> None:
        def _fn(data):
            before = len(data["accounts"])
            data["accounts"] = [a for a in data["accounts"] if a["id"] != account_id]
            removed = len(data["accounts"]) != before
            # GPT r1-code #7: clearing last_foreground is ALSO a change, even
            # when the account entry was already gone — otherwise the dangling
            # last_foreground would never be persisted away.
            cleared_lf = False
            if data.get("last_foreground") == account_id:
                data["last_foreground"] = None
                cleared_lf = True
            return None, (removed or cleared_lf)

        self._txn(_fn)
