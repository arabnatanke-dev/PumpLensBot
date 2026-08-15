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

from pumplens.portfolio.live_marks import LiveMarkPriceStore
from pumplens.storage.db import Database
from pumplens.storage.models import (
    ExchangeAccountRecord,
    PositionRecord,
    PositionRiskStateRecord,
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
        live_marks: LiveMarkPriceStore | None = None,
        live_mark_stale_seconds: float = 5.0,
    ) -> None:
        self._database = database
        self._interval_seconds = interval_seconds
        self._stale_after_seconds = stale_after_seconds
        self._liquidation_warning_pct = liquidation_warning_pct
        self._pnl_milestones = tuple(sorted(pnl_milestones_pct))
        self._live_marks = live_marks
        self._live_mark_stale_seconds = live_mark_stale_seconds

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
        async with self._database.session() as session, session.begin():
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
            risk_states = list(await session.scalars(select(PositionRiskStateRecord)))
            risk_state_by_key = {
                (row.exchange_account_id, row.symbol, row.side): row for row in risk_states
            }
            active_position_keys: set[tuple[uuid.UUID, str, str]] = set()
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
                account_states: dict[tuple[str, str], PositionRiskStateRecord] = {}
                for position in positions:
                    key = (account.id, position.symbol, position.side)
                    active_position_keys.add(key)
                    state = risk_state_by_key.get(key)
                    if state is None:
                        state = PositionRiskStateRecord(
                            exchange_account_id=account.id,
                            symbol=position.symbol,
                            side=position.side,
                            max_profit_milestone=0,
                            max_loss_milestone=0,
                            last_roi_pct=0,
                        )
                        session.add(state)
                        risk_state_by_key[key] = state
                    account_states[(position.symbol, position.side)] = state
                result.extend(
                    self._account_conditions(
                        account,
                        user,
                        positions,
                        holdings,
                        signals,
                        account_states,
                        now,
                    )
                )
            # A missing position starts a new milestone lifetime on the next open.
            # Отсутствующая позиция начинает новую жизнь порогов при следующем открытии.
            for state in risk_states:
                key = (state.exchange_account_id, state.symbol, state.side)
                if key not in active_position_keys:
                    await session.delete(state)
        return result

    def _account_conditions(
        self,
        account: ExchangeAccountRecord,
        user: UserRecord,
        positions: list[PositionRecord],
        holdings: list[SpotHoldingRecord],
        signals: list[SignalRecord],
        risk_states: dict[tuple[str, str], PositionRiskStateRecord],
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
            mark = self._current_mark(position)
            if mark is None:
                # REST mark/PnL may be minutes old; silence is safer than a stale risk claim.
                # REST mark/PnL могут быть старыми; лучше пропустить, чем дать ложный риск.
                continue
            distance = _liquidation_distance_pct(position, mark)
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
            pnl = _unrealized_pnl(position, mark)
            roi = _position_roi_pct(position, mark)
            state = risk_states[(position.symbol, position.side)]
            milestone = _advance_milestone(state, roi, self._pnl_milestones)
            if milestone is not None:
                label = f"+{milestone:g}" if milestone > 0 else f"{milestone:g}"
                result.append(
                    _condition(
                        account,
                        user,
                        position.symbol,
                        "PNL_MILESTONE",
                        f"pnl:{account.id}:{position.symbol}:{position.side}:"
                        f"{'profit' if milestone > 0 else 'loss'}:{abs(milestone):g}",
                        f"💹 <b>PnL {label}%</b> — {html.escape(position.symbol)}\n"
                        f"Текущая оценка ROI: {roi:+.2f}% · PnL {pnl:+.2f} USDT.",
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

    def _current_mark(self, position: PositionRecord) -> Decimal | None:
        if self._live_marks is None:
            return position.mark
        mark = self._live_marks.get(
            position.symbol,
            max_age_seconds=self._live_mark_stale_seconds,
        )
        return mark.price if mark is not None else None


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
            # The monitor may resolve an alert after it was claimed but before delivery.
            # Монитор мог снять алерт после claim, но до фактической отправки.
            if alert is None or not alert.active or alert.status != "PROCESSING":
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
            if alert is not None and alert.active and alert.status == "PROCESSING":
                alert.status = status
                alert.error = error
                if status == "SENT":
                    alert.last_sent_at = datetime.now(UTC)

    async def _retry(self, alert_id: uuid.UUID, error: str) -> None:
        async with self._database.session() as session, session.begin():
            alert = await session.get(RiskAlertRecord, alert_id)
            if alert is not None and alert.active and alert.status == "PROCESSING":
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


def _liquidation_distance_pct(
    position: PositionRecord,
    mark: Decimal | None = None,
) -> float | None:
    current_mark = position.mark if mark is None else mark
    if position.liquidation is None or current_mark <= 0:
        return None
    if position.side == "LONG":
        return float((current_mark - position.liquidation) / current_mark * Decimal(100))
    return float((position.liquidation - current_mark) / current_mark * Decimal(100))


def _position_roi_pct(position: PositionRecord, mark: Decimal | None = None) -> float:
    notional = abs(position.entry * position.amount)
    if notional <= 0 or position.leverage <= 0:
        return 0.0
    margin = notional / Decimal(position.leverage)
    pnl = position.pnl if mark is None else _unrealized_pnl(position, mark)
    return float(pnl / margin * Decimal(100))


def _unrealized_pnl(position: PositionRecord, mark: Decimal) -> Decimal:
    basis = position.break_even if position.break_even is not None else position.entry
    return (mark - basis) * position.amount


def _milestone(value: float, levels: tuple[float, ...]) -> float | None:
    sign = 1.0 if value >= 0 else -1.0
    reached = [level for level in levels if abs(value) >= level]
    return sign * max(reached) if reached else None


def _advance_milestone(
    state: PositionRiskStateRecord,
    roi: float,
    levels: tuple[float, ...],
) -> float | None:
    """Return only a new high-watermark crossing. / Возвращает только новый пересечённый порог."""

    state.last_roi_pct = roi
    reached = max((level for level in levels if abs(roi) >= level), default=None)
    if reached is None:
        return None
    if roi >= 0 and reached > float(state.max_profit_milestone):
        state.max_profit_milestone = reached
        return reached
    if roi < 0 and reached > float(state.max_loss_milestone):
        state.max_loss_milestone = reached
        return -reached
    return None


def _base_asset(symbol: str) -> str:
    for quote in ("USDT", "USDC", "FDUSD", "BUSD"):
        if symbol.endswith(quote):
            return symbol[: -len(quote)]
    return symbol
