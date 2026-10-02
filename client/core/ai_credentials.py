"""Install-wide encrypted AI credentials. Never uses an account's secret.key.

Like the portable token vault, this protects accidental disclosure of the data
file, not theft of the entire installation (the encryption key lives beside it).
Corrupt/missing keys fail closed; we never replace a key covering existing data.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import uuid

from cryptography.fernet import Fernet, InvalidToken
from coord_locks import app_settings_lock


class CredentialError(RuntimeError):
    def __init__(self):
        super().__init__("ai_error_credentials")


class CredentialStore:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.key_path = self.directory / "ai_credentials.key"
        self.data_path = self.directory / "ai_credentials.enc"

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
            k not in ("openai", "gemini") or not isinstance(v, str)
            for k, v in value.items()
        ):
            raise CredentialError()
        return value

    @staticmethod
    def _provider(provider):
        if provider not in ("openai", "gemini"):
            raise CredentialError()

    def get(self, provider):
        self._provider(provider)
        try:
            with app_settings_lock(str(self.directory)):
                return self._read().get(provider, "")
        except (OSError, ValueError, TypeError, InvalidToken, RuntimeError):
            raise CredentialError() from None

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
        """Explicitly discard both keys, including an unreadable store.

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
        """Validate all drafts first, then atomically write both providers."""
        for provider, value in changes.items():
            self._provider(provider)
            if value and (not isinstance(value, str) or len(value) > 4096 or any(c.isspace() for c in value)):
                raise CredentialError()
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
                if not changes and not reset:
                    return
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
