"""Local security state for WinZapp's locked-conversation vault.

This module deliberately contains no wx or database code.  The UI owns focus
and dialogs; ``MainWindow`` owns persistence.  Keeping the secret handling
here makes the important parts testable without opening a window.

The existing per-account Fernet key protects the serialized vault metadata at
rest.  PINs, recovery keys and reveal codes are never stored reversibly: each
is independently salted and passed through scrypt.  The PIN gates access in
WinZapp; the recovery key may replace it, but cannot reveal the old PIN.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import unicodedata
from dataclasses import dataclass
from typing import Callable

from cryptography.fernet import Fernet, InvalidToken


VAULT_VERSION = 1
PIN_MIN_LENGTH = 6
PIN_MAX_LENGTH = 12
REVEAL_CODE_MIN_LENGTH = 4
REVEAL_CODE_MAX_LENGTH = 64

_SCRYPT_N = 1 << 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SALT_BYTES = 16
_HASH_BYTES = 32
_RECOVERY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


class VaultStateError(ValueError):
    """The persisted vault state is unreadable or structurally invalid."""


def validate_pin(pin: str) -> bool:
    return bool(pin and pin.isascii() and pin.isdigit()
                and PIN_MIN_LENGTH <= len(pin) <= PIN_MAX_LENGTH)


def normalize_reveal_code(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").strip().casefold()
    # Unicode case-folding turns Turkish capital dotted I into ``i`` plus a
    # combining dot. Treat that as the same search code as the ordinary lower
    # case ``i`` users naturally type later, while preserving other accents.
    return unicodedata.normalize("NFKC", normalized.replace("\N{COMBINING DOT ABOVE}", ""))


def validate_reveal_code(value: str) -> bool:
    normalized = normalize_reveal_code(value)
    return REVEAL_CODE_MIN_LENGTH <= len(normalized) <= REVEAL_CODE_MAX_LENGTH


def normalize_recovery_code(value: str) -> str:
    return "".join(ch for ch in (value or "").upper() if ch in _RECOVERY_ALPHABET)


def generate_recovery_code() -> str:
    raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(20))
    return "-".join(raw[index:index + 4] for index in range(0, len(raw), 4))


def jid_fingerprint(fernet_key: bytes, jid: str) -> str:
    """Return a keyed, non-reversible identifier for a chat JID.

    MainWindow stores these beside the encrypted vault token as a corruption
    fallback.  If the token becomes unreadable, locked chats can still remain
    hidden without placing phone-number-bearing JIDs in plaintext metadata.
    """
    try:
        key = base64.urlsafe_b64decode(fernet_key)
    except Exception:
        key = fernet_key
    return hmac.new(key, (jid or "").encode("utf-8"), hashlib.sha256).hexdigest()


def _secret_record(value: str) -> dict:
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.scrypt(
        value.encode("utf-8"), salt=salt,
        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_HASH_BYTES,
    )
    return {"salt": salt.hex(), "digest": digest.hex()}


def _verify_secret(value: str, record: object) -> bool:
    if not isinstance(record, dict):
        return False
    try:
        salt = bytes.fromhex(str(record["salt"]))
        expected = bytes.fromhex(str(record["digest"]))
    except (KeyError, TypeError, ValueError):
        return False
    if len(salt) != _SALT_BYTES or len(expected) != _HASH_BYTES:
        return False
    actual = hashlib.scrypt(
        value.encode("utf-8"), salt=salt,
        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_HASH_BYTES,
    )
    return hmac.compare_digest(actual, expected)


class ChatLockVault:
    """Mutable, per-account locked-conversation state."""

    def __init__(self, fernet_key: bytes, state: dict | None = None):
        self._fernet = Fernet(fernet_key)
        self._state = state if state is not None else {
            "version": VAULT_VERSION,
            "configured": False,
            "hide_navigation": True,
            "locked_jids": [],
            "pin": None,
            "recovery": None,
            "reveal": None,
        }
        self._validate_state()

    @classmethod
    def load(cls, fernet_key: bytes, token: str | None) -> "ChatLockVault":
        if not token:
            return cls(fernet_key)
        try:
            payload = Fernet(fernet_key).decrypt(token.encode("ascii"))
            state = json.loads(payload.decode("utf-8"))
        except (InvalidToken, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise VaultStateError("locked-conversation state could not be decrypted") from exc
        if not isinstance(state, dict):
            raise VaultStateError("locked-conversation state is not an object")
        return cls(fernet_key, state)

    def _validate_state(self) -> None:
        if self._state.get("version") != VAULT_VERSION:
            raise VaultStateError("unsupported locked-conversation state version")
        locked = self._state.get("locked_jids")
        if not isinstance(locked, list) or not all(isinstance(jid, str) for jid in locked):
            raise VaultStateError("locked-conversation identifiers are invalid")
        # De-duplicate without changing the user's stable order.
        self._state["locked_jids"] = list(dict.fromkeys(jid for jid in locked if jid))
        self._state["configured"] = bool(self._state.get("configured"))
        self._state["hide_navigation"] = bool(self._state.get("hide_navigation", True))
        if self.configured and not all(
            isinstance(self._state.get(name), dict)
            for name in ("pin", "recovery", "reveal")
        ):
            raise VaultStateError("configured locked-conversation state is incomplete")

    @property
    def configured(self) -> bool:
        return bool(self._state.get("configured"))

    @property
    def hide_navigation(self) -> bool:
        return bool(self._state.get("hide_navigation", True))

    @property
    def locked_jids(self) -> frozenset[str]:
        return frozenset(self._state.get("locked_jids", ()))

    def configure(self, pin: str, reveal_code: str) -> str:
        if self.configured:
            raise ValueError("vault is already configured")
        if not validate_pin(pin):
            raise ValueError("PIN must contain 6 to 12 ASCII digits")
        if not validate_reveal_code(reveal_code):
            raise ValueError("reveal code must contain 4 to 64 characters")
        recovery_code = generate_recovery_code()
        self._state.update({
            "configured": True,
            "pin": _secret_record(pin),
            "recovery": _secret_record(normalize_recovery_code(recovery_code)),
            "reveal": _secret_record(normalize_reveal_code(reveal_code)),
        })
        return recovery_code

    def verify_pin(self, pin: str) -> bool:
        return self.configured and validate_pin(pin) and _verify_secret(pin, self._state["pin"])

    def matches_reveal_code(self, value: str) -> bool:
        normalized = normalize_reveal_code(value)
        return bool(normalized) and self.configured and _verify_secret(
            normalized, self._state["reveal"]
        )

    def verify_recovery_code(self, value: str) -> bool:
        normalized = normalize_recovery_code(value)
        return bool(normalized) and self.configured and _verify_secret(
            normalized, self._state["recovery"]
        )

    def authorizes_reveal(self, value: str) -> bool:
        """Whether search input may reveal the PIN dialog, never the vault.

        The recovery key is a deliberately narrow fallback for a forgotten
        secret search code. It grants no chat access by itself; the user must
        still enter the PIN or explicitly run the recovery reset flow.
        """
        return self.matches_reveal_code(value) or self.verify_recovery_code(value)

    def reset_pin(self, recovery_code: str, new_pin: str) -> str:
        if not self.verify_recovery_code(recovery_code):
            raise ValueError("recovery code is invalid")
        if not validate_pin(new_pin):
            raise ValueError("PIN must contain 6 to 12 ASCII digits")
        new_recovery = generate_recovery_code()
        self._state["pin"] = _secret_record(new_pin)
        self._state["recovery"] = _secret_record(
            normalize_recovery_code(new_recovery)
        )
        return new_recovery

    def change_reveal_code(self, pin: str, reveal_code: str) -> None:
        if not self.verify_pin(pin):
            raise ValueError("PIN is invalid")
        if not validate_reveal_code(reveal_code):
            raise ValueError("reveal code must contain 4 to 64 characters")
        self._state["reveal"] = _secret_record(normalize_reveal_code(reveal_code))

    def set_hide_navigation(self, hide: bool) -> None:
        self._state["hide_navigation"] = bool(hide)

    def lock_chat(self, jid: str) -> None:
        if not self.configured:
            raise ValueError("vault is not configured")
        if jid and jid not in self._state["locked_jids"]:
            self._state["locked_jids"].append(jid)

    def unlock_chat(self, jid: str) -> None:
        self._state["locked_jids"] = [
            current for current in self._state["locked_jids"] if current != jid
        ]

    def is_locked(self, jid: str) -> bool:
        return bool(jid) and jid in self.locked_jids

    def encrypted_token(self) -> str:
        payload = json.dumps(
            self._state, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return self._fernet.encrypt(payload).decode("ascii")


@dataclass
class PinAttemptLimiter:
    """Small in-memory brute-force delay for the PIN dialog."""

    max_attempts: int = 3
    lockout_seconds: float = 30.0
    clock: Callable[[], float] = time.monotonic
    failures: int = 0
    locked_until: float = 0.0

    def remaining_seconds(self) -> int:
        remaining = self.locked_until - self.clock()
        return max(0, int(remaining + 0.999))

    def record_failure(self) -> int:
        if self.remaining_seconds():
            return self.remaining_seconds()
        self.failures += 1
        if self.failures >= self.max_attempts:
            self.failures = 0
            self.locked_until = self.clock() + self.lockout_seconds
        return self.remaining_seconds()

    def record_success(self) -> None:
        self.failures = 0
        self.locked_until = 0.0
