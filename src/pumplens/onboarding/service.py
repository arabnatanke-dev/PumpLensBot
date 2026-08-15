"""Persistent button-first onboarding state. / Сохраняемая кнопочная регистрация."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pumplens.onboarding.invites import InviteError, InviteService
from pumplens.storage.models import (
    UserConsentRecord,
    UserPreferenceRecord,
    UserRecord,
)
from pumplens.storage.repositories import UserRepository

ONBOARDING_STATES = ("WELCOME", "CONSENT", "PROFILE", "DIRECTIONS", "BINANCE", "COMPLETE")


class OnboardingError(ValueError):
    """Invalid or out-of-order onboarding action. / Неверный шаг регистрации."""


class OnboardingService:
    def __init__(self, invite_service: InviteService | None = None) -> None:
        self._invites = invite_service or InviteService()

    async def start(
        self,
        session: AsyncSession,
        *,
        telegram_user_id: int,
        chat_id: int,
        display_name: str,
        language: str,
        invite_code: str | None,
        invite_only: bool = True,
        bypass_invite: bool = False,
    ) -> UserRecord:
        existing = await session.scalar(
            select(UserRecord).where(UserRecord.telegram_user_id == telegram_user_id)
        )
        if existing is not None:
            existing.chat_id = chat_id
            return existing
        if invite_only and not bypass_invite:
            if not invite_code:
                raise InviteError("invite_required")
            await self._invites.redeem(session, invite_code)
        return await UserRepository(session).get_or_create(
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
            display_name=display_name,
            language=language,
        )

    async def accept_consent(
        self,
        session: AsyncSession,
        user: UserRecord,
        *,
        privacy_version: str,
        terms_version: str,
        telegram_metadata: dict[str, object],
    ) -> None:
        metadata_hash = hashlib.sha256(
            json.dumps(telegram_metadata, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        session.add(
            UserConsentRecord(
                user_id=user.id,
                privacy_version=privacy_version,
                terms_version=terms_version,
                accepted_at=datetime.now(UTC),
                telegram_metadata_hash=metadata_hash,
            )
        )
        user.onboarding_state = "PROFILE"

    async def set_profile(
        self,
        session: AsyncSession,
        user: UserRecord,
        profile: str,
    ) -> None:
        if profile not in {"safe", "balanced", "wild"}:
            raise OnboardingError("invalid_profile")
        preference = await self._preferences(session, user.id)
        preference.profile = profile
        user.onboarding_state = "DIRECTIONS"

    async def set_directions(
        self,
        session: AsyncSession,
        user: UserRecord,
        directions: list[str],
    ) -> None:
        normalized = sorted(set(directions))
        if not normalized or not set(normalized) <= {"LONG", "SHORT"}:
            raise OnboardingError("invalid_directions")
        preference = await self._preferences(session, user.id)
        preference.directions = normalized
        user.onboarding_state = "BINANCE"

    @staticmethod
    def complete(user: UserRecord) -> None:
        user.onboarding_state = "COMPLETE"
        user.status = "ACTIVE"

    @staticmethod
    async def _preferences(
        session: AsyncSession,
        user_id: object,
    ) -> UserPreferenceRecord:
        preference = await session.scalar(
            select(UserPreferenceRecord).where(UserPreferenceRecord.user_id == user_id)
        )
        if preference is None:
            raise OnboardingError("preferences_missing")
        return preference
