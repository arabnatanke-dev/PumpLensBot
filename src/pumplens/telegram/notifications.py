"""Persistent fan-out and rate-limited delivery. / Сохраняемый fan-out и доставка."""

from __future__ import annotations

import asyncio
import html
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNotFound,
    TelegramRetryAfter,
)
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

    def __init__(self, database: Database, *, early_shadow_mode: bool = True) -> None:
        self._database = database
        self._early_shadow_mode = early_shadow_mode

    async def __call__(self, transition: SignalTransition) -> None:
        async with self._database.session() as session, session.begin():
            signal_id = await SignalRepository(session).persist_transition(transition)
            if transition.to_state in {SignalState.NORMAL, SignalState.CANDIDATE}:
                return
            action = "EDIT"
            if transition.to_state is SignalState.EARLY:
                if self._early_shadow_mode:
                    return
                recipients = await self._early_recipients(session, transition)
                action = "SEND"
            elif transition.to_state is SignalState.WATCH:
                recipients = await self._existing_recipients(session, signal_id)
                if not recipients:
                    recipients = await self._watch_recipients(session, transition)
                    action = "SEND"
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
                            action=action,
                            status="PENDING",
                        )
                    )

    @staticmethod
    async def _early_recipients(
        session: AsyncSession,
        transition: SignalTransition,
    ) -> list[UserRecord]:
        return await TransitionFanout._eligible_recipients(
            session,
            transition,
            apply_min_score=False,
        )

    @staticmethod
    async def _watch_recipients(
        session: AsyncSession,
        transition: SignalTransition,
    ) -> list[UserRecord]:
        return await TransitionFanout._eligible_recipients(
            session,
            transition,
            apply_min_score=True,
        )

    @staticmethod
    async def _eligible_recipients(
        session: AsyncSession,
        transition: SignalTransition,
        *,
        apply_min_score: bool,
    ) -> list[UserRecord]:
        filters = [
            UserRecord.status == "ACTIVE",
            UserPreferenceRecord.paused.is_(False),
        ]
        if apply_min_score:
            filters.append(UserPreferenceRecord.min_score <= transition.snapshot.score)
        statement = (
            select(UserRecord, UserPreferenceRecord)
            .join(UserPreferenceRecord, UserPreferenceRecord.user_id == UserRecord.id)
            .where(*filters)
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
                DeliveryRecord.stage.in_([SignalState.EARLY.value, SignalState.WATCH.value]),
            )
            .distinct()
        )
        return list(await session.scalars(statement))

class DeliveryWorker:
    def __init__(
        self,
        database: Database,
        bot: Bot,
        public_base_url: str | None = None,
        signal_ttl_hours: int = 47,
    ) -> None:
        self._database = database
        self._bot = bot
        self._public_base_url = public_base_url
        self._signal_ttl = timedelta(hours=signal_ttl_hours)
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
            keyboard = signal_keyboard(signal.symbol, self._public_base_url)
            target_message_id = await self._signal_message_id(session, job)

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
            await self._mark(
                job_id,
                "SENT",
                None,
                message_id,
                delete_after=datetime.now(UTC) + self._signal_ttl,
            )
        except TelegramRetryAfter as exc:
            await asyncio.sleep(float(exc.retry_after))
            await self._retry(job_id, "telegram_rate_limit")
        except TelegramForbiddenError:
            await self._mark(job_id, "FAILED", "chat_forbidden")
        except Exception as exc:
            log.warning("telegram_delivery_failed", job_id=str(job_id), error=type(exc).__name__)
            await self._retry(job_id, type(exc).__name__)

    async def _signal_message_id(
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
                DeliveryRecord.stage.in_([SignalState.EARLY.value, SignalState.WATCH.value]),
                DeliveryRecord.status == "SENT",
            )
            .order_by(DeliveryRecord.created_at)
            .limit(1)
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
        delete_after: datetime | None = None,
    ) -> None:
        async with self._database.session() as session, session.begin():
            job = await session.get(DeliveryRecord, job_id)
            if job is not None:
                job.status = status
                job.error = error
                if message_id is not None:
                    job.message_id = message_id
                if delete_after is not None:
                    job.delete_after = delete_after

    async def _retry(self, job_id: uuid.UUID, error: str) -> None:
        async with self._database.session() as session, session.begin():
            job = await session.get(DeliveryRecord, job_id)
            if job is not None:
                job.retry_count += 1
                job.error = error[:128]
                job.status = "PENDING" if job.retry_count <= 5 else "FAILED"


