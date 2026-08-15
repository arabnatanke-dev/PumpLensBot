"""Production signal outcome evaluator. / Production-оценщик результатов сигналов."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import structlog
from sqlalchemy import or_, select

from pumplens.binance.public_rest import BinancePublicClient
from pumplens.domain.enums import Direction
from pumplens.domain.models import Kline
from pumplens.replay.outcomes import evaluate_outcome
from pumplens.storage.db import Database
from pumplens.storage.models import SignalOutcomeRecord, SignalRecord

log = structlog.get_logger(__name__)


class SignalOutcomeEvaluator:
    def __init__(
        self,
        database: Database,
        rest_base_url: str,
        *,
        interval_seconds: float = 30.0,
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
                    log.warning("signal_outcome_evaluation_failed", error=type(exc).__name__)
                await asyncio.sleep(self._interval_seconds)

    async def evaluate_once(self, client: BinancePublicClient) -> None:
        now = datetime.now(UTC)
        async with self._database.session() as session:
            rows = (
                await session.execute(
                    select(SignalRecord, SignalOutcomeRecord)
                    .outerjoin(
                        SignalOutcomeRecord,
                        SignalOutcomeRecord.signal_id == SignalRecord.id,
                    )
                    .where(
                        SignalRecord.trigger_price.is_not(None),
                        SignalRecord.watch_at.is_not(None),
                        SignalRecord.watch_at <= now - timedelta(minutes=5),
                        or_(
                            SignalOutcomeRecord.id.is_(None),
                            SignalOutcomeRecord.evaluated_at.is_(None),
                        ),
                    )
                    .order_by(SignalRecord.watch_at)
                    .limit(25)
                )
            ).all()
        for signal, outcome in rows:
            await self._evaluate_signal(client, signal, outcome, now)

    async def _evaluate_signal(
        self,
        client: BinancePublicClient,
        signal: SignalRecord,
        existing: SignalOutcomeRecord | None,
        now: datetime,
    ) -> None:
        started = _aware(signal.watch_at)
        if started is None or signal.trigger_price is None:
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
        result = evaluate_outcome(
            direction,
            float(signal.trigger_price),
            [item.close for item in closed],
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
            if elapsed_minutes >= 60:
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
