"""Isolated Spot/Futures REST reconciliation. / Изолированная сверка Spot/Futures."""

from __future__ import annotations

import asyncio
import base64
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from pumplens.binance.signed_rest import BinanceCredentialError, BinanceReadOnlyClient
from pumplens.security.credential_vault import CredentialVault, EncryptedCredentials
from pumplens.storage.db import Database
from pumplens.storage.models import (
    EncryptedCredentialRecord,
    ExchangeAccountRecord,
    PortfolioSnapshotRecord,
    PositionRecord,
    SpotHoldingRecord,
    UserRecord,
)

log = structlog.get_logger(__name__)
ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class PortfolioSummary:
    exchange_account_id: uuid.UUID
    spot_value: Decimal
    futures_wallet: Decimal
    available: Decimal
    unrealized_pnl: Decimal
    positions_count: int
    data_quality: str


class PortfolioService:
    def __init__(self, vault: CredentialVault) -> None:
        self._vault = vault

    async def reconcile(
        self,
        session: AsyncSession,
        exchange_account_id: uuid.UUID,
    ) -> PortfolioSummary:
        account = await session.get(ExchangeAccountRecord, exchange_account_id)
        credential = await session.get(EncryptedCredentialRecord, exchange_account_id)
        if account is None or credential is None:
            raise ValueError("exchange_account_not_found")
        user = await session.get(UserRecord, account.user_id)
        if user is None:
            raise ValueError("exchange_account_user_not_found")

        encrypted = EncryptedCredentials(
            ciphertext_b64=base64.b64encode(credential.ciphertext).decode(),
            nonce_b64=base64.b64encode(credential.nonce).decode(),
            key_version=credential.key_version,
            api_key_last4=account.key_last4,
        )
        api_key, secret_key = self._vault.decrypt(
            encrypted,
            exchange_account_id=str(account.id),
            telegram_user_id=user.telegram_user_id,
        )

        try:
            async with BinanceReadOnlyClient(api_key, secret_key) as client:
                spot, futures, positions, prices = await asyncio.gather(
                    client.spot_account(),
                    client.futures_account(),
                    client.position_risk(),
                    client.spot_prices(),
                )
        except BinanceCredentialError:
            raise

        spot_rows, spot_value, partial = _parse_spot(spot, prices, account.id)
        position_rows = _parse_positions(positions, account.id)
        futures_wallet = _decimal(futures.get("totalWalletBalance"))
        available = _decimal(futures.get("availableBalance"))
        unrealized = _decimal(futures.get("totalUnrealizedProfit"))
        quality = "PARTIAL" if partial else "FRESH"

        # Replace only rows for this account; tenant isolation is explicit.
        # Заменяем строки только этого аккаунта; tenant isolation указана явно.
        await session.execute(
            delete(SpotHoldingRecord).where(
                SpotHoldingRecord.exchange_account_id == account.id
            )
        )
        await session.execute(
            delete(PositionRecord).where(PositionRecord.exchange_account_id == account.id)
        )
        session.add_all([*spot_rows, *position_rows])
        session.add(
            PortfolioSnapshotRecord(
                exchange_account_id=account.id,
                ts=datetime.now(UTC),
                spot_value=spot_value,
                futures_wallet=futures_wallet,
                available=available,
                unrealized_pnl=unrealized,
                data_quality=quality,
            )
        )
        account.status = "ACTIVE"
        account.last_sync_at = datetime.now(UTC)
        await session.flush()
        return PortfolioSummary(
            exchange_account_id=account.id,
            spot_value=spot_value,
            futures_wallet=futures_wallet,
            available=available,
            unrealized_pnl=unrealized,
            positions_count=len(position_rows),
            data_quality=quality,
        )