class DeliveryCleanupWorker:
    """Delete expired signal messages from persistent jobs. / Удаляет просроченные сигналы."""

    def __init__(self, database: Database, bot: Bot, *, scan_seconds: float = 60.0) -> None:
        self._database = database
        self._bot = bot
        self._scan_seconds = scan_seconds

    async def run(self) -> None:
        while True:
            try:
                await self.cleanup_once()
            except Exception as exc:
                log.warning("telegram_cleanup_failed", error=type(exc).__name__)
            await asyncio.sleep(self._scan_seconds)

    async def cleanup_once(self, *, now: datetime | None = None) -> int:
        cutoff = now or datetime.now(UTC)
        async with self._database.session() as session:
            jobs = list(
                await session.scalars(
                    select(DeliveryRecord)
                    .where(
                        DeliveryRecord.message_id.is_not(None),
                        DeliveryRecord.status == "SENT",
                        DeliveryRecord.delete_after.is_not(None),
                        DeliveryRecord.delete_after <= cutoff,
                        DeliveryRecord.deleted_at.is_(None),
                    )
                    .order_by(DeliveryRecord.delete_after)
                    .limit(100)
                )
            )
        completed = 0
        for job in jobs:
            if job.message_id is None:
                continue
            error: str | None = None
            try:
                await self._bot.delete_message(job.chat_id, job.message_id)
            except (TelegramBadRequest, TelegramNotFound):
                # Missing messages are already in the desired final state.
                # Отсутствующее сообщение уже находится в нужном конечном состоянии.
                error = "already_deleted"
            except TelegramForbiddenError:
                error = "chat_forbidden"
            except Exception as exc:
                await self._record_cleanup_error(job.id, type(exc).__name__)
                continue
            await self._mark_deleted(job.id, cutoff, error)
            completed += 1
        return completed

    async def _mark_deleted(
        self,
        job_id: uuid.UUID,
        deleted_at: datetime,
        error: str | None,
    ) -> None:
        async with self._database.session() as session, session.begin():
            job = await session.get(DeliveryRecord, job_id)
            if job is not None and job.deleted_at is None:
                job.deleted_at = deleted_at
                job.cleanup_error = error

    async def _record_cleanup_error(self, job_id: uuid.UUID, error: str) -> None:
        async with self._database.session() as session, session.begin():
            job = await session.get(DeliveryRecord, job_id)
            if job is not None:
                job.cleanup_error = error[:128]


