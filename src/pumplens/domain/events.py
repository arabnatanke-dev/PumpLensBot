"""Typed normalized market events. / Типизированные нормализованные события рынка."""

from __future__ import annotations

from dataclasses import dataclass

from pumplens.domain.models import AggTrade, BookTicker, DepthSnapshot, Kline, MarkPrice, Ticker24h


@dataclass(frozen=True, slots=True)
class KlineEvent:
    value: Kline


@dataclass(frozen=True, slots=True)
class BookTickerEvent:
    value: BookTicker


@dataclass(frozen=True, slots=True)
class TickerEvent:
    value: Ticker24h


@dataclass(frozen=True, slots=True)
class MarkPriceEvent:
    value: MarkPrice


@dataclass(frozen=True, slots=True)
class AggTradeEvent:
    value: AggTrade


@dataclass(frozen=True, slots=True)
class DepthEvent:
    value: DepthSnapshot


MarketEvent = (
    KlineEvent
    | BookTickerEvent
    | TickerEvent
    | MarkPriceEvent
    | AggTradeEvent
    | DepthEvent
)
