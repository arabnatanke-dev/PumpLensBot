"""Deterministic cached-candle structure primitives. / Детерминированная структура."""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass

from pumplens.domain.enums import Direction, MarketStructure
from pumplens.domain.models import Kline, PriceZone, TimeframeStructure

EPSILON = 1e-12


@dataclass(frozen=True, slots=True)
class PivotPoint:
    index: int
    price: float
    kind: str


@dataclass(frozen=True, slots=True)
class LevelMap:
    support_zones: tuple[PriceZone, ...]
    resistance_zones: tuple[PriceZone, ...]


def aggregate_klines(bars: Sequence[Kline], minutes: int) -> list[Kline]:
    """Aggregate 1m bars without external data. / Собирает старший TF из 1m-кэша."""

    if minutes <= 1:
        return list(bars)
    interval_ms = minutes * 60_000
    groups: list[list[Kline]] = []
    group_key: int | None = None
    for bar in bars:
        key = bar.open_time_ms // interval_ms
        if key != group_key:
            groups.append([])
            group_key = key
        groups[-1].append(bar)
    result: list[Kline] = []
    for group in groups:
        first = group[0]
        last = group[-1]
        result.append(
            Kline(
                symbol=first.symbol,
                open_time_ms=first.open_time_ms,
                close_time_ms=last.close_time_ms,
                open=first.open,
                high=max(item.high for item in group),
                low=min(item.low for item in group),
                close=last.close,
                base_volume=sum(item.base_volume for item in group),
                quote_volume=sum(item.quote_volume for item in group),
                trade_count=sum(item.trade_count for item in group),
                taker_buy_quote_volume=sum(
                    item.taker_buy_quote_volume for item in group
                ),
                closed=all(item.closed for item in group),
            )
        )
    return result


def detect_pivots(bars: Sequence[Kline], window: int) -> tuple[PivotPoint, ...]:
    if window < 1 or len(bars) < window * 2 + 1:
        return ()
    pivots: list[PivotPoint] = []
    for index in range(window, len(bars) - window):
        bar = bars[index]
        neighbours = list(bars[index - window : index]) + list(
            bars[index + 1 : index + window + 1]
        )
        if bar.high >= max(item.high for item in neighbours) and any(
            bar.high > item.high for item in neighbours
        ):
            pivots.append(PivotPoint(index=index, price=bar.high, kind="HIGH"))
        if bar.low <= min(item.low for item in neighbours) and any(
            bar.low < item.low for item in neighbours
        ):
            pivots.append(PivotPoint(index=index, price=bar.low, kind="LOW"))
    return tuple(pivots)


def analyze_timeframe(
    bars: Sequence[Kline],
    *,
    timeframe: str,
    pivot_window: int,
    compression_ratio: float,
    expansion_ratio: float,
) -> TimeframeStructure:
    if len(bars) < pivot_window * 2 + 3:
        return TimeframeStructure(
            timeframe=timeframe,
            structure=MarketStructure.NEUTRAL,
            pattern="INSUFFICIENT_DATA",
            last_swing_high=None,
            last_swing_low=None,
            compression=False,
            expansion=False,
            bos_direction=None,
        )
    history = bars[:-1]
    pivots = detect_pivots(history, pivot_window)
    highs = [pivot.price for pivot in pivots if pivot.kind == "HIGH"]
    lows = [pivot.price for pivot in pivots if pivot.kind == "LOW"]
    pattern = "MIXED"
    structure = MarketStructure.RANGE
    if len(highs) >= 2 and len(lows) >= 2:
        higher_high = highs[-1] > highs[-2]
        higher_low = lows[-1] > lows[-2]
        lower_high = highs[-1] < highs[-2]
        lower_low = lows[-1] < lows[-2]
        if higher_high and higher_low:
            structure = MarketStructure.BULLISH
            pattern = "HH_HL"
        elif lower_high and lower_low:
            structure = MarketStructure.BEARISH
            pattern = "LH_LL"
        elif not higher_high and not lower_high and not higher_low and not lower_low:
            pattern = "FLAT_RANGE"
    elif not highs or not lows:
        structure = MarketStructure.NEUTRAL
        pattern = "INSUFFICIENT_SWINGS"

    last_high = highs[-1] if highs else None
    last_low = lows[-1] if lows else None
    close = bars[-1].close
    bos_direction: Direction | None = None
    if last_high is not None and close > last_high:
        bos_direction = Direction.LONG
    elif last_low is not None and close < last_low:
        bos_direction = Direction.SHORT

    recent_ranges = [_range_pct(item) for item in bars[-5:]]
    baseline_ranges = [_range_pct(item) for item in bars[-15:-5]]
    recent_range = statistics.median(recent_ranges) if recent_ranges else 0.0
    baseline_range = statistics.median(baseline_ranges) if baseline_ranges else 0.0
    compression = baseline_range > 0 and recent_range <= baseline_range * compression_ratio
    expansion = baseline_range > 0 and recent_range >= baseline_range * expansion_ratio
    return TimeframeStructure(
        timeframe=timeframe,
        structure=structure,
        pattern=pattern,
        last_swing_high=last_high,
        last_swing_low=last_low,
        compression=compression,
        expansion=expansion,
        bos_direction=bos_direction,
    )


