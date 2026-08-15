"""AES-256-GCM credential vault. / Хранилище credentials на AES-256-GCM."""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class CredentialVaultError(ValueError):
    """Safe public error without secret material. / Безопасная ошибка без секретов."""


@dataclass(frozen=True, slots=True)
class EncryptedCredentials:
    ciphertext_b64: str
    nonce_b64: str
    key_version: int
    api_key_last4: str


class CredentialVault:
    def __init__(self, master_key_b64: str, key_version: int = 1) -> None:
        try:
            master_key = base64.b64decode(master_key_b64, validate=True)
        except (ValueError, TypeError) as exc:
            raise CredentialVaultError("Master key must be valid base64") from exc
        if len(master_key) != 32:
            raise CredentialVaultError("Master key must decode to exactly 32 bytes")
        self._cipher = AESGCM(master_key)
        self._key_version = key_version

    def encrypt(
        self,
        api_key: str,
        secret_key: str,
        *,
        exchange_account_id: str,
        telegram_user_id: int,
    ) -> EncryptedCredentials:
        if not api_key or not secret_key:
            raise CredentialVaultError("API key and secret key are required")
        nonce = os.urandom(12)
        aad = self._aad(exchange_account_id, telegram_user_id, self._key_version)
        plaintext = bytearray(
            json.dumps(
                {"api_key": api_key, "secret_key": secret_key},
                separators=(",", ":"),
            ).encode("utf-8")
        )
        try:
            ciphertext = self._cipher.encrypt(nonce, bytes(plaintext), aad)
        finally:
            # Best effort: clear the mutable working copy immediately.
            # Best effort: сразу очищаем изменяемую рабочую копию.
            plaintext[:] = b"\x00" * len(plaintext)
        return EncryptedCredentials(
            ciphertext_b64=base64.b64encode(ciphertext).decode("ascii"),
            nonce_b64=base64.b64encode(nonce).decode("ascii"),
            key_version=self._key_version,
            api_key_last4=api_key[-4:],
        )

    def decrypt(
        self,
        encrypted: EncryptedCredentials,
        *,
        exchange_account_id: str,
        telegram_user_id: int,
    ) -> tuple[str, str]:
        try:
            nonce = base64.b64decode(encrypted.nonce_b64, validate=True)
            ciphertext = base64.b64decode(encrypted.ciphertext_b64, validate=True)
            aad = self._aad(
                exchange_account_id,
                telegram_user_id,
                encrypted.key_version,
            )
            plaintext = self._cipher.decrypt(nonce, ciphertext, aad)
            payload = json.loads(plaintext)
            return str(payload["api_key"]), str(payload["secret_key"])
        except Exception as exc:
            # Never include ciphertext, AAD, or key details in the exception.
            # Никогда не включаем ciphertext, AAD или ключи в исключение.
            raise CredentialVaultError("Credential decryption failed") from exc

    @staticmethod
    def _aad(exchange_account_id: str, telegram_user_id: int, key_version: int) -> bytes:
        return f"{exchange_account_id}:{telegram_user_id}:{key_version}".encode()
