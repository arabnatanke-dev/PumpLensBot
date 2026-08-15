"""Portfolio risk math tests. / Тесты математики портфельного риска."""

from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

from pumplens.domain.models import MarkPrice
from pumplens.portfolio.live_marks import LiveMarkPriceStore
from pumplens.portfolio.risk import (
    RiskAlertWorker,
    _advance_milestone,
    _liquidation_distance_pct,
    _milestone,
    _position_roi_pct,
)
from pumplens.storage.db import Database
from pumplens.storage.models import PositionRecord, PositionRiskStateRecord


def _position(**overrides: object) -> PositionRecord:
    values: dict[str, object] = {
        "exchange_account_id": uuid.uuid4(),
        "symbol": "BTCUSDT",
        "side": "LONG",
        "amount": Decimal("0.01"),
        "entry": Decimal("60000"),
        "mark": Decimal("61000"),
        "liquidation": Decimal("59000"),
        "leverage": 10,
        "margin_type": "isolated",
        "pnl": Decimal("10"),
    }
    values.update(overrides)
    return PositionRecord(**values)


def test_margin_distance_and_roi_are_position_aware() -> None:
    position = _position()
    assert round(_liquidation_distance_pct(position) or 0, 3) == 3.279
    assert round(_position_roi_pct(position), 3) == 16.667


def test_pnl_milestone_uses_highest_reached_signed_level() -> None:
    assert _milestone(11.0, (5.0, 8.0, 10.0, 15.0)) == 10.0
    assert _milestone(-8.5, (5.0, 8.0, 10.0)) == -8.0
    assert _milestone(3.0, (5.0, 8.0)) is None


def test_pnl_milestones_are_one_shot_on_rollbacks() -> None:
    state = PositionRiskStateRecord(
        exchange_account_id=uuid.uuid4(),
        symbol="BTCUSDT",
        side="LONG",
        max_profit_milestone=0,
        max_loss_milestone=0,
        last_roi_pct=0,
    )
    levels = (5.0, 8.0, 10.0)
    assert [_advance_milestone(state, value, levels) for value in (4, 5, 8, 10, 9)] == [
        None,
        5,
        8,
        10,
        None,
    ]
    assert _advance_milestone(state, -8, levels) == -8
    assert _advance_milestone(state, -6, levels) is None


def test_live_mark_store_rejects_stale_prices() -> None:
    store = LiveMarkPriceStore()
    store.update(MarkPrice("BTCUSDT", 62_000, 62_000, 0, 1))
    mark = store.get("BTCUSDT", max_age_seconds=5)
    assert mark is not None and mark.price == Decimal("62000")
    assert (
        store.get(
            "BTCUSDT",
            max_age_seconds=5,
            now_monotonic=mark.received_monotonic + 6,
        )
        is None
    )


def test_roi_can_be_recalculated_from_live_mark() -> None:
    position = _position(mark=Decimal("61000"), pnl=Decimal("10"))
    assert round(_position_roi_pct(position, Decimal("62000")), 3) == 33.333


async def test_resolved_claim_is_not_delivered() -> None:
    class Session:
        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def get(self, *_args: object) -> object:
            return SimpleNamespace(active=False, status="RESOLVED")

    class FakeDatabase:
        def session(self) -> Session:
            return Session()

    class FakeBot:
        sent = False

        async def send_message(self, *_args: object, **_kwargs: object) -> None:
            self.sent = True

    bot = FakeBot()
    worker = RiskAlertWorker(
        cast(Database, cast(Any, FakeDatabase())),
        cast(Any, bot),
        None,
    )
    await worker._deliver(uuid.uuid4())
    assert not bot.sent
