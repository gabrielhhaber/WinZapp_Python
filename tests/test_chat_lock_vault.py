from cryptography.fernet import Fernet
import pytest

from core.chat_lock_vault import (
    AUTO_LOCK_DEFAULT_MINUTES,
    ChatLockVault,
    PinAttemptLimiter,
    VaultStateError,
    jid_fingerprint,
    normalize_recovery_code,
    normalize_reveal_code,
    validate_pin,
    validate_reveal_code,
)


def _configured():
    vault = ChatLockVault(Fernet.generate_key())
    recovery = vault.configure("246810", " Kasa Şifrem ")
    return vault, recovery


def test_pin_and_reveal_validation_is_explicit():
    assert validate_pin("123456")
    assert validate_pin("123456789012")
    assert not validate_pin("12345")
    assert not validate_pin("１２３４５６")
    assert not validate_pin("12345a")
    assert validate_reveal_code("kasa")
    assert not validate_reveal_code("abc")
    assert normalize_reveal_code("  KaSa ŞİFREM  ") == "kasa şifrem"


def test_serialized_state_is_encrypted_and_contains_no_plain_secrets():
    key = Fernet.generate_key()
    vault = ChatLockVault(key)
    recovery = vault.configure("246810", "gizli-kod")
    vault.lock_chat("1234567890@s.whatsapp.net")

    token = vault.encrypted_token()

    assert "246810" not in token
    assert "gizli-kod" not in token
    assert normalize_recovery_code(recovery) not in token
    assert "1234567890@s.whatsapp.net" not in token

    restored = ChatLockVault.load(key, token)
    assert restored.verify_pin("246810")
    assert restored.matches_reveal_code(" GİZLİ-KOD ")
    assert restored.matches_reveal_code(" GIZLI-KOD ")
    assert restored.is_locked("1234567890@s.whatsapp.net")


def test_recovery_resets_pin_and_rotates_recovery_code():
    vault, recovery = _configured()

    replacement = vault.reset_pin(recovery.lower(), "135790")

    assert not vault.verify_pin("246810")
    assert vault.verify_pin("135790")
    assert not vault.verify_recovery_code(recovery)
    assert vault.verify_recovery_code(replacement)
    assert replacement != recovery


def test_changing_pin_requires_the_current_pin_and_rotates_recovery():
    vault, recovery = _configured()

    with pytest.raises(ValueError):
        vault.change_pin("000000", "135790")

    replacement = vault.change_pin("246810", "135790")

    assert not vault.verify_pin("246810")
    assert vault.verify_pin("135790")
    assert not vault.verify_recovery_code(recovery)
    assert vault.verify_recovery_code(replacement)


def test_recovery_key_can_reveal_pin_dialog_but_does_not_verify_as_pin():
    vault, recovery = _configured()

    assert vault.authorizes_reveal("kasa şifrem")
    assert vault.authorizes_reveal(recovery.lower())
    assert not vault.verify_pin(recovery)
    assert not vault.authorizes_reveal("wrong search")


def test_encrypted_snapshot_can_roll_back_a_cancelled_recovery_rotation():
    key = Fernet.generate_key()
    vault = ChatLockVault(key)
    recovery = vault.configure("246810", "gizli-kod")
    snapshot = vault.encrypted_token()

    replacement = vault.reset_pin(recovery, "135790")
    restored = ChatLockVault.load(key, snapshot)

    assert restored.verify_pin("246810")
    assert not restored.verify_pin("135790")
    assert restored.verify_recovery_code(recovery)
    assert not restored.verify_recovery_code(replacement)


def test_locked_jids_are_unique_and_can_be_removed():
    vault, _ = _configured()
    vault.lock_chat("a@s.whatsapp.net")
    vault.lock_chat("a@s.whatsapp.net")
    vault.lock_chat("b@g.us")
    assert vault.locked_jids == {"a@s.whatsapp.net", "b@g.us"}
    vault.unlock_chat("a@s.whatsapp.net")
    assert vault.locked_jids == {"b@g.us"}


def test_auto_lock_timeout_defaults_to_five_minutes_and_round_trips():
    key = Fernet.generate_key()
    vault = ChatLockVault(key)

    assert vault.auto_lock_minutes == AUTO_LOCK_DEFAULT_MINUTES

    vault.set_auto_lock_minutes(30)
    restored = ChatLockVault.load(key, vault.encrypted_token())
    assert restored.auto_lock_minutes == 30

    restored.set_auto_lock_minutes(0)
    assert restored.auto_lock_minutes == 0


def test_auto_lock_timeout_rejects_unsupported_values():
    vault = ChatLockVault(Fernet.generate_key())

    with pytest.raises(ValueError):
        vault.set_auto_lock_minutes(7)


def test_wrong_key_or_corrupt_state_fails_closed():
    key = Fernet.generate_key()
    vault = ChatLockVault(key)
    vault.configure("246810", "gizli-kod")
    token = vault.encrypted_token()

    with pytest.raises(VaultStateError):
        ChatLockVault.load(Fernet.generate_key(), token)
    with pytest.raises(VaultStateError):
        ChatLockVault.load(key, token[:-4] + "xxxx")


def test_decryptable_but_structurally_empty_state_is_not_treated_as_new():
    key = Fernet.generate_key()
    empty_state_token = Fernet(key).encrypt(b"{}").decode("ascii")

    with pytest.raises(VaultStateError):
        ChatLockVault.load(key, empty_state_token)


def test_jid_fingerprint_is_keyed_and_contains_no_jid():
    jid = "1234567890@s.whatsapp.net"
    first = jid_fingerprint(Fernet.generate_key(), jid)
    second = jid_fingerprint(Fernet.generate_key(), jid)
    assert first != second
    assert jid not in first


def test_pin_attempt_limiter_locks_after_three_failures_and_resets():
    now = [100.0]
    limiter = PinAttemptLimiter(lockout_seconds=10, clock=lambda: now[0])
    assert limiter.record_failure() == 0
    assert limiter.record_failure() == 0
    assert limiter.record_failure() == 10
    now[0] = 105.1
    assert limiter.remaining_seconds() == 5
    now[0] = 110.0
    assert limiter.remaining_seconds() == 0
    limiter.record_failure()
    limiter.record_success()
    assert limiter.failures == 0


def test_pin_lockout_survives_an_encrypted_vault_reload():
    now = [100.0]
    key = Fernet.generate_key()
    vault = ChatLockVault(key)
    vault.configure("246810", "gizli-kod")

    assert vault.record_pin_failure(clock=lambda: now[0], lockout_seconds=10) == 0
    assert vault.record_pin_failure(clock=lambda: now[0], lockout_seconds=10) == 0
    assert vault.record_pin_failure(clock=lambda: now[0], lockout_seconds=10) == 10

    restored = ChatLockVault.load(key, vault.encrypted_token())
    now[0] = 105.1
    assert restored.remaining_pin_lockout_seconds(lambda: now[0]) == 5

    assert restored.clear_pin_failures()
    assert restored.remaining_pin_lockout_seconds(lambda: now[0]) == 0
