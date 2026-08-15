"""Portfolio parsing and tenant-row tests. / Тесты разбора портфеля."""

import uuid
from decimal import Decimal

from pumplens.portfolio.service import _parse_positions, _parse_spot


def test_spot_value_is_labeled_and_partial_when_price_missing() -> None:
    account_id = uuid.uuid4()
    rows, total, partial = _parse_spot(
        {
            "balances": [
                {"asset": "USDT", "free": "10", "locked": "2"},
                {"asset": "BTC", "free": "0.01", "locked": "0"},
                {"asset": "UNKNOWN", "free": "5", "locked": "0"},
            ]
        },
        {"BTCUSDT": 60_000},
        account_id,
    )
    assert total == Decimal("612.00")
    assert len(rows) == 3
    assert partial is True
    assert all(row.exchange_account_id == account_id for row in rows)


def test_only_open_futures_positions_are_stored() -> None:
    account_id = uuid.uuid4()
    rows = _parse_positions(
        [
            {
                "symbol": "BTCUSDT",
                "positionAmt": "0.01",
                "entryPrice": "60000",
                "breakEvenPrice": "60010",
                "markPrice": "61000",
                "liquidationPrice": "50000",
                "leverage": "3",
                "marginType": "isolated",
                "unRealizedProfit": "10",
            },
            {"symbol": "ETHUSDT", "positionAmt": "0"},
        ],
        account_id,
    )
    assert len(rows) == 1
    assert rows[0].side == "LONG"
    assert rows[0].pnl == Decimal("10")
