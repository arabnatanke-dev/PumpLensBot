"""Credential and Mini App security tests. / Тесты безопасности credentials и Mini App."""

import base64
import hashlib
import hmac
import json
from urllib.parse import urlencode

import pytest

from pumplens.binance.permissions import FORBIDDEN_TRUE_FIELDS, validate_read_only_permissions
from pumplens.security.credential_vault import (
    CredentialVault,
    CredentialVaultError,
    EncryptedCredentials,
)
from pumplens.security.redaction import redact
from pumplens.security.telegram_init_data import TelegramInitDataError, validate_init_data


def test_vault_round_trip_and_aad_isolation() -> None:
    master = base64.b64encode(b"x" * 32).decode()
    vault = CredentialVault(master)
    encrypted = vault.encrypt(
        "api-key-1234",
        "secret-value",
        exchange_account_id="account-1",
        telegram_user_id=42,
    )
    assert "api-key" not in encrypted.ciphertext_b64
    assert encrypted.api_key_last4 == "1234"
    assert vault.decrypt(
        encrypted,
        exchange_account_id="account-1",
        telegram_user_id=42,
    ) == ("api-key-1234", "secret-value")
    with pytest.raises(CredentialVaultError):
        vault.decrypt(
            encrypted,
            exchange_account_id="another-account",
            telegram_user_id=42,
        )


def test_vault_rejects_modified_ciphertext() -> None:
    master = base64.b64encode(b"y" * 32).decode()
    vault = CredentialVault(master)
    encrypted = vault.encrypt(
        "api-key",
        "secret",
        exchange_account_id="account-1",
        telegram_user_id=42,
    )
    tampered = EncryptedCredentials(
        ciphertext_b64=encrypted.ciphertext_b64[:-2] + "AA",
        nonce_b64=encrypted.nonce_b64,
        key_version=encrypted.key_version,
        api_key_last4=encrypted.api_key_last4,
    )
    with pytest.raises(CredentialVaultError):
        vault.decrypt(tampered, exchange_account_id="account-1", telegram_user_id=42)


def test_permissions_fail_closed() -> None:
    safe = {"enableReading": True, **dict.fromkeys(FORBIDDEN_TRUE_FIELDS, False)}
    assert validate_read_only_permissions(safe).accepted is True
    assert validate_read_only_permissions({"enableReading": True}).accepted is False
    unsafe = {**safe, "enableFutures": True}
    decision = validate_read_only_permissions(unsafe)
    assert decision.accepted is False
    assert "enableFutures" in decision.reason


def signed_init_data(bot_token: str, now: int) -> str:
    values = {
        "auth_date": str(now),
        "query_id": "query-1",
        "user": json.dumps(
            {"id": 42, "first_name": "Rose", "username": "rose"},
            separators=(",", ":"),
        ),
    }
    check = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


def test_telegram_init_data_signature_and_age() -> None:
    token = "123456:bot-token"
    raw = signed_init_data(token, now=1_700_000_000)
    user = validate_init_data(raw, token, now=1_700_000_010)
    assert user.user_id == 42
    with pytest.raises(TelegramInitDataError):
        validate_init_data(raw, "wrong-token", now=1_700_000_010)
    with pytest.raises(TelegramInitDataError):
        validate_init_data(raw, token, now=1_700_001_000)


def test_recursive_redaction() -> None:
    safe = redact(
        {
            "api_key": "must-not-leak",
            "nested": {"password": "must-not-leak"},
            "message": "Authorization: Bearer abc.def.ghi",
        }
    )
    assert safe["api_key"] == "[REDACTED]"
    assert safe["nested"]["password"] == "[REDACTED]"
    assert "abc.def.ghi" not in safe["message"]
