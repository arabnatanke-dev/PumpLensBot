"""Idempotent persistence operations. / Идемпотентные операции хранения."""

from __future__ import annotations

import base64
import dataclasses
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pumplens.analytics.fsm import SignalTransition
from pumplens.domain.enums import SignalState
from pumplens.security.credential_vault import EncryptedCredentials
from pumplens.storage.models import (
    EncryptedCredentialRecord,
    ExchangeAccountRecord,
    SignalEventRecord,
    SignalFeatureRecord,
    SignalRecord,
    UserPreferenceRecord,
    UserRecord,
)


class SignalRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def persist_transition(self, transition: SignalTransition) -> uuid.UUID:
        statement = select(SignalRecord).where(
            SignalRecord.symbol == transition.symbol,
            SignalRecord.direction == transition.direction.value,
            SignalRecord.is_active.is_(True),
        )
        signal = await self._session.scalar(statement)
        if signal is None:
            signal = SignalRecord(
                symbol=transition.symbol,
                direction=transition.direction.value,
                state=transition.to_state.value,
                score=transition.snapshot.score,
                start_price=_decimal(transition.levels.start_price)
                if transition.levels
                else _decimal(transition.snapshot.last_price),
                is_active=True,
            )
            self._session.add(signal)
            await self._session.flush()

        signal.state = transition.to_state.value
        signal.score = transition.snapshot.score
        if transition.to_state is SignalState.EARLY:
            signal.early_price = _decimal(transition.snapshot.last_price)
            signal.early_liquidity_tier = transition.liquidity_tier
        if transition.levels is not None:
            signal.start_price = _decimal(transition.levels.start_price)
            signal.trigger_price = _decimal(transition.levels.trigger_price)
            signal.invalidation = _decimal(transition.levels.invalidation)
            signal.late_line = _decimal(transition.levels.late_line_5m)
        self._apply_transition_time(signal, transition)

        self._session.add(
            SignalEventRecord(
                signal_id=signal.id,
                from_state=transition.from_state.value,
                to_state=transition.to_state.value,
                reason_code=transition.reason_code,
                reason_text=transition.reason_text,
                ts=transition.timestamp,
            )
        )
        self._session.add(
            SignalFeatureRecord(
                signal_id=signal.id,
                ts=transition.timestamp,
                features_json=_snapshot_dict(transition),
                penalties_json=list(transition.snapshot.penalties),
                raw_score=transition.snapshot.score,
            )
        )
        await self._session.flush()
        return signal.id

    @staticmethod
    def _apply_transition_time(signal: SignalRecord, transition: SignalTransition) -> None:
        if transition.to_state is SignalState.CANDIDATE:
            signal.candidate_at = transition.timestamp
        elif transition.to_state is SignalState.EARLY:
            signal.early_at = transition.timestamp
        elif transition.to_state is SignalState.WATCH:
            signal.watch_at = transition.timestamp
        elif transition.to_state is SignalState.CONFIRMED:
            signal.confirmed_at = transition.timestamp
        elif transition.to_state in {
            SignalState.INVALIDATED,
            SignalState.TOO_LATE,
            SignalState.COOLDOWN,
        }:
            signal.terminal_at = transition.timestamp
            if transition.to_state is SignalState.COOLDOWN:
                signal.is_active = False


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_or_create(
        self,
        *,
        telegram_user_id: int,
        chat_id: int,
        display_name: str,
        language: str,
    ) -> UserRecord:
        user = await self._session.scalar(
            select(UserRecord).where(UserRecord.telegram_user_id == telegram_user_id)
        )
        if user is not None:
            user.chat_id = chat_id
            user.display_name = display_name
            return user
        user = UserRecord(
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
            display_name=display_name,
            language=language,
        )
        self._session.add(user)
        await self._session.flush()
        self._session.add(UserPreferenceRecord(user_id=user.id))
        await self._session.flush()
        return user


class CredentialRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def connect_binance(
        self,
        *,
        exchange_account_id: uuid.UUID,
        user_id: uuid.UUID,
        label: str,
        permissions: Mapping[str, object],
        encrypted: EncryptedCredentials,
    ) -> ExchangeAccountRecord:
        account = await self._session.scalar(
            select(ExchangeAccountRecord).where(
                ExchangeAccountRecord.user_id == user_id,
                ExchangeAccountRecord.exchange == "BINANCE",
            )
        )
        if account is None:
            account = ExchangeAccountRecord(
                id=exchange_account_id,
                user_id=user_id,
                exchange="BINANCE",
                label=label,
                status="ACTIVE",
                permissions_json=dict(permissions),
                key_last4=encrypted.api_key_last4,
            )
            self._session.add(account)
            await self._session.flush()
        else:
            account.label = label
            account.status = "ACTIVE"
            account.permissions_json = dict(permissions)
            account.key_last4 = encrypted.api_key_last4

        credential = await self._session.get(EncryptedCredentialRecord, account.id)
        ciphertext = base64.b64decode(encrypted.ciphertext_b64)
        nonce = base64.b64decode(encrypted.nonce_b64)
        if credential is None:
            credential = EncryptedCredentialRecord(
                exchange_account_id=account.id,
                ciphertext=ciphertext,
                nonce=nonce,
                key_version=encrypted.key_version,
            )
            self._session.add(credential)
        else:
            credential.ciphertext = ciphertext
            credential.nonce = nonce
            credential.key_version = encrypted.key_version
        await self._session.flush()
        return account


def _decimal(value: float) -> Decimal:
    return Decimal(str(value))


def _snapshot_dict(transition: SignalTransition) -> dict[str, object]:
    raw = dataclasses.asdict(transition.snapshot)
    raw["timestamp"] = transition.snapshot.timestamp.isoformat()
    raw["direction"] = transition.snapshot.direction.value
    raw["data_quality"] = transition.snapshot.data_quality.value
    return raw


def utc_now() -> datetime:
    return datetime.now(UTC)
