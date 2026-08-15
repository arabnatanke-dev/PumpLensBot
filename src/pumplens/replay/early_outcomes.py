"""EARLY checkpoint and replay calculations. / Расчёты EARLY по точкам и replay."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from pumplens.domain.enums import Direction
from pumplens.domain.events import MarketEvent
from pumplens.domain.models import AggTrade, Kline, MarkPrice, Ticker24h
from pumplens.replay.outcomes import evaluate_outcome

CHECKPOINT_SECONDS = (15, 30, 60, 180, 300)


@dataclass(frozen=True, slots=True)
class EarlySample:
    elapsed_seconds: float
    price: float


@dataclass(frozen=True, slots=True)
class EarlyReplayOutcome:
    prices: dict[int, float]
    mfe_pct: float
    mae_pct: float


def evaluate_early_samples(
    direction: Direction,
    early_price: float,
    samples: Iterable[EarlySample],
) -> EarlyReplayOutcome:
    ordered = sorted(
        (sample for sample in samples if 0 <= sample.elapsed_seconds <= 300),
        key=lambda sample: sample.elapsed_seconds,
    )
    checkpoints: dict[int, float] = {}
    for seconds in CHECKPOINT_SECONDS:
        sample = next((item for item in ordered if item.elapsed_seconds >= seconds), None)
        if sample is not None:
            checkpoints[seconds] = sample.price
    outcome = evaluate_outcome(direction, early_price, [sample.price for sample in ordered])
    return EarlyReplayOutcome(
        checkpoints,
        max(0.0, outcome.mfe_pct),
        min(0.0, outcome.mae_pct),
    )


def evaluate_early_replay(
    direction: Direction,
    symbol: str,
    early_price: float,
    early_time_ms: int,
    events: Iterable[tuple[int, MarketEvent]],
) -> EarlyReplayOutcome:
    samples: list[EarlySample] = []
    for timestamp_ms, event in events:
        elapsed = (timestamp_ms - early_time_ms) / 1_000.0
        if elapsed < 0 or elapsed > 300:
            continue
        value = event.value
        if getattr(value, "symbol", None) != symbol:
            continue
        price = _event_price(value)
        if price is not None:
            samples.append(EarlySample(elapsed, price))
    return evaluate_early_samples(direction, early_price, samples)


def directional_move_pct(direction: Direction, start: float, price: float) -> float:
    if start <= 0 or price <= 0:
        return 0.0
    sign = 1.0 if direction is Direction.LONG else -1.0
    return (price / start - 1.0) * 100.0 * sign


def _event_price(value: object) -> float | None:
    if isinstance(value, AggTrade):
        return value.price
    if isinstance(value, MarkPrice):
        return value.mark_price
    if isinstance(value, Ticker24h):
        return value.last_price
    if isinstance(value, Kline):
        return value.close
    return None
