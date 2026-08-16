"""Production signal outcome evaluator. / Production-оценщик результатов сигналов."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import structlog
from sqlalchemy import and_, func, or_, select

from pumplens.binance.public_rest import BinancePublicClient
from pumplens.domain.enums import Direction
from pumplens.domain.models import Kline
from pumplens.replay.early_outcomes import directional_move_pct
from pumplens.replay.outcomes import evaluate_kline_outcome
from pumplens.storage.db import Database
from pumplens.storage.models import EarlyOutcomeRecord, SignalOutcomeRecord, SignalRecord

log = structlog.get_logger(__name__)


class SignalOutcomeEvaluator:
    def __init__(
        self,
        database: Database,
        rest_base_url: str,
        *,
        interval_seconds: float = 30.0,
        batch_size: int = 5,
        request_spacing_seconds: float = 0.2,
    ) -> None:
        self._database = database
        self._rest_base_url = rest_base_url
        self._interval_seconds = interval_seconds
        self._batch_size = batch_size
        self._request_spacing_seconds = request_spacing_seconds

    async def run(self) -> None:
        async with BinancePublicClient(self._rest_base_url) as client:
            while True:
                try:
                    await self.evaluate_once(client)
                except Exception as exc:
                    log.warning("signal_outcome_evaluation_failed", error=type(exc).__name__)
                await asyncio.sleep(self._interval_seconds)

    async def evaluate_once(self, client: BinancePublicClient) -> None:
        now = datetime.now(UTC)
        observed_at = func.coalesce(SignalRecord.watch_at, SignalRecord.candidate_at)
        async with self._database.session() as session:
            rows = (
                await session.execute(
                    select(SignalRecord, SignalOutcomeRecord)
                    .outerjoin(
                        SignalOutcomeRecord,
                        SignalOutcomeRecord.signal_id == SignalRecord.id,
                    )
                    .where(
                        SignalRecord.start_price.is_not(None),
                        or_(
                            SignalRecord.watch_at.is_not(None),
                            SignalRecord.final_decision.is_not(None),
                        ),
                        observed_at <= now - timedelta(minutes=5),
                        or_(
                            SignalOutcomeRecord.id.is_(None),
                            and_(
                                SignalOutcomeRecord.evaluated_at.is_(None),
                                or_(
                                    SignalOutcomeRecord.last_sampled_at.is_(None),
                                    SignalOutcomeRecord.last_sampled_at
                                    <= now - timedelta(minutes=5),
                                ),
                            ),
                        ),
                    )
                    .order_by(observed_at)
                    .limit(self._batch_size)
                )
            ).all()
        for index, (signal, outcome) in enumerate(rows):
            await self._evaluate_signal(client, signal, outcome, now)
            if index + 1 < len(rows) and self._request_spacing_seconds > 0:
                await asyncio.sleep(self._request_spacing_seconds)

    async def _evaluate_signal(
        self,
        client: BinancePublicClient,
        signal: SignalRecord,
        existing: SignalOutcomeRecord | None,
        now: datetime,
    ) -> None:
        started = _aware(signal.watch_at or signal.candidate_at)
        entry_price = signal.trigger_price or signal.start_price
        if started is None or entry_price is None:
            return
        elapsed_minutes = (now - started).total_seconds() / 60.0
        end = min(now, started + timedelta(minutes=61))
        klines = await client.klines(
            signal.symbol,
            limit=70,
            start_time_ms=int(started.timestamp() * 1_000),
            end_time_ms=int(end.timestamp() * 1_000),
        )
        closed = [item for item in klines if item.closed]
        if not closed:
            return
        direction = Direction(signal.direction)
        result = evaluate_kline_outcome(
            direction,
            float(entry_price),
            closed,
        )
        prices = {
            minutes: _price_at(closed, started + timedelta(minutes=minutes))
            for minutes in (5, 15, 60)
            if elapsed_minutes >= minutes
        }
        async with self._database.session() as session, session.begin():
            row = await session.scalar(
                select(SignalOutcomeRecord).where(SignalOutcomeRecord.signal_id == signal.id)
            )
            if row is None:
                row = SignalOutcomeRecord(signal_id=signal.id)
                session.add(row)
            if 5 in prices:
                row.price_5m = Decimal(str(prices[5]))
            if 15 in prices:
                row.price_15m = Decimal(str(prices[15]))
            if 60 in prices:
                row.price_60m = Decimal(str(prices[60]))
            row.mfe = result.mfe_pct
            row.mae = result.mae_pct
            row.hit_rule = result.hit_rule
            row.last_sampled_at = now
            if elapsed_minutes >= 60:
                row.evaluated_at = now


class EarlyOutcomeEvaluator:
    """Sample short EARLY horizons while replay stores the raw feed. / Снимает короткие точки."""

    def __init__(
        self,
        database: Database,
        rest_base_url: str,
        *,
        interval_seconds: float = 5.0,
    ) -> None:
        self._database = database
        self._rest_base_url = rest_base_url
        self._interval_seconds = interval_seconds

    async def run(self) -> None:
        async with BinancePublicClient(self._rest_base_url) as client:
            while True:
                try:
                    await self.evaluate_once(client)
                except Exception as exc:
                    log.warning("early_outcome_evaluation_failed", error=type(exc).__name__)
                await asyncio.sleep(self._interval_seconds)

    async def evaluate_once(self, client: BinancePublicClient) -> None:
        now = datetime.now(UTC)
        async with self._database.session() as session:
            signals = list(
                await session.scalars(
                    select(SignalRecord)
                    .outerjoin(
                        EarlyOutcomeRecord,
                        EarlyOutcomeRecord.signal_id == SignalRecord.id,
                    )
                    .where(
                        SignalRecord.early_at.is_not(None),
                        SignalRecord.early_price.is_not(None),
                        SignalRecord.early_at >= now - timedelta(seconds=330),
                        or_(
                            EarlyOutcomeRecord.id.is_(None),
                            EarlyOutcomeRecord.evaluated_at.is_(None),
                        ),
                    )
                    .order_by(SignalRecord.early_at)
                    .limit(50)
                )
            )
        if not signals:
            return
        prices = await client.ticker_prices()
        for signal in signals:
            started = _aware(signal.early_at)
            if started is None or signal.early_price is None:
                continue
            elapsed = (now - started).total_seconds()
            if elapsed < 0:
                continue
            price = prices.get(signal.symbol)
            if price is None:
                continue
            await self._store_sample(signal, elapsed, price, now)

    async def _store_sample(
        self,
        signal: SignalRecord,
        elapsed_seconds: float,
        price: float,
        now: datetime,
    ) -> None:
        start_price = float(signal.early_price or 0)
        move = directional_move_pct(Direction(signal.direction), start_price, price)
        async with self._database.session() as session, session.begin():
            row = await session.scalar(
                select(EarlyOutcomeRecord).where(EarlyOutcomeRecord.signal_id == signal.id)
            )
            if row is None:
                row = EarlyOutcomeRecord(signal_id=signal.id)
                session.add(row)
            previous_mfe = float(row.mfe) if row.mfe is not None else 0.0
            previous_mae = float(row.mae) if row.mae is not None else 0.0
            row.mfe = max(previous_mfe, move)
            row.mae = min(previous_mae, move)
            _set_checkpoint(
                row,
                elapsed_seconds,
                price,
                tolerance_seconds=max(self._interval_seconds * 2.0, 10.0),
            )
            if elapsed_seconds >= 300:
                row.evaluated_at = now


def _price_at(klines: list[Kline], target: datetime) -> float:
    target_ms = int(target.timestamp() * 1_000)
    eligible = [item for item in klines if item.close_time_ms <= target_ms]
    selected = eligible[-1] if eligible else klines[0]
    return float(selected.close)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _set_checkpoint(
    row: EarlyOutcomeRecord,
    elapsed_seconds: float,
    price: float,
    *,
    tolerance_seconds: float,
) -> None:
    value = Decimal(str(price))
    if 15 <= elapsed_seconds <= 15 + tolerance_seconds and row.price_15s is None:
        row.price_15s = value
    if 30 <= elapsed_seconds <= 30 + tolerance_seconds and row.price_30s is None:
        row.price_30s = value
    if 60 <= elapsed_seconds <= 60 + tolerance_seconds and row.price_1m is None:
        row.price_1m = value
    if 180 <= elapsed_seconds <= 180 + tolerance_seconds and row.price_3m is None:
        row.price_3m = value
    if 300 <= elapsed_seconds <= 300 + tolerance_seconds and row.price_5m is None:
        row.price_5m = value
