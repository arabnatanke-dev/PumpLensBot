"""Hashed invite-code lifecycle. / Жизненный цикл хешированных invite-кодов."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pumplens.storage.models import InviteCodeRecord


class InviteError(ValueError):
    """Expected invite rejection. / Ожидаемое отклонение invite-кода."""


class InviteService:
    @staticmethod
    def hash_code(code: str) -> str:
        return hashlib.sha256(code.encode()).hexdigest()

    async def create(
        self,
        session: AsyncSession,
        *,
        created_by: uuid.UUID | None,
        ttl_hours: int = 24,
        max_uses: int = 1,
    ) -> str:
        raw_code = secrets.token_urlsafe(24)
        session.add(
            InviteCodeRecord(
                code_hash=self.hash_code(raw_code),
                expires_at=datetime.now(UTC) + timedelta(hours=ttl_hours),
                max_uses=max_uses,
                created_by=created_by,
            )
        )
        await session.flush()
        # The plaintext is returned once and never persisted.
        # Plaintext возвращается один раз и никогда не сохраняется.
        return raw_code

    async def redeem(self, session: AsyncSession, raw_code: str) -> InviteCodeRecord:
        statement = (
            select(InviteCodeRecord)
            .where(InviteCodeRecord.code_hash == self.hash_code(raw_code))
            .with_for_update()
        )
        invite = await session.scalar(statement)
        now = datetime.now(UTC)
        if invite is None:
            raise InviteError("invite_not_found")
        expires_at = invite.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if invite.disabled_at is not None or expires_at <= now:
            raise InviteError("invite_expired")
        if invite.used_count >= invite.max_uses:
            raise InviteError("invite_limit_reached")
        invite.used_count += 1
        await session.flush()
        return invite
