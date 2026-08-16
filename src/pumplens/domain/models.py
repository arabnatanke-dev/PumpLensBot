"""Immutable market and analytics models. / Неизменяемые модели рынка и аналитики."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pumplens.domain.enums import (
    BreakoutState,
    DataQuality,
    Direction,
    EntryDecision,
    MarketStructure,
    RetestState,
)


@dataclass(frozen=True, slots=True)
class SymbolSpec:
    symbol: str
    status: str
    contract_type: str
    quote_asset: str
    base_asset: str
    price_precision: int
    quantity_precision: int
    tick_size: float | None = None
    step_size: float | None = None
    min_quantity: float | None = None
    raw_filters: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class Kline:
    symbol: str
    open_time_ms: int
    close_time_ms: int
    open: float
    high: float
    low: float
    close: float
    base_volume: float
    quote_volume: float
    trade_count: int
    taker_buy_quote_volume: float
    closed: bool = True

    @property
    def range_pct(self) -> float:
        return ((self.high - self.low) / self.open * 100.0) if self.open else 0.0


@dataclass(frozen=True, slots=True)
class Ticker24h:
    symbol: str
    last_price: float
    quote_volume: float
    price_change_pct: float
    event_time_ms: int = 0


@dataclass(frozen=True, slots=True)
class BookTicker:
    symbol: str
    bid_price: float
    bid_quantity: float
    ask_price: float
    ask_quantity: float
    event_time_ms: int

    @property
    def mid(self) -> float:
        return (self.bid_price + self.ask_price) / 2.0

    @property
    def spread_pct(self) -> float:
        mid = self.mid
        return ((self.ask_price - self.bid_price) / mid * 100.0) if mid else float("inf")


@dataclass(frozen=True, slots=True)
class MarkPrice:
    symbol: str
    mark_price: float
    index_price: float
    funding_rate: float
    event_time_ms: int


@dataclass(frozen=True, slots=True)
class AggTrade:
    symbol: str
    price: float
    quantity: float
    event_time_ms: int
    buyer_is_maker: bool

    @property
    def quote_notional(self) -> float:
        return self.price * self.quantity

    @property
    def aggressive_buy(self) -> bool:
        # m=false means the buyer crossed the spread.
        # m=false означает, что покупатель был агрессором.
        return not self.buyer_is_maker


@dataclass(frozen=True, slots=True)
class DepthSnapshot:
    symbol: str
    bids: tuple[tuple[float, float], ...]
    asks: tuple[tuple[float, float], ...]
    event_time_ms: int

    def depth_within_pct(self, band_pct: float = 0.5) -> tuple[float, float]:
        if not self.bids or not self.asks:
            return 0.0, 0.0
        mid = (self.bids[0][0] + self.asks[0][0]) / 2.0
        lower = mid * (1.0 - band_pct / 100.0)
        upper = mid * (1.0 + band_pct / 100.0)
        bid_depth = sum(price * quantity for price, quantity in self.bids if price >= lower)
        ask_depth = sum(price * quantity for price, quantity in self.asks if price <= upper)
        return bid_depth, ask_depth


@dataclass(frozen=True, slots=True)
class OpenInterestPoint:
    symbol: str
    open_interest: float
    timestamp_ms: int


@dataclass(slots=True)
class TradeBucket:
    second: int
    buy_quote: float = 0.0
    sell_quote: float = 0.0
    trade_count: int = 0


@dataclass(frozen=True, slots=True)
class PriceZone:
    low: float
    high: float
    touches: int
    strength: float

    @property
    def midpoint(self) -> float:
        return (self.low + self.high) / 2.0


@dataclass(frozen=True, slots=True)
class TimeframeStructure:
    timeframe: str
    structure: MarketStructure
    pattern: str
    last_swing_high: float | None
    last_swing_low: float | None
    compression: bool
    expansion: bool
    bos_direction: Direction | None


@dataclass(frozen=True, slots=True)
class EntryAnalysis:
    signal_score: float
    entry_quality: float
    structure_1m: TimeframeStructure
    structure_5m: TimeframeStructure
    structure_15m: TimeframeStructure
    trend_alignment_score: float
    nearest_support: PriceZone | None
    nearest_resistance: PriceZone | None
    distance_to_support_pct: float | None
    distance_to_resistance_pct: float | None
    room_up_pct: float | None
    room_down_pct: float | None
    breakout_state: BreakoutState
    breakout_level: PriceZone | None
    breakout_detected: bool
    retest_state: RetestState
    retest_started: bool
    retest_held: bool
    retest_failed: bool
    atr_pct: float
    distance_from_vwap_pct: float
    late_score: float
    late: bool
    exhaustion_score: float
    entry_reference: float
    invalidation_price: float
    potential_target: float
    risk_pct: float
    reward_pct: float
    rr: float | None
    final_decision: EntryDecision
    reason_codes: tuple[str, ...]
    positive_reasons: tuple[str, ...]
    negative_reasons: tuple[str, ...]
    optional_data_missing: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FeatureSnapshot:
    symbol: str
    timestamp: datetime
    direction: Direction
    last_price: float
    return_1m: float
    return_3m: float
    return_5m: float
    return_15m: float
    acceleration_1m: float
    volume_ratio_1m: float
    volume_robust_z: float
    buy_pressure: float
    trade_rate_ratio: float
    spread_pct: float
    relative_strength_1m: float
    range_pct_1m: float
    candle_structure: float
    quote_volume_24h: float
    quote_volume_1m: float = 0.0
    trade_count_1m: int = 0
    agg_buy_pressure: float = 0.5
    agg_trade_rate_ratio: float = 0.0
    depth_imbalance: float = 0.0
    bid_depth_usdt: float = 0.0
    ask_depth_usdt: float = 0.0
    oi_delta_pct: float = 0.0
    trade_data_ready: bool = False
    depth_data_ready: bool = False
    oi_data_ready: bool = False
    deep_data_ready: bool = False
    score: float = 0.0
    data_quality: DataQuality = DataQuality.WARMING_UP
    reasons: tuple[str, ...] = ()
    penalties: tuple[str, ...] = ()
    too_late: bool = False
    entry_analysis: EntryAnalysis | None = None

    @classmethod
    def empty(cls, symbol: str) -> FeatureSnapshot:
        return cls(
            symbol=symbol,
            timestamp=datetime.now(UTC),
            direction=Direction.LONG,
            last_price=0.0,
            return_1m=0.0,
            return_3m=0.0,
            return_5m=0.0,
            return_15m=0.0,
            acceleration_1m=0.0,
            volume_ratio_1m=0.0,
            volume_robust_z=0.0,
            buy_pressure=0.5,
            trade_rate_ratio=0.0,
            spread_pct=float("inf"),
            relative_strength_1m=0.0,
            range_pct_1m=0.0,
            candle_structure=0.0,
            quote_volume_24h=0.0,
        )


@dataclass(frozen=True, slots=True)
class Candidate:
    snapshot: FeatureSnapshot
    selected: bool
    hard_reject_reason: str | None = None
    confirmations: tuple[str, ...] = field(default_factory=tuple)
    early_selected: bool = False
    early_liquidity_tier: str | None = None
    early_snapshot: FeatureSnapshot | None = None
