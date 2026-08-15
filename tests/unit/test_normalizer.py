"""Binance payload normalization tests. / Тесты нормализации payload Binance."""

import pytest

from pumplens.binance.normalizer import BinanceNormalizer
from pumplens.domain.events import (
    AggTradeEvent,
    BookTickerEvent,
    DepthEvent,
    KlineEvent,
    TickerEvent,
)


def test_combined_kline_is_normalized() -> None:
    normalizer = BinanceNormalizer(frozenset({"BTCUSDT"}))
    payload = {
        "stream": "btcusdt@kline_1m",
        "data": {
            "e": "kline",
            "E": 1_700_000_001_000,
            "s": "BTCUSDT",
            "k": {
                "t": 1_700_000_000_000,
                "T": 1_700_000_059_999,
                "s": "BTCUSDT",
                "o": "100",
                "h": "103",
                "l": "99",
                "c": "102",
                "v": "10",
                "q": "1010",
                "n": 42,
                "Q": "700",
                "x": False,
            },
        },
    }
    events = normalizer.normalize(payload)
    assert len(events) == 1
    assert isinstance(events[0], KlineEvent)
    assert events[0].value.close == 102
    assert events[0].value.taker_buy_quote_volume == 700


def test_all_market_array_is_flattened_and_unknown_symbol_is_dropped() -> None:
    normalizer = BinanceNormalizer(frozenset({"BTCUSDT"}))
    payload = {
        "stream": "!ticker@arr",
        "data": [
            {"e": "24hrTicker", "s": "BTCUSDT", "c": "101", "q": "5000000", "P": "2"},
            {"e": "24hrTicker", "s": "UNKNOWN", "c": "1", "q": "1", "P": "0"},
        ],
    }
    events = normalizer.normalize(payload)
    assert len(events) == 1
    assert isinstance(events[0], TickerEvent)


def test_non_usdm_stream_type_is_rejected() -> None:
    normalizer = BinanceNormalizer(frozenset({"BTCUSDT"}))
    payload = {
        "e": "bookTicker",
        "s": "BTCUSDT",
        "st": 2,
        "b": "100",
        "B": "2",
        "a": "101",
        "A": "3",
    }
    assert normalizer.normalize(payload) == []


def test_book_ticker_without_event_name_is_supported() -> None:
    normalizer = BinanceNormalizer(frozenset({"BTCUSDT"}))
    events = normalizer.normalize(
        {"s": "BTCUSDT", "b": "100", "B": "2", "a": "100.1", "A": "3"}
    )
    assert isinstance(events[0], BookTickerEvent)
    assert events[0].value.spread_pct > 0


def test_agg_trade_side_and_depth_are_normalized() -> None:
    normalizer = BinanceNormalizer(frozenset({"BTCUSDT"}))
    trade = normalizer.normalize(
        {
            "e": "aggTrade",
            "s": "BTCUSDT",
            "p": "100",
            "q": "2",
            "T": 1_700_000_000_000,
            "m": False,
        }
    )[0]
    depth = normalizer.normalize(
        {
            "e": "depthUpdate",
            "s": "BTCUSDT",
            "E": 1_700_000_000_001,
            "b": [["99.9", "3"]],
            "a": [["100.1", "4"]],
        }
    )[0]
    assert isinstance(trade, AggTradeEvent)
    assert trade.value.aggressive_buy is True
    assert trade.value.quote_notional == 200
    assert isinstance(depth, DepthEvent)
    bid_depth, ask_depth = depth.value.depth_within_pct()
    assert bid_depth == pytest.approx(299.7)
    assert ask_depth == pytest.approx(400.4)


def test_raw_trade_uses_the_same_aggressor_semantics() -> None:
    normalizer = BinanceNormalizer(frozenset({"BTCUSDT"}))
    event = normalizer.normalize(
        {
            "e": "trade",
            "s": "BTCUSDT",
            "p": "100",
            "q": "2",
            "T": 1_700_000_000_000,
            "m": True,
        }
    )[0]
    assert isinstance(event, AggTradeEvent)
    assert event.value.aggressive_buy is False
