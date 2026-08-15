"""Persistent fan-out and rate-limited delivery. / Сохраняемый fan-out и доставка."""

from __future__ import annotations

import asyncio
import html
import time
import uuid
from typing import Any

import structlog
from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pumplens.analytics.fsm import SignalTransition
from pumplens.domain.enums import SignalState
from pumplens.storage.db import Database
from pumplens.storage.models import (
    DeliveryRecord,
    SignalFeatureRecord,
    SignalRecord,
    UserPreferenceRecord,
    UserRecord,
)
from pumplens.storage.repositories import SignalRepository

log = structlog.get_logger(__name__)


class TransitionFanout:
    """Persist, then enqueue per-user delivery. / Сохраняет и ставит в очередь."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def __call__(self, transition: SignalTransition) -> None:
        async with self._database.session() as session, session.begin():
            signal_id = await SignalRepository(session).persist_transition(transition)
            if transition.to_state is SignalState.CANDIDATE:
                return
            if transition.to_state is SignalState.WATCH:
                recipients = await self._watch_recipients(session, transition)
            else:
                recipients = await self._existing_recipients(session, signal_id)
            for user in recipients:
                exists = await session.scalar(
                    select(DeliveryRecord.id).where(
                        DeliveryRecord.signal_id == signal_id,
                        DeliveryRecord.user_id == user.id,
                        DeliveryRecord.stage == transition.to_state.value,
                    )
                )
                if exists is None:
                    session.add(
                        DeliveryRecord(
                            signal_id=signal_id,
                            user_id=user.id,
                            chat_id=user.chat_id,
                            stage=transition.to_state.value,
                            action="SEND"
                            if transition.to_state is SignalState.WATCH
                            else "EDIT",
                            status="PENDING",
                        )
                    )

    @staticmethod
    async def _watch_recipients(
        session: AsyncSession,
        transition: SignalTransition,
    ) -> list[UserRecord]:
        statement = (
            select(UserRecord, UserPreferenceRecord)
            .join(UserPreferenceRecord, UserPreferenceRecord.user_id == UserRecord.id)
            .where(
                UserRecord.status == "ACTIVE",
                UserPreferenceRecord.paused.is_(False),
                UserPreferenceRecord.min_score <= transition.snapshot.score,
            )
        )
        rows = (await session.execute(statement)).all()
        return [
            user
            for user, preference in rows
            if transition.direction.value in preference.directions
        ]

    @staticmethod
    async def _existing_recipients(
        session: AsyncSession,
        signal_id: uuid.UUID,
    ) -> list[UserRecord]:
        statement = (
            select(UserRecord)
            .join(DeliveryRecord, DeliveryRecord.user_id == UserRecord.id)
            .where(
                DeliveryRecord.signal_id == signal_id,
                DeliveryRecord.stage == SignalState.WATCH.value,
            )
            .distinct()
        )
        return list(await session.scalars(statement))


class DeliveryWorker:
    def __init__(self, database: Database, bot: Bot) -> None:
        self._database = database
        self._bot = bot
        self._last_chat_delivery: dict[int, float] = {}

    async def run(self) -> None:
        while True:
            job_id = await self._claim_one()
            if job_id is None:
                await asyncio.sleep(0.5)
                continue
            await self._deliver(job_id)

    async def _claim_one(self) -> uuid.UUID | None:
        async with self._database.session() as session, session.begin():
            job = await session.scalar(
                select(DeliveryRecord)
                .where(DeliveryRecord.status == "PENDING")
                .order_by(DeliveryRecord.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if job is None:
                return None
            job.status = "PROCESSING"
            return job.id

    async def _deliver(self, job_id: uuid.UUID) -> None:
        async with self._database.session() as session:
            job = await session.get(DeliveryRecord, job_id)
            if job is None:
                return
            signal = await session.get(SignalRecord, job.signal_id)
            if signal is None:
                await self._mark(job_id, "FAILED", "signal_missing")
                return
            feature = await session.scalar(
                select(SignalFeatureRecord)
                .where(SignalFeatureRecord.signal_id == signal.id)
                .order_by(SignalFeatureRecord.ts.desc())
                .limit(1)
            )
            text = format_signal(signal, feature.features_json if feature else {})
            keyboard = signal_keyboard(signal.symbol)
            target_message_id = await self._watch_message_id(session, job)

        await self._respect_chat_rate(job.chat_id)
        try:
            if job.action == "EDIT" and target_message_id is not None:
                await self._bot.edit_message_text(
                    text,
                    chat_id=job.chat_id,
                    message_id=target_message_id,
                    reply_markup=keyboard,
                )
                message_id = target_message_id
            else:
                message = await self._bot.send_message(
                    job.chat_id,
                    text,
                    reply_markup=keyboard,
                )
                message_id = message.message_id
            await self._mark(job_id, "SENT", None, message_id)
        except TelegramRetryAfter as exc:
            await asyncio.sleep(float(exc.retry_after))
            await self._retry(job_id, "telegram_rate_limit")
        except TelegramForbiddenError:
            await self._mark(job_id, "FAILED", "chat_forbidden")
        except Exception as exc:
            log.warning("telegram_delivery_failed", job_id=str(job_id), error=type(exc).__name__)
            await self._retry(job_id, type(exc).__name__)

    async def _watch_message_id(
        self,
        session: AsyncSession,
        job: DeliveryRecord,
    ) -> int | None:
        if job.action != "EDIT":
            return None
        return await session.scalar(
            select(DeliveryRecord.message_id).where(
                DeliveryRecord.signal_id == job.signal_id,
                DeliveryRecord.user_id == job.user_id,
                DeliveryRecord.stage == SignalState.WATCH.value,
                DeliveryRecord.status == "SENT",
            )
        )

    async def _respect_chat_rate(self, chat_id: int) -> None:
        now = time.monotonic()
        delay = 1.0 - (now - self._last_chat_delivery.get(chat_id, 0.0))
        if delay > 0:
            await asyncio.sleep(delay)
        self._last_chat_delivery[chat_id] = time.monotonic()

    async def _mark(
        self,
        job_id: uuid.UUID,
        status: str,
        error: str | None,
        message_id: int | None = None,
    ) -> None:
        async with self._database.session() as session, session.begin():
            job = await session.get(DeliveryRecord, job_id)
            if job is not None:
                job.status = status
                job.error = error
                if message_id is not None:
                    job.message_id = message_id

    async def _retry(self, job_id: uuid.UUID, error: str) -> None:
        async with self._database.session() as session, session.begin():
            job = await session.get(DeliveryRecord, job_id)
            if job is not None:
                job.retry_count += 1
                job.error = error[:128]
                job.status = "PENDING" if job.retry_count <= 5 else "FAILED"


def format_signal(signal: SignalRecord, features: dict[str, Any]) -> str:
    direction_icon = "📈" if signal.direction == "LONG" else "📉"
    state_icons = {
        "WATCH": "🟡",
        "CONFIRMED": "🟢",
        "INVALIDATED": "🔴",
        "TOO_LATE": "⚠️",
        "COOLDOWN": "🔵",
    }
    icon = state_icons.get(signal.state, "⚪️")
    price = features.get("last_price", signal.trigger_price or signal.start_price or 0)
    reasons = features.get("reasons", [])
    reason_text = ", ".join(str(value) for value in reasons[:4]) or "сбор подтверждений"
    return (
        f"{icon} <b>{html.escape(signal.state)} {signal.direction}</b> — "
        f"<b>{html.escape(signal.symbol)}</b> {direction_icon}\n"
        f"Score: {float(signal.score):.0f}/100\n"
        f"Цена: {price}\n"
        f"Причины: {html.escape(reason_text)}\n"
        "⚠️ Это наблюдение, не команда на вход. Score не является вероятностью прибыли."
    )


def signal_keyboard(symbol: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Открыть Binance",
                    url=f"https://www.binance.com/en/futures/{symbol}",
                )
            ]
        ]
    )
