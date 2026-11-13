"""Install-wide encrypted AI credentials. Never uses an account's secret.key.

Like the portable token vault, this protects accidental disclosure of the data
file, not theft of the entire installation (the encryption key lives beside it).
Corrupt/missing keys fail closed; we never replace a key covering existing data.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
import uuid

from cryptography.fernet import Fernet, InvalidToken
from coord_locks import app_settings_lock
from core.ai_media.config import PROVIDERS


class CredentialError(RuntimeError):
    def __init__(self):
        super().__init__("ai_error_credentials")


class CredentialStore:
    basename = "ai_credentials"

    def __init__(self, directory):
        self.directory = Path(directory)
        self.key_path = self.directory / (self.basename + ".key")
        self.data_path = self.directory / (self.basename + ".enc")

    def _cipher(self, create=False):
        if not self.key_path.exists():
            if not create or self.data_path.exists():
                raise CredentialError()
            self._atomic(self.key_path, Fernet.generate_key())
        return Fernet(self.key_path.read_bytes())

    def _read(self):
        if not self.data_path.exists():
            return {}
        value = json.loads(self._cipher().decrypt(self.data_path.read_bytes()))
        if not isinstance(value, dict) or any(
            not self._accepts(k) or not isinstance(v, str)
            for k, v in value.items()
        ):
            raise CredentialError()
        return value

    @staticmethod
    def _accepts(provider):
        return isinstance(provider, str) and provider in PROVIDERS

    def _provider(self, provider):
        if not self._accepts(provider):
            raise CredentialError()

    def get(self, provider):
        self._provider(provider)
        try:
            with app_settings_lock(str(self.directory)):
                return self._read().get(provider, "")
        except (OSError, ValueError, TypeError, InvalidToken, RuntimeError):
            raise CredentialError() from None

    def saved(self):
        """The providers that have a key saved. An unreadable store reports
        none: the actions that need a key then simply are not offered."""
        try:
            with app_settings_lock(str(self.directory)):
                return frozenset(p for p, v in self._read().items() if v)
        except (OSError, ValueError, TypeError, InvalidToken, RuntimeError):
            return frozenset()

    def set(self, provider, value):
        """Blank input preserves an existing key; deletion is explicit."""
        self._provider(provider)
        value = value.strip()
        if not value:
            return
        if len(value) > 4096 or any(c.isspace() for c in value):
            raise CredentialError()
        self.apply({provider: value})

    def delete(self, provider):
        self._provider(provider)
        self.apply({provider: None})

    def reset(self):
        """Explicitly discard every key, including an unreadable store.

        UI requires separate confirmation. Removing encrypted data BEFORE the
        key means an interrupted reset cannot strand data under a new key.
        """
        try:
            with app_settings_lock(str(self.directory)):
                self.data_path.unlink(missing_ok=True)
                self.key_path.unlink(missing_ok=True)
        except (OSError, RuntimeError):
            raise CredentialError() from None

    def apply(self, changes, *, reset=False):
        """Validate all drafts first, then atomically write every provider."""
        for provider, value in changes.items():
            self._provider(provider)
            if value and (not isinstance(value, str) or len(value) > 4096 or any(c.isspace() for c in value)):
                raise CredentialError()
        if not changes and not reset:
            return  # nothing staged: an unreadable store must not block unrelated Settings
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with app_settings_lock(str(self.directory)):
                if reset:
                    self.reset()
                data = self._read()
                for provider, value in changes.items():
                    if value is None:
                        data.pop(provider, None)
                    elif value:
                        data[provider] = value
                cipher = self._cipher(create=True)
                self._atomic(self.data_path, cipher.encrypt(json.dumps(data).encode()))
        except (OSError, ValueError, TypeError, InvalidToken, RuntimeError):
            raise CredentialError() from None

    @staticmethod
    def _atomic(path, data):
        tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with open(tmp, "xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)


class TranslationCredentialStore(CredentialStore):
    """Translation-only keys stay out of the vault older builds read."""
    basename = "translation_credentials"

    @staticmethod
    def _accepts(provider):
        return isinstance(provider, str) and (
            provider == "grok" or re.fullmatch(r"translation_[0-9a-f]{64}", provider) is not None
        )


def store_for_translation(shared_store, provider):
    if CredentialStore._accepts(provider):
        return shared_store
    return TranslationCredentialStore(shared_store.directory)


def apply_with_translation(shared_store, changes, *, reset=False):
    """Write staged keys, each to its vault: the AI providers' keys to the
    store older builds read, Grok's and custom endpoints' keys to the
    translation store (an older build would read them as a damaged store).
    A reset empties both."""
    shared = {provider: key for provider, key in changes.items() if CredentialStore._accepts(provider)}
    extra = {provider: key for provider, key in changes.items() if provider not in shared}
    if shared or reset:
        shared_store.apply(shared, reset=reset)
    if extra or reset:
        translation = TranslationCredentialStore(shared_store.directory)
        if extra:
            translation.apply(extra, reset=reset)
        elif translation.data_path.exists() or translation.key_path.exists():
            translation.reset()
