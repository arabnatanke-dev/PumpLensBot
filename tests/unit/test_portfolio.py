"""Portfolio parsing and tenant-row tests. / Тесты разбора портфеля."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

import pytest
from sqlalchemy import select

import pumplens.portfolio.service as portfolio_service_module
from pumplens.binance.signed_rest import BinanceCredentialError, BinanceReadOnlyClient
from pumplens.portfolio.service import (
    PortfolioReconciler,
    PortfolioService,
    _optional_earn,
    _optional_funding,
    _parse_positions,
    _parse_spot,
)
from pumplens.storage.db import Database
from pumplens.storage.models import (
    Base,
    EarnHoldingRecord,
    ExchangeAccountRecord,
    FundingHoldingRecord,
    PortfolioSnapshotRecord,
    SpotHoldingRecord,
    UserRecord,
)
from pumplens.telegram.clean_ui import _portfolio_data
from pumplens.telegram.formatter import format_portfolio


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


def test_earn_and_funding_values_keep_unknown_assets_partial() -> None:
    account_id = uuid.uuid4()
    earn, earn_total, earn_partial = _optional_earn(
        [
            {"asset": "USDT", "totalAmount": "5", "productId": "flex-usdt"},
            {"asset": "UNKNOWN", "totalAmount": "2", "productId": "flex-x"},
        ],
        "FLEXIBLE",
        {},
        account_id,
    )
    funding, funding_total, funding_partial = _optional_funding(
        [{"asset": "USDC", "free": "3", "locked": "1", "freeze": "1"}],
        {},
        account_id,
    )
    assert len(earn) == 2 and earn_total == Decimal("5") and earn_partial is True
    assert funding[0].amount == Decimal("5")
    assert funding_total == Decimal("5") and funding_partial is False


async def test_simple_earn_pagination_reads_every_page() -> None:
    client = BinanceReadOnlyClient("api", "secret")
    pages: list[int] = []

    async def fake_get(
        _client: object,
        _path: str,
        params: dict[str, int],
    ) -> dict[str, object]:
        page = params["current"]
        pages.append(page)
        count = 100 if page == 1 else 1
        return {"rows": [{"asset": "USDT"}] * count, "total": 101}

    cast(Any, client)._signed_get = fake_get
    try:
        rows = await client.simple_earn_flexible_positions()
    finally:
        await client.aclose()
    assert len(rows) == 101
    assert pages == [1, 2]


class _TestPortfolioService(PortfolioService):
    async def credentials(self, _session: object, _account_id: uuid.UUID) -> tuple[str, str]:
        return "api", "secret"


class _FakeBinanceClient:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    async def __aenter__(self) -> "_FakeBinanceClient":
        return self

    async def __aexit__(self, *_args: object) -> None:
        pass

    async def spot_account(self) -> dict[str, object]:
        return {"balances": [{"asset": "USDT", "free": "10", "locked": "0"}]}

    async def futures_account(self) -> dict[str, str]:
        return {
            "totalWalletBalance": "7.4",
            "totalMarginBalance": "9.4",
            "availableBalance": "5",
            "totalUnrealizedProfit": "2",
        }

    async def position_risk(self) -> list[dict[str, str]]:
        return []

    async def spot_prices(self) -> dict[str, float]:
        return {"BTCUSDT": 60_000.0}

    async def simple_earn_flexible_positions(self) -> list[dict[str, str]]:
        raise BinanceCredentialError("optional_forbidden")

    async def simple_earn_locked_positions(self) -> list[dict[str, str]]:
        return [{"asset": "USDT", "amount": "3", "projectId": "locked-1"}]

    async def funding_wallet(self) -> list[dict[str, str]]:
        return [{"asset": "USDT", "free": "4", "locked": "0"}]


async def test_optional_failure_preserves_core_and_marks_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(portfolio_service_module, "BinanceReadOnlyClient", _FakeBinanceClient)
    try:
        async with database.session() as session, session.begin():
            user = UserRecord(telegram_user_id=77, chat_id=77)
            session.add(user)
            await session.flush()
            account = ExchangeAccountRecord(
                user_id=user.id,
                exchange="BINANCE",
                permissions_json={},
                key_last4="1234",
            )
            session.add(account)
            await session.flush()
            account_id = account.id
            summary = await _TestPortfolioService(cast(Any, None)).reconcile(
                session, account_id
            )
        assert summary.data_quality == "PARTIAL"
        assert summary.earn_value is None
        assert summary.funding_value == Decimal("4")
        assert summary.source_status["earn_flexible"] == "UNAVAILABLE"
        async with database.session() as session:
            stored_account = await session.get(ExchangeAccountRecord, account_id)
            snapshot = await session.scalar(select(PortfolioSnapshotRecord))
            assert stored_account is not None and stored_account.status == "ACTIVE"
            assert snapshot is not None and snapshot.spot_value == Decimal("10")
            assert len(list(await session.scalars(select(EarnHoldingRecord)))) == 1
            assert len(list(await session.scalars(select(FundingHoldingRecord)))) == 1
    finally:
        await database.dispose()


def test_portfolio_total_does_not_double_count_unrealized_pnl() -> None:
    snapshot = PortfolioSnapshotRecord(
        exchange_account_id=uuid.uuid4(),
        ts=cast(Any, None),
        spot_value=Decimal("10"),
        futures_wallet=Decimal("7.4"),
        available=Decimal("5"),
        unrealized_pnl=Decimal("2"),
        earn_value=Decimal("3"),
        funding_value=Decimal("4"),
        source_status_json={},
        data_quality="FRESH",
    )
    text = format_portfolio(snapshot, [])
    assert "24.40 USDT" in text
    assert "26.40 USDT" not in text


async def test_telegram_spot_holdings_are_sorted_by_value() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        async with database.session() as session, session.begin():
            user = UserRecord(telegram_user_id=99, chat_id=99)
            session.add(user)
            await session.flush()
            account = ExchangeAccountRecord(
                user_id=user.id,
                exchange="BINANCE",
                permissions_json={},
                key_last4="1234",
            )
            session.add(account)
            await session.flush()
            session.add(
                PortfolioSnapshotRecord(
                    exchange_account_id=account.id,
                    ts=datetime.now(UTC),
                    spot_value=Decimal("11"),
                    futures_wallet=Decimal("0"),
                    available=Decimal("0"),
                    unrealized_pnl=Decimal("0"),
                    earn_value=Decimal("0"),
                    funding_value=Decimal("0"),
                    source_status_json={},
                    data_quality="FRESH",
                )
            )
            session.add_all(
                [
                    SpotHoldingRecord(
                        exchange_account_id=account.id,
                        asset="LOW",
                        free=Decimal("1"),
                        locked=Decimal("0"),
                        value_usdt=Decimal("1"),
                    ),
                    SpotHoldingRecord(
                        exchange_account_id=account.id,
                        asset="HIGH",
                        free=Decimal("1"),
                        locked=Decimal("0"),
                        value_usdt=Decimal("10"),
                    ),
                ]
            )
        data = await _portfolio_data(99, database)
        assert data is not None
        assert [row.asset for row in data.spot] == ["HIGH", "LOW"]
    finally:
        await database.dispose()


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
            session.add(
                PortfolioSnapshotRecord(
                    exchange_account_id=account_id,
                    ts=datetime.now(UTC),
                    spot_value=Decimal("1"),
                    futures_wallet=Decimal("1"),
                    available=Decimal("1"),
                    unrealized_pnl=Decimal("0"),
                    earn_value=None,
                    funding_value=None,
                    source_status_json={},
                    data_quality="FRESH",
                )
            )

        reconciler = PortfolioReconciler(database, _FailingPortfolioService())  # type: ignore[arg-type]
        await reconciler._reconcile_one(account_id)

        async with database.session() as session:
            status = await session.scalar(
                select(ExchangeAccountRecord.status).where(ExchangeAccountRecord.id == account_id)
            )
            quality = await session.scalar(
                select(PortfolioSnapshotRecord.data_quality).where(
                    PortfolioSnapshotRecord.exchange_account_id == account_id
                )
            )
        assert status == "STALE"
        assert quality == "STALE"
    finally:
        await database.dispose()
