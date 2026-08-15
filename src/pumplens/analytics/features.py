"""Feature formulas from the technical specification. / Формулы признаков из ТЗ."""

from __future__ import annotations

import math
import statistics
import time
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime

from pumplens.analytics.buffers import MarketState, SymbolBuffer
from pumplens.domain.enums import DataQuality, Direction
from pumplens.domain.models import FeatureSnapshot, Kline

EPSILON = 1e-12


def pct_return(now: float, before: float) -> float:
    """Return in percentage points. / Доходность в процентных пунктах."""

    return (now / before - 1.0) * 100.0 if before > 0 else 0.0


def median_absolute_deviation(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    center = statistics.median(values)
    return statistics.median(abs(value - center) for value in values)


def robust_z_score(value: float, history: Sequence[float]) -> float:
    if not history:
        return 0.0
    center = statistics.median(history)
    mad = median_absolute_deviation(history)
    if mad <= EPSILON:
        return 0.0 if math.isclose(value, center) else (value - center) / EPSILON
    return 0.6745 * (value - center) / mad


def price_at_or_before(points: Sequence[tuple[int, float]], target_ms: int) -> float | None:
    for timestamp_ms, price in reversed(points):
        if timestamp_ms <= target_ms:
            return price
    return None


class FeatureEngine:
    def __init__(self, state: MarketState, stale_after_seconds: float) -> None:
        self._state = state
        self._stale_after_seconds = stale_after_seconds

    def snapshot_all(self) -> list[FeatureSnapshot]:
        raw = [self._snapshot_symbol(symbol) for symbol in self._state.symbols]
        fresh_returns = [
            item.return_1m for item in raw if item.data_quality is DataQuality.FRESH
        ]
        market_median = statistics.median(fresh_returns) if fresh_returns else 0.0
        return [
            replace(item, relative_strength_1m=item.return_1m - market_median)
            for item in raw
        ]

    def _snapshot_symbol(self, symbol: str) -> FeatureSnapshot:
        buffer = self._state.get(symbol)
        if buffer is None or len(buffer.closed_klines) < 30:
            return FeatureSnapshot.empty(symbol)

        closed = list(buffer.closed_klines)
        current = buffer.current_kline
        last_price = self._last_price(buffer, closed)
        if last_price <= 0:
            return FeatureSnapshot.empty(symbol)

        return_1m = self._window_return(buffer, closed, last_price, 60)
        return_3m = self._window_return(buffer, closed, last_price, 180)
        return_5m = self._window_return(buffer, closed, last_price, 300)
        return_15m = self._window_return(buffer, closed, last_price, 900)
        acceleration = self._acceleration_1m(buffer, last_price)
        volume_ratio, volume_z, trade_ratio, buy_pressure = self._flow_features(current, closed)

        direction = Direction.LONG if return_1m >= 0 else Direction.SHORT
        book = buffer.book
        spread = book.spread_pct if book is not None else float("inf")
        range_pct = current.range_pct if current is not None else closed[-1].range_pct
        structure = candle_structure(current or closed[-1], direction)
        quote_volume = buffer.ticker.quote_volume if buffer.ticker is not None else 0.0
        (
            agg_pressure,
            agg_trade_ratio,
            depth_imbalance,
            bid_depth,
            ask_depth,
            oi_delta,
            deep_ready,
        ) = self._deep_features(buffer)
        age = time.monotonic() - buffer.last_received_monotonic
        quality = (
            DataQuality.FRESH
            if buffer.last_received_monotonic > 0 and age <= self._stale_after_seconds
            else DataQuality.STALE
        )

        return FeatureSnapshot(
            symbol=symbol,
            timestamp=datetime.now(UTC),
            direction=direction,
            last_price=last_price,
            return_1m=return_1m,
            return_3m=return_3m,
            return_5m=return_5m,
            return_15m=return_15m,
            acceleration_1m=acceleration,
            volume_ratio_1m=volume_ratio,
            volume_robust_z=volume_z,
            buy_pressure=buy_pressure,
            trade_rate_ratio=trade_ratio,
            spread_pct=spread,
            relative_strength_1m=0.0,
            range_pct_1m=range_pct,
            candle_structure=structure,
            quote_volume_24h=quote_volume,
            agg_buy_pressure=agg_pressure,
            agg_trade_rate_ratio=agg_trade_ratio,
            depth_imbalance=depth_imbalance,
            bid_depth_usdt=bid_depth,
            ask_depth_usdt=ask_depth,
            oi_delta_pct=oi_delta,
            deep_data_ready=deep_ready,
            data_quality=quality,
        )

    @staticmethod
    def _last_price(buffer: SymbolBuffer, closed: Sequence[Kline]) -> float:
        if buffer.current_kline is not None:
            return buffer.current_kline.close
        if buffer.ticker is not None:
            return buffer.ticker.last_price
        return closed[-1].close

    @staticmethod
    def _window_return(
        buffer: SymbolBuffer,
        closed: Sequence[Kline],
        last_price: float,
        seconds: int,
    ) -> float:
        if buffer.price_points:
            target = buffer.price_points[-1][0] - seconds * 1_000
            before = price_at_or_before(buffer.price_points, target)
            if before is not None:
                return pct_return(last_price, before)
        bars_back = max(1, seconds // 60)
        index = max(0, len(closed) - bars_back)
        return pct_return(last_price, closed[index].open)

    @staticmethod
    def _acceleration_1m(buffer: SymbolBuffer, last_price: float) -> float:
        if len(buffer.price_points) < 2:
            return 0.0
        now_ms = buffer.price_points[-1][0]
        p_15 = price_at_or_before(buffer.price_points, now_ms - 15_000)
        p_30 = price_at_or_before(buffer.price_points, now_ms - 30_000)
        if p_15 is None or p_30 is None:
            return 0.0
        return pct_return(last_price, p_15) - pct_return(p_15, p_30)

    @staticmethod
    def _flow_features(
        current: Kline | None,
        closed: Sequence[Kline],
    ) -> tuple[float, float, float, float]:
        baseline = closed[-31:-1] if len(closed) >= 31 else closed[-30:]
        volumes = [bar.quote_volume for bar in baseline]
        trades = [float(bar.trade_count) for bar in baseline]
        if not volumes:
            return 0.0, 0.0, 0.0, 0.5

        if current is None:
            observed = closed[-1]
            elapsed_seconds = 60.0
        else:
            observed = current
            elapsed_seconds = max(
                1.0,
                min(60.0, (time.time() * 1_000 - current.open_time_ms) / 1_000),
            )

        fraction = elapsed_seconds / 60.0
        expected_volume = statistics.median(volumes) * fraction
        volume_ratio = observed.quote_volume / max(expected_volume, EPSILON)
        projected_volume = observed.quote_volume / max(fraction, EPSILON)
        volume_z = robust_z_score(projected_volume, volumes)

        baseline_trade_rate = statistics.median(trades) / 60.0
        current_trade_rate = observed.trade_count / elapsed_seconds
        trade_ratio = current_trade_rate / max(baseline_trade_rate, EPSILON)
        pressure = observed.taker_buy_quote_volume / max(observed.quote_volume, EPSILON)
        return volume_ratio, volume_z, trade_ratio, min(max(pressure, 0.0), 1.0)

    @staticmethod
    def _deep_features(
        buffer: SymbolBuffer,
    ) -> tuple[float, float, float, float, float, float, bool]:
        buckets = list(buffer.trade_buckets)
        recent = buckets[-60:]
        total_buy = sum(bucket.buy_quote for bucket in recent)
        total_sell = sum(bucket.sell_quote for bucket in recent)
        total_quote = total_buy + total_sell
        agg_pressure = total_buy / total_quote if total_quote > 0 else 0.5

        # Compare the latest 15 seconds with older collected seconds.
        # Сравниваем последние 15 секунд с ранее накопленными секундами.
        active = recent[-15:]
        baseline = recent[:-15]
        active_rate = statistics.mean(bucket.trade_count for bucket in active) if active else 0.0
        baseline_counts = [float(bucket.trade_count) for bucket in baseline]
        baseline_rate = statistics.median(baseline_counts) if baseline_counts else 0.0
        agg_trade_ratio = active_rate / max(baseline_rate, EPSILON) if baseline else 0.0

        bid_depth = ask_depth = depth_imbalance = 0.0
        if buffer.depth is not None:
            bid_depth, ask_depth = buffer.depth.depth_within_pct(0.5)
            total_depth = bid_depth + ask_depth
            if total_depth > 0:
                depth_imbalance = (bid_depth - ask_depth) / total_depth

        oi_delta = 0.0
        oi_points = list(buffer.open_interest)
        if len(oi_points) >= 2:
            latest = oi_points[-1]
            old = next(
                (
                    point
                    for point in oi_points
                    if latest.timestamp_ms - point.timestamp_ms >= 45_000
                ),
                None,
            )
            if old is not None:
                oi_delta = pct_return(latest.open_interest, old.open_interest)

        deep_ready = total_quote > 0 and buffer.depth is not None
        return (
            agg_pressure,
            agg_trade_ratio,
            depth_imbalance,
            bid_depth,
            ask_depth,
            oi_delta,
            deep_ready,
        )


def candle_structure(kline: Kline, direction: Direction) -> float:
    """1.0 means close is near the impulse extreme. / 1.0 — закрытие у экстремума."""

    full_range = max(kline.high - kline.low, EPSILON)
    if direction is Direction.LONG:
        opposing_wick = kline.high - max(kline.open, kline.close)
    else:
        opposing_wick = min(kline.open, kline.close) - kline.low
    return min(max(1.0 - opposing_wick / full_range, 0.0), 1.0)
