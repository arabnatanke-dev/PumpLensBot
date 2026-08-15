"""Real-time Binance account stream coordinator. / Координатор приватных потоков Binance."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
import websockets
from sqlalchemy import select

from pumplens.binance.signed_rest import BinanceReadOnlyClient
from pumplens.portfolio.service import PortfolioReconciler, PortfolioService
from pumplens.storage.db import Database
from pumplens.storage.models import ExchangeAccountRecord

log = structlog.get_logger(__name__)
FUTURES_PRIVATE_BASE = "wss://fstream.binance.com/private/ws"
SPOT_WEBSOCKET_API = "wss://ws-api.binance.com:443/ws-api/v3?returnRateLimits=false"


class PrivateAccountStreamManager:
    """Own one resilient Spot/Futures stream pair per account. / Ведёт пару потоков."""

    def __init__(
        self,
        database: Database,
        service: PortfolioService,
        reconciler: PortfolioReconciler,
        *,
        discovery_seconds: float = 5.0,
        reconcile_debounce_seconds: float = 1.0,
        listen_key_keepalive_seconds: int = 2_700,
        max_accounts: int = 50,
    ) -> None:
        self._database = database
        self._service = service
        self._reconciler = reconciler
        self._discovery_seconds = discovery_seconds
        self._debounce_seconds = reconcile_debounce_seconds
        self._keepalive_seconds = listen_key_keepalive_seconds
        self._max_accounts = max_accounts
        self._workers: dict[uuid.UUID, asyncio.Task[None]] = {}

    async def run(self) -> None:
        try:
            while True:
                account_ids = await self._active_account_ids()
                wanted = set(account_ids[: self._max_accounts])
                for account_id in wanted - self._workers.keys():
                    self._workers[account_id] = asyncio.create_task(
                        self._account_worker(account_id),
                        name=f"private-account-{account_id}",
                    )
                for account_id in set(self._workers) - wanted:
                    self._workers.pop(account_id).cancel()
                for account_id, task in tuple(self._workers.items()):
                    if task.done():
                        self._workers.pop(account_id)
                        _consume_task(task)
                await asyncio.sleep(self._discovery_seconds)
        finally:
            for task in self._workers.values():
                task.cancel()
            await asyncio.gather(*self._workers.values(), return_exceptions=True)
            self._workers.clear()

    async def _active_account_ids(self) -> list[uuid.UUID]:
        async with self._database.session() as session:
            return list(
                await session.scalars(
                    select(ExchangeAccountRecord.id)
                    .where(ExchangeAccountRecord.status.in_(["ACTIVE", "STALE"]))
                    .order_by(ExchangeAccountRecord.created_at)
                )
            )

    async def _account_worker(self, account_id: uuid.UUID) -> None:
        backoff = 1.0
        last_reconcile = 0.0

        async def changed() -> None:
            nonlocal last_reconcile
            now = time.monotonic()
            if now - last_reconcile < self._debounce_seconds:
                return
            last_reconcile = now
            await self._reconciler.reconcile_now(account_id)

        while True:
            try:
                async with self._database.session() as session:
                    api_key, secret_key = await self._service.credentials(session, account_id)
                async with BinanceReadOnlyClient(api_key, secret_key) as client:
                    await changed()
                    futures = asyncio.create_task(
                        self._futures_stream(client, changed),
                        name=f"futures-private-{account_id}",
                    )
                    spot = asyncio.create_task(
                        self._spot_stream(client, changed),
                        name=f"spot-private-{account_id}",
                    )
                    done, pending = await asyncio.wait(
                        (futures, spot),
                        # A graceful 24h close is still a reconnect signal.
                        # Даже штатное закрытие через 24ч требует переподключения.
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in pending:
                        task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                    for task in done:
                        task.result()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning(
                    "private_account_stream_reconnecting",
                    exchange_account_id=str(account_id),
                    error=type(exc).__name__,
                    retry_seconds=backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, 30.0)

    async def _futures_stream(
        self,
        client: BinanceReadOnlyClient,
        changed: Callable[[], Awaitable[None]],
    ) -> None:
        listen_key = await client.start_futures_user_stream()
        url = (
            f"{FUTURES_PRIVATE_BASE}?listenKey={listen_key}"
            "&events=ORDER_TRADE_UPDATE/ACCOUNT_UPDATE"
        )

        async def keepalive() -> None:
            while True:
                await asyncio.sleep(self._keepalive_seconds)
                await client.keepalive_futures_user_stream()

        try:
            async with websockets.connect(
                url,
                open_timeout=15,
                ping_interval=20,
                ping_timeout=60,
                max_queue=1_024,
            ) as websocket:
                keepalive_task = asyncio.create_task(keepalive())
                try:
                    async for raw in websocket:
                        payload = _json_object(raw)
                        event = payload.get("e")
                        if event in {"ACCOUNT_UPDATE", "ORDER_TRADE_UPDATE"}:
                            await changed()
                        elif event == "listenKeyExpired":
                            raise ConnectionError("futures_listen_key_expired")
                finally:
                    keepalive_task.cancel()
                    await asyncio.gather(keepalive_task, return_exceptions=True)
        finally:
            with contextlib.suppress(Exception):
                await client.close_futures_user_stream()

    async def _spot_stream(
        self,
        client: BinanceReadOnlyClient,
        changed: Callable[[], Awaitable[None]],
    ) -> None:
        async with websockets.connect(
            SPOT_WEBSOCKET_API,
            open_timeout=15,
            ping_interval=20,
            ping_timeout=60,
            max_queue=1_024,
        ) as websocket:
            request_id = str(uuid.uuid4())
            await websocket.send(
                json.dumps(
                    {
                        "id": request_id,
                        "method": "userDataStream.subscribe.signature",
                        "params": client.signed_websocket_params(),
                    }
                )
            )
            async for raw in websocket:
                payload = _json_object(raw)
                if payload.get("id") == request_id:
                    if payload.get("status") != 200:
                        raise ConnectionError("spot_user_stream_subscription_failed")
                    continue
                event = payload.get("event")
                if not isinstance(event, dict):
                    continue
                event_type = event.get("e")
                if event_type in {
                    "outboundAccountPosition",
                    "balanceUpdate",
                    "executionReport",
                    "externalLockUpdate",
                }:
                    await changed()
                elif event_type in {"eventStreamTerminated", "serverShutdown"}:
                    raise ConnectionError("spot_user_stream_terminated")


def _json_object(raw: str | bytes) -> dict[str, Any]:
    payload = json.loads(raw)
    return payload if isinstance(payload, dict) else {}


def _consume_task(task: asyncio.Task[None]) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        log.warning("private_account_worker_stopped", error=type(exc).__name__)
