"""Bounded in-memory market state. / Ограниченное рыночное состояние в памяти."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field

import structlog

from pumplens.binance.public_rest import BinancePublicClient
from pumplens.domain.events import (
    AggTradeEvent,
    BookTickerEvent,
    DepthEvent,
    KlineEvent,
    MarketEvent,
    MarkPriceEvent,
    TickerEvent,
)
from pumplens.domain.models import (
    BookTicker,
    DepthSnapshot,
    Kline,
    MarkPrice,
    OpenInterestPoint,
    Ticker24h,
    TradeBucket,
)

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class SymbolBuffer:
    """Fixed-size state for one symbol. / Состояние фиксированного размера для символа."""

    closed_klines: deque[Kline] = field(default_factory=lambda: deque(maxlen=180))
    price_points: deque[tuple[int, float]] = field(default_factory=lambda: deque(maxlen=1_800))
    current_kline: Kline | None = None
    ticker: Ticker24h | None = None
    book: BookTicker | None = None
    mark: MarkPrice | None = None
    depth: DepthSnapshot | None = None
    trade_buckets: deque[TradeBucket] = field(default_factory=lambda: deque(maxlen=300))
    open_interest: deque[OpenInterestPoint] = field(default_factory=lambda: deque(maxlen=20))
    last_kline_at: float = 0.0
    last_ticker_at: float = 0.0
    last_book_at: float = 0.0
    last_mark_at: float = 0.0
    last_trade_at: float = 0.0
    last_depth_at: float = 0.0
    last_oi_at: float = 0.0

    def seed(self, klines: Sequence[Kline]) -> None:
        for kline in klines:
            if kline.closed:
                self._append_closed(kline)
            else:
                self.current_kline = kline

    def apply(self, event: MarketEvent) -> None:
        received_at = time.monotonic()
        if isinstance(event, KlineEvent):
            self.last_kline_at = received_at
            kline = event.value
            if kline.closed:
                self._append_closed(kline)
                if self.current_kline and self.current_kline.open_time_ms == kline.open_time_ms:
                    self.current_kline = None
            else:
                self.current_kline = kline
            self._append_price(kline.close_time_ms, kline.close)
        elif isinstance(event, TickerEvent):
            self.last_ticker_at = received_at
            self.ticker = event.value
            self._append_price(event.value.event_time_ms, event.value.last_price)
        elif isinstance(event, BookTickerEvent):
            self.last_book_at = received_at
            self.book = event.value
        elif isinstance(event, MarkPriceEvent):
            self.last_mark_at = received_at
            self.mark = event.value
        elif isinstance(event, AggTradeEvent):
            self.last_trade_at = received_at
            self._append_agg_trade(
                event.value.event_time_ms,
                event.value.quote_notional,
                event.value.aggressive_buy,
            )
        elif isinstance(event, DepthEvent):
            self.last_depth_at = received_at
            self.depth = event.value

    def _append_closed(self, kline: Kline) -> None:
        # REST warmup and WebSocket close may overlap; replace instead of duplicating.
        # REST-прогрев и закрытие WS могут совпасть; заменяем, а не дублируем.
        if self.closed_klines and self.closed_klines[-1].open_time_ms == kline.open_time_ms:
            self.closed_klines[-1] = kline
        elif not self.closed_klines or self.closed_klines[-1].open_time_ms < kline.open_time_ms:
            self.closed_klines.append(kline)

    def _append_price(self, timestamp_ms: int, price: float) -> None:
        if timestamp_ms <= 0 or price <= 0:
            return
        if self.price_points and timestamp_ms < self.price_points[-1][0]:
            return
        if self.price_points and timestamp_ms == self.price_points[-1][0]:
            self.price_points[-1] = (timestamp_ms, price)
        else:
            self.price_points.append((timestamp_ms, price))

    def _append_agg_trade(self, timestamp_ms: int, quote: float, aggressive_buy: bool) -> None:
        second = timestamp_ms // 1_000
        if not self.trade_buckets or self.trade_buckets[-1].second != second:
            self.trade_buckets.append(TradeBucket(second=second))
        bucket = self.trade_buckets[-1]
        if aggressive_buy:
            bucket.buy_quote += quote
        else:
            bucket.sell_quote += quote
        bucket.trade_count += 1

    def record_open_interest(self, point: OpenInterestPoint) -> None:
        if self.open_interest and point.timestamp_ms <= self.open_interest[-1].timestamp_ms:
            return
        self.open_interest.append(point)
        self.last_oi_at = time.monotonic()


class MarketState:
    def __init__(self, symbols: Sequence[str]) -> None:
        self._buffers = {symbol: SymbolBuffer() for symbol in symbols}

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(self._buffers)

    def get(self, symbol: str) -> SymbolBuffer | None:
        return self._buffers.get(symbol)

    async def handle(self, event: MarketEvent) -> None:
        value = event.value
        buffer = self._buffers.get(value.symbol)
        if buffer is not None:
            # No await inside apply: each mutation is atomic to the event loop.
            # В apply нет await: изменение атомарно для event loop.
            buffer.apply(event)

    def seed(self, symbol: str, klines: Sequence[Kline]) -> None:
        buffer = self._buffers.get(symbol)
        if buffer is not None:
            buffer.seed(klines)

    def record_open_interest(self, point: OpenInterestPoint) -> None:
        buffer = self._buffers.get(point.symbol)
        if buffer is not None:
            buffer.record_open_interest(point)


class WarmupLoader:
    def __init__(
        self,
        client: BinancePublicClient,
        state: MarketState,
        concurrency: int,
    ) -> None:
        self._client = client
        self._state = state
        self._semaphore = asyncio.Semaphore(concurrency)

    async def load(self, symbols: Sequence[str], limit: int) -> None:
        completed = 0
        total = len(symbols)
        counter_lock = asyncio.Lock()

        async def load_one(symbol: str) -> None:
            nonlocal completed
            async with self._semaphore:
                klines = await self._client.klines(symbol, limit=limit)
            self._state.seed(symbol, klines)
            async with counter_lock:
                completed += 1
                if completed == total or completed % 50 == 0:
                    log.info("warmup_progress", completed=completed, total=total)

        async with asyncio.TaskGroup() as group:
            for symbol in symbols:
                group.create_task(load_one(symbol))
