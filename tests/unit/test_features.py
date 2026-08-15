"""Deterministic formula tests. / Детерминированные тесты формул."""

import pytest

from pumplens.analytics.features import (
    candle_structure,
    median_absolute_deviation,
    pct_return,
    robust_z_score,
)
from pumplens.domain.enums import Direction
from pumplens.domain.models import Kline


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
