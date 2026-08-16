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
    EarnHoldingRecord,
    EncryptedCredentialRecord,
    ExchangeAccountRecord,
    FundingHoldingRecord,
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
    earn_value: Decimal | None
    funding_value: Decimal | None
    positions_count: int
    data_quality: str
    source_status: dict[str, str]


class PortfolioService:
    def __init__(self, vault: CredentialVault) -> None:
        self._vault = vault

    async def reconcile(
        self,
        session: AsyncSession,
        exchange_account_id: uuid.UUID,
    ) -> PortfolioSummary:
        account = await session.get(ExchangeAccountRecord, exchange_account_id)
        if account is None:
            raise ValueError("exchange_account_not_found")
        api_key, secret_key = await self.credentials(session, exchange_account_id)

        async with BinanceReadOnlyClient(api_key, secret_key) as client:
            try:
                spot, futures, positions, prices = await asyncio.gather(
                    client.spot_account(),
                    client.futures_account(),
                    client.position_risk(),
                    client.spot_prices(),
                )
            except BinanceCredentialError:
                raise
            optional = await asyncio.gather(
                client.simple_earn_flexible_positions(),
                client.simple_earn_locked_positions(),
                client.funding_wallet(),
                return_exceptions=True,
            )

        spot_rows, spot_value, partial = _parse_spot(spot, prices, account.id)
        position_rows = _parse_positions(positions, account.id)
        # Binance USD-M Account Information V3 defines totalMarginBalance as current
        # margin equity (wallet balance including unrealized PnL). Store that single
        # documented field in the legacy futures_wallet column, and never add
        # totalUnrealizedProfit again. / Account Information V3 определяет
        # totalMarginBalance как текущий equity с unrealized PnL; второй раз PnL не
        # прибавляем. Docs: https://developers.binance.com/docs/derivatives/
        # usds-margined-futures/account/rest-api-v3/Account-Information-V3
        futures_wallet = _decimal(futures.get("totalMarginBalance"))
        available = _decimal(futures.get("availableBalance"))
        unrealized = _decimal(futures.get("totalUnrealizedProfit"))
        source_status = {
            "spot": "PARTIAL" if partial else "FRESH",
            "futures": "FRESH",
        }
        flexible_rows, flexible_value, flexible_partial = _optional_earn(
            optional[0], "FLEXIBLE", prices, account.id
        )
        locked_rows, locked_value, locked_partial = _optional_earn(
            optional[1], "LOCKED", prices, account.id
        )
        funding_rows, funding_value, funding_partial = _optional_funding(
            optional[2], prices, account.id
        )
        source_status["earn_flexible"] = _optional_status(optional[0], flexible_partial)
        source_status["earn_locked"] = _optional_status(optional[1], locked_partial)
        source_status["funding"] = _optional_status(optional[2], funding_partial)
        earn_available = not isinstance(optional[0], BaseException) and not isinstance(
            optional[1], BaseException
        )
        earn_value = flexible_value + locked_value if earn_available else None
        quality = (
            "PARTIAL"
            if partial
            or any(status != "FRESH" for status in source_status.values())
            else "FRESH"
        )

        # Replace only rows for this account; tenant isolation is explicit.
        # Заменяем строки только этого аккаунта; tenant isolation указана явно.
        await session.execute(
            delete(SpotHoldingRecord).where(
                SpotHoldingRecord.exchange_account_id == account.id
            )
        )
        if not isinstance(optional[0], BaseException):
            await session.execute(
                delete(EarnHoldingRecord).where(
                    EarnHoldingRecord.exchange_account_id == account.id,
                    EarnHoldingRecord.product_type == "FLEXIBLE",
                )
            )
        if not isinstance(optional[1], BaseException):
            await session.execute(
                delete(EarnHoldingRecord).where(
                    EarnHoldingRecord.exchange_account_id == account.id,
                    EarnHoldingRecord.product_type == "LOCKED",
                )
            )
        if not isinstance(optional[2], BaseException):
            await session.execute(
                delete(FundingHoldingRecord).where(
                    FundingHoldingRecord.exchange_account_id == account.id
                )
            )
        await session.execute(
            delete(PositionRecord).where(PositionRecord.exchange_account_id == account.id)
        )
        session.add_all(
            [
                *spot_rows,
                *position_rows,
                *flexible_rows,
                *locked_rows,
                *funding_rows,
            ]
        )
        session.add(
            PortfolioSnapshotRecord(
                exchange_account_id=account.id,
                ts=datetime.now(UTC),
                spot_value=spot_value,
                futures_wallet=futures_wallet,
                available=available,
                unrealized_pnl=unrealized,
                earn_value=earn_value,
                funding_value=(
                    None if isinstance(optional[2], BaseException) else funding_value
                ),
                source_status_json=source_status,
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
            earn_value=earn_value,
            funding_value=(None if isinstance(optional[2], BaseException) else funding_value),
            positions_count=len(position_rows),
            data_quality=quality,
            source_status=source_status,
        )

    async def credentials(
        self,
        session: AsyncSession,
        exchange_account_id: uuid.UUID,
    ) -> tuple[str, str]:
        """Decrypt credentials only inside the portfolio boundary. / Расшифровывает ключи."""

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
        return self._vault.decrypt(
            encrypted,
            exchange_account_id=str(account.id),
            telegram_user_id=user.telegram_user_id,
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
        self._account_locks: dict[uuid.UUID, asyncio.Lock] = {}

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
                    group.create_task(self.reconcile_now(account_id))
            await asyncio.sleep(self._interval_seconds)

    async def reconcile_now(self, account_id: uuid.UUID) -> bool:
        """Run one serialized reconciliation. / Выполняет одну последовательную сверку."""

        lock = self._account_locks.setdefault(account_id, asyncio.Lock())
        async with lock, self._semaphore:
            try:
                async with self._database.session() as session, session.begin():
                    await self._service.reconcile(session, account_id)
                return True
            except BinanceCredentialError as exc:
                await self._mark_stale(account_id)
                log.warning(
                    "portfolio_reconcile_failed",
                    exchange_account_id=str(account_id),
                    error=type(exc).__name__,
                )
                return False
            except Exception as exc:
                log.warning(
                    "portfolio_reconcile_failed",
                    exchange_account_id=str(account_id),
                    error=type(exc).__name__,
                )
                return False

    async def _reconcile_one(self, account_id: uuid.UUID) -> None:
        """Compatibility wrapper for tests. / Совместимость со старыми тестами."""

        await self.reconcile_now(account_id)

    async def _mark_stale(self, account_id: uuid.UUID) -> None:
        """Commit STALE outside the failed sync. / Коммитит STALE вне неудачной сверки."""

        async with self._database.session() as session, session.begin():
            account = await session.get(ExchangeAccountRecord, account_id)
            if account is not None:
                account.status = "STALE"
            latest_snapshot = await session.scalar(
                select(PortfolioSnapshotRecord)
                .where(PortfolioSnapshotRecord.exchange_account_id == account_id)
                .order_by(PortfolioSnapshotRecord.ts.desc())
                .limit(1)
            )
            if latest_snapshot is not None:
                latest_snapshot.data_quality = "STALE"


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


def _optional_earn(
    payload: object,
    product_type: str,
    prices: dict[str, float],
    account_id: uuid.UUID,
) -> tuple[list[EarnHoldingRecord], Decimal, bool]:
    if isinstance(payload, BaseException) or not isinstance(payload, list):
        return [], ZERO, False
    rows: list[EarnHoldingRecord] = []
    total = ZERO
    partial = False
    for item in payload:
        if not isinstance(item, dict):
            continue
        asset = str(item.get("asset", ""))
        amount_key = "totalAmount" if product_type == "FLEXIBLE" else "amount"
        amount = _decimal(item.get(amount_key))
        if not asset or amount <= 0:
            continue
        value = _value_usdt(asset, amount, prices)
        partial = partial or value is None
        if value is not None:
            total += value
        product_id = (
            item.get("productId")
            if product_type == "FLEXIBLE"
            else item.get("positionId") or item.get("projectId")
        )
        rows.append(
            EarnHoldingRecord(
                exchange_account_id=account_id,
                asset=asset,
                product_type=product_type,
                product_id=str(product_id) if product_id is not None else None,
                amount=amount,
                value_usdt=value,
            )
        )
    return rows, total, partial


def _optional_funding(
    payload: object,
    prices: dict[str, float],
    account_id: uuid.UUID,
) -> tuple[list[FundingHoldingRecord], Decimal, bool]:
    if isinstance(payload, BaseException) or not isinstance(payload, list):
        return [], ZERO, False
    rows: list[FundingHoldingRecord] = []
    total = ZERO
    partial = False
    for item in payload:
        if not isinstance(item, dict):
            continue
        asset = str(item.get("asset", ""))
        amount = sum(
            (_decimal(item.get(key)) for key in ("free", "locked", "freeze", "withdrawing")),
            ZERO,
        )
        if not asset or amount <= 0:
            continue
        value = _value_usdt(asset, amount, prices)
        partial = partial or value is None
        if value is not None:
            total += value
        rows.append(
            FundingHoldingRecord(
                exchange_account_id=account_id,
                asset=asset,
                amount=amount,
                value_usdt=value,
            )
        )
    return rows, total, partial


def _optional_status(payload: object, partial: bool) -> str:
    if isinstance(payload, BaseException):
        return "UNAVAILABLE"
    return "PARTIAL" if partial else "FRESH"


def _value_usdt(
    asset: str,
    amount: Decimal,
    prices: dict[str, float],
) -> Decimal | None:
    if asset in {"USDT", "USDC", "FDUSD"}:
        return amount
    price = prices.get(f"{asset}USDT")
    return amount * Decimal(str(price)) if price is not None else None


def _decimal(value: object) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except InvalidOperation:
        return ZERO


def _optional_decimal(value: object) -> Decimal | None:
    parsed = _decimal(value)
    return parsed if parsed != 0 else None
