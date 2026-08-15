"""Recorder/replay determinism tests. / Тесты детерминированности replay."""

from pathlib import Path

import pytest

from pumplens.domain.enums import Direction
from pumplens.domain.events import BookTickerEvent, KlineEvent
from pumplens.domain.models import BookTicker, Kline
from pumplens.replay.outcomes import evaluate_outcome
from pumplens.replay.reader import ReplayReader
from pumplens.replay.recorder import MarketEventRecorder


@pytest.mark.parametrize(
    ("direction", "prices", "expected"),
    [
        (Direction.LONG, [98.4, 102, 103], "STOP_FIRST"),
        (Direction.SHORT, [99, 98, 101], "TARGET_FIRST"),
    ],
)
def test_outcome_hit_order(
    direction: Direction,
    prices: list[float],
    expected: str,
) -> None:
    outcome = evaluate_outcome(direction, 100, prices)
    assert outcome.hit_rule == expected


async def test_same_recording_replays_identically(tmp_path: Path) -> None:
    path = tmp_path / "market.jsonl"
    recorder = MarketEventRecorder(path)
    await recorder.open()
    await recorder.record(
        KlineEvent(
            Kline(
                symbol="BTCUSDT",
                open_time_ms=0,
                close_time_ms=59_999,
                open=100,
                high=102,
                low=99,
                close=101,
                base_volume=1,
                quote_volume=100,
                trade_count=10,
                taker_buy_quote_volume=60,
            )
        )
    )
    await recorder.record(
        BookTickerEvent(
            BookTicker(
                symbol="BTCUSDT",
                bid_price=100,
                bid_quantity=1,
                ask_price=100.1,
                ask_quantity=1,
                event_time_ms=60_000,
            )
        )
    )
    await recorder.close()

    async def collect() -> list[tuple[str, str]]:
        rows: list[tuple[str, str]] = []

        async def handler(event: object) -> None:
            rows.append((type(event).__name__, event.value.symbol))

        count = await ReplayReader(path).play(handler, speed=0)  # type: ignore[arg-type]
        assert count == 2
        return rows

    assert await collect() == await collect()
    assert len(ReplayReader(path).sha256()) == 64
