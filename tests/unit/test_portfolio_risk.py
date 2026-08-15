"""Portfolio risk math tests. / Тесты математики портфельного риска."""

import uuid
from decimal import Decimal

from pumplens.portfolio.risk import _liquidation_distance_pct, _milestone, _position_roi_pct
from pumplens.storage.models import PositionRecord


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
