"""Resilient public market WebSocket client. / Устойчивый клиент рыночных WebSocket."""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import structlog
import websockets

from pumplens.binance.normalizer import BinanceNormalizer
from pumplens.domain.events import MarketEvent

log = structlog.get_logger(__name__)

EventHandler = Callable[[MarketEvent], Awaitable[None]]


class MarketWebSocket:
    """Split routed streams and reconnect independently. / Разделяет routed-потоки."""

    def __init__(
        self,
        base_url: str,
        symbols: Sequence[str],
        normalizer: BinanceNormalizer,
        handler: EventHandler,
        *,
        streams_per_connection: int = 180,
        reconnect_max_seconds: float = 30.0,
        planned_rotation_seconds: float = 85_500.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._symbols = tuple(symbols)
        self._normalizer = normalizer
        self._handler = handler
        self._streams_per_connection = streams_per_connection
        self._reconnect_max_seconds = reconnect_max_seconds
        self._planned_rotation_seconds = planned_rotation_seconds

    async def run(self) -> None:
        market_groups = self._market_stream_groups()
        if not market_groups:
            raise RuntimeError("Cannot run WebSocket without symbols")
        async with asyncio.TaskGroup() as group:
            for index, streams in enumerate(market_groups):
                group.create_task(
                    self._run_group("market", index, streams),
                    name=f"market-ws-{index}",
                )
            # Binance routes high-frequency order-book feeds through /public.
            # Binance направляет высокочастотный стакан через /public.
            group.create_task(
                self._run_group("public", 0, ("!bookTicker",)),
                name="public-book-ws-0",
            )

    def _market_stream_groups(self) -> list[list[str]]:
        # One kline stream per symbol; all-market streams are added once.
        # Для каждого символа одна свеча; общерыночные потоки добавляются один раз.
        streams = [f"{symbol.lower()}@kline_1m" for symbol in self._symbols]
        groups = [
            streams[index : index + self._streams_per_connection]
            for index in range(0, len(streams), self._streams_per_connection)
        ]
        groups[0][0:0] = ["!ticker@arr", "!markPrice@arr@1s"]
        return groups

    async def _run_group(self, route: str, index: int, streams: Sequence[str]) -> None:
        attempt = 0
        while True:
            url = combined_stream_url(self._base_url, route, streams)
            try:
                await self._consume_connection(route, index, url)
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                delay = min(2**attempt, self._reconnect_max_seconds)
                # Randomness only spreads reconnects; it does not protect secrets.
                # Случайность только разводит reconnect по времени и не защищает секреты.
                delay *= random.uniform(0.8, 1.2)
                log.warning(
                    "market_ws_reconnect",
                    route=route,
                    connection=index,
                    delay_seconds=round(delay, 2),
                    error=type(exc).__name__,
                )
                await asyncio.sleep(delay)
                attempt = min(attempt + 1, 10)

    async def _consume_connection(self, route: str, index: int, url: str) -> None:
        started_at = time.monotonic()
        log.info("market_ws_connecting", route=route, connection=index)
        async with websockets.connect(
            url,
            open_timeout=20,
            close_timeout=10,
            ping_interval=150,
            ping_timeout=600,
            max_queue=4_096,
        ) as websocket:
            log.info("market_ws_connected", route=route, connection=index)
            async for raw in websocket:
                if time.monotonic() - started_at >= self._planned_rotation_seconds:
                    log.info("market_ws_planned_rotation", route=route, connection=index)
                    await websocket.close(code=1000, reason="planned rotation")
                    return
                payload: Any = json.loads(raw)
                for event in self._normalizer.normalize(payload):
                    await self._handler(event)


def combined_stream_url(base_url: str, route: str, streams: Sequence[str]) -> str:
    """Build a routed combined URL. / Формирует routed combined URL."""

    if route not in {"market", "public"}:
        raise ValueError(f"Unsupported Binance WebSocket route: {route}")
    if not streams:
        raise ValueError("At least one Binance WebSocket stream is required")
    return f"{base_url.rstrip('/')}/{route}/stream?streams={'/'.join(streams)}"


async def cancel_task(task: asyncio.Task[object]) -> None:
    """Cancel helper for graceful shutdown. / Помощник корректной остановки задачи."""

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
