"""Tests for MainWindow._get_wa_token()/_set_wa_token() — the migration from
plaintext settings["privateinfo"]["WA_token"] to Fernet-protected storage
(settings["privateinfo"]["WA_token_protected"], core/token_vault.py).

token_vault's actual Fernet calls are monkeypatched here with a simple
reversible transform so these tests exercise the migration/fallback LOGIC
(which field wins, when a plaintext copy gets removed, what happens when
protection fails or a value fails to unprotect) without depending on the
real cryptography library — that round-trip is covered separately by
tests/test_token_vault.py.

MainWindow is a wx.Frame and cannot be instantiated without a running wx.App,
so the methods under test are exercised as plain functions against a small
stub — same approach as tests/test_sender_names.py.
"""

import pytest

from core import token_vault
from main import MainWindow


class _Stub:
    """Minimal stand-in for MainWindow for token storage/migration."""

    _get_wa_token = MainWindow._get_wa_token
    _set_wa_token = MainWindow._set_wa_token
    _token_key = MainWindow._token_key

    def __init__(self, privateinfo=None, key=b"fake-key"):
        self.settings = {"privateinfo": dict(privateinfo or {})}
        self.save_calls = 0
        self.key = key

    def retrieve_secret_key(self):
        # Should not be needed since self.key is pre-set in these tests,
        # but present so _token_key()'s lazy-load path has something to
        # call if a test ever clears self.key.
        self.key = b"lazily-loaded-key"
        return self.key

    def save_settings(self):
        self.save_calls += 1


@pytest.fixture
def fake_fernet(monkeypatch):
    """Reversible fake standing in for real Fernet: protect() just tags the
    string (with the key baked in) so tests can assert a value really went
    through it, without touching the cryptography library at all."""
    monkeypatch.setattr(
        token_vault, "protect_token",
        lambda t, k: f"PROTECTED({t})[{k!r}]" if t else "",
    )

    def _unprotect(b, k):
        prefix = "PROTECTED("
        suffix = f")[{k!r}]"
        if b.startswith(prefix) and b.endswith(suffix):
            return b[len(prefix):-len(suffix)]
        return ""

    monkeypatch.setattr(token_vault, "unprotect_token", _unprotect)
    return monkeypatch


class TestSetWaToken:
    def test_invalidates_account_list_cache_before_replacing_or_clearing_token(self, fake_fernet):
        mw = _Stub({"WA_token": "old-fake"})
        seen = []
        mw._invalidate_wa_lists = lambda: seen.append(dict(mw.settings["privateinfo"]))
        mw._set_wa_token("new-fake")
        mw._set_wa_token("")
        assert seen[0] == {"WA_token": "old-fake"}
        assert seen[1]["WA_token_protected"].startswith("PROTECTED(new-fake)")

    def test_stores_protected_form_and_no_plaintext(self, fake_fernet):
        mw = _Stub()
        mw._set_wa_token("secret-token:hash")
        pi = mw.settings["privateinfo"]
        assert pi["WA_token_protected"] == f"PROTECTED(secret-token:hash)[{mw.key!r}]"
        assert "WA_token" not in pi
        assert mw.save_calls == 1

    def test_overwrites_a_pre_existing_plaintext_copy(self, fake_fernet):
        mw = _Stub({"WA_token": "old-plaintext-token"})
        mw._set_wa_token("new-token")
        pi = mw.settings["privateinfo"]
        assert pi["WA_token_protected"] == f"PROTECTED(new-token)[{mw.key!r}]"
        assert "WA_token" not in pi

    def test_empty_token_clears_both_fields(self, fake_fernet):
        mw = _Stub({"WA_token_protected": "PROTECTED(x)[k]", "WA_token": "y"})
        mw._set_wa_token("")
        pi = mw.settings["privateinfo"]
        assert "WA_token_protected" not in pi
        assert pi["WA_token"] == ""

    def test_protection_failure_falls_back_to_plaintext(self, monkeypatch):
        def _raise(t, k):
            raise RuntimeError("encryption backend unavailable")
        monkeypatch.setattr(token_vault, "protect_token", _raise)
        mw = _Stub()
        mw._set_wa_token("secret-token")
        pi = mw.settings["privateinfo"]
        assert pi["WA_token"] == "secret-token"
        assert "WA_token_protected" not in pi


class TestGetWaToken:
    def test_reads_back_a_protected_token(self, fake_fernet):
        mw = _Stub()
        mw._set_wa_token("abc")
        mw.save_calls = 0
        assert mw._get_wa_token() == "abc"
        assert mw.save_calls == 0  # plain read, no migration triggered

    def test_migrates_legacy_plaintext_token_on_first_read(self, fake_fernet):
        mw = _Stub({"WA_token": "legacy-plaintext-token"})

        token = mw._get_wa_token()

        assert token == "legacy-plaintext-token"
        pi = mw.settings["privateinfo"]
        # Migration happened: protected form now stored, plaintext gone.
        assert pi["WA_token_protected"] == f"PROTECTED(legacy-plaintext-token)[{mw.key!r}]"
        assert "WA_token" not in pi
        assert mw.save_calls == 1

    def test_no_token_anywhere_returns_empty(self, fake_fernet):
        mw = _Stub()
        assert mw._get_wa_token() == ""

    def test_corrupted_protected_value_falls_back_to_legacy_field(self, monkeypatch):
        """A protected value that fails to unprotect (wrong key — e.g.
        settings.json copied without secret.key, or corruption) must not be
        treated as a crash or as a real token — only as a signal to fall
        back exactly like unprotect_token() itself promises."""
        monkeypatch.setattr(token_vault, "unprotect_token", lambda b, k: "")
        monkeypatch.setattr(token_vault, "protect_token", lambda t, k: f"PROTECTED({t})" if t else "")
        mw = _Stub({"WA_token_protected": "some-corrupted-value"})

        assert mw._get_wa_token() == ""

    def test_second_read_after_migration_uses_protected_field_only(self, fake_fernet):
        mw = _Stub({"WA_token": "legacy-token"})
        mw._get_wa_token()  # triggers migration
        mw.save_calls = 0  # reset to prove the second read doesn't re-save

        token = mw._get_wa_token()

        assert token == "legacy-token"
        assert mw.save_calls == 0


class TestTokenKeyLazyLoad:
    def test_uses_already_loaded_key_without_calling_retrieve(self, fake_fernet):
        mw = _Stub(key=b"already-loaded")
        calls = []
        mw.retrieve_secret_key = lambda: calls.append(1) or b"should-not-be-used"

        assert mw._token_key() == b"already-loaded"
        assert calls == []

    def test_lazily_loads_key_when_not_yet_set(self, fake_fernet):
        mw = _Stub()
        mw.key = None  # simulates running before prepare_sync() set self.key

        result = mw._token_key()

        assert result == b"lazily-loaded-key"
        assert mw.key == b"lazily-loaded-key"