def format_signal(signal: SignalRecord, features: dict[str, Any]) -> str:
    direction_icon = "📈" if signal.direction == "LONG" else "📉"
    state_icons = {
        "EARLY": "⚡",
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
    if signal.state == SignalState.EARLY.value:
        directional_pressure = features.get("buy_pressure", 0.5)
        if signal.direction == "SHORT":
            directional_pressure = 1.0 - float(directional_pressure)
        return (
            f"⚡ <b>EARLY {signal.direction}</b> — <b>{html.escape(signal.symbol)}</b>\n\n"
            f"Цена: {float(features.get('return_1m', 0)):+.2f}% за минуту\n"
            f"Volume: {float(features.get('volume_ratio_1m', 0)):.1f}x\n"
            f"Trades: {float(features.get('trade_rate_ratio', 0)):.1f}x\n"
            f"Pressure: {float(directional_pressure):.0%}\n"
            f"Score: {float(signal.score):.0f}\n\n"
            "Очень ранняя аномалия. Подтверждения пока нет. "
            "Высокий риск ложного сигнала."
        )
    analysis = features.get("entry_analysis")
    if isinstance(analysis, dict):
        return _format_stage_c_signal(signal, features, analysis, icon, direction_icon)
    return (
        f"{icon} <b>{html.escape(signal.state)} {signal.direction}</b> — "
        f"<b>{html.escape(signal.symbol)}</b> {direction_icon}\n"
        f"Score: {float(signal.score):.0f}/100\n"
        f"Цена: {price}\n"
        f"Причины: {html.escape(reason_text)}\n"
        "⚠️ Это наблюдение, не команда на вход. Score не является вероятностью прибыли."
    )


def format_signal_explanation(
    signal: SignalRecord,
    features: dict[str, Any],
) -> str:
    """Explain the last deterministic snapshot. / Объясняет последний snapshot без LLM."""

    analysis = features.get("entry_analysis")
    if not isinstance(analysis, dict):
        reasons = features.get("reasons", [])
        penalties = features.get("penalties", [])
        return "\n".join(
            [
                f"<b>Почему {html.escape(signal.symbol)} {signal.direction}</b>",
                f"Signal Score: {float(signal.score):.0f}/100",
                "Stage C ещё не был рассчитан для последнего snapshot.",
                f"Подтверждения: {html.escape(', '.join(map(str, reasons)) or '—')}",
                f"Штрафы: {html.escape(', '.join(map(str, penalties)) or '—')}",
            ]
        )
    lines = [
        f"<b>Почему {html.escape(signal.symbol)} {signal.direction}</b>",
        f"Signal Score: {float(signal.score):.0f}/100",
        f"Entry Quality: {float(analysis.get('entry_quality', 0)):.0f}/100",
        "",
        *_market_lines(analysis),
        "",
        *_level_lines(analysis),
        "",
        *_setup_lines(analysis),
        "",
        f"Решение: {_decision_text(str(analysis.get('final_decision', 'WATCH')))}",
    ]
    positive = analysis.get("positive_reasons", [])
    negative = analysis.get("negative_reasons", [])
    if positive or negative:
        lines.append("Причины:")
        lines.extend(
            f"+ {_reason_text(str(code))}" for code in list(positive)[:6]
        )
        lines.extend(
            f"− {_reason_text(str(code))}" for code in list(negative)[:6]
        )
    lines.append("\n⚠️ Объяснение наблюдения, не команда на вход.")
    return "\n".join(lines)


def _format_stage_c_signal(
    signal: SignalRecord,
    features: dict[str, Any],
    analysis: dict[str, Any],
    icon: str,
    direction_icon: str,
) -> str:
    price = float(features.get("last_price", signal.trigger_price or signal.start_price or 0))
    lines = [
        f"{icon} <b>{html.escape(signal.state)} {signal.direction}</b> — "
        f"<b>{html.escape(signal.symbol)}</b> {direction_icon}",
        "",
        f"Signal Score: {float(signal.score):.0f}/100",
        f"Entry Quality: {float(analysis.get('entry_quality', 0)):.0f}/100",
        f"Цена: {price:g}",
        "",
        *_market_lines(analysis),
        "",
        *_level_lines(analysis),
        "",
        *_setup_lines(analysis),
        "",
        f"Decision: {_decision_text(str(analysis.get('final_decision', 'WATCH')))}",
    ]
    positive = list(analysis.get("positive_reasons", []))
    negative = list(analysis.get("negative_reasons", []))
    if positive or negative:
        lines.append("Причины:")
        lines.extend(f"+ {_reason_text(str(code))}" for code in positive[:3])
        lines.extend(f"− {_reason_text(str(code))}" for code in negative[:3])
    lines.append("\n⚠️ Наблюдение, не команда на вход. Score не является вероятностью прибыли.")
    return "\n".join(lines)


def _market_lines(analysis: dict[str, Any]) -> list[str]:
    one = analysis.get("structure_1m", {})
    five = analysis.get("structure_5m", {})
    fifteen = analysis.get("structure_15m", {})
    return [
        "<b>Market</b>",
        f"• 1m: {str(one.get('structure', '—')).lower()} · {one.get('pattern', '—')}",
        f"• 5m: {str(five.get('structure', '—')).lower()} · {five.get('pattern', '—')}",
        f"• 15m: {str(fifteen.get('structure', '—')).lower()}",
        f"• Alignment: {float(analysis.get('trend_alignment_score', 0)):+.0f}",
    ]


def _level_lines(analysis: dict[str, Any]) -> list[str]:
    room_up = analysis.get("room_up_pct")
    room_down = analysis.get("room_down_pct")
    room_up_text = "—" if room_up is None else f"{float(room_up):.2f}%"
    room_down_text = "—" if room_down is None else f"{float(room_down):.2f}%"
    return [
        "<b>Levels</b>",
        f"• Support: {_zone_text(analysis.get('nearest_support'))}",
        f"• Resistance: {_zone_text(analysis.get('nearest_resistance'))}",
        f"• Room up/down: {room_up_text} / {room_down_text}",
    ]


def _setup_lines(analysis: dict[str, Any]) -> list[str]:
    rr = analysis.get("rr")
    rr_text = "—" if rr is None else f"{float(rr):.2f}"
    return [
        "<b>Setup</b>",
        f"• Breakout: {str(analysis.get('breakout_state', 'NONE')).lower()}",
        f"• Retest: {str(analysis.get('retest_state', 'NOT_APPLICABLE')).lower()}",
        f"• Late: {'yes' if analysis.get('late') else 'no'} "
        f"({float(analysis.get('late_score', 0)):.0f}/100)",
        f"• Exhaustion: {float(analysis.get('exhaustion_score', 0)):.0f}/100",
        f"• Invalidation: {float(analysis.get('invalidation_price', 0)):g}",
        f"• Potential target: {float(analysis.get('potential_target', 0)):g}",
        f"• R:R: {rr_text}",
    ]


def _zone_text(value: object) -> str:
    if not isinstance(value, dict):
        return "—"
    return f"{float(value.get('low', 0)):g}–{float(value.get('high', 0)):g}"


def _decision_text(code: str) -> str:
    labels = {
        "ENTER_CANDIDATE": "🟢 setup подтверждён для наблюдения",
        "WAIT_RETEST": "🟠 лучше дождаться retest",
        "WATCH": "🟡 наблюдать",
        "SKIP_LATE": "⚫ слишком поздно",
        "SKIP_BAD_RR": "⚫ пропустить: слабый R:R",
        "SKIP_RESISTANCE_TOO_CLOSE": "⚫ сопротивление слишком близко",
        "SKIP_SUPPORT_TOO_CLOSE": "⚫ поддержка слишком близко",
        "SKIP_EXHAUSTION": "⚫ импульс выглядит истощённым",
        "SKIP_STRUCTURE_CONFLICT": "⚫ конфликт структуры",
        "INVALIDATED": "🔴 сценарий сломан",
    }
    return labels.get(code, html.escape(code))


def _reason_text(code: str) -> str:
    labels = {
        "STRUCTURE_ALIGNED": "структура совпадает с направлением",
        "BREAKOUT_CONFIRMED": "пробой подтверждён потоком",
        "RETEST_HELD": "уровень удержан на retest",
        "VOLUME_CONFIRMED": "объём расширился",
        "TRADES_CONFIRMED": "частота сделок выросла",
        "PRESSURE_CONFIRMED": "агрессивный поток подтверждает направление",
        "OI_CONFIRMED": "OI подтверждает движение",
        "SPREAD_HEALTHY": "спред остаётся нормальным",
        "DEPTH_SUPPORTIVE": "стакан поддерживает направление",
        "ROOM_AVAILABLE": "до следующего уровня есть пространство",
        "RR_ACCEPTABLE": "гипотетический R:R приемлем",
        "STRUCTURE_CONFLICT": "старший timeframe против направления",
        "FAILED_BREAKOUT": "цена вернулась за пробитый уровень",
        "RETEST_FAILED": "retest не удержал уровень",
        "LEVEL_TOO_CLOSE": "следующий уровень слишком близко",
        "BAD_RR": "риск велик относительно цели",
        "LATE_ENTRY": "движение далеко ушло относительно ATR",
        "MOMENTUM_EXHAUSTED": "есть признаки истощения импульса",
        "TOO_FAR_FROM_VWAP": "цена далеко от VWAP",
        "LARGE_OPPOSING_WICK": "большая встречная тень",
        "OI_DIVERGENCE": "OI расходится с направлением",
        "THIN_DIRECTIONAL_DEPTH": "стакан слаб в направлении движения",
    }
    return labels.get(code, html.escape(code.replace("_", " ").lower()))


def signal_keyboard(symbol: str, public_base_url: str | None = None) -> InlineKeyboardMarkup:
    target = f"https://www.binance.com/en/futures/{symbol}"
    if public_base_url:
        target = f"{public_base_url.rstrip('/')}/go/binance/{symbol}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Открыть Binance",
                    url=target,
                )
            ]
        ]
    )
