"""Bounded production outcome sampling. / Ограниченный production sampling outcomes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from pumplens.binance.public_rest import BinancePublicClient
from pumplens.domain.models import Kline
from pumplens.replay.evaluator import EarlyOutcomeEvaluator, SignalOutcomeEvaluator
from pumplens.storage.db import Database
from pumplens.storage.models import (
    Base,
    EarlyOutcomeRecord,
    SignalOutcomeRecord,
    SignalRecord,
)


@pytest.fixture
async def database() -> Database:
    db = Database("sqlite+aiosqlite:///:memory:")
    async with db.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield db
    finally:
        await db.dispose()


async def test_all_symbol_ticker_prices_use_one_weight_two_request() -> None:
    client = BinancePublicClient("https://example.invalid")
    request = AsyncMock(
        return_value=[
            {"symbol": "BTCUSDT", "price": "60000.1"},
            {"symbol": "ETHUSDT", "price": "3000.2"},
        ]
    )
    client._get = request  # type: ignore[method-assign]
    try:
        assert await client.ticker_prices() == {
            "BTCUSDT": 60000.1,
            "ETHUSDT": 3000.2,
        }
        request.assert_awaited_once_with("/fapi/v2/ticker/price", weight=2)
    finally:
        await client.aclose()


async def test_early_outcomes_share_one_ticker_request(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    async with database.session() as session, session.begin():
        session.add_all(
            [
                SignalRecord(
                    symbol=f"TEST{index}USDT",
                    direction="LONG",
                    state="EARLY",
                    score=60,
                    start_price=Decimal("100"),
                    early_at=now - timedelta(seconds=20),
                    early_price=Decimal("100"),
                )
                for index in range(12)
            ]
        )

    class FakeClient:
        def __init__(self) -> None:
            self.calls = 0

        async def ticker_prices(self) -> dict[str, float]:
            self.calls += 1
            return {f"TEST{index}USDT": 101.0 for index in range(12)}

    client = FakeClient()
    evaluator = EarlyOutcomeEvaluator(database, "https://example.invalid")
    await evaluator.evaluate_once(cast(BinancePublicClient, cast(Any, client)))

    assert client.calls == 1
    async with database.session() as session:
        assert await session.scalar(select(func.count(EarlyOutcomeRecord.id))) == 12


async def test_signal_outcomes_are_staggered_in_bounded_batches(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    observed_at = now - timedelta(minutes=10)
    async with database.session() as session, session.begin():
        session.add_all(
            [
                SignalRecord(
                    symbol=f"TEST{index}USDT",
                    direction="LONG",
                    state="WATCH",
                    score=72,
                    start_price=Decimal("100"),
                    watch_at=observed_at,
                )
                for index in range(12)
            ]
        )

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def klines(self, symbol: str, **_kwargs: object) -> list[Kline]:
            self.calls.append(symbol)
            return [
                Kline(
                    symbol=symbol,
                    open_time_ms=int(observed_at.timestamp() * 1_000),
                    close_time_ms=int((observed_at + timedelta(minutes=6)).timestamp() * 1_000),
                    open=100,
                    high=101,
                    low=99,
                    close=100.5,
                    base_volume=1,
                    quote_volume=100,
                    trade_count=10,
                    taker_buy_quote_volume=60,
                    closed=True,
                )
            ]

    client = FakeClient()
    evaluator = SignalOutcomeEvaluator(
        database,
        "https://example.invalid",
        batch_size=5,
        request_spacing_seconds=0,
    )
    await evaluator.evaluate_once(cast(BinancePublicClient, cast(Any, client)))

    assert len(client.calls) == 5
    async with database.session() as session:
        assert await session.scalar(select(func.count(SignalOutcomeRecord.id))) == 5
