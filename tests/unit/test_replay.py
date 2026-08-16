"""Recorder/replay determinism tests. / Тесты детерминированности replay."""

import asyncio
from pathlib import Path

import pytest

from pumplens.domain.enums import Direction
from pumplens.domain.events import BookTickerEvent, KlineEvent
from pumplens.domain.models import BookTicker, Kline
from pumplens.replay.outcomes import evaluate_kline_outcome, evaluate_outcome
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


def test_kline_outcome_uses_high_low_and_marks_ambiguous_order() -> None:
    target_only = Kline("BTCUSDT", 0, 1, 100, 102.5, 99.5, 100, 1, 1, 1, 1)
    result = evaluate_kline_outcome(Direction.LONG, 100, [target_only])
    assert result.hit_rule == "TARGET_FIRST"
    assert result.mfe_pct == pytest.approx(2.5)
    assert result.mae_pct == pytest.approx(-0.5)

    both = Kline("BTCUSDT", 0, 1, 100, 103, 98, 100, 1, 1, 1, 1)
    assert evaluate_kline_outcome(Direction.LONG, 100, [both]).hit_rule == "AMBIGUOUS"


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


async def test_recorder_can_skip_book_ticker_and_rotate(tmp_path: Path) -> None:
    path = tmp_path / "market.jsonl"
    recorder = MarketEventRecorder(
        path,
        batch_size=1,
        max_file_size_bytes=150,
        gzip_rotated=True,
        record_book_ticker=False,
    )
    await recorder.open()
    await recorder.record(
        BookTickerEvent(BookTicker("BTCUSDT", 100, 1, 101, 1, 1))
    )
    for offset in range(3):
        await recorder.record(
            KlineEvent(
                Kline(
                    "BTCUSDT",
                    offset,
                    offset + 1,
                    100,
                    101,
                    99,
                    100,
                    1,
                    100,
                    10,
                    60,
                )
            )
        )
    await recorder.close()
    rotated = await asyncio.to_thread(lambda: list(tmp_path.glob("market-*.jsonl.gz")))
    assert rotated
    assert "BookTickerEvent" not in path.read_text()


async def test_recorder_sustains_bursty_market_input(tmp_path: Path) -> None:
    path = tmp_path / "market.jsonl"
    recorder = MarketEventRecorder(
        path,
        queue_size=2_000,
        batch_size=500,
        flush_interval_seconds=0.05,
    )
    await recorder.open()
    for offset in range(10_000):
        await recorder.record(
            KlineEvent(
                Kline(
                    "BTCUSDT",
                    offset,
                    offset + 1,
                    100,
                    101,
                    99,
                    100,
                    1,
                    100,
                    10,
                    60,
                )
            )
        )
        if offset % 100 == 0:
            await asyncio.sleep(0)
    await recorder.close()

    assert recorder.dropped_events == 0
    assert sum(1 for _ in path.open(encoding="utf-8")) == 10_000
