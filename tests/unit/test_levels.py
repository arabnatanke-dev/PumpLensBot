"""Signal level tests. / Тесты уровней сигнала."""

from pumplens.analytics.levels import calculate_levels
from pumplens.config import LateSettings
from pumplens.domain.enums import Direction
from pumplens.domain.models import Kline


def bar(low: float, high: float) -> Kline:
    return Kline(
        symbol="TESTUSDT",
        open_time_ms=0,
        close_time_ms=59_999,
        open=(low + high) / 2,
        high=high,
        low=low,
        close=(low + high) / 2,
        base_volume=1,
        quote_volume=1,
        trade_count=1,
        taker_buy_quote_volume=0.5,
    )


def test_long_invalidation_uses_low_of_last_three_bars() -> None:
    levels = calculate_levels(
        Direction.LONG,
        start_price=100,
        trigger_price=102,
        recent_closed=[bar(98, 101), bar(97, 102), bar(99, 103)],
        late=LateSettings(),
    )
    assert levels.invalidation == 97
    assert levels.late_line_5m == 108


def test_short_levels_are_mirrored() -> None:
    levels = calculate_levels(
        Direction.SHORT,
        start_price=100,
        trigger_price=98,
        recent_closed=[bar(98, 101), bar(97, 103), bar(96, 102)],
        late=LateSettings(),
    )
    assert levels.invalidation == 103
    assert levels.late_line_5m == 92