def build_level_map(
    bars: Sequence[Kline],
    *,
    pivot_window: int,
    tolerance_pct: float,
    min_touches: int = 1,
) -> LevelMap:
    pivots = detect_pivots(bars, pivot_window)
    supports = _cluster_prices(
        [pivot.price for pivot in pivots if pivot.kind == "LOW"],
        tolerance_pct,
    )
    resistances = _cluster_prices(
        [pivot.price for pivot in pivots if pivot.kind == "HIGH"],
        tolerance_pct,
    )
    return LevelMap(
        tuple(zone for zone in supports if zone.touches >= min_touches),
        tuple(zone for zone in resistances if zone.touches >= min_touches),
    )


def nearest_zones(
    levels: LevelMap,
    price: float,
) -> tuple[PriceZone | None, PriceZone | None]:
    support_candidates = [zone for zone in levels.support_zones if zone.midpoint < price]
    resistance_candidates = [
        zone for zone in levels.resistance_zones if zone.midpoint > price
    ]
    support = max(support_candidates, key=lambda zone: zone.midpoint, default=None)
    resistance = min(resistance_candidates, key=lambda zone: zone.midpoint, default=None)
    return support, resistance


def average_true_range(bars: Sequence[Kline], period: int) -> float:
    if len(bars) < 2:
        return 0.0
    values: list[float] = []
    start = max(1, len(bars) - period)
    for index in range(start, len(bars)):
        bar = bars[index]
        previous_close = bars[index - 1].close
        values.append(
            max(
                bar.high - bar.low,
                abs(bar.high - previous_close),
                abs(bar.low - previous_close),
            )
        )
    return statistics.mean(values) if values else 0.0


def rolling_vwap(bars: Sequence[Kline], lookback: int = 30) -> float:
    selected = bars[-lookback:]
    total_volume = sum(item.quote_volume for item in selected)
    if total_volume <= EPSILON:
        return selected[-1].close if selected else 0.0
    return sum(
        ((item.high + item.low + item.close) / 3.0) * item.quote_volume
        for item in selected
    ) / total_volume


def opposing_wick_ratio(bar: Kline, direction: Direction) -> float:
    full_range = max(bar.high - bar.low, EPSILON)
    if direction is Direction.LONG:
        wick = bar.high - max(bar.open, bar.close)
    else:
        wick = min(bar.open, bar.close) - bar.low
    return min(max(wick / full_range, 0.0), 1.0)


def _cluster_prices(prices: Sequence[float], tolerance_pct: float) -> list[PriceZone]:
    clusters: list[list[float]] = []
    for price in sorted(value for value in prices if value > 0):
        if not clusters:
            clusters.append([price])
            continue
        center = statistics.mean(clusters[-1])
        distance_pct = abs(price / center - 1.0) * 100.0
        if distance_pct <= tolerance_pct:
            clusters[-1].append(price)
        else:
            clusters.append([price])
    zones: list[PriceZone] = []
    for cluster in clusters:
        center = statistics.mean(cluster)
        buffer = center * tolerance_pct / 200.0
        touches = len(cluster)
        zones.append(
            PriceZone(
                low=min(cluster) - buffer,
                high=max(cluster) + buffer,
                touches=touches,
                strength=min(touches / 3.0, 1.0),
            )
        )
    return zones


def _range_pct(bar: Kline) -> float:
    return (bar.high - bar.low) / max(bar.open, EPSILON) * 100.0
