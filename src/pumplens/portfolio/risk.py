"""Portfolio-aware risk detection and delivery. / Риск-анализ портфеля и доставка."""

from __future__ import annotations

import asyncio
import html
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import structlog
from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from sqlalchemy import select

from pumplens.storage.db import Database
from pumplens.storage.models import (
    ExchangeAccountRecord,
    PositionRecord,
    RiskAlertRecord,
    SignalRecord,
    SpotHoldingRecord,
    UserRecord,
)
from pumplens.telegram.notifications import signal_keyboard

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RiskCondition:
    user_id: uuid.UUID
    account_id: uuid.UUID
    chat_id: int
    symbol: str | None
    alert_type: str
    dedupe_key: str
    message: str


class PortfolioRiskMonitor:
    """Continuously derives re-armable conditions from DB state. / Вычисляет риск-состояния."""

    def __init__(
        self,
        database: Database,
        *,
        interval_seconds: float = 5.0,
        stale_after_seconds: int = 600,
        liquidation_warning_pct: float = 5.0,
        pnl_milestones_pct: tuple[float, ...] = (5.0, 8.0, 10.0),
    ) -> None:
        self._database = database
        self._interval_seconds = interval_seconds
        self._stale_after_seconds = stale_after_seconds
        self._liquidation_warning_pct = liquidation_warning_pct
        self._pnl_milestones = tuple(sorted(pnl_milestones_pct))

    async def run(self) -> None:
        while True:
            try:
                await self.scan_once()
            except Exception as exc:
                log.warning("portfolio_risk_scan_failed", error=type(exc).__name__)
            await asyncio.sleep(self._interval_seconds)

    async def scan_once(self) -> None:
        conditions = await self._conditions()
        by_key = {condition.dedupe_key: condition for condition in conditions}
        async with self._database.session() as session, session.begin():
            existing = list(await session.scalars(select(RiskAlertRecord)))
            existing_by_key = {alert.dedupe_key: alert for alert in existing}
            for key, condition in by_key.items():
                alert = existing_by_key.get(key)
                if alert is None:
                    session.add(
                        RiskAlertRecord(
                            user_id=condition.user_id,
                            exchange_account_id=condition.account_id,
                            chat_id=condition.chat_id,
                            symbol=condition.symbol,
                            alert_type=condition.alert_type,
                            dedupe_key=condition.dedupe_key,
                            message=condition.message,
                            status="PENDING",
                            active=True,
                        )
                    )
                else:
                    alert.message = condition.message
                    if not alert.active:
                        alert.active = True
                        alert.status = "PENDING"
                        alert.retry_count = 0
                        alert.error = None
            for alert in existing:
                if alert.active and alert.dedupe_key not in by_key:
                    alert.active = False
                    alert.status = "RESOLVED"

    async def _conditions(self) -> list[RiskCondition]:
        now = datetime.now(UTC)
        result: list[RiskCondition] = []
        async with self._database.session() as session:
            accounts = (
                await session.execute(
                    select(ExchangeAccountRecord, UserRecord)
                    .join(UserRecord, UserRecord.id == ExchangeAccountRecord.user_id)
                    .where(ExchangeAccountRecord.status != "DISCONNECTED")
                )
            ).all()
            signals = list(
                await session.scalars(
                    select(SignalRecord).where(
                        SignalRecord.is_active.is_(True),
                        SignalRecord.state.in_(["WATCH", "CONFIRMED"]),
                    )
                )
            )
            for account, user in accounts:
                positions = list(
                    await session.scalars(
                        select(PositionRecord).where(
                            PositionRecord.exchange_account_id == account.id
                        )
                    )
                )
                holdings = list(
                    await session.scalars(
                        select(SpotHoldingRecord).where(
                            SpotHoldingRecord.exchange_account_id == account.id
                        )
                    )
                )
                result.extend(
                    self._account_conditions(account, user, positions, holdings, signals, now)
                )
        return result

    def _account_conditions(
        self,
        account: ExchangeAccountRecord,
        user: UserRecord,
        positions: list[PositionRecord],
        holdings: list[SpotHoldingRecord],
        signals: list[SignalRecord],
        now: datetime,
    ) -> list[RiskCondition]:
        result: list[RiskCondition] = []
        last_sync = _aware(account.last_sync_at)
        created_at = _aware(account.created_at)
        never_synced_too_long = (
            last_sync is None
            and created_at is not None
            and (now - created_at).total_seconds() > self._stale_after_seconds
        )
        stale = account.status == "STALE" or never_synced_too_long or (
            last_sync is not None
            and (now - last_sync).total_seconds() > self._stale_after_seconds
        )
        if stale:
            result.append(
                _condition(
                    account,
                    user,
                    None,
                    "STALE_PORTFOLIO",
                    f"stale:{account.id}",
                    "⚠️ <b>STALE PORTFOLIO</b>\nДанные Binance устарели. "
                    "Новые портфельные сигналы временно не считаются надёжными.",
                )
            )
            # Do not derive risk claims from stale balances or positions.
            # Не строим риск-выводы по устаревшим балансам и позициям.
            return result

        for position in positions:
            distance = _liquidation_distance_pct(position)
            if distance is not None and 0 <= distance <= self._liquidation_warning_pct:
                result.append(
                    _condition(
                        account,
                        user,
                        position.symbol,
                        "MARGIN_RISK",
                        f"margin:{account.id}:{position.symbol}:{position.side}",
                        f"🚨 <b>MARGIN RISK</b> — {html.escape(position.symbol)}\n"
                        f"До ликвидации примерно {distance:.2f}% · "
                        f"{position.side} x{position.leverage}.",
                    )
                )
            roi = _position_roi_pct(position)
            milestone = _milestone(roi, self._pnl_milestones)
            if milestone is not None:
                label = f"+{milestone:g}" if milestone > 0 else f"{milestone:g}"
                result.append(
                    _condition(
                        account,
                        user,
                        position.symbol,
                        "PNL_MILESTONE",
                        f"pnl:{account.id}:{position.symbol}:{position.side}:{label}",
                        f"💹 <b>PnL {label}%</b> — {html.escape(position.symbol)}\n"
                        f"Текущая оценка ROI: {roi:+.2f}% · PnL {position.pnl:+.2f} USDT.",
                    )
                )

        holding_assets = {
            holding.asset for holding in holdings if holding.free + holding.locked > 0
        }
        for signal in signals:
            base_asset = _base_asset(signal.symbol)
            if base_asset in holding_assets:
                result.append(
                    _condition(
                        account,
                        user,
                        signal.symbol,
                        "PORTFOLIO_MATCH",
                        f"match:{account.id}:{signal.id}",
                        f"🎯 <b>PORTFOLIO MATCH</b> — {html.escape(signal.symbol)}\n"
                        "Актив есть в вашем Spot-портфеле; сигнал "
                        f"{signal.state} {signal.direction}.",
                    )
                )
            for position in positions:
                if position.symbol == signal.symbol and position.side != signal.direction:
                    result.append(
                        _condition(
                            account,
                            user,
                            signal.symbol,
                            "POSITION_RISK",
                            f"opposite:{account.id}:{signal.id}:{position.side}",
                            f"⚡️ <b>POSITION RISK</b> — {html.escape(signal.symbol)}\n"
                            f"Открыта {position.side}, а PumpLens показывает "
                            f"{signal.state} {signal.direction}.",
                        )
                    )
        return result