class PortfolioReconciler:
    """One failing key cannot affect other accounts. / Ошибка ключа не влияет на других."""

    def __init__(
        self,
        database: Database,
        service: PortfolioService,
        interval_seconds: int = 300,
        concurrency: int = 8,
    ) -> None:
        self._database = database
        self._service = service
        self._interval_seconds = interval_seconds
        self._semaphore = asyncio.Semaphore(concurrency)

    async def run(self) -> None:
        while True:
            async with self._database.session() as session:
                ids = tuple(
                    await session.scalars(
                        select(ExchangeAccountRecord.id).where(
                            ExchangeAccountRecord.status.in_(["ACTIVE", "STALE"])
                        )
                    )
                )
            async with asyncio.TaskGroup() as group:
                for account_id in ids:
                    group.create_task(self._reconcile_one(account_id))
            await asyncio.sleep(self._interval_seconds)

    async def _reconcile_one(self, account_id: uuid.UUID) -> None:
        async with self._semaphore:
            try:
                async with self._database.session() as session, session.begin():
                    await self._service.reconcile(session, account_id)
            except BinanceCredentialError as exc:
                await self._mark_stale(account_id)
                log.warning(
                    "portfolio_reconcile_failed",
                    exchange_account_id=str(account_id),
                    error=type(exc).__name__,
                )
            except Exception as exc:
                log.warning(
                    "portfolio_reconcile_failed",
                    exchange_account_id=str(account_id),
                    error=type(exc).__name__,
                )

    async def _mark_stale(self, account_id: uuid.UUID) -> None:
        """Commit STALE outside the failed sync. / Коммитит STALE вне неудачной сверки."""

        async with self._database.session() as session, session.begin():
            account = await session.get(ExchangeAccountRecord, account_id)
            if account is not None:
                account.status = "STALE"


def _parse_spot(
    payload: dict[str, Any] | Any,
    prices: dict[str, float],
    account_id: uuid.UUID,
) -> tuple[list[SpotHoldingRecord], Decimal, bool]:
    rows: list[SpotHoldingRecord] = []
    total = ZERO
    partial = False
    balances = payload.get("balances", []) if isinstance(payload, dict) else []
    for item in balances:
        asset = str(item.get("asset", ""))
        free = _decimal(item.get("free"))
        locked = _decimal(item.get("locked"))
        amount = free + locked
        if amount <= 0 or not asset:
            continue
        value: Decimal | None
        if asset in {"USDT", "USDC", "FDUSD"}:
            value = amount
        else:
            price = prices.get(f"{asset}USDT")
            value = amount * Decimal(str(price)) if price is not None else None
            partial = partial or value is None
        if value is not None:
            total += value
        rows.append(
            SpotHoldingRecord(
                exchange_account_id=account_id,
                asset=asset,
                free=free,
                locked=locked,
                value_usdt=value,
            )
        )
    return rows, total, partial


def _parse_positions(
    payload: list[dict[str, Any]] | Any,
    account_id: uuid.UUID,
) -> list[PositionRecord]:
    rows: list[PositionRecord] = []
    if not isinstance(payload, list):
        return rows
    for item in payload:
        amount = _decimal(item.get("positionAmt"))
        if amount == 0:
            continue
        side = "LONG" if amount > 0 else "SHORT"
        rows.append(
            PositionRecord(
                exchange_account_id=account_id,
                symbol=str(item.get("symbol", "")),
                side=side,
                amount=amount,
                entry=_decimal(item.get("entryPrice")),
                break_even=_optional_decimal(item.get("breakEvenPrice")),
                mark=_decimal(item.get("markPrice")),
                liquidation=_optional_decimal(item.get("liquidationPrice")),
                leverage=int(item.get("leverage", 1)),
                margin_type=str(item.get("marginType", "unknown")),
                pnl=_decimal(item.get("unRealizedProfit")),
            )
        )
    return rows


def _decimal(value: object) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except InvalidOperation:
        return ZERO


def _optional_decimal(value: object) -> Decimal | None:
    parsed = _decimal(value)
    return parsed if parsed != 0 else None
