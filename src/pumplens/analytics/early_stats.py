"""Aggregated EARLY quality statistics. / Агрегированная статистика качества EARLY."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from statistics import mean

from sqlalchemy import select

from pumplens.storage.db import Database
from pumplens.storage.models import EarlyOutcomeRecord, SignalEventRecord, SignalRecord


@dataclass(frozen=True, slots=True)
class EarlyStatRow:
    signal_id: uuid.UUID
    early_at: datetime
    watch_at: datetime | None
    confirmed_at: datetime | None
    liquidity_tier: str
    direction: str
    early_price: float
    invalidated: bool
    price_15s: float | None = None
    price_30s: float | None = None
    price_1m: float | None = None
    price_3m: float | None = None
    price_5m: float | None = None
    mfe: float | None = None
    mae: float | None = None


@dataclass(frozen=True, slots=True)
class EarlyStatistics:
    total: int
    watch_conversion_pct: float
    confirmed_conversion_pct: float
    invalidated_pct: float
    average_watch_lead_seconds: float | None
    average_confirmed_lead_seconds: float | None
    early_per_hour: float
    average_returns_pct: dict[str, float]
    average_mfe_pct: float | None
    average_mae_pct: float | None
    liquidity_groups: dict[str, EarlyLiquidityStatistics]


@dataclass(frozen=True, slots=True)
class EarlyLiquidityStatistics:
    total: int
    watch_conversion_pct: float
    confirmed_conversion_pct: float
    invalidated_pct: float


def calculate_early_statistics(rows: list[EarlyStatRow]) -> EarlyStatistics:
    if not rows:
        return EarlyStatistics(0, 0.0, 0.0, 0.0, None, None, 0.0, {}, None, None, {})
    watch_leads = [
        (_aware(row.watch_at) - _aware(row.early_at)).total_seconds()
        for row in rows
        if row.watch_at is not None
    ]
    confirmed_leads = [
        (_aware(row.confirmed_at) - _aware(row.early_at)).total_seconds()
        for row in rows
        if row.confirmed_at is not None
    ]
    starts = [_aware(row.early_at) for row in rows]
    observed_hours = max((max(starts) - min(starts)).total_seconds() / 3_600.0, 1.0)
    returns: dict[str, float] = {}
    for label, field in (
        ("15s", "price_15s"),
        ("30s", "price_30s"),
        ("1m", "price_1m"),
        ("3m", "price_3m"),
        ("5m", "price_5m"),
    ):
        values = [
            (float(getattr(row, field)) / row.early_price - 1.0)
            * 100.0
            * (1.0 if row.direction == "LONG" else -1.0)
            for row in rows
            if getattr(row, field) is not None and row.early_price > 0
        ]
        if values:
            returns[label] = mean(values)
    liquidity: dict[str, EarlyLiquidityStatistics] = {}
    for tier in sorted({row.liquidity_tier for row in rows}):
        tier_rows = [row for row in rows if row.liquidity_tier == tier]
        tier_total = len(tier_rows)
        liquidity[tier] = EarlyLiquidityStatistics(
            total=tier_total,
            watch_conversion_pct=sum(row.watch_at is not None for row in tier_rows)
            / tier_total
            * 100.0,
            confirmed_conversion_pct=sum(row.confirmed_at is not None for row in tier_rows)
            / tier_total
            * 100.0,
            invalidated_pct=sum(row.invalidated for row in tier_rows) / tier_total * 100.0,
        )
    total = len(rows)
    mfe_values = [row.mfe for row in rows if row.mfe is not None]
    mae_values = [row.mae for row in rows if row.mae is not None]
    return EarlyStatistics(
        total=total,
        watch_conversion_pct=sum(row.watch_at is not None for row in rows) / total * 100.0,
        confirmed_conversion_pct=sum(row.confirmed_at is not None for row in rows)
        / total
        * 100.0,
        invalidated_pct=sum(row.invalidated for row in rows) / total * 100.0,
        average_watch_lead_seconds=mean(watch_leads) if watch_leads else None,
        average_confirmed_lead_seconds=mean(confirmed_leads) if confirmed_leads else None,
        early_per_hour=total / observed_hours,
        average_returns_pct=returns,
        average_mfe_pct=mean(mfe_values) if mfe_values else None,
        average_mae_pct=mean(mae_values) if mae_values else None,
        liquidity_groups=liquidity,
    )


async def load_early_statistics(database: Database) -> EarlyStatistics:
    async with database.session() as session:
        result = (
            await session.execute(
                select(SignalRecord, EarlyOutcomeRecord)
                .outerjoin(EarlyOutcomeRecord, EarlyOutcomeRecord.signal_id == SignalRecord.id)
                .where(SignalRecord.early_at.is_not(None))
                .order_by(SignalRecord.early_at)
            )
        ).all()
        invalidated_ids = set(
            await session.scalars(
                select(SignalEventRecord.signal_id).where(
                    SignalEventRecord.from_state == "EARLY",
                    SignalEventRecord.to_state == "INVALIDATED"
                )
            )
        )
    rows = [
        EarlyStatRow(
            signal_id=signal.id,
            early_at=_aware(signal.early_at),
            watch_at=signal.watch_at,
            confirmed_at=signal.confirmed_at,
            liquidity_tier=signal.early_liquidity_tier or "unknown",
            direction=signal.direction,
            early_price=float(signal.early_price or 0),
            invalidated=signal.id in invalidated_ids,
            price_15s=_optional_float(outcome.price_15s if outcome else None),
            price_30s=_optional_float(outcome.price_30s if outcome else None),
            price_1m=_optional_float(outcome.price_1m if outcome else None),
            price_3m=_optional_float(outcome.price_3m if outcome else None),
            price_5m=_optional_float(outcome.price_5m if outcome else None),
            mfe=_optional_float(outcome.mfe if outcome else None),
            mae=_optional_float(outcome.mae if outcome else None),
        )
        for signal, outcome in result
        if signal.early_at is not None
    ]
    return calculate_early_statistics(rows)


def _aware(value: datetime | None) -> datetime:
    if value is None:
        raise ValueError("EARLY timestamp is required")
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _optional_float(value: Decimal | float | int | None) -> float | None:
    return float(value) if value is not None else None