class RiskAlertWorker:
    def __init__(self, database: Database, bot: Bot, public_base_url: str | None) -> None:
        self._database = database
        self._bot = bot
        self._public_base_url = public_base_url

    async def run(self) -> None:
        while True:
            alert_id = await self._claim_one()
            if alert_id is None:
                await asyncio.sleep(0.5)
                continue
            await self._deliver(alert_id)

    async def _claim_one(self) -> uuid.UUID | None:
        async with self._database.session() as session, session.begin():
            alert = await session.scalar(
                select(RiskAlertRecord)
                .where(RiskAlertRecord.status == "PENDING", RiskAlertRecord.active.is_(True))
                .order_by(RiskAlertRecord.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if alert is None:
                return None
            alert.status = "PROCESSING"
            return alert.id

    async def _deliver(self, alert_id: uuid.UUID) -> None:
        async with self._database.session() as session:
            alert = await session.get(RiskAlertRecord, alert_id)
            if alert is None:
                return
            chat_id = alert.chat_id
            message = alert.message
            keyboard = (
                signal_keyboard(alert.symbol, self._public_base_url) if alert.symbol else None
            )
        try:
            await self._bot.send_message(chat_id, message, reply_markup=keyboard)
            await self._mark(alert_id, "SENT", None)
        except TelegramRetryAfter as exc:
            await asyncio.sleep(float(exc.retry_after))
            await self._retry(alert_id, "telegram_rate_limit")
        except TelegramForbiddenError:
            await self._mark(alert_id, "FAILED", "chat_forbidden")
        except Exception as exc:
            log.warning("risk_alert_delivery_failed", error=type(exc).__name__)
            await self._retry(alert_id, type(exc).__name__)

    async def _mark(self, alert_id: uuid.UUID, status: str, error: str | None) -> None:
        async with self._database.session() as session, session.begin():
            alert = await session.get(RiskAlertRecord, alert_id)
            if alert is not None:
                alert.status = status
                alert.error = error
                if status == "SENT":
                    alert.last_sent_at = datetime.now(UTC)

    async def _retry(self, alert_id: uuid.UUID, error: str) -> None:
        async with self._database.session() as session, session.begin():
            alert = await session.get(RiskAlertRecord, alert_id)
            if alert is not None:
                alert.retry_count += 1
                alert.error = error[:128]
                alert.status = "PENDING" if alert.retry_count <= 5 else "FAILED"


def _condition(
    account: ExchangeAccountRecord,
    user: UserRecord,
    symbol: str | None,
    alert_type: str,
    dedupe_key: str,
    message: str,
) -> RiskCondition:
    return RiskCondition(user.id, account.id, user.chat_id, symbol, alert_type, dedupe_key, message)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _liquidation_distance_pct(position: PositionRecord) -> float | None:
    if position.liquidation is None or position.mark <= 0:
        return None
    if position.side == "LONG":
        return float((position.mark - position.liquidation) / position.mark * Decimal(100))
    return float((position.liquidation - position.mark) / position.mark * Decimal(100))


def _position_roi_pct(position: PositionRecord) -> float:
    notional = abs(position.entry * position.amount)
    if notional <= 0 or position.leverage <= 0:
        return 0.0
    margin = notional / Decimal(position.leverage)
    return float(position.pnl / margin * Decimal(100))


def _milestone(value: float, levels: tuple[float, ...]) -> float | None:
    sign = 1.0 if value >= 0 else -1.0
    reached = [level for level in levels if abs(value) >= level]
    return sign * max(reached) if reached else None


def _base_asset(symbol: str) -> str:
    for quote in ("USDT", "USDC", "FDUSD", "BUSD"):
        if symbol.endswith(quote):
            return symbol[: -len(quote)]
    return symbol
