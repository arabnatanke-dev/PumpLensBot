"""One-time Mini App connection sessions. / Одноразовые сессии Mini App."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass


class ConnectSessionError(ValueError):
    """Expired, reused, or mismatched session. / Истёкшая или неверная сессия."""


@dataclass(frozen=True, slots=True)
class ConnectSessionTokens:
    session_token: str
    csrf_token: str
    expires_at: float


@dataclass(slots=True)
class _StoredSession:
    session_hash: str
    csrf_hash: str
    telegram_user_id: int
    expires_at: float
    used: bool = False


class ConnectSessionStore:
    def __init__(self, ttl_seconds: int = 600) -> None:
        self._ttl_seconds = ttl_seconds
        self._sessions: dict[str, _StoredSession] = {}

    def create(self, telegram_user_id: int) -> ConnectSessionTokens:
        self._purge()
        session_token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(24)
        session_hash = self._hash(session_token)
        expires_at = time.monotonic() + self._ttl_seconds
        self._sessions[session_hash] = _StoredSession(
            session_hash=session_hash,
            csrf_hash=self._hash(csrf_token),
            telegram_user_id=telegram_user_id,
            expires_at=expires_at,
        )
        return ConnectSessionTokens(session_token, csrf_token, expires_at)

    def consume(self, session_token: str, csrf_token: str, telegram_user_id: int) -> None:
        stored = self._validate(session_token, csrf_token, telegram_user_id)
        stored.used = True

    def validate(self, session_token: str, csrf_token: str, telegram_user_id: int) -> None:
        """Validate without consuming. / Проверяет без погашения ссылки."""

        self._validate(session_token, csrf_token, telegram_user_id)

    def _validate(
        self,
        session_token: str,
        csrf_token: str,
        telegram_user_id: int,
    ) -> _StoredSession:
        self._purge()
        session_hash = self._hash(session_token)
        stored = self._sessions.get(session_hash)
        if stored is None or stored.used or stored.expires_at <= time.monotonic():
            raise ConnectSessionError("connect_session_invalid")
        if stored.telegram_user_id != telegram_user_id:
            raise ConnectSessionError("connect_session_user_mismatch")
        if not hmac.compare_digest(stored.csrf_hash, self._hash(csrf_token)):
            raise ConnectSessionError("csrf_invalid")
        return stored

    def _purge(self) -> None:
        now = time.monotonic()
        self._sessions = {
            key: value
            for key, value in self._sessions.items()
            if not value.used and value.expires_at > now
        }

    @staticmethod
    def _hash(value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()
