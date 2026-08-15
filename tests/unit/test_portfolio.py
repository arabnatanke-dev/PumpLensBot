"""Portfolio parsing and tenant-row tests. / Тесты разбора портфеля."""

import uuid
from decimal import Decimal

from sqlalchemy import select

from pumplens.binance.signed_rest import BinanceCredentialError
from pumplens.portfolio.service import PortfolioReconciler, _parse_positions, _parse_spot
from pumplens.storage.db import Database
from pumplens.storage.models import Base, ExchangeAccountRecord, UserRecord


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


class _FailingPortfolioService:
    async def reconcile(self, _session: object, _account_id: uuid.UUID) -> None:
        raise BinanceCredentialError("invalid_key")


async def test_binance_failure_commits_stale_in_separate_transaction() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        async with database.session() as session, session.begin():
            user = UserRecord(telegram_user_id=42, chat_id=42)
            session.add(user)
            await session.flush()
            account = ExchangeAccountRecord(
                user_id=user.id,
                exchange="BINANCE",
                label="Main",
                status="ACTIVE",
                permissions_json={},
                key_last4="1234",
            )
            session.add(account)
            await session.flush()
            account_id = account.id

        reconciler = PortfolioReconciler(database, _FailingPortfolioService())  # type: ignore[arg-type]
        await reconciler._reconcile_one(account_id)

        async with database.session() as session:
            status = await session.scalar(
                select(ExchangeAccountRecord.status).where(ExchangeAccountRecord.id == account_id)
            )
        assert status == "STALE"
    finally:
        await database.dispose()
