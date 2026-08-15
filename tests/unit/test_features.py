"""Deterministic formula tests. / Детерминированные тесты формул."""

import pytest

from pumplens.analytics.buffers import MarketState
from pumplens.analytics.features import (
    FeatureEngine,
    candle_structure,
    median_absolute_deviation,
    pct_return,
    robust_z_score,
)
from pumplens.domain.enums import DataQuality, Direction
from pumplens.domain.events import (
    AggTradeEvent,
    BookTickerEvent,
    DepthEvent,
    KlineEvent,
    MarkPriceEvent,
    TickerEvent,
)
from pumplens.domain.models import (
    AggTrade,
    BookTicker,
    DepthSnapshot,
    Kline,
    MarkPrice,
    OpenInterestPoint,
    Ticker24h,
)


def make_kline(*, open_: float, high: float, low: float, close: float) -> Kline:
    return Kline(
        symbol="TESTUSDT",
        open_time_ms=0,
        close_time_ms=59_999,
        open=open_,
        high=high,
        low=low,
        close=close,
        base_volume=1,
        quote_volume=100,
        trade_count=10,
        taker_buy_quote_volume=60,
    )


def test_percentage_return_uses_percentage_points() -> None:
    assert pct_return(102.5, 100) == pytest.approx(2.5)


def test_robust_statistics_resist_outlier() -> None:
    history = [10, 10, 11, 9, 10, 1_000]
    assert median_absolute_deviation(history) == pytest.approx(0.5)
    assert robust_z_score(12, history) > 1


def test_candle_structure_rewards_close_near_directional_extreme() -> None:
    strong_long = make_kline(open_=100, high=105, low=99, close=104.9)
    weak_long = make_kline(open_=100, high=105, low=99, close=101)
    assert candle_structure(strong_long, Direction.LONG) > candle_structure(
        weak_long, Direction.LONG
    )


def _seeded_state() -> MarketState:
    state = MarketState(["TESTUSDT"])
    state.seed(
        "TESTUSDT",
        [
            Kline(
                symbol="TESTUSDT",
                open_time_ms=index * 60_000,
                close_time_ms=index * 60_000 + 59_999,
                open=100,
                high=102,
                low=99,
                close=101,
                base_volume=10,
                quote_volume=1_000,
                trade_count=20,
                taker_buy_quote_volume=600,
            )
            for index in range(31)
        ],
    )
    return state


def _apply_required_streams(state: MarketState) -> None:
    buffer = state.get("TESTUSDT")
    assert buffer is not None
    buffer.apply(
        KlineEvent(
            Kline(
                "TESTUSDT",
                31 * 60_000,
                32 * 60_000 - 1,
                101,
                103,
                100,
                102,
                12,
                1_200,
                24,
                720,
                closed=False,
            )
        )
    )
    buffer.apply(TickerEvent(Ticker24h("TESTUSDT", 102, 1_000_000, 2.0, 1)))
    buffer.apply(BookTickerEvent(BookTicker("TESTUSDT", 101.9, 5, 102.1, 5, 1)))
    buffer.apply(MarkPriceEvent(MarkPrice("TESTUSDT", 102, 102, 0.0, 1)))


def test_one_fresh_stream_cannot_mask_stale_required_streams() -> None:
    state = _seeded_state()
    buffer = state.get("TESTUSDT")
    assert buffer is not None
    buffer.apply(BookTickerEvent(BookTicker("TESTUSDT", 100, 1, 101, 1, 1)))
    engine = FeatureEngine(state, stale_after_seconds=12)

    assert engine.snapshot_all()[0].data_quality is DataQuality.STALE

    _apply_required_streams(state)
    assert engine.snapshot_all()[0].data_quality is DataQuality.FRESH

    for timestamp_name in (
        "last_kline_at",
        "last_ticker_at",
        "last_book_at",
        "last_mark_at",
    ):
        received_at = getattr(buffer, timestamp_name)
        setattr(buffer, timestamp_name, 0.0)
        assert engine.snapshot_all()[0].data_quality is DataQuality.STALE
        setattr(buffer, timestamp_name, received_at)


def test_deep_data_requires_fresh_trade_depth_and_open_interest() -> None:
    state = _seeded_state()
    _apply_required_streams(state)
    buffer = state.get("TESTUSDT")
    assert buffer is not None
    buffer.apply(AggTradeEvent(AggTrade("TESTUSDT", 102, 1, 1_000, False)))
    buffer.apply(
        DepthEvent(
            DepthSnapshot("TESTUSDT", bids=((101.9, 5),), asks=((102.1, 5),), event_time_ms=1)
        )
    )
    buffer.record_open_interest(OpenInterestPoint("TESTUSDT", 1_000, 1))
    engine = FeatureEngine(state, stale_after_seconds=12, oi_stale_after_seconds=60)

    assert engine.snapshot_all()[0].deep_data_ready is True

    for timestamp_name in ("last_trade_at", "last_depth_at", "last_oi_at"):
        received_at = getattr(buffer, timestamp_name)
        setattr(buffer, timestamp_name, 0.0)
        assert engine.snapshot_all()[0].deep_data_ready is False
        setattr(buffer, timestamp_name, received_at)
