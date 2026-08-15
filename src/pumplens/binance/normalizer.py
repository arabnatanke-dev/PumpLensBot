"""Convert Binance payloads into domain events. / Нормализует payload Binance."""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from pumplens.domain.events import (
    AggTradeEvent,
    BookTickerEvent,
    DepthEvent,
    KlineEvent,
    MarketEvent,
    MarkPriceEvent,
    TickerEvent,
)
from pumplens.domain.models import AggTrade, BookTicker, DepthSnapshot, Kline, MarkPrice, Ticker24h


class BinanceNormalizer:
    def __init__(self, known_symbols: frozenset[str]) -> None:
        self._known_symbols = known_symbols

    def normalize(self, payload: Any) -> list[MarketEvent]:
        """Accept raw or combined-stream envelopes. / Принимает raw и combined payload."""

        if isinstance(payload, Mapping) and "data" in payload:
            payload = payload["data"]
        if isinstance(payload, list):
            events: list[MarketEvent] = []
            for item in payload:
                events.extend(self.normalize(item))
            return events
        if not isinstance(payload, Mapping):
            return []

        symbol = str(payload.get("s", ""))
        if symbol not in self._known_symbols:
            return []

        # Some all-contract futures streams expose st=1 for USD-M.
        # Некоторые общие futures-потоки помечают USD-M как st=1.
        stream_type = payload.get("st")
        if stream_type is not None and int(stream_type) != 1:
            return []

        event_type = str(payload.get("e", ""))
        now_ms = int(time.time() * 1_000)

        if event_type == "kline" and isinstance(payload.get("k"), Mapping):
            k = payload["k"]
            return [
                KlineEvent(
                    Kline(
                        symbol=symbol,
                        open_time_ms=int(k["t"]),
                        close_time_ms=int(k["T"]),
                        open=float(k["o"]),
                        high=float(k["h"]),
                        low=float(k["l"]),
                        close=float(k["c"]),
                        base_volume=float(k["v"]),
                        quote_volume=float(k["q"]),
                        trade_count=int(k["n"]),
                        taker_buy_quote_volume=float(k["Q"]),
                        closed=bool(k["x"]),
                    )
                )
            ]
        if event_type == "bookTicker" or (not event_type and {"b", "a"} <= payload.keys()):
            return [
                BookTickerEvent(
                    BookTicker(
                        symbol=symbol,
                        bid_price=float(payload["b"]),
                        bid_quantity=float(payload.get("B", 0.0)),
                        ask_price=float(payload["a"]),
                        ask_quantity=float(payload.get("A", 0.0)),
                        event_time_ms=int(payload.get("E", payload.get("T", now_ms))),
                    )
                )
            ]
        if event_type == "24hrTicker":
            return [
                TickerEvent(
                    Ticker24h(
                        symbol=symbol,
                        last_price=float(payload["c"]),
                        quote_volume=float(payload["q"]),
                        price_change_pct=float(payload["P"]),
                        event_time_ms=int(payload.get("E", now_ms)),
                    )
                )
            ]
        if event_type == "markPriceUpdate":
            return [
                MarkPriceEvent(
                    MarkPrice(
                        symbol=symbol,
                        mark_price=float(payload["p"]),
                        index_price=float(payload["i"]),
                        funding_rate=float(payload.get("r", 0.0)),
                        event_time_ms=int(payload.get("E", now_ms)),
                    )
                )
            ]
        if event_type in {"aggTrade", "trade"}:
            return [
                AggTradeEvent(
                    AggTrade(
                        symbol=symbol,
                        price=float(payload["p"]),
                        quantity=float(payload["q"]),
                        event_time_ms=int(payload.get("T", payload.get("E", now_ms))),
                        buyer_is_maker=bool(payload["m"]),
                    )
                )
            ]
        if event_type == "depthUpdate" or {"b", "a"} <= payload.keys():
            bids = payload.get("b", payload.get("bids", ()))
            asks = payload.get("a", payload.get("asks", ()))
            if isinstance(bids, list) and isinstance(asks, list):
                return [
                    DepthEvent(
                        DepthSnapshot(
                            symbol=symbol,
                            bids=tuple((float(row[0]), float(row[1])) for row in bids),
                            asks=tuple((float(row[0]), float(row[1])) for row in asks),
                            event_time_ms=int(payload.get("E", payload.get("T", now_ms))),
                        )
                    )
                ]
        return []
